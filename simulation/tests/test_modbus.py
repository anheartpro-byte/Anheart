import asyncio
from dataclasses import replace

import pytest

from simulation.modbus import FailedExchange, InlineExecutor, SimulatedMaster, immediate
from simulation.replay_modbus import TapeMaster, parse_native
from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive, SerialSettings
from src.motor.drive import CommTimeout, ControlWord, DriveFault, DriveState, RegisterMap
from src.motor.observation import (
    ExceptionReply,
    ExchangeKind,
    ExchangeLog,
    InvalidReply,
    ModbusFailure,
    NoResponse,
    PortClosed,
    SdkError,
    SdkException,
    WrongFunction,
)
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.drive_tap import exchange_source
from src.record.schema import DriveFrame
from src.result import Err, Ok
from src.units import Monotonic, MotorRpm, RegisterAddress, Seconds


def frame(
    kind: ExchangeKind = ExchangeKind.READ,
    *,
    t: float = 0,
    register: int | tuple[int, ...] | None = 3201,
    value: int | tuple[int | None, ...] | None = 0x0640,
    ok: bool = True,
    latency_ms: float = 0,
    raw_hex: str | None = None,
    error: ModbusFailure | None = None,
) -> DriveFrame:
    return DriveFrame(
        t=t,
        kind=kind.value,
        register=register,
        value=value,
        ok=ok,
        latency_ms=latency_ms,
        raw_hex=raw_hex,
        error=error,
    )


def tape(*frames: DriveFrame, at: float = 0) -> tuple[TapeMaster, ManualClock]:
    parsed = parse_native(frames)
    assert isinstance(parsed, Ok), parsed
    clock = ManualClock(Monotonic(at))
    return TapeMaster(parsed.value, clock, Monotonic(0)), clock


def test_inline_executor_preserves_result_exception_and_shutdown() -> None:
    executor = InlineExecutor()

    def multiply(number: int, *, factor: int) -> int:
        return number * factor

    assert executor.submit(multiply, 3, factor=4).result() == 12

    def fail() -> int:
        raise ValueError("scripted")

    with pytest.raises(ValueError, match="scripted"):
        executor.submit(fail).result()
    executor.shutdown()
    with pytest.raises(RuntimeError, match="shutdown"):
        executor.submit(fail)


def test_the_physical_model_must_not_suspend_inside_a_register_call() -> None:
    async def suspended() -> None:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="suspended"):
        immediate(suspended())


async def test_the_real_driver_uses_the_model_for_states_ramps_limits_offset_and_stop() -> None:
    clock = ManualClock()
    registers = RegisterMap(offset=1)
    model = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    master = SimulatedMaster(model, clock, registers)
    drive = ATV320Drive(
        clock,
        master,
        SerialSettings(port="simulated"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    log = ExchangeLog(clock, transport=False)
    drive.observe_exchanges(log)
    assert isinstance(await drive.open(), Ok)
    limits = await drive.read_limits()
    assert isinstance(limits, Ok)
    assert limits.value == SimulatedDriveConfig().limits
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await drive.write_command(word), Ok)
    assert isinstance(await drive.write_speed(MotorRpm(-138)), Ok)
    clock.advance(Seconds(0.4))
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.state is DriveState.OPERATION_ENABLED
    assert status.value.setpoint_echo_rpm == -138
    assert -138 < status.value.output_rpm < 0
    assert status.value.current > 0
    assert isinstance(await drive.close(), Ok)
    assert model.commanded_setpoint == 0
    assert log.entries[-1].kind is ExchangeKind.CLOSE
    assert log.entries[1].register == registers.eta
    assert any(entry.register == registers.tfr for entry in log.entries)


async def test_a_model_fault_is_read_from_lft_and_requires_an_explicit_reset() -> None:
    clock = ManualClock()
    registers = RegisterMap()
    model = SimulatedDrive(clock)
    master = SimulatedMaster(model, clock, registers)
    drive = ATV320Drive(
        clock,
        master,
        SerialSettings(port="simulated"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    assert isinstance(await drive.open(), Ok)
    model.inject_fault(DriveFault.OVERCURRENT)
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.fault is DriveFault.OVERCURRENT
    assert status.value.fault_code == 9
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
    assert isinstance(await drive.enable(), Err)
    assert isinstance(await drive.write_command(ControlWord.FAULT_RESET), Ok)
    assert isinstance(await drive.close(), Ok)


async def test_the_slave_reports_bad_addresses_and_a_closed_or_silent_link() -> None:
    clock = ManualClock()
    registers = RegisterMap()
    master = SimulatedMaster(SimulatedDrive(clock), clock, registers)
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="simulated"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    assert isinstance(await driver.open(), Ok)
    assert isinstance(await driver.read_register(RegisterAddress(0)), Err)
    master.close()
    assert isinstance(await driver.read_status(), Err)
    assert master.connect()
    master.model.inject_comms_loss(Seconds(1))
    result = await driver.read_limits()
    assert isinstance(result, Err)
    assert isinstance(result.error, CommTimeout)
    await driver.close()


@pytest.mark.parametrize(
    "error",
    [
        NoResponse(),
        ExceptionReply(code=2),
        WrongFunction(code=3),
        SdkException(category=SdkError.CONNECTION),
        SdkException(category=SdkError.IO),
        SdkException(category=SdkError.OS),
        SdkException(category=SdkError.MODBUS),
        SdkException(category=SdkError.OTHER),
        InvalidReply(),
        InvalidReply(out_of_range=True),
    ],
)
@pytest.mark.parametrize("kind", [ExchangeKind.READ, ExchangeKind.WRITE])
async def test_each_native_failure_is_recorded_and_replayed_under_the_real_driver(
    error: ModbusFailure,
    kind: ExchangeKind,
) -> None:
    clock = ManualClock()
    registers = RegisterMap()
    master = SimulatedMaster(SimulatedDrive(clock), clock, registers)
    drive = ATV320Drive(
        clock,
        master,
        SerialSettings(port="simulated"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    assert isinstance(await drive.open(), Ok)
    address = registers.eta if kind is ExchangeKind.READ else registers.cmd
    master.failures.append(FailedExchange(kind, error, address, Seconds(0.025)))
    original = (
        await drive.read_register(address)
        if kind is ExchangeKind.READ
        else await drive.write_command(ControlWord.SHUTDOWN)
    )
    assert isinstance(original, Err)
    observations = exchange_source(log)().observations
    failed = observations[-1].frame(Monotonic(0))
    replayed_master, replayed_clock = tape(failed)
    replayed_drive = ATV320Drive(
        replayed_clock,
        replayed_master,
        SerialSettings(port="replay"),
        registers,
        executor_factory=InlineExecutor,
        wait=replayed_master.wait,
    )
    replayed = (
        await replayed_drive.read_register(address)
        if kind is ExchangeKind.READ
        else await replayed_drive.write_command(ControlWord.SHUTDOWN)
    )
    assert isinstance(replayed, Err)
    assert type(replayed.error) is type(original.error)
    assert replayed_master.mismatch is None
    assert replayed_master.pending is None
    assert replayed_clock.monotonic() == pytest.approx(0.025)
    await drive.close()
    await replayed_drive.close()


def test_step_boundaries_never_borrow_the_next_step_and_leftovers_are_named() -> None:
    master, _ = tape(frame(t=0.2))
    master.begin_step(Seconds(0.2))
    master.read_holding_registers(3201)
    assert master.mismatch is not None
    assert master.mismatch.t == Seconds(0)
    assert "register 3201" in master.mismatch.requested
    assert "next modbus_read register 3201" in master.mismatch.recorded
    assert master.finish_step() is master.mismatch
    assert master.connect() is False
    master, _ = tape(frame(t=0.1))
    master.begin_step(Seconds(0.2))
    found = master.finish_step()
    assert found is not None
    assert found.t == 0.1
    assert "missing" in found.requested
    master, _ = tape(frame())
    assert master.finish_step() is None
    master.begin_step(Seconds(0.2))
    master.read_holding_registers(3201)
    assert master.finish_step() is None
    master.read_holding_registers(3201)
    assert master.mismatch is not None
    assert master.mismatch.recorded == "no further exchange"


@pytest.mark.parametrize(("address", "value", "at"), [(8502, 6, 0), (8501, 7, 0), (8501, 6, 0.3)])
def test_native_calls_require_the_exact_register_write_value_and_time(
    address: int, value: int, at: float
) -> None:
    master, _ = tape(frame(ExchangeKind.WRITE, register=8501, value=6), at=at)
    master.write_register(address, value)
    assert master.mismatch is not None
    assert "8501" in master.mismatch.recorded


@pytest.mark.parametrize(
    "recorded",
    [
        frame(latency_ms=-1.0),
        frame(register=None),
        frame(register=-1),
        frame(register=65536),
        frame(register=(3201,)),
        frame(value=None),
        frame(value=-1),
        frame(value=65536),
        frame(value=(1,)),
    ],
)
def test_native_frames_require_single_unsigned_registers_and_nonnegative_latency(
    recorded: DriveFrame,
) -> None:
    assert isinstance(parse_native((recorded,)), Err)


def test_native_lifecycle_has_no_registers_and_sdk_fragments_are_diagnostic_only() -> None:
    assert isinstance(parse_native((frame(ExchangeKind.CONNECT),)), Err)
    sdk = frame(ExchangeKind.SEND, register=None, value=None, raw_hex="abcd")
    assert isinstance(parse_native((sdk,)), Err)
    valid = parse_native((sdk, frame(), replace(sdk, kind="modbus_receive_chunk")))
    assert isinstance(valid, Ok)
    assert len(valid.value.exchanges) == 1
    assert isinstance(parse_native((frame(), replace(frame(), kind="open"))), Err)
    master, _ = tape(
        frame(ExchangeKind.CONNECT, register=None, value=None, ok=False, error=NoResponse())
    )
    assert master.connect() is False


@pytest.mark.parametrize(
    "cause", [PortClosed(), NoResponse(), SdkException(category=SdkError.OTHER)]
)
async def test_simulated_and_tape_masters_preserve_port_open_failure(cause: ModbusFailure) -> None:
    clock = ManualClock()
    registers = RegisterMap()
    master = SimulatedMaster(SimulatedDrive(clock), clock, registers)
    master.failures.append(FailedExchange(ExchangeKind.CONNECT, cause))
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="test"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    log = ExchangeLog(clock)
    driver.observe_exchanges(log)
    original = await driver.open()
    assert isinstance(original, Err)
    observations = exchange_source(log)().observations
    recorded, replay_clock = tape(*(item.frame(Monotonic(0)) for item in observations))
    replay_driver = ATV320Drive(
        replay_clock,
        recorded,
        SerialSettings(port="replay"),
        registers,
        executor_factory=InlineExecutor,
        wait=recorded.wait,
    )
    replayed = await replay_driver.open()
    assert isinstance(replayed, Err)
    assert type(replayed.error) is type(original.error)
    assert recorded.mismatch is None
    await driver.close()
    await replay_driver.close()


async def test_port_close_failure_replays_under_the_real_driver() -> None:
    clock = ManualClock()
    registers = RegisterMap()
    master = SimulatedMaster(SimulatedDrive(clock), clock, registers)
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="test"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    log = ExchangeLog(clock)
    driver.observe_exchanges(log)
    assert isinstance(await driver.open(), Ok)
    master.failures.append(FailedExchange(ExchangeKind.CLOSE, SdkException(category=SdkError.OS)))
    assert isinstance(await driver.close(), Ok)
    observations = exchange_source(log)().observations
    recorded, replay_clock = tape(*(item.frame(Monotonic(0)) for item in observations))
    replay_driver = ATV320Drive(
        replay_clock,
        recorded,
        SerialSettings(port="replay"),
        registers,
        executor_factory=InlineExecutor,
        wait=recorded.wait,
    )
    assert isinstance(await replay_driver.open(), Ok)
    assert isinstance(await replay_driver.close(), Ok)
    assert recorded.pending is None
    assert recorded.mismatch is None
    await recorded.wait(0.2)
    assert replay_clock.monotonic() == pytest.approx(0.2)


async def test_altering_the_tape_failure_class_changes_the_driver_variant() -> None:
    errors: list[type[object]] = []
    for cause in (NoResponse(), ExceptionReply(code=2)):
        master, clock = tape(frame(ok=False, value=None, error=cause))
        driver = ATV320Drive(
            clock,
            master,
            SerialSettings(port="replay"),
            RegisterMap(),
            executor_factory=InlineExecutor,
            wait=master.wait,
        )
        result = await driver.read_register(RegisterAddress(3201))
        assert isinstance(result, Err)
        errors.append(type(result.error))
        await driver.close()
    assert errors[0] is CommTimeout
    assert errors[1].__name__ == "BadResponse"


class FixedReply:
    def __init__(self, response: object) -> None:
        self.response: object = response

    def connect(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface
        return self.response

    def write_register(self, address: int, value: int, slave: int = 1) -> object:  # noqa: ARG002  # ModbusMaster interface
        return self.response


async def test_invalid_requests_and_filtered_failures_reach_the_driver() -> None:
    clock = ManualClock()
    registers = RegisterMap()
    master = SimulatedMaster(SimulatedDrive(clock), clock, registers)
    assert master.connect()
    responses = (
        (master.read_holding_registers(3201, count=2), 3),
        (master.write_register(8501, 999), 3),
        (master.write_register(0, 0), 2),
    )
    for response, code in responses:
        driver = ATV320Drive(
            clock,
            FixedReply(response),
            SerialSettings(port="test"),
            registers,
            executor_factory=InlineExecutor,
            wait=master.wait,
        )
        log = ExchangeLog(clock)
        driver.observe_exchanges(log)
        assert isinstance(await driver.read_register(registers.eta), Err)
        assert log.entries[-1].error == ExceptionReply(code=code)
        await driver.close()
    master.failures.append(FailedExchange(ExchangeKind.READ, NoResponse(), 123))
    assert isinstance(immediate(master.model.write_command(ControlWord.SHUTDOWN)), Ok)
    driver = ATV320Drive(
        clock,
        master,
        SerialSettings(port="test"),
        registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    assert isinstance(await driver.read_register(registers.eta), Ok)
    assert len(master.failures) == 1
    result = await driver.read_register(RegisterAddress(123))
    assert isinstance(result, Err)
    assert isinstance(result.error, CommTimeout)
    await driver.close()
