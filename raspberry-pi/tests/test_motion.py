"""Tests for the motion profiler: slew, g-dot, the domain, and the accrual.

The property tests carry the guarantee (contract rule 7, "coverage is
necessary, not sufficient"): for ANY current setpoint, target and interval,
a step stays in ``{0} union [min_run, ...]``, never overshoots the target, and
never moves more than the rate allows - the 0 <-> min_run passage excepted.
"""

from __future__ import annotations

import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Final, Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.geometry import MachineGeometry
from src.result import Err, Ok
from src.training.motion import (
    DEFAULT_MOTION_LIMITS,
    MotionLimits,
    carry_after,
    count_reversals,
    load_motion_limits,
    motor_rate_limit,
    next_setpoint,
    parse_motion_limits,
    ramp_duration,
)
from src.units import (
    GearRatio,
    GLoadPerSecond,
    Metres,
    MotorRpm,
    OutputRpm,
    OutputRpmPerSecond,
    ResultantG,
    Seconds,
    g_rate_to_motor_slew,
    output_to_motor_slew,
)

ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.5))
LIMITS: Final[MotionLimits] = DEFAULT_MOTION_LIMITS
MIN_RUN: Final[int] = int(LIMITS.min_run)
CEILING: Final[int] = 1600

#: A geometry and limits where the g-dot bound binds inside the tested range:
#: a 10 m arm and a gentle 0.005 g/s.
LONG_ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(10.0))
GENTLE: Final[MotionLimits] = replace(LIMITS, g_rate=GLoadPerSecond(0.005))

in_domain = st.one_of(st.just(0), st.integers(min_value=MIN_RUN, max_value=CEILING))
any_target = st.integers(min_value=-100, max_value=CEILING)
intervals = st.floats(min_value=0.0, max_value=5.0, allow_nan=False)


# =========================================================================
# The record
# =========================================================================


def test_the_defaults_are_the_plans_numbers() -> None:
    assert LIMITS.output_accel == 0.25
    assert LIMITS.g_rate == 0.03
    assert LIMITS.min_run == 55
    assert LIMITS.max_interval == 1.0


class _Positional(Protocol):
    def __call__(self, output_accel: float, g_rate: float, min_run: int, /) -> MotionLimits: ...


def _with(field: str, bad: float) -> MotionLimits:
    """``LIMITS`` with one field replaced, each through its own typed keyword."""
    if field == "output_accel":
        return replace(LIMITS, output_accel=OutputRpmPerSecond(bad))
    if field == "g_rate":
        return replace(LIMITS, g_rate=GLoadPerSecond(bad))
    return replace(LIMITS, max_interval=Seconds(bad))


def test_the_limits_are_frozen_and_keyword_only() -> None:
    with pytest.raises(FrozenInstanceError):
        LIMITS.min_run = MotorRpm(1)  # type: ignore[misc]  # the point of the test
    positional = cast("_Positional", MotionLimits)
    with pytest.raises(TypeError):
        positional(0.25, 0.03, 55)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("output_accel", 0.0),
        ("output_accel", -0.1),
        ("output_accel", math.nan),
        ("output_accel", math.inf),
        ("output_accel", 2.5),
        ("g_rate", 0.0),
        ("g_rate", math.nan),
        ("g_rate", 0.6),
        ("max_interval", 0.0),
        ("max_interval", 2.5),
        ("max_interval", math.nan),
    ],
)
def test_limits_that_bound_nothing_are_refused(field: str, bad: float) -> None:
    """NaN first: every comparison against it is false, so ``<= 0`` would accept it."""
    with pytest.raises(ValueError, match=field):
        _with(field, bad)


@pytest.mark.parametrize("bad", [0, -5, 201])
def test_a_min_run_outside_its_band_is_refused(bad: int) -> None:
    with pytest.raises(ValueError, match="min_run"):
        replace(LIMITS, min_run=MotorRpm(bad))


# =========================================================================
# The rate
# =========================================================================


def test_the_angular_limit_binds_at_this_machines_speeds() -> None:
    """0.25 output rpm/s through i = 49.79 is 12.4 motor rpm/s, up to past 1600 rpm."""
    for rpm in (0, 55, 300, 990, 1380, 1600):
        assert motor_rate_limit(MotorRpm(rpm), LIMITS, ARM) == pytest.approx(0.25 * 49.79)


def test_the_g_dot_limit_binds_when_the_arm_is_long_and_the_limit_gentle() -> None:
    """At standstill the angular limit applies; faster, the g-dot one takes over."""
    angular = 0.25 * 49.79
    assert motor_rate_limit(MotorRpm(0), GENTLE, LONG_ARM) == pytest.approx(angular)
    fast = motor_rate_limit(MotorRpm(1000), GENTLE, LONG_ARM)
    expected = g_rate_to_motor_slew(
        GLoadPerSecond(0.005), OutputRpm(1000 / 49.79), Metres(10.0), GearRatio(49.79)
    )
    assert fast == pytest.approx(expected)
    assert fast < angular


@given(
    low=st.integers(min_value=0, max_value=3000),
    extra=st.integers(min_value=0, max_value=3000),
)
def test_the_rate_never_increases_with_speed(low: int, extra: int) -> None:
    """What lets a step be bounded by the rate at its FASTER end."""
    slow = motor_rate_limit(MotorRpm(low), GENTLE, LONG_ARM)
    fast = motor_rate_limit(MotorRpm(low + extra), GENTLE, LONG_ARM)
    assert fast <= slow + 1e-12


def test_the_unit_helpers_convert_and_refuse_nonsense() -> None:
    assert output_to_motor_slew(OutputRpmPerSecond(0.25), GearRatio(49.79)) == pytest.approx(
        12.4475
    )
    unbounded = g_rate_to_motor_slew(
        GLoadPerSecond(0.03), OutputRpm(0.0), Metres(1.5), GearRatio(49.79)
    )
    assert math.isinf(unbounded)
    # dGc/dt = 2 w r / g0 * dw/dt, checked against a finite difference.
    rpm = OutputRpm(20.0)
    slew = g_rate_to_motor_slew(GLoadPerSecond(0.03), rpm, Metres(1.5), GearRatio(49.79))
    d_out = slew / 49.79 * 0.001
    gc = [(2 * math.pi * n / 60) ** 2 * 1.5 / 9.80665 for n in (rpm, rpm + d_out)]
    assert (gc[1] - gc[0]) / 0.001 == pytest.approx(0.03, rel=1e-3)
    for rate, speed, radius, ratio in (
        (math.nan, 20.0, 1.5, 49.79),
        (0.03, math.inf, 1.5, 49.79),
        (0.03, 20.0, math.nan, 49.79),
        (0.03, 20.0, 1.5, math.nan),
        (0.0, 20.0, 1.5, 49.79),
        (0.03, 20.0, 0.0, 49.79),
        (0.03, 20.0, 1.5, 0.0),
    ):
        assert (
            g_rate_to_motor_slew(
                GLoadPerSecond(rate), OutputRpm(speed), Metres(radius), GearRatio(ratio)
            )
            == 0.0
        )


# =========================================================================
# One step: the properties
# =========================================================================


def _bound(current: int, moved: int, dt: float, limits: MotionLimits, arm: MachineGeometry) -> int:
    interval = min(dt, limits.max_interval)
    return math.floor(motor_rate_limit(MotorRpm(max(current, moved)), limits, arm) * interval)


def _is_passage(current: int, moved: int, limits: MotionLimits) -> bool:
    return {current, moved} == {0, int(limits.min_run)}


@settings(max_examples=400)
@given(current=in_domain, target=any_target, dt=intervals, gentle=st.booleans())
def test_a_step_stays_in_the_domain_moves_towards_the_target_and_within_the_rate(
    current: int, target: int, dt: float, *, gentle: bool
) -> None:
    limits, arm = (GENTLE, LONG_ARM) if gentle else (LIMITS, ARM)
    moved = int(next_setpoint(MotorRpm(current), MotorRpm(target), limits, arm, Seconds(dt)))
    goal = 0 if target < limits.min_run else target
    # The domain.
    assert moved == 0 or moved >= limits.min_run
    # Towards the target, never past it.
    assert min(current, goal) <= moved <= max(current, goal)
    # Within the rate, bar the one passage, which still needs a paid-for rpm.
    if moved != current:
        allowance = math.floor(
            motor_rate_limit(MotorRpm(current), limits, arm) * min(dt, limits.max_interval)
        )
        assert allowance >= 1
        if not _is_passage(current, moved, limits):
            assert abs(moved - current) <= _bound(current, moved, dt, limits, arm)


@given(current=in_domain, target=any_target, dt=st.sampled_from([math.nan, -1.0, 0.0, math.inf]))
def test_a_meaningless_interval_moves_nothing(current: int, target: int, dt: float) -> None:
    assert next_setpoint(MotorRpm(current), MotorRpm(target), LIMITS, ARM, Seconds(dt)) == current


def test_a_target_in_the_gap_is_rounded_down_to_zero() -> None:
    """Rounding up would move the machine faster than it was asked to."""
    assert next_setpoint(MotorRpm(0), MotorRpm(30), LIMITS, ARM, Seconds(0.2)) == 0
    assert next_setpoint(MotorRpm(55), MotorRpm(30), LIMITS, ARM, Seconds(0.2)) == 0
    assert next_setpoint(MotorRpm(55), MotorRpm(-30), LIMITS, ARM, Seconds(0.2)) == 0


def test_leaving_standstill_is_the_passage_to_min_run_once_an_rpm_is_paid_for() -> None:
    assert next_setpoint(MotorRpm(0), MotorRpm(300), LIMITS, ARM, Seconds(0.05)) == 0
    assert next_setpoint(MotorRpm(0), MotorRpm(300), LIMITS, ARM, Seconds(0.2)) == MIN_RUN


def test_a_setpoint_outside_the_domain_is_brought_into_it() -> None:
    """A negative reading is read as 0; one in the gap climbs to min_run or drops to 0."""
    assert next_setpoint(MotorRpm(-40), MotorRpm(300), LIMITS, ARM, Seconds(0.2)) == MIN_RUN
    assert next_setpoint(MotorRpm(30), MotorRpm(300), LIMITS, ARM, Seconds(0.2)) == MIN_RUN
    assert next_setpoint(MotorRpm(30), MotorRpm(0), LIMITS, ARM, Seconds(0.2)) == 0


def test_a_descent_stops_at_min_run_before_the_passage_to_zero() -> None:
    assert next_setpoint(MotorRpm(57), MotorRpm(0), LIMITS, ARM, Seconds(0.4)) == MIN_RUN
    assert next_setpoint(MotorRpm(MIN_RUN), MotorRpm(0), LIMITS, ARM, Seconds(0.2)) == 0
    assert next_setpoint(MotorRpm(300), MotorRpm(299), LIMITS, ARM, Seconds(0.2)) == 299


def test_a_climb_the_faster_end_cannot_pay_for_does_not_move() -> None:
    """One rpm is paid for at the current speed but not at the next: the g-dot bound wins."""
    start = MotorRpm(500)
    rate_here = motor_rate_limit(start, GENTLE, LONG_ARM)
    dt = Seconds((1.0 / rate_here) * (1.0 + 1e-9))
    assert dt < GENTLE.max_interval
    assert math.floor(rate_here * dt) == 1
    assert math.floor(motor_rate_limit(MotorRpm(501), GENTLE, LONG_ARM) * dt) == 0
    assert next_setpoint(start, MotorRpm(600), GENTLE, LONG_ARM, dt) == start


def test_the_interval_is_capped_so_a_long_hold_banks_nothing() -> None:
    capped = next_setpoint(MotorRpm(300), MotorRpm(1300), LIMITS, ARM, Seconds(60.0))
    assert capped == 300 + math.floor(12.4475 * LIMITS.max_interval)


# =========================================================================
# The accrual and the carry
# =========================================================================


def test_the_carry_keeps_the_fraction_a_tick_could_not_pay_for() -> None:
    rate = motor_rate_limit(MotorRpm(300), LIMITS, ARM)
    carry = carry_after(MotorRpm(300), MotorRpm(302), LIMITS, ARM, Seconds(0.2))
    assert carry == pytest.approx(0.2 - 2 / rate)
    # Never more than one rpm's worth.
    long = carry_after(MotorRpm(300), MotorRpm(301), LIMITS, ARM, Seconds(1.0))
    assert long == pytest.approx(1.0 / rate)


@pytest.mark.parametrize(
    ("current", "moved", "dt"),
    [(0, 55, 0.2), (55, 0, 0.2), (300, 300, 0.2), (300, 302, math.nan), (300, 302, 0.0)],
)
def test_nothing_is_carried_across_the_passage_or_a_non_step(
    current: int, moved: int, dt: float
) -> None:
    assert carry_after(MotorRpm(current), MotorRpm(moved), LIMITS, ARM, Seconds(dt)) == 0.0


@settings(max_examples=150)
@given(
    targets=st.lists(st.tuples(any_target, st.integers(1, 40)), min_size=1, max_size=12),
    tick=st.sampled_from([0.1, 0.2, 0.25, 0.5]),
    gentle=st.booleans(),
)
def test_over_any_run_each_change_is_bounded_by_the_time_since_the_last_one_plus_one_rpm(
    targets: list[tuple[int, int]], tick: float, *, gentle: bool
) -> None:
    """The runtime's own loop, walked: accrue, step, carry. The +1 is the carry, at most."""
    limits, arm = (GENTLE, LONG_ARM) if gentle else (LIMITS, ARM)
    here = MotorRpm(0)
    now = 0.0
    since = 0.0
    last_change = 0.0
    for target, ticks in targets:
        for _ in range(ticks):
            now += tick
            dt = Seconds(now - since)
            moved = next_setpoint(here, MotorRpm(target), limits, arm, dt)
            if moved != here:
                if not _is_passage(here, moved, limits):
                    fastest = motor_rate_limit(MotorRpm(max(here, moved)), limits, arm)
                    window = min(now - last_change, float(limits.max_interval) + 1 / fastest)
                    assert abs(moved - here) <= fastest * window + 1 + 1e-9
                since = now - carry_after(here, moved, limits, arm, dt)
                last_change = now
                here = moved
            assert here == 0 or here >= limits.min_run


# =========================================================================
# Ramp time and reversals
# =========================================================================


def test_the_ramp_to_300_rpm_takes_about_twenty_seconds() -> None:
    """0 -> 55 is the free passage, then 245 rpm at 12.4 rpm/s."""
    duration = ramp_duration(MotorRpm(0), MotorRpm(300), LIMITS, ARM)
    assert duration is not None
    assert 19.0 <= duration <= 21.0
    assert ramp_duration(MotorRpm(300), MotorRpm(0), LIMITS, ARM) == pytest.approx(duration)


def test_the_ramp_to_one_and_a_half_g_takes_about_a_hundred_seconds() -> None:
    """The plan's figure: 0 -> 1.5 Gr (1286 motor rpm) in roughly 105 s."""
    duration = ramp_duration(MotorRpm(0), ARM.motor_rpm_for(ResultantG(1.5)), LIMITS, ARM)
    assert duration is not None
    assert 95.0 <= duration <= 105.0


def test_a_ramp_to_where_it_already_is_takes_no_time() -> None:
    assert ramp_duration(MotorRpm(300), MotorRpm(300), LIMITS, ARM) == 0.0
    assert ramp_duration(MotorRpm(0), MotorRpm(20), LIMITS, ARM) == 0.0


def test_limits_too_slow_to_ever_move_have_no_ramp_time() -> None:
    crawling = replace(LIMITS, output_accel=OutputRpmPerSecond(0.01))
    assert ramp_duration(MotorRpm(0), MotorRpm(300), crawling, ARM) is None


@pytest.mark.parametrize(
    ("setpoints", "expected"),
    [
        ((), 0),
        ((0,), 0),
        ((0, 55, 60, 60, 70), 0),
        ((0, 55, 60, 50, 0), 1),
        ((0, 55, 60, 60, 50, 50, 70, 0), 3),
    ],
)
def test_reversals_are_counted_across_holds(setpoints: tuple[int, ...], expected: int) -> None:
    assert count_reversals([MotorRpm(value) for value in setpoints]) == expected


# =========================================================================
# Loading
# =========================================================================

GOOD_DOCUMENT: Final[dict[str, object]] = {
    "output_rpm_per_s": 0.25,
    "g_per_s": 0.03,
    "min_run_motor_rpm": 55,
    "max_interval_s": 1.0,
}


def test_the_shipped_file_loads_the_defaults() -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "motion_limits.json"
    assert load_motion_limits(shipped) == Ok(LIMITS)


def test_the_interval_is_optional() -> None:
    document = {key: value for key, value in GOOD_DOCUMENT.items() if key != "max_interval_s"}
    assert parse_motion_limits(document) == Ok(LIMITS)


@pytest.mark.parametrize(
    ("document", "match"),
    [
        ([], "JSON object"),
        ({**GOOD_DOCUMENT, "g_per_s": True}, "g_per_s must be a number"),
        ({**GOOD_DOCUMENT, "output_rpm_per_s": "fast"}, "output_rpm_per_s must be a number"),
        ({key: v for key, v in GOOD_DOCUMENT.items() if key != "g_per_s"}, "g_per_s"),
        ({**GOOD_DOCUMENT, "min_run_motor_rpm": 55.5}, "whole number"),
        ({**GOOD_DOCUMENT, "output_rpm_per_s": 9.0}, "output_accel"),
    ],
)
def test_a_bad_document_is_refused_in_words(document: object, match: str) -> None:
    parsed = parse_motion_limits(document)
    assert isinstance(parsed, Err)
    assert match in parsed.error


def test_a_file_that_cannot_be_read_or_parsed_is_refused(tmp_path: Path) -> None:
    missing = load_motion_limits(tmp_path / "absent.json")
    assert isinstance(missing, Err)
    assert "cannot read" in missing.error
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    broken = load_motion_limits(garbage)
    assert isinstance(broken, Err)
    assert "not valid JSON" in broken.error
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({**GOOD_DOCUMENT, "g_per_s": -1}), encoding="utf-8")
    refused = load_motion_limits(wrong)
    assert isinstance(refused, Err)
    assert str(wrong) in refused.error
