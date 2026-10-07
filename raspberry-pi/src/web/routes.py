"""The HTTP handlers. Every one of them a coroutine, and every one of them thin.

What a handler is allowed to do
------------------------------
Read state through the hub or the control surface, submit an intention through
the control surface, translate a refusal into a status code. That is all. No
handler touches the drive, and no handler waits for the control loop to do
anything: a request that blocked on a tick would make the operator interface as
slow as the slowest Modbus read, and the emergency stop as slow as both.

Why the routes are registered on the app directly
-------------------------------------------------
Not through ``include_router``, and this is not a style preference. In this
version of FastAPI an included router is stored as a single private
``_IncludedRouter`` node, so ``app.routes`` no longer enumerates the handlers
inside it - and the startup assertion in ``app.py`` walks ``app.routes`` to
prove every handler is ``async def``. Registered through a router, that
assertion would pass by looking at nothing, which is worse than not having it.
Registering directly keeps ``app.routes`` flat and the guarantee real.

Status codes that mean something
--------------------------------
* **202** for a start or a stop: accepted into the mailbox, the loop has not
  acted yet. Saying 200 would claim the machine had done something it has not.
* **200** for the emergency stop: the latch really has happened, synchronously,
  before this response was written.
* **409** for "the machine is not in a state for that", **412** for "the
  emergency-stop wiring has not been attested this boot", **422** for a profile
  that is not a usable programme.
* **403** for any request for motion while motion is disabled in this build
  (:attr:`~src.web.deps.Services.motion_enabled`, the local console's read-only
  milestone). Checked before anything else, so a disabled console never even
  validates a motion request, let alone submits one.

Exceptions are used here, which the contract permits in the web layer (rule 3).
The motor path below still returns ``Result``; ``HTTPException`` never crosses
into it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Final, assert_never

from fastapi import Depends, FastAPI, HTTPException, Request, params
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from src.control_surface import (
    CommandRefusal,
    EndRefusal,
    NothingRunning,
    RunState,
    SafetyHolding,
    StartRefusal,
    SurfaceBusy,
)
from src.geometry import MachineGeometry
from src.record.export import BUSY, TIMEOUT, UNKNOWN_RECORD, discard
from src.record.schema import RecordError
from src.result import Err, Ok
from src.sensors.registry import SPECS
from src.training.plan import (
    DeleteError,
    Malformed,
    ProfileParseError,
    ProfileStore,
    Program,
    Rejected,
    ResolveError,
    RevMismatch,
    StoreRev,
    StoreUnwritable,
    TrainingProfile,
    UnknownProfile,
    UpsertError,
    is_profile_id,
    parse_profile,
)
from src.training.safety import (
    ESTOP_ATTESTATION,
    AcknowledgeRefusal,
    AttestationRefusal,
    EmergencyStopStillLatched,
    EstopUnattested,
    GoSilentIsTerminal,
    NothingLatched,
    Unattributed,
)
from src.training.types import Occupancy
from src.units import OutputRpm, Seconds
from src.web.deps import TOKEN_HEADER, Services, WebConfig
from src.web.profile_writer import ProfileWriter
from src.web.schemas import (
    AckBody,
    AckRow,
    AttestationRow,
    AttestBody,
    BindRow,
    CameraRow,
    CommandRow,
    EcgRow,
    EndBody,
    EstopBody,
    EstopRow,
    FaultResetBody,
    HealthRow,
    ManualStartBody,
    ManualTargetBody,
    PanelRow,
    PlanPreviewRow,
    PortRow,
    PresenceBody,
    PresenceRow,
    PreviewBody,
    ProfileListRow,
    ProfileRow,
    RecordRow,
    RecordsRow,
    SafetyRow,
    SensorRow,
    SensorsRow,
    SnapshotRow,
    StartBody,
    StatusRow,
)

_logger: logging.Logger = logging.getLogger(__name__)

SERVICE_NAME: Final[str] = "anheart-operator-interface"

#: Files the page is made of, and the only paths served off the filesystem.
#: An explicit table rather than a static-files mount: a mount is an ASGI app
#: whose dispatch is not one of our handlers, so it would be the one thing the
#: all-routes-are-coroutines assertion could not speak about - and it would
#: serve whatever else ended up in the directory.
_STATIC_FILES: Final[Mapping[str, str]] = {
    "/": "text/html; charset=utf-8",
    "/app.css": "text/css; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
}

_INDEX: Final[str] = "index.html"


def register_routes(app: FastAPI, *, services: Services, config: WebConfig) -> None:
    """Register every HTTP route on ``app``. Called once, from ``create_app``."""
    _register_public(app, config=config)
    _register_api(app, services=services, config=config)


# =========================================================================
# Authentication
# =========================================================================


def token_guard(config: WebConfig) -> Callable[[Request], Awaitable[None]]:
    """Build the dependency that refuses a request without the shared token.

    A closure over the configuration rather than a lookup through
    ``request.app.state``: Starlette's ``State`` resolves attributes
    dynamically, so reading the config through it would be untyped at every use
    site, and this is the layer where a missed type is an unauthenticated motor
    control.

    Constant-time comparison, and a deliberately uninformative message: an
    unauthenticated caller learns that a token is required and nothing else -
    not whether a session is running, not who the operator is.
    """

    async def guard(request: Request) -> None:
        if config.token_matches(request.headers.get(TOKEN_HEADER)):
            return
        _logger.warning(
            "rejected %s %s: bad or missing %s", request.method, request.url.path, TOKEN_HEADER
        )
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail=f"a valid {TOKEN_HEADER} header is required",
        )

    return guard


# =========================================================================
# The unauthenticated surface
# =========================================================================


def _register_public(app: FastAPI, *, config: WebConfig) -> None:
    """Health and the page itself: reachable with no token.

    ``/healthz`` is unauthenticated because the container healthcheck has no
    token, and it therefore reports **nothing** about the session - see
    :class:`~src.web.schemas.HealthRow`.

    The page is unauthenticated because it is inert: HTML, CSS and JavaScript
    with no data in them. Every number it displays comes from the API, which
    the token gates. Serving the shell freely is what lets an operator reach a
    login-ish token field at all.
    """

    @app.get("/healthz", tags=["system"])
    async def healthz() -> HealthRow:
        """Liveness for the container healthcheck. Says nothing about the session."""
        return HealthRow(status="ok", service=SERVICE_NAME)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """The operator page."""
        return _static_response(config.static_dir / _INDEX, _STATIC_FILES["/"])

    @app.get("/app.css", include_in_schema=False)
    async def stylesheet() -> FileResponse:
        """The page's stylesheet."""
        return _static_response(config.static_dir / "app.css", _STATIC_FILES["/app.css"])

    @app.get("/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        """The page's script. No build step, no CDN: the Pi serves this offline."""
        return _static_response(config.static_dir / "app.js", _STATIC_FILES["/app.js"])


def _static_response(path: Path, media_type: str) -> FileResponse:
    """A file response, or a 404 that names what is missing.

    The existence check is a single ``stat`` on a local path - bounded work,
    unlike the serial and subprocess calls contract rule 8 is about - and it
    buys a diagnosable 404 instead of a bare 500 from inside the response
    object. ``FileResponse`` itself reads the body on a worker thread, so the
    event loop is not blocked by the transfer.
    """
    if not path.is_file():
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"{path.name} is missing from the installation",
        )
    return FileResponse(path=path, media_type=media_type)


# =========================================================================
# The authenticated API
# =========================================================================


def _register_api(app: FastAPI, *, services: Services, config: WebConfig) -> None:
    """Register everything behind the token check.

    Split into four functions by subject rather than written as one: the
    dependency list is built once, here, so no group of routes can be
    registered without it by accident.
    """
    auth: Sequence[params.Depends] = (Depends(token_guard(config)),)
    _register_reads(app, services=services, config=config, auth=auth)
    _register_profiles(app, services=services, auth=auth)
    _register_session(app, services=services, auth=auth)
    _register_manual(app, services=services, auth=auth)
    _register_safety(app, services=services, auth=auth)
    _register_records(app, services=services, auth=auth)


def _register_records(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """The session records on disk: their list, and one of them as an archive.

    Both read the disk, on threads kept apart from everything else the console
    does (:class:`~src.record.export.RecordIo`): no handler here blocks the
    loop the control tick runs on, and none can take a thread from the ECG
    treatment. Both are refused while a session is in progress: the machine
    reads its records at rest.
    """
    surface = services.surface

    def at_rest_only(what: str) -> None:
        if surface.run_state is not RunState.IDLE:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f"{what} pendant une seance : attendre le retour au repos",
            )

    @app.get("/api/records", dependencies=auth, tags=["records"])
    async def list_records() -> RecordsRow:
        """Every session record on this machine, newest first. 409 during a session."""
        exporter = services.records
        if exporter is None:
            return RecordsRow(recording=False, records=())
        at_rest_only("liste des enregistrements refusee")
        listed = await exporter.listing()
        if isinstance(listed, Err):
            raise _record_failure(listed.error)
        return RecordsRow(
            recording=True,
            records=tuple(
                RecordRow(name=entry.name, closed=entry.closed) for entry in listed.value
            ),
        )

    @app.get("/api/records/{name}/archive", dependencies=auth, tags=["records"])
    async def export_record(name: str) -> FileResponse:
        """One record as ``<name>.tar.gz``. 409 while a session is in progress.

        Refused during a session on purpose: compressing tens of megabytes is
        work this machine should not be doing with somebody on board, and the
        record of the session in progress is not complete yet. The archive is
        a temporary file, removed once the response has been sent.
        """
        exporter = services.records
        if exporter is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND, detail="cette console n'enregistre pas"
            )
        at_rest_only("export refuse")
        built = await exporter.archive(name)
        if isinstance(built, Err):
            raise _record_failure(built.error)
        return FileResponse(
            path=built.value,
            media_type="application/gzip",
            filename=f"{name}.tar.gz",
            background=BackgroundTask(discard, built.value),
        )


def _record_failure(error: RecordError) -> HTTPException:
    """Why a record could not be read, as the status and the sentence the page shows."""
    if error.detail == UNKNOWN_RECORD:
        return HTTPException(
            status_code=HTTPStatus.NOT_FOUND, detail="enregistrement inconnu sur cette machine"
        )
    if error.detail == BUSY:
        return HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="lecture des enregistrements deja en cours : reessayer dans un instant",
        )
    if error.detail == TIMEOUT:
        return HTTPException(
            status_code=HTTPStatus.GATEWAY_TIMEOUT,
            detail="le disque des enregistrements ne repond pas",
        )
    return HTTPException(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        detail=f"lecture des enregistrements impossible ({error.detail})",
    )


def _register_reads(
    app: FastAPI, *, services: Services, config: WebConfig, auth: Sequence[params.Depends]
) -> None:
    """Status, the latest snapshot, and the ECG trace."""
    surface = services.surface
    hub = services.hub
    supervisor = services.supervisor
    store = services.store

    @app.get("/api/status", dependencies=auth, tags=["system"])
    async def read_status() -> StatusRow:
        """Everything the setup view needs, including every reason not to start.

        One request rather than six, because the start button's enabled state
        depends on all of them at once and a page that assembled it from
        several responses would render a moment that never existed.
        """
        now = services.clock.monotonic()
        ecg = hub.ecg_window(limit=0)
        attested = supervisor.require_estop_confirmed()
        accepted, refused, published = surface.counters
        pending = surface.pending
        return StatusRow(
            run_state=surface.run_state.value,
            estop_latched=surface.estop_latched,
            supervisor_estop=SafetyRow.of(supervisor.estop, now),
            attested=isinstance(attested, Ok),
            attestation=AttestationRow.of(attested.value) if isinstance(attested, Ok) else None,
            attestation_statement=ESTOP_ATTESTATION,
            standing=SafetyRow.of(supervisor.standing, now),
            floor=SafetyRow.of(supervisor.floor, now),
            live=tuple(
                row
                for row in (SafetyRow.of(verdict, now) for verdict in supervisor.live)
                if row is not None
            ),
            retained_hr_samples=supervisor.retained_samples,
            pending=None if pending is None else CommandRow.of(pending),
            attendant_last_seen=surface.attendant_last_seen,
            clients=hub.client_count,
            evictions=hub.evictions,
            ecg_fs_hz=ecg.fs_hz,
            ecg_seq=ecg.seq,
            profile_rev=int(store.rev),
            profile_ids=tuple(profile.profile_id for profile in store.list_profiles()),
            ports=tuple(PortRow.of(port) for port in await services.ports.list_ports()),
            bind=BindRow.of(config),
            counters=(accepted, refused, published),
        )

    @app.get("/api/snapshot", dependencies=auth, tags=["session"])
    async def read_snapshot() -> SnapshotRow | None:
        """The newest telemetry snapshot, or ``null`` before the first tick.

        ``null`` is a real answer and the page renders it as "no data". The
        fallback for a browser whose socket dropped, and the reason it exists
        at all: a page that polls this while reconnecting shows stale-but-aged
        numbers rather than frozen ones.
        """
        snapshot = surface.latest
        return None if snapshot is None else SnapshotRow.of(snapshot)

    @app.get("/api/ecg", dependencies=auth, tags=["session"])
    async def read_ecg(after: int | None = None, limit: int | None = None) -> EcgRow:
        """Recent ECG for the live trace, by sequence number.

        ``after`` is the sequence the caller already holds, so a page polls for
        only what is new. When the ring has moved past that point the response
        says ``gap: true`` rather than splicing: joining two ends of a
        discontinuity draws a vertical stroke that reads as a QRS complex.
        """
        return EcgRow.of(hub.ecg_window(after=after, limit=limit))

    @app.get("/api/panel", dependencies=auth, tags=["system"])
    async def read_panel() -> PanelRow | None:
        """The local console's link panel, or ``null`` when no console is wired.

        Drive link (idle reads, failures, latency, last error), BITalino link
        (decoder counters, DSP bridge), whether motion is enabled, and the
        geometry every g on the page was computed with.
        """
        panel = services.panel
        return None if panel is None else PanelRow.of(panel.panel_status())

    @app.get("/api/camera", dependencies=auth, tags=["safety"])
    async def read_camera() -> CameraRow:
        """The camera fail-safe: configured or not, its last decision, any latched rule."""
        return CameraRow.of(services.presence, services.camera)

    @app.get("/api/sensors", dependencies=auth, tags=["sensors"])
    async def read_sensors() -> SensorsRow:
        """Every acquired BITalino channel: latest window, quality and metrics.

        Monitoring only: none of these numbers commands the motor (the heart
        rate in control is the one in ``/api/snapshot``). An empty list when
        this build acquires no channel beyond what the snapshot already shows.
        """
        source = services.sensors
        if source is None:
            return SensorsRow(sensors=())
        latest = source.latest()
        return SensorsRow(
            sensors=tuple(SensorRow.of(SPECS[kind], latest.get(kind)) for kind in source.kinds)
        )


def _register_profiles(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """Profile CRUD and the dry-run phase-plan preview."""
    store = services.store
    geometry = services.geometry
    writer = ProfileWriter(store)

    @app.get("/api/profiles", dependencies=auth, tags=["profiles"])
    async def list_profiles() -> ProfileListRow:
        """Every stored profile, with the revision they were read at."""
        return _profile_list(store)

    @app.put("/api/profiles/{profile_id}", dependencies=auth, tags=["profiles"])
    async def upsert_profile(
        profile_id: str, rev: int, document: Mapping[str, object]
    ) -> ProfileRow:
        """Create or replace one profile.

        The document is untrusted JSON and goes through
        :func:`~src.training.plan.parse_profile`, which is the only thing in
        this system that decides whether a set of numbers is a usable training
        programme - including the check that the zone sits below 90% of the
        subject's maximum. A refusal comes back as 422 listing every violation,
        because an operator fixing a profile needs all of them rather than the
        first.

        ``rev`` is optimistic concurrency. Two browsers editing the same
        profile produce a 409 for the second one instead of a silent overwrite.
        """
        if profile_id != document.get("profile_id"):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=(
                    f"the URL names {profile_id!r} and the document names "
                    f"{document.get('profile_id')!r}: they must agree"
                ),
            )
        parsed = parse_profile(document, where=f"profile {profile_id!r}")
        if isinstance(parsed, Ok):
            return await _commit_profile(writer, parsed.value, rev)
        raise _parse_failure(parsed.error)

    @app.delete("/api/profiles/{profile_id}", dependencies=auth, tags=["profiles"])
    async def delete_profile(profile_id: str, rev: int) -> ProfileListRow:
        """Delete one profile. Deleting the last one leaves an empty store."""
        removed = await writer.delete(profile_id, StoreRev(rev))
        if isinstance(removed, Ok):
            return _profile_list(store)
        raise _delete_failure(removed.error)

    @app.post("/api/plan/preview", dependencies=auth, tags=["profiles"])
    async def preview_plan(body: PreviewBody) -> PlanPreviewRow:
        """Resolve a profile into a programme **without starting anything**.

        The same resolution a start performs, including the total-duration
        override, so what the operator approves is what would run. Every speed
        comes back as motor rpm, output rpm, Hz and g together, because the
        g-load at the ceiling is the number worth reading before somebody is in
        the machine rather than after.
        """
        resolved = store.resolve(
            body.profile_id,
            at=services.clock.unix_millis(),
            total_duration_s=_optional_seconds(body.total_duration_s),
        )
        if isinstance(resolved, Ok):
            return _preview(resolved.value, geometry)
        raise _resolve_failure(resolved.error)


def _register_session(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """Start, stop and the emergency stop."""
    surface = services.surface

    @app.post(
        "/api/session/start",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["session"],
    )
    async def start_session(body: StartBody) -> CommandRow:
        """Ask for a session to start. 202: accepted, not yet acted on.

        The profile id is checked for shape here and its existence is checked
        by resolving it, before anything is submitted, so an operator gets a
        404 rather than a session that starts and immediately ends.
        """
        _require_motion_enabled(services)
        _require_programs_enabled(services)
        operator = _require_operator(body.operator)
        _require_known_profile(services, body.profile_id, body.total_duration_s)
        submitted = surface.submit_start(
            profile_id=body.profile_id,
            operator=operator,
            total_duration_s=_optional_seconds(body.total_duration_s),
            subject_age=body.subject_age,
        )
        if isinstance(submitted, Ok):
            return CommandRow.of(submitted.value)
        raise _start_failure(submitted.error)

    @app.post(
        "/api/session/stop",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["session"],
    )
    async def stop_session(body: EndBody) -> CommandRow:
        """End the running session on the commissioned ramp. 202: accepted.

        The ordinary stop. It is deliberately *not* the fast one: a
        deceleration faster than the drive's commissioned ramp trips
        overvoltage and drops the machine into a freewheel, which is a longer
        coast-down with a person inside.
        """
        operator = _require_operator(body.operator)
        submitted = surface.submit_end(operator=operator, reason=body.reason)
        if isinstance(submitted, Ok):
            return CommandRow.of(submitted.value)
        raise _end_failure(submitted.error)

    @app.post("/api/session/estop", dependencies=auth, tags=["session"])
    async def emergency_stop(body: EstopBody) -> EstopRow:
        """Latch the emergency stop. 200, because it has already happened.

        Never refused. Not for a blank operator name, not for a missing reason,
        not because no session is running, and there is no confirmation step -
        a stop that asks "are you sure?" is not an emergency stop.

        This returns **before the control loop has ticked**: the latch is three
        synchronous attribute-level steps in
        :meth:`~src.control_surface.ControlSurface.submit_estop` and none of
        them awaits. A test asserts exactly that, because a stop whose
        effectiveness depends on the loop's cadence is not independent of the
        thing most likely to have failed.

        And what it does not claim: the machine is not stopped. STO is jumpered,
        so the fastest stop available is the ramp, and the measured output speed
        in the next snapshot is the only thing that speaks about motion.
        """
        return EstopRow.of(surface.submit_estop(operator=body.operator, reason=body.reason))


def _register_manual(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """The manual session: start it, set its target, and the drive's fault reset.

    Every one of them is a request for motion or a write to the drive, so each
    is behind the 403 of :func:`_require_motion_enabled` first. Each is a 202:
    the loop has not acted yet, and whether the MACHINE can act is its answer,
    published as an event (``refused``) and visible in the next snapshot.
    """
    surface = services.surface

    @app.post(
        "/api/manual/start",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["manual"],
    )
    async def start_manual(body: ManualStartBody) -> CommandRow:
        """Start a manual session, target 0. The occupancy is declared here, once.

        403 when motion is disabled or the occupancy is refused by
        configuration (``occupied`` until milestone M6); 422 for an occupancy
        this build does not know.
        """
        _require_motion_enabled(services)
        operator = _require_operator(body.operator)
        occupancy = _require_occupancy(body.occupancy)
        _require_occupancy_allowed(services, occupancy)
        submitted = surface.submit_start_manual(occupancy=occupancy, operator=operator)
        if isinstance(submitted, Ok):
            return CommandRow.of(submitted.value)
        raise _start_failure(submitted.error)

    @app.post(
        "/api/manual/target",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["manual"],
    )
    async def manual_target(body: ManualTargetBody) -> CommandRow:
        """Set the manual target in OUTPUT rpm. The setpoint walks to it at the motion limits.

        Only a target: never a setpoint, never faster than the anti-nausea
        limits, and every safety verdict still overrides it. A value outside
        ``0`` or ``[min_run, ceiling]`` is refused by the loop, not clamped.
        """
        _require_motion_enabled(services)
        operator = _require_operator(body.operator)
        if not math.isfinite(body.output_rpm) or body.output_rpm < 0.0:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail="the target must be a finite, non-negative output speed",
            )
        submitted = surface.submit_manual_target(
            output_rpm=OutputRpm(body.output_rpm), operator=operator
        )
        if isinstance(submitted, Ok):
            return CommandRow.of(submitted.value)
        raise _command_failure(submitted.error)

    @app.post(
        "/api/drive/fault-reset",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["manual"],
    )
    async def fault_reset(body: FaultResetBody) -> CommandRow:
        """Reset a drive fault: named, explicit, and never automatic.

        The loop refuses it unless the machine is at rest, every verdict has
        been acknowledged by name, and the drive shows the shaft stopped.
        """
        _require_motion_enabled(services)
        operator = _require_operator(body.operator)
        submitted = surface.submit_fault_reset(operator=operator)
        if isinstance(submitted, Ok):
            return CommandRow.of(submitted.value)
        raise _command_failure(submitted.error)


def _register_safety(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """The attestation gate, acknowledgement and the presence ping."""
    surface = services.surface

    @app.post("/api/safety/attest", dependencies=auth, tags=["safety"])
    async def attest(body: AttestBody) -> AttestationRow:
        """Record, by name, that a real emergency stop is wired in.

        Both boxes must be ticked. They are two separate facts - the STO jumper
        is gone, and a latching mushroom is wired normally-closed into
        P24 -> STO - and an operator who is sure of only one of them must not
        be able to pass both with one click. Refusing without recording is the
        point: a half-attestation in the log would be worse than none.

        Per boot, not per install: the record lives in this process and dies
        with it, so a Pi that has rebooted is a Pi whose wiring nobody has
        vouched for since.
        """
        if not (body.sto_jumper_removed and body.mushroom_wired_nc):
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=(
                    "both confirmations are required, separately: the STO jumper is "
                    "removed, AND a latching emergency stop is wired normally-closed "
                    "into P24 -> STO"
                ),
            )
        recorded = surface.attest_estop_wiring(body.operator)
        if isinstance(recorded, Ok):
            return AttestationRow.of(recorded.value)
        raise _attestation_failure(recorded.error)

    @app.post("/api/safety/acknowledge", dependencies=auth, tags=["safety"])
    async def acknowledge(body: AckBody) -> AckRow:
        """Clear the latched safety verdicts. The only thing that can.

        Never called automatically from anywhere. ``estop_released`` is the
        operator stating that the mushroom has been pulled back out; software
        cannot see the contact, and the default is ``False`` so that forgetting
        to ask fails closed.
        """
        cleared = surface.acknowledge(body.operator, estop_released=body.estop_released)
        if isinstance(cleared, Ok):
            return AckRow.of(cleared.value)
        raise _acknowledge_failure(cleared.error)

    @app.post("/api/presence", dependencies=auth, tags=["safety"])
    async def presence(body: PresenceBody) -> PresenceRow:
        """An attendant's screen saying it is still there.

        Feeds the ``attendant_absent`` rule, which freezes the setpoint after a
        minute of silence and ramps down after two. Deliberately cheap and
        deliberately unlogged as an event: it happens every few seconds, and the
        freshness of the instant is the entire signal.

        It proves a browser tab is open and reachable. It does not prove a human
        is watching, and nothing in software can.
        """
        return PresenceRow(at=surface.note_presence(body.operator))


# =========================================================================
# Translating refusals into status codes
# =========================================================================
#
# One function per closed union, each ending in assert_never. A variant added
# to any of those unions fails the type check here, naming the one that has no
# status code yet - which is the whole reason they are closed unions.


def _start_failure(refusal: StartRefusal) -> HTTPException:
    """Map a start refusal. See :data:`~src.control_surface.StartRefusal`."""
    match refusal:
        case SurfaceBusy():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    f"the machine is {refusal.state.value}, not idle"
                    + (f" (pending: {refusal.pending})" if refusal.pending is not None else "")
                ),
            )
        case SafetyHolding():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    f"a latched safety verdict stands ({refusal.verdict.rule}): "
                    f"{refusal.verdict.detail} - it must be acknowledged by name first"
                ),
            )
        case EstopUnattested():
            return HTTPException(
                status_code=HTTPStatus.PRECONDITION_FAILED,
                detail=f"nobody has attested the emergency stop this boot: {refusal.statement}",
            )
    raise assert_never(refusal)


def _end_failure(refusal: EndRefusal) -> HTTPException:
    """Map an end refusal. See :data:`~src.control_surface.EndRefusal`."""
    match refusal:
        case NothingRunning():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f"there is no session to end (the machine is {refusal.state.value})",
            )
        case SurfaceBusy():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f"the machine is already {refusal.state.value}",
            )
    raise assert_never(refusal)


def _command_failure(refusal: CommandRefusal) -> HTTPException:
    """Map a target or fault-reset refusal. See :data:`~src.control_surface.CommandRefusal`."""
    match refusal:
        case NothingRunning():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=f"no manual session is running (the machine is {refusal.state.value})",
            )
        case SurfaceBusy():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    f"the machine is {refusal.state.value}"
                    + (f" (pending: {refusal.pending})" if refusal.pending is not None else "")
                    + "; try again in a moment"
                ),
            )
    raise assert_never(refusal)


def _acknowledge_failure(refusal: AcknowledgeRefusal) -> HTTPException:
    """Map an acknowledgement refusal."""
    match refusal:
        case Unattributed():
            return HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=refusal.detail)
        case GoSilentIsTerminal():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    f"{refusal.rule} demanded GO_SILENT, which is one-way: this process "
                    "will not command motion again, and recovery is an operator action "
                    "on a machine that has demonstrably stopped"
                ),
            )
        case EmergencyStopStillLatched():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    "the emergency stop is still latched: confirm the mushroom has been "
                    "pulled back out (estop_released) before acknowledging"
                ),
            )
        case NothingLatched():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT, detail="nothing is latched to acknowledge"
            )
    raise assert_never(refusal)


def _attestation_failure(refusal: AttestationRefusal) -> HTTPException:
    """Map an attestation refusal."""
    match refusal:
        case Unattributed():
            return HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=refusal.detail)
    raise assert_never(refusal)


def _parse_failure(error: ProfileParseError) -> HTTPException:
    """Map a profile parse failure."""
    match error:
        case Malformed():
            return HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"{error.detail}: " + "; ".join(error.problems),
            )
        case Rejected():
            return HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f"{error.detail}: "
                + ", ".join(violation.value for violation in error.violations),
            )
    raise assert_never(error)


def _resolve_failure(error: ResolveError) -> HTTPException:
    """Map a profile resolution failure."""
    match error:
        case UnknownProfile():
            return HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"no profile {error.profile_id!r}; known: {', '.join(error.known)}",
            )
        case Rejected():
            return HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=f"{error.detail}: "
                + ", ".join(violation.value for violation in error.violations),
            )
    raise assert_never(error)


def _upsert_failure(error: UpsertError) -> HTTPException:
    """Map a profile write failure."""
    match error:
        case RevMismatch():
            return HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    f"the store moved on: you edited revision {error.expected}, it is now "
                    f"{error.actual}. Reload before saving, or an edit is lost silently"
                ),
            )
        case StoreUnwritable():
            return HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail=f"cannot write {error.path}: {error.detail}",
            )
    raise assert_never(error)


def _delete_failure(error: DeleteError) -> HTTPException:
    """Map a profile delete failure."""
    match error:
        case UnknownProfile():
            return HTTPException(
                status_code=HTTPStatus.NOT_FOUND, detail=f"no profile {error.profile_id!r}"
            )
        case RevMismatch() | StoreUnwritable():
            return _upsert_failure(error)
    raise assert_never(error)


# =========================================================================
# Small helpers
# =========================================================================


MOTION_DISABLED_DETAIL: Final[str] = (
    "mouvement desactive : cette console est en lecture seule. "
    "STOP, E-STOP, acquittement et lectures restent disponibles."
)


def _require_motion_enabled(services: Services) -> None:
    """Refuse any request for motion with a 403 while motion is disabled."""
    if not services.motion_enabled:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=MOTION_DISABLED_DETAIL)


PROGRAMS_DISABLED_DETAIL: Final[str] = (
    "seances programmees desactivees sur cette console (jalon M5) : utiliser le mode MANUEL."
)


def _require_programs_enabled(services: Services) -> None:
    """Refuse a programmed session with a 403 while this build has none."""
    if not services.programs_enabled:
        raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=PROGRAMS_DISABLED_DETAIL)


def _require_occupancy(value: str) -> Occupancy:
    """The declared occupancy, or a 422 naming the ones that exist."""
    for occupancy in Occupancy:
        if occupancy.value == value:
            return occupancy
    known = ", ".join(occupancy.value for occupancy in Occupancy)
    raise HTTPException(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        detail=f"unknown occupancy {value!r}: expected one of {known}",
    )


def _require_occupancy_allowed(services: Services, occupancy: Occupancy) -> None:
    """403 unless the configuration gives this occupancy a ceiling."""
    ceilings = services.ceilings
    if ceilings is None:
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN,
            detail="this interface has no manual sessions",
        )
    match ceilings(occupancy):
        case Ok():
            return
        case Err(refused):
            raise HTTPException(status_code=HTTPStatus.FORBIDDEN, detail=refused.detail)


def _require_operator(operator: str) -> str:
    """The operator's name, or a 400 explaining why one is needed.

    Required for a start, an end and an acknowledgement, and deliberately **not**
    for an emergency stop: a stop must never be refused for a missing form field.
    """
    if not operator.strip():
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail="an operator name is required: an unattributable session record is not one",
        )
    return operator


def _require_known_profile(
    services: Services, profile_id: str, total_duration_s: float | None
) -> None:
    """Refuse a start for a profile that is not a usable programme, before submitting.

    The control surface does no I/O and therefore cannot check this; the loop
    would find out and end the session. Checking here turns "the session started
    and stopped and nobody knows why" into a 404 or a 422 on the screen of the
    person who typed it.
    """
    if not is_profile_id(profile_id):
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"{profile_id!r} is not a well-formed profile id",
        )
    resolved = services.store.resolve(
        profile_id,
        at=services.clock.unix_millis(),
        total_duration_s=_optional_seconds(total_duration_s),
    )
    if isinstance(resolved, Err):
        raise _resolve_failure(resolved.error)


def _optional_seconds(value: float | None) -> Seconds | None:
    """Tag an operator-supplied duration as ``Seconds``, or pass ``None`` through.

    The one place a number from the wire becomes a domain duration. The plan
    layer refuses a non-finite or out-of-range total, so nothing is validated
    twice here.
    """
    return None if value is None else Seconds(value)


def _profile_list(store: ProfileStore) -> ProfileListRow:
    """Render the store's contents, with the revision an edit must send back."""
    return ProfileListRow(
        rev=int(store.rev),
        profiles=tuple(ProfileRow.of(profile) for profile in store.list_profiles()),
    )


async def _commit_profile(writer: ProfileWriter, profile: TrainingProfile, rev: int) -> ProfileRow:
    """Write one validated profile, or raise the mapped failure."""
    written = await writer.upsert(profile, StoreRev(rev))
    if isinstance(written, Ok):
        return ProfileRow.of(profile)
    raise _upsert_failure(written.error)


def _preview(program: Program, geometry: MachineGeometry) -> PlanPreviewRow:
    """Render a resolved programme with its speeds expressed four ways."""
    profile = program.profile
    return PlanPreviewRow.of(
        program,
        ceiling=geometry.view(profile.max_rpm),
        warmup_ceiling=geometry.view(profile.warmup_rpm_ceiling),
        min_run=geometry.view(profile.min_run_rpm),
    )
