"""A sample source that corrupts the BITalino's raw samples before the REAL DSP sees them.

In ``dsp`` mode the heart rate the runtime acts on comes out of the production
path: simulated BITalino -> ``SignalTreatment`` (BioSPPy) -> ``EcgBridge``.
This wrapper sits between the first two, on the one seam the bridge reads
(``read_samples``), and does to the samples what a bad cable, a dead
amplifier or a lossy radio does to them (:class:`~simulation.scenario.SignalFault`).
The DSP, its quality grade and the runtime's gates are then left to cope, which
is the whole point: nothing here decides what the heart rate is.

Every corruption is bounded in time and seeded, so a run is reproducible.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import replace
from typing import Final, Protocol, assert_never, final

from simulation.scenario import SignalFault
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import Clock
from src.units import Monotonic

ADC_FULL_SCALE: Final[float] = 1023.0
"""A 10-bit BITalino channel's top count."""

ADC_MIDSCALE: Final[float] = 512.0
"""What a flat, centred line reads."""

MAINS_HZ: Final[float] = 50.0
MAINS_COUNTS: Final[float] = 300.0
"""Hum amplitude in counts: ~0.9 mV at the sensor, well above the R wave."""

GAP_KEEP_EVERY: Final[int] = 4
"""With ``GAPS``, one batch in this many survives."""

SAMPLE_RATE: Final[int] = 1000


class SampleSource(Protocol):
    """What the bridge reads (``src.ecg_pipeline.SampleSource``)."""

    async def read_samples(self, count: int = ...) -> SampleBatch | None: ...


@final
class FaultySource:
    """Delegates to ``inner``; corrupts while a fault is armed. Mutable, owned by the loop."""

    __slots__ = ("_batches", "_clock", "_fault", "_inner", "_rng", "_until")

    def __init__(self, inner: SampleSource, clock: Clock, *, seed: int) -> None:
        self._inner: SampleSource = inner
        self._clock: Clock = clock
        self._rng: random.Random = random.Random(seed)  # noqa: S311  # simulation noise, not crypto
        self._fault: SignalFault | None = None
        self._until: Monotonic | None = None
        self._batches: int = 0

    def corrupt(self, fault: SignalFault, until: Monotonic) -> None:
        """Apply ``fault`` to every batch read before ``until``."""
        self._fault = fault
        self._until = until

    def _active(self) -> SignalFault | None:
        until = self._until
        if until is None or self._clock.monotonic() >= until:
            return None
        return self._fault

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        """The next batch, corrupted or dropped as armed; ``None`` when there is none."""
        while True:
            batch = await self._inner.read_samples(count)
            fault = self._active()
            if batch is None or fault is None:
                return batch
            self._batches += 1
            match fault:
                case SignalFault.STOPPED:
                    continue  # read and lost: the frames never arrived
                case SignalFault.GAPS:
                    if self._batches % GAP_KEEP_EVERY:
                        continue
                    return batch
                case SignalFault.FLAT:
                    return _rewrite(batch, lambda: ADC_MIDSCALE)
                case SignalFault.SATURATED:
                    return _rewrite(batch, lambda: ADC_FULL_SCALE)
                case SignalFault.CORRUPTED:
                    return _rewrite(batch, self._random_count)
                case SignalFault.MAINS:
                    return _hum(batch, self._batches)
                case _ as unreachable:
                    assert_never(unreachable)

    def _random_count(self) -> float:
        """One uniformly random ADC count (a frame that decodes into noise)."""
        return float(self._rng.randint(0, int(ADC_FULL_SCALE)))


def _rewrite(batch: SampleBatch, sample: Callable[[], float]) -> SampleBatch:
    """Every sample of every channel replaced by ``sample()``."""
    channels = [
        ChannelData(channel=data.channel, values=[sample() for _ in data.values])
        for data in batch.channels
    ]
    return replace(batch, channels=channels)


def _hum(batch: SampleBatch, index: int) -> SampleBatch:
    """A 50 Hz sinusoid added to every channel, clipped to the ADC range."""
    channels: list[ChannelData] = []
    for data in batch.channels:
        start = index * len(data.values)
        hummed = [
            min(
                ADC_FULL_SCALE,
                max(
                    0.0,
                    value
                    + MAINS_COUNTS * math.sin(2.0 * math.pi * MAINS_HZ * (start + i) / SAMPLE_RATE),
                ),
            )
            for i, value in enumerate(data.values)
        ]
        channels.append(ChannelData(channel=data.channel, values=hummed))
    return replace(batch, channels=channels)
