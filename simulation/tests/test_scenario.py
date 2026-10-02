"""The scenario schema: every mistake a scenario author can make is named, never ignored."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.harness import Session
from simulation.scenario import (
    ACTION_NAMES,
    SCENARIO_DIR,
    Acknowledge,
    DriveLatency,
    EcgDropout,
    EcgMode,
    EcgQuality,
    EcgSpec,
    Expectation,
    ManualTarget,
    Scenario,
    SessionKind,
    load_scenario,
    parse_scenario,
    resolve,
    scenario_paths,
    with_duration,
)
from simulation.tests.conftest import document, obj, seq
from src.motor.drive import DriveFault
from src.result import Err, Ok
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy, SignalQuality
from src.units import Metres, OutputRpm, Seconds

MINIMAL = {"name": "m", "kind": "manual", "duration_s": 10}


def _parse(document: object) -> Scenario:
    parsed = parse_scenario(json.dumps(document))
    if isinstance(parsed, Err):
        raise AssertionError(parsed.error.detail)
    return parsed.value


def _problems(document: object) -> str:
    parsed = parse_scenario(json.dumps(document))
    assert isinstance(parsed, Err), "the document was accepted"
    return parsed.error.detail


def test_every_scenario_file_parses_and_has_a_unique_name() -> None:
    names: list[str] = []
    for path in scenario_paths():
        loaded = load_scenario(path)
        assert isinstance(loaded, Ok), loaded
        assert loaded.value.name == path.stem
        assert loaded.value.description
        names.append(loaded.value.name)
    assert len(names) == len(set(names))
    assert len(names) >= 50


def test_a_minimal_manual_scenario_takes_every_default() -> None:
    scenario = _parse(MINIMAL)
    assert scenario.kind is SessionKind.MANUAL
    assert scenario.manual.occupancy is Occupancy.BENCH
    assert scenario.manual.ceiling == 1380
    assert scenario.ecg == EcgSpec()
    assert scenario.expect.start is Expectation.ACCEPTED
    assert scenario.known_defect is None
    assert with_duration(scenario, Seconds(5.0)).duration == 5.0


def test_a_full_scenario_reads_every_field() -> None:
    scenario = _parse(
        {
            **MINIMAL,
            "kind": "auto",
            "profile": "jog_150_short",
            "profile_overrides": {"total_duration_s": 800},
            "geometry": {"reference_radius_m": 2.0, "leg_tip_radius_m": 1.4},
            "subject": {"hr_rest": 60, "k_g": 100},
            "subject_events": [{"event": "vasovagal_drop", "at_s": 100, "duration_s": 20}],
            "ecg": {"mode": "dsp", "period_s": 0.5, "noise_bpm": 1, "seed": 3},
            "drive": {
                "tto_s": 2,
                "acceleration_time_s": 8,
                "hsp_motor_rpm": 1300,
                "initial_fault": "OVERCURRENT",
            },
            "actions": [
                {"at_s": 9, "do": "acknowledge", "estop_released": False},
                {"at_s": 1, "do": "drive_latency", "latency_s": 1, "duration_s": 2},
                {"at_s": 2, "do": "ecg_quality", "quality": "noisy", "duration_s": 3},
            ],
            "expect": {
                "start": "refused",
                "end_reason": "emergency_stop",
                "final_state": "finished",
                "rules": ["x"],
                "forbid_rules": ["y"],
                "reaches_output_rpm": 1,
                "max_output_rpm": 2,
                "min_in_zone_fraction": 0.5,
            },
            "known_defect": "because",
        }
    )
    assert scenario.profile is not None
    assert float(scenario.profile.total_duration_s) == 800.0
    assert scenario.reference_radius == Metres(2.0)
    assert scenario.subject.hr_rest == 60
    assert float(scenario.events[0].start) == 100.0
    assert scenario.ecg.mode is EcgMode.DSP
    assert scenario.drive.initial_fault is DriveFault.OVERCURRENT
    assert [type(a) for a in scenario.actions] == [DriveLatency, EcgQuality, Acknowledge]
    assert scenario.actions[1] == EcgQuality(Seconds(2.0), SignalQuality.NOISY, Seconds(3.0))
    assert scenario.expect.end_reason is EndReason.EMERGENCY_STOP
    assert scenario.expect.final_state is RuntimeState.FINISHED
    assert scenario.known_defect == "because"


def test_an_inline_profile_document_is_accepted() -> None:
    shipped = document((SCENARIO_DIR / "_profiles.json").read_text(encoding="utf-8"))
    profile = dict(obj(seq(shipped["profiles"])[0]))
    scenario = _parse({**MINIMAL, "kind": "auto", "profile": profile})
    assert scenario.profile is not None
    assert scenario.profile.profile_id == "jog_150_30_min"


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ({**MINIMAL, "typo": 1}, "unknown key 'typo'"),
        ({"name": "m", "duration_s": 1}, "missing 'kind'"),
        ({**MINIMAL, "kind": "sideways"}, "not a valid SessionKind"),
        ({**MINIMAL, "duration_s": 0}, "'duration_s' must be positive"),
        ({**MINIMAL, "duration_s": "long"}, "'duration_s' must be a number"),
        ({**MINIMAL, "teardown_s": -1}, "'teardown_s' must not be negative"),
        ({**MINIMAL, "name": 3}, "'name' must be a string"),
        ({**MINIMAL, "tags": "x"}, "'tags' must be a list of strings"),
        ({**MINIMAL, "profile": "standard_30_min"}, "'profile' belongs to an auto scenario"),
        ({**MINIMAL, "kind": "auto", "profile": "nope"}, "is not a known profile"),
        ({**MINIMAL, "kind": "auto", "profile": 3}, "must be a shipped profile id or a profile"),
        (
            {
                **MINIMAL,
                "kind": "auto",
                "profile": "standard_30_min",
                "profile_overrides": {"zone_high_bpm": 200},
            },
            "profile refused",
        ),
        ({**MINIMAL, "geometry": {"radius": 1}}, "unknown key 'radius'"),
        ({**MINIMAL, "geometry": 3}, "'geometry' must be an object"),
        ({**MINIMAL, "subject": {"hr_rest": 300}}, "heart rates must be ordered"),
        ({**MINIMAL, "subject": {"k_g": True}}, "'k_g' must be a number"),
        ({**MINIMAL, "subject": {"hr_rest": 1.5}}, "'hr_rest' must be an integer"),
        (
            {**MINIMAL, "subject_events": [{"event": "nope", "at_s": 0, "duration_s": 1}]},
            "not a valid ScriptedEvent",
        ),
        (
            {**MINIMAL, "subject_events": [{"event": "hr_spike", "at_s": -1, "duration_s": 1}]},
            "must not be negative",
        ),
        ({**MINIMAL, "subject_events": [3]}, "'subject_events'[0] must be an object"),
        ({**MINIMAL, "subject_events": 3}, "must be a list of objects"),
        ({**MINIMAL, "ecg": {"mode": "x"}}, "not a valid EcgMode"),
        ({**MINIMAL, "ecg": {"period_s": 0}}, "'period_s' must be positive"),
        ({**MINIMAL, "ecg": {"noise_bpm": -1}}, "'noise_bpm' must not be negative"),
        ({**MINIMAL, "drive": {"tto_s": 0}}, "every drive number must be positive"),
        ({**MINIMAL, "drive": {"initial_fault": "SMOKE"}}, "is not a DriveFault"),
        ({**MINIMAL, "manual": {"occupancy": "crowd"}}, "not a valid Occupancy"),
        ({**MINIMAL, "actions": [{"at_s": 1, "do": "dance"}]}, "'do' = 'dance'"),
        ({**MINIMAL, "actions": [{"at_s": -1, "do": "estop"}]}, "'at_s' must not be negative"),
        ({**MINIMAL, "actions": [{"at_s": 1, "do": "estop", "speed": 3}]}, "unknown key 'speed'"),
        (
            {**MINIMAL, "actions": [{"at_s": 1, "do": "drive_fault", "fault": "SMOKE"}]},
            "is not a DriveFault",
        ),
        (
            {**MINIMAL, "actions": [{"at_s": 1, "do": "ecg_quality", "duration_s": 1}]},
            "missing 'quality'",
        ),
        (
            {**MINIMAL, "actions": [{"at_s": 1, "do": "comms_loss", "duration_s": 0}]},
            "'duration_s' must be positive",
        ),
        (
            {
                **MINIMAL,
                "actions": [{"at_s": 1, "do": "manual_target", "output_rpm": 1, "expect": "maybe"}],
            },
            "not a valid Expectation",
        ),
        (
            {**MINIMAL, "actions": [{"at_s": 1, "do": "acknowledge", "estop_released": 1}]},
            "'estop_released' must be true or false",
        ),
        ({**MINIMAL, "expect": {"end_reason": "boredom"}}, "not a valid EndReason"),
        ({**MINIMAL, "expect": {"sometimes": 1}}, "unknown key 'sometimes'"),
        ({**MINIMAL, "expect": {"reaches_output_rpm": float("inf")}}, "must be finite"),
        ({**MINIMAL, "known_defect": 3}, "'known_defect' must be a string"),
    ],
)
def test_a_mistake_is_named(document: object, fragment: str) -> None:
    assert fragment in _problems(document)


@pytest.mark.parametrize("raw", ["nope", "[1]"])
def test_a_document_that_is_not_an_object_is_refused(raw: str) -> None:
    parsed = parse_scenario(raw)
    assert isinstance(parsed, Err)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    loaded = load_scenario(tmp_path / "absent.json")
    assert isinstance(loaded, Err)
    assert "absent.json" in loaded.error.detail


def test_a_scenario_is_found_by_name_or_by_path(tmp_path: Path) -> None:
    assert resolve("manual_27_rpm") == SCENARIO_DIR / "manual_27_rpm.json"
    own = tmp_path / "mine.json"
    assert resolve(str(own)) == own
    own.write_text("{}", encoding="utf-8")
    assert resolve(str(tmp_path / "mine.json")) == own


def test_every_action_name_has_a_builder() -> None:
    for name in ACTION_NAMES:
        extra: dict[str, object] = {
            "output_rpm": 1.0,
            "fault": "OVERCURRENT",
            "duration_s": 1.0,
            "latency_s": 1.0,
            "quality": "noisy",
            "bpm": 90,
            "jump_s": 60.0,
            "signal": "flat",
        }
        keys = {
            "manual_target": ["output_rpm"],
            "drive_fault": ["fault"],
            "comms_loss": ["duration_s"],
            "drive_latency": ["latency_s", "duration_s"],
            "ecg_dropout": ["duration_s"],
            "ecg_quality": ["quality", "duration_s"],
            "ecg_repeat_seq": ["duration_s"],
            "ecg_value": ["bpm", "duration_s"],
            "loop_stall": ["duration_s"],
            "clock_jump": ["jump_s"],
            "bitalino_signal": ["signal", "duration_s"],
        }.get(name, [])
        action = {"at_s": 1, "do": name, **{key: extra[key] for key in keys}}
        assert len(_parse({**MINIMAL, "actions": [action]}).actions) == 1, name


def test_the_harness_refuses_what_it_cannot_run_faithfully() -> None:
    base = _parse(MINIMAL)
    dsp = replace(
        base,
        ecg=EcgSpec(mode=EcgMode.DSP),
        actions=(EcgDropout(Seconds(1.0), Seconds(1.0)),),
    )
    with pytest.raises(ValueError, match=r"needs ecg\.mode 'direct'"):
        Session(dsp)
    with pytest.raises(ValueError, match="needs a profile"):
        Session(replace(base, kind=SessionKind.AUTO))
    with pytest.raises(ValueError, match="geometry unusable"):
        Session(replace(base, leg_tip_radius=Metres(-2.0)))
    target = ManualTarget(Seconds(1.0), OutputRpm(1.0), Expectation.ANY)
    assert target.expect is Expectation.ANY
