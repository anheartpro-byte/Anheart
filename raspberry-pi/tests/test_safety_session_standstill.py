"""``session_standstill``: the supervisor's side of "a stopped arm never restarts by itself".

The runtime states a fact in its observation
(:attr:`~src.training.safety.SafetyObservation.stopped_by`): inside this
session, after the arm had moved, the setpoint came back to zero and nobody had
asked for that. This rule turns the statement into a latched ``RAMP_DOWN``, so
that the ending is a verdict of the supervisor like any other: on the floor the
status page and the start gate read, cleared by the same named acknowledgement
(ANH-176, product decisions of 2026-10-05 and 2026-10-06).

``tests/test_runtime_standstill.py`` proves WHEN the runtime makes the
statement, on the real tick. This file proves what the rule does with it, on
the supervisor rig of ``tests/test_safety.py``: when it fires, for how long it
holds, what an acknowledgement does at each point, and that the sentence an
unlatched verdict carries (``SELF_CLEARING``) is only there while it is true.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.result import Err, Ok
from src.training.safety import (
    ALL_RULES,
    CAN_STILL_MOVE,
    RULE_HR_STALE,
    RULE_SESSION_STANDSTILL,
    SELF_CLEARING,
    NothingLatched,
    SafetyObservation,
)
from src.training.types import Phase, SafetyAction, SafetyVerdict
from src.units import Seconds
from tests.test_safety import Rig

OPERATOR = "dr-mensah"
WARNING = "the warning hr_stale"
REGULATION = "the heart-rate regulation"

MOVING = (Phase.BASELINE, Phase.WARMUP, Phase.HOLD)
ENDING = (Phase.COOLDOWN, Phase.RECOVERY)


def _tick(rig: Rig, stopped_by: str | None, phase: Phase = Phase.HOLD) -> SafetyVerdict | None:
    """One control period with the runtime's statement set to ``stopped_by``."""
    rig.phase = phase
    rig.clock.advance(rig.limits.control_period)
    rig.elapsed = Seconds(rig.elapsed + rig.limits.control_period)
    rig.seq += 1
    observation: SafetyObservation = replace(rig.observation(), stopped_by=stopped_by)
    return rig.supervisor.evaluate(observation)


def _fired(rig: Rig) -> SafetyVerdict | None:
    return rig.verdict_for(RULE_SESSION_STANDSTILL)


def _floor_rule(rig: Rig) -> str | None:
    floor = rig.floor()
    return None if floor is None else floor.rule


# =========================================================================
# When it fires
# =========================================================================


def test_the_rule_is_one_of_the_supervisor_s_own() -> None:
    """A tracker is allocated per entry of ALL_RULES: a rule outside it raises in the tick."""
    assert RULE_SESSION_STANDSTILL == "session_standstill"
    assert RULE_SESSION_STANDSTILL in ALL_RULES


def test_an_observation_says_nothing_stopped_the_arm_unless_the_runtime_states_it() -> None:
    """The default is ``None``: an observation built without the field ends no session."""
    rig = Rig()
    assert rig.observation().stopped_by is None
    rig.run(Seconds(30.0))
    assert _fired(rig) is None
    assert rig.standing() is None


@pytest.mark.parametrize("cause", [WARNING, REGULATION])
def test_a_stated_standstill_latches_a_ramp_down_on_the_first_tick(cause: str) -> None:
    """No dwell: the fact is the runtime's, already acknowledged by the drive."""
    rig = Rig()
    rig.tick()
    assert rig.standing() is None

    verdict = _tick(rig, cause)
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_SESSION_STANDSTILL,
        SafetyAction.RAMP_DOWN,
        True,
    )
    assert verdict.since == rig.clock.monotonic()
    assert f"{cause} brought the setpoint to zero" in verdict.detail
    assert "the session has ended" in verdict.detail
    assert "a stopped arm never restarts by itself" in verdict.detail
    assert "new start" in verdict.detail
    assert SELF_CLEARING not in verdict.detail
    assert rig.floor() == verdict
    assert rig.standing() == verdict
    assert _fired(rig) == verdict


@pytest.mark.parametrize("phase", [*MOVING, *ENDING])
def test_it_fires_in_every_phase_of_a_session_that_is_not_over(phase: Phase) -> None:
    """The statement is about the session, so the rule does not ask which phase it is in."""
    rig = Rig()
    verdict = _tick(rig, WARNING, phase)
    assert verdict is not None
    assert verdict.rule == RULE_SESSION_STANDSTILL


def test_it_does_not_fire_on_a_session_that_is_over() -> None:
    """DONE: nothing is being ended any more. What stands then is the floor, if it was raised."""
    rig = Rig()
    assert _tick(rig, WARNING, Phase.DONE) is None
    assert _fired(rig) is None
    assert rig.floor() is None


# =========================================================================
# For how long it holds, and what an acknowledgement does
# =========================================================================


def test_the_verdict_keeps_its_first_instant_while_the_session_ends() -> None:
    rig = Rig()
    first = _tick(rig, WARNING)
    assert first is not None
    for phase in (Phase.COOLDOWN, Phase.COOLDOWN, Phase.RECOVERY, Phase.RECOVERY):
        again = _tick(rig, WARNING, phase)
        assert again is not None
        assert again.rule == RULE_SESSION_STANDSTILL
        assert again.since == first.since


def test_an_acknowledgement_before_the_session_is_over_is_taken_back_on_the_next_tick() -> None:
    """Allowed and harmless, as for any rule whose condition is still true."""
    rig = Rig()
    _tick(rig, WARNING)
    _tick(rig, WARNING, Phase.COOLDOWN)

    early = rig.supervisor.acknowledge(OPERATOR)
    assert isinstance(early, Ok)
    assert early.value.cleared == (RULE_SESSION_STANDSTILL,)
    assert rig.floor() is None

    _tick(rig, WARNING, Phase.RECOVERY)
    assert _floor_rule(rig) == RULE_SESSION_STANDSTILL
    assert rig.standing_action() is SafetyAction.RAMP_DOWN


def test_once_the_session_is_over_the_floor_stands_until_a_named_acknowledgement() -> None:
    """DONE releases the rule and leaves the latch: cleared once, by name, and it stays cleared."""
    rig = Rig()
    _tick(rig, WARNING)
    _tick(rig, WARNING, Phase.RECOVERY)

    for _ in range(10):
        _tick(rig, WARNING, Phase.DONE)
    assert _fired(rig) is None, "the rule still fires on a session that is over"
    assert _floor_rule(rig) == RULE_SESSION_STANDSTILL
    assert rig.standing_action() is SafetyAction.RAMP_DOWN

    acknowledged = rig.supervisor.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL,)
    for _ in range(10):
        assert _tick(rig, WARNING, Phase.DONE) is None
    assert rig.floor() is None
    again = rig.supervisor.acknowledge(OPERATOR)
    assert isinstance(again, Err)
    assert isinstance(again.error, NothingLatched)


def test_a_new_session_that_states_nothing_is_not_ended_by_the_previous_one() -> None:
    """The runtime forgets the statement when it arms again: the rule is quiet from tick one."""
    rig = Rig()
    _tick(rig, REGULATION)
    _tick(rig, REGULATION, Phase.DONE)
    assert isinstance(rig.supervisor.acknowledge(OPERATOR), Ok)

    for phase in MOVING:
        assert _tick(rig, None, phase) is None
    assert rig.floor() is None


def test_a_statement_that_returns_starts_a_new_verdict_with_a_new_instant() -> None:
    """Release is complete: a later standstill is a later fact, dated when it happened."""
    rig = Rig()
    first = _tick(rig, WARNING)
    assert first is not None
    _tick(rig, WARNING, Phase.DONE)
    assert isinstance(rig.supervisor.acknowledge(OPERATOR), Ok)
    _tick(rig, None, Phase.BASELINE)

    second = _tick(rig, REGULATION, Phase.HOLD)
    assert second is not None
    assert second.since > first.since
    assert REGULATION in second.detail


# =========================================================================
# The sentence of an unlatched verdict: only where it is true
# =========================================================================


def _heart_rate_gone(rig: Rig, stopped_by: str | None, phase: Phase) -> SafetyVerdict:
    """Twelve seconds with no fresh heart rate in ``phase``: ``hr_stale`` holds, unlatched."""
    _tick(rig, stopped_by, phase)
    held: SafetyVerdict | None = None
    for _ in range(round(12.0 / rig.limits.control_period)):
        rig.seq -= 1  # _tick advances it: take it back, so that no reading is new
        _tick(rig, stopped_by, phase)
    held = rig.verdict_for(RULE_HR_STALE)
    assert held is not None, f"hr_stale is not firing in {phase}: {rig.live()}"
    assert held.latched is False
    return held


def test_every_phase_is_on_one_side_of_the_line_or_the_other() -> None:
    """A set, not an exhaustive match: so a phase added later must be placed HERE, by hand.

    Nothing reached from ``evaluate`` may raise, which is why the supervisor
    does not match on the phase. This test is what a new phase fails instead.
    """
    assert frozenset(MOVING) == CAN_STILL_MOVE
    assert set(Phase) == CAN_STILL_MOVE | {*ENDING, Phase.DONE}


@pytest.mark.parametrize("phase", MOVING)
def test_a_warning_says_it_lifts_by_itself_while_the_session_can_still_move(phase: Phase) -> None:
    """BASELINE counts: nothing turns yet, and the programme starts the arm by itself after it."""
    held = _heart_rate_gone(Rig(), None, phase)
    assert held.detail.endswith(SELF_CLEARING)
    assert held.detail.count(SELF_CLEARING) == 1


@pytest.mark.parametrize("phase", ENDING)
def test_a_warning_does_not_say_it_on_a_session_that_is_ending(phase: Phase) -> None:
    """Its own cooldown, an ending under way: the speed follows nothing upwards from there."""
    held = _heart_rate_gone(Rig(), None, phase)
    assert SELF_CLEARING not in held.detail
    assert "NOT LATCHED" not in held.detail


def test_a_warning_does_not_say_it_once_the_arm_has_stopped_by_itself() -> None:
    """The tick the standstill is judged on: the phase is still HOLD, the session is not."""
    rig = Rig()
    held = _heart_rate_gone(rig, WARNING, Phase.HOLD)
    assert SELF_CLEARING not in held.detail
    assert rig.standing_action() is SafetyAction.RAMP_DOWN
