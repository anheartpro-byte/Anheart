import csv
import hashlib
import io
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from src.record.codec import IDENTIFIER, Privacy, validate_manifest
from src.record.ecg import RawBlock, decode_block
from src.record.rows import Row, finite
from src.record.schema import DriveFrame, Event, Manifest, ReadWarning, RecordError
from src.record.writer import EVENT, FRAME, MANIFEST, SENSOR_COLUMNS, TICK_COLUMNS
from src.result import Err, Ok, Result

ROW: Final[TypeAdapter[Row]] = TypeAdapter(Row)
CSV_TEXT_COLUMNS: Final = frozenset(
    {
        "state",
        "mode",
        "phase",
        "drive_state",
        "sim_state",
        "safety_action",
        "hr_quality",
        "channel",
        "quality",
        "metric",
        "unit",
    }
)


@dataclass(frozen=True, slots=True)
class SensorRow:
    t: float
    channel: str
    quality: str
    metric: str
    value: float | None
    unit: str


SENSOR: Final[TypeAdapter[SensorRow]] = TypeAdapter(SensorRow)


@dataclass(frozen=True, slots=True)
class Recording:
    manifest: Manifest
    rows: tuple[Row, ...]
    events: tuple[Event, ...]
    frames: tuple[DriveFrame, ...]
    raw: tuple[RawBlock, ...]
    sensors: tuple[SensorRow, ...]
    warnings: tuple[ReadWarning, ...]


def complete_lines(path: Path, warnings: list[ReadWarning]) -> list[str]:
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        warnings.append(ReadWarning(path.name, "truncated"))
        raw = raw[: raw.rfind(b"\n") + 1]
    return raw.decode("utf-8").splitlines()


def _jsonl[T](path: Path, adapter: TypeAdapter[T], warnings: list[ReadWarning]) -> tuple[T, ...]:
    return tuple(adapter.validate_json(line) for line in complete_lines(path, warnings))


def _csv[T](
    path: Path, adapter: TypeAdapter[T], names: tuple[str, ...], warnings: list[ReadWarning]
) -> tuple[T, ...]:
    lines = complete_lines(path, warnings)
    if not lines:
        return ()
    reader = csv.reader(io.StringIO("\n".join(lines)))
    if tuple(next(reader)) != names:
        raise ValueError("CSV columns do not match schema 2")
    result: list[T] = []
    for row in reader:
        if len(row) != len(names):
            raise ValueError("CSV field count does not match header")
        values = {
            name: (None if value == "" and name not in CSV_TEXT_COLUMNS else value)
            for name, value in zip(names, row, strict=True)
        }
        result.append(adapter.validate_json(json.dumps(values)))
    return tuple(result)


def _checksums(path: Path, warnings: list[ReadWarning]) -> None:
    checksum = path / "checksums.sha256"
    if not checksum.exists():
        warnings.append(ReadWarning(checksum.name, "missing_checksums"))
        return
    indexed: set[str] = set()
    for line in complete_lines(checksum, warnings):
        digest, name = line.split("  ", 1)
        member = path / name
        if member.resolve().is_relative_to(path.resolve()) and member.is_file():
            actual = hashlib.sha256(member.read_bytes()).hexdigest()
            if actual != digest:
                warnings.append(ReadWarning(name, "checksum_mismatch"))
        else:
            warnings.append(ReadWarning(name, "missing_file"))
        indexed.add(name)
    for member in path.rglob("*"):
        name = member.relative_to(path).as_posix()
        if member.is_file() and name != "checksums.sha256" and name not in indexed:
            warnings.append(ReadWarning(name, "checksum_mismatch"))


def read(path: Path) -> Result[Recording, RecordError]:
    warnings: list[ReadWarning] = []
    try:
        manifest = MANIFEST.validate_json((path / "manifest.json").read_bytes())
        validate_manifest(manifest, Privacy())
        _checksums(path, warnings)
        rows = _csv(path / "ticks.csv", ROW, TICK_COLUMNS, warnings)
        for row in rows:
            row.to_json()
        sensors = _csv(path / "sensors.csv", SENSOR, SENSOR_COLUMNS, warnings)
        for sensor in sensors:
            finite(sensor.t)
            if sensor.value is not None:
                finite(sensor.value)
        events = _jsonl(path / "events.jsonl", EVENT, warnings)
        if any(IDENTIFIER.fullmatch(event.actor) is None for event in events):
            return Err(RecordError("read", "invalid_actor"))
        recording = Recording(
            manifest,
            rows,
            events,
            _jsonl(path / "drive_frames.jsonl", FRAME, warnings),
            _raw(path / "ecg_raw", warnings),
            sensors,
            tuple(warnings),
        )
    except (OSError, ValueError, EOFError, zlib.error) as error:
        return Err(RecordError("read", type(error).__name__))
    return Ok(recording)


def _raw(path: Path, warnings: list[ReadWarning]) -> tuple[RawBlock, ...]:
    blocks: list[RawBlock] = []
    for member in sorted(path.glob("*.bin.gz")):
        raw = member.read_bytes()
        if not raw:
            warnings.append(ReadWarning(member.name, "truncated"))
            continue
        try:
            blocks.append(decode_block(raw))
        except EOFError:
            warnings.append(ReadWarning(member.name, "truncated"))
    return tuple(blocks)
