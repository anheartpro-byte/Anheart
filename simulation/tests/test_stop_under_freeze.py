"""A FREEZE holds a speed, never a stop: the simulation's side of ANH-175.

``raspberry-pi/tests/test_runtime_stop_freeze.py`` proves the rule on the fake
drive. Here it is the real runtime against the simulated drive and the
simulated rider, read from the trace two scenarios leave behind:

* ``stop_operator_auto_under_freeze``: the shipped 30-min programme, the heart
  rate lost at 420 s for 27 s, STOP at 432 s under the FREEZE of ``hr_stale``.
  Measured on ``develop``: the setpoint stayed at 165 motor rpm until the
  heart rate came back at 447 s, and reached zero at 460 s. Here: zero at
  444.6 s, under the FREEZE;
* ``stop_remote_manual_under_latched_freeze``: empty capsule at 27 rpm, a
  1.4 s stall of the loop (``loop_stall``, latched), then a stop requested off
  the machine at 160 s. Measured on ``develop``: 1344 motor rpm held to the
  end of the scenario, 240 s later, the mode saying ARRET. Here: zero at
  265.8 s, the console back to REPOS.

The battery (``test_battery.py``) already judges each file's own expectations
and every invariant. The tests here say it about the rows, so that a failure
names the instant; and they read again one scenario that was already in the
battery for what must NOT have changed: with no stop asked for, a FREEZE holds.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

from simulation.invariants import rules_seen
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import run_file
from src.record.rows import Row
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.safety import (
    RULE_HR_STALE,
    RULE_LOOP_STALL,
    RULE_SESSION_STANDSTILL,
)

PROGRAMME: Final = SCENARIO_DIR / "stop_operator_auto_under_freeze.json"
MANUAL: Final = SCENARIO_DIR / "stop_remote_manual_under_latched_freeze.json"
HELD: Final = SCENARIO_DIR / "fault_ecg_dropout_short.json"

LOST_AT: Final[float] = 420.0
STOP_AT: Final[float] = 432.0
"""The programme scenario's instants: heart rate lost, then STOP twelve seconds later."""

REMOTE_STOP_AT: Final[float] = 160.0
"""The manual scenario's stop, ten seconds after the loop stalled."""

MIN_RUN: Final[int] = int(DEFAULT_MOTION_LIMITS.min_run)

TICK: Final[float] = 0.2


def _between(rows: tuple[Row, ...], start: float, stop: float) -> list[Row]:
    return [row for row in rows if start <= row.t <= stop]


def _walk(rows: tuple[Row, ...], stop_at: float) -> list[Row]:
    """From the last row before the stop to the first row at zero, both included."""
    before = [row for row in rows if row.t < stop_at][-1]
    after = [row for row in rows if row.t >= stop_at]
    zero = next(index for index, row in enumerate(after) if row.setpoint_motor_rpm == 0)
    return [before, *after[: zero + 1]]


def _assert_walks_down_under(walk: list[Row], rule: str) -> None:
    """Held before the stop; then lower within two ticks, never up, and FREEZE all the way."""
    before, down = walk[0], walk[1:]
    assert (before.safety_rule, before.safety_action) == (rule, "FREEZE")
    assert before.setpoint_motor_rpm > MIN_RUN, "the arm was not turning"
    assert down[1].setpoint_motor_rpm < before.setpoint_motor_rpm, (
        "the stop left the setpoint where it was under the FREEZE"
    )
    assert all(
        later.setpoint_motor_rpm <= earlier.setpoint_motor_rpm for earlier, later in pairwise(walk)
    )
    turning = [row for row in down if row.setpoint_motor_rpm != 0]
    assert {(row.safety_rule, row.safety_action) for row in turning} == {(rule, "FREEZE")}, (
        "something other than the stop brought the setpoint down"
    )
    assert {row.mode for row in down[1:]} == {"arret"}


def test_a_stop_under_the_freeze_of_a_lost_heart_rate_comes_down_before_the_rule_s_reduce() -> None:
    """STOP at 432 s: the descent begins at once and is over before 450 s, under FREEZE."""
    result = run_file(PROGRAMME)
    rows = result.trace.rows
    held = _between(rows, LOST_AT + 11.0, STOP_AT - TICK)
    assert {(row.safety_rule, row.safety_action) for row in held} == {(RULE_HR_STALE, "FREEZE")}
    assert len({row.setpoint_motor_rpm for row in held}) == 1, "the FREEZE did not hold the speed"

    walk = _walk(rows, STOP_AT)
    _assert_walks_down_under(walk, RULE_HR_STALE)
    assert walk[-1].t < LOST_AT + 30.0, "the arm came down only with the rule's own REDUCE"
    assert MIN_RUN in {row.setpoint_motor_rpm for row in walk}, "the walk skipped the minimum speed"

    after = [row for row in rows if row.t >= walk[-1].t]
    assert {row.setpoint_motor_rpm for row in after} == {0}
    assert {row.lfrd_motor_rpm for row in after} == {0}, "a non-zero reference reached the drive"
    assert RULE_SESSION_STANDSTILL not in rules_seen(result)
    assert rules_seen(result) == (RULE_HR_STALE,)
    assert result.trace.final.end_reason == "operator_stop"
    assert result.trace.final.runtime_state == "finished"
    assert not after[-1].output_enabled


def test_a_stop_sent_off_the_machine_under_a_latched_freeze_reaches_standstill() -> None:
    """``loop_stall`` latches: on ``develop`` only an emergency stop got this arm down."""
    result = run_file(MANUAL)
    rows = result.trace.rows
    held = _between(rows, REMOTE_STOP_AT - 5.0, REMOTE_STOP_AT - TICK)
    assert {(row.safety_rule, row.safety_action) for row in held} == {(RULE_LOOP_STALL, "FREEZE")}
    assert len({row.setpoint_motor_rpm for row in held}) == 1

    walk = _walk(rows, REMOTE_STOP_AT)
    _assert_walks_down_under(walk, RULE_LOOP_STALL)
    after = [row for row in rows if row.t >= walk[-1].t]
    assert {row.setpoint_motor_rpm for row in after} == {0}
    assert {row.mode for row in after} <= {"arret", "repos"}
    last = rows[-1]
    assert last.measured_motor_rpm == 0, "the shaft did not follow"
    assert last.mode == "repos"
    assert not last.output_enabled
    assert rules_seen(result) == (RULE_LOOP_STALL,)
    assert result.trace.final.end_reason == "operator_stop"
    assert result.trace.final.runtime_state == "finished"


def test_with_no_stop_asked_for_the_freeze_of_a_lost_heart_rate_still_holds() -> None:
    """UNCHANGED: 15 s without a heart rate mid-HOLD, the speed held, then regulation resumes."""
    result = run_file(HELD)
    rows = result.trace.rows
    gap = _between(rows, 911.0, 914.0)
    assert {(row.safety_rule, row.safety_action) for row in gap} == {(RULE_HR_STALE, "FREEZE")}
    assert len({row.setpoint_motor_rpm for row in gap}) == 1
    assert gap[0].setpoint_motor_rpm > MIN_RUN
    assert result.trace.final.end_reason == "programme_complete"
