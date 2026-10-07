"""An alert latched at the end of a session says something true (ANH-185).

After ANH-181 three measured sequences still left a latched alert to
acknowledge although nothing had gone wrong. Nothing moved wrongly in any of
them; the operator learnt to acknowledge without reading, which is how the
latches that matter get ignored.

* **An ending opened late.** A STOP, an emergency stop or a verdict in the last
  270 s of the shipped 1800 s programme opens a whole 300 s recovery, which
  ends after the programme's 1830 s: ``session_overrun`` latched during it.
* **A manual session reaching its 3600 s limit at speed.** The descent takes
  more than the 30 s of grace (104 s from 1344 motor rpm), and with a person
  on board a 60 s recovery follows any descent: ``session_overrun`` latched.
* **A verdict at rest after a programme that ended by itself.** An emergency
  stop or a drive fault opened an ending over a session that was over: ARRET,
  a whole recovery, and the heart-rate rules judging somebody who was no longer
  in a session, up to a latched ``hr_stale`` once the electrodes were off.

What changed. The runtime states the ending it has opened and the rule gives
it its own descent and recovery before judging (the first two); and no ending
is opened over a session that is over (the third).
``tests/test_safety_ending_overrun.py`` proves what the rule does with the
statement, instant by instant. This file proves what happens on the real tick:

* **the three sequences**: these fail on ``develop`` and pass here;
* **what the runtime states**, and that it cannot move once stated;
* **the rule stays armed**: a descent that does not finish is still brought to
  zero by it. The runtime follows a stop under a FREEZE (ANH-175, ANH-189), so
  nothing holds a descent any more; as in
  ``tests/test_runtime_session_overrun.py`` that guard is forced off to show
  that the rule behind it still works;
* **what stays latched**: an emergency stop and a drive fault at rest are
  still latched, shown, refused against and acknowledged by name.

The fake drive and the manual clock of ``tests/test_runtime.py``. The three
sequences run the shipped programme, the console's own limits and the
machine's motion limits; the others the rig's 130 s programme.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import pytest

from src.local_panel import RUNTIME_LIMITS
from src.motor.drive import DriveFault
from src.result import Err, Ok, is_ok
from src.training import runtime as runtime_module
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.plan import COMMISSIONED_DECEL_S, MIN_RECOVERY_S
from src.training.runtime import (
    BENCH_RECOVERY,
    MANUAL_SESSION_LIMIT,
    EndReason,
    ResetWhileCommanded,
    RuntimeState,
    SafetyStanding,
    TrainingRuntime,
)
from src.training.safety import (
    RULE_COMMS_LOST,
    RULE_DRIVE_FAULT,
    RULE_HR_STALE,
    RULE_OPERATOR_ESTOP,
    RULE_SESSION_OVERRUN,
    EndingInProgress,
    GoSilentIsTerminal,
)
from src.training.types import Occupancy, Phase, RunMode, SafetyAction
from src.units import MotorRpm, Seconds
from tests.test_runtime import (
    OPERATOR,
    REAL_PROFILE,
    TICK,
    Occupant,
    Rig,
    _rig,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
)
from tests.test_runtime_manual import (
    _manual as _manual_session,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_manual import (
    _rig as _manual_rig,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_manual import (
    _target,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_session_overrun import (
    DEADLINE,
    TOTAL,
    _live_rules,  # pyright: ignore[reportPrivateUsage]
    _mode,  # pyright: ignore[reportPrivateUsage]
    _nothing_asks_for_the_descent,  # pyright: ignore[reportPrivateUsage]
    _observations,  # pyright: ignore[reportPrivateUsage]
    _overrun,  # pyright: ignore[reportPrivateUsage]
    _programme,  # pyright: ignore[reportPrivateUsage]
    _run_to_its_end,  # pyright: ignore[reportPrivateUsage]
    _since_start,  # pyright: ignore[reportPrivateUsage]
    _standing_rule,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _occupied_manual,  # pyright: ignore[reportPrivateUsage]
)

PLANNED: Final[float] = float(REAL_PROFILE.total_duration_s)
"""The shipped programme: 1800 s. COOLDOWN from 1260 s, RECOVERY from 1500 s."""

OLD_DEADLINE: Final[float] = PLANNED + 30.0
"""Where ``develop`` latched ``session_overrun`` over any ending still in progress."""

COOLDOWN: Final[float] = 10.0
"""The rig programme's cooldown, in seconds: what one of its endings is given to reach zero."""

RECOVERY: Final[float] = 60.0
"""The rig programme's monitored recovery, in seconds."""


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _floor_rule(rig: Rig) -> str | None:
    floor = rig.runtime.supervisor.floor
    return None if floor is None else floor.rule


def _believe(runtime: TrainingRuntime, name: str, value: object) -> None:
    """Write one field of the runtime through a parameter: a state no path of it produces."""
    setattr(runtime, name, value)


async def _shipped() -> Rig:
    """The shipped programme on the console's own limits, a simulated person on board."""
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    started = await rig.start()
    assert is_ok(started), started
    return rig


async def _a_new_programme_starts(rig: Rig) -> None:
    started = await rig.start()
    assert is_ok(started), f"the start was refused once the session was over: {started}"
    assert rig.state() is RuntimeState.RUNNING


# =========================================================================
# 1. AN ENDING OPENED LATE: fails on develop (session_overrun at 1830.2 s)
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Late:
    """One ending opened in the last minutes of the shipped programme."""

    what: str
    at: float
    """Seconds after the start at which it happens."""

    stands: str | None
    """The rule left latched once the session is over: the one that ended it, or none."""

    over_by: float
    """The session is over, the console at REPOS, by this many seconds after the start."""


_LATE: Final[tuple[_Late, ...]] = (
    _Late("a STOP", 1531.0, None, 1832.0),
    _Late("a STOP", 1600.0, None, 1901.0),
    _Late("a STOP", 1799.0, None, 2100.0),
    _Late("an e-stop", 1600.0, RULE_OPERATOR_ESTOP, 1901.0),
    _Late("the electrodes taken off", 1600.0, RULE_HR_STALE, 1961.0),
    _Late("the electrodes taken off", 1700.0, RULE_HR_STALE, 2061.0),
)


@pytest.mark.parametrize("late", _LATE, ids=[f"{late.what} at {late.at:.0f} s" for late in _LATE])
async def test_an_ending_opened_late_in_the_shipped_programme_latches_no_overrun(
    late: _Late,
) -> None:
    """The first measured case, and the acceptance criterion. On ``develop``: 1830.2 s.

    The arm is at rest: the programme is in its own recovery. A STOP is typed,
    the emergency stop is pressed, or the electrodes come off and ``hr_stale``
    ends the session 60 s later. Each opens a whole 300 s recovery, which ends
    after 1830 s. On every tick until the console is back at REPOS the rule
    does not fire and is not on the latched floor; what stands at the end is
    only what ended the session; one named acknowledgement clears it, and the
    next start is taken.
    """
    rig = await _shipped()
    await rig.run(late.at)
    assert _applied(rig) == 0, "the programme is not in its own recovery: not this case"
    assert rig.state() is RuntimeState.RUNNING

    feed = True
    match late.what:
        case "a STOP":
            rig.runtime.request_stop("operator pressed STOP")
        case "an e-stop":
            rig.runtime.request_estop("console web: e-stop")
        case _:
            feed = False

    while rig.state() is not RuntimeState.FINISHED:
        await rig.step(feed=feed)
        at = _since_start(rig)
        assert _overrun(rig) is None, f"session_overrun fired over a normal ending, {at:.1f} s"
        assert _floor_rule(rig) != RULE_SESSION_OVERRUN
        assert at < late.over_by, "the ending did not finish: not this case"
    assert _since_start(rig) > OLD_DEADLINE, "over before the old deadline: not this case"
    assert _mode(rig) is RunMode.REPOS
    assert _standing_rule(rig) == late.stands

    if late.stands is not None:
        acknowledged = rig.runtime.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(acknowledged, Ok)
        assert acknowledged.value.cleared == (late.stands,)
    await rig.run(60.0, feed=feed)
    assert _standing_rule(rig) is None
    await _a_new_programme_starts(rig)


# =========================================================================
# 2. A MANUAL SESSION REACHING ITS LIMIT AT SPEED: fails on develop (3630.2 s)
# =========================================================================


@dataclass(frozen=True, slots=True)
class _AtTheLimit:
    """One manual session left running until it ends itself."""

    who: str
    occupancy: Occupancy
    speed: int
    """The ceiling and the target, motor rpm."""

    descent: tuple[float, float]
    """The walk to zero from that speed takes between these many seconds."""


_AT_THE_LIMIT: Final[tuple[_AtTheLimit, ...]] = (
    _AtTheLimit("the capsule empty, 27 output rpm", Occupancy.BENCH, 1344, (103.0, 105.0)),
    _AtTheLimit("the capsule empty, the nameplate speed", Occupancy.BENCH, 1380, (106.0, 108.0)),
    _AtTheLimit("a person on board", Occupancy.OCCUPIED, 200, (11.0, 13.0)),
)


@pytest.mark.parametrize("case", _AT_THE_LIMIT, ids=[case.who for case in _AT_THE_LIMIT])
async def test_a_manual_session_that_reaches_its_limit_at_speed_latches_no_overrun(
    case: _AtTheLimit,
) -> None:
    """The second measured case, and the acceptance criterion. On ``develop``: 3630.2 s.

    The real limit, an hour. The console brings the arm down at the motion
    limits: 104 s from 1344 motor rpm, 107 s from the 1380 rpm nameplate, the
    highest ceiling a console can be given. With a person on board a 60 s
    recovery follows, so that session latched on ``develop`` whatever its
    speed. Nothing fires and nothing stands, from the limit to REPOS; the
    session ends as a completed one, and a new manual start is taken.
    """
    limit = float(MANUAL_SESSION_LIMIT)
    if case.occupancy is Occupancy.OCCUPIED:
        rig = await _occupied_manual(case.speed)
    else:
        rig = await _manual_session(_manual_rig(), ceiling=MotorRpm(case.speed))
        _target(rig, case.speed)
        await rig.step()
    await rig.run(limit - _since_start(rig) - 1.0)
    assert _applied(rig) == case.speed, "the session is not at speed at its limit"
    assert rig.state() is RuntimeState.RUNNING
    assert _standing_rule(rig) is None

    at_zero: float | None = None
    while rig.state() is not RuntimeState.FINISHED:
        await rig.step()
        at = _since_start(rig)
        assert _overrun(rig) is None, f"session_overrun fired in the ending, {at:.1f} s"
        assert _standing_rule(rig) is None, f"{_standing_rule(rig)} stands, {at:.1f} s"
        if at_zero is None and _applied(rig) == 0:
            at_zero = at - limit
        assert at < limit + 300.0, "the ending did not finish: not this case"
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _mode(rig) is RunMode.REPOS
    assert _since_start(rig) > limit + 30.0, "over before the old deadline: not this case"
    assert at_zero is not None
    assert case.descent[0] < at_zero < case.descent[1], f"the walk to zero took {at_zero:.1f} s"

    await rig.run(60.0)
    assert _standing_rule(rig) is None
    again = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300))
    assert is_ok(again), f"the manual start was refused after the limit: {again}"


# =========================================================================
# 3. A VERDICT AT REST AFTER A PROGRAMME THAT ENDED BY ITSELF: fails on develop
# =========================================================================


@pytest.mark.parametrize("arrives", ["an e-stop", "a drive fault", "the link lost"])
async def test_a_verdict_at_rest_after_a_programme_ended_by_itself_reopens_no_ending(
    arrives: str,
) -> None:
    """The third measured case. On ``develop``: ARRET, 300 s of recovery, a latched ``hr_stale``.

    The shipped programme has run to its own end. A minute later the
    electrodes are off, the rider is getting out, and somebody presses the
    emergency stop; or the drive is switched off and reports a fault; or its
    cable is pulled. The session stays over on every tick for the 400 s that
    follow: REPOS, the phase ``DONE``, no ending recorded, its own end reason
    kept, nothing commanded and the output stage off. The heart-rate rules
    judge nobody: no ``hr_stale`` at any tick with no heart rate at all.

    And the verdict is everything it was: latched at once, the only thing
    standing, and every start refused in its name.
    """
    rig = await _shipped()
    await rig.run(PLANNED + 2.0)
    assert rig.state() is RuntimeState.FINISHED
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    await rig.run(58.0)
    await rig.run(5.0, feed=False)
    assert _standing_rule(rig) is None
    frames = len(rig.drive.writes)

    match arrives:
        case "an e-stop":
            rig.runtime.request_estop("console web: e-stop")
            expected, action = RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP
            assert _standing_rule(rig) == expected, "the e-stop waited for a tick to latch"
        case "a drive fault":
            rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
            expected, action = RULE_DRIVE_FAULT, SafetyAction.RAMP_DOWN
        case _:
            rig.drive.break_comms()
            expected, action = RULE_COMMS_LOST, SafetyAction.GO_SILENT

    for _ in range(round(400.0 / TICK)):
        snapshot = await rig.step(feed=False)
        at = f"{_since_start(rig):.1f} s after the start"
        assert snapshot.mode is RunMode.REPOS, f"the console left REPOS, {at}"
        assert snapshot.phase is Phase.DONE, f"a finished session went back to {snapshot.phase}"
        assert rig.state() is RuntimeState.FINISHED
        assert rig.runtime.ending is None, f"an ending was opened over a finished session, {at}"
        assert _live_rules(rig) <= {expected}, f"{_live_rules(rig)} judged a finished session"
        assert snapshot.setpoint.motor_rpm == 0
    standing = rig.runtime.standing
    assert standing is not None
    assert (standing.rule, standing.action, standing.latched) == (expected, action, True)
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert not rig.drive.is_enabled()

    refused = await rig.start()
    assert isinstance(refused, Err)
    if arrives == "the link lost":
        assert rig.runtime.silent
        terminal = rig.runtime.acknowledge(OPERATOR)
        assert isinstance(terminal, Err)
        assert isinstance(terminal.error, GoSilentIsTerminal)
        return
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == expected

    if arrives == "a drive fault":
        reset = await rig.runtime.fault_reset()
        assert is_ok(reset), reset
        await rig.run(2.0, feed=False)
    acknowledged = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (expected,)
    await rig.run(30.0, feed=False)
    assert _standing_rule(rig) is None, "the acknowledgement was taken back"
    await _a_new_programme_starts(rig)


async def test_a_drive_fault_at_rest_after_a_programme_can_be_reset_at_once() -> None:
    """What the operator gained: no 300 s to wait before the reset is accepted.

    A fault reset is taken only at rest, the phase ``DONE``. On ``develop`` the
    fault opened an ending, the phase was ``recovery`` for the 60 s of this
    programme (300 s on the shipped one), and the reset was refused for all of
    it with "the machine is still commanded".
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await rig.run(1.0)
    assert _standing_rule(rig) == RULE_DRIVE_FAULT
    assert rig.phase() is Phase.DONE

    reset = await rig.runtime.fault_reset()
    assert not (isinstance(reset, Err) and isinstance(reset.error, ResetWhileCommanded))
    assert is_ok(reset), reset


async def test_a_stop_that_finds_a_speed_commanded_after_the_end_still_opens_an_ending() -> None:
    """The fail-safe half of "over": DONE alone opens no door.

    No path of the runtime leaves a setpoint in force once a programme has
    completed, so this state is written by hand. With any speed commanded the
    session is not over, whatever its phase machine has said: the emergency
    stop opens a real ending, as it always did, the reference is zeroed, and
    the console shows ARRET until the machine is down.
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    _believe(rig.runtime, "_applied_rpm", MotorRpm(120))

    rig.runtime.request_estop("console web: e-stop")
    ending = rig.runtime.ending
    assert ending is not None, "a commanded speed was left with no ending to bring it down"
    assert ending.reason is EndReason.EMERGENCY_STOP
    assert rig.runtime.end_reason is EndReason.EMERGENCY_STOP
    assert _applied(rig) == 0
    assert _mode(rig) is RunMode.ARRET


async def test_a_session_ended_by_a_stop_still_opens_nothing_for_a_verdict_at_rest() -> None:
    """Unchanged, and now the one behaviour: what a STOP-ended session always did.

    The e-stop at rest finds that session's ending recorded and adds nothing
    to it. REPOS throughout, the reason of the end unchanged.
    """
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.request_stop("operator pressed STOP")
    await rig.run(COOLDOWN + RECOVERY + 5.0)
    assert rig.state() is RuntimeState.FINISHED
    recorded = rig.runtime.ending

    rig.runtime.request_estop("console web: e-stop")
    for _ in range(round(90.0 / TICK)):
        snapshot = await rig.step(feed=False)
        assert snapshot.mode is RunMode.REPOS
        assert snapshot.phase is Phase.DONE
        assert _live_rules(rig) == set()
    assert rig.runtime.ending == recorded
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
    assert _standing_rule(rig) == RULE_OPERATOR_ESTOP


# =========================================================================
# WHAT THE RUNTIME STATES
# =========================================================================


async def test_the_runtime_states_its_ending_from_the_tick_it_opens_until_the_next_start() -> None:
    """What reaches the rule, asserted on the recorded observations.

    None while the session runs. From the tick the STOP is honoured: when the
    ending opened, counted from the start, and the programme's own cooldown
    and recovery. The same three numbers on every tick from there, through the
    descent, the recovery and the rest that follows, whatever arrives
    meanwhile (an emergency stop here): an ending cannot move its own
    deadline. None again from the first tick of the next session.
    """
    rig = await _programme()
    with _observations() as seen:
        await rig.run(40.0)
        assert all(o.ending is None for o in seen), "an ending was stated before any opened"

        rig.runtime.request_stop("operator pressed STOP")
        await rig.step()
        opened = _since_start(rig)
        stated = seen[-1].ending
        assert stated == EndingInProgress(
            opened=Seconds(opened), descent=Seconds(COOLDOWN), recovery=Seconds(RECOVERY)
        )

        await rig.run(5.0)
        rig.runtime.request_estop("console web: e-stop")
        before = len(seen)
        await rig.run(200.0)
        assert rig.state() is RuntimeState.FINISHED
        assert {o.ending for o in seen[before:]} == {stated}, "the ending moved once stated"

        acknowledged = rig.runtime.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(acknowledged, Ok)
        again = len(seen)
        await _a_new_programme_starts(rig)
        await rig.run(5.0)
        assert len(seen) > again
        assert all(o.ending is None for o in seen[again:]), "the new session began as ending"


@pytest.mark.parametrize(
    ("occupancy", "recovery"),
    [(Occupancy.BENCH, BENCH_RECOVERY), (Occupancy.OCCUPIED, MIN_RECOVERY_S)],
)
async def test_a_manual_ending_is_stated_with_the_descent_from_its_ceiling(
    occupancy: Occupancy, recovery: Seconds
) -> None:
    """The budget a manual ending is given: the walk from the ceiling, plus the drive's ramp.

    From a ceiling of 300 motor rpm the motion-limited walk to zero takes
    20 s, and the drive's commissioned ramp 4 s. No recovery with the capsule
    empty, the shortest a profile may declare with a person on board.
    """
    if occupancy is Occupancy.OCCUPIED:
        rig = await _occupied_manual(200)
    else:
        rig = await _manual_session()
        _target(rig, 200)
        await rig.run(30.0)
    with _observations() as seen:
        rig.runtime.request_stop("operator pressed STOP")
        await rig.step()
    stated = seen[-1].ending
    assert stated is not None
    assert stated.opened == Seconds(_since_start(rig))
    assert float(stated.descent) == pytest.approx(20.0 + float(COMMISSIONED_DECEL_S))
    assert stated.recovery == recovery


async def test_an_emergency_stop_at_an_idle_console_is_counted_from_zero() -> None:
    """No session at all: the ending is stated from zero, like the time since no start.

    Nothing has been started, so nothing is measured: the rule cannot fire,
    with the statement as without it.
    """
    rig = _rig()
    with _observations() as seen:
        rig.runtime.request_estop("console web: e-stop")
        await rig.run(60.0)
    assert {o.ending for o in seen} == {
        EndingInProgress(opened=Seconds(0.0), descent=COMMISSIONED_DECEL_S, recovery=BENCH_RECOVERY)
    }
    assert {float(o.elapsed) for o in seen} == {0.0}
    assert _standing_rule(rig) == RULE_OPERATOR_ESTOP
    assert _floor_rule(rig) is None


# =========================================================================
# THE RULE STAYS ARMED: a descent that does not finish is brought to zero
# =========================================================================


async def test_a_descent_blocked_by_a_latched_freeze_is_still_stopped_by_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EX-1, in a programme: an ending whose descent does not happen is ended by the rule.

    The guard that follows a stop under a FREEZE is forced off, so a FREEZE
    holds the setpoint whatever was asked, as it did before ANH-175. A FREEZE
    latched at 40 s holds the arm at speed through the programme's cooldown
    and recovery. STOP is typed at 125 s, five seconds before the end: an
    ending opens, in time, and its descent is blocked too.

    That ending had its 10 s of cooldown to bring the setpoint to zero: due at
    135 s, judged past 165 s. Not at 160 s, the programme's own deadline,
    which the ending has moved back by those five seconds; and not at 225 s
    either: none of its 60 s of recovery is lent to an arm that is still
    turning. Past 165 s the rule fires, RAMP_DOWN and latched; RAMP_DOWN
    outranks the FREEZE, the setpoint comes down to zero and the shaft
    follows.
    """
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(TOTAL - 5.0 - 40.0)
    held = _applied(rig)
    assert held > 0, "with the guard off the FREEZE did not hold the arm"
    rig.runtime.request_stop("operator pressed STOP")
    await rig.step()
    opened = _since_start(rig)
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP

    due = opened + COOLDOWN + 30.0
    assert due > DEADLINE + 1.0, "the ending's own deadline is not the later one: not this case"
    while _since_start(rig) < due - 1.0:
        await rig.step()
        assert _overrun(rig) is None, f"fired early, {_since_start(rig):.1f} s in"
        assert _applied(rig) == held, "the blocked descent moved: not this case"
    assert _since_start(rig) > DEADLINE, "quiet only because the old deadline had not come"

    await rig.run(2.0)
    verdict = _overrun(rig)
    assert verdict is not None, f"nothing ended a blocked descent {_since_start(rig):.1f} s in"
    assert _since_start(rig) < opened + COOLDOWN + RECOVERY, "the recovery was lent to the descent"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert "with 10 s to bring the setpoint back to zero plus 30 s of grace" in verdict.detail
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN

    await rig.run(10.0)
    assert _applied(rig) == 0, "RAMP_DOWN did not bring the blocked descent down"
    assert abs(rig.drive.shaft_rpm) < 1.0


async def test_a_manual_descent_blocked_at_the_limit_is_still_stopped_by_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EX-1, at the manual limit: the same barrier behind the ending the limit opens.

    A short limit, the guard forced off, a FREEZE latched while the arm turns
    at 200 motor rpm. At the limit the session ends itself and its descent is
    blocked. The ending is given the walk from the ceiling plus the drive's
    ramp, 24 s, and the grace: the rule fires 54 s after the limit and the
    setpoint comes down at the motion limits.
    """
    limit = Seconds(100.0)
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", limit)
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _manual_session()
    _target(rig, 200)
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(float(limit) - 40.0 + 1.0)
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _applied(rig) == 200, "with the guard off the FREEZE did not hold the arm"

    budget = 20.0 + float(COMMISSIONED_DECEL_S) + 30.0
    await rig.run(budget - 2.0)
    assert _overrun(rig) is None, f"fired early, {_since_start(rig):.1f} s in"
    assert _applied(rig) == 200
    await rig.run(2.0)
    verdict = _overrun(rig)
    assert verdict is not None, "nothing ended a manual descent blocked at the limit"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)

    await rig.run(30.0)
    assert _applied(rig) == 0, "RAMP_DOWN did not bring the blocked descent down"
    assert abs(rig.drive.shaft_rpm) < 1.0


async def test_an_overrun_that_opens_its_own_ending_keeps_firing_until_the_session_is_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged: the ending a real overrun opens is given nothing.

    A FREEZE holds the arm past the end of the programme (the guard forced
    off) and no ending has been opened: the rule fires at 160.2 s, as before,
    and its RAMP_DOWN opens the ending. That ending is stated to the rule
    like any other, and it comes too late to excuse anything: the rule goes
    on firing on every tick, an acknowledgement is taken back on the next
    one, until the session is over.
    """
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(DEADLINE - 40.0 - 1.0)
    assert _overrun(rig) is None
    await rig.run(2.0)
    assert _overrun(rig) is not None
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT

    with _observations() as seen:
        while True:
            acknowledged = rig.runtime.acknowledge(OPERATOR)
            assert isinstance(acknowledged, Ok)
            await rig.step()
            if rig.phase() is Phase.DONE:
                break
            assert _overrun(rig) is not None, f"quiet {_since_start(rig):.1f} s in, before the end"
            assert _standing_rule(rig) == RULE_SESSION_OVERRUN
    assert len(seen) > round(RECOVERY / TICK), "the session was over at once: not this case"
    assert all(o.ending is not None for o in seen), "no ending was stated: not this case"
    assert _applied(rig) == 0
    assert _overrun(rig) is None
    assert _floor_rule(rig) is None, "the last acknowledgement did not hold once over"


# =========================================================================
# WITH NO ENDING OPENED: the rule is what it was
# =========================================================================


async def test_a_programme_run_to_its_end_states_no_ending_and_is_never_judged() -> None:
    """EX-2 on the tick: a session that opens no ending gives the rule nothing new to read.

    The shipped programme, nobody touching anything. No ending is stated on
    any tick, from the start to an hour of rest, so the rule reads exactly
    what it read before this change, and it never fires.
    """
    rig = await _shipped()
    with _observations() as seen:
        await rig.run(PLANNED + 2.0)
        assert rig.state() is RuntimeState.FINISHED
        await rig.run(600.0)
    assert all(o.ending is None for o in seen)
    assert rig.runtime.ending is None
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _standing_rule(rig) is None
