from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from simulation import signal_loss as loss
from simulation import signal_readings as dsp
from simulation.harness import EcgObservation, RunResult
from simulation.scenario import EcgMode, Scenario
from simulation.tests.conftest import document, obj, seq
from simulation.tracefile import JsonValue, Row
from src.training.types import Phase, SafetyAction, SignalQuality, ZoneCounters
from src.units import Bpm, MotorRpm, Seconds


def row(t: float, rpm: int = 0, phase: str = "warmup") -> Row:
    return Row(
        t=t,
        state="running",
        mode="seance",
        phase=phase,
        drive_state="OPERATION_ENABLED",
        sim_state="OPERATION_ENABLED",
        setpoint_motor_rpm=rpm,
        lfrd_motor_rpm=rpm,
        measured_motor_rpm=rpm,
        measured_fresh=True,
        output_rpm=0,
        hertz=0,
        g_reference=0,
        g_leg_tip=0,
        setpoint_output_rpm=0,
        setpoint_g_leg_tip=0,
        manual_target_motor_rpm=0,
        hr_true=70,
        hr_live=70,
        target_bpm=120,
        safety_action="NONE",
        safety_rule=None,
        output_enabled=True,
        silent=False,
        current_a=None,
    )


def reading(t: float, *, accepted: bool = True, bpm: Bpm | None = Bpm(70)) -> EcgObservation:
    return EcgObservation(Seconds(t), round(t * 100), SignalQuality.GOOD, bpm, accepted)


@pytest.fixture(scope="module")
def baseline() -> RunResult:
    return loss.run(
        replace(loss.load("manual_occupied_ceiling"), duration=Seconds(2), teardown=Seconds(0))
    )


def with_inputs(
    baseline: RunResult, rows: Sequence[Row], readings: Sequence[EcgObservation]
) -> RunResult:
    return replace(
        baseline, trace=replace(baseline.trace, rows=tuple(rows)), readings=tuple(readings)
    )


def test_grid_is_exact_and_subset_is_stable(
    monkeypatch: pytest.MonkeyPatch, baseline: RunResult
) -> None:
    rows = tuple(
        row(float(i), 0 if i < 20 else i, "warmup" if i < 50 else "hold") for i in range(130)
    )

    def no_loss(scenario: Scenario) -> RunResult:
        assert scenario.ecg.mode is EcgMode.DIRECT
        return replace(baseline, trace=replace(baseline.trace, rows=rows))

    monkeypatch.setattr(loss, "run", no_loss)
    cases = loss.select_cases()
    assert len(cases) == 724
    assert len({case.name for case in cases}) == 724
    assert Counter(case.category for case in cases) == {
        loss.Category.REST: 240,
        loss.Category.ROTATING: 408,
        loss.Category.MANUAL: 76,
    }
    assert [case.at for case in cases[:30]] == [Seconds(14 + i * 0.2) for i in range(30)]
    assert {
        case.scenario.subject for case in cases if case.category is not loss.Category.MANUAL
    } == {loss.load(f"auto_jog_subject_{subject}").subject for subject in loss.SUBJECTS}
    subset = loss.select_cases(subset=True)
    assert subset == loss.select_cases(subset=True)
    assert len(subset) == 51
    assert set(subset).issubset(cases)
    with pytest.raises(ValueError, match="eligible ticks"):
        loss.sample_times([], 26)


def test_extract_uses_reading_times_and_each_applied_increment(baseline: RunResult) -> None:
    case = loss.LossCase("moving", loss.Category.ROTATING, baseline.scenario, Seconds(1))
    rows = [row(0.8, 224), row(1.0, 224), row(1.2, 226), row(2.2, 230), row(3.2, 230)]
    rows[-1] = replace(rows[-1], safety_action="FREEZE", safety_rule="hr_stale")
    result = with_inputs(baseline, rows, [reading(0.01), reading(1.01, accepted=False, bpm=None)])
    measured = loss.extract(case, result)
    assert measured.last_usable == Seconds(0.01)
    assert measured.first_missing == Seconds(1.01)
    assert measured.before == MotorRpm(224)
    assert measured.max_rise == MotorRpm(6)
    assert [(r.t, r.amount) for r in measured.rises] == [(Seconds(1.2), 2), (Seconds(2.2), 4)]
    assert measured.first_verdict == (Seconds(3.2), SafetyAction.FREEZE, "hr_stale")
    assert measured.violations(Seconds(4)) == ()
    encoded = measured.to_json()
    assert encoded["last_rise_after_usable_s"] == pytest.approx(2.19)
    assert encoded["last_rise_after_missing_s"] == pytest.approx(1.19)
    assert not encoded["starts_from_rest"]
    assert encoded["rises_under_verdict"] == []
    assert obj(encoded["first_verdict"])["rule"] == "hr_stale"


def test_acceptance_detects_late_rise_start_and_standing_verdict(baseline: RunResult) -> None:
    case = loss.LossCase("start", loss.Category.REST, baseline.scenario, Seconds(1))
    rows = [
        row(1),
        row(2.6, 67),
        replace(row(3, 68), safety_action="FREEZE", safety_rule="hr_stale"),
    ]
    measured = loss.extract(
        case, with_inputs(baseline, rows, [reading(0), reading(1, accepted=False, bpm=None)])
    )
    reasons = measured.violations(Seconds(2.5))
    assert reasons == (
        "late_rise@2.600",
        "start_without_recent_reading@2.600",
        "late_rise@3.000",
        "rise_under_verdict@3.000",
    )
    assert measured.violations(Seconds(4)) == ("rise_under_verdict@3.000",)
    assert measured.to_json()["starts_from_rest"]
    assert len(seq(measured.to_json()["rises_under_verdict"])) == 1
    no_last = replace(measured, last_usable=None)
    assert "start_without_recent_reading@2.600" in no_last.violations(Seconds(999))
    assert no_last.to_json()["last_rise_after_usable_s"] is None
    action_only = replace(measured, rises=(replace(measured.rises[0], action=SafetyAction.FREEZE),))
    assert action_only.violations(Seconds(4)) == ("rise_under_verdict@2.600",)
    assert len(seq(action_only.to_json()["rises_under_verdict"])) == 1


def test_no_rise_and_no_previous_reading_are_explicit(baseline: RunResult) -> None:
    case = loss.LossCase("none", loss.Category.MANUAL, baseline.scenario, Seconds(0))
    measured = loss.extract(
        case, with_inputs(baseline, [row(0.2), row(1)], [reading(0, accepted=False, bpm=None)])
    )
    assert measured.last_usable is None
    assert measured.before == measured.max_rise == 0
    assert measured.rises == ()
    assert measured.violations(Seconds(0)) == ()
    encoded = measured.to_json()
    assert encoded["last_rise_after_usable_s"] is None
    assert encoded["last_rise_after_missing_s"] is None
    assert encoded["first_verdict"] is None
    summary = loss.loss_summary([measured], [Seconds(4)])
    assert obj(obj(summary["categories"])["programme_rest"])["max_rise_motor_rpm"] == 0
    assert obj(summary["acceptance"])["4"] == []


@pytest.mark.parametrize("jobs", [1, 2])
def test_cli_replays_real_runtime_and_writes_only_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: RunResult, jobs: int
) -> None:
    case = loss.LossCase(
        "manual", loss.Category.MANUAL, replace(baseline.scenario, duration=Seconds(25)), Seconds(4)
    )

    def selection(*, subset: bool = False) -> tuple[loss.LossCase, ...]:
        assert subset
        return (case,)

    monkeypatch.setattr(loss, "select_cases", selection)
    assert loss.main(["--out", str(tmp_path), "--jobs", str(jobs), "--subset"]) == 0
    summary = document((tmp_path / "summary.json").read_text())
    measured = obj(seq(summary["measurements"])[0])
    assert summary["count"] == 1
    assert measured["first_missing_s"] == pytest.approx(4)
    assert measured["last_usable_s"] == pytest.approx(3)
    assert measured["starts_from_rest"] is True
    assert obj(measured["first_verdict"])["rule"] == "hr_stale"
    assert {path.name for path in tmp_path.iterdir()} == {
        "manual.csv",
        "instants.csv",
        "summary.json",
    }
    with (tmp_path / "manual.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 1
    assert loss.main(["--out", str(tmp_path), "--jobs", "1", "--subset", "--delay", "0"]) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        [],
        ["--out", ".", "--jobs", "5"],
        ["--out", ".", "--delay", "nan"],
        ["--out", ".", "--delay", "-1"],
    ],
)
def test_cli_usage(args: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        loss.main(args)
    assert error.value.code == (0 if args == ["--help"] else 2)


def test_invalid_scenarios_are_reported(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(ValueError, match="kind"):
        loss.load(str(bad))
    with pytest.raises(ValueError, match="kind"):
        dsp.reference_scenario(str(bad))


def test_gap_exposure_is_clipped_by_phase_and_threshold() -> None:
    rows = [row(10), row(15), row(20, phase="hold"), row(25, phase="hold")]
    gaps = [(Seconds(10), Seconds(20))]
    assert dsp.exposure(gaps, rows, Seconds(2.5), Phase.WARMUP) == {"count": 1, "seconds": 2.5}
    assert dsp.exposure(gaps, rows, Seconds(2.5), Phase.HOLD) == {"count": 1, "seconds": 5.0}
    assert dsp.exposure(gaps, rows, Seconds(10), Phase.HOLD) == {"count": 0, "seconds": 0.0}


def test_dsp_histogram_uses_accepted_sequences_and_censored_tail(baseline: RunResult) -> None:
    accepted = [-1.0, 0.0, 2.0, 5.0, 9.0, 14.0, 24.0, 35.0, 55.0, 76.0]
    readings = [reading(t) for t in accepted]
    readings += [reading(1, accepted=False, bpm=None), reading(3, accepted=False)]
    rows = [row(1), replace(row(10, 100, "hold"), hr_live=120), row(80, 100, "hold")]
    rows += [replace(row(85, 0, "done"), state="finished"), row(90)]
    rows[0] = replace(rows[0], safety_action="FREEZE", safety_rule="hr_stale", hr_live=None)
    result = replace(
        with_inputs(baseline, rows, readings),
        profile=loss.load("auto_standard_30_min").profile,
        counters=ZoneCounters(Seconds(2), Seconds(3), Seconds(4)),
    )
    measured = dsp.measure_readings(result)
    assert measured["duration_s"] == 85
    assert measured["gap_histogram"] == {
        "1": 1,
        "2": 1,
        "3": 1,
        "4": 1,
        "5-10": 2,
        "10-20": 2,
        ">20": 1,
    }
    assert measured["accepted"] == 9
    assert measured["without_rate"] == 1
    assert measured["without_rate_fraction"] == pytest.approx(1 / 11)
    assert measured["censored_tail_s"] == 9
    assert measured["longest_completed_gap_s"] == 21
    assert measured["first_movement_s"] == 10
    assert measured["warmup_zone_arrival_s"] == 10
    assert measured["zone_reached_s"] == 10
    assert obj(measured["counters_s"])["total"] == 9
    assert seq(measured["verdicts"])[-1] == {"t": 10, "action": "NONE", "rule": None}
    for phase in ("warmup", "hold"):
        assert obj(obj(obj(measured["gap_exposure"])[phase])["1.5"])["seconds"] is not None


def test_dsp_empty_and_unfinished_inputs(baseline: RunResult) -> None:
    measured = dsp.measure_readings(with_inputs(baseline, [row(1), row(10)], []))
    assert measured["duration_s"] == 10
    assert measured["without_rate_fraction"] is None
    assert measured["first_movement_s"] is None
    assert measured["longest_completed_gap_s"] is None
    assert measured["censored_tail_s"] == 10
    assert measured["warmup_zone_arrival_s"] is None
    assert measured["zone_reached_s"] is None
    assert measured["completed_gaps"] == 0
    profile = loss.load("auto_standard_30_min").profile
    result = replace(with_inputs(baseline, [row(1)], [reading(0)]), profile=profile)
    assert dsp.measure_readings(result)["warmup_ended_early"] is False


def test_reference_runs_real_dsp(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = replace(
        dsp.reference_scenario("auto_jog_150_dsp"),
        duration=Seconds(2),
        preroll=Seconds(2),
        teardown=Seconds(0),
    )
    assert scenario.ecg.mode is EcgMode.DSP

    def short_scenario(_name: str) -> Scenario:
        return scenario

    monkeypatch.setattr(dsp, "reference_scenario", short_scenario)
    measured = dsp.reference("short")
    assert measured["duration_s"] == pytest.approx(2)
    assert measured["readings"] == 11


def short_reference(name: str) -> dict[str, JsonValue]:
    return {"scenario": name, "duration_s": 2}


@pytest.mark.parametrize("jobs", [1, 2])
def test_dsp_cli_exports_all_reference_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jobs: int
) -> None:
    monkeypatch.setattr(dsp, "reference", short_reference)
    assert loss.main(["--out", str(tmp_path), "--jobs", str(jobs), "--dsp"]) == 0
    summary = document((tmp_path / "summary.json").read_text())
    assert [obj(entry)["scenario"] for entry in seq(summary["sessions"])] == list(dsp.REFERENCES)
    with (tmp_path / "dsp.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 7
