"""The ECG bridge's independent confirmation and batch-continuity gate.

The defect this pins: the legacy DSP (``src/signal_processing.py``) grades
white noise, a corrupted stream and motion artefact ``good`` and BioSPPy then
reads a heart rate out of them, and a window stitched across lost batches gives
RR intervals that never existed. Before the fix every one of those numbers
reached ``TrainingRuntime.observe_ecg`` as usable.

Two layers of tests:

* the pure pieces (:func:`judge`, :func:`typed_rate`, :func:`withheld`) and the
  bridge's continuity bookkeeping, driven by a scripted legacy DSP and a
  scripted independent processor, so every branch is reached on purpose; plus
  the two properties the fix exists for, under hypothesis;
* the REAL legacy DSP and the REAL typed processor, inline, on synthesised ECG
  (clean, white noise, motion artefact, NaN, lost batches, one gap), recording
  both what the legacy DSP offered (the "before") and what the runtime was
  handed (the "after").
"""

from __future__ import annotations

import asyncio
import importlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, cast, override

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src import dsp
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import (
    AGREEMENT_BPM,
    CONTINUITY_TOLERANCE_MS,
    ECG_CHANNEL,
    LEGACY_WINDOW_S,
    Confirmation,
    EcgBridge,
    EcgFrame,
    EcgMetrics,
    Treatment,
    Verdict,
    confirm_off_loop,
    judge,
    load_treatment,
    treat_ecg,
    typed_rate,
    window_samples,
    withheld,
)
from src.sensors import ecg as typed_ecg
from src.sensors.base import Metric, SensorKind, SensorReading
from src.sim.ecg import EcgConfig, EcgSynthesizer
from src.sim.physiology import SubjectState
from src.training.types import SignalQuality
from src.units import Bpm, GLoad, Millivolts, Monotonic, MotorRpm, OutputRpm, Seconds, UnixMillis

FS: Final[int] = 1000
BLOCK: Final[int] = 200
EPOCH: Final[int] = 1_700_000_000_000

type Metrics = dict[str, dict[str, object]]


def reading(
    bpm: float | None,
    quality: SignalQuality = SignalQuality.GOOD,
    *,
    kind: SensorKind = SensorKind.ECG,
    metrics: tuple[Metric, ...] | None = None,
) -> SensorReading:
    """An independent-processor reading saying ``bpm`` under ``quality``."""
    found = (
        (Metric(key=typed_ecg.HEART_RATE, label="", value=bpm, unit="bpm"),)
        if metrics is None
        else metrics
    )
    return SensorReading(
        kind=kind,
        at=Monotonic(0.0),
        display_rate=typed_ecg.SPEC.display_rate,
        waveform=(),
        quality=quality,
        detail="",
        metrics=found,
    )


def good(seq: int, bpm: int) -> EcgMetrics:
    return EcgMetrics(seq=seq, quality=SignalQuality.GOOD, bpm=Bpm(bpm))


def usable(quality: SignalQuality, bpm: Bpm | None) -> bool:
    """What the runtime may act on: exactly ``HeartRateSample.usable_bpm``'s rule."""
    return quality.is_trustworthy and bpm is not None


# =========================================================================
# judge / typed_rate / withheld
# =========================================================================


def test_a_rate_both_processors_grade_good_and_agree_on_is_confirmed() -> None:
    confirmed = judge(good(3, 70), reading(71.4))
    assert confirmed == Confirmation(verdict=Verdict.CONFIRMED, metrics=good(3, 70), typed_bpm=71.4)


def test_the_legacy_rate_is_the_one_handed_on() -> None:
    """The typed processor only vetoes: within the tolerance the legacy number stands."""
    assert judge(good(1, 70), reading(70.0 + AGREEMENT_BPM)).metrics.bpm == Bpm(70)
    assert judge(good(1, 70), reading(70.0 - AGREEMENT_BPM)).metrics.bpm == Bpm(70)


@pytest.mark.parametrize("typed", [70.0 + AGREEMENT_BPM + 0.01, 70.0 - AGREEMENT_BPM - 0.01, 133.0])
def test_a_disagreement_withholds_the_rate(typed: float) -> None:
    assert judge(good(9, 70), reading(typed)) == Confirmation(
        verdict=Verdict.DISAGREE,
        metrics=EcgMetrics(seq=9, quality=SignalQuality.NOISY, bpm=None),
        typed_bpm=typed,
    )


@pytest.mark.parametrize(
    "typed",
    [
        reading(70.0, SignalQuality.NOISY),
        reading(70.0, SignalQuality.MAINS_DOMINATED),
        reading(70.0, SignalQuality.NO_SIGNAL),
        reading(None),
        reading(math.nan),
        reading(math.inf),
        reading(70.0, metrics=()),
        reading(70.0, kind=SensorKind.SPO2),
    ],
    ids=["noisy", "mains", "no_signal", "none", "nan", "inf", "no_metric", "not_ecg"],
)
def test_anything_short_of_a_good_finite_typed_rate_withholds(typed: SensorReading) -> None:
    confirmation = judge(good(4, 70), typed)
    assert confirmation.verdict is Verdict.TYPED_REJECTED
    assert confirmation.metrics == EcgMetrics(seq=4, quality=SignalQuality.NOISY, bpm=None)
    assert typed_rate(typed) is None


@pytest.mark.parametrize(
    "legacy",
    [
        EcgMetrics(seq=2, quality=SignalQuality.MAINS_DOMINATED, bpm=None),
        EcgMetrics(seq=2, quality=SignalQuality.GOOD, bpm=None),
        EcgMetrics(seq=0, quality=SignalQuality.NO_SIGNAL, bpm=None),
    ],
)
def test_a_reading_without_a_rate_is_passed_on_unchanged(legacy: EcgMetrics) -> None:
    """Its own grade says more to the operator than NOISY would; it carries no number anyway."""
    assert judge(legacy, reading(70.0)) == Confirmation(verdict=Verdict.NO_RATE, metrics=legacy)


def test_withheld_keeps_the_seq_and_drops_the_rate() -> None:
    assert withheld(good(12, 80), Verdict.STITCHED) == Confirmation(
        verdict=Verdict.STITCHED,
        metrics=EcgMetrics(seq=12, quality=SignalQuality.NOISY, bpm=None),
    )


_QUALITIES = st.sampled_from(list(SignalQuality))
_BPMS = st.one_of(st.none(), st.integers(min_value=20, max_value=260).map(Bpm))
_TYPED_BPMS = st.one_of(
    st.none(), st.floats(allow_nan=True, allow_infinity=True), st.floats(20.0, 260.0)
)


@given(
    seq=st.integers(min_value=0, max_value=10_000),
    legacy_quality=_QUALITIES,
    legacy_bpm=_BPMS,
    typed_quality=_QUALITIES,
    typed_bpm=_TYPED_BPMS,
)
def test_a_usable_rate_needs_both_processors_good_and_agreeing(
    seq: int,
    legacy_quality: SignalQuality,
    legacy_bpm: Bpm | None,
    typed_quality: SignalQuality,
    typed_bpm: float | None,
) -> None:
    legacy = EcgMetrics(seq=seq, quality=legacy_quality, bpm=legacy_bpm)
    out = judge(legacy, reading(typed_bpm, typed_quality)).metrics
    assert out.seq == seq
    assert out.bpm in {None, legacy_bpm}
    if usable(out.quality, out.bpm):
        assert legacy_quality is SignalQuality.GOOD
        assert typed_quality is SignalQuality.GOOD
        assert legacy_bpm is not None
        assert typed_bpm is not None
        assert math.isfinite(typed_bpm)
        assert abs(typed_bpm - legacy_bpm) <= AGREEMENT_BPM


# =========================================================================
# The bridge, scripted
# =========================================================================


class ScriptedDsp(Treatment):
    """A legacy DSP that says whatever the test queued, advancing seq when told to."""

    def __init__(self) -> None:
        self.queue: list[tuple[SignalQuality, int | None, bool]] = []
        self.seq: int = 0
        self.handed: list[list[float]] = []

    @override
    def treat_batch(
        self, samples: Sequence[Mapping[str, object]]
    ) -> tuple[list[dict[str, object]], Metrics]:
        values = samples[0]["values"]
        self.handed.append(list(cast("list[float]", values)))
        quality, bpm, fresh = self.queue.pop(0) if self.queue else (SignalQuality.GOOD, 70, True)
        self.seq += int(fresh)
        metric: dict[str, object] = {"quality": quality.value}
        if bpm is not None:
            metric["heartRate"] = bpm
        return [{"channel": ECG_CHANNEL, "values": [0.5]}], {ECG_CHANNEL: metric}

    @override
    def metric_seq(self, channel: str) -> int:
        return self.seq


@dataclass
class ScriptedTyped:
    """An independent processor that says ``bpm`` under ``quality``; records its windows."""

    bpm: float | None = 70.0
    quality: SignalQuality = SignalQuality.GOOD
    windows: list[tuple[float, ...]] = field(default_factory=list[tuple[float, ...]])

    async def __call__(self, window: Sequence[float], _fs: int, _at: Monotonic) -> SensorReading:
        self.windows.append(tuple(window))
        return reading(self.bpm, self.quality)


class Runtime:
    def __init__(self) -> None:
        self.seen: list[tuple[int, SignalQuality, Bpm | None]] = []
        self.at: list[Monotonic] = []

    def observe_ecg(
        self, now: Monotonic, seq: int, quality: SignalQuality, heart_rate: Bpm | None
    ) -> object:
        self.at.append(now)
        self.seen.append((seq, quality, heart_rate))
        return None


class Ring:
    def __init__(self) -> None:
        self.values: list[Millivolts] = []

    def record_ecg(self, values: Sequence[Millivolts]) -> int:
        self.values.extend(values)
        return len(self.values)


class Source:
    def __init__(self, batches: Sequence[SampleBatch]) -> None:
        self.batches: list[SampleBatch] = list(batches)

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:  # noqa: ARG002 - the protocol's name
        return self.batches.pop(0) if self.batches else None


async def _inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    return treat_ecg(treatment, batch)


async def _no_frame(_treatment: Treatment, _batch: SampleBatch) -> EcgFrame | None:
    return None


def ecg_batch(at_ms: float, values: Sequence[float], *, extra: bool = False) -> SampleBatch:
    channels = [ChannelData(channel=ECG_CHANNEL, values=list(values))]
    if extra:
        channels.append(ChannelData(channel="EDA", values=[1.0] * len(values)))
    return SampleBatch(timestamp=UnixMillis(EPOCH + round(at_ms)), channels=channels)


def stream(count: int, *, fs: int = FS, block: int = BLOCK, start: int = 0) -> list[SampleBatch]:
    """``count`` contiguous blocks, each sample its own index (so windows can be checked)."""
    return [
        ecg_batch(
            (start + i) * block * 1000 / fs,
            [float((start + i) * block + k) for k in range(block)],
        )
        for i in range(count)
    ]


@dataclass
class Bench:
    bridge: EcgBridge
    runtime: Runtime
    ring: Ring
    dsp: ScriptedDsp
    typed: ScriptedTyped
    source: Source


def bench(
    batches: Sequence[SampleBatch],
    *,
    fs: int = FS,
    typed: ScriptedTyped | None = None,
    link_gaps: list[int] | None = None,
    no_frame: bool = False,
) -> Bench:
    source = Source(batches)
    runtime = Runtime()
    ring = Ring()
    scripted = ScriptedDsp()
    independent = ScriptedTyped() if typed is None else typed
    counter = link_gaps

    def lost() -> int:
        return counter.pop(0) if counter else 0

    bridge = EcgBridge(
        clock=ManualClock(Monotonic(5.0)),
        source=source,
        treatment=scripted,
        heart_rate=runtime,
        waveform=ring,
        sample_rate=fs,
        treat=_no_frame if no_frame else _inline,
        max_batches=1000,
        confirm=independent,
        link_gaps=None if counter is None else lost,
    )
    return Bench(bridge, runtime, ring, scripted, independent, source)


def span(fs: int = FS) -> int:
    return round(float(LEGACY_WINDOW_S) * fs)


async def test_the_independent_processor_sees_exactly_the_legacy_window() -> None:
    """The last LEGACY_WINDOW_S of raw counts: the very samples BioSPPy was just given."""
    run = bench(stream(45))  # 9 s
    await run.bridge.pump()
    last = run.typed.windows[-1]
    assert len(last) == span() == window_samples(FS)
    assert last == tuple(float(i) for i in range(45 * BLOCK - span(), 45 * BLOCK))
    handed = [v for batch in run.dsp.handed for v in batch][-span() :]
    assert list(last) == handed
    assert all(q is SignalQuality.GOOD and bpm == 70 for _, q, bpm in run.runtime.seen)
    assert run.bridge.stats.last_verdict is Verdict.CONFIRMED
    assert run.bridge.continuous


def test_the_window_is_the_one_the_legacy_dsp_keeps() -> None:
    """``window_samples`` against the real ``ChannelProcessor`` it mirrors."""

    class _Window(Protocol):
        @property
        def maxlen(self) -> int | None: ...

    class _Processor(Protocol):
        _window: _Window

    class _Factory(Protocol):
        def __call__(self, channel: str, fs_in: int, fs_out: int) -> _Processor: ...

    class _Module(Protocol):
        ChannelProcessor: _Factory

    module = cast("_Module", importlib.import_module("src.signal_processing"))
    for fs in (100, 500, 1000):
        processor = module.ChannelProcessor("ECG", fs, 50)
        window = processor._window  # pyright: ignore[reportPrivateUsage] - no public route
        assert window.maxlen == window_samples(fs)


@pytest.mark.parametrize("jitter_ms", [-2.0, -1.0, 1.0, 2.0, CONTINUITY_TOLERANCE_MS])
async def test_timestamp_rounding_is_not_a_gap(jitter_ms: float) -> None:
    batches = stream(10)
    batches[6] = ecg_batch(6 * BLOCK + jitter_ms, batches[6].channels[0].values)
    run = bench(batches)
    await run.bridge.pump()
    assert run.bridge.stats.gaps == 0
    assert run.bridge.stats.withheld == 0


@pytest.mark.parametrize("jump_ms", [CONTINUITY_TOLERANCE_MS + 1.0, 200.0, -6.0, -400.0])
async def test_a_timestamp_jump_is_a_gap_and_no_rate_until_the_window_refills(
    jump_ms: float,
) -> None:
    before = stream(10)
    after = [
        ecg_batch(float(b.timestamp - EPOCH) + jump_ms, b.channels[0].values)
        for b in stream(60, start=10)
    ]
    run = bench(before + after)
    await run.bridge.pump()
    seen = run.runtime.seen
    assert run.bridge.stats.gaps == 1
    refill = span() // BLOCK  # batches until the window holds only post-gap samples
    assert all(usable(q, bpm) for _, q, bpm in seen[:10])
    stitched = seen[10 : 10 + refill - 1]
    assert stitched
    assert all(q is SignalQuality.NOISY and bpm is None for _, q, bpm in stitched)
    assert all(usable(q, bpm) for _, q, bpm in seen[10 + refill - 1 :])
    assert run.bridge.stats.withheld == refill - 1
    # The seqs are the DSP's own, withheld or not: the runtime still sees fresh evidence.
    assert [seq for seq, _, _ in seen] == list(range(1, 71))
    # Nothing stitched was ever shown to the independent processor either.
    first_confirmed = run.typed.windows[10]
    assert first_confirmed[0] == float(10 * BLOCK)


async def test_a_loss_the_link_reports_is_a_gap_even_when_the_timestamps_look_whole() -> None:
    """Frames the device dropped are filled with a held value: timing kept, content not."""
    counts = [0] * 12 + [3] * 60
    run = bench(stream(72), link_gaps=counts)
    await run.bridge.pump()
    assert run.bridge.stats.gaps == 1
    seen = run.runtime.seen
    assert usable(*seen[11][1:])
    assert not usable(*seen[12][1:])
    assert usable(*seen[-1][1:])


async def test_the_link_counter_baseline_is_not_a_gap() -> None:
    run = bench(stream(3), link_gaps=[7, 7, 7])
    await run.bridge.pump()
    assert run.bridge.stats.gaps == 0


async def test_a_batch_without_an_ecg_column_breaks_the_window() -> None:
    no_ecg = SampleBatch(
        timestamp=UnixMillis(EPOCH + 10 * BLOCK), channels=[ChannelData("EDA", [1.0] * BLOCK)]
    )
    run = bench([*stream(10), no_ecg, *stream(60, start=11)], link_gaps=[0] * 10 + [1] + [1] * 60)
    await run.bridge.pump()
    stats = run.bridge.stats
    assert stats.missing_channel == 1
    assert stats.gaps == 1  # the link's report, on the batch that had no ECG
    seen = run.runtime.seen
    assert len(seen) == 70
    refill = span() // BLOCK
    assert not any(usable(q, bpm) for _, q, bpm in seen[10 : 10 + refill - 1])
    assert usable(*seen[-1][1:])


async def test_no_frame_from_the_dsp_breaks_the_window() -> None:
    run = bench(stream(3), no_frame=True)
    await run.bridge.pump()
    assert run.runtime.seen == []
    assert run.bridge.stats.missing_channel == 3
    assert not run.bridge.continuous


async def test_other_channels_do_not_reach_the_window() -> None:
    batches = [ecg_batch(i * BLOCK, [float(i)] * BLOCK, extra=True) for i in range(5)]
    run = bench(batches)
    await run.bridge.pump()
    assert run.typed.windows[-1] == tuple(float(i) for i in range(5) for _ in range(BLOCK))


async def test_a_repeated_result_is_never_usable() -> None:
    run = bench(stream(3))
    run.dsp.queue = [
        (SignalQuality.GOOD, 70, True),
        (SignalQuality.GOOD, 70, False),
        (SignalQuality.GOOD, 70, True),
    ]
    await run.bridge.pump()
    assert run.runtime.seen == [
        (1, SignalQuality.GOOD, Bpm(70)),
        (1, SignalQuality.NOISY, None),
        (2, SignalQuality.GOOD, Bpm(70)),
    ]
    assert len(run.typed.windows) == 2  # not consulted for the repeat


async def test_a_reading_without_a_rate_passes_through_and_is_not_counted_withheld() -> None:
    run = bench(stream(2))
    run.dsp.queue = [(SignalQuality.MAINS_DOMINATED, None, True), (SignalQuality.GOOD, 70, True)]
    await run.bridge.pump()
    assert run.runtime.seen[0] == (1, SignalQuality.MAINS_DOMINATED, None)
    assert run.bridge.stats.withheld == 0
    assert len(run.typed.windows) == 1


@pytest.mark.parametrize(
    ("typed", "verdict"),
    [
        (ScriptedTyped(bpm=133.0), Verdict.DISAGREE),
        (ScriptedTyped(quality=SignalQuality.NOISY), Verdict.TYPED_REJECTED),
    ],
)
async def test_the_independent_processor_can_veto(typed: ScriptedTyped, verdict: Verdict) -> None:
    run = bench(stream(4), typed=typed)
    await run.bridge.pump()
    assert run.runtime.seen == [(seq, SignalQuality.NOISY, None) for seq in range(1, 5)]
    assert run.bridge.stats.withheld == 4
    assert run.bridge.stats.last_verdict is verdict
    assert run.bridge.stats.last_metrics == EcgMetrics(4, SignalQuality.NOISY, None)


async def test_the_waveform_is_untouched_by_a_veto() -> None:
    run = bench(stream(4), typed=ScriptedTyped(bpm=133.0))
    await run.bridge.pump()
    assert run.ring.values == [0.5] * 4


async def test_the_default_confirmation_runs_the_real_processor_off_the_loop() -> None:
    raw = _clean(70.0, 8 * FS)
    off = await confirm_off_loop(raw, FS, Monotonic(3.0))
    assert off == typed_ecg.make().process(raw, FS, Monotonic(3.0))
    assert off.quality is SignalQuality.GOOD


# --- Properties -------------------------------------------------------------

_SMALL_FS: Final[int] = 50
"""Tiny rate, tiny window (400 samples): hypothesis can cover many windows quickly."""


@dataclass(frozen=True, slots=True)
class Step:
    size: int
    offset_ms: float
    """0 = continuous; otherwise a jump well outside the tolerance, either way."""
    legacy_quality: SignalQuality
    legacy_bpm: int | None
    fresh: bool
    typed_quality: SignalQuality
    typed_bpm: float | None


_STEPS = st.lists(
    st.builds(
        Step,
        size=st.integers(min_value=1, max_value=120),
        offset_ms=st.one_of(
            st.just(0.0),
            st.just(0.0),
            st.just(0.0),
            st.floats(min_value=10.0, max_value=5000.0),
            st.floats(min_value=-5000.0, max_value=-10.0),
        ),
        legacy_quality=_QUALITIES,
        legacy_bpm=st.one_of(st.none(), st.integers(min_value=30, max_value=220)),
        fresh=st.booleans(),
        typed_quality=_QUALITIES,
        typed_bpm=_TYPED_BPMS,
    ),
    min_size=1,
    max_size=60,
)


@dataclass
class _StepTyped:
    """Answers for the step whose batch the scripted DSP was handed last."""

    steps: list[Step]
    dsp: ScriptedDsp

    async def __call__(self, _w: Sequence[float], _fs: int, _at: Monotonic) -> SensorReading:
        step = self.steps[len(self.dsp.handed) - 1]
        return reading(step.typed_bpm, step.typed_quality)


async def _run_steps(steps: list[Step]) -> tuple[list[tuple[int, SignalQuality, Bpm | None]], int]:
    batches: list[SampleBatch] = []
    at = 0.0
    for step in steps:
        at += step.offset_ms
        batches.append(ecg_batch(at, [512.0] * step.size))
        at += step.size * 1000.0 / _SMALL_FS
    runtime = Runtime()
    scripted = ScriptedDsp()
    scripted.queue = [(s.legacy_quality, s.legacy_bpm, s.fresh) for s in steps]
    bridge = EcgBridge(
        clock=ManualClock(),
        source=Source(batches),
        treatment=scripted,
        heart_rate=runtime,
        waveform=Ring(),
        sample_rate=_SMALL_FS,
        treat=_inline,
        max_batches=len(steps),
        confirm=_StepTyped(steps, scripted),
    )
    await bridge.pump()
    return runtime.seen, bridge.stats.gaps


@settings(deadline=None, max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(steps=_STEPS)
def test_for_any_stream_a_usable_rate_was_confirmed_on_an_unbroken_window(
    steps: list[Step],
) -> None:
    """The two guarantees: both processors good and agreeing; no gap inside the window."""
    seen, gaps = asyncio.run(_run_steps(steps))
    assert len(seen) == len(steps)
    assert gaps == sum(1 for s in steps[1:] if s.offset_ms != 0.0)
    unbroken = 0  # samples since the last discontinuity, counted here independently
    last_seq = 0
    seq = 0
    for i, (step, (got_seq, quality, bpm)) in enumerate(zip(steps, seen, strict=True)):
        unbroken = step.size if i > 0 and step.offset_ms != 0.0 else unbroken + step.size
        fresh = step.fresh
        seq += int(fresh)
        assert got_seq == seq
        if not usable(quality, bpm):
            continue
        assert bpm is not None
        # Both processors graded it good and agree.
        assert step.legacy_quality is SignalQuality.GOOD
        assert step.legacy_bpm == bpm
        assert step.typed_quality is SignalQuality.GOOD
        assert step.typed_bpm is not None
        assert abs(step.typed_bpm - bpm) <= AGREEMENT_BPM
        # Fresh, and no discontinuity inside the legacy window (or none since the start).
        assert seq > last_seq
        before_first_gap = all(s.offset_ms == 0.0 for s in steps[1 : i + 1])
        assert before_first_gap or unbroken >= span(_SMALL_FS)
        last_seq = seq


# =========================================================================
# The REAL legacy DSP and the REAL typed processor, on synthesised ECG
# =========================================================================


def _subject(bpm: float, g_load: float = 0.0) -> SubjectState:
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(g_load),
        heart_rate=Bpm(round(bpm)),
        rr_interval=Seconds(60.0 / bpm),
        steady_state=Bpm(round(bpm)),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def _clean(bpm: float, count: int, g_load: float = 0.0) -> list[float]:
    synth = EcgSynthesizer(EcgConfig())
    return [float(v) for v in synth.render(count, _subject(bpm, g_load))]


def _white_noise(count: int, seed: int) -> list[float]:
    noise = np.random.default_rng(seed).normal(512.0, 60.0, count)
    return [min(1023.0, max(0.0, float(round(v)))) for v in dsp.values_of(noise)]


def _batches(raw: Sequence[float], *, keep: set[int] | None = None) -> list[SampleBatch]:
    """``raw`` in 200 ms blocks stamped at their true instants; ``keep`` drops the others."""
    return [
        ecg_batch(i * BLOCK * 1000 / FS, raw[i * BLOCK : (i + 1) * BLOCK])
        for i in range(len(raw) // BLOCK)
        if keep is None or i in keep
    ]


@dataclass(frozen=True, slots=True)
class Outcome:
    offered: list[EcgMetrics]
    """What the legacy DSP produced, per batch: what reached the runtime BEFORE the fix."""
    handed: list[tuple[int, SignalQuality, Bpm | None]]
    """What the runtime was handed AFTER the fix."""
    gaps: int

    @property
    def offered_rates(self) -> list[int]:
        return [m.bpm for m in self.offered if m.bpm is not None]

    @property
    def handed_rates(self) -> list[int]:
        return [bpm for _, q, bpm in self.handed if usable(q, bpm) and bpm is not None]


async def _real(batches: Sequence[SampleBatch], legacy_override: int | None = None) -> Outcome:
    offered: list[EcgMetrics] = []

    async def treat(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
        frame = treat_ecg(treatment, batch)
        if frame is not None and legacy_override is not None and frame.metrics.bpm is not None:
            frame = EcgFrame(
                millivolts=frame.millivolts,
                metrics=EcgMetrics(frame.metrics.seq, frame.metrics.quality, Bpm(legacy_override)),
            )
        if frame is not None:
            offered.append(frame.metrics)
        return frame

    async def confirm(window: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        return typed_ecg.make().process(window, fs, at)

    runtime = Runtime()
    bridge = EcgBridge(
        clock=ManualClock(),
        source=Source(batches),
        treatment=load_treatment(FS, 250),
        heart_rate=runtime,
        waveform=Ring(),
        sample_rate=FS,
        treat=treat,
        max_batches=1000,
        confirm=confirm,
    )
    await bridge.pump()
    return Outcome(offered=offered, handed=runtime.seen, gaps=bridge.stats.gaps)


async def test_real_clean_ecg_is_confirmed_and_accurate() -> None:
    """Agreement: a clean 70 bpm heart is handed on, within 3 bpm, once the window is judged."""
    out = await _real(_batches(_clean(70.0, 20 * FS)))
    assert out.gaps == 0
    rates = out.handed_rates
    assert len(rates) >= 60  # from ~4 s (the typed processor's minimum) to 20 s
    assert all(abs(rate - 70) <= 3 for rate in rates)


async def test_real_white_noise_never_reaches_the_runtime_as_a_rate() -> None:
    """BEFORE: the legacy DSP read ~133 bpm from pure noise, graded good. AFTER: nothing."""
    out = await _real(_batches(_white_noise(20 * FS, seed=1)))
    assert out.offered_rates, "the legacy DSP no longer offers a rate: re-pin this evidence"
    assert max(out.offered_rates) > 100
    assert out.handed_rates == []
    assert all(q is not SignalQuality.GOOD for _, q, _ in out.handed[20:])


@pytest.mark.parametrize("g_load", [0.15, 0.2])
async def test_real_motion_artefact_never_reaches_the_runtime_as_a_false_rate(
    g_load: float,
) -> None:
    """BEFORE: 60 bpm under 0.15-0.2 g of vibration was read at 110-140 bpm, graded good."""
    out = await _real(_batches(_clean(60.0, 20 * FS, g_load=g_load)))
    assert any(abs(rate - 60) > 20 for rate in out.offered_rates)
    assert all(abs(rate - 60) <= AGREEMENT_BPM for rate in out.handed_rates)


async def test_real_nan_samples_never_reach_the_runtime_as_a_rate() -> None:
    raw = _clean(70.0, 20 * FS)
    corrupt = [math.nan if 10 * FS <= i < 12 * FS and i % 7 == 0 else v for i, v in enumerate(raw)]
    out = await _real(_batches(corrupt))
    after = out.handed[50:]  # from the first NaN batch on
    window = span() // BLOCK
    assert not any(usable(q, bpm) for _, q, bpm in after[: 10 + window - 1])


@pytest.mark.parametrize(("every", "fabricated_before"), [(2, True), (4, False)])
async def test_real_lost_batches_never_reach_the_runtime_as_a_rate(
    every: int, fabricated_before: bool
) -> None:
    """Only one batch in ``every`` kept after 10 s. AFTER the fix: every one a gap, no rate.

    BEFORE, at rest: with one batch in two lost the legacy DSP read 126-134 bpm
    for a true 70, graded good (the simulation's WARMUP case, three in four
    lost under motion, read 47 then 135). At rest three in four happens to
    stay near 70, then goes silent; it is kept for the gap count.
    """
    raw = _clean(70.0, 40 * FS)
    kept = sorted(i for i in range(200) if i < 50 or i % every == 0)
    out = await _real(_batches(raw, keep=set(kept)))
    broken = [n for n, i in enumerate(kept) if i > 50]  # batch 50 still follows 49
    assert out.gaps == len(broken)
    assert not any(usable(*out.handed[n][1:]) for n in broken)
    wrong = [m.bpm for n, m in enumerate(out.offered) if n in broken and m.bpm is not None]
    if fabricated_before:
        assert any(abs(bpm - 70) > 20 for bpm in wrong), wrong


async def test_real_one_gap_withholds_the_rate_until_a_clean_window_has_refilled() -> None:
    """A stitched window: 1 s lost at 10 s; no rate for 8 s after, then a true one again."""
    raw = _clean(70.0, 30 * FS)
    kept = {i for i in range(150) if not 50 <= i < 55}
    out = await _real(_batches(raw, keep=kept))
    assert out.gaps == 1
    refill = span() // BLOCK
    after_gap = out.handed[50:]
    assert not any(usable(q, bpm) for _, q, bpm in after_gap[: refill - 1])
    later = [bpm for _, q, bpm in after_gap[refill:] if usable(q, bpm) and bpm is not None]
    assert later
    assert all(abs(rate - 70) <= 3 for rate in later)


async def test_real_disagreement_is_withheld() -> None:
    """A legacy 100 bpm against a clean 70 bpm window: the typed processor vetoes it."""
    out = await _real(_batches(_clean(70.0, 12 * FS)), legacy_override=100)
    assert out.offered_rates
    assert out.handed_rates == []
