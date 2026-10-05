from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from re import compile as compile_pattern
from typing import Final, NewType

from src.record.codec import IDENTIFIER
from src.result import Err, Ok, Result

MachineId = NewType("MachineId", str)
OrganizationId = NewType("OrganizationId", str)
LocalReference = NewType("LocalReference", str)
RetentionDays = NewType("RetentionDays", int)
SoftwareVersion = NewType("SoftwareVersion", str)
DEFAULT_ROOT: Final = Path("data/records")
DEFAULT_RETENTION: Final = RetentionDays(30)
UNKNOWN_VERSION: Final = SoftwareVersion("unversioned")
MAX_IDENTIFIER: Final = 128
MAX_PATH: Final = 4096
VERSION: Final = compile_pattern(r"[A-Za-z0-9_.+-]+")


class RecordingKey(StrEnum):
    MACHINE = "RECORD_MACHINE_ID"
    ORGANIZATION = "RECORD_ORGANIZATION_ID"
    ROOT = "RECORD_ROOT"
    RETENTION = "RECORD_LOCAL_RETENTION_DAYS"
    SOFTWARE = "ANHEART_SOFTWARE_VERSION"


class RecordingProblemCode(StrEnum):
    INVALID_IDENTIFIER = "recording_identifier_invalid"
    INVALID_ROOT = "recording_root_invalid"
    INVALID_RETENTION = "recording_retention_invalid"
    INVALID_VERSION = "recording_version_invalid"


@dataclass(frozen=True, slots=True)
class RecordingProblem:
    key: RecordingKey
    code: RecordingProblemCode


@dataclass(frozen=True, slots=True)
class RecordingConfig:
    machine_id: MachineId | None = None
    organization_id: OrganizationId | None = None
    root: Path = DEFAULT_ROOT
    retention_days: RetentionDays = DEFAULT_RETENTION
    software_version: SoftwareVersion = UNKNOWN_VERSION


DEFAULT_RECORDING: Final = RecordingConfig()


def opaque_identifier(value: str) -> bool:
    return 0 < len(value) <= MAX_IDENTIFIER and IDENTIFIER.fullmatch(value) is not None


def load_recording_config(
    env: Mapping[str, str],
) -> Result[RecordingConfig, tuple[RecordingProblem, ...]]:
    machine = env.get(RecordingKey.MACHINE, "").strip()
    organization = env.get(RecordingKey.ORGANIZATION, "").strip()
    root = env.get(RecordingKey.ROOT, "").strip() or str(DEFAULT_ROOT)
    retention = env.get(RecordingKey.RETENTION, "").strip() or str(DEFAULT_RETENTION)
    version = env.get(RecordingKey.SOFTWARE, "").strip() or UNKNOWN_VERSION
    problems = [
        RecordingProblem(key, RecordingProblemCode.INVALID_IDENTIFIER)
        for key, value in (
            (RecordingKey.MACHINE, machine),
            (RecordingKey.ORGANIZATION, organization),
        )
        if value and not opaque_identifier(value)
    ]
    if len(root) > MAX_PATH or "\x00" in root:
        problems.append(RecordingProblem(RecordingKey.ROOT, RecordingProblemCode.INVALID_ROOT))
    try:
        days = int(retention)
    except ValueError:
        days = -1
    if days < 0:
        problems.append(
            RecordingProblem(RecordingKey.RETENTION, RecordingProblemCode.INVALID_RETENTION)
        )
    if len(version) > MAX_IDENTIFIER or VERSION.fullmatch(version) is None:
        problems.append(
            RecordingProblem(RecordingKey.SOFTWARE, RecordingProblemCode.INVALID_VERSION)
        )
    if problems:
        return Err(tuple(problems))
    return Ok(
        RecordingConfig(
            MachineId(machine) if machine else None,
            OrganizationId(organization) if organization else None,
            Path(root),
            RetentionDays(days),
            SoftwareVersion(version),
        )
    )
