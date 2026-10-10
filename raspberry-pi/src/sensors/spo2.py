"""Photoplethysmogramme au doigt (voie SPO2, A3): the processor for BITalino channel SpO2.

**What this one analog channel actually carries.** A pulse oximeter computes
saturation from the *ratio of ratios* of two wavelengths,
``R = (AC_red / DC_red) / (AC_ir / DC_ir)``, mapped through an empirical
calibration curve. That needs two photodetector signals, time-multiplexed from
a red and an infrared LED, demultiplexed in step with the LED drive.

The BITalino exposes this position as ONE 10-bit analog input sampled by the
board's ADC. PLUX's saturation-capable sensor (the biosignalsplux SpO2) is a
*digital* sensor with its own LED multiplexing, which a single analog BITalino
input cannot demultiplex; what plugs into an analog BITalino port at this
position is a photoplethysmographic (PPG / BVP) front end: one light path, one
photodiode, an amplified voltage that falls and rises with the blood volume
under the finger at every heartbeat. So this module assumes:

    **the channel is a single-wavelength PPG waveform, in arbitrary units.**

What that honestly supports, and nothing more:

* ``pulse_rate`` - beats per minute from the systolic peaks,
  ``60 / mean(peak-to-peak interval)`` (the mean, not the median: at 100 Hz
  one-sample interval quantisation would otherwise cost 3 bpm at 180 bpm).
* ``perfusion_index`` - ``100 * AC / DC``: the median beat amplitude of the
  band-passed pulse (systolic peak minus the preceding foot) over the mean raw
  count. **Relative**, not the clinical PI: the front end adds an amplifier
  offset, so the count is not proportional to transmitted light. It tracks
  changes within one session (vasoconstriction under g lowers it); it is not
  comparable with a hospital monitor's number.
* ``pulse_regularity`` - coefficient of variation of the peak-to-peak
  intervals, ``100 * pstdev / mean`` (%): pulse-rate variability and, above a
  threshold, the evidence of missed or spurious beats.
* ``spo2`` - **always ``None``.** One wavelength has no ratio of ratios; any
  saturation printed from it would be invented. Reported, with the reason in
  its label and in :data:`SPEC`'s description, so the screen says why it is a
  dash rather than looking broken.

Quality, in order: the shared :func:`~src.sensors.base.raw_quality` checks;
too short a window; no pulsatile component (finger absent); a DC shift (finger
withdrawn or clip moved); a burst of band power or beat-to-beat amplitude
jumps (motion artefact, which grows with the g load on a clipped finger); too
few beats; an implausible or incoherent rhythm. Metrics are ``None`` unless
the verdict is GOOD. Monitoring only: nothing here reaches the motor (see
``src/sensors/base.py``).

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

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
from src.units import Hertz, Monotonic, Seconds

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.SPO2,
    label="Photoplethysmogramme (pouls au doigt)",
    unit="u.a.",
    description=(
        "Pince au doigt : onde de pouls (PPG) sur une seule longueur d'onde. Frequence du "
        "pouls, regularite et indice de perfusion relatif. La saturation SpO2 n'est pas "
        "calculable : il faudrait deux longueurs d'onde (rouge et infrarouge)."
    ),
    window_s=Seconds(10.0),
    display_rate=100,
)

METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("pulse_rate", "Frequence du pouls", "bpm"),
    ("perfusion_index", "Indice de perfusion relatif", "%"),
    ("pulse_regularity", "Irregularite du pouls (CV)", "%"),
    ("spo2", "SpO2 : non mesurable (une seule longueur d'onde)", "%"),
)
"""(key, French label, unit) of every metric, in display order."""

BAND_LOW: Final[Hertz] = Hertz(0.35)
BAND_HIGH: Final[Hertz] = Hertz(8.0)
"""The pulse band, up to the dicrotic harmonics. The low edge sits at 0.35 Hz
(21 bpm) rather than the textbook 0.5 Hz on purpose: at 0.5 Hz a 25 bpm pulse
loses its fundamental, its second harmonic dominates, and the peak detector
reports a confident 50 bpm - on exactly the bradycardic subject whose rate
matters. With the fundamental kept, such a pulse reads as what it is and is
graded out by :data:`MIN_PULSE_BPM`. Slow breathing is removed below the edge;
exercise breathing (0.4-0.6 Hz) is not, and is harmless: it moves the baseline
by a fraction of a beat's amplitude."""

MIN_WINDOW: Final[Seconds] = Seconds(3.0)
"""Below this there are too few beats to call a rate."""

MIN_PULSATILE_COUNTS: Final[float] = 3.0
"""Band-passed 5..95 percentile span below this: no pulse under the sensor."""

FINGER_SHIFT_MIN_COUNTS: Final[float] = 40.0
FINGER_SHIFT_RATIO: Final[float] = 1.5
"""The 1-second means of the raw count moving by more than
``max(40, 1.5 * pulse span)``: the DC level jumped, the finger left the clip.
Respiration moves the baseline by a fraction of the pulse, far below this."""

BURST_RATIO: Final[float] = 2.5
"""A 1-second block of the band-passed pulse whose RMS exceeds this multiple of
the median block's: a motion burst, not a heartbeat."""

AMPLITUDE_JUMP_RATIO: Final[float] = 2.0
"""A beat more than twice, or less than half, the median beat: motion artefact."""

PROMINENCE_FRACTION: Final[float] = 0.35
"""A systolic peak must stand out by this fraction of the pulse span, which
rejects the diastolic wave after the dicrotic notch."""

MIN_PULSE_BPM: Final[float] = 30.0
MAX_PULSE_BPM: Final[float] = 220.0
MIN_BEATS: Final[int] = 4
MAX_INTERVAL_CV_PERCENT: Final[float] = 20.0
"""Beyond this the interval series is beats missed or invented, not variability."""

SECONDS_PER_MINUTE: Final[float] = 60.0
PERCENT: Final[float] = 100.0
LOW_PERCENTILE: Final[float] = 0.05
HIGH_PERCENTILE: Final[float] = 0.95


@dataclass(frozen=True, slots=True)
class PulseFigures:
    """What a GOOD window yields."""

    pulse_rate: float
    """bpm."""

    perfusion_index: float
    """Relative, %."""

    pulse_regularity: float
    """Coefficient of variation of the intervals, %."""


type Verdict = tuple[SignalQuality, str]


class SpO2Processor:
    """The SpO2 (PPG) processor. Stateless between windows."""

    __slots__ = ()

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        assessed = assess(raw, fs)
        if isinstance(assessed, PulseFigures):
            quality, detail = SignalQuality.GOOD, ""
            # In METRICS order; the saturation stays None (module docstring).
            values: tuple[float | None, ...] = (
                assessed.pulse_rate,
                assessed.perfusion_index,
                assessed.pulse_regularity,
                None,
            )
            metrics = tuple(
                Metric(key=key, label=label, value=value, unit=unit)
                for (key, label, unit), value in zip(METRICS, values, strict=True)
            )
        else:
            quality, detail = assessed
            metrics = blank_metrics(METRICS)
        return SensorReading(
            kind=SPEC.kind,
            at=at,
            display_rate=SPEC.display_rate,
            waveform=decimate(raw, fs, SPEC.display_rate),
            quality=quality,
            detail=detail,
            metrics=metrics,
        )


def make() -> SpO2Processor:
    """A fresh processor (processors may keep state between windows)."""
    return SpO2Processor()


# =========================================================================
# The analysis
# =========================================================================


def assess(raw: Sequence[float], fs: int) -> Verdict | PulseFigures:
    """Grade one window and, if GOOD, measure it. Total."""
    filtered = _filtered(raw, fs)
    if isinstance(filtered, tuple):
        return filtered
    # From here fs > 0 (the band-pass refused anything else) and the samples
    # are finite (it refused NaN and infinity too).
    pulse = dsp.values_of(filtered)
    span = _percentile(pulse, HIGH_PERCENTILE) - _percentile(pulse, LOW_PERCENTILE)
    envelope = _envelope_verdict(raw, pulse, span, fs)
    if envelope is not None:
        return envelope
    found = dsp.peaks(
        filtered,
        Hertz(float(fs)),
        min_interval=Seconds(SECONDS_PER_MINUTE / MAX_PULSE_BPM),
        prominence=PROMINENCE_FRACTION * span,
    )
    if len(found) < MIN_BEATS:
        return SignalQuality.NO_SIGNAL, "pas assez de battements detectes"
    return _beats(raw, pulse, found, fs)


def _filtered(raw: Sequence[float], fs: int) -> Verdict | dsp.Signal:
    """The band-passed window, or why there is none."""
    judged = raw_quality(raw, SPEC.kind.adc_max)
    if judged is not None:
        return judged
    if len(raw) < MIN_WINDOW * fs:
        return SignalQuality.NO_SIGNAL, f"fenetre trop courte (moins de {MIN_WINDOW:.0f} s)"
    filtered = dsp.bandpass(dsp.as_signal(raw), Hertz(float(fs)), BAND_LOW, BAND_HIGH)
    if isinstance(filtered, Err):
        return SignalQuality.NO_SIGNAL, f"filtrage impossible : {filtered.error.detail}"
    return filtered.value


def _envelope_verdict(
    raw: Sequence[float], pulse: Sequence[float], span: float, fs: int
) -> Verdict | None:
    """No pulse, finger out, or a motion burst: judged before any beat is counted."""
    if span < MIN_PULSATILE_COUNTS:
        return SignalQuality.NO_SIGNAL, "aucune composante pulsatile : doigt absent ou mal place"
    means = [statistics.fmean(block) for block in _blocks(raw, fs)]
    if max(means) - min(means) > max(FINGER_SHIFT_MIN_COUNTS, FINGER_SHIFT_RATIO * span):
        return SignalQuality.NO_SIGNAL, "saut de ligne de base : doigt retire ou pince deplacee"
    energies = [_rms(block) for block in _blocks(pulse, fs)]
    if max(energies) > BURST_RATIO * statistics.median(energies):
        return SignalQuality.NOISY, "artefact de mouvement : bouffee d'amplitude"
    return None


def _beats(
    raw: Sequence[float], pulse: Sequence[float], found: Sequence[int], fs: int
) -> Verdict | PulseFigures:
    """Judge the detected beats (at least :data:`MIN_BEATS`) and measure them."""
    # Peak minus the foot before it. Positive: the foot lies between two
    # distinct peaks, and a prominent peak has a lower base on each side.
    amplitudes = [pulse[peak] - min(pulse[before:peak]) for before, peak in pairwise(found)]
    typical = statistics.median(amplitudes)
    if max(amplitudes) > AMPLITUDE_JUMP_RATIO * typical or (
        min(amplitudes) * AMPLITUDE_JUMP_RATIO < typical
    ):
        return SignalQuality.NOISY, "artefact de mouvement : amplitude des battements instable"

    intervals = [(peak - before) / fs for before, peak in pairwise(found)]
    rate = SECONDS_PER_MINUTE / statistics.fmean(intervals)
    if rate < MIN_PULSE_BPM:
        return SignalQuality.NOISY, f"pouls a {rate:.0f} bpm : hors physiologie"
    regularity = PERCENT * statistics.pstdev(intervals) / statistics.fmean(intervals)
    if regularity > MAX_INTERVAL_CV_PERCENT:
        return SignalQuality.NOISY, "rythme incoherent : battements manques ou artefacts"

    # The mean count is positive: raw_quality guarantees every sample is in
    # 0..1023 with a span above two counts, so at least one sample exceeds 2.
    dc = statistics.fmean(raw)
    return PulseFigures(
        pulse_rate=rate,
        perfusion_index=PERCENT * typical / dc,
        pulse_regularity=regularity,
    )


def _blocks(values: Sequence[float], fs: int) -> list[Sequence[float]]:
    """Consecutive whole one-second blocks (at least three, given MIN_WINDOW)."""
    return [values[start : start + fs] for start in range(0, len(values) - fs + 1, fs)]


def _rms(values: Sequence[float]) -> float:
    return math.sqrt(math.fsum(v * v for v in values) / len(values))


def _percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile of a non-empty sequence."""
    ordered = sorted(values)
    return ordered[round(fraction * (len(ordered) - 1))]
