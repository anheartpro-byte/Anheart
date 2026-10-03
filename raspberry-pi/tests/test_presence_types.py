"""The presence vocabulary: the confidence boundary, the frame-sequence gate, motion states."""

from __future__ import annotations

import math

import pytest

from src.presence.types import (
    CameraHealth,
    CapsuleState,
    Confidence,
    ConfidenceOutOfRange,
    MotionState,
    PresenceObservation,
    ZoneClear,
    parse_confidence,
)
from src.result import Err, Ok
from src.units import Monotonic


@pytest.mark.parametrize("value", [0.0, 0.3, 1.0])
def test_a_confidence_inside_the_unit_interval_parses(value: float) -> None:
    assert parse_confidence(value) == Ok(Confidence(value))


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, -math.inf])
def test_a_confidence_outside_the_unit_interval_or_not_finite_is_refused(value: float) -> None:
    parsed = parse_confidence(value)
    assert isinstance(parsed, Err)
    assert isinstance(parsed.error, ConfidenceOutOfRange)


def _frame(seq: int) -> PresenceObservation:
    return PresenceObservation(
        frame_seq=seq,
        at=Monotonic(0.0),
        health=CameraHealth.HEALTHY,
        zone=ZoneClear(),
        capsule=CapsuleState.EMPTY,
    )


def test_only_a_strictly_newer_frame_is_new_evidence() -> None:
    """The frozen camera repeats its frame: equal is not new, and neither is a restart."""
    assert _frame(0).is_new_evidence_after(None)
    assert _frame(5).is_new_evidence_after(4)
    assert not _frame(4).is_new_evidence_after(4)
    assert not _frame(0).is_new_evidence_after(4)


def test_only_a_machine_at_rest_rules_motion_out() -> None:
    assert not MotionState.AT_REST.motion_possible
    for state in (MotionState.ARMED, MotionState.TURNING, MotionState.UNKNOWN):
        assert state.motion_possible, state
