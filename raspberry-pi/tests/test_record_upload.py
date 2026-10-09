"""A session record read back for the dashboard (ANH-129 EX-1).

What is established here:

* the 1 Hz telemetry is the first tick of each whole second of ``ticks.csv``,
  a rule that depends on the record alone: read again from the same place, or
  from the beginning, it gives the same points with the same dates;
* a point is dated from the start the manifest holds plus the time elapsed on
  the monotonic clock, never from a later reading of the wall clock;
* the events of ``events.jsonl`` each carry their rank in the file;
* only complete lines are read, a bounded amount per call, and a line that
  cannot be understood is passed over and counted;
* an offset that is not the start of a line of the file is refused, never
  read from;
* a run of bytes too long to be a line (zeroes left by a power cut) is passed
  over, across as many readings as it takes, counted once, and what follows
  it is read;
* a cursor is used only for the record it names;
* which records the dashboard is still owed: those whose cursor is not
  complete, and those made since the synchronisation first listed the
  directory that were never tied to a cursor.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record.cursor import (
    BASELINE_NAME,
    CURSOR_SUFFIX,
    Baseline,
    Cursor,
    CursorError,
    NoCursor,
    UnreadableCursor,
    cursor_of,
    load,
    load_baseline,
    store,
    store_baseline,
)
from src.record.logbook import ACTIVE, DIRECTORY, PREVIOUS
from src.record.reader import read
from src.record.retention import PurgeReport, confirm_deposit, purge
from src.record.schema import EndObservation, Event, EventKind
from src.record.upload import (
    INTERRUPTED,
    MAX_EVENT_DETAIL,
    MAX_EVENTS,
    MAX_POINTS,
    MISALIGNED,
    OTHER_RECORD,
    Ended,
    Listed,
    open_record,
    owed_records,
    read_batch,
    read_events,
    read_head,
    read_last_tick_ms,
    read_points,
    reference_in,
)
from src.record.writer import TICK_COLUMNS, Writer
from src.result import Err, Ok, Result
from src.units import Monotonic, UnixMillis
from tests.record_support import manifest, row, writer
from tests.record_uplink_support import PROGRAMME, launched_programme

START_MS = 1_791_195_072_000
"""``2026-10-05T10:11:12Z``, the start the test manifest carries."""


def ticking(made: Writer, seconds: float, *, hertz: int = 5, bpm: int | None = 73) -> None:
    """Write ``seconds`` of ticks at ``hertz``, the first at ``t = 0``."""
    for index in range(round(seconds * hertz)):
        tick = dataclasses.replace(
            row(), t=index / hertz, measured_motor_rpm=100 + index, hr_live=bpm
        )
        assert isinstance(made.tick(tick), Ok)


def events_of(made: Writer, count: int) -> None:
    for index in range(count):
        event = Event(t=index + 0.25, kind=EventKind.PHASE, detail=f"phase {index}")
        assert isinstance(made.event(event), Ok)


# =========================================================================
# The manifest
# =========================================================================


def test_the_head_of_an_open_record_says_who_and_when_and_no_end(tmp_path: Path) -> None:
    made = writer(tmp_path)

    head = read_head(made.path)

    assert isinstance(head, Ok)
    found = head.value
    assert (found.local_ref, found.remote_id) == ("local-4", None)
    assert (found.kind, found.occupancy, found.operator) == ("manual", "bench", "op-5")
    assert found.profile is None
    assert found.start_ms == START_MS
    assert found.monotonic_start == 10.0
    assert found.closed is None


def test_the_head_of_a_closed_record_says_how_it_ended_on_the_session_s_own_axis(
    tmp_path: Path,
) -> None:
    made = writer(tmp_path)
    # Closed while the wall clock says something else entirely: it is not read.
    closing = ManualClock(Monotonic(500.0), UnixMillis(5_000_000_000_000))
    observation = EndObservation(t=312.4, stop_reason="operator pressed STOP")
    closed = made.close(closing, "operator_stop", observation)
    assert isinstance(closed, Ok)

    head = read_head(made.path)

    assert isinstance(head, Ok)
    assert head.value.closed == Ended(
        reason="operator_stop", stop_reason="operator pressed STOP", at_ms=START_MS + 312_400
    )


def test_a_record_closed_without_an_observation_has_no_date_of_its_own(tmp_path: Path) -> None:
    made = writer(tmp_path)
    closed = made.close(ManualClock(), "superseded")
    assert isinstance(closed, Ok)

    head = read_head(made.path)

    assert isinstance(head, Ok)
    assert head.value.closed == Ended(reason="superseded", stop_reason=None, at_ms=None)


def test_a_closing_without_a_reason_is_an_interruption(tmp_path: Path) -> None:
    made = writer(tmp_path)
    closed = made.close(ManualClock(), "x")
    assert isinstance(closed, Ok)
    path = made.path / "manifest.json"
    text = path.read_text(encoding="utf-8")
    assert '"end_reason":"x"' in text
    path.write_text(text.replace('"end_reason":"x"', '"end_reason":null'), encoding="utf-8")

    head = read_head(made.path)

    assert isinstance(head, Ok)
    assert head.value.closed == Ended(reason=INTERRUPTED, stop_reason=None, at_ms=None)


def test_the_head_carries_the_dashboard_s_identifier_and_the_programme(tmp_path: Path) -> None:
    record = launched_programme(tmp_path)

    head = read_head(record)

    assert isinstance(head, Ok)
    assert head.value.remote_id == "k17remote"
    assert head.value.kind == "auto"
    assert head.value.profile == PROGRAMME


@pytest.mark.parametrize("content", [None, b"", b"{", b'{"schema_version": 3}'])
def test_a_manifest_that_is_absent_or_cannot_be_read_is_an_error(
    tmp_path: Path, content: bytes | None
) -> None:
    made = writer(tmp_path)
    path = made.path / "manifest.json"
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)

    head = read_head(made.path)

    assert isinstance(head, Err)
    assert head.error.operation == "read"


# =========================================================================
# Telemetry at 1 Hz
# =========================================================================


def test_one_point_per_whole_second_the_first_tick_of_each(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 3.0)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    points = found.value.points
    assert [point["elapsedS"] for point in points] == [0.0, 1.0, 2.0]
    assert [point["t"] for point in points] == [START_MS, START_MS + 1000, START_MS + 2000]
    # The tick kept for second 1 is the first of that second: tick number 5.
    assert [point["motorRpm"] for point in points] == [100, 105, 110]
    assert found.value.last_t == START_MS + 2000
    assert found.value.more is False
    assert found.value.skipped == 0
    assert found.value.offset == (made.path / "ticks.csv").stat().st_size


def test_a_point_is_what_the_dashboard_s_route_takes(tmp_path: Path) -> None:
    made = writer(tmp_path)
    tick = dataclasses.replace(
        row(),
        t=12.3456,
        phase="cooldown",
        measured_motor_rpm=1203,
        output_rpm=24.16789,
        setpoint_motor_rpm=1200,
        g_reference=1.23456789,
        safety_action="RAMP_DOWN",
        hr_live=141,
    )
    assert isinstance(made.tick(tick), Ok)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert found.value.points == (
        {
            "t": START_MS + 12_346,
            "elapsedS": 12.35,
            "phase": "cooldown",
            "motorRpm": 1203,
            "outputRpm": 24.168,
            "setpointMotorRpm": 1200,
            "gLoad": 1.2346,
            "safetyAction": "ramp_down",
            "bpm": 141,
        },
    )


def test_no_heart_rate_is_sent_when_the_record_holds_none(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 1.0, bpm=None)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert "bpm" not in found.value.points[0]


def test_reading_again_from_the_cursor_sends_no_second_twice(tmp_path: Path) -> None:
    """The cursor may stop in the middle of a second: its other ticks are not points."""
    made = writer(tmp_path)
    ticking(made, 4.0)
    first = read_points(made.path, start_ms=START_MS, offset=0, after_t=None, limit=2)
    assert isinstance(first, Ok)
    assert [point["elapsedS"] for point in first.value.points] == [0.0, 1.0]
    assert first.value.more is True
    assert first.value.offset < (made.path / "ticks.csv").stat().st_size

    rest = read_points(
        made.path, start_ms=START_MS, offset=first.value.offset, after_t=first.value.last_t
    )

    assert isinstance(rest, Ok)
    assert [point["elapsedS"] for point in rest.value.points] == [2.0, 3.0]
    assert rest.value.more is False


def test_reading_from_the_beginning_gives_the_same_points_with_the_same_dates(
    tmp_path: Path,
) -> None:
    """What lets the dashboard recognise a point it already has: it never changes."""
    made = writer(tmp_path)
    ticking(made, 12.0)
    whole = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)
    assert isinstance(whole, Ok)

    pieces: list[object] = []
    offset, after = 0, None
    for _ in range(4):
        piece = read_points(made.path, start_ms=START_MS, offset=offset, after_t=after, limit=3)
        assert isinstance(piece, Ok)
        pieces.extend(piece.value.points)
        offset, after = piece.value.offset, piece.value.last_t

    assert pieces == list(whole.value.points)
    assert len(pieces) == 12


def test_a_gap_in_the_ticks_is_a_gap_in_the_points_never_an_invention(tmp_path: Path) -> None:
    made = writer(tmp_path)
    for t in (0.0, 0.2, 3.4, 3.6, 5.0):
        assert isinstance(made.tick(dataclasses.replace(row(), t=t)), Ok)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert [point["elapsedS"] for point in found.value.points] == [0.0, 3.4, 5.0]


def test_the_ticks_before_the_start_are_not_telemetry(tmp_path: Path) -> None:
    """A simulation writes its idle ticks with a negative time: they are no session."""
    made = writer(tmp_path)
    for t in (-0.4, -0.2, 0.0, 0.2):
        assert isinstance(made.tick(dataclasses.replace(row(), t=t)), Ok)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert [point["elapsedS"] for point in found.value.points] == [0.0]
    assert found.value.skipped == 0


def test_at_most_three_hundred_points_are_read_at_a_time(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, MAX_POINTS + 20, hertz=1)

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert len(found.value.points) == MAX_POINTS
    assert found.value.more is True


def test_a_line_still_being_written_is_left_for_the_next_reading(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 1.0)
    path = made.path / "ticks.csv"
    complete = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"1.0,active,manual,hold,OPERATION")

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert [point["elapsedS"] for point in found.value.points] == [0.0]
    assert found.value.offset == complete
    # Nothing but that half line after the cursor: nothing read, nothing moved.
    again = read_points(made.path, start_ms=START_MS, offset=complete, after_t=found.value.last_t)
    assert isinstance(again, Ok)
    assert (again.value.points, again.value.offset, again.value.more) == ((), complete, False)


def test_a_record_with_only_its_header_or_nothing_at_all_has_no_point(tmp_path: Path) -> None:
    made = writer(tmp_path)

    header_only = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)
    (made.path / "ticks.csv").write_bytes(b"")
    empty = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(header_only, Ok)
    assert header_only.value.points == ()
    assert header_only.value.last_t is None
    assert isinstance(empty, Ok)
    assert (empty.value.points, empty.value.offset) == ((), 0)


def test_lines_that_are_not_ticks_are_passed_over_and_counted(tmp_path: Path) -> None:
    made = writer(tmp_path)
    assert isinstance(made.tick(dataclasses.replace(row(), t=0.0)), Ok)
    good = (made.path / "ticks.csv").read_bytes().splitlines()[1]
    fields = good.decode("utf-8").split(",")

    def line(**changes: str) -> bytes:
        changed = list(fields)
        for name, value in changes.items():
            changed[TICK_COLUMNS.index(name)] = value
        return ",".join(changed).encode("utf-8") + b"\n"

    with (made.path / "ticks.csv").open("ab") as handle:
        handle.write(b"only,three,columns\n")
        handle.write(b"\n")
        handle.write(line(t="soon"))
        handle.write(line(t="1.0", measured_motor_rpm="fast"))
        handle.write(line(t="1.0", hr_live="seventy"))
        handle.write(line(t="nan"))
        handle.write(line(t="1.0", output_rpm="inf"))
        handle.write(line(t="1.0", g_reference="-inf"))
        handle.write(b"\xff\xfe not text\n")
        handle.write(line(t="2.0"))

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Ok)
    assert [point["elapsedS"] for point in found.value.points] == [0.0, 2.0]
    assert found.value.skipped == 9


@pytest.mark.parametrize(
    "header",
    [b"t,state,mode\n", b"\xff\xfe\n", b"0.0,active,manual,hold\n"],
)
def test_a_file_that_does_not_start_with_this_format_s_columns_is_refused(
    tmp_path: Path, header: bytes
) -> None:
    made = writer(tmp_path)
    (made.path / "ticks.csv").write_bytes(header + b"0.0\n")

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Err)
    assert found.error.detail == "columns"


def test_an_offset_that_is_not_the_start_of_a_line_is_refused(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 2.0)
    size = (made.path / "ticks.csv").stat().st_size

    for offset in (5, size - 3, size + 40):
        found = read_points(made.path, start_ms=START_MS, offset=offset, after_t=None)
        assert isinstance(found, Err)
        # Told apart from a disk that refuses: the sending starts again from 0.
        assert found.error.detail == MISALIGNED


def test_ticks_that_cannot_be_opened_are_an_error(tmp_path: Path) -> None:
    made = writer(tmp_path)
    (made.path / "ticks.csv").unlink()

    found = read_points(made.path, start_ms=START_MS, offset=0, after_t=None)

    assert isinstance(found, Err)
    assert found.error.detail == "FileNotFoundError:ENOENT"


def test_a_reading_is_bounded_in_bytes_and_goes_on_from_where_it_stopped(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 6.0)
    size = (made.path / "ticks.csv").stat().st_size

    seconds: list[object] = []
    offset, after, readings = 0, None, 0
    while True:
        found = read_points(made.path, start_ms=START_MS, offset=offset, after_t=after, chunk=1000)
        assert isinstance(found, Ok)
        readings += 1
        seconds.extend(point["elapsedS"] for point in found.value.points)
        offset = found.value.offset
        after = found.value.last_t if found.value.last_t is not None else after
        if not found.value.more:
            break

    assert seconds == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert offset == size
    assert readings > 3


def ticking_on(made: Writer, first: int, seconds: int) -> None:
    """Write ``seconds`` more of ticks at 5 Hz, the first at ``t = first``."""
    for index in range(seconds * 5):
        tick = dataclasses.replace(row(), t=first + index / 5, hr_live=73)
        assert isinstance(made.tick(tick), Ok)


@dataclasses.dataclass
class Followed:
    """What a reader that follows its own cursor got out of ``ticks.csv``, reading after reading."""

    seconds: list[object] = dataclasses.field(default_factory=list[object])
    skipped: int = 0
    readings: int = 0
    inside: int = 0
    """Readings that stopped inside a line being passed over."""

    offset: int = 0
    mid_line: bool = False
    after: int | None = None

    def read(self, record: Path, *, chunk: int) -> None:
        """Read until the stream gives no more, each reading from where the last stopped."""
        while True:
            found = read_points(
                record,
                start_ms=START_MS,
                offset=self.offset,
                after_t=self.after,
                mid_line=self.mid_line,
                chunk=chunk,
            )
            assert isinstance(found, Ok), found
            self.readings += 1
            self.seconds.extend(point["elapsedS"] for point in found.value.points)
            self.skipped += found.value.skipped
            self.inside += found.value.mid_line
            self.offset = found.value.offset
            self.mid_line = found.value.mid_line
            self.after = found.value.last_t if found.value.last_t is not None else self.after
            if not found.value.more:
                return


@pytest.mark.parametrize("zeroes", [2500, 2990, 3000])
def test_a_line_too_long_to_be_one_is_passed_over_across_several_readings(
    tmp_path: Path, zeroes: int
) -> None:
    """Zeroes a power cut left in the file, longer than one reading and with no end of line.

    They are counted once, where they begin; every reading goes on from where
    the one before stopped; and the ticks after them are read like any other.
    Never is an offset given that the next reading refuses.
    """
    made = writer(tmp_path)
    ticking_on(made, 0, 2)
    path = made.path / "ticks.csv"
    with path.open("ab") as handle:
        handle.write(b"\x00" * zeroes + b"\n")
    ticking_on(made, 2, 2)

    follower = Followed()
    follower.read(made.path, chunk=1000)

    assert follower.seconds == [0.0, 1.0, 2.0, 3.0]
    assert follower.skipped == 1
    assert follower.inside >= 2, "the zeroes took more than one reading"
    assert (follower.offset, follower.mid_line) == (path.stat().st_size, False)


def test_a_line_too_long_that_reaches_the_end_of_the_file_is_waited_for(tmp_path: Path) -> None:
    """Nothing after the zeroes yet: the reading stands inside them, and goes on when more comes."""
    made = writer(tmp_path)
    ticking_on(made, 0, 1)
    path = made.path / "ticks.csv"
    with path.open("ab") as handle:
        handle.write(b"\x00" * 2300)

    follower = Followed()
    follower.read(made.path, chunk=1000)
    assert (follower.seconds, follower.skipped) == ([0.0], 1)
    assert (follower.offset, follower.mid_line) == (path.stat().st_size, True)
    follower.read(made.path, chunk=1000)
    assert (follower.offset, follower.mid_line) == (path.stat().st_size, True), "it waits there"

    with path.open("ab") as handle:
        handle.write(b"\n")
    ticking_on(made, 1, 1)
    follower.read(made.path, chunk=1000)

    assert (follower.seconds, follower.skipped) == ([0.0, 1.0], 1)
    assert (follower.offset, follower.mid_line) == (path.stat().st_size, False)


def test_a_place_inside_a_long_line_that_the_file_does_not_reach_is_refused(
    tmp_path: Path,
) -> None:
    """A cursor that stands inside a line being passed over, past the end of the file."""
    made = writer(tmp_path)
    ticking_on(made, 0, 1)
    size = (made.path / "ticks.csv").stat().st_size

    found = read_points(made.path, start_ms=START_MS, offset=size + 1, after_t=None, mid_line=True)

    assert isinstance(found, Err)
    assert found.error.detail == MISALIGNED


def test_an_event_line_too_long_to_be_one_takes_its_rank_and_is_passed_over(
    tmp_path: Path,
) -> None:
    made = writer(tmp_path)
    events_of(made, 2)
    path = made.path / "events.jsonl"
    with path.open("ab") as handle:
        handle.write(b"\x00" * 700 + b"\n")
    assert isinstance(made.event(Event(t=9.0, kind=EventKind.PHASE, detail="after")), Ok)

    ranks: list[object] = []
    skipped, offset, seq, mid = 0, 0, -1, False
    for _ in range(12):
        found = read_events(
            made.path, start_ms=START_MS, offset=offset, last_seq=seq, mid_line=mid, chunk=300
        )
        assert isinstance(found, Ok), found
        ranks.extend(event["seq"] for event in found.value.events)
        skipped += found.value.skipped
        offset, seq, mid = found.value.offset, found.value.last_seq, found.value.mid_line

    # The zeroes are the third line of the file: rank 2, like any line that is no event.
    assert (ranks, skipped, seq) == ([0, 1, 3], 1, 3)
    assert (offset, mid) == (path.stat().st_size, False)


def test_the_last_tick_dates_a_record_that_was_cut_short(tmp_path: Path) -> None:
    made = writer(tmp_path)
    ticking(made, 7.0)
    with (made.path / "ticks.csv").open("ab") as handle:
        handle.write(b"7.0,active,manual,hold,OPER")  # the write the kill interrupted

    assert read_last_tick_ms(made.path) == Ok(6800)
    # From a tail that starts in the middle of a line, too.
    assert read_last_tick_ms(made.path, chunk=400) == Ok(6800)


def test_a_record_without_a_tick_has_no_last_tick(tmp_path: Path) -> None:
    made = writer(tmp_path)

    assert read_last_tick_ms(made.path) == Ok(None)

    (made.path / "ticks.csv").unlink()
    missing = read_last_tick_ms(made.path)
    assert isinstance(missing, Err)
    assert missing.error.detail == "FileNotFoundError:ENOENT"


# =========================================================================
# Events, each with its rank
# =========================================================================


def test_each_event_carries_its_rank_in_the_record(tmp_path: Path) -> None:
    made = writer(tmp_path)
    assert isinstance(
        made.event(Event(t=0.0, kind=EventKind.OPERATOR_ACTION, detail="manual session started")),
        Ok,
    )
    assert isinstance(
        made.event(
            Event(t=12.4, kind=EventKind.VERDICT, detail="hr_above_hard_max", actor="system")
        ),
        Ok,
    )
    assert isinstance(
        made.event(Event(t=13.0, kind=EventKind.REMOTE_COMMAND, detail="stop", actor="remote")),
        Ok,
    )

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert found.value.events == (
        {
            "seq": 0,
            "t": START_MS,
            "kind": "operator_action",
            "detail": "manual session started",
            "actor": "system",
        },
        {
            "seq": 1,
            "t": START_MS + 12_400,
            "kind": "verdict",
            "detail": "hr_above_hard_max",
            "actor": "system",
        },
        {
            "seq": 2,
            "t": START_MS + 13_000,
            "kind": "remote_command",
            "detail": "stop",
            "actor": "remote",
        },
    )
    assert found.value.last_seq == 2
    assert found.value.more is False
    assert found.value.offset == (made.path / "events.jsonl").stat().st_size


def test_events_go_on_from_the_cursor_with_the_ranks_that_follow(tmp_path: Path) -> None:
    made = writer(tmp_path)
    events_of(made, 7)
    first = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1, limit=3)
    assert isinstance(first, Ok)
    assert [event["seq"] for event in first.value.events] == [0, 1, 2]
    assert first.value.more is True

    rest = read_events(
        made.path, start_ms=START_MS, offset=first.value.offset, last_seq=first.value.last_seq
    )

    assert isinstance(rest, Ok)
    assert [event["seq"] for event in rest.value.events] == [3, 4, 5, 6]
    assert rest.value.events[0]["detail"] == "phase 3"


def test_at_most_two_hundred_events_are_read_at_a_time(tmp_path: Path) -> None:
    made = writer(tmp_path)
    events_of(made, MAX_EVENTS + 5)

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert len(found.value.events) == MAX_EVENTS
    assert found.value.more is True
    assert found.value.last_seq == MAX_EVENTS - 1


def test_a_record_without_an_event_has_none_and_moves_no_rank(tmp_path: Path) -> None:
    made = writer(tmp_path)

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert (found.value.events, found.value.offset, found.value.last_seq) == ((), 0, -1)


def test_a_line_that_is_not_an_event_keeps_its_rank_and_is_passed_over(tmp_path: Path) -> None:
    made = writer(tmp_path)
    events_of(made, 1)
    good = {"t": 1.0, "kind": "phase", "detail": "hold", "actor": "system"}
    intruders = [
        b"not json",
        b"[1, 2]",
        b"",
        json.dumps({**good, "t": "soon"}).encode(),
        json.dumps({**good, "t": True}).encode(),
        b'{"t": Infinity, "kind": "phase", "detail": "hold", "actor": "system"}',
        json.dumps({**good, "kind": 3}).encode(),
        json.dumps({**good, "kind": "Not A Kind"}).encode(),
        json.dumps({**good, "detail": None}).encode(),
        json.dumps({**good, "actor": 7}).encode(),
        json.dumps({**good, "actor": "Dr Attending"}).encode(),
        json.dumps({**good, "actor": "a" * 65}).encode(),
    ]
    with (made.path / "events.jsonl").open("ab") as handle:
        for intruder in intruders:
            handle.write(intruder + b"\n")
        handle.write(json.dumps({**good, "t": 5}).encode() + b"\n")

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert [event["seq"] for event in found.value.events] == [0, len(intruders) + 1]
    assert found.value.events[1]["t"] == START_MS + 5000
    assert found.value.skipped == len(intruders)
    assert found.value.last_seq == len(intruders) + 1


def test_a_text_longer_than_the_dashboard_takes_is_cut(tmp_path: Path) -> None:
    made = writer(tmp_path)
    long = {"t": 1.0, "kind": "warning", "detail": "x" * (MAX_EVENT_DETAIL + 50), "actor": "system"}
    (made.path / "events.jsonl").write_bytes(json.dumps(long).encode() + b"\n")

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert found.value.events[0]["detail"] == "x" * MAX_EVENT_DETAIL


def test_an_event_still_being_written_is_left_and_has_no_rank_yet(tmp_path: Path) -> None:
    made = writer(tmp_path)
    events_of(made, 2)
    path = made.path / "events.jsonl"
    complete = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b'{"t":2.25,"kind":"pha')

    found = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(found, Ok)
    assert (len(found.value.events), found.value.offset, found.value.last_seq) == (2, complete, 1)


def test_events_are_not_read_from_an_offset_that_is_not_theirs(tmp_path: Path) -> None:
    made = writer(tmp_path)
    events_of(made, 2)

    misaligned = read_events(made.path, start_ms=START_MS, offset=3, last_seq=-1)
    (made.path / "events.jsonl").unlink()
    missing = read_events(made.path, start_ms=START_MS, offset=0, last_seq=-1)

    assert isinstance(misaligned, Err)
    assert misaligned.error.detail == MISALIGNED
    assert isinstance(missing, Err)
    assert missing.error.detail == "FileNotFoundError:ENOENT"


# =========================================================================
# One reading, and the records still owed
# =========================================================================


def test_one_reading_gives_the_closing_the_points_and_the_events_after_the_cursor(
    tmp_path: Path,
) -> None:
    made = writer(tmp_path)
    ticking(made, 5.0)
    events_of(made, 3)
    whole = read_batch(made.path, start_ms=START_MS, cursor=Cursor(), with_last_tick=False)
    assert isinstance(whole, Ok)
    assert whole.value.closed is None
    assert len(whole.value.points.points) == 5
    assert len(whole.value.events.events) == 3
    assert whole.value.last_tick_ms is None

    cursor = Cursor(
        ticks_offset=whole.value.points.offset,
        last_t=whole.value.points.last_t,
        events_offset=whole.value.events.offset,
        last_seq=whole.value.events.last_seq,
    )
    closed = made.close(ManualClock(), "operator_stop", EndObservation(t=5.0))
    assert isinstance(closed, Ok)
    after = read_batch(made.path, start_ms=START_MS, cursor=cursor, with_last_tick=True)

    assert isinstance(after, Ok)
    assert after.value.closed == Ended(
        reason="operator_stop", stop_reason=None, at_ms=START_MS + 5000
    )
    assert (after.value.points.points, after.value.events.events) == ((), ())
    assert after.value.last_tick_ms == 4800


@pytest.mark.parametrize("missing", ["manifest.json", "ticks.csv", "events.jsonl"])
def test_one_reading_fails_whole_when_a_part_of_the_record_cannot_be_read(
    tmp_path: Path, missing: str
) -> None:
    made = writer(tmp_path)
    ticking(made, 1.0)
    (made.path / missing).unlink()

    found = read_batch(made.path, start_ms=START_MS, cursor=Cursor(), with_last_tick=True)

    assert isinstance(found, Err)
    assert found.error.operation == "read"


def test_one_reading_fails_when_only_the_last_tick_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = writer(tmp_path)
    ticking(made, 1.0)

    def refuse(_descriptor: int) -> object:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("src.record.upload.os.fstat", refuse)

    found = read_batch(made.path, start_ms=START_MS, cursor=Cursor(), with_last_tick=True)

    assert isinstance(found, Err)
    assert found.error.detail == "OSError:EIO"


def test_a_record_is_opened_with_its_manifest_and_what_its_cursor_says(tmp_path: Path) -> None:
    made = writer(tmp_path)
    name = made.path.name

    bare = open_record(tmp_path, name)
    assert isinstance(bare, Ok)
    assert bare.value.record == made.path
    assert bare.value.head.local_ref == "local-4"
    assert bare.value.cursor == NoCursor()
    assert bare.value.last_tick_ms is None
    ticking(made, 3.0)
    lasting = open_record(tmp_path, name)
    assert isinstance(lasting, Ok)
    assert lasting.value.last_tick_ms == 2800
    (made.path / "ticks.csv").rename(made.path / "ticks.away")
    assert isinstance(open_record(tmp_path, name), Err)
    (made.path / "ticks.away").rename(made.path / "ticks.csv")

    mine = Cursor(local_ref="local-4", session_id="cloud-1", ticks_offset=44)
    assert isinstance(store(made.path, mine), Ok)
    known = open_record(tmp_path, name)
    assert isinstance(known, Ok)
    assert known.value.cursor == mine

    assert isinstance(open_record(tmp_path, "2026-01-01T000000Z_gone"), Err)


@pytest.mark.parametrize("named", ["local-9", None])
def test_a_cursor_that_names_another_record_is_not_used(tmp_path: Path, named: str | None) -> None:
    """A cursor file copied or renamed by hand: well formed, and about another record."""
    made = writer(tmp_path)
    other = Cursor(local_ref=named, session_id="cloud-7", ticks_offset=44, state="complete")
    assert isinstance(store(made.path, other), Ok)

    opened = open_record(tmp_path, made.path.name)

    assert isinstance(opened, Ok)
    assert opened.value.cursor == UnreadableCursor(OTHER_RECORD)
    # And the record is owed, though that cursor says nothing of it is.
    assert owed_records(tmp_path) == Ok(Listed((made.path.name,)))


def record_made(root: Path, stamp: str, ref: str) -> Writer:
    started = f"{stamp[:10]}T{stamp[11:13]}:{stamp[13:15]}:{stamp[15:17]}Z"
    described = dataclasses.replace(
        manifest(),
        local_ref=ref,
        started_at=started,
        clocks=dataclasses.replace(manifest().clocks, utc_start=started),
    )
    created = Writer.create(root, described)
    assert isinstance(created, Ok)
    return created.value


def record_named(root: Path, stamp: str, ref: str) -> Path:
    return record_made(root, stamp, ref).path


def test_the_records_still_owed_are_those_with_a_cursor_that_is_not_complete(
    tmp_path: Path,
) -> None:
    """Oldest first. A record that was there, cursor-less, at the first listing is left alone."""
    older_software = record_named(tmp_path, "2026-10-02T080000Z", "older-software")
    assert owed_records(tmp_path) == Ok(Listed((), set_aside=1))
    sent = record_named(tmp_path, "2026-10-01T080000Z", "sent")
    half = record_named(tmp_path, "2026-10-03T080000Z", "half")
    torn = record_named(tmp_path, "2026-10-04T080000Z", "torn")
    untouched = record_named(tmp_path, "2026-10-05T080000Z", "untouched")
    assert isinstance(store(sent, Cursor(local_ref="sent", state="complete", end="sent")), Ok)
    assert isinstance(store(half, Cursor(local_ref="half", session_id="c3", ticks_offset=900)), Ok)
    cursor_of(torn).write_bytes(b'{"schema_version":1,"ticks_of')
    assert isinstance(store(untouched, Cursor(local_ref="untouched")), Ok)
    orphan = tmp_path / "2026-09-01T080000Z_purged.sync.json"
    orphan.write_text("{}", encoding="utf-8")

    owed = owed_records(tmp_path)

    assert owed == Ok(Listed((half.name, torn.name, untouched.name)))
    assert older_software.is_dir()
    assert not orphan.exists()
    # A record the synchronisation left alone still reads as its writer left it.
    assert isinstance(read(older_software), Ok)
    # What was there without a cursor is listed once, privately, next to the records.
    assert load_baseline(tmp_path) == Baseline(left_alone=(older_software.name,))
    assert (tmp_path / BASELINE_NAME).stat().st_mode & 0o777 == 0o600


def test_a_record_made_since_the_first_listing_and_never_tied_to_a_cursor_is_owed(
    tmp_path: Path,
) -> None:
    """Killed in its first second, or the disk refused its cursor: it is not lost for that."""
    before = record_named(tmp_path, "2026-10-01T080000Z", "before")
    assert owed_records(tmp_path) == Ok(Listed((), set_aside=1))

    since = record_named(tmp_path, "2026-10-02T080000Z", "since")
    open_now = record_named(tmp_path, "2026-10-03T080000Z", "open-now")

    # The record the console has open is the running session's own to send.
    assert owed_records(tmp_path, open_now.name) == Ok(Listed((since.name,)))
    assert owed_records(tmp_path) == Ok(Listed((since.name, open_now.name)))
    assert load_baseline(tmp_path) == Baseline(left_alone=(before.name,))


def test_the_record_open_at_the_first_listing_is_not_taken_for_an_older_one(
    tmp_path: Path,
) -> None:
    before = record_named(tmp_path, "2026-10-01T080000Z", "before")
    open_now = record_named(tmp_path, "2026-10-02T080000Z", "open-now")

    assert owed_records(tmp_path, open_now.name) == Ok(Listed((), set_aside=1))

    assert load_baseline(tmp_path) == Baseline(left_alone=(before.name,))
    # Never tied to its cursor in the end: it is found at the next start.
    assert owed_records(tmp_path) == Ok(Listed((open_now.name,)))


@pytest.mark.parametrize("lost", ["truncated", "deleted"])
def test_a_list_of_older_records_that_is_lost_is_made_again_and_what_it_sets_aside_is_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, lost: str
) -> None:
    """Nothing is sent that might be older than the synchronisation: what has no cursor now.

    A record made since and never tied to a cursor was waiting to be sent: it
    is set aside with the others, and counted, so that it can be said.
    """
    before = record_named(tmp_path, "2026-10-01T080000Z", "before")
    assert owed_records(tmp_path) == Ok(Listed((), set_aside=1))
    sent = record_named(tmp_path, "2026-10-02T080000Z", "sent")
    assert isinstance(store(sent, Cursor(local_ref="sent", state="complete", end="sent")), Ok)
    waiting = record_named(tmp_path, "2026-10-03T080000Z", "waiting")
    assert owed_records(tmp_path) == Ok(Listed((waiting.name,)))
    if lost == "truncated":
        (tmp_path / BASELINE_NAME).write_bytes(b'{"schema_version":1,"left_al')
    else:
        (tmp_path / BASELINE_NAME).unlink()

    with caplog.at_level(logging.WARNING, logger="src.record.upload"):
        owed = owed_records(tmp_path)

    assert owed == Ok(Listed((), set_aside=2))
    assert "it is made now, and the 2 records that have no cursor are set aside" in caplog.text
    assert load_baseline(tmp_path) == Baseline(left_alone=(before.name, waiting.name))
    # Said once: the list is there again.
    assert owed_records(tmp_path) == Ok(Listed(()))


def test_whenever_the_list_of_older_records_is_made_what_it_sets_aside_is_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The first time too, and with no cursor anywhere: nothing tells a list never made
    from one that was deleted, so the count is always given, to be said."""
    before = record_named(tmp_path, "2026-10-01T080000Z", "before")
    too = record_named(tmp_path, "2026-10-02T080000Z", "before-too")
    in_the_logbook(tmp_path)

    with caplog.at_level(logging.WARNING, logger="src.record.upload"):
        owed = owed_records(tmp_path)

    assert owed == Ok(Listed((), set_aside=2))
    assert "the 2 records that have no cursor are set aside and not sent" in caplog.text
    # The logbook is no record: neither counted nor put on the list.
    assert load_baseline(tmp_path) == Baseline(left_alone=(before.name, too.name))
    (tmp_path / BASELINE_NAME).unlink()
    assert owed_records(tmp_path) == Ok(Listed((), set_aside=2))


def in_the_logbook(root: Path) -> Path:
    """What the console writes of itself while no session is recorded, next to the records."""
    logbook = root / DIRECTORY
    logbook.mkdir()
    for name in (ACTIVE, PREVIOUS):
        (logbook / name).write_text('{"kind":"refusal","detail":"refused"}\n', encoding="utf-8")
    return logbook


def test_the_logbook_next_to_the_records_is_never_listed_as_owed(tmp_path: Path) -> None:
    """Out-of-session events are not a session's: with or without a list of older records,
    and whatever else stands next to the records (a marker of deposit, a record closing)."""
    logbook = in_the_logbook(tmp_path)
    assert owed_records(tmp_path) == Ok(Listed(()))
    assert load_baseline(tmp_path) == Baseline()

    since = record_named(tmp_path, "2026-10-02T080000Z", "since")
    (tmp_path / f"{since.name}.deposit.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".record-k3j2h1").mkdir()

    assert owed_records(tmp_path) == Ok(Listed((since.name,)))
    assert sorted(path.name for path in logbook.iterdir()) == [PREVIOUS, ACTIVE]
    assert not (tmp_path / f"{DIRECTORY}{CURSOR_SUFFIX}").exists()


def deposited(root: Path, stamp: str, ref: str, cursor: Cursor) -> Path:
    """A record closed, synchronised as ``cursor`` says, and confirmed deposited off the machine."""
    made = record_made(root, stamp, ref)
    assert isinstance(made.tick(row()), Ok)
    closed = made.close(ManualClock(), "operator_stop")
    assert isinstance(closed, Ok)
    assert isinstance(store(made.path, dataclasses.replace(cursor, local_ref=ref)), Ok)
    assert isinstance(confirm_deposit(made.path, "kg2storage", UnixMillis(START_MS)), Ok)
    return made.path


def test_a_purge_between_two_starts_and_the_sweep_of_cursors_do_not_touch_each_other_s_files(
    tmp_path: Path,
) -> None:
    """The retention removes a deposited record with its marker, and nothing of the
    synchronisation; at the next start the synchronisation removes the cursor of the record
    that is gone, and nothing of the retention. What is still owed is still listed."""
    whole = deposited(tmp_path, "2026-10-01T080000Z", "whole", Cursor(state="complete", end="sent"))
    half = deposited(tmp_path, "2026-10-02T080000Z", "half", Cursor(ticks_offset=10))
    kept = record_named(tmp_path, "2026-10-03T080000Z", "kept")
    assert isinstance(store(kept, Cursor(local_ref="kept", ticks_offset=5)), Ok)
    logbook = in_the_logbook(tmp_path)
    assert owed_records(tmp_path) == Ok(Listed((half.name, kept.name)))
    listed = load_baseline(tmp_path)

    report = purge(tmp_path, UnixMillis(START_MS), 0)

    assert report == PurgeReport(removed=(whole.name, half.name), kept=1, failed=())
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [
            cursor_of(whole).name,
            cursor_of(half).name,
            kept.name,
            cursor_of(kept).name,
            BASELINE_NAME,
            DIRECTORY,
        ]
    ), "the record and its marker are gone; the cursors and the list are not the retention's"

    assert owed_records(tmp_path) == Ok(Listed((kept.name,)))

    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [kept.name, cursor_of(kept).name, BASELINE_NAME, DIRECTORY]
    )
    assert load(kept) == Cursor(local_ref="kept", ticks_offset=5)
    assert load_baseline(tmp_path) == listed
    assert sorted(path.name for path in logbook.iterdir()) == [PREVIOUS, ACTIVE]
    # And the other way round: a purge after that start finds nothing more to do.
    assert purge(tmp_path, UnixMillis(START_MS), 0) == PurgeReport(removed=(), kept=1, failed=())
    assert owed_records(tmp_path) == Ok(Listed((kept.name,)))


def test_a_list_of_older_records_made_of_nothing_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="src.record.upload"):
        owed = owed_records(tmp_path)

    assert owed == Ok(Listed(()))
    assert caplog.text == ""
    assert load_baseline(tmp_path) == Baseline()


def test_a_list_of_older_records_that_cannot_be_written_is_made_again_at_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    before = record_named(tmp_path, "2026-10-01T080000Z", "before")

    def refused(_root: Path, _baseline: Baseline) -> Result[None, CursorError]:
        return Err(CursorError("OSError:ENOSPC"))

    with monkeypatch.context() as patched:
        patched.setattr("src.record.upload.store_baseline", refused)
        with caplog.at_level(logging.WARNING, logger="src.record.upload"):
            owed = owed_records(tmp_path)

    assert owed == Ok(Listed((), set_aside=1))
    assert "could not be written (OSError:ENOSPC)" in caplog.text
    assert load_baseline(tmp_path) is None
    assert owed_records(tmp_path) == Ok(Listed((), set_aside=1))
    assert load_baseline(tmp_path) == Baseline(left_alone=(before.name,))
    assert isinstance(store_baseline(tmp_path, Baseline()), Ok)


def test_a_complete_cursor_next_to_a_record_that_cannot_be_read_is_left_be(tmp_path: Path) -> None:
    sent = record_named(tmp_path, "2026-10-01T080000Z", "sent")
    assert isinstance(store(sent, Cursor(local_ref="sent", state="complete", end="sent")), Ok)
    (sent / "manifest.json").write_bytes(b"{")

    assert owed_records(tmp_path) == Ok(Listed(()))


def test_the_reference_of_a_session_started_at_the_machine_is_in_the_name_of_its_record(
    tmp_path: Path,
) -> None:
    """What lets such a session be declared before any file of its record is read."""
    made = writer(tmp_path)
    head = read_head(made.path)
    assert isinstance(head, Ok)

    assert reference_in(made.path.name) == head.value.local_ref == "local-4"
    assert reference_in(record_named(tmp_path, "2026-10-01T080000Z", "a_b-c").name) == "a_b-c"
    for other in ("logbook", ".sync-baseline.json", "2026-10-01T080000Z", "2026-10-01T080000Z_"):
        assert reference_in(other) is None


def test_an_unusable_cursor_is_told_from_no_cursor_when_a_record_is_opened(tmp_path: Path) -> None:
    made = writer(tmp_path)
    cursor_of(made.path).write_bytes(b"{")

    opened = open_record(tmp_path, made.path.name)

    assert isinstance(opened, Ok)
    assert opened.value.cursor == UnreadableCursor("invalid")


def test_a_records_directory_that_cannot_be_listed_owes_nothing_it_can_name(
    tmp_path: Path,
) -> None:
    owed = owed_records(tmp_path / "absent")

    assert isinstance(owed, Err)
    assert owed.error.detail == "FileNotFoundError:ENOENT"
