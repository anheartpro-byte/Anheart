"""Explicit success/failure values for the motor and safety paths.

Why this exists rather than exceptions: an unhandled exception in the drive
path leaves a motor commanded while the traceback unwinds. With a closed error
union plus ``typing.assert_never``, a caller that forgets to handle one failure
mode **fails the type check** instead of failing on the bench.

    match await drive.write_speed(rpm):
        case Ok():
            pass
        case Err(error):
            match error:                       # narrow to Err, THEN match
                case CommTimeout():
                    safety.trip(SafetyAction.GO_SILENT)
                case _ as unreachable:
                    assert_never(unreachable)  # a new variant fails the build

Use that nested shape. Matching variants directly inside ``Err(...)`` runs
correctly but does not narrow the type argument, so ``assert_never`` cannot see
exhaustiveness and the guarantee silently evaporates.
``tests/test_typing_contract.py`` runs a checker against a deliberately
incomplete match to prove this still bites.

There is deliberately no raising ``unwrap()``. Reintroducing an exception at
the point of use would defeat the whole arrangement; use ``unwrap_or`` or match.

See .claude/skills/anheart-strict-python/SKILL.md rule 3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeGuard

__all__ = ["Err", "Ok", "Result", "err_of", "is_err", "is_ok", "map_ok", "unwrap_or"]


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """A successful outcome carrying its value."""

    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    """A failed outcome carrying a value from a closed error union."""

    error: E


type Result[T, E] = Ok[T] | Err[E]
"""One or the other. ``Result[MotorRpm, DriveError]`` reads as exactly that."""


def is_ok[T, E](result: Result[T, E]) -> TypeGuard[Ok[T]]:
    """Narrow to ``Ok`` for call sites where a full match would be noise."""
    return isinstance(result, Ok)


def is_err[T, E](result: Result[T, E]) -> TypeGuard[Err[E]]:
    """Narrow to ``Err``."""
    return isinstance(result, Err)


def unwrap_or[T, E](result: Result[T, E], default: T) -> T:
    """Return the value, or ``default`` when the result is an error.

    The safe alternative to a raising ``unwrap``: the caller must supply a
    value that is correct when the operation failed, which forces the question
    "what is safe here?" to be answered in code rather than deferred.
    """
    if isinstance(result, Ok):
        return result.value
    return default


def map_ok[T, E, U](result: Result[T, E], fn: Callable[[T], U]) -> Result[U, E]:
    """Apply ``fn`` to a success value, passing any error through untouched."""
    if isinstance(result, Ok):
        return Ok(fn(result.value))
    return result


def err_of[T, E](result: Result[T, E]) -> E | None:
    """Return the error, or ``None`` when the result succeeded."""
    if isinstance(result, Err):
        return result.error
    return None
