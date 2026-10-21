"""The library of recorded sessions the gate replays: ``simulation/scenarios/real/``.

One file per session: ``<name>.tar.gz``, the archive of one closed schema-2
record folder (the form a record leaves the console in). A session holds
thousands of raw ECG blocks, so the folder itself is not committed.

Every archive in the directory is judged by ``tests/test_real_records.py``
(:func:`judge`): closed, intact, anonymous, and replayed. That is what makes a
recorded session a non-regression scenario. Two things can stand next to an
archive:

* nothing: the replay must match;
* ``<name>.accepted.json``: a difference somebody examined and accepted, with
  the ticket that says why. It holds the replay's whole JSON report, and the
  replay must then give back THAT report and no other: the same first
  difference, the same counts of every kind, the same divergence. One more
  difference anywhere, or one fewer, and the gate fails; the day the replay
  matches again, the file must go.

A record made by the simulation is never excused that way, and the gate refuses
an accepted file next to one: it is exported again (:func:`export`), which
leaves an archive alone when what it holds did not change. A real session
cannot be recorded again, which is what the accepted file is for.
``docs/framework-de-test.md`` has the procedure.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import re
import shutil
import tarfile
import zlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Final, override

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from simulation.harness import OPERATOR, REMOTE, SYSTEM, run_scenario
from simulation.replay import ReplayError, integrity_line, replay, replay_recording
from simulation.replay_config import sidecar
from simulation.replay_report import Outcome, ReplayReport
from simulation.scenario import SCENARIO_DIR, load_scenario, scenario_paths
from src.record.codec import JSON, Privacy, mapping
from src.record.containment import resolve_under
from src.record.reader import Recording, read
from src.record.rows import JsonValue
from src.record.schema import Manifest
from src.result import Err, Ok, Result

REAL_DIR: Final[Path] = SCENARIO_DIR / "real"
ARCHIVE_SUFFIX: Final[str] = ".tar.gz"
ACCEPTED_SUFFIX: Final[str] = ".accepted.json"
MAX_ARCHIVE_BYTES: Final[int] = 512 * 1024 * 1024
"""At most 512 MiB of effective file sizes, also bounding decompressed reads below."""
MAX_ARCHIVE_MEMBERS: Final[int] = 40_000
"""Files and directories together, including repeated names."""
MAX_ARCHIVE_HEADER_BYTES: Final[int] = (
    MAX_ARCHIVE_MEMBERS * (2 * tarfile.BLOCKSIZE - 1) + tarfile.RECORDSIZE
)
"""One 512-byte header and up to 511 padding bytes per member, plus a tar record.

Extension headers and their payloads share the decompressed byte budget;
they are hidden by tarfile's public member iterator, so are not counted as members.
"""

SIMULATION_MACHINE: Final[str] = "simulation"
"""``machine_id`` of a record the simulation made (``simulation.session_record``)."""

SIMULATION_ORGANIZATION: Final[str] = "synthetic"

ANONYMIZED: Final[str] = "anonymized"
"""What stands where a real session named a machine, an organisation or an operator."""

UNVERSIONED: Final[str] = "unversioned"
"""``software_version`` of an exported record: never read from the environment."""

LOCAL_REF_LENGTH: Final[int] = 16

SCENARIO_NAME: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_]+")
"""A battery scenario, as its file is named: never a path."""


def library_identity(name: str) -> str:
    """The ``record_id`` of the library record ``name``: it says the name and nothing else.

    Derived, never drawn: the same scenario exported twice is the same record,
    and the identifier of a real session cannot be the one its source knows it by.
    """
    return hashlib.sha256(f"anheart-library:{name}".encode()).hexdigest()[:32]


def name_of(archive: Path) -> str:
    """``auto_jog_150_dsp`` for ``.../auto_jog_150_dsp.tar.gz``."""
    return archive.name.removesuffix(ARCHIVE_SUFFIX)


def archives(directory: Path = REAL_DIR) -> tuple[Path, ...]:
    """Every archived record of the library, sorted by name."""
    return tuple(sorted(directory.glob(f"*{ARCHIVE_SUFFIX}")))


# =========================================================================
# Archives
# =========================================================================


def pack(record: Path, archive: Path) -> None:
    """Archive the record folder ``record`` as ``archive``. Raises ``OSError``.

    Members sorted, no owner, no date: the same folder gives the same bytes.
    """
    archive.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as packed,
        tarfile.open(fileobj=packed, mode="w") as tar,
    ):
        for member in sorted(path for path in record.rglob("*") if path.is_file()):
            info = tarfile.TarInfo(f"{record.name}/{member.relative_to(record).as_posix()}")
            info.size = member.stat().st_size
            info.mode = 0o644
            with member.open("rb") as content:
                tar.addfile(info, content)


def _place(tar: tarfile.TarFile, member: tarfile.TarInfo, into: Path) -> str | None:
    """Write one member of an archive under ``into``. Why it is refused, or ``None``.

    An archive is somebody else's file. A record folder holds plain files in
    plain folders and nothing else, so nothing else is written: no link, no
    device, and no member whose name is absolute or climbs out of the folder
    it is extracted into. The content is copied to the path :func:`resolve_under`
    returns, and to no other.
    """
    name = member.name
    if name.startswith(("/", "\\")) or ".." in PurePosixPath(name.replace("\\", "/")).parts:
        return "the archive holds a member whose name is absolute or leaves its folder"
    target = resolve_under(into, name)
    if target is None:
        return "the archive holds a member that cannot be written inside its folder"
    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return None
    # Only a regular file has content of its own: a link's would be another
    # member's, or a file of this machine's.
    content = tar.extractfile(member) if member.isreg() else None
    if content is None:
        return "the archive holds a link or a device: a record is files in folders"
    target.parent.mkdir(parents=True, exist_ok=True)
    with content, target.open("wb") as written:
        shutil.copyfileobj(content, written)
    return None


class _ArchiveError(ValueError):
    pass


class BoundedArchive(gzip.GzipFile):
    """Count decompressed bytes before tarfile can collect an extension payload."""

    def __init__(self, archive: Path) -> None:
        super().__init__(filename=archive, mode="rb")
        # Mutable, owned by this extraction alone.
        self._read_bytes: int = 0
        self.last_read: bytes = b""

    @override
    def read(self, size: int | None = -1) -> bytes:
        remaining = MAX_ARCHIVE_BYTES + MAX_ARCHIVE_HEADER_BYTES - self._read_bytes
        requested = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        content = super().read(requested)
        self.last_read = content
        self._read_bytes += len(content)
        if self._read_bytes > MAX_ARCHIVE_BYTES + MAX_ARCHIVE_HEADER_BYTES:
            raise _ArchiveError("the archive exceeds MAX_ARCHIVE_BYTES")
        return content


def _check_size(member: tarfile.TarInfo) -> None:
    if member.size < 0:
        raise _ArchiveError("the archive holds a member with a negative size")
    if not member.isreg() and member.size != 0:
        raise _ArchiveError("the archive holds a non-file member with a nonzero size")


def _unpack_members(tar: tarfile.TarFile, content: BoundedArchive, into: Path) -> None:
    total = 0
    for count, member in enumerate(tar, start=1):
        if count > MAX_ARCHIVE_MEMBERS:
            raise _ArchiveError("the archive exceeds MAX_ARCHIVE_MEMBERS")
        _check_size(member)
        total += member.size
        if total > MAX_ARCHIVE_BYTES:
            raise _ArchiveError("the archive exceeds MAX_ARCHIVE_BYTES")
        refused = _place(tar, member, into)
        if refused is not None:
            raise _ArchiveError(refused)
    if content.last_read.strip(b"\0"):
        raise _ArchiveError("the archive cannot be read as a .tar.gz of a record folder")


def unpack(archive: Path, into: Path) -> Result[Path, str]:
    """Extract one archived record under ``into``; the record folder, or why there is none.

    Member by member, each one checked (:func:`_place`): nothing is ever written
    outside ``into``, and the first member that is not a plain file or folder
    inside it ends the extraction.
    """
    try:
        into.mkdir(parents=True, exist_ok=True)
        # A block-sized stream leaves the terminating header visible: tarfile
        # may silently stop on an invalid header instead of a zero block.
        with (
            BoundedArchive(archive) as content,
            tarfile.open(fileobj=content, mode="r|", bufsize=tarfile.BLOCKSIZE) as tar,
        ):
            _unpack_members(tar, content, into)
    except _ArchiveError as error:
        return Err(str(error))
    except (OSError, EOFError, tarfile.TarError, zlib.error):
        return Err("the archive cannot be read as a .tar.gz of a record folder")
    folders = [path for path in sorted(into.iterdir()) if (path / "manifest.json").is_file()]
    if len(folders) != 1:
        return Err("the archive must hold exactly one record folder")
    return Ok(folders[0])


def replay_path(path: Path, config: Path | None = None) -> Result[ReplayReport, ReplayError]:
    """Replay a record given as its folder or as its archive."""
    if path.is_dir():
        return replay(path, config)
    with TemporaryDirectory(prefix="anheart-replay-") as work:
        unpacked = unpack(path, Path(work))
        if isinstance(unpacked, Err):
            return Err(ReplayError(unpacked.error))
        return replay(unpacked.value, sidecar(path) if config is None else config)


# =========================================================================
# What the gate demands of a library record
# =========================================================================


def is_simulated(manifest: Manifest) -> bool:
    """Whether the record says the simulation made it."""
    return manifest.machine_id == SIMULATION_MACHINE


def identifying(recording: Recording, name: str, folder: str) -> str | None:  # noqa: PLR0911  # one return per field
    """Which field still says who or where, or ``None`` when the record is anonymous.

    ``name`` is the library's name for the record, ``folder`` the name of the
    record folder inside its archive.

    Two closed forms, and nothing in between. A record of the simulation names
    the simulation everywhere. Any other record names nobody: ``anonymized``
    for the machine, the organisation, the operator and every event actor.
    Either way the identifiers are the library's (:func:`library_identity`),
    never the record's own: a real session relabelled ``simulation`` would
    otherwise keep the identifiers its source knows it by. And the folder is
    named after the manifest's ``local_ref``, as the writer names it, so it
    cannot carry an identifier the manifest dropped.
    """
    manifest = recording.manifest
    if manifest.subject_id is not None:
        return "subject_id"
    if manifest.session_id is not None:
        return "session_id"
    simulation = is_simulated(manifest)
    if not simulation and manifest.machine_id != ANONYMIZED:
        return "machine_id"
    if manifest.organization_id != (SIMULATION_ORGANIZATION if simulation else ANONYMIZED):
        return "organization_id"
    operator = OPERATOR if simulation else ANONYMIZED
    if manifest.operator != operator:
        return "operator"
    identity = library_identity(name)
    if manifest.record_id != identity:
        return "record_id"
    if manifest.local_ref != identity[:LOCAL_REF_LENGTH]:
        return "local_ref"
    if any(event.actor not in {SYSTEM, REMOTE, operator} for event in recording.events):
        return "actor"
    if not folder.endswith(f"_{manifest.local_ref}"):
        return "folder"
    return None


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class Accepted:
    """A replay difference somebody examined, and the ticket that says why it stands."""

    ticket: str
    reason: str
    report: Mapping[str, JsonValue]
    """The replay's whole report, as ``--replay <archive> --json`` prints it."""


_ACCEPTED: Final[TypeAdapter[Accepted]] = TypeAdapter(Accepted)
_ACCEPTABLE: Final[frozenset[str]] = frozenset({Outcome.DIFFERENCE.value, Outcome.DIVERGENCE.value})


def accepted_for(archive: Path) -> Result[Accepted | None, str]:
    """The accepted difference standing next to ``archive``, ``None`` if there is none."""
    sidecar = archive.with_name(name_of(archive) + ACCEPTED_SUFFIX)
    if not sidecar.exists():
        return Ok(None)
    try:
        accepted = _ACCEPTED.validate_json(sidecar.read_bytes())
    except (OSError, ValidationError):
        return Err(f"{sidecar.name} is not an accepted-difference file")
    outcome = accepted.report.get("outcome")
    if not isinstance(outcome, str) or outcome not in _ACCEPTABLE:
        return Err(f"{sidecar.name} holds a report that is neither a difference nor a divergence")
    return Ok(accepted)


def found_at(report: ReplayReport) -> float | None:
    """The instant a report points at: the divergence, else the first difference."""
    if report.divergence is not None:
        return report.divergence.t
    first = report.comparison.first
    return None if first is None else first.t


def refusal(
    report: ReplayReport, accepted: Accepted | None, *, simulated: bool = False
) -> str | None:
    """Why the gate refuses this replay, or ``None`` when it is what the library says.

    An accepted difference is the WHOLE report, compared whole: the first
    difference with its kind and its tick, the count of every kind of failed
    check, what was tolerated, the divergence with what was asked and what the
    record holds. Comparing only "a difference, at that instant" would let a
    second, unrelated difference ride in behind the accepted one.
    """
    where = found_at(report)
    if accepted is None:
        if where is None:
            return None
        return f"unexplained {report.outcome.value} at t={where:.3f} s"
    if simulated:
        return (
            "a record of the simulation is exported again, never excused: remove its accepted file"
        )
    if where is None:
        return "the accepted difference no longer shows: remove its accepted file"
    found = mapping(JSON.validate_json(report.to_json()))
    parts = sorted(
        key for key in {*found, *accepted.report} if found.get(key) != accepted.report.get(key)
    )
    if parts:
        return f"the replay is not the accepted one: it differs in {', '.join(parts)}"
    return None


def judge(archive: Path, work: Path) -> str | None:  # noqa: PLR0911  # one return per demand
    """Why the gate refuses the library record ``archive``, or ``None`` when it stands.

    ``work`` is a directory of the caller's the archive is extracted into. In
    order: an archive of one record; closed and intact (every member listed in
    the checksums, and matching); anonymous; then replayed, and the replay is
    what the library says it is.
    """
    unpacked = unpack(archive, work)
    if isinstance(unpacked, Err):
        return unpacked.error
    loaded = read(unpacked.value)
    if isinstance(loaded, Err):
        return f"the record cannot be read ({loaded.error.detail})"
    recording = loaded.value
    if recording.warnings:
        flaw = recording.warnings[0]
        return f"the record is not closed and intact: {integrity_line(flaw.file, flaw.code)}"
    if recording.manifest.ended_at is None:
        return "the record is not closed: its manifest has no end"
    names = identifying(recording, name_of(archive), unpacked.value.name)
    if names is not None:
        return f"the record is not anonymous: {names}"
    accepted = accepted_for(archive)
    if isinstance(accepted, Err):
        return accepted.error
    replayed = asyncio.run(replay_recording(recording, sidecar(archive)))
    if isinstance(replayed, Err):
        return f"the record is not replayable: {replayed.error.detail}"
    refused = refusal(replayed.value, accepted.value, simulated=is_simulated(recording.manifest))
    return None if refused is None else f"{refused}\n{replayed.value.to_text()}"


# =========================================================================
# Records made by the simulation
# =========================================================================


def simulated(directory: Path = REAL_DIR, scenarios: Path = SCENARIO_DIR) -> tuple[str, ...]:
    """The library records that are battery scenarios, by name: the ones :func:`export` remakes."""
    battery = {path.stem for path in scenario_paths(scenarios)}
    return tuple(name for name in map(name_of, archives(directory)) if name in battery)


@dataclass(frozen=True, slots=True)
class Exported:
    """Where an exported record stands, and whether its archive had to be written."""

    archive: Path
    rewritten: bool


def _same(record: Path, archive: Path, work: Path) -> bool:
    """Whether ``archive`` already holds the record folder ``record``, content for content.

    Read, not compared byte for byte: two zlib builds compress the same blocks
    into different bytes, and that is not a change worth a commit.
    """
    if not archive.exists():
        return False
    unpacked = unpack(archive, work)
    if isinstance(unpacked, Err):
        return False
    held, made = read(unpacked.value), read(record)
    return isinstance(held, Ok) and isinstance(made, Ok) and held.value == made.value


def export(  # noqa: PLR0911  # one return per refusal
    name: str, directory: Path = REAL_DIR, scenarios: Path = SCENARIO_DIR
) -> Result[Exported, str]:
    """Run the battery scenario ``name``, and put its record in the library.

    The record is anonymous (no ``subject_id``), and it is the same record every
    time: its identifiers come from the name (:func:`library_identity`), its
    dates from the harness clock, its version from nowhere. It is replayed
    before it is kept: an export that does not replay green is not written.
    And an archive that already holds exactly this record is left alone, so
    that remaking the library costs the repository only what really changed.
    """
    if SCENARIO_NAME.fullmatch(name) is None:
        return Err("a scenario is named by letters, digits and underscores, not by a path")
    scenario = resolve_under(scenarios, f"{name}.json")
    archive = resolve_under(directory, f"{name}{ARCHIVE_SUFFIX}")
    if scenario is None or archive is None:
        return Err(f"{name} would be read or written through a link that leaves its directory")
    loaded = load_scenario(scenario)
    if isinstance(loaded, Err):
        return Err(f"{name} is not a scenario of the battery")
    trace = asyncio.run(run_scenario(loaded.value)).trace
    identity = library_identity(name)
    manifest = replace(
        trace.manifest,
        subject_id=None,
        record_id=identity,
        local_ref=identity[:LOCAL_REF_LENGTH],
        software_version=UNVERSIONED,
    )
    with TemporaryDirectory(prefix="anheart-export-") as work:
        record = replace(trace, manifest=manifest).write_record(Path(work) / "made", Privacy())
        report = replay(record)
        if isinstance(report, Err):
            return Err(f"{name} is not replayable: {report.error.detail}")
        if not report.value.matches:
            return Err(f"the record of {name} does not replay green:\n{report.value.to_text()}")
        if _same(record, archive, Path(work) / "held"):
            return Ok(Exported(archive, rewritten=False))
        pack(record, archive)
    return Ok(Exported(archive, rewritten=True))
