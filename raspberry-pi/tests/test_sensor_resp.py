"""RESP: the PZT transfer function, the breathing model and the processor, end to end.

The round trip is the main evidence: the generator breathes at a known rate,
the processor must recover it within one breath per minute, and the degraded
inputs (band off, motion, apnea, corrupt frames) must each be named.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src import dsp
from src.clock import ManualClock
from src.geometry import MachineGeometry
from src.sensors.base import SensorKind, SensorReading
from src.sensors.resp import (
    MIN_AMPLITUDE,
    SPEC,
    BreathsPerMinute,
    Percent,
    RESPProcessor,
    block_means,
    make,
    percentile,
    spectral_rate,
    to_count,
    to_percent,
)
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.sim.signals import resp as sim_resp
from src.sim.signals.base import SignalContext
from src.sim.signals.resp import Apnea, RespConfig, RESPGenerator, breath_shape
from src.training.types import SignalQuality
from src.units import Bpm, GearRatio, GLoad, Hertz, Metres, Monotonic, MotorRpm, Seconds

ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.5), ratio=GearRatio(49.79))
FS: Final[int] = 1000
WINDOW: Final[int] = round(SPEC.window_s * FS)


def _rest() -> SubjectState:
    return Physiology(geometry=ARM, origin=Monotonic(0.0)).advance(Monotonic(1.0), MotorRpm(0))


def _render(
    generator: RESPGenerator, seconds: int, subject: SubjectState | None = None, fs: int = FS
) -> list[float]:
    state = _rest() if subject is None else subject
    out: list[float] = []
    for k in range(seconds):
        context = SignalContext(start=Monotonic(float(k)), fs=fs, count=fs, subject=state)
        out.extend(float(v) for v in generator.render(context))
    return out


def _fixed_rate(rate: float, sigh_probability: float = 0.02) -> RespConfig:
    """A subject breathing at ``rate`` whatever the load."""
    return RespConfig(
        rest_rate=BreathsPerMinute(rate),
        rate_per_bpm=0.0,
        rate_per_g=0.0,
        rate_floor=BreathsPerMinute(1.0),
        rate_ceiling=BreathsPerMinute(100.0),
        sigh_probability=sigh_probability,
    )


def _value(reading: SensorReading, key: str) -> float | None:
    metric = reading.metric(key)
    assert metric is not None, key
    return metric.value


def _process(raw: Sequence[float], fs: int = FS) -> SensorReading:
    return make().process(raw, fs, Monotonic(100.0))


def _counts(percent: Sequence[float]) -> list[float]:
    return [float(to_count(Percent(p))) for p in percent]


# =========================================================================
# Transfer function
# =========================================================================


def test_transfer_function_matches_the_plux_datasheet() -> None:
    assert to_percent(0.0) == -50.0
    assert to_percent(512.0) == 0.0
    assert to_percent(1024.0) == 50.0
    assert to_percent(768.0) == 25.0


@given(st.integers(min_value=0, max_value=1023))
def test_transfer_function_round_trips_every_count(count: int) -> None:
    assert to_count(to_percent(float(count))) == count


def test_inverse_clamps_to_the_channel() -> None:
    assert to_count(Percent(80.0)) == 1023
    assert to_count(Percent(-80.0)) == 0


def test_spec_is_the_resp_channel() -> None:
    processor = RESPProcessor()
    assert processor.spec is SPEC
    assert SPEC.kind is SensorKind.RESP
    assert SensorKind.RESP.adc_bits == 10


# =========================================================================
# Round trip: generator -> processor
# =========================================================================


@pytest.mark.parametrize("rate", [8.0, 12.0, 20.0, 30.0, 40.0])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_round_trip_recovers_the_rate(rate: float, seed: int) -> None:
    raw = _render(RESPGenerator(seed, _fixed_rate(rate)), 60)
    reading = _process(raw[-WINDOW:])
    assert reading.quality is SignalQuality.GOOD, reading.detail
    assert reading.detail == ""
    measured = _value(reading, "resp_rate")
    assert measured is not None
    assert abs(measured - rate) <= 1.0
    amplitude = _value(reading, "amplitude")
    assert amplitude is not None
    assert 7.0 <= amplitude <= 13.0  # rest_depth 10 % peak-to-peak, jittered
    regularity = _value(reading, "regularity")
    assert regularity is not None
    assert 0.8 <= regularity <= 1.0
    apnea = _value(reading, "apnea_s")
    assert apnea is not None
    assert apnea < 60.0 / rate + 1.5


def test_round_trip_at_a_lower_acquisition_rate() -> None:
    raw = _render(RESPGenerator(0, _fixed_rate(15.0)), 40, fs=100)
    reading = _process(raw[-3000:], fs=100)
    assert reading.quality is SignalQuality.GOOD
    measured = _value(reading, "resp_rate")
    assert measured is not None
    assert abs(measured - 15.0) <= 1.0
    assert len(reading.waveform) == 1500  # 30 s at display_rate 50


def test_breathing_speeds_up_and_deepens_with_effort() -> None:
    rest = _process(_render(RESPGenerator(3), 60)[-WINDOW:])
    loaded_state = dataclasses.replace(_rest(), heart_rate=Bpm(150), g_load=GLoad(1.2))
    loaded = _process(_render(RESPGenerator(3), 60, loaded_state)[-WINDOW:])
    assert rest.quality is SignalQuality.GOOD
    assert loaded.quality is SignalQuality.GOOD
    rest_rate, loaded_rate = _value(rest, "resp_rate"), _value(loaded, "resp_rate")
    rest_amp, loaded_amp = _value(rest, "amplitude"), _value(loaded, "amplitude")
    assert rest_rate is not None
    assert loaded_rate is not None
    assert rest_amp is not None
    assert loaded_amp is not None
    assert loaded_rate > rest_rate + 10.0
    assert loaded_amp > 1.5 * rest_amp


def test_target_rate_follows_heart_rate_and_g_then_clamps() -> None:
    generator = RESPGenerator()
    assert generator.target_rate(Bpm(70), 0.0) == 12.0
    assert generator.target_rate(Bpm(120), 1.0) == pytest.approx(12.0 + 9.0 + 2.0)
    assert generator.target_rate(Bpm(300), 5.0) == 40.0
    assert generator.target_rate(Bpm(20), 0.0) == 6.0


def test_breath_shape_is_asymmetric_and_continuous() -> None:
    assert breath_shape(0.0, 0.4) == pytest.approx(0.0)
    assert breath_shape(0.4, 0.4) == pytest.approx(1.0)
    assert breath_shape(1.0, 0.4) == pytest.approx(0.0)
    # Inhalation (0.4 of the cycle) is faster than exhalation (0.6).
    assert breath_shape(0.2, 0.4) == pytest.approx(0.5)
    assert breath_shape(0.7, 0.4) == pytest.approx(0.5)


def test_sighs_are_deeper_breaths_and_still_good() -> None:
    config = _fixed_rate(12.0, sigh_probability=1.0)
    generator = RESPGenerator(0, config)
    raw = _render(generator, 60)
    percent = [to_percent(v) for v in raw]
    assert max(percent) - min(percent) > 20.0  # 2.5 x the 10 % rest depth
    reading = _process(raw[-WINDOW:])
    assert reading.quality is SignalQuality.GOOD
    measured = _value(reading, "resp_rate")
    assert measured is not None
    assert abs(measured - 12.0 / 1.5) <= 1.0  # every cycle a sigh: 1.5 x longer


# =========================================================================
# Apnea
# =========================================================================


def test_scripted_apnea_is_graded_and_named() -> None:
    config = RespConfig(apneas=(Apnea(start=Monotonic(40.0), duration=Seconds(14.0)),))
    raw = _render(RESPGenerator(0, config), 60)
    reading = _process(raw[-WINDOW:])
    assert reading.quality is SignalQuality.NOISY
    assert reading.detail.startswith("apnee ")
    assert reading.detail.endswith(" s")
    apnea = _value(reading, "apnea_s")
    assert apnea is not None
    assert 13.0 <= apnea <= 20.0
    assert _value(reading, "resp_rate") is None
    assert _value(reading, "amplitude") is None
    assert _value(reading, "regularity") is None


def test_an_apnea_in_progress_is_seen_growing() -> None:
    config = RespConfig(apneas=(Apnea(start=Monotonic(45.0), duration=Seconds(60.0)),))
    raw = _render(RESPGenerator(0, config), 60)
    reading = _process(raw[-WINDOW:])
    assert reading.quality is SignalQuality.NOISY
    apnea = _value(reading, "apnea_s")
    assert apnea is not None
    assert apnea >= 12.0


def test_apnea_window_bounds() -> None:
    pause = Apnea(start=Monotonic(10.0), duration=Seconds(5.0))
    assert pause.covers(10.0)
    assert pause.covers(14.9)
    assert not pause.covers(15.0)
    assert not pause.covers(9.9)


def test_a_short_pause_is_not_an_apnea() -> None:
    config = RespConfig(apneas=(Apnea(start=Monotonic(45.0), duration=Seconds(3.0)),))
    reading = _process(_render(RESPGenerator(0, config), 60)[-WINDOW:])
    assert reading.quality is SignalQuality.GOOD
    apnea = _value(reading, "apnea_s")
    assert apnea is not None
    assert apnea <= 10.0


# =========================================================================
# Degraded inputs
# =========================================================================


def _all_blank(reading: SensorReading) -> bool:
    return all(metric.value is None for metric in reading.metrics)


def test_empty_window_is_no_signal() -> None:
    reading = _process([])
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert _all_blank(reading)
    assert reading.waveform == ()


def test_flat_band_is_no_signal() -> None:
    reading = _process([512.0] * WINDOW)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "plat" in reading.detail
    assert _all_blank(reading)


def test_saturated_band_is_noisy() -> None:
    raw = [0.0 if (i // 500) % 2 else 1023.0 for i in range(WINDOW)]
    reading = _process(raw)
    assert reading.quality is SignalQuality.NOISY
    assert "sature" in reading.detail
    assert _all_blank(reading)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_samples_are_a_corrupt_frame(bad: float) -> None:
    raw = [512.0 + 50.0 * math.sin(i / 300.0) for i in range(WINDOW)]
    raw[1234] = bad
    reading = _process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "non numeriques" in reading.detail
    assert reading.waveform == ()
    assert _all_blank(reading)


@pytest.mark.parametrize("fs", [0, -1000])
def test_non_positive_rate_is_refused(fs: int) -> None:
    reading = make().process([512.0, 600.0], fs, Monotonic(0.0))
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert _all_blank(reading)


def test_short_window_is_no_signal() -> None:
    raw = _render(RESPGenerator(), 5)
    reading = _process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "trop courte" in reading.detail
    assert len(reading.waveform) > 0


def test_motion_artefact_is_noisy() -> None:
    state = dataclasses.replace(_rest(), g_load=GLoad(2.0))
    config = RespConfig(motion_per_g=Percent(10.0))
    reading = _process(_render(RESPGenerator(0, config), 40, state)[-WINDOW:])
    assert reading.quality is SignalQuality.NOISY
    assert reading.detail == "artefact de mouvement"
    assert _all_blank(reading)


def test_worn_band_without_breathing_is_no_oscillation() -> None:
    raw = [400.0 + 200.0 * i / WINDOW for i in range(WINDOW)]  # slow postural drift
    reading = _process(raw)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert "aucune oscillation" in reading.detail
    assert _all_blank(reading)


def _bumps(centres: Sequence[float], seconds: float, fs: int, height: float) -> list[float]:
    return _counts(
        [
            height * sum(math.exp(-(((i / fs) - c) ** 2) / (2 * 1.2**2)) for c in centres)
            for i in range(round(seconds * fs))
        ]
    )


def test_rate_below_range_keeps_the_pause_visible() -> None:
    reading = _process(_bumps((5.0, 25.0), 30.0, 100, 20.0), fs=100)
    assert reading.quality is SignalQuality.NOISY
    assert "hors de 4-60/min" in reading.detail
    assert _value(reading, "resp_rate") is None
    apnea = _value(reading, "apnea_s")
    assert apnea is not None
    assert apnea == pytest.approx(20.0, abs=0.5)


def test_cycles_disagreeing_with_the_spectrum_are_not_trusted() -> None:
    # Panting at 54/min: faster than the 1.4 s breath spacing lets the cycle
    # count see, so the cycle count reads about half of what the spectrum does.
    raw = _counts([10.0 * math.sin(2 * math.pi * 0.9 * i / 100) for i in range(3000)])
    reading = _process(raw, fs=100)
    assert reading.quality is SignalQuality.NOISY
    assert "desaccord" in reading.detail
    assert _value(reading, "resp_rate") is None
    assert _value(reading, "apnea_s") is not None


def test_band_pass_refused_at_an_absurd_rate() -> None:
    raw = [512.0 + 100.0 * math.sin(i) for i in range(30)]
    reading = _process(raw, fs=1)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert reading.detail.startswith("filtrage impossible")


def test_motion_filter_refused_when_nyquist_is_below_its_cut_off() -> None:
    raw = [512.0 + 100.0 * math.sin(i / 2.0) for i in range(90)]
    reading = _process(raw, fs=3)
    assert reading.quality is SignalQuality.NO_SIGNAL
    assert reading.detail.startswith("filtrage impossible")


def test_spectral_rate_of_silence_is_none() -> None:
    assert spectral_rate(dsp.as_signal([0.0] * 100), Hertz(25.0)) is None
    tone = [math.sin(2 * math.pi * 0.25 * i / 25) for i in range(750)]
    found = spectral_rate(dsp.as_signal(tone), Hertz(25.0))
    assert found == pytest.approx(15.0, abs=1.0)


def test_helpers() -> None:
    assert block_means([1.0, 3.0, 5.0, 7.0, 9.0], 2) == [2.0, 6.0]
    assert percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0], 1.0) == 3.0
    assert MIN_AMPLITUDE > 0.0


# =========================================================================
# Totality and determinism
# =========================================================================


@settings(max_examples=150, deadline=None)
@given(
    raw=st.lists(
        st.one_of(
            st.floats(allow_nan=True, allow_infinity=True),
            st.floats(min_value=0.0, max_value=1023.0),
        ),
        max_size=400,
    ),
    fs=st.integers(min_value=-5, max_value=2000),
)
def test_process_is_total(raw: list[float], fs: int) -> None:
    reading = make().process(raw, fs, Monotonic(0.0))
    assert reading.kind is SensorKind.RESP
    assert [m.key for m in reading.metrics] == ["resp_rate", "amplitude", "regularity", "apnea_s"]
    if reading.quality is not SignalQuality.GOOD:
        assert reading.detail != ""
    for metric in reading.metrics:
        assert metric.value is None or math.isfinite(metric.value)


@settings(max_examples=40, deadline=None)
@given(
    raw=st.lists(st.floats(min_value=0.0, max_value=1023.0), min_size=250, max_size=1500),
    fs=st.sampled_from([25, 50, 100]),
)
def test_process_is_total_on_long_plausible_windows(raw: list[float], fs: int) -> None:
    reading = make().process(raw, fs, Monotonic(0.0))
    for metric in reading.metrics:
        assert metric.value is None or math.isfinite(metric.value)
    if reading.quality is SignalQuality.GOOD:
        rate = _value(reading, "resp_rate")
        assert rate is not None
        assert 4.0 <= rate <= 60.0


@settings(max_examples=30, deadline=None)
@given(
    heart_rate=st.integers(min_value=30, max_value=230),
    g_load=st.floats(min_value=-5.0, max_value=5.0),
    fs=st.sampled_from([-1, 0, 10, 100]),
    count=st.integers(min_value=-5, max_value=300),
    seed=st.integers(min_value=0, max_value=2**32),
)
def test_generator_is_total_and_in_range(
    heart_rate: int, g_load: float, fs: int, count: int, seed: int
) -> None:
    state = dataclasses.replace(_rest(), heart_rate=Bpm(heart_rate), g_load=GLoad(g_load))
    context = SignalContext(start=Monotonic(0.0), fs=fs, count=count, subject=state)
    samples = RESPGenerator(seed, RespConfig(motion_per_g=Percent(50.0))).render(context)
    assert len(samples) == max(0, count)
    assert all(0 <= s <= SensorKind.RESP.adc_max for s in samples)


def test_same_seed_replays_the_same_signal() -> None:
    assert _render(sim_resp.make(7), 20) == _render(sim_resp.make(7), 20)
    assert _render(sim_resp.make(7), 20) != _render(sim_resp.make(8), 20)


def test_processor_is_deterministic() -> None:
    raw = _render(sim_resp.make(1), 30)
    assert _process(raw) == _process(raw)


def test_generator_kind() -> None:
    assert sim_resp.make().kind is SensorKind.RESP


# =========================================================================
# Through the simulated BITalino
# =========================================================================


async def test_simulated_bitalino_column_is_graded_good() -> None:
    clock = ManualClock()
    subject = Physiology(geometry=ARM, origin=clock.monotonic())
    client = SimulatedBitalinoClient(
        clock,
        physiology=subject,
        channels=[0, SensorKind.RESP.channel],
        sample_rate=FS,
        generators={SensorKind.RESP.value: sim_resp.make(5)},
    )
    assert await client.connect()
    assert await client.start_acquisition()
    window: list[float] = []
    for _ in range(40):
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(FS)
        assert batch is not None
        (column,) = [c for c in batch.channels if c.channel == SensorKind.RESP.value]
        window.extend(column.values)
    reading = make().process(window[-WINDOW:], FS, clock.monotonic())
    assert reading.quality is SignalQuality.GOOD, reading.detail
    rate = _value(reading, "resp_rate")
    assert rate is not None
    assert abs(rate - 12.0) <= 1.5
    await client.stop_acquisition()
    await client.disconnect()
