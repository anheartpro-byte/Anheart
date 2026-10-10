"""Synthetic EDA: a tonic level that follows the load, and bi-exponential SCRs.

The model, per sample (``dt = 1 / fs``):

* **Tonic (SCL)** relaxes first-order (time constant :attr:`EDAModel.tau_tonic`)
  towards ``rest_scl + scl_per_g * g + scl_per_bpm * max(0, HR - hr_rest)``,
  capped at :attr:`EDAModel.scl_max`: the skin conducts more as the subject
  sweats under load.
* **Phasic (SCRs)** arrive as a Poisson process whose rate rises with the same
  two stressors (``rest_rate + rate_per_g * g + rate_per_bpm * (HR - rest)``,
  capped). Each has the bi-exponential shape of Benedek & Kaernbach (J.
  Neurosci. Methods 190, 2010), ``exp(-t/tau_decay) - exp(-t/tau_rise)``
  scaled so its peak is the drawn amplitude; with 0.75 s / 2 s it peaks
  1.18 s after onset. Amplitudes are ``min + Exp(mean)``, clamped.
* A slow Ornstein-Uhlenbeck wander on the tonic level (SCL is never a ruler
  line), about one count of Gaussian sensor noise (which also dithers the
  0.024 uS quantisation), and the
  sum goes through the inverse of the datasheet transfer function
  (:func:`src.sensors.eda.adc_count`), clamped to 0..1023.

Deterministic for a seed, stateful across blocks, never reads a clock and
never raises; the subject is taken as constant over a block, as the ECG does.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from src.sensors.base import SensorKind
from src.sensors.eda import Microsiemens, adc_count
from src.sim.signals.base import SignalContext
from src.units import AdcCount, Bpm, Seconds

# Module constants rather than calls in the dataclass defaults (RUF009).
_REST_SCL: Final[Microsiemens] = Microsiemens(3.0)
_SCL_MAX: Final[Microsiemens] = Microsiemens(20.0)
_AMPLITUDE_MIN: Final[Microsiemens] = Microsiemens(0.1)
_AMPLITUDE_MEAN: Final[Microsiemens] = Microsiemens(0.3)
_AMPLITUDE_MAX: Final[Microsiemens] = Microsiemens(2.0)
_WANDER: Final[Microsiemens] = Microsiemens(0.03)
_NOISE: Final[Microsiemens] = Microsiemens(0.02)


@dataclass(frozen=True, slots=True)
class EDAModel:
    """The synthetic subject's electrodermal numbers. Literature-shaped placeholders."""

    rest_scl: Microsiemens = _REST_SCL
    scl_per_g: float = 1.5
    """uS of tonic level per g of centripetal load."""
    scl_per_bpm: float = 0.03
    """uS of tonic level per bpm above rest."""
    scl_max: Microsiemens = _SCL_MAX
    tau_tonic: Seconds = Seconds(20.0)
    hr_rest: Bpm = Bpm(70)

    rest_rate: float = 2.0
    """SCRs per minute at rest."""
    rate_per_g: float = 4.0
    rate_per_bpm: float = 0.08
    rate_max: float = 15.0

    amplitude_min: Microsiemens = _AMPLITUDE_MIN
    amplitude_mean: Microsiemens = _AMPLITUDE_MEAN
    """Mean of the exponential part above ``amplitude_min``."""
    amplitude_max: Microsiemens = _AMPLITUDE_MAX
    tau_rise: Seconds = Seconds(0.75)
    tau_decay: Seconds = Seconds(2.0)
    wander: Microsiemens = _WANDER
    """Standard deviation of the tonic level's slow random fluctuation."""
    tau_wander: Seconds = Seconds(20.0)
    noise: Microsiemens = _NOISE
    """Sensor noise, about one count rms."""


DEFAULT_EDA_MODEL: Final[EDAModel] = EDAModel()

_SCR_LIFETIME_DECAYS: Final[float] = 10.0
"""An SCR is dropped once this many decay constants old (below 5e-5 of its peak)."""

_SECONDS_PER_MINUTE: Final[float] = 60.0


def scr_shape(t: float, model: EDAModel = DEFAULT_EDA_MODEL) -> float:
    """The unit-peak bi-exponential response ``t`` seconds after onset (0 before it)."""
    if t <= 0.0:
        return 0.0
    rise, decay = float(model.tau_rise), float(model.tau_decay)
    t_peak = math.log(decay / rise) * rise * decay / (decay - rise)
    norm = math.exp(-t_peak / decay) - math.exp(-t_peak / rise)
    return (math.exp(-t / decay) - math.exp(-t / rise)) / norm


class EDAGenerator:
    """The simulated EDA channel.

    **Mutable by design**: it is a subject's skin, with a tonic level and the
    responses still decaying on it. Every mutation happens in :meth:`render`.
    """

    __slots__ = (
        "_model",
        "_responses",
        "_rng",
        "_seed",
        "_started",
        "_t",
        "_tonic",
        "_wander",
    )

    def __init__(self, seed: int = 0, model: EDAModel = DEFAULT_EDA_MODEL) -> None:
        self._seed: int = seed
        self._model: EDAModel = model
        self._rng: random.Random = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self._tonic: float = float(model.rest_scl)
        self._t: float = 0.0
        """Seconds of signal rendered so far: the generator's own timeline."""
        self._responses: list[tuple[float, float]] = []
        """(onset on the own timeline, amplitude in uS) of the SCRs still visible."""
        self._started: int = 0
        self._wander: float = 0.0
        """The tonic level's current random offset, in uS."""

    @property
    def kind(self) -> SensorKind:
        return SensorKind.EDA

    @property
    def tonic(self) -> Microsiemens:
        """The current tonic level (ground truth, for tests and logs)."""
        return Microsiemens(self._tonic)

    @property
    def responses_started(self) -> int:
        """SCRs begun since construction (ground truth, for tests and logs)."""
        return self._started

    def targets(self, context: SignalContext) -> tuple[Microsiemens, float]:
        """Where the tonic level is heading, and the SCR rate per minute, for this subject."""
        model = self._model
        g = float(context.subject.g_load)
        load = g if math.isfinite(g) and g > 0.0 else 0.0
        above = float(max(0, context.subject.heart_rate - model.hr_rest))
        tonic = model.rest_scl + model.scl_per_g * load + model.scl_per_bpm * above
        rate = model.rest_rate + model.rate_per_g * load + model.rate_per_bpm * above
        return Microsiemens(min(float(model.scl_max), tonic)), min(model.rate_max, rate)

    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        count = max(0, context.count)
        if count == 0:
            return ()
        model = self._model
        dt = 1.0 / context.fs if context.fs > 0 else 0.0
        target, rate = self.targets(context)
        relax = 1.0 - math.exp(-dt / float(model.tau_tonic))
        pull = dt / float(model.tau_wander)
        kick = float(model.wander) * math.sqrt(2.0 * pull)
        p_scr = rate / _SECONDS_PER_MINUTE * dt
        stress = 1.0 + 0.05 * min(10.0, max(0.0, rate - model.rest_rate))
        samples: list[AdcCount] = []
        for _ in range(count):
            self._tonic += (float(target) - self._tonic) * relax
            self._wander += -self._wander * pull + kick * self._rng.gauss(0.0, 1.0)
            if self._rng.random() < p_scr:
                drawn = model.amplitude_min + self._rng.expovariate(
                    1.0 / float(model.amplitude_mean)
                )
                self._responses.append((self._t, min(float(model.amplitude_max), drawn * stress)))
                self._started += 1
            phasic = sum(amp * scr_shape(self._t - onset, model) for onset, amp in self._responses)
            noise = self._rng.gauss(0.0, float(model.noise))
            samples.append(adc_count(Microsiemens(self._tonic + self._wander + phasic + noise)))
            self._t += dt
        horizon = _SCR_LIFETIME_DECAYS * float(model.tau_decay)
        self._responses = [(o, a) for o, a in self._responses if self._t - o < horizon]
        return tuple(samples)


def make(seed: int = 0) -> EDAGenerator:
    """A fresh generator; the same seed replays the same signal."""
    return EDAGenerator(seed)
