import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.clock import ManualClock
from src.record.codec import mapping
from src.record.ecg import Header, RawBlock, decode_block, encode_block
from src.record.reader import read
from src.record.schema import Clocks, DriveFrame, Event, EventKind
from src.record.writer import Writer
from src.result import Err, Ok
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.types import SignalQuality
from src.units import Monotonic
from tests.record_support import manifest, row, writer


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_ex9_each_numeric_stream_rejects_nonfinite_values(tmp_path: Path, bad: float) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.tick(replace(row(), t=bad)), Err)
    assert len((recording.path / "ticks.csv").read_text().splitlines()) == 1
    with pytest.raises(ValidationError):
        Event(t=bad, kind=EventKind.WARNING, detail="")
    with pytest.raises(ValidationError):
        DriveFrame(t=0, kind="read", register=3201, value=0, ok=True, latency_ms=bad)
    with pytest.raises(ValidationError):
        Header(0, bad, 1, ("ECG",))
    with pytest.raises(ValidationError):
        Clocks(monotonic_start=Monotonic(bad), utc_start="2026-10-05T00:00:00Z", ntp_offset_s=None)
    readings = (
        SensorReading(
            SensorKind.ECG,
            Monotonic(0),
            1,
            (),
            SignalQuality.GOOD,
            "",
            (Metric("rate", "Rate", bad, "bpm"),),
        ),
    )
    assert isinstance(recording.sensors(0, readings), Err)
    assert len((recording.path / "sensors.csv").read_text().splitlines()) == 1


@pytest.mark.parametrize("bad", ["../escape", "with space", "person@example.invalid", ""])
def test_ex1_ex10_record_identifiers_cannot_escape_or_contain_free_text(
    tmp_path: Path, bad: str
) -> None:
    result = Writer.create(tmp_path, replace(manifest(), local_ref=bad))
    assert isinstance(result, Err)
    assert not tuple(tmp_path.iterdir())


def test_ex2_bad_hash_and_non_utc_clocks_are_rejected(tmp_path: Path) -> None:
    assert isinstance(Writer.create(tmp_path, replace(manifest(), config_hash="not-sha256")), Err)
    assert isinstance(Writer.create(tmp_path, replace(manifest(), started_at="2026-10-05")), Err)
    assert isinstance(Writer.create(tmp_path, replace(manifest(), started_at="invalid")), Err)
    with pytest.raises(TypeError):
        mapping([1, 2])


def test_ex8_existing_record_is_never_overwritten_and_closed_writer_refuses_append(
    tmp_path: Path,
) -> None:
    recording = writer(tmp_path)
    assert isinstance(Writer.create(tmp_path, manifest()), Err)
    closed = recording.close(ManualClock(), "done")
    assert isinstance(closed, Ok)
    before = (recording.path / "ticks.csv").read_bytes()
    assert isinstance(recording.tick(row()), Err)
    closed = recording.close(ManualClock(), "second")
    assert isinstance(closed, Err)
    assert (recording.path / "ticks.csv").read_bytes() == before


def test_ex12_io_failures_return_errors_without_claiming_close(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    tick_path = recording.path / "ticks.csv"
    tick_path.unlink()
    tick_path.mkdir()
    assert isinstance(recording.tick(row()), Err)
    manifest_path = recording.path / "manifest.json"
    manifest_path.unlink()
    manifest_path.mkdir()
    closed = recording.close(ManualClock(), "done")
    assert isinstance(closed, Err)
    assert not recording.closed
    assert not (recording.path / "checksums.sha256").exists()


@pytest.mark.parametrize(
    "block",
    [
        RawBlock(Header(-1, 0, 1, ("ECG",)), ((0,),)),
        RawBlock(Header(0, 0, 0, ("ECG",)), ((),)),
        RawBlock(Header(0, 0, 1, ()), ()),
        RawBlock(Header(0, 0, 1, ("ECG", "ECG")), ((0,), (0,))),
        RawBlock(Header(0, 0, 1, ("ECG",)), ()),
        RawBlock(Header(0, 0, 2, ("ECG",)), ((0,),)),
        RawBlock(Header(0, 0, 1, ("ECG",)), ((32768,),)),
        RawBlock(Header(0, 0, 1, ("PrivateName",)), ((1,),)),
    ],
)
def test_ex6_invalid_raw_block_never_creates_a_file(tmp_path: Path, block: RawBlock) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.raw(block), Err)
    assert not tuple((recording.path / "ecg_raw").iterdir())


def test_ex6_duplicate_block_refuses_overwrite_and_bad_binary_length_is_rejected(
    tmp_path: Path,
) -> None:
    recording = writer(tmp_path)
    block = RawBlock(Header(0, 0, 1, ("ECG",)), ((123,),))
    assert isinstance(recording.raw(block), Ok)
    assert isinstance(recording.raw(block), Err)
    encoded = encode_block(block)
    prefix = gzip.decompress(encoded).split(b"\n", 1)[0]
    with pytest.raises(ValueError, match="payload length"):
        decode_block(gzip.compress(prefix + b"\n\x01"))


def test_ex7_blank_missing_and_invalid_sensor_values_keep_their_meaning(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    blank = SensorReading(SensorKind.EDA, Monotonic(0), 1, (), SignalQuality.NO_SIGNAL, "", ())
    missing = replace(blank, kind=SensorKind.ECG, metrics=(Metric("rate", "Rate", None, "bpm"),))
    assert isinstance(recording.sensors(0, (blank, missing)), Ok)
    result = read(recording.path)
    assert isinstance(result, Ok)
    assert [(r.channel, r.metric, r.value) for r in result.value.sensors] == [
        ("EDA", "", None),
        ("ECG", "rate", None),
    ]
    assert isinstance(recording.sensors(float("nan"), ()), Err)
    closed = recording.close(ManualClock(), "done")
    assert isinstance(closed, Ok)
    assert isinstance(recording.sensors(2, (blank,)), Err)


@pytest.mark.parametrize("content", ["wrong,header\n", "t,state\n1,x\n"])
def test_ex9_malformed_complete_csv_is_an_error_not_silently_discarded(
    tmp_path: Path, content: str
) -> None:
    recording = writer(tmp_path)
    (recording.path / "ticks.csv").write_text(content)
    assert isinstance(read(recording.path), Err)


def test_ex9_empty_truncated_csv_returns_no_rows_and_a_warning(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    (recording.path / "ticks.csv").write_bytes(b"t,sta")
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == ()
    assert any(w.code == "truncated" for w in loaded.value.warnings)


def test_ex9_incorrect_csv_width_and_nonfinite_sensor_file_are_rejected(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    with (recording.path / "ticks.csv").open("a") as handle:
        handle.write("1,x\n")
    assert isinstance(read(recording.path), Err)
    (recording.path / "ticks.csv").write_text("")
    with (recording.path / "sensors.csv").open("a") as handle:
        handle.write("0,ECG,good,rate,NaN,bpm\n")
    assert isinstance(read(recording.path), Err)


def test_ex8_checksums_warn_on_changed_missing_and_unindexed_files(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    recording.close(ManualClock(), "done")
    with (recording.path / "events.jsonl").open("a") as handle:
        handle.write(
            json.dumps({"t": 0, "kind": "warning", "detail": "changed", "actor": "system"}) + "\n"
        )
    (recording.path / "extra").write_text("unindexed")
    with (recording.path / "checksums.sha256").open("a") as handle:
        handle.write("0  missing\n0  ../outside\n")
    result = read(recording.path)
    assert isinstance(result, Ok)
    assert {(w.file, w.code) for w in result.value.warnings} == {
        ("events.jsonl", "checksum_mismatch"),
        ("extra", "checksum_mismatch"),
        ("missing", "missing_file"),
        ("../outside", "missing_file"),
    }
