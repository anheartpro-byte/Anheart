"""The 30-subject cohort: reproducible data, and the battery of 90 runs over the real runtime."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from simulation.cohort import battery
from simulation.cohort.battery import (
    CohortOutcome,
    Eligibility,
    SessionType,
    base_profile,
    eligibility,
    known_defect_for,
    minor_is_refused_or_capped,
    pairs,
    run_cohort,
    run_subject,
)
from simulation.cohort.generate import (
    COHORT_PATH,
    MAX_AGE,
    MIN_AGE,
    SIZE,
    CohortSubject,
    Condition,
    dumps,
    generate,
    load_cohort,
    main,
    parse_cohort,
    tanaka,
)
from simulation.scenario import SIM_PROFILES_PATH
from src.local_config import DEFAULT_MIN_RIDER_AGE
from src.result import Err, Ok
from src.training.plan import ProfileStore, UnknownProfile

SUBJECTS: Final[tuple[CohortSubject, ...]] = load_cohort()
BY_ID: Final[dict[str, CohortSubject]] = {subject.subject_id: subject for subject in SUBJECTS}
_OUTCOMES: dict[tuple[str, SessionType], CohortOutcome] = {}


def _outcome(subject: CohortSubject, session: SessionType) -> CohortOutcome:
    """The whole cohort, run once per session over every CPU, then looked up."""
    if not _OUTCOMES:
        for outcome in run_cohort(SUBJECTS, workers=os.cpu_count() or 1):
            _OUTCOMES[(outcome.subject.subject_id, outcome.session)] = outcome
    return _OUTCOMES[(subject.subject_id, session)]


def _jog_gate(subject: CohortSubject) -> Eligibility:
    gate = eligibility(subject, SessionType.AUTO_JOG)
    if gate is None:
        raise AssertionError("an auto session always has a gate")
    return gate


def _refused_adult() -> CohortSubject:
    """An adult the jog refuses on physiology (zone above 90% of HRmax), not on age."""
    return next(
        s
        for s in SUBJECTS
        if s.age_years >= DEFAULT_MIN_RIDER_AGE and _jog_gate(s).refusal is not None
    )


# -- the data ------------------------------------------------------------------


def test_the_committed_cohort_is_exactly_what_the_seed_generates() -> None:
    assert COHORT_PATH.read_text(encoding="utf-8") == dumps(generate())
    assert main(["--check"]) == 0


def test_the_generator_writes_and_detects_a_stale_file(tmp_path: Path) -> None:
    out = tmp_path / "cohort.json"
    assert main(["--check", "--out", str(out)]) == 1
    assert main(["--out", str(out)]) == 0
    assert main(["--check", "--out", str(out)]) == 0
    assert load_cohort(out) == SUBJECTS


def test_the_cohort_spans_the_brief() -> None:
    assert len(SUBJECTS) == SIZE == 30
    ages = [subject.age_years for subject in SUBJECTS]
    assert min(ages) == MIN_AGE == 10
    assert max(ages) <= MAX_AGE
    assert sum(subject.minor for subject in SUBJECTS) == 5
    conditions = {subject.condition for subject in SUBJECTS}
    assert conditions == set(Condition)
    for subject in SUBJECTS:
        assert subject.hr_max_tanaka == tanaka(subject.age_years)
        assert abs(subject.hr_max - subject.hr_max_tanaka) <= 15
        config = subject.physiology()  # the plant accepts every subject
        assert config.hr_rest < config.hr_max
        assert (subject.vasovagal_at_s is not None) is (
            subject.condition is Condition.VASOVAGAL_PRONE
        )
    assert {subject.sex.value for subject in SUBJECTS} == {"female", "male"}


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ("[]", "top level"),
        ('{"subjects": []}', "no subjects"),
        ('{"subjects": 3}', "list of objects"),
        ('{"subjects": [{"sex": "robot"}]}', "not a Sex"),
    ],
)
def test_a_broken_cohort_file_is_named(raw: str, fragment: str) -> None:
    parsed = parse_cohort(raw)
    assert isinstance(parsed, Err)
    assert fragment in parsed.error.detail


def test_a_missing_or_malformed_cohort_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        load_cohort(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.json"):
        load_cohort(bad)


def test_a_subject_round_trips_through_json() -> None:
    parsed = parse_cohort(json.dumps({"subjects": [SUBJECTS[3].to_json()]}))
    assert isinstance(parsed, Ok)
    assert parsed.value == (SUBJECTS[3],)


# -- the gate ------------------------------------------------------------------


def test_the_real_gate_refuses_exactly_the_riders_the_brief_says() -> None:
    for subject in SUBJECTS:
        gate = _jog_gate(subject)
        assert (gate.refusal is not None) is gate.expected_refusal, subject
        assert (gate.refusal is not None) is (
            subject.age_years < DEFAULT_MIN_RIDER_AGE or 0.9 * subject.hr_max < 155
        )
    assert eligibility(SUBJECTS[0], SessionType.MANUAL_BENCH) is None
    assert not _refused_adult().minor


MINORS: Final[tuple[CohortSubject, ...]] = tuple(s for s in SUBJECTS if s.minor)
ADULTS: Final[tuple[CohortSubject, ...]] = tuple(
    s for s in SUBJECTS if s.age_years >= DEFAULT_MIN_RIDER_AGE
)
"""Riders the console's age gate lets through to a programme."""


@pytest.mark.parametrize(
    "subject",
    [pytest.param(subject) for subject in MINORS],
    ids=[subject.subject_id for subject in MINORS],
)
def test_a_rider_under_16_is_refused_or_capped(subject: CohortSubject) -> None:
    assert minor_is_refused_or_capped(subject)


def test_a_refused_adult_counts_as_refused() -> None:
    assert minor_is_refused_or_capped(_refused_adult())


def test_an_unknown_profile_is_an_error_not_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="nope"):
        base_profile("nope", SIM_PROFILES_PATH)

    def vanished(*_args: object, **_kwargs: object) -> Err[UnknownProfile]:
        return Err(UnknownProfile(profile_id="jog_150_30_min", known=()))

    monkeypatch.setattr(ProfileStore, "resolve", vanished)
    with pytest.raises(ValueError, match="vanished"):
        eligibility(ADULTS[0], SessionType.AUTO_JOG)


def test_an_unusable_profile_library_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "defaults.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setitem(
        battery._PROFILE_OF,  # pyright: ignore[reportPrivateUsage]  # the table under test
        SessionType.AUTO_JOG,
        ("jog_150_30_min", broken),
    )
    with pytest.raises(ValueError, match="unusable"):
        eligibility(ADULTS[0], SessionType.AUTO_JOG)


def test_the_status_of_an_outcome() -> None:
    subject = SUBJECTS[0]
    base = CohortOutcome(
        subject=subject,
        session=SessionType.MANUAL_BENCH,
        eligibility=None,
        metrics=None,
        end="-",
        violations=(),
        known_defect=None,
    )
    assert base.status == "PASS"
    assert replace(base, violations=("x",)).status == "FAIL"
    assert replace(base, violations=("x",), known_defect="d").status == "XFAIL"
    assert replace(base, known_defect="d").status == "FIXED?"


def test_a_gate_that_disagrees_with_the_brief_is_a_violation() -> None:
    check = battery._eligibility_violations  # pyright: ignore[reportPrivateUsage]  # the check itself
    assert check(None) == []
    wrong_refusal = Eligibility(profile=None, refusal="no", expected_refusal=False)
    assert "refused although" in check(wrong_refusal)[0]
    wrong_accept = Eligibility(profile=None, refusal=None, expected_refusal=True)
    assert "accepted although" in check(wrong_accept)[0]


def test_one_pair_of_each_kind_in_this_process() -> None:
    """The coverage of the battery itself (the full battery runs over processes)."""
    assert run_subject(_refused_adult(), SessionType.AUTO_JOG).end == "refused"
    kept: list[object] = []
    vasovagal = next(s for s in ADULTS if s.condition is Condition.VASOVAGAL_PRONE)
    outcome = run_subject(vasovagal, SessionType.AUTO_JOG, keep=kept.append)
    assert kept
    assert outcome.status == "XFAIL"
    nonresponder = next(s for s in SUBJECTS if s.condition is Condition.NONRESPONDER)
    assert run_subject(nonresponder, SessionType.AUTO_JOG).status == "PASS"
    only = run_cohort([SUBJECTS[1]], [SessionType.MANUAL_BENCH])
    assert [o.status for o in only] == ["PASS"]
    assert len(pairs(SUBJECTS)) == 90


# -- the battery ---------------------------------------------------------------


def _pair_params() -> Iterator[object]:
    for subject, session in pairs(SUBJECTS):
        defect = known_defect_for(subject, session)
        marks = () if defect is None else (pytest.mark.xfail(strict=True, reason=defect),)
        yield pytest.param(
            subject, session, marks=marks, id=f"{subject.subject_id}-{session.value}"
        )


@pytest.mark.parametrize(("subject", "session"), list(_pair_params()))
def test_the_subject_session_holds_every_invariant(
    subject: CohortSubject, session: SessionType
) -> None:
    outcome = _outcome(subject, session)
    assert not outcome.violations, "\n".join(outcome.violations)


@pytest.mark.parametrize(
    ("subject", "session"),
    pairs(SUBJECTS),
    ids=[f"{subject.subject_id}-{session.value}" for subject, session in pairs(SUBJECTS)],
)
def test_every_subject_session_leaves_the_motor_stopped(
    subject: CohortSubject, session: SessionType
) -> None:
    outcome = _outcome(subject, session)
    exit_problems = [v for v in outcome.violations if v.startswith("[exit_")]
    assert not exit_problems, exit_problems
    if outcome.metrics is not None and session is not SessionType.MANUAL_BENCH:
        gate = outcome.eligibility
        assert gate is not None
        assert gate.profile is not None
        assert outcome.metrics.peak_motor_rpm <= gate.profile.max_rpm + 1


def test_an_invalid_cohort_document_is_refused() -> None:
    with pytest.raises(ValueError, match="kind"):
        battery.scenario_for({"name": "x", "kind": "sideways", "duration_s": 1})
