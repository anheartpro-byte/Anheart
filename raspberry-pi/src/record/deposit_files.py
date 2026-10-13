from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, NewType

from src.record.containment import resolve_under
from src.record.cursor import Cursor, load, store
from src.record.retention import confirm_deposit, confirmed_deposit, records
from src.record.schema import RecordError
from src.record.writer import MANIFEST, describe_failure
from src.result import Err, Ok, Result
from src.units import UnixMillis

ByteCount = NewType("ByteCount", int)
Sha256 = NewType("Sha256", str)
MAX_FILE_BYTES: Final[ByteCount] = ByteCount(16 * 1024 * 1024)
MAX_FILES: Final[int] = 20_000
MAX_PATH_LENGTH: Final[int] = 160
SAFE_PATH: Final[re.Pattern[str]] = re.compile(
    r"(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+"
)


@dataclass(frozen=True, slots=True)
class DepositRecord:
    path: Path
    session_id: str
    files: tuple[str, ...]
    checksums: Mapping[str, Sha256]


@dataclass(frozen=True, slots=True)
class FilePayload:
    path: str
    sha256: Sha256
    size: ByteCount
    content: bytes
    storage_id: str | None


def next_record(root: Path) -> Result[DepositRecord | None, RecordError]:
    try:
        for path in records(root):
            if confirmed_deposit(path) is not None:
                continue
            cursor = load(path)
            if not isinstance(cursor, Cursor) or cursor.state != "complete":
                continue
            if cursor.session_id is None or cursor.end != "sent":
                continue
            manifest = MANIFEST.validate_json((path / "manifest.json").read_bytes())
            if manifest.local_ref != cursor.local_ref or manifest.ended_at is None:
                continue
            files = manifest.files
            if files is None or not files or len(files) > MAX_FILES:
                continue
            if len(set(files)) != len(files) or any(
                len(name) > MAX_PATH_LENGTH or SAFE_PATH.fullmatch(name) is None for name in files
            ):
                return Err(RecordError("read", "invalid_deposit_manifest"))
            checksums: dict[str, Sha256] = {}
            for line in (path / "checksums.sha256").read_text(encoding="ascii").splitlines():
                digest, name = line.split("  ", 1)
                if not re.fullmatch(r"[a-f0-9]{64}", digest) or name in checksums:
                    return Err(RecordError("read", "invalid_checksums"))
                checksums[name] = Sha256(digest)
            if set(checksums) != set(files) - {"checksums.sha256"}:
                return Err(RecordError("read", "incomplete_checksums"))
            return Ok(DepositRecord(path, cursor.session_id, files, checksums))
    except (OSError, ValueError) as error:
        return Err(RecordError("read", describe_failure(error)))
    return Ok(None)


def payload(record: DepositRecord, name: str) -> Result[FilePayload, RecordError]:
    if name not in record.files:
        return Err(RecordError("read", "unexpected_file"))
    path = resolve_under(record.path, name)
    if path is None:
        return Err(RecordError("read", "file_outside_record"))
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            return Err(RecordError("read", "file_exceeds_upload_limit"))
        digest = Sha256(hashlib.sha256(content).hexdigest())
        if name != "checksums.sha256" and record.checksums.get(name) != digest:
            return Err(RecordError("read", "local_checksum_mismatch"))
        cursor = load(record.path)
        storage_id = cursor.record_uploads.get(name) if isinstance(cursor, Cursor) else None
    except (OSError, ValueError) as error:
        return Err(RecordError("read", describe_failure(error)))
    return Ok(FilePayload(name, digest, ByteCount(len(content)), content, storage_id))


def remember(record: DepositRecord, name: str, storage_id: str) -> Result[None, RecordError]:
    cursor = load(record.path)
    if not isinstance(cursor, Cursor) or cursor.session_id != record.session_id:
        return Err(RecordError("read", "deposit_cursor_unavailable"))
    uploads = dict(cursor.record_uploads)
    uploads[name] = storage_id
    saved = store(record.path, replace(cursor, record_uploads=uploads))
    if isinstance(saved, Err):
        return Err(RecordError("sync", saved.error.detail))
    return Ok(None)


def acknowledge(record: DepositRecord, verified: frozenset[str]) -> Result[None, RecordError]:
    cursor = load(record.path)
    if not isinstance(cursor, Cursor) or cursor.session_id != record.session_id:
        return Err(RecordError("read", "deposit_cursor_unavailable"))
    pending = {
        name: storage_id
        for name, storage_id in cursor.record_uploads.items()
        if name not in verified
    }
    if len(pending) == len(cursor.record_uploads):
        return Ok(None)
    saved = store(record.path, replace(cursor, record_uploads=pending))
    return Err(RecordError("sync", saved.error.detail)) if isinstance(saved, Err) else Ok(None)


def confirm(
    record: DepositRecord, verified: dict[str, str], at: UnixMillis
) -> Result[None, RecordError]:
    expected = record.files
    if not expected or set(expected) != set(verified):
        return Err(RecordError("close", "incomplete_deposit"))
    for name in expected:
        read = payload(record, name)
        if isinstance(read, Err):
            return read
        if read.value.sha256 != verified[name]:
            return Err(RecordError("close", "deposit_checksum_mismatch"))
    return confirm_deposit(record.path, record.session_id, at)
