"""ANH-162 EX-2: a console out of service, at its page and at its routes.

The REAL console (:func:`src.local_panel.build_panel`) on a manual clock, with
the restart state it would find after three deaths in a row. What is pinned:

* ``GET /api/panel`` carries the state: in service or not, how many unclean
  starts, the open record, whether the state is kept, and what the control
  loop has told the service manager;
* out of service, no start is armed: not a manual one, not a programme, and
  each is answered with the reason and the way out;
* ``POST /api/service/restore`` lifts it, by name and only by name, takes the
  token like every other route, and holds even when the disk refuses the new
  state; it starts nothing.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import pytest

from src import local_panel
from src.clock import ManualClock
from src.control_surface import EventKind
from src.local_panel import OUT_OF_SERVICE, DriveSide, build_panel, open_service
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record import logbook
from src.record.journal import Journal
from src.result import Ok
from src.service_state import STATE_NAME, THRESHOLD, WINDOW, Stored, load, store
from src.training.runtime import RuntimeState
from src.units import Monotonic, UnixMillis
from src.web.app import create_app
from src.web.deps import WebConfig
from src.web.schemas import PanelRow, ServiceRow, StatusRow
from tests.test_failure_rig import (
    BENCH_ENV,
    ENERGISED,
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    SHORT_STORE,
    Rig,
    attest,
    config_of,
    no_dsp,
    precommanded,
)
from tests.test_web_api import TOKEN, auth, detail, parse

BOOT: Final[str] = "2f1c7a0e-5b0d-4c58-9d39-000000000001"


def _nothing() -> None:
    """The simulator has no port to release."""


def console(
    tmp_path: Path,
    *,
    deaths: int = 0,
    programmes: bool = False,
    recorded: bool = True,
    monkeypatch: pytest.MonkeyPatch,
    clock: ManualClock | None = None,
    simulator: SimulatedDrive | None = None,
) -> Rig:
    """The console as it starts after ``deaths`` unclean stops in a row.

    The state the dead processes left is written as they would have: every
    one of them started (``running``) and none said it stopped.
    ``simulator``: the drive as those processes left it, on ``clock``.
    """
    monkeypatch.setattr(local_panel, "boot_identity", lambda: BOOT)
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_700_000_000_000)) if clock is None else clock
    records = tmp_path / "records"
    journal = Journal(records, clock) if recorded else None
    if deaths:
        earlier = tuple(100.0 - 10.0 * age for age in range(deaths - 1, 0, -1))
        left = Stored(boot_id=BOOT, unclean_starts=earlier, running=True)
        assert store(records / STATE_NAME, left) == Ok(None)
    sim = (
        SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
        if simulator is None
        else simulator
    )
    defaults = tmp_path / "defaults.json"
    defaults.write_text(SHORT_STORE, encoding="utf-8")
    config = config_of(PROGRAMME_ENV if programmes else BENCH_ENV)
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        defaults_path=defaults,
        treat=no_dsp,
        drive=DriveSide(backend=sim, simulator=sim, release=_nothing),
        journal=journal,
        service=None if journal is None else open_service(journal, clock),
    )
    return Rig(
        clock=clock, panel=panel, simulator=sim, watcher=panel.hub.subscribe(), config=config
    )


def state_of(rig: Rig) -> RuntimeState:
    """The runtime's state, read through a call: a checker keeps a property's narrowing
    across an ``await``."""
    return rig.panel.runtime.state


def refusals(rig: Rig) -> list[str]:
    return [event.detail for event in rig.events if event.kind is EventKind.REFUSED]


async def restore(session: httpx.AsyncClient, operator: str = OPERATOR) -> httpx.Response:
    return await session.post("/api/service/restore", json={"operator": operator})


async def start_written(rig: Rig) -> None:
    """What the console does once its first tick has read the drive: write this start."""
    service = rig.panel.service
    assert service is not None
    await service.record_start()


# =========================================================================
# What the page is told
# =========================================================================


async def test_ex2_the_panel_says_a_console_is_in_service_and_what_it_told_the_service_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    async with rig.http() as session:
        panel = parse(PanelRow, await session.get("/api/panel"))
    assert panel.service == ServiceRow(
        out_of_service=False,
        unclean_starts=0,
        threshold=THRESHOLD,
        window_s=float(WINDOW),
        unclean_stop=False,
        open_record=None,
        persisted=True,
    )
    # No service manager started this console: nothing was sent, and nothing failed.
    assert not panel.watchdog.enabled
    assert (panel.watchdog.sent, panel.watchdog.failed, panel.watchdog.last_error) == (0, 0, None)


async def test_ex2_the_panel_says_a_console_is_out_of_service_after_three_deaths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    async with rig.http() as session:
        panel = parse(PanelRow, await session.get("/api/panel"))
    assert panel.service.out_of_service
    assert panel.service.unclean_starts == THRESHOLD
    assert panel.service.unclean_stop
    assert panel.service.persisted
    # One death fewer is not a loop.
    almost = console(tmp_path / "other", deaths=THRESHOLD - 1, monkeypatch=monkeypatch)
    async with almost.http() as session:
        panel = parse(PanelRow, await session.get("/api/panel"))
    assert not panel.service.out_of_service
    assert panel.service.unclean_starts == THRESHOLD - 1


# =========================================================================
# Out of service: nothing is armed
# =========================================================================


async def test_ex2_out_of_service_a_manual_start_is_refused_with_the_reason_and_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    async with rig.http() as session:
        await attest(session)
        asked = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert asked.status_code == HTTPStatus.ACCEPTED
        await rig.tick(2.0)
    assert refusals(rig) == [f"demarrage refuse : {OUT_OF_SERVICE}"]
    assert "remettre en service" in OUT_OF_SERVICE
    assert "operateur nomme" in OUT_OF_SERVICE
    assert state_of(rig) is RuntimeState.IDLE
    assert rig.simulator.sim_state not in ENERGISED
    assert rig.simulator.commanded_setpoint == 0


async def test_ex2_out_of_service_a_programme_is_refused_before_any_other_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, programmes=True, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    async with rig.http() as session:
        await attest(session)
        asked = await session.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_age": 30,
            },
        )
        assert asked.status_code == HTTPStatus.ACCEPTED
        await rig.tick(2.0)
    assert refusals(rig) == [f"demarrage refuse : {OUT_OF_SERVICE}"]
    assert state_of(rig) is RuntimeState.IDLE
    assert rig.simulator.sim_state not in ENERGISED


async def test_ex2_out_of_service_the_console_still_reads_the_drive_and_stops_one_found_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of service takes nothing away from what a start does to a drive left running."""
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    assert rig.panel.runtime.idle_link.reads >= 1
    assert rig.panel.runtime.snapshot().safety is None
    assert rig.panel.watchdog.stalled(rig.clock.monotonic()) is False


# =========================================================================
# Putting it back in service
# =========================================================================


async def test_ex2_a_named_operator_puts_the_console_back_in_service_and_nothing_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    await rig.tick(1.0)
    async with rig.http() as session:
        answered = await restore(session)
        row = parse(ServiceRow, answered)
        assert not row.out_of_service
        assert row.unclean_starts == 0
        assert row.persisted
        assert not parse(PanelRow, await session.get("/api/panel")).service.out_of_service
        await rig.tick(2.0)
        # Lifting it starts nothing: no session, no frame.
        assert state_of(rig) is RuntimeState.IDLE
        assert rig.simulator.sim_state not in ENERGISED
        # And a start is a start again: attested, asked for, armed.
        await attest(session)
        asked = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert asked.status_code == HTTPStatus.ACCEPTED
        await rig.tick(2.0)
        assert state_of(rig) is RuntimeState.RUNNING
        assert refusals(rig) == []
        # Once lifted there is nothing left to lift.
        again = await restore(session)
        assert again.status_code == HTTPStatus.CONFLICT
        assert "nothing to lift" in detail(again)
    kept = load(tmp_path / "records" / STATE_NAME)
    assert isinstance(kept, Stored)
    assert not kept.out_of_service
    assert kept.unclean_starts == ()
    await rig.panel.close()


async def test_ex2_restoring_takes_a_name_and_refuses_a_console_in_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    async with rig.http() as session:
        nameless = await restore(session, operator="   ")
        assert nameless.status_code == HTTPStatus.BAD_REQUEST
        assert "operator name is required" in detail(nameless)
        assert parse(PanelRow, await session.get("/api/panel")).service.out_of_service
    in_service = console(tmp_path / "in-service", monkeypatch=monkeypatch)
    async with in_service.http() as session:
        refused = await restore(session)
        assert refused.status_code == HTTPStatus.CONFLICT
        assert "nothing to lift" in detail(refused)
    stateless = console(tmp_path / "stateless", recorded=False, monkeypatch=monkeypatch)
    async with stateless.http() as session:
        refused = await restore(session)
        assert refused.status_code == HTTPStatus.CONFLICT
        assert "keeps no restart state" in detail(refused)
        assert not parse(PanelRow, await session.get("/api/panel")).service.out_of_service


async def test_ex2_restoring_takes_the_token_like_every_other_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    app = create_app(
        services=rig.panel.services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as session:
        anonymous = await restore(session)
        assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
        service = rig.panel.service
        assert service is not None
        assert service.status().out_of_service, "a caller without the token lifted nothing"
        known = await session.post(
            "/api/service/restore", json={"operator": OPERATOR}, headers=auth()
        )
        assert not parse(ServiceRow, known).out_of_service


async def test_ex2_restoring_holds_when_the_disk_refuses_the_new_state_and_the_page_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism never holds the machine: in service now, out again after a restart."""
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    await start_written(rig)

    def refused(_path: Path, _stored: Stored) -> Ok[None]:
        raise OSError(30, "read-only file system")

    monkeypatch.setattr(local_panel, "store", refused)
    async with rig.http() as session:
        row = parse(ServiceRow, await restore(session))
    assert not row.out_of_service, "in effect at once, whatever the disk does"
    assert not row.persisted, "and the page is told it is not kept"
    service = rig.panel.service
    assert service is not None
    assert service.refusal() is None
    on_disk = load(tmp_path / "records" / STATE_NAME)
    assert isinstance(on_disk, Stored)
    assert on_disk.out_of_service, "the next start is out of service again, and lifted the same way"


async def test_ex2_restoring_lifts_neither_a_latched_verdict_nor_an_emergency_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of service, AND a drive found running, AND an emergency stop: putting the
    console back in service is about the restarts and about nothing else. Every latch
    is still there afterwards, to be acknowledged by name like any other day."""
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_700_000_000_000))
    left_running = await precommanded(clock, 300)
    rig = console(
        tmp_path,
        deaths=THRESHOLD,
        monkeypatch=monkeypatch,
        clock=clock,
        simulator=left_running,
    )
    await rig.tick(1.0)
    found = rig.panel.runtime.snapshot().safety
    assert found is not None
    assert found.rule == "drive_precommanded"
    assert found.latched
    async with rig.http() as session:
        pressed = await session.post(
            "/api/session/estop", json={"operator": OPERATOR, "reason": "restore test"}
        )
        assert pressed.status_code < HTTPStatus.BAD_REQUEST, pressed.text
        await rig.tick(1.0)
        before = parse(StatusRow, await session.get("/api/status"))
        assert before.estop_latched
        assert before.supervisor_estop is not None

        row = parse(ServiceRow, await restore(session))
        assert not row.out_of_service
        await rig.tick(1.0)

        after = parse(StatusRow, await session.get("/api/status"))
        assert after.estop_latched, "the emergency stop is still latched"
        assert after.supervisor_estop is not None
        assert after.supervisor_estop.rule == before.supervisor_estop.rule
        standing = rig.panel.runtime.standing
        assert standing is not None, "the verdict of the drive found running still stands"
        assert standing.latched
        # In service again, and a start is still refused: by the latches, as it must be.
        await attest(session)
        asked = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert asked.status_code == HTTPStatus.CONFLICT, asked.text
        assert "latched safety verdict" in detail(asked)
    assert state_of(rig) is not RuntimeState.RUNNING
    assert rig.simulator.commanded_setpoint == 0


# =========================================================================
# What a start writes
# =========================================================================


def logged(records: Path) -> list[dict[str, object]]:
    path = records / logbook.DIRECTORY / logbook.ACTIVE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_ex2_the_start_that_goes_out_of_service_writes_it_and_the_restoring_is_signed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    recorder = rig.panel.recorder
    assert recorder is not None
    async with rig.http() as session:
        parse(ServiceRow, await restore(session))
    recorder.journal.drain()
    lines = logged(tmp_path / "records")
    assert [(line["kind"], line["actor"], line["detail"]) for line in lines[:2]] == [
        ("warning", "system", "unclean_stop: demarrage apres arret non propre"),
        ("warning", "system", f"out_of_service: {OUT_OF_SERVICE}"),
    ]
    signed = lines[2]
    assert signed["kind"] == "operator_action"
    assert signed["detail"] == "service_restored: console remise en service"
    assert str(signed["actor"]).startswith("op-")
    assert OPERATOR not in json.dumps(lines), "an alias in the logbook, never the name"


async def test_ex2_a_start_after_a_clean_stop_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = console(tmp_path, monkeypatch=monkeypatch)
    recorder = rig.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    assert not (tmp_path / "records" / logbook.DIRECTORY).exists()
