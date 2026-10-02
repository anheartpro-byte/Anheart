"""The motion profiler: how fast a setpoint may move, for a person's inner ear.

The heart-rate control law has its own slew limit (``SpeedLimits.slew``), sized
for a PI loop. A setpoint an operator types in, or a session step, is a
different animal: it is a *jump* in the target, and walking the machine to it
has to respect the two things that make a rotating person sick - how fast the
arm's speed changes, and how fast the load they feel changes. This module is
that walk, and nothing else. It is pure: no clock, no I/O except the one JSON
loader, no drive. The runtime owns the time base and hands it ``dt``.

The two limits, and why both
----------------------------

* **angular acceleration** of the arm, :attr:`MotionLimits.output_accel`
  (0.25 output rpm/s by default: 12.4 motor rpm/s through i = 49.79). Bounds
  the Coriolis cross-coupling a head movement produces while the speed is
  changing, which is what nauseates at LOW speed.
* **g-dot**, :attr:`MotionLimits.g_rate` (0.03 g/s of centripetal load).
  ``Gc`` is quadratic in speed, so the same angular acceleration changes the
  load faster the faster the arm turns: at high speed this is the binding one.
  See :func:`~src.units.g_rate_to_motor_slew`.

The setpoint follows the lower of the two at every speed, deceleration exactly
as acceleration: the post-rotatory sensation makes a descent as provocative as
a climb, so there is no faster "down" rate. The drive's own 3-4 s commissioned
ramp is reserved for QUICK_STOP and the emergency stop, which this module never
sees.

The domain
----------

Every setpoint this module returns is in ``{0} union [min_run, ...]``: an arm
creeping at 0.3 output rpm is neither at rest nor doing anything useful, and a
drive asked for 10 rpm at the motor sits in its own low-speed dead band. The
passage between 0 and ``min_run`` (55 motor rpm, 1.1 output rpm) is the one
step allowed to exceed ``rate * dt`` - it is 1.1 output rpm, a negligible load
(0.002 g at 1.5 m) - and it is taken only on a tick that has earned at least
one rpm of allowance, so a stalled time base cannot take it.

Integer rpm, and the accrual that keeps them honest
---------------------------------------------------

The drive takes whole motor rpm. 12.4 rpm/s over a 0.2 s tick is 2.48 rpm, and
a limiter that floored each tick independently would move 2 rpm per tick - a
silent 20 % slowdown, and at a slow enough rate, no motion at all. So ``dt`` is
the time since the setpoint LAST MOVED, not since the last tick: the caller
keeps that instant, and a tick that cannot pay for a whole rpm leaves the
allowance growing. ``max_interval`` caps it, so a setpoint that sat still for a
minute (a FREEZE, a hold) does not bank a minute of allowance and then spend it
in one step.

The guarantee the property tests pin: for any current setpoint, target and
``dt``, ``|next - current| <= rate(max(current, next)) * min(dt, max_interval)``
except for the one 0 <-> ``min_run`` passage, and ``next`` is in the domain and
between ``current`` and the target. ``rate`` is evaluated at the FASTER end of
the step because the g-dot bound tightens with speed.

All numbers here are [MED]: to be signed off by the medical side before
anybody rides. See ``config/motion_limits.json``.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, cast

from src.geometry import MachineGeometry
from src.result import Err, Ok, Result
from src.units import (
    GLoadPerSecond,
    MotorRpm,
    OutputRpmPerSecond,
    Seconds,
    g_rate_to_motor_slew,
    motor_to_output_rpm,
    output_to_motor_slew,
)

MAX_OUTPUT_ACCEL: Final[OutputRpmPerSecond] = OutputRpmPerSecond(2.0)
"""Refused above this: 100 motor rpm/s, already far past anything [MED] would sign."""

MAX_G_RATE: Final[GLoadPerSecond] = GLoadPerSecond(0.5)
"""Refused above this: half a g per second is a jolt, not a ramp."""

MAX_MIN_RUN: Final[MotorRpm] = MotorRpm(200)
"""Refused above this: a 4-output-rpm step out of standstill is not "negligible"."""

MAX_INTERVAL_CEILING: Final[Seconds] = Seconds(2.0)
"""Refused above this: the accrual cap must stay a few control periods long."""

RAMP_ESTIMATE_STEP: Final[Seconds] = Seconds(0.2)
"""The time step :func:`ramp_duration` walks at: the control period."""

RAMP_ESTIMATE_LIMIT: Final[Seconds] = Seconds(3600.0)
"""Beyond this :func:`ramp_duration` gives up and answers ``None``."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _finite_in(name: str, value: float, high: float) -> None:
    """Finite, strictly positive and at most ``high``. NaN fails, deliberately first."""
    _require(
        math.isfinite(value) and 0.0 < value <= high,
        f"{name} must be finite and in (0, {high}], got {value}",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionLimits:
    """The anti-nausea limits every non-emergency setpoint change obeys. All [MED].

    Keyword-only and frozen: four numbers in four units, and a limit that could
    be edited mid-rotation would make a session log stop being evidence of what
    the machine was allowed to do. Raises ``ValueError`` on an incoherent set
    (startup only, nothing spinning), like every configuration record here.
    """

    output_accel: OutputRpmPerSecond
    """Angular acceleration limit of the arm, output rpm per second."""

    g_rate: GLoadPerSecond
    """Rate-of-change limit of the centripetal load, g per second."""

    min_run: MotorRpm
    """The slowest non-zero setpoint, motor rpm: the domain's lower edge."""

    max_interval: Seconds = Seconds(1.0)
    """The longest interval one step may be paid for with (see the module docstring)."""

    def __post_init__(self) -> None:
        _finite_in("output_accel", self.output_accel, MAX_OUTPUT_ACCEL)
        _finite_in("g_rate", self.g_rate, MAX_G_RATE)
        _finite_in("max_interval", self.max_interval, MAX_INTERVAL_CEILING)
        _require(
            1 <= self.min_run <= MAX_MIN_RUN,
            f"min_run must be 1..{MAX_MIN_RUN} motor rpm, got {self.min_run}",
        )


DEFAULT_MOTION_LIMITS: Final[MotionLimits] = MotionLimits(
    output_accel=OutputRpmPerSecond(0.25),
    g_rate=GLoadPerSecond(0.03),
    min_run=MotorRpm(55),
)
"""The plan's numbers, all [MED]: 0.25 output rpm/s, 0.03 g/s, 55 motor rpm."""


def motor_rate_limit(speed: MotorRpm, limits: MotionLimits, geometry: MachineGeometry) -> float:
    """The fastest allowed motor-shaft slew at ``speed``, in motor rpm per second.

    The lower of the two limits. Non-increasing in ``|speed|``: the angular
    limit is constant and the g-dot one falls as the arm speeds up, which is
    what lets :func:`next_setpoint` evaluate it at the faster end of a step.
    """
    angular = output_to_motor_slew(limits.output_accel, geometry.ratio)
    load = g_rate_to_motor_slew(
        limits.g_rate,
        motor_to_output_rpm(MotorRpm(abs(speed)), geometry.ratio),
        geometry.radius,
        geometry.ratio,
    )
    return float(min(angular, load))


def _allowance(speed: MotorRpm, limits: MotionLimits, geometry: MachineGeometry, dt: float) -> int:
    """Whole motor rpm the interval pays for at ``speed``. Never negative."""
    return max(0, math.floor(motor_rate_limit(speed, limits, geometry) * dt))


def next_setpoint(
    current: MotorRpm,
    target: MotorRpm,
    limits: MotionLimits,
    geometry: MachineGeometry,
    dt: Seconds,
) -> MotorRpm:
    """One step from ``current`` towards ``target``, inside every limit. Total, pure.

    ``dt`` is the time since the setpoint last moved (see the module
    docstring), capped at ``limits.max_interval``; a non-finite or
    non-positive ``dt`` moves nothing.

    One direction of rotation only: a negative ``current`` or ``target`` is
    read as 0. A ``target`` inside the gap ``(0, min_run)`` is rounded DOWN to
    0 - rounding up would move the machine faster than it was asked to.
    """
    here = max(0, int(current))
    goal = max(0, int(target))
    if goal < limits.min_run:
        goal = 0
    if not math.isfinite(dt) or dt <= 0.0:
        return MotorRpm(here)
    interval = min(float(dt), float(limits.max_interval))
    if goal > here:
        return MotorRpm(_climb(here, goal, limits, geometry, interval))
    if goal < here:
        return MotorRpm(_descend(here, goal, limits, geometry, interval))
    return MotorRpm(here)


def _climb(here: int, goal: int, limits: MotionLimits, geometry: MachineGeometry, dt: float) -> int:
    """Upwards. From standstill the first step is the passage to ``min_run``."""
    if _allowance(MotorRpm(here), limits, geometry, dt) < 1:
        return here
    if here < limits.min_run:
        return int(limits.min_run)
    first = min(goal, here + _allowance(MotorRpm(here), limits, geometry, dt))
    # Re-evaluated at the faster end of the step, where the g-dot bound is
    # tighter. The limit is non-increasing in speed, so the second step is no
    # larger than the first and the bound it was computed at still holds.
    step = _allowance(MotorRpm(first), limits, geometry, dt)
    if step < 1:
        return here
    return min(goal, here + step)


def _descend(
    here: int, goal: int, limits: MotionLimits, geometry: MachineGeometry, dt: float
) -> int:
    """Downwards, at the same rate: the faster end of a descent is where it starts."""
    step = _allowance(MotorRpm(here), limits, geometry, dt)
    if step < 1:
        return here
    candidate = here - step
    floor = max(goal, int(limits.min_run))
    if candidate >= floor:
        return candidate
    if goal > 0 or here > limits.min_run:
        return floor
    # At (or below) min_run with 0 asked for: the one passage out of the domain's gap.
    return 0


def carry_after(
    current: MotorRpm,
    moved: MotorRpm,
    limits: MotionLimits,
    geometry: MachineGeometry,
    dt: Seconds,
) -> Seconds:
    """The part of ``dt`` a step from ``current`` to ``moved`` did NOT spend.

    The caller restarts its accrual that far in the past rather than at "now",
    so the fraction of an rpm a tick could not pay for is not thrown away (at
    12.4 rpm/s and 5 Hz, flooring every tick would silently run at 10 rpm/s).
    Capped at the time one rpm costs, so what is carried can never add more
    than ONE rpm to the next step: over any window the setpoint moves at most
    ``rate * window + 1`` rpm. The 0 <-> ``min_run`` passage spends all of
    ``dt``, and so does a step that did not move.
    """
    here = max(0, int(current))
    there = max(0, int(moved))
    if not math.isfinite(dt) or dt <= 0.0 or here == there or 0 in (here, there):
        return Seconds(0.0)
    interval = min(float(dt), float(limits.max_interval))
    rate = motor_rate_limit(MotorRpm(max(here, there)), limits, geometry)
    spent = abs(there - here) / rate
    return Seconds(min(max(0.0, interval - spent), 1.0 / rate))


def ramp_duration(
    start: MotorRpm,
    target: MotorRpm,
    limits: MotionLimits,
    geometry: MachineGeometry,
) -> Seconds | None:
    """How long :func:`next_setpoint` takes from ``start`` to ``target``, ticking at 5 Hz.

    Walked rather than integrated, with the same accrual and carry the runtime
    uses, so the answer is what the machine will actually do, integer rpm and
    all. ``None`` when it would not get there within
    :data:`RAMP_ESTIMATE_LIMIT`: limits too slow for this geometry to ever
    move, which a caller must refuse.
    """
    goal = MotorRpm(0 if target < limits.min_run else target)
    here = MotorRpm(max(0, start))
    now = 0.0
    since = 0.0
    while here != goal:
        now += RAMP_ESTIMATE_STEP
        if now > RAMP_ESTIMATE_LIMIT:
            return None
        dt = Seconds(now - since)
        moved = next_setpoint(here, goal, limits, geometry, dt)
        if moved != here:
            since = now - carry_after(here, moved, limits, geometry, dt)
            here = moved
    return Seconds(now)


def count_reversals(setpoints: Sequence[MotorRpm]) -> int:
    """How many times a setpoint sequence changed direction (climb <-> descent).

    Holds are ignored: climb, hold, climb is one climb. This is the counter the
    session validation bounds (a reversal is a fresh vestibular transient).
    """
    reversals = 0
    direction = 0
    for before, after in pairwise(setpoints):
        if after == before:
            continue
        moving = 1 if after > before else -1
        if direction not in (0, moving):
            reversals += 1
        direction = moving
    return reversals


# =========================================================================
# Loading
# =========================================================================


def parse_motion_limits(document: object) -> Result[MotionLimits, str]:
    """Build limits from a decoded JSON object, or say in words why not.

    Keys: ``output_rpm_per_s``, ``g_per_s``, ``min_run_motor_rpm`` and the
    optional ``max_interval_s``. Every value must be a real number (a JSON
    ``true`` is not 1), and the record's own validation then applies.
    """
    if not isinstance(document, dict):
        return Err("the motion limits must be a JSON object")
    # json.loads builds str keys; the values are still unknown and checked below.
    entries = cast("dict[str, object]", document)
    fields: dict[str, float] = {}
    for key in ("output_rpm_per_s", "g_per_s", "min_run_motor_rpm", "max_interval_s"):
        value = entries.get(key)
        if value is None and key == "max_interval_s":
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return Err(f"{key} must be a number, got {value!r}")
        fields[key] = float(value)
    min_run = fields["min_run_motor_rpm"]
    if not min_run.is_integer():
        return Err(f"min_run_motor_rpm must be a whole number of rpm, got {min_run}")
    try:
        limits = MotionLimits(
            output_accel=OutputRpmPerSecond(fields["output_rpm_per_s"]),
            g_rate=GLoadPerSecond(fields["g_per_s"]),
            min_run=MotorRpm(int(min_run)),
            max_interval=Seconds(fields.get("max_interval_s", 1.0)),
        )
    except ValueError as error:
        return Err(str(error))
    return Ok(limits)


def load_motion_limits(path: Path) -> Result[MotionLimits, str]:
    """Read and validate ``config/motion_limits.json``. Startup only."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return Err(f"cannot read {path}: {error}")
    try:
        document: object = json.loads(text)  # pyright: ignore[reportAny]  # narrowed below
    except json.JSONDecodeError as error:
        return Err(f"{path} is not valid JSON: {error}")
    parsed = parse_motion_limits(document)
    if isinstance(parsed, Err):
        return Err(f"{path}: {parsed.error}")
    return parsed
