"""The ATV320 Modbus driver, exercised against an in-memory drive.

No hardware, and nothing marked ``hardware``: there is no serial port on the
machine this was written on, and waiting for one would mean shipping an
untested driver to a bench where the motor is attached to a person-carrying
centrifuge. So :class:`FakeBus` is a real little Altivar rather than a stub -
it records every transaction, answers an address it does not implement with
Modbus exception code 2 (exactly what a wrong ``RegisterMap.offset`` produces),
**acks** a write to an address it does not implement (exactly what a real drive
does, and the whole reason write-verify exists), and walks the CiA402 states in
response to command words.

This is the one test module in the repository that imports ``pymodbus``, and it
does so deliberately. ``src/motor/atv320.py`` exists to convert pymodbus's
reply shapes into the closed ``DriveError`` union, and that mapping is only
worth anything if it is checked against the library's **real** objects - a
hand-rolled "looks like a PDU" double would prove the mapping matches my
understanding of pymodbus rather than pymodbus. A test at the bottom asserts
no other module under ``src/`` imports it.

The ``make_bus`` fixture checks, after every test that used it, that no fake
drive was left with a speed in LFRD. The most valuable property in this
codebase is that no exit path leaves the motor commanded, and that property is
worth nothing if the tests themselves normalise leaving a setpoint behind.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from collections import deque
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto, unique
from pathlib import Path
from typing import Final, Protocol, final, get_args

import pytest

# pyserial ships no type information. It is imported here for exactly one
# purpose - to pin the fact that `serial.SerialException` derives from
# `OSError`, which is what lets src/motor/atv320.py cover pyserial failures by
# catching OSError instead of importing the library at all (contract rule 5:
# one importer per untyped library, and that importer is the driver).
import serial  # type: ignore[import-untyped]
from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import (
    ConnectionException,
    ModbusIOException,
    ParameterException,
)
from pymodbus.pdu import ExceptionResponse
from pymodbus.pdu.register_read_message import ReadHoldingRegistersResponse
from pymodbus.pdu.register_write_message import WriteSingleRegisterResponse
from pymodbus.transport import CommParams

from src.clock import Clock, ManualClock, RealClock
from src.motor.atv320 import (
    DEFAULT_BAUDRATE,
    DEFAULT_BYTESIZE,
    DEFAULT_RETRIES,
    DEFAULT_SLAVE_ADDRESS,
    DEFAULT_STOPBITS,
    DEFAULT_TIMEOUT,
    EMERGENCY_SCHEDULING_MARGIN,
    MAX_EMERGENCY_BUDGET,
    PYMODBUS_BODY_RETRY_PAUSE,
    SCHNEIDER_POINT_TO_POINT_ADDRESS,
    ATV320Drive,
    FtdiModbusClient,
    ModbusMaster,
    OwnedSerialClient,
    SerialSettings,
    emergency_budget_for,
    serial_master,
    transaction_worst_case,
)
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveLimits,
    DriveState,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    RegisterMap,
    StopUnconfirmed,
    UnexpectedState,
)
from src.motor.ftdi_link import USB_STALL_BOUND, Parity, wait_overshoot
from src.result import Err, Ok, Result, err_of
from src.units import Hertz, MotorRpm, OutOfRange, Seconds

# =========================================================================
# Modbus and CiA402 constants used by the fake drive
# =========================================================================

READ_HOLDING_REGISTERS: Final[int] = 3
WRITE_SINGLE_REGISTER: Final[int] = 6

ILLEGAL_DATA_ADDRESS: Final[int] = 2
ILLEGAL_DATA_VALUE: Final[int] = 3

# ETA words, from the CiA402 patterns drive.py decodes.
ETA_SWITCH_ON_DISABLED: Final[int] = 0x0040
ETA_READY: Final[int] = 0x0021
ETA_SWITCHED_ON: Final[int] = 0x0023
ETA_OPERATION_ENABLED: Final[int] = 0x0027
ETA_FAULT: Final[int] = 0x0008
ETA_FAULT_WHILE_OPERATIONAL: Final[int] = 0x002F
"""The operation-enabled pattern with the fault bit set. A real drive reports
this during a fault reaction, while the centrifuge is still turning."""

#: The state each command word takes this fake drive to.
CIA402_TRANSITIONS: Final[Mapping[ControlWord, int]] = {
    ControlWord.SHUTDOWN: ETA_READY,
    ControlWord.SWITCH_ON: ETA_SWITCHED_ON,
    ControlWord.ENABLE_OPERATION: ETA_OPERATION_ENABLED,
}

LFT_OVERCURRENT: Final[int] = 9
LFT_UNDERVOLTAGE: Final[int] = 22


# =========================================================================
# Real pymodbus reply objects
# =========================================================================
#
# Each helper carries one `type: ignore[no-untyped-call]`: pymodbus ships
# py.typed but several of its PDU and exception constructors are unannotated,
# so mypy calls them untyped. Confined to four one-line factories rather than
# sprinkled through thirty tests, and every one of them constructs an object
# whose runtime shape is then asserted on.


def read_reply(values: list[int]) -> object:
    """A normal function-3 response carrying ``values``."""
    return ReadHoldingRegistersResponse(values=values)  # type: ignore[no-untyped-call]


def write_echo(address: int, value: int) -> object:
    """A normal function-6 response: the request, echoed back.

    Note what it does NOT carry: a ``registers`` block. ``ModbusPDU`` declares
    ``registers: list[int]`` without assigning it, so reading that attribute
    off this object raises ``AttributeError`` - which is what happens when the
    framer decodes a stale write echo as the reply to a read.
    """
    return WriteSingleRegisterResponse(address=address, value=value)  # type: ignore[no-untyped-call]


def io_exception(detail: str = "no response received") -> BaseException:
    """What pymodbus 3.7.4's sync transaction manager RETURNS on a silent drive."""
    return ModbusIOException(detail, READ_HOLDING_REGISTERS)  # type: ignore[no-untyped-call]


def connection_exception(detail: str = "port would not open") -> BaseException:
    """What ``ModbusBaseSyncClient.execute`` RAISES when connect() fails."""
    return ConnectionException(detail)  # type: ignore[no-untyped-call]


def parameter_exception(detail: str = "count out of range") -> BaseException:
    """A ModbusException that is neither a timeout nor an I/O failure."""
    return ParameterException(detail)  # type: ignore[no-untyped-call]


def exception_response(function_code: int, code: int) -> object:
    """A drive that answers "no such address" / "bad value". Constructor is typed."""
    return ExceptionResponse(function_code, code)


# =========================================================================
# The fake bus
# =========================================================================


@unique
class Access(Enum):
    """Which Modbus function a recorded transaction used."""

    READ = auto()
    WRITE = auto()


@unique
class Behave(Enum):
    """The absence of a scripted outcome: act like a drive."""

    NORMALLY = auto()


@dataclass(frozen=True, slots=True)
class Returned:
    """Script an exception object to be RETURNED rather than raised.

    This distinction is not pedantry. pymodbus 3.7.4 both *raises*
    ``ConnectionException`` (from ``ModbusBaseSyncClient.execute``) and
    *returns* a ``ModbusIOException`` instance where its own annotation
    promises a ``ModbusPDU`` (from ``SyncModbusTransactionManager.execute``).
    A fake that could only raise would leave the returned path - the common
    one, a drive that simply went quiet - completely untested.
    """

    reply: BaseException


@dataclass(frozen=True, slots=True)
class Transaction:
    """One thing the driver put on the wire."""

    access: Access
    address: int
    slave: int
    value: int | None = None
    """The value written; ``None`` for a read."""


@final
class FakeBus:
    """An in-memory Altivar on an in-memory RS-485 pair.

    Scripting: ``script_reads`` / ``script_writes`` are queues consumed one per
    transaction, and ``sticky_reads`` / ``sticky_writes`` apply once a queue is
    empty. An entry that is a ``BaseException`` is **raised**; any other object
    is **returned** in place of the reply PDU (because that is a thing pymodbus
    genuinely does); :data:`Behave.NORMALLY` means act like a drive.
    """

    __slots__ = (
        "_in_flight",
        "accepted_leftover_setpoint",
        "clock",
        "close_calls",
        "connect_calls",
        "entered_read",
        "latency",
        "log",
        "max_in_flight",
        "obeys",
        "on_command",
        "overlap_raises",
        "port_opens",
        "raise_on_close",
        "raise_on_connect",
        "real_read_block",
        "real_write_block",
        "registers",
        "regs",
        "script_reads",
        "script_writes",
        "sticky_reads",
        "sticky_writes",
    )

    def __init__(self, clock: ManualClock, registers: RegisterMap) -> None:
        self.clock: ManualClock = clock
        self.regs: RegisterMap = registers
        self.registers: dict[int, int] = {}
        self.log: list[Transaction] = []

        self.port_opens: bool = True
        self.connect_calls: int = 0
        self.close_calls: int = 0
        self.raise_on_connect: BaseException | None = None
        self.raise_on_close: BaseException | None = None

        self.script_reads: deque[object] = deque()
        self.script_writes: deque[object] = deque()
        self.sticky_reads: object = Behave.NORMALLY
        self.sticky_writes: object = Behave.NORMALLY

        self.latency: Seconds = Seconds(0.0)
        """Advanced on the injected clock per transaction, so a measured
        ``CommTimeout.after`` can be asserted without any real waiting."""

        self.obeys: frozenset[ControlWord] = frozenset(CIA402_TRANSITIONS)
        """Command words this drive acts on. Drop one to make the enable
        sequence stall exactly there."""

        self.on_command: dict[ControlWord, int] = {}
        """Override the ETA word a command word leads to, e.g. to make a fault
        appear in the middle of the start sequence."""

        self.accepted_leftover_setpoint: int | None = None
        """Declared by a test that deliberately ends with a non-zero LFRD
        because it is asserting the ENCODING rather than a command. Anything
        undeclared fails the autouse teardown check."""

        self._in_flight: int = 0
        self.max_in_flight: int = 0

        # --- REAL wall time, for the bounds a ManualClock cannot express ----
        #
        # `latency` above advances the injected clock, which makes measured
        # CommTimeout values exact and costs no test runtime - but it is
        # therefore incapable of proving that a call RETURNS within a duration.
        # A pyserial read really does block the thread, and a budget that is
        # only enforced against a clock the test advances is not a budget. The
        # two fields below block for real, honouring the configured serial
        # timeout the way pyserial does.
        self.real_read_block: Seconds = Seconds(0.0)
        self.real_write_block: Seconds = Seconds(0.0)

        self.entered_read: threading.Event = threading.Event()
        """Set once a read is inside the transaction body, so a test can know
        the executor thread is genuinely mid-transaction before racing it."""

        self.overlap_raises: bool = False
        """Raise ConnectionException on any transaction that starts while
        another is in flight.

        This is the pymodbus race, modelled: `connect()` is called OUTSIDE the
        library's transaction lock and the port is opened `exclusive=True`, so
        the second thread in loses the race, `connect()` returns False, and
        `execute` raises ConnectionException. A driver that drives the shared
        client from two threads therefore does not get corrupt data, it gets a
        LOST WRITE - and on the emergency path that is the write that mattered.
        """

    # --- Inspection -----------------------------------------------------

    def setpoint(self) -> int:
        """The raw LFRD register, at this bus's own addressing."""
        return self.registers.get(self.regs.lfrd, 0)

    def reads(self) -> list[int]:
        return [t.address for t in self.log if t.access is Access.READ]

    def writes(self) -> list[tuple[int, int | None]]:
        return [(t.address, t.value) for t in self.log if t.access is Access.WRITE]

    def command_words(self) -> list[int | None]:
        """Every value written to CMD, in order."""
        return [
            t.value for t in self.log if t.access is Access.WRITE and t.address == self.regs.cmd
        ]

    # --- ModbusMaster ---------------------------------------------------

    def connect(self) -> bool:
        self.connect_calls += 1
        if self.raise_on_connect is not None:
            self.clock.advance(self.latency)
            raise self.raise_on_connect
        self.clock.advance(self.latency)
        return self.port_opens

    def close(self) -> None:
        self.close_calls += 1
        if self.raise_on_close is not None:
            raise self.raise_on_close

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        with self._transaction(self.real_read_block, self.entered_read):
            self.log.append(Transaction(Access.READ, address, slave))
            scripted = _next_outcome(self.script_reads, self.sticky_reads)
            if scripted is not Behave.NORMALLY:
                return _respond(scripted)
            if address not in self.registers:
                # A real drive refuses an address it does not implement. This is
                # the one failure a wrong offset reliably produces.
                return exception_response(READ_HOLDING_REGISTERS, ILLEGAL_DATA_ADDRESS)
            return read_reply([self.registers[address]] * count)

    def write_register(self, address: int, value: int, slave: int = 1) -> object:
        with self._transaction(self.real_write_block, None):
            self.log.append(Transaction(Access.WRITE, address, slave, value))
            scripted = _next_outcome(self.script_writes, self.sticky_writes)
            if scripted is not Behave.NORMALLY:
                return _respond(scripted)
            # Acked and stored even at an address this drive never heard of:
            # that is what an Altivar does, and it is why a write to the wrong
            # register is invisible to the write itself.
            self.registers[address] = value
            self._follow_cia402(address, value)
            return write_echo(address, value)

    # --- Internals ------------------------------------------------------

    @contextmanager
    def _transaction(self, real_block: Seconds, entered: threading.Event | None) -> Generator[None]:
        """Assert the half-duplex invariant from inside the slave.

        No test can observe two overlapping transactions; the bus can, and a
        single-flight bug shows up on the bench as corrupt frames rather than
        as a failing assertion.

        ``overlap_raises`` turns the observation into the failure a real
        exclusive serial port produces - see the field's docstring - so the
        consequence of overlapping can be tested and not merely counted.

        ``entered`` is signalled BEFORE the real block, not after it: a test
        racing the executor thread needs to know the transaction has started,
        and an event set once it has nearly finished would make every such race
        a no-op that quietly passes.
        """
        overlapping = self._in_flight > 0
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        self.clock.advance(self.latency)
        if entered is not None:
            entered.set()
        try:
            if overlapping and self.overlap_raises:
                raise connection_exception("the port is already open exclusively elsewhere")
            if real_block > 0.0:
                # REAL seconds, in the thread the caller is on, exactly as a
                # pyserial read does. This is the only way a test can say
                # anything about how long a call takes to return.
                time.sleep(real_block)
            yield
        finally:
            self._in_flight -= 1

    def _follow_cia402(self, address: int, value: int) -> None:
        """Move ETA the way a drive would, so a start sequence is verifiable."""
        if address != self.regs.cmd:
            return
        word = _as_control_word(value)
        if word is None or word not in self.obeys:
            return
        override = self.on_command.get(word)
        self.registers[self.regs.eta] = (
            override if override is not None else CIA402_TRANSITIONS[word]
        )


def _next_outcome(queue: deque[object], sticky: object) -> object:
    if queue:
        return queue.popleft()
    return sticky


def _respond(scripted: object) -> object:
    """A scripted outcome: raise it, return it, or stand in for the reply PDU."""
    if isinstance(scripted, Returned):
        return scripted.reply
    if isinstance(scripted, BaseException):
        raise scripted
    return scripted


def _as_control_word(value: int) -> ControlWord | None:
    for word in ControlWord:
        if word.value == value:
            return word
    return None


# =========================================================================
# Fixtures
# =========================================================================


class BusFactory(Protocol):
    """Builds fake drives inside a test, and registers them for the teardown check.

    A Protocol rather than ``Callable[..., FakeBus]``: the ellipsis form is an
    implicit ``Any`` over the parameters, which this repository does not allow
    even in a fixture type.
    """

    def __call__(self, *, offset: int = 0, eta: int = ETA_SWITCH_ON_DISABLED) -> FakeBus: ...


SETTINGS: Final[SerialSettings] = SerialSettings(port="COM-NONE")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def make_bus(clock: ManualClock) -> Iterator[BusFactory]:
    """Build fake drives, and refuse to let the test end with one still commanded."""
    created: list[FakeBus] = []

    def build(*, offset: int = 0, eta: int = ETA_SWITCH_ON_DISABLED) -> FakeBus:
        registers = RegisterMap(offset=offset)
        bus = FakeBus(clock, registers)
        bus.registers[registers.eta] = eta
        bus.registers[registers.lfrd] = 0
        bus.registers[registers.rfrd] = 0
        bus.registers[registers.lcr] = 0
        bus.registers[registers.lft] = 0
        bus.registers[registers.cmd] = 0
        created.append(bus)
        return bus

    yield build

    for index, bus in enumerate(created):
        allowed = 0 if bus.accepted_leftover_setpoint is None else bus.accepted_leftover_setpoint
        assert bus.setpoint() == allowed, (
            f"bus #{index} ends this test with LFRD = {bus.setpoint()}, i.e. with a "
            "speed still commanded. Zero it, close the drive, or set "
            "accepted_leftover_setpoint if the test is asserting the encoding."
        )


@pytest.fixture
def bus(make_bus: BusFactory) -> FakeBus:
    return make_bus()


def build_drive(
    clock: Clock,
    bus: FakeBus,
    *,
    offset: int = 0,
    failure_threshold: int = 3,
    settle_attempts: int = 1,
    stop_attempts: int = 1,
    settings: SerialSettings = SETTINGS,
) -> ATV320Drive:
    """A driver wired to a fake bus.

    ``settle_attempts`` and ``stop_attempts`` default to 1 so most tests see
    exactly one ETA read per enable step and one RFRD read per close, keeping
    the transaction log readable; the retry behaviour of each has tests of its
    own. ``settle_delay`` and ``stop_poll_interval`` are zero so nothing sleeps.
    """
    return ATV320Drive(
        clock,
        bus,
        settings,
        RegisterMap(offset=offset),
        failure_threshold=failure_threshold,
        settle_attempts=settle_attempts,
        settle_delay=Seconds(0.0),
        stop_attempts=stop_attempts,
        stop_poll_interval=Seconds(0.0),
    )


@pytest.fixture
def drive(clock: ManualClock, bus: FakeBus) -> ATV320Drive:
    return build_drive(clock, bus)


# =========================================================================
# The seam
# =========================================================================


def test_the_fake_bus_satisfies_the_same_protocol_as_pymodbus(bus: FakeBus) -> None:
    assert isinstance(bus, ModbusMaster)


def test_atv320_satisfies_the_drive_backend_protocol(drive: ATV320Drive) -> None:
    backend: DriveBackend = drive
    assert isinstance(backend, DriveBackend)


def line_settings(master: ModbusMaster) -> CommParams:
    """The line settings pymodbus actually recorded for this client.

    ``CommParams`` is a fully typed pymodbus dataclass, but basedpyright reads
    the ``comm_params`` attribute itself as unknown - it is assigned inside an
    ``if`` in ``ModbusBaseSyncClient.__init__`` with no class-level
    declaration. Narrowed once here so the assertions below are ordinary typed
    comparisons: rule 6 applied to a test.
    """
    assert isinstance(master, ModbusSerialClient)
    params: CommParams = master.comm_params  # pyright: ignore[reportUnknownMemberType]
    return params


def test_serial_master_pins_the_commissioned_line_settings() -> None:
    """19200 8E1, and the port is NOT opened by constructing the client."""
    master = serial_master(SerialSettings(port="COM-NONE"), ManualClock())
    assert isinstance(master, ModbusMaster)
    assert type(master) is OwnedSerialClient
    params = line_settings(master)
    assert params.host == "COM-NONE"
    assert params.baudrate == DEFAULT_BAUDRATE == 19200
    assert params.bytesize == DEFAULT_BYTESIZE == 8
    assert params.parity == Parity.EVEN.value == "E"
    assert params.stopbits == DEFAULT_STOPBITS == 1
    assert params.timeout_connect == DEFAULT_TIMEOUT == 0.1


@pytest.mark.parametrize("port", ["/dev/cu.usbserial-A10K1234", "/dev/ttyUSB0", "COM3"])
def test_an_os_serial_device_gets_the_stock_pymodbus_client(port: str) -> None:
    """pyserial's own ports have a working in_waiting; only ftdi:// needs the adapter."""
    master = serial_master(SerialSettings(port=port), ManualClock())
    assert type(master) is OwnedSerialClient
    assert line_settings(master).host == port


@pytest.mark.parametrize("port", ["ftdi://schneider:rs485/1", "FTDI://schneider:rs485/1"])
def test_an_ftdi_url_gets_the_buffered_ftdi_client(port: str) -> None:
    """Same line settings, a different port underneath. Nothing is opened here."""
    master = serial_master(SerialSettings(port=port), ManualClock())
    assert isinstance(master, FtdiModbusClient)
    params = line_settings(master)
    assert params.host == port
    assert params.parity == "E"
    assert params.timeout_connect == DEFAULT_TIMEOUT


def test_the_defaults_are_what_the_bench_measured() -> None:
    """Address 248 answered on the bench and address 1 did not."""
    assert DEFAULT_SLAVE_ADDRESS == SCHNEIDER_POINT_TO_POINT_ADDRESS == 248
    assert SerialSettings(port="p").slave_address == 248
    assert DEFAULT_RETRIES == 1


def test_serial_settings_reject_the_broadcast_address() -> None:
    """Address 0 commands every drive on the bus and answers nothing."""
    with pytest.raises(ValueError, match="broadcast"):
        SerialSettings(port="p", slave_address=0)
    assert SerialSettings(port="p", slave_address=1).slave_address == 1
    assert SerialSettings(port="p", slave_address=247).slave_address == 247


def test_serial_settings_accept_the_schneider_point_to_point_address() -> None:
    """248 is not a broadcast: it is the address an Altivar answers alone on a link."""
    assert SerialSettings(port="p", slave_address=248).slave_address == 248


@pytest.mark.parametrize("address", [-1, 249, 255, 256])
def test_serial_settings_reject_every_other_address(address: int) -> None:
    with pytest.raises(ValueError, match="point-to-point"):
        SerialSettings(port="p", slave_address=address)


@pytest.mark.parametrize("retries", [0, -1])
def test_serial_settings_refuse_retries_that_never_read_a_reply_body(retries: int) -> None:
    """pymodbus 3.7.4 reads a reply's body ``retries`` times. With 0: every read fails."""
    with pytest.raises(ValueError, match="never reads the body"):
        SerialSettings(port="p", retries=retries)


@pytest.mark.parametrize("port", ["", "   "])
def test_serial_settings_refuse_an_empty_port(port: str) -> None:
    with pytest.raises(ValueError, match="MOTOR_PORT"):
        SerialSettings(port=port)


def test_serial_settings_reject_a_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SerialSettings(port="p", timeout=Seconds(0.0))
    with pytest.raises(ValueError, match="must be positive"):
        SerialSettings(port="p", timeout=Seconds(-1.0))


def test_the_driver_refuses_tuning_that_would_verify_nothing(
    clock: ManualClock, bus: FakeBus
) -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        build_drive(clock, bus, failure_threshold=0)
    with pytest.raises(ValueError, match="settle_attempts"):
        build_drive(clock, bus, settle_attempts=0)
    # With zero attempts, close() would never read RFRD - so it could never
    # confirm standstill, and every close would leave the run command in place
    # on a machine that had in fact stopped.
    with pytest.raises(ValueError, match="stop_attempts"):
        build_drive(clock, bus, stop_attempts=0)


async def test_the_slave_address_is_on_every_transaction(clock: ManualClock, bus: FakeBus) -> None:
    """A wrong unit address talks confidently to a different drive on the bus."""
    drive = build_drive(clock, bus, settings=SerialSettings(port="p", slave_address=7))
    opened = await drive.open()
    assert isinstance(opened, Ok)
    assert [t.slave for t in bus.log] == [7]


# =========================================================================
# open()
# =========================================================================


async def test_open_proves_the_addressing_with_a_read_and_commands_nothing(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A wrong offset found by writing has already written a speed somewhere."""
    opened = await drive.open()
    assert isinstance(opened, Ok)
    assert bus.connect_calls == 1
    assert bus.writes() == []
    assert bus.reads() == [RegisterMap().eta]


async def test_open_reports_a_port_that_will_not_open(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.port_opens = False
    bus.latency = Seconds(0.12)
    match await drive.open():
        case Err(CommTimeout(after=waited)):
            assert waited == pytest.approx(0.12)
        case other:
            pytest.fail(f"expected a measured CommTimeout, got {other!r}")
    assert bus.log == []


async def test_open_reports_a_raising_connect(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.raise_on_connect = connection_exception()
    opened = await drive.open()
    assert isinstance(opened, Err)


async def test_open_reports_a_wrong_register_offset(clock: ManualClock, bus: FakeBus) -> None:
    """The drive is at offset 0; the driver is told -1, so ETA does not exist."""
    drive = build_drive(clock, bus, offset=-1)
    match await drive.open():
        case Err(BadResponse(detail=detail)):
            assert "exception code 2" in detail
            assert "offset" in detail
        case other:
            pytest.fail(f"expected BadResponse naming the offset, got {other!r}")


async def test_open_after_close_works(drive: ATV320Drive, bus: FakeBus) -> None:
    """close() shuts the executor down; open() must leave the object usable."""
    opened = await drive.open()
    assert isinstance(opened, Ok)
    closed = await drive.close()
    assert isinstance(closed, Ok)
    latched_by_close = drive.link_lost
    assert latched_by_close
    reopened = await drive.open()
    assert isinstance(reopened, Ok)
    latched_after_reopen = drive.link_lost
    assert not latched_after_reopen
    assert bus.connect_calls == 2


# =========================================================================
# The register offset
# =========================================================================


@pytest.mark.parametrize("offset", [0, -1, 1])
async def test_the_register_offset_shifts_every_address(
    clock: ManualClock, make_bus: BusFactory, offset: int
) -> None:
    bus = make_bus(offset=offset, eta=ETA_OPERATION_ENABLED)
    drive = build_drive(clock, bus, offset=offset)

    assert isinstance(await drive.read_status(), Ok)
    assert bus.reads() == [3201 + offset, 8602 + offset, 8604 + offset, 3204 + offset]

    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
    assert bus.writes() == [(8501 + offset, ControlWord.SHUTDOWN.value)]


@pytest.mark.parametrize("offset", [0, -1])
async def test_the_speed_setpoint_lands_at_the_offset_address(
    clock: ManualClock, make_bus: BusFactory, offset: int
) -> None:
    """The exact address written, asserted for offset 0 and -1."""
    bus = make_bus(offset=offset)
    drive = build_drive(clock, bus, offset=offset)
    assert isinstance(await drive.write_speed(MotorRpm(0)), Ok)
    assert bus.writes() == [(8602 + offset, 0)]
    assert bus.reads() == [8602 + offset]


# =========================================================================
# write_speed: signed handling and write-verify
# =========================================================================


@pytest.mark.parametrize(
    ("rpm", "expected_register"),
    [
        (0, 0x0000),
        (1, 0x0001),
        (-1, 0xFFFF),
        (1380, 0x0564),
        (-1380, 0xFA9C),
        (32767, 0x7FFF),
        (-32768, 0x8000),
    ],
)
async def test_write_speed_round_trips_signed_values(
    drive: ATV320Drive, bus: FakeBus, rpm: int, expected_register: int
) -> None:
    """-1 rpm must go out as 0xFFFF and come back as -1, never as 65535."""
    bus.accepted_leftover_setpoint = expected_register
    assert isinstance(await drive.write_speed(MotorRpm(rpm)), Ok)
    assert bus.setpoint() == expected_register

    match await drive.read_status():
        case Ok(status):
            assert status.setpoint_echo_rpm == rpm
        case other:
            pytest.fail(f"expected a status, got {other!r}")


@pytest.mark.parametrize("rpm", [32768, -32769, 100000])
async def test_write_speed_rejects_values_outside_signed_16_bit(
    drive: ATV320Drive, bus: FakeBus, rpm: int
) -> None:
    """Rejected at the boundary, and nothing reaches the wire."""
    match await drive.write_speed(MotorRpm(rpm)):
        case Err(OutOfRange(quantity=quantity)):
            assert quantity == "signed16"
        case other:
            pytest.fail(f"expected OutOfRange, got {other!r}")
    assert bus.log == []


async def test_write_speed_catches_a_disagreeing_echo(drive: ATV320Drive, bus: FakeBus) -> None:
    """The setpoint was acked and did not land: that is the failure to catch."""
    bus.script_writes.append(write_echo(RegisterMap().lfrd, 500))
    bus.script_reads.append(read_reply([123]))
    match await drive.write_speed(MotorRpm(500)):
        case Err(BadResponse(detail=detail)):
            assert "write-verify failed" in detail
            assert "500 rpm" in detail
            assert "123 rpm" in detail
        case other:
            pytest.fail(f"expected a write-verify BadResponse, got {other!r}")


async def test_write_verify_cannot_catch_a_uniform_offset_error(
    clock: ManualClock, make_bus: BusFactory
) -> None:
    """The documented limit of write-verify, pinned so nobody over-claims it.

    With the offset wrong by the same amount everywhere, the write and the
    read-back both go to the wrong register, so they agree and the check
    passes. What catches a uniform offset is the ETA read in open() and the
    exception responses from addresses the drive does not implement - and,
    finally, the bench.
    """
    bus = make_bus(offset=0)
    drive = build_drive(clock, bus, offset=1)
    bus.accepted_leftover_setpoint = 0

    assert isinstance(await drive.write_speed(MotorRpm(700)), Ok)
    assert bus.registers[8603] == 700, "the write landed one register along"
    assert bus.setpoint() == 0, "the real setpoint never moved"


async def test_write_speed_reports_a_value_the_drive_rejects(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.script_writes.append(exception_response(WRITE_SINGLE_REGISTER, ILLEGAL_DATA_VALUE))
    match await drive.write_speed(MotorRpm(900)):
        case Err(BadResponse(detail=detail)):
            assert "rejected a write" in detail
            assert "exception code 3" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_write_speed_propagates_a_failed_read_back(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.accepted_leftover_setpoint = 250
    bus.script_reads.append(io_exception())
    assert isinstance(await drive.write_speed(MotorRpm(250)), Err)


async def test_write_speed_refuses_once_the_link_is_latched(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    await latch_the_link(drive, bus)
    bus.log.clear()
    assert isinstance(await drive.write_speed(MotorRpm(400)), Err)
    assert bus.log == [], "a latched driver must put nothing on the wire"


# =========================================================================
# read_status
# =========================================================================


async def test_read_status_assembles_one_observation(drive: ATV320Drive, bus: FakeBus) -> None:
    regs = RegisterMap()
    bus.registers[regs.eta] = ETA_OPERATION_ENABLED
    bus.registers[regs.lfrd] = 1380
    bus.registers[regs.rfrd] = 1375
    bus.registers[regs.lcr] = 21
    bus.registers[regs.lft] = LFT_UNDERVOLTAGE  # historical; must NOT be read
    # Seeded, not commanded: this test observes a drive that is already
    # running, so the teardown check is told the setpoint is expected.
    bus.accepted_leftover_setpoint = 1380

    match await drive.read_status():
        case Ok(status):
            assert status.state is DriveState.OPERATION_ENABLED
            assert status.status_word == ETA_OPERATION_ENABLED
            assert status.setpoint_echo_rpm == 1380
            assert status.output_rpm == 1375
            assert status.current == pytest.approx(2.1)
            assert status.fault is None
            assert not status.fault_present
        case other:
            pytest.fail(f"expected a status, got {other!r}")
    assert regs.lft not in bus.reads(), "LFT was read on a healthy drive"


async def test_read_status_decodes_a_negative_output_speed(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.registers[RegisterMap().rfrd] = 0xFFFF
    match await drive.read_status():
        case Ok(status):
            assert status.output_rpm == -1
        case other:
            pytest.fail(f"expected a status, got {other!r}")


@pytest.mark.parametrize("eta", [ETA_FAULT, ETA_FAULT_WHILE_OPERATIONAL])
async def test_read_status_names_the_fault_only_when_faulted(
    drive: ATV320Drive, bus: FakeBus, eta: int
) -> None:
    regs = RegisterMap()
    bus.registers[regs.eta] = eta
    bus.registers[regs.lft] = LFT_UNDERVOLTAGE

    match await drive.read_status():
        case Ok(status):
            assert status.state is DriveState.FAULT
            assert status.fault_present
            assert status.fault is DriveFault.UNDERVOLTAGE
        case other:
            pytest.fail(f"expected a faulted status, got {other!r}")
    assert bus.reads()[-1] == regs.lft


async def test_read_status_reports_an_unknown_fault_code(drive: ATV320Drive, bus: FakeBus) -> None:
    regs = RegisterMap()
    bus.registers[regs.eta] = ETA_FAULT
    bus.registers[regs.lft] = 999
    match await drive.read_status():
        case Ok(status):
            assert status.fault is DriveFault.UNKNOWN
        case other:
            pytest.fail(f"expected a faulted status, got {other!r}")


async def test_read_status_stops_at_the_first_failed_read(drive: ATV320Drive, bus: FakeBus) -> None:
    """Four timeouts would spend the whole control budget learning one thing."""
    bus.script_reads.append(io_exception())
    assert isinstance(await drive.read_status(), Err)
    assert len(bus.reads()) == 1


async def test_read_status_propagates_a_failed_fault_read(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.registers[RegisterMap().eta] = ETA_FAULT
    bus.script_reads.extend([Behave.NORMALLY] * 4)
    bus.script_reads.append(io_exception())
    assert isinstance(await drive.read_status(), Err)


async def test_read_status_refuses_once_the_link_is_latched(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    await latch_the_link(drive, bus)
    bus.log.clear()
    match await drive.read_status():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected CommTimeout, got {other!r}")
    assert bus.log == []


# =========================================================================
# read_limits: tFr / HSP / LSP / ACC / dEC, read only
# =========================================================================


def seed_bench_limits(bus: FakeBus) -> None:
    """The five values the bench read today, at the bus's own addressing."""
    regs = bus.regs
    bus.registers[regs.tfr] = 600
    bus.registers[regs.hsp] = 500
    bus.registers[regs.lsp] = 0
    bus.registers[regs.acc] = 30
    bus.registers[regs.dec] = 30


async def test_read_limits_reads_the_five_parameters_and_writes_nothing(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    seed_bench_limits(bus)
    outcome = await drive.read_limits()
    assert outcome == Ok(
        DriveLimits(
            max_frequency=Hertz(60.0),
            high_speed=Hertz(50.0),
            low_speed=Hertz(0.0),
            acceleration=Seconds(3.0),
            deceleration=Seconds(3.0),
        )
    )
    assert bus.reads() == [3103, 3104, 3105, 9001, 9002]
    assert bus.writes() == []


async def test_read_limits_uses_the_register_offset(
    clock: ManualClock, make_bus: BusFactory
) -> None:
    bus = make_bus(offset=-1)
    seed_bench_limits(bus)
    drive = build_drive(clock, bus, offset=-1)
    assert isinstance(await drive.read_limits(), Ok)
    assert bus.reads() == [3102, 3103, 3104, 9000, 9001]


async def test_read_limits_stops_at_the_first_failed_read(drive: ATV320Drive, bus: FakeBus) -> None:
    seed_bench_limits(bus)
    bus.script_reads.extend([Behave.NORMALLY, io_exception()])
    assert isinstance(await drive.read_limits(), Err)
    assert len(bus.reads()) == 2


async def test_read_limits_reports_a_parameter_the_drive_does_not_have(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Unseeded addresses answer exception code 2, as a wrong offset would."""
    assert bus.regs.tfr not in bus.registers
    match await drive.read_limits():
        case Err(BadResponse(detail=detail)):
            assert "3103" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_read_limits_refuses_once_the_link_is_latched(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    await latch_the_link(drive, bus)
    bus.log.clear()
    assert isinstance(await drive.read_limits(), Err)
    assert bus.log == []


# =========================================================================
# read_register: one register, for operator tooling
# =========================================================================


async def test_read_register_reads_one_register_and_writes_nothing(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.registers[bus.regs.eta] = 0x0250
    assert await drive.read_register(bus.regs.eta) == Ok(0x0250)
    assert bus.reads() == [3201]
    assert bus.writes() == []


async def test_read_register_reports_a_failed_read(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.script_reads.append(io_exception())
    match await drive.read_register(bus.regs.eta):
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected CommTimeout, got {other!r}")


async def test_read_register_refuses_once_the_link_is_latched(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    await latch_the_link(drive, bus)
    bus.log.clear()
    assert isinstance(await drive.read_register(bus.regs.eta), Err)
    assert bus.log == []


# =========================================================================
# Malformed replies
# =========================================================================


async def test_a_short_read_is_a_bad_response(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.script_reads.append(read_reply([]))
    match await drive.read_status():
        case Err(BadResponse(detail=detail)):
            assert "expected exactly 1 register" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_a_reply_with_no_register_block_is_a_bad_response(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A write echo decoded as the answer to a read. ModbusPDU promises
    ``registers: list[int]`` and never assigns it, so this raises."""
    bus.script_reads.append(write_echo(8602, 0))
    match await drive.read_status():
        case Err(BadResponse(detail=detail)):
            assert "carries no register data" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


@pytest.mark.parametrize("reply", [None, "OK", 3])
async def test_a_reply_that_is_not_a_pdu_is_a_bad_response(
    drive: ATV320Drive, bus: FakeBus, reply: object
) -> None:
    """pymodbus returns None when a caller sets no_response_expected."""
    bus.script_reads.append(reply)
    match await drive.read_status():
        case Err(BadResponse(detail=detail)):
            assert "neither a Modbus PDU" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_a_non_pdu_write_reply_is_a_bad_response(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.script_writes.append(None)
    match await drive.write_command(ControlWord.SHUTDOWN):
        case Err(BadResponse(detail=detail)):
            assert "neither a Modbus PDU" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_a_write_is_not_acked_by_somebody_elses_reply(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A read response is a perfectly good PDU, and it is not an acknowledgement.

    On a noisy half-duplex pair the framer can match a stale READ reply to this
    write's exchange. Accepting any ModbusPDU - which is what "is it a PDU?"
    amounts to - reports "the write was acked" on the strength of somebody
    else's answer, which is precisely the assumption the whole write-verify
    argument in this driver rests on not being true.
    """
    bus.script_writes.append(read_reply([0]))
    match await drive.write_command(ControlWord.SHUTDOWN):
        case Err(BadResponse(detail=detail)):
            assert "function code 3" in detail
            assert "NOT acknowledged" in detail
        case other:
            pytest.fail(f"expected BadResponse, got {other!r}")


async def test_a_genuine_write_echo_is_accepted(drive: ATV320Drive, bus: FakeBus) -> None:
    """The other half of the check: function code 6 is what a write ack IS.

    Without this the shape validation could be satisfied by refusing
    everything, which would be a driver that can never write.
    """
    bus.script_writes.append(write_echo(RegisterMap().cmd, ControlWord.SHUTDOWN.value))
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)


async def test_a_register_outside_the_16_bit_domain_is_out_of_range(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Parsed at the boundary, so nothing downstream has to check again."""
    bus.script_reads.append(read_reply([0x1FFFF]))
    match await drive.read_status():
        case Err(OutOfRange(quantity=quantity)):
            assert quantity == "register"
        case other:
            pytest.fail(f"expected OutOfRange, got {other!r}")


async def test_a_returned_io_exception_is_a_comm_timeout(drive: ATV320Drive, bus: FakeBus) -> None:
    """pymodbus 3.7.4 RETURNS this object where it promises a PDU."""
    bus.latency = Seconds(0.31)
    bus.script_reads.append(Returned(io_exception()))
    match await drive.read_status():
        case Err(CommTimeout(after=waited)):
            assert waited == pytest.approx(0.31)
        case other:
            pytest.fail(f"expected a measured CommTimeout, got {other!r}")


async def test_a_returned_io_exception_on_a_write_is_a_comm_timeout(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.script_writes.append(Returned(io_exception()))
    match await drive.write_command(ControlWord.SHUTDOWN):
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected CommTimeout, got {other!r}")


# =========================================================================
# The error-mapping table
# =========================================================================


def test_pyserial_errors_are_os_errors() -> None:
    """Why this module catches OSError and never imports serial for the mapping."""
    assert issubclass(serial.SerialException, OSError)
    assert not issubclass(ModbusIOException, OSError)


@pytest.mark.parametrize(
    ("raised", "expect_timeout", "fragment"),
    [
        (connection_exception(), True, ""),
        (io_exception(), True, ""),
        (serial.SerialException("device disappeared"), True, ""),
        (OSError("cable"), True, ""),
        (parameter_exception(), False, "pymodbus rejected the transaction"),
        (ValueError("bad count"), False, "unexpected ValueError"),
    ],
)
async def test_every_transport_failure_maps_onto_the_closed_union(
    drive: ATV320Drive,
    bus: FakeBus,
    raised: BaseException,
    expect_timeout: bool,
    fragment: str,
) -> None:
    bus.latency = Seconds(0.05)
    bus.script_reads.append(raised)
    match await drive.read_status():
        case Err(CommTimeout(after=waited)):
            assert expect_timeout, f"{raised!r} should not have been a CommTimeout"
            assert waited == pytest.approx(0.05)
        case Err(BadResponse(detail=detail)):
            assert not expect_timeout, f"{raised!r} should have been a CommTimeout"
            assert fragment in detail
        case other:
            pytest.fail(f"expected a mapped DriveError, got {other!r}")


def error_of[T](outcome: Result[T, DriveError]) -> DriveError:
    """The error from a result that was supposed to fail."""
    error = err_of(outcome)
    assert error is not None, f"expected a failure, got {outcome!r}"
    return error


def closed_union_members() -> frozenset[object]:
    """The variants of the ``DriveError`` alias, as plain objects.

    ``typing.get_args`` is annotated ``tuple[Any, ...]``, so the untyped value
    is narrowed to ``object`` once, right here - rule 6 applied to a test -
    instead of leaking through the comparison below.
    """
    members: tuple[object, ...] = get_args(DriveError.__value__)  # pyright: ignore[reportAny]
    return frozenset(members)


async def test_every_variant_of_the_closed_error_union_is_reachable(
    clock: ManualClock, make_bus: BusFactory
) -> None:
    """Each DriveError produced by a real path through this driver.

    A variant nothing here can return is either dead weight in the union or a
    failure mode the driver silently folds into another one. Adding a variant
    to ``DriveError`` fails this test, which is the point: the new mode has to
    be given a path or argued away on purpose.
    """
    produced: set[type[object]] = set()

    # CommTimeout - the drive went quiet.
    bus = make_bus()
    bus.script_reads.append(Returned(io_exception()))
    produced.add(type(error_of(await build_drive(clock, bus).read_status())))

    # BadResponse - it answered, and the answer made no sense.
    bus = make_bus()
    bus.script_reads.append(read_reply([]))
    produced.add(type(error_of(await build_drive(clock, bus).read_status())))

    # OutOfRange - a setpoint that does not fit the signed-16-bit wire format.
    bus = make_bus()
    produced.add(type(error_of(await build_drive(clock, bus).write_speed(MotorRpm(40000)))))

    # UnexpectedState - it did not follow the start sequence.
    bus = make_bus()
    bus.obeys = frozenset()
    produced.add(type(error_of(await build_drive(clock, bus).enable())))

    # DriveFaulted - it cannot follow it, and here is the mnemonic.
    bus = make_bus(eta=ETA_FAULT)
    bus.registers[RegisterMap().lft] = LFT_OVERCURRENT
    produced.add(type(error_of(await build_drive(clock, bus).enable())))

    # EnableUnconfirmed - the enabling word went out and then the link failed,
    # so the output stage may be live. Not a transport diagnosis: a fact about
    # the motor.
    bus = make_bus()
    bus.script_reads.extend([Behave.NORMALLY, Behave.NORMALLY, Behave.NORMALLY, io_exception()])
    produced.add(type(error_of(await build_drive(clock, bus).enable())))

    # StopUnconfirmed - the reference was zeroed, the shaft never reported
    # standstill, and the run command was deliberately left in place.
    bus = make_bus()
    bus.registers[RegisterMap().rfrd] = 600
    produced.add(type(error_of(await build_drive(clock, bus).close())))

    assert produced == closed_union_members()


# =========================================================================
# The comms-failure policy
# =========================================================================


async def latch_the_link(drive: ATV320Drive, bus: FakeBus) -> None:
    """Drive the link into its latched-down state through the public API."""
    bus.sticky_reads = io_exception()
    bus.sticky_writes = io_exception()
    for _ in range(3):
        assert isinstance(await drive.read_status(), Err)
    assert drive.link_lost
    bus.sticky_reads = Behave.NORMALLY
    bus.sticky_writes = Behave.NORMALLY


async def test_consecutive_failures_latch_the_link_and_stop_the_writing(
    clock: ManualClock, bus: FakeBus
) -> None:
    """Below the threshold the caller keeps the specific diagnosis; at it, the
    link is declared lost and this driver sends nothing more."""
    drive = build_drive(clock, bus, failure_threshold=3)
    bus.latency = Seconds(0.1)
    bus.sticky_reads = parameter_exception()

    for attempt in (1, 2):
        match await drive.read_status():
            case Err(BadResponse()):
                assert not drive.link_lost, f"latched too early, on attempt {attempt}"
            case other:
                pytest.fail(f"expected the specific diagnosis, got {other!r}")

    match await drive.read_status():
        case Err(CommTimeout(after=waited)):
            # Measured from the first OBSERVED failure of the run to this one,
            # i.e. two transaction latencies across three failures - not the
            # configured 0.3 s budget, and not just this attempt.
            assert waited == pytest.approx(0.2)
        case other:
            pytest.fail(f"expected a summarising CommTimeout, got {other!r}")

    assert drive.link_lost
    bus.sticky_reads = Behave.NORMALLY
    bus.log.clear()
    assert isinstance(await drive.write_speed(MotorRpm(600)), Err)
    assert isinstance(await drive.write_command(ControlWord.ENABLE_OPERATION), Err)
    assert isinstance(await drive.enable(), Err)
    assert bus.log == [], "a latched driver must stop writing, healthy bus or not"


async def test_a_success_ends_the_failure_run(clock: ManualClock, bus: FakeBus) -> None:
    drive = build_drive(clock, bus, failure_threshold=3)
    for _ in range(2):
        bus.script_reads.append(io_exception())
        assert isinstance(await drive.read_status(), Err)
    assert isinstance(await drive.read_status(), Ok)
    for _ in range(2):
        bus.script_reads.append(io_exception())
        assert isinstance(await drive.read_status(), Err)
    assert not drive.link_lost, "two failures either side of a success are not five"


async def test_a_link_that_heals_itself_is_not_permission_to_resume(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Only open() clears the latch. There is no automatic resumption of motion."""
    await latch_the_link(drive, bus)
    assert isinstance(await drive.read_status(), Err)
    still_latched = drive.link_lost
    assert still_latched, "a healthy bus is not permission to resume"
    opened = await drive.open()
    assert isinstance(opened, Ok)
    latched_after_reopen = drive.link_lost
    assert not latched_after_reopen
    assert isinstance(await drive.read_status(), Ok)


async def test_one_failure_is_enough_at_a_threshold_of_one(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus, failure_threshold=1)
    bus.script_reads.append(io_exception())
    match await drive.read_status():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected CommTimeout, got {other!r}")
    assert drive.link_lost


# =========================================================================
# enable(): the CiA402 sequence
# =========================================================================


async def test_enable_walks_the_sequence_and_verifies_eta_between_steps(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    regs = RegisterMap()
    assert isinstance(await drive.enable(), Ok)
    assert bus.command_words() == [6, 7, 15]
    assert bus.registers[regs.eta] == ETA_OPERATION_ENABLED
    # One ETA read before the first word (to refuse a faulted drive) and one
    # after each of the three, so the sequence is never written blind.
    assert bus.reads() == [regs.eta] * 4
    assert bus.log[0].access is Access.READ


async def test_enable_writes_no_speed_of_its_own(drive: ATV320Drive, bus: FakeBus) -> None:
    """Pinned, because it is a deliberate omission rather than an oversight.

    Whether a non-zero LFRD left over from an earlier session should block a
    start is policy - it depends on whether somebody is in the machine - so the
    safety layer must write speed 0 before enabling. If that ever moves in
    here, this test is the thing that has to be changed on purpose.
    """
    assert isinstance(await drive.enable(), Ok)
    assert RegisterMap().lfrd not in [address for address, _ in bus.writes()]


@pytest.mark.parametrize(
    ("stalls_at", "expected"),
    [
        (ControlWord.SHUTDOWN, DriveState.READY),
        (ControlWord.SWITCH_ON, DriveState.SWITCHED_ON),
    ],
)
async def test_enable_fails_at_whichever_step_the_drive_does_not_follow(
    drive: ATV320Drive, bus: FakeBus, stalls_at: ControlWord, expected: DriveState
) -> None:
    """A drive that did not follow must be distinguishable from one that did.

    The two words that cannot energise the output stage: whatever happened, the
    motor is not now commanded, so the transport-level diagnosis is the whole
    story. The energising word gets its own tests below, because there the same
    error would be understating things badly.
    """
    bus.obeys = frozenset(CIA402_TRANSITIONS) - {stalls_at}
    match await drive.enable():
        case Err(UnexpectedState(expected=want, actual=got)):
            assert want is expected
            assert got is not expected
        case other:
            pytest.fail(f"expected UnexpectedState at {stalls_at.name}, got {other!r}")
    # Nothing is sent after the step that failed.
    assert bus.command_words()[-1] == stalls_at.value


async def test_enable_that_fails_at_the_energising_word_says_the_motor_may_be_running(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """The defect: Err while the output stage is live, reported as a link problem.

    Demonstrated on the previous version: ``enable()`` returned
    ``Err(CommTimeout)`` with ``[6, 7, 15]`` all on the wire and ETA decoding to
    OPERATION_ENABLED. ``CommTimeout`` reads as "the link died", not "the motor
    is now enabled", and LFRD may still hold a speed from an earlier session -
    so the caller is told to worry about comms while the machine spins up.

    A failure at the word that energises the output stage therefore has to be
    its own variant, and the driver has to try to undo what it may have done.
    """
    regs = RegisterMap()
    bus.script_reads.extend([Behave.NORMALLY, Behave.NORMALLY, Behave.NORMALLY, io_exception()])

    match await drive.enable():
        case Err(
            EnableUnconfirmed(
                detail=detail,
                reference_zeroed=zeroed,
                run_command_removed=removed,
            )
        ):
            assert zeroed, "the rollback zeroed LFRD"
            assert removed, "and removed the run command"
            assert "may be live" in detail
            assert "CommTimeout" in detail, "the underlying failure is still named"
        case other:
            pytest.fail(f"expected EnableUnconfirmed, got {other!r}")

    # ETA really did reach OPERATION_ENABLED before the failure: this is the
    # "reported failure while the motor runs" case and not a hypothetical.
    assert bus.command_words()[:3] == [6, 7, 15]
    # The rollback, in the order that cannot freewheel: zero the reference, then
    # transition 5 (which ramps). NOT SHUTDOWN, which would drop the output
    # stage on a machine that may already be turning.
    assert bus.writes()[-2:] == [(regs.lfrd, 0), (regs.cmd, ControlWord.SWITCH_ON.value)]
    assert ControlWord.SHUTDOWN.value not in bus.command_words()[3:]


async def test_an_unconfirmed_enable_reports_a_rollback_that_failed_too(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Both booleans False is the worst case, and it has to be machine-readable.

    Nothing is known to have undone the enable, so the drive's own ttO timeout
    is the only stop left. A caller has to be able to branch on that without
    parsing prose.
    """
    bus.script_reads.extend([Behave.NORMALLY, Behave.NORMALLY, Behave.NORMALLY, io_exception()])
    # The three command words land; only the rollback writes fail.
    bus.script_writes.extend([Behave.NORMALLY, Behave.NORMALLY, Behave.NORMALLY])
    bus.sticky_writes = io_exception()

    match await drive.enable():
        case Err(EnableUnconfirmed(reference_zeroed=zeroed, run_command_removed=removed)):
            assert not zeroed
            assert not removed
        case other:
            pytest.fail(f"expected EnableUnconfirmed, got {other!r}")
    assert bus.command_words()[:3] == [6, 7, 15], "the enable really did go out"


async def test_an_unconfirmed_enable_can_still_be_stopped_by_hand(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """The second half of the defect: a latched link that refuses the stop.

    The failed reads inside ``enable()`` count towards the failure threshold, so
    a run of them latches the link - and the refusal then blocked the very
    ``write_command(SHUTDOWN)`` a caller would reach for to undo the enable. A
    refusal must never block a stop.
    """
    bus.sticky_reads = io_exception()
    for _ in range(3):
        assert isinstance(await drive.enable(), Err)
    assert drive.link_lost

    bus.sticky_reads = Behave.NORMALLY
    bus.sticky_writes = Behave.NORMALLY
    bus.log.clear()

    assert isinstance(await drive.write_speed(MotorRpm(0)), Ok), "a zero setpoint is a stop"
    assert isinstance(await drive.write_command(ControlWord.SWITCH_ON), Ok)
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
    assert bus.command_words() == [ControlWord.SWITCH_ON.value, ControlWord.SHUTDOWN.value]

    # And nothing else got through with it.
    assert isinstance(await drive.write_command(ControlWord.ENABLE_OPERATION), Err)
    assert isinstance(await drive.write_command(ControlWord.FAULT_RESET), Err)
    assert isinstance(await drive.write_speed(MotorRpm(600)), Err)
    assert isinstance(await drive.enable(), Err)


async def test_enable_refuses_a_faulted_drive_and_names_the_fault(
    clock: ManualClock, make_bus: BusFactory
) -> None:
    bus = make_bus(eta=ETA_FAULT)
    bus.registers[RegisterMap().lft] = LFT_OVERCURRENT
    drive = build_drive(clock, bus)

    match await drive.enable():
        case Err(DriveFaulted(fault=fault, raw_code=code)):
            assert fault is DriveFault.OVERCURRENT
            assert code == LFT_OVERCURRENT
        case other:
            pytest.fail(f"expected DriveFaulted, got {other!r}")
    assert bus.command_words() == [], "not one command word to a faulted drive"


async def test_enable_reports_a_fault_that_appears_mid_sequence(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.on_command[ControlWord.SWITCH_ON] = ETA_FAULT
    bus.registers[RegisterMap().lft] = LFT_OVERCURRENT
    match await drive.enable():
        case Err(DriveFaulted(fault=fault)):
            assert fault is DriveFault.OVERCURRENT
        case other:
            pytest.fail(f"expected DriveFaulted, got {other!r}")
    assert bus.command_words() == [6, 7], "ENABLE_OPERATION must not follow a fault"


async def test_enable_reports_the_link_when_the_fault_code_cannot_be_read(
    clock: ManualClock, make_bus: BusFactory
) -> None:
    """Inventing a fault name would be worse than reporting the dead link."""
    bus = make_bus(eta=ETA_FAULT)
    drive = build_drive(clock, bus)
    bus.script_reads.append(Behave.NORMALLY)
    bus.script_reads.append(io_exception())
    match await drive.enable():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected CommTimeout, got {other!r}")


async def test_enable_propagates_a_failed_command_write(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.script_writes.append(io_exception())
    assert isinstance(await drive.enable(), Err)
    assert bus.command_words() == [6]


async def test_enable_propagates_a_failed_eta_read(drive: ATV320Drive, bus: FakeBus) -> None:
    bus.script_reads.append(io_exception())
    assert isinstance(await drive.enable(), Err)
    assert bus.command_words() == []


async def test_enable_reports_an_eta_read_that_fails_after_a_command_word(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """The write landed; the verification read did not come back.

    Distinct from a pre-check failure: the command word is already on the
    drive, so the caller must not read this as "nothing happened".
    """
    bus.script_reads.extend([Behave.NORMALLY, io_exception()])
    assert isinstance(await drive.enable(), Err)
    assert bus.command_words() == [6], "the sequence stops at the unverified step"


async def test_enable_reports_an_eta_read_that_fails_while_settling(
    clock: ManualClock, bus: FakeBus
) -> None:
    """The link dies between the settle attempts."""
    drive = build_drive(clock, bus, settle_attempts=3)
    bus.script_reads.extend(
        [
            Behave.NORMALLY,  # pre-check
            read_reply([ETA_SWITCH_ON_DISABLED]),  # not settled yet
            io_exception(),  # and now the link is gone
        ]
    )
    assert isinstance(await drive.enable(), Err)
    assert bus.command_words() == [6]


async def test_enable_lets_the_drive_settle_before_giving_up(
    clock: ManualClock, bus: FakeBus
) -> None:
    """A command word takes effect on the drive's next scan, so one read is
    not evidence the drive refused."""
    drive = build_drive(clock, bus, settle_attempts=3)
    regs = RegisterMap()
    # Pre-check reads SWITCH_ON_DISABLED; after SHUTDOWN the first read still
    # shows the old word, the second shows READY.
    bus.script_reads.extend([Behave.NORMALLY, read_reply([ETA_SWITCH_ON_DISABLED])])

    assert isinstance(await drive.enable(), Ok)
    assert bus.command_words() == [6, 7, 15]
    assert bus.registers[regs.eta] == ETA_OPERATION_ENABLED


async def test_enable_gives_up_after_the_allowed_settle_attempts(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus, settle_attempts=3)
    bus.obeys = frozenset()
    match await drive.enable():
        case Err(UnexpectedState(expected=want, actual=got)):
            assert want is DriveState.READY
            assert got is DriveState.SWITCH_ON_DISABLED
        case other:
            pytest.fail(f"expected UnexpectedState, got {other!r}")
    # One pre-check read plus exactly three attempts for the one step tried.
    assert len(bus.reads()) == 4


async def test_enable_does_not_wait_out_a_fault_that_appears_while_settling(
    clock: ManualClock, bus: FakeBus
) -> None:
    """Sleeping through a fault reaction is sleeping while a loaded centrifuge
    decelerates."""
    drive = build_drive(clock, bus, settle_attempts=5)
    bus.on_command[ControlWord.SHUTDOWN] = ETA_FAULT
    bus.registers[RegisterMap().lft] = LFT_UNDERVOLTAGE
    match await drive.enable():
        case Err(DriveFaulted(fault=fault)):
            assert fault is DriveFault.UNDERVOLTAGE
        case other:
            pytest.fail(f"expected DriveFaulted, got {other!r}")
    # Pre-check, one read that saw the fault, one LFT read. No settling.
    assert len(bus.reads()) == 3


# =========================================================================
# close()
# =========================================================================


async def test_close_ramps_to_a_stop_before_it_drops_the_output_stage(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """The order is the fix: zero the reference, WAIT, then remove the command.

    CMD = SHUTDOWN out of OPERATION_ENABLED is CiA402 transition 8, which drops
    the output stage and leaves a loaded centrifuge freewheeling for minutes
    while the drive reports READY. The previous version wrote it about 20 ms
    after zeroing LFRD, so the ramp had no time to act and was then aborted.

    So the transaction log itself is the assertion: LFRD = 0, then a READ of
    RFRD proving standstill was checked and not assumed, and only then the
    documented stop pair 7 -> 6.
    """
    regs = RegisterMap()
    assert isinstance(await drive.enable(), Ok)
    bus.log.clear()
    closed = await drive.close()
    assert isinstance(closed, Ok)

    assert [(t.access, t.address, t.value) for t in bus.log] == [
        (Access.WRITE, regs.lfrd, 0),
        (Access.READ, regs.rfrd, None),
        (Access.WRITE, regs.cmd, ControlWord.SWITCH_ON.value),
        (Access.WRITE, regs.cmd, ControlWord.SHUTDOWN.value),
    ]
    assert bus.close_calls == 1
    assert drive.link_lost


async def test_close_leaves_the_run_command_in_place_while_the_shaft_still_turns(
    clock: ManualClock, bus: FakeBus
) -> None:
    """The whole point of waiting: never drop the output stage on a moving load.

    The fake drive reports 600 rpm on RFRD and keeps reporting it, so standstill
    is never confirmed. A stop that cannot be confirmed must end with the drive
    still enabled and a zero reference - ttO's ramp - and must say so, rather
    than writing the word that turns a 10 s ramp into a 145 s coast.
    """
    regs = RegisterMap()
    drive = build_drive(clock, bus, stop_attempts=3)
    bus.latency = Seconds(0.1)
    assert isinstance(await drive.enable(), Ok)
    bus.registers[regs.rfrd] = 600
    bus.log.clear()

    match await drive.close():
        case Err(StopUnconfirmed(waited=waited, last_output_rpm=rpm, detail=detail)):
            assert rpm == MotorRpm(600)
            assert waited == pytest.approx(0.3)  # three RFRD reads at 0.1 s each
            assert "left in place" in detail
        case other:
            pytest.fail(f"expected StopUnconfirmed, got {other!r}")

    assert bus.writes() == [(regs.lfrd, 0)], "no command word went out"
    assert ControlWord.SHUTDOWN.value not in bus.command_words()
    assert bus.reads() == [regs.rfrd] * 3
    assert bus.close_calls == 1


async def test_close_stops_as_soon_as_the_shaft_has_stopped(
    clock: ManualClock, bus: FakeBus
) -> None:
    """The poll is bounded but it is not a fixed wait: it ends on the evidence.

    Twenty attempts are allowed and three are used, because the third RFRD read
    is the one that says zero. A close that always spent its whole budget would
    make every session end twenty polls late.
    """
    drive = build_drive(clock, bus, stop_attempts=20)
    assert isinstance(await drive.enable(), Ok)
    bus.log.clear()
    # Still turning, still turning, stopped - then the default behaviour, which
    # reads the fake's RFRD register of 0.
    bus.script_reads.extend([read_reply([600]), read_reply([600])])

    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert len(bus.reads()) == 3, "it stopped polling the moment RFRD read zero"
    assert bus.command_words() == [ControlWord.SWITCH_ON.value, ControlWord.SHUTDOWN.value]


async def test_close_treats_one_rpm_as_stopped(clock: ManualClock, bus: FakeBus) -> None:
    """1 rpm is the finest speed RFRD can report, so it is the standstill floor.

    Without this, a shaft resting at the drive's own resolution limit would look
    like a machine that never stops, and every close would refuse to finish.
    """
    regs = RegisterMap()
    drive = build_drive(clock, bus)
    assert isinstance(await drive.enable(), Ok)
    bus.registers[regs.rfrd] = 0xFFFF  # -1 rpm, signed
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert bus.command_words()[-2:] == [ControlWord.SWITCH_ON.value, ControlWord.SHUTDOWN.value]


async def test_close_reports_a_failure_to_zero_the_reference_and_writes_no_command(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A reference that may not be zero is the worst moment to remove torque.

    If LFRD = 0 did not land, the drive may still be holding a speed. Writing
    the stop pair then risks transition 8 against an unknown speed, so the run
    command stays and ttO takes over.
    """
    regs = RegisterMap()
    bus.script_writes.append(io_exception())
    match await drive.close():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected the first failure to be reported, got {other!r}")
    assert bus.writes() == [(regs.lfrd, 0)]
    assert bus.reads() == [], "there is no point polling a link that just failed"
    assert bus.close_calls == 1


async def test_close_reports_a_failed_standstill_read(drive: ATV320Drive, bus: FakeBus) -> None:
    """A link that died mid-poll is the more urgent diagnosis, so it wins."""
    bus.script_reads.append(io_exception())
    match await drive.close():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected the transport failure, got {other!r}")
    assert ControlWord.SHUTDOWN.value not in bus.command_words()
    assert bus.close_calls == 1


async def test_close_reports_a_failure_to_remove_the_run_command(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Both words are attempted: the shaft is already stopped, so 6 is safe."""
    regs = RegisterMap()
    bus.script_writes.extend([Behave.NORMALLY, io_exception()])
    closed = await drive.close()
    assert isinstance(closed, Err)
    assert bus.writes() == [
        (regs.lfrd, 0),
        (regs.cmd, ControlWord.SWITCH_ON.value),
        (regs.cmd, ControlWord.SHUTDOWN.value),
    ]
    assert bus.close_calls == 1


async def test_close_reports_the_first_of_two_failed_command_words(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    bus.script_writes.extend([Behave.NORMALLY, Behave.NORMALLY, io_exception()])
    match await drive.close():
        case Err(CommTimeout()):
            pass
        case other:
            pytest.fail(f"expected the failed SHUTDOWN to be reported, got {other!r}")


async def test_close_releases_the_port_even_if_releasing_it_raises(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A driver that will not let go of a port because close() threw is a
    driver nobody can restart."""
    bus.raise_on_close = OSError("handle already gone")
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert bus.close_calls == 1
    assert drive.link_lost


async def test_close_on_a_latched_link_still_attempts_the_stop(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """A refusal must never block a stop.

    The comms latch exists so this driver stops feeding a keepalive it cannot
    verify - not so that a motor stays commanded. It used to return the refusal
    and write nothing, which meant a link latched by a failed enable could not
    be stopped by the very close the caller reached for.
    """
    regs = RegisterMap()
    await latch_the_link(drive, bus)
    bus.log.clear()
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert bus.writes() == [
        (regs.lfrd, 0),
        (regs.cmd, ControlWord.SWITCH_ON.value),
        (regs.cmd, ControlWord.SHUTDOWN.value),
    ]
    assert bus.close_calls == 1


async def test_close_is_idempotent_and_does_not_raise_the_second_time(
    drive: ATV320Drive, bus: FakeBus
) -> None:
    """Teardown paths call close twice: an except branch, then a finally.

    The first call shuts the executor pool down, so the second used to raise
    ``RuntimeError: cannot schedule new futures after shutdown`` - out of a
    shutdown handler, which is where an exception has nobody left to catch it.
    """
    closed = await drive.close()
    assert isinstance(closed, Ok)
    transactions = len(bus.log)

    closed = await drive.close()
    assert isinstance(closed, Ok)
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert len(bus.log) == transactions, "a second close must not touch the wire"
    assert bus.close_calls == 1


async def test_a_second_close_repeats_the_first_verdict(drive: ATV320Drive, bus: FakeBus) -> None:
    """Idempotent is not the same as cheerful.

    If the first close could not stop the machine, the second must not report
    that it did. A caller that retries a close is asking the same question, and
    the answer has not changed.
    """
    bus.script_writes.append(io_exception())
    first = err_of(await drive.close())
    assert isinstance(first, CommTimeout)
    second = err_of(await drive.close())
    assert isinstance(second, CommTimeout)
    assert second == first


async def test_reopening_after_a_close_clears_the_close(drive: ATV320Drive, bus: FakeBus) -> None:
    """open() is the one place any latch is cleared, this one included."""
    closed = await drive.close()
    assert isinstance(closed, Ok)
    opened = await drive.open()
    assert isinstance(opened, Ok)
    bus.log.clear()
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert bus.writes()[0] == (RegisterMap().lfrd, 0), "the stop was attempted again"


# =========================================================================
# emergency_disable_blocking
# =========================================================================


def test_emergency_disable_zeroes_the_reference_and_leaves_the_run_command(
    clock: ManualClock, bus: FakeBus
) -> None:
    """A synchronous test, on purpose: this has to work from atexit and from an
    OS signal handler, where there may be no loop and await does not exist.

    And it writes LFRD = 0 and **nothing else**. Removing the run command here
    would be CiA402 transition 8 - the output stage dropped on a machine this
    call cannot afford to wait for - whereas a zeroed reference leaves the drive
    ramping on its own dEC and arms ttO to finish it. The absence of any CMD
    write is the assertion.
    """
    drive = build_drive(clock, bus)
    regs = RegisterMap()
    bus.registers[regs.lfrd] = 1380

    outcome = drive.emergency_disable_blocking(Seconds(1.0))

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert bus.writes() == [(regs.lfrd, 0)]
    assert bus.command_words() == [], "the run command must stay in place"
    assert bus.setpoint() == 0
    assert drive.link_lost, "the async side must not re-command a speed afterwards"


def test_emergency_disable_returns_when_the_transport_is_dead(
    clock: ManualClock, bus: FakeBus
) -> None:
    """Reaching the end of this test IS the assertion: it must not hang.

    And the outcome has to be reportable, because an atexit path that cannot
    tell "sent" from "not sent" cannot escalate.
    """
    drive = build_drive(clock, bus)
    bus.sticky_writes = serial.SerialException("adapter unplugged")

    outcome = drive.emergency_disable_blocking(Seconds(1.0))

    assert outcome is EmergencyStopOutcome.SENT_UNCONFIRMED
    assert len(bus.writes()) == 1
    assert drive.link_lost


def test_emergency_disable_reports_a_transport_that_carried_nothing(
    clock: ManualClock, bus: FakeBus
) -> None:
    """NOTHING_SENT is the member that exists to be escalated on.

    pymodbus raises ConnectionException when the port will not open, so no frame
    reached the drive and it is still commanded at whatever setpoint it held.
    Before the method returned anything, this was indistinguishable from success.
    """
    drive = build_drive(clock, bus)
    bus.registers[RegisterMap().lfrd] = 1380
    bus.accepted_leftover_setpoint = 1380
    bus.sticky_writes = connection_exception()

    assert drive.emergency_disable_blocking(Seconds(1.0)) is EmergencyStopOutcome.NOTHING_SENT
    assert bus.setpoint() == 1380, "nothing changed, and the caller is told so"


def test_emergency_disable_reports_a_reply_it_cannot_use(clock: ManualClock, bus: FakeBus) -> None:
    """The reply is classified, never acted on: no retry, no second exchange.

    Classifying costs no wire time - pymodbus has already waited for the frame -
    and it is what lets a caller tell an acknowledged stop from a hopeful one.
    """
    drive = build_drive(clock, bus)
    bus.sticky_writes = exception_response(WRITE_SINGLE_REGISTER, ILLEGAL_DATA_ADDRESS)

    outcome = drive.emergency_disable_blocking(Seconds(1.0))

    assert outcome is EmergencyStopOutcome.SENT_UNCONFIRMED
    assert len(bus.writes()) == 1, "one write, and no retry on a bad answer"


@pytest.mark.parametrize("timeout", [Seconds(0.0), Seconds(-1.0), Seconds(-0.0001)])
def test_emergency_disable_writes_even_with_no_budget_at_all(
    clock: ManualClock, bus: FakeBus, timeout: Seconds
) -> None:
    """A non-positive budget must not be able to turn this into a no-op.

    Measured on the previous version: with ``Seconds(0.0)`` or a negative
    timeout NOTHING was sent, and the method then latched the link, so the
    following ``close()`` refused and wrote nothing either. A stop request that
    silently sends no frames is worse than an unbounded one.
    """
    drive = build_drive(clock, bus)
    regs = RegisterMap()
    bus.registers[regs.lfrd] = 900

    outcome = drive.emergency_disable_blocking(timeout)

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert bus.writes() == [(regs.lfrd, 0)]
    assert bus.setpoint() == 0


def test_a_budget_smaller_than_the_link_allows_is_reported_not_obeyed(
    clock: ManualClock, bus: FakeBus
) -> None:
    """A bound this driver cannot keep must be said out loud, not pretended to.

    The line settings were fixed at construction and a blocking call cannot be
    cut short, so a caller asking for less than the floor is asking for
    something impossible. Silently returning early without writing would be the
    only genuinely unsafe answer, so the floor is used and logged - and
    :attr:`ATV320Drive.emergency_budget` exists so a caller need never guess.
    """
    drive = build_drive(clock, bus)
    regs = RegisterMap()
    bus.registers[regs.lfrd] = 1380

    assert drive.emergency_budget == emergency_budget_for(SETTINGS)
    assert drive.emergency_budget > Seconds(0.2)
    outcome = drive.emergency_disable_blocking(Seconds(0.2))

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert bus.writes() == [(regs.lfrd, 0)]


def test_emergency_disable_sends_the_write_even_when_the_budget_is_gone(
    clock: ManualClock, bus: FakeBus
) -> None:
    """The write that matters is never the write to sacrifice.

    The previous version checked the deadline BEFORE each write, so whichever
    write happened to be second was the one that got skipped. There is now only
    one write and no check in front of it: the budget buys the lock wait, and
    nothing else.
    """
    drive = build_drive(clock, bus)
    regs = RegisterMap()
    bus.registers[regs.lfrd] = 1380
    # Latency ten times the budget, charged to the injected clock, so the
    # "budget already spent" condition is unambiguously true.
    bus.latency = Seconds(10.0 * drive.emergency_budget)

    assert (
        drive.emergency_disable_blocking(drive.emergency_budget)
        is EmergencyStopOutcome.ACKNOWLEDGED
    )
    assert bus.writes() == [(regs.lfrd, 0)]
    assert bus.setpoint() == 0


# --- What one transaction really costs -----------------------------------


def _character_time(settings: SerialSettings) -> float:
    return (1 + settings.bytesize + settings.stopbits) / settings.baudrate


def test_a_transaction_costs_five_serial_timeouts_at_worst_not_one() -> None:
    """The finding this budget was rebuilt for: one write is not one timeout.

    pymodbus 3.7.4, read off its source: the send waits up to T for a
    transaction state left behind; the head read polls in_waiting for up to T
    and then reads for up to T; the body the same again, once per ``retries``.
    With retries=1 that is 5 T, before any overshoot. The old bound assumed
    one T per write and so promised an emergency stop it could overrun by 5x.
    """
    settings = SerialSettings(port="p", timeout=Seconds(0.1))
    char = _character_time(settings)
    silence = 3.5 * char
    per_wait_overshoot = wait_overshoot(settings.frame) + max(4 * char, 0.001)

    worst = transaction_worst_case(settings)

    assert worst == pytest.approx(5 * 0.1 + silence + 5 * per_wait_overshoot + USB_STALL_BOUND)
    assert worst > 5 * settings.timeout


def test_each_extra_retry_adds_two_timeouts_and_pymodbus_s_fixed_pause() -> None:
    one = SerialSettings(port="p", timeout=Seconds(0.05), retries=1)
    two = SerialSettings(port="p", timeout=Seconds(0.05), retries=2)
    per_wait_overshoot = wait_overshoot(one.frame) + max(4 * _character_time(one), 0.001)

    added = transaction_worst_case(two) - transaction_worst_case(one)

    assert added == pytest.approx(2 * 0.05 + PYMODBUS_BODY_RETRY_PAUSE + 2 * per_wait_overshoot)


def test_above_19200_baud_pymodbus_uses_a_fixed_inter_frame_silence() -> None:
    """3.5 characters at 19200 and below; a flat 1.75 ms above, as pymodbus does."""
    fast = SerialSettings(port="p", baudrate=38400, timeout=Seconds(0.1))
    slow = SerialSettings(port="p", baudrate=19200, timeout=Seconds(0.1))
    fast_overshoot = 5 * (wait_overshoot(fast.frame) + max(4 * _character_time(fast), 0.001))
    slow_overshoot = 5 * (wait_overshoot(slow.frame) + max(4 * _character_time(slow), 0.001))

    assert transaction_worst_case(fast) - fast_overshoot - USB_STALL_BOUND == pytest.approx(
        0.5 + 0.00175
    )
    assert transaction_worst_case(slow) - slow_overshoot - USB_STALL_BOUND == pytest.approx(
        0.5 + 3.5 * _character_time(slow)
    )


def test_a_stalled_usb_transfer_is_counted_once_per_transaction() -> None:
    """pyftdi's 5 s USB default would have been invisible to the budget; 0.1 s is not."""
    settings = SerialSettings(port="p", timeout=Seconds(0.1))
    assert USB_STALL_BOUND > 0.0
    assert transaction_worst_case(settings) > 5 * settings.timeout + USB_STALL_BOUND


def test_the_emergency_budget_is_two_transactions_plus_headroom() -> None:
    """One in flight that the emergency write must queue behind, then its own."""
    worst = transaction_worst_case(SETTINGS)
    assert emergency_budget_for(SETTINGS) == pytest.approx(2 * worst + EMERGENCY_SCHEDULING_MARGIN)
    assert emergency_budget_for(SETTINGS) <= MAX_EMERGENCY_BUDGET


def test_a_line_whose_emergency_bound_is_too_long_is_refused() -> None:
    """The bound is made real at construction, where nothing is spinning yet.

    A blocking serial call cannot be cut short once it has started, so the only
    way to keep the emergency stop short is to refuse a line whose worst case
    makes it long. Measured on an older version with no such check: a legal
    ``timeout=5.0`` made the call block ~5 s - the "the Pi will not shut down
    and gets power-cycled mid-session" case. 0.3 s, the previous default, is
    refused too: two worst-case transactions of 5 x 0.3 s is a 3 s stop.
    """
    for too_slow in (
        SerialSettings(port="COM-NONE", timeout=Seconds(0.3)),
        SerialSettings(port="COM-NONE", timeout=Seconds(0.1), retries=5),
    ):
        with pytest.raises(ValueError, match="emergency-stop bound"):
            ATV320Drive(
                ManualClock(),
                FakeBus(ManualClock(), RegisterMap()),
                too_slow,
                RegisterMap(),
            )


def test_emergency_disable_returns_within_its_budget_in_real_wall_time(bus: FakeBus) -> None:
    """The bound, measured against a transport that blocks in REAL seconds.

    The fake sleeps the thread for a whole worst-case transaction, the driver
    holds a RealClock, and the elapsed time is read with ``perf_counter``.
    """
    settings = SerialSettings(port="COM-NONE", timeout=Seconds(0.02))
    drive = build_drive(RealClock(), bus, settings=settings)
    bus.real_write_block = transaction_worst_case(settings)

    started = time.perf_counter()
    outcome = drive.emergency_disable_blocking(drive.emergency_budget)
    elapsed = time.perf_counter() - started

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert elapsed < drive.emergency_budget, (
        f"took {elapsed:.3f} s against a {drive.emergency_budget:.3f} s budget"
    )
    assert elapsed >= bus.real_write_block, "the fake really did block the thread"
    assert len(bus.writes()) == 1


async def _race_the_executor(
    drive: ATV320Drive, bus: FakeBus, budget: Seconds
) -> tuple[EmergencyStopOutcome, float]:
    """Fire the emergency stop while a transaction is in flight on the executor.

    ``open()`` is used as the in-flight operation because it costs exactly one
    blocking read, so the interleaving is one transaction and not four.
    ``entered_read`` is set from inside the fake's transaction body, so this
    really does race a transaction that has started, rather than one that is
    merely scheduled.
    """
    opening = asyncio.get_running_loop().create_task(drive.open())
    assert await asyncio.to_thread(bus.entered_read.wait, 5.0)
    started = time.perf_counter()
    outcome = drive.emergency_disable_blocking(budget)
    taken = time.perf_counter() - started
    await opening
    return outcome, taken


def test_emergency_disable_gives_up_on_the_lock_rather_than_waiting_it_out(
    bus: FakeBus,
) -> None:
    """A bounded lock wait, measured in real wall time, then write anyway.

    The wait is what the budget leaves after reserving the write's own
    worst-case transaction. This fake has no transaction lock of its own, so
    the two overlap here, which is the evidence that this driver stopped
    waiting; the real pymodbus client would queue the write on its ``RLock``
    until the in-flight exchange ended, which the budget already counts.
    """
    settings = SerialSettings(port="COM-NONE", timeout=Seconds(0.02))
    drive = build_drive(RealClock(), bus, settings=settings)
    budget = drive.emergency_budget
    lock_wait = budget - transaction_worst_case(settings) - EMERGENCY_SCHEDULING_MARGIN
    bus.real_read_block = Seconds(budget + 0.2)  # far longer than the lock wait

    outcome, taken = asyncio.run(_race_the_executor(drive, bus, budget))

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED, "it wrote anyway"
    assert taken >= lock_wait, "it really did wait for the lock"
    assert taken < budget, f"and it gave up inside the budget, not after {taken:.3f} s"
    assert bus.max_in_flight == 2, "it stopped waiting rather than sitting out the fault"


def test_the_transport_lock_keeps_the_emergency_write_off_a_busy_bus(bus: FakeBus) -> None:
    """The race from defect 5, and the consequence it has on a real port.

    ``emergency_disable_blocking`` drives the shared ModbusSerialClient from the
    CALLING thread while the executor thread may be mid-transaction. pymodbus
    calls ``connect()`` outside its own transaction lock and closes the port on
    every no-response, so both threads can race to open an ``exclusive=True``
    port; the loser's ``connect()`` returns False and ``execute`` raises
    ConnectionException, which ``_blind_write`` swallows. The emergency write is
    then lost, during a comms fault, which is exactly when it is needed.

    ``overlap_raises`` models that consequence. Without the shared threading
    lock the emergency write overlaps the in-flight read and is lost; with it,
    the write waits the short time the bus needs and lands.
    """
    regs = RegisterMap()
    drive = build_drive(RealClock(), bus)
    bus.registers[regs.lfrd] = 1380
    bus.overlap_raises = True
    bus.real_read_block = Seconds(0.05)  # comfortably inside the lock wait

    outcome, _ = asyncio.run(_race_the_executor(drive, bus, drive.emergency_budget))

    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert bus.max_in_flight == 1, "the two threads did not overlap on the wire"
    assert bus.setpoint() == 0


# =========================================================================
# Half duplex
# =========================================================================


async def test_transactions_do_not_interleave(clock: ManualClock, bus: FakeBus) -> None:
    """RS-485 is half duplex: two overlapping transactions corrupt each other.

    Two concurrent write_speed calls each do write-then-read. Without the
    single-flight lock the log could read W W R R, with each task verifying the
    other's setpoint. It must read W R W R.
    """
    drive = build_drive(clock, bus)
    bus.accepted_leftover_setpoint = None

    await asyncio.gather(drive.write_speed(MotorRpm(0)), drive.write_speed(MotorRpm(0)))

    assert bus.max_in_flight == 1
    assert [t.access for t in bus.log] == [
        Access.WRITE,
        Access.READ,
        Access.WRITE,
        Access.READ,
    ]


# =========================================================================
# Contract rule 5
# =========================================================================

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
ISOLATION_MODULE: Final[Path] = PROJECT_ROOT / "src" / "motor" / "atv320.py"
FTDI_ISOLATION_MODULE: Final[Path] = PROJECT_ROOT / "src" / "motor" / "ftdi_link.py"

#: Each untyped transport library, and the ONE module under src/ allowed to
#: import it. The same table as rule 5 of the strict-python skill.
ISOLATION_TABLE: Final[Mapping[str, Path]] = {
    "pymodbus": ISOLATION_MODULE,
    "serial": ISOLATION_MODULE,
    "pyftdi": FTDI_ISOLATION_MODULE,
    "usb": FTDI_ISOLATION_MODULE,
}


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_each_transport_library_has_exactly_one_importer() -> None:
    """Contract rule 5. Everything above these modules sees domain types only.

    A second importer is how the wire format leaks upward: two places that each
    decide how to handle a returned ModbusIOException - or a USB error - will
    eventually disagree, and the disagreement shows up as a drive error that
    one of them swallows.
    """
    offenders: dict[str, set[str]] = {}
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        imported = _imported_roots(path.read_text(encoding="utf-8"))
        found = {lib for lib, owner in ISOLATION_TABLE.items() if lib in imported and owner != path}
        if found:
            offenders[str(path.relative_to(PROJECT_ROOT))] = found
    assert not offenders, (
        f"transport libraries imported outside their isolation module: {offenders}"
    )


def test_the_isolation_modules_really_do_import_their_libraries() -> None:
    """Otherwise the test above would pass vacuously after a rename."""
    for library, owner in ISOLATION_TABLE.items():
        if library == "serial":
            continue  # reserved for atv320.py, which needs nothing from it today
        assert library in _imported_roots(owner.read_text(encoding="utf-8")), library


@pytest.mark.parametrize("module", [ISOLATION_MODULE, FTDI_ISOLATION_MODULE])
def test_the_isolation_modules_do_not_read_the_clock_directly(module: Path) -> None:
    """Contract rule 4: timing comes from the injected Clock, never time.*."""
    source = module.read_text(encoding="utf-8")
    assert "time.monotonic()" not in source
    assert "time.time()" not in source
    assert "import time" not in source
