"""ANH-215: the frames of a session wait for its record, and what is lost is counted.

The journal thread does not choose when it comes. It can pass while a session
is being armed, before the record of that session exists; it can take the
frames just before the loop runs the last tick of a session and queues its
closing. Each frame still goes to the record of its session:

* what began after the last session closed **waits, from one cycle to the
  next**, for the opening that claims it. It is let go only when it is no
  session's: it began before the arming of the record that opens, or before a
  control tick the loop has since reported with no record opened;
* the tap is **taken again when a record is closed**: the last frames of its
  session were noted before its closing was queued;
* what waits is **bounded**, and what the bound or the tap costs is **counted**
  for every record whose session it can have begun in (two when nothing says
  which of two lost it), and for none when it began in no session.

On the journal alone first, with the instants chosen by hand. Then on the REAL
console in simulation: the two replays of the review (the journal passing
during an arming, the last tick run while the journal takes the frames), with
the thread on time and with the thread away, and the same pair of sessions
replayed with the journal passing inside every call made to the drive, one
after the other. Every call the drive received is compared with what the
records hold: none lost, none twice, none in the record of another session.
"""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import httpx
import pytest

from src import local_panel
from src.clock import Clock, ManualClock
from src.motor.drive import DriveBackend
from src.record import session as session_module
from src.record.codec import Privacy
from src.record.drive_tap import FrameSource, Taken, Tapped, tap_drive
from src.record.journal import Cause, Gap, Journal, Limits
from src.record.reader import read
from src.record.retention import records
from src.record.schema import EventKind
from src.result import Ok
from src.training.runtime import RuntimeState
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_console_support import RecordedRig
from tests.record_journal_support import closing, record_of, session
from tests.test_failure_rig import TICK
from tests.test_record_drive_frames import Waiting, frames_in, frames_of, started_at
from tests.test_record_frame_sessions import (
    Told,
    ask_for_a_bench_session,
    ask_to_stop,
    frames_read,
    tick_to_the_end,
    told_rig,
)

# =========================================================================
# On the journal alone
# =========================================================================


def early_clock() -> ManualClock:
    """A console built at 9 s: its sessions start at 10 s, as ``record_support`` writes them."""
    return ManualClock(Monotonic(9.0), UnixMillis(1_791_195_000_000))


def listening(tmp_path: Path, limits: Limits | None = None) -> tuple[Journal, Waiting, ManualClock]:
    clock = early_clock()
    root = tmp_path / "records"
    journal = Journal(root, clock) if limits is None else Journal(root, clock, limits=limits)
    waiting = Waiting()
    journal.listen(waiting)
    return journal, waiting, clock


def warnings_of(path: Path) -> list[str]:
    loaded = read(path)
    assert isinstance(loaded, Ok), loaded
    return [e.detail for e in loaded.value.events if e.kind is EventKind.WARNING]


def test_the_start_of_an_arming_taken_before_its_record_opens_waits_for_it(tmp_path: Path) -> None:
    """The thread passes during an arming that lasts longer than one of its cycles."""
    journal, waiting, clock = listening(tmp_path)
    waiting.add(9.5, "open")
    waiting.add(9.6, "read_limits")
    journal.drain()  # no record yet: the session is still being armed
    waiting.add(9.7, "command")
    journal.drain()  # and still
    waiting.add(9.9, "command")
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.drain()
    assert frames_of(journal) == [
        (-0.5, "open"),
        (-0.4, "read_limits"),
        (-0.3, "command"),
        (-0.1, "command"),
    ]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0)


def test_what_began_before_a_tick_the_loop_reported_with_no_record_opened_is_no_sessions(
    tmp_path: Path,
) -> None:
    """A start that is refused: its exchanges wait, then the loop's next tick disowns them."""
    journal, waiting, clock = listening(tmp_path)
    waiting.add(9.2)  # the console at rest
    waiting.add(9.4, "open")  # a start is asked for at 9.3, and refused
    journal.drain()  # kept: nothing says yet that no record will claim them
    journal.dispatched(Monotonic(9.6))  # the loop: that tick is dispatched, nothing was armed
    waiting.add(9.7)  # at rest again, in that tick
    journal.drain()
    # Had they still been waiting, a record that claims everything since 9.1 would take them.
    journal.open(session(), Privacy(), since=Monotonic(9.1))
    journal.drain()
    assert frames_of(journal) == [(-0.3, "read_status")]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0), "not a loss"


class Overtaken:
    """A frame source during whose take the loop runs on.

    ``then`` happens once, just after the frames were taken: before the journal
    thread has read its queue, as when the loop gets its turn in between.
    """

    def __init__(self, inner: FrameSource) -> None:
        self.inner: FrameSource = inner
        self.then: Callable[[], None] | None = None

    def __call__(self) -> Taken:
        taken = self.inner()
        then, self.then = self.then, None
        if then is not None:
            then()
        return taken


def test_the_last_frames_noted_while_the_thread_took_the_others_are_in_the_record_it_closes(
    tmp_path: Path,
) -> None:
    """The loop runs the last tick of a session between the take and the read of the queue."""
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    source = Overtaken(waiting)
    journal.listen(source)
    journal.open(session(), Privacy())
    journal.drain()
    waiting.add(10.4, "speed")

    def last_tick() -> None:
        waiting.add(11.0, "speed")
        waiting.add(11.02)
        journal.close(closing(until=Monotonic(11.05)))
        waiting.add(11.2)  # and the console is at rest already

    source.then = last_tick
    journal.drain()
    assert frames_in(record_of(journal)) == [
        (0.4, "speed"),
        (1.0, "speed"),
        (1.02, "read_status"),
    ]


def test_past_the_bound_the_oldest_are_let_go_and_the_record_they_began_in_says_so(
    tmp_path: Path,
) -> None:
    journal, waiting, clock = listening(tmp_path, Limits(frames=3))
    for index in range(5):  # an arming that began at 9.5, and no record for it yet
        waiting.add(9.5 + index / 10, "command")
    journal.drain()
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.drain()

    assert frames_of(journal) == [(-0.3, "command"), (-0.2, "command"), (-0.1, "command")], (
        "the newest are kept: they are the ones a record is about to claim"
    )
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.cause, status.dropped) == (True, Cause.QUEUE_FULL, 2)
    journal.close(closing())
    journal.drain()
    assert warnings_of(record_of(journal)) == [
        "record_degraded: dropped=0 failures=0 drive_frames_lost=2"
    ]


def test_what_the_bound_cost_before_an_arming_began_is_not_counted_for_its_record(
    tmp_path: Path,
) -> None:
    journal, waiting, clock = listening(tmp_path, Limits(frames=3))
    for index in range(5):  # nothing is being armed: 9.1 to 9.5
        waiting.add(9.1 + index / 10)
    journal.drain()
    waiting.add(9.8, "open")
    journal.open(session(), Privacy(), since=Monotonic(9.7))
    journal.drain()
    assert frames_of(journal) == [(-0.2, "open")]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped) == (False, 0), "they began in no session"


def test_what_the_bound_cost_in_an_arming_that_was_refused_is_no_loss_either(
    tmp_path: Path,
) -> None:
    journal, waiting, clock = listening(tmp_path, Limits(frames=3))
    for index in range(5):
        waiting.add(9.1 + index / 10, "command")
    journal.drain()
    journal.dispatched(Monotonic(9.6))  # that start was refused: the loop went on
    journal.drain()
    # A session whose arming would overlap what was let go, had it still been counted.
    journal.open(session(), Privacy(), since=Monotonic(9.0))
    journal.drain()
    status = journal.status(clock.monotonic())
    assert (frames_of(journal), status.dropped) == ([], 0)


def test_what_the_bound_cost_at_a_closing_from_before_that_arming_is_not_counted_either(
    tmp_path: Path,
) -> None:
    """The room runs out when the tap is taken again for a closing: the oldest go, the polling."""
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock, limits=Limits(frames=3))
    waiting = Waiting()
    source = Overtaken(waiting)
    journal.listen(source)
    waiting.add(9.1)  # at rest, before the arming
    waiting.add(9.2)
    waiting.add(10.2, "speed")
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.close(closing(until=Monotonic(10.5)))
    source.then = lambda: waiting.add(10.3, "speed")  # noted while the thread took the others
    journal.drain()
    assert frames_in(record_of(journal)) == [(0.2, "speed"), (0.3, "speed")]
    assert warnings_of(record_of(journal)) == [], "what was let go began before its arming"


def test_two_gaps_are_one_over_the_stretch_that_covers_both() -> None:
    first = Gap(3, Monotonic(12.0), Monotonic(14.0))
    second = Gap(2, Monotonic(11.0), Monotonic(13.0))
    assert first.joined(second) == Gap(5, Monotonic(11.0), Monotonic(14.0))
    assert second.joined(first) == Gap(5, Monotonic(11.0), Monotonic(14.0))


def test_what_the_tap_refused_while_two_sessions_waited_is_counted_for_both(
    tmp_path: Path,
) -> None:
    """The tap filled during the first session: nothing says which of the two lost what."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy())
    waiting.add(10.5)  # the last frame the tap could keep
    waiting.lost = 63
    journal.close(closing(until=Monotonic(12.0)))
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.5))
    clock.advance(Seconds(12.0))  # the thread comes back at 21 s
    journal.drain()
    journal.close(closing(until=Monotonic(21.5)))
    journal.drain()

    first, second = records(journal.root)
    assert frames_in(first) == [(0.5, "read_status")]
    assert frames_in(second) == []
    said = "record_degraded: dropped=0 failures=0 drive_frames_lost=63"
    assert warnings_of(first) == [said]
    assert warnings_of(second) == [said]


def test_what_the_tap_refused_after_a_session_ended_is_counted_for_the_next_only(
    tmp_path: Path,
) -> None:
    """The last frame it kept began after the first session ended: only the second can have lost."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy())
    waiting.add(10.5)
    journal.close(closing(until=Monotonic(12.0)))
    waiting.add(19.6, "open")  # the tap filled in the arming of the second
    waiting.lost = 40
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.5))
    clock.advance(Seconds(12.0))
    journal.drain()
    journal.close(closing(until=Monotonic(21.5)))
    journal.drain()

    first, second = records(journal.root)
    assert warnings_of(first) == []
    assert warnings_of(second) == ["record_degraded: dropped=0 failures=0 drive_frames_lost=40"]
    assert frames_in(second) == [(-0.4, "open")]


@pytest.mark.parametrize(
    ("ended", "counted"),
    [(10.9, 0), (11.5, 7)],
    ids=["a session that ended before the previous take", "one that ended after it"],
)
def test_what_the_tap_refused_with_nothing_kept_began_after_the_previous_take(
    tmp_path: Path, ended: float, counted: int
) -> None:
    """A take that brings a count and no frame: all it says is "since I was last taken"."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy())
    clock.advance(Seconds(2.0))
    journal.drain()  # taken at 11 s
    journal.close(closing(until=Monotonic(ended)))
    waiting.lost = 7  # refused between 11 s and the next take, at 12 s
    clock.advance(Seconds(1.0))
    journal.drain()
    said = [f"record_degraded: dropped=0 failures=0 drive_frames_lost={counted}"]
    assert warnings_of(record_of(journal)) == (said if counted else [])


# =========================================================================
# The REAL console in simulation
# =========================================================================

REST: Final[float] = 0.6
"""Seconds the console is left at rest between two sessions: three ticks."""


def kinds_in(path: Path) -> list[str]:
    return [kind for _, kind in frames_read(path)]


NONE: Final[frozenset[Path]] = frozenset()


def in_order(recorded: RecordedRig, known: frozenset[Path] = NONE) -> list[Path]:
    """The records by the instant their session started on the console's clock.

    Their names carry the UTC second of the start and a random reference: two
    sessions started within one second do not sort by name. ``known``: records
    to leave out.
    """
    started: list[tuple[float, Path]] = []
    for path in recorded.records():
        if path in known:
            continue
        loaded = read(path)
        assert isinstance(loaded, Ok), loaded
        started.append((loaded.value.manifest.clocks.monotonic_start, path))
    return [path for _, path in sorted(started)]


def nothing_counted(recorded: RecordedRig) -> bool:
    """Whether the journal, asked now, counts nothing lost or refused for its last record."""
    status = recorded.journal.status(recorded.clock.monotonic())
    return (status.cause, status.dropped, status.failures) == (None, 0, 0)


async def ticks(recorded: RecordedRig, seconds: float, *, away: bool) -> None:
    """Control ticks, the journal after each unless ``away``.

    Without the publication of the sensors that ``RecordedRig.tick`` adds once
    a second: it asks nothing of the drive, and it is most of what a tick of
    that rig costs.
    """
    for _ in range(round(seconds / TICK)):
        await recorded.rig.tick(float(TICK))
        if not away:
            recorded.drain()


async def to_the_end(recorded: RecordedRig, *, away: bool) -> None:
    """Tick until the tick that ends the session, the journal BEFORE each tick unless ``away``.

    Never after the last: the closing, and the frames of the tick that queued
    it, are left waiting.
    """
    for _ in range(600):
        if not away:
            recorded.drain()
        await recorded.rig.tick(float(TICK))
        if not recorded.recorder.recording:
            return
    raise AssertionError("the session did not end")


async def a_session(
    recorded: RecordedRig,
    drive: Told,
    http: httpx.AsyncClient,
    part: str,
    *,
    away: bool,
    lasting: float = 0.6,
) -> None:
    """One bench session, from the request to the tick that ends it, as ``part``.

    Armed, left at standstill for ``lasting`` seconds, then stopped.
    """
    drive.part = part
    await ask_for_a_bench_session(http)
    await ticks(recorded, lasting, away=away)
    assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
    await ask_to_stop(http)
    await to_the_end(recorded, away=away)


async def a_pair(
    recorded: RecordedRig, drive: Told, http: httpx.AsyncClient, name: str, *, away: bool
) -> None:
    """Two sessions, the console at rest between them and after them, the journal last."""
    await a_session(recorded, drive, http, f"{name}: first", away=away)
    drive.part = f"{name}: at rest"
    await ticks(recorded, REST, away=away)
    await a_session(recorded, drive, http, f"{name}: second", away=away)
    drive.part = f"{name}: after"
    await ticks(recorded, REST, away=away)
    recorded.drain()
    recorded.drain()


def exact(recorded: RecordedRig, drive: Told, name: str, known: frozenset[Path]) -> str:
    """What is wrong with the two records of the pair ``name``; nothing when they are exact.

    Exact: each holds the calls made to the drive in its session, all of them,
    once, in order, and nothing else. ``known``: the records that were there
    before the pair, which are not read again.
    """
    first, second = (path for path in in_order(recorded, known))
    wrong: list[str] = []
    for label, path in ((f"{name}: first", first), (f"{name}: second", second)):
        loaded = read(path)
        assert isinstance(loaded, Ok), loaded
        held = [frame.kind for frame in loaded.value.frames]
        made = drive.made_in(label)
        if held != made or loaded.value.warnings:
            lost = Counter(made) - Counter(held)
            extra = Counter(held) - Counter(made)
            wrong.append(
                f"{label}: {len(made)} calls, {len(held)} frames, missing {dict(lost)}, "
                f"not its own {dict(extra)}, {loaded.value.warnings}"
            )
        degraded = [e.detail for e in loaded.value.events if e.kind is EventKind.WARNING]
        if degraded:
            wrong.append(f"{label}: {degraded}")
    return "; ".join(wrong)


async def test_l1_the_start_of_an_arming_is_in_its_record_when_the_journal_passes_during_it(
    tmp_path: Path,
) -> None:
    """The thread is on time, and one of its cycles falls inside the fourth exchange."""
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await recorded.tick(REST)
        drive.pass_inside(4, recorded.drain)
        await a_session(recorded, drive, http, "session", away=False)
        recorded.drain()
    assert drive.passed(), "the journal did pass inside that call"
    made = drive.made_in("session")
    assert made[:4] == ["open", "read_status", "read_limits", "speed"]
    assert kinds_in(recorded.only_record()) == made
    assert recorded.recording().warnings == ()
    assert nothing_counted(recorded)


async def test_l1b_away_since_the_previous_session_the_journal_passes_during_the_next_arming(
    tmp_path: Path,
) -> None:
    """The replay of the review: base wrote these three at the end of the first record."""
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "pair: first"
        await ask_for_a_bench_session(http)
        await recorded.tick(0.6)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=False)
        drive.part = "pair: at rest"
        await recorded.tick(REST, drain=False)
        # Away since the last ticks of the first session; one cycle, inside the fourth
        # exchange of the arming of the second; then on time again.
        drive.pass_inside(4, recorded.drain)
        await a_session(recorded, drive, http, "pair: second", away=False)
        recorded.drain()
    assert drive.passed()
    assert exact(recorded, drive, "pair", NONE) == ""
    second = kinds_in(in_order(recorded)[-1])
    assert second[:3] == ["open", "read_status", "read_limits"]
    assert nothing_counted(recorded)


class Held:
    """A frame source that can stop the journal's cycle right after its take.

    Armed with ``hold``, the next take is made, then waits for ``go``: the
    cycle has taken the frames and has not read its queue yet. Once only: the
    takes that follow in that cycle go through.
    """

    def __init__(self, inner: FrameSource) -> None:
        self.inner: FrameSource = inner
        self.hold: bool = False
        self.taken: threading.Event = threading.Event()
        self.go: threading.Event = threading.Event()

    def __call__(self) -> Taken:
        taken = self.inner()
        if self.hold:
            self.hold = False
            self.taken.set()
            self.go.wait(30.0)
        return taken


def held_tap(monkeypatch: pytest.MonkeyPatch) -> list[Held]:
    """Make the console's tap one that can be held; the list receives it when the rig is built."""
    made: list[Held] = []

    def tapping(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        tapped = tap_drive(backend, clock, sdk_frames=sdk_frames)
        held = Held(tapped.source)
        made.append(held)
        return replace(tapped, source=held)

    monkeypatch.setattr(local_panel, "tap_drive", tapping)
    return made


async def a_cycle_overtaken_by(
    recorded: RecordedRig, held: Held, loop_runs: Callable[[], Awaitable[None]]
) -> None:
    """One cycle of the journal, on a thread of its own as in the console.

    Once it has taken the frames, and before it reads its queue, the loop
    runs ``loop_runs`` to its end.
    """
    held.taken.clear()
    held.go.clear()
    held.hold = True
    cycle = threading.Thread(target=recorded.drain, name="journal-cycle-of-a-test")
    cycle.start()
    reached = await asyncio.to_thread(held.taken.wait, 30.0)
    assert reached, "the cycle never took the frames"
    await loop_runs()
    held.go.set()
    await asyncio.to_thread(cycle.join, 30.0)
    assert not cycle.is_alive()


async def test_l2_the_last_tick_run_while_the_journal_takes_the_frames_is_in_its_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    taps = held_tap(monkeypatch)
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "session"
        await ask_for_a_bench_session(http)
        await recorded.tick(0.6)
        await ask_to_stop(http)

        async def to_the_end() -> None:
            await tick_to_the_end(recorded, away=True)

        await a_cycle_overtaken_by(recorded, taps[0], to_the_end)
        drive.part = "after"
        await recorded.tick(REST)
    assert kinds_in(recorded.only_record()) == drive.made_in("session")
    assert recorded.recording().warnings == ()
    assert nothing_counted(recorded)


async def test_l2b_then_a_second_session_is_armed_before_the_journal_comes_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay of the review: base wrote the last frames of the first in the second record."""
    taps = held_tap(monkeypatch)
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "pair: first"
        await ask_for_a_bench_session(http)
        await recorded.tick(0.6)
        await ask_to_stop(http)

        async def to_the_end() -> None:
            await tick_to_the_end(recorded, away=True)

        await a_cycle_overtaken_by(recorded, taps[0], to_the_end)
        drive.part = "pair: at rest"
        await recorded.tick(REST, drain=False)
        await a_session(recorded, drive, http, "pair: second", away=True)
        drive.part = "pair: after"
        await recorded.tick(REST, drain=False)
        recorded.drain()
    assert exact(recorded, drive, "pair", NONE) == ""
    assert nothing_counted(recorded)


@pytest.mark.parametrize("away", [False, True], ids=["the thread on time", "the thread away"])
async def test_the_journal_passing_inside_any_call_of_two_sessions_costs_no_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, away: bool
) -> None:
    """The sweep of the review: the same pair of sessions, one pass inside each call in turn.

    ``away``: the journal comes at that one moment and at the end of the pair,
    and at no other. A measurement of the free space that old would refuse the
    second start: the arming gate is told it is fresh, it is not what is judged.
    """
    monkeypatch.setattr(session_module, "STORAGE_STALE_AFTER", Seconds(1e9))
    recorded, drive = told_rig(tmp_path)
    wrong: list[str] = []
    async with recorded.rig.http() as http:
        await a_pair(recorded, drive, http, "reference", away=away)
        assert exact(recorded, drive, "reference", NONE) == ""
        calls = len(drive.calls)
        assert calls > 40, "two sessions, each armed and brought to its end, and the rest"
        for position in range(1, calls + 1):
            name = f"pass {position}"
            known = frozenset(recorded.records())
            drive.pass_inside(position, recorded.drain)
            await a_pair(recorded, drive, http, name, away=away)
            assert drive.passed(), f"{name}: the pair made fewer calls than the reference"
            found = exact(recorded, drive, name, known)
            if found:
                wrong.append(f"{name} (inside a '{drive.calls[drive.at - 1][1]}'): {found}")
    assert wrong == []
    assert nothing_counted(recorded)


async def test_on_a_clock_that_stands_still_in_a_tick_the_last_exchanges_are_in_the_record(
    tmp_path: Path,
) -> None:
    """Every exchange of the last tick begins at the very instant the session ends."""
    recorded, drive = told_rig(tmp_path, answers_take_time=False)
    async with recorded.rig.http() as http:
        await a_session(recorded, drive, http, "session", away=False)
        drive.part = "after"
        await recorded.tick(REST)
    made = drive.made_in("session")
    assert kinds_in(recorded.only_record()) == made
    recording = recorded.recording()
    observation = recording.manifest.end_observation
    assert observation is not None
    last_tick = [frame.kind for frame in recording.frames if frame.t == observation.t]
    assert last_tick == made[-len(last_tick) :]
    assert last_tick, "the last tick did talk to the drive, at the instant the record ends on"


async def test_a_session_armed_at_the_instant_the_previous_one_ended_costs_and_doubles_nothing(
    tmp_path: Path,
) -> None:
    """The clock stands still from the last tick of one session through the arming of the next.

    Nothing then tells the last exchanges of the first from the arming of the
    second: they began at one instant. Whichever record takes them, each is
    written once, in one record, and none is lost.
    """
    recorded, drive = told_rig(tmp_path, answers_take_time=False)
    async with recorded.rig.http() as http:
        drive.part = "first"
        await ask_for_a_bench_session(http)
        await recorded.tick(0.6)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=False)
        ended_at = recorded.clock.monotonic()

        drive.part = "second"
        await ask_for_a_bench_session(http)
        await recorded.rig.panel.control_step()  # the next tick, the clock not moved
        assert recorded.clock.monotonic() == ended_at
        assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
        recorded.drain()  # one cycle: the first is closed, the second opened
        await recorded.tick(0.4)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=False)
        drive.part = "after"
        await recorded.tick(REST)

    first, second = in_order(recorded)
    held = Counter(kinds_in(first)) + Counter(kinds_in(second))
    made = Counter(drive.made_in("first")) + Counter(drive.made_in("second"))
    assert held == made, "each call of the two sessions is written once, and none is lost"
    assert len(kinds_in(first)) >= len(drive.made_in("first"))
    for path in (first, second):
        loaded = read(path)
        assert isinstance(loaded, Ok), loaded
        assert loaded.value.warnings == ()


@pytest.mark.parametrize("away", [False, True], ids=["the thread on time", "the thread away"])
async def test_the_exchanges_of_a_start_that_is_refused_are_in_no_record_and_are_no_loss(
    tmp_path: Path, away: bool
) -> None:
    """An arming that reaches the drive and is refused, then a session that is not.

    The exchanges of the refused arming wait like those of any arming: nothing
    tells them apart until the loop goes on with no record opened. They are
    then no session's, like the polling around them, and the session armed
    next does not get them.
    """
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await ticks(recorded, REST, away=away)

        drive.part = "refused"
        drive.limits_unreadable = True
        await ask_for_a_bench_session(http)
        await ticks(recorded, 0.4, away=away)
        assert recorded.state() is not RuntimeState.RUNNING
        assert recorded.rig.refusals(), "that start was refused"

        await a_session(recorded, drive, http, "session", away=away)
        drive.part = "after"
        await ticks(recorded, REST, away=away)
        recorded.drain()
    assert drive.made_in("refused")[:3] == ["open", "read_status", "read_limits"], (
        "the refused start did reach the drive"
    )
    assert kinds_in(recorded.only_record()) == drive.made_in("session")
    assert recorded.recording().warnings == ()
    assert nothing_counted(recorded)


async def test_what_a_small_tap_refused_while_two_sessions_waited_is_said_in_each_for_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third limit of the review: the overflow was counted for the first only, or for none.

    ANH-215 had both records say all of it, an upper bound, because the tap
    says only how many it refused. Since ANH-222 the console reads that count
    when an arming begins and when the session ends: each record says what
    its own session was refused, and what the console was refused at rest
    between the two is counted for neither.
    """
    monkeypatch.setattr(session_module, "STORAGE_STALE_AFTER", Seconds(1e9))

    def small(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        return tap_drive(backend, clock, capacity=40, sdk_frames=sdk_frames)

    monkeypatch.setattr(local_panel, "tap_drive", small)
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        await a_session(recorded, drive, http, "first", away=True, lasting=6.0)
        drive.part = "at rest"
        await recorded.tick(REST, drain=False)
        await a_session(recorded, drive, http, "second", away=True)
        recorded.drain()

    first, second = in_order(recorded)
    made = drive.made_in("first")
    assert len(made) > 40
    assert kinds_in(first) == made[:40], "the tap kept the oldest forty: all of the first session"
    assert kinds_in(second) == []
    said = "record_degraded: dropped=0 failures=0 drive_frames_lost="
    assert warnings_of(first) == [f"{said}{len(made) - 40}"]
    assert warnings_of(second) == [f"{said}{len(drive.made_in('second'))}"]
    at_rest = len(drive.calls) - len(made) - len(drive.made_in("second"))
    assert at_rest > 0, "the console was refused frames at rest too: they are in neither count"


async def test_what_was_let_go_while_the_console_was_at_rest_is_not_counted_with_a_later_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A console long at rest, then a session of which the tap refuses frames.

    The console polls its drive at rest for longer than the journal thread has
    room to keep. The loop reports its ticks, so that polling is known to be no
    session's as it goes, and is forgotten: it never fills the room, and none
    of it is let go for lack of it. What the session then loses is counted
    alone: its record says how many frames the tap refused, and not one more.
    """
    monkeypatch.setattr(session_module, "STORAGE_STALE_AFTER", Seconds(1e9))

    def small(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        return tap_drive(backend, clock, capacity=40, sdk_frames=sdk_frames)

    monkeypatch.setattr(local_panel, "tap_drive", small)
    recorded, drive = told_rig(tmp_path, limits=Limits(frames=46))
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await ticks(recorded, 120.0, away=False)
        assert len(drive.calls) > 2 * 46, "far more than there is room for"
        await a_session(recorded, drive, http, "session", away=True, lasting=6.0)
        recorded.drain()

    made = drive.made_in("session")
    assert len(made) > 40
    assert kinds_in(recorded.only_record()) == made[:40]
    refused = len(made) - 40
    assert warnings_of(recorded.only_record()) == [
        f"record_degraded: dropped=0 failures=0 drive_frames_lost={refused}"
    ]
