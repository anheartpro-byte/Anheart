"""Speeds, g-loads and the limits that bound them: what the operator asked about.

* the g-load at 27 and 32 output rpm, at the reference radius and at the leg tip;
* 27 output rpm is reachable on the bench, 32 is refused (it needs 57.7 Hz,
  above the nameplate and the 50 Hz HSP this runtime arms against) - the refusal
  is the correct behaviour and is asserted as such;
* the anti-nausea limits, judged on the MEASURED arm speed: held by manual
  sessions; two findings are recorded as strict xfails (see their reasons).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from simulation.harness import run_scenario
from simulation.invariants import check_motion_limits, in_zone_fraction
from simulation.rig import load_geometry
from simulation.scenario import SCENARIO_DIR, ManualSpec, SessionKind, scenario_paths
from simulation.tests.conftest import load, run_file
from src.result import Ok
from src.units import Hertz, MotorRpm, OutputRpm, motor_rpm_to_hertz, output_to_motor_rpm

STANDARD_GRAVITY: Final[float] = 9.80665


def _g(rpm: float, radius: float) -> float:
    """Hand-derived here, independently of src.units: g = (2 pi n / 60)^2 r / g0."""
    omega = 2.0 * math.pi * rpm / 60.0
    return omega * omega * radius / STANDARD_GRAVITY


@pytest.mark.parametrize(
    ("rpm", "reference", "leg_tip", "estimate"),
    [(27.0, 1.2228, 1.9772, 1.1590), (32.0, 1.7176, 2.7773, 1.6281)],
)
def test_the_g_load_at_27_and_32_output_rpm(
    rpm: float, reference: float, leg_tip: float, estimate: float
) -> None:
    loaded = load_geometry()
    assert isinstance(loaded, Ok)
    rig = loaded.value
    assert rig.g_reference(OutputRpm(rpm)) == pytest.approx(reference, abs=1e-4)
    assert rig.g_leg_tip(OutputRpm(rpm)) == pytest.approx(leg_tip, abs=1e-4)
    assert _g(rpm, float(rig.leg_tip_radius)) == pytest.approx(leg_tip, abs=1e-4)
    assert _g(rpm, 1.4218) == pytest.approx(estimate, abs=1e-4)


def test_27_output_rpm_is_reached_on_the_bench() -> None:
    result = run_file(SCENARIO_DIR / "manual_27_rpm.json")
    (target,) = result.targets
    assert target.accepted
    assert target.detail.endswith("1344 motor rpm")
    peak = max(row.measured_motor_rpm for row in result.trace.rows)
    assert peak == 1344
    assert max(row.hertz for row in result.trace.rows) == pytest.approx(48.7, abs=0.05)
    assert max(row.g_leg_tip for row in result.trace.rows) == pytest.approx(1.977, abs=0.002)


def test_32_output_rpm_is_refused_and_nothing_moves() -> None:
    result = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json")
    (target,) = result.targets
    assert not target.accepted
    assert "TargetOutOfRange" in target.detail
    assert "ceiling=1380" in target.detail
    assert all(row.setpoint_motor_rpm == 0 for row in result.trace.rows)


def test_32_output_rpm_needs_57_7_hz_above_the_nameplate() -> None:
    loaded = load_geometry()
    assert isinstance(loaded, Ok)
    motor = output_to_motor_rpm(OutputRpm(32.0), loaded.value.machine.ratio)
    assert motor == 1593
    hertz = motor_rpm_to_hertz(motor, MotorRpm(1380), Hertz(50.0))
    assert hertz == pytest.approx(57.72, abs=0.01)


def test_a_bench_ceiling_above_the_hsp_is_refused_at_the_start() -> None:
    """A ceiling of 1593 motor rpm (32 output rpm) cannot even be armed: HSP is 50 Hz."""
    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    scenario = replace(base, manual=ManualSpec(ceiling=MotorRpm(1593)))
    result = asyncio.run(run_scenario(scenario))
    assert result.start_refusal == "PlanUnusable"
    assert all(row.setpoint_motor_rpm == 0 for row in result.trace.rows)


MANUAL: Final[tuple[Path, ...]] = tuple(
    p for p in scenario_paths() if load(p).kind is SessionKind.MANUAL
)


@pytest.mark.parametrize("path", MANUAL, ids=[p.stem for p in MANUAL])
def test_manual_sessions_hold_the_anti_nausea_limits_on_the_measured_arm(path: Path) -> None:
    violations = check_motion_limits(run_file(path))
    assert not violations, "\n".join(str(v) for v in violations[:10])


def test_programmed_sessions_hold_the_anti_nausea_limits() -> None:
    """Was a strict xfail: the control law stepped the setpoint by up to 75 motor rpm per 5 s
    period and the measured arm reached ~0.54 output rpm/s (limit 0.25), with no g-dot limit.
    Every non-emergency programme setpoint change now walks the motion profiler."""
    violations = check_motion_limits(run_file(SCENARIO_DIR / "auto_jog_150_nominal.json"))
    assert not violations, "\n".join(str(v) for v in violations[:10])


def test_the_g_rate_limit_holds_at_the_leg_tip_too() -> None:
    violations = check_motion_limits(run_file(SCENARIO_DIR / "manual_27_rpm.json"), at_leg_tip=True)
    assert not violations, "\n".join(str(v) for v in violations[:10])


@pytest.mark.parametrize("name", ["auto_standard_30_min", "auto_standard_45_min"])
def test_the_shipped_profiles_cannot_reach_their_zone_and_saturate_safely(name: str) -> None:
    """FINDING, not a defect: max_rpm 276 (5.5 output rpm, 0.05 g) leaves 118-138 bpm out of reach.

    The correct behaviour - asserted - is to saturate at the ceiling, never exceed
    it, and complete: the unreachable zone must not become "push harder".
    """
    result = run_file(SCENARIO_DIR / f"{name}.json")
    profile = result.profile
    assert profile is not None
    assert max(row.setpoint_motor_rpm for row in result.trace.rows) == profile.max_rpm
    assert max(row.hr_true or 0 for row in result.trace.rows) < profile.zone_low_bpm
    assert not in_zone_fraction(result)
    assert result.trace.final.end_reason == "programme_complete"
