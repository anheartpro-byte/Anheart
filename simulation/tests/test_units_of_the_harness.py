"""The small pieces: the DIRECT sensor, the recording drive, the trace writer, the tickers."""

from __future__ import annotations

import asyncio
import math
from itertools import pairwise
from pathlib import Path

import pytest

from simulation.harness import (
    MOTION_LIMITS_PATH,
    PANEL_SAFETY,
    RUNTIME_LIMITS,
    TICK,
    ManualTicker,
    PacedTicker,
    load_motion,
)
from simulation.recording import FrameKind, InjectedTickError, RecordingDrive
from simulation.scenario import SCENARIO_DIR
from simulation.sensors import DirectSensor
from simulation.tests.conftest import run_file
from simulation.tracefile import finite
from src.clock import ManualClock, SimClock
from src.motor.drive import ControlWord, DriveFault
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok
from src.training.types import SignalQuality
from src.units import Bpm, Monotonic, MotorRpm, Seconds


def test_the_harness_mirrors_the_console_it_stands_in_for() -> None:
    """Drift detector: the harness's constants are the composition root's."""
    from src import local_panel  # noqa: PLC0415  # heavy import, this test only
    from src.local_config import DEFAULT_MOTION_LIMITS_PATH  # noqa: PLC0415

    assert local_panel.RUNTIME_LIMITS == RUNTIME_LIMITS
    assert local_panel.CONTROL_PERIOD == TICK
    assert PANEL_SAFETY.hard_max_bpm == local_panel.PANEL_HARD_MAX_BPM
    assert PANEL_SAFETY.critical_bpm == local_panel.PANEL_CRITICAL_BPM
    assert MOTION_LIMITS_PATH == local_panel.PROJECT_ROOT / DEFAULT_MOTION_LIMITS_PATH
    assert load_motion().min_run == 55


def test_an_unusable_motion_file_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from simulation import harness  # noqa: PLC0415

    broken = tmp_path / "limits.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(harness, "MOTION_LIMITS_PATH", broken)
    with pytest.raises(ValueError, match="motion limits unusable"):
        harness.load_motion()


# -- the DIRECT sensor ---------------------------------------------------------


def _sensor(noise: float = 0.0) -> DirectSensor:
    return DirectSensor(start=Monotonic(0.0), period=Seconds(1.0), noise_bpm=noise, seed=1)


def test_the_sensor_reports_once_a_period_with_an_advancing_seq() -> None:
    sensor = _sensor()
    assert sensor.connected
    first = sensor.sample(Monotonic(0.0), Bpm(70))
    assert first is not None
    assert (first.seq, first.quality, first.bpm) == (1, SignalQuality.GOOD, 70)
    assert sensor.sample(Monotonic(0.5), Bpm(70)) is None
    second = sensor.sample(Monotonic(1.0), Bpm(71))
    assert second is not None
    assert second.seq == 2


def test_the_sensor_models_every_ecg_fault() -> None:
    sensor = _sensor()
    sensor.dropout(Monotonic(2.0))
    assert sensor.sample(Monotonic(0.0), Bpm(70)) is None
    last = sensor.sample(Monotonic(2.0), Bpm(72))
    assert last is not None
    sensor.repeat(Monotonic(5.0))
    assert sensor.sample(Monotonic(3.0), Bpm(90)) == last
    sensor.grade(SignalQuality.MAINS_DOMINATED, Monotonic(7.0))
    graded = sensor.sample(Monotonic(5.0), Bpm(90))
    assert graded is not None
    assert (graded.quality, graded.bpm) == (SignalQuality.MAINS_DOMINATED, None)
    sensor.force(Bpm(180), Monotonic(9.0))
    forced = sensor.sample(Monotonic(7.0), Bpm(90))
    assert forced is not None
    assert (forced.quality, forced.bpm) == (SignalQuality.GOOD, 180)
    implausible = sensor.sample(Monotonic(9.0), Bpm(250))
    assert implausible is not None
    assert implausible.bpm is None
    sensor.disconnect()
    assert not sensor.connected
    assert sensor.sample(Monotonic(20.0), Bpm(70)) is None


def test_sensor_noise_is_seeded_and_reproducible() -> None:
    a, b = _sensor(noise=5.0), _sensor(noise=5.0)
    seen_a = [a.sample(Monotonic(float(t)), Bpm(100)) for t in range(20)]
    seen_b = [b.sample(Monotonic(float(t)), Bpm(100)) for t in range(20)]
    assert seen_a == seen_b
    assert len({r.bpm for r in seen_a if r is not None}) > 1


# -- the recording drive ---------------------------------------------------------


async def test_the_recording_drive_logs_every_frame_it_passes_on() -> None:
    clock = ManualClock()
    drive = RecordingDrive(SimulatedDrive(clock), clock)
    assert isinstance(await drive.open(), Ok)
    assert isinstance(await drive.read_limits(), Ok)
    assert isinstance(await drive.read_status(), Ok)
    await drive.write_command(ControlWord.SHUTDOWN)
    await drive.write_speed(MotorRpm(100))
    assert drive.emergency_budget > 0
    drive.emergency_disable_blocking(drive.emergency_budget)
    await drive.close()
    failed = await drive.read_status()
    assert isinstance(failed, Err)
    kinds = [frame.kind for frame in drive.frames]
    assert kinds[:7] == [
        FrameKind.OPEN,
        FrameKind.READ_LIMITS,
        FrameKind.READ,
        FrameKind.COMMAND,
        FrameKind.SPEED,
        FrameKind.EMERGENCY_ZERO,
        FrameKind.CLOSE,
    ]
    assert FrameKind.READ_FAILED in kinds
    assert drive.reads == 1
    drive.raise_on_next_read()
    with pytest.raises(InjectedTickError):
        await drive.read_status()


# -- the trace -------------------------------------------------------------------


def test_nothing_non_finite_is_ever_written() -> None:
    assert finite(1.0) == 1.0
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="non-finite"):
            finite(bad)


def test_the_csv_has_one_line_per_tick(tmp_path: Path) -> None:
    result = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json")
    written = result.trace.write_csv(tmp_path / "rows.csv")
    lines = written.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("t,state,mode,phase")
    assert len(lines) == len(result.trace.rows) + 1


# -- the tickers -------------------------------------------------------------------


async def test_the_manual_ticker_moves_by_exactly_one_tick() -> None:
    ticker = ManualTicker()
    start = ticker.clock.monotonic()
    assert await ticker.next() == pytest.approx(start + TICK)


async def test_the_paced_ticker_sleeps_in_real_time_scaled() -> None:
    """Ticks are due every TICK of simulated time; a late loop catches up, never skips back."""
    ticker = PacedTicker(SimClock(speed=10.0))  # one tick = 20 ms of real time
    instants = [await ticker.next() for _ in range(5)]
    assert all(b >= a for a, b in pairwise(instants))
    assert instants[-1] - instants[0] == pytest.approx(4 * TICK, abs=0.1)
    await asyncio.sleep(0.1)  # 1 simulated second late: the next ticks are already due
    late = await ticker.next()
    assert late - instants[-1] > 5 * TICK


async def test_the_sleeping_ticker_is_deterministic_and_refuses_a_zero_speed() -> None:
    from simulation.harness import SleepingTicker  # noqa: PLC0415

    ticker = SleepingTicker(1000.0)
    start = ticker.clock.monotonic()
    assert await ticker.next() == pytest.approx(start + TICK)
    with pytest.raises(ValueError, match="speed must be positive"):
        SleepingTicker(0.0)


def test_an_injected_fault_before_the_start_is_a_refused_start() -> None:
    result = run_file(SCENARIO_DIR / "fault_drive_already_faulted.json")
    assert result.start_refusal == "DriveInFault"
    assert result.scenario.drive.initial_fault is DriveFault.MOTOR_OVERLOAD
    assert not [f for f in result.trace.frames if f["kind"] == "speed" and f["value"] != 0]


def test_a_direct_only_action_is_refused_even_if_validation_were_bypassed() -> None:
    """The in-loop guard behind the constructor's check: it raises, it does not ignore."""
    from dataclasses import replace  # noqa: PLC0415

    from simulation.harness import Session  # noqa: PLC0415
    from simulation.scenario import EcgDropout, EcgMode, EcgSpec  # noqa: PLC0415
    from simulation.tests.conftest import load  # noqa: PLC0415

    base = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    session = Session(replace(base, ecg=EcgSpec(mode=EcgMode.DSP)))
    action = EcgDropout(Seconds(1.0), Seconds(1.0))
    with pytest.raises(ValueError, match="needs ecg"):
        asyncio.run(session._apply(Monotonic(0.0), action))  # pyright: ignore[reportPrivateUsage]  # the guard itself


def test_a_drive_that_never_answers_again_is_reported_unknown_not_stopped() -> None:
    """A comms loss that outlasts the teardown: the probe says 'unknown', never '0 rpm'."""
    from dataclasses import replace  # noqa: PLC0415

    from simulation.harness import run_scenario  # noqa: PLC0415
    from simulation.invariants import check_invariants  # noqa: PLC0415
    from simulation.scenario import CommsLoss  # noqa: PLC0415
    from simulation.tests.conftest import load  # noqa: PLC0415

    base = load(SCENARIO_DIR / "manual_27_rpm.json")
    scenario = replace(
        base,
        duration=Seconds(30.0),
        teardown=Seconds(5.0),
        actions=(base.actions[0], CommsLoss(Seconds(20.0), Seconds(600.0))),
    )
    result = asyncio.run(run_scenario(scenario))
    assert result.trace.final.shaft_motor_rpm is None
    assert "exit_unknown" in {v.check for v in check_invariants(result)}


def test_a_reader_notes_a_missing_required_integer() -> None:
    from simulation.jsondoc import Reader  # noqa: PLC0415

    reader = Reader({}, "doc")
    assert reader.integer("n") == 0
    assert reader.problems == ("doc: missing 'n'",)


def test_a_broken_profile_library_refuses_to_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import simulation.scenario as scenario_module  # noqa: PLC0415

    broken = tmp_path / "_profiles.json"
    broken.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(scenario_module, "SIM_PROFILES_PATH", broken)
    with pytest.raises(ValueError, match=r"_profiles\.json unusable"):
        scenario_module.parse_scenario(
            '{"name": "x", "kind": "auto", "duration_s": 1, "profile": "x"}'
        )
