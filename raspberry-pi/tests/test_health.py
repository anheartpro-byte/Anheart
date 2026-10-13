from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from src.cloud_sync import CloudSync
from src.contract import CONTRACT_VERSION, SERVER_VERSION_FIELD
from src.health import HealthContext, HealthWatch, pending_records
from src.health_counters import Counters, HealthCounters, load_counters, save_counters
from src.health_system import (
    Celsius,
    Percent,
    SystemHealth,
    finite_file,
    memory_used,
    system_health,
)
from src.local_panel import build_panel
from src.record.retention import confirm_deposit
from src.result import Ok
from src.units import Monotonic, MotorRpm, Seconds
from tests.test_cloud_sync import POLL_PATH, Dashboard, config_of, ok, rig
from tests.test_record_retention import CONFIRMED, closed_record


def test_missing_and_malformed_system_sources_are_unknown(tmp_path: Path) -> None:
    assert finite_file(tmp_path / "missing") is None
    assert memory_used(tmp_path / "missing") is None
    source = tmp_path / "source"
    for text in ("", "broken", "nan", "inf", "\xff"):
        source.write_text(text)
        assert finite_file(source) is None
    source.write_text("123.5 ignored")
    assert finite_file(source) == 123.5
    source.write_text("MemTotal: 100 kB\nMemAvailable: 25 kB\n")
    assert memory_used(source) == Percent(75)
    for text in ("MemTotal: 0\nMemAvailable: 0", "MemTotal: 10\nMemAvailable: 11", "bad"):
        source.write_text(text)
        assert memory_used(source) is None
    observed = system_health(tmp_path / "missing", tmp_path)
    assert observed.cpu_temp_c is None
    assert observed.mem_used_pct is None
    assert observed.disk_free_mb is None
    assert observed.uptime_s is None


def test_persisted_counters_recover_interrupted_sessions_and_reject_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "counters.json"
    counter = HealthCounters(load_counters(path))
    counter.started()
    value = counter.value
    assert value is not None
    assert save_counters(path, value)
    recovered = HealthCounters(load_counters(path)).value
    assert recovered is not None
    assert recovered.sessions_total == 1
    assert recovered.sessions_failed == 1
    assert not recovered.session_open
    path.write_text('{"sessions_total":-1}')
    assert load_counters(path) is None
    unknown = HealthCounters(None)
    unknown.started()
    assert unknown.value is None
    path.write_text('{"drive_faults_by_code":{"bad key":1}}')
    assert load_counters(path) is None
    path.write_text("truncated {")
    assert load_counters(path) is None


def test_rotation_counts_only_observed_intervals_and_session_end_once(tmp_path: Path) -> None:
    setup = rig(tmp_path)
    snapshot = setup.panel.runtime.snapshot()
    snapshot = replace(snapshot, measured=replace(snapshot.measured, motor_rpm=MotorRpm(100)))
    counter = HealthCounters(Counters(drive_faults_by_code={}))
    counter.started()
    for at in (0, 1, 2, 100):
        counter.observe(replace(snapshot, at=Monotonic(at)), None)
    counter.observe(replace(snapshot, at=Monotonic(101)), True)
    counter.observe(replace(snapshot, at=Monotonic(102)), True)
    value = counter.value
    assert value is not None
    assert value.rotation_hours == pytest.approx(4 / 3600)
    assert value.sessions_total == value.sessions_failed == 1
    assert not value.session_open


def test_pending_records_require_a_confirmed_deposit(tmp_path: Path) -> None:
    root = tmp_path / "records"
    assert pending_records(None) is None
    record = closed_record(root)
    assert pending_records(root) == 1
    assert isinstance(confirm_deposit(record, "verified-storage", CONFIRMED), Ok)
    assert pending_records(root) == 0


async def test_health_worker_does_not_block_the_event_loop_and_times_out(tmp_path: Path) -> None:
    release = threading.Event()
    entered = threading.Event()

    def blocked() -> SystemHealth:
        entered.set()
        release.wait(5)
        return SystemHealth(cpu_temp_c=Celsius(42))

    watch = HealthWatch(None, probe=blocked)
    snapshot = rig(tmp_path).panel.runtime.snapshot()
    task = asyncio.create_task(watch.sample(snapshot, HealthContext(), None, None))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert entered.is_set()
        assert not task.done()
        result = await task
        assert result["cpu_temp_c"] is None
        assert result["records_pending"] is None
        again = await watch.sample(snapshot, HealthContext(), None, None)
        assert again["cpu_temp_c"] is None
    finally:
        release.set()


async def test_heartbeat_health_carries_previous_rtt_and_validated_poll_echo(
    tmp_path: Path,
) -> None:
    setup = rig(tmp_path)
    setup.dashboard.answer(
        POLL_PATH,
        ok(
            {
                "session": None,
                SERVER_VERSION_FIELD: CONTRACT_VERSION,
                "incidents_open_high": 3,
            }
        ),
    )
    watch = HealthWatch(None, probe=lambda: SystemHealth(cpu_temp_c=Celsius(42)))
    sync = CloudSync(
        clock=setup.clock,
        transport=setup.dashboard,
        runtime=setup.runtime,
        surface=setup.panel.surface,
        store=setup.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        health=watch,
        health_context=lambda: HealthContext(bitalino_connected=False, restart_loop=True),
    )
    await sync.step()
    setup.clock.advance(Seconds(11))
    await sync.step()
    sent = setup.dashboard.to("/api/machine/heartbeat")
    first: object = sent[0]["health"]
    last: object = sent[1]["health"]
    assert isinstance(first, Mapping)
    assert isinstance(last, Mapping)
    assert first["link_rtt_ms"] is None
    assert last["link_rtt_ms"] == 0
    assert last["incidents_open_high"] == 3
    assert last["cpu_temp_c"] == 42
    assert last["bitalino_link"] == "down"
    assert last["restart_loop"] is True
    assert last["preflight_last"] is None
    json.dumps(sent)


async def test_composed_console_uses_its_health_observer(tmp_path: Path) -> None:
    setup = rig(tmp_path)
    dashboard = Dashboard()
    panel = build_panel(
        config_of(),
        clock=setup.clock,
        profiles_path=tmp_path / "health-profiles.json",
        transport=lambda _config: dashboard,
        health=HealthWatch(None, probe=lambda: SystemHealth(cpu_temp_c=Celsius(42))),
    )
    try:
        await panel.control_step()
        await panel.cloud_step()
        sent = dashboard.to("/api/machine/heartbeat")
        assert len(sent) == 1
        health: object = sent[0]["health"]
        assert isinstance(health, Mapping)
        assert health["cpu_temp_c"] == 42
        assert health["drive_link"] == "up"
        assert health["record_degraded"] is None
    finally:
        await panel.close()
