"""ANH-141: between a configuration written and the restart that runs it, nothing starts.

From the moment the configuration is written (a safety revision confirmed at the
console, or an exploitation revision taken at rest), every start is refused until
the process ends for its restart: manual, bench, programme, and a launch from the
dashboard, which is told so as a failed session. The runtime's own refusal is
:class:`~src.training.runtime.RestartPending` (``tests/test_runtime.py``).
"""

from __future__ import annotations

import base64
from http import HTTPStatus
from pathlib import Path
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src import local_panel
from src.clock import ManualClock
from src.cloud_sync import AutoLaunch, BenchLaunch
from src.config_sync import CONFIG_PATH, MACHINE_UNASSIGNED
from src.control_surface import StartManual
from src.local_panel import RESTART_PENDING, LocalPanel, build_panel
from src.result import Err, Ok
from src.training.runtime import RestartPending, RuntimeState
from src.training.types import Ballast, Occupancy
from src.units import Bpm
from tests.roster_support import RIDER_ID
from tests.test_cloud_sync import OPERATOR as DASHBOARD_CONFIRMER
from tests.test_cloud_sync import POLL_PATH, Dashboard, launch_answer, linked, ok
from tests.test_config_sync import BASE, ENV, MACHINE, payload
from tests.test_failure_rig import OPERATOR, SHORT_PROFILE, attest
from tests.test_local_panel import config_from
from tests.test_out_of_service_console import console, refusals, state_of
from tests.test_remote_bench_launch import BENCH, END
from tests.test_web_api import shown_digest

REFUSED: Final[str] = f"demarrage refuse : {RESTART_PENDING}"
LINKED: Final[dict[str, str]] = {**ENV, "MACHINE_API_KEY": "k", "CONVEX_URL": "http://localhost"}


def held(panel: LocalPanel) -> RestartPending | None:
    """The runtime's hold, read through a call: a checker keeps a property's narrowing
    across an ``await``."""
    return panel.runtime.restart_hold


async def test_a_manual_bench_start_is_refused_while_a_restart_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    rig.panel.runtime.hold_for_restart(True)
    async with rig.http() as session:
        await attest(session)
        asked = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert asked.status_code == HTTPStatus.ACCEPTED
        await rig.tick(2.0)
    assert refusals(rig) == [REFUSED]
    assert state_of(rig) is RuntimeState.IDLE
    assert rig.simulator.commanded_setpoint == 0


async def test_a_programme_is_refused_while_a_restart_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, programmes=True, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    rig.panel.runtime.hold_for_restart(True)
    async with rig.http() as session:
        await attest(session)
        asked = await session.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_id": RIDER_ID,
                "profiles_digest": await shown_digest(session),
            },
        )
        assert asked.status_code == HTTPStatus.ACCEPTED
        await rig.tick(2.0)
    assert refusals(rig) == [REFUSED]
    assert state_of(rig) is RuntimeState.IDLE


async def test_a_launch_from_the_dashboard_is_refused_before_it_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, programmes=True, monkeypatch=monkeypatch)
    rig.panel.runtime.hold_for_restart(True)
    bench = BenchLaunch(
        session_id="remote-bench", operator="Dr Manager", ballast=Ballast(None), notes=None
    )
    auto = AutoLaunch(
        session_id="remote-auto",
        profile_id=SHORT_PROFILE,
        total_duration_s=None,
        subject_id=RIDER_ID,
        subject_hr_max=Bpm(180),
        operator="Dr Manager",
        subject_age=30,
        subject_label=None,
        profiles_digest=None,
    )
    assert rig.panel.preflight_remote_start(bench) == Err(REFUSED)
    assert rig.panel.preflight_remote_start(auto) == Err(REFUSED)
    await rig.panel.close()


async def test_a_launch_confirmed_before_the_write_ends_failed_with_the_reason(
    tmp_path: Path,
) -> None:
    """Confirmed at the console, its start sent; the configuration is written before it
    arms: the arming is refused, and the dashboard holds the session failed, saying why."""
    rig_ = linked(tmp_path)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(0.2)
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", DASHBOARD_CONFIRMER, True), Ok)
    rig_.panel.runtime.hold_for_restart(True)
    await rig_.run(2.0)
    assert rig_.panel.runtime.manual is None
    (end,) = rig_.dashboard.to(END)
    assert end["sessionId"] == "remote-bench"
    assert end["failed"] is True
    assert RESTART_PENDING in str(end["reason"])
    await rig_.panel.close()


async def test_the_console_holds_its_starts_from_the_configuration_it_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired through: the link that writes a configuration holds the runtime itself."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public)
    config = config_from({**LINKED, "RECORD_MACHINE_ID": MACHINE})
    text = payload(BASE)  # its safety keys are this .env's: taken at once, at rest
    signature = base64.b64encode(key.sign(text.encode("utf-8"))).decode("ascii")
    dashboard = Dashboard()
    dashboard.answer(CONFIG_PATH, ok({"status": "signed", "payload": text, "signature": signature}))
    panel = build_panel(
        config,
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
    )
    assert held(panel) is None
    await panel.config_step()
    assert held(panel) == RestartPending()
    assert (tmp_path / "config.applied.json").exists()
    await panel.close()


async def test_r2_an_unnamed_machine_asks_for_no_configuration_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R2: linked, ``RECORD_MACHINE_ID`` not set: another machine's signed configuration is
    never asked for, never written, nothing held; said once, and on the Securite card."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    monkeypatch.setattr(local_panel, "read_public_key", lambda: public)
    text = payload(BASE, level="occupied", machine="machine-OTHER")
    signature = base64.b64encode(key.sign(text.encode("utf-8"))).decode("ascii")
    dashboard = Dashboard()
    dashboard.answer(CONFIG_PATH, ok({"status": "signed", "payload": text, "signature": signature}))
    panel = build_panel(
        config_from(LINKED),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
    )
    await panel.config_step()
    assert dashboard.to(CONFIG_PATH) == []
    assert held(panel) is None
    assert not (tmp_path / "config.applied.json").exists()
    assert panel.services.config_view().refusal == MACHINE_UNASSIGNED
    assert caplog.text.count(MACHINE_UNASSIGNED) == 1
    await panel.close()


async def test_r2_a_start_being_armed_is_not_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: from the mailbox to its arming, a start keeps the console away from rest, so
    no configuration is written under it; refused or armed, rest is judged as before."""
    rig = console(tmp_path, monkeypatch=monkeypatch)
    seen: list[bool] = []

    async def arming(panel: LocalPanel, _command: StartManual) -> None:
        seen.append(panel.catalog_at_rest())

    monkeypatch.setattr(LocalPanel, "_start_manual", arming)
    surface = rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    assert not rig.panel.catalog_at_rest(), "waiting in the mailbox"
    await rig.panel.control_cycle()
    assert seen == [False], "being armed"
    assert rig.panel.catalog_at_rest()
    await rig.panel.close()
