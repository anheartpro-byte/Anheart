"""Fan-out from one control loop to however many browsers are watching.

The hub sits between the 5 Hz session loop and N connected clients, and its
whole job is to make sure the arithmetic of that N never reaches the loop.

The failure this module exists to prevent
-----------------------------------------
A tablet on the clinic wifi goes to sleep mid-session. Its TCP socket does not
close; it stops reading. Every write the server makes to it now sits in a
buffer, and once that buffer is full the write *blocks*. If the control loop is
the thing doing that write - directly, or through an unbounded queue that grows
until the process is out of memory - then a sleeping tablet has just added
latency to, or stopped, the loop that is regulating the speed of a centrifuge
with a person inside it.

So: every client gets a **bounded** amount of the hub's memory, publishing is
synchronous and never waits, and a client that cannot keep up is dropped rather
than served. Dropping a viewer is a visible, recoverable failure. Delaying the
control loop is neither.

Snapshots coalesce; events do not
---------------------------------
A :class:`~src.training.types.TelemetrySnapshot` is *complete state*: if a
client misses one, the next one tells it everything, so the queue for snapshots
is one slot deep and the newest always wins. That is coalescing, not dropping -
no information is lost, only redundant copies of it.

A :class:`~src.control_surface.SessionEvent` is a *fact about a moment*: an
emergency stop that was pressed and then acknowledged appears in no later
snapshot. Events are therefore never coalesced and never silently discarded. If
a client's event queue fills, the client is **evicted** - its socket is closed
with a resync notice, the page shows its "connection lost" banner and reloads.
The event is still in every other client's queue and in the log; the one thing
that never happens is an operator's screen quietly omitting a stop.

Pacing
------
Each client is released at most every :data:`DEFAULT_MIN_INTERVAL`. That bound
sits *below* one control period on purpose (0.15 s against 0.2 s), so a loop
running at its normal 5 Hz is never throttled - only a producer faster than
about 6.7 Hz is coalesced. Setting the gate at exactly one period would make
ordinary float jitter drop every other tick and halve the visible rate.

The ECG ring
------------
Also here, because it has the same shape of problem: a live trace needs the
last few seconds of samples, a session produces a quarter of a million of them,
and the trace must cost a bounded amount of memory. A ring buffer with a
monotonically increasing sample counter lets a client ask for "everything after
what I already have" and lets it *detect* that it fell behind rather than
drawing a discontinuity as if it were a heartbeat.

No clock is read here. The hub holds an injected ``Clock`` (contract rule 4),
which is what makes the pacing testable without waiting.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, final

from src.clock import Clock
from src.control_surface import SessionEvent
from src.training.types import TelemetrySnapshot
from src.units import Millivolts, Monotonic, Seconds

_logger: logging.Logger = logging.getLogger(__name__)


CLIENT_QUEUE_LIMIT: Final[int] = 8
"""How many messages one client may have waiting. The bound that protects the loop.

Eight, not eighty: at 5 Hz a client that is eight messages behind is more than
a second behind, and a second-old dashboard shown as live is the failure this
whole project is organised around. Snapshots do not consume slots (they
coalesce into one), so in practice this bounds the *event* backlog, and eight
un-read events means the socket is not being read at all.
"""

DEFAULT_MIN_INTERVAL: Final[Seconds] = Seconds(0.15)
"""Shortest interval between releases to one client: about 6.7 Hz.

Below the 0.2 s control period on purpose - see the module docstring.
"""

ECG_TRACE_SECONDS: Final[Seconds] = Seconds(6.0)
"""How much ECG the ring keeps. Six seconds shows four to eight beats.

Long enough for an operator to see rhythm and morphology rather than a wiggle,
short enough that the buffer stays small: at the 250 Hz output rate this is
1500 floats, which is nothing, and it is a fixed nothing for a 45-minute
session.
"""

DEFAULT_ECG_RATE_HZ: Final[int] = 250
"""The treated output rate (``OUTPUT_SAMPLE_RATE``), for sizing the ring."""


# =========================================================================
# What a client is handed
# =========================================================================


@unique
class PayloadKind(Enum):
    """What one release to a client contains.

    String values: they go over the wire and the browser switches on them.
    """

    SNAPSHOT = "snapshot"
    # Complete current state. Coalesced: this is always the newest one.

    EVENT = "event"
    # One thing that happened. Never coalesced, never silently dropped.

    RESYNC = "resync"
    # "You fell behind and were evicted." The last thing a client is told
    # before its socket closes, so the page can say so instead of continuing to
    # display state that stopped updating.


@dataclass(frozen=True, slots=True)
class Payload:
    """One release to one client: exactly one of a snapshot, an event, a notice.

    A tagged record rather than three queues, so the order things happened in
    survives the trip: an event raised between two snapshots is delivered
    between them, and a screen never shows an acknowledgement before the stop
    it acknowledged.
    """

    kind: PayloadKind
    snapshot: TelemetrySnapshot | None
    event: SessionEvent | None
    notice: str


@dataclass(frozen=True, slots=True)
class ClientStats:
    """What one client's connection cost, for the status page and the log.

    ``coalesced`` is the interesting one: a client with a large count is being
    served a slower view than the loop is producing, which is a fact an
    operator debugging "the screen feels laggy" needs, and which is invisible
    otherwise because coalescing is by design silent.
    """

    released: int
    coalesced: int
    events: int
    evicted: bool


# =========================================================================
# One connected browser
# =========================================================================


@final
class TelemetryClient:
    """One subscriber's bounded, coalescing inbox.

    Mutable state shared between the publishing side (the control loop, in
    synchronous calls) and the consuming side (one WebSocket task). Safe
    without a lock for the same reason as ``src.control_surface``: both run on
    the same event loop, every publishing method is synchronous and non-awaiting,
    and the only ``await`` here is the consumer waiting to be woken.

    Created by :meth:`TelemetryHub.subscribe`; never constructed directly, so
    that the hub always knows about every client it can block on.
    """

    __slots__ = (
        "_clock",
        "_coalesced",
        "_events",
        "_eviction",
        "_min_interval",
        "_released",
        "_released_at",
        "_seen_events",
        "_snapshot",
        "_wakeup",
    )

    def __init__(self, *, clock: Clock, min_interval: Seconds = DEFAULT_MIN_INTERVAL) -> None:
        self._clock: Clock = clock
        self._min_interval: Seconds = min_interval
        self._snapshot: TelemetrySnapshot | None = None
        self._events: deque[SessionEvent] = deque()
        self._wakeup: asyncio.Event = asyncio.Event()
        self._eviction: str | None = None
        self._released: int = 0
        self._coalesced: int = 0
        self._seen_events: int = 0
        self._released_at: Monotonic | None = None

    # --- the publishing side (synchronous, never blocks) -----------------

    def offer_snapshot(self, snapshot: TelemetrySnapshot, now: Monotonic) -> None:
        """Put the newest snapshot in the one slot, replacing whatever was there.

        Never blocks and never grows. Replacing an unread snapshot is counted
        as ``coalesced`` rather than as a drop, because the value that replaced
        it is strictly better information about the same thing.

        The consumer is woken only when the pacing interval has elapsed. It is
        not left asleep indefinitely by that: the loop publishes every tick, so
        the wake arrives at most one tick after the interval does.
        """
        if self._eviction is not None:
            return
        if self._snapshot is not None:
            self._coalesced += 1
        self._snapshot = snapshot
        if self._due(now):
            self._wakeup.set()

    def offer_event(self, event: SessionEvent) -> None:
        """Queue one event. Never coalesced, never dropped, always wakes the consumer.

        When the queue is already at :data:`CLIENT_QUEUE_LIMIT` the client is
        evicted instead: the event stays in the queue and the *client* is what
        goes, because a browser that has not read eight events is not reading,
        and quietly discarding the ninth would leave an operator looking at a
        screen with a stop missing from it.
        """
        if self._eviction is not None:
            return
        self._events.append(event)
        self._seen_events += 1
        if len(self._events) > CLIENT_QUEUE_LIMIT:
            self.evict(
                f"{len(self._events)} events unread, limit {CLIENT_QUEUE_LIMIT}: "
                "this screen fell behind and its data is not live"
            )
        self._wakeup.set()

    def evict(self, reason: str) -> None:
        """Mark the client for disconnection, with a reason it can display.

        The reason is kept in its own slot rather than pushed into the event
        queue: an eviction is not a session event, and synthesising one would
        put a record in the operator's event log for something that happened to
        a socket.
        """
        if self._eviction is not None:
            return
        self._eviction = reason
        _logger.warning("telemetry client evicted: %s", reason)
        self._wakeup.set()

    # --- the consuming side (one task, awaits) ---------------------------

    async def next_payload(self) -> Payload:
        """Wait for something to send, then return exactly one payload.

        Events first, oldest first, so ordering is preserved; then the
        coalesced snapshot. Eviction wins over both, because a client that is
        being disconnected should be told that rather than handed more state.

        Snapshots are held back until the pacing interval has elapsed - the gate
        is on the RELEASE and not only on the wake-up, because a consumer that
        has just finished a write comes straight back and would otherwise be
        served at the producer's rate rather than at its own. Events are never
        held back.

        A consumer can therefore sit waiting with an undue snapshot in its slot
        until the next offer wakes it. That is the honest behaviour: if the
        producer has stopped there is nothing newer to show, and the page's own
        watchdog is what says the screen is no longer live.
        """
        while True:
            ready = self._take()
            if ready is not None:
                return ready
            self._wakeup.clear()
            await self._wakeup.wait()

    def _take(self) -> Payload | None:
        """Pull one payload out, or ``None`` if there is nothing to send yet."""
        eviction = self._eviction
        if eviction is not None:
            self._events.clear()
            self._snapshot = None
            return Payload(kind=PayloadKind.RESYNC, snapshot=None, event=None, notice=eviction)
        if self._events:
            event = self._events.popleft()
            self._released += 1
            return Payload(kind=PayloadKind.EVENT, snapshot=None, event=event, notice="")
        snapshot = self._snapshot
        if snapshot is not None and self._due(self._clock.monotonic()):
            self._snapshot = None
            self._released += 1
            # Stamped from the CLOCK, not from ``snapshot.at``. The two are the
            # same instant in production and diverge exactly when it matters: a
            # snapshot arriving from a slow or replaying producer carries an old
            # stamp, and pacing measured from it would either throttle a healthy
            # client forever or stop throttling a struggling one at all.
            self._released_at = self._clock.monotonic()
            return Payload(kind=PayloadKind.SNAPSHOT, snapshot=snapshot, event=None, notice="")
        return None

    # --- reading ---------------------------------------------------------

    @property
    def evicted(self) -> bool:
        """Whether this client has been dropped and must close its socket."""
        return self._eviction is not None

    @property
    def stats(self) -> ClientStats:
        """What this connection has cost so far."""
        return ClientStats(
            released=self._released,
            coalesced=self._coalesced,
            events=self._seen_events,
            evicted=self.evicted,
        )

    def _due(self, now: Monotonic) -> bool:
        """Whether enough has passed since the last release to wake the consumer."""
        released_at = self._released_at
        if released_at is None:
            return True
        return now - released_at >= self._min_interval


# =========================================================================
# The ECG ring
# =========================================================================


@dataclass(frozen=True, slots=True)
class EcgWindow:
    """A slice of the recent ECG, with enough context to draw it honestly.

    ``seq`` is the index of the LAST sample in ``samples``, counted from the
    start of the ring's life. A client that asks for everything after the
    ``seq`` it already holds gets exactly the new samples, and can tell from
    the arithmetic whether it fell so far behind that the ring wrapped -
    ``gap`` says so. That matters: joining two ends of a discontinuity draws a
    vertical line that looks like a QRS complex.
    """

    seq: int
    fs_hz: int
    samples: tuple[Millivolts, ...]
    gap: bool


@final
class EcgRing:
    """Fixed-size ring of recent ECG samples in millivolts.

    Millivolts, not ADC counts: the treatment pipeline on this machine converts
    to physical units before anything leaves it, and a trace drawn from raw
    counts would silently change scale if the gain ever did.
    """

    __slots__ = ("_fs_hz", "_samples", "_written")

    def __init__(
        self,
        *,
        fs_hz: int = DEFAULT_ECG_RATE_HZ,
        span: Seconds = ECG_TRACE_SECONDS,
    ) -> None:
        if fs_hz <= 0:
            raise ValueError(f"ECG rate must be positive, got {fs_hz}")
        if span <= 0.0:
            raise ValueError(f"ECG span must be positive, got {span}")
        self._fs_hz: int = fs_hz
        self._samples: deque[Millivolts] = deque(maxlen=max(1, int(fs_hz * span)))
        self._written: int = 0

    @property
    def capacity(self) -> int:
        """How many samples the ring holds before it starts overwriting."""
        return self._samples.maxlen or 0

    @property
    def written(self) -> int:
        """Total samples ever appended. The sequence number a client tracks."""
        return self._written

    def extend(self, values: Sequence[Millivolts]) -> int:
        """Append a treated batch. Returns the new sequence number.

        ``deque`` with a ``maxlen`` rather than a list plus slicing: appending
        is O(1) and the oldest sample falls off by itself, so there is no
        periodic compaction to get wrong and no moment where the buffer is
        twice its nominal size.
        """
        self._samples.extend(values)
        self._written += len(values)
        return self._written

    def window(self, *, after: int | None = None, limit: int | None = None) -> EcgWindow:
        """The samples following ``after``, newest last.

        ``after=None`` means "everything you have", which is what a freshly
        connected page needs. A client whose ``after`` is older than the ring
        gets what the ring still holds plus ``gap=True``, never a silent splice.

        ``limit=0`` means "how far has the ring got?" - the sequence number and
        the rate, with no samples. Handled explicitly, because a naive
        ``wanted[-limit:]`` with a limit of zero is ``wanted[0:]``, which returns
        the WHOLE ring: the one value that asks for nothing would have returned
        everything.
        """
        held = tuple(self._samples)
        available = len(held)
        first_held = self._written - available
        if after is None:
            wanted = held
            gap = False
        elif after >= self._written:
            wanted = ()
            gap = False
        else:
            gap = after < first_held
            start = 0 if gap else after - first_held
            wanted = held[start:]
        if limit is not None and limit <= 0:
            return EcgWindow(seq=self._written, fs_hz=self._fs_hz, samples=(), gap=gap)
        if limit is not None and len(wanted) > limit:
            wanted = wanted[-limit:]
            gap = True
        return EcgWindow(seq=self._written, fs_hz=self._fs_hz, samples=wanted, gap=gap)


# =========================================================================
# The hub
# =========================================================================


@final
class TelemetryHub:
    """Publishes to every connected client, synchronously and without waiting.

    Implements ``src.control_surface.TelemetrySink``, which is how the control
    surface reaches it without depending on the transport.

    Mutable shared state, deliberately, and the same single-event-loop argument
    applies: every publishing method below is synchronous and contains no
    ``await``, so a publish runs to completion before any WebSocket task can
    observe a half-updated client set.
    """

    __slots__ = ("_clients", "_clock", "_ecg", "_events", "_evictions", "_latest", "_min_interval")

    def __init__(
        self,
        *,
        clock: Clock,
        min_interval: Seconds = DEFAULT_MIN_INTERVAL,
        ecg_rate_hz: int = DEFAULT_ECG_RATE_HZ,
        ecg_span: Seconds = ECG_TRACE_SECONDS,
    ) -> None:
        self._clock: Clock = clock
        self._min_interval: Seconds = min_interval
        self._clients: list[TelemetryClient] = []
        self._ecg: EcgRing = EcgRing(fs_hz=ecg_rate_hz, span=ecg_span)
        self._latest: TelemetrySnapshot | None = None
        self._events: int = 0
        self._evictions: int = 0

    # --- subscription ----------------------------------------------------

    def subscribe(self) -> TelemetryClient:
        """Register a client and hand back its inbox, primed with current state.

        Primed deliberately: a page that connects mid-session must not show
        empty fields until the next tick. If there is no snapshot yet, nothing
        is primed and the page shows "no data", which is the truth.
        """
        client = TelemetryClient(clock=self._clock, min_interval=self._min_interval)
        self._clients.append(client)
        snapshot = self._latest
        if snapshot is not None:
            client.offer_snapshot(snapshot, self._clock.monotonic())
        return client

    def release(self, client: TelemetryClient) -> None:
        """Deregister a client. Idempotent: a socket can fail on either side."""
        if client in self._clients:
            self._clients.remove(client)

    @property
    def client_count(self) -> int:
        """How many clients are currently subscribed."""
        return len(self._clients)

    @property
    def evictions(self) -> int:
        """How many clients have been dropped for falling behind, this process."""
        return self._evictions

    # --- publishing (the sink protocol) ----------------------------------

    def publish_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        """Offer a snapshot to every client. Coalescing, bounded, non-blocking."""
        self._latest = snapshot
        now = self._clock.monotonic()
        for client in self._clients:
            client.offer_snapshot(snapshot, now)
        self._reap()

    def publish_event(self, event: SessionEvent) -> None:
        """Offer an event to every client. Never coalesced, never dropped."""
        self._events += 1
        for client in self._clients:
            client.offer_event(event)
        self._reap()

    # --- the ECG trace ---------------------------------------------------

    def record_ecg(self, values: Sequence[Millivolts]) -> int:
        """Append treated ECG samples for the live trace. Returns the new sequence.

        Not pushed to clients. The trace is *pulled* over the same socket by
        sequence number, because a 250 Hz stream pushed to a sleeping tablet is
        precisely the backpressure this module exists to prevent, and because a
        client that missed a second of samples wants the newest second rather
        than a queue of the ones it missed.
        """
        return self._ecg.extend(values)

    def ecg_window(self, *, after: int | None = None, limit: int | None = None) -> EcgWindow:
        """Recent ECG for the trace. See :meth:`EcgRing.window`."""
        return self._ecg.window(after=after, limit=limit)

    # --- internals -------------------------------------------------------

    def _reap(self) -> None:
        """Forget clients that evicted themselves.

        Their WebSocket task still has to wake up, send the resync notice and
        close, so the object outlives this call - but the hub stops publishing
        to it immediately, which is the part that has to be bounded.
        """
        still_here = [client for client in self._clients if not client.evicted]
        dropped = len(self._clients) - len(still_here)
        if dropped:
            self._evictions += dropped
            self._clients = still_here
