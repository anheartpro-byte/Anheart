"""The STO-ready preflight control, shared by all arming paths.

This is the preflight module: ANH-142's automatic preflight controls and their
orchestration will be added here, next to the memory-only STO readiness check,
which is the only control implemented today.
"""

from dataclasses import dataclass
from typing import assert_never

from src.motor.sto import StoReading, StoState, sto_ready


@dataclass(frozen=True, slots=True, kw_only=True)
class StoNotReady:
    """A positive ready observation is required before arming or acknowledging."""

    state: StoState
    detail: str


def check_sto_ready(reading: StoReading) -> StoNotReady | None:
    """Pure STO-ready control; ANH-142 will supply the other preflight controls."""
    if sto_ready(reading):
        return None
    return StoNotReady(state=reading.state, detail=reading.detail)


def describe_sto_refusal(refusal: StoNotReady) -> str:
    """The same actionable words for the console and dashboard, without accents."""
    match refusal.state:
        case StoState.OPEN:
            return "chaine STO ouverte : retablir la chaine de securite"
        case StoState.UNKNOWN:
            return "etat STO inconnu : retablir une lecture fiable de la chaine de securite"
        case StoState.NOT_CONFIGURED:
            return "lecture STO non configuree : configurer la source de la chaine de securite"
        case StoState.READY:
            return "chaine STO prete"
    raise assert_never(refusal.state)
