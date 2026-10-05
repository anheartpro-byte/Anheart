"""What one scenario run leaves behind: the per-tick trace, the drive frames, the events.

The primary export is the shared schema-2 directory from ``src.record``.
JSON Lines (``meta``, ``row``, ``frame``, ``event``, ``final``) remains an
explicit schema-1 compatibility export; CSV uses the same shared columns.

Every number that leaves this module is finite: :func:`finite` refuses NaN and
infinity rather than writing ``NaN`` into a file a browser will then parse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, assert_never, override

from simulation.recording import Frame
from src.clock import ManualClock
from src.record.codec import Privacy, encode
from src.record.ecg import decode_block
from src.record.rows import JsonScalar, JsonValue, Row, finite
from src.record.schema import EndObservation, Event, EventKind, Manifest, RecordError
from src.record.writer import DEFAULT_PRIVACY, FRAME, TICK_COLUMNS, Writer, csv_line, tick_line
from src.result import Err, Ok, Result
from src.sensors.base import SensorReading
from src.units import UnixMillis

SCHEMA: Final[int] = 2
__all__ = [
    "Event",
    "FinalState",
    "JsonScalar",
    "JsonValue",
    "Row",
    "Trace",
    "finite",
    "frame_to_json",
]


@dataclass(frozen=True, slots=True)
class RecordingWriteError(RuntimeError):
    error: RecordError

    @override
    def __str__(self) -> str:
        return f"record {self.error.operation}: {self.error.detail}"


def _required[T](result: Result[T, RecordError]) -> T:
    match result:
        case Ok(value):
            return value
        case Err(error):
            raise RecordingWriteError(error)
        case _ as unreachable:
            assert_never(unreachable)


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


def frame_to_json(frame: Frame, origin: float) -> Mapping[str, JsonValue]:
    """One drive frame, timed from the session start."""
    return {
        "type": "frame",
        "t": round(finite(frame.at - origin), 3),
        "kind": frame.kind.value,
        "value": tuple(value for _, value in frame.observations)
        if frame.observations
        else frame.value,
        "label": frame.label,
        "ok": frame.ok,
        "register": tuple(register for register, _ in frame.observations)
        if frame.observations
        else frame.register,
        "latency_ms": finite(frame.latency_ms),
        "raw_hex": frame.raw_hex,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class Trace:
    """A whole run."""

    meta: Mapping[str, JsonValue]
    rows: tuple[Row, ...]
    frames: tuple[Mapping[str, JsonValue], ...]
    events: tuple[Event, ...]
    final: FinalState
    manifest: Manifest
    ended_at: UnixMillis
    final_observed_t: float
    raw: tuple[bytes, ...] = ()
    sensors: tuple[tuple[float, tuple[SensorReading, ...]], ...] = ()

    def write_record(self, root: Path, privacy: Privacy) -> Path:
        writer = _required(Writer.create(root, self.manifest, privacy))
        for row in self.rows:
            _required(writer.tick(row))
        for event in self.events:
            _required(writer.event(event))
        for frame in self.frames:
            payload = {
                key: frame[key] for key in ("t", "kind", "register", "value", "ok", "latency_ms")
            }
            payload["raw_hex"] = frame.get("raw_hex")
            _required(writer.frame(FRAME.validate_json(encode(payload, privacy))))
        for block in self.raw:
            _required(writer.raw(decode_block(block)))
        for at, readings in self.sensors:
            _required(writer.sensors(at, readings))
        _required(
            writer.event(
                Event(
                    t=self.final_observed_t,
                    kind=EventKind.END,
                    detail=self.final.end_reason or "simulation_horizon",
                )
            )
        )
        _required(
            writer.close(
                ManualClock(epoch_millis=self.ended_at),
                self.final.end_reason or "simulation_horizon",
                EndObservation(
                    t=self.final_observed_t,
                    runtime_state=self.final.runtime_state,
                    drive_state=self.final.sim_state,
                    runtime_output_enabled=self.final.runtime_output_enabled,
                    runtime_applied_rpm=self.final.runtime_applied_rpm,
                    lfrd_motor_rpm=self.final.lfrd_motor_rpm,
                    shaft_motor_rpm=self.final.shaft_motor_rpm,
                    energised=self.final.energised,
                    silent=self.final.silent,
                    stop_reason=self.final.stop_reason,
                    shutdown_detail=self.final.shutdown_detail,
                ),
            )
        )
        return writer.path

    @property
    def public_meta(self) -> Mapping[str, JsonValue]:
        return {
            key: value
            for key, value in self.meta.items()
            if key not in {"description", "tags", "profile_id"}
        }

    def lines(self, privacy: Privacy = DEFAULT_PRIVACY) -> list[str]:
        """The JSONL file, line by line."""
        out = [encode({"type": "meta", "schema": 1, **self.public_meta}, privacy)]
        out.extend(encode(row.to_json(), privacy) for row in self.rows)
        out.extend(encode(frame, privacy) for frame in self.frames)
        out.extend(encode(event.to_json(), privacy) for event in self.events)
        out.append(encode(self.final.to_json(), privacy))
        return out

    def write_jsonl(self, path: Path, privacy: Privacy = DEFAULT_PRIVACY) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines(privacy)) + "\n", encoding="utf-8")
        return path

    def write_csv(self, path: Path, privacy: Privacy = DEFAULT_PRIVACY) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(csv_line(TICK_COLUMNS))
            for row in self.rows:
                handle.write(tick_line(row, privacy))
        return path
