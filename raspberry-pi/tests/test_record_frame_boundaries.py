"""ANH-222: the boundaries of the black box, each held by a test that names it.

The review of ANH-215 changed forty lines of the journal one at a time and ran
the tests: ten changes passed. Each of them is one boundary of the rule "a
frame of the drive goes to the record of the session it began in, and what is
lost is counted for that session". Here is one named test per boundary, each
of which fails when its line is changed, first on the journal alone with the
instants chosen by hand, then on the REAL console in simulation.

* **the instant of the tick is read before the queue is counted** (EX-1).
  Read after that, it can be the instant of the very tick that armed a
  session whose opening this cycle did not count: the first exchanges of that
  arming, taken a moment earlier, are then let go as no session's, and
  counted for nobody. The replay of the review: the thread is held while it
  closes the record of one session and the next session is armed meanwhile.
  (The cycle reads it first of all, before the tap. Between the tap and the
  count of the queue would do as well: every opening queued before the tick
  that instant is of is then counted, and this cycle opens its record);
* **the stretch of what the bound let go runs from its oldest frame to its
  newest** (EX-2, two changes): a session armed, or ended, in the middle of
  that stretch counts it;
* **a new loss joins the one that waits** (EX-2): it does not replace it;
* **five equalities** (EX-2): a frame, or a loss, at the very instant of the
  tick the loop reported still waits for a record; a loss at the very instant
  a session began, or ended, is that session's; and one that ended with a
  session is not kept for the next.

The tenth change is no boundary: without the first line of
``Scribe._forget`` (``if self._writer is not None: return``) nothing changes.
``_forget`` has one caller, ``Scribe.cycle``, which calls it right after
``_hand_frames(None)``. With a record open, that call has just emptied what
waited (``self._waiting = ()``) and settled every loss (``until is None``
clears both); writing the frames after that touches neither. So ``_forget``
then filters an empty tuple and finds nothing to forget, with the line or
without it: no record, no count and no status can differ. The line is kept
because it states what the method is for.

Then what ANH-222 changed:

* **the count of the frames the tap refused is exact** (EX-3). The console
  reads how many the tap has refused when an arming begins and when the
  session ends; the record counts the difference, and no longer what a
  console at rest was refused before it or after it;
* **a fault reset between two sessions** is in neither record (EX-4);
* **a record that is replaced says what it lost** before it is closed, and
  **the exchange during which a tick raises is noted** (EX-5);
* **a recorder that fails leaves the calls to the drive as they were** (EX-6).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import override

import pytest

from src import local_panel
from src.clock import Clock, ManualClock
from src.motor.drive import (
    CMD_LOGICAL,
    LFRD_LOGICAL,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveStatus,
)
from src.motor.simulated import SimulatedDrive
from src.record import session as session_module
from src.record.codec import Privacy
from src.record.drive_tap import CallTap, FrameSource, Taken, Tapped, tap_drive
from src.record.journal import (
    DEFAULT_LIMITS,
    Cause,
    Closing,
    Item,
    Journal,
    Limits,
    Opening,
    Progress,
    Scribe,
)
from src.record.reader import read
from src.record.retention import records
from src.record.session import SessionRecorder, SessionRequest
from src.result import Ok, Result
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Metres, Monotonic, MotorRpm, Seconds, UnixMillis
from tests.record_console_support import RecordedRig, set_target
from tests.record_journal_support import closing, record_of, session
from tests.record_support import row
from tests.test_failure_rig import OPERATOR, PROGRAMME_ENV, TICK, start_programme
from tests.test_record_drive_frames import (
    Native,
    Scripted,
    Waiting,
    exchange,
    frames_in,
    frames_of,
    started_at,
)
from tests.test_record_frame_carry import (
    NONE,
    REST,
    Overtaken,
    a_session,
    early_clock,
    exact,
    in_order,
    kinds_in,
    listening,
    nothing_counted,
    ticks,
    to_the_end,
    warnings_of,
)
from tests.test_record_frame_sessions import (
    STAMP,
    Told,
    ask_for_a_bench_session,
    ask_to_stop,
    degradations,
    frames_read,
    tick_to_the_end,
    told_rig,
)

# =========================================================================
# EX-1 on the journal alone: when the instant of the tick is read
# =========================================================================


@pytest.mark.parametrize("first", [0, 1, 7, 14, 15, 19])
def test_a_window_never_reports_fewer_shed_frames_than_it_actually_lost(
    tmp_path: Path, first: int
) -> None:
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock, limits=Limits(frames=5))
    waiting = Waiting()
    journal.listen(waiting)
    instants = [10 + index / 5 for index in range(20)]
    for at in instants:
        waiting.add(at)
    journal.drain()
    boundary = Monotonic(instants[first])
    journal.open(started_at(1, 14), Privacy(), since=boundary)
    journal.drain()
    recording = read(record_of(journal))
    assert isinstance(recording, Ok)
    assert [frame.t for frame in recording.value.frames] == [
        round(at - 14, 3) for at in instants[-5:] if at >= boundary
    ]
    actual = sum(at >= boundary for at in instants[:-5])
    announced = journal.status(clock.monotonic()).dropped
    assert actual <= announced <= 15
    assert announced == (15 if first < 15 else 0)


def test_ex1_an_arming_reported_while_the_thread_closes_a_record_keeps_its_first_frames(
    tmp_path: Path,
) -> None:
    """The cycle took the start of an arming; the tick that armed is reported before it ends.

    What the cycle goes by is the tick reported BEFORE it took anything: the
    one at rest, from before that arming. The frames it took then wait for
    the record the next cycle opens.
    """
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    source = Overtaken(waiting)
    journal.listen(source)
    journal.open(started_at(1, 10.0), Privacy())
    journal.drain()
    waiting.add(10.4, "speed")
    journal.close(closing(until=Monotonic(10.5)))
    journal.dispatched(Monotonic(10.6))  # at rest: that tick is reported, nothing was armed
    waiting.add(10.62)  # its polling
    # The next tick arms a second session. The thread comes inside its fourth exchange.
    waiting.add(10.81, "open")
    waiting.add(10.82)
    waiting.add(10.83, "read_limits")

    def the_arming_goes_on() -> None:
        """Meanwhile, on the loop: the arming ends, its record is queued, its tick reported."""
        waiting.add(10.84, "speed")
        journal.open(started_at(2, 10.9), Privacy(), since=Monotonic(10.8))
        journal.dispatched(Monotonic(10.9))

    def at_the_take_of_the_closing() -> None:
        source.then = the_arming_goes_on

    source.then = at_the_take_of_the_closing
    journal.drain()  # closes the first record; the second is queued behind what it counted
    journal.drain()

    first, second = records(journal.root)
    assert frames_in(first) == [(0.4, "speed")]
    assert frames_read(second) == [
        (-0.09, "open"),
        (-0.08, "read_status"),
        (-0.07, "read_limits"),
        (-0.06, "speed"),
    ], "the three exchanges taken before the record was queued, and the fourth"
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0)


class CountedThen(deque[Item]):
    """A queue right after whose count the loop runs on, once.

    ``then`` happens between the instruction by which the journal thread
    counts what is queued and its next one: what it queues is not counted.
    """

    then: Callable[[], None] | None = None

    @override
    def __len__(self) -> int:
        size = super().__len__()
        then, self.then = self.then, None
        if then is not None:
            then()
        return size


def test_ex1_the_instant_of_the_tick_is_read_before_the_queue_is_counted(tmp_path: Path) -> None:
    """Read one instruction after that count, it can already be the tick of an uncounted arming.

    On the journal thread's own object, with a queue that lets the loop run
    at that very place: the arming ends, its record is queued, its tick is
    reported. The cycle goes by the tick it read BEFORE, keeps the three
    exchanges it took, and the next cycle writes them.
    """
    clock = early_clock()
    reported = [Monotonic(10.6)]  # the console at rest: that tick is dispatched
    published: list[Progress] = []
    scribe = Scribe(
        tmp_path / "records",
        clock,
        None,
        published.append,
        dispatched=lambda: reported[0],
        room=DEFAULT_LIMITS.frames,
    )
    waiting = Waiting()
    scribe.listen(waiting)
    queue = CountedThen()
    waiting.add(10.62)  # its polling
    waiting.add(10.81, "open")  # the next tick arms a session: three exchanges so far
    waiting.add(10.82)
    waiting.add(10.83, "read_limits")

    def the_arming_ends() -> None:
        waiting.add(10.84, "speed")
        queue.append(Opening(1, started_at(1, 10.9), Privacy(), Monotonic(10.8)))
        reported[0] = Monotonic(10.9)

    queue.then = the_arming_ends
    scribe.cycle(queue, 0, 0)
    scribe.cycle(queue, 0, 0)
    path = published[-1].path
    assert path is not None
    assert frames_read(path) == [
        (-0.09, "open"),
        (-0.08, "read_status"),
        (-0.07, "read_limits"),
        (-0.06, "speed"),
    ]
    assert published[-1].frames_lost == 0


# =========================================================================
# EX-2 on the journal alone: the stretch of a loss, two losses, five equalities
# =========================================================================

LOST: str = "record_degraded: dropped=0 failures=0 drive_frames_lost="


def test_ex2_a_session_armed_in_the_middle_of_what_the_bound_let_go_counts_it(
    tmp_path: Path,
) -> None:
    """The stretch of what was let go ends at its NEWEST frame, not at its oldest."""
    journal, waiting, clock = listening(tmp_path, Limits(frames=3))
    for index in range(6):  # 9.1 to 9.6, and no record yet: 9.1, 9.2 and 9.3 are let go
        waiting.add(9.1 + index / 10, "command")
    journal.drain()
    # Its arming began at 9.25: the frame of 9.3 was its own, and nobody has it.
    journal.open(session(), Privacy(), since=Monotonic(9.25))
    journal.drain()
    assert frames_of(journal) == [(-0.6, "command"), (-0.5, "command"), (-0.4, "command")]
    assert journal.status(clock.monotonic()).dropped == 3
    journal.close(closing())
    journal.drain()
    assert warnings_of(record_of(journal)) == [f"{LOST}3"]


def test_ex2_a_session_that_ended_in_the_middle_of_what_the_bound_let_go_counts_it(
    tmp_path: Path,
) -> None:
    """The stretch of what was let go begins at its OLDEST frame, not at its newest."""
    journal, waiting, _ = listening(tmp_path, Limits(frames=3))
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.drain()
    # From here the thread is away. The session goes on, ends at 10.25, and the console
    # is at rest: six frames wait, and there is room for three.
    waiting.add(10.1, "speed")
    waiting.add(10.2)
    journal.close(closing(until=Monotonic(10.25)))
    for index in range(4):
        waiting.add(10.3 + index / 10)
    journal.drain()
    assert frames_in(record_of(journal)) == []
    assert warnings_of(record_of(journal)) == [f"{LOST}3"], (
        "10.1 and 10.2 were its own: the record says what the bound cost"
    )


def test_ex2_a_second_loss_before_a_record_settled_the_first_is_added_to_it(
    tmp_path: Path,
) -> None:
    """Two cycles during one arming, the tap full at each: the record counts both."""
    journal, waiting, clock = listening(tmp_path)
    waiting.add(9.5, "open")
    waiting.lost = 4  # refused after the frame of 9.5, before this take
    clock.advance(Seconds(0.6))
    journal.drain()
    waiting.add(9.7, "command")
    waiting.lost = 7  # and three more
    clock.advance(Seconds(0.2))
    journal.drain()
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.drain()
    assert frames_of(journal) == [(-0.5, "open"), (-0.3, "command")]
    assert journal.status(clock.monotonic()).dropped == 7
    journal.close(closing())
    journal.drain()
    assert warnings_of(record_of(journal)) == [f"{LOST}7"]


def test_ex2_what_the_bound_cost_twice_before_a_record_settled_it_is_counted_whole(
    tmp_path: Path,
) -> None:
    """The same for what the thread itself let go: two cycles, each past the bound."""
    journal, waiting, clock = listening(tmp_path, Limits(frames=2))
    for index in range(4):  # 9.5 to 9.8: the two oldest are let go
        waiting.add(9.5 + index / 10, "command")
    journal.drain()
    for index in range(3):  # and three more wait: 9.7, 9.8 and 9.9 are let go
        waiting.add(9.9 + index / 100, "command")
    journal.drain()
    journal.open(session(), Privacy(), since=Monotonic(9.5))
    journal.drain()
    assert frames_of(journal) == [(-0.09, "command"), (-0.08, "command")]
    assert journal.status(clock.monotonic()).dropped == 5


def test_ex2_a_frame_that_began_at_the_very_instant_of_the_tick_reported_still_waits(
    tmp_path: Path,
) -> None:
    """On a clock that did not move since that tick, it is the next arming's first exchange."""
    journal, waiting, clock = listening(tmp_path)
    journal.dispatched(Monotonic(9.4))
    waiting.add(9.4, "open")
    journal.drain()  # no record yet
    journal.open(session(), Privacy(), since=Monotonic(9.4))
    journal.drain()
    assert frames_of(journal) == [(-0.6, "open")]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0)


def test_ex2_a_loss_that_ended_at_the_very_instant_of_the_tick_reported_is_still_counted(
    tmp_path: Path,
) -> None:
    """The same arming, and more exchanges than there is room for: what was let go is its own."""
    journal, waiting, clock = listening(tmp_path, Limits(frames=2))
    journal.dispatched(Monotonic(9.4))
    for _ in range(5):
        waiting.add(9.4, "command")
    journal.drain()  # three are let go, at 9.4; no record yet
    journal.open(session(), Privacy(), since=Monotonic(9.4))
    journal.drain()
    assert frames_of(journal) == [(-0.6, "command"), (-0.6, "command")]
    assert journal.status(clock.monotonic()).dropped == 3


def test_ex2_a_loss_that_ended_at_the_very_instant_an_arming_began_is_counted_for_it(
    tmp_path: Path,
) -> None:
    """Every exchange of an arming began at one instant, and some were let go."""
    journal, waiting, clock = listening(tmp_path, Limits(frames=2))
    for _ in range(5):
        waiting.add(9.4, "command")
    journal.drain()
    journal.open(session(), Privacy(), since=Monotonic(9.4))
    journal.drain()
    assert frames_of(journal) == [(-0.6, "command"), (-0.6, "command")]
    assert journal.status(clock.monotonic()).dropped == 3
    journal.close(closing())
    journal.drain()
    assert warnings_of(record_of(journal)) == [f"{LOST}3"]


def ended_on_a_loss_then_armed_again(tmp_path: Path) -> tuple[Path, Path]:
    """Two records. The bound cost the first the exchanges of its last instant, 10.2.

    The thread was away from the middle of the first session: its last three
    exchanges, all begun at the instant it ended, and the arming of the
    second, begun at that same instant on a clock that did not move, wait
    together, and there is room for two.
    """
    journal, waiting, _ = listening(tmp_path, Limits(frames=2))
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5))
    journal.drain()
    for _ in range(3):
        waiting.add(10.2, "speed")
    journal.close(closing(until=Monotonic(10.2)))
    waiting.add(10.4, "open")
    waiting.add(10.6, "command")
    journal.drain()
    journal.open(started_at(2, 10.7), Privacy(), since=Monotonic(10.2))
    journal.drain()
    journal.close(closing())
    journal.drain()
    first, second = records(journal.root)
    return first, second


def test_ex2_a_loss_that_began_at_the_very_instant_a_session_ended_is_counted_for_it(
    tmp_path: Path,
) -> None:
    first, _ = ended_on_a_loss_then_armed_again(tmp_path)
    assert frames_in(first) == []
    assert warnings_of(first) == [f"{LOST}3"]


def test_ex2_a_loss_that_ended_with_a_session_is_not_kept_for_the_one_armed_at_that_instant(
    tmp_path: Path,
) -> None:
    """Like a frame of that instant, it is the first session's: counted once."""
    _, second = ended_on_a_loss_then_armed_again(tmp_path)
    assert frames_in(second) == [(-0.3, "open"), (-0.1, "command")]
    assert warnings_of(second) == []


# =========================================================================
# EX-3 on the journal alone: what the tap refused, counted between two readings
# =========================================================================


def ended(until: float, refused: int | None) -> Closing:
    """The closing of a session that ended at ``until``, the tap's count read then."""
    return replace(closing(until=Monotonic(until)), refused=refused)


def test_ex3_what_the_tap_refused_a_console_at_rest_is_not_counted_with_the_session_after_it(
    tmp_path: Path,
) -> None:
    """The replay of the review, 64 said for 47 lost: the tap was full before the arming."""
    journal, waiting, clock = listening(tmp_path)
    waiting.add(9.1)  # the last frame the tap could keep: the console at rest
    # Seventeen refused at rest. Then the session, every exchange of it refused: 47 more.
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=17)
    journal.close(ended(12.0, refused=64))
    waiting.lost = 64
    clock.advance(Seconds(4.0))
    journal.drain()
    assert frames_in(record_of(journal)) == []
    assert warnings_of(record_of(journal)) == [f"{LOST}47"]


def test_ex3_what_the_tap_refused_after_a_session_ended_is_not_counted_with_it(
    tmp_path: Path,
) -> None:
    """The other replay, 835 said for 829 lost: the console at rest before the thread came."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=0)
    waiting.add(10.5, "speed")  # the last frame the tap could keep
    journal.close(ended(12.0, refused=829))
    waiting.lost = 835  # six more, at rest
    clock.advance(Seconds(4.0))
    journal.drain()
    assert frames_in(record_of(journal)) == [(0.5, "speed")]
    assert warnings_of(record_of(journal)) == [f"{LOST}829"]


def test_ex3_two_sessions_that_waited_through_one_overflow_each_count_what_they_lost(
    tmp_path: Path,
) -> None:
    """Without the two readings both said all 58 (``test_record_frame_carry``): an upper bound."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=0)
    waiting.add(10.5)  # the last frame the tap could keep
    journal.close(ended(12.0, refused=33))
    # Twelve refused at rest between the two: neither session's.
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.5), refused=45)
    journal.close(ended(21.5, refused=58))
    waiting.lost = 58
    clock.advance(Seconds(12.0))
    journal.drain()
    first, second = records(journal.root)
    assert warnings_of(first) == [f"{LOST}33"]
    assert warnings_of(second) == [f"{LOST}13"]


def test_ex3_while_a_session_lasts_its_record_counts_what_the_tap_refused_it_so_far(
    tmp_path: Path,
) -> None:
    journal, waiting, clock = listening(tmp_path)
    waiting.lost = 5  # refused at rest, before the arming
    journal.open(session(), Privacy(), since=Monotonic(9.5), refused=5)
    clock.advance(Seconds(1.0))
    journal.drain()
    before = journal.status(clock.monotonic())
    assert (before.dropped, before.cause) == (0, None), "what was refused before is not its own"
    waiting.lost = 9
    clock.advance(Seconds(0.2))
    journal.drain()
    during = journal.status(clock.monotonic())
    assert (during.dropped, during.cause) == (4, Cause.QUEUE_FULL)
    waiting.lost = 12
    journal.close(ended(11.0, refused=12))
    journal.drain()
    assert warnings_of(record_of(journal)) == [f"{LOST}7"]


def test_ex3_a_record_whose_arming_nobody_read_owes_nothing_to_the_count_of_the_one_before(
    tmp_path: Path,
) -> None:
    """Each record starts its own count: by the two readings, or by the stretch."""
    journal, waiting, clock = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=0)
    waiting.lost = 5
    journal.close(ended(12.0, refused=5))
    clock.advance(Seconds(4.0))
    journal.drain()
    journal.dispatched(Monotonic(13.5))
    clock.advance(Seconds(1.0))
    journal.drain()
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.5))
    clock.advance(Seconds(7.0))
    journal.drain()
    first, second = records(journal.root)
    assert warnings_of(first) == [f"{LOST}5"]
    assert second == record_of(journal)
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped) == (False, 0)


def test_ex3_with_the_count_of_the_arming_and_not_of_the_end_the_latest_seen_stands_for_it(
    tmp_path: Path,
) -> None:
    """Nobody read the tap when the session ended: counted up to the take of its closing.

    That can only be more than the session lost, never fewer.
    """
    journal, waiting, clock = listening(tmp_path)
    journal.open(session(), Privacy(), since=Monotonic(9.5), refused=5)
    journal.close(closing(until=Monotonic(11.0)))
    waiting.lost = 20
    clock.advance(Seconds(4.0))
    journal.drain()
    assert warnings_of(record_of(journal)) == [f"{LOST}15"]


def test_ex3_what_the_tap_refused_between_a_take_and_the_arming_after_it_is_not_subtracted(
    tmp_path: Path,
) -> None:
    """The cycle took the tap, then the console was refused frames at rest, then it armed.

    The record this cycle opens was refused nothing yet, as far as that take
    knows: nothing, not fewer than nothing.
    """
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    source = Overtaken(waiting)
    journal.listen(source)

    def refused_at_rest_then_armed() -> None:
        waiting.lost = 5
        journal.open(session(), Privacy(), since=Monotonic(9.5), refused=5)

    source.then = refused_at_rest_then_armed
    journal.drain()
    status = journal.status(clock.monotonic())
    assert (status.recording, status.dropped, status.cause) == (True, 0, None)


def test_ex3_what_the_tap_refused_a_console_at_rest_is_forgotten_with_its_tick(
    tmp_path: Path,
) -> None:
    """Nobody read the tap's count for the session that follows: it goes by the stretch.

    What was refused before a tick the loop has since reported, with no record
    open, is no session's: a session whose stretch would have covered it does
    not count it.
    """
    journal, waiting, clock = listening(tmp_path)
    waiting.lost = 4  # the tap was full, the console at rest
    clock.advance(Seconds(0.4))
    journal.drain()  # kept: nothing says yet that no record will count them
    journal.dispatched(Monotonic(9.6))  # the loop went on, and armed nothing
    journal.drain()
    journal.open(session(), Privacy(), since=Monotonic(9.0))
    journal.drain()
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped) == (False, 0)


class Before:
    """A frame source before whose takes the loop runs on: one entry of ``then`` per take."""

    def __init__(self, inner: FrameSource) -> None:
        self.inner: FrameSource = inner
        self.then: list[Callable[[], None] | None] = []

    def __call__(self) -> Taken:
        then = self.then.pop(0) if self.then else None
        if then is not None:
            then()
        return self.inner()


def test_ex3_what_the_tap_refused_since_a_session_ended_is_not_its_own_even_for_one_cycle(
    tmp_path: Path,
) -> None:
    """A cycle opens a record whose session ends meanwhile, then takes the tap for another.

    That second take sees what the tap refused the console at rest since. The
    record open at the end of the cycle is counted by the FIRST take, which
    its session had not ended before.
    """
    clock = early_clock()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    source = Before(waiting)
    journal.listen(source)
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=0)
    journal.drain()
    journal.close(ended(10.5, refused=0))
    journal.open(started_at(2, 11.0), Privacy(), since=Monotonic(10.8), refused=0)

    def the_second_session_ends_and_the_console_is_at_rest() -> None:
        journal.close(ended(11.5, refused=3))
        waiting.lost = 8  # three in that session, five at rest since

    source.then = [None, the_second_session_ends_and_the_console_is_at_rest]
    clock.advance(Seconds(3.0))
    journal.drain()  # at the take it makes to close the first record, the loop runs on
    meanwhile = journal.status(clock.monotonic())
    assert (meanwhile.dropped, meanwhile.cause) == (0, None), "never the eight"
    journal.drain()
    first, second = records(journal.root)
    assert warnings_of(first) == []
    assert warnings_of(second) == [f"{LOST}3"]


def test_ex3_the_count_of_the_refused_is_read_without_taking_anything_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    journal = Journal(tmp_path / "records", early_clock())
    nobody = journal.frames_refused()
    assert nobody is None, "no drive is listened to yet"

    waiting = Waiting()
    waiting.lost = 3
    journal.listen(waiting, lambda: waiting.lost)
    counted = journal.frames_refused()
    assert counted == 3
    assert waiting.taken == 0, "nothing was taken to read it"

    def unreadable() -> int:
        raise RuntimeError("injected: the count cannot be read")

    journal.listen(waiting, unreadable)
    with caplog.at_level(logging.ERROR, logger="src.record.journal"):
        unknown = journal.frames_refused()
    assert unknown is None, "unknown, and nothing raised in the tick that asked"
    assert "the count of refused drive frames is unreadable" in caplog.text


async def test_ex3_a_tap_says_how_many_it_refused_whichever_kind_of_drive_it_listens_to() -> None:
    """The delegating tap and the native driver's log: the count ``take`` gives, without a take."""
    clock = ManualClock(Monotonic(100.0))
    simulated = tap_drive(Scripted(clock), clock, capacity=2)
    for _ in range(5):
        await simulated.backend.read_status()
    heard = simulated.refused()
    assert heard == 3
    taken = simulated.source()
    assert (len(taken.observations), taken.lost) == (2, 3)

    native = Native(clock)
    reporting = tap_drive(native, clock, capacity=2)
    log = native.log
    assert log is not None
    for index in range(6):
        log.append(exchange(float(index)))
    counted = reporting.refused()
    assert counted == log.refused == 4
    drained = reporting.source()
    assert (len(drained.observations), drained.lost) == (2, 4)


def test_ex3_a_session_begun_over_one_never_seen_ending_shares_out_what_the_tap_refused(
    tmp_path: Path,
) -> None:
    """What the recorder says of the count when nobody else does.

    When nobody timed an arming, it reads the count itself as the record
    begins. To a session it never saw ending it gives, for its end, the count
    read as the next arming began.
    """
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    recorder = SessionRecorder(
        clock=clock, journal=journal, stamp=STAMP, radius=Metres(1.5), leg_tip=Metres(1.5)
    )
    waiting = Waiting()
    journal.listen(waiting, lambda: waiting.lost)
    waiting.lost = 6  # at rest: nobody's
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    waiting.lost = 10  # four in the first session
    clock.advance(Seconds(2.0))
    heard = recorder.frames_refused()
    assert heard == 10
    recorder.begin(
        SessionRequest(
            occupancy=Occupancy.BENCH,
            operator=OPERATOR,
            arming_began=Monotonic(11.9),
            arming_refused=heard,
        )
    )
    waiting.lost = 13  # three in the second, so far
    journal.drain()
    first, second = records(journal.root)
    assert second == record_of(journal)
    assert degradations(first) == [f"{LOST}4"]
    assert journal.status(clock.monotonic()).dropped == 3


# =========================================================================
# EX-5 on the journal and on the tap alone
# =========================================================================


def test_ex5_a_record_that_is_replaced_says_what_it_lost_before_it_is_closed(
    tmp_path: Path,
) -> None:
    """An opening that finds the previous record open closes it, as ``superseded``.

    It was closed without a word of what it was missing since its last
    periodic line: a reader of that record concluded it was whole. It now
    says so like a record that is closed: the frames it lost, and the values
    the queue refused its session, which the producer counts and starts again
    at each opening.
    """
    journal, waiting, clock = listening(tmp_path, Limits(entries=4))
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.5), refused=0)
    journal.drain()
    queued = [journal.submit(row()) for _ in range(6)]
    assert queued == [True, True, True, True, False, False], "two rows found the queue full"
    # The tap refused five frames of that session, then three of the one that replaces it.
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.5), refused=5)
    waiting.lost = 8
    clock.advance(Seconds(1.0))
    journal.drain()

    first, second = records(journal.root)
    replaced = read(first)
    assert isinstance(replaced, Ok), replaced
    assert replaced.value.manifest.end_reason == "superseded"
    assert replaced.value.warnings == (), "closed and whole"
    assert degradations(first) == ["record_degraded: dropped=2 failures=0 drive_frames_lost=5"]
    assert len(replaced.value.rows) == 4
    # The counts started again with the record that replaced it.
    assert second == record_of(journal)
    status = journal.status(clock.monotonic())
    assert (status.dropped, status.failures) == (3, 0)


def test_ex5_a_record_that_is_replaced_and_lost_nothing_says_nothing(tmp_path: Path) -> None:
    journal, _, _ = listening(tmp_path)
    journal.open(started_at(1, 10.0), Privacy())
    journal.drain()
    journal.open(started_at(2, 20.0), Privacy())
    journal.drain()
    first, _ = records(journal.root)
    assert degradations(first) == []


class Raising(Scripted):
    """A backend every call of which raises after 25 ms: a bug under the tick."""

    def __init__(self, clock: ManualClock, raised: type[BaseException]) -> None:
        super().__init__(clock)
        self.raised: type[BaseException] = raised

    @override
    def _answer(self, what: str) -> None:
        super()._answer(what)
        raise self.raised(f"injected: {what} raised")


@pytest.mark.parametrize(
    "raised",
    [RuntimeError, asyncio.CancelledError],
    ids=["a bug under the tick", "the tick cancelled inside the call"],
)
async def test_ex5_a_call_that_raises_is_noted_as_one_that_failed_and_raises_on(
    raised: type[BaseException],
) -> None:
    """Each call of the drive seam: what was asked is kept, and the caller gets the exception."""
    clock = ManualClock(Monotonic(100.0))
    inner = Raising(clock, raised)
    tap = CallTap(inner, clock)
    calls: tuple[Callable[[], Awaitable[object]], ...] = (
        tap.open,
        tap.close,
        lambda: tap.write_command(ControlWord.ENABLE_OPERATION),
        lambda: tap.write_speed(MotorRpm(150)),
        tap.read_status,
        tap.read_limits,
    )
    for call in calls:
        with pytest.raises(raised, match="injected"):
            await call()
    with pytest.raises(raised, match="injected"):
        tap.emergency_disable_blocking(Seconds(1.25))
    assert len(inner.asked) == 7, "each call reached the drive"

    taken = tap.take()
    assert taken.lost == 0
    assert [(o.kind, o.register, o.value, o.ok) for o in taken.observations] == [
        ("open", None, None, False),
        ("close", None, None, False),
        ("command", CMD_LOGICAL, int(ControlWord.ENABLE_OPERATION), False),
        ("speed", LFRD_LOGICAL, 150, False),
        ("read_failed", None, None, False),
        ("read_limits", None, None, False),
        ("emergency_zero", LFRD_LOGICAL, 0, False),
    ]
    assert [round(o.latency_ms, 3) for o in taken.observations] == [25.0] * 7
    began = [float(o.at) for o in taken.observations]
    assert began == pytest.approx([100.0 + 0.025 * index for index in range(7)])


# =========================================================================
# The REAL console in simulation
# =========================================================================


class HeldInClosing:
    """A frame source that can stop the journal's cycle at the take it makes to close a record.

    A cycle takes the frames once when it begins and once more before each
    record it closes. Armed with ``hold``, the SECOND take from then on is
    made, then waits for ``go``: the cycle has counted its queue and is
    closing a record. Once only.
    """

    def __init__(self, inner: FrameSource) -> None:
        self.inner: FrameSource = inner
        self.before: int = 0
        """Takes left before the one that is held; 0: none is."""
        self.taken: threading.Event = threading.Event()
        self.go: threading.Event = threading.Event()

    def hold(self) -> None:
        self.before = 2

    def __call__(self) -> Taken:
        taken = self.inner()
        if self.before:
            self.before -= 1
            if not self.before:
                self.taken.set()
                self.go.wait(30.0)
        return taken


def tap_held_in_closing(monkeypatch: pytest.MonkeyPatch) -> list[HeldInClosing]:
    """Make the console's tap one that can be held; the list receives it when the rig is built."""
    made: list[HeldInClosing] = []

    def tapping(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        tapped = tap_drive(backend, clock, sdk_frames=sdk_frames)
        held = HeldInClosing(tapped.source)
        made.append(held)
        return replace(tapped, source=held)

    monkeypatch.setattr(local_panel, "tap_drive", tapping)
    return made


async def test_ex1_held_while_it_closes_one_record_as_the_next_session_is_armed_it_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay of the review, on the console, the journal's cycle on a thread of its own.

    The thread was away since the last tick of the first session. It comes
    back inside the fourth exchange of the arming of the second: it takes the
    first three, counts its queue (the closing of the first record, and no
    opening yet), and is held there, closing that record, while the loop ends
    the arming, queues the record of the second session and reports its tick.
    Had the cycle gone by THAT tick, the three exchanges it holds would have
    begun before it with no record open: let go as no session's, and counted
    for nobody.
    """
    taps = tap_held_in_closing(monkeypatch)
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "pair: first"
        await ask_for_a_bench_session(http)
        await recorded.tick(0.6)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=False)
        drive.part = "pair: at rest"
        await ticks(recorded, REST, away=True)

        held = taps[0]
        cycle = threading.Thread(target=recorded.drain, name="journal-cycle-of-a-test")

        def the_journal_comes() -> None:
            """A cycle begins inside that exchange, and is held when it closes the record."""
            held.hold()
            cycle.start()
            reached = held.taken.wait(30.0)
            assert reached, "the cycle never got to the closing of the first record"

        drive.pass_inside(4, the_journal_comes)
        drive.part = "pair: second"
        await ask_for_a_bench_session(http)
        await ticks(recorded, 0.4, away=True)  # the tick that arms, and the next: both reported
        assert drive.passed(), "the journal did come inside that exchange"
        assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
        assert cycle.is_alive(), "still held in the closing of the first record"
        held.go.set()
        await asyncio.to_thread(cycle.join, 30.0)
        assert not cycle.is_alive()

        await ticks(recorded, 0.4, away=False)
        await ask_to_stop(http)
        await to_the_end(recorded, away=False)
        drive.part = "pair: after"
        await ticks(recorded, REST, away=False)
        recorded.drain()

    assert drive.made_in("pair: second")[:4] == ["open", "read_status", "read_limits", "speed"]
    assert exact(recorded, drive, "pair", NONE) == ""
    second = kinds_in(in_order(recorded)[-1])
    arming = second.index("read_limits") - 2
    assert second[arming : arming + 3] == ["open", "read_status", "read_limits"]
    assert nothing_counted(recorded)


@pytest.mark.parametrize(
    "away", [False, True], ids=["the thread on time", "the thread away: one cycle"]
)
async def test_ex4_a_fault_reset_between_two_sessions_is_in_neither_record(
    tmp_path: Path, away: bool
) -> None:
    """A session ends on a drive fault; the operator acknowledges, resets it, and starts again.

    The reset is two commands to the drive (``FAULT_RESET``, then ``SHUTDOWN``
    on a later tick), made with no session open: like the polling around
    them they are no session's, and each record holds the calls of its own
    session, all of them, once. ``away``: the journal comes once, at the end.
    """
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "pair: first"
        await ask_for_a_bench_session(http)
        await ticks(recorded, 0.6, away=away)
        recorded.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
        await to_the_end(recorded, away=away)
        assert recorded.state() is RuntimeState.ENDING, "the fault is latched: nobody reset it"

        drive.part = "pair: at rest"
        await ticks(recorded, REST, away=away)
        acknowledged = await http.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        reset = await http.post("/api/drive/fault-reset", json={"operator": OPERATOR})
        assert reset.status_code == 202, reset.text
        await ticks(recorded, 2.0, away=away)
        assert recorded.state() is RuntimeState.FINISHED, recorded.rig.refusals()

        await a_session(recorded, drive, http, "pair: second", away=away)
        drive.part = "pair: after"
        await ticks(recorded, REST, away=away)
        recorded.drain()
        recorded.drain()

    between = drive.made_in("pair: at rest")
    assert between.count("command") == 2, "the reset reached the drive: its two commands"
    assert exact(recorded, drive, "pair", NONE) == ""
    first, second = in_order(recorded)
    assert kinds_in(first).count("open") == kinds_in(second).count("open") == 1
    assert nothing_counted(recorded)


def small_tap(monkeypatch: pytest.MonkeyPatch, capacity: int) -> None:
    """Give the console a tap with room for ``capacity`` frames."""

    def small(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        return tap_drive(backend, clock, capacity=capacity, sdk_frames=sdk_frames)

    monkeypatch.setattr(local_panel, "tap_drive", small)


async def test_ex3_a_session_says_what_it_lost_and_not_what_the_console_was_refused_at_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two replays of the review (64 said for 47 lost, 835 for 829), on the console.

    The journal thread is away. A console at rest fills its tap and is refused
    frames; a session is armed and every exchange of it is refused; the
    console is at rest again, and refused again. Then the thread comes. The
    record said all of it; it says what the session was refused, to the frame.
    The arming gate would refuse on a measurement that old: it is told the
    measurement is fresh, it is not what is judged.
    """
    monkeypatch.setattr(session_module, "STORAGE_STALE_AFTER", Seconds(1e9))
    small_tap(monkeypatch, 40)
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await ticks(recorded, 40.0, away=True)
        before = len(drive.calls)
        assert before > 40, "the tap is full, and has refused frames already"
        await a_session(recorded, drive, http, "session", away=True)
        drive.part = "after"
        await ticks(recorded, REST, away=True)
        recorded.drain()

    made = drive.made_in("session")
    assert made[0] == "open"
    assert kinds_in(recorded.only_record()) == [], "the tap had no room left for any of them"
    assert warnings_of(recorded.only_record()) == [f"{LOST}{len(drive.calls) - 40}"]
    refused = len(drive.calls) - 40
    assert refused > len(made) + (before - 40), "at rest before, in the session, at rest after"


async def test_ex3_a_programme_counts_from_its_own_arming_like_a_manual_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way a session is armed reads the tap's count as its arming begins, too.

    The session is still going when the thread comes: its record says what
    it was refused so far, and nothing of what the console was refused at rest.
    """
    monkeypatch.setattr(session_module, "STORAGE_STALE_AFTER", Seconds(1e9))
    small_tap(monkeypatch, 40)
    recorded, drive = told_rig(tmp_path, PROGRAMME_ENV)
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await ticks(recorded, 40.0, away=True)
        assert len(drive.calls) > 40, "the tap is full, and has refused frames already"
        drive.part = "programme"
        await start_programme(http)
        await ticks(recorded, 1.0, away=True)
        assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
        recorded.drain()
        status = recorded.journal.status(recorded.clock.monotonic())
    made = drive.made_in("programme")
    assert made[0] == "open"
    assert kinds_in(recorded.only_record()) == []
    assert (status.dropped, status.cause) == (len(drive.calls) - 40, Cause.QUEUE_FULL)


class ToldThenRaises(Told):
    """``Told``, and one status read that raises once ``armed``: a bug under the tick."""

    armed: bool = False

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self.armed:
            self.armed = False
            self._told("read_failed")  # the name of a status read that gave no answer
            raise RuntimeError("injected: a bug inside the tick")
        return await super().read_status()


async def test_ex5_the_exchange_during_which_a_tick_raises_is_in_the_record(
    tmp_path: Path,
) -> None:
    """The last call of a tick that raised is the one a reader looks for.

    The tap noted a call when it returned: that one did not return, and the
    record held every call of the session but it. It is now noted where it
    raises, as a read that failed, and the exception goes on as it was: the
    console fails closed (the reference is zeroed on the way out, which is
    the session's last exchange) and its record is closed ``tick_exception``.
    """
    recorded, drive = told_rig(tmp_path, wrap=ToldThenRaises)
    assert isinstance(drive, ToldThenRaises)
    async with recorded.rig.http() as http:
        drive.part = "session"
        await ask_for_a_bench_session(http)
        await ticks(recorded, 0.6, away=False)
        drive.armed = True
        recorded.clock.advance(TICK)
        with pytest.raises(RuntimeError, match="a bug inside the tick"):
            await recorded.rig.panel.control_step()
    left = await recorded.rig.panel.close()
    assert left

    recording = recorded.recording()
    assert recording.warnings == ()
    assert recording.manifest.end_reason == "tick_exception"
    made = drive.made_in("session")
    assert made[-2:] == ["read_failed", "emergency_zero"]
    assert [frame.kind for frame in recording.frames] == made, "every call, that one included"
    raised = recording.frames[-2]
    assert (raised.kind, raised.ok, raised.value) == ("read_failed", False, None)
    assert await recorded.rig.left_stopped() == ""


class Witness(Told):
    """``Told``, and what each write asked of the drive: the command word, the speed."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.asked: list[tuple[str, int]] = []

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self.asked.append(("command", int(word)))
        return await super().write_command(word)

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self.asked.append(("speed", int(rpm)))
        return await super().write_speed(rpm)


async def a_session_that_turns(recorded: RecordedRig, drive: Told) -> None:
    """At rest, a bench session that climbs and is stopped, at rest again; the journal on time."""
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await ticks(recorded, REST, away=False)
        drive.part = "session"
        await ask_for_a_bench_session(http)
        await ticks(recorded, 0.2, away=False)
        await set_target(http, 5.0)
        await ticks(recorded, 6.0, away=False)
        await ask_to_stop(http)
        await to_the_end(recorded, away=False)
        drive.part = "after"
        await ticks(recorded, REST, away=False)
        recorded.drain()


async def test_ex6_a_count_of_the_refused_that_cannot_be_read_changes_no_call_to_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """What ANH-222 put in the tick that arms and in the tick that ends: one read of a count.

    With that read raising every time, the console asks the drive exactly
    what a healthy one asks, call for call and value for value, the machine
    is left stopped, and the session is recorded whole.
    """
    healthy, told = told_rig(tmp_path / "healthy", wrap=Witness)
    assert isinstance(told, Witness)
    await a_session_that_turns(healthy, told)

    def unreadable() -> int:
        raise RuntimeError("injected: the count cannot be read")

    def tapping(backend: DriveBackend, clock: Clock, *, sdk_frames: bool) -> Tapped:
        return replace(tap_drive(backend, clock, sdk_frames=sdk_frames), refused=unreadable)

    monkeypatch.setattr(local_panel, "tap_drive", tapping)
    broken, witness = told_rig(tmp_path / "broken", wrap=Witness)
    assert isinstance(witness, Witness)
    with caplog.at_level(logging.ERROR, logger="src.record.journal"):
        await a_session_that_turns(broken, witness)
    assert caplog.text.count("the count of refused drive frames is unreadable") == 2, (
        "asked when the arming began and when the session ended, and at no other tick"
    )

    assert len(told.calls) > 150
    assert max(value for kind, value in told.asked if kind == "speed") > 55, "the session turned"
    assert witness.calls == told.calls, "the same calls, in the same order"
    assert witness.asked == told.asked, "and the same values written"
    assert await broken.rig.left_stopped() == ""
    assert kinds_in(broken.only_record()) == witness.calls_for(broken.recording())
    assert broken.recording().warnings == ()
    assert not broken.degraded()
