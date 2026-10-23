"""The STO relay feedback read stays out of the drive's exchange log, not out of its link.

The exchange log feeds the session record, which a native replay reproduces
tick by tick. An auxiliary read polled between ticks is no part of that path:
it is not appended to the log, while it still goes through the one driver,
its lock, its refusal once latched and its failure accounting.
"""

from __future__ import annotations

from src.clock import ManualClock, RealClock
from src.motor.atv320 import ATV320Drive, AuxiliaryRegisterReads
from src.motor.drive import LOGIC_INPUTS_LOGICAL, CommTimeout, DriveError, RegisterMap
from src.motor.observation import Exchange, ExchangeKind, ExchangeLog
from src.result import Err, Ok, Result
from src.units import MotorRpm, RawRegister, Seconds
from tests.test_atv320 import FakeBus, build_drive, connection_exception
from tests.test_ftdi_link import DRIVE_ADDRESS, FakeAltivar, crc16, ftdi_master

REGISTERS = RegisterMap()
STATUS = {3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0}


def kept(log: ExchangeLog) -> tuple[Exchange, ...]:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return log.entries


def lost(drive: ATV320Drive) -> bool:
    """Read afresh, for the same reason."""
    return drive.link_lost


async def opened(clock: ManualClock, bus: FakeBus) -> ATV320Drive:
    drive = build_drive(clock, bus)
    assert isinstance(await drive.open(), Ok)
    return drive


async def test_an_auxiliary_read_is_kept_out_of_the_exchange_log_only() -> None:
    clock = ManualClock()
    bus = FakeBus(clock, REGISTERS)
    bus.registers.update({**STATUS, REGISTERS.logic_inputs: 1})
    drive = await opened(clock, bus)
    log = ExchangeLog(clock, transport=False)
    drive.observe_exchanges(log)
    reads = AuxiliaryRegisterReads(drive)
    try:
        assert reads.drive is drive
        assert await reads.read_register(REGISTERS.logic_inputs) == Ok(RawRegister(1))
        assert kept(log) == ()
        assert bus.reads()[-1] == REGISTERS.logic_inputs  # it did go on the wire
        assert await drive.read_register(REGISTERS.logic_inputs) == Ok(RawRegister(1))
        assert [(entry.kind, entry.register) for entry in kept(log)] == [
            (ExchangeKind.READ, REGISTERS.logic_inputs)
        ]
        assert bus.max_in_flight == 1
    finally:
        await drive.close()


async def test_failed_auxiliary_reads_still_count_in_the_link_health() -> None:
    clock = ManualClock()
    bus = FakeBus(clock, REGISTERS)
    bus.registers.update({**STATUS, REGISTERS.logic_inputs: 1})
    drive = await opened(clock, bus)
    log = ExchangeLog(clock, transport=False)
    drive.observe_exchanges(log)
    reads = AuxiliaryRegisterReads(drive)
    try:
        # A success between failures resets the run, as for any read.
        for outcome in (connection_exception(), connection_exception()):
            bus.script_reads.append(outcome)
            assert isinstance(await reads.read_register(REGISTERS.logic_inputs), Err)
        assert await reads.read_register(REGISTERS.logic_inputs) == Ok(RawRegister(1))
        assert not lost(drive)
        outcomes: list[Result[RawRegister, DriveError]] = []
        for _ in range(3):
            bus.script_reads.append(connection_exception())
            outcomes.append(await reads.read_register(REGISTERS.logic_inputs))
        assert lost(drive)  # three in a row latch the shared link down
        assert [type(outcome) for outcome in outcomes] == [Err, Err, Err]
        last = outcomes[-1]
        assert isinstance(last, Err)
        assert isinstance(last.error, CommTimeout)
        sent = len(bus.log)
        refused = await reads.read_register(REGISTERS.logic_inputs)
        assert isinstance(refused, Err)
        assert isinstance(await drive.read_status(), Err)
        assert len(bus.log) == sent  # a latched link carries no auxiliary frame
        assert kept(log) == ()
    finally:
        await drive.close()


async def test_the_wire_bytes_of_an_auxiliary_read_stay_visible_to_a_transport_diagnostic() -> None:
    """With the SDK's transport calls kept, the frame is seen as bytes, never as a READ."""
    clock = RealClock()
    chip = FakeAltivar({**STATUS, LOGIC_INPUTS_LOGICAL: 0x0001})
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))
    drive = ATV320Drive(clock, master, settings, REGISTERS)
    diagnostic = ExchangeLog(clock)
    drive.observe_exchanges(diagnostic)
    try:
        assert isinstance(await drive.open(), Ok)
        before = len(diagnostic.entries)
        requests = len(chip.requests)
        assert await AuxiliaryRegisterReads(drive).read_register(LOGIC_INPUTS_LOGICAL) == Ok(
            RawRegister(1)
        )
        added = diagnostic.entries[before:]
        assert added, "the transport diagnostic saw nothing"
        assert {entry.kind for entry in added} <= {ExchangeKind.SEND, ExchangeKind.RECEIVE_CHUNK}
        assert added[0].kind is ExchangeKind.SEND
        assert added[0].raw_hex == chip.requests[requests].hex()
        assert all(entry.register is None for entry in added)
    finally:
        await drive.close()


async def test_a_feedback_reply_arriving_after_its_read_expired_is_never_taken_for_the_next() -> (
    None
):
    """The drive answers 5202 late, just before the next frame: that answer is discarded.

    pymodbus closes the port on a read left without an answer and reopens it for
    the next transaction; the reopened chip is settled, and pymodbus drops any
    byte waiting before it sends. The next exchanges read their own replies.
    """
    clock = RealClock()
    chip = FakeAltivar({**STATUS, LOGIC_INPUTS_LOGICAL: 0x0001})
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))
    drive = ATV320Drive(clock, master, settings, REGISTERS)
    try:
        assert isinstance(await drive.open(), Ok)
        chip.silent = True
        expired = await AuxiliaryRegisterReads(drive).read_register(LOGIC_INPUTS_LOGICAL)
        assert isinstance(expired, Err)
        assert isinstance(expired.error, CommTimeout)
        assert chip.close_calls == 1  # the port was closed on the silence
        chip.silent = False
        late = bytes((DRIVE_ADDRESS, 3, 2, 0x00, 0x01))
        chip.pending.extend((late + crc16(late))[index : index + 3] for index in (0, 3, 6))
        sent = len(chip.requests)
        assert await drive.write_speed(MotorRpm(0)) == Ok(None)  # its echo, not the late word
        status = await drive.read_status()
        assert isinstance(status, Ok)
        assert int(status.value.status_word) == 64  # ETA, not the late 0x0001
        assert len(chip.requests) == sent + 6  # LFRD write, echo, then the four status reads
        assert not chip.pending
    finally:
        await drive.close()
