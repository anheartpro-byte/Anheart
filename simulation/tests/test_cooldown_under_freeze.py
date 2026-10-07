"""Under a FREEZE the setpoint follows the programme's descent: the simulation's side of ANH-189.

``raspberry-pi/tests/test_runtime_cooldown_freeze.py`` proves the rule on the
fake drive. Here it is the real runtime against the simulated drive and the
simulated rider, read from the trace one scenario leaves behind:

* ``auto_cooldown_under_latched_freeze``: the shipped 30-min programme, a 1.4 s
  stall of the loop at 900 s, in the middle of HOLD (``loop_stall``, a FREEZE
  that latches), and nobody asking for a stop. The setpoint is held to the
  end of HOLD, comes down from the entry into COOLDOWN at 1260 s, and the
  session is over at its planned 1800 s as a programme run to its end. The
  verdict is acknowledged at 1840 s, after the 1830 s at which
  ``session_overrun`` judges a session still in progress.

The battery (``test_battery.py``) already judges the file's own expectations
and every invariant. The tests here say it about the rows, so that a failure
names the instant, and they set the descent beside the one the same programme
makes with nothing standing (``auto_standard_30_min``, already in the battery):
the same setpoints, tick for tick.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Final

from simulation.invariants import rules_seen
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import run_file
from src.record.rows import Row
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.safety import RULE_LOOP_STALL, RULE_SESSION_OVERRUN, RULE_SESSION_STANDSTILL

FROZEN: Final = SCENARIO_DIR / "auto_cooldown_under_latched_freeze.json"
NOMINAL: Final = SCENARIO_DIR / "auto_standard_30_min.json"

STALL_AT: Final[float] = 900.0
COOLDOWN_AT: Final[float] = 1260.0
RECOVERY_AT: Final[float] = 1500.0
PLANNED: Final[float] = 1800.0
OVERRUN_AT: Final[float] = PLANNED + 30.0
"""Past this ``session_overrun`` ends a session that is still in progress."""

MIN_RUN: Final[int] = int(DEFAULT_MOTION_LIMITS.min_run)

TICK: Final[float] = 0.2

STANDING: Final[tuple[str, str]] = (RULE_LOOP_STALL, "FREEZE")


def _between(rows: tuple[Row, ...], start: float, stop: float) -> list[Row]:
    return [row for row in rows if start <= row.t <= stop]


def _descent(rows: tuple[Row, ...]) -> list[Row]:
    """From the first COOLDOWN row to the first row at zero, both included."""
    cooling = [row for row in rows if row.phase != "hold" and row.t >= COOLDOWN_AT - TICK]
    zero = next(index for index, row in enumerate(cooling) if row.setpoint_motor_rpm == 0)
    return cooling[: zero + 1]


def test_under_a_latched_freeze_the_setpoint_is_held_to_the_end_of_hold_and_no_further() -> None:
    """Held exactly for six minutes of HOLD; lower on the first row of COOLDOWN."""
    rows = run_file(FROZEN).trace.rows
    held = _between(rows, STALL_AT + 2.0, COOLDOWN_AT - TICK / 2)
    assert {(row.safety_rule, row.safety_action) for row in held} == {STANDING}
    assert {row.phase for row in held} == {"hold"}
    assert len({row.setpoint_motor_rpm for row in held}) == 1, "the FREEZE did not hold the speed"
    speed = held[-1].setpoint_motor_rpm
    assert speed > MIN_RUN, "the arm was not turning"

    first = _descent(rows)[0]
    assert first.phase == "cooldown"
    assert abs(first.t - COOLDOWN_AT) < TICK / 2, f"COOLDOWN began at {first.t} s"
    assert (first.safety_rule, first.safety_action) == STANDING
    assert first.setpoint_motor_rpm < speed, "the FREEZE held the setpoint into the cooldown"


def test_the_descent_under_the_freeze_is_the_one_the_programme_makes_with_nothing_standing() -> (
    None
):
    """Never up, FREEZE all the way, and the setpoints of the nominal run of the same programme."""
    frozen = _descent(run_file(FROZEN).trace.rows)
    nominal = _descent(run_file(NOMINAL).trace.rows)
    walked = [row.setpoint_motor_rpm for row in frozen]
    assert all(later <= earlier for earlier, later in pairwise(walked))
    assert MIN_RUN in walked, "the walk skipped the minimum speed"
    turning = [row for row in frozen if row.setpoint_motor_rpm != 0]
    assert {(row.safety_rule, row.safety_action) for row in turning} == {STANDING}, (
        "something other than the programme brought the setpoint down"
    )
    assert {row.phase for row in frozen} == {"cooldown"}
    assert {row.mode for row in frozen} == {"seance"}

    assert {row.safety_action for row in nominal} == {"NONE"}
    assert walked == [row.setpoint_motor_rpm for row in nominal], (
        "the descent under the FREEZE is not the ordinary cooldown"
    )
    assert frozen[-1].t < RECOVERY_AT, "the arm was still coming down when RECOVERY began"


def test_the_session_ends_at_its_planned_duration_and_the_overrun_rule_stays_silent() -> None:
    """Zero from the end of the descent on; DONE at 1800 s; nothing but the FREEZE ever stood."""
    result = run_file(FROZEN)
    rows = result.trace.rows
    stopped = _descent(rows)[-1].t
    after = [row for row in rows if row.t >= stopped]
    assert {row.setpoint_motor_rpm for row in after} == {0}
    assert {row.lfrd_motor_rpm for row in after} == {0}, "a non-zero reference reached the drive"

    over = next(row for row in rows if row.phase == "done")
    assert abs(over.t - PLANNED) <= TICK, f"the session was over at {over.t} s"
    assert (over.state, over.mode) == ("finished", "repos")
    assert not over.output_enabled
    assert over.measured_motor_rpm == 0

    assert rows[-1].t > OVERRUN_AT, "the scenario ended before the deadline of the rule"
    assert rules_seen(result) == (RULE_LOOP_STALL,)
    assert RULE_SESSION_OVERRUN not in rules_seen(result)
    assert RULE_SESSION_STANDSTILL not in rules_seen(result)
    at_rest = _between(rows, PLANNED + TICK, OVERRUN_AT + 5.0)
    assert {(row.safety_rule, row.safety_action) for row in at_rest} == {STANDING}, (
        "the latched FREEZE did not stand at rest until it was acknowledged"
    )
    assert rows[-1].safety_rule is None, "the acknowledgement did not clear the FREEZE"
    assert result.trace.final.end_reason == "programme_complete"
    assert result.trace.final.runtime_state == "finished"
