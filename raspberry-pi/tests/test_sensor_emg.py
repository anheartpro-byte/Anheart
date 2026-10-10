"""The EMG sensor: transfer function, processor, generator, and the round trip between them.

The generator and the processor are written independently (the generator
re-derives the transfer function from the datasheet), so a round trip through
both is evidence about each: more g must read as more activation and more
amplitude, sustained effort must read as a falling median frequency, and a
clean simulated channel must be graded GOOD by the real processor.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src import dsp
from src.clock import ManualClock
from src.geometry import CONFIRMED_GEAR_RATIO, MachineGeometry
from src.result import Err, Ok
from src.sensors import emg as processor_module
from src.sensors.base import SensorKind, SensorReading
from src.sensors.emg import (
    LSB_MV,
    METRICS,
    MIN_FS,
    EMGProcessor,
    active_samples,
    band_edges,
    counts_to_mv,
    looks_like_ecg,
    mains_fraction,
    percentile,
    quantisation_power,
    value_or_none,
)
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.sim.signals import emg as generator_module
from src.sim.signals.base import SignalContext
from src.sim.signals.emg import EMGConfig, EMGGenerator, effort_of, to_count
from src.training.types import SignalQuality
from src.units import (
    AdcCount,
    Bpm,
    GLoad,
    Hertz,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
)

FS: Final[int] = 1000
WINDOW: Final[int] = 5 * FS
ADC_MAX: Final[int] = SensorKind.EMG.adc_max
MID: Final[int] = (ADC_MAX + 1) // 2


# =========================================================================
# Helpers
# =========================================================================


def _subject(g_load: float = 0.0, rr_interval: float = 0.8) -> SubjectState:
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g_load),
        heart_rate=Bpm(75),
        rr_interval=Seconds(rr_interval),
        steady_state=Bpm(75),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def _context(second: float, g_load: float, *, fs: int = FS, count: int = FS) -> SignalContext:
    return SignalContext(start=Monotonic(second), fs=fs, count=count, subject=_subject(g_load))


def _value(reading: SensorReading, key: str) -> float:
    metric = reading.metric(key)
    assert metric is not None, key
    assert metric.value is not None, key
    return metric.value


def _readings(
    g_load: float, seed: int, *, seconds: int = 20, config: EMGConfig | None = None
) -> list[SensorReading]:
    """One reading per second over a sliding 5 s window, after the first full one."""
    generator = EMGGenerator(seed, EMGConfig() if config is None else config)
    processor = EMGProcessor()
    raw: list[float] = []
    out: list[SensorReading] = []
    for second in range(seconds):
        raw.extend(float(v) for v in generator.render(_context(float(second), g_load)))
        raw = raw[-WINDOW:]
        if len(raw) == WINDOW:
            out.append(processor.process(raw, FS, Monotonic(float(second))))
    return out


def _good_mean(readings: Sequence[SensorReading], key: str) -> float:
    values = [_value(r, key) for r in readings if r.quality is SignalQuality.GOOD]
    assert values, key
    return statistics.fmean(values)


def _sine(freq: float, amplitude_counts: float, n: int = WINDOW) -> list[float]:
    return [
        float(round(MID + amplitude_counts * math.sin(2 * math.pi * freq * i / FS)))
        for i in range(n)
    ]


def _noise(seed: int, sd_counts: float, n: int = WINDOW) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 - test data, not crypto
    return [float(min(ADC_MAX, max(0, round(MID + rng.gauss(0.0, sd_counts))))) for _ in range(n)]


# =========================================================================
# Transfer function and the 6-bit resolution
# =========================================================================


def test_the_transfer_function_is_the_plux_one_at_six_bits() -> None:
    assert SensorKind.EMG.adc_bits == 6
    assert counts_to_mv(32) == 0.0
    assert counts_to_mv(0) == pytest.approx(-0.5 * 3.3 / 1009 * 1000)
    assert counts_to_mv(63) == pytest.approx((63 / 64 - 0.5) * 3.3 / 1009 * 1000)
    assert pytest.approx(0.05110, abs=1e-5) == LSB_MV
    assert counts_to_mv(1) - counts_to_mv(0) == pytest.approx(LSB_MV)
    assert counts_to_mv(512, bits=10) == 0.0


def test_the_generator_inverts_the_processor_transfer_function_exactly() -> None:
    assert generator_module.VCC_V == processor_module.VCC_V
    assert generator_module.GAIN == processor_module.GAIN
    for count in range(ADC_MAX + 1):
        assert to_count(counts_to_mv(count), ADC_MAX) == count
    assert to_count(100.0, ADC_MAX) == ADC_MAX
    assert to_count(-100.0, ADC_MAX) == 0
    assert to_count(math.nan, ADC_MAX) == MID


def test_the_band_respects_nyquist() -> None:
    assert band_edges(1000) == (20.0, 450.0)
    assert band_edges(500) == (20.0, 225.0)
    # White quantisation noise: LSB^2/12 times the band's share of Nyquist.
    assert quantisation_power(1000) == pytest.approx(LSB_MV**2 / 12 * 430 / 500)


# =========================================================================
# Accuracy on a signal whose answer is known
# =========================================================================


def test_a_known_sinusoid_is_measured_correctly() -> None:
    reading = EMGProcessor().process(_sine(100.0, 20.0), FS, Monotonic(5.0))
    assert reading.quality is SignalQuality.GOOD
    assert reading.detail == ""
    expected_rms = 20.0 * LSB_MV / math.sqrt(2.0)
    assert _value(reading, "rms") == pytest.approx(expected_rms, rel=0.03)
    assert _value(reading, "activation") == pytest.approx(100.0, abs=1.0)
    assert _value(reading, "median_frequency") == pytest.approx(100.0, abs=4.0)
    assert _value(reading, "peak") == pytest.approx(20.0 * LSB_MV, rel=0.08)
    assert reading.display_rate == 250
    assert len(reading.waveform) == WINDOW // 4
    assert [m.key for m in reading.metrics] == [key for key, _, _ in METRICS]


def test_a_quiet_window_has_no_median_frequency_and_little_activation() -> None:
    processor = EMGProcessor()
    assert processor.baseline_mv is None
    reading = processor.process(_noise(1, 1.0), FS, Monotonic(5.0))
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "activation") < 5.0
    metric = reading.metric("median_frequency")
    assert metric is not None
    assert metric.value is None
    assert processor.baseline_mv is not None


def test_the_remembered_baseline_only_relaxes_slowly() -> None:
    processor = EMGProcessor()
    processor.process(_noise(1, 1.0), FS, Monotonic(5.0))
    quiet = processor.baseline_mv
    assert quiet is not None
    processor.process(_sine(100.0, 20.0), FS, Monotonic(10.0))
    after = processor.baseline_mv
    assert after is not None
    assert after == pytest.approx(quiet * 1.05)
    assert processor.spec is processor_module.SPEC


def test_a_sustained_contraction_from_the_first_window_still_reads_active() -> None:
    """The baseline cap: a window without rest does not become its own baseline."""
    reading = processor_module.make().process(_sine(90.0, 12.0), FS, Monotonic(5.0))
    assert _value(reading, "activation") > 90.0


# =========================================================================
# Round trip: generator -> processor
# =========================================================================


def test_more_g_reads_as_more_activation_and_more_amplitude() -> None:
    activation: list[float] = []
    amplitude: list[float] = []
    for g in (0.0, 0.5, 1.2):
        readings = [r for seed in range(3) for r in _readings(g, seed)]
        activation.append(_good_mean(readings, "activation"))
        amplitude.append(_good_mean(readings, "rms"))
    assert activation[0] < activation[1] < activation[2]
    assert amplitude[0] < amplitude[1] < amplitude[2]
    assert activation[0] < 10.0
    assert activation[2] > 80.0


def test_the_median_frequency_falls_as_the_muscle_tires() -> None:
    generator = generator_module.make(3)
    processor = EMGProcessor()
    raw: list[float] = []
    measured: dict[int, float] = {}
    for second in range(600):
        raw.extend(float(v) for v in generator.render(_context(float(second), 1.2)))
        raw = raw[-WINDOW:]
        if second in (5, 599):
            reading = processor.process(raw, FS, Monotonic(float(second)))
            measured[second] = _value(reading, "median_frequency")
    assert generator.fatigue > 0.8
    assert measured[5] > 95.0
    assert measured[599] < 0.8 * measured[5]


def test_a_clean_generator_is_graded_good_nearly_always() -> None:
    readings = [r for g in (0.0, 0.6, 1.2) for seed in range(4) for r in _readings(g, seed)]
    good = sum(1 for r in readings if r.quality is SignalQuality.GOOD)
    assert good >= 0.97 * len(readings)


def test_the_generator_mains_is_graded_mains_dominated() -> None:
    readings = _readings(0.0, 1, seconds=8, config=EMGConfig(mains_mv=Millivolts(0.5)))
    assert all(r.quality is SignalQuality.MAINS_DOMINATED for r in readings)
    assert all(m.value is None for r in readings for m in r.metrics)
    assert "50 Hz" in readings[0].detail


def test_the_generator_ecg_crosstalk_is_graded_noisy() -> None:
    readings = _readings(0.0, 1, seconds=10, config=EMGConfig(ecg_mv=Millivolts(1.2)))
    assert all(r.quality is SignalQuality.NOISY for r in readings)
    assert "ECG" in readings[0].detail
    assert all(m.value is None for r in readings for m in r.metrics)


async def test_a_simulated_bitalino_with_this_generator_yields_a_good_emg_column() -> None:
    clock = ManualClock()
    arm = MachineGeometry(radius=Metres(1.5), ratio=CONFIRMED_GEAR_RATIO)
    client = SimulatedBitalinoClient(
        clock,
        physiology=Physiology(geometry=arm, origin=clock.monotonic()),
        channels=[0, SensorKind.EMG.channel],
        generators={"EMG": generator_module.make(7)},
    )
    assert await client.connect()
    assert await client.start_acquisition()
    client.set_motor_rpm(MotorRpm(900))
    column: list[float] = []
    for _ in range(6):
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(FS)
        assert batch is not None
        (emg_column,) = [c for c in batch.channels if c.channel == SensorKind.EMG.value]
        column.extend(emg_column.values)
    assert all(0.0 <= v <= ADC_MAX for v in column)
    reading = EMGProcessor().process(column[-WINDOW:], FS, clock.monotonic())
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert _value(reading, "rms") > 0.0


# =========================================================================
# Degraded inputs
# =========================================================================


@pytest.mark.parametrize(
    ("raw", "fs", "quality", "fragment"),
    [
        ([], FS, SignalQuality.NO_SIGNAL, "aucun"),
        ([float(MID), math.nan] * 3000, FS, SignalQuality.NO_SIGNAL, "non numeriques"),
        ([float(MID), math.inf] * 3000, FS, SignalQuality.NO_SIGNAL, "non numeriques"),
        ([float(MID), 70.0] * 3000, FS, SignalQuality.NO_SIGNAL, "hors de"),
        ([float(MID)] * WINDOW, FS, SignalQuality.NO_SIGNAL, "plat"),
        ([0.0, float(ADC_MAX)] * 2500, FS, SignalQuality.NOISY, "sature"),
        (_sine(20.0, 20.0, n=1000), 100, SignalQuality.NO_SIGNAL, "insuffisant"),
        (_sine(100.0, 20.0, n=10), FS, SignalQuality.NO_SIGNAL, "trop courte pour filtrer"),
        (_sine(100.0, 20.0, n=500), FS, SignalQuality.NO_SIGNAL, "fenetre trop courte (0.5 s"),
        (_sine(50.0, 20.0), FS, SignalQuality.MAINS_DOMINATED, "secteur"),
    ],
)
def test_degraded_windows_are_graded_and_carry_no_metrics(
    raw: list[float], fs: int, quality: SignalQuality, fragment: str
) -> None:
    reading = EMGProcessor().process(raw, fs, Monotonic(1.0))
    assert reading.quality is quality
    assert fragment in reading.detail
    assert [m.key for m in reading.metrics] == [key for key, _, _ in METRICS]
    assert all(m.value is None for m in reading.metrics)
    assert all(math.isfinite(v) for v in reading.waveform)


def test_a_slow_wander_reads_as_an_electrode_off() -> None:
    rng = random.Random(4)  # noqa: S311 - test data, not crypto
    raw = [
        float(round(MID + 25.0 * math.sin(2 * math.pi * 0.3 * i / FS) + rng.gauss(0.0, 0.4)))
        for i in range(WINDOW)
    ]
    reading = EMGProcessor().process(raw, FS, Monotonic(5.0))
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "electrode" in reading.detail


# =========================================================================
# The helpers, branch by branch
# =========================================================================


def test_value_or_none() -> None:
    assert value_or_none(Ok(Hertz(80.0))) == 80.0
    assert value_or_none(Err(dsp.DspRefused("no"))) is None


def test_mains_fraction_is_zero_without_a_spectrum_or_power() -> None:
    assert mains_fraction(dsp.as_signal([1.0, 2.0]), Hertz(1000.0), 20.0, 450.0) == 0.0
    assert mains_fraction(dsp.as_signal([0.0] * 512), Hertz(1000.0), 20.0, 450.0) == 0.0


def test_looks_like_ecg_on_each_criterion() -> None:
    assert not looks_like_ecg([0.0, 1.0], FS)
    assert not looks_like_ecg([0.0, 1.0, 0.0, 1.0, 0.0], 0)
    assert not looks_like_ecg([0.01] * 3000, FS)  # no burst height
    one_burst = [0.0] * 3000
    one_burst[1500] = 1.0
    assert not looks_like_ecg(one_burst, FS)  # too few bursts
    regular = [0.0] * WINDOW
    for beat in range(400, WINDOW, 800):
        for k in range(-40, 41):
            regular[beat + k] = 1.0 - abs(k) / 41.0
    assert looks_like_ecg(regular, FS)
    wide = [0.0] * WINDOW
    for beat in range(400, WINDOW, 800):
        for k in range(-150, 151):
            wide[beat + k] = 1.0
    assert not looks_like_ecg(wide, FS)  # too broad: contractions, not QRS


def test_active_samples_ignores_short_excursions() -> None:
    env = [0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0]
    assert active_samples(env, 0.5, 2) == 5
    assert active_samples(env, 0.5, 1) == 6
    assert active_samples([], 0.5, 1) == 0


def test_percentile() -> None:
    assert percentile([], 0.1) == 0.0
    assert percentile([3.0, 1.0, 2.0], 0.0) == 1.0
    assert percentile([3.0, 1.0, 2.0], 1.0) == 3.0


def test_min_fs_leaves_a_real_band() -> None:
    low, high = band_edges(MIN_FS)
    assert high > 10 * low


# =========================================================================
# The generator
# =========================================================================


def test_the_generator_is_deterministic_per_seed() -> None:
    def run(seed: int) -> list[AdcCount]:
        generator = EMGGenerator(seed, EMGConfig(mains_mv=Millivolts(0.1), ecg_mv=Millivolts(0.3)))
        out: list[AdcCount] = []
        for second in range(3):
            out.extend(generator.render(_context(float(second), 0.7)))
        return out

    assert run(5) == run(5)
    assert run(5) != run(6)
    assert generator_module.make().kind is SensorKind.EMG


def test_the_generator_edge_cases() -> None:
    generator = EMGGenerator(0)
    assert generator.render(_context(0.0, 1.0, count=0)) == ()
    assert generator.render(_context(0.0, 1.0, fs=0, count=5)) == (AdcCount(ADC_MAX // 2),) * 5
    instant = EMGGenerator(0, EMGConfig(fatigue_tau_s=Seconds(0.0)))
    instant.render(_context(0.0, 1.0, count=10))
    assert instant.fatigue == 1.0
    no_heart = EMGGenerator(0, EMGConfig(ecg_mv=Millivolts(1.0)))
    context = SignalContext(
        start=Monotonic(0.0), fs=FS, count=200, subject=_subject(0.0, rr_interval=0.0)
    )
    assert len(no_heart.render(context)) == 200
    # The carrier's centre never passes Nyquist, however low the rate.
    assert generator.centre_hz(100) <= 40.0


def test_effort_follows_the_load_and_is_total() -> None:
    config = EMGConfig()
    assert effort_of(GLoad(0.0), config) == 0.0
    assert effort_of(GLoad(0.5), config) == 0.5
    assert effort_of(GLoad(3.0), config) == 1.0
    assert effort_of(GLoad(-0.5), config) == 0.5
    assert effort_of(GLoad(math.nan), config) == 0.0
    assert effort_of(GLoad(1.0), EMGConfig(effort_g=GLoad(0.0))) == 0.0


@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 2**32),
    g_load=st.floats(allow_nan=True, allow_infinity=True),
    rr=st.floats(allow_nan=False, allow_infinity=False, min_value=-1.0, max_value=3.0),
    fs=st.integers(1, 2000),
    count=st.integers(-5, 400),
    mains=st.floats(0.0, 5.0),
    ecg=st.floats(0.0, 5.0),
)
def test_the_generator_always_stays_within_the_channel(
    *, seed: int, g_load: float, rr: float, fs: int, count: int, mains: float, ecg: float
) -> None:
    generator = EMGGenerator(seed, EMGConfig(mains_mv=Millivolts(mains), ecg_mv=Millivolts(ecg)))
    context = SignalContext(start=Monotonic(0.0), fs=fs, count=count, subject=_subject(g_load, rr))
    samples = generator.render(context)
    assert len(samples) == max(0, count)
    assert all(0 <= s <= ADC_MAX for s in samples)


# =========================================================================
# Totality of the processor
# =========================================================================


@settings(max_examples=80, deadline=None)
@given(
    raw=st.one_of(
        st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=1500),
        st.lists(st.integers(0, ADC_MAX).map(float), min_size=0, max_size=3000),
    ),
    fs=st.sampled_from([-1, 0, 1, 10, 100, 500, 1000]),
)
def test_the_processor_is_total(raw: list[float], fs: int) -> None:
    reading = EMGProcessor().process(raw, fs, Monotonic(0.0))
    assert reading.kind is SensorKind.EMG
    assert [m.key for m in reading.metrics] == [key for key, _, _ in METRICS]
    if reading.quality is not SignalQuality.GOOD:
        assert all(m.value is None for m in reading.metrics)
        assert reading.detail
    assert all(math.isfinite(v) for v in reading.waveform)
    for metric in reading.metrics:
        assert metric.value is None or math.isfinite(metric.value)
