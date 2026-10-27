"""The failure matrix's ``drive_input`` cases (:mod:`simulation.failures`), case by case.

Each case is judged in ``test_failures.py`` like every other one. These tests
say more about the reading itself, from the trace and the STO observations:
in every category of stop no register 5202 frame reaches the simulated drive
until the stop is over, then the reads resume and show the chain; a faulted
drive's coast is read; a programme's own descent is no stop and is read; a
0.1 s opening between two ticks is never read, yet latched; and the judge
names a read taken during a stop.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Final

import pytest

from simulation.failures import (
    DRIVE_INPUT_STOPS,
    READ_LATCH_S,
    FailureCase,
    cases,
    judge,
    run_case,
    scenario_of,
    stop_under_way,
)
from simulation.harness import TICK, RunResult, StoObservation, run_scenario
from simulation.scenario import StoVariant
from simulation.tracefile import Row
from src.local_panel import StoReadGate
from src.motor.sto import StoState
from src.training.runtime import STO_OPEN_DEDUCED

CASES: Final[dict[str, FailureCase]] = {case.name: case for case in cases()}
DRIVE_INPUT: Final[tuple[FailureCase, ...]] = tuple(
    case for case in CASES.values() if scenario_of(case).sto is StoVariant.DRIVE_INPUT
)
STOPS: Final[tuple[str, ...]] = tuple(
    f"sto_drive_input_{stop.name}_{shape}"
    for stop in DRIVE_INPUT_STOPS
    if stop.name != "faulted_drive_coast"
    for shape in ("left_open", "closed_again")
)
_RUNS: Final[dict[str, RunResult]] = {}


def _run(case: FailureCase) -> RunResult:
    """Each case is run once per test session, whatever asks about it."""
    cached = _RUNS.get(case.name)
    if cached is None:
        cached = asyncio.run(run_case(case))
        _RUNS[case.name] = cached
    return cached


def _pairs(result: RunResult) -> list[tuple[Row, StoObservation]]:
    """Each tick's row with what the STO reading did in the gap after it."""
    return list(zip(result.trace.rows, result.sto, strict=True))


def _first_latch(result: RunResult) -> float:
    return next(row.t for row in result.trace.rows if row.safety_rule == "sto_open")


def _checks(case: FailureCase, result: RunResult) -> set[str]:
    return {violation.check for violation in judge(case, result)}


def test_the_variant_is_used_only_where_a_case_asks_for_it() -> None:
    assert len(DRIVE_INPUT) == 19
    assert all(case.name.startswith("sto_drive_input_") for case in DRIVE_INPUT)
    read_from_memory = [case for case in CASES.values() if case not in DRIVE_INPUT]
    assert read_from_memory
    assert all(scenario_of(case).sto is StoVariant.SIM_READY for case in read_from_memory)


def test_with_the_gate_bypassed_a_stop_is_read_and_the_judge_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always(_gate: StoReadGate) -> bool:
        return True

    monkeypatch.setattr(StoReadGate, "permitted", always)
    case = CASES["sto_drive_input_operator_stop_closed_again"]
    result = asyncio.run(run_case(case))
    assert any(view.reads for row, view in _pairs(result) if stop_under_way(row))
    assert "sto_read_in_stop" in _checks(case, result)


# -- each category of stop ----------------------------------------------------------


@pytest.mark.parametrize("name", STOPS)
def test_a_stop_withholds_every_read_then_the_reads_resume_and_show_the_chain(name: str) -> None:
    case = CASES[name]
    at = case.at
    assert at is not None
    result = _run(case)
    pairs = _pairs(result)
    stopping = [row.t for row, _ in pairs if stop_under_way(row)]
    began, ended = stopping[0], stopping[-1]
    assert began < at < ended, (began, at, ended)
    assert any(view.reads for row, view in pairs if began - 3 * TICK <= row.t < began)
    assert not any(view.reads for row, view in pairs if began <= row.t <= ended)
    assert any(view.reads for row, view in pairs if row.t > ended)
    assert 0.0 < _first_latch(result) - at <= TICK
    final = StoState.READY if name.endswith("closed_again") else StoState.OPEN
    assert result.sto[-1].state is final
    assert all(not request.accepted for request in result.requests)


@pytest.mark.parametrize(
    "name", [case.name for case in DRIVE_INPUT if "faulted_drive_coast" not in case.name]
)
def test_the_reads_change_no_write_nor_any_row_of_the_same_case_read_from_memory(
    name: str,
) -> None:
    """A read never delays a write: the control path is the one a chain read at once gives.

    The same document with ``sim_ready``, whose chain is read from memory at
    the tick: every frame the runtime wrote, at the same instant, and every
    row. A faulted drive's coast is the exception: its chain is read, so an
    opening there is seen at the next read, not at the tick.
    """
    case = CASES[name]
    reading = _run(case)
    memory = asyncio.run(run_scenario(replace(scenario_of(case), sto=StoVariant.SIM_READY)))
    assert any(view.reads for view in reading.sto)
    assert not any(view.reads for view in memory.sto)
    assert reading.trace.frames == memory.trace.frames
    assert reading.trace.rows == memory.trace.rows


@pytest.mark.parametrize("shape", ["left_open", "closed_again"])
def test_a_faulted_drive_s_coast_is_read_and_the_reading_latches_the_opening(shape: str) -> None:
    case = CASES[f"sto_drive_input_faulted_drive_coast_{shape}"]
    at = case.at
    assert at is not None
    result = _run(case)
    latched = _first_latch(result)
    assert 0.0 < latched - at <= READ_LATCH_S
    coasting = [
        (row, view)
        for row, view in _pairs(result)
        if row.drive_state == "FAULT" and row.measured_motor_rpm > 1
    ]
    assert any(view.reads for row, view in coasting if row.t < at)
    assert any(view.state is StoState.OPEN for row, view in coasting if row.t <= latched)
    assert any(view.reads for row, view in coasting if row.t > latched)
    words = [message.text for message in result.messages if message.text.startswith("sto_open")]
    assert words
    assert STO_OPEN_DEDUCED not in words[0]


@pytest.mark.parametrize("phase", ["cooldown", "recovery"])
def test_a_programme_s_own_descent_is_no_stop_and_its_reads_go_on(phase: str) -> None:
    case = CASES[f"sto_drive_input_programme_descent_{phase}"]
    at = case.at
    assert at is not None
    result = _run(case)
    descending = [
        (row, view)
        for row, view in _pairs(result)
        if row.phase in {"cooldown", "recovery"} and row.t < at and row.setpoint_motor_rpm > 0
    ]
    assert len(descending) > 20
    assert not any(stop_under_way(row) for row, _ in descending)
    assert sum(view.reads for _, view in descending) >= (len(descending) - 2) // 3
    assert 0.0 < _first_latch(result) - at <= TICK


@pytest.mark.parametrize("label", ["manual", "programme"])
def test_a_brief_opening_between_two_ticks_is_never_read_yet_latched(label: str) -> None:
    case = CASES[f"sto_drive_input_brief_opening_{label}"]
    at = case.at
    assert at is not None
    result = _run(case)
    assert not any(view.state is StoState.OPEN for view in result.sto)
    assert 0.0 < _first_latch(result) - at <= TICK
    assert [request.accepted for request in result.requests] == [False]


# -- the judge and the scenario key ------------------------------------------------


def test_the_drive_input_judge_names_each_broken_promise() -> None:
    case = CASES["sto_drive_input_operator_stop_left_open"]
    result = _run(case)
    assert not _checks(case, result)
    silent = tuple(replace(view, reads=0) for view in result.sto)
    assert "sto_reads" in _checks(case, replace(result, sto=silent))
    pairs = _pairs(result)
    index = next(i for i, (row, _) in enumerate(pairs) if stop_under_way(row))
    read = list(result.sto)
    read[index] = replace(read[index], reads=1)
    assert "sto_read_in_stop" in _checks(case, replace(result, sto=tuple(read)))
    latched = next(i for i, (row, _) in enumerate(pairs) if row.safety_rule == "sto_open")
    for changes in ({"safety_rule": "another_rule"}, {"setpoint_motor_rpm": 5}):
        rows = list(result.trace.rows)
        rows[latched + 3] = replace(rows[latched + 3], **changes)
        broken = replace(result, trace=replace(result.trace, rows=tuple(rows)))
        assert "sto_latch_held" in _checks(case, broken)
    unread = replace(result, scenario=replace(result.scenario, sto=StoVariant.SIM_READY))
    assert not _checks(case, replace(unread, sto=silent))


def test_a_stop_under_way_is_read_from_the_trace_columns() -> None:
    result = _run(CASES["sto_drive_input_operator_stop_left_open"])
    cruising = next(row for row in result.trace.rows if row.t >= 30.0)
    assert (cruising.state, cruising.setpoint_motor_rpm > 0) == ("running", True)
    assert not stop_under_way(cruising)
    ending = replace(cruising, state="ending")
    stopping = [
        ending,
        replace(ending, setpoint_motor_rpm=0),
        replace(cruising, safety_action="RAMP_DOWN"),
        replace(cruising, safety_action="QUICK_STOP"),
        replace(cruising, safety_action="FREEZE", phase="cooldown"),
        replace(cruising, manual_target_motor_rpm=0),
        replace(cruising, state="finished", setpoint_motor_rpm=0),
    ]
    assert all(stop_under_way(row) for row in stopping)
    going_on = [
        replace(cruising, safety_action="FREEZE"),
        replace(cruising, safety_action="REDUCE"),
        replace(cruising, mode="seance", manual_target_motor_rpm=0),
        replace(ending, drive_state="FAULT"),
        replace(ending, setpoint_motor_rpm=0, output_enabled=False),
        replace(cruising, state="finished", setpoint_motor_rpm=0, output_enabled=False),
    ]
    assert not any(stop_under_way(row) for row in going_on)
