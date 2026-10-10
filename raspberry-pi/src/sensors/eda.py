"""Activite electrodermale (EDA): the processor for BITalino channel EDA (A2, 10-bit).

**Transfer function.** PLUX, *Electrodermal Activity (EDA) Sensor Data Sheet*
(BITalino EDA, rev. 2020, "Transfer function" section)::

    EDA(uS) = ((ADC / 2^n) * VCC) / 0.132        n = 10 bits, VCC = 3.3 V

so one count is 3.3 / 1024 / 0.132 = 0.0244 uS and full scale (1023) is
24.98 uS - the datasheet's 0-25 uS range, with a 0-2.8 Hz analog bandwidth.
(The pre-2016 sensor used ``1 / (1 - ADC/2^n)`` in MOhm; that is not this one.)

**Processing.** Counts become uS through the transfer function, then two
zero-phase Butterworth low-passes from :mod:`src.dsp`:

* 5 Hz (the "clean" trace, at most 0.4 fs): the waveform shown, and what the
  artefact checks run on;
* 1 Hz: what the tonic level and the responses are measured on. An SCR rises
  over 0.5-5 s, so nothing physiological lives above 1 Hz.

The tonic level (SCL) is the 10th percentile of the 1 Hz trace: a response
only ever adds to the tonic level, so the lower envelope is its estimate, and
the median would be dragged up by the SCRs (by 8-12 % at 10-15 SCR/min). Phasic responses (SCRs)
are trough-to-peak: a local maximum at least 1 s from the previous one with a
prominence of :data:`SCR_THRESHOLD_US`; its onset is the trough reached walking
back from it (at most :data:`MAX_RISE_S`); it counts if its amplitude is at
least :data:`SCR_THRESHOLD_US` and its rise time at least :data:`MIN_RISE_S`
(Boucsein, *Electrodermal Activity*, 2nd ed., 2012: SCR rise 0.5-5 s; 0.01-
0.05 uS minimum-amplitude criteria; 0.05 uS is two counts here).

**Quality**, in this order - the first rule that applies decides:

1. :func:`~src.sensors.base.raw_quality` (empty, out of 0..1023, flat, clipping);
2. a NaN or infinite sample, a sample rate below 10 Hz, or under 5 s of data;
3. SCL below 0.05 uS: out of the physiological range (Kleckner et al., IEEE
   TBME 65(7), 2018, rule 1: valid EDA is 0.05-60 uS). The 60 uS ceiling is
   above this sensor's 25 uS full scale, so the clipping check of step 1 is
   what enforces it;
4. any 0.5 s block of the clean trace below 0.05 uS: the electrode came off
   (the conductance collapses towards zero part-way through the window);
5. a slope steeper than 10 uS/s over 0.1 s: movement artefact (Kleckner et
   al. 2018, rule 2);
6. otherwise GOOD. The metrics are ``None`` unless GOOD, and ``scr_amplitude``
   is also ``None`` when no response occurred (there is nothing to average).

See ``src/sensors/base.py`` for the contract.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, NewType

from src.dsp import as_signal, lowpass, peaks, values_of
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

Microsiemens = NewType("Microsiemens", float)
"""Skin conductance. Local until ``src/units.py`` grows it (reported)."""

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.EDA,
    label="Activite electrodermale",
    unit="uS",
    description=(
        "Conductance de la peau (deux electrodes sur la paume ou les doigts) : niveau "
        "tonique et reponses phasiques au stress."
    ),
    window_s=Seconds(20.0),
    display_rate=50,  # A resting skin conductance can span one count (0.024 uS) for a whole
    # window: only a perfectly constant line is "unplugged".
    flat_span_counts=0.5,
)

# --- Transfer function (PLUX BITalino EDA datasheet) ----------------------
VCC_V: Final[float] = 3.3
EDA_GAIN: Final[float] = 0.132
"""The datasheet's divisor: volts per microsiemens of the sensor's front end."""

_FULL_SCALE: Final[float] = float(1 << SPEC.kind.adc_bits)


def microsiemens(count: float) -> Microsiemens:
    """ADC count to skin conductance: ``((count / 2^n) * VCC) / 0.132``."""
    return Microsiemens(count / _FULL_SCALE * VCC_V / EDA_GAIN)


def adc_count(value: Microsiemens) -> AdcCount:
    """The inverse, rounded and clamped to the channel: what the ADC would read."""
    if not math.isfinite(value):
        return AdcCount(0)
    raw = round(value * EDA_GAIN / VCC_V * _FULL_SCALE)
    return AdcCount(min(SPEC.kind.adc_max, max(0, raw)))


# --- Processing parameters ------------------------------------------------
SCR_CUTOFF_HZ: Final[float] = 1.0
CLEAN_CUTOFF_HZ: Final[float] = 5.0
MAX_CUTOFF_FRACTION: Final[float] = 0.4
"""A cut-off never above 0.4 fs, so a 10 Hz acquisition still filters."""

MIN_FS: Final[int] = 10
MIN_WINDOW_S: Final[float] = 5.0
DETECT_RATE: Final[int] = 10
"""The 1 Hz trace is analysed at 10 Hz: nothing above 1 Hz is left to alias."""

CLEAN_RATE: Final[int] = 50
SLOPE_LAG_S: Final[float] = 0.1
BLOCK_S: Final[float] = 0.5

MIN_SCL_US: Final[float] = 0.05
MAX_SLOPE_US_PER_S: Final[float] = 10.0
SCR_THRESHOLD_US: Final[float] = 0.05
SCR_MIN_INTERVAL_S: Final[float] = 1.0
MIN_RISE_S: Final[float] = 0.5
MAX_RISE_S: Final[float] = 5.0

SECONDS_PER_MINUTE: Final[float] = 60.0

METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("scl", "Niveau tonique (SCL)", "uS"),
    ("scr_rate", "Frequence des reponses (SCR)", "/min"),
    ("scr_amplitude", "Amplitude moyenne des SCR", "uS"),
    ("scr_count", "Nombre de SCR", ""),
)


@dataclass(frozen=True, slots=True)
class Scr:
    """One skin-conductance response found in a trace."""

    onset: int
    """Index of the trough it rose from."""

    peak: int
    amplitude: Microsiemens
    rise_time: Seconds


def detect_scrs(trace: Sequence[float], fs: float) -> tuple[Scr, ...]:
    """Trough-to-peak SCRs in a low-passed conductance trace (uS) sampled at ``fs``. Total."""
    if fs <= 0.0 or not all(math.isfinite(v) for v in trace):
        return ()
    found = peaks(
        as_signal(trace),
        Hertz(fs),
        min_interval=Seconds(SCR_MIN_INTERVAL_S),
        prominence=SCR_THRESHOLD_US,
    )
    max_back = max(1, round(MAX_RISE_S * fs))
    responses: list[Scr] = []
    previous = 0
    for peak in found:
        limit = max(previous, peak - max_back)
        onset = peak
        while onset > limit and trace[onset - 1] < trace[onset]:
            onset -= 1
        amplitude = trace[peak] - trace[onset]
        rise = (peak - onset) / fs
        if amplitude >= SCR_THRESHOLD_US and rise >= MIN_RISE_S:
            responses.append(
                Scr(
                    onset=onset,
                    peak=peak,
                    amplitude=Microsiemens(amplitude),
                    rise_time=Seconds(rise),
                )
            )
        previous = peak
    return tuple(responses)


def max_slope(trace: Sequence[float], fs: float) -> float:
    """Steepest change over :data:`SLOPE_LAG_S`, in uS/s. 0.0 for a trace too short."""
    lag = max(1, round(SLOPE_LAG_S * fs))
    if fs <= 0.0 or len(trace) <= lag:
        return 0.0
    span = lag / fs
    return max(abs(trace[i + lag] - trace[i]) for i in range(len(trace) - lag)) / span


def lowest_block(trace: Sequence[float], fs: float) -> float:
    """The smallest median over consecutive :data:`BLOCK_S` blocks (inf for an empty trace)."""
    size = max(1, round(BLOCK_S * fs))
    medians = [
        statistics.median(trace[start : start + size]) for start in range(0, len(trace), size)
    ]
    return min(medians, default=math.inf)


def _filtered(values: Sequence[float], fs: int, cutoff: float) -> list[float] | None:
    found = lowpass(as_signal(values), Hertz(fs), Hertz(min(cutoff, MAX_CUTOFF_FRACTION * fs)))
    if isinstance(found, Err):
        return None
    return values_of(found.value)


def _filter_pair(values: Sequence[float], fs: int) -> tuple[list[float], list[float]] | None:
    """The clean (5 Hz) and slow (1 Hz) traces, or ``None`` if either filter refused."""
    clean = _filtered(values, fs, CLEAN_CUTOFF_HZ)
    slow = _filtered(values, fs, SCR_CUTOFF_HZ)
    if clean is None or slow is None:
        return None
    return clean, slow


type Verdict = tuple[SignalQuality, str]
"""A degraded grade and the French sentence saying why."""


def _degraded(verdict: Verdict, shown: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
    return SensorReading(
        kind=SPEC.kind,
        at=at,
        display_rate=SPEC.display_rate,
        waveform=decimate(shown, fs, SPEC.display_rate),
        quality=verdict[0],
        detail=verdict[1],
        metrics=blank_metrics(METRICS),
    )


def _input_verdict(raw: Sequence[float], fs: int) -> Verdict | None:
    """Rules 1-2 of the module docstring: what is wrong with the window as delivered.

    ``raw_quality`` refuses NaN and infinity first, so no finiteness check here.
    """
    judged = raw_quality(raw, SPEC.kind.adc_max, SPEC.flat_span_counts)
    if judged is not None:
        return judged
    if fs < MIN_FS:
        return SignalQuality.NO_SIGNAL, f"frequence d'echantillonnage trop basse ({fs} Hz)"
    if len(raw) < MIN_WINDOW_S * fs:
        return SignalQuality.NO_SIGNAL, "fenetre trop courte"
    return None


def _trace_verdict(scl: float, clean: Sequence[float], fs: float) -> Verdict | None:
    """Rules 3-5: what the conductance itself says about the electrodes."""
    if scl < MIN_SCL_US:
        return (
            SignalQuality.NO_SIGNAL,
            "conductance hors plage physiologique : electrodes absentes ou seches",
        )
    if lowest_block(clean, fs) < MIN_SCL_US:
        return SignalQuality.NO_SIGNAL, "chute brutale vers zero : electrode decollee"
    if max_slope(clean, fs) > MAX_SLOPE_US_PER_S:
        return SignalQuality.NOISY, "variation trop rapide : artefact de mouvement"
    return None


class EDAProcessor:
    """The EDA processor. Stateless: every window is judged on its own."""

    __slots__ = ()

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        finite = all(math.isfinite(v) for v in raw)
        conductance = [float(microsiemens(v)) for v in raw] if finite else []
        verdict = _input_verdict(raw, fs)
        if verdict is not None:
            return _degraded(verdict, conductance, fs, at)
        traces = _filter_pair(conductance, fs)
        if traces is None:
            return _degraded((SignalQuality.NO_SIGNAL, "filtrage impossible"), conductance, fs, at)
        clean, slow = traces

        clean_step = max(1, fs // CLEAN_RATE)
        scl = statistics.quantiles(slow, n=10)[0]
        verdict = _trace_verdict(scl, clean[::clean_step], fs / clean_step)
        if verdict is not None:
            return _degraded(verdict, clean, fs, at)

        detect_step = max(1, fs // DETECT_RATE)
        responses = detect_scrs(slow[::detect_step], fs / detect_step)
        minutes = len(raw) / fs / SECONDS_PER_MINUTE
        amplitude = statistics.fmean(float(r.amplitude) for r in responses) if responses else None
        values: tuple[float | None, ...] = (
            scl,
            len(responses) / minutes,
            amplitude,
            float(len(responses)),
        )
        return SensorReading(
            kind=SPEC.kind,
            at=at,
            display_rate=SPEC.display_rate,
            waveform=decimate(clean, fs, SPEC.display_rate),
            quality=SignalQuality.GOOD,
            detail="",
            metrics=tuple(
                Metric(key=key, label=label, value=value, unit=unit)
                for (key, label, unit), value in zip(METRICS, values, strict=True)
            ),
        )


def make() -> EDAProcessor:
    """A fresh processor (processors may keep state between windows)."""
    return EDAProcessor()
