"""The macOS IOBluetooth RFCOMM transport, against fake channels. No Bluetooth.

Three layers of evidence:

* **The device alone** (:class:`RfcommDevice`) against :class:`FakeLink`, a
  scripted :class:`~src.bitalino_rfcomm_macos.NativeLink`: silence raises
  ``LinkLostError``, fragmented bytes reassemble through the real
  ``FrameDecoder``, commands go out in the vendor's byte format, and ``close()``
  stops the run-loop thread.
* **The pyobjc glue** (:func:`open_iobluetooth_channel`) against fake
  ``Foundation`` and ``IOBluetooth`` modules put in ``sys.modules``, so the
  lazy import resolves to them on any platform - including the Pi, where
  pyobjc does not exist - and the whole chain runs under
  ``BITalinoClient``.
* **On macOS only**, the real pyobjc: the stubs name members the binding really
  has, and the delegate class built at run time really receives the
  ``rfcommChannelData:data:length:`` callback. Neither opens a device.

Hard rule for this file: nothing pages, connects to or opens a real Bluetooth
device. ``IOBluetoothDevice`` is only ever a fake here.
"""

from __future__ import annotations

import ast
import asyncio
import itertools
import logging
import sys
import threading
import time
import types
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Final, Protocol, cast, final, runtime_checkable

import pytest

from src import bitalino_rfcomm_macos as rfcomm
from src.bitalino_client import BITalinoClient, FrameDecoder, LinkLostError, VendorDevice
from src.bitalino_rfcomm_macos import (
    BITALINO_RFCOMM_CHANNEL,
    MAX_BUFFERED_BYTES,
    STOP_COMMAND,
    VERSION_COMMAND,
    ChannelEvents,
    LinkState,
    NativeLink,
    RfcommDevice,
    device_factory_for,
    is_rfcomm_address,
    parse_rfcomm_address,
    parse_version,
    rate_command,
    rfcomm_factory,
    start_command,
)
from tests.test_bitalino import stream

ADDRESS: Final[str] = "rfcomm:98-D3-91-FE-4E-9F"
MAC: Final[str] = "98-d3-91-fe-4e-9f"
LINK_TIMEOUT: Final[float] = 0.05
VERSION_REPLY: Final[bytes] = b"\x00\x13BITalino_v5.2\n"


# =========================================================================
# Pure pieces
# =========================================================================


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (ADDRESS, MAC),
        ("rfcomm:98-d3-91-fe-4e-9f", MAC),
        ("  RFCOMM:98-d3-91-fe-4e-9f ", MAC),
        ("98:d3:91:fe:4e:9f", None),
        ("rfcomm:98:d3:91:fe:4e:9f", None),
        ("rfcomm:98-d3-91-fe-4e", None),
        ("rfcomm:98-d3-91-fe-4e-9g", None),
        ("/dev/rfcomm0", None),
        ("", None),
    ],
)
def test_the_address_scheme(address: str, expected: str | None) -> None:
    assert parse_rfcomm_address(address) == expected
    assert is_rfcomm_address(address) is (expected is not None)


def test_the_address_selects_the_transport() -> None:
    assert device_factory_for(ADDRESS) is rfcomm_factory
    assert device_factory_for("/dev/rfcomm0") is VendorDevice
    assert device_factory_for("98:D3:91:FE:4E:9F") is VendorDevice


@pytest.mark.parametrize(("rate", "byte"), [(1, 0x03), (10, 0x43), (100, 0x83), (1000, 0xC3)])
def test_the_rate_command_matches_the_vendor(rate: int, byte: int) -> None:
    assert rate_command(rate) == bytes([byte])


@pytest.mark.parametrize("rate", [0, 2, 500, 2000])
def test_an_invalid_rate_is_refused(rate: int) -> None:
    with pytest.raises(ValueError, match="Sample rate"):
        rate_command(rate)


@pytest.mark.parametrize(
    ("channels", "byte"),
    [([0], 0b0000_0101), ([0, 1], 0b0000_1101), ([5], 0b1000_0001), ([0, 1, 2, 3, 4, 5], 0xFD)],
)
def test_the_start_command_matches_the_vendor(channels: Sequence[int], byte: int) -> None:
    assert start_command(channels) == bytes([byte])


@pytest.mark.parametrize(("channels", "message"), [([], "At least"), ([6], "0-5"), ([-1], "0-5")])
def test_an_invalid_channel_list_is_refused(channels: Sequence[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        start_command(channels)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (VERSION_REPLY, "BITalino_v5.2"),
        (b"v5.2\n", "v5.2"),
        (b"\xffBITalino_v4.1\r\n", "BITalino_v4.1"),
    ],
)
def test_the_version_reply_is_sliced_like_the_vendor(reply: bytes, expected: str) -> None:
    assert parse_version(reply) == expected


# =========================================================================
# A scripted native link
# =========================================================================


@final
class FakeLink:
    """A :class:`NativeLink` whose callbacks fire inside ``pump``, as IOBluetooth's do.

    ``open_status`` is reported on the first pump (``None``: never). Each pump
    then delivers at most one queued fragment, so a stream arrives in pieces.
    ``replies`` maps a command to the fragments the "firmware" answers with.
    """

    def __init__(self, events: ChannelEvents, open_status: int | None = 0) -> None:
        self.events: ChannelEvents = events
        self.open_status: int | None = open_status
        self.opened_on: threading.Thread = threading.current_thread()
        self.pumped_on: set[threading.Thread] = set()
        self.intervals: list[float] = []
        self.fragments: deque[bytes] = deque()
        self.replies: dict[bytes, Sequence[bytes]] = {VERSION_COMMAND: [VERSION_REPLY]}
        self.writes: list[bytes] = []
        self.write_error: OSError | None = None
        self.write_gate: threading.Event | None = None
        self.pump_error: OSError | None = None
        self.parked: threading.Event = threading.Event()
        self.pump_gate: threading.Event | None = None
        self.on_pump: Callable[[], None] | None = None
        self.close_error: OSError | None = None
        self.closed: bool = False
        self._announced: bool = False

    def pump(self, seconds: float, /) -> None:
        self.pumped_on.add(threading.current_thread())
        self.intervals.append(seconds)
        if self.pump_gate is not None:
            self.parked.set()
            self.pump_gate.wait(5.0)
        if self.on_pump is not None:
            self.on_pump()
        if self.pump_error is not None:
            raise self.pump_error
        if not self._announced and self.open_status is not None:
            self._announced = True
            self.events.on_open(self.open_status)
        if self.fragments:
            self.events.on_data(self.fragments.popleft())
        time.sleep(min(seconds, 0.001))

    def write(self, data: bytes, /) -> None:
        if self.write_gate is not None:
            self.write_gate.wait(5.0)
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)
        self.fragments.extend(self.replies.get(data, ()))

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _wait_until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("condition not reached")
        time.sleep(0.001)


@final
class Opener:
    """A :data:`NativeOpener` that remembers the link it made."""

    def __init__(self, open_status: int | None = 0, error: Exception | None = None) -> None:
        self.open_status: int | None = open_status
        self.error: Exception | None = error
        self.links: list[FakeLink] = []
        self.addresses: list[str] = []
        self.prepare: Callable[[FakeLink], None] | None = None

    def __call__(self, mac: str, events: ChannelEvents) -> NativeLink:
        self.addresses.append(mac)
        if self.error is not None:
            raise self.error
        link = FakeLink(events, self.open_status)
        if self.prepare is not None:
            self.prepare(link)
        self.links.append(link)
        return link

    @property
    def link(self) -> FakeLink:
        return self.links[-1]


def open_device(opener: Opener, **overrides: float) -> RfcommDevice:
    return RfcommDevice(
        ADDRESS,
        overrides.get("link_timeout", LINK_TIMEOUT),
        opener=opener,
        open_timeout=overrides.get("open_timeout", 2.0),
        pump_slice=0.001,
        command_gap=overrides.get("command_gap", 0.001),
    )


@pytest.fixture
def opener() -> Opener:
    return Opener()


@pytest.fixture
def device(opener: Opener) -> Iterator[RfcommDevice]:
    opened = open_device(opener)
    yield opened
    opened.close()


# =========================================================================
# Opening and closing
# =========================================================================


def test_the_channel_is_opened_on_the_run_loop_thread_and_pumped_there(
    device: RfcommDevice, opener: Opener
) -> None:
    link = opener.link
    assert opener.addresses == [MAC]
    assert device.address == MAC
    assert device.state is LinkState.OPEN
    assert device.is_running
    assert link.opened_on is not threading.current_thread()
    assert link.pumped_on == {link.opened_on}


def test_close_stops_the_run_loop_thread_and_closes_the_channel(opener: Opener) -> None:
    device = open_device(opener)
    device.close()
    assert not device.is_running
    assert opener.link.closed
    assert device.state is LinkState.CLOSED
    device.close()  # idempotent
    assert not device.is_running


def test_a_failing_native_close_still_stops_the_thread(
    opener: Opener, caplog: pytest.LogCaptureFixture
) -> None:
    opener.prepare = lambda link: setattr(link, "close_error", OSError("already gone"))
    device = open_device(opener)
    with caplog.at_level(logging.DEBUG, logger=rfcomm.__name__):
        device.close()
    assert not device.is_running
    assert "already gone" in caplog.text


def test_a_non_rfcomm_address_is_refused_before_any_thread_starts(opener: Opener) -> None:
    with pytest.raises(ValueError, match="not an RFCOMM address"):
        RfcommDevice("/dev/rfcomm0", LINK_TIMEOUT, opener=opener)
    assert opener.addresses == []


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (OSError("IOBluetooth does not know it"), "does not know it"),
        (RuntimeError(), "RuntimeError"),
    ],
)
def test_an_opener_failure_is_an_os_error(error: Exception, message: str) -> None:
    with pytest.raises(OSError, match=message):
        open_device(Opener(error=error))


def test_a_refused_channel_is_an_os_error_and_leaves_nothing_running() -> None:
    opener = Opener(open_status=0xE00002C5)
    with pytest.raises(OSError, match="0xe00002c5"):
        open_device(opener)
    assert opener.link.closed


def test_an_open_that_never_completes_times_out() -> None:
    opener = Opener(open_status=None)
    with pytest.raises(OSError, match="timed out"):
        open_device(opener, open_timeout=0.05)
    assert opener.link.closed


def test_the_default_open_timeout_is_not_the_link_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rfcomm, "OPEN_TIMEOUT_FLOOR_S", 0.05)
    opener = Opener(open_status=None)
    started = time.monotonic()
    with pytest.raises(OSError, match=r"timed out after 0\.1 s"):
        RfcommDevice(ADDRESS, 0.1, opener=opener, pump_slice=0.001)
    assert time.monotonic() - started >= 0.1


def test_a_late_open_callback_changes_nothing(device: RfcommDevice) -> None:
    device.on_open(0xE00002C5)
    assert device.state is LinkState.OPEN


def test_close_from_the_run_loop_thread_does_not_join_itself(opener: Opener) -> None:
    device = open_device(opener)
    opener.link.on_pump = device.close
    _wait_until(lambda: not device.is_running)
    assert opener.link.closed


def test_a_run_loop_that_will_not_stop_is_reported(
    opener: Opener, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(rfcomm, "JOIN_TIMEOUT_S", 0.01)
    device = open_device(opener)
    gate = threading.Event()
    opener.link.pump_gate = gate
    assert opener.link.parked.wait(2.0)
    with caplog.at_level(logging.WARNING, logger=rfcomm.__name__):
        device.close()
    assert "did not stop in time" in caplog.text
    gate.set()
    _wait_until(lambda: not device.is_running)


# =========================================================================
# Commands
# =========================================================================


def test_version_sends_0x07_and_reads_the_reply_in_fragments(
    device: RfcommDevice, opener: Opener
) -> None:
    opener.link.replies[VERSION_COMMAND] = [b"\x00\x13BITal", b"ino_v5", b".2\n", b"\x01"]
    assert device.version() == "BITalino_v5.2"
    assert opener.link.writes == [VERSION_COMMAND]
    # What followed the newline is stream data, left for read_chunk.
    assert device.read_chunk(10) == b"\x01"


def test_a_missing_version_reply_is_a_lost_link(device: RfcommDevice, opener: Opener) -> None:
    opener.link.replies[VERSION_COMMAND] = [b"BITalino_v5.2"]  # no newline, ever
    with pytest.raises(LinkLostError, match="no version reply"):
        device.version()


def test_start_sets_the_rate_then_starts_the_channels(device: RfcommDevice, opener: Opener) -> None:
    device.start(1000, (0,))
    assert opener.link.writes == [b"\xc3", b"\x05"]


def test_start_discards_bytes_that_arrived_before_it(device: RfcommDevice) -> None:
    device.on_data(b"stale")
    device.start(1000, (0,))
    with pytest.raises(LinkLostError, match="no data"):
        device.read_chunk(10)


def test_an_invalid_start_writes_nothing(device: RfcommDevice, opener: Opener) -> None:
    with pytest.raises(ValueError, match="Sample rate"):
        device.start(250, (0,))
    with pytest.raises(ValueError, match="0-5"):
        device.start(1000, (7,))
    assert opener.link.writes == []


def test_stop_sends_0x00(device: RfcommDevice, opener: Opener) -> None:
    device.stop()
    assert opener.link.writes == [STOP_COMMAND]


def test_every_command_is_followed_by_the_command_gap(opener: Opener) -> None:
    """The run loop is pumped for the gap after each write, as the vendor sleeps 0.1 s."""
    device = open_device(opener, command_gap=0.037)
    try:
        device.start(1000, (0,))
        assert opener.link.intervals.count(0.037) == 2
    finally:
        device.close()


@pytest.mark.parametrize(
    ("error", "message"), [(OSError("busy"), "busy"), (OSError(), "write failed")]
)
def test_a_failed_write_is_a_lost_link(
    device: RfcommDevice, opener: Opener, error: OSError, message: str
) -> None:
    opener.link.write_error = error
    with pytest.raises(LinkLostError, match=message):
        device.stop()


def test_a_write_that_hangs_is_a_lost_link(device: RfcommDevice, opener: Opener) -> None:
    gate = threading.Event()
    opener.link.write_gate = gate
    try:
        with pytest.raises(LinkLostError, match="timed out"):
            device.stop()
    finally:
        gate.set()


# =========================================================================
# Reading
# =========================================================================


def test_fragmented_frames_reassemble_through_the_frame_decoder(
    device: RfcommDevice, opener: Opener
) -> None:
    values = list(range(100, 160))
    data = stream(values)
    cuts = [0, 1, 4, 5, 11, 29, 30, 77, len(data)]
    opener.link.fragments.extend(data[a:b] for a, b in itertools.pairwise(cuts))
    decoder = FrameDecoder(1)
    rows: list[tuple[int, ...]] = []
    received = 0
    while received < len(data):
        chunk = device.read_chunk(7)
        assert 0 < len(chunk) <= 7
        received += len(chunk)
        rows.extend(decoder.feed(chunk))
    assert [row[0] for row in rows] == values
    assert decoder.sync_losses == 0
    assert decoder.skipped_bytes == 0


def test_silence_raises_link_lost_after_the_link_timeout(device: RfcommDevice) -> None:
    started = time.monotonic()
    with pytest.raises(LinkLostError, match="no data"):
        device.read_chunk(100)
    assert time.monotonic() - started >= LINK_TIMEOUT


def test_a_channel_closed_by_the_device_fails_reads_and_writes_at_once(
    opener: Opener,
) -> None:
    device = open_device(opener, link_timeout=5.0)
    try:
        device.on_closed()
        started = time.monotonic()
        with pytest.raises(LinkLostError, match="closed the RFCOMM channel"):
            device.read_chunk(10)
        with pytest.raises(LinkLostError, match="closed the RFCOMM channel"):
            device.stop()
        assert time.monotonic() - started < 1.0
    finally:
        device.close()


def test_bytes_already_received_are_still_read_after_the_channel_closes(
    device: RfcommDevice,
) -> None:
    device.on_data(b"last")
    device.on_closed()
    assert device.read_chunk(10) == b"last"
    with pytest.raises(LinkLostError, match="closed"):
        device.read_chunk(10)


def test_a_broken_run_loop_is_a_lost_link(opener: Opener) -> None:
    device = open_device(opener, link_timeout=5.0)
    opener.link.pump_error = OSError("CFRunLoop gone")
    _wait_until(lambda: not device.is_running)
    assert device.state is LinkState.CLOSED
    with pytest.raises(LinkLostError, match="run loop failed"):
        device.read_chunk(10)
    assert opener.link.closed


def test_a_write_queued_when_the_run_loop_dies_is_failed_not_left_waiting(
    opener: Opener,
) -> None:
    device = open_device(opener, link_timeout=5.0)
    link = opener.link
    gate = threading.Event()
    link.pump_gate = gate
    assert link.parked.wait(2.0)
    link.pump_error = OSError("run loop broke")
    outcome: list[BaseException] = []

    def stop() -> None:
        try:
            device.stop()
        except LinkLostError as error:
            outcome.append(error)

    stopper = threading.Thread(target=stop)
    stopper.start()
    _wait_until(lambda: device.pending_writes > 0)
    gate.set()
    stopper.join(2.0)
    assert len(outcome) == 1
    assert "closed" in str(outcome[0])
    assert link.writes == []
    _wait_until(lambda: not device.is_running)


def test_unread_bytes_are_capped(device: RfcommDevice) -> None:
    device.on_data(bytes(MAX_BUFFERED_BYTES))
    device.on_data(b"\x01\x02\x03")
    assert device.dropped_bytes == 3
    chunk = device.read_chunk(MAX_BUFFERED_BYTES)
    assert len(chunk) == MAX_BUFFERED_BYTES
    assert chunk[-3:] == b"\x01\x02\x03"


# =========================================================================
# The pyobjc glue, against fake Foundation / IOBluetooth modules
# =========================================================================


class FakeNSObject:
    @classmethod
    def alloc(cls) -> FakeNSObject:
        return cls()

    def init(self) -> FakeNSObject:
        return self


@final
class FakeVarList:
    """pyobjc's ``objc.varlist``: a pointer with no length of its own."""

    def __init__(self, data: bytes) -> None:
        self._data: bytes = data

    def as_buffer(self, count: int, /) -> memoryview:
        return memoryview(self._data)[:count]


@final
class Air:
    """The radio: callbacks queued here are delivered by the next run-loop pump."""

    def __init__(self) -> None:
        self.lock: threading.Lock = threading.Lock()
        self.pending: deque[Callable[[], None]] = deque()
        self.pump_threads: set[threading.Thread] = set()
        self.intervals: list[float] = []

    def post(self, callback: Callable[[], None]) -> None:
        with self.lock:
            self.pending.append(callback)

    def pump(self, seconds: float) -> None:
        self.pump_threads.add(threading.current_thread())
        self.intervals.append(seconds)
        with self.lock:
            callback = self.pending.popleft() if self.pending else None
        if callback is not None:
            callback()
        time.sleep(0.0005)


@final
class FakeNSDate:
    def __init__(self, seconds: float) -> None:
        self.seconds: float = seconds

    @classmethod
    def dateWithTimeIntervalSinceNow_(cls, seconds: float, /) -> FakeNSDate:  # noqa: N802
        return cls(seconds)


@runtime_checkable
class RfcommDelegate(Protocol):
    """The delegate methods IOBluetooth calls, as the runtime-built class has them."""

    def rfcommChannelOpenComplete_status_(self, channel: object, status: int, /) -> None: ...  # noqa: N802
    def rfcommChannelData_data_length_(  # noqa: N802
        self, channel: object, data: object, length: int, /
    ) -> None: ...
    def rfcommChannelClosed_(self, channel: object, /) -> None: ...  # noqa: N802


@final
class FakeChannel:
    def __init__(self, air: Air, write_status: int = 0) -> None:
        self.air: Air = air
        self.delegate: RfcommDelegate | None = None
        self.write_status: int = write_status
        self.writes: list[bytes] = []
        self.stream: Sequence[object] = ()
        self.closed: bool = False

    def writeSync_length_(self, data: bytes, length: int, /) -> int:  # noqa: N802
        assert length == len(data)
        if self.write_status == 0:
            self.writes.append(data)
            if data == VERSION_COMMAND:
                self.deliver(VERSION_REPLY)
            if data == start_command([0]):
                for piece in self.stream:
                    self.deliver(piece)
        return self.write_status

    def deliver(self, payload: object) -> None:
        length = len(payload) if isinstance(payload, bytes) else 4096
        self.air.post(
            lambda: self._delegate().rfcommChannelData_data_length_(self, payload, length)
        )

    def hang_up(self) -> None:
        self.air.post(lambda: self._delegate().rfcommChannelClosed_(self))

    def _delegate(self) -> RfcommDelegate:
        assert self.delegate is not None
        return self.delegate

    def closeChannel(self) -> int:  # noqa: N802
        self.closed = True
        return 0


@final
class FakeDevice:
    def __init__(self, air: Air, channel: FakeChannel) -> None:
        self.air: Air = air
        self.channel: FakeChannel = channel
        self.connection_status: int = 0
        self.channel_status: int = 0
        self.completion_status: int = 0
        self.give_no_channel: bool = False
        self.connected: bool = False
        self.opened_with: tuple[object, int] | None = None

    def openConnection(self) -> int:  # noqa: N802
        self.connected = self.connection_status == 0
        return self.connection_status

    def closeConnection(self) -> int:  # noqa: N802
        self.connected = False
        return 0

    def openRFCOMMChannelAsync_withChannelID_delegate_(  # noqa: N802
        self, out: None, channel_id: int, delegate: object, /
    ) -> tuple[int, FakeChannel | None]:
        self.opened_with = (out, channel_id)
        if self.channel_status != 0 or self.give_no_channel:
            return (self.channel_status, None)
        assert isinstance(delegate, RfcommDelegate)
        self.channel.delegate = delegate
        status = self.completion_status
        self.air.post(lambda: delegate.rfcommChannelOpenComplete_status_(self.channel, status))
        return (0, self.channel)


@final
class Bluetooth:
    """Fake ``Foundation`` + ``IOBluetooth`` modules, and the one device they know."""

    def __init__(self) -> None:
        self.air: Air = Air()
        self.channel: FakeChannel = FakeChannel(self.air)
        self.device: FakeDevice = FakeDevice(self.air, self.channel)
        self.known: dict[str, FakeDevice] = {MAC: self.device}
        self.looked_up: list[str] = []
        air = self.air
        bluetooth = self

        class FakeRunLoop:
            @classmethod
            def currentRunLoop(cls) -> FakeRunLoop:  # noqa: N802
                return cls()

            def runUntilDate_(self, limit: FakeNSDate, /) -> None:  # noqa: N802
                air.pump(limit.seconds)

        class FakeIOBluetoothDevice:
            @classmethod
            def deviceWithAddressString_(cls, address: str, /) -> FakeDevice | None:  # noqa: N802
                bluetooth.looked_up.append(address)
                return bluetooth.known.get(address)

        foundation = types.ModuleType("Foundation")
        foundation.NSObject = FakeNSObject  # type: ignore[attr-defined]  # building a fake module
        foundation.NSDate = FakeNSDate  # type: ignore[attr-defined]  # building a fake module
        foundation.NSRunLoop = FakeRunLoop  # type: ignore[attr-defined]  # building a fake module
        iobluetooth = types.ModuleType("IOBluetooth")
        iobluetooth.IOBluetoothDevice = FakeIOBluetoothDevice  # type: ignore[attr-defined]  # building a fake module
        self.modules: dict[str, types.ModuleType] = {
            "Foundation": foundation,
            "IOBluetooth": iobluetooth,
        }


@pytest.fixture
def bluetooth(monkeypatch: pytest.MonkeyPatch) -> Bluetooth:
    fake = Bluetooth()
    for name, module in fake.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return fake


def test_the_factory_opens_rfcomm_channel_1_and_pumps_the_run_loop(bluetooth: Bluetooth) -> None:
    device = rfcomm_factory(ADDRESS, LINK_TIMEOUT)
    try:
        assert bluetooth.looked_up == [MAC]
        assert bluetooth.device.opened_with == (None, BITALINO_RFCOMM_CHANNEL)
        assert bluetooth.device.connected
        assert device.version() == "BITalino_v5.2"
        assert bluetooth.channel.writes == [VERSION_COMMAND]
        assert rfcomm.PUMP_SLICE_S in bluetooth.air.intervals
        assert threading.current_thread() not in bluetooth.air.pump_threads
    finally:
        device.close()
    assert bluetooth.channel.closed
    assert not bluetooth.device.connected


def test_the_whole_chain_under_the_client(bluetooth: Bluetooth) -> None:
    """BITalinoClient + rfcomm_factory: frames in bytes and varlist pieces arrive as samples."""
    values = list(range(200, 260))
    data = stream(values)
    bluetooth.channel.stream = [data[:7], FakeVarList(data[7:40]), data[40:41], data[41:]]

    async def scenario() -> list[float]:
        client = BITalinoClient(
            ADDRESS, channels=[0], sample_rate=1000, auto_pair=False, device_factory=rfcomm_factory
        )
        assert await client.connect(timeout=5.0)
        assert await client.start_acquisition()
        try:
            for _ in range(200):
                batch = await client.read_samples(len(values))
                if batch is not None:
                    return list(batch.channels[0].values)
                await asyncio.sleep(0.005)
            raise AssertionError("no batch")
        finally:
            await client.disconnect()

    received = asyncio.run(scenario())
    assert received == [float(v) for v in values]
    assert bluetooth.channel.writes == [VERSION_COMMAND, b"\xc3", b"\x05", STOP_COMMAND]
    assert bluetooth.channel.closed


def test_a_device_closing_the_channel_is_seen(bluetooth: Bluetooth) -> None:
    device = rfcomm_factory(ADDRESS, 5.0)
    try:
        bluetooth.channel.hang_up()
        with pytest.raises(LinkLostError, match="closed the RFCOMM channel"):
            device.read_chunk(10)
    finally:
        device.close()


def test_an_unreadable_payload_is_dropped_with_one_warning(
    bluetooth: Bluetooth, caplog: pytest.LogCaptureFixture
) -> None:
    device = rfcomm_factory(ADDRESS, LINK_TIMEOUT)
    try:
        with caplog.at_level(logging.WARNING, logger=rfcomm.__name__):
            bluetooth.channel.deliver(12345)
            bluetooth.channel.deliver(67890)
            bluetooth.channel.deliver(b"ok")
            assert device.read_chunk(10) == b"ok"
        assert caplog.text.count("unreadable int") == 1
    finally:
        device.close()


def test_a_failed_write_status_is_a_lost_link(bluetooth: Bluetooth) -> None:
    device = rfcomm_factory(ADDRESS, LINK_TIMEOUT)
    try:
        bluetooth.channel.write_status = 0xE00002D6
        with pytest.raises(LinkLostError, match="0xe00002d6"):
            device.stop()
    finally:
        device.close()


def test_an_unknown_device_is_an_os_error(bluetooth: Bluetooth) -> None:
    bluetooth.known.clear()
    with pytest.raises(OSError, match="does not know"):
        rfcomm_factory(ADDRESS, LINK_TIMEOUT)


def test_a_failed_baseband_connection_is_an_os_error(bluetooth: Bluetooth) -> None:
    bluetooth.device.connection_status = 0xE00002BC
    with pytest.raises(OSError, match="baseband connection"):
        rfcomm_factory(ADDRESS, LINK_TIMEOUT)


@pytest.mark.parametrize("no_channel", [True, False])
def test_a_refused_rfcomm_open_closes_the_baseband_connection(
    bluetooth: Bluetooth, *, no_channel: bool
) -> None:
    if no_channel:
        bluetooth.device.give_no_channel = True
    else:
        bluetooth.device.channel_status = 0xE00002C5
    with pytest.raises(OSError, match="RFCOMM channel open"):
        rfcomm_factory(ADDRESS, LINK_TIMEOUT)
    assert not bluetooth.device.connected


def test_a_failed_open_completion_is_an_os_error(bluetooth: Bluetooth) -> None:
    bluetooth.device.completion_status = 0xE00002C0
    with pytest.raises(OSError, match="0xe00002c0"):
        rfcomm_factory(ADDRESS, LINK_TIMEOUT)
    assert bluetooth.channel.closed


def test_without_pyobjc_the_factory_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the Pi sees if it is ever pointed at an rfcomm: address."""
    monkeypatch.setitem(sys.modules, "IOBluetooth", None)
    monkeypatch.setitem(sys.modules, "Foundation", None)
    with pytest.raises(OSError, match="Foundation"):
        rfcomm_factory(ADDRESS, LINK_TIMEOUT)


# =========================================================================
# The real pyobjc (macOS only; nothing is opened)
# =========================================================================

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="pyobjc is macOS only")


@darwin_only
def test_the_stubs_name_members_the_binding_really_has() -> None:
    """Loads the frameworks (as any import does) and opens nothing."""
    from Foundation import NSDate, NSRunLoop  # noqa: PLC0415  # macOS only
    from IOBluetooth import IOBluetoothDevice, IOBluetoothRFCOMMChannel  # noqa: PLC0415

    stubbed: dict[type[object], Sequence[str]] = {
        IOBluetoothDevice: (
            "deviceWithAddressString_",
            "openConnection",
            "closeConnection",
            "openRFCOMMChannelAsync_withChannelID_delegate_",
        ),
        IOBluetoothRFCOMMChannel: ("writeSync_length_", "closeChannel"),
        NSDate: ("dateWithTimeIntervalSinceNow_",),
        NSRunLoop: ("currentRunLoop", "runUntilDate_"),
    }
    for cls, members in stubbed.items():
        for member in members:
            assert hasattr(cls, member), f"{cls.__name__}.{member}"


@final
class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def on_open(self, status: int, /) -> None:
        self.events.append(("open", status))

    def on_data(self, data: bytes, /) -> None:
        self.events.append(("data", data))

    def on_closed(self) -> None:
        self.events.append(("closed", None))


class ObjCDelegate(RfcommDelegate, Protocol):
    def respondsToSelector_(self, selector: bytes, /) -> bool: ...  # noqa: N802


@darwin_only
def test_the_runtime_built_delegate_receives_the_real_selectors() -> None:
    """Through the Objective-C bridge and back, with the framework's own metadata."""
    from Foundation import NSObject  # noqa: PLC0415  # macOS only
    from IOBluetooth import IOBluetoothDevice  # noqa: PLC0415  # registers the signatures

    assert IOBluetoothDevice is not None
    recorder = Recorder()
    built = rfcomm.build_delegate(NSObject, recorder)
    # Not isinstance(): runtime_checkable uses getattr_static, which cannot see
    # selectors pyobjc resolves on demand. respondsToSelector_ is the real test.
    delegate = cast("ObjCDelegate", built)
    for selector in (
        b"rfcommChannelOpenComplete:status:",
        b"rfcommChannelData:data:length:",
        b"rfcommChannelClosed:",
    ):
        assert delegate.respondsToSelector_(selector)
    delegate.rfcommChannelOpenComplete_status_(None, 0)
    delegate.rfcommChannelData_data_length_(None, b"ab\x00\xff", 4)
    delegate.rfcommChannelClosed_(None)
    assert recorder.events == [("open", 0), ("data", b"ab\x00\xff"), ("closed", None)]
    # A second channel gets its own class: the runtime refuses a name twice.
    assert type(rfcomm.build_delegate(NSObject, recorder)) is not type(built)


# =========================================================================
# Contract rules 4 and 5
# =========================================================================

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PYOBJC_MODULE: Final[Path] = PROJECT_ROOT / "src" / "bitalino_rfcomm_macos.py"
PYOBJC_ROOTS: Final[frozenset[str]] = frozenset(
    {"objc", "Foundation", "IOBluetooth", "CoreFoundation", "AppKit", "PyObjCTools"}
)


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def test_pyobjc_has_exactly_one_importer() -> None:
    offenders = {
        str(path.relative_to(PROJECT_ROOT)): found
        for path in sorted((PROJECT_ROOT / "src").rglob("*.py"))
        if path != PYOBJC_MODULE
        and (found := PYOBJC_ROOTS & _imported_roots(path.read_text(encoding="utf-8")))
    }
    assert not offenders, f"pyobjc imported outside {PYOBJC_MODULE.name}: {offenders}"
    assert {"Foundation", "IOBluetooth"} <= _imported_roots(
        PYOBJC_MODULE.read_text(encoding="utf-8")
    )


def test_the_pyobjc_module_does_not_read_the_clock_directly() -> None:
    source = PYOBJC_MODULE.read_text(encoding="utf-8")
    assert "time.monotonic()" not in source
    assert "time.time()" not in source
    assert "import time" not in source
