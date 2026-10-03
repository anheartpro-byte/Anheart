"""The ECG sensor processor (``src/sensors/ecg.py``): the display-only heart rate.

Signals come from the real synthesiser (``src/sim/ecg.py``), so the processor
is handed exactly the RAW 10-bit counts a BITalino would send, contaminants
included. Its heart rate is checked against the synthesiser's ground truth and
against the legacy BioSPPy path that drives the motor (``src/ecg_pipeline.py``
through ``src/signal_processing.py``), on the very same samples.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src import dsp
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import (
    ECG_CHANNEL,
    HEART_RATE_KEY,
    QUALITY_KEY,
    EcgBridge,
    EcgFrame,
    Treatment,
    load_treatment,
    treat_ecg,
)
from src.result import Err, Ok, Result
from src.sensors import ecg
from src.sensors.base import SensorKind, SensorReading, decimate
from src.sim.ecg import ECG_MV_PER_COUNT, EcgConfig, EcgSynthesizer
from src.sim.physiology import ScriptedEvent, SubjectState
from src.training.types import SignalQuality
from src.units import (
    Bpm,
    GLoad,
    Hertz,
    Millivolts,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
    UnixMillis,
)

FS: int = 1000
WINDOW: int = round(float(ecg.SPEC.window_s) * FS)
AT: Monotonic = Monotonic(12.5)


def _subject(
    bpm: float = 70.0,
    *,
    g_load: float = 0.0,
    artifacts: frozenset[ScriptedEvent] = frozenset(),
) -> SubjectState:
    """A hand-built subject: only ``rr_interval``, ``g_load`` and ``artifacts`` are rendered."""
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g_load),
        heart_rate=Bpm(round(bpm)),
        rr_interval=Seconds(60.0 / bpm),
        steady_state=Bpm(round(bpm)),
        drift_bpm=0.0,
        artifacts=artifacts,
    )


def _render(
    bpm: float = 70.0,
    *,
    count: int = WINDOW,
    g_load: float = 0.0,
    artifacts: frozenset[ScriptedEvent] = frozenset(),
    config: EcgConfig | None = None,
) -> list[float]:
    synth = EcgSynthesizer(config if config is not None else EcgConfig())
    return [
        float(v) for v in synth.render(count, _subject(bpm, g_load=g_load, artifacts=artifacts))
    ]


def _process(raw: Sequence[float], fs: int = FS) -> SensorReading:
    return ecg.make().process(raw, fs, AT)


def _value(reading: SensorReading, key: str) -> float:
    metric = reading.metric(key)
    assert metric is not None, key
    assert metric.value is not None, (key, reading.quality, reading.detail)
    return metric.value


def _legacy(raw: Sequence[float]) -> tuple[str, int | None]:
    """The motor path's (quality, heartRate) for the same window, through its typed loader."""
    treatment = load_treatment(FS, ecg.SPEC.display_rate)
    _treated, metrics = treatment.treat_batch([{"channel": ECG_CHANNEL, "values": list(raw)}])
    found = metrics.get(ECG_CHANNEL, {})
    quality = found.get(QUALITY_KEY)
    rate = found.get(HEART_RATE_KEY)
    return (
        quality if isinstance(quality, str) else "",
        rate if isinstance(rate, int) and not isinstance(rate, bool) else None,
    )


def _handed(raw: Sequence[float]) -> list[tuple[SignalQuality, Bpm | None]]:
    """What the RUNTIME is handed for ``raw``, streamed in 200 ms batches through the bridge.

    The real legacy DSP and this processor as the confirmation, both inline.
    """

    class _Source:
        def __init__(self) -> None:
            self.batches: list[SampleBatch] = [
                SampleBatch(
                    timestamp=UnixMillis(i * 200),
                    channels=[ChannelData(ECG_CHANNEL, list(raw[i * 200 : (i + 1) * 200]))],
                )
                for i in range(len(raw) // 200)
            ]

        async def read_samples(self, count: int = 1000) -> SampleBatch | None:  # noqa: ARG002
            return self.batches.pop(0) if self.batches else None

    class _Runtime:
        def __init__(self) -> None:
            self.seen: list[tuple[SignalQuality, Bpm | None]] = []

        def observe_ecg(
            self, now: Monotonic, seq: int, quality: SignalQuality, heart_rate: Bpm | None
        ) -> object:
            del now, seq
            self.seen.append((quality, heart_rate))
            return None

    class _Ring:
        def record_ecg(self, values: Sequence[Millivolts]) -> int:
            return len(values)

    async def treat(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
        return treat_ecg(treatment, batch)

    async def confirm(window: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        return ecg.make().process(window, fs, at)

    runtime = _Runtime()
    bridge = EcgBridge(
        clock=ManualClock(),
        source=_Source(),
        treatment=load_treatment(FS, ecg.SPEC.display_rate),
        heart_rate=runtime,
        waveform=_Ring(),
        sample_rate=FS,
        treat=treat,
        max_batches=1000,
        confirm=confirm,
    )
    asyncio.run(bridge.pump())
    assert len(runtime.seen) == len(raw) // 200
    return runtime.seen


def _white_noise(*, seed: int) -> list[float]:
    """Gaussian counts around mid-scale, sd 60: no heart at all."""
    noise = np.random.default_rng(seed).normal(512.0, 60.0, WINDOW)
    return [min(1023.0, max(0.0, float(round(v)))) for v in dsp.values_of(noise)]


def _assert_blank(reading: SensorReading) -> None:
    assert reading.quality is not SignalQuality.GOOD
    assert reading.detail
    assert [m.key for m in reading.metrics] == [key for key, _, _ in ecg.METRICS]
    assert all(m.value is None for m in reading.metrics)


# =========================================================================
# Spec, transfer function, wiring
# =========================================================================


def test_spec_and_factory() -> None:
    processor = ecg.make()
    assert isinstance(processor, ecg.ECGProcessor)
    assert processor.spec is ecg.SPEC
    assert ecg.SPEC.kind is SensorKind.ECG
    assert ecg.SPEC.unit == "mV"
    assert ecg.ADC_LEVELS == 1024


def test_transfer_function_matches_the_datasheet_and_the_simulator() -> None:
    # ECG(mV) = ((ADC / 2^n - 1/2) * VCC) / G_ECG * 1000
    assert ecg.adc_to_millivolts(512.0) == 0.0
    assert ecg.adc_to_millivolts(0.0) == pytest.approx(-0.5 * 3.3 / 1100 * 1000)
    assert ecg.adc_to_millivolts(1024.0) == pytest.approx(0.5 * 3.3 / 1100 * 1000)
    slope = ecg.adc_to_millivolts(513.0) - ecg.adc_to_millivolts(512.0)
    assert slope == pytest.approx(ECG_MV_PER_COUNT, rel=1e-12)


def test_the_reading_carries_its_identity() -> None:
    reading = _process(_render())
    assert reading.kind is SensorKind.ECG
    assert reading.at == AT
    assert reading.display_rate == ecg.SPEC.display_rate


# =========================================================================
# Accuracy against ground truth and against the legacy (motor) path
# =========================================================================


@pytest.mark.parametrize("bpm", [50.0, 70.0, 120.0, 150.0, 190.0])
def test_recovers_the_rendered_heart_rate(bpm: float) -> None:
    reading = _process(_render(bpm))
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert reading.detail == ""
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(bpm, abs=2.0)
    assert _value(reading, ecg.RR_MEAN) == pytest.approx(60_000.0 / bpm, rel=0.03)
    beats = _value(reading, ecg.BEATS)
    assert abs(beats - bpm / 60.0 * 10.0) <= 1.0
    # 3 % respiratory sinus arrhythmia: a real, small, positive HRV.
    assert 0.0 < _value(reading, ecg.RMSSD) < 60.0


@pytest.mark.parametrize("bpm", [50.0, 70.0, 120.0, 150.0, 190.0])
def test_a_metronome_is_measured_exactly(bpm: float) -> None:
    reading = _process(_render(bpm, config=EcgConfig(rr_variability=0.0)))
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(bpm, abs=1.0)
    assert _value(reading, ecg.RMSSD) < 3.0


@pytest.mark.parametrize("bpm", [30.0, 220.0])
def test_the_edges_of_the_plausible_range_are_measured(bpm: float) -> None:
    reading = _process(_render(bpm))
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(bpm, abs=2.0)


def test_another_sample_rate_is_measured() -> None:
    raw = _render(80.0, count=5000, config=EcgConfig(sample_rate=500))
    reading = _process(raw, fs=500)
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(80.0, abs=2.0)
    assert len(reading.waveform) == len(decimate(raw, 500, ecg.SPEC.display_rate))


@pytest.mark.parametrize("bpm", [50.0, 70.0, 120.0, 150.0, 190.0])
def test_agrees_with_the_legacy_motor_path(bpm: float) -> None:
    raw = _render(bpm)
    reading = _process(raw)
    quality, legacy_bpm = _legacy(raw)
    assert quality == "good"
    assert legacy_bpm is not None
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(legacy_bpm, abs=3.0)


# These two were strict xfails against the legacy motor path alone: it grades
# white noise 'good' at ~133 bpm, and a 60 bpm subject under 0.2 g of motion
# artefact 'good' at 124-140 bpm. The legacy grader still does (and this
# processor still grades both 'noisy'); what changed is that src/ecg_pipeline.py
# now hands the runtime a rate only when THIS processor confirms it on the same
# window. So the assertion moved to what the motor path actually acts on.


def test_white_noise_never_reaches_the_motor_path_as_a_rate() -> None:
    raw = _white_noise(seed=1)
    assert _process(raw).quality is SignalQuality.NOISY
    assert all(bpm is None for _quality, bpm in _handed(raw))


def test_the_motor_path_is_right_or_silent_under_motion() -> None:
    raw = _render(60.0, g_load=0.2)
    assert _process(raw).quality is SignalQuality.NOISY
    for quality, bpm in _handed(raw):
        assert quality is not SignalQuality.GOOD or bpm is None or abs(bpm - 60) <= 5


def test_light_motion_is_tolerated() -> None:
    reading = _process(_render(60.0, g_load=0.1))
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(60.0, abs=2.0)


# =========================================================================
# Degraded inputs: never a number, always a reason
# =========================================================================


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ([], "aucun echantillon"),
        ([512.0], "plat"),
        ([512.0] * WINDOW, "plat"),
        ([2000.0, 500.0, 10.0] * 100, "hors de 0..1023"),
        ([-5.0, 500.0, 10.0] * 100, "hors de 0..1023"),
        ([500.0, math.nan, 10.0] * 100, "non finies"),
        ([math.nan, 500.0, 10.0] * 100, "non finies"),
        ([500.0, math.inf, 10.0] * 100, "non finies"),
        ([500.0, -math.inf, 10.0] * 100, "non finies"),
    ],
)
def test_no_signal_inputs(raw: list[float], fragment: str) -> None:
    reading = _process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert fragment in reading.detail
    _assert_blank(reading)
    assert all(math.isfinite(v) for v in reading.waveform)
    assert len(reading.waveform) == len(decimate(raw, FS, ecg.SPEC.display_rate))


def test_a_corrupt_frame_is_drawn_clamped_and_at_zero_for_nan() -> None:
    reading = _process([2000.0, -5.0, math.nan] * 4, fs=ecg.SPEC.display_rate)
    top = ecg.adc_to_millivolts(1023.0)
    bottom = ecg.adc_to_millivolts(0.0)
    assert reading.waveform[:3] == (top, bottom, 0.0)


def test_a_saturated_front_end_is_noisy() -> None:
    reading = _process(_render(70.0, config=EcgConfig(baseline_mv=3.0)))
    assert reading.quality is SignalQuality.NOISY
    assert "sature" in reading.detail
    _assert_blank(reading)


def test_mains_hum_dominates() -> None:
    raw = _render(70.0, artifacts=frozenset({ScriptedEvent.MAINS_BURST}))
    reading = _process(raw)
    assert reading.quality is SignalQuality.MAINS_DOMINATED
    assert "50 Hz" in reading.detail
    _assert_blank(reading)
    assert _legacy(raw)[0] == "mains_dominated"


def test_white_noise_is_noisy() -> None:
    raw = _white_noise(seed=7)
    reading = _process(raw)
    assert reading.quality is SignalQuality.NOISY
    _assert_blank(reading)


@pytest.mark.parametrize("g_load", [0.2, 0.3, 0.86])
def test_motion_artefact_is_noisy(g_load: float) -> None:
    reading = _process(_render(60.0, g_load=g_load))
    assert reading.quality is SignalQuality.NOISY
    _assert_blank(reading)


def test_an_electrode_coming_off_mid_window_is_noisy() -> None:
    synth = EcgSynthesizer(EcgConfig())
    on = synth.render(WINDOW // 2, _subject(70.0))
    off = synth.render(
        WINDOW // 2, _subject(70.0, artifacts=frozenset({ScriptedEvent.ELECTRODE_OFF}))
    )
    reading = _process([float(v) for v in on + off])
    assert reading.quality is SignalQuality.NOISY
    assert "manquants" in reading.detail
    _assert_blank(reading)


def test_an_ectopic_beat_is_noisy() -> None:
    synth = EcgSynthesizer(EcgConfig())
    before = synth.render(4000, _subject(70.0))
    # The phase races for 250 ms: the next R arrives far too early (a premature beat).
    premature = synth.render(250, _subject(200.0))
    after = synth.render(WINDOW - 4250, _subject(70.0))
    reading = _process([float(v) for v in before + premature + after])
    assert reading.quality is SignalQuality.NOISY
    assert "irregulier" in reading.detail
    _assert_blank(reading)


def test_too_few_beats_is_noisy() -> None:
    reading = _process(_render(12.0))
    assert reading.quality is SignalQuality.NOISY
    assert "trop peu" in reading.detail
    _assert_blank(reading)


def test_one_beat_then_the_electrode_off_is_too_few_beats() -> None:
    synth = EcgSynthesizer(EcgConfig())
    on = synth.render(1500, _subject(60.0))
    off = synth.render(
        WINDOW - 1500, _subject(60.0, artifacts=frozenset({ScriptedEvent.ELECTRODE_OFF}))
    )
    reading = _process([float(v) for v in on + off])
    assert reading.quality is SignalQuality.NOISY
    assert "trop peu" in reading.detail


def test_an_implausibly_slow_rate_is_noisy() -> None:
    reading = _process(_render(25.0))
    assert reading.quality is SignalQuality.NOISY
    assert "implausible" in reading.detail
    _assert_blank(reading)


def test_a_short_window_is_not_judged() -> None:
    reading = _process(_render(70.0, count=2000))
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "trop courte" in reading.detail
    _assert_blank(reading)
    assert len(reading.waveform) == 2000 // (FS // ecg.SPEC.display_rate)


@pytest.mark.parametrize(("count", "fs"), [(20, FS), (WINDOW, 0), (WINDOW, -3), (WINDOW, 50)])
def test_a_window_the_filter_refuses(count: int, fs: int) -> None:
    raw = _render(70.0)[:count]
    reading = _process(raw, fs=fs)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "filtrage impossible" in reading.detail
    _assert_blank(reading)
    assert len(reading.waveform) == len(decimate(raw, fs, ecg.SPEC.display_rate))


# =========================================================================
# Helpers, directly
# =========================================================================


def test_both_returns_the_first_error() -> None:
    ok: Result[int, str] = Ok(1)
    first: Result[int, str] = Err("first")
    second: Result[str, str] = Err("second")
    assert ecg.both(ok, Ok("x")) == Ok((1, "x"))
    assert ecg.both(first, second) == Err("first")
    assert ecg.both(ok, second) == Err("second")


def test_detect_beats_is_total_on_degenerate_input() -> None:
    fs = Hertz(float(FS))
    empty = dsp.as_signal([])
    assert ecg.detect_beats(empty, empty, fs) == ecg.Beats(peaks=(), rr_s=())
    short = dsp.as_signal([1.0, 2.0])
    assert ecg.detect_beats(short, short, fs).peaks == ()
    mismatched = dsp.as_signal([0.0] * 10)
    assert ecg.detect_beats(mismatched[:5], mismatched, fs).peaks == ()


def test_heart_metrics_arithmetic() -> None:
    found = ecg.heart_metrics(ecg.Beats(peaks=(0, 1000, 1900, 2900), rr_s=(1.0, 0.9, 1.0)))
    assert found.beats == 4
    assert found.heart_rate == pytest.approx(60.0)
    assert found.rr_mean_ms == pytest.approx(2900.0 / 3.0)
    assert found.rmssd_ms == pytest.approx(100.0)


def test_mains_ratio_is_defined_without_power() -> None:
    silent = dsp.Spectrum(frequencies=dsp.as_signal([0.0, 50.0]), power=dsp.as_signal([0.0, 0.0]))
    assert ecg.mains_ratio(silent) == 0.0
    hum = dsp.Spectrum(
        frequencies=dsp.as_signal([0.0, 10.0, 50.0]), power=dsp.as_signal([9.0, 1.0, 3.0])
    )
    assert ecg.mains_ratio(hum) == pytest.approx(0.75)


# =========================================================================
# Properties
# =========================================================================

_ANY_FLOATS = st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=3000)
_COUNTS = st.lists(st.integers(min_value=0, max_value=1023).map(float), max_size=6000)
_RATES = st.sampled_from([-1, 0, 1, 7, 100, 250, 333, 500, 1000, 2000])


def _check_total(reading: SensorReading, raw: Sequence[float], fs: int) -> None:
    assert reading.kind is SensorKind.ECG
    if reading.quality is not SignalQuality.GOOD:
        assert all(m.value is None for m in reading.metrics)
        assert reading.detail
    assert all(m.value is None or math.isfinite(m.value) for m in reading.metrics)
    assert all(math.isfinite(v) for v in reading.waveform)
    assert len(reading.waveform) == len(decimate(raw, fs, ecg.SPEC.display_rate))


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(raw=_ANY_FLOATS, fs=_RATES)
def test_process_is_total_on_any_floats(raw: list[float], fs: int) -> None:
    _check_total(_process(raw, fs), raw, fs)


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(raw=_COUNTS, fs=_RATES)
def test_process_is_total_on_valid_counts(raw: list[float], fs: int) -> None:
    _check_total(_process(raw, fs), raw, fs)


@settings(deadline=None, max_examples=25, suppress_health_check=[HealthCheck.too_slow])
@given(
    bpm=st.floats(min_value=40.0, max_value=200.0),
    offset=st.integers(min_value=0, max_value=3000),
)
def test_a_clean_rendered_rate_is_recovered(bpm: float, offset: int) -> None:
    """Any rate, any alignment of the window on the beats.

    Without sinus arrhythmia: with it the "true" rate of a 10 s window is itself
    spread over +/- 3 %, and the median RR of 2.5 breathing cycles lands
    anywhere in that spread (179 bpm reads 177 with it, exactly 179 without).
    """
    raw = _render(bpm, count=WINDOW + offset, config=EcgConfig(rr_variability=0.0))
    reading = _process(raw[offset:])
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert _value(reading, ecg.HEART_RATE) == pytest.approx(bpm, abs=2.0)
    assert np.isfinite(_value(reading, ecg.RMSSD))
