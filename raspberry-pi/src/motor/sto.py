"""Non-blocking STO observations; Modbus polling belongs outside the control tick.

The console paces the relay feedback read after its control ticks: a read
starts just after a completed tick, at most once per ``STO_POLL_PERIOD``, and
only when the link time planned for it (the port's read budget) ends before
the next scheduled tick. Otherwise it waits for the next completed tick, so
the tick keeps the link for its command writes.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
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
    """A memory-only observation, stamped at the last successful read.

    read_unresolved marks an attempt pending or failed since that success.
    Only a successful read clears it, including after cancellation of a poll.
    """

    state: StoState
    source: StoSourceKind
    last_success_at: Monotonic | None
    detail: str
    read_unresolved: bool = False


def sto_ready(reading: StoReading) -> bool:
    """Only a positive ready observation attests readiness."""
    return reading.state is StoState.READY


def rest_of_poll_period(began: Monotonic, now: Monotonic) -> Seconds:
    """What is left of ``STO_POLL_PERIOD`` at ``now`` after a read that began at ``began``.

    The polling task waits it out before it looks for the next completed tick
    (:meth:`DriveInputStoSource.run`). The simulation's ``drive_input`` variant,
    whose clock moves by whole control ticks, starts its next read after the
    first completed tick at which it is zero: the same cadence, from the same
    function.
    """
    return Seconds(max(0.0, STO_POLL_PERIOD - elapsed(began, now)))


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

    ``read_budget`` is the link time planned for one read, judged by the
    console's guard against the next scheduled tick: zero for a port whose
    exchanges never suspend (the simulated drive), the ATV320's
    ``AUXILIARY_READ_BUDGET`` on the real link.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        drive: StoRegisterPort,
        address: RegisterAddress,
        input_: LogicInput,
        read_budget: Seconds = Seconds(0.0),
    ) -> None:
        self._clock: Clock = clock
        self._drive: StoRegisterPort = drive
        self._address: RegisterAddress = address
        self._input: LogicInput = input_
        self._read_budget: Seconds = read_budget
        self._poll_permitted: Callable[[], bool] | None = None
        self._next_tick: Callable[[Seconds], Awaitable[None]] | None = None
        # Counted when a read starts, after the guard: what run() paces on.
        self._attempts: int = 0
        self._reading: StoReading = StoReading(
            state=StoState.UNKNOWN,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=None,
            detail="never read",
        )

    @property
    def port(self) -> StoRegisterPort:
        """The drive transport every read goes through."""
        return self._drive

    @property
    def read_budget(self) -> Seconds:
        """The link time planned for one read, before the next scheduled tick."""
        return self._read_budget

    @property
    def attempts(self) -> int:
        """Reads started so far: a refused poll starts none."""
        return self._attempts

    def guard_polling(self, permitted: Callable[[], bool]) -> None:
        """Bind the console's silence/release guard at composition, before polling starts."""
        self._poll_permitted = permitted

    def pace_polling(self, next_tick: Callable[[Seconds], Awaitable[None]]) -> None:
        """Bind the console's wait for its next completed tick, bounded by the given time."""
        self._next_tick = next_tick

    async def poll(self) -> None:
        """One asynchronous exchange, called only by the console's polling task."""
        permitted = self._poll_permitted
        if permitted is not None and not permitted():
            return
        self._attempts += 1
        self._reading = replace(self._reading, read_unresolved=True)
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
                    read_unresolved=True,
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def run(self) -> None:
        """Poll until cancelled by the console owner, paced by its completed ticks.

        After a read, wait out the rest of ``STO_POLL_PERIOD`` from its start;
        then, with the console's pacing bound, wait for its next completed tick
        (at most one period, so the task stays alive while control stalls) and
        poll again: a read starts just after a tick, never at a random point of
        the period. A refused poll starts no read and goes straight back to
        waiting for a tick. The guard is checked immediately before every read.
        This single task awaits each exchange: at most one read already in
        flight can complete after silence/release begins, then no further frame
        refreshes ttO. The cached success simply ages to UNKNOWN while polling
        is forbidden.
        """
        while True:
            began = self._clock.monotonic()
            attempts = self._attempts
            await self.poll()
            next_tick = self._next_tick
            if self._attempts != attempts or next_tick is None:
                await asyncio.sleep(rest_of_poll_period(began, self._clock.monotonic()))
            if next_tick is not None:
                await next_tick(STO_POLL_PERIOD)

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
                read_unresolved=self._reading.read_unresolved,
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
