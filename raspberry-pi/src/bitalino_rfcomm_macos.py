"""The BITalino over macOS IOBluetooth RFCOMM. The ONLY module that imports pyobjc.

Contract rule 5: ``Foundation`` and ``IOBluetooth`` (pyobjc) are touched here
and nowhere else, and only lazily, inside :func:`_load_bindings`: pyobjc exists
on macOS alone, and this module must still import - and be tested - on the Pi.

Why this module exists
----------------------

On macOS the serial node ``/dev/cu.BITalino-4E-9F`` is intermittent: it
streamed once at ~700 frames/s, then stayed silent. Opening RFCOMM channel 1
through IOBluetooth directly is what was verified to work (firmware
BITalino_v5.2; one channel at 1000 Hz is ~3000 B/s).

It plugs into :class:`src.bitalino_client.BITalinoClient` as a
:data:`~src.bitalino_client.DeviceFactory`, so the whole reader - the
:class:`~src.bitalino_client.FrameDecoder` with its CRC resync, the reconnect
backoff, the segments and the link counters - is reused as is. This module is
bytes in, bytes out, plus the four command bytes of the vendor protocol.

Threading
---------

IOBluetooth delivers its delegate callbacks only while a run loop is pumped on
the thread that opened the channel. So each :class:`RfcommDevice` owns one
daemon thread that opens the channel, then pumps its run loop in short slices
and performs the queued writes between slices (IOBluetooth objects stay on the
thread that made them). Callers - the client's executor and reader threads -
only ever touch a byte buffer and a write queue, both guarded by one
:class:`threading.Condition`.

Silence is a lost link: :meth:`RfcommDevice.read_chunk` raises
:class:`~src.bitalino_client.LinkLostError` when no byte arrives for a whole
link timeout, which is what makes the client's existing reconnect path run.
Nothing here retries on its own.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import TYPE_CHECKING, Final, Protocol, cast, final, runtime_checkable

from src.bitalino_client import (
    MAX_CHANNEL,
    MIN_CHANNEL,
    Device,
    DeviceFactory,
    LinkLostError,
    VendorDevice,
)

if TYPE_CHECKING:
    from Foundation import NSDate, NSObject, NSRunLoop
    from IOBluetooth import IOBluetoothDevice, IOBluetoothRFCOMMChannel

logger = logging.getLogger(__name__)


# =========================================================================
# Addresses
# =========================================================================

RFCOMM_SCHEME: Final[str] = "rfcomm:"
"""An address starting with this goes through this module: ``rfcomm:98-d3-91-fe-4e-9f``."""

_RFCOMM_ADDRESS: Final[re.Pattern[str]] = re.compile(
    r"^rfcomm:([0-9a-f]{2}(?:-[0-9a-f]{2}){5})$", re.IGNORECASE
)


def parse_rfcomm_address(address: str) -> str | None:
    """The IOBluetooth address string (``98-d3-91-fe-4e-9f``), or ``None`` if not ours."""
    match = _RFCOMM_ADDRESS.match(address.strip())
    return None if match is None else match.group(1).lower()


def is_rfcomm_address(address: str) -> bool:
    """Whether ``address`` selects this transport."""
    return parse_rfcomm_address(address) is not None


# =========================================================================
# The vendor wire protocol (bitalino.py 1.2.6: start/stop/version/send)
# =========================================================================

BITALINO_RFCOMM_CHANNEL: Final[int] = 1
"""The BITalino's serial-port service sits on RFCOMM channel 1."""

IO_RETURN_SUCCESS: Final[int] = 0
"""``kIOReturnSuccess``."""

RATE_CODES: Final[dict[int, int]] = {1: 0, 10: 1, 100: 2, 1000: 3}
"""Sampling rate in Hz -> the 2-bit code of the set-rate command."""

VERSION_COMMAND: Final[bytes] = b"\x07"
STOP_COMMAND: Final[bytes] = b"\x00"
VERSION_MARKER: Final[str] = "BITalino"

COMMAND_GAP_S: Final[float] = 0.1
"""Pause after every command byte, as the vendor's ``send`` sleeps 0.1 s: the
firmware drops a command that follows the previous one too closely."""


def rate_command(sample_rate: int) -> bytes:
    """The set-rate command: ``(code << 6) | 0x03``."""
    code = RATE_CODES.get(sample_rate)
    if code is None:
        raise ValueError(f"Sample rate must be 1, 10, 100 or 1000 Hz, got {sample_rate}")
    return bytes([(code << 6) | 0x03])


def start_command(channels: Sequence[int]) -> bytes:
    """The start command for live mode: ``1 | sum(1 << (2 + ch))``."""
    if not channels:
        raise ValueError("At least one analog channel is required")
    command = 1
    for channel in channels:
        if channel < MIN_CHANNEL or channel > MAX_CHANNEL:
            raise ValueError(f"Channel must be 0-5, got {channel}")
        command |= 1 << (2 + channel)
    return bytes([command])


def parse_version(reply: bytes) -> str:
    """The firmware's reply to ``0x07``, from ``BITalino`` on (as the vendor slices it)."""
    text = reply.decode("ascii", errors="replace")
    start = text.find(VERSION_MARKER)
    return (text[start:] if start >= 0 else text).strip()


# =========================================================================
# The native seam
# =========================================================================


class ChannelEvents(Protocol):
    """What the native channel reports, on the run-loop thread."""

    def on_open(self, status: int, /) -> None: ...
    def on_data(self, data: bytes, /) -> None: ...
    def on_closed(self) -> None: ...


class NativeLink(Protocol):
    """An opened RFCOMM channel. Every call is made on the run-loop thread."""

    def pump(self, seconds: float, /) -> None:
        """Run the thread's run loop for ``seconds``; callbacks fire in here."""
        ...

    def write(self, data: bytes, /) -> None:
        """Send ``data``. Raises ``OSError`` on failure."""
        ...

    def close(self) -> None: ...


type NativeOpener = Callable[[str, ChannelEvents], NativeLink]
"""Opens RFCOMM channel 1 of the device at an IOBluetooth address string,
reporting to the events sink. Runs on the run-loop thread; blocking; may raise."""


# =========================================================================
# The device
# =========================================================================

PUMP_SLICE_S: Final[float] = 0.02
"""How long one run-loop pump lasts: the latency of a queued write."""

OPEN_TIMEOUT_FLOOR_S: Final[float] = 10.0
"""Least time an open may take: paging a device and opening RFCOMM takes
seconds, well past the 3 s link timeout that governs silence once streaming."""

JOIN_TIMEOUT_S: Final[float] = 2.0

MAX_BUFFERED_BYTES: Final[int] = 1 << 20
"""Cap on unread bytes (~5 min of one channel at 1000 Hz). Past it the oldest
bytes go: the frame decoder resynchronises and counts the gap."""


@unique
class LinkState(Enum):
    OPENING = "opening"
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass(slots=True)
class _Write:
    """One queued command. Mutable on purpose: the run-loop thread completes it
    in place, and every access holds the device's condition."""

    data: bytes
    done: bool = False
    error: str | None = None


@final
class RfcommDevice:
    """A BITalino on an IOBluetooth RFCOMM channel, implementing :class:`Device`.

    Construction opens the channel (blocking, up to ``open_timeout``) and
    raises ``OSError`` if it cannot. After that every method is safe to call
    from any thread except the run-loop thread itself.
    """

    def __init__(
        self,
        address: str,
        link_timeout: float,
        *,
        opener: NativeOpener,
        open_timeout: float | None = None,
        pump_slice: float = PUMP_SLICE_S,
        command_gap: float = COMMAND_GAP_S,
    ) -> None:
        mac = parse_rfcomm_address(address)
        if mac is None:
            raise ValueError(f"not an RFCOMM address (rfcomm:XX-XX-XX-XX-XX-XX): {address!r}")
        self.address: str = mac
        self._link_timeout: float = link_timeout
        self._opener: NativeOpener = opener
        self._pump_slice: float = pump_slice
        self._command_gap: float = command_gap

        # Shared between the run-loop thread and callers; every access holds _cond.
        self._cond: threading.Condition = threading.Condition()
        self._state: LinkState = LinkState.OPENING
        self._reason: str = "opening"
        self._buffer: bytearray = bytearray()
        self._writes: deque[_Write] = deque()
        self.dropped_bytes: int = 0
        """Bytes discarded because nobody read them (see :data:`MAX_BUFFERED_BYTES`)."""

        self._stop: threading.Event = threading.Event()
        self._thread: threading.Thread = threading.Thread(
            target=self._run, name=f"rfcomm-{mac}", daemon=True
        )
        self._thread.start()

        deadline = max(link_timeout, OPEN_TIMEOUT_FLOOR_S) if open_timeout is None else open_timeout
        with self._cond:
            self._cond.wait_for(lambda: self._state is not LinkState.OPENING, deadline)
            state, reason = self._state, self._reason
        if state is LinkState.OPEN:
            logger.info("RFCOMM channel %d open to %s", BITALINO_RFCOMM_CHANNEL, mac)
            return
        self.close()
        if state is LinkState.OPENING:
            raise OSError(f"RFCOMM open to {mac} timed out after {deadline:.1f} s")
        raise OSError(f"RFCOMM open to {mac} failed: {reason}")

    # -- state --------------------------------------------------------------

    @property
    def state(self) -> LinkState:
        with self._cond:
            return self._state

    @property
    def pending_writes(self) -> int:
        """Commands queued for the run-loop thread and not yet written."""
        with self._cond:
            return len(self._writes)

    @property
    def is_running(self) -> bool:
        """Whether the run-loop thread is still alive."""
        return self._thread.is_alive()

    def _settle(self, state: LinkState, reason: str) -> None:
        """Move out of OPENING/OPEN once; a terminal state keeps its first reason."""
        with self._cond:
            if self._state in (LinkState.OPENING, LinkState.OPEN):
                self._state = state
                self._reason = reason
            self._cond.notify_all()

    # -- ChannelEvents (run-loop thread) -------------------------------------

    def on_open(self, status: int, /) -> None:
        with self._cond:
            if self._state is LinkState.OPENING:
                if status == IO_RETURN_SUCCESS:
                    self._state = LinkState.OPEN
                else:
                    self._state = LinkState.FAILED
                    self._reason = f"channel open completed with IOReturn {status:#x}"
            self._cond.notify_all()

    def on_data(self, data: bytes, /) -> None:
        with self._cond:
            self._buffer.extend(data)
            excess = len(self._buffer) - MAX_BUFFERED_BYTES
            if excess > 0:
                del self._buffer[:excess]
                self.dropped_bytes += excess
            self._cond.notify_all()

    def on_closed(self) -> None:
        self._settle(LinkState.CLOSED, "the device closed the RFCOMM channel")

    # -- the run-loop thread --------------------------------------------------

    def _run(self) -> None:
        try:
            link = self._opener(self.address, self)
        except Exception as error:  # OSError, ImportError off macOS, objc.error
            self._settle(LinkState.FAILED, str(error) or type(error).__name__)
            return
        try:
            while not self._stop.is_set():
                link.pump(self._pump_slice)
                self._flush_one(link)
        except Exception as error:  # a broken run loop is a lost link, never a hang
            logger.warning("RFCOMM run loop for %s failed: %s", self.address, error)
            self._settle(LinkState.CLOSED, f"run loop failed: {error}")
        finally:
            try:
                link.close()
            except Exception as error:  # closing a dead channel may itself fail
                logger.debug("Closing the RFCOMM channel failed: %s", error)
            self._settle(LinkState.CLOSED, "closed")
            self._fail_pending("the RFCOMM channel is closed")

    def _flush_one(self, link: NativeLink) -> None:
        with self._cond:
            if not self._writes:
                return
            write = self._writes.popleft()
        error: str | None = None
        try:
            link.write(write.data)
        except OSError as failure:
            error = str(failure) or "write failed"
        link.pump(self._command_gap)
        with self._cond:
            write.done = True
            write.error = error
            self._cond.notify_all()

    def _fail_pending(self, reason: str) -> None:
        with self._cond:
            while self._writes:
                write = self._writes.popleft()
                write.done = True
                write.error = reason
            self._cond.notify_all()

    # -- callers --------------------------------------------------------------

    def _send(self, data: bytes) -> None:
        write = _Write(data)
        with self._cond:
            if self._state is not LinkState.OPEN:
                raise LinkLostError(f"RFCOMM link to {self.address} is {self._reason}")
            self._writes.append(write)
            completed = self._cond.wait_for(
                lambda: write.done, self._link_timeout + self._command_gap
            )
            if not completed:
                raise LinkLostError(f"RFCOMM write to {self.address} timed out")
            if write.error is not None:
                raise LinkLostError(f"RFCOMM write to {self.address} failed: {write.error}")

    def version(self) -> str:
        with self._cond:
            self._buffer.clear()
        self._send(VERSION_COMMAND)
        with self._cond:
            self._cond.wait_for(
                lambda: b"\n" in self._buffer or self._state is not LinkState.OPEN,
                self._link_timeout,
            )
            end = self._buffer.find(b"\n")
            if end < 0:
                raise LinkLostError(f"no version reply from {self.address}")
            reply = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]
        return parse_version(reply)

    def start(self, sample_rate: int, channels: Sequence[int], /) -> None:
        rate = rate_command(sample_rate)
        start = start_command(channels)
        with self._cond:
            self._buffer.clear()
        self._send(rate)
        self._send(start)

    def read_chunk(self, max_bytes: int, /) -> bytes:
        with self._cond:
            self._cond.wait_for(
                lambda: bool(self._buffer) or self._state is not LinkState.OPEN,
                self._link_timeout,
            )
            if self._buffer:
                chunk = bytes(self._buffer[:max_bytes])
                del self._buffer[:max_bytes]
                return chunk
            if self._state is LinkState.OPEN:
                raise LinkLostError(f"no data from {self.address} for {self._link_timeout:.1f} s")
            raise LinkLostError(f"RFCOMM link to {self.address} is {self._reason}")

    def stop(self) -> None:
        self._send(STOP_COMMAND)

    def close(self) -> None:
        """Stop the run loop and close the channel. Idempotent."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is threading.current_thread():
            return
        self._thread.join(JOIN_TIMEOUT_S)
        if self._thread.is_alive():
            logger.warning("RFCOMM run loop for %s did not stop in time", self.address)


# =========================================================================
# pyobjc (macOS only)
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Bindings:
    device_class: type[IOBluetoothDevice]
    ns_object: type[NSObject]
    run_loop: type[NSRunLoop]
    date: type[NSDate]


def _load_bindings() -> _Bindings:
    """Import pyobjc. Lazy, so this module imports on Linux; raises ImportError there."""
    # Imported here, not at the top: pyobjc exists on macOS only (see the module doc).
    from Foundation import NSDate, NSObject, NSRunLoop  # noqa: PLC0415
    from IOBluetooth import IOBluetoothDevice  # noqa: PLC0415

    return _Bindings(
        device_class=IOBluetoothDevice, ns_object=NSObject, run_loop=NSRunLoop, date=NSDate
    )


@runtime_checkable
class _VarList(Protocol):
    """pyobjc's ``objc.varlist``: a C array of unknown length."""

    def as_buffer(self, count: int, /) -> memoryview: ...


def _payload(data: object, length: int) -> bytes | None:
    """The bytes of one ``rfcommChannelData:data:length:`` callback, in either form pyobjc uses."""
    if isinstance(data, bytes | bytearray):
        return bytes(data[:length])
    if isinstance(data, _VarList):
        return bytes(data.as_buffer(length))
    return None


_delegate_serial: Final[itertools.count[int]] = itertools.count(1)


def build_delegate(ns_object: type[NSObject], events: ChannelEvents) -> NSObject:
    """A fresh NSObject subclass whose methods forward to ``events``.

    One Objective-C class per channel, named uniquely (the runtime refuses to
    register a name twice), so the sink is a closure rather than an instance
    attribute. The method names are the selectors of IOBluetooth's informal
    ``IOBluetoothRFCOMMChannelDelegate`` protocol; pyobjc takes their
    signatures from the framework metadata.
    """
    warned = threading.Event()

    def open_complete(_self: object, _channel: object, status: int) -> None:
        events.on_open(status)

    def data_arrived(_self: object, _channel: object, data: object, length: int) -> None:
        payload = _payload(data, length)
        if payload is not None:
            events.on_data(payload)
        elif not warned.is_set():
            warned.set()
            logger.warning("RFCOMM data arrived as an unreadable %s", type(data).__name__)

    def closed(_self: object, _channel: object) -> None:
        events.on_closed()

    name = f"AnheartRfcommDelegate{next(_delegate_serial)}"
    namespace: dict[str, object] = {
        "rfcommChannelOpenComplete_status_": open_complete,
        "rfcommChannelData_data_length_": data_arrived,
        "rfcommChannelClosed_": closed,
    }
    # type() returns a bare `type`; the class really is an NSObject subclass.
    delegate_class = cast("type[NSObject]", type(name, (ns_object,), namespace))
    return delegate_class.alloc().init()


@final
class _IOBluetoothLink:
    """:class:`NativeLink` over an opened ``IOBluetoothRFCOMMChannel``."""

    def __init__(
        self,
        bindings: _Bindings,
        device: IOBluetoothDevice,
        channel: IOBluetoothRFCOMMChannel,
        delegate: NSObject,
    ) -> None:
        self._bindings: _Bindings = bindings
        self._device: IOBluetoothDevice = device
        self._channel: IOBluetoothRFCOMMChannel = channel
        self._delegate: NSObject = delegate
        """Held so the delegate lives as long as the channel that calls it."""

    def pump(self, seconds: float, /) -> None:
        limit = self._bindings.date.dateWithTimeIntervalSinceNow_(seconds)
        self._bindings.run_loop.currentRunLoop().runUntilDate_(limit)

    def write(self, data: bytes, /) -> None:
        status = self._channel.writeSync_length_(data, len(data))
        if status != IO_RETURN_SUCCESS:
            raise OSError(f"writeSync failed with IOReturn {status:#x}")

    def close(self) -> None:
        self._channel.closeChannel()
        self._device.closeConnection()


def open_iobluetooth_channel(mac: str, events: ChannelEvents) -> NativeLink:
    """The production :data:`NativeOpener`: page the device, open RFCOMM channel 1."""
    bindings = _load_bindings()
    device = bindings.device_class.deviceWithAddressString_(mac)
    if device is None:
        raise OSError(f"IOBluetooth does not know {mac}")
    status = device.openConnection()
    if status != IO_RETURN_SUCCESS:
        raise OSError(f"baseband connection to {mac} failed with IOReturn {status:#x}")
    delegate = build_delegate(bindings.ns_object, events)
    status, channel = device.openRFCOMMChannelAsync_withChannelID_delegate_(
        None, BITALINO_RFCOMM_CHANNEL, delegate
    )
    if status != IO_RETURN_SUCCESS or channel is None:
        device.closeConnection()
        raise OSError(f"RFCOMM channel open to {mac} failed with IOReturn {status:#x}")
    return _IOBluetoothLink(bindings, device, channel, delegate)


# =========================================================================
# Factories
# =========================================================================


def rfcomm_factory(address: str, link_timeout: float) -> Device:
    """The :data:`~src.bitalino_client.DeviceFactory` for ``rfcomm:`` addresses."""
    return RfcommDevice(address, link_timeout, opener=open_iobluetooth_channel)


def device_factory_for(address: str) -> DeviceFactory:
    """The transport an address selects: ``rfcomm:`` -> IOBluetooth, anything else -> vendor."""
    return rfcomm_factory if is_rfcomm_address(address) else VendorDevice
