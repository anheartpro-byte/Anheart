"""Non-blocking STO observations; Modbus polling belongs outside the control tick."""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol, assert_never, runtime_checkable

from src.clock import Clock
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    DriveError,
    DriveFaulted,
    EnableUnconfirmed,
    LogicInput,
    StopUnconfirmed,
    UnexpectedState,
    decode_logic_input,
)
from src.result import Err, Ok, Result
from src.units import Monotonic, OutOfRange, RawRegister, RegisterAddress, Seconds, elapsed

STO_POLL_PERIOD: Final[Seconds] = Seconds(0.5)
"""Two single-register reads/s: budget 40 ms/s at 19200 8E1, within the
shared-link budget documented in docs/raspberry-pi.md. Not a medical limit."""

STO_STALE_AFTER: Final[Seconds] = Seconds(2.0)
"""Allow four polling periods, like DRIVE_STATUS_STALE_AFTER; an older
observation cannot attest the relay's current state. Not a medical limit."""


@unique
class StoSourceKind(Enum):
    """The configured observation source (STO_SOURCE)."""

    NONE = "none"
    SIM_READY = "sim_ready"
    SIM_OPEN = "sim_open"
    DRIVE_INPUT = "drive_input"


@unique
class StoState(Enum):
    """What is known about the safety chain, independently of shaft motion."""

    READY = "ready"
    OPEN = "open"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True, kw_only=True)
class StoReading:
    """A memory-only observation, stamped at the last successful read."""

    state: StoState
    source: StoSourceKind
    last_success_at: Monotonic | None
    detail: str


def sto_ready(reading: StoReading) -> bool:
    """Only a positive ready observation attests readiness."""
    return reading.state is StoState.READY


class StoSource(Protocol):
    """A future GPIO adapter can provide the same memory-only seam."""

    @abstractmethod
    def latest(self, now: Monotonic) -> StoReading:
        """Return immediately, without I/O or waiting."""


class UnconfiguredStoSource:
    """No configured observation can attest readiness."""

    def latest(self, now: Monotonic) -> StoReading:
        del now
        return StoReading(
            state=StoState.NOT_CONFIGURED,
            source=StoSourceKind.NONE,
            last_success_at=None,
            detail="not configured",
        )


class SimulatedStoChain:
    """Mutable physical state shared by the simulated drive and its reader.

    Opening notifies connected plants synchronously at the supplied instant,
    even with no console or Modbus task running. Closing never enables a drive.
    """

    def __init__(self, *, ready: bool = True) -> None:
        self._ready: bool = ready
        self._opened: list[Callable[[Monotonic], None]] = []

    @property
    def ready(self) -> bool:
        """Whether the simulated safety-relay contact is closed."""
        return self._ready

    def connect(self, opened: Callable[[Monotonic], None]) -> None:
        """Connect a plant's torque cut; no asynchronous work is scheduled."""
        self._opened.append(opened)

    def open(self, now: Monotonic) -> None:
        """Cut connected plants at this instant, then expose the open contact."""
        for opened in self._opened:
            opened(now)
        self._ready = False

    def close(self, now: Monotonic) -> None:
        """Restore the contact only; plants still require a complete enable sequence."""
        del now
        self._ready = True


@dataclass(frozen=True, slots=True, kw_only=True)
class SimulatedStoSource:
    """Read the shared contact immediately, without a polling cache."""

    chain: SimulatedStoChain
    source: StoSourceKind

    def latest(self, now: Monotonic) -> StoReading:
        return StoReading(
            state=StoState.READY if self.chain.ready else StoState.OPEN,
            source=self.source,
            last_success_at=now,
            detail="simulated relay contact",
        )


@runtime_checkable
class StoRegisterPort(Protocol):
    """The existing ATV320 transport owns locking, execution and link health."""

    @abstractmethod
    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        """Read one resolved register through the drive's normal transport."""


class DriveInputStoSource:
    """Mutable polling cache; latest() never touches the Modbus link.

    A failed attempt invalidates the value immediately, retaining only the
    instant of the last success for diagnostics, never the device's free text.
    """

    def __init__(
        self, *, clock: Clock, drive: StoRegisterPort, address: RegisterAddress, input_: LogicInput
    ) -> None:
        self._clock: Clock = clock
        self._drive: StoRegisterPort = drive
        self._address: RegisterAddress = address
        self._input: LogicInput = input_
        self._poll_permitted: Callable[[], bool] | None = None
        self._reading: StoReading = StoReading(
            state=StoState.UNKNOWN,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=None,
            detail="never read",
        )

    def guard_polling(self, permitted: Callable[[], bool]) -> None:
        """Bind the console's silence/release guard at composition, before polling starts."""
        self._poll_permitted = permitted

    async def poll(self) -> None:
        """One asynchronous exchange, called only by the console's polling task."""
        permitted = self._poll_permitted
        if permitted is not None and not permitted():
            return
        result = await self._drive.read_register(self._address)
        match result:
            case Ok(raw):
                self._reading = StoReading(
                    state=StoState.READY if decode_logic_input(raw, self._input) else StoState.OPEN,
                    source=StoSourceKind.DRIVE_INPUT,
                    last_success_at=self._clock.monotonic(),
                    detail="logic input relay contact",
                )
            case Err(error):
                self._reading = StoReading(
                    state=StoState.UNKNOWN,
                    source=StoSourceKind.DRIVE_INPUT,
                    last_success_at=self._reading.last_success_at,
                    detail=_error_variant(error),
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def run(self) -> None:
        """Poll at the technical period until cancelled by the console owner.

        The guard is checked immediately before every read. This single task
        awaits each exchange: at most one read already in flight can complete
        after silence/release begins, then no further frame refreshes ttO.
        The cached success simply ages to UNKNOWN while polling is forbidden.
        """
        while True:
            began = self._clock.monotonic()
            await self.poll()
            remaining = max(0.0, STO_POLL_PERIOD - elapsed(began, self._clock.monotonic()))
            await asyncio.sleep(remaining)

    def latest(self, now: Monotonic) -> StoReading:
        last = self._reading.last_success_at
        if last is None or self._reading.state is StoState.UNKNOWN:
            return self._reading
        if elapsed(last, now) > STO_STALE_AFTER:
            return StoReading(
                state=StoState.UNKNOWN,
                source=StoSourceKind.DRIVE_INPUT,
                last_success_at=last,
                detail="stale reading",
            )
        return self._reading


def _error_variant(error: DriveError) -> str:
    """Closed variant names only; device-provided diagnostic text stays private."""
    match error:
        case (
            CommTimeout()
            | BadResponse()
            | UnexpectedState()
            | DriveFaulted()
            | OutOfRange()
            | EnableUnconfirmed()
            | StopUnconfirmed()
        ):
            return type(error).__name__
    raise assert_never(error)
