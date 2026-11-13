"""ANH-152 on the console's HTTP API: who may act, who may read, and the stops.

* every route that acts refuses a request with no operator signed in on the
  device (401 ``operator_required``), and the set of routes that do not is
  pinned here, so that a new route has to be placed on one side; every GET
  route too: a new one fails until it is named a read or an action;
* the reads take a device token: the console's own, or a tablet of the list;
* the STOP and the emergency stop take a device token only, are attributed to
  the device when nobody is signed in, and never wait for a sign-in;
* the sign-in, its cookie, its lockout, its expiry and the sign-out;
* a console that never accepted a list opens no action.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from starlette.routing import Route

from src.clock import ManualClock
from src.console_access import (
    LOCAL_DEVICE,
    LOCKOUT_FAILURES,
    SESSION_IDLE,
    ConsoleAccess,
    ScryptCode,
    code_matches,
    device_actor,
)
from src.control_surface import ControlSurface
from src.geometry import MachineGeometry
from src.result import Ok
from src.telemetry import TelemetryHub
from src.training.plan import SHIPPED_DEFAULTS_PATH, ProfileStore
from src.training.safety import SafetyLimits, SafetySupervisor
from src.units import Bpm, Metres, Seconds
from src.web.app import create_app
from src.web.deps import SESSION_COOKIE, Services, TlsFiles, WebConfig, identify_device
from src.web.routes import END_REASON, ESTOP_REASON, OPERATOR_REQUIRED, STOP_BODY_LIMIT
from src.web.schemas import AccessRow
from tests.access_support import CODE, OPERATOR_ID, OPERATOR_NAME, held_list, tablet
from tests.test_web_api import StubPorts, event, make_snapshot, probe_socket

TOKEN: Final[str] = "a-device-token-of-the-console"  # noqa: S105  # a test fixture
TABLET_TOKEN: Final[str] = "a-token-of-a-tablet-of-the-list"  # noqa: S105  # a test fixture
TABLET: Final[str] = "tablette-1"
ACCESS: Final[TypeAdapter[AccessRow]] = TypeAdapter(AccessRow)

UNSIGNED_POSTS: Final[frozenset[str]] = frozenset(
    {
        "/api/access/sign-in",
        "/api/access/sign-out",
        "/api/plan/preview",
        "/api/session/stop",
        "/api/session/estop",
    }
)
"""The only routes that change something, or may, and take no operator: signing in and
out, the dry run of a programme (it starts nothing), and the two stops."""

READS: Final[frozenset[str]] = frozenset(
    {
        "/",
        "/app.css",
        "/app.js",
        "/healthz",
        "/openapi.json",
        "/api/access",
        "/api/camera",
        "/api/ecg",
        "/api/feedback",
        "/api/panel",
        "/api/preflight",
        "/api/profiles",
        "/api/records",
        "/api/remote-launch",
        "/api/roster",
        "/api/sensors",
        "/api/snapshot",
        "/api/status",
    }
)
"""Every GET route that only reads, and takes no operator. A new GET route is in neither
this set nor :data:`ACTING_GETS` and fails the test that pins them."""

ACTING_GETS: Final[frozenset[str]] = frozenset({"/api/records/{name}/archive"})
"""The GET routes that act (an archive marks its record), and so take an operator."""


@dataclass(slots=True)
class Console:
    clock: ManualClock
    surface: ControlSurface
    access: ConsoleAccess | None
    services: Services
    app: FastAPI

    def client(self, host: str = "127.0.0.1") -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(host, 51234))
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def console(
    tmp_path: Path,
    *,
    held: bool = True,
    access: bool = True,
    tls: TlsFiles | None = None,
    verify: Callable[[str, ScryptCode], bool] = code_matches,
) -> Console:
    clock = ManualClock()
    supervisor = SafetySupervisor(
        clock=clock, limits=SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
    )
    hub = TelemetryHub(clock=clock)
    surface = ControlSurface(clock=clock, supervisor=supervisor, sink=hub)
    store = ProfileStore(tmp_path / "profiles.json", SHIPPED_DEFAULTS_PATH)
    assert isinstance(store.load(), Ok)
    gate = (
        ConsoleAccess(
            clock=clock,
            held=held_list(devices=(tablet(TABLET, TABLET_TOKEN),)) if held else None,
            lockout_path=None,
            verify=verify,
        )
        if access
        else None
    )
    services = Services(
        clock=clock,
        surface=surface,
        hub=hub,
        supervisor=supervisor,
        store=store,
        ports=StubPorts(),
        geometry=MachineGeometry(radius=Metres(1.0)),
        access=gate,
    )
    config = WebConfig(host="127.0.0.1", port=8080, token=TOKEN, tls=tls)
    return Console(clock, surface, gate, services, create_app(services=services, config=config))


def device(token: str = TOKEN) -> dict[str, str]:
    return {"X-Anheart-Token": token}


async def signed(client: httpx.AsyncClient, token: str = TOKEN) -> httpx.Response:
    return await client.post(
        "/api/access/sign-in", json={"user_id": OPERATOR_ID, "code": CODE}, headers=device(token)
    )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[tuple[Console, httpx.AsyncClient]]:
    c = console(tmp_path)
    async with c.client() as client:
        yield c, client


def posts(app: FastAPI) -> set[str]:
    """Every path the app answers a POST on."""
    return {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "POST" in (route.methods or set[str]())
    }


def gets(app: FastAPI) -> set[str]:
    """Every path the app answers a GET on, its own and FastAPI's."""
    return {
        route.path
        for route in app.routes
        if isinstance(route, Route) and "GET" in (route.methods or set[str]())
    }


def acting_routes(app: FastAPI) -> list[tuple[str, str]]:
    found = [("POST", path) for path in posts(app) if path not in UNSIGNED_POSTS]
    return sorted([*found, *(("GET", path) for path in ACTING_GETS)])


# =========================================================================
# EX-3: every action takes an operator signed in
# =========================================================================


async def test_every_action_route_refuses_a_device_with_nobody_signed_in(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    c, client = rig
    routes = acting_routes(c.app)
    assert len(routes) >= 15
    for method, path in routes:
        answered = await client.request(
            method, path.replace("{name}", "record-1"), json={}, headers=device()
        )
        assert answered.status_code == HTTPStatus.UNAUTHORIZED, path
        assert answered.json()["code"] == OPERATOR_REQUIRED, path


async def test_the_routes_that_take_no_operator_are_exactly_these(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    c, _ = rig
    assert posts(c.app) >= UNSIGNED_POSTS
    assert {p for _, p in acting_routes(c.app)} >= {
        "/api/session/start",
        "/api/manual/start",
        "/api/manual/target",
        "/api/drive/fault-reset",
        "/api/safety/attest",
        "/api/safety/acknowledge",
        "/api/presence",
        "/api/preflight/run",
        "/api/preflight/acknowledge",
        "/api/remote-launch/confirm",
        "/api/service/restore",
        "/api/feedback",
        "/api/feedback/skip",
        "/api/access/refresh",
        "/api/records/{name}/archive",
    }


async def test_every_get_route_is_a_read_or_takes_an_operator(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    c, client = rig
    assert gets(c.app) == READS | ACTING_GETS
    surface = c.surface

    def state() -> tuple[object, ...]:
        return (
            surface.counters,
            surface.fates,
            surface.pending,
            surface.attendant_last_seen,
            surface.estop_latched,
            c.services.store.rev,
        )

    before = state()
    for path in sorted(READS):
        answered = await client.get(path, headers=device())
        assert answered.status_code != HTTPStatus.UNAUTHORIZED, path
    # Read with nobody signed in, and nothing was asked of the machine.
    assert state() == before


async def test_an_operator_signed_in_acts_under_their_account(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    _, client = rig
    answered = await signed(client)
    assert answered.status_code == HTTPStatus.OK
    attested = await client.post(
        "/api/safety/attest",
        json={"sto_jumper_removed": True, "mushroom_wired_nc": True, "operator": "Nom tape"},
        headers=device(),
    )
    assert attested.status_code == HTTPStatus.OK
    assert attested.json()["operator"] == OPERATOR_ID


async def test_a_console_without_a_list_opens_no_action_but_stops(tmp_path: Path) -> None:
    for c in (console(tmp_path, held=False), console(tmp_path, access=False)):
        async with c.client() as client:
            refused = await signed(client)
            assert refused.status_code == HTTPStatus.FORBIDDEN
            assert refused.json()["code"] == "no_list"
            access = await client.get("/api/access", headers=device())
            assert access.json()["received"] is False
            assert access.json()["operators"] == []
            start = await client.post(
                "/api/manual/start", json={"occupancy": "bench"}, headers=device()
            )
            assert start.status_code == HTTPStatus.UNAUTHORIZED
            stop = await client.post("/api/session/estop", json={}, headers=device())
            assert stop.status_code == HTTPStatus.OK
            assert stop.json()["operator"] == device_actor(LOCAL_DEVICE)


# =========================================================================
# EX-4: the stops take a device token only, and wait for nothing
# =========================================================================


async def test_the_emergency_stop_is_the_device_s_with_nobody_signed_in(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    c, client = rig
    refused = await client.post("/api/session/estop", json={})
    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    from_tablet = await client.post("/api/session/estop", json={}, headers=device(TABLET_TOKEN))
    assert from_tablet.status_code == HTTPStatus.OK
    assert from_tablet.json()["operator"] == device_actor(TABLET) == "device:tablette-1"
    assert c.services.supervisor.standing is not None


async def test_the_emergency_stop_names_the_operator_signed_in(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    _, client = rig
    await signed(client)
    stop = await client.post("/api/session/estop", json={}, headers=device())
    assert stop.json()["operator"] == OPERATOR_ID


async def test_the_stop_takes_no_sign_in(rig: tuple[Console, httpx.AsyncClient]) -> None:
    _, client = rig
    answered = await client.post("/api/session/stop", json={}, headers=device())
    assert answered.status_code == HTTPStatus.CONFLICT
    assert answered.json()["code"] == "nothing_running"


STOP_BODIES: Final[tuple[tuple[bytes | None, str | None], ...]] = (
    (None, None),
    (b"", "application/json"),
    (b"stop!", "text/plain"),
    (b"\xff\xfe", "application/json"),
    (b"null", "application/json"),
    (b"[1, 2]", "application/json"),
    (b'{"reason": 7}', "application/json"),
    (b'{"reason": "  "}', "application/json"),
    (b"[" * 2000 + b"]" * 2000, "application/json"),
    (b'{"reason": "' + b"x" * STOP_BODY_LIMIT + b'"}', "application/json"),
)
"""Bodies no stop is refused for: none, empty, not JSON, not UTF-8, not an object, a
reason that is not text or is blank, nested too deep to read, too long to read."""


@pytest.mark.parametrize(("content", "content_type"), STOP_BODIES)
async def test_a_stop_is_never_refused_for_its_body(
    tmp_path: Path, content: bytes | None, content_type: str | None
) -> None:
    c = console(tmp_path)
    headers = device() | ({} if content_type is None else {"content-type": content_type})
    async with c.client() as client:
        stop = await client.post("/api/session/stop", content=content, headers=headers)
        assert stop.status_code == HTTPStatus.CONFLICT, stop.text
        estop = await client.post("/api/session/estop", content=content, headers=headers)
        assert estop.status_code == HTTPStatus.OK, estop.text
        assert estop.json()["reason"] == ESTOP_REASON
    assert c.services.supervisor.standing is not None


async def test_a_stop_says_the_reason_its_body_gives(tmp_path: Path) -> None:
    c = console(tmp_path)
    async with c.client() as client:
        given = await client.post(
            "/api/session/estop", json={"reason": "fumee", "operator": "Jean"}, headers=device()
        )
        assert given.json()["reason"] == "fumee"
        assert given.json()["operator"] == device_actor(LOCAL_DEVICE)
    assert END_REASON == "operator ended the session"


async def test_a_sign_in_being_checked_never_delays_the_emergency_stop(tmp_path: Path) -> None:
    release = threading.Event()
    started = threading.Event()

    def slow(code: str, held: ScryptCode) -> bool:
        started.set()
        release.wait(5.0)
        return code_matches(code, held)

    c = console(tmp_path, verify=slow)
    async with c.client() as signing, c.client() as stopping:
        sign_in = asyncio.create_task(signed(signing))
        assert await asyncio.to_thread(started.wait, 5.0)
        stop = await asyncio.wait_for(
            stopping.post("/api/session/estop", json={}, headers=device()), timeout=1.0
        )
        assert stop.status_code == HTTPStatus.OK
        assert not sign_in.done()
        release.set()
        assert (await sign_in).status_code == HTTPStatus.OK


# =========================================================================
# The reads: a device token, the console's or a tablet's
# =========================================================================


async def test_the_reads_take_a_device_token(rig: tuple[Console, httpx.AsyncClient]) -> None:
    c, client = rig
    assert (await client.get("/api/status")).status_code == HTTPStatus.UNAUTHORIZED
    assert (await client.get("/api/status", headers=device("wrong"))).status_code == 401
    assert (await client.get("/api/status", headers=device())).status_code == HTTPStatus.OK
    assert (await client.get("/api/status", headers=device(TABLET_TOKEN))).status_code == 200
    # Revoked on the site: the next list no longer holds it.
    assert c.access is not None
    c.access.adopt(held_list(issued_at=1_790_000_000_001))
    assert (await client.get("/api/status", headers=device(TABLET_TOKEN))).status_code == 401


async def test_the_socket_takes_a_tablet_token_of_the_list(tmp_path: Path) -> None:
    c = console(tmp_path)
    c.surface.publish(make_snapshot())
    accepted = await probe_socket(c.app, origin=None, token=TABLET_TOKEN)
    assert accepted[0]["type"] == "websocket.accept"
    refused = await probe_socket(c.app, origin=None, token="not-a-token-of-it")  # noqa: S106  # a test value
    assert [m["type"] for m in refused] == ["websocket.close"]


async def test_a_revoked_tablet_s_socket_is_closed_before_its_next_frame(
    tmp_path: Path,
) -> None:
    c = console(tmp_path)
    access = c.access
    assert access is not None
    c.surface.publish(make_snapshot())

    def revoke() -> None:
        access.adopt(held_list(issued_at=1_790_000_000_001))  # the tablet is gone
        c.services.hub.publish_event(event("after the revocation"))

    sent = await probe_socket(
        c.app, origin=None, token=TABLET_TOKEN, replies=2, on_first_frame=revoke
    )
    frames = [m for m in sent if m["type"] == "websocket.send"]
    assert len(frames) == 1
    assert sent[-1]["type"] == "websocket.close"
    assert sent[-1]["code"] == 1008
    # The console's own token is no list's to revoke.
    kept = await probe_socket(c.app, origin=None, token=TOKEN, replies=2, on_first_frame=revoke)
    assert len([m for m in kept if m["type"] == "websocket.send"]) >= 2
    assert all(m.get("code") != 1008 for m in kept)


def test_who_a_device_is() -> None:
    clock = ManualClock()
    access = ConsoleAccess(
        clock=clock, held=held_list(devices=(tablet(TABLET, TABLET_TOKEN),)), lockout_path=None
    )
    on_loopback = WebConfig(host="127.0.0.1", port=8080, token=None)
    configured = WebConfig(host="127.0.0.1", port=8080, token=TOKEN)
    assert identify_device(on_loopback, None, None) == "loopback"
    assert identify_device(configured, access, TOKEN) == LOCAL_DEVICE
    assert identify_device(configured, access, TABLET_TOKEN) == TABLET
    assert identify_device(configured, access, "other") is None
    assert identify_device(configured, None, TABLET_TOKEN) is None


async def test_health_is_public_from_the_pi_only(tmp_path: Path) -> None:
    c = console(tmp_path)
    async with c.client() as local, c.client("10.42.0.7") as remote:
        assert (await local.get("/healthz")).status_code == HTTPStatus.OK
        assert (await remote.get("/healthz")).status_code == HTTPStatus.UNAUTHORIZED
        assert (await remote.get("/healthz", headers=device())).status_code == HTTPStatus.OK


# =========================================================================
# EX-2: the sign-in, its cookie, its lockout, its expiry
# =========================================================================


async def test_the_cookie_is_http_only_strict_and_secure_on_tls_only(tmp_path: Path) -> None:
    cert = TlsFiles(certificate_path=Path("console.crt"), key_path=Path("console.key"))
    for tls, secure in ((None, False), (cert, True)):
        c = console(tmp_path, tls=tls)
        async with c.client() as client:
            answered = await signed(client)
            assert answered.status_code == HTTPStatus.OK
            cookie = answered.headers["set-cookie"].lower()
            assert cookie.startswith(f"{SESSION_COOKIE}=")
            assert "httponly" in cookie
            assert "samesite=strict" in cookie
            assert "path=/" in cookie
            assert ("secure" in cookie.split("; ")) is secure
            assert CODE not in answered.text


async def test_the_sign_in_screen_lists_names_never_a_hash(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    _, client = rig
    answered = await client.get("/api/access", headers=device())
    before = ACCESS.validate_json(answered.content)
    assert before.received
    assert before.device == LOCAL_DEVICE
    assert [(o.user_id, o.name, o.locked_for_s) for o in before.operators] == [
        (OPERATOR_ID, OPERATOR_NAME, None)
    ]
    assert before.signed_in is None
    assert "scrypt" not in answered.text
    assert "salt" not in answered.text
    after = ACCESS.validate_json((await signed(client)).content).signed_in
    assert after is not None
    assert (after.user_id, after.idle_left_s) == (OPERATOR_ID, SESSION_IDLE)


async def test_wrong_codes_lock_the_operator_out(rig: tuple[Console, httpx.AsyncClient]) -> None:
    _, client = rig
    attempt = {"user_id": OPERATOR_ID, "code": "000000"}
    for _ in range(LOCKOUT_FAILURES - 1):
        wrong = await client.post("/api/access/sign-in", json=attempt, headers=device())
        assert wrong.status_code == HTTPStatus.UNAUTHORIZED
        assert wrong.json()["code"] == "wrong_code"
        assert "000000" not in wrong.text
    locked = await client.post("/api/access/sign-in", json=attempt, headers=device())
    assert locked.status_code == HTTPStatus.LOCKED
    assert (await signed(client)).status_code == HTTPStatus.LOCKED
    listed = ACCESS.validate_json((await client.get("/api/access", headers=device())).content)
    assert listed.operators[0].locked_for_s is not None
    unknown = await client.post(
        "/api/access/sign-in", json={"user_id": "x", "code": CODE}, headers=device()
    )
    assert unknown.status_code == HTTPStatus.FORBIDDEN


async def test_a_session_is_its_tablet_s(rig: tuple[Console, httpx.AsyncClient]) -> None:
    _, client = rig
    await signed(client, TABLET_TOKEN)
    on_tablet = await client.post("/api/access/refresh", headers=device(TABLET_TOKEN))
    assert on_tablet.status_code == HTTPStatus.OK
    elsewhere = await client.post("/api/access/refresh", headers=device())
    assert elsewhere.status_code == HTTPStatus.UNAUTHORIZED


async def test_a_session_ends_idle_and_an_action_restarts_the_period(
    rig: tuple[Console, httpx.AsyncClient],
) -> None:
    c, client = rig
    await signed(client)
    c.clock.advance(Seconds(SESSION_IDLE - 10.0))
    # The attendant's ping is not an action: it does not keep the session alive.
    assert (await client.post("/api/presence", headers=device())).status_code == HTTPStatus.OK
    assert (await client.post("/api/access/refresh", headers=device())).status_code == 200
    c.clock.advance(Seconds(SESSION_IDLE - 10.0))
    assert (await client.post("/api/presence", headers=device())).status_code == HTTPStatus.OK
    c.clock.advance(Seconds(20.0))
    expired = await client.post("/api/presence", headers=device())
    assert expired.status_code == HTTPStatus.UNAUTHORIZED
    assert (await client.get("/api/access", headers=device())).json()["signed_in"] is None


async def test_the_sign_out_ends_the_session(rig: tuple[Console, httpx.AsyncClient]) -> None:
    _, client = rig
    await signed(client)
    out = await client.post("/api/access/sign-out", headers=device())
    assert out.status_code == HTTPStatus.OK
    assert out.json()["signed_in"] is None
    assert SESSION_COOKIE in out.headers["set-cookie"]
    refused = await client.post("/api/access/refresh", headers=device())
    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    # Signing out with nothing signed in, and on a console without access, changes nothing.
    assert (await client.post("/api/access/sign-out", headers=device())).status_code == 200


async def test_signing_out_of_a_console_without_access(tmp_path: Path) -> None:
    c = console(tmp_path, access=False)
    async with c.client() as client:
        assert (await client.post("/api/access/sign-out", headers=device())).status_code == 200
