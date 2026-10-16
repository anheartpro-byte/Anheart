"""Immutable session envelope and closed on-disk event vocabulary."""

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Self, assert_never

from pydantic import BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator
from pydantic.dataclasses import dataclass

from src.motor.drive import (
    BadResponse,
    CommTimeout,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    EnableUnconfirmed,
    StopUnconfirmed,
    UnexpectedState,
)
from src.motor.observation import MODBUS_FAILURES, ModbusFailure
from src.record.rows import JsonValue
from src.units import Monotonic, MotorRpm, OutOfRange, RawRegister, Seconds

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
    wall_trusted: bool | None = None
    """Whether a time service was keeping the wall clock that gave ``utc_start``.

    ``False``: nobody answered for that clock when the record was opened, and
    its dates may be wrong by however long the machine had been off. ``None``:
    the producer did not judge its clock (the simulation, and every record
    written before this field existed). Optional when read; the writer always
    emits it."""


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
    files: tuple[str, ...] | None = None
    subject_fit: Literal[True] | None = None
    fitness_questionnaire_version: str | None = None


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


def _state(value: object) -> DriveState:
    if isinstance(value, DriveState):
        return value
    if isinstance(value, str) and value in DriveState.__members__:
        return DriveState[value]
    raise ValueError("unknown drive state")


def _fault(value: object) -> DriveFault:
    if isinstance(value, DriveFault):
        return value
    if isinstance(value, str) and value in DriveFault.__members__:
        return DriveFault[value]
    raise ValueError("unknown drive fault")


def _state_name(value: DriveState) -> str:
    return value.name


def _fault_name(value: DriveFault) -> str:
    return value.name


def _absent(value: object) -> bool:
    return value is None


FAILURE_CONFIG = ConfigDict(allow_inf_nan=False, extra="forbid", strict=True)


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class TimeoutFailure:
    type: Literal["CommTimeout"] = "CommTimeout"
    after: Annotated[Seconds, Field(ge=0)]


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class ResponseFailure:
    type: Literal["BadResponse"] = "BadResponse"


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class StateFailure:
    type: Literal["UnexpectedState"] = "UnexpectedState"
    expected: Annotated[DriveState, BeforeValidator(_state), PlainSerializer(_state_name)]
    actual: Annotated[DriveState, BeforeValidator(_state), PlainSerializer(_state_name)]


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class FaultFailure:
    type: Literal["DriveFaulted"] = "DriveFaulted"
    fault: Annotated[DriveFault, BeforeValidator(_fault), PlainSerializer(_fault_name)]
    raw_code: Annotated[RawRegister, Field(ge=0, le=65535)]


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class RangeFailure:
    type: Literal["OutOfRange"] = "OutOfRange"


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class EnableFailure:
    type: Literal["EnableUnconfirmed"] = "EnableUnconfirmed"
    reference_zeroed: bool
    run_command_removed: bool


@dataclass(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class StopFailure:
    type: Literal["StopUnconfirmed"] = "StopUnconfirmed"
    waited: Annotated[Seconds, Field(ge=0)]
    last_output_rpm: MotorRpm


type RecordedFailure = Annotated[
    TimeoutFailure
    | ResponseFailure
    | StateFailure
    | FaultFailure
    | RangeFailure
    | EnableFailure
    | StopFailure,
    Field(discriminator="type"),
]
"""Closed failure vocabulary for drive-call observations.

Only decision-bearing fields are kept. Human diagnostics never enter this value.
"""


def record_failure(error: DriveError) -> RecordedFailure:  # noqa: PLR0911  # one return per error
    """Strip prose from an error without changing the evidence the runtime acts on."""
    match error:
        case CommTimeout():
            return TimeoutFailure(after=error.after)
        case BadResponse():
            return ResponseFailure()
        case UnexpectedState():
            return StateFailure(expected=error.expected, actual=error.actual)
        case DriveFaulted():
            return FaultFailure(fault=error.fault, raw_code=error.raw_code)
        case OutOfRange():
            return RangeFailure()
        case EnableUnconfirmed():
            return EnableFailure(
                reference_zeroed=error.reference_zeroed,
                run_command_removed=error.run_command_removed,
            )
        case StopUnconfirmed():
            return StopFailure(waited=error.waited, last_output_rpm=error.last_output_rpm)
    raise assert_never(error)


def replay_failure(error: RecordedFailure) -> DriveError:  # noqa: PLR0911  # one return per error
    """Restore the variant and its evidence; omitted prose gets a fixed diagnostic."""
    match error:
        case TimeoutFailure():
            return CommTimeout(after=error.after)
        case ResponseFailure():
            return BadResponse(detail="recorded failure")
        case StateFailure():
            return UnexpectedState(expected=error.expected, actual=error.actual)
        case FaultFailure():
            return DriveFaulted(fault=error.fault, raw_code=error.raw_code)
        case RangeFailure():
            return OutOfRange(quantity="recorded", value=0.0, low=0.0, high=0.0)
        case EnableFailure():
            return EnableUnconfirmed(
                detail="recorded failure",
                reference_zeroed=error.reference_zeroed,
                run_command_removed=error.run_command_removed,
            )
        case StopFailure():
            return StopUnconfirmed(
                waited=error.waited,
                last_output_rpm=error.last_output_rpm,
                detail="recorded failure",
            )
    raise assert_never(error)


@dataclass(frozen=True, slots=True, kw_only=True, config=CONFIG)
class DriveFrame:
    t: float
    kind: str
    register: int | tuple[int, ...] | None
    value: int | tuple[int | None, ...] | None
    ok: bool
    latency_ms: float
    raw_hex: str | None = None
    error: RecordedFailure | ModbusFailure | None = Field(default=None, exclude_if=_absent)

    @model_validator(mode="after")
    def failed_only(self) -> Self:
        if self.ok and self.error is not None:
            raise ValueError("a successful exchange cannot carry an error")
        if self.error is not None and isinstance(
            self.error, MODBUS_FAILURES
        ) != self.kind.startswith("modbus_"):
            raise ValueError(
                "the failure vocabulary must match native exchanges or call observations"
            )
        return self


@dataclass(frozen=True, slots=True)
class RecordError:
    operation: Literal[
        "create", "append", "tick", "event", "raw", "sensors", "sync", "close", "read", "view"
    ]
    detail: str


@dataclass(frozen=True, slots=True)
class ReadWarning:
    file: str
    code: Literal["missing_checksums", "truncated", "checksum_mismatch", "missing_file"]
