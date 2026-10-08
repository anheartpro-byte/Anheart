"""``session_overrun`` and an ending in progress: judged against the ending (ANH-185).

After ANH-181 the rule still latched over three endings that were going
exactly as they should, and the operator acknowledged a sentence that said
nothing true. Two of the three are this rule's: an ending opened in the last
270 s of the shipped 1800 s programme (its 300 s recovery ends after 1830 s),
and a manual session reaching its 3600 s limit at speed (its descent takes
more than the 30 s of grace).

The runtime now states the ending it has opened
(:attr:`~src.training.safety.SafetyObservation.ending`), and the rule gives it
the descent expected from the speed it opened at, and its recovery, before
judging.
``tests/test_runtime_ending_alerts.py`` proves WHEN the runtime says so and
what follows on the real tick. This file proves what the rule does with the
statement, on the supervisor rig of ``tests/test_safety.py``, with the shipped
numbers:

* no ending stated: threshold, action, latch and sentence are what they were;
* an ending never brings the deadline forward, so no session is judged
  earlier than before and nothing new can latch;
* an ending opened late is left alone for as long as it goes as it should,
  and no longer;
* the rule stays armed: an arm still turning is judged on the descent
  expected from the speed the ending found, and on nothing longer; a session
  parked in its recovery on descent plus recovery; and a setpoint that leaves
  zero again is judged at once;
* an ending opened once the session had already overrun excuses nothing: the
  rule goes on firing through the ending its own verdict opens;
* the statement delays this rule and no other, and cannot silence it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.motor.drive import DriveState
from src.result import Ok
from src.training.safety import (
    RULE_DRIVE_FAULT,
    RULE_SESSION_OVERRUN,
    EndingInProgress,
    SafetyObservation,
)
from src.training.types import Phase, SafetyAction, SafetyVerdict
from src.units import MotorRpm, Seconds
from tests.test_safety import Rig

PROGRAMME: Final[Seconds] = Seconds(1800.0)
"""The shipped programme: COOLDOWN from 1260 s, RECOVERY from 1500 s."""

AT_REST: Final[Seconds] = Seconds(4.0)
"""The descent expected of an ending opened at rest: the drive's own ramp, nothing else."""

FROM_HOLD: Final[Seconds] = Seconds(29.7)
"""The descent expected from 193 motor rpm in the shipped programme, as the runtime states it."""

RECOVERY: Final[Seconds] = Seconds(300.0)
"""The shipped profile's monitored recovery."""

GRACE: Final[float] = 30.0
"""The rule's grace, unchanged."""

MANUAL_LIMIT: Final[Seconds] = Seconds(3600.0)
"""What a manual session is measured against, and where it ends itself."""

MANUAL_DESCENT: Final[Seconds] = Seconds(107.8)
"""The descent expected of a manual session from 1344 motor rpm: 103.8 s of
motion-limited walk plus the drive's 4 s ramp."""

MANUAL_DESCENT_FROM_300: Final[Seconds] = Seconds(24.0)
"""The same from 300 motor rpm, whatever the session's ceiling: 20 s of walk, 4 s of ramp."""

SPEED: Final[MotorRpm] = MotorRpm(193)
"""A setpoint that is not zero: the shipped programme's HOLD, a person on board."""


def _ending(
    opened: float, *, descent: Seconds = AT_REST, recovery: Seconds = RECOVERY
) -> EndingInProgress:
    """An ending of the shipped programme, opened at rest unless ``descent`` says otherwise."""
    return EndingInProgress(opened=Seconds(opened), descent=descent, recovery=recovery)


def _rig(total: Seconds = PROGRAMME) -> Rig:
    rig = Rig()
    rig.total_duration = total
    rig.phase = Phase.RECOVERY
    return rig


def _judge(
    rig: Rig,
    at: float,
    *,
    ending: EndingInProgress | None,
    commanded: MotorRpm = MotorRpm(0),
    over: bool = False,
) -> SafetyVerdict | None:
    """One evaluation ``at`` seconds after the start, with the runtime's statements set.

    Returns this rule's verdict alone. The rig jumps there in one step, so the
    measured speed follows the setpoint: no other rule has anything to say
    about a setpoint that has always been where it is.
    """
    rig.clock.advance(Seconds(at - rig.elapsed))
    rig.elapsed = Seconds(at)
    rig.seq += 1
    rig.commanded = commanded
    rig.measured = commanded
    observation: SafetyObservation = replace(rig.observation(), ending=ending, session_over=over)
    rig.supervisor.evaluate(observation)
    return rig.verdict_for(RULE_SESSION_OVERRUN)


# =========================================================================
# EX-2. No ending stated: the rule is what it was
# =========================================================================


def test_an_observation_states_no_ending_unless_the_runtime_does() -> None:
    """The default is ``None``: an observation built without the field is judged as before."""
    rig = _rig()
    assert rig.observation().ending is None
    rig.tick(Seconds(float(PROGRAMME) + GRACE + 1.0))
    assert rig.action_for(RULE_SESSION_OVERRUN) is SafetyAction.RAMP_DOWN


@pytest.mark.parametrize("commanded", [MotorRpm(0), SPEED])
def test_with_no_ending_the_threshold_the_action_the_latch_and_the_sentence_are_unchanged(
    commanded: MotorRpm,
) -> None:
    """Strictly past the total plus the grace, RAMP_DOWN, latched, in the same words."""
    rig = _rig()
    at_the_deadline = _judge(rig, float(PROGRAMME) + GRACE, ending=None, commanded=commanded)
    assert at_the_deadline is None, "the rule fired at the deadline itself, not past it"

    verdict = _judge(rig, float(PROGRAMME) + GRACE + 0.2, ending=None, commanded=commanded)
    assert verdict is not None
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert verdict.since == rig.clock.monotonic()
    assert verdict.detail == (
        "the session has run 1830 s against a programme of 1800 s plus 30 s of grace: "
        "the phase machine has lost track"
    )
    assert rig.floor() == verdict


# =========================================================================
# An ending never brings the deadline forward
# =========================================================================


@pytest.mark.parametrize("commanded", [MotorRpm(0), SPEED])
@pytest.mark.parametrize("opened", [0.0, 420.0, 1259.8, 1470.0])
def test_an_ending_opened_early_is_judged_exactly_as_a_session_with_none(
    opened: float, commanded: MotorRpm
) -> None:
    """Its own due time is before the programme's: the programme's stands, word for word.

    A STOP at 420 s whose descent never finished was caught at 1830.2 s, and
    it still is: not at 480 s, which is where the ending's own descent ends.
    Every verdict this rule gave before is given at the same instant.
    """
    told, untold = _rig(), _rig()
    ending = _ending(opened, descent=FROM_HOLD)
    for at in (opened + float(FROM_HOLD) + GRACE + 1.0, float(PROGRAMME) + GRACE):
        assert _judge(told, at, ending=ending, commanded=commanded) is None, f"fired early, {at} s"
        assert _judge(untold, at, ending=None, commanded=commanded) is None

    past = float(PROGRAMME) + GRACE + 0.2
    with_ending = _judge(told, past, ending=ending, commanded=commanded)
    without = _judge(untold, past, ending=None, commanded=commanded)
    assert with_ending is not None
    assert with_ending == without


# =========================================================================
# EX-1. An ending opened late: left alone while it goes as it should
# =========================================================================


@pytest.mark.parametrize("opened", [1531.0, 1600.0, 1760.0, 1799.8])
def test_an_ending_opened_late_is_not_judged_through_its_recovery(opened: float) -> None:
    """The first measured case, at the rule. On ``develop``: latched at 1830.2 s.

    STOP at 1531 s or at 1600 s, an e-stop at 1600 s, ``hr_stale`` ending the
    session at 1760 s: the arm is at rest, a whole 300 s recovery begins, and
    it is over 300 s later. Nothing fires at 1830.2 s, nor at any instant up
    to the end of that recovery.
    """
    rig = _rig()
    ending = _ending(opened)
    due = opened + float(AT_REST) + float(RECOVERY)
    for at in (float(PROGRAMME) + GRACE + 0.2, opened + float(RECOVERY) + 0.4, due + GRACE - 0.2):
        assert _judge(rig, at, ending=ending) is None, f"judged an ending going as it should, {at}"
    assert rig.floor() is None


def test_a_recovery_that_never_ends_is_judged_when_its_ending_is_overdue() -> None:
    """Armed: the ending's own time, plus the grace, and not one tick more.

    An ending opened at 1600 s with the setpoint at zero has the drive's 4 s
    and 300 s of recovery: due at 1904 s. A phase machine parked in that
    recovery is judged on the first tick past 1934 s, RAMP_DOWN and latched,
    in a sentence that names the ending.
    """
    rig = _rig()
    ending = _ending(1600.0)
    assert _judge(rig, 1934.0, ending=ending) is None

    verdict = _judge(rig, 1934.2, ending=ending)
    assert verdict is not None, "an ending that never finishes is no longer judged"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert verdict.detail == (
        "the session has run 1934 s against an ending opened at 1600 s with 304 s to finish "
        "its descent and its monitored recovery plus 30 s of grace: the phase machine has "
        "lost track"
    )
    assert rig.floor() == verdict


def test_a_descent_that_does_not_finish_is_judged_on_the_expected_descent_alone() -> None:
    """Armed against a turning arm: its own descent, and not the recovery.

    STOP 0.8 s before the end of the programme, the setpoint still at 193
    motor rpm, which a descent brings back to zero in 29.7 s at the latest:
    due at 1828.9 s, judged on the first tick past 1858.9 s. Not at 2158.9 s:
    the 300 s of recovery belong to an arm at rest. And not at the 2069.4 s
    of a budget taken from the profile's 240 s cooldown, whatever the speed.
    """
    rig = _rig()
    ending = _ending(1799.2, descent=FROM_HOLD)
    assert _judge(rig, 1858.8, ending=ending, commanded=SPEED) is None

    verdict = _judge(rig, 1859.0, ending=ending, commanded=SPEED)
    assert verdict is not None, "a descent that never finishes is no longer judged"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert verdict.detail == (
        "the session has run 1859 s against an ending opened at 1799 s with 30 s to bring "
        "the setpoint back to zero plus 30 s of grace: the phase machine has lost track"
    )


@pytest.mark.parametrize("opened", [1260.0, 1400.0, 1600.0, 1770.0])
def test_a_turning_arm_is_judged_at_the_unchanged_instant_unless_its_ending_opened_too_late(
    opened: float,
) -> None:
    """Up to 30 s before the end, the expected descent ends before the programme: 1830.2 s.

    An ending opened on an arm still turning at 193 motor rpm is judged when
    a session with no ending is, unless it opened in the last 29.7 s. Pressing
    STOP on an arm that is not coming down does not buy it minutes.
    """
    rig = _rig()
    ending = _ending(opened, descent=FROM_HOLD)
    assert _judge(rig, float(PROGRAMME) + GRACE, ending=ending, commanded=SPEED) is None
    late = _judge(rig, float(PROGRAMME) + GRACE + 0.2, ending=ending, commanded=SPEED)
    assert late is not None
    assert "against a programme of 1800 s" in late.detail


def test_a_setpoint_that_leaves_zero_inside_an_ending_is_judged_at_once() -> None:
    """The setpoint decides which time applies, and it is read on every tick.

    At 1900 s an ending opened at 1600 s is in its recovery, at rest, and not
    judged. The same instant with a setpoint that is not zero has nothing but
    the descent it was given at its opening, long spent: the rule fires on
    that tick.
    """
    at_rest, turning = _rig(), _rig()
    ending = _ending(1600.0)
    assert _judge(at_rest, 1900.0, ending=ending) is None
    assert _judge(turning, 1899.8, ending=ending) is None

    verdict = _judge(turning, 1900.0, ending=ending, commanded=MotorRpm(55))
    assert verdict is not None
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)


# =========================================================================
# An ending opened once the session had overrun excuses nothing
# =========================================================================


@pytest.mark.parametrize("commanded", [MotorRpm(0), SPEED])
def test_the_ending_the_rule_itself_opens_does_not_quieten_it(commanded: MotorRpm) -> None:
    """Unchanged: once raised, the rule fires until the session is over.

    A session runs past 1830 s with no ending opened: the rule fires, and its
    RAMP_DOWN opens an ending on that very tick. That ending was opened by a
    session that had already overrun, so it is given nothing: the rule is
    still firing on the next tick and a minute later, in the same words, and
    an acknowledgement given before the session is over is taken back.
    """
    rig = _rig()
    raised = _judge(rig, 1830.2, ending=None, commanded=commanded)
    assert raised is not None

    its_own = _ending(1830.2)
    for at in (1830.4, 1890.0):
        still = _judge(rig, at, ending=its_own, commanded=commanded)
        assert still is not None, f"the rule's own ending quietened it, {at} s"
        assert still.since == raised.since
        assert "against a programme of 1800 s" in still.detail

    acknowledged = rig.supervisor.acknowledge("dr-mensah")
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_OVERRUN,)
    assert rig.floor() is None
    _judge(rig, 1890.2, ending=its_own, commanded=commanded)
    floor = rig.floor()
    assert floor is not None, "an acknowledgement held before the session was over"
    assert floor.rule == RULE_SESSION_OVERRUN


def test_an_ending_opened_on_the_last_instant_before_the_overrun_is_still_in_time() -> None:
    """The boundary: at the programme's duration plus the grace, exactly, the ending counts.

    That is the last instant at which the rule has not fired. A manual
    session's own limit opens its ending at the duration itself, or one tick
    after it, which is well inside.
    """
    rig = _rig()
    assert _judge(rig, 1830.2, ending=_ending(1830.0)) is None
    assert _judge(rig, 2164.0, ending=_ending(1830.0)) is None
    assert _judge(rig, 2164.2, ending=_ending(1830.0)) is not None


# =========================================================================
# The manual session that reaches its limit at speed
# =========================================================================


@pytest.mark.parametrize("recovery", [Seconds(0.0), Seconds(60.0)])
def test_a_manual_limit_reached_at_speed_is_given_its_descent(recovery: Seconds) -> None:
    """The second measured case, at the rule. On ``develop``: latched at 3630.2 s.

    The limit opens the ending at 3600 s, which is also the duration the
    session is measured against. From 1344 motor rpm the walk to zero takes
    104 s: nothing fires during it, and nothing during the recovery that
    follows when a person is on board.
    """
    rig = _rig(MANUAL_LIMIT)
    ending = _ending(3600.0, descent=MANUAL_DESCENT, recovery=recovery)
    for at in (3630.2, 3700.0, 3737.0):
        speed = MotorRpm(700)
        assert _judge(rig, at, ending=ending, commanded=speed) is None, f"fired in the descent {at}"
    assert _judge(rig, 3737.0 + float(recovery), ending=ending) is None


def test_a_manual_descent_that_does_not_finish_is_judged_past_its_expected_descent() -> None:
    """Armed: 3600 s, plus the 107.8 s the descent from 1344 motor rpm takes, plus the grace."""
    rig = _rig(MANUAL_LIMIT)
    ending = _ending(3600.0, descent=MANUAL_DESCENT, recovery=Seconds(60.0))
    assert _judge(rig, 3737.0, ending=ending, commanded=MotorRpm(1344)) is None

    verdict = _judge(rig, 3738.0, ending=ending, commanded=MotorRpm(1344))
    assert verdict is not None
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert "an ending opened at 3600 s with 108 s to bring the setpoint back to zero" in (
        verdict.detail
    )


def test_a_manual_descent_from_below_the_ceiling_is_judged_on_the_speed_it_started_at() -> None:
    """Armed: the limit reached at 300 motor rpm is given the 24 s that speed needs.

    Under a ceiling of 1380 motor rpm the walk from the ceiling takes 110.8 s,
    and a budget taken from it kept the rule away from an arm held at 300 for
    111 s. From 300 the walk takes 20 s: the rule fires 54 s after the limit.
    """
    rig = _rig(MANUAL_LIMIT)
    ending = _ending(3600.2, descent=MANUAL_DESCENT_FROM_300, recovery=Seconds(0.0))
    assert _judge(rig, 3654.0, ending=ending, commanded=MotorRpm(300)) is None

    verdict = _judge(rig, 3654.4, ending=ending, commanded=MotorRpm(300))
    assert verdict is not None, "a manual descent held below the ceiling is no longer judged"
    assert "an ending opened at 3600 s with 24 s to bring the setpoint back to zero" in (
        verdict.detail
    )


# =========================================================================
# What the statement does not do
# =========================================================================


def test_a_session_that_is_over_is_not_judged_whatever_its_ending_says() -> None:
    """Unchanged (ANH-181): once the session is over there is nothing left to outlive."""
    rig = _rig()
    assert _judge(rig, 5000.0, ending=_ending(1600.0), over=True) is None
    assert rig.floor() is None


@pytest.mark.parametrize(
    "nonsense",
    [
        _ending(math.nan),
        _ending(1600.0, descent=Seconds(math.nan)),
        _ending(1600.0, recovery=Seconds(math.nan)),
        _ending(math.inf),
        _ending(1600.0, descent=Seconds(math.inf)),
        _ending(1600.0, recovery=Seconds(math.inf)),
        _ending(-math.inf),
        _ending(1600.0, descent=Seconds(-1.0e9)),
    ],
)
def test_a_statement_that_is_not_a_finite_later_time_leaves_the_programme_deadline(
    nonsense: EndingInProgress,
) -> None:
    """The statement can delay the rule by a finite time, and by nothing else.

    Not a number, infinite, or absurdly early: the session is judged at the
    programme's deadline, as if no ending had been stated. An ending cannot
    silence this rule.
    """
    rig = _rig()
    assert _judge(rig, float(PROGRAMME) + GRACE, ending=nonsense) is None
    verdict = _judge(rig, float(PROGRAMME) + GRACE + 0.2, ending=nonsense)
    assert verdict is not None, "a statement that is not a time silenced the rule"
    assert "against a programme of 1800 s" in verdict.detail


def test_the_statement_delays_this_rule_and_no_other() -> None:
    """Two supervisors, the same evidence, one told of an ending: one rule apart."""
    told, untold = _rig(), _rig()
    for rig, ending in ((told, _ending(1700.0)), (untold, None)):
        rig.drive_state = DriveState.FAULT
        rig.heart_rate_present = False
        rig.attendant_present = False
        _judge(rig, 1700.0, ending=ending)
        _judge(rig, 1900.0, ending=ending)

    fired_untold = {verdict.rule for verdict in untold.live()}
    fired_told = {verdict.rule for verdict in told.live()}
    assert RULE_SESSION_OVERRUN in fired_untold
    assert RULE_DRIVE_FAULT in fired_told
    assert fired_told == fired_untold - {RULE_SESSION_OVERRUN}
    assert len(fired_told) >= 3, "the evidence fired too little to tell the rules apart"


_SECONDS: Final = st.floats(min_value=0.0, max_value=5000.0, allow_nan=False)


@dataclass(frozen=True, slots=True)
class _Instant:
    """One arbitrary programme, one arbitrary ending, and an instant to judge it at."""

    planned: float
    opened: float
    descent: float
    recovery: float
    elapsed: float
    turning: bool


_INSTANTS: Final = st.builds(
    _Instant,
    planned=_SECONDS,
    opened=_SECONDS,
    descent=_SECONDS,
    recovery=_SECONDS,
    elapsed=st.floats(min_value=0.0, max_value=20000.0, allow_nan=False),
    turning=st.booleans(),
)


@given(case=_INSTANTS)
def test_for_any_ending_the_rule_fires_past_the_later_of_the_two_deadlines_and_only_then(
    case: _Instant,
) -> None:
    """The requirement as a property, for any programme, any ending and any instant.

    The rule fires if and only if the session has run past the later of the
    programme's duration and the ending's own due time, plus the grace; and
    past the programme's alone when the ending was opened after the overrun.
    Two consequences are asserted with it: it never fires where a session
    with no ending would not (nothing new latches), and it always fires in
    the end (an ending cannot silence it).
    """
    rig = _rig(Seconds(case.planned))
    ending = _ending(case.opened, descent=Seconds(case.descent), recovery=Seconds(case.recovery))
    budget = case.descent if case.turning else case.descent + case.recovery
    in_time = case.opened <= case.planned + GRACE
    due = max(case.planned, case.opened + budget) if in_time else case.planned
    deadline = due + GRACE
    commanded = SPEED if case.turning else MotorRpm(0)

    fired = _judge(rig, case.elapsed, ending=ending, commanded=commanded) is not None
    assert fired == (case.elapsed > deadline)
    if fired:
        assert case.elapsed > case.planned + GRACE, "fired where no ending would not have"

    later = _rig(Seconds(case.planned))
    in_the_end = _judge(later, deadline + 1.0, ending=ending, commanded=commanded)
    assert in_the_end is not None, "an ending silenced the rule"
