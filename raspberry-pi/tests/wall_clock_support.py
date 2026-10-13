"""A wall clock whose standing the test decides: the fake of ``src.wall_clock.WallClockProbe``.

No test asks the real kernel what the machine's clock is worth, except the one
that is about that call (``test_wall_clock``): a test must give the same answer
on a laptop without a network and in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from src.units import Seconds
from src.wall_clock import Synchronised, Unsynchronised, WallClockState

IN_SYNC: Final[Synchronised] = Synchronised(
    estimated_error=Seconds(0.0004), max_error=Seconds(0.045)
)
"""A clock a time service keeps: estimated 0.4 ms off, 45 ms at most."""

NO_NETWORK: Final[Unsynchronised] = Unsynchronised()
"""The Pi started without a network: nobody answers for its clock."""


@dataclass
class ScriptedClock:
    """Answers ``state`` until the test changes it; raises ``fault`` when one is set."""

    state: WallClockState = NO_NETWORK
    fault: Exception | None = None
    reads: int = 0

    def read(self) -> WallClockState:
        self.reads += 1
        if self.fault is not None:
            raise self.fault
        return self.state
