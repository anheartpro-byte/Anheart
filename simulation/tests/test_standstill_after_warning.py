"""A stopped arm never restarts by itself: a standstill inside a session ends it (ANH-176).

The simulation's side of ``raspberry-pi/tests/test_runtime_standstill.py``: the
detached electrode, on the real runtime against the simulated drive and the
simulated rider, read from the trace the scenario leaves behind.

``fault_ecg_electrode_refitted_standstill`` loses the heart rate for 50 s in
WARMUP of the shipped 30-min programme and then gets it back. On ``develop``
the trace showed the arm at standstill at 460 s, the heart rate back at 470 s,
71 motor rpm at 500 s and the programme's 276 rpm ceiling at 575 s, with nobody
having touched the console. The battery (``test_battery.py``) already judges
the file's own expectations and every invariant; the tests here say the same
thing about the rows themselves, so that a failure names the instant.

Two scenarios that were ALREADY in the battery are read again for what must
not have changed: a speed that was only held, and a speed lowered part way,
still resume by themselves.
"""

from __future__ import annotations

from typing import Final

from simulation.invariants import rules_seen
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import run_file
from src.record.rows import Row
from src.training.safety import RULE_HR_STALE, RULE_SESSION_STANDSTILL, SELF_CLEARING

ELECTRODE: Final = SCENARIO_DIR / "fault_ecg_electrode_refitted_standstill.json"
HELD: Final = SCENARIO_DIR / "fault_ecg_dropout_short.json"
LOWERED: Final = SCENARIO_DIR / "fault_ecg_repeat_seq.json"

LOST_AT: Final[float] = 420.0
BACK_AT: Final[float] = 470.0
"""The scenario's own instants: the heart rate lost for 50 s, then delivered again."""

FAULT_AT: Final[float] = 900.0
"""When the two older scenarios lose their heart rate (mid-HOLD of the jog programme)."""

MIN_RUN: Final[int] = 55


def _between(rows: tuple[Row, ...], start: float, stop: float) -> list[Row]:
    return [row for row in rows if start <= row.t <= stop]


def test_the_arm_stays_at_standstill_after_the_electrode_is_refitted() -> None:
    """Held, lowered, stopped; then the heart rate returns and nothing moves again."""
    result = run_file(ELECTRODE)
    rows = result.trace.rows
    turning = _between(rows, LOST_AT - 5.0, LOST_AT)
    assert min(row.setpoint_motor_rpm for row in turning) > MIN_RUN, "the arm was not turning"

    held = _between(rows, LOST_AT + 12.0, LOST_AT + 28.0)
    assert {row.safety_action for row in held} == {"FREEZE"}
    assert len({row.setpoint_motor_rpm for row in held}) == 1, "FREEZE did not hold the speed"
    lowered = _between(rows, LOST_AT + 32.0, LOST_AT + 38.0)
    assert {(row.safety_rule, row.safety_action) for row in lowered} == {(RULE_HR_STALE, "REDUCE")}
    assert lowered[-1].setpoint_motor_rpm < lowered[0].setpoint_motor_rpm

    stopped = next(row for row in rows if row.t > LOST_AT and row.setpoint_motor_rpm == 0)
    assert stopped.t < BACK_AT, "the standstill came after the heart rate was back"
    after = [row for row in rows if row.t >= stopped.t]
    moved = [(row.t, row.setpoint_motor_rpm) for row in after if row.setpoint_motor_rpm != 0]
    assert not moved, f"the arm restarted by itself (session time, motor rpm): {moved[:3]}"
    assert {row.lfrd_motor_rpm for row in after} == {0}, "a non-zero reference reached the drive"
    # The tick that wrote the zero still shows the warning; the supervisor's
    # verdict, and the ending, land on the very next one.
    assert (stopped.safety_rule, stopped.safety_action) == (RULE_HR_STALE, "REDUCE")
    ended = after[1]
    assert ended.t - stopped.t < 0.25
    assert ended.safety_rule == RULE_SESSION_STANDSTILL
    assert ended.safety_action == "RAMP_DOWN"
    assert ended.mode == "arret"
    assert {row.safety_rule for row in after[1:] if row.mode == "arret"} == {
        RULE_SESSION_STANDSTILL
    }
    back = [row for row in after if row.t >= BACK_AT + 10.0]
    assert all(row.hr_live is not None for row in back[:300]), "the heart rate never came back"
    assert {row.measured_motor_rpm for row in back} == {0}
    assert {row.mode for row in back} <= {"arret", "repos"}
    assert not back[-1].output_enabled
    assert result.trace.final.end_reason == "safety_verdict"
    assert result.trace.final.runtime_state == "finished"


def test_the_operator_is_told_why_and_a_start_is_refused_until_the_acknowledgement() -> None:
    """The verdict names the warning; both STARTs meet the latch, in the console's words."""
    result = run_file(ELECTRODE)
    verdicts = [message.text for message in result.messages if message.source == "verdict"]
    ended = [text for text in verdicts if text.startswith(f"{RULE_SESSION_STANDSTILL} ")]
    assert len(ended) == 1, verdicts
    assert "RAMP_DOWN" in ended[0]
    assert f"the warning {RULE_HR_STALE} brought the setpoint to zero" in ended[0]
    assert "a stopped arm never restarts by itself" in ended[0]

    assert [request.request for request in result.requests] == ["start", "start"]
    for request in result.requests:
        assert not request.accepted
        assert "SafetyStanding" in request.detail
        assert RULE_SESSION_STANDSTILL in request.detail
    refusals = [message.text for message in result.messages if message.source == "refusal"]
    assert len(refusals) == 2
    assert all(f"verdict {RULE_SESSION_STANDSTILL} a acquitter" in text for text in refusals)
    assert rules_seen(result) == (RULE_HR_STALE, RULE_SESSION_STANDSTILL)


def test_while_the_speed_can_still_resume_the_operator_is_told_so() -> None:
    """The two unlatched levels say they lift by themselves; the latched ending does not."""
    result = run_file(ELECTRODE)
    verdicts = [message.text for message in result.messages if message.source == "verdict"]
    warned = [text for text in verdicts if text.startswith(f"{RULE_HR_STALE} ")]
    assert [text.split(":")[0] for text in warned] == [
        f"{RULE_HR_STALE} (FREEZE)",
        f"{RULE_HR_STALE} (REDUCE)",
    ]
    assert all(text.endswith(SELF_CLEARING) for text in warned)
    assert all(SELF_CLEARING not in text for text in verdicts if text not in warned)


def test_a_speed_that_was_only_held_still_resumes_by_itself() -> None:
    """UNCHANGED: 15 s without a heart rate mid-HOLD, FREEZE, then the programme carries on."""
    result = run_file(HELD)
    rows = result.trace.rows
    gap = _between(rows, FAULT_AT + 11.0, FAULT_AT + 14.0)
    assert {(row.safety_rule, row.safety_action) for row in gap} == {(RULE_HR_STALE, "FREEZE")}
    held = gap[0].setpoint_motor_rpm
    assert held > MIN_RUN
    assert {row.setpoint_motor_rpm for row in gap} == {held}
    after = _between(rows, FAULT_AT + 20.0, FAULT_AT + 120.0)
    assert {row.mode for row in after} == {"seance"}
    assert {row.safety_action for row in after[-50:]} == {"NONE"}
    assert min(row.setpoint_motor_rpm for row in after) > MIN_RUN
    assert RULE_SESSION_STANDSTILL not in rules_seen(result)
    assert result.trace.final.end_reason == "programme_complete"


def test_a_speed_lowered_part_way_still_resumes_by_itself() -> None:
    """UNCHANGED: 45 s without a fresh reading, REDUCE from 30 s, slowed but never stopped."""
    result = run_file(LOWERED)
    rows = result.trace.rows
    before = _between(rows, FAULT_AT - 5.0, FAULT_AT)[-1].setpoint_motor_rpm
    lowered = _between(rows, FAULT_AT + 32.0, FAULT_AT + 44.0)
    assert {(row.safety_rule, row.safety_action) for row in lowered} == {(RULE_HR_STALE, "REDUCE")}
    low = min(row.setpoint_motor_rpm for row in _between(rows, FAULT_AT, FAULT_AT + 60.0))
    assert MIN_RUN < low < before, "the REDUCE did not bite, or it reached standstill"
    after = _between(rows, FAULT_AT + 60.0, FAULT_AT + 160.0)
    assert {row.mode for row in after} == {"seance"}
    assert after[-1].setpoint_motor_rpm > low, "regulation did not resume"
    assert RULE_SESSION_STANDSTILL not in rules_seen(result)
    assert result.trace.final.end_reason == "programme_complete"
