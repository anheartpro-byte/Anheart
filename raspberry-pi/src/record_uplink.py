"""Sending sessions up to the dashboard from their records on disk, with a cursor.

What the dashboard receives of a session (its 1 Hz telemetry, its events, its
end) is read back from the session's local record
(:mod:`src.record.upload`), from the place the dashboard last acknowledged
(:mod:`src.record.cursor`). Nothing of a session waits in memory: what is not
acknowledged yet is on disk, and stays there across a lost link, a dashboard
of another contract, or a console that restarts, by choice or not.

One session, in order
---------------------
1. **declared** (a session started at the machine), under the reference its
   record carries, so that declaring it again, even after a restart, gives the
   same session; or **confirmed** (a launch from the dashboard). Nothing is
   sent before that. A confirmation the dashboard answers with anything but a
   yes has the running session stopped at once: the machine does not run on
   for a session the dashboard does not hold started. For the session that is
   RUNNING this is :meth:`RecordUplink.greet`, called from the stop watch's
   task and not from the sending: it is the first thing the dashboard hears
   of a session and what lets its stop be asked for, so it waits behind no
   reading of a record and no catching up, and reads nothing from the disk;
2. **telemetry** then **events**, read from the cursor, one bounded batch at a
   time. The cursor moves when the dashboard acknowledges a batch, and is then
   written to disk;
3. the **end**, once the record is closed and its telemetry is acknowledged:
   the reason the record holds. A record that was never closed (the console
   was killed) ends as ``interrupted``, at its last tick.

Which session goes first
------------------------
The session that is running. While it has anything ready to send, nothing
else is sent. Then the session that has just ended, which the dashboard still
shows as running. Then every other, oldest first: those a previous run of the
console left unfinished (found once, when nothing else is being sent), and
those that ended in this run while the link was down.

One pace for all of it: never two batches of telemetry less than
:data:`CATCH_UP_PERIOD` apart, whichever sessions they are of, each of at
most 300 points. A running session that is up to date sends every
:data:`TELEMETRY_PERIOD`.

What the link answers
---------------------
This module knows four answers (:data:`Answer`), and :mod:`src.cloud_sync`
tells which one an HTTP exchange was:

* :class:`Acked`: received. The cursor moves past everything the request
  carried, stored or not: the dashboard counts what it did not store, and that
  count is logged and kept in the cursor;
* :class:`Held`: nothing was received, and nothing will be until something
  changes on the link (no network, a dashboard of another contract, a key that
  is not accepted, a server error). **Nothing moves and nothing is dropped**:
  the same request is made again later. A session in progress under a refused
  contract is therefore sent whole, and closed, once the two sides agree again;
* :class:`RouteMissing`: the dashboard does not know the route. For the events
  (a dashboard older than contract 1.1) they stay owed, and the telemetry and
  the end go on without them;
* :class:`Refusal`: the dashboard read the request and refused it. One whose
  stable code says it will always be refused is passed over at once; another
  is tried :data:`MAX_REFUSALS` times, then passed over. Either way it is
  counted in the cursor and logged, and what follows is not held back.

Never in the way
----------------
:meth:`RecordUplink.step` runs in the dashboard task, never in the control
tick. Every file is read and written on a record I/O thread
(:class:`~src.record.export.RecordIo`): one at a time, each bounded in size and
in time, on a daemon thread. A disk that does not answer costs the sending,
which waits; it holds neither a tick nor the exit of the console.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import functools
import logging
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum, unique
from pathlib import Path
from typing import Final, Protocol, assert_never
from uuid import uuid4

from src.clock import Clock
from src.record.cursor import Cursor, UnreadableCursor, store
from src.record.export import BUSY, TIMEOUT, RecordIo
from src.record.rows import JsonValue
from src.record.schema import RecordError
from src.record.upload import (
    INTERRUPTED,
    MISALIGNED,
    Batch,
    Events,
    Head,
    Opened,
    Points,
    open_record,
    owed_records,
    read_batch,
    read_head,
    reference_in,
)
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, UnixMillis, elapsed

_logger: Final[logging.Logger] = logging.getLogger(__name__)

TELEMETRY_PERIOD: Final[Seconds] = Seconds(5.0)
"""How often a running session that is up to date sends what it has measured since."""

CATCH_UP_PERIOD: Final[Seconds] = Seconds(2.0)
"""The least time between two batches of telemetry, whichever sessions they are of."""

RETRY_PERIOD: Final[Seconds] = Seconds(15.0)
"""After the link held a request, or refused one it may accept later: the wait."""

EVENTS_RETRY_PERIOD: Final[Seconds] = Seconds(60.0)
"""How often a dashboard that does not know the events route is asked again."""

BIND_GRACE: Final[Seconds] = Seconds(10.0)
"""How long a session started at the machine waits for its record before being declared without."""

CLOSE_GRACE: Final[Seconds] = Seconds(10.0)
"""How long an ended session waits for its record to be closed before its end is sent anyway."""

READ_TIMEOUT: Final[Seconds] = Seconds(2.0)
"""How long one reading or one cursor write may take before the step goes on without it."""

SCAN_TIMEOUT: Final[Seconds] = Seconds(10.0)
"""The same, for the one listing of the records directory at startup."""

MAX_REFUSALS: Final[int] = 3
"""Times a request the dashboard refuses without a final code is made before it is passed over."""

LOCAL_PATH: Final[str] = "/api/machine/training/local"
START_PATH: Final[str] = "/api/machine/training/start"
TELEMETRY_PATH: Final[str] = "/api/machine/training/telemetry"
EVENTS_PATH: Final[str] = "/api/machine/training/events"
END_PATH: Final[str] = "/api/machine/training/end"

UNOBSERVED_END: Final[str] = "fin non observee par le lien"
"""The end of a session whose runtime was reset for the next one before the link saw it end."""

EARLIEST_BELIEVABLE_START_MS: Final[int] = 1_704_067_200_000
"""1 January 2024, UTC. A record dated before it was dated by a clock that was never set
(a Raspberry Pi has no real-time clock). The dashboard holds the same date as the
earliest a session can have started."""

UNDATED_MARGIN: Final[Seconds] = Seconds(60.0)
"""For a record of an earlier start of the system dated by such a clock: how long before
this console started it is taken to have ended, at the least. Its measurements are then
never read as current: they are a minute old at the very least."""

SUCCESSFUL_ENDS: Final[frozenset[str]] = frozenset({"programme_complete", "operator_stop"})
"""The end reasons of a record the dashboard shows as completed; every other is a failure."""

DECLARED_WITHOUT_RECORD: Final[str] = (
    "seance declaree sans son enregistrement : le tableau de bord n'en recevra les mesures "
    "que si l'enregistrement apparait"
)
CURSOR_UNWRITTEN: Final[str] = (
    "curseur de synchronisation non ecrit : si la console redemarre, la suite de cette "
    "seance pourrait ne pas arriver au tableau de bord"
)
LIST_LOST: Final[str] = (
    "liste des enregistrements anterieurs a la synchronisation perdue et refaite : "
    "{count} enregistrement(s) sans curseur mis de cote, non envoyes au tableau de bord"
)
"""What the operator reads on the console when a session will not reach the dashboard as
it should. Said once per session for the first, once per failure of the disk for the
second, once per start of the console for the third."""

START_UNKNOWN: Final[str] = "route de confirmation inconnue du tableau de bord"
"""Why a start was not taken by a dashboard that does not know the route at all."""


# =========================================================================
# What the link answers
# =========================================================================


@dataclass(frozen=True, slots=True)
class Acked:
    """Received: everything the request carried is acknowledged."""

    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Held:
    """Not received, and not about this request: the same one is made again later."""

    detail: str
    refused: bool = False
    """Whether the dashboard answered at all: ``True`` for a refusal that is about the
    link (its key, its contract, an error of its own), ``False`` when nothing came back."""


@dataclass(frozen=True, slots=True)
class RouteMissing:
    """The dashboard does not know this route: it is older than the contract that added it."""


@dataclass(frozen=True, slots=True)
class Refusal:
    """The dashboard read the request and refused it."""

    detail: str
    final: bool
    """Whether its stable code says the same request will always be refused."""


type Answer = Acked | Held | RouteMissing | Refusal


class Sender(Protocol):
    """The one thing this module asks of the link. Never raises."""

    @abstractmethod
    async def send(self, path: str, body: Mapping[str, JsonValue]) -> Answer:
        """POST ``body`` to ``path`` and say which of the four answers came back."""


# =========================================================================
# What the link tells this module
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordSource:
    """The records directory, as the sending may use it."""

    root: Path
    current: Callable[[], Path | None]
    """The directory of the record the console has open, or last opened; memory only."""

    io: RecordIo
    """The threads every read and write here runs on."""

    boot_id: str | None
    """What names this start of the system (:func:`~src.record.cursor.boot_identity`)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Declaration:
    """What the dashboard is told of a session started at the machine, beyond its dates."""

    kind: str
    operator: str
    profile_id: str | None = None
    profile_name: str | None = None
    zone_low_bpm: int | None = None
    zone_high_bpm: int | None = None
    total_duration_s: float | None = None
    subject_hr_max: int | None = None
    occupancy: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ArmedSession:
    """A session the runtime has just armed."""

    declaration: Declaration
    started_at: UnixMillis
    cloud_session_id: str | None
    """The launch from the dashboard this answers; ``None`` for a start at the machine."""


@dataclass(frozen=True, slots=True)
class RuntimeEnd:
    """How the runtime itself says a session ended."""

    failed: bool
    reason: str


# =========================================================================
# One session the dashboard is owed
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Ended:
    """The runtime's own end of a session of this run, kept in case its record never closes."""

    end: RuntimeEnd
    at_ms: int | None
    """On the session's axis; ``None`` for a launch that never started."""

    since: Monotonic


@dataclass(frozen=True, slots=True)
class _End:
    failed: bool
    reason: str
    at_ms: int | None


@dataclass(slots=True)
class _Owed:
    """One session the dashboard is owed news of.

    Mutable on purpose, and owned by exactly one :class:`RecordUplink`: it is
    the delivery state of that session, which changes as the link answers. It
    holds no measurement: those are on disk, behind ``cursor``.
    """

    declaration: Declaration
    start_ms: int
    """The start of the session on the machine's own axis (unix ms)."""

    origin: Monotonic | None
    """That start on this boot's monotonic clock; ``None`` when it cannot be told.

    From it comes the age the dashboard is told, which is what makes the
    dashboard date the session on its own clock instead of the machine's."""

    local_ref: str
    cursor: Cursor
    named: bool = False
    """``local_ref`` is the reference the record carries, not one made up while waiting."""

    greeting: bool = False
    """The stop watch's task is declaring it or confirming its start right now."""

    record: Path | None = None
    stored: Cursor | None = None
    """The cursor as the disk holds it; written again whenever ``cursor`` differs."""

    refused_launch: bool = False
    """A launch from the dashboard that was refused here: only its end is owed."""

    unrecorded: bool = False
    """Declared without a record: none had appeared when it had to be declared."""

    stopped: bool = False
    """The console was asked to stop it on the dashboard's account (a stop asked for
    there, or a start it did not take). It is asked once, and no stop is looked for after."""

    left: Path | None = None
    """For a session that ended before it was bound: the record the console had open then."""

    caught_up: bool = False
    last_transfer: Monotonic | None = None
    next_try: Monotonic | None = None
    refusals: int = 0
    events_retry: Monotonic | None = None
    ended: _Ended | None = None


@unique
class _Wait(Enum):
    """Why a request was not acknowledged, when it is not a final refusal."""

    HOLD = "hold"
    RETRY = "retry"
    MISSING = "missing"


@unique
class _Step(Enum):
    """What one pass over a session came to."""

    HELD = "held"
    """The link answers nothing: nothing else is tried in this step."""

    BUSY = "busy"
    """More of this session is ready, or one of its requests is to be made again."""

    IDLE = "idle"
    """Nothing more of it to send for now."""

    DONE = "done"
    """Nothing of it is owed any more."""

    DROPPED = "dropped"
    """Set aside: it is taken up again at the next start of the console."""


_NOTHING_TO_DECLARE: Final[Declaration] = Declaration(kind="auto", operator="")
"""For a launch that never started: the dashboard already knows it."""


def declaration_from(head: Head) -> Declaration:
    """What a manifest alone says of a session: no name, and no programme name."""
    profile = head.profile
    return Declaration(
        kind=head.kind,
        operator=head.operator,
        zone_low_bpm=None if profile is None else profile.zone_low_bpm,
        zone_high_bpm=None if profile is None else profile.zone_high_bpm,
        total_duration_s=None if profile is None else profile.total_duration_s,
        subject_hr_max=None if profile is None else profile.subject_hr_max,
        occupancy=head.occupancy if head.kind == "manual" else None,
    )


def _write(record: Path, cursor: Cursor) -> Result[None, RecordError]:
    wrote = store(record, cursor)
    return Ok(None) if isinstance(wrote, Ok) else Err(RecordError("sync", wrote.error.detail))


def _due(last: Monotonic | None, now: Monotonic, period: Seconds) -> bool:
    return last is None or elapsed(last, now) >= period


def _waiting(until: Monotonic | None, now: Monotonic) -> bool:
    return until is not None and now < until


def _greeted(cursor: Cursor) -> bool:
    """Whether the dashboard holds the session started: declared, or its start confirmed."""
    return cursor.session_id is not None and cursor.start_confirmed


def _last_instant(owed: _Owed, batch: Batch) -> int:
    """The last instant a record is known to have run, on the session's axis (unix ms).

    Its last tick. When the end of the file holds none (a write cut by the
    kill, then bytes that are no line), the last point the record gave, which
    is the first tick of its last second: less than a second earlier. With no
    point at all, its start.
    """
    if batch.last_tick_ms is not None:
        return owed.start_ms + batch.last_tick_ms
    return owed.start_ms if owed.cursor.last_t is None else owed.cursor.last_t


def _halted(wait: _Wait) -> _Step:
    """What a pass comes to when a request was not acknowledged."""
    return _Step.HELD if wait is _Wait.HOLD else _Step.BUSY


def _refused(answer: Acked | Refusal) -> int:
    """One more request refused for good, or none."""
    return 1 if isinstance(answer, Refusal) else 0


def _rejected(answer: Acked | Refusal, what: str, session_id: str) -> int:
    """How many items the dashboard acknowledged without storing them. Said, never silent."""
    if isinstance(answer, Refusal):
        return 0
    count = answer.document.get("rejected")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return 0
    _logger.warning(
        "dashboard: %d %s of session %s acknowledged but not stored (dated outside the session)",
        count,
        what,
        session_id,
    )
    return count


def _passed_over(count: int, what: str, session_id: str) -> None:
    """Lines of a record that are not of its format, left behind the cursor. Said too."""
    if count > 0:
        _logger.warning(
            "dashboard: %d %s of the record of session %s cannot be read: passed over",
            count,
            what,
            session_id,
        )


class RecordUplink:
    """The sending, stepped from the dashboard task. See the module docstring."""

    __slots__ = (
        "_backlog",
        "_born",
        "_catching",
        "_clock",
        "_held_until",
        "_io",
        "_last_batch",
        "_live",
        "_said",
        "_scan_at",
        "_scanned",
        "_sender",
        "_source",
        "_start_refused",
        "_unwritten",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        sender: Sender,
        source: RecordSource | None,
        start_refused: Callable[[str], None],
        said: Callable[[str], None],
    ) -> None:
        """``source``: ``None`` on a console that records nothing; it then only declares
        and ends its sessions. ``start_refused``: called with the dashboard's words when
        it does not take the start of the session that is running. ``said``: called with
        a sentence for the operator when something of a session will not reach the
        dashboard as it should."""
        self._clock: Clock = clock
        self._born: Monotonic = clock.monotonic()
        self._sender: Sender = sender
        self._source: RecordSource | None = source
        self._io: RecordIo = RecordIo(1) if source is None else source.io
        self._start_refused: Callable[[str], None] = start_refused
        self._said: Callable[[str], None] = said
        # Delivery state, mutated only by this object's own step and by the
        # three calls of the link below, all on the event loop.
        self._live: _Owed | None = None
        self._catching: _Owed | None = None
        self._backlog: tuple[str, ...] = ()
        self._scanned: bool = False
        self._scan_at: Monotonic | None = None
        self._held_until: Monotonic | None = None
        self._last_batch: Monotonic | None = None
        self._unwritten: bool = False

    # --- read access -----------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a session of this console is running, as far as the link was told."""
        return self._live is not None

    @property
    def session_id(self) -> str | None:
        """The dashboard's identifier of the running session, once known."""
        return None if self._live is None else self._live.cursor.session_id

    @property
    def following(self) -> str | None:
        """The running session while the dashboard holds it started and not ended.

        Only then may its stop be asked for: before, the dashboard does not
        know it runs; after, the dashboard would answer that it no longer
        holds it active, which reads as a request to stop. And not once a stop
        was forwarded for it: it is asked for once.
        """
        live = self._live
        if (
            live is None
            or live.stopped
            or not live.cursor.start_confirmed
            or live.cursor.end == "sent"
        ):
            return None
        return live.cursor.session_id

    @property
    def refusal_owed(self) -> bool:
        """Whether a launch refused here still has its end to deliver.

        No launch is asked for meanwhile: the dashboard still holds that one
        waiting, and would hand it out again.
        """
        owed = self._catching
        return owed is not None and owed.refused_launch

    @property
    def owed(self) -> int:
        """Sessions the dashboard has not acknowledged whole, as far as this run knows."""
        live = self._live
        running = 0 if live is None or live.cursor.state == "complete" else 1
        return running + (0 if self._catching is None else 1) + len(self._backlog)

    # --- what the link tells it ------------------------------------------

    async def greet(self) -> None:
        """Tell the dashboard of the running session: declare it, or confirm its start.

        Called from the stop watch's task, never from :meth:`step`: no reading
        of a record and no catching up comes before it, and it reads nothing
        from the disk itself. A session started at the machine is declared as
        soon as its record's directory exists, under the reference its name
        carries; a launch from the dashboard is confirmed as soon as it is
        armed. Until then nothing of the session is sent, and its stop cannot
        be asked for.
        """
        now = self._clock.monotonic()
        live = self._live
        if live is None or live.cursor.state == "complete" or _greeted(live.cursor):
            return
        if _waiting(self._held_until, now) or _waiting(live.next_try, now):
            return
        live.greeting = True
        try:
            session_id = live.cursor.session_id
            if session_id is None:
                self._name(live)
                await self._declare(now, live)
            else:
                await self._confirm(now, live, session_id)
        finally:
            live.greeting = False

    def _name(self, owed: _Owed) -> None:
        """Take the reference of the running session's record from its name, reading nothing."""
        source = self._source
        if owed.named or source is None:
            return
        path = source.current()
        reference = None if path is None else reference_in(path.name)
        if reference is not None:
            owed.local_ref = reference
            owed.named = True

    def stop_forwarded(self) -> None:
        """The console was asked to stop the running session on the dashboard's account.

        Kept with that session, and gone with it: it neither hides a stop asked
        for in the next one nor stands for one.
        """
        if self._live is not None:
            self._live.stopped = True

    def begin(self, session: ArmedSession) -> None:
        """A session was armed: it is the one that goes first from now on."""
        now = self._clock.monotonic()
        previous = self._live
        if previous is not None:
            # Its end fell between two steps of the link: the runtime was reset
            # for this start and its reason is gone. Its record says how it ended.
            self._retire(previous, RuntimeEnd(True, UNOBSERVED_END), now)
        remote = session.cloud_session_id
        source = self._source
        self._live = _Owed(
            declaration=session.declaration,
            start_ms=int(session.started_at),
            origin=now,
            local_ref=uuid4().hex,
            cursor=Cursor(
                boot_id=None if source is None else source.boot_id,
                session_id=remote,
                start_confirmed=remote is None,
            ),
        )

    def finished(self, end: RuntimeEnd) -> None:
        """The runtime has finished the running session."""
        live = self._live
        if live is not None:
            self._live = None
            if self._source is not None:
                # Read from memory, now: at the next start it is another session's.
                live.left = self._source.current()
            self._retire(live, end, self._clock.monotonic())

    def owe_refusal(self, cloud_session_id: str, reason: str) -> None:
        """A launch from the dashboard was refused here: it is owed as a failed session."""
        self._take(
            _Owed(
                declaration=_NOTHING_TO_DECLARE,
                start_ms=0,
                origin=None,
                local_ref="",
                cursor=Cursor(session_id=cloud_session_id),
                refused_launch=True,
                ended=_Ended(RuntimeEnd(True, reason), None, self._clock.monotonic()),
            )
        )

    def _retire(self, owed: _Owed, end: RuntimeEnd, now: Monotonic) -> None:
        if owed.cursor.state == "complete":
            return
        age = self._age_ms(now, owed)
        owed.ended = _Ended(end, None if age is None else owed.start_ms + age, now)
        self._take(owed)

    def _take(self, owed: _Owed) -> None:
        """Make ``owed`` the session being caught up: the one that just ended comes first."""
        waiting = self._catching
        if waiting is not None:
            if waiting.record is None:
                # No record to take it up from later, and one slot: the older is given up.
                _logger.warning(
                    "dashboard: the end of %s could not be delivered and is given up",
                    waiting.cursor.session_id or waiting.local_ref,
                )
            else:
                self._queue(waiting.record.name)
        self._catching = owed

    def _queue(self, *names: str) -> None:
        """Add records to those waiting, which stay oldest first.

        The name of a record begins with the date of its start: the order of
        the names is the order of the sessions.
        """
        self._backlog = tuple(sorted({*self._backlog, *names}))

    # --- the step ----------------------------------------------------------

    async def step(self) -> None:
        """One pass: the running session first, then one step of catching up."""
        now = self._clock.monotonic()
        live = self._live
        source = self._source
        if live is not None and source is not None:
            # Whatever the link says: a record that has its cursor is one the
            # console takes up again after a restart.
            await self._bind(live, source.current())
        if self._held_until is not None and now < self._held_until:
            return
        if live is not None:
            outcome = await self._serve(now, live)
            if outcome is _Step.HELD or outcome is _Step.BUSY:
                return
        await self._catch_up(now)

    async def _catch_up(self, now: Monotonic) -> None:
        owed = self._catching
        if owed is None:
            owed = await self._next(now)
            if owed is None:
                return
            self._catching = owed
        # A session that ended before the link had tied it to its record.
        await self._bind(owed, owed.left)
        outcome = await self._serve(now, owed, live=False)
        # `is owed`: a session that ended during the request may have taken the place.
        if (outcome is _Step.DONE or outcome is _Step.DROPPED) and self._catching is owed:
            self._catching = None

    async def _next(self, now: Monotonic) -> _Owed | None:
        """The oldest record still owed, loaded from disk; ``None`` when there is none now."""
        source = self._source
        if source is None or not await self._listed(now, source) or not self._backlog:
            return None
        name = self._backlog[0]
        opened = await self._disk(functools.partial(open_record, source.root, name), READ_TIMEOUT)
        if self._catching is not None:
            return None  # a session ended while the disk was read: it goes first
        if isinstance(opened, Err):
            if opened.error.detail not in (BUSY, TIMEOUT):
                _logger.warning(
                    "dashboard: record %s cannot be read (%s): left until the next start",
                    name,
                    opened.error.detail,
                )
                self._forget(name)
            return None  # a disk that is only slow: the same record at the next step
        self._forget(name)
        return self._resumed(opened.value, source)

    async def _listed(self, now: Monotonic, source: RecordSource) -> bool:
        """Find, once, the records an earlier run of the console left owed. Whether it is done."""
        if self._scanned:
            return True
        if self._scan_at is not None and now < self._scan_at:
            return False
        open_now = source.current()
        listed = await self._disk(
            functools.partial(
                owed_records, source.root, None if open_now is None else open_now.name
            ),
            SCAN_TIMEOUT,
        )
        if isinstance(listed, Err):
            self._scan_at = Monotonic(now + RETRY_PERIOD)
            return False
        self._scanned = True
        if listed.value.set_aside:
            _logger.warning(
                "dashboard: %d records without a cursor were set aside with a list of older "
                "records that had to be made again: they are not sent",
                listed.value.set_aside,
            )
            self._said(LIST_LOST.format(count=listed.value.set_aside))
        known = {
            owed.record.name
            for owed in (self._live, self._catching)
            if owed is not None and owed.record is not None
        }
        self._queue(*(name for name in listed.value.names if name not in known))
        return True

    def _forget(self, name: str) -> None:
        """Take ``name`` out of the records waiting: by name, the list may have moved."""
        self._backlog = tuple(waiting for waiting in self._backlog if waiting != name)

    def _resumed(self, opened: Opened, source: RecordSource) -> _Owed | None:
        head = opened.head
        read = opened.cursor
        if isinstance(read, Cursor):
            if read.state == "complete":
                return None
            cursor = read
        else:
            if isinstance(read, UnreadableCursor):
                _logger.warning(
                    "dashboard: the cursor of %s cannot be used (%s): sent again from its start",
                    opened.record.name,
                    read.detail,
                )
            else:
                _logger.warning(
                    "dashboard: record %s was never tied to a cursor: sent from its start",
                    opened.record.name,
                )
            # From the beginning: the dashboard stores once what it already has.
            cursor = Cursor(
                local_ref=head.local_ref,
                session_id=head.remote_id,
                start_confirmed=head.remote_id is None,
            )
        return _Owed(
            declaration=declaration_from(head),
            start_ms=head.start_ms,
            origin=self._origin(opened, cursor, source),
            local_ref=head.local_ref,
            cursor=cursor,
            named=True,
            record=opened.record,
            stored=read if isinstance(read, Cursor) else None,
        )

    def _origin(self, opened: Opened, cursor: Cursor, source: RecordSource) -> Monotonic | None:
        """When a record of an earlier run started, on the monotonic clock of this boot.

        * made during this start of the system: the monotonic reading its
          manifest holds is still on the same clock. Its age is exact;
        * made during an earlier one, and dated believably: ``None``. Nothing
          links the two clocks; the date its manifest carries stands;
        * made during an earlier one, by a clock that was never set: its true
          date is lost. It ended before this console started, so it is placed
          at the latest it can have begun, :data:`UNDATED_MARGIN` earlier
          still. It is then shown at a date of the dashboard's own clock, and
          nothing of it can be read as measured just now.
        """
        head = opened.head
        if cursor.boot_id is not None and cursor.boot_id == source.boot_id:
            return Monotonic(head.monotonic_start)
        if head.start_ms >= EARLIEST_BELIEVABLE_START_MS:
            return None
        lasted = (opened.last_tick_ms or 0) / 1000
        return Monotonic(self._born - lasted - UNDATED_MARGIN)

    # --- one session --------------------------------------------------------

    async def _serve(self, now: Monotonic, owed: _Owed, *, live: bool = True) -> _Step:
        if owed.cursor.state == "complete":
            return _Step.IDLE
        if owed.next_try is not None and now < owed.next_try:
            return _Step.BUSY
        outcome = await self._deliver(now, owed, live=live)
        await self._persist(owed)
        return outcome

    async def _bind(self, owed: _Owed, path: Path | None) -> None:
        """Tie a session of this run to ``path``, the record the console opened for it.

        Whenever that record appears, even after the session had to be
        declared without it: what the dashboard was told stands, and the
        measurements follow.
        """
        if owed.record is not None or path is None:
            return
        read = await self._disk(functools.partial(read_head, path), READ_TIMEOUT)
        if isinstance(read, Err):
            return  # the manifest is being written, or the disk is slow: at the next step
        head = read.value
        owed.record = path
        owed.start_ms = head.start_ms
        owed.local_ref = head.local_ref
        owed.named = True
        owed.cursor = replace(owed.cursor, local_ref=head.local_ref)
        # Written at once, before any answer and whatever the link says: the
        # cursor on disk is what lets the console take the record up again
        # where the dashboard stopped, with the age of its session.
        await self._persist(owed)

    async def _persist(self, owed: _Owed) -> None:
        record = owed.record
        cursor = owed.cursor
        if record is None or cursor == owed.stored:
            return
        wrote = await self._disk(functools.partial(_write, record, cursor), READ_TIMEOUT)
        if isinstance(wrote, Ok):
            owed.stored = cursor
            self._unwritten = False
        elif not self._unwritten:
            self._unwritten = True
            _logger.warning(
                "dashboard: the cursor of %s could not be written (%s): after a restart "
                "the record is sent again from where its cursor on disk stands, from its "
                "start if it has none",
                record.name,
                wrote.error.detail,
            )
            self._said(CURSOR_UNWRITTEN)

    async def _deliver(self, now: Monotonic, owed: _Owed, *, live: bool) -> _Step:
        """Send what follows the cursor, then the end, once the dashboard holds the session.

        The running session is told to the dashboard by :meth:`greet`, from
        the stop watch's task: here it only waits for that. A session of
        before is declared or confirmed here, in its turn.
        """
        if owed.greeting or (live and not _greeted(owed.cursor)):
            return _Step.BUSY
        session_id = owed.cursor.session_id
        if session_id is None:
            declared = await self._declare(now, owed)
            if isinstance(declared, _Step):
                return declared
            session_id = declared
        if not owed.cursor.start_confirmed:
            waiting = await self._confirm(now, owed, session_id)
            if waiting is not None:
                return waiting
        record = owed.record
        if record is None:
            return await self._end_from_memory(now, owed, session_id)
        return await self._transfer(now, owed, record, session_id, live=live)

    def _age_ms(self, now: Monotonic, owed: _Owed) -> int | None:
        """How long ago the session started, on the monotonic clock; ``None`` if untold."""
        origin = owed.origin
        return None if origin is None or now < origin else round((now - origin) * 1000)

    async def _declare(self, now: Monotonic, owed: _Owed) -> str | _Step:
        """Declare a session started at the machine. Its dashboard identifier, or why not yet."""
        if not owed.named and not owed.unrecorded:
            origin = owed.origin
            waited = BIND_GRACE if origin is None else elapsed(origin, now)
            if owed.ended is None and self._source is not None and waited < BIND_GRACE:
                return _Step.BUSY  # its record is being created: it carries the reference
            owed.unrecorded = True
            if self._source is not None:
                _logger.warning(
                    "dashboard: a session is declared without its record: its measurements "
                    "are sent only if the record appears"
                )
                self._said(DECLARED_WITHOUT_RECORD)
        declaration = owed.declaration
        body: dict[str, JsonValue] = {
            "localRef": owed.local_ref,
            "kind": declaration.kind,
            "startedAt": owed.start_ms,
            "operatorName": declaration.operator,
        }
        age = self._age_ms(now, owed)
        if age is not None:
            body["sessionAgeMs"] = age
        optional: Mapping[str, JsonValue] = {
            "profileId": declaration.profile_id,
            "profileName": declaration.profile_name,
            "zoneLowBpm": declaration.zone_low_bpm,
            "zoneHighBpm": declaration.zone_high_bpm,
            "totalDurationS": declaration.total_duration_s,
            "subjectHrMax": declaration.subject_hr_max,
            "occupancy": declaration.occupancy,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        answer = await self._post(now, owed, LOCAL_PATH, body)
        if isinstance(answer, _Wait):
            return _halted(answer)
        if isinstance(answer, Refusal):
            # It cannot be declared, so nothing of it can be sent: it is closed here.
            owed.cursor = replace(owed.cursor, state="complete", refused=owed.cursor.refused + 1)
            return _Step.DONE
        found = answer.document.get("sessionId")
        if not isinstance(found, str):
            # An answer, so the link works: this session waits and is declared
            # again later. The running session, the one that has just ended and
            # a refused launch do not wait for it.
            _logger.warning("dashboard: registration answered without a session id")
            owed.next_try = Monotonic(now + RETRY_PERIOD)
            return _Step.BUSY
        owed.cursor = replace(owed.cursor, session_id=found)
        return found

    async def _confirm(self, now: Monotonic, owed: _Owed, session_id: str) -> _Step | None:
        """Confirm the start of a launch from the dashboard. ``None`` once it is settled.

        Anything but a yes or a silence has the running session stopped: the
        machine is armed for a session the dashboard does not hold started
        (cancelled there between the poll and the arm, or not taken for a
        reason of the link's own), and whoever launched it could not stop it.
        A refusal that is about the link leaves the confirmation owed: it is
        made again, and the record sent, once the dashboard takes it.

        "The running session" is read AFTER the answer: one that arrives when
        its session has ended is about that session alone, and stops no other.
        """
        body: dict[str, JsonValue] = {"sessionId": session_id, "startedAt": owed.start_ms}
        age = self._age_ms(now, owed)
        if age is not None:
            body["sessionAgeMs"] = age
        answer = await self._sender.send(START_PATH, body)
        match answer:
            case Acked():
                owed.cursor = replace(owed.cursor, start_confirmed=True)
                return None
            case Held():
                self._held_until = Monotonic(now + RETRY_PERIOD)
                if answer.refused:
                    self._not_taken(owed, answer.detail)
                return _Step.HELD
            case RouteMissing():
                self._held_until = Monotonic(now + RETRY_PERIOD)
                self._not_taken(owed, START_UNKNOWN)
                return _Step.HELD
            case Refusal():
                # Cancelled on the dashboard between the poll and the arm.
                owed.cursor = replace(owed.cursor, start_confirmed=True)
                self._not_taken(owed, answer.detail)
                return None
        raise assert_never(answer)

    def _not_taken(self, owed: _Owed, detail: str) -> None:
        """The dashboard did not take the start of ``owed``: stop it if it still runs, once."""
        if self._live is owed and not owed.stopped:
            owed.stopped = True
            self._start_refused(detail)

    async def _end_from_memory(self, now: Monotonic, owed: _Owed, session_id: str) -> _Step:
        """Send the end the runtime gave, for a session that has no record to say it."""
        ended = owed.ended
        if ended is None:
            return _Step.IDLE  # a session running without a record: nothing to send yet
        end = _End(ended.end.failed, ended.end.reason, ended.at_ms)
        waiting = await self._send_end(now, owed, session_id, end)
        if waiting is not None:
            return waiting
        owed.cursor = replace(owed.cursor, state="complete")
        return _Step.DONE

    async def _transfer(
        self, now: Monotonic, owed: _Owed, record: Path, session_id: str, *, live: bool
    ) -> _Step:
        """Send what the record holds after the cursor: telemetry, events, then the end.

        Never two batches of telemetry less than :data:`CATCH_UP_PERIOD` apart,
        whichever sessions they are of. A session that is behind is not even
        read before its turn. One that is up to date is read: its end, which
        carries no telemetry, does not wait; a few points left do.
        """
        if live and owed.caught_up and not _due(owed.last_transfer, now, TELEMETRY_PERIOD):
            return _Step.IDLE
        too_soon = not _due(self._last_batch, now, CATCH_UP_PERIOD)
        if too_soon and not owed.caught_up:
            return _Step.BUSY
        over = owed.ended is not None or not live
        read = await self._disk(
            functools.partial(
                read_batch,
                record,
                start_ms=owed.start_ms,
                cursor=owed.cursor,
                with_last_tick=over and owed.cursor.end == "pending",
            ),
            READ_TIMEOUT,
        )
        if isinstance(read, Err):
            return await self._unreadable(now, owed, session_id, read.error, live=live)
        batch = read.value
        if too_soon and batch.points.points:
            return _Step.BUSY
        owed.last_transfer = now
        return await self._send(now, owed, record, session_id, batch, live=live)

    async def _send(
        self,
        now: Monotonic,
        owed: _Owed,
        record: Path,
        session_id: str,
        batch: Batch,
        *,
        live: bool,
    ) -> _Step:
        """Send one reading of a record: its points, its events, and its end if it is over."""
        waiting = await self._send_points(now, owed, session_id, batch.points)
        if waiting is None:
            waiting = await self._send_events(now, owed, session_id, batch.events)
        if waiting is not None:
            return waiting
        held = owed.events_retry is not None
        owed.caught_up = not batch.points.more and (held or not batch.events.more)
        if not owed.caught_up:
            return _Step.BUSY
        return await self._finish(now, owed, record, session_id, batch, live=live)

    async def _send_points(
        self, now: Monotonic, owed: _Owed, session_id: str, points: Points
    ) -> _Step | None:
        """Send one batch of telemetry and move the cursor past it. ``None``: acknowledged."""
        cursor = replace(owed.cursor, ticks_offset=points.offset, ticks_mid_line=points.mid_line)
        if points.points:
            self._last_batch = now
            answer = await self._post(
                now, owed, TELEMETRY_PATH, {"sessionId": session_id, "points": points.points}
            )
            if isinstance(answer, _Wait):
                return _halted(answer)
            cursor = replace(
                cursor,
                last_t=points.last_t,
                rejected_points=cursor.rejected_points + _rejected(answer, "points", session_id),
                refused=cursor.refused + _refused(answer),
            )
        owed.cursor = cursor
        _passed_over(points.skipped, "lines of ticks", session_id)
        return None

    async def _send_events(
        self, now: Monotonic, owed: _Owed, session_id: str, events: Events
    ) -> _Step | None:
        """Send one batch of events and move the cursor past it. ``None``: nothing stops the pass.

        A dashboard that does not know the route leaves ``events_retry`` set:
        the events stay owed, and it is asked again when that time has come.
        """
        if owed.events_retry is not None and now < owed.events_retry:
            return None
        cursor = replace(
            owed.cursor,
            events_offset=events.offset,
            events_mid_line=events.mid_line,
            last_seq=events.last_seq,
        )
        if not events.events:
            owed.events_retry = None
            owed.cursor = cursor
            _passed_over(events.skipped, "lines of events", session_id)
            return None
        answer = await self._post(
            now,
            owed,
            EVENTS_PATH,
            {"sessionId": session_id, "events": events.events},
            optional=True,
        )
        if answer is _Wait.MISSING:
            if owed.events_retry is None:
                _logger.warning(
                    "dashboard: it does not take events (older than contract 1.1); they stay owed"
                )
            owed.events_retry = Monotonic(now + EVENTS_RETRY_PERIOD)
            return None
        if isinstance(answer, _Wait):
            return _halted(answer)
        owed.events_retry = None
        owed.cursor = replace(
            cursor,
            rejected_events=cursor.rejected_events + _rejected(answer, "events", session_id),
            refused=cursor.refused + _refused(answer),
        )
        _passed_over(events.skipped, "lines of events", session_id)
        return None

    async def _send_end(
        self, now: Monotonic, owed: _Owed, session_id: str, end: _End
    ) -> _Step | None:
        """Send the end of the session, once. ``None``: the dashboard has it."""
        if owed.cursor.end == "sent":
            return None
        body: dict[str, JsonValue] = {
            "sessionId": session_id,
            "failed": end.failed,
            "reason": end.reason,
        }
        if end.at_ms is not None:
            body["endedAt"] = end.at_ms
        answer = await self._post(now, owed, END_PATH, body)
        if isinstance(answer, _Wait):
            return _halted(answer)
        owed.cursor = replace(
            owed.cursor, end="sent", refused=owed.cursor.refused + _refused(answer)
        )
        return None

    async def _finish(
        self,
        now: Monotonic,
        owed: _Owed,
        record: Path,
        session_id: str,
        batch: Batch,
        *,
        live: bool,
    ) -> _Step:
        """Everything on disk is acknowledged: send the end if the session is over."""
        end = self._end_of(now, owed, record, batch, live=live)
        if end is None:
            return _Step.IDLE
        waiting = await self._send_end(now, owed, session_id, end)
        if waiting is not None:
            return waiting
        if owed.events_retry is not None:
            # Everything but the events, which this dashboard does not take.
            return _Step.IDLE if live else _Step.DROPPED
        owed.cursor = replace(owed.cursor, state="complete")
        return _Step.DONE

    def _end_of(
        self, now: Monotonic, owed: _Owed, record: Path, batch: Batch, *, live: bool
    ) -> _End | None:
        """How the session ended, once that is known; ``None`` while it is not."""
        closed = batch.closed
        last_tick = _last_instant(owed, batch)
        if closed is not None:
            reason = closed.reason
            words = reason if closed.stop_reason is None else f"{reason}: {closed.stop_reason}"
            at_ms = last_tick if closed.at_ms is None else closed.at_ms
            return _End(reason not in SUCCESSFUL_ENDS, words, at_ms)
        ended = owed.ended
        if ended is not None:
            # The runtime ended it in this run and the record is not closed yet.
            if elapsed(ended.since, now) < CLOSE_GRACE:
                return None
            return _End(ended.end.failed, ended.end.reason, ended.at_ms)
        source = self._source
        if live or (source is not None and source.current() == record):
            return None  # still being written
        # A record of an earlier run that nobody closed: the console stopped first.
        return _End(True, INTERRUPTED, last_tick)

    async def _unreadable(
        self, now: Monotonic, owed: _Owed, session_id: str, error: RecordError, *, live: bool
    ) -> _Step:
        """The record could not be read this time."""
        if error.detail == MISALIGNED:
            _logger.warning(
                "dashboard: the cursor of session %s does not match its record: "
                "sent again from its start",
                session_id,
            )
            owed.cursor = replace(
                owed.cursor,
                ticks_offset=0,
                ticks_mid_line=False,
                last_t=None,
                events_offset=0,
                events_mid_line=False,
                last_seq=-1,
            )
            return _Step.BUSY
        ended = owed.ended
        if ended is not None and elapsed(ended.since, now) >= CLOSE_GRACE:
            # The disk does not give the record back: the dashboard is at least
            # told that the session is over, and how.
            return await self._end_from_memory(now, owed, session_id)
        if live or ended is not None or error.detail in (BUSY, TIMEOUT):
            return _Step.IDLE
        _logger.warning(
            "dashboard: the record of session %s cannot be read (%s): left until the next start",
            session_id,
            error.detail,
        )
        return _Step.DROPPED

    # --- the two things every pass does -------------------------------------

    async def _post(
        self,
        now: Monotonic,
        owed: _Owed,
        path: str,
        body: Mapping[str, JsonValue],
        *,
        optional: bool = False,
    ) -> Acked | Refusal | _Wait:
        """Make one request. A :class:`Refusal` comes back only once it is final."""
        answer = await self._sender.send(path, body)
        match answer:
            case Acked():
                owed.refusals = 0
                owed.next_try = None
                return answer
            case Held():
                self._held_until = Monotonic(now + RETRY_PERIOD)
                return _Wait.HOLD
            case RouteMissing():
                if optional:
                    return _Wait.MISSING
                self._held_until = Monotonic(now + RETRY_PERIOD)
                return _Wait.HOLD
            case Refusal():
                if not answer.final and owed.refusals + 1 < MAX_REFUSALS:
                    owed.refusals += 1
                    owed.next_try = Monotonic(now + RETRY_PERIOD)
                    return _Wait.RETRY
                owed.refusals = 0
                owed.next_try = None
                _logger.warning(
                    "dashboard: %s refused for good (%s): passed over", path, answer.detail
                )
                return answer
        raise assert_never(answer)

    async def _disk[T](
        self, work: Callable[[], Result[T, RecordError]], limit: Seconds
    ) -> Result[T, RecordError]:
        """Run one read or write on a record I/O thread, and wait for it at most ``limit``."""
        ran = await self._io.run(work, limit)
        return ran.value if isinstance(ran, Ok) else Err(ran.error)
