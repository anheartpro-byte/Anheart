"""The catalogue validates a programme as this console does (ANH-137).

``contracts/programme-validation-cases.json`` is read here and by
``convex/programmes.test.ts``. For every case, :func:`parse_profile` must give
the verdict and the message the file states, and so must
``convex/lib/programmes.ts``: a programme the site accepts is one this console
accepts, refused with the same words otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, NotRequired, TypedDict, assert_never, cast

import pytest

from src.result import Err, Ok
from src.training.plan import Malformed, Rejected, parse_profile


class Verdict(TypedDict):
    verdict: Literal["accepted", "rejected", "malformed"]
    violations: NotRequired[list[str]]
    detail: NotRequired[str]


class Case(TypedDict):
    name: str
    changes: dict[str, object]
    expected: Verdict


class Cases(TypedDict):
    description: str
    base: dict[str, object]
    cases: list[Case]


CASES_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "programme-validation-cases.json"
)


def _cases() -> Cases:
    decoded: object = json.loads(CASES_PATH.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(decoded, dict)
    return cast(Cases, decoded)


CASES: Final[Cases] = _cases()


def _verdict(document: Mapping[str, object]) -> Verdict:
    parsed = parse_profile(document)
    match parsed:
        case Ok():
            return {"verdict": "accepted"}
        case Err(error):
            match error:
                case Rejected():
                    return {
                        "verdict": "rejected",
                        "violations": [violation.value for violation in error.violations],
                        "detail": error.detail,
                    }
                case Malformed():
                    return {"verdict": "malformed", "detail": error.detail}
            raise assert_never(error)
    raise assert_never(parsed)


@pytest.mark.parametrize("case", CASES["cases"], ids=[case["name"] for case in CASES["cases"]])
def test_parse_profile_gives_the_shared_verdict(case: Case) -> None:
    assert _verdict({**CASES["base"], **case["changes"]}) == case["expected"]
