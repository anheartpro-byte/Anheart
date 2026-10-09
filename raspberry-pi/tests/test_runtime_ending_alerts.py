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
it the descent expected from the speed it opened at, and its recovery, before
judging (the first two); and no ending is opened over a session that is over
(the third).
``tests/test_safety_ending_overrun.py`` proves what the rule does with the
statement, instant by instant. This file proves what happens on the real tick:

* **the three sequences**: these fail on ``develop`` and pass here;
* **what the runtime states**, and that it cannot move once stated;
* **the rule stays armed**: a descent that does not finish is still brought to
  zero by it, and an ending buys an arm that is not coming down the time of
  its own descent and no more. The runtime follows a stop under a FREEZE
  (ANH-175, ANH-189), so nothing holds a descent any more; as in
  ``tests/test_runtime_session_overrun.py`` that guard is forced off to show
  that the rule behind it still works;
* **every descent fits the time it is given**, on the shipped settings and
  with a slew slower than the motion limits;
* **what stays latched**: an emergency stop and a drive fault at rest are
  still latched, shown, refused against and acknowledged by name.

The fake drive and the manual clock of ``tests/test_runtime.py``.

**What these cost, and why they are built the way they are.** Under coverage
one run of the shipped 30-minute programme costs about half a minute of CI.
So the shipped programme is run ONCE for the acceptance criterion, and each
of its late endings starts from a copy of the instant it is opened at; the
manual session with a person on board reaches a shortened limit (the limit is
one constant, read alike by the phase machine and by what the rule is
measured against); and everything else runs the rig's 130 s programme, which
has the same phases and the same late endings.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Final

import pytest

from src.local_panel import RUNTIME_LIMITS
from src.motor.drive import DriveFault
from src.result import Err, Ok, is_ok
from src.training import runtime as runtime_module
from src.training.hr_control import Gains
from src.training.motion import DEFAULT_MOTION_LIMITS, MotionLimits, ramp_duration
from src.training.plan import COMMISSIONED_DECEL_S, MIN_RECOVERY_S, TrainingProfile
from src.training.runtime import (
    BENCH_RECOVERY,
    MANUAL_SESSION_LIMIT,
    EndReason,
    ResetWhileCommanded,
    RuntimeLimits,
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
from src.units import Bpm, MotorRpm, RpmPerSecond, Seconds
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    REAL_PROFILE,
    RIG_MOTION,
    TICK,
    Occupant,
    Rig,
    _profile,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _rig,  # pyright: ignore[reportPrivateUsage]
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

SHORT_LIMIT: Final[Seconds] = Seconds(180.0)
"""A shortened manual session limit (3600 s on the machine), where the hour proves nothing."""

MIN_RUN: Final[int] = 55
"""The slowest running speed of every profile here, motor rpm: the last step of a stop."""

SLOW_SLEW: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(7.0),
    start_hysteresis_rpm=MotorRpm(10),
    gains=Gains(period=Seconds(10.0), step_cap=Seconds(20.0)),
)
"""A control law slower than the shipped motion limits (12.4 motor rpm/s): not a shipped setting.

Seven rpm/s in whole rpm per 0.2 s tick is one rpm a tick, five rpm/s: the
descent the control law paces is then two and a half times longer than the
motion-limited walk.
"""

SLOW_PROFILE: Final[TrainingProfile] = _profile(
    total_duration_s=Seconds(600.0),
    baseline_s=Seconds(10.0),
    warmup_max_s=Seconds(200.0),
    hold_min_s=Seconds(60.0),
    cooldown_s=Seconds(60.0),
    recovery_s=Seconds(60.0),
)
"""Long enough for that slow law to reach the profile's ceiling: 276 motor rpm by 300 s."""


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _floor_rule(rig: Rig) -> str | None:
    floor = rig.runtime.supervisor.floor
    return None if floor is None else floor.rule


def _believe(target: object, name: str, value: object) -> None:
    """Write one field through a parameter: a state the test sets by hand."""
    setattr(target, name, value)


def _expected(speed: int, motion: MotionLimits, slew: float | None) -> float:
    """The descent an ending is expected to make from ``speed``, worked out apart from the runtime.

    The specification its own figure is checked against. The motion-limited
    walk to zero; in a programme (``slew`` given, the arm turning) the longer
    of that walk with the wait before its last step, and of twice the setpoint
    over the slew, which bounds the control law's whole-rpm ramp; and the
    drive's 4 s ramp in every case.
    """
    walk = ramp_duration(MotorRpm(speed), MotorRpm(0), motion, GEOMETRY)
    assert walk is not None
    drive = float(COMMISSIONED_DECEL_S)
    if slew is None or speed == 0:
        return float(walk) + drive
    return max(float(walk) + MIN_RUN / slew, 2.0 * speed / slew) + drive


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

    at: float
    """Seconds after the start at which it happens."""

    what: str

    stands: str | None
    """The rule left latched once the session is over: the one that ended it, or none."""

    over_by: float
    """The session is over, the console at REPOS, by this many seconds after the start."""


_LATE: Final[tuple[_Late, ...]] = (
    _Late(1531.0, "a STOP", None, 1832.0),
    _Late(1600.0, "a STOP", None, 1901.0),
    _Late(1600.0, "an e-stop", RULE_OPERATOR_ESTOP, 1901.0),
    _Late(1600.0, "the electrodes taken off", RULE_HR_STALE, 1961.0),
    _Late(1799.0, "a STOP", None, 2100.0),
)
"""In the order of the clock: one programme is run through all of them."""


def _fork(rig: Rig) -> Rig:
    """An independent copy of a whole rig at this instant: runtime, drive, clock, occupant.

    What lets one run of the shipped programme serve several endings. The copy
    shares nothing mutable with the original (one ``deepcopy`` call keeps the
    three references to the clock pointing at the same new clock), and the test
    that uses it checks that two copies given the same tick answer the same.
    """
    return copy.deepcopy(rig)


async def test_an_ending_opened_late_in_the_shipped_programme_latches_no_overrun() -> None:
    """The first measured case, and the acceptance criterion. On ``develop``: 1830.2 s.

    The shipped programme is in its own recovery, the arm at rest. At 1531 s,
    the first second at which ``develop`` latched, a STOP is typed. At 1600 s,
    from that same instant: a STOP; the emergency stop; the electrodes come
    off and ``hr_stale`` ends the session 60 s later. At 1799 s, one second
    before the end, a STOP. Each opens a whole 300 s recovery, which ends
    after 1830 s. On every tick until the console is back at REPOS the rule
    does not fire and is not on the latched floor; what stands at the end is
    only what ended the session; one named acknowledgement clears it, and the
    next start is taken.
    """
    programme = await _shipped()
    await programme.run(_LATE[0].at)
    first, second = _fork(programme), _fork(programme)
    assert first.runtime is not second.runtime
    one, other = await first.step(), await second.step()
    assert one == other, "two copies of one rig did not answer the same tick alike"

    for late in _LATE:
        await programme.run(late.at - _since_start(programme))
        assert _applied(programme) == 0, "the programme is not in its own recovery: not this case"
        assert programme.state() is RuntimeState.RUNNING
        rig = _fork(programme)
        feed = late.what != "the electrodes taken off"
        if late.what == "a STOP":
            rig.runtime.request_stop("operator pressed STOP")
        elif late.what == "an e-stop":
            rig.runtime.request_estop("console web: e-stop")

        while rig.state() is not RuntimeState.FINISHED:
            await rig.step(feed=feed)
            at = f"{late.what} at {late.at:.0f} s, {_since_start(rig):.1f} s"
            assert _overrun(rig) is None, f"session_overrun fired over a normal ending: {at}"
            assert _floor_rule(rig) != RULE_SESSION_OVERRUN, at
            assert _since_start(rig) < late.over_by, f"the ending did not finish: {at}"
        assert _since_start(rig) > OLD_DEADLINE, f"{late.what}: over before the old deadline"
        assert _mode(rig) is RunMode.REPOS
        assert _standing_rule(rig) == late.stands, late.what

        if late.stands is not None:
            acknowledged = rig.runtime.acknowledge(OPERATOR, estop_released=True)
            assert isinstance(acknowledged, Ok), late.what
            assert acknowledged.value.cleared == (late.stands,)
        await rig.run(10.0, feed=feed)
        assert _standing_rule(rig) is None, late.what
        await _a_new_programme_starts(rig)
    assert programme.state() is RuntimeState.RUNNING, "an ending reached the rig it was copied from"


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

    limit: Seconds
    """The limit it runs to: the machine's hour, or a shortened one."""

    descent: tuple[float, float]
    """The walk to zero from that speed takes between these many seconds."""


_AT_THE_LIMIT: Final[tuple[_AtTheLimit, ...]] = (
    _AtTheLimit(
        "the capsule empty, the nameplate speed, the real hour",
        Occupancy.BENCH,
        1380,
        MANUAL_SESSION_LIMIT,
        (106.0, 108.0),
    ),
    _AtTheLimit("a person on board", Occupancy.OCCUPIED, 200, SHORT_LIMIT, (11.0, 13.0)),
)


@pytest.mark.parametrize("case", _AT_THE_LIMIT, ids=[case.who for case in _AT_THE_LIMIT])
async def test_a_manual_session_that_reaches_its_limit_at_speed_latches_no_overrun(
    monkeypatch: pytest.MonkeyPatch, case: _AtTheLimit
) -> None:
    """The second measured case, and the acceptance criterion. On ``develop``: 30.2 s past it.

    The console brings the arm down at the motion limits: 107 s from the
    1380 rpm nameplate, the highest ceiling a console can be given (104 s from
    the 1344 motor rpm of the measured case, which is the same walk started
    three seconds lower), at the machine's real limit of an hour. With a
    person on board a 60 s recovery follows, so that session latched on
    ``develop`` whatever its speed; its limit is shortened here, where the
    hour proves nothing. Nothing fires and nothing stands, from the limit to
    REPOS; the session ends as a completed one, and a new manual start is
    taken.
    """
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", case.limit)
    limit = float(case.limit)
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
    """The third measured case. On ``develop``: ARRET, a whole recovery, a latched ``hr_stale``.

    The programme has run to its own end. A minute later the electrodes are
    off, the rider is getting out, and somebody presses the emergency stop;
    or the drive is switched off and reports a fault; or its cable is pulled.
    On ``develop`` that opened an ending: ARRET for the 60 s of this
    programme's recovery (300 s on the shipped one), with ``hr_stale`` latched
    60 s after the last reading. Here the session stays over on every tick
    for the 200 s that follow: REPOS, the phase ``DONE``, no ending recorded,
    its own end reason kept, nothing commanded and the output stage off. The
    heart-rate rules judge nobody: no ``hr_stale`` at any tick with no heart
    rate at all.

    And the verdict is everything it was: latched at once, the only thing
    standing, and every start refused in its name.
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    await rig.run(58.0)
    await rig.run(5.0, feed=False)
    assert _standing_rule(rig) is None
    frames = len(rig.drive.writes)

    if arrives == "an e-stop":
        rig.runtime.request_estop("console web: e-stop")
        expected, action = RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP
        assert _standing_rule(rig) == expected, "the e-stop waited for a tick to latch"
    elif arrives == "a drive fault":
        rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
        expected, action = RULE_DRIVE_FAULT, SafetyAction.RAMP_DOWN
    else:
        rig.drive.break_comms()
        expected, action = RULE_COMMS_LOST, SafetyAction.GO_SILENT

    for _ in range(round(200.0 / TICK)):
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


@pytest.mark.parametrize("arrives", ["an e-stop", "a drive fault"])
async def test_a_verdict_over_a_setpoint_of_zero_and_a_shaft_still_turning_opens_no_ending(
    arrives: str,
) -> None:
    """A session is over by its setpoint, not by its shaft: what happens when the two disagree.

    The programme has run to its end: the setpoint is zero and the output
    stage is off. The shaft is then set turning by hand at 120 motor rpm, as
    an arm that somebody pushes, or that coasts. The verdict arrives over it.

    No ending is opened: there is no setpoint to bring down, and no torque to
    remove. The verdict is latched and is the only thing standing, every start
    is refused in its name, the reference stays at zero and the output stage
    stays off. The console shows REPOS, which says that nothing is commanded,
    and the measured speed goes on saying that the arm turns: it is the
    measured speed, never the mode, that says whether an arm is stopped.
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    _believe(rig.drive, "_rpm", 120.0)
    still = await rig.step()
    assert still.measured.motor_rpm > 100, "the shaft is not turning: not this case"
    assert still.setpoint.motor_rpm == 0
    assert rig.state() is RuntimeState.FINISHED
    frames = len(rig.drive.writes)

    if arrives == "an e-stop":
        rig.runtime.request_estop("console web: e-stop")
        expected = RULE_OPERATOR_ESTOP
    else:
        rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
        expected = RULE_DRIVE_FAULT
    first = await rig.step()
    assert first.measured.motor_rpm > 0, "the screen no longer shows the shaft turning"
    for snapshot in [first, *await rig.run(30.0)]:
        at = f"{_since_start(rig):.1f} s after the start"
        assert snapshot.mode is RunMode.REPOS, f"the console left REPOS, {at}"
        assert snapshot.phase is Phase.DONE
        assert snapshot.setpoint.motor_rpm == 0
        assert snapshot.safety is not None
        assert snapshot.safety.rule == expected, f"{snapshot.safety.rule} stands, {at}"
    assert rig.runtime.ending is None, "an ending was opened with nothing to bring down"
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _live_rules(rig) <= {expected}
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert not rig.drive.is_enabled()

    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == expected


# =========================================================================
# WHAT THE RUNTIME STATES
# =========================================================================


async def test_the_runtime_states_its_ending_from_the_tick_it_opens_until_the_next_start() -> None:
    """What reaches the rule, asserted on the recorded observations.

    None while the session runs. From the tick the STOP is honoured: when the
    ending opened, counted from the start; the descent expected from the
    setpoint that was in force at that instant; and the programme's recovery.
    The same three numbers on every tick from there, through the descent, the
    recovery and the rest that follows, whatever arrives meanwhile (an
    emergency stop here): an ending cannot move its own deadline. None again
    from the first tick of the next session.
    """
    rig = await _programme()
    with _observations() as seen:
        await rig.run(40.0)
        assert all(o.ending is None for o in seen), "an ending was stated before any opened"

        speed = int(_applied(rig))
        assert speed > 0, "the arm is not turning: not this case"
        rig.runtime.request_stop("operator pressed STOP")
        await rig.step()
        opened = _since_start(rig)
        stated = seen[-1].ending
        assert stated is not None
        assert stated.opened == Seconds(opened)
        assert float(stated.descent) == pytest.approx(
            _expected(speed, RIG_MOTION, float(LIMITS.slew))
        )
        assert stated.recovery == Seconds(RECOVERY)
        ending = rig.runtime.ending
        assert ending is not None
        assert ending.setpoint == speed

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

        rig.runtime.request_stop("operator pressed STOP")
        await rig.step()
        second = seen[-1].ending
        assert second is not None
        assert second.descent == COMMISSIONED_DECEL_S, "the descent of the first session was kept"


async def test_an_ending_opened_at_rest_is_given_the_drive_ramp_and_nothing_else() -> None:
    """The late endings of the ticket: nothing to bring down, so nothing to wait for.

    A STOP in a programme's BASELINE, the setpoint at zero. The ending states
    a descent of 4 s, the drive's own ramp, whatever the profile's cooldown
    and whatever speed the session could have reached.
    """
    rig = await _programme()
    await rig.run(5.0)
    assert rig.phase() is Phase.BASELINE
    with _observations() as seen:
        rig.runtime.request_stop("operator pressed STOP")
        await rig.step()
    stated = seen[-1].ending
    assert stated is not None
    assert stated.descent == COMMISSIONED_DECEL_S
    assert stated.recovery == Seconds(RECOVERY)


@pytest.mark.parametrize(
    ("occupancy", "recovery"),
    [(Occupancy.BENCH, BENCH_RECOVERY), (Occupancy.OCCUPIED, MIN_RECOVERY_S)],
)
async def test_a_manual_ending_is_stated_with_the_descent_from_the_speed_it_opened_at(
    occupancy: Occupancy, recovery: Seconds
) -> None:
    """What a manual ending is given: the walk from its speed, plus the drive's ramp.

    At 200 motor rpm under a ceiling of 300: the motion-limited walk to zero
    takes 12 s from there, and the drive's commissioned ramp 4 s. Not the 24 s
    of a walk from the ceiling. No recovery with the capsule empty, the
    shortest a profile may declare with a person on board.
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
    assert float(stated.descent) == pytest.approx(_expected(200, DEFAULT_MOTION_LIMITS, None))
    assert float(stated.descent) == pytest.approx(12.0 + float(COMMISSIONED_DECEL_S))
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
    and recovery. STOP is typed two seconds before the end: an ending opens,
    in time, and its descent is blocked too.

    That ending has the descent expected from the speed it found, a few
    seconds on this rig, and the grace. Not the programme's own deadline,
    which it has moved back by those few seconds; and none of its 60 s of
    recovery, which is not lent to an arm that is still turning. Past it the
    rule fires, RAMP_DOWN and latched; RAMP_DOWN outranks the FREEZE, the
    setpoint comes down to zero and the shaft follows.
    """
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(TOTAL - 2.0 - 40.0)
    held = _applied(rig)
    assert held > 0, "with the guard off the FREEZE did not hold the arm"
    rig.runtime.request_stop("operator pressed STOP")
    await rig.step()
    opened = _since_start(rig)
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP

    expected = _expected(int(held), RIG_MOTION, float(LIMITS.slew))
    due = opened + expected + 30.0
    assert due > DEADLINE + 1.0, "the ending's own deadline is not the later one: not this case"
    while _since_start(rig) < due - 1.0:
        await rig.step()
        assert _overrun(rig) is None, f"fired early, {_since_start(rig):.1f} s in"
        assert _applied(rig) == held, "the blocked descent moved: not this case"
    assert _since_start(rig) > DEADLINE, "quiet only because the old deadline had not come"

    await rig.run(2.0)
    verdict = _overrun(rig)
    assert verdict is not None, f"nothing ended a blocked descent {_since_start(rig):.1f} s in"
    assert _since_start(rig) < opened + RECOVERY, "the recovery was lent to the descent"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert f"with {expected:.0f} s to bring the setpoint back to zero" in verdict.detail
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN

    await rig.run(10.0)
    assert _applied(rig) == 0, "RAMP_DOWN did not bring the blocked descent down"
    assert abs(rig.drive.shaft_rpm) < 1.0


async def test_a_stop_on_an_arm_that_is_not_coming_down_buys_its_own_descent_and_no_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped programme, a blocked descent, STOP at 1799 s: 1859 s, not 2069 s.

    The guard forced off, a FREEZE latched at 900 s holds the arm at its HOLD
    speed through the cooldown and the recovery. With nobody touching
    anything the rule ends that at 1830.2 s. STOP is typed one second before
    the end and its descent does not happen either.

    The ending is given the descent expected from the 193 motor rpm it found,
    29.7 s, and the grace: the rule fires no later than that, at 1859 s. A
    budget taken from the profile's 240 s cooldown gave the same arm until
    2069.4 s, four minutes for having pressed STOP. What the ending still
    costs against doing nothing is the time its own descent would have taken.
    """
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _shipped()
    await rig.run(900.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(PLANNED - 1.0 - 900.0)
    held = int(_applied(rig))
    assert held > MIN_RUN, "with the guard off the FREEZE did not hold the arm at speed"
    rig.runtime.request_stop("operator pressed STOP")
    await rig.step()
    opened = _since_start(rig)

    expected = _expected(held, DEFAULT_MOTION_LIMITS, float(RUNTIME_LIMITS.slew))
    assert expected < 45.0, "the descent expected from a HOLD speed is not a matter of minutes"
    latest = opened + expected + 30.0
    while _overrun(rig) is None:
        await rig.step()
        assert _since_start(rig) <= latest + 2 * TICK, (
            f"an arm held at {held} motor rpm was left turning past its expected descent"
        )
        assert _applied(rig) == held or _overrun(rig) is not None
    verdict = _overrun(rig)
    assert verdict is not None
    assert _since_start(rig) > latest - 2 * TICK, "fired before the ending's own time had run out"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert f"an ending opened at {opened:.0f} s with {expected:.0f} s to bring" in verdict.detail

    await rig.run(30.0)
    assert _applied(rig) == 0, "RAMP_DOWN did not bring the held arm down"
    assert abs(rig.drive.shaft_rpm) < 1.0


async def test_a_manual_descent_blocked_below_the_ceiling_is_judged_on_the_speed_it_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EX-1, at the manual limit: the walk from the speed in force, not from the ceiling.

    A short limit, the guard forced off, a ceiling of 1380 motor rpm and a
    FREEZE latched while the arm turns at 300. At the limit the session ends
    itself and its descent is blocked. The walk from 300 takes 20 s, the
    drive's ramp 4 s: the rule fires 54 s after the limit and the setpoint
    comes down at the motion limits. A budget taken from the ceiling, whose
    walk takes 107 s, left that arm at 300 for 141 s.
    """
    limit = Seconds(100.0)
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", limit)
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _manual_session(_manual_rig(), ceiling=MotorRpm(1380))
    _target(rig, 300)
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(float(limit) - 40.0 + 1.0)
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _applied(rig) == 300, "with the guard off the FREEZE did not hold the arm"
    manual = rig.runtime.manual
    assert manual is not None
    assert float(manual.cooldown) > 100.0, (
        "the ceiling's own walk is not the long one: not this case"
    )

    expected = _expected(300, DEFAULT_MOTION_LIMITS, None)
    assert expected == pytest.approx(24.0)
    await rig.run(expected + 30.0 - 2.0)
    assert _overrun(rig) is None, f"fired early, {_since_start(rig):.1f} s in"
    assert _applied(rig) == 300
    await rig.run(2.0)
    verdict = _overrun(rig)
    assert verdict is not None, "an arm held below the ceiling was left to the ceiling's budget"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert "with 24 s to bring the setpoint back to zero" in verdict.detail

    await rig.run(40.0)
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
# EVERY DESCENT FITS THE TIME ITS ENDING IS GIVEN
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Pace:
    """One configuration under which a programme is brought to speed, then ended."""

    who: str
    limits: RuntimeLimits
    profile: TrainingProfile
    at_speed: float
    """Seconds after the start at which the arm turns at the profile's ceiling, in HOLD."""


_PACES: Final[tuple[_Pace, ...]] = (
    _Pace("the shipped settings", RUNTIME_LIMITS, REAL_PROFILE, 560.0),
    _Pace("a slew slower than the motion limits", SLOW_SLEW, SLOW_PROFILE, 300.0),
    _Pace("a slew far quicker than the motion limits", LIMITS, SLOW_PROFILE, 300.0),
)

_DESCENTS: Final[tuple[str, ...]] = (
    "a STOP",
    "a STOP under a latched FREEZE",
    "a verdict that ramps down",
)


@pytest.mark.parametrize("pace", _PACES, ids=[pace.who for pace in _PACES])
async def test_every_descent_of_a_programme_fits_the_time_its_ending_is_given(pace: _Pace) -> None:
    """The three descents a programme has, each from the profile's ceiling of 276 motor rpm.

    * an ordinary STOP: the setpoint walks at the motion limits towards the
      controller's demand, which comes down on the control law's ramp;
    * a STOP under a FREEZE: the motion-limited walk alone, and the wait
      before its last step;
    * a verdict's own descent (RAMP_DOWN): the control law's ramp.

    Each brings the setpoint to zero within the descent its ending states, so
    the 30 s of grace are not what a normal ending relies on. That holds on
    the shipped settings (15 rpm/s, 26 s at the longest against 40.8 s
    stated), and with a control law slower than the motion limits, which is
    not a shipped setting: at 7 rpm/s the ordinary descent takes 51.8 s and a
    verdict's 52.2 s where the walk alone takes 18 s. A time worked out from
    the walk, the drive's ramp and the grace together would be 52.0 s, which
    the second overruns; the ending is given 82.9 s. With a
    control law far quicker than the motion limits (70 rpm/s) it is the walk
    that paces every descent, and the walk that is stated.
    """
    programme = _rig(profile=pace.profile, limits=pace.limits, motion=DEFAULT_MOTION_LIMITS)
    programme.fed_bpm = Bpm(82)
    started = await programme.start()
    assert is_ok(started), started
    await programme.run(float(pace.profile.baseline_s) + 1.0)
    programme.fed_bpm = Bpm(65)
    await programme.run(pace.at_speed - _since_start(programme))
    speed = int(_applied(programme))
    assert speed == pace.profile.max_rpm, "the arm is not at the profile's ceiling: not this case"
    assert _standing_rule(programme) is None
    slew = float(pace.limits.slew)

    took: dict[str, float] = {}
    for what in _DESCENTS:
        rig = _fork(programme)
        with _observations() as seen:
            if what == "a STOP under a latched FREEZE":
                rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
            if what == "a verdict that ramps down":
                rig.runtime.trip_from_thread("rig_ramp_down", SafetyAction.RAMP_DOWN, "under test")
            else:
                rig.runtime.request_stop("operator pressed STOP")
            await rig.step()
            opened = _since_start(rig)
            while _applied(rig) != 0:
                await rig.step()
                assert _since_start(rig) < opened + 300.0, f"{what}: the setpoint never came down"
            stated = seen[-1].ending
        assert stated is not None, what
        assert float(stated.descent) == pytest.approx(_expected(speed, DEFAULT_MOTION_LIMITS, slew))
        took[what] = _since_start(rig) - opened
        assert took[what] <= float(stated.descent) - float(COMMISSIONED_DECEL_S) + TICK, (
            f"{what}: {took[what]:.1f} s to come down from {speed} motor rpm, "
            f"{float(stated.descent):.1f} s expected"
        )
    walk_alone = _expected(speed, DEFAULT_MOTION_LIMITS, None)
    assert took["a STOP under a latched FREEZE"] < took["a STOP"] + 2 * TICK
    if slew < 12.0:
        assert took["a STOP"] > walk_alone + 25.0, "the slow law is not what paced this descent"
    if slew > 30.0:
        assert took["a STOP"] > 2.0 * speed / slew + 5.0, "the walk is not what paced this descent"


# =========================================================================
# WITH NO ENDING OPENED: the rule is what it was
# =========================================================================


async def test_a_programme_run_to_its_end_states_no_ending_and_is_never_judged() -> None:
    """EX-2 on the tick: a session that opens no ending gives the rule nothing new to read.

    A programme, nobody touching anything. No ending is stated on any tick,
    from the first one to five minutes of rest, so the rule reads exactly what
    it read before this change, and it never fires.
    """
    rig = await _programme()
    with _observations() as seen:
        await rig.run(TOTAL + 2.0)
        assert rig.state() is RuntimeState.FINISHED
        await rig.run(300.0)
    assert seen, "nothing was observed: not this case"
    assert all(o.ending is None for o in seen)
    assert rig.runtime.ending is None
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _standing_rule(rig) is None
