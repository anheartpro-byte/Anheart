"""The scenario battery: every file under ``simulation/scenarios/``, run through the real runtime.

Per scenario, three things are asserted:

* every physical invariant of :mod:`simulation.invariants` holds and the
  scenario's own expectations are met (xfail(strict) when the file carries a
  ``known_defect``: correct behaviour that ``raspberry-pi/src`` does not deliver
  today - it turns red the day the defect is fixed, so the key gets removed);
* **no exit path leaves the motor running**: after the teardown window the
  drive is producing no torque and the shaft is stopped - asserted for every
  scenario, known defect or not;
* the trace survives the round trip to JSONL.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from simulation.harness import ENERGISED
from simulation.invariants import check_expectations, check_invariants
from simulation.scenario import ACTION_NAMES, scenario_paths
from simulation.tests.conftest import document, load, obj, run_file, seq

PATHS: Final[tuple[Path, ...]] = scenario_paths()


def _verdict_params() -> Iterator[object]:
    for path in PATHS:
        defect = load(path).known_defect
        marks = () if defect is None else (pytest.mark.xfail(strict=True, reason=defect),)
        yield pytest.param(path, marks=marks, id=path.stem)


@pytest.mark.parametrize("path", list(_verdict_params()))
def test_scenario_holds_every_invariant_and_expectation(path: Path) -> None:
    result = run_file(path)
    violations = (*check_invariants(result), *check_expectations(result))
    assert not violations, "\n".join(str(violation) for violation in violations)


@pytest.mark.parametrize("path", PATHS, ids=[path.stem for path in PATHS])
def test_no_exit_path_leaves_the_motor_running(path: Path) -> None:
    final = run_file(path).trace.final
    assert final.sim_state not in {state.name for state in ENERGISED}, final
    assert not final.energised, final
    assert final.shaft_motor_rpm == 0, final
    assert final.runtime_state in {"finished", "ending", "idle"}, final
    if not final.silent:
        assert final.lfrd_motor_rpm == 0, final
        assert final.runtime_applied_rpm == 0, final
        assert not final.runtime_output_enabled, final


@pytest.mark.parametrize("path", PATHS, ids=[path.stem for path in PATHS])
def test_the_trace_round_trips_through_jsonl(path: Path, tmp_path: Path) -> None:
    result = run_file(path)
    written = result.trace.write_jsonl(tmp_path / "trace.jsonl")
    lines = [document(line) for line in written.read_text(encoding="utf-8").splitlines()]
    kinds = [line["type"] for line in lines]
    assert kinds[0] == "meta"
    assert kinds[-1] == "final"
    assert kinds.count("row") == len(result.trace.rows)
    assert kinds.count("frame") == len(result.trace.frames)
    assert lines[0]["scenario"] == result.scenario.name


def test_the_battery_exercises_every_action_and_both_session_kinds() -> None:
    """Every action is used by a scenario file or by a case of the failure matrix."""
    from simulation.failures import cases  # noqa: PLC0415

    used: set[str] = set()
    kinds: set[str] = set()
    documents = [document(path.read_text(encoding="utf-8")) for path in PATHS]
    documents += [obj(case.document) for case in cases()]
    for doc in documents:
        kinds.add(str(doc["kind"]))
        used.update(str(obj(action)["do"]) for action in seq(doc.get("actions", [])))
    assert kinds == {"auto", "manual"}
    # acknowledge and drive_latency are exercised; every action name is used somewhere
    assert set(ACTION_NAMES) <= used, set(ACTION_NAMES) - used


def test_every_shipped_profile_has_a_nominal_scenario() -> None:
    from simulation.scenario import shipped_profile_ids  # noqa: PLC0415

    profiles = {document(p.read_text(encoding="utf-8")).get("profile") for p in PATHS}
    assert set(shipped_profile_ids()) <= profiles
