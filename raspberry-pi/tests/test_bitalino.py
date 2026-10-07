"""BITalino client: frame decoding, resynchronisation, timing, reconnects.

The decoder is checked differentially against the vendor's own ``read()`` so
the bit layout cannot drift from the device's, then everything else runs
against a scripted fake device through the public surface only.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Final, final

import pytest
from bitalino import BITalino
from hypothesis import given
from hypothesis import strategies as st

from src import bitalino_client
from src.bitalino_client import (
    CHANNEL_MAP,
    CHANNEL_NAMES,
    BITalinoClient,
    Device,
    FrameDecoder,
    LinkLostError,
    SampleBatch,
    VendorDevice,
    crc_ok,
    decode_analog,
    ensure_paired,
    frame_size,
    link_timeout_for,
    run_command,
    sequence_number,
)
from src.clock import ManualClock
from src.units import Monotonic, UnixMillis

EPOCH: Final[UnixMillis] = UnixMillis(1_000_000)
MAC: Final[str] = "AA:BB:CC:DD:EE:FF"
WAIT_S: Final[float] = 3.0
POLL_S: Final[float] = 0.002

# 10-bit for A1..A4, 6-bit for A5..A6.
MAX_VALUES: Final[tuple[int, ...]] = (1023, 1023, 1023, 1023, 63, 63)


# =========================================================================
# Frame construction (the inverse of the vendor's bit layout)
# =========================================================================


def encode_frame(seq: int, values: Sequence[int], n_channels: int) -> bytes:
    """Pack one frame exactly as the firmware does, CRC included."""
    size = frame_size(n_channels)
    d = [0] * size
    v = [*values, 0, 0, 0, 0, 0, 0]
    d[-1] = seq << 4
    d[-2] |= v[0] >> 6
    d[-3] |= (v[0] & 0x3F) << 2
    if n_channels > 1:
        d[-3] |= v[1] >> 8
        d[-4] |= v[1] & 0xFF
    if n_channels > 2:
        d[-5] |= v[2] >> 2
        d[-6] |= (v[2] & 0x03) << 6
    if n_channels > 3:
        d[-6] |= v[3] >> 4
        d[-7] |= (v[3] & 0x0F) << 4
    if n_channels > 4:
        d[-7] |= v[4] >> 2
        d[-8] |= (v[4] & 0x03) << 6
    if n_channels > 5:
        d[-8] |= v[5] & 0x3F
    for crc in range(16):
        candidate = bytes([*d[:-1], d[-1] | crc])
        if crc_ok(candidate):
            return candidate
    raise AssertionError("no CRC nibble validates the frame")


def stream(values: Sequence[int], *, first_seq: int = 0, skip: Sequence[int] = ()) -> bytes:
    """Single-channel frames carrying ``values``; indices in ``skip`` are
    consumed from the sequence but not sent (the device dropped them)."""
    out = bytearray()
    for index, value in enumerate(values):
        if index not in skip:
            out += encode_frame((first_seq + index) % 16, [value], 1)
    return bytes(out)


# =========================================================================
# 1. Decoding agrees with the vendor, bit for bit
# =========================================================================


@final
class _BytePort:
    """Stands in for the vendor's serial port: hands out one byte per read."""

    def __init__(self, data: bytes) -> None:
        self._data = deque(data)

    def read(self, size: int = 1) -> bytes:
        return bytes(self._data.popleft() for _ in range(size))


def _vendor_decoder(data: bytes, n_channels: int) -> BITalino:
    """A vendor instance mid-acquisition, reading from ``data``."""
    device = BITalino.__new__(BITalino)
    state: dict[str, object] = {
        "started": True,
        "analogChannels": list(range(n_channels)),
        "serial": True,
        "isPython2": False,
        "blocking": True,
        "socket": _BytePort(data),
    }
    for name, value in state.items():
        setattr(device, name, value)
    return device


@given(
    n_channels=st.integers(min_value=1, max_value=6),
    seq=st.integers(min_value=0, max_value=15),
    raw=st.lists(st.integers(min_value=0, max_value=1023), min_size=6, max_size=6),
)
def test_decoding_matches_the_vendor(n_channels: int, seq: int, raw: list[int]) -> None:
    values = [min(value, top) for value, top in zip(raw, MAX_VALUES, strict=True)][:n_channels]
    frame = encode_frame(seq, values, n_channels)

    matrix = _vendor_decoder(frame, n_channels).read(1)

    assert matrix.item(0, 0) == sequence_number(frame) == seq
    vendor = tuple(matrix.item(0, 5 + k) for k in range(n_channels))
    assert decode_analog(frame, n_channels) == vendor == tuple(values)


@given(
    n_channels=st.integers(min_value=1, max_value=6),
    position=st.integers(min_value=0, max_value=7),
    bit=st.integers(min_value=0, max_value=7),
)
def test_crc_verdict_matches_the_vendor(n_channels: int, position: int, bit: int) -> None:
    """A single flipped bit anywhere is rejected by both, never by one only."""
    frame = bytearray(encode_frame(5, [100] * n_channels, n_channels))
    frame[position % len(frame)] ^= 1 << bit

    try:
        _vendor_decoder(bytes(frame), n_channels).read(1)
        vendor_ok = True
    except Exception:  # the vendor raises a bare Exception on a bad CRC
        vendor_ok = False

    assert crc_ok(frame) is vendor_ok is False


@pytest.mark.parametrize(("n_channels", "size"), [(1, 3), (2, 4), (3, 6), (4, 7), (5, 8), (6, 8)])
def test_frame_sizes(n_channels: int, size: int) -> None:
    assert frame_size(n_channels) == size


# =========================================================================
# 2. The stream decoder survives what a noisy link does
# =========================================================================


def test_a_clean_stream_decodes_every_frame() -> None:
    decoder = FrameDecoder(1)
    assert decoder.feed(stream([10, 20, 30, 40])) == [(10,), (20,), (30,), (40,)]
    assert (decoder.frames, decoder.skipped_bytes, decoder.filled_samples) == (4, 0, 0)


def test_frames_split_across_reads_are_reassembled() -> None:
    data = stream([1, 2, 3, 4, 5])
    decoder = FrameDecoder(1)
    rows = [row for i in range(0, len(data), 2) for row in decoder.feed(data[i : i + 2])]
    assert rows == [(1,), (2,), (3,), (4,), (5,)]


def test_joining_mid_frame_skips_to_the_next_boundary() -> None:
    decoder = FrameDecoder(1)
    rows = decoder.feed(b"\x00\xff" + stream([7, 8, 9]))
    assert rows == [(7,), (8,), (9,)]
    assert decoder.skipped_bytes == 2


def test_a_corrupt_frame_costs_one_sample_not_the_session() -> None:
    """The old client ended the session here; now the frame is skipped, the
    stream relocks, and the lost sample is held so timing stays right."""
    data = bytearray(stream([10, 20, 30, 40, 50]))
    data[7] ^= 0xFF  # corrupt the third frame (bytes 6..8)
    decoder = FrameDecoder(1)

    rows = decoder.feed(bytes(data))

    assert rows == [(10,), (20,), (20,), (40,), (50,)]
    assert decoder.sync_losses == 1
    assert decoder.skipped_bytes == 3
    assert decoder.filled_samples == 1


def test_frames_the_device_dropped_are_filled_by_holding_the_last_value() -> None:
    """A missing sample shortens every RR interval across it (reads as a higher
    heart rate), so the count of samples must be preserved."""
    decoder = FrameDecoder(1)
    rows = decoder.feed(stream([1, 2, 3, 4, 5, 6], skip=[2, 3]))
    assert rows == [(1,), (2,), (2,), (2,), (5,), (6,)]
    assert decoder.filled_samples == 2


def test_the_sequence_number_wraps_without_a_false_gap() -> None:
    decoder = FrameDecoder(1)
    rows = decoder.feed(stream([1, 2, 3], first_seq=15))
    assert rows == [(1,), (2,), (3,)]
    assert decoder.filled_samples == 0


def test_a_lone_frame_is_not_trusted_until_its_successor_arrives() -> None:
    decoder = FrameDecoder(1)
    data = stream([1, 2])
    assert decoder.feed(data[:3]) == []
    assert decoder.feed(data[3:]) == [(1,), (2,)]


# =========================================================================
# 3. Fakes for the client
# =========================================================================


@final
class FakeDevice:
    """A scripted device. Script items: bytes are returned, an exception is
    raised, an Event is waited on and then the link is reported lost."""

    def __init__(
        self,
        script: Sequence[bytes | Exception | threading.Event] = (),
        *,
        version: str | Exception = "BITalino_v5.2",
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._script: deque[bytes | Exception | threading.Event] = deque(script)
        self._lock = threading.Lock()
        self._version = version
        self._start_error = start_error
        self._stop_error = stop_error
        self._close_error = close_error
        self.started_with: tuple[int, tuple[int, ...]] | None = None
        self.stopped = threading.Event()
        self.closed = threading.Event()

    def push(self, item: bytes | Exception | threading.Event) -> None:
        with self._lock:
            self._script.append(item)

    def version(self) -> str:
        if isinstance(self._version, Exception):
            raise self._version
        return self._version

    def start(self, sample_rate: int, channels: Sequence[int], /) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started_with = (sample_rate, tuple(channels))

    def read_chunk(self, max_bytes: int, /) -> bytes:
        assert max_bytes > 0
        if self.stopped.is_set() or self.closed.is_set():
            raise LinkLostError("stopped")
        with self._lock:
            item = self._script.popleft() if self._script else None
        if item is None:
            self.stopped.wait(POLL_S)
            return b""
        if isinstance(item, threading.Event):
            # Blocks like a real read until released - or until stop() makes
            # the stream (and so the read) fail.
            while not item.is_set() and not self.stopped.is_set():
                self.stopped.wait(POLL_S)
            raise LinkLostError("released")
        if isinstance(item, Exception):
            raise item
        return item

    def stop(self) -> None:
        self.stopped.set()
        if self._stop_error is not None:
            raise self._stop_error

    def close(self) -> None:
        self.closed.set()
        if self._close_error is not None:
            raise self._close_error


@final
class Factory:
    """Hands out the given devices (or raises the given errors) in order."""

    def __init__(self, *outcomes: FakeDevice | Exception) -> None:
        self._outcomes: deque[FakeDevice | Exception] = deque(outcomes)
        self.calls: list[tuple[str, float]] = []

    def __call__(self, address: str, timeout: float) -> Device:
        self.calls.append((address, timeout))
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(
    factory: Callable[[str, float], Device],
    *,
    channels: Sequence[int] = (0,),
    sample_rate: int = 1000,
    reconnect_delays: Sequence[float] = (0.0,),
    max_backlog_s: float = 60.0,
    address: str = "COM4",
) -> BITalinoClient:
    return BITalinoClient(
        address,
        channels=channels,
        sample_rate=sample_rate,
        clock=ManualClock(start=Monotonic(0.0), epoch_millis=EPOCH),
        device_factory=factory,
        command_runner=_no_commands,
        reconnect_delays=reconnect_delays,
        max_backlog_s=max_backlog_s,
    )


def _no_commands(argv: Sequence[str], timeout: float) -> str:
    pytest.fail(f"unexpected command {list(argv)} (timeout {timeout})")


async def eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(int(WAIT_S / POLL_S)):
        if predicate():
            return
        await asyncio.sleep(POLL_S)
    pytest.fail("condition not reached in time")


async def next_batch(client: BITalinoClient, count: int) -> SampleBatch:
    for _ in range(int(WAIT_S / POLL_S)):
        batch = await client.read_samples(count)
        if batch is not None:
            return batch
        await asyncio.sleep(POLL_S)
    raise AssertionError("no batch arrived in time")


async def running(client: BITalinoClient) -> None:
    assert await client.connect()
    assert await client.start_acquisition()


# =========================================================================
# 4. Construction
# =========================================================================


def test_channel_names_are_the_inverse_of_the_channel_map() -> None:
    assert {index: name for name, index in CHANNEL_MAP.items()} == CHANNEL_NAMES
    assert CHANNEL_MAP["ECG"] == 0


def test_defaults() -> None:
    client = BITalinoClient(MAC)
    assert client.channels == (0,)
    assert client.sample_rate == 1000
    assert not client.is_connected
    assert not client.is_acquiring


def test_channels_are_put_in_the_order_the_device_sends_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The start command is a bitmask: request order cannot survive it."""
    client = make_client(Factory(), channels=[3, 0, 3])
    assert client.channels == (0, 3)
    assert "ascending, unique" in caplog.text


@pytest.mark.parametrize(
    ("channels", "sample_rate", "message"),
    [([0], 500, "Sample rate"), ([6], 1000, "Channel must be"), ([], 1000, "At least one")],
)
def test_invalid_configuration_is_rejected(
    channels: list[int], sample_rate: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        BITalinoClient(MAC, channels=channels, sample_rate=sample_rate)


def test_link_timeout_covers_slow_frame_rates() -> None:
    assert link_timeout_for(1000) == 3.0
    assert link_timeout_for(1) == 5.0


# =========================================================================
# 5. Connecting
# =========================================================================


async def test_connect_opens_the_device_and_reads_its_version(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    factory = Factory(FakeDevice())
    client = make_client(factory)
    assert await client.connect()
    assert client.is_connected
    assert factory.calls == [("COM4", 3.0)]
    assert "BITalino_v5.2" in caplog.text


async def test_an_unreadable_version_does_not_fail_the_connection() -> None:
    client = make_client(Factory(FakeDevice(version=OSError("garbled"))))
    assert await client.connect()


async def test_a_failed_open_reports_false() -> None:
    client = make_client(Factory(OSError("port busy")))
    assert not await client.connect()
    assert not client.is_connected


async def test_an_open_that_finishes_after_the_timeout_is_closed() -> None:
    """Otherwise the port stays open and every later attempt fails 'busy'."""
    gate = threading.Event()
    late = FakeDevice()

    def slow(_address: str, _timeout: float) -> Device:
        gate.wait(WAIT_S)
        return late

    client = make_client(slow)
    assert not await client.connect(timeout=0.01)
    gate.set()
    await eventually(late.closed.is_set)


async def test_a_late_open_that_fails_needs_no_cleanup() -> None:
    gate = threading.Event()
    finished = threading.Event()

    def slow(_address: str, _timeout: float) -> Device:
        gate.wait(WAIT_S)
        finished.set()
        raise OSError("gave up")

    client = make_client(slow)
    assert not await client.connect(timeout=0.01)
    gate.set()
    await eventually(finished.is_set)
    await asyncio.sleep(0.01)


async def test_a_late_open_that_cannot_be_closed_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = threading.Event()
    late = FakeDevice(close_error=OSError("stuck"))

    def slow(_address: str, _timeout: float) -> Device:
        gate.wait(WAIT_S)
        return late

    client = make_client(slow)
    assert not await client.connect(timeout=0.01)
    gate.set()
    await eventually(lambda: "late-opened" in caplog.text)


async def test_a_mac_address_is_paired_before_connecting() -> None:
    commands: list[list[str]] = []

    def runner(argv: Sequence[str], _timeout: float) -> str:
        commands.append(list(argv))
        return "Paired: yes"

    client = BITalinoClient(MAC, device_factory=Factory(FakeDevice()), command_runner=runner)
    assert await client.connect()
    assert commands == [["bluetoothctl", "info", MAC]]


# =========================================================================
# 6. Pairing
# =========================================================================


def test_an_already_paired_device_is_left_alone() -> None:
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], _timeout: float) -> str:
        calls.append(list(argv))
        return "Name: BITalino\nPaired: yes\n"

    assert ensure_paired(MAC, run=runner)
    assert len(calls) == 1


@pytest.mark.parametrize("pairs", [True, False])
def test_an_unpaired_device_is_paired_once(pairs: bool) -> None:
    answers = deque(["Paired: no", "", "Paired: yes" if pairs else "Paired: no"])
    calls: list[list[str]] = []

    def runner(argv: Sequence[str], _timeout: float) -> str:
        calls.append(list(argv))
        return answers.popleft()

    assert ensure_paired(MAC, "1234", runner) is pairs
    assert calls[1][:2] == [sys.executable, "-c"]
    assert calls[1][-2:] == [MAC, "1234"]  # through argv, never interpolated


def test_a_missing_bluetoothctl_is_not_a_crash() -> None:
    def runner(argv: Sequence[str], _timeout: float) -> str:
        raise FileNotFoundError(argv[0])

    assert not ensure_paired(MAC, run=runner)


def test_the_real_command_runner_returns_stdout() -> None:
    assert run_command([sys.executable, "-c", "print('hi')"], 30.0) == "hi\n"


# =========================================================================
# 7. Acquisition, batching and timing
# =========================================================================


async def test_starting_without_a_connection_fails() -> None:
    assert not await make_client(Factory()).start_acquisition()


async def test_a_refused_start_reports_false() -> None:
    client = make_client(Factory(FakeDevice(start_error=OSError("nak"))))
    assert await client.connect()
    assert not await client.start_acquisition()
    assert not client.is_acquiring


async def test_samples_come_back_labelled_in_wire_order() -> None:
    frames = b"".join(encode_frame(i % 16, [100 + i, 200 + i], 2) for i in range(10))
    device = FakeDevice([frames])
    client = make_client(Factory(device), channels=[3, 0])
    await running(client)
    assert device.started_with == (1000, (0, 3))
    assert await client.start_acquisition()  # already running: a no-op

    batch = await next_batch(client, 10)

    assert [ch.channel for ch in batch.channels] == ["ECG", "RESP"]
    assert list(batch.channels[0].values) == [100.0 + i for i in range(10)]
    assert list(batch.channels[1].values) == [200.0 + i for i in range(10)]
    await client.disconnect()


async def test_nothing_is_returned_before_enough_samples_arrive() -> None:
    device = FakeDevice([stream([1, 2, 3])])
    client = make_client(Factory(device))
    assert await client.read_samples(1) is None  # not acquiring yet
    await running(client)
    await eventually(lambda: client.link_stats().frames == 3)
    assert await client.read_samples(4) is None
    device.push(stream([4], first_seq=3))
    batch = await next_batch(client, 4)
    assert list(batch.channels[0].values) == [1.0, 2.0, 3.0, 4.0]
    await client.disconnect()


async def test_everything_queued_is_returned_so_no_backlog_builds() -> None:
    """Asking for 2 must drain all 5: taking exactly `count` per poll is what
    made the old client fall steadily behind real time."""
    client = make_client(Factory(FakeDevice([stream([1, 2, 3, 4, 5])])))
    await running(client)
    await eventually(lambda: client.link_stats().frames == 5)
    batch = await next_batch(client, 2)
    assert len(batch.channels[0].values) == 5
    assert await client.read_samples(1) is None
    await client.disconnect()


async def test_timestamps_follow_the_sample_count_not_the_read_time() -> None:
    device = FakeDevice([stream(range(100))])
    client = make_client(Factory(device))
    await running(client)
    first = await next_batch(client, 100)
    device.push(stream(range(50), first_seq=100 % 16))
    second = await next_batch(client, 50)

    # The first chunk is anchored so its last sample lands at "now".
    assert first.timestamp == EPOCH - 100
    assert second.timestamp == first.timestamp + 100
    await client.disconnect()


async def test_link_trouble_is_reported_with_the_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_client(Factory(FakeDevice([stream([1, 2, 3, 4, 5], skip=[2]), b"\x00"])))
    await running(client)
    batch = await next_batch(client, 5)
    assert list(batch.channels[0].values) == [1.0, 2.0, 2.0, 4.0, 5.0]
    assert "1 dropped samples filled" in caplog.text
    assert client.link_stats().filled_samples == 1
    await client.disconnect()


async def test_a_dead_channel_is_flagged_once(caplog: pytest.LogCaptureFixture) -> None:
    device = FakeDevice([stream([0, 1, 0, 1])])
    client = make_client(Factory(device))
    await running(client)
    await next_batch(client, 4)
    device.push(stream([0, 1], first_seq=4))
    await next_batch(client, 2)
    assert caplog.text.count("has no analog signal") == 1
    await client.disconnect()


async def test_a_live_channel_is_not_flagged(caplog: pytest.LogCaptureFixture) -> None:
    client = make_client(Factory(FakeDevice([stream([500, 510, 490, 520])])))
    await running(client)
    await next_batch(client, 4)
    assert "has no analog signal" not in caplog.text
    await client.disconnect()


async def test_an_unconsumed_backlog_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    chunks = [stream([i, i, i], first_seq=3 * i) for i in range(4)]
    client = make_client(Factory(FakeDevice(chunks)), sample_rate=10, max_backlog_s=0.7)
    await running(client)
    await eventually(lambda: client.link_stats().dropped_backlog_samples == 6)
    batch = await next_batch(client, 1)
    assert list(batch.channels[0].values) == [2.0, 2.0, 2.0, 3.0, 3.0, 3.0]
    assert "backlog over limit" in caplog.text
    await client.disconnect()


# =========================================================================
# 8. Losing the link
# =========================================================================


async def test_a_lost_link_reconnects_and_starts_a_new_segment() -> None:
    first = FakeDevice([stream([1, 2]), LinkLostError("gone")])
    second = FakeDevice([stream([7, 8, 9])])
    factory = Factory(first, OSError("not yet"), second)
    client = make_client(factory, reconnect_delays=(0.0, 0.0))
    await running(client)

    before = await next_batch(client, 100)  # short: its segment has ended
    after = await next_batch(client, 3)

    assert list(before.channels[0].values) == [1.0, 2.0]
    assert list(after.channels[0].values) == [7.0, 8.0, 9.0]
    assert after.timestamp > before.timestamp
    assert first.closed.is_set()
    assert second.started_with == (1000, (0,))
    stats = client.link_stats()
    assert (stats.reconnects, stats.frames) == (1, 5)
    await client.disconnect()


async def test_a_link_that_cannot_be_restored_notifies_the_owner() -> None:
    notified = asyncio.Event()

    async def on_disconnect() -> None:
        notified.set()

    device = FakeDevice([LinkLostError("gone")], close_error=OSError("already closed"))
    client = make_client(Factory(device, OSError("no")), reconnect_delays=(0.0,))
    client.set_disconnect_callback(on_disconnect)
    await running(client)

    await asyncio.wait_for(notified.wait(), WAIT_S)
    assert not client.is_acquiring
    assert not client.is_connected
    await client.disconnect()


async def test_giving_up_without_a_callback_is_quiet() -> None:
    client = make_client(Factory(FakeDevice([LinkLostError("gone")])), reconnect_delays=())
    await running(client)
    await eventually(lambda: not client.is_acquiring)
    await client.stop_acquisition()


def test_a_callback_for_a_closed_loop_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = threading.Event()
    client = make_client(Factory(FakeDevice([gate])), reconnect_delays=())

    async def on_disconnect() -> None:
        pytest.fail("the loop is closed; this must not run")

    async def scenario() -> None:
        client.set_disconnect_callback(on_disconnect)
        await running(client)

    asyncio.run(scenario())
    gate.set()
    for _ in range(int(WAIT_S / POLL_S)):
        if "Could not deliver" in caplog.text:
            break
        time.sleep(POLL_S)
    assert "Could not deliver" in caplog.text


async def test_stopping_during_a_reconnect_wait_ends_cleanly() -> None:
    notified = asyncio.Event()

    async def on_disconnect() -> None:
        notified.set()

    client = make_client(Factory(FakeDevice([LinkLostError("gone")])), reconnect_delays=(60.0,))
    client.set_disconnect_callback(on_disconnect)
    await running(client)
    await asyncio.sleep(0.02)  # the reader is now waiting to retry
    await client.stop_acquisition()
    assert not client.is_acquiring
    assert not notified.is_set()  # a deliberate stop is not a disconnect


# =========================================================================
# 9. Stopping and disconnecting
# =========================================================================


async def test_a_read_broken_by_our_own_stop_is_not_a_lost_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = FakeDevice([threading.Event()])
    client = make_client(Factory(device))
    await running(client)
    await asyncio.sleep(0.02)  # the reader is now blocked in read_chunk
    await client.stop_acquisition()
    assert "link lost" not in caplog.text
    assert not device.closed.is_set()  # closing is disconnect()'s job


async def test_stop_without_start_and_disconnect_without_connect_are_no_ops() -> None:
    client = make_client(Factory())
    await client.stop_acquisition()
    await client.disconnect()
    assert not client.is_connected


async def test_errors_while_shutting_down_are_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = FakeDevice(stop_error=OSError("nak"), close_error=OSError("busy"))
    client = make_client(Factory(device))
    await running(client)
    await client.disconnect()
    assert "stopping the BITalino stream" in caplog.text
    assert "closing the BITalino" in caplog.text
    assert not client.is_connected


# =========================================================================
# 10. The vendor adapter
# =========================================================================


@final
class _Port:
    def __init__(self, data: bytes) -> None:
        self.timeout: float | None = None
        self._data = data

    def read(self, size: int = 1) -> bytes:
        data, self._data = self._data[:size], self._data[size:]
        return data


@final
class _FakeVendor:
    """Records what the adapter asks of ``bitalino.BITalino``."""

    last: _FakeVendor | None = None

    def __init__(self, address: str, timeout: float | None = None) -> None:
        self.address = address
        self.timeout = timeout
        self.socket: object = _Port(b"\x01\x02") if address.startswith("COM") else object()
        self.calls: list[str] = []
        _FakeVendor.last = self

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, SamplingRate: int = 1000, analogChannels: Sequence[int] = ()) -> None:  # noqa: N803  # vendor names
        self.calls.append(f"start {SamplingRate} {list(analogChannels)}")

    def receive(self, nbytes: int) -> bytes:
        return b"\x09" * nbytes

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


def _vendor(monkeypatch: pytest.MonkeyPatch, address: str) -> tuple[VendorDevice, _FakeVendor]:
    monkeypatch.setattr(bitalino_client, "BITalino", _FakeVendor)
    device = VendorDevice(address, 3.0)
    fake = _FakeVendor.last
    assert fake is not None
    return device, fake


def test_a_serial_port_is_read_in_bulk_with_a_real_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, fake = _vendor(monkeypatch, "COM4")
    assert isinstance(fake.socket, _Port)
    assert fake.socket.timeout == 3.0
    assert device.version() == "BITalino_v5.2"
    assert device.read_chunk(10) == b"\x01\x02"
    with pytest.raises(LinkLostError, match="no data"):
        device.read_chunk(10)
    device.start(1000, (0, 3))
    device.stop()
    device.close()
    assert fake.calls == ["start 1000 [0, 3]", "stop", "close"]


def test_a_bluetooth_socket_uses_the_vendor_receive(monkeypatch: pytest.MonkeyPatch) -> None:
    device, _ = _vendor(monkeypatch, MAC)
    assert device.read_chunk(3) == b"\x09\x09\x09"
