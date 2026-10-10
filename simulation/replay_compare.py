"""Pure comparison of independently supplied, already parsed decision sequences."""

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import zip_longest
from types import MappingProxyType
from typing import NewType

from simulation.replay_report import (
    SETPOINT_TOLERANCE_RPM,
    VERDICT_TOLERANCE_TICKS,
    ComparisonReport,
    DecisionSummary,
    Difference,
    DifferenceCode,
    printable_rule,
)
from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, Seconds

RuleKey = NewType("RuleKey", str)


@dataclass(frozen=True, slots=True)
class SafetyDecision:
    """Rule strings are compared whole; a report shows one only if it is a plain identifier."""

    action: SafetyAction
    rule: RuleKey | None


@dataclass(frozen=True, slots=True)
class Decision:
    """Entry time and output for one tick, with finite, parsed domain values."""

    t: Seconds
    setpoint: MotorRpm
    phase: Phase
    safety: SafetyDecision

    def summary(self) -> DecisionSummary:
        return DecisionSummary(
            self.t,
            self.setpoint,
            self.phase,
            self.safety.action,
            printable_rule(self.safety.rule),
        )


@dataclass(frozen=True, slots=True)
class _Transition:
    index: int
    before: SafetyDecision
    after: SafetyDecision


def _transitions(rows: tuple[Decision, ...]) -> Iterator[_Transition]:
    for index in range(1, len(rows)):
        before, after = rows[index - 1].safety, rows[index].safety
        if before != after:
            yield _Transition(index, before, after)


@dataclass(frozen=True, slots=True)
class _Finding:
    code: DifferenceCode
    index: int
    expected_transition: int | None = None
    actual_transition: int | None = None


@dataclass(frozen=True, slots=True)
class _SafetyAlignment:
    boundaries: Mapping[int, tuple[SafetyDecision, SafetyDecision]]
    shifts: tuple[int, int]


def _align_safety(
    expected: tuple[Decision, ...],
    actual: tuple[Decision, ...],
    record: Callable[[_Finding], None],
) -> _SafetyAlignment:
    boundaries: dict[int, tuple[SafetyDecision, SafetyDecision]] = {}
    shifts = [0, 0]
    expected_transitions, actual_transitions = (
        tuple(_transitions(expected)),
        tuple(_transitions(actual)),
    )
    for ordinal, left in enumerate(expected_transitions):
        if ordinal >= len(actual_transitions):
            record(_Finding(DifferenceCode.TRANSITION_COUNT, left.index, left.index, None))
            continue
        right = actual_transitions[ordinal]
        index = min(left.index, right.index)
        if (left.before, left.after) != (right.before, right.after):
            record(_Finding(DifferenceCode.TRANSITION_STATE, index, left.index, right.index))
            continue
        delta = right.index - left.index
        if abs(delta) > VERDICT_TOLERANCE_TICKS:
            record(_Finding(DifferenceCode.TRANSITION_TIMING, index, left.index, right.index))
            continue
        if delta:
            shifts[int(delta > 0)] += 1
            boundaries[index] = (
                (left.after, right.before) if delta > 0 else (left.before, right.after)
            )
    for right in actual_transitions[len(expected_transitions) :]:
        record(_Finding(DifferenceCode.TRANSITION_COUNT, right.index, None, right.index))
    return _SafetyAlignment(MappingProxyType(boundaries), (shifts[0], shifts[1]))


type _Cells = tuple[float, int, str, str, str | None]


def _cells(row: Decision | None) -> _Cells | None:
    if row is None:
        return None
    return (row.t, row.setpoint, row.phase.value, row.safety.action.name, row.safety.rule)


def _deviations(expected: tuple[Decision, ...], actual: tuple[Decision, ...]) -> tuple[int, str]:
    """How many ticks are not identical on both sides, and a digest of every one of them.

    The report details the FIRST difference and counts the others by kind. That
    leaves room: a second difference can be swapped for another of its kind, a
    tolerated rpm can move to another tick, and the counts do not notice. The
    digest does: it takes the tick, the recorded decision and the replayed one,
    for every tick where the two are not equal, tolerated or not, and the
    number of ticks on each side. A tick one side has and the other has not
    (the record goes on after a replay stopped) is such a tick, with nothing
    on the side that lacks it. Two replays with the same digest departed from
    their record at the same ticks, from the same recorded decisions to the
    same replayed ones, and left the same recorded ticks unreached. Rule names
    go into the digest whole and come out of it as hexadecimal: nothing of the
    record is printed.
    """
    digest = hashlib.sha256(f"{len(expected)}|{len(actual)}\n".encode())
    deviating = 0
    for index, (left, right) in enumerate(zip_longest(expected, actual)):
        if left != right:
            deviating += 1
            digest.update((json.dumps([index, _cells(left), _cells(right)]) + "\n").encode())
    return deviating, digest.hexdigest()


def compare_decisions(
    expected: tuple[Decision, ...],
    actual: tuple[Decision, ...],
    *,
    unreached: tuple[Decision, ...] = (),
) -> ComparisonReport:
    """Compare exact entry grids/phases, ±1 motor rpm, and ordinal transitions.

    Initial safety states must match: no pre-sequence state is inferred. Every
    transition, including clearing, has exactly one ordinal partner. Only that
    pair's one-index boundary can excuse its precise before/after tuple mismatch.
    Input parsing, causal completeness and transport failures belong to callers.

    ``unreached`` is what the record holds after ``expected``, when the caller
    stopped before it (a replay that diverged). Nothing is compared to it and
    no finding comes from it: it only counts among the ticks not replayed as
    recorded, and goes into their fingerprint, so that a report also stands
    for the part of the record it could not replay.
    """
    counts: dict[DifferenceCode, int] = {}
    first: _Finding | None = None

    def record(finding: _Finding) -> None:
        nonlocal first
        counts[finding.code] = counts.get(finding.code, 0) + 1
        if first is None or finding.index < first.index:
            first = finding

    if len(expected) != len(actual):
        record(_Finding(DifferenceCode.TICK_COUNT, min(len(expected), len(actual))))

    alignment = _align_safety(expected, actual, record)
    tolerated = [0, 0]
    for index, (left_row, right_row) in enumerate(zip(expected, actual, strict=False)):
        if left_row.t != right_row.t:
            record(_Finding(DifferenceCode.TICK_GRID, index))
        delta_rpm = right_row.setpoint - left_row.setpoint
        if abs(delta_rpm) > SETPOINT_TOLERANCE_RPM:
            record(_Finding(DifferenceCode.SETPOINT, index))
        elif delta_rpm:
            tolerated[int(delta_rpm > 0)] += 1
        if left_row.phase != right_row.phase:
            record(_Finding(DifferenceCode.PHASE, index))
        pair = (left_row.safety, right_row.safety)
        if left_row.safety != right_row.safety and alignment.boundaries.get(index) != pair:
            record(_Finding(DifferenceCode.SAFETY_STATE, index))

    difference: Difference | None = None
    if first is not None:
        expected_first = expected[first.index] if first.index < len(expected) else None
        actual_first = actual[first.index] if first.index < len(actual) else None
        location = (expected if first.index < len(expected) else actual)[first.index]
        difference = Difference(
            code=first.code,
            index=first.index,
            t=location.t,
            expected=None if expected_first is None else expected_first.summary(),
            actual=None if actual_first is None else actual_first.summary(),
            rule_changed=(
                expected_first is not None
                and actual_first is not None
                and expected_first.safety.rule != actual_first.safety.rule
            ),
            expected_transition=first.expected_transition,
            actual_transition=first.actual_transition,
        )
    deviating, fingerprint = _deviations((*expected, *unreached), actual)
    return ComparisonReport(
        len(expected),
        len(actual),
        difference,
        tuple((code, counts[code]) for code in DifferenceCode if code in counts),
        (tolerated[0], tolerated[1]),
        alignment.shifts,
        deviating,
        fingerprint,
    )
