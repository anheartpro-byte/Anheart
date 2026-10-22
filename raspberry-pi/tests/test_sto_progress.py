from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.local_panel import DriveSide, LocalPanel, build_panel
from src.motor import sto as sto_module
from src.motor.atv320 import ATV320Drive
from src.motor.drive import DriveError, DriveFault, RegisterMap
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import DriveInputStoSource, StoState
from src.result import Err, Ok, Result
from src.training.preflight import StoNotReady
from src.training.runtime import IDLE_POLL_PERIOD
from src.training.types import Occupancy
from src.units import MotorRpm, OutputRpm, RawRegister, RegisterAddress, Seconds
from tests.test_atv320 import Behave, FakeBus, build_drive, connection_exception
from tests.test_failure_rig import OPERATOR, config_of, make_rig
from tests.test_local_panel import FakeWeb
from tests.test_sto_poll_guard import STO_ENV


async def test_live_sto_poller_cannot_feed_watchdog_when_only_control_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: asyncio.Queue[None] = asyncio.Queue()
    original = DriveInputStoSource.poll

    async def observed_poll(source: DriveInputStoSource) -> None:
        await original(source)
        attempts.put_nowait(None)

    monkeypatch.setattr(DriveInputStoSource, "poll", observed_poll)
    monkeypatch.setattr(sto_module, "STO_POLL_PERIOD", Seconds(0.001))
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    await panel.control_cycle()
    await source.poll()
    assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
    )
    assert isinstance(panel.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(100):
        rig.clock.advance(Seconds(0.2))
        await panel.control_cycle()
        await source.poll()
    assert rig.simulator.commanded_setpoint > 0
    assert rig.simulator.sim_state.name == "OPERATION_ENABLED"
    # Leave one completed tick available, then suspend only the control task.
    await panel.control_cycle()
    while not attempts.empty():
        attempts.get_nowait()
    entered, stop = asyncio.Event(), asyncio.Event()

    async def stalled(_panel: LocalPanel) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(LocalPanel, "control_cycle", stalled)
    before = rig.simulator.acquisition_evidence.possible_frames
    runner = asyncio.create_task(panel.run(stop, FakeWeb()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(10):
            rig.clock.advance(Seconds(0.5))
            # Observe actual poll attempts; the poller stays alive throughout.
            await asyncio.wait_for(attempts.get(), 2)
            await asyncio.wait_for(attempts.get(), 2)
            rig.simulator.advance(rig.clock.monotonic())
        rig.simulator.advance(rig.clock.monotonic())
        frames = rig.simulator.acquisition_evidence.possible_frames - before
        logging.getLogger(__name__).info(
            "stalled control: additional_frames=%d, state=%s", frames, rig.simulator.sim_state.name
        )
        assert not runner.done()
        assert panel.watchdog.stalled(rig.clock.monotonic())
        assert not panel.runtime.silent
        assert frames <= 1
        assert rig.simulator.sim_state is SimState.FAULT
        status = await rig.simulator.read_status()
        assert isinstance(status, Ok)
        assert status.value.fault is DriveFault.MODBUS_COMM_LOSS
        assert status.value.output_rpm == 0
    finally:
        stop.set()
        await asyncio.wait_for(runner, 3)


async def test_inflight_read_and_last_tick_allow_two_frames_then_watchdog_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    await panel.control_cycle()
    await source.poll()
    assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
    )
    assert isinstance(panel.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(100):
        rig.clock.advance(Seconds(0.2))
        await panel.control_cycle()
        await source.poll()
    assert rig.simulator.commanded_setpoint > 0
    await panel.control_cycle()
    entered, release = asyncio.Event(), asyncio.Event()
    attempts: asyncio.Queue[None] = asyncio.Queue()
    original_read = SimulatedDrive.read_register
    original_poll = DriveInputStoSource.poll
    calls = 0

    async def inflight(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original_read(drive, address)

    async def observed_poll(self: DriveInputStoSource) -> None:
        await original_poll(self)
        attempts.put_nowait(None)

    monkeypatch.setattr(SimulatedDrive, "read_register", inflight)
    monkeypatch.setattr(DriveInputStoSource, "poll", observed_poll)
    monkeypatch.setattr(sto_module, "STO_POLL_PERIOD", Seconds(0.001))
    poller = asyncio.create_task(source.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        rig.clock.advance(Seconds(0.2))
        await panel.control_cycle()
        before = rig.simulator.acquisition_evidence.possible_frames
        release.set()
        for _ in range(3):
            await asyncio.wait_for(attempts.get(), 2)
        assert calls == 2
        assert rig.simulator.acquisition_evidence.possible_frames == before + 2
        for _ in range(10):
            rig.clock.advance(Seconds(0.5))
            await asyncio.wait_for(attempts.get(), 2)
            rig.simulator.advance(rig.clock.monotonic())
        assert calls == 2
        assert rig.simulator.acquisition_evidence.possible_frames == before + 2
        assert not panel.runtime.silent
        assert rig.simulator.sim_state is SimState.FAULT
        status = await rig.simulator.read_status()
        assert isinstance(status, Ok)
        assert status.value.fault is DriveFault.MODBUS_COMM_LOSS
        assert status.value.output_rpm == 0
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        await panel.close()


@pytest.mark.parametrize("interleave", [False, True])
async def test_unacquired_idle_outage_cannot_become_traffic_fed_by_sto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interleave: bool
) -> None:
    clock, registers = ManualClock(), RegisterMap()
    bus = FakeBus(clock, registers)
    bus.registers.update(
        {registers.eta: 0x40, registers.lfrd: 0, registers.rfrd: 0, registers.lcr: 0}
    )
    bus.port_opens = False
    bus.sticky_reads = connection_exception()
    drive = build_drive(clock, bus)
    panel = build_panel(
        config_of(STO_ENV),
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        drive=DriveSide(backend=drive, simulator=None, release=lambda: None),
    )
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    original_open = ATV320Drive.open

    async def interleaved_open(self: ATV320Drive) -> Result[None, DriveError]:
        await source.poll()
        return await original_open(self)

    if interleave:
        monkeypatch.setattr(ATV320Drive, "open", interleaved_open)
    try:
        await source.poll()
        assert bus.connect_calls == 0
        assert not bus.log
        assert drive.acquisition_evidence.possible_frames == 0
        for _ in range(8):
            clock.advance(IDLE_POLL_PERIOD)
            await panel.control_step()
            await source.poll()
        assert (panel.runtime.drive_link_acquired, panel.runtime.silent) == (False, False)
        assert not panel.runtime.silent
        assert panel.runtime.standing is None
        assert drive.acquisition_evidence.possible_frames == 0
        assert not bus.log
        assert source.latest(clock.monotonic()).state is StoState.UNKNOWN
        assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
        refused = await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(refused, Err)
        assert isinstance(refused.error, StoNotReady)
        bus.port_opens = True
        bus.sticky_reads = Behave.NORMALLY
        bus.registers[registers.logic_inputs] = 1
        clock.advance(IDLE_POLL_PERIOD)
        await panel.control_step()
        assert (panel.runtime.drive_link_acquired, panel.runtime.silent) == (True, False)
        await source.poll()
        assert source.latest(clock.monotonic()).state is StoState.READY
        assert sum(tx.address == registers.logic_inputs for tx in bus.log) == 1
        before = drive.acquisition_evidence.possible_frames
        await source.poll()
        assert drive.acquisition_evidence.possible_frames == before
        bus.port_opens = False
        bus.sticky_reads = connection_exception()
        clock.advance(IDLE_POLL_PERIOD)
        await panel.control_step()
        assert not panel.runtime.drive_link_acquired
        before = drive.acquisition_evidence.possible_frames
        await source.poll()
        assert drive.acquisition_evidence.possible_frames == before
    finally:
        await panel.close()
