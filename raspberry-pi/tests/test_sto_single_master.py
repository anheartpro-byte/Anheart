"""One master on the cable: the STO read goes through the runtime's own drive link.

The real composition (``build_panel`` with ``MOTOR_BACKEND=serial``) builds one
Modbus master and one ATV320 driver. The STO source reads register 5202
through that same driver instance, so through its lock and its request queue,
never through a second serial connection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src import local_panel
from src.clock import Clock, ManualClock
from src.local_panel import build_panel
from src.motor.atv320 import ATV320Drive, AuxiliaryRegisterReads, ModbusMaster, SerialSettings
from src.motor.drive import DriveError, DriveStatus, RegisterMap
from src.motor.observation import ExchangeLog
from src.motor.simulated import SimulatedDrive
from src.motor.sto import DriveInputStoSource, StoState
from src.result import Ok, Result
from src.training.runtime import IDLE_POLL_PERIOD
from src.units import RawRegister, RegisterAddress, Seconds
from tests.test_atv320 import FakeBus
from tests.test_failure_rig import BENCH_ENV, config_of
from tests.test_sto_poll_guard import STO_ENV

SERIAL_STO_ENV = {**STO_ENV, "MOTOR_BACKEND": "serial", "MOTOR_PORT": "/dev/ttyUSB-test"}


async def test_the_sto_source_reads_through_the_one_driver_and_master_of_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    registers = RegisterMap()
    bus = FakeBus(clock, registers)
    bus.registers.update(
        {
            registers.eta: 0x40,
            registers.lfrd: 0,
            registers.rfrd: 0,
            registers.lcr: 0,
            registers.logic_inputs: 1,
        }
    )
    built: list[SerialSettings] = []

    def one_master(
        settings: SerialSettings,
        _clock: Clock,
        *,
        open_device: object = None,
        exchange_log: ExchangeLog | None = None,
    ) -> ModbusMaster:
        del open_device, exchange_log
        built.append(settings)
        return bus

    callers: list[tuple[str, int]] = []
    original_open = ATV320Drive.open
    original_status = ATV320Drive.read_status
    original_auxiliary = ATV320Drive.read_auxiliary_register

    async def noted_open(drive: ATV320Drive) -> Result[None, DriveError]:
        callers.append(("open", id(drive)))
        return await original_open(drive)

    async def noted_status(drive: ATV320Drive) -> Result[DriveStatus, DriveError]:
        callers.append(("status", id(drive)))
        return await original_status(drive)

    async def noted_auxiliary(
        drive: ATV320Drive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        callers.append(("sto", id(drive)))
        return await original_auxiliary(drive, address)

    monkeypatch.setattr(local_panel, "serial_master", one_master)
    monkeypatch.setattr(ATV320Drive, "open", noted_open)
    monkeypatch.setattr(ATV320Drive, "read_status", noted_status)
    monkeypatch.setattr(ATV320Drive, "read_auxiliary_register", noted_auxiliary)
    panel = build_panel(config_of(SERIAL_STO_ENV), clock=clock, profiles_path=tmp_path / "p.json")
    drive = panel.drive.backend
    source = panel.sto
    try:
        assert isinstance(drive, ATV320Drive)
        assert isinstance(source, DriveInputStoSource)
        port = source.port
        assert isinstance(port, AuxiliaryRegisterReads)
        assert port.drive is drive
        assert len(built) == 1  # one master, built once
        polls = 6
        for _ in range(polls):
            clock.advance(IDLE_POLL_PERIOD)
            await panel.control_step()
            await source.poll()
        assert source.latest(clock.monotonic()).state is StoState.READY
        # The runtime's reads and the STO reads: one driver instance.
        assert {kind for kind, _ in callers} == {"open", "status", "sto"}
        assert {caller for _, caller in callers} == {id(drive)}
        assert [kind for kind, _ in callers].count("sto") == polls
        # Every 5202 transaction went over the one master, which was opened once.
        assert bus.reads().count(registers.logic_inputs) == polls
        assert bus.connect_calls == 1
        # Concurrent requests queue on the driver's one lock: never two on the wire.
        bus.real_read_block = Seconds(0.02)
        status, feedback = await asyncio.gather(
            drive.read_status(), port.read_register(registers.logic_inputs)
        )
        assert isinstance(status, Ok)
        assert feedback == Ok(RawRegister(1))
        assert bus.max_in_flight == 1
    finally:
        bus.real_read_block = Seconds(0.0)
        await panel.close()


def test_the_simulated_console_reads_its_one_simulated_drive(tmp_path: Path) -> None:
    """With the simulator the source reads the drive the runtime commands, not a copy."""
    panel = build_panel(
        config_of({**BENCH_ENV, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI3"}),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
    )
    source = panel.sto
    simulator = panel.drive.simulator
    assert isinstance(source, DriveInputStoSource)
    assert isinstance(simulator, SimulatedDrive)
    assert panel.drive.backend is simulator
    assert source.port is simulator
