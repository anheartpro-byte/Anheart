import asyncio
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.motor.drive import DriveState, RegisterMap
from src.motor.simulated import SimState
from src.result import Err, Ok
from src.training.runtime import IDLE_POLL_PERIOD
from src.training.types import Occupancy
from src.units import MotorRpm
from tests import test_atv320
from tests.test_atv320 import FakeBus, build_drive, parameter_exception
from tests.test_automatic_recovery import runtime_for
from tests.test_failure_rig import attest, make_rig
from tests.test_initial_inspection_cancellation import HeldInspection
from tests.test_native_acquisition_evidence import HeldRead

clock = test_atv320.clock
bus = test_atv320.bus
make_bus = test_atv320.make_bus


async def test_cancelled_unproven_native_acquisition_terminates_without_blind_write(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    assert isinstance(runtime.confirm_estop_wiring("synthetic operator"), Ok)
    held = HeldRead()
    bus.entered_read = held
    bus.sticky_reads = parameter_exception()
    task = asyncio.create_task(
        runtime.start_manual(Occupancy.BENCH, "synthetic operator", MotorRpm(276))
    )
    try:
        assert await asyncio.to_thread(held.wait, 2)
        task.cancel()
        held.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert runtime.silent
        assert runtime.output_enabled
        assert runtime.snapshot().drive_state is DriveState.COMM_LOST
        assert not drive.acquisition_evidence.address_proven
        assert bus.writes() == []
        before = len(bus.log)
        report = await runtime.shutdown("synthetic cancellation")
        assert report.silent
        assert not report.output_disabled
        assert len(bus.log) == before
    finally:
        held.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("requested_start", [False, True])
async def test_cancelled_wrapper_error_cannot_erase_delegate_address_proof(
    tmp_path: Path, requested_start: bool
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.hold_open = True
    held.fail_open = True
    async with rig.http() as client:
        if requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(rig.panel.control_step())
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            assert held.acquisition_evidence.address_proven
            task.cancel()
            held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await rig.panel.close()
            assert held.writes > 0
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            before = (held.reads, held.writes)
            await rig.panel.control_step()
            assert (held.reads, held.writes) == before
        finally:
            held.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


async def test_failed_manual_start_returns_ownership_to_automatic_idle_recovery(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    assert isinstance(runtime.confirm_estop_wiring("synthetic operator"), Ok)
    bus.script_reads.extend((test_atv320.Behave.NORMALLY, parameter_exception()))
    outcome = await runtime.start_manual(Occupancy.BENCH, "synthetic operator", MotorRpm(276))
    assert isinstance(outcome, Err)
    before = len(bus.log)
    await runtime.tick(clock.monotonic())
    assert len(bus.log) == before
    clock.advance(IDLE_POLL_PERIOD)
    snapshot = await runtime.tick(clock.monotonic())
    assert runtime.idle_link.reads == 1
    assert snapshot.drive_state is DriveState.SWITCH_ON_DISABLED
    assert not runtime.output_enabled
    assert runtime.manual is None
    assert bus.writes() == []


async def test_failed_reacquisition_releases_previous_refused_start_service_ownership(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    assert isinstance(runtime.confirm_estop_wiring("synthetic operator"), Ok)
    bus.registers[RegisterMap().eta] = test_atv320.ETA_FAULT
    assert isinstance(
        await runtime.start_manual(Occupancy.BENCH, "synthetic operator", MotorRpm(276)), Err
    )
    bus.script_reads.append(parameter_exception())
    assert isinstance(
        await runtime.start_manual(Occupancy.BENCH, "synthetic operator", MotorRpm(276)), Err
    )
    before = len(bus.log)
    await runtime.tick(clock.monotonic())
    assert len(bus.log) == before
    assert bus.writes() == []
    assert runtime.snapshot().drive_state is DriveState.COMM_LOST
    assert runtime.snapshot().drive_status_age is not None
    clock.advance(IDLE_POLL_PERIOD)
    await runtime.tick(clock.monotonic())
    assert runtime.idle_link.reads == 1
    assert bus.writes() == []
