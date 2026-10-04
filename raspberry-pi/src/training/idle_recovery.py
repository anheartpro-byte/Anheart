from __future__ import annotations

from dataclasses import dataclass

from src.units import Seconds


@dataclass(frozen=True, slots=True)
class IdleLink:
    """What the idle, read-only polling of the drive has seen. For the operator screen.

    These diagnostic counters remain separate from session failures. A total
    no-frame outage retries indefinitely. UnknownEpisode separately bounds
    unresolved inspections that may have refreshed the drive's watchdog.
    """

    open: bool = False
    """Whether the link is believed open for polling. A failure re-opens it."""

    reads: int = 0
    """Successful idle reads since this runtime was built."""

    failures: int = 0
    """Failed idle exchanges (open or read) since this runtime was built."""

    consecutive_failures: int = 0
    """The current run of failures; 0 after any success."""

    last_latency: Seconds | None = None
    """How long the last successful ``read_status`` took, by the injected clock."""

    last_error: str | None = None
    """The operator-facing description of the last failure, or ``None``."""


@dataclass(frozen=True, slots=True)
class UnknownEpisode:
    failures: int = 0

    def failed(self, *, possible_frames: bool) -> UnknownEpisode:
        if self.failures == 0 and not possible_frames:
            return self
        return UnknownEpisode(self.failures + 1)
