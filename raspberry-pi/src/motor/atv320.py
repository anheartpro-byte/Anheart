"""The real Modbus RTU driver for the ATV320. The ONLY module that imports pymodbus.

This is the isolation module required by contract rule 5: ``pymodbus`` and
``serial`` are touched here and nowhere else (``pyftdi`` has its own, in
:mod:`src.motor.ftdi_link`), so everything above :mod:`src.motor.drive` sees
domain types only. It implements ``drive.DriveBackend`` and adds
:meth:`ATV320Drive.enable`, which is a mechanism (the CiA402 start sequence)
rather than a policy.

Hardware, from the commissioning notes and the bench: ATV320U04M2C driving a
SEW KA37 DRS71S4 (1380 rpm at 50 Hz) through i = 49.79. Modbus RTU at
**19200 baud, 8E1**, answering on **address 248** - Schneider's point-to-point
address, measured on the bench; the drive's configured address did not answer.
Those are the constructor defaults. Nothing is hardcoded beyond defaults: every
transport parameter arrives in :class:`SerialSettings` so the bench can change
one without editing a safety module.

The port is either an OS serial device (``/dev/ttyUSB0``, ``/dev/cu.*``,
``COM3``) or, for the Schneider USB-RS485 cable that macOS gives no device
node, the pyftdi URL ``ftdi://schneider:rs485/1``. The second goes through
:class:`FtdiModbusClient`, whose port has a working ``in_waiting``; see
:mod:`src.motor.ftdi_link` for why that is the difference between 2 s and
tens of milliseconds per register.

What this driver guarantees, and what it refuses to
---------------------------------------------------

* **Nothing blocks the event loop.** Every pymodbus call is blocking, and the
  same loop serves the operator's emergency-stop endpoint. A 0.3 s serial read
  taken on the loop is 0.3 s during which the button does nothing, so all of
  them go through ``run_in_executor`` on a private single-thread pool.
* **One transaction at a time.** RS-485 is half duplex: two overlapping
  transactions do not take turns, they transmit over each other and each gets
  the other's reply. An ``asyncio.Lock`` enforces single flight, and the
  executor has exactly one worker so the invariant survives anything that
  finds a way around the lock.
* **A write is not a landing.** Every speed write is read back (see
  :meth:`ATV320Drive.write_speed`). Most Altivar parameters are writable while
  running, so a misaddressed write does not bounce - it is acked while landing
  in ACC/DEC/HSP next door, and every later read still looks normal. A write is
  not acknowledged either until the reply's function code says so: any decoded
  PDU would otherwise do, including a stale read reply.
* **A stop never drops the output stage on a moving machine.** CMD = SHUTDOWN
  out of OPERATION_ENABLED is CiA402 transition 8: torque goes away and a loaded
  centrifuge freewheels for minutes while the drive reports READY. So
  :meth:`ATV320Drive.close` zeroes LFRD, polls RFRD to standstill, and only then
  writes 7 and 6 - and if standstill cannot be confirmed it leaves the run
  command in place and says so, because the drive's own ``ttO`` ramp beats a
  freewheel by two orders of magnitude. The budget-bounded
  :meth:`ATV320Drive.emergency_disable_blocking` cannot wait at all, so it
  writes LFRD = 0 and touches CMD not at all, for the same reason.
* **The comms latch never blocks a stop.** Once the link is declared lost this
  driver stops writing, so the drive's ``ttO`` applies - but the two
  :data:`STOP_WORDS` and a zero setpoint still go out. The latch exists to stop
  a keepalive nobody can verify, not to keep a motor commanded.
* **No internal software watchdog, deliberately.** The independent watchdog is
  the drive's own ``ttO`` Modbus timeout, configured on the drive and therefore
  outside this process. Any watchdog written here would die with the event loop
  it was supposed to be watching, which is precisely the failure it claims to
  cover. What this code offers instead is the complementary half: after
  :data:`DEFAULT_FAILURE_THRESHOLD` consecutive failed transactions it latches
  the link down and **stops writing**, so the drive stops hearing a keepalive
  and applies ``ttO`` for real. Only :meth:`ATV320Drive.open` clears that
  latch, because there is no automatic resumption of motion anywhere in this
  system.
* **No policy.** No speed clamp, no ramp shaping, no retry loop, no fault
  reset. Those decisions have a person inside the centrifuge attached to them
  and belong in the safety layer, where they can be tested against a plant
  model.

pymodbus 3.7.4 facts this module is pinned to (verified against the installed
package, not assumed - the keyword names moved across 3.x)
---------------------------------------------------------

* ``ModbusSerialClient(port, framer=FramerType.RTU, baudrate, bytesize,
  parity, stopbits, timeout, retries)``; constructing it does **not** open the
  port, ``connect()`` does.
* The slave-address keyword is ``slave``:
  ``read_holding_registers(address, count=1, slave=1)`` and
  ``write_register(address, value, slave=1)``. Not ``unit``, not ``device_id``.
* The package ships ``py.typed``, so no hand-written stub is needed - but its
  annotations are **not honest about failure**. ``read_holding_registers`` is
  annotated ``-> ModbusPDU`` while ``SyncModbusTransactionManager.execute``
  *returns* a ``ModbusIOException`` instance when the drive does not answer,
  and ``ModbusBaseSyncClient.execute`` *raises* ``ConnectionException`` when
  the port will not open. Both shapes are handled here, which is why
  :class:`ModbusMaster` types replies as ``object``: the lie has to be
  narrowed, and an ``object`` cannot be dereferenced by accident.
* ``ModbusPDU`` declares ``registers: list[int]`` in ``__init__`` without ever
  assigning it, so a PDU that carries no register block (a write echo, say)
  raises ``AttributeError`` on access rather than answering empty. Guarded.
* ``serial.SerialException`` derives from ``OSError`` while ``ModbusException``
  does not, so catching ``OSError`` covers pyserial without importing it.
  pyftdi's ``FtdiError`` derives from ``IOError``, which is the same class.
* ``retries`` does NOT retransmit anything in the synchronous client, whatever
  its name says. ``SyncModbusTransactionManager.execute`` wraps the response
  check in ``while retries > 0:`` and then ``break``s unconditionally on the
  first pass; ``_retry_transaction`` is never called. Its one live effect is in
  ``_recv``, where it bounds how many ``recv()`` calls fetch the frame BODY
  after the 4-byte head. With ``retries=0`` the body is never read, every reply
  is "incomplete", and every transaction returns ``ModbusIOException`` - which
  is what the bench saw. Hence :data:`MIN_RETRIES`.
* One transaction can spend several serial timeouts, not one: see
  :func:`transaction_worst_case`, from which the emergency-stop budget is
  derived.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Final, Protocol, assert_never, final, override, runtime_checkable

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.framer import FramerType
from pymodbus.pdu import ExceptionResponse, ModbusPDU

from src.clock import Clock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFaulted,
    DriveLimits,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    FaultReport,
    RegisterMap,
    StopUnconfirmed,
    UnexpectedState,
    decode_current,
    decode_limits,
    decode_speed,
    decode_status_word,
    describe_fault,
)
from src.motor.ftdi_link import (
    USB_STALL_BOUND,
    BufferedFtdiPort,
    DeviceFactory,
    Parity,
    UartFrame,
    is_ftdi_url,
    open_ftdi_port,
    wait_overshoot,
)
from src.motor.link_health import LinkHealth
from src.motor.observation import (
    Exchange,
    ExchangeKind,
    ExchangeLog,
    ObservableDrive,
    RegisterRequest,
)
from src.result import Err, Ok, Result, err_of
from src.units import (
    Monotonic,
    MotorRpm,
    RawRegister,
    RegisterAddress,
    Seconds,
    StatusWord,
    elapsed,
    raw_register,
    signed_to_register,
)

logger = logging.getLogger(__name__)


# =========================================================================
# The pymodbus seam
# =========================================================================


@runtime_checkable
class ModbusMaster(Protocol):
    """Exactly the four pymodbus operations this driver performs.

    Narrowing the library to four methods buys two things. It is the whole
    surface a test double has to implement, so the driver's error handling can
    be exercised without a serial port; and it lets the replies be typed
    ``object``.

    That ``object`` is the important part. pymodbus annotates both calls
    ``-> ModbusPDU``, but its synchronous transaction manager **returns** a
    ``ModbusIOException`` when the drive does not answer, and returns ``None``
    when a caller sets ``no_response_expected``. Typing the reply the way the
    library promises would let this module dereference a value that is not a
    PDU; typing it as ``object`` forces the narrowing reality requires.
    """

    def connect(self) -> bool:
        """Open the serial port. ``False`` means it did not open."""
        ...

    def close(self) -> None:
        """Release the serial port."""
        ...

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        """Modbus function 3."""
        ...

    def write_register(self, address: int, value: int, slave: int = 1) -> object:
        """Modbus function 6."""
        ...


WRITE_SINGLE_REGISTER_CODE: Final[int] = 6
"""Modbus function code 6, checked on every write reply.

Without it, any object pymodbus's framer managed to decode counts as an
acknowledgement - including the answer to a previous READ, which on a noisy
half-duplex pair is exactly what turns up. "The write was acked" would then be
a statement about the framer rather than about the drive.

There is deliberately no matching check on the READ path, and it is not an
oversight: a read reply is already validated by the thing a read actually needs
(a register block of exactly one value, in range), which no other PDU shape can
satisfy - ``ModbusPDU`` declares ``registers`` without assigning it, so a write
echo decoded into a read's window fails there instead. Adding a redundant
function-code test there would only make that branch unreachable.
"""


# --- Transport defaults, from the commissioning notes and the bench -----
DEFAULT_BAUDRATE: Final[int] = 19200
DEFAULT_BYTESIZE: Final[int] = 8
DEFAULT_PARITY: Final[Parity] = Parity.EVEN
DEFAULT_STOPBITS: Final[int] = 1

DEFAULT_TIMEOUT: Final[Seconds] = Seconds(0.1)
"""Per-read serial timeout.

Sized against the control budget and against what the link really takes, not
against the drive's patience. A healthy exchange is tens of milliseconds - an
8-byte request and a 7-byte reply at 19200 8E1 are ~9 ms of wire time, plus the
drive's turnaround and one USB latency period - so 0.1 s is several times the
real answer while keeping a MISSING answer cheap. That matters twice over: one
transaction spends this timeout several times (see
:func:`transaction_worst_case`), and the emergency-stop budget, which blocks
the event loop, is derived from it. Raise it only against a bench measurement
that shows real replies arriving late.
"""

SCHNEIDER_POINT_TO_POINT_ADDRESS: Final[int] = 248
"""Schneider's point-to-point Modbus address.

An Altivar answers on 248 whatever its configured ``Add`` is, provided it is
the only device on the link - which is the case here: one drive, one cable. It
is outside the standard 1..247 unit-address range on purpose, so it can never
collide with a configured address. Measured on the bench: the drive answers
248, and did not answer 1.
"""

DEFAULT_SLAVE_ADDRESS: Final[int] = SCHNEIDER_POINT_TO_POINT_ADDRESS

MIN_RETRIES: Final[int] = 1
"""The smallest ``retries`` pymodbus 3.7.4 can work with.

Not a retry count, whatever pymodbus calls it: see the module docstring. With 0
the body of every reply is never read and every transaction fails.
"""

DEFAULT_RETRIES: Final[int] = MIN_RETRIES
"""One body read per reply, and no retransmission - the 3.7.4 synchronous
client never retransmits at any setting.

More would buy nothing on a healthy link, where the body is already waiting
when the head has been read, and each extra one adds two serial timeouts plus a
fixed 0.1 s pause to the worst case of a transaction (see
:func:`transaction_worst_case`). That worst case is a time budget the layer
above is spending without seeing it, and a retry is a policy decision - it
depends on whether somebody is in the machine - that does not belong hidden in
the transport.
"""

MODBUS_SLAVE_MIN: Final[int] = 1
MODBUS_SLAVE_MAX: Final[int] = 247
"""Standard Modbus RTU unit-address range. 0 is the BROADCAST address and is
rejected: a broadcast write commands every drive on the bus and gets no reply,
so it is both wider than intended and impossible to verify. The one address
accepted outside this range is :data:`SCHNEIDER_POINT_TO_POINT_ADDRESS`."""


def is_valid_slave_address(address: int) -> bool:
    """A standard unit address, or Schneider's point-to-point 248. Never broadcast."""
    return (
        MODBUS_SLAVE_MIN <= address <= MODBUS_SLAVE_MAX
        or address == SCHNEIDER_POINT_TO_POINT_ADDRESS
    )


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Everything needed to open the drive's port, and nothing else.

    Frozen so the link cannot be re-parameterised under a running session, and
    a plain record so the operator-visible configuration is one object to log.

    Validated at construction, in the style :class:`src.motor.drive.RegisterMap`
    already sets (startup boundary, nothing spinning, refusing to start is the
    correct answer), where a wrong value would fail silently or confidently
    rather than loudly: the unit address (talks to the wrong device), the
    timeout (reads nothing), ``retries`` (never reads a reply body) and an empty
    port. A wrong baud rate or parity is not checked: it fails loudly on the
    first CRC.
    """

    port: str
    """``/dev/ttyUSB0`` on the Pi, ``COM3`` on Windows, a ``/dev/cu.*`` device
    on macOS - or ``ftdi://schneider:rs485/1`` for the Schneider USB-RS485
    cable, which macOS gives no device node (see :mod:`src.motor.ftdi_link`)."""

    baudrate: int = DEFAULT_BAUDRATE
    bytesize: int = DEFAULT_BYTESIZE
    parity: Parity = DEFAULT_PARITY
    stopbits: int = DEFAULT_STOPBITS
    timeout: Seconds = DEFAULT_TIMEOUT
    slave_address: int = DEFAULT_SLAVE_ADDRESS
    retries: int = DEFAULT_RETRIES

    def __post_init__(self) -> None:
        if not self.port.strip():
            raise ValueError(
                "the drive's port is empty; set MOTOR_PORT to a serial device or to an ftdi:// URL"
            )
        if not is_valid_slave_address(self.slave_address):
            raise ValueError(
                f"Modbus slave address {self.slave_address} is neither in "
                f"{MODBUS_SLAVE_MIN}..{MODBUS_SLAVE_MAX} nor Schneider's point-to-point "
                f"address {SCHNEIDER_POINT_TO_POINT_ADDRESS}; address 0 is the broadcast "
                "address, which commands every drive on the bus and cannot be verified"
            )
        if self.timeout <= 0.0:
            raise ValueError(
                f"serial timeout {self.timeout} s must be positive; a zero timeout "
                "makes every read return nothing, which this driver would report as "
                "a dead drive"
            )
        if self.retries < MIN_RETRIES:
            raise ValueError(
                f"retries={self.retries} must be at least {MIN_RETRIES}: pymodbus 3.7.4 "
                "uses it as the number of reads of a reply's body, not as a "
                "retransmission count, so with 0 it never reads the body and every "
                "transaction fails with 'No response'"
            )

    @property
    def frame(self) -> UartFrame:
        """The character format alone, as the FTDI chip is told it."""
        return UartFrame(
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
        )


# =========================================================================
# What one transaction can cost
# =========================================================================

PYMODBUS_BODY_RETRY_PAUSE: Final[Seconds] = Seconds(0.1)
"""The fixed ``time.sleep(0.1)`` pymodbus 3.7.4 takes before each body read
after the first (``SyncModbusTransactionManager._recv``)."""

EMERGENCY_SCHEDULING_MARGIN: Final[Seconds] = Seconds(0.05)
"""Headroom in the emergency budget for thread scheduling and lock hand-over.
Windows timer granularity alone is ~15 ms per wait."""

MAX_EMERGENCY_BUDGET: Final[Seconds] = Seconds(2.0)
"""The longest emergency-stop budget this driver agrees to be built with.

The emergency zero blocks the calling thread - in the training runtime, the
event loop, on purpose (see ``TrainingRuntime._emergency_zero``). A line whose
worst case pushes the bound past this refuses to start, at startup, with
nothing spinning, rather than discovering at shutdown that the stop takes long
enough for somebody to pull the plug on the Pi.
"""


def _character_time(settings: SerialSettings) -> Seconds:
    """One character, as pymodbus counts it: start + data + stop bits, no parity bit."""
    return Seconds((1 + settings.bytesize + settings.stopbits) / settings.baudrate)


def _silent_interval(settings: SerialSettings) -> Seconds:
    """pymodbus's inter-frame silence: 3.5 characters, or 1.75 ms above 19200 baud."""
    if settings.baudrate > DEFAULT_BAUDRATE:
        return Seconds(0.00175)
    return Seconds(3.5 * _character_time(settings))


def transaction_worst_case(settings: SerialSettings) -> Seconds:
    """The longest one pymodbus 3.7.4 transaction can block, from its first byte out.

    Not one serial timeout ``T``. Read off ``pymodbus/client/serial.py`` and
    ``pymodbus/transaction.py``, with ``r`` = ``retries``:

    * **send, up to T + silence.** ``ModbusSerialClient.send`` waits for the
      client's state to return to IDLE, polling until ``T`` has passed, when a
      previous exchange left it mid-transaction (an exception escaping
      ``execute``, or another thread's in-flight transaction); plus 3.5
      characters of inter-frame silence.
    * **head, up to 2T.** ``recv(4)``: ``_wait_for_data`` polls ``in_waiting``
      for up to ``T``, and then ``read(4)`` waits up to ``T`` again. A drive
      that says nothing costs both.
    * **body, up to r x 2T, plus (r - 1) x 0.1 s.** The same pair per body
      read, with pymodbus's fixed pause between them.
    * **overshoot**, per timed wait (3 + 2r of them): one poll interval of
      pymodbus's and one drain of the FTDI port, which on a chattering line
      is several characters long, not one latency period (see
      :func:`~src.motor.ftdi_link.wait_overshoot`).
    * **one stalled USB transfer**, once: libusb gives up on a chip that stops
      answering after :data:`~src.motor.ftdi_link.USB_STALL_BOUND`, the
      transfer raises, and pymodbus ends the transaction on it, so no
      transaction pays it twice. On an OS serial device these two terms
      over-count, which errs on the safe side.

    What it does NOT bound, stated rather than glossed: ``connect()``. pymodbus
    closes the port after every failed exchange and reopens it on the next,
    and opening a device (a tty, or a USB interface through libusb) is an OS
    call no read timeout covers. For an ``ftdi://`` port it also includes
    :func:`~src.motor.ftdi_link.settle`: at least ``SETTLE_DWELL`` (0.25 s),
    at most ``SETTLE_BUDGET`` (1.0 s), because the FT232R times out USB
    transfers for ~100-200 ms after being configured (measured on the bench).
    """
    timeout = settings.timeout
    retries = settings.retries
    waits = 3 + 2 * retries
    poll = max(4 * _character_time(settings), 0.001)
    overshoot = waits * (wait_overshoot(settings.frame) + poll)
    return Seconds(
        (timeout + _silent_interval(settings))
        + 2 * timeout
        + retries * 2 * timeout
        + (retries - 1) * PYMODBUS_BODY_RETRY_PAUSE
        + overshoot
        + USB_STALL_BOUND
    )


def emergency_budget_for(settings: SerialSettings) -> Seconds:
    """The smallest whole-call bound the emergency zero can keep on this line.

    Two transactions plus headroom, and the reason it is two is pymodbus's own
    transaction lock (an ``RLock`` around ``execute``). When the emergency
    arrives while the executor thread is mid-transaction, the emergency write
    cannot overlap it however it is written: it waits, on this driver's wire
    lock or failing that on pymodbus's, until the in-flight transaction ends -
    at most one :func:`transaction_worst_case` after the call began - and then
    spends at most one more on its own.
    """
    return Seconds(2 * transaction_worst_case(settings) + EMERGENCY_SCHEDULING_MARGIN)


# =========================================================================
# Building the pymodbus client
# =========================================================================


class ObservedModbusClient(ModbusSerialClient):
    _exchange_log: ExchangeLog | None = None

    def observe_exchanges(self, log: ExchangeLog) -> None:
        self._exchange_log = log

    @override
    def send(self, request: bytes) -> int:
        if self._exchange_log is None:
            return super().send(request)
        send = super().send
        return self._exchange_log.send(request, lambda: send(request))

    @override
    def recv(self, size: int | None) -> bytes:
        if self._exchange_log is None:
            return super().recv(size)
        recv = super().recv
        return self._exchange_log.receive(lambda: recv(size))


@final
class FtdiModbusClient(ObservedModbusClient):
    """``ModbusSerialClient`` whose port is :class:`~src.motor.ftdi_link.BufferedFtdiPort`.

    Only :meth:`connect` differs. pymodbus 3.7.4 opens ports with
    ``serial.serial_for_url``, which for ``ftdi://`` would hand back pyftdi's
    own port, the one whose ``in_waiting`` is always 0. Everything after the
    port is open - framing, CRC, inter-frame silence, the transaction lock,
    closing on failure - is pymodbus's, unchanged.

    Why a subclass rather than a pyserial URL handler: a handler has to live in
    a module named ``protocol_ftdi`` inside a package on pyserial's global
    search list, and it would silently change what ``ftdi://`` means for every
    other user of pyserial in the process. This changes one client, visibly.
    """

    def __init__(self, settings: SerialSettings, open_port: Callable[[], BufferedFtdiPort]) -> None:
        super().__init__(
            port=settings.port,
            framer=FramerType.RTU,
            baudrate=settings.baudrate,
            bytesize=settings.bytesize,
            parity=settings.parity.value,
            stopbits=settings.stopbits,
            timeout=settings.timeout,
            retries=settings.retries,
        )
        self._port_opener: Callable[[], BufferedFtdiPort] = open_port

    @override
    def connect(self) -> bool:
        """Open the cable if it is not open. ``False``, never an exception, on failure.

        Mirrors ``ModbusSerialClient.connect``: pymodbus calls this before every
        transaction and after every failure, and turns ``False`` into
        ``ConnectionException``, which the driver maps to ``CommTimeout``.
        """
        if self.socket is not None:
            return True
        try:
            port = self._port_opener()
        except Exception:
            logger.exception("ATV320: could not open %s", self.comm_params.host)
            return False
        # pymodbus annotates `socket` as `serial.Serial`, but everything it does
        # with it is in_waiting / read / write / close / is_open /
        # inter_byte_timeout - verified against pymodbus 3.7.4's
        # client/serial.py and transaction.py, and exercised end to end through
        # the real transaction manager in tests/test_ftdi_link.py.
        self.socket = port  # pyright: ignore[reportAttributeAccessIssue]
        self.last_frame_end = None
        return True


def serial_master(
    settings: SerialSettings,
    clock: Clock,
    *,
    open_device: DeviceFactory | None = None,
    exchange_log: ExchangeLog | None = None,
) -> ModbusMaster:
    """Build the real pymodbus RTU master. The ONE place a client is constructed.

    An ``ftdi://`` port gets :class:`FtdiModbusClient`; anything else - a tty,
    a ``/dev/cu.*``, a ``COM`` port - the stock ``ModbusSerialClient``, whose
    pyserial port has a working ``in_waiting`` of its own.

    Constructing does not touch the hardware - both clients open the port in
    ``connect()`` - so this is safe at startup and testable with a port that
    does not exist. ``open_device`` replaces the USB opener, for tests.
    """
    if is_ftdi_url(settings.port):
        client = FtdiModbusClient(
            settings,
            lambda: open_ftdi_port(
                settings.port,
                settings.frame,
                timeout=settings.timeout,
                clock=clock,
                create=open_device,
            ),
        )
        if exchange_log is not None:
            client.observe_exchanges(exchange_log)
        return client
    client_type = ModbusSerialClient if exchange_log is None else ObservedModbusClient
    serial = client_type(
        port=settings.port,
        framer=FramerType.RTU,
        baudrate=settings.baudrate,
        bytesize=settings.bytesize,
        parity=settings.parity.value,
        stopbits=settings.stopbits,
        timeout=settings.timeout,
        retries=settings.retries,
    )
    if isinstance(serial, ObservedModbusClient) and exchange_log is not None:
        serial.observe_exchanges(exchange_log)
    return serial


# =========================================================================
# Driver tuning
# =========================================================================

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
"""Consecutive failed transactions before the link is latched down.

More than one, because a single lost frame on an RS-485 pair is normal and
tripping on it would stop sessions for noise. Small, because every failure
costs a whole serial timeout while the layer above waits.
"""

DEFAULT_SETTLE_ATTEMPTS: Final[int] = 3
"""ETA reads allowed per :meth:`ATV320Drive.enable` step before giving up.

A command word takes effect on the drive's next internal scan, so the first
read after a write can legitimately still show the previous state. One read
would make the start sequence fail on the bench for a few milliseconds'
reason. Bounded, and it never waits out a FAULT.
"""

DEFAULT_SETTLE_DELAY: Final[Seconds] = Seconds(0.02)
"""Pause between those ETA reads. Roughly one drive scan cycle."""

ENABLE_SEQUENCE: Final[tuple[tuple[ControlWord, DriveState], ...]] = (
    (ControlWord.SHUTDOWN, DriveState.READY),
    (ControlWord.SWITCH_ON, DriveState.SWITCHED_ON),
    (ControlWord.ENABLE_OPERATION, DriveState.OPERATION_ENABLED),
)
"""The CiA402 start sequence, each command word paired with the state the drive
must reach before the next one is sent. 6 -> 7 -> 15, from the commissioning
notes. Writing all three blind is the failure this pairing prevents: if the
drive did not reach SWITCHED_ON, sending 15 asks a drive in an unknown state to
energise its output."""

ENERGISING_WORD: Final[ControlWord] = ControlWord.ENABLE_OPERATION
"""The one word in :data:`ENABLE_SEQUENCE` that can put torque on the shaft.

Named rather than tested for by position, because the consequence attaches to
the word and not to the index: a failure at this step means the output stage
may be live, so it gets a rollback and :class:`~src.motor.drive.
EnableUnconfirmed` instead of the bare transport error.
"""

STOP_WORDS: Final[frozenset[ControlWord]] = frozenset({ControlWord.SWITCH_ON, ControlWord.SHUTDOWN})
"""The two words that can only ever reduce torque demand.

These stay writable even when the comms latch is down. The latch exists to stop
this driver feeding a keepalive to a drive it can no longer verify - but a
refusal that also blocks a STOP is a refusal that keeps a motor commanded, and
:meth:`ATV320Drive.enable` failing repeatedly is precisely how a caller ends up
latched while holding the enable it now wants to undo. One stop attempt costs
one frame of ttO grace; refusing it costs the stop.
"""

STANDSTILL_RPM: Final[MotorRpm] = MotorRpm(1)
"""At or below this the shaft is taken to be stopped.

1 rpm is the finest speed RFRD can report, so nothing observable is being
discarded. It is a MOTOR-shaft figure: 1 motor rpm is 0.02 output rpm.
"""

DEFAULT_STOP_ATTEMPTS: Final[int] = 40
"""RFRD reads allowed while waiting for the shaft to stop in :meth:`close`.

Bounded by a count rather than only by a clock, so the wait terminates even if
the clock is not advancing. 40 x 0.5 s is 20 s, which is comfortably longer than
the commissioned 3-4 s ``dEC`` ramp and far shorter than the ~145 s freewheel
this whole sequence exists to avoid. Reaching the end of it is not a timeout to
retry; it is evidence the ramp is not what the notes claim.
"""

DEFAULT_STOP_POLL_INTERVAL: Final[Seconds] = Seconds(0.5)
"""Pause between those RFRD reads. Slow on purpose: nothing is being controlled
here, and a tight poll would spend the bus for no information."""


def _new_executor() -> ThreadPoolExecutor:
    """One worker thread, so the half-duplex bus is serialised by construction.

    A second worker would let two transactions overlap on the wire the moment
    anything bypassed the ``asyncio.Lock``. The thread is created lazily on the
    first submission, so building this is free.
    """
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="atv320")


# Why this module uses `isinstance(outcome, Err)` early returns rather than
# `match Ok()/Err()` throughout: it never BRANCHES on which DriveError it got,
# it only propagates one. The nested-match-plus-`assert_never` discipline of
# contract rule 3 exists for callers that choose a different response per
# variant - the safety layer - and it earns its keep there. Here it would buy
# nothing, and every such match leaves an unreachable "no case matched"
# fall-through arc that the 100%-branch gate cannot close without a `pragma`.
# `isinstance` narrows in BOTH directions, so the success value stays fully
# typed with no sentinel and no possibly-undefined local.


@final
class ATV320Drive:
    """``DriveBackend`` over Modbus RTU. Never raises on the drive path.

    Construction is inert: no port is opened and no thread is started until
    :meth:`open` and the first transaction. The ``master`` is injected rather
    than built here so the whole error-mapping path can be tested against a
    fake bus, and so production has exactly one call to :func:`serial_master`
    to audit.

    ``registers`` has no default on purpose. The addressing offset is the one
    piece of configuration that can turn a correct speed write into a write
    into ACC/DEC/HSP, so it must be stated by the caller rather than defaulted
    to a value nobody vouched for.
    """

    __slots__ = (
        "_clock",
        "_close_error",
        "_closed",
        "_emergency_budget",
        "_exchange_log",
        "_executor",
        "_health",
        "_lock",
        "_master",
        "_registers",
        "_settings",
        "_settle_attempts",
        "_settle_delay",
        "_stop_attempts",
        "_stop_poll_interval",
        "_wire_lock",
    )

    def __init__(
        self,
        clock: Clock,
        master: ModbusMaster,
        settings: SerialSettings,
        registers: RegisterMap,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        settle_attempts: int = DEFAULT_SETTLE_ATTEMPTS,
        settle_delay: Seconds = DEFAULT_SETTLE_DELAY,
        stop_attempts: int = DEFAULT_STOP_ATTEMPTS,
        stop_poll_interval: Seconds = DEFAULT_STOP_POLL_INTERVAL,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(
                f"failure_threshold {failure_threshold} must be at least 1; a "
                "threshold of 0 would latch the link down before any transaction "
                "had been attempted"
            )
        if settle_attempts < 1:
            raise ValueError(
                f"settle_attempts {settle_attempts} must be at least 1; with 0 the "
                "enable sequence would never read ETA and so would verify nothing"
            )
        if stop_attempts < 1:
            raise ValueError(
                f"stop_attempts {stop_attempts} must be at least 1; with 0 close() "
                "would never read RFRD, so it could never confirm standstill and "
                "would leave the run command in place on a machine that had stopped"
            )
        # THE BOUND THAT MAKES THE EMERGENCY STOP'S PROMISE MEAN SOMETHING. A
        # blocking serial call cannot be interrupted once started, so the only
        # way to promise the emergency path returns inside its budget is to
        # derive that budget from what the line can really cost, and to refuse
        # a line whose cost is unacceptable. Refusing at startup, with nothing
        # spinning, is the right place: the alternative is discovering it
        # during shutdown, which is when it hangs the Pi.
        emergency_budget = emergency_budget_for(settings)
        if emergency_budget > MAX_EMERGENCY_BUDGET:
            raise ValueError(
                f"serial timeout {settings.timeout} s with retries={settings.retries} "
                f"gives a worst-case transaction of {transaction_worst_case(settings):.3f} s "
                f"and so an emergency-stop bound of {emergency_budget:.3f} s, over the "
                f"{MAX_EMERGENCY_BUDGET} s this driver accepts. Lower the serial timeout "
                "or retries - do not leave the emergency stop with a bound that long."
            )
        self._clock: Clock = clock
        self._exchange_log: ExchangeLog | None = None
        self._master: ModbusMaster = master
        self._settings: SerialSettings = settings
        self._registers: RegisterMap = registers
        self._settle_attempts: int = settle_attempts
        self._settle_delay: Seconds = settle_delay
        self._stop_attempts: int = stop_attempts
        self._stop_poll_interval: Seconds = stop_poll_interval
        self._emergency_budget: Seconds = emergency_budget

        # WHY a lock and not just careful call ordering: RS-485 is half duplex.
        # Two coroutines that each write-then-read do not interleave politely,
        # they transmit over each other and read each other's replies.
        self._lock: asyncio.Lock = asyncio.Lock()
        # WHY a SECOND, threading lock: the asyncio one only orders coroutines,
        # and the emergency path is not one - it drives the shared pymodbus
        # client from the CALLING thread while the executor thread may be
        # mid-transaction. pymodbus holds its own transaction lock around the
        # transaction body but calls connect() OUTSIDE it and close()s on every
        # no-response, so `socket` is None between transactions and both threads
        # can race to open an exclusive=True port. The loser's connect() returns
        # False, execute raises ConnectionException, and the emergency write is
        # lost in the middle of a comms fault. This lock closes that window;
        # the emergency path takes it with a BOUNDED wait so it degrades to
        # "write anyway" rather than queueing behind the fault it is reacting to.
        self._wire_lock: threading.Lock = threading.Lock()
        self._executor: ThreadPoolExecutor = _new_executor()

        self._health: LinkHealth = LinkHealth(failure_threshold)

        # Close accounting, so a second close() repeats the first verdict
        # instead of raising on an executor that is already gone.
        self._closed: bool = False
        self._close_error: DriveError | None = None

    # --- Observable state ------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self._health.acquisition_evidence

    @property
    def link_lost(self) -> bool:
        """Whether the link is latched down and this driver has stopped writing.

        Exposed so an operator screen can say "link latched, open() required"
        instead of showing a generic timeout for ever.

        Note what it does NOT mean: a latched link still accepts the two
        :data:`STOP_WORDS` and a zero setpoint. See :meth:`_refusal`.
        """
        return self._health.lost_since is not None

    @property
    def emergency_budget(self) -> Seconds:
        """The smallest whole-call bound the emergency stop can actually honour.

        Derived from the line, never configured: :func:`emergency_budget_for`
        of the :class:`SerialSettings` this driver was built with, i.e. two
        worst-case pymodbus transactions plus scheduling headroom. Exposed so
        the training runtime, an ``atexit`` or a signal handler can pass a
        budget this driver is able to keep - ``drive.emergency_disable_blocking(
        drive.emergency_budget)`` - instead of inventing a number that the
        serial timeouts would silently overrun.
        """
        return self._emergency_budget

    # =====================================================================
    # DriveBackend
    # =====================================================================

    async def open(self) -> Result[None, DriveError]:
        """Acquire the link and prove the addressing. Commands nothing.

        The ETA read at the end is the only evidence that the unit address and
        ``RegisterMap.offset`` are both usable, and it is a **read**: an offset
        discovered to be wrong by writing has already written a speed into a
        neighbouring parameter. An address the drive does not implement answers
        with exception code 2, which is what makes this a real check.

        This is also the only place the comms latch is cleared, so re-arming a
        link that dropped is an explicit act.
        """
        async with self._lock:
            generation = self._health.acquisition_generation
            # A previous close() shut the pool down; a fresh one keeps this
            # object reusable without a branch only production would take.
            self._executor.shutdown(wait=False)
            self._executor = _new_executor()
            self._closed = False
            self._close_error = None

            match await self._run(self._blocking_connect):
                case Err(error):
                    return Err(self._note_failure(error, self._clock.monotonic()))
                case Ok():
                    return await self._confirm_addressing(generation)
                case _ as unreachable:
                    assert_never(unreachable)

    async def close(self) -> Result[None, DriveError]:
        """Ramp the machine to a stop, then release the port. Latches the link down.

        Dropping the port while the drive is in OPERATION_ENABLED would leave a
        commanded motor with nothing talking to it, relying entirely on the
        drive's ``ttO`` timeout. So the stop is attempted first - and the port
        is released either way, because a driver that will not let go of a
        serial port because a write failed is a driver nobody can restart.

        **This method waits, and the waiting is the safety feature.** See
        :meth:`_attempt_stop`: LFRD = 0, then RFRD polled to standstill, and
        only then the two command words. It does NOT write SHUTDOWN to a
        turning machine, because that is CiA402 transition 8 and freewheels.

        The error returned is the stop attempt's, not the port's: the caller
        needs to know the motor may still be commanded, which matters strictly
        more than how tidily the file descriptor went away.

        Idempotent, and it has to be: a teardown path may well call it from an
        ``except`` branch and again from a ``finally``, and the first call has
        already shut the executor down, so a second attempt at a transaction
        would raise ``RuntimeError`` out of a shutdown handler. The second call
        repeats the first verdict rather than inventing a cheerful ``Ok`` for a
        stop that may never have landed.
        """
        async with self._lock:
            if self._closed:
                logger.debug(
                    "ATV320 close() called again on %s; repeating the first verdict "
                    "(%r) rather than touching a pool that is already gone",
                    self._settings.port,
                    self._close_error,
                )
                return self._closed_result()
            self._closed = True
            self._close_error = await self._attempt_stop()
            await self._run(self._blocking_close)
            self._executor.shutdown(wait=False)
            self._health.latch(self._clock.monotonic())
            return self._closed_result()

    def _closed_result(self) -> Result[None, DriveError]:
        """The verdict of the one and only stop attempt this object made."""
        error = self._close_error
        if error is not None:
            return Err(error)
        return Ok(None)

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        """Write CMD. Enabling the output stage means the motor may turn.

        Unverified on purpose: CMD has no meaningful echo of its own, and the
        evidence that a command word landed is the state the drive moves to.
        :meth:`enable` is the verified path; a caller using this primitive is
        responsible for reading ETA afterwards.

        A latched link refuses ENABLE_OPERATION and FAULT_RESET but still
        accepts the two :data:`STOP_WORDS`. A refusal that blocked a stop would
        be a refusal that keeps a motor commanded.
        """
        async with self._lock:
            refusal = self._refusal(stopping=word in STOP_WORDS)
            if refusal is not None:
                return Err(refusal)
            return await self._write(self._registers.cmd, RawRegister(word.value))

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        """Write LFRD, a signed MOTOR-shaft setpoint, and read it back.

        The read-back exists because a Modbus write response echoes the
        *request*: a write the drive did not honour - a value it clamped or
        refused, or an address that is not actually the speed reference - is
        acked exactly like a write that landed, and every later read still
        looks normal. Reading LFRD back is the only thing that can tell the
        difference.

        Its limit, stated plainly because overstating it would be worse than
        not having it: if the offset is wrong by the SAME amount everywhere,
        the write and the read-back go to the same wrong register, agree, and
        this check passes. A uniform offset is caught by the ETA read in
        :meth:`open` and by exception responses from addresses the drive does
        not implement - and finally only by the bench. A test pins that limit,
        so nobody reads more into this guard than it actually gives.

        The comparison is exact, deliberately. If the bench shows the drive
        clamping LFRD to HSP (so the echo legitimately differs from a setpoint
        above the ceiling), the fix is to keep the software ceiling below HSP -
        which the safety layer does anyway - and NOT to loosen this check: a
        tolerance wide enough to absorb clamping is wide enough to absorb a
        write that never landed.

        A setpoint of zero survives the comms latch, for the same reason the
        :data:`STOP_WORDS` do: it can only ever reduce torque demand. Any other
        setpoint is refused.
        """
        async with self._lock:
            refusal = self._refusal(stopping=rpm == 0)
            if refusal is not None:
                return Err(refusal)
            # Signed 16-bit at the boundary: -1 rpm goes out as 0xFFFF, and
            # read back as unsigned it would look like 65535 rpm and sail
            # through any ceiling expressed as "less than 900".
            encoded = signed_to_register(rpm)
            if isinstance(encoded, Err):
                return Err(encoded.error)
            return await self._write_verified_speed(rpm, encoded.value)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        """Read ETA, LFRD, RFRD and LCR (plus LFT when faulted) as one observation.

        Every register is parsed and range-checked here, at the boundary, so
        nothing downstream re-validates: that is contract rule 6.
        """
        async with self._lock:
            refusal = self._refusal(stopping=False)
            if refusal is not None:
                return Err(refusal)
            return await self._assemble_status()

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        """Read ONE holding register, range-checked. Nothing is written.

        For operator tooling - the bench console and the link-latency script -
        that needs one register timed on its own rather than a whole
        :meth:`read_status`. It is the same transaction every other read here
        is: single flight, executor thread, failure accounting, and refused
        once the link is latched, so a tool cannot hammer a link this driver
        has already declared lost.

        ``address`` is used as given: resolve it through the same
        :class:`~src.motor.drive.RegisterMap` the driver was built with, so a
        tool and the driver cannot disagree about the offset.
        """
        async with self._lock:
            refusal = self._refusal(stopping=False)
            if refusal is not None:
                return Err(refusal)
            return await self._read(address)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        """Read tFr, HSP, LSP, ACC and dEC, parsed. Five READS; nothing is written.

        Deliberately no judgement here: whether HSP is acceptable depends on
        whether the arm is coupled, which is the installation's call
        (:func:`~src.motor.drive.check_limits`), not the transport's. Short-
        circuits on the first failure, like :meth:`read_status`, and a latched
        link refuses it like any other non-stop exchange.
        """
        async with self._lock:
            refusal = self._refusal(stopping=False)
            if refusal is not None:
                return Err(refusal)
            values: list[RawRegister] = []
            for address in (
                self._registers.tfr,
                self._registers.hsp,
                self._registers.lsp,
                self._registers.acc,
                self._registers.dec,
            ):
                outcome = await self._read(address)
                if isinstance(outcome, Err):
                    return Err(outcome.error)
                values.append(outcome.value)
            limits = decode_limits(
                tfr=values[0], hsp=values[1], lsp=values[2], acc=values[3], dec=values[4]
            )
            logger.info("ATV320 limits on %s: %s", self._settings.port, limits.describe())
            return Ok(limits)

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        """Zero the setpoint, synchronously, unconditionally. Never raises.

        **No asyncio.** This runs in the calling thread so it works from
        ``atexit``, from an OS signal handler, and from an ``except`` branch -
        places where the loop may be gone or may be the thing that died. It
        does not take the ``asyncio.Lock`` either, for the same reason.

        **One blind write: LFRD = 0. The run command is left in place.** This
        is the fastest stop actually available, and the reason is CiA402: out
        of OPERATION_ENABLED, CMD = SHUTDOWN is transition 8, which drops the
        output stage and FREEWHEELS a loaded centrifuge for minutes while the
        drive reports READY. A zeroed reference leaves the drive in control of
        its own commissioned ramp, and once this process stops writing, the
        drive's ``ttO`` timeout ramps it down for real. So this call ends in a
        RAMP, not a freewheel - which is exactly why it does not touch CMD.

        The write is **unconditional**: no budget check can skip it. The budget
        buys the wait for :attr:`_wire_lock` and nothing else, because the one
        write this method exists to send is never the write to sacrifice.

        The bound is real rather than advisory, and it is two transactions, not
        one. A blocking serial call cannot be cut short once started, and one
        pymodbus transaction can spend the serial timeout several times over
        (:func:`transaction_worst_case`). If the executor thread is
        mid-transaction, this write cannot overlap it either way - pymodbus
        serialises ``execute`` with its own ``RLock`` - so it waits for that
        transaction to end (at most one worst case after this call began) and
        then spends at most one more. :attr:`emergency_budget` is exactly that
        sum plus headroom, the constructor refuses a line that makes it
        unacceptably long, and the lock wait is whatever the budget leaves
        after reserving this call's own transaction. A ``timeout`` smaller than
        that floor cannot be honoured and is reported rather than obeyed:
        returning early without writing would be the one outcome that is never
        safer.

        The honest residual, stated rather than glossed: pymodbus calls
        ``connect()`` from inside ``write_register`` when its socket is closed,
        and opening a device - a tty, or the USB interface of the FTDI cable -
        is an OS call that no read timeout bounds. So the guarantee is "at most
        two transactions, each bounded by its timeouts" and not "no syscall can
        ever be slow".

        The reply is classified but never acted on: no retry, no diagnosis, no
        second exchange. It costs no wire time (pymodbus has already waited for
        it) and it is what lets :class:`~src.motor.drive.EmergencyStopOutcome`
        tell ``NOTHING_SENT`` from success, so an ``atexit`` path can escalate.

        Returning does **not** mean the motor stopped. With STO jumpered there
        is no independent torque removal and the ramp takes seconds; this
        reports only what the attempt achieved. It then latches the link down
        so the async side cannot re-command a speed on the next tick of a loop
        that is still alive - a stop, however, still gets through the latch.
        """
        budget = self._emergency_budget_for(timeout)
        outcome = self._blind_write(self._registers.lfrd, RawRegister(0), budget)
        self._health.latch(self._clock.monotonic())
        logger.error(
            "ATV320 emergency disable on %s: LFRD=0 %s; the run command was left in "
            "place so the drive ramps on its own dEC and its ttO timeout stops it for "
            "real. The machine is NOT stopped yet.",
            self._settings.port,
            outcome.name,
        )
        return outcome

    def _emergency_budget_for(self, timeout: Seconds) -> Seconds:
        """The bound this driver will actually keep for one emergency call.

        Three cases, and each one is logged rather than silently absorbed,
        because the caller's number and the achievable number differing is a
        configuration fact somebody needs to fix before a session:

        * non-positive - rejected outright. ``Seconds(0.0)`` used to mean "skip
          both writes", i.e. a silent no-op that also latched the link so the
          following ``close()`` wrote nothing either. There is no reading of
          "stop the motor in zero seconds" that justifies sending nothing.
        * below the floor - cannot be honoured, because the line settings it
          would have to fit inside were fixed at construction. The floor is used
          and said out loud.
        * at or above the floor - taken as given.
        """
        floor = self._emergency_budget
        if timeout <= 0.0:
            logger.error(
                "ATV320 emergency disable: a timeout of %s s is not a budget; using "
                "%s s. Writing nothing would have been the only unsafe answer.",
                timeout,
                floor,
            )
            return floor
        if timeout < floor:
            logger.error(
                "ATV320 emergency disable: a budget of %s s cannot be honoured - a "
                "transaction on %s (timeout %s s, retries %d) can block for %.3f s, so "
                "the smallest real bound is %.3f s. Using %.3f s.",
                timeout,
                self._settings.port,
                self._settings.timeout,
                self._settings.retries,
                transaction_worst_case(self._settings),
                floor,
                floor,
            )
            return floor
        return timeout

    # =====================================================================
    # The CiA402 start sequence
    # =====================================================================

    async def enable(self) -> Result[None, DriveError]:
        """Take the drive to OPERATION_ENABLED, verifying ETA at every step.

        6 -> 7 -> 15, and after each word the drive must actually report the
        state that word asks for before the next one is sent. Writing the three
        blind would mean asking a drive in an unknown state to energise its
        output, and a drive that did not follow would be indistinguishable from
        one that did.

        A fault seen at any point returns :class:`DriveFaulted` with the LFT
        code rather than ``UnexpectedState(..., FAULT)``: the operator needs the
        mnemonic, and "unexpected state" understates a latched fault.

        Holds the single-flight lock for the whole sequence (six transactions,
        ~120 ms). Letting another command interleave between 7 and 15 would be
        worse than the wait; the emergency path does not use this lock.

        **What this does NOT do:** it does not zero the setpoint first. If LFRD
        still holds a non-zero value from an earlier session, writing 15 starts
        the motor at that speed. Whether to refuse that is policy - it depends
        on whether somebody is in the machine - so the safety layer must write
        speed 0 before calling this. A test pins the fact that ``enable``
        writes no speed of its own, so the omission stays a decision rather
        than becoming an oversight.
        """
        async with self._lock:
            refusal = self._refusal(stopping=False)
            if refusal is not None:
                return Err(refusal)
            return await self._run_enable_sequence()

    async def _run_enable_sequence(self) -> Result[None, DriveError]:
        """The sequence itself. Lock already held."""
        # Look before writing anything: a faulted drive cannot be enabled, and
        # sending SHUTDOWN to it would only be the first of three pointless
        # writes.
        observed = await self._observe_state()
        if isinstance(observed, Err):
            return Err(observed.error)
        if observed.value is DriveState.FAULT:
            return Err(await self._fault_error())

        for word, expected in ENABLE_SEQUENCE:
            step = await self._enable_step(word, expected)
            if isinstance(step, Err):
                if word is ENERGISING_WORD:
                    return Err(await self._unwind_energised(step.error))
                return step
        return Ok(None)

    async def _unwind_energised(self, cause: DriveError) -> DriveError:
        """Undo an enable that may have landed, and report that it may have.

        Reached when the step carrying :data:`ENERGISING_WORD` failed. The
        failure says nothing about whether the word landed: a Modbus request
        whose reply was lost was still transmitted, and the observed case is
        ``[6, 7, 15]`` all on the wire, ETA decoding to OPERATION_ENABLED, and
        ``Err(CommTimeout)`` returned - which reads as "the link died" while
        the output stage is live and LFRD may still hold a speed from an
        earlier session.

        So: zero the reference, then remove the run command with SWITCH_ON
        (transition 5, which ramps - **not** SHUTDOWN, which would drop the
        output stage on a machine that may already be turning), and report
        :class:`~src.motor.drive.EnableUnconfirmed` carrying whether either
        write was acknowledged. Both ``False`` means nothing is known to have
        undone the enable and only ttO remains.

        These two writes go through the normal accounting on purpose. If the
        link really has died they add to the failure run and latch it, which is
        correct - and a latched link still accepts a stop, so a caller can
        still undo this by hand.
        """
        reference = err_of(await self._write(self._registers.lfrd, RawRegister(0)))
        run_command = err_of(
            await self._write(self._registers.cmd, RawRegister(ControlWord.SWITCH_ON.value))
        )
        unconfirmed = EnableUnconfirmed(
            detail=(
                f"the {ENERGISING_WORD.name} step failed with {cause!r}, AFTER the word "
                "may already have reached the drive, so the output stage may be live. "
                f"Rollback: LFRD=0 {'acked' if reference is None else f'failed ({reference!r})'}, "
                f"CMD=SWITCH_ON "
                f"{'acked' if run_command is None else f'failed ({run_command!r})'}."
            ),
            reference_zeroed=reference is None,
            run_command_removed=run_command is None,
        )
        logger.error("ATV320 enable on %s: %s", self._settings.port, unconfirmed.detail)
        return unconfirmed

    async def _enable_step(
        self, word: ControlWord, expected: DriveState
    ) -> Result[None, DriveError]:
        """Write one command word and confirm the drive reached ``expected``."""
        written = await self._write(self._registers.cmd, RawRegister(word.value))
        if isinstance(written, Err):
            return written
        settled = await self._settled_state(expected)
        if isinstance(settled, Err):
            return Err(settled.error)
        if settled.value is expected:
            return Ok(None)
        if settled.value is DriveState.FAULT:
            return Err(await self._fault_error())
        return Err(UnexpectedState(expected=expected, actual=settled.value))

    async def _settled_state(self, expected: DriveState) -> Result[DriveState, DriveError]:
        """Read ETA until it reports ``expected``, at most ``settle_attempts`` times.

        Returns the LAST state observed so the caller can report what it
        actually saw. A FAULT returns immediately: there is nothing to wait for,
        and sleeping through a fault reaction is sleeping while a loaded
        centrifuge decelerates.
        """
        outcome = await self._observe_state()
        for _ in range(self._settle_attempts - 1):
            if isinstance(outcome, Err):
                return outcome
            if outcome.value is expected or outcome.value is DriveState.FAULT:
                return outcome
            await asyncio.sleep(self._settle_delay)
            outcome = await self._observe_state()
        return outcome

    async def _fault_error(self) -> DriveError:
        """Name the latched fault from LFT so the caller gets a mnemonic, not a state."""
        outcome = await self._read(self._registers.lft)
        if isinstance(outcome, Err):
            # The link died while we were asking which fault it was. That
            # failure is now the more urgent one, and inventing a fault name
            # here would be worse than reporting the link.
            return outcome.error
        report = describe_fault(outcome.value)
        return DriveFaulted(fault=report.fault, raw_code=report.raw_code)

    # =====================================================================
    # Composite operations (the single-flight lock is already held)
    # =====================================================================

    async def _confirm_addressing(self, generation: int) -> Result[None, DriveError]:
        """One ETA read, purely as evidence that the link and addressing work."""
        match await self._read(self._registers.eta):
            case Err(error):
                return Err(error)
            case Ok(eta):
                self._health.confirm_acquisition(generation)
                # Logged rather than judged: seeing OPERATION_ENABLED here means a
                # previous process died with the motor turning, and what to do about
                # that (command zero, disable, latch, require an operator
                # acknowledgement) is the safety layer's decision, not the transport's.
                logger.info(
                    "ATV320 link open on %s: ETA=0x%04X -> %s",
                    self._settings.port,
                    eta,
                    decode_status_word(StatusWord(eta)).name,
                )
                return Ok(None)
            case _ as unreachable:
                assert_never(unreachable)

    async def _attempt_stop(self) -> DriveError | None:
        """Ramp to a stop, confirm it, and only then drop the output stage.

        The order is the whole point, and it is the order the CiA402 profile
        requires rather than the one that is quickest to write:

        1. **LFRD = 0.** The drive starts decelerating down its commissioned
           ``dEC`` ramp while still in full control of the motor.
        2. **Poll RFRD to standstill.** Bounded by ``stop_attempts``. This step
           is the one that was missing: the previous version wrote SHUTDOWN
           ~20 ms after zeroing the reference, which is CiA402 transition 8 -
           the output stage dropped and a loaded centrifuge left freewheeling.
           Against this repo's own plant model that is standstill at t = 144.6 s
           instead of t = 10.0 s, and the drive reports READY throughout.
        3. **SWITCH_ON, then SHUTDOWN.** Transition 5 then transition 2, on a
           machine that has already stopped, so there is nothing left to
           freewheel. The stop pair from the commissioning notes, in order.

        If standstill cannot be CONFIRMED - the RFRD read failed, or the shaft
        was still turning when the attempts ran out - step 3 is deliberately
        skipped and the drive is left in OPERATION_ENABLED with a zero
        reference. That is not giving up: it is choosing ttO's ramp over
        transition 8's freewheel, which is the better of the two endings
        available at that point. The caller is told which one it got.

        No comms-latch refusal here. This is a stop, and a latched link that
        cannot be stopped is the failure the latch was supposed to prevent.
        """
        zeroed = err_of(await self._write(self._registers.lfrd, RawRegister(0)))
        if zeroed is not None:
            logger.error(
                "ATV320 close on %s: could not zero LFRD (%r). The run command is left "
                "in place so the drive's ttO timeout ramps the motor down; the machine "
                "is NOT known to be stopped.",
                self._settings.port,
                zeroed,
            )
            return zeroed

        halted = await self._wait_for_standstill()
        if isinstance(halted, Err):
            logger.error(
                "ATV320 close on %s: standstill not confirmed (%r). Leaving the run "
                "command in place: removing it now would be CiA402 transition 8 on a "
                "machine that may still be turning, i.e. a freewheel. The drive's ttO "
                "timeout will ramp it down instead.",
                self._settings.port,
                halted.error,
            )
            return halted.error

        logger.info(
            "ATV320 close on %s: shaft at %d rpm, removing the run command",
            self._settings.port,
            halted.value,
        )
        # Transition 5 first, then transition 2. SHUTDOWN is attempted even if
        # SWITCH_ON failed: the shaft is already stopped, so there is no
        # freewheel left to cause, and the first error is the one reported
        # because it happened first.
        disabled = err_of(
            await self._write(self._registers.cmd, RawRegister(ControlWord.SWITCH_ON.value))
        )
        removed = err_of(
            await self._write(self._registers.cmd, RawRegister(ControlWord.SHUTDOWN.value))
        )
        if disabled is not None:
            return disabled
        return removed

    async def _wait_for_standstill(self) -> Result[MotorRpm, DriveError]:
        """Read RFRD until the shaft has stopped, at most ``stop_attempts`` times.

        RFRD and not ETA, and this is the reason the whole method exists: no
        status word distinguishes "decelerating on a ramp" from "stopped", and
        several of them - READY, NOT_READY, FAULT - read as reassuring while a
        centrifuge is still at hundreds of rpm. Only the measured speed speaks
        about motion.

        Bounded by a COUNT as well as by the clock, so this terminates even
        under a clock that is not advancing. Exhausting the count is not a
        transport failure and must not be reported as one: it means the ramp is
        slower than the commissioning notes claim, which is a fact about the
        machine, so it gets :class:`~src.motor.drive.StopUnconfirmed` carrying
        the last speed actually seen.
        """
        started = self._clock.monotonic()
        # Never read: the constructor refuses stop_attempts < 1, so the loop
        # below always assigns this before anything looks at it. It is here
        # because a checker cannot know that range(n) with n >= 1 runs at least
        # once, and a sentinel is better than silencing the check.
        observed = MotorRpm(0)
        for attempt in range(self._stop_attempts):
            if attempt > 0:
                await asyncio.sleep(self._stop_poll_interval)
            reading = await self._read(self._registers.rfrd)
            if isinstance(reading, Err):
                return Err(reading.error)
            observed = decode_speed(reading.value)
            if abs(observed) <= STANDSTILL_RPM:
                return Ok(observed)
        waited = elapsed(started, self._clock.monotonic())
        return Err(
            StopUnconfirmed(
                waited=waited,
                last_output_rpm=observed,
                detail=(
                    f"RFRD still reported {observed} rpm after {self._stop_attempts} "
                    f"reads over {waited:.1f} s, so the shaft is NOT known to have "
                    "stopped. The run command has been left in place on purpose: the "
                    "drive keeps ramping and its ttO timeout finishes the job, whereas "
                    "removing it now would drop the output stage on a turning "
                    "centrifuge. Check the commissioned dEC ramp time."
                ),
            )
        )

    async def _write_verified_speed(
        self, rpm: MotorRpm, value: RawRegister
    ) -> Result[None, DriveError]:
        """Write LFRD then read it back. See :meth:`write_speed` for why."""
        written = await self._write(self._registers.lfrd, value)
        if isinstance(written, Err):
            return written
        echo = await self._read(self._registers.lfrd)
        if isinstance(echo, Err):
            return Err(echo.error)
        landed = decode_speed(echo.value)
        if landed == rpm:
            return Ok(None)
        return Err(
            BadResponse(
                detail=(
                    f"LFRD write-verify failed: wrote {rpm} rpm (register "
                    f"0x{value:04X}) to address {self._registers.lfrd}, read back "
                    f"{landed} rpm (0x{echo.value:04X}). The setpoint did NOT land. "
                    "Suspect a value the drive refused or clamped, or an address that "
                    "is not the speed reference at all."
                )
            )
        )

    async def _assemble_status(self) -> Result[DriveStatus, DriveError]:
        """Build one :class:`DriveStatus`. Lock already held."""
        readings = await self._read_status_registers()
        if isinstance(readings, Err):
            return Err(readings.error)
        eta_raw, lfrd_raw, rfrd_raw, lcr_raw = readings.value
        word = StatusWord(eta_raw)
        state = decode_status_word(word)
        named = await self._read_fault(state)
        if isinstance(named, Err):
            return Err(named.error)
        report = named.value
        return Ok(
            DriveStatus(
                state=state,
                status_word=word,
                setpoint_echo_rpm=decode_speed(lfrd_raw),
                output_rpm=decode_speed(rfrd_raw),
                current=decode_current(lcr_raw),
                fault=None if report is None else report.fault,
                fault_code=None if report is None else report.raw_code,
            )
        )

    async def _read_status_registers(
        self,
    ) -> Result[tuple[RawRegister, RawRegister, RawRegister, RawRegister], DriveError]:
        """Read ETA, LFRD, RFRD and LCR in that order, stopping at the first failure.

        ETA first so the state is the oldest thing in the observation rather
        than the newest: a state read after the speeds could name a state that
        never coexisted with them.

        Short-circuits, because on a dead link four reads would spend four
        serial timeouts - more than the whole 200 ms control budget - learning
        the same thing four times.

        Four separate single-register transactions, not Modbus block reads.
        ETA(3201)..LCR(3204) and LFRD(8602)..RFRD(8604) look contiguous, but the
        addresses in between are unrelated parameters, and a block read spanning
        one the drive does not implement is answered with an exception response
        for the WHOLE block - so the optimisation costs the entire status, not
        one field. It becomes available once the bench has confirmed those
        addresses read, and never by assumption. At 19200 8E1 a transaction is
        ~20 ms, so a status is ~80 ms of the 200 ms available at 5 Hz, ~100 ms
        when faulted.
        """
        values: list[RawRegister] = []
        for address in (
            self._registers.eta,
            self._registers.lfrd,
            self._registers.rfrd,
            self._registers.lcr,
        ):
            outcome = await self._read(address)
            if isinstance(outcome, Err):
                return Err(outcome.error)
            values.append(outcome.value)
        # Exactly four appends in the order above. The indices never leave this
        # function; the caller unpacks named values.
        return Ok((values[0], values[1], values[2], values[3]))

    async def _read_fault(self, state: DriveState) -> Result[FaultReport | None, DriveError]:
        """Name the latched fault, but only when the status word says there is one.

        LFT holds the LAST fault, so reading it unconditionally would hang a
        historical name on a healthy status - and spend a fifth transaction out
        of the control budget every cycle to do it.
        """
        if state is not DriveState.FAULT:
            return Ok(None)
        outcome = await self._read(self._registers.lft)
        if isinstance(outcome, Err):
            return Err(outcome.error)
        return Ok(describe_fault(outcome.value))

    async def _observe_state(self) -> Result[DriveState, DriveError]:
        """One ETA read, decoded."""
        outcome = await self._read(self._registers.eta)
        if isinstance(outcome, Err):
            return Err(outcome.error)
        return Ok(decode_status_word(StatusWord(outcome.value)))

    # =====================================================================
    # One transaction
    # =====================================================================

    async def _read(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        return await self._transact(lambda: self._blocking_read(address))

    async def _write(
        self, address: RegisterAddress, value: RawRegister
    ) -> Result[None, DriveError]:
        return await self._transact(lambda: self._blocking_write(address, value))

    async def _transact[T](self, fn: Callable[[], Result[T, DriveError]]) -> Result[T, DriveError]:
        """Run one blocking transaction off the loop and account for the outcome.

        ``run_in_executor`` here is not an optimisation. The same event loop
        serves the operator's emergency-stop endpoint, and a 0.3 s serial read
        taken on it is 0.3 s in which the button does nothing.
        """
        outcome = await self._run(fn)
        now = self._clock.monotonic()
        if isinstance(outcome, Err):
            return Err(self._note_failure(outcome.error, now))
        self._health.note_success()
        return outcome

    async def _run[T](self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    # =====================================================================
    # Failure accounting
    # =====================================================================

    def _refusal(self, *, stopping: bool) -> DriveError | None:
        """The error to return instead of touching a latched-down link.

        This is the "stops writing" half of the watchdog argument: once this
        driver has decided the link is gone it sends nothing more, so the drive
        stops hearing a keepalive and its own ``ttO`` timeout - the watchdog
        that does not live in this process - ramps the motor down.

        **A stop is never refused.** ``stopping=True`` - the two
        :data:`STOP_WORDS`, a zero setpoint, the close sequence - goes through
        the latch. The argument for the latch is that this driver must stop
        feeding a keepalive it cannot verify; it was never that a motor should
        stay commanded. And the shape of the bug it caused is real: a run of
        failures inside :meth:`enable` latches the link, and the caller then
        finds the very ``write_command(SHUTDOWN)`` it needs to undo that enable
        refused. One stop attempt costs one frame of ttO grace, which is
        nothing against leaving the machine running.

        Note what this does NOT do: it does not second-guess which stop word
        the caller chose. ``SHUTDOWN`` goes through the latch too, and on a
        turning machine that is a freewheel (see
        :class:`~src.motor.drive.ControlWord`). That is the caller's decision to
        make with ``write_command``, which is the unverified primitive;
        :meth:`close` and :meth:`emergency_disable_blocking` are the paths that
        own the sequencing and neither of them writes 6 to a moving shaft.
        """
        if self._health.lost_since is None:
            return None
        if stopping:
            logger.warning(
                "ATV320 %s: the link is latched down, but this is a stop - letting it "
                "through rather than refusing it",
                self._settings.port,
            )
            return None
        return CommTimeout(after=elapsed(self._health.lost_since, self._clock.monotonic()))

    def _note_failure(self, error: DriveError, now: Monotonic) -> DriveError:
        """Count a failed transaction; latch the link down at the threshold.

        Below the threshold the caller gets the specific diagnosis, because a
        library that refused the transaction and a drive that went silent need
        different responses. At the threshold it gets :class:`CommTimeout`
        measured from the first observed failure of the run, which is the
        number that says how long the drive has been out of contact.
        """
        timeout = self._health.note_failure(now)
        if timeout is None:
            logger.warning(
                "ATV320 transaction failed (%d/%d consecutive): %r",
                self._health.consecutive_failures,
                self._health.failure_threshold,
                error,
            )
            return error
        logger.error(
            "ATV320 link declared LOST after %d consecutive failures over %.3f s; this "
            "driver will send nothing further until open() is called again, so the "
            "drive's own ttO timeout applies. Last error: %r",
            self._health.consecutive_failures,
            timeout.after,
            error,
        )
        return timeout

    # =====================================================================
    # The blocking half: everything below runs in the executor thread
    # =====================================================================

    def _blocking_connect(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        try:
            with self._wire_lock:
                opened = self._master.connect()
        except Exception as exc:
            return Err(self._classify(exc, started))
        if not opened:
            return Err(CommTimeout(after=elapsed(started, self._clock.monotonic())))
        return Ok(None)

    def _blocking_close(self) -> None:
        """Release the port. Swallows everything: there is no useful failure here.

        A port that will not close cleanly is still a port this process has
        stopped using, and the caller's real question - "is the motor still
        commanded?" - is answered by the stop attempt, not by this.
        """
        try:
            with self._wire_lock:
                self._master.close()
        except Exception:
            logger.exception("ATV320: releasing %s failed", self._settings.port)

    def observe_exchanges(self, log: ExchangeLog) -> None:
        self._exchange_log = log
        if isinstance(self._master, ObservableDrive):
            self._master.observe_exchanges(log)

    def _observe_exchange(
        self,
        request: RegisterRequest,
        result: Result[RawRegister | None, DriveError],
    ) -> None:
        if self._exchange_log is not None:
            match result:
                case Ok(returned):
                    self._exchange_log.append(
                        Exchange(
                            at=request.at,
                            kind=request.kind,
                            register=request.register,
                            value=request.value if returned is None else returned,
                            ok=True,
                            latency_ms=(self._clock.monotonic() - request.at) * 1000,
                            detail="ok",
                        )
                    )
                case Err(error):
                    self._exchange_log.append(
                        Exchange(
                            at=request.at,
                            kind=request.kind,
                            register=request.register,
                            value=request.value,
                            ok=False,
                            latency_ms=(self._clock.monotonic() - request.at) * 1000,
                            detail=type(error).__name__,
                        )
                    )
                case _ as unreachable:
                    assert_never(unreachable)

    def _blocking_read(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        started = self._clock.monotonic()
        observed_started = started
        try:
            with self._wire_lock:
                self._health.note_request()
                if self._exchange_log is not None:
                    observed_started = self._clock.monotonic()
                reply: object = self._master.read_holding_registers(
                    address, count=1, slave=self._settings.slave_address
                )
        except Exception as exc:
            result: Result[RawRegister, DriveError] = Err(self._classify(exc, started))
        else:
            result = self._interpret_read(reply, address, started)
        self._observe_exchange(
            RegisterRequest(
                at=observed_started, kind=ExchangeKind.READ, register=address, value=None
            ),
            result,
        )
        return result

    def _blocking_write(
        self, address: RegisterAddress, value: RawRegister
    ) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        observed_started = started
        try:
            with self._wire_lock:
                self._health.note_request()
                if self._exchange_log is not None:
                    observed_started = self._clock.monotonic()
                reply: object = self._master.write_register(
                    address, value, slave=self._settings.slave_address
                )
        except Exception as exc:
            result: Result[None, DriveError] = Err(self._classify(exc, started))
        else:
            result = self._interpret_write(reply, address, started)
        self._observe_exchange(
            RegisterRequest(
                at=observed_started, kind=ExchangeKind.WRITE, register=address, value=value
            ),
            result,
        )
        return result

    def _blind_write(
        self, address: RegisterAddress, value: RawRegister, budget: Seconds
    ) -> EmergencyStopOutcome:
        """One fire-and-forget write for the emergency path. Never raises.

        Unconditional: there is no budget check that can stop this from being
        attempted, because this is the write the emergency path exists to send.
        The budget is spent on the lock wait only.

        The lock wait is bounded by what the budget leaves after reserving
        this write's own worst-case transaction (and the scheduling margin),
        and then **gives up and writes anyway**. With the real pymodbus client
        "anyway" still cannot put two frames on the wire at once - its own
        transaction ``RLock`` makes the write queue behind an in-flight
        ``execute`` - but it no longer waits behind this driver's lock, which
        also covers ``connect()``: a USB open that is hanging is not something
        the emergency write should sit out. Which branch ran is logged, because
        "the emergency write waited" and "it went without the lock" are
        different stories afterwards.

        The reply is classified but not acted upon - see
        :meth:`emergency_disable_blocking`.
        """
        lock_budget = Seconds(
            budget - transaction_worst_case(self._settings) - EMERGENCY_SCHEDULING_MARGIN
        )
        acquired = self._wire_lock.acquire(timeout=lock_budget)
        if acquired:
            logger.info(
                "ATV320 emergency disable: transport lock acquired, writing 0x%04X to "
                "register %d with the bus to itself",
                value,
                address,
            )
        else:
            logger.error(
                "ATV320 emergency disable: transport lock still held after %.3f s, so a "
                "transaction or a port open is in flight on the executor thread. Writing "
                "0x%04X to register %d ANYWAY - a frame that goes late beats no frame.",
                lock_budget,
                value,
                address,
            )
        started = self._clock.monotonic()
        try:
            self._health.note_request()
            reply: object = self._master.write_register(
                address, value, slave=self._settings.slave_address
            )
        except ConnectionException as exc:
            # pymodbus could not get (or keep) the port, so nothing was
            # transmitted. The one outcome that has to escalate.
            logger.exception(
                "ATV320 emergency disable: the transport refused to carry the write of "
                "0x%04X to register %d, so NO frame reached the drive. It is still "
                "commanded at whatever setpoint it held.",
                value,
                address,
            )
            if self._exchange_log is not None:
                self._observe_exchange(
                    RegisterRequest(
                        at=started, kind=ExchangeKind.WRITE, register=address, value=value
                    ),
                    Err(self._classify(exc, started)),
                )
            return EmergencyStopOutcome.NOTHING_SENT
        except Exception as exc:
            # Anything else: the library was already in the middle of something,
            # so the frame may or may not have gone out. "I do not know" is the
            # only honest answer and is never a dangerous one.
            logger.exception(
                "ATV320 emergency disable: write of 0x%04X to register %d raised; "
                "whether it reached the drive is unknown",
                value,
                address,
            )
            if self._exchange_log is not None:
                self._observe_exchange(
                    RegisterRequest(
                        at=started, kind=ExchangeKind.WRITE, register=address, value=value
                    ),
                    Err(self._classify(exc, started)),
                )
            return EmergencyStopOutcome.SENT_UNCONFIRMED
        finally:
            if acquired:
                self._wire_lock.release()
        result = self._interpret_write(reply, address, started)
        self._observe_exchange(
            RegisterRequest(at=started, kind=ExchangeKind.WRITE, register=address, value=value),
            result,
        )
        if isinstance(result, Err):
            logger.error(
                "ATV320 emergency disable: no usable acknowledgement for the write of "
                "0x%04X to register %d; it was transmitted but cannot be confirmed",
                value,
                address,
            )
            return EmergencyStopOutcome.SENT_UNCONFIRMED
        return EmergencyStopOutcome.ACKNOWLEDGED

    # --- Reply interpretation (pure apart from the clock) ----------------

    def _interpret_read(
        self, reply: object, address: RegisterAddress, started: Monotonic
    ) -> Result[RawRegister, DriveError]:
        """Turn whatever pymodbus handed back into one register value or a DriveError."""
        if isinstance(reply, ModbusException):
            # pymodbus 3.7.4's synchronous transaction manager RETURNS the
            # exception object when no reply arrives, while annotating the call
            # `-> ModbusPDU`. Same failure as a raised one, so same classifier.
            return Err(self._classify(reply, started))
        if isinstance(reply, ExceptionResponse):
            return Err(
                BadResponse(
                    detail=(
                        f"drive answered a read of register {address} with Modbus "
                        f"exception code {reply.exception_code}. Code 2 (illegal data "
                        "address) means the address does not exist on this drive: fix "
                        "RegisterMap.offset, do not retry."
                    )
                )
            )
        if not isinstance(reply, ModbusPDU):
            return Err(
                BadResponse(
                    detail=(
                        f"transport returned {type(reply).__name__} for register "
                        f"{address}, which is neither a Modbus PDU nor a ModbusException"
                    )
                )
            )
        try:
            registers: list[int] = reply.registers
        except AttributeError:
            # ModbusPDU declares `registers: list[int]` without ever assigning
            # it, so a PDU carrying no register block raises here instead of
            # answering empty. Reachable on a noisy half-duplex bus, where the
            # framer can decode a stale write echo as the reply to this read.
            return Err(
                BadResponse(
                    detail=(
                        f"reply to register {address} is a {type(reply).__name__}, which "
                        "carries no register data - the framer decoded the wrong PDU"
                    )
                )
            )
        if len(registers) != 1:
            return Err(
                BadResponse(
                    detail=f"expected exactly 1 register from {address}, got {len(registers)}"
                )
            )
        # Contract rule 6: this is the boundary, so the range is checked HERE
        # and never again. An out-of-domain value means a framing or decode
        # problem, not a measurement.
        return raw_register(registers[0])

    def _interpret_write(
        self, reply: object, address: RegisterAddress, started: Monotonic
    ) -> Result[None, DriveError]:
        """Confirm a write was acked. NOT that it landed where it was meant to.

        The echoed address and value are deliberately not compared: a Modbus
        write response echoes the *request*, so a write to the wrong address
        echoes back perfectly. Only a read of the register that was supposed to
        change can tell - see :meth:`_write_verified_speed`.

        The **function code** is compared, and that is a different claim. Being
        a ``ModbusPDU`` only means the framer decoded something; a
        ``ReadHoldingRegistersResponse`` is a perfectly good PDU and is what a
        noisy half-duplex pair hands back when a stale read reply lands in this
        exchange's window. Accepting it would report "the write was
        acknowledged" on the strength of somebody else's answer - the one
        failure mode this driver's whole write-verify argument assumes away.
        """
        if isinstance(reply, ModbusException):
            return Err(self._classify(reply, started))
        if isinstance(reply, ExceptionResponse):
            return Err(
                BadResponse(
                    detail=(
                        f"drive rejected a write to register {address} with Modbus "
                        f"exception code {reply.exception_code}. Code 2 (illegal data "
                        "address) means the address does not exist on this drive; code 3 "
                        "means the value is out of range for that parameter."
                    )
                )
            )
        if not isinstance(reply, ModbusPDU):
            return Err(
                BadResponse(
                    detail=(
                        f"transport returned {type(reply).__name__} for a write to register "
                        f"{address}, which is neither a Modbus PDU nor a ModbusException"
                    )
                )
            )
        if reply.function_code != WRITE_SINGLE_REGISTER_CODE:
            return Err(
                BadResponse(
                    detail=(
                        f"reply to a write of register {address} carries function code "
                        f"{reply.function_code}, not {WRITE_SINGLE_REGISTER_CODE} "
                        f"(write single register): the framer matched a "
                        f"{type(reply).__name__} to this exchange, so the write is NOT "
                        "acknowledged"
                    )
                )
            )
        return Ok(None)

    def _classify(self, exc: BaseException, started: Monotonic) -> DriveError:
        """Map one transport failure onto the closed ``DriveError`` union.

        ``after`` is measured, never the configured budget: a timeout firing at
        0.6 s when 0.3 s was allowed is a different problem from one firing at
        0.3 s, and only the measurement separates them.
        """
        waited = elapsed(started, self._clock.monotonic())
        if isinstance(exc, ConnectionException | ModbusIOException):
            # ConnectionException: the port would not open, or pymodbus closed
            # it after an I/O error. ModbusIOException: no reply, or a reply
            # that would not decode. Both mean "the drive did not answer".
            return CommTimeout(after=waited)
        if isinstance(exc, ModbusException):
            # ParameterException, NoSuchSlaveException, InvalidMessageReceived:
            # the library refused the transaction rather than the wire losing
            # it, so retrying is not the answer and the text is the diagnosis.
            return BadResponse(detail=f"pymodbus rejected the transaction: {exc}")
        if isinstance(exc, OSError):
            # serial.SerialException derives from OSError, as does a yanked USB
            # adapter. Caught by base class so pyserial need not be imported.
            return CommTimeout(after=waited)
        return BadResponse(
            detail=f"unexpected {type(exc).__name__} from the Modbus transport: {exc}"
        )
