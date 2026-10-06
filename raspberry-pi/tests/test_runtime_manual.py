"""The runtime's manual session (milestone M3): target, motion limits, stops, fault reset.

Reuses the fake drive and the rig of ``tests/test_runtime.py``, so the manual
path is judged by the same strict plant model as the programme path: only
writes feed ``ttO``, word 6 on a turning shaft freewheels, a fault ramps down.

What is pinned here, in the plan's words:

* the setpoint walks to the operator's target at the motion limits, and
  every change is bounded by the rate (plus the one carried rpm), in both
  directions, inside ``{0} union [min_run, ceiling]``;
* the verdict still decides first: FREEZE holds, REDUCE descends at the
  motion rate, QUICK_STOP zeroes, GO_SILENT is silent;
* after ANY stop the target is zero, and once the output stage is shown off
  the mode is back to REPOS: moving again takes a new start;
* BENCH runs with the heart-rate rules off; OCCUPIED needs a heart rate and
  never rises without one;
* a fault reset happens only on an explicit call, at rest, behind an
  acknowledgement, with the shaft shown stopped.
"""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise
from typing import Final, cast

import pytest

import src.training.runtime as runtime_module
from src.local_panel import describe_reset_refusal
from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveFault,
    DriveState,
    DriveStatus,
)
from src.result import Err, Ok, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS, motor_rate_limit
from src.training.runtime import (
    RULE_DRIVE_PRECOMMANDED,
    AlreadyStarted,
    EndReason,
    ManualEnding,
    NoFaultToReset,
    NoManualSession,
    NotAttested,
    PlanUnusable,
    ResetBehindVerdict,
    ResetForbidden,
    ResetUndelivered,
    ResetWhileCommanded,
    RuntimeState,
    SafetyStanding,
    ShaftStillTurning,
    TargetOutOfRange,
    TrainingRuntime,
    status_fault_report,
)
from src.training.safety import RULE_DRIVE_FAULT, RULE_HR_STALE, RULE_SESSION_STANDSTILL
from src.training.types import Occupancy, Phase, RunMode, SafetyAction, TelemetrySnapshot
from src.units import (
    Amperes,
    Bpm,
    MotorRpm,
    OutputRpm,
    OutputRpmPerSecond,
    RawRegister,
    Seconds,
    StatusWord,
    motor_to_output_rpm,
)
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    TICK,
    FakeState,
    Rig,
    _program,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
)
from tests.test_runtime import (
    _rig as _programme_rig,  # pyright: ignore[reportPrivateUsage]
)


def _rig(**kwargs: object) -> Rig:
    """The shared rig with the machine's OWN anti-nausea limits: what manual sessions walk.

    The shared builder now defaults to the accelerated rig's faster limits,
    because a programme's setpoint walks them too; every manual test here is
    about the real 0.25 output rpm/s and 0.03 g/s.
    """
    return _programme_rig(motion=DEFAULT_MOTION_LIMITS, **kwargs)  # type: ignore[arg-type]  # keyword pass-through


CEILING: Final[MotorRpm] = MotorRpm(300)
MIN_RUN: Final[int] = int(DEFAULT_MOTION_LIMITS.min_run)
RATE: Final[float] = motor_rate_limit(MotorRpm(0), DEFAULT_MOTION_LIMITS, GEOMETRY)


def _out(rpm: int) -> OutputRpm:
    """The output speed an operator types for a motor speed (exact through i)."""
    return motor_to_output_rpm(MotorRpm(rpm), GEOMETRY.ratio)


async def _manual(
    rig: Rig | None = None,
    *,
    occupancy: Occupancy = Occupancy.BENCH,
    ceiling: MotorRpm = CEILING,
    attest: bool = True,
) -> Rig:
    rig = _rig() if rig is None else rig
    if attest:
        assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    started = await rig.runtime.start_manual(occupancy, OPERATOR, ceiling)
    assert is_ok(started), started
    return rig


def _target(rig: Rig, rpm: int) -> None:
    assert rig.runtime.set_manual_target(_out(rpm)) == Ok(MotorRpm(rpm))


def _mode(rig: Rig) -> RunMode:
    return rig.runtime.mode


def _last_command(rig: Rig) -> ControlWord:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return rig.drive.commands[-1]


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _assert_ramp_conforms(setpoints: list[int]) -> None:
    """Domain, and the rate: each change bounded by the time since the previous one, +1 rpm."""
    last_change = 0
    for index, (before, after) in enumerate(pairwise(setpoints), start=1):
        assert after == 0 or after >= MIN_RUN, f"{after} rpm is outside the domain"
        if after == before:
            continue
        if {before, after} != {0, MIN_RUN}:
            window = min(float(index - last_change) * TICK, 1.0 + 1.0 / RATE)
            assert abs(after - before) <= RATE * window + 1 + 1e-9, (before, after)
        last_change = index


# =========================================================================
# Starting
# =========================================================================


async def test_a_manual_start_arms_the_drive_at_zero_and_moves_nothing() -> None:
    rig = await _manual()
    runtime = rig.runtime
    assert runtime.state is RuntimeState.RUNNING
    assert runtime.mode is RunMode.MANUEL
    assert runtime.phase is Phase.HOLD
    assert rig.drive.is_enabled()
    assert rig.drive.trace.index("lfrd:0") < rig.drive.trace.index("cmd:SHUTDOWN")
    manual = runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    assert manual.ceiling == CEILING
    snapshots = await rig.run(5.0)
    assert set(_setpoints(snapshots)) == {0}
    last = snapshots[-1]
    assert last.mode is RunMode.MANUEL
    assert last.manual is not None
    assert not last.manual.ramping
    assert last.manual.ramp_eta == 0.0
    assert last.manual.ceiling.motor_rpm == CEILING


@pytest.mark.parametrize(
    ("occupancy", "operator", "ceiling", "match"),
    [
        (Occupancy.BENCH, "  ", CEILING, "name the operator"),
        (Occupancy.BENCH, OPERATOR, MotorRpm(MIN_RUN - 1), "outside"),
        (Occupancy.BENCH, OPERATOR, MotorRpm(1381), "outside"),
        (Occupancy.OCCUPIED, OPERATOR, CEILING, "fresh, trustworthy heart rate"),
    ],
)
async def test_a_manual_start_is_refused_before_the_drive_is_touched(
    occupancy: Occupancy, operator: str, ceiling: MotorRpm, match: str
) -> None:
    rig = _rig()
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    refused = await rig.runtime.start_manual(occupancy, operator, ceiling)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, PlanUnusable)
    assert match in refused.error.detail
    assert rig.drive.trace == []


async def test_motion_limits_too_slow_to_ever_move_refuse_the_start() -> None:
    rig = _rig()
    crawling = replace(DEFAULT_MOTION_LIMITS, output_accel=OutputRpmPerSecond(0.01))
    runtime = TrainingRuntime(
        clock=rig.clock,
        drive=rig.drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=rig.runtime.supervisor.limits,
        motion=crawling,
    )
    assert is_ok(runtime.confirm_estop_wiring(OPERATOR))
    refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, PlanUnusable)
    assert "too slow" in refused.error.detail


async def test_the_common_start_gates_apply_to_a_manual_start() -> None:
    rig = _rig()
    unattested = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(unattested, Err)
    assert isinstance(unattested.error, NotAttested)
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    rig.runtime.request_estop("the button")
    standing = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(standing, Err)
    assert isinstance(standing.error, SafetyStanding)


async def test_a_second_start_while_running_is_refused() -> None:
    rig = await _manual()
    again = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(again, Err)
    assert again.error == AlreadyStarted(RuntimeState.RUNNING)
    programme = await rig.runtime.start(_program(), runtime_module.Subject("s", OPERATOR))
    assert isinstance(programme, Err)
    assert isinstance(programme.error, AlreadyStarted)


async def test_a_drive_that_cannot_be_armed_refuses_the_manual_start() -> None:
    rig = _rig()
    rig.drive.break_comms()
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    refused = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, runtime_module.DriveUnavailable)
    assert rig.runtime.manual is None
    assert rig.runtime.mode is RunMode.REPOS


async def test_a_faulted_drive_refuses_the_manual_start_at_arming() -> None:
    rig = _rig()
    rig.drive.inject_fault(DriveFault.OVERCURRENT)
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    refused = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, runtime_module.DriveInFault)


# =========================================================================
# The target and the ramp
# =========================================================================


async def test_zero_to_300_to_150_to_zero_follows_the_motion_limits() -> None:
    """The plan's bench sequence: 0 -> 300 -> 150 -> 0 motor rpm, each ramp conforming."""
    rig = await _manual()
    _target(rig, 300)
    climb = await rig.run(25.0)
    assert climb[0].manual is not None
    assert climb[0].manual.ramping
    assert climb[0].manual.ramp_eta is not None
    assert climb[0].manual.ramp_eta > 15.0
    assert _setpoints(climb)[-1] == 300
    # 245 rpm past the passage at 12.4 rpm/s: about twenty seconds, never much faster.
    reached = _setpoints(climb).index(300)
    assert reached * TICK >= 245 / RATE - 1.0
    assert rig.drive.shaft_rpm == pytest.approx(300, abs=2)
    assert climb[-1].manual is not None
    assert not climb[-1].manual.ramping

    _target(rig, 150)
    down = await rig.run(15.0)
    assert _setpoints(down)[-1] == 150
    _target(rig, 0)
    stop = await rig.run(15.0)
    assert _setpoints(stop)[-1] == 0
    _assert_ramp_conforms([0, *_setpoints(climb), *_setpoints(down), *_setpoints(stop)])
    # Target 0 is not a stop: the session still runs, output stage energised.
    assert rig.runtime.state is RuntimeState.RUNNING
    assert rig.drive.is_enabled()


@pytest.mark.parametrize(
    ("requested", "field"),
    [(_out(30), "gap"), (_out(301), "ceiling"), (-1.0, "negative"), (math.nan, "nan")],
)
async def test_a_target_outside_zero_or_min_run_to_ceiling_is_refused_not_clamped(
    requested: float, field: str
) -> None:
    rig = await _manual()
    _target(rig, 200)
    refused = rig.runtime.set_manual_target(OutputRpm(requested))
    assert isinstance(refused, Err), field
    assert isinstance(refused.error, TargetOutOfRange)
    assert refused.error.ceiling == CEILING
    assert rig.runtime.manual_target == 200


async def test_a_target_without_a_manual_session_is_refused() -> None:
    rig = _rig()
    refused = rig.runtime.set_manual_target(_out(100))
    assert refused == Err(NoManualSession(RuntimeState.IDLE))


async def test_the_programme_path_is_untouched_and_reads_as_a_session() -> None:
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    assert rig.runtime.mode is RunMode.SEANCE
    assert rig.runtime.manual is None
    refused = rig.runtime.set_manual_target(_out(100))
    assert refused == Err(NoManualSession(RuntimeState.RUNNING))
    snapshot = await rig.step()
    assert snapshot.manual is None


# =========================================================================
# Stopping: target 0, REPOS, no resumption
# =========================================================================


async def test_a_stop_zeroes_the_target_ramps_down_and_returns_to_repos() -> None:
    rig = await _manual()
    _target(rig, 300)
    await rig.run(25.0)
    rig.runtime.request_stop("operator pressed STOP")
    first = await rig.step()
    assert rig.runtime.manual_target == 0
    assert first.mode is RunMode.ARRET
    assert first.manual is not None
    assert first.manual.target.motor_rpm == 0
    refused = rig.runtime.set_manual_target(_out(300))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ManualEnding)
    down = await rig.run(40.0)
    _assert_ramp_conforms([300, *_setpoints([first, *down])])
    assert rig.runtime.state is RuntimeState.FINISHED
    assert rig.runtime.mode is RunMode.REPOS
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
    assert not rig.drive.is_enabled()
    assert rig.runtime.applied_rpm == 0
    # BENCH: no RECOVERY to sit through, nobody was on board.
    assert down[-1].mode is RunMode.REPOS


async def test_a_stop_requested_before_its_tick_already_refuses_a_target() -> None:
    rig = await _manual()
    rig.runtime.request_stop("stop")
    refused = rig.runtime.set_manual_target(_out(100))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ManualEnding)


async def test_a_finished_machine_writes_only_a_zero_and_can_be_armed_again() -> None:
    rig = await _manual()
    _target(rig, 120)
    await rig.run(10.0)
    rig.runtime.request_stop("stop")
    await rig.run(20.0)
    assert rig.runtime.state is RuntimeState.FINISHED
    rig.drive.trace.clear()
    await rig.run(2.0)
    # The zero keepalive and the reads: no command word, no speed.
    assert set(rig.drive.trace) <= {"lfrd:0", "lfrd:0:ack", "eta", "eta:ack"}
    # A new, explicit start: from scratch, at target 0.
    again = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert is_ok(again)
    assert rig.runtime.manual_target == 0
    assert rig.state() is RuntimeState.RUNNING
    assert rig.runtime.end_reason is None
    snapshots = await rig.run(3.0)
    assert set(_setpoints(snapshots)) == {0}


async def test_an_estop_during_a_ramp_zeroes_now_and_nothing_resumes() -> None:
    rig = await _manual()
    _target(rig, 300)
    await rig.run(8.0)
    assert rig.runtime.applied_rpm > MIN_RUN
    calls = rig.drive.emergency_calls
    rig.runtime.request_estop("web e-stop")
    assert rig.drive.emergency_calls == calls + 1
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime.manual_target == 0
    after = await rig.run(10.0)
    assert set(_setpoints(after)) == {0}
    assert all(snapshot.safety_action is SafetyAction.QUICK_STOP for snapshot in after)
    assert is_ok(rig.runtime.acknowledge(OPERATOR, estop_released=True))
    later = await rig.run(10.0)
    assert set(_setpoints(later)) == {0}
    assert rig.runtime.mode is RunMode.REPOS
    assert rig.runtime.manual_target == 0


async def test_a_comms_loss_goes_silent_and_nothing_ever_resumes() -> None:
    rig = await _manual()
    _target(rig, 200)
    await rig.run(10.0)
    rig.drive.break_comms()
    await rig.run(2.0)
    assert rig.runtime.silent
    assert rig.runtime.standing_action is SafetyAction.GO_SILENT
    rig.drive.trace.clear()
    await rig.run(5.0)
    assert rig.drive.trace == []
    refused = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, AlreadyStarted)
    reset = await rig.runtime.fault_reset()
    assert isinstance(reset, Err)
    assert isinstance(reset.error, ResetUndelivered)
    assert rig.drive.trace == []


async def test_the_manual_session_ends_itself_at_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", Seconds(20.0))
    rig = await _manual()
    _target(rig, 150)
    await rig.run(25.0)
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert rig.runtime.manual_target == 0
    await rig.run(30.0)
    assert rig.runtime.state is RuntimeState.FINISHED


# =========================================================================
# The verdict still decides
# =========================================================================


async def test_freeze_holds_and_the_ramp_resumes_without_a_banked_jump() -> None:
    rig = await _manual()
    _target(rig, 300)
    await rig.run(6.0)
    held = rig.runtime.applied_rpm
    assert MIN_RUN < held < 300
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    frozen = await rig.run(20.0)
    assert all(snapshot.safety_action is SafetyAction.FREEZE for snapshot in frozen)
    assert set(_setpoints(frozen)) == {held}
    assert is_ok(rig.runtime.acknowledge(OPERATOR))
    resumed = await rig.run(3.0)
    _assert_ramp_conforms([held, *_setpoints(resumed)])
    assert rig.runtime.applied_rpm > held


async def test_reduce_descends_at_the_motion_rate_whatever_the_target() -> None:
    rig = await _manual()
    _target(rig, 300)
    await rig.run(25.0)
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    down = await rig.run(30.0)
    _assert_ramp_conforms([300, *_setpoints(down)])
    assert _setpoints(down)[-1] == 0
    turning = [snapshot for snapshot in down if snapshot.setpoint.motor_rpm != 0]
    assert turning
    assert all(snapshot.safety_action is SafetyAction.REDUCE for snapshot in turning)
    # A REDUCE is not a stop while the arm turns: the operator's target is kept.
    assert all(
        snapshot.manual is not None and snapshot.manual.target.motor_rpm == 300
        for snapshot in turning
    )
    # The standstill it reaches is one (ANH-176, decisions of 2026-10-05 and
    # 2026-10-06): a stopped arm never restarts by itself, empty capsule
    # included. The session ends there, latched, and the target is zeroed as
    # after any stop. Before, the target stayed at 300 and the arm went back to
    # it as soon as the REDUCE was acknowledged.
    last = down[-1].safety
    assert last is not None
    assert (last.action, last.rule, last.latched) == (
        SafetyAction.RAMP_DOWN,
        RULE_SESSION_STANDSTILL,
        True,
    )
    assert rig.runtime.manual_target == 0
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT


async def test_a_ramp_down_verdict_ends_the_session_at_the_motion_rate() -> None:
    rig = await _manual()
    _target(rig, 300)
    await rig.run(25.0)
    rig.runtime.trip_from_thread("rig_ramp", SafetyAction.RAMP_DOWN, "under test")
    down = await rig.run(40.0)
    _assert_ramp_conforms([300, *_setpoints(down)])
    assert rig.runtime.manual_target == 0
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT
    assert _setpoints(down)[-1] == 0


# =========================================================================
# Occupancy
# =========================================================================


async def test_bench_runs_with_the_heart_rate_rules_off_and_every_other_on() -> None:
    """Nobody on board: no ECG at all for two minutes is not a reason to stop a bench test."""
    rig = await _manual()
    _target(rig, 200)
    snapshots = await rig.run(120.0, feed=False)
    assert all(snapshot.safety is None for snapshot in snapshots)
    assert rig.runtime.applied_rpm == 200
    # The attendant rule is still live.
    stale = await rig.run(65.0, feed=False, ping=False)
    assert stale[-1].safety is not None
    assert stale[-1].safety.rule == "attendant_absent"


async def _occupied() -> Rig:
    rig = _rig()
    rig.fed_bpm = Bpm(80)
    rig.feed(Bpm(80))
    started = await _manual(rig, occupancy=Occupancy.OCCUPIED)
    # Six more seconds of readings, so the heart rate's trend is known, as it
    # is on a console whose ECG has been running. With a single reading the
    # first target is refused since ANH-178: it used to be accepted and to wait.
    await started.run(6.0)
    return started


async def test_occupied_needs_a_heart_rate_and_never_rises_without_one() -> None:
    rig = await _occupied()
    _target(rig, 300)
    await rig.run(5.0)
    assert rig.runtime.applied_rpm > MIN_RUN
    # The heart rate disappears: the setpoint stops rising within the tracker's
    # freshness window, well before hr_stale's FREEZE at ten seconds.
    blind = await rig.run(9.0, feed=False)
    plateau = _setpoints(blind)[-1]
    assert plateau < 300
    assert _setpoints(blind)[-20:] == [plateau] * 20
    assert all(snapshot.safety is None for snapshot in blind[:40])
    later = await rig.run(3.0, feed=False)
    assert later[-1].safety is not None
    assert later[-1].safety.rule == RULE_HR_STALE
    assert rig.runtime.manual is not None
    assert rig.runtime.manual.cooldown > 0


async def test_occupied_keeps_its_recovery_after_a_stop() -> None:
    rig = await _occupied()
    rig.runtime.request_stop("stop")
    await rig.run(10.0)
    assert rig.runtime.phase is Phase.RECOVERY
    await rig.run(60.0)
    assert rig.runtime.state is RuntimeState.FINISHED


# =========================================================================
# Fault reset
# =========================================================================


async def _faulted_after_a_session() -> Rig:
    """A drive fault during a manual session: RAMP_DOWN latched, the shaft brought down."""
    rig = await _manual()
    _target(rig, 200)
    await rig.run(20.0)
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await rig.run(15.0)
    assert rig.runtime.phase is Phase.DONE
    assert rig.runtime.applied_rpm == 0
    return rig


async def test_a_fault_is_reset_on_request_then_acknowledged_then_the_console_is_at_rest() -> None:
    rig = await _faulted_after_a_session()
    runtime = rig.runtime
    # The run command could not be removed from a faulted drive: still ENDING.
    assert runtime.state is RuntimeState.ENDING
    assert runtime.mode is RunMode.ARRET
    # Any OTHER verdict must be acknowledged first.
    runtime.request_estop("web e-stop")
    behind = await runtime.fault_reset()
    assert isinstance(behind, Err)
    assert isinstance(behind.error, ResetBehindVerdict)
    assert behind.error.verdict.rule == "operator_estop"
    assert ControlWord.FAULT_RESET not in rig.drive.commands
    assert is_ok(runtime.acknowledge(OPERATOR, estop_released=True))
    await rig.run(0.2)
    # drive_fault itself stands, and re-latches while the drive shows FAULT:
    # it is acknowledged AFTER the reset, not before.
    assert runtime.standing is not None
    assert runtime.standing.rule == RULE_DRIVE_FAULT
    assert (await runtime.fault_reset()) == Ok(None)
    assert _last_command(rig) is ControlWord.FAULT_RESET
    assert rig.state() is RuntimeState.FINISHED
    await rig.run(0.2)
    assert _last_command(rig) is ControlWord.SHUTDOWN
    assert rig.drive.state is FakeState.READY
    assert _mode(rig) is RunMode.REPOS
    refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert is_ok(runtime.acknowledge(OPERATOR))
    assert is_ok(await runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING))


async def test_a_fault_reset_while_motion_is_commanded_is_refused() -> None:
    rig = await _manual()
    refused = await rig.runtime.fault_reset()
    assert refused == Err(ResetWhileCommanded(state=RuntimeState.RUNNING, phase=Phase.HOLD))


async def test_a_healthy_drive_has_no_fault_to_reset() -> None:
    rig = _rig()
    never_read = await rig.runtime.fault_reset()
    assert never_read == Err(NoFaultToReset(None))
    await rig.run(1.0)
    healthy = await rig.runtime.fault_reset()
    assert healthy == Err(NoFaultToReset(DriveState.SWITCH_ON_DISABLED))


async def test_a_fault_on_a_turning_shaft_is_not_reset() -> None:
    """You cannot reset your way out of a spinning mass.

    The idle console now latches ``drive_precommanded`` the moment it sees a
    shaft turning that nobody commanded (the failure matrix's idle-console
    defect), so this reaches the shaft check only after that verdict has been
    acknowledged by name and the stop has run its course - with a drive that
    lost torque and is coasting (a FAULT with no ramp-down reaction), which is
    what keeps it turning that long.
    """
    rig = _rig(state=FakeState.FAULT, rpm=MotorRpm(600))
    await rig.run(0.6)
    behind = await rig.runtime.fault_reset()
    assert isinstance(behind, Err)
    assert isinstance(behind.error, ResetWhileCommanded)
    await rig.run(6.0)
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_DRIVE_PRECOMMANDED
    assert is_ok(rig.runtime.acknowledge(OPERATOR))
    refused = await rig.runtime.fault_reset()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ShaftStillTurning)


async def test_an_idle_faulted_drive_is_reset_on_request_and_the_write_can_fail() -> None:
    rig = _rig()
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await rig.run(1.0)
    rig.drive.command_errors[ControlWord.FAULT_RESET] = CommTimeout(after=Seconds(0.1))
    undelivered = await rig.runtime.fault_reset()
    assert isinstance(undelivered, Err)
    assert isinstance(undelivered.error, ResetUndelivered)
    del rig.drive.command_errors[ControlWord.FAULT_RESET]
    rig.drive.command_errors[ControlWord.SHUTDOWN] = CommTimeout(after=Seconds(0.1))
    assert (await rig.runtime.fault_reset()) == Ok(None)
    await rig.run(1.0)
    assert rig.drive.state is FakeState.SWITCH_ON_DISABLED
    assert rig.runtime.mode is RunMode.REPOS


async def test_a_runtime_that_went_silent_between_the_two_words_never_sends_the_second() -> None:
    rig = _rig()
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await rig.run(1.0)
    assert (await rig.runtime.fault_reset()) == Ok(None)
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    rig.clock.advance(Seconds(0.1))
    await rig.runtime.tick(rig.now)
    assert rig.runtime.silent
    rig.drive.trace.clear()
    await rig.run(1.0)
    assert rig.drive.trace == []


# =========================================================================
# The raw LFT code
# =========================================================================


def test_the_raw_lft_code_survives_into_the_report() -> None:
    """LFT = 5 was seen after a comm loss on the bench: the number is what is shown."""
    status = DriveStatus(
        state=DriveState.FAULT,
        status_word=StatusWord(0x0038),
        setpoint_echo_rpm=MotorRpm(0),
        output_rpm=MotorRpm(0),
        current=Amperes(0.0),
        fault=DriveFault.UNKNOWN,
        fault_code=RawRegister(5),
    )
    report = status_fault_report(status)
    assert report is not None
    assert report.raw_code == 5
    no_code = status_fault_report(replace(status, fault_code=None))
    assert no_code is None


async def test_a_tick_too_short_to_pay_for_an_rpm_moves_nothing() -> None:
    """The allowance accrues across ticks: a 20 ms tick buys nothing and loses nothing."""
    rig = await _manual()
    await rig.run(1.0)
    _target(rig, 300)
    rig.clock.advance(Seconds(0.02))
    await rig.runtime.tick(rig.now)
    assert rig.runtime.applied_rpm == 0
    after = await rig.run(1.0)
    _assert_ramp_conforms([0, *_setpoints(after)])
    assert rig.runtime.applied_rpm > MIN_RUN


def test_a_forwarded_presence_never_moves_the_record_backwards() -> None:
    rig = _rig()
    rig.runtime.note_presence(rig.now)
    first = rig.now
    rig.clock.advance(Seconds(5.0))
    rig.runtime.note_presence(first)  # a late, older ping
    rig.runtime.note_presence(rig.now)
    assert rig.runtime.presence_ping() >= rig.now


def _bogus_state(_self: TrainingRuntime) -> RuntimeState:
    return cast("RuntimeState", "bogus")


async def test_a_runtime_state_that_is_not_one_fails_loudly_in_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _rig()
    monkeypatch.setattr(TrainingRuntime, "state", property(_bogus_state))
    with pytest.raises(AssertionError):
        _ = rig.runtime.mode


@pytest.mark.parametrize(
    "fault", [DriveFault.OVERCURRENT, DriveFault.MOTOR_SHORT_CIRCUIT, DriveFault.POWER_REMOVAL]
)
async def test_a_fault_that_is_not_resettable_is_never_reset_from_the_console(
    fault: DriveFault,
) -> None:
    """Short circuits, the drive's own hardware, STO: power off and inspect, never a click.

    The fault still latched drive_fault and stopped the machine exactly as any
    other; only the reset is refused, with the mnemonic the drive is showing.
    """
    assert not fault.resettable
    rig = _rig()
    rig.drive.inject_fault(fault)
    await rig.run(1.0)
    refused = await rig.runtime.fault_reset()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ResetForbidden)
    report = refused.error.report
    assert report is not None
    assert report.fault is fault
    assert ControlWord.FAULT_RESET not in rig.drive.commands
    text = describe_reset_refusal(refused.error)
    assert fault.mnemonic in text
    assert "non rearmable" in text


async def test_a_fault_whose_code_was_not_read_counts_as_not_resettable() -> None:
    """No LFT code: nothing says the fault is benign, so it is not reset."""
    rig = _rig(state=FakeState.FAULT)
    await rig.run(1.0)
    refused = await rig.runtime.fault_reset()
    assert isinstance(refused, Err)
    assert refused.error == ResetForbidden(None)
    assert "inconnu" in describe_reset_refusal(refused.error)
