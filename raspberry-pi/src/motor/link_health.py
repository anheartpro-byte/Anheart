"""Native transaction evidence and failure accounting, independent of transport."""

import threading

from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import CommTimeout
from src.units import Monotonic, elapsed


class LinkHealth:
    """Mutable failure-run state with a separately latched loss timestamp.

    Mutation is deliberate: successful transactions clear only the failure
    run, retaining a latch written independently by the emergency thread.
    Replacing the whole state after a transaction could discard that latch.
    """

    __slots__ = (
        "_address_proven",
        "_consecutive_failures",
        "_evidence_lock",
        "_failed_since",
        "_failure_threshold",
        "_latch_generation",
        "_lost_since",
        "_possible_frames",
    )

    def __init__(self, failure_threshold: int) -> None:
        self._failure_threshold: int = failure_threshold
        # `_failed_since` is the first failure of the current run (None while
        # healthy); `_lost_since` is set when a run reaches the threshold and
        # is cleared ONLY by successful open(), because a link that healed on
        # its own is not permission to command a motor again.
        self._consecutive_failures: int = 0
        self._failed_since: Monotonic | None = None
        self._lost_since: Monotonic | None = None
        self._evidence_lock: threading.Lock = threading.Lock()
        self._possible_frames: int = 0
        self._address_proven: bool = False
        self._latch_generation: int = 0

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        with self._evidence_lock:
            return AcquisitionEvidence(self._possible_frames, self._address_proven)

    @property
    def acquisition_generation(self) -> int:
        """Remember the latch generation before starting an acquisition."""
        with self._evidence_lock:
            return self._latch_generation

    def note_request(self) -> None:
        """Count possible dispatch without waiting for a transport acknowledgement."""
        with self._evidence_lock:
            self._possible_frames += 1

    def confirm_acquisition(self, generation: int) -> None:
        """Accept ETA proof without clearing a stop latched during acquisition."""
        with self._evidence_lock:
            self._address_proven = True
            if self._latch_generation == generation:
                self._lost_since = None

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

    def latch(self, now: Monotonic) -> None:
        """Latch after close or an emergency stop, retaining the failure run."""
        with self._evidence_lock:
            self._latch_generation += 1
            self._lost_since = now
