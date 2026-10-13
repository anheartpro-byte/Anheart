from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, NewType

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from src.record.writer import create_private
from src.training.types import TelemetrySnapshot
from src.units import Monotonic, Seconds, elapsed

SECONDS_PER_HOUR: Final[int] = 3600
MAX_FAULT_CODES: Final[int] = 127
MAX_OBSERVATION_GAP: Final[Seconds] = Seconds(1)
RotationHours = NewType("RotationHours", float)


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid"))
class Counters:
    rotation_hours: RotationHours = RotationHours(0)
    sessions_total: int = 0
    sessions_failed: int = 0
    drive_faults_by_code: Mapping[str, int] | None = None
    session_open: bool = False


COUNTERS: Final[TypeAdapter[Counters]] = TypeAdapter(Counters)


def load_counters(path: Path) -> Counters | None:
    try:
        result = COUNTERS.validate_json(path.read_bytes(), strict=True)
    except FileNotFoundError:
        return Counters(drive_faults_by_code={})
    except (OSError, ValidationError):
        return None
    faults = result.drive_faults_by_code
    if (
        not math.isfinite(result.rotation_hours)
        or result.rotation_hours < 0
        or result.sessions_total < 0
        or not 0 <= result.sessions_failed <= result.sessions_total
        or faults is None
        or len(faults) > MAX_FAULT_CODES + 1
        or any(value < 0 for value in faults.values())
        or any(re.fullmatch(r"[A-Za-z0-9_:-]{1,32}", key) is None for key in faults)
        or (result.session_open and result.sessions_failed >= result.sessions_total)
    ):
        return None
    return result


def save_counters(path: Path, counters: Counters) -> bool:
    temporary = path.with_suffix(".new")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        create_private(temporary, COUNTERS.dump_json(counters) + b"\n")
        temporary.replace(path)
    except OSError:
        return False
    return True


class HealthCounters:
    """Counters updated only on the event loop; disk workers receive frozen copies."""

    def __init__(self, initial: Counters | None) -> None:
        self._value: Counters | None = (
            None
            if initial is None
            else replace(
                initial,
                sessions_failed=initial.sessions_failed + int(initial.session_open),
                session_open=False,
            )
        )
        self._previous: Monotonic | None = None
        self._rotating: bool = False
        self._session_open: bool = False
        self._fault: int | None = None

    @property
    def value(self) -> Counters | None:
        return self._value

    def started(self) -> None:
        self._session_open = True
        if self._value is not None:
            self._value = replace(
                self._value,
                sessions_total=self._value.sessions_total + 1,
                session_open=True,
            )

    def observe(self, snapshot: TelemetrySnapshot, failed: bool | None) -> None:
        value = self._value
        previous = self._previous
        self._previous = snapshot.at
        rotating = snapshot.measured.motor_rpm > 0
        duration = Seconds(0) if previous is None else elapsed(previous, snapshot.at)
        rotation = (
            duration
            if self._rotating and rotating and 0 <= duration <= MAX_OBSERVATION_GAP
            else Seconds(0)
        )
        self._rotating = rotating
        fault = None if snapshot.fault is None else int(snapshot.fault.raw_code)
        new_fault = fault is not None and fault != self._fault
        self._fault = fault
        ended = self._session_open and failed is not None
        if ended:
            self._session_open = False
        if value is None:
            return
        failures = int(ended and failed is True)
        codes = {} if value.drive_faults_by_code is None else dict(value.drive_faults_by_code)
        if new_fault:
            key = str(fault)
            if key not in codes and len(codes) >= MAX_FAULT_CODES:
                key = "other"
            codes[key] = codes.get(key, 0) + 1
        self._value = replace(
            value,
            rotation_hours=RotationHours(value.rotation_hours + rotation / SECONDS_PER_HOUR),
            sessions_failed=value.sessions_failed + failures,
            drive_faults_by_code=codes,
            session_open=self._session_open,
        )
