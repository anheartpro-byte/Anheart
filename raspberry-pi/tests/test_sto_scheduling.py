"""The control tick keeps the link: an STO read never delays its command writes.

Measured on the real console (``LocalPanel.run``) on virtual time
(:mod:`tests.virtual_time`): every exchange holds the half-duplex link for a
latency, and each command write (keepalive, setpoint, command word) is
recorded at the instant its frame goes out. The same session runs with
``STO_SOURCE=drive_input``, which reads register 5202 over that link, and with
a simulated ready chain, which reads nothing: the instants must be identical.

Two links: the simulated drive behind a latent wrapper, and the real ATV320
driver over the tests' fake bus, whose transactions return after their latency.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

import pytest

from src.clock import ManualClock
from src.cloud_sync import DASHBOARD_OPERATOR
from src.local_panel import DriveSide, LocalPanel, build_panel
from src.motor.atv320 import ATV320Drive, transaction_worst_case
from src.motor.drive import (
    LOGIC_INPUTS_LOGICAL,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
    RegisterMap,
)
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import (
    STO_POLL_PERIOD,
    STO_STALE_AFTER,
    DriveInputStoSource,
    StoReading,
    StoState,
)
from src.presence.monitor import RULE_PRESENCE_CAMERA_LOST, RULE_PRESENCE_INTRUSION
from src.result import Err, Ok, Result
from src.training import runtime as runtime_module
from src.training.preflight import StoNotReady
from src.training.runtime import RULE_STO_UNREADABLE, RuntimeState
from src.training.types import Occupancy, SafetyAction, SafetyVerdict, TelemetrySnapshot
from src.units import Monotonic, MotorRpm, OutputRpm, RawRegister, RegisterAddress, Seconds
from tests.test_atv320 import SETTINGS, FakeBus, io_exception
from tests.test_failure_rig import BENCH_ENV, OPERATOR, Wrapped, config_of, no_dsp
from tests.test_local_panel import FakeWeb
from tests.virtual_time import run_virtual

LATENCY: Final[Seconds] = Seconds(0.01)
"""Link time of one exchange, below the ATV320's AUXILIARY_READ_BUDGET."""

STO_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "STO_SOURCE": "drive_input",
    "STO_DRIVE_INPUT": "LI1",
}
NO_READ_ENV: Final[Mapping[str, str]] = {**BENCH_ENV, "STO_SOURCE": "sim_ready"}
REGISTERS: Final[RegisterMap] = RegisterMap()
WORST: Final[Seconds] = transaction_worst_case(SETTINGS)


@pytest.fixture(autouse=True)
def inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sensor step's thread runs inline: virtual time knows no real thread."""

    async def inline[**P, T](work: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return work(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)


@dataclass(frozen=True, slots=True)
class Frame:
    """One exchange on the link: what was asked, when, and when it held the link."""

    kind: str
    register: int | None
    value: int | None
    asked: float
    began: float
    ended: float


COMMANDS: Final[frozenset[str]] = frozenset({"write_speed", "write_command", "emergency"})


def now() -> float:
    return asyncio.get_running_loop().time()


# =========================================================================
# The simulated drive behind one latent link
# =========================================================================


class LatentLink(Wrapped):
    """The simulator behind one half-duplex link: each exchange holds it for LATENCY."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        # One link, one exchange at a time, in arrival order, as on RS-485.
        self.link: asyncio.Lock = asyncio.Lock()
        self.frames: list[Frame] = []
        self.silent_feedback: bool = False
        self.latency: Seconds = LATENCY

    async def _held[R](
        self,
        kind: str,
        call: Callable[[], Awaitable[R]],
        *,
        register: int | None = None,
        value: int | None = None,
        latency: Seconds | None = None,
    ) -> R:
        asked = now()
        async with self.link:
            began = now()
            await asyncio.sleep(self.latency if latency is None else latency)
            result = await call()
        self.frames.append(Frame(kind, register, value, asked, began, now()))
        return result

    @override
    async def open(self) -> Result[None, DriveError]:
        return await self._held("open", self.inner.open)

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        return await self._held(
            "write_command", lambda: self.inner.write_command(word), value=word.value
        )

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        return await self._held("write_speed", lambda: self.inner.write_speed(rpm), value=rpm)

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        return await self._held("read_status", self.inner.read_status)

    @override
    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return await self._held("read_limits", self.inner.read_limits)

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        if self.silent_feedback:
            return await self._held(
                "read_register", self._unanswered, register=address, latency=WORST
            )
        return await self._held(
            "read_register", lambda: self.inner.read_register(address), register=address
        )

    async def _unanswered(self) -> Result[RawRegister, DriveError]:
        return Err(CommTimeout(after=WORST))

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        at = now()
        self.frames.append(Frame("emergency", None, 0, at, at, at))
        return self.inner.emergency_disable_blocking(timeout)


# =========================================================================
# The ATV320 driver over the fake bus, each transaction returning late
# =========================================================================


@dataclass(frozen=True, slots=True)
class Sent:
    access: str
    address: int
    value: int | None
    at: float


class TimedBus:
    """The tests' fake Altivar, timestamping each transaction; RFRD follows LFRD."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock: ManualClock = clock
        self.fake: FakeBus = FakeBus(clock, REGISTERS)
        self.fake.registers.update(
            {
                REGISTERS.eta: 0x40,
                REGISTERS.lfrd: 0,
                REGISTERS.rfrd: 0,
                REGISTERS.lcr: 0,
                REGISTERS.lft: 0,
                REGISTERS.cmd: 0,
                REGISTERS.tfr: 500,
                REGISTERS.hsp: 500,
                REGISTERS.lsp: 0,
                REGISTERS.acc: 100,
                REGISTERS.dec: 40,
                REGISTERS.logic_inputs: 1,
            }
        )
        # Appended by the one driver on this bus; read by the test afterwards.
        self.sent: list[Sent] = []
        self.latency_of_next: Seconds = LATENCY
        self.exchange_latency: Seconds = LATENCY
        self.silent_feedback: bool = False

    def connect(self) -> bool:
        return self.fake.connect()

    def close(self) -> None:
        self.fake.close()

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        self.sent.append(Sent("read", address, None, float(self.clock.monotonic())))
        self.latency_of_next = self.exchange_latency
        if address == REGISTERS.logic_inputs and self.silent_feedback:
            self.latency_of_next = WORST
            raise io_exception()
        return self.fake.read_holding_registers(address, count, slave)

    def write_register(self, address: int, value: int, slave: int = 1) -> object:
        self.sent.append(Sent("write", address, value, float(self.clock.monotonic())))
        self.latency_of_next = self.exchange_latency
        reply = self.fake.write_register(address, value, slave)
        if address == REGISTERS.lfrd:
            self.fake.registers[REGISTERS.rfrd] = value
        return reply


class DeferredExecutor(Executor):
    """Runs each transaction on the loop at once, and hands its result back after its latency.

    The driver keeps its lock until then: the link is busy for that long, as it
    is while a real executor thread waits on the wire.
    """

    def __init__(self, bus: TimedBus) -> None:
        # Owned by one driver on one virtual-time loop.
        self._bus: TimedBus = bus
        self._closed: bool = False

    @override
    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        if self._closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        future: Future[T] = Future()
        loop = asyncio.get_running_loop()
        try:
            value = fn(*args, **kwargs)
        except Exception as error:  # handed to the awaiting driver
            loop.call_later(self._bus.latency_of_next, future.set_exception, error)
        else:
            loop.call_later(self._bus.latency_of_next, future.set_result, value)
        return future

    @override
    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._closed = True


def bus_panel(
    tmp_path: Path, clock: ManualClock, env: Mapping[str, str]
) -> tuple[LocalPanel, TimedBus]:
    bus = TimedBus(clock)
    drive = ATV320Drive(
        clock,
        bus,
        SETTINGS,
        REGISTERS,
        settle_delay=Seconds(0.0),
        stop_poll_interval=Seconds(0.0),
        executor_factory=lambda: DeferredExecutor(bus),
        wait=asyncio.sleep,
    )
    panel = build_panel(
        config_of(env),
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=drive, simulator=None, release=lambda: None),
    )
    return panel, bus


def latent_panel(
    tmp_path: Path, clock: ManualClock, env: Mapping[str, str]
) -> tuple[LocalPanel, LatentLink]:
    simulator = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    link = LatentLink(simulator)
    panel = build_panel(
        config_of(env),
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=link, simulator=simulator, release=lambda: None),
    )
    return panel, link


# =========================================================================
# What a session sent over the link
# =========================================================================


@dataclass(frozen=True, slots=True)
class Measured:
    """Command writes at the instant their frame went out, and the STO reads."""

    commands: tuple[tuple[int, int | None, float], ...]
    feedback: tuple[tuple[float, float], ...]
    waited: tuple[float, ...]
    """When a command write that had to wait for the link asked for it."""
    misses: int


def from_link(link: LatentLink, panel: LocalPanel) -> Measured:
    commands = tuple(
        (0 if frame.kind == "write_command" else 1, frame.value, frame.began)
        for frame in link.frames
        if frame.kind in COMMANDS
    )
    feedback = tuple(
        (frame.began, frame.ended)
        for frame in link.frames
        if frame.kind == "read_register" and frame.register == LOGIC_INPUTS_LOGICAL
    )
    waited = tuple(
        frame.asked for frame in link.frames if frame.kind in COMMANDS and frame.began > frame.asked
    )
    return Measured(commands, feedback, waited, panel.sto_window_misses)


def from_bus(bus: TimedBus, panel: LocalPanel) -> Measured:
    commands = tuple(
        (sent.address, sent.value, sent.at) for sent in bus.sent if sent.access == "write"
    )
    feedback = tuple(
        (sent.at, sent.at)
        for sent in bus.sent
        if sent.access == "read" and sent.address == REGISTERS.logic_inputs
    )
    return Measured(commands, feedback, (), panel.sto_window_misses)


Event = Callable[[LocalPanel], None]


def attest_and_start(panel: LocalPanel) -> None:
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )


def cruise(panel: LocalPanel) -> None:
    assert panel.runtime.state is RuntimeState.RUNNING
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(3), operator=OPERATOR), Ok
    )


SESSION: Final[tuple[tuple[float, Event], ...]] = ((1.15, attest_and_start), (1.75, cruise))
"""A bench session at 3 output rpm. Ticks start every 0.2 s from 0: each request
falls 0.15 s after one, once its exchanges and STO read are over, never on a tie."""


async def console(panel: LocalPanel, events: tuple[tuple[float, Event], ...], end: float) -> None:
    """Run the real console; apply each event at its offset from the start, stop at ``end``."""
    origin = now()
    stop = asyncio.Event()
    runner = asyncio.create_task(panel.run(stop, FakeWeb()))
    for at, event in sorted(events, key=lambda timed: timed[0]):
        await asyncio.sleep(origin + at - now())
        event(panel)
    await asyncio.sleep(origin + end - now())
    stop.set()
    await runner


def measure(
    tmp_path: Path, variant: str, env: Mapping[str, str], end: float, latency: Seconds
) -> Measured:
    clock = ManualClock(Monotonic(10.0))
    if variant == "simulated":
        panel, link = latent_panel(tmp_path, clock, env)
        link.latency = latency
        run_virtual(clock, console(panel, SESSION, end))
        return from_link(link, panel)
    panel, bus = bus_panel(tmp_path, clock, env)
    bus.exchange_latency = bus.latency_of_next = latency
    run_virtual(clock, console(panel, SESSION, end))
    return from_bus(bus, panel)


@pytest.mark.parametrize("latency", [Seconds(0.01), Seconds(0.015)])
@pytest.mark.parametrize("variant", ["simulated", "fake_bus"])
def test_sto_reads_leave_every_command_write_at_its_instant(
    tmp_path: Path, variant: str, latency: Seconds
) -> None:
    """Two latencies under the read budget: the ticks fall at two phases of the period."""
    reading = measure(tmp_path / "reading", variant, STO_ENV, 13.95, latency)
    silent = measure(tmp_path / "silent", variant, NO_READ_ENV, 13.95, latency)
    assert silent.feedback == ()
    assert len(reading.feedback) >= 15
    assert len(reading.commands) >= 60  # dozens of ticks, each with its keepalive
    assert reading.commands == silent.commands
    assert reading.waited == ()


# =========================================================================
# No room left after the tick: no read, and every fallback still holds
# =========================================================================


def slow_link(bus: TimedBus, latency: Seconds) -> Event:
    def slow(_panel: LocalPanel) -> None:
        bus.exchange_latency = latency

    return slow


def test_ticks_that_fill_their_period_skip_every_read_and_latch_in_session(
    tmp_path: Path,
) -> None:
    """A session's ticks taking the whole period: no read, READY lost, then a controlled stop."""
    clock = ManualClock(Monotonic(10.0))
    panel, bus = bus_panel(tmp_path, clock, STO_ENV)
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    seen: list[Seen] = []

    def note(console: LocalPanel) -> None:
        reading = source.latest(Monotonic(now()))
        seen.append(
            Seen(source.attempts, console.sto_window_misses, reading, console.runtime.floor)
        )

    # 6 exchanges of 0.034 s: every tick of the running session lasts 0.204 s or more.
    events = (*SESSION, (4.15, note), (4.15, slow_link(bus, Seconds(0.034))), (9.15, note))
    run_virtual(clock, console(panel, events, 9.25))
    before, after = seen
    assert before.reading.state is StoState.READY
    assert before.verdict is None
    assert after.attempts == before.attempts  # no read once the ticks filled the period
    assert after.misses > before.misses + 5  # each tick's permission was skipped, and counted
    assert after.reading.state is StoState.UNKNOWN
    assert after.reading.last_success_at == before.reading.last_success_at
    assert not after.reading.read_unresolved  # a skipped read is no attempt
    verdict = after.verdict
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_UNREADABLE,
        SafetyAction.RAMP_DOWN,
        True,
    )
    last = after.reading.last_success_at
    assert last is not None
    assert verdict.since - last <= STO_STALE_AFTER + 0.272 + 1e-9  # one tick: 8 x 0.034 s


@dataclass(frozen=True, slots=True)
class Seen:
    attempts: int
    misses: int
    reading: StoReading
    verdict: SafetyVerdict | None


def test_ticks_that_fill_their_period_at_rest_lose_ready_and_refuse_arming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_module, "IDLE_POLL_PERIOD", Seconds(0.0))  # read every idle tick
    clock = ManualClock(Monotonic(10.0))
    panel, bus = bus_panel(tmp_path, clock, STO_ENV)
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    refusals: list[object] = []
    states: list[StoState] = []

    def try_to_arm(console: LocalPanel) -> None:
        states.append(source.latest(Monotonic(now())).state)
        assert isinstance(console.surface.attest_estop_wiring(OPERATOR), Ok)
        refused = console.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
        assert isinstance(refused, Err)
        refusals.append(refused.error)
        assert console.runtime.state is RuntimeState.IDLE
        assert console.sto_window_misses > 5

    def ready(_console: LocalPanel) -> None:
        states.append(source.latest(Monotonic(now())).state)

    # 4 status reads of 0.06 s: every idle tick lasts 0.24 s, and none leaves room.
    events = ((1.15, ready), (1.15, slow_link(bus, Seconds(0.06))), (4.15, try_to_arm))
    run_virtual(clock, console(panel, events, 4.35))
    assert states == [StoState.READY, StoState.UNKNOWN]
    (refusal,) = refusals
    assert isinstance(refusal, StoNotReady)
    assert refusal.state is StoState.UNKNOWN


# =========================================================================
# The residual: a read left without an answer
# =========================================================================


def silence(link: TimedBus | LatentLink) -> Event:
    def silent(_panel: LocalPanel) -> None:
        link.silent_feedback = True

    return silent


@dataclass(frozen=True, slots=True)
class Timeline:
    """Tick starts, and each tick's first frame out, from the instrumented console."""

    starts: tuple[float, ...]
    states: tuple[RuntimeState, ...]
    """The runtime's state at the end of each tick."""
    sent: tuple[Sent, ...]

    def stopped_at(self, after: float) -> float:
        """When the stop's last write went out: the run command removed (word 6)."""
        return next(
            sent.at
            for sent in self.sent
            if sent.access == "write"
            and sent.address == REGISTERS.cmd
            and sent.value == ControlWord.SHUTDOWN.value
            and sent.at >= after
        )

    def first_write_delays(self, after: float) -> tuple[tuple[float, float], ...]:
        """(tick start, delay of its first command write) for the ticks from ``after``."""
        delays: list[tuple[float, float]] = []
        for index, start in enumerate(self.starts):
            if start < after:
                continue
            end = self.starts[index + 1] if index + 1 < len(self.starts) else float("inf")
            writes = [
                sent.at for sent in self.sent if sent.access == "write" and start <= sent.at < end
            ]
            if writes:
                delays.append((start, writes[0] - start))
        return tuple(delays)

    def feedback_from(self, after: float) -> tuple[float, ...]:
        return tuple(
            sent.at
            for sent in self.sent
            if sent.access == "read" and sent.address == REGISTERS.logic_inputs and sent.at >= after
        )


def timed_console(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[tuple[float, Event], ...],
    end: float,
    build: Callable[[TimedBus], tuple[tuple[float, Event], ...]],
) -> Timeline:
    starts: list[float] = []
    states: list[RuntimeState] = []
    original = LocalPanel.control_step

    async def timed_step(panel: LocalPanel) -> TelemetrySnapshot:
        starts.append(now())
        snapshot = await original(panel)
        states.append(panel.runtime.state)
        return snapshot

    monkeypatch.setattr(LocalPanel, "control_step", timed_step)
    clock = ManualClock(Monotonic(10.0))
    panel, bus = bus_panel(tmp_path, clock, STO_ENV)
    run_virtual(clock, console(panel, (*events, *build(bus)), end))
    completed = len(states)
    return Timeline(
        tuple(start - 10.0 for start in starts[:completed]),
        tuple(states),
        tuple(Sent(sent.access, sent.address, sent.value, sent.at - 10.0) for sent in bus.sent),
    )


def test_an_unanswered_read_delays_the_next_tick_by_at_most_one_worst_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residual, bounded: transaction_worst_case, once per period, never two reads at once."""
    timeline = timed_console(
        tmp_path, monkeypatch, SESSION, 7.95, lambda bus: ((4.15, silence(bus)),)
    )
    unanswered = timeline.feedback_from(4.15)
    assert unanswered, "no read went to the silent drive"
    # Each read starts only once the previous one is resolved, a period or more later.
    for earlier, later in itertools.pairwise(unanswered):
        assert later - earlier >= max(WORST, STO_POLL_PERIOD)
    delays = timeline.first_write_delays(4.15)
    delayed = [(start, delay) for start, delay in delays if delay > 1e-9]
    assert delayed, "the residual did not show: no tick waited"
    assert all(delay <= WORST + 1e-9 for _, delay in delays)
    for start, _ in delayed:  # only a tick that began while an unanswered read was in flight
        assert any(read < start < read + WORST for read in unanswered)
    for (earlier, _), (later, _) in itertools.pairwise(delayed):
        assert later - earlier >= STO_POLL_PERIOD


def test_a_stop_asked_during_an_unanswered_read_waits_at_most_for_it_and_then_no_read_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case a stop write can wait for an STO read: one already in flight, bounded."""

    def stop(console: LocalPanel) -> None:
        assert isinstance(console.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)

    # Reads follow every third tick: the one after the tick at 4.4 s goes to a silent drive.
    timeline = timed_console(
        tmp_path,
        monkeypatch,
        (*SESSION, (4.55, stop)),
        11.95,
        lambda bus: ((4.35, silence(bus)),),
    )
    stopped = timeline.stopped_at(4.55)
    in_flight, *later = timeline.feedback_from(4.35)
    assert in_flight < 4.55 < in_flight + WORST
    # No read starts while the stop is pending or under way: only once it is over.
    assert later
    assert all(read > stopped for read in later)
    stopping = [
        (start, delay) for start, delay in timeline.first_write_delays(4.55) if start <= stopped
    ]
    (first, delay), *rest = stopping
    assert 0 < delay <= in_flight + WORST - first + 1e-9 <= WORST + 1e-9
    assert rest
    assert all(after <= 1e-9 for _, after in rest)


# =========================================================================
# Stops keep the link: no read starts once a stop is asked for
# =========================================================================


def operator_stop(panel: LocalPanel) -> None:
    assert isinstance(panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)


def dashboard_stop(panel: LocalPanel) -> None:
    ended = panel.surface.submit_end(operator=DASHBOARD_OPERATOR, reason="arret depuis le site")
    assert isinstance(ended, Ok)


def target_zero(panel: LocalPanel) -> None:
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(0), operator=OPERATOR), Ok
    )


def web_estop(panel: LocalPanel) -> None:
    panel.surface.submit_estop(operator=OPERATOR, reason="test")


def verdict(rule: str, action: SafetyAction) -> Event:
    def tripped(panel: LocalPanel) -> None:
        panel.runtime.trip_from_thread(rule, action, "injected for the test")

    return tripped


STOPS: Final[Mapping[str, Event]] = {
    "operator_stop": operator_stop,
    "dashboard_stop": dashboard_stop,
    "manual_target_zero": target_zero,
    "web_estop": web_estop,
    "ramp_down_verdict": verdict(RULE_PRESENCE_CAMERA_LOST, SafetyAction.RAMP_DOWN),
    "quick_stop_verdict": verdict(RULE_PRESENCE_INTRUSION, SafetyAction.QUICK_STOP),
}

ASKED: Final[float] = 5.15
"""When the stop is asked for: after a tick's STO read, never during one."""


@dataclass(frozen=True, slots=True)
class Stop:
    commands: tuple[tuple[int, int | None, float], ...]
    feedback: tuple[float, ...]
    stopped: float


def stopping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    env: Mapping[str, str],
    stop: Event,
) -> Stop:
    """The session, then a stop asked for while the drive stops answering 5202."""
    ends: list[tuple[float, bool]] = []
    original = LocalPanel.control_step

    async def noted_step(panel: LocalPanel) -> TelemetrySnapshot:
        snapshot = await original(panel)
        ends.append((now(), panel.runtime.stop_in_progress))
        return snapshot

    monkeypatch.setattr(LocalPanel, "control_step", noted_step)
    clock = ManualClock(Monotonic(10.0))
    end = 13.95
    if variant == "simulated":
        panel, link = latent_panel(tmp_path, clock, env)
        events = (*SESSION, (ASKED, silence(link)), (ASKED, stop))
        run_virtual(clock, console(panel, events, end))
        measured = from_link(link, panel)
    else:
        panel, bus = bus_panel(tmp_path, clock, env)
        events = (*SESSION, (ASKED, silence(bus)), (ASKED, stop))
        run_virtual(clock, console(panel, events, end))
        measured = from_bus(bus, panel)
    asked, closed = 10.0 + ASKED, 10.0 + end
    # The stop is over at the end of the first tick that leaves none under way:
    # its last write (zero setpoint, run command removed) has gone out.
    under_way = [at for at, stopping in ends if at >= asked and stopping]
    assert under_way, "the stop never got under way"
    stopped = next(
        (at for at, stopping in ends if at > under_way[0] and not stopping and at < closed),
        closed,
    )
    return Stop(
        tuple(command for command in measured.commands if asked <= command[2] <= stopped),
        tuple(began for began, _ in measured.feedback),
        stopped,
    )


@pytest.mark.parametrize("variant", ["simulated", "fake_bus"])
@pytest.mark.parametrize("category", list(STOPS))
def test_no_read_starts_once_a_stop_is_asked_for_and_stop_writes_keep_their_instants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str, category: str
) -> None:
    reading = stopping(tmp_path / "reading", monkeypatch, variant, STO_ENV, STOPS[category])
    silent = stopping(tmp_path / "silent", monkeypatch, variant, NO_READ_ENV, STOPS[category])
    asked = 10.0 + ASKED
    assert any(began < asked for began in reading.feedback)  # reads went on until then
    assert not any(began < asked < began + LATENCY for began in reading.feedback)
    # From the request to the end of the stop: no read at all, on a drive gone silent.
    assert [began for began in reading.feedback if asked <= began < reading.stopped] == []
    assert reading.stopped == silent.stopped
    assert reading.commands
    assert reading.commands == silent.commands
