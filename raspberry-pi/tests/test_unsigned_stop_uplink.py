"""ANH-152: a STOP and an E-STOP with nobody signed in reach the dashboard's timeline.

Neither waits for a sign-in: pressed with no operator signed in, each is
recorded in the name of the device it came from, ``device:<id>`` (contract
1.13). On the REAL console in simulation, with its real record and its real
link, both must arrive at ``POST /api/machine/training/events`` under that
actor, as the dashboard takes it.
"""

from __future__ import annotations

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
