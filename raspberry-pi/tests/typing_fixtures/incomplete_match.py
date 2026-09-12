"""A deliberately WRONG module, used as evidence by tests/test_typing_contract.py.

This file is never imported and never runs. It exists so the test suite can
assert that the type checker still catches an unhandled error variant, which is
the single property the whole Result design rests on. If a future tool upgrade,
config change, or refactor quietly breaks that narrowing, a plain green build
would otherwise hide it.

``handle_incomplete`` omits ``BadCrc``. mypy must report it by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from src.result import Err, Ok, Result


@dataclass(frozen=True, slots=True)
class CommTimeout:
    after_s: float


@dataclass(frozen=True, slots=True)
class BadCrc:
    expected: int


FakeDriveError = CommTimeout | BadCrc


def handle_incomplete(result: Result[int, FakeDriveError]) -> str:
    """Handles CommTimeout but forgets BadCrc. This MUST fail the type check."""
    match result:
        case Ok(value):
            return f"ok:{value}"
        case Err(error):
            match error:
                case CommTimeout():
                    return "timeout"
                case _ as unreachable:
                    assert_never(unreachable)
