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
* which records the dashboard is still owed.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record.cursor import Cursor, NoCursor, UnreadableCursor, cursor_of, store
from src.record.reader import read
from src.record.schema import EndObservation, Event, EventKind
from src.record.upload import (
    INTERRUPTED,
    MAX_EVENT_DETAIL,
    MAX_EVENTS,
    MAX_POINTS,
    MISALIGNED,
    Ended,
    open_record,
    owed_records,
    read_batch,
    read_events,
    read_head,
    read_last_tick_ms,
    read_points,
)
from src.record.writer import TICK_COLUMNS, Writer
from src.result import Err, Ok
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


def test_bytes_that_are_no_line_at_all_are_passed_over_a_chunk_at_a_time(tmp_path: Path) -> None:
    made = writer(tmp_path)
    path = made.path / "ticks.csv"
    start = path.stat().st_size
    with path.open("ab") as handle:
        handle.write(b"x" * 250)

    found = read_points(made.path, start_ms=START_MS, offset=start, after_t=None, chunk=100)

    assert isinstance(found, Ok)
    assert (found.value.points, found.value.offset, found.value.more) == ((), start + 100, True)


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

    assert isinstance(store(made.path, Cursor(session_id="cloud-1", ticks_offset=44)), Ok)
    known = open_record(tmp_path, name)
    assert isinstance(known, Ok)
    assert known.value.cursor == Cursor(session_id="cloud-1", ticks_offset=44)

    assert isinstance(open_record(tmp_path, "2026-01-01T000000Z_gone"), Err)


def record_named(root: Path, stamp: str, ref: str) -> Path:
    started = f"{stamp[:10]}T{stamp[11:13]}:{stamp[13:15]}:{stamp[15:17]}Z"
    described = dataclasses.replace(
        manifest(),
        local_ref=ref,
        started_at=started,
        clocks=dataclasses.replace(manifest().clocks, utc_start=started),
    )
    created = Writer.create(root, described)
    assert isinstance(created, Ok)
    return created.value.path


def test_the_records_still_owed_are_those_with_a_cursor_that_is_not_complete(
    tmp_path: Path,
) -> None:
    """Oldest first. A record with no cursor was never opened by the synchronisation."""
    sent = record_named(tmp_path, "2026-10-01T080000Z", "sent")
    never_opened = record_named(tmp_path, "2026-10-02T080000Z", "older-software")
    half = record_named(tmp_path, "2026-10-03T080000Z", "half")
    torn = record_named(tmp_path, "2026-10-04T080000Z", "torn")
    untouched = record_named(tmp_path, "2026-10-05T080000Z", "untouched")
    assert isinstance(store(sent, Cursor(state="complete", end="sent")), Ok)
    assert isinstance(store(half, Cursor(session_id="cloud-3", ticks_offset=900)), Ok)
    cursor_of(torn).write_bytes(b'{"schema_version":1,"ticks_of')
    assert isinstance(store(untouched, Cursor()), Ok)
    orphan = tmp_path / "2026-09-01T080000Z_purged.sync.json"
    orphan.write_text("{}", encoding="utf-8")

    owed = owed_records(tmp_path)

    assert owed == Ok((half.name, torn.name, untouched.name))
    assert never_opened.is_dir()
    assert not orphan.exists()
    # A record the synchronisation left alone still reads as its writer left it.
    assert isinstance(read(never_opened), Ok)


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
