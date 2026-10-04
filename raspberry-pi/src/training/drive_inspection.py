from __future__ import annotations

from dataclasses import dataclass

from src.clock import Clock
from src.motor.drive import DriveBackend, DriveError, DriveStatus
from src.result import Err
from src.units import Seconds, elapsed


@dataclass(frozen=True, slots=True)
class OpenFailed:
    error: DriveError


@dataclass(frozen=True, slots=True)
class StatusFailed:
    error: DriveError


@dataclass(frozen=True, slots=True)
class StatusRead:
    status: DriveStatus
    latency: Seconds | None


@dataclass(frozen=True, slots=True)
class InspectionReady:
    """Opening has completed; callers adopt ownership before awaiting status."""

    drive: DriveBackend

    async def read_status(self, clock: Clock | None = None) -> StatusFailed | StatusRead:
        began = None if clock is None else clock.monotonic()
        result = await self.drive.read_status()
        if isinstance(result, Err):
            return StatusFailed(result.error)
        latency = None
        if clock is not None and began is not None:
            latency = elapsed(began, clock.monotonic())
        return StatusRead(result.value, latency)


async def begin_inspection(drive: DriveBackend, *, reopen: bool) -> OpenFailed | InspectionReady:
    if reopen:
        result = await drive.open()
        if isinstance(result, Err):
            return OpenFailed(result.error)
    return InspectionReady(drive)
