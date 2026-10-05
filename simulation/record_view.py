import json
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from src.record.codec import document, mapping
from src.record.reader import read
from src.record.rows import JsonValue
from src.record.schema import EndObservation, GeometrySnapshot, RecordError
from src.result import Err, Ok, Result

GEOMETRY: Final[TypeAdapter[GeometrySnapshot]] = TypeAdapter(GeometrySnapshot)
OBSERVATION: Final[TypeAdapter[EndObservation]] = TypeAdapter(EndObservation)


def view_record(root: Path, requested: str) -> Result[str, RecordError]:
    path = (root / requested.lstrip("/")).resolve()
    if not path.is_relative_to(root.resolve()):
        return Err(RecordError("view", "outside_root"))
    loaded = read(path)
    if isinstance(loaded, Err):
        return loaded
    record = loaded.value
    meta: dict[str, JsonValue] = {
        "type": "meta",
        "schema": 2,
        "scenario": record.manifest.record_id,
        "kind": record.manifest.kind,
    }
    profile = record.manifest.profile
    if profile is not None:
        meta.update(
            zone_low_bpm=profile.zone_low_bpm,
            zone_high_bpm=profile.zone_high_bpm,
            hard_max_bpm=profile.hard_max_bpm,
            critical_bpm=profile.critical_bpm,
        )
    geometry = record.manifest.geometry
    if geometry is not None:
        meta.update(mapping(document(GEOMETRY, geometry)))
    final: dict[str, JsonValue] = {
        "type": "final",
        "end_reason": record.manifest.end_reason,
        "sim_state": "inconnu",
        "shaft_motor_rpm": None,
        "energised": None,
    }
    observation = record.manifest.end_observation
    if observation is not None:
        final.update(mapping(document(OBSERVATION, observation)))
        final["sim_state"] = observation.drive_state or "inconnu"
    warnings: list[JsonValue] = [
        {"type": "warning", "file": warning.file, "code": warning.code}
        for warning in record.warnings
    ]
    if geometry is None:
        meta["gear_ratio"] = "?"
        warnings.append(
            {"type": "warning", "file": "manifest.json", "code": "missing_viewer_geometry"}
        )
    if observation is None or any(
        value is None
        for value in (observation.drive_state, observation.shaft_motor_rpm, observation.energised)
    ):
        warnings.append(
            {"type": "warning", "file": "manifest.json", "code": "missing_final_drive_state"}
        )
    lines: list[JsonValue] = [
        meta,
        *(row.to_json() for row in record.rows),
        *(event.to_json() for event in record.events),
        final,
        *warnings,
    ]
    return Ok("".join(json.dumps(line, allow_nan=False) + "\n" for line in lines))
