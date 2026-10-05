from dataclasses import replace
from pathlib import Path

import pytest

from simulation.record_view import view_record
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import document, run_file
from src.clock import ManualClock
from src.record.schema import EndObservation, GeometrySnapshot
from src.record.writer import Writer
from src.result import Ok


def test_ex11_native_like_shared_writer_drives_the_same_viewer_geometry_and_final_state(
    tmp_path: Path,
) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    geometry = GeometrySnapshot(
        reference_radius_m=1.7,
        leg_tip_radius_m=2.2,
        leg_tip_measured=True,
        arm_tip_radius_m=1.8,
        capsule_near_radius_m=0.3,
        capsule_far_radius_m=2.4,
        counterweight_radius_m=0.9,
        gear_ratio=49.79,
        nominal_motor_rpm=1380,
        base_hz=50,
    )
    created = Writer.create(
        tmp_path, replace(trace.manifest, machine_id="synthetic-pi", geometry=geometry)
    )
    assert isinstance(created, Ok)
    for row in trace.rows:
        assert isinstance(created.value.tick(replace(row, hr_true=None, sim_state="")), Ok)
    observed = EndObservation(
        t=70,
        runtime_state="finished",
        drive_state="SWITCH_ON_DISABLED",
        shaft_motor_rpm=0,
        energised=False,
        runtime_applied_rpm=0,
    )
    assert isinstance(created.value.close(ManualClock(), "operator_stop", observed), Ok)
    loaded = view_record(tmp_path, created.value.path.name)
    assert isinstance(loaded, Ok)
    lines = [document(line) for line in loaded.value.splitlines()]
    assert lines[0]["reference_radius_m"] == 1.7
    assert lines[0]["leg_tip_radius_m"] == 2.2
    assert lines[0]["leg_tip_measured"] is True
    assert lines[0]["gear_ratio"] == 49.79
    assert not [line for line in lines if line["type"] == "warning"]
    final = lines[-1]
    assert final["sim_state"] == "SWITCH_ON_DISABLED"
    assert final["shaft_motor_rpm"] == 0
    assert final["energised"] is False
    assert final["runtime_state"] == "finished"
    assert final["t"] == 70


@pytest.mark.parametrize(
    "observed",
    [
        EndObservation(t=1, shaft_motor_rpm=0, energised=False),
        EndObservation(t=1, drive_state="SWITCH_ON_DISABLED", energised=False),
        EndObservation(t=1, drive_state="SWITCH_ON_DISABLED", shaft_motor_rpm=0),
    ],
)
def test_ex11_partially_unobserved_final_state_remains_unknown_and_warned(
    tmp_path: Path, observed: EndObservation
) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    created = Writer.create(tmp_path, trace.manifest)
    assert isinstance(created, Ok)
    assert isinstance(created.value.close(ManualClock(), "done", observed), Ok)
    loaded = view_record(tmp_path, created.value.path.name)
    assert isinstance(loaded, Ok)
    lines = [document(line) for line in loaded.value.splitlines()]
    assert lines[-1]["code"] == "missing_final_drive_state"
    final = next(line for line in lines if line["type"] == "final")
    assert final["shaft_motor_rpm"] == observed.shaft_motor_rpm
    assert final["energised"] == observed.energised
