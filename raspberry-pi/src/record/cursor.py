"""The synchronisation cursor of a session record: how far the dashboard has acknowledged it.

A session record is sent to the dashboard by reading it back from disk
(:mod:`src.record.upload`). What was sent AND acknowledged is remembered in one
small file per record, so that a console that restarts, by choice or not,
resumes where the dashboard stopped acknowledging and sends nothing it does
not have to.

Where it lives
--------------
NEXT TO the record directory, never inside it: ``<record directory
name>.sync.json``, like the deposit marker of :mod:`src.record.retention`. The
record's own files are the format's (``docs/enregistrement.md``): its checksums
index every one of them, and a reader reports any other file as a mismatch.

What it guarantees, and what it does not
----------------------------------------
* It is written **atomically**: a private temporary file in the same
  directory, flushed to the device, then renamed over the cursor. A reader
  sees the old cursor or the new one, never half of one.
* It is **private** to the service user (mode 600): it names a session of the
  dashboard. The file is created by the record writer's own call
  (:func:`~src.record.writer.create_private`), which gives the mode to the
  system call that creates it, as for every file of a record or next to one.
* It is **not the truth**, only a saving. A cursor that is lost, truncated or
  unreadable costs a second sending from the beginning of the record, which the
  dashboard stores once (it knows a point by ``(session, t)`` and an event by
  ``(session, seq)``). Nothing is ever lost or doubled by it.

A cursor names its record (``local_ref``, the reference the manifest carries):
one that names another record, a file copied or renamed by hand, is not used.

A record with NO cursor at all is one of two things, told apart by the list
this module keeps next to the records (:data:`BASELINE_NAME`): the records
that were already there, cursor-less, the first time the synchronisation
listed the directory. Those were made before it existed here, and are left
alone. Any other record without a cursor was made since and never tied to one
(the console was killed in its first second, or the disk refused the cursor):
it is sent, from its beginning, at the next start of the console.

Everything here touches the disk: it is called from the record I/O threads
(:class:`~src.record.export.RecordIo`), never from the event loop.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from pydantic import ConfigDict, TypeAdapter
from pydantic.dataclasses import dataclass as validated

from src.record.retention import RECORD_NAME
from src.record.writer import create_private, describe_os_error
from src.result import Err, Ok, Result

CURSOR_SUFFIX: Final[str] = ".sync.json"
"""The cursor of a record is ``<record directory name>.sync.json``, next to it."""

TEMP_PREFIX: Final[str] = ".sync-"
TEMP_SUFFIX: Final[str] = ".tmp"
"""A cursor being written. One left behind by a console that was killed is swept away."""

BASELINE_NAME: Final[str] = ".sync-baseline.json"
"""Next to the records: which of them were there before the synchronisation first looked."""

BOOT_ID_PATH: Final[Path] = Path("/proc/sys/kernel/random/boot_id")
"""Linux names each start of the system here. Absent elsewhere."""

_BOOT_ID: Final[re.Pattern[str]] = re.compile(r"[0-9A-Fa-f-]{8,64}")


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class Cursor:
    """What the dashboard has acknowledged of one record. Replaced whole, never mutated."""

    schema_version: Literal[1] = 1

    local_ref: str | None = None
    """The reference the record's manifest carries: which record this cursor is about.

    A cursor is used only for the record that carries the same one."""

    session_id: str | None = None
    """The dashboard's identifier of the session, once it is known."""

    boot_id: str | None = None
    """The start of the system during which the record was made (:func:`boot_identity`).

    While it is still the current one, the console's monotonic clock is the one
    the record was dated on, and the console can say how long ago the session
    started without reading a wall clock that may have been wrong."""

    start_confirmed: bool = True
    """``False`` for a launch from the dashboard whose start it has not acknowledged yet."""

    ticks_offset: int = 0
    """Bytes of ``ticks.csv`` behind which every 1 Hz point was acknowledged."""

    ticks_mid_line: bool = False
    """``ticks_offset`` is inside a line too long to be a tick, which is being passed over."""

    last_t: int | None = None
    """``t`` (unix ms, the machine's clock) of the last point acknowledged."""

    events_offset: int = 0
    """Bytes of ``events.jsonl`` behind which every event was acknowledged."""

    events_mid_line: bool = False
    """``events_offset`` is inside a line too long to be an event, which is being passed over."""

    last_seq: int = -1
    """Rank of the last event acknowledged; ``-1`` before the first."""

    end: Literal["pending", "sent"] = "pending"
    """Whether the dashboard acknowledged the end of the session."""

    state: Literal["pending", "complete"] = "pending"
    """``complete``: nothing of this record is owed any more."""

    rejected_points: int = 0
    rejected_events: int = 0
    """What the dashboard acknowledged without storing it, as dated outside the session."""

    refused: int = 0
    """Requests the dashboard refused for good; what they carried was passed over."""

    def __post_init__(self) -> None:
        """Refuse a cursor no sending can have written: it is read as unusable."""
        counts = (
            self.ticks_offset,
            self.events_offset,
            self.last_seq + 1,
            self.rejected_points,
            self.rejected_events,
            self.refused,
        )
        if min(counts) < 0:
            raise ValueError("a cursor counts nothing below zero")


_CURSOR: Final[TypeAdapter[Cursor]] = TypeAdapter(Cursor)


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class Baseline:
    """The records that had no cursor when the synchronisation first listed the directory.

    Made before it existed here (an earlier version of this software, or no
    dashboard configured then): they are not sent. Written once; a record
    that appears later without a cursor is not in it, and is sent.
    """

    schema_version: Literal[1] = 1
    left_alone: tuple[str, ...] = ()


_BASELINE: Final[TypeAdapter[Baseline]] = TypeAdapter(Baseline)


@dataclass(frozen=True, slots=True)
class NoCursor:
    """The record has no cursor: the synchronisation never opened it."""


@dataclass(frozen=True, slots=True)
class UnreadableCursor:
    """The cursor is there and cannot be used: the record is sent again from its beginning."""

    detail: str


type CursorRead = Cursor | NoCursor | UnreadableCursor


@dataclass(frozen=True, slots=True)
class CursorError:
    """The cursor could not be written. The sending goes on; it is written at the next step."""

    detail: str


def cursor_of(record: Path) -> Path:
    """Where the cursor of ``record`` is, or would be."""
    return record.with_name(record.name + CURSOR_SUFFIX)


def load(record: Path) -> CursorRead:
    """The cursor of ``record``. Never raises: a doubt is an :class:`UnreadableCursor`."""
    try:
        raw = cursor_of(record).read_bytes()
    except FileNotFoundError:
        return NoCursor()
    except OSError as error:
        return UnreadableCursor(describe_os_error(error))
    try:
        return _CURSOR.validate_json(raw)
    except ValueError:  # truncated, not JSON, of another version, or a field out of range
        return UnreadableCursor("invalid")


def store(record: Path, cursor: Cursor) -> Result[None, CursorError]:
    """Write the cursor of ``record``, atomically and privately. Blocking; never raises."""
    return _write(cursor_of(record), _CURSOR.dump_json(cursor))


def load_baseline(root: Path) -> Baseline | None:
    """The baseline of ``root``; ``None`` when there is none yet, or none that can be used."""
    try:
        return _BASELINE.validate_json((root / BASELINE_NAME).read_bytes())
    except (OSError, ValueError):
        return None


def store_baseline(root: Path, baseline: Baseline) -> Result[None, CursorError]:
    """Write the baseline of ``root``, as a cursor is written. Blocking; never raises."""
    return _write(root / BASELINE_NAME, _BASELINE.dump_json(baseline))


def _write(target: Path, document: bytes) -> Result[None, CursorError]:
    """Replace ``target`` by ``document``: a private temporary file, flushed, then renamed.

    The file is created by :func:`~src.record.writer.create_private`, as every
    file next to a record is: mode 600 from the call that creates it, and
    refused if the name exists. Two things are kept around that call, which a
    cursor needs and a file written once does not. It REPLACES the cursor of
    before, after each acknowledgement: so it is created under a name of its
    own and renamed over the other, and a reader sees one or the other. And
    it is flushed to the device before the rename: a power cut leaves the
    old cursor or the new one, not an empty file under the cursor's name.
    """
    temporary = target.with_name(f"{TEMP_PREFIX}{uuid4().hex}{TEMP_SUFFIX}")
    try:
        create_private(temporary, document + b"\n")
        _flush(temporary)
        temporary.replace(target)
    except OSError as error:
        _discard(temporary)
        return Err(CursorError(describe_os_error(error)))
    return Ok(None)


def _flush(path: Path) -> None:
    """Have the device hold what ``path`` was given. Raises ``OSError``."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard(path: Path) -> None:
    """Remove a file that is of no use any more. Left for the next sweep if refused."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def sweep(root: Path) -> tuple[str, ...]:
    """Remove what no record owns any more under ``root``; the names removed.

    * a cursor whose record directory is gone (a record removed by the local
      retention, or by hand): it says nothing of anything;
    * a temporary cursor left by a console that was killed while writing one.

    Raises ``OSError`` when the directory cannot be listed. A file that cannot
    be removed stays, and is tried again at the next start.
    """
    removed: list[str] = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        orphan = (
            name.endswith(CURSOR_SUFFIX)
            and RECORD_NAME.fullmatch(name.removesuffix(CURSOR_SUFFIX)) is not None
            and not entry.with_name(name.removesuffix(CURSOR_SUFFIX)).is_dir()
        )
        leftover = name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)
        if orphan or leftover:
            _discard(entry)
            removed.append(name)
    return tuple(removed)


def boot_identity(path: Path = BOOT_ID_PATH) -> str | None:
    """What names the current start of the system, or ``None`` where nothing does.

    Read once, at startup. ``None`` (not Linux, unreadable, or not an
    identifier) is never equal to another ``None``: a console that cannot name
    its boot never claims a record was made during it.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return None
    return text if _BOOT_ID.fullmatch(text) is not None else None
