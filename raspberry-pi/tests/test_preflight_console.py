"""ANH-142 on the console: the battery read at rest, the gate of every start, the page,
the heartbeat, the logbook and a launch from the dashboard.

What is judged lives in ``tests/test_preflight.py``; here, that the console wires it:
the same list reaches the runtime's arming gate, the remote preflight, the page,
the heartbeat and the logbook.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src import local_panel
from src.clock import ManualClock
from src.cloud_sync import DRIVE_INTERVENTIONS_FILE, CloudSync
from src.contract import CONTRACT_VERSION, SERVER_VERSION_FIELD
from src.health import HealthWatch
from src.health_system import Celsius, SystemHealth
from src.local_panel import (
    LocalPanel,
    build_panel,
    describe_start_refusal,
    record_preflight,
    record_source,
)
from src.motor.drive import DriveFault
from src.record.logbook import ACTIVE as LOGBOOK_ACTIVE
from src.record.logbook import DIRECTORY as LOGBOOK
from src.record.session import operator_alias
from src.record_uplink import (
    LOST_STOP,
    NOT_LINKED,
    NOTHING_LOST,
    RELEASED_BEFORE,
    SESSION_RUNS,
)
from src.result import Err, Ok, Result
from src.sim.bitalino import SimulatedBitalinoClient
from src.training.plan import JsonValue
from src.training.preflight import (
    BATTERY_WAIT,
    CONTROLS,
    ControlState,
    PreflightMonitor,
    PreflightRefused,
    judge,
)
from src.training.runtime import RuntimeState, Subject
from src.training.types import Occupancy
from src.units import Monotonic, MotorRpm, Percent, Seconds, UnixMillis
from src.web.app import create_app
from src.web.preflight_routes import PreflightRow
from tests.access_support import OPERATOR_ID, signed_http
from tests.test_cloud_sync import (
    DOWN,
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    Dashboard,
    Linked,
    Reply,
    config_of,
    launch_answer,
    linked,
    ok,
    rig,
)
from tests.test_contract import shared
from tests.test_local_panel import BASE_ENV, SERIAL_ENV, FakeEcgClient, config_from, ecg_link
from tests.test_preflight import _green  # pyright: ignore[reportPrivateUsage]  # one fixture
from tests.test_remote_bench_launch import BENCH
from tests.test_runtime import (
    _profile,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _program,  # pyright: ignore[reportPrivateUsage]
)
from tests.wall_clock_support import ScriptedClock

TICK: Final[Seconds] = Seconds(0.2)
END: Final[str] = "/api/machine/training/end"


def _console(tmp_path: Path, clock: ManualClock) -> LocalPanel:
    return build_panel(
        config_from(BASE_ENV), clock=clock, profiles_path=tmp_path / "p.json", preflight=True
    )


async def _settle(panel: LocalPanel, clock: ManualClock, seconds: float) -> None:
    """``seconds`` of the console's own tasks: the tick, the ECG and the pre-flight."""
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        await panel.control_step()
        await panel.ecg_step()
        await panel.preflight_step()


async def _run_out(panel: LocalPanel, clock: ManualClock) -> None:
    """Long enough for a run to wait for an absent BITalino, then for its longest control."""
    for _ in range(80):
        clock.advance(Seconds(1.0))
        await panel.preflight_step()


def _acquiring(client: FakeEcgClient) -> bool:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return client.is_acquiring


def _running(monitor: PreflightMonitor) -> bool:
    """Read through a call, for the reason :func:`_acquiring` gives."""
    return monitor.running


def _battery(monitor: PreflightMonitor) -> Percent | None:
    """Read through a call, for the reason :func:`_acquiring` gives."""
    return monitor.battery


def _object(value: object) -> Mapping[str, object]:
    """A JSON object as the console sent it, its values checked one by one."""
    assert isinstance(value, Mapping)
    return cast("Mapping[str, object]", value)


# =========================================================================
# The battery, read at rest
# =========================================================================


async def test_the_battery_is_read_with_the_acquisition_paused_then_restarted() -> None:
    clock = ManualClock()
    client = FakeEcgClient()
    client.battery = Percent(64.0)
    link = ecg_link(client, clock)
    await link.step()
    assert _acquiring(client)
    client.charge_read.clear()
    reading = asyncio.create_task(link.read_battery())
    await asyncio.sleep(0)
    assert not _acquiring(client)
    connects = client.connects
    await link.step()  # nothing is restarted while the device is asked
    assert not _acquiring(client)
    assert client.connects == connects
    client.charge_read.set()
    assert await reading == Percent(64.0)
    await link.step()  # and the acquisition comes back at once, without the retry wait
    assert _acquiring(client)


# =========================================================================
# Every start is judged by the same list
# =========================================================================


def _clock_on_board(panel: LocalPanel) -> ControlState:
    monitor = panel.preflight
    assert monitor is not None
    # As the arming gate judges it: nothing waited for.
    report = judge(CONTROLS, monitor.inputs(), Occupancy.OCCUPIED)
    return next(r for r in report.results if r.id == "clock").state


async def test_an_unknown_clock_only_warns_a_console_on_the_simulated_drive(tmp_path: Path) -> None:
    panel = _console(tmp_path, ManualClock())  # no probe: a system that cannot be asked
    try:
        assert panel.wall_clock is None
        assert _clock_on_board(panel) is ControlState.WARN
    finally:
        await panel.close()


def test_an_unknown_clock_refuses_a_person_on_board_with_a_real_drive(tmp_path: Path) -> None:
    """The ATV320 over Modbus: on the Pi the clock is always asked; elsewhere it is red."""
    panel = build_panel(
        config_from(SERIAL_ENV),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
        preflight=True,
    )
    assert panel.drive.simulator is None
    assert _clock_on_board(panel) is ControlState.BLOCK


async def test_a_console_without_a_preflight_judges_none() -> None:
    panel = build_panel(config_from(BASE_ENV), clock=ManualClock())
    try:
        assert panel.preflight is None
        assert panel.services.preflight is None
        await panel.preflight_step()
    finally:
        await panel.close()


async def test_a_low_battery_warns_at_the_bench_and_the_bench_start_goes_through(
    tmp_path: Path,
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = _console(tmp_path, clock)
    try:
        monitor = panel.preflight
        assert monitor is not None
        assert panel.services.preflight is monitor
        simulator = panel.ecg.client
        assert isinstance(simulator, SimulatedBitalinoClient)
        simulator.battery = Percent(20.0)
        await _settle(panel, clock, 2.0)
        assert _battery(monitor) == Percent(20.0)
        assert simulator.is_acquiring  # the read paused it, the next step started it again
        refused = monitor.refusal(Occupancy.BENCH)
        assert refused is not None
        assert refused.control == "estop"  # red at the bench too
        assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
        bench = monitor.report(Occupancy.BENCH)
        battery = next(r for r in bench.results if r.id == "bitalino")
        assert battery.detail == "batterie BITalino 20 % < 30 %"
        assert monitor.refusal(Occupancy.BENCH) is None  # only a warning at the bench
        assert monitor.refusal(Occupancy.OCCUPIED) is not None
        submitted = panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
        assert isinstance(submitted, Ok)
        await _settle(panel, clock, 0.4)
        assert panel.runtime.manual is not None  # it armed
    finally:
        await panel.close()


def test_the_refusal_of_a_start_says_pre_vol_and_the_reason() -> None:
    refused = PreflightRefused("bitalino", "batterie BITalino 20 % < 30 %")
    assert describe_start_refusal(refused) == (
        "demarrage refuse : pre-vol : batterie BITalino 20 % < 30 %"
    )


async def test_a_failing_preflight_step_never_stops_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(self: SimulatedBitalinoClient) -> Percent | None:
        del self
        raise RuntimeError("battery read failed")

    monkeypatch.setattr(SimulatedBitalinoClient, "read_battery", broken)
    clock = ManualClock()
    panel = _console(tmp_path, clock)
    try:
        await panel.ecg_step()
        await panel.preflight_step()
        assert "pre-flight step failed" in caplog.text
        assert panel.preflight is not None
        assert panel.preflight.running
    finally:
        await panel.close()


# =========================================================================
# A launch from the dashboard
# =========================================================================


async def test_a_launch_on_a_red_preflight_comes_back_failed_with_its_reason(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path, preflight=True)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    r.confirm_offer()
    await r.step()
    # Nothing was confirmed to the dashboard: the machine refused before.
    assert r.dashboard.to(START) == []
    (end,) = r.dashboard.to(END)
    assert end["failed"] is True
    reason = end["reason"]
    assert isinstance(reason, str)
    # The panel's drive was never read in this rig: the first red control.
    assert reason == (
        "refusee par la machine : pre-vol : variateur injoignable : aucune lecture recente "
        "de son etat"
    )
    # ANH-145: the dashboard counts three such refusals in a row by these words.
    incidents = cast("Mapping[str, object]", shared()["incidents"])
    prefix = incidents["preflight_refusal_prefix"]
    assert isinstance(prefix, str)
    assert reason.startswith(prefix)
    await r.panel.close()


# =========================================================================
# What is written down and announced
# =========================================================================


async def test_a_completed_run_is_written_to_the_logbook(tmp_path: Path) -> None:
    r = rig(tmp_path, record_root=tmp_path / "records", preflight=True)
    journal = r.journal
    assert journal is not None
    monitor = r.panel.preflight
    assert monitor is not None
    await _run_out(r.panel, r.clock)
    assert not _running(monitor)
    last = monitor.last
    assert last is not None
    assert last.occupancy is Occupancy.OCCUPIED  # programmes are enabled on this console
    journal.drain()
    text = (journal.root / "logbook" / "events.jsonl").read_text("utf-8")
    lines = [cast("Mapping[str, object]", json.loads(line)) for line in text.splitlines()]
    (written,) = [line for line in lines if line["kind"] == "preflight"]
    detail = written["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("pre-vol block (personne a bord) : drive_link block")
    assert written["actor"] == "system"
    await r.panel.close()


def test_a_run_on_a_console_that_records_nothing_is_only_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.local_panel")
    report = judge(CONTROLS, _green(), Occupancy.BENCH)
    record_preflight(report, None, ManualClock())
    assert "pre-vol warn (banc)" in caplog.text


def _hot_pi(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel's thermal zone says 42 °C (there is none on the machine running the tests)."""

    def measured(root: Path = Path("/")) -> Celsius | None:
        del root
        return Celsius(42.0)

    monkeypatch.setattr(local_panel, "cpu_temperature", measured)


async def test_a_console_with_no_dashboard_measures_its_pi_all_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hot_pi(monkeypatch)
    panel = _console(tmp_path, ManualClock())
    try:
        assert panel.cloud is None
        monitor = panel.preflight
        assert monitor is not None
        await panel.preflight_step()
        temperature = next(
            r for r in monitor.report(Occupancy.BENCH).results if r.id == "temperature"
        )
        assert temperature.detail == "Pi a 42 °C"
    finally:
        await panel.close()


async def test_the_heartbeat_carries_the_last_run_and_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hot_pi(monkeypatch)
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    dashboard = Dashboard()
    panel = build_panel(
        config_of(),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _config: dashboard,
        health=HealthWatch(None, probe=lambda: SystemHealth(cpu_temp_c=Celsius(42))),
        preflight=True,
    )
    try:
        await panel.control_step()
        await panel.ecg_step()
        await panel.preflight_step()
        await panel.cloud_step()
        first = dashboard.to("/api/machine/heartbeat")[0]["health"]
        assert isinstance(first, Mapping)
        assert first["bitalino_battery_pct"] == 100.0
        assert first["preflight_last"] is None  # the run is still waiting
        monitor = panel.preflight
        assert monitor is not None
        # The temperature the pre-flight measured itself.
        temperature = next(
            r for r in monitor.report(Occupancy.BENCH).results if r.id == "temperature"
        )
        assert temperature.detail == "Pi a 42 °C"
        clock.advance(BATTERY_WAIT)
        await _run_out(panel, clock)
        assert monitor.last is not None
        clock.advance(Seconds(11.0))
        await panel.cloud_step()
        last = dashboard.to("/api/machine/heartbeat")[-1]["health"]
        assert isinstance(last, Mapping)
        assert last["preflight_last"] == monitor.last.verdict.value
        assert last["preflight_last"] in ("ok", "warn", "block")
    finally:
        await panel.close()


# =========================================================================
# The page
# =========================================================================


async def test_the_page_reads_the_list_and_asks_for_a_new_run(tmp_path: Path) -> None:
    clock = ManualClock()
    panel = _console(tmp_path, clock)
    app = create_app(services=panel.services, config=config_from(BASE_ENV).web)
    try:
        await _run_out(panel, clock)
        monitor = panel.preflight
        assert monitor is not None
        assert not _running(monitor)
        async with signed_http(app, panel.services) as session:
            read = await session.get("/api/preflight")
            assert read.status_code == HTTPStatus.OK
            row = cast("Mapping[str, object]", read.json())
            assert row["running"] is False
            assert row["last"] in ("warn", "block")
            bench = _object(row["bench"])
            assert bench["occupancy"] == "bench"
            controls = bench["controls"]
            assert isinstance(controls, list)
            ids = [_object(entry)["id"] for entry in cast("list[object]", controls)]
            assert ids == [control.id for control in CONTROLS]
            assert _object(row["occupied"])["first_block"] is not None
            rerun = await session.post("/api/preflight/run")
            assert rerun.status_code == HTTPStatus.ACCEPTED
            assert cast("Mapping[str, object]", rerun.json())["running"] is True
        assert _running(monitor)
    finally:
        await panel.close()


async def test_a_console_without_a_preflight_serves_none_and_refuses_a_run() -> None:
    panel = build_panel(config_from(BASE_ENV), clock=ManualClock())
    services = replace(panel.services, preflight=None)
    app = create_app(services=services, config=config_from(BASE_ENV).web)
    try:
        async with signed_http(app, services) as session:
            assert (await session.get("/api/preflight")).json() is None
            refused = await session.post("/api/preflight/run")
            assert refused.status_code == HTTPStatus.CONFLICT
    finally:
        await panel.close()


# =========================================================================
# A bench session launched from the site (ANH-250): the bench's rules
# =========================================================================


async def test_a_bench_launch_is_refused_before_its_start_when_a_bench_control_is_red(
    tmp_path: Path,
) -> None:
    """A drive in fault blocks the bench as it blocks a person on board."""
    rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(1.0)
    simulator = rig_.panel.drive.simulator
    assert simulator is not None
    simulator.inject_fault(DriveFault.OVERCURRENT)
    await rig_.run(1.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    refused = monitor.refusal(Occupancy.BENCH)
    assert refused is not None
    assert refused.control == "drive_state"
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    await rig_.run(2.0)
    assert rig_.panel.runtime.manual is None
    assert rig_.dashboard.to(START) == []  # refused before training/start
    (end,) = rig_.dashboard.to(END)
    assert end["sessionId"] == "remote-bench"
    assert end["failed"] is True
    reason = end["reason"]
    assert isinstance(reason, str)
    assert reason.startswith("refusee par la machine : pre-vol : variateur en defaut")
    await rig_.panel.close()


async def test_a_serious_incident_read_on_the_poll_refuses_a_person_on_board_and_warns_at_the_bench(
    tmp_path: Path,
) -> None:
    """ANH-145 EX-4: the count the server announces on the poll closes programmed sessions,
    and leaves the manual bench session that looks for the cause."""
    rig_ = linked(tmp_path, preflight=True)
    announced = ok(
        {"session": dict(BENCH), SERVER_VERSION_FIELD: CONTRACT_VERSION, "incidents_open_high": 1}
    )
    rig_.dashboard.answer(POLL_PATH, announced)
    await rig_.run(13.0)  # the poll offers the launch and announces the count
    monitor = rig_.panel.preflight
    assert monitor is not None
    on_board = monitor.refusal(Occupancy.OCCUPIED)
    assert on_board is not None
    assert on_board.control == "incidents"
    assert on_board.detail == (
        "pre-vol : 1 incident(s) grave(s) ouvert(s) sur cette machine : aucune personne a bord "
        "avant leur cloture par un admin Anheart"
    )
    assert monitor.refusal(Occupancy.BENCH) is None
    bench = {r.id: r.state for r in judge(CONTROLS, monitor.inputs(), Occupancy.BENCH).results}
    assert bench["incidents"] is ControlState.WARN
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    await rig_.run(1.8)
    assert [body["sessionId"] for body in rig_.dashboard.to(START)] == ["remote-bench"]
    manual = rig_.panel.runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    await rig_.panel.close()


DRIVE_INTERVENTION_REFUSAL: Final[str] = (
    "pre-vol : 1 defaut(s) variateur non rearmable(s) en attente d'intervention : aucune "
    "personne a bord avant l'intervention au carnet de bord et la cloture de l'incident"
)


def _interventions(session: Mapping[str, object] | None, due: object) -> Reply:
    """A poll answer of this contract: no serious incident, ``due`` drive interventions."""
    return ok(
        {
            "session": cast("JsonValue", session),
            SERVER_VERSION_FIELD: CONTRACT_VERSION,
            "incidents_open_high": 0,
            "drive_interventions_due": cast("JsonValue", due),
        }
    )


def _interventions_state(monitor: PreflightMonitor, occupancy: Occupancy) -> ControlState:
    report = judge(CONTROLS, monitor.inputs(), occupancy)
    return next(r.state for r in report.results if r.id == "drive_interventions")


async def test_a_drive_intervention_due_refuses_every_start_with_a_person_on_board(
    tmp_path: Path,
) -> None:
    """ANH-149 EX-4: a programme and an occupied manual session alike, stricter than the
    dashboard, which closes programmed sessions only; the bench is warned and arms."""
    rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, _interventions(None, 1))
    await rig_.run(13.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    on_board = monitor.refusal(Occupancy.OCCUPIED)
    assert on_board is not None
    assert on_board.control == "drive_interventions"
    assert on_board.detail == DRIVE_INTERVENTION_REFUSAL
    runtime = rig_.panel.runtime
    programme = await runtime.start(
        _program(_profile()), Subject(subject_id="rider-1", operator=OPERATOR)
    )
    assert isinstance(programme, Err)
    assert programme.error == on_board
    occupied = await runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MotorRpm(300))
    assert isinstance(occupied, Err)
    assert occupied.error == on_board
    assert _interventions_state(monitor, Occupancy.BENCH) is ControlState.WARN
    assert monitor.refusal(Occupancy.BENCH) is None
    submitted = rig_.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(submitted, Ok)
    await rig_.run(0.4)
    manual = runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    await rig_.panel.close()


@pytest.mark.parametrize(
    ("due", "state"),
    [
        pytest.param(0, ControlState.OK, id="none-due"),
        pytest.param(None, ControlState.WARN, id="not-announced"),
    ],
)
async def test_no_drive_intervention_due_or_none_announced_refuses_nobody(
    tmp_path: Path, due: object, state: ControlState
) -> None:
    """ANH-149: a count of 0 lets a person on board; one not announced (a 1.15 dashboard, an
    unreadable value) is orange, as the incidents' count is."""
    rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, _interventions(None, due))
    await rig_.run(13.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    for occupancy in Occupancy:
        assert _interventions_state(monitor, occupancy) is state
    refused = monitor.refusal(Occupancy.OCCUPIED)
    assert refused is None or refused.control != "drive_interventions"
    await rig_.panel.close()


KEPT_BEFORE: Final[str] = (
    " (compte annonce avant le redemarrage, le 2023-11-14 22:13 UTC, pas encore relu)"
)


async def _power_cycle(tmp_path: Path, due: int) -> Linked:
    """A console that read ``due`` from its dashboard, then started again with none."""
    first = linked(tmp_path, preflight=True)
    first.dashboard.answer(POLL_PATH, _interventions(None, due))
    await first.run(13.0)
    await first.panel.close()
    again = linked(tmp_path, preflight=True)
    again.dashboard.answer(POLL_PATH, DOWN)
    await again.run(13.0)
    return again


async def test_a_count_kept_across_a_power_cycle_holds_a_person_on_board_until_a_poll_says_0(
    tmp_path: Path,
) -> None:
    """ANH-149: the likely sequence. A drive fault the console cannot reset, the machine
    switched off and on again: the count read before holds, offline and through answers
    that carry none, until the dashboard says 0."""
    rig_ = await _power_cycle(tmp_path, 1)
    monitor = rig_.panel.preflight
    assert monitor is not None
    on_board = monitor.refusal(Occupancy.OCCUPIED)
    assert on_board is not None
    assert on_board.control == "drive_interventions"
    assert on_board.detail == DRIVE_INTERVENTION_REFUSAL + KEPT_BEFORE
    occupied = await rig_.panel.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MotorRpm(300))
    assert isinstance(occupied, Err)
    assert occupied.error == on_board
    assert _interventions_state(monitor, Occupancy.BENCH) is ControlState.WARN
    assert monitor.refusal(Occupancy.BENCH) is None
    # A dashboard rolled back below 1.16 says no count: it lifts nothing.
    rig_.dashboard.answer(POLL_PATH, ok({SERVER_VERSION_FIELD: "1.15", "session": None}))
    await rig_.run(4.0)
    still = monitor.refusal(Occupancy.OCCUPIED)
    assert still is not None
    assert still.control == "drive_interventions"
    rig_.dashboard.answer(POLL_PATH, _interventions(None, 0))
    await rig_.run(4.0)
    assert _interventions_state(monitor, Occupancy.OCCUPIED) is ControlState.OK
    lifted = monitor.refusal(Occupancy.OCCUPIED)
    assert lifted is None or lifted.control != "drive_interventions"
    await rig_.panel.close()


async def test_a_0_kept_across_a_power_cycle_is_orange_with_its_date(tmp_path: Path) -> None:
    rig_ = await _power_cycle(tmp_path, 0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    report = judge(CONTROLS, monitor.inputs(), Occupancy.OCCUPIED)
    kept = next(r for r in report.results if r.id == "drive_interventions")
    assert kept.state is ControlState.WARN
    assert kept.detail == "aucun defaut variateur en attente d'intervention" + KEPT_BEFORE
    await rig_.panel.close()


@pytest.mark.parametrize("content", [None, b"{not json"], ids=["no-file", "corrupt"])
async def test_a_count_never_kept_or_unreadable_is_orange(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes | None
) -> None:
    """ANH-149: never known, it is unknown, as before; a file that does not parse is
    unknown too, and said in the log: never taken for 0."""
    if content is not None:
        (tmp_path / DRIVE_INTERVENTIONS_FILE).write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="src.cloud_sync"):
        rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, DOWN)
    await rig_.run(13.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    for occupancy in Occupancy:
        assert _interventions_state(monitor, occupancy) is ControlState.WARN
    said = f"{DRIVE_INTERVENTIONS_FILE} is not a count this console wrote" in caplog.text
    assert said is (content is not None)
    await rig_.panel.close()


async def test_a_launch_from_the_site_is_refused_before_its_start_while_an_intervention_is_due(
    tmp_path: Path,
) -> None:
    """ANH-149: the console refuses it itself, before confirming it to the dashboard."""
    rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, _interventions(dict(LAUNCH), 1))
    await rig_.run(13.0)
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-1", OPERATOR), Ok)
    await rig_.run(2.0)
    assert rig_.panel.runtime.state is RuntimeState.IDLE
    assert rig_.dashboard.to(START) == []
    (end,) = rig_.dashboard.to(END)
    assert end["sessionId"] == "remote-1"
    assert end["failed"] is True
    assert end["reason"] == f"refusee par la machine : {DRIVE_INTERVENTION_REFUSAL}"
    await rig_.panel.close()


async def test_a_stop_the_dashboard_has_not_counted_holds_a_person_on_board_not_the_bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANH-145: the pre-flight reads from the dashboard link whether a session that ended
    on a stop is still unsent; the count the server announced cannot hold it yet."""
    words = "seance terminee sur safety_verdict pas encore recue par le tableau de bord"

    def unreported(_self: CloudSync) -> str:
        return words

    monkeypatch.setattr(CloudSync, "unreported_stop", property(unreported))
    announced = ok(
        {"session": dict(BENCH), SERVER_VERSION_FIELD: CONTRACT_VERSION, "incidents_open_high": 0}
    )
    rig_ = linked(tmp_path, preflight=True)
    rig_.dashboard.answer(POLL_PATH, announced)
    await rig_.run(13.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    on_board = monitor.refusal(Occupancy.OCCUPIED)
    assert on_board is not None
    assert on_board.control == "incidents"
    assert words in on_board.detail
    assert monitor.refusal(Occupancy.BENCH) is None
    # It clears by itself once the link delivers: not offered to the operator.
    assert not PreflightRow.of(monitor).acknowledgeable
    words = LOST_STOP.format(why="fin refusee, k17remote")
    row = PreflightRow.of(monitor)
    assert row.unreported_stop == words
    assert row.acknowledgeable, "a session lost for good is"
    await rig_.panel.close()


async def test_the_page_acknowledges_a_lost_stop_by_name(tmp_path: Path) -> None:
    """ANH-145: ``POST /api/preflight/acknowledge``, wired to the panel, which notes it."""
    rig_ = linked(tmp_path, preflight=True)
    assert rig_.panel.services.stop_acknowledge == rig_.panel.acknowledge_lost_stops
    told: list[str] = []
    answers: list[Result[tuple[str, ...], str]] = [
        Ok(("fin refusee, k17remote",)),
        Err(NOTHING_LOST),
    ]

    async def acknowledge(operator: str) -> Result[tuple[str, ...], str]:
        told.append(operator)
        return answers.pop(0)

    services = replace(rig_.panel.services, stop_acknowledge=acknowledge)
    app = create_app(services=services, config=config_from(BASE_ENV).web)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as anonymous:
            row = cast("Mapping[str, object]", (await anonymous.get("/api/preflight")).json())
            assert row["unreported_stop"] is None
            unsigned = await anonymous.post("/api/preflight/acknowledge")
            assert unsigned.status_code == HTTPStatus.UNAUTHORIZED
        async with signed_http(app, services) as session:
            done = await session.post("/api/preflight/acknowledge")
            assert done.status_code == HTTPStatus.OK
            again = await session.post("/api/preflight/acknowledge")
            assert again.status_code == HTTPStatus.CONFLICT
            assert cast("Mapping[str, object]", again.json())["detail"] == NOTHING_LOST
            assert told == [OPERATOR_ID, OPERATOR_ID]
            unlinked_services = replace(services, stop_acknowledge=None)
            unlinked = create_app(services=unlinked_services, config=config_from(BASE_ENV).web)
            async with signed_http(unlinked, unlinked_services) as other:
                refused = await other.post("/api/preflight/acknowledge")
                assert refused.status_code == HTTPStatus.CONFLICT
                assert cast("Mapping[str, object]", refused.json())["detail"] == NOT_LINKED
    finally:
        await rig_.panel.close()


async def test_the_panel_notes_an_acknowledgement_in_the_logbook_by_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ANH-145: what was released, by whom, in the logbook and in the log (``%r``)."""
    r = rig(tmp_path, record_root=tmp_path / "records", preflight=True)
    journal = r.journal
    assert journal is not None
    try:

        async def released(
            _self: CloudSync, noted: Callable[[str], None]
        ) -> Result[tuple[str, ...], str]:
            noted("lost_stops_acknowledged: fin refusee, k17remote")
            return Ok(("fin refusee, k17remote",))

        monkeypatch.setattr(CloudSync, "acknowledge_lost_stops", released)
        done = await r.panel.acknowledge_lost_stops("Mme\nDurand")
        assert done == Ok(("fin refusee, k17remote",))
        assert "'Mme\\nDurand'" in caplog.text, "the name is quoted in the log"
        journal.drain()
        logbook = (journal.root / LOGBOOK / LOGBOOK_ACTIVE).read_text(encoding="utf-8")
        line = cast("Mapping[str, object]", json.loads(logbook.splitlines()[-1]))
        assert line["kind"] == "operator_action"
        assert line["detail"] == "lost_stops_acknowledged: fin refusee, k17remote"
        assert line["actor"] == operator_alias("Mme\nDurand")

        async def refused(
            _self: CloudSync, _noted: Callable[[str], None]
        ) -> Result[tuple[str, ...], str]:
            return Err(SESSION_RUNS)

        monkeypatch.setattr(CloudSync, "acknowledge_lost_stops", refused)
        assert await r.panel.acknowledge_lost_stops(OPERATOR) == Err(SESSION_RUNS)
    finally:
        await r.panel.close()


async def test_what_the_sending_says_goes_to_the_logbook_whatever_runs(tmp_path: Path) -> None:
    """ANH-145: an acknowledgement an earlier run made, which the register tells the next
    start, is a line of the logbook, written by the system: never a session's record."""
    r = rig(tmp_path, record_root=tmp_path / "records", preflight=True)
    journal = r.journal
    assert journal is not None
    try:
        record_source(journal, r.clock).logbook(RELEASED_BEFORE + "fin refusee, k1")
        journal.drain()
        logbook = (journal.root / LOGBOOK / LOGBOOK_ACTIVE).read_text(encoding="utf-8")
        line = cast("Mapping[str, object]", json.loads(logbook.splitlines()[-1]))
        assert line["detail"] == RELEASED_BEFORE + "fin refusee, k1"
        assert line["kind"] == "warning"
        assert line["actor"] == "system"
    finally:
        await r.panel.close()


async def test_a_console_that_records_nothing_logs_an_acknowledgement_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rig_ = linked(tmp_path, preflight=True)
    try:

        async def released(
            _self: CloudSync, noted: Callable[[str], None]
        ) -> Result[tuple[str, ...], str]:
            noted("lost_stops_acknowledged: fin abandonnee, k1")
            return Ok(("fin abandonnee, k1",))

        monkeypatch.setattr(CloudSync, "acknowledge_lost_stops", released)
        assert await rig_.panel.acknowledge_lost_stops(OPERATOR) == Ok(("fin abandonnee, k1",))
        assert "lost_stops_acknowledged: fin abandonnee, k1" in caplog.text
    finally:
        await rig_.panel.close()


async def test_a_console_not_linked_has_nothing_to_acknowledge(tmp_path: Path) -> None:
    panel = _console(tmp_path, ManualClock())
    try:
        assert await panel.acknowledge_lost_stops(OPERATOR) == Err(NOT_LINKED)
    finally:
        await panel.close()


async def test_a_bench_launch_arms_when_only_a_person_on_board_would_be_refused(
    tmp_path: Path,
) -> None:
    """No BITalino and a clock nobody keeps: red with a person on board, warnings at the bench."""
    rig_ = linked(tmp_path, preflight=True, wall_clock=ScriptedClock())
    simulator = rig_.panel.ecg.client
    assert isinstance(simulator, SimulatedBitalinoClient)
    simulator.inject_connect_failure()
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(1.0)
    monitor = rig_.panel.preflight
    assert monitor is not None
    on_board = monitor.refusal(Occupancy.OCCUPIED)
    assert on_board is not None
    assert monitor.refusal(Occupancy.BENCH) is None
    bench = {r.id: r.state for r in judge(CONTROLS, monitor.inputs(), Occupancy.BENCH).results}
    assert bench["bitalino"] is ControlState.WARN
    assert bench["clock"] is ControlState.WARN
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    await rig_.run(1.8)
    assert [body["sessionId"] for body in rig_.dashboard.to(START)] == ["remote-bench"]
    manual = rig_.panel.runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    assert rig_.dashboard.to(END) == []
    await rig_.panel.close()
