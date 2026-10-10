import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record.codec import JSON, Privacy, encode, mapping
from src.record.ecg import Header, RawBlock, decode_block, encode_block
from src.record.reader import read
from src.record.schema import DriveFrame, Event, EventKind
from src.record.writer import TICK_COLUMNS, Writer
from src.result import Err, Ok
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.types import SignalQuality
from src.units import Monotonic, UnixMillis
from tests.record_support import manifest, row, writer


def test_ex1_ex2_ex8_closed_record_has_exact_members_and_independent_hashes(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.tick(row()), Ok)
    closed = recording.close(ManualClock(epoch_millis=UnixMillis(1791195100000)), "operator_stop")
    assert isinstance(closed, Ok)
    assert recording.path.name == "2026-10-05T101112Z_local-4"
    assert {p.name for p in recording.path.iterdir()} == {
        "manifest.json",
        "ticks.csv",
        "events.jsonl",
        "drive_frames.jsonl",
        "ecg_raw",
        "sensors.csv",
        "checksums.sha256",
    }
    for line in (recording.path / "checksums.sha256").read_text().splitlines():
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((recording.path / name).read_bytes()).hexdigest()
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.end_reason == "operator_stop"
    assert loaded.value.manifest.ended_at == "2026-10-05T10:11:40Z"
    assert not loaded.value.warnings


def test_ex3_rows_round_trip_with_shared_column_order_and_observed_hr(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.tick(row()), Ok)
    assert (recording.path / "ticks.csv").read_text().splitlines()[0].split(",") == list(
        TICK_COLUMNS
    )
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)


@pytest.mark.parametrize("kind", list(EventKind))
def test_ex4_every_closed_event_kind_preserves_actor(tmp_path: Path, kind: EventKind) -> None:
    recording = writer(tmp_path)
    event = Event(t=0.4, kind=kind, detail="synthetic", actor="op-5")
    assert isinstance(recording.event(event), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.events == (event,)
    assert event.to_json()["actor"] == "op-5"


def test_ex5_individual_register_observations_preserve_failure_and_latency(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    frames = (
        DriveFrame(t=0.1, kind="write_request", register=8602, value=100, ok=True, latency_ms=0),
        DriveFrame(t=0.12, kind="write_response", register=8602, value=100, ok=True, latency_ms=20),
        DriveFrame(
            t=0.14, kind="read_response", register=3201, value=None, ok=False, latency_ms=50
        ),
    )
    for frame in frames:
        assert isinstance(recording.frame(frame), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.frames == frames


def test_ex6_raw_blocks_are_gzip_json_then_channel_major_int16_le_with_gaps(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    first = RawBlock(Header(0, 0.0, 3, ("ECG", "EDA")), ((-32768, 0, 32767), (1, 2, 3)))
    third = replace(first, header=replace(first.header, seq=2, t_first=0.006))
    for block in (first, third):
        assert isinstance(recording.raw(block), Ok)
    paths = sorted((recording.path / "ecg_raw").iterdir())
    assert [path.name for path in paths] == ["000000.bin.gz", "000002.bin.gz"]
    header, samples = gzip.decompress(paths[0].read_bytes()).split(b"\n", 1)
    assert mapping(JSON.validate_json(header))["sample_rate"] == 1000
    assert samples == b"\x00\x80\x00\x00\xff\x7f\x01\x00\x02\x00\x03\x00"
    assert decode_block(paths[0].read_bytes()) == first
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.raw == (first, third)


def test_anh131_reception_instant_is_optional_and_a_block_without_it_keeps_its_bytes() -> None:
    plain = RawBlock(Header(0, 0.0, 1, ("ECG",)), ((7,),))
    received = replace(plain, header=replace(plain.header, t_received=0.2))
    header, _ = gzip.decompress(encode_block(plain)).split(b"\n", 1)
    assert header == b'{"seq":0,"t_first":0.0,"n_samples":1,"channels":["ECG"],"sample_rate":1000}'
    stamped, _ = gzip.decompress(encode_block(received)).split(b"\n", 1)
    assert mapping(JSON.validate_json(stamped))["t_received"] == 0.2
    assert decode_block(encode_block(plain)) == plain
    assert decode_block(encode_block(received)) == received
    with pytest.raises(ValueError, match="finite"):
        Header(0, 0.0, 1, ("ECG",), 1000, float("nan"))


def test_ex7_configured_sensor_metrics_are_published_at_one_hz(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    readings = tuple(
        SensorReading(
            kind,
            Monotonic(10),
            10,
            (),
            SignalQuality.GOOD,
            "",
            (Metric("observed", "display label", float(i), "unit"),),
        )
        for i, kind in enumerate(SensorKind)
    )
    for t in (0.0, 0.2, 0.8, 1.0):
        assert isinstance(recording.sensors(t, readings), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.sensors) == 12
    assert {r.channel for r in loaded.value.sensors} == {kind.value for kind in SensorKind}
    assert {r.t for r in loaded.value.sensors} == {0.0, 1.0}


def test_ex8_abrupt_close_missing_checksums_is_readable_and_warned(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    recording.tick(row())
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)
    assert [w.code for w in loaded.value.warnings] == ["missing_checksums"]


@pytest.mark.parametrize(
    "filename", ["ticks.csv", "events.jsonl", "drive_frames.jsonl", "sensors.csv"]
)
def test_ex9_only_complete_lines_survive_a_truncated_tail(tmp_path: Path, filename: str) -> None:
    recording = writer(tmp_path)
    recording.tick(row())
    recording.event(Event(t=0.1, kind=EventKind.WARNING, detail="synthetic"))
    before = read(recording.path)
    assert isinstance(before, Ok)
    with (recording.path / filename).open("ab") as handle:
        handle.write(b"partial-\xc3")
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == before.value.rows
    assert loaded.value.events == before.value.events
    assert any(w.file == filename and w.code == "truncated" for w in loaded.value.warnings)


def test_ex10_named_passenger_and_email_are_absent_from_every_file_and_path(tmp_path: Path) -> None:
    passenger = "SyntheticPassengerSentinel"
    email = "passenger.sentinel@example.invalid"
    privacy = Privacy((passenger,))
    given = replace(manifest(), software_version=f"{passenger} {email}")
    created = Writer.create(tmp_path, given, privacy)
    assert isinstance(created, Ok)
    recording = created.value
    recording.tick(replace(row(), safety_rule=passenger))
    recording.event(Event(t=0.1, kind=EventKind.WARNING, detail=f'{passenger} says "{email}"'))
    recording.close(ManualClock(), passenger)
    for path in recording.path.rglob("*"):
        assert passenger not in str(path)
        assert email not in str(path)
        if path.is_file():
            assert passenger.encode() not in path.read_bytes()
            assert email.encode() not in path.read_bytes()
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.events[0].detail == '[redacted] says "[redacted]"'
    assert isinstance(
        Writer.create(tmp_path, replace(manifest(), local_ref=passenger), privacy), Err
    )


def test_ex9_nonfinite_nested_json_is_refused_without_writing() -> None:
    with pytest.raises(ValueError, match="Out of range"):
        encode({"nested": [float("nan")]}, Privacy())
    assert mapping(JSON.validate_json(encode({"key": (1, True, None)}, Privacy()))) == {
        "key": [1, True, None]
    }
    assert json.loads(encode({"name": "x"}, Privacy(("",)))) == {"name": "x"}
