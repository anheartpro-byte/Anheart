import csv
import io
import os
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, assert_never

from pydantic import TypeAdapter

from src.clock import Clock
from src.record.codec import IDENTIFIER, Privacy, document, encode, validate_manifest
from src.record.ecg import RawBlock, encode_block
from src.record.rows import JsonScalar, Row, finite
from src.record.schema import DriveFrame, EndObservation, Event, Manifest, RecordError
from src.record.storage import (
    checksums,
    failure_detail,
    file_handle,
    make_directory,
    private_root,
    private_supported,
    publish,
    sync,
    write_all,
)
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

    def __init__(
        self, path: Path, manifest: Manifest, privacy: Privacy, *, private: bool = False
    ) -> None:
        self.path: Path = path
        self.manifest: Manifest = manifest
        self.privacy: Privacy = privacy
        self.closed: bool = False
        self._private = private
        self._sensor_at: float | None = None
        self._failed: set[str] = set()
        self._dirty: set[str] = set()

    @classmethod
    def create(
        cls,
        root: Path,
        manifest: Manifest,
        privacy: Privacy = DEFAULT_PRIVACY,
        *,
        require_private: bool = False,
    ) -> Result["Writer", RecordError]:
        """Shared callers use portable storage; require_private opts into strict POSIX storage."""
        private = require_private
        if private and not private_supported():
            return Err(RecordError("create", "private_storage_unavailable"))
        try:
            validate_manifest(manifest, privacy)
            encoded = encode(document(MANIFEST, manifest), privacy)
            stamp = datetime.fromisoformat(manifest.started_at).strftime("%Y-%m-%dT%H%M%SZ")
            path = root / f"{stamp}_{manifest.session_id or manifest.local_ref}"
            private_root(root, private=private)
            make_directory(path, private=private)
            make_directory(path / "ecg_raw", private=private)
            publish(path / "manifest.json", (encoded + "\n").encode(), private=private)
            for name, content in (
                ("ticks.csv", csv_line(TICK_COLUMNS)),
                ("sensors.csv", csv_line(SENSOR_COLUMNS)),
                ("events.jsonl", ""),
                ("drive_frames.jsonl", ""),
            ):
                publish(path / name, content.encode(), private=private)
        except (OSError, ValueError) as error:
            return Err(RecordError("create", failure_detail(error)))
        return Ok(cls(path, manifest, privacy, private=private))

    def _append(self, name: str, content: str) -> Result[None, RecordError]:
        if self.closed:
            return Err(RecordError("append", "closed"))
        if name in self._failed:
            return Err(RecordError("append", "abandoned_stream"))
        try:
            with file_handle(
                self.path / name, os.O_RDWR | os.O_APPEND, private=self._private
            ) as handle:
                write_all(handle, content.encode("utf-8"))
        except OSError as error:
            self._failed.add(name)
            return Err(RecordError("append", failure_detail(error)))
        self._dirty.add(name)
        return Ok(None)

    def checkpoint(self) -> Result[None, RecordError]:
        try:
            for name in self._dirty:
                with file_handle(self.path / name, os.O_RDWR, private=self._private) as handle:
                    sync(handle)
        except OSError as error:
            return Err(RecordError("append", failure_detail(error)))
        self._dirty.clear()
        return Ok(None)

    def tick(self, row: Row) -> Result[None, RecordError]:
        try:
            line = tick_line(row, self.privacy)
        except ValueError as error:
            return Err(RecordError("tick", failure_detail(error)))
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
        if self.closed:
            return Err(RecordError("append", "closed"))
        try:
            payload = encode_block(block)
            publish(
                self.path / f"ecg_raw/{block.header.seq:06d}.bin.gz", payload, private=self._private
            )
        except (OSError, ValueError) as error:
            return Err(RecordError("raw", failure_detail(error)))
        return Ok(None)

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
            return Err(RecordError("sensors", failure_detail(error)))
        result = self._append("sensors.csv", "".join(lines))
        match result:
            case Ok():
                self._sensor_at = at
            case Err():
                return result
            case _ as unreachable:
                assert_never(unreachable)
        return result

    def close(
        self, clock: Clock, end_reason: str, observation: EndObservation | None = None
    ) -> Result[None, RecordError]:
        if self.closed:
            return Err(RecordError("close", "closed"))
        if self._failed:
            return Err(RecordError("close", "abandoned_stream"))
        match self.checkpoint():
            case Err(error):
                return Err(error)
            case Ok():
                pass
            case _ as unreachable:
                assert_never(unreachable)
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
            publish(
                self.path / "manifest.json",
                (encoded + "\n").encode(),
                replace=True,
                private=self._private,
            )
            checksums(self.path, private=self._private)
        except (OSError, ValueError, OverflowError) as error:
            return Err(RecordError("close", failure_detail(error)))
        self.manifest = manifest
        self.closed = True
        return Ok(None)
