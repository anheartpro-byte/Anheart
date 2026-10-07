import csv
import errno
import hashlib
import io
import os
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
DIRECTORY_SYNC: Final = os.name == "posix"
"""Whether a directory can be opened and fsynced here (not on Windows)."""


def describe_os_error(error: OSError) -> str:
    """``OSError:ENOSPC``: the class and the errno's name, never the path or the message."""
    if error.errno is None:
        return type(error).__name__
    return f"{type(error).__name__}:{errno.errorcode.get(error.errno, str(error.errno))}"


def describe_failure(error: OSError | ValueError) -> str:
    """What refused: an errno for the disk, the class alone for a value the format refuses."""
    return describe_os_error(error) if isinstance(error, OSError) else type(error).__name__


def write_file(path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
    """Append to a stream, or create a block that must not exist yet. Raises ``OSError``."""
    with path.open(mode) as handle:
        handle.write(content)


def fsync_path(path: Path) -> None:
    """Flush one file or directory to the device. Raises ``OSError``."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    """Mutable single-owner append writer. All I/O failures are returned as Result.

    Nothing here is flushed to the device until :meth:`sync` is called, and
    nothing here may be called from the control loop: on the console the one
    owner is the journal thread of :mod:`src.record.journal`.
    """

    def __init__(self, path: Path, manifest: Manifest, privacy: Privacy) -> None:
        self.path: Path = path
        self.manifest: Manifest = manifest
        self.privacy: Privacy = privacy
        self.closed: bool = False
        self._sensor_at: float | None = None
        # Mutable, owned by the one caller. The length of each append stream
        # after its last complete line: a refused write is cut back to it, so
        # a torn line never ends up in the middle of a file.
        self._lengths: dict[str, int] = {}
        # Streams whose torn tail could not be cut back: never appended again.
        self._abandoned: set[str] = set()
        # Files written since the last successful :meth:`sync`.
        self._unsynced: set[str] = set()

    @classmethod
    def create(
        cls, root: Path, manifest: Manifest, privacy: Privacy = DEFAULT_PRIVACY
    ) -> Result["Writer", RecordError]:
        try:
            validate_manifest(manifest, privacy)
            encoded = encode(document(MANIFEST, manifest), privacy)
            stamp = datetime.fromisoformat(manifest.started_at).strftime("%Y-%m-%dT%H%M%SZ")
            path = root / f"{stamp}_{manifest.session_id or manifest.local_ref}"
            headers = {
                "ticks.csv": csv_line(TICK_COLUMNS).encode("utf-8"),
                "sensors.csv": csv_line(SENSOR_COLUMNS).encode("utf-8"),
                "events.jsonl": b"",
                "drive_frames.jsonl": b"",
            }
            path.mkdir(parents=True, exist_ok=False)
            (path / "ecg_raw").mkdir()
            (path / "manifest.json").write_text(encoded + "\n", encoding="utf-8")
            for name, header in headers.items():
                (path / name).write_bytes(header)
        except (OSError, ValueError) as error:
            return Err(RecordError("create", describe_failure(error)))
        writer = cls(path, manifest, privacy)
        writer._lengths = {name: len(header) for name, header in headers.items()}
        writer._unsynced = {"manifest.json", *headers}
        return Ok(writer)

    def _append(self, name: str, content: str) -> Result[None, RecordError]:
        return self._store(name, content.encode("utf-8"), "ab")

    def _store(
        self, name: str, content: bytes, mode: Literal["ab", "xb"]
    ) -> Result[None, RecordError]:
        if self.closed:
            return Err(RecordError("append", "closed"))
        if name in self._abandoned:
            return Err(RecordError("append", "abandoned"))
        try:
            write_file(self.path / name, content, mode)
        except FileExistsError as error:
            # A block is never replaced: what is already there stays as it is.
            return Err(RecordError("append", describe_os_error(error)))
        except OSError as error:
            self._cut_back(name)
            return Err(RecordError("append", describe_os_error(error)))
        if name in self._lengths:
            self._lengths[name] += len(content)
        self._unsynced.add(name)
        return Ok(None)

    def _cut_back(self, name: str) -> None:
        """Remove what a refused write may have left: a torn line, or a partial block.

        A disk that fills up mid-line leaves half a line. Appending after it
        would put that half in the middle of the file, where the reader refuses
        the whole recording; cut back to the last complete line, the stream can
        go on once the disk accepts writes again. When even that is refused the
        stream is abandoned: its torn tail stays at the end, which the reader
        reports as ``truncated`` and reads up to.
        """
        target = self.path / name
        length = self._lengths.get(name)
        try:
            if length is None:
                target.unlink(missing_ok=True)
            else:
                os.truncate(target, length)
        except OSError:
            self._abandoned.add(name)

    def sync(self, *, directories: bool = DIRECTORY_SYNC) -> Result[None, RecordError]:
        """fsync every file written since the last call, and the directories holding them.

        Allowed on a closed writer: the final manifest and the checksums are
        written by :meth:`close` and flushed by the next call here. A file that
        could not be flushed stays owed to the next call.
        """
        names = sorted(self._unsynced)
        folders = sorted({(self.path / name).parent for name in names}) if directories else []
        try:
            for name in names:
                fsync_path(self.path / name)
            for folder in folders:
                fsync_path(folder)
        except OSError as error:
            return Err(RecordError("sync", describe_os_error(error)))
        self._unsynced.difference_update(names)
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
            return Err(RecordError("close", describe_failure(error)))
        self.manifest = manifest
        self.closed = True
        self._unsynced.update(("manifest.json", "checksums.sha256"))
        return Ok(None)
