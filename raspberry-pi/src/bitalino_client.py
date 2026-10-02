"""BITalino acquisition: the one module allowed to import the ``bitalino`` vendor library.

The heart rate that sets the motor speed is computed from what this module hands
out, so it is in the safety chain and under the 100% branch gate. It exists to
turn a noisy Bluetooth byte stream into time-correct, correctly-labelled samples,
and to say so loudly when it cannot.

Why the vendor ``read()`` is not used
-------------------------------------
``bitalino.BITalino.read()`` raises the same bare ``Exception`` for "one frame
failed its 4-bit CRC" as for "the device is gone", and it throws away the whole
chunk it was assembling. A single corrupted byte - common next to a variable-
frequency drive - therefore used to end the session as a disconnect while the
device was still streaming happily. It also reads one byte per Python call.

So the vendor library is kept for what it does well (opening the port and
sending the start/stop commands) and the byte stream is decoded here by
:class:`FrameDecoder`, which:

* re-synchronises on a corrupt frame by sliding one byte, instead of giving up;
* detects frames the device dropped from the 4-bit sequence number, and fills
  the gap by holding the last value. Filling matters: a missing sample shortens
  every RR interval that spans it, which reads as a *higher* heart rate;
* counts all of it in :class:`LinkStats` so a bench test can see the link.

Channel order
-------------
The start command is a bitmask (``A6 A5 A4 A3 A2 A1 0 1``), so the device always
streams its analog channels in **ascending** index order, whatever order they
were requested in. The requested channels are therefore sorted and
de-duplicated before anything else sees them. Before this, a session asking for
``["RESP", "ECG"]`` got the two traces swapped, and a duplicate index made the
vendor expect a frame size the device never sends, so every frame failed CRC.

Timing
------
Batches are stamped with the wall-clock time of their **first** sample, derived
from the sample count at the nominal rate (plus the filled gaps), not from when
the chunk happened to be read. After a reconnect a new segment starts with a
fresh anchor, and a batch never spans two segments.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol, final, runtime_checkable

from bitalino import BITalino

from src.clock import Clock, RealClock
from src.units import UnixMillis

logger = logging.getLogger(__name__)


# =========================================================================
# Channel naming
# =========================================================================

# Authoritative mapping between sensor names and BITalino analog channel indices
# (A1-A6 -> 0-5). This is the SINGLE source of truth for name<->channel: the
# session manager, the training plan and the simulator all derive from it.
CHANNEL_MAP: dict[str, int] = {
    "ECG": 0,  # A1 - Electrocardiography (heart)
    "EDA": 1,  # A2 - Electrodermal Activity (skin conductance/stress)
    "SpO2": 2,  # A3 - Pulse Oximetry (blood oxygen via finger clip)
    "RESP": 3,  # A4 - Respiration (chest band)
    "EMG": 4,  # A5 - Electromyography (muscle); 6-bit on the wire
    "LUX": 5,  # A6 - Light sensor or other; 6-bit on the wire
}

# Reverse lookup, derived from CHANNEL_MAP so the two can never drift apart.
CHANNEL_NAMES: dict[int, str] = {index: name for name, index in CHANNEL_MAP.items()}

VALID_SAMPLE_RATES: Final[frozenset[int]] = frozenset({1, 10, 100, 1000})
MIN_CHANNEL: Final[int] = 0
MAX_CHANNEL: Final[int] = 5

BITALINO_PIN: Final[str] = "1234"
MILLIS_PER_SECOND: Final[int] = 1000

SEQUENCE_MODULUS: Final[int] = 16
"""The frame sequence number is 4 bits, so a gap of 16 frames aliases to 0 and
larger gaps are counted modulo 16: gap counts are a lower bound."""

NO_SIGNAL_MAX: Final[int] = 4
NO_SIGNAL_STD: Final[float] = 1.0
"""A live analog channel swings across its range; one stuck at a near-constant
tiny value (the {0, 1, 3} seen with the sensor unplugged) carries no signal."""


# =========================================================================
# Records handed to the rest of the application
# =========================================================================


@dataclass(frozen=True, slots=True)
class ChannelData:
    """One channel's samples for one batch, in ADC counts."""

    channel: str
    values: Sequence[float]


@dataclass(frozen=True, slots=True)
class SampleBatch:
    """Time-contiguous samples from every acquired channel.

    ``timestamp`` is the wall-clock time of the FIRST sample; sample ``i`` was
    taken at ``timestamp + i * 1000 / sample_rate`` ms.
    """

    timestamp: UnixMillis
    channels: Sequence[ChannelData]


@dataclass(frozen=True, slots=True)
class LinkStats:
    """Cumulative link health since acquisition started. All counts in samples
    or bytes, never reset by a reconnect."""

    frames: int = 0
    """Frames that passed CRC and were decoded."""
    skipped_bytes: int = 0
    """Bytes discarded while hunting for a valid frame boundary."""
    sync_losses: int = 0
    """Times a frame failed CRC while the stream was aligned."""
    filled_samples: int = 0
    """Samples synthesised (held value) to cover frames the device dropped."""
    dropped_backlog_samples: int = 0
    """Samples discarded because nobody consumed them in time."""
    reconnects: int = 0
    """Successful re-opens of the link after it was lost."""


# =========================================================================
# Frame decoding (pure; the bit layout is the vendor's, verified against it)
# =========================================================================

type Row = tuple[int, ...]
"""One sample instant: the analog values in ascending channel order."""

_TEN_BIT_CHANNELS: Final[int] = 4


def frame_size(n_channels: int) -> int:
    """Bytes per frame for ``n_channels`` analog channels (1..6).

    A1-A4 are 10-bit, A5-A6 are 6-bit, plus 4 digital bits, a 4-bit sequence
    number and a 4-bit CRC.
    """
    if n_channels <= _TEN_BIT_CHANNELS:
        bits = 12 + 10 * n_channels
    else:
        bits = 52 + 6 * (n_channels - _TEN_BIT_CHANNELS)
    return -(-bits // 8)


def crc_ok(frame: bytes | bytearray) -> bool:
    """Check the 4-bit CRC in the low nibble of the last byte.

    The CRC is computed over the whole frame with that nibble zeroed; a straight
    port of the vendor's loop (polynomial x^4 + x + 1).
    """
    expected = frame[-1] & 0x0F
    crc = 0
    last = len(frame) - 1
    for index, byte in enumerate(frame):
        value = byte & 0xF0 if index == last else byte
        for bit in range(7, -1, -1):
            crc <<= 1
            if crc & 0x10:
                crc ^= 0x03
            crc ^= (value >> bit) & 0x01
    return (crc & 0x0F) == expected


_ANALOG_FIELDS: Final[tuple[Callable[[bytes | bytearray], int], ...]] = (
    lambda d: ((d[-2] & 0x0F) << 6) | (d[-3] >> 2),  # A1, 10-bit
    lambda d: ((d[-3] & 0x03) << 8) | d[-4],  # A2, 10-bit
    lambda d: (d[-5] << 2) | (d[-6] >> 6),  # A3, 10-bit
    lambda d: ((d[-6] & 0x3F) << 4) | (d[-7] >> 4),  # A4, 10-bit
    lambda d: ((d[-7] & 0x0F) << 2) | (d[-8] >> 6),  # A5, 6-bit
    lambda d: d[-8] & 0x3F,  # A6, 6-bit
)
"""Where each analog value sits, counted from the END of the frame. The Nth
field is the Nth *acquired* channel (ascending index), not channel N."""


def sequence_number(frame: bytes | bytearray) -> int:
    """The 4-bit frame counter, 0..15."""
    return frame[-1] >> 4


def decode_analog(frame: bytes | bytearray, n_channels: int) -> Row:
    """The analog values of a CRC-valid frame, in ascending channel order."""
    return tuple(field(frame) for field in _ANALOG_FIELDS[:n_channels])


class FrameDecoder:
    """Turns an arbitrary byte stream into rows, surviving corruption.

    Mutable by design (it owns a byte buffer and counters) and used by exactly
    one thread: the acquisition thread. A new instance is made per segment.
    """

    def __init__(self, n_channels: int) -> None:
        self._n: int = n_channels
        self._size: int = frame_size(n_channels)
        self._buffer: bytearray = bytearray()
        self._synced: bool = False
        self._last: tuple[int, Row] | None = None
        """Sequence number and values of the previous accepted frame."""
        self.frames: int = 0
        self.skipped_bytes: int = 0
        self.sync_losses: int = 0
        self.filled_samples: int = 0

    def feed(self, data: bytes) -> list[Row]:
        """Append ``data`` and return every row it completes, gaps filled."""
        self._buffer.extend(data)
        buffer = self._buffer
        size = self._size
        rows: list[Row] = []
        pos = 0
        while len(buffer) - pos >= size:
            frame = buffer[pos : pos + size]
            if self._synced:
                if crc_ok(frame):
                    self._accept(frame, rows)
                    pos += size
                    continue
                self._synced = False
                self.sync_losses += 1
            # Hunting: a lone 4-bit CRC passes by chance 1 time in 16, so demand
            # two valid frames with consecutive sequence numbers before locking.
            if len(buffer) - pos < 2 * size:
                break
            follower = buffer[pos + size : pos + 2 * size]
            if (
                crc_ok(frame)
                and crc_ok(follower)
                and sequence_number(follower) == (sequence_number(frame) + 1) % SEQUENCE_MODULUS
            ):
                self._synced = True
                continue
            pos += 1
            self.skipped_bytes += 1
        del buffer[:pos]
        return rows

    def _accept(self, frame: bytearray, rows: list[Row]) -> None:
        seq = sequence_number(frame)
        row = decode_analog(frame, self._n)
        if self._last is not None:
            last_seq, last_row = self._last
            missing = (seq - last_seq - 1) % SEQUENCE_MODULUS
            rows.extend([last_row] * missing)
            self.filled_samples += missing
        rows.append(row)
        self._last = (seq, row)
        self.frames += 1


# =========================================================================
# The device seam
# =========================================================================


class Device(Protocol):
    """What the client needs from an open BITalino. Positional-only so both the
    vendor adapter and test fakes satisfy it regardless of parameter names."""

    def version(self) -> str: ...
    def start(self, sample_rate: int, channels: Sequence[int], /) -> None: ...
    def read_chunk(self, max_bytes: int, /) -> bytes: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


type DeviceFactory = Callable[[str, float], Device]
"""Opens ``address`` with a link timeout in seconds. Blocking; may raise."""


class LinkLostError(Exception):
    """No byte arrived for a whole link timeout."""


@runtime_checkable
class _TimedPort(Protocol):
    """The part of a ``pyserial`` port the adapter uses. Checked structurally so
    this module never imports ``serial`` (rule 5 reserves it for the drive)."""

    timeout: float | None

    def read(self, size: int = 1) -> bytes: ...


@final
class VendorDevice:
    """Adapter over ``bitalino.BITalino`` implementing :class:`Device`.

    On a serial port (``COM4``, ``/dev/rfcomm0`` - the paths actually used) it
    reads in bulk straight from the port with a real timeout: the vendor's own
    timeout busy-waits a whole CPU core and its blocking mode can hang forever.
    On a PyBluez socket it falls back to the vendor's ``receive``.
    """

    def __init__(self, address: str, link_timeout: float) -> None:
        self._device: BITalino = BITalino(address, link_timeout)
        port = self._device.socket
        self._port: _TimedPort | None = port if isinstance(port, _TimedPort) else None
        if self._port is not None:
            self._port.timeout = link_timeout
        self._link_timeout: float = link_timeout

    def version(self) -> str:
        return self._device.version()

    def start(self, sample_rate: int, channels: Sequence[int], /) -> None:
        self._device.start(sample_rate, list(channels))

    def read_chunk(self, max_bytes: int, /) -> bytes:
        if self._port is None:
            return self._device.receive(max_bytes)
        data = self._port.read(max_bytes)
        if not data:
            raise LinkLostError(f"no data from the BITalino for {self._link_timeout:.1f} s")
        return data

    def stop(self) -> None:
        self._device.stop()

    def close(self) -> None:
        self._device.close()


# =========================================================================
# Pairing (Linux / BlueZ only; a no-op for COM and /dev paths)
# =========================================================================

MAC_ADDRESS: Final[re.Pattern[str]] = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

type CommandRunner = Callable[[Sequence[str], float], str]
"""Runs argv with a timeout in seconds and returns its stdout. Blocking."""

INFO_TIMEOUT_S: Final[float] = 10.0
PAIR_TIMEOUT_S: Final[float] = 60.0

# Run in a child interpreter so pexpect (Linux-only) is never imported here.
# The MAC and PIN arrive through argv, never through string formatting.
_PAIR_SCRIPT: Final[str] = """
import subprocess, sys
mac, pin = sys.argv[1], sys.argv[2]
try:
    import pexpect
except ImportError:
    for cmd in (["agent", "on"], ["default-agent"], ["pair", mac], ["trust", mac]):
        subprocess.run(["bluetoothctl", *cmd], capture_output=True, timeout=20)
    raise SystemExit(0)
child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=30)
for line in ("agent on", "default-agent", "pair " + mac):
    child.sendline(line)
try:
    prompts = ["PIN code", "Passkey", "Enter PIN", "successful", "AlreadyExists", "Failed"]
    if child.expect(prompts) < 3:
        child.sendline(pin)
        child.expect(["successful", "AlreadyExists", "Failed"])
except pexpect.exceptions.ExceptionPexpect:
    pass
child.sendline("trust " + mac)
child.sendline("quit")
child.close()
"""


def run_command(argv: Sequence[str], timeout: float) -> str:
    """The production :data:`CommandRunner`."""
    completed = subprocess.run(  # noqa: S603  # fixed argv built in this module
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )
    return completed.stdout


def _is_paired(mac: str, run: CommandRunner) -> bool:
    return "Paired: yes" in run(["bluetoothctl", "info", mac], INFO_TIMEOUT_S)


def ensure_paired(mac: str, pin: str = BITALINO_PIN, run: CommandRunner = run_command) -> bool:
    """Pair and trust ``mac`` through BlueZ unless it already is. Blocking.

    Only acts when asked about an unpaired device, so the normal cost is one
    ``bluetoothctl info``. Returns whether the device ends up paired; never
    raises, because a failed pairing is worth a log line, not a crash - the
    connect attempt that follows gives the definitive answer.
    """
    try:
        if _is_paired(mac, run):
            return True
        logger.info("Pairing with %s (PIN %s)...", mac, pin)
        run([sys.executable, "-c", _PAIR_SCRIPT, mac, pin], PAIR_TIMEOUT_S)
        paired = _is_paired(mac, run)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Could not run bluetoothctl to pair %s: %s", mac, error)
        return False
    if not paired:
        logger.warning("%s is still not paired; run scripts/pair_device.sh once by hand", mac)
    return paired


# =========================================================================
# The client
# =========================================================================

DEFAULT_RECONNECT_DELAYS: Final[tuple[float, ...]] = (0.5, 1.0, 2.0, 4.0, 8.0, 8.0)
"""Waits before each reconnect attempt, in seconds (~24 s in total). When they
run out, the disconnect callback fires and the session is ended. Meanwhile no
fresh heart rate is produced, so the safety supervisor's staleness rule - not
this module - decides what the motor does."""

DEFAULT_MAX_BACKLOG_S: Final[float] = 60.0
CHUNKS_PER_SECOND: Final[int] = 10


def link_timeout_for(sample_rate: int) -> float:
    """How long silence may last before the link counts as lost: 3 s, or five
    frame periods at the slow rates where frames are that far apart."""
    return max(3.0, 5.0 / sample_rate)


@dataclass(frozen=True, slots=True)
class _Chunk:
    segment: int
    first_ts: UnixMillis
    rows: Sequence[Row]


class BITalinoClient:
    """Bluetooth Classic client for BITalino / psychoBIT devices.

    The public surface (``connect``/``start_acquisition``/``read_samples``/
    ``stop_acquisition``/``disconnect``, ``is_connected``/``is_acquiring``) is
    what ``src/session_manager.py`` and the simulator in ``src/sim`` share.
    ``is_connected``/``is_acquiring`` are plain attributes written by both the
    event loop and, on give-up, the acquisition thread; single bool stores are
    atomic under the GIL and only ever move towards ``False`` from the thread.
    """

    def __init__(
        self,
        mac_address: str,
        channels: Sequence[int] | None = None,
        sample_rate: int = 1000,
        auto_pair: bool = True,
        *,
        clock: Clock | None = None,
        device_factory: DeviceFactory = VendorDevice,
        command_runner: CommandRunner = run_command,
        reconnect_delays: Sequence[float] = DEFAULT_RECONNECT_DELAYS,
        max_backlog_s: float = DEFAULT_MAX_BACKLOG_S,
    ) -> None:
        requested = list(channels) if channels is not None else [0]
        if sample_rate not in VALID_SAMPLE_RATES:
            raise ValueError(f"Sample rate must be 1, 10, 100, or 1000 Hz, got {sample_rate}")
        if not requested:
            raise ValueError("At least one analog channel is required")
        for ch in requested:
            if ch < MIN_CHANNEL or ch > MAX_CHANNEL:
                raise ValueError(f"Channel must be 0-5, got {ch}")
        wire = tuple(sorted(set(requested)))
        if list(wire) != requested:
            logger.warning(
                "Channels %s requested; the device streams them as %s (ascending, unique)",
                requested,
                list(wire),
            )

        self.mac_address: str = mac_address
        self.channels: tuple[int, ...] = wire
        """The acquired channels in the order they appear in every frame."""
        self.sample_rate: int = sample_rate
        self.auto_pair: bool = auto_pair
        self.is_connected: bool = False
        self.is_acquiring: bool = False

        self._clock: Clock = clock if clock is not None else RealClock()
        self._factory: DeviceFactory = device_factory
        self._run: CommandRunner = command_runner
        self._reconnect_delays: tuple[float, ...] = tuple(reconnect_delays)
        self._max_backlog: int = int(max_backlog_s * sample_rate)
        self._link_timeout: float = link_timeout_for(sample_rate)
        self._chunk_bytes: int = frame_size(len(wire)) * max(1, sample_rate // CHUNKS_PER_SECOND)

        self._device: Device | None = None
        self._on_disconnect: Callable[[], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop: threading.Event = threading.Event()

        # Shared between the acquisition thread (producer) and the event loop
        # (consumer); every access holds _lock.
        self._lock: threading.Lock = threading.Lock()
        self._chunks: deque[_Chunk] = deque()
        self._queued_rows: int = 0
        self._stats: LinkStats = LinkStats()

        # Owned by the acquisition thread alone.
        self._decoder: FrameDecoder = FrameDecoder(len(wire))
        self._segment: int = 0
        self._segment_anchor: UnixMillis | None = None
        self._segment_rows: int = 0
        self._next_ts_floor: int = 0
        self._banked: LinkStats = LinkStats()
        """Counters from decoders of earlier segments, so stats survive reconnects."""

        # Owned by the event loop alone.
        self._integrity_checked: bool = False
        self._reported: LinkStats = LinkStats()

    # -- connection ---------------------------------------------------------

    def set_disconnect_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Called on the event loop once the link is lost for good."""
        self._on_disconnect = callback

    async def connect(self, timeout: float = 30.0) -> bool:  # noqa: ASYNC109  # shared surface with the simulator
        """Open the device. Never blocks the event loop; returns success."""
        loop = asyncio.get_running_loop()
        if self.auto_pair and MAC_ADDRESS.match(self.mac_address):
            await loop.run_in_executor(
                None, ensure_paired, self.mac_address, BITALINO_PIN, self._run
            )
        logger.info("Connecting to BITalino at %s...", self.mac_address)
        opening = loop.run_in_executor(None, self._factory, self.mac_address, self._link_timeout)
        try:
            device = await asyncio.wait_for(asyncio.shield(opening), timeout)
        except TimeoutError:
            # The open is still running in its thread. If it succeeds later, close
            # it: a leaked open COM port makes every later attempt fail "busy".
            opening.add_done_callback(_close_if_opened)
            logger.warning("Connection to %s timed out after %.0f s", self.mac_address, timeout)
            return False
        except Exception as error:  # the vendor raises bare Exception
            logger.warning("Connection to %s failed: %s", self.mac_address, error)
            return False
        self._device = device
        self.is_connected = True
        try:
            version = await loop.run_in_executor(None, device.version)
            logger.info("Connected to %s", version)
        except Exception as error:  # informational only
            logger.warning("Connected, but could not read the firmware version: %s", error)
        return True

    async def disconnect(self) -> None:
        """Stop acquiring if needed and close the device."""
        await self.stop_acquisition()
        device = self._device
        self._device = None
        self.is_connected = False
        if device is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(None, device.close)
        except Exception as error:
            logger.warning("Error while closing the BITalino: %s", error)
        logger.info("Disconnected from BITalino")

    # -- acquisition --------------------------------------------------------

    async def start_acquisition(self) -> bool:
        """Start streaming and the background reader. Returns success."""
        device = self._device
        if not self.is_connected or device is None:
            logger.error("Cannot start acquisition: not connected")
            return False
        if self.is_acquiring:
            return True

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, device.start, self.sample_rate, self.channels)
        except Exception as error:
            logger.warning("Failed to start acquisition: %s", error)
            return False

        with self._lock:
            self._chunks.clear()
            self._queued_rows = 0
            self._stats = LinkStats()
        self._decoder = FrameDecoder(len(self.channels))
        self._segment = 0
        self._segment_anchor = None
        self._segment_rows = 0
        self._next_ts_floor = 0
        self._banked = LinkStats()
        self._integrity_checked = False
        self._reported = LinkStats()
        self._loop = loop
        self._stop.clear()

        self._thread = threading.Thread(
            target=self._acquisition_loop, name="bitalino-reader", daemon=True
        )
        self.is_acquiring = True
        self._thread.start()
        logger.info(
            "Started acquisition: channels=%s, rate=%d Hz", list(self.channels), self.sample_rate
        )
        return True

    async def stop_acquisition(self) -> None:
        """Stop the reader and the device stream. Safe to call when idle."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._stop.set()
        loop = asyncio.get_running_loop()
        device = self._device
        if device is not None and self.is_acquiring:
            try:
                await loop.run_in_executor(None, device.stop)
            except Exception as error:
                logger.warning("Error while stopping the BITalino stream: %s", error)
        # The reader wakes within one link timeout (its read times out).
        await loop.run_in_executor(None, thread.join, self._link_timeout + 1.0)
        self.is_acquiring = False
        logger.info("Stopped acquisition; link %s", self.link_stats())

    def link_stats(self) -> LinkStats:
        """A snapshot of the link counters for this acquisition."""
        with self._lock:
            return self._stats

    def _acquisition_loop(self) -> None:
        """Reader thread: bytes -> rows -> timestamped chunks; reconnects on loss."""
        device = self._device
        while device is not None and not self._stop.is_set():
            try:
                data = device.read_chunk(self._chunk_bytes)
            except Exception as error:  # vendor, serial and socket errors alike
                if self._stop.is_set():
                    return
                logger.warning("BITalino link lost: %s", error)
                device = self._reconnect(device)
                continue
            self._ingest(data)
        if device is None:
            self._device = None
            self._give_up()

    def _reconnect(self, lost: Device) -> Device | None:
        """Re-open the link with backoff. Returns the new device, or None."""
        try:
            lost.close()
        except Exception as error:
            logger.debug("Closing the lost link failed: %s", error)
        for attempt, delay in enumerate(self._reconnect_delays, start=1):
            if self._stop.wait(delay):
                return None
            try:
                device = self._factory(self.mac_address, self._link_timeout)
                device.start(self.sample_rate, self.channels)
            except Exception as error:
                logger.warning("Reconnect attempt %d failed: %s", attempt, error)
                continue
            self._device = device
            self._new_segment()
            with self._lock:
                self._stats = replace(self._stats, reconnects=self._stats.reconnects + 1)
            logger.warning("BITalino link re-established on attempt %d", attempt)
            return device
        return None

    def _new_segment(self) -> None:
        """Start a fresh timeline after a reconnect, keeping the counters."""
        self._banked = self._decoder_totals()
        self._decoder = FrameDecoder(len(self.channels))
        self._segment += 1
        self._segment_anchor = None
        self._segment_rows = 0

    def _decoder_totals(self) -> LinkStats:
        d = self._decoder
        b = self._banked
        return LinkStats(
            frames=b.frames + d.frames,
            skipped_bytes=b.skipped_bytes + d.skipped_bytes,
            sync_losses=b.sync_losses + d.sync_losses,
            filled_samples=b.filled_samples + d.filled_samples,
        )

    def _give_up(self) -> None:
        """The link could not be restored (or stop won the race)."""
        if self._stop.is_set():
            return
        logger.error("BITalino link could not be re-established; giving up")
        self.is_acquiring = False
        self.is_connected = False
        callback = self._on_disconnect
        loop = self._loop
        if callback is None or loop is None:
            return

        async def notify() -> None:
            await callback()

        coroutine = notify()
        try:
            asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError as error:  # the loop has already closed
            coroutine.close()
            logger.warning("Could not deliver the disconnect callback: %s", error)

    def _ingest(self, data: bytes) -> None:
        rows = self._decoder.feed(data)
        totals = self._decoder_totals()
        if not rows:
            with self._lock:
                self._stats = _merge(self._stats, totals)
            return
        if self._segment_anchor is None:
            now = self._clock.unix_millis()
            anchor = now - _millis(len(rows), self.sample_rate)
            self._segment_anchor = UnixMillis(max(anchor, self._next_ts_floor))
        first_ts = UnixMillis(self._segment_anchor + _millis(self._segment_rows, self.sample_rate))
        self._segment_rows += len(rows)
        self._next_ts_floor = (
            self._segment_anchor + _millis(self._segment_rows - 1, self.sample_rate) + 1
        )
        chunk = _Chunk(segment=self._segment, first_ts=first_ts, rows=rows)
        with self._lock:
            self._chunks.append(chunk)
            self._queued_rows += len(rows)
            dropped = 0
            while self._queued_rows > self._max_backlog and len(self._chunks) > 1:
                oldest = self._chunks.popleft()
                self._queued_rows -= len(oldest.rows)
                dropped += len(oldest.rows)
            self._stats = _merge(self._stats, totals, dropped)
        if dropped:
            logger.warning("Dropped %d unconsumed samples (backlog over limit)", dropped)

    # -- consumer -----------------------------------------------------------

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        """Everything queued, once at least ``count`` samples per channel are.

        Returns ALL contiguous queued samples rather than exactly ``count``, so
        a consumer that polls slightly slower than real time cannot build an
        ever-growing backlog. A batch never spans a reconnect: a segment that
        has ended is returned even if shorter than ``count``.
        """
        with self._lock:
            if not self._chunks:
                return None
            segment = self._chunks[0].segment
            run = 0
            available = 0
            for chunk in self._chunks:
                if chunk.segment != segment:
                    break
                run += 1
                available += len(chunk.rows)
            segment_closed = run < len(self._chunks)
            if available < count and not segment_closed:
                return None
            taken = [self._chunks.popleft() for _ in range(run)]
            self._queued_rows -= available
            stats = self._stats

        rows = [row for chunk in taken for row in chunk.rows]
        channels = [
            ChannelData(
                channel=CHANNEL_NAMES[channel],
                values=[float(row[column]) for row in rows],
            )
            for column, channel in enumerate(self.channels)
        ]
        self._report(stats)
        self._check_signal_integrity(channels)
        return SampleBatch(timestamp=taken[0].first_ts, channels=channels)

    def _report(self, stats: LinkStats) -> None:
        """Log link trouble since the previous batch, once per batch at most."""
        before = self._reported
        self._reported = stats
        filled = stats.filled_samples - before.filled_samples
        skipped = stats.skipped_bytes - before.skipped_bytes
        losses = stats.sync_losses - before.sync_losses
        if filled or skipped or losses:
            logger.warning(
                "BITalino link: %d dropped samples filled, %d corrupt frames, %d bytes skipped",
                filled,
                losses,
                skipped,
            )

    def _check_signal_integrity(self, channels: Sequence[ChannelData]) -> None:
        """Warn once per acquisition about a channel carrying no analog signal."""
        if self._integrity_checked:
            return
        self._integrity_checked = True
        for ch in channels:
            values = ch.values
            peak = max(values)
            spread = statistics.pstdev(values)
            if peak <= NO_SIGNAL_MAX and spread < NO_SIGNAL_STD:
                logger.warning(
                    "Channel %s has no analog signal (max=%d, std=%.2f) - check the "
                    "sensor is plugged into its BITalino port.",
                    ch.channel,
                    int(peak),
                    spread,
                )


# =========================================================================
# Small helpers
# =========================================================================


def _millis(samples: int, sample_rate: int) -> int:
    return round(samples * MILLIS_PER_SECOND / sample_rate)


def _merge(stats: LinkStats, totals: LinkStats, dropped: int = 0) -> LinkStats:
    """Decoder totals plus the counters only the client keeps."""
    return replace(
        totals,
        dropped_backlog_samples=stats.dropped_backlog_samples + dropped,
        reconnects=stats.reconnects,
    )


def _close_if_opened(opening: asyncio.Future[Device]) -> None:
    """Done-callback for an open that finished after we stopped waiting."""
    if opening.cancelled() or opening.exception() is not None:
        return
    try:
        opening.result().close()
    except Exception as error:
        logger.warning("Could not close a late-opened BITalino: %s", error)
