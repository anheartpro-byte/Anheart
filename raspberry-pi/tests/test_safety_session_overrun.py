"""``session_overrun`` judges a session in progress, and nothing else (ANH-181).

The rule ends a session that has outlived its programme by more than its
grace. The time it measures, the time since the start, goes on counting after
the session has ended, until the next start. So on ``develop`` the rule
latched over a machine at rest, its condition then stayed true for good, and
every acknowledgement was taken back on the next tick.

The runtime now states in its observation whether the session is over
(:attr:`~src.training.safety.SafetyObservation.session_over`), and the rule is
judged only while it is not. ``tests/test_runtime_session_overrun.py`` proves
WHEN the runtime says so, on the real tick. This file proves what the rule
does with the statement, on the supervisor rig of ``tests/test_safety.py``:

* said or not, nothing changes while a session is in progress: same
  threshold, same action, same latch (the tests of ``tests/test_safety.py``
  for this rule are untouched and still pass);
* a session that is over is never judged, whatever the time since its start;
* a verdict raised during the session is released when the session is over,
  stays on the latched floor until a named operator clears it, and then stays
  cleared;
* the statement silences this rule and no other, and it is the statement that
  does it, not the phase.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.motor.drive import DriveState
from src.result import Err, Ok
from src.training.safety import (
    RULE_DRIVE_FAULT,
    RULE_SESSION_OVERRUN,
    NothingLatched,
    SafetyObservation,
)
from src.training.types import Phase, SafetyAction, SafetyVerdict
from src.units import Seconds
from tests.test_safety import Rig

OPERATOR = "dr-mensah"
TOTAL = Seconds(100.0)
"""The programme's length in these tests; the rule's grace is the shipped 30 s."""

PAST_THE_DEADLINE = Seconds(131.0)


def _rig() -> Rig:
    rig = Rig()
    rig.total_duration = TOTAL
    return rig


def _tick(
    rig: Rig, *, over: bool, phase: Phase = Phase.HOLD, advance: Seconds | None = None
) -> SafetyVerdict | None:
    """One control period (or ``advance``) with the runtime's statement set to ``over``."""
    step = rig.limits.control_period if advance is None else advance
    rig.phase = phase
    rig.clock.advance(step)
    rig.elapsed = Seconds(rig.elapsed + step)
    rig.seq += 1
    observation: SafetyObservation = replace(rig.observation(), session_over=over)
    return rig.supervisor.evaluate(observation)


def _run(rig: Rig, seconds: float, *, over: bool, phase: Phase = Phase.HOLD) -> None:
    for _ in range(round(seconds / rig.limits.control_period)):
        _tick(rig, over=over, phase=phase)


def _fired(rig: Rig) -> SafetyVerdict | None:
    return rig.verdict_for(RULE_SESSION_OVERRUN)


def _floor_rule(rig: Rig) -> str | None:
    floor = rig.floor()
    return None if floor is None else floor.rule


# =========================================================================
# A session in progress: nothing changes
# =========================================================================


def test_an_observation_says_its_session_is_in_progress_unless_the_runtime_states_otherwise() -> (
    None
):
    """The default is ``False``: an observation built without the field is judged as before."""
    rig = _rig()
    assert rig.observation().session_over is False
    rig.tick(PAST_THE_DEADLINE)
    assert rig.action_for(RULE_SESSION_OVERRUN) is SafetyAction.RAMP_DOWN


def test_a_session_in_progress_is_judged_at_the_same_instant_as_before() -> None:
    """Strictly past the total plus the grace, on the first tick, latched, by this rule."""
    rig = _rig()
    _tick(rig, over=False, advance=Seconds(130.0))
    assert _fired(rig) is None, "the rule fired at the deadline itself, not past it"

    verdict = _tick(rig, over=False, advance=Seconds(0.5))
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_SESSION_OVERRUN,
        SafetyAction.RAMP_DOWN,
        True,
    )
    assert verdict.since == rig.clock.monotonic()
    assert "100 s plus 30 s of grace" in verdict.detail
    assert rig.floor() == verdict


@pytest.mark.parametrize("phase", list(Phase))
def test_the_phase_alone_never_silences_the_rule(phase: Phase) -> None:
    """Not even ``DONE``: it is the runtime's statement that is read, not the phase.

    A runtime that has gone silent reaches ``DONE`` with a setpoint it can no
    longer take back, and a verdict arriving at rest puts a finished session
    back in ``RECOVERY``. Neither is told apart by the phase, so the rule does
    not look at it.
    """
    rig = _rig()
    _run(rig, 131.0, over=False, phase=phase)
    assert rig.action_for(RULE_SESSION_OVERRUN) is SafetyAction.RAMP_DOWN


# =========================================================================
# A session that is over: never judged
# =========================================================================


@pytest.mark.parametrize("phase", list(Phase))
def test_a_session_that_is_over_is_not_judged_however_long_ago_it_started(phase: Phase) -> None:
    """The measured failure: 30 s after the end on ``develop``, and then for good."""
    rig = _rig()
    _run(rig, 90.0, over=False)
    _run(rig, 250.0, over=True, phase=phase)
    assert rig.elapsed > 3 * TOTAL
    assert _fired(rig) is None
    assert rig.standing() is None
    assert rig.floor() is None
    refused = rig.supervisor.acknowledge(OPERATOR)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, NothingLatched)


def test_a_new_session_is_judged_again_from_its_own_start() -> None:
    """Over, then a start: the count restarts and so does the rule."""
    rig = _rig()
    _run(rig, 200.0, over=True, phase=Phase.DONE)
    assert _fired(rig) is None

    rig.elapsed = Seconds(0.0)
    _tick(rig, over=False, advance=Seconds(130.0))
    assert _fired(rig) is None
    _tick(rig, over=False, advance=Seconds(0.5))
    assert rig.action_for(RULE_SESSION_OVERRUN) is SafetyAction.RAMP_DOWN


# =========================================================================
# A verdict raised during the session, once the session is over
# =========================================================================


def test_an_overrun_raised_during_a_session_is_released_when_the_session_is_over() -> None:
    """Released, not forgotten: it stays on the floor until somebody names themselves."""
    rig = _rig()
    _run(rig, 131.0, over=False)
    raised = _fired(rig)
    assert raised is not None

    _run(rig, 60.0, over=False, phase=Phase.RECOVERY)
    assert _fired(rig) is not None, "the rule let go while the session was still on its way out"

    _tick(rig, over=True, phase=Phase.DONE)
    assert _fired(rig) is None
    floor = rig.floor()
    assert floor is not None, "the latch went with the rule: nobody acknowledged it"
    assert (floor.rule, floor.latched, floor.since) == (RULE_SESSION_OVERRUN, True, raised.since)
    assert rig.standing() == floor


def test_acknowledging_an_overrun_holds_once_the_session_is_over() -> None:
    """On ``develop`` every acknowledgement was taken back on the next tick, for good."""
    rig = _rig()
    _run(rig, 131.0, over=False)
    _run(rig, 60.0, over=True, phase=Phase.DONE)
    assert _floor_rule(rig) == RULE_SESSION_OVERRUN

    acknowledged = rig.supervisor.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_OVERRUN,)
    _run(rig, 200.0, over=True, phase=Phase.DONE)
    assert rig.standing() is None, "the acknowledgement was taken back"
    assert rig.floor() is None


def test_acknowledging_an_overrun_before_the_session_is_over_is_taken_back() -> None:
    """Unchanged: while its condition is true a rule re-latches, like every other one."""
    rig = _rig()
    _run(rig, 131.0, over=False)
    assert isinstance(rig.supervisor.acknowledge(OPERATOR), Ok)
    assert rig.floor() is None

    _tick(rig, over=False, phase=Phase.RECOVERY)
    assert _floor_rule(rig) == RULE_SESSION_OVERRUN


# =========================================================================
# What the statement does not do
# =========================================================================


def test_the_statement_silences_this_rule_and_no_other() -> None:
    """Two supervisors, the same evidence, one told the session is over: one rule apart."""
    told, untold = _rig(), _rig()
    for rig, over in ((told, True), (untold, False)):
        rig.drive_state = DriveState.FAULT
        rig.heart_rate_present = False
        rig.attendant_present = False
        _run(rig, 200.0, over=over, phase=Phase.RECOVERY)

    fired_untold = {verdict.rule for verdict in untold.live()}
    fired_told = {verdict.rule for verdict in told.live()}
    assert RULE_SESSION_OVERRUN in fired_untold
    assert RULE_DRIVE_FAULT in fired_told
    assert fired_told == fired_untold - {RULE_SESSION_OVERRUN}
    assert len(fired_told) >= 3, "the evidence fired too little to tell the rules apart"
