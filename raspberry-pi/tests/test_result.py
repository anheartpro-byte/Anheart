"""Tests for the Result type.

The point of these is not that ``Ok(5).value == 5``. It is that the pattern
the motor path relies on actually holds: structural matching narrows, a closed
error union is exhaustive, and the values are immutable so a decision cannot be
rewritten after it was made.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from typing import assert_never

import pytest

from src.result import Err, Ok, Result, err_of, is_err, is_ok, map_ok, unwrap_or


@dataclass(frozen=True, slots=True)
class CommTimeout:
    after_s: float


@dataclass(frozen=True, slots=True)
class BadCrc:
    expected: int


#: A closed union, exactly as the drive layer declares its errors.
DriveError = CommTimeout | BadCrc


def classify(result: Result[int, DriveError]) -> str:
    """Every branch handled, with assert_never proving it to the type checker.

    Note the shape: narrow to ``Err(error)`` first, then match the error on its
    own. Matching variants directly inside ``Err(...)`` runs correctly but does
    not narrow the type argument, so ``assert_never`` would not see
    exhaustiveness and the guarantee would silently evaporate. See
    tests/test_typing_contract.py, which proves the check still bites.
    """
    match result:
        case Ok(value):
            return f"ok:{value}"
        case Err(error):
            match error:
                case CommTimeout(after_s=after):
                    return f"timeout:{after}"
                case BadCrc(expected=expected):
                    return f"crc:{expected}"
            raise assert_never(error)
    raise assert_never(result)


def test_match_narrows_every_variant() -> None:
    assert classify(Ok(5)) == "ok:5"
    assert classify(Err(CommTimeout(0.3))) == "timeout:0.3"
    assert classify(Err(BadCrc(7))) == "crc:7"


def test_ok_is_immutable() -> None:
    """A recorded outcome must not be editable after the fact."""
    result = Ok(1)
    with pytest.raises(FrozenInstanceError):
        result.value = 2  # type: ignore[misc]  # asserting the frozen contract


def test_err_is_immutable() -> None:
    result = Err(BadCrc(1))
    with pytest.raises(FrozenInstanceError):
        result.error = BadCrc(2)  # type: ignore[misc]  # asserting the frozen contract


def test_slots_are_declared() -> None:
    """slots=True keeps these cheap; they are allocated on every drive read.

    Asserted via the absence of ``__dict__`` rather than by trying to set an
    unknown attribute: on a frozen+slots dataclass that path raises TypeError
    from a broken zero-arg ``super()`` cell, which tests an interpreter quirk
    rather than our contract.
    """
    assert Ok(1).__slots__ == ("value",)
    assert Err(1).__slots__ == ("error",)
    assert not hasattr(Ok(1), "__dict__")
    assert not hasattr(Err(1), "__dict__")


def test_is_ok_and_is_err_agree() -> None:
    ok: Result[int, DriveError] = Ok(1)
    err: Result[int, DriveError] = Err(BadCrc(1))
    assert is_ok(ok) is True
    assert is_err(ok) is False
    assert is_ok(err) is False
    assert is_err(err) is True


def test_unwrap_or_returns_value_on_success() -> None:
    assert unwrap_or(Ok(9), -1) == 9


def test_unwrap_or_returns_default_on_error() -> None:
    """The default is the caller stating what is safe when the read failed."""
    assert unwrap_or(Err(CommTimeout(1.0)), -1) == -1


def _double(value: int) -> int:
    return value * 2


def test_map_ok_transforms_success() -> None:
    assert map_ok(Ok(3), _double) == Ok(6)


def test_map_ok_passes_errors_through_untouched() -> None:
    original: Result[int, DriveError] = Err(BadCrc(4))
    assert map_ok(original, _double) is original


def test_err_of_extracts_or_none() -> None:
    assert err_of(Ok(1)) is None
    assert err_of(Err(BadCrc(2))) == BadCrc(2)
