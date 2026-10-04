"""The typed boundary between the ECG DSP and everything that acts on a heart rate.

``src/signal_processing.py`` is still outside both type checkers: it hands back
``str``-keyed dicts of ``object``. The runtime needs a heart rate it can
regulate a motor on, which means three facts it can trust:

* ``seq`` - a freshness counter that advances only when the DSP produced a NEW
  result. ``treat_batch`` re-emits its previous metrics dict when extraction
  fails, so "unchanged" does not mean "still true"; the counter is what lets
  the runtime ignore a repeat (read from ``SignalTreatment.metric_seq``, never
  counted here per call, which would defeat the gate).
* ``quality`` - parsed with :meth:`SignalQuality.from_metric`, which is total
  and reads anything unknown as ``NO_SIGNAL``.
* ``bpm`` - present only when the quality is ``GOOD`` **and** the number is an
  integer inside the plausible range. Never invented, never passed through
  unchecked.

This is "parse, don't validate" (contract rule 6): the checks live here, once,
and everything past :class:`EcgMetrics` trusts the types.

Independent confirmation (:class:`EcgBridge`)
---------------------------------------------
Parsing is not enough. The legacy grader passes white noise, random ADC counts
and motion artefact as ``good``, and BioSPPy then finds "beats" in them: 133
bpm from pure noise, 116-146 bpm from a corrupted stream, 124-140 bpm for a
subject truly at 60 under 0.2 g of vibration, 47 then 135 bpm for a true 71
when three batches in four are lost. So before a heart rate reaches the
runtime as usable, the bridge demands three things of it (:func:`judge`):

1. the legacy DSP graded the window ``good`` and produced a plausible rate
   (the parse above);
2. the typed, independent processor ``src/sensors/ecg.py`` (Pan-Tompkins
   detection, RR-regularity and missing-beat grading) grades the **same**
   window - the last :data:`LEGACY_WINDOW_S` of raw ECG counts, exactly what
   BioSPPy was just given - ``GOOD`` as well;
3. the two rates agree within :data:`AGREEMENT_BPM`.

And the window must be one unbroken stretch of samples (:data:`CONTINUITY_TOLERANCE_MS`):
a window stitched across lost batches has RR intervals that never existed.

Anything else goes to the runtime as ``NOISY`` with no rate and the DSP's own
``seq``, which the runtime's tracker sees as fresh but unusable evidence, so
the heart rate goes *stale* (``hr_stale``: FREEZE, REDUCE, RAMP_DOWN) instead
of being a made-up number the control law and the physiological rules act on.

The rate handed on when all three hold is the legacy one: the typed processor
only ever vetoes, it never commands (which keeps ``src/sensors/ecg.py``'s
"this heart rate never commands the motor" true), and every characterisation
of the control loop was made on the legacy number. Within the tolerance the
choice moves the rate by at most :data:`AGREEMENT_BPM`.

The DSP module is loaded through ``importlib`` and cast to :class:`Treatment`,
a Protocol naming the two members used, for the reason ``tests/test_sim.py``
gives: a static import would pull an unchecked module's diagnostics into this
checked one. The cast is the boundary; ``tests/test_ecg_pipeline.py`` runs the
real ``SignalTreatment`` through it so the Protocol cannot quietly become a lie.

See .claude/skills/anheart-strict-python/SKILL.md rules 5, 6 and 8.
"""

from __future__ import annotations

import asyncio
import importlib
import math
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol, cast, final

from src.bitalino_client import SampleBatch
from src.clock import Clock
from src.sensors import ecg as typed_ecg
from src.sensors.base import SensorKind, SensorReading
from src.training.hr_control import MAX_PLAUSIBLE_BPM, MIN_PLAUSIBLE_BPM
from src.training.types import SignalQuality
from src.units import Bpm, Millivolts, Monotonic, Seconds, UnixMillis

ECG_CHANNEL: Final[str] = "ECG"
"""The channel name, as ``src.bitalino_client.CHANNEL_NAMES`` labels A1."""

QUALITY_KEY: Final[str] = "quality"
HEART_RATE_KEY: Final[str] = "heartRate"
TREATED_CHANNEL_KEY: Final[str] = "channel"
TREATED_VALUES_KEY: Final[str] = "values"


class Treatment(Protocol):
    """The two members of ``SignalTreatment`` this boundary uses."""

    def treat_batch(
        self, samples: Sequence[Mapping[str, object]]
    ) -> tuple[Sequence[Mapping[str, object]], Mapping[str, Mapping[str, object]]]: ...

    def metric_seq(self, channel: str) -> int: ...


class _TreatmentFactory(Protocol):
    def __call__(self, fs_in: int, fs_out: int) -> Treatment: ...


class _SignalProcessingModule(Protocol):
    SignalTreatment: _TreatmentFactory


def load_treatment(fs_in: int, fs_out: int) -> Treatment:
    """A real ``SignalTreatment``, seen through :class:`Treatment`."""
    module = cast("_SignalProcessingModule", importlib.import_module("src.signal_processing"))
    return module.SignalTreatment(fs_in, fs_out)


@dataclass(frozen=True, slots=True)
class EcgMetrics:
    """One heart-rate reading as the runtime may act on it."""

    seq: int
    """Advances only on a fresh DSP result. 0 = nothing computed yet."""

    quality: SignalQuality

    bpm: Bpm | None
    """``None`` unless ``quality`` is ``GOOD`` and the value was plausible."""


@dataclass(frozen=True, slots=True)
class EcgFrame:
    """One treated ECG batch: the waveform to draw and the reading to act on."""

    millivolts: tuple[Millivolts, ...]
    """The filtered, decimated waveform, finite values only."""

    metrics: EcgMetrics


def parse_ecg_metrics(metrics: Mapping[str, object] | None, seq: int) -> EcgMetrics:
    """Turn the DSP's untyped ECG metrics dict into an :class:`EcgMetrics`. Total.

    ``None`` (no metrics yet), a non-string quality, a missing, non-integer
    (``bool`` included, which Python calls an ``int``) or implausible heart
    rate all degrade towards "no usable heart rate", never towards a number.
    """
    if metrics is None:
        return EcgMetrics(seq=seq, quality=SignalQuality.NO_SIGNAL, bpm=None)
    raw_quality = metrics.get(QUALITY_KEY)
    quality = SignalQuality.from_metric(raw_quality if isinstance(raw_quality, str) else None)
    return EcgMetrics(seq=seq, quality=quality, bpm=_plausible_bpm(quality, metrics))


def _plausible_bpm(quality: SignalQuality, metrics: Mapping[str, object]) -> Bpm | None:
    if not quality.is_trustworthy:
        return None
    raw = metrics.get(HEART_RATE_KEY)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    if raw < MIN_PLAUSIBLE_BPM or raw > MAX_PLAUSIBLE_BPM:
        return None
    return Bpm(raw)


def treat_ecg(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    """Push the ECG column of ``batch`` through the DSP and parse what comes back.

    Only the ECG channel is handed over: the local console acts on nothing
    else, and every extra channel costs a BioSPPy extraction per batch.
    ``None`` when the batch carries no ECG channel (a misconfigured channel
    list), which a caller must treat as "no reading", not as a flat line.
    """
    ecg = next((channel for channel in batch.channels if channel.channel == ECG_CHANNEL), None)
    if ecg is None:
        return None
    treated, metrics = treatment.treat_batch(
        [{TREATED_CHANNEL_KEY: ECG_CHANNEL, TREATED_VALUES_KEY: list(ecg.values)}]
    )
    return EcgFrame(
        millivolts=_waveform(treated),
        metrics=parse_ecg_metrics(metrics.get(ECG_CHANNEL), treatment.metric_seq(ECG_CHANNEL)),
    )


def _waveform(treated: Sequence[Mapping[str, object]]) -> tuple[Millivolts, ...]:
    """The ECG waveform out of the treated list, finite numbers only.

    Display-only data, so a non-finite or non-numeric value is dropped rather
    than failing the batch: it must never reach a JSON frame as NaN.
    """
    for entry in treated:
        if entry.get(TREATED_CHANNEL_KEY) != ECG_CHANNEL:
            continue
        values = entry.get(TREATED_VALUES_KEY)
        if not isinstance(values, list):
            return ()
        items = cast("list[object]", values)
        return tuple(
            Millivolts(float(value))
            for value in items
            if isinstance(value, float | int)
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    return ()


# =========================================================================
# Independent confirmation: two processors must agree before a rate is usable
# =========================================================================

LEGACY_WINDOW_S: Final[Seconds] = Seconds(8.0)
"""The window BioSPPy's heart rate is computed over (``ChannelProcessor``'s
``metric_window_s``). The typed processor is run on exactly this much, so both
judge the same samples; ``tests/test_ecg_pipeline.py`` checks the figure
against the real module, so the two cannot drift apart unnoticed."""

AGREEMENT_BPM: Final[float] = 5.0
"""Largest difference between the two rates for either to be believed.

Measured on the synthesiser's ECG (respiratory sinus arrhythmia on), both
processors fed the same 8 s windows every 200 ms along 20 s streams: on a
clean signal the two differ by at most 2.5 bpm from 40 to 180 bpm (0.8 at 50,
1.1 at 70, 1.8 at 140; the legacy number is an integer, so 0.5 of that is its
rounding), and by 4.7 bpm at 200. 5 bpm clears the whole clean range, while
every failure seen is caught by an order of magnitude: under 0.1-0.15 g of
motion artefact the legacy DSP still says ``good`` and the two disagree by 30
to 125 bpm (it doubles a 40-70 bpm heart); noise, corruption and lost
batches are further off still. It is also well inside the 20 bpm the
failure tests call a fabricated rate. Where a real heart at ~200 bpm with
some motion is vetoed (up to 6.3 bpm apart at 0.05 g), the cost is a stale
reading (FREEZE), the safe side, at a rate no programme targets.
"""

MILLIS_PER_SECOND: Final[float] = 1000.0

CONTINUITY_TOLERANCE_MS: Final[float] = 5.0
"""How far a batch's first-sample timestamp may sit from where the previous
batch ended before the stream is declared broken.

Timestamps are whole milliseconds derived from the sample count
(``src/bitalino_client.py``, ``src/sim/bitalino.py``), so a continuous stream
lands within +/- 2 ms of the expected instant from rounding alone. The
smallest loss that matters is a whole chunk of frames (tens of ms and up); a
5 ms slip that went unnoticed would move one RR interval by 5 ms, under 0.6
bpm at 70 bpm."""


@unique
class Verdict(Enum):
    """What the confirmation made of one reading. Wire strings, for the link panel."""

    CONFIRMED = "confirmed"
    """Both processors graded the window good and agree: the rate is usable."""

    NO_RATE = "no_rate"
    """The legacy DSP offered no rate: its reading is passed on unchanged."""

    REPEATED = "repeated"
    """Not a fresh result (the DSP re-emitted an old one): never usable."""

    STITCHED = "stitched"
    """The window still holds samples from before a discontinuity."""

    TYPED_REJECTED = "typed_rejected"
    """The independent processor did not grade the window good."""

    DISAGREE = "disagree"
    """Both graded it good, but the rates differ by more than AGREEMENT_BPM."""


@dataclass(frozen=True, slots=True)
class Confirmation:
    """One reading after the confirmation, and why it came out that way."""

    verdict: Verdict
    metrics: EcgMetrics
    """What the runtime is handed. A rate only when ``verdict`` is CONFIRMED."""

    typed_bpm: float | None = None
    """The independent processor's rate, when it produced one."""


def withheld(legacy: EcgMetrics, verdict: Verdict, typed_bpm: float | None = None) -> Confirmation:
    """``legacy`` stripped of its rate: NOISY, no bpm, the SAME ``seq``.

    The same seq, so the runtime's tracker counts it as new evidence (it was),
    finds it unusable, and lets the heart rate go stale: nothing is invented,
    and nothing is hidden from the freshness gate either.
    """
    return Confirmation(
        verdict=verdict,
        metrics=EcgMetrics(seq=legacy.seq, quality=SignalQuality.NOISY, bpm=None),
        typed_bpm=typed_bpm,
    )


def typed_rate(reading: SensorReading) -> float | None:
    """The independent heart rate of ``reading``: only from a GOOD ECG window, only finite."""
    if reading.kind is not SensorKind.ECG or reading.quality is not SignalQuality.GOOD:
        return None
    metric = reading.metric(typed_ecg.HEART_RATE)
    value = None if metric is None else metric.value
    if value is None or not math.isfinite(value):
        return None
    return value


def judge(legacy: EcgMetrics, typed: SensorReading) -> Confirmation:
    """Whether the legacy rate survives the independent processor's view of the same window.

    Pure and total. Continuity and freshness are the bridge's to establish
    before it calls this; here only the two verdicts and the two numbers meet.
    """
    if legacy.bpm is None:
        return Confirmation(verdict=Verdict.NO_RATE, metrics=legacy)
    rate = typed_rate(typed)
    if rate is None:
        return withheld(legacy, Verdict.TYPED_REJECTED)
    if abs(rate - legacy.bpm) > AGREEMENT_BPM:
        return withheld(legacy, Verdict.DISAGREE, rate)
    return Confirmation(verdict=Verdict.CONFIRMED, metrics=legacy, typed_bpm=rate)


# =========================================================================
# The bridge: acquisition -> DSP -> waveform ring + runtime
# =========================================================================

BATCHES_PER_SECOND: Final[int] = 5
"""One read every 200 ms: a fifth of a second of samples per batch."""

DEFAULT_MAX_BATCHES_PER_PUMP: Final[int] = 10
"""Most batches drained by one :meth:`EcgBridge.pump`: two seconds of catch-up.

Bounded so that a backlog (a stalled loop, a slow DSP) is worked off over a
few pumps instead of in one long burst that would delay the control tick.
"""


class SampleSource(Protocol):
    """The one call the bridge makes on an acquisition client, real or simulated."""

    async def read_samples(self, count: int = ...) -> SampleBatch | None: ...


type BatchTap = Callable[[SampleBatch], None]
"""Receives each acquired batch, every channel, before the ECG is treated. Must not raise."""


class HeartRateSink(Protocol):
    """Where a parsed reading goes: ``TrainingRuntime.observe_ecg``.

    The return value is not used here: the runtime's own tracker is the gate,
    and a rejection is its to report, not this bridge's to second-guess.
    """

    def observe_ecg(
        self, now: Monotonic, seq: int, quality: SignalQuality, heart_rate: Bpm | None
    ) -> object: ...


class WaveformSink(Protocol):
    """Where the waveform goes: ``TelemetryHub.record_ecg``."""

    def record_ecg(self, values: Sequence[Millivolts]) -> int: ...


type TreatFunction = Callable[[Treatment, SampleBatch], Awaitable[EcgFrame | None]]
"""Runs :func:`treat_ecg` somewhere. Injected so a test can run it inline."""


async def treat_off_loop(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    """Run the DSP on a worker thread.

    BioSPPy's R-peak extraction over an 8 s window costs tens of milliseconds
    on a laptop and more on the Pi. Run on the event loop it would delay the
    control tick (and the ``loop_stall`` rule measures exactly that), so it
    goes to a thread (contract rule 8). One batch at a time: the bridge awaits
    each result before the next, so the treatment is never used concurrently.
    """
    return await asyncio.to_thread(treat_ecg, treatment, batch)


type ConfirmFunction = Callable[[Sequence[float], int, Monotonic], Awaitable[SensorReading]]
"""Runs the independent processor on a window of raw ECG counts. Injected like the DSP."""


async def confirm_off_loop(
    window: Sequence[float], sample_rate: int, at: Monotonic
) -> SensorReading:
    """The typed ECG processor on a worker thread, for the reason :func:`treat_off_loop` gives.

    About 8 ms per 8 s window on a laptop, more on the Pi, once per fresh
    reading. The processor is stateless, so a fresh one per call is free.
    """
    return await asyncio.to_thread(typed_ecg.make().process, window, sample_rate, at)


type LinkGaps = Callable[[], int]
"""A cumulative count of samples the acquisition link lost or synthesised.

For the real client, ``link_stats().filled_samples +
link_stats().dropped_backlog_samples``: frames the device dropped (filled with
a held value, and undercounted by multiples of 16) and samples discarded
unread. Any advance is a discontinuity. Must not raise.
"""


@dataclass(frozen=True, slots=True)
class EcgBridgeStats:
    """What the bridge has done, for the operator's link panel."""

    batches: int = 0
    """Batches read from the acquisition client and treated."""

    samples: int = 0
    """Raw ECG samples (at the acquisition rate) that went through the DSP."""

    missing_channel: int = 0
    """Batches that carried no ECG column: a channel misconfiguration."""

    last_batch_at: Monotonic | None = None
    """When the last batch was treated, by the injected clock."""

    last_metrics: EcgMetrics | None = None
    """The last reading, AS HANDED TO THE RUNTIME (after the confirmation)."""

    gaps: int = 0
    """Discontinuities in the ECG stream: a timestamp jump, or a link-reported loss."""

    withheld: int = 0
    """Legacy rates NOT passed on as usable (stitched, rejected, disagreeing, repeated)."""

    last_verdict: Verdict | None = None
    """What the confirmation made of the last reading."""


@final
class EcgBridge:
    """Acquisition -> DSP -> confirmation -> (waveform ring, runtime), one :meth:`pump` at a time.

    Mutable, and owned by the one event loop that calls :meth:`pump`; only the
    DSP and the typed processor run on worker threads, and the bridge awaits
    each before touching its own fields again.

    It invents nothing: no batch means no reading, and a reading goes to the
    runtime with the DSP's own ``seq``, so a re-emitted result is recognised
    there as old evidence and never counted as fresh. A rate goes on as usable
    only when it survives the confirmation described in the module docstring.

    **Continuity.** The bridge keeps its own copy of the last
    :data:`LEGACY_WINDOW_S` of raw ECG, the same samples BioSPPy holds. When
    the stream breaks (a batch that does not start where the previous one
    ended, a loss the link reports, a batch without an ECG column) the copy is
    emptied and no rate is usable until a whole window of unbroken samples has
    come in since. By then BioSPPy's own rolling window, which is exactly that
    long, holds nothing from before the break either: that is its reset. Its
    streaming display filter is left alone, so the waveform is unchanged.
    """

    __slots__ = (
        "_block",
        "_clock",
        "_confirm",
        "_expected_ms",
        "_heart_rate",
        "_last_seq",
        "_link_gaps",
        "_link_seen",
        "_max_batches",
        "_refill",
        "_sample_rate",
        "_source",
        "_span",
        "_stats",
        "_tap",
        "_treat",
        "_treatment",
        "_waveform",
        "_window",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        source: SampleSource,
        treatment: Treatment,
        heart_rate: HeartRateSink,
        waveform: WaveformSink,
        sample_rate: int,
        treat: TreatFunction = treat_off_loop,
        max_batches: int = DEFAULT_MAX_BATCHES_PER_PUMP,
        tap: BatchTap | None = None,
        confirm: ConfirmFunction = confirm_off_loop,
        link_gaps: LinkGaps | None = None,
    ) -> None:
        """Raises ``ValueError`` on a rate too low to make a batch, or no batch budget.

        ``tap`` receives every batch as acquired, all channels, before the ECG
        is treated: the multi-sensor hub's feed. The heart-rate path itself is
        handed the ECG column ONLY, whatever else is acquired, so adding a
        sensor can never change what the controller is told.

        ``confirm`` runs the independent processor (off the loop by default).
        ``link_gaps``, when given, is the acquisition client's loss counter
        (see :data:`LinkGaps`); without it, continuity is judged from the
        batch timestamps alone.
        """
        block = sample_rate // BATCHES_PER_SECOND
        if block < 1:
            raise ValueError(f"sample rate {sample_rate} Hz is too low for 200 ms batches")
        if max_batches < 1:
            raise ValueError(f"max_batches must be at least 1, got {max_batches}")
        self._clock: Clock = clock
        self._source: SampleSource = source
        self._treatment: Treatment = treatment
        self._heart_rate: HeartRateSink = heart_rate
        self._waveform: WaveformSink = waveform
        self._treat: TreatFunction = treat
        self._confirm: ConfirmFunction = confirm
        self._tap: BatchTap | None = tap
        self._link_gaps: LinkGaps | None = link_gaps
        self._block: int = block
        self._sample_rate: int = sample_rate
        self._max_batches: int = max_batches
        self._stats: EcgBridgeStats = EcgBridgeStats()
        # The continuity state. Mutable, owned by the loop like the rest.
        self._span: int = window_samples(sample_rate)
        self._window: deque[float] = deque(maxlen=self._span)
        self._expected_ms: float | None = None
        """Where the next batch should start (ms); None after a break or before the first."""
        self._refill: int = 0
        """Samples still needed before the window holds nothing from before a break."""
        self._link_seen: int | None = None
        self._last_seq: int = 0
        """The highest DSP seq seen: a reading is fresh only above it."""

    @property
    def stats(self) -> EcgBridgeStats:
        """What has gone through so far."""
        return self._stats

    @property
    def block(self) -> int:
        """Samples per batch requested from the source."""
        return self._block

    @property
    def continuous(self) -> bool:
        """Whether the window is one unbroken stretch (a rate may be confirmed on it)."""
        return self._refill == 0

    async def pump(self) -> int:
        """Drain what the source has ready, up to the batch budget. Returns batches treated."""
        treated = 0
        while treated < self._max_batches:
            batch = await self._source.read_samples(self._block)
            if batch is None:
                break
            await self._one(batch)
            treated += 1
        return treated

    async def _one(self, batch: SampleBatch) -> None:
        if self._tap is not None:
            self._tap(batch)
        ecg = ecg_only(batch)
        gap = self._follow(ecg)
        frame = await self._treat(self._treatment, ecg)
        now = self._clock.monotonic()
        stats = self._stats
        if frame is None:
            self._break()  # nothing reached the DSP: its window no longer matches ours
            self._stats = EcgBridgeStats(
                batches=stats.batches + 1,
                samples=stats.samples,
                missing_channel=stats.missing_channel + 1,
                last_batch_at=now,
                last_metrics=stats.last_metrics,
                gaps=stats.gaps + int(gap),
                withheld=stats.withheld,
                last_verdict=stats.last_verdict,
            )
            return
        self._waveform.record_ecg(frame.millivolts)
        confirmation = await self._vet(frame.metrics, now)
        metrics = confirmation.metrics
        self._heart_rate.observe_ecg(now, metrics.seq, metrics.quality, metrics.bpm)
        held = frame.metrics.bpm is not None and confirmation.verdict is not Verdict.CONFIRMED
        self._stats = EcgBridgeStats(
            batches=stats.batches + 1,
            samples=stats.samples + _ecg_sample_count(batch),
            missing_channel=stats.missing_channel,
            last_batch_at=now,
            last_metrics=metrics,
            gaps=stats.gaps + int(gap),
            withheld=stats.withheld + int(held),
            last_verdict=confirmation.verdict,
        )

    def _follow(self, ecg: SampleBatch) -> bool:
        """Append the ECG column to the window. Returns whether the stream broke before it."""
        lost = self._link_lost()
        column = next((c.values for c in ecg.channels if c.channel == ECG_CHANNEL), None)
        if column is None:
            self._break()
            return lost
        gap = lost or self._jumped(ecg.timestamp)
        if gap:
            self._break()
        self._expected_ms = ecg.timestamp + len(column) * MILLIS_PER_SECOND / self._sample_rate
        self._window.extend(column)
        self._refill = max(0, self._refill - len(column))
        return gap

    def _link_lost(self) -> bool:
        """Whether the link's loss counter advanced since the previous batch."""
        if self._link_gaps is None:
            return False
        count = self._link_gaps()
        seen = self._link_seen
        self._link_seen = count
        return seen is not None and count != seen

    def _jumped(self, timestamp: UnixMillis) -> bool:
        expected = self._expected_ms
        return expected is not None and abs(timestamp - expected) > CONTINUITY_TOLERANCE_MS

    def _break(self) -> None:
        """Forget the window: nothing is confirmed until a whole unbroken one has refilled."""
        self._window.clear()
        self._expected_ms = None
        self._refill = self._span

    async def _vet(self, legacy: EcgMetrics, now: Monotonic) -> Confirmation:
        """The confirmation of one parsed reading (module docstring)."""
        fresh = legacy.seq > self._last_seq
        self._last_seq = max(self._last_seq, legacy.seq)
        if legacy.bpm is None:
            return Confirmation(verdict=Verdict.NO_RATE, metrics=legacy)
        if not fresh:
            return withheld(legacy, Verdict.REPEATED)
        if not self.continuous:
            return withheld(legacy, Verdict.STITCHED)
        return judge(legacy, await self._confirm(tuple(self._window), self._sample_rate, now))


def window_samples(sample_rate: int) -> int:
    """Samples in :data:`LEGACY_WINDOW_S` at ``sample_rate``, as ``ChannelProcessor`` sizes it."""
    return int(LEGACY_WINDOW_S * sample_rate)


def ecg_only(batch: SampleBatch) -> SampleBatch:
    """``batch`` with every channel but the ECG removed (the same object if nothing to remove)."""
    kept = tuple(c for c in batch.channels if c.channel == ECG_CHANNEL)
    if len(kept) == len(batch.channels):
        return batch
    return SampleBatch(timestamp=batch.timestamp, channels=kept)


def _ecg_sample_count(batch: SampleBatch) -> int:
    """Samples in the ECG column of ``batch`` (0 if absent)."""
    return next((len(c.values) for c in batch.channels if c.channel == ECG_CHANNEL), 0)
