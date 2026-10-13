from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, assert_never

from src.health_counters import Counters, HealthCounters, load_counters, save_counters
from src.health_system import SystemHealth, system_health
from src.record.export import RecordIo
from src.record.retention import confirmed_deposit, records
from src.record.rows import JsonValue
from src.result import Err, Ok
from src.training.types import TelemetrySnapshot
from src.units import Seconds

HEALTH_TIMEOUT: Final[Seconds] = Seconds(1)
MILLISECONDS: Final[int] = 1000


@dataclass(frozen=True, slots=True)
class HealthContext:
    bitalino_connected: bool | None = None
    clock_offset_s: Seconds | None = None
    record_degraded: bool | None = None
    restart_loop: bool | None = None


@dataclass(frozen=True, slots=True)
class Collected:
    system: SystemHealth
    records_pending: int | None
    counters_saved: bool


def pending_records(root: Path | None) -> int | None:
    if root is None:
        return None
    try:
        return sum(confirmed_deposit(record) is None for record in records(root))
    except OSError:
        return None


def collect(
    root: Path | None,
    counter_file: Path | None,
    counters: Counters | None,
    probe: Callable[[], SystemHealth],
) -> Collected:
    saved = counter_file is None or counters is None or save_counters(counter_file, counters)
    return Collected(probe(), pending_records(root), saved)


def counter_wire(counters: Counters | None) -> Mapping[str, JsonValue]:
    faults: dict[str, JsonValue] | None = None
    if counters is not None and counters.drive_faults_by_code is not None:
        faults = dict(counters.drive_faults_by_code)
    return {
        "rotation_hours": None if counters is None else counters.rotation_hours,
        "sessions_total": None if counters is None else counters.sessions_total,
        "sessions_failed": None if counters is None else counters.sessions_failed,
        "drive_faults_by_code": faults,
    }


class HealthWatch:
    """Memory observations stay on the control loop; collection has one bounded daemon worker."""

    def __init__(
        self,
        root: Path | None,
        *,
        probe: Callable[[], SystemHealth] | None = None,
        io: RecordIo | None = None,
    ) -> None:
        self._root: Path | None = root
        self._counter_file: Path | None = (
            None if root is None else root.parent / "health-counters.json"
        )
        initial = (
            Counters(drive_faults_by_code={})
            if self._counter_file is None
            else load_counters(self._counter_file)
        )
        self._counters: HealthCounters = HealthCounters(initial)
        self._io: RecordIo = RecordIo(1) if io is None else io
        self._probe: Callable[[], SystemHealth] = (
            functools.partial(system_health, root) if probe is None else probe
        )

    def started(self) -> None:
        self._counters.started()

    def observe(self, snapshot: TelemetrySnapshot, failed: bool | None) -> None:
        self._counters.observe(snapshot, failed)

    async def sample(
        self,
        snapshot: TelemetrySnapshot,
        context: HealthContext,
        rtt: Seconds | None,
        incidents_open_high: int | None,
    ) -> Mapping[str, JsonValue]:
        counters = self._counters.value
        work = functools.partial(collect, self._root, self._counter_file, counters, self._probe)
        sampled = await self._io.run(work, HEALTH_TIMEOUT)
        system = SystemHealth()
        pending: int | None = None
        reported_counters: Counters | None = None
        match sampled:
            case Ok(collected):
                system = collected.system
                pending = collected.records_pending
                reported_counters = counters if collected.counters_saved else None
            case Err():
                pass
            case _ as unreachable:
                assert_never(unreachable)
        drive_link: str | None = None
        if snapshot.drive_status_age is not None:
            drive_link = "down" if snapshot.drive_status_is_stale else "up"
        return {
            "cpu_temp_c": system.cpu_temp_c,
            "cpu_load_1m": system.cpu_load_1m,
            "mem_used_pct": system.mem_used_pct,
            "disk_free_mb": system.disk_free_mb,
            "uptime_s": system.uptime_s,
            "clock_offset_s": context.clock_offset_s,
            "link_rtt_ms": None if rtt is None else rtt * MILLISECONDS,
            "bitalino_battery_pct": None,
            "bitalino_link": None
            if context.bitalino_connected is None
            else "up"
            if context.bitalino_connected
            else "down",
            "drive_link": drive_link,
            "drive_thermal_state": None,
            "records_pending": pending,
            "record_degraded": context.record_degraded,
            "restart_loop": context.restart_loop,
            "preflight_last": None,
            "incidents_open_high": incidents_open_high,
            "counters": dict(counter_wire(reported_counters)),
        }
