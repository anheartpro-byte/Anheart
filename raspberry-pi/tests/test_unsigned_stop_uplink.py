"""ANH-152: a STOP and an E-STOP with nobody signed in reach the dashboard's timeline.

Neither waits for a sign-in: pressed with no operator signed in, each is
recorded in the name of the device it came from, ``device:<id>`` (contract
1.13). On the REAL console in simulation, with its real record and its real
link, both must arrive at ``POST /api/machine/training/events`` under that
actor, as the dashboard takes it.
"""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from pathlib import Path

import httpx

from src.console_access import LOCAL_DEVICE, device_actor
from src.record import commands
from src.record.journal import Journal
from src.result import Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.web.app import create_app
from src.web.routes import STOP_BODY_LIMIT
from tests.fake_dashboard import FakeDashboard
from tests.test_cloud_journal_e2e import LINKED_ENV, one_second
from tests.test_failure_rig import OPERATOR, JumpClock, make_rig


async def test_a_stop_and_an_e_stop_with_nobody_signed_in_are_on_the_dashboard(
    tmp_path: Path,
) -> None:
    clock = JumpClock()
    dashboard = FakeDashboard(now=lambda: int(clock.unix_millis()))
    token = secrets.token_hex(24)
    journal = Journal(tmp_path / "records", clock)
    (tmp_path / "console").mkdir()
    rig, _ = make_rig(
        tmp_path / "console",
        env={**LINKED_ENV, "DEVICE_TOKEN": token},
        clock=clock,
        transport=lambda _config: dashboard,
        journal=journal,
    )
    surface = rig.panel.surface
    app = create_app(services=rig.panel.services, config=rig.config.web)
    nobody = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"x-anheart-token": token},
    )
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    async with nobody:
        for route in ("/api/session/stop", "/api/session/estop"):
            started = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
            assert isinstance(started, Ok)
            for _ in range(5):
                await one_second(rig, journal)
            assert rig.panel.runtime.state is RuntimeState.RUNNING
            pressed = await nobody.post(route, json={})
            assert pressed.status_code in {HTTPStatus.OK, HTTPStatus.ACCEPTED}, pressed.text
            for _ in range(20):
                await one_second(rig, journal)
            assert rig.panel.runtime.state is not RuntimeState.RUNNING
    await rig.panel.close()

    # The presses themselves, and what the console said of them, in the device's name.
    console = device_actor(LOCAL_DEVICE)
    attributed = {
        str(stored.item["detail"])
        for stored in dashboard.events.values()
        if stored.item["actor"] == console
    }
    pressed_here = {commands.encode(commands.Stop()), commands.encode(commands.EmergencyStop())}
    assert pressed_here <= attributed
    assert dashboard.rejected == 0


async def test_an_e_stop_with_the_longest_emoji_reason_still_reaches_the_dashboard(
    tmp_path: Path,
) -> None:
    """The reason goes into the events' details: cut as Convex counts, in UTF-16 units,
    the batch that carries the E-STOP is taken (the dashboard double checks as Convex)."""
    clock = JumpClock()
    dashboard = FakeDashboard(now=lambda: int(clock.unix_millis()))
    journal = Journal(tmp_path / "records", clock)
    (tmp_path / "console").mkdir()
    rig, _ = make_rig(
        tmp_path / "console",
        env=LINKED_ENV,
        clock=clock,
        transport=lambda _config: dashboard,
        journal=journal,
    )
    surface = rig.panel.surface
    app = create_app(services=rig.panel.services, config=rig.config.web)
    reason = "\U0001f6a8" * (STOP_BODY_LIMIT // 4 - 4)
    body = json.dumps({"reason": reason}, ensure_ascii=False).encode()
    assert len(body) <= STOP_BODY_LIMIT
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as page:
        for _ in range(5):
            await one_second(rig, journal)
        pressed = await page.post(
            "/api/session/estop", content=body, headers={"content-type": "application/json"}
        )
        assert pressed.json()["reason"] == reason
        for _ in range(20):
            await one_second(rig, journal)
    await rig.panel.close()
    details = [str(stored.item["detail"]) for stored in dashboard.events.values()]
    assert commands.encode(commands.EmergencyStop()) in details
    assert any(reason[:100] in detail for detail in details)
    assert dashboard.rejected == 0
