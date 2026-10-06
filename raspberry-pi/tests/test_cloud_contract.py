"""The dashboard link under the versioned contract (ANH-133).

What this file establishes, requirement by requirement:

* EX-2: every request carries ``X-Anheart-Contract``, and the heartbeat says
  which software and which contract this console runs;
* EX-3, seen from the machine: a 426 is read, said once on the console, and
  changes nothing about the machine;
* EX-4, the safety requirement: a launch that comes down in a poll answer of
  another major (or of no stated version) is **never** submitted to the
  control surface. It is said on the console and goes back to the dashboard
  as a failed session;
* EX-5: a refusal is read as ``{error: <stable code>, message}`` and the code
  is what the log carries;
* the one thing that crosses every contract: a stop request. It reaches a
  running session whatever major the dashboard is of, and it is the only
  thing taken from an answer of another major.

The rig is the one of ``test_cloud_sync.py``: the REAL control surface and
profile store of a simulated panel, a scripted dashboard, no network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src import cloud_sync, contract
from src.clock import ManualClock
from src.cloud_sync import (
    HEARTBEAT_PERIOD,
    INCOMPATIBLE_REPEAT,
    POLL_PERIOD,
    CloudError,
    CloudSync,
    HttpxTransport,
    Refused,
    describe_refusal,
)
from src.contract import (
    CONTRACT_HEADER,
    CONTRACT_UNSUPPORTED,
    CONTRACT_VERSION,
    SERVER_VERSION_FIELD,
    UNKNOWN_SOFTWARE_VERSION,
    ContractVersion,
    ErrorCode,
    SoftwareVersion,
    read_software_version,
)
from src.control_surface import EventKind, RunState, StartSession
from src.local_config import CloudConfig
from src.local_panel import build_panel
from src.result import Err, Ok
from src.telemetry import PayloadKind, TelemetryClient
from src.training.runtime import RuntimeState
from src.units import Monotonic, Seconds, UnixMillis
from tests.test_cloud_sync import (
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    PROFILE,
    Dashboard,
    Linked,
    Reply,
    Rig,
    config_of,
    launch_answer,
    linked,
    manual,
    ok,
    rig,
    state_of,
    status_answer,
    transport_answering,
)
from tests.test_contract import shared

HEARTBEAT: Final[str] = "/api/machine/heartbeat"
END: Final[str] = "/api/machine/training/end"
STATUS: Final[str] = "/api/machine/training/status"
STOP_REASON: Final[str] = "arret demande depuis le tableau de bord"

OTHER_MAJOR: Final[str] = "serveur incompatible (contrat 1.0 vs 2.0)"
NO_VERSION: Final[str] = "serveur incompatible (contrat 1.0 vs inconnu)"


def unsupported(*supported: str) -> Reply:
    """The 426 of a dashboard that serves only the majors given."""
    return Err(Refused(426, "Unsupported machine contract", CONTRACT_UNSUPPORTED, supported))


async def refusals_shown(watcher: TelemetryClient) -> list[str]:
    """Every refusal the operator's screen has received so far, oldest first."""
    shown: list[str] = []
    while True:
        try:
            payload = await asyncio.wait_for(watcher.next_payload(), 0.001)
        except TimeoutError:
            return shown
        event = payload.event
        if payload.kind is PayloadKind.EVENT and event is not None:
            assert event.kind is EventKind.REFUSED
            assert event.operator == ""  # nobody's command: the link's own news
            shown.append(event.detail)


def attested(tmp_path: Path) -> tuple[Rig, TelemetryClient]:
    """A rig whose console WOULD arm a launch, and a screen watching its events."""
    r = rig(tmp_path)
    watcher = r.panel.hub.subscribe()
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    return r, watcher


async def attestation_seen(watcher: TelemetryClient) -> None:
    """Drain the attestation event, so only the link's refusals remain to be read."""
    payload = await asyncio.wait_for(watcher.next_payload(), 0.001)
    assert payload.event is not None
    assert payload.event.kind is EventKind.ATTESTED


# =========================================================================
# EX-2: the header on every request, the versions in the heartbeat
# =========================================================================


async def test_ex2_every_request_carries_the_contract_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    # The injected client knows nothing of the contract: the transport adds it.
    transport = transport_answering(handler)
    await transport.get("/a", {"sessionId": "s"})
    await transport.post("/b", {"x": 1})
    assert [request.headers[CONTRACT_HEADER] for request in seen] == [CONTRACT_VERSION] * 2
    assert CONTRACT_HEADER == "X-Anheart-Contract"
    await transport.close()


async def test_ex2_the_console_s_own_client_carries_the_header_and_the_key() -> None:
    transport = HttpxTransport(CloudConfig(url="https://example.convex.site", api_key="k"))
    headers = transport._client.headers  # pyright: ignore[reportPrivateUsage]  # the client it built
    assert headers[CONTRACT_HEADER] == CONTRACT_VERSION
    assert headers["Authorization"] == "Bearer k"
    await transport.close()


async def test_ex2_the_heartbeat_names_the_software_and_the_contract(tmp_path: Path) -> None:
    r = rig(tmp_path)
    sync = CloudSync(
        clock=r.clock,
        transport=r.dashboard,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        software_version=SoftwareVersion("pi-0.4.2"),
    )
    r.clock.advance(Seconds(1.0))
    await sync.step()
    beat = r.dashboard.to(HEARTBEAT)[0]
    assert beat["software_version"] == "pi-0.4.2"
    assert beat["contract_version"] == CONTRACT_VERSION
    # Present and null: this console has neither a signed medical file nor a
    # configuration hash yet, and says so rather than leaving the keys out.
    assert beat["medical_parameters_version"] is None
    assert beat["config_hash"] is None
    fields = shared()["heartbeat_version_fields"]
    assert isinstance(fields, list)
    assert set(cast("list[str]", fields)) <= set(beat)
    # What was already sent is still sent.
    assert beat["programsEnabled"] is True
    assert isinstance(beat["live"], Mapping)


async def test_ex2_a_link_built_without_a_version_says_it_is_unknown(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await r.step()
    assert r.dashboard.to(HEARTBEAT)[0]["software_version"] == UNKNOWN_SOFTWARE_VERSION


async def test_ex2_the_console_announces_the_version_of_its_version_file(tmp_path: Path) -> None:
    dashboard = Dashboard()
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(
        config_of(), clock=clock, profiles_path=tmp_path / "p.json", transport=lambda _c: dashboard
    )
    clock.advance(Seconds(1.0))
    await panel.cloud_step()
    beat = dashboard.to(HEARTBEAT)[0]
    assert beat["software_version"] == read_software_version()
    assert beat["software_version"] != UNKNOWN_SOFTWARE_VERSION
    await panel.close()


# =========================================================================
# EX-5: a refusal is a stable code and words
# =========================================================================


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                400, json={"error": "session_not_found", "message": "Session not found"}
            ),
            Refused(400, "Session not found", ErrorCode("session_not_found")),
        ),
        (
            httpx.Response(
                426,
                json={
                    "error": "contract_unsupported",
                    "message": "Unsupported machine contract",
                    "supported": ["2", 3, "x", "3"],
                },
            ),
            Refused(426, "Unsupported machine contract", CONTRACT_UNSUPPORTED, ("2", "3")),
        ),
        # A code with no words: the code stands for both.
        (
            httpx.Response(401, json={"error": "unauthorized"}),
            Refused(401, "unauthorized", ErrorCode("unauthorized")),
        ),
        (
            httpx.Response(400, json={"error": "invalid_request", "message": 3}),
            Refused(400, "invalid_request", ErrorCode("invalid_request")),
        ),
        # A dashboard older than the stable codes: its sentence, and no code.
        (
            httpx.Response(400, json={"error": "Session not found"}),
            Refused(400, "Session not found"),
        ),
        (
            httpx.Response(400, json={"error": "Session not found", "message": "Words"}),
            Refused(400, "Words"),
        ),
        (httpx.Response(500, json={"message": "no code"}), Refused(500, "HTTP 500")),
        (httpx.Response(502, text="<html>"), Refused(502, "HTTP 502")),
    ],
)
async def test_ex5_a_refusal_is_read_as_a_stable_code_and_words(
    response: httpx.Response, expected: CloudError
) -> None:
    transport = transport_answering(lambda _request: response)
    assert await transport.get("/a") == Err(expected)


def test_ex5_a_refusal_is_described_by_its_code_first() -> None:
    coded = Refused(400, "Session not found", ErrorCode("session_not_found"))
    assert describe_refusal(coded) == "session_not_found (HTTP 400): Session not found"
    assert describe_refusal(Refused(401, "HTTP 401")) == "sans code (HTTP 401): HTTP 401"


async def test_ex5_the_log_carries_the_code_once_per_distinct_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    r = rig(tmp_path)
    unauthorized = Err(Refused(401, "Invalid API key", ErrorCode("unauthorized")))
    r.dashboard.answer(HEARTBEAT, unauthorized)
    await r.step()
    await r.step(HEARTBEAT_PERIOD)
    line = "dashboard refused a request: unauthorized (HTTP 401): Invalid API key"
    assert caplog.text.count(line) == 1
    assert r.sync.online  # an answer, even a refusal, is a working link
    r.dashboard.answer(HEARTBEAT, Err(Refused(500, "HTTP 500")))
    await r.step(HEARTBEAT_PERIOD)
    assert "dashboard refused a request: sans code (HTTP 500): HTTP 500" in caplog.text


async def test_ex5_a_refused_start_is_logged_with_its_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    r = rig(tmp_path)
    r.dashboard.answer(
        "/api/machine/training/start",
        Err(Refused(400, "Session is not pending", ErrorCode("session_not_pending"))),
    )
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    assert (
        "dashboard refused the start "
        "(session_not_pending (HTTP 400): Session is not pending): stopping"
    ) in caplog.text


# =========================================================================
# EX-4: a launch of another major is never armed
# =========================================================================


async def test_ex4_a_launch_of_this_major_is_armed(tmp_path: Path) -> None:
    """The control: the same launch, from a dashboard of this major, IS submitted."""
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH), version="1.7"))
    await r.step()
    pending = r.panel.surface.pending
    assert isinstance(pending, StartSession)
    assert pending.cloud_session_id == "remote-1"
    assert r.dashboard.to(END) == []
    # START_REQUESTED is the only event: nothing was refused.
    payload = await asyncio.wait_for(watcher.next_payload(), 0.001)
    assert payload.event is not None
    assert payload.event.kind is EventKind.START_REQUESTED


@pytest.mark.parametrize(
    ("version", "sentence"),
    [
        ("2.0", OTHER_MAJOR),
        ("0.9", "serveur incompatible (contrat 1.0 vs 0.9)"),
        ("10.0", "serveur incompatible (contrat 1.0 vs 10.0)"),
        (None, NO_VERSION),
        ("", NO_VERSION),
        ("1", NO_VERSION),
        (1, NO_VERSION),
        (1.0, NO_VERSION),
        (True, NO_VERSION),
        (["1.0"], NO_VERSION),
        ("1.0.0", NO_VERSION),
    ],
)
async def test_ex4_a_launch_of_another_major_is_never_armed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, version: object, sentence: str
) -> None:
    caplog.set_level(logging.ERROR, logger="src.cloud_sync")
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH), version=version))
    await r.step()
    # Nothing reached the mailbox, and the surface is as idle as before.
    assert r.panel.surface.pending is None
    assert r.panel.surface.run_state is RunState.IDLE
    # Said on the console, word for word, and logged.
    assert await refusals_shown(watcher) == [sentence]
    assert f"dashboard: {sentence}" in caplog.text
    # And the dashboard learns why its launch did not start.
    ends = r.dashboard.to(END)
    assert len(ends) == 1
    assert ends[0]["sessionId"] == "remote-1"
    assert ends[0]["failed"] is True
    assert ends[0]["reason"] == f"refusee par la machine : {sentence}"


async def test_ex4_an_answer_that_names_no_version_at_all_arms_nothing(tmp_path: Path) -> None:
    """A dashboard older than the contract: its poll answer has no version field."""
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, ok({"session": dict(LAUNCH)}))
    await r.step()
    assert r.panel.surface.pending is None
    assert await refusals_shown(watcher) == [NO_VERSION]
    assert len(r.dashboard.to(END)) == 1


async def test_ex4_an_incompatible_dashboard_with_nothing_waiting_is_not_said_at_every_poll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="src.cloud_sync")
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer(None, version="2.0"))
    for _ in range(4):
        await r.step(POLL_PERIOD)
    assert len(r.dashboard.to(POLL_PATH)) == 4
    assert await refusals_shown(watcher) == [OTHER_MAJOR]
    assert r.dashboard.to(END) == []  # no launch, so nothing to fail
    # While it lasts it is said again, slowly: a screen opened since must learn of it.
    await r.step(INCOMPATIBLE_REPEAT)
    assert await refusals_shown(watcher) == [OTHER_MAJOR]
    await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == []
    # The log is written when the sentence changes, not at each reminder.
    assert caplog.text.count(f"dashboard: {OTHER_MAJOR}") == 1


async def test_ex4_a_launch_still_waiting_is_refused_and_failed_once(tmp_path: Path) -> None:
    """The dashboard keeps offering the same launch (its end was not taken): no flood."""
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH), version="2.0"))
    for _ in range(3):
        await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == [OTHER_MAJOR]
    assert len(r.dashboard.to(END)) == 1
    # A second launch is a second refusal, said and failed in its turn.
    r.dashboard.answer(POLL_PATH, launch_answer({**LAUNCH, "sessionId": "remote-2"}, version="2.0"))
    await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == [OTHER_MAJOR]
    assert [end["sessionId"] for end in r.dashboard.to(END)] == ["remote-1", "remote-2"]
    assert r.panel.surface.pending is None


async def test_ex4_the_sentence_is_said_again_when_it_changes_or_comes_back(
    tmp_path: Path,
) -> None:
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer(None, version="2.0"))
    await r.step(POLL_PERIOD)
    r.dashboard.answer(POLL_PATH, launch_answer(None, version="3.0"))
    await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == [
        OTHER_MAJOR,
        "serveur incompatible (contrat 1.0 vs 3.0)",
    ]
    # Compatible again: nothing is said, and a launch is armed as usual.
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == []
    # The same incompatibility coming back is news again.
    r.dashboard.answer(POLL_PATH, launch_answer(None, version="3.0"))
    await r.step(POLL_PERIOD)
    assert await refusals_shown(watcher) == ["serveur incompatible (contrat 1.0 vs 3.0)"]


async def test_ex4_a_malformed_launch_of_another_major_is_said_but_fails_nothing(
    tmp_path: Path,
) -> None:
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(POLL_PATH, launch_answer({**LAUNCH, "sessionId": 3}, version="2.0"))
    await r.step()
    assert r.panel.surface.pending is None
    assert await refusals_shown(watcher) == [OTHER_MAJOR]
    assert r.dashboard.to(END) == []


async def test_ex4_the_refusal_does_not_unqueue_a_start_typed_at_the_console(
    tmp_path: Path,
) -> None:
    """News about the dashboard must not touch the operator's own pending start."""
    r, _watcher = attested(tmp_path)
    typed = r.panel.surface.submit_start(
        profile_id=PROFILE, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(typed, Ok)
    counted = r.panel.surface.counters[:2]
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH), version="2.0"))
    await r.step()
    assert r.panel.surface.pending is typed.value
    assert r.panel.surface.run_state is RunState.STARTING
    # Nor is it counted among the commands the console accepted or refused.
    assert r.panel.surface.counters[:2] == counted
    assert len(r.dashboard.to(END)) == 1


# =========================================================================
# EX-3, from the machine: a 426 is read and shown, and changes nothing
# =========================================================================


async def test_ex3_a_refused_contract_is_said_once_on_the_console(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    for path in (HEARTBEAT, POLL_PATH, "/api/machine/profiles"):
        r.dashboard.answer(path, unsupported("2"))
    await r.step()
    await r.step(HEARTBEAT_PERIOD)
    sentence = "serveur incompatible (contrat 1.0 vs 2)"
    assert await refusals_shown(watcher) == [sentence]
    assert caplog.text.count(f"dashboard: {sentence}") == 1
    assert "contract_unsupported (HTTP 426)" in caplog.text
    assert r.sync.online
    assert r.panel.surface.pending is None
    assert r.dashboard.to(END) == []


async def test_ex3_a_426_that_names_no_major_is_still_said(tmp_path: Path) -> None:
    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    r.dashboard.answer(HEARTBEAT, unsupported())
    await r.step()
    assert await refusals_shown(watcher) == [NO_VERSION]


async def test_ex3_a_console_of_contract_2_is_refused_by_a_1_x_server_and_shows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance case, end to end on the machine's side.

    The console is made to speak contract 2.0; the server below applies the
    dashboard's rule for a 1.x deployment (serve major 1, answer 426 otherwise)
    to the header it actually receives.
    """
    monkeypatch.setattr(contract, "CONTRACT_VERSION", ContractVersion("2.0"))
    monkeypatch.setattr(cloud_sync, "CONTRACT_VERSION", ContractVersion("2.0"))
    received: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        received.append(request.headers[CONTRACT_HEADER])
        if request.headers[CONTRACT_HEADER].split(".")[0] != "1":
            return httpx.Response(
                426,
                json={
                    "error": "contract_unsupported",
                    "message": "Unsupported machine contract",
                    "supported": ["1"],
                },
            )
        return httpx.Response(200, json={"session": dict(LAUNCH), SERVER_VERSION_FIELD: "1.0"})

    r, watcher = attested(tmp_path)
    await attestation_seen(watcher)
    transport = transport_answering(server)
    sync = CloudSync(
        clock=r.clock,
        transport=transport,
        runtime=r.runtime,
        surface=r.panel.surface,
        store=r.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
    )
    r.clock.advance(Seconds(1.0))
    await sync.step()
    assert received
    assert set(received) == {"2.0"}
    assert await refusals_shown(watcher) == ["serveur incompatible (contrat 2.0 vs 1)"]
    assert r.panel.surface.pending is None
    assert sync.online
    await transport.close()


def test_the_scripted_poll_answer_is_what_the_server_sends() -> None:
    """The fake dashboard's answer has the shape pinned by the shared contract file."""
    answer = launch_answer(None)
    assert isinstance(answer, Ok)
    assert json.loads(json.dumps(answer.value)) == {
        "session": None,
        shared()["server_version_field"]: CONTRACT_VERSION,
    }


# =========================================================================
# A stop crosses every contract
# =========================================================================

OTHER_VERSIONS: Final[tuple[object, ...]] = ("2.0", "0.9", None, "", 1, ["1.0"], "1.0.0")
"""What a dashboard of another major, or one that states none, may announce."""


def test_the_route_that_carries_the_stop_is_the_one_the_shared_file_exempts() -> None:
    exempt = shared()["contract_exempt_routes"]
    assert isinstance(exempt, dict)
    assert list(cast("dict[str, object]", exempt)) == [f"GET {STATUS}"]


@pytest.mark.parametrize("version", OTHER_VERSIONS)
async def test_a_stop_request_of_another_major_is_forwarded_once(
    tmp_path: Path, version: object
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer(stop=True, version=version))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    await r.step(5.0)
    # Asked once over GET, then never again: the stop was forwarded at once.
    asked = [(method, path) for method, path, _body in r.dashboard.calls if path == STATUS]
    assert asked == [("GET", STATUS)]


async def test_a_stop_request_with_no_version_field_at_all_is_forwarded(tmp_path: Path) -> None:
    """A dashboard older than the contract: its status answer carries no version."""
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, ok({"active": True, "stopRequested": True}))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    assert len(r.dashboard.to(STATUS)) == 1


@pytest.mark.parametrize("version", OTHER_VERSIONS)
@pytest.mark.parametrize("stop", [False, None, "true", 1, "yes"])
async def test_nothing_but_a_stop_request_is_taken_from_a_status_answer_of_another_major(
    tmp_path: Path, version: object, stop: object
) -> None:
    """``active: false`` and everything else in it is ignored; only a real ``true`` stops."""
    r = rig(tmp_path)
    r.dashboard.answer(
        STATUS,
        ok(
            {
                "active": False,
                "status": "completed",
                "stopRequested": stop,
                SERVER_VERSION_FIELD: version,
            }
        ),
    )
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    await r.step(5.0)
    # Still asking: no stop was forwarded.
    assert len(r.dashboard.to(STATUS)) == 3


async def test_the_same_answer_from_this_major_is_believed(tmp_path: Path) -> None:
    """The control: from a dashboard of this major, a session no longer active ends here too."""
    r = rig(tmp_path)
    r.dashboard.answer(STATUS, status_answer(active=False, version="1.7"))
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    await r.step()
    await r.step(5.0)
    assert len(r.dashboard.to(STATUS)) == 1


def stop_reason_of(rig_: Linked) -> str | None:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return rig_.panel.runtime.stop_reason


async def running_remote_session(tmp_path: Path) -> Linked:
    """The real simulated console, turning under a launch from a dashboard of this major."""
    rig_ = linked(tmp_path)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await rig_.run(2.0)
    assert state_of(rig_.panel) is RuntimeState.RUNNING
    return rig_


def refuse_everything_but_the_status(dashboard: Dashboard) -> None:
    """The dashboard becomes one that does not serve this console's major."""
    for path in (
        HEARTBEAT,
        POLL_PATH,
        END,
        "/api/machine/profiles",
        "/api/machine/training/telemetry",
        "/api/machine/training/local",
        "/api/machine/training/start",
    ):
        dashboard.answer(path, unsupported("2"))


@pytest.mark.parametrize("version", ["2.0", None])
async def test_a_stop_from_a_dashboard_of_another_major_stops_a_running_session(
    tmp_path: Path, version: object
) -> None:
    """The whole path on the simulated machine: everything refused, and the stop still lands."""
    rig_ = await running_remote_session(tmp_path)
    refuse_everything_but_the_status(rig_.dashboard)
    rig_.dashboard.answer(STATUS, status_answer(stop=True, version=version))
    await rig_.run(5.0)
    assert state_of(rig_.panel) is RuntimeState.ENDING
    assert stop_reason_of(rig_) == STOP_REASON
    await rig_.panel.close()


async def test_a_dashboard_of_another_major_cannot_end_a_running_session_any_other_way(
    tmp_path: Path,
) -> None:
    rig_ = await running_remote_session(tmp_path)
    refuse_everything_but_the_status(rig_.dashboard)
    rig_.dashboard.answer(STATUS, status_answer(active=False, version="2.0"))
    await rig_.run(10.0)
    # No stop was asked for: the session runs on under the local supervisor.
    assert state_of(rig_.panel) is RuntimeState.RUNNING
    assert stop_reason_of(rig_) is None
    # The moment a stop IS asked for, it is honoured.
    rig_.dashboard.answer(STATUS, status_answer(active=False, stop=True, version="2.0"))
    await rig_.run(5.0)
    assert state_of(rig_.panel) is RuntimeState.ENDING
    assert stop_reason_of(rig_) == STOP_REASON
    await rig_.panel.close()
