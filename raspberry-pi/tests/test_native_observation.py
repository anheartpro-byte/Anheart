import pytest

from src.clock import ManualClock, RealClock
from src.motor.atv320 import ATV320Drive
from src.motor.drive import ControlWord, RegisterMap
from src.motor.observation import ExchangeKind, ExchangeLog
from src.result import Err, Ok
from src.units import Monotonic, RegisterAddress, Seconds
from tests.test_atv320 import (
    Behave,
    FakeBus,
    build_drive,
    connection_exception,
    exception_response,
    io_exception,
)
from tests.test_ftdi_link import FakeAltivar, ftdi_master


async def test_native_observer_retains_partial_status_and_actual_write_outcomes() -> None:
    clock = ManualClock()
    bus = FakeBus(clock, RegisterMap())
    bus.registers.update({3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0})
    drive = build_drive(clock, bus)
    assert isinstance(await drive.open(), Ok)
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    bus.latency = Seconds(0.025)
    bus.script_reads.extend((Behave.NORMALLY, io_exception()))
    try:
        assert isinstance(await drive.read_status(), Err)
        assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
        bus.script_writes.append(exception_response(6, 3))
        assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Err)
        assert [entry.register for entry in log.entries] == [3201, 8602, 8501, 8501]
        assert [entry.value for entry in log.entries] == [64, None, 6, 6]
        assert [entry.ok for entry in log.entries] == [True, False, True, False]
        assert all(entry.latency_ms == pytest.approx(25) for entry in log.entries)
    finally:
        await drive.close()


@pytest.mark.parametrize("failure", ["success", "connection", "raised", "returned"])
async def test_native_observer_preserves_emergency_outcomes(failure: str) -> None:
    clock = ManualClock()
    bus = FakeBus(clock, RegisterMap())
    bus.registers.update({3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0})
    drive = build_drive(clock, bus)
    assert isinstance(await drive.open(), Ok)
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    if failure == "connection":
        bus.script_writes.append(connection_exception())
    elif failure == "raised":
        bus.script_writes.append(OSError("synthetic failure"))
    elif failure == "returned":
        bus.script_writes.append(exception_response(6, 3))
    bus.latency = Seconds(0.025)
    try:
        drive.emergency_disable_blocking(drive.emergency_budget)
        (entry,) = log.entries
        assert entry.kind is ExchangeKind.WRITE
        assert entry.register == 8602
        assert entry.value == 0
        assert entry.ok is (failure == "success")
        assert entry.latency_ms == pytest.approx(25)
    finally:
        await drive.close()


async def test_native_observer_reaches_the_real_sdk_transport() -> None:
    clock = RealClock()
    chip = FakeAltivar({3201: 64, 8602: 0, 8604: 0, 3204: 0, 8501: 0})
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))
    drive = ATV320Drive(clock, master, settings, RegisterMap())
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)
    try:
        assert isinstance(await drive.open(), Ok)
        assert [entry.kind for entry in log.entries] == [
            ExchangeKind.SEND,
            ExchangeKind.RECEIVE_CHUNK,
            ExchangeKind.RECEIVE_CHUNK,
            ExchangeKind.READ,
        ]
        assert log.entries[0].raw_hex == chip.requests[0].hex()
        assert log.entries[-1].value == 64
        assert all(entry.latency_ms > 0 for entry in log.entries)
    finally:
        await drive.close()


async def test_native_observer_refuses_an_invalid_internal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = ManualClock()
    bus = FakeBus(clock, RegisterMap())
    bus.registers.update({3201: 64, 8602: 0, 8604: 0, 3204: 0, 8501: 0})
    drive = build_drive(clock, bus)
    assert isinstance(await drive.open(), Ok)
    log = ExchangeLog(clock)
    drive.observe_exchanges(log)

    def invalid_result(
        _drive: ATV320Drive, _reply: object, _address: RegisterAddress, _started: Monotonic
    ) -> None:
        return None

    try:
        with monkeypatch.context() as patch:
            patch.setattr(ATV320Drive, "_interpret_read", invalid_result)
            with pytest.raises(AssertionError):
                await drive.read_status()
        assert log.entries == ()
    finally:
        await drive.close()
