"""ANH-215 EX-1 to EX-3: a record holds the drive's exchanges of its own session, and no other.

The journal thread takes the drive's frames when it comes, which can be long
after they were noted (it was closing a long record, the disk hung). What
waits for it then is not one session's: the end of a session, the polling of
the console at rest, the arming of the next. Each frame goes by the instant it
began:

* **EX-1**: from the instant an arming began, the frames are those of the
  session it arms. Two sessions chained inside one cycle of the journal each
  keep their own ``open`` exchange;
* **EX-2**: past the instant a session ended, and before the arming of the
  next began, the console is at rest: its polling is in no record, whether
  the thread took it on time or found it waiting;
* **EX-3**: when the frames of a cycle cannot be taken at all, the record that
  very cycle opens counts it, like the one it closes.

On the journal alone first, with the instants chosen by hand; then on the REAL
console in simulation, with a drive that takes time to answer and the journal
kept away, where every call made to the drive is compared with what the
records hold: none lost, none twice, each in the record of its session.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, override

import httpx

from src.clock import ManualClock
from src.motor.drive import (
    ControlWord,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.simulated import SimulatedDrive
from src.record.codec import Privacy
from src.record.journal import Cause, Journal, just_before
from src.record.reader import read
from src.record.retention import records
from src.record.schema import EventKind, RecordError
from src.record.session import INTERRUPTED, SessionRecorder, SessionRequest, Stamp
from src.result import Ok, Result
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Metres, Monotonic, MotorRpm, Seconds, UnixMillis
from tests.record_console_support import RecordedRig, set_target
from tests.record_journal_support import clock_at_start, closing, opened, record_of, session
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    PROGRAMME_ENV,
    Wrapped,
    attest,
    make_rig,
    start_programme,
)
from tests.test_record_drive_frames import Broken, Waiting, frames_in, frames_of, started_at

# =========================================================================
# On the journal alone: where the frames of a session start, and where they stop
# =========================================================================


def test_ex1_two_sessions_chained_within_one_cycle_each_keep_the_exchanges_of_their_own_arming(
    tmp_path: Path,
) -> None:
    """The thread comes back to the end of one session and the start of the next, together."""
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(9.9, "open")
    waiting.add(9.95, "command")
    journal.open(started_at(1, 10.0), Privacy(), since=Monotonic(9.9))
    journal.drain()

    # From here the thread is away: all of this waits for one cycle.
    waiting.add(10.5, "speed")
    waiting.add(12.0)  # the last exchange of the first session
    journal.close(closing(until=Monotonic(12.05)))
    waiting.add(12.2)  # the console at rest
    waiting.add(15.0)
    waiting.add(19.7, "open")  # the second session is asked for at 19.6, and armed
    waiting.add(19.8, "read_limits")
    waiting.add(19.9, "command")
    journal.open(started_at(2, 20.0), Privacy(), since=Monotonic(19.6))
    waiting.add(20.2, "speed")
    journal.drain()
    journal.close(closing(until=Monotonic(21.0)))
    journal.drain()

    first, second = records(journal.root)
    assert frames_in(first) == [
        (-0.1, "open"),
        (-0.05, "command"),
        (0.5, "speed"),
        (2.0, "read_status"),
    ]
    assert frames_in(second) == [
        (-0.3, "open"),
        (-0.2, "read_limits"),
        (-0.1, "command"),
        (0.2, "speed"),
    ], "its own arming, timed from its own start: before it, so negative"
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0)


def test_ex2_the_polling_of_a_console_at_rest_that_waited_behind_a_closing_is_not_in_the_record(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    waiting = Waiting()
    journal.listen(waiting)
    journal.drain()

    waiting.add(10.4, "speed")
    waiting.add(11.0)
    # Ended at the very instant its last exchange began: a clock that did not move in the tick.
    journal.close(closing(until=Monotonic(11.0)))
    waiting.add(11.2)  # at rest, and the thread has still not come
    waiting.add(11.4)
    journal.drain()

    assert frames_in(record_of(journal)) == [(0.4, "speed"), (1.0, "read_status")]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.dropped, status.failures) == (False, 0, 0), (
        "left out, not lost: nothing is counted"
    )


def test_ex2_the_polling_that_waited_before_an_arming_is_not_in_the_record_that_arming_opens(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(3.0)  # at rest for a long while, and nobody came for them
    waiting.add(9.0)
    waiting.add(9.9, "open")  # at the very instant the arming began: the arming's
    waiting.add(9.95, "command")
    journal.open(session(), Privacy(), since=Monotonic(9.9))
    journal.drain()
    assert frames_of(journal) == [(-0.1, "open"), (-0.05, "command")]


def test_ex2_a_frame_from_before_the_arming_found_once_the_record_is_open_is_left_out_as_well(
    tmp_path: Path,
) -> None:
    """Nobody said when the arming began: the frames of the record begin with the record."""
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    waiting = Waiting()
    journal.listen(waiting)
    journal.drain()
    waiting.add(9.8)  # noted before the record started, taken in a later cycle than its opening
    waiting.add(10.0, "open")
    waiting.add(10.2, "speed")
    journal.drain()
    assert frames_of(journal) == [(0.0, "open"), (0.2, "speed")]


def test_the_instant_just_before_another_is_before_it_and_nothing_lies_between() -> None:
    instant = Monotonic(19.6)
    before = just_before(instant)
    assert before < instant
    assert (before + instant) / 2 in (before, instant), "no instant of the clock lies between"


# =========================================================================
# The recorder says when: the arming it was told of, and the end it sees
# =========================================================================

STAMP: Final[Stamp] = Stamp(
    machine_id="machine-1",
    organization_id="org-1",
    software_version="test",
    config_hash="ab" * 32,
    medical_parameters_version="cd" * 32,
)


def frames_read(path: Path) -> list[tuple[float, str]]:
    """The frames of a record that may still be open."""
    loaded = read(path)
    assert isinstance(loaded, Ok), loaded
    return [(frame.t, frame.kind) for frame in loaded.value.frames]


def test_ex1_a_session_begun_over_one_never_seen_ending_takes_its_own_arming_and_no_more(
    tmp_path: Path,
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    recorder = SessionRecorder(
        clock=clock, journal=journal, stamp=STAMP, radius=Metres(1.5), leg_tip=Metres(1.5)
    )
    waiting = Waiting()
    journal.listen(waiting)
    waiting.add(9.9)  # before a record whose arming nobody timed: not its own
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    first = record_of(journal)

    waiting.add(10.5, "speed")
    waiting.add(11.9)  # the end of the first was never seen: its own until the next is armed
    waiting.add(12.0, "open")  # asked at 12.0, and the arming took a tenth of a second
    clock.advance(Seconds(2.1))
    recorder.begin(
        SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR, arming_began=Monotonic(12.0))
    )
    waiting.add(12.3, "speed")
    journal.drain()

    closed = read(first)
    assert isinstance(closed, Ok)
    assert closed.value.manifest.end_reason == INTERRUPTED
    assert frames_read(first) == [(0.5, "speed"), (1.9, "read_status")]
    second = record_of(journal)
    assert second != first
    assert frames_read(second) == [(-0.1, "open"), (0.2, "speed")]


# =========================================================================
# EX-3: the frames of the cycle that opens a record could not be taken
# =========================================================================


def degradations(path: Path) -> list[str]:
    loaded = read(path)
    assert isinstance(loaded, Ok), loaded
    return [e.detail for e in loaded.value.events if e.kind is EventKind.WARNING]


def test_ex3_a_frame_source_that_raises_in_the_cycle_that_opens_a_record_is_counted_for_it(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    source = Broken()
    journal.listen(source)
    journal.open(session(), Privacy())
    journal.drain()
    assert source.asked == 1, "the one cycle that opened the record: its frames were not taken"

    status = journal.status(clock.monotonic())
    assert (status.degraded, status.cause) == (True, Cause.WRITE_FAILED)
    assert status.failures == 1
    assert status.error == RecordError("append", "RuntimeError")

    # And the record says so itself, once the source works again.
    source.broken = False
    clock.advance(Seconds(2.0))
    journal.drain()
    journal.close(closing())
    journal.drain()
    assert degradations(record_of(journal)) == [
        "record_degraded: dropped=0 failures=1 last=append:RuntimeError"
    ]


def test_ex3_the_record_closed_and_the_record_opened_in_that_cycle_both_count_it(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    source = Broken()
    source.broken = False
    journal.listen(source)
    journal.drain()
    first = record_of(journal)

    source.broken = True
    journal.close(closing())
    journal.open(session(2), Privacy())
    journal.drain()

    assert degradations(first) == [
        "record_degraded: dropped=0 failures=1 last=append:RuntimeError"
    ], "the last frames of the first were among them"
    status = journal.status(clock.monotonic())
    assert status.path != first
    assert (status.cause, status.failures) == (Cause.WRITE_FAILED, 1), (
        "and so was the arming of the second"
    )


def test_ex3_a_fault_of_the_source_in_an_earlier_cycle_is_not_counted_for_a_record_opened_later(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    source = Broken()
    journal.listen(source)
    journal.drain()  # the source raises, and nobody is recording
    source.broken = False
    journal.open(session(), Privacy())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.failures, status.error) == (False, 0, None)


# =========================================================================
# The REAL console in simulation, the journal kept away, a drive that takes time
# =========================================================================

ANSWER: Final[Seconds] = Seconds(0.002)
"""What each call to the drive takes here: the clock moves during an arming, as a real one."""


class Told(Wrapped):
    """The simulated drive, slow to answer, and a witness of every call made to it.

    Each call is written down with the part of the test it was made in
    (``part``, set by the test between two ticks) and under the name its
    frame has in a record. It sits under the tap that listens for the record:
    what is written here owes nothing to the code under test.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.clock: ManualClock | None = None
        self.part: str = "first"
        self.calls: list[tuple[str, str]] = []

    def _told(self, kind: str) -> None:
        self.calls.append((self.part, kind))
        if self.clock is not None:
            self.clock.advance(ANSWER)

    def made_in(self, part: str) -> list[str]:
        return [kind for told, kind in self.calls if told == part]

    @override
    async def open(self) -> Result[None, DriveError]:
        self._told("open")
        return await self.inner.open()

    @override
    async def close(self) -> Result[None, DriveError]:
        self._told("close")
        return await self.inner.close()

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self._told("command")
        return await self.inner.write_command(word)

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self._told("speed")
        return await self.inner.write_speed(rpm)

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self._told("read_status")
        return await self.inner.read_status()

    @override
    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        self._told("read_limits")
        return await self.inner.read_limits()

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self._told("emergency_zero")
        return self.inner.emergency_disable_blocking(timeout)


def told_rig(tmp_path: Path, env: Mapping[str, str] = BENCH_ENV) -> tuple[RecordedRig, Told]:
    """The recording console of ``record_console_support``, over a drive that tells."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    rig, wrapper = make_rig(tmp_path, env=env, wrap=Told, clock=clock, journal=journal)
    assert isinstance(wrapper, Told)
    wrapper.clock = clock
    return RecordedRig(rig=rig, journal=journal, clock=clock), wrapper


async def ask_for_a_bench_session(http: httpx.AsyncClient) -> None:
    await attest(http)
    started = await http.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    assert started.status_code == 202, started.text


async def ask_to_stop(http: httpx.AsyncClient) -> None:
    ended = await http.post("/api/session/stop", json={"operator": OPERATOR, "reason": "done"})
    assert ended.status_code == 202, ended.text


async def tick_to_the_end(recorded: RecordedRig, *, away: bool) -> None:
    """Tick until the tick that ends the session, its record not yet closed on disk.

    The journal comes BEFORE each tick, never after the last: the closing, and
    the frames of the tick that queued it, are left waiting. ``away``: it does
    not come at all.
    """
    for _ in range(600):
        if not away:
            recorded.drain()
        await recorded.tick(0.2, drain=False)
        if not recorded.recorder.recording:
            return
    raise AssertionError("the session did not end")


async def test_ex1_acceptance_two_chained_sessions_each_hold_their_own_exchanges_and_no_other(
    tmp_path: Path,
) -> None:
    """Every call made to the drive, against what the two records hold.

    The journal thread is away from the last tick of the first session to the
    first ticks of the second: the closing of one record and the opening of
    the next are written in one cycle, with the polling of the console at rest
    waiting between them. Then it is away again from the last tick of the
    second session, through a console at rest, with no session after it.
    """
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        await ask_for_a_bench_session(http)
        await recorded.tick(0.2)
        await set_target(http, 5.0)
        await recorded.tick(4.0)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=False)

        drive.part = "at rest"
        await recorded.tick(0.6, drain=False)

        drive.part = "second"
        await ask_for_a_bench_session(http)
        await recorded.tick(1.0, drain=False)
        assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
        assert len(recorded.records()) == 1, "the first is still open on disk: nobody came"
        recorded.drain()  # one cycle: the first is closed, the second opened
        assert len(recorded.records()) == 2

        await set_target(http, 5.0)
        await recorded.tick(3.0)
        await ask_to_stop(http)
        await tick_to_the_end(recorded, away=True)

        drive.part = "at rest again"
        await recorded.tick(0.6, drain=False)
        recorded.drain()

    first, second = (read(path) for path in recorded.records())
    assert isinstance(first, Ok), first
    assert isinstance(second, Ok), second
    assert first.value.warnings == second.value.warnings == (), "closed and whole, both"

    # The test itself is sound: the console went on talking to its drive at rest, both times.
    assert "read_status" in drive.made_in("at rest")
    assert "read_status" in drive.made_in("at rest again")

    # None lost, none twice, each in the record of its session, in the order it was made:
    # the frames of a record ARE the calls made in its session, and what the console did
    # at rest is in neither.
    held = [[frame.kind for frame in loaded.value.frames] for loaded in (first, second)]
    assert held == [drive.made_in("first"), drive.made_in("second")]
    for frames in held:
        assert frames.count("open") == 1, "its own arming, and nobody else's"
        assert frames[0] == "open"

    # The clock moved during the arming: its exchanges are timed before the start of
    # the record they open, and nothing in a record is timed after its session ended.
    for loaded in (first, second):
        recording = loaded.value
        assert recording.frames[0].t < 0
        observation = recording.manifest.end_observation
        assert observation is not None
        assert recording.frames[-1].t <= observation.t + 0.2
        times = [frame.t for frame in recording.frames]
        assert times == sorted(times)


async def test_ex2_on_time_too_the_polling_of_a_console_at_rest_is_in_no_record(
    tmp_path: Path,
) -> None:
    """The journal comes after every tick, as its thread does: the same rule, the same record."""
    recorded, drive = told_rig(tmp_path)
    async with recorded.rig.http() as http:
        drive.part = "before"
        await recorded.tick(0.6)
        drive.part = "first"
        await ask_for_a_bench_session(http)
        await recorded.tick(2.0)
        await ask_to_stop(http)
        for _ in range(600):
            await recorded.tick(0.2)
            if not recorded.recorder.recording:
                break
        drive.part = "after"
        await recorded.tick(0.6)
    recording = recorded.recording()
    assert recording.warnings == ()
    assert drive.made_in("before")
    assert drive.made_in("after")
    assert [frame.kind for frame in recording.frames] == drive.made_in("first")


async def test_ex1_a_programme_keeps_the_exchanges_of_its_own_arming_like_a_manual_session(
    tmp_path: Path,
) -> None:
    """The other way a session is armed: a programme, the journal away since before it."""
    recorded, drive = told_rig(tmp_path, PROGRAMME_ENV)
    async with recorded.rig.http() as http:
        drive.part = "at rest"
        await recorded.tick(0.6, drain=False)
        drive.part = "programme"
        await start_programme(http)
        await recorded.tick(1.0, drain=False)
        assert recorded.state() is RuntimeState.RUNNING, recorded.rig.refusals()
        recorded.drain()
    assert drive.made_in("at rest")
    frames = frames_read(recorded.only_record())
    assert [kind for _, kind in frames] == drive.made_in("programme")
    assert frames[0][1] == "open"
    assert frames[0][0] < 0, "the arming took time, and came before the start of the record"
