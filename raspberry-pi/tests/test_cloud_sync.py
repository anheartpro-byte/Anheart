"""The dashboard link: what goes up, the two things that come down, and failure.

Three layers, tested separately:

* the transport, over ``httpx.MockTransport`` - status codes, bodies that are
  not JSON, a network that is not there;
* :class:`~src.cloud_sync.CloudSync` against a scripted dashboard
  (:class:`Dashboard`) and a runtime whose state the test sets
  (:class:`StubRuntime`), with the REAL control surface and profile store of a
  simulated panel - so a launch really goes through the surface's gates;
* the panel itself, in simulation, for the programme path it now runs: every
  gate between a dashboard launch and a turning shaft, and a refusal at each.

No test here reaches a network: every transport is a fake or a mock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import (
    DASHBOARD_OPERATOR,
    HEARTBEAT_PERIOD,
    LAUNCH_TIMEOUT,
    MAX_BATCH,
    MAX_FINISHED,
    RETRY_PERIOD,
    TELEMETRY_PERIOD,
    CloudError,
    CloudSync,
    CloudTransport,
    Document,
    HttpxTransport,
    Refused,
    SessionKind,
    StartedSession,
    Unreachable,
    describe_surface_refusal,
    end_is_failure,
    live_row,
    runnable_profiles,
    telemetry_point,
)
from src.contract import CONTRACT_VERSION, SERVER_VERSION_FIELD
from src.control_surface import LOCAL_SUBJECT, StartRefusal, StartSession
from src.local_config import CardiacTiers, CloudConfig, LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel, describe_resolve_error
from src.result import Err, Ok, Result
from src.training.plan import JsonValue, ResolveError
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import Bpm, Monotonic, Seconds, UnixMillis

TICK: Final[Seconds] = Seconds(0.2)
OPERATOR: Final[str] = "dr. attending"
PROFILE: Final[str] = "standard_30_min"

LINKED_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "PROGRAMS_ENABLED": "true",
    "OCCUPANCY_OCCUPIED_ENABLED": "true",
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

LAUNCH: Final[Mapping[str, JsonValue]] = {
    "sessionId": "remote-1",
    "profileId": PROFILE,
    "totalDurationS": None,
    "subjectId": "user-1",
    "subjectHrMax": 170,
    "operatorName": "Dr Manager",
    "subjectAge": 30,
}


def config_of(**changes: str) -> LocalConfig:
    loaded = load_local_config({**LINKED_ENV, **changes})
    assert isinstance(loaded, Ok), loaded
    return loaded.value


# =========================================================================
# A scripted dashboard
# =========================================================================

type Reply = Result[Document, CloudError]
type Handler = Callable[[Mapping[str, object]], Reply]


@dataclass
class Dashboard:
    """A :class:`CloudTransport` that answers from a table and records every call."""

    handlers: dict[str, Handler] = field(default_factory=dict[str, Handler])
    calls: list[tuple[str, str, Mapping[str, object]]] = field(
        default_factory=list[tuple[str, str, Mapping[str, object]]]
    )

    def answer(self, path: str, reply: Reply) -> None:
        self.handlers[path] = lambda _body: reply

    def _reply(self, method: str, path: str, body: Mapping[str, object]) -> Reply:
        self.calls.append((method, path, body))
        handler = self.handlers.get(path)
        if handler is not None:
            return handler(body)
        # Unscripted, it is a dashboard of this console's contract with nothing waiting.
        return launch_answer() if path == POLL_PATH else ok()

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        return self._reply("GET", path, dict(params or {}))

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        return self._reply("POST", path, body)

    def to(self, path: str) -> list[Mapping[str, object]]:
        return [body for _method, called, body in self.calls if called == path]


DOWN: Final[Reply] = Err(Unreachable("no route to host"))
POLL_PATH: Final[str] = "/api/machine/training/poll"


def ok(document: Document | None = None) -> Reply:
    """A typed success, so an empty ``{}`` is a Document and not a ``dict[Unknown, Unknown]``."""
    return Ok({} if document is None else document)


def launch_answer(
    session: Mapping[str, JsonValue] | None = None, *, version: object = CONTRACT_VERSION
) -> Reply:
    """A poll answer carrying ``session``, from a dashboard announcing ``version``."""
    return ok({"session": session, SERVER_VERSION_FIELD: version})


def status_answer(
    *, active: bool = True, stop: bool = False, version: object = CONTRACT_VERSION
) -> Reply:
    """A status answer for a running session, from a dashboard announcing ``version``."""
    return ok({"active": active, "stopRequested": stop, SERVER_VERSION_FIELD: version})


def count(value: object) -> int:
    """The length of a JSON array the fake dashboard received."""
    assert isinstance(value, Sequence)
    return len(cast("Sequence[object]", value))


NO: Final[Reply] = Err(Refused(400, "no"))


@dataclass
class StubRuntime:
    """The four things the link reads, set by the test. The snapshot is a real one."""

    source: Callable[[], TelemetrySnapshot]
    state: RuntimeState = RuntimeState.IDLE
    end_reason: EndReason | None = None
    stop_reason: str | None = None

    def snapshot(self) -> TelemetrySnapshot:
        return self.source()


@dataclass
class Rig:
    clock: ManualClock
    panel: LocalPanel
    dashboard: Dashboard
    runtime: StubRuntime
    sync: CloudSync

    async def step(self, seconds: float = 1.0) -> None:
        self.clock.advance(Seconds(seconds))
        await self.sync.step()


def rig(tmp_path: Path, *, programs: bool = True, tiers: CardiacTiers | None = None) -> Rig:
    """A real simulated panel's surface and store, with a stub runtime and a scripted link."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    config = config_of()
    dashboard = Dashboard()
    panel = build_panel(
        config, clock=clock, profiles_path=tmp_path / "p.json", transport=lambda _c: dashboard
    )
    runtime = StubRuntime(source=panel.runtime.snapshot)
    sync = CloudSync(
        clock=clock,
        transport=dashboard,
        runtime=runtime,
        surface=panel.surface,
        store=panel.services.store,
        tiers=config.tiers if tiers is None else tiers,
        programs_enabled=programs,
    )
    return Rig(clock=clock, panel=panel, dashboard=dashboard, runtime=runtime, sync=sync)


def manual(clock: ManualClock, *, remote: str | None = None) -> StartedSession:
    return StartedSession(
        kind=SessionKind.MANUAL,
        operator=OPERATOR,
        started_at=clock.unix_millis(),
        subject_id=LOCAL_SUBJECT,
        cloud_session_id=remote,
        occupancy=Occupancy.BENCH,
    )


# =========================================================================
# The transport
# =========================================================================


def transport_answering(handler: Callable[[httpx.Request], httpx.Response]) -> HttpxTransport:
    config = CloudConfig(url="https://example.convex.site", api_key="k")
    client = httpx.AsyncClient(
        base_url=config.url,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer k"},
    )
    return HttpxTransport(config, client=client)


async def test_get_and_post_speak_json_with_the_machine_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = transport_answering(handler)
    assert await transport.get("/a", {"sessionId": "s"}) == Ok({"ok": True})
    assert await transport.post("/b", {"x": 1}) == Ok({"ok": True})
    assert seen[0].url.params["sessionId"] == "s"
    assert seen[0].headers["Authorization"] == "Bearer k"
    assert json.loads(seen[1].content) == {"x": 1}
    await transport.close()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(400, json={"error": "Session not found"}),
            Refused(400, "Session not found"),
        ),
        (httpx.Response(401, text="nope"), Refused(401, "HTTP 401")),
        (httpx.Response(500, json={"error": 3}), Refused(500, "HTTP 500")),
        (httpx.Response(200, text="<html>"), Unreachable("HTTP 200: not a JSON object")),
        (httpx.Response(200, json=[1, 2]), Unreachable("HTTP 200: not a JSON object")),
    ],
)
async def test_answers_that_are_refusals_or_not_json(
    response: httpx.Response, expected: CloudError
) -> None:
    transport = transport_answering(lambda _request: response)
    assert await transport.get("/a") == Err(expected)


async def test_an_empty_body_is_an_empty_document() -> None:
    transport = transport_answering(lambda _request: httpx.Response(200))
    assert await transport.post("/a", {}) == ok()


async def test_no_network_is_unreachable_never_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = transport_answering(handler)
    got = await transport.get("/a")
    posted = await transport.post("/a", {})
    assert isinstance(got, Err)
    assert isinstance(got.error, Unreachable)
    assert isinstance(posted, Err)
    assert isinstance(posted.error, Unreachable)


async def test_the_default_client_is_built_from_the_configuration() -> None:
    transport = HttpxTransport(CloudConfig(url="https://example.convex.site", api_key="k"))
    await transport.close()


# =========================================================================
# Wire shapes
# =========================================================================


@pytest.mark.parametrize(
    ("reason", "failed"),
    [
        (EndReason.PROGRAMME_COMPLETE, False),
        (EndReason.OPERATOR_STOP, False),
        (EndReason.EMERGENCY_STOP, True),
        (EndReason.SAFETY_VERDICT, True),
        (EndReason.TICK_EXCEPTION, True),
        (EndReason.SHUTDOWN, True),
        (None, True),
    ],
)
def test_only_a_completed_or_operator_ended_session_is_not_a_failure(
    reason: EndReason | None, failed: bool
) -> None:
    assert end_is_failure(reason) is failed


def test_only_presets_on_this_machine_s_tiers_are_offered(tmp_path: Path) -> None:
    r = rig(tmp_path)
    profiles = r.panel.services.store.list_profiles()
    assert runnable_profiles(profiles, CardiacTiers(Bpm(148), Bpm(158))) == profiles
    assert runnable_profiles(profiles, CardiacTiers(Bpm(165), Bpm(175))) == ()


async def test_a_heart_rate_is_sent_only_when_it_may_be_shown(tmp_path: Path) -> None:
    r = rig(tmp_path)
    cold = r.panel.runtime.snapshot()
    assert cold.live_bpm is None
    assert "bpm" not in live_row(cold, None)
    assert "bpm" not in telemetry_point(cold)
    assert "sessionId" not in live_row(cold, None)
    for _ in range(round(15.0 / TICK)):
        r.clock.advance(TICK)
        await r.panel.ecg_step()
        await r.panel.control_step()
    warm = r.panel.runtime.snapshot()
    assert warm.live_bpm is not None
    assert live_row(warm, "s")["bpm"] == int(warm.live_bpm)
    assert live_row(warm, "s")["sessionId"] == "s"
    assert telemetry_point(warm)["bpm"] == int(warm.live_bpm)


def test_every_surface_refusal_has_words(tmp_path: Path) -> None:
    r = rig(tmp_path)
    surface = r.panel.surface
    unattested = surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(unattested, Err)
    assert "non atteste" in describe_surface_refusal(unattested.error)
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        surface.submit_start(
            profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
        ),
        Ok,
    )
    busy = surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(busy, Err)
    assert "occupee" in describe_surface_refusal(busy.error)


def test_a_standing_verdict_has_words(tmp_path: Path) -> None:
    r = rig(tmp_path)
    surface = r.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    surface.submit_estop(operator=OPERATOR, reason="test")
    holding = surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(holding, Err)
    assert "verdict de securite" in describe_surface_refusal(holding.error)


# =========================================================================
# Heartbeat and presets
# =========================================================================


async def test_the_heartbeat_reports_the_machine_every_period(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await r.step()
    await r.step()
    beats = r.dashboard.to("/api/machine/heartbeat")
    assert len(beats) == 1
    assert beats[0]["programsEnabled"] is True
    assert "activeSessionId" not in beats[0]
    await r.step(HEARTBEAT_PERIOD)
    assert len(r.dashboard.to("/api/machine/heartbeat")) == 2
    assert r.sync.online


async def test_the_heartbeat_names_the_running_session(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-9"}))
    r.sync.session_started(manual(r.clock))
    await r.step()
    await r.step(HEARTBEAT_PERIOD)
    beat = r.dashboard.to("/api/machine/heartbeat")[-1]
    assert beat["activeSessionId"] == "cloud-9"
    live = beat["live"]
    assert isinstance(live, Mapping)
    assert cast("Mapping[str, object]", live)["sessionId"] == "cloud-9"


async def test_presets_are_pushed_once_per_revision_and_retried_on_failure(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/profiles", DOWN)
    await r.step()
    await r.step()  # inside the retry window: not attempted again
    assert len(r.dashboard.to("/api/machine/profiles")) == 1
    r.dashboard.answer("/api/machine/profiles", ok({"count": 2}))
    await r.step(RETRY_PERIOD)
    pushed = r.dashboard.to("/api/machine/profiles")
    assert len(pushed) == 2
    assert count(pushed[-1]["profiles"]) == 2
    await r.step(RETRY_PERIOD)
    assert len(r.dashboard.to("/api/machine/profiles")) == 2


async def test_presets_the_supervisor_would_refuse_are_not_offered(tmp_path: Path) -> None:
    r = rig(tmp_path, tiers=CardiacTiers(Bpm(165), Bpm(175)))
    await r.step()
    assert r.dashboard.to("/api/machine/profiles")[0]["profiles"] == []


# =========================================================================
# Launches from the dashboard
# =========================================================================


async def test_a_launch_is_submitted_through_the_surface_with_the_rider(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(
        "/api/machine/training/poll", launch_answer({**LAUNCH, "totalDurationS": 2700})
    )
    await r.step()
    pending = r.panel.surface.pending
    assert isinstance(pending, StartSession)
    assert pending.profile_id == PROFILE
    assert pending.subject_id == "user-1"
    assert pending.subject_hr_max == Bpm(170)
    assert pending.cloud_session_id == "remote-1"
    assert pending.total_duration_s == Seconds(2700.0)
    assert DASHBOARD_OPERATOR in pending.operator
    # While the loop has not answered, the machine does not ask again.
    await r.step(5.0)
    assert len(r.dashboard.to("/api/machine/training/poll")) == 1


async def test_a_launch_the_surface_refuses_comes_back_failed(tmp_path: Path) -> None:
    r = rig(tmp_path)  # e-stop wiring NOT attested
    r.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await r.step()
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["sessionId"] == "remote-1"
    assert ends[0]["failed"] is True
    reason = ends[0]["reason"]
    assert isinstance(reason, str)
    assert "non atteste" in reason


async def test_a_launch_the_loop_refuses_comes_back_failed(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await r.step()
    r.sync.start_refused("someone-else", "not this one")
    r.sync.start_refused("remote-1", "variateur en defaut")
    await r.step()
    reasons = [body["reason"] for body in r.dashboard.to("/api/machine/training/end")]
    assert reasons == [
        "refusee par la machine : not this one",
        "refusee par la machine : variateur en defaut",
    ]
    # The wait is over, so the machine polls again.
    await r.step(5.0)
    assert len(r.dashboard.to("/api/machine/training/poll")) == 2


async def test_a_launch_nobody_answers_times_out(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await r.step()
    await r.step(LAUNCH_TIMEOUT)
    reason = r.dashboard.to("/api/machine/training/end")[0]["reason"]
    assert isinstance(reason, str)
    assert "ni demarre ni refuse" in reason


@pytest.mark.parametrize(
    "answer",
    [
        DOWN,
        ok({SERVER_VERSION_FIELD: CONTRACT_VERSION}),  # no "session" key at all
        launch_answer(None),
        launch_answer({**LAUNCH, "sessionId": 3}),
        launch_answer({**LAUNCH, "profileId": None}),
        launch_answer({**LAUNCH, "subjectId": None}),
        launch_answer({**LAUNCH, "subjectHrMax": "170"}),
        launch_answer({**LAUNCH, "subjectHrMax": True}),
        launch_answer({**LAUNCH, "operatorName": None}),
    ],
)
async def test_no_launch_or_a_malformed_one_submits_nothing(tmp_path: Path, answer: Reply) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer("/api/machine/training/poll", answer)
    await r.step()
    assert r.panel.surface.pending is None
    assert r.dashboard.to("/api/machine/training/end") == []


async def test_a_blank_operator_and_odd_duration_are_tolerated(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    odd = {**LAUNCH, "operatorName": "", "totalDurationS": True}
    r.dashboard.answer("/api/machine/training/poll", launch_answer(odd))
    await r.step()
    pending = r.panel.surface.pending
    assert isinstance(pending, StartSession)
    assert pending.total_duration_s is None
    assert pending.operator.startswith(DASHBOARD_OPERATOR)


async def test_no_poll_without_programmes(tmp_path: Path) -> None:
    r = rig(tmp_path, programs=False)
    await r.step()
    assert r.dashboard.to("/api/machine/training/poll") == []


@pytest.mark.parametrize("state", [RuntimeState.RUNNING, RuntimeState.ENDING])
async def test_no_poll_while_the_machine_is_busy(tmp_path: Path, state: RuntimeState) -> None:
    r = rig(tmp_path)
    r.runtime.state = state
    await r.step()
    assert r.dashboard.to("/api/machine/training/poll") == []


async def test_polls_are_spaced(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await r.step()
    await r.step()
    assert len(r.dashboard.to("/api/machine/training/poll")) == 1
    await r.step(3.0)
    assert len(r.dashboard.to("/api/machine/training/poll")) == 2


# =========================================================================
# One session's delivery
# =========================================================================


async def test_a_local_session_is_registered_then_reported_then_ended(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-1"}))
    r.dashboard.answer("/api/machine/training/status", status_answer())
    r.runtime.state = RuntimeState.RUNNING
    r.sync.session_started(manual(r.clock))
    assert r.sync.owed == 1
    for _ in range(12):
        await r.step()
    registered = r.dashboard.to("/api/machine/training/local")
    assert len(registered) == 1
    assert registered[0]["kind"] == "manual"
    assert registered[0]["occupancy"] == "bench"
    assert r.sync.current_session_id == "cloud-1"
    assert r.dashboard.to("/api/machine/training/start") == []  # local: nothing to confirm
    assert r.dashboard.to("/api/machine/training/telemetry")
    assert r.panel.surface.pending is None  # still active: nothing forwarded

    r.runtime.state = RuntimeState.FINISHED
    r.runtime.end_reason = EndReason.OPERATOR_STOP
    r.runtime.stop_reason = "fini"
    await r.step()
    end = r.dashboard.to("/api/machine/training/end")
    assert end == [
        {
            "sessionId": "cloud-1",
            "failed": False,
            "reason": "operator_stop: fini",
            "endedAt": int(r.clock.unix_millis()),
        }
    ]
    assert r.sync.owed == 0
    assert r.sync.current_session_id is None


async def test_an_auto_session_registers_its_programme(tmp_path: Path) -> None:
    r = rig(tmp_path)
    profile = r.panel.services.store.list_profiles()[0]
    r.sync.session_started(
        StartedSession(
            kind=SessionKind.AUTO,
            operator=OPERATOR,
            started_at=r.clock.unix_millis(),
            subject_id=LOCAL_SUBJECT,
            cloud_session_id=None,
            profile=profile,
        )
    )
    await r.step()
    body = r.dashboard.to("/api/machine/training/local")[0]
    assert body["kind"] == "auto"
    assert body["profileId"] == profile.profile_id
    assert body["zoneHighBpm"] == int(profile.zone_high_bpm)
    assert "occupancy" not in body


@pytest.mark.parametrize("answer", [DOWN, ok({"sessionId": 7})])
async def test_a_failed_registration_is_retried_later(tmp_path: Path, answer: Reply) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/local", answer)
    r.sync.session_started(manual(r.clock))
    await r.step()
    await r.step()
    assert len(r.dashboard.to("/api/machine/training/local")) == 1
    r.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-2"}))
    await r.step(RETRY_PERIOD)
    assert r.sync.current_session_id == "cloud-2"


async def test_a_remote_start_is_confirmed_and_retried_while_down(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/start", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    r.dashboard.answer("/api/machine/training/start", ok({"success": True}))
    await r.step(RETRY_PERIOD)
    await r.step(RETRY_PERIOD)
    assert len(r.dashboard.to("/api/machine/training/start")) == 2


async def test_a_start_the_dashboard_refuses_stops_the_machine(tmp_path: Path) -> None:
    """Cancelled on the dashboard between the poll and the arm: nobody wants this session."""
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/start", Err(Refused(400, "not pending")))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    # Nothing is running in this rig, so the surface refuses the end: logged, not raised.
    await r.step(RETRY_PERIOD)
    assert len(r.dashboard.to("/api/machine/training/start")) == 1
    assert r.dashboard.to("/api/machine/training/status") == []  # stop already forwarded


@pytest.mark.parametrize(
    "status",
    [status_answer(stop=True), status_answer(active=False)],
)
async def test_a_stop_from_the_dashboard_is_forwarded_once(tmp_path: Path, status: Reply) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/status", status)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    await r.step(5.0)
    assert len(r.dashboard.to("/api/machine/training/status")) == 1


@pytest.mark.parametrize("status", [DOWN, status_answer()])
async def test_no_stop_is_forwarded_without_a_request(tmp_path: Path, status: Reply) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/status", status)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    assert len(r.dashboard.to("/api/machine/training/status")) == 2


async def test_telemetry_is_kept_while_the_link_is_down(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/telemetry", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(TELEMETRY_PERIOD)
    r.dashboard.answer("/api/machine/training/telemetry", ok({"stored": 1}))
    await r.step(RETRY_PERIOD)
    sent = r.dashboard.to("/api/machine/training/telemetry")
    assert count(sent[-1]["points"]) == 3


async def test_a_refused_batch_is_dropped_not_retried_forever(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/telemetry", NO)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(TELEMETRY_PERIOD)
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1  # the refused points did not hold the end back
    assert ends[0]["reason"] == "fin"
    assert ends[0]["failed"] is True


async def test_a_long_backlog_goes_in_batches_before_the_end(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/telemetry", DOWN)
    r.dashboard.answer("/api/machine/training/status", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    for _ in range(MAX_BATCH + 10):
        await r.step()
    r.dashboard.answer("/api/machine/training/telemetry", ok())
    r.runtime.state = RuntimeState.FINISHED
    await r.step(RETRY_PERIOD)
    assert r.dashboard.to("/api/machine/training/end") == []  # backlog first
    await r.step()
    batches = r.dashboard.to("/api/machine/training/telemetry")
    sizes = [count(b["points"]) for b in batches]
    assert sizes[-2] == MAX_BATCH
    assert len(r.dashboard.to("/api/machine/training/end")) == 1


@pytest.mark.parametrize(("answer", "owed"), [(DOWN, 1), (NO, 0)])
async def test_an_end_is_retried_while_down_and_dropped_when_refused(
    tmp_path: Path, answer: Reply, owed: int
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/end", answer)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    assert r.sync.owed == owed


async def test_an_unobserved_end_is_closed_when_the_next_session_starts(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.sync.session_started(manual(r.clock, remote="first"))
    r.sync.session_started(manual(r.clock, remote="second"))
    await r.step()
    ends = r.dashboard.to("/api/machine/training/end")
    assert [(e["sessionId"], e["reason"]) for e in ends] == [
        ("first", "fin non observee par le lien")
    ]
    assert r.sync.current_session_id == "second"


async def test_the_backlog_of_ended_sessions_is_bounded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path)
    for n in range(MAX_FINISHED + 1):
        r.sync.start_refused(f"s{n}", "offline")
    assert r.sync.owed == MAX_FINISHED
    assert "dropping undelivered session" in caplog.text


async def test_losing_and_regaining_the_link_is_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r = rig(tmp_path)
    await r.step()
    assert "dashboard reachable" in caplog.text
    r.dashboard.answer("/api/machine/heartbeat", DOWN)
    await r.step(HEARTBEAT_PERIOD)
    assert caplog.text.count("dashboard unreachable") == 1
    r.dashboard.answer("/api/machine/heartbeat", NO)  # an answer, even a no, is a link
    await r.step(HEARTBEAT_PERIOD)
    assert r.sync.online
    assert caplog.text.count("dashboard reachable") == 2


# =========================================================================
# The panel's programme path, on the real simulated runtime
# =========================================================================


@dataclass
class Linked:
    clock: ManualClock
    panel: LocalPanel
    dashboard: Dashboard

    async def run(self, seconds: float) -> None:
        for n in range(round(seconds / TICK)):
            self.clock.advance(TICK)
            self.panel.surface.note_presence(OPERATOR)
            await self.panel.ecg_step()
            await self.panel.control_step()
            if n % 5 == 0:
                await self.panel.cloud_step()


def state_of(panel: LocalPanel) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return panel.runtime.state


def linked(tmp_path: Path, **changes: str) -> Linked:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    dashboard = Dashboard()
    panel = build_panel(
        config_of(**changes),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    return Linked(clock=clock, panel=panel, dashboard=dashboard)


async def test_a_dashboard_launch_runs_and_a_dashboard_stop_ends_it(tmp_path: Path) -> None:
    rig_ = linked(tmp_path)
    rig_.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await rig_.run(2.0)
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    assert rig_.dashboard.to("/api/machine/training/start") == [{"sessionId": "remote-1"}]
    assert rig_.panel.cloud is not None
    assert rig_.panel.cloud.current_session_id == "remote-1"

    rig_.dashboard.answer("/api/machine/training/status", status_answer(stop=True))
    await rig_.run(5.0)
    assert state_of(rig_.panel) is RuntimeState.ENDING
    assert rig_.panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    await rig_.panel.close()


async def refused_launch(tmp_path: Path, launch: Mapping[str, JsonValue], **env: str) -> str:
    rig_ = linked(tmp_path, **env)
    rig_.dashboard.answer("/api/machine/training/poll", launch_answer(dict(launch)))
    await rig_.run(3.0)
    assert rig_.panel.runtime.state is RuntimeState.IDLE
    ends = rig_.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    reason = ends[0]["reason"]
    assert isinstance(reason, str)
    await rig_.panel.close()
    return reason


async def test_occupied_riding_must_be_enabled_for_a_programme(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, LAUNCH, OCCUPANCY_OCCUPIED_ENABLED="false")
    assert "OCCUPANCY_OCCUPIED_ENABLED" in reason


async def test_an_unknown_preset_is_refused(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "profileId": "jog_150"})
    assert "inconnu" in reason


async def test_a_preset_too_hard_for_the_rider_is_refused(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "subjectHrMax": 140})
    assert "inadapte a ce passager" in reason


async def test_a_preset_faster_than_the_occupied_ceiling_is_refused(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, LAUNCH, MOTOR_MAX_RPM="200")
    assert "au-dessus du plafond personne a bord" in reason


async def test_tiers_that_disagree_with_the_preset_are_refused_by_the_runtime(
    tmp_path: Path,
) -> None:
    reason = await refused_launch(tmp_path, LAUNCH, HR_HARD_MAX_BPM="150", HR_CRITICAL_BPM="160")
    assert "demarrage refuse" in reason


async def test_a_launch_that_reached_a_panel_without_programmes_is_refused(
    tmp_path: Path,
) -> None:
    """Belt and braces: the link does not poll then, but a start in the mailbox is still refused."""
    rig_ = linked(tmp_path, PROGRAMS_ENABLED="false")
    submitted = rig_.panel.surface.submit_start(
        profile_id=PROFILE,
        operator=OPERATOR,
        total_duration_s=None,
        subject_age=30,
        cloud_session_id="remote-1",
    )
    assert isinstance(submitted, Ok)
    await rig_.run(1.0)
    reason = rig_.dashboard.to("/api/machine/training/end")[0]["reason"]
    assert isinstance(reason, str)
    assert "PROGRAMS_ENABLED=false" in reason
    await rig_.panel.close()


async def test_a_local_programme_is_registered_as_auto(tmp_path: Path) -> None:
    rig_ = linked(tmp_path)
    rig_.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-3"}))
    submitted = rig_.panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(submitted, Ok)
    await rig_.run(2.0)
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    body = rig_.dashboard.to("/api/machine/training/local")[0]
    assert body["kind"] == "auto"
    assert body["operatorName"] == OPERATOR
    await rig_.panel.close()


async def test_a_manual_session_at_the_machine_is_registered(tmp_path: Path) -> None:
    rig_ = linked(tmp_path)
    submitted = rig_.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(submitted, Ok)
    await rig_.run(2.0)
    body = rig_.dashboard.to("/api/machine/training/local")[0]
    assert body["kind"] == "manual"
    assert body["occupancy"] == "bench"
    await rig_.panel.close()


async def test_a_link_failure_never_reaches_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig_ = linked(tmp_path)

    async def broken(_self: CloudSync) -> None:
        raise RuntimeError("bug in the link")

    monkeypatch.setattr(CloudSync, "step", broken)
    await rig_.panel.cloud_step()
    assert "dashboard link step failed" in caplog.text
    await rig_.panel.close()


async def test_an_unlinked_console_has_no_link_step(tmp_path: Path) -> None:
    clock = ManualClock()
    config = config_of(MACHINE_API_KEY="")
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json")
    assert panel.cloud is None
    await panel.cloud_step()
    await panel.close()


async def test_the_console_builds_and_releases_its_own_transport(tmp_path: Path) -> None:
    panel = build_panel(config_of(), clock=ManualClock(), profiles_path=tmp_path / "p.json")
    assert panel.cloud is not None
    await panel.close()


def test_the_fake_dashboard_is_a_transport() -> None:
    transport: CloudTransport = Dashboard()
    assert transport is not None


async def test_an_unlinked_console_still_runs_a_programme(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(
        config_of(MACHINE_API_KEY=""), clock=clock, profiles_path=tmp_path / "p.json"
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    submitted = panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(submitted, Ok)
    clock.advance(TICK)
    await panel.control_step()
    assert state_of(panel) is RuntimeState.RUNNING
    await panel.close()


async def test_an_ended_session_waits_for_its_telemetry_to_go(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/telemetry", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    r.runtime.state = RuntimeState.FINISHED
    await r.step(RETRY_PERIOD)
    assert r.dashboard.to("/api/machine/training/end") == []
    assert r.sync.owed == 1


# =========================================================================
# Exhaustiveness guards: a new variant must be handled, not fall through
# =========================================================================


def test_an_unknown_surface_refusal_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        describe_surface_refusal(cast("StartRefusal", object()))


def test_an_unknown_end_reason_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        end_is_failure(cast("EndReason", "meteor"))


def test_an_unknown_resolve_error_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        describe_resolve_error(cast("ResolveError", object()))


async def test_an_unknown_transport_error_fails_loudly(tmp_path: Path) -> None:
    r = rig(tmp_path)
    alien: Reply = Err(cast("CloudError", object()))
    r.dashboard.answer("/api/machine/heartbeat", alien)
    with pytest.raises(AssertionError):
        await r.step()


async def test_a_launch_without_the_rider_s_age_is_refused(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "subjectAge": None})
    assert "age du passager requis" in reason


async def test_a_launch_for_a_child_is_refused(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "subjectAge": 10})
    assert "passager de 10 ans, minimum 18 ans" in reason


async def test_a_boolean_age_is_not_an_age(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "subjectAge": True})
    assert "age du passager requis" in reason


async def test_the_minimum_age_is_configurable(tmp_path: Path) -> None:
    reason = await refused_launch(tmp_path, {**LAUNCH, "subjectAge": 30}, MIN_RIDER_AGE="40")
    assert "minimum 40 ans" in reason
