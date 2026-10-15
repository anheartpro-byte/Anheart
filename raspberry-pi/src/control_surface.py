"""The seam between the operator's browser and the session loop.

Two jobs, and they are deliberately the *only* two:

1. a **single-slot command mailbox** - the web layer drops one intent in, the
   session loop picks it up on its next tick and acts on it;
2. the **latest telemetry snapshot** - the loop drops one in, the web layer
   reads it whenever a request or a socket asks for it.

Nothing here talks to the drive, the BITalino or the network, and nothing here
awaits. That is what makes the operator's stop independent of whatever the
control loop happens to be doing: a request handler can latch an emergency
stop, get an answer, and return, without the loop having ticked once.

Why a mailbox rather than direct calls
--------------------------------------
A request handler that drove the drive itself would be a second writer on the
Modbus link, racing the control loop mid-sequence: a CiA402 start is three
writes in order and an interleaved write from a browser can leave the drive in
Switched On with a live speed reference. So the web layer states an intention
and the one thread that owns the wire carries it out.

The mailbox holds **one** command. A queue would let an operator's impatient
double-click stack up a start behind a stop; a single slot makes the second
click an explicit refusal on screen, which is the answer that tells them
something.

One request, one fate, and the surface says it
----------------------------------------------
Several screens can ask at once, and the loop awaits the drive inside its
tick: two requests can meet in one tick. So the slot is never taken over in
silence, and every request the surface accepts is given an identifier
(:meth:`ControlSurface.submit_start` and the others) that its answer and every
event about it carry. What a request meets:

===========  ==================================================================
arrives      what the surface does
===========  ==================================================================
a start      accepted only at IDLE with the slot empty. While another start
             waits or is being armed (STARTING) it is refused at once; so is
             one that finds a reset, a target or a stop waiting. Never replaces.
a target     accepted only while RUNNING with the slot empty. Never replaces.
a reset      accepted with the slot empty and no start in flight. Never replaces.
a stop       accepted whenever a session is starting or running, **whatever
             waits in the slot**: it takes the slot, and the request it
             displaces is told so (:attr:`EventKind.CANCELLED`). Refused only
             when there is nothing to stop, or a stop is already under way.
an e-stop    never refused and never in the slot. What waits there stays, and
             the loop judges it under the latched stop (a start is refused).
===========  ==================================================================

Each accepted request then ends exactly one way, published as an event that
carries its identifier: the loop took it (:attr:`EventKind.TAKEN`), the loop
refused it (:attr:`EventKind.REFUSED`), or a stop displaced it before the loop
got to it (:attr:`EventKind.CANCELLED`). The last few are kept for
``GET /api/status`` (:attr:`ControlSurface.fates`), for a screen whose socket
was down when the event went out.

The identifier is display data. Nothing in the loop reads it to decide
anything: it is generated here, opaque, and never derived from who asked.

The emergency stop is the deliberate exception - see :meth:`ControlSurface.submit_estop`.
It does not go in the mailbox, because a stop that waits for the loop to reach
its next tick is not a stop.

Why there are no locks
----------------------
Every method here is synchronous and contains no ``await``. The FastAPI app is
mounted as a task on the **same event loop** as the session loop (see
``src/web/app.py``), so both sides run on one thread and a call to any method
below runs to completion before the other side can observe anything: a
coroutine can only be interrupted at an ``await``, and there is none. Mutation
is therefore atomic with respect to the other side by construction, and a lock
would add a way for a request handler to block the control loop without adding
any safety.

Two consequences of that, both load-bearing:

* if this module ever grows an ``await``, the reasoning above is void and the
  invariants have to be re-established. ``tests/test_web_api.py`` parses this
  file and fails on ``async def`` or ``await``.
* the web layer must never be run in a thread or a second process. It is not a
  deployment preference; it is the premise of the whole arrangement.

Composite state is published as ONE attribute write wherever a reader could
otherwise see a half-built value: the frozen records below exist so that
"latched, by this operator, for this reason, at this instant" arrives all at
once or not at all.
"""

from __future__ import annotations

import logging
import secrets
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol, assert_never, final, runtime_checkable

from src.clock import Clock
from src.result import Err, Ok, Result
from src.training.safety import (
    AcknowledgeRefusal,
    AttestationRefusal,
    EstopAttestation,
    EstopUnattested,
    SafetyAcknowledgement,
    SafetySupervisor,
)
from src.training.types import Occupancy, SafetyVerdict, TelemetrySnapshot
from src.units import Bpm, Monotonic, OutputRpm, Seconds, UnixMillis

_logger: logging.Logger = logging.getLogger(__name__)

LOCAL_SUBJECT: Final[str] = "local"
"""The subject id of a start typed at the console, where no rider is named."""

FATES_KEPT: Final[int] = 16
"""How many fates :attr:`ControlSurface.fates` keeps: what a screen can still ask about.

A screen asks about its own last request after its socket dropped. Sixteen is
several seconds of every button of several screens pressed in turn, and a
fixed, small cost in every status answer.
"""

CANCELLED_BY_STOP: Final[str] = (
    "un STOP est arrive avant que la machine prenne cette demande, rien n'a ete fait"
)
"""What a request displaced by a stop is told. One sentence, the same for every request:
it names neither the request nor who stopped, and so can be kept in a record."""


# =========================================================================
# What the surface is allowed to lean on
# =========================================================================


class Acknowledger(Protocol):
    """Whatever clears the latched verdicts, by name.

    The supervisor by default. The local console hands in the runtime, whose
    :meth:`~src.training.runtime.TrainingRuntime.acknowledge` clears the
    supervisor AND the runtime's own latches (a drive found already running, a
    tick that raised) - with the supervisor alone, those would stand forever
    behind a screen that says "acknowledged".
    """

    @abstractmethod
    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Clear the latches, or refuse and say why. Synchronous, no I/O."""


@runtime_checkable
class TelemetrySink(Protocol):
    """Where snapshots and events go for fan-out to connected browsers.

    ``src.telemetry.TelemetryHub`` implements it. A protocol rather than the
    concrete hub so that this module does not depend on the transport, and so a
    test can assert exactly what was published without a socket.

    Both methods must be synchronous, non-blocking and total: they are called
    from the control loop and from request handlers, and a sink that raised
    would unwind a tick with the motor commanded.
    """

    @abstractmethod
    def publish_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        """Offer the newest snapshot. May coalesce; must never block."""

    @abstractmethod
    def publish_event(self, event: SessionEvent) -> None:
        """Offer a discrete event. Must not drop it, and must never block."""


# =========================================================================
# Events
# =========================================================================


@unique
class EventKind(Enum):
    """A discrete thing that happened, as opposed to a state that is true.

    String values because they are sent over the wire and written to the
    session log; ``auto()`` would renumber on a reorder and rewrite the meaning
    of every stored record.

    Events exist separately from snapshots because they are **not
    interchangeable**. A snapshot may be coalesced away - the next one carries
    the whole state, so nothing is lost. An event is a fact about a moment: an
    emergency stop that was pressed and then acknowledged leaves no trace in any
    later snapshot, and a screen that only ever showed the latest state would
    show a machine that looks like it was never stopped.
    """

    START_REQUESTED = "start_requested"
    FAULT_RESET_REQUESTED = "fault_reset_requested"
    END_REQUESTED = "end_requested"
    EMERGENCY_STOP = "emergency_stop"
    ACKNOWLEDGED = "acknowledged"
    ATTESTED = "attested"
    LAUNCH_CONFIRMED = "launch_confirmed"
    SESSION_RUNNING = "session_running"
    SESSION_IDLE = "session_idle"
    REFUSED = "refused"
    # The loop could not act on a request it took from the mailbox, or took a
    # manual target back by itself (that one answers nobody, and carries no
    # request identifier).

    TAKEN = "taken"
    # The loop took a request from the mailbox and acted on it: the session is
    # armed, the target is held, the reset is written, the stop is under way.
    # Said by the console, so that no screen has to deduce it from a frame.

    CANCELLED = "cancelled"
    # A stop took the place of a request that was still waiting in the
    # mailbox. The loop never saw that request.

    DASHBOARD = "dashboard"
    # What the dashboard link turned down on the machine's behalf. Never an
    # answer to something typed at this console, hence not ``refused``.

    RECORDING = "recording"
    # The session record (the local black box) changed health: degraded, or
    # back. Raised by the loop about itself, so it carries no operator.


@dataclass(frozen=True, slots=True)
class SessionEvent:
    """One operator-visible event, stamped with both clocks.

    Both clocks, because they answer different questions and neither can
    replace the other: ``at`` orders events against every other duration in
    the system (and keeps ordering across a wall-clock step on a Pi with no
    RTC), while ``wall_clock`` is what a person reads in a report afterwards.

    ``operator`` is never blank for an event that records a human decision -
    the safety supervisor refuses unattributed acknowledgements for the same
    reason. It is empty only for events the loop raises about itself.

    ``request_id`` is the identifier of the request the event speaks of: the
    one a ``*_requested`` event announces, the one a ``taken``, ``refused`` or
    ``cancelled`` event answers, and for ``session_running`` and
    ``session_idle`` the start the session came from. ``None`` when the event
    is about no request. Display data: a screen tells its own requests from
    another screen's by it, and nothing on the machine reads it back.
    """

    kind: EventKind
    at: Monotonic
    wall_clock: UnixMillis
    operator: str
    detail: str
    request_id: str | None = None


# =========================================================================
# Where the session is, as far as the surface knows
# =========================================================================


@unique
class RunState(Enum):
    """The surface's view of the session, which is a view of intentions.

    Derived from four plain flags rather than stored, so that no method has to
    write two fields consistently and no reader can catch it between them.

    This is **not** the drive's state and not the phase. It answers one
    question - "may I accept a start?" - and it is deliberately pessimistic:
    STARTING and STOPPING both refuse a start, because a machine that has been
    asked to stop and has not yet said it has is a machine that is still
    turning.
    """

    IDLE = "idle"
    # Nothing requested, nothing running. The only state a start is accepted in.

    STARTING = "starting"
    # A start was accepted: it waits in the mailbox, or the loop is arming it.
    # Held until the loop has answered for that start, and never dropped
    # because a tick ended at rest while it waited: a second start is refused.

    RUNNING = "running"
    # The loop has confirmed a session is live.

    STOPPING = "stopping"
    # An end or an emergency stop was accepted and the loop has not yet
    # reported itself idle. An e-stop holds this state until it is acknowledged.


# =========================================================================
# Commands
# =========================================================================


@dataclass(frozen=True, slots=True)
class StartSession:
    """Start a session on ``profile_id``, optionally over a different total.

    ``total_duration_s`` is the one override the plan supports; ``None`` means
    "as the profile says". Resolution happens in the loop, not here: turning a
    profile id into a :class:`~src.training.plan.Program` reads the profile
    store from disk, and this module does no I/O.
    """

    profile_id: str
    operator: str
    total_duration_s: Seconds | None
    at: Monotonic
    request_id: str
    """The identifier of this request. Display data, like every ``request_id`` below."""

    subject_id: str = LOCAL_SUBJECT
    """Who rides: the rider's dashboard identifier. :data:`LOCAL_SUBJECT` names nobody,
    which a programmed session refuses."""

    subject_hr_max: Bpm | None = None
    """For a launch from the dashboard: the rider's maximum heart rate it carries.

    A start at the console carries none: the loop reads it, and the age, from
    the riders' list (:mod:`src.roster`) when it arms."""

    subject_age: int | None = None
    """For a launch from the dashboard: the rider's age in years it carries.

    A programmed session refuses an unknown or too-young rider."""

    cloud_session_id: str | None = None
    """The dashboard session this start answers, or ``None`` for a local start."""

    confirmed_by: str | None = None


@dataclass(frozen=True, slots=True)
class EndSession:
    """End the running session in the ordinary, controlled way.

    Not an emergency stop. This is the operator saying "we are finished", and
    the loop is expected to take it through COOLDOWN on the commissioned ramp.
    An operator who needs the machine to stop *now* presses the other button,
    which does not come through the mailbox at all.
    """

    operator: str
    reason: str
    at: Monotonic
    request_id: str


@dataclass(frozen=True, slots=True)
class StartManual:
    """Start a manual session for ``occupancy``, target 0.

    The occupancy is declared here, before anything turns, and never changes
    during the rotation. The ceiling is resolved in the loop from the
    configuration for that occupancy, not carried here.
    """

    occupancy: Occupancy
    operator: str
    at: Monotonic
    request_id: str
    subject_id: str = LOCAL_SUBJECT
    """Who rides: a rider of the list with a person on board; :data:`LOCAL_SUBJECT`
    on the bench, where nobody does."""


@dataclass(frozen=True, slots=True)
class SetManualTarget:
    """Set the manual target, in OUTPUT rpm. The loop walks the setpoint to it."""

    output_rpm: OutputRpm
    operator: str
    at: Monotonic
    request_id: str


@dataclass(frozen=True, slots=True)
class FaultReset:
    """Reset a drive fault: an explicit operator action, never an automatic one."""

    operator: str
    at: Monotonic
    request_id: str


type Command = StartSession | EndSession | StartManual | SetManualTarget | FaultReset
"""What the loop can find in the mailbox. Closed; match it with the nested form."""


def is_start(command: Command | None) -> bool:
    """Whether ``command`` asks for a session to start, programmed or manual."""
    return isinstance(command, StartSession | StartManual)


def words_of(command: Command) -> str:
    """What ``command`` asks for, in the words its events carry. Never who asked."""
    match command:
        case StartSession():
            return f"start {command.profile_id}"
        case StartManual():
            return f"manuel {command.occupancy.value}"
        case SetManualTarget():
            return f"cible {command.output_rpm:.2f} tr/min de sortie"
        case FaultReset():
            return "reset defaut"
        case EndSession():
            return command.reason
    raise assert_never(command)


# =========================================================================
# What became of a request
# =========================================================================


@unique
class Fate(Enum):
    """The one way an accepted request ended. String values: they go over the wire."""

    TAKEN = "taken"
    # The loop took it from the mailbox and acted on it.

    REFUSED = "refused"
    # The loop took it from the mailbox and could not act on it.

    CANCELLED = "cancelled"
    # A stop took its place in the mailbox: the loop never saw it.


@dataclass(frozen=True, slots=True)
class RequestFate:
    """What became of one request, as its event said it.

    Kept for a screen that was not listening when the event went out. ``detail``
    is the event's own: the loop's reason for a refusal, what was asked for a
    request taken. It names no operator.
    """

    request_id: str
    fate: Fate
    detail: str
    at: Monotonic
    wall_clock: UnixMillis


# =========================================================================
# Why a command can be refused
# =========================================================================


@dataclass(frozen=True, slots=True)
class SurfaceBusy:
    """The surface is not in a state that accepts this command.

    Carries the pending command as well as the state, because "busy" on its own
    is the answer that makes an operator click again. Of the pending command a
    screen is told that one waits and its identifier, never its content: it may
    be another operator's (``src/web/routes.py``).

    ``in_the_way`` is the identifier of the request this one found in its way,
    when there is one: the request waiting in the mailbox, or the start the
    loop is arming.
    """

    state: RunState
    pending: Command | None
    in_the_way: str | None = None


@dataclass(frozen=True, slots=True)
class SafetyHolding:
    """A latched safety verdict stands, so no session may be started.

    Clearing it is an acknowledgement with a name against it - never automatic,
    and never a side effect of asking to start again.
    """

    verdict: SafetyVerdict


@dataclass(frozen=True, slots=True)
class NothingRunning:
    """There is no session to end."""

    state: RunState


type StartRefusal = SurfaceBusy | SafetyHolding | EstopUnattested
"""Every way a start can be refused. Closed.

:class:`~src.training.safety.EstopUnattested` is reused from the safety module
rather than restated, because it is the same refusal: nobody has confirmed,
this boot, that a real emergency stop is wired in. A second type meaning the
same thing is a second place for the wording to drift.
"""

type EndRefusal = NothingRunning | SurfaceBusy
"""Every way an end can be refused. Closed."""

type CommandRefusal = NothingRunning | SurfaceBusy
"""Every way a target or a fault reset can be refused by the surface. Closed.

The surface only judges whether the mailbox can take it; whether the machine
can act on it is the runtime's answer, published as a REFUSED event.
"""

_FATE_EVENTS: Final[Mapping[Fate, EventKind]] = {
    Fate.TAKEN: EventKind.TAKEN,
    Fate.REFUSED: EventKind.REFUSED,
    Fate.CANCELLED: EventKind.CANCELLED,
}
"""The event each fate is published as."""


@dataclass(frozen=True, slots=True)
class EstopReceipt:
    """Proof that an emergency stop was latched, built in one piece.

    Returned to the request handler so the browser can display what actually
    happened rather than a bare 200, and published as an event so every other
    connected screen learns of it within one socket write.

    ``at`` is the supervisor's own latch instant, copied from the verdict rather
    than read again. One instant, one meaning: two reads would let the receipt
    and the safety log disagree about when the machine was stopped.

    **This receipt is not a claim that the machine has stopped.** The drive's
    STO input is jumpered, there is no independent removal of torque, and the
    fastest stop available is the commissioned deceleration ramp. It says the
    demand is latched.
    """

    operator: str
    reason: str
    verdict: SafetyVerdict
    at: Monotonic
    wall_clock: UnixMillis


# =========================================================================
# The surface
# =========================================================================


@final
class ControlSurface:
    """The mailbox, the latest snapshot, and the emergency stop.

    Mutable shared state, which the contract says must be the exception and
    must be commented. It is: this is the one object two sides of the system
    write to, and the module docstring above is the comment. Every field is a
    plain flag or a frozen record, and every method is synchronous, so the
    "single event-loop thread" argument is the whole synchronisation strategy.
    """

    __slots__ = (
        "_accepted",
        "_acknowledger",
        "_boot",
        "_clock",
        "_ending",
        "_estop_latched",
        "_estop_receipt",
        "_fates",
        "_last_seen",
        "_latest",
        "_live",
        "_pending",
        "_published",
        "_refused",
        "_requests",
        "_session_request",
        "_sink",
        "_starting",
        "_supervisor",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        supervisor: SafetySupervisor,
        sink: TelemetrySink,
        acknowledger: Acknowledger | None = None,
        boot: str | None = None,
    ) -> None:
        """Wire the surface to the clock, the supervisor and the telemetry sink.

        ``acknowledger`` clears latches on :meth:`acknowledge`; the supervisor
        when omitted. See :class:`Acknowledger`.

        ``boot`` is what the request identifiers of this surface begin with. A
        few random characters when omitted, drawn once, here: a console that
        restarts counts its requests from one again, and a screen that outlived
        it must not take the new console's third request for the third request
        it sent to the old one. Nothing else in this module asks the operating
        system for anything.
        """
        self._clock: Clock = clock
        self._supervisor: SafetySupervisor = supervisor
        self._sink: TelemetrySink = sink
        self._acknowledger: Acknowledger = supervisor if acknowledger is None else acknowledger

        self._pending: Command | None = None
        self._latest: TelemetrySnapshot | None = None
        self._estop_latched: bool = False
        self._estop_receipt: EstopReceipt | None = None
        self._starting: bool = False
        self._live: bool = False
        self._ending: bool = False
        self._last_seen: Monotonic | None = None

        # Counters, for the status page and the session log. Not safety state.
        self._accepted: int = 0
        self._refused: int = 0
        self._published: int = 0

        # What the screens are told about requests. Display data: no decision
        # of this class or of the loop reads any of the four.
        self._boot: str = secrets.token_hex(4) if boot is None else boot
        self._requests: int = 0
        self._fates: tuple[RequestFate, ...] = ()
        self._session_request: str | None = None

    # =====================================================================
    # Reading
    # =====================================================================

    @property
    def run_state(self) -> RunState:
        """Where the session is, derived from the flags. Never stored.

        Order matters: a latched emergency stop outranks everything, and
        "asked to stop" outranks "running", because the honest answer while a
        stop is in progress is that the machine has not stopped.
        """
        if self._estop_latched or self._ending:
            return RunState.STOPPING
        if self._live:
            return RunState.RUNNING
        if self._starting:
            return RunState.STARTING
        return RunState.IDLE

    @property
    def pending(self) -> Command | None:
        """What is in the mailbox, without taking it. For the status page."""
        return self._pending

    @property
    def latest(self) -> TelemetrySnapshot | None:
        """The newest snapshot the loop published, or ``None`` before the first tick.

        ``None`` is a real answer and must be rendered as one. A page that
        substituted zeros for "no snapshot yet" would show a stopped machine
        before anything had been measured, which is the exact lie this system
        is organised around not telling.
        """
        return self._latest

    @property
    def estop_latched(self) -> bool:
        """Whether an emergency stop is latched here. Sticky until acknowledged."""
        return self._estop_latched

    @property
    def attendant_last_seen(self) -> Monotonic | None:
        """When the attendant's screen last pinged, or ``None`` if never.

        Feeds :attr:`~src.training.safety.SafetyObservation.attendant_last_seen`,
        which is what the ``attendant_absent`` rule reads. ``None`` before the
        first ping is deliberate and the rule treats it as "no attendant", not
        as "fine": a browser that never connected is indistinguishable from one
        that was closed.

        A ping proves a browser tab is open and reachable. It does not prove a
        human is looking at it, and nothing in software can.
        """
        return self._last_seen

    @property
    def counters(self) -> tuple[int, int, int]:
        """``(accepted, refused, published)``, for the status page."""
        return (self._accepted, self._refused, self._published)

    @property
    def fates(self) -> tuple[RequestFate, ...]:
        """What became of the last :data:`FATES_KEPT` requests answered, oldest first.

        For the status page: a screen whose socket was down when the event of
        its request went out finds the answer here. A request that still waits
        in the mailbox, or that the loop is acting on at this instant, is not
        in it yet.
        """
        return self._fates

    @property
    def session_request(self) -> str | None:
        """The identifier of the start the session in progress came from, or ``None``.

        From the moment that start is accepted until the surface is idle again:
        while it waits, while the loop arms it, while the session runs and
        while it stops. ``None`` at rest, and after a start that was refused or
        displaced. Display data.
        """
        return self._session_request

    # =====================================================================
    # The emergency stop
    # =====================================================================

    def submit_estop(self, *, operator: str, reason: str) -> EstopReceipt:
        """Latch an emergency stop. Synchronous, non-awaiting, no I/O, never refused.

        **Three things happen, in this order, and then it returns.** The order
        is the design:

        1. the flag is set - one plain attribute write, which cannot fail;
        2. the supervisor's own synchronous ``latch_estop`` runs, so
           ``standing_action`` reports ``QUICK_STOP`` on the very next read from
           any code path, with no tick in between;
        3. the event is published, so every other connected screen learns of it.

        The flag goes first so that even if something below it misbehaved, the
        loop would still find the stop latched on its next tick. Publishing goes
        last because it is the only step that touches another object's queues.

        What is **not** here, on purpose: no ``await``, no Modbus write, no
        database write, no log flush ahead of the latch, and no confirmation
        dialog. A stop that first asks "are you sure?" is not an emergency stop,
        and a stop that waits for the control loop to finish awaiting a serial
        read is not one either.

        Note also what this cannot do. The surface never touches the wire, so
        the loop must still call
        :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`; and a
        browser button is a *convenience* stop that depends on a network, a web
        server, an event loop and this process, any one of which can be the
        thing that failed. It is never safety-rated. The safety-rated stop is
        the wired mushroom, and while STO is jumpered even that one is a ramp.
        """
        # 1. Latch. One write, no computation, nothing that can raise.
        self._estop_latched = True

        # 2. The supervisor's synchronous latch.
        verdict = self._supervisor.latch_estop(f"{operator or 'unattributed'}: {reason}")

        # 3. Publish. The receipt is built complete and assigned in one write.
        receipt = EstopReceipt(
            operator=operator,
            reason=reason,
            verdict=verdict,
            at=verdict.since,
            wall_clock=self._clock.unix_millis(),
        )
        self._estop_receipt = receipt
        self._sink.publish_event(
            SessionEvent(
                kind=EventKind.EMERGENCY_STOP,
                at=receipt.at,
                wall_clock=receipt.wall_clock,
                operator=operator,
                detail=receipt.verdict.detail,
            )
        )
        _logger.error("emergency stop submitted")
        return receipt

    def take_estop(self) -> EstopReceipt | None:
        """Hand the pending receipt to the loop, once.

        The loop uses this to know it must act - zero the reference, then
        attempt the synchronous disable. :attr:`estop_latched` stays ``True``
        afterwards, because the *demand* outlives the loop's reaction to it and
        only an acknowledgement clears it.
        """
        receipt = self._estop_receipt
        self._estop_receipt = None
        return receipt

    # =====================================================================
    # The mailbox
    # =====================================================================

    def submit_start(
        self,
        *,
        profile_id: str,
        operator: str,
        total_duration_s: Seconds | None,
        subject_id: str = "",
        subject_hr_max: Bpm | None = None,
        subject_age: int | None = None,
        cloud_session_id: str | None = None,
        confirmed_by: str | None = None,
    ) -> Result[StartSession, StartRefusal]:
        """Ask for a session to start. Refused unless everything below is true.

        Three gates, in this order, and the order is from least to most
        recoverable so the operator is told the thing they can act on:

        1. the emergency-stop wiring must be attested this boot
           (:meth:`~src.training.safety.SafetySupervisor.require_estop_confirmed`);
        2. no latched safety verdict may stand;
        3. the surface must be IDLE **and its mailbox empty**. A start never
           takes the place of a request that waits: while another start waits
           or is being armed this one is refused at once, and so is one that
           finds a reset, a target or a stop still waiting for the loop.

        Note what is **not** checked here: whether ``profile_id`` exists. That
        needs the profile store, which is a file read, and this module does no
        I/O. The route validates the id before submitting, and the loop
        re-resolves it; a start for an unknown profile ends the session
        immediately with a refusal in the log rather than commanding motion.
        """
        checked = self.check_start()
        if isinstance(checked, Err):
            return self._refuse_start(checked.error)

        command = StartSession(
            profile_id=profile_id,
            operator=operator,
            total_duration_s=total_duration_s,
            at=self._clock.monotonic(),
            request_id=self._next_request(),
            subject_id=subject_id or LOCAL_SUBJECT,
            subject_hr_max=subject_hr_max,
            subject_age=subject_age,
            cloud_session_id=cloud_session_id,
            confirmed_by=confirmed_by,
        )
        self._accept_start(command)
        return Ok(command)

    def check_start(self) -> Result[None, StartRefusal]:
        """Check the three start gates without recording or submitting a request."""
        attested = self._supervisor.require_estop_confirmed()
        if isinstance(attested, Err):
            return Err(attested.error)
        standing = self._supervisor.standing
        if standing is not None and standing.latched:
            return Err(SafetyHolding(standing))
        busy = self._in_the_way_of_a_start()
        if busy is not None:
            return Err(busy)
        return Ok(None)

    def submit_start_manual(
        self, *, occupancy: Occupancy, operator: str, subject_id: str = ""
    ) -> Result[StartManual, StartRefusal]:
        """Ask for a manual session to start. The same three gates as :meth:`submit_start`.

        ``subject_id`` names the rider with a person on board; the loop checks
        it against the riders' list when it arms, as it checks the occupancy.
        """
        attested = self._supervisor.require_estop_confirmed()
        if isinstance(attested, Err):
            return self._refuse_start(attested.error)
        standing = self._supervisor.standing
        if standing is not None and standing.latched:
            return self._refuse_start(SafetyHolding(standing))
        busy = self._in_the_way_of_a_start()
        if busy is not None:
            return self._refuse_start(busy)
        command = StartManual(
            occupancy=occupancy,
            operator=operator,
            at=self._clock.monotonic(),
            request_id=self._next_request(),
            subject_id=subject_id or LOCAL_SUBJECT,
        )
        self._accept_start(command)
        return Ok(command)

    def submit_manual_target(
        self, *, output_rpm: OutputRpm, operator: str
    ) -> Result[SetManualTarget, CommandRefusal]:
        """Hand the loop a new manual target. Only while a session runs, one at a time.

        No event is published for the request itself: the operator adjusts the
        target in steps, and the setpoint that follows is in every snapshot.
        What the loop made of it is published, like every other answer.
        """
        state = self.run_state
        if state is not RunState.RUNNING:
            return self._refuse_command(NothingRunning(state))
        waiting = self._pending
        if waiting is not None:
            return self._refuse_command(
                SurfaceBusy(state=state, pending=waiting, in_the_way=waiting.request_id)
            )
        command = SetManualTarget(
            output_rpm=output_rpm,
            operator=operator,
            at=self._clock.monotonic(),
            request_id=self._next_request(),
        )
        self._pending = command
        self._accepted += 1
        return Ok(command)

    def submit_fault_reset(self, *, operator: str) -> Result[FaultReset, CommandRefusal]:
        """Hand the loop an operator's fault reset. Whether it is safe is the runtime's call.

        Accepted whenever the mailbox is empty and no start is in flight: a
        drive can fault during a session that then cannot finish, and that is
        exactly when the reset is needed. The runtime refuses it unless the
        machine is at rest, every verdict is acknowledged and the shaft is
        shown stopped.
        """
        state = self.run_state
        waiting = self._pending
        if waiting is not None:
            return self._refuse_command(
                SurfaceBusy(state=state, pending=waiting, in_the_way=waiting.request_id)
            )
        if state is RunState.STARTING:
            # The loop is arming a start it has already taken from the mailbox.
            return self._refuse_command(
                SurfaceBusy(state=state, pending=None, in_the_way=self._session_request)
            )
        command = FaultReset(
            operator=operator, at=self._clock.monotonic(), request_id=self._next_request()
        )
        self._pending = command
        self._accepted += 1
        self._publish(
            EventKind.FAULT_RESET_REQUESTED,
            command.at,
            operator,
            words_of(command),
            command.request_id,
        )
        return Ok(command)

    def submit_end(self, *, operator: str, reason: str) -> Result[EndSession, EndRefusal]:
        """Ask for the running session to end on the controlled ramp.

        **Never refused, and never made to wait, because of what the mailbox
        holds.** A stop takes the slot whatever is in it: a start the loop has
        not taken yet, a target, a reset. What it displaces is not lost in
        silence: its screen is told, under its own identifier, that a stop
        took its place (:attr:`EventKind.CANCELLED`). The only refusals are
        the two that have nothing to do with the slot: no session to stop, and
        a stop already under way.
        """
        state = self.run_state
        if state is RunState.IDLE:
            return self._refuse_end(NothingRunning(state))
        if state is RunState.STOPPING:
            return self._refuse_end(SurfaceBusy(state=state, pending=self._pending))

        command = EndSession(
            operator=operator,
            reason=reason,
            at=self._clock.monotonic(),
            request_id=self._next_request(),
        )
        displaced = self._pending
        self._ending = True
        self._pending = command
        self._accepted += 1
        self._publish(EventKind.END_REQUESTED, command.at, operator, reason, command.request_id)
        if displaced is not None:
            self._cancel(displaced)
        return Ok(command)

    def take_command(self) -> Command | None:
        """Empty the mailbox and hand the command to the loop.

        The slot is cleared before the loop acts, so a command can never be
        executed twice, and a refusal to the operator's second click is issued
        on the basis of :attr:`run_state` rather than of whether the loop has
        got round to it yet.
        """
        command = self._pending
        self._pending = None
        return command

    # =====================================================================
    # What the loop reports back
    # =====================================================================

    def publish(self, snapshot: TelemetrySnapshot) -> None:
        """Publish the tick's snapshot: store it, then fan it out.

        Stored first. A sink that coalesced or evicted must not be able to leave
        the surface without the newest state, because ``GET /api/snapshot`` is
        what a browser falls back to when its socket has dropped.
        """
        self._latest = snapshot
        self._published += 1
        self._sink.publish_snapshot(snapshot)

    def note_running(self) -> None:
        """The loop reports the session is live. Idempotent."""
        if self._live:
            return
        self._live = True
        self._starting = False
        self._publish(
            EventKind.SESSION_RUNNING,
            self._clock.monotonic(),
            "",
            "session running",
            self._session_request,
        )

    def note_idle(self) -> None:
        """The loop reports that no session is in progress. Idempotent.

        Clears the start/stop intentions but **not** the emergency-stop latch: a
        session ending is not somebody having answered for why it was stopped.
        Only :meth:`acknowledge` clears that.

        **And not an intention whose request still waits in the mailbox.** The
        loop awaits the drive inside its tick, so a start (or a stop) can be
        accepted while a tick is under way, and that tick then ends at rest
        with the request it never saw still in the slot. Calling the surface
        idle at that point let a second start be accepted over the first. The
        loop's report is about the tick that has just ended; the request is for
        the next one. So ``session_idle`` is published when the last intention
        is cleared, once, and not while a request it stands for is waiting.
        """
        waiting = self._pending
        starting = self._starting and is_start(waiting)
        ending = self._ending and isinstance(waiting, EndSession)
        if (self._live, self._starting, self._ending) == (False, starting, ending):
            return
        self._live = False
        self._starting = starting
        self._ending = ending
        if starting or ending:
            return
        over = self._session_request
        self._session_request = None
        self._publish(EventKind.SESSION_IDLE, self._clock.monotonic(), "", "session idle", over)

    def note_taken(self, command: Command) -> None:
        """The loop took ``command`` from the mailbox and acted on it. Say so, once.

        Published as it happens, with the request's identifier, so that the
        screen that asked reads "taken" from the console and not from a frame
        that happens to show a session. What it says is that the loop acted:
        the session is armed, the target is held, the reset is written, the
        stop is under way. What the machine does next is in the snapshots.
        """
        self._conclude(command, Fate.TAKEN, words_of(command))

    def note_refused(self, command: Command, detail: str) -> None:
        """The loop could not act on ``command``, taken from the mailbox. Say so, once.

        A start that the runtime refused leaves the surface idle again, so the
        operator can correct and retry; the reason goes out as an event, under
        the request's identifier. Only the refusal of a START does that: a
        reset or a target refused while a start waits in the mailbox says
        nothing about that start, which keeps its place.
        """
        self._refused += 1
        if is_start(command):
            self._starting = False
            self._session_request = None
        self._conclude(command, Fate.REFUSED, detail)
        _logger.warning("command refused by the loop")

    def note_withdrawn(self, detail: str) -> None:
        """The machine took back a manual target it had accepted. Say so, once.

        Nobody's request: the event names no operator and carries no request
        identifier, and every screen showing the session is told. It touches
        neither the mailbox nor an intention.
        """
        self._refused += 1
        self._publish(EventKind.REFUSED, self._clock.monotonic(), "", detail)
        _logger.warning("manual target withdrawn by the loop")

    def note_remote_refusal(self, detail: str) -> None:
        """The dashboard link turned something down on this machine's behalf. Say so.

        The operator reads it in the same list as everything else, but it
        answers no command from the mailbox. Unlike :meth:`note_refused` it
        neither counts as a refused command nor touches the start intention,
        so a start the operator has just typed here is not un-queued by news
        about the dashboard. And it goes out under its own kind: on the page a
        ``refused`` event is the machine's answer about the manual target on
        screen, which this never is.
        """
        self._publish(EventKind.DASHBOARD, self._clock.monotonic(), "", detail)

    def note_launch_confirmed(self, operator: str) -> None:
        self._publish(EventKind.LAUNCH_CONFIRMED, self._clock.monotonic(), operator, "")

    def note_recording(self, detail: str) -> None:
        """The session record cannot be written as it should, or can again. Say so.

        A message, never a command: the session it is about goes on, and so
        does every safety rule. Nothing here touches the mailbox or a flag.
        """
        self._publish(EventKind.RECORDING, self._clock.monotonic(), "", detail)

    def note_presence(self, operator: str) -> Monotonic:
        """Record an attendant presence ping. Returns the instant recorded.

        No event is published: pings arrive every few seconds and would drown
        the event stream, which exists for things that happened once. The
        freshness of this instant is the whole signal.
        """
        now = self._clock.monotonic()
        self._last_seen = now
        _logger.debug("attendant presence from %r", operator)
        return now

    # =====================================================================
    # Clearing what was latched
    # =====================================================================

    def attest_estop_wiring(self, operator: str) -> Result[EstopAttestation, AttestationRefusal]:
        """Record the operator's attestation that a real emergency stop is wired in.

        A thin pass-through to the supervisor plus an event, so the attestation
        appears in the same operator-visible stream as everything else. The
        gate itself, and the reason it is per-boot rather than per-install,
        lives in :meth:`~src.training.safety.SafetySupervisor.confirm_estop_wiring`.
        """
        recorded = self._supervisor.confirm_estop_wiring(operator)
        match recorded:
            case Ok(attestation):
                self._publish(EventKind.ATTESTED, attestation.at, operator, attestation.statement)
            case Err(_):
                self._refused += 1
        return recorded

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Clear the latched verdicts, by name, and release this surface's latch.

        The surface's own flag is cleared **only** when the supervisor accepted
        the acknowledgement. Clearing it on a refusal would let the screen show
        an un-latched machine while the supervisor still holds ``QUICK_STOP`` -
        the two would disagree, and the screen is the one the operator believes.

        ``estop_released`` defaults to ``False`` all the way up the stack for
        the same reason it does in the supervisor: forgetting to ask whether the
        mushroom has been pulled back out must fail closed.
        """
        cleared = self._acknowledger.acknowledge(operator, estop_released=estop_released)
        match cleared:
            case Ok(record):
                self._estop_latched = False
                self._estop_receipt = None
                self._publish(
                    EventKind.ACKNOWLEDGED,
                    record.at,
                    operator,
                    ", ".join(record.cleared),
                )
            case Err(_):
                self._refused += 1
        return cleared

    # =====================================================================
    # Internals
    # =====================================================================

    def _publish(
        self,
        kind: EventKind,
        at: Monotonic,
        operator: str,
        detail: str,
        request_id: str | None = None,
    ) -> None:
        """Build and hand one event to the sink."""
        self._sink.publish_event(
            SessionEvent(
                kind=kind,
                at=at,
                wall_clock=self._clock.unix_millis(),
                operator=operator,
                detail=detail,
                request_id=request_id,
            )
        )

    def _next_request(self) -> str:
        """A new request identifier: this boot's prefix and a count. Opaque to everyone else."""
        self._requests += 1
        return f"{self._boot}-{self._requests}"

    def _in_the_way_of_a_start(self) -> SurfaceBusy | None:
        """What keeps a start out of the mailbox, or ``None`` when it may go in."""
        state = self.run_state
        waiting = self._pending
        if state is RunState.STARTING:
            return SurfaceBusy(state=state, pending=waiting, in_the_way=self._session_request)
        if state is not RunState.IDLE:
            return SurfaceBusy(state=state, pending=waiting)
        if waiting is not None:
            # Idle, and a reset or a target still waits for the loop: it keeps its place.
            return SurfaceBusy(state=state, pending=waiting, in_the_way=waiting.request_id)
        return None

    def _accept_start(self, command: StartSession | StartManual) -> None:
        """Put an accepted start in the empty mailbox, and say that it was asked."""
        self._starting = True
        self._session_request = command.request_id
        self._pending = command
        self._accepted += 1
        self._publish(
            EventKind.START_REQUESTED,
            command.at,
            command.operator,
            words_of(command),
            command.request_id,
        )

    def _cancel(self, displaced: Command) -> None:
        """A stop has taken the place of ``displaced`` in the mailbox. Tell its screen.

        A start that is displaced no longer exists: nothing is starting. The
        stop that displaced it stands, and the surface reads STOPPING until the
        loop has taken that stop and reported itself idle.
        """
        if is_start(displaced):
            self._starting = False
            self._session_request = None
        self._conclude(displaced, Fate.CANCELLED, CANCELLED_BY_STOP)

    def _conclude(self, command: Command, fate: Fate, detail: str) -> None:
        """Record the one fate of ``command`` and publish it, under its identifier.

        Kept first, published second: a screen that asks the status instead of
        listening must find at least what the others were told.
        """
        at = self._clock.monotonic()
        wall_clock = self._clock.unix_millis()
        said = RequestFate(
            request_id=command.request_id, fate=fate, detail=detail, at=at, wall_clock=wall_clock
        )
        self._fates = (*self._fates, said)[-FATES_KEPT:]
        self._sink.publish_event(
            SessionEvent(
                kind=_FATE_EVENTS[fate],
                at=at,
                wall_clock=wall_clock,
                operator=command.operator,
                detail=detail,
                request_id=command.request_id,
            )
        )

    def _refuse_start(self, refusal: StartRefusal) -> Err[StartRefusal]:
        """Count and log a refused start, then return it."""
        self._refused += 1
        _logger.warning("start refused: %s", type(refusal).__name__)
        return Err(refusal)

    def _refuse_command(self, refusal: CommandRefusal) -> Err[CommandRefusal]:
        """Count and log a refused target or fault reset, then return it."""
        self._refused += 1
        _logger.warning("command refused: %s", type(refusal).__name__)
        return Err(refusal)

    def _refuse_end(self, refusal: EndRefusal) -> Err[EndRefusal]:
        """Count and log a refused end, then return it."""
        self._refused += 1
        _logger.warning("end refused: %s", type(refusal).__name__)
        return Err(refusal)
