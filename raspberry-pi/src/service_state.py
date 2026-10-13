"""Whether this console keeps restarting: counted across processes, in one small file.

Every restart of the console is a new process that knows nothing of the one
before. What has to outlive a process is written here, next to the session
records (``<records root>/service-state.json``): when the console started
after a stop that was not a clean one, and whether it has been taken out of
service.

What is counted
---------------
A start is **unclean** when the process before it never wrote that it had
stopped cleanly: it was killed, it crashed, its control loop froze and the
service manager ended it, the power went. A clean stop is the console's own
controlled exit with code 0, and nothing else.

:data:`THRESHOLD` unclean starts within :data:`WINDOW`, during one run of the
system, is a restart loop: the console is **out of service**. It still starts,
serves its page, reads the drive, and stops and latches a drive it finds
running; it arms nothing. A machine whose console dies every few minutes is
not one to put a person in, and each of those deaths is an uncontrolled stop
of whatever session was running.

The instants compared are the console's monotonic clock, which is only
comparable within one start of the system. A stored state of another start of
the system (:func:`~src.record.cursor.boot_identity`) keeps no instant: a Pi
that is switched off at the wall every evening does not add up to a loop.

Out of service is kept until a named operator lifts it
-------------------------------------------------------
Once written, the state survives restarts and reboots, whatever the clock
says: somebody answers for putting the machine back in service
(:meth:`ServiceGuard.restore`, from the console's page).

This mechanism can never hold a machine for good:

* the page that says "out of service" is the page that lifts it;
* lifting it takes effect in memory at once, whether or not the disk takes
  the new state: a disk that refuses costs a second click after the next
  restart, never the machine;
* a state file that is absent is a first start. One that cannot be read is
  taken as an unclean stop of unknown history, counted as one, and never as a
  reason to be out of service: a damaged file is not a restart loop;
* removing the file by hand is the same as lifting it.

The start that follows an unclean stop
--------------------------------------
:attr:`ServiceStatus.unclean_stop` says so, and :attr:`ServiceStatus.open_record`
names the last session record when it was left open (no checksums: its session
was cut). An open record is reported once: it stays open for ever, and the
starts after it are not all starts after an unclean stop.

When the disk is touched
-----------------------
Everything here that touches the disk is blocking, and none of it runs on the
event loop while the console runs:

* :meth:`ServiceGuard.begin` only READS, at startup, before anything turns:
  the state file, a few hundred bytes. What it decides is kept in memory;
* that start is WRITTEN (:func:`store`, with its flush to the device) only
  once the first control tick has completed, so after the drive was read, and
  on a thread of its own: the one write that could be slow never stands
  between a start and the first look at the drive;
* :meth:`ServiceGuard.stopped` writes last of all, at exit, after the drive
  is stopped.

A process that dies before its start is written leaves the file as the one
before it left it: that start is not counted. It is a process that never
completed a tick, which the service manager counts for itself.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, assert_never

from pydantic import ConfigDict, TypeAdapter
from pydantic.dataclasses import dataclass as validated

from src.clock import Clock
from src.record.writer import create_private, describe_os_error, fsync_path
from src.result import Err, Ok, Result
from src.units import Seconds

_logger: Final[logging.Logger] = logging.getLogger(__name__)

STATE_NAME: Final[str] = "service-state.json"
"""Under the records root. Not a record's name, nor a marker's."""

TEMPORARY_NAME: Final[str] = ".service-state.tmp"
"""The state being written; renamed over :data:`STATE_NAME` once on the device."""

THRESHOLD: Final[int] = 3
"""This many unclean starts within :data:`WINDOW` take the console out of service."""

WINDOW: Final[Seconds] = Seconds(600.0)
"""Ten minutes: with the unit's two seconds between a death and the next start, a
console that cannot hold gets there in well under a minute, and one that dies once in
a session does not."""


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class Stored:
    """What one process leaves for the next. Replaced whole, never mutated."""

    schema_version: Literal[1] = 1

    boot_id: str | None = None
    """The start of the system the instants below were read in."""

    unclean_starts: tuple[float, ...] = ()
    """When the console started after an unclean stop: its monotonic clock, in seconds."""

    running: bool = False
    """``True`` from a start until a clean stop. Found ``True`` at a start: the stop was not."""

    out_of_service: bool = False

    reported_record: str | None = None
    """The open session record a start has already reported."""

    def __post_init__(self) -> None:
        """Refuse a state no console can have written: it is read as unusable."""
        if any(instant < 0 for instant in self.unclean_starts):
            raise ValueError("an instant of the monotonic clock is not negative")


_STORED: Final[TypeAdapter[Stored]] = TypeAdapter(Stored)


@dataclass(frozen=True, slots=True)
class Absent:
    """No state file: this console has never started here."""


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A state file that cannot be used: unreadable, truncated, of another version."""

    detail: str


type Previous = Stored | Absent | Unreadable


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceStatus:
    """What the console says of its own restarts, to the page and to the logbook."""

    out_of_service: bool = False
    unclean_starts: int = 0
    """Unclean starts within :data:`WINDOW`, this one included."""

    unclean_stop: bool = False
    """Whether this start follows a stop that was not clean, or found a session record
    left open that no start had reported."""

    open_record: str | None = None
    """That record's name, when this start is the one that reports it."""

    persisted: bool = True
    """Whether the state was written. ``False``: what this page shows holds until the
    console restarts, and the count of unclean starts is not kept."""


IN_SERVICE: Final[ServiceStatus] = ServiceStatus()
"""A console that keeps no state (the tests, a console built without a records root)."""


@dataclass(frozen=True, slots=True)
class NotOutOfService:
    """There is nothing to lift: the console is in service."""


def load(path: Path) -> Previous:
    """The state the previous process left at ``path``. Never raises."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Absent()
    except OSError as error:
        return Unreadable(describe_os_error(error))
    try:
        return _STORED.validate_json(raw)
    except ValueError:  # truncated, not JSON, of another version, or a field out of range
        return Unreadable("invalid")


def store(path: Path, stored: Stored) -> Result[None, str]:
    """Replace the state at ``path``: a private file, flushed, then renamed. Blocking.

    A reader, and a power cut, see the old state or the new one. Never raises.
    """
    temporary = path.with_name(TEMPORARY_NAME)
    try:
        temporary.unlink(missing_ok=True)
        create_private(temporary, _STORED.dump_json(stored) + b"\n")
        fsync_path(temporary)
        temporary.replace(path)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        return Err(describe_os_error(error))
    return Ok(None)


@dataclass(frozen=True, slots=True)
class _Carried:
    """What a start takes over from the state the previous process left."""

    unclean: bool
    earlier: tuple[float, ...] = ()
    held: bool = False
    reported: str | None = None


def _carried(previous: Previous, boot_id: str | None, now: float) -> _Carried:
    """Read ``previous`` for a start at ``now``. Exhaustive over what :func:`load` returns."""
    match previous:
        case Absent():
            return _Carried(unclean=False)
        case Unreadable():
            return _Carried(unclean=True)
        case Stored():
            # Instants are comparable within one start of the system only. One
            # from after `now` is of another start too (of a system that could
            # not name itself): dropped like the rest.
            same_start = previous.boot_id == boot_id
            return _Carried(
                unclean=previous.running,
                earlier=tuple(
                    instant
                    for instant in previous.unclean_starts
                    if same_start and 0 <= now - instant < WINDOW
                ),
                held=previous.out_of_service,
                reported=previous.reported_record,
            )
    raise assert_never(previous)


def next_start(
    previous: Previous, *, boot_id: str | None, now: float, open_record: str | None
) -> tuple[Stored, ServiceStatus]:
    """What this start writes, and what it says, given what the previous process left.

    Pure. ``now`` is the console's monotonic clock; ``open_record`` the name of
    the newest session record when it is not closed.
    """
    carried = _carried(previous, boot_id, now)
    starts = (*carried.earlier, now) if carried.unclean else carried.earlier
    fresh_record = open_record is not None and open_record != carried.reported
    out = carried.held or len(starts) >= THRESHOLD
    stored = Stored(
        boot_id=boot_id,
        unclean_starts=starts,
        running=True,
        out_of_service=out,
        reported_record=carried.reported if open_record is None else open_record,
    )
    status = ServiceStatus(
        out_of_service=out,
        unclean_starts=len(starts),
        unclean_stop=carried.unclean or fresh_record,
        open_record=open_record if fresh_record else None,
    )
    return stored, status


class ServiceGuard:
    """The state of this run, and the file it is kept in. See the module docstring.

    Mutable. :meth:`begin` reads the disk at startup and :meth:`stopped` writes
    it at exit; in between, the event loop thread reads :attr:`status` and
    :attr:`stored` and calls :meth:`restore` and :meth:`saved`, which touch
    memory only. The writes of a running console are :func:`store`, called on
    another thread with a value this object handed out.
    """

    __slots__ = ("_clock", "_path", "_status", "_stored")

    def __init__(self, root: Path, clock: Clock) -> None:
        """``root``: the records directory. Nothing is read or written here."""
        self._path: Path = root / STATE_NAME
        self._clock: Clock = clock
        self._stored: Stored = Stored()
        self._status: ServiceStatus = IN_SERVICE

    @property
    def path(self) -> Path:
        """Where the state is kept."""
        return self._path

    @property
    def status(self) -> ServiceStatus:
        """What this run says of itself. Memory only."""
        return self._status

    @property
    def stored(self) -> Stored:
        """This run's state as it is to be written. Memory only."""
        return self._stored

    def begin(self, *, boot_id: str | None, open_record: str | None) -> ServiceStatus:
        """This process starts: read what the previous one left, and decide. Blocking.

        Startup I/O, before anything turns: one small read. Nothing is written
        here: :attr:`stored` is what the caller writes once the drive has been
        read (see the module docstring).
        """
        previous = load(self._path)
        if isinstance(previous, Unreadable):
            _logger.error(
                "service state unreadable (%s): taken as an unclean stop of unknown history",
                previous.detail,
            )
        stored, status = next_start(
            previous, boot_id=boot_id, now=self._clock.monotonic(), open_record=open_record
        )
        self._stored = stored
        self._status = status
        return self._status

    def restore(self) -> Result[Stored, NotOutOfService]:
        """Put the console back in service, now. Memory only: returns the state to write.

        The count starts again from nothing: whoever lifts it has answered for
        the restarts before.
        """
        if not self._status.out_of_service:
            return Err(NotOutOfService())
        self._stored = replace(self._stored, unclean_starts=(), out_of_service=False)
        self._status = replace(self._status, out_of_service=False, unclean_starts=0)
        return Ok(self._stored)

    def saved(self, outcome: Result[None, str]) -> None:
        """What a :func:`store` of this run's state answered. Memory only."""
        if isinstance(outcome, Err):
            _logger.error("service state not written (%s)", outcome.error)
        self._status = replace(self._status, persisted=isinstance(outcome, Ok))

    def stopped(self) -> None:
        """The console has stopped cleanly: say so for the next start. Blocking.

        Called last, once the drive is stopped and the session record closed.
        A process that never gets here is what an unclean stop is.
        """
        self._stored = replace(self._stored, running=False)
        self.saved(store(self._path, self._stored))
