"""Respiration (RESP): the processor for BITalino channel RESP (A4, 10-bit, PZT chest band).

**Monitoring only.** Nothing here commands the motor or trips the safety
supervisor (see ``src/sensors/base.py``). An apnea is *shown* - graded and
spelled out in ``detail`` - and it is up to the operator to act on it.

Transfer function
-----------------

PLUX, *BITalino (r)evolution Respiration (PZT) Sensor Data Sheet* (REV B,
"Transfer function")::

    RESP(%) = (ADC / 2^n - 1/2) x 100 %          n = 10 bits on A1-A4

so the channel reads a displacement in percent of the sensor's full range,
-50 % .. +50 %, zero at mid-scale. The band has no absolute calibration (the
reading depends on how tight it is strapped), so every metric below is
relative: a rate, an amplitude in % of full scale, a regularity, a duration.

Processing, per window
----------------------

1. Guards: non-finite values, non-positive rate, the physiology-free
   :func:`~src.sensors.base.raw_quality` checks, a window shorter than
   :data:`MIN_WINDOW_S`.
2. Counts -> % (above), then block means down to ~:data:`PROCESSING_RATE` Hz:
   breathing lives below 1 Hz, and a 30 s window at 1000 Hz would otherwise
   cost 30 000 samples of zero-phase filtering for nothing. The block mean is
   its own (boxcar) anti-alias filter.
3. Zero-phase Butterworth band-pass :data:`BAND_LOW` .. :data:`BAND_HIGH`
   (0.06 - 1.0 Hz, i.e. 3.6 - 60 breaths/min). The low edge sits a little under
   the conventional 0.1 Hz so that 4-6 breaths/min - the bottom of the
   plausible range this module grades against - is passed, not attenuated.
4. Motion artefact: the energy above :data:`MOTION_CUTOFF` (1.5 Hz, far above
   any breath) against the respiratory band's.
5. Breath detection: maxima of the band-passed signal at least
   :data:`MIN_BREATH_INTERVAL` apart and at least :func:`_min_prominence` tall,
   then at least :data:`BREATH_FRACTION` of the median breath's height
   (:func:`_true_breaths`: the filter's rebound inside a long pause is not a
   breath).
6. Rate = 60 / median breath-to-breath interval (median: a sigh or a dropped
   breath moves it by at most one interval's worth), graded against
   :data:`RATE_MIN` .. :data:`RATE_MAX`.
7. Apnea: the longest stretch of the window with no breath, the stretches
   from the window's start to the first breath and from the last breath to
   the window's end included - so an apnea still in progress is seen growing.
8. Spectral cross-check: :func:`src.dsp.dominant_frequency` over the same band
   must agree with the cycle count, or the detection is not trusted.

Metrics
-------

``resp_rate`` (breaths/min), ``amplitude`` (% of full scale, median
peak-to-trough), ``regularity`` (``1 - CV`` of the breath intervals, clamped
to 0..1: 1 is a metronome), ``apnea_s`` (s).

``None`` unless the reading is GOOD, **except** ``apnea_s``, which is also
reported when breaths were reliably detected but the window is graded down
*because of the breathing itself* (an apnea over :data:`APNEA_EVENT_S`, a rate
out of range, cycles disagreeing with the spectrum). Those are exactly the
windows where the pause is the information; hiding it behind a dash would
hide the event. It stays ``None`` when the *signal* is the problem (band off,
flat, motion, corrupt frame): there no breath was detected, and "no breath
detected" with a loose band is not evidence of an apnea.

An apnea over :data:`APNEA_EVENT_S` grades the window NOISY with the detail
``"apnee 14 s"``: the rate computed from the breaths either side of a pause is
a plausible-looking number that is not true while the pause lasts, so it is
not shown. (``SignalQuality`` has no clinical-event member; NOISY is the
mildest "do not read the numbers" grade. The detail says why.)

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from itertools import pairwise
from typing import Final, NewType

from src import dsp
from src.result import Err
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
from src.units import AdcCount, Hertz, Monotonic, Seconds

BreathsPerMinute = NewType("BreathsPerMinute", float)
"""A respiratory rate. Local to this module: ``src/units.py`` has no such unit
yet (``Bpm`` is a HEART rate and an ``int``); proposed for promotion there."""

Percent = NewType("Percent", float)
"""A PZT reading or amplitude, in percent of the sensor's full range."""

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.RESP,
    label="Respiration",
    unit="%",
    description=("Ceinture thoracique (capteur PZT) : frequence et amplitude respiratoires."),
    window_s=Seconds(30.0),
    display_rate=50,
)

SECONDS_PER_MINUTE: Final[float] = 60.0

# -- processing constants ---------------------------------------------------

PROCESSING_RATE: Final[int] = 25
"""Target rate, Hz, the window is block-averaged down to before filtering."""

MIN_WINDOW_S: Final[Seconds] = Seconds(10.0)
"""Below this there are not enough breaths to say anything (two at 12/min)."""

BAND_LOW: Final[Hertz] = Hertz(0.06)
BAND_HIGH: Final[Hertz] = Hertz(1.0)
"""The respiratory band, 3.6 - 60 breaths/min."""

MOTION_CUTOFF: Final[Hertz] = Hertz(1.5)
"""Everything above this is movement of the band, not breathing."""

MOTION_RATIO: Final[float] = 0.35
"""RMS above :data:`MOTION_CUTOFF` beyond this fraction of the band's RMS: motion."""

MIN_BREATH_INTERVAL: Final[Seconds] = Seconds(1.4)
"""Minimum spacing of two breaths (43/min). Nominally 1.5 s (40/min); 1.4 s so
that a 40/min breather with ordinary breath-to-breath variability is not
decimated by the spacing rule itself."""

MIN_AMPLITUDE: Final[Percent] = Percent(0.5)
"""Absolute floor on a breath's prominence: ~5 counts, above quantisation."""

RELATIVE_AMPLITUDE: Final[float] = 0.25
"""And relative: a breath is at least this fraction of the window's 5-95 % span."""

MIN_BREATHS: Final[int] = 2
"""Fewer than this and there is no interval to measure."""

BREATH_FRACTION: Final[float] = 0.5
"""A breath is at least this fraction of the window's median breath height."""

RATE_MIN: Final[BreathsPerMinute] = BreathsPerMinute(4.0)
RATE_MAX: Final[BreathsPerMinute] = BreathsPerMinute(60.0)
"""Plausible respiratory rates. Outside them the detection, not the subject, is wrong."""

APNEA_EVENT_S: Final[Seconds] = Seconds(10.0)
"""A pause longer than this is a clinically relevant apnea (the usual 10 s definition)."""

SPECTRAL_TOLERANCE: Final[float] = 0.25
"""Relative disagreement allowed between the cycle count and the spectrum..."""

SPECTRAL_BINS: Final[float] = 1.5
"""...or this many spectral bins, whichever is wider (a short window is coarse)."""

_METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("resp_rate", "Frequence respiratoire", "/min"),
    ("amplitude", "Amplitude", "%"),
    ("regularity", "Regularite", ""),
    ("apnea_s", "Plus longue pause", "s"),
)


def to_percent(count: float, bits: int = SensorKind.RESP.adc_bits) -> Percent:
    """PLUX PZT transfer function: ``(ADC / 2^n - 1/2) x 100``."""
    return Percent((count / float(1 << bits) - 0.5) * 100.0)


def to_count(percent: Percent, bits: int = SensorKind.RESP.adc_bits) -> AdcCount:
    """The inverse, rounded and clamped to the channel: what the simulator writes."""
    full = 1 << bits
    count = round((float(percent) / 100.0 + 0.5) * full)
    return AdcCount(min(full - 1, max(0, count)))


# =========================================================================
# The processor
# =========================================================================


class RESPProcessor:
    """The RESP processor. Stateless: every window is judged on its own."""

    __slots__ = ()

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        if not all(math.isfinite(v) for v in raw):
            return _reading(
                at, (), SignalQuality.NO_SIGNAL, "valeurs non numeriques : trame corrompue"
            )
        if fs <= 0:
            return _reading(at, (), SignalQuality.NO_SIGNAL, f"frequence d'echantillonnage {fs} Hz")
        percent = [to_percent(v) for v in raw]
        waveform = decimate(percent, fs, SPEC.display_rate)
        judged = raw_quality(raw, SensorKind.RESP.adc_max)
        if judged is not None:
            return _reading(at, waveform, judged[0], judged[1])
        duration = len(raw) / fs
        if duration < MIN_WINDOW_S:
            return _reading(
                at, waveform, SignalQuality.NO_SIGNAL, f"fenetre trop courte ({duration:.0f} s)"
            )
        return _analyse(at, waveform, percent, fs)


def make() -> RESPProcessor:
    """A fresh processor (processors may keep state between windows)."""
    return RESPProcessor()


# =========================================================================
# Internals
# =========================================================================


def _reading(
    at: Monotonic,
    waveform: tuple[float, ...],
    quality: SignalQuality,
    detail: str,
    metrics: tuple[Metric, ...] | None = None,
) -> SensorReading:
    return SensorReading(
        kind=SensorKind.RESP,
        at=at,
        display_rate=SPEC.display_rate,
        waveform=waveform,
        quality=quality,
        detail=detail,
        metrics=blank_metrics(_METRICS) if metrics is None else metrics,
    )


def _metrics(
    rate: float | None, amplitude: float | None, regularity: float | None, apnea: float | None
) -> tuple[Metric, ...]:
    values = (rate, amplitude, regularity, apnea)
    return tuple(
        Metric(key=k, label=label, value=v, unit=unit)
        for (k, label, unit), v in zip(_METRICS, values, strict=True)
    )


def block_means(values: Sequence[float], step: int) -> list[float]:
    """Means of consecutive ``step``-sample blocks (a trailing partial block is dropped)."""
    return [sum(values[i : i + step]) / step for i in range(0, len(values) - step + 1, step)]


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty sequence."""
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _min_prominence(band: Sequence[float]) -> float:
    """How tall a maximum must be to count as a breath, in %."""
    ordered = sorted(band)
    span = percentile(ordered, 0.95) - percentile(ordered, 0.05)
    return max(float(MIN_AMPLITUDE), RELATIVE_AMPLITUDE * span)


def spectral_rate(band: dsp.Signal, pfs: Hertz) -> BreathsPerMinute | None:
    """The spectrum's dominant respiratory rate, or ``None`` if it has none."""
    found = dsp.dominant_frequency(band, pfs, BAND_LOW, BAND_HIGH)
    return None if isinstance(found, Err) else BreathsPerMinute(found.value * SECONDS_PER_MINUTE)


def _amplitudes(band: Sequence[float], breaths: Sequence[int]) -> list[float]:
    """Each breath's height above the lowest point since the previous breath."""
    heights: list[float] = []
    previous = 0
    for index in breaths:
        heights.append(band[index] - min(band[previous : index + 1]))
        previous = index
    return heights


def _true_breaths(band: Sequence[float], candidates: Sequence[int]) -> tuple[int, ...]:
    """The candidates at least :data:`BREATH_FRACTION` as tall as the median one.

    A long pause makes the band-pass ring: the high-pass edge rebounds into a
    low hump in the middle of the pause, as tall as a shallow breath in
    absolute terms but a fraction of the breaths either side. Judged against
    the median breath rather than the window's span, so a sigh (a few times
    deeper) does not raise the bar over the ordinary breaths around it.
    """
    heights = _amplitudes(band, candidates)
    if not heights:
        return ()
    bar = BREATH_FRACTION * statistics.median(heights)
    return tuple(index for index, height in zip(candidates, heights, strict=True) if height >= bar)


def _analyse(
    at: Monotonic, waveform: tuple[float, ...], percent: Sequence[float], fs: int
) -> SensorReading:
    step = max(1, fs // PROCESSING_RATE)
    pfs = Hertz(fs / step)
    signal = dsp.as_signal(block_means(percent, step))

    filtered = dsp.bandpass(signal, pfs, BAND_LOW, BAND_HIGH)
    if isinstance(filtered, Err):
        return _reading(
            at, waveform, SignalQuality.NO_SIGNAL, f"filtrage impossible : {filtered.error.detail}"
        )
    residue = dsp.highpass(signal, pfs, MOTION_CUTOFF)
    if isinstance(residue, Err):
        return _reading(
            at, waveform, SignalQuality.NO_SIGNAL, f"filtrage impossible : {residue.error.detail}"
        )
    band_rms = dsp.rms(filtered.value)
    if dsp.rms(residue.value) > MOTION_RATIO * band_rms:
        return _reading(at, waveform, SignalQuality.NOISY, "artefact de mouvement")

    return _grade_breaths(at, waveform, filtered.value, pfs)


def _grade_breaths(
    at: Monotonic, waveform: tuple[float, ...], filtered: dsp.Signal, pfs: Hertz
) -> SensorReading:
    """Detect the breaths in the band-passed window and grade what they say."""
    band = dsp.values_of(filtered)
    candidates = dsp.peaks(
        filtered, pfs, min_interval=MIN_BREATH_INTERVAL, prominence=_min_prominence(band)
    )
    breaths = _true_breaths(band, candidates)
    if len(breaths) < MIN_BREATHS:
        return _reading(
            at,
            waveform,
            SignalQuality.NO_SIGNAL,
            "aucune oscillation respiratoire : ceinture non portee ou lache, ou apnee prolongee",
        )

    times = [i / pfs for i in breaths]
    intervals = [b - a for a, b in pairwise(times)]
    window = len(band) / pfs
    apnea = max((times[0], *intervals, window - times[-1]))
    mean_interval = statistics.fmean(intervals)
    rate = BreathsPerMinute(SECONDS_PER_MINUTE / statistics.median(intervals))
    apnea_only = _metrics(None, None, None, apnea)

    if not RATE_MIN <= rate <= RATE_MAX:
        return _reading(
            at,
            waveform,
            SignalQuality.NOISY,
            f"frequence {rate:.0f}/min hors de {RATE_MIN:.0f}-{RATE_MAX:.0f}/min",
            apnea_only,
        )
    if apnea > APNEA_EVENT_S:
        return _reading(at, waveform, SignalQuality.NOISY, f"apnee {apnea:.0f} s", apnea_only)

    spectral = spectral_rate(filtered, pfs)
    resolution = pfs / len(band) * SECONDS_PER_MINUTE
    tolerance = max(SPECTRAL_TOLERANCE * rate, SPECTRAL_BINS * resolution)
    if spectral is None or abs(spectral - rate) > tolerance:
        return _reading(
            at,
            waveform,
            SignalQuality.NOISY,
            f"cycles ({rate:.0f}/min) et spectre ({spectral or 0.0:.0f}/min) en desaccord",
            apnea_only,
        )

    amplitude = statistics.median(_amplitudes(band, breaths))
    regularity = min(1.0, max(0.0, 1.0 - statistics.pstdev(intervals) / mean_interval))
    return _reading(
        at, waveform, SignalQuality.GOOD, "", _metrics(rate, amplitude, regularity, apnea)
    )
