"""Local retention of session records: what may be purged, and what never is.

A record stays on the Pi's disk until BOTH are true:

* it was deposited off the machine and that deposit was **confirmed**: a
  marker file written by :func:`confirm_deposit` sits NEXT TO the record
  directory (``<record>.deposit.json``, see :func:`marker_of`), bound to the
  exact content that was deposited. Next to it and not inside: the record's
  own files are the format's, and its checksums index every one of them;
* the confirmation is older than the configured retention
  (``RECORD_LOCAL_RETENTION_DAYS``, 30 by default).

**Nothing in this repository writes the marker yet.** The deposit itself
(Convex Storage) is another ticket, waiting on a legal opinion; until it calls
:func:`confirm_deposit`, :func:`purge` finds nothing to remove and every record
stays where it is. That is the whole point of the default: a record that was
not deposited is never purged automatically, whatever its age and however full
the disk is.

The marker is refused, and the record kept, whenever anything about it is in
doubt: unreadable or malformed, of another version, about a record still open
(no ``checksums.sha256``), or about a content that is not the one on disk
(its ``checksum`` is the SHA-256 of ``checksums.sha256`` at confirmation, so a
record modified afterwards is not "the deposited one" any more).

Everything here touches the disk: it runs on the journal thread, between
sessions (:mod:`src.record.journal`), never on the event loop.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass as validated

from src.record.codec import IDENTIFIER
from src.record.schema import RecordError
from src.record.writer import create_private, describe_os_error
from src.result import Err, Ok, Result
from src.units import UnixMillis

MARKER_SUFFIX: Final[str] = ".deposit.json"
"""The "deposited and confirmed" marker is ``<record directory name>.deposit.json``."""

CHECKSUMS: Final[str] = "checksums.sha256"

RECORD_NAME: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z_[A-Za-z0-9_-]+")
"""A record directory, as the writer names it. Nothing else under the root is ever removed."""

MILLIS_PER_DAY: Final[int] = 86_400_000


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class Deposit:
    """The confirmation of one deposit. Written once, by :func:`confirm_deposit`."""

    schema_version: Literal[1]
    storage_id: str
    """The opaque identifier the remote storage gave the deposited record."""

    confirmed_at: str
    """When the remote side confirmed it holds the record: ISO 8601 UTC, ``Z``."""

    checksum: str
    """SHA-256 of the record's ``checksums.sha256`` as it was when deposited."""


_DEPOSIT: Final[TypeAdapter[Deposit]] = TypeAdapter(Deposit)


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """What one purge did. ``kept`` counts records left alone, for whatever reason."""

    removed: tuple[str, ...]
    kept: int
    failed: tuple[str, ...]
    """Records that were due but could not be removed (they stay, and are tried again)."""


def records(root: Path) -> tuple[Path, ...]:
    """Every record directory directly under ``root``, oldest first. Raises ``OSError``."""
    return tuple(
        sorted(
            path
            for path in root.iterdir()
            if RECORD_NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
        )
    )


def marker_of(record: Path) -> Path:
    """Where the confirmation of ``record``'s deposit is, or would be."""
    return record.with_name(record.name + MARKER_SUFFIX)


def _content_checksum(record: Path) -> str:
    return hashlib.sha256((record / CHECKSUMS).read_bytes()).hexdigest()


def _stamp(at: UnixMillis) -> str:
    return datetime.fromtimestamp(at / 1000, UTC).isoformat().replace("+00:00", "Z")


def confirm_deposit(record: Path, storage_id: str, at: UnixMillis) -> Result[None, RecordError]:
    """Mark ``record`` as deposited and confirmed at ``at``. For the deposit step, and only it.

    Refused for a record that is still open (no checksums: there is no fixed
    content to have deposited), for an identifier that is not opaque, and for
    a record already marked: a confirmation is written once.
    """
    if IDENTIFIER.fullmatch(storage_id) is None:
        return Err(RecordError("close", "invalid_storage_id"))
    try:
        deposit = Deposit(
            schema_version=1,
            storage_id=storage_id,
            confirmed_at=_stamp(at),
            checksum=_content_checksum(record),
        )
        create_private(marker_of(record), _DEPOSIT.dump_json(deposit) + b"\n")
    except OSError as error:
        return Err(RecordError("close", describe_os_error(error)))
    return Ok(None)


def confirmed_deposit(record: Path) -> Deposit | None:
    """The valid confirmation ``record`` carries, or ``None``: no doubt is given the benefit."""
    try:
        deposit = _DEPOSIT.validate_json(marker_of(record).read_bytes())
        confirmed = datetime.fromisoformat(deposit.confirmed_at)
        current = _content_checksum(record)
    except (OSError, ValueError, ValidationError):
        return None
    if not deposit.confirmed_at.endswith("Z") or confirmed.utcoffset() is None:
        return None
    return deposit if deposit.checksum == current else None


def purgeable(record: Path, now: UnixMillis, retention_days: int) -> bool:
    """Whether ``record`` was deposited, confirmed, and kept for the whole retention since."""
    deposit = confirmed_deposit(record)
    if deposit is None:
        return False
    confirmed_ms = datetime.fromisoformat(deposit.confirmed_at).timestamp() * 1000
    return now - confirmed_ms >= retention_days * MILLIS_PER_DAY


def purge(
    root: Path,
    now: UnixMillis,
    retention_days: int,
    pulse: Callable[[], None] | None = None,
) -> PurgeReport:
    """Remove every record under ``root`` that :func:`purgeable` allows. Never raises.

    The record goes first and its marker after: stopped in between, what is
    left is a marker with no record, which the next purge removes. The other
    order could leave a deposited record with no marker, kept for ever.

    ``pulse`` is called after each record looked at: removing a long session is
    thousands of files, and the caller has periodic work of its own.
    """
    try:
        candidates = records(root)
        markers = tuple(root.glob(f"*{MARKER_SUFFIX}"))
    except OSError:
        return PurgeReport(removed=(), kept=0, failed=())
    removed: list[str] = []
    failed: list[str] = []
    kept = 0
    for record in candidates:
        if purgeable(record, now, retention_days):
            try:
                shutil.rmtree(record)
            except OSError:
                failed.append(record.name)
            else:
                removed.append(record.name)
        else:
            kept += 1
        if pulse is not None:
            pulse()
    for marker in markers:
        if not marker.with_name(marker.name.removesuffix(MARKER_SUFFIX)).exists():
            _forget(marker)
    return PurgeReport(removed=tuple(removed), kept=kept, failed=tuple(failed))


def _forget(marker: Path) -> None:
    """Remove the marker of a record that is gone. Left for the next purge if refused."""
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        return
