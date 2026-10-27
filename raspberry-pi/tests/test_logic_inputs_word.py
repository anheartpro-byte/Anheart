"""The shared logic-input word: what the STO poll keeps of it, what the simulated drive puts in it.

Other logic inputs are read from the same 5202 frame as the relay feedback,
without another Modbus transaction: :attr:`DriveInputStoSource.last_word` keeps
the whole word of the last successful read, and
:meth:`SimulatedDrive.connect_logic_input` puts more inputs into the simulated
word without touching anything else of the plant.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest

from src.clock import ManualClock
from src.motor.drive import (
    LOGIC_INPUTS_LOGICAL,
    CommTimeout,
    ControlWord,
    DriveError,
    LogicInput,
)
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import STO_STALE_AFTER, DriveInputStoSource, SimulatedStoChain, StoState
from src.result import Err, Ok, Result
from src.units import Monotonic, MotorRpm, RawRegister, RegisterAddress, Seconds
from tests.test_sto import ERRORS, enable

LAST: Final[RawRegister] = RawRegister(0x0015)
"""LI1 (the relay feedback below), LI3 and LI5 high."""


class WordPort:
    """Answers every read with ``result``; a read started after ``suspend`` waits for release."""

    def __init__(self) -> None:
        self.result: Result[RawRegister, DriveError] = Ok(RawRegister(0))
        self.entered: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event | None = None

    def suspend(self) -> asyncio.Event:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        return self.release

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        del address
        release = self.release
        if release is not None:
            self.entered.set()
            await release.wait()
        return self.result


class Level:
    """One wire's level, set by the test; counts how often the drive samples it."""

    def __init__(self, *, high: bool = False) -> None:
        self.high: bool = high
        self.samples: int = 0

    def __call__(self) -> bool:
        self.samples += 1
        return self.high


def word_of(source: DriveInputStoSource) -> RawRegister | None:
    """A call, so the type checkers do not keep an asserted None across a poll."""
    return source.last_word


def relay_source(clock: ManualClock, port: WordPort) -> DriveInputStoSource:
    return DriveInputStoSource(
        clock=clock, drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )


async def read_in_flight(source: DriveInputStoSource, port: WordPort) -> asyncio.Task[None]:
    """Start a poll and return once its read is suspended inside the port."""
    port.suspend()
    pending = asyncio.create_task(source.poll())
    await asyncio.wait_for(port.entered.wait(), 3)
    return pending


# -- DriveInputStoSource.last_word -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "state"),
    [
        (0x0001, StoState.READY),
        (0x003E, StoState.OPEN),
        (0xFFFF, StoState.READY),
        (0x8020, StoState.OPEN),
    ],
)
async def test_last_word_is_none_before_any_success_then_the_whole_word_read(
    raw: int, state: StoState
) -> None:
    clock, port = ManualClock(), WordPort()
    source = relay_source(clock, port)
    assert word_of(source) is None
    source.guard_polling(lambda: False)
    await source.poll()
    assert word_of(source) is None
    source.guard_polling(lambda: True)
    port.result = Ok(RawRegister(raw))
    clock.advance(Seconds(1))
    await source.poll()
    assert word_of(source) == raw
    reading = source.latest(clock.monotonic())
    assert reading.state is state
    assert reading.last_success_at == Monotonic(1)


async def test_last_word_does_not_age_its_freshness_is_that_of_latest() -> None:
    clock, port = ManualClock(), WordPort()
    source = relay_source(clock, port)
    port.result = Ok(LAST)
    await source.poll()
    clock.advance(Seconds(STO_STALE_AFTER + 0.001))
    stale = source.latest(clock.monotonic())
    assert stale.state is StoState.UNKNOWN
    assert stale.detail == "stale reading"
    assert stale.last_success_at == Monotonic(0)
    assert word_of(source) == LAST
    source.guard_polling(lambda: False)
    port.result = Ok(RawRegister(0))
    await source.poll()
    assert source.attempts == 1
    assert word_of(source) == LAST


@pytest.mark.parametrize("error", ERRORS)
async def test_a_failed_read_drops_the_word_until_the_next_success(error: DriveError) -> None:
    clock, port = ManualClock(), WordPort()
    source = relay_source(clock, port)
    port.result = Err(error)
    await source.poll()
    assert word_of(source) is None
    port.result = Ok(LAST)
    await source.poll()
    assert word_of(source) == LAST
    clock.advance(Seconds(0.1))
    port.result = Err(error)
    await source.poll()
    assert word_of(source) is None
    failed = source.latest(clock.monotonic())
    assert failed.state is StoState.UNKNOWN
    assert failed.last_success_at == Monotonic(0)
    clock.advance(Seconds(0.1))
    port.result = Ok(RawRegister(0x0002))
    await source.poll()
    assert word_of(source) == 0x0002
    assert source.latest(clock.monotonic()).state is StoState.OPEN


@pytest.mark.parametrize(
    ("ending", "word", "state"),
    [
        (Ok(RawRegister(0x0002)), RawRegister(0x0002), StoState.OPEN),
        (Err(CommTimeout(after=Seconds(0.1))), None, StoState.UNKNOWN),
    ],
)
async def test_a_read_in_flight_after_a_success_keeps_its_word_until_it_ends(
    ending: Result[RawRegister, DriveError], word: RawRegister | None, state: StoState
) -> None:
    clock, port = ManualClock(), WordPort()
    source = relay_source(clock, port)
    port.result = Ok(LAST)
    await source.poll()
    pending = await read_in_flight(source, port)
    try:
        in_flight = source.latest(clock.monotonic())
        assert in_flight.state is StoState.READY
        assert in_flight.read_unresolved
        assert word_of(source) == LAST
        port.result = ending
        release = port.release
        assert release is not None
        release.set()
        await asyncio.wait_for(pending, 3)
        assert word_of(source) == word
        assert source.latest(clock.monotonic()).state is state
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_a_cancelled_read_keeps_the_word_of_the_last_success() -> None:
    clock, port = ManualClock(), WordPort()
    source = relay_source(clock, port)
    port.result = Ok(LAST)
    await source.poll()
    pending = await read_in_flight(source, port)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert pending.cancelled()
    assert word_of(source) == LAST
    reading = source.latest(clock.monotonic())
    assert reading.state is StoState.READY
    assert reading.read_unresolved


# -- SimulatedDrive.connect_logic_input --------------------------------------


@pytest.mark.parametrize("chain_ready", [None, True, False])
async def test_simulated_word_carries_each_connected_input_at_its_level_when_read(
    chain_ready: bool | None,
) -> None:
    clock = ManualClock()
    chain = None if chain_ready is None else SimulatedStoChain(ready=chain_ready)
    drive = SimulatedDrive(clock, sto_chain=chain)
    li3, li5 = Level(), Level()
    drive.connect_logic_input(LogicInput.LI3, li3)
    drive.connect_logic_input(LogicInput.LI5, li5)
    assert (li3.samples, li5.samples) == (0, 0)
    opened_link = await drive.open()
    assert isinstance(opened_link, Ok)
    relay = 0x0001 if chain_ready else 0
    for high3, high5, word in (
        (False, False, relay),
        (True, False, relay | 0x0004),
        (False, True, relay | 0x0010),
        (True, True, relay | 0x0014),
    ):
        li3.high, li5.high = high3, high5
        assert await drive.read_register(LOGIC_INPUTS_LOGICAL) == Ok(RawRegister(word))
    assert (li3.samples, li5.samples) == (4, 4)


@pytest.mark.parametrize("sto_input", list(LogicInput))
async def test_an_input_is_connected_once_and_never_over_the_relay_feedback(
    sto_input: LogicInput,
) -> None:
    drive = SimulatedDrive(ManualClock(), SimulatedDriveConfig(sto_input=sto_input))
    with pytest.raises(ValueError, match="STO relay feedback"):
        drive.connect_logic_input(sto_input, Level(high=True))
    other = LogicInput.LI3 if sto_input is LogicInput.LI2 else LogicInput.LI2
    first, second = Level(), Level(high=True)
    drive.connect_logic_input(other, first)
    with pytest.raises(ValueError, match="already connected"):
        drive.connect_logic_input(other, second)
    opened_link = await drive.open()
    assert isinstance(opened_link, Ok)
    assert await drive.read_register(LOGIC_INPUTS_LOGICAL) == Ok(RawRegister(0))
    assert (first.samples, second.samples) == (1, 0)


@pytest.mark.parametrize("others_high", [True, False])
@pytest.mark.parametrize("sto_input", list(LogicInput))
async def test_a_drive_input_sto_source_reads_only_its_own_bit_among_connected_inputs(
    sto_input: LogicInput, others_high: bool
) -> None:
    clock, chain = ManualClock(), SimulatedStoChain(ready=False)
    drive = SimulatedDrive(clock, SimulatedDriveConfig(sto_input=sto_input), sto_chain=chain)
    others = [input_ for input_ in LogicInput if input_ is not sto_input]
    for input_ in others:
        drive.connect_logic_input(input_, Level(high=others_high))
    opened_link = await drive.open()
    assert isinstance(opened_link, Ok)
    source = DriveInputStoSource(
        clock=clock, drive=drive, address=LOGIC_INPUTS_LOGICAL, input_=sto_input
    )
    others_word = sum(1 << input_ for input_ in others) if others_high else 0
    for ready, state in ((False, StoState.OPEN), (True, StoState.READY), (False, StoState.OPEN)):
        if ready:
            chain.close(clock.monotonic())
        else:
            chain.open(clock.monotonic())
        await source.poll()
        assert source.latest(clock.monotonic()).state is state
        assert word_of(source) == others_word | ((1 << sto_input) if ready else 0)


async def test_connected_inputs_act_on_nothing_but_the_word_read() -> None:
    clock = ManualClock()
    plain = SimulatedDrive(clock, sto_chain=SimulatedStoChain())
    wired = SimulatedDrive(clock, sto_chain=SimulatedStoChain())
    levels = {input_: Level() for input_ in LogicInput if input_ is not LogicInput.LI1}
    for input_, level in levels.items():
        wired.connect_logic_input(input_, level)
    for drive in (plain, wired):
        opened_link = await drive.open()
        assert isinstance(opened_link, Ok)
        await enable(drive)
    reads = 0
    for step in range(24):
        if step == 12:
            for drive in (plain, wired):
                assert isinstance(await drive.write_command(ControlWord.SWITCH_ON), Ok)
        for level in levels.values():
            level.high = step % 3 != 0
        clock.advance(Seconds(0.5))
        for drive in (plain, wired):
            assert isinstance(await drive.write_speed(MotorRpm(900 if step < 12 else 0)), Ok)
            assert isinstance(await drive.read_register(LOGIC_INPUTS_LOGICAL), Ok)
        reads += 1
        plain_status, wired_status = await plain.read_status(), await wired.read_status()
        assert wired_status == plain_status
        assert wired.sim_state is plain.sim_state
        assert wired.commanded_setpoint == plain.commanded_setpoint
        if step == 11:
            assert isinstance(plain_status, Ok)
            assert plain_status.value.output_rpm > 0
    assert all(level.samples == reads for level in levels.values())
