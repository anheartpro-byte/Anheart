"""The control tick never waits on the session record: ANH-128 EX-2, measured and proven.

Five angles on the one safety requirement of the black box:

* **measured**: the same console, ticked with and without the writer active,
  takes the same time per tick. The medians, read on a watch, differ by far
  less than a millisecond (the supervisor's ``loop_stall`` rule acts at
  600 ms): a tick that waits or computes more every time moves a median, the
  few ticks a loaded runner sets aside do not. The tail is judged on the
  processor time of the loop thread, which the load of the machine does not
  move (ANH-183);
* **stopped wherever it stands**: the journal thread is held before every
  line of ``src/record/`` it runs, through the opening of a record, its
  rows, its closing and the periodic work, and at each of them the calls a
  tick makes return at once. A tick that waits on that thread only now and
  then, too rarely for a median, has no line to hide behind (ANH-183). Since
  ANH-191 the thread also takes the drive's frames and writes the logbook,
  and a tick also notes each drive call and each console event: both sides
  are in that test;
* **structural**: while a recorded session ticks, the thread that runs the
  loop opens, creates, renames, truncates and flushes NOTHING under the
  records directory. Every one of those is the journal thread's;
* **adversarial**: with a disk that does not answer at all, the real task
  loop goes on ticking at 5 Hz, no ``loop_stall`` is ever raised, the operator
  is told the record is degraded, and the session ends when asked;
* **with a person on board**: record reads stuck on that same dead disk take
  no thread from the ECG treatment. The heart rate stays fresh, no verdict is
  raised, and the console still stops.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from types import CodeType
from typing import Final, Literal

import pytest

import src.record.writer as writer_module
from src.clock import ManualClock, RealClock
from src.control_surface import EventKind
from src.ecg_pipeline import treat_off_loop
from src.local_panel import EXIT_OK, DriveSide, LocalPanel, build_panel
from src.motor import observation as observation_module
from src.motor.observation import Exchange, ExchangeKind, ExchangeLog
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record import export as export_module
from src.record import journal as journal_module
from src.record.codec import Privacy
from src.record.drive_tap import CallTap, Taken, exchange_source
from src.record.export import RecordEntry
from src.record.journal import STOP_TIMEOUT, Activity, Cause, Item, Journal, Limits, Scribe
from src.record.logbook import ACTIVE as LOGBOOK_FILE
from src.record.logbook import DIRECTORY as LOGBOOK
from src.record.logbook import note
from src.record.reader import read
from src.record.retention import records
from src.record.schema import Event as RecordEvent
from src.record.schema import EventKind as RecordEventKind
from src.result import Ok
from src.telemetry import PayloadKind
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_LOOP_STALL
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import Monotonic, OutputRpm, RawRegister, RegisterAddress, Seconds, UnixMillis
from tests.record_console_support import recorded_rig, set_target
from tests.record_journal_support import (
    batch,
    closing,
    count_fsync,
    journal_threads,
    session,
    wait_for,
    watch_tree,
)
from tests.record_support import row
from tests.test_failure_process import FakeWeb
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    TICK,
    Rig,
    attest,
    config_of,
    make_rig,
    no_dsp,
)

BLOCKS: Final[int] = 6
TICKS_PER_BLOCK: Final[int] = 250

SAME: Final[float] = 0.001
"""Seconds on a watch. "The tick does not change": the medians differ by less than this."""

EXIT_BOUND: Final[float] = 12.0
"""Seconds. A console whose records disk is dead still leaves: the 5 s it gives the
journal thread (``STOP_TIMEOUT``), plus the rest of its exit, with room for a slow runner."""

TAIL: Final[float] = 0.1
"""Seconds of processor. Ninety-nine ticks in a hundred, writer active, stay under a
sixth of the 0.6 s ``loop_stall`` FREEZE.

The percentile, not the maximum: now and then a tick computes for longer
whatever the code under it does (the interpreter collects its garbage inside
whichever call set it off), and that says nothing about the writer. The
slowest tick is printed. The bound on every single tick is the tests that
follow: the calls a tick makes return at once wherever the journal thread
stands, nothing the tick does touches the disk, and under the real loop a disk
that never answers raises no ``loop_stall``."""


@dataclass(frozen=True, slots=True)
class Spent:
    """What one ``control_step`` took, in seconds, read on two clocks."""

    watch: float
    """From its start to its end, as a watch reads it (``time.perf_counter``).

    It counts everything a tick can cost: what it computes, and what it waits
    for (a lock, a sleep, the interpreter held by another thread). Its median
    is judged. Its tail is printed and not judged: on a shared runner the
    watch also counts every moment the thread was set aside for another
    process (170 ms was seen for one tick on a loaded machine, in the arm
    WITHOUT the writer as well)."""
    processor: float
    """What the loop thread itself spent computing (``time.thread_time``).

    Its tail is judged: it does not move with the load of the machine. It
    counts no wait, which is why it is not the only thing judged."""


def verdict(without: Sequence[Spent], with_writer: Sequence[Spent]) -> tuple[bool, str]:
    """Whether the tick takes the same time with the writer active, and the measurement as text.

    Two rules, each on the clock that can carry it:

    * the medians on a watch differ by less than ``SAME``. Every tick that
      waits, or computes more, is longer on a watch, and the median with it;
      a median does not move for a few ticks set aside by the machine, and the
      two arms are measured in turn, so a slow moment falls on both;
    * ninety-nine ticks in a hundred, writer active, compute for less than
      ``TAIL``. On a watch that tail belongs to the runner as much as to the
      code, so it is read on the processor time of the thread.

    Neither sees a tick that waits on the journal thread once in a while: the
    next test does, without a stopwatch.
    """

    def median(ticks: Sequence[Spent], *, processor: bool) -> float:
        return statistics.median(tick.processor if processor else tick.watch for tick in ticks)

    def p99(ticks: Sequence[Spent], *, processor: bool) -> float:
        values = [tick.processor if processor else tick.watch for tick in ticks]
        return statistics.quantiles(values, n=100)[98]

    def shown(seconds: float) -> str:
        return f"{seconds * 1e6:.0f} us"

    summary = (
        f"control tick, {len(without)} ticks each. On a watch: "
        f"median {shown(median(without, processor=False))} without the writer, "
        f"{shown(median(with_writer, processor=False))} with (judged); "
        f"p99 {shown(p99(without, processor=False))} without, "
        f"{shown(p99(with_writer, processor=False))} with; "
        f"max {shown(max(tick.watch for tick in without))} without, "
        f"{shown(max(tick.watch for tick in with_writer))} with. "
        f"Processor time of the loop thread: "
        f"median {shown(median(without, processor=True))} without, "
        f"{shown(median(with_writer, processor=True))} with; "
        f"p99 {shown(p99(without, processor=True))} without, "
        f"{shown(p99(with_writer, processor=True))} with (judged)"
    )
    same = median(with_writer, processor=False) - median(without, processor=False) < SAME
    return same and p99(with_writer, processor=True) < TAIL, summary


async def cruising(tmp_path: Path, name: str, *, record: bool) -> tuple[Rig, Journal | None]:
    """A console climbing in a bench manual session; with its journal THREAD when ``record``."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    # This test ticks hundreds of times faster than real time: the bounds that fit a
    # real session are raised so the writer has every tick to write, not a sample of them.
    roomy = Limits(entries=1_000_000, samples=100_000_000)
    journal = (
        Journal(tmp_path / name / "records", clock, limits=roomy, period=Seconds(0.005))
        if record
        else None
    )
    rig, _ = make_rig(tmp_path / name, clock=clock, journal=journal)
    surface = rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await rig.tick(1.0)
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(20.0), operator=OPERATOR), Ok
    )
    await rig.tick(2.0)
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    return rig, journal


async def timed_ticks(rig: Rig, count: int) -> list[Spent]:
    """``count`` control ticks as the loop runs them; what each ``control_step`` took."""
    panel = rig.panel
    spent: list[Spent] = []
    for index in range(count):
        rig.clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        if index % 5 == 0:
            await panel.sensor_step()
        started = time.perf_counter()
        computing = time.thread_time()
        await panel.control_step()
        computed = time.thread_time() - computing
        spent.append(Spent(watch=time.perf_counter() - started, processor=computed))
    return spent


async def test_ex2_the_tick_takes_the_same_time_with_the_writer_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A manual clock runs far ahead of the journal thread's real one: without
    # this the stall detector would call a few milliseconds of backlog a stall.
    monkeypatch.setattr(journal_module, "STALL_AFTER", Seconds(1e9))
    (tmp_path / "plain").mkdir()
    (tmp_path / "recorded").mkdir()
    plain, _ = await cruising(tmp_path, "plain", record=False)
    recorded, journal = await cruising(tmp_path, "recorded", record=True)
    assert journal is not None
    journal.start()

    without: list[Spent] = []
    with_writer: list[Spent] = []
    for _ in range(BLOCKS):  # interleaved, so a slow moment of the machine hits both
        without.extend(await timed_ticks(plain, TICKS_PER_BLOCK))
        with_writer.extend(await timed_ticks(recorded, TICKS_PER_BLOCK))

    wait_for(lambda: journal.status(recorded.clock.monotonic()).pending == 0, timeout=30.0)
    status = journal.status(recorded.clock.monotonic())
    assert journal.stop()
    assert not status.degraded, status
    path = status.path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.rows) >= BLOCKS * TICKS_PER_BLOCK, "the writer really was active"
    assert len(loaded.value.raw) >= BLOCKS * TICKS_PER_BLOCK

    same, summary = verdict(without, with_writer)
    print(summary)  # noqa: T201 - the measurement the ticket asks for, shown with -s
    assert same, summary
    await plain.panel.close()
    await recorded.panel.close()


def test_ex2_the_verdict_on_the_tick_sees_a_wait_and_not_the_load_of_the_machine() -> None:
    """ANH-183 EX-8: a tick that waits is refused; a few ticks set aside by a runner are not."""
    usual = Spent(watch=60e-6, processor=50e-6)
    quiet = [usual] * 300
    # A loaded runner: now and then a tick is set aside for 200 ms, in the writer's arm
    # only, the worst case for the comparison. The thread computed no more.
    crowded = [
        *[Spent(watch=62e-6, processor=52e-6)] * 290,
        *[Spent(watch=0.2, processor=52e-6)] * 10,
    ]
    passed, said = verdict(quiet, crowded)
    assert passed, said
    assert "200000 us with" in said, "the slowest tick on a watch is still shown"
    # Every tick waits 5 ms on the record and computes no more: no processor time shows it.
    waiting = [Spent(watch=5.06e-3, processor=52e-6)] * 300
    waited, _ = verdict(quiet, waiting)
    assert not waited
    # The same wait in every tick of BOTH arms is the machine, not the writer.
    slow_machine, _ = verdict([Spent(watch=5.06e-3, processor=50e-6)] * 300, waiting)
    assert slow_machine
    # The writer makes the tick compute 2 ms more: no load explains that.
    costlier = [Spent(watch=2.1e-3, processor=2.05e-3)] * 300
    refused, _ = verdict(quiet, costlier)
    assert not refused
    # One tick in fifty computes for 150 ms with the writer: the median hides it, the tail does not.
    heavy_tail = [*[usual] * 294, *[Spent(watch=0.15, processor=0.15)] * 6]
    tailed, _ = verdict(quiet, heavy_tail)
    assert not tailed
    # A single long tick (the interpreter collecting its garbage, say) is not the writer's doing.
    one_pause = [*[usual] * 299, Spent(watch=0.3, processor=0.3)]
    spared, _ = verdict(quiet, one_pause)
    assert spared


# --- The journal thread, held wherever it stands -----------------------------------
#
# A stopwatch cannot see a tick that waits on the journal thread only now and
# then (a lock that thread holds for a moment of each cycle, say): too rare for
# a median, and a bound on every single tick would judge the runner. What
# follows times nothing. It holds the journal thread before each instruction
# of ``src/record/`` it is about to run, and makes the calls of a tick while it
# stands there: whatever that thread holds at that point, it holds for as long
# as the test wants, so a call that needs it does not return.
#
# What it cannot see, stated rather than hidden: something taken and given
# back inside one call that leaves ``src/record/`` (a library, the interpreter
# itself), and the code this scenario does not make the thread run (what it
# does when the disk refuses a write).

RECORD_SOURCES: Final[str] = str(Path(journal_module.__file__).resolve().parent)
"""``src/record/``: the journal thread is held before every instruction of it."""

HELD_SOURCES: Final[tuple[str, ...]] = (
    RECORD_SOURCES,
    str(Path(observation_module.__file__).resolve()),
)
"""Where the journal thread is held: ``src/record/``, and the exchange log of the drive
seam, which that thread drains since ANH-191 (``src/motor/observation.py``)."""

WAITED: Final[float] = 5.0
"""Seconds. A call of a tick that has not returned after this was waiting on the journal
thread, which is held: nothing but this test lets it go, so the wait would never end. No
call that does not wait comes near it, whatever the machine is doing; it is only ever
reached on the way to a failure."""

SETTLED: Final[float] = 120.0
"""Seconds. How long the journal thread is given to write what one step of the scenario
queued, held at every new instruction on the way: past it the failure names that step. It
judges no speed."""


@dataclass(frozen=True, slots=True)
class Stop:
    """Where the journal thread was held: before an instruction of this line of the held sources."""

    file: str
    function: str
    line: int


def line_of(code: CodeType, offset: int) -> int:
    """The line of the instruction at ``offset`` of ``code``; 0 when it has none."""
    for first, after, line in code.co_lines():
        if first <= offset < after:
            return line or 0
    return 0


class Stepper:
    """Holds the journal thread before each instruction of ``HELD_SOURCES`` it has not run yet.

    Mutable on purpose: two threads hand each other the turn through it. The
    journal thread is held inside ``arrived``, which the interpreter calls
    before an instruction; the test reads ``held``, makes its calls through
    ``calling``, and lets the thread go to its next stop with ``resume``.

    An instruction is a stop the first time the thread gets there, not every
    time: what a thread holds at a point of its code, a lock taken above it,
    it holds every time it gets there, and one more stop would show nothing
    more. A watchdog thread lets everything go when a call of the test has
    been waiting for ``WAITED``: the test then fails instead of hanging.
    """

    def __init__(self, strangers: frozenset[int]) -> None:
        self._strangers: frozenset[int] = strangers
        self._held: threading.Event = threading.Event()
        self._resumed: threading.Event = threading.Event()
        self._over: threading.Event = threading.Event()
        self._at: Stop | None = None
        self._calling_since: float | None = None
        self.waited_at: Stop | None = None
        """Where the journal thread was held when a call of the test waited on it."""

    def arrived(self, code: CodeType, offset: int) -> object:
        """Called by the interpreter, in the thread about to run that instruction of ``code``."""
        if not code.co_filename.startswith(HELD_SOURCES):
            return sys.monitoring.DISABLE
        thread = threading.current_thread()
        if thread.name != "record-journal" or thread.ident in self._strangers:
            # The loop thread runs some of this code too: it stays a stop for the journal thread.
            return None
        if self._over.is_set():
            return sys.monitoring.DISABLE
        self._at = Stop(Path(code.co_filename).name, code.co_name, line_of(code, offset))
        self._resumed.clear()
        self._held.set()
        self._resumed.wait()
        return sys.monitoring.DISABLE

    def held(self, patience: float) -> Stop | None:
        """Where the journal thread is held; ``None`` if it still runs after ``patience``."""
        return self._at if self._held.wait(patience) else None

    def calling(self, calls: Callable[[], None]) -> None:
        """Make calls of the loop, watched: if they wait on the journal thread, it is let go."""
        self._calling_since = time.monotonic()
        calls()
        self._calling_since = None

    def resume(self) -> None:
        self._at = None
        self._held.clear()
        self._resumed.set()

    def release(self) -> None:
        """No more stops: the journal thread runs on."""
        self._over.set()
        self.resume()

    def watch(self) -> None:
        """The watchdog: let the journal thread go once a call has waited too long on it."""
        while not self._over.wait(0.1):
            since = self._calling_since
            if since is not None and time.monotonic() - since > WAITED:
                self.waited_at = self._at
                self.release()


@contextmanager
def stepped() -> Generator[Stepper]:
    """Hold the journal thread started inside the block before each new instruction it runs."""
    monitoring = sys.monitoring
    # A tool number that no debugger, coverage tool or profiler takes: the coverage of the
    # gate goes on measuring this test like any other.
    tool = next(number for number in (3, 4) if monitoring.get_tool(number) is None)
    # Journal threads other tests left behind (one stuck on a dead disk is a daemon) are not ours.
    stepper = Stepper(frozenset(thread.ident or 0 for thread in journal_threads()))
    watchdog = threading.Thread(target=stepper.watch, name="stepper-watchdog", daemon=True)
    monitoring.use_tool_id(tool, "anheart-journal-stepper")
    try:
        monitoring.restart_events()  # what an earlier use switched off is a stop again
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, stepper.arrived)
        monitoring.set_events(tool, monitoring.events.INSTRUCTION)
        watchdog.start()
        yield stepper
    finally:
        stepper.release()
        monitoring.set_events(tool, monitoring.events.NO_EVENTS)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)
        watchdog.join(WAITED)


def test_ex2_the_calls_of_a_tick_return_at_once_wherever_the_journal_thread_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANH-183 EX-8: nowhere in its cycle does the journal thread hold what a tick needs.

    ANH-191 gave that thread two more things to do, and the tick three more
    calls. The thread takes the drive's frames at every cycle and writes the
    logbook: it is held in that code too. A tick notes each call it makes to
    the drive, the native driver notes each exchange (on its worker, which the
    tick awaits), and a console event becomes a line for the logbook: each of
    the three is made at every stop, and must return at once like the others.
    """
    monkeypatch.setattr(journal_module, "STALL_AFTER", Seconds(1e9))
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_791_195_000_000))
    roomy = Limits(entries=1_000_000, samples=100_000_000)
    journal = Journal(
        tmp_path / "records", clock, limits=roomy, period=Seconds(0.001), retention_days=30
    )
    # Both kinds of drive at once, each with room for a frame per stop: the tap around
    # one that reports nothing, and the exchange log of one that reports its own.
    tap = CallTap(SimulatedDrive(clock), clock, capacity=1_000_000)
    exchanges = ExchangeLog(clock, capacity=1_000_000)
    reported = exchange_source(exchanges)

    def frames() -> Taken:
        """What the journal thread takes at every cycle: it is held inside both."""
        noted = tap.take()
        native = reported()
        return Taken((*noted.observations, *native.observations), noted.lost + native.lost)

    journal.listen(frames)
    drive_calls = asyncio.new_event_loop()
    cycles: list[int] = []
    whole_cycle = Scribe.cycle

    def counted(scribe: Scribe, queue: deque[Item], dropped: int, unlogged: int) -> None:
        whole_cycle(scribe, queue, dropped, unlogged)
        cycles.append(len(queue))

    monkeypatch.setattr(Scribe, "cycle", counted)
    recording = [False]
    stops: list[Stop] = []

    def calls_of_a_tick() -> None:
        """What the loop asks of the journal in a tick. Either way in, a value is queued."""
        if recording[0]:
            assert journal.submit(row()), "a value was refused: nothing was queued at this stop"
        else:
            journal.close(closing())  # never refused; with no record open the thread drops it
        # A call to the drive, through the tap that notes it for the record.
        drive_calls.run_until_complete(tap.read_status())
        # An exchange of the native driver, at the one entry every one of them goes through.
        exchanges.append(
            Exchange(
                at=clock.monotonic(),
                kind=ExchangeKind.READ,
                register=RegisterAddress(3201),
                value=RawRegister(0x0637),
                ok=True,
                latency_ms=1.0,
                detail="read",
            )
        )
        # A console event, session or not: a line for the logbook.
        line = note(
            wall_clock=clock.unix_millis(),
            at=clock.monotonic(),
            kind=RecordEventKind.REFUSAL,
            detail="refused: demarrage refuse",
            actor="op-1",
        )
        logged = journal.log(line)
        assert logged, "a line of the logbook was refused: nothing was queued at this stop"
        status = journal.status(clock.monotonic())
        assert status.recording is recording[0]
        assert status.free_bytes == journal.storage.free_bytes
        # What the arming gate reads besides the free space.
        assert journal.activity in tuple(Activity)

    try:
        with stepped() as stepper:

            def asked(step: str, calls: Callable[[], None]) -> None:
                """Make calls of the loop; fail if they had to wait for the journal thread."""
                stepper.calling(calls)
                assert stepper.waited_at is None, (
                    f"{step}: a call of the loop waited on the journal thread, "
                    f"held at {stepper.waited_at}"
                )

            def settle(step: str) -> None:
                """Go on, stop by stop, until all that is queued is written and nothing is new."""
                deadline = time.monotonic() + SETTLED
                quiet_since = len(cycles)
                while True:
                    at = stepper.held(0.02)
                    if at is not None:
                        stops.append(at)
                        asked(step, calls_of_a_tick)
                        stepper.resume()
                        quiet_since = len(cycles)
                    elif (
                        journal.status(clock.monotonic()).pending == 0
                        and len(cycles) >= quiet_since + 3
                    ):
                        return
                    assert time.monotonic() < deadline, (
                        f"{step}: the journal thread did not get there"
                    )

            def opened_and_written_to() -> None:
                journal.open(session(1), Privacy())
                assert journal.submit(row())
                assert journal.submit(batch(0))
                assert journal.submit(RecordEvent(t=0.2, kind=RecordEventKind.PHASE, detail="hold"))
                journal.start()

            def later() -> None:
                clock.advance(Seconds(6.0))
                assert journal.submit(row())

            def opened_over_one_left_open() -> None:
                journal.open(session(2), Privacy())
                assert journal.submit(row())
                journal.open(session(3), Privacy())
                assert journal.submit(row())

            # A record is opened and written to: a row, raw samples, an event.
            step = "a record opened and written to"
            recording[0] = True
            asked(step, opened_and_written_to)
            settle(step)
            # Later: the periodic work has something to do (a sync, a measure of the free space).
            step = "the periodic work of an open record"
            asked(step, later)
            settle(step)
            # The record is closed; between two sessions the thread applies the retention.
            step = "a record closed, and the work between two sessions"
            recording[0] = False
            asked(step, lambda: journal.close(closing()))
            settle(step)
            # A new session, and one more that finds it still open.
            step = "a record opened over one left open"
            recording[0] = True
            asked(step, opened_over_one_left_open)
            settle(step)
        journal.close(closing())
    finally:
        # Passed or not, this test leaves no journal thread running behind it.
        ended = journal.stop()
        drive_calls.close()
    assert ended
    # The thread really was held all along its cycle: in its own loop, in the writing of each
    # kind of value, in the periodic work and in the retention.
    functions = {(stop.file, stop.function) for stop in stops}
    for expected in (
        ("journal.py", "_run"),
        ("journal.py", "_cycle"),
        ("journal.py", "drain"),
        ("journal.py", "cycle"),
        ("journal.py", "_take"),
        ("journal.py", "_open"),
        ("journal.py", "_append"),
        ("journal.py", "_close"),
        ("journal.py", "_checkpoint"),
        ("journal.py", "_publish"),
        ("writer.py", "tick"),
        ("writer.py", "raw"),
        ("writer.py", "event"),
        ("writer.py", "sync"),
        ("writer.py", "close"),
        ("retention.py", "purge"),
        # ANH-191: taking the drive's frames, handing them to a record, and the logbook.
        ("journal.py", "_taken"),
        ("drive_tap.py", "take"),
        ("observation.py", "drain"),
        ("journal.py", "_hand_frames"),
        ("drive_tap.py", "frame"),
        ("writer.py", "frame"),
        ("journal.py", "_log"),
        ("logbook.py", "append"),
        ("logbook.py", "sync"),
    ):
        assert expected in functions, f"the journal thread was never held in {expected}"
    assert len(stops) > 1000, f"the journal thread was held {len(stops)} times only"
    # Held inside the taking itself, for both kinds of drive, at more than one instruction of
    # it: where a lock shared with whoever notes would be held.
    for file, function in (("drive_tap.py", "take"), ("observation.py", "drain")):
        inside = [stop for stop in stops if (stop.file, stop.function) == (file, function)]
        assert len(inside) > 3, f"{file}: the journal thread was held {len(inside)} times in it"
    # And it was writing: the three records are there, the first with a row for each of the
    # stops made while it was open, and closed the way the loop asked.
    found = records(journal.root)
    assert [path.name.rpartition("_")[2] for path in found] == ["local-1", "local-2", "local-3"]
    first = read(found[0])
    assert isinstance(first, Ok)
    assert first.value.manifest.end_reason == "operator_stop"
    assert len(first.value.rows) > 500, "the rows queued while the thread was held were not written"
    # The frames noted and the lines logged at each stop were taken and written as well.
    kinds = {frame.kind for frame in first.value.frames}
    assert kinds == {"read_failed", "modbus_read"}, kinds
    assert len(first.value.frames) > 1000, "a drive call and an exchange per stop"
    logged_lines = (journal.root / LOGBOOK / LOGBOOK_FILE).read_text(encoding="utf-8").splitlines()
    assert len(logged_lines) > 1000, "a line of the logbook per stop"
    assert not journal.status(clock.monotonic()).degraded


async def test_ex2_no_tick_touches_the_records_directory_or_calls_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "STALL_AFTER", Seconds(1e9))
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock, period=Seconds(0.005))
    rig, _ = make_rig(tmp_path, clock=clock, journal=journal)
    surface = rig.panel.surface
    loop_thread = threading.get_ident()
    fsyncs = count_fsync(monkeypatch)

    with watch_tree(journal.root) as touches:
        journal.start()
        writing = journal_threads()[0].ident
        assert writing is not None
        assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        await rig.tick(1.0)
        assert isinstance(
            surface.submit_manual_target(output_rpm=OutputRpm(5.0), operator=OPERATOR), Ok
        )
        for _ in range(100):
            await rig.tick(0.2)
            await rig.panel.sensor_step()
        surface.submit_end(operator=OPERATOR, reason="done")
        await rig.tick(40.0)
        assert rig.panel.runtime.state is RuntimeState.FINISHED
        wait_for(lambda: journal.status(clock.monotonic()).pending == 0, timeout=30.0)
        await rig.panel.close()  # stops the journal thread, off the loop

    assert touches.by(loop_thread) == [], "the loop's thread touched the records directory"
    assert loop_thread not in fsyncs
    assert set(fsyncs) == {writing}
    by_journal = touches.by(writing)
    assert "os.mkdir" in by_journal
    assert by_journal.count("open") > 500, "rows, events and raw blocks were all written there"
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert loaded.value.manifest.end_reason == "operator_stop"


# =========================================================================
# A disk that does not answer, under the real task loop
# =========================================================================


class StuckDisk:
    """Every write blocks until released: an SD card that stopped answering."""

    def __init__(self) -> None:
        self.release: threading.Event = threading.Event()
        self.blocked: threading.Event = threading.Event()
        self._real = writer_module.write_file

    def write(self, path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        self.blocked.set()
        self.release.wait(60.0)
        self._real(path, content, mode)


async def test_ex3_a_disk_that_never_answers_stalls_the_record_and_never_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = RealClock()
    config = config_of(BENCH_ENV)
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    journal = Journal(tmp_path / "records", clock)
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=sim, simulator=sim, release=lambda: None),
        journal=journal,
    )
    disk = StuckDisk()
    monkeypatch.setattr(writer_module, "write_file", disk.write)
    watcher = panel.hub.subscribe()
    stop = asyncio.Event()
    seen: list[float] = []
    said: list[str] = []
    asked_to_stop: list[float] = []

    async def watch() -> None:
        """What the operator's page receives: every snapshot's instant, every record message."""
        while True:
            payload = await watcher.next_payload()
            if payload.kind is PayloadKind.SNAPSHOT and payload.snapshot is not None:
                seen.append(float(payload.snapshot.at))
            if payload.kind is PayloadKind.EVENT and payload.event is not None:
                event = payload.event
                if event.kind is EventKind.RECORDING:
                    said.append(event.detail)

    async def operate(panel: LocalPanel) -> tuple[RuntimeState, Cause | None, bool]:
        surface = panel.surface
        await asyncio.sleep(0.3)
        assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        for _ in range(35):  # 7 s: longer than the 5 s after which a stall is declared
            surface.note_presence(OPERATOR)
            await asyncio.sleep(0.2)
        recorder = panel.recorder
        assert recorder is not None
        state = panel.runtime.state
        cause = recorder.status.cause
        stuck = disk.blocked.is_set()
        surface.submit_end(operator=OPERATOR, reason="done")
        for _ in range(15):
            surface.note_presence(OPERATOR)
            await asyncio.sleep(0.2)
        asked_to_stop.append(time.monotonic())
        stop.set()
        return state, cause, stuck

    watching = asyncio.create_task(watch())
    try:
        (state, cause, stuck), code = await asyncio.gather(
            operate(panel), panel.run(stop, FakeWeb(None))
        )
        left_after = time.monotonic() - asked_to_stop[0]
    finally:
        watching.cancel()
        disk.release.set()

    assert stuck, "the journal thread never reached the stuck disk"
    assert state is RuntimeState.RUNNING, "the session stopped because of the record"
    assert cause is Cause.STALLED
    assert len(said) == 1
    assert said[0].startswith("enregistrement de seance degrade : le disque ne repond plus")
    # The loop never stalled: 5 Hz throughout, and the supervisor never said otherwise.
    # 0.6 s is the supervisor's own bound (loop_stall FREEZE, which latches): the
    # check on its verdict below is the proof, this one names the gap if it fails.
    gaps = [later - earlier for earlier, later in pairwise(seen)]
    assert len(seen) > 40
    assert max(gaps) < 0.6, f"a tick came {max(gaps):.3f} s after the previous one"
    assert panel.runtime.end_reason is EndReason.OPERATOR_STOP
    standing = panel.runtime.standing
    assert standing is None or standing.rule != RULE_LOOP_STALL
    assert code == EXIT_OK
    # The console's exit waited 5 s for the record at most, then left: the thread is a daemon.
    assert "session record not finalised: the disk did not answer in time" in caplog.text
    assert STOP_TIMEOUT <= left_after < EXIT_BOUND, f"the console left after {left_after:.1f} s"
    wait_for(lambda: journal_threads() == [], timeout=30.0)


# =========================================================================
# Record reads on a dead disk, with a person on board
# =========================================================================

OCCUPIED_ENV: Final[Mapping[str, str]] = {**BENCH_ENV, "OCCUPANCY_OCCUPIED_ENABLED": "true"}

REQUESTS: Final[int] = 40
"""More than any default thread pool has workers (``min(32, cpus + 4)``)."""


async def test_ex3_record_reads_stuck_on_a_dead_disk_take_nothing_from_an_occupied_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The records disk stops answering and the page keeps asking for the list.

    The ECG treatment here is the production one: it runs on the event loop's
    default thread pool. Were the record reads on that pool too, forty stuck
    requests would take every thread of it, no heart rate would come out any
    more, and the supervisor would end the session on a stale one: a session
    stopped by the disk. They have threads of their own, two, and nothing else.
    """
    recorded = recorded_rig(tmp_path, env=OCCUPIED_ENV, treat=treat_off_loop)
    exporter = recorded.rig.panel.services.records
    assert exporter is not None
    answer = threading.Event()

    def never(_root: Path) -> tuple[RecordEntry, ...]:
        answer.wait(120.0)
        return ()

    monkeypatch.setattr(export_module, "listing", never)
    runtime = recorded.rig.panel.runtime
    seen: list[TelemetrySnapshot] = []
    try:
        async with recorded.rig.http() as session:
            asking = [asyncio.create_task(session.get("/api/records")) for _ in range(REQUESTS)]
            for _ in range(200):
                if sum(task.done() for task in asking) == REQUESTS - 2:
                    break
                await asyncio.sleep(0.01)
            refused = [task.result().status_code for task in asking if task.done()]
            assert refused == [503] * (REQUESTS - 2), "all but two were told at once"
            assert exporter.io.active == 2

            # A heart rate the runtime trusts, from the simulated ECG through the real
            # treatment, on the pool the stuck reads are NOT on.
            for _ in range(120):
                await asyncio.wait_for(recorded.tick(1.0), 30.0)
                if runtime.snapshot().live_bpm is not None:
                    break
            assert runtime.snapshot().live_bpm is not None, "no heart rate came out"

            await attest(session)
            started = await session.post(
                "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
            )
            assert started.status_code == 202, started.text
            await asyncio.wait_for(recorded.tick(1.0), 30.0)
            assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
            await set_target(session, 3.0)
            for _ in range(60):
                await asyncio.wait_for(recorded.tick(1.0), 30.0)
                seen.append(runtime.snapshot())

            assert recorded.state() is RuntimeState.RUNNING
            assert all(snapshot.live_bpm is not None for snapshot in seen), "a stale heart rate"
            assert [s.safety.rule for s in seen if s.safety is not None] == []
            assert seen[-1].measured.motor_rpm > 55, "the session went on turning"
            assert exporter.io.active == 2, "the two record threads are still on the dead disk"
            assert not recorded.degraded(), "the record itself was written all along"

            # And the console still stops, with those two threads still stuck.
            detail = await asyncio.wait_for(recorded.rig.panel.close(), 20.0)
            for task in asking:
                task.cancel()
            await asyncio.gather(*asking, return_exceptions=True)
    finally:
        answer.set()
    assert runtime.end_reason is EndReason.SHUTDOWN
    recording = recorded.recording()
    assert recording.warnings == ()
    assert recording.manifest.occupancy == "occupied"
    observation = recording.manifest.end_observation
    assert observation is not None
    assert observation.shutdown_detail == detail
    assert await recorded.rig.left_stopped() == ""
