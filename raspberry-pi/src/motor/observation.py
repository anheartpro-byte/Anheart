from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Annotated, Final, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass as validated

from src.clock import Clock
from src.units import Monotonic, RawRegister, RegisterAddress

WRITE_SINGLE_REGISTER_CODE: Final[int] = 6
"""Modbus function code required for a write acknowledgement, unlike a read's register block."""


class ExchangeKind(StrEnum):
    CONNECT = "modbus_connect"
    CLOSE = "modbus_close"
    READ = "modbus_read"
    WRITE = "modbus_write"
    SEND = "modbus_send"
    RECEIVE_CHUNK = "modbus_receive_chunk"


FAILURE_CONFIG = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class NoResponse:
    type: Literal["no_response"] = "no_response"


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class ExceptionReply:
    type: Literal["exception_response"] = "exception_response"
    code: Annotated[int, Field(ge=1, le=255)]


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class WrongFunction:
    type: Literal["wrong_function"] = "wrong_function"
    code: Annotated[int, Field(ge=0, le=255)]


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class PortClosed:
    type: Literal["port_closed"] = "port_closed"


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class InvalidReply:
    type: Literal["invalid_response"] = "invalid_response"
    out_of_range: bool = False


class SdkError(StrEnum):
    CONNECTION = "connection"
    IO = "io"
    OS = "os"
    MODBUS = "modbus"
    OTHER = "other"


@validated(frozen=True, slots=True, kw_only=True, config=FAILURE_CONFIG)
class SdkException:
    type: Literal["sdk_exception"] = "sdk_exception"
    category: SdkError


type ModbusFailure = Annotated[
    NoResponse | ExceptionReply | WrongFunction | PortClosed | InvalidReply | SdkException,
    Field(discriminator="type"),
]

MODBUS_FAILURES: Final = (
    NoResponse,
    ExceptionReply,
    WrongFunction,
    PortClosed,
    InvalidReply,
    SdkException,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterRequest:
    at: Monotonic
    kind: Literal[ExchangeKind.READ, ExchangeKind.WRITE, ExchangeKind.CONNECT, ExchangeKind.CLOSE]
    register: RegisterAddress | None
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
    error: ModbusFailure | None = None


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
    """Opt-in in-memory observations, appended by the driver and taken by one consumer.

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

    **Whoever drains holds nothing an append needs.** Exchanges are appended
    at one end of a ``deque`` and taken from the other, each by one operation
    the interpreter does not interrupt: :meth:`drain` takes no lock at all. A
    consumer stopped anywhere, even in the middle of a drain, therefore keeps
    nobody from the drive, and the tick that awaits the driver never waits on
    it. The one lock here is between those who APPEND (a Modbus worker, the
    emergency path): it keeps the bound and the count of the refused exact
    when two of them append at once, and it is held for that append alone.
    One consumer drains: two at once could each count on the same exchange.
    """

    def __init__(
        self, clock: Clock, capacity: int | None = None, *, transport: bool = True
    ) -> None:
        self.clock: Clock = clock
        self._capacity: int | None = capacity
        self._transport: bool = transport
        self._entries: deque[Exchange] = deque()
        self._refused: int = 0
        self._appending: Lock = Lock()

    @property
    def entries(self) -> tuple[Exchange, ...]:
        """What waits now, oldest first, left where it is."""
        return tuple(self._entries.copy())

    @property
    def refused(self) -> int:
        """Exchanges lost to capacity or observation failures: what :meth:`drain` reports.

        Read without taking anything out, and without the lock: one ``int``,
        whole at any instant, for whoever needs the count at a moment of its
        own choosing (the console, at the two ends of a session).
        """
        return self._refused

    def append(self, exchange: Exchange) -> None:
        if not self._transport and exchange.kind in TRANSPORT_KINDS:
            return
        with self._appending:
            if self._capacity is not None and len(self._entries) >= self._capacity:
                self._refused += 1
                return
            self._entries.append(exchange)

    def refuse(self) -> None:
        with self._appending:
            self._refused += 1

    def drain(self) -> Drained:
        """Take out everything that waits now, oldest first. No lock: see the class."""
        entries = [self._entries.popleft() for _ in range(len(self._entries))]
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
