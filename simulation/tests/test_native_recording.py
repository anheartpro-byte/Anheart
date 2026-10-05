from dataclasses import replace
from pathlib import Path

import pytest
from tests.record_support import manifest
from tests.test_atv320 import (
    Behave,
    FakeBus,
    build_drive,
    connection_exception,
    exception_response,
    io_exception,
)
from tests.test_ftdi_link import FakeAltivar, ftdi_master

from simulation.harness import run_scenario
from simulation.recording import RecordingDrive
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import load
from simulation.tracefile import frame_to_json
from src.clock import ManualClock, RealClock
from src.motor.atv320 import ATV320Drive
from src.motor.drive import ControlWord, RegisterMap
from src.record.codec import Privacy, encode
from src.record.reader import read
from src.record.writer import FRAME, Writer
from src.result import Err, Ok
from src.units import Monotonic, Seconds


@pytest.mark.parametrize("partial_failure", [False, True])
async def test_ex5_native_status_preserves_each_actual_exchange(partial_failure: bool) -> None:
    clock = ManualClock()
    bus = FakeBus(clock, RegisterMap())
    bus.registers.update({3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0})
    native = build_drive(clock, bus)
    drive = RecordingDrive(native, clock)
    assert isinstance(await drive.open(), Ok)
    first = len(drive.frames)
    bus.log.clear()
    bus.latency = Seconds(0.025)
    if partial_failure:
        bus.script_reads.extend((Behave.NORMALLY, io_exception()))
    try:
        result = await drive.read_status()
        frames = drive.frames[first:]
        expected = [3201, 8602] if partial_failure else [3201, 8602, 8604, 3204]
        assert [call.address for call in bus.log] == expected
        assert [frame.register for frame in frames] == expected
        assert [frame.value for frame in frames] == (
            [64, None] if partial_failure else [64, 0, 0, 0]
        )
        assert [frame.ok for frame in frames] == ([True, False] if partial_failure else [True] * 4)
        assert all(frame.latency_ms == pytest.approx(25) for frame in frames)
        assert isinstance(result, Err if partial_failure else Ok)
    finally:
        await drive.close()


@pytest.mark.parametrize("offset", [0, -1])
async def test_ex5_native_raw_offset_retry_and_write_roundtrip(tmp_path: Path, offset: int) -> None:
    clock = ManualClock()
    clock.advance(Seconds(5))
    regs = RegisterMap(offset=offset)
    bus = FakeBus(clock, regs)
    bus.registers.update({regs.eta: 64, regs.lfrd: 0, regs.rfrd: 0, regs.lcr: 0, regs.cmd: 0})
    native = build_drive(clock, bus, offset=offset)
    drive = RecordingDrive(native, clock)
    assert isinstance(await drive.open(), Ok)
    first = len(drive.frames)
    bus.latency = Seconds(0.025)
    bus.script_reads.extend((io_exception(),))
    bus.registers[regs.rfrd] = 65535
    try:
        assert isinstance(await native.read_register(regs.rfrd), Err)
        assert isinstance(await native.read_register(regs.rfrd), Ok)
        assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
        bus.script_writes.append(exception_response(6, 3))
        assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Err)
        frames = drive.frames[first:]
        assert [f.register for f in frames] == [regs.rfrd, regs.rfrd, regs.cmd, regs.cmd]
        assert [f.value for f in frames] == [None, 65535, 6, 6]
        assert [f.ok for f in frames] == [False, True, True, False]
        assert all(f.latency_ms == pytest.approx(25) for f in frames)
        created = Writer.create(tmp_path, manifest(), Privacy())
        assert isinstance(created, Ok)
        for frame in frames:
            payload = frame_to_json(frame, 5)
            saved = FRAME.validate_json(
                encode(
                    {
                        key: payload[key]
                        for key in ("t", "kind", "register", "value", "ok", "latency_ms", "raw_hex")
                    },
                    Privacy(),
                )
            )
            assert isinstance(created.value.frame(saved), Ok)
        loaded = read(created.value.path)
        assert isinstance(loaded, Ok)
        assert [f.value for f in loaded.value.frames] == [None, 65535, 6, 6]
        assert loaded.value.frames[0].t == 0
        assert loaded.value.frames[-1].t == pytest.approx(0.075)
    finally:
        bus.registers[regs.rfrd] = 0
        await drive.close()


@pytest.mark.parametrize("failure", ["success", "connection", "raised", "returned"])
async def test_ex5_native_emergency_write_is_an_actual_exchange(failure: str) -> None:
    clock = ManualClock()
    bus = FakeBus(clock, RegisterMap())
    bus.registers.update({3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0})
    native = build_drive(clock, bus)
    drive = RecordingDrive(native, clock)
    assert isinstance(await drive.open(), Ok)
    first = len(drive.frames)
    if failure == "connection":
        bus.script_writes.append(connection_exception())
    elif failure == "raised":
        bus.script_writes.append(OSError("synthetic transport loss"))
    elif failure == "returned":
        bus.script_writes.append(exception_response(6, 3))
    bus.latency = Seconds(0.025)
    try:
        drive.emergency_disable_blocking(drive.emergency_budget)
        (frame,) = drive.frames[first:]
        assert frame.register == 8602
        assert frame.value == 0
        assert frame.ok is (failure == "success")
        assert frame.latency_ms == pytest.approx(25)
        assert frame.at >= Monotonic(0)
    finally:
        await drive.close()


async def test_ex5_real_sdk_native_observations_survive_shared_trace_writer(tmp_path: Path) -> None:
    clock = RealClock()
    chip = FakeAltivar({3201: 64, 8602: 0, 8604: 0, 3204: 0, 7121: 0, 8501: 0})
    master, settings = ftdi_master(chip, timeout=Seconds(0.05))
    native = ATV320Drive(clock, master, settings, RegisterMap())
    drive = RecordingDrive(native, clock)
    try:
        assert isinstance(await drive.open(), Ok)
        first = len(drive.frames)
        origin = clock.monotonic()
        assert isinstance(await drive.read_status(), Ok)
        frames = drive.frames[first:]
        sent = [f for f in frames if f.kind.value == "modbus_send"]
        received = [f for f in frames if f.kind.value == "modbus_receive_chunk"]
        reads = [f for f in frames if f.kind.value == "modbus_read"]
        assert len(sent) == len(reads) == 4
        assert [f.raw_hex for f in sent] == [request.hex() for request in chip.requests[-4:]]
        assert len(received) == 8
        assert all(f.raw_hex for f in received)
        assert [f.register for f in reads] == [3201, 8602, 8604, 3204]
        assert all(f.latency_ms > 0 for f in frames)
        trace = (await run_scenario(load(SCENARIO_DIR / "manual_32_rpm_refused.json"))).trace
        recorded = replace(trace, frames=tuple(frame_to_json(f, origin) for f in frames))
        directory = recorded.write_record(tmp_path, Privacy())
        loaded = read(directory)
        assert isinstance(loaded, Ok)
        assert [f.raw_hex for f in loaded.value.frames] == [f.raw_hex for f in frames]
        assert [f.register for f in loaded.value.frames] == [f.register for f in frames]
    finally:
        await drive.close()
