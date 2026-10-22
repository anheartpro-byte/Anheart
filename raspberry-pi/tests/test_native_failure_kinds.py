import json
from pathlib import Path
from typing import cast

import pytest

from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive, SerialSettings
from src.motor.drive import BadResponse, ControlWord, DriveError, RegisterMap
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
from src.record.reader import read
from src.record.schema import native_failure_matches
from src.result import Err, Ok, Result
from src.units import RawRegister
from tests.record_support import writer
from tests.test_atv320 import exception_response
from tests.test_modbus_recording import bus_at_rest


@pytest.mark.parametrize("kind", list(ExchangeKind))
@pytest.mark.parametrize(
    ("failure", "allowed"),
    [
        (NoResponse(), {ExchangeKind.READ, ExchangeKind.WRITE}),
        (ExceptionReply(code=2), {ExchangeKind.READ, ExchangeKind.WRITE}),
        (PortClosed(), {ExchangeKind.CONNECT}),
        (WrongFunction(code=3), {ExchangeKind.WRITE}),
        (WrongFunction(code=6), set[ExchangeKind]()),
        (InvalidReply(), {ExchangeKind.READ, ExchangeKind.WRITE}),
        (InvalidReply(out_of_range=True), {ExchangeKind.READ}),
        (
            SdkException(category=SdkError.OS),
            {ExchangeKind.CONNECT, ExchangeKind.CLOSE, ExchangeKind.READ, ExchangeKind.WRITE},
        ),
    ],
)
def test_reader_accepts_only_causes_the_driver_can_emit_for_the_kind(
    tmp_path: Path, kind: ExchangeKind, failure: ModbusFailure, allowed: set[ExchangeKind]
) -> None:
    recording = writer(tmp_path)
    error: dict[str, object] = {"type": failure.type}
    if isinstance(failure, ExceptionReply | WrongFunction):
        error["code"] = failure.code
    elif isinstance(failure, InvalidReply):
        error["out_of_range"] = failure.out_of_range
    elif isinstance(failure, SdkException):
        error["category"] = failure.category.value
    (recording.path / "drive_frames.jsonl").write_text(
        json.dumps(
            {
                "t": 0,
                "kind": kind.value,
                "register": None,
                "value": None,
                "ok": False,
                "latency_ms": 0,
                "error": error,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    closed = recording.close(ManualClock(), "test")
    assert isinstance(closed, Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok) is (kind in allowed)


@pytest.mark.parametrize("writing", [False, True])
@pytest.mark.parametrize("code", [0, 256])
async def test_invalid_wire_exception_code_is_recorded_as_invalid_response(
    writing: bool, code: int
) -> None:
    clock = ManualClock()
    bus = bus_at_rest(clock)
    registers = RegisterMap()
    drive = ATV320Drive(clock, bus, SerialSettings(port="test"), registers)
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    result: Result[RawRegister | None, DriveError]
    if writing:
        bus.script_writes.append(exception_response(6, code))
        result = await drive.write_command(ControlWord.SHUTDOWN)
    else:
        bus.script_reads.append(exception_response(3, code))
        result = await drive.read_register(registers.eta)
    assert isinstance(result, Err)
    assert isinstance(result.error, BadResponse)
    assert log.entries[-1].error == InvalidReply()
    assert log.refused == 0
    await drive.close()


def test_an_unknown_native_failure_cannot_fall_through() -> None:
    with pytest.raises(AssertionError):
        native_failure_matches(cast(ModbusFailure, object()), "modbus_read")
