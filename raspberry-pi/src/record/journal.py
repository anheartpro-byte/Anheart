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
the retention is removed; one that was not deposited never is.

The producer side (:meth:`Journal.open`, :meth:`Journal.submit`,
:meth:`Journal.close`, :meth:`Journal.status`) belongs to the event loop
thread, and to it alone: the bound relies on there being one producer.
"""

from __future__ import annotations

import logging
import shutil
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
from src.record.ecg import Header, RawBlock
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


type Item = Opening | Closing | Entry


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
    """Values refused because the queue was full, this session."""

    failures: int
    """Values the disk refused or that had no record to go to, this session."""

    pending: int
    path: Path | None
    free_bytes: int | None


def prepare_root(root: Path) -> None:
    """Create the records directory, private to the service user. Raises ``OSError``."""
    root.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    root.chmod(PRIVATE_MODE)


def measure(root: Path, at: Monotonic) -> Storage:
    """The free space under ``root`` at ``at``; ``free_bytes`` is ``None`` when unreadable."""
    try:
        return Storage(shutil.disk_usage(root).free, at)
    except OSError:
        return Storage(None, at)


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

    The producer reads only the :class:`Progress` this returns, a frozen value
    swapped in by one attribute assignment.
    """

    __slots__ = (
        "_clock",
        "_consumed",
        "_discarded",
        "_error",
        "_failures",
        "_path",
        "_probed_at",
        "_purged_at",
        "_retention_days",
        "_root",
        "_samples",
        "_session",
        "_storage",
        "_synced_at",
        "_warned",
        "_writer",
    )

    def __init__(self, root: Path, clock: Clock, retention_days: int | None) -> None:
        self._root: Path = root
        self._clock: Clock = clock
        self._retention_days: int | None = retention_days
        # Due at once: the first idle cycle after startup applies the retention.
        self._purged_at: Monotonic = Monotonic(clock.monotonic() - PURGE_PERIOD)
        self._writer: Writer | None = None
        self._session: int = 0
        self._consumed: int = 0
        self._samples: int = 0
        self._failures: int = 0
        self._discarded: int = 0
        self._warned: int = 0
        self._error: RecordError | None = None
        self._path: Path | None = None
        self._synced_at: Monotonic = clock.monotonic()
        self._probed_at: Monotonic = clock.monotonic()
        self._storage: Storage = self._claim_root()

    def _claim_root(self) -> Storage:
        try:
            prepare_root(self._root)
        except OSError as error:
            _logger.warning("records directory unusable: %s", describe_os_error(error))
            return Storage(None, self._probed_at)
        return measure(self._root, self._probed_at)

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
        )

    def cycle(self, queue: deque[Item], dropped: int, publish: Callable[[Progress], None]) -> None:
        """Write everything queued when the cycle began, then the periodic work.

        ``publish`` receives the progress every :data:`PUBLISH_PERIOD` while the
        cycle lasts, and once at the end.
        """
        now = self._clock.monotonic()
        published = now
        for _ in range(len(queue)):
            item = queue.popleft()
            self._consumed += 1
            self._samples += _sample_count(item)
            self._note(self._take(item, now, dropped))
            moment = self._clock.monotonic()
            if moment - published >= PUBLISH_PERIOD:
                published = moment
                publish(self.progress())
        self._checkpoint(now, dropped)
        publish(self.progress())

    def _note(self, outcome: Result[None, RecordError]) -> None:
        if isinstance(outcome, Err):
            self._failures += 1
            self._error = outcome.error

    def _take(self, item: Item, now: Monotonic, dropped: int) -> Result[None, RecordError]:
        try:
            match item:
                case Opening():
                    return self._open(item, now)
                case Closing():
                    return self._close(item, now, dropped)
                case _:
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
            case _ as unreachable:
                assert_never(unreachable)

    def _open(self, opening: Opening, now: Monotonic) -> Result[None, RecordError]:
        previous = self._writer
        if previous is not None:
            # The producer closes a session before opening the next. One that
            # was left open is still finalised, and says how it ended.
            self._writer = None
            self._note(previous.close(self._clock, "superseded"))
            self._note(previous.sync())
        self._session = opening.session
        self._failures = 0
        self._discarded = 0
        self._warned = 0
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
        closed = writer.close(
            ManualClock(epoch_millis=closing.ended_at), closing.end_reason, closing.observation
        )
        synced = writer.sync()
        return synced if isinstance(closed, Ok) else closed

    def _checkpoint(self, now: Monotonic, dropped: int) -> None:
        writer = self._writer
        if writer is not None and now - self._synced_at >= FSYNC_PERIOD:
            self._synced_at = now
            self._warn(writer, now, dropped)
            self._note(writer.sync())
        if now - self._probed_at >= PROBE_PERIOD:
            self._probed_at = now
            self._storage = measure(self._root, now)
        retention = self._retention_days
        if writer is None and retention is not None and now - self._purged_at >= PURGE_PERIOD:
            self._purged_at = now
            report = purge(self._root, self._clock.unix_millis(), retention)
            if report.removed or report.failed:
                _logger.warning(
                    "local retention (%d days): removed %s, could not remove %s",
                    retention,
                    list(report.removed),
                    list(report.failed),
                )

    def _warn(self, writer: Writer, now: Monotonic, dropped: int) -> None:
        """Say in the record itself what it is missing, once per change, if the disk lets us."""
        lost = dropped + self._failures
        if lost == self._warned:
            return
        error = self._error
        last = "" if error is None else f" last={error.operation}:{error.detail}"
        warning = Event(
            t=round(now - writer.manifest.clocks.monotonic_start, 3),
            kind=EventKind.WARNING,
            detail=f"record_degraded: dropped={dropped} failures={self._failures}{last}",
        )
        if isinstance(writer.event(warning), Ok):
            self._warned = lost


# =========================================================================
# The producer side
# =========================================================================


class Journal:
    """The bounded queue and its thread. See the module docstring.

    Mutable. The fields below are written by the event loop thread only, except
    ``_progress``, which the journal thread replaces whole; ``_dropped`` is the
    one producer field that thread reads (an ``int``, for the warning it writes
    into the record).
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
        self._scribe: Scribe = Scribe(root, clock, retention_days)
        self._progress: Progress = self._scribe.progress()
        self._session: int = 0
        self._open: bool = False
        self._put: int = 0
        self._put_samples: int = 0
        self._dropped: int = 0
        self._mark: tuple[int, Monotonic] = (0, clock.monotonic())
        self._stopping: ThreadEvent = ThreadEvent()
        self._thread: Thread | None = None

    @property
    def root(self) -> Path:
        """The records directory."""
        return self._root

    # --- the producer: event loop thread only -----------------------------

    def open(self, manifest: Manifest, privacy: Privacy) -> None:
        """A session was armed: queue the creation of its record. Never refused."""
        self._session += 1
        self._open = True
        self._dropped = 0
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
        cause: Cause | None = None
        if stalled:
            cause = Cause.STALLED
        elif failures:
            cause = Cause.WRITE_FAILED
        elif self._dropped:
            cause = Cause.QUEUE_FULL
        elif progress.storage.free_bytes is None:
            cause = Cause.STORAGE_UNAVAILABLE
        return JournalStatus(
            recording=self._open,
            degraded=cause is not None,
            cause=cause,
            error=error,
            dropped=self._dropped,
            failures=failures,
            pending=pending,
            path=progress.path if current else None,
            free_bytes=progress.storage.free_bytes,
        )

    @property
    def storage(self) -> Storage:
        """The journal thread's last measurement of the records directory. Memory only."""
        return self._progress.storage

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
        self._scribe.cycle(self._queue, self._dropped, self._publish)

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
