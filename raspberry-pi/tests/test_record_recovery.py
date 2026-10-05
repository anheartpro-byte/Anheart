from dataclasses import replace
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record.ecg import Header, RawBlock
from src.record.reader import read
from src.record.schema import Event, EventKind
from src.result import Err, Ok
from tests.record_support import writer


def test_ex9_incomplete_gzip_blocks_are_skipped_without_inventing_samples(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    block = RawBlock(Header(0, 0, 2, ("ECG",)), ((1, 2),))
    recording.raw(block)
    recording.raw(replace(block, header=replace(block.header, seq=1)))
    second = recording.path / "ecg_raw/000001.bin.gz"
    second.write_bytes(second.read_bytes()[:-5])
    (recording.path / "ecg_raw/000002.bin.gz").touch()
    result = read(recording.path)
    assert isinstance(result, Ok)
    assert result.value.raw == (block,)
    assert {(w.file, w.code) for w in result.value.warnings} >= {
        ("000001.bin.gz", "truncated"),
        ("000002.bin.gz", "truncated"),
    }


def test_ex4_ex10_actor_is_an_identifier_and_cannot_be_a_named_person(tmp_path: Path) -> None:
    recording = writer(tmp_path, ("NamedPassenger",))
    for actor in ("NamedPassenger", "a b", "person@example.invalid"):
        event = Event(t=0, kind=EventKind.OPERATOR_ACTION, detail="start", actor=actor)
        assert isinstance(recording.event(event), Err)
    assert not (recording.path / "events.jsonl").read_bytes()


def test_ex9_unknown_event_kind_is_rejected_by_shared_reader(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    (recording.path / "events.jsonl").write_text(
        '{"t":0,"kind":"invented","detail":"","actor":"system"}\n'
    )
    assert isinstance(read(recording.path), Err)


def test_ex8_failed_manifest_publish_preserves_the_open_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    original = (recording.path / "manifest.json").read_bytes()

    def failed_replace(_source: Path, _target: Path) -> Path:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(Path, "replace", failed_replace)
    assert isinstance(recording.close(ManualClock(), "done"), Err)
    assert (recording.path / "manifest.json").read_bytes() == original
    assert not (recording.path / "checksums.sha256").exists()


def test_ex9_corrupt_deflate_returns_a_typed_read_error(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    malformed = bytes.fromhex("1f8b0800000000000203") + b"\xff" * 20
    (recording.path / "ecg_raw/000000.bin.gz").write_bytes(malformed)
    assert isinstance(read(recording.path), Err)


def test_ex4_reader_refuses_a_free_text_actor(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    (recording.path / "events.jsonl").write_text(
        '{"t":0,"kind":"warning","detail":"","actor":"not an ID"}\n'
    )
    assert isinstance(read(recording.path), Err)
