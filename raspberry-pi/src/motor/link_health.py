"""Native transaction failure accounting, independent of transport and logging."""

from src.motor.drive import CommTimeout
from src.units import Monotonic, elapsed


class LinkHealth:
    """Mutable failure-run state with a separately latched loss timestamp.

    Mutation is deliberate: successful transactions clear only the failure
    run, retaining a latch written independently by the emergency thread.
    Replacing the whole state after a transaction could discard that latch.
    """

    __slots__ = ("_consecutive_failures", "_failed_since", "_failure_threshold", "_lost_since")

    def __init__(self, failure_threshold: int) -> None:
        self._failure_threshold: int = failure_threshold
        # `_failed_since` is the first failure of the current run (None while
        # healthy); `_lost_since` is set when a run reaches the threshold and
        # is cleared ONLY by open(), because a link that healed on its own is
        # not permission to command a motor again.
        self._consecutive_failures: int = 0
        self._failed_since: Monotonic | None = None
        self._lost_since: Monotonic | None = None

    @property
    def failure_threshold(self) -> int:
        return self._failure_threshold

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def lost_since(self) -> Monotonic | None:
        return self._lost_since

    def note_failure(self, now: Monotonic) -> CommTimeout | None:
        """Latch at the threshold; below it, retain the caller's diagnosis.

        The timeout measures from the first observed failure of the run,
        rather than the configured transaction budget or the latest attempt.
        """
        self._consecutive_failures += 1
        first = self._failed_since if self._failed_since is not None else now
        self._failed_since = first
        if self._consecutive_failures < self._failure_threshold:
            return None
        self._lost_since = first
        return CommTimeout(after=elapsed(first, now))

    def note_success(self) -> None:
        """A completed transaction ends the current failure run.

        It does NOT clear ``_lost_since``. A link that came back on its own is
        not permission to resume commanding a motor; only ``open()`` is.
        """
        self._consecutive_failures = 0
        self._failed_since = None

    def reset(self) -> None:
        """Clear the failure run and latch for an explicit open attempt."""
        self._consecutive_failures = 0
        self._failed_since = None
        self._lost_since = None

    def latch(self, now: Monotonic) -> None:
        """Latch after close or an emergency stop, retaining the failure run."""
        self._lost_since = now
