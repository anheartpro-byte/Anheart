"""ANH-142: reading the BITalino's battery for the pre-flight never costs the ECG its link.

Four ways the read used to go wrong, each found by a review and reproduced here:

* a read during the reader's reconnect backoff left a client that said it was
  connected with no device, so the ECG never came back until a restart;
* a read could run ``state`` on the port while a connection was still being
  opened (``version``, ``start``) on it;
* what the stream had sent before the stop byte was read as the state reply,
  so a false battery could pass the CRC;
* a read with no bound, and on a transport that cannot report the battery at all.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from typing import Final, final

import pytest

from src.bitalino_client import BITalinoClient, LinkLostError, VendorDevice
from src.clock import ManualClock
from src.ecg_pipeline import EcgBridge
from src.local_config import EcgSource
from src.local_panel import EcgClient, EcgLink
from src.units import Percent, Seconds
from tests.test_bitalino import (
    MAC,
    Factory,
    FakeDevice,
    _vendor,  # pyright: ignore[reportPrivateUsage]  # the vendor fake's one builder
    make_client,
)
from tests.test_ecg_pipeline import FakeTreatment
from tests.test_local_panel import FakeEcgClient, NullRing, NullRuntime

ADDRESS: Final[str] = "/dev/rfcomm0"


def _link(client: EcgClient, *, bound: Seconds = Seconds(15.0)) -> EcgLink:
    """The console's ECG side around ``client``, its bridge fed to nothing."""
    clock = ManualClock()
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

    def start(self, sample_rate: int, channels: Sequence[int], /) -> None:
        del sample_rate, channels
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
    await opening
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
