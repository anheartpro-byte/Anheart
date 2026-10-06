"""Export of one session record as a ``.tar.gz`` archive, for the console's download route.

Listing the records and building an archive both read the disk, and an archive
compresses tens of megabytes: :class:`RecordExporter` runs both on a worker
thread, so the event loop (and the control tick on it) never waits for either.
The web route adds its own rule: no export while a session is in progress.

Only a record directory directly under the records root can be named, by the
exact name the writer gave it; anything else (a path, a link, another
directory) is "unknown". The archive is a temporary file next to the records,
removed once sent; one left behind by an interrupted download is removed by a
later export.
"""

from __future__ import annotations

import asyncio
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from src.clock import Clock
from src.record.retention import CHECKSUMS, RECORD_NAME, records
from src.record.schema import RecordError
from src.record.writer import describe_os_error
from src.result import Err, Ok, Result
from src.units import UnixMillis

EXPORT_PREFIX: Final[str] = ".export-"
EXPORT_SUFFIX: Final[str] = ".tar.gz"

STALE_AFTER_MS: Final[int] = 600_000
"""An archive older than ten minutes was abandoned by its download: it is removed."""

UNKNOWN_RECORD: Final[str] = "unknown_record"


@dataclass(frozen=True, slots=True)
class RecordEntry:
    """One record on disk, as the console lists it."""

    name: str
    closed: bool
    """Whether it was finalised (it has its checksums); ``False`` for an interrupted one."""


def listing(root: Path) -> tuple[RecordEntry, ...]:
    """Every record under ``root``, newest first. Empty when the directory cannot be read."""
    try:
        found = records(root)
    except OSError:
        return ()
    return tuple(
        RecordEntry(name=path.name, closed=(path / CHECKSUMS).is_file()) for path in reversed(found)
    )


def locate(root: Path, name: str) -> Path | None:
    """The record directory called ``name`` directly under ``root``, or ``None``."""
    if RECORD_NAME.fullmatch(name) is None:
        return None
    path = root / name
    return path if path.is_dir() and not path.is_symlink() else None


def _anonymous(member: tarfile.TarInfo) -> tarfile.TarInfo:
    """The archive says nothing about the account the console runs under."""
    member.uid = member.gid = 0
    member.uname = member.gname = ""
    return member


def discard(path: Path) -> None:
    """Remove an archive that was sent, or could not be. Never raises."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


def _sweep(root: Path, now: UnixMillis) -> None:
    for leftover in root.glob(f"{EXPORT_PREFIX}*{EXPORT_SUFFIX}"):
        try:
            abandoned = now - leftover.stat().st_mtime * 1000 > STALE_AFTER_MS
        except OSError:
            continue
        if abandoned:
            discard(leftover)


def build_archive(root: Path, name: str, now: UnixMillis) -> Result[Path, RecordError]:
    """Write ``<name>.tar.gz`` to a temporary file under ``root``. Blocking: a worker thread's.

    The caller owns the file it gets back and removes it with :func:`discard`.
    """
    record = locate(root, name)
    if record is None:
        return Err(RecordError("read", UNKNOWN_RECORD))
    _sweep(root, now)
    target: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=EXPORT_PREFIX, suffix=EXPORT_SUFFIX, dir=root, delete=False
        ) as handle:
            target = Path(handle.name)
            with tarfile.open(fileobj=handle, mode="w:gz") as archive:
                archive.add(record, arcname=record.name, filter=_anonymous)
    except OSError as error:
        if target is not None:
            discard(target)
        return Err(RecordError("read", describe_os_error(error)))
    return Ok(target)


class RecordExporter:
    """The records directory, as the web layer may read it: off the loop, and read-only."""

    __slots__ = ("_clock", "_root")

    def __init__(self, root: Path, clock: Clock) -> None:
        self._root: Path = root
        self._clock: Clock = clock

    async def listing(self) -> tuple[RecordEntry, ...]:
        """Every record, newest first."""
        return await asyncio.to_thread(listing, self._root)

    async def archive(self, name: str) -> Result[Path, RecordError]:
        """Build the archive of one record; the caller sends it, then discards it."""
        return await asyncio.to_thread(build_archive, self._root, name, self._clock.unix_millis())
