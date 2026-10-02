"""The finger PPG channel (A3): its processor and its simulated signal.

The processor reports what one wavelength honestly supports - pulse rate,
relative perfusion index, interval regularity - and never a saturation. The
generator follows the plant's heart. The round trip (generator -> processor)
must recover the subject's own heart rate, and every degraded input must be
graded out with every metric ``None``.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.geometry import MachineGeometry
from src.sensors import spo2
from src.sensors.base import SensorKind, SensorReading
from src.sensors.registry import processor_for
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.sim.signals import spo2 as spo2_signal
from src.sim.signals.base import SignalContext
from src.training.types import SignalQuality
from src.units import (
    Bpm,
    GearRatio,
    GLoad,
    Metres,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
)

FS: Final[int] = 1000
WINDOW: Final[int] = 10
ARM: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.0), ratio=GearRatio(49.79))
RATE_TOLERANCE_BPM: Final[float] = 3.0


def _state(heart_rate: float, g: float = 0.0) -> SubjectState:
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g),
        heart_rate=Bpm(round(heart_rate)),
        rr_interval=Seconds(60.0 / heart_rate),
        steady_state=Bpm(round(heart_rate)),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def _render(
    subject: SubjectState, *, fs: int = FS, seconds: int = WINDOW, seed: int = 0
) -> list[float]:
    generator = spo2_signal.make(seed)
    context = SignalContext(start=Monotonic(0.0), fs=fs, count=fs * seconds, subject=subject)
    return [float(v) for v in generator.render(context)]


def _ppg(
    periods: Sequence[float],
    *,
    fs: int = 100,
    scale: Callable[[int], float] = lambda _beat: 1.0,
) -> list[float]:
    """A hand-built pulse train, one beat per period, from the generator's beat shape."""
    rng = random.Random(0)  # noqa: S311 - test signal, not crypto
    out: list[float] = []
    for beat, period in enumerate(periods):
        n = round(period * fs)
        out.extend(
            520.0 + 60.0 * scale(beat) * spo2_signal.pulse_shape(k / n) + rng.gauss(0.0, 0.8)
            for k in range(n)
        )
    return out


def _read(raw: Sequence[float], fs: int = FS) -> SensorReading:
    return spo2.make().process(raw, fs, Monotonic(12.0))


def _value(reading: SensorReading, key: str) -> float:
    metric = reading.metric(key)
    assert metric is not None
    assert metric.value is not None
    return metric.value


# =========================================================================
# The spec and the honest SpO2
# =========================================================================


def test_the_spec_describes_a_single_wavelength_ppg() -> None:
    processor = processor_for(SensorKind.SPO2)
    assert processor.spec is spo2.SPEC
    assert spo2.SPEC.kind is SensorKind.SPO2
    assert spo2.SPEC.unit == "u.a."
    assert "longueur d'onde" in spo2.SPEC.description
    assert [key for key, _label, _unit in spo2.METRICS] == [
        "pulse_rate",
        "perfusion_index",
        "pulse_regularity",
        "spo2",
    ]


def test_a_good_window_measures_the_pulse_and_never_invents_a_saturation() -> None:
    reading = _read(_render(_state(72.0)))
    assert reading.quality is SignalQuality.GOOD
    assert reading.detail == ""
    assert reading.kind is SensorKind.SPO2
    assert reading.at == Monotonic(12.0)
    assert reading.display_rate == spo2.SPEC.display_rate
    assert len(reading.waveform) == WINDOW * spo2.SPEC.display_rate
    assert _value(reading, "pulse_rate") == pytest.approx(72.0, abs=RATE_TOLERANCE_BPM)
    assert 1.0 < _value(reading, "perfusion_index") < 30.0
    assert 0.0 <= _value(reading, "pulse_regularity") < 5.0
    saturation = reading.metric("spo2")
    assert saturation is not None
    assert saturation.value is None
    assert "non mesurable" in saturation.label


# =========================================================================
# Round trip: the generator's pulse is the processor's pulse
# =========================================================================


@pytest.mark.parametrize("fs", [100, 1000])
@pytest.mark.parametrize("heart_rate", [45.0, 60.0, 90.0, 120.0, 150.0, 180.0, 200.0])
@pytest.mark.parametrize("seed", [0, 7])
def test_round_trip_recovers_the_pulse_rate(fs: int, heart_rate: float, seed: int) -> None:
    reading = _read(_render(_state(heart_rate, g=0.5), fs=fs, seed=seed), fs)
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "pulse_rate") == pytest.approx(heart_rate, abs=RATE_TOLERANCE_BPM)


@pytest.mark.parametrize("motor_rpm", [0, 600, 1000, 1200, 1500])
def test_the_pulse_rate_agrees_with_the_subjects_true_heart_rate(motor_rpm: int) -> None:
    subject = Physiology(origin=Monotonic(0.0), geometry=ARM)
    state = subject.advance(Monotonic(10_000.0), MotorRpm(motor_rpm))
    assert float(state.g_load) < spo2_signal.BURST_ONSET_G
    reading = _read(_render(state, seed=motor_rpm))
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "pulse_rate") == pytest.approx(state.heart_rate, abs=RATE_TOLERANCE_BPM)


def test_vasoconstriction_under_g_lowers_the_perfusion_index() -> None:
    rest = _value(_read(_render(_state(90.0, g=0.0))), "perfusion_index")
    loaded = _value(_read(_render(_state(90.0, g=1.5))), "perfusion_index")
    assert loaded < 0.8 * rest


def test_motion_artefact_under_heavy_g_is_graded_noisy() -> None:
    reading = _read(_render(_state(150.0, g=5.0), seed=1))
    assert reading.quality is SignalQuality.NOISY
    assert "mouvement" in reading.detail or "rythme" in reading.detail
    assert all(metric.value is None for metric in reading.metrics)


async def test_a_simulated_bitalino_with_this_generator_yields_a_good_column() -> None:
    clock = ManualClock()
    client = SimulatedBitalinoClient(
        clock,
        physiology=Physiology(origin=clock.monotonic(), geometry=ARM),
        channels=[0, spo2.SPEC.kind.channel],
        sample_rate=FS,
        generators={SensorKind.SPO2.value: spo2_signal.make(3)},
    )
    assert await client.connect()
    assert await client.start_acquisition()
    values: list[float] = []
    clock.advance(Seconds(WINDOW + 0.5))
    for _ in range(50):  # 50 blocks of 0.2 s: the generator's phases carry over
        batch = await client.read_samples(FS // 5)
        assert batch is not None
        columns = {column.channel: column.values for column in batch.channels}
        values.extend(columns[SensorKind.SPO2.value])
    reading = _read(values)
    assert reading.quality is SignalQuality.GOOD
    assert _value(reading, "pulse_rate") == pytest.approx(
        client.subject.advance(clock.monotonic(), MotorRpm(0)).heart_rate,
        abs=RATE_TOLERANCE_BPM,
    )


# =========================================================================
# Degraded windows: graded out, every metric None
# =========================================================================


def _slow_sine(frequency: float, seconds: int, fs: int = 100) -> list[float]:
    return [
        520.0 + 40.0 * math.sin(2.0 * math.pi * frequency * k / fs) for k in range(seconds * fs)
    ]


def _with_step(raw: list[float]) -> list[float]:
    half = len(raw) // 2
    return raw[:half] + [v + 150.0 for v in raw[half:]]


def _with_burst(raw: list[float], fs: int = FS) -> list[float]:
    start = 4 * fs
    burst = [
        v + 150.0 * math.sin(2.0 * math.pi * 4.0 * (k - start) / fs)
        if start <= k < start + fs
        else v
        for k, v in enumerate(raw)
    ]
    return [min(1023.0, max(0.0, v)) for v in burst]


def _drift() -> list[float]:
    rng = random.Random(1)  # noqa: S311 - test signal, not crypto
    return [
        520.0 + 5.0 * math.sin(2.0 * math.pi * 0.05 * k / 100) + rng.gauss(0.0, 0.3)
        for k in range(1000)
    ]


@pytest.mark.parametrize(
    ("raw", "fs", "quality", "fragment"),
    [
        pytest.param([], FS, SignalQuality.NO_SIGNAL, "aucun echantillon", id="empty"),
        pytest.param([512.0] * 5000, FS, SignalQuality.NO_SIGNAL, "plat", id="flat"),
        pytest.param([0.0, 1023.0] * 2000, FS, SignalQuality.NOISY, "sature", id="saturated"),
        pytest.param(
            _render(_state(70.0), seconds=2), FS, SignalQuality.NO_SIGNAL, "courte", id="short"
        ),
        pytest.param(
            _ppg([1.0] * 10, fs=10), 10, SignalQuality.NO_SIGNAL, "filtrage", id="fs-too-low"
        ),
        pytest.param(
            [500.0, math.nan, 520.0] * 400, 100, SignalQuality.NO_SIGNAL, "non numeriques", id="nan"
        ),
        pytest.param(_drift(), 100, SignalQuality.NO_SIGNAL, "pulsatile", id="no-pulse"),
        pytest.param(
            _with_step(_render(_state(80.0))), FS, SignalQuality.NO_SIGNAL, "doigt", id="finger-out"
        ),
        pytest.param(
            _with_burst(_render(_state(80.0))), FS, SignalQuality.NOISY, "bouffee", id="burst"
        ),
        pytest.param(_slow_sine(0.6, 5), 100, SignalQuality.NO_SIGNAL, "battements", id="few"),
        pytest.param(
            _ppg([1.0] * 10, scale=lambda beat: 2.2 if beat == 5 else 1.0),
            100,
            SignalQuality.NOISY,
            "amplitude",
            id="strong-beat",
        ),
        pytest.param(
            _ppg([0.8] * 12, scale=lambda beat: 0.45 if beat == 6 else 1.0),
            100,
            SignalQuality.NOISY,
            "amplitude",
            id="weak-beat",
        ),
        pytest.param(_ppg([2.4] * 9), 100, SignalQuality.NOISY, "hors physiologie", id="too-slow"),
        pytest.param(_ppg([0.6, 1.2] * 6), 100, SignalQuality.NOISY, "rythme", id="irregular"),
    ],
)
def test_degraded_windows_are_graded_out(
    raw: list[float], fs: int, quality: SignalQuality, fragment: str
) -> None:
    reading = _read(raw, fs)
    assert reading.quality is quality
    assert fragment in reading.detail
    assert [metric.key for metric in reading.metrics] == [key for key, _l, _u in spo2.METRICS]
    assert all(metric.value is None for metric in reading.metrics)


# =========================================================================
# Totality and determinism
# =========================================================================

_samples = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.floats(min_value=0.0, max_value=1023.0),
    st.integers(min_value=0, max_value=1023).map(float),
)


@settings(max_examples=200, deadline=None)
@given(
    raw=st.lists(_samples, max_size=1500),
    fs=st.sampled_from([-1, 0, 1, 10, 100, 250]),
)
def test_the_processor_is_total_and_honest(raw: list[float], fs: int) -> None:
    reading = _read(raw, fs)
    assert reading.kind is SensorKind.SPO2
    values = [metric.value for metric in reading.metrics]
    if reading.quality is SignalQuality.GOOD:
        rate = _value(reading, "pulse_rate")
        assert spo2.MIN_PULSE_BPM <= rate <= spo2.MAX_PULSE_BPM
        assert all(v is None or math.isfinite(v) for v in values)
    else:
        assert reading.detail != ""
        assert all(v is None for v in values)
    saturation = reading.metric("spo2")
    assert saturation is not None
    assert saturation.value is None


@settings(max_examples=100, deadline=None)
@given(
    rr=st.floats(allow_nan=True, allow_infinity=True),
    g=st.floats(allow_nan=True, allow_infinity=True),
    count=st.integers(min_value=-5, max_value=400),
    fs=st.sampled_from([-1, 0, 1, 100, 1000]),
    seed=st.integers(min_value=0, max_value=2**32),
)
def test_the_generator_is_total_and_in_range(
    rr: float, g: float, count: int, fs: int, seed: int
) -> None:
    subject = SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g),
        heart_rate=Bpm(70),
        rr_interval=Seconds(rr),
        steady_state=Bpm(70),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )
    generator = spo2_signal.make(seed)
    assert generator.kind is SensorKind.SPO2
    samples = generator.render(
        SignalContext(start=Monotonic(0.0), fs=fs, count=count, subject=subject)
    )
    expected = count if count > 0 and fs > 0 else 0
    assert len(samples) == expected
    assert all(0 <= v <= SensorKind.SPO2.adc_max for v in samples)


def test_the_same_seed_replays_the_same_signal_and_another_does_not() -> None:
    state = _state(95.0, g=3.0)
    assert _render(state, seed=11) == _render(state, seed=11)
    assert _render(state, seed=11) != _render(state, seed=12)


def test_rendering_in_blocks_is_continuous() -> None:
    state = _state(80.0)
    whole = spo2_signal.make(5).render(
        SignalContext(start=Monotonic(0.0), fs=FS, count=2000, subject=state)
    )
    split = spo2_signal.make(5)
    first = split.render(SignalContext(start=Monotonic(0.0), fs=FS, count=700, subject=state))
    second = split.render(SignalContext(start=Monotonic(0.7), fs=FS, count=1300, subject=state))
    assert first + second == whole
