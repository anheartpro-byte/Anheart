from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from simulation.import_privacy import (
    NEUTRAL_FOLDER,
    NEUTRAL_MILLIS,
    anonymous_events,
    anonymous_manifest,
    identifying_content,
    identifying_frames,
    identities,
    remaining_identity,
)
from simulation.real_records import (
    ACCEPTED_SUFFIX,
    ARCHIVE_SUFFIX,
    REAL_DIR,
    SCENARIO_NAME,
    pack,
    unpack,
)
from simulation.replay import replay
from simulation.replay_config import sidecar
from simulation.replay_report import ReplayReport
from src.clock import ManualClock
from src.record.codec import JSON, Privacy, document, encode
from src.record.containment import resolve_under
from src.record.reader import Recording, read
from src.record.writer import EVENT, MANIFEST, Writer
from src.result import Err, Ok, Result
from src.units import UnixMillis


@dataclass(frozen=True, slots=True)
class Imported:
    archive: Path
    report: ReplayReport


@dataclass(frozen=True, slots=True)
class Acceptance:
    ticket: str
    reason: str


def _configuration(source: Path, recording: Recording, config: Path | None) -> Path:
    if config is not None:
        return config
    adjacent = sidecar(source)
    if adjacent.exists() or not source.is_dir():
        return adjacent
    return source.parent / "config" / (recording.manifest.config_hash + ".json")


def _anonymize(source: Path, recording: Recording, name: str, work: Path) -> Result[Path, str]:
    events = anonymous_events(recording)
    if isinstance(events, Err):
        return events
    manifest = anonymous_manifest(recording.manifest, name)
    if identifying_content(MANIFEST.dump_json(manifest), identities(recording)):
        return Err("identity or absolute date remains in the manifest")
    folder = work / f"{NEUTRAL_FOLDER}_{manifest.local_ref}"
    shutil.copytree(source, folder)
    (folder / "checksums.sha256").unlink()
    (folder / "events.jsonl").write_text(
        "".join(encode(document(EVENT, event), Privacy()) + "\n" for event in events.value),
        encoding="utf-8",
    )
    writer = Writer(folder, manifest, Privacy())
    closed = writer.close(
        ManualClock(epoch_millis=UnixMillis(NEUTRAL_MILLIS)),
        manifest.end_reason or "",
        manifest.end_observation,
    )
    if isinstance(closed, Err):
        return Err("the anonymized record could not be finalized")
    if remaining_identity(folder, identities(recording)):
        return Err("identity or absolute date remains in the record")
    return Ok(folder)


def _publish(files: tuple[tuple[Path, Path], ...]) -> None:
    created: list[Path] = []
    try:
        for source, target in files:
            with target.open("xb") as output:
                created.append(target)
                with source.open("rb") as content:
                    shutil.copyfileobj(content, output)
    except OSError:
        for target in created:
            target.unlink()
        raise


def _load_source(source: Path) -> Result[Recording, str]:  # noqa: PLR0911  # one return per refusal
    members = tuple(source.rglob("*"))
    if any(path.is_symlink() for path in members):
        return Err("the source record contains a link")
    allowed = {
        "manifest.json",
        "events.jsonl",
        "drive_frames.jsonl",
        "ticks.csv",
        "sensors.csv",
        "checksums.sha256",
        "ecg_raw",
    }
    if any(
        (not path.is_file() and not path.is_dir())
        or (
            path.relative_to(source).as_posix() not in allowed
            and re.fullmatch(r"ecg_raw/[0-9]+\.bin\.gz", path.relative_to(source).as_posix())
            is None
        )
        for path in members
    ):
        return Err("the source record contains an entry outside schema 2")
    loaded = read(source)
    if isinstance(loaded, Err):
        return Err("the source record cannot be read")
    recording = loaded.value
    if recording.manifest.ended_at is None or recording.manifest.end_reason is None:
        return Err("the source record is not closed")
    if recording.warnings:
        return Err("the source record is not intact")
    if identifying_frames(recording, identities(recording)):
        return Err("identity, absolute date or invalid hex remains in drive frames")
    return Ok(recording)


def _import_folder(  # noqa: PLR0911  # one return per refusal
    source: Path,
    original: Path,
    name: str,
    archive: Path,
    work: Path,
    *,
    config: Path | None,
    acceptance: Acceptance | None,
) -> Result[Imported, str]:
    loaded = _load_source(source)
    if isinstance(loaded, Err):
        return loaded
    recording = loaded.value
    known = identities(recording)
    if acceptance is not None and identifying_content(acceptance.reason.encode("utf-8"), known):
        return Err("identity or absolute date remains in the acceptance reason")
    if identifying_content(name.encode("utf-8"), known):
        return Err("the library name contains an original identity or absolute date")
    made = _anonymize(source, recording, name, work)
    if isinstance(made, Err):
        return made
    configuration = _configuration(original, recording, config)
    original_replay = replay(source, configuration)
    if isinstance(original_replay, Err):
        return Err("the source record is not replayable")
    config_bytes = configuration.read_bytes()
    if identifying_content(config_bytes, identities(recording)):
        return Err("identity or absolute date remains in the configuration document")
    replayed = replay(made.value, configuration)
    if isinstance(replayed, Err):
        return Err("the anonymized record is not replayable")
    report = replayed.value
    original_report = original_replay.value
    if (
        report.outcome != original_report.outcome
        or report.comparison != original_report.comparison
        or report.divergence != original_report.divergence
        or report.recorded_ticks != original_report.recorded_ticks
    ):
        return Err("the anonymized replay differs from the source replay")
    files: list[tuple[Path, Path]] = []
    if not report.matches:
        if acceptance is None:
            return Err("a replay difference requires --accept-ticket and --accept-reason")
        accepted = work / "accepted.json"
        accepted.write_text(
            json.dumps(
                {
                    "ticket": acceptance.ticket,
                    "reason": acceptance.reason,
                    "report": JSON.validate_json(report.to_json()),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        files.append((accepted, archive.with_name(name + ACCEPTED_SUFFIX)))
    elif acceptance is not None:
        return Err("a matching replay has no difference to accept")
    saved_config = work / "config.json"
    saved_config.write_bytes(config_bytes)
    files.append((saved_config, sidecar(archive)))
    staged = work / "archive.tar.gz"
    pack(made.value, staged)
    files.append((staged, archive))
    archive.parent.mkdir(parents=True, exist_ok=True)
    _publish(tuple(files))
    return Ok(Imported(archive, report))


def _destination(name: str, directory: Path) -> Result[Path, str]:
    try:
        archive = resolve_under(directory, name + ARCHIVE_SUFFIX)
        adjacent = (directory / (name + ".config.json"), directory / (name + ACCEPTED_SUFFIX))
        if archive is None or any(resolve_under(directory, path.name) is None for path in adjacent):
            return Err("the destination leaves its library directory")
        if archive.exists() or any(path.exists() for path in adjacent):
            return Err("the archive or one of its sidecars already exists")
    except OSError:
        return Err("the destination cannot be inspected")
    return Ok(archive)


def import_record(
    source: Path,
    name: str,
    directory: Path = REAL_DIR,
    *,
    config: Path | None = None,
    acceptance: Acceptance | None = None,
) -> Result[Imported, str]:
    if SCENARIO_NAME.fullmatch(name) is None:
        return Err("a library name uses only letters, digits and underscores")
    destination = _destination(name, directory)
    if isinstance(destination, Err):
        return destination
    if acceptance is not None and (
        not re_ticket(acceptance.ticket) or not acceptance.reason.strip()
    ):
        return Err("an accepted difference requires a ticket ANH-<number> and a reason")
    try:
        with TemporaryDirectory(prefix="anheart-import-") as temporary:
            work = Path(temporary)
            folder = source
            if not source.is_dir():
                extracted = unpack(source, work / "source")
                if isinstance(extracted, Err):
                    return extracted
                folder = extracted.value
            return _import_folder(
                folder, source, name, destination.value, work, config=config, acceptance=acceptance
            )
    except OSError:
        return Err("the import could not read or write its files")


def re_ticket(ticket: str) -> bool:
    prefix, separator, number = ticket.partition("-")
    return prefix == "ANH" and separator == "-" and number.isascii() and number.isdecimal()


class _Args(argparse.Namespace):
    source: Path = Path()
    name: str = ""
    directory: Path = REAL_DIR
    config: Path | None = None
    accept_ticket: str | None = None
    accept_reason: str | None = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.import_record")
    parser.add_argument("source", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--directory", type=Path, default=REAL_DIR)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--accept-ticket")
    parser.add_argument("--accept-reason")
    args = parser.parse_args(argv, namespace=_Args())
    ticket, reason = args.accept_ticket, args.accept_reason
    if (ticket is None) != (reason is None):
        parser.error("--accept-ticket and --accept-reason must be supplied together")
    acceptance = None if ticket is None or reason is None else Acceptance(ticket, reason)
    result = import_record(
        args.source, args.name, args.directory, config=args.config, acceptance=acceptance
    )
    if isinstance(result, Err):
        print(result.error, file=sys.stderr)  # noqa: T201  # CLI diagnostic
        return 2
    print(str(result.value.archive))  # noqa: T201  # CLI result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
