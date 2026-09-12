"""Domain units for the Anheart machine-control path.

Every quantity that crosses a function boundary is a distinct ``NewType``, so
the mix-ups that would actually hurt someone cannot compile:

* a heart rate handed to a speed setpoint,
* a motor-shaft speed used as an output-shaft speed (the gearbox ratio is
  49.79, so this is a 50x error and invisible in a plain ``float``),
* a monotonic reading used as a wall-clock timestamp,
* a raw ADC count used as a voltage.

``NewType`` is erased at runtime, so this costs nothing at 5 Hz on a Pi. The
cost is friction when writing code: arithmetic on a ``NewType`` yields the
underlying type, so results must be re-wrapped deliberately. That friction is
the feature, because it marks every point where a quantity changes meaning.

This module also owns the **only** conversions between units, and the
validating constructors used at the hardware boundary ("parse, don't
validate": ranges are checked once, here, and never again downstream).

See .claude/skills/anheart-strict-python/SKILL.md rules 1 and 6.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, NewType

from src.result import Err, Ok, Result

# --- Cardiac -------------------------------------------------------------
Bpm = NewType("Bpm", int)
"""Heart rate in beats per minute."""

BpmPerMinute = NewType("BpmPerMinute", float)
"""Rate of change of heart rate; the safety supervisor bounds this."""

# --- Rotation ------------------------------------------------------------
MotorRpm = NewType("MotorRpm", int)
"""Speed at the MOTOR shaft, signed, as written to / read from the drive."""

OutputRpm = NewType("OutputRpm", float)
"""Speed at the GEARBOX OUTPUT shaft: what the centrifuge actually turns at."""

RpmPerSecond = NewType("RpmPerSecond", float)
"""Slew rate at the motor shaft."""

GearRatio = NewType("GearRatio", float)
"""Gearbox reduction, motor turns per output turn (SEW KA37: 49.79)."""

Hertz = NewType("Hertz", float)
"""Drive output frequency."""

# --- Mechanics -----------------------------------------------------------
Metres = NewType("Metres", float)

GLoad = NewType("GLoad", float)
"""Centripetal acceleration in multiples of standard gravity."""

# --- Time ----------------------------------------------------------------
Seconds = NewType("Seconds", float)
"""A DURATION. Never a point in time."""

Monotonic = NewType("Monotonic", float)
"""A point in time from a monotonic clock. Meaningless as a wall-clock value."""

UnixMillis = NewType("UnixMillis", int)
"""A wall-clock timestamp in milliseconds. Never used for elapsed-time maths."""

# --- Electrical / acquisition -------------------------------------------
Millivolts = NewType("Millivolts", float)

AdcCount = NewType("AdcCount", int)
"""A raw BITalino analog reading, 10-bit: 0..1023."""

Amperes = NewType("Amperes", float)

# --- Modbus --------------------------------------------------------------
RegisterAddress = NewType("RegisterAddress", int)

RawRegister = NewType("RawRegister", int)
"""An uninterpreted 16-bit register value, 0..65535."""

StatusWord = NewType("StatusWord", int)
"""The drive ETA status word, uninterpreted."""


# --- Domain bounds -------------------------------------------------------
STANDARD_GRAVITY: Final[float] = 9.80665

ADC_MIN: Final[int] = 0
ADC_MAX: Final[int] = 1023
ADC_LEVELS: Final[int] = 1024

ADC_BASELINE: Final[int] = 512
"""Mid-scale: a DC-removed signal sits here."""

REGISTER_MIN: Final[int] = 0
REGISTER_MAX: Final[int] = 0xFFFF
SIGNED16_MIN: Final[int] = -32768
SIGNED16_MAX: Final[int] = 32767


@dataclass(frozen=True, slots=True)
class OutOfRange:
    """A value arriving from hardware fell outside its physical domain.

    Carries enough context to name the offending quantity in a log line or on
    the operator screen, because "out of range" alone is useless at 2am.
    """

    quantity: str
    value: float
    low: float
    high: float


# --- Validating constructors (the hardware boundary) --------------------


def adc_count(raw: int) -> Result[AdcCount, OutOfRange]:
    """Parse one BITalino analog sample.

    A value outside 0..1023 means the frame was misaligned or the column index
    was wrong, never a real measurement, so it must not reach the DSP.
    """
    if raw < ADC_MIN or raw > ADC_MAX:
        return Err(OutOfRange("adc", float(raw), float(ADC_MIN), float(ADC_MAX)))
    return Ok(AdcCount(raw))


def raw_register(value: int) -> Result[RawRegister, OutOfRange]:
    """Parse one 16-bit Modbus register as read off the wire."""
    if value < REGISTER_MIN or value > REGISTER_MAX:
        return Err(OutOfRange("register", float(value), float(REGISTER_MIN), float(REGISTER_MAX)))
    return Ok(RawRegister(value))


def register_to_signed(value: RawRegister) -> int:
    """Reinterpret a 16-bit register as a signed integer.

    The Altivar speed registers (LFRD setpoint, RFRD feedback) are signed rpm.
    Read as unsigned, a reverse speed of -1 rpm reads as 65535.
    """
    return value - 0x10000 if value > SIGNED16_MAX else value


def signed_to_register(value: int) -> Result[RawRegister, OutOfRange]:
    """Encode a signed integer into a 16-bit register for writing."""
    if value < SIGNED16_MIN or value > SIGNED16_MAX:
        return Err(OutOfRange("signed16", float(value), float(SIGNED16_MIN), float(SIGNED16_MAX)))
    return Ok(RawRegister(value & 0xFFFF))


# --- Conversions (the ONLY place units change meaning) ------------------


def motor_to_output_rpm(rpm: MotorRpm, ratio: GearRatio) -> OutputRpm:
    """Motor-shaft speed to centrifuge speed."""
    return OutputRpm(rpm / ratio)


def output_to_motor_rpm(rpm: OutputRpm, ratio: GearRatio) -> MotorRpm:
    """Centrifuge speed to motor-shaft speed, rounded to the drive integer rpm."""
    return MotorRpm(round(rpm * ratio))


def motor_rpm_to_hertz(rpm: MotorRpm, nominal_rpm: MotorRpm, base: Hertz) -> Hertz:
    """Motor speed to drive output frequency, via the nameplate point.

    SEW KA37 DRS71S4: 1380 rpm at 50 Hz. Used to set the drive HSP ceiling in
    Hz to match a software rpm ceiling, which is the ceiling that survives a
    software bug.
    """
    return Hertz(rpm / nominal_rpm * base)


def hertz_to_motor_rpm(freq: Hertz, nominal_rpm: MotorRpm, base: Hertz) -> MotorRpm:
    """Drive output frequency to motor speed."""
    return MotorRpm(round(freq / base * nominal_rpm))


def output_rpm_to_g(rpm: OutputRpm, radius: Metres) -> GLoad:
    """Centrifuge speed to centripetal load in g at the given radius.

    g = omega^2 * r / 9.80665, with omega = 2*pi*rpm/60. Quadratic in speed,
    which is why the heart-rate response to speed is not linear and why the
    control gain falls off at low speed.
    """
    omega = 2.0 * math.pi * rpm / 60.0
    return GLoad(omega * omega * radius / STANDARD_GRAVITY)


def g_to_output_rpm(load: GLoad, radius: Metres) -> OutputRpm:
    """Inverse of :func:`output_rpm_to_g`. Non-physical inputs give zero."""
    if load <= 0.0 or radius <= 0.0:
        return OutputRpm(0.0)
    omega = math.sqrt(load * STANDARD_GRAVITY / radius)
    return OutputRpm(omega * 60.0 / (2.0 * math.pi))


def adc_to_millivolts(count: AdcCount, mv_per_count: float) -> Millivolts:
    """Raw count to millivolts for a DC-removed (band-passed) signal.

    The mid-scale offset cancels once DC is removed, so this is a pure scale.
    """
    return Millivolts(count * mv_per_count)


def millivolts_to_adc(value: Millivolts, mv_per_count: float) -> AdcCount:
    """Exact inverse of :func:`adc_to_millivolts`, clamped to the ADC domain.

    Used by the ECG simulator so synthetic signals enter the real DSP pipeline
    through exactly the transform the pipeline will undo. Clamping is correct
    rather than an error here: a real BITalino clips too, and the quality
    classifier is meant to notice.
    """
    raw = ADC_BASELINE + round(value / mv_per_count)
    return AdcCount(min(ADC_MAX, max(ADC_MIN, raw)))


def elapsed(start: Monotonic, now: Monotonic) -> Seconds:
    """Duration between two monotonic readings.

    Exists so that subtracting instants yields a ``Seconds`` duration rather
    than a bare float that could be mistaken for another instant.
    """
    return Seconds(now - start)
