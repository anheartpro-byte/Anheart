"""Synthetic surface EMG on BITalino channel A5, driven by the subject's g-load.

What is modelled, and why each part is there:

* **A carrier that looks like EMG to a spectrum.** Unit-variance white noise
  through a two-pole resonator (an AR(2) process) centred on ``fresh_hz``
  with a ``bandwidth_hz`` wide peak: a broadband, band-limited noise whose
  median frequency sits near the centre, like the interference pattern of
  many motor units.
* **Effort from the load.** In a centrifuge the subject braces against the
  load (legs, abdomen), so the tonic activation rises with the centripetal g:
  ``effort = min(1, g / effort_g)``, and a tonic amplitude
  ``tonic_mv * effort`` rides on the carrier.
* **Occasional voluntary contractions.** Bursts arrive as a Poisson process
  whose rate grows with the effort, each ``burst_min_s``..``burst_max_s``
  long, at ``burst_mv`` scaled by a random 0.6..1.0.
* **Fatigue.** Sustained effort slows the conduction velocity of the muscle
  fibres, which moves the EMG spectrum down: the classic fatigue index is a
  falling median frequency. A fatigue state ``F`` follows the effort with a
  first-order lag ``fatigue_tau_s`` and the carrier's centre moves from
  ``fresh_hz`` down to ``fatigued_hz`` as ``F`` goes from 0 to 1.
* **Electrode and amplifier noise**, broadband, at ``baseline_mv`` rms.
* **Optional contamination**, off by default: mains hum at ``mains_hz`` and
  ECG crosstalk (a QRS-shaped pulse at the subject's RR interval), so the
  processor's quality grading can be exercised by name.

The output is quantised exactly as the channel does it. A5 is **6-bit** on
the wire: the PLUX transfer function (see ``src/sensors/emg.py``) inverted is
``count = 2^n * (mV / 1000 * G / VCC + 1/2)``, so one count is
``3.3 / 64 / 1009 * 1000 = 0.0511 mV`` and the range is about +-1.63 mV. Counts
are clamped to ``0..adc_max``. The constants are re-derived here from the
datasheet rather than imported from the processor, on purpose: a simulator
that borrowed the processor's arithmetic would be marking its own homework;
``tests/test_sensor_emg.py`` checks that the two agree.

Stdlib only (``math`` and ``random``), stateful, seeded, total, and it never
reads a clock: time comes from the block's :class:`SignalContext`.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from src.sensors.base import SensorKind
from src.sim.signals.base import SignalContext, flat
from src.units import AdcCount, GLoad, Hertz, Millivolts, Seconds

VCC_V: Final[float] = 3.3
"""BITalino supply voltage, per the PLUX EMG datasheet."""

GAIN: Final[float] = 1009.0
"""EMG sensor gain, per the PLUX EMG datasheet."""

MAX_CENTRE_FRACTION: Final[float] = 0.4
"""The carrier's centre is kept below this fraction of the sample rate (Nyquist 0.5)."""

QRS_SIGMA_S: Final[float] = 0.008
"""Width of the Mexican-hat pulse standing in for a QRS complex (~28 Hz peak)."""

QRS_DELAY_S: Final[float] = 0.04
"""Centre of the pulse after the beat instant, so the whole pulse is rendered."""

QRS_SPAN_S: Final[float] = 0.08
"""Samples further than this after the beat carry no crosstalk."""


@dataclass(frozen=True, slots=True)
class EMGConfig:
    """Everything that shapes the synthetic EMG. Defaults: a clean recording."""

    baseline_mv: Millivolts = Millivolts(0.04)
    """RMS of electrode and amplifier noise: ~0.8 count, what a relaxed muscle reads."""

    tonic_mv: Millivolts = Millivolts(0.25)
    """RMS of the bracing activity at full effort."""

    burst_mv: Millivolts = Millivolts(0.45)
    """RMS of a voluntary contraction (times a random 0.6..1.0)."""

    effort_g: GLoad = GLoad(1.0)
    """Load at which the bracing effort is full."""

    burst_rate_rest_hz: Hertz = Hertz(0.05)
    """Contractions per second with no load."""

    burst_rate_effort_hz: Hertz = Hertz(0.25)
    """Extra contractions per second at full effort."""

    burst_min_s: Seconds = Seconds(0.4)
    burst_max_s: Seconds = Seconds(1.2)

    fresh_hz: Hertz = Hertz(110.0)
    """Carrier centre of a rested muscle."""

    fatigued_hz: Hertz = Hertz(65.0)
    """Carrier centre of a fully fatigued muscle."""

    bandwidth_hz: Hertz = Hertz(60.0)
    """Width of the carrier's spectral peak."""

    fatigue_tau_s: Seconds = Seconds(300.0)
    """Time constant of the fatigue state following the effort."""

    mains_mv: Millivolts = Millivolts(0.0)
    """Amplitude of mains hum; 0 disables it."""

    mains_hz: Hertz = Hertz(50.0)

    ecg_mv: Millivolts = Millivolts(0.0)
    """Peak of the ECG crosstalk pulse; 0 disables it."""


DEFAULT_EMG_CONFIG: Final[EMGConfig] = EMGConfig()


def effort_of(g_load: GLoad, config: EMGConfig) -> float:
    """Bracing effort in 0..1 for a centripetal load. Total (a bad config reads as no effort)."""
    if not config.effort_g > 0.0 or not math.isfinite(g_load):
        return 0.0
    return min(1.0, max(0.0, abs(g_load) / config.effort_g))


def to_count(mv: float, adc_max: int) -> AdcCount:
    """The inverse PLUX transfer function, rounded and clamped to ``0..adc_max``."""
    levels = adc_max + 1
    ideal = levels * (mv / 1000.0 * GAIN / VCC_V + 0.5)
    if not math.isfinite(ideal):
        return AdcCount(levels // 2)
    return AdcCount(min(adc_max, max(0, round(ideal))))


def _qrs(since_beat: float) -> float:
    """Unit-peak Mexican hat centred ``QRS_DELAY_S`` after the beat."""
    tau = (since_beat - QRS_DELAY_S) / QRS_SIGMA_S
    return (1.0 - tau * tau) * math.exp(-0.5 * tau * tau)


class EMGGenerator:
    """The simulated EMG channel.

    **Mutable by design**: the resonator's two past outputs, the contraction
    in progress, the fatigue state and the ECG beat phase all carry from one
    block to the next, so consecutive blocks form one continuous recording.
    """

    __slots__ = (
        "_burst_amp",
        "_burst_left",
        "_config",
        "_fatigue",
        "_rng",
        "_seed",
        "_since_beat",
        "_y1",
        "_y2",
    )

    def __init__(self, seed: int = 0, config: EMGConfig = DEFAULT_EMG_CONFIG) -> None:
        self._seed: int = seed
        self._config: EMGConfig = config
        self._rng: random.Random = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self._y1: float = 0.0
        self._y2: float = 0.0
        self._burst_left: int = 0
        self._burst_amp: float = 0.0
        self._fatigue: float = 0.0
        self._since_beat: float = 0.0

    @property
    def kind(self) -> SensorKind:
        return SensorKind.EMG

    @property
    def fatigue(self) -> float:
        """The fatigue state, 0 (rested) to 1 (fully fatigued). For tests and logs."""
        return self._fatigue

    def centre_hz(self, fs: int) -> float:
        """Where the carrier's spectral peak sits now, at sample rate ``fs``."""
        cfg = self._config
        centre = cfg.fresh_hz - (cfg.fresh_hz - cfg.fatigued_hz) * self._fatigue
        return min(max(centre, 1.0), MAX_CENTRE_FRACTION * fs)

    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        kind = SensorKind.EMG
        if context.count <= 0 or context.fs <= 0:
            return flat(kind, context.count)
        cfg = self._config
        fs = context.fs
        dt = 1.0 / fs
        effort = effort_of(context.subject.g_load, cfg)

        step = min(1.0, context.count * dt / cfg.fatigue_tau_s) if cfg.fatigue_tau_s > 0 else 1.0
        self._fatigue += (effort - self._fatigue) * step

        omega = 2.0 * math.pi * self.centre_hz(fs) / fs
        radius = math.exp(-math.pi * max(cfg.bandwidth_hz, 1.0) / fs)
        a1 = 2.0 * radius * math.cos(omega)
        a2 = -radius * radius
        # Stationary variance of y = e + a1*y1 + a2*y2 for unit-variance e.
        variance = (1.0 - a2) / ((1.0 + a2) * ((1.0 - a2) ** 2 - a1 * a1))
        norm = 1.0 / math.sqrt(variance)

        tonic = cfg.tonic_mv * effort
        rate = cfg.burst_rate_rest_hz + cfg.burst_rate_effort_hz * effort
        rr = context.subject.rr_interval
        out: list[AdcCount] = []
        for i in range(context.count):
            y = self._rng.gauss(0.0, 1.0) + a1 * self._y1 + a2 * self._y2
            self._y2 = self._y1
            self._y1 = y
            if self._burst_left > 0:
                self._burst_left -= 1
            elif self._rng.random() < rate * dt:
                duration = self._rng.uniform(cfg.burst_min_s, cfg.burst_max_s)
                self._burst_left = max(1, round(duration * fs))
                self._burst_amp = cfg.burst_mv * self._rng.uniform(0.6, 1.0)
            burst = self._burst_amp if self._burst_left > 0 else 0.0
            amplitude = math.sqrt(tonic * tonic + burst * burst)
            mv = amplitude * norm * y + cfg.baseline_mv * self._rng.gauss(0.0, 1.0)
            if cfg.mains_mv != 0.0:
                t = context.start + i * dt
                mv += cfg.mains_mv * math.sin(2.0 * math.pi * cfg.mains_hz * t)
            if cfg.ecg_mv != 0.0:
                self._since_beat += dt
                if rr > 0.0 and self._since_beat >= rr:
                    self._since_beat -= rr
                if self._since_beat <= QRS_SPAN_S:
                    mv += cfg.ecg_mv * _qrs(self._since_beat)
            out.append(to_count(mv, kind.adc_max))
        return tuple(out)


def make(seed: int = 0) -> EMGGenerator:
    """A fresh generator; the same seed replays the same signal."""
    return EMGGenerator(seed)
