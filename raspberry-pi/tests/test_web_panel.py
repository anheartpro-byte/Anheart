"""The routes the local console adds: the motion gate (403) and the link panel.

The rest of the interface is pinned by ``tests/test_web_api.py``; its rig is
reused here with the two console-specific services switched on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest

from src.bitalino_client import LinkStats
from src.contract import UNKNOWN_SOFTWARE_VERSION, SoftwareVersion
from src.ecg_pipeline import EcgBridgeStats, EcgMetrics
from src.geometry import MachineGeometry
from src.link_state import MAX_DETAIL, NOT_CONFIGURED, LinkState, LinkStatus, label_of
from src.local_config import EcgSource, MotorBackend
from src.panel_status import EcgLinkStatus, PanelStatus
from src.result import Err, Ok, Result
from src.training.runtime import IdleLink, RiseHold
from src.training.types import Occupancy, OccupancyRefused, SignalQuality
from src.units import Bpm, BpmPerMinute, GearRatio, Metres, Monotonic, MotorRpm, Seconds
from src.web.app import create_app
from src.web.deps import Services
from src.web.routes import MOTION_DISABLED_DETAIL, PROGRAMS_DISABLED_DETAIL
from src.web.schemas import PanelRow
from tests.test_web_api import (
    OPERATOR,
    PROFILE_ID,
    TOKEN,
    Rig,
    StubPorts,
    auth,
    build_rig,
    parse,
)

NOW = Monotonic(100.0)


def _status(*, link: LinkStats | None, metrics: EcgMetrics | None) -> PanelStatus:
    return PanelStatus(
        at=NOW,
        motion_enabled=False,
        programs_enabled=False,
        motor_backend=MotorBackend.SERIAL,
        drive_link="ftdi://schneider:rs485/1 @ 19200 8E1, esclave 248",
        drive=IdleLink(
            open=True,
            reads=12,
            failures=1,
            consecutive_failures=0,
            last_latency=Seconds(0.034),
            last_error="the drive did not answer within 0.100 s",
        ),
        ecg=EcgLinkStatus(
            source=EcgSource.RFCOMM,
            address="rfcomm:98-d3-91-fe-4e-9f",
            connected=True,
            acquiring=True,
            connect_attempts=1,
            last_error=None,
            link=link,
            bridge=EcgBridgeStats(
                batches=5,
                samples=1000,
                missing_channel=0,
                last_batch_at=Monotonic(99.5),
                last_metrics=metrics,
            ),
        ),
        heart_rate_trend=BpmPerMinute(3.5),
        manual_rise_hold=RiseHold.HEART_RATE_FALLING,
        radius=Metres(1.5),
        ratio=GearRatio(49.79),
        motor_max_rpm=MotorRpm(300),
        software_version=SoftwareVersion("pi-1.4.2"),
        dashboard=LinkStatus(LinkState.REACHABLE, "", Seconds(4.5)),
    )


class FixedPanel:
    """A panel source with a scripted answer."""

    def __init__(self, status: PanelStatus) -> None:
        self.status: PanelStatus = status

    def panel_status(self) -> PanelStatus:
        return self.status


def _console_app(rig: Rig, status: PanelStatus) -> httpx.AsyncClient:
    services = Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=MachineGeometry(radius=Metres(1.5)),
        motion_enabled=False,
        panel=FixedPanel(status),
    )
    app = create_app(services=services, config=rig.config)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return build_rig(tmp_path)


@pytest.fixture
async def console(rig: Rig) -> AsyncIterator[httpx.AsyncClient]:
    status = _status(
        link=LinkStats(frames=900, sync_losses=2, reconnects=1),
        metrics=EcgMetrics(seq=4, quality=SignalQuality.GOOD, bpm=Bpm(72)),
    )
    async with _console_app(rig, status) as session:
        yield session


async def test_a_start_is_forbidden_before_anything_else_is_checked(
    console: httpx.AsyncClient, rig: Rig
) -> None:
    """403 even for an attested operator, a valid profile and an idle surface."""
    rig.attest()
    response = await console.post(
        "/api/session/start",
        headers=auth(),
        json={"profile_id": PROFILE_ID, "operator": OPERATOR},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert response.json() == {"detail": MOTION_DISABLED_DETAIL}
    assert rig.surface.pending is None
    # And a malformed one is refused for the same reason, not validated first.
    blank = await console.post(
        "/api/session/start", headers=auth(), json={"profile_id": "?", "operator": ""}
    )
    assert blank.status_code == HTTPStatus.FORBIDDEN


async def test_stopping_stays_available_on_a_read_only_console(
    console: httpx.AsyncClient, rig: Rig
) -> None:
    estop = await console.post(
        "/api/session/estop", headers=auth(), json={"operator": OPERATOR, "reason": "test"}
    )
    assert estop.status_code == HTTPStatus.OK
    assert rig.surface.estop_latched
    stop = await console.post("/api/session/stop", headers=auth(), json={"operator": OPERATOR})
    # Refused because nothing runs, never because motion is disabled.
    assert stop.status_code != HTTPStatus.FORBIDDEN


async def test_the_panel_reports_both_links_and_the_geometry(console: httpx.AsyncClient) -> None:
    row = parse(PanelRow, await console.get("/api/panel", headers=auth()))
    assert not row.motion_enabled
    assert not row.programs_enabled
    assert row.motor_backend == "serial"
    assert row.drive.reads == 12
    assert row.drive.latency_ms == pytest.approx(34.0)
    assert row.drive.description is not None
    assert row.ecg.source == "rfcomm"
    assert row.ecg.link is not None
    assert row.ecg.link.frames == 900
    assert row.ecg.link.sync_losses == 2
    assert row.ecg.last_batch_age_s == pytest.approx(0.5)
    assert row.ecg.dsp_seq == 4
    assert row.ecg.dsp_quality == "good"
    assert row.heart_rate_trend_bpm_per_min == pytest.approx(3.5)
    assert row.manual_rise_hold == "heart_rate_falling"
    assert row.radius_m == pytest.approx(1.5)
    assert row.gear_ratio == pytest.approx(49.79)
    assert row.motor_max_rpm == 300
    assert row.software_version == "pi-1.4.2"
    assert (row.dashboard.state, row.dashboard.label, row.dashboard.detail) == (
        "reachable",
        "joignable",
        "",
    )
    assert row.dashboard.last_answer_age_s == pytest.approx(4.5)


EVERY_STATE: dict[LinkState, tuple[str, str]] = {
    LinkState.NOT_CONFIGURED: ("not_configured", "non configure"),
    LinkState.WAITING: ("waiting", "en attente"),
    LinkState.REACHABLE: ("reachable", "joignable"),
    LinkState.UNREACHABLE: ("unreachable", "injoignable"),
    LinkState.INCOMPATIBLE: ("incompatible", "incompatible"),
    LinkState.KEY_REFUSED: ("key_refused", "cle refusee"),
    LinkState.SERVER_ERROR: ("server_error", "en erreur"),
}
"""Every state of the link, its wire value and the word the operator reads. The page's
own table (``tests/web/panel_dashboard_link.test.mjs``) and the docs quote these."""


def test_every_state_of_the_link_has_its_word() -> None:
    assert set(EVERY_STATE) == set(LinkState)
    spoken = {state: (state.value, label_of(state)) for state in LinkState}
    assert spoken == EVERY_STATE
    words = [label for _wire, label in EVERY_STATE.values()]
    assert len(set(words)) == len(words), "two states must never read the same"


@pytest.mark.parametrize("state", list(LinkState))
async def test_ex2_the_panel_serves_the_state_of_the_dashboard_link_and_the_version(
    rig: Rig, state: LinkState
) -> None:
    """EX-2: one standing state, with its word, on the route the page already polls."""
    status = replace(
        _status(link=None, metrics=None),
        dashboard=LinkStatus(state, "serveur incompatible (contrat 1.1 vs 2)", Seconds(12.0)),
    )
    async with _console_app(rig, status) as session:
        answered = await session.get("/api/panel", headers=auth())
    row = parse(PanelRow, answered)
    wire, label = EVERY_STATE[state]
    assert (row.dashboard.state, row.dashboard.label) == (wire, label)
    assert row.dashboard.detail == "serveur incompatible (contrat 1.1 vs 2)"
    assert row.dashboard.last_answer_age_s == pytest.approx(12.0)
    assert row.software_version == "pi-1.4.2"


async def test_ex2_a_console_without_a_key_and_without_a_version_file_says_so(rig: Rig) -> None:
    status = replace(
        _status(link=None, metrics=None),
        software_version=UNKNOWN_SOFTWARE_VERSION,
        dashboard=NOT_CONFIGURED,
    )
    async with _console_app(rig, status) as session:
        row = parse(PanelRow, await session.get("/api/panel", headers=auth()))
    assert row.software_version == "pi-unknown"
    assert (row.dashboard.state, row.dashboard.label) == ("not_configured", "non configure")
    assert "MACHINE_API_KEY" in row.dashboard.detail
    assert row.dashboard.last_answer_age_s is None
    assert len(row.dashboard.detail) <= MAX_DETAIL


async def test_the_state_of_the_link_is_not_served_without_the_token(rig: Rig) -> None:
    """It names the dashboard's refusals: nothing of it for a caller with no token."""
    async with _console_app(rig, _status(link=None, metrics=None)) as session:
        refused = await session.get("/api/panel")
    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    assert "joignable" not in refused.text
    assert "pi-1.4.2" not in refused.text


async def test_the_panel_renders_a_simulator_and_a_link_never_measured(rig: Rig) -> None:
    status = _status(link=None, metrics=None)
    status = replace(
        status,
        drive=IdleLink(),
        ecg=replace(status.ecg, bridge=EcgBridgeStats()),
        heart_rate_trend=None,
        manual_rise_hold=None,
    )
    async with _console_app(rig, status) as session:
        row = parse(PanelRow, await session.get("/api/panel", headers=auth()))
    assert row.ecg.link is None
    assert row.ecg.dsp_seq is None
    assert row.ecg.dsp_quality is None
    assert row.ecg.last_batch_age_s is None
    assert row.drive.latency_ms is None
    assert row.heart_rate_trend_bpm_per_min is None
    assert row.manual_rise_hold is None, "no hold must travel as null, never as a word"


async def test_the_panel_is_null_without_a_console_and_needs_the_token(rig: Rig) -> None:
    transport = httpx.ASGITransport(app=rig.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as session:
        assert (await session.get("/api/panel", headers=auth())).json() is None
        refused = await session.get("/api/panel")
    assert refused.status_code == HTTPStatus.UNAUTHORIZED
    assert TOKEN not in refused.text


# =========================================================================
# Milestone M3: the manual routes
# =========================================================================


def _ceiling(occupancy: Occupancy) -> Result[MotorRpm, OccupancyRefused]:
    if occupancy is Occupancy.BENCH:
        return Ok(MotorRpm(300))
    return Err(OccupancyRefused(occupancy, "personne a bord refusee"))


def _manual_app(
    rig: Rig, *, ceilings: Callable[[Occupancy], Result[MotorRpm, OccupancyRefused]] | None
) -> httpx.AsyncClient:
    services = Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=MachineGeometry(radius=Metres(1.5)),
        motion_enabled=True,
        programs_enabled=False,
        ceilings=ceilings,
    )
    app = create_app(services=services, config=rig.config)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_a_manual_session_is_started_targeted_and_its_fault_reset_submitted(
    rig: Rig,
) -> None:
    rig.attest()
    async with _manual_app(rig, ceilings=_ceiling) as session:
        started = await session.post(
            "/api/manual/start", headers=auth(), json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == HTTPStatus.ACCEPTED
        assert started.json()["kind"] == "manual_start"
        assert started.json()["detail"] == "bench"
        # Busy: the start is still in the mailbox.
        again = await session.post(
            "/api/manual/start", headers=auth(), json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert again.status_code == HTTPStatus.CONFLICT
        # No session is running yet, so a target has nothing to act on.
        early = await session.post(
            "/api/manual/target", headers=auth(), json={"output_rpm": 2.0, "operator": OPERATOR}
        )
        assert early.status_code == HTTPStatus.CONFLICT
        assert "no manual session" in early.json()["detail"]
        # The loop takes the start and reports the session running.
        rig.surface.take_command()
        rig.surface.note_running()
        target = await session.post(
            "/api/manual/target", headers=auth(), json={"output_rpm": 2.0, "operator": OPERATOR}
        )
        assert target.status_code == HTTPStatus.ACCEPTED
        assert target.json()["kind"] == "manual_target"
        assert target.json()["detail"] == "2.00 output rpm"
        busy = await session.post(
            "/api/manual/target", headers=auth(), json={"output_rpm": 3.0, "operator": OPERATOR}
        )
        assert busy.status_code == HTTPStatus.CONFLICT
        assert "try again" in busy.json()["detail"]
        busy_reset = await session.post(
            "/api/drive/fault-reset", headers=auth(), json={"operator": OPERATOR}
        )
        assert busy_reset.status_code == HTTPStatus.CONFLICT
        rig.surface.take_command()
        reset = await session.post(
            "/api/drive/fault-reset", headers=auth(), json={"operator": OPERATOR}
        )
        assert reset.status_code == HTTPStatus.ACCEPTED
        assert reset.json()["kind"] == "fault_reset"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"output_rpm": -1.0, "operator": OPERATOR}, HTTPStatus.UNPROCESSABLE_ENTITY),
        ({"output_rpm": 2.0, "operator": " "}, HTTPStatus.BAD_REQUEST),
    ],
)
async def test_a_target_that_is_not_a_speed_is_refused_at_the_route(
    rig: Rig, body: dict[str, object], status: HTTPStatus
) -> None:
    async with _manual_app(rig, ceilings=_ceiling) as session:
        response = await session.post("/api/manual/target", headers=auth(), json=body)
    assert response.status_code == status
    assert rig.surface.pending is None


async def test_occupancy_is_parsed_and_gated_before_anything_is_submitted(rig: Rig) -> None:
    rig.attest()
    async with _manual_app(rig, ceilings=_ceiling) as session:
        unknown = await session.post(
            "/api/manual/start", headers=auth(), json={"occupancy": "crew", "operator": OPERATOR}
        )
        assert unknown.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        assert "bench, occupied" in unknown.json()["detail"]
        occupied = await session.post(
            "/api/manual/start",
            headers=auth(),
            json={"occupancy": "occupied", "operator": OPERATOR},
        )
        assert occupied.status_code == HTTPStatus.FORBIDDEN
        assert occupied.json()["detail"] == "personne a bord refusee"
    async with _manual_app(rig, ceilings=None) as session:
        none = await session.post(
            "/api/manual/start", headers=auth(), json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert none.status_code == HTTPStatus.FORBIDDEN
        programme = await session.post(
            "/api/session/start",
            headers=auth(),
            json={"profile_id": PROFILE_ID, "operator": OPERATOR},
        )
        assert programme.status_code == HTTPStatus.FORBIDDEN
        assert programme.json() == {"detail": PROGRAMS_DISABLED_DETAIL}
    assert rig.surface.pending is None


async def test_the_manual_routes_are_forbidden_on_a_read_only_console(
    console: httpx.AsyncClient,
) -> None:
    for path, body in (
        ("/api/manual/start", {"occupancy": "bench", "operator": OPERATOR}),
        ("/api/manual/target", {"output_rpm": 1.0, "operator": OPERATOR}),
        ("/api/drive/fault-reset", {"operator": OPERATOR}),
    ):
        response = await console.post(path, headers=auth(), json=body)
        assert response.status_code == HTTPStatus.FORBIDDEN, path


async def test_a_manual_start_refused_by_the_surface_maps_to_its_status(rig: Rig) -> None:
    """Not attested: 412, like a programmed start."""
    async with _manual_app(rig, ceilings=_ceiling) as session:
        response = await session.post(
            "/api/manual/start", headers=auth(), json={"occupancy": "bench", "operator": OPERATOR}
        )
    assert response.status_code == HTTPStatus.PRECONDITION_FAILED
