"""The bench tool's coast-down hold on a line that loses status reads (ANH-92, review M1).

A noisy RS-485 pair fails the five transactions of a status read more often than the
two of a speed write: the slider's LFRD lands, read back, while the poll after it fails,
and each landed write resets the driver's count of failures, so the tool stays armed.
What the tool itself wrote, Activer and each LFRD that landed, is what bounds the hold
then. One pass of the bus thread at a time (:meth:`BusWorker.cycle_once`), on a clock
the test moves.
"""

from __future__ import annotations

from typing import Final, cast

import pytest

import scripts.bench_console as bench
from scripts.drive_link import BuiltDrive
from src.clock import Clock, ManualClock
from src.local_config import DriveLink, load_local_config
from src.motor.atv320 import ATV320Drive, ModbusMaster
from src.motor.coast import coast_hold
from src.motor.drive import RegisterMap
from src.result import Ok
from src.units import MotorRpm, Seconds
from tests.test_atv320 import (
    ETA_FAULT,
    ETA_OPERATION_ENABLED,
    ETA_SWITCH_ON_DISABLED,
    SETTINGS,
    FakeBus,
    io_exception,
    seed_bench_limits,
)

TAU: Final[Seconds] = Seconds(40.0)
ONE: Final[MotorRpm] = MotorRpm(1)

FROM_250: Final[Seconds] = coast_hold(
    MotorRpm(250), since_read=Seconds(0.0), tau=TAU, standstill=ONE
)
"""What a shaft driven towards 300 for a second may need: 221 s."""


def _link() -> DriveLink:
    loaded = load_local_config(
        {
            "MOTOR_BACKEND": "serial",
            "MOTOR_PORT": "COM-NONE",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
        }
    )
    assert isinstance(loaded, Ok), loaded
    link = loaded.value.drive_link
    assert link is not None
    return link


class LosesStatus:
    """Carries every frame, except a read of RFRD while ``lossy``: the status read fails,
    while the enable's ETA checks and the slider's LFRD read-back get through.
    ``lossy_from_enable``: lossy from the moment CMD = 15 is written."""

    def __init__(self, bus: FakeBus) -> None:
        self.bus: FakeBus = bus
        self.lossy: bool = False
        self.lossy_from_enable: bool = False

    def connect(self) -> bool:
        return self.bus.connect()

    def close(self) -> None:
        self.bus.close()

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        if self.lossy and address == self.bus.regs.rfrd:
            raise io_exception()
        return self.bus.read_holding_registers(address, count, slave)

    def write_register(self, address: int, value: int, slave: int = 1) -> object:
        if self.lossy_from_enable and address == self.bus.regs.cmd and value == 15:
            self.lossy = True
        return self.bus.write_register(address, value, slave)


class Bench:
    """A worker over a drive in memory, the line between them, and the clock."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock: ManualClock = ManualClock()
        regs = RegisterMap()
        self.bus: FakeBus = FakeBus(self.clock, regs)
        for address in (regs.lfrd, regs.rfrd, regs.lcr, regs.lft, regs.cmd):
            self.bus.registers[address] = 0
        self.bus.registers[regs.eta] = ETA_SWITCH_ON_DISABLED
        seed_bench_limits(self.bus)
        self.line: LosesStatus = LosesStatus(self.bus)
        driver = ATV320Drive(
            self.clock,
            cast(ModbusMaster, self.line),
            SETTINGS,
            regs,
            failure_threshold=3,
            settle_attempts=1,
            settle_delay=Seconds(0.0),
            stop_attempts=1,
            stop_poll_interval=Seconds(0.0),
        )
        built = BuiltDrive(drive=driver, master=cast(ModbusMaster, self.line))

        def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
            return built

        monkeypatch.setattr(bench, "build_drive", build)
        monkeypatch.setattr(bench, "COAST_TAU", TAU)
        self.worker: bench.BusWorker = bench.BusWorker(_link(), max_rpm=300, clock=self.clock)

    async def cycle(self, *commands: bench.Command) -> None:
        """One pass of the bus thread, then a cycle's time."""
        for command in commands:
            self.worker.submit(command)
        await self.worker.cycle_once()
        self.clock.advance(Seconds(bench.CYCLE_S))

    async def armed(self) -> None:
        """Connected and read at rest, the stage off: the first read is spent. Armed."""
        await self.cycle()
        self.worker.snap.armed = True

    def shown(self) -> str:
        return self.worker.snap.last_action

    def held(self) -> float | None:
        return self.worker.snap.coast_hold_s

    def sto_opens(self) -> None:
        """Stage off, RFRD 0, the reads back."""
        self.bus.registers[self.bus.regs.eta] = ETA_FAULT
        self.line.lossy = False


async def test_a_reference_up_and_down_between_two_reads_bounds_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled at rest, read on at RFRD 0 and LFRD 0; the reads fail while the slider's 300
    and then 0 land; the STO opens on the way down. The read that comes back shows RFRD 0,
    LFRD 0: the 300 written in between says where the shaft may have got to."""
    rig = Bench(monkeypatch)
    await rig.armed()
    await rig.cycle(bench.Command("enable"))
    assert rig.shown().startswith("enable: ok"), rig.shown()
    rig.line.lossy = True
    await rig.cycle(bench.Command("speed", 300))
    assert rig.shown() == "speed 300 tr/min: ok (relu sur LFRD)"
    await rig.cycle()
    await rig.cycle(bench.Command("speed", 0))
    assert rig.shown() == "speed 0 tr/min: ok (relu sur LFRD)"
    assert rig.worker.snap.armed, "still armed: the slider is live"
    rig.sto_opens()
    await rig.cycle()
    left = rig.held()
    assert left is not None
    assert left >= FROM_250
    # The STO closed again: Activer waits all the same.
    rig.bus.registers[rig.bus.regs.eta] = ETA_SWITCH_ON_DISABLED
    await rig.cycle()
    await rig.cycle(bench.Command("enable"))
    assert "activation refusee (arbre peut-etre encore en roue libre" in rig.shown(), rig.shown()


async def test_a_stage_enabled_and_never_read_on_drops_with_its_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activer succeeds (its 6, 7, 15 are checked on ETA in the driver), then every poll
    fails while the slider's 300 lands; the STO opens. No read ever showed the stage on."""
    rig = Bench(monkeypatch)
    await rig.armed()
    rig.line.lossy_from_enable = True
    await rig.cycle(bench.Command("enable"))
    assert rig.line.lossy
    assert rig.shown().startswith("enable: ok"), rig.shown()
    await rig.cycle(bench.Command("speed", 300))
    await rig.cycle()
    assert rig.worker.snap.armed
    rig.sto_opens()
    await rig.cycle()
    assert rig.bus.registers[rig.bus.regs.lfrd] == 300
    left = rig.held()
    assert left is not None
    assert left >= FROM_250


async def test_an_exit_after_a_stage_enabled_and_never_read_on_leaves_the_longest_coast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, then Ctrl+C before a read comes back: the driver's stop reads RFRD 0 on
    the dropped stage and calls it confirmed. What the tool did itself still says a
    coast may follow."""
    rig = Bench(monkeypatch)
    await rig.armed()
    rig.line.lossy_from_enable = True
    await rig.cycle(bench.Command("enable"))
    await rig.cycle(bench.Command("speed", 300))
    await rig.cycle()
    rig.bus.registers[rig.bus.regs.eta] = ETA_FAULT
    left = rig.worker.coast_after_exit(confirmed=True)
    assert left is not None
    assert left >= FROM_250


async def test_a_stop_from_speed_read_to_rest_with_the_stage_on_holds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arrêter from 300 rpm: LFRD 0, the status read until RFRD 0 with the stage on, then
    7 and 6 at standstill. Nothing coasts, and nothing is held for the next Activer."""
    rig = Bench(monkeypatch)
    await rig.armed()
    regs = rig.bus.regs
    rig.bus.registers[regs.eta] = ETA_OPERATION_ENABLED
    rig.bus.registers[regs.rfrd] = 300
    rig.bus.registers[regs.lfrd] = 300
    await rig.cycle()
    rig.bus.registers[regs.rfrd] = 0  # the dEC ramp is over by the stop's first read
    await rig.cycle(bench.Command("stop"))
    assert rig.shown() == "stop: ok (RFRD=0, puis CMD=6)", rig.shown()
    await rig.cycle()
    assert rig.held() is None
    assert rig.worker.coast_after_exit(confirmed=True) is None


async def test_a_stop_during_which_the_stage_drops_is_no_standstill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The STO opens during Arrêter: RFRD reads 0, the stage is off. That is no standstill,
    6 is not written, and the hold starts from the speed last read under torque."""
    rig = Bench(monkeypatch)
    await rig.armed()
    regs = rig.bus.regs
    rig.bus.registers[regs.eta] = ETA_OPERATION_ENABLED
    rig.bus.registers[regs.rfrd] = 300
    rig.bus.registers[regs.lfrd] = 300
    await rig.cycle()
    rig.bus.registers[regs.eta] = ETA_FAULT
    rig.bus.registers[regs.rfrd] = 0
    await rig.cycle(bench.Command("stop"))
    assert rig.shown().startswith("stop: ECHEC: arret NON confirme"), rig.shown()
    assert "etage coupe (FAULT)" in rig.shown()
    assert "CMD=6 NON envoye" in rig.shown()
    left = rig.held()
    assert left is not None
    assert left >= coast_hold(MotorRpm(300), since_read=Seconds(0.0), tau=TAU, standstill=ONE)
