"""The library of recorded sessions the gate replays: ``simulation/scenarios/real/``.

One file per session: ``<name>.tar.gz``, the archive of one closed schema-2
record folder (the form a record leaves the console in). A session holds
thousands of raw ECG blocks, so the folder itself is not committed.

Every archive in the directory is replayed by ``tests/test_real_records.py``,
and must replay green: that is what makes a recorded session a non-regression
scenario. Two things can stand next to an archive:

* nothing: the replay must match;
* ``<name>.accepted.json``: a difference somebody examined and accepted, with
  the ticket that says why. The replay must then show THAT difference, at that
  instant, and no other; the day it no longer shows, the file must go.

A record made by the simulation is never patched that way: it is exported again
(:func:`export`). A real session cannot be recorded again, which is what the
accepted file is for. ``docs/framework-de-test.md`` has the procedure.
"""

from __future__ import annotations

import asyncio
import gzip
import tarfile
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Literal

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from simulation.harness import run_scenario
from simulation.replay import ReplayError, replay
from simulation.replay_report import Outcome, ReplayReport
from simulation.scenario import SCENARIO_DIR, load_scenario, scenario_paths
from src.record.codec import Privacy
from src.record.schema import Manifest
from src.result import Err, Ok, Result

REAL_DIR: Final[Path] = SCENARIO_DIR / "real"
ARCHIVE_SUFFIX: Final[str] = ".tar.gz"
ACCEPTED_SUFFIX: Final[str] = ".accepted.json"

ANONYMOUS_ORGANIZATIONS: Final[frozenset[str]] = frozenset({"synthetic", "anonymized"})
"""What ``organization_id`` may be in the library: the simulation's, or the anonymiser's."""

INSTANT_TOLERANCE: Final[float] = 1e-3
"""An accepted difference names its instant to the millisecond, as the record does."""


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


def unpack(archive: Path, into: Path) -> Result[Path, str]:
    """Extract one archived record under ``into``; the record folder, or why there is none."""
    try:
        into.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, mode="r:gz") as tar:
            tar.extractall(into, filter="data")
    except (OSError, EOFError, tarfile.TarError):
        return Err("the archive cannot be read as a .tar.gz of a record folder")
    folders = [path for path in sorted(into.iterdir()) if (path / "manifest.json").is_file()]
    if len(folders) != 1:
        return Err("the archive must hold exactly one record folder")
    return Ok(folders[0])


def replay_path(path: Path) -> Result[ReplayReport, ReplayError]:
    """Replay a record given as its folder or as its archive."""
    if path.is_dir():
        return replay(path)
    with TemporaryDirectory(prefix="anheart-replay-") as work:
        unpacked = unpack(path, Path(work))
        if isinstance(unpacked, Err):
            return Err(ReplayError(unpacked.error))
        return replay(unpacked.value)


# =========================================================================
# What the gate demands of a library record
# =========================================================================


def identifying(manifest: Manifest) -> str | None:
    """Which field still says who or where, or ``None`` when the manifest is anonymous."""
    if manifest.subject_id is not None:
        return "subject_id"
    if manifest.session_id is not None:
        return "session_id"
    if manifest.organization_id not in ANONYMOUS_ORGANIZATIONS:
        return "organization_id"
    return None


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", allow_inf_nan=False))
class Accepted:
    """A replay difference somebody examined, and the ticket that says why it stands."""

    ticket: str
    reason: str
    outcome: Literal["difference", "divergence"]
    t: float
    """The instant of the first difference, or of the divergence, in seconds."""


_ACCEPTED: Final[TypeAdapter[Accepted]] = TypeAdapter(Accepted)


def accepted_for(archive: Path) -> Result[Accepted | None, str]:
    """The accepted difference standing next to ``archive``, ``None`` if there is none."""
    sidecar = archive.with_name(name_of(archive) + ACCEPTED_SUFFIX)
    if not sidecar.exists():
        return Ok(None)
    try:
        return Ok(_ACCEPTED.validate_json(sidecar.read_bytes()))
    except (OSError, ValidationError):
        return Err(f"{sidecar.name} is not an accepted-difference file")


def found_at(report: ReplayReport) -> float | None:
    """The instant a report points at: the divergence, else the first difference."""
    if report.divergence is not None:
        return report.divergence.t
    first = report.comparison.first
    return None if first is None else first.t


def refusal(report: ReplayReport, accepted: Accepted | None) -> str | None:
    """Why the gate refuses this replay, or ``None`` when it is what the library says."""
    where = found_at(report)
    if accepted is None:
        if where is None:
            return None
        return f"unexplained {report.outcome.value} at t={where:.3f} s"
    if where is None:
        return "the accepted difference no longer shows: remove its accepted file"
    if report.outcome is not Outcome(accepted.outcome):
        return f"a {report.outcome.value} where a {accepted.outcome} was accepted"
    if abs(where - accepted.t) > INSTANT_TOLERANCE:
        return f"{report.outcome.value} at t={where:.3f} s, accepted at t={accepted.t:.3f} s"
    return None


# =========================================================================
# Records made by the simulation
# =========================================================================


def simulated(directory: Path = REAL_DIR, scenarios: Path = SCENARIO_DIR) -> tuple[str, ...]:
    """The library records that are battery scenarios, by name: the ones :func:`export` remakes."""
    battery = {path.stem for path in scenario_paths(scenarios)}
    return tuple(name for name in map(name_of, archives(directory)) if name in battery)


def export(
    name: str, directory: Path = REAL_DIR, scenarios: Path = SCENARIO_DIR
) -> Result[Path, str]:
    """Run the battery scenario ``name``, and put its record in the library.

    The record is anonymous (no ``subject_id``) and is replayed before it is
    kept: an export that does not replay green is not written.
    """
    loaded = load_scenario(scenarios / f"{name}.json")
    if isinstance(loaded, Err):
        return Err(f"{name} is not a scenario of the battery")
    trace = asyncio.run(run_scenario(loaded.value)).trace
    anonymous = replace(trace, manifest=replace(trace.manifest, subject_id=None))
    with TemporaryDirectory(prefix="anheart-export-") as work:
        record = anonymous.write_record(Path(work), Privacy())
        report = replay(record)
        if isinstance(report, Err):
            return Err(f"{name} is not replayable: {report.error.detail}")
        if not report.value.matches:
            return Err(f"the record of {name} does not replay green:\n{report.value.to_text()}")
        archive = directory / f"{name}{ARCHIVE_SUFFIX}"
        pack(record, archive)
    return Ok(archive)
