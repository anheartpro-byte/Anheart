"""The tape: a record's drive frames and ECG blocks, played back and checked against the calls."""

from __future__ import annotations

from dataclasses import replace

import pytest

from simulation.replay_tape import (
    EMERGENCY_BUDGET,
    LIMIT_REGISTERS,
    NO_FURTHER,
    STATUS_REGISTERS,
    TICK,
    Call,
    Exchange,
    Pace,
    TapeDrive,
    TapeSource,
    deliveries,
    describe,
    located,
    parse_frames,
)
from src.clock import ManualClock
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveFault,
    DriveState,
    EmergencyStopOutcome,
)
from src.record.ecg import Header, RawBlock
from src.record.schema import DriveFrame
from src.result import Err, Ok
from src.units import Amperes, Hertz, Monotonic, MotorRpm, Seconds, UnixMillis

ORIGIN = Monotonic(115.0)
DISABLED = 0x0640
ENABLED = 0x0637
FAULTED = 0x0648
OVERCURRENT_LFT = 9


def frame(
    kind: str,
    *,
    t: float = 0.0,
    register: int | tuple[int, ...] | None = None,
    value: int | tuple[int | None, ...] | None = None,
    ok: bool = True,
    latency_ms: float = 0.0,
) -> DriveFrame:
    return DriveFrame(t=t, kind=kind, register=register, value=value, ok=ok, latency_ms=latency_ms)


def status(word: int = DISABLED, *, t: float = 0.0, ok: bool = True) -> DriveFrame:
    values = (word, 150, 148, 12, None)
    return frame("read_status", t=t, register=STATUS_REGISTERS, value=values, ok=ok)


def limits(*, t: float = 0.0) -> DriveFrame:
    return frame("read_limits", t=t, register=LIMIT_REGISTERS, value=(500, 500, 0, 30, 35))


def tape(*frames: DriveFrame, at: float = 0.0) -> tuple[TapeDrive, ManualClock]:
    parsed = parse_frames(frames)
    assert isinstance(parsed, Ok)
    clock = ManualClock(start=Monotonic(ORIGIN + at))
    return TapeDrive(parsed.value, clock, ORIGIN), clock


# -- reading the frames -----------------------------------------------------


def test_ex1_every_drive_call_observation_becomes_an_exchange_with_what_it_recorded() -> None:
    parsed = parse_frames(
        (
            frame("open", t=-0.2),
            status(FAULTED, t=0.0),
            frame("read_failed", t=0.2, ok=False, latency_ms=500.0),
            limits(t=0.4),
            frame("command", t=0.6, register=8501, value=6),
            frame("speed", t=0.8, register=8602, value=-150),
            frame("emergency_zero", t=1.0, register=8602, value=0),
            frame("close", t=1.2),
        )
    )
    assert isinstance(parsed, Ok)
    exchanges = parsed.value
    assert [exchange.call for exchange in exchanges] == [
        Call.OPEN,
        Call.READ_STATUS,
        Call.READ_STATUS,
        Call.READ_LIMITS,
        Call.COMMAND,
        Call.SPEED,
        Call.EMERGENCY_ZERO,
        Call.CLOSE,
    ]
    read = exchanges[1].status
    assert read is not None
    assert read.state is DriveState.FAULT
    assert (read.status_word, read.setpoint_echo_rpm, read.output_rpm) == (FAULTED, 150, 148)
    assert read.current == pytest.approx(1.2)
    assert (read.fault, read.fault_code) == (None, None)
    assert exchanges[2].status is None
    assert exchanges[2].latency == Seconds(0.5)
    held = exchanges[3].limits
    assert held is not None
    assert (held.max_frequency, held.high_speed, held.low_speed) == (
        Hertz(50.0),
        Hertz(50.0),
        Hertz(0.0),
    )
    assert (held.acceleration, held.deceleration) == (Seconds(3.0), Seconds(3.5))
    assert [exchange.written for exchange in exchanges[4:7]] == [6, -150, None]


def test_ex1_a_recorded_fault_code_comes_back_as_the_fault_it_names() -> None:
    faulted = replace(status(FAULTED), value=(FAULTED, 0, 0, 0, OVERCURRENT_LFT))
    parsed = parse_frames((faulted,))
    assert isinstance(parsed, Ok)
    read = parsed.value[0].status
    assert read is not None
    assert read.fault is DriveFault.OVERCURRENT
    assert read.fault_code == OVERCURRENT_LFT


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (frame("modbus_read", register=3201, value=64), "not a drive-call observation"),
        (frame("anything else"), "not a drive-call observation"),
        (frame("open", latency_ms=-1.0), "negative latency"),
        (frame("command", register=8602, value=6), "command without its word"),
        (frame("command", register=8501, value=None), "command without its word"),
        (frame("command", register=8501, value=99), "does not know"),
        (frame("speed", register=8602, value=None), "speed write without its value"),
        (status(ok=False), "kind and outcome disagree"),
        (frame("read_failed", ok=True), "kind and outcome disagree"),
        (replace(status(), register=LIMIT_REGISTERS), "without its five registers"),
        (replace(status(), value=7), "without its five registers"),
        (replace(status(), value=(DISABLED, 0, 0, 0)), "without its five registers"),
        (replace(status(), value=(None, 0, 0, 0, None)), "without its five registers"),
        (replace(status(), value=(DISABLED, None, 0, 0, None)), "without its five registers"),
        (replace(status(), value=(DISABLED, 0, None, 0, None)), "without its five registers"),
        (replace(status(), value=(DISABLED, 0, 0, None, None)), "without its five registers"),
        (replace(status(), value=(70000, 0, 0, 0, None)), "without its five registers"),
        (replace(status(), value=(DISABLED, 0, 0, -1, None)), "without its five registers"),
        (replace(status(), value=(DISABLED, 0, 0, 0, 70000)), "without its five registers"),
        (replace(limits(), register=STATUS_REGISTERS), "without its five registers"),
        (replace(limits(), value=500), "without its five registers"),
        (replace(limits(), value=(500, 500, 0, None, 35)), "without its five registers"),
    ],
)
def test_ex1_a_frame_that_is_not_a_tape_is_refused_by_position_and_reason(
    bad: DriveFrame, reason: str
) -> None:
    parsed = parse_frames((frame("open"), replace(bad, t=12.4)))
    assert isinstance(parsed, Err)
    assert parsed.error.startswith("drive frame 1 (t=12.400 s) is ")
    assert reason in parsed.error


def test_ex1_a_failed_limits_read_needs_no_register() -> None:
    parsed = parse_frames((frame("read_limits", ok=False),))
    assert isinstance(parsed, Ok)
    assert parsed.value[0].limits is None


# -- the drive ----------------------------------------------------------------


async def test_ex1_the_tape_answers_each_call_with_what_was_recorded_in_order() -> None:
    drive, clock = tape(
        frame("open"),
        status(ENABLED),
        limits(),
        frame("command", register=8501, value=int(ControlWord.SHUTDOWN)),
        frame("speed", register=8602, value=151, latency_ms=25.0),
        frame("emergency_zero", register=8602, value=0),
        frame("close"),
    )
    assert isinstance(drive, DriveBackend)
    assert drive.acquisition_evidence == AcquisitionEvidence(0, address_proven=False)
    assert drive.emergency_budget == EMERGENCY_BUDGET
    opened = await drive.open()
    assert isinstance(opened, Ok)
    assert drive.acquisition_evidence == AcquisitionEvidence(1, address_proven=True)
    read = await drive.read_status()
    assert isinstance(read, Ok)
    assert read.value.state is DriveState.OPERATION_ENABLED
    assert read.value.current == Amperes(12 * 0.1)
    held = await drive.read_limits()
    assert isinstance(held, Ok)
    assert held.value.high_speed == Hertz(50.0)
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Ok)
    # One rpm off the recorded write is the same write (EX-2's setpoint tolerance).
    assert isinstance(await drive.write_speed(MotorRpm(150)), Ok)
    assert clock.monotonic() == Monotonic(ORIGIN + 0.025)
    assert drive.emergency_disable_blocking(Seconds(0.5)) is EmergencyStopOutcome.ACKNOWLEDGED
    before_the_last = drive.pending
    closed = await drive.close()
    assert isinstance(closed, Ok)
    assert (before_the_last is None, drive.pending is None) == (False, True)
    assert drive.mismatch is None
    assert drive.acquisition_evidence == AcquisitionEvidence(7, address_proven=True)


async def test_ex1_a_recorded_failure_is_replayed_as_a_failure_with_its_latency() -> None:
    drive, clock = tape(
        frame("open", ok=False, latency_ms=500.0),
        frame("read_failed", t=0.5, ok=False),
        frame("read_limits", t=0.5, ok=False),
        frame("command", t=0.5, register=8501, value=7, ok=False),
        frame("speed", t=0.5, register=8602, value=0, ok=False),
        frame("emergency_zero", t=0.5, register=8602, value=0, ok=False),
        frame("close", t=0.5, ok=False),
    )
    opened = await drive.open()
    assert opened == Err(CommTimeout(after=Seconds(0.5)))
    assert clock.monotonic() == Monotonic(ORIGIN + 0.5)
    assert drive.acquisition_evidence == AcquisitionEvidence(0, address_proven=False)
    assert isinstance(await drive.read_status(), Err)
    assert isinstance(await drive.read_limits(), Err)
    assert isinstance(await drive.write_command(ControlWord.SWITCH_ON), Err)
    assert isinstance(await drive.write_speed(MotorRpm(0)), Err)
    assert drive.emergency_disable_blocking(Seconds(0.5)) is EmergencyStopOutcome.SENT_UNCONFIRMED
    closed = await drive.close()
    assert isinstance(closed, Err)
    assert drive.mismatch is None


async def test_ex3_another_call_than_the_recorded_one_stops_the_tape_and_says_where() -> None:
    drive, _ = tape(frame("speed", t=12.4, register=8602, value=150), at=12.4)
    assert isinstance(await drive.read_status(), Err)
    found = drive.mismatch
    assert found is not None
    assert found.t == pytest.approx(12.4)
    assert found.requested == "read_status"
    assert found.recorded == "speed 150 motor rpm at t=12.400 s"
    # The frame was not consumed, and nothing is answered any more: every call
    # fails at once, without raising, and the first mismatch stands.
    assert drive.pending is not None
    opened = await drive.open()
    assert opened == Err(CommTimeout(after=Seconds(0.0)))
    assert drive.acquisition_evidence == AcquisitionEvidence(0, address_proven=False)
    closed = await drive.close()
    assert isinstance(closed, Err)
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Err)
    assert isinstance(await drive.read_limits(), Err)
    assert isinstance(await drive.write_speed(MotorRpm(150)), Err)
    assert drive.emergency_disable_blocking(Seconds(0.5)) is EmergencyStopOutcome.NOTHING_SENT
    assert drive.mismatch == found


async def test_ex3_another_command_word_is_not_the_recorded_exchange() -> None:
    drive, _ = tape(frame("command", register=8501, value=int(ControlWord.SWITCH_ON)))
    assert isinstance(await drive.write_command(ControlWord.ENABLE_OPERATION), Err)
    found = drive.mismatch
    assert found is not None
    assert (found.requested, found.recorded) == (
        "command ENABLE_OPERATION",
        "command SWITCH_ON at t=0.000 s",
    )


@pytest.mark.parametrize("asked", [148, 152])
async def test_ex3_a_speed_more_than_one_rpm_from_the_recorded_one_is_a_divergence(
    asked: int,
) -> None:
    drive, _ = tape(frame("speed", register=8602, value=150))
    assert isinstance(await drive.write_speed(MotorRpm(asked)), Err)
    found = drive.mismatch
    assert found is not None
    assert found.requested == f"speed {asked} motor rpm"


@pytest.mark.parametrize("asked", [149, 150, 151])
async def test_ex2_a_speed_within_one_rpm_is_the_recorded_exchange(asked: int) -> None:
    drive, _ = tape(frame("speed", register=8602, value=150))
    assert isinstance(await drive.write_speed(MotorRpm(asked)), Ok)


@pytest.mark.parametrize("recorded_at", [-0.3, 0.3])
async def test_ex3_the_recorded_exchange_asked_for_at_another_moment_is_a_divergence(
    recorded_at: float,
) -> None:
    drive, _ = tape(status(t=recorded_at))
    assert isinstance(await drive.read_status(), Err)
    found = drive.mismatch
    assert found is not None
    assert found.recorded == f"read_status at t={recorded_at:.3f} s"


@pytest.mark.parametrize("recorded_at", [-0.2, 0.0, 0.2])
async def test_ex2_an_exchange_within_one_tick_of_its_instant_is_served(
    recorded_at: float,
) -> None:
    drive, _ = tape(status(t=recorded_at))
    assert isinstance(await drive.read_status(), Ok)


async def test_ex3_a_call_past_the_end_of_the_tape_is_a_divergence() -> None:
    drive, _ = tape(frame("open"))
    opened = await drive.open()
    assert isinstance(opened, Ok)
    assert isinstance(await drive.read_limits(), Err)
    found = drive.mismatch
    assert found is not None
    assert (found.requested, found.recorded) == ("read_limits", NO_FURTHER)


def test_ex3_the_emergency_zero_keeps_the_mismatch_for_the_loop_like_any_other_call() -> None:
    drive, _ = tape(frame("speed", register=8602, value=150))
    assert drive.emergency_disable_blocking(Seconds(0.5)) is EmergencyStopOutcome.NOTHING_SENT
    found = drive.mismatch
    assert found is not None
    assert (found.requested, found.recorded) == (
        "emergency_zero",
        "speed 150 motor rpm at t=0.000 s",
    )


def test_a_call_is_described_in_the_tapes_own_words() -> None:
    assert describe(Call.COMMAND, 15) == "command ENABLE_OPERATION"
    assert describe(Call.SPEED, -3) == "speed -3 motor rpm"
    assert describe(Call.OPEN, None) == "open"
    exchange = Exchange(t=Seconds(1.5), call=Call.CLOSE, ok=True, latency=Seconds(0.0))
    assert located(exchange) == "close at t=1.500 s"


# -- the clock ------------------------------------------------------------------


def test_ex1_the_clock_moves_by_recorded_intervals_and_lands_on_the_producers_floats() -> None:
    # The harness clock: 100.0, then 0.2 added once per tick. 75 ticks of pre-roll.
    produced = 100.0
    for _ in range(75):
        produced += 0.2
    assert produced != 100.0 + 15.0  # why an instant rebuilt as start + t is not enough
    clock = ManualClock(start=Monotonic(100.0))
    pace = Pace(clock, Seconds(-15.0))
    for tick in range(1, 76):
        pace.to(Seconds(round(-15.0 + 0.2 * tick, 3)))
    assert clock.monotonic() == produced


def test_ex1_a_stall_is_as_many_periods_and_an_odd_interval_is_taken_as_it_is() -> None:
    clock = ManualClock(start=Monotonic(100.0))
    pace = Pace(clock, Seconds(0.0))
    pace.to(Seconds(0.6))
    assert clock.monotonic() == 100.0 + TICK + TICK + TICK
    pace.to(Seconds(0.6))
    assert clock.monotonic() == 100.0 + TICK + TICK + TICK
    pace.to(Seconds(0.85))
    assert clock.monotonic() == pytest.approx(100.85, abs=1e-9)
    pace.to(Seconds(0.9))
    assert clock.monotonic() == pytest.approx(100.9, abs=1e-9)


def test_ex1_a_replayed_latency_is_never_taken_back() -> None:
    clock = ManualClock(start=Monotonic(100.0))
    pace = Pace(clock, Seconds(0.0))
    clock.advance(Seconds(0.5))  # an exchange that took half a second
    pace.to(Seconds(0.2))
    assert clock.monotonic() == Monotonic(100.5)
    pace.to(Seconds(0.8))
    assert clock.monotonic() == pytest.approx(100.8, abs=1e-9)


# -- the ECG ----------------------------------------------------------------------


def block(seq: int, t_first: float, t_received: float | None = None) -> RawBlock:
    header = Header(seq, t_first, 2, ("ECG", "EDA"), 1000, t_received)
    return RawBlock(header, ((500 + seq, 501 + seq), (10, 11)))


def test_ex1_a_block_is_due_when_it_was_received_or_at_its_last_sample() -> None:
    made = deliveries((block(0, -0.4, t_received=0.0), block(1, 0.2)), ORIGIN)
    assert [delivery.due for delivery in made] == [0.0, pytest.approx(0.202)]
    first = made[0].batch
    assert first.timestamp == UnixMillis(114_600)
    assert [(data.channel, tuple(data.values)) for data in first.channels] == [
        ("ECG", (500.0, 501.0)),
        ("EDA", (10.0, 11.0)),
    ]
    assert made[1].batch.timestamp == UnixMillis(115_200)


async def test_ex1_blocks_come_back_in_order_and_not_before_their_instant() -> None:
    clock = ManualClock(start=Monotonic(ORIGIN - 0.2))
    made = deliveries(
        (block(0, -0.4, t_received=0.0), block(1, -0.2, t_received=0.0), block(2, 0.0, 0.4)),
        ORIGIN,
    )
    source = TapeSource(made, clock, ORIGIN)
    assert await source.read_samples(200) is None
    clock.advance(Seconds(0.2))
    assert await source.read_samples(200) is made[0].batch
    assert await source.read_samples(200) is made[1].batch
    assert await source.read_samples(200) is None
    clock.advance(Seconds(0.4))
    assert await source.read_samples() is made[2].batch
    assert await source.read_samples() is None
