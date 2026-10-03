"""Every acquired channel, windowed and processed: what the sensor pages show.

Fed by the ECG bridge's tap (:meth:`SensorHub.accept`, every batch, every
channel, on the event loop) and refreshed once a second
(:meth:`SensorHub.refresh`): the windows are copied on the loop, processed on
a worker thread, and the readings published back on the loop. The heart-rate
control path does not go through here (see ``src/sensors/base.py``).

A processor is required to be total, and each one is tested for it. The hub
still fences every call: these are monitoring views, and a bug in one sensor's
arithmetic must neither silence the others nor stop the console. A processor
that raises gets a NO_SIGNAL reading saying so, and the exception is logged.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from src.bitalino_client import SampleBatch
from src.clock import Clock
from src.sensors.base import SensorKind, SensorProcessor, SensorReading
from src.sensors.registry import processor_for
from src.training.types import SignalQuality
from src.units import Monotonic

_logger: Final[logging.Logger] = logging.getLogger(__name__)

type Offload = Callable[
    [Callable[[], tuple[SensorReading, ...]]], Awaitable[tuple[SensorReading, ...]]
]
"""Runs the processing somewhere other than the loop; tests pass an inline one."""


async def to_thread(work: Callable[[], tuple[SensorReading, ...]]) -> tuple[SensorReading, ...]:
    """The production :data:`Offload`: a worker thread."""
    return await asyncio.to_thread(work)


@dataclass(frozen=True, slots=True)
class _Window:
    kind: SensorKind
    samples: tuple[float, ...]
    at: Monotonic


class SensorHub:
    """Rolling windows per channel and the latest reading of each.

    Mutable, owned by the event loop: :meth:`accept` and the publication step
    of :meth:`refresh` both run there, and the worker thread only ever sees
    immutable copies of the windows.
    """

    __slots__ = ("_buffers", "_clock", "_fs", "_last_at", "_latest", "_offload", "_processors")

    def __init__(
        self,
        *,
        clock: Clock,
        kinds: Sequence[SensorKind],
        fs: int,
        processors: Mapping[SensorKind, SensorProcessor] | None = None,
        offload: Offload = to_thread,
    ) -> None:
        if fs <= 0:
            raise ValueError(f"sample rate must be positive, got {fs}")
        self._clock: Clock = clock
        self._fs: int = fs
        self._offload: Offload = offload
        chosen = {} if processors is None else dict(processors)
        self._processors: dict[SensorKind, SensorProcessor] = {
            kind: chosen.get(kind) or processor_for(kind) for kind in dict.fromkeys(kinds)
        }
        self._buffers: dict[SensorKind, deque[float]] = {
            kind: deque(maxlen=max(1, round(float(p.spec.window_s) * fs)))
            for kind, p in self._processors.items()
        }
        self._last_at: Monotonic | None = None
        self._latest: dict[SensorKind, SensorReading] = {}

    @property
    def kinds(self) -> tuple[SensorKind, ...]:
        """The channels this hub processes, in configuration order."""
        return tuple(self._processors)

    @property
    def sample_rate(self) -> int:
        """Acquisition rate the windows are sized for."""
        return self._fs

    def processor(self, kind: SensorKind) -> SensorProcessor | None:
        """The processor for ``kind``, or ``None`` if it is not configured."""
        return self._processors.get(kind)

    def latest(self) -> Mapping[SensorKind, SensorReading]:
        """The most recent reading of every channel processed so far."""
        return MappingProxyType(dict(self._latest))

    def accept(self, batch: SampleBatch) -> None:
        """Append a batch's samples to their channels' windows. Unknown channels are ignored."""
        by_name = {kind.value: kind for kind in self._buffers}
        for column in batch.channels:
            kind = by_name.get(column.channel)
            if kind is not None:
                self._buffers[kind].extend(column.values)
        self._last_at = self._clock.monotonic()

    async def refresh(self) -> tuple[SensorReading, ...]:
        """Process every window off the loop and publish the readings. Returns them."""
        at = self._last_at if self._last_at is not None else self._clock.monotonic()
        windows = tuple(
            _Window(kind=kind, samples=tuple(buffer), at=at)
            for kind, buffer in self._buffers.items()
        )
        readings = await self._offload(lambda: self._process_all(windows))
        for reading in readings:
            self._latest[reading.kind] = reading
        return readings

    def _process_all(self, windows: Sequence[_Window]) -> tuple[SensorReading, ...]:
        return tuple(self._process_one(window) for window in windows)

    def _process_one(self, window: _Window) -> SensorReading:
        processor = self._processors[window.kind]
        try:
            return processor.process(window.samples, self._fs, window.at)
        except Exception:  # see the module docstring: one sensor's bug stays that sensor's
            _logger.exception("processor %s raised; reading marked no_signal", window.kind.value)
            return SensorReading(
                kind=window.kind,
                at=window.at,
                display_rate=processor.spec.display_rate,
                waveform=(),
                quality=SignalQuality.NO_SIGNAL,
                detail="erreur de traitement (voir le journal)",
                metrics=(),
            )
