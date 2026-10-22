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
from collections.abc import Iterator, Mapping
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
from simulation.harness import ENERGISED, RequestOutcome, RunResult
from simulation.scenario import Expectation
from simulation.tracefile import JsonValue
from src.motor.sto import StoState
from src.units import Seconds

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
        for family in (
            "drive_comm_timeout_auto",
            "ecg_disconnect",
            "process_sigterm_auto",
            "sto_open_auto",
        ):
            assert f"{family}_{phase}" in names
    for stage in MANUAL_PHASES:
        assert f"drive_comm_timeout_manual_{stage}" in names
        assert f"process_sigterm_manual_{stage}" in names
        assert f"sto_open_manual_{stage}" in names


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


def test_sto_closure_keeps_the_latch_until_the_end_of_the_scenario() -> None:
    case = next(case for case in CASES if case.name == "sto_close_without_acknowledgement")
    result = _run(case)
    assert result.trace.rows[-1].t >= 329 - 1e-6
    assert all(view.latched for view in result.sto if view.at >= 170 - 1e-6)
    assert all(not request.accepted for request in result.requests)
    assert result.sto[-1].state is StoState.READY


@pytest.mark.parametrize("stage", MANUAL_PHASES)
def test_sto_injection_observes_each_manual_motion_stage(stage: str) -> None:
    case = next(case for case in CASES if case.name == f"sto_open_manual_{stage}")
    result = _run(case)
    at = MANUAL_PHASES[stage]
    before = next(row for row in reversed(result.trace.rows) if row.t < at - 1e-6)
    earlier = next(row for row in reversed(result.trace.rows) if row.t < at - 1)
    if stage == "ramp_up":
        assert 0 < before.setpoint_motor_rpm < before.manual_target_motor_rpm
        assert earlier.setpoint_motor_rpm < before.setpoint_motor_rpm
    elif stage == "at_speed":
        assert before.setpoint_motor_rpm == before.manual_target_motor_rpm > 0
        assert before.measured_motor_rpm == before.setpoint_motor_rpm
    else:
        assert earlier.setpoint_motor_rpm > before.setpoint_motor_rpm > 0
        assert before.phase == "cooldown"


def test_sto_acknowledgement_requires_ready_and_does_not_resume_a_session() -> None:
    case = next(case for case in CASES if case.name == "sto_acknowledgement_then_new_start")
    result = _run(case)
    acknowledgements = [request for request in result.requests if request.request == "acknowledge"]
    assert [request.accepted for request in acknowledgements] == [False, True]
    assert "StoNotReady" in acknowledgements[0].detail
    assert any(
        event.actor == "sim-operator"
        for event in result.trace.events
        if event.kind.value == "verdict_ack"
    )
    assert all(row.setpoint_motor_rpm == 0 for row in result.trace.rows if 40 <= row.t < 210)
    assert any(row.setpoint_motor_rpm > 0 for row in result.trace.rows if row.t > 211)


def test_sto_judge_detects_broken_safety_evidence() -> None:
    case = next(case for case in CASES if case.name == "sto_open_manual_at_speed")
    result = _run(case)
    assert not _checks(case, result)
    assert "sto_evidence" in _checks(replace(case, at=None), result)
    assert "sto_evidence" in _checks(case, replace(result, sto=()))
    unlatched = tuple(replace(view, latched=False) for view in result.sto)
    assert "sto_latch" in _checks(case, replace(result, sto=unlatched))
    unfinished = tuple(replace(view, end_reason=None) for view in result.sto)
    assert "sto_latch" in _checks(case, replace(result, sto=unfinished))
    early_ack = RequestOutcome(
        at=Seconds(169), request="acknowledge", expected=Expectation.ANY, accepted=True, detail=""
    )
    assert "sto_latch" in _checks(case, replace(result, requests=(early_ack,)))
    at = next(index for index, row in enumerate(result.trace.rows) if row.t >= 170 - 1e-6)
    rows = list(result.trace.rows)
    rows[at] = replace(rows[at], setpoint_motor_rpm=1, sim_state="OPERATION_ENABLED", current_a=1)
    bad = replace(result, trace=replace(result.trace, rows=tuple(rows)))
    assert {"sto_output", "sto_torque"} <= _checks(case, bad)
    rows[at] = replace(result.trace.rows[at], measured_motor_rpm=9000)
    bad = replace(result, trace=replace(result.trace, rows=tuple(rows)))
    assert "sto_coast" in _checks(case, bad)
    for changes in (
        {"lfrd_motor_rpm": 1},
        {"sim_state": "OPERATION_ENABLED"},
        {"safety_action": "RAMP_DOWN"},
        {"safety_rule": "another_rule"},
    ):
        row = replace(result.trace.rows[at], **changes)
        rows = [*result.trace.rows[:at], row, *result.trace.rows[at + 1 :]]
        assert "sto_output" in _checks(
            case, replace(result, trace=replace(result.trace, rows=tuple(rows)))
        )
    rows = list(result.trace.rows)
    rows[at] = replace(rows[at], current_a=1)
    assert "sto_torque" in _checks(
        case, replace(result, trace=replace(result.trace, rows=tuple(rows)))
    )
    assert "sto_zero" in _checks(case, replace(result, trace=replace(result.trace, frames=())))
    for kind, value in (("speed", 1), ("command", 15)):
        frames: tuple[Mapping[str, JsonValue], ...] = (
            *result.trace.frames,
            {"t": 171, "kind": kind, "value": value},
        )
        assert "sto_reactivation" in _checks(
            case, replace(result, trace=replace(result.trace, frames=frames))
        )
    frames = (*result.trace.frames, {"t": "invalid", "kind": "speed", "value": 55})
    assert not _checks(case, replace(result, trace=replace(result.trace, frames=frames)))
    bad_keepalives: tuple[tuple[JsonValue, int], ...] = ((9000, 170), ("invalid", 170), (1, 171))
    for bad_value, stamp in bad_keepalives:
        frames = ({"t": stamp, "kind": "speed", "value": bad_value}, *result.trace.frames)
        assert "sto_keepalive" in _checks(
            case, replace(result, trace=replace(result.trace, frames=frames))
        )
    frames = ({"t": 171, "kind": "speed", "value": 0},)
    assert "sto_zero" in _checks(case, replace(result, trace=replace(result.trace, frames=frames)))


def test_sto_idle_judge_requires_a_refusal_without_latch_then_an_accepted_start() -> None:
    case = next(case for case in CASES if case.name == "sto_idle_start_gate")
    result = _run(case)
    assert not _checks(case, result)
    assert "sto_idle_gate" in _checks(case, replace(result, requests=()))
    opened = tuple(
        replace(view, latched=True) if view.state is StoState.OPEN else view for view in result.sto
    )
    assert "sto_idle_gate" in _checks(case, replace(result, sto=opened))
    closed = tuple(replace(view, state=StoState.READY) for view in result.sto)
    assert "sto_idle_gate" in _checks(case, replace(result, sto=closed))
    assert "sto_evidence" in _checks(replace(case, at=400), result)
    accepted_only = tuple(request for request in result.requests if request.accepted)
    assert "sto_idle_gate" in _checks(case, replace(result, requests=accepted_only))
    refused_only = tuple(request for request in result.requests if not request.accepted)
    assert "sto_idle_gate" in _checks(case, replace(result, requests=refused_only))
