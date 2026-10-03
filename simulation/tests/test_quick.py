"""``python -m simulation.quick``: targets, the fast path, the report files, the exit code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation import quick
from simulation.cohort.generate import load_cohort
from simulation.failures import cases
from simulation.quick import (
    EMPTY,
    Job,
    JobKind,
    chart,
    execute,
    fast,
    jobs_for,
    main,
    status_of,
)
from simulation.scenario import SCENARIO_DIR, EcgMode
from simulation.tests.conftest import document, load, seq


def _report(out: Path) -> dict[str, object]:
    return dict(document((out / "report.json").read_text(encoding="utf-8")))


def test_one_scenario_writes_a_verdict_and_both_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["manual_27_rpm", "--out", str(tmp_path), "--workers", "1"]) == 0
    printed = capsys.readouterr().out
    assert "PASS" in printed
    assert "manual_27_rpm" in printed
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "<svg" in page
    assert "prefers-color-scheme: dark" in page
    report = _report(tmp_path)
    assert report["counts"] == {"PASS": 1}


def test_a_subject_runs_its_three_sessions_over_processes(tmp_path: Path) -> None:
    refused = next(s for s in load_cohort() if s.subject_id == "S08")
    assert main([refused.subject_id.lower(), "--out", str(tmp_path), "--workers", "2"]) == 0
    assert len(seq(_report(tmp_path)["runs"])) == 3
    page = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "no data" in page  # the refused programme has no trace


def test_a_failure_case_and_a_dsp_case_without_the_dsp(tmp_path: Path) -> None:
    assert main(["drive_comm_timeout_auto_baseline", "--out", str(tmp_path)]) == 0
    refused = execute(Job(JobKind.COHORT, "S08:auto_jog"))
    assert refused.end == "refused"
    assert "zone" in refused.messages[0][1]
    assert execute(Job(JobKind.FAILURE, "ecg_dsp_flat")).status == "SKIPPED"


def test_a_dsp_scenario_runs_on_the_fast_sensor_unless_asked() -> None:
    base = load(SCENARIO_DIR / "fault_bitalino_disconnect_dsp.json")
    fast_one = fast(base)
    assert fast_one is not None
    assert fast_one.ecg.mode is EcgMode.DIRECT
    direct = load(SCENARIO_DIR / "manual_27_rpm.json")
    assert fast(direct) is direct
    path = str(SCENARIO_DIR / "manual_32_rpm_refused.json")
    assert execute(Job(JobKind.SCENARIO, path, dsp=True)).status == "PASS"


@pytest.mark.parametrize("name", ["fault_ecg_electrode_off_dsp", "fault_ecg_mains_burst_dsp"])
def test_waveform_artifacts_need_real_dsp_and_keep_their_safety_expectations(name: str) -> None:
    path = SCENARIO_DIR / f"{name}.json"
    assert fast(load(path)) is None
    assert execute(Job(JobKind.SCENARIO, str(path))).status == "SKIPPED"
    report = execute(Job(JobKind.SCENARIO, str(path), dsp=True))
    assert report.status == "PASS"
    assert "hr_stale" in report.rules


def test_a_scenario_that_needs_the_dsp_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "needs_dsp.json"
    path.write_text(
        json.dumps(
            {
                "name": "needs_dsp",
                "kind": "manual",
                "duration_s": 5,
                "ecg": {"mode": "dsp"},
                "actions": [
                    {"at_s": 1, "do": "bitalino_signal", "signal": "flat", "duration_s": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert execute(Job(JobKind.SCENARIO, str(path))).status == "SKIPPED"
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        execute(Job(JobKind.SCENARIO, str(broken)))


def test_a_failing_run_fails_the_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "wrong.json"
    path.write_text(
        json.dumps(
            {
                "name": "wrong",
                "kind": "manual",
                "duration_s": 5,
                "expect": {"end_reason": "programme_complete"},
            }
        ),
        encoding="utf-8",
    )
    assert main([str(path), "--out", str(tmp_path)]) == 1


def test_the_batteries_and_all_select_their_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    few_cases = cases()[:1]
    few_subjects = load_cohort()[:1]
    monkeypatch.setattr(quick, "cases", lambda: few_cases)
    monkeypatch.setattr(quick, "load_cohort", lambda: few_subjects)
    monkeypatch.setattr(
        quick, "scenario_paths", lambda: (SCENARIO_DIR / "manual_32_rpm_refused.json",)
    )
    assert main(["--all", "--out", str(tmp_path), "--workers", "1"]) == 0
    assert len(seq(_report(tmp_path)["runs"])) == 1 + 1 + 3
    assert main(["--failures", "--cohort", "--dsp", "--out", str(tmp_path), "--workers", "1"]) == 0


def test_an_unknown_target_or_no_target_is_refused() -> None:
    with pytest.raises(ValueError, match="neither"):
        jobs_for("nothing_of_the_kind", dsp=False)
    with pytest.raises(SystemExit):
        main([])


def test_the_small_pieces() -> None:
    assert status_of([], None) == "PASS"
    assert status_of(["x"], None) == "FAIL"
    assert status_of(["x"], "d") == "XFAIL"
    assert status_of([], "d") == "FIXED?"
    assert "no data" in chart("empty", EMPTY.t, [("a", "--s1", EMPTY.output_rpm)])
    drawn = chart("hr", (0.0, 1.0, 2.0), [("a", "--s1", (70, None, 72))], (145, 155))
    assert drawn.count("M") >= 2  # the pen lifted over the gap
    assert "zone" in drawn
