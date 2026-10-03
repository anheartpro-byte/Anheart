"""The multi-sensor framework: DSP boundary, contract helpers, hub, wiring, API.

The six processors are tested in their own ``tests/test_sensor_<kind>.py``;
this file tests what they all stand on, and the one property the whole
framework exists to keep: **adding a sensor never changes what the heart-rate
controller is told** (the ECG bridge hands the controller the ECG column only).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final

import httpx
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

from src import dsp
from src.bitalino_client import ChannelData, LinkStats, SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import ecg_only
from src.local_config import LocalConfig, load_local_config
from src.local_panel import acquired_channels, build_panel, link_losses, simulated_generators
from src.result import Err, Ok, Result
from src.sensors.base import (
    Metric,
    SensorKind,
    SensorReading,
    SensorSpec,
    blank_metrics,
    decimate,
    parse_kind,
    raw_quality,
    unprocessed,
)
from src.sensors.hub import SensorHub, to_thread
from src.sensors.registry import FACTORIES, SPECS, processor_for
from src.sim.physiology import SubjectState
from src.sim.signals.base import SignalContext, flat
from src.sim.signals.registry import GENERATORS, generator_for
from src.training.types import SignalQuality
from src.units import (
    AdcCount,
    Bpm,
    GLoad,
    Hertz,
    Monotonic,
    MotorRpm,
    OutputRpm,
    Seconds,
    UnixMillis,
)
from src.web.app import create_app
from src.web.schemas import SensorRow, SensorsRow

FS: Final[Hertz] = Hertz(1000.0)
SENSORS_ROW: Final[TypeAdapter[SensorsRow]] = TypeAdapter(SensorsRow)
SIM_ENV: Final[dict[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
}


def sine(frequency: float, seconds: float = 4.0, fs: float = 1000.0) -> dsp.Signal:
    t = np.arange(round(seconds * fs), dtype=np.float64) / fs
    return np.sin(2.0 * math.pi * frequency * t)


def config_of(**changes: str) -> LocalConfig:
    loaded = load_local_config({**SIM_ENV, **changes})
    assert isinstance(loaded, Ok), loaded
    return loaded.value


# =========================================================================
# src/dsp.py
# =========================================================================


def test_filters_keep_the_band_and_remove_the_rest() -> None:
    mixed = sine(5.0) + sine(100.0)
    low = dsp.lowpass(mixed, FS, Hertz(20.0))
    high = dsp.highpass(mixed, FS, Hertz(50.0))
    band = dsp.bandpass(mixed, FS, Hertz(2.0), Hertz(20.0))
    assert isinstance(low, Ok) and isinstance(high, Ok) and isinstance(band, Ok)  # noqa: PT018
    assert dsp.rms(low.value) == pytest.approx(math.sqrt(0.5), rel=0.05)
    assert dsp.rms(high.value) == pytest.approx(math.sqrt(0.5), rel=0.05)
    assert dsp.rms(band.value) == pytest.approx(math.sqrt(0.5), rel=0.05)


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        (lambda: dsp.lowpass(sine(5.0), Hertz(0.0), Hertz(1.0)), "sample rate"),
        (lambda: dsp.lowpass(dsp.as_signal([]), FS, Hertz(1.0)), "empty"),
        (lambda: dsp.lowpass(dsp.as_signal([1.0, math.nan]), FS, Hertz(1.0)), "NaN"),
        (lambda: dsp.lowpass(sine(5.0), FS, Hertz(600.0)), "cut-off"),
        (lambda: dsp.highpass(sine(5.0), FS, Hertz(-1.0)), "cut-off"),
        (lambda: dsp.bandpass(sine(5.0), FS, Hertz(20.0), Hertz(10.0)), "empty"),
        (lambda: dsp.bandpass(sine(5.0), FS, Hertz(0.0), Hertz(10.0)), "cut-off"),
        (lambda: dsp.lowpass(dsp.as_signal([1.0, 2.0, 3.0]), FS, Hertz(10.0)), "low-pass"),
        (lambda: dsp.lowpass(dsp.as_signal([1e12, 1.0]), FS, Hertz(10.0)), "magnitude"),
    ],
)
def test_filters_refuse_rather_than_raise(
    call: Callable[[], Result[dsp.Signal, dsp.DspRefused]], fragment: str
) -> None:
    result = call()
    assert isinstance(result, Err)
    assert fragment in result.error.detail


def test_peaks_find_every_beat_and_nothing_in_garbage() -> None:
    x = sine(2.0, seconds=5.0)
    assert len(dsp.peaks(x, FS, min_interval=Seconds(0.3))) == 10
    assert dsp.peaks(dsp.as_signal([1.0, 2.0]), FS, min_interval=Seconds(0.3)) == ()
    assert dsp.peaks(dsp.as_signal([1.0, math.inf, 1.0]), FS, min_interval=Seconds(0.3)) == ()
    assert dsp.peaks(x, Hertz(0.0), min_interval=Seconds(0.3)) == ()
    assert dsp.peaks(x, FS, min_interval=Seconds(0.0)) == ()
    assert dsp.peaks(x * 1e12, FS, min_interval=Seconds(0.3)) == ()


def test_spectra_find_the_line() -> None:
    x = sine(12.0)
    found = dsp.dominant_frequency(x, FS, Hertz(5.0), Hertz(30.0))
    assert isinstance(found, Ok)
    assert found.value == pytest.approx(12.0, abs=0.5)
    median = dsp.median_frequency(x, FS)
    assert isinstance(median, Ok)
    assert median.value == pytest.approx(12.0, abs=4.0)


def test_spectra_refuse_short_silent_or_empty_bands() -> None:
    short = dsp.spectrum(dsp.as_signal([1.0, 2.0, 3.0]), FS)
    assert isinstance(short, Err)
    assert isinstance(dsp.median_frequency(dsp.as_signal([1.0, 2.0]), FS), Err)
    silent = dsp.as_signal([0.0] * 512)
    assert isinstance(dsp.median_frequency(silent, FS), Err)
    assert isinstance(dsp.dominant_frequency(silent, FS, Hertz(1.0), Hertz(10.0)), Err)
    assert isinstance(dsp.dominant_frequency(sine(5.0), FS, Hertz(600.0), Hertz(700.0)), Err)
    assert isinstance(dsp.dominant_frequency(dsp.as_signal([]), FS, Hertz(1.0), Hertz(2.0)), Err)


def test_rms_of_nothing_or_garbage_is_zero() -> None:
    assert dsp.rms(dsp.as_signal([0.0, 0.0])) == 0.0
    assert dsp.rms(dsp.as_signal([1e300, -1e300])) == pytest.approx(1e300)
    assert dsp.rms(dsp.as_signal([])) == 0.0
    assert dsp.rms(dsp.as_signal([math.nan])) == 0.0
    assert dsp.rms(dsp.as_signal([3.0, -3.0])) == pytest.approx(3.0)


@given(st.lists(st.floats(allow_nan=True, allow_infinity=True), max_size=300))
def test_no_dsp_function_ever_raises(values: list[float]) -> None:
    x = dsp.as_signal(values)
    dsp.lowpass(x, FS, Hertz(10.0))
    dsp.bandpass(x, FS, Hertz(1.0), Hertz(40.0))
    dsp.highpass(x, FS, Hertz(1.0))
    dsp.peaks(x, FS, min_interval=Seconds(0.1))
    dsp.median_frequency(x, FS)
    dsp.dominant_frequency(x, FS, Hertz(1.0), Hertz(40.0))
    assert math.isfinite(dsp.rms(x))


# =========================================================================
# src/sensors/base.py and the registry
# =========================================================================


def test_every_kind_names_its_channel_and_resolution() -> None:
    assert [k.channel for k in SensorKind] == [0, 1, 2, 3, 4, 5]
    assert SensorKind.ECG.adc_max == 1023
    assert SensorKind.EMG.adc_bits == 6
    assert SensorKind.LUX.adc_max == 63


@pytest.mark.parametrize(("name", "kind"), [("ecg", SensorKind.ECG), (" spo2 ", SensorKind.SPO2)])
def test_kinds_parse_case_insensitively(name: str, kind: SensorKind) -> None:
    assert parse_kind(name) is kind


def test_an_unknown_kind_does_not_parse() -> None:
    assert parse_kind("EEG") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([], SignalQuality.NO_SIGNAL),
        ([-1.0, 5.0], SignalQuality.NO_SIGNAL),
        ([5.0, math.nan, 7.0], SignalQuality.NO_SIGNAL),
        ([math.inf, 5.0], SignalQuality.NO_SIGNAL),
        ([5.0, 2000.0], SignalQuality.NO_SIGNAL),
        ([500.0, 501.0, 502.0], SignalQuality.NO_SIGNAL),
        ([0.0] * 10 + [500.0] * 10, SignalQuality.NOISY),
    ],
)
def test_raw_quality_catches_what_needs_no_physiology(
    raw: list[float], expected: SignalQuality
) -> None:
    judged = raw_quality(raw, 1023)
    assert judged is not None
    assert judged[0] is expected


def test_raw_quality_passes_a_live_signal_on() -> None:
    assert raw_quality([400.0, 600.0, 500.0, 450.0], 1023) is None


def test_decimate_and_blank_metrics() -> None:
    assert decimate([1.0, 2.0, 3.0, 4.0], 4, 2) == (1.0, 3.0)
    assert decimate([], 4, 2) == ()
    assert decimate([1.0], 0, 2) == ()
    assert blank_metrics([("rate", "Frequence", "/min")]) == (
        Metric(key="rate", label="Frequence", value=None, unit="/min"),
    )


def test_a_reading_finds_its_metrics() -> None:
    reading = SensorReading(
        kind=SensorKind.EDA,
        at=Monotonic(1.0),
        display_rate=10,
        waveform=(),
        quality=SignalQuality.GOOD,
        detail="",
        metrics=(Metric(key="scl", label="SCL", value=2.0, unit="uS"),),
    )
    assert reading.metric("scl") == Metric(key="scl", label="SCL", value=2.0, unit="uS")
    assert reading.metric("nope") is None


def test_an_unprocessed_live_signal_says_so() -> None:
    spec = SPECS[SensorKind.LUX]
    reading = unprocessed(spec, [10.0, 40.0, 20.0], 1000, Monotonic(2.0))
    assert reading.quality is SignalQuality.NOISY
    assert reading.detail == "traitement non implemente"


def test_every_kind_has_a_processor_and_a_spec() -> None:
    assert set(FACTORIES) == set(SensorKind) == set(SPECS)
    for kind in SensorKind:
        processor = processor_for(kind)
        assert processor.spec.kind is kind
        assert processor.spec is SPECS[kind]
        reading = processor.process([], 1000, Monotonic(0.0))
        assert reading.kind is kind
        assert reading.quality is SignalQuality.NO_SIGNAL


# =========================================================================
# Simulated signals
# =========================================================================


def subject() -> SubjectState:
    return SubjectState(
        at=Monotonic(1.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(0.0),
        heart_rate=Bpm(70),
        rr_interval=Seconds(60 / 70),
        steady_state=Bpm(70),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def test_every_channel_but_the_ecg_has_a_generator() -> None:
    assert set(GENERATORS) == set(SensorKind) - {SensorKind.ECG}
    assert generator_for(SensorKind.ECG) is None
    context = SignalContext(start=Monotonic(0.0), fs=1000, count=50, subject=subject())
    for kind in GENERATORS:
        generator = generator_for(kind, seed=3)
        assert generator is not None
        assert generator.kind is kind
        samples = generator.render(context)
        assert len(samples) == 50
        assert all(0 <= s <= kind.adc_max for s in samples)


def test_flat_is_mid_scale_and_never_negative_length() -> None:
    assert flat(SensorKind.EMG, 2) == (AdcCount(31), AdcCount(31))
    assert flat(SensorKind.ECG, -1) == ()


# =========================================================================
# The hub
# =========================================================================


def batch(**columns: Sequence[float]) -> SampleBatch:
    return SampleBatch(
        timestamp=UnixMillis(0),
        channels=tuple(ChannelData(channel=name, values=v) for name, v in columns.items()),
    )


async def inline(work: Callable[[], tuple[SensorReading, ...]]) -> tuple[SensorReading, ...]:
    return work()


async def test_the_hub_windows_each_channel_and_processes_it() -> None:
    clock = ManualClock(Monotonic(5.0))
    hub = SensorHub(
        clock=clock, kinds=(SensorKind.ECG, SensorKind.EDA, SensorKind.ECG), fs=10, offload=inline
    )
    assert hub.kinds == (SensorKind.ECG, SensorKind.EDA)
    assert hub.sample_rate == 10
    assert hub.processor(SensorKind.RESP) is None
    assert hub.processor(SensorKind.EDA) is not None
    before = await hub.refresh()
    assert {r.kind for r in before} == {SensorKind.ECG, SensorKind.EDA}
    assert all(r.at == Monotonic(5.0) for r in before)
    hub.accept(batch(ECG=[1.0, 900.0, 3.0], EDA=[500.0] * 3, RESP=[1.0] * 3))
    clock.advance(Seconds(1.0))
    readings = await hub.refresh()
    assert hub.latest()[SensorKind.ECG] in readings
    ecg = hub.latest()[SensorKind.ECG]
    assert ecg.at == Monotonic(5.0)  # the batch's arrival, not the refresh
    assert hub.latest()[SensorKind.EDA].quality is SignalQuality.NO_SIGNAL  # flat


async def test_the_hub_keeps_only_each_sensor_s_window() -> None:
    hub = SensorHub(clock=ManualClock(), kinds=(SensorKind.EMG,), fs=100, offload=inline)
    window = round(float(SPECS[SensorKind.EMG].window_s) * 100)
    hub.accept(batch(EMG=[float(i % 60) for i in range(window * 3)]))
    reading = (await hub.refresh())[0]
    assert len(reading.waveform) <= window


class Exploding:
    """A processor that breaks the contract, to prove the hub's fence."""

    @property
    def spec(self) -> SensorSpec:
        return SPECS[SensorKind.RESP]

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:  # noqa: ARG002
        raise ZeroDivisionError


async def test_one_broken_processor_does_not_silence_the_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = SensorHub(
        clock=ManualClock(),
        kinds=(SensorKind.RESP, SensorKind.ECG),
        fs=100,
        processors={SensorKind.RESP: Exploding()},
        offload=inline,
    )
    readings = {r.kind: r for r in await hub.refresh()}
    assert readings[SensorKind.RESP].detail == "erreur de traitement (voir le journal)"
    assert readings[SensorKind.ECG].kind is SensorKind.ECG
    assert "processor RESP raised" in caplog.text


async def test_the_production_offload_runs_on_a_worker_thread() -> None:
    assert await to_thread(tuple) == ()


def test_a_hub_needs_a_positive_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        SensorHub(clock=ManualClock(), kinds=(SensorKind.ECG,), fs=0)


# =========================================================================
# The heart-rate path sees the ECG only
# =========================================================================


def test_the_heart_rate_path_is_handed_the_ecg_column_only() -> None:
    both = batch(ECG=[1.0], EDA=[2.0])
    stripped = ecg_only(both)
    assert [c.channel for c in stripped.channels] == ["ECG"]
    assert stripped.timestamp == both.timestamp
    only = batch(ECG=[1.0])
    assert ecg_only(only) is only


# =========================================================================
# Configuration, wiring, API
# =========================================================================


def test_sensors_default_to_the_ecg_and_parse_a_list() -> None:
    assert config_of().sensors == (SensorKind.ECG,)
    config = config_of(SENSORS="ECG, eda,RESP,eda,,")
    assert config.sensors == (SensorKind.ECG, SensorKind.EDA, SensorKind.RESP)
    assert acquired_channels(config) == (0, 1, 3)
    assert set(simulated_generators(config)) == {"EDA", "RESP"}


@pytest.mark.parametrize(
    ("value", "fragment"), [("ECG,EEG", "inconnu"), ("EDA", "ECG obligatoire")]
)
def test_bad_sensor_lists_are_refused(value: str, fragment: str) -> None:
    loaded = load_local_config({**SIM_ENV, "SENSORS": value})
    assert isinstance(loaded, Err)
    assert [p.key for p in loaded.error] == ["SENSORS"]
    assert fragment in loaded.error[0].detail


async def test_every_configured_sensor_reaches_the_api(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    config = config_of(SENSORS="ECG,EDA,SpO2,RESP,EMG,LUX")
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json")
    for _ in range(10):
        clock.advance(Seconds(0.2))
        await panel.ecg_step()
        await panel.control_step()
    await panel.sensor_step()
    assert set(panel.sensors.latest()) == set(SensorKind)
    app = create_app(services=panel.services, config=config.web)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/sensors")
    assert response.status_code == 200
    rows = SENSORS_ROW.validate_json(response.text).sensors
    assert [row.kind for row in rows] == ["ECG", "EDA", "SpO2", "RESP", "EMG", "LUX"]
    assert [row.channel for row in rows] == [0, 1, 2, 3, 4, 5]
    await panel.close()


def test_a_sensor_with_no_reading_yet_is_a_row_saying_so() -> None:
    row = SensorRow.of(SPECS[SensorKind.EDA], None)
    assert row.at is None
    assert row.quality == "no_signal"
    reading = SensorReading(
        kind=SensorKind.EDA,
        at=Monotonic(1.0),
        display_rate=10,
        waveform=(1.0, math.nan),
        quality=SignalQuality.GOOD,
        detail="",
        metrics=(
            Metric(key="scl", label="SCL", value=math.inf, unit="uS"),
            Metric(key="scr", label="SCR", value=None, unit="/min"),
        ),
    )
    rendered = SensorRow.of(SPECS[SensorKind.EDA], reading)
    assert rendered.waveform == (1.0, 0.0)
    assert [m.value for m in rendered.metrics] == [None, None]


async def test_an_api_without_sensors_answers_an_empty_list(tmp_path: Path) -> None:
    panel = build_panel(config_of(), clock=ManualClock(), profiles_path=tmp_path / "p.json")
    services = replace(panel.services, sensors=None)
    app = create_app(services=services, config=config_of().web)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/sensors")
    assert SENSORS_ROW.validate_json(response.text).sensors == ()
    await panel.close()


# =========================================================================
# Contract rule 5: each untyped library is imported from one module only
# =========================================================================

SRC: Final[Path] = Path(__file__).resolve().parent.parent / "src"


@pytest.mark.parametrize(
    ("library", "allowed"),
    [
        # signal_processing.py is the legacy heart-rate path, pending migration.
        ("scipy", {"dsp.py", "signal_processing.py"}),
        ("biosppy", {"signal_processing.py"}),
    ],
)
def test_untyped_signal_libraries_stay_in_their_one_module(library: str, allowed: set[str]) -> None:
    importers = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if any(
            line.strip().startswith((f"import {library}", f"from {library}"))
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    }
    assert importers <= allowed, f"{library} imported outside its module: {importers - allowed}"


def test_the_flat_threshold_is_per_sensor() -> None:
    """A quiet skin conductance spanning one count is signal for EDA, flat for the default."""
    quiet = [500.0, 501.0] * 50
    assert raw_quality(quiet, 1023) is not None
    assert raw_quality(quiet, 1023, SPECS[SensorKind.EDA].flat_span_counts) is None
    assert raw_quality([500.0] * 100, 1023, SPECS[SensorKind.EDA].flat_span_counts) is not None


def test_the_real_client_s_hidden_losses_break_the_ecg_stream() -> None:
    """Held values and dropped backlog are losses the timestamps cannot show."""
    count = link_losses(lambda: LinkStats(filled_samples=3, dropped_backlog_samples=4))
    assert count() == 7
