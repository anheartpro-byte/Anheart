"""Electromyogramme (EMG): the processor for BITalino channel EMG (A5, **6-bit**).

**Transfer function.** PLUX, *Electromyography (EMG) Sensor Data Sheet*
(BITalino EMG, "Transfer function" section)::

    EMG(mV) = ((ADC / 2^n - 1/2) * VCC) / G_EMG * 1000
              n = channel resolution, VCC = 3.3 V, G_EMG = 1009

The sensor itself spans +-1.64 mV with a 25-480 Hz analog bandwidth.

**The 6-bit limitation, stated plainly.** When six channels are acquired,
A5 arrives with 6 bits (``SensorKind.adc_bits``, decoded in
``src/bitalino_client.py``), not 10. So:

* one count (the quantisation step) is ``3.3 / 64 / 1009 * 1000`` =
  **0.0511 mV** (:data:`LSB_MV`), against 0.0032 mV at 10 bits; count 32 is
  exactly 0 mV, and the range is -1.635..+1.584 mV;
* the quantisation noise is ``LSB / sqrt(12)`` = 0.0148 mV rms, white;
* **what it cannot resolve:** a relaxed muscle (roughly 0.005-0.02 mV rms)
  is below one count, so rest reads as a line wobbling by a count or two - and
  if nothing else moves it, :func:`~src.sensors.base.raw_quality` calls it flat
  (NO_SIGNAL), which at 6 bits may mean "relaxed" as much as "electrode off".
  Weak contractions under ~0.1 mV rms (two counts) are coarsely quantised,
  and fine amplitude detail below 0.05 mV (peaks, onset shape) is lost;
* **what it can:** moderate to strong contractions (0.1-1.5 mV), their timing
  (the sample rate is unaffected), the fraction of time the muscle is active,
  and the spectrum - quantisation noise is white, so it flattens the spectrum
  only when the signal is within a few counts of it.

Two corrections follow from that. The ``rms`` metric removes the in-band
quantisation power (Sheppard's correction: ``rms^2 - LSB^2/12`` scaled by the
band's share of Nyquist). The ``median_frequency`` is reported only when the
muscle is active at least :data:`MIN_ACTIVATION_FOR_MDF` of the window,
because on a quiet window it would be the median of white quantisation noise
(half the band, about 235 Hz) and look like a rested muscle.

**Processing** (all through :mod:`src.dsp`):

1. counts -> mV by the transfer function;
2. zero-phase Butterworth band-pass 20-450 Hz (the upper edge clamped to 0.9 of
   Nyquist; below :data:`MIN_FS` Hz of sampling there is no EMG band left
   worth the name and the window is refused);
3. envelope: full-wave rectification, then a 6 Hz low-pass;
4. onset detection on the envelope: a sample is active when the envelope
   exceeds ``max(ONSET_FACTOR * baseline, THRESHOLD_FLOOR_MV)`` for at least
   :data:`MIN_BURST_S` (Hodges & Bui, *EEG Clin. Neurophysiol.* 101, 1996:
   threshold on the resting baseline, with a minimum duration). The baseline is
   the 10th percentile of the envelope, remembered across windows as the
   quietest seen (relaxing by :data:`BASELINE_RELAX` per window so a changed
   electrode can re-settle) and capped at :data:`BASELINE_CAP_MV`: above that
   no muscle is at rest, so a window that starts in a sustained contraction is
   still measured as active instead of becoming its own baseline.

**Metrics**, all ``None`` unless GOOD: ``rms`` (mV, quantisation-corrected),
``activation`` (% of the window active), ``median_frequency`` (Hz, the fatigue
index: it falls as the muscle tires; ``None`` also when the muscle is not
active enough) and ``peak`` (mV, largest absolute band-passed sample).

**Quality**, in this order - the first rule that applies decides:

1. a NaN or infinite sample: NO_SIGNAL (a corrupt frame);
2. :func:`~src.sensors.base.raw_quality` (empty, out of 0..63, flat, clipping);
3. sample rate below :data:`MIN_FS`: NO_SIGNAL;
4. the filter refuses the window (too short to filter), or less than
   :data:`MIN_WINDOW_S` of data: NO_SIGNAL;
5. more than :data:`MAINS_FRACTION` of the band's power within 45-55 Hz:
   MAINS_DOMINATED;
6. the envelope is a train of narrow, regular bursts at 40-180 per minute:
   ECG crosstalk, NOISY (the "activity" is the heart, not the muscle). It is
   judged on the envelope alone (this processor never sees the ECG channel),
   so it catches crosstalk that dominates the window; a contraction louder
   than the QRS hides it, and then the window is graded on the muscle;
7. the band holds less than :data:`MIN_BAND_SHARE` of the signal's variance:
   a slow wander dominates, i.e. the electrode is off or moving, NO_SIGNAL;
8. otherwise GOOD.

The processor is stateful (the remembered baseline) and deterministic: the
same sequence of windows yields the same sequence of readings.

See ``src/sensors/base.py`` for the contract.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from src import dsp
from src.result import Err, Result, unwrap_or
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
from src.units import Hertz, Millivolts, Monotonic, Seconds

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.EMG,
    label="Electromyogramme",
    unit="mV",
    description=(
        "Activite electrique d'un muscle (deux electrodes sur le muscle, une de "
        "reference) : activation et fatigue."
    ),
    window_s=Seconds(5.0),
    display_rate=250,
)

VCC_V: Final[float] = 3.3
"""Supply voltage in the PLUX transfer function."""

GAIN: Final[float] = 1009.0
"""G_EMG, the sensor gain in the PLUX transfer function."""

LSB_MV: Final[float] = VCC_V / (1 << SensorKind.EMG.adc_bits) / GAIN * 1000.0
"""One count on A5 at 6 bits: 0.0511 mV."""

LOW_HZ: Final[float] = 20.0
HIGH_HZ: Final[float] = 450.0
NYQUIST_MARGIN: Final[float] = 0.9
"""The upper band edge is kept below this fraction of Nyquist."""

MIN_FS: Final[int] = 500
"""Below this there is no EMG band left (the upper edge would be under 225 Hz)."""

MIN_WINDOW_S: Final[float] = 1.0
"""Shorter windows are refused: activation and spectrum need a second at least."""

EDGE_S: Final[float] = 0.05
"""Trimmed from each end of the filtered window before anything is measured."""

ENVELOPE_HZ: Final[float] = 6.0
"""Low-pass of the rectified signal (linear envelope)."""

ONSET_FACTOR: Final[float] = 3.0
"""Active when the envelope exceeds this many times the resting baseline..."""

THRESHOLD_FLOOR_MV: Final[float] = 0.06
"""...and at least this: a little over one count, the 6-bit resolution."""

BASELINE_CAP_MV: Final[float] = 0.05
"""No resting envelope is above this; a higher 10th percentile is a contraction."""

BASELINE_PERCENTILE: Final[float] = 0.10
BASELINE_RELAX: Final[float] = 0.05
"""Per window, the remembered baseline may rise by this fraction."""

MIN_BURST_S: Final[float] = 0.025
"""Envelope excursions shorter than this are not contractions."""

MIN_ACTIVATION_FOR_MDF: Final[float] = 20.0
"""Median frequency needs the muscle active on at least this % of the window."""

MAINS_HZ: Final[float] = 50.0
MAINS_HALF_BAND_HZ: Final[float] = 5.0
MAINS_FRACTION: Final[float] = 0.5
"""More than this share of the band power around 50 Hz: hum, not muscle."""

MIN_BAND_SHARE: Final[float] = 0.1
"""Less than this share of the variance inside the EMG band: a slow wander."""

ECG_MIN_RR_S: Final[float] = 60.0 / 180.0
ECG_MAX_RR_S: Final[float] = 60.0 / 40.0
ECG_MIN_BEATS: Final[int] = 4
ECG_PROMINENCE: Final[float] = 0.35
"""A burst counts when it stands this share of the envelope's height above its surroundings."""

ECG_INTERVAL_TOLERANCE: Final[float] = 0.1
ECG_REGULAR_SHARE: Final[float] = 0.8
"""A heart: this share of the burst intervals within 10 % of their median.

A median rather than a mean, so one contraction landing between two beats
(which splits one interval) does not hide the crosstalk."""

ECG_MAX_DUTY: Final[float] = 0.25
"""The envelope sits above half the burst height at most this share of the time."""

ECG_MIN_HEIGHT_MV: Final[float] = LSB_MV
"""Bursts smaller than one count are not judged at all."""

METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("rms", "Amplitude efficace", "mV"),
    ("activation", "Activation", "%"),
    ("median_frequency", "Frequence mediane", "Hz"),
    ("peak", "Amplitude crete", "mV"),
)


def counts_to_mv(count: float, bits: int = SensorKind.EMG.adc_bits) -> Millivolts:
    """The PLUX EMG transfer function for one ``bits``-bit count."""
    return Millivolts((count / (1 << bits) - 0.5) * VCC_V / GAIN * 1000.0)


def band_edges(fs: int) -> tuple[Hertz, Hertz]:
    """The band-pass edges at sample rate ``fs``: 20 Hz to min(450, 0.9 Nyquist)."""
    return Hertz(LOW_HZ), Hertz(min(HIGH_HZ, NYQUIST_MARGIN * fs / 2.0))


def quantisation_power(fs: int) -> float:
    """In-band power of the 6-bit quantisation noise, in mV^2 (white, LSB^2/12)."""
    low, high = band_edges(fs)
    return LSB_MV * LSB_MV / 12.0 * (high - low) / (fs / 2.0)


def value_or_none(result: Result[Hertz, dsp.DspRefused]) -> float | None:
    """A dsp estimate, or ``None`` when it was refused (the metric is then not shown)."""
    if isinstance(result, Err):
        return None
    return float(result.value)


def mains_fraction(x: dsp.Signal, fs: Hertz, low: float, high: float) -> float:
    """Share of the power within ``low..high`` Hz that sits within 5 Hz of 50 Hz.

    0.0 when there is no spectrum or no power (nothing to call hum).
    """
    found = dsp.spectrum(x, fs)
    if isinstance(found, Err):
        return 0.0
    band = 0.0
    mains = 0.0
    frequencies = dsp.values_of(found.value.frequencies)
    for f, p in zip(frequencies, dsp.values_of(found.value.power), strict=True):
        if low <= f <= high:
            band += p
            if abs(f - MAINS_HZ) <= MAINS_HALF_BAND_HZ:
                mains += p
    return mains / band if band > 0.0 else 0.0


def looks_like_ecg(env: Sequence[float], fs: int) -> bool:
    """Whether an envelope is a train of narrow bursts at a heart's pace.

    At least :data:`ECG_MIN_BEATS` bursts of half the envelope's height above
    its median, spaced 1/3-1.5 s with :data:`ECG_REGULAR_SHARE` of the
    intervals within 10 % of their median (or of twice it: a beat lost under a
    contraction), and above half height at most :data:`ECG_MAX_DUTY` of
    the time. A contraction is neither that regular nor that brief.
    """
    if len(env) < ECG_MIN_BEATS or fs <= 0:
        return False
    median = statistics.median(env)
    height = max(env) - median
    if height < ECG_MIN_HEIGHT_MV:
        return False
    found = dsp.peaks(
        dsp.as_signal(env),
        Hertz(float(fs)),
        min_interval=Seconds(ECG_MIN_RR_S),
        prominence=ECG_PROMINENCE * height,
    )
    if len(found) < ECG_MIN_BEATS:
        return False
    intervals = [(b - a) / fs for a, b in itertools.pairwise(found)]
    typical = statistics.median(intervals)
    near = sum(
        1
        for i in intervals
        if min(abs(i - typical), abs(i - 2.0 * typical)) <= ECG_INTERVAL_TOLERANCE * typical
    )
    regular = near >= ECG_REGULAR_SHARE * len(intervals)
    duty = sum(1 for v in env if v > median + 0.5 * height) / len(env)
    return ECG_MIN_RR_S <= typical <= ECG_MAX_RR_S and regular and duty <= ECG_MAX_DUTY


def active_samples(env: Sequence[float], threshold: float, min_run: int) -> int:
    """Samples above ``threshold`` in runs of at least ``min_run`` samples."""
    total = 0
    run = 0
    for v in (*env, -math.inf):  # the sentinel closes a run that reaches the end
        if v > threshold:
            run += 1
            continue
        if run >= min_run:
            total += run
        run = 0
    return total


def percentile(values: Sequence[float], fraction: float) -> float:
    """The ``fraction`` quantile by nearest rank; 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class _Verdict:
    quality: SignalQuality
    detail: str


class EMGProcessor:
    """The EMG processor.

    **Mutable by design**: it remembers the quietest envelope baseline seen so
    far (``_baseline``), because a muscle braced for the whole window has no
    rest in it to measure against. Owned by whichever thread calls
    :meth:`process`; the hub calls it from one worker at a time.
    """

    __slots__ = ("_baseline",)

    def __init__(self) -> None:
        self._baseline: float | None = None

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    @property
    def baseline_mv(self) -> float | None:
        """The remembered resting envelope, mV; ``None`` before the first GOOD window."""
        return self._baseline

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        finite = dsp.is_finite(dsp.as_signal(raw))
        mv = [float(counts_to_mv(v)) for v in raw] if finite else []
        waveform = decimate(mv, fs, SPEC.display_rate)
        refused = _before_filtering(raw, fs, finite=finite)
        if refused is not None:
            return _reading(at, waveform, refused.quality, refused.detail)
        rate = Hertz(float(fs))
        low, high = band_edges(fs)
        centred = dsp.as_signal(mv)
        passed = dsp.bandpass(centred, rate, low, high)
        if isinstance(passed, Err):
            detail = f"fenetre trop courte pour filtrer ({passed.error.detail})"
            return _reading(at, waveform, SignalQuality.NO_SIGNAL, detail)
        filtered = passed.value
        waveform = decimate(dsp.values_of(filtered), fs, SPEC.display_rate)
        if len(raw) < MIN_WINDOW_S * fs:
            detail = f"fenetre trop courte ({len(raw) / fs:.1f} s, >= {MIN_WINDOW_S:.0f} s)"
            return _reading(at, waveform, SignalQuality.NO_SIGNAL, detail)
        # The zero-phase filter's transients live in the first and last few
        # samples; everything measured is measured on the interior.
        edge = round(EDGE_S * fs)
        band = dsp.values_of(filtered)[edge:-edge]
        filtered = dsp.as_signal(band)
        rectified = [abs(v) for v in band]
        env = dsp.values_of(
            unwrap_or(
                dsp.lowpass(dsp.as_signal(rectified), rate, Hertz(ENVELOPE_HZ)),
                dsp.as_signal(rectified),  # a refused smoother: the rectified signal itself
            )
        )
        verdict = _grade(filtered, band, mv, env, fs)
        if verdict is not None:
            return _reading(at, waveform, verdict.quality, verdict.detail)
        return _reading(
            at, waveform, SignalQuality.GOOD, "", self._metrics(filtered, band, env, fs)
        )

    def _metrics(
        self, filtered: dsp.Signal, band: Sequence[float], env: Sequence[float], fs: int
    ) -> tuple[Metric, ...]:
        quiet = min(BASELINE_CAP_MV, percentile(env, BASELINE_PERCENTILE))
        previous = self._baseline
        baseline = quiet if previous is None else min(quiet, previous * (1.0 + BASELINE_RELAX))
        self._baseline = baseline
        threshold = max(ONSET_FACTOR * baseline, THRESHOLD_FLOOR_MV)
        active = active_samples(env, threshold, max(1, round(MIN_BURST_S * fs)))
        activation = 100.0 * active / len(env)
        power = sum(v * v for v in band) / len(band)
        rms = math.sqrt(max(0.0, power - quantisation_power(fs)))
        mdf = (
            value_or_none(dsp.median_frequency(filtered, Hertz(float(fs))))
            if activation >= MIN_ACTIVATION_FOR_MDF
            else None
        )
        peak = max(abs(v) for v in band)
        values = (rms, activation, mdf, peak)
        return tuple(
            Metric(key=key, label=label, value=value, unit=unit)
            for (key, label, unit), value in zip(METRICS, values, strict=True)
        )


def _before_filtering(raw: Sequence[float], fs: int, *, finite: bool) -> _Verdict | None:
    """Steps 1-3 of the module docstring; ``None`` when the window may be filtered."""
    if not finite:
        return _Verdict(SignalQuality.NO_SIGNAL, "valeurs non numeriques : trame corrompue")
    judged = raw_quality(raw, SensorKind.EMG.adc_max)
    if judged is not None:
        return _Verdict(judged[0], judged[1])
    if fs < MIN_FS:
        return _Verdict(
            SignalQuality.NO_SIGNAL,
            f"echantillonnage {fs} Hz insuffisant pour l'EMG (>= {MIN_FS} Hz)",
        )
    return None


def _grade(
    filtered: dsp.Signal,
    band: Sequence[float],
    mv: Sequence[float],
    env: Sequence[float],
    fs: int,
) -> _Verdict | None:
    """Steps 5-7 of the module docstring; ``None`` when the window is GOOD."""
    low, high = band_edges(fs)
    share = mains_fraction(filtered, Hertz(float(fs)), low, high)
    if share > MAINS_FRACTION:
        return _Verdict(
            SignalQuality.MAINS_DOMINATED, f"secteur 50 Hz : {100.0 * share:.0f} % de la bande"
        )
    if looks_like_ecg(env, fs):
        return _Verdict(
            SignalQuality.NOISY, "contamination ECG : bouffees regulieres au rythme cardiaque"
        )
    total = statistics.pvariance(mv)
    inside = statistics.pvariance(band)
    if inside < MIN_BAND_SHARE * total:
        return _Verdict(
            SignalQuality.NO_SIGNAL,
            "derive lente dominante : electrode decollee ou en mouvement",
        )
    return None


def _reading(
    at: Monotonic,
    waveform: tuple[float, ...],
    quality: SignalQuality,
    detail: str,
    metrics: tuple[Metric, ...] | None = None,
) -> SensorReading:
    return SensorReading(
        kind=SensorKind.EMG,
        at=at,
        display_rate=SPEC.display_rate,
        waveform=waveform,
        quality=quality,
        detail=detail,
        metrics=blank_metrics(METRICS) if metrics is None else metrics,
    )


def make() -> EMGProcessor:
    """A fresh processor (processors may keep state between windows)."""
    return EMGProcessor()
