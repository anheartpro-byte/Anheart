"""What the drive was asked and what it answered, kept in memory for the session record.

``drive_frames.jsonl`` is fed from here. Nothing in this module opens a file:
observations wait in a bounded list until the journal thread
(:mod:`src.record.journal`) takes them, every drain period, and writes them.

Two ways in, chosen by :func:`tap_drive` from what the backend is:

* a backend that reports its own Modbus exchanges
  (:class:`~src.motor.observation.ObservableDrive`, the ATV320 driver) is given
  a bounded :class:`~src.motor.observation.ExchangeLog`. Its transactions are
  the observations, and nothing is put between the runtime and the driver.
  By default these are the register transactions (``modbus_read``,
  ``modbus_write``): what was asked of which register, what came back, how
  long it took. The SDK's own transport calls (``modbus_send``,
  ``modbus_receive_chunk``, with their bytes) are kept only when asked for
  (``sdk_frames``, the console's ``RECORD_DRIVE_SDK_FRAMES``): a bench
  diagnostic of the link, at about four times the lines;
* any other backend (the simulated drive) is wrapped by :class:`CallTap`, which
  delegates every call and notes it afterwards: the same eight call
  observations, with the same registers and values, as the simulation harness
  writes. Never both: a call observation is not made up next to native frames.

What both guarantee:

* **bounded**: at most ``capacity`` observations wait. Past that the newest is
  refused and counted (:attr:`Taken.lost`), and the record says so.
* **never in the way of the drive**: the answer of the drive is returned
  whatever happens to its observation. Noting one is an append at one end of
  a ``deque``; the journal thread takes from the other end, one observation
  at a time, and **takes no lock to do it**: stopped anywhere, even in the
  middle of taking, it holds nothing a drive call needs. (A stepping test
  holds that thread before each of its instructions and makes a drive call:
  ``tests/test_record_tick_isolation.py``.) The only lock is between those
  who note, so that the bound and the count stay exact if two of them ever
  note at once; the journal thread never touches it.
* **total**: an observation that cannot be built is one observation lost,
  counted like a refused one. It never reaches the caller.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Final, final

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
from src.motor.observation import ExchangeLog, ObservableDrive
from src.record.schema import DriveFrame
from src.result import Err, Ok, Result
from src.units import Monotonic, MotorRpm, Seconds

FRAME_BACKLOG: Final[int] = 4096
"""Observations that may wait for the journal thread, which comes every 0.2 s.

Measured: the simulated console makes about 12 a second, and the native driver
about 30 (6 register transactions per tick over a simulated FTDI link), or
about 120 with the SDK's transport calls kept (24 per tick: each transaction,
its request and the chunks of its answer). This is minutes of the first two
and half a minute of the last, a hundred times what a healthy journal ever
leaves, and a fixed ceiling (a megabyte or two) when the disk has stopped
answering and nobody comes."""

type Registers = int | tuple[int, ...] | None
type Values = int | tuple[int | None, ...] | None


class CallKind(StrEnum):
    """One call of the drive seam. The values are the ``kind`` of a drive frame."""

    OPEN = "open"
    CLOSE = "close"
    COMMAND = "command"
    SPEED = "speed"
    EMERGENCY_ZERO = "emergency_zero"
    READ_LIMITS = "read_limits"
    READ_FAILED = "read_failed"
    READ = "read_status"


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    """One drive observation on the console's clock, before any session's time axis."""

    at: Monotonic
    """When the call or the exchange began."""

    kind: str
    register: Registers
    value: Values
    ok: bool
    latency_ms: float
    raw_hex: str | None = None

    def frame(self, origin: Monotonic) -> DriveFrame:
        """The line of ``drive_frames.jsonl``, timed from ``origin`` to the millisecond."""
        return DriveFrame(
            t=round(self.at - origin, 3),
            kind=self.kind,
            register=self.register,
            value=self.value,
            ok=self.ok,
            latency_ms=self.latency_ms,
            raw_hex=self.raw_hex,
        )


@dataclass(frozen=True, slots=True)
class Taken:
    """What the journal thread took, and what could not be kept for it so far."""

    observations: tuple[Observation, ...]
    lost: int
    """Observations refused (the list was full) or that could not be built, since startup."""


type FrameSource = Callable[[], Taken]
"""Take every waiting observation, oldest first. Called by the journal thread only."""


def status_observation(status: DriveStatus) -> tuple[Registers, Values]:
    """One status read as ONE grouped observation: aligned registers and raw values."""
    return (
        (ETA_LOGICAL, LFRD_LOGICAL, RFRD_LOGICAL, LCR_LOGICAL, LFT_LOGICAL),
        (
            int(status.status_word),
            int(status.setpoint_echo_rpm),
            int(status.output_rpm),
            round(status.current * 10),
            status.fault_code,
        ),
    )


def limits_observation(limits: DriveLimits) -> tuple[Registers, Values]:
    """The drive's parameters as read, back in the registers' own scale (0.1 Hz, 0.1 s)."""
    return (
        (TFR_LOGICAL, HSP_LOGICAL, LSP_LOGICAL, ACC_LOGICAL, DEC_LOGICAL),
        (
            round(limits.max_frequency * 10),
            round(limits.high_speed * 10),
            round(limits.low_speed * 10),
            round(limits.acceleration * 10),
            round(limits.deceleration * 10),
        ),
    )


def _nothing() -> tuple[Registers, Values]:
    return (None, None)


@final
class CallTap:
    """A ``DriveBackend`` that delegates to ``inner`` and notes every call. See the module.

    Mutable. ``_waiting`` is appended to by whoever calls the drive (the event
    loop; the emergency path, which may be another thread) and emptied by the
    journal thread from its other end, with no lock between the two sides.
    ``_noting`` is held by those who note only, for one append: the journal
    thread reads ``_lost`` (an ``int``) and never takes it.
    """

    __slots__ = ("_capacity", "_clock", "_inner", "_lost", "_noting", "_waiting")

    def __init__(self, inner: DriveBackend, clock: Clock, capacity: int = FRAME_BACKLOG) -> None:
        self._inner: DriveBackend = inner
        self._clock: Clock = clock
        self._capacity: int = capacity
        self._waiting: deque[Observation] = deque()
        self._lost: int = 0
        self._noting: Lock = Lock()

    def take(self) -> Taken:
        """The journal thread's side: what waits now, oldest first. It takes no lock.

        As many as were there when it began, one ``popleft`` each: what is
        noted meanwhile waits for the next cycle. Only this side removes, so
        every one of them is there to take.
        """
        waiting = [self._waiting.popleft() for _ in range(len(self._waiting))]
        return Taken(tuple(waiting), self._lost)

    def _note(
        self,
        kind: CallKind,
        started: Monotonic,
        *,
        ok: bool,
        said: Callable[[], tuple[Registers, Values]] = _nothing,
    ) -> None:
        """Remember one call. Total: the drive's answer is already on its way back."""
        try:
            register, value = said()
            observation = Observation(
                at=started,
                kind=kind.value,
                register=register,
                value=value,
                ok=ok,
                latency_ms=(self._clock.monotonic() - started) * 1000,
            )
        except Exception:  # an observer: what it cannot say is lost, never raised
            with self._noting:
                self._lost += 1
            return
        with self._noting:
            if len(self._waiting) >= self._capacity:
                self._lost += 1
                return
            self._waiting.append(observation)

    # -- DriveBackend -----------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self._inner.acquisition_evidence

    async def open(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.open()
        self._note(CallKind.OPEN, started, ok=isinstance(result, Ok))
        return result

    async def close(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.close()
        self._note(CallKind.CLOSE, started, ok=isinstance(result, Ok))
        return result

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.write_command(word)
        self._note(
            CallKind.COMMAND,
            started,
            ok=isinstance(result, Ok),
            said=lambda: (CMD_LOGICAL, int(word)),
        )
        return result

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.write_speed(rpm)
        self._note(
            CallKind.SPEED,
            started,
            ok=isinstance(result, Ok),
            said=lambda: (LFRD_LOGICAL, int(rpm)),
        )
        return result

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.read_status()
        if isinstance(result, Err):
            self._note(CallKind.READ_FAILED, started, ok=False)
        else:
            status = result.value
            self._note(CallKind.READ, started, ok=True, said=lambda: status_observation(status))
        return result

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        started = self._clock.monotonic()
        result = await self._inner.read_limits()
        if isinstance(result, Err):
            self._note(CallKind.READ_LIMITS, started, ok=False)
        else:
            limits = result.value
            self._note(
                CallKind.READ_LIMITS, started, ok=True, said=lambda: limits_observation(limits)
            )
        return result

    @property
    def emergency_budget(self) -> Seconds:
        return self._inner.emergency_budget

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        started = self._clock.monotonic()
        outcome = self._inner.emergency_disable_blocking(timeout)
        self._note(
            CallKind.EMERGENCY_ZERO,
            started,
            ok=outcome is EmergencyStopOutcome.ACKNOWLEDGED,
            said=lambda: (LFRD_LOGICAL, 0),
        )
        return outcome


def exchange_source(log: ExchangeLog) -> FrameSource:
    """The native driver's exchanges, as the journal thread takes them."""

    def take() -> Taken:
        drained = log.drain()
        return Taken(
            tuple(
                Observation(
                    at=exchange.at,
                    kind=exchange.kind.value,
                    register=exchange.register,
                    value=exchange.value,
                    ok=exchange.ok,
                    latency_ms=exchange.latency_ms,
                    raw_hex=exchange.raw_hex,
                )
                for exchange in drained.entries
            ),
            drained.refused,
        )

    return take


@dataclass(frozen=True, slots=True)
class Tapped:
    """A drive that is listened to: what the runtime must be given, and where to take from."""

    backend: DriveBackend
    source: FrameSource


def tap_drive(
    backend: DriveBackend,
    clock: Clock,
    capacity: int = FRAME_BACKLOG,
    *,
    sdk_frames: bool = False,
) -> Tapped:
    """Listen to ``backend`` for the session record. Opens nothing, and starts nothing.

    ``sdk_frames``: for a backend that reports its own exchanges, keep the
    SDK's transport calls too, not only the register transactions.
    """
    if isinstance(backend, ObservableDrive):
        log = ExchangeLog(clock, capacity, transport=sdk_frames)
        backend.observe_exchanges(log)
        return Tapped(backend, exchange_source(log))
    tap = CallTap(backend, clock, capacity)
    return Tapped(tap, tap.take)
