"""Time, injected rather than read.

This is the **only** module in ``src/`` allowed to call ``time.monotonic()`` or
``time.time()``. Everything else takes ``now`` as a parameter or holds a
``Clock``. A grep-based test enforces that.

The reason is not tidiness. A 45-minute training session has to be testable in
under a second, because the closed-loop simulation is the only evidence the
control law and the safety supervisor behave correctly before any hardware
exists, let alone before a person sits in the machine. Timing that reads a
global clock cannot be tested that way.

Three implementations:

* :class:`RealClock` in production.
* :class:`ManualClock` in unit tests: nothing moves unless the test advances it,
  so every dwell time and hysteresis window is exact and there are no flakes.
* :class:`SimClock` for the accelerated end-to-end run, where wall time still
  passes but faster, so async code that genuinely awaits still works.

See .claude/skills/anheart-strict-python/SKILL.md rule 4.
"""

from __future__ import annotations

import time
from typing import Protocol, final, runtime_checkable

from src.units import Monotonic, Seconds, UnixMillis

MILLIS_PER_SECOND: int = 1000


@runtime_checkable
class Clock(Protocol):
    """A source of time.

    ``monotonic`` is for durations and scheduling; it never goes backwards and
    is unaffected by a clock step. ``unix_millis`` is for stamping records that
    leave the machine. They are different ``NewType``s so one cannot be used
    where the other belongs: a Pi with no RTC boots with a wrong wall clock but
    a perfectly good monotonic one, and elapsed-time logic must not care.
    """

    def monotonic(self) -> Monotonic:
        """Seconds from an arbitrary origin, monotonically non-decreasing."""
        ...

    def unix_millis(self) -> UnixMillis:
        """Wall-clock milliseconds since the Unix epoch."""
        ...


@final
class RealClock:
    """The system clock. Used in production."""

    __slots__ = ()

    def monotonic(self) -> Monotonic:
        return Monotonic(time.monotonic())

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(int(time.time() * MILLIS_PER_SECOND))


@final
class ManualClock:
    """A clock that moves only when a test moves it.

    Deterministic by construction: a dwell of "10 s" in a safety rule is tested
    by advancing exactly 10 s, not by sleeping and hoping. That removes both
    flakiness and multi-second test runtimes.
    """

    __slots__ = ("_epoch_millis", "_now")

    def __init__(
        self,
        start: Monotonic = Monotonic(0.0),
        epoch_millis: UnixMillis = UnixMillis(0),
    ) -> None:
        self._now: Monotonic = start
        self._epoch_millis: UnixMillis = epoch_millis

    def monotonic(self) -> Monotonic:
        return self._now

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(self._epoch_millis + int(self._now * MILLIS_PER_SECOND))

    def advance(self, delta: Seconds) -> Monotonic:
        """Move time forward. Rejects going backwards, which a monotonic clock cannot do."""
        if delta < 0.0:
            raise ValueError(f"cannot advance a monotonic clock by {delta}")
        self._now = Monotonic(self._now + delta)
        return self._now


@final
class SimClock:
    """Real time, scaled, so an accelerated session still exercises async code.

    ``SimClock(speed=60)`` makes a 45-minute programme finish in 45 seconds
    while ``await`` and I/O still behave normally. Used by
    ``scripts/simulate_session.py``; unit tests should prefer
    :class:`ManualClock`.
    """

    __slots__ = ("_epoch_millis", "_origin", "_real_origin", "_speed")

    def __init__(
        self,
        speed: float = 1.0,
        origin: Monotonic = Monotonic(0.0),
        epoch_millis: UnixMillis = UnixMillis(0),
    ) -> None:
        if speed <= 0.0:
            raise ValueError(f"speed must be positive, got {speed}")
        self._speed: float = speed
        self._origin: Monotonic = origin
        self._epoch_millis: UnixMillis = epoch_millis
        self._real_origin: float = time.monotonic()

    @property
    def speed(self) -> float:
        return self._speed

    def monotonic(self) -> Monotonic:
        real_elapsed = time.monotonic() - self._real_origin
        return Monotonic(self._origin + real_elapsed * self._speed)

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(self._epoch_millis + int(self.monotonic() * MILLIS_PER_SECOND))

    def real_seconds_for(self, simulated: Seconds) -> Seconds:
        """How long a simulated duration actually takes, for sleeps in scripts."""
        return Seconds(simulated / self._speed)
