"""Delegating capture for model calls or authoritative native Modbus exchanges."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import final

from src.clock import Clock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    ACC_LOGICAL,
    CMD_LOGICAL,
    DEC_LOGICAL,
    ETA_LOGICAL,
    HSP_LOGICAL,
    LCR_LOGICAL,
    LFRD_LOGICAL,
    LFT_LOGICAL,
    LSP_LOGICAL,
    RFRD_LOGICAL,
    TFR_LOGICAL,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.observation import ExchangeKind, ExchangeLog, ObservableDrive
from src.result import Err, Ok, Result
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
    READ = "read_status"


@dataclass(frozen=True, slots=True, kw_only=True)
class Frame:
    """One exchange with the drive, as sent."""

    at: Monotonic
    kind: FrameKind | ExchangeKind
    value: int | None
    """Requested write or returned raw register; unknown values stay ``None``."""

    label: str

    ok: bool
    """Whether the drive acknowledged it."""

    register: int | None = None
    latency_ms: float = 0.0
    observations: tuple[tuple[int, int | None], ...] = ()
    raw_hex: str | None = None


class InjectedTickError(RuntimeError):
    """Raised from ``read_status`` by the ``tick_exception`` scenario action."""


@final
class RecordingDrive:
    """Delegates to ``inner`` and records every frame. Mutable, owned by the harness loop."""

    __slots__ = (
        "_clock",
        "_exchanges",
        "_frames",
        "_inner",
        "_raise_next_read",
        "_reads",
        "_status_word",
    )

    def __init__(self, inner: DriveBackend, clock: Clock) -> None:
        self._inner: DriveBackend = inner
        self._clock: Clock = clock
        self._frames: list[Frame] = []
        self._reads: int = 0
        self._raise_next_read: bool = False
        self._status_word: int | None = None
        self._exchanges: ExchangeLog | None = None
        if isinstance(inner, ObservableDrive):
            self._exchanges = ExchangeLog(clock)
            inner.observe_exchanges(self._exchanges)

    @property
    def status_word(self) -> int | None:
        return self._status_word

    @property
    def frames(self) -> tuple[Frame, ...]:
        """Every frame so far, in order."""
        if self._exchanges is None:
            return tuple(self._frames)
        return tuple(
            Frame(
                at=item.at,
                kind=item.kind,
                register=item.register,
                value=item.value,
                ok=item.ok,
                latency_ms=item.latency_ms,
                label=item.detail,
                raw_hex=item.raw_hex,
            )
            for item in self._exchanges.entries
        )

    @property
    def reads(self) -> int:
        """Successful status reads so far."""
        return self._reads

    def raise_on_next_read(self) -> None:
        """Make the next ``read_status`` raise :class:`InjectedTickError`."""
        self._raise_next_read = True

    def _note(
        self,
        kind: FrameKind,
        value: int | None,
        label: str,
        *,
        ok: bool,
        started: Monotonic,
        register: int | None = None,
        observations: tuple[tuple[int, int | None], ...] = (),
    ) -> None:
        if self._exchanges is not None:
            return
        self._frames.append(
            Frame(
                at=started,
                kind=kind,
                value=value,
                label=label,
                ok=ok,
                register=register,
                latency_ms=(self._clock.monotonic() - started) * 1000,
                observations=observations,
            )
        )

    # -- DriveBackend -----------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self._inner.acquisition_evidence

    async def open(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.open()
        self._note(
            FrameKind.OPEN, None, _label(result), ok=not isinstance(result, Err), started=started
        )
        return result

    async def close(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.close()
        self._note(
            FrameKind.CLOSE, None, _label(result), ok=not isinstance(result, Err), started=started
        )
        return result

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.write_command(word)
        self._note(
            FrameKind.COMMAND,
            int(word),
            word.name,
            ok=not isinstance(result, Err),
            started=started,
            register=CMD_LOGICAL,
        )
        return result

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.write_speed(rpm)
        self._note(
            FrameKind.SPEED,
            int(rpm),
            _label(result),
            ok=not isinstance(result, Err),
            started=started,
            register=LFRD_LOGICAL,
        )
        return result

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self._raise_next_read:
            self._raise_next_read = False
            raise InjectedTickError("injected: read_status raised inside the tick")
        started = self._clock.monotonic()
        result = await self._inner.read_status()
        if isinstance(result, Err):
            self._status_word = None
            self._note(FrameKind.READ_FAILED, None, _label(result), ok=False, started=started)
        else:
            self._reads += 1
            status = result.value
            self._status_word = int(status.status_word)
            observations = (
                (ETA_LOGICAL, int(status.status_word)),
                (LFRD_LOGICAL, int(status.setpoint_echo_rpm)),
                (RFRD_LOGICAL, int(status.output_rpm)),
                (LCR_LOGICAL, round(status.current * 10)),
                (LFT_LOGICAL, status.fault_code),
            )
            self._note(
                FrameKind.READ, None, "status", ok=True, started=started, observations=observations
            )
        return result

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.read_limits()
        observations: tuple[tuple[int, int | None], ...] = ()
        if isinstance(result, Ok):
            limits = result.value
            observations = (
                (TFR_LOGICAL, round(limits.max_frequency * 10)),
                (HSP_LOGICAL, round(limits.high_speed * 10)),
                (LSP_LOGICAL, round(limits.low_speed * 10)),
                (ACC_LOGICAL, round(limits.acceleration * 10)),
                (DEC_LOGICAL, round(limits.deceleration * 10)),
            )
        self._note(
            FrameKind.READ_LIMITS,
            None,
            _label(result),
            ok=not isinstance(result, Err),
            started=started,
            observations=observations,
        )
        return result

    @property
    def emergency_budget(self) -> Seconds:
        return self._inner.emergency_budget

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        started = self._clock.monotonic()
        outcome = self._inner.emergency_disable_blocking(timeout)
        self._note(
            FrameKind.EMERGENCY_ZERO,
            0,
            outcome.name,
            ok=outcome is EmergencyStopOutcome.ACKNOWLEDGED,
            started=started,
            register=LFRD_LOGICAL,
        )
        return outcome


def _label[T](result: Result[T, DriveError]) -> str:
    """``ok``, or the class name of the drive error."""
    if isinstance(result, Err):
        return type(result.error).__name__
    return "ok"
