from __future__ import annotations

from threading import Thread
from typing import Literal, assert_never

import pytest

from src.result import is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import RuntimeState
from src.training.types import Occupancy, SafetyAction
from src.units import Bpm, MotorRpm, Seconds, motor_to_output_rpm
from tests.test_runtime import (
    GEOMETRY,
    OPERATOR,
    Rig,
    _profile,  # pyright: ignore[reportPrivateUsage]  # existing shared rig builders
    _rig,  # pyright: ignore[reportPrivateUsage]
    _running_rig,  # pyright: ignore[reportPrivateUsage]
)

Mode = Literal["auto", "bench", "occupied"]
Source = Literal["hr_stale", "attendant_absent", "loop_stall", "thread"]


async def moving(mode: Mode) -> Rig:
    occupancy: Occupancy
    match mode:
        case "auto":
            return await _running_rig(profile=_profile(total_duration_s=Seconds(400)))
        case "bench":
            occupancy = Occupancy.BENCH
        case "occupied":
            occupancy = Occupancy.OCCUPIED
        case _ as unreachable:
            assert_never(unreachable)
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    rig.feed(Bpm(82))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(occupancy, OPERATOR, MotorRpm(300)))
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(300), GEOMETRY.ratio)))
    await rig.run(8)
    assert rig.runtime.applied_rpm > 100
    return rig


async def freeze(rig: Rig, source: Source) -> None:
    match source:
        case "hr_stale":
            await rig.run(12, feed=False)
        case "attendant_absent":
            await rig.run(62, ping=False)
        case "loop_stall":
            rig.clock.advance(Seconds(0.7))
            await rig.step()
        case "thread":
            thread = Thread(
                target=rig.runtime.trip_from_thread,
                args=("native_freeze", SafetyAction.FREEZE, "test"),
            )
            thread.start()
            thread.join()
            await rig.step()
        case _ as unreachable:
            assert_never(unreachable)
    assert rig.runtime.standing_action is SafetyAction.FREEZE


@pytest.mark.parametrize("mode", ["auto", "bench", "occupied"])
@pytest.mark.parametrize("source", ["attendant_absent", "loop_stall", "thread"])
@pytest.mark.parametrize("reason", ["operator", "site"])
async def test_stop_descends_next_tick_under_freeze(
    mode: Mode, source: Source, reason: str
) -> None:
    rig = await moving(mode)
    await freeze(rig, source)
    before = rig.runtime.applied_rpm
    rig.runtime.request_stop(reason)
    first = await rig.step(feed=False, ping=False)
    assert 0 < first.setpoint.motor_rpm < before
    after = await rig.run(15, feed=False, ping=False)
    assert after[-1].setpoint.motor_rpm == 0


@pytest.mark.parametrize("mode", ["auto", "occupied"])
async def test_stop_descends_next_tick_under_stale_hr(mode: Mode) -> None:
    rig = await moving(mode)
    await freeze(rig, "hr_stale")
    before = rig.runtime.applied_rpm
    rig.runtime.request_stop("operator")
    first = await rig.step(feed=False)
    assert 0 < first.setpoint.motor_rpm < before
    after = await rig.run(15, feed=False)
    assert after[-1].setpoint.motor_rpm == 0


@pytest.mark.parametrize("mode", ["bench", "occupied"])
@pytest.mark.parametrize("source", ["attendant_absent", "loop_stall", "thread"])
async def test_manual_zero_descends_without_ending(mode: Mode, source: Source) -> None:
    rig = await moving(mode)
    await freeze(rig, source)
    before = rig.runtime.applied_rpm
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(0), GEOMETRY.ratio)))
    first = await rig.step(feed=False, ping=False)
    assert 0 < first.setpoint.motor_rpm < before
    after = await rig.run(15, feed=False, ping=False)
    assert after[-1].setpoint.motor_rpm == 0
    assert rig.runtime.state is RuntimeState.RUNNING


@pytest.mark.parametrize("mode", ["auto", "bench", "occupied"])
async def test_freeze_appearing_during_stop_keeps_descending(mode: Mode) -> None:
    rig = await moving(mode)
    rig.runtime.request_stop("operator")
    await rig.run(0.6)
    before = rig.runtime.applied_rpm
    rig.runtime.trip_from_thread("native_freeze", SafetyAction.FREEZE, "test")
    first = await rig.step()
    assert first.setpoint.motor_rpm < before


async def test_frozen_manual_target_reports_no_active_ramp_or_eta() -> None:
    rig = await moving("bench")
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)))
    await freeze(rig, "thread")
    before = rig.runtime.applied_rpm
    snapshots = await rig.run(3)
    assert {snapshot.setpoint.motor_rpm for snapshot in snapshots} == {before}
    manual = snapshots[-1].manual
    assert manual is not None
    assert not manual.ramping
    assert manual.ramp_eta is None


async def test_manual_zero_descends_under_stale_hr() -> None:
    rig = await moving("occupied")
    await freeze(rig, "hr_stale")
    before = rig.runtime.applied_rpm
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(0), GEOMETRY.ratio)))
    first = await rig.step(feed=False)
    assert 0 < first.setpoint.motor_rpm < before
    after = await rig.run(15, feed=False)
    assert after[-1].setpoint.motor_rpm == 0


async def test_stale_hr_appearing_during_manual_stop_cannot_pause_descent() -> None:
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(82)
    rig.feed(Bpm(82))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MotorRpm(300)))
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(300), GEOMETRY.ratio)))
    await rig.run(25)
    rig.runtime.request_stop("operator")
    snapshots = await rig.run(15, feed=False)
    frozen = [snapshot for snapshot in snapshots if snapshot.safety_action is SafetyAction.FREEZE]
    assert len(frozen) > 2
    assert frozen[-1].setpoint.motor_rpm < frozen[0].setpoint.motor_rpm
    assert frozen[-1].manual is not None
    assert frozen[-1].manual.ramping
    assert frozen[-1].manual.ramp_eta is not None
    after = await rig.run(12, feed=False)
    assert after[-1].setpoint.motor_rpm == 0


@pytest.mark.parametrize("mode", ["auto", "bench", "occupied"])
@pytest.mark.parametrize("source", ["attendant_absent", "loop_stall", "thread"])
async def test_freeze_without_stop_holds_the_setpoint(mode: Mode, source: Source) -> None:
    rig = await moving(mode)
    await freeze(rig, source)
    before = rig.runtime.applied_rpm
    snapshots = await rig.run(2, feed=False, ping=False)
    assert {snapshot.setpoint.motor_rpm for snapshot in snapshots} == {before}


@pytest.mark.parametrize("mode", ["auto", "bench", "occupied"])
@pytest.mark.parametrize(
    "action",
    [SafetyAction.REDUCE, SafetyAction.RAMP_DOWN, SafetyAction.QUICK_STOP, SafetyAction.GO_SILENT],
)
async def test_stronger_verdict_still_dominates_stop(mode: Mode, action: SafetyAction) -> None:
    rig = await moving(mode)
    await freeze(rig, "thread")
    before = rig.runtime.applied_rpm
    rig.runtime.request_stop("operator")
    rig.runtime.trip_from_thread("stronger", action, "test")
    await rig.run(0.6)
    assert rig.runtime.standing_action is action
    match action:
        case SafetyAction.REDUCE | SafetyAction.RAMP_DOWN:
            assert 0 < rig.runtime.applied_rpm < before
        case SafetyAction.QUICK_STOP:
            assert rig.runtime.applied_rpm == 0
        case SafetyAction.GO_SILENT:
            rig.drive.trace.clear()
            await rig.run(1)
            assert rig.drive.trace == []
        case SafetyAction.NONE | SafetyAction.FREEZE:
            pytest.fail("the stronger-verdict matrix requires a stronger action")
        case _ as unreachable:
            assert_never(unreachable)
