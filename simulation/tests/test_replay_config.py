from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.harness import PANEL_SAFETY, RUNTIME_LIMITS
from simulation.replay import replay
from simulation.replay_config import AppliedRuntime, load_configuration, sidecar
from simulation.tests.replay_support import edit_lines, record, short
from src.geometry import MachineGeometry
from src.local_config import load_local_config
from src.record.codec import JSON, mapping
from src.record.configuration import MAX_CONFIG_BYTES
from src.record.rows import JsonValue
from src.record.session import applied_configuration
from src.result import Err, Ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.units import (
    Bpm,
    GearRatio,
    Hertz,
    Metres,
    MotorRpm,
    OutputRpmPerSecond,
    RpmPerSecond,
)


def _document() -> dict[str, JsonValue]:
    loaded = load_local_config({"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"})
    assert isinstance(loaded, Ok)
    return dict(
        mapping(
            JSON.validate_json(
                applied_configuration(
                    loaded.value, RUNTIME_LIMITS, PANEL_SAFETY, DEFAULT_MOTION_LIMITS
                )
            )
        )
    )


def test_adjacent_document_names_distinguish_folders_from_archives(tmp_path: Path) -> None:
    folder = tmp_path / "folder.tar.gz"
    folder.mkdir()
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"")
    assert sidecar(folder) == tmp_path / "folder.tar.gz.config.json"
    assert sidecar(archive) == tmp_path / "archive.config.json"


def _load(tmp_path: Path, document: JsonValue) -> Ok[AppliedRuntime] | Err[str]:
    raw = json.dumps(document).encode()
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    return load_configuration(path, hashlib.sha256(raw).hexdigest())


def test_verified_document_uses_the_domain_constructors_and_all_recorded_limits(
    tmp_path: Path,
) -> None:
    loaded = load_local_config({"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"})
    assert isinstance(loaded, Ok)
    geometry = MachineGeometry(
        radius=Metres(2.1), ratio=GearRatio(50.0), nominal_rpm=MotorRpm(1300), base_hz=Hertz(45.0)
    )
    config = replace(loaded.value, geometry=geometry, leg_tip_radius=Metres(2.6))
    runtime = replace(RUNTIME_LIMITS, slew=RpmPerSecond(18))
    safety = replace(PANEL_SAFETY, hard_max_bpm=Bpm(147))
    motion = replace(DEFAULT_MOTION_LIMITS, output_accel=OutputRpmPerSecond(0.18))
    raw = applied_configuration(config, runtime, safety, motion)
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    found = load_configuration(path, hashlib.sha256(raw).hexdigest())
    assert isinstance(found, Ok), found
    assert found.value.geometry == geometry
    assert found.value.limit_radius == Metres(2.6)
    assert found.value.runtime == runtime
    assert found.value.safety == safety
    assert found.value.motion == motion
    assert found.value.check.differing_parts == ("safety", "motion", "runtime")


@pytest.mark.parametrize("part", ["file_sha256", "signed", "min_rider_age"])
def test_current_repository_differences_are_named_without_echoing_the_document(
    tmp_path: Path, part: str
) -> None:
    document = _document()
    medical = dict(mapping(document["medical"]))
    change: JsonValue = "f" * 64 if part == "file_sha256" else (False if part == "signed" else 99)
    medical[part] = change
    document["medical"] = medical
    found = _load(tmp_path, document)
    assert isinstance(found, Ok), found
    assert found.value.check.differing_parts == (
        ("min_rider_age",) if part == "min_rider_age" else ("medical_file",)
    )


@pytest.mark.parametrize("document", [None, {}, [], {"unknown": 1}])
def test_an_invalid_but_correctly_hashed_document_is_refused(
    tmp_path: Path, document: JsonValue
) -> None:
    found = _load(tmp_path, document)
    assert isinstance(found, Err)
    assert "refuses the console configuration document" in found.error


@pytest.mark.parametrize(
    "geometry",
    [[0, 49.79, 1380, 50, None], [1.5, 49.79, 1380, 50, 1.0], [1.5, 49.79, 1380, 50, float("nan")]],
)
def test_geometry_is_revalidated_at_the_replay_boundary(
    tmp_path: Path, geometry: list[JsonValue]
) -> None:
    document = _document()
    document["geometry"] = geometry
    found = _load(tmp_path, document)
    assert isinstance(found, Err)
    assert "configuration document" in found.error


@pytest.mark.parametrize("part", ["safety", "motion", "runtime"])
def test_unknown_limit_fields_are_refused(tmp_path: Path, part: str) -> None:
    document = _document()
    medical = dict(mapping(document["medical"]))
    owner = document if part == "runtime" else medical
    encoded = owner[part]
    assert isinstance(encoded, str)
    limits = dict(mapping(JSON.validate_json(encoded)))
    limits["unknown"] = 1
    owner[part] = json.dumps(limits)
    document["medical"] = medical
    assert isinstance(_load(tmp_path, document), Err)


def test_absent_unreadable_large_and_modified_documents_have_named_refusals(tmp_path: Path) -> None:
    absent = load_configuration(None, "a" * 64)
    assert isinstance(absent, Err)
    assert "absent" in absent.error
    unreadable = load_configuration(tmp_path, "a" * 64)
    assert isinstance(unreadable, Err)
    assert "unreadable" in unreadable.error
    path = tmp_path / "config.json"
    path.write_bytes(b"x" * (MAX_CONFIG_BYTES + 1))
    large = load_configuration(path, "a" * 64)
    assert isinstance(large, Err)
    assert "MAX_CONFIG_BYTES" in large.error
    path.write_bytes(b"{}")
    modified = load_configuration(path, "a" * 64)
    assert isinstance(modified, Err)
    assert "manifest aaaaaaaaaaaaaaaa" in modified.error
    assert hashlib.sha256(b"{}").hexdigest()[:16] in modified.error


def test_a_console_configuration_warning_refuses_the_record(tmp_path: Path) -> None:
    folder = record(short("manual_27_rpm", duration=0.4, preroll=0.4), tmp_path)
    edit_lines(
        folder,
        "events.jsonl",
        lambda lines: [
            *lines,
            {
                "t": 0,
                "kind": "warning",
                "actor": "system",
                "detail": "replay_config: OSError:ENOSPC",
            },
        ],
    )
    result = replay(folder)
    assert isinstance(result, Err)
    assert "could not persist its configuration document" in result.error.detail
