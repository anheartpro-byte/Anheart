from __future__ import annotations

import json
from pathlib import Path
from typing import Final, TypedDict, cast

import pytest

from src.local_panel import rider_age_refusal
from src.medical import SHIPPED_PARAMETERS
from src.result import Err, Ok
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    Rejected,
    StoreContent,
    Violation,
    hr_max_from_age,
    parse_profile,
    parse_store,
    profile_to_document,
)


class ZoneCase(TypedDict):
    hr_max: int
    zone_high_bpm: int
    hard_max_bpm: int
    expected: list[str]


class AgeEstimate(TypedDict):
    age: int
    hr_max: int


class AgeMinimum(TypedDict):
    age: int
    refused: bool


class ParityFixture(TypedDict):
    description: str
    zone_cases: list[ZoneCase]
    age_estimates: list[AgeEstimate]
    age_minimum: list[AgeMinimum]


FIXTURE_PATH: Final[Path] = Path(__file__).parent / "fixtures" / "medical_zone_parity.json"


def _fixture() -> ParityFixture:
    decoded: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(decoded, dict)
    return cast(ParityFixture, decoded)


FIXTURE: Final[ParityFixture] = _fixture()


def _valid_profile_document() -> dict[str, object]:
    parsed = parse_store(SHIPPED_DEFAULTS_PATH.read_text(encoding="utf-8"))
    if isinstance(parsed, Err):
        pytest.fail(f"shipped example profile did not parse: {parsed.error!r}")
    store: StoreContent = parsed.value
    source = profile_to_document(store.profiles[0])
    document: dict[str, object] = dict(source)
    return document


def _zone_verdict(case: ZoneCase) -> tuple[str, ...]:
    document = _valid_profile_document()
    document.update(
        {
            "subject_hr_max": case["hr_max"],
            "zone_low_bpm": case["zone_high_bpm"] - 1,
            "zone_high_bpm": case["zone_high_bpm"],
            "hard_max_bpm": case["hard_max_bpm"],
            "critical_bpm": case["hard_max_bpm"] + 1,
        }
    )
    parsed = parse_profile(document)
    if isinstance(parsed, Ok):
        return ()
    if not isinstance(parsed.error, Rejected):
        pytest.fail(f"parity profile was malformed: {parsed.error!r}")

    codes = {
        Violation.ZONE_ABOVE_SUBJECT_CEILING: "zone_above_ceiling",
        Violation.HARD_MAX_ABOVE_SUBJECT_MAX: "hard_max_above_hr_max",
    }
    assert all(violation in codes for violation in parsed.error.violations)
    return tuple(codes[violation] for violation in parsed.error.violations if violation in codes)


def test_zone_refusals_match_the_shared_cases() -> None:
    for case in FIXTURE["zone_cases"]:
        assert _zone_verdict(case) == tuple(case["expected"]), case


def test_age_estimates_match_the_shared_cases() -> None:
    for case in FIXTURE["age_estimates"]:
        estimate = hr_max_from_age(case["age"], SHIPPED_PARAMETERS.hr_max_formula)
        if isinstance(estimate, Err):
            pytest.fail(f"fixture age was outside the estimate range: {case!r}")
        assert int(estimate.value) == case["hr_max"], case


def test_age_minimum_refusals_match_the_shared_cases() -> None:
    for case in FIXTURE["age_minimum"]:
        refused = rider_age_refusal(case["age"], SHIPPED_PARAMETERS.min_rider_age) is not None
        assert refused is case["refused"], case
