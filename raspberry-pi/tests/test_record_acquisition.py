from dataclasses import replace
from pathlib import Path

import pytest

from src.acquisition_provenance import ActualSpan, Origin, SampleIndex, SegmentId
from src.bitalino_client import FrameDecoder, SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import EcgBridge
from src.record.acquisition import AcquisitionProblem, RawCapture, Unavailable
from src.record.reader import read
from src.record.writer import Writer
from src.result import Err, Ok
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology
from src.units import Monotonic, Seconds
from tests.record_acquisition_support import IngestClient, Sink, client, encoded
from tests.record_acquisition_support import batch as synthetic_batch
from tests.record_support import manifest
from tests.test_bitalino import Factory, FakeDevice
from tests.test_ecg_pipeline import (
    GOOD_70,
    FakeTreatment,
    RecordingRing,
    RecordingRuntime,
    ScriptedSource,
)
from tests.test_sim import SIM_ARM


async def test_gap_raw_reader_contains_no_held_samples(tmp_path: Path) -> None:
    clock = ManualClock(start=Monotonic(10))
    source = client(clock)
    frames = b"".join(
        encoded(seq, (value,)) for seq, value in ((1, 111), (2, 222), (5, 555), (6, 666))
    )
    source.ingest(frames)
    clock.advance(Seconds(30))

    batch = await source.read_samples(1)

    assert batch is not None
    assert tuple(batch.channels[0].values) == (111, 222, 222, 222, 555, 666)
    assert batch.provenance is not None
    assert batch.provenance.spans == (ActualSpan(0, 2), ActualSpan(4, 2))
    sink = Sink()
    result = RawCapture(Monotonic(9), sink).capture(batch)
    assert isinstance(result, Ok)
    assert result.value.source_delta.filled_samples == 2
    made = Writer.create(tmp_path / "record", manifest())
    assert isinstance(made, Ok)
    for entry in sink.observations:
        assert isinstance(made.value.raw(entry.block), Ok)
    assert isinstance(made.value.close(clock, "done"), Ok)
    recovered = read(made.value.path)
    assert isinstance(recovered, Ok)
    assert [block.samples for block in recovered.value.raw] == [((111, 222),), ((555, 666),)]
    assert [block.header.t_first for block in recovered.value.raw] == pytest.approx([0.994, 0.998])


async def test_contiguous_six_channel_decoding_preserves_every_column() -> None:
    clock = ManualClock(start=Monotonic(10))
    source = client(clock, (5, 3, 1, 0, 4, 2))
    first = (101, 202, 303, 404, 15, 26)
    second = (111, 222, 333, 444, 16, 27)
    data = encoded(14, first) + encoded(15, second)
    assert FrameDecoder(6).feed(data) == [first, second]
    source.ingest(data)

    batch = await source.read_samples(1)

    assert batch is not None
    sink = Sink()
    assert isinstance(RawCapture(Monotonic(0), sink).capture(batch), Ok)
    observed = sink.observations[0]
    assert observed.block.samples == tuple(zip(first, second, strict=True))
    assert observed.block.header.channels == ("ECG", "EDA", "SpO2", "RESP", "EMG", "LUX")
    assert observed.block.header.t_first == pytest.approx(9.998)
    assert observed.origin is Origin.SOFTWARE_NOMINAL
    assert await source.read_samples(1) is None


async def test_reconnect_keeps_source_index_and_frozen_segment_loss() -> None:
    clock = ManualClock(start=Monotonic(10))
    reopened = FakeDevice([])
    source = IngestClient(
        "unused",
        clock=clock,
        auto_pair=False,
        device_factory=Factory(reopened),
        reconnect_delays=(0,),
    )
    source.ingest(encoded(1, (101,)) + encoded(2, (202,)))
    clock.advance(Seconds(5))
    assert source.reconnect_link(FakeDevice([])) is reopened
    source.ingest(encoded(9, (303,)) + encoded(10, (404,)))

    earlier = await source.read_samples(100)
    later = await source.read_samples(1)

    assert earlier is not None
    assert later is not None
    assert earlier.provenance is not None
    assert later.provenance is not None
    assert earlier.provenance.loss.frames == 2
    assert earlier.provenance.loss.reconnects == 0
    assert later.provenance.loss.frames == 4
    assert later.provenance.loss.reconnects == 1
    assert (earlier.provenance.segment, later.provenance.segment) == (0, 1)
    assert (earlier.provenance.first_index, later.provenance.first_index) == (0, 2)
    sink = Sink()
    capture = RawCapture(Monotonic(9), sink)
    assert isinstance(capture.capture(earlier), Ok)
    result = capture.capture(later)
    assert isinstance(result, Ok)
    assert result.value.source_delta.reconnects == 1
    assert result.value.source_delta.frames == 2
    assert [entry.block.header.seq for entry in sink.observations] == [0, 1]
    assert [entry.at for entry in sink.observations] == pytest.approx([9.998, 14.998])


async def test_backlog_drop_is_separate_from_device_gap_and_raw_rejection() -> None:
    source = IngestClient(
        "unused", clock=ManualClock(start=Monotonic(10)), auto_pair=False, max_backlog_s=0.005
    )
    source.ingest(encoded(1, (101,)) + encoded(2, (202,)))
    source.ingest(encoded(5, (505,)) + encoded(6, (606,)))

    batch = await source.read_samples(1)

    assert batch is not None
    assert batch.provenance is not None
    assert tuple(batch.channels[0].values) == (202, 202, 505, 606)
    assert batch.provenance.first_index == 2
    assert batch.provenance.spans == (ActualSpan(2, 2),)
    sink = Sink(reject=frozenset({0}))
    capture = RawCapture(Monotonic(9), sink)
    result = capture.capture(batch)
    assert isinstance(result, Ok)
    assert result.value.source_delta.filled_samples == 2
    assert result.value.source_delta.dropped_backlog_samples == 2
    assert result.value.recorder_loss.count == 1
    assert result.value.recorder_loss.first_sequence == 0
    assert result.value.recorder_loss.last_at == pytest.approx(10.003)
    source.ingest(encoded(7, (707,)) + encoded(8, (808,)))
    next_batch = await source.read_samples(1)
    assert next_batch is not None
    recovered = capture.capture(next_batch)
    assert isinstance(recovered, Ok)
    assert recovered.value.source_delta.filled_samples == 0
    assert recovered.value.source_delta.dropped_backlog_samples == 0
    assert sink.observations[0].block.header.seq == 1
    assert sink.observations[0].first_index == 6


async def test_corrupt_bytes_loss_is_batch_associated_and_not_raw_fill() -> None:
    source = client(ManualClock(start=Monotonic(10)))
    source.ingest(encoded(0, (101,)) + encoded(1, (202,)))
    corrupted = bytearray(encoded(2, (303,)))
    corrupted[1] ^= 255
    source.ingest(bytes(corrupted) + encoded(3, (404,)) + encoded(4, (505,)))
    batch = await source.read_samples(1)
    assert batch is not None
    assert batch.provenance is not None
    source.ingest(b"\xff" * 20)

    sink = Sink()
    result = RawCapture(Monotonic(0), sink).capture(batch)

    assert isinstance(result, Ok)
    assert result.value.source_delta.skipped_bytes == 3
    assert result.value.source_delta.sync_losses == 1
    assert result.value.source_delta.filled_samples == 1
    assert tuple(batch.channels[0].values) == (101, 202, 202, 404, 505)
    assert [item.block.samples for item in sink.observations] == [((101, 202),), ((404, 505),)]


def test_overflow_drop_ranges_do_not_reuse_ordinals_after_segment_change() -> None:
    first = synthetic_batch(tuple(float(300 + i % 700) for i in range(2501)))
    sink = Sink(reject=frozenset({0, 1}))
    capture = RawCapture(Monotonic(0), sink)

    result = capture.capture(first)

    assert isinstance(result, Ok)
    assert result.value.accepted == 1
    assert (capture.recorder_loss.first_sequence, capture.recorder_loss.last_sequence) == (0, 1)
    assert capture.recorder_loss.first_at == Monotonic(9)
    assert capture.recorder_loss.last_at == pytest.approx(10.999)
    assert sink.observations[0].block.header.seq == 2
    assert sink.observations[0].block.header.n_samples == 501
    assert first.provenance is not None
    following = replace(
        first.provenance,
        segment=SegmentId(2),
        first_index=SampleIndex(2501),
        first_at=Monotonic(20),
    )
    assert isinstance(capture.capture(replace(first, provenance=following)), Ok)
    assert [item.block.header.seq for item in sink.observations] == [2, 3, 4, 5]


def test_unknown_external_batch_is_not_assumed_to_be_physical_raw() -> None:
    sink = Sink()
    source = synthetic_batch()
    external = SampleBatch(source.timestamp, source.channels)

    result = RawCapture(Monotonic(0), sink).capture(external)

    assert result == Err(AcquisitionProblem(Unavailable.UNKNOWN))
    assert sink.observations == []


async def test_simulated_source_is_explicit_complete_synthetic() -> None:
    clock = ManualClock(start=Monotonic(10))
    simulated = SimulatedBitalinoClient(
        clock, physiology=Physiology(geometry=SIM_ARM, origin=clock.monotonic())
    )
    assert await simulated.connect()
    assert await simulated.start_acquisition()
    clock.advance(Seconds(1))
    batch = await simulated.read_samples(1000)
    assert batch is not None
    assert batch.provenance is not None
    assert batch.provenance.spans == (ActualSpan(0, 1000),)
    sink = Sink()
    assert isinstance(RawCapture(Monotonic(9), sink).capture(batch), Ok)
    assert sink.observations[0].origin is Origin.SYNTHETIC
    assert sink.observations[0].block.header.t_first == 1
    assert sink.observations[0].block.samples == (tuple(int(v) for v in batch.channels[0].values),)


async def test_gap_capture_does_not_change_sensor_tap_or_dsp_rows() -> None:
    source = client(ManualClock(start=Monotonic(10)), (0, 1))
    source.ingest(
        b"".join(
            encoded(seq, (value, value + 1))
            for seq, value in ((1, 101), (2, 202), (5, 505), (6, 606))
        )
    )
    batch = await source.read_samples(1)
    assert batch is not None
    tapped: list[SampleBatch] = []
    raw = Sink()
    capture = RawCapture(Monotonic(0), raw)

    def tap(value: SampleBatch) -> None:
        tapped.append(value)
        assert isinstance(capture.capture(value), Ok)

    treatment = FakeTreatment([{"channel": "ECG", "values": [0.1]}], {"ECG": GOOD_70}, seq=1)
    bridge = EcgBridge(
        clock=ManualClock(),
        source=ScriptedSource([batch]),
        treatment=treatment,
        heart_rate=RecordingRuntime(),
        waveform=RecordingRing(),
        sample_rate=1000,
        tap=tap,
    )
    assert await bridge.pump() == 1
    assert tapped == [batch]
    assert treatment.handed == [
        [{"channel": "ECG", "values": [101.0, 202.0, 202.0, 202.0, 505.0, 606.0]}]
    ]
    assert [item.block.samples for item in raw.observations] == [
        ((101, 202), (102, 203)),
        ((505, 606), (506, 607)),
    ]


def test_modulo_sixteen_loss_is_only_a_lower_bound() -> None:
    decoder = FrameDecoder(1)
    assert decoder.feed(encoded(1, (301,)) + encoded(2, (302,)) + encoded(3, (703,))) == [
        (301,),
        (302,),
        (703,),
    ]
    assert decoder.filled_samples == 0
    assert decoder.actual_spans == (ActualSpan(0, 3),)
