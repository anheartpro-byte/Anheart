"""Immutable session envelope and closed on-disk event vocabulary."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from src.record.rows import JsonValue
from src.units import Monotonic, Seconds

CONFIG = ConfigDict(allow_inf_nan=False, extra="forbid", ser_json_inf_nan="constants")


class EventKind(StrEnum):
    VERDICT = "verdict"
    VERDICT_ACK = "verdict_ack"
    REFUSAL = "refusal"
    PHASE = "phase"
    OPERATOR_ACTION = "operator_action"
    DRIVE_FAULT = "drive_fault"
    REMOTE_COMMAND = "remote_command"
    PREFLIGHT = "preflight"
    WARNING = "warning"
    END = "end"


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Clocks:
    monotonic_start: Monotonic
    utc_start: str
    ntp_offset_s: Seconds | None


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Profile:
    """Resolved numeric programme; display names are deliberately not serializable."""

    total_duration_s: float
    baseline_s: float
    warmup_max_s: float
    hold_min_s: float
    cooldown_s: float
    recovery_s: float
    zone_low_bpm: int
    zone_high_bpm: int
    hard_max_bpm: int
    critical_bpm: int
    subject_hr_max: int
    min_run_rpm: int
    max_rpm: int
    warmup_rpm_ceiling_fraction: float
    channels: tuple[str, ...]
    allow_above_nameplate: bool
    source_rev: int
    resolved_at: int
    total_overridden: bool


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Preflight:
    check: str
    passed: bool


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class GeometrySnapshot:
    reference_radius_m: float
    leg_tip_radius_m: float
    leg_tip_measured: bool
    arm_tip_radius_m: float
    capsule_near_radius_m: float
    capsule_far_radius_m: float
    counterweight_radius_m: float
    gear_ratio: float
    nominal_motor_rpm: int
    base_hz: float


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class EndObservation:
    t: float
    runtime_state: str | None = None
    drive_state: str | None = None
    runtime_output_enabled: bool | None = None
    runtime_applied_rpm: int | None = None
    lfrd_motor_rpm: int | None = None
    shaft_motor_rpm: int | None = None
    energised: bool | None = None
    silent: bool | None = None
    stop_reason: str | None = None
    shutdown_detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Manifest:
    schema_version: Literal[2]
    record_id: str
    machine_id: str
    organization_id: str
    session_id: str | None
    local_ref: str
    kind: Literal["auto", "manual"]
    occupancy: Literal["bench", "occupied"]
    operator: str
    subject_id: str | None
    profile: Profile | None
    config_hash: str
    software_version: str
    contract_version: str
    medical_parameters_version: str
    clocks: Clocks
    started_at: str
    ended_at: str | None
    end_reason: str | None
    preflight: tuple[Preflight, ...] | None
    geometry: GeometrySnapshot | None = None
    end_observation: EndObservation | None = None


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class Event:
    t: float
    kind: EventKind
    detail: str
    actor: str = "system"

    def to_json(self) -> Mapping[str, JsonValue]:
        return {
            "type": "event",
            "t": self.t,
            "kind": self.kind.value,
            "detail": self.detail,
            "actor": self.actor,
        }


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class DriveFrame:
    t: float
    kind: str
    register: int | tuple[int, ...] | None
    value: int | tuple[int | None, ...] | None
    ok: bool
    latency_ms: float
    raw_hex: str | None = None


@dataclass(frozen=True, slots=True)
class RecordError:
    operation: Literal[
        "create", "append", "tick", "event", "raw", "sensors", "close", "read", "view"
    ]
    detail: str


@dataclass(frozen=True, slots=True)
class ReadWarning:
    file: str
    code: Literal["missing_checksums", "truncated", "checksum_mismatch", "missing_file"]
