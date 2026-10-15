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

Down, from the dashboard to this machine, exactly three things:

* an AUTO launch: a preset id, a rider and the rider's maximum heart rate. It
  first passes the same surface and panel checks as a local start, without
  submitting or arming. Only then does this task send ``training/start``;
  ``submit_start`` and the panel re-run every gate after a 2xx before the
  runtime is asked to arm. A local refusal goes back as a failed session with
  its reason. A refusal by the dashboard is shown on the console and is not
  ended again; a missing route or an unavailable link leaves the machine
  unarmed so a later poll can offer the launch again;
* the list of the riders this machine may offer (:mod:`src.roster`), asked
  for at the start and every :data:`~src.roster.ROSTER_PERIOD` while no
  session runs, kept in memory only. It starts nothing: a start at the
  console names one of them and goes through every gate as before, the
  rider's maximum heart rate and age from that list;
* a stop request, forwarded as an ordinary end on the commissioned ramp. It
  is looked for by :meth:`CloudSync.watch_stop`, from a task of its own: the
  question keeps its cadence whatever the sending waits for. A session armed
  by an older console or before this handshake is still stopped at once when
  its start is refused or no longer active on the dashboard.

**Nothing else.** In particular a MANUAL session can never be started from
the dashboard: nothing here can build one. Manual control needs somebody at
the machine.

Why the link can never stop the machine by failing
--------------------------------------------------
Every call returns a :class:`~src.result.Result`; a dead network is
``Err(Unreachable)``, logged and retried later. A remote launch stays unarmed
until ``training/start`` answers 2xx; a running session continues under the
local supervisor when its link is lost. The worst a lost link does is leave
the dashboard's picture stale, which the dashboard shows as stale. Nothing
waits for the link in memory: what it owes the dashboard is in the session
records, and is sent when the link answers again, after a restart of the
console too (:func:`answer_of` says what each answer does to the sending).

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

What the operator reads of the link
-----------------------------------
Every exchange above is also told to a :class:`~src.link_state.LinkHealth`
(:meth:`CloudSync._note`), which the console's page reads as one standing
state: reachable, unreachable, incompatible, key refused, in error. No request
is made for it: it is what the exchanges the link makes anyway have last said.
It reads reachable only on an answer in the dashboard's own shape that took
what was sent (a success, or a refusal under one of its stable codes), on a
route that carries the heartbeat or the session. The stop question's answers
say the dashboard is there and nothing more.

The dashboard may ask the console to wait
-----------------------------------------
A route the console calls more often than the dashboard takes (contract 1.4)
is answered 429 ``rate_limited`` with the wait in ``retryAfterMs``.
:class:`PacedTransport` keeps to it: nothing more is sent on that route until
the wait is over, whoever asks and however often, and the wait is logged. A
console that works as designed never meets these limits: its cadences are
pinned to them by its tests. The stop question is never held.

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

import asyncio
import json
import logging
import math
from abc import abstractmethod
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, unique
from http import HTTPStatus
from types import MappingProxyType
from typing import Final, Protocol, assert_never, cast

import httpx

from src.clock import Clock
from src.contract import (
    ARMING_REFUSALS,
    CONTRACT_HEADER,
    CONTRACT_UNSUPPORTED,
    CONTRACT_VERSION,
    RATE_LIMITED,
    SERVER_VERSION_FIELD,
    STABLE_CODES,
    UNKNOWN_SOFTWARE_VERSION,
    ErrorCode,
    SoftwareVersion,
    error_code_of,
    majors_of,
    server_refusal,
    unsupported_refusal,
)
from src.control_surface import (
    LOCAL_SUBJECT,
    ControlSurface,
    SafetyHolding,
    StartRefusal,
    SurfaceBusy,
)
from src.health import HealthContext, HealthWatch
from src.link_state import NOT_THE_DASHBOARD, LinkHealth, LinkStatus, bounded
from src.local_config import CardiacTiers, CloudConfig
from src.medical import MedicalStatus
from src.record_uplink import (
    START_PATH,
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
from src.remote_launch import (
    ConfirmationCode,
    ConfirmationRefusal,
    LaunchStage,
    RemoteLaunchView,
)
from src.result import Err, Ok, Result
from src.roster import (
    ROSTER_PATH,
    NoList,
    PassengerRefusal,
    Rider,
    Roster,
    RosterState,
    describe_passenger_refusal,
)
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

TOO_MANY_REQUESTS: Final[int] = 429
"""The status of a request the dashboard asks to wait for (``rate_limited``)."""

HELD_STATUSES: Final[frozenset[int]] = frozenset({401, 403, 408, 425, 426, TOO_MANY_REQUESTS})
"""Refusals that are about the link, not about what was sent: the key is not accepted,
the contract is not served, the server asks to wait. A 5xx is one too."""

MAX_RATE_WAIT: Final[Seconds] = Seconds(300.0)
"""The longest a 429 keeps the console from a route: a longer ``retryAfterMs`` is read as
this. The longest back-off of the deposit too; the dashboard's own waits are seconds."""

RATE_WAIT_UNSAID: Final[Seconds] = RETRY_PERIOD
"""How long a 429 that names no readable wait keeps the console from its route."""

STATUS_PATH: Final[str] = "/api/machine/training/status"
"""The stop question's route: never held by :class:`PacedTransport`."""

HELD_HERE: Final[str] = "not sent: the dashboard asked this console to wait on this route"
"""The words of a refusal :class:`PacedTransport` gives without asking the dashboard."""

SERVER_ERROR: Final[int] = 500

KEY_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
"""Among those, the refusals that say this machine's key is not accepted."""

FINAL_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_request",
        "session_not_found",
        "session_not_pending",
        "machine_not_found",
        "payload_too_large",
    }
)
"""Stable codes that say the same request will always be refused
(``contracts/machine-api.json``). ``request_failed`` is not one: it also covers a
failure of the moment."""

DASHBOARD_OPERATOR: Final[str] = "tableau de bord"
"""Who a stop from the dashboard is attributed to in the console's event list."""

ARMING_REFUSAL_WORDS: Final[Mapping[ErrorCode, str]] = MappingProxyType(
    {
        ErrorCode("expired_before_start"): "lancement expire : depart non confirme a temps",
        ErrorCode("machine_deleted_before_start"): "machine supprimee avant le depart",
        ErrorCode("organization_changed_before_start"): (
            "machine passee dans une autre organisation avant le depart"
        ),
        ErrorCode("right_revoked_before_start"): "droit retire avant le depart",
        ErrorCode("age_refused_before_start"): "age du pratiquant refuse avant le depart",
        ErrorCode("hr_max_changed_before_start"): "FC max du pratiquant modifiee avant le depart",
        ErrorCode("rider_busy"): (
            "pratiquant deja dans une autre seance en attente ou en cours : une seule a la fois"
        ),
    }
)


# =========================================================================
# The transport
# =========================================================================


@dataclass(frozen=True, slots=True)
class Unreachable:
    """No answer: no network, a timeout, or a reply that is not JSON."""

    detail: str
    status: int | None = None
    """The HTTP status of a reply that was no answer (a page of HTML, say); ``None``
    when nothing came back at all."""


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

    retry_after: Seconds | None = field(default=None, compare=False)
    """With ``rate_limited``: how long the dashboard asks to wait (``retryAfterMs``), at
    most :data:`MAX_RATE_WAIT`; ``None`` when it said nothing readable. Not part of what
    makes two refusals the same one: the wait is different at each."""


type CloudError = Unreachable | Refused
type Document = Mapping[str, object]
UPLOAD_CHUNK_BYTES: Final[int] = 16 * 1024
UPLOAD_BYTES_PER_SECOND: Final[int] = 256 * 1024
MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024


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


class DepositTask(Protocol):
    @abstractmethod
    def pause(self) -> None:
        """Cancel transfer work when a session starts, without waiting for it."""

    @abstractmethod
    def schedule(self) -> None:
        """Schedule idle transfer work when its retry delay has elapsed."""

    @abstractmethod
    async def close(self) -> None:
        """Cancel and await the owned transfer task before closing its transport."""


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

    __slots__ = ("_client", "_storage_origin")

    def __init__(self, config: CloudConfig, *, client: httpx.AsyncClient | None = None) -> None:
        origin = httpx.URL(config.url)
        host = origin.host
        if host.endswith(".convex.site"):
            host = host.removesuffix(".convex.site") + ".convex.cloud"
        self._storage_origin: tuple[str, str, int | None] = (origin.scheme, host, origin.port)
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
        request = self._client.build_request("GET", path, params=dict(params or {}))
        return await self._send(request)

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        request = self._client.build_request("POST", path, content=json.dumps(body))
        return await self._send(request)

    async def _send(self, request: httpx.Request) -> Result[Document, CloudError]:
        request.headers["Accept-Encoding"] = "identity"
        try:
            response = await self._client.send(request, stream=True)
            try:
                return await _bounded_answer(response)
            finally:
                await response.aclose()
        except httpx.HTTPError as error:
            return Err(Unreachable(f"{request.method} {request.url.path}: {type(error).__name__}"))

    async def close(self) -> None:
        """Release the connection pool."""
        await self._client.aclose()

    async def upload(
        self, url: str, content_type: str, content: bytes
    ) -> Result[Document, CloudError]:
        try:
            target = httpx.URL(url)
        except httpx.InvalidURL:
            return Err(Unreachable("Invalid storage URL"))
        if (target.scheme, target.host, target.port) != self._storage_origin:
            return Err(Unreachable("Storage origin mismatch"))

        async def paced() -> AsyncIterator[bytes]:
            for offset in range(0, len(content), UPLOAD_CHUNK_BYTES):
                chunk = content[offset : offset + UPLOAD_CHUNK_BYTES]
                yield chunk
                await asyncio.sleep(len(chunk) / UPLOAD_BYTES_PER_SECOND)

        request = httpx.Request(
            "POST",
            target,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
            },
            content=paced(),
        )
        return await self._send(request)


async def _bounded_answer(response: httpx.Response) -> Result[Document, CloudError]:
    encoding = (
        response.headers["Content-Encoding"].strip().lower()
        if "Content-Encoding" in response.headers
        else "identity"
    )
    if encoding != "identity":
        return Err(Unreachable("Unsupported response encoding", response.status_code))
    if response.is_stream_consumed:
        if len(response.content) > MAX_RESPONSE_BYTES:
            return Err(Unreachable("Response exceeds byte limit", response.status_code))
        return _answer(response)
    body = bytearray()
    async for chunk in response.aiter_raw(chunk_size=UPLOAD_CHUNK_BYTES):
        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            return Err(Unreachable("Response exceeds byte limit", response.status_code))
        body.extend(chunk)
    return _answer(
        httpx.Response(response.status_code, content=bytes(body), headers=response.headers)
    )


def _answer(response: httpx.Response) -> Result[Document, CloudError]:
    document = _parse_document(response.text)
    if response.status_code >= httpx.codes.BAD_REQUEST:
        return Err(_refusal(response.status_code, document))
    if document is None:
        return Err(
            Unreachable(f"HTTP {response.status_code}: not a JSON object", response.status_code)
        )
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
        retry_after=retry_after_of(document.get("retryAfterMs")),
    )


def retry_after_of(value: object) -> Seconds | None:
    """The wait in an untrusted ``retryAfterMs``; ``None`` unless a number of ms, 0 or more.

    Bounded by :data:`MAX_RATE_WAIT`, whatever was sent.
    """
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        return None
    return Seconds(min(value / 1000, MAX_RATE_WAIT))


class PacedTransport:
    """A :class:`CloudTransport` that keeps to the waits the dashboard asks for (contract 1.4).

    A 429 holds its route until the wait it names is over (``retryAfterMs``,
    at most :data:`MAX_RATE_WAIT`; :data:`RATE_WAIT_UNSAID` when it names
    none): a request to that route meanwhile is not sent, and is answered here
    as the dashboard answered it. Whoever asks (the heartbeat, the poll, the
    sending of the records, the deposit) and however often, nothing reaches
    the dashboard on that route before the wait is over: there is no tight
    loop to make. Every hold is logged once, when it begins, with its route
    and its length. The other routes are not held, and the stop question
    never is: the dashboard never limits it, and a stop is never the side
    that waits. Never raises.
    """

    __slots__ = ("_clock", "_held", "_inner")

    def __init__(self, inner: CloudTransport, clock: Clock) -> None:
        self._inner: CloudTransport = inner
        self._clock: Clock = clock
        # Route -> when it may be asked again. Mutable on purpose, and only by
        # this object's own calls, all on the event loop. At most one entry
        # per route of the machine API: a hold that is over is overwritten by
        # the next, never needed again otherwise.
        self._held: dict[str, Monotonic] = {}

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        waiting = self._waiting(path)
        if waiting is not None:
            return Err(waiting)
        return self._heard(path, await self._inner.get(path, params))

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        waiting = self._waiting(path)
        if waiting is not None:
            return Err(waiting)
        return self._heard(path, await self._inner.post(path, body))

    def _waiting(self, path: str) -> Refused | None:
        """The refusal a held route is answered with, without asking; ``None`` when it is free."""
        until = self._held.get(path)
        now = self._clock.monotonic()
        if until is None or now >= until:
            return None
        return Refused(
            status=TOO_MANY_REQUESTS,
            detail=HELD_HERE,
            code=RATE_LIMITED,
            retry_after=Seconds(until - now),
        )

    def _heard(
        self, path: str, result: Result[Document, CloudError]
    ) -> Result[Document, CloudError]:
        """Hold ``path`` when the dashboard answered it 429. The answer goes on unchanged."""
        if path == STATUS_PATH or isinstance(result, Ok):
            return result
        refused = result.error
        if not isinstance(refused, Refused) or refused.status != TOO_MANY_REQUESTS:
            return result
        wait = RATE_WAIT_UNSAID if refused.retry_after is None else refused.retry_after
        self._held[path] = Monotonic(self._clock.monotonic() + wait)
        _logger.warning(
            "dashboard: %s refused for its rate (HTTP 429, %s): not asked again for %.1f s",
            path,
            "sans code" if refused.code is None else refused.code,
            wait,
        )
        return result


LOGGED_WORDS_LIMIT: Final[int] = 200
"""Most characters the dashboard's own words take in the log, quotes and escapes included."""


def logged_words(words: str) -> str:
    """The dashboard's own words as the log writes them: quoted, on one line, and bounded.

    The same form as a peer's header in the log of the page's socket
    (:func:`~src.web.ws.logged_header`): line breaks replaced first, then
    quoted and escaped the way ``%r`` would write it, and that written form
    cut to :data:`LOGGED_WORDS_LIMIT` characters, the cut stated with the
    length received.
    """
    single = words.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    written = repr(single)
    if len(written) <= LOGGED_WORDS_LIMIT:
        return written
    return f"{written[:LOGGED_WORDS_LIMIT]}... ({len(single)} characters received)"


def describe_refusal(refused: Refused) -> str:
    """A refusal in a few words: its stable code first, then the status and the words.

    The words are the dashboard's: they are kept on one line and bounded
    (:func:`~src.link_state.bounded`), since this goes into other sentences,
    the reason of a stop among them.
    """
    code = "sans code" if refused.code is None else refused.code
    return f"{code} (HTTP {refused.status}): {bounded(refused.detail)}"


def _remote_start_refusal(refused: Refusal) -> str:
    code = refused.code
    if refused.status == HTTPStatus.CONFLICT and code is not None and code in ARMING_REFUSALS:
        return f"{ARMING_REFUSAL_WORDS[code]} ({code})"
    named_code = "" if code is None else f", {code}"
    return f"depart distant refuse (HTTP {refused.status}{named_code})"


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
    """The rider's dashboard identifier; :data:`~src.control_surface.LOCAL_SUBJECT`
    for a session that names nobody (a bench session)."""

    cloud_session_id: str | None
    """The dashboard launch this answers; ``None`` for a start at the machine."""

    profile: TrainingProfile | None = None
    """The programme actually run (already fitted to the rider); ``None`` for MANUAL."""

    occupancy: Occupancy | None = None
    """Declared for a MANUAL session; ``None`` for AUTO (always occupied)."""

    subject_label: str | None = None
    """The rider's name as the list gave it, for the dashboard; memory only, never a disk."""


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
    """What the dashboard is told of a session this console has just armed.

    The rider, when one is named: the dashboard's identifier, and the name the
    list gave, which only the dashboard keeps. A bench session names nobody.
    """
    profile = started.profile
    occupancy = started.occupancy
    named = started.subject_id not in ("", LOCAL_SUBJECT)
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
        user_id=started.subject_id if named else None,
        subject_label=started.subject_label if named else None,
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
            return Refusal(
                describe_refusal(error),
                final=error.code in FINAL_CODES,
                status=error.status,
                code=error.code,
            )
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
        "_confirmation_operator",
        "_confirmed_launch_id",
        "_deposit",
        "_health",
        "_health_context",
        "_incidents_open_high",
        "_incompatible",
        "_last_heartbeat",
        "_last_poll",
        "_last_refused",
        "_last_rtt",
        "_last_status",
        "_link",
        "_medical",
        "_next_profiles",
        "_offer_reason",
        "_offer_stage",
        "_offered_launch",
        "_online",
        "_preflight_start",
        "_programs_enabled",
        "_pushed_rev",
        "_record_degraded",
        "_refused_launch",
        "_roster",
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
        medical: MedicalStatus | None = None,
        preflight_start: Callable[[Launch], Result[None, str]] | None = None,
        software_version: SoftwareVersion = UNKNOWN_SOFTWARE_VERSION,
        record_degraded: Callable[[], bool] | None = None,
        records: RecordSource | None = None,
        link: LinkHealth | None = None,
        health: HealthWatch | None = None,
        health_context: Callable[[], HealthContext] = HealthContext,
        deposit: DepositTask | None = None,
        roster: Roster | None = None,
    ) -> None:
        """``record_degraded``: whether the local session record is incomplete or cannot
        be written, read at every heartbeat; ``None`` when this console records nothing.
        ``records``: the session records the telemetry, the events and the end of every
        session are read back from; ``None`` on a console that records nothing, which
        then only declares and ends its sessions. ``link``: where what each exchange
        says of the link is kept for the console's page; ``None``: one of this object's
        own. ``roster``: the riders' list this link keeps up to date; ``None``: no list
        is asked for. ``medical``: the medical parameters and their verdict, whose
        version the heartbeat announces when signed; ``None``: the heartbeat says there
        is none."""
        self._clock: Clock = clock
        self._deposit: DepositTask | None = deposit
        self._roster: Roster | None = roster
        self._health: HealthWatch | None = health
        self._health_context: Callable[[], HealthContext] = health_context
        self._last_rtt: Seconds | None = None
        self._incidents_open_high: int | None = None
        self._link: LinkHealth = LinkHealth(clock.monotonic()) if link is None else link
        self._record_degraded: Callable[[], bool] | None = record_degraded
        self._transport: CloudTransport = transport
        self._runtime: RuntimeView = runtime
        self._surface: ControlSurface = surface
        self._preflight_start: Callable[[Launch], Result[None, str]] | None = preflight_start
        self._store: ProfileStore = store
        self._tiers: CardiacTiers = tiers
        self._programs_enabled: bool = programs_enabled
        self._medical: MedicalStatus | None = medical
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
        self._confirmed_launch_id: str | None = None
        self._confirmation_operator: str | None = None
        self._offered_launch: Launch | None = None
        self._offer_stage: LaunchStage = LaunchStage.WAITING_CONFIRMATION
        self._offer_reason: str | None = None
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
    def link(self) -> LinkStatus:
        """What the operator reads of the link now (:mod:`src.link_state`)."""
        return self._link.status(self._clock.monotonic())

    @property
    def current_session_id(self) -> str | None:
        """The dashboard id of the session running now, once known."""
        return self._uplink.session_id

    @property
    def owed(self) -> int:
        """Sessions the dashboard has not acknowledged whole, as far as this run knows."""
        return self._uplink.owed

    def current_remote_launch(self) -> RemoteLaunchView | None:
        launch = self._offered_launch
        if launch is None:
            return None
        found = self._rider_for_launch(launch)
        if isinstance(found, Ok):
            passenger_name = found.value.name
            passenger_reason = None
        else:
            passenger_name = None
            passenger_reason = describe_passenger_refusal(found.error)
        profile = self._store.get(launch.profile_id)
        program_name = profile.value.name if isinstance(profile, Ok) else launch.profile_id
        return RemoteLaunchView(
            session_id=launch.session_id,
            profile_id=launch.profile_id,
            program_name=program_name,
            launched_by=launch.operator,
            passenger_name=passenger_name,
            passenger_reason=passenger_reason,
            stage=self._offer_stage,
            reason=self._offer_reason,
        )

    def confirm_remote_launch(
        self, session_id: str, operator: str
    ) -> Result[None, ConfirmationRefusal]:
        launch = self._offered_launch
        if launch is None or launch.session_id != session_id:
            return Err(
                ConfirmationRefusal(
                    ConfirmationCode.NOT_PENDING,
                    "ce lancement n'est plus propose par le tableau de bord",
                )
            )
        if (
            self._offer_stage is LaunchStage.REQUESTING
            or self._awaiting is not None
            or self._runtime.state in (RuntimeState.RUNNING, RuntimeState.ENDING)
            or self._confirmation_operator is not None
        ):
            return Err(
                ConfirmationRefusal(
                    ConfirmationCode.IN_PROGRESS,
                    "une demande de depart est deja en cours",
                )
            )
        found = self._rider_for_launch(launch)
        if isinstance(found, Err):
            detail = describe_passenger_refusal(found.error)
            self._offer_stage = LaunchStage.REFUSED
            self._offer_reason = detail
            return Err(ConfirmationRefusal(ConfirmationCode.PASSENGER_UNAVAILABLE, detail))
        self._confirmation_operator = operator
        self._offer_stage = LaunchStage.REQUESTING
        self._offer_reason = None
        return Ok(None)

    def _rider_for_launch(self, launch: Launch) -> Result[Rider, PassengerRefusal]:
        roster = self._roster
        if roster is None:
            return Err(NoList(RosterState.UNLINKED, None))
        return roster.find(launch.subject_id, self._clock.monotonic())

    # --- what the panel calls --------------------------------------------

    def session_started(self, started: StartedSession) -> None:
        """Track a session the runtime has just armed. A launch's wait is over."""
        self._clear_offer()
        remote = started.cloud_session_id
        confirmed = remote is not None and remote == self._confirmed_launch_id
        if confirmed:
            self._confirmed_launch_id = None
        # The first question about its stop is asked as soon as it may be,
        # whenever the last one about the session before was.
        self._last_status = None
        if self._deposit is not None:
            self._deposit.pause()
        self._uplink.begin(
            ArmedSession(
                declaration=declaration_of(started),
                started_at=started.started_at,
                cloud_session_id=remote,
                start_confirmed=confirmed,
            )
        )
        if self._awaiting is not None and self._awaiting.cloud_session_id == remote:
            self._awaiting = None

    def start_refused(self, cloud_session_id: str, detail: str) -> None:
        """A launch the loop refused becomes a failed session on the dashboard."""
        launch = self._offered_launch
        if launch is not None and launch.session_id == cloud_session_id:
            self._offer_stage = LaunchStage.REFUSED
            self._offer_reason = _safe_launch_refusal(launch, detail)
        if self._confirmed_launch_id == cloud_session_id:
            self._confirmed_launch_id = None
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
        await self._refresh_roster(now)
        if (
            not self._uplink.running
            and self._runtime.state not in (RuntimeState.RUNNING, RuntimeState.ENDING)
            and (self._offered_launch is not None or not self._uplink.refusal_owed)
        ):
            await self._poll(now)
            await self._process_confirmation(now)
        self._check_launch_timeout(now)
        # The sending last, so a refusal found above goes out in this same step.
        await self._uplink.step()
        if self._deposit is not None:
            if self._runtime.state in (RuntimeState.RUNNING, RuntimeState.ENDING) or self._awaiting:
                self._deposit.pause()
            else:
                self._deposit.schedule()

    async def close(self) -> None:
        if self._deposit is not None:
            await self._deposit.close()

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
        self._note(sent, log_refusal_words=path != START_PATH)
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
            # The signed version, or null: an unsigned file vouches for no version.
            "medical_parameters_version": (
                None if self._medical is None else self._medical.announced_version
            ),
            # This console has none yet: said to be absent, not left out.
            "config_hash": None,
        }
        session = self.current_session_id
        if session is not None:
            body["activeSessionId"] = session
        if self._record_degraded is not None:
            # ``record_degraded`` in the wire's own spelling. Top level, never
            # inside ``live``: that object is validated field by field.
            body["recordDegraded"] = self._record_degraded()
        if self._health is not None:
            body["health"] = dict(
                await self._health.sample(
                    snapshot,
                    self._health_context(),
                    self._last_rtt,
                    self._incidents_open_high,
                )
            )
        began = self._clock.monotonic()
        sent = await self._transport.post("/api/machine/heartbeat", body)
        match sent:
            case Ok(document):
                refusal = server_refusal(document.get(SERVER_VERSION_FIELD))
                self._link.heartbeat_answered(refusal)
                if refusal is not None:
                    self._refuse_server(refusal, None)
            case Err(error):
                match error:
                    case Unreachable():
                        self._link.heartbeat_failed(error.detail)
                    case Refused():
                        self._link.heartbeat_failed(f"HTTP {error.status}")
                    case _ as unreachable:
                        assert_never(unreachable)
            case _ as unreachable:
                assert_never(unreachable)
        self._last_rtt = elapsed(began, self._clock.monotonic()) if isinstance(sent, Ok) else None
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

    # --- the riders' list --------------------------------------------------

    async def _refresh_roster(self, now: Monotonic) -> None:
        """Ask for the riders' list: at the start, then every period, never during a session.

        An answer replaces the list held, whole. No answer keeps it, to age
        (:data:`~src.roster.ROSTER_MAX_AGE`). A refused key empties it: a
        machine the dashboard no longer recognises offers nobody.
        """
        roster = self._roster
        if roster is None or not roster.due(now):
            return
        if self._runtime.state in (RuntimeState.RUNNING, RuntimeState.ENDING):
            return
        fetched = await self._transport.get(ROSTER_PATH)
        self._hear_roster(roster, fetched, now)
        self._note(fetched)

    @staticmethod
    def _hear_roster(roster: Roster, fetched: Result[Document, CloudError], now: Monotonic) -> None:
        """What one answer, or its absence, does to the list held."""
        match fetched:
            case Ok(document):
                roster.heard(document, now)
            case Err(error):
                match error:
                    case Unreachable():
                        roster.missed(f"tableau de bord injoignable : {error.detail}", now)
                    case Refused():
                        refusal = describe_refusal(error)
                        if error.status in KEY_STATUSES:
                            roster.refused(refusal, now)
                        else:
                            roster.missed(refusal, now)
                    case _ as unreachable:
                        assert_never(unreachable)
            case _ as unreachable:
                assert_never(unreachable)

    # --- launches from the dashboard ---------------------------------------

    async def _poll(self, now: Monotonic) -> None:
        # In the bench mode programmes are off, and a launch is still asked for:
        # the panel refuses it, saying the medical parameters are not signed.
        bench_mode = self._medical is not None and not self._medical.signed
        if (not self._programs_enabled and not bench_mode) or self._runtime.state in (
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
        self._link.announced(refusal)
        if refusal is not None:
            self._clear_offer()
            self._refuse_server(refusal, launch)
            return
        self._incompatible = None
        incidents = polled.value.get("incidents_open_high")
        self._incidents_open_high = (
            incidents
            if isinstance(incidents, int) and not isinstance(incidents, bool) and incidents >= 0
            else None
        )
        if launch is None:
            self._clear_offer()
            return
        awaiting = self._awaiting
        if awaiting is not None and launch.session_id != awaiting.cloud_session_id:
            self._clear_offer()
            return
        self._offer_launch(launch)

    def _offer_launch(self, launch: Launch) -> None:
        previous = self._offered_launch
        if previous is not None and previous.session_id == launch.session_id:
            self._offered_launch = launch
            return
        self._offered_launch = launch
        self._confirmation_operator = None
        self._offer_stage = LaunchStage.WAITING_CONFIRMATION
        self._offer_reason = None

    def _clear_offer(self) -> None:
        self._offered_launch = None
        self._confirmation_operator = None
        self._offer_stage = LaunchStage.WAITING_CONFIRMATION
        self._offer_reason = None

    async def _process_confirmation(self, now: Monotonic) -> None:
        operator = self._confirmation_operator
        launch = self._offered_launch
        if operator is None or launch is None:
            return
        self._confirmation_operator = None
        if self._awaiting is not None or self._runtime.state in (
            RuntimeState.RUNNING,
            RuntimeState.ENDING,
        ):
            self._offer_stage = LaunchStage.REFUSED
            self._offer_reason = "une seance est deja en cours"
            return
        admitted = self._preflight_remote_start(launch)
        if isinstance(admitted, Err):
            self._refuse_local_launch(launch, admitted.error)
            return
        await self._confirm_remote_start(launch, operator, now)

    def _preflight_remote_start(self, launch: Launch) -> Result[None, str]:
        surface_check = self._surface.check_start()
        if isinstance(surface_check, Err):
            return Err(describe_surface_refusal(surface_check.error))
        preflight = self._preflight_start
        if preflight is None:
            return Err("verification locale indisponible")
        return preflight(launch)

    async def _confirm_remote_start(self, launch: Launch, operator: str, now: Monotonic) -> None:
        started_at = self._clock.unix_millis()
        answer = await self.send(
            START_PATH,
            {
                "sessionId": launch.session_id,
                "startedAt": int(started_at),
                "sessionAgeMs": 0,
            },
        )
        match answer:
            case Acked():
                self._submit_confirmed_launch(launch, operator, now)
            case Refusal():
                sentence = _remote_start_refusal(answer)
                self._refused_launch = launch.session_id
                self._offer_stage = LaunchStage.REFUSED
                self._offer_reason = sentence
                self._surface.note_remote_refusal(sentence)
                _logger.warning("dashboard launch %s refused: %s", launch.session_id, sentence)
            case Held():
                sentence = "depart distant non confirme : confirmation indisponible"
                self._offer_stage = LaunchStage.UNCONFIRMED
                self._offer_reason = sentence
                self._surface.note_remote_refusal(sentence)
                _logger.warning("dashboard launch %s held", launch.session_id)
            case RouteMissing():
                sentence = "route training/start inconnue"
                self._offer_stage = LaunchStage.UNCONFIRMED
                self._offer_reason = sentence
                self._surface.note_remote_refusal(sentence)
                _logger.warning("dashboard launch %s held: %s", launch.session_id, sentence)
            case _ as unreachable:
                assert_never(unreachable)

    def _submit_confirmed_launch(self, launch: Launch, operator: str, now: Monotonic) -> None:
        submitted = self._surface.submit_start(
            profile_id=launch.profile_id,
            operator=f"{launch.operator} ({DASHBOARD_OPERATOR})",
            total_duration_s=launch.total_duration_s,
            subject_id=launch.subject_id,
            subject_hr_max=launch.subject_hr_max,
            subject_age=launch.subject_age,
            cloud_session_id=launch.session_id,
            confirmed_by=operator,
        )
        if isinstance(submitted, Err):
            self._refuse_local_launch(launch, describe_surface_refusal(submitted.error))
            return
        self._confirmed_launch_id = launch.session_id
        _logger.info("dashboard launch %s submitted", launch.session_id)
        self._awaiting = _Awaiting(launch.session_id, now)

    def _refuse_local_launch(self, launch: Launch, detail: str) -> None:
        self._refused_launch = launch.session_id
        safe_detail = _safe_launch_refusal(launch, detail)
        self._offer_stage = LaunchStage.REFUSED
        self._offer_reason = safe_detail
        self._surface.note_remote_refusal(safe_detail)
        self._owe_refusal(launch.session_id, safe_detail)

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
        self._confirmed_launch_id = None
        launch = self._offered_launch
        if launch is not None and launch.session_id == awaiting.cloud_session_id:
            self._offer_stage = LaunchStage.UNCONFIRMED
            self._offer_reason = "la boucle n'a ni demarre ni refuse"
        self._owe_refusal(awaiting.cloud_session_id, "la boucle n'a ni demarre ni refuse")

    # --- a stop from the dashboard ------------------------------------------

    async def watch_stop(self) -> None:
        """Tell the dashboard of the running session, then ask whether it wants it stopped.

        Stepped from a task of its own, never from :meth:`step`: whatever the
        sending is waiting for (a slow disk, a slow upload, a backlog to catch
        up), a session is declared or its start confirmed as soon as it is
        armed, the first question about its stop follows at once, and the
        question then keeps its cadence (:data:`STATUS_PERIOD`).
        """
        await self._uplink.greet()
        now = self._clock.monotonic()
        session_id = self._uplink.following
        if session_id is None or not _due(self._last_status, now, STATUS_PERIOD):
            return
        self._last_status = now
        sent = await self._transport.get(STATUS_PATH, {"sessionId": session_id})
        # This route answers under every contract: its answers say nothing of
        # whether the dashboard serves this console's.
        self._note(sent, contract=False)
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

    def _note(
        self,
        result: Result[Document, CloudError],
        *,
        contract: bool = True,
        log_refusal_words: bool = True,
    ) -> None:
        """Log what an exchange says of the link, and tell the console's indicator.

        ``contract``: the route answers only a console whose major the
        dashboard serves. What the indicator is told is memory only, and
        nothing is decided on it: the sending, the launches and the stops go
        by the answer itself, exactly as without it.
        """
        was = self._online
        self._online = True
        now = self._clock.monotonic()
        if isinstance(result, Err):
            match result.error:
                case Unreachable() as lost:
                    self._online = False
                    if lost.status is None:
                        self._link.silent(lost.detail, contract=contract)
                    else:
                        # Something replied, and not in the dashboard's shape.
                        reason = f"HTTP {lost.status} : {NOT_THE_DASHBOARD}"
                        self._link.unrecognised(reason, contract=contract)
                    if was:
                        _logger.warning("dashboard unreachable: %s", lost.detail)
                case Refused() as refused:
                    # An answer, even a refusal, is a working link.
                    self._tell_refusal(now, refused, contract=contract)
                    self._hear_refusal(refused, include_words=log_refusal_words)
                case _ as unreachable:
                    assert_never(unreachable)
        else:
            self._link.answered(now, contract=contract)
        if self._online and not was:
            _logger.info("dashboard reachable")

    def _tell_refusal(self, now: Monotonic, refused: Refused, *, contract: bool) -> None:
        """Which of the link's states a refusal speaks of. Its status and stable code only:
        the dashboard's own sentence stays in the log.

        Only a refusal under one of the dashboard's stable codes is taken for
        the dashboard's answer about what was sent. A refused key and an
        error are read from the status alone, and never read as reachable. Any
        other refusal (a 404 or a 400 with no stable code) could come from
        anything that listens at the address: it proves nothing.
        """
        words = f"HTTP {refused.status}"
        if refused.code is not None:
            words = f"{words} ({refused.code})"
        if refused.code == CONTRACT_UNSUPPORTED:
            sentence = unsupported_refusal(refused.supported)
            self._link.contract_refused(now, sentence, contract=contract)
        elif refused.status in KEY_STATUSES:
            self._link.key_refused(now, words, contract=contract)
        elif refused.status in HELD_STATUSES or refused.status >= SERVER_ERROR:
            self._link.errored(words, contract=contract)
        elif refused.code in STABLE_CODES:
            # The dashboard read the request and refused it: it is there, and working.
            self._link.answered(now, contract=contract)
        else:
            self._link.unrecognised(f"{words} : {NOT_THE_DASHBOARD}", contract=contract)

    def _hear_refusal(self, refused: Refused, *, include_words: bool = True) -> None:
        """Log a refusal by its stable code, once; a refused contract goes to the console."""
        if refused != self._last_refused:
            self._last_refused = refused
            code = "sans code" if refused.code is None else refused.code
            if include_words:
                _logger.warning(
                    "dashboard refused a request: %s (HTTP %d): %s",
                    code,
                    refused.status,
                    logged_words(refused.detail),
                )
            else:
                _logger.warning("dashboard refused a request: %s (HTTP %d)", code, refused.status)
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
    subject_label: str | None


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
    label = session.get("subjectLabel")
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
        subject_label=label if isinstance(label, str) else None,
    )


def _safe_launch_refusal(launch: Launch, detail: str) -> str:
    safe = detail
    if launch.profile_id:
        safe = safe.replace(launch.profile_id, "programme")
    age = launch.subject_age
    if age is not None:
        safe = safe.replace(f"passager de {age} ans", "passager")
    label = launch.subject_label
    if label:
        safe = safe.replace(label, "passager")
    return safe.replace(launch.operator, "operateur")


def _due(last: Monotonic | None, now: Monotonic, period: Seconds) -> bool:
    return last is None or elapsed(last, now) >= period
