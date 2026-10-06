"""What a replay reports: decision numbers, enum values and vetted rule names, nothing else.

A report is printed in CI logs of a public repository, about a record that may
come from a real session. So it carries no free text from the record: a
recorded rule name is shown only when it is a plain identifier
(:func:`printable_rule`), and everything else is a number or an enum value the
code under test defines.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict

from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, Seconds

SETPOINT_TOLERANCE_RPM: Final[int] = 1
"""EX-2: a replayed setpoint within this many motor rpm of the recorded one is the same."""

VERDICT_TOLERANCE_TICKS: Final[int] = 1
"""EX-2: a safety transition this many recorded ticks early or late is the same."""

_RULE: Final = re.compile(r"[a-z][a-z0-9_]{0,47}")
UNPRINTABLE_RULE: Final[str] = "<unprintable>"


def printable_rule(rule: str | None) -> str | None:
    """``rule`` if it is a plain rule identifier, else a placeholder. ``None`` stays ``None``."""
    if rule is None or _RULE.fullmatch(rule) is not None:
        return rule
    return UNPRINTABLE_RULE


class DifferenceCode(StrEnum):
    TICK_COUNT = "tick_count"
    TICK_GRID = "tick_grid"
    SETPOINT = "setpoint"
    PHASE = "phase"
    TRANSITION_COUNT = "transition_count"
    TRANSITION_STATE = "transition_state"
    TRANSITION_TIMING = "transition_timing"
    SAFETY_STATE = "safety_state"


class SummaryDocument(TypedDict):
    t: float
    setpoint: int
    phase: str
    action: str
    rule: str | None


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    t: Seconds
    setpoint: MotorRpm
    phase: Phase
    action: SafetyAction
    rule: str | None
    """Already through :func:`printable_rule`."""

    def document(self) -> SummaryDocument:
        return {
            "t": self.t,
            "setpoint": self.setpoint,
            "phase": self.phase.value,
            "action": self.action.name,
            "rule": self.rule,
        }

    def sentence(self) -> str:
        rule = "" if self.rule is None else f" ({self.rule})"
        return (
            f"setpoint {self.setpoint} motor rpm, phase {self.phase.value}, "
            f"safety {self.action.name}{rule}"
        )


class DifferenceDocument(TypedDict):
    code: str
    index: int
    t: float
    expected: SummaryDocument | None
    actual: SummaryDocument | None
    rule_changed: bool
    expected_transition: int | None
    actual_transition: int | None


@dataclass(frozen=True, slots=True)
class Difference:
    code: DifferenceCode
    index: int
    t: Seconds
    expected: DecisionSummary | None
    actual: DecisionSummary | None
    rule_changed: bool
    expected_transition: int | None
    actual_transition: int | None

    def document(self) -> DifferenceDocument:
        return {
            "code": self.code.value,
            "index": self.index,
            "t": self.t,
            "expected": None if self.expected is None else self.expected.document(),
            "actual": None if self.actual is None else self.actual.document(),
            "rule_changed": self.rule_changed,
            "expected_transition": self.expected_transition,
            "actual_transition": self.actual_transition,
        }


class ComparisonDocument(TypedDict):
    matches: bool
    expected_ticks: int
    actual_ticks: int
    first: DifferenceDocument | None
    counts: dict[str, int]
    tolerated_rpm: tuple[int, int]
    shifted_transitions: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Counts are failed checks; tolerance pairs count deltas (-1, +1).

    The first difference uses the earliest index, with transition checks winning
    same-index safety ties. This is a decision comparison, not replay completeness.
    """

    expected_ticks: int
    actual_ticks: int
    first: Difference | None
    counts: tuple[tuple[DifferenceCode, int], ...]
    tolerated_rpm: tuple[int, int]
    shifted_transitions: tuple[int, int]

    @property
    def matches(self) -> bool:
        return self.first is None

    def document(self) -> ComparisonDocument:
        return {
            "matches": self.matches,
            "expected_ticks": self.expected_ticks,
            "actual_ticks": self.actual_ticks,
            "first": None if self.first is None else self.first.document(),
            "counts": {code.value: count for code, count in self.counts},
            "tolerated_rpm": self.tolerated_rpm,
            "shifted_transitions": self.shifted_transitions,
        }

    def to_json(self) -> str:
        return json.dumps(self.document(), sort_keys=True, separators=(",", ":")) + "\n"

    def to_text(self) -> str:
        status = "match" if self.matches else "difference"
        return (
            f"decision comparison: {status}\n"
            + json.dumps(self.document(), sort_keys=True, indent=2)
            + "\n"
        )


# =========================================================================
# The whole replay
# =========================================================================


class Outcome(StrEnum):
    MATCH = "match"
    """Every replayed decision is the recorded one, within the tolerances."""

    DIFFERENCE = "difference"
    """The runtime asked the drive for what the record holds, and decided otherwise (EX-2)."""

    DIVERGENCE = "divergence"
    """The runtime asked the drive for something the record does not hold (EX-3)."""


class DivergenceDocument(TypedDict):
    t: float
    tick: int
    requested: str
    recorded: str


@dataclass(frozen=True, slots=True)
class Divergence:
    """Where the replay had to stop: the recorded answers no longer fit the questions."""

    t: Seconds
    """The replay instant, on the record's axis."""

    tick: int
    """How many recorded ticks had been replayed in full."""

    requested: str
    """What the runtime did, in the tape's vocabulary (no text from the record)."""

    recorded: str
    """What the record holds at that point, in the same vocabulary."""

    def document(self) -> DivergenceDocument:
        return {
            "t": self.t,
            "tick": self.tick,
            "requested": self.requested,
            "recorded": self.recorded,
        }


class ToleranceDocument(TypedDict):
    setpoint_motor_rpm: int
    verdict_ticks: int


class ReplayDocument(TypedDict):
    record: str
    outcome: str
    recorded_ticks: int
    tolerances: ToleranceDocument
    comparison: ComparisonDocument
    divergence: DivergenceDocument | None
    integrity: list[str]


@dataclass(frozen=True, slots=True)
class ReplayReport:
    """One record, replayed against the runtime of this checkout."""

    record: str
    """The manifest's opaque ``record_id``."""

    recorded_ticks: int
    comparison: ComparisonReport
    """Recorded against replayed decisions, over the ticks that were replayed."""

    divergence: Divergence | None
    integrity: tuple[str, ...] = ()
    """The reader's warnings (``file: code``): a record altered after it was closed says so."""

    @property
    def outcome(self) -> Outcome:
        if self.divergence is not None:
            return Outcome.DIVERGENCE
        return Outcome.MATCH if self.comparison.matches else Outcome.DIFFERENCE

    @property
    def matches(self) -> bool:
        return self.outcome is Outcome.MATCH

    def document(self) -> ReplayDocument:
        return {
            "record": self.record,
            "outcome": self.outcome.value,
            "recorded_ticks": self.recorded_ticks,
            "tolerances": {
                "setpoint_motor_rpm": SETPOINT_TOLERANCE_RPM,
                "verdict_ticks": VERDICT_TOLERANCE_TICKS,
            },
            "comparison": self.comparison.document(),
            "divergence": None if self.divergence is None else self.divergence.document(),
            "integrity": list(self.integrity),
        }

    def to_json(self) -> str:
        """One line, keys sorted: byte-identical for identical replays (EX-7)."""
        return json.dumps(self.document(), sort_keys=True, separators=(",", ":")) + "\n"

    def to_text(self) -> str:
        """The report for a person: the verdict first, then where and what."""
        comparison = self.comparison
        lines = [
            f"replay of record {self.record}: {self.outcome.value.upper()}",
            f"  ticks: {self.recorded_ticks} recorded, {comparison.actual_ticks} replayed",
            f"  tolerances: setpoint +/-{SETPOINT_TOLERANCE_RPM} motor rpm, verdict instant "
            f"+/-{VERDICT_TOLERANCE_TICKS} tick, phase and rule exact",
        ]
        first = comparison.first
        if first is not None:
            lines.append(f"  first difference: {first.code.value} at t={first.t:.3f} s")
            lines.append(f"    tick index {first.index}")
            if first.expected is not None:
                lines.append(f"    recorded: {first.expected.sentence()}")
            if first.actual is not None:
                lines.append(f"    replayed: {first.actual.sentence()}")
            counts = ", ".join(f"{code.value} {count}" for code, count in comparison.counts)
            lines.append(f"  failed checks by kind: {counts}")
        lower, upper = comparison.tolerated_rpm
        earlier, later = comparison.shifted_transitions
        lines.append(
            f"  tolerated: {lower + upper} setpoints one rpm apart, "
            f"{earlier + later} verdicts one tick apart"
        )
        divergence = self.divergence
        if divergence is not None:
            lines.append(
                f"  divergence at t={divergence.t:.3f} s, after {divergence.tick} ticks: "
                f"the runtime requested {divergence.requested}; "
                f"the record holds {divergence.recorded}"
            )
            lines.append("    the replay stopped there: later recorded answers fit no question")
        lines.extend(f"  integrity warning: {warning}" for warning in self.integrity)
        return "\n".join(lines) + "\n"
