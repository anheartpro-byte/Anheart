"""What every BITalino sensor processor is, and what it hands the screen.

One processor per analog channel (``src/sensors/<kind>.py``), each behind the
same :class:`SensorProcessor` protocol, so the acquisition hub, the web API
and the page treat all six alike and a new sensor is one module.

**None of these readings commands the motor.** The heart rate that drives the
speed still comes through ``src/ecg_pipeline.py`` into the runtime and its
safety supervisor, exactly as before. These processors are the monitoring
view: what the operator and the dashboard see for every channel, the ECG
included (a second, independent computation of the heart rate, displayed next
to the one in control). Promoting any of them into the control or safety path
is a separate, deliberate decision - taken once, for the ECG processor, and
only as a VETO: ``src/ecg_pipeline.py`` withholds a heart rate from the runtime
unless this independent computation grades the same window GOOD and agrees
with it. It can make a rate unusable; it never supplies one.

Two rules every processor follows, and the tests hold them to:

* **Total.** :meth:`SensorProcessor.process` never raises. A window that is
  flat, saturated, too short, NaN-ridden or garbage yields a reading with a
  degraded :class:`~src.training.types.SignalQuality`, a ``detail`` saying
  why, and metrics whose value is ``None`` - never an exception, and never a
  plausible-looking number made up from noise.
* **Honest.** A metric is ``None`` unless the quality is trustworthy enough to
  compute it; the page shows a dash for ``None``, as it already does for a
  stale heart rate.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol

from src.training.types import SignalQuality
from src.units import Monotonic, Seconds

#: Channels A1-A4 are 10-bit on the wire, A5-A6 6-bit (see bitalino_client).
_TEN_BIT_CHANNELS: Final[int] = 4


@unique
class SensorKind(Enum):
    """The six BITalino analog inputs, by the names ``CHANNEL_MAP`` uses. Wire strings."""

    ECG = "ECG"
    EDA = "EDA"
    SPO2 = "SpO2"
    RESP = "RESP"
    EMG = "EMG"
    LUX = "LUX"

    @property
    def channel(self) -> int:
        """The analog index on the board (A1 = 0), as ``bitalino_client.CHANNEL_MAP``."""
        return _CHANNELS[self]

    @property
    def adc_bits(self) -> int:
        """Resolution of this channel on the wire: 10 bits for A1-A4, 6 for A5-A6."""
        return 10 if self.channel < _TEN_BIT_CHANNELS else 6

    @property
    def adc_max(self) -> int:
        """Largest count this channel can report."""
        return (1 << self.adc_bits) - 1


_CHANNELS: Final[dict[SensorKind, int]] = {
    SensorKind.ECG: 0,
    SensorKind.EDA: 1,
    SensorKind.SPO2: 2,
    SensorKind.RESP: 3,
    SensorKind.EMG: 4,
    SensorKind.LUX: 5,
}


def parse_kind(name: str) -> SensorKind | None:
    """The kind for a channel name, case-insensitively (``"spo2"`` is SpO2)."""
    wanted = name.strip().upper()
    for kind in SensorKind:
        if kind.value.upper() == wanted:
            return kind
    return None


@dataclass(frozen=True, slots=True)
class SensorSpec:
    """What the screen says about a sensor, and how much history it needs."""

    kind: SensorKind
    label: str
    """French, for the operator: "Activite electrodermale"."""

    unit: str
    """Unit of the displayed waveform: "mV", "uS", "%", "u.a."..."""

    description: str
    """One or two French sentences: what it measures, where it goes on the body."""

    window_s: Seconds
    """How much history :meth:`SensorProcessor.process` is given each time."""

    display_rate: int
    """Samples per second of the waveform handed to the screen (decimated)."""

    flat_span_counts: float = 2.0
    """A window spanning at most this many counts is graded "flat" (no signal).

    Per sensor, because "quiet" is not "unplugged" everywhere: a resting skin
    conductance can span a single count for twenty seconds.
    """


@dataclass(frozen=True, slots=True)
class Metric:
    """One derived number. ``value`` is ``None`` when it cannot honestly be computed."""

    key: str
    """Stable identifier, snake_case: "heart_rate", "scr_count"."""

    label: str
    """French label for the screen."""

    value: float | None
    unit: str


@dataclass(frozen=True, slots=True)
class SensorReading:
    """One processed window of one channel: waveform, quality, metrics."""

    kind: SensorKind
    at: Monotonic
    """When the window's LAST sample was taken."""

    display_rate: int
    waveform: tuple[float, ...]
    """The window in the spec's unit, decimated to ``display_rate``."""

    quality: SignalQuality
    detail: str
    """Why the quality is what it is, in French; empty when GOOD."""

    metrics: tuple[Metric, ...]

    def metric(self, key: str) -> Metric | None:
        """The metric named ``key``, or ``None`` if this sensor does not report it."""
        for metric in self.metrics:
            if metric.key == key:
                return metric
        return None


class SensorProcessor(Protocol):
    """One channel's processing. Stateful between calls if it needs to be; total always."""

    @property
    def spec(self) -> SensorSpec: ...

    def process(self, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
        """Process the last ``spec.window_s`` seconds of ADC counts sampled at ``fs`` Hz.

        ``raw`` may be shorter than the window (acquisition just started) or
        empty. Never raises.
        """
        ...


# =========================================================================
# Helpers every processor shares
# =========================================================================

FLAT_SPAN_COUNTS: Final[float] = 2.0
"""A window whose peak-to-peak span is at most this many counts carries no signal."""

SATURATED_FRACTION: Final[float] = 0.05
"""More than this fraction of samples at either rail: the input is clipping."""


def decimate(values: Sequence[float], fs: int, rate: int) -> tuple[float, ...]:
    """Every ``fs // rate``-th sample (display only: no anti-alias filter needed for a screen)."""
    if not values or fs <= 0 or rate <= 0:
        return ()
    step = max(1, fs // rate)
    return tuple(values[::step])


def raw_quality(
    raw: Sequence[float], adc_max: int, flat_span: float = FLAT_SPAN_COUNTS
) -> tuple[SignalQuality, str] | None:
    """The quality verdicts that need no physiology: empty, flat, saturated, out of range.

    ``None`` when none of them applies and the processor must judge the rest.
    """
    if not raw:
        return SignalQuality.NO_SIGNAL, "aucun echantillon recu"
    # First, because min() and max() give position-dependent answers with a NaN.
    if not all(math.isfinite(v) for v in raw):
        return SignalQuality.NO_SIGNAL, "valeurs non numeriques : trame corrompue"
    low = min(raw)
    high = max(raw)
    if not (low >= 0.0 and high <= adc_max):
        return SignalQuality.NO_SIGNAL, f"valeurs hors de 0..{adc_max} : trame corrompue"
    if high - low <= flat_span:
        return SignalQuality.NO_SIGNAL, "signal plat : capteur debranche ou electrode decollee"
    rails = sum(1 for v in raw if v <= 0.0 or v >= adc_max)
    if rails > SATURATED_FRACTION * len(raw):
        return SignalQuality.NOISY, "signal sature : gain ou contact a verifier"
    return None


def blank_metrics(spec_metrics: Sequence[tuple[str, str, str]]) -> tuple[Metric, ...]:
    """Every metric a sensor reports, all ``None``: what a degraded window shows."""
    return tuple(
        Metric(key=k, label=label, value=None, unit=unit) for k, label, unit in spec_metrics
    )


def unprocessed(spec: SensorSpec, raw: Sequence[float], fs: int, at: Monotonic) -> SensorReading:
    """The reading of a sensor with no physiology implemented yet: raw counts, no metrics."""
    judged = raw_quality(raw, spec.kind.adc_max, spec.flat_span_counts)
    quality, detail = (
        judged if judged is not None else (SignalQuality.NOISY, "traitement non implemente")
    )
    return SensorReading(
        kind=spec.kind,
        at=at,
        display_rate=spec.display_rate,
        waveform=decimate(raw, fs, spec.display_rate),
        quality=quality,
        detail=detail,
        metrics=(),
    )
