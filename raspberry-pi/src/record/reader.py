import csv
import hashlib
import io
import json
import stat
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from src.record.codec import ACTOR, Privacy, validate_manifest
from src.record.ecg import (
    MAX_BLOCK_HEADER_BYTES,
    MAX_RAW_BLOCK_BYTES,
    SAMPLE_RATE,
    RawBlock,
    decode_block,
)
from src.record.journal import RAW_BLOCKS_PER_SECOND
from src.record.rows import Row, finite
from src.record.schema import DriveFrame, Event, Manifest, ReadWarning, RecordError
from src.record.writer import EVENT, FRAME, MANIFEST, SENSOR_COLUMNS, TICK_COLUMNS
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind

ROW: Final[TypeAdapter[Row]] = TypeAdapter(Row)

LONGEST_RECORD_S: Final[int] = 2 * 3600 + 10 * 60
"""The longest record read: a programme of two hours, the longest the deposit plans for
(``docs/enregistrement.md``, its 36 000 raw blocks), and ten minutes for the eight seconds
before the start, the descent and the monitored recovery."""

RAW_HEADER_ALLOWANCE: Final[int] = 512
"""Bytes allowed for a raw block's header line: 235 for six channels with both delivery
instants written in full (a newline included), here a little over twice that."""

MAX_RAW_BLOCKS: Final[int] = RAW_BLOCKS_PER_SECOND * LONGEST_RECORD_S
"""39 000 raw blocks: one per acquisition batch, five a second."""

MAX_RAW_BYTES: Final[int] = MAX_RAW_BLOCKS * (
    SAMPLE_RATE // RAW_BLOCKS_PER_SECOND * len(SensorKind) * 2 + RAW_HEADER_ALLOWANCE
)
"""What those blocks may hold decompressed: 39 000 blocks of 0.2 s, each 200 samples of
six channels at two bytes (2 400 bytes) and its header line (512 allowed), 39 000 x 2 912
= 113 568 000 bytes. A real six-channel record of 2 h 10 min holds 39 000 x 2 635, about
102.8 MB.

The reader keeps every decoded block, and a decoded block takes at most about twenty
times its bytes in memory (16.6 measured): a record at this bound needs at most about
2.3 GB for its ECG, as a real one of that length does. With the files of
:data:`FILE_BOUNDS`, at most about 4 GB for the whole record."""

GZIP_SIZE_BYTES: Final[int] = 4
"""The trailer field of a gzip member that gives its decompressed size (ISIZE)."""

LARGEST_BLOCK_FILE: Final[int] = MAX_BLOCK_HEADER_BYTES + MAX_RAW_BLOCK_BYTES + 64 * 1024
"""The largest a raw block's file can be: what it can hold, and room for gzip's overhead,
which on data that does not compress is a few bytes per 16 KiB."""

FILE_BOUNDS: Final[Mapping[str, int]] = {
    "manifest.json": 2 * 1024 * 1024,
    "checksums.sha256": (MAX_RAW_BLOCKS + 16) * 128,
    "ticks.csv": LONGEST_RECORD_S * 5 * 512,
    "sensors.csv": LONGEST_RECORD_S * len(SensorKind) * 128,
    "events.jsonl": LONGEST_RECORD_S * 20 * 512,
    "drive_frames.jsonl": LONGEST_RECORD_S * 128 * 256,
}
"""The files the reader reads whole, each no larger than the longest record makes it.

* ``manifest.json``: 2 MiB, what the deposit accepts;
* ``checksums.sha256``: a line of at most 128 bytes (87 for a block) per block and per
  other file, 4 994 048 bytes;
* ``ticks.csv``: five rows a second, 512 bytes allowed each (344 is the longest written,
  every column filled), 19 968 000 bytes;
* ``sensors.csv``: a row a second for each of the six channels, 128 bytes allowed each (61
  the longest written), 5 990 400 bytes;
* ``events.jsonl``: twenty events a second, 512 bytes allowed each (126 the longest
  written; a console makes about five a second at its busiest), 79 872 000 bytes;
* ``drive_frames.jsonl``: 128 frames a second, 256 bytes allowed each (106 the longest
  written): the native driver makes about 30 a second, and about 120 with the SDK's
  transport calls kept, the bench diagnostic of the link; 255 590 400 bytes.

Each over 7 800 s (:data:`LONGEST_RECORD_S`). Read, a tick or a sensor row takes at most
about ten times its bytes in memory, an event or a frame about four: at these bounds,
about 200, 60, 320 and 1 020 MB, and the ECG about 2 300 (:data:`MAX_RAW_BYTES`), at most
about 4 GB in all. A real record of this length without the link's diagnostic takes
much less."""

RAW_BLOCKS_ABOVE_LONGEST_RECORD: Final[str] = "raw_blocks_above_longest_record"
RAW_BYTES_ABOVE_LONGEST_RECORD: Final[str] = "raw_bytes_above_longest_record"
FILE_ABOVE_ITS_BOUND: Final[str] = "file_above_its_bound"
NOT_A_REGULAR_FILE: Final[str] = "not_a_regular_file"
"""Why ``read`` refuses a record before reading it, as the ``detail`` of its error."""

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
            # Hashed as it is read, a file the reader keeps nothing of is never held whole.
            with member.open("rb") as content:
                actual = hashlib.file_digest(content, "sha256").hexdigest()
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
        # Before one byte is read: the reader keeps what it reads, all of it.
        outside = _outside_bounds(path)
        if outside is not None:
            return Err(RecordError("read", outside))
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
        if any(ACTOR.fullmatch(event.actor) is None for event in events):
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


def _inflated_size(block: Path, size: int) -> int:
    """The size a gzip block of ``size`` bytes says it holds decompressed (its last four).

    Counted for no more than a block can hold: a block cut short while it was being
    written ends in other bytes than its trailer, and is read as ``truncated``. A block
    whose trailer lies fails its own check when it is decoded (``inflate``): a block
    that is read holds what it says.
    """
    if size < GZIP_SIZE_BYTES:
        return 0
    with block.open("rb") as content:
        content.seek(size - GZIP_SIZE_BYTES)
        said = int.from_bytes(content.read(GZIP_SIZE_BYTES), "little")
    return min(said, MAX_BLOCK_HEADER_BYTES + MAX_RAW_BLOCK_BYTES)


def _outside_bounds(path: Path) -> str | None:
    """Why the files ``read`` reads whole are not what a record holds, or ``None``.

    From what the file system says, before a byte is read: each a regular file, never a
    link, a pipe or a device (a folder given as it is, not extracted, can hold one), and
    no larger than its bound (:data:`FILE_BOUNDS`), then the raw blocks.
    """
    for name, bound in FILE_BOUNDS.items():
        try:
            status = (path / name).lstat()
        except FileNotFoundError:
            continue  # read as missing, as ever
        if not stat.S_ISREG(status.st_mode):
            return NOT_A_REGULAR_FILE
        if status.st_size > bound:
            return FILE_ABOVE_ITS_BOUND
    return _raw_above_longest_record(path / "ecg_raw")


def _raw_above_longest_record(path: Path) -> str | None:
    """Why the raw blocks under ``path`` hold more than the longest record, or ``None``."""
    blocks = sorted(path.glob("*.bin.gz"))
    if len(blocks) > MAX_RAW_BLOCKS:
        return RAW_BLOCKS_ABOVE_LONGEST_RECORD
    sizes = [block.lstat() for block in blocks]
    if any(not stat.S_ISREG(status.st_mode) for status in sizes):
        return NOT_A_REGULAR_FILE
    if any(status.st_size > LARGEST_BLOCK_FILE for status in sizes):
        return FILE_ABOVE_ITS_BOUND
    said = sum(
        _inflated_size(block, status.st_size) for block, status in zip(blocks, sizes, strict=True)
    )
    if said > MAX_RAW_BYTES:
        return RAW_BYTES_ABOVE_LONGEST_RECORD
    return None


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
