"""Synthetic LUX signal: the light a sensor in the rotating capsule sees.

Model, in % of the sensor range (PLUX transfer function, see
``src/sensors/lux.py``), then quantised to the 6-bit A6 channel:

    light = room + depth * ((1 + cos(phase)) / 2)^2 + noise

* ``room`` is the ambient level: :attr:`LuxModel.room_percent` with the lights
  on, :attr:`LuxModel.room_percent` minus :attr:`LuxModel.switch_drop_percent`
  with them off. Lights are switched at random (a Poisson process of mean
  interval :attr:`LuxModel.switch_interval_s`): the "events" the processor
  counts.
* the flicker: once per turn of the arm the capsule passes a lamp fixed in the
  room. ``phase`` advances at the subject's OUTPUT speed
  (``SubjectState.output_rpm``); the squared raised cosine is a smooth pulse
  whose fundamental dominates its harmonics (16:1 in power), as a lamp seen
  through a wide window does. ``depth`` is :attr:`LuxModel.flicker_depth_percent`.
* ``noise`` is white, :attr:`LuxModel.noise_counts` counts RMS, before the
  quantisation to whole counts in ``0..63``.

Stateful (the arm's phase, the lights) and seeded: a seed replays the same
room. Never reads a clock; the sample rate arrives in the context.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Final

from src.sensors.base import SensorKind
from src.sim.signals.base import SignalContext
from src.units import AdcCount

FULL_SCALE_COUNTS: Final[int] = 1 << SensorKind.LUX.adc_bits
"""``2^n`` of the transfer function: counts = % / 100 x 64."""


@dataclass(frozen=True, slots=True)
class LuxModel:
    """The simulated room and lamp."""

    room_percent: float = 45.0
    """Ambient light with the lights on, % of range."""

    flicker_depth_percent: float = 10.0
    """Peak-to-peak rise as the capsule passes the lamp, % of range. 0: no lamp."""

    noise_counts: float = 0.35
    """White noise RMS, in counts (about the 6-bit quantisation noise)."""

    switch_interval_s: float = 300.0
    """Mean time between light switches. ``math.inf``: never switched."""

    switch_drop_percent: float = 25.0
    """How much darker the room is with the lights off, % of range."""

    def __post_init__(self) -> None:
        """Reject a room that cannot exist. Raised at construction, nothing spinning."""
        for name, value in (
            ("room_percent", self.room_percent),
            ("flicker_depth_percent", self.flicker_depth_percent),
            ("noise_counts", self.noise_counts),
            ("switch_drop_percent", self.switch_drop_percent),
        ):
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and not negative, got {value}")
        if not self.switch_interval_s > 0.0:
            raise ValueError(f"switch_interval_s must be positive, got {self.switch_interval_s}")


DEFAULT_LUX_MODEL: Final[LuxModel] = LuxModel()


class LUXGenerator:
    """The simulated LUX channel.

    Mutable by design: the arm's phase and the state of the lights carry over
    from one block to the next, as they do in the room.
    """

    __slots__ = ("_lights_on", "_model", "_phase", "_rng")

    def __init__(self, seed: int = 0, model: LuxModel = DEFAULT_LUX_MODEL) -> None:
        self._model: LuxModel = model
        self._rng: random.Random = random.Random(seed)  # noqa: S311 - simulation, not crypto
        self._phase: float = self._rng.uniform(0.0, 2.0 * math.pi)
        self._lights_on: bool = True

    @property
    def kind(self) -> SensorKind:
        return SensorKind.LUX

    @property
    def lights_on(self) -> bool:
        """Whether the room lights are on at the end of the last block. For tests."""
        return self._lights_on

    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        if context.fs <= 0:
            return ()
        model = self._model
        dt = 1.0 / context.fs
        step = 2.0 * math.pi * abs(float(context.subject.output_rpm)) / 60.0 * dt
        switch_probability = dt / model.switch_interval_s
        top = SensorKind.LUX.adc_max
        samples: list[AdcCount] = []
        for _ in range(max(0, context.count)):
            if self._rng.random() < switch_probability:
                self._lights_on = not self._lights_on
            room = model.room_percent - (0.0 if self._lights_on else model.switch_drop_percent)
            passing = ((1.0 + math.cos(self._phase)) / 2.0) ** 2
            percent = max(0.0, room) + model.flicker_depth_percent * passing
            counts = percent / 100.0 * FULL_SCALE_COUNTS + self._rng.gauss(0.0, model.noise_counts)
            samples.append(AdcCount(min(top, max(0, round(counts)))))
            self._phase = (self._phase + step) % (2.0 * math.pi)
        return tuple(samples)


def make(seed: int = 0) -> LUXGenerator:
    """A fresh generator; the same seed replays the same signal."""
    return LUXGenerator(seed)
