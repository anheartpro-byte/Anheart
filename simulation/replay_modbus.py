from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

from simulation.replay_tape import INSTANT_EPSILON, NO_FURTHER, REGISTER_MAX, TICK, Mismatch
from src.clock import ManualClock
from src.motor.atv320 import failure_reply, register_reply, write_reply
from src.motor.observation import (
    ExchangeKind,
    ModbusFailure,
    NoResponse,
    PortClosed,
)
from src.record.schema import DriveFrame
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, elapsed

NATIVE_KINDS: Final = frozenset(
    {
        ExchangeKind.CONNECT.value,
        ExchangeKind.CLOSE.value,
        ExchangeKind.READ.value,
        ExchangeKind.WRITE.value,
    }
)
SDK_KINDS: Final = frozenset({ExchangeKind.SEND.value, ExchangeKind.RECEIVE_CHUNK.value})


@dataclass(frozen=True, slots=True)
class NativeExchange:
    t: Seconds
    kind: ExchangeKind
    register: int | None
    value: int | None
    ok: bool
    latency: Seconds
    error: ModbusFailure | None

    def located(self) -> str:
        written = self.value if self.kind is ExchangeKind.WRITE else None
        return f"{describe(self.kind, self.register, written)} at t={self.t:.3f} s"


@dataclass(frozen=True, slots=True)
class NativeFrames:
    exchanges: tuple[NativeExchange, ...]


def describe(kind: ExchangeKind, register: int | None, value: int | None) -> str:
    request = kind.value if register is None else f"{kind.value} register {register}"
    return request if value is None else f"{request} value {value}"


def parse_native(frames: Sequence[DriveFrame]) -> Result[NativeFrames, str]:
    exchanges: list[NativeExchange] = []
    for index, frame in enumerate(frames):
        if frame.kind in SDK_KINDS:
            continue
        if frame.kind not in NATIVE_KINDS:
            return Err("mixed drive-call observations and native Modbus exchanges")
        lifecycle = frame.kind in (ExchangeKind.CONNECT, ExchangeKind.CLOSE)
        if (
            frame.latency_ms < 0
            or (lifecycle and (frame.register is not None or frame.value is not None))
            or (
                not lifecycle
                and (not isinstance(frame.register, int) or not 0 <= frame.register <= REGISTER_MAX)
            )
            or (
                not lifecycle
                and (frame.ok or frame.kind == ExchangeKind.WRITE)
                and (not isinstance(frame.value, int) or not 0 <= frame.value <= REGISTER_MAX)
            )
            or isinstance(frame.register, tuple)
            or isinstance(frame.value, tuple)
        ):
            return Err(f"native drive frame {index} at t={frame.t:.3f} s has invalid register data")
        exchanges.append(
            NativeExchange(
                Seconds(frame.t),
                ExchangeKind(frame.kind),
                frame.register,
                frame.value,
                frame.ok,
                Seconds(frame.latency_ms / 1000),
                cast("ModbusFailure | None", frame.error),
            )
        )
    if not exchanges:
        return Err("SDK fragments without native register exchanges cannot be replayed")
    return Ok(NativeFrames(tuple(exchanges)))


class TapeMaster:
    """A single-use Modbus master that consumes only exchanges in the current replay step."""

    def __init__(self, frames: NativeFrames, clock: ManualClock, origin: Monotonic) -> None:
        # Owned by one replay; no worker or wall clock changes this cursor.
        self._exchanges: tuple[NativeExchange, ...] = frames.exchanges
        self._clock: ManualClock = clock
        self._origin: Monotonic = origin
        self._cursor: int = 0
        self._until: Seconds | None = None
        self.mismatch: Mismatch | None = None

    @property
    def pending(self) -> NativeExchange | None:
        if self._cursor < len(self._exchanges):
            return self._exchanges[self._cursor]
        return None

    def begin_step(self, until: Seconds | None) -> None:
        self._until = until

    def _belongs(self, recorded: NativeExchange) -> bool:
        return self._until is None or round(recorded.t, 3) < self._until

    def finish_step(self) -> Mismatch | None:
        recorded = self.pending
        if (
            self._until is not None
            and self.mismatch is None
            and recorded is not None
            and self._belongs(recorded)
        ):
            self.mismatch = Mismatch(
                recorded.t, "missing replay call (tick or command)", recorded.located()
            )
        return self.mismatch

    def _take(
        self, kind: ExchangeKind, register: int | None = None, value: int | None = None
    ) -> NativeExchange | None:
        if self.mismatch is not None:
            return None
        now = elapsed(self._origin, self._clock.monotonic())
        recorded = self.pending
        requested = describe(kind, register, value)
        if recorded is not None and not self._belongs(recorded):
            self.mismatch = Mismatch(
                now,
                f"extra replay call (tick or command): {requested}",
                f"no exchange in this step; next {recorded.located()}",
            )
            return None
        if recorded is not None and (
            recorded.kind is kind
            and recorded.register == register
            and (kind is not ExchangeKind.WRITE or recorded.value == value)
            and abs(recorded.t - now) <= TICK + INSTANT_EPSILON
        ):
            self._cursor += 1
            if recorded.latency > 0:
                self._clock.advance(recorded.latency)
            return recorded
        self.mismatch = Mismatch(
            now, requested, NO_FURTHER if recorded is None else recorded.located()
        )
        return None

    async def wait(self, delay: float) -> None:
        self._clock.advance(Seconds(delay))

    def connect(self) -> bool:
        exchange = self._take(ExchangeKind.CONNECT)
        if exchange is None:
            return False
        if exchange.ok:
            return True
        if isinstance(exchange.error, PortClosed) or exchange.error is None:
            return False
        failure_reply(exchange.error)
        return False

    def close(self) -> None:
        exchange = self._take(ExchangeKind.CLOSE)
        if exchange is not None and not exchange.ok and exchange.error is not None:
            failure_reply(exchange.error)

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface; the driver always reads one register
        exchange = self._take(ExchangeKind.READ, address)
        if exchange is None or not exchange.ok:
            return failure_reply(
                NoResponse() if exchange is None or exchange.error is None else exchange.error
            )
        return register_reply((cast("int", exchange.value),))

    def write_register(self, address: int, value: int, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface
        exchange = self._take(ExchangeKind.WRITE, address, value)
        if exchange is None or not exchange.ok:
            return failure_reply(
                NoResponse() if exchange is None or exchange.error is None else exchange.error
            )
        return write_reply(address, value)
