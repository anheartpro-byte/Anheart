"""Tests for the operator interface: the control surface, the hub, and the app.

What these tests are actually for. Three of them carry guarantees that nothing
else in the suite can:

* ``test_every_route_handler_is_a_coroutine_function`` - a synchronous handler
  is dispatched by Starlette to a thread pool, where it would touch the control
  surface, the telemetry hub and the safety supervisor from off the event loop.
  All three are lock-free *on the premise* that one thread touches them, so one
  ``def`` instead of one ``async def`` turns a documented invariant into a race
  on the state that stops a motor.
* ``test_emergency_stop_returns_before_the_loop_has_ticked_once`` - the stop is
  only worth anything if it does not wait for the loop, because the loop is the
  thing most likely to be stuck. The test runs a "loop" that is blocked on an
  await that never completes and asserts the stop still latches, with the tick
  counter provably at zero.
* ``test_the_control_surface_never_awaits`` - parses ``src/control_surface.py``
  and fails on any ``async def`` or ``await``. That file's freedom from locks is
  argued from exactly that property, so the argument has to be checked rather
  than believed.

``ManualClock`` is used for the surface and hub tests, and it is honest to do so
there: nothing below claims a wall-clock bound. Where a bound *is* claimed - the
pacing interval, the staleness window - the test advances the clock by the exact
amount and asserts the boundary, which is a statement about the arithmetic and
not about how fast the machine ran.
"""

from __future__ import annotations

import ast
import asyncio
import math
import re
import socket
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final, Never

import httpx
import pytest
from fastapi import FastAPI
from pydantic import TypeAdapter
from starlette.websockets import WebSocketDisconnect

from src.clock import ManualClock
from src.control_surface import (
    ControlSurface,
    EndSession,
    EventKind,
    RunState,
    SessionEvent,
    StartSession,
)
from src.geometry import MachineGeometry
from src.motor.drive import DriveFault, DriveState, FaultReport
from src.result import Err, Ok
from src.telemetry import (
    CLIENT_QUEUE_LIMIT,
    DEFAULT_MIN_INTERVAL,
    EcgRing,
    PayloadKind,
    TelemetryHub,
)
from src.training.plan import ProfileStore
from src.training.safety import SafetyLimits, SafetyObservation, SafetySupervisor
from src.training.types import (
    HeartRateSample,
    Phase,
    SafetyAction,
    SignalQuality,
    SpeedView,
    TelemetrySnapshot,
    ZoneCounters,
)
from src.units import (
    Amperes,
    Bpm,
    GearRatio,
    Hertz,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    RawRegister,
    Seconds,
    UnixMillis,
)
from src.web.app import (
    LoopSafeServer,
    build_server,
    create_app,
    require_async_routes,
    serve,
)
from src.web.deps import (
    MIN_TOKEN_LENGTH,
    FilesystemPortLister,
    SerialPortInfo,
    Services,
    WebConfig,
)
from src.web.schemas import (
    AckRow,
    AttestationRow,
    CommandRow,
    EcgRow,
    EstopRow,
    HealthRow,
    PlanPreviewRow,
    PresenceRow,
    ProfileListRow,
    ProfileRow,
    SnapshotRow,
    StatusRow,
    WsEnvelope,
    encode_envelope,
)
from src.web.ws import TELEMETRY_PATH

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

TOKEN: Final[str] = "a-token-of-at-least-sixteen-characters"  # noqa: S105  # a test fixture
ORIGIN: Final[str] = "http://127.0.0.1:8080"
FOREIGN_ORIGIN: Final[str] = "http://clinic-wifi-printer.local"


def free_port() -> int:
    """A port the system says is free, for the tests that really bind a socket.

    Asked of the system each time (bind to port 0, read the number, let go),
    never a fixed number: a fixed port is shared by every test that binds, in
    this process and in the others of a gate run as several processes, and two
    of them at the same moment fail each other (ANH-183 EX-3). The number has
    to be known before the server is built: ``WebConfig`` refuses port 0, and
    rightly - an operator cannot type a URL for a port nobody knows. If the
    bind then fails, the test fails loudly, which is the correct outcome for a
    test whose whole point is that the bind really happened.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]  # pyright: ignore[reportAny]
    return port


PROFILE_ID: Final[str] = "standard_30_min"
OPERATOR: Final[str] = "dr. attending"

GEOMETRY: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.0))


# =========================================================================
# Test doubles and a rig
# =========================================================================


@dataclass(slots=True)
class StubPorts:
    """A port lister with a fixed answer, so the status page is testable."""

    ports: tuple[SerialPortInfo, ...] = (
        SerialPortInfo(device="/dev/ttyUSB0", description="usb-FTDI_drive-if00-port0"),
    )

    async def list_ports(self) -> tuple[SerialPortInfo, ...]:
        return self.ports


@dataclass(slots=True)
class RecordingSink:
    """Captures what the control surface publishes, with no transport at all."""

    snapshots: list[TelemetrySnapshot] = field(default_factory=list)
    events: list[SessionEvent] = field(default_factory=list)

    def publish_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self.snapshots.append(snapshot)

    def publish_event(self, event: SessionEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class Rig:
    """Everything wired together, the way ``src/local_panel.py`` wires it."""

    clock: ManualClock
    supervisor: SafetySupervisor
    hub: TelemetryHub
    surface: ControlSurface
    store: ProfileStore
    config: WebConfig
    app: FastAPI

    def attest(self) -> None:
        """Pass the startup gate, the way an operator does before a session."""
        recorded = self.surface.attest_estop_wiring(OPERATOR)
        assert isinstance(recorded, Ok)


def build_limits() -> SafetyLimits:
    """Limits for this rig. The two cardiac ones have no defaults, by design."""
    return SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))


def build_rig(tmp_path: Path, *, token: str | None = TOKEN, host: str = "127.0.0.1") -> Rig:
    """Assemble a complete interface over a temporary profile store."""
    clock = ManualClock()
    supervisor = SafetySupervisor(clock=clock, limits=build_limits())
    hub = TelemetryHub(clock=clock)
    surface = ControlSurface(clock=clock, supervisor=supervisor, sink=hub)
    store = ProfileStore(tmp_path / "profiles.json")
    loaded = store.load()
    assert isinstance(loaded, Ok)
    config = WebConfig(host=host, port=8080, token=token)
    services = Services(
        clock=clock,
        surface=surface,
        hub=hub,
        supervisor=supervisor,
        store=store,
        ports=StubPorts(),
        geometry=GEOMETRY,
    )
    return Rig(
        clock=clock,
        supervisor=supervisor,
        hub=hub,
        surface=surface,
        store=store,
        config=config,
        app=create_app(services=services, config=config),
    )


def make_observation(*, at: Monotonic = Monotonic(1.0)) -> SafetyObservation:
    """One observation of a machine sitting still, for driving the supervisor.

    Every field is stated: the supervisor has no defaults for them, and a
    default here is how a test ends up asserting something about a machine it
    did not describe.
    """
    return SafetyObservation(
        now=at,
        phase=Phase.HOLD,
        elapsed=Seconds(100.0),
        total_duration=Seconds(1800.0),
        commanded_rpm=MotorRpm(0),
        ramping=False,
        heart_rate=None,
        drive_state=DriveState.SWITCHED_ON,
        measured_rpm=MotorRpm(0),
        current=Amperes(0.0),
        fault=None,
        consecutive_comm_failures=0,
        attendant_last_seen=at,
    )


def make_snapshot(
    *,
    at: Monotonic = Monotonic(10.0),
    phase: Phase = Phase.HOLD,
    bpm: Bpm | None = Bpm(124),
    quality: SignalQuality = SignalQuality.GOOD,
    hr_age: Seconds | None = Seconds(1.0),
    measured_rpm: MotorRpm = MotorRpm(200),
    setpoint_rpm: MotorRpm = MotorRpm(205),
    drive_state: DriveState = DriveState.OPERATION_ENABLED,
    drive_age: Seconds | None = Seconds(0.2),
    fault: FaultReport | None = None,
    seq: int = 7,
    sampled: bool = True,
) -> TelemetrySnapshot:
    """One plausible snapshot. Every field explicit; nothing defaulted to zero."""
    return TelemetrySnapshot(
        at=at,
        wall_clock=UnixMillis(1_700_000_000_000),
        phase=phase,
        elapsed=Seconds(600.0),
        remaining=Seconds(1200.0),
        heart_rate=(HeartRateSample(bpm=bpm, quality=quality, seq=seq, at=at) if sampled else None),
        heart_rate_age=hr_age if sampled else None,
        target_bpm=Bpm(128),
        setpoint=view(setpoint_rpm),
        measured=view(measured_rpm),
        setpoint_confirmed=True,
        drive_state=drive_state,
        drive_status_age=drive_age,
        current=Amperes(1.8),
        fault=fault,
        safety=None,
        counters=ZoneCounters(
            in_zone=Seconds(300.0), above_zone=Seconds(30.0), below_zone=Seconds(60.0)
        ),
    )


def view(rpm: MotorRpm) -> SpeedView:
    """A speed through this rig's geometry."""
    return SpeedView.from_motor_rpm(
        rpm,
        ratio=GearRatio(49.79),
        radius=Metres(1.0),
        nominal_rpm=MotorRpm(1380),
        base_hz=Hertz(50.0),
    )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    """A rig with a token configured, on loopback."""
    return build_rig(tmp_path)


@pytest.fixture
async def client(rig: Rig) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client speaking to the app in-process, over ASGI."""
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as session:
        yield session


def auth() -> dict[str, str]:
    """The header a valid caller presents."""
    return {"X-Anheart-Token": TOKEN}


# =========================================================================
# The route contract
# =========================================================================


def test_every_route_handler_is_a_coroutine_function(rig: Rig) -> None:
    """The invariant the lock-free design rests on.

    Starlette dispatches a synchronous endpoint to a thread pool. One ``def``
    where an ``async def`` belongs would let a request handler touch the control
    surface, the hub and the supervisor from another thread - and all three are
    lock-free precisely because nothing does.
    """
    paths = require_async_routes(rig.app)
    assert paths, "the walk must have looked at something"
    for expected in ("/healthz", "/api/status", "/api/session/estop", TELEMETRY_PATH):
        assert expected in paths


def test_the_route_walk_rejects_a_synchronous_handler(rig: Rig) -> None:
    """And it has to actually bite, or the test above proves nothing."""

    @rig.app.get("/sync-by-mistake")
    def offender() -> str:  # a deliberate mistake, dispatched to a thread pool
        return "this would run off the event loop"

    with pytest.raises(RuntimeError, match="not coroutine functions"):
        require_async_routes(rig.app)


def test_the_route_walk_refuses_a_route_it_cannot_inspect(rig: Rig) -> None:
    """A mount has no endpoint, so nothing could promise it runs on the loop."""
    from starlette.routing import BaseRoute, Mount  # noqa: PLC0415

    routes: list[BaseRoute] = rig.app.routes
    routes.append(Mount("/assets", routes=[]))
    with pytest.raises(RuntimeError, match="no endpoint to check"):
        require_async_routes(rig.app)


def test_the_route_walk_refuses_an_app_with_no_routes() -> None:
    """A walk that enumerates nothing must fail rather than pass vacuously."""
    with pytest.raises(RuntimeError, match="no routes were registered"):
        require_async_routes(FastAPI(openapi_url=None))


async def test_the_lifespan_rechecks_the_route_contract(rig: Rig) -> None:
    """The startup assertion runs on startup, not only at construction."""
    started: list[str] = []

    async def receive() -> dict[str, object]:
        return {"type": "lifespan.startup"} if not started else {"type": "lifespan.shutdown"}

    async def send(message: MutableMapping[str, object]) -> None:
        kind = message.get("type")
        assert isinstance(kind, str)
        started.append(kind)

    scope: dict[str, object] = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    await rig.app(scope, receive, send)
    assert "lifespan.startup.complete" in started


# =========================================================================
# Authentication
# =========================================================================


async def test_healthz_needs_no_token(client: httpx.AsyncClient) -> None:
    """The container healthcheck has no token, so this endpoint takes none."""
    response = await client.get("/healthz")
    assert response.status_code == HTTPStatus.OK
    assert parse(HealthRow, response).status == "ok"


async def test_healthz_reveals_nothing_about_the_session(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """It is unauthenticated, so it must not leak state about the person inside."""
    rig.surface.publish(make_snapshot())
    response = await client.get("/healthz")
    # The declared shape has exactly two fields, and the body has exactly those:
    # nothing about the session, the operator or the person in the machine.
    assert parse(HealthRow, response).service
    assert sorted(_keys(response)) == ["service", "status"]


async def test_the_api_refuses_a_request_with_no_token(client: httpx.AsyncClient) -> None:
    """Everything that reads state or commands the machine is behind the token."""
    response = await client.get("/api/status")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_the_api_refuses_a_wrong_token(client: httpx.AsyncClient) -> None:
    """A near-miss is a refusal, not a partial success."""
    response = await client.get("/api/status", headers={"X-Anheart-Token": TOKEN[:-1]})
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_the_api_accepts_the_configured_token(client: httpx.AsyncClient) -> None:
    """And the right one gets in."""
    response = await client.get("/api/status", headers=auth())
    assert response.status_code == HTTPStatus.OK
    assert parse(StatusRow, response).run_state == RunState.IDLE.value


async def test_every_command_endpoint_is_behind_the_token(client: httpx.AsyncClient) -> None:
    """Including the emergency stop: it is loud, not open."""
    for path in (
        "/api/session/start",
        "/api/session/stop",
        "/api/session/estop",
        "/api/safety/acknowledge",
        "/api/safety/attest",
        "/api/presence",
        "/api/plan/preview",
    ):
        response = await client.post(path, json={})
        assert response.status_code == HTTPStatus.UNAUTHORIZED, path


# =========================================================================
# The session flow
# =========================================================================


async def test_a_start_is_refused_until_the_estop_wiring_is_attested(
    client: httpx.AsyncClient,
) -> None:
    """The startup gate. 412, and the response carries the statement to make.

    While the drive's STO input is jumpered there is no independent way to
    remove torque, so the only real emergency stop is a wired mushroom. Software
    cannot see it, so it blocks until a human says so, by name, every boot.
    """
    response = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert response.status_code == HTTPStatus.PRECONDITION_FAILED
    assert "attested" in detail(response)


async def test_the_attestation_needs_both_confirmations(client: httpx.AsyncClient) -> None:
    """Two separate facts; one click must not answer for both."""
    response = await client.post(
        "/api/safety/attest",
        headers=auth(),
        json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": False},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_the_attestation_needs_a_name(client: httpx.AsyncClient) -> None:
    """An unattributable safety record is not a safety record."""
    response = await client.post(
        "/api/safety/attest",
        headers=auth(),
        json={"operator": "  ", "sto_jumper_removed": True, "mushroom_wired_nc": True},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_a_recorded_attestation_opens_the_gate(client: httpx.AsyncClient, rig: Rig) -> None:
    """And it is recorded with the exact statement that was made."""
    recorded = parse(
        AttestationRow,
        await client.post(
            "/api/safety/attest",
            headers=auth(),
            json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
        ),
    )
    assert recorded.operator == OPERATOR
    assert "STO" in recorded.statement
    assert isinstance(rig.supervisor.require_estop_confirmed(), Ok)


async def test_start_stop_flow(client: httpx.AsyncClient, rig: Rig) -> None:
    """The ordinary path: attest, start, the loop picks it up, stop."""
    rig.attest()

    started = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    assert parse(CommandRow, started).kind == "start"
    assert state_of(rig.surface) is RunState.STARTING

    # The loop picks the command up and reports itself running.
    command = rig.surface.take_command()
    assert isinstance(command, StartSession)
    assert command.profile_id == PROFILE_ID
    rig.surface.note_running()
    assert state_of(rig.surface) is RunState.RUNNING

    stopped = await client.post("/api/session/stop", headers=auth(), json={"operator": OPERATOR})
    assert stopped.status_code == HTTPStatus.ACCEPTED
    assert parse(CommandRow, stopped).kind == "end"
    assert state_of(rig.surface) is RunState.STOPPING
    assert isinstance(rig.surface.take_command(), EndSession)

    rig.surface.note_idle()
    assert state_of(rig.surface) is RunState.IDLE


async def test_a_second_start_is_refused(client: httpx.AsyncClient, rig: Rig) -> None:
    """A double click must be an answer on screen, not a queued second session.

    Refused while the surface is still STARTING - that is, before the loop has
    touched the first command - because "the loop has not got round to it" is
    not a reason to accept another one.
    """
    rig.attest()
    body = {"profile_id": PROFILE_ID, "operator": OPERATOR}
    first = await client.post("/api/session/start", headers=auth(), json=body)
    assert first.status_code == HTTPStatus.ACCEPTED

    second = await client.post("/api/session/start", headers=auth(), json=body)
    assert second.status_code == HTTPStatus.CONFLICT
    assert RunState.STARTING.value in detail(second)
    assert isinstance(rig.surface.pending, StartSession)


async def test_a_stop_with_nothing_running_is_refused(client: httpx.AsyncClient) -> None:
    """There is nothing to end, and saying 202 would claim otherwise."""
    response = await client.post("/api/session/stop", headers=auth(), json={"operator": OPERATOR})
    assert response.status_code == HTTPStatus.CONFLICT


async def test_a_second_stop_is_refused(client: httpx.AsyncClient, rig: Rig) -> None:
    """Already stopping is not a state that accepts another stop."""
    rig.attest()
    await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    first = await client.post("/api/session/stop", headers=auth(), json={"operator": OPERATOR})
    assert first.status_code == HTTPStatus.ACCEPTED
    second = await client.post("/api/session/stop", headers=auth(), json={"operator": OPERATOR})
    assert second.status_code == HTTPStatus.CONFLICT


async def test_a_start_needs_an_operator_name(client: httpx.AsyncClient, rig: Rig) -> None:
    """A session record nobody is answerable for is not a session record."""
    rig.attest()
    response = await client.post(
        "/api/session/start", headers=auth(), json={"profile_id": PROFILE_ID, "operator": " "}
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_a_start_for_an_unknown_profile_is_refused_before_submission(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """404 on the screen of whoever typed it, rather than a session that self-ends."""
    rig.attest()
    response = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": "no_such_profile", "operator": OPERATOR},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert rig.surface.pending is None
    assert state_of(rig.surface) is RunState.IDLE


async def test_a_start_with_a_malformed_profile_id_is_refused(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """The id keys stored session records, so its shape is checked."""
    rig.attest()
    response = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": "Not A Profile Id", "operator": OPERATOR},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_an_impossible_duration_override_is_refused(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """A total that leaves no HOLD is a refusal with the violation named."""
    rig.attest()
    response = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR, "total_duration_s": 60.0},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "hold_too_short" in detail(response)


# =========================================================================
# The emergency stop
# =========================================================================


async def test_emergency_stop_returns_before_the_loop_has_ticked_once(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """The guarantee the whole arrangement exists for.

    A "control loop" is started and immediately blocked on an await that never
    completes - which is what a stalled Modbus read, a hung BITalino or a slow
    disk looks like from here. The emergency stop must still latch and answer.

    The assertions are deliberately about *evidence that no tick happened*:
    ``ticks == 0`` and ``surface.latest is None``. A stop that merely happened
    to be fast would pass a timing assertion; only a stop that does not depend
    on the loop at all passes this one.
    """
    ticks = 0
    gate = asyncio.Event()

    async def stalled_loop() -> None:
        nonlocal ticks
        await gate.wait()  # the loop never gets to its first tick
        ticks += 1
        rig.surface.publish(make_snapshot())

    loop_task = asyncio.create_task(stalled_loop())
    await asyncio.sleep(0)  # let the loop reach its await

    response = await client.post(
        "/api/session/estop", headers=auth(), json={"operator": OPERATOR, "reason": "pain"}
    )

    assert response.status_code == HTTPStatus.OK
    assert ticks == 0, "the loop must not have run for the stop to have been independent"
    assert rig.surface.latest is None, "no tick published anything"
    assert rig.surface.estop_latched
    assert rig.supervisor.standing_action is SafetyAction.QUICK_STOP
    assert parse(EstopRow, response).action == "quick_stop"

    gate.set()
    await loop_task


async def test_the_emergency_stop_needs_neither_a_name_nor_a_reason(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """It must never be refused for a missing form field."""
    response = await client.post("/api/session/estop", headers=auth(), json={})
    assert response.status_code == HTTPStatus.OK
    assert rig.surface.estop_latched


async def test_the_emergency_stop_works_with_nothing_running(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """No session, no attestation, no profile: it still latches.

    A machine can be turning because a previous process died with the drive
    enabled, which is exactly the state in which this page has no session.
    """
    response = await client.post("/api/session/estop", headers=auth(), json={})
    assert response.status_code == HTTPStatus.OK
    assert state_of(rig.surface) is RunState.STOPPING


async def test_the_emergency_stop_claims_nothing_about_having_stopped(
    client: httpx.AsyncClient,
) -> None:
    """The receipt says the demand is latched; no field says the shaft stopped."""
    response = await client.post("/api/session/estop", headers=auth(), json={})
    receipt = parse(EstopRow, response)
    assert receipt.rule == "operator_estop"
    # No field on the receipt claims the shaft has stopped, and none may be
    # added: with STO jumpered the fastest stop available is the ramp.
    assert not any("stop" in key and key != "reason" for key in _keys(response))


async def test_a_start_is_refused_while_a_latched_verdict_stands(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """No automatic resumption of motion, anywhere."""
    rig.attest()
    await client.post("/api/session/estop", headers=auth(), json={})
    rig.surface.note_idle()
    response = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert "acknowledged" in detail(response)


async def test_acknowledging_without_releasing_the_mushroom_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """The default is False, so forgetting to ask fails closed."""
    await client.post("/api/session/estop", headers=auth(), json={})
    response = await client.post(
        "/api/safety/acknowledge", headers=auth(), json={"operator": OPERATOR}
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert "pulled back out" in detail(response)


async def test_acknowledging_needs_a_name(client: httpx.AsyncClient) -> None:
    """Putting the machine back into service is a human act with a name on it."""
    await client.post("/api/session/estop", headers=auth(), json={})
    response = await client.post(
        "/api/safety/acknowledge",
        headers=auth(),
        json={"operator": "", "estop_released": True},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_acknowledging_with_nothing_latched_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """Nothing to clear, and a 200 would suggest something was cleared."""
    response = await client.post(
        "/api/safety/acknowledge",
        headers=auth(),
        json={"operator": OPERATOR, "estop_released": True},
    )
    assert response.status_code == HTTPStatus.CONFLICT


async def test_acknowledging_clears_the_latch_and_allows_a_start(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """And the surface's own flag clears only because the supervisor accepted."""
    rig.attest()
    await client.post("/api/session/estop", headers=auth(), json={})
    rig.surface.note_idle()
    cleared = parse(
        AckRow,
        await client.post(
            "/api/safety/acknowledge",
            headers=auth(),
            json={"operator": OPERATOR, "estop_released": True},
        ),
    )
    assert "operator_estop" in cleared.cleared
    assert not rig.surface.estop_latched
    started = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert started.status_code == HTTPStatus.ACCEPTED


# =========================================================================
# Reading state
# =========================================================================


async def test_the_snapshot_endpoint_says_null_before_the_first_tick(
    client: httpx.AsyncClient,
) -> None:
    """ "No data yet" is a real answer, and inventing zeros would be a lie."""
    response = await client.get("/api/snapshot", headers=auth())
    assert response.status_code == HTTPStatus.OK
    assert response.text == "null"


async def test_the_snapshot_shows_all_four_speed_renderings(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """The mandatory simultaneous display: motor rpm, output rpm, Hz and g.

    Together, because the gearbox ratio is 49.79 and any one of them alone makes
    a fiftyfold confusion invisible. The output figure is checked against the
    ratio so a display that quietly showed motor rpm twice would fail.
    """
    rig.surface.publish(make_snapshot(measured_rpm=MotorRpm(200)))
    response = await client.get("/api/snapshot", headers=auth())
    measured = parse(SnapshotRow, response).measured
    # Hand-derived from the nameplate, not recomputed with the code under test:
    #   200 / 49.79        =  4.016870 output rpm
    #   200 / 1380 * 50    =  7.246377 Hz
    assert measured.motor_rpm == pytest.approx(200.0)
    assert measured.output_rpm == pytest.approx(4.016870, rel=1e-6)
    assert measured.hertz == pytest.approx(7.246377, rel=1e-6)
    assert measured.g_load is not None
    assert measured.g_load > 0.0
    # All four present in the body, so a page cannot render one of them alone.
    assert measured.resultant_g is not None
    assert measured.resultant_g == pytest.approx(math.sqrt(measured.g_load**2 + 1.0))
    assert sorted(_keys(response, "measured")) == [
        "g_load",
        "hertz",
        "motor_rpm",
        "output_rpm",
        "resultant_g",
    ]


async def test_the_snapshot_separates_measured_from_setpoint(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """Two fields, never one: a commanded zero is not a measured zero."""
    rig.surface.publish(make_snapshot(measured_rpm=MotorRpm(120), setpoint_rpm=MotorRpm(0)))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.setpoint.motor_rpm == pytest.approx(0.0)
    assert row.measured.motor_rpm == pytest.approx(120.0)


async def test_the_snapshot_carries_the_age_of_every_measurement(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """A number without its age is the dangerous form of that number."""
    rig.surface.publish(make_snapshot(hr_age=Seconds(9.0), drive_age=Seconds(5.0)))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.heart_rate is not None
    assert row.heart_rate.age_s == pytest.approx(9.0)
    assert row.heart_rate.stale is True
    assert row.drive_status_age_s == pytest.approx(5.0)
    assert row.drive_status_stale is True
    assert row.live_bpm is None, "a stale reading must not be offered as the live one"


async def test_an_untrustworthy_quality_yields_no_heart_rate(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """Mains hum has a perfectly steady rate. It is not a heart rate."""
    rig.surface.publish(make_snapshot(bpm=Bpm(50), quality=SignalQuality.MAINS_DOMINATED))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.heart_rate is not None
    assert row.heart_rate.quality == "mains_dominated"
    assert row.heart_rate.bpm is None
    assert row.live_bpm is None


async def test_a_fault_is_rendered_with_its_keypad_mnemonic(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """The operator is comparing this screen with the drive's own display."""
    report = FaultReport(
        fault=DriveFault.DC_BUS_OVERVOLTAGE, raw_code=RawRegister(9), message="ObF (LFT 9)"
    )
    rig.surface.publish(make_snapshot(fault=report))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.fault is not None
    assert row.fault.name == "dc_bus_overvoltage"
    assert row.fault.mnemonic
    assert row.fault.raw_code == 9


async def test_the_drive_state_travels_as_a_name_not_an_ordinal(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """``DriveState`` uses ``auto()``, so its integer renumbers on a reorder.

    A stored session keyed on the integer would silently change meaning; the
    name does not.
    """
    rig.surface.publish(make_snapshot(drive_state=DriveState.COMM_LOST))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.drive_state == "comm_lost"


async def test_the_status_page_reports_every_reason_not_to_start(
    client: httpx.AsyncClient,
) -> None:
    """One request, because the start button depends on all of them at once."""
    response = await client.get("/api/status", headers=auth())
    status = parse(StatusRow, response)
    assert status.attested is False
    assert status.estop_latched is False
    assert status.run_state == "idle"
    assert status.standing is None
    assert status.bind.loopback is True
    assert status.bind.token_required is True
    assert TOKEN not in response.text, "the status page must never echo the token"
    assert status.ports[0].device == "/dev/ttyUSB0"
    assert PROFILE_ID in status.profile_ids


async def test_the_presence_ping_feeds_the_attendant_rule(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """Before the first ping the answer is "never", which the rule reads as absent."""
    assert rig.surface.attendant_last_seen is None
    rig.clock.advance(Seconds(12.0))
    pinged = parse(
        PresenceRow,
        await client.post("/api/presence", headers=auth(), json={"operator": OPERATOR}),
    )
    assert pinged.at == pytest.approx(12.0)
    assert rig.surface.attendant_last_seen == pytest.approx(12.0)


async def test_a_snapshot_before_the_first_sample_has_no_heart_rate_object(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """``null``, not a zero and not an object full of blanks.

    The machine can be commanded before any beat has been detected, and a page
    that showed "0 bpm" there would be showing a heart rate nobody measured.
    """
    rig.surface.publish(make_snapshot(sampled=False))
    row = parse(SnapshotRow, await client.get("/api/snapshot", headers=auth()))
    assert row.heart_rate is None
    assert row.live_bpm is None


async def test_the_status_page_renders_a_standing_verdict(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """With the rule that asked, how long it has stood, and that it is latched."""
    rig.clock.advance(Seconds(5.0))
    await client.post("/api/session/estop", headers=auth(), json={"operator": OPERATOR})
    rig.clock.advance(Seconds(2.0))
    status = parse(StatusRow, await client.get("/api/status", headers=auth()))
    assert status.standing is not None
    assert status.standing.rule == "operator_estop"
    assert status.standing.action == "quick_stop"
    assert status.standing.latched is True
    assert status.standing.age_s == pytest.approx(2.0)


async def test_the_status_page_shows_a_stop_the_camera_latched_behind_go_silent(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """A page opened afterwards has this answer and nothing else to go by (ANH-182).

    The camera's stop goes straight to the supervisor: the interface's own flag
    stays down. Then the link to the drive is lost, and ``GO_SILENT`` takes the
    standing verdict and the floor. The stop is still latched, the
    acknowledgement would still demand the mushroom, and the status says so.
    """
    idle = parse(StatusRow, await client.get("/api/status", headers=auth()))
    assert idle.supervisor_estop is None

    rig.clock.advance(Seconds(5.0))
    rig.supervisor.latch_estop("camera presence: intrusion")
    rig.clock.advance(Seconds(2.0))
    rig.supervisor.trip_from_thread("drive_comm", SafetyAction.GO_SILENT, "link gone")
    rig.supervisor.evaluate(make_observation(at=rig.clock.monotonic()))

    status = parse(StatusRow, await client.get("/api/status", headers=auth()))
    assert status.estop_latched is False, "the interface's flag is not the camera's to set"
    assert status.standing is not None
    assert status.standing.action == "go_silent"
    assert status.floor is not None
    assert status.floor.action == "go_silent"
    held = status.supervisor_estop
    assert held is not None, "the stop latched behind GO_SILENT is in no field of the status"
    assert (held.rule, held.action, held.latched) == ("operator_estop", "quick_stop", True)
    assert "camera presence" in held.detail
    assert held.age_s == pytest.approx(2.0)


async def test_the_status_page_shows_the_stop_of_its_own_button_in_the_same_field(
    client: httpx.AsyncClient,
) -> None:
    """One field for the latch, whoever set it; an acknowledgement empties it."""
    await client.post("/api/session/estop", headers=auth(), json={"operator": OPERATOR})
    pressed = parse(StatusRow, await client.get("/api/status", headers=auth()))
    assert pressed.estop_latched is True
    assert pressed.supervisor_estop is not None
    assert pressed.supervisor_estop.rule == "operator_estop"

    acknowledged = await client.post(
        "/api/safety/acknowledge",
        headers=auth(),
        json={"operator": OPERATOR, "estop_released": True},
    )
    assert acknowledged.status_code == HTTPStatus.OK
    cleared = parse(StatusRow, await client.get("/api/status", headers=auth()))
    assert cleared.estop_latched is False
    assert cleared.supervisor_estop is None


async def test_the_ecg_endpoint_serves_only_what_is_new(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """The polling fallback for a page whose socket dropped.

    By sequence number, so a reconnecting page asks for what it is missing
    rather than for the whole ring on every request.
    """
    rig.hub.record_ecg([Millivolts(0.1), Millivolts(0.2)])
    first = parse(EcgRow, await client.get("/api/ecg", headers=auth()))
    assert len(first.samples) == 2
    assert first.samples[0] == pytest.approx(0.1)
    assert first.samples[1] == pytest.approx(0.2)
    assert first.fs_hz == 250

    rig.hub.record_ecg([Millivolts(0.3)])
    second = parse(EcgRow, await client.get(f"/api/ecg?after={first.seq}", headers=auth()))
    assert len(second.samples) == 1
    assert second.samples[0] == pytest.approx(0.3)
    assert second.gap is False


async def test_a_go_silent_verdict_cannot_be_acknowledged_away(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """GO_SILENT is one-way, and the refusal says why rather than just saying no.

    Ceasing to write hands the stop to a timer inside the drive, on the far side
    of the serial link. Nothing in this process resumes writing afterwards:
    recovery is an operator action on a machine that has demonstrably stopped.
    """
    rig.supervisor.trip_from_thread("drive_comm", SafetyAction.GO_SILENT, "link gone")
    rig.supervisor.evaluate(make_observation())

    refused = await client.post(
        "/api/safety/acknowledge",
        headers=auth(),
        json={"operator": OPERATOR, "estop_released": True},
    )
    assert refused.status_code == HTTPStatus.CONFLICT
    assert "one-way" in detail(refused)

    rig.attest()
    start = await client.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert start.status_code == HTTPStatus.CONFLICT


# =========================================================================
# Profiles and the dry-run preview
# =========================================================================


async def test_the_profiles_endpoint_lists_the_shipped_defaults(
    client: httpx.AsyncClient,
) -> None:
    """With the revision an edit has to send back."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    assert listing.rev >= 0
    assert PROFILE_ID in [profile.profile_id for profile in listing.profiles]


async def test_the_preview_resolves_a_plan_without_starting_anything(
    client: httpx.AsyncClient, rig: Rig
) -> None:
    """A dry run: the phase plan, the ceiling, and the g-load it implies."""
    preview = parse(
        PlanPreviewRow,
        await client.post("/api/plan/preview", headers=auth(), json={"profile_id": PROFILE_ID}),
    )
    assert [span.phase for span in preview.spans] == [
        "baseline",
        "warmup",
        "hold",
        "cooldown",
        "recovery",
    ]
    assert preview.ceiling.g_load is not None
    assert preview.ceiling.g_load > 0.0
    assert preview.total_overridden is False
    assert state_of(rig.surface) is RunState.IDLE
    assert rig.surface.pending is None


async def test_the_preview_applies_the_duration_override(client: httpx.AsyncClient) -> None:
    """HOLD absorbs the change; every fixed phase keeps its declared length."""
    preview = parse(
        PlanPreviewRow,
        await client.post(
            "/api/plan/preview",
            headers=auth(),
            json={"profile_id": PROFILE_ID, "total_duration_s": 2400.0},
        ),
    )
    assert preview.total_overridden is True
    assert preview.profile.total_duration_s == pytest.approx(2400.0)
    hold = next(span for span in preview.spans if span.phase == "hold")
    assert hold.duration_s == pytest.approx(2400.0 - 180.0 - 300.0 - 240.0 - 300.0)


async def test_the_preview_refuses_an_unknown_profile(client: httpx.AsyncClient) -> None:
    """With the known ids listed, because that is the actionable part."""
    response = await client.post("/api/plan/preview", headers=auth(), json={"profile_id": "nope"})
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert PROFILE_ID in detail(response)


async def test_a_profile_can_be_saved_and_read_back(client: httpx.AsyncClient, rig: Rig) -> None:
    """Round trip through the store, at the revision it was read at."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    document = profile_document("bench_short")
    saved = await client.put(
        f"/api/profiles/bench_short?rev={listing.rev}", headers=auth(), json=document
    )
    assert parse(ProfileRow, saved).profile_id == "bench_short"
    assert "bench_short" in [profile.profile_id for profile in rig.store.list_profiles()]


async def test_a_store_that_cannot_be_written_says_so_rather_than_traceback(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full or read-only disk is an operator-visible failure, not a 500 with no text.

    The store writes through a temporary file and one rename; failing the rename
    is what a wedged filesystem does. The response has to name the path, because
    "500" on its own sends somebody looking in the wrong place.
    """
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    monkeypatch.setattr(Path, "replace", _explode)
    response = await client.put(
        f"/api/profiles/bench_short?rev={listing.rev}",
        headers=auth(),
        json=profile_document("bench_short"),
    )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "profiles.json" in detail(response)


async def test_a_profile_whose_zone_is_too_high_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """The check the plan layer exists for, surfaced as a 422 with the reason.

    A zone above 90% of the subject's own maximum is a programme that asks the
    control law to drive somebody past their screening ceiling.
    """
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    document = profile_document("too_hot")
    document["zone_high_bpm"] = 200
    response = await client.put(
        f"/api/profiles/too_hot?rev={listing.rev}", headers=auth(), json=document
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "zone" in detail(response)


async def test_a_profile_save_at_a_stale_revision_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """Optimistic concurrency: the second editor is told, not overwritten."""
    document = profile_document("bench_short")
    response = await client.put("/api/profiles/bench_short?rev=999", headers=auth(), json=document)
    assert response.status_code == HTTPStatus.CONFLICT
    assert "revision" in detail(response)


async def test_a_profile_save_whose_url_and_body_disagree_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """Two names for one thing is how the wrong profile gets edited."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    document = profile_document("bench_short")
    response = await client.put(
        f"/api/profiles/other_name?rev={listing.rev}", headers=auth(), json=document
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_a_malformed_profile_document_is_refused(client: httpx.AsyncClient) -> None:
    """Untrusted JSON, parsed once, with every problem reported."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    response = await client.put(
        f"/api/profiles/broken?rev={listing.rev}",
        headers=auth(),
        json={"profile_id": "broken"},
    )
    assert response.status_code == HTTPStatus.BAD_REQUEST


async def test_a_profile_can_be_deleted(client: httpx.AsyncClient) -> None:
    """And the listing that comes back is the one after the deletion."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    remaining = parse(
        ProfileListRow,
        await client.delete(f"/api/profiles/{PROFILE_ID}?rev={listing.rev}", headers=auth()),
    )
    assert PROFILE_ID not in [profile.profile_id for profile in remaining.profiles]


async def test_deleting_at_a_stale_revision_is_refused(client: httpx.AsyncClient) -> None:
    """The same optimistic-concurrency answer a save gets, for the same reason."""
    response = await client.delete(f"/api/profiles/{PROFILE_ID}?rev=999", headers=auth())
    assert response.status_code == HTTPStatus.CONFLICT
    assert "revision" in detail(response)


async def test_deleting_an_unknown_profile_is_a_404(client: httpx.AsyncClient) -> None:
    """Rather than a silent success that looks like it worked."""
    listing = parse(ProfileListRow, await client.get("/api/profiles", headers=auth()))
    response = await client.delete(f"/api/profiles/nope?rev={listing.rev}", headers=auth())
    assert response.status_code == HTTPStatus.NOT_FOUND


# =========================================================================
# The page itself
# =========================================================================


async def test_the_page_is_served_with_no_token(client: httpx.AsyncClient) -> None:
    """The shell is inert HTML; every number in it comes from the gated API."""
    for path, fragment in (
        ("/", "Console du banc"),
        ("/app.css", "--bg"),
        ("/app.js", "STALE_FRAME_MS"),
    ):
        response = await client.get(path)
        assert response.status_code == HTTPStatus.OK, path
        assert fragment in response.text


async def test_the_page_references_nothing_off_this_machine() -> None:
    """No CDN, no web font, no analytics: the Pi serves this with no internet.

    A dashboard that renders blank because a script did not download is a
    dashboard that is absent at the moment somebody needs it.
    """
    static = REPO_ROOT / "src" / "web" / "static"
    for name in ("index.html", "app.css", "app.js"):
        source = (static / name).read_text(encoding="utf-8")
        for forbidden in ("http://", "https://", "//cdn", "integrity="):
            assert forbidden not in source, f"{name} reaches off the machine: {forbidden}"


async def test_the_page_carries_both_footer_stops_in_both_views() -> None:
    """The moment to stop the machine is not a moment for finding the right tab."""
    source = (REPO_ROOT / "src" / "web" / "static" / "index.html").read_text(encoding="utf-8")
    footer = source[source.index("<footer") :]
    assert 'id="stop"' in footer
    assert 'id="estop"' in footer


async def test_the_estop_button_asks_no_confirmation() -> None:
    """One that asks "are you sure?" is not an emergency stop."""
    script = (REPO_ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    estop = script[script.index("function doEstop") :]
    body = estop[: estop.index("function doAcknowledge")]
    assert "confirm(" not in body
    assert "/api/session/estop" in body


def test_every_element_the_script_looks_up_exists_in_the_page() -> None:
    """A renamed id is a field that stops updating while still looking live.

    The page has no framework to complain, so ``el("hr")`` on an element that no
    longer exists throws once, inside one render, and leaves the rest of the
    dashboard showing the previous values. That is the failure mode this whole
    interface is written against, so it is checked here instead.
    """
    static = REPO_ROOT / "src" / "web" / "static"
    script = (static / "app.js").read_text(encoding="utf-8")
    markup = (static / "index.html").read_text(encoding="utf-8")
    wanted = set(re.findall(r'el\("([^"]+)"\)', script))
    present = set(re.findall(r'id="([^"]+)"', markup))
    assert wanted, "the id extraction found nothing, so this test proves nothing"
    assert wanted <= present, f"the script looks up ids the page does not have: {wanted - present}"
    # The load-bearing ones, named so that deleting one is a failure here.
    for required in (
        "banner",
        "estop",
        "stop",
        "hr",
        "measured-output",
        "setpoint-output",
        "ecg",
        "console-hr",
        "console-output",
        "console-ecg",
        "console-motion",
    ):
        assert required in present


def test_the_motion_indicator_reads_the_measured_speed_not_the_setpoint() -> None:
    """A mandatory display rule, asserted rather than trusted to a comment.

    Commanded zero is not measured zero on a coasting mass: the drive can be
    reporting a zero reference while a loaded centrifuge turns for minutes. The
    "is it stopped" pill must therefore read the measured output speed, and must
    refuse to claim standstill on a stale reading.
    """
    script = (REPO_ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    body = script[script.index("function renderMotion") : script.index("function renderZoneBand")]
    assert "snapshot.measured.output_rpm" in body
    assert "setpoint" not in body, "the stopped indicator must not read the commanded speed"
    assert "drive_status_stale" in body, "a stale reading must not be reported as standstill"


async def test_a_missing_static_file_is_a_named_404(tmp_path: Path) -> None:
    """A diagnosable 404 rather than a bare 500 from inside the response."""
    rig = build_rig(tmp_path)
    broken = WebConfig(host="127.0.0.1", port=8080, token=TOKEN, static_dir=tmp_path / "gone")
    services = Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=GEOMETRY,
    )
    app = create_app(services=services, config=broken)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as session:
        response = await session.get("/")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "index.html" in detail(response)


# =========================================================================
# The WebSocket
# =========================================================================


async def probe_socket(
    app: FastAPI,
    *,
    origin: str | None,
    token: str | None,
    replies: int = 1,
    fail_on_send: Exception | None = None,
    on_first_frame: Callable[[], None] | None = None,
) -> tuple[MutableMapping[str, object], ...]:
    """Drive the ASGI websocket protocol by hand and return what was sent.

    By hand rather than through a test client, because the property under test
    is what happens **at the handshake**: a rejection has to be a refusal to
    upgrade, not an accept followed by a close. Only the raw message sequence
    distinguishes those two.
    """
    headers: list[tuple[bytes, bytes]] = [(b"host", b"127.0.0.1:8080")]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    query = b"" if token is None else f"token={token}".encode()
    scope: dict[str, object] = {
        "type": "websocket",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "ws",
        "path": TELEMETRY_PATH,
        "raw_path": TELEMETRY_PATH.encode(),
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 8080),
        "subprotocols": [],
        "state": {},
    }
    sent: list[MutableMapping[str, object]] = []
    incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    seen = 0

    async def receive() -> dict[str, object]:
        return await incoming.get()

    async def send(message: MutableMapping[str, object]) -> None:
        nonlocal seen
        sent.append(message)
        if fail_on_send is not None and message.get("type") == "websocket.send":
            # What a socket that has gone away looks like from inside the handler.
            raise fail_on_send
        if message.get("type") == "websocket.send":
            seen += 1
            if seen == 1 and on_first_frame is not None:
                on_first_frame()
            if seen >= replies:
                # Disconnect once we have what we came for, so the handler's
                # reader task sees the socket go and the stream winds down.
                incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})

    await app(scope, receive, send)
    return tuple(sent)


async def test_the_socket_handshake_is_refused_for_a_foreign_origin(rig: Rig) -> None:
    """WebSockets are not subject to CORS, and SameSite cookies do not help.

    Without this check any page open on the clinic wifi could open a socket to
    this machine, because a browser will make the connection from any origin and
    hand the result to that page's script. The refusal must land before
    ``accept``: a socket that is accepted and then closed has already been
    upgraded and the page has already been told it connected.
    """
    sent = await probe_socket(rig.app, origin=FOREIGN_ORIGIN, token=TOKEN)
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008
    reason = sent[0].get("reason")
    assert isinstance(reason, str)
    assert "origin" in reason


async def test_the_socket_handshake_is_refused_without_the_token(rig: Rig) -> None:
    """The socket carries the same secret the API does, in the query string."""
    sent = await probe_socket(rig.app, origin=ORIGIN, token=None)
    assert [message["type"] for message in sent] == ["websocket.close"]
    assert sent[0]["code"] == 1008


async def test_the_socket_accepts_an_allowed_origin_and_streams_state(rig: Rig) -> None:
    """The page's own origin gets in, and gets the current state immediately."""
    rig.surface.publish(make_snapshot())
    sent = await probe_socket(rig.app, origin=ORIGIN, token=TOKEN)
    kinds = [message["type"] for message in sent]
    assert kinds[0] == "websocket.accept"
    first = next(message for message in sent if message["type"] == "websocket.send")
    payload = first["text"]
    assert isinstance(payload, str)
    assert '"kind":"snapshot"' in payload
    assert rig.hub.client_count == 0, "the client must be released when the socket goes"


async def test_a_socket_with_no_origin_header_is_allowed(rig: Rig) -> None:
    """A browser cannot omit Origin, so an absent one is not the CSRF case.

    It is still subject to the token check, which the previous test pins.
    """
    rig.surface.publish(make_snapshot())
    sent = await probe_socket(rig.app, origin=None, token=TOKEN)
    assert sent[0]["type"] == "websocket.accept"


async def test_the_socket_delivers_an_event_frame(rig: Rig) -> None:
    """Events are never coalesced, so a stop that happened reaches the screen.

    A snapshot cannot carry this: an emergency stop that was latched and then
    acknowledged appears in no later snapshot at all.

    The event is raised while the socket is already streaming, which is how it
    happens in life: the operator presses a button while somebody is watching.
    The ring is seeded first so the handler has a frame to send, which is the
    moment the test can hook to know the client has subscribed.
    """
    rig.hub.record_ecg([Millivolts(0.1)])
    sent = await probe_socket(
        rig.app,
        origin=ORIGIN,
        token=TOKEN,
        replies=2,
        on_first_frame=lambda: rig.hub.publish_event(event("something happened")),
    )
    frames = [message["text"] for message in sent if message["type"] == "websocket.send"]
    joined = " ".join(value for value in frames if isinstance(value, str))
    assert '"kind":"event"' in joined
    assert "something happened" in joined


@pytest.mark.parametrize("failure", [WebSocketDisconnect(code=1006), RuntimeError("closed")])
async def test_a_socket_that_dies_mid_write_releases_its_client(
    rig: Rig, failure: Exception
) -> None:
    """A browser closing a tab is the normal case, not an error to propagate.

    Both shapes the failure takes are covered: Starlette raises
    ``WebSocketDisconnect`` when the peer has gone and ``RuntimeError`` when the
    close has already been sent. Treating only one of them would leak the other
    out of the handler.

    What must not happen either way is the hub keeping a client that no longer
    reads: that is the backpressure the bounded queue exists to keep away from
    the control loop.
    """
    rig.surface.publish(make_snapshot())
    sent = await probe_socket(rig.app, origin=ORIGIN, token=TOKEN, fail_on_send=failure)
    assert sent[0]["type"] == "websocket.accept"
    assert rig.hub.client_count == 0


async def test_the_socket_sends_the_ecg_ring_on_connection(rig: Rig) -> None:
    """So a page that connects mid-session has a trace rather than a blank box."""
    rig.hub.record_ecg([Millivolts(0.1), Millivolts(-0.2), Millivolts(0.3)])
    rig.surface.publish(make_snapshot())
    sent = await probe_socket(rig.app, origin=ORIGIN, token=TOKEN, replies=2)
    texts = [message["text"] for message in sent if message["type"] == "websocket.send"]
    joined = " ".join(value for value in texts if isinstance(value, str))
    assert '"kind":"ecg"' in joined
    assert '"fs_hz":250' in joined


# =========================================================================
# The control surface
# =========================================================================


def state_of(surface: ControlSurface) -> RunState:
    """``surface.run_state``, read through a call.

    mypy narrows a property access and then treats the next read of the same
    expression as the already-narrowed type, so "it was IDLE, then it became
    STARTING" is reported as an unreachable statement. Reading through a
    function re-widens it. The same workaround appears in the safety tests.
    """
    return surface.run_state


def surface_rig() -> tuple[ManualClock, SafetySupervisor, RecordingSink, ControlSurface]:
    """A surface with a recording sink, for the mailbox tests."""
    clock = ManualClock()
    supervisor = SafetySupervisor(clock=clock, limits=build_limits())
    sink = RecordingSink()
    surface = ControlSurface(clock=clock, supervisor=supervisor, sink=sink)
    return (clock, supervisor, sink, surface)


def test_the_mailbox_holds_exactly_one_command() -> None:
    """A queue would stack a start behind a stop. A slot makes the second a refusal."""
    _clock, supervisor, _sink, surface = surface_rig()
    assert isinstance(supervisor.confirm_estop_wiring(OPERATOR), Ok)
    first = surface.submit_start(profile_id=PROFILE_ID, operator=OPERATOR, total_duration_s=None)
    assert isinstance(first, Ok)
    second = surface.submit_start(profile_id=PROFILE_ID, operator=OPERATOR, total_duration_s=None)
    assert isinstance(second, Err)
    taken = surface.take_command()
    assert taken is first.value
    assert surface.take_command() is None, "a command must not be executable twice"


def test_the_run_state_is_pessimistic_while_stopping() -> None:
    """ "Asked to stop and not yet stopped" is a machine that is still turning."""
    _clock, supervisor, _sink, surface = surface_rig()
    assert isinstance(supervisor.confirm_estop_wiring(OPERATOR), Ok)
    assert state_of(surface) is RunState.IDLE
    assert isinstance(
        surface.submit_start(profile_id=PROFILE_ID, operator=OPERATOR, total_duration_s=None), Ok
    )
    assert state_of(surface) is RunState.STARTING
    surface.note_running()
    assert state_of(surface) is RunState.RUNNING
    assert isinstance(surface.submit_end(operator=OPERATOR, reason="done"), Ok)
    assert state_of(surface) is RunState.STOPPING
    surface.note_idle()
    assert state_of(surface) is RunState.IDLE


def test_an_emergency_stop_holds_the_state_until_it_is_acknowledged() -> None:
    """A session ending is not somebody having answered for why it was stopped."""
    _clock, _supervisor, _sink, surface = surface_rig()
    surface.submit_estop(operator=OPERATOR, reason="pain")
    surface.note_idle()
    assert state_of(surface) is RunState.STOPPING
    assert isinstance(surface.acknowledge(OPERATOR, estop_released=True), Ok)
    assert state_of(surface) is RunState.IDLE


def test_a_refused_acknowledgement_leaves_the_latch_alone() -> None:
    """The screen and the supervisor must not be able to disagree about this."""
    _clock, supervisor, _sink, surface = surface_rig()
    surface.submit_estop(operator=OPERATOR, reason="pain")
    refused = surface.acknowledge(OPERATOR, estop_released=False)
    assert isinstance(refused, Err)
    assert surface.estop_latched
    assert supervisor.standing_action is SafetyAction.QUICK_STOP


def test_the_estop_receipt_stamps_the_supervisors_own_latch_instant() -> None:
    """One instant, one meaning: two clock reads could disagree in the log."""
    clock, _supervisor, _sink, surface = surface_rig()
    clock.advance(Seconds(3.25))
    receipt = surface.submit_estop(operator=OPERATOR, reason="pain")
    assert receipt.at == receipt.verdict.since
    assert receipt.at == pytest.approx(3.25)


def test_the_estop_publishes_exactly_one_event() -> None:
    """Three steps, and the third is the publish. No more, no fewer."""
    _clock, _supervisor, sink, surface = surface_rig()
    surface.submit_estop(operator=OPERATOR, reason="pain")
    assert [event.kind for event in sink.events] == [EventKind.EMERGENCY_STOP]


def test_the_loop_takes_the_estop_receipt_once_but_the_latch_persists() -> None:
    """The demand outlives the loop's reaction to it."""
    _clock, _supervisor, _sink, surface = surface_rig()
    surface.submit_estop(operator=OPERATOR, reason="pain")
    assert surface.take_estop() is not None
    assert surface.take_estop() is None
    assert surface.estop_latched


def test_an_end_request_fills_the_mailbox() -> None:
    """The stop reaches the loop through the mailbox, and through nothing else."""
    _clock, supervisor, _sink, surface = surface_rig()
    assert isinstance(supervisor.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        surface.submit_start(profile_id=PROFILE_ID, operator=OPERATOR, total_duration_s=None), Ok
    )
    surface.note_running()
    assert isinstance(surface.submit_end(operator=OPERATOR, reason="done"), Ok)
    assert isinstance(surface.pending, EndSession)


def test_note_running_and_note_idle_are_idempotent() -> None:
    """The loop reports every tick; the event stream must not."""
    _clock, supervisor, sink, surface = surface_rig()
    assert isinstance(supervisor.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        surface.submit_start(profile_id=PROFILE_ID, operator=OPERATOR, total_duration_s=None), Ok
    )
    surface.note_running()
    surface.note_running()
    surface.note_idle()
    surface.note_idle()
    kinds = [event.kind for event in sink.events]
    assert kinds.count(EventKind.SESSION_RUNNING) == 1
    assert kinds.count(EventKind.SESSION_IDLE) == 1


def test_the_presence_ping_publishes_no_event() -> None:
    """It happens every few seconds; it would drown the event stream."""
    _clock, _supervisor, sink, surface = surface_rig()
    surface.note_presence(OPERATOR)
    surface.note_presence(OPERATOR)
    assert sink.events == []


def test_publishing_stores_the_snapshot_before_fanning_it_out() -> None:
    """``GET /api/snapshot`` is the fallback when a socket drops; it must be current."""
    _clock, _supervisor, sink, surface = surface_rig()
    snapshot = make_snapshot()
    surface.publish(snapshot)
    assert surface.latest is snapshot
    assert sink.snapshots == [snapshot]
    assert surface.counters[2] == 1


def test_the_control_surface_never_awaits() -> None:
    """The lock-free argument in that module rests on exactly this property.

    No ``async def``, no ``await``: a call therefore runs to completion before
    the other side of the event loop can observe anything, which is what makes
    the absence of locks safe rather than lucky.
    """
    tree = ast.parse((REPO_ROOT / "src" / "control_surface.py").read_text(encoding="utf-8"))
    offenders = [
        type(node).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.Await | ast.AsyncWith | ast.AsyncFor)
    ]
    assert not offenders, (
        "src/control_surface.py has become asynchronous, which voids the reasoning "
        f"that justifies its absence of locks: {offenders}"
    )


def test_the_control_surface_takes_no_locks() -> None:
    """A lock here would let a request handler block the control loop."""
    source = (REPO_ROOT / "src" / "control_surface.py").read_text(encoding="utf-8")
    for forbidden in ("threading.Lock", "asyncio.Lock", "RLock", "Semaphore"):
        assert forbidden not in source


# =========================================================================
# The telemetry hub
# =========================================================================


def hub_with_clock() -> tuple[ManualClock, TelemetryHub]:
    """A hub whose pacing can be advanced exactly."""
    clock = ManualClock()
    return (clock, TelemetryHub(clock=clock))


async def test_snapshots_coalesce_into_one_slot() -> None:
    """A missed snapshot loses nothing: the next one carries the whole state.

    So the queue is one slot deep and the newest wins. The client sees the
    LATEST, not a backlog, which is the difference between a bounded queue and
    a slow-motion replay of the session.
    """
    clock, hub = hub_with_clock()
    client = hub.subscribe()
    for index in range(5):
        clock.advance(Seconds(0.2))
        hub.publish_snapshot(make_snapshot(at=Monotonic(float(index))))
    payload = await client.next_payload()
    assert payload.kind is PayloadKind.SNAPSHOT
    assert payload.snapshot is not None
    assert payload.snapshot.at == pytest.approx(4.0), "the newest snapshot, not the oldest"
    assert client.stats.coalesced == 4


async def test_events_are_never_coalesced() -> None:
    """An acknowledged stop appears in no later snapshot; it must survive."""
    clock, hub = hub_with_clock()
    client = hub.subscribe()
    for index in range(3):
        hub.publish_event(event(f"event {index}"))
        clock.advance(Seconds(0.1))
    details: list[str] = []
    for _ in range(3):
        payload = await client.next_payload()
        assert payload.event is not None
        details.append(payload.event.detail)
    assert details == ["event 0", "event 1", "event 2"]


async def test_a_client_that_will_not_read_is_evicted_rather_than_served() -> None:
    """The bound that protects the control loop, and it drops the CLIENT.

    Eight queued messages at 5 Hz is more than a second behind. Rather than
    discarding the ninth event - which could be the emergency stop - the socket
    is closed with a resync notice, so the page shows its banner and reloads.
    """
    _clock, hub = hub_with_clock()
    client = hub.subscribe()
    for index in range(CLIENT_QUEUE_LIMIT + 1):
        hub.publish_event(event(f"event {index}"))
    assert client.evicted
    assert hub.client_count == 0, "the hub stops publishing to it immediately"
    payload = await client.next_payload()
    assert payload.kind is PayloadKind.RESYNC
    assert "fell behind" in payload.notice


async def test_an_evicted_client_is_told_before_anything_else() -> None:
    """It is being disconnected; handing it more state would be misleading."""
    _clock, hub = hub_with_clock()
    client = hub.subscribe()
    hub.publish_snapshot(make_snapshot())
    client.evict("test eviction")
    payload = await client.next_payload()
    assert payload.kind is PayloadKind.RESYNC
    assert payload.snapshot is None


async def test_an_evicted_client_is_fed_nothing_further() -> None:
    """Its queue is the thing that was bounded, so it must stop growing at once."""
    clock, hub = hub_with_clock()
    client = hub.subscribe()
    client.evict("test eviction")
    # Offered directly to the CLIENT rather than through the hub, because the hub
    # reaps an evicted client after its first publish and the property under test
    # is that the client itself refuses to queue anything more.
    client.offer_event(event("after the eviction"))
    client.offer_snapshot(make_snapshot(), clock.monotonic())
    clock.advance(Seconds(1.0))
    assert client.stats.events == 0
    assert client.stats.coalesced == 0
    payload = await client.next_payload()
    assert payload.kind is PayloadKind.RESYNC
    assert payload.notice == "test eviction"


def test_evicting_twice_keeps_the_first_reason() -> None:
    """The first explanation is the true one; the second would overwrite it."""
    _clock, hub = hub_with_clock()
    client = hub.subscribe()
    client.evict("fell behind")
    client.evict("something else")
    assert client.evicted


async def test_a_new_client_is_primed_with_the_current_state() -> None:
    """A page connecting mid-session must not show blanks until the next tick."""
    _clock, hub = hub_with_clock()
    hub.publish_snapshot(make_snapshot())
    client = hub.subscribe()
    payload = await client.next_payload()
    assert payload.kind is PayloadKind.SNAPSHOT


async def test_a_client_with_no_state_yet_waits_rather_than_inventing_some() -> None:
    """No snapshot is a real answer; zeros would be a fabricated one."""
    _clock, hub = hub_with_clock()
    client = hub.subscribe()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.next_payload(), timeout=0.05)


async def test_a_client_is_released_no_faster_than_the_pacing_interval() -> None:
    """The bound, asserted at its boundary rather than described.

    A producer faster than the gate does not queue up: the slot holds the newest
    snapshot and the consumer is woken only once the interval has elapsed. The
    clock is advanced by exact amounts, so this is a statement about the
    arithmetic and not about how fast the machine ran.
    """
    clock, hub = hub_with_clock()
    client = hub.subscribe()
    hub.publish_snapshot(make_snapshot(at=Monotonic(1.0)))
    first = await client.next_payload()
    assert first.snapshot is not None

    # Still inside the interval: the newest snapshot is held, not released.
    clock.advance(Seconds(DEFAULT_MIN_INTERVAL / 2))
    hub.publish_snapshot(make_snapshot(at=Monotonic(2.0)))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.next_payload(), timeout=0.02)

    # Past it, and the release carries the NEWEST state, not the one it held.
    clock.advance(Seconds(DEFAULT_MIN_INTERVAL))
    hub.publish_snapshot(make_snapshot(at=Monotonic(3.0)))
    second = await client.next_payload()
    assert second.snapshot is not None
    assert second.snapshot.at == pytest.approx(3.0)


def test_the_pacing_gate_sits_below_one_control_period() -> None:
    """Set at exactly one period, float jitter would halve the visible rate.

    The producer is the 5 Hz control loop (0.2 s). The gate is 0.15 s, so normal
    operation is never throttled and only a producer faster than about 6.7 Hz is
    coalesced.
    """
    assert DEFAULT_MIN_INTERVAL < 0.2


def test_releasing_a_client_twice_is_harmless() -> None:
    """A socket can fail on either side, so teardown runs more than once."""
    _clock, hub = hub_with_clock()
    client = hub.subscribe()
    hub.release(client)
    hub.release(client)
    assert hub.client_count == 0


# =========================================================================
# The ECG ring
# =========================================================================


def test_the_ecg_ring_is_bounded() -> None:
    """A 45-minute session must cost a fixed amount of memory for its trace."""
    ring = EcgRing(fs_hz=250, span=Seconds(2.0))
    assert ring.capacity == 500
    ring.extend([Millivolts(float(index)) for index in range(2000)])
    assert len(ring.window().samples) == 500
    assert ring.written == 2000


def test_the_ecg_ring_serves_only_what_is_new() -> None:
    """So a page polls for the newest samples rather than for all of them."""
    ring = EcgRing(fs_hz=10, span=Seconds(10.0))
    ring.extend([Millivolts(1.0), Millivolts(2.0)])
    first = ring.window()
    ring.extend([Millivolts(3.0)])
    second = ring.window(after=first.seq)
    assert second.samples == (3.0,)
    assert second.gap is False


def test_the_ecg_ring_reports_a_gap_rather_than_splicing() -> None:
    """Joining two ends of a discontinuity draws a stroke that reads as a QRS.

    So the page is told, and breaks the line instead.
    """
    ring = EcgRing(fs_hz=10, span=Seconds(1.0))
    ring.extend([Millivolts(float(index)) for index in range(10)])
    stale_cursor = ring.window().seq
    ring.extend([Millivolts(float(index)) for index in range(20)])
    window = ring.window(after=stale_cursor)
    assert window.gap is True


def test_asking_for_samples_after_the_newest_yields_nothing() -> None:
    """An idle machine costs one frame per tick, not two."""
    ring = EcgRing(fs_hz=10, span=Seconds(1.0))
    ring.extend([Millivolts(1.0)])
    window = ring.window(after=ring.written)
    assert window.samples == ()
    assert window.gap is False


def test_a_truncated_window_is_reported_as_a_gap() -> None:
    """A limit that drops samples is a discontinuity like any other."""
    ring = EcgRing(fs_hz=10, span=Seconds(10.0))
    ring.extend([Millivolts(float(index)) for index in range(10)])
    window = ring.window(limit=3)
    assert len(window.samples) == 3
    assert window.gap is True


def test_a_zero_limit_asks_for_the_sequence_number_and_not_for_the_ring() -> None:
    """``wanted[-0:]`` is ``wanted[0:]``, so the naive form returns everything.

    The status page asks this question on every poll, and answering it with the
    whole six-second ring would be both wrong and expensive.
    """
    ring = EcgRing(fs_hz=10, span=Seconds(10.0))
    ring.extend([Millivolts(float(index)) for index in range(5)])
    window = ring.window(limit=0)
    assert window.samples == ()
    assert window.seq == 5
    assert window.fs_hz == 10
    assert window.gap is False


@pytest.mark.parametrize(("fs_hz", "span"), [(0, Seconds(1.0)), (250, Seconds(0.0))])
def test_the_ecg_ring_refuses_nonsense_geometry(fs_hz: int, span: Seconds) -> None:
    """A zero-length ring would render an empty trace as if it were a flat line."""
    with pytest.raises(ValueError, match="must be positive"):
        EcgRing(fs_hz=fs_hz, span=span)


# =========================================================================
# Binding and authorisation configuration
# =========================================================================


def test_serving_off_loopback_without_a_token_is_refused() -> None:
    """This endpoint starts and stops a motor with a person in the machine.

    A warning would be read once during commissioning and then never again, so
    this is a refusal to start.
    """
    with pytest.raises(ValueError, match="without a token"):
        WebConfig(host="192.168.1.20", port=8080, token=None)


def test_serving_off_loopback_with_a_short_token_is_refused() -> None:
    """The floor exists to refuse "admin", not to compute an entropy budget."""
    with pytest.raises(ValueError, match="characters are required"):
        WebConfig(host="192.168.1.20", port=8080, token="x" * (MIN_TOKEN_LENGTH - 1))


def test_serving_off_loopback_with_a_real_token_is_allowed() -> None:
    """A tablet at a fixed address is a real deployment, not a mistake."""
    config = WebConfig(host="192.168.1.20", port=8080, token="x" * MIN_TOKEN_LENGTH)
    assert config.requires_token
    assert not config.is_loopback


def test_loopback_needs_no_token() -> None:
    """A browser on the Pi itself is the default deployment."""
    config = WebConfig()
    assert config.is_loopback
    assert not config.requires_token
    assert config.token_matches(None)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "127.5.5.5"])
def test_loopback_hosts_are_recognised(host: str) -> None:
    """Including the whole 127/8 block and the IPv6 loopback."""
    assert WebConfig(host=host, port=8080).is_loopback


@pytest.mark.parametrize("host", ["192.168.1.20", "0.0.0.0", "pi.local", "10.0.0.1"])  # noqa: S104
def test_non_loopback_hosts_are_treated_as_a_network_bind(host: str) -> None:
    """Anything not demonstrably loopback fails closed, hostnames included."""
    with pytest.raises(ValueError, match="refusing to serve"):
        WebConfig(host=host, port=8080, token=None)


def test_an_impossible_port_is_refused() -> None:
    """Caught at construction rather than at bind time."""
    with pytest.raises(ValueError, match="port must be"):
        WebConfig(host="127.0.0.1", port=70000)


def test_the_origin_allowlist_covers_the_bound_address() -> None:
    """Whichever of the two names the operator typed into the browser."""
    config = WebConfig(host="127.0.0.1", port=8080)
    assert config.origin_allowed("http://127.0.0.1:8080")
    assert config.origin_allowed("http://localhost:8080")
    assert not config.origin_allowed(FOREIGN_ORIGIN)


def test_an_extra_origin_can_be_allowed_explicitly() -> None:
    """An allowlist, never a wildcard: "any origin" on a socket means any page."""
    config = WebConfig(
        host="192.168.1.20",
        port=8080,
        token="x" * MIN_TOKEN_LENGTH,
        extra_origins=("http://tablet.local",),
    )
    assert config.origin_allowed("http://tablet.local")
    assert not config.origin_allowed("http://other.local")


def test_a_wrong_token_never_matches() -> None:
    """Constant-time, and length differences do not throw."""
    config = WebConfig(token=TOKEN)
    assert config.token_matches(TOKEN)
    assert not config.token_matches(TOKEN[:-1])
    assert not config.token_matches(None)
    assert not config.token_matches("")


# =========================================================================
# Machine geometry
# =========================================================================


def test_the_geometry_has_no_default_radius() -> None:
    """It is a measurement of this rig, and a default is how a wrong one is used.

    ``g`` is linear in the radius, so a guessed value misstates the load an
    operator approves a programme on.
    """
    with pytest.raises(TypeError):
        MachineGeometry()  # type: ignore[call-arg]  # the point of the test


@pytest.mark.parametrize(
    "kwargs",
    [
        {"radius": Metres(0.0)},
        {"radius": Metres(-1.0)},
        {"radius": Metres(float("nan"))},
        {"radius": Metres(1.0), "ratio": GearRatio(0.0)},
        {"radius": Metres(1.0), "nominal_rpm": MotorRpm(0)},
        {"radius": Metres(1.0), "base_hz": Hertz(float("inf"))},
    ],
)
def test_the_geometry_refuses_values_that_would_make_every_speed_nonsense(
    kwargs: dict[str, object],
) -> None:
    """A non-finite or non-positive value would propagate into every rendering."""
    with pytest.raises(ValueError, match="positive and finite"):
        MachineGeometry(**kwargs)  # type: ignore[arg-type]  # deliberately wrong values


def test_the_geometry_renders_the_gear_ratio_it_was_given() -> None:
    """A fiftyfold error is the one this rendering exists to make visible.

    The expected numbers are derived by hand from the nameplate rather than
    recomputed with the functions under test, so they cannot agree with a bug in
    ``src/units.py``. At 1380 motor rpm and r = 1.5 m::

        output = 1380 / 49.79            = 27.716410 rpm
        omega  = 2*pi*27.716410 / 60     =  2.902458 rad/s
        g      = omega^2 * 1.5 / 9.80665 =  1.288553
    """
    speeds = MachineGeometry(radius=Metres(1.5)).view(MotorRpm(1380))
    assert speeds.motor_rpm == 1380
    assert speeds.output_rpm == pytest.approx(27.716410, rel=1e-6)
    assert speeds.hertz == pytest.approx(50.0)
    assert speeds.g_load == pytest.approx(1.288553, rel=1e-5)


# =========================================================================
# Serialization
# =========================================================================


def test_a_non_finite_number_becomes_null_rather_than_nan() -> None:
    """JSON has no NaN, and a page given one renders "NaN bpm" or draws a zero."""
    row = SnapshotRow.of(
        make_snapshot(hr_age=Seconds(float("nan")), drive_age=Seconds(float("inf")))
    )
    assert row.heart_rate is not None
    assert row.heart_rate.age_s is None
    assert row.drive_status_age_s is None


def test_the_safety_action_travels_as_a_name_and_a_rank() -> None:
    """The name is what an operator reads; the rank is what a page can compare.

    Sending the ``IntEnum`` alone would put a bare severity number on the wire
    with nothing saying what it means.
    """
    snapshot = make_snapshot()
    row = SnapshotRow.of(snapshot)
    assert row.safety_action == "none"
    assert row.safety_rank == int(SafetyAction.NONE)


def test_an_envelope_round_trips_through_json() -> None:
    """The one encoder, used for every frame the socket writes."""
    encoded = encode_envelope(WsEnvelope.of_ecg(EcgRing(fs_hz=250).window()))
    assert '"kind":"ecg"' in encoded
    assert '"fs_hz":250' in encoded


# =========================================================================
# The server
# =========================================================================


def test_the_server_does_not_take_the_processs_signal_handlers(rig: Rig) -> None:
    """SIGINT and SIGTERM belong to the session process, whose shutdown stops the motor.

    uvicorn's own handler would shut the web server down and let the process
    exit with the drive still enabled - and with STO jumpered, an exiting
    process is a centrifuge coasting on its last setpoint until the drive's own
    ``ttO`` timeout notices the keepalive stopped.
    """
    import uvicorn  # noqa: PLC0415

    server = build_server(rig.app, rig.config)
    assert isinstance(server, LoopSafeServer)
    assert "capture_signals" in vars(LoopSafeServer), (
        "the override that neutralises uvicorn's signal handling has gone"
    )
    hooks = [name for name in vars(uvicorn.Server) if "signal" in name.lower()]
    assert hooks, "uvicorn has no signal machinery under a name we recognise any more"
    for name in hooks:
        assert name in vars(LoopSafeServer), f"uvicorn.Server.{name} is not neutralised"


def test_entering_the_signal_context_installs_no_handler(rig: Rig) -> None:
    """The override does nothing, which is the whole point of overriding it.

    Asserted by comparing the process's SIGINT handler across the context: the
    base class replaces it and restores it, and this subclass must leave it
    exactly where it was, because that handler is the session process's - the
    one whose shutdown path stops the motor.
    """
    import signal  # noqa: PLC0415

    server = build_server(rig.app, rig.config)
    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is before, (
            "uvicorn has installed its own handler; the process no longer owns its signals"
        )
    assert signal.getsignal(signal.SIGINT) is before


async def test_the_server_serves_and_stops_without_touching_the_signal_handlers(
    rig: Rig,
) -> None:
    """A real bind, a real lifespan, and the process keeps its own signal handlers.

    An ephemeral port, and ``should_exit`` set before ``serve`` so the main loop
    exits on its first pass. It is worth doing for real: this is the path where
    the startup assertion runs, where uvicorn would install its handlers, and
    where ``log_config`` would otherwise be applied.
    """
    import signal  # noqa: PLC0415

    config = WebConfig(host="127.0.0.1", port=free_port(), token=TOKEN)
    server = build_server(rig.app, config)
    before = signal.getsignal(signal.SIGINT)
    server.should_exit = True
    await serve(server)
    assert signal.getsignal(signal.SIGINT) is before
    assert server.started is False or server.should_exit


async def test_a_server_binds_while_a_neighbour_still_holds_its_own_port(rig: Rig) -> None:
    """ANH-183 EX-3: two tests that bind at the same moment are given two ports.

    The neighbour stands for the other bind test, running in another process
    of the gate. When both used one fixed port, the second bind failed.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as neighbour:
        neighbour.bind(("127.0.0.1", free_port()))
        neighbour.listen()
        taken: int = neighbour.getsockname()[1]  # pyright: ignore[reportAny]
        port = free_port()
        assert port != taken
        server = build_server(rig.app, WebConfig(host="127.0.0.1", port=port, token=TOKEN))
        server.should_exit = True
        await serve(server)
        # The neighbour was never disturbed: it still holds what it bound.
        assert neighbour.getsockname()[1] == taken


def test_the_server_does_not_replace_the_applications_logging(rig: Rig) -> None:
    """uvicorn's default config runs ``dictConfig``, which disables existing loggers.

    The session's own logging is configured before the interface starts, so
    letting uvicorn install its own would silence it the moment the page came up.
    """
    server = build_server(rig.app, rig.config)
    assert server.config.log_config is None
    assert server.config.access_log is False


def test_the_signal_hook_check_reports_an_unneutralised_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check has to bite, or it is decoration.

    The hook has already been renamed once - ``install_signal_handlers`` became
    the ``capture_signals`` context manager - and an override of a method that
    no longer exists neutralises nothing, silently.
    """
    from src.web import app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "SIGNAL_HOOK_HINT", "serve")
    assert app_module.unneutralised_signal_hooks()


def test_building_a_server_refuses_an_unneutralised_hook(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the refusal happens where the server is built, not at the first signal."""
    from src.web import app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "SIGNAL_HOOK_HINT", "serve")
    with pytest.raises(RuntimeError, match="signal machinery"):
        build_server(rig.app, rig.config)


# =========================================================================
# Port discovery
# =========================================================================


async def test_port_discovery_survives_a_machine_with_no_dev(tmp_path: Path) -> None:
    """A Windows bench has no ``/dev``; the status page says "none", not "error".

    Discovery is a convenience. The port actually used comes from configuration.
    """
    lister = FilesystemPortLister(root=tmp_path / "not-here", by_id=tmp_path / "nor-here")
    assert await lister.list_ports() == ()


async def test_port_discovery_never_raises_when_the_filesystem_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged USB subsystem must not turn the status page into a 500.

    Both blocking calls are made to fail: a ``glob`` that raises, and a
    ``resolve`` that raises on a symlink loop (which ``/dev/serial/by-id`` can
    genuinely present). Discovery is a convenience - the port actually used comes
    from configuration - so the correct answer is a short list, never an error.
    """
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-FTDI_drive-if00-port0").touch()
    monkeypatch.setattr(Path, "glob", _explode)
    monkeypatch.setattr(Path, "resolve", _explode)
    found = await FilesystemPortLister(root=tmp_path, by_id=by_id).list_ports()
    assert [port.description for port in found] == ["usb-FTDI_drive-if00-port0"]


async def test_port_discovery_prefers_the_by_id_names(tmp_path: Path) -> None:
    """A by-id name identifies the adapter; ``ttyUSB0`` identifies the plug order.

    Which matters the moment somebody unplugs the BITalino and plugs it back in:
    the drive's adapter can change number, and an operator picking a port from
    this list needs to see which device it is.
    """
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    (by_id / "usb-FTDI_drive-if00-port0").touch()
    found = await FilesystemPortLister(root=tmp_path / "dev", by_id=by_id).list_ports()
    assert [port.description for port in found] == ["usb-FTDI_drive-if00-port0"]


async def test_port_discovery_finds_tty_devices(tmp_path: Path) -> None:
    """By pattern under the given root, with no vendor library involved.

    ``serial.tools.list_ports`` would be richer, but contract rule 5 reserves
    ``serial`` for ``src/motor/atv320.py``, so a richer lister belongs behind
    the ``PortLister`` protocol rather than in the web layer.
    """
    (tmp_path / "ttyUSB0").touch()
    (tmp_path / "ttyACM1").touch()
    (tmp_path / "random").touch()
    found = await FilesystemPortLister(root=tmp_path, by_id=tmp_path / "absent").list_ports()
    devices = [port.device for port in found]
    assert any(device.endswith("ttyUSB0") for device in devices)
    assert any(device.endswith("ttyACM1") for device in devices)
    assert not any(device.endswith("random") for device in devices)


# =========================================================================
# Small helpers used above
# =========================================================================


@dataclass(frozen=True, slots=True)
class ErrorBody:
    """The shape FastAPI returns for a refusal."""

    detail: str


def parse[T](shape: type[T], response: httpx.Response) -> T:
    """Validate a successful response against the schema it claims to return.

    Through the response dataclass rather than by indexing a decoded ``dict``,
    and for two reasons. It types every field these assertions touch, so a
    renamed field is a type error here rather than a ``KeyError`` at runtime;
    and it additionally asserts the endpoint really returned the shape it
    declared, which a dictionary lookup cannot.
    """
    assert response.status_code < HTTPStatus.BAD_REQUEST, response.text
    return TypeAdapter(shape).validate_json(response.content)


def detail(response: httpx.Response) -> str:
    """The refusal message from a response that must have failed."""
    assert response.status_code >= HTTPStatus.BAD_REQUEST, response.text
    return TypeAdapter(ErrorBody).validate_json(response.content).detail


def _keys(response: httpx.Response, *path: str) -> tuple[str, ...]:
    """The field names a response body actually carries, at ``path``.

    Used where the test is about the SHAPE - "all four speeds are present",
    "the healthcheck leaks nothing" - rather than about a value. Validated
    through a ``TypeAdapter`` so the decoded body is typed rather than ``Any``.
    """
    decoded = TypeAdapter(dict[str, object]).validate_json(response.content)
    for step in path:
        nested = decoded[step]
        decoded = TypeAdapter(dict[str, object]).validate_python(nested)
    return tuple(decoded)


def _explode(*_args: object, **_kwargs: object) -> Never:
    """Stands in for a filesystem call that fails. Never returns."""
    raise OSError("simulated filesystem failure")


def event(detail: str) -> SessionEvent:
    """One session event, for the hub tests."""
    return SessionEvent(
        kind=EventKind.START_REQUESTED,
        at=Monotonic(1.0),
        wall_clock=UnixMillis(1_700_000_000_000),
        operator=OPERATOR,
        detail=detail,
    )


def profile_document(profile_id: str) -> dict[str, object]:
    """A valid profile document, as the setup view would submit it."""
    return {
        "profile_id": profile_id,
        "name": "bench",
        "total_duration_s": 1500.0,
        "baseline_s": 180.0,
        "warmup_max_s": 300.0,
        "hold_min_s": 300.0,
        "cooldown_s": 240.0,
        "recovery_s": 300.0,
        "zone_low_bpm": 110,
        "zone_high_bpm": 130,
        "hard_max_bpm": 145,
        "critical_bpm": 155,
        "subject_hr_max": 160,
        "min_run_rpm": 55,
        "max_rpm": 276,
        "warmup_rpm_ceiling_fraction": 0.6,
        "channels": ["ECG"],
        "allow_above_nameplate": False,
    }
