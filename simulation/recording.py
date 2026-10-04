"""A :class:`~src.motor.drive.DriveBackend` that records every frame on its way to the drive.

Layer 1 of the simulation is "what does the variateur receive?", so the answer
is taken at the seam itself: every command word, every speed setpoint, every
emergency zero, every open and close, with the instant it was sent and whether
the drive acknowledged it. Reads are counted (5 Hz of them would drown the
log) but a failed read is recorded, because it is an event.

The wrapped backend is the REAL :class:`~src.motor.simulated.SimulatedDrive`;
this class adds nothing to what it does except the log, plus one fault hook the
simulator does not offer (an exception raised from inside ``read_status``, the
"tick exception" exit path).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import final

from src.clock import Clock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    ControlWord,
    DriveBackend,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.result import Err, Result
from src.units import Monotonic, MotorRpm, Seconds


@unique
class FrameKind(Enum):
    """What went to the drive."""

    OPEN = "open"
    CLOSE = "close"
    COMMAND = "command"
    SPEED = "speed"
    EMERGENCY_ZERO = "emergency_zero"
    READ_LIMITS = "read_limits"
    READ_FAILED = "read_failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """One exchange with the drive, as sent."""

    at: Monotonic
    kind: FrameKind
    value: int | None
    """The control word (``COMMAND``) or motor-rpm setpoint (``SPEED``); else ``None``."""

    label: str
    """``ControlWord`` name, outcome name, or error class: for the log and the trace file."""

    ok: bool
    """Whether the drive acknowledged it."""


class InjectedTickError(RuntimeError):
    """Raised from ``read_status`` by the ``tick_exception`` scenario action."""


@final
class RecordingDrive:
    """Delegates to ``inner`` and records every frame. Mutable, owned by the harness loop."""

    __slots__ = ("_clock", "_frames", "_inner", "_raise_next_read", "_reads")

    def __init__(self, inner: DriveBackend, clock: Clock) -> None:
        self._inner: DriveBackend = inner
        self._clock: Clock = clock
        self._frames: list[Frame] = []
        self._reads: int = 0
        self._raise_next_read: bool = False

    @property
    def frames(self) -> tuple[Frame, ...]:
        """Every frame so far, in order."""
        return tuple(self._frames)

    @property
    def reads(self) -> int:
        """Successful status reads so far."""
        return self._reads

    def raise_on_next_read(self) -> None:
        """Make the next ``read_status`` raise :class:`InjectedTickError`."""
        self._raise_next_read = True

    def _note(self, kind: FrameKind, value: int | None, label: str, *, ok: bool) -> None:
        self._frames.append(
            Frame(at=self._clock.monotonic(), kind=kind, value=value, label=label, ok=ok)
        )

    # -- DriveBackend -----------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self._inner.acquisition_evidence

    async def open(self) -> Result[None, DriveError]:
        result = await self._inner.open()
        self._note(FrameKind.OPEN, None, _label(result), ok=not isinstance(result, Err))
        return result

    async def close(self) -> Result[None, DriveError]:
        result = await self._inner.close()
        self._note(FrameKind.CLOSE, None, _label(result), ok=not isinstance(result, Err))
        return result

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        result = await self._inner.write_command(word)
        self._note(FrameKind.COMMAND, int(word), word.name, ok=not isinstance(result, Err))
        return result

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        result = await self._inner.write_speed(rpm)
        self._note(FrameKind.SPEED, int(rpm), _label(result), ok=not isinstance(result, Err))
        return result

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self._raise_next_read:
            self._raise_next_read = False
            raise InjectedTickError("injected: read_status raised inside the tick")
        result = await self._inner.read_status()
        if isinstance(result, Err):
            self._note(FrameKind.READ_FAILED, None, _label(result), ok=False)
        else:
            self._reads += 1
        return result

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        result = await self._inner.read_limits()
        self._note(FrameKind.READ_LIMITS, None, _label(result), ok=not isinstance(result, Err))
        return result

    @property
    def emergency_budget(self) -> Seconds:
        return self._inner.emergency_budget

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        outcome = self._inner.emergency_disable_blocking(timeout)
        self._note(
            FrameKind.EMERGENCY_ZERO,
            0,
            outcome.name,
            ok=outcome is EmergencyStopOutcome.ACKNOWLEDGED,
        )
        return outcome


def _label(result: Result[object, DriveError]) -> str:
    """``ok``, or the class name of the drive error."""
    if isinstance(result, Err):
        return type(result.error).__name__
    return "ok"
