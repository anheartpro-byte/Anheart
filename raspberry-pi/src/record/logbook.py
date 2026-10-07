"""The console's logbook: what is asked and answered while no session record is open.

A session record is closed when the session's phase reaches ``DONE``, and a
closed record never changes again: its checksums index every file, a deposit
is bound to them, and the reader reports any later change as a mismatch. So
what happens at the console afterwards (an acknowledgement, a fault reset, a
start that is refused, news of the dashboard link) cannot go into it, and had
nowhere to go. It goes here.

One JSON object per line, in ``<records root>/logbook/events.jsonl``::

    {"at":"2026-10-08T10:11:12.345Z","monotonic":1234.5,"kind":"refusal",
     "detail":"refused: demarrage refuse : ...","actor":"op-0123456789abcdef"}

``kind``, ``detail`` and ``actor`` are those of a session record's event
(:class:`~src.record.schema.Event`), written by the same rules: an opaque
actor, never a name. There is no session, so no session time axis: ``at`` is
the wall clock (UTC) and ``monotonic`` the console's own clock in seconds,
which orders the lines of one run of the console across a wall-clock step.

**Bounded on disk.** When the next line would take ``events.jsonl`` past
:data:`MAX_BYTES`, it becomes ``events.1.jsonl`` (replacing the previous one)
and a new file is started: two files, :data:`MAX_BYTES` each at most, the most
recent lines always kept. Nothing here is purged by the retention and nothing
here is part of a record: the directory's name is not a record's.

Like the writer, this has no thread and no queue: on the console its one owner
is the journal thread (:mod:`src.record.journal`), which is handed the notes
through the same bounded queue as everything else. Nothing here may be called
from the control loop.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass

from src.record.codec import IDENTIFIER, Privacy, document, encode
from src.record.schema import CONFIG, EventKind, RecordError
from src.record.writer import (
    DIRECTORY_SYNC,
    PRIVATE_DIRECTORY,
    describe_os_error,
    fsync_path,
    write_file,
)
from src.result import Err, Ok, Result
from src.units import Monotonic, UnixMillis

DIRECTORY: Final[str] = "logbook"
"""Under the records root. Not a record's name, nor a marker's (``<record>.<x>.json``)."""

ACTIVE: Final[str] = "events.jsonl"
PREVIOUS: Final[str] = "events.1.jsonl"

MAX_BYTES: Final[int] = 1_000_000
"""One file of the logbook, at most. A line is a few hundred bytes: thousands of lines."""

_PRIVACY: Final[Privacy] = Privacy()


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Note:
    """One line of the logbook. Frozen: built on the event loop, written by the journal thread."""

    at: str
    """The wall clock, ISO 8601 UTC with a ``Z``."""

    monotonic: float
    """The console's clock, in seconds: comparable within one run of the console only."""

    kind: EventKind
    detail: str
    actor: str = "system"


_NOTE: Final[TypeAdapter[Note]] = TypeAdapter(Note)


def note(
    *, wall_clock: UnixMillis, at: Monotonic, kind: EventKind, detail: str, actor: str
) -> Note:
    """A line for something that happened at ``at`` on the console's clock."""
    stamp = datetime.fromtimestamp(wall_clock / 1000, UTC).isoformat(timespec="milliseconds")
    return Note(
        at=stamp.replace("+00:00", "Z"),
        monotonic=round(at, 3),
        kind=kind,
        detail=detail,
        actor=actor,
    )


class Logbook:
    """The two files of the logbook. Mutable, single owner; every failure is a ``Result``."""

    __slots__ = ("_directory", "_limit", "_unsynced")

    def __init__(self, root: Path, limit: int = MAX_BYTES) -> None:
        self._directory: Path = root / DIRECTORY
        self._limit: int = limit
        self._unsynced: bool = False

    @property
    def directory(self) -> Path:
        """Where the logbook is, or will be once a first line is written."""
        return self._directory

    def append(self, entry: Note) -> Result[None, RecordError]:
        """Add one line, starting a new file first when this one is full.

        A line the disk refuses half-way is taken back out, so that the next
        one never lands behind half a line.
        """
        if IDENTIFIER.fullmatch(entry.actor) is None:
            return Err(RecordError("event", "invalid_actor"))
        active = self._directory / ACTIVE
        # A note is validated when it is built: it always has a line.
        line = (encode(document(_NOTE, entry), _PRIVACY) + "\n").encode("utf-8")
        size: int | None = None
        try:
            self._directory.mkdir(mode=PRIVATE_DIRECTORY, exist_ok=True)
            size = _size(active)
            if size and size + len(line) > self._limit:
                active.replace(self._directory / PREVIOUS)
                size = 0
            write_file(active, line, "ab")
        except OSError as error:
            if size is not None:
                # Only back to a length that was read: never cut on a guess.
                _cut_back(active, size)
            return Err(RecordError("append", describe_os_error(error)))
        self._unsynced = True
        return Ok(None)

    def sync(self, *, directories: bool = DIRECTORY_SYNC) -> Result[None, RecordError]:
        """Flush what was added since the last call. Nothing to do, nothing done."""
        if not self._unsynced:
            return Ok(None)
        try:
            fsync_path(self._directory / ACTIVE)
            if directories:
                fsync_path(self._directory)
        except OSError as error:
            return Err(RecordError("sync", describe_os_error(error)))
        self._unsynced = False
        return Ok(None)


def _size(path: Path) -> int:
    """The length of ``path``, 0 when it does not exist yet. Raises any other ``OSError``."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _cut_back(path: Path, length: int) -> None:
    """Remove what a refused write may have left after ``length``. Never raises."""
    try:
        if _size(path) > length:
            os.truncate(path, length)
    except OSError:
        return
