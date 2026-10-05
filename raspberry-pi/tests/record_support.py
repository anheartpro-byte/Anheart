from pathlib import Path

from src.record.codec import Privacy
from src.record.rows import Row
from src.record.schema import Clocks, Manifest
from src.record.writer import Writer
from src.result import Ok
from src.units import Monotonic


def manifest() -> Manifest:
    return Manifest(
        schema_version=2,
        record_id="record-1",
        machine_id="machine-2",
        organization_id="org-3",
        session_id=None,
        local_ref="local-4",
        kind="manual",
        occupancy="bench",
        operator="op-5",
        subject_id=None,
        profile=None,
        config_hash="ab" * 32,
        software_version="test-commit",
        contract_version="2",
        medical_parameters_version="synthetic-1",
        clocks=Clocks(
            monotonic_start=Monotonic(10), utc_start="2026-10-05T10:11:12Z", ntp_offset_s=None
        ),
        started_at="2026-10-05T10:11:12Z",
        ended_at=None,
        end_reason=None,
        preflight=None,
    )


def writer(root: Path, names: tuple[str, ...] = ()) -> Writer:
    result = Writer.create(root, manifest(), Privacy(names))
    assert isinstance(result, Ok)
    return result.value


def row() -> Row:
    return Row(
        t=0.2,
        state="active",
        mode="manual",
        phase="hold",
        drive_state="OPERATION_ENABLED",
        sim_state="OPERATION_ENABLED",
        setpoint_motor_rpm=123,
        lfrd_motor_rpm=122,
        measured_motor_rpm=121,
        measured_fresh=True,
        output_rpm=2.4,
        hertz=4.4,
        g_reference=0.01,
        g_leg_tip=0.02,
        setpoint_output_rpm=2.5,
        setpoint_g_leg_tip=0.03,
        manual_target_motor_rpm=124,
        hr_true=72,
        hr_live=73,
        target_bpm=None,
        safety_action="NONE",
        safety_rule=None,
        output_enabled=True,
        silent=False,
        current_a=0.2,
        hr_raw=74,
        hr_confirmed=73,
        hr_quality="good",
        drive_status_word=39,
    )
