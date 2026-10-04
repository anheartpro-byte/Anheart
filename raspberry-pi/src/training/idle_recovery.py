from dataclasses import dataclass

from src.units import Seconds


@dataclass(frozen=True, slots=True)
class IdleLink:
    """What the idle, read-only polling of the drive has seen. For the operator screen.

    Kept apart from the session's own failure counters on purpose: an idle
    read that fails is "the drive is not answering", shown as unknown, and
    must never feed ``comms_lost`` - that rule's answer is ``GO_SILENT``, which
    is one-way, and a console that went permanently silent because a cable was
    unplugged while nothing was commanded would have to be restarted to show
    anything at all.
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
