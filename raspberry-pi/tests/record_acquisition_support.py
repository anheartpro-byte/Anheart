from collections.abc import Sequence
from dataclasses import dataclass, field
from sys import getsizeof

from src.acquisition_provenance import (
    ActualSpan,
    LinkStats,
    Origin,
    Provenance,
    SampleIndex,
    SegmentId,
)
from src.bitalino_client import BITalinoClient, ChannelData, Device, SampleBatch
from src.clock import ManualClock
from src.record.acquisition import RawCapture, RawObservation
from src.record.budget import MAX_SEQUENCE, retained_charge
from src.record.messages import Message, SequenceNumber
from src.units import Hertz, Monotonic, UnixMillis


def encoded(seq: int, values: Sequence[int]) -> bytes:
    bits = 12 + 10 * min(4, len(values)) + 6 * max(0, len(values) - 4)
    size = (bits + 7) // 8
    packed = seq << (size * 8 - 4)
    position = size * 8 - 12
    for index, value in enumerate(values):
        width = 10 if index < 4 else 6
        position -= width
        packed |= value << position
    data = packed.to_bytes(size, "little")
    remainder = 0
    for byte in data:
        for bit in range(7, -1, -1):
            remainder = (remainder << 1) ^ ((byte >> bit) & 1)
            if remainder & 16:
                remainder ^= 0x13
    return data[:-1] + bytes((data[-1] | remainder,))


class IngestClient(BITalinoClient):
    def ingest(self, data: bytes) -> None:
        self._ingest(data)

    def reconnect_link(self, lost: Device) -> Device | None:
        return self._reconnect(lost)

    @property
    def metadata_items(self) -> int:
        return self._provenance_budget.items


class ExhaustibleCapture(RawCapture):
    def exhaust(self) -> None:
        self._sequence = MAX_SEQUENCE


def client(clock: ManualClock, channels: Sequence[int] = (0,)) -> IngestClient:
    return IngestClient("unused", channels=channels, auto_pair=False, clock=clock)


def batch(values: Sequence[float] = (101.0, 202.0)) -> SampleBatch:
    return SampleBatch(
        UnixMillis(999_000),
        (ChannelData("ECG", values),),
        Provenance(
            Origin.SYNTHETIC,
            SegmentId(1),
            SampleIndex(0),
            Monotonic(9),
            Hertz(1000),
            len(values),
            (ActualSpan(0, len(values)),),
            LinkStats(frames=len(values)),
        ),
    )


@dataclass(frozen=True, slots=True)
class Sink:
    observations: list[RawObservation] = field(default_factory=list)
    reject: frozenset[int] = frozenset()

    def __call__(self, observation: RawObservation) -> bool:
        if observation.block.header.seq in self.reject:
            return False
        self.observations.append(observation)
        return True


def metadata_bytes(value: Provenance) -> int:
    scalar_fields = (
        value.segment,
        value.first_index,
        value.first_at,
        value.sample_rate,
        value.count,
        value.loss.frames,
        value.loss.skipped_bytes,
        value.loss.sync_losses,
        value.loss.filled_samples,
        value.loss.dropped_backlog_samples,
        value.loss.reconnects,
    )
    return (
        getsizeof(value)
        + getsizeof(value.spans)
        + getsizeof(value.loss)
        + sum(getsizeof(item) for item in scalar_fields)
        + sum(
            getsizeof(span) + getsizeof(span.offset) + getsizeof(span.count) for span in value.spans
        )
    )


@dataclass(frozen=True, slots=True)
class StreamingSink:
    totals: list[int] = field(default_factory=lambda: [0, 0, 0])

    def __call__(self, observation: RawObservation) -> bool:
        self.totals[0] += 1
        self.totals[1] += observation.block.header.n_samples
        self.totals[2] = max(
            self.totals[2],
            retained_charge(
                Message(
                    SequenceNumber(observation.block.header.seq), observation.at, observation.block
                )
            ),
        )
        return True
