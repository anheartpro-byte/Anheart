"""EDA: the datasheet transfer function, the processor's grading, and the generator.

The round trips run the synthetic subject through the real processor: a tonic
level the generator knows (ground truth) must come back as the SCL, and the
SCR rate must rise with the stress that drives the Poisson process.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import src.sensors.eda as eda_module
from src.clock import ManualClock
from src.dsp import DspRefused, Signal
from src.geometry import MachineGeometry
from src.result import Err, Result
from src.sensors.base import SensorKind, SensorReading
from src.sensors.eda import (
    EDA_GAIN,
    MAX_RISE_S,
    MIN_SCL_US,
    SCR_THRESHOLD_US,
    SPEC,
    VCC_V,
    EDAProcessor,
    Microsiemens,
    adc_count,
    detect_scrs,
    lowest_block,
    make,
    max_slope,
    microsiemens,
)
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.sim.signals import eda as eda_signal
from src.sim.signals.base import SignalContext
from src.sim.signals.eda import DEFAULT_EDA_MODEL, EDAGenerator, EDAModel, scr_shape
from src.training.types import SignalQuality
from src.units import (
    AdcCount,
    Bpm,
    GearRatio,
    GLoad,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
)

FS: Final[int] = 100
WINDOW: Final[int] = round(float(SPEC.window_s) * FS)
AT: Final[Monotonic] = Monotonic(12.5)
ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.0), ratio=GearRatio(49.79))


def subject(g: float = 0.0, hr: int = 70) -> SubjectState:
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g),
        heart_rate=Bpm(hr),
        rr_interval=Seconds(60.0 / hr),
        steady_state=Bpm(hr),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def render(
    generator: EDAGenerator, seconds: int, *, g: float = 0.0, hr: int = 70, fs: int = FS
) -> list[float]:
    state = subject(g, hr)
    out: list[float] = []
    for second in range(seconds):
        block = generator.render(
            SignalContext(start=Monotonic(float(second)), fs=fs, count=fs, subject=state)
        )
        out.extend(float(v) for v in block)
    return out


def counts(values_us: Sequence[float]) -> list[float]:
    return [float(adc_count(Microsiemens(v))) for v in values_us]


def noisy_level(level_us: float, n: int, seed: int = 0) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 - test data, not crypto
    return counts([level_us + rng.gauss(0.0, 0.03) for _ in range(n)])


def value(reading: SensorReading, key: str) -> float | None:
    metric = reading.metric(key)
    return None if metric is None else metric.value


def process(raw: Sequence[float], fs: int = FS) -> SensorReading:
    return make().process(raw, fs, AT)


def assert_blank(reading: SensorReading) -> None:
    assert reading.quality is not SignalQuality.GOOD
    assert reading.detail
    assert [m.key for m in reading.metrics] == ["scl", "scr_rate", "scr_amplitude", "scr_count"]
    assert all(m.value is None for m in reading.metrics)


# =========================================================================
# Transfer function
# =========================================================================


def test_transfer_function_matches_the_datasheet() -> None:
    # EDA(uS) = ((ADC / 2^10) * 3.3) / 0.132
    assert VCC_V == 3.3
    assert EDA_GAIN == 0.132
    assert microsiemens(0.0) == 0.0
    assert microsiemens(512.0) == pytest.approx(12.5)
    assert microsiemens(1023.0) == pytest.approx(24.9756, abs=1e-4)
    assert microsiemens(1.0) == pytest.approx(0.024414, abs=1e-6)


@given(st.integers(min_value=0, max_value=1023))
def test_counts_round_trip_through_microsiemens(count: int) -> None:
    assert adc_count(microsiemens(float(count))) == count


def test_adc_count_clamps_and_refuses_non_finite() -> None:
    assert adc_count(Microsiemens(-3.0)) == 0
    assert adc_count(Microsiemens(80.0)) == SPEC.kind.adc_max
    assert adc_count(Microsiemens(math.nan)) == 0
    assert adc_count(Microsiemens(math.inf)) == 0


# =========================================================================
# Processor: the good path
# =========================================================================


def test_spec_and_factory() -> None:
    processor = make()
    assert isinstance(processor, EDAProcessor)
    assert processor.spec is SPEC
    assert SPEC.kind is SensorKind.EDA
    assert SPEC.kind.adc_bits == 10
    assert SPEC.unit == "uS"


def test_quiet_tonic_level_is_good_with_no_responses() -> None:
    reading = process(noisy_level(4.0, WINDOW))
    assert reading.quality is SignalQuality.GOOD
    assert reading.detail == ""
    assert reading.at == AT
    assert value(reading, "scl") == pytest.approx(4.0, abs=0.03)
    assert value(reading, "scr_count") == 0.0
    assert value(reading, "scr_rate") == 0.0
    assert value(reading, "scr_amplitude") is None
    assert reading.display_rate == SPEC.display_rate
    assert len(reading.waveform) == WINDOW // (FS // SPEC.display_rate)
    assert all(3.8 < v < 4.2 for v in reading.waveform)


def test_known_responses_are_found_with_their_amplitude() -> None:
    rng = random.Random(3)  # noqa: S311 - test data, not crypto
    onsets = (3.0, 9.0, 15.0)
    trace = [
        2.0 + sum(0.4 * scr_shape(i / FS - t0) for t0 in onsets) + rng.gauss(0.0, 0.01)
        for i in range(WINDOW)
    ]
    reading = process(counts(trace))
    assert reading.quality is SignalQuality.GOOD
    assert value(reading, "scr_count") == 3.0
    assert value(reading, "scr_rate") == pytest.approx(9.0)
    amplitude = value(reading, "scr_amplitude")
    assert amplitude is not None
    assert amplitude == pytest.approx(0.4, rel=0.15)


def test_ten_hertz_acquisition_still_filters() -> None:
    reading = process(noisy_level(5.0, 200), fs=10)
    assert reading.quality is SignalQuality.GOOD


# =========================================================================
# Processor: degraded inputs
# =========================================================================


def test_empty_window_is_no_signal() -> None:
    reading = process([])
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert reading.waveform == ()
    assert_blank(reading)


def test_flat_window_is_no_signal() -> None:
    assert_blank(process([300.0] * WINDOW))


def test_saturated_window_is_noisy() -> None:
    raw = noisy_level(20.0, WINDOW)
    raw[: WINDOW // 5] = [1023.0] * (WINDOW // 5)
    reading = process(raw)
    assert reading.quality is SignalQuality.NOISY
    assert_blank(reading)


def test_out_of_adc_range_is_no_signal() -> None:
    reading = process([*noisy_level(4.0, WINDOW - 1), 5000.0])
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert_blank(reading)


def test_nan_is_no_signal_with_no_waveform() -> None:
    raw = noisy_level(4.0, WINDOW)
    raw[500] = math.nan
    reading = process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert reading.waveform == ()
    assert_blank(reading)


def test_infinity_is_no_signal() -> None:
    reading = process([math.inf, *noisy_level(4.0, WINDOW)])
    assert reading.waveform == ()
    assert_blank(reading)


@pytest.mark.parametrize("fs", [0, -5, 1])
def test_too_low_sample_rate_is_no_signal(fs: int) -> None:
    reading = process(noisy_level(4.0, 400), fs=fs)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "frequence" in reading.detail
    assert_blank(reading)


def test_short_window_is_no_signal() -> None:
    reading = process(noisy_level(4.0, 3 * FS))
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "courte" in reading.detail
    assert reading.waveform
    assert_blank(reading)


def test_filter_refusal_is_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(x: Signal, fs: Hertz, cutoff: Hertz, order: int = 4) -> Result[Signal, DspRefused]:
        del x, fs, cutoff, order
        return Err(DspRefused("refused"))

    monkeypatch.setattr(eda_module, "lowpass", refuse)
    reading = process(noisy_level(4.0, WINDOW))
    assert "filtrage" in reading.detail
    assert_blank(reading)


def test_real_filter_refuses_a_too_short_trace() -> None:
    too_short = eda_module._filtered([1.0, 2.0, 3.0], FS, 1.0)  # pyright: ignore[reportPrivateUsage]
    assert too_short is None


def test_conductance_below_physiological_range_is_no_signal() -> None:
    # Between 0 and 3 counts (< 0.075 uS) with fractional values: not rails, not flat.
    rng = random.Random(0)  # noqa: S311 - test data, not crypto
    raw = [rng.uniform(0.5, 3.5) for _ in range(WINDOW)]
    reading = process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "plage" in reading.detail
    assert_blank(reading)


def test_electrode_coming_off_is_no_signal() -> None:
    rng = random.Random(1)  # noqa: S311 - test data, not crypto
    raw = noisy_level(3.0, WINDOW - 2 * FS)
    raw += [rng.uniform(0.5, 1.5) for _ in range(2 * FS)]  # ~0.02 uS, not exactly zero
    reading = process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "decollee" in reading.detail
    assert_blank(reading)


def test_movement_artefact_is_noisy() -> None:
    raw = noisy_level(3.0, WINDOW)
    step = counts([6.0])[0]
    raw[WINDOW // 2 :] = [step] * (WINDOW - WINDOW // 2)
    reading = process(raw)
    assert reading.quality is SignalQuality.NOISY
    assert "mouvement" in reading.detail
    assert_blank(reading)


def test_white_noise_counts_are_not_good() -> None:
    rng = random.Random(7)  # noqa: S311 - test data, not crypto
    reading = process([float(rng.randint(100, 900)) for _ in range(WINDOW)])
    assert_blank(reading)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=1200),
    st.sampled_from([-1, 0, 1, 10, 100]),
)
def test_process_is_total_on_any_floats(raw: list[float], fs: int) -> None:
    reading = process(raw, fs=fs)
    assert reading.kind is SensorKind.EDA
    if reading.quality is not SignalQuality.GOOD:
        assert all(m.value is None for m in reading.metrics)
    assert all(math.isfinite(v) for v in reading.waveform)


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.integers(min_value=0, max_value=1023), min_size=0, max_size=1200),
    st.sampled_from([10, 100]),
)
def test_process_is_total_on_any_counts(raw: list[int], fs: int) -> None:
    reading = process([float(v) for v in raw], fs=fs)
    if reading.quality is SignalQuality.GOOD:
        scl = value(reading, "scl")
        assert scl is not None
        assert scl >= MIN_SCL_US
    else:
        assert all(m.value is None for m in reading.metrics)


# =========================================================================
# The pieces, directly
# =========================================================================


def test_detect_scrs_refuses_bad_input() -> None:
    assert detect_scrs([1.0, 2.0, 1.0], 0.0) == ()
    assert detect_scrs([1.0, math.nan, 1.0, 2.0], 10.0) == ()


def test_detect_scrs_rejects_a_too_fast_rise() -> None:
    # 0.2 uS in one sample at 10 Hz: a 0.1 s rise, not a sweat gland.
    trace = [1.0] * 20 + [1.2] + [1.0] * 20
    assert detect_scrs(trace, 10.0) == ()


def test_detect_scrs_rejects_a_too_small_response() -> None:
    # Prominent against a deep trough far away, but the local rise is tiny.
    trace = [1.0, 1.1, 1.2, 1.3, 1.4, 1.39, 1.395, 1.41, 1.0, 1.0]
    found = detect_scrs(trace, 1.0)
    assert all(r.amplitude >= SCR_THRESHOLD_US for r in found)
    assert all(r.peak != 7 for r in found)


def test_detect_scrs_bounds_the_onset_by_the_previous_peak() -> None:
    fs = 10.0
    trace = [
        2.0 + 0.5 * scr_shape(i / fs - 1.0) + 0.5 * scr_shape(i / fs - 4.0) for i in range(120)
    ]
    found = detect_scrs(trace, fs)
    assert len(found) == 2
    first, second = found
    assert second.onset >= first.peak
    assert first.rise_time == pytest.approx(1.2, abs=0.15)
    assert all(r.rise_time <= MAX_RISE_S for r in found)


def test_max_slope_and_lowest_block_edge_cases() -> None:
    assert max_slope([1.0], 10.0) == 0.0
    assert max_slope([1.0, 2.0], 0.0) == 0.0
    assert max_slope([0.0, 1.0, 1.0], 10.0) == pytest.approx(10.0)
    assert lowest_block([], 10.0) == math.inf
    assert lowest_block([3.0] * 5 + [0.0] * 5, 10.0) == 0.0


# =========================================================================
# Generator
# =========================================================================


def test_generator_identity() -> None:
    generator = eda_signal.make(4)
    assert generator.kind is SensorKind.EDA
    assert generator.tonic == DEFAULT_EDA_MODEL.rest_scl
    assert generator.responses_started == 0


@pytest.mark.parametrize("count", [0, -3])
def test_generator_empty_block(count: int) -> None:
    ctx = SignalContext(start=Monotonic(0.0), fs=FS, count=count, subject=subject())
    assert EDAGenerator().render(ctx) == ()


def test_generator_zero_rate_does_not_evolve() -> None:
    generator = EDAGenerator(1)
    ctx = SignalContext(start=Monotonic(0.0), fs=0, count=50, subject=subject(3.0, 180))
    out = generator.render(ctx)
    assert len(out) == 50
    assert generator.tonic == DEFAULT_EDA_MODEL.rest_scl


def test_scr_shape_peaks_at_one_with_the_right_timing() -> None:
    assert scr_shape(0.0) == 0.0
    assert scr_shape(-1.0) == 0.0
    t_peak = math.log(2.0 / 0.75) * 0.75 * 2.0 / 1.25
    assert scr_shape(t_peak) == pytest.approx(1.0)
    assert scr_shape(t_peak - 0.2) < 1.0
    assert scr_shape(t_peak + 0.2) < 1.0
    assert scr_shape(20.0) < 1e-3


def test_targets_rise_with_load_and_cap() -> None:
    generator = EDAGenerator()

    def at(g: float, hr: int) -> tuple[float, float]:
        ctx = SignalContext(start=Monotonic(0.0), fs=FS, count=1, subject=subject(g, hr))
        tonic, rate = generator.targets(ctx)
        return float(tonic), rate

    rest = at(0.0, 70)
    assert rest == (3.0, 2.0)
    assert at(0.0, 60) == rest  # below rest does not lower it
    assert at(-1.0, 70) == rest  # nonsensical load ignored
    assert at(math.nan, 70) == rest
    loaded = at(1.0, 120)
    assert loaded[0] > rest[0]
    assert loaded[1] > rest[1]
    assert at(50.0, 200) == (20.0, 15.0)


@settings(max_examples=40, deadline=None)
@given(
    st.integers(min_value=0, max_value=2**32),
    st.floats(min_value=0.0, max_value=10.0),
    st.integers(min_value=30, max_value=220),
    st.integers(min_value=0, max_value=300),
    st.sampled_from([10, 100, 1000]),
)
def test_generator_counts_range_and_determinism(
    seed: int, g: float, hr: int, count: int, fs: int
) -> None:
    ctx = SignalContext(start=Monotonic(0.0), fs=fs, count=count, subject=subject(g, hr))
    first = EDAGenerator(seed)
    second = EDAGenerator(seed)
    for _ in range(3):
        out = first.render(ctx)
        assert out == second.render(ctx)
        assert len(out) == count
        assert all(0 <= v <= SPEC.kind.adc_max for v in out)


def test_generator_seeds_differ() -> None:
    assert render(EDAGenerator(1), 5) != render(EDAGenerator(2), 5)


def test_generator_saturates_at_full_scale() -> None:
    hot = EDAModel(rest_scl=Microsiemens(40.0), scl_max=Microsiemens(40.0))
    out = EDAGenerator(0, hot).render(
        SignalContext(start=Monotonic(0.0), fs=FS, count=FS, subject=subject())
    )
    assert set(out) == {AdcCount(SPEC.kind.adc_max)}


def test_generator_prunes_old_responses() -> None:
    frequent = EDAModel(rest_rate=60.0, rate_max=60.0)
    generator = EDAGenerator(5, frequent)
    render(generator, 120, fs=10)
    started = generator.responses_started
    assert started > 60
    # Only the last ~20 s of responses are still carried.
    assert len(generator._responses) < 40  # pyright: ignore[reportPrivateUsage]


# =========================================================================
# Round trips: generator -> processor
# =========================================================================


def windows(trace: Sequence[float], skip_s: int) -> list[SensorReading]:
    processor = make()
    return [
        processor.process(trace[start : start + WINDOW], FS, AT)
        for start in range(skip_s * FS, len(trace) - WINDOW + 1, WINDOW)
    ]


@pytest.mark.parametrize(("g", "hr"), [(0.0, 70), (1.0, 120), (2.0, 160)])
def test_round_trip_recovers_the_tonic_level(g: float, hr: int) -> None:
    generator = EDAGenerator(11)
    trace = render(generator, 240, g=g, hr=hr)
    readings = windows(trace, 140)
    assert readings
    assert all(r.quality is SignalQuality.GOOD for r in readings)
    scls = [value(r, "scl") for r in readings]
    truth = float(generator.tonic)
    for scl in scls:
        assert scl is not None
        assert scl == pytest.approx(truth, rel=0.08)


def test_round_trip_scr_rate_rises_with_stress() -> None:
    def mean_rate(g: float, hr: int) -> float:
        rates: list[float] = []
        for seed in range(3):
            readings = windows(render(EDAGenerator(seed), 400, g=g, hr=hr), 100)
            for reading in readings:
                assert reading.quality is SignalQuality.GOOD
                rate = value(reading, "scr_rate")
                assert rate is not None
                rates.append(rate)
        return sum(rates) / len(rates)

    rest = mean_rate(0.0, 70)
    stressed = mean_rate(2.0, 160)
    assert 0.5 < rest < 4.0  # model: 2 /min
    assert stressed > 3.0 * rest  # model: 15 /min (overlapping SCRs merge)


async def test_simulated_bitalino_eda_column_is_good() -> None:
    clock = ManualClock()
    client = SimulatedBitalinoClient(
        clock,
        physiology=Physiology(origin=clock.monotonic(), geometry=ARM),
        channels=[0, 1],
        sample_rate=FS,
        generators={"EDA": EDAGenerator(9)},
    )
    assert await client.connect()
    assert await client.start_acquisition()
    column: list[float] = []
    for _ in range(25):
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(FS)
        assert batch is not None
        names = [c.channel for c in batch.channels]
        assert names == ["ECG", "EDA"]
        column.extend(batch.channels[1].values)
    reading = make().process(column[-WINDOW:], FS, clock.monotonic())
    assert reading.quality is SignalQuality.GOOD, reading.detail
    scl = value(reading, "scl")
    assert scl is not None
    assert scl == pytest.approx(float(DEFAULT_EDA_MODEL.rest_scl), rel=0.1)
