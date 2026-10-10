"""The LUX processor and its simulated room: transfer function, rotation from flicker, quality.

The headline test is the round trip: a simulated capsule turning at 5, 15 and
27 tr/min past a lamp, through the 6-bit quantisation, noise and light
switches, and the processor must read the rotation back within 1 tr/min.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.geometry import CONFIRMED_GEAR_RATIO, MachineGeometry
from src.result import Err, Ok
from src.sensors import lux
from src.sensors.base import SensorKind, SensorReading
from src.sensors.lux import (
    FULL_SCALE_COUNTS,
    SPEC,
    LUXProcessor,
    NoFlicker,
    RotationCheck,
    cross_check,
    detect_events,
    estimate_flicker,
    judge,
    smooth,
    to_percent,
)
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.sim.signals.base import SignalContext
from src.sim.signals.lux import LUXGenerator, LuxModel
from src.sim.signals.lux import make as make_generator
from src.training.types import SignalQuality
from src.units import (
    Bpm,
    GLoad,
    Hertz,
    Metres,
    Monotonic,
    OutputRpm,
    Seconds,
    output_to_motor_rpm,
)

FS: Final[int] = 100
AT: Final[Monotonic] = Monotonic(123.0)
WINDOW_SAMPLES: Final[int] = round(SPEC.window_s * FS)
ROUND_TRIP_RPM: Final[tuple[float, ...]] = (5.0, 15.0, 27.0)
ACCURACY_RPM: Final[float] = 1.0
SWITCHING_ROOM: Final[LuxModel] = LuxModel(switch_interval_s=40.0)


def _subject(output_rpm: float) -> SubjectState:
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=output_to_motor_rpm(OutputRpm(output_rpm), CONFIRMED_GEAR_RATIO),
        output_rpm=OutputRpm(output_rpm),
        g_load=GLoad(0.0),
        heart_rate=Bpm(70),
        rr_interval=Seconds(60.0 / 70.0),
        steady_state=Bpm(70),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def _render(
    generator: LUXGenerator, output_rpm: float, *, seconds: float = 60.0, fs: int = FS
) -> list[float]:
    """``seconds`` of the channel, in 0.2 s blocks as the simulated client asks for them."""
    block = max(1, fs // 5)
    samples: list[float] = []
    for index in range(round(seconds * fs / block)):
        context = SignalContext(
            start=Monotonic(index * block / fs), fs=fs, count=block, subject=_subject(output_rpm)
        )
        samples.extend(float(v) for v in generator.render(context))
    return samples


def _value(reading: SensorReading, key: str) -> float | None:
    metric = reading.metric(key)
    return None if metric is None else metric.value


def _all_blank(reading: SensorReading) -> bool:
    return all(metric.value is None for metric in reading.metrics)


def _sine(freq: float, seconds: float, rate: float, amplitude: float = 5.0) -> list[float]:
    return [
        50.0 + amplitude * math.sin(2.0 * math.pi * freq * i / rate)
        for i in range(round(seconds * rate))
    ]


# =========================================================================
# Transfer function and spec
# =========================================================================


def test_transfer_function_is_plux_adc_over_two_to_the_n() -> None:
    assert FULL_SCALE_COUNTS == 64
    assert to_percent(0.0) == 0.0
    assert to_percent(32.0) == 50.0
    assert to_percent(63.0) == pytest.approx(98.4375)


def test_spec_and_factory() -> None:
    processor = lux.make()
    assert isinstance(processor, LUXProcessor)
    assert processor.spec is SPEC
    assert SPEC.kind is SensorKind.LUX
    assert SPEC.unit == "%"
    assert make_generator(4).kind is SensorKind.LUX


# =========================================================================
# The rotation cross-check: round trip through the simulated room
# =========================================================================


@pytest.mark.parametrize("output_rpm", ROUND_TRIP_RPM)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_round_trip_recovers_the_rotation(output_rpm: float, seed: int) -> None:
    raw = _render(LUXGenerator(seed, SWITCHING_ROOM), output_rpm)
    reading = LUXProcessor().process(raw, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    flicker = _value(reading, "flicker_rpm")
    assert flicker is not None
    assert abs(flicker - output_rpm) <= ACCURACY_RPM
    assert cross_check(OutputRpm(flicker), OutputRpm(output_rpm)) is RotationCheck.AGREES


def test_round_trip_at_the_hardware_rate() -> None:
    raw = _render(LUXGenerator(7, SWITCHING_ROOM), 15.0, fs=1000)
    reading = LUXProcessor().process(raw, 1000, AT)
    flicker = _value(reading, "flicker_rpm")
    assert reading.quality is SignalQuality.GOOD
    assert flicker is not None
    assert abs(flicker - 15.0) <= ACCURACY_RPM


def test_light_switches_are_counted_and_do_not_fake_a_rotation() -> None:
    generator = LUXGenerator(2, LuxModel(switch_interval_s=20.0))
    raw = _render(generator, 0.0)
    reading = LUXProcessor().process(raw, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    events = _value(reading, "events")
    assert events is not None
    assert events >= 1.0
    assert _value(reading, "flicker_rpm") is None
    assert reading.detail != ""


def test_simulated_bitalino_with_the_lux_generator_yields_a_good_column() -> None:
    clock = ManualClock()
    arm = MachineGeometry(radius=Metres(1.5), ratio=CONFIRMED_GEAR_RATIO)
    client = SimulatedBitalinoClient(
        clock,
        physiology=Physiology(geometry=arm, origin=clock.monotonic()),
        channels=[0, 5],
        sample_rate=FS,
        generators={"LUX": make_generator(3)},
    )
    client.set_motor_rpm(output_to_motor_rpm(OutputRpm(15.0), CONFIRMED_GEAR_RATIO))

    async def acquire() -> list[float]:
        assert await client.connect()
        assert await client.start_acquisition()
        column: list[float] = []
        clock.advance(Seconds(60.5))
        for _ in range(300):
            batch = await client.read_samples(FS // 5)
            assert batch is not None
            column.extend(next(c.values for c in batch.channels if c.channel == "LUX"))
        return column

    column = asyncio.run(acquire())
    assert all(0.0 <= v <= SensorKind.LUX.adc_max for v in column)
    reading = LUXProcessor().process(column, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    flicker = _value(reading, "flicker_rpm")
    assert flicker is not None
    assert abs(flicker - 15.0) <= ACCURACY_RPM


# =========================================================================
# Steady light and degraded inputs
# =========================================================================


def test_steady_light_is_good_with_no_variation() -> None:
    reading = LUXProcessor().process([30.0] * WINDOW_SAMPLES, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "level") == pytest.approx(to_percent(30.0))
    assert _value(reading, "variation") == pytest.approx(0.0, abs=1e-9)
    assert _value(reading, "flicker_rpm") is None
    assert _value(reading, "events") == 0.0
    assert "pas de scintillement" in reading.detail
    assert reading.waveform[0] == pytest.approx(to_percent(30.0))


def test_steady_light_toggling_one_lsb_is_still_good() -> None:
    raw = [30.0 + (i // 7) % 2 for i in range(WINDOW_SAMPLES)]
    reading = LUXProcessor().process(raw, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    variation = _value(reading, "variation")
    assert variation is not None
    assert variation < 1.0
    assert _value(reading, "flicker_rpm") is None


def test_steady_light_in_a_real_room_reads_no_rotation() -> None:
    raw = _render(LUXGenerator(5, LuxModel(switch_interval_s=math.inf)), 0.0)
    reading = LUXProcessor().process(raw, FS, AT)
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "flicker_rpm") is None


@pytest.mark.parametrize(
    ("raw", "fs", "quality", "words"),
    [
        ([], FS, SignalQuality.NO_SIGNAL, "aucun"),
        ([30.0] * 300, 0, SignalQuality.NO_SIGNAL, "frequence"),
        ([30.0, math.nan] * 150, FS, SignalQuality.NO_SIGNAL, "corrompue"),
        ([30.0, math.inf] * 150, FS, SignalQuality.NO_SIGNAL, "corrompue"),
        ([30.0, 64.0] * 150, FS, SignalQuality.NO_SIGNAL, "corrompue"),
        ([30.0, -1.0] * 150, FS, SignalQuality.NO_SIGNAL, "corrompue"),
        ([30.0] * 100, FS, SignalQuality.NO_SIGNAL, "courte"),
        ([0.0] * 300, FS, SignalQuality.NO_SIGNAL, "debranche"),
        ([1.0, 0.0] * 150, FS, SignalQuality.NO_SIGNAL, "debranche"),
        ([63.0] * 300, FS, SignalQuality.NOISY, "butee"),
        ([63.0, 40.0] * 150, FS, SignalQuality.NOISY, "sature"),
    ],
)
def test_degraded_inputs(raw: list[float], fs: int, quality: SignalQuality, words: str) -> None:
    reading = LUXProcessor().process(raw, fs, AT)
    assert reading.quality is quality
    assert words in reading.detail
    assert _all_blank(reading)
    assert [m.key for m in reading.metrics] == ["level", "variation", "flicker_rpm", "events"]
    assert all(math.isfinite(v) for v in reading.waveform)
    assert reading.kind is SensorKind.LUX
    assert reading.at == AT


def test_a_flicker_dipping_to_zero_is_not_saturation() -> None:
    raw = [0.0 if (i // 150) % 2 else 20.0 for i in range(WINDOW_SAMPLES)]
    assert judge(raw, FS) is None


# =========================================================================
# The pieces
# =========================================================================


def test_smooth_decimates_and_filters() -> None:
    y, rate = smooth([50.0] * 1000, 1000)
    assert rate == 20.0
    assert len(y) == 20
    assert y[5] == pytest.approx(50.0)


def test_smooth_uses_raw_samples_when_the_filter_refuses() -> None:
    y, rate = smooth([10.0, 20.0, 30.0], 1000)
    assert y == (10.0,)
    assert rate == 20.0


def test_smooth_leaves_slow_rates_unfiltered() -> None:
    assert smooth([1.0, 2.0, 3.0], 10) == ((1.0, 2.0, 3.0), 10.0)


def test_detect_events_finds_each_step_where_it_happens() -> None:
    rate = Hertz(20.0)
    y = [40.0] * 200 + [10.0] * 200 + [40.0] * 200
    assert detect_events(y, rate) == (200, 400)


def test_detect_events_ignores_a_glitch_and_a_short_window() -> None:
    rate = Hertz(20.0)
    assert detect_events([40.0] * 100 + [0.0] + [40.0] * 100, rate) == ()
    assert detect_events([40.0, 0.0], rate) == ()
    assert detect_events([], rate) == ()


def test_detect_events_ignores_a_shallow_flicker() -> None:
    rate = Hertz(20.0)
    for rpm in (3.0, 15.0, 30.0, 60.0):
        assert detect_events(_sine(rpm / 60.0, 60.0, rate, amplitude=8.0), rate) == ()


def _err(result: Ok[OutputRpm] | Err[NoFlicker]) -> str:
    assert isinstance(result, Err), f"expected no flicker, got {result!r}"
    return result.error.detail


def test_estimate_flicker_refusals() -> None:
    rate = Hertz(20.0)
    assert "il en faut" in _err(estimate_flicker(_sine(0.25, 10.0, rate), rate))
    assert "frequents" in _err(estimate_flicker(_sine(0.25, 60.0, rate), rate, (1, 2, 3, 4)))
    assert "pas de scintillement" in _err(estimate_flicker([50.0] * 1200, rate))
    assert _err(estimate_flicker(_sine(0.25, 60.0, rate), Hertz(math.nan))) != ""
    assert "aucune puissance" in _err(estimate_flicker(_sine(0.01, 100.0, 0.1), Hertz(0.1)))
    noise = [50.0 + 5.0 * math.sin(i * i * 0.7) for i in range(1200)]
    assert "raie nette" in _err(estimate_flicker(noise, rate))


def test_estimate_flicker_tolerates_a_change_at_the_very_edge() -> None:
    rate = Hertz(20.0)
    found = estimate_flicker(_sine(0.25, 60.0, rate), rate, (0,))
    assert isinstance(found, Ok)
    assert found.value == pytest.approx(15.0, abs=ACCURACY_RPM)


def test_estimate_flicker_reads_the_fundamental_not_a_harmonic() -> None:
    rate = Hertz(20.0)
    fundamental = 0.15  # 9 tr/min
    y = [
        50.0
        + 3.0 * math.sin(2.0 * math.pi * fundamental * i / rate)
        + 5.0 * math.sin(2.0 * math.pi * 2.0 * fundamental * i / rate)
        for i in range(1200)
    ]
    found = estimate_flicker(y, rate)
    assert isinstance(found, Ok)
    assert found.value == pytest.approx(9.0, abs=ACCURACY_RPM)


def test_estimate_flicker_ignores_a_weak_harmonic() -> None:
    rate = Hertz(20.0)
    y = [
        50.0
        + 5.0 * math.sin(2.0 * math.pi * 0.4 * i / rate)
        + 1.0 * math.sin(2.0 * math.pi * 0.8 * i / rate)
        for i in range(1200)
    ]
    found = estimate_flicker(y, rate)
    assert isinstance(found, Ok)
    assert found.value == pytest.approx(24.0, abs=ACCURACY_RPM)


def test_cross_check() -> None:
    assert cross_check(None, OutputRpm(15.0)) is RotationCheck.UNAVAILABLE
    assert cross_check(OutputRpm(15.0), OutputRpm(1.0)) is RotationCheck.UNAVAILABLE
    assert cross_check(OutputRpm(15.0), OutputRpm(80.0)) is RotationCheck.UNAVAILABLE
    assert cross_check(OutputRpm(15.4), OutputRpm(15.0)) is RotationCheck.AGREES
    assert cross_check(OutputRpm(15.4), OutputRpm(-15.0)) is RotationCheck.AGREES
    assert cross_check(OutputRpm(30.0), OutputRpm(15.0)) is RotationCheck.DISAGREES


# =========================================================================
# The generator
# =========================================================================


def test_generator_is_deterministic_per_seed() -> None:
    first = _render(LUXGenerator(11, SWITCHING_ROOM), 15.0, seconds=5.0)
    again = _render(LUXGenerator(11, SWITCHING_ROOM), 15.0, seconds=5.0)
    other = _render(LUXGenerator(12, SWITCHING_ROOM), 15.0, seconds=5.0)
    assert first == again
    assert first != other


def test_processor_is_deterministic() -> None:
    raw = _render(LUXGenerator(1), 27.0)
    assert LUXProcessor().process(raw, FS, AT) == LUXProcessor().process(raw, FS, AT)


def test_generator_degenerate_blocks_are_empty() -> None:
    generator = LUXGenerator()
    subject = _subject(10.0)
    assert generator.render(SignalContext(start=AT, fs=0, count=10, subject=subject)) == ()
    assert generator.render(SignalContext(start=AT, fs=FS, count=0, subject=subject)) == ()
    assert generator.render(SignalContext(start=AT, fs=FS, count=-3, subject=subject)) == ()


def test_generator_switches_the_lights() -> None:
    generator = LUXGenerator(0, LuxModel(switch_interval_s=0.05))
    seen: set[bool] = set()
    for _ in range(20):
        _render(generator, 0.0, seconds=0.2)
        seen.add(generator.lights_on)
    assert seen == {True, False}


def test_generator_without_lamp_or_noise_is_constant() -> None:
    model = LuxModel(flicker_depth_percent=0.0, noise_counts=0.0, switch_interval_s=math.inf)
    raw = _render(LUXGenerator(0, model), 20.0, seconds=2.0)
    assert set(raw) == {round(45.0 / 100.0 * 64)}


@pytest.mark.parametrize(
    "fields",
    [
        {"room_percent": -1.0},
        {"flicker_depth_percent": math.nan},
        {"noise_counts": math.inf},
        {"switch_drop_percent": -0.1},
        {"switch_interval_s": 0.0},
        {"switch_interval_s": math.nan},
    ],
)
def test_impossible_rooms_are_rejected(fields: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        LuxModel(**fields)


# =========================================================================
# Properties
# =========================================================================

_anything: Final = st.floats(allow_nan=True, allow_infinity=True, width=64)


def _check_reading(reading: SensorReading) -> None:
    assert reading.kind is SensorKind.LUX
    assert all(math.isfinite(v) for v in reading.waveform)
    if reading.quality is SignalQuality.GOOD:
        for metric in reading.metrics:
            assert metric.value is None or math.isfinite(metric.value)
        flicker = _value(reading, "flicker_rpm")
        assert flicker is None or 2.0 <= flicker <= 61.0
    else:
        assert _all_blank(reading)
        assert reading.detail != ""


@settings(max_examples=150, deadline=None)
@given(
    raw=st.lists(_anything, max_size=400),
    fs=st.sampled_from([-1, 0, 1, 10, 100, 1000]),
)
def test_process_is_total_on_garbage(raw: Sequence[float], fs: int) -> None:
    _check_reading(LUXProcessor().process(raw, fs, AT))


@settings(max_examples=60, deadline=None)
@given(
    raw=st.lists(st.integers(min_value=0, max_value=63), min_size=20, max_size=800),
    fs=st.sampled_from([1, 10]),
)
def test_process_is_total_on_valid_counts(raw: Sequence[int], fs: int) -> None:
    _check_reading(LUXProcessor().process([float(v) for v in raw], fs, AT))


@settings(max_examples=40, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**32),
    output_rpm=st.floats(min_value=-80.0, max_value=80.0),
    fs=st.sampled_from([1, 10, 100, 1000]),
    count=st.integers(min_value=0, max_value=500),
    model=st.builds(
        LuxModel,
        room_percent=st.floats(min_value=0.0, max_value=150.0),
        flicker_depth_percent=st.floats(min_value=0.0, max_value=150.0),
        switch_drop_percent=st.floats(min_value=0.0, max_value=150.0),
        switch_interval_s=st.just(0.5),
    ),
)
def test_generator_stays_on_the_six_bit_scale(
    seed: int, output_rpm: float, fs: int, count: int, model: LuxModel
) -> None:
    samples = LUXGenerator(seed, model).render(
        SignalContext(start=AT, fs=fs, count=count, subject=_subject(output_rpm))
    )
    assert len(samples) == count
    assert all(0 <= v <= SensorKind.LUX.adc_max for v in samples)


@settings(max_examples=25, deadline=None)
@given(
    output_rpm=st.floats(min_value=4.0, max_value=55.0),
    seed=st.integers(min_value=0, max_value=1000),
)
def test_round_trip_holds_across_the_band(output_rpm: float, seed: int) -> None:
    raw = _render(LUXGenerator(seed, LuxModel(switch_interval_s=math.inf)), output_rpm, fs=20)
    reading = LUXProcessor().process(raw, 20, AT)
    flicker = _value(reading, "flicker_rpm")
    assert flicker is not None
    assert abs(flicker - output_rpm) <= ACCURACY_RPM
