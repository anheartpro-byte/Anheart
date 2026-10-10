"""What a simulated sensor is given, and what it must return.

The simulated BITalino (``src/sim/bitalino.py``) renders the ECG itself, from
the physiology plant. Every OTHER channel is rendered by one generator here,
from the same :class:`~src.sim.physiology.SubjectState`, so that every channel
of a simulated session describes the same person at the same instant: the
breathing speeds up with the heart rate, the skin conductance rises with the
load, the muscle fires with the g.

A generator is stateful (phase of the breath, the last skin-conductance
response) and deterministic for a given seed, so a scenario replays exactly.
It returns ADC counts within its channel's range, never raises, and never
reads a clock: the block's start time arrives in the context.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from src.sensors.base import SensorKind
from src.sim.physiology import SubjectState
from src.units import AdcCount, Monotonic


@dataclass(frozen=True, slots=True)
class SignalContext:
    """One block to render: when it starts, how fast, and the subject meanwhile."""

    start: Monotonic
    """Time of the block's first sample."""

    fs: int
    count: int
    subject: SubjectState
    """The subject integrated to the END of the block (as the ECG uses it)."""


class SignalGenerator(Protocol):
    """One channel's synthetic signal."""

    @property
    @abstractmethod
    def kind(self) -> SensorKind:
        """The channel this generator synthesises."""

    @abstractmethod
    def render(self, context: SignalContext) -> tuple[AdcCount, ...]:
        """``context.count`` samples, each within ``0..kind.adc_max``."""


def flat(kind: SensorKind, count: int) -> tuple[AdcCount, ...]:
    """Mid-scale constant: what an unmodelled channel reads (graded no_signal downstream)."""
    return (AdcCount(kind.adc_max // 2),) * max(0, count)
