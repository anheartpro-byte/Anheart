"""Bounded acquisition facts, independent of recording and DSP consumers."""

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, NewType

from src.units import Hertz, Monotonic

SampleIndex = NewType("SampleIndex", int)
SegmentId = NewType("SegmentId", int)
MAX_SPANS: Final = 4096
MAX_BATCH_SAMPLES: Final = 65_536
MAX_QUEUED_PROVENANCE: Final = 1024


class Origin(StrEnum):
    SOFTWARE_NOMINAL = "decoded_software_nominal"
    SYNTHETIC = "synthetic_complete"


@dataclass(frozen=True, slots=True)
class LinkStats:
    """Cumulative acquisition counts; filled_samples is a modulo-16 loss lower bound."""

    frames: int = 0
    skipped_bytes: int = 0
    sync_losses: int = 0
    filled_samples: int = 0
    dropped_backlog_samples: int = 0
    reconnects: int = 0


@dataclass(frozen=True, slots=True)
class ActualSpan:
    offset: int
    count: int


@dataclass(frozen=True, slots=True)
class Provenance:
    """Offsets address the unchanged DSP rows; indexes include inferred held slots.

    Software time is nominal-rate timing, not a device hardware timestamp.
    """

    origin: Origin
    segment: SegmentId
    first_index: SampleIndex
    first_at: Monotonic
    sample_rate: Hertz
    count: int
    spans: tuple[ActualSpan, ...]
    loss: LinkStats


def append_span(spans: list[ActualSpan], span: ActualSpan) -> bool:
    """Coalesce adjacent actual rows, refusing metadata growth beyond the cap."""
    if spans and spans[-1].offset + spans[-1].count == span.offset:
        previous = spans[-1]
        spans[-1] = ActualSpan(previous.offset, previous.count + span.count)
    elif len(spans) < MAX_SPANS:
        spans.append(span)
    else:
        return False
    return True


def joined(parts: Iterable[Provenance | None], dropped: int) -> Provenance | None:
    """Merge consumed chunks only, plus backlog loss observed under the queue lock."""
    first: Provenance | None = None
    last: Provenance | None = None
    spans: list[ActualSpan] = []
    count = 0
    for part in parts:
        if part is None or count + part.count > MAX_BATCH_SAMPLES:
            return None
        if first is None:
            first = part
        for span in part.spans:
            if not append_span(spans, ActualSpan(count + span.offset, span.count)):
                return None
        count += part.count
        last = part
    if first is None or last is None:
        return None
    return replace(
        first,
        count=count,
        spans=tuple(spans),
        loss=replace(last.loss, dropped_backlog_samples=dropped),
    )


def loss_delta(current: LinkStats, previous: LinkStats) -> LinkStats:
    return LinkStats(
        frames=current.frames - previous.frames,
        skipped_bytes=current.skipped_bytes - previous.skipped_bytes,
        sync_losses=current.sync_losses - previous.sync_losses,
        filled_samples=current.filled_samples - previous.filled_samples,
        dropped_backlog_samples=current.dropped_backlog_samples - previous.dropped_backlog_samples,
        reconnects=current.reconnects - previous.reconnects,
    )


class ProvenanceBudget:
    """Queue-lock-owned mutable accounting, independent of the existing DSP row budget."""

    __slots__ = ("items", "spans")

    def __init__(self) -> None:
        self.items = 0
        self.spans = 0

    def retain(self, value: Provenance | None) -> Provenance | None:
        if (
            value is None
            or self.items >= MAX_QUEUED_PROVENANCE
            or self.spans + len(value.spans) > MAX_SPANS
        ):
            return None
        self.items += 1
        self.spans += len(value.spans)
        return value

    def release(self, value: Provenance | None) -> None:
        if value is not None:
            self.items -= 1
            self.spans -= len(value.spans)
