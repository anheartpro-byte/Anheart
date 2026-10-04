from __future__ import annotations

from dataclasses import dataclass

from src.clock import Clock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import DriveBackend, DriveError, DriveStatus
from src.result import Err
from src.units import Seconds, elapsed


@dataclass(frozen=True, slots=True)
class OpenFailed:
    error: DriveError
    before: AcquisitionEvidence
    after: AcquisitionEvidence


@dataclass(frozen=True, slots=True)
class StatusFailed:
    error: DriveError
    before: AcquisitionEvidence
    after: AcquisitionEvidence


@dataclass(frozen=True, slots=True)
class StatusRead:
    status: DriveStatus
    latency: Seconds | None
    before: AcquisitionEvidence
    after: AcquisitionEvidence


@dataclass(frozen=True, slots=True)
class InspectionReady:
    """Opening has completed; callers adopt ownership before awaiting status."""

    drive: DriveBackend
    before: AcquisitionEvidence

    async def read_status(self, clock: Clock | None = None) -> StatusFailed | StatusRead:
        began = None if clock is None else clock.monotonic()
        result = await self.drive.read_status()
        after = self.drive.acquisition_evidence
        if isinstance(result, Err):
            return StatusFailed(result.error, self.before, after)
        latency = None
        if clock is not None and began is not None:
            latency = elapsed(began, clock.monotonic())
        return StatusRead(result.value, latency, self.before, after)


async def begin_inspection(drive: DriveBackend, *, reopen: bool) -> OpenFailed | InspectionReady:
    before = drive.acquisition_evidence
    if reopen:
        result = await drive.open()
        if isinstance(result, Err):
            return OpenFailed(result.error, before, drive.acquisition_evidence)
    return InspectionReady(drive, before)
