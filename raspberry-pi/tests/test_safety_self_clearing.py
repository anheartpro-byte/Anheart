"""Every unlatched verdict says that it lifts by itself; no latched one does (ANH-176).

The console shows a verdict's ``detail`` verbatim. While a speed is only held
(FREEZE) or lowered (REDUCE) by a warning that is not latched, the arm can
speed up again with nobody clicking the moment the warning's cause ends, and
"held" reads as "stopped for good" to somebody about to walk up to it. So the
supervisor appends :data:`~src.training.safety.SELF_CLEARING` to the detail of
every unlatched verdict, in one place, and to no latched one.

Each case below produces the verdict the way its own rule does, on the
supervisor rig of ``tests/test_safety.py``, whose session is in HOLD: a phase
in which the speed can still be asked to rise. No threshold is touched: the
cases only choose evidence. That the sentence is left off where it would be
untrue (a session that is ending, or that has stopped by itself) is proved in
``tests/test_safety_session_standstill.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import pytest

from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_CURRENT_HIGH,
    RULE_HR_HARD_MAX,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    RULE_LOOP_STALL,
    SELF_CLEARING,
)
from src.training.types import SafetyAction, SafetyVerdict
from src.units import Amperes, Bpm, MotorRpm, Seconds
from tests.test_safety import HARD_MAX, LIMITS, Rig

Build = Callable[[], Rig]


def _heart_rate_gone(seconds: float) -> Rig:
    """No fresh heart rate for ``seconds``: FREEZE past 10 s, REDUCE past 30 s, latched past 60."""
    rig = Rig()
    rig.tick()
    rig.fresh = False
    rig.run(Seconds(seconds))
    return rig


def _attendant_gone(seconds: float) -> Rig:
    """No presence ping for ``seconds``: FREEZE past 60 s, latched past 120 s."""
    rig = Rig()
    rig.tick()
    rig.attendant_last_seen = rig.clock.monotonic()
    rig.attendant_present = False
    rig.run(Seconds(seconds))
    return rig


def _heart_rate_rising() -> Rig:
    """Half a bpm per second for 30 s: 30 bpm/min against a bound of 25."""
    rig = Rig()
    for step in range(round(30.0 / rig.limits.control_period)):
        rig.bpm = Bpm(100 + step // 10)
        rig.tick()
    return rig


def _heart_not_following() -> Rig:
    """Five minutes of a speed ramp to 1200 motor rpm (~1 g) with a flat heart rate."""
    rig = Rig()
    for second in range(300):
        rig.commanded = MotorRpm(1200 * (second + 1) // 300)
        rig.measured = rig.commanded
        rig.tick(Seconds(1.0))
    return rig


def _current_above(amperes: float, seconds: float) -> Rig:
    """The motor current at ``amperes`` for ``seconds``: warning at 2.4 A, trip at 3.2 A."""
    rig = Rig()
    rig.current = Amperes(amperes)
    rig.run(Seconds(seconds))
    return rig


def _heart_rate_above_the_hard_maximum() -> Rig:
    rig = Rig()
    rig.bpm = Bpm(HARD_MAX + 5)
    rig.run(Seconds(6.0))
    return rig


def _recovered_stall() -> Rig:
    """One tick 0.7 s late on the real limits: ``loop_stall`` latches a FREEZE."""
    rig = Rig(LIMITS)
    rig.tick()
    rig.tick(Seconds(0.7))
    return rig


UNLATCHED: Final[tuple[tuple[str, Build, str, SafetyAction], ...]] = (
    ("hr_stale holds", lambda: _heart_rate_gone(12.0), RULE_HR_STALE, SafetyAction.FREEZE),
    ("hr_stale lowers", lambda: _heart_rate_gone(32.0), RULE_HR_STALE, SafetyAction.REDUCE),
    (
        "attendant_absent holds",
        lambda: _attendant_gone(62.0),
        RULE_ATTENDANT_ABSENT,
        SafetyAction.FREEZE,
    ),
    ("hr_rate lowers", _heart_rate_rising, RULE_HR_RATE, SafetyAction.REDUCE),
    ("hr_unresponsive lowers", _heart_not_following, RULE_HR_UNRESPONSIVE, SafetyAction.REDUCE),
    (
        "current_high lowers",
        lambda: _current_above(2.5, 11.0),
        RULE_CURRENT_HIGH,
        SafetyAction.REDUCE,
    ),
)
"""Every verdict a rule can raise without latching it: the whole list, by reading safety.py."""

LATCHED: Final[tuple[tuple[str, Build, str, SafetyAction], ...]] = (
    ("hr_stale ends", lambda: _heart_rate_gone(62.0), RULE_HR_STALE, SafetyAction.RAMP_DOWN),
    (
        "attendant_absent ends",
        lambda: _attendant_gone(122.0),
        RULE_ATTENDANT_ABSENT,
        SafetyAction.RAMP_DOWN,
    ),
    (
        "current_high trips",
        lambda: _current_above(3.3, 0.2),
        RULE_CURRENT_HIGH,
        SafetyAction.RAMP_DOWN,
    ),
    (
        "hr_hard_max ends",
        _heart_rate_above_the_hard_maximum,
        RULE_HR_HARD_MAX,
        SafetyAction.RAMP_DOWN,
    ),
    ("loop_stall holds, latched", _recovered_stall, RULE_LOOP_STALL, SafetyAction.FREEZE),
)


def _raised(rig: Rig, rule: str) -> SafetyVerdict:
    verdict = rig.verdict_for(rule)
    assert verdict is not None, f"{rule} is not firing: {rig.live()}"
    return verdict


@pytest.mark.parametrize(
    ("build", "rule", "action"),
    [case[1:] for case in UNLATCHED],
    ids=[case[0] for case in UNLATCHED],
)
def test_an_unlatched_verdict_says_that_it_lifts_by_itself(
    build: Build, rule: str, action: SafetyAction
) -> None:
    """Held or lowered, never latched: the sentence on the screen ends with the warning."""
    rig = build()
    verdict = _raised(rig, rule)
    assert verdict.action is action
    assert verdict.latched is False
    assert verdict.detail.endswith(SELF_CLEARING)
    assert verdict.detail.count(SELF_CLEARING) == 1
    # Its own sentence still comes first, untouched.
    assert len(verdict.detail) > len(SELF_CLEARING) + 20
    # What stands is that same verdict: nothing latched, so the floor is empty.
    assert rig.standing() == verdict
    assert rig.floor() is None


@pytest.mark.parametrize(
    ("build", "rule", "action"),
    [case[1:] for case in LATCHED],
    ids=[case[0] for case in LATCHED],
)
def test_a_latched_verdict_never_says_it(build: Build, rule: str, action: SafetyAction) -> None:
    """A latched verdict stands until a named operator clears it: nothing resumes behind it."""
    rig = build()
    verdict = _raised(rig, rule)
    assert verdict.action is action
    assert verdict.latched is True
    assert SELF_CLEARING not in verdict.detail
    floor = rig.floor()
    assert floor is not None
    assert SELF_CLEARING not in floor.detail


def test_the_sentence_says_what_an_operator_needs_before_walking_up_to_the_arm() -> None:
    """Pinned in words, because it is the one line of this module written for the screen."""
    assert SELF_CLEARING.startswith("; NOT LATCHED: ")
    assert "lifts by itself" in SELF_CLEARING
    assert "follows the programme or the manual target again" in SELF_CLEARING
    assert "upwards too" in SELF_CLEARING
    assert "nobody clicking" in SELF_CLEARING
    # Unconditional, because it is only shown where it is true (see
    # tests/test_safety_session_standstill.py): no "if the session is still running".
    assert "still running" not in SELF_CLEARING


def test_the_warning_goes_with_the_verdict_and_leaves_when_it_is_latched() -> None:
    """One rule, three levels: the sentence is there at FREEZE and REDUCE and gone at RAMP_DOWN."""
    rig = _heart_rate_gone(12.0)
    assert _raised(rig, RULE_HR_STALE).detail.endswith(SELF_CLEARING)
    rig.run(Seconds(20.0))
    lowered = _raised(rig, RULE_HR_STALE)
    assert lowered.action is SafetyAction.REDUCE
    assert lowered.detail.endswith(SELF_CLEARING)
    rig.run(Seconds(30.0))
    ended = _raised(rig, RULE_HR_STALE)
    assert ended.action is SafetyAction.RAMP_DOWN
    assert SELF_CLEARING not in ended.detail
    # And when the evidence returns, the unlatched levels are gone entirely.
    rig.fresh = True
    rig.tick()
    assert rig.verdict_for(RULE_HR_STALE) is None
