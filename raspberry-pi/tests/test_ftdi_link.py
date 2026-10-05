"""The buffered FTDI port, against fake chips. No USB, no libusb, no cable.

Two layers of evidence:

* **The port alone**, against :class:`FakeChip` on a :class:`ManualClock`:
  ``in_waiting`` drains what the chip already holds and never waits, ``read``
  serves the buffer first and waits only for what is missing, a partial frame
  is completed across several USB reads, and the timeout is honoured to the
  poll - all in zero wall time.
* **The port under the real pymodbus 3.7.4 transaction manager**, against
  :class:`FakeAltivar`, which answers RTU requests with correct CRCs and hands
  the reply up in small pieces the way the FT232R does. This is what proves the
  bench defect is gone: with pyftdi's own port (``in_waiting`` always 0) every
  transaction waited out its whole serial timeout, measured at 2.0 s per read;
  here the same read has to finish in a fraction of its timeout.

Hard rule for this file: nothing opens a real port, USB device or libusb. The
one function that would (``open_schneider_device``) is exercised with pyftdi's
``open_from_url`` and pyusb's ``get_backend`` monkeypatched out.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Final, final

import pytest
from pyftdi.ftdi import Ftdi
from pyftdi.usbtools import UsbTools
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ModbusPDU

from src.clock import ManualClock, RealClock
from src.motor.atv320 import (
    ATV320Drive,
    FtdiModbusClient,
    ModbusMaster,
    SerialSettings,
    serial_master,
    transaction_worst_case,
)
from src.motor.drive import DriveState, RegisterMap
from src.motor.drive_process_lock import DriveLease
from src.motor.ftdi_link import (
    LATENCY_TIMER_MS,
    LIBUSB_DIRECTORIES,
    POLL_INTERVAL,
    READ_CHUNK,
    SCHNEIDER_CABLE_URL,
    SCHNEIDER_PRODUCT_ID,
    SCHNEIDER_PRODUCT_NAME,
    SCHNEIDER_VENDOR_ID,
    SCHNEIDER_VENDOR_NAME,
    SETTLE_BUDGET,
    SETTLE_CONFIRMATIONS,
    SETTLE_DWELL,
    SETTLE_POLL,
    USB_READ_TIMEOUT_MS,
    USB_STALL_BOUND,
    USB_WRITE_TIMEOUT_MS,
    BufferedFtdiPort,
    ConfigurableFtdi,
    DeviceFactory,
    FtdiDevice,
    Parity,
    UartFrame,
    configure,
    drain_worst_case,
    find_libusb,
    is_ftdi_url,
    load_libusb_backend,
    open_ftdi_port,
    open_schneider_device,
    register_schneider_cable,
    settle,
    wait_overshoot,
    wire_character_time,
)
from src.result import Ok
from src.units import Monotonic, Seconds

FRAME: Final[UartFrame] = UartFrame(baudrate=19200, bytesize=8, parity=Parity.EVEN, stopbits=1)

# =========================================================================
# A fake FT232R: bytes arrive when the test says so
# =========================================================================


@final
class FakeChip:
    """An FTDI chip whose receive side is a script.

    ``arrivals`` is consumed one entry per ``read_data`` call - one USB bulk
    read - so ``[b"ab", b"", b"cd"]`` is "two bytes now, nothing on the next
    poll, the rest on the one after". Past the script, the chip is silent.
    Mutable on purpose: it records what the port asked of it.
    """

    def __init__(self, *arrivals: bytes) -> None:
        self.arrivals: deque[bytes] = deque(arrivals)
        self.read_sizes: list[int] = []
        self.written: list[bytes] = []
        self.close_calls: int = 0
        self.accept: int | None = None
        """Bytes ``write_data`` claims to have taken; ``None`` means all of them."""

    def read_data(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if not self.arrivals:
            return b""
        chunk = self.arrivals.popleft()
        if len(chunk) > size:
            self.arrivals.appendleft(chunk[size:])
            chunk = chunk[:size]
        return chunk

    def write_data(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data) if self.accept is None else self.accept

    def close(self) -> None:
        self.close_calls += 1


@final
class ConfigurableChip:
    """A :class:`FakeChip` that also records how it was set up."""

    def __init__(self, chip: FtdiDevice | None = None) -> None:
        self.chip: FtdiDevice = FakeChip() if chip is None else chip
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.fail_on: str | None = None
        self._timeouts: tuple[int, int] = (5000, 5000)
        """pyftdi's own default: five seconds each way."""

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))
        if self.fail_on == name:
            raise ValueError(f"{name} refused")

    def set_baudrate(self, baudrate: int, constrain: bool = True) -> int:
        self._record("set_baudrate", baudrate, constrain)
        return baudrate

    def set_line_property(
        self, bits: int, stopbit: int | float, parity: str, break_: bool = False
    ) -> None:
        self._record("set_line_property", bits, stopbit, parity, break_)

    def set_flowctrl(self, flowctrl: str) -> None:
        self._record("set_flowctrl", flowctrl)

    def set_latency_timer(self, latency: int) -> None:
        self._record("set_latency_timer", latency)

    def purge_buffers(self) -> None:
        self._record("purge_buffers")

    @property
    def timeouts(self) -> tuple[int, int]:
        return self._timeouts

    @timeouts.setter
    def timeouts(self, timeouts: tuple[int, int]) -> None:
        self._record("timeouts", timeouts)
        self._timeouts = timeouts

    def read_data(self, size: int) -> bytes:
        return self.chip.read_data(size)

    def write_data(self, data: bytes) -> int:
        return self.chip.write_data(data)

    def close(self) -> None:
        self.chip.close()


@final
class Sleeps:
    """A sleep that advances the ManualClock instead of the thread."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock: ManualClock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(Seconds(seconds))


@final
class ChatteringChip:
    """pyftdi 0.57.2's ``read_data`` loop, against a line that keeps talking.

    :class:`FakeChip` answers one scripted chunk per call, instantly, which is
    not what pyftdi does: ``Ftdi.read_data_bytes`` keeps issuing USB reads for
    as long as each carries payload and stops only on an empty one or once
    ``size`` bytes are in hand, keeping any surplus of the last packet in its
    own cache. This fake does the same on a :class:`ManualClock`: a byte
    arrives every character time, each USB read costs one latency period and
    returns what arrived meanwhile. ``stops_after`` ends the chatter after that
    many bytes; ``None`` never does.
    """

    def __init__(self, clock: ManualClock, frame: UartFrame, *, stops_after: int | None) -> None:
        self.clock: ManualClock = clock
        self.character: Seconds = wire_character_time(frame)
        self.latency: Seconds = Seconds(LATENCY_TIMER_MS / 1000)
        self.stops_after: int | None = stops_after
        self.started: Monotonic = clock.monotonic()
        self.delivered: int = 0
        self.cached: int = 0
        self.usb_reads: int = 0

    def _arrived(self) -> int:
        count = int((self.clock.monotonic() - self.started) / self.character)
        return count if self.stops_after is None else min(count, self.stops_after)

    def _usb_read(self) -> int:
        self.usb_reads += 1
        self.clock.advance(self.latency)
        fresh = self._arrived() - self.delivered
        self.delivered += fresh
        return fresh

    def read_data(self, size: int) -> bytes:
        taken = min(size, self.cached)
        self.cached -= taken
        while taken < size:
            fresh = self._usb_read()
            if fresh == 0:
                break
            part = min(fresh, size - taken)
            taken += part
            self.cached += fresh - part
        return b"\x55" * taken

    def write_data(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        return None


def make_port(
    chip: FtdiDevice,
    clock: ManualClock,
    *,
    timeout: Seconds = Seconds(0.1),
    buffer_limit: int = 4096,
) -> tuple[BufferedFtdiPort, Sleeps]:
    sleeps = Sleeps(clock)
    port = BufferedFtdiPort(
        chip, timeout=timeout, clock=clock, sleep=sleeps, buffer_limit=buffer_limit
    )
    return port, sleeps


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


# =========================================================================
# The seam
# =========================================================================


def test_the_fakes_satisfy_the_protocols_the_real_chip_does() -> None:
    assert isinstance(FakeChip(), FtdiDevice)
    assert isinstance(ChatteringChip(ManualClock(), FRAME, stops_after=None), FtdiDevice)
    assert isinstance(ConfigurableChip(), ConfigurableFtdi)


def test_the_real_pyftdi_class_satisfies_the_configurable_protocol() -> None:
    """Checked statically, which is the check that matters: the assignment below
    is a type error the moment pyftdi's Ftdi stops being a ConfigurableFtdi."""
    factory: Callable[[str, DriveLease], ConfigurableFtdi] = open_schneider_device
    assert callable(factory)


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        ("ftdi://schneider:rs485/1", True),
        ("FTDI://schneider:rs485/1", True),
        ("ftdi://0x16de:0x0003/1", True),
        ("/dev/cu.usbserial-A10K", False),
        ("/dev/ttyUSB0", False),
        ("COM3", False),
    ],
)
def test_ftdi_urls_are_told_from_os_devices(port: str, expected: bool) -> None:
    assert is_ftdi_url(port) is expected


def test_the_cable_url_names_the_schneider_ids() -> None:
    assert SCHNEIDER_CABLE_URL == "ftdi://schneider:rs485/1"
    assert (SCHNEIDER_VENDOR_ID, SCHNEIDER_PRODUCT_ID) == (0x16DE, 0x0003)
    assert (SCHNEIDER_VENDOR_NAME, SCHNEIDER_PRODUCT_NAME) == ("schneider", "rs485")


# =========================================================================
# in_waiting
# =========================================================================


def test_in_waiting_reports_what_the_chip_already_holds_without_waiting(
    clock: ManualClock,
) -> None:
    """The member pyftdi's own port hard-codes to 0 - the whole bench defect."""
    port, sleeps = make_port(FakeChip(b"\xf8\x03\x02\x02\x50\x00\x00"), clock)

    assert port.in_waiting == 7
    assert sleeps.calls == [], "in_waiting must never wait"
    assert clock.monotonic() == 0.0


def test_in_waiting_is_zero_on_a_silent_line_and_still_does_not_wait(clock: ManualClock) -> None:
    port, sleeps = make_port(FakeChip(), clock)
    assert port.in_waiting == 0
    assert sleeps.calls == []


def test_in_waiting_grows_as_a_frame_arrives_in_pieces(clock: ManualClock) -> None:
    """How pymodbus decides a reply is complete: in_waiting stops growing."""
    port, _ = make_port(FakeChip(b"\xf8\x03", b"\x02\x02", b"", b"\x50"), clock)
    assert [port.in_waiting for _ in range(5)] == [2, 4, 4, 5, 5]


def test_in_waiting_does_not_consume_what_it_counts(clock: ManualClock) -> None:
    port, _ = make_port(FakeChip(b"abc"), clock)
    assert port.in_waiting == 3
    assert port.read(3) == b"abc"


def test_the_port_asks_the_chip_for_a_whole_chunk(clock: ManualClock) -> None:
    chip = FakeChip(b"x")
    port, _ = make_port(chip, clock)
    assert port.in_waiting == 1
    assert chip.read_sizes == [READ_CHUNK]


def test_a_character_on_the_wire_counts_the_parity_bit() -> None:
    """8E1 is 11 bits a character; 8N1 is 10."""
    assert wire_character_time(FRAME) == pytest.approx(11 / 19200)
    no_parity = UartFrame(baudrate=19200, bytesize=8, parity=Parity.NONE, stopbits=1)
    assert wire_character_time(no_parity) == pytest.approx(10 / 19200)


def test_a_drain_is_bounded_by_the_chunk_s_wire_time_and_two_latency_periods() -> None:
    latency = LATENCY_TIMER_MS / 1000
    assert drain_worst_case(FRAME) == pytest.approx(READ_CHUNK * 11 / 19200 + 2 * latency)
    assert wait_overshoot(FRAME) == pytest.approx(drain_worst_case(FRAME) + POLL_INTERVAL)


def test_the_chunk_keeps_one_drain_to_milliseconds_at_19200() -> None:
    """At 512 bytes one drain of a chattering line held the lock ~293 ms."""
    assert READ_CHUNK * wire_character_time(FRAME) < 0.01
    assert wait_overshoot(FRAME) < 0.02


def test_pyftdi_s_loop_makes_a_big_ask_last_as_long_as_the_chatter() -> None:
    """Why READ_CHUNK is small: the model of pyftdi, asked for 512, reads for ~0.3 s."""
    clock = ManualClock()
    chip = ChatteringChip(clock, FRAME, stops_after=None)

    assert len(chip.read_data(512)) == 512
    assert clock.monotonic() > 0.25
    assert clock.monotonic() > 10 * wait_overshoot(FRAME)


def test_one_drain_of_a_chattering_line_stays_within_its_bound(clock: ManualClock) -> None:
    chip = ChatteringChip(clock, FRAME, stops_after=None)
    port, sleeps = make_port(chip, clock)

    assert port.in_waiting == READ_CHUNK
    assert sleeps.calls == []
    assert 0.0 < clock.monotonic() <= drain_worst_case(FRAME)


def test_a_drain_of_a_line_that_goes_quiet_ends_on_the_empty_read(clock: ManualClock) -> None:
    """A 7-byte reply, then silence: one drain collects it and stops on the empty read."""
    chip = ChatteringChip(clock, FRAME, stops_after=7)
    port, _ = make_port(chip, clock)
    clock.advance(Seconds(0.01))

    assert port.in_waiting == 7
    assert chip.usb_reads == 2
    assert port.in_waiting == 7


def test_a_read_on_a_chattering_line_overruns_its_deadline_by_at_most_the_overshoot(
    clock: ManualClock,
) -> None:
    """The bound transaction_worst_case multiplies by the number of waits."""
    chip = ChatteringChip(clock, FRAME, stops_after=None)
    timeout = Seconds(0.1)
    port, _ = make_port(chip, clock, timeout=timeout)

    got = port.read(4096)

    assert 0 < len(got) < 4096
    assert timeout <= clock.monotonic() <= timeout + wait_overshoot(FRAME)


# =========================================================================
# read
# =========================================================================


def test_read_serves_the_buffer_first_and_keeps_the_rest(clock: ManualClock) -> None:
    port, sleeps = make_port(FakeChip(b"\x01\x02\x03\x04\x05"), clock)
    assert port.in_waiting == 5

    assert port.read(2) == b"\x01\x02"
    assert port.read(3) == b"\x03\x04\x05"
    assert sleeps.calls == []


def test_read_completes_a_partial_frame_across_several_usb_reads(clock: ManualClock) -> None:
    """A reply split over polls is assembled, and read returns as soon as it is whole."""
    port, sleeps = make_port(FakeChip(b"\xf8\x03", b"", b"", b"\x02\x02\x50"), clock)

    assert port.read(5) == b"\xf8\x03\x02\x02\x50"
    assert sleeps.calls == [POLL_INTERVAL] * 3
    assert clock.monotonic() < port.timeout


def test_read_returns_as_soon_as_enough_bytes_are_there(clock: ManualClock) -> None:
    port, sleeps = make_port(FakeChip(b"abcdef"), clock)
    assert port.read(4) == b"abcd"
    assert sleeps.calls == []
    assert port.in_waiting == 2


def test_read_honours_the_timeout_and_returns_what_arrived(clock: ManualClock) -> None:
    """pyserial semantics: short read at the timeout, never an exception."""
    timeout = Seconds(0.01)
    port, sleeps = make_port(FakeChip(b"\xf8\x03"), clock, timeout=timeout)

    assert port.read(7) == b"\xf8\x03"
    assert clock.monotonic() >= timeout
    assert clock.monotonic() <= timeout + POLL_INTERVAL + 1e-9, "it gives up at the deadline"
    assert len(sleeps.calls) == pytest.approx(timeout / POLL_INTERVAL, abs=1)


def test_read_of_a_silent_line_returns_nothing_after_the_timeout(clock: ManualClock) -> None:
    port, _ = make_port(FakeChip(), clock, timeout=Seconds(0.005))
    assert port.read(4) == b""
    assert clock.monotonic() >= 0.005


def test_a_zero_timeout_reads_once_and_never_waits(clock: ManualClock) -> None:
    port, sleeps = make_port(FakeChip(b"ab", b"cd"), clock, timeout=Seconds(0.0))
    assert port.read(4) == b"ab"
    assert sleeps.calls == []


@pytest.mark.parametrize("size", [0, -1])
def test_an_empty_read_touches_nothing(clock: ManualClock, size: int) -> None:
    chip = FakeChip(b"ab")
    port, _ = make_port(chip, clock)
    assert port.read(size) == b""
    assert chip.read_sizes == []


def test_bytes_that_arrive_after_a_short_read_go_to_the_next_read(clock: ManualClock) -> None:
    port, _ = make_port(FakeChip(b"ab", b"", b"", b"cd"), clock, timeout=Seconds(0.002))
    assert port.read(4) == b"ab"
    assert port.read(2) == b"cd"


# =========================================================================
# write, close, limits
# =========================================================================


def test_write_hands_the_frame_to_the_chip(clock: ManualClock) -> None:
    chip = FakeChip()
    port, _ = make_port(chip, clock)
    assert port.write(b"\xf8\x03\x0c\x81\x00\x01") == 6
    assert chip.written == [b"\xf8\x03\x0c\x81\x00\x01"]


def test_write_reports_what_the_chip_accepted(clock: ManualClock) -> None:
    chip = FakeChip()
    chip.accept = 3
    port, _ = make_port(chip, clock)
    assert port.write(b"\x01\x02\x03\x04") == 3


def test_close_releases_the_chip_once_and_is_idempotent(clock: ManualClock) -> None:
    chip = FakeChip(b"stale")
    port, _ = make_port(chip, clock)
    assert port.in_waiting == 5

    port.close()
    port.close()

    assert chip.close_calls == 1
    assert port.is_open is False


@pytest.mark.parametrize("operation", ["read", "write", "in_waiting"])
def test_a_closed_port_raises_an_os_error(clock: ManualClock, operation: str) -> None:
    """OSError, as pyserial's PortNotOpenError is: pymodbus treats it as a transport failure."""
    port, _ = make_port(FakeChip(b"ab"), clock)
    port.close()
    actions: dict[str, Callable[[], object]] = {
        "read": lambda: port.read(1),
        "write": lambda: port.write(b"x"),
        "in_waiting": lambda: port.in_waiting,
    }
    action = actions[operation]
    with pytest.raises(OSError, match="closed"):
        action()


def test_a_runaway_talker_cannot_grow_the_buffer_without_bound(clock: ManualClock) -> None:
    """The oldest bytes go; the newest - the only place a reply could be - stay."""
    port, _ = make_port(FakeChip(b"0123456789"), clock, buffer_limit=4)
    assert port.in_waiting == 4
    assert port.read(4) == b"6789"


def test_the_constructor_refuses_settings_that_cannot_work(clock: ManualClock) -> None:
    with pytest.raises(ValueError, match="negative"):
        BufferedFtdiPort(FakeChip(), timeout=Seconds(-0.1), clock=clock)
    with pytest.raises(ValueError, match="at least one byte"):
        BufferedFtdiPort(FakeChip(), timeout=Seconds(0.1), clock=clock, buffer_limit=0)


def test_pymodbus_can_set_the_inter_byte_timeout_it_always_sets(clock: ManualClock) -> None:
    """ModbusSerialClient.connect assigns it; the port accepts and ignores it."""
    port, _ = make_port(FakeChip(), clock)
    assert port.inter_byte_timeout is None
    port.inter_byte_timeout = 0.00086
    assert port.inter_byte_timeout == 0.00086


# =========================================================================
# Opening and configuring
# =========================================================================


def test_configure_sets_the_line_the_latency_and_clears_the_buffers() -> None:
    chip = ConfigurableChip()
    configure(chip, FRAME)
    assert chip.calls == [
        ("timeouts", ((USB_READ_TIMEOUT_MS, USB_WRITE_TIMEOUT_MS),)),
        ("set_baudrate", (19200, True)),
        ("set_line_property", (8, 1, "E", False)),
        ("set_flowctrl", ("",)),
        ("set_latency_timer", (LATENCY_TIMER_MS,)),
        ("purge_buffers", ()),
    ]


def test_the_usb_timeouts_replace_pyftdi_s_five_seconds() -> None:
    """A half-hung chip must raise within the emergency budget, not after 5 s."""
    chip = ConfigurableChip()
    configure(chip, FRAME)
    assert chip.timeouts == (USB_READ_TIMEOUT_MS, USB_WRITE_TIMEOUT_MS)
    assert USB_READ_TIMEOUT_MS > 10 * LATENCY_TIMER_MS, "a healthy chip must never time out"
    assert max(USB_READ_TIMEOUT_MS, USB_WRITE_TIMEOUT_MS) / 1000 == USB_STALL_BOUND
    assert USB_STALL_BOUND <= 0.1


def test_the_latency_timer_is_short_and_legal() -> None:
    """The FT232R accepts 1..255 ms; the factory 16 ms would dominate a reply."""
    assert 1 <= LATENCY_TIMER_MS <= 2


def test_open_ftdi_port_configures_and_wraps_the_chip(clock: ManualClock) -> None:
    # Two empty bulk reads for settle() to confirm on, then the payload.
    chip = ConfigurableChip(FakeChip(b"", b"", b"ok"))
    opened: list[str] = []

    def create(url: str) -> ConfigurableFtdi:
        opened.append(url)
        return chip

    port = open_ftdi_port(
        SCHNEIDER_CABLE_URL,
        FRAME,
        timeout=Seconds(0.05),
        clock=clock,
        create=create,
        sleep=Sleeps(clock),
    )

    assert opened == [SCHNEIDER_CABLE_URL]
    assert ("set_latency_timer", (LATENCY_TIMER_MS,)) in chip.calls
    assert chip.calls[-1] == ("purge_buffers", ()), "settle() empties the chip last"
    assert port.timeout == Seconds(0.05)
    assert port.read(2) == b"ok"
    port.close()


def test_a_chip_that_refuses_its_settings_is_released(clock: ManualClock) -> None:
    """Otherwise the USB interface stays claimed until the process exits."""
    raw = FakeChip()
    chip = ConfigurableChip(raw)
    chip.fail_on = "set_baudrate"
    with pytest.raises(ValueError, match="refused"):
        open_ftdi_port(
            SCHNEIDER_CABLE_URL, FRAME, timeout=Seconds(0.05), clock=clock, create=lambda _: chip
        )
    assert raw.close_calls == 1


@final
class StallingChip:
    """A chip whose bulk reads time out ``stalls`` times, then answer empty.

    What the FT232R does for ~100-200 ms after being configured on macOS.
    """

    def __init__(self, stalls: int) -> None:
        self.stalls: int = stalls
        self.reads: int = 0
        self.close_calls: int = 0
        self.read_sizes: list[int] = []

    def read_data(self, size: int) -> bytes:
        self.read_sizes.append(size)
        self.reads += 1
        if self.reads <= self.stalls:
            raise OSError(60, "Operation timed out")
        return b""

    def write_data(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.close_calls += 1


def test_settle_dwells_then_confirms_then_purges(clock: ManualClock) -> None:
    chip = ConfigurableChip(FakeChip(b"noise"))
    sleeps = Sleeps(clock)
    settle(chip, clock=clock, sleep=sleeps)
    assert sleeps.calls == [SETTLE_DWELL]
    assert chip.calls == [("purge_buffers", ())]
    assert SETTLE_CONFIRMATIONS == 2
    assert 0.2 <= SETTLE_DWELL < SETTLE_BUDGET <= 1.0


def test_settle_waits_out_a_chip_that_times_out_right_after_configuration(
    clock: ManualClock,
) -> None:
    raw = StallingChip(stalls=3)
    chip = ConfigurableChip(raw)
    sleeps = Sleeps(clock)
    settle(chip, clock=clock, sleep=sleeps)
    assert sleeps.calls == [SETTLE_DWELL, SETTLE_POLL, SETTLE_POLL, SETTLE_POLL]
    assert raw.read_sizes == [READ_CHUNK] * (3 + SETTLE_CONFIRMATIONS)
    assert chip.calls == [("purge_buffers", ())]


def test_a_stall_between_clean_reads_restarts_the_count(clock: ManualClock) -> None:
    raw = FakeChip()
    chip = ConfigurableChip(raw)
    outcomes: deque[bool] = deque([True, False, True, True])
    reads: list[int] = []

    def flaky(size: int) -> bytes:
        reads.append(size)
        if not outcomes.popleft():
            raise OSError(60, "Operation timed out")
        return b""

    chip.read_data = flaky  # type: ignore[method-assign]  # per-call scripted failure
    settle(chip, clock=clock, sleep=Sleeps(clock))
    assert reads == [READ_CHUNK] * 4


def test_a_chip_that_never_answers_fails_settle_and_is_released(clock: ManualClock) -> None:
    raw = StallingChip(stalls=10_000)
    chip = ConfigurableChip(raw)
    with pytest.raises(OSError, match="still not answering"):
        open_ftdi_port(
            SCHNEIDER_CABLE_URL,
            FRAME,
            timeout=Seconds(0.05),
            clock=clock,
            create=lambda _: chip,
            sleep=Sleeps(clock),
        )
    assert raw.close_calls == 1
    assert clock.monotonic() >= SETTLE_BUDGET
    assert chip.calls.count(("purge_buffers", ())) == 1, "only configure() purged"


@pytest.fixture
def no_usb(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """pyftdi's opener and pyusb's loader replaced, so nothing reaches libusb.

    Returns the log of what the real ``open_schneider_device`` asked for.
    """
    log: list[str] = []
    backend = object()

    def get_backend(find_library: Callable[[str], str | None] | None = None) -> object:
        log.append(f"get_backend:{find_library is find_libusb}")
        return backend

    def add_custom_vendor(vid: int, vidname: str = "") -> None:
        log.append(f"vendor:{vid:#06x}:{vidname}")

    def add_custom_product(vid: int, pid: int, pidname: str = "") -> None:
        log.append(f"product:{vid:#06x}:{pid:#06x}:{pidname}")

    def open_from_url(_self: Ftdi, url: str) -> None:
        log.append(f"open:{url}")

    def flush_cache() -> None:
        log.append("flush")

    monkeypatch.setattr("usb.backend.libusb1.get_backend", get_backend)
    monkeypatch.setattr(UsbTools, "flush_cache", flush_cache)
    monkeypatch.setattr(Ftdi, "add_custom_vendor", add_custom_vendor)
    monkeypatch.setattr(Ftdi, "add_custom_product", add_custom_product)
    monkeypatch.setattr(Ftdi, "open_from_url", open_from_url)
    chip = ConfigurableChip()
    monkeypatch.setattr(Ftdi, "set_baudrate", staticmethod(chip.set_baudrate))
    monkeypatch.setattr(Ftdi, "set_line_property", staticmethod(chip.set_line_property))
    monkeypatch.setattr(Ftdi, "set_flowctrl", staticmethod(chip.set_flowctrl))
    monkeypatch.setattr(Ftdi, "set_latency_timer", staticmethod(chip.set_latency_timer))
    monkeypatch.setattr(Ftdi, "purge_buffers", staticmethod(chip.purge_buffers))
    monkeypatch.setattr(Ftdi, "read_data", staticmethod(chip.read_data))
    monkeypatch.setattr(Ftdi, "write_data", staticmethod(chip.write_data))
    return log


def test_the_real_opener_registers_the_cable_and_loads_libusb_first(no_usb: list[str]) -> None:
    lease = DriveLease.claim()
    try:
        device: object = open_schneider_device(SCHNEIDER_CABLE_URL, lease)
        assert isinstance(device, Ftdi)
        device.close()
    finally:
        lease.close()
    assert no_usb == [
        "vendor:0x16de:schneider",
        "product:0x16de:0x0003:rs485",
        "get_backend:True",
        "flush",
        f"open:{SCHNEIDER_CABLE_URL}",
    ]


def test_every_open_re_enumerates_so_a_replugged_cable_is_found_again(
    no_usb: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """pyftdi caches enumeration forever; without a flush a first miss is permanent.

    The cable is absent at the first connect, then plugged in. With pyftdi's
    cache left alone, the empty result of the first enumeration would be served
    to every later open. Here the fake opener only finds the cable once the
    cache has been flushed after it appeared - as pyftdi's would.
    """
    plugged = False

    def open_from_url(_self: Ftdi, url: str) -> None:
        no_usb.append(f"open:{url}")
        if not plugged or no_usb[-2] != "flush":
            raise OSError("UsbError: [Errno 19] No such device (it may have been disconnected)")

    monkeypatch.setattr(Ftdi, "open_from_url", open_from_url)
    master = serial_master(SerialSettings(port=SCHNEIDER_CABLE_URL), RealClock())

    assert master.connect() is False
    plugged = True
    assert master.connect() is True
    master.close()
    assert [entry for entry in no_usb if entry in ("flush", f"open:{SCHNEIDER_CABLE_URL}")] == [
        "flush",
        f"open:{SCHNEIDER_CABLE_URL}",
        "flush",
        f"open:{SCHNEIDER_CABLE_URL}",
    ]


def test_open_ftdi_port_uses_the_real_opener_by_default(
    no_usb: list[str], clock: ManualClock
) -> None:
    port = open_ftdi_port(
        SCHNEIDER_CABLE_URL, FRAME, timeout=Seconds(0.05), clock=clock, sleep=Sleeps(clock)
    )
    assert port.is_open
    assert f"open:{SCHNEIDER_CABLE_URL}" in no_usb
    port.close()


def test_registering_the_cable_twice_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """pyftdi raises ValueError for an id it already knows; a reconnect must not."""
    seen: list[str] = []

    def already(*args: object) -> None:
        seen.append(repr(args))
        raise ValueError("already registered")

    monkeypatch.setattr(Ftdi, "add_custom_vendor", already)
    monkeypatch.setattr(Ftdi, "add_custom_product", already)

    register_schneider_cable()
    assert len(seen) == 2


def test_a_missing_libusb_is_named_rather_than_left_to_pyftdi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_backend(find_library: Callable[[str], str | None] | None = None) -> None:
        assert find_library is find_libusb

    monkeypatch.setattr("usb.backend.libusb1.get_backend", no_backend)
    with pytest.raises(OSError, match="brew install libusb"):
        load_libusb_backend()


# =========================================================================
# Finding libusb on a Mac
# =========================================================================


def test_the_loader_s_own_answer_wins() -> None:
    assert find_libusb("usb-1.0", system_find=lambda _: "/usr/lib/libusb.so") == (
        "/usr/lib/libusb.so"
    )


@pytest.mark.parametrize(
    ("candidate", "file_name"),
    [("libusb-1.0", "libusb-1.0.dylib"), ("usb-1.0", "libusb-1.0.dylib")],
)
def test_homebrew_s_prefix_is_searched_when_the_loader_misses(
    tmp_path: Path, candidate: str, file_name: str
) -> None:
    """What DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib used to be needed for."""
    empty = tmp_path / "empty"
    empty.mkdir()
    brew = tmp_path / "brew"
    brew.mkdir()
    (brew / file_name).write_bytes(b"")

    found = find_libusb(candidate, system_find=lambda _: None, directories=(empty, brew))

    assert found == str(brew / file_name)


def test_nothing_found_is_none(tmp_path: Path) -> None:
    assert find_libusb("usb-1.0", system_find=lambda _: None, directories=(tmp_path,)) is None


def test_the_default_directories_cover_both_homebrews_and_macports() -> None:
    assert Path("/opt/homebrew/lib") in LIBUSB_DIRECTORIES
    assert Path("/usr/local/lib") in LIBUSB_DIRECTORIES
    assert Path("/opt/local/lib") in LIBUSB_DIRECTORIES


# =========================================================================
# Under the real pymodbus transaction manager
# =========================================================================

DRIVE_ADDRESS: Final[int] = 248
ETA_ADDRESS: Final[int] = 3201


def crc16(frame: bytes) -> bytes:
    """Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF), little-endian on the wire."""
    crc = 0xFFFF
    for byte in frame:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


@final
class FakeAltivar:
    """An FT232R with an ATV320 on the far side of the RS-485 pair.

    It answers function 3 and 6 on address 248 with correct CRCs, and hands
    each reply up ``piece`` bytes per USB read - the partial frames the real
    chip delivers when a reply straddles its latency timer. ``silent`` makes
    it answer nothing, as a drive at the wrong address does.
    """

    def __init__(self, registers: dict[int, int], *, piece: int = 3) -> None:
        self.registers: dict[int, int] = registers
        self.piece: int = piece
        self.silent: bool = False
        self.stale: bytes = b""
        self.pending: deque[bytes] = deque()
        self.requests: list[bytes] = []
        self.close_calls: int = 0

    def read_data(self, size: int) -> bytes:
        if self.stale:
            chunk, self.stale = self.stale, b""
            return chunk
        if not self.pending:
            return b""
        return self.pending.popleft()[:size]

    def write_data(self, data: bytes) -> int:
        frame = bytes(data)
        self.requests.append(frame)
        if self.silent:
            return len(frame)
        body, crc = frame[:-2], frame[-2:]
        assert crc == crc16(body), "the master sent a frame with a bad CRC"
        slave, function = body[0], body[1]
        address = int.from_bytes(body[2:4], "big")
        operand = int.from_bytes(body[4:6], "big")
        assert slave == DRIVE_ADDRESS
        if function == 3:
            values = [self.registers[address + i] for i in range(operand)]
            reply = bytes((slave, 3, 2 * operand)) + b"".join(v.to_bytes(2, "big") for v in values)
        else:
            self.registers[address] = operand
            reply = body
        wire = reply + crc16(reply)
        for i in range(0, len(wire), self.piece):
            self.pending.append(wire[i : i + self.piece])
        return len(frame)

    def close(self) -> None:
        self.close_calls += 1


def opener(chip: FakeAltivar, opens: list[str] | None = None) -> DeviceFactory:
    """A device factory that hands out the fake Altivar behind a configurable chip."""

    def create(url: str) -> ConfigurableFtdi:
        if opens is not None:
            opens.append(url)
        return ConfigurableChip(chip)

    return create


def ftdi_master(
    chip: FakeAltivar, *, timeout: Seconds = Seconds(1.0)
) -> tuple[ModbusMaster, SerialSettings]:
    settings = SerialSettings(port=SCHNEIDER_CABLE_URL, timeout=timeout)
    master = serial_master(settings, RealClock(), open_device=opener(chip))
    assert isinstance(master, FtdiModbusClient)
    return master, settings


def test_a_register_read_takes_milliseconds_not_the_serial_timeout() -> None:
    """THE bench defect. With pyftdi's port this read took 2 x the 1.0 s timeout."""
    chip = FakeAltivar({ETA_ADDRESS: 0x0250})
    master, _ = ftdi_master(chip, timeout=Seconds(1.0))
    # Opened first: connect() includes settle()'s dwell, which is not what is timed here.
    assert master.connect()

    started = time.perf_counter()
    reply: object = master.read_holding_registers(ETA_ADDRESS, count=1, slave=DRIVE_ADDRESS)
    taken = time.perf_counter() - started

    assert isinstance(reply, ModbusPDU)
    assert reply.registers == [0x0250]
    assert taken < 0.25, f"took {taken:.3f} s: in_waiting is not being honoured"
    master.close()
    assert chip.close_calls == 1


def test_a_write_is_echoed_through_the_real_framer() -> None:
    chip = FakeAltivar({8602: 700})
    master, _ = ftdi_master(chip)

    reply: object = master.write_register(8602, 0, slave=DRIVE_ADDRESS)

    assert isinstance(reply, ModbusPDU)
    assert reply.function_code == 6
    assert chip.registers[8602] == 0
    master.close()


def test_stale_bytes_on_the_line_are_discarded_before_the_request() -> None:
    """pymodbus reads in_waiting before sending and throws it away - only if it is real."""
    chip = FakeAltivar({ETA_ADDRESS: 0x0021})
    chip.stale = b"\x00\xff\x13"
    master, _ = ftdi_master(chip)

    reply: object = master.read_holding_registers(ETA_ADDRESS, count=1, slave=DRIVE_ADDRESS)

    assert isinstance(reply, ModbusPDU)
    assert reply.registers == [0x0021]
    master.close()


def test_a_silent_drive_costs_a_bounded_time_and_is_a_modbus_io_exception() -> None:
    """No answer: two waits on the head, then pymodbus gives up and closes the port."""
    chip = FakeAltivar({})
    chip.silent = True
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))

    started = time.perf_counter()
    reply: object = master.read_holding_registers(ETA_ADDRESS, count=1, slave=DRIVE_ADDRESS)
    taken = time.perf_counter() - started

    assert isinstance(reply, ModbusIOException)
    assert taken >= 2 * settings.timeout * 0.9
    assert taken < transaction_worst_case(settings) + 0.1
    assert chip.close_calls == 1, "pymodbus closes the port after a failed exchange"


def test_connect_is_idempotent_and_reopens_after_a_close() -> None:
    opens: list[str] = []
    chip = FakeAltivar({ETA_ADDRESS: 0x0250})
    settings = SerialSettings(port=SCHNEIDER_CABLE_URL)
    master = serial_master(settings, RealClock(), open_device=opener(chip, opens))

    assert master.connect() is True
    assert master.connect() is True
    assert len(opens) == 1
    master.close()
    assert master.connect() is True
    assert len(opens) == 2
    master.close()


def test_a_cable_that_will_not_open_is_false_not_an_exception() -> None:
    """pymodbus turns False into ConnectionException, which the driver maps to CommTimeout."""

    def unplugged(_: str) -> ConfigurableFtdi:
        raise OSError("device not found")

    master = serial_master(
        SerialSettings(port=SCHNEIDER_CABLE_URL), RealClock(), open_device=unplugged
    )
    assert master.connect() is False


def test_the_whole_driver_opens_and_reads_status_over_the_ftdi_port() -> None:
    """ATV320Drive on FtdiModbusClient on BufferedFtdiPort on a fake Altivar.

    The bench's register values, the Schneider point-to-point address, and the
    default 0.1 s timeout - and a status read (four transactions) well inside
    one 5 Hz control period.
    """
    regs = RegisterMap()
    chip = FakeAltivar(
        {
            regs.eta: 0x0250,
            regs.lfrd: 0,
            regs.rfrd: 0,
            regs.lcr: 0,
        }
    )
    settings = SerialSettings(port=SCHNEIDER_CABLE_URL)
    drive = ATV320Drive(
        RealClock(),
        serial_master(settings, RealClock(), open_device=opener(chip)),
        settings,
        regs,
    )

    async def session() -> float:
        opened = await drive.open()
        assert isinstance(opened, Ok)
        started = time.perf_counter()
        status = await drive.read_status()
        taken = time.perf_counter() - started
        assert isinstance(status, Ok)
        assert status.value.state is DriveState.SWITCH_ON_DISABLED
        closed = await drive.close()
        assert isinstance(closed, Ok)
        return taken

    taken = asyncio.run(session())

    assert taken < 0.2, f"a status read took {taken:.3f} s"
    assert all(request[0] == DRIVE_ADDRESS for request in chip.requests)
