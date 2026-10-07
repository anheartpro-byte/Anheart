from collections.abc import Iterator

import pytest

from simulation.recording import FrameKind, RecordingDrive
from simulation.tracefile import frame_to_json
from src.clock import ManualClock
from src.motor.drive import ETA_LOGICAL, LFRD_LOGICAL, RFRD_LOGICAL
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok
from src.units import Monotonic, UnixMillis


class ExchangeClock:
    def __init__(self) -> None:
        self.instants: Iterator[Monotonic] = iter((Monotonic(5), Monotonic(5.025)))

    def monotonic(self) -> Monotonic:
        return next(self.instants)

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(0)


async def test_ex5_measured_call_latency_and_start_time_are_not_placeholder_values() -> None:
    drive = RecordingDrive(SimulatedDrive(ManualClock()), ExchangeClock())
    opened = await drive.open()
    assert isinstance(opened, Ok)
    (frame,) = drive.frames
    assert frame.at == Monotonic(5.0)
    assert frame.latency_ms == pytest.approx(25)
    assert frame_to_json(frame, 3)["t"] == 2


async def test_ex5_read_status_is_one_grouped_observation_and_failed_limits_remain_unknown() -> (
    None
):
    clock = ManualClock()
    drive = RecordingDrive(SimulatedDrive(clock), clock)
    assert isinstance(await drive.read_limits(), Err)
    assert drive.frames[-1].observations == ()
    assert not drive.frames[-1].ok
    opened = await drive.open()
    assert isinstance(opened, Ok)
    readback = await drive.read_status()
    assert isinstance(readback, Ok)
    observed = drive.frames[-1]
    assert observed.kind is FrameKind.READ
    registers = dict(observed.observations)
    assert registers[ETA_LOGICAL] == readback.value.status_word
    assert registers[LFRD_LOGICAL] == readback.value.setpoint_echo_rpm
    assert registers[RFRD_LOGICAL] == readback.value.output_rpm
    assert len(drive.frames) == 3
