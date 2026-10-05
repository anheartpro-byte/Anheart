from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.reader import read
from src.record.schema import EndObservation, GeometrySnapshot
from src.record.writer import Writer
from src.result import Ok
from tests.record_support import manifest, row


def geometry() -> GeometrySnapshot:
    return GeometrySnapshot(
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


def test_ex11_shared_writer_context_survives_an_abrupt_session_without_final_observations(
    tmp_path: Path,
) -> None:
    given = replace(manifest(), geometry=geometry())
    created = Writer.create(tmp_path, given)
    assert isinstance(created, Ok)
    created.value.tick(row())
    loaded = read(created.value.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.geometry == geometry()
    assert loaded.value.manifest.end_observation is None
    assert any(w.code == "missing_checksums" for w in loaded.value.warnings)


def test_ex11_shared_writer_closes_with_actual_observations_without_inferring_unknowns(
    tmp_path: Path,
) -> None:
    observed = EndObservation(
        t=12.4,
        runtime_state="finished",
        drive_state="SWITCH_ON_DISABLED",
        shaft_motor_rpm=0,
        energised=False,
    )
    created = Writer.create(tmp_path, replace(manifest(), geometry=geometry()))
    assert isinstance(created, Ok)
    assert isinstance(created.value.close(ManualClock(), "operator_stop", observed), Ok)
    loaded = read(created.value.path)
    assert isinstance(loaded, Ok)
    actual = loaded.value.manifest.end_observation
    assert actual is not None
    assert actual == observed
    assert actual.runtime_applied_rpm is None


def test_ex10_context_diagnostics_use_the_same_identity_redaction_boundary(tmp_path: Path) -> None:
    sentinel = "SyntheticEndObserverSentinel"
    email = "sentinel@example.invalid"
    created = Writer.create(
        tmp_path, replace(manifest(), geometry=geometry()), Privacy((sentinel,))
    )
    assert isinstance(created, Ok)
    observed = EndObservation(t=1, shutdown_detail=f"{sentinel} {email}")
    assert isinstance(created.value.close(ManualClock(), "done", observed), Ok)
    for member in created.value.path.rglob("*"):
        if member.is_file():
            assert sentinel.encode() not in member.read_bytes()
            assert email.encode() not in member.read_bytes()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_ex9_neutral_context_rejects_nonfinite_geometry_and_observation_time(bad: float) -> None:
    with pytest.raises(ValidationError):
        replace(geometry(), reference_radius_m=bad)
    with pytest.raises(ValidationError):
        EndObservation(t=bad)
