"""A session record, read back for the dashboard: its 1 Hz telemetry and its events.

What the dashboard receives of a session is no longer what the console held in
memory while it ran: it is what the record on disk says, read from the place
the dashboard last acknowledged (:mod:`src.record.cursor`). A console that was
killed in the middle of a session therefore owes the dashboard exactly what
its disk holds, and can send it after it restarts.

Two streams are read, each from a byte offset, a bounded amount at a time:

* ``ticks.csv`` (5 Hz) gives the **1 Hz telemetry**: the first tick of each
  whole second of the session. The rule depends on the record alone, so
  reading it again, from the same place or from the beginning, gives the same
  points with the same dates, which is what lets the dashboard recognise one it
  already has;
* ``events.jsonl`` gives the **events**, each with its rank in the file.

Dates
-----
A tick is dated in seconds since the session started, on the console's
monotonic clock. What is sent is that time added to the start the console
wrote in the manifest when it armed the session: one reading of the wall
clock, at the start, then only durations. A wall clock that is corrected in
the middle of a session (a Raspberry Pi has no real-time clock, and gets the
time when the network returns) therefore moves no point: the whole session
stays on one axis, the one the dashboard is told the start of.

Only complete lines are read. A last line still being written is left for the
next reading; a complete line that cannot be understood is passed over and
counted, so that one bad line never holds back what follows it.

Everything here touches the disk and is bounded per call: it runs on the
record I/O threads (:class:`~src.record.export.RecordIo`), never on the event
loop.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

from src.record.codec import IDENTIFIER
from src.record.cursor import (
    BASELINE_NAME,
    Baseline,
    Cursor,
    CursorRead,
    NoCursor,
    UnreadableCursor,
    load,
    load_baseline,
    store_baseline,
    sweep,
)
from src.record.retention import records
from src.record.rows import JsonValue
from src.record.schema import Profile, RecordError
from src.record.writer import MANIFEST, TICK_COLUMNS, describe_failure, describe_os_error
from src.result import Err, Ok, Result

_logger: Final[logging.Logger] = logging.getLogger(__name__)

MAX_POINTS: Final[int] = 300
"""Telemetry points read, and sent, at a time: five minutes of a session."""

MAX_EVENTS: Final[int] = 200
"""Events read, and sent, at a time: what the dashboard accepts in one request."""

MAX_EVENT_DETAIL: Final[int] = 2000
"""Characters of an event's text the dashboard accepts; a longer one is cut to this."""

READ_CHUNK: Final[int] = 512 * 1024
"""Bytes of a stream read in one call: about three hundred seconds of ticks."""

TAIL_CHUNK: Final[int] = 64 * 1024
"""Bytes read from the end of ``ticks.csv`` to date the last tick of a record cut short."""

INTERRUPTED: Final[str] = "interrupted"
"""The end reason of a record that was never closed: the console stopped before the session."""

MISALIGNED: Final[str] = "cursor_misaligned"
"""A cursor that does not point at the start of a line of the stream it is about."""

OTHER_RECORD: Final[str] = "of another record"
"""A cursor that names another record than the one it sits next to."""

_EVENT_KIND: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ACTOR_MAX: Final[int] = 64

_T: Final[int] = TICK_COLUMNS.index("t")
_PHASE: Final[int] = TICK_COLUMNS.index("phase")
_MOTOR: Final[int] = TICK_COLUMNS.index("measured_motor_rpm")
_OUTPUT: Final[int] = TICK_COLUMNS.index("output_rpm")
_SETPOINT: Final[int] = TICK_COLUMNS.index("setpoint_motor_rpm")
_G: Final[int] = TICK_COLUMNS.index("g_reference")
_ACTION: Final[int] = TICK_COLUMNS.index("safety_action")
_BPM: Final[int] = TICK_COLUMNS.index("hr_live")

type Wire = Mapping[str, JsonValue]
"""One point or one event, as the dashboard's route takes it."""


# =========================================================================
# What the manifest says
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Ended:
    """How a record was closed."""

    reason: str
    """The manifest's ``end_reason``: the runtime's own word, or ``interrupted``."""

    stop_reason: str | None
    """The words of the stop that ended it, names and addresses already removed."""

    at_ms: int | None
    """When, on the session's own axis (unix ms); ``None`` when the closing does not say."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Head:
    """What the dashboard is told of a session, as its manifest holds it. No name is in it."""

    local_ref: str
    remote_id: str | None
    """The dashboard's identifier, for a session it launched."""

    kind: Literal["auto", "manual"]
    occupancy: Literal["bench", "occupied"]
    operator: str
    """The operator's stable alias (``op-...``): the record never holds the name."""

    profile: Profile | None
    start_ms: int
    """The start of the session as the console dated it (unix ms): the origin of its axis."""

    monotonic_start: float
    """That same instant on the monotonic clock of the boot that recorded it."""

    closed: Ended | None
    """``None`` while the manifest is open: the session runs, or was cut short."""


def read_head(record: Path) -> Result[Head, RecordError]:
    """The manifest of ``record``, for the dashboard. Blocking; never raises."""
    try:
        manifest = MANIFEST.validate_json((record / "manifest.json").read_bytes())
        start_ms = _millis(manifest.clocks.utc_start)
    except (OSError, ValueError) as error:
        return Err(RecordError("read", describe_failure(error)))
    closed: Ended | None = None
    if manifest.ended_at is not None:
        observed = manifest.end_observation
        closed = Ended(
            reason=manifest.end_reason or INTERRUPTED,
            stop_reason=None if observed is None else observed.stop_reason,
            at_ms=None if observed is None else start_ms + round(observed.t * 1000),
        )
    return Ok(
        Head(
            local_ref=manifest.local_ref,
            remote_id=manifest.session_id,
            kind=manifest.kind,
            occupancy=manifest.occupancy,
            operator=manifest.operator,
            profile=manifest.profile,
            start_ms=start_ms,
            monotonic_start=float(manifest.clocks.monotonic_start),
            closed=closed,
        )
    )


def _millis(stamp: str) -> int:
    """An ISO 8601 UTC stamp of the manifest, in unix milliseconds."""
    return round(datetime.fromisoformat(stamp).timestamp() * 1000)


# =========================================================================
# Complete lines of an append-only stream, from a byte offset
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Lines:
    """The complete lines found from an offset: each with the offset just after it."""

    lines: tuple[tuple[bytes, int], ...]
    end: int
    """Where the next reading starts when every line above was taken."""

    full: bool
    """Whether the chunk was read to its limit: the stream may hold more."""

    mid: bool = False
    """``end`` is inside a line too long to be one of the format, which is being passed over."""

    overlong: int = 0
    """1 when such a line began in this reading: it is counted once, where it begins."""


def _lines(path: Path, offset: int, chunk: int, *, mid: bool = False) -> _Lines:
    """Read at most ``chunk`` bytes of ``path`` from ``offset``. Raises ``OSError``, ``ValueError``.

    A whole chunk without an end of line is no line of this format (zeroes
    left by a power cut, for instance). It is passed over: this reading and
    those that follow, each from where the one before stopped (``mid``), go
    on to its end, and the lines after it are read as any other.

    ``ValueError``: the offset is not one a reading of this file can have
    given (past its end, or in the middle of a line that is not being passed
    over), so it was not written about it.
    """
    with path.open("rb") as handle:
        if mid:
            if offset > os.fstat(handle.fileno()).st_size:
                raise ValueError(MISALIGNED)
            handle.seek(offset)
        elif offset > 0:
            handle.seek(offset - 1)
            if handle.read(1) != b"\n":
                raise ValueError(MISALIGNED)
        data = handle.read(chunk)
    full = len(data) == chunk
    start = 0
    if mid:
        ends = data.find(b"\n")
        if ends < 0:
            return _Lines((), offset + len(data), full, mid=True)
        start = ends + 1
    base = offset + start
    cut = data.rfind(b"\n") + 1
    if cut <= start:
        if full and start == 0:
            return _Lines((), offset + len(data), full, mid=True, overlong=1)
        # A last line still being written, or one the next reading will judge whole.
        return _Lines((), base, full)
    lines: list[tuple[bytes, int]] = []
    position = base
    for raw in data[start:cut].split(b"\n")[:-1]:
        position += len(raw) + 1
        lines.append((raw, position))
    return _Lines(tuple(lines), position, full)


# =========================================================================
# ticks.csv -> telemetry at 1 Hz
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Points:
    """Telemetry read from one place of ``ticks.csv``."""

    points: tuple[Wire, ...]
    offset: int
    """Where the next reading starts once these points are acknowledged."""

    last_t: int | None
    """``t`` of the last point above; ``None`` when there is none."""

    more: bool
    """Whether the stream may already hold more than was read."""

    skipped: int
    """Lines that are not ticks of this format: passed over."""

    mid_line: bool = False
    """``offset`` is inside a line too long to be a tick, which is being passed over."""


def _fields(raw: bytes) -> Sequence[str] | None:
    """The columns of one line of ``ticks.csv``, or ``None`` when it is not one."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields = next(csv.reader([text]), None)
    return fields if fields is not None and len(fields) == len(TICK_COLUMNS) else None


@dataclass(frozen=True, slots=True)
class _Tick:
    """One line of ``ticks.csv``: how far into the session it is, and its point."""

    millis: int
    point: Wire


def _tick(fields: Sequence[str], start_ms: int) -> _Tick | None:
    """One tick as the dashboard takes a point, or ``None`` when the line cannot be read."""
    try:
        t = float(fields[_T])
        output = float(fields[_OUTPUT])
        g_load = float(fields[_G])
        motor = int(fields[_MOTOR])
        setpoint = int(fields[_SETPOINT])
        bpm = None if fields[_BPM] == "" else int(fields[_BPM])
    except ValueError:
        return None
    if not (math.isfinite(t) and math.isfinite(output) and math.isfinite(g_load)):
        return None
    millis = round(t * 1000)
    point: dict[str, JsonValue] = {
        "t": start_ms + millis,
        "elapsedS": round(t, 2),
        "phase": fields[_PHASE],
        "motorRpm": motor,
        "outputRpm": round(output, 3),
        "setpointMotorRpm": setpoint,
        "gLoad": round(g_load, 4),
        "safetyAction": fields[_ACTION].lower(),
    }
    if bpm is not None:
        point["bpm"] = bpm
    return _Tick(millis, point)


def read_points(
    record: Path,
    *,
    start_ms: int,
    offset: int,
    after_t: int | None,
    mid_line: bool = False,
    limit: int = MAX_POINTS,
    chunk: int = READ_CHUNK,
) -> Result[Points, RecordError]:
    """At most ``limit`` 1 Hz points of ``record`` from ``offset``. Blocking; never raises.

    ``after_t`` is the ``t`` of the last point already acknowledged: the tick
    kept for a second of the session is the first one of that second, and no
    other tick of a second already sent is sent.
    """
    try:
        found = _lines(record / "ticks.csv", offset, chunk, mid=mid_line)
    except OSError as error:
        return Err(RecordError("read", describe_os_error(error)))
    except ValueError:
        return Err(RecordError("read", MISALIGNED))
    lines = found.lines
    skipped = found.overlong
    if offset == 0 and lines:
        header = _fields(lines[0][0])
        if header is None or tuple(header) != TICK_COLUMNS:
            return Err(RecordError("read", "columns"))
        lines = lines[1:]
    second = -1 if after_t is None else (after_t - start_ms) // 1000
    points: list[Wire] = []
    last_t: int | None = None
    for raw, end in lines:
        fields = _fields(raw)
        tick = None if fields is None else _tick(fields, start_ms)
        if tick is None:
            skipped += 1
            continue
        if tick.millis < 0 or tick.millis // 1000 <= second:
            # Before the start (the idle ticks a simulation writes), or another
            # tick of a second that already has its point.
            continue
        second = tick.millis // 1000
        points.append(tick.point)
        last_t = start_ms + tick.millis
        if len(points) == limit:
            return Ok(
                Points(points=tuple(points), offset=end, last_t=last_t, more=True, skipped=skipped)
            )
    return Ok(
        Points(
            points=tuple(points),
            offset=found.end,
            last_t=last_t,
            more=found.full,
            skipped=skipped,
            mid_line=found.mid,
        )
    )


def read_last_tick_ms(record: Path, chunk: int = TAIL_CHUNK) -> Result[int | None, RecordError]:
    """How far into the session the last complete tick of ``record`` is, in ms.

    ``None``: the record holds no tick. For a record that was never closed,
    it is the last instant the console is known to have been running the
    session. Blocking; never raises.
    """
    path = record / "ticks.csv"
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(max(0, size - chunk))
            data = handle.read(chunk)
    except OSError as error:
        return Err(RecordError("read", describe_os_error(error)))
    for raw in reversed(data[: data.rfind(b"\n") + 1].split(b"\n")):
        fields = _fields(raw)
        tick = None if fields is None else _tick(fields, 0)
        if tick is not None:
            return Ok(tick.millis)
    return Ok(None)


# =========================================================================
# events.jsonl -> events, each with its rank
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Events:
    """Events read from one place of ``events.jsonl``."""

    events: tuple[Wire, ...]
    offset: int
    last_seq: int
    """Rank of the last line taken, readable or not; unchanged when none was."""

    more: bool
    skipped: int
    """Lines that are not events of this format. Each still has its rank."""

    mid_line: bool = False
    """``offset`` is inside a line too long to be an event, which is being passed over."""


def _event(raw: bytes, seq: int, start_ms: int) -> Wire | None:
    """One line of ``events.jsonl`` as the dashboard takes it, or ``None`` if it is not one."""
    try:
        parsed: object = json.loads(raw)  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    document = cast("Mapping[str, object]", parsed)
    t = document.get("t")
    kind = document.get("kind")
    detail = document.get("detail")
    actor = document.get("actor")
    if (
        not isinstance(t, int | float)
        or isinstance(t, bool)
        or not math.isfinite(t)
        or not isinstance(kind, str)
        or _EVENT_KIND.fullmatch(kind) is None
        or not isinstance(detail, str)
        or not isinstance(actor, str)
        or IDENTIFIER.fullmatch(actor) is None
        or len(actor) > _ACTOR_MAX
    ):
        return None
    return {
        "seq": seq,
        "t": start_ms + round(t * 1000),
        "kind": kind,
        "detail": detail[:MAX_EVENT_DETAIL],
        "actor": actor,
    }


def read_events(
    record: Path,
    *,
    start_ms: int,
    offset: int,
    last_seq: int,
    mid_line: bool = False,
    limit: int = MAX_EVENTS,
    chunk: int = READ_CHUNK,
) -> Result[Events, RecordError]:
    """At most ``limit`` events of ``record`` from ``offset``. Blocking; never raises.

    The rank of an event is the rank of its line in the file, counted from 0:
    it never changes, the file is only ever appended to. A line that is not an
    event keeps its rank and is passed over.
    """
    try:
        found = _lines(record / "events.jsonl", offset, chunk, mid=mid_line)
    except OSError as error:
        return Err(RecordError("read", describe_os_error(error)))
    except ValueError:
        return Err(RecordError("read", MISALIGNED))
    events: list[Wire] = []
    # A line too long to be an event takes its rank where it begins, like any other.
    skipped = found.overlong
    seq = last_seq + found.overlong
    for raw, end in found.lines:
        seq += 1
        event = _event(raw, seq, start_ms)
        if event is None:
            skipped += 1
            continue
        events.append(event)
        if len(events) == limit:
            return Ok(
                Events(events=tuple(events), offset=end, last_seq=seq, more=True, skipped=skipped)
            )
    return Ok(
        Events(
            events=tuple(events),
            offset=found.end,
            last_seq=seq,
            more=found.full,
            skipped=skipped,
            mid_line=found.mid,
        )
    )


# =========================================================================
# One reading of a record, and the records still owed
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Batch:
    """What one reading of a record gives: its state, and what follows the cursor."""

    closed: Ended | None
    points: Points
    events: Events
    last_tick_ms: int | None
    """Into the session, the last tick on disk; read only when asked for."""


def read_batch(
    record: Path, *, start_ms: int, cursor: Cursor, with_last_tick: bool
) -> Result[Batch, RecordError]:
    """Read the manifest's closing, then the points and the events after ``cursor``.

    One call, so that one thread of the record I/O does it. Blocking; never
    raises.
    """
    head = read_head(record)
    if isinstance(head, Err):
        return Err(head.error)
    points = read_points(
        record,
        start_ms=start_ms,
        offset=cursor.ticks_offset,
        after_t=cursor.last_t,
        mid_line=cursor.ticks_mid_line,
    )
    if isinstance(points, Err):
        return Err(points.error)
    events = read_events(
        record,
        start_ms=start_ms,
        offset=cursor.events_offset,
        last_seq=cursor.last_seq,
        mid_line=cursor.events_mid_line,
    )
    if isinstance(events, Err):
        return Err(events.error)
    last_tick: int | None = None
    if with_last_tick:
        tail = read_last_tick_ms(record)
        if isinstance(tail, Err):
            return Err(tail.error)
        last_tick = tail.value
    return Ok(
        Batch(
            closed=head.value.closed,
            points=points.value,
            events=events.value,
            last_tick_ms=last_tick,
        )
    )


@dataclass(frozen=True, slots=True)
class Opened:
    """A record still owed, with its manifest and what its cursor says."""

    record: Path
    head: Head
    cursor: CursorRead
    last_tick_ms: int | None
    """How far into the session its last tick is; ``None`` when it holds no tick."""


def open_record(root: Path, name: str) -> Result[Opened, RecordError]:
    """The record called ``name`` directly under ``root``. Blocking; never raises."""
    record = root / name
    head = read_head(record)
    if isinstance(head, Err):
        return Err(head.error)
    last_tick = read_last_tick_ms(record)
    if isinstance(last_tick, Err):
        return Err(last_tick.error)
    cursor = load(record)
    if isinstance(cursor, Cursor) and cursor.local_ref != head.value.local_ref:
        cursor = UnreadableCursor(OTHER_RECORD)
    return Ok(Opened(record, head.value, cursor, last_tick.value))


def owed_records(root: Path, current: str | None = None) -> Result[tuple[str, ...], RecordError]:
    """The records under ``root`` the dashboard is still owed, oldest first.

    Owed: a record whose cursor does not say ``complete``, or cannot be read,
    or names another record; and a record made since the synchronisation
    first listed this directory that has no cursor at all (it was never tied
    to one: :mod:`src.record.cursor`). Not owed: a record that was already
    there, cursor-less, at that first listing, and ``current``, the record the
    console has open, which the running session sends itself.

    What no record owns any more is swept away on the way. Blocking; never
    raises.
    """
    try:
        sweep(root)
        found = records(root)
    except OSError as error:
        return Err(RecordError("read", describe_os_error(error)))
    left_alone = _left_alone(root, found, current)
    owed: list[str] = []
    for record in found:
        if record.name == current:
            continue
        cursor = load(record)
        if isinstance(cursor, NoCursor):
            if record.name not in left_alone:
                owed.append(record.name)
            continue
        if isinstance(cursor, Cursor) and cursor.state == "complete" and _is_about(cursor, record):
            continue
        owed.append(record.name)
    return Ok(tuple(owed))


def _left_alone(root: Path, found: Sequence[Path], current: str | None) -> frozenset[str]:
    """The records made before the synchronisation existed here: listed once, then kept."""
    baseline = load_baseline(root)
    if baseline is None:
        if (root / BASELINE_NAME).exists():
            _logger.warning(
                "dashboard: the list of the records made before the synchronisation cannot "
                "be read: it is made again from the records that have no cursor now"
            )
        baseline = Baseline(
            left_alone=tuple(
                record.name
                for record in found
                if record.name != current and isinstance(load(record), NoCursor)
            )
        )
        wrote = store_baseline(root, baseline)
        if isinstance(wrote, Err):
            _logger.warning(
                "dashboard: the list of the records made before the synchronisation could "
                "not be written (%s): it is made again at the next start",
                wrote.error.detail,
            )
    return frozenset(baseline.left_alone)


def _is_about(cursor: Cursor, record: Path) -> bool:
    """Whether ``cursor`` names ``record``; a record whose manifest cannot be read is left be."""
    head = read_head(record)
    return isinstance(head, Err) or head.value.local_ref == cursor.local_ref
