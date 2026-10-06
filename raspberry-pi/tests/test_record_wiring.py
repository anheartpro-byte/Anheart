"""Where the session record touches the rest of the console (ANH-128).

The arming gate inside the runtime (EX-4), the ``record_degraded`` field of the
dashboard heartbeat (EX-3), a launch from the dashboard recorded under its
session id (EX-1), and the record's configuration keys.
"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest

from src.clock import ManualClock
from src.cloud_sync import HEARTBEAT_PERIOD
from src.geometry import MachineGeometry
from src.local_config import (
    DEFAULT_RECORD_CONFIG,
    ConfigProblem,
    RecordConfig,
    load_local_config,
)
from src.local_panel import build_panel
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.journal import Journal
from src.record.reader import read
from src.record.schema import EventKind
from src.record.session import operator_alias
from src.result import Err, Ok
from src.training.runtime import (
    NotAttested,
    RecordStorageLow,
    RuntimeLimits,
    RuntimeState,
    TrainingRuntime,
)
from src.training.safety import SafetyLimits
from src.training.types import Occupancy
from src.units import Bpm, Metres, Monotonic, MotorRpm, RpmPerSecond, UnixMillis
from tests.record_journal_support import refuse_writes
from tests.test_cloud_sync import LAUNCH, OPERATOR, Dashboard, Linked, config_of, ok

SIM_ENV: Mapping[str, str] = {"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"}

# =========================================================================
# EX-4: the gate is one of the runtime's own arming gates
# =========================================================================


class Gate:
    """A scripted arming gate that counts how often it was asked."""

    def __init__(self, refusal: RecordStorageLow | None) -> None:
        self.refusal: RecordStorageLow | None = refusal
        self.asked: int = 0

    def __call__(self) -> RecordStorageLow | None:
        self.asked += 1
        return self.refusal


def gated_runtime(gate: Gate | None) -> tuple[TrainingRuntime, SimulatedDrive]:
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_700_000_000_000))
    drive = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    runtime = TrainingRuntime(
        clock=clock,
        drive=drive,
        geometry=MachineGeometry(radius=Metres(1.5)),
        limits=RuntimeLimits(slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10)),
        safety=SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158)),
        arming_gate=gate,
    )
    return runtime, drive


def state_of(runtime: TrainingRuntime) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return runtime.state


LOW = RecordStorageLow(free_bytes=10, required_bytes=500_000_000, where="/srv/records")


async def test_ex4_the_gate_refuses_the_arming_before_the_drive_is_touched() -> None:
    gate = Gate(LOW)
    runtime, drive = gated_runtime(gate)
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300))
    assert isinstance(refused, Err)
    assert refused.error is LOW
    assert gate.asked == 1
    assert state_of(runtime) is RuntimeState.IDLE
    assert drive.commanded_setpoint == 0
    assert runtime.drive_status is None, "the session path never read the drive"

    gate.refusal = None
    started = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300))
    assert isinstance(started, Ok)
    assert gate.asked == 2
    assert state_of(runtime) is RuntimeState.RUNNING


async def test_ex4_the_gate_is_asked_last_so_the_operator_first_hears_what_only_they_can_fix() -> (
    None
):
    gate = Gate(LOW)
    runtime, _drive = gated_runtime(gate)
    refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, NotAttested)
    assert gate.asked == 0


async def test_a_runtime_without_a_gate_arms_as_before_and_shows_its_last_drive_status() -> None:
    runtime, _drive = gated_runtime(None)
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    started = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300))
    assert isinstance(started, Ok)
    status = runtime.drive_status
    assert status is not None
    assert status.setpoint_echo_rpm == 0


# =========================================================================
# EX-3: the heartbeat says when the record is degraded
# =========================================================================


def recording_linked(tmp_path: Path) -> tuple[Linked, Dashboard, Journal]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    dashboard = Dashboard()
    journal = Journal(tmp_path / "records", clock)
    panel = build_panel(
        config_of(),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _config: dashboard,
        journal=journal,
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    return Linked(clock=clock, panel=panel, dashboard=dashboard), dashboard, journal


def beats(dashboard: Dashboard) -> list[Mapping[str, object]]:
    return dashboard.to("/api/machine/heartbeat")


async def test_ex3_the_heartbeat_carries_record_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, dashboard, journal = recording_linked(tmp_path)
    await rig.run(1.0)
    assert beats(dashboard)[-1]["recordDegraded"] is False
    assert "recordDegraded" not in cast("Mapping[str, object]", beats(dashboard)[-1]["live"])

    assert isinstance(
        rig.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await rig.run(1.0)
    journal.drain()
    refuse_writes(monkeypatch, "ticks.csv", errno.ENOSPC)
    await rig.run(1.0)
    journal.drain()
    await rig.run(float(HEARTBEAT_PERIOD))
    assert beats(dashboard)[-1]["recordDegraded"] is True
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    await rig.panel.close()


async def test_a_console_that_records_nothing_sends_no_record_field(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    dashboard = Dashboard()
    panel = build_panel(
        config_of(),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _config: dashboard,
    )
    await Linked(clock=clock, panel=panel, dashboard=dashboard).run(1.0)
    assert "recordDegraded" not in beats(dashboard)[-1]
    assert panel.recorder is None
    await panel.close()


# =========================================================================
# EX-1: a launch from the dashboard is recorded under its session id
# =========================================================================


async def test_ex1_a_dashboard_launch_is_recorded_with_its_identifiers_and_remote_commands(
    tmp_path: Path,
) -> None:
    rig, dashboard, journal = recording_linked(tmp_path)
    dashboard.answer("/api/machine/training/poll", ok({"session": dict(LAUNCH)}))
    await rig.run(2.0)
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    dashboard.answer("/api/machine/training/status", ok({"active": True, "stopRequested": True}))
    await rig.run(5.0)
    await rig.panel.close()

    records = [path for path in journal.root.iterdir() if not path.name.startswith(".")]
    assert [path.name.split("_", 1)[1] for path in records] == ["remote-1"]
    loaded = read(records[0])
    assert isinstance(loaded, Ok)
    recording = loaded.value
    assert recording.warnings == ()
    manifest = recording.manifest
    assert (manifest.kind, manifest.occupancy) == ("auto", "occupied")
    assert (manifest.session_id, manifest.subject_id) == ("remote-1", "user-1")
    assert manifest.operator == operator_alias("Dr Manager (tableau de bord)")
    assert manifest.profile is not None
    assert manifest.profile.subject_hr_max == 170
    remote = [(e.kind, e.actor) for e in recording.events if e.actor == "remote"]
    assert (EventKind.REMOTE_COMMAND, "remote") in remote
    on_disk = b"".join(p.read_bytes() for p in records[0].rglob("*") if p.is_file())
    assert b"Dr Manager" not in on_disk
    assert b"machine-key" not in on_disk


# =========================================================================
# The record's configuration keys
# =========================================================================


def problems(env: Mapping[str, str]) -> tuple[ConfigProblem, ...]:
    loaded = load_local_config(env)
    assert isinstance(loaded, Err), loaded
    return loaded.error


def record_of(env: Mapping[str, str]) -> RecordConfig:
    loaded = load_local_config(env)
    assert isinstance(loaded, Ok), loaded
    return loaded.value.record


def test_the_record_defaults_to_data_records_thirty_days_and_no_identity() -> None:
    record = record_of(SIM_ENV)
    assert record == DEFAULT_RECORD_CONFIG
    assert record.root == Path("data/records")
    assert record.retention_days == 30
    assert (record.machine_id, record.organization_id) == ("unassigned", "unassigned")
    assert record.software_version == "unversioned"
    blank = {
        **SIM_ENV,
        "RECORD_ROOT": " ",
        "RECORD_LOCAL_RETENTION_DAYS": "",
        "RECORD_MACHINE_ID": "",
    }
    assert record_of(blank) == DEFAULT_RECORD_CONFIG


def test_every_record_key_can_be_set() -> None:
    record = record_of(
        {
            **SIM_ENV,
            "RECORD_ROOT": "/var/lib/anheart/records",
            "RECORD_LOCAL_RETENTION_DAYS": "0",
            "RECORD_MACHINE_ID": "k17machine_A-1",
            "RECORD_ORGANIZATION_ID": "org_2abc",
            "ANHEART_SOFTWARE_VERSION": "2026.10.1+g1a2b3c4",
        }
    )
    assert record == RecordConfig(
        root=Path("/var/lib/anheart/records"),
        retention_days=0,
        machine_id="k17machine_A-1",
        organization_id="org_2abc",
        software_version="2026.10.1+g1a2b3c4",
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("RECORD_LOCAL_RETENTION_DAYS", "-1"),
        ("RECORD_LOCAL_RETENTION_DAYS", "3651"),
        ("RECORD_LOCAL_RETENTION_DAYS", "a month"),
        ("RECORD_MACHINE_ID", "Centrifugeuse de Lyon"),
        ("RECORD_MACHINE_ID", "../escape"),
        ("RECORD_ORGANIZATION_ID", "jean.dupont@example.org"),
        ("RECORD_ORGANIZATION_ID", "x" * 129),
        ("ANHEART_SOFTWARE_VERSION", "version one"),
    ],
)
def test_a_bad_record_key_is_named_and_refuses_the_configuration(key: str, value: str) -> None:
    assert [problem.key for problem in problems({**SIM_ENV, key: value})] == [key]


def test_every_record_problem_is_reported_with_the_others() -> None:
    found = problems(
        {
            "MOTOR_BACKEND": "sim",
            "ECG_SOURCE": "sim",
            "RECORD_LOCAL_RETENTION_DAYS": "never",
            "RECORD_MACHINE_ID": "a name",
            "RECORD_ORGANIZATION_ID": "another name",
            "ANHEART_SOFTWARE_VERSION": "v 1",
        }
    )
    assert {problem.key for problem in found} == {
        "ARM_RADIUS_M",
        "RECORD_LOCAL_RETENTION_DAYS",
        "RECORD_MACHINE_ID",
        "RECORD_ORGANIZATION_ID",
        "ANHEART_SOFTWARE_VERSION",
    }
    assert "identifiant opaque attendu" in next(
        problem.detail for problem in found if problem.key == "RECORD_MACHINE_ID"
    )
