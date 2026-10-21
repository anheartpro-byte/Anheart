from collections import deque
from collections.abc import Callable, Coroutine
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import override

from src.clock import ManualClock
from src.motor.atv320 import failure_reply, register_reply, write_reply
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
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFaulted,
    RegisterMap,
    UnexpectedState,
    decode_speed,
)
from src.motor.observation import (
    ExceptionReply,
    ExchangeKind,
    ModbusFailure,
    NoResponse,
    PortClosed,
)
from src.motor.simulated import SimulatedDrive
from src.result import Err
from src.units import RawRegister, RegisterAddress, Seconds


class InlineExecutor(Executor):
    def __init__(self) -> None:
        # The driver owns this executor and its lifecycle on one replay loop.
        self._closed: bool = False

    @override
    def submit[T, **P](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        if self._closed:
            raise RuntimeError("cannot schedule new futures after shutdown")
        future: Future[T] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as error:
            future.set_exception(error)
        return future

    @override
    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self._closed = True


def immediate[T](operation: Coroutine[None, None, T]) -> T:
    # Model operations must complete without suspending: this adapter never runs a nested loop.
    values: list[T] = []

    async def capture() -> None:
        values.append(await operation)

    coroutine = capture()
    try:
        coroutine.send(None)
    except StopIteration:
        return values[0]
    coroutine.close()
    raise RuntimeError("the physical model suspended during a register transaction")


@dataclass(frozen=True, slots=True)
class FailedExchange:
    kind: ExchangeKind
    error: ModbusFailure
    register: int | None = None
    latency: Seconds = Seconds(0.0)


class SimulatedMaster:
    """Synchronous Modbus seam over the existing physical model, with scripted failures."""

    def __init__(self, model: SimulatedDrive, clock: ManualClock, registers: RegisterMap) -> None:
        # One driver owns this mutable master and its failure queue.
        self.model: SimulatedDrive = model
        self.clock: ManualClock = clock
        self.registers: RegisterMap = registers
        self.failures: deque[FailedExchange] = deque()

    async def wait(self, delay: float) -> None:
        self.clock.advance(Seconds(delay))

    def _failure(self, kind: ExchangeKind, address: int | None = None) -> ModbusFailure | None:
        if not self.failures:
            return None
        next_failure = self.failures[0]
        if next_failure.kind is not kind or (
            next_failure.register is not None and next_failure.register != address
        ):
            return None
        self.failures.popleft()
        self.clock.advance(next_failure.latency)
        return next_failure.error

    def connect(self) -> bool:
        failed = self._failure(ExchangeKind.CONNECT)
        if isinstance(failed, PortClosed):
            return False
        if failed is not None:
            failure_reply(failed)
            return False
        immediate(self.model.open())
        return True

    def close(self) -> None:
        failed = self._failure(ExchangeKind.CLOSE)
        if failed is not None:
            failure_reply(failed)
        immediate(self.model.close())

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface
        failed = self._failure(ExchangeKind.READ, address)
        if failed is not None:
            return failure_reply(failed)
        if count != 1:
            return failure_reply(ExceptionReply(code=3))
        logical = RegisterAddress(address - self.registers.offset)
        if logical in (TFR_LOGICAL, HSP_LOGICAL, LSP_LOGICAL, ACC_LOGICAL, DEC_LOGICAL):
            limits = immediate(self.model.read_limits())
            if isinstance(limits, Err):
                return _rejected(limits)
            value = {
                TFR_LOGICAL: round(limits.value.max_frequency * 10),
                HSP_LOGICAL: round(limits.value.high_speed * 10),
                LSP_LOGICAL: round(limits.value.low_speed * 10),
                ACC_LOGICAL: round(limits.value.acceleration * 10),
                DEC_LOGICAL: round(limits.value.deceleration * 10),
            }[logical]
        else:
            status = immediate(self.model.read_status())
            if isinstance(status, Err):
                return _rejected(status)
            values = {
                ETA_LOGICAL: int(status.value.status_word),
                LFRD_LOGICAL: int(status.value.setpoint_echo_rpm) & 0xFFFF,
                RFRD_LOGICAL: int(status.value.output_rpm) & 0xFFFF,
                LCR_LOGICAL: round(status.value.current * 10),
                LFT_LOGICAL: 0 if status.value.fault_code is None else int(status.value.fault_code),
            }
            if logical not in values:
                return failure_reply(ExceptionReply(code=2))
            value = values[logical]
        return register_reply((value,))

    def write_register(self, address: int, value: int, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface
        failed = self._failure(ExchangeKind.WRITE, address)
        if failed is not None:
            return failure_reply(failed)
        logical = RegisterAddress(address - self.registers.offset)
        if logical == CMD_LOGICAL:
            try:
                word = ControlWord(value)
            except ValueError:
                return failure_reply(ExceptionReply(code=3))
            result = immediate(self.model.write_command(word))
            if isinstance(result, Err) and isinstance(result.error, UnexpectedState | DriveFaulted):
                return write_reply(address, value)
        elif logical == LFRD_LOGICAL:
            result = immediate(self.model.write_speed(decode_speed(RawRegister(value))))
        else:
            return failure_reply(ExceptionReply(code=2))
        if isinstance(result, Err):
            return _rejected(result)
        return write_reply(address, value)


def _rejected(result: Err[DriveError]) -> object:
    return failure_reply(
        NoResponse() if isinstance(result.error, CommTimeout) else ExceptionReply(code=3)
    )
