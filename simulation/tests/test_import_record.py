from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from simulation import import_record as importer
from simulation import real_records
from simulation.import_privacy import NEUTRAL_MILLIS, NEUTRAL_UTC, anonymous_manifest, identities
from simulation.import_record import Acceptance, import_record
from simulation.real_records import judge, pack, replay_path, unpack
from simulation.replay import ReplayError, replay
from simulation.replay_config import sidecar
from simulation.replay_report import ReplayReport
from simulation.tests.replay_support import (
    Cells,
    copy,
    edit_lines,
    edit_manifest,
    edit_ticks,
    reseal,
)
from simulation.tests.test_record_console_parity import console_record
from simulation.tests.test_replay_modbus import console_native
from src.record import writer as shared_writer
from src.record.codec import JSON, mapping
from src.record.reader import Recording, read
from src.result import Err, Ok


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, Path]]:
    result: dict[str, tuple[Path, Path]] = {}
    for name in ("manual", "programme", "native"):
        root = tmp_path_factory.mktemp("import-" + name)
        if name == "native":
            result[name] = asyncio.run(console_native(root))
        else:
            folder = asyncio.run(console_record(root, programme=name == "programme"))
            loaded = read(folder)
            assert isinstance(loaded, Ok)
            result[name] = (
                folder,
                folder.parent / "config" / (loaded.value.manifest.config_hash + ".json"),
            )
    return result


def loaded(folder: Path) -> Recording:
    result = read(folder)
    assert isinstance(result, Ok), result
    return result.value


def fingerprints(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in folder.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("kind", ["manual", "programme", "native"])
def test_console_import_is_anonymous_intact_replayed_and_judged(
    sources: dict[str, tuple[Path, Path]],
    tmp_path: Path,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder, config = sources[kind]
    original = loaded(folder)
    before = fingerprints(folder)
    source = folder
    if kind == "manual":
        source = tmp_path / "source.tar.gz"
        pack(folder, source)
        sidecar(source).write_bytes(config.read_bytes())
        assert (
            importer.main([str(source), "--name", kind, "--directory", str(tmp_path / "library")])
            == 0
        )
        assert capsys.readouterr().out.strip() == str(tmp_path / "library" / (kind + ".tar.gz"))
        report = replay_path(tmp_path / "library" / (kind + ".tar.gz"))
        assert isinstance(report, Ok)
        archive = tmp_path / "library" / (kind + ".tar.gz")
    else:
        result = import_record(
            source, kind, tmp_path / "library", config=config if kind == "native" else None
        )
        assert isinstance(result, Ok), result
        report = Ok(result.value.report)
        archive = result.value.archive
    assert report.value.matches, report.value.to_text()
    assert report.value.comparison.deviating_ticks == 0
    assert report.value.comparison.tolerated_rpm == (0, 0)
    assert report.value.comparison.shifted_transitions == (0, 0)
    assert report.value.configuration.config_hash_verified
    assert report.value.integrity == ()
    assert sidecar(archive).read_bytes() == config.read_bytes()
    assert judge(archive, tmp_path / "judged") is None
    extracted = unpack(archive, tmp_path / "checked")
    assert isinstance(extracted, Ok)
    anon = loaded(extracted.value)
    assert anon.warnings == ()
    assert anon.manifest == replace(
        anonymous_manifest(original.manifest, kind), files=anon.manifest.files
    )
    assert (
        anon.manifest.started_at
        == anon.manifest.ended_at
        == anon.manifest.clocks.utc_start
        == NEUTRAL_UTC
    )
    if original.manifest.profile is not None:
        assert anon.manifest.profile is not None
        assert anon.manifest.profile.resolved_at == NEUTRAL_MILLIS
        assert original.manifest.profile.resolved_at != NEUTRAL_MILLIS
    assert anon.rows == original.rows
    assert anon.frames == original.frames
    assert anon.raw == original.raw
    assert anon.sensors == original.sensors
    assert [event.t for event in anon.events] == [event.t for event in original.events]
    for name, digest in before.items():
        if name not in {"manifest.json", "events.jsonl", "checksums.sha256"}:
            assert hashlib.sha256((extracted.value / name).read_bytes()).hexdigest() == digest
    for member in extracted.value.rglob("*"):
        if member.is_file():
            content = member.read_bytes()
            if member.name.endswith(".bin.gz"):
                content = gzip.decompress(content)
            for sentinel in identities(original):
                assert sentinel.encode().lower() not in content.lower()
            assert b"2023-11-14" not in content
            assert (
                str(original.manifest.profile.resolved_at).encode() not in content
                if original.manifest.profile is not None
                else True
            )
    assert fingerprints(folder) == before
    with capsys.disabled():
        print(report.value.to_json(), end="")  # noqa: T201


@pytest.mark.parametrize(
    "failure", ["open", "reader", "integrity", "command", "replay", "link", "extra", "fifo"]
)
def test_source_refusals_never_write_an_archive(
    sources: dict[str, tuple[Path, Path]],
    tmp_path: Path,
    failure: str,
) -> None:
    source, config = sources["programme"]
    folder = copy(source, tmp_path / "source")
    if failure == "open":
        edit_manifest(folder, lambda value: value.update(ended_at=None))
        reseal(folder)
    elif failure == "reader":
        edit_manifest(folder, lambda value: value.update(schema_version=999))
    elif failure == "integrity":
        path = folder / "events.jsonl"
        path.write_bytes(
            path.read_bytes() + b'{"t":0,"kind":"warning","detail":"dashboard:","actor":"system"}\n'
        )
    elif failure == "link":
        (folder / "planted").symlink_to(config)
    elif failure == "extra":
        (folder / "notes.txt").write_text("free prose is outside schema 2", encoding="utf-8")
        reseal(folder)
    elif failure == "fifo":
        (folder / "ticks.csv").unlink()
        os.mkfifo(folder / "ticks.csv")
    else:

        def change(lines: list[dict[str, object]]) -> list[dict[str, object]]:
            if failure == "command":
                next(line for line in lines if line["kind"] == "operator_action")["detail"] = (
                    "unknown"
                )
            else:
                lines.append(
                    {
                        "t": 0,
                        "kind": "warning",
                        "detail": "replay_config: unavailable",
                        "actor": "system",
                    }
                )
            return lines

        edit_lines(folder, "events.jsonl", change)
        reseal(folder)
    result = import_record(folder, "opaque", tmp_path / "library", config=config)
    assert isinstance(result, Err), result
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize("name", ["../outside", "", "a/b", "a-b", "a name", "x" * 256])
def test_invalid_names_are_refused_before_reading(tmp_path: Path, name: str) -> None:
    assert isinstance(import_record(tmp_path / "missing", name, tmp_path / "library"), Err)
    assert not (tmp_path / "library").exists()


@pytest.mark.parametrize("suffix", [".tar.gz", ".config.json", ".accepted.json"])
def test_existing_archive_or_sidecar_is_never_overwritten(tmp_path: Path, suffix: str) -> None:
    existing = tmp_path / ("opaque" + suffix)
    existing.write_bytes(b"keep exactly")
    result = import_record(tmp_path / "missing", "opaque", tmp_path)
    assert isinstance(result, Err)
    assert existing.read_bytes() == b"keep exactly"


def test_difference_needs_explicit_ticket_and_pins_the_whole_report(
    sources: dict[str, tuple[Path, Path]],
    tmp_path: Path,
) -> None:
    source, config = sources["native"]
    folder = copy(source, tmp_path / "source")

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        row = next(row for row in rows if float(row[header.index("t")]) > 2)
        column = header.index("setpoint_motor_rpm")
        row[column] = str(int(row[column]) + 3)
        return rows

    edit_ticks(folder, change)
    reseal(folder)
    shelf = tmp_path / "library"
    refused = import_record(folder, "opaque", shelf, config=config)
    assert isinstance(refused, Err)
    assert "--accept-ticket" in refused.error
    assert not shelf.exists()
    assert (
        importer.main(
            [
                str(folder),
                "--name",
                "opaque",
                "--directory",
                str(shelf),
                "--config",
                str(config),
                "--accept-ticket",
                "ANH-192",
                "--accept-reason",
                "controlled setpoint change",
            ]
        )
        == 0
    )
    archive = shelf / "opaque.tar.gz"
    replayed = replay_path(archive)
    assert isinstance(replayed, Ok)
    assert not replayed.value.matches
    document = mapping(JSON.validate_json((shelf / "opaque.accepted.json").read_bytes()))
    assert document["ticket"] == "ANH-192"
    assert document["report"] == JSON.validate_json(replayed.value.to_json())
    assert judge(archive, tmp_path / "judged") is None


@pytest.mark.parametrize("ticket", ["", "wrong", "ANH-x", "ANH-\u0661"])
def test_acceptance_requires_a_ticket(tmp_path: Path, ticket: str) -> None:
    assert isinstance(
        import_record(
            tmp_path / "missing",
            "opaque",
            tmp_path / "library",
            acceptance=Acceptance(ticket, "reason"),
        ),
        Err,
    )


def test_cli_refusal_and_incomplete_acceptance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        importer.main(
            [str(tmp_path / "absent"), "--name", "opaque", "--directory", str(tmp_path / "library")]
        )
        == 2
    )
    assert "archive cannot be read" in capsys.readouterr().err
    with pytest.raises(SystemExit) as caught:
        importer.main([str(tmp_path / "absent"), "--name", "opaque", "--accept-ticket", "ANH-192"])
    assert caught.value.code == 2


def test_links_cannot_redirect_the_library(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "opaque.tar.gz").symlink_to(tmp_path / "elsewhere")
    assert isinstance(import_record(tmp_path / "absent", "opaque", library), Err)


@pytest.mark.parametrize("suffix", [".config.json", ".accepted.json"])
def test_sidecar_links_cannot_redirect_the_library(tmp_path: Path, suffix: str) -> None:
    (tmp_path / ("opaque" + suffix)).symlink_to(tmp_path.parent / "elsewhere")
    assert isinstance(import_record(tmp_path / "absent", "opaque", tmp_path), Err)


def test_destination_io_failure_is_a_refusal_without_a_path_or_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cannot_resolve(_root: Path, _requested: str) -> None:
        raise OSError("private destination diagnostic")

    monkeypatch.setattr(importer, "resolve_under", cannot_resolve)
    result = import_record(tmp_path / "absent", "opaque", tmp_path)
    assert result == Err("the destination cannot be inspected")


def test_acceptance_needs_a_reason(tmp_path: Path) -> None:
    result = import_record(
        tmp_path / "absent", "opaque", tmp_path / "library", acceptance=Acceptance("ANH-192", " ")
    )
    assert isinstance(result, Err)


@pytest.mark.parametrize(
    "failure",
    [
        "manifest_identity",
        "manifest_date",
        "manifest_email",
        "sensor_identity",
        "configuration",
        "raw_hex",
        "ended_reason",
        "name",
        "acceptance_identity",
        "matching_acceptance",
        "finalize",
        "anonymized_replay",
    ],
)
def test_privacy_and_finalization_failures_are_refused(
    sources: dict[str, tuple[Path, Path]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source, config = sources["programme"]
    folder = copy(source, tmp_path / "source")
    record = loaded(folder)
    acceptance: Acceptance | None = None
    name = "opaque"
    if failure == "manifest_identity":
        edit_manifest(
            folder, lambda value: value.update(medical_parameters_version=record.manifest.operator)
        )
    elif failure == "manifest_date":
        edit_manifest(
            folder, lambda value: value.update(medical_parameters_version="2026-10-15T15:10:01Z")
        )
    elif failure == "manifest_email":
        edit_manifest(
            folder, lambda value: value.update(medical_parameters_version="someone@example.test")
        )
    elif failure == "sensor_identity":
        path = folder / "sensors.csv"
        path.write_text(
            path.read_text(encoding="utf-8").replace("ECG", record.manifest.operator, 1),
            encoding="utf-8",
        )
    elif failure == "ended_reason":
        edit_manifest(folder, lambda value: value.update(end_reason=None))
    elif failure == "configuration":
        content = dict(mapping(JSON.validate_json(config.read_bytes())))
        content["drive_link"] = record.manifest.operator
        config = tmp_path / "changed.config.json"
        config.write_text(json.dumps(content, separators=(",", ":")), encoding="utf-8")
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        edit_manifest(folder, lambda value: value.update(config_hash=digest))
    elif failure == "raw_hex":

        def add_hex(lines: list[dict[str, object]]) -> list[dict[str, object]]:
            lines[0]["raw_hex"] = record.manifest.operator.encode().hex()
            return lines

        edit_lines(folder, "drive_frames.jsonl", add_hex)
    elif failure == "name":
        name = record.manifest.record_id
    elif failure == "acceptance_identity":
        acceptance = Acceptance("ANH-192", "changed " + record.manifest.operator)
    elif failure == "matching_acceptance":
        acceptance = Acceptance("ANH-192", "controlled change")
    elif failure == "finalize":

        def cannot_write(_path: Path, _content: bytes) -> None:
            raise OSError("test disk refused")

        monkeypatch.setattr(shared_writer, "create_private", cannot_write)
    else:

        def refuse_anonymized(
            path: Path, config: Path | None = None
        ) -> Ok[ReplayReport] | Err[ReplayError]:
            if path.name.startswith("2000-"):
                return Err(ReplayError("controlled refusal"))
            return replay(path, config)

        monkeypatch.setattr(importer, "replay", refuse_anonymized)
    reseal(folder)
    result = import_record(folder, name, tmp_path / "library", config=config, acceptance=acceptance)
    assert isinstance(result, Err), result
    assert not (tmp_path / "library").exists()


def test_import_refuses_gzip_metadata_the_shared_reader_can_read(
    sources: dict[str, tuple[Path, Path]], tmp_path: Path
) -> None:
    source, config = sources["programme"]
    folder = copy(source, tmp_path / "source")
    block = next((folder / "ecg_raw").glob("*.bin.gz"))
    block.write_bytes(gzip.compress(gzip.decompress(block.read_bytes()), mtime=1_700_000_000))
    reseal(folder)
    assert loaded(folder).warnings == ()
    result = import_record(folder, "opaque", tmp_path / "library", config=config)
    assert result == Err("identity or absolute date remains in the record")
    assert not (tmp_path / "library").exists()


def test_folder_sidecar_and_event_sentinels_are_supported(
    sources: dict[str, tuple[Path, Path]],
    tmp_path: Path,
) -> None:
    source, config = sources["programme"]
    folder = copy(source, tmp_path / "source")
    sidecar(folder).write_bytes(config.read_bytes())

    def with_sentinels(lines: list[dict[str, object]]) -> list[dict[str, object]]:
        lines.extend(
            [
                {
                    "t": 1.1,
                    "kind": "warning",
                    "detail": "dashboard: sentinel_identity 2026-10-15 999 bpm",
                    "actor": "remote",
                },
                {
                    "t": 1.2,
                    "kind": "refusal",
                    "detail": "sentinel_identity 2026-10-15 999 bpm",
                    "actor": "another_alias",
                },
            ]
        )
        return lines

    edit_lines(folder, "events.jsonl", with_sentinels)
    edit_manifest(
        folder,
        lambda value: value.update(session_id="session_sentinel", subject_id="subject_sentinel"),
    )
    reseal(folder)
    result = import_record(folder, "opaque", tmp_path / "library")
    assert isinstance(result, Ok), result
    assert result.value.report.matches
    unpacked = unpack(result.value.archive, tmp_path / "checked")
    assert isinstance(unpacked, Ok)
    content = (unpacked.value / "events.jsonl").read_text(encoding="utf-8")
    assert not any(
        value in content
        for value in ("sentinel_identity", "999 bpm", "2026-10-15", "another_alias")
    )
    assert judge(result.value.archive, tmp_path / "judged") is None


def test_publish_cleans_only_its_own_files_on_collision(
    sources: dict[str, tuple[Path, Path]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, config = sources["programme"]
    shelf = tmp_path / "library"
    collision = shelf / "opaque.tar.gz"

    def collide(folder: Path, target: Path) -> None:
        pack(folder, target)
        shelf.mkdir()
        collision.write_bytes(b"theirs")

    monkeypatch.setattr(importer, "pack", collide)
    result = import_record(source, "opaque", shelf, config=config)
    assert isinstance(result, Err)
    assert not sidecar(collision).exists()
    assert collision.read_bytes() == b"theirs"


def test_import_refuses_an_archive_above_the_shared_extraction_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_bytes(b"{}")
    archive = tmp_path / "source.tar.gz"
    pack(source, archive)
    monkeypatch.setattr(real_records, "MAX_ARCHIVE_BYTES", 1)
    result = import_record(archive, "opaque", tmp_path / "library")
    assert result == Err("the archive exceeds MAX_ARCHIVE_BYTES")
    assert not (tmp_path / "library").exists()
