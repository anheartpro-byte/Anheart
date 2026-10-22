"""Private, bounded configuration documents. Disk work belongs to the journal thread."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from src.record.writer import (
    DIRECTORY_SYNC,
    PRIVATE_DIRECTORY,
    create_private,
    describe_os_error,
    fsync_path,
)

DIRECTORY: Final[str] = "config"
MAX_CONFIG_BYTES: Final[int] = 64 * 1024
WARNING_PREFIX: Final[str] = "replay_config:"


def persist(root: Path, document: bytes) -> str | None:
    """Keep one immutable document per hash; return a machine-only warning on failure.

    An existing different file is never replaced. Reads are bounded too. A
    failed write may leave a partial file: it will be refused, never silently
    repaired. This function must never be called by the control loop.
    """
    if len(document) > MAX_CONFIG_BYTES:
        return f"{WARNING_PREFIX} document_exceeds_limit"
    directory = root / DIRECTORY
    path = directory / (hashlib.sha256(document).hexdigest() + ".json")
    try:
        directory.mkdir(mode=PRIVATE_DIRECTORY, parents=True, exist_ok=True)
        directory.chmod(PRIVATE_DIRECTORY)
        try:
            create_private(path, document)
        except FileExistsError:
            with path.open("rb") as handle:
                existing = handle.read(MAX_CONFIG_BYTES + 1)
            if existing != document:
                return f"{WARNING_PREFIX} existing_document_differs"
            return None
        fsync_path(path)
        if DIRECTORY_SYNC:
            fsync_path(directory)
    except OSError as error:
        return f"{WARNING_PREFIX} {describe_os_error(error)}"
    return None
