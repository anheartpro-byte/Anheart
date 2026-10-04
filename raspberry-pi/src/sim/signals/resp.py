"""Synthetic RESP: a PZT chest band on a subject who breathes harder as the load rises.

The model, per breath cycle (phase 0 -> 1, drawn afresh at every cycle start):

* **Rate** ``rest_rate + rate_per_bpm * (HR - hr_rest) + rate_per_g * g``,
  clamped to :attr:`RespConfig.rate_floor` .. :attr:`RespConfig.rate_ceiling`,
  with a breath-to-breath jitter of ``+-variability``.
* **Depth** (peak-to-peak, % of full scale) ``rest_depth * (1 + depth_per_effort
  * effort)`` where ``effort`` is the same drive normalised to 0..1, jittered.
* **Shape**: inhalation is the first :attr:`RespConfig.inhale_fraction` of the
  cycle (I:E about 1:1.5 at rest), a half-cosine rise; exhalation is a slower
  half-cosine fall. Continuous at both joins, so no spurious harmonics.
* **Sighs**: with probability :attr:`RespConfig.sigh_probability` a cycle is a
  sigh, :attr:`RespConfig.sigh_depth` times deeper and 1.5 times longer.
* **Motion**: band-limited (2-5 Hz) displacement proportional to the g-load,
  plus a slow postural baseline wander, plus a little sensor noise.
* **Scripted apnea**: inside any :attr:`RespConfig.apneas` window, the cycle in
  progress is finished and the next one is not started - the chest holds at
  end-expiration - until the window closes.

Samples are written through the inverse of the PLUX transfer function
(:func:`src.sensors.resp.to_count`), so the processor's forward transfer
function recovers the model's percentages exactly, up to quantisation.

Stateful (phase, current cycle, noise phases) and deterministic for a seed.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from src.sensors.base import SensorKind
from src.sensors.resp import SECONDS_PER_MINUTE, BreathsPerMinute, Percent, to_count
from src.sim.signals.base import SignalContext
from src.units import AdcCount, Bpm, Monotonic, Seconds

_MOTION_HZ: Final[tuple[float, ...]] = (2.1, 3.3, 4.7)
"""Frequencies of the band's movement under load: far above any breath."""

_WANDER_PERIOD_S: Final[float] = 90.0
_SIGH_STRETCH: Final[float] = 1.5

# Defaults of RespConfig, named so the dataclass defaults are plain names (a
# NewType call is an identity, but ruff's RUF009 cannot see that for a unit
# declared outside src/units.py).
_REST_RATE: Final[BreathsPerMinute] = BreathsPerMinute(12.0)
_RATE_FLOOR: Final[BreathsPerMinute] = BreathsPerMinute(6.0)
_RATE_CEILING: Final[BreathsPerMinute] = BreathsPerMinute(40.0)
_REST_DEPTH: Final[Percent] = Percent(10.0)
_MOTION_PER_G: Final[Percent] = Percent(0.4)
_WANDER: Final[Percent] = Percent(3.0)
_NOISE: Final[Percent] = Percent(0.05)


@dataclass(frozen=True, slots=True)
class Apnea:
    """A scripted pause in breathing."""

    start: Monotonic
    duration: Seconds

    def covers(self, t: float) -> bool:
        return self.start <= t < self.start + self.duration


@dataclass(frozen=True, slots=True)
class RespConfig:
    """The breathing model's knobs. Literature-shaped placeholders."""

    rest_rate: BreathsPerMinute = _REST_RATE
    hr_rest: Bpm = Bpm(70)
    """The heart rate at which the drive is zero (``PhysiologyConfig.hr_rest``)."""

    rate_per_bpm: float = 0.18
    """Breaths/min per bpm of heart rate above rest."""

    rate_per_g: float = 2.0
    """Breaths/min per g of centripetal load."""

    rate_floor: BreathsPerMinute = _RATE_FLOOR
    rate_ceiling: BreathsPerMinute = _RATE_CEILING
    effort_span_bpm: float = 100.0
    """Heart rate above rest that counts as full effort for the depth."""

    rest_depth: Percent = _REST_DEPTH
    depth_per_effort: float = 1.0
    """Depth multiplier at full effort is ``1 + depth_per_effort``."""

    inhale_fraction: float = 0.4
    variability: float = 0.04
    sigh_probability: float = 0.02
    sigh_depth: float = 2.5
    motion_per_g: Percent = _MOTION_PER_G
    """Motion artefact amplitude, % of full scale per g."""

    wander: Percent = _WANDER
    noise: Percent = _NOISE
    apneas: tuple[Apnea, ...] = ()


DEFAULT_RESP_CONFIG: Final[RespConfig] = RespConfig()


def breath_shape(phase: float, inhale_fraction: float) -> float:
    """Chest displacement over one cycle, 0 (end-expiration) .. 1 (end-inspiration)."""
    if phase < inhale_fraction:
        return 0.5 - 0.5 * math.cos(math.pi * phase / inhale_fraction)
    return 0.5 + 0.5 * math.cos(math.pi * (phase - inhale_fraction) / (1.0 - inhale_fraction))


class RESPGenerator:
    """The simulated RESP channel. **Mutable**: the breath in progress is state."""

    __slots__ = ("_config", "_depth", "_motion_phases", "_period", "_phase", "_random")

    def __init__(self, seed: int = 0, config: RespConfig = DEFAULT_RESP_CONFIG) -> None:
        self._config: RespConfig = config
        self._random: random.Random = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self._motion_phases: tuple[float, ...] = tuple(
            self._random.uniform(0.0, 2.0 * math.pi) for _ in _MOTION_HZ
        )
        # The cycle in progress. phase >= 1.0 means "none": the next sample starts one
        # (or, inside an apnea, holds at end-expiration).
        self._phase: float = 1.0
        self._period: float = 5.0
        self._depth: float = float(config.rest_depth)

    @property
    def kind(self) -> SensorKind:
        return SensorKind.RESP

    def target_rate(self, heart_rate: Bpm, g_load: float) -> BreathsPerMinute:
        """The rate the subject breathes at, before jitter."""
        c = self._config
        rate = c.rest_rate + c.rate_per_bpm * (heart_rate - c.hr_rest) + c.rate_per_g * g_load
        return BreathsPerMinute(min(c.rate_ceiling, max(c.rate_floor, rate)))

    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        c = self._config
        subject = context.subject
        g = abs(float(subject.g_load))
        rate = self.target_rate(subject.heart_rate, g)
        effort = min(1.0, max(0.0, (subject.heart_rate - c.hr_rest) / c.effort_span_bpm))
        depth = float(c.rest_depth) * (1.0 + c.depth_per_effort * effort)
        dt = 1.0 / context.fs if context.fs > 0 else 0.0
        samples: list[AdcCount] = []
        for i in range(max(0, context.count)):
            t = context.start + i * dt
            if self._phase >= 1.0 and not any(a.covers(t) for a in c.apneas):
                self._start_cycle(rate, depth)
            breath = self._depth * (breath_shape(min(self._phase, 1.0), c.inhale_fraction) - 0.5)
            if self._phase < 1.0:
                self._phase += dt / self._period
            value = breath + self._artefacts(t, g)
            samples.append(to_count(Percent(value)))
        return tuple(samples)

    def _start_cycle(self, rate: BreathsPerMinute, depth: float) -> None:
        c = self._config
        jitter = self._random.uniform(-c.variability, c.variability)
        sigh = self._random.random() < c.sigh_probability
        stretch = _SIGH_STRETCH if sigh else 1.0
        self._period = SECONDS_PER_MINUTE / rate * (1.0 + jitter) * stretch
        self._depth = depth * (1.0 + self._random.uniform(-c.variability, c.variability) * 2.5)
        self._depth *= c.sigh_depth if sigh else 1.0
        self._phase = 0.0

    def _artefacts(self, t: float, g: float) -> float:
        c = self._config
        motion = sum(
            math.sin(2.0 * math.pi * hz * t + phase)
            for hz, phase in zip(_MOTION_HZ, self._motion_phases, strict=True)
        )
        wander = float(c.wander) * math.sin(2.0 * math.pi * t / _WANDER_PERIOD_S)
        noise = self._random.gauss(0.0, float(c.noise))
        return float(c.motion_per_g) * g * motion / len(_MOTION_HZ) + wander + noise


def make(seed: int = 0) -> RESPGenerator:
    """A fresh generator; the same seed replays the same signal."""
    return RESPGenerator(seed)
