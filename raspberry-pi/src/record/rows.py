from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Sequence[JsonValue] | Mapping[str, JsonValue]


def finite(value: float) -> float:
    if not isfinite(value):
        raise ValueError(f"non-finite value {value} in a trace")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Row:
    t: float
    state: str
    mode: str
    phase: str
    drive_state: str
    sim_state: str
    setpoint_motor_rpm: int
    lfrd_motor_rpm: int
    measured_motor_rpm: int
    measured_fresh: bool
    output_rpm: float
    hertz: float
    g_reference: float
    g_leg_tip: float
    setpoint_output_rpm: float
    setpoint_g_leg_tip: float
    manual_target_motor_rpm: int
    hr_true: int | None
    hr_live: int | None
    target_bpm: int | None
    safety_action: str
    safety_rule: str | None
    output_enabled: bool
    silent: bool
    current_a: float | None
    hr_raw: int | None = None
    hr_confirmed: int | None = None
    hr_quality: str = "no_signal"
    drive_status_word: int | None = None

    def to_json(self) -> Mapping[str, JsonValue]:
        return {
            "type": "row",
            "t": round(finite(self.t), 3),
            "state": self.state,
            "mode": self.mode,
            "phase": self.phase,
            "drive_state": self.drive_state,
            "sim_state": self.sim_state,
            "setpoint_motor_rpm": self.setpoint_motor_rpm,
            "lfrd_motor_rpm": self.lfrd_motor_rpm,
            "measured_motor_rpm": self.measured_motor_rpm,
            "measured_fresh": self.measured_fresh,
            "output_rpm": round(finite(self.output_rpm), 4),
            "hertz": round(finite(self.hertz), 4),
            "g_reference": round(finite(self.g_reference), 4),
            "g_leg_tip": round(finite(self.g_leg_tip), 4),
            "setpoint_output_rpm": round(finite(self.setpoint_output_rpm), 4),
            "setpoint_g_leg_tip": round(finite(self.setpoint_g_leg_tip), 4),
            "manual_target_motor_rpm": self.manual_target_motor_rpm,
            "hr_true": self.hr_true,
            "hr_live": self.hr_live,
            "target_bpm": self.target_bpm,
            "safety_action": self.safety_action,
            "safety_rule": self.safety_rule,
            "output_enabled": self.output_enabled,
            "silent": self.silent,
            "current_a": None if self.current_a is None else round(finite(self.current_a), 4),
            "hr_raw": self.hr_raw,
            "hr_confirmed": self.hr_confirmed,
            "hr_quality": self.hr_quality,
            "drive_status_word": self.drive_status_word,
        }
