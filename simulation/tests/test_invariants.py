"""The checker itself: every invariant and expectation must FIRE on a trace that breaks it.

A clean battery proves nothing about a checker that can never fail. Each test
here takes a real, clean run and corrupts one thing about it - a setpoint in
the min-run gap, a jump faster than the slew, a verdict ignored, a motor left
turning - and asserts the right check names it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from simulation.harness import RunResult, TargetOutcome
from simulation.invariants import (
    check_expectations,
    check_invariants,
    check_motion_limits,
    in_zone_fraction,
    measure,
)
from simulation.scenario import SCENARIO_DIR, Expect, Expectation
from simulation.tests.conftest import run_file
from simulation.tracefile import JsonValue, Row
from src.training.runtime import EndReason, RuntimeState
from src.units import OutputRpm, Seconds

MANUAL: Final = SCENARIO_DIR / "manual_27_rpm.json"
AUTO: Final = SCENARIO_DIR / "auto_jog_150_nominal.json"


def _checks(result: RunResult) -> set[str]:
    return {
        violation.check for violation in (*check_invariants(result), *check_expectations(result))
    }


def _with_rows(result: RunResult, rows: list[Row]) -> RunResult:
    return replace(result, trace=replace(result.trace, rows=tuple(rows)))


def _cruising(result: RunResult) -> int:
    """The index of a row in the middle of steady running at 1344 rpm."""
    rows = result.trace.rows
    return next(i for i, row in enumerate(rows) if row.t > 150.0)


def _edit(path: Path, index: int, **changes: object) -> RunResult:
    result = run_file(path)
    rows = list(result.trace.rows)
    rows[index] = replace(rows[index], **changes)  # type: ignore[arg-type]  # a field splat
    return _with_rows(result, rows)


def test_the_clean_runs_are_clean() -> None:
    assert not _checks(run_file(MANUAL))
    assert not check_invariants(run_file(AUTO))


def test_a_non_finite_value_is_named() -> None:
    result = run_file(MANUAL)
    assert "finite" in _checks(_edit(MANUAL, _cruising(result), g_leg_tip=math.nan))


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"setpoint_motor_rpm": 30}, "setpoint_domain"),
        ({"setpoint_motor_rpm": 2000}, "setpoint_domain"),
        ({"measured_motor_rpm": 2000}, "measured_bound"),
        ({"measured_motor_rpm": -50}, "reverse"),
    ],
)
def test_the_domain_checks_fire(changes: dict[str, object], check: str) -> None:
    index = _cruising(run_file(MANUAL))
    assert check in _checks(_edit(MANUAL, index, **changes))


def test_a_value_written_outside_the_domain_is_named() -> None:
    result = run_file(MANUAL)
    frames: tuple[Mapping[str, JsonValue], ...] = (
        *result.trace.frames,
        {"type": "frame", "t": 1.0, "kind": "speed", "value": 30, "label": "ok", "ok": True},
        {"type": "frame", "t": 1.0, "kind": "speed", "value": None, "label": "ok", "ok": True},
    )
    corrupted = replace(result, trace=replace(result.trace, frames=frames))
    assert "written_domain" in _checks(corrupted)


@pytest.mark.parametrize("path", [MANUAL, AUTO], ids=["manual", "auto"])
def test_a_setpoint_faster_than_promised_is_named(path: Path) -> None:
    result = run_file(path)
    rows = list(result.trace.rows)
    index = next(i for i, row in enumerate(rows) if row.setpoint_motor_rpm >= 900)
    up = _with_rows(
        result, [*rows[:index], replace(rows[index], setpoint_motor_rpm=1300), *rows[index + 1 :]]
    )
    assert "slew_up" in _checks(up)
    down = replace(rows[index], setpoint_motor_rpm=400, safety_action="NONE")
    assert "slew_down" in _checks(_with_rows(result, [*rows[:index], down, *rows[index + 1 :]]))


def test_a_manual_g_rate_jump_is_named() -> None:
    result = run_file(MANUAL)
    index = _cruising(result)
    assert "g_rate" in _checks(_edit(MANUAL, index, setpoint_motor_rpm=900))


def test_a_shaft_faster_than_the_drive_ramp_is_named() -> None:
    result = run_file(MANUAL)
    index = _cruising(result)
    assert "measured_rate" in _checks(_edit(MANUAL, index, measured_motor_rpm=100))


def test_a_rise_under_a_verdict_is_named() -> None:
    result = run_file(MANUAL)
    rows = list(result.trace.rows)
    index = next(i for i, row in enumerate(rows) if 500 < row.setpoint_motor_rpm < 1300)
    rows[index] = replace(rows[index], safety_action="FREEZE", safety_rule="hr_stale")
    assert "safety_dominates" in _checks(_with_rows(result, rows))


def test_a_rise_after_the_end_began_is_named() -> None:
    result = run_file(MANUAL)
    rows = list(result.trace.rows)
    index = next(i for i, row in enumerate(rows) if 500 < row.setpoint_motor_rpm < 1300)
    rows[index - 1] = replace(rows[index - 1], state="ending")
    assert "rise_after_end" in _checks(_with_rows(result, rows))


def test_an_acceleration_during_a_vasovagal_collapse_is_named() -> None:
    """A setpoint rise inside the collapse window is named.

    This used to be the real defect, shown by the unmodified run; the runtime
    no longer accelerates during a collapse (its gate holds the setpoint), so the
    fixed run is clean and a rise is injected into it, as for every other check.
    """
    result = run_file(SCENARIO_DIR / "vasovagal_auto_hold.json")
    assert "vasovagal_no_accel" not in _checks(result)
    rows = [
        replace(row, setpoint_motor_rpm=row.setpoint_motor_rpm + 10)
        if 905.0 <= row.t <= 906.0
        else row
        for row in result.trace.rows
    ]
    assert "vasovagal_no_accel" in _checks(_with_rows(result, rows))


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"energised": True}, "exit_energised"),
        ({"shaft_motor_rpm": None}, "exit_unknown"),
        ({"shaft_motor_rpm": 40}, "exit_turning"),
        ({"lfrd_motor_rpm": 100}, "exit_reference"),
        ({"runtime_applied_rpm": 100}, "exit_belief"),
    ],
)
def test_every_exit_failure_is_named(changes: dict[str, object], check: str) -> None:
    result = run_file(MANUAL)
    final = replace(result.trace.final, **changes)  # type: ignore[arg-type]  # a field splat
    assert check in _checks(replace(result, trace=replace(result.trace, final=final)))


def test_a_frame_after_going_silent_is_named() -> None:
    result = run_file(MANUAL)
    rows = [replace(row, silent=row.t > 100.0) for row in result.trace.rows]
    assert "silent_wrote" in _checks(_with_rows(result, rows))


def test_a_refused_start_that_moved_is_named() -> None:
    result = replace(run_file(MANUAL), start_refusal="DriveInFault")
    assert "refused_moved" in _checks(result)


def test_the_arm_limits_fire_on_a_fast_measured_ramp() -> None:
    result = run_file(MANUAL)
    rows = list(result.trace.rows)
    index = _cruising(result)
    for offset in range(6):
        rows[index + offset] = replace(rows[index + offset], measured_motor_rpm=1100 + 40 * offset)
    names = {v.check for v in check_motion_limits(_with_rows(result, rows))}
    assert names == {"arm_accel", "arm_g_rate"}
    assert check_motion_limits(_with_rows(result, rows), at_leg_tip=True)


def _expecting(result: RunResult, **changes: object) -> set[str]:
    expect = replace(result.scenario.expect, **changes)  # type: ignore[arg-type]  # a field splat
    return {
        v.check
        for v in check_expectations(
            replace(result, scenario=replace(result.scenario, expect=expect))
        )
    }


def test_every_expectation_is_checked() -> None:
    result = run_file(MANUAL)
    assert "expect_start" in _expecting(result, start=Expectation.REFUSED)
    assert "expect_start" in {
        v.check for v in check_expectations(replace(result, start_refusal="PlanUnusable"))
    }
    assert "expect_end" in _expecting(result, end_reason=EndReason.EMERGENCY_STOP)
    assert "expect_state" in _expecting(result, final_state=RuntimeState.IDLE)
    assert "expect_rule" in _expecting(result, rules=("hr_drop",))
    assert "expect_reach" in _expecting(result, reaches_output_rpm=OutputRpm(30.0))
    assert "expect_max" in _expecting(result, max_output_rpm=OutputRpm(10.0))
    assert "expect_zone" in _expecting(result, min_in_zone_fraction=0.5)
    assert not _expecting(result, start=Expectation.ANY)
    fired = run_file(SCENARIO_DIR / "estop_manual_27.json")
    assert "forbid_rule" in _expecting(fired, forbid_rules=("operator_estop",))


def test_a_target_outcome_that_contradicts_its_expectation_is_named() -> None:
    result = run_file(MANUAL)
    wrong = (
        TargetOutcome(
            at=Seconds(1.0),
            requested=OutputRpm(5.0),
            expected=Expectation.REFUSED,
            accepted=True,
            detail="a",
        ),
        TargetOutcome(
            at=Seconds(2.0),
            requested=OutputRpm(50.0),
            expected=Expectation.ACCEPTED,
            accepted=False,
            detail="r",
        ),
    )
    violations = check_expectations(
        replace(result, targets=wrong, scenario=replace(result.scenario, expect=Expect()))
    )
    assert [v.check for v in violations] == ["expect_target", "expect_target"]


def test_the_zone_fraction_needs_a_programme_and_a_live_rate() -> None:
    assert in_zone_fraction(run_file(MANUAL)) is None
    auto = run_file(AUTO)
    hold_free = _with_rows(auto, [row for row in auto.trace.rows if row.phase != "hold"])
    assert in_zone_fraction(hold_free) is None
    fraction = in_zone_fraction(auto)
    assert fraction is not None
    assert 0.5 <= fraction <= 1.0


def test_the_metrics_of_a_run_that_never_moved_are_zero() -> None:
    metrics = measure(run_file(SCENARIO_DIR / "fault_drive_already_faulted.json"))
    assert metrics.peak_output_rpm == 0.0
    assert metrics.peak_arm_accel_output_rpm_s == 0.0
    assert metrics.rules == ("drive_fault",)
    assert str(check_invariants(replace(run_file(MANUAL), start_refusal="X"))[0]).startswith(
        "[refused_moved]"
    )


def test_an_emergency_descent_is_exempt_from_the_ramp_limits() -> None:
    """QUICK_STOP drops the reference on the drive's own ramp: not a motion-profile step."""
    result = run_file(MANUAL)
    index = _cruising(result)
    corrupted = _edit(MANUAL, index, setpoint_motor_rpm=900, safety_action="QUICK_STOP")
    names = _checks(corrupted)
    assert "slew_down" not in names
    # the climb back from 900 to 1344 on the next tick is still caught
    assert "slew_up" in names


def test_the_metrics_of_real_runs() -> None:
    manual = measure(run_file(MANUAL))
    assert manual.peak_motor_rpm == 1344
    assert manual.peak_setpoint_rate_output_rpm_s == pytest.approx(0.30, abs=0.02)
    assert manual.peak_arm_accel_output_rpm_s <= 0.25 + 2 / 49.79
    auto = measure(run_file(AUTO))
    assert auto.peak_arm_accel_output_rpm_s > 0.25  # the programme finding, see test_limits


def test_an_operator_request_that_went_the_wrong_way_is_named() -> None:
    from simulation.harness import RequestOutcome  # noqa: PLC0415
    from simulation.invariants import check_expectations  # noqa: PLC0415

    result = run_file(SCENARIO_DIR / "manual_27_rpm.json")
    wrong = RequestOutcome(
        at=Seconds(1.0), request="start", expected=Expectation.REFUSED, accepted=True, detail="ok"
    )
    right = replace(wrong, expected=Expectation.ANY)
    checks = check_expectations(replace(result, requests=(wrong, right)))
    assert [v.check for v in checks] == ["expect_request"]
