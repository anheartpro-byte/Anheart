"""The STO-ready preflight control, shared by all arming paths.

This is the preflight module: ANH-142's automatic preflight controls and their
orchestration will be added here, next to the memory-only STO readiness check,
which is the only control implemented today.
"""

from dataclasses import dataclass
from typing import Final, assert_never

from src.motor.sto import StoReading, StoState, sto_bridged, sto_ready
from src.training.types import Occupancy


@dataclass(frozen=True, slots=True, kw_only=True)
class StoNotReady:
    """A positive ready observation is required before arming or acknowledging."""

    state: StoState
    detail: str


@dataclass(frozen=True, slots=True)
class StoBridgedOnBoard:
    """Under a declared STO bridge only a bench session, with nobody on board, arms.

    A programme (a person on board by definition), a manual session with a
    person on board, and a check that does not say who is on board are
    refused, whatever the medical parameters and the enabled modes.
    """


BRIDGED_BENCH_ONLY: Final[str] = (
    "chaine STO pontee (declaree), aucun arret d'urgence materiel : seule une seance "
    "manuelle de banc, capsule vide ou lestee, aucune personne a bord, peut demarrer"
)
"""The words of :class:`StoBridgedOnBoard` and of a BRIDGED reading, for the console
and the dashboard."""


@dataclass(frozen=True, slots=True)
class StoBridgedRemoteLaunch:
    """Under a declared STO bridge no launch from the dashboard arms.

    Its own refusal, apart from :class:`StoBridgedOnBoard`: a dashboard launch
    is a programme today, and bench sessions start at the console only.
    """


BRIDGED_REMOTE_LAUNCH: Final[str] = (
    "STO ponte : lancement depuis le site refuse, seances de banc a la console seulement"
)
"""The words of :class:`StoBridgedRemoteLaunch`, for the console and the dashboard."""


def check_sto_ready(reading: StoReading) -> StoNotReady | None:
    """Pure STO-ready control; ANH-142 will supply the other preflight controls."""
    if sto_ready(reading):
        return None
    return StoNotReady(state=reading.state, detail=reading.detail)


def check_sto_for_start(
    reading: StoReading, occupancy: Occupancy | None
) -> StoNotReady | StoBridgedOnBoard | None:
    """The STO control of an arming: READY, or a declared bridge with nobody on board.

    ``occupancy`` is who the start puts on board: ``OCCUPIED`` for a
    programme, ``None`` for a check that does not know it. Any other reading
    gets :func:`check_sto_ready`'s answer, whatever the occupancy.
    """
    if not sto_bridged(reading):
        return check_sto_ready(reading)
    return None if occupancy is Occupancy.BENCH else StoBridgedOnBoard()


def describe_sto_refusal(refusal: StoNotReady) -> str:
    """The same actionable words for the console and dashboard, without accents."""
    match refusal.state:
        case StoState.OPEN:
            return "chaine STO ouverte : retablir la chaine de securite"
        case StoState.UNKNOWN:
            return "etat STO inconnu : retablir une lecture fiable de la chaine de securite"
        case StoState.NOT_CONFIGURED:
            return "lecture STO non configuree : configurer la source de la chaine de securite"
        case StoState.BRIDGED:
            return BRIDGED_BENCH_ONLY
        case StoState.READY:
            return "chaine STO prete"
    raise assert_never(refusal.state)
