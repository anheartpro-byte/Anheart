"""Bounded comparison reports: only decision numbers, enum values and rule equality."""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, Seconds


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


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    t: Seconds
    setpoint: MotorRpm
    phase: Phase
    action: SafetyAction

    def document(self) -> SummaryDocument:
        return {
            "t": self.t,
            "setpoint": self.setpoint,
            "phase": self.phase.value,
            "action": self.action.name,
        }


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


class ReportDocument(TypedDict):
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

    def document(self) -> ReportDocument:
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
        status = "match" if self.matches else "divergence"
        return (
            f"decision comparison: {status}\n"
            + json.dumps(self.document(), sort_keys=True, indent=2)
            + "\n"
        )
