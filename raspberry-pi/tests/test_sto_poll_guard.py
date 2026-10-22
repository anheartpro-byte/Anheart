from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.motor import sto as sto_module
from src.motor.drive import DriveError, DriveFault
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import STO_STALE_AFTER, DriveInputStoSource, StoState
from src.result import Ok, Result
from src.training.types import Occupancy, SafetyAction
from src.units import MotorRpm, OutputRpm, RawRegister, RegisterAddress, Seconds
from tests.test_failure_rig import BENCH_ENV, OPERATOR, make_rig

STO_ENV = {**BENCH_ENV, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI1"}


@pytest.mark.parametrize("release", ["panel_close", "runtime_shutdown"])
async def test_polling_runs_at_idle_and_armed_but_never_after_release(
    tmp_path: Path, release: str
) -> None:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    source = rig.panel.sto
    assert isinstance(source, DriveInputStoSource)
    assert isinstance(await rig.simulator.open(), Ok)
    before = rig.simulator.acquisition_evidence.possible_frames
    await source.poll()
    assert rig.simulator.acquisition_evidence.possible_frames == before + 1
    assert source.latest(rig.clock.monotonic()).state is StoState.READY
    assert isinstance(rig.panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    if release == "runtime_shutdown":
        assert isinstance(
            await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
        )
    rig.clock.advance(Seconds(0.2))
    await source.poll()
    assert source.latest(rig.clock.monotonic()).last_success_at == rig.clock.monotonic()
    if release == "runtime_shutdown":
        await rig.panel.runtime.shutdown("operator")
        assert not rig.panel.runtime.drive_reads_permitted
    else:
        await rig.panel.close()
    released = rig.simulator.acquisition_evidence.possible_frames
    rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
    await source.poll()
    await source.poll()
    assert rig.simulator.acquisition_evidence.possible_frames == released
    assert source.latest(rig.clock.monotonic()).state is StoState.UNKNOWN
    if release == "runtime_shutdown":
        await rig.panel.close()


async def test_one_inflight_sto_read_then_no_frame_after_silence_and_watchdog_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    source = rig.panel.sto
    assert isinstance(source, DriveInputStoSource)
    assert isinstance(await rig.simulator.open(), Ok)
    await source.poll()
    runtime = rig.panel.runtime
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(50):
        await source.poll()
        rig.clock.advance(Seconds(0.2))
        await rig.panel.control_step()
    assert rig.simulator.commanded_setpoint > 0
    entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = SimulatedDrive.read_register

    async def inflight(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        entered.set()
        await release.wait()
        result = await original(drive, address)
        delivered.set()
        return result

    monkeypatch.setattr(SimulatedDrive, "read_register", inflight)
    monkeypatch.setattr(sto_module, "STO_POLL_PERIOD", Seconds(0.001))
    poller = asyncio.create_task(source.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime.trip_from_thread("test_silence", SafetyAction.GO_SILENT, "watchdog stop")
        rig.clock.advance(Seconds(0.2))
        await rig.panel.control_step()
        assert runtime.silent
        verdict = runtime.standing
        assert verdict is not None
        assert verdict.rule == "test_silence"
        before = rig.simulator.acquisition_evidence.possible_frames
        release.set()
        await asyncio.wait_for(delivered.wait(), 2)
        await asyncio.sleep(0.01)
        assert rig.simulator.acquisition_evidence.possible_frames == before + 1
        rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
        await source.poll()
        assert source.latest(rig.clock.monotonic()).state is StoState.UNKNOWN
        caplog.clear()
        await rig.panel.control_step()
        assert runtime.standing == verdict
        assert "sto_unreadable" not in caplog.text
        rig.clock.advance(Seconds(4))
        rig.simulator.advance(rig.clock.monotonic())
        await asyncio.sleep(0.01)
        assert rig.simulator.acquisition_evidence.possible_frames == before + 1
        assert rig.simulator.sim_state in (SimState.FAULT_REACTION_RAMP_STOP, SimState.FAULT)
        status = await rig.simulator.read_status()
        assert isinstance(status, Ok)
        assert status.value.fault is DriveFault.MODBUS_COMM_LOSS
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        await rig.panel.close()
