"""A warning that walks the arm to a standstill ends the session there (ANH-176).

The product decision of 2026-10-05 (``docs/securite.md``): a warning that is
not latched lifts by itself when its cause clears, and the setpoint then
follows the controller again with nobody clicking. That is kept while the
speed was only HELD (FREEZE) or lowered PART WAY (REDUCE): the arm never
stopped. Once a REDUCE has walked the setpoint all the way to zero with a
person on board, the session ends there, latched, and nothing moves again
without an acknowledgement by name and a new start.

The scenario behind it is the detached electrode. Measured on ``develop``, on
the shipped 30 min programme with the console's own limits: 12 s without a
heart rate, 164 motor rpm held; 32 s, 122 rpm under REDUCE; 42 s, standstill,
the mode still SEANCE; the heart rate returns at 50 s with nobody clicking; 45 s
after that the setpoint is 69 rpm, and two minutes after it 168. The operator saw the
arm stopped, walked to the capsule to refit the electrode, and it started
beside them.

Three groups of tests, and the split matters when reading a failure:

* **the decision**: these fail on ``develop`` (the arm restarts, or nothing is
  latched) and pass here;
* **unchanged on purpose**: a held speed and a speed lowered part way still
  resume by themselves, and the cases the decision does not cover (a warning
  that finds the arm already stopped, a BENCH session, an ending already under
  way) behave exactly as before. These pass on both;
* **what the screen is told** while the speed can still resume by itself.

Every test runs on the fake drive and the manual clock of
``tests/test_runtime.py``. Where the machine's own numbers matter (the
electrode scenario) the rig uses the shipped profile, the console's
``RUNTIME_LIMITS`` and the shipped motion limits; elsewhere the accelerated rig
keeps the walk to standstill short.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import pytest

from src.local_panel import RUNTIME_LIMITS, describe_start_refusal
from src.motor.drive import CommTimeout, ControlWord
from src.result import Err, Ok, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.plan import MIN_RECOVERY_S, TrainingProfile
from src.training.runtime import (
    RULE_REDUCED_TO_STANDSTILL,
    AlreadyStarted,
    Ending,
    EndReason,
    ManualEnding,
    RuntimeState,
    SafetyStanding,
)
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_CURRENT_HIGH,
    RULE_HR_CRITICAL,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    RULE_OPERATOR_ESTOP,
    SELF_CLEARING,
    GoSilentIsTerminal,
    SafetyLimits,
    SafetySupervisor,
    Unattributed,
)
from src.training.types import (
    Occupancy,
    Phase,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    TelemetrySnapshot,
    is_rule_id,
)
from src.units import Amperes, Bpm, MotorRpm, OutputRpm, Seconds, motor_to_output_rpm
from src.web.schemas import SnapshotRow
from tests.test_runtime import (
    GEOMETRY,
    OPERATOR,
    REAL_PROFILE,
    Occupant,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _lenient_comms,  # pyright: ignore[reportPrivateUsage]
    _profile,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
    _running_rig,  # pyright: ignore[reportPrivateUsage]
    _safety_for,  # pyright: ignore[reportPrivateUsage]
)

AT_SPEED: Final[float] = 420.0
"""Seconds into the shipped programme at which the arm turns at its WARMUP ceiling."""

IN_HOLD: Final[float] = 500.0
"""Seconds into the shipped programme at which HOLD is under way, with speed still to gain."""

LONG_HOLD: Final[TrainingProfile] = _profile(total_duration_s=Seconds(600.0))
"""The accelerated rig's profile with a HOLD of several minutes (the default one is 30 s)."""

MANUAL_CEILING: Final[MotorRpm] = MotorRpm(300)
MIN_RUN: Final[MotorRpm] = REAL_PROFILE.min_run_rpm


# =========================================================================
# Reading the runtime through calls
# =========================================================================
#
# mypy narrows a property to what a previous assertion established and then
# calls the next assertion about the same expression unreachable. Every one of
# these tests is "it was X, then it became Y", so each fact is read through a
# call, which is re-widened every time (see ``Rig.phase`` in test_runtime.py).


def _standing(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.standing


def _demand(rig: Rig) -> tuple[str, SafetyAction, bool] | None:
    """The standing verdict as ``(rule, action, latched)``, or ``None``."""
    verdict = rig.runtime.standing
    return None if verdict is None else (verdict.rule, verdict.action, verdict.latched)


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _end(rig: Rig) -> EndReason | None:
    return rig.runtime.end_reason


def _ending(rig: Rig) -> Ending | None:
    return rig.runtime.ending


def _mode(rig: Rig) -> RunMode:
    return rig.runtime.mode


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _rules_shown(snapshots: list[TelemetrySnapshot]) -> set[str]:
    return {snapshot.safety.rule for snapshot in snapshots if snapshot.safety is not None}


def _live_rules(rig: Rig) -> set[str]:
    return {verdict.rule for verdict in rig.runtime.supervisor.live}


STANDSTILL: Final[tuple[str, SafetyAction, bool]] = (
    RULE_REDUCED_TO_STANDSTILL,
    SafetyAction.RAMP_DOWN,
    True,
)
"""What stands once a warning has walked the arm to zero: this module's latched ending."""


# =========================================================================
# Rigs
# =========================================================================


async def _console_rig(seconds: float = AT_SPEED) -> Rig:
    """The shipped programme on the console's own limits, a simulated occupant, the arm turning."""
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    await rig.run(seconds)
    assert _applied(rig) > MIN_RUN, "the arm is not turning yet"
    assert _standing(rig) is None
    return rig


async def _until_standstill(rig: Rig, *, feed: bool = True, limit: float = 120.0) -> None:
    """Tick until the setpoint is zero. Fails if it never gets there within ``limit`` seconds."""
    for _ in range(round(limit / 0.2)):
        if _applied(rig) == 0:
            return
        await rig.step(feed=feed)
    assert _applied(rig) == 0, f"the setpoint never reached zero: {_applied(rig)}"


async def _stopped_by_a_lost_heart_rate() -> Rig:
    """The electrode scenario up to the standstill: no heart rate, the REDUCE has reached zero."""
    rig = await _console_rig()
    await _until_standstill(rig, feed=False)
    return rig


async def _occupied_manual(target: int = 200) -> Rig:
    """A manual session with a person declared on board, at ``target`` motor rpm."""
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(80)
    rig.feed(Bpm(80))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(
        await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING),
    )
    wanted = motor_to_output_rpm(MotorRpm(target), GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MotorRpm(target))
    await rig.run(40.0)
    assert _applied(rig) == target
    return rig


def _low_current_warning(profile: TrainingProfile) -> SafetyLimits:
    """The profile's limits with the current WARNING within the fake drive's reach.

    The fake drive draws 0.5 A at standstill and 0.82 A at the rig's ceiling, so
    the machine's 2.4 A warning can never be reached on it. Lowered here (0.7 A,
    released at 0.55 A) the real rule fires at speed and necessarily clears at
    standstill, which is what a binding arm does: the current falls with the
    speed. No threshold of the machine is changed.
    """
    return replace(
        _safety_for(profile),
        current_warn_a=Amperes(0.7),
        current_release_a=Amperes(0.15),
    )


# =========================================================================
# THE DECISION: fails on develop, passes here
# =========================================================================


async def test_an_arm_stopped_by_a_lost_heart_rate_does_not_restart_when_it_returns() -> None:
    """THE ticket's scenario, on the machine's own numbers: the detached electrode.

    The heart rate is lost for 50 s and then comes back, and nobody clicks
    anything. The arm is held (FREEZE at 10 s), lowered (REDUCE at 30 s) and
    reaches standstill. From there it must not move again, for as long as
    anybody cares to wait: on ``develop`` the first non-zero setpoint came 40 s
    after the heart rate returned, and it was back to 69 motor rpm 5 s later.
    """
    rig = await _console_rig()
    running = _applied(rig)

    held = await rig.run(12.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert _setpoints(held)[-1] == running, "FREEZE did not hold the speed"

    await rig.run(20.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert 0 < _applied(rig) < running, "REDUCE did not lower the speed"

    await rig.run(18.0, feed=False)
    assert _applied(rig) == 0, "the REDUCE never reached standstill"
    assert abs(rig.drive.shaft_rpm) < 1.0

    # 50 s after it was lost, the heart rate is back. Nobody clicks.
    frames = len(rig.drive.writes)
    back = await rig.run(180.0)
    moved = [
        (float(snapshot.elapsed), int(snapshot.setpoint.motor_rpm))
        for snapshot in back
        if snapshot.setpoint.motor_rpm != 0
    ]
    assert not moved, f"the arm restarted by itself (session time, motor rpm): {moved[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}, "a non-zero reference was written"
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commanded_rpm == 0
    assert not rig.drive.is_enabled(), "the output stage was left energised at standstill"
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert {snapshot.mode for snapshot in back} <= {RunMode.ARRET, RunMode.REPOS}
    assert all(snapshot.live_bpm is not None for snapshot in back[-25:]), "no heart rate came back"


async def test_the_standstill_is_a_latched_ending_that_names_the_warning() -> None:
    """What the machine is, and says, on the tick the descent reaches zero and afterwards."""
    rig = await _stopped_by_a_lost_heart_rate()
    reached = rig.snapshots[-1]

    # The verdict: this module's own, latched, the same demand as the 60 s end.
    assert is_rule_id(RULE_REDUCED_TO_STANDSTILL)
    assert _demand(rig) == STANDSTILL
    shown = reached.safety
    assert shown is not None
    assert (shown.rule, shown.action, shown.latched) == STANDSTILL
    assert RULE_HR_STALE in shown.detail, "the operator is not told which warning did it"
    assert "does not restart by itself" in shown.detail
    assert SELF_CLEARING not in shown.detail

    # The ending: recorded once, on that tick, with that verdict as its cause.
    ending = _ending(rig)
    assert ending is not None
    assert ending.reason is EndReason.SAFETY_VERDICT
    assert ending.action is SafetyAction.RAMP_DOWN
    assert ending.detail.startswith(f"{RULE_REDUCED_TO_STANDSTILL}: ")
    assert ending.at == reached.at
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert rig.state() is RuntimeState.ENDING
    assert reached.mode is RunMode.ARRET
    assert reached.phase is Phase.COOLDOWN

    # The run command is NOT removed on that tick: the shaft is still coming
    # down from the minimum running speed, and word 6 on a turning shaft is
    # CiA402 transition 8 (a freewheel).
    assert rig.drive.shaft_rpm >= 1.0
    assert rig.drive.is_enabled()
    await rig.run(2.0, feed=False)
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commands[-2:] == [ControlWord.SWITCH_ON, ControlWord.SHUTDOWN]
    assert not rig.drive.is_enabled()
    assert not rig.runtime.output_enabled

    # RECOVERY is kept, with the heart-rate rules still live, then DONE.
    assert rig.phase() is Phase.RECOVERY
    assert RULE_HR_STALE in _live_rules(rig)
    assert _ending(rig) == ending, "a later tick rewrote the ending"
    rig.fed_bpm = Bpm(82)
    await rig.run(float(REAL_PROFILE.recovery_s) + 1.0)
    assert rig.phase() is Phase.DONE
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    assert _demand(rig) == STANDSTILL, "the latch did not outlive the session"
    assert _applied(rig) == 0


async def test_no_start_is_accepted_until_the_standstill_is_acknowledged_by_name() -> None:
    """A programme, a manual session: nothing arms until a named operator has acknowledged."""
    rig = await _stopped_by_a_lost_heart_rate()
    rig.fed_bpm = Bpm(82)
    await rig.run(5.0)

    # Still ENDING: the latch is what refuses, not merely "busy".
    busy = await rig.start()
    assert isinstance(busy, Err)
    assert isinstance(busy.error, SafetyStanding)
    assert busy.error.verdict.rule == RULE_REDUCED_TO_STANDSTILL
    assert RULE_REDUCED_TO_STANDSTILL in describe_start_refusal(busy.error)

    await rig.run(float(REAL_PROFILE.recovery_s) + 1.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    programme = await rig.start()
    assert isinstance(programme, Err)
    assert isinstance(programme.error, SafetyStanding)
    manual = await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING)
    assert isinstance(manual, Err)
    assert isinstance(manual.error, SafetyStanding)
    assert manual.error.verdict.rule == RULE_REDUCED_TO_STANDSTILL
    assert max(rig.drive.writes[-50:]) == 0

    unnamed = rig.runtime.acknowledge("   ")
    assert isinstance(unnamed, Err)
    assert isinstance(unnamed.error, Unattributed)
    assert _demand(rig) == STANDSTILL

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.operator == OPERATOR
    assert acknowledged.value.cleared == (RULE_REDUCED_TO_STANDSTILL,)
    assert _standing(rig) is None

    # A NEW session, from scratch: BASELINE at standstill, as any start.
    assert is_ok(await rig.start())
    assert rig.state() is RuntimeState.RUNNING
    assert rig.phase() is Phase.BASELINE
    assert _end(rig) is None
    assert _ending(rig) is None
    fresh = await rig.run(10.0)
    assert set(_setpoints(fresh)) == {0}
    assert {snapshot.mode for snapshot in fresh} == {RunMode.SEANCE}


async def test_acknowledging_the_standstill_does_not_resume_the_session() -> None:
    """An acknowledgement puts the machine back into service; it restarts nothing.

    Acknowledged at once, with the heart rate still missing: the warning is
    back as the standing verdict (unlatched, at a setpoint of zero), the session
    is still ending, and when the heart rate returns nothing moves. The latch
    is not raised a second time: the descent that reached zero is over.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)

    blind = await rig.run(4.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert set(_setpoints(blind)) == {0}
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(blind)
    assert _ending(rig) == ending
    assert rig.state() is RuntimeState.ENDING

    rig.fed_bpm = Bpm(82)
    back = await rig.run(120.0)
    assert _standing(rig) is None
    assert set(_setpoints(back)) == {0}, "the acknowledgement resumed the session"
    assert {snapshot.mode for snapshot in back} == {RunMode.ARRET}
    refused = await rig.start()
    assert isinstance(refused, Err)
    assert refused.error == AlreadyStarted(RuntimeState.ENDING)


async def test_a_manual_session_with_a_person_on_board_ends_at_the_standstill_too() -> None:
    """MANUEL, occupied: on ``develop`` the arm was back at its 200 rpm target 15 s later."""
    rig = await _occupied_manual(200)
    await rig.run(12.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert _applied(rig) == 200
    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _mode(rig) is RunMode.ARRET
    assert rig.runtime.manual_target == 0, "after ANY stop the target is zero"

    target = motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)
    refused = rig.runtime.set_manual_target(target)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ManualEnding)

    back = await rig.run(float(MIN_RECOVERY_S) + 30.0)
    assert set(_setpoints(back)) == {0}, "the arm went back to the operator's target"
    assert all(snapshot.manual is not None for snapshot in back)
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    again = await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING)
    assert isinstance(again, Err)
    assert isinstance(again.error, SafetyStanding)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING))
    assert rig.runtime.manual_target == 0


async def test_a_target_of_zero_typed_during_the_descent_does_not_exempt_the_standstill() -> None:
    """MANUEL, occupied: the operator asks for zero while the warning is lowering the speed.

    Under REDUCE the verdict's own step IS the setpoint, whatever the target,
    so the standstill is still the warning's and the session ends there. The
    cautious reading of the decision, pinned so that it is a choice and not an
    accident: it costs one acknowledgement, and it keeps one rule.
    """
    rig = await _occupied_manual(200)
    await rig.run(32.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    assert rig.runtime.set_manual_target(OutputRpm(0.0)) == Ok(MotorRpm(0))
    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL
    assert _end(rig) is EndReason.SAFETY_VERDICT


# --- every other warning that lowers the speed without latching ----------


async def test_a_heart_rate_rising_too_fast_that_reaches_standstill_ends_the_session() -> None:
    """``hr_rate``: REDUCE while the rate rises faster than 25 bpm/min, released below 15.

    The rate climbs 30 bpm/min for 50 s and then settles. On ``develop`` the
    warning lifted about 20 s after the standstill and the arm restarted 50 s
    later, into a heart that had just been judged to be rising too fast.
    """
    rig = _rig(profile=REAL_PROFILE, limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(80)
    assert is_ok(await rig.start())
    await rig.run(AT_SPEED)
    assert _applied(rig) > MIN_RUN
    for second in range(50):
        rig.fed_bpm = Bpm(80 + (second + 1) // 2)
        await rig.run(1.0)
    assert _demand(rig) == (RULE_HR_RATE, SafetyAction.REDUCE, False)
    assert _applied(rig) > 0
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert RULE_HR_RATE in reached.detail

    settled = await rig.run(150.0)
    assert RULE_HR_RATE not in _live_rules(rig), "the warning never lifted: nothing was proven"
    assert set(_setpoints(settled)) == {0}, "the arm restarted when the rise settled"
    assert _end(rig) is EndReason.SAFETY_VERDICT


async def test_a_heart_that_ignores_the_load_and_reaches_standstill_ends_the_session() -> None:
    """``hr_unresponsive``: REDUCE when the load rose and the heart rate did not move.

    A flat 100 bpm under a programme that climbs to 0.33 g. The warning walks
    the arm to zero and, with the load gone, eventually lifts by itself. On
    ``develop`` the arm then climbed straight back to its ceiling, under the
    very load the heart had just been shown not to answer.
    """
    profile = _profile(
        total_duration_s=Seconds(1200.0),
        baseline_s=Seconds(60.0),
        hold_min_s=Seconds(900.0),
        max_rpm=MotorRpm(700),
    )
    rig = _rig(profile=profile)
    rig.fed_bpm = Bpm(100)
    assert is_ok(await rig.start())
    for _ in range(round(300.0 / 0.2)):
        await rig.step()
        if _standing(rig) is not None:
            break
    assert _demand(rig) == (RULE_HR_UNRESPONSIVE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert RULE_HR_UNRESPONSIVE in reached.detail

    after = await rig.run(320.0)
    assert RULE_HR_UNRESPONSIVE not in _live_rules(rig)
    assert set(_setpoints(after)) == {0}, "the arm climbed back under the load"
    assert rig.state() is RuntimeState.FINISHED


async def test_a_motor_current_warning_that_reaches_standstill_ends_the_session() -> None:
    """``current_high`` at its warning level: the one that ALWAYS clears at standstill.

    A current above the warning level means something is binding. The current
    falls with the speed, so at standstill the warning lifts by construction:
    on ``develop`` the arm was back at 83 rpm two seconds after it stopped, and
    cycled like that for as long as the session lasted.
    """
    profile = LONG_HOLD
    rig = await _running_rig(profile=profile, safety=_low_current_warning(profile))
    for _ in range(round(60.0 / 0.2)):
        await rig.step()
        if _standing(rig) is not None:
            break
    assert _demand(rig) == (RULE_CURRENT_HIGH, SafetyAction.REDUCE, False)
    assert rig.phase() is Phase.HOLD
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert RULE_CURRENT_HIGH in reached.detail

    after = await rig.run(30.0)
    assert RULE_CURRENT_HIGH not in _live_rules(rig), "the current warning never lifted"
    assert set(_setpoints(after)) == {0}, "the arm restarted as soon as the current fell"


async def test_a_latched_reduce_ends_at_standstill_so_an_acknowledgement_restarts_nothing() -> None:
    """A REDUCE that latches (a trip from a thread) is covered by the same arm.

    No rule produces one today. If one ever does: on ``develop`` it held the
    arm at zero until somebody acknowledged it, and the acknowledgement then
    restarted the arm inside the same session, which is the resumption on an
    operator's gesture the decision rejected.
    """
    rig = await _running_rig(profile=LONG_HOLD)
    # Back to the resting rate the rig measured in BASELINE: still far below the
    # zone, so the control law wants speed, and no fall for hr_drop to judge
    # once the load is gone.
    rig.fed_bpm = Bpm(82)
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    await rig.step()
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == ("rig_reduce", RULE_REDUCED_TO_STANDSTILL)
    after = await rig.run(20.0)
    assert _standing(rig) is None
    assert set(_setpoints(after)) == {0}, "the acknowledgement restarted the arm"
    assert _end(rig) is EndReason.SAFETY_VERDICT


# --- the last step of the descent ----------------------------------------


async def test_a_zero_the_drive_did_not_acknowledge_ends_nothing_until_it_lands() -> None:
    """The ending is taken on an ACKNOWLEDGED zero, never on one that was only sent.

    The setpoint believed in force advances on an acknowledged write and on
    nothing else. While the last step of the descent is refused, the arm is
    still commanded at its minimum running speed: the session is not over, and
    the next tick tries again.
    """
    rig = await _running_rig(profile=LONG_HOLD, safety=_lenient_comms())
    minimum = LONG_HOLD.min_run_rpm
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.REDUCE
        for _ in range(100):
            if _applied(rig) == minimum:
                break
            await rig.step()
        assert _applied(rig) == minimum
        # Shorter than the fake drive's own ttO (2 s), so the drive does not
        # fault for want of a keepalive: the only thing failing is the write.
        rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
        rig.drive.trace.clear()
        refused = await rig.run(1.4)
        assert f"lfrd:{MotorRpm(0)}" in rig.drive.trace, "the last step was never attempted"
        assert f"lfrd:{MotorRpm(0)}:ack" not in rig.drive.trace
        assert set(_setpoints(refused)) == {int(minimum)}
        assert rig.drive.commanded_rpm == minimum
        assert _end(rig) is None
        assert rig.state() is RuntimeState.RUNNING
        assert _standing(rig) is None

        rig.drive.speed_error = None
        await rig.step()
        assert _applied(rig) == 0
        assert rig.drive.commanded_rpm == 0
        assert _demand(rig) == STANDSTILL
        assert _end(rig) is EndReason.SAFETY_VERDICT
    assert rig.runtime.supervisor.floor is None, "something else ended the session"


# --- what may still happen after the standstill --------------------------


async def test_the_rule_s_own_latched_end_at_sixty_seconds_joins_the_standstill_latch() -> None:
    """The heart rate never returns: ``hr_stale`` latches its own RAMP_DOWN at 60 s.

    Two latches then stand, of equal severity. The one in force is the first,
    the ending keeps the cause it was recorded with, and one acknowledgement
    names both.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    await rig.run(25.0, feed=False)
    floor = rig.runtime.supervisor.floor
    assert floor is not None
    assert (floor.rule, floor.action, floor.latched) == (
        RULE_HR_STALE,
        SafetyAction.RAMP_DOWN,
        True,
    )
    assert _demand(rig) == STANDSTILL
    assert _ending(rig) == ending
    assert _applied(rig) == 0

    rig.fed_bpm = Bpm(82)
    await rig.run(5.0)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_HR_STALE, RULE_REDUCED_TO_STANDSTILL)
    assert _standing(rig) is None
    after = await rig.run(30.0)
    assert set(_setpoints(after)) == {0}


async def test_an_emergency_stop_after_the_standstill_takes_over_and_rewrites_nothing() -> None:
    """QUICK_STOP outranks the standstill latch; the ending keeps its first cause."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    rig.runtime.request_estop("under test")
    await rig.run(2.0, feed=False)
    assert _demand(rig) == (RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP, True)
    assert _ending(rig) == ending
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _applied(rig) == 0
    still_pressed = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(still_pressed, Err)
    assert _demand(rig) == (RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP, True)
    released = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(released, Ok)
    assert released.value.cleared == (RULE_OPERATOR_ESTOP, RULE_REDUCED_TO_STANDSTILL)


async def test_a_stop_asked_for_after_the_standstill_changes_nothing() -> None:
    """The session has already ended: an operator stop on top of it is recorded, and no more."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert ending is not None
    rig.runtime.request_stop("operator: stop button")
    after = await rig.run(3.0, feed=False)
    assert _ending(rig) == ending
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _demand(rig) == STANDSTILL
    assert rig.runtime.stop_reason == "operator: stop button"
    assert set(_setpoints(after)) == {0}


async def test_a_lost_link_after_the_standstill_goes_silent_and_is_terminal() -> None:
    """GO_SILENT after the standstill: no further frame, and nothing can be acknowledged away."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert ending is not None
    rig.drive.break_comms()
    await rig.run(2.0, feed=False)
    assert rig.runtime.silent
    assert _demand(rig) == (RULE_COMMS_LOST, SafetyAction.GO_SILENT, True)
    assert _ending(rig) == ending, "the later cause rewrote the ending"
    assert _applied(rig) == 0
    refused = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, GoSilentIsTerminal)
    again = await rig.start()
    assert isinstance(again, Err)
    assert isinstance(again.error, AlreadyStarted)


# =========================================================================
# UNCHANGED ON PURPOSE: passes on develop and here
# =========================================================================


async def test_a_held_speed_resumes_by_itself_when_the_heart_rate_returns() -> None:
    """FREEZE only: 20 s without a heart rate, the speed held, then regulation carries on."""
    rig = await _console_rig(IN_HOLD)
    assert rig.phase() is Phase.HOLD
    held = _applied(rig)
    assert held < REAL_PROFILE.max_rpm, "no speed left to gain: a resume would not show"
    frozen = await rig.run(20.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert set(_setpoints(frozen)[-40:]) == {int(held)}

    resumed = await rig.run(40.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _mode(rig) is RunMode.SEANCE
    assert _applied(rig) > held, "regulation did not resume"
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(frozen + resumed)


async def test_a_page_closed_for_a_hundred_seconds_holds_then_regulation_resumes() -> None:
    """``attendant_absent``: FREEZE at 60 s without a presence ping, released when it returns."""
    rig = await _console_rig()
    away = await rig.run(100.0, ping=False)
    assert _demand(rig) == (RULE_ATTENDANT_ABSENT, SafetyAction.FREEZE, False)
    held = _applied(rig)
    assert set(_setpoints(away)[-190:]) == {int(held)}, "the speed moved while nobody watched"
    assert held < REAL_PROFILE.max_rpm

    back = await rig.run(45.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _applied(rig) > held, "regulation did not resume when the page reopened"
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(away + back)


async def test_a_speed_lowered_part_way_resumes_by_itself_when_the_heart_rate_returns() -> None:
    """REDUCE part way: 36 s without a heart rate, the arm slowed but never stopped."""
    rig = await _console_rig()
    running = _applied(rig)
    lost = await rig.run(36.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    low = _applied(rig)
    assert MIN_RUN <= low < running
    assert min(_setpoints(lost)) == low

    back = await rig.run(60.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert min(_setpoints(back)) >= MIN_RUN, "the arm stopped after all"
    assert _applied(rig) > low, "regulation did not resume"
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(lost + back)


async def test_a_warning_that_lifts_at_the_minimum_running_speed_resumes() -> None:
    """The drive's minimum is not zero: parked at 55 motor rpm, the arm is still turning.

    The descent stops at the minimum running speed until it has earned the
    whole jump to zero (3.7 s at 15 rpm/s). A warning that lifts there has not
    stopped the arm, so regulation resumes as from any other speed.
    """
    rig = await _console_rig()
    for _ in range(round(60.0 / 0.2)):
        if _applied(rig) == MIN_RUN:
            break
        await rig.step(feed=False)
    assert _applied(rig) == MIN_RUN
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)

    back = await rig.run(60.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert min(_setpoints(back)) >= MIN_RUN
    assert _applied(rig) > MIN_RUN


# --- overtaken before standstill: the other verdict owns the ending -------


async def test_a_critical_heart_rate_during_the_descent_ends_it_as_before() -> None:
    """QUICK_STOP before the REDUCE reaches zero: the zero is the emergency's, not the warning's."""
    rig = await _console_rig()
    await rig.run(33.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    rig.fed_bpm = REAL_PROFILE.critical_bpm
    after = await rig.run(20.0)
    assert _demand(rig) == (RULE_HR_CRITICAL, SafetyAction.QUICK_STOP, True)
    assert _applied(rig) == 0
    ending = _ending(rig)
    assert ending is not None
    assert ending.detail.startswith(f"{RULE_HR_CRITICAL}: ")
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(after)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_HR_CRITICAL,)


async def test_a_descent_too_long_to_reach_zero_ends_on_the_rule_s_own_latch() -> None:
    """From 700 motor rpm the REDUCE cannot reach zero before ``hr_stale`` latches at 60 s."""
    profile = _profile(
        total_duration_s=Seconds(900.0),
        hold_min_s=Seconds(600.0),
        max_rpm=MotorRpm(700),
    )
    rig = _rig(profile=profile, limits=RUNTIME_LIMITS)
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.fed_bpm = Bpm(65)
    await rig.run(200.0)
    assert _applied(rig) == 700
    seen = await rig.run(61.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.RAMP_DOWN, True)
    assert _applied(rig) > 0, "the descent was not supposed to be over yet"
    seen += await rig.run(40.0, feed=False)
    assert _applied(rig) == 0
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.RAMP_DOWN, True)
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(seen)


# --- outside the decision: measured, and left exactly as they were --------


async def test_a_warning_that_finds_the_arm_already_stopped_ends_nothing() -> None:
    """OPEN QUESTION for the product owner: the standstill here is not the warning's doing.

    No heart rate for the first 45 s of BASELINE (electrodes still being
    fitted): FREEZE at 10 s, REDUCE at 30 s, at a setpoint that has been zero
    since the start. The warning then lifts, the programme carries on, and the
    arm makes its FIRST motion at the start of WARMUP with nobody clicking.
    Unchanged by ANH-176.
    """
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    blind = await rig.run(45.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert set(_setpoints(blind)) == {0}
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING

    later = await rig.run(255.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.phase() is Phase.WARMUP
    assert _applied(rig) >= MIN_RUN, "the programme never made its first motion"
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(blind + later)


async def test_a_bench_session_stopped_by_a_warning_still_restarts_by_itself() -> None:
    """OPEN QUESTION for the product owner: BENCH (nobody declared on board) is not covered.

    The heart-rate rules are off on the bench, so the one warning that can walk
    a bench session to zero is ``current_high`` - which always lifts at
    standstill. The session is not ended, the operator's target is kept, and
    the arm climbs back towards it with nobody clicking. Unchanged by ANH-176.
    """
    rig = _rig(motion=DEFAULT_MOTION_LIMITS, safety=_low_current_warning(_profile()))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    wanted = motor_to_output_rpm(MANUAL_CEILING, GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MANUAL_CEILING)
    climb = await rig.run(21.0, feed=False)
    assert _demand(rig) == (RULE_CURRENT_HIGH, SafetyAction.REDUCE, False)
    await _until_standstill(rig, feed=False)
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert rig.runtime.manual_target == MANUAL_CEILING

    after = await rig.run(20.0, feed=False)
    assert max(_setpoints(after)) >= MIN_RUN, "the bench arm did not restart: update this test"
    assert _end(rig) is None
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(climb + after)


async def test_an_operator_stop_under_a_warning_keeps_its_own_ending() -> None:
    """A stop was asked for first: the standstill is the operator's, and nothing latches."""
    rig = await _running_rig(profile=LONG_HOLD)
    rig.fed_bpm = Bpm(82)  # the rig's resting rate: no fall for hr_drop once the load is gone
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.REDUCE
        await rig.step()
        assert _applied(rig) > 0
        rig.runtime.request_stop("operator: stop button")
        await _until_standstill(rig)
        # Reached under the imposed REDUCE, and nothing of this module's latched.
        assert _standing(rig) is None
        assert _end(rig) is EndReason.OPERATOR_STOP
        imposed.action = None
        seen = await rig.run(75.0)
    assert _standing(rig) is None
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert rig.state() is RuntimeState.FINISHED
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(seen)
    assert is_ok(await rig.start()), "an operator stop must not need an acknowledgement"


async def test_a_warning_during_the_programme_s_own_cooldown_lets_it_complete() -> None:
    """COOLDOWN on the timeline: the programme was going to zero anyway, and nothing latches."""
    rig = await _running_rig()
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        for _ in range(round(60.0 / 0.2)):
            if rig.phase() is Phase.COOLDOWN:
                break
            await rig.step()
        assert rig.phase() is Phase.COOLDOWN
        assert _applied(rig) > 0, "the cooldown was already over"
        imposed.action = SafetyAction.REDUCE
        await _until_standstill(rig)
        assert rig.phase() is Phase.COOLDOWN
        assert _end(rig) is None
        imposed.action = None
        seen = await rig.run(75.0)
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert _ending(rig) is None
    assert rig.state() is RuntimeState.FINISHED
    assert _standing(rig) is None
    assert set(_setpoints(seen)) == {0}
    assert RULE_REDUCED_TO_STANDSTILL not in _rules_shown(seen)


# =========================================================================
# WHAT THE SCREEN IS TOLD while the speed can still resume by itself
# =========================================================================


async def test_the_screen_is_told_for_as_long_as_the_speed_can_resume_by_itself() -> None:
    """EX-3, from what the page already renders: the verdict's sentence, and ``latched``.

    The console shows a verdict's ``detail`` verbatim on its Seance and
    Securite pages, and ``verrouille: non`` beside it. For every tick a speed
    is held or lowered by a warning, that sentence says the warning lifts by
    itself and that the session may then speed up again; the moment the
    standstill is reached it is replaced by the latched ending, which says the
    opposite. The three fields a page needs to raise its own banner
    (``mode``, ``safety.latched``, ``safety_rank``) are pinned on the wire row.
    """
    rig = await _console_rig()
    lost = await rig.run(36.0, feed=False)
    blind = [snapshot for snapshot in lost if snapshot.safety is not None]
    assert len(blind) >= round(24.0 / 0.2), "the warning stood for less time than expected"
    assert min(_setpoints(blind)) >= MIN_RUN, "the arm stopped: the sentence would be untrue"
    warned = [SnapshotRow.of(snapshot) for snapshot in blind]
    for row in warned:
        assert row.mode == "seance"
        assert row.safety is not None
        assert row.safety.rule == RULE_HR_STALE
        assert row.safety.latched is False
        assert row.safety.detail.endswith(SELF_CLEARING)
    # Held first, then lowered: the rank the page colours its pill with.
    ranks = [row.safety_rank for row in warned]
    assert ranks == sorted(ranks)
    assert set(ranks) == {int(SafetyAction.FREEZE), int(SafetyAction.REDUCE)}
    assert {row.safety_action for row in warned} == {"freeze", "reduce"}

    await _until_standstill(rig, feed=False)
    stopped = SnapshotRow.of(rig.snapshots[-1])
    assert stopped.mode == "arret"
    assert stopped.safety is not None
    assert stopped.safety.rule == RULE_REDUCED_TO_STANDSTILL
    assert stopped.safety.latched is True
    assert stopped.safety_rank == int(SafetyAction.RAMP_DOWN)
    assert SELF_CLEARING not in stopped.safety.detail


async def test_the_screen_is_told_with_the_arm_stopped_too_while_it_can_still_start_alone() -> None:
    """EX-3 says "including with the arm stopped": the two cases the decision leaves open.

    A warning that found the arm already at zero ends nothing, so motion can
    still begin by itself when it lifts (here: the first motion of WARMUP, after
    a BASELINE that began without a heart rate). For as long as that warning
    stands, at a setpoint of zero, the sentence on the screen says so.
    """
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    blind = [s for s in await rig.run(45.0, feed=False) if s.safety is not None]
    assert len(blind) >= round(33.0 / 0.2)
    assert set(_setpoints(blind)) == {0}
    for row in (SnapshotRow.of(snapshot) for snapshot in blind):
        assert row.mode == "seance"
        assert row.safety is not None
        assert row.safety.latched is False
        assert row.safety.detail.endswith(SELF_CLEARING)
    assert {snapshot.safety_action for snapshot in blind} == {
        SafetyAction.FREEZE,
        SafetyAction.REDUCE,
    }
