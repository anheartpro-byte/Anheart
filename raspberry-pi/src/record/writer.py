import csv
import hashlib
import io
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Literal

from pydantic import TypeAdapter

from src.clock import Clock
from src.record.codec import IDENTIFIER, Privacy, document, encode, validate_manifest
from src.record.ecg import RawBlock, encode_block
from src.record.rows import JsonScalar, Row, finite
from src.record.schema import DriveFrame, EndObservation, Event, Manifest, RecordError
from src.result import Err, Ok, Result
from src.sensors.base import SensorReading

MANIFEST: Final[TypeAdapter[Manifest]] = TypeAdapter(Manifest)
EVENT: Final[TypeAdapter[Event]] = TypeAdapter(Event)
FRAME: Final[TypeAdapter[DriveFrame]] = TypeAdapter(DriveFrame)
TICK_COLUMNS: Final = tuple(field.name for field in fields(Row))
SENSOR_COLUMNS: Final = ("t", "channel", "quality", "metric", "value", "unit")
DEFAULT_PRIVACY: Final = Privacy()
CADENCE_EPSILON: Final = 1e-9


def csv_line(values: tuple[JsonScalar, ...]) -> str:
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerow(values)
    return stream.getvalue()


def tick_line(row: Row, privacy: Privacy) -> str:
    encoded = row.to_json()
    encode(encoded, privacy)
    values = tuple(encoded[name] for name in TICK_COLUMNS)
    return csv_line(
        tuple(
            "" if value is None else privacy.text(str(value)).replace("\r", " ").replace("\n", " ")
            for value in values
        )
    )


class Writer:
    """Mutable single-owner append writer. All I/O failures are returned as Result."""

    def __init__(self, path: Path, manifest: Manifest, privacy: Privacy) -> None:
        self.path: Path = path
        self.manifest: Manifest = manifest
        self.privacy: Privacy = privacy
        self.closed: bool = False
        self._sensor_at: float | None = None

    @classmethod
    def create(
        cls, root: Path, manifest: Manifest, privacy: Privacy = DEFAULT_PRIVACY
    ) -> Result["Writer", RecordError]:
        try:
            validate_manifest(manifest, privacy)
            encoded = encode(document(MANIFEST, manifest), privacy)
            stamp = datetime.fromisoformat(manifest.started_at).strftime("%Y-%m-%dT%H%M%SZ")
            path = root / f"{stamp}_{manifest.session_id or manifest.local_ref}"
            path.mkdir(parents=True, exist_ok=False)
            (path / "ecg_raw").mkdir()
            (path / "manifest.json").write_text(encoded + "\n", encoding="utf-8")
            (path / "ticks.csv").write_text(csv_line(TICK_COLUMNS), encoding="utf-8")
            (path / "sensors.csv").write_text(csv_line(SENSOR_COLUMNS), encoding="utf-8")
            (path / "events.jsonl").touch()
            (path / "drive_frames.jsonl").touch()
        except (OSError, ValueError) as error:
            return Err(RecordError("create", type(error).__name__))
        return Ok(cls(path, manifest, privacy))

    def _append(self, name: str, content: str) -> Result[None, RecordError]:
        return self._store(name, content.encode("utf-8"), "ab")

    def _store(
        self, name: str, content: bytes, mode: Literal["ab", "xb"]
    ) -> Result[None, RecordError]:
        if self.closed:
            return Err(RecordError("append", "closed"))
        try:
            with (self.path / name).open(mode) as handle:
                handle.write(content)
        except OSError as error:
            return Err(RecordError("append", type(error).__name__))
        return Ok(None)

    def tick(self, row: Row) -> Result[None, RecordError]:
        try:
            line = tick_line(row, self.privacy)
        except ValueError as error:
            return Err(RecordError("tick", type(error).__name__))
        return self._append("ticks.csv", line)

    def event(self, event: Event) -> Result[None, RecordError]:
        if (
            IDENTIFIER.fullmatch(event.actor) is None
            or self.privacy.text(event.actor) != event.actor
        ):
            return Err(RecordError("event", "invalid_actor"))
        return self._append("events.jsonl", encode(document(EVENT, event), self.privacy) + "\n")

    def frame(self, frame: DriveFrame) -> Result[None, RecordError]:
        return self._append(
            "drive_frames.jsonl", encode(document(FRAME, frame), self.privacy) + "\n"
        )

    def raw(self, block: RawBlock) -> Result[None, RecordError]:
        try:
            payload = encode_block(block)
        except ValueError as error:
            return Err(RecordError("raw", type(error).__name__))
        return self._store(f"ecg_raw/{block.header.seq:06d}.bin.gz", payload, "xb")

    def sensors(self, at: float, readings: tuple[SensorReading, ...]) -> Result[None, RecordError]:
        if self._sensor_at is not None and at - self._sensor_at < 1.0 - CADENCE_EPSILON:
            return Ok(None)
        lines: list[str] = []
        try:
            finite(at)
            for reading in readings:
                prefix = (at, reading.kind.value, reading.quality.value)
                if not reading.metrics:
                    lines.append(csv_line((*prefix, "", None, "")))
                for metric in reading.metrics:
                    value = None if metric.value is None else finite(metric.value)
                    key = self.privacy.text(metric.key).replace("\r", " ").replace("\n", " ")
                    unit = self.privacy.text(metric.unit).replace("\r", " ").replace("\n", " ")
                    lines.append(csv_line((*prefix, key, value, unit)))
        except ValueError as error:
            return Err(RecordError("sensors", type(error).__name__))
        result = self._append("sensors.csv", "".join(lines))
        if isinstance(result, Ok):
            self._sensor_at = at
        return result

    def close(
        self, clock: Clock, end_reason: str, observation: EndObservation | None = None
    ) -> Result[None, RecordError]:
        if self.closed:
            return Err(RecordError("close", "closed"))
        try:
            ended = (
                datetime.fromtimestamp(clock.unix_millis() / 1000, UTC)
                .isoformat()
                .replace("+00:00", "Z")
            )
            manifest = replace(
                self.manifest,
                ended_at=ended,
                end_reason=self.privacy.text(end_reason),
                end_observation=observation,
            )
            encoded = encode(document(MANIFEST, manifest), self.privacy)
            with TemporaryDirectory(prefix=".record-", dir=self.path.parent) as temporary:
                final_manifest = Path(temporary) / "manifest.json"
                final_manifest.write_text(encoded + "\n", encoding="utf-8")
                final_manifest.replace(self.path / "manifest.json")
            paths = sorted(path for path in self.path.rglob("*") if path.is_file())
            checksums = "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(self.path).as_posix()}\n"
                for path in paths
            )
            with (self.path / "checksums.sha256").open("x", encoding="utf-8") as handle:
                handle.write(checksums)
        except (OSError, ValueError) as error:
            return Err(RecordError("close", type(error).__name__))
        self.manifest = manifest
        self.closed = True
        return Ok(None)
