"""What one scenario run leaves behind: the per-tick trace, the drive frames, the events.

Written as JSON Lines (one ``meta`` line, then one line per ``row``, ``frame``
and ``event``, then one ``final`` line) so a 45-minute run streams to disk and
can be tailed, and as a CSV of the rows for a spreadsheet. The 2D viewer reads
the JSONL directly.

Every number that leaves this module is finite: :func:`finite` refuses NaN and
infinity rather than writing ``NaN`` into a file a browser will then parse.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

from simulation.recording import Frame

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | Sequence[JsonValue] | Mapping[str, JsonValue]

SCHEMA: Final[int] = 1


def finite(value: float) -> float:
    """``value`` if it is finite; raises ``ValueError`` otherwise (never written as NaN)."""
    if not math.isfinite(value):
        raise ValueError(f"non-finite value {value} in a trace")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Row:
    """One control tick, as observed. ``t`` is seconds since the session start command."""

    t: float
    state: str
    mode: str
    phase: str
    drive_state: str
    sim_state: str
    setpoint_motor_rpm: int
    lfrd_motor_rpm: int
    """The speed reference as the drive holds it (``SimulatedDrive.commanded_setpoint``)."""

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
            "output_rpm": _num(self.output_rpm),
            "hertz": _num(self.hertz),
            "g_reference": _num(self.g_reference),
            "g_leg_tip": _num(self.g_leg_tip),
            "setpoint_output_rpm": _num(self.setpoint_output_rpm),
            "setpoint_g_leg_tip": _num(self.setpoint_g_leg_tip),
            "manual_target_motor_rpm": self.manual_target_motor_rpm,
            "hr_true": self.hr_true,
            "hr_live": self.hr_live,
            "target_bpm": self.target_bpm,
            "safety_action": self.safety_action,
            "safety_rule": self.safety_rule,
            "output_enabled": self.output_enabled,
            "silent": self.silent,
            "current_a": None if self.current_a is None else _num(self.current_a),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Something that happened at an instant: an action, a refusal, a verdict, a phase."""

    t: float
    kind: str
    detail: str

    def to_json(self) -> Mapping[str, JsonValue]:
        return {
            "type": "event",
            "t": round(finite(self.t), 3),
            "kind": self.kind,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalState:
    """How the machine was left, read from the simulator after the teardown window."""

    runtime_state: str
    end_reason: str | None
    stop_reason: str | None
    silent: bool
    runtime_output_enabled: bool
    runtime_applied_rpm: int
    sim_state: str
    energised: bool
    lfrd_motor_rpm: int
    shaft_motor_rpm: int | None
    """RFRD read by a probe after teardown; ``None`` if the drive still did not answer."""

    shutdown_detail: str | None

    def to_json(self) -> Mapping[str, JsonValue]:
        return {
            "type": "final",
            "runtime_state": self.runtime_state,
            "end_reason": self.end_reason,
            "stop_reason": self.stop_reason,
            "silent": self.silent,
            "runtime_output_enabled": self.runtime_output_enabled,
            "runtime_applied_rpm": self.runtime_applied_rpm,
            "sim_state": self.sim_state,
            "energised": self.energised,
            "lfrd_motor_rpm": self.lfrd_motor_rpm,
            "shaft_motor_rpm": self.shaft_motor_rpm,
            "shutdown_detail": self.shutdown_detail,
        }


def _num(value: float) -> float:
    return round(finite(value), 4)


def frame_to_json(frame: Frame, origin: float) -> Mapping[str, JsonValue]:
    """One drive frame, timed from the session start."""
    return {
        "type": "frame",
        "t": round(finite(frame.at - origin), 3),
        "kind": frame.kind.value,
        "value": frame.value,
        "label": frame.label,
        "ok": frame.ok,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Trace:
    """A whole run."""

    meta: Mapping[str, JsonValue]
    rows: tuple[Row, ...]
    frames: tuple[Mapping[str, JsonValue], ...]
    events: tuple[Event, ...]
    final: FinalState

    def lines(self) -> list[str]:
        """The JSONL file, line by line."""
        out = [json.dumps({"type": "meta", "schema": SCHEMA, **self.meta}, allow_nan=False)]
        out.extend(json.dumps(row.to_json(), allow_nan=False) for row in self.rows)
        out.extend(json.dumps(frame, allow_nan=False) for frame in self.frames)
        out.extend(json.dumps(event.to_json(), allow_nan=False) for event in self.events)
        out.append(json.dumps(self.final.to_json(), allow_nan=False))
        return out

    def write_jsonl(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines()) + "\n", encoding="utf-8")
        return path

    def write_csv(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        names = [spec.name for spec in fields(Row)]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(names)
            for row in self.rows:
                encoded = row.to_json()
                writer.writerow(["" if encoded[name] is None else encoded[name] for name in names])
        return path
