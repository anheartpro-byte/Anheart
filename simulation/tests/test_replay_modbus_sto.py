"""A native console that reads the STO relay feedback records and replays as one that does not.

The console reads register 5202 through the runtime's own ATV320 driver,
between control ticks. Those reads stay out of the exchange log that feeds the
session record (``ATV320Drive.read_auxiliary_register``): the record holds the
control path's transactions only, so the native replay, whose runtime reads a
ready simulated chain and never asks for 5202, consumes it without a divergence.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import override

import pytest
from tests.sto_support import ready_sto

from simulation.modbus import InlineExecutor, SimulatedMaster
from simulation.replay import replay
from simulation.tests.test_record_console_parity import ENV, OPERATOR, TICK, console_inline
from src import local_panel
from src.clock import Clock, ManualClock
from src.local_config import LocalConfig, load_local_config
from src.local_panel import DriveSide, LocalPanel, build_panel
from src.motor.atv320 import ATV320Drive, AuxiliaryRegisterReads, SerialSettings
from src.motor.drive import DriveBackend, RegisterMap
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import DriveInputStoSource, SimulatedStoChain, StoSource, StoState
from src.record.journal import Journal
from src.record.reader import Recording, read
from src.record.retention import records
from src.result import Err, Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, RawRegister, UnixMillis

NATIVE = {**ENV, "MOTOR_BACKEND": "serial", "MOTOR_PORT": "simulated", "MOTOR_REG_OFFSET": "-1"}
READING = {"STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI1"}


class CountingMaster(SimulatedMaster):
    """The simulation's master, noting every register read that reaches it."""

    def __init__(self, model: SimulatedDrive, clock: ManualClock, registers: RegisterMap) -> None:
        super().__init__(model, clock, registers)
        # Appended by the one driver that owns this master.
        self.reads: list[int] = []

    @override
    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        self.reads.append(address)
        return super().read_holding_registers(address, count, slave)


def ready_source(
    _config: LocalConfig, _clock: Clock, _drive: DriveBackend, _chain: SimulatedStoChain | None
) -> StoSource:
    return ready_sto()


async def ticks(panel: LocalPanel, journal: Journal, clock: ManualClock, seconds: float) -> None:
    """``console_ticks``, plus the console's STO poll just after each completed tick."""
    source = panel.sto
    for index in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        if index % 5 == 0:
            await panel.sensor_step()
        await panel.control_step()
        if isinstance(source, DriveInputStoSource):
            await source.poll()
        journal.drain()


@dataclass(frozen=True, slots=True)
class Session:
    record: Recording
    folder: Path
    config: Path
    master: CountingMaster


async def console(root: Path, *, reads_sto: bool) -> Session:
    """One manual session of the native console, recorded and closed."""
    loaded = load_local_config({**NATIVE, **READING} if reads_sto else NATIVE)
    assert isinstance(loaded, Ok), loaded
    config = loaded.value
    link = config.drive_link
    assert link is not None
    clock = ManualClock(Monotonic(10), UnixMillis(1_700_000_000_000))
    model = SimulatedDrive(
        clock, SimulatedDriveConfig(reads_reset_watchdog=True), sto_chain=SimulatedStoChain()
    )
    master = CountingMaster(model, clock, link.registers)
    drive = ATV320Drive(
        clock,
        master,
        link.settings,
        link.registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    journal = Journal(root / "records", clock)
    with pytest.MonkeyPatch.context() as patch:
        if not reads_sto:
            # The same session with no STO read at all: a ready chain read from memory.
            patch.setattr(local_panel, "build_sto", ready_source)
        panel = build_panel(
            config,
            clock=clock,
            profiles_path=root / "profiles.json",
            treat=console_inline,
            journal=journal,
            drive=DriveSide(backend=drive, simulator=None, release=master.close),
        )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    await ticks(panel, journal, clock, 12)
    assert panel.sto.latest(clock.monotonic()).state is StoState.READY
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await ticks(panel, journal, clock, 1)
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(3), operator=OPERATOR), Ok
    )
    await ticks(panel, journal, clock, 7)
    assert panel.runtime.state is RuntimeState.RUNNING
    assert isinstance(panel.surface.submit_end(operator=OPERATOR, reason="done"), Ok)
    await ticks(panel, journal, clock, 60)
    await panel.close()
    assert model.commanded_setpoint == 0
    found = records(journal.root)
    assert len(found) == 1
    result = read(found[0])
    assert isinstance(result, Ok), result
    assert result.value.warnings == ()
    document = journal.root / "config" / (result.value.manifest.config_hash + ".json")
    return Session(result.value, found[0], document, master)


@pytest.fixture(scope="module")
def sessions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Session, Session]:
    reading = asyncio.run(console(tmp_path_factory.mktemp("reading"), reads_sto=True))
    silent = asyncio.run(console(tmp_path_factory.mktemp("silent"), reads_sto=False))
    return reading, silent


LOGIC_INPUTS = RegisterMap(offset=-1).logic_inputs


def test_a_session_that_reads_the_sto_feedback_replays_without_divergence(
    sessions: tuple[Session, Session],
) -> None:
    reading, _ = sessions
    # The STO reads went over the native link, through the runtime's own driver.
    assert reading.master.reads.count(LOGIC_INPUTS) > 100
    assert any(frame.kind == "modbus_write" for frame in reading.record.frames)
    assert any(row.measured_motor_rpm > 0 for row in reading.record.rows)
    assert not any(frame.register == LOGIC_INPUTS for frame in reading.record.frames)
    one = replay(reading.folder, reading.config)
    assert isinstance(one, Ok), one
    assert one.value.matches, one.value.to_text()
    assert one.value.divergence is None
    assert one.value.comparison.deviating_ticks == 0
    assert one.value.comparison.tolerated_rpm == (0, 0)
    assert one.value.comparison.shifted_transitions == (0, 0)


def test_reading_the_sto_feedback_records_exactly_the_exchanges_of_a_session_without_it(
    sessions: tuple[Session, Session],
) -> None:
    reading, silent = sessions
    assert LOGIC_INPUTS not in silent.master.reads
    assert reading.record.frames
    assert reading.record.frames == silent.record.frames


async def test_the_simulated_master_answers_the_relay_feedback_from_its_model() -> None:
    clock = ManualClock()
    registers = RegisterMap(offset=-1)
    chain = SimulatedStoChain()
    model = SimulatedDrive(clock, SimulatedDriveConfig(), sto_chain=chain)
    master = SimulatedMaster(model, clock, registers)
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="simulated"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    assert isinstance(await driver.open(), Ok)
    reads = AuxiliaryRegisterReads(driver)
    assert await reads.read_register(registers.logic_inputs) == Ok(RawRegister(1))
    chain.open(clock.monotonic())
    assert await reads.read_register(registers.logic_inputs) == Ok(RawRegister(0))
    master.close()  # the model's link is closed: it refuses, as for any register
    refused = await reads.read_register(registers.logic_inputs)
    assert isinstance(refused, Err)
    await driver.close()
