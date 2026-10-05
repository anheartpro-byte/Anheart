"""Recording provenance only; this is not the future replay constructor contract."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, assert_never
from uuid import uuid4

from pydantic import TypeAdapter

from src.clock import Clock
from src.local_config import LocalConfig
from src.record.budget import bounded_manifest
from src.record.codec import JSON, document
from src.record.config import LocalReference, RecordingConfig, opaque_identifier
from src.record.rows import JsonValue
from src.record.schema import Clocks, Manifest, Profile
from src.result import Err, Ok, Result
from src.training.motion import MotionLimits, parse_motion_limits
from src.training.plan import Program
from src.training.runtime import RuntimeLimits
from src.training.safety import SafetyLimits
from src.training.types import Occupancy
from src.units import Hertz

MAX_MEDICAL_BYTES: Final = 65_536


class ContextUnavailable(StrEnum):
    IDENTITY_MISSING = "recording_identity_missing"
    SOURCE_MISSING = "recording_source_missing"
    MEDICAL_UNAVAILABLE = "recording_medical_version_unavailable"
    IDENTIFIER_INVALID = "recording_subject_or_session_invalid"
    METADATA_LIMIT = "recording_metadata_limit"


@dataclass(frozen=True, slots=True)
class ContextProblem:
    code: ContextUnavailable


@dataclass(frozen=True, slots=True)
class Parameters:
    runtime: RuntimeLimits
    safety: SafetyLimits
    motion: MotionLimits
    ecg_sample_rate: Hertz
    dsp_output_rate: Hertz


PARAMETERS: Final[TypeAdapter[Parameters]] = TypeAdapter(Parameters)


@dataclass(frozen=True, slots=True)
class ContextSource:
    configuration: RecordingConfig
    config_hash: str
    medical_version: str | None


@dataclass(frozen=True, slots=True)
class SessionRequest:
    occupancy: Occupancy
    program: Program | None = None
    subject_id: str | None = None
    cloud_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionContext:
    """Frozen attempt metadata; the operator identifier is an unverified, session-scoped alias."""

    local_ref: LocalReference
    clocks: Clocks
    manifest: Result[Manifest, ContextProblem]


def medical_version(path: Path, expected: MotionLimits) -> str | None:
    """Hash bounded startup bytes only when they match the actual loaded numerical limits."""
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_MEDICAL_BYTES + 1)
        if len(content) > MAX_MEDICAL_BYTES:
            return None
        parsed = parse_motion_limits(JSON.validate_json(content))
    except (OSError, ValueError):
        return None
    match parsed:
        case Err():
            return None
        case Ok(actual):
            return hashlib.sha256(content).hexdigest() if actual == expected else None
        case _ as unreachable:
            assert_never(unreachable)


def context_source(
    config: LocalConfig, parameters: Parameters, medical_path: Path
) -> ContextSource:
    link = config.drive_link
    facts: JsonValue = {
        "geometry": (
            config.geometry.radius,
            config.geometry.ratio,
            config.geometry.nominal_rpm,
            config.geometry.base_hz,
            config.leg_tip_radius,
        ),
        "motor_max_rpm": config.motor_max_rpm,
        "occupied_enabled": config.occupied_enabled,
        "programs_enabled": config.programs_enabled,
        "min_rider_age": config.min_rider_age,
        "sensors": tuple(kind.value for kind in config.sensors),
        "motor_backend": config.motor_backend.value,
        "ecg_source": config.ecg_source.value,
        "camera": config.camera.value,
        "drive_link": None
        if link is None
        else (
            link.settings.baudrate,
            link.settings.bytesize,
            link.settings.parity.value,
            link.settings.stopbits,
            link.settings.timeout,
            link.settings.slave_address,
            link.settings.retries,
            link.registers.offset,
        ),
        "parameters": document(PARAMETERS, parameters),
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return ContextSource(
        config.recording,
        hashlib.sha256(encoded.encode()).hexdigest(),
        medical_version(medical_path, parameters.motion),
    )


def resolved_profile(program: Program) -> Profile:
    profile = program.profile
    return Profile(
        total_duration_s=profile.total_duration_s,
        baseline_s=profile.baseline_s,
        warmup_max_s=profile.warmup_max_s,
        hold_min_s=profile.hold_min_s,
        cooldown_s=profile.cooldown_s,
        recovery_s=profile.recovery_s,
        zone_low_bpm=profile.zone_low_bpm,
        zone_high_bpm=profile.zone_high_bpm,
        hard_max_bpm=profile.hard_max_bpm,
        critical_bpm=profile.critical_bpm,
        subject_hr_max=profile.subject_hr_max,
        min_run_rpm=profile.min_run_rpm,
        max_rpm=profile.max_rpm,
        warmup_rpm_ceiling_fraction=profile.warmup_rpm_ceiling_fraction,
        channels=tuple(channel.value for channel in profile.channels),
        allow_above_nameplate=profile.allow_above_nameplate,
        source_rev=program.source_rev,
        resolved_at=program.resolved_at,
        total_overridden=program.total_overridden,
    )


def session_context(
    source: ContextSource | None, request: SessionRequest, clock: Clock
) -> SessionContext:
    local_ref = LocalReference(uuid4().hex)
    stamp = (
        datetime.fromtimestamp(clock.unix_millis() / 1000, UTC).isoformat().replace("+00:00", "Z")
    )
    clocks = Clocks(monotonic_start=clock.monotonic(), utc_start=stamp, ntp_offset_s=None)
    if source is None:
        return SessionContext(
            local_ref, clocks, Err(ContextProblem(ContextUnavailable.SOURCE_MISSING))
        )
    configuration = source.configuration
    if configuration.machine_id is None or configuration.organization_id is None:
        return SessionContext(
            local_ref, clocks, Err(ContextProblem(ContextUnavailable.IDENTITY_MISSING))
        )
    if source.medical_version is None:
        return SessionContext(
            local_ref, clocks, Err(ContextProblem(ContextUnavailable.MEDICAL_UNAVAILABLE))
        )
    if any(
        value is not None and not opaque_identifier(value)
        for value in (request.subject_id, request.cloud_session_id)
    ):
        return SessionContext(
            local_ref, clocks, Err(ContextProblem(ContextUnavailable.IDENTIFIER_INVALID))
        )
    manifest = Manifest(
        schema_version=2,
        record_id=local_ref,
        local_ref=local_ref,
        machine_id=configuration.machine_id,
        organization_id=configuration.organization_id,
        session_id=request.cloud_session_id,
        subject_id=request.subject_id,
        kind="manual" if request.program is None else "auto",
        occupancy=request.occupancy.value,
        operator=uuid4().hex,
        profile=None if request.program is None else resolved_profile(request.program),
        config_hash=source.config_hash,
        software_version=configuration.software_version,
        contract_version="2",
        medical_parameters_version=source.medical_version,
        clocks=clocks,
        started_at=stamp,
        ended_at=None,
        end_reason=None,
        preflight=None,
        geometry=None,
    )
    if not bounded_manifest(manifest):
        return SessionContext(
            local_ref, clocks, Err(ContextProblem(ContextUnavailable.METADATA_LIMIT))
        )
    return SessionContext(local_ref, clocks, Ok(manifest))
