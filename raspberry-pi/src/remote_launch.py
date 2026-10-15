from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique


@unique
class LaunchStage(Enum):
    WAITING_CONFIRMATION = "waiting_confirmation"
    REQUESTING = "request_in_progress"
    REFUSED = "refused"
    UNCONFIRMED = "unconfirmed"


@unique
class ConfirmationCode(Enum):
    NOT_PENDING = "launch_not_pending"
    IN_PROGRESS = "launch_in_progress"
    PASSENGER_UNAVAILABLE = "passenger_unavailable"
    REFUSED = "launch_refused"


@dataclass(frozen=True, slots=True)
class ConfirmationRefusal:
    code: ConfirmationCode
    detail: str


@dataclass(frozen=True, slots=True)
class RemoteLaunchView:
    session_id: str
    profile_id: str
    program_name: str
    launched_by: str
    passenger_name: str | None
    passenger_reason: str | None
    stage: LaunchStage
    reason: str | None
