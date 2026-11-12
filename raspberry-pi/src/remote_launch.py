"""A launch from the dashboard as the console offers it: its stage, and what a confirmation needs.

Two modes since contract 1.9 (ANH-250): an AUTO launch, a programme for a
rider, and a BENCH launch, a ballasted bench session with nobody on board. A
bench launch is confirmed only with the operator's attestation
(:data:`BENCH_LAUNCH_ATTESTATION`), ticked at the console.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import ClassVar, Final

from src.units import Kilograms

BENCH_LAUNCH_ATTESTATION: Final[str] = (
    "capsule lestee, aucune personne a bord, personne dans le rayon du bras"
)
"""What the operator attests, at the console, before a bench launch from the dashboard
is sent to start (``bench_launch`` of ``contracts/machine-api.json``)."""


@unique
class LaunchMode(Enum):
    """What a launch from the dashboard starts. Wire strings (``mode`` of the poll)."""

    AUTO = "auto"
    BENCH = "bench"


@unique
class LaunchStage(Enum):
    WAITING_CONFIRMATION = "waiting_confirmation"
    REQUESTING = "request_in_progress"
    REFUSED = "refused"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True, slots=True)
class OfferWaiting:
    stage: ClassVar[LaunchStage] = LaunchStage.WAITING_CONFIRMATION
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OfferConfirmationQueued:
    stage: ClassVar[LaunchStage] = LaunchStage.REQUESTING
    operator: str
    generation: int
    reason: None = None


@dataclass(frozen=True, slots=True)
class OfferRequestInFlight:
    stage: ClassVar[LaunchStage] = LaunchStage.REQUESTING
    generation: int
    reason: None = None


@dataclass(frozen=True, slots=True)
class OfferRefused:
    stage: ClassVar[LaunchStage] = LaunchStage.REFUSED
    reason: str


@dataclass(frozen=True, slots=True)
class OfferUnconfirmed:
    stage: ClassVar[LaunchStage] = LaunchStage.UNCONFIRMED
    reason: str


type LaunchOfferState = (
    OfferWaiting | OfferConfirmationQueued | OfferRequestInFlight | OfferRefused | OfferUnconfirmed
)


@unique
class ConfirmationCode(Enum):
    NOT_PENDING = "launch_not_pending"
    IN_PROGRESS = "launch_in_progress"
    PASSENGER_UNAVAILABLE = "passenger_unavailable"
    REFUSED = "launch_refused"
    BENCH_UNATTESTED = "bench_unattested"


@dataclass(frozen=True, slots=True)
class ConfirmationRefusal:
    code: ConfirmationCode
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RemoteLaunchView:
    """The offered launch, for the console's card. Memory only: never written anywhere."""

    session_id: str
    mode: LaunchMode
    profile_id: str | None
    """``None`` for a bench launch: it runs no programme."""

    program_name: str | None
    launched_by: str
    passenger_name: str | None
    """``None`` for a bench launch, and for a rider the list does not hold now."""

    passenger_reason: str | None
    stage: LaunchStage
    reason: str | None
    ballast_kg: Kilograms | None = None
    """The mass a bench launch declares, in kg; ``None`` when none was given."""

    notes: str | None = None
    """The launcher's note on a bench launch, shown as text."""

    attestation: str | None = None
    """What a bench launch's confirmation attests (:data:`BENCH_LAUNCH_ATTESTATION`)."""
