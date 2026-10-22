"""ANH-162: the REAL console under a service manager that restarts it.

The console here is the production composition (:func:`src.local_panel.build_panel`,
:meth:`LocalPanel.run`), on the real clock, with a simulated drive and a real
local datagram socket where the service manager listens. What stands in for
systemd is :class:`ServiceManager`, a model of what ``scripts/anheart.service``
asks of it and of nothing else: it hears the notifications, ends a console that
has said nothing for the watchdog's time, and starts another one after a
failure. The real systemd, with the real unit, is exercised by
``scripts/pi/test_install.sh`` (workflow ``pi-install``).

**The simulated drive is the machine: it outlives every console.** Each
"process" is a console built anew on that same drive, so what the one before
left behind (an output stage enabled, a reference still set, a shaft turning)
is what the next one finds.

What is pinned:

* EX-1: the notifications are the completed ticks, one for one. A tick that
  never returns stops them although the event loop still runs everything
  else; a tick that raises sends none;
* EX-4: ``tick_exception`` and ``loop_stall`` end the process with a failure
  and the service manager starts another one. On a drive whose own timeout
  would NOT have stopped it, the new console finds it running, zeroes it and
  latches; nothing can move until a named operator has attested, acknowledged
  and started, and then given a target;
* EX-2: a console restarted three times after unclean stops comes up out of
  service, refuses every start, and is put back in service by name;
* EX-3: the drive is read by the first tick of a restarted console, before any
  command can exist, and a start that finds the last session record open
  writes a ``warning`` to the logbook.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Final, override

import pytest

from src import local_panel
from src.clock import RealClock
from src.control_surface import EventKind
from src.local_config import LocalConfig
from src.local_panel import (
    EXIT_FAILED,
    EXIT_OK,
    OUT_OF_SERVICE,
    DriveSide,
    LocalPanel,
    ServiceDesk,
    build_panel,
    open_service,
    run_console,
)
from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.simulated import SimState, SimulatedDrive, SimulatedDriveConfig
from src.record import logbook
from src.record.cursor import boot_identity
from src.record.export import listing
from src.record.journal import Journal
from src.result import Err, Ok, Result
from src.service_state import STATE_NAME, THRESHOLD, ServiceGuard, Stored, load
from src.service_watchdog import ALIVE, READY, STOPPING
from src.telemetry import TelemetryClient
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import SELF_CLEARING, EstopUnattested
from src.training.types import Occupancy, SafetyAction, TelemetrySnapshot
from src.units import MotorRpm, OutputRpm, Seconds
from tests.notify_support import Listener
from tests.test_failure_process import FakeWeb
from tests.test_failure_rig import (
    BENCH_ENV,
    ENERGISED,
    OPERATOR,
    Wrapped,
    backend,
    config_of,
    no_dsp,
)

NEVER: Final[Seconds] = Seconds(3600.0)
"""A drive communication timeout that will not fire in a test: the worst case, where
the drive's own safety net does not stop the motor a dead console left turning."""

WATCHDOG: Final[float] = 1.0
"""The model's ``WatchdogSec``. The unit's is 10 s; nothing below depends on the figure."""

FREEZE: Final[float] = 3.4
"""How long a console is frozen whole in these tests: past its own stall bound (3 s),
as the 10 s of the unit's watchdog are."""

STALL_WATCHDOG: Final[float] = 3.3
"""Longer than the console's own stall bound (3 s): a stop that arrives after it is
one the console itself must call unclean."""

ABORT: Final[float] = 0.5
"""The model's ``TimeoutAbortSec``: how long a console that answers nothing is waited
for before it is killed. The unit's is 15 s; nothing below depends on the figure."""

LEAVE: Final[float] = 10.0
"""How long a console that DOES answer is given to leave by itself in these tests: its
exit is what is asserted on, and a loaded machine must not turn it into a kill."""

GONE: Final[DriveError] = CommTimeout(Seconds(0.0))


class Link(Wrapped):
    """One console process's link to the drive, and the three things that happen to a process."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.bug: bool = False
        """The next status read raises: a bug inside the tick."""
        self.hung: bool = False
        """The next status read does not return: the tick is stuck, the event loop is not."""
        self.dead: bool = False
        """The process was killed: nothing it still does reaches the drive."""
        self.killed: asyncio.Event = asyncio.Event()
        """Set by the kill: what the process was waiting on dies with it."""
        self.stuck: asyncio.Event = asyncio.Event()
        """Set once a tick is inside the read that does not return."""

    @override
    async def open(self) -> Result[None, DriveError]:
        return Err(GONE) if self.dead else await self.inner.open()

    @override
    async def close(self) -> Result[None, DriveError]:
        return Err(GONE) if self.dead else await self.inner.close()

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        return Err(GONE) if self.dead else await self.inner.write_command(word)

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        return Err(GONE) if self.dead else await self.inner.write_speed(rpm)

    @override
    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return Err(GONE) if self.dead else await self.inner.read_limits()

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self.dead:
            return Err(GONE)
        if self.bug:
            raise RuntimeError("injected: a bug inside the tick")
        if self.hung:
            self.stuck.set()
            await self.killed.wait()
            return Err(GONE)
        return await self.inner.read_status()

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        if self.dead:
            return EmergencyStopOutcome.NOTHING_SENT
        return self.inner.emergency_disable_blocking(timeout)


@dataclass
class Console:
    """One process: a console built anew, its link, and its run."""

    panel: LocalPanel
    link: Link
    stop: asyncio.Event
    task: asyncio.Task[int]
    journal: Journal | None
    graveyard: set[int]
    """The journals of killed consoles (:func:`mortal_journals`)."""

    def acquired(self) -> int:
        """Batches the acquisition task has pumped so far: evidence the loop turns."""
        return self.panel.ecg.status().bridge.batches

    async def kill(self) -> None:
        """SIGKILL: from now on nothing this process does reaches the drive or the disk,
        and it ends."""
        self.graveyard.add(id(self.journal))
        self.link.dead = True
        self.link.killed.set()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


@dataclass
class Ticks:
    """The control ticks of the consoles of one test, counted from outside the console."""

    started: int = 0
    completed: int = 0
    """Ticks that returned. One that raised, or never came back, began and is not here."""


@dataclass
class Machine:
    """What outlives a console: the drive, the records directory, the service manager's socket."""

    sim: SimulatedDrive
    clock: RealClock
    listener: Listener
    root: Path
    graveyard: set[int]
    ticks: Ticks
    records: Path | None = None
    """Where the consoles keep their records and their restart state; ``None``: nowhere."""

    def boot(self) -> Console:
        """A new process on this machine. Nothing of the previous one is handed to it."""
        link = Link(self.sim)
        journal = None if self.records is None else Journal(self.records, self.clock)
        panel = build_panel(
            config_of(BENCH_ENV),
            clock=self.clock,
            profiles_path=self.root / "profiles.json",
            treat=no_dsp,
            drive=DriveSide(backend=backend(link), simulator=self.sim, release=_nothing),
            journal=journal,
            notify_socket=str(self.listener.path),
            service=None if journal is None else open_service(journal, self.clock),
        )
        stop = asyncio.Event()
        run = asyncio.create_task(panel.run(stop, FakeWeb(None)))
        return Console(panel, link, stop, run, journal, self.graveyard)


def _nothing() -> None:
    """The simulator has no port to release."""


@pytest.fixture
def graveyard(monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """The journals of killed consoles: from the kill on, their thread writes nothing.

    A killed process does not close its session record. In one process that
    takes this: the console's own exit path still runs when its task is
    cancelled, and would finalise the record a real kill leaves open.
    """
    dead: set[int] = set()
    real = Journal.drain

    def drain(journal: Journal) -> None:
        if id(journal) not in dead:
            real(journal)

    monkeypatch.setattr(Journal, "drain", drain)
    return dead


@pytest.fixture
def ticks(monkeypatch: pytest.MonkeyPatch) -> Ticks:
    """Count every control tick that begins and every one that returns.

    Around the console's own ``control_step``, so that what the service
    manager hears can be held against what the control loop really did,
    without asking the console.
    """
    counted = Ticks()
    real = LocalPanel.control_step

    async def control_step(panel: LocalPanel) -> TelemetrySnapshot:
        counted.started += 1
        snapshot = await real(panel)
        counted.completed += 1
        return snapshot

    monkeypatch.setattr(LocalPanel, "control_step", control_step)
    return counted


@pytest.fixture
def machine(tmp_path: Path, listener: Listener, graveyard: set[int], ticks: Ticks) -> Machine:
    clock = RealClock()
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True, tto=NEVER))
    return Machine(
        sim=sim, clock=clock, listener=listener, root=tmp_path, graveyard=graveyard, ticks=ticks
    )


@dataclass(frozen=True, slots=True)
class Ended:
    """How the service manager saw one console end."""

    code: int | None
    """Its exit code; ``None`` when it had to be killed."""

    watchdog: bool
    """Whether the watchdog is what ended it."""


class ServiceManager:
    """systemd, as far as ``anheart.service`` asks of it. A model: see the module docstring.

    ``Type=notify``: the watchdog counts from the first ``READY=1``.
    ``WatchdogSec``: a console that says nothing for that long is asked to stop
    (``WatchdogSignal=SIGTERM``, the console's own controlled stop), given
    ``TimeoutAbortSec`` to leave, then killed.
    """

    def __init__(self, machine: Machine, *, watchdog: float = WATCHDOG) -> None:
        self.machine: Machine = machine
        self.watchdog: float = watchdog
        self.heard: list[bytes] = []

    def listen(self) -> list[bytes]:
        """Take what arrived since the last call; all of it is kept in :attr:`heard`."""
        fresh = self.machine.listener.heard()
        self.heard.extend(fresh)
        return fresh

    async def watch(self, console: Console, *, frozen: bool = False) -> Ended:
        """Until the console ends by itself, or has said nothing for the watchdog's time.

        ``frozen``: the process does not answer a signal either (it is stopped,
        or stuck outside its event loop): only the kill ends it.
        """
        clock = self.machine.clock
        alive_at: float | None = None
        while True:
            fresh = self.listen()
            if any(ALIVE in datagram for datagram in fresh):
                alive_at = clock.monotonic()
            if console.task.done():
                return Ended(console.task.result(), watchdog=False)
            if alive_at is not None and clock.monotonic() - alive_at > self.watchdog:
                break
            await asyncio.sleep(0.02)
        if not frozen:
            console.stop.set()
        try:
            code = await asyncio.wait_for(asyncio.shield(console.task), ABORT if frozen else LEAVE)
        except TimeoutError:
            await console.kill()
            self.listen()
            return Ended(None, watchdog=True)
        return Ended(code, watchdog=True)


async def until(condition: Callable[[], bool], within: float, what: str) -> None:
    """Wait for ``condition`` on the real clock, or fail saying what never happened."""
    for _ in range(round(within / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never happened within {within} s: {what}")


async def first_tick(console: Console) -> None:
    """Until the console's control loop has completed a tick (and so read the drive)."""
    await until(lambda: console.panel.watchdog.status().sent > 0, 5.0, "a first completed tick")


async def at_speed(console: Console) -> float:
    """An operator starts a bench session and takes the arm to 2 output rpm. Motor rpm reached."""
    surface = console.panel.surface
    runtime = console.panel.runtime
    await first_tick(console)
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await until(lambda: state_of(console) is RuntimeState.RUNNING, 5.0, "the session running")
    surface.note_presence(OPERATOR)
    target = surface.submit_manual_target(output_rpm=OutputRpm(2.0), operator=OPERATOR)
    assert isinstance(target, Ok)

    def turning() -> bool:
        surface.note_presence(OPERATOR)
        return runtime.snapshot().measured.motor_rpm >= 55

    await until(turning, 8.0, "the shaft turning at speed")
    return float(runtime.snapshot().measured.motor_rpm)


def state_of(console: Console) -> RuntimeState:
    """The runtime's state, read through a call: a checker keeps a property's narrowing
    across an ``await``."""
    return console.panel.runtime.state


def shaft_of(console: Console) -> float:
    """The shaft speed the console last measured, motor rpm."""
    return abs(float(console.panel.runtime.snapshot().measured.motor_rpm))


async def assert_found_running_stopped_latched_and_inert(
    machine: Machine, console: Console
) -> None:
    """The acceptance of a restart on a drive left running. Nothing here is an operator
    action until the comment that says so."""
    panel = console.panel
    runtime = panel.runtime
    surface = panel.surface
    sim = machine.sim

    # The first tick read the drive, found it commanded by nobody, and took it down.
    await until(lambda: runtime.snapshot().safety is not None, 10.0, "a verdict at the restart")
    verdict = runtime.snapshot().safety
    assert verdict is not None
    assert verdict.rule == "drive_precommanded"
    assert verdict.latched
    assert verdict.action is SafetyAction.QUICK_STOP
    assert sim.commanded_setpoint == 0, "the reference the dead console left is zeroed"

    # The shaft comes to rest and the output stage is removed. Nobody touched anything.
    await until(lambda: shaft_of(console) < 1, 10.0, "the shaft at rest")
    await until(lambda: sim.sim_state not in ENERGISED, 5.0, "the output stage off")
    assert sim.commanded_setpoint == 0

    # Left alone, it stays there: a restarted console never resumes anything.
    await asyncio.sleep(1.0)
    assert shaft_of(console) < 1
    assert sim.commanded_setpoint == 0
    assert sim.sim_state not in ENERGISED
    assert state_of(console) is not RuntimeState.RUNNING
    assert runtime.snapshot().safety is not None, "the latch does not lift by itself"

    # Nothing of the session before came back: not the attestation, not the session.
    unattested = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(unattested, Err)
    assert isinstance(unattested.error, EstopUnattested)

    # FROM HERE AN OPERATOR ACTS. Attesting is not enough: the runtime's latch stands,
    # and the start it is handed is refused without a frame.
    watcher = panel.hub.subscribe()
    refused_before = surface.counters[1]
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await until(lambda: surface.counters[1] > refused_before, 10.0, "the loop's answer")
    assert await refusals(watcher) == [
        "demarrage refuse : verdict drive_precommanded a acquitter "
        f"({verdict.detail.removesuffix(SELF_CLEARING)})"
    ]
    assert state_of(console) is not RuntimeState.RUNNING
    assert sim.commanded_setpoint == 0
    assert sim.sim_state not in ENERGISED

    # Acknowledged by name, then started by name: armed, and still at rest.
    assert isinstance(surface.acknowledge(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await until(lambda: state_of(console) is RuntimeState.RUNNING, 5.0, "the new session running")
    for _ in range(5):
        surface.note_presence(OPERATOR)
        await asyncio.sleep(0.2)
    assert sim.commanded_setpoint == 0, "a session that was given no target commands nothing"
    assert shaft_of(console) < 1


async def refusals(watcher: TelemetryClient) -> list[str]:
    """What the loop refused since ``watcher`` subscribed, in the operator's words."""
    said: list[str] = []
    while True:
        try:
            payload = await asyncio.wait_for(watcher.next_payload(), 0.05)
        except TimeoutError:
            return said
        if payload.event is not None and payload.event.kind is EventKind.REFUSED:
            said.append(payload.event.detail)


async def end(console: Console) -> int:
    """SIGTERM, and the exit code. A stop with code 0 is written as clean, as
    :func:`~src.local_panel.run_console` does once its console has stopped."""
    console.stop.set()
    code = await asyncio.wait_for(console.task, 30.0)
    service = console.panel.service
    if code == EXIT_OK and service is not None:
        service.guard.stopped()
    return code


# =========================================================================
# EX-1: the feeding is the completed ticks, and nothing else
# =========================================================================


async def test_ex1_a_healthy_console_says_ready_then_alive_once_per_completed_tick(
    machine: Machine,
) -> None:
    manager = ServiceManager(machine)
    console = machine.boot()

    def ten_ticks() -> bool:
        manager.listen()
        return machine.ticks.completed >= 10

    await until(ten_ticks, 10.0, "ten completed ticks")
    assert await end(console) == EXIT_OK
    manager.listen()
    heard = manager.heard
    assert heard[0] == READY, "the first completed tick says the console is up"
    assert heard[-1] == STOPPING, "its own stop is announced"
    assert set(heard[1:-1]) == {ALIVE}
    # One notification per tick that returned, counted from outside the console,
    # and not one more: nothing else in the console sends any.
    assert len(heard) - 1 == machine.ticks.completed
    status = console.panel.watchdog.status()
    assert (status.sent, status.failed) == (len(heard), 0)


async def test_ex1_a_tick_that_never_returns_stops_the_feeding_while_the_loop_still_runs(
    machine: Machine,
) -> None:
    """The proof that the watchdog cannot be fed by a console whose control loop is stuck.

    Only the tick is stuck: the event loop, the acquisition task and this test
    itself go on running on it. A notification sent from a timer, from another
    task or from a thread would still arrive here. None does.
    """
    manager = ServiceManager(machine)
    console = machine.boot()
    ticks = machine.ticks
    await until(lambda: ticks.completed >= 5, 10.0, "five completed ticks")
    console.link.hung = True
    await asyncio.wait_for(console.link.stuck.wait(), 10.0)
    # A tick is now inside a read that does not return.
    manager.listen()
    completed = ticks.completed
    assert ticks.started == completed + 1
    assert len(manager.heard) == completed, "one notification per tick that returned, so far"
    acquired_before = console.acquired()

    await asyncio.sleep(2.0)

    assert manager.listen() == [], "a stuck tick told the service manager it was alive"
    assert (ticks.started, ticks.completed) == (completed + 1, completed), "still that same tick"
    assert console.panel.watchdog.status().sent == completed
    assert console.acquired() >= acquired_before + 3, "the loop itself was running all along"
    assert not console.task.done()
    await console.kill()


async def test_ex1_a_tick_that_raises_sends_nothing(machine: Machine) -> None:
    manager = ServiceManager(machine)
    console = machine.boot()
    ticks = machine.ticks
    await until(lambda: ticks.completed >= 5, 10.0, "five completed ticks")
    console.link.bug = True
    assert await asyncio.wait_for(console.task, 30.0) == EXIT_FAILED
    manager.listen()
    heard = manager.heard
    assert ticks.started == ticks.completed + 1, "one tick began and raised"
    assert heard[-1] == STOPPING, "the console says it stops"
    # Every tick that returned was notified, and the one that raised was not.
    assert len(heard) - 1 == ticks.completed
    assert set(heard[1:-1]) == {ALIVE}
    assert console.panel.runtime.end_reason is EndReason.TICK_EXCEPTION


# =========================================================================
# EX-4: tick_exception and loop_stall end the process; the restart never moves
# =========================================================================


@pytest.mark.slow
async def test_ex4_a_tick_exception_at_speed_ends_the_process_and_the_restart_stops_and_latches(
    machine: Machine,
) -> None:
    # A watchdog too long to fire here: this console must leave by itself.
    manager = ServiceManager(machine, watchdog=LEAVE)
    first = machine.boot()
    reached = await at_speed(first)
    assert reached >= 55

    first.link.bug = True
    ended = await manager.watch(first)
    assert ended == Ended(EXIT_FAILED, watchdog=False), "the process left by itself, in failure"
    # The dying console zeroed the reference; the output stage is still enabled,
    # and this drive's own timeout will not remove it.
    assert machine.sim.sim_state is SimState.OPERATION_ENABLED

    # Restart=on-failure: another process, on the same machine.
    second = machine.boot()
    await assert_found_running_stopped_latched_and_inert(machine, second)
    assert await end(second) == EXIT_OK


@pytest.mark.slow
async def test_ex4_a_stalled_loop_is_ended_by_the_watchdog_and_the_restart_stops_and_latches(
    machine: Machine,
) -> None:
    """``loop_stall`` with the event loop alive: the watchdog's SIGTERM is taken, the
    console leaves by its controlled stop, and says itself that this was no clean exit."""
    manager = ServiceManager(machine, watchdog=STALL_WATCHDOG)
    first = machine.boot()
    await at_speed(first)

    first.link.hung = True
    ended = await manager.watch(first)
    assert ended.watchdog, "nothing but the watchdog ended it"
    assert ended.code == EXIT_FAILED, "asked to stop while its loop completed no tick"
    assert first.panel.runtime.silent, "the stuck tick failed closed on its way out"
    assert machine.sim.sim_state is SimState.OPERATION_ENABLED

    second = machine.boot()
    await assert_found_running_stopped_latched_and_inert(machine, second)
    assert await end(second) == EXIT_OK


@pytest.mark.slow
async def test_ex4_a_frozen_console_is_killed_and_the_restart_stops_a_drive_still_at_speed(
    machine: Machine,
) -> None:
    """The worst case, and the one that matters most: the process answers nothing, it is
    killed, and the drive is left ENABLED AT SPEED with its reference still set."""
    manager = ServiceManager(machine)
    first = machine.boot()
    reached = await at_speed(first)
    setpoint = machine.sim.commanded_setpoint
    assert setpoint >= 55

    first.link.hung = True
    ended = await manager.watch(first, frozen=True)
    assert ended == Ended(None, watchdog=True), "the watchdog had to kill it"
    # Nothing was sent on the way out: the machine is exactly as the freeze left it.
    assert machine.sim.commanded_setpoint == setpoint
    assert machine.sim.sim_state is SimState.OPERATION_ENABLED

    second = machine.boot()
    await first_tick(second)
    assert shaft_of(second) >= reached - 5 or second.panel.runtime.snapshot().safety is not None
    await assert_found_running_stopped_latched_and_inert(machine, second)
    assert await end(second) == EXIT_OK


@pytest.mark.slow
async def test_ex4_when_the_drive_stopped_itself_the_restart_finds_it_stopped_and_starts_nothing(
    tmp_path: Path, listener: Listener, graveyard: set[int], ticks: Ticks
) -> None:
    """The nominal chain: the console freezes, the drive's own communication timeout
    stops the motor, the console is restarted and commands nothing."""
    clock = RealClock()
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True, tto=Seconds(1.0)))
    machine = Machine(
        sim=sim, clock=clock, listener=listener, root=tmp_path, graveyard=graveyard, ticks=ticks
    )
    manager = ServiceManager(machine)
    first = machine.boot()
    await at_speed(first)

    first.link.hung = True
    ended = await manager.watch(first, frozen=True)
    assert ended == Ended(None, watchdog=True)

    second = machine.boot()
    await until(lambda: shaft_of(second) < 1, 20.0, "the shaft at rest after the drive's timeout")
    await until(lambda: sim.sim_state not in ENERGISED, 5.0, "the output stage off")
    await asyncio.sleep(1.0)
    assert shaft_of(second) < 1
    assert state_of(second) is not RuntimeState.RUNNING
    assert sim.commanded_setpoint == 0
    assert sim.sim_state not in ENERGISED
    unattested = second.panel.surface.submit_start_manual(
        occupancy=Occupancy.BENCH, operator=OPERATOR
    )
    assert isinstance(unattested, Err)
    assert isinstance(unattested.error, EstopUnattested)
    await end(second)


def freeze_the_process(seconds: float) -> None:
    """SIGSTOP, as far as a process can do it to itself: its event loop runs nothing.

    Called from the test's own coroutine, which is on the console's loop: for
    ``seconds`` no task of the console is given a turn. Not a control tick,
    not a signal handler, not a timer. (Its other threads go on, which a real
    SIGSTOP would stop too; none of them ticks or notifies.)
    """
    ThreadEvent().wait(seconds)


@pytest.mark.slow
@pytest.mark.parametrize(
    "late_tick",
    [True, False],
    ids=["one-late-tick-runs-before-the-stop", "the-stop-is-handled-first"],
)
async def test_ex4_a_console_frozen_then_woken_by_its_own_stop_leaves_as_an_unclean_stop(
    machine: Machine, late_tick: bool
) -> None:
    """What systemd and Docker really do to a frozen console, replayed.

    The watchdog's stop is a SIGTERM followed by a SIGCONT, and ``docker run``
    relays both: the stop itself wakes the console. In the run that showed it
    (workflow ``pi-install``, step 9) the woken loop completed ONE tick, 10.09 s
    after the one before, and only then handled the stop. That tick must not
    make the stop a clean one: exit code 1, nothing written as clean, and the
    next start counts it. Whichever of the tick and the stop comes first.
    """
    records = machine.root / "records"
    machine.records = records
    manager = ServiceManager(machine)
    first = machine.boot()
    await at_speed(first)
    manager.listen()
    ticks = machine.ticks
    completed = ticks.completed

    freeze_the_process(FREEZE)

    assert manager.listen() == [], "a frozen console told the service manager nothing"
    assert ticks.completed == completed
    if late_tick:
        # The thaw. The loop runs again, and one tick comes back before the stop is handled.
        await until(lambda: ticks.completed > completed, 10.0, "the tick that comes back late")
        assert first.panel.runtime.silent, "that tick saw the stall and went silent for good"
        assert ALIVE in manager.listen(), "it is a completed tick, and is notified as one"
    code = await end(first)
    assert code == EXIT_FAILED, "a stop that follows a freeze is never a clean exit"
    left = load(records / STATE_NAME)
    assert isinstance(left, Stored)
    assert left.running, "and it is never written as a clean stop"

    second = machine.boot()
    service = second.panel.service
    assert service is not None
    status = service.status()
    assert status.unclean_stop
    assert status.unclean_starts == 1, "the restart after a freeze is counted"
    # The console left by its own exit path: it closed its record on the way.
    assert status.open_record is None
    if late_tick:
        await assert_found_running_stopped_latched_and_inert(machine, second)
    assert await end(second) == EXIT_OK


# =========================================================================
# EX-3: the drive first, and the warning after an unclean stop
# =========================================================================


async def test_ex3_a_restarted_console_reads_the_drive_before_any_command_can_exist(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the restart path adds comes after the drive was read: the notification that
    the console is ready, and the write of this start to the disk. Before the first tick
    the restart state was only read."""
    records = machine.root / "records"
    machine.records = records
    seen: list[tuple[int, bool]] = []
    real = ServiceDesk.record_start

    async def record_start(desk: ServiceDesk) -> None:
        # What was true at the instant this start is handed to the disk.
        seen.append((console.panel.runtime.idle_link.reads, state_at(records) is None))
        await real(desk)

    monkeypatch.setattr(ServiceDesk, "record_start", record_start)
    console = machine.boot()
    surface = console.panel.surface
    assert surface.pending is None
    assert console.panel.runtime.idle_link.reads == 0, "building the console read nothing"
    assert machine.listener.heard() == [], "and it has not said it is ready"
    assert state_at(records) is None, "and nothing of this start is on the disk yet"
    await first_tick(console)
    assert console.panel.runtime.idle_link.reads >= 1, "ready is said after the drive was read"
    await until(lambda: state_at(records) is not None, 10.0, "the start written to the disk")
    assert len(seen) == 1, "this start is written once"
    reads, unwritten = seen[0]
    assert reads >= 1, "the drive was read before this start was written"
    assert unwritten
    assert await end(console) == EXIT_OK


def logbook_lines(records: Path) -> list[dict[str, object]]:
    path = records / logbook.DIRECTORY / logbook.ACTIVE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def unclean_warnings_in(records: Path) -> list[dict[str, object]]:
    return [
        line
        for line in logbook_lines(records)
        if line["kind"] == "warning"
        and isinstance(detail := line.get("detail"), str)
        and detail.startswith("unclean_stop:")
    ]


@pytest.mark.slow
async def test_ex3_a_start_after_a_killed_session_writes_a_warning_naming_the_open_record(
    machine: Machine,
) -> None:
    records = machine.root / "records"
    machine.records = records
    first = machine.boot()
    await at_speed(first)
    await until(lambda: bool(listing(records)), 5.0, "the session's record on disk")
    await first.kill()
    left = listing(records)
    assert len(left) == 1
    assert not left[0].closed, "a killed console leaves its record open"

    second = machine.boot()
    service = second.panel.service
    assert service is not None
    status = service.status()
    assert status.unclean_stop
    assert status.open_record == left[0].name
    assert status.unclean_starts == 1
    assert not status.out_of_service
    await until(lambda: bool(unclean_warnings_in(records)), 5.0, "the warning in the logbook")
    said = unclean_warnings_in(records)
    assert len(said) == 1
    assert said[0]["actor"] == "system"
    assert said[0]["detail"] == (
        "unclean_stop: demarrage apres arret non propre : "
        f"enregistrement de seance {left[0].name} non clos"
    )
    # The drive was left running too: the same restart stops it and latches.
    await assert_found_running_stopped_latched_and_inert(machine, second)
    assert await end(second) == EXIT_OK

    # A third start follows that clean stop: nothing more is written about the old record.
    third = machine.boot()
    third_service = third.panel.service
    assert third_service is not None
    assert not third_service.status().unclean_stop
    assert await end(third) == EXIT_OK
    assert len(unclean_warnings_in(records)) == 1


# =========================================================================
# EX-2: what the real entry point writes of a start and of a stop
# =========================================================================


def recording_config(records: Path) -> LocalConfig:
    return config_of({**BENCH_ENV, "RECORD_ROOT": str(records)})


def state_at(records: Path) -> Stored | None:
    """The state on disk, when there is one that reads."""
    found = load(records / STATE_NAME)
    return found if isinstance(found, Stored) else None


async def test_ex2_run_console_writes_running_after_its_first_tick_and_stopped_only_at_a_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_console`` itself, on the real clock, with a web server that serves nothing."""
    records = tmp_path / "records"
    webs: list[FakeWeb] = []

    def web(_services: object, _config: object) -> FakeWeb:
        webs.append(FakeWeb(None))
        return webs[0]

    monkeypatch.setattr(local_panel, "UvicornRunner", web)
    stop = asyncio.Event()
    run = asyncio.create_task(run_console(recording_config(records), stop=stop))
    await until(lambda: state_at(records) is not None, 20.0, "the start written to the disk")
    while_running = state_at(records)
    assert while_running is not None
    assert while_running.running, "a console that is running is written as running"
    assert not while_running.out_of_service
    stop.set()
    assert await asyncio.wait_for(run, 30.0) == EXIT_OK
    after = state_at(records)
    assert after is not None
    assert not after.running, "its clean stop is written, last"


async def test_ex2_run_console_never_writes_a_failed_exit_as_a_clean_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"

    def web(_services: object, _config: object) -> FakeWeb:
        return FakeWeb(1.0)  # the web server dies a second in: the console stops, in failure

    monkeypatch.setattr(local_panel, "UvicornRunner", web)
    code = await asyncio.wait_for(run_console(recording_config(records)), 30.0)
    assert code == EXIT_FAILED
    left = state_at(records)
    assert left is not None, "it ticked: its start was written"
    assert left.running, "an exit in failure leaves the state saying running"
    # And the next start counts it.
    again = ServiceGuard(records, RealClock())
    found = again.begin(boot_id=boot_identity(), open_record=None)
    assert found.unclean_stop
    assert found.unclean_starts == 1


# =========================================================================
# EX-2: a console that keeps restarting comes up out of service
# =========================================================================


@pytest.mark.slow
async def test_ex2_three_restarts_after_unclean_stops_take_the_console_out_of_service(
    machine: Machine, caplog: pytest.LogCaptureFixture
) -> None:
    records = machine.root / "records"
    machine.records = records
    manager = ServiceManager(machine)
    console = machine.boot()
    for death in range(THRESHOLD):
        service = console.panel.service
        assert service is not None
        assert not service.status().out_of_service, f"out of service after {death} deaths"
        assert service.status().unclean_starts == death
        await first_tick(console)
        console.link.bug = True
        ended = await manager.watch(console)
        assert ended.code == EXIT_FAILED
        console = machine.boot()

    # The third restart: the console runs, says so, and arms nothing.
    service = console.panel.service
    assert service is not None
    status = service.status()
    assert status.out_of_service
    assert status.unclean_starts == THRESHOLD
    assert status.persisted
    assert service.refusal() == OUT_OF_SERVICE
    assert "out_of_service: console hors service" in caplog.text
    await first_tick(console)
    surface = console.panel.surface
    hub = console.panel.hub.subscribe()
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await until(lambda: surface.counters[1] >= 1, 10.0, "the loop's answer")
    assert state_of(console) is RuntimeState.IDLE, "a console out of service armed"
    assert machine.sim.sim_state not in ENERGISED
    assert await refusals(hub) == [f"demarrage refuse : {OUT_OF_SERVICE}"]

    # Out of service is on disk: a clean restart, even much later, changes nothing.
    assert await end(console) == EXIT_OK
    console = machine.boot()
    service = console.panel.service
    assert service is not None
    assert service.status().out_of_service

    # A named operator puts it back in service: in effect at once, and kept.
    restored = await service.restore(OPERATOR)
    assert isinstance(restored, Ok)
    assert not restored.value.out_of_service
    assert restored.value.persisted
    assert service.refusal() is None
    kept = load(records / STATE_NAME)
    assert isinstance(kept, Stored)
    assert not kept.out_of_service
    surface = console.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await until(lambda: state_of(console) is RuntimeState.RUNNING, 5.0, "a session, in service")
    assert await end(console) == EXIT_OK
    lines = logbook_lines(records)
    restores = [line for line in lines if str(line["detail"]).startswith("service_restored")]
    assert [line["detail"] for line in restores] == ["service_restored: console remise en service"]
    assert restores[0]["kind"] == "operator_action"
    assert str(restores[0]["actor"]).startswith("op-"), "an alias, never the name"
    assert OPERATOR not in (records / logbook.DIRECTORY / logbook.ACTIVE).read_text(
        encoding="utf-8"
    )
    # Restoring twice: there is nothing left to lift.
    assert isinstance(await service.restore(OPERATOR), Err)
