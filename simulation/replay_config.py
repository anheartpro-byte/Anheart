from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ConfigDict, TypeAdapter
from pydantic.dataclasses import dataclass as validated

from simulation.harness import PANEL_SAFETY, RUNTIME_LIMITS, load_motion
from simulation.replay_report import ConfigurationCheck, CurrentPart
from src.geometry import MachineGeometry
from src.local_config import CameraSource
from src.medical import SHIPPED_PARAMETERS, shipped_status
from src.motor.atv320 import SerialSettings
from src.motor.drive import RegisterMap
from src.motor.ftdi_link import Parity
from src.panel_status import EcgSource, MotorBackend
from src.record.configuration import MAX_CONFIG_BYTES
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind
from src.training.motion import MotionLimits
from src.training.runtime import RuntimeLimits
from src.training.safety import SafetyLimits
from src.units import GearRatio, Hertz, Metres, MotorRpm, Seconds


@validated(frozen=True, slots=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class _Medical:
    safety: str
    motion: str
    min_rider_age: int
    file_sha256: str
    signed: bool


@validated(frozen=True, slots=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class _NativeDrive:
    baudrate: int
    bytesize: int
    parity: Parity
    stopbits: int
    timeout: float
    slave_address: int
    retries: int
    offset: int

    def build(self) -> tuple[SerialSettings, RegisterMap]:
        return (
            SerialSettings(
                port="replay",
                baudrate=self.baudrate,
                bytesize=self.bytesize,
                parity=self.parity,
                stopbits=self.stopbits,
                timeout=Seconds(self.timeout),
                slave_address=self.slave_address,
                retries=self.retries,
            ),
            RegisterMap(offset=self.offset),
        )


@validated(frozen=True, slots=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class _Document:
    medical: _Medical
    runtime: str
    geometry: tuple[float, float, int, float, float | None]
    motor_max_rpm: int
    occupied_enabled: bool
    programs_enabled: bool
    sensors: tuple[SensorKind, ...]
    motor_backend: MotorBackend
    ecg_source: EcgSource
    camera: CameraSource
    drive_link: str | None
    native_drive: _NativeDrive | None = None


_DOCUMENT: Final[TypeAdapter[_Document]] = TypeAdapter(_Document)
_SAFETY: Final[TypeAdapter[SafetyLimits]] = TypeAdapter(SafetyLimits)
_MOTION: Final[TypeAdapter[MotionLimits]] = TypeAdapter(MotionLimits)
_RUNTIME: Final[TypeAdapter[RuntimeLimits]] = TypeAdapter(RuntimeLimits)


@dataclass(frozen=True, slots=True)
class AppliedRuntime:
    geometry: MachineGeometry
    limit_radius: Metres | None
    runtime: RuntimeLimits
    safety: SafetyLimits
    motion: MotionLimits
    check: ConfigurationCheck
    native_drive: tuple[SerialSettings, RegisterMap] | None = None


def sidecar(path: Path) -> Path:
    name = path.name if path.is_dir() else path.name.removesuffix(".tar.gz")
    return path.with_name(name + ".config.json")


def _differences(
    document: _Document, safety: SafetyLimits, motion: MotionLimits, runtime: RuntimeLimits
) -> tuple[CurrentPart, ...]:
    current = shipped_status()
    parts: list[CurrentPart] = []
    if (document.medical.file_sha256, document.medical.signed) != (
        current.document.sha256,
        current.signed,
    ):
        parts.append("medical_file")
    if safety != PANEL_SAFETY:
        parts.append("safety")
    if motion != load_motion():
        parts.append("motion")
    if runtime != RUNTIME_LIMITS:
        parts.append("runtime")
    if document.medical.min_rider_age != SHIPPED_PARAMETERS.min_rider_age:
        parts.append("min_rider_age")
    return tuple(parts)


def load_configuration(path: Path | None, expected_hash: str) -> Result[AppliedRuntime, str]:  # noqa: PLR0911  # one return per refusal
    if path is None:
        return Err(
            "the console configuration document is absent: "
            "supply --config or a .config.json sidecar"
        )
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return Err("the console configuration document is absent or unreadable")
    if len(content) > MAX_CONFIG_BYTES:
        return Err("the console configuration document exceeds MAX_CONFIG_BYTES")
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        return Err(
            f"configuration hash mismatch: manifest {expected_hash[:16]}, "
            f"document {actual_hash[:16]}"
        )
    try:
        document = _DOCUMENT.validate_json(content, strict=True)
        native = None if document.native_drive is None else document.native_drive.build()
        safety = _SAFETY.validate_json(document.medical.safety, strict=True, extra="forbid")
        motion = _MOTION.validate_json(document.medical.motion, strict=True, extra="forbid")
        runtime = _RUNTIME.validate_json(document.runtime, strict=True, extra="forbid")
        radius, ratio, nominal, base, leg_tip = document.geometry
        geometry = MachineGeometry(
            radius=Metres(radius),
            ratio=GearRatio(ratio),
            nominal_rpm=MotorRpm(nominal),
            base_hz=Hertz(base),
        )
        if leg_tip is not None and leg_tip < radius:
            return Err("the console configuration document holds an invalid leg-tip radius")
    except ValueError:
        return Err("this build refuses the console configuration document's geometry or limits")
    differing = _differences(document, safety, motion, runtime)
    return Ok(
        AppliedRuntime(
            geometry,
            None if leg_tip is None else Metres(leg_tip),
            runtime,
            safety,
            motion,
            ConfigurationCheck(True, bool(differing), differing),
            native,
        )
    )
