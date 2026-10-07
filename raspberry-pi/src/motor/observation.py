from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Literal, Protocol, runtime_checkable

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


class ExchangeLog:
    """Opt-in in-memory observations; worker and emergency threads append under a lock."""

    def __init__(self, clock: Clock) -> None:
        self.clock: Clock = clock
        self._entries: list[Exchange] = []
        self._lock: Lock = Lock()

    @property
    def entries(self) -> tuple[Exchange, ...]:
        with self._lock:
            return tuple(self._entries)

    def append(self, exchange: Exchange) -> None:
        with self._lock:
            self._entries.append(exchange)

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
