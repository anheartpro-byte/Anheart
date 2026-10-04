"""Lumiere (LUX): the processor for BITalino channel LUX (A6, 6-bit on the wire).

**Transfer function.** PLUX, *LUX Sensor Data Sheet* (BITalino, rev. B),
"Transfer Function": ``LUX(%) = ADC / 2^n x 100`` with ``n`` the channel
resolution. A6 is sampled on 6 bits (``SensorKind.LUX.adc_bits``), so one count
is ``100 / 64 = 1.5625 %`` of the sensor range and the largest reading, 63
counts, is 98.4 %. The unit is a percentage of the sensor's range, not lux: the
datasheet gives no absolute calibration, so none is invented here.

**Why a light sensor on a centrifuge.** The capsule rides the arm. With a
light source FIXED in the room (a ceiling lamp, a window), the sensor sees the
light rise and fall once per turn of the arm: a flicker at the rotation
frequency. Its dominant frequency is therefore an estimate of the OUTPUT-shaft
speed that owes nothing to the drive, the encoder or the gearbox ratio - a
cheap, independent cross-check of the speed the drive reports
(:func:`cross_check`).

Assumptions, stated because the number is only as good as they are:

* exactly one fixed light source, or one that dominates (``k`` equally spaced
  sources flicker at ``k`` times the rotation: the estimate would read ``k``
  times too fast; a lit room with no source to pass shows no flicker at all);
* the speed is roughly constant over the window (60 s): the estimate is the
  window's average, so compare it to the average measured speed;
* the flicker is shallower than :data:`EVENT_JUMP_PERCENT` (see below).

When there is no clear spectral line the metric is ``None`` and the reading's
``detail`` says why (capsule stopped, no light source to pass, too short a
window, too many light changes). **Monitoring only: never a control input**,
like every processor here (see ``src/sensors/base.py``).

Metrics (all ``None`` unless the quality is GOOD):

* ``level`` - mean light over the window, %;
* ``variation`` - its standard deviation, % (about 0 under steady light);
* ``flicker_rpm`` - rotation estimate from the flicker, OUTPUT tr/min;
* ``events`` - sudden light changes in the window (a door opened, lights
  switched): a jump of at least :data:`EVENT_JUMP_PERCENT` between the median
  of the second before and the median of the second after.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique
from itertools import pairwise
from typing import Final, NewType

from src import dsp
from src.result import Err, Ok, Result
from src.sensors.base import (
    FLAT_SPAN_COUNTS,
    SATURATED_FRACTION,
    Metric,
    SensorKind,
    SensorReading,
    SensorSpec,
    blank_metrics,
    decimate,
)
from src.training.types import SignalQuality
from src.units import Hertz, Monotonic, OutputRpm, Seconds

LuxPercent = NewType("LuxPercent", float)
"""Light as a percentage of the LUX sensor's range (PLUX transfer function)."""

SPEC: Final[SensorSpec] = SensorSpec(
    kind=SensorKind.LUX,
    label="Lumiere",
    unit="%",
    description=(
        "Capteur de luminosite ambiante (A6). Dans la capsule, le passage devant "
        "une lampe fixe fait scintiller la lumiere a chaque tour : controle "
        "independant de la vitesse de rotation."
    ),
    # 60 s: the spectrum's bins are 1/60 Hz = 1 tr/min apart, and 3 tr/min
    # (the slowest rotation looked for) still fits three turns in the window.
    window_s=Seconds(60.0),
    display_rate=10,
)

FULL_SCALE_COUNTS: Final[int] = 1 << SensorKind.LUX.adc_bits
"""``2^n`` of the transfer function: 64 on the 6-bit A6."""

PERCENT_PER_COUNT: Final[float] = 100.0 / FULL_SCALE_COUNTS

MIN_WINDOW: Final[Seconds] = Seconds(2.0)
"""Below this there is no level worth displaying."""

DARK_MAX_COUNTS: Final[float] = 1.0
"""A flat window at or below this is graded "unplugged" (see :func:`judge`)."""

ANALYSIS_RATE_HZ: Final[int] = 20
"""The smoothed signal is decimated to about this rate before analysis."""

LOWPASS_HZ: Final[Hertz] = Hertz(4.0)
"""Removes the 100/120 Hz mains ripple of lamps and the sensor noise, and keeps
the flicker (<= 1 Hz) with its first harmonics. Also the anti-alias filter of
the decimation to :data:`ANALYSIS_RATE_HZ`."""

FLICKER_LOW_HZ: Final[Hertz] = Hertz(0.05)
FLICKER_HIGH_HZ: Final[Hertz] = Hertz(1.0)
"""The rotation band searched: 3 to 60 tr/min at the output shaft."""

FLICKER_MIN_WINDOW: Final[Seconds] = Seconds(30.0)
"""Shorter than this, the bins are more than 2 tr/min apart: no estimate."""

MIN_CYCLES: Final[float] = 3.0
"""A rotation must repeat at least this often in the window to be believed."""

MIN_FLICKER_STD_COUNTS: Final[float] = 0.3
"""Below this (about the 6-bit quantisation noise) there is nothing to analyse."""

LINE_CONCENTRATION: Final[float] = 0.5
"""Share of the band's power the flicker's harmonic comb must hold to be a line."""

SUBHARMONIC_RATIO: Final[float] = 0.3
"""A sub-multiple of the peak with this share of its power is the true rotation."""

MAX_HARMONICS: Final[int] = 4
"""Harmonics counted in the line test (one bin either side each): enough for a
pulse, few enough that a slow fundamental's comb does not cover the band."""

LOBE_BINS: Final[int] = 2
"""Half-width of a spectral line under the periodogram's Hann window, in bins."""

EVENT_JUMP_PERCENT: Final[float] = 15.0
"""A median-to-median jump at least this big (% of range) is a sudden change.

A flicker moves the 1 s medians by at most about 2/3 of its depth (worst at a
2 s period, 30 tr/min), so a flicker up to about 20 % deep never reads as an
event, and a switch of 25 % is still seen through a 10 % flicker."""

EVENT_HALF: Final[Seconds] = Seconds(1.0)
"""Each side of a candidate change is summarised by its median over this long."""

MAX_EVENTS_FOR_FLICKER: Final[int] = 3
"""More changes than this in one window: the light is not a stable reference."""

CROSS_CHECK_TOLERANCE: Final[OutputRpm] = OutputRpm(2.0)
"""Flicker and drive agree within this (the estimate is good to about 1 tr/min)."""

METRICS: Final[tuple[tuple[str, str, str], ...]] = (
    ("level", "Niveau lumineux", "%"),
    ("variation", "Variation lumineuse", "%"),
    ("flicker_rpm", "Rotation estimee (scintillement)", "tr/min"),
    ("events", "Changements brusques", "evt"),
)


def to_percent(count: float) -> LuxPercent:
    """PLUX transfer function: ``LUX(%) = ADC / 2^n x 100`` with ``n = 6``."""
    return LuxPercent(count * PERCENT_PER_COUNT)


def judge(raw: Sequence[float], fs: int) -> tuple[SignalQuality, str] | None:
    """The verdicts that stop processing, or ``None`` when the window may be analysed.

    Not ``base.raw_quality``, on purpose, for three reasons proper to a light
    sensor:

    * **Flat is not a fault here.** Steady light is the normal case and, on
      6 bits, it legitimately reads as a constant. A flat window is graded
      GOOD (variation about 0) when its level is off both rails. It is graded
      NO_SIGNAL only when flat at (about) zero: the sensor's photodiode
      amplifier under any real light gives a non-zero output, so a dead-zero
      line is what an unplugged sensor (or a cut lead) reads. Total darkness
      reads the same and cannot be told apart from it by the signal alone -
      which is exactly why it is not graded GOOD. Flat at the top rail is a
      saturated sensor (NOISY).
    * **Zero is not clipping.** 0 % is "no light", a true reading, so a flicker
      that dips to zero in a dark room is not saturated. Only the top rail
      clips (light brighter than the sensor's range).
    * NaN is checked explicitly: ``min``/``max`` silently skip it.
    """
    framing = _frame_verdict(raw, fs)
    return framing if framing is not None else _level_verdict(raw)


def _frame_verdict(raw: Sequence[float], fs: int) -> tuple[SignalQuality, str] | None:
    """Empty, badly clocked, corrupt or too short: nothing to look at."""
    adc_max = SensorKind.LUX.adc_max
    if not raw:
        return SignalQuality.NO_SIGNAL, "aucun echantillon recu"
    if fs <= 0:
        return SignalQuality.NO_SIGNAL, f"frequence d'echantillonnage invalide : {fs}"
    if not all(math.isfinite(v) and 0.0 <= v <= adc_max for v in raw):
        return SignalQuality.NO_SIGNAL, f"valeurs hors de 0..{adc_max} : trame corrompue"
    if len(raw) < MIN_WINDOW * fs:
        return SignalQuality.NO_SIGNAL, "fenetre trop courte : acquisition en cours"
    return None


def _level_verdict(raw: Sequence[float]) -> tuple[SignalQuality, str] | None:
    """Flat or clipped, judged as a light sensor (see :func:`judge`). ``raw`` is valid."""
    adc_max = SensorKind.LUX.adc_max
    low = min(raw)
    high = max(raw)
    if high - low <= FLAT_SPAN_COUNTS:
        mean = statistics.fmean(raw)
        if mean <= DARK_MAX_COUNTS:
            return (
                SignalQuality.NO_SIGNAL,
                "signal plat a zero : capteur debranche (ou obscurite totale)",
            )
        if mean >= adc_max - DARK_MAX_COUNTS:
            return SignalQuality.NOISY, "signal plat en butee haute : capteur sature"
        return None
    top = sum(1 for v in raw if v >= adc_max)
    if top > SATURATED_FRACTION * len(raw):
        return SignalQuality.NOISY, "signal sature : lumiere trop forte pour le capteur"
    return None


def smooth(percent: Sequence[float], fs: int) -> tuple[tuple[float, ...], Hertz]:
    """Low-pass then decimate to about :data:`ANALYSIS_RATE_HZ`. Returns (signal, its rate).

    At the slow BITalino rates (1 and 10 Hz) there is nothing above the flicker
    band to remove and the signal is used as is. If the filter refuses the
    window (too short for its padding) the unfiltered samples are used: the
    analysis below is robust to them, only noisier.
    """
    values: Sequence[float] = percent
    if fs >= 4.0 * LOWPASS_HZ:
        filtered = dsp.lowpass(dsp.as_signal(percent), Hertz(float(fs)), LOWPASS_HZ)
        if isinstance(filtered, Ok):
            values = dsp.values_of(filtered.value)
    step = max(1, fs // ANALYSIS_RATE_HZ)
    return tuple(values[::step]), Hertz(fs / step)


def detect_events(y: Sequence[float], rate: Hertz) -> tuple[int, ...]:
    """Indices where the light jumps by at least :data:`EVENT_JUMP_PERCENT`.

    ``y`` in %. At each boundary ``b`` the median of the :data:`EVENT_HALF`
    after it is compared with the median of the one before; consecutive
    boundaries over the threshold are one event, placed at the steepest sample.
    A median, so a single glitch cannot make an event, and a periodic flicker
    shallower than the threshold cannot either.
    """
    half = max(1, round(EVENT_HALF * rate))
    count = len(y) - half + 1
    medians = [statistics.median(y[i : i + half]) for i in range(max(0, count))]
    over = [
        b
        for b in range(half, len(y) - half + 1)
        if abs(medians[b] - medians[b - half]) >= EVENT_JUMP_PERCENT
    ]
    runs: list[list[int]] = []
    for b in over:
        if runs and b == runs[-1][-1] + 1:
            runs[-1].append(b)
        else:
            runs.append([b])
    # The medians jump over a plateau of boundaries; the change itself is
    # where the light moves fastest within it.
    return tuple(max(run, key=lambda b: abs(y[b] - y[b - 1])) for run in runs)


@dataclass(frozen=True, slots=True)
class NoFlicker:
    """Why no rotation could be read from the light. French, for the screen."""

    detail: str


def _destep(y: Sequence[float], events: Sequence[int]) -> list[float]:
    """Each stretch between two light changes, minus its own mean (steps would swamp the line)."""
    bounds = (0, *events, len(y))
    out: list[float] = []
    for start, stop in pairwise(bounds):
        piece = y[start:stop]
        mean = statistics.fmean(piece) if piece else 0.0
        out.extend(v - mean for v in piece)
    return out


def estimate_flicker(
    y: Sequence[float], rate: Hertz, events: Sequence[int] = ()
) -> Result[OutputRpm, NoFlicker]:
    """The rotation rate, in output tr/min, from the flicker in ``y`` (% sampled at ``rate``).

    1. the light changes (``events``) are removed, stretch by stretch;
    2. periodogram (``dsp.spectrum`` over the whole window: the estimate
       ``dsp.dominant_frequency`` is built on, called directly because the
       line test below needs the power, not only the peak's frequency);
    3. the strongest bin within the band is the candidate; a sub-multiple of
       it (1/2, 1/3) holding :data:`SUBHARMONIC_RATIO` of its power is the
       real rotation (a lamp passed briefly is a pulse train: rich harmonics);
    4. a clear line: the harmonic comb of the rotation holds at least
       :data:`LINE_CONCENTRATION` of the band's power, else ``Err``;
    5. the frequency is refined by the power centroid of the three bins
       around the peak (better than the 1 tr/min bin spacing).
    """
    duration = len(y) / rate
    if duration < FLICKER_MIN_WINDOW:
        return Err(NoFlicker(f"fenetre de {duration:.0f} s : il en faut {FLICKER_MIN_WINDOW:.0f}"))
    if len(events) > MAX_EVENTS_FOR_FLICKER:
        return Err(NoFlicker("changements de lumiere trop frequents pour lire la rotation"))
    residual = _destep(y, events)
    if statistics.pstdev(residual) < MIN_FLICKER_STD_COUNTS * PERCENT_PER_COUNT:
        return Err(
            NoFlicker("pas de scintillement : capsule a l'arret, ou pas de source de lumiere fixe")
        )
    return _spectral_line(residual, rate, low=max(FLICKER_LOW_HZ, MIN_CYCLES / duration))


def _spectral_line(
    residual: Sequence[float], rate: Hertz, *, low: float
) -> Result[OutputRpm, NoFlicker]:
    """Steps 2 to 5 of :func:`estimate_flicker`, on the de-stepped signal."""
    found = dsp.spectrum(dsp.as_signal(residual), rate, segment=len(residual))
    if isinstance(found, Err):
        return Err(NoFlicker(found.error.detail))
    frequencies = dsp.values_of(found.value.frequencies)
    power = dsp.values_of(found.value.power)
    high = min(FLICKER_HIGH_HZ, 0.45 * rate)
    inside = [i for i, f in enumerate(frequencies) if low <= f <= high]
    band_total = sum(power[i] for i in inside)
    if not inside or band_total <= 0.0:
        return Err(NoFlicker(f"aucune puissance entre {low * 60:.0f} et {high * 60:.0f} tr/min"))
    bin_hz = rate / len(residual)

    def lobe(target: float, half_width: int = LOBE_BINS) -> list[int]:
        return [i for i in inside if abs(frequencies[i] - target) <= half_width * bin_hz]

    peak = max(inside, key=lambda i: power[i])
    # Lowest sub-multiple first: a pulse train peaking at its 3rd harmonic
    # must not be read as its 3/2. One bin either side (the peak is within
    # half a bin of the true line, so its 1/k is within half a bin / k), and
    # never inside the peak's own lobe, whose leakage is not a sub-harmonic.
    candidates = [
        max(around, key=lambda i: power[i])
        for divisor in (3, 2)
        if (
            around := [i for i in lobe(frequencies[peak] / divisor, 1) if abs(i - peak) > LOBE_BINS]
        )
    ]
    chosen = next(
        (i for i in candidates if power[i] >= SUBHARMONIC_RATIO * power[peak]),
        peak,
    )
    fundamental = frequencies[chosen]
    harmonics = range(2, min(MAX_HARMONICS, int(high / fundamental)) + 1)
    comb = {*lobe(fundamental), *(i for k in harmonics for i in lobe(k * fundamental, 1))}
    if sum(power[i] for i in comb) < LINE_CONCENTRATION * band_total:
        return Err(NoFlicker("pas de raie nette : pas de scintillement regulier"))
    neighbours = range(max(0, chosen - 1), min(len(power), chosen + 2))
    weight = sum(power[i] for i in neighbours)
    centroid = sum(frequencies[i] * power[i] for i in neighbours) / weight
    return Ok(OutputRpm(centroid * 60.0))


@unique
class RotationCheck(Enum):
    """The light's view of the rotation against the drive's. Monitoring only."""

    AGREES = "agrees"
    DISAGREES = "disagrees"
    UNAVAILABLE = "unavailable"
    """No flicker estimate, or a speed outside the band the flicker can show."""


def cross_check(
    flicker: OutputRpm | None,
    measured: OutputRpm,
    tolerance: OutputRpm = CROSS_CHECK_TOLERANCE,
) -> RotationCheck:
    """Compare :func:`estimate_flicker` with the drive's measured OUTPUT speed.

    ``measured`` should be the average over the same window. Sign is ignored:
    light flickers the same whichever way the arm turns. A disagreement is for
    the operator and the log; nothing here stops or slows the motor.
    """
    speed = abs(measured)
    if flicker is None or not FLICKER_LOW_HZ * 60.0 <= speed <= FLICKER_HIGH_HZ * 60.0:
        return RotationCheck.UNAVAILABLE
    if abs(flicker - speed) <= tolerance:
        return RotationCheck.AGREES
    return RotationCheck.DISAGREES


class LUXProcessor:
    """The LUX processor. Stateless: every window is judged on its own."""

    __slots__ = ()

    @property
    def spec(self) -> SensorSpec:
        return SPEC

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        verdict = judge(raw, fs)
        adc_max = SensorKind.LUX.adc_max
        valid = all(math.isfinite(v) and 0.0 <= v <= adc_max for v in raw)
        percent = [to_percent(v) for v in raw] if valid else []
        waveform = decimate(percent, fs, SPEC.display_rate)
        if verdict is not None:
            quality, detail = verdict
            return self._reading(at, waveform, quality, detail, blank_metrics(METRICS))
        y, rate = smooth(percent, fs)
        events = detect_events(y, rate)
        found = estimate_flicker(y, rate, events)
        flicker = found.value if isinstance(found, Ok) else None
        # GOOD with an explanation: the light is fine, the rotation is not
        # readable from it, and the operator is told why (base.SensorReading
        # otherwise keeps ``detail`` empty when GOOD).
        detail = found.error.detail if isinstance(found, Err) else ""
        values: tuple[float | None, ...] = (
            statistics.fmean(y),
            statistics.pstdev(y),
            flicker,
            float(len(events)),
        )
        metrics = tuple(
            Metric(key=key, label=label, value=value, unit=unit)
            for (key, label, unit), value in zip(METRICS, values, strict=True)
        )
        return self._reading(at, waveform, SignalQuality.GOOD, detail, metrics)

    @staticmethod
    def _reading(
        at: Monotonic,
        waveform: tuple[float, ...],
        quality: SignalQuality,
        detail: str,
        metrics: tuple[Metric, ...],
    ) -> SensorReading:
        return SensorReading(
            kind=SensorKind.LUX,
            at=at,
            display_rate=SPEC.display_rate,
            waveform=waveform,
            quality=quality,
            detail=detail,
            metrics=metrics,
        )


def make() -> LUXProcessor:
    """A fresh processor (processors may keep state between windows)."""
    return LUXProcessor()
