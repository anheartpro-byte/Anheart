"""The failure-injection pieces on their own: the lying drive, the corrupting source, the sensor
artefacts, the harness clock, and the schema of the new actions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from simulation.faultdrive import FaultyDrive
from simulation.faultsource import ADC_FULL_SCALE, ADC_MIDSCALE, FaultySource
from simulation.harness import HarnessClock, ManualTicker, PacedTicker, Session
from simulation.scenario import (
    SCENARIO_DIR,
    BitalinoSignal,
    ClockJump,
    EcgMode,
    EcgSpec,
    Scenario,
    SignalFault,
    parse_scenario,
)
from simulation.sensors import DirectSensor
from simulation.tests.conftest import load
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock, SimClock
from src.motor.drive import BadResponse, ControlWord
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok
from src.units import Bpm, Monotonic, MotorRpm, Seconds, UnixMillis

MINIMAL = {"name": "m", "kind": "manual", "duration_s": 10}


def _problems(document: object) -> str:
    parsed = parse_scenario(json.dumps(document))
    assert isinstance(parsed, Err), "the document was accepted"
    return parsed.error.detail


def _parse(document: object) -> Scenario:
    parsed = parse_scenario(json.dumps(document))
    assert isinstance(parsed, Ok), parsed
    return parsed.value


# -- the schema ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "fragment"),
    [
        ({**MINIMAL, "preroll_s": -1}, "preroll_s"),
        ({**MINIMAL, "ecg": {"ectopic_rate": 2}}, "probability"),
        ({**MINIMAL, "ecg": {"motion_noise_bpm_per_g": -1}}, "must not be negative"),
        ({**MINIMAL, "drive": {"initial_enabled_rpm": 5000}}, "initial_enabled_rpm"),
        (
            {**MINIMAL, "drive": {"initial_enabled_rpm": 900, "initial_fault": "OVERCURRENT"}},
            "exclusive",
        ),
        ({**MINIMAL, "drive": {"refuse_commands": ["BOGUS"]}}, "not a ControlWord"),
        (
            {**MINIMAL, "actions": [{"at_s": 1, "do": "drive_refuse_command", "word": "NOPE"}]},
            "not a ControlWord",
        ),
        (
            {
                **MINIMAL,
                "actions": [{"at_s": 1, "do": "bitalino_signal", "signal": "x", "duration_s": 1}],
            },
            "SignalFault",
        ),
    ],
)
def test_a_new_key_mistake_is_named(document: object, fragment: str) -> None:
    assert fragment in _problems(document)


def test_the_new_keys_are_read() -> None:
    scenario = _parse(
        {
            **MINIMAL,
            "preroll_s": 30,
            "ecg": {
                "ectopic_rate": 0.1,
                "ectopic_bpm": 20,
                "motion_noise_bpm_per_g": 3,
                "connect_fails": True,
            },
            "drive": {"initial_enabled_rpm": 900, "refuse_commands": ["SWITCH_ON"]},
            "actions": [
                {"at_s": 1, "do": "drive_refuse_command"},
                {"at_s": 2, "do": "clock_jump", "jump_s": -5},
                {"at_s": 3, "do": "ecg_seq_gap"},
                {"at_s": 4, "do": "start_again", "expect": "accepted"},
                {"at_s": 5, "do": "fault_reset"},
            ],
        }
    )
    assert float(scenario.preroll) == 30.0
    assert scenario.ecg.connect_fails
    assert scenario.ecg.ectopic_rate == pytest.approx(0.1)
    assert scenario.drive.initial_enabled_rpm == 900
    assert scenario.drive.refuse_commands == (ControlWord.SWITCH_ON,)
    assert isinstance(scenario.actions[1], ClockJump)
    assert float(scenario.actions[1].jump) == -5.0


def test_a_dsp_only_action_needs_the_dsp() -> None:
    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    signal = BitalinoSignal(Seconds(1.0), SignalFault.FLAT, Seconds(1.0))
    with pytest.raises(ValueError, match=r"needs ecg\.mode 'dsp'"):
        Session(replace(base, actions=(signal,)))
    session = Session(base)
    with pytest.raises(ValueError, match=r"needs ecg\.mode 'dsp'"):
        asyncio.run(session._apply(Monotonic(0.0), signal))  # pyright: ignore[reportPrivateUsage]  # the in-loop guard


def test_a_wall_clock_jump_needs_the_harness_clock() -> None:
    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    session = Session(base, ticker=PacedTicker(SimClock(speed=10.0)))
    with pytest.raises(ValueError, match="harness clock"):
        asyncio.run(session._apply(Monotonic(0.0), ClockJump(Seconds(0.0), Seconds(5.0))))  # pyright: ignore[reportPrivateUsage]  # the guard


# -- the harness clock -------------------------------------------------------------


def test_the_harness_clock_steps_the_wall_clock_only() -> None:
    clock = HarnessClock(Monotonic(10.0), UnixMillis(1_000_000))
    assert clock.unix_millis() == 1_010_000
    clock.jump_wall(Seconds(-2.5))
    assert clock.monotonic() == 10.0
    assert clock.unix_millis() == 1_007_500
    assert clock.advance(Seconds(1.0)) == 11.0
    with pytest.raises(ValueError, match="cannot advance"):
        clock.advance(Seconds(-1.0))
    assert isinstance(ManualTicker(clock).clock, HarnessClock)


# -- the lying drive ------------------------------------------------------------------


async def test_the_faulty_drive_refuses_lies_and_freezes() -> None:
    clock = ManualClock()
    sim = SimulatedDrive(clock)
    drive = FaultyDrive(sim)
    assert isinstance(await drive.open(), Ok)
    drive.refuse(ControlWord.SHUTDOWN)
    refused = await drive.write_command(ControlWord.SHUTDOWN)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, BadResponse)
    assert isinstance(await drive.write_command(ControlWord.SWITCH_ON), Ok | Err)
    assert isinstance(await drive.write_speed(MotorRpm(100)), Ok)
    drive.mismatch_echo(MotorRpm(7))
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.setpoint_echo_rpm == 7
    drive.stick_speed()
    first = await drive.read_status()
    clock.advance(Seconds(1.0))
    second = await drive.read_status()
    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value.output_rpm == second.value.output_rpm
    drive.freeze_status()
    frozen = await drive.read_status()
    again = await drive.read_status()
    assert isinstance(frozen, Ok)
    assert isinstance(again, Ok)
    assert frozen.value is again.value
    assert isinstance(await drive.read_limits(), Ok)
    assert drive.emergency_budget == sim.emergency_budget
    drive.emergency_disable_blocking(drive.emergency_budget)
    sim.inject_comms_loss(Seconds(5.0))
    assert isinstance(await drive.read_status(), Err)
    assert isinstance(await drive.close(), Ok)


# -- the corrupting source --------------------------------------------------------------


class _Source:
    """Hands out one fixed batch per call, ``remaining`` times, then ``None``."""

    def __init__(self, remaining: int) -> None:
        self.remaining = remaining

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        if self.remaining <= 0:
            return None
        self.remaining -= 1
        return SampleBatch(
            timestamp=UnixMillis(0), channels=[ChannelData("ECG", [500.0] * min(count, 8))]
        )


async def _read(fault: SignalFault | None, remaining: int = 1) -> SampleBatch | None:
    clock = ManualClock()
    source = FaultySource(_Source(remaining), clock, seed=3)
    if fault is not None:
        source.corrupt(fault, Monotonic(10.0))
    return await source.read_samples(8)


async def test_the_source_corrupts_only_while_armed() -> None:
    clean = await _read(None)
    assert clean is not None
    assert list(clean.channels[0].values) == [500.0] * 8
    flat = await _read(SignalFault.FLAT)
    assert flat is not None
    assert set(flat.channels[0].values) == {ADC_MIDSCALE}
    saturated = await _read(SignalFault.SATURATED)
    assert saturated is not None
    assert set(saturated.channels[0].values) == {ADC_FULL_SCALE}
    corrupted = await _read(SignalFault.CORRUPTED)
    assert corrupted is not None
    assert len(set(corrupted.channels[0].values)) > 1
    hummed = await _read(SignalFault.MAINS)
    assert hummed is not None
    assert max(hummed.channels[0].values) > 500.0
    assert await _read(SignalFault.STOPPED, remaining=3) is None
    gapped = await _read(SignalFault.GAPS, remaining=8)
    assert gapped is not None  # the fourth batch survives
    assert await _read(SignalFault.GAPS, remaining=2) is None


async def test_the_corruption_expires() -> None:
    clock = ManualClock()
    source = FaultySource(_Source(1), clock, seed=3)
    source.corrupt(SignalFault.FLAT, Monotonic(1.0))
    clock.advance(Seconds(2.0))
    batch = await source.read_samples(8)
    assert batch is not None
    assert list(batch.channels[0].values) == [500.0] * 8


# -- the sensor's new artefacts ------------------------------------------------------------


def test_the_sensor_skips_stops_and_adds_artefacts() -> None:
    sensor = DirectSensor(start=Monotonic(0.0), period=Seconds(1.0), noise_bpm=0.0, seed=1)
    first = sensor.sample(Monotonic(0.0), Bpm(80))
    assert first is not None
    sensor.skip(4)
    second = sensor.sample(Monotonic(1.0), Bpm(80))
    assert second is not None
    assert second.seq == first.seq + 5
    sensor.stop_silently()
    assert sensor.connected
    assert sensor.sample(Monotonic(2.0), Bpm(80)) is None

    noisy = DirectSensor(
        start=Monotonic(0.0),
        period=Seconds(1.0),
        noise_bpm=0.0,
        seed=2,
        ectopic_rate=1.0,
        ectopic_bpm=25.0,
        motion_noise_bpm_per_g=5.0,
    )
    offsets: set[int] = set()
    for second_index in range(20):
        reading = noisy.sample(Monotonic(float(second_index)), Bpm(90), 1.0)
        assert reading is not None
        assert reading.bpm is not None
        offsets.add(reading.bpm - 90)
    assert any(offset > 10 for offset in offsets)
    assert any(offset < -10 for offset in offsets)


def test_a_connect_failure_leaves_the_direct_sensor_silent() -> None:
    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    session = Session(replace(base, ecg=EcgSpec(mode=EcgMode.DIRECT, connect_fails=True)))
    sensor = session._sensor  # pyright: ignore[reportPrivateUsage]  # the wiring under test
    assert sensor is not None
    assert not sensor.connected


async def test_a_verdict_standing_before_the_start_is_recorded() -> None:
    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    session = Session(base)
    runtime = session._runtime  # pyright: ignore[reportPrivateUsage]  # the recorder under test
    runtime.request_estop("idle e-stop")
    snapshot = await runtime.tick(Monotonic(101.0))
    session._note_preroll(snapshot)  # pyright: ignore[reportPrivateUsage]
    session._note_preroll(snapshot)  # pyright: ignore[reportPrivateUsage]  # recorded once
    assert session._preroll_rules == ["operator_estop"]  # pyright: ignore[reportPrivateUsage]
