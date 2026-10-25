from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.real_records import REAL_DIR, unpack
from simulation.record_view import view_record
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import document, obj, run_file
from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.reader import read
from src.record.schema import EndObservation, GeometrySnapshot
from src.record.writer import Writer
from src.result import Ok
from src.training.types import ZoneCounters
from src.units import Seconds


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
    closed = created.value.close(ManualClock(), "operator_stop", observed)
    assert isinstance(closed, Ok)
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
    closed = created.value.close(ManualClock(), "done", observed)
    assert isinstance(closed, Ok)
    loaded = view_record(tmp_path, created.value.path.name)
    assert isinstance(loaded, Ok)
    lines = [document(line) for line in loaded.value.splitlines()]
    assert lines[-1]["code"] == "missing_final_drive_state"
    final = next(line for line in lines if line["type"] == "final")
    assert final["shaft_motor_rpm"] == observed.shaft_motor_rpm
    assert final["energised"] == observed.energised


ZONE_KEYS = ("zone_in_s", "zone_above_s", "zone_below_s")


def final_line(root: Path, name: str) -> Mapping[str, object]:
    """The viewer's ``final`` line of the record ``name`` under ``root``."""
    loaded = view_record(root, name)
    assert isinstance(loaded, Ok)
    lines = [document(line) for line in loaded.value.splitlines()]
    return next(line for line in lines if line["type"] == "final")


def test_t05_a_programme_run_closes_its_record_with_the_runtimes_zone_counters(
    tmp_path: Path,
) -> None:
    result = run_file(SCENARIO_DIR / "hr_critical.json")
    counted = result.counters
    assert result.trace.counters == counted
    assert counted.in_zone > 0.0
    path = result.trace.write_record(tmp_path, Privacy())
    loaded = read(path)
    assert isinstance(loaded, Ok)
    observation = loaded.value.manifest.end_observation
    assert observation is not None
    assert (observation.zone_in_s, observation.zone_above_s, observation.zone_below_s) == (
        counted.in_zone,
        counted.above_zone,
        counted.below_zone,
    )
    # The viewer copies the whole final observation: the three keys come with it.
    final = final_line(tmp_path, path.name)
    assert [final[key] for key in ZONE_KEYS] == [
        counted.in_zone,
        counted.above_zone,
        counted.below_zone,
    ]


def test_t05_a_record_without_a_programme_has_no_zone_whatever_the_runtime_counted(
    tmp_path: Path,
) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    assert trace.manifest.profile is None
    observed = ZoneCounters(in_zone=Seconds(4.0), above_zone=Seconds(2.0), below_zone=Seconds(1.0))
    path = replace(trace, counters=observed).write_record(tmp_path, Privacy())
    loaded = read(path)
    assert isinstance(loaded, Ok)
    observation = loaded.value.manifest.end_observation
    assert observation is not None
    assert (observation.zone_in_s, observation.zone_above_s, observation.zone_below_s) == (
        None,
        None,
        None,
    )
    final = final_line(tmp_path, path.name)
    assert [final[key] for key in ZONE_KEYS] == [None, None, None]


def test_t05_counters_nobody_observed_are_written_as_unknown(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "hr_critical.json").trace
    assert trace.manifest.profile is not None
    path = replace(trace, counters=None).write_record(tmp_path, Privacy())
    loaded = read(path)
    assert isinstance(loaded, Ok)
    observation = loaded.value.manifest.end_observation
    assert observation is not None
    assert (observation.zone_in_s, observation.zone_above_s, observation.zone_below_s) == (
        None,
        None,
        None,
    )


def test_t05_a_library_record_written_before_the_counters_reads_them_as_unknown(
    tmp_path: Path,
) -> None:
    archive = REAL_DIR / "auto_jog_150_dsp.tar.gz"
    unpacked = unpack(archive, tmp_path)
    assert isinstance(unpacked, Ok)
    written = document((unpacked.value / "manifest.json").read_text(encoding="utf-8"))
    assert written["kind"] == "auto"
    end = obj(written["end_observation"])
    assert not set(ZONE_KEYS) & set(end), "the archive predates the counters"
    loaded = read(unpacked.value)
    assert isinstance(loaded, Ok)
    assert not loaded.value.warnings
    observation = loaded.value.manifest.end_observation
    assert observation is not None
    assert (observation.zone_in_s, observation.zone_above_s, observation.zone_below_s) == (
        None,
        None,
        None,
    )
    final = final_line(REAL_DIR, archive.name)
    assert [final[key] for key in ZONE_KEYS] == [None, None, None]
