"""An event loop whose time is a ManualClock: the real console, timed to the instant.

``LocalPanel.run`` paces its tasks with ``asyncio`` timers and reads its own
clock for the instants it decides on. Here both are the same ManualClock: the
loop's ``time()`` reads it, and when nothing is ready the selector advances it
straight to the next timer instead of sleeping. Every timer, every awaited
latency and every instant a test records is therefore exact and repeatable,
and a minute of console takes a fraction of a second.

Only for coroutines that do not wait on real threads: a thread's result would
arrive at a real instant the virtual clock knows nothing about.
"""

from __future__ import annotations

import asyncio
import selectors
from collections.abc import Coroutine
from typing import override

from src.clock import ManualClock
from src.units import Seconds


class VirtualSelector(selectors.SelectSelector):
    """Polls real descriptors without waiting, then jumps the clock to the next timer."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__()
        self._clock: ManualClock = clock

    @override
    # typeshed's SelectorKey carries ``data: Any``; nothing this module adds is Any.
    def select(  # type: ignore[explicit-any]
        self, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            raise RuntimeError("virtual time: nothing is scheduled, the loop would wait for ever")
        self._clock.advance(Seconds(timeout))
        return []


class VirtualLoop(asyncio.SelectorEventLoop):
    """A selector loop on virtual time."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__(VirtualSelector(clock))
        self._virtual: ManualClock = clock

    @override
    def time(self) -> float:
        return float(self._virtual.monotonic())


def run_virtual[T](clock: ManualClock, main: Coroutine[object, object, T]) -> T:
    """Run ``main`` to completion on a fresh virtual-time loop, then close it."""
    loop = VirtualLoop(clock)
    try:
        return loop.run_until_complete(main)
    finally:
        loop.close()
