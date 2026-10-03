"""Tests for the typed boundary between the ECG DSP and the runtime.

Most tests drive :func:`treat_ecg` through a fake treatment, so every branch of
the parse can be reached with a crafted dict. Two run the REAL
``SignalTreatment`` on synthetic ECG from the simulator, so the Protocol the
boundary casts to is checked against the module it describes, and the
freshness counter is checked where it is produced.
"""

from __future__ import annotations

import asyncio
import itertools
import math
from collections.abc import Mapping, Sequence
from typing import Final, override

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import (
    ECG_CHANNEL,
    EcgBridge,
    EcgBridgeStats,
    EcgFrame,
    EcgMetrics,
    Treatment,
    Verdict,
    load_treatment,
    parse_ecg_metrics,
    treat_ecg,
    treat_off_loop,
)
from src.geometry import MachineGeometry
from src.sensors import ecg as typed_ecg
from src.sensors.base import Metric, SensorKind, SensorReading
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, PhysiologyConfig
from src.training.hr_control import MAX_PLAUSIBLE_BPM, MIN_PLAUSIBLE_BPM
from src.training.types import SignalQuality
from src.units import Bpm, Metres, Millivolts, Monotonic, Seconds, UnixMillis

type Metrics = Mapping[str, Mapping[str, object]]
type Treated = Sequence[Mapping[str, object]]


class FakeTreatment(Treatment):
    """Returns whatever the test scripted, and records what it was handed."""

    def __init__(self, treated: Treated, metrics: Metrics, seq: int) -> None:
        self.treated: Treated = treated
        self.metrics: Metrics = metrics
        self.seq: int = seq
        self.handed: list[Sequence[Mapping[str, object]]] = []

    @override
    def treat_batch(self, samples: Sequence[Mapping[str, object]]) -> tuple[Treated, Metrics]:
        self.handed.append(samples)
        return self.treated, self.metrics

    @override
    def metric_seq(self, channel: str) -> int:
        return self.seq if channel == ECG_CHANNEL else -1


def _batch(*names: str) -> SampleBatch:
    return SampleBatch(
        timestamp=UnixMillis(0),
        channels=[ChannelData(channel=name, values=[512.0, 513.0]) for name in names],
    )


GOOD_70: Final[Mapping[str, object]] = {"quality": "good", "heartRate": 70}


# =========================================================================
# parse_ecg_metrics
# =========================================================================


def test_a_good_plausible_reading_carries_its_heart_rate() -> None:
    assert parse_ecg_metrics(GOOD_70, 4) == EcgMetrics(
        seq=4, quality=SignalQuality.GOOD, bpm=Bpm(70)
    )


def test_no_metrics_yet_is_no_signal() -> None:
    assert parse_ecg_metrics(None, 0) == EcgMetrics(
        seq=0, quality=SignalQuality.NO_SIGNAL, bpm=None
    )


@pytest.mark.parametrize("quality", [None, 3, "excellent", "GOOD"])
def test_an_unknown_quality_is_no_signal_and_carries_no_rate(quality: object) -> None:
    """Never optimistic on unknown input: a renamed grade must not read as good."""
    parsed = parse_ecg_metrics({"quality": quality, "heartRate": 70}, 1)
    assert parsed.quality is SignalQuality.NO_SIGNAL
    assert parsed.bpm is None


@pytest.mark.parametrize("quality", ["noisy", "no_signal", "mains_dominated"])
def test_a_rate_under_an_untrustworthy_quality_is_dropped(quality: str) -> None:
    parsed = parse_ecg_metrics({"quality": quality, "heartRate": 70}, 1)
    assert parsed.quality is SignalQuality.from_metric(quality)
    assert parsed.bpm is None


@pytest.mark.parametrize("rate", [None, "70", 70.0, True, math.nan])
def test_a_rate_that_is_not_an_integer_is_dropped(rate: object) -> None:
    """``True`` is an ``int`` to Python; it is not 1 bpm."""
    parsed = parse_ecg_metrics({"quality": "good", "heartRate": rate}, 1)
    assert parsed.quality is SignalQuality.GOOD
    assert parsed.bpm is None


def test_a_missing_rate_under_good_quality_is_none() -> None:
    assert parse_ecg_metrics({"quality": "good"}, 1).bpm is None


@given(rate=st.integers(min_value=-1000, max_value=5000))
def test_a_rate_is_passed_only_inside_the_plausible_range(rate: int) -> None:
    parsed = parse_ecg_metrics({"quality": "good", "heartRate": rate}, 1)
    if MIN_PLAUSIBLE_BPM <= rate <= MAX_PLAUSIBLE_BPM:
        assert parsed.bpm == rate
    else:
        assert parsed.bpm is None


# =========================================================================
# treat_ecg
# =========================================================================


def test_only_the_ecg_column_is_handed_to_the_dsp() -> None:
    fake = FakeTreatment(
        treated=[{"channel": ECG_CHANNEL, "values": [0.1, 0.2]}],
        metrics={ECG_CHANNEL: GOOD_70},
        seq=7,
    )
    frame = treat_ecg(fake, _batch("EDA", ECG_CHANNEL))
    assert frame is not None
    assert frame.millivolts == (0.1, 0.2)
    assert frame.metrics == EcgMetrics(seq=7, quality=SignalQuality.GOOD, bpm=Bpm(70))
    assert fake.handed == [[{"channel": ECG_CHANNEL, "values": [512.0, 513.0]}]]


def test_a_batch_without_an_ecg_column_is_no_frame() -> None:
    fake = FakeTreatment(treated=[], metrics={}, seq=0)
    assert treat_ecg(fake, _batch("EDA")) is None
    assert fake.handed == []


def test_absent_ecg_metrics_are_no_signal() -> None:
    frame = treat_ecg(FakeTreatment(treated=[], metrics={}, seq=0), _batch(ECG_CHANNEL))
    assert frame is not None
    assert frame.millivolts == ()
    assert frame.metrics.quality is SignalQuality.NO_SIGNAL


@pytest.mark.parametrize(
    ("treated", "expected"),
    [
        (
            [{"channel": "EDA", "values": [9.0]}, {"channel": ECG_CHANNEL, "values": [1, 2.5]}],
            (1.0, 2.5),
        ),
        ([{"channel": ECG_CHANNEL, "values": (1.0,)}], ()),
        ([{"channel": ECG_CHANNEL, "values": [math.nan, math.inf, "x", True, 0.5]}], (0.5,)),
        ([{"channel": "EDA", "values": [9.0]}], ()),
    ],
)
def test_the_waveform_keeps_finite_numbers_only(
    treated: Treated, expected: tuple[float, ...]
) -> None:
    """Display data, but a NaN must never reach a JSON frame."""
    frame = treat_ecg(FakeTreatment(treated=treated, metrics={}, seq=0), _batch(ECG_CHANNEL))
    assert frame is not None
    assert frame.millivolts == expected


# =========================================================================
# The real DSP through the boundary
# =========================================================================


async def _synthetic_batches(seconds: int) -> list[SampleBatch]:
    clock = ManualClock()
    subject = Physiology(
        geometry=MachineGeometry(radius=Metres(1.5)),
        origin=clock.monotonic(),
        config=PhysiologyConfig(hr_rest=Bpm(72), hr_max=Bpm(72)),
    )
    client = SimulatedBitalinoClient(clock, physiology=subject)
    assert await client.connect()
    assert await client.start_acquisition()
    batches: list[SampleBatch] = []
    for _ in range(seconds):
        clock.advance(Seconds(1.0))
        batch = await client.read_samples(1000)
        assert batch is not None
        batches.append(batch)
    return batches


def test_the_real_dsp_yields_a_fresh_good_rate_through_the_boundary() -> None:
    """Below 3 s of data nothing is computed (seq 0); then +1 per fresh result, never more.

    Not "one per batch from the third": BioSPPy may fail to extract on a short
    window, and a failed extraction re-emits the old dict WITHOUT advancing the
    counter, which is precisely the property under test.
    """
    treatment = load_treatment(1000, 250)
    frames = [treat_ecg(treatment, batch) for batch in asyncio.run(_synthetic_batches(10))]
    seqs = [frame.metrics.seq for frame in frames if frame is not None]
    assert len(seqs) == 10
    assert seqs[:2] == [0, 0]
    assert all(later - earlier in {0, 1} for earlier, later in itertools.pairwise(seqs))
    assert seqs[-1] >= 5
    last = frames[-1]
    assert last is not None
    assert last.metrics.quality is SignalQuality.GOOD
    assert last.metrics.bpm is not None
    assert abs(last.metrics.bpm - 72) <= 3
    assert len(last.millivolts) == 250


def test_a_reemitted_result_does_not_advance_the_counter() -> None:
    """An empty batch re-emits the previous metrics dict: same seq, so the runtime ignores it."""
    treatment = load_treatment(1000, 250)
    batches = asyncio.run(_synthetic_batches(8))
    for batch in batches:
        treat_ecg(treatment, batch)
    before = treatment.metric_seq(ECG_CHANNEL)
    assert before > 0
    empty = SampleBatch(timestamp=UnixMillis(0), channels=[ChannelData(ECG_CHANNEL, [])])
    frame = treat_ecg(treatment, empty)
    assert frame is not None
    assert frame.metrics.seq == before
    assert treatment.metric_seq("EDA") == 0


# =========================================================================
# EcgBridge
# =========================================================================


class ScriptedSource:
    """Hands out the scripted batches in order, then ``None``; records the counts asked."""

    def __init__(self, batches: Sequence[SampleBatch | None]) -> None:
        self.batches: list[SampleBatch | None] = list(batches)
        self.asked: list[int] = []

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        self.asked.append(count)
        return self.batches.pop(0) if self.batches else None


class RecordingRuntime:
    """Stands in for ``TrainingRuntime.observe_ecg``."""

    def __init__(self) -> None:
        self.seen: list[tuple[Monotonic, int, SignalQuality, Bpm | None]] = []

    def observe_ecg(
        self, now: Monotonic, seq: int, quality: SignalQuality, heart_rate: Bpm | None
    ) -> object:
        self.seen.append((now, seq, quality, heart_rate))
        return None


class RecordingRing:
    """Stands in for ``TelemetryHub.record_ecg``."""

    def __init__(self) -> None:
        self.values: list[Millivolts] = []

    def record_ecg(self, values: Sequence[Millivolts]) -> int:
        self.values.extend(values)
        return len(self.values)


async def _inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    return treat_ecg(treatment, batch)


def typed_reading(bpm: float | None, quality: SignalQuality = SignalQuality.GOOD) -> SensorReading:
    """What the independent processor would say: ``bpm`` under ``quality``."""
    return SensorReading(
        kind=SensorKind.ECG,
        at=Monotonic(0.0),
        display_rate=typed_ecg.SPEC.display_rate,
        waveform=(),
        quality=quality,
        detail="" if quality is SignalQuality.GOOD else "test",
        metrics=(Metric(key=typed_ecg.HEART_RATE, label="", value=bpm, unit="bpm"),),
    )


async def _agrees_at_70(_window: Sequence[float], _fs: int, _at: Monotonic) -> SensorReading:
    """A stand-in independent processor that confirms 70 bpm, whatever it is shown."""
    return typed_reading(70.0)


def _bridge(
    source: ScriptedSource,
    treatment: Treatment,
    *,
    max_batches: int = 10,
) -> tuple[EcgBridge, RecordingRuntime, RecordingRing, ManualClock]:
    clock = ManualClock(Monotonic(50.0))
    runtime = RecordingRuntime()
    ring = RecordingRing()
    bridge = EcgBridge(
        clock=clock,
        source=source,
        treatment=treatment,
        heart_rate=runtime,
        waveform=ring,
        sample_rate=1000,
        treat=_inline,
        max_batches=max_batches,
        confirm=_agrees_at_70,
    )
    return bridge, runtime, ring, clock


def _treated(values: Sequence[float]) -> Treated:
    return [{"channel": ECG_CHANNEL, "values": list(values)}]


async def test_the_bridge_feeds_the_ring_and_the_runtime_with_the_dsps_own_seq() -> None:
    source = ScriptedSource([_batch(ECG_CHANNEL), _batch(ECG_CHANNEL)])
    treatment = FakeTreatment(_treated([0.1, 0.2]), {ECG_CHANNEL: GOOD_70}, seq=7)
    bridge, runtime, ring, _ = _bridge(source, treatment)
    assert await bridge.pump() == 2
    assert source.asked == [200, 200, 200]
    assert bridge.block == 200
    assert ring.values == [0.1, 0.2, 0.1, 0.2]
    # The same seq twice: the first is confirmed; the repeat is handed on with
    # its seq but no rate (the runtime's tracker would ignore it anyway).
    assert runtime.seen == [
        (Monotonic(50.0), 7, SignalQuality.GOOD, Bpm(70)),
        (Monotonic(50.0), 7, SignalQuality.NOISY, None),
    ]
    assert bridge.stats == EcgBridgeStats(
        batches=2,
        samples=4,
        missing_channel=0,
        last_batch_at=Monotonic(50.0),
        last_metrics=EcgMetrics(seq=7, quality=SignalQuality.NOISY, bpm=None),
        gaps=0,
        withheld=1,
        last_verdict=Verdict.REPEATED,
    )


async def test_no_batch_means_no_reading() -> None:
    treatment = FakeTreatment(_treated([0.1]), {ECG_CHANNEL: GOOD_70}, seq=1)
    bridge, runtime, ring, _ = _bridge(ScriptedSource([None]), treatment)
    assert await bridge.pump() == 0
    assert runtime.seen == []
    assert ring.values == []
    assert bridge.stats == EcgBridgeStats()


async def test_a_batch_without_an_ecg_column_is_counted_and_never_becomes_a_reading() -> None:
    treatment = FakeTreatment(_treated([0.1]), {ECG_CHANNEL: GOOD_70}, seq=1)
    source = ScriptedSource([_batch("EDA")])
    bridge, runtime, ring, _ = _bridge(source, treatment)
    assert await bridge.pump() == 1
    assert runtime.seen == []
    assert ring.values == []
    assert bridge.stats.missing_channel == 1
    assert bridge.stats.samples == 0
    assert bridge.stats.last_metrics is None


async def test_the_pump_is_bounded_so_a_backlog_is_worked_off_over_several_pumps() -> None:
    treatment = FakeTreatment(_treated([0.1]), {ECG_CHANNEL: GOOD_70}, seq=1)
    source = ScriptedSource([_batch(ECG_CHANNEL)] * 5)
    bridge, _, _, _ = _bridge(source, treatment, max_batches=2)
    assert await bridge.pump() == 2
    assert await bridge.pump() == 2
    assert await bridge.pump() == 1


@pytest.mark.parametrize(("rate", "budget"), [(1, 10), (1000, 0)])
def test_the_bridge_refuses_a_rate_or_budget_that_can_never_make_a_batch(
    rate: int, budget: int
) -> None:
    with pytest.raises(ValueError, match=r"too low|at least 1"):
        EcgBridge(
            clock=ManualClock(),
            source=ScriptedSource([]),
            treatment=FakeTreatment((), {}, seq=0),
            heart_rate=RecordingRuntime(),
            waveform=RecordingRing(),
            sample_rate=rate,
            max_batches=budget,
        )


async def test_the_default_treat_runs_the_dsp_off_the_event_loop() -> None:
    """``treat_off_loop`` gives the same frame as the inline call, from a worker thread."""
    treatment = FakeTreatment(_treated([0.5]), {ECG_CHANNEL: GOOD_70}, seq=3)
    frame = await treat_off_loop(treatment, _batch(ECG_CHANNEL))
    assert frame == treat_ecg(treatment, _batch(ECG_CHANNEL))


async def test_the_real_dsp_through_the_bridge_produces_a_waveform_and_readings() -> None:
    """End to end on the simulator: sim client -> real SignalTreatment -> bridge."""
    clock = ManualClock()
    subject = Physiology(origin=clock.monotonic(), geometry=MachineGeometry(radius=Metres(1.5)))
    client = SimulatedBitalinoClient(clock, physiology=subject)
    assert await client.connect()
    assert await client.start_acquisition()
    runtime = RecordingRuntime()
    ring = RecordingRing()
    bridge = EcgBridge(
        clock=clock,
        source=client,
        treatment=load_treatment(1000, 250),
        heart_rate=runtime,
        waveform=ring,
        sample_rate=1000,
    )
    for _ in range(60):  # 12 s of 200 ms batches
        clock.advance(Seconds(0.2))
        await bridge.pump()
    # floor(elapsed * fs) on a float clock may leave the last block a hair short.
    assert bridge.stats.batches >= 59
    assert len(ring.values) == bridge.stats.batches * 50
    seqs = [seq for _, seq, _, _ in runtime.seen]
    assert seqs == sorted(seqs)
    assert seqs[-1] > 0
    assert any(bpm is not None for _, _, _, bpm in runtime.seen)


async def test_the_tap_sees_every_channel_and_the_dsp_only_the_ecg() -> None:
    tapped: list[SampleBatch] = []
    treatment = FakeTreatment(_treated([0.1]), {ECG_CHANNEL: GOOD_70}, seq=1)
    bridge = EcgBridge(
        clock=ManualClock(),
        source=ScriptedSource([_batch("EDA", ECG_CHANNEL)]),
        treatment=treatment,
        heart_rate=RecordingRuntime(),
        waveform=RecordingRing(),
        sample_rate=1000,
        treat=_inline,
        tap=tapped.append,
        confirm=_agrees_at_70,
    )
    assert await bridge.pump() == 1
    assert [c.channel for c in tapped[0].channels] == ["EDA", ECG_CHANNEL]
    assert treatment.handed == [[{"channel": ECG_CHANNEL, "values": [512.0, 513.0]}]]
