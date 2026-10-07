from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Final, Literal, Protocol, runtime_checkable

from src.clock import Clock
from src.units import Monotonic, RawRegister, RegisterAddress


class ExchangeKind(StrEnum):
    READ = "modbus_read"
    WRITE = "modbus_write"
    SEND = "modbus_send"
    RECEIVE_CHUNK = "modbus_receive_chunk"


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterRequest:
    at: Monotonic
    kind: Literal[ExchangeKind.READ, ExchangeKind.WRITE]
    register: RegisterAddress
    value: RawRegister | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Exchange:
    at: Monotonic
    kind: ExchangeKind
    register: RegisterAddress | None
    value: RawRegister | None
    ok: bool
    latency_ms: float
    detail: str
    raw_hex: str | None = None


TRANSPORT_KINDS: Final[frozenset[ExchangeKind]] = frozenset(
    {ExchangeKind.SEND, ExchangeKind.RECEIVE_CHUNK}
)
"""The SDK's own calls, as opposed to the register transactions they carry."""


@dataclass(frozen=True, slots=True)
class Drained:
    """What :meth:`ExchangeLog.drain` took out, and what the log had to refuse so far."""

    entries: tuple[Exchange, ...]
    refused: int
    """Exchanges refused because the log was full, since the log was created."""


class ExchangeLog:
    """Opt-in in-memory observations; worker and emergency threads append under a lock.

    ``capacity`` bounds what waits in memory. A consumer that runs as long as
    the drive does (the console's session record) gives one and calls
    :meth:`drain` regularly; past the bound the newest exchange is refused and
    counted, so a consumer that stops coming costs observations, never memory.
    ``None`` keeps everything, for a short run that reads :attr:`entries` at
    its end.

    ``transport=False`` keeps the register transactions only. The SDK's own
    calls (each request sent, each chunk of an answer received) are then not
    kept, and take no room under the bound: they are about three lines in
    four, and what they add is the bytes on the wire, which a bench diagnostic
    of the link wants and a session record does not need.

    The lock is held for one list operation, never across anything that waits:
    whoever appends (a Modbus worker, the emergency thread) is not kept from
    the drive by whoever drains.
    """

    def __init__(
        self, clock: Clock, capacity: int | None = None, *, transport: bool = True
    ) -> None:
        self.clock: Clock = clock
        self._capacity: int | None = capacity
        self._transport: bool = transport
        self._entries: list[Exchange] = []
        self._refused: int = 0
        self._lock: Lock = Lock()

    @property
    def entries(self) -> tuple[Exchange, ...]:
        with self._lock:
            return tuple(self._entries)

    def append(self, exchange: Exchange) -> None:
        if not self._transport and exchange.kind in TRANSPORT_KINDS:
            return
        with self._lock:
            if self._capacity is not None and len(self._entries) >= self._capacity:
                self._refused += 1
                return
            self._entries.append(exchange)

    def drain(self) -> Drained:
        """Take everything out, oldest first. The log goes on from empty."""
        with self._lock:
            entries = self._entries
            self._entries = []
            return Drained(tuple(entries), self._refused)

    def send(self, request: bytes, call: Callable[[], int]) -> int:
        started = self.clock.monotonic()
        raw: bytes | None = None
        ok = False
        try:
            result = call()
            raw = request[:result]
            ok = result == len(request)
            return result
        finally:
            self.append(
                Exchange(
                    at=started,
                    kind=ExchangeKind.SEND,
                    register=None,
                    value=None,
                    ok=ok,
                    latency_ms=(self.clock.monotonic() - started) * 1000,
                    detail="SDK transport call",
                    raw_hex=None if raw is None else raw.hex(),
                )
            )

    def receive(self, call: Callable[[], bytes]) -> bytes:
        started = self.clock.monotonic()
        raw: bytes | None = None
        try:
            raw = call()
            return raw
        finally:
            self.append(
                Exchange(
                    at=started,
                    kind=ExchangeKind.RECEIVE_CHUNK,
                    register=None,
                    value=None,
                    ok=bool(raw),
                    latency_ms=(self.clock.monotonic() - started) * 1000,
                    detail="SDK transport call",
                    raw_hex=None if raw is None else raw.hex(),
                )
            )


@runtime_checkable
class ObservableDrive(Protocol):
    @abstractmethod
    def observe_exchanges(self, log: ExchangeLog) -> None:
        """Record every exchange with the drive into ``log`` from now on."""
