"""What the commissioning card shows and asks (ANH-115), apart from how it is done.

The web layer reads this; :mod:`src.commissioning` does it. Kept apart as
:mod:`src.pairing_view` is: the console's configuration (:mod:`src.local_config`)
is read by the web layer's setup, and must not depend on the module that talks to the
dashboard.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum, unique
from typing import Protocol

from src.result import Result
from src.training.preflight import ControlResult
from src.units import UnixMillis


@unique
class Verdict(Enum):
    """What the dashboard recorded. Wire strings (``verdicts`` of ``machine_commissioning``)."""

    CONFORMING = "conforming"
    NONCONFORMING = "nonconforming"


@dataclass(frozen=True, slots=True)
class Finding:
    """One difference the dashboard found with the registry, in its own words."""

    id: str
    expected: str
    read: str


@dataclass(frozen=True, slots=True)
class Recorded:
    """The dashboard recorded the commissioning, compared with the machine's registry."""

    record_id: str
    verdict: Verdict
    findings: tuple[Finding, ...]
    signed: bool


@dataclass(frozen=True, slots=True)
class NotRecorded:
    """The commissioning was judged, and the dashboard did not record it: why."""

    reason: str


type Outcome = Recorded | NotRecorded


@dataclass(frozen=True, slots=True)
class LastRun:
    """The last commissioning of this run of the console: who, when, what it judged, and what
    the dashboard made of it."""

    at: UnixMillis
    operator: str
    controls: tuple[ControlResult, ...]
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class CommissioningView:
    """What the card shows: a commissioning under way, why none can start now, the last one."""

    running: bool
    refusal: str | None
    """Why a commissioning cannot start now, in the card's words; ``None``: one can."""

    last: LastRun | None


class CommissioningScreen(Protocol):
    """The commissioning card's side of the desk (:class:`src.commissioning.CommissioningDesk`)."""

    @abstractmethod
    def view(self) -> CommissioningView:
        """What the card shows now."""

    @abstractmethod
    async def run(self, operator: str) -> Result[LastRun, str]:
        """Run a commissioning for ``operator``, signed in, and send it; or why not, in the
        card's words."""
