"""ANH-142: reading the BITalino's battery for the pre-flight never costs the ECG its link.

Four ways the read used to go wrong, each found by a review and reproduced here:

* a read during the reader's reconnect backoff left a client that said it was
  connected with no device, so the ECG never came back until a restart;
* a read could run ``state`` on the port while a connection was still being
  opened (``version``, ``start``) on it;
* what the stream had sent before the stop byte was read as the state reply,
  so a false battery could pass the CRC;
* a read with no bound, and on a transport that cannot report the battery at all.

And the reader a cut read leaves behind (its read or its stop hangs) must never
reach into the next acquisition: its device, its data, its "reconnecting".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final, final, override

import pytest

from src.bitalino_client import BITalinoClient, Device, LinkLostError, VendorDevice
from src.clock import ManualClock
from src.ecg_pipeline import EcgBridge
from src.local_config import EcgSource
from src.local_panel import ECG_RETRY, EcgClient, EcgLink
from src.units import Percent, Seconds
from tests.test_bitalino import (
    MAC,
    Factory,
    FakeDevice,
    _vendor,  # pyright: ignore[reportPrivateUsage]  # the vendor fake's one builder
    make_client,
    stream,
)
from tests.test_ecg_pipeline import FakeTreatment
from tests.test_local_panel import FakeEcgClient, NullRing, NullRuntime

ADDRESS: Final[str] = "/dev/rfcomm0"


def _link(
    client: EcgClient, *, bound: Seconds = Seconds(15.0), clock: ManualClock | None = None
) -> EcgLink:
    """The console's ECG side around ``client``, its bridge fed to nothing."""
    clock = ManualClock() if clock is None else clock
    bridge = EcgBridge(
        clock=clock,
        source=client,
        treatment=FakeTreatment((), {}, seq=0),
        heart_rate=NullRuntime(),
        waveform=NullRing(),
        sample_rate=1000,
    )
    return EcgLink(
        clock=clock,
        source=EcgSource.SERIAL,
        address=ADDRESS,
        client=client,
        bridge=bridge,
        link_stats=None,
        battery_bound=bound,
    )


async def _until(condition: Callable[[], bool], within: float = 3.0) -> None:
    """Wait on the loop until ``condition`` holds; fail after ``within`` seconds."""
    async with asyncio.timeout(within):
        while not condition():  # noqa: ASYNC110  # set by the reader thread, not the loop
            await asyncio.sleep(0.005)


def _acquiring(client: BITalinoClient | FakeEcgClient) -> bool:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return client.is_acquiring


def _connected(client: BITalinoClient) -> bool:
    """Read through a call, for the reason :func:`_acquiring` gives."""
    return client.is_connected


def _reconnecting(client: BITalinoClient) -> bool:
    """Read through a call, for the reason :func:`_acquiring` gives."""
    return client.reconnecting


# =========================================================================
# 1. The reconnect backoff
# =========================================================================


async def test_a_stop_in_the_reconnect_backoff_leaves_no_connected_client_without_device() -> None:
    """Reproduction 1: the stop wins the backoff, the device is gone, and the console
    must see it as gone, or it never connects again."""
    lost = FakeDevice([LinkLostError("Bluetooth dropped")])
    client = make_client(Factory(lost, FakeDevice()), reconnect_delays=(3.0,))
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    assert await asyncio.to_thread(lost.closed.wait, 3.0)  # the reader is in its backoff
    await client.stop_acquisition()
    assert not _connected(client)
    # And the console's ECG side brings it back at its next step.
    link = _link(client)
    await link.step()
    assert _connected(client)
    assert _acquiring(client)
    await client.disconnect()


async def test_no_battery_read_starts_while_the_reader_reconnects() -> None:
    """The read waits for a reader acquiring normally: it never stops one that reconnects."""
    lost = FakeDevice([LinkLostError("Bluetooth dropped")])
    back = FakeDevice()
    client = make_client(Factory(lost, back), reconnect_delays=(0.3,))
    link = _link(client)
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    assert await asyncio.to_thread(lost.closed.wait, 3.0)
    assert client.reconnecting
    assert await link.read_battery() is None
    assert _acquiring(client)  # untouched
    await _until(lambda: not client.reconnecting)
    assert back.started_with is not None  # the reader came back on its own
    assert _connected(client)
    await client.disconnect()


# =========================================================================
# 2. One owner of the port
# =========================================================================


@final
class SlowDevice:
    """A device whose opening exchanges take time, and which says what ran when."""

    battery_readable: bool = True

    def __init__(self) -> None:
        self.log: list[str] = []
        self._lock = threading.Lock()

    def _say(self, what: str) -> None:
        with self._lock:
            self.log.append(what)

    def version(self) -> str:
        self._say("version>")
        time.sleep(0.2)
        self._say("version<")
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        self._say("start>")
        time.sleep(0.1)
        self._say("start<")

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        time.sleep(0.01)
        return b""

    def stop(self) -> None:
        self._say("stop")

    def close(self) -> None:
        self._say("close")

    def battery_raw(self) -> int:
        self._say("state>")
        time.sleep(0.05)
        self._say("state<")
        return 589


async def test_the_battery_is_never_read_while_the_connection_is_being_opened() -> None:
    """Reproduction 2: the pre-flight's step saw "connected" between the open and the
    start, and ran ``state`` on the port while ``version`` was still on it."""
    device = SlowDevice()
    client = make_client(Factory(device), address=ADDRESS)
    link = _link(client)
    opening = asyncio.create_task(link.step())
    await _until(lambda: _connected(client))
    battery = await link.read_battery()
    await asyncio.wait_for(opening, 3.0)
    assert battery == pytest.approx(50.0, abs=0.5)
    log = device.log
    assert log.index("state>") > log.index("start<"), log
    await client.disconnect()


# =========================================================================
# 3. What the stream left in the port
# =========================================================================


def _state_reply(battery: int) -> bytes:
    """A 17-byte ``state`` answer of a BITalino 2, firmware 5.2, with its CRC."""
    data = bytearray(17)
    data[12] = battery & 0xFF
    data[13] = battery >> 8
    crc = 0
    for byte in data:
        for bit in range(7, -1, -1):
            crc <<= 1
            if crc & 0x10:
                crc ^= 0x03
            crc ^= (byte >> bit) & 0x01
    data[16] |= crc & 0x0F
    return bytes(data)


@final
class SerialBitalino:
    """A BITalino 2 behind a serial port, as the vendor library talks to it.

    The stream does not end the instant the stop byte is written: what it had
    sent is still in the port's input (``leftover``) when the next command goes
    out. ``endless``: it never stops at all.
    """

    leftover: bytes = b""
    endless: bool = False
    battery: int = 589

    def __init__(self, address: str, baudrate: int) -> None:
        del address, baudrate
        self.timeout: float | None = None
        self._input = bytearray()

    def write(self, data: bytes) -> int:
        command = data[0]
        if command == 7:
            self._input += b"BITalino_v5.2\n"
        elif command == 11:
            self._input += _state_reply(self.battery)
        elif command & 0b11 == 0b01:  # start: the stream
            self._input += self.leftover
        return len(data)

    def inWaiting(self) -> int:  # noqa: N802  # pyserial's name
        return len(self._input)

    def read(self, size: int = 1) -> bytes:
        if self.endless:
            return b"\x00" * size
        chunk = bytes(self._input[:size])
        del self._input[:size]
        return chunk


def _streamed(
    monkeypatch: pytest.MonkeyPatch, leftover: bytes, *, endless: bool = False
) -> VendorDevice:
    """The adapter over the real vendor library, a stream started then stopped."""
    monkeypatch.setattr("serial.Serial", SerialBitalino)
    monkeypatch.setattr(SerialBitalino, "leftover", leftover)
    device = VendorDevice("/dev/fake-bitalino", 3.0)
    device.start(1000, (0,))
    device.stop()
    monkeypatch.setattr(SerialBitalino, "endless", endless)
    return device


@pytest.mark.parametrize(
    "leftover",
    [_state_reply(1000), b"\x55" * 23],
    ids=["frames-that-pass-the-crc", "frames-that-fail-it"],
)
def test_what_the_stream_left_is_drained_before_the_state_is_asked(
    monkeypatch: pytest.MonkeyPatch, leftover: bytes
) -> None:
    """Reproduction 3: read as the reply, the leftover gave a false battery (1 in 16)."""
    device = _streamed(monkeypatch, leftover)
    assert device.battery_readable
    assert device.battery_raw() == 589


def test_a_stream_that_never_stops_is_no_battery(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _streamed(monkeypatch, b"", endless=True)
    with pytest.raises(LinkLostError, match="did not stop"):
        device.battery_raw()


def test_a_bluetooth_socket_cannot_report_the_battery(monkeypatch: pytest.MonkeyPatch) -> None:
    device, _ = _vendor(monkeypatch, MAC)
    assert not device.battery_readable
    with pytest.raises(LinkLostError, match="serial port"):
        device.battery_raw()


# =========================================================================
# 4. A bound, and no pause for a transport that cannot say
# =========================================================================


async def test_a_battery_read_that_does_not_end_is_cut_and_the_link_reopened() -> None:
    client = FakeEcgClient()
    client.battery = Percent(64.0)
    link = _link(client, bound=Seconds(0.05))
    await link.step()
    client.charge_read.clear()  # the device never answers
    assert await asyncio.wait_for(link.read_battery(), 2.0) is None
    assert client.disconnects == 1  # the port is closed: whatever hung on it ends
    await link.step()
    assert _acquiring(client)


async def test_a_transport_that_cannot_report_the_battery_is_never_stopped_for_it() -> None:
    client = FakeEcgClient()
    client.reads_battery = False  # the macOS RFCOMM transport
    link = _link(client)
    await link.step()
    assert await link.read_battery() is None
    assert _acquiring(client)


@final
class HangingStop:
    """Streams nothing, and hangs in ``stop`` until closed: a BITalino gone mid-read, its
    stop byte a write the link never takes."""

    battery_readable: bool = True

    def __init__(self) -> None:
        self.released = threading.Event()

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        """Starts at once."""

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        if self.released.wait(0.02):
            raise LinkLostError("closed")
        return b""

    def stop(self) -> None:
        self.released.wait(10.0)

    def close(self) -> None:
        self.released.set()

    def battery_raw(self) -> int:
        return 589


async def test_a_read_cut_inside_a_stop_that_hangs_leaves_the_real_client_idle() -> None:
    """Reproduction 3: the bound fired inside ``stop_acquisition``, the client stayed
    "acquiring" with no device, and the console pumped it for ever without reconnecting."""
    hanging = HangingStop()
    after = FakeDevice()
    factory = Factory(hanging, after, FakeDevice())
    client = make_client(factory)
    link = _link(client, bound=Seconds(0.3))
    await link.step()
    assert link.streaming
    assert await asyncio.wait_for(link.read_battery(), 5.0) is None
    assert not _acquiring(client)
    assert not _connected(client)
    assert not client.reads_battery  # no device left
    assert hanging.released.is_set()  # closed: the stop that hung on it ends
    await link.step()
    assert link.streaming
    assert after.started_with is not None
    # The reader the cut stop left behind never takes the client back.
    await asyncio.sleep(0.1)
    assert len(factory.calls) == 2
    await client.disconnect()


async def test_the_wait_for_the_port_is_bounded_too() -> None:
    """A connection being opened holds the port: the read gives up, and leaves it alone."""
    device = SlowDevice()
    client = make_client(Factory(device), address=ADDRESS)
    link = _link(client, bound=Seconds(0.05))
    opening = asyncio.create_task(link.step())
    await _until(lambda: _connected(client))
    assert await link.read_battery() is None
    await asyncio.wait_for(opening, 3.0)
    assert link.streaming
    assert "state>" not in device.log
    await client.disconnect()


@final
class StoppedWhileStarting:
    """A reconnection that is still starting when the acquisition is stopped."""

    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.starting = threading.Event()
        self.go_on = threading.Event()
        self.closed = threading.Event()
        self._close_error = close_error

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        self.starting.set()
        self.go_on.wait(3.0)

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        return b""

    def stop(self) -> None:
        return

    def close(self) -> None:
        self.closed.set()
        if self._close_error is not None:
            raise self._close_error


@pytest.mark.parametrize("close_error", [None, OSError("port gone")], ids=["closes", "fails"])
async def test_a_device_opened_for_a_stopped_acquisition_is_closed_not_adopted(
    close_error: Exception | None,
) -> None:
    lost = FakeDevice([LinkLostError("Bluetooth dropped")])
    late = StoppedWhileStarting(close_error=close_error)
    client = make_client(Factory(lost, late), reconnect_delays=(0.0,))
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    assert await asyncio.to_thread(late.starting.wait, 3.0)
    stopping = asyncio.create_task(client.stop_acquisition())
    await asyncio.sleep(0.05)
    late.go_on.set()
    await asyncio.wait_for(stopping, 3.0)
    assert late.closed.is_set()
    assert not _connected(client)
    assert not client.reads_battery


# =========================================================================
# 5. A reader a cut stop left behind never reaches into the next acquisition
# =========================================================================


def _readers() -> int:
    return sum(thread.name == "bitalino-reader" for thread in threading.enumerate())


@final
class LateRead:
    """Streams once, then a read that returns only when released, with other data: a
    tty read that a close does not wake. Its stop hangs until released too."""

    battery_readable: bool = True

    def __init__(self) -> None:
        self.first = True
        self.late = threading.Event()
        self.released = threading.Event()
        self.closed = threading.Event()

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        """Starts at once."""

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        if self.first:
            self.first = False
            return stream([100] * 50)
        self.late.wait(5.0)
        return stream([999] * 200)

    def stop(self) -> None:
        self.released.wait(5.0)

    def close(self) -> None:
        self.closed.set()

    def battery_raw(self) -> int:
        return 589


async def test_a_late_read_of_an_abandoned_reader_never_feeds_the_next_acquisition() -> None:
    late = LateRead()
    back = FakeDevice([stream([500] * 50)])
    client = make_client(Factory(late, back))
    link = _link(client, bound=Seconds(0.3))
    await link.step()
    await _until(lambda: client.link_stats().frames >= 50)
    assert await link.read_battery() is None  # cut inside the stop that hangs
    assert late.closed.is_set()
    await link.step()
    assert link.streaming
    late.late.set()  # the abandoned reader's read returns now, into the next acquisition
    await _until(lambda: _readers() == 1)
    seen: set[float] = set()
    while (batch := await client.read_samples(1)) is not None:
        seen.update(batch.channels[0].values)
    assert seen == {500.0}
    late.released.set()
    await client.disconnect()


@final
class DropsThenHangs:
    """Streams once, then loses the link; its stop hangs until released."""

    battery_readable: bool = True

    def __init__(self) -> None:
        self.reads = 0
        self.released = threading.Event()

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        """Starts at once."""

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        self.reads += 1
        if self.reads == 1:
            return stream([100] * 50)
        raise LinkLostError("Bluetooth dropped")

    def stop(self) -> None:
        self.released.wait(5.0)

    def close(self) -> None:
        return

    def battery_raw(self) -> int:
        return 589


@final
class HoldsTheReaderInItsLog(logging.Handler):
    """Holds the reader in its "link lost" line: past its stop check, before its
    reconnect, the window in which a stop can fall."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.entered = threading.Event()
        self.go_on = threading.Event()

    @override
    def emit(self, record: logging.LogRecord) -> None:
        reader = threading.current_thread().name == "bitalino-reader"
        if reader and "link lost" in record.getMessage():
            self.entered.set()
            self.go_on.wait(5.0)


async def test_an_abandoned_reader_never_drops_the_device_of_the_next_acquisition() -> None:
    dropping = DropsThenHangs()
    back = FakeDevice()
    factory = Factory(dropping, back, FakeDevice())
    client = make_client(factory)
    link = _link(client, bound=Seconds(0.3))
    holder = HoldsTheReaderInItsLog()
    logger = logging.getLogger("src.bitalino_client")
    logger.addHandler(holder)
    try:
        await link.step()
        assert await asyncio.to_thread(holder.entered.wait, 3.0)
        assert link.streaming  # it does not reconnect yet
        assert await link.read_battery() is None  # cut inside the stop that hangs
        await link.step()
        assert link.streaming
        holder.go_on.set()  # the abandoned reader goes on to its reconnect
        await _until(lambda: _readers() == 1)
    finally:
        logger.removeHandler(holder)
        holder.go_on.set()
        dropping.released.set()
    assert _connected(client)
    assert link.streaming
    assert len(factory.calls) == 2  # nothing re-opened for the abandoned reader
    await client.disconnect()
    assert back.closed.is_set()


@final
class SecondOpenHangs:
    """Opens ``first``, then an open that hangs until released (``late``), then ``then``."""

    def __init__(self, first: Device, late: Device, then: Device) -> None:
        self._devices: tuple[Device, Device, Device] = (first, late, then)
        self.calls = 0
        self.opening = threading.Event()
        self.release = threading.Event()

    def __call__(self, address: str, timeout: float) -> Device:
        del address, timeout
        self.calls += 1
        device = self._devices[min(self.calls, 3) - 1]
        if device is self._devices[1]:
            self.opening.set()
            self.release.wait(5.0)
        return device


async def test_an_abandoned_reader_that_reconnects_says_so_of_its_own_acquisition_only() -> None:
    late = FakeDevice()
    back = FakeDevice()
    factory = SecondOpenHangs(FakeDevice([LinkLostError("Bluetooth dropped")]), late, back)
    client = make_client(factory)
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    assert await asyncio.to_thread(factory.opening.wait, 3.0)
    assert _reconnecting(client)
    with contextlib.suppress(TimeoutError):  # its reader never joins: a stop cut short
        await asyncio.wait_for(client.stop_acquisition(), 0.2)
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    assert not _reconnecting(client)  # the abandoned reader still opens, for itself
    factory.release.set()
    assert await asyncio.to_thread(late.closed.wait, 3.0)  # opened for nobody: closed
    await _until(lambda: _readers() == 1)
    assert _connected(client)
    assert _acquiring(client)
    await client.disconnect()
    assert back.closed.is_set()


async def test_a_reconnection_that_opens_but_does_not_start_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    refusing = FakeDevice(start_error=OSError("nak"))
    back = FakeDevice()
    lost = FakeDevice([LinkLostError("Bluetooth dropped")])
    client = make_client(
        Factory(lost, OSError("no port"), refusing, back), reconnect_delays=(0.0, 0.0, 0.0)
    )
    assert await client.connect(1.0)
    assert await client.start_acquisition()
    await _until(lambda: back.started_with is not None)
    assert refusing.closed.is_set()  # left open, every later open would find it busy
    messages = [record.getMessage() for record in caplog.records]
    assert "Reconnect attempt 1 failed to open: no port" in messages
    assert "Reconnect attempt 2 failed to start: nak" in messages
    await _until(lambda: _connected(client))
    await client.disconnect()


async def test_a_close_that_hangs_after_a_cut_read_does_not_hold_the_port() -> None:
    client = FakeEcgClient()
    client.battery = Percent(64.0)
    link = _link(client, bound=Seconds(0.05))
    await link.step()
    client.charge_read.clear()  # the device never answers
    client.closed.clear()  # nor does its close
    assert await asyncio.wait_for(link.read_battery(), 2.0) is None
    assert client.disconnects == 1
    await asyncio.wait_for(link.step(), 2.0)  # the port is free: the link opens again
    assert _acquiring(client)


@final
class GoneDuringTheRead:
    """Streams, stops, then is gone: Bluetooth lost while the acquisition was stopped
    for the read. Its state is never answered and it never starts again."""

    battery_readable: bool = True

    def __init__(self) -> None:
        self.starts = 0
        self.closed = threading.Event()

    def version(self) -> str:
        return "BITalino_v5.2"

    def start(self, _sample_rate: int, _channels: Sequence[int], /) -> None:
        self.starts += 1
        if self.starts > 1:
            raise OSError("Bluetooth gone")

    def read_chunk(self, max_bytes: int, /) -> bytes:
        del max_bytes
        if self.closed.wait(0.01):
            raise LinkLostError("closed")
        return b""

    def stop(self) -> None:
        return

    def close(self) -> None:
        self.closed.set()

    def battery_raw(self) -> int:
        raise OSError("Bluetooth gone")


async def test_a_link_lost_during_the_read_is_opened_afresh_not_started_for_ever() -> None:
    """R1: the stopped client still said it was connected, with a dead device, and the
    console asked it to start every 5 s without ever reconnecting."""
    gone = GoneDuringTheRead()
    back = FakeDevice()
    clock = ManualClock()
    client = make_client(Factory(gone, back))
    link = _link(client, clock=clock)
    await link.step()
    assert link.streaming
    assert await link.read_battery() is None
    assert _connected(client)  # what the client knows: the loss is not seen at rest
    await link.step()  # its start fails: the link is closed
    assert gone.closed.is_set()
    assert not _connected(client)
    clock.advance(ECG_RETRY)
    await link.step()
    assert link.streaming
    assert back.started_with is not None
    await client.disconnect()
