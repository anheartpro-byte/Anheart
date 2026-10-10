"""The CAD-derived geometry: provenance, values, and the loader's refusals."""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from simulation.rig import GEOMETRY_PATH, RigGeometry, load_geometry, parse_geometry
from simulation.tests.conftest import document, num, obj, seq
from src.geometry import MachineGeometry
from src.result import Err, Ok
from src.units import Metres, OutputRpm

CAD_DIR: Final[Path] = GEOMETRY_PATH.parent
STEP: Final[Path] = CAD_DIR.parent.parent / "CAO" / "Gaura_Assy_2907.STEP"
CAD_PYTHON: Final[Path] = CAD_DIR / ".venv-cad" / "bin" / "python"


def _document() -> dict[str, object]:
    return dict(document(GEOMETRY_PATH.read_text(encoding="utf-8")))


def test_the_extracted_geometry_states_where_every_number_came_from() -> None:
    doc = _document()
    assert obj(doc["units"])["length"] == "m"
    source = obj(doc["source"])
    assert source["file"] == "Gaura_Assy_2907.STEP"
    assert len(str(source["sha256"])) == 64
    axis = obj(doc["rotation_axis"])
    assert axis["confirmed"] is True
    assert list(seq(axis["direction"])) == [0.0, 1.0, 0.0]
    assert all(num(obj(check)["offset_mm"]) <= 0.5 for check in seq(axis["coaxial_checks"]))
    assert num(obj(doc["arm"])["outboard_tip_radius_m"]) == pytest.approx(1.84)
    capsule = obj(doc["capsule"])
    assert num(capsule["far_wall_inner_radius_max_m"]) == pytest.approx(2.4254, abs=1e-3)
    assert num(capsule["near_wall_inner_radius_m"]) == pytest.approx(0.3282, abs=1e-3)
    # The rider is not modelled: every rider number is a flagged parameter.
    assert obj(doc["rider"])["modelled_in_cad"] is False
    derived_all = obj(doc["derived"])
    for key in ("leg_tip_radius_m", "leg_tip_radius_stature_estimate_m", "reference_radius_m"):
        derived = obj(derived_all[key])
        assert derived["must_be_measured"] is True
        assert derived["from"]
    assert obj(derived_all["leg_tip_radius_m"])["kind"] == "upper_bound"
    assert "NONE" in str(obj(doc["confidence"])["reference_radius"])


def test_the_default_rig_is_the_cad_upper_bound_and_the_project_radius() -> None:
    loaded = load_geometry()
    assert isinstance(loaded, Ok)
    rig = loaded.value
    assert rig.reference_radius == pytest.approx(1.5)
    assert rig.leg_tip_radius == pytest.approx(2.4254, abs=1e-3)
    assert rig.leg_tip_radius == pytest.approx(rig.capsule_far_radius)
    assert not rig.leg_tip_measured
    assert rig.machine.ratio == pytest.approx(49.79)
    assert rig.resultant_leg_tip(OutputRpm(0.0)) == pytest.approx(1.0)
    assert rig.resultant_leg_tip(OutputRpm(27.0)) == pytest.approx(
        math.hypot(1.9772, 1.0), abs=1e-3
    )


def test_a_scenario_may_override_either_radius() -> None:
    loaded = load_geometry(reference_radius=Metres(2.0), leg_tip_radius=Metres(1.4218))
    assert isinstance(loaded, Ok)
    assert loaded.value.reference_radius == pytest.approx(2.0)
    assert loaded.value.g_leg_tip(OutputRpm(27.0)) == pytest.approx(1.159, abs=1e-3)


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("not json", "not JSON"),
        ("[]", "JSON object"),
        ("{}", "missing derived.reference_radius_m.value"),
    ],
)
def test_a_broken_geometry_file_is_refused_with_a_reason(raw: str, fragment: str) -> None:
    parsed = parse_geometry(raw)
    assert isinstance(parsed, Err)
    assert fragment in parsed.error.detail


@pytest.mark.parametrize("bad", [True, "1.5", 0.0, -1.0])
def test_a_non_physical_number_is_refused(bad: object) -> None:
    doc = _document()
    doc["arm"] = {**obj(doc["arm"]), "outboard_tip_radius_m": bad}
    parsed = parse_geometry(json.dumps(doc))
    assert isinstance(parsed, Err)
    assert "arm.outboard_tip_radius_m" in parsed.error.detail


def test_an_override_that_is_not_physical_is_refused() -> None:
    parsed = parse_geometry(GEOMETRY_PATH.read_text(encoding="utf-8"), leg_tip_radius=Metres(-1.0))
    assert isinstance(parsed, Err)
    assert "leg_tip_radius" in parsed.error.detail


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    loaded = load_geometry(tmp_path / "absent.json")
    assert isinstance(loaded, Err)
    assert "cannot read" in loaded.error.detail


def test_the_rig_record_refuses_nan() -> None:
    with pytest.raises(ValueError, match="arm_tip_radius"):
        RigGeometry(
            machine=MachineGeometry(radius=Metres(1.5)),
            leg_tip_radius=Metres(2.0),
            arm_tip_radius=Metres(math.nan),
            capsule_near_radius=Metres(0.3),
            capsule_far_radius=Metres(2.4),
            counterweight_radius=Metres(0.9),
            leg_tip_measured=False,
        )


@pytest.mark.skipif(
    not (STEP.exists() and CAD_PYTHON.exists()), reason="STEP file or CAD venv not present"
)
def test_the_extraction_is_reproducible(tmp_path: Path) -> None:
    """Re-running the extractor on the STEP file reproduces the committed JSON exactly."""
    out = tmp_path / "machine_geometry.json"
    subprocess.run(  # noqa: S603  # fixed interpreter and script, no shell
        [str(CAD_PYTHON), str(CAD_DIR / "extract_geometry.py"), str(STEP), str(out)],
        check=True,
        capture_output=False,
        timeout=600,
    )
    assert document(out.read_text(encoding="utf-8")) == document(
        GEOMETRY_PATH.read_text(encoding="utf-8")
    )
    assert sys.executable  # the test itself ran in the project venv
