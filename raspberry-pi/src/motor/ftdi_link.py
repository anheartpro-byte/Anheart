"""The Schneider USB-RS485 cable, driven from user space. The ONLY module that imports pyftdi.

Contract rule 5: ``pyftdi`` and ``usb`` (pyusb) are touched here and nowhere
else. :mod:`src.motor.atv320` plugs the port built here into pymodbus; nothing
above that sees any of it.

Why this module exists
----------------------

The bench cable is a Schneider TCSMCNAM3M002P "USB - RS485 SL": an FT232R
behind a Schneider vendor id (VID 0x16de, PID 0x0003). macOS binds no serial
driver to that id, so there is no ``/dev/cu.*`` for it, and the only way in is
libusb through pyftdi: ``ftdi://schneider:rs485/1``.

pyftdi ships a pyserial-compatible port for that URL, and pymodbus 3.7.4
accepts it - but that port's ``in_waiting`` is hard-coded to ``0`` ("not
implemented", ``pyftdi/serialext/protocol_ftdi.py``). pymodbus's serial client
decides a reply has arrived by polling ``in_waiting`` until it stops growing
(``ModbusSerialClient._wait_for_data``), so with a constant zero every receive
waits out the WHOLE serial timeout before reading bytes that were there all
along. Measured on the bench: 2.0 s per register read, 200/200 correct, 401 s
for the run. The link was right and unusably slow; at the 5 Hz control loop a
status read (four transactions) would have taken eight seconds.

:class:`BufferedFtdiPort` is the fix: a port whose ``in_waiting`` is real. It
drains whatever the FT232R has already received into a local buffer - a USB
bulk read that the chip answers within its latency timer, set here to
:data:`LATENCY_TIMER_MS` - and ``read(n)`` serves from that buffer first,
polling for the rest only until the timeout. A 7-byte reply at 19200 8E1 then
costs its wire time plus a couple of milliseconds, i.e. tens of milliseconds
per transaction instead of the full timeout.

It is also why the latency timer is set explicitly. pyftdi's own serial port
enables "dynamic latency" (12 ms rising to 200 ms while the line is idle),
which is right for streaming throughput and wrong for a request/response
protocol whose line is idle between every exchange.

What this module deliberately does not do
------------------------------------------

* **No Modbus.** Framing, CRC and inter-frame silence stay in pymodbus; this is
  bytes in, bytes out.
* **No retries, no reconnection of its own.** A USB error propagates as the ``OSError``
  it already is (``FtdiError`` derives from ``IOError``), and pymodbus closes
  the port on it, exactly as it does for a pyserial failure.
* **No clock of its own.** Timeouts are measured on the injected
  :class:`~src.clock.Clock` (contract rule 4); only the poll *sleep* is real.

libusb on macOS
---------------

pyusb finds libusb through ``ctypes.util.find_library``, which does not search
Homebrew's prefix. pyusb >= 1.2 special-cases ``/opt/homebrew/lib`` on Apple
Silicon, but not an Intel Homebrew (``/usr/local/lib``) nor a MacPorts install.
:func:`load_libusb_backend` loads it once from an explicit list, so nobody has
to export ``DYLD_FALLBACK_LIBRARY_PATH`` before starting the service.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import ctypes.util
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from time import sleep as _real_sleep
from typing import Final, Protocol, final, runtime_checkable

from pyftdi.ftdi import Ftdi
from pyftdi.usbtools import UsbTools
from usb.backend import libusb1

from src.clock import Clock
from src.motor.drive_process_lock import DriveLease
from src.units import Monotonic, Seconds

logger = logging.getLogger(__name__)


# =========================================================================
# The cable
# =========================================================================

FTDI_URL_SCHEME: Final[str] = "ftdi://"
"""pyftdi's URL scheme. A ``MOTOR_PORT`` starting with it goes through this module."""

SCHNEIDER_VENDOR_ID: Final[int] = 0x16DE
SCHNEIDER_PRODUCT_ID: Final[int] = 0x0003
SCHNEIDER_VENDOR_NAME: Final[str] = "schneider"
SCHNEIDER_PRODUCT_NAME: Final[str] = "rs485"

SCHNEIDER_CABLE_URL: Final[str] = (
    f"{FTDI_URL_SCHEME}{SCHNEIDER_VENDOR_NAME}:{SCHNEIDER_PRODUCT_NAME}/1"
)
"""``ftdi://schneider:rs485/1``: the first Schneider cable, interface 1.

pyftdi only resolves the names after :func:`register_schneider_cable`, which
:func:`open_ftdi_port` calls. ``/1`` is the FT232R's only UART interface.
"""

LATENCY_TIMER_MS: Final[int] = 2
"""FT232R latency timer, in milliseconds.

The chip holds received bytes until its 64-byte packet fills or this timer
expires, so it is the floor on how late a short Modbus reply can reach the
host. The factory default is 16 ms, pyftdi's serial port raises it to as much
as 200 ms while idle, and a request/response protocol is idle between every
exchange. 2 ms rather than the minimum 1: at 19200 baud a character takes
0.57 ms, so either is far below the wire time of a reply, and 2 ms halves the
empty USB polls for nothing measurable.
"""

READ_CHUNK: Final[int] = 16
"""Most bytes asked of pyftdi per drain - small on purpose, because it bounds a drain.

pyftdi's ``read_data`` is not one USB read. It loops (``Ftdi.read_data_bytes``)
for as long as each USB read carries payload, and returns only when a read
comes back empty or ``size`` bytes are collected. On a quiet line that is one
reply; on a pair where something keeps talking it is ``size`` characters of
wire time, all of it under :attr:`BufferedFtdiPort._lock`. At 19200 8E1 a
512-byte ask could hold the lock for ~293 ms; 16 bytes holds it for ~9 ms.

Nothing is lost by asking for less: pyftdi keeps the rest of the USB packet in
its own cache and hands it over on the next drain, without touching USB. A
longer frame simply takes more drains, which ``in_waiting`` and ``read``
already loop over.
"""

USB_READ_TIMEOUT_MS: Final[int] = 100
"""libusb timeout on one bulk read from the chip, in milliseconds.

pyftdi's default is 5000 ms, which would let a half-hung FT232R (a USB hub
hiccup, EMI from the drive next to it) block one drain for five seconds - far
past the emergency-stop budget. A healthy chip answers every bulk read within
its latency timer (:data:`LATENCY_TIMER_MS`) whether or not it has data, so
100 ms is 50 times the normal answer and still short against the budget. A
timeout raises ``FtdiError`` (an ``OSError``), which ends the transaction.
"""

USB_WRITE_TIMEOUT_MS: Final[int] = 100
"""libusb timeout on one bulk write to the chip, in milliseconds.

The same reasoning: a Modbus request is a few bytes into a 256-byte FIFO the
chip accepts at once, so 100 ms only ever expires on a chip that is not
answering, and pyftdi's 5000 ms default would freeze the emergency write.
"""

USB_STALL_BOUND: Final[Seconds] = Seconds(max(USB_READ_TIMEOUT_MS, USB_WRITE_TIMEOUT_MS) / 1000)
"""The longest one stalled USB transfer blocks before it raises.

Counted once per transaction, not once per wait: a stalled transfer raises an
``OSError``, and pymodbus ends the transaction on it (``_transact`` catches it
and closes the port), so no transaction pays it twice.
"""

RX_BUFFER_LIMIT: Final[int] = 4096
"""Most bytes held between reads before the OLDEST are dropped.

pymodbus discards whatever is waiting before every request, so a healthy link
never gets near this. It exists so something chattering on the pair cannot grow
this process's memory without bound, and dropping the oldest keeps the newest
- which is the only data a reply could still be in.
"""

POLL_INTERVAL: Final[Seconds] = Seconds(0.001)
"""Pause between drains while ``read`` waits for more bytes.

Each drain already costs up to one latency period when the chip has nothing,
so this only keeps a fast or absent USB answer from spinning a core.
"""

SETTLE_DWELL: Final[Seconds] = Seconds(0.25)
"""Wait after configuring the chip before trusting its bulk endpoints.

Measured on the bench (macOS 26, Schneider TCSMCNAM3M002P): for roughly
100-200 ms after ``create_from_url`` + :func:`configure`, USB transfers to the
FT232R time out (``UsbError: [Errno 60] Operation timed out``) against the
:data:`USB_READ_TIMEOUT_MS` / :data:`USB_WRITE_TIMEOUT_MS` this module sets.
With a 200 ms pause every read then took ~34 ms; with none, every read failed -
and since pymodbus closes the port on each failure and reopens it on the next
transaction, the link never escaped the window. pyftdi's own port hides this
only because its 5 s USB timeouts wait it out.
"""

SETTLE_BUDGET: Final[Seconds] = Seconds(1.0)
"""Longest :func:`settle` waits, dwell included, before declaring the chip dead."""

SETTLE_CONFIRMATIONS: Final[int] = 2
"""Consecutive clean bulk reads that count as "the chip answers"."""

SETTLE_POLL: Final[Seconds] = Seconds(0.01)
"""Pause between settle probes after a failed one."""


def wire_character_time(frame: UartFrame) -> Seconds:
    """One character on the wire: start, data, parity (if any) and stop bits."""
    parity_bits = 0 if frame.parity is Parity.NONE else 1
    return Seconds((1 + frame.bytesize + parity_bits + frame.stopbits) / frame.baudrate)


def drain_worst_case(frame: UartFrame) -> Seconds:
    """The longest one drain of a working chip can take on this line.

    pyftdi keeps reading while the chip keeps delivering, so a drain lasts up
    to :data:`READ_CHUNK` characters of wire time when the line chatters, plus
    one latency period for the packet carrying the last of them and one more
    for the empty read that ends the loop. A chip that stops answering is not
    this bound's business: its transfer raises after the USB timeout (see
    :data:`USB_STALL_BOUND`).
    """
    latency = Seconds(LATENCY_TIMER_MS / 1000)
    return Seconds(READ_CHUNK * wire_character_time(frame) + 2 * latency)


def wait_overshoot(frame: UartFrame) -> Seconds:
    """How far one timed wait on this port can run past its deadline.

    The deadline is checked between drains, so a wait can overrun it by at most
    one drain plus one poll sleep. Exposed so the drive's emergency-stop budget
    can count it rather than hope it is zero.
    """
    return Seconds(drain_worst_case(frame) + POLL_INTERVAL)


def is_ftdi_url(port: str) -> bool:
    """Whether ``port`` names a pyftdi device rather than an OS serial device."""
    return port.lower().startswith(FTDI_URL_SCHEME)


# =========================================================================
# Line settings
# =========================================================================


@unique
class Parity(Enum):
    """Serial parity, in the single-character spelling pyserial and pyftdi use.

    An enum rather than a bare ``str`` because "E" is a value from a fixed set
    with meaning attached, and this drive is commissioned for 8**E**1: a silent
    "N" gives a port that opens and then fails every CRC.
    """

    NONE = "N"
    EVEN = "E"
    ODD = "O"


@dataclass(frozen=True, slots=True)
class UartFrame:
    """The character format and speed, everything the chip needs to be told."""

    baudrate: int
    bytesize: int
    parity: Parity
    stopbits: int


# =========================================================================
# The seam to pyftdi
# =========================================================================


@runtime_checkable
class FtdiDevice(Protocol):
    """The three operations :class:`BufferedFtdiPort` performs on an open chip.

    A protocol so the buffering can be tested against a fake chip that
    delivers a reply in pieces, late, or never - none of which a test can make
    real USB hardware do on demand.
    """

    def read_data(self, size: int) -> bytes:
        """At most ``size`` bytes already received. Returns ``b""`` when none.

        On the real chip this is one USB bulk read, answered within the latency
        timer whether or not data is waiting - which is what makes it usable as
        a non-blocking poll.
        """
        ...

    def write_data(self, data: bytes) -> int:
        """Queue ``data`` for transmission; returns the count accepted."""
        ...

    def close(self) -> None:
        """Release the USB interface."""
        ...


@runtime_checkable
class ConfigurableFtdi(FtdiDevice, Protocol):
    """An :class:`FtdiDevice` that can also be set up. What ``Ftdi`` really is."""

    def set_baudrate(self, baudrate: int, constrain: bool = True) -> int:
        """Pick the closest achievable rate; refuse one outside 3% when constrained."""
        ...

    def set_line_property(
        self, bits: int, stopbit: int | float, parity: str, break_: bool = False
    ) -> None:
        """Character size, stop bits and parity."""
        ...

    def set_flowctrl(self, flowctrl: str) -> None:
        """``""`` for none. RS-485 direction is switched by the cable itself."""
        ...

    def set_latency_timer(self, latency: int) -> None:
        """Milliseconds the chip may hold a short packet before sending it up."""
        ...

    def purge_buffers(self) -> None:
        """Drop anything already queued in either direction."""
        ...

    @property
    def timeouts(self) -> tuple[int, int]:
        """libusb (read, write) timeouts in milliseconds, for every transfer after this."""
        ...

    @timeouts.setter
    def timeouts(self, timeouts: tuple[int, int]) -> None: ...


type Sleeper = Callable[[float], None]
"""A blocking sleep. ``time.sleep`` in production; a clock-advancing fake in tests."""

type DeviceFactory = Callable[[str], ConfigurableFtdi]
"""Opens the chip a URL names. :func:`open_schneider_device` in production."""


# =========================================================================
# The port pymodbus reads
# =========================================================================


@final
class BufferedFtdiPort:
    """The pyserial surface pymodbus 3.7.4 uses, with an ``in_waiting`` that works.

    Exactly the members ``ModbusSerialClient`` touches on its ``socket``:
    ``in_waiting``, ``read``, ``write``, ``close``, ``is_open`` and
    ``inter_byte_timeout`` (assigned by pymodbus's own ``connect`` and never
    read back; kept so an assignment does not fail). Nothing else of pyserial is
    offered, so a caller relying on more fails loudly rather than silently.

    **Thread safety.** The drive's emergency path may reach this port from a
    second thread while the executor thread is mid-transaction (see
    ``ATV320Drive.emergency_disable_blocking``), so the buffer and the chip are
    touched only under :attr:`_lock`. The lock is held for one drain at a time,
    never across a poll sleep, so a waiting ``read`` holds off a write for at
    most one drain - :func:`drain_worst_case`, which :data:`READ_CHUNK` keeps
    to milliseconds - or, on a chip that stopped answering, one USB timeout
    after which the drain raises.
    """

    __slots__ = (
        "_buffer_limit",
        "_clock",
        "_device",
        "_lease",
        "_lock",
        "_poll_interval",
        "_rx",
        "_sleep",
        "_timeout",
        "inter_byte_timeout",
        "is_open",
    )

    def __init__(
        self,
        device: FtdiDevice,
        *,
        timeout: Seconds,
        clock: Clock,
        sleep: Sleeper = _real_sleep,
        poll_interval: Seconds = POLL_INTERVAL,
        buffer_limit: int = RX_BUFFER_LIMIT,
        lease: DriveLease | None = None,
    ) -> None:
        if timeout < 0.0:
            raise ValueError(f"read timeout {timeout} s must not be negative")
        if buffer_limit < 1:
            raise ValueError(f"buffer_limit {buffer_limit} must be at least one byte")
        self._device: FtdiDevice = device
        self._lease: DriveLease | None = lease
        self._timeout: Seconds = timeout
        self._clock: Clock = clock
        self._sleep: Sleeper = sleep
        self._poll_interval: Seconds = poll_interval
        self._buffer_limit: int = buffer_limit
        # Mutable shared state, on purpose: bytes the chip has handed over and
        # no read has consumed yet. Guarded by `_lock`.
        self._rx: bytearray = bytearray()
        self._lock: threading.Lock = threading.Lock()
        self.is_open: bool = True
        self.inter_byte_timeout: float | None = None

    @property
    def timeout(self) -> Seconds:
        """How long ``read`` waits for the bytes it was asked for."""
        return self._timeout

    @property
    def in_waiting(self) -> int:
        """Bytes received and not yet read - asking the chip first. Never waits for more.

        This is the member pyftdi's own port gets wrong, and the one pymodbus
        polls to decide a reply has finished arriving.
        """
        with self._lock:
            self._require_open()
            self._drain()
            return len(self._rx)

    def read(self, size: int = 1) -> bytes:
        """Up to ``size`` bytes: from the buffer first, then polling until the timeout.

        pyserial semantics: returns as soon as ``size`` bytes are available,
        otherwise whatever arrived by the timeout, possibly nothing. A
        ``size`` of zero or less returns ``b""`` without touching the chip.
        """
        if size <= 0:
            return b""
        deadline = Monotonic(self._clock.monotonic() + self._timeout)
        while True:
            with self._lock:
                self._require_open()
                self._drain()
                if len(self._rx) >= size or self._clock.monotonic() >= deadline:
                    taken = bytes(self._rx[:size])
                    del self._rx[:size]
                    return taken
            self._sleep(self._poll_interval)

    def write(self, data: bytes) -> int:
        """Hand ``data`` to the chip. Returns the count it accepted."""
        with self._lock:
            self._require_open()
            return self._device.write_data(data)

    def close(self) -> None:
        """Release the chip. Idempotent: pymodbus closes on every failed exchange."""
        with self._lock:
            if not self.is_open:
                return
            self._device.close()
            self.is_open = False
            self._rx.clear()
            if self._lease is not None:
                self._lease.close()

    def _require_open(self) -> None:
        if self._lease is not None:
            self._lease.require_active()
        if not self.is_open:
            # OSError, as pyserial's PortNotOpenError is: pymodbus treats it as
            # a transport failure and closes, rather than crashing the thread.
            raise OSError("the FTDI port is closed")

    def _drain(self) -> None:
        """Move what the chip already holds into the buffer. Lock held by the caller."""
        chunk = self._device.read_data(READ_CHUNK)
        if not chunk:
            return
        self._rx.extend(chunk)
        overflow = len(self._rx) - self._buffer_limit
        if overflow > 0:
            del self._rx[:overflow]
            logger.warning(
                "FTDI receive buffer over %d bytes: dropped the oldest %d. Something "
                "on the RS-485 pair is talking when nothing asked it to.",
                self._buffer_limit,
                overflow,
            )


# =========================================================================
# Opening the cable
# =========================================================================


def open_ftdi_port(
    url: str,
    frame: UartFrame,
    *,
    timeout: Seconds,
    clock: Clock,
    create: DeviceFactory | None = None,
    sleep: Sleeper = _real_sleep,
) -> BufferedFtdiPort:
    """Open the chip ``url`` names, configure it for ``frame``, and wrap it.

    The chip is released again if configuring it fails, so a refused baud rate
    does not leave an interface claimed until the process exits.

    ``create`` defaults to :func:`open_schneider_device`; a test passes a fake.
    """
    lease = DriveLease.claim()
    transferred = False
    try:
        device = open_schneider_device(url, lease) if create is None else create(url)
        configured = False
        try:
            configure(device, frame)
            settle(device, clock=clock, sleep=sleep)
            port = BufferedFtdiPort(device, timeout=timeout, clock=clock, sleep=sleep, lease=lease)
            configured = True
        finally:
            if not configured:
                device.close()
        transferred = True
        return port
    finally:
        if not transferred:
            lease.close()


def settle(
    device: ConfigurableFtdi,
    *,
    clock: Clock,
    sleep: Sleeper = _real_sleep,
    dwell: Seconds = SETTLE_DWELL,
    budget: Seconds = SETTLE_BUDGET,
) -> None:
    """Wait until a freshly configured chip answers bulk reads, then empty it.

    Dwells :data:`SETTLE_DWELL` (see there for the bench measurement), then
    needs :data:`SETTLE_CONFIRMATIONS` consecutive clean ``read_data`` calls.
    A read raising ``OSError`` (``usb.core.USBError`` is one) restarts the
    count. Past ``budget`` the last error is re-raised as ``OSError``, which
    :class:`~src.motor.atv320.FtdiModbusClient` turns into ``connect() ->
    False``. Anything read here is noise from before the first request and is
    dropped by the final purge. Nothing is written to the RS-485 pair.
    """
    deadline = Monotonic(clock.monotonic() + budget)
    sleep(dwell)
    clean = 0
    while True:
        try:
            device.read_data(READ_CHUNK)
        except OSError as exc:
            clean = 0
            if clock.monotonic() >= deadline:
                raise OSError(
                    f"FTDI chip still not answering {budget} s after it was configured: {exc}"
                ) from exc
            sleep(SETTLE_POLL)
            continue
        clean += 1
        if clean >= SETTLE_CONFIRMATIONS:
            device.purge_buffers()
            return


def configure(device: ConfigurableFtdi, frame: UartFrame) -> None:
    """Short USB timeouts, line settings, no flow control, a short latency timer, empty buffers.

    The USB timeouts go first so that even the configuration transfers cannot
    hang for pyftdi's default five seconds. ``constrain=True``: pyftdi then
    refuses a rate the chip can only approach by more than 3%, which would open
    cleanly and fail every CRC.
    """
    device.timeouts = (USB_READ_TIMEOUT_MS, USB_WRITE_TIMEOUT_MS)
    device.set_baudrate(frame.baudrate, True)
    device.set_line_property(frame.bytesize, frame.stopbits, frame.parity.value)
    device.set_flowctrl("")
    device.set_latency_timer(LATENCY_TIMER_MS)
    device.purge_buffers()


def open_schneider_device(url: str, lease: DriveLease) -> Ftdi:
    """The real opener: vendor ids registered, libusb loaded, chip opened.

    The one function in this module that reaches USB hardware.

    pyftdi caches its USB enumeration per vendor/product id and never renews
    it on its own (``UsbTools._find_devices``). A cable unplugged and
    replugged, or an FT232R that re-enumerated after a brown-out next to the
    drive, would then resolve to the stale device and fail every reopen with
    USB error 19; a first open with the cable absent would cache "no device"
    for good. Opening happens only on (re)connect, never per transaction, so
    the cache is flushed every time: one enumeration, a few milliseconds.
    """
    lease.require_active()
    register_schneider_cable()
    load_libusb_backend()
    UsbTools.flush_cache()
    return Ftdi.create_from_url(url)


def register_schneider_cable() -> None:
    """Teach pyftdi the cable's non-FTDI vendor id and the names in the URL.

    Idempotent. pyftdi keeps these tables on the class and raises
    ``ValueError`` for an id it already knows, which on a second call - a
    reconnect after pymodbus closed the port - means "done already", not an
    error.
    """
    try:
        Ftdi.add_custom_vendor(SCHNEIDER_VENDOR_ID, SCHNEIDER_VENDOR_NAME)
    except ValueError:
        logger.debug("pyftdi already knows vendor 0x%04x", SCHNEIDER_VENDOR_ID)
    try:
        Ftdi.add_custom_product(SCHNEIDER_VENDOR_ID, SCHNEIDER_PRODUCT_ID, SCHNEIDER_PRODUCT_NAME)
    except ValueError:
        logger.debug(
            "pyftdi already knows product 0x%04x:0x%04x",
            SCHNEIDER_VENDOR_ID,
            SCHNEIDER_PRODUCT_ID,
        )


# =========================================================================
# libusb
# =========================================================================

LIBUSB_DIRECTORIES: Final[tuple[Path, ...]] = (
    Path("/opt/homebrew/lib"),  # Homebrew, Apple Silicon
    Path("/usr/local/lib"),  # Homebrew, Intel; and source installs
    Path("/opt/local/lib"),  # MacPorts
)
"""Where libusb lives on a Mac when the dynamic loader's default search misses it."""


def find_libusb(
    candidate: str,
    *,
    system_find: Callable[[str], str | None] = ctypes.util.find_library,
    directories: Sequence[Path] = LIBUSB_DIRECTORIES,
) -> str | None:
    """pyusb's ``find_library`` hook: the loader's own answer, else a known prefix.

    pyusb calls this once per candidate name (``usb-1.0``, ``libusb-1.0``,
    ``usb``) and loads the first non-empty answer.
    """
    found = system_find(candidate)
    if found:
        return found
    for directory in directories:
        for name in (f"{candidate}.dylib", f"lib{candidate}.dylib"):
            path = directory / name
            if path.is_file():
                return str(path)
    return None


def load_libusb_backend() -> None:
    """Load libusb-1.0 for pyusb, from :func:`find_libusb`. Raises ``OSError`` if absent.

    pyusb caches the loaded library in a module global, so after this every
    parameterless ``get_backend()`` - pyftdi's included - reuses it. Raising
    here names the missing library; letting pyftdi find out would surface as
    "No backend available", which says nothing about what to install.
    """
    if libusb1.get_backend(find_library=find_libusb) is None:
        raise OSError(
            "libusb-1.0 was not found, so the Schneider USB-RS485 cable cannot be "
            "opened. macOS: `brew install libusb`. Raspberry Pi: "
            "`sudo apt install libusb-1.0-0`."
        )
