"""The dashboard validates a machine configuration as this console does (ANH-141).

``contracts/machine-config-cases.json`` is read here and by
``convex/machineConfigs.test.ts``. For every case, :func:`review` must give the
problems the file states, in its order and its words, and so must
``convex/lib/machineConfig.ts``: a configuration two admins approve on the site
is one this console accepts, refused with the same words otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, Literal, NotRequired, TypedDict, cast

import pytest

from src.machine_config import ValidationLevel, review
from src.result import Ok
from tests.medical_support import signed


class Problem(TypedDict):
    key: str
    detail: str


class Verdict(TypedDict):
    verdict: Literal["accepted", "refused"]
    problems: NotRequired[list[Problem]]


class Case(TypedDict):
    name: str
    changes: dict[str, object]
    remove: NotRequired[list[str]]
    level: NotRequired[str]
    expected: Verdict


class Cases(TypedDict):
    description: str
    base: dict[str, object]
    cases: list[Case]


CASES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "machine-config-cases.json"
)


def _cases() -> Cases:
    decoded: object = json.loads(CASES_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(decoded, dict)
    return cast(Cases, decoded)


CASES: Final[Cases] = _cases()


def _verdict(case: Case) -> Verdict:
    document = {**CASES["base"], **case["changes"]}
    for key in case.get("remove", []):
        del document[key]
    level = ValidationLevel(case.get("level", ValidationLevel.NONE.value))
    reviewed = review(document, {}, level, signed())
    if isinstance(reviewed, Ok):
        return {"verdict": "accepted"}
    return {
        "verdict": "refused",
        "problems": [{"key": problem.key, "detail": problem.detail} for problem in reviewed.error],
    }


@pytest.mark.parametrize("case", CASES["cases"], ids=[case["name"] for case in CASES["cases"]])
def test_review_gives_the_shared_verdict(case: Case) -> None:
    assert _verdict(case) == case["expected"]


def test_the_cases_refuse_and_accept() -> None:
    """A file of accepted cases only, or refused ones only, would hold half the rule."""
    verdicts = {case["expected"]["verdict"] for case in CASES["cases"]}
    assert verdicts == {"accepted", "refused"}
