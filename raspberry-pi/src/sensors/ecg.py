"""Electrocardiogramme (ECG): the processor for BITalino channel A1 (10 bits).

**DISPLAY ONLY. THIS HEART RATE NEVER COMMANDS THE MOTOR.** The heart rate
that regulates the speed still comes from ``src/ecg_pipeline.py`` (BioSPPy,
through ``src/signal_processing.py``) into the runtime and its safety
supervisor. This module is a *second, independent* computation of the same
quantity, from the same samples, with a different detector and a stricter
quality grader, shown next to the one in control so the operator can see when
the two disagree.

**It does sit in the safety path as a VETO.** ``src/ecg_pipeline.py`` runs it
on the window the legacy DSP used and withholds that rate from the runtime
unless this processor grades it GOOD and agrees within ``AGREEMENT_BPM``: the
legacy grader called white noise "good" at ~133 bpm, and this one does not.
It can only make a rate unusable (the runtime then sees a stale heart rate,
which its safety layer handles); the number that regulates is never this one.

The chain, for one window of RAW counts:

1. **Transfer function** (PLUX, *BITalino ECG Sensor Data Sheet*, rev. B)::

       ECG(mV) = ((ADC / 2**n - 1/2) * VCC) / G_ECG * 1000

   with ``n = 10`` bits on A1, ``VCC = 3.3 V`` and ``G_ECG = 1100``: about
   0.00293 mV per count, centred on count 512.
2. **Band-pass 0.5-40 Hz** (``src.dsp.bandpass``, zero phase): removes the
   baseline wander and everything above the ECG's diagnostic band. This is
   also the waveform the screen draws, decimated to ``SPEC.display_rate``.
3. **R-peak detection**, Pan-Tompkins style: a 5-15 Hz QRS band-pass,
   derivative, squaring, a 150 ms
   moving-window integration, then ``src.dsp.peaks`` above 30 % of the
   integrated signal's 99th percentile with a 250 ms refractory period. The
   squared derivative favours the steep QRS over the slow P and T waves by two
   orders of magnitude, and the percentile (rather than the maximum) keeps one
   short artefact from raising the threshold above every real beat. Each
   detection is then timed on the largest deflection of the 0.5-40 Hz signal
   within 75 ms of it, the R apex.
4. **Quality**, from the most to the least fundamental, the first that applies
   wins (see :func:`_grade`). Only ``GOOD`` publishes numbers.

See ``src/sensors/base.py`` for the processor contract and
.claude/skills/anheart-strict-python/SKILL.md for the rules.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

import numpy as np

from src import dsp
from src.result import Err, Ok, Result
from src.sensors.base import (
    Metric,
    SensorKind,
    SensorReading,
    SensorSpec,
    blank_metrics,
    decimate,
    raw_quality,
)
from src.training.types import SignalQuality
from src.units import Hertz, Monotonic, Seconds

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.ECG,
    label="Electrocardiogramme",
    unit="mV",
    description=(
        "Activite electrique du coeur (derivation thoracique, 3 electrodes). Affichage et"
        " second calcul de la frequence cardiaque ; la regulation utilise toujours "
        "src/ecg_pipeline.py."
    ),
    window_s=Seconds(10.0),
    display_rate=250,
)

# --- Transfer function (PLUX BITalino ECG datasheet) ----------------------
VCC_VOLTS: Final[float] = 3.3
ECG_GAIN: Final[float] = 1100.0
MILLIVOLTS_PER_VOLT: Final[float] = 1000.0
ADC_LEVELS: Final[int] = 1 << SensorKind.ECG.adc_bits
"""``2**n``: 1024 on the 10-bit A1 channel."""

# --- Filtering and detection ----------------------------------------------
BAND_LOW: Final[Hertz] = Hertz(0.5)
BAND_HIGH: Final[Hertz] = Hertz(40.0)
QRS_LOW: Final[Hertz] = Hertz(5.0)
QRS_HIGH: Final[Hertz] = Hertz(15.0)
"""Pan-Tompkins' QRS band: where the beats are searched for. It keeps the QRS
energy and rejects most of the P and T waves, the wander and broadband motion
noise; sweeping 0.5-40, 5-25 and 8-20 Hz on the simulator's motion artefact
found 5-15 Hz the most robust."""

INTEGRATION_S: Final[Seconds] = Seconds(0.150)
"""Pan-Tompkins moving-window integration: about one QRS width."""

REFRACTORY_S: Final[Seconds] = Seconds(0.250)
"""No two R peaks closer than this (240 bpm, above the plausible ceiling)."""

SEARCH_S: Final[Seconds] = Seconds(0.075)
"""How far either side of an integrator peak the R apex is looked for."""

THRESHOLD_FRACTION: Final[float] = 0.3
THRESHOLD_PERCENTILE: Final[float] = 0.99

# --- Quality ---------------------------------------------------------------
MIN_WINDOW_S: Final[Seconds] = Seconds(4.0)
"""Less history than this is not judged: acquisition has just started."""

MAINS_HZ: Final[Hertz] = Hertz(50.0)
MAINS_HALF_BAND: Final[Hertz] = Hertz(2.0)
MAINS_RATIO_MAX: Final[float] = 0.5
"""Above this share of the power (>0.5 Hz) within 50 +/- 2 Hz, the hum dominates."""

POWER_FLOOR: Final[float] = 1e-12
"""mV^2: keeps the mains ratio defined when the in-band power is nil."""

MIN_BEATS: Final[int] = 3
"""Two RR intervals: the fewest a median and an RMSSD can be taken from."""

MIN_BPM: Final[float] = 30.0
MAX_BPM: Final[float] = 220.0

RR_TOLERANCE: Final[float] = 0.3
"""Every RR must lie within +/- 30 % of the window's median RR.

Respiratory sinus arrhythmia stays well inside it; an ectopic beat (a short
RR then a compensatory pause), a missed beat (a doubled RR) or an artefact
counted as a beat does not. Such a window is graded ``noisy``: the rate may be
nearly right, but the RMSSD of a window with an ectopic beat is not HRV."""

EDGE_FACTOR: Final[float] = 1.5
"""No beat for longer than this many median RRs at either end: beats are missing."""

MS_PER_S: Final[float] = 1000.0
S_PER_MIN: Final[float] = 60.0

HEART_RATE: Final[str] = "heart_rate"
RR_MEAN: Final[str] = "rr_mean"
RMSSD: Final[str] = "rmssd"
BEATS: Final[str] = "beats"

METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    (HEART_RATE, "Frequence cardiaque (affichage)", "bpm"),
    (RR_MEAN, "Intervalle RR moyen", "ms"),
    (RMSSD, "RMSSD (variabilite)", "ms"),
    (BEATS, "Battements dans la fenetre", ""),
)
"""(key, French label, unit) of every metric, in display order."""


def adc_to_millivolts(count: float) -> float:
    """One raw A1 count in millivolts at the electrode (datasheet transfer function)."""
    return (count / ADC_LEVELS - 0.5) * VCC_VOLTS / ECG_GAIN * MILLIVOLTS_PER_VOLT


def _display_counts(raw: Sequence[float]) -> list[float]:
    """Counts fit to draw: non-finite values at mid-scale (0 mV), others clamped to 0..1023.

    Only for the waveform of a window that could not be filtered; the window's
    quality already says it is corrupt.
    """
    top = float(SensorKind.ECG.adc_max)
    mid = ADC_LEVELS / 2.0
    return [min(max(v, 0.0), top) if math.isfinite(v) else mid for v in raw]


@dataclass(frozen=True, slots=True)
class Beats:
    """The R peaks of one window and the intervals between them."""

    peaks: tuple[int, ...]
    """Sample indices of the R peaks, increasing."""

    rr_s: tuple[float, ...]
    """Successive R-R intervals, seconds. One fewer than :attr:`peaks`."""


def detect_beats(filtered: dsp.Signal, qrs: dsp.Signal, fs: Hertz) -> Beats:
    """R peaks of an ECG sampled at ``fs``. Total: too short, no beats.

    ``qrs`` is the ECG band-passed to the QRS band (where the beats are found),
    ``filtered`` the same samples in the display band (where each beat's apex
    is timed). Both in mV, both the same length.
    """
    if qrs.size < dsp.MIN_PEAK_SAMPLES or qrs.size != filtered.size:
        return Beats(peaks=(), rr_s=())
    slope = np.diff(qrs) * float(fs)
    energy = slope * slope
    width = min(max(1, round(float(INTEGRATION_S) * float(fs))), int(energy.size))
    integrated = np.convolve(energy, np.full(width, 1.0 / width), mode="same")
    ordered = sorted(dsp.values_of(integrated))
    level = ordered[int(THRESHOLD_PERCENTILE * (len(ordered) - 1))]
    coarse = dsp.peaks(integrated, fs, min_interval=REFRACTORY_S, height=THRESHOLD_FRACTION * level)
    found = _refined(filtered, coarse, fs)
    rr = tuple((b - a) / float(fs) for a, b in pairwise(found))
    return Beats(peaks=found, rr_s=rr)


def _refined(filtered: dsp.Signal, coarse: Sequence[int], fs: Hertz) -> tuple[int, ...]:
    """Each integrator peak moved onto the largest deflection of the ECG near it.

    The integrator's maximum is broad and wanders by a few milliseconds from
    beat to beat; the R apex does not. Timing on the apex is what keeps that
    wander out of the RMSSD. Largest in magnitude, so an inverted lead works.
    """
    reach = max(1, round(float(SEARCH_S) * float(fs)))
    magnitude = np.abs(filtered)
    # A set: two detections refined onto the same apex are one beat.
    return tuple(sorted({_apex(magnitude, i, reach) for i in coarse}))


def _apex(magnitude: dsp.Signal, near: int, reach: int) -> int:
    """Index of the largest sample within ``reach`` of ``near`` (which is in range)."""
    start = max(0, near - reach)
    return start + int(np.argmax(magnitude[start : near + reach + 1]))


@dataclass(frozen=True, slots=True)
class HeartMetrics:
    """What a GOOD window reports."""

    heart_rate: float
    rr_mean_ms: float
    rmssd_ms: float
    beats: int


def heart_metrics(beats: Beats) -> HeartMetrics:
    """The four numbers of a window with at least two RR intervals."""
    rr_ms = [rr * MS_PER_S for rr in beats.rr_s]
    successive = [b - a for a, b in pairwise(rr_ms)]
    return HeartMetrics(
        heart_rate=S_PER_MIN * MS_PER_S / statistics.median(rr_ms),
        rr_mean_ms=statistics.fmean(rr_ms),
        rmssd_ms=math.sqrt(statistics.fmean(d * d for d in successive)),
        beats=len(beats.peaks),
    )


def mains_ratio(found: dsp.Spectrum) -> float:
    """Share of the power above ``BAND_LOW`` that lies within 50 +/- 2 Hz."""
    frequencies = dsp.values_of(found.frequencies)
    power = dsp.values_of(found.power)
    total = 0.0
    mains = 0.0
    for f, p in zip(frequencies, power, strict=True):
        if f >= BAND_LOW:
            total += p
        if abs(f - MAINS_HZ) <= MAINS_HALF_BAND:
            mains += p
    return mains / max(total, POWER_FLOOR)


def _grade(
    beats: Beats, samples: int, fs: Hertz, mains: float
) -> tuple[SignalQuality, str, HeartMetrics | None]:
    """Quality of a filtered window, and its metrics when (and only when) GOOD."""
    duration = samples / float(fs)
    if duration < MIN_WINDOW_S:
        return SignalQuality.NO_SIGNAL, f"fenetre trop courte ({duration:.1f} s)", None
    if mains > MAINS_RATIO_MAX:
        return (
            SignalQuality.MAINS_DOMINATED,
            f"secteur 50 Hz : {mains:.0%} de la puissance, verifier les electrodes",
            None,
        )
    return _rhythm(beats, samples, fs)


def _rhythm(
    beats: Beats, samples: int, fs: Hertz
) -> tuple[SignalQuality, str, HeartMetrics | None]:
    """The physiological half of :func:`_grade`: enough beats, plausible, regular, unbroken."""
    if len(beats.peaks) < MIN_BEATS:
        return SignalQuality.NOISY, f"trop peu de battements ({len(beats.peaks)})", None
    metrics = heart_metrics(beats)
    if not MIN_BPM <= metrics.heart_rate <= MAX_BPM:
        return (
            SignalQuality.NOISY,
            f"frequence implausible ({metrics.heart_rate:.0f} bpm)",
            None,
        )
    median_rr = statistics.median(beats.rr_s)
    if any(abs(rr - median_rr) > RR_TOLERANCE * median_rr for rr in beats.rr_s):
        return SignalQuality.NOISY, "rythme irregulier : extrasystole ou artefact", None
    lead = beats.peaks[0] / float(fs)
    tail = (samples - 1 - beats.peaks[-1]) / float(fs)
    if max(lead, tail) > EDGE_FACTOR * median_rr:
        return SignalQuality.NOISY, "battements manquants : electrode ou mouvement", None
    return SignalQuality.GOOD, "", metrics


def _metrics_of(found: HeartMetrics | None) -> tuple[Metric, ...]:
    if found is None:
        return blank_metrics(METRICS)
    values = (found.heart_rate, found.rr_mean_ms, found.rmssd_ms, float(found.beats))
    return tuple(
        Metric(key=key, label=label, value=value, unit=unit)
        for (key, label, unit), value in zip(METRICS, values, strict=True)
    )


def both[A, B, E](first: Result[A, E], second: Result[B, E]) -> Result[tuple[A, B], E]:
    """Both values, or the first error. (A candidate for ``src/result.py``.)"""
    if isinstance(first, Err):
        return first
    if isinstance(second, Err):
        return second
    return Ok((first.value, second.value))


class ECGProcessor:
    """The ECG processor. Stateless: every window is judged on its own."""

    __slots__ = ()

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        """Grade one window of RAW A1 counts and, when GOOD, measure the heart in it."""
        fallback = decimate(
            [adc_to_millivolts(v) for v in _display_counts(raw)], fs, SPEC.display_rate
        )
        finite = all(math.isfinite(v) for v in raw)
        judged = (
            raw_quality(raw, SensorKind.ECG.adc_max)
            if finite
            else (SignalQuality.NO_SIGNAL, "valeurs non finies : trame corrompue")
        )
        if judged is not None:
            return self._reading(at, fallback, judged[0], judged[1], None)
        rate = Hertz(float(fs))
        millivolts = dsp.as_signal([adc_to_millivolts(v) for v in raw])
        # One-second Welch segments: 1 Hz bins, so the mains line is resolved.
        segment = max(1, min(len(raw), fs))
        analysed = both(
            both(
                dsp.bandpass(millivolts, rate, BAND_LOW, BAND_HIGH),
                dsp.bandpass(millivolts, rate, QRS_LOW, QRS_HIGH),
            ),
            dsp.spectrum(millivolts, rate, segment=segment),
        )
        if isinstance(analysed, Err):
            detail = f"filtrage impossible : {analysed.error.detail}"
            return self._reading(at, fallback, SignalQuality.NO_SIGNAL, detail, None)
        (filtered, qrs), power = analysed.value
        beats = detect_beats(filtered, qrs, rate)
        quality, detail, found = _grade(beats, len(raw), rate, mains_ratio(power))
        waveform = decimate(dsp.values_of(filtered), fs, SPEC.display_rate)
        return self._reading(at, waveform, quality, detail, found)

    @staticmethod
    def _reading(
        at: Monotonic,
        waveform: tuple[float, ...],
        quality: SignalQuality,
        detail: str,
        found: HeartMetrics | None,
    ) -> SensorReading:
        return SensorReading(
            kind=SensorKind.ECG,
            at=at,
            display_rate=SPEC.display_rate,
            waveform=waveform,
            quality=quality,
            detail=detail,
            metrics=_metrics_of(found),
        )


def make() -> ECGProcessor:
    """A fresh processor (processors may keep state between windows)."""
    return ECGProcessor()
