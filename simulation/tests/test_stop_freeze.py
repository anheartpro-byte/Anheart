from __future__ import annotations

from pathlib import Path

from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import run_file


def test_remote_stop_descends_on_next_tick_during_hr_freeze(tmp_path: Path) -> None:
    result = run_file(SCENARIO_DIR / "stop_during_hr_freeze.json")
    result.trace.write_jsonl(tmp_path / "stop-during-freeze.jsonl")
    before = [row for row in result.trace.rows if 41 <= row.t < 42][-1]
    after = [row for row in result.trace.rows if 42 <= row.t < 43]
    assert before.safety_action == "FREEZE"
    assert 0 < after[0].setpoint_motor_rpm < before.setpoint_motor_rpm
    assert all(row.setpoint_motor_rpm < before.setpoint_motor_rpm for row in after)
    assert result.trace.final.runtime_applied_rpm == 0
    assert not result.trace.final.energised
