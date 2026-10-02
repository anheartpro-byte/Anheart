"""The rotating geometry of this machine: the ONE record every speed is rendered through.

There used to be two ``MachineGeometry`` classes (one in the runtime, one in the
web layer) and a third, implicit radius of 1.0 m inside the physiology
simulator, and the bench console quoted "g at 1 m". They disagreed with each
other, which is how a screen ends up showing a g-load for a radius nobody
measured. This module replaces all of them.

Two numbers matter and they are treated differently on purpose:

* ``radius`` has **no default**. It is a measurement of this rig (axis to the
  occupant), g is linear in it, and a default is how a wrong one gets used
  without anybody choosing it. The local console reads it from the required
  ``ARM_RADIUS_M``.
* ``ratio`` defaults to :data:`CONFIRMED_GEAR_RATIO`, 49.79, because that one
  was *confirmed on the bench*: at 1380 motor rpm, i = 49.79 predicts 27.7
  output rpm, 13.9 turns in 30 s, and 14 were counted. The plate's "1380/28"
  is motor rpm over output rpm (rounded), not a second, different ratio.

The nameplate point (1380 rpm at 50 Hz) is a fact of the motor bolted on, and
defaults to it.

See .claude/skills/anheart-strict-python/SKILL.md rule 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from src.training.plan import NAMEPLATE_BASE_HERTZ, NAMEPLATE_MOTOR_RPM
from src.training.types import SpeedView
from src.units import (
    GearRatio,
    Hertz,
    Metres,
    MotorRpm,
    OutputRpm,
    ResultantG,
    output_to_motor_rpm,
    resultant_g_to_output_rpm,
)

CONFIRMED_GEAR_RATIO: Final[GearRatio] = GearRatio(49.79)
"""SEW KA37 i = 49.79, confirmed on the bench (14 output turns in 30 s at 1380 rpm)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MachineGeometry:
    """Radius, gear ratio and nameplate point: everything a speed is rendered through.

    Keyword-only, so ``MachineGeometry(1.5, 49.79)`` cannot silently swap the
    radius and the ratio, which are both bare-looking floats at a call site.
    """

    radius: Metres
    """Distance from the axis to the occupant, where every g is quoted. No default."""

    ratio: GearRatio = CONFIRMED_GEAR_RATIO
    """Gearbox reduction, motor turns per output turn."""

    nominal_rpm: MotorRpm = NAMEPLATE_MOTOR_RPM
    base_hz: Hertz = NAMEPLATE_BASE_HERTZ

    def __post_init__(self) -> None:
        """Refuse geometry that would make every speed on every screen nonsense.

        ``math.isfinite`` first, deliberately: every comparison against NaN is
        false, so a bare ``value <= 0.0`` test *accepts* NaN. Raises rather than
        returning a ``Result``: this is built at startup with nothing spinning,
        and refusing to start is the right answer to numbers nobody can vouch for.
        """
        for name, value in (
            ("radius", float(self.radius)),
            ("ratio", float(self.ratio)),
            ("nominal_rpm", float(self.nominal_rpm)),
            ("base_hz", float(self.base_hz)),
        ):
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive and finite, got {value}")

    def view(self, rpm: MotorRpm) -> SpeedView:
        """One motor-shaft speed, rendered in every unit, through this geometry."""
        return SpeedView.from_motor_rpm(
            rpm,
            ratio=self.ratio,
            radius=self.radius,
            nominal_rpm=self.nominal_rpm,
            base_hz=self.base_hz,
        )

    def output_rpm_for(self, load: ResultantG) -> OutputRpm:
        """The output speed at which the occupant feels ``load`` (resultant g)."""
        return resultant_g_to_output_rpm(load, self.radius)

    def motor_rpm_for(self, load: ResultantG) -> MotorRpm:
        """The motor speed at which the occupant feels ``load``, rounded to the drive's rpm."""
        return output_to_motor_rpm(self.output_rpm_for(load), self.ratio)
