"""The local console's link to the Convex dashboard. Optional, and never in charge.

What crosses the link, and in which direction
---------------------------------------------
Up, from this machine to the dashboard:

* a heartbeat every :data:`HEARTBEAT_PERIOD` carrying what the machine is doing
  right now (run mode, phase, heart rate, speeds, g, safety action);
* the training presets of the profile store, whenever its revision changes -
  only those whose cardiac tiers match this machine's supervisor, because a
  preset the runtime would refuse must not be offered on a screen;
* every session this console runs, whoever started it: an AUTO session
  launched from the dashboard, and any session started here at the machine
  (MANUAL, or AUTO from the panel), declared under the reference its record
  carries, so that declaring it again, even after a restart, cannot create it
  twice;
* its telemetry at 1 Hz, its events and its end, read back from the session's
  record on disk by :mod:`src.record_uplink`: what the dashboard has not
  acknowledged is on disk, never in a queue here.

Down, from the dashboard to this machine, exactly two things:

* an AUTO launch: a preset id, a rider and the rider's maximum heart rate. It
  goes through :meth:`~src.control_surface.ControlSurface.submit_start` like a
  start typed at the console, so the same gates apply (emergency-stop wiring
  attested this boot, no standing verdict, the surface idle), and then through
  the panel's own checks (programmes enabled, occupied ceiling, the preset
  re-validated against this rider). Any refusal goes back as a failed session
  with the reason;
* a stop request, forwarded as an ordinary end on the commissioned ramp. It
  is looked for by :meth:`CloudSync.watch_stop`, from a task of its own: the
  question keeps its cadence whatever the sending waits for. And a launch
  whose start the dashboard does not take, for any reason it gives, is
  stopped at once: a session that came from the dashboard never runs on
  where the dashboard could not stop it.

**Nothing else.** In particular a MANUAL session can never be started from
the dashboard: nothing here can build one. Manual control needs somebody at
the machine.

Why the link can never stop the machine by failing
--------------------------------------------------
Every call returns a :class:`~src.result.Result`; a dead network is
``Err(Unreachable)``, logged and retried later, and the session carries on
under the local supervisor exactly as it would with no dashboard at all. The
worst a lost link does is leave the dashboard's picture stale, which the
dashboard shows as stale. Nothing waits for the link in memory: what it owes
the dashboard is in the session records, and is sent when the link answers
again, after a restart of the console too (:func:`answer_of` says what each
answer does to the sending).

One contract, checked on both sides
-----------------------------------
Every request carries the contract this console speaks
(:mod:`src.contract`), and the heartbeat says which software it runs. A
dashboard of another major refuses the console (426), and the console refuses
the dashboard: a poll answer that does not name this console's major arms
nothing, whatever launch it carries. Either way the operator reads
``serveur incompatible (contrat X vs Y)`` in the console's event list, and the
machine goes on exactly as it would with no dashboard. A session that runs
while the console is refused loses nothing: it is on disk, and is sent whole,
with its end, once the two sides agree again.

One thing crosses every contract: **a stop**. "Stop" means the same under any
version, and refusing one is never the safe side. The status route answers
whatever contract the console announces, and its answer ends the session
whatever major it is of, in either of the two ways it always could: a stop
was asked for, or the dashboard no longer holds the session active. Both can
only ever cause an ordinary stop. Nothing else in an answer of another major
is acted on: nothing in it can start, resume or re-arm anything.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol, assert_never, cast

import httpx

from src.clock import Clock
from src.contract import (
    CONTRACT_HEADER,
    CONTRACT_UNSUPPORTED,
    CONTRACT_VERSION,
    SERVER_VERSION_FIELD,
    UNKNOWN_SOFTWARE_VERSION,
    ErrorCode,
    SoftwareVersion,
    error_code_of,
    majors_of,
    server_refusal,
    unsupported_refusal,
)
from src.control_surface import ControlSurface, SafetyHolding, StartRefusal, SurfaceBusy
from src.local_config import CardiacTiers, CloudConfig
from src.record_uplink import (
    Acked,
    Answer,
    ArmedSession,
    Declaration,
    Held,
    RecordSource,
    RecordUplink,
    Refusal,
    RouteMissing,
    RuntimeEnd,
)
from src.result import Err, Ok, Result
from src.training.plan import JsonValue, ProfileStore, TrainingProfile
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import EstopUnattested
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import Bpm, Monotonic, Seconds, UnixMillis, elapsed

_logger: logging.Logger = logging.getLogger(__name__)

# --- cadence ---------------------------------------------------------------

HEARTBEAT_PERIOD: Final[Seconds] = Seconds(10.0)
"""Well inside the dashboard's 90 s offline cutoff, cheap enough to be live."""

POLL_PERIOD: Final[Seconds] = Seconds(3.0)
"""How often an idle machine asks whether a launch is waiting."""

STATUS_PERIOD: Final[Seconds] = Seconds(3.0)
"""How often a running session asks whether the dashboard wants it stopped."""

RETRY_PERIOD: Final[Seconds] = Seconds(15.0)
"""After a failure, how long before a preset push is retried."""

LAUNCH_TIMEOUT: Final[Seconds] = Seconds(60.0)
"""A launch the loop has neither started nor refused by now is reported as failed."""

INCOMPATIBLE_REPEAT: Final[Seconds] = Seconds(60.0)
"""How often a dashboard of another contract is said again on the console while it lasts.

The event list is only sent to the screens connected at that moment, so a
standing condition said once at startup would never reach a page opened later.
"""

HELD_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 408, 425, 426, 429})
"""Refusals that are about the link, not about what was sent: the key is not accepted,
the contract is not served, the server asks to wait. A 5xx is one too."""

SERVER_ERROR: Final[int] = 500

FINAL_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_request", "session_not_found", "session_not_pending", "machine_not_found"}
)
"""Stable codes that say the same request will always be refused
(``contracts/machine-api.json``). ``request_failed`` is not one: it also covers a
failure of the moment."""

DASHBOARD_OPERATOR: Final[str] = "tableau de bord"
"""Who a stop from the dashboard is attributed to in the console's event list."""


# =========================================================================
# The transport
# =========================================================================


@dataclass(frozen=True, slots=True)
class Unreachable:
    """No answer: no network, a timeout, or a reply that is not JSON."""

    detail: str


@dataclass(frozen=True, slots=True)
class Refused:
    """The dashboard answered and said no. Retrying the same request will not help."""

    status: int
    detail: str
    """The dashboard's words, for a person."""

    code: ErrorCode | None = None
    """Its stable code (``contracts/machine-api.json``); ``None`` when it sent only words."""

    supported: tuple[str, ...] = ()
    """With ``contract_unsupported``: the contract majors the dashboard serves."""


type CloudError = Unreachable | Refused
type Document = Mapping[str, object]


class CloudTransport(Protocol):
    """GET and POST against the dashboard's machine API. Never raises."""

    @abstractmethod
    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        """GET ``path`` with ``params``. The answered document, or why there is none."""

    @abstractmethod
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        """POST ``body`` to ``path``. The answered document, or why there is none."""


def _parse_document(text: str) -> Document | None:
    """The one place an untyped value enters: annotated ``object`` at once, then narrowed."""
    try:
        parsed: object = json.loads(text or "{}")  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    # JSON object keys are strings by construction.
    return cast("Document", parsed)


class HttpxTransport:
    """:class:`CloudTransport` over ``httpx``, with a short timeout: the loop must not wait."""

    __slots__ = ("_client",)

    def __init__(self, config: CloudConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._client: httpx.AsyncClient = client or httpx.AsyncClient(
            base_url=config.url,
            timeout=httpx.Timeout(3.0),
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        # On the client itself, so no request can leave without it.
        self._client.headers[CONTRACT_HEADER] = CONTRACT_VERSION

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        try:
            response = await self._client.get(path, params=dict(params or {}))
        except httpx.HTTPError as error:
            return Err(Unreachable(f"GET {path}: {error!r}"))
        return _answer(response)

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        try:
            response = await self._client.post(path, content=json.dumps(body))
        except httpx.HTTPError as error:
            return Err(Unreachable(f"POST {path}: {error!r}"))
        return _answer(response)

    async def close(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()


def _answer(response: httpx.Response) -> Result[Document, CloudError]:
    document = _parse_document(response.text)
    if response.status_code >= httpx.codes.BAD_REQUEST:
        return Err(_refusal(response.status_code, document))
    if document is None:
        return Err(Unreachable(f"HTTP {response.status_code}: not a JSON object"))
    return Ok(document)


def _refusal(status: int, document: Document | None) -> Refused:
    """A refusal as the dashboard words it: ``{error: <stable code>, message: <words>}``.

    A dashboard older than the stable codes sends its sentence in ``error``
    alone; it is then kept as the words, with no code.
    """
    found = None if document is None else document.get("error")
    if document is None or not isinstance(found, str):
        return Refused(status=status, detail=f"HTTP {status}")
    message = document.get("message")
    return Refused(
        status=status,
        detail=message if isinstance(message, str) else found,
        code=error_code_of(found),
        supported=majors_of(document.get("supported")),
    )


def describe_refusal(refused: Refused) -> str:
    """One log line for a refusal: its stable code first, then the status and the words."""
    code = "sans code" if refused.code is None else refused.code
    return f"{code} (HTTP {refused.status}): {refused.detail}"


# =========================================================================
# What the panel tells the link
# =========================================================================


@unique
class SessionKind(Enum):
    """The dashboard's two training kinds. Wire strings."""

    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True, kw_only=True)
class StartedSession:
    """A session the runtime has just armed, as the dashboard records it."""

    kind: SessionKind
    operator: str
    started_at: UnixMillis
    subject_id: str
    cloud_session_id: str | None
    """The dashboard launch this answers; ``None`` for a start at the machine."""

    profile: TrainingProfile | None = None
    """The programme actually run (already fitted to the rider); ``None`` for MANUAL."""

    occupancy: Occupancy | None = None
    """Declared for a MANUAL session; ``None`` for AUTO (always occupied)."""


class SessionListener(Protocol):
    """What the panel calls after it has acted on a start. Never blocks, never raises."""

    @abstractmethod
    def session_started(self, started: StartedSession) -> None:
        """A session has just been armed."""

    @abstractmethod
    def start_refused(self, cloud_session_id: str, detail: str) -> None:
        """The launch ``cloud_session_id`` was refused; ``detail`` says why."""


class RuntimeView(Protocol):
    """The four things the link reads from the runtime."""

    @property
    @abstractmethod
    def state(self) -> RuntimeState:
        """Where the runtime is in a session's life."""

    @property
    @abstractmethod
    def end_reason(self) -> EndReason | None:
        """Why the session ended, or ``None`` while it has not."""

    @property
    @abstractmethod
    def stop_reason(self) -> str | None:
        """The reason given by the first operator stop request, or ``None``."""

    @abstractmethod
    def snapshot(self) -> TelemetrySnapshot:
        """The last published picture of the session."""


# =========================================================================
# Wire shapes
# =========================================================================


def profile_row(profile: TrainingProfile) -> Mapping[str, JsonValue]:
    """A preset, as ``/api/machine/profiles`` takes it."""
    return {
        "profileId": profile.profile_id,
        "name": profile.name,
        "totalDurationS": float(profile.total_duration_s),
        "zoneLowBpm": int(profile.zone_low_bpm),
        "zoneHighBpm": int(profile.zone_high_bpm),
        "hardMaxBpm": int(profile.hard_max_bpm),
        "criticalBpm": int(profile.critical_bpm),
        "subjectHrMax": int(profile.subject_hr_max),
        "minRunRpm": int(profile.min_run_rpm),
        "maxRpm": int(profile.max_rpm),
    }


def runnable_profiles(
    profiles: Sequence[TrainingProfile], tiers: CardiacTiers
) -> tuple[TrainingProfile, ...]:
    """The presets this machine's supervisor would accept: their tiers are its tiers."""
    return tuple(
        p
        for p in profiles
        if p.hard_max_bpm == tiers.hard_max_bpm and p.critical_bpm == tiers.critical_bpm
    )


def live_row(snapshot: TelemetrySnapshot, session_id: str | None) -> Mapping[str, JsonValue]:
    """The machine's state for the heartbeat. ``bpm`` only when fresh and trustworthy."""
    row: dict[str, JsonValue] = {
        "runMode": snapshot.mode.value,
        "phase": snapshot.phase.value,
        "motorRpm": int(snapshot.measured.motor_rpm),
        "outputRpm": round(float(snapshot.measured.output_rpm), 3),
        "setpointMotorRpm": int(snapshot.setpoint.motor_rpm),
        "gLoad": round(float(snapshot.measured.g_load), 4),
        "safetyAction": snapshot.safety_action.name.lower(),
        "driveState": snapshot.drive_state.name.lower(),
    }
    bpm = snapshot.live_bpm
    if bpm is not None:
        row["bpm"] = int(bpm)
    if session_id is not None:
        row["sessionId"] = session_id
    return row


def describe_surface_refusal(refusal: StartRefusal) -> str:
    """One line for a launch the console's surface refused. Exhaustive."""
    match refusal:
        case EstopUnattested():
            return "cablage de l'arret d'urgence non atteste depuis le demarrage de la console"
        case SafetyHolding(verdict=verdict):
            return f"verdict de securite {verdict.rule} a acquitter a la console"
        case SurfaceBusy(state=state):
            return f"console occupee ({state.value})"
    raise assert_never(refusal)


def end_is_failure(reason: EndReason | None) -> bool:
    """Whether the dashboard records the session as failed rather than completed."""
    match reason:
        case EndReason.PROGRAMME_COMPLETE | EndReason.OPERATOR_STOP:
            return False
        case (
            EndReason.EMERGENCY_STOP
            | EndReason.SAFETY_VERDICT
            | EndReason.TICK_EXCEPTION
            | EndReason.SHUTDOWN
            | None
        ):
            return True
    raise assert_never(reason)


# =========================================================================
# The link
# =========================================================================


def declaration_of(started: StartedSession) -> Declaration:
    """What the dashboard is told of a session this console has just armed."""
    profile = started.profile
    occupancy = started.occupancy
    return Declaration(
        kind=started.kind.value,
        operator=started.operator,
        profile_id=None if profile is None else profile.profile_id,
        profile_name=None if profile is None else profile.name,
        zone_low_bpm=None if profile is None else int(profile.zone_low_bpm),
        zone_high_bpm=None if profile is None else int(profile.zone_high_bpm),
        total_duration_s=None if profile is None else float(profile.total_duration_s),
        subject_hr_max=None if profile is None else int(profile.subject_hr_max),
        occupancy=None if occupancy is None else occupancy.value,
    )


def answer_of(result: Result[Document, CloudError]) -> Answer:
    """What an exchange with the dashboard means for what was sent.

    * an answer: **acknowledged**;
    * no answer, or a refusal that is about the link itself (the key, the
      contract, a server error, a request to wait): **held**. Nothing was
      received, nothing is dropped, and the same request is made again later;
    * 404 with no stable code: the dashboard **does not know the route**;
    * any other refusal: about the request. ``final`` when its stable code
      says the same request will always be refused.
    """
    if isinstance(result, Ok):
        return Acked(result.value)
    error = result.error
    match error:
        case Unreachable():
            return Held(error.detail)
        case Refused():
            if error.status in HELD_STATUSES or error.status >= SERVER_ERROR:
                return Held(describe_refusal(error), refused=True)
            if error.status == httpx.codes.NOT_FOUND and error.code is None:
                return RouteMissing()
            return Refusal(describe_refusal(error), final=error.code in FINAL_CODES)
    raise assert_never(error)


@dataclass(frozen=True, slots=True)
class _Awaiting:
    cloud_session_id: str
    since: Monotonic


class CloudSync:
    """The link, stepped from the console's own loop. See the module docstring."""

    __slots__ = (
        "_awaiting",
        "_clock",
        "_incompatible",
        "_last_heartbeat",
        "_last_poll",
        "_last_refused",
        "_last_status",
        "_next_profiles",
        "_online",
        "_programs_enabled",
        "_pushed_rev",
        "_record_degraded",
        "_refused_launch",
        "_runtime",
        "_said_at",
        "_software_version",
        "_store",
        "_surface",
        "_tiers",
        "_transport",
        "_uplink",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        transport: CloudTransport,
        runtime: RuntimeView,
        surface: ControlSurface,
        store: ProfileStore,
        tiers: CardiacTiers,
        programs_enabled: bool,
        software_version: SoftwareVersion = UNKNOWN_SOFTWARE_VERSION,
        record_degraded: Callable[[], bool] | None = None,
        records: RecordSource | None = None,
    ) -> None:
        """``record_degraded``: whether the local session record is incomplete or cannot
        be written, read at every heartbeat; ``None`` when this console records nothing.
        ``records``: the session records the telemetry, the events and the end of every
        session are read back from; ``None`` on a console that records nothing, which
        then only declares and ends its sessions."""
        self._clock: Clock = clock
        self._record_degraded: Callable[[], bool] | None = record_degraded
        self._transport: CloudTransport = transport
        self._runtime: RuntimeView = runtime
        self._surface: ControlSurface = surface
        self._store: ProfileStore = store
        self._tiers: CardiacTiers = tiers
        self._programs_enabled: bool = programs_enabled
        self._software_version: SoftwareVersion = software_version
        # What was last said about the dashboard's contract and its refusals,
        # so that a standing condition is not said again at every exchange.
        self._incompatible: str | None = None
        self._said_at: Monotonic | None = None
        self._refused_launch: str | None = None
        self._last_refused: Refused | None = None
        # What every session owes the dashboard is sent from its record. This
        # object keeps the state of the session that is running, and no queue.
        self._uplink: RecordUplink = RecordUplink(
            clock=clock,
            sender=self,
            source=records,
            start_refused=self._start_cancelled,
            said=surface.note_remote_refusal,
        )
        self._awaiting: _Awaiting | None = None
        self._pushed_rev: int | None = None
        self._next_profiles: Monotonic | None = None
        self._last_heartbeat: Monotonic | None = None
        self._last_poll: Monotonic | None = None
        self._last_status: Monotonic | None = None
        self._online: bool = False

    # --- read access -----------------------------------------------------

    @property
    def online(self) -> bool:
        """Whether the last exchange with the dashboard got an answer."""
        return self._online

    @property
    def current_session_id(self) -> str | None:
        """The dashboard id of the session running now, once known."""
        return self._uplink.session_id

    @property
    def owed(self) -> int:
        """Sessions the dashboard has not acknowledged whole, as far as this run knows."""
        return self._uplink.owed

    # --- what the panel calls --------------------------------------------

    def session_started(self, started: StartedSession) -> None:
        """Track a session the runtime has just armed. A launch's wait is over."""
        remote = started.cloud_session_id
        self._uplink.begin(
            ArmedSession(
                declaration=declaration_of(started),
                started_at=started.started_at,
                cloud_session_id=remote,
            )
        )
        if self._awaiting is not None and self._awaiting.cloud_session_id == remote:
            self._awaiting = None

    def start_refused(self, cloud_session_id: str, detail: str) -> None:
        """A launch the loop refused becomes a failed session on the dashboard."""
        if self._awaiting is not None and self._awaiting.cloud_session_id == cloud_session_id:
            self._awaiting = None
        self._owe_refusal(cloud_session_id, detail)

    # --- the step ----------------------------------------------------------

    async def step(self) -> None:
        """One pass: detect an end, then whatever network work is due.

        Everything but the stop asked for from the dashboard, which
        :meth:`watch_stop` looks for from a task of its own: this pass may wait
        on a slow disk or a slow upload, and a stop must never wait behind it.
        """
        now = self._clock.monotonic()
        snapshot = self._runtime.snapshot()
        self._observe()
        await self._heartbeat(now, snapshot)
        await self._push_profiles(now)
        if not self._uplink.running and self._awaiting is None and not self._uplink.refusal_owed:
            await self._poll(now)
        self._check_launch_timeout(now)
        # The sending last, so a refusal found above goes out in this same step.
        await self._uplink.step()

    def _observe(self) -> None:
        """Tell the sending that the runtime has finished the running session."""
        if not self._uplink.running or self._runtime.state is not RuntimeState.FINISHED:
            return
        reason = self._runtime.end_reason
        words = reason.value if reason is not None else "fin"
        stop = self._runtime.stop_reason
        detail = words if stop is None else f"{words}: {stop}"
        self._uplink.finished(RuntimeEnd(end_is_failure(reason), detail))

    def _owe_refusal(self, cloud_session_id: str, detail: str) -> None:
        self._uplink.owe_refusal(cloud_session_id, f"refusee par la machine : {detail}")

    async def send(self, path: str, body: Mapping[str, JsonValue]) -> Answer:
        """POST for the sending of the records, and say what the answer means for it."""
        sent = await self._transport.post(path, body)
        self._note(sent)
        return answer_of(sent)

    # --- heartbeat and presets --------------------------------------------

    async def _heartbeat(self, now: Monotonic, snapshot: TelemetrySnapshot) -> None:
        if not _due(self._last_heartbeat, now, HEARTBEAT_PERIOD):
            return
        self._last_heartbeat = now
        body: dict[str, JsonValue] = {
            "live": live_row(snapshot, self.current_session_id),
            "programsEnabled": self._programs_enabled,
            "software_version": self._software_version,
            "contract_version": CONTRACT_VERSION,
            # This console has neither yet: said to be absent, not left out.
            "medical_parameters_version": None,
            "config_hash": None,
        }
        session = self.current_session_id
        if session is not None:
            body["activeSessionId"] = session
        if self._record_degraded is not None:
            # ``record_degraded`` in the wire's own spelling. Top level, never
            # inside ``live``: that object is validated field by field.
            body["recordDegraded"] = self._record_degraded()
        sent = await self._transport.post("/api/machine/heartbeat", body)
        self._note(sent)

    async def _push_profiles(self, now: Monotonic) -> None:
        rev = int(self._store.rev)
        if rev == self._pushed_rev:
            return
        if self._next_profiles is not None and now < self._next_profiles:
            return
        rows: list[JsonValue] = [
            profile_row(p) for p in runnable_profiles(self._store.list_profiles(), self._tiers)
        ]
        sent = await self._transport.post(
            "/api/machine/profiles",
            {"storeRev": rev, "programsEnabled": self._programs_enabled, "profiles": rows},
        )
        self._note(sent)
        if isinstance(sent, Ok):
            self._pushed_rev = rev
            self._next_profiles = None
        else:
            self._next_profiles = Monotonic(now + RETRY_PERIOD)

    # --- launches from the dashboard ---------------------------------------

    async def _poll(self, now: Monotonic) -> None:
        if not self._programs_enabled or self._runtime.state in (
            RuntimeState.RUNNING,
            RuntimeState.ENDING,
        ):
            return
        if not _due(self._last_poll, now, POLL_PERIOD):
            return
        self._last_poll = now
        polled = await self._transport.get("/api/machine/training/poll")
        self._note(polled)
        if isinstance(polled, Err):
            return
        launch = _launch_of(polled.value)
        refusal = server_refusal(polled.value.get(SERVER_VERSION_FIELD))
        if refusal is not None:
            # An answer of another major arms nothing here, whatever it carries.
            self._refuse_server(refusal, launch)
            return
        self._incompatible = None
        if launch is None:
            return
        submitted = self._surface.submit_start(
            profile_id=launch.profile_id,
            operator=f"{launch.operator} ({DASHBOARD_OPERATOR})",
            total_duration_s=launch.total_duration_s,
            subject_id=launch.subject_id,
            subject_hr_max=launch.subject_hr_max,
            subject_age=launch.subject_age,
            cloud_session_id=launch.session_id,
        )
        if isinstance(submitted, Err):
            self._owe_refusal(launch.session_id, describe_surface_refusal(submitted.error))
            return
        _logger.info("dashboard launch %s submitted", launch.session_id)
        self._awaiting = _Awaiting(launch.session_id, now)

    def _refuse_server(self, sentence: str, launch: Launch | None) -> None:
        """The dashboard is of another contract: say so on the console, and fail its launch.

        Said for each launch refused, when the sentence changes, and otherwise
        every :data:`INCOMPATIBLE_REPEAT`: the condition lasts until somebody
        updates one side, and must not flood the event list meanwhile.
        """
        now = self._clock.monotonic()
        if launch is not None and launch.session_id != self._refused_launch:
            self._refused_launch = launch.session_id
            _logger.warning("dashboard launch %s refused: %s", launch.session_id, sentence)
            self._say_incompatible(sentence, now)
            self._owe_refusal(launch.session_id, sentence)
        elif sentence != self._incompatible or _due(self._said_at, now, INCOMPATIBLE_REPEAT):
            self._say_incompatible(sentence, now)

    def _say_incompatible(self, sentence: str, now: Monotonic) -> None:
        if sentence != self._incompatible:
            _logger.error("dashboard: %s", sentence)  # logged when it changes, not at each reminder
        self._incompatible = sentence
        self._said_at = now
        self._surface.note_remote_refusal(sentence)

    def _check_launch_timeout(self, now: Monotonic) -> None:
        awaiting = self._awaiting
        if awaiting is None or elapsed(awaiting.since, now) < LAUNCH_TIMEOUT:
            return
        self._awaiting = None
        self._owe_refusal(awaiting.cloud_session_id, "la boucle n'a ni demarre ni refuse")

    # --- a stop from the dashboard ------------------------------------------

    async def watch_stop(self) -> None:
        """Ask, when it is due, whether the dashboard wants the running session stopped.

        Stepped from a task of its own, never from :meth:`step`: the question
        keeps its cadence (:data:`STATUS_PERIOD`) whatever the sending is
        waiting for, a slow disk, a slow upload or a backlog to catch up.
        """
        now = self._clock.monotonic()
        session_id = self._uplink.following
        if session_id is None or not _due(self._last_status, now, STATUS_PERIOD):
            return
        self._last_status = now
        sent = await self._transport.get("/api/machine/training/status", {"sessionId": session_id})
        self._note(sent)
        if isinstance(sent, Err):
            return
        if self._uplink.following != session_id:
            # That session ended, or was stopped, while the dashboard answered:
            # what it says of it stops no other.
            return
        document = sent.value
        # Deliberately not gated on the answer's contract version: a stop asked
        # for, or a session the dashboard no longer holds active, can only end
        # this session on the ordinary ramp, and that is the safe side under
        # any contract. These two fields are all that is read here.
        if document.get("stopRequested") is True or document.get("active") is False:
            self._forward_stop("arret demande depuis le tableau de bord")

    def _start_cancelled(self, detail: str) -> None:
        """The dashboard did not take the start of the running session. End it.

        Cancelled there between the poll and the arm, or refused for the
        link's own reasons (contract, key, an error of the server): either
        way the machine is armed for a session the dashboard does not hold
        started, and nobody there could stop it.
        """
        _logger.warning("dashboard refused the start (%s): stopping", detail)
        self._forward_stop(f"annulee au tableau de bord ({detail})")

    def _forward_stop(self, reason: str) -> None:
        # Kept with the session it is about: asked once, and for that one only.
        self._uplink.stop_forwarded()
        ended = self._surface.submit_end(operator=DASHBOARD_OPERATOR, reason=reason)
        if isinstance(ended, Err):
            _logger.warning("dashboard stop not accepted by the console: %s", ended.error)

    def _note(self, result: Result[Document, CloudError]) -> None:
        was = self._online
        self._online = True
        if isinstance(result, Err):
            match result.error:
                case Unreachable(detail=detail):
                    self._online = False
                    if was:
                        _logger.warning("dashboard unreachable: %s", detail)
                case Refused() as refused:
                    # An answer, even a refusal, is a working link.
                    self._hear_refusal(refused)
                case _ as unreachable:
                    assert_never(unreachable)
        if self._online and not was:
            _logger.info("dashboard reachable")

    def _hear_refusal(self, refused: Refused) -> None:
        """Log a refusal by its stable code, once; a refused contract goes to the console."""
        if refused != self._last_refused:
            self._last_refused = refused
            _logger.warning("dashboard refused a request: %s", describe_refusal(refused))
        if refused.code == CONTRACT_UNSUPPORTED:
            self._refuse_server(unsupported_refusal(refused.supported), None)


# =========================================================================
# Parsing a launch
# =========================================================================


@dataclass(frozen=True, slots=True)
class Launch:
    """An AUTO session waiting on the dashboard for this machine."""

    session_id: str
    profile_id: str
    total_duration_s: Seconds | None
    subject_id: str
    subject_hr_max: Bpm
    operator: str
    subject_age: int | None
    """From the rider's birth year on the dashboard; ``None`` if unknown (the panel refuses)."""


def _launch_of(document: Document) -> Launch | None:
    """The launch in a ``/api/machine/training/poll`` answer; ``None`` if absent or malformed."""
    raw = document.get("session")
    if not isinstance(raw, dict):
        return None
    session = cast("Document", raw)
    session_id = session.get("sessionId")
    profile_id = session.get("profileId")
    subject_id = session.get("subjectId")
    hr_max = session.get("subjectHrMax")
    operator = session.get("operatorName")
    total = session.get("totalDurationS")
    age = session.get("subjectAge")
    if (
        not isinstance(session_id, str)
        or not isinstance(profile_id, str)
        or not isinstance(subject_id, str)
        or not isinstance(hr_max, int | float)
        or isinstance(hr_max, bool)
        or not isinstance(operator, str)
    ):
        _logger.warning("dashboard: malformed launch ignored")
        return None
    duration = (
        Seconds(float(total))
        if isinstance(total, int | float) and not isinstance(total, bool)
        else None
    )
    return Launch(
        session_id=session_id,
        profile_id=profile_id,
        total_duration_s=duration,
        subject_id=subject_id,
        subject_hr_max=Bpm(round(hr_max)),
        operator=operator or DASHBOARD_OPERATOR,
        subject_age=age if isinstance(age, int) and not isinstance(age, bool) else None,
    )


def _due(last: Monotonic | None, now: Monotonic, period: Seconds) -> bool:
    return last is None or elapsed(last, now) >= period
