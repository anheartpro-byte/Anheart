"""The failure-injection matrix (:mod:`simulation.failures`), case by case.

Each case asserts, through :func:`simulation.failures.judge`: every invariant
(motor at 0, no torque, no NaN, ...), the output disabled or the drive's own
watchdog engaged, the right end reason and verdict, an actionable operator
message, the phase it claims to test, and (ECG) no false heart rate. A case
carrying ``known_defect`` is a strict xfail: correct behaviour ``raspberry-pi``
does not deliver today, with the evidence in the reason.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from typing import Final

import pytest

from simulation.failures import (
    AUTO_PHASES,
    MANUAL_PHASES,
    Category,
    FailureCase,
    cases,
    judge,
    run_case,
    scenario_of,
)
from simulation.harness import ENERGISED, RunResult

CASES: Final[tuple[FailureCase, ...]] = cases()
_RUNS: Final[dict[str, RunResult]] = {}


def _run(case: FailureCase) -> RunResult:
    """Each case is run once per test session, whatever asks about it."""
    cached = _RUNS.get(case.name)
    if cached is None:
        cached = asyncio.run(run_case(case))
        _RUNS[case.name] = cached
    return cached


def _params() -> Iterator[object]:
    for case in CASES:
        marks = (
            ()
            if case.known_defect is None
            else (pytest.mark.xfail(strict=True, reason=case.known_defect),)
        )
        yield pytest.param(case, marks=marks, id=case.name)


@pytest.mark.parametrize("case", list(_params()))
def test_the_failure_is_handled(case: FailureCase) -> None:
    result = _run(case)
    violations = judge(case, result)
    assert not violations, "\n".join(str(violation) for violation in violations)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_no_failure_leaves_the_motor_turning(case: FailureCase) -> None:
    """Known defect or not: after teardown, no torque and the shaft at rest."""
    final = _run(case).trace.final
    assert final.sim_state not in {state.name for state in ENERGISED}, final
    assert final.shaft_motor_rpm == 0, final


def test_the_matrix_covers_every_category_and_every_phase() -> None:
    assert {case.category for case in CASES} == set(Category)
    names = {case.name for case in CASES}
    assert len(names) == len(CASES)
    for phase in AUTO_PHASES:
        for family in ("drive_comm_timeout_auto", "ecg_disconnect", "process_sigterm_auto"):
            assert f"{family}_{phase}" in names
    for stage in MANUAL_PHASES:
        assert f"drive_comm_timeout_manual_{stage}" in names
        assert f"process_sigterm_manual_{stage}" in names


def test_every_case_is_a_valid_scenario_document() -> None:
    for case in CASES:
        assert scenario_of(case).name == case.name


def test_an_invalid_case_document_is_refused() -> None:
    broken = FailureCase(
        name="broken",
        category=Category.OPERATOR,
        description="",
        document={"name": "broken", "kind": "sideways", "duration_s": 1.0},
        end_reasons=frozenset(),
    )
    with pytest.raises(ValueError, match="sideways"):
        scenario_of(broken)


# -- the judge fires on what it is meant to catch ---------------------------------


def _probe() -> tuple[FailureCase, RunResult]:
    case = next(case for case in CASES if case.name == "drive_comm_timeout_manual_at_speed")
    return case, _run(case)


def _checks(case: FailureCase, result: RunResult) -> set[str]:
    return {violation.check for violation in judge(case, result)}


def test_the_judge_names_each_broken_promise() -> None:
    case, result = _probe()
    assert _checks(case, result) == set()
    assert "failure_silent" in _checks(replace(case, silent=False), result)
    assert "failure_end" in _checks(replace(case, end_reasons=frozenset({"shutdown"})), result)
    assert "failure_phase" in _checks(replace(case, phase="warmup"), result)
    assert "failure_rule" in _checks(replace(case, rules=("hr_drop",)), result)
    late = replace(case, rules=("comms_lost", "hr_drop"), deadline=1.0)
    assert {"failure_late", "failure_rule"} <= _checks(late, result)
    assert "failure_message" in _checks(replace(case, messages=("never said",)), result)
    assert "failure_still_turning" not in _checks(replace(case, stopped_by=250.0), result)
    rows = tuple(replace(row, silent=False) for row in result.trace.rows)
    unsilent = replace(result, trace=replace(result.trace, rows=rows))
    turning = replace(case, silent=None, stopped_by=100.0)
    assert "failure_still_turning" in _checks(turning, unsilent)


def test_the_judge_sees_a_missing_watchdog_and_a_held_output() -> None:
    case, result = _probe()
    rows = tuple(replace(row, sim_state="OPERATION_ENABLED") for row in result.trace.rows)
    final = replace(result.trace.final, sim_state="SWITCH_ON_DISABLED")
    blind = replace(result, trace=replace(result.trace, rows=rows, final=final))
    assert "failure_watchdog" in _checks(case, blind)
    held = replace(result.trace.final, silent=False, runtime_output_enabled=True)
    holding = replace(result, trace=replace(result.trace, final=held))
    assert "failure_output" in _checks(replace(case, silent=None), holding)


def test_the_idle_check_passes_once_the_turning_drive_is_handled() -> None:
    case = next(case for case in CASES if case.name == "drive_stuck_enabled_idle_console")
    result = _run(case)
    handled = replace(result, preroll=replace(result.preroll, rules=("drive_precommanded",)))
    assert "idle_left_turning" not in _checks(case, handled)


def test_an_idle_console_leaving_a_drive_turning_unflagged_is_named() -> None:
    """The idle-console check on a corrupted trace: the shaft still turning, no verdict.

    The runtime no longer does this (it stops the drive and latches
    drive_precommanded on its first idle poll), so the real run is clean and
    the failure is injected into its pre-roll record, as for every other check.
    """
    case = next(c for c in CASES if c.name == "drive_stuck_enabled_idle_console")
    result = _run(case)
    assert not [v for v in judge(case, result) if v.check == "idle_left_turning"]
    left = replace(result.preroll, final_motor_rpm=900, rules=())
    corrupted = replace(result, preroll=left)
    assert [v for v in judge(case, corrupted) if v.check == "idle_left_turning"]
