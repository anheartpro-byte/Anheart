"""Synchronous bounded raw projection; no storage, compression or DSP decisions."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from math import isfinite
from typing import assert_never

from src.acquisition_provenance import (
    MAX_BATCH_SAMPLES,
    MAX_SPANS,
    LinkStats,
    Origin,
    Provenance,
    SampleIndex,
    SegmentId,
    loss_delta,
)
from src.bitalino_client import SampleBatch
from src.record.budget import MAX_CHANNELS, MAX_SAMPLES, MAX_SEQUENCE
from src.record.ecg import SAMPLE_RATE, Header, RawBlock
from src.record.messages import Loss, SequenceNumber
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind
from src.units import Hertz, Monotonic


class Unavailable(StrEnum):
    UNKNOWN = "raw_provenance_unknown"
    BOUNDS = "raw_provenance_bounds"
    RATE = "raw_rate_unsupported"
    CHANNELS = "raw_channels_invalid"
    ADC = "raw_adc_invalid"
    ORDER = "raw_source_order_invalid"


@dataclass(frozen=True, slots=True)
class AcquisitionProblem:
    code: Unavailable


@dataclass(frozen=True, slots=True)
class RawObservation:
    block: RawBlock
    segment: SegmentId
    first_index: SampleIndex
    at: Monotonic
    origin: Origin


@dataclass(frozen=True, slots=True)
class CaptureReport:
    accepted: int
    source_delta: LinkStats
    recorder_loss: Loss


def validated(batch: SampleBatch) -> Result[Provenance, AcquisitionProblem]:
    provenance = batch.provenance
    if provenance is None:
        return Err(AcquisitionProblem(Unavailable.UNKNOWN))
    if provenance.sample_rate != Hertz(SAMPLE_RATE):
        return Err(AcquisitionProblem(Unavailable.RATE))
    if (
        not 0 < provenance.count <= MAX_BATCH_SAMPLES
        or not 0 < len(provenance.spans) <= MAX_SPANS
        or not 0 <= provenance.first_index <= MAX_SEQUENCE - provenance.count
        or not 0 <= provenance.segment <= MAX_SEQUENCE
        or not isfinite(provenance.first_at)
    ):
        return Err(AcquisitionProblem(Unavailable.BOUNDS))
    end = 0
    for span in provenance.spans:
        if span.offset < end or span.count <= 0 or span.offset + span.count > provenance.count:
            return Err(AcquisitionProblem(Unavailable.BOUNDS))
        end = span.offset + span.count
    return _validated_values(batch, provenance)


def _validated_values(
    batch: SampleBatch, provenance: Provenance
) -> Result[Provenance, AcquisitionProblem]:
    if not 0 < len(batch.channels) <= MAX_CHANNELS:
        return Err(AcquisitionProblem(Unavailable.CHANNELS))
    names = tuple(channel.channel for channel in batch.channels)
    if len(set(names)) != len(names):
        return Err(AcquisitionProblem(Unavailable.CHANNELS))
    for channel in batch.channels:
        try:
            kind = SensorKind(channel.channel)
        except ValueError:
            return Err(AcquisitionProblem(Unavailable.CHANNELS))
        if len(channel.values) != provenance.count:
            return Err(AcquisitionProblem(Unavailable.BOUNDS))
        for span in provenance.spans:
            for index in range(span.offset, span.offset + span.count):
                value = channel.values[index]
                if not isfinite(value) or not 0 <= value <= kind.adc_max or value != int(value):
                    return Err(AcquisitionProblem(Unavailable.ADC))
    return Ok(provenance)


class RawCapture:
    """One consumer owns mutable ordinals/loss counters; sink must be bounded and nonblocking.

    The sink owns accepted immutable observations. False drops newest; ordinals
    are allocated first, so rejection cannot make the subsequent stream look complete.
    """

    __slots__ = ("_loss", "_origin", "_sequence", "_sink", "_source_end", "_source_loss")

    def __init__(self, origin: Monotonic, sink: Callable[[RawObservation], bool]) -> None:
        self._origin = origin
        self._sink = sink
        self._sequence = 0
        self._source_end = SampleIndex(0)
        self._source_loss = LinkStats()
        self._loss = Loss()

    @property
    def recorder_loss(self) -> Loss:
        return self._loss

    def capture(self, batch: SampleBatch) -> Result[CaptureReport, AcquisitionProblem]:
        match validated(batch):
            case Err() as error:
                return error
            case Ok(provenance):
                return self._capture(batch, provenance)
            case _ as unreachable:
                assert_never(unreachable)

    def _capture(
        self, batch: SampleBatch, provenance: Provenance
    ) -> Result[CaptureReport, AcquisitionProblem]:
        delta = loss_delta(provenance.loss, self._source_loss)
        blocks = sum((span.count + MAX_SAMPLES - 1) // MAX_SAMPLES for span in provenance.spans)
        if (
            not isfinite(provenance.first_at - self._origin)
            or self._sequence > MAX_SEQUENCE - blocks
        ):
            return Err(AcquisitionProblem(Unavailable.BOUNDS))
        if (
            provenance.first_index < self._source_end
            or min(
                delta.frames,
                delta.skipped_bytes,
                delta.sync_losses,
                delta.filled_samples,
                delta.dropped_backlog_samples,
                delta.reconnects,
            )
            < 0
        ):
            return Err(AcquisitionProblem(Unavailable.ORDER))
        self._source_end = SampleIndex(provenance.first_index + provenance.count)
        self._source_loss = provenance.loss
        accepted = 0
        names = tuple(channel.channel for channel in batch.channels)
        for span in provenance.spans:
            for offset in range(span.offset, span.offset + span.count, MAX_SAMPLES):
                count = min(MAX_SAMPLES, span.offset + span.count - offset)
                sequence = SequenceNumber(self._sequence)
                self._sequence += 1
                at = Monotonic(provenance.first_at + offset / SAMPLE_RATE)
                block = RawBlock(
                    Header(sequence, at - self._origin, count, names),
                    tuple(
                        tuple(int(channel.values[i]) for i in range(offset, offset + count))
                        for channel in batch.channels
                    ),
                )
                if self._sink(
                    RawObservation(
                        block,
                        provenance.segment,
                        SampleIndex(provenance.first_index + offset),
                        at,
                        provenance.origin,
                    )
                ):
                    accepted += 1
                else:
                    self._loss = replace(
                        self._loss,
                        count=self._loss.count + 1,
                        first_sequence=sequence
                        if self._loss.count == 0
                        else self._loss.first_sequence,
                        last_sequence=sequence,
                        first_at=at if self._loss.count == 0 else self._loss.first_at,
                        last_at=Monotonic(at + (count - 1) / SAMPLE_RATE),
                    )
        return Ok(CaptureReport(accepted, delta, self._loss))
