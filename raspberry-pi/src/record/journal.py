"""The session journal: every byte of a session record is written here, off the control loop.

The control tick (5 Hz) and the other tasks of the console's one event loop hand
frozen values to :meth:`Journal.submit`. That call appends to an in-memory
deque and returns: it takes no lock, opens no file, and never waits. The
supervisor's ``loop_stall`` rule freezes the arm after 0.6 s between two ticks
and silences the process after 3 s, so a tick that waited on an SD card would
be a hazard of its own - a black box that can stall the loop is worse than no
black box.

ONE dedicated thread (``record-journal``) drains the queue every
:data:`DRAIN_PERIOD`, owns the :class:`~src.record.writer.Writer`, and calls
``fsync`` every :data:`FSYNC_PERIOD`, never more often. What follows from that
split, and is tested:

* **bounded**: at most :attr:`Limits.entries` values and
  :attr:`Limits.samples` raw sample values wait in memory. Past that the newest
  value is refused and counted; nothing grows, nothing blocks.
* **a failed write costs the record, never the session**: every refusal of the
  disk (full, permission, I/O) is a ``Result`` counted here. It reaches the
  producer only as :class:`JournalStatus`, read without waiting.
* **a hung disk is seen**: the thread stops consuming, the producer notices
  that nothing was consumed for :data:`STALL_AFTER` and reports it; the queue
  fills, then refuses. The loop never learns of it any other way.
* **an abrupt stop loses at most one drain period** of what was submitted
  (what the OS already holds survives a killed process), and at most
  :data:`FSYNC_PERIOD` more if the power goes with it.

Between sessions the same thread applies the local retention
(:mod:`src.record.retention`): a record deposited AND confirmed longer ago than
the retention is removed; one that was not deposited never is. And it writes
the logbook (:mod:`src.record.logbook`): what is asked and answered at the
console while no record is open, which a closed record can no longer take.

The drive's frames do not go through the queue. They wait in a bounded list of
their own (:mod:`src.record.drive_tap`), filled by whoever talks to the drive,
and the journal thread takes them at every cycle: nothing is put on the control
tick for them.

A long operation (closing a record of thousands of files, the retention) is
still one cycle, but not a silent one: between two files the thread measures
the disk and publishes when either is due (:meth:`Scribe._pulse`), and it says
what it is doing (:class:`Activity`). The arming gate then refuses on a
measurement that is really old, and says why in words that are true.

The producer side (:meth:`Journal.open`, :meth:`Journal.submit`,
:meth:`Journal.close`, :meth:`Journal.status`) belongs to the event loop
thread, and to it alone: the bound relies on there being one producer.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Final, assert_never

from src.clock import Clock, ManualClock
from src.record.codec import Privacy
from src.record.drive_tap import FrameSource, Taken
from src.record.ecg import Header, RawBlock
from src.record.logbook import Logbook, Note, note
from src.record.retention import purge
from src.record.rows import Row
from src.record.schema import DriveFrame, EndObservation, Event, EventKind, Manifest, RecordError
from src.record.writer import Writer, describe_os_error
from src.result import Err, Ok, Result
from src.sensors.base import SensorReading
from src.units import Monotonic, Seconds, UnixMillis

_logger: Final[logging.Logger] = logging.getLogger(__name__)

DRAIN_PERIOD: Final[Seconds] = Seconds(0.2)
"""How often the journal thread empties the queue: one control period."""

FSYNC_PERIOD: Final[Seconds] = Seconds(2.0)
"""The time between two ``fsync``: what a power cut can take, and never more often."""

PROBE_PERIOD: Final[Seconds] = Seconds(5.0)
"""How often the free space under the records directory is measured, session or not."""

STORAGE_STALE_AFTER: Final[Seconds] = Seconds(15.0)
"""A free-space measurement older than this is no longer evidence: three missed probes.

The journal thread measures every :data:`PROBE_PERIOD`. When it is stuck on a
disk that does not answer, its last number stays, and looks as good as it was.
Past this age the arming gate refuses on it, exactly as on an unreadable one."""

STALL_AFTER: Final[Seconds] = Seconds(5.0)
"""Submitted values waiting this long with nothing consumed: the disk is not answering."""

STOP_TIMEOUT: Final[Seconds] = Seconds(5.0)
"""How long the console's exit waits for the last record to be closed."""

PURGE_PERIOD: Final[Seconds] = Seconds(6 * 3600.0)
"""How often the retention is applied, and only between two sessions."""

PUBLISH_PERIOD: Final[Seconds] = Seconds(1.0)
"""Inside one cycle, the thread publishes its progress at least this often.

A long backlog on a slow disk is one long cycle: without this the producer
would see nothing consumed for its whole length and call it a stall."""

MIN_FREE_BYTES: Final[int] = 500_000_000
"""Below 500 MB free under the records directory, no session is armed."""

RAW_BLOCKS_PER_SECOND: Final[int] = 5
"""Files a session adds every second: one raw block per acquisition batch, and the
console acquires every 0.2 s. Everything else is appended to files that exist."""

FIXED_ENTRIES: Final[int] = 8
"""What a record takes besides its raw blocks: its directory, ``ecg_raw/``, the
manifest, the four streams and the checksums."""

LONGEST_SESSION: Final[Seconds] = Seconds(3600.0)
"""The longest session the gate plans for: the hour after which the runtime ends a
manual session by itself (``MANUAL_SESSION_LIMIT``)."""

SESSION_ENTRIES: Final[int] = round(RAW_BLOCKS_PER_SECOND * LONGEST_SESSION) + FIXED_ENTRIES
"""Inodes one such session takes: 18 008."""

MIN_FREE_INODES: Final[int] = 2 * SESSION_ENTRIES
"""Below this many free inodes under the records directory, no session is armed: 36 016.

A file system can run out of inodes long before it runs out of bytes, and a
record is thousands of small files: every write is then refused although
megabytes are free. The gate is asked once, at arming, so it must leave room
for the whole session being armed: one :data:`SESSION_ENTRIES`. And as much
again for what it cannot know: a programme longer than an hour, the descent
and the monitored recovery after it, the logbook, and whatever else shares the
file system (on the Pi, the system itself)."""

PRIVATE_MODE: Final[int] = 0o700
"""The records directory belongs to the service user alone (interim rule, ANH-128 EX-7)."""


@dataclass(frozen=True, slots=True)
class Limits:
    """What may wait in memory. Past either bound the newest value is refused."""

    entries: int = 4096
    """Queued values of any kind: several minutes of a session at its normal rate."""

    samples: int = 200_000
    """Queued raw sample values: about half a minute of six channels at 1000 Hz."""


DEFAULT_LIMITS: Final[Limits] = Limits()


# =========================================================================
# What goes through the queue
# =========================================================================


@dataclass(frozen=True, slots=True)
class Sensors:
    """One publication of every processed channel, for ``sensors.csv``."""

    at: float
    readings: tuple[SensorReading, ...]


@dataclass(frozen=True, slots=True)
class RawBatch:
    """One acquisition batch exactly as received, channel-major, in ADC counts.

    ``samples`` are the acquisition's own sequences, shared and never mutated:
    turning them into a block (and compressing it) is the journal thread's work.
    """

    seq: int
    t_first: float
    channels: tuple[str, ...]
    samples: tuple[Sequence[float], ...]


type Entry = Row | Event | DriveFrame | RawBatch | Sensors
"""One value of a session's streams."""


@dataclass(frozen=True, slots=True)
class Opening:
    """Create the record directory of a session that was just armed."""

    session: int
    manifest: Manifest
    privacy: Privacy


@dataclass(frozen=True, slots=True)
class Closing:
    """Finalise the manifest and the checksums of the session that just ended."""

    ended_at: UnixMillis
    end_reason: str
    observation: EndObservation | None


type Item = Opening | Closing | Entry | Note


# =========================================================================
# What comes back
# =========================================================================


@dataclass(frozen=True, slots=True)
class Storage:
    """The last measurement of the records directory, and when it was taken."""

    free_bytes: int | None
    """``None``: it could not be measured."""

    measured_at: Monotonic
    """On the injected clock. Whoever reads the number decides whether it is still fresh."""

    free_inodes: int | None = None
    """Files that can still be created. ``None``: this file system does not count them
    (it has no fixed number of inodes), or nothing could be measured at all."""


class Activity(StrEnum):
    """What the journal thread said it was starting, the last time it published."""

    IDLE = "idle"
    """Its ordinary cycle: write what is queued, flush, measure."""

    CLOSING = "closing"
    """Finalising a record: every file of it is read back for the checksums."""

    PURGING = "purging"
    """Applying the retention: removing records deposited long enough ago."""


@dataclass(frozen=True, slots=True)
class Progress:
    """What the journal thread last published. Replaced whole, never mutated."""

    storage: Storage
    consumed: int = 0
    """Items taken off the queue since the process started."""

    samples: int = 0
    """Raw sample values taken off the queue since the process started."""

    session: int = 0
    """The session the counts below are about; 0 before the first one."""

    failures: int = 0
    """Writes the disk refused during that session."""

    discarded: int = 0
    """Values that had no record to go to (its directory could not be created)."""

    error: RecordError | None = None
    """The most recent refusal of that session."""

    path: Path | None = None
    """The record directory of that session, once created."""

    frames_lost: int = 0
    """Drive observations that session's record did not get: refused by their bounded
    list, or impossible to build."""

    activity: Activity = Activity.IDLE


class Cause(StrEnum):
    """Why a record is degraded, the most pressing first."""

    STALLED = "stalled"
    """Values are waiting and the journal thread consumes none: the disk does not answer."""

    WRITE_FAILED = "write_failed"
    """The disk refused a write, or the record directory could not be created."""

    QUEUE_FULL = "queue_full"
    """Values were refused because the bounded queue was full."""

    STORAGE_UNAVAILABLE = "storage_unavailable"
    """The records directory cannot be created, made private, or measured."""


@dataclass(frozen=True, slots=True)
class JournalStatus:
    """The journal as the event loop sees it, without waiting for anything."""

    recording: bool
    """Whether a session is open on the producer side."""

    degraded: bool
    cause: Cause | None
    """Why, when degraded; else ``None``."""

    error: RecordError | None
    dropped: int
    """Values refused because the queue was full, this session: rows, events, blocks,
    lines of the logbook, and the drive observations their own list refused."""

    failures: int
    """Values the disk refused or that had no record to go to, this session."""

    pending: int
    path: Path | None
    free_bytes: int | None
    free_inodes: int | None = None


def prepare_root(root: Path) -> None:
    """Create the records directory, private to the service user. Raises ``OSError``."""
    root.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    root.chmod(PRIVATE_MODE)


def measure(root: Path, at: Monotonic) -> Storage:
    """The free space under ``root`` at ``at``; ``free_bytes`` is ``None`` when unreadable."""
    try:
        return Storage(shutil.disk_usage(root).free, at, free_inodes(root))
    except OSError:
        return Storage(None, at)


def free_inodes(root: Path) -> int | None:
    """Files this user may still create under ``root``. Raises ``OSError``.

    ``None`` where the question has no answer: a file system that allocates
    its inodes as it goes reports none in total, and Windows has no such call.
    """
    if sys.platform == "win32":
        return None
    stats = os.statvfs(root)
    return stats.f_favail if stats.f_files > 0 else None


def raw_block(batch: RawBatch) -> RawBlock:
    """The on-disk block of a batch. Raises ``ValueError`` on a fractional ADC count."""
    if any(not value.is_integer() for channel in batch.samples for value in channel):
        raise ValueError("raw acquisition requires integer ADC counts")
    count = len(batch.samples[0]) if batch.samples else 0
    return RawBlock(
        Header(seq=batch.seq, t_first=batch.t_first, n_samples=count, channels=batch.channels),
        tuple(tuple(int(value) for value in channel) for channel in batch.samples),
    )


def _write_raw(writer: Writer, batch: RawBatch) -> Result[None, RecordError]:
    try:
        block = raw_block(batch)
    except ValueError as error:
        return Err(RecordError("raw", type(error).__name__))
    return writer.raw(block)


def _sample_count(item: Item) -> int:
    if isinstance(item, RawBatch):
        return sum(len(channel) for channel in item.samples)
    return 0


# =========================================================================
# The disk side: one thread, and nothing else, touches this
# =========================================================================


class Scribe:
    """Everything the journal thread owns. Mutable, and never read by the producer.

    The producer reads only the :class:`Progress` this publishes, a frozen
    value swapped in by one attribute assignment.
    """

    __slots__ = (
        "_activity",
        "_clock",
        "_consumed",
        "_discarded",
        "_error",
        "_failures",
        "_frames",
        "_frames_lost",
        "_frames_lost_before",
        "_logbook",
        "_logbook_synced_at",
        "_path",
        "_probed_at",
        "_publish",
        "_published_at",
        "_purged_at",
        "_retention_days",
        "_root",
        "_samples",
        "_session",
        "_storage",
        "_synced_at",
        "_unlogged",
        "_warned",
        "_writer",
    )

    def __init__(
        self,
        root: Path,
        clock: Clock,
        retention_days: int | None,
        publish: Callable[[Progress], None],
    ) -> None:
        self._root: Path = root
        self._clock: Clock = clock
        self._retention_days: int | None = retention_days
        self._publish: Callable[[Progress], None] = publish
        # Due at once: the first idle cycle after startup applies the retention.
        self._purged_at: Monotonic = Monotonic(clock.monotonic() - PURGE_PERIOD)
        self._writer: Writer | None = None
        self._logbook: Logbook = Logbook(root)
        self._frames: FrameSource | None = None
        self._activity: Activity = Activity.IDLE
        self._session: int = 0
        self._consumed: int = 0
        self._samples: int = 0
        self._failures: int = 0
        self._discarded: int = 0
        self._warned: int = 0
        self._unlogged: int = 0
        self._frames_lost: int = 0
        self._frames_lost_before: int = 0
        self._error: RecordError | None = None
        self._path: Path | None = None
        self._synced_at: Monotonic = clock.monotonic()
        self._logbook_synced_at: Monotonic = clock.monotonic()
        self._probed_at: Monotonic = clock.monotonic()
        self._published_at: Monotonic = clock.monotonic()
        self._storage: Storage = self._claim_root()

    def _claim_root(self) -> Storage:
        try:
            prepare_root(self._root)
        except OSError as error:
            _logger.warning("records directory unusable: %s", describe_os_error(error))
            return Storage(None, self._probed_at)
        return measure(self._root, self._probed_at)

    def listen(self, frames: FrameSource) -> None:
        """Where the drive's observations wait. Set once, when the console is built."""
        self._frames = frames

    def progress(self) -> Progress:
        return Progress(
            storage=self._storage,
            consumed=self._consumed,
            samples=self._samples,
            session=self._session,
            failures=self._failures,
            discarded=self._discarded,
            error=self._error,
            path=self._path,
            frames_lost=self._frames_lost - self._frames_lost_before,
            activity=self._activity,
        )

    def cycle(self, queue: deque[Item], dropped: int, unlogged: int) -> None:
        """Write everything queued when the cycle began, then the periodic work.

        The progress is published every :data:`PUBLISH_PERIOD` while the cycle
        lasts, and once at the end.

        The drive's observations are taken first, and go to the record that
        was open when they were taken: a closing queued behind them does not
        cost the last frames of its session. With no record open they wait for
        the queue, which may open one (the frames of an arming belong to the
        session it armed); if it does not, nobody is recording and they are
        let go.
        """
        now = self._clock.monotonic()
        taken = self._taken()
        open_before = self._writer
        if open_before is not None:
            self._write_frames(open_before, taken)
        for _ in range(len(queue)):
            item = queue.popleft()
            self._consumed += 1
            self._samples += _sample_count(item)
            self._note(self._take(item, now, dropped, unlogged))
            self._pulse()
        opened = self._writer
        if open_before is None and opened is not None:
            self._write_frames(opened, taken)
        self._checkpoint(now, dropped)
        self._published_at = self._clock.monotonic()
        self._publish(self.progress())

    def _pulse(self) -> None:
        """Between two steps of anything long: measure the disk and publish, when due.

        Without it a cycle that lasts (a backlog on a slow card, a close, the
        retention) would leave the last measurement to go stale, and the arming
        gate to refuse on a disk that is only busy.
        """
        moment = self._clock.monotonic()
        if moment - self._probed_at >= PROBE_PERIOD:
            self._probed_at = moment
            self._storage = measure(self._root, moment)
        if moment - self._published_at >= PUBLISH_PERIOD:
            self._published_at = moment
            self._publish(self.progress())

    def _note(self, outcome: Result[None, RecordError]) -> None:
        if isinstance(outcome, Err):
            self._failures += 1
            self._error = outcome.error

    def _taken(self) -> Taken:
        """The drive observations waiting now. The count of the lost is kept either way."""
        frames = self._frames
        if frames is None:
            return _NO_FRAMES
        taken = frames()
        self._frames_lost = taken.lost
        return taken

    def _write_frames(self, writer: Writer, taken: Taken) -> None:
        origin = Monotonic(writer.manifest.clocks.monotonic_start)
        for observation in taken.observations:
            try:
                written = writer.frame(observation.frame(origin))
            except Exception as error:  # one frame the format refuses costs that frame
                written = Err(RecordError("append", type(error).__name__))
            self._note(written)
            self._pulse()

    def _take(
        self, item: Item, now: Monotonic, dropped: int, unlogged: int
    ) -> Result[None, RecordError]:
        try:
            if isinstance(item, Opening):
                return self._open(item, now)
            if isinstance(item, Closing):
                return self._close(item, now, dropped)
            if isinstance(item, Note):
                return self._log(item, now, unlogged)
            return self._append(item)
        except Exception as error:  # a recording bug costs one value, never the thread
            _logger.exception("session journal: one value could not be written")
            return Err(RecordError("append", type(error).__name__))

    def _append(self, entry: Entry) -> Result[None, RecordError]:
        writer = self._writer
        if writer is None:
            self._discarded += 1
            return Ok(None)
        match entry:
            case Row():
                return writer.tick(entry)
            case Event():
                return writer.event(entry)
            case DriveFrame():
                return writer.frame(entry)
            case RawBatch():
                return _write_raw(writer, entry)
            case Sensors():
                return writer.sensors(entry.at, entry.readings)
        raise assert_never(entry)

    def _log(self, entry: Note, now: Monotonic, unlogged: int) -> Result[None, RecordError]:
        """One line of the logbook; before it, what the logbook missed, if anything new."""
        if unlogged and unlogged != self._unlogged:
            missed = note(
                wall_clock=self._clock.unix_millis(),
                at=now,
                kind=EventKind.WARNING,
                detail=f"logbook: dropped={unlogged}",
                actor="system",
            )
            if isinstance(self._logbook.append(missed), Ok):
                self._unlogged = unlogged
        return self._logbook.append(entry)

    def _open(self, opening: Opening, now: Monotonic) -> Result[None, RecordError]:
        previous = self._writer
        if previous is not None:
            # The producer closes a session before opening the next. One that
            # was left open is still finalised, and says how it ended.
            self._writer = None
            self._note(self._finalise(previous, self._clock, "superseded", None))
        self._session = opening.session
        self._failures = 0
        self._discarded = 0
        self._warned = 0
        self._unlogged = 0
        self._frames_lost_before = self._frames_lost
        self._error = None
        self._path = None
        try:
            prepare_root(self._root)
        except OSError as error:
            return Err(RecordError("create", describe_os_error(error)))
        created = Writer.create(self._root, opening.manifest, opening.privacy)
        if isinstance(created, Err):
            return Err(created.error)
        writer = created.value
        self._writer = writer
        self._path = writer.path
        self._synced_at = now
        return writer.sync()

    def _close(self, closing: Closing, now: Monotonic, dropped: int) -> Result[None, RecordError]:
        writer = self._writer
        if writer is None:
            return Ok(None)
        self._writer = None
        self._warn(writer, now, dropped)
        return self._finalise(
            writer,
            ManualClock(epoch_millis=closing.ended_at),
            closing.end_reason,
            closing.observation,
        )

    def _finalise(
        self, writer: Writer, clock: Clock, reason: str, observation: EndObservation | None
    ) -> Result[None, RecordError]:
        """Close ``writer`` and flush it, saying so first: the index reads every file back."""
        self._begin(Activity.CLOSING)
        try:
            closed = writer.close(clock, reason, observation, pulse=self._pulse)
            synced = writer.sync()
        finally:
            self._activity = Activity.IDLE
        return synced if isinstance(closed, Ok) else closed

    def _begin(self, activity: Activity) -> None:
        """Say what is starting BEFORE starting it: whoever waits can then name it."""
        self._activity = activity
        self._publish(self.progress())

    def _checkpoint(self, now: Monotonic, dropped: int) -> None:
        writer = self._writer
        if writer is not None and now - self._synced_at >= FSYNC_PERIOD:
            self._synced_at = now
            self._warn(writer, now, dropped)
            self._note(writer.sync())
        if now - self._logbook_synced_at >= FSYNC_PERIOD:
            self._logbook_synced_at = now
            self._note(self._logbook.sync())
        self._pulse()
        retention = self._retention_days
        if writer is None and retention is not None and now - self._purged_at >= PURGE_PERIOD:
            self._purged_at = now
            self._begin(Activity.PURGING)
            try:
                report = purge(self._root, self._clock.unix_millis(), retention, self._pulse)
            finally:
                self._activity = Activity.IDLE
            if report.removed or report.failed:
                _logger.warning(
                    "local retention (%d days): removed %s, could not remove %s",
                    retention,
                    list(report.removed),
                    list(report.failed),
                )

    def _warn(self, writer: Writer, now: Monotonic, dropped: int) -> None:
        """Say in the record itself what it is missing, once per change, if the disk lets us."""
        frames = self._frames_lost - self._frames_lost_before
        lost = dropped + self._failures + frames
        if lost == self._warned:
            return
        error = self._error
        last = "" if error is None else f" last={error.operation}:{error.detail}"
        unheard = f" drive_frames_lost={frames}" if frames else ""
        warning = Event(
            t=round(now - writer.manifest.clocks.monotonic_start, 3),
            kind=EventKind.WARNING,
            detail=f"record_degraded: dropped={dropped} failures={self._failures}{unheard}{last}",
        )
        if isinstance(writer.event(warning), Ok):
            self._warned = lost


_NO_FRAMES: Final[Taken] = Taken((), 0)


# =========================================================================
# The producer side
# =========================================================================


class Journal:
    """The bounded queue and its thread. See the module docstring.

    Mutable. The fields below are written by the event loop thread only, except
    ``_progress``, which the journal thread replaces whole; ``_dropped`` and
    ``_unlogged`` are the two producer fields that thread reads (two ``int``,
    for the warnings it writes into the record and into the logbook).
    """

    __slots__ = (
        "_clock",
        "_dropped",
        "_limits",
        "_mark",
        "_open",
        "_period",
        "_progress",
        "_put",
        "_put_samples",
        "_queue",
        "_root",
        "_scribe",
        "_session",
        "_stopping",
        "_thread",
        "_unlogged",
    )

    def __init__(
        self,
        root: Path,
        clock: Clock,
        *,
        limits: Limits = DEFAULT_LIMITS,
        period: Seconds = DRAIN_PERIOD,
        retention_days: int | None = None,
    ) -> None:
        """Claim the records directory and measure it. Startup I/O, nothing is turning.

        A directory that cannot be created or made private is not an error
        here: it is an unknown free space, which refuses the next arming.

        ``retention_days``: how long a record deposited and confirmed is kept
        (:mod:`src.record.retention`). ``None``: nothing is ever removed.
        """
        self._root: Path = root
        self._clock: Clock = clock
        self._limits: Limits = limits
        self._period: Seconds = period
        self._queue: deque[Item] = deque()
        self._scribe: Scribe = Scribe(root, clock, retention_days, self._publish)
        self._progress: Progress = self._scribe.progress()
        self._session: int = 0
        self._open: bool = False
        self._put: int = 0
        self._put_samples: int = 0
        self._dropped: int = 0
        self._unlogged: int = 0
        self._mark: tuple[int, Monotonic] = (0, clock.monotonic())
        self._stopping: ThreadEvent = ThreadEvent()
        self._thread: Thread | None = None

    @property
    def root(self) -> Path:
        """The records directory."""
        return self._root

    def listen(self, frames: FrameSource) -> None:
        """Take the drive's observations from ``frames`` at every cycle, off the loop.

        Called once, where the console is built, before anything turns: from
        then on only the journal thread calls ``frames``.
        """
        self._scribe.listen(frames)

    # --- the producer: event loop thread only -----------------------------

    def open(self, manifest: Manifest, privacy: Privacy) -> None:
        """A session was armed: queue the creation of its record. Never refused."""
        self._session += 1
        self._open = True
        self._dropped = 0
        self._unlogged = 0
        self._enqueue(Opening(self._session, manifest, privacy))

    def close(self, closing: Closing) -> None:
        """The session is over: queue the finalisation of its record. Never refused."""
        self._open = False
        self._enqueue(closing)

    def submit(self, entry: Entry) -> bool:
        """Queue one value of the open session. ``False``: no session, or no room.

        Never blocks and never raises. A value refused for lack of room is
        counted (:attr:`JournalStatus.dropped`) and the record says so.
        """
        if not self._open:
            return False
        progress = self._progress
        samples = _sample_count(entry)
        if (
            self._put - progress.consumed >= self._limits.entries
            or self._put_samples - progress.samples + samples > self._limits.samples
        ):
            self._dropped += 1
            return False
        self._put_samples += samples
        self._enqueue(entry)
        return True

    def log(self, entry: Note) -> bool:
        """Queue one line of the logbook, session or not. ``False``: no room.

        Never blocks and never raises, like :meth:`submit`, and bounded by the
        same queue. A line refused is counted with the dropped values, and the
        logbook says so before its next line.
        """
        if self._put - self._progress.consumed >= self._limits.entries:
            self._unlogged += 1
            return False
        self._enqueue(entry)
        return True

    def _enqueue(self, item: Item) -> None:
        self._put += 1
        self._queue.append(item)

    def status(self, now: Monotonic) -> JournalStatus:
        """Where the record stands. Reads memory only; call it every tick.

        The stall detector lives here: it compares what the thread says it
        consumed with what it said at the previous call.
        """
        progress = self._progress
        pending = self._put - progress.consumed
        consumed_then, since = self._mark
        if pending == 0 or progress.consumed != consumed_then:
            self._mark = (progress.consumed, now)
            stalled = False
        else:
            stalled = now - since > STALL_AFTER
        current = progress.session == self._session
        failures = progress.failures + progress.discarded if current else 0
        error = progress.error if current else None
        dropped = self._dropped + self._unlogged + (progress.frames_lost if current else 0)
        cause: Cause | None = None
        if stalled:
            cause = Cause.STALLED
        elif failures:
            cause = Cause.WRITE_FAILED
        elif dropped:
            cause = Cause.QUEUE_FULL
        elif progress.storage.free_bytes is None:
            cause = Cause.STORAGE_UNAVAILABLE
        return JournalStatus(
            recording=self._open,
            degraded=cause is not None,
            cause=cause,
            error=error,
            dropped=dropped,
            failures=failures,
            pending=pending,
            path=progress.path if current else None,
            free_bytes=progress.storage.free_bytes,
            free_inodes=progress.storage.free_inodes,
        )

    @property
    def storage(self) -> Storage:
        """The journal thread's last measurement of the records directory. Memory only."""
        return self._progress.storage

    @property
    def activity(self) -> Activity:
        """What the journal thread last said it was starting. Memory only."""
        return self._progress.activity

    # --- the thread --------------------------------------------------------

    def start(self) -> None:
        """Start the journal thread. A second call does nothing."""
        self._started()

    def _started(self) -> Thread:
        thread = self._thread
        if thread is None:
            thread = Thread(target=self._run, name="record-journal", daemon=True)
            self._thread = thread
            thread.start()
        return thread

    def _run(self) -> None:
        while not self._stopping.wait(self._period):
            self._cycle()
        # Whatever was queued before the stop, the closing included.
        self._cycle()

    def _cycle(self) -> None:
        try:
            self.drain()
        except Exception:  # the thread outlives its own bugs; the stall detector reports them
            _logger.exception("session journal cycle failed")

    def drain(self) -> None:
        """One cycle of the journal thread. Blocking I/O: never call it from the event loop.

        Public for the tests that run without the thread, one cycle at a time.
        """
        self._scribe.cycle(self._queue, self._dropped, self._unlogged)

    def _publish(self, progress: Progress) -> None:
        """The journal thread's one write to the producer's side: a whole value, swapped in."""
        self._progress = progress

    def request_stop(self) -> None:
        """Ask the thread to write what is queued and end. Returns at once.

        The last drain is the thread's own work even when it was never started
        (it is started here, to do just that): nothing on this side ever
        touches the disk, so the console's exit can ask from the event loop.
        """
        self._stopping.set()
        self._started()

    @property
    def stopped(self) -> bool:
        """Whether the thread has ended. ``False`` while it was never asked to."""
        thread = self._thread
        return thread is not None and not thread.is_alive()

    def stop(self, timeout: Seconds = STOP_TIMEOUT) -> bool:
        """:meth:`request_stop`, then wait for the thread. Blocking: never on the event loop.

        Returns whether the thread finished within ``timeout``. It is a daemon:
        a disk that never answers cannot keep the process from exiting.
        """
        self._stopping.set()
        thread = self._started()
        thread.join(timeout)
        return not thread.is_alive()
