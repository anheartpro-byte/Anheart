"""Synthetic finger photoplethysmogram for channel SpO2 (A3).

The channel carries one wavelength's PPG (see ``src/sensors/spo2.py`` for why
that is the honest assumption), so this renders exactly that, following the
subject's own heart:

* **One pulse per heartbeat.** The beat phase advances at ``1 / rr_interval``,
  with a 1% per-beat jitter, so the pulse rate is the plant's heart rate.
* **Pulse shape**: a systolic wave, the dicrotic notch, and a smaller
  diastolic (reflected) wave - two Gaussians per beat.
* **Respiration** moves the baseline and modulates the amplitude, at a rate
  that rises with the heart rate.
* **Vasoconstriction under g**: the pulse amplitude falls as
  ``1 / (1 + k * g)`` - peripheral perfusion drops as blood pools away from
  the hand.
* **Motion artefact grows with g**: above an onset load, bursts of a few-hertz
  oscillation (a clipped finger shaken on the arm) start at a rate and with an
  amplitude proportional to the load.

Seeded, stateful (beat, breath and burst phases carry across blocks), never
reads a clock, never raises, and returns counts within ``0..1023``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from src.sensors.base import SensorKind
from src.sim.signals.base import SignalContext
from src.units import AdcCount

DC_COUNTS: Final[float] = 520.0
"""Mean level of the photodiode amplifier with a finger in the clip."""

PULSE_COUNTS: Final[float] = 60.0
"""Systolic amplitude at rest (1 g-free)."""

VASOCONSTRICTION_PER_G: Final[float] = 0.35
"""``amplitude = PULSE_COUNTS / (1 + k * g)``."""

SYSTOLIC_AT: Final[float] = 0.18
SYSTOLIC_WIDTH: Final[float] = 0.07
DIASTOLIC_AT: Final[float] = 0.46
DIASTOLIC_WIDTH: Final[float] = 0.10
DIASTOLIC_SHARE: Final[float] = 0.4
"""Beat shape, in fractions of the beat period."""

BEAT_JITTER: Final[float] = 0.01
"""Relative standard deviation of one beat's duration."""

RESP_BASELINE_COUNTS: Final[float] = 8.0
RESP_AMPLITUDE_SHARE: Final[float] = 0.05
RESP_HZ_AT_REST: Final[float] = 0.25
RESP_HZ_PER_BPM: Final[float] = 0.004
RESP_HZ_MIN: Final[float] = 0.15
RESP_HZ_MAX: Final[float] = 0.6
REST_BPM: Final[float] = 70.0

NOISE_COUNTS: Final[float] = 0.8
"""White sensor noise, standard deviation."""

BURST_ONSET_G: Final[float] = 2.0
BURSTS_PER_SECOND_PER_G: Final[float] = 0.15
BURST_COUNTS_PER_G: Final[float] = 25.0
BURST_SECONDS: Final[float] = 0.6
BURST_HZ: Final[float] = 4.0

MIN_RR_SECONDS: Final[float] = 0.2
"""Floor on the beat interval, so a corrupt state cannot stall the phase."""

MAX_G_MODELLED: Final[float] = 20.0
"""Ceiling on the load the model is rendered at. Far past anything survivable;
it exists so a corrupt ``g_load`` (huge, infinite, NaN) cannot turn the motion
term into infinity and the sample into NaN."""

SECONDS_PER_MINUTE: Final[float] = 60.0
TWO_PI: Final[float] = 2.0 * math.pi


@dataclass(frozen=True, slots=True)
class _Block:
    """What stays constant over one rendered block: the subject is one state per block."""

    dt: float
    """Sample period, s."""

    rr: float
    """Beat interval, s (floored)."""

    g: float
    """Load, g (clamped to 0..MAX_G_MODELLED)."""

    amplitude: float
    """Pulse amplitude after vasoconstriction, counts."""

    resp_hz: float
    burst_chance: float
    """Probability that a motion burst starts on a given sample."""

    burst_samples: int


class SpO2Generator:
    """The simulated SpO2 channel: a finger PPG. **Mutable** (the phases)."""

    __slots__ = (
        "_beat_phase",
        "_burst_amplitude",
        "_burst_left",
        "_burst_phase",
        "_jitter",
        "_resp_phase",
        "_rng",
        "_seed",
    )

    def __init__(self, seed: int = 0) -> None:
        self._seed: int = seed
        self._rng: random.Random = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self._beat_phase: float = 0.0
        self._jitter: float = 1.0
        self._resp_phase: float = 0.0
        self._burst_left: int = 0
        self._burst_amplitude: float = 0.0
        self._burst_phase: float = 0.0

    @property
    def kind(self) -> SensorKind:
        return SensorKind.SPO2

    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        if context.count <= 0 or context.fs <= 0:
            return ()
        dt = 1.0 / context.fs
        subject = context.subject
        rr = max(MIN_RR_SECONDS, float(subject.rr_interval))
        g = min(MAX_G_MODELLED, max(0.0, float(subject.g_load)))  # NaN -> 0.0
        amplitude = PULSE_COUNTS / (1.0 + VASOCONSTRICTION_PER_G * g)
        resp_hz = min(
            RESP_HZ_MAX,
            max(
                RESP_HZ_MIN,
                RESP_HZ_AT_REST + RESP_HZ_PER_BPM * (SECONDS_PER_MINUTE / rr - REST_BPM),
            ),
        )
        block = _Block(
            dt=dt,
            rr=rr,
            g=g,
            amplitude=amplitude,
            resp_hz=resp_hz,
            burst_chance=BURSTS_PER_SECOND_PER_G * max(0.0, g - BURST_ONSET_G) * dt,
            burst_samples=max(1, round(BURST_SECONDS * context.fs)),
        )
        return tuple(self._sample(block) for _ in range(context.count))

    def _sample(self, block: _Block) -> AdcCount:
        rng = self._rng
        dt, g, amplitude = block.dt, block.g, block.amplitude
        rr, resp_hz = block.rr, block.resp_hz
        burst_chance, burst_samples = block.burst_chance, block.burst_samples
        self._beat_phase += dt / (rr * self._jitter)
        if self._beat_phase >= 1.0:
            self._beat_phase %= 1.0
            self._jitter = max(0.5, 1.0 + rng.gauss(0.0, BEAT_JITTER))
        self._resp_phase = (self._resp_phase + resp_hz * dt) % 1.0
        breath = math.sin(TWO_PI * self._resp_phase)

        if self._burst_left == 0 and rng.random() < burst_chance:
            self._burst_left = burst_samples
            self._burst_amplitude = BURST_COUNTS_PER_G * g * (0.5 + rng.random())
        motion = 0.0
        if self._burst_left > 0:
            self._burst_left -= 1
            self._burst_phase = (self._burst_phase + BURST_HZ * dt) % 1.0
            motion = self._burst_amplitude * math.sin(TWO_PI * self._burst_phase)

        value = (
            DC_COUNTS
            + RESP_BASELINE_COUNTS * breath
            + amplitude * (1.0 + RESP_AMPLITUDE_SHARE * breath) * pulse_shape(self._beat_phase)
            + motion
            + rng.gauss(0.0, NOISE_COUNTS)
        )
        return AdcCount(min(SensorKind.SPO2.adc_max, max(0, round(value))))


def pulse_shape(phase: float) -> float:
    """One beat's normalised volume wave at ``phase`` in ``[0, 1)``: peak about 1."""
    systolic = math.exp(-0.5 * ((phase - SYSTOLIC_AT) / SYSTOLIC_WIDTH) ** 2)
    diastolic = math.exp(-0.5 * ((phase - DIASTOLIC_AT) / DIASTOLIC_WIDTH) ** 2)
    return systolic + DIASTOLIC_SHARE * diastolic


def make(seed: int = 0) -> SpO2Generator:
    """A fresh generator; the same seed replays the same signal."""
    return SpO2Generator(seed)
