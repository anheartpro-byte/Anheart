import json
import tracemalloc
from dataclasses import replace
from math import inf, nan
from pathlib import Path

import pytest

from src.acquisition_provenance import (
    MAX_BATCH_SAMPLES,
    MAX_QUEUED_PROVENANCE,
    MAX_SPANS,
    ActualSpan,
    LinkStats,
    Provenance,
    ProvenanceBudget,
    SampleIndex,
    SegmentId,
    joined,
)
from src.bitalino_client import ChannelData, FrameDecoder, SampleBatch
from src.clock import ManualClock
from src.record import acquisition
from src.record.acquisition import AcquisitionProblem, RawCapture, Unavailable, validated
from src.record.budget import MAX_SEQUENCE
from src.result import Err, Ok
from src.units import Hertz, Monotonic
from tests.record_acquisition_support import (
    ExhaustibleCapture,
    Sink,
    StreamingSink,
    batch,
    client,
    encoded,
    metadata_bytes,
)


def facts() -> Provenance:
    value = batch().provenance
    assert value is not None
    return value


@pytest.mark.parametrize("value", [nan, inf, -inf, -1, 1024, 1.25])
def test_invalid_adc_is_rejected_without_rounding(value: float) -> None:
    sink = Sink()
    assert RawCapture(Monotonic(0), sink).capture(batch((value,))) == Err(
        AcquisitionProblem(Unavailable.ADC)
    )
    assert sink.observations == []


@pytest.mark.parametrize("name", ["EMG", "LUX"])
def test_six_bit_adc_range_is_preserved(name: str) -> None:
    original = batch((63.0, 64.0))
    assert validated(replace(original, channels=(ChannelData(name, (63.0, 64.0)),))) == Err(
        AcquisitionProblem(Unavailable.ADC)
    )
    assert isinstance(validated(replace(original, channels=(ChannelData(name, (0.0, 63.0)),))), Ok)


@pytest.mark.parametrize(
    "provenance",
    [
        replace(facts(), count=0),
        replace(facts(), count=MAX_BATCH_SAMPLES + 1),
        replace(facts(), spans=()),
        replace(facts(), spans=(ActualSpan(0, 1),) * (MAX_SPANS + 1)),
        replace(facts(), first_index=SampleIndex(-1)),
        replace(facts(), first_index=SampleIndex(MAX_SEQUENCE)),
        replace(facts(), segment=SegmentId(-1)),
        replace(facts(), segment=SegmentId(MAX_SEQUENCE + 1)),
        replace(facts(), first_at=Monotonic(nan)),
        replace(facts(), first_at=Monotonic(inf)),
        replace(facts(), spans=(ActualSpan(-1, 1),)),
        replace(facts(), spans=(ActualSpan(0, 0),)),
        replace(facts(), spans=(ActualSpan(0, 3),)),
        replace(facts(), spans=(ActualSpan(0, 2), ActualSpan(1, 1))),
    ],
)
def test_invalid_provenance_bounds(provenance: Provenance) -> None:
    assert validated(replace(batch(), provenance=provenance)) == Err(
        AcquisitionProblem(Unavailable.BOUNDS)
    )


@pytest.mark.parametrize("rate", [1, 10, 100])
def test_other_acquisition_rates_remain_explicitly_unsupported_by_raw_schema(rate: int) -> None:
    assert validated(replace(batch(), provenance=replace(facts(), sample_rate=Hertz(rate)))) == Err(
        AcquisitionProblem(Unavailable.RATE)
    )


@pytest.mark.parametrize(
    "channels",
    [
        (),
        (ChannelData("other", (1, 2)),),
        (ChannelData("ECG", (1, 2)),) * 2,
        (ChannelData("ECG", (1, 2)),) * 7,
    ],
)
def test_invalid_channel_metadata(channels: tuple[ChannelData, ...]) -> None:
    assert validated(replace(batch(), channels=channels)) == Err(
        AcquisitionProblem(Unavailable.CHANNELS)
    )


def test_invalid_count_and_nonfinite_origin_and_sequence_exhaustion() -> None:
    assert validated(replace(batch(), channels=(ChannelData("ECG", (1,)),))) == Err(
        AcquisitionProblem(Unavailable.BOUNDS)
    )
    sink = Sink()
    capture = RawCapture(Monotonic(inf), sink)
    assert capture.capture(batch()) == Err(AcquisitionProblem(Unavailable.BOUNDS))
    extreme = replace(batch(), provenance=replace(facts(), first_at=Monotonic(1e308)))
    assert RawCapture(Monotonic(-1e308), sink).capture(extreme) == Err(
        AcquisitionProblem(Unavailable.BOUNDS)
    )
    exhausted = ExhaustibleCapture(Monotonic(0), sink)
    exhausted.exhaust()
    assert exhausted.capture(batch()) == Err(AcquisitionProblem(Unavailable.BOUNDS))
    assert sink.observations == []


def test_duplicate_source_or_regressing_loss_snapshot_is_rejected() -> None:
    sink = Sink()
    capture = RawCapture(Monotonic(0), sink)
    assert isinstance(capture.capture(batch()), Ok)
    assert capture.capture(batch()) == Err(AcquisitionProblem(Unavailable.ORDER))
    reversed_loss = replace(facts(), first_index=SampleIndex(2), loss=LinkStats())
    assert capture.capture(replace(batch(), provenance=reversed_loss)) == Err(
        AcquisitionProblem(Unavailable.ORDER)
    )
    assert len(sink.observations) == 1


def test_raw_copy_is_immutable_after_source_values_change() -> None:
    values = [301.0, 302.0]
    sink = Sink()
    assert isinstance(RawCapture(Monotonic(0), sink).capture(batch(values)), Ok)
    values[0] = 999.0
    assert sink.observations[0].block.samples == ((301, 302),)


def test_exhaustive_result_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid(_batch: SampleBatch) -> None:
        return None

    monkeypatch.setattr(acquisition, "validated", invalid)
    with pytest.raises(AssertionError):
        RawCapture(Monotonic(0), Sink()).capture(batch())


def test_joined_metadata_refuses_unbounded_or_unknown_parts() -> None:
    assert joined((), 0) is None
    assert joined((None,), 0) is None
    assert joined((replace(facts(), count=MAX_BATCH_SAMPLES + 1),), 0) is None
    many = replace(
        facts(), count=MAX_SPANS * 2, spans=tuple(ActualSpan(i * 2, 1) for i in range(MAX_SPANS))
    )
    assert joined((many, many), 0) is None


def test_queue_metadata_budget_has_independent_item_and_span_caps() -> None:
    budget = ProvenanceBudget()
    assert budget.retain(None) is None
    for _ in range(MAX_QUEUED_PROVENANCE):
        assert budget.retain(facts()) is not None
    assert budget.retain(facts()) is None
    budget.release(facts())
    assert budget.retain(facts()) is not None
    budget.release(None)
    spans = replace(facts(), spans=(ActualSpan(0, 1),) * MAX_SPANS)
    budget = ProvenanceBudget()
    assert budget.retain(spans) is not None
    assert budget.retain(facts()) is None


async def test_queue_overflow_preserves_dsp_and_next_batch_provenance() -> None:
    source = client(ManualClock(start=Monotonic(10)))
    for i in range(MAX_QUEUED_PROVENANCE + 1):
        source.ingest(encoded((i * 2) % 16, (301,)) + encoded((i * 2 + 1) % 16, (302,)))
    assert source.metadata_items == MAX_QUEUED_PROVENANCE
    consumed = await source.read_samples(1)
    assert consumed is not None
    assert consumed.provenance is None
    assert tuple(consumed.channels[0].values) == (301, 302) * (MAX_QUEUED_PROVENANCE + 1)
    source.ingest(encoded(2, (401,)) + encoded(3, (402,)))
    following = await source.read_samples(1)
    assert following is not None
    assert following.provenance is not None
    assert source.metadata_items == 0


async def test_decoder_metadata_overflow_does_not_change_filled_rows() -> None:
    decoder = FrameDecoder(1)
    data = (
        encoded(0, (301,))
        + encoded(1, (302,))
        + b"".join(encoded((3 + 2 * i) % 16, (400,)) for i in range(MAX_SPANS + 1))
    )
    rows = decoder.feed(data)
    assert rows[:4] == [(301,), (302,), (302,), (400,)]
    assert len(rows) == 2 + 2 * (MAX_SPANS + 1)
    assert decoder.actual_spans is None
    assert decoder.filled_samples == MAX_SPANS + 1
    source = client(ManualClock(start=Monotonic(20)))
    source.ingest(data)
    consumed = await source.read_samples(1)
    assert consumed is not None
    assert consumed.provenance is None
    assert tuple(consumed.channels[0].values) == tuple(row[0] for row in rows)


def test_maximum_batch_streams_one_bounded_conversion_at_a_time(tmp_path: Path) -> None:
    samples = tuple(float(300 + i % 700) for i in range(MAX_BATCH_SAMPLES))
    small = tuple(float(i % 64) for i in range(MAX_BATCH_SAMPLES))
    source = batch(samples)
    source = replace(
        source,
        channels=tuple(
            ChannelData(name, small if name in {"EMG", "LUX"} else samples)
            for name in ("ECG", "EDA", "SpO2", "RESP", "EMG", "LUX")
        ),
    )
    sink = StreamingSink()
    capture = RawCapture(Monotonic(0), sink)
    tracemalloc.start()
    try:
        result = capture.capture(source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert isinstance(result, Ok)
    assert sink.totals[:2] == [66, MAX_BATCH_SAMPLES]
    assert peak < 2 * 1024 * 1024
    assert sink.totals[2] < 512 * 1024
    (tmp_path / "conversion-allocation.json").write_text(
        json.dumps(
            {
                "peak_bytes": peak,
                "max_task2_message_charge": sink.totals[2],
                "blocks": sink.totals[0],
            }
        )
    )


def test_queue_detached_batch_decoder_and_join_metadata_allowance(tmp_path: Path) -> None:
    values = tuple(
        replace(
            facts(),
            first_index=SampleIndex(100_000 + i),
            count=2048,
            spans=tuple(ActualSpan(int(str(300 + k * 300)), int(str(257))) for k in range(4)),
            loss=LinkStats(frames=int(str(100_000 + i))),
        )
        for i in range(MAX_QUEUED_PROVENANCE)
    )
    budget = ProvenanceBudget()
    assert len({id(span.count) for value in values for span in value.spans}) == MAX_SPANS
    assert all(budget.retain(value) is not None for value in values)
    assert budget.spans == MAX_SPANS
    queue = sum(metadata_bytes(value) for value in values)
    spans = replace(facts(), spans=tuple(ActualSpan(i * 2, 1) for i in range(MAX_SPANS)))
    span_stage = metadata_bytes(spans)
    added_chunk_references = 60_000 * 8
    aggregate = 2 * queue + 3 * span_stage + added_chunk_references + 2 * 1024 * 1024
    assert aggregate * 2 < 16 * 1024 * 1024
    (tmp_path / "metadata-allocation.json").write_text(
        json.dumps(
            {
                "one_queue": queue,
                "one_span_stage": span_stage,
                "default_queue_extra_references": added_chunk_references,
                "conversion_allowance": 2 * 1024 * 1024,
                "aggregate_with_2x_allocator_margin": aggregate * 2,
                "incremental_task3_allowance": 16 * 1024 * 1024,
            }
        )
    )
