import json
from pathlib import Path
from typing import cast

import pytest

from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive, SerialSettings, failure_reply, register_reply, write_reply
from src.motor.drive import BadResponse, CommTimeout, ControlWord, DriveError, RegisterMap
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
from src.record.drive_tap import exchange_source
from src.record.reader import read
from src.record.writer import FRAME
from src.result import Err, Ok, Result
from src.units import OutOfRange, RawRegister, Seconds
from tests.record_support import writer
from tests.test_atv320 import (
    FakeBus,
    Returned,
    connection_exception,
    exception_response,
    io_exception,
    parameter_exception,
    read_reply,
    write_echo,
)

CASES: tuple[
    tuple[object, ModbusFailure, type[CommTimeout] | type[BadResponse] | type[OutOfRange]], ...
] = (
    (Returned(io_exception()), NoResponse(), CommTimeout),
    (Returned(connection_exception()), NoResponse(), CommTimeout),
    (Returned(parameter_exception()), SdkException(category=SdkError.MODBUS), BadResponse),
    (io_exception(), SdkException(category=SdkError.IO), CommTimeout),
    (connection_exception(), SdkException(category=SdkError.CONNECTION), CommTimeout),
    (OSError("private diagnostic"), SdkException(category=SdkError.OS), CommTimeout),
    (parameter_exception(), SdkException(category=SdkError.MODBUS), BadResponse),
    (ValueError("private diagnostic"), SdkException(category=SdkError.OTHER), BadResponse),
    (exception_response(3, 2), ExceptionReply(code=2), BadResponse),
    (None, InvalidReply(), BadResponse),
)


def bus_at_rest(clock: ManualClock) -> FakeBus:
    registers = RegisterMap()
    bus = FakeBus(clock, registers)
    bus.registers.update(
        {
            registers.eta: 0x0640,
            registers.lfrd: 0,
            registers.rfrd: 0,
            registers.lcr: 0,
            registers.lft: 0,
            registers.cmd: 0,
        }
    )
    return bus


def _reply(error: ModbusFailure) -> object:
    try:
        response = failure_reply(error)
    except Exception as raised:
        return raised
    return Returned(response) if isinstance(response, BaseException) else response


@pytest.mark.parametrize(("response", "cause", "variant"), CASES)
@pytest.mark.parametrize("writing", [False, True], ids=["read", "write"])
async def test_native_failure_replays_through_the_sdk_with_the_same_driver_variant(
    response: object,
    cause: ModbusFailure,
    variant: type[CommTimeout] | type[BadResponse] | type[OutOfRange],
    writing: bool,
) -> None:
    observed: list[DriveError] = []
    for reply in (response, _reply(cause)):
        clock = ManualClock()
        registers = RegisterMap()
        bus = bus_at_rest(clock)
        drive = ATV320Drive(clock, bus, SerialSettings(port="test"), registers)
        log = ExchangeLog(clock, transport=False)
        drive.observe_exchanges(log)
        opened_9 = await drive.open()
        assert isinstance(opened_9, Ok)
        bus.latency = Seconds(0.025)
        answer: Result[RawRegister | None, DriveError]
        if writing:
            bus.script_writes.append(reply)
            answer = await drive.write_command(ControlWord.SHUTDOWN)
        else:
            bus.script_reads.append(reply)
            answer = await drive.read_register(registers.eta)
        assert isinstance(answer, Err)
        assert isinstance(answer.error, variant)
        entries = exchange_source(log)().observations
        failure = entries[-1]
        assert failure.error == cause
        encoded = FRAME.dump_json(failure.frame(clock.monotonic()))
        assert b"private diagnostic" not in encoded
        assert FRAME.validate_json(encoded).error == cause
        observed.append(answer.error)
        closed_6 = await drive.close()
        assert isinstance(closed_6, Ok)
    assert type(observed[0]) is type(observed[1])
    if isinstance(observed[0], CommTimeout):
        assert observed[0] == observed[1]


@pytest.mark.parametrize(
    ("response", "cause", "writing", "variant"),
    [
        (read_reply([65536]), InvalidReply(out_of_range=True), False, OutOfRange),
        (read_reply([]), InvalidReply(), False, BadResponse),
        (write_echo(3201, 0), InvalidReply(), False, BadResponse),
        (read_reply([0]), WrongFunction(code=3), True, BadResponse),
    ],
)
async def test_malformed_and_wrong_function_replies_keep_their_closed_cause(
    response: object,
    cause: ModbusFailure,
    writing: bool,
    variant: type[BadResponse] | type[OutOfRange],
) -> None:
    await test_native_failure_replays_through_the_sdk_with_the_same_driver_variant(
        response, cause, variant, writing
    )


async def test_connect_and_close_failures_are_native_bounded_and_never_prose() -> None:
    clock = ManualClock()
    bus = bus_at_rest(clock)
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), RegisterMap())
    log = ExchangeLog(clock, capacity=2, transport=False)
    drive.observe_exchanges(log)
    bus.port_opens = False
    opened = await drive.open()
    assert isinstance(opened, Err)
    port_failure = log.entries[-1].error
    assert port_failure == PortClosed()
    bus.raise_on_connect = OSError("private diagnostic")
    opened_2 = await drive.open()
    assert isinstance(opened_2, Err)
    raised_failure = log.entries[-1].error
    assert raised_failure == SdkException(category=SdkError.OS)
    assert log.refused == 0
    opened_3 = await drive.open()
    assert isinstance(opened_3, Err)
    assert log.refused == 1
    bus.raise_on_connect = None
    bus.port_opens = True
    log.drain()
    opened_4 = await drive.open()
    assert isinstance(opened_4, Ok)
    log.drain()
    bus.raise_on_close = RuntimeError("private diagnostic")
    await drive.close()
    assert log.refused > 1


async def test_bad_observation_costs_only_the_record_never_a_drive_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    bus = bus_at_rest(clock)
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), RegisterMap())
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)

    def broken(*_: object) -> None:
        raise RuntimeError("private diagnostic")

    monkeypatch.setattr(log, "append", broken)
    opened_5 = await drive.open()
    assert isinstance(opened_5, Ok)
    assert log.refused == 2
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert log.refused > 2


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "unknown"},
        {"type": "exception_response", "code": 0},
        {"type": "exception_response", "code": 256},
        {"type": "wrong_function", "code": -1},
        {"type": "wrong_function", "code": 256},
        {"type": "wrong_function", "code": True},
        {"type": "no_response", "detail": "private diagnostic"},
        {"type": "sdk_exception", "category": "unknown"},
        {"type": "sdk_exception", "category": 1},
        {"type": "invalid_response", "out_of_range": 1},
        {"type": "BadResponse"},
    ],
)
def test_reader_refuses_unknown_native_causes_and_call_failure_vocabulary(
    tmp_path: Path, payload: object
) -> None:
    recording = writer(tmp_path)
    (recording.path / "drive_frames.jsonl").write_text(
        json.dumps(
            {
                "t": 0,
                "kind": "modbus_read",
                "register": 3201,
                "value": None,
                "ok": False,
                "latency_ms": 0,
                "error": payload,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    closed_2 = recording.close(ManualClock(), "test")
    assert isinstance(closed_2, Ok)
    assert isinstance(read(recording.path), Err)


def test_reader_refuses_native_cause_on_call_observation() -> None:
    with pytest.raises(ValueError, match="vocabulary"):
        FRAME.validate_json(
            b'{"t":0,"kind":"read_failed","register":null,"value":null,"ok":false,"latency_ms":0,"error":{"type":"no_response"}}'
        )


@pytest.mark.parametrize("response", [connection_exception(), OSError("private diagnostic")])
async def test_emergency_write_keeps_raised_sdk_cause(response: BaseException) -> None:
    clock = ManualClock()
    bus = bus_at_rest(clock)
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), RegisterMap())
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    opened_6 = await drive.open()
    assert isinstance(opened_6, Ok)
    bus.script_writes.append(response)
    outcome = drive.emergency_disable_blocking(drive.emergency_budget)
    assert outcome.name == (
        "NOTHING_SENT" if isinstance(response, type(connection_exception())) else "SENT_UNCONFIRMED"
    )
    error = log.entries[-1].error
    assert isinstance(error, SdkException)
    assert error.category is (
        SdkError.CONNECTION if outcome.name == "NOTHING_SENT" else SdkError.OS
    )
    closed_3 = await drive.close()
    assert isinstance(closed_3, Ok)


async def test_close_failure_and_closed_port_reply_have_a_closed_vocabulary() -> None:
    with pytest.raises(Exception, match="recorded closed port"):
        failure_reply(PortClosed())
    clock = ManualClock()
    bus = bus_at_rest(clock)
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), RegisterMap())
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    opened_7 = await drive.open()
    assert isinstance(opened_7, Ok)
    bus.raise_on_close = RuntimeError("private diagnostic")
    closed_4 = await drive.close()
    assert isinstance(closed_4, Ok)
    assert log.entries[-1].kind is ExchangeKind.CLOSE
    assert log.entries[-1].error == SdkException(category=SdkError.OTHER)


async def test_response_factories_use_the_sdk_shapes_the_driver_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    bus = bus_at_rest(clock)
    bus.script_reads.append(register_reply((0x0640,)))
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), RegisterMap())
    opened_8 = await drive.open()
    assert isinstance(opened_8, Ok)
    bus.script_writes.append(write_reply(8501, 6))
    write_command_result = await drive.write_command(ControlWord.SHUTDOWN)
    assert isinstance(write_command_result, Ok)
    closed_5 = await drive.close()
    assert isinstance(closed_5, Ok)
    with pytest.raises(AssertionError):
        failure_reply(cast(ModbusFailure, object()))
    cause = SdkException(category=SdkError.OTHER)
    with monkeypatch.context() as patch:
        patch.setattr(SdkException, "category", object())
        with pytest.raises(AssertionError):
            failure_reply(cause)
