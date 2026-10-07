"""Export of one session record as a ``.tar.gz`` archive, for the console's download route.

Listing the records and building an archive both read the disk, and an archive
compresses tens of megabytes: :class:`RecordExporter` runs both off the event
loop, so the control tick on it never waits for either. The web routes add
their own rule: neither is served while a session is in progress.

**Not on the loop's default thread pool** (:class:`RecordIo`). That pool is the
one the ECG treatment, the sensor processors and the BITalino link work on. A
records directory that stops answering would take its threads one request at a
time; with none left the heart rate goes stale and the supervisor ends the
session: a session stopped by the disk, which is exactly what the session
record must never do. Record reads have a few daemon threads of their own, and
when those are taken the next request is refused at once.

Only a record directory directly under the records root can be named, by the
exact name the writer gave it; anything else (a path, a link, another
directory) is "unknown". The archive is a temporary file next to the records,
removed once sent; one left behind by an interrupted download is removed by a
later export.
"""

from __future__ import annotations

import asyncio
import functools
import os
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Final

from src.clock import Clock
from src.record.retention import CHECKSUMS, RECORD_NAME, records
from src.record.schema import RecordError
from src.record.writer import describe_os_error
from src.result import Err, Ok, Result
from src.units import Seconds, UnixMillis

EXPORT_PREFIX: Final[str] = ".export-"
EXPORT_SUFFIX: Final[str] = ".tar.gz"

STALE_AFTER_MS: Final[int] = 600_000
"""An archive older than ten minutes was abandoned by its download: it is removed."""

UNKNOWN_RECORD: Final[str] = "unknown_record"

BUSY: Final[str] = "busy"
"""Every record I/O thread is taken: refused at once, nothing is queued."""

TIMEOUT: Final[str] = "timeout"
"""The disk did not answer in time. The thread stays on it; the caller does not."""

IO_THREADS: Final[int] = 2
"""Record reads in progress at the same time, at most: one listing and one archive."""

LISTING_TIMEOUT: Final[Seconds] = Seconds(5.0)
ARCHIVE_TIMEOUT: Final[Seconds] = Seconds(120.0)
"""How long a request waits for the disk. A whole session on an SD card is tens of
megabytes in thousands of small files."""


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
    """The record directory called ``name`` directly under ``root``, or ``None``.

    ``name`` comes from a request. Two controls, each sufficient alone:

    * it must be a record's name, as the writer gives them
      (:data:`~src.record.retention.RECORD_NAME`: no separator, no dot segment);
    * the path it gives is normalised, refused unless it is still directly
      inside the records directory, and that normalised path is the only one
      this function touches or returns.
    """
    if RECORD_NAME.fullmatch(name) is None:
        return None
    base = os.path.normpath(root)
    inside = base + os.sep
    candidate = os.path.normpath(inside + name)
    if not candidate.startswith(inside):
        return None
    path = Path(candidate)
    if path.parent != Path(base):
        return None
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


class RecordIo:
    """A few daemon threads for record reads, apart from every other thread of the console.

    At most ``limit`` operations run at a time, each on a thread of its own.
    One more is refused at once (:data:`BUSY`): nothing is queued, so nothing
    piles up behind a disk that does not answer. A caller waits at most its
    ``timeout``; the thread it leaves behind keeps its slot until the disk
    lets it go, and holds nothing else: not a thread of the loop's pool, and,
    being a daemon, not the exit of the process.

    Mutable. ``_active`` is read and written on the event loop thread only (a
    worker hands its result back through ``call_soon_threadsafe``).
    """

    __slots__ = ("_active", "_limit")

    def __init__(self, limit: int = IO_THREADS) -> None:
        self._limit: int = limit
        self._active: int = 0

    @property
    def active(self) -> int:
        """Operations whose thread has not come back yet."""
        return self._active

    async def run[T](
        self,
        work: Callable[[], T],
        timeout: Seconds,  # noqa: ASYNC109  # the bound is this method's whole purpose
        late: Callable[[T], None] | None = None,
    ) -> Result[T, RecordError]:
        """Run ``work`` on a thread of this group and wait for it, at most ``timeout``.

        ``late`` receives a result that arrives after its caller stopped
        waiting (on the worker thread: it may touch the disk). A caller that
        leaves in the instant between the worker's check and its answer is not
        seen as gone: for an archive, :func:`_sweep` removes the file later.
        """
        if self._active >= self._limit:
            return Err(RecordError("read", BUSY))
        loop = asyncio.get_running_loop()
        done: asyncio.Future[Result[T, RecordError]] = loop.create_future()
        left = ThreadEvent()
        self._active += 1

        def settle(outcome: Result[T, RecordError]) -> None:
            self._active -= 1
            done.set_result(outcome)

        def body() -> None:
            outcome: Result[T, RecordError]
            try:
                value = work()
                if late is not None and left.is_set():
                    late(value)
                outcome = Ok(value)
            except Exception as error:  # a bug here is an answer, never a slot that stays taken
                outcome = Err(RecordError("read", type(error).__name__))
            try:
                loop.call_soon_threadsafe(settle, outcome)
            except RuntimeError:
                # The loop is closed: the console is gone, nobody is left to tell.
                return

        Thread(target=body, name="record-io", daemon=True).start()
        try:
            return await asyncio.wait_for(asyncio.shield(done), timeout)
        except TimeoutError:
            return Err(RecordError("read", TIMEOUT))
        finally:
            left.set()


def _discard_built(built: Result[Path, RecordError]) -> None:
    """An archive finished after its request gave up: nobody will send it."""
    if isinstance(built, Ok):
        discard(built.value)


class RecordExporter:
    """The records directory, as the web layer may read it: read-only, and never on the loop.

    Every read goes through :class:`RecordIo`.
    """

    __slots__ = ("_clock", "_io", "_root")

    def __init__(self, root: Path, clock: Clock, io: RecordIo | None = None) -> None:
        self._root: Path = root
        self._clock: Clock = clock
        self._io: RecordIo = RecordIo() if io is None else io

    @property
    def io(self) -> RecordIo:
        """The threads the reads run on."""
        return self._io

    async def listing(self) -> Result[tuple[RecordEntry, ...], RecordError]:
        """Every record, newest first; or why the disk could not be asked."""
        return await self._io.run(functools.partial(listing, self._root), LISTING_TIMEOUT)

    async def archive(self, name: str) -> Result[Path, RecordError]:
        """Build the archive of one record; the caller sends it, then discards it."""
        build = functools.partial(build_archive, self._root, name, self._clock.unix_millis())
        built = await self._io.run(build, ARCHIVE_TIMEOUT, _discard_built)
        return built.value if isinstance(built, Ok) else Err(built.error)
