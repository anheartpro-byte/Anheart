"""The dashboard link: what goes up, the two things that come down, and failure.

Three layers, tested separately:

* the transport, over ``httpx.MockTransport`` - status codes, bodies that are
  not JSON, a network that is not there;
* :class:`~src.cloud_sync.CloudSync` against a scripted dashboard
  (:class:`Dashboard`) and a runtime whose state the test sets
  (:class:`StubRuntime`), with the REAL control surface and profile store of a
  simulated panel - so a launch really goes through the surface's gates. This
  link records nothing: it declares and ends its sessions, and sends no
  telemetry. What a recording console sends from its records is in
  ``test_record_uplink.py`` and ``test_cloud_journal_e2e.py``;
* the panel itself, in simulation, for the programme path it now runs: every
  gate between a dashboard launch and a turning shaft, and a refusal at each.

No test here reaches a network: every transport is a fake or a mock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from typing import Final, cast, override

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import (
    ARMING_REFUSAL_WORDS,
    DASHBOARD_OPERATOR,
    FINAL_CODES,
    HEARTBEAT_PERIOD,
    LAUNCH_TIMEOUT,
    POLL_PERIOD,
    RETRY_PERIOD,
    STATUS_PERIOD,
    CloudError,
    CloudSync,
    CloudTransport,
    Document,
    HttpxTransport,
    Refused,
    SessionKind,
    StartedSession,
    Unreachable,
    answer_of,
    declaration_of,
    describe_refusal,
    describe_surface_refusal,
    end_is_failure,
    live_row,
    runnable_profiles,
)
from src.contract import ARMING_REFUSALS, CONTRACT_VERSION, SERVER_VERSION_FIELD, ErrorCode
from src.control_surface import (
    LOCAL_SUBJECT,
    ControlSurface,
    EndRefusal,
    EndSession,
    EventKind,
    StartRefusal,
    StartSession,
)
from src.local_config import CardiacTiers, CloudConfig, LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel, describe_resolve_error
from src.medical import SHIPPED_PARAMETERS, MedicalStatus
from src.motor.drive import ControlWord, DriveError
from src.motor.simulated import SimulatedDrive
from src.record.journal import Journal
from src.record.reader import read
from src.record.session import operator_alias
from src.record_uplink import (
    MAX_REFUSALS,
    SUCCESSFUL_ENDS,
    Acked,
    Answer,
    Held,
    RecordSource,
    Refusal,
    RouteMissing,
)
from src.remote_launch import ConfirmationCode, LaunchStage
from src.result import Err, Ok, Result
from src.roster import ROSTER_MAX_AGE, ROSTER_PATH, ROSTER_PERIOD, Roster, RosterState
from src.training.plan import JsonValue, ResolveError
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import Bpm, Monotonic, MotorRpm, Seconds, UnixMillis
from src.web.app import create_app
from tests.medical_support import signed, unsigned
from tests.roster_support import RIDER, RIDER_ID, RIDER_NAME, listed, roster_document
from tests.test_contract import shared

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
    "subjectId": RIDER_ID,
    "subjectHrMax": 170,
    "operatorName": "Dr Manager",
    "subjectAge": 30,
    "subjectLabel": RIDER_NAME,
}


def config_of(medical: MedicalStatus | None = None, **changes: str) -> LocalConfig:
    loaded = load_local_config({**LINKED_ENV, **changes}, medical)
    assert isinstance(loaded, Ok), loaded
    return loaded.value


# =========================================================================
# A scripted dashboard
# =========================================================================

type Reply = Result[Document, CloudError]
type Handler = Callable[[Mapping[str, object]], Reply]


@dataclass(frozen=True, slots=True)
class ReplacedOfferReply:
    replacement: Mapping[str, JsonValue]
    reply: Reply
    expected_session: str
    expected_stage: LaunchStage
    end_due: bool


@dataclass
class Dashboard:
    """A :class:`CloudTransport` that answers from a table and records every call."""

    handlers: dict[str, Handler] = field(default_factory=dict[str, Handler])
    calls: list[tuple[str, str, Mapping[str, object]]] = field(
        default_factory=list[tuple[str, str, Mapping[str, object]]]
    )

    def answer(self, path: str, reply: Reply) -> None:
        self.handlers[path] = lambda _body: reply

    def handle(self, path: str, handler: Handler) -> None:
        self.handlers[path] = handler

    def reply(self, method: str, path: str, body: Mapping[str, object]) -> Reply:
        self.calls.append((method, path, body))
        handler = self.handlers.get(path)
        if handler is not None:
            return handler(body)
        # Unscripted, it is a dashboard of this console's contract with nothing waiting.
        if path == POLL_PATH:
            return launch_answer()
        if path == "/api/machine/heartbeat":
            return ok({SERVER_VERSION_FIELD: CONTRACT_VERSION})
        if path == ROSTER_PATH:
            return ok(roster_document())
        return ok()

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        return self.reply("GET", path, dict(params or {}))

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        return self.reply("POST", path, body)

    def to(self, path: str) -> list[Mapping[str, object]]:
        return [body for _method, called, body in self.calls if called == path]


DOWN: Final[Reply] = Err(Unreachable("no route to host"))
POLL_PATH: Final[str] = "/api/machine/training/poll"
START: Final[str] = "/api/machine/training/start"
STATUS: Final[str] = "/api/machine/training/status"
WEB_KEY: Final[str] = "console-test-key-long-enough"


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


def assert_private_words_absent(
    root: Path, caplog: pytest.LogCaptureFixture, private_words: Sequence[str]
) -> None:
    written = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    for private_word in private_words:
        encoded = re.escape(private_word.encode("utf-8"))
        pattern = re.compile(rb"(?<![A-Za-z0-9_])" + encoded + rb"(?![A-Za-z0-9_])")
        assert pattern.search(written) is None
        assert pattern.search(caplog.text.encode("utf-8")) is None


def expected_dashboard_refusal(detail: str, launch: Mapping[str, JsonValue]) -> str:
    profile_id = launch.get("profileId")
    if isinstance(profile_id, str):
        detail = detail.replace(repr(profile_id), "[retire]")
    age = launch.get("subjectAge")
    if isinstance(age, int) and not isinstance(age, bool):
        detail = detail.replace(f"passager de {age} ans", "[retire]")
    return f"refusee par la machine : {detail}"


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
    journal: Journal | None = None

    def confirm_offer(self, session_id: str = "remote-1", operator: str = OPERATOR) -> None:
        accepted = self.sync.confirm_remote_launch(session_id, operator)
        assert isinstance(accepted, Ok), accepted

    async def step(self, seconds: float = 1.0) -> None:
        """One second of the link's two tasks: the stop watch looks before and after
        the sending, as it does on the console, where it looks four times a second."""
        self.clock.advance(Seconds(seconds))
        await self.sync.watch_stop()
        await self.sync.step()
        await self.sync.watch_stop()


def rig(
    tmp_path: Path,
    *,
    programs: bool = True,
    panel_programs: bool = True,
    tiers: CardiacTiers | None = None,
    records: RecordSource | None = None,
    medical: MedicalStatus | None = None,
    record_root: Path | None = None,
    with_roster: bool = True,
) -> Rig:
    """A real simulated panel's surface and store, with a stub runtime and a scripted link."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    config = config_of(PROGRAMS_ENABLED="true" if panel_programs else "false")
    dashboard = Dashboard()
    journal = None if record_root is None else Journal(record_root, clock)
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
        journal=journal,
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
        medical=medical,
        preflight_start=panel.preflight_remote_start,
        records=records,
        roster=panel.roster if with_roster else None,
    )
    return Rig(
        clock=clock,
        panel=panel,
        dashboard=dashboard,
        runtime=runtime,
        sync=sync,
        journal=journal,
    )


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


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
async def test_httpx_transport_acknowledges_every_2xx(status: int) -> None:
    response = httpx.Response(status, content=b"" if status == 204 else b"{}")
    transport = transport_answering(lambda _request: response)
    result = await transport.post(START, {"sessionId": "remote-1"})
    assert isinstance(result, Ok)
    assert isinstance(answer_of(result), Acked)
    await transport.close()


async def test_httpx_transport_does_not_acknowledge_a_json_redirect() -> None:
    response = httpx.Response(302, json={}, headers={"Location": "/other"})
    transport = transport_answering(lambda _request: response)
    result = await transport.post(START, {"sessionId": "remote-1"})
    assert isinstance(result, Err)
    assert isinstance(answer_of(result), Held)
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
        (httpx.Response(200, text="<html>"), Unreachable("HTTP 200: not a JSON object", 200)),
        (httpx.Response(200, json=[1, 2]), Unreachable("HTTP 200: not a JSON object", 200)),
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


@pytest.mark.parametrize("reason", list(EndReason))
def test_a_record_s_end_is_a_failure_exactly_when_the_runtime_s_is(reason: EndReason) -> None:
    """The end read back from a record is judged on its word; the two must agree."""
    assert (reason.value not in SUCCESSFUL_ENDS) is end_is_failure(reason)


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        (Ok({"stored": 3}), Acked({"stored": 3})),
        (Err(Unreachable("no route to host")), Held("no route to host")),
        (
            Err(Refused(426, "Unsupported machine contract", ErrorCode("contract_unsupported"))),
            Held("contract_unsupported (HTTP 426): Unsupported machine contract", refused=True),
        ),
        (
            Err(Refused(401, "Invalid API key", ErrorCode("unauthorized"))),
            Held("unauthorized (HTTP 401): Invalid API key", refused=True),
        ),
        (Err(Refused(403, "HTTP 403")), Held("sans code (HTTP 403): HTTP 403", refused=True)),
        (Err(Refused(408, "HTTP 408")), Held("sans code (HTTP 408): HTTP 408", refused=True)),
        (Err(Refused(429, "HTTP 429")), Held("sans code (HTTP 429): HTTP 429", refused=True)),
        (Err(Refused(500, "HTTP 500")), Held("sans code (HTTP 500): HTTP 500", refused=True)),
        (Err(Refused(503, "HTTP 503")), Held("sans code (HTTP 503): HTTP 503", refused=True)),
        (Err(Refused(404, "HTTP 404")), RouteMissing()),
        (
            Err(Refused(404, "Session not found", ErrorCode("session_not_found"))),
            Refusal(
                "session_not_found (HTTP 404): Session not found",
                final=True,
                status=404,
                code=ErrorCode("session_not_found"),
            ),
        ),
        (
            Err(Refused(400, "Malformed event", ErrorCode("invalid_request"))),
            Refusal(
                "invalid_request (HTTP 400): Malformed event",
                final=True,
                status=400,
                code=ErrorCode("invalid_request"),
            ),
        ),
        (
            Err(Refused(400, "Try again", ErrorCode("request_failed"))),
            Refusal(
                "request_failed (HTTP 400): Try again",
                final=False,
                status=400,
                code=ErrorCode("request_failed"),
            ),
        ),
        (
            Err(Refused(400, "no")),
            Refusal("sans code (HTTP 400): no", final=False, status=400),
        ),
    ],
)
def test_what_an_exchange_means_for_what_was_sent(reply: Reply, answer: object) -> None:
    """Held: nothing received, nothing dropped. Refused: about the request itself."""
    assert answer_of(reply) == answer


def test_the_codes_that_end_a_request_for_good_are_codes_of_the_shared_contract() -> None:
    codes = shared()["error_codes"]
    assert isinstance(codes, dict)
    assert set(cast("dict[str, object]", codes)) >= FINAL_CODES
    assert "request_failed" not in FINAL_CODES
    assert "contract_unsupported" not in FINAL_CODES


def test_a_session_is_declared_with_its_programme_and_its_occupancy(tmp_path: Path) -> None:
    r = rig(tmp_path)
    profile = r.panel.services.store.list_profiles()[0]
    auto = declaration_of(
        StartedSession(
            kind=SessionKind.AUTO,
            operator=OPERATOR,
            started_at=r.clock.unix_millis(),
            subject_id=LOCAL_SUBJECT,
            cloud_session_id=None,
            profile=profile,
        )
    )
    assert (auto.kind, auto.operator, auto.occupancy) == ("auto", OPERATOR, None)
    assert (auto.profile_id, auto.profile_name) == (profile.profile_id, profile.name)
    assert (auto.zone_low_bpm, auto.zone_high_bpm) == (
        int(profile.zone_low_bpm),
        int(profile.zone_high_bpm),
    )
    assert auto.total_duration_s == float(profile.total_duration_s)
    assert auto.subject_hr_max == int(profile.subject_hr_max)
    bench = declaration_of(manual(r.clock))
    assert (bench.kind, bench.occupancy, bench.profile_id) == ("manual", "bench", None)
    # Nobody named, nobody declared.
    assert (auto.user_id, auto.subject_label) == (None, None)
    assert (bench.user_id, bench.subject_label) == (None, None)


def test_a_session_for_a_rider_is_declared_with_their_identifier_and_name(tmp_path: Path) -> None:
    r = rig(tmp_path)
    named = declaration_of(
        StartedSession(
            kind=SessionKind.MANUAL,
            operator=OPERATOR,
            started_at=r.clock.unix_millis(),
            subject_id=RIDER_ID,
            cloud_session_id=None,
            occupancy=Occupancy.OCCUPIED,
            subject_label=RIDER_NAME,
        )
    )
    assert (named.user_id, named.subject_label) == (RIDER_ID, RIDER_NAME)
    # A label without a rider names nobody: it is not declared.
    stray = declaration_of(replace(manual(r.clock), subject_label=RIDER_NAME))
    assert (stray.user_id, stray.subject_label) == (None, None)


# =========================================================================
# The riders' list
# =========================================================================


def roster_rig(tmp_path: Path, *, programs: bool = True) -> tuple[Rig, Roster]:
    """The link of :func:`rig`, keeping a riders' list up to date."""
    r = rig(tmp_path, programs=programs)
    roster = Roster(linked=True)
    sync = CloudSync(
        clock=r.clock,
        transport=r.dashboard,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=programs,
        preflight_start=r.panel.preflight_remote_start,
        roster=roster,
    )
    return replace(r, sync=sync), roster


def roster_asks(r: Rig) -> int:
    return len(r.dashboard.to(ROSTER_PATH))


async def test_the_list_is_asked_for_at_the_start_then_once_a_period_at_rest(
    tmp_path: Path,
) -> None:
    r, roster = roster_rig(tmp_path)
    await r.step()
    assert roster_asks(r) == 1
    assert roster.find(RIDER_ID, r.clock.monotonic()) == Ok(RIDER)
    for _ in range(round(ROSTER_PERIOD) - 1):
        await r.step()
    assert roster_asks(r) == 1
    await r.step()
    assert roster_asks(r) == 2


@pytest.mark.parametrize("state", [RuntimeState.RUNNING, RuntimeState.ENDING])
async def test_the_list_is_not_asked_for_during_a_session(
    tmp_path: Path, state: RuntimeState
) -> None:
    r, roster = roster_rig(tmp_path)
    r.runtime.state = state
    await r.step(ROSTER_PERIOD)
    await r.step(ROSTER_PERIOD)
    assert roster_asks(r) == 0
    assert roster.state(r.clock.monotonic()) is RosterState.WAITING
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    assert roster_asks(r) == 1


async def test_a_link_without_a_list_asks_for_none(tmp_path: Path) -> None:
    r = rig(tmp_path, with_roster=False)
    await r.step(ROSTER_PERIOD)
    assert roster_asks(r) == 0


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (DOWN, "tableau de bord injoignable : no route to host"),
        (
            Err(Refused(status=500, detail="boom", code=ErrorCode("request_failed"))),
            "request_failed (HTTP 500): boom",
        ),
        (Err(Refused(status=404, detail="HTTP 404")), "sans code (HTTP 404): HTTP 404"),
        (ok({"active": True}), "reponse du tableau de bord sans liste de passagers"),
    ],
    ids=["unreachable", "server-error", "route-unknown", "not-a-list"],
)
async def test_without_a_list_the_one_held_is_kept(
    tmp_path: Path, reply: Reply, problem: str
) -> None:
    r, roster = roster_rig(tmp_path)
    await r.step()
    r.dashboard.answer(ROSTER_PATH, reply)
    await r.step(ROSTER_PERIOD)
    view = roster.view(r.clock.monotonic())
    assert (view.state, view.riders, view.problem) == (RosterState.CURRENT, (RIDER,), problem)


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_key_empties_the_list(tmp_path: Path, status: int) -> None:
    r, roster = roster_rig(tmp_path)
    await r.step()
    r.dashboard.answer(ROSTER_PATH, Err(Refused(status=status, detail="no")))
    await r.step(ROSTER_PERIOD)
    view = roster.view(r.clock.monotonic())
    assert (view.state, view.riders) == (RosterState.REFUSED, ())
    assert view.problem == f"sans code (HTTP {status}): no"


async def test_the_list_s_answer_says_the_dashboard_is_there(tmp_path: Path) -> None:
    # Given a link whose every other request of this step goes unanswered
    # (programmes off: no launch is asked for).
    r, _roster = roster_rig(tmp_path, programs=False)
    r.dashboard.answer("/api/machine/heartbeat", DOWN)
    r.dashboard.answer("/api/machine/profiles", DOWN)
    # When the list is answered, the link is heard online.
    await r.step()
    assert r.sync.online
    # And when it is not, offline.
    r.dashboard.answer(ROSTER_PATH, DOWN)
    await r.step(ROSTER_PERIOD)
    assert not r.sync.online


@pytest.mark.parametrize(
    "reply",
    [cast("Reply", Err(cast("CloudError", object()))), cast("Reply", object())],
    ids=["unknown-error", "not-a-result"],
)
async def test_an_unknown_answer_to_the_list_fails_loudly(tmp_path: Path, reply: Reply) -> None:
    r, _roster = roster_rig(tmp_path)
    r.dashboard.answer(ROSTER_PATH, reply)
    with pytest.raises(AssertionError):
        await r.step()


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
    assert "sessionId" not in live_row(cold, None)
    for _ in range(round(15.0 / TICK)):
        r.clock.advance(TICK)
        await r.panel.ecg_step()
        await r.panel.control_step()
    warm = r.panel.runtime.snapshot()
    assert warm.live_bpm is not None
    assert live_row(warm, "s")["bpm"] == int(warm.live_bpm)
    assert live_row(warm, "s")["sessionId"] == "s"


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


@pytest.mark.parametrize(
    ("medical", "announced"), [(signed(), "0.1.0"), (unsigned(), None), (None, None)]
)
async def test_the_heartbeat_announces_the_signed_medical_version_and_no_other(
    tmp_path: Path, medical: MedicalStatus | None, announced: str | None
) -> None:
    """ANH-139 EX-4: the file's version when its signature holds; null otherwise."""
    r = rig(tmp_path, medical=medical)
    await r.step()
    (beat,) = r.dashboard.to("/api/machine/heartbeat")
    assert beat["medical_parameters_version"] == announced
    assert "medical_parameters_version" in beat


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
    assert r.panel.surface.pending is None
    assert r.dashboard.to(START) == []
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.passenger_name == RIDER_NAME
    assert offer.program_name == "30 min"
    assert offer.launched_by == "Dr Manager"
    r.confirm_offer()
    await r.step()
    pending = pending_start(r.panel.surface)
    assert pending is not None
    requested_profile = LAUNCH["profileId"]
    assert isinstance(requested_profile, str)
    assert pending.profile_id == requested_profile
    assert pending.subject_id == RIDER_ID
    assert pending.subject_hr_max == Bpm(170)
    assert pending.cloud_session_id == "remote-1"
    assert pending.total_duration_s == Seconds(2700.0)
    assert DASHBOARD_OPERATOR in pending.operator
    assert pending.confirmed_by == OPERATOR
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(5.0)
    assert len(r.dashboard.to("/api/machine/training/poll")) == 2


async def test_a_launch_disappears_when_the_poll_no_longer_offers_it(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    assert r.sync.current_remote_launch() is not None
    r.dashboard.answer(POLL_PATH, launch_answer(None))

    await r.step(POLL_PERIOD)

    assert r.sync.current_remote_launch() is None
    rejected = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(rejected, Err)
    assert rejected.error.code.value == "launch_not_pending"
    assert r.dashboard.to(START) == []


async def test_a_launch_needs_a_roster_even_when_the_poll_offers_a_passenger(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path, with_roster=False)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()

    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.passenger_name is None
    refused = r.sync.confirm_remote_launch("remote-1", OPERATOR)

    assert isinstance(refused, Err)
    assert refused.error.code is ConfirmationCode.PASSENGER_UNAVAILABLE
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []


async def test_confirmation_refuses_a_passenger_absent_from_the_current_roster(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.panel.roster.adopt((), r.clock.monotonic())

    rejected = r.sync.confirm_remote_launch("remote-1", OPERATOR)

    assert isinstance(rejected, Err)
    assert rejected.error.code.value == "passenger_unavailable"
    assert "n'est pas dans" in rejected.error.detail
    view = r.sync.current_remote_launch()
    assert view is not None
    assert view.passenger_name is None
    assert view.reason is None
    assert view.passenger_reason == rejected.error.detail
    assert view.stage is LaunchStage.WAITING_CONFIRMATION
    await r.step()
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []
    r.panel.roster.adopt((RIDER,), r.clock.monotonic())
    recovered = r.sync.current_remote_launch()
    assert recovered is not None
    assert recovered.passenger_name == RIDER_NAME
    assert recovered.passenger_reason is None
    assert recovered.reason is None
    assert recovered.stage is LaunchStage.WAITING_CONFIRMATION
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []
    r.dashboard.answer(START, ok())
    reconfirmed = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(reconfirmed, Ok)
    await r.step()
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_roster_refresh_removing_the_rider_abandons_the_queued_confirmation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.clock.advance(ROSTER_PERIOD)
    r.confirm_offer()
    r.dashboard.answer(ROSTER_PATH, ok({"riders": []}))

    await r.sync.step()

    unavailable = r.sync.current_remote_launch()
    assert unavailable is not None
    assert unavailable.stage is LaunchStage.WAITING_CONFIRMATION
    assert unavailable.passenger_name is None
    assert unavailable.reason is None
    assert unavailable.passenger_reason is not None
    assert "n'est pas dans" in unavailable.passenger_reason
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []

    r.dashboard.answer(ROSTER_PATH, ok(roster_document()))
    r.clock.advance(ROSTER_PERIOD)
    await r.sync.step()
    recovered = r.sync.current_remote_launch()
    assert recovered is not None
    assert recovered.passenger_name == RIDER_NAME
    assert recovered.stage is LaunchStage.WAITING_CONFIRMATION
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []

    confirmed = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(confirmed, Ok)
    await r.sync.step()
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    r.journal.drain()
    assert_private_words_absent(
        r.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def test_a_stale_roster_refuses_confirmation_without_ending_the_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.panel.roster.adopt(
        (RIDER,),
        Monotonic(float(r.clock.monotonic()) - float(ROSTER_MAX_AGE) - 1.0),
    )

    refused = r.sync.confirm_remote_launch("remote-1", OPERATOR)

    assert isinstance(refused, Err)
    assert refused.error.code is ConfirmationCode.PASSENGER_UNAVAILABLE
    assert "perimee" in refused.error.detail
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.passenger_name is None
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_second_confirmation_is_refused_while_the_first_is_queued(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    first = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    second = r.sync.confirm_remote_launch("remote-1", OPERATOR)

    assert isinstance(first, Ok)
    assert isinstance(second, Err)
    assert second.error.code is ConfirmationCode.IN_PROGRESS
    assert r.dashboard.to(START) == []


async def test_a_queued_confirmation_is_abandoned_when_another_session_arms(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    queued = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(queued, Ok)
    assert r.dashboard.to(START) == []

    r.runtime.state = RuntimeState.RUNNING
    r.dashboard.answer("/api/machine/training/local", ok({"sessionId": "local-session"}))
    r.sync.session_started(manual(r.clock))
    await r.step()
    assert r.dashboard.to(START) == []
    r.runtime.state = RuntimeState.FINISHED
    r.runtime.end_reason = EndReason.OPERATOR_STOP
    r.runtime.stop_reason = "fini"
    await r.step()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step(POLL_PERIOD)

    offered = r.sync.current_remote_launch()
    assert offered is not None
    assert offered.stage is LaunchStage.WAITING_CONFIRMATION
    await r.step()
    assert r.dashboard.to(START) == []


async def test_a_late_2xx_cannot_reuse_a_confirmation_after_a_local_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = linked(tmp_path, record_root=tmp_path / "records")
    assert r.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            entered.set()
            await release.wait()
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    request = asyncio.create_task(r.panel.cloud_step())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    local = r.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(local, Ok)
    r.clock.advance(TICK)
    r.panel.surface.note_presence(OPERATOR)
    await r.panel.ecg_step()
    await r.panel.control_step()
    assert state_of(r.panel) is RuntimeState.RUNNING
    recorder = r.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert recorder.journal.status(r.clock.monotonic()).path is not None

    ended = r.panel.surface.submit_end(operator=OPERATOR, reason="local stop")
    assert isinstance(ended, Ok)
    for _ in range(50):
        r.clock.advance(TICK)
        r.panel.surface.note_presence(OPERATOR)
        await r.panel.ecg_step()
        await r.panel.control_step()
        if state_of(r.panel) is RuntimeState.FINISHED:
            break
    assert state_of(r.panel) is RuntimeState.FINISHED

    release.set()
    await asyncio.wait_for(request, timeout=1.0)

    assert r.panel.surface.pending is None
    assert state_of(r.panel) is RuntimeState.FINISHED
    assert len(r.dashboard.to(START)) == 1
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["sessionId"] == "remote-1"
    assert ends[0]["failed"] is True
    assert "confirmation distante abandonnee" in str(ends[0]["reason"])
    assert r.panel.cloud is not None
    assert r.panel.cloud.current_remote_launch() is None
    r.clock.advance(TICK)
    await r.panel.control_step()
    assert r.panel.runtime.state is RuntimeState.FINISHED
    assert r.panel.surface.pending is None
    await r.panel.close()
    recorder.journal.drain()
    assert_private_words_absent(
        recorder.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def test_a_late_2xx_while_a_local_session_runs_keeps_the_failed_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = linked(tmp_path, record_root=tmp_path / "records")
    assert r.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            entered.set()
            await release.wait()
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    request = asyncio.create_task(r.panel.cloud_step())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    local = r.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(local, Ok)
    r.clock.advance(TICK)
    r.panel.surface.note_presence(OPERATOR)
    await r.panel.ecg_step()
    await r.panel.control_step()
    assert state_of(r.panel) is RuntimeState.RUNNING

    release.set()
    await asyncio.wait_for(request, timeout=1.0)

    assert state_of(r.panel) is RuntimeState.RUNNING
    assert r.panel.surface.pending is None
    assert r.panel.cloud is not None
    assert r.panel.cloud.current_session_id is None
    assert len(r.dashboard.to(START)) == 1
    assert not any(
        end["sessionId"] == "remote-1" for end in r.dashboard.to("/api/machine/training/end")
    )

    ended = r.panel.surface.submit_end(operator=OPERATOR, reason="local stop")
    assert isinstance(ended, Ok)
    for _ in range(50):
        r.clock.advance(TICK)
        r.panel.surface.note_presence(OPERATOR)
        await r.panel.ecg_step()
        await r.panel.control_step()
        if state_of(r.panel) is RuntimeState.FINISHED:
            break
    assert state_of(r.panel) is RuntimeState.FINISHED

    await r.run(30.0)
    ends = [
        end for end in r.dashboard.to("/api/machine/training/end") if end["sessionId"] == "remote-1"
    ]
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    assert "confirmation distante abandonnee" in str(ends[0]["reason"])
    assert "the end of remote-1 could not be delivered and is given up" not in caplog.text
    await r.panel.close()
    recorder = r.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert_private_words_absent(
        recorder.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def test_a_late_2xx_after_a_local_session_ends_keeps_both_ends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "records"
    local_path = "/api/machine/training/local"
    end_path = "/api/machine/training/end"
    r = linked(tmp_path, record_root=root)
    assert r.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(local_path, ok({"sessionId": "local-9"}))
    await r.run(0.2)
    confirm_linked(r.panel)

    entered = asyncio.Event()
    release = asyncio.Event()
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            entered.set()
            await release.wait()
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    request = asyncio.create_task(r.panel.cloud_step())
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    local = r.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(local, Ok)
    r.clock.advance(TICK)
    r.panel.surface.note_presence(OPERATOR)
    await r.panel.ecg_step()
    await r.panel.control_step()
    assert state_of(r.panel) is RuntimeState.RUNNING

    ended = r.panel.surface.submit_end(operator=OPERATOR, reason="local stop")
    assert isinstance(ended, Ok)
    for _ in range(50):
        r.clock.advance(TICK)
        r.panel.surface.note_presence(OPERATOR)
        await r.panel.ecg_step()
        await r.panel.control_step()
        if state_of(r.panel) is RuntimeState.FINISHED:
            break
    assert state_of(r.panel) is RuntimeState.FINISHED

    release.set()
    await asyncio.wait_for(request, timeout=1.0)
    await r.run(30.0)

    assert len(r.dashboard.to(local_path)) == 1
    ends = r.dashboard.to(end_path)
    assert [(end["sessionId"], end["failed"]) for end in ends] == [
        ("remote-1", True),
        ("local-9", False),
    ]
    assert "could not be delivered and is given up" not in caplog.text

    await r.panel.close()
    recorder = r.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert_private_words_absent(
        root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def test_a_confirmation_racing_with_a_running_session_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.confirm_offer()
    r.clock.advance(POLL_PERIOD)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_get = r.dashboard.get

    async def blocked_get(path: str, params: Mapping[str, str] | None = None) -> Reply:
        if path == POLL_PATH:
            entered.set()
            await release.wait()
        return await original_get(path, params)

    monkeypatch.setattr(r.dashboard, "get", blocked_get)
    step = asyncio.create_task(r.sync.step())
    await asyncio.wait_for(entered.wait(), timeout=0.1)
    r.runtime.state = RuntimeState.RUNNING
    release.set()
    await asyncio.wait_for(step, timeout=1.0)

    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION
    assert offer.reason == "une seance est deja en cours"
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []


async def test_a_changed_poll_clears_a_waiting_start_before_its_loop_timeout(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, ok())
    await r.step()
    r.confirm_offer()
    await r.step()
    assert len(r.dashboard.to(START)) == 1

    r.dashboard.answer(POLL_PATH, launch_answer({**LAUNCH, "sessionId": "remote-2"}))
    await r.step(POLL_PERIOD)
    assert r.sync.current_remote_launch() is None
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(LAUNCH_TIMEOUT)
    assert r.sync.current_remote_launch() is None
    assert len(r.dashboard.to(START)) == 1


async def test_a_changed_offer_invalidates_a_confirmation_queued_for_the_previous_offer(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    client = r.panel.hub.subscribe()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.confirm_offer()
    r.dashboard.answer(POLL_PATH, launch_answer({**LAUNCH, "operatorName": "Another operator"}))
    r.clock.advance(POLL_PERIOD)

    await r.sync.step()

    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.launched_by == "Another operator"
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION
    assert offer.reason == "l'offre a change, une nouvelle confirmation est requise"
    assert r.dashboard.to(START) == []
    assert r.dashboard.to("/api/machine/training/end") == []
    payload = await client.next_payload()
    assert payload.event is not None
    assert payload.event.detail == offer.reason
    assert isinstance(r.sync.confirm_remote_launch("remote-1", OPERATOR), Ok)


async def test_a_definitive_refusal_is_not_reoffered_after_the_poll_removes_it(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, Err(Refused(409, "refused", ErrorCode("right_revoked_before_start"))))
    await r.step()
    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 1
    refused = r.sync.current_remote_launch()
    assert refused is not None
    assert refused.stage is LaunchStage.REFUSED
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(POLL_PERIOD)
    assert r.sync.current_remote_launch() is None
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))

    await r.step(POLL_PERIOD)

    assert r.sync.current_remote_launch() is None
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []


async def test_a_changed_poll_does_not_clear_a_definitive_refusal_for_the_same_session(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, Err(Refused(409, "refused", ErrorCode("right_revoked_before_start"))))
    await r.step()
    r.confirm_offer()
    await r.step()
    refused = r.sync.current_remote_launch()
    assert refused is not None
    assert refused.stage is LaunchStage.REFUSED
    reason = refused.reason
    r.dashboard.answer(POLL_PATH, launch_answer({**LAUNCH, "operatorName": "Another operator"}))

    await r.step(POLL_PERIOD)

    updated = r.sync.current_remote_launch()
    assert updated is not None
    assert updated.launched_by == "Another operator"
    assert updated.stage is LaunchStage.REFUSED
    assert updated.reason == reason
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            ReplacedOfferReply(
                {**LAUNCH, "operatorName": "Changed operator"},
                ok(),
                "remote-1",
                LaunchStage.REFUSED,
                True,
            ),
            id="same-session-2xx",
        ),
        pytest.param(
            ReplacedOfferReply(
                {**LAUNCH, "sessionId": "remote-2"},
                ok(),
                "remote-2",
                LaunchStage.WAITING_CONFIRMATION,
                True,
            ),
            id="replaced-session-2xx",
        ),
        pytest.param(
            ReplacedOfferReply(
                {**LAUNCH, "operatorName": "Changed operator"},
                DOWN,
                "remote-1",
                LaunchStage.WAITING_CONFIRMATION,
                False,
            ),
            id="same-session-no-2xx",
        ),
    ],
)
async def test_a_reply_for_a_replaced_offer_cannot_submit_or_arm_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ReplacedOfferReply,
) -> None:
    r = linked(tmp_path)
    assert r.panel.cloud is not None
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    entered, release = asyncio.Event(), asyncio.Event()
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            r.dashboard.calls.append(("POST", path, body))
            entered.set()
            await release.wait()
            return case.reply
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    request = asyncio.create_task(r.panel.cloud_step())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(case.replacement)))
    r.clock.advance(POLL_PERIOD)

    await r.panel.cloud_step()

    release.set()
    await asyncio.wait_for(request, timeout=1.0)

    assert r.panel.runtime.state is RuntimeState.IDLE
    assert r.panel.surface.pending is None
    assert r.panel.cloud.current_session_id is None
    assert len(r.dashboard.to(START)) == 1
    offer = r.panel.cloud.current_remote_launch()
    assert offer is not None
    assert offer.session_id == case.expected_session
    assert offer.stage is case.expected_stage
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == int(case.end_due)
    if case.end_due:
        assert ends[0]["sessionId"] == "remote-1"
        assert ends[0]["failed"] is True
    await r.panel.close()


async def test_a_refusal_after_the_offer_was_removed_keeps_its_reason(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(POLL_PERIOD)
    assert r.sync.current_remote_launch() is None

    r.sync.start_refused("remote-1", "surface occupee")
    await r.step()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step(POLL_PERIOD)

    assert r.sync.current_remote_launch() is None
    (end,) = r.dashboard.to("/api/machine/training/end")
    assert end["failed"] is True
    assert end["reason"] == "refusee par la machine : surface occupee"


async def test_a_loop_refusal_redacts_private_words_after_the_offer_is_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.confirm_offer()
    r.dashboard.answer(START, ok())
    await r.step()
    assert pending_start(r.panel.surface) is not None

    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(POLL_PERIOD)
    assert r.sync.current_remote_launch() is None

    detail = f"demarrage refuse : programme {PROFILE!r} inconnu; passager de 30 ans"
    r.sync.start_refused("remote-1", detail)
    await r.step()

    (end,) = r.dashboard.to("/api/machine/training/end")
    assert end["reason"] == (
        "refusee par la machine : demarrage refuse : programme [retire] inconnu; [retire]"
    )
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans"))


async def test_a_launch_the_surface_refuses_comes_back_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    r.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await r.step()
    client = r.panel.hub.subscribe()
    r.confirm_offer()
    r.panel.surface.note_launch_confirmed(OPERATOR)
    await r.step()
    assert r.dashboard.to(START) == []
    assert r.panel.surface.pending is None
    refused_offer = r.sync.current_remote_launch()
    assert refused_offer is not None
    assert refused_offer.stage is LaunchStage.REFUSED
    retry = r.sync.confirm_remote_launch(refused_offer.session_id, OPERATOR)
    assert isinstance(retry, Err)
    assert retry.error.code is ConfirmationCode.REFUSED
    assert retry.error.detail == refused_offer.reason
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["sessionId"] == "remote-1"
    assert ends[0]["failed"] is True
    reason = ends[0]["reason"]
    confirmation = await client.next_payload()
    assert confirmation.event is not None
    assert confirmation.event.kind is EventKind.LAUNCH_CONFIRMED
    refusal = await client.next_payload()
    assert refusal.event is not None
    assert "non atteste" in refusal.event.detail
    assert reason == f"refusee par la machine : {refusal.event.detail}"
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_locked_surface_verdict_prevents_the_confirmation_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.panel.runtime.supervisor.latch_estop("test verdict")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    client = r.panel.hub.subscribe()

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.dashboard.to(START) == []
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    refusal = await client.next_payload()
    assert refusal.event is not None
    assert "verdict de securite" in refusal.event.detail
    assert ends[0]["reason"] == f"refusee par la machine : {refusal.event.detail}"
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_waiting_surface_command_prevents_the_confirmation_request(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    reset = r.panel.surface.submit_fault_reset(operator=OPERATOR)
    assert isinstance(reset, Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    client = r.panel.hub.subscribe()

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.dashboard.to(START) == []
    assert r.panel.surface.pending == reset.value
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    refusal = await client.next_payload()
    assert refusal.event is not None
    assert "console occupee" in refusal.event.detail
    assert ends[0]["reason"] == f"refusee par la machine : {refusal.event.detail}"
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_panel_with_programmes_disabled_refuses_before_confirmation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, panel_programs=False, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    client = r.panel.hub.subscribe()

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.dashboard.to(START) == []
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    refusal = await client.next_payload()
    assert refusal.event is not None
    assert "PROGRAMS_ENABLED=false" in refusal.event.detail
    assert ends[0]["reason"] == f"refusee par la machine : {refusal.event.detail}"
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_a_dashboard_launch_fails_closed_without_local_preflight(tmp_path: Path) -> None:
    r = rig(tmp_path)
    sync = CloudSync(
        clock=r.clock,
        transport=r.dashboard,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        roster=r.panel.roster,
    )
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))

    r.clock.advance(Seconds(1.0))
    await sync.step()
    accepted = sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(accepted, Ok)
    r.clock.advance(Seconds(1.0))
    await sync.step()

    assert r.dashboard.to(START) == []
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    assert r.panel.surface.pending is None


@pytest.mark.parametrize("code", sorted(ARMING_REFUSALS))
async def test_a_409_is_shown_logged_and_never_ended_or_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, code: ErrorCode
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert r.journal is not None
    words = ARMING_REFUSAL_WORDS[code]
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    client = r.panel.hub.subscribe()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(
        START,
        Err(Refused(409, f"Declined {PROFILE} passager de 30 ans {RIDER_NAME}", code)),
    )

    await r.step()
    r.confirm_offer()
    r.panel.surface.note_launch_confirmed(OPERATOR)
    await r.step()

    starts = r.dashboard.to(START)
    assert starts == [
        {"sessionId": "remote-1", "startedAt": int(r.clock.unix_millis()), "sessionAgeMs": 0}
    ]
    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE
    assert r.dashboard.to("/api/machine/training/end") == []
    confirmation = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert confirmation.event is not None
    assert confirmation.event.kind is EventKind.LAUNCH_CONFIRMED
    assert confirmation.event.operator == OPERATOR
    payload = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert payload.event is not None
    assert words in payload.event.detail
    assert code in payload.event.detail

    r.journal.drain()
    logbook = (r.journal.root / "logbook" / "events.jsonl").read_text(encoding="utf-8")
    assert "launch_confirmed" in logbook
    assert operator_alias(OPERATOR) in logbook
    assert words in logbook
    assert code in logbook
    assert_private_words_absent(
        r.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step(POLL_PERIOD)
    assert r.dashboard.to(START) == starts
    assert r.dashboard.to("/api/machine/training/end") == []


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(Refused(400, "Not pending", ErrorCode("session_not_pending")), id="400-code"),
        pytest.param(Refused(409, "Not pending", ErrorCode("session_not_pending")), id="409-code"),
        pytest.param(Refused(409, "HTTP 409"), id="409-no-code"),
    ],
)
async def test_another_start_refusal_is_shown_without_arming_or_ending(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, refusal: Refused
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    client = r.panel.hub.subscribe()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, Err(refusal))

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE
    assert r.dashboard.to("/api/machine/training/end") == []
    payload = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert payload.event is not None
    assert "depart distant refuse" in payload.event.detail
    if refusal.code is not None:
        assert refusal.code in payload.event.detail
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step(POLL_PERIOD)
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    refused_offer = r.sync.current_remote_launch()
    assert refused_offer is not None
    assert refused_offer.stage is LaunchStage.REFUSED
    retry = r.sync.confirm_remote_launch(refused_offer.session_id, OPERATOR)
    assert isinstance(retry, Err)
    assert retry.error.code is ConfirmationCode.REFUSED
    assert retry.error.detail == refused_offer.reason
    await r.step()
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME))


async def test_an_unknown_confirmation_answer_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    original_send = CloudSync.send

    async def unknown_answer(sync: CloudSync, path: str, body: Mapping[str, JsonValue]) -> Answer:
        if path == START:
            return cast("Answer", object())
        return await original_send(sync, path, body)

    monkeypatch.setattr(CloudSync, "send", unknown_answer)
    await r.step()
    r.confirm_offer()
    with pytest.raises(AssertionError):
        await r.step()

    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(DOWN, id="offline"),
        pytest.param(Err(Refused(403, "Invalid key", ErrorCode("unauthorized"))), id="key"),
        pytest.param(Err(Refused(429, "Wait")), id="rate-limit"),
        pytest.param(Err(Refused(503, "Unavailable")), id="server-error"),
        pytest.param(Err(Refused(404, "Not found")), id="unknown-route"),
    ],
)
async def test_an_unconfirmed_start_stays_unarmed_and_has_no_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, answer: Reply
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    client = r.panel.hub.subscribe()
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, answer)

    await r.step()
    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 1
    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE
    assert r.dashboard.to("/api/machine/training/end") == []
    payload = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert payload.event is not None
    assert payload.event.detail
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step(POLL_PERIOD)
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    r.journal.drain()
    assert_private_words_absent(
        r.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )

    r.dashboard.answer(START, ok())
    r.confirm_offer()
    await r.step()
    assert len(r.dashboard.to(START)) == 2


@pytest.mark.parametrize(
    ("launch", "private_words", "visible_words"),
    [
        pytest.param(
            {**LAUNCH, "profileId": "private-program-71"},
            ("private-program-71", "passager de 30 ans", RIDER_NAME, "Dr Manager"),
            "demarrage refuse : programme 'private-program-71' inconnu sur cette machine",
            id="unknown-program",
        ),
        pytest.param(
            {**LAUNCH, "profileId": "private-program-72", "subjectAge": 10},
            ("private-program-72", "passager de 10 ans", RIDER_NAME, "Dr Manager"),
            "demarrage refuse : passager de 10 ans, minimum 18 ans "
            "(min_rider_age des parametres medicaux)",
            id="too-young",
        ),
        pytest.param(
            {**LAUNCH, "profileId": "private-program-73", "subjectLabel": ""},
            ("private-program-73", "passager de 30 ans", "Dr Manager"),
            "demarrage refuse : programme 'private-program-73' inconnu sur cette machine",
            id="unknown-program-without-label",
        ),
    ],
)
async def test_a_local_preflight_refusal_keeps_private_words_off_disk_and_end(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    launch: Mapping[str, JsonValue],
    private_words: tuple[str, ...],
    visible_words: str,
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    client = r.panel.hub.subscribe()
    r.dashboard.answer(POLL_PATH, launch_answer(launch))

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.dashboard.to(START) == []
    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    reason = ends[0]["reason"]
    assert isinstance(reason, str)
    assert reason == expected_dashboard_refusal(visible_words, launch)
    payload = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert payload.event is not None
    assert visible_words in payload.event.detail
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.REFUSED
    assert offer.reason == visible_words
    retry = r.sync.confirm_remote_launch(offer.session_id, OPERATOR)
    assert isinstance(retry, Err)
    assert retry.error.code is ConfirmationCode.REFUSED
    assert retry.error.detail == visible_words
    assert r.dashboard.to(START) == []

    r.journal.drain()
    assert_private_words_absent(r.journal.root, caplog, private_words)
    for private_word in private_words:
        assert private_word not in reason


@pytest.mark.parametrize("profile_id", ["de", "e", "passager"])
async def test_short_profile_ids_and_age_words_never_corrupt_the_end_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, profile_id: str
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    launch = {**LAUNCH, "profileId": profile_id, "subjectAge": 10}
    r.dashboard.answer(POLL_PATH, launch_answer(launch))

    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.dashboard.to(START) == []
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    reason = ends[0]["reason"]
    assert isinstance(reason, str)
    assert (
        reason == "refusee par la machine : demarrage refuse : [retire], minimum 18 ans "
        "(min_rider_age des parametres medicaux)"
    )
    assert "passager de 10 ans" not in reason
    r.journal.drain()
    assert_private_words_absent(
        r.journal.root,
        caplog,
        (profile_id, "passager de 10 ans", RIDER_NAME),
    )


@pytest.mark.parametrize(
    ("answer", "visible_words"),
    [
        pytest.param(
            Held(f"{PROFILE}, passager de 30 ans, {RIDER_NAME}, Dr Manager"),
            "confirmation indisponible",
            id="held",
        ),
        pytest.param(RouteMissing(), "route training/start inconnue", id="route-missing"),
    ],
)
async def test_held_and_missing_start_routes_do_not_log_launch_words(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    answer: Answer,
    visible_words: str,
) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records")
    assert r.journal is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    client = r.panel.hub.subscribe()
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))

    async def answer_confirmation(
        _sync: CloudSync, _path: str, _body: Mapping[str, JsonValue]
    ) -> Answer:
        return answer

    monkeypatch.setattr(CloudSync, "send", answer_confirmation)
    await r.step()
    r.confirm_offer()
    await r.step()

    assert r.panel.surface.pending is None
    assert r.runtime.state is RuntimeState.IDLE
    assert r.dashboard.to("/api/machine/training/end") == []
    payload = await asyncio.wait_for(client.next_payload(), timeout=0.1)
    assert payload.event is not None
    assert visible_words in payload.event.detail
    for private_word in (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"):
        assert private_word not in payload.event.detail

    r.journal.drain()
    assert_private_words_absent(
        r.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def test_a_held_start_can_be_confirmed_again_once(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    r.dashboard.answer(START, DOWN)

    await r.step()
    r.confirm_offer()
    await r.step()

    held_offer = r.sync.current_remote_launch()
    assert held_offer is not None
    assert held_offer.stage is LaunchStage.UNCONFIRMED
    assert len(r.dashboard.to(START)) == 1

    r.dashboard.answer(START, ok())
    retry = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(retry, Ok)
    await r.step()

    assert len(r.dashboard.to(START)) == 2
    assert pending_start(r.panel.surface) is not None


async def test_a_launch_the_loop_refuses_comes_back_failed(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await r.step()
    # A refusal for another launch does not end the wait for this one.
    r.sync.start_refused("someone-else", "not this one")
    await r.step()
    assert len(r.dashboard.to("/api/machine/training/poll")) == 1
    r.sync.start_refused("remote-1", "variateur en defaut")
    refused = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(refused, Err)
    assert refused.error.code is ConfirmationCode.REFUSED
    assert refused.error.detail == "variateur en defaut"
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
    r.confirm_offer()
    await r.step()
    await r.step(LAUNCH_TIMEOUT)
    reason = r.dashboard.to("/api/machine/training/end")[0]["reason"]
    assert isinstance(reason, str)
    assert "ni demarre ni refuse" in reason
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.REFUSED
    rejected = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(rejected, Err)
    assert rejected.error.code is ConfirmationCode.REFUSED


async def test_a_late_refusal_does_not_refuse_a_new_remote_offer(tmp_path: Path) -> None:
    r = rig(tmp_path)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.confirm_offer()
    r.dashboard.answer(START, ok())
    await r.step()

    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(float(LAUNCH_TIMEOUT) + 1.0)
    assert r.dashboard.to("/api/machine/training/end")

    second = {**LAUNCH, "sessionId": "remote-2"}
    r.dashboard.answer(POLL_PATH, launch_answer(second))
    await r.step(POLL_PERIOD)
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.session_id == "remote-2"
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION

    detail = f"demarrage refuse : programme {PROFILE!r}; passager de 30 ans"
    r.sync.start_refused("remote-1", detail)
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.session_id == "remote-2"
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION
    assert offer.reason is None

    await r.sync.step()
    ends = r.dashboard.to("/api/machine/training/end")
    assert ends[-1]["sessionId"] == "remote-1"
    assert ends[-1]["reason"] == expected_dashboard_refusal(detail, LAUNCH)
    assert "passager de 30 ans" not in str(ends[-1]["reason"])
    assert PROFILE not in str(ends[-1]["reason"])

    confirmation = r.sync.confirm_remote_launch("remote-2", OPERATOR)
    assert isinstance(confirmation, Ok)


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
    odd = {**LAUNCH, "operatorName": "", "subjectLabel": None, "totalDurationS": True}
    r.dashboard.answer("/api/machine/training/poll", launch_answer(odd))
    await r.step()
    r.confirm_offer()
    await r.step()
    pending = r.panel.surface.pending
    assert isinstance(pending, StartSession)
    assert pending.total_duration_s is None
    assert pending.operator.startswith(DASHBOARD_OPERATOR)


async def test_no_poll_without_programmes(tmp_path: Path) -> None:
    r = rig(tmp_path, programs=False, medical=signed())
    await r.step()
    assert r.dashboard.to("/api/machine/training/poll") == []


async def test_in_bench_mode_a_launch_is_still_asked_for_so_that_it_is_refused(
    tmp_path: Path,
) -> None:
    """Programmes are off because the parameters are unsigned: a waiting launch gets that reason."""
    r = rig(tmp_path, programs=False, medical=unsigned())
    await r.step()
    assert len(r.dashboard.to("/api/machine/training/poll")) == 1


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


async def test_a_local_session_is_registered_then_ended(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-1"}))
    r.dashboard.answer("/api/machine/training/status", status_answer())
    r.runtime.state = RuntimeState.RUNNING
    started = manual(r.clock)
    r.sync.session_started(started)
    assert r.sync.owed == 1
    for _ in range(12):
        await r.step()
    registered = r.dashboard.to("/api/machine/training/local")
    assert len(registered) == 1
    assert registered[0]["kind"] == "manual"
    assert registered[0]["occupancy"] == "bench"
    # The start as this console dated it, and how long ago that was when it said so.
    assert registered[0]["startedAt"] == int(started.started_at)
    assert registered[0]["sessionAgeMs"] == 1000
    assert r.sync.current_session_id == "cloud-1"
    assert r.dashboard.to("/api/machine/training/start") == []  # local: nothing to confirm
    # This console records nothing: no measurement is sent from memory.
    assert r.dashboard.to("/api/machine/training/telemetry") == []
    assert r.dashboard.to("/api/machine/training/events") == []
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
    started = manual(r.clock, remote="remote-1")
    r.sync.session_started(started)
    await r.step()
    r.dashboard.answer("/api/machine/training/start", ok({"success": True}))
    await r.step(RETRY_PERIOD)
    await r.step(RETRY_PERIOD)
    starts = r.dashboard.to("/api/machine/training/start")
    # Each says when the machine dated the start, and how long ago that was by then.
    assert starts == [
        {"sessionId": "remote-1", "startedAt": int(started.started_at), "sessionAgeMs": 1000},
        {"sessionId": "remote-1", "startedAt": int(started.started_at), "sessionAgeMs": 16000},
    ]


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


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every end the link asked the console for: who it is attributed to, and in what words."""
    asked: list[tuple[str, str]] = []
    real = ControlSurface.submit_end

    def spy(
        surface: ControlSurface, *, operator: str, reason: str
    ) -> Result[EndSession, EndRefusal]:
        asked.append((operator, reason))
        return real(surface, operator=operator, reason=reason)

    monkeypatch.setattr(ControlSurface, "submit_end", spy)
    return asked


async def test_the_stop_is_looked_for_by_the_watch_never_by_the_sending_step(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """Two tasks on the console: the sending may wait on a disk or an upload, the watch not."""
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer(stop=True))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    for _ in range(8):
        r.clock.advance(Seconds(1.0))
        await r.sync.step()
    assert r.dashboard.to(STATUS) == []
    assert stops == []

    await r.sync.watch_stop()

    assert len(r.dashboard.to(STATUS)) == 1
    assert stops == [(DASHBOARD_OPERATOR, "arret demande depuis le tableau de bord")]


async def test_the_watch_asks_every_three_seconds_however_often_it_looks(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer())
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    asked_at: list[float] = []
    for _ in range(40):
        r.clock.advance(Seconds(0.25))
        before = len(r.dashboard.to(STATUS))
        await r.sync.watch_stop()
        if len(r.dashboard.to(STATUS)) > before:
            asked_at.append(float(r.clock.monotonic()))

    assert [later - earlier for earlier, later in pairwise(asked_at)] == [float(STATUS_PERIOD)] * 2


async def launch_whose_start_is_answered(
    tmp_path: Path, stops: list[tuple[str, str]], refusal: Refused
) -> Rig:
    """A launch from the dashboard is armed, and its confirmation gets ``refusal``."""
    r = rig(tmp_path)
    r.dashboard.answer(START, Err(refusal))
    r.dashboard.answer(STATUS, status_answer(stop=True))
    r.sync.session_started(manual(r.clock, remote="remote-1"))

    await r.step()

    words = describe_refusal(refusal)
    assert stops == [(DASHBOARD_OPERATOR, f"annulee au tableau de bord ({words})")]
    # Asked once, however long the dashboard goes on refusing; and the
    # confirmation is still owed: it is made again when the dashboard takes it.
    for _ in range(3):
        await r.step(RETRY_PERIOD)
    assert len(stops) == 1
    assert len(r.dashboard.to(START)) == 4
    return r


async def test_a_launch_whose_start_is_answered_426_is_stopped_at_once(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """A dashboard of another contract: nobody there could stop what it does not hold started."""
    refusal = Refused(426, "Unsupported machine contract", ErrorCode("contract_unsupported"))
    await launch_whose_start_is_answered(tmp_path, stops, refusal)


async def test_a_launch_whose_start_is_answered_401_is_stopped_at_once(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """A key the dashboard no longer accepts."""
    refusal = Refused(401, "Invalid API key", ErrorCode("unauthorized"))
    await launch_whose_start_is_answered(tmp_path, stops, refusal)


async def test_a_launch_whose_start_is_answered_503_is_stopped_at_once(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """An error of the server's own."""
    await launch_whose_start_is_answered(tmp_path, stops, Refused(503, "HTTP 503"))


async def test_a_launch_whose_start_gets_no_answer_runs_on_under_the_local_supervisor(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """No network: nothing was refused, and no stop can be asked for from there either."""
    r = rig(tmp_path)
    r.dashboard.answer(START, DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))

    await r.step()
    await r.step(RETRY_PERIOD)

    assert stops == []
    assert len(r.dashboard.to(START)) == 2


async def test_the_first_stop_question_of_a_session_is_asked_as_soon_as_it_is_confirmed(
    tmp_path: Path,
) -> None:
    """In the same look of the watch as the confirmation, and whenever the last question
    about the session before was asked: a second ago does not make this one wait."""
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer())
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.sync.watch_stop()
    assert [call[1] for call in r.dashboard.calls] == [START, STATUS]
    r.runtime.state = RuntimeState.FINISHED
    r.clock.advance(Seconds(1.0))
    await r.sync.step()

    r.runtime.state = RuntimeState.RUNNING
    r.sync.session_started(manual(r.clock, remote="remote-2"))
    await r.sync.watch_stop()

    asked = [call["sessionId"] for call in r.dashboard.to(STATUS)]
    assert asked == ["remote-1", "remote-2"]


async def test_a_stop_forwarded_in_one_session_does_not_hide_a_stop_asked_for_in_the_next(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer(stop=True))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(STATUS_PERIOD)
    assert len(stops) == 1
    r.runtime.state = RuntimeState.FINISHED
    await r.step()

    r.runtime.state = RuntimeState.RUNNING
    r.sync.session_started(manual(r.clock, remote="remote-2"))
    await r.step(STATUS_PERIOD)
    await r.step(STATUS_PERIOD)

    asked = [call["sessionId"] for call in r.dashboard.to(STATUS)]
    assert asked == ["remote-1", "remote-2"]
    assert len(stops) == 2, "the second session's stop is forwarded too, once"


async def test_a_stop_answered_after_its_session_ended_does_not_stop_the_next_one(
    tmp_path: Path, stops: list[tuple[str, str]]
) -> None:
    """The answer was on its way when the session ended and another began: it is about neither."""
    r = rig(tmp_path)
    answering, release = asyncio.Event(), asyncio.Event()

    class Late(Dashboard):
        @override
        async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
            if path == STATUS and not release.is_set():
                answering.set()
                await release.wait()
                return status_answer(stop=True)
            return await super().get(path, params)

    late = Late()
    sync = CloudSync(
        clock=r.clock,
        transport=late,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
    )
    sync.session_started(manual(r.clock, remote="remote-1"))
    r.clock.advance(Seconds(1.0))
    await sync.step()
    watching = asyncio.create_task(sync.watch_stop())
    await answering.wait()

    # The first session ends and a second starts while the dashboard answers.
    r.runtime.state = RuntimeState.FINISHED
    r.clock.advance(Seconds(1.0))
    await sync.step()
    r.runtime.state = RuntimeState.RUNNING
    sync.session_started(manual(r.clock, remote="remote-2"))
    r.clock.advance(Seconds(1.0))
    await sync.step()
    release.set()
    await asyncio.gather(watching)

    assert stops == [], "a stop asked for the first session does not end the second"
    # And the second is still watched for a stop of its own.
    late.answer(STATUS, status_answer(stop=True))
    r.clock.advance(STATUS_PERIOD)
    await sync.watch_stop()
    assert [operator for operator, _reason in stops] == [DASHBOARD_OPERATOR]
    assert late.to(STATUS)[-1] == {"sessionId": "remote-2"}


async def test_no_stop_is_asked_before_the_dashboard_holds_the_session_started(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/start", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    assert r.dashboard.to("/api/machine/training/status") == []


async def test_an_end_is_retried_while_the_link_is_down(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/end", DOWN)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    assert r.sync.owed == 1
    r.dashboard.answer("/api/machine/training/end", ok())
    await r.step(RETRY_PERIOD)
    assert r.sync.owed == 0
    assert len(r.dashboard.to("/api/machine/training/end")) == 2


async def test_an_end_refused_without_a_final_code_is_tried_three_times_then_given_up(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/end", NO)
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    assert r.sync.owed == 1
    for _ in range(MAX_REFUSALS - 1):
        await r.step(RETRY_PERIOD)
    assert len(r.dashboard.to("/api/machine/training/end")) == MAX_REFUSALS
    assert r.sync.owed == 0
    await r.step(RETRY_PERIOD)
    assert len(r.dashboard.to("/api/machine/training/end")) == MAX_REFUSALS


async def test_an_end_refused_for_good_is_given_up_at_once(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(
        "/api/machine/training/end",
        Err(Refused(400, "Session not found", ErrorCode("session_not_found"))),
    )
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    r.runtime.state = RuntimeState.FINISHED
    await r.step()
    assert len(r.dashboard.to("/api/machine/training/end")) == 1
    assert r.sync.owed == 0


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


async def test_failed_launch_ends_wait_in_order_and_no_launch_is_asked_meanwhile(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/training/end", DOWN)
    r.sync.start_refused("s0", "offline")
    await r.step()
    assert r.sync.owed == 1
    assert r.dashboard.to(POLL_PATH) == []
    r.sync.start_refused("s1", "offline")
    assert r.sync.owed == 2
    assert "the end of s0 could not be delivered and is given up" not in caplog.text
    r.dashboard.answer("/api/machine/training/end", ok())
    await r.step(RETRY_PERIOD)
    await r.step()
    ends = r.dashboard.to("/api/machine/training/end")
    assert [end["sessionId"] for end in ends] == ["s0", "s0", "s1"]
    assert all(end["failed"] is True for end in ends)
    assert all(end["reason"] == "refusee par la machine : offline" for end in ends)
    assert r.sync.owed == 0
    await r.step(POLL_PERIOD)
    assert len(r.dashboard.to(POLL_PATH)) == 1


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
            # The link's two tasks: the stop watch four times a second (here at
            # each tick), the sending once a second.
            await self.panel.cloud_stop_step()
            if n % 5 == 0:
                await self.panel.cloud_step()


def state_of(panel: LocalPanel) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return panel.runtime.state


def pending_start(surface: ControlSurface) -> StartSession | None:
    pending = surface.pending
    return pending if isinstance(pending, StartSession) else None


def linked(
    tmp_path: Path,
    medical: MedicalStatus | None = None,
    *,
    record_root: Path | None = None,
    env: Mapping[str, str] | None = None,
    **changes: str,
) -> Linked:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    dashboard = Dashboard()
    journal = None if record_root is None else Journal(record_root, clock)
    environment = {} if env is None else dict(env)
    environment.update(changes)
    panel = build_panel(
        config_of(medical, **environment),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
        journal=journal,
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    return Linked(clock=clock, panel=panel, dashboard=dashboard)


def confirm_linked(panel: LocalPanel, operator: str = OPERATOR) -> None:
    cloud = panel.cloud
    assert cloud is not None
    accepted = cloud.confirm_remote_launch("remote-1", operator)
    assert isinstance(accepted, Ok), accepted
    panel.surface.note_launch_confirmed(operator)


def observe_dashboard_and_drive(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> list[str]:
    writes: list[str] = []
    original_reply = Dashboard.reply
    original_command = SimulatedDrive.write_command
    original_speed = SimulatedDrive.write_speed

    def observe_reply(
        dashboard: Dashboard, method: str, path: str, body: Mapping[str, object]
    ) -> Reply:
        if path == START:
            order.append("training/start")
        return original_reply(dashboard, method, path, body)

    async def command(drive: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
        writes.append(f"command:{word.name}")
        order.append("drive")
        return await original_command(drive, word)

    async def speed(drive: SimulatedDrive, rpm: MotorRpm) -> Result[None, DriveError]:
        writes.append(f"speed:{rpm}")
        order.append("drive")
        return await original_speed(drive, rpm)

    monkeypatch.setattr(Dashboard, "reply", observe_reply)
    monkeypatch.setattr(SimulatedDrive, "write_command", command)
    monkeypatch.setattr(SimulatedDrive, "write_speed", speed)
    return writes


async def test_a_dashboard_launch_runs_and_a_dashboard_stop_ends_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    observe_dashboard_and_drive(monkeypatch, order)
    rig_ = linked(tmp_path, record_root=tmp_path / "records")
    rig_.dashboard.answer("/api/machine/training/poll", launch_answer(dict(LAUNCH)))
    await rig_.run(0.2)
    confirm_linked(rig_.panel)
    expected_start = int(rig_.clock.unix_millis()) + 200
    await rig_.run(1.8)
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    starts = rig_.dashboard.to("/api/machine/training/start")
    assert len(starts) == 1
    assert starts[0]["sessionId"] == "remote-1"
    assert starts[0]["sessionAgeMs"] == 0
    started_at = starts[0]["startedAt"]
    assert isinstance(started_at, int)
    assert abs(started_at - expected_start) <= 1
    assert order.index("training/start") < order.index("drive")
    assert rig_.panel.cloud is not None
    assert rig_.panel.cloud.current_session_id == "remote-1"

    rig_.dashboard.answer("/api/machine/training/status", status_answer(stop=True))
    await rig_.run(5.0)
    assert state_of(rig_.panel) is RuntimeState.ENDING
    assert rig_.panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    assert len(rig_.dashboard.to("/api/machine/training/start")) == 1
    await rig_.panel.close()


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
async def test_httpx_2xx_arms_the_real_panel(
    status: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(status, content=b"" if status == 204 else b"{}")
    http_transport = transport_answering(lambda _request: response)
    r = linked(tmp_path)
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            r.dashboard.calls.append(("POST", path, body))
            return await http_transport.post(path, body)
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(1.8)

    assert r.panel.runtime.state is RuntimeState.RUNNING
    assert r.panel.cloud is not None
    assert r.panel.cloud.current_session_id == "remote-1"
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    await r.panel.close()
    await http_transport.close()


async def test_httpx_redirect_never_arms_or_ends_the_real_panel(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = httpx.Response(302, json={}, headers={"Location": "/other"})
    http_transport = transport_answering(lambda _request: response)
    r = linked(tmp_path, record_root=tmp_path / "records")
    assert r.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    original_post = r.dashboard.post

    async def post(path: str, body: Mapping[str, JsonValue]) -> Reply:
        if path == START:
            r.dashboard.calls.append(("POST", path, body))
            return await http_transport.post(path, body)
        return await original_post(path, body)

    monkeypatch.setattr(r.dashboard, "post", post)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(1.8)

    assert r.panel.runtime.state is RuntimeState.IDLE
    assert r.panel.cloud is not None
    assert r.panel.cloud.current_session_id is None
    assert r.panel.surface.pending is None
    assert len(r.dashboard.to(START)) == 1
    assert r.dashboard.to("/api/machine/training/end") == []
    offer = r.panel.cloud.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.UNCONFIRMED
    recorder = r.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert_private_words_absent(
        recorder.journal.root, caplog, (PROFILE, "passager de 30 ans", RIDER_NAME)
    )
    await r.panel.close()
    await http_transport.close()


async def test_the_confirming_operator_is_recorded_and_the_roster_name_stays_off_disk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    confirmer = "Console Operator"
    caplog.set_level(logging.INFO)
    rig_ = linked(tmp_path, record_root=tmp_path / "records")
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await rig_.run(0.2)
    web = replace(config_of().web, token=WEB_KEY)
    app = create_app(services=rig_.panel.services, config=web)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        headers = {"X-Anheart-Token": WEB_KEY}
        offered = await session.get("/api/remote-launch", headers=headers)
        assert offered.status_code == 200
        assert offered.json()["passenger_name"] == RIDER_NAME
        assert offered.json()["program"] == "30 min"
        assert offered.json()["launched_by"] == "Dr Manager"
        response = await session.post(
            "/api/remote-launch/confirm",
            headers=headers,
            json={"session_id": "remote-1", "operator": confirmer},
        )
        assert response.status_code == 202
        assert rig_.dashboard.to(START) == []
    await rig_.run(1.8)
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    assert rig_.dashboard.to(START)
    assert rig_.dashboard.to("/api/machine/training/end") == []
    rig_.dashboard.answer(STATUS, status_answer(stop=True))
    await rig_.run(5.0)
    await rig_.panel.close()

    recorder = rig_.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    root = recorder.journal.root
    session_events = tuple(
        path for path in root.rglob("events.jsonl") if path.parent.name != "logbook"
    )
    assert len(session_events) == 1
    loaded = read(session_events[0].parent)
    assert isinstance(loaded, Ok)
    confirmations = [event for event in loaded.value.events if event.detail == "launch_confirmed"]
    assert len(confirmations) == 1
    assert confirmations[0].actor == operator_alias(confirmer)
    logbook = (root / "logbook" / "events.jsonl").read_text(encoding="utf-8")
    assert "launch_confirmed" in logbook
    assert operator_alias(confirmer) in logbook
    written = b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    assert RIDER_NAME.encode("utf-8") not in written
    assert RIDER_NAME not in caplog.text


async def test_a_409_never_commands_the_simulated_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    writes = observe_dashboard_and_drive(monkeypatch, order)
    rig_ = linked(tmp_path)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    rig_.dashboard.answer(
        START,
        Err(Refused(409, "Right revoked", ErrorCode("right_revoked_before_start"))),
    )

    await rig_.run(0.2)
    confirm_linked(rig_.panel)
    await rig_.run(1.8)

    assert rig_.panel.runtime.state is RuntimeState.IDLE
    assert writes == []
    assert order == ["training/start"]
    assert len(rig_.dashboard.to(START)) == 1
    assert rig_.dashboard.to("/api/machine/training/end") == []


async def test_a_rechecked_arm_refusal_after_2xx_ends_without_drive_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    writes = observe_dashboard_and_drive(monkeypatch, order)
    rig_ = linked(tmp_path, record_root=tmp_path / "records")
    caplog.set_level(logging.INFO, logger="src.cloud_sync")

    def latch_verdict_before_ack(_body: Mapping[str, object]) -> Reply:
        rig_.panel.runtime.supervisor.latch_estop("verdict pendant la confirmation")
        return ok()

    rig_.dashboard.handle(START, latch_verdict_before_ack)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))

    await rig_.run(0.2)
    confirm_linked(rig_.panel)
    await rig_.run(2.8)

    ends = rig_.dashboard.to("/api/machine/training/end")
    assert len(rig_.dashboard.to(START)) == 1
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    assert writes == []
    assert order == ["training/start"]
    assert rig_.panel.runtime.state is not RuntimeState.RUNNING
    await rig_.panel.close()
    recorder = rig_.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert_private_words_absent(
        recorder.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


@pytest.mark.parametrize("stale", [False, True], ids=["removed", "expired"])
async def test_the_arm_rechecks_a_roster_removed_or_expired_after_2xx(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stale: bool
) -> None:
    r = linked(tmp_path, record_root=tmp_path / "records")
    assert r.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")

    def acknowledge_start(_body: Mapping[str, object]) -> Reply:
        if stale:
            r.panel.roster.adopt(
                (RIDER,),
                Monotonic(float(r.clock.monotonic()) - float(ROSTER_MAX_AGE) - 1.0),
            )
        else:
            r.panel.roster.adopt((), r.clock.monotonic())
        return ok()

    r.dashboard.handle(START, acknowledge_start)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(2.8)

    assert r.panel.runtime.state is RuntimeState.IDLE
    assert r.panel.surface.pending is None
    assert len(r.dashboard.to(START)) == 1
    ends = r.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["sessionId"] == "remote-1"
    assert ends[0]["failed"] is True
    offer = r.panel.cloud.current_remote_launch() if r.panel.cloud is not None else None
    assert offer is not None
    assert offer.stage is LaunchStage.REFUSED
    assert offer.reason is not None
    assert ends[0]["reason"] == f"refusee par la machine : {offer.reason}"
    recorder = r.panel.recorder
    assert recorder is not None
    await r.panel.close()
    recorder.journal.drain()
    assert_private_words_absent(
        recorder.journal.root,
        caplog,
        (PROFILE, "passager de 30 ans", RIDER_NAME, "Dr Manager"),
    )


async def refused_launch(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    launch: Mapping[str, JsonValue],
    medical: MedicalStatus | None = None,
    *,
    after_confirmation: bool = False,
    env: Mapping[str, str] | None = None,
) -> str:
    rig_ = linked(tmp_path, medical, record_root=tmp_path / "records", env=env)
    assert rig_.panel.recorder is not None
    caplog.set_level(logging.INFO, logger="src.cloud_sync")
    subscriber = rig_.panel.hub.subscribe()
    rig_.dashboard.answer("/api/machine/training/poll", launch_answer(dict(launch)))
    await rig_.run(0.2)
    confirm_linked(rig_.panel)
    await rig_.run(2.8)
    assert rig_.panel.runtime.state is RuntimeState.IDLE
    if not after_confirmation:
        assert rig_.dashboard.to(START) == []
    ends = rig_.dashboard.to("/api/machine/training/end")
    assert len(ends) == 1
    assert ends[0]["failed"] is True
    expected = EventKind.REFUSED if after_confirmation else EventKind.DASHBOARD
    refusal = None
    for _ in range(5):
        payload = await subscriber.next_payload()
        if payload.event is not None and payload.event.kind is expected:
            refusal = payload.event
            break
    assert refusal is not None
    assert ends[0]["reason"] == expected_dashboard_refusal(refusal.detail, launch)
    recorder = rig_.panel.recorder
    await rig_.panel.close()
    recorder.journal.drain()
    profile_id = launch.get("profileId")
    age = launch.get("subjectAge")
    private_words = (
        RIDER_NAME,
        "Dr Manager",
        profile_id if isinstance(profile_id, str) else "",
        f"passager de {age} ans" if isinstance(age, int) and not isinstance(age, bool) else "",
    )
    assert_private_words_absent(
        recorder.journal.root,
        caplog,
        tuple(word for word in private_words if word),
    )
    return refusal.detail


async def test_occupied_riding_must_be_enabled_for_a_programme(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(
        tmp_path, caplog, LAUNCH, env={"OCCUPANCY_OCCUPIED_ENABLED": "false"}
    )
    assert "OCCUPANCY_OCCUPIED_ENABLED" in reason


@pytest.mark.parametrize("profile_id", ["jog_150", ""])
async def test_an_unknown_preset_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, profile_id: str
) -> None:
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "profileId": profile_id})
    assert "inconnu" in reason


async def test_a_preset_too_hard_for_the_rider_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "subjectHrMax": 140})
    assert "inadapte a ce passager" in reason


async def test_a_preset_faster_than_the_occupied_ceiling_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(tmp_path, caplog, LAUNCH, env={"MOTOR_MAX_RPM": "200"})
    assert "au-dessus du plafond personne a bord" in reason


async def test_tiers_that_disagree_with_the_preset_are_refused_by_the_runtime(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    screened = signed(
        replace(SHIPPED_PARAMETERS, hr_hard_max_bpm=Bpm(150), hr_critical_bpm=Bpm(160))
    )
    reason = await refused_launch(tmp_path, caplog, LAUNCH, screened, after_confirmation=True)
    assert "demarrage refuse" in reason


async def test_a_launch_to_a_console_in_bench_mode_is_refused_for_its_medical_parameters(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """ANH-139 EX-3: unsigned parameters, whatever the .env enables: the reason goes back."""
    reason = await refused_launch(tmp_path, caplog, LAUNCH, unsigned())
    assert reason.startswith("parametres medicaux non signes")


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
    (end,) = rig_.dashboard.to("/api/machine/training/end")
    assert end["failed"] is True
    reason = end["reason"]
    assert isinstance(reason, str)
    assert reason.startswith("refusee par la machine : seances programmees desactivees")
    assert "PROGRAMS_ENABLED=false" in reason
    await rig_.panel.close()


async def test_a_local_programme_is_registered_as_auto_for_its_rider(tmp_path: Path) -> None:
    # Given a console that has the dashboard's riders' list.
    rig_ = linked(tmp_path)
    rig_.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-3"}))
    await rig_.panel.cloud_step()
    # When a programme is started at the console for a rider of that list.
    submitted = rig_.panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_id=RIDER_ID
    )
    assert isinstance(submitted, Ok)
    await rig_.run(2.0)
    # Then it runs, and the dashboard is told whose session it is.
    assert rig_.panel.runtime.state is RuntimeState.RUNNING
    body = rig_.dashboard.to("/api/machine/training/local")[0]
    assert body["kind"] == "auto"
    assert body["operatorName"] == OPERATOR
    assert body["userId"] == RIDER_ID
    assert body["subjectLabel"] == RIDER_NAME
    assert body["subjectHrMax"] == 190
    await rig_.panel.close()


async def test_a_manual_session_at_the_machine_is_registered(tmp_path: Path) -> None:
    rig_ = linked(tmp_path)
    submitted = rig_.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(submitted, Ok)
    await rig_.run(2.0)
    body = rig_.dashboard.to("/api/machine/training/local")[0]
    assert body["kind"] == "manual"
    assert body["occupancy"] == "bench"
    # Nobody is on the bench: no rider is declared.
    assert "userId" not in body
    assert "subjectLabel" not in body
    await rig_.panel.close()


async def test_an_occupied_manual_session_is_registered_for_its_rider(tmp_path: Path) -> None:
    rig_ = linked(tmp_path)
    # Long enough for the list, and for the heart rate a person on board needs.
    await rig_.run(12.0)
    submitted = rig_.panel.surface.submit_start_manual(
        occupancy=Occupancy.OCCUPIED, operator=OPERATOR, subject_id=RIDER_ID
    )
    assert isinstance(submitted, Ok)
    await rig_.run(2.0)
    body = rig_.dashboard.to("/api/machine/training/local")[0]
    assert body["occupancy"] == "occupied"
    assert body["userId"] == RIDER_ID
    assert body["subjectLabel"] == RIDER_NAME
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


async def test_a_failure_of_the_stop_watch_never_reaches_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig_ = linked(tmp_path)

    async def broken(_self: CloudSync) -> None:
        raise RuntimeError("bug in the watch")

    monkeypatch.setattr(CloudSync, "watch_stop", broken)
    await rig_.panel.cloud_stop_step()
    assert "dashboard stop watch failed" in caplog.text
    await rig_.panel.close()


async def test_an_unlinked_console_has_no_link_step(tmp_path: Path) -> None:
    clock = ManualClock()
    config = config_of(MACHINE_API_KEY="")
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json")
    assert panel.cloud is None
    await panel.cloud_step()
    await panel.cloud_stop_step()
    await panel.close()


async def test_the_console_builds_and_releases_its_own_transport(tmp_path: Path) -> None:
    panel = build_panel(config_of(), clock=ManualClock(), profiles_path=tmp_path / "p.json")
    assert panel.cloud is not None
    await panel.close()


def test_the_fake_dashboard_is_a_transport() -> None:
    transport: CloudTransport = Dashboard()
    assert transport is not None


async def test_an_unlinked_console_offers_nobody_and_refuses_a_programme(tmp_path: Path) -> None:
    # Given a console with no dashboard: the riders' list comes from the site.
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(
        config_of(MACHINE_API_KEY=""), clock=clock, profiles_path=tmp_path / "p.json"
    )
    assert panel.roster.state(clock.monotonic()) is RosterState.UNLINKED
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    # When a programme is asked for a rider it cannot know.
    submitted = panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_id=RIDER_ID
    )
    assert isinstance(submitted, Ok)
    clock.advance(TICK)
    await panel.control_step()
    # Then nothing turns, and the operator reads why.
    assert state_of(panel) is RuntimeState.IDLE
    assert [fate.detail for fate in panel.surface.fates] == [
        "demarrage refuse : aucun tableau de bord configure sur cette console : "
        "la liste des passagers vient du site"
    ]
    await panel.close()


async def test_an_unlinked_console_runs_a_programme_for_a_rider_it_holds(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(
        config_of(MACHINE_API_KEY=""),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        roster=listed(clock),
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    submitted = panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_id=RIDER_ID
    )
    assert isinstance(submitted, Ok)
    clock.advance(TICK)
    await panel.control_step()
    assert state_of(panel) is RuntimeState.RUNNING
    await panel.close()


# =========================================================================
# Exhaustiveness guards: a new variant must be handled, not fall through
# =========================================================================


def test_an_unknown_surface_refusal_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        describe_surface_refusal(cast(StartRefusal, object()))


def test_an_unknown_end_reason_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        end_is_failure(cast("EndReason", "meteor"))


def test_an_unknown_resolve_error_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        describe_resolve_error(cast("ResolveError", object()))


@pytest.mark.parametrize("path", ["/api/machine/heartbeat", "/api/machine/profiles"])
async def test_an_unknown_transport_error_fails_loudly(tmp_path: Path, path: str) -> None:
    r = rig(tmp_path)
    alien: Reply = Err(cast("CloudError", object()))
    r.dashboard.answer(path, alien)
    with pytest.raises(AssertionError):
        await r.step()
    with pytest.raises(AssertionError):
        answer_of(alien)


async def test_an_unknown_heartbeat_result_fails_loudly(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer("/api/machine/heartbeat", cast("Reply", object()))

    with pytest.raises(AssertionError):
        await r.step()


async def test_a_launch_without_the_rider_s_age_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "subjectAge": None})
    assert "age du passager requis" in reason


async def test_a_launch_for_a_child_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "subjectAge": 10})
    assert "passager de 10 ans, minimum 18 ans" in reason


async def test_a_boolean_age_is_not_an_age(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "subjectAge": True})
    assert "age du passager requis" in reason


async def test_the_minimum_age_is_configurable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    older = signed(replace(SHIPPED_PARAMETERS, min_rider_age=40))
    reason = await refused_launch(tmp_path, caplog, {**LAUNCH, "subjectAge": 30}, older)
    assert "minimum 40 ans" in reason
