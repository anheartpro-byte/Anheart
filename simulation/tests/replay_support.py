"""Short recorded sessions for the replay tests, and the tools to alter a copy of one.

Each record is made by the real harness (real runtime, real DSP) and written by
the shared writer. A test never alters the record a fixture built: it takes a
copy under its own ``tmp_path`` and edits that.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from simulation.real_records import ANONYMIZED, LOCAL_REF_LENGTH, library_identity
from simulation.scenario import SCENARIO_DIR, Action, EcgMode, Scenario
from simulation.tests.conftest import document, load, run
from src.record.codec import Privacy
from src.units import Seconds

type Cells = list[str]
type Line = dict[str, object]


def short(
    name: str, *, duration: float, actions: tuple[Action, ...] = (), preroll: float = 10.0
) -> Scenario:
    """A battery scenario cut short, on the REAL ECG path (the only one a record can replay)."""
    given = load(SCENARIO_DIR / f"{name}.json")
    return replace(
        given,
        ecg=replace(given.ecg, mode=EcgMode.DSP),
        actions=actions,
        duration=Seconds(duration),
        preroll=Seconds(preroll),
        teardown=Seconds(0.0),
    )


def record(scenario: Scenario, root: Path) -> Path:
    """Run ``scenario`` and write its schema-2 record under ``root``."""
    return run(scenario).trace.write_record(root, Privacy())


def copy(source: Path, into: Path) -> Path:
    """A private copy of a record folder, free to alter."""
    target = into / source.name
    shutil.copytree(source, target)
    return target


def edit_ticks(folder: Path, change: Callable[[Cells, list[Cells]], list[Cells]]) -> None:
    """Rewrite ``ticks.csv``: ``change(header, rows)`` returns the rows to keep."""
    path = folder / "ticks.csv"
    table = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    header, rows = table[0], table[1:]
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(change(header, rows))
    path.write_text(stream.getvalue(), encoding="utf-8")


def edit_lines(folder: Path, name: str, change: Callable[[list[Line]], list[Line]]) -> None:
    """Rewrite one JSON Lines stream of a record through ``change``."""
    path = folder / name
    lines = [dict(document(line)) for line in path.read_text(encoding="utf-8").splitlines()]
    path.write_text(
        "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in change(lines)),
        encoding="utf-8",
    )


def edit_manifest(folder: Path, change: Callable[[Line], None]) -> None:
    path = folder / "manifest.json"
    manifest = dict(document(path.read_text(encoding="utf-8")))
    change(manifest)
    path.write_text(json.dumps(manifest, separators=(",", ":")) + "\n", encoding="utf-8")


def at(line: Mapping[str, object]) -> float:
    """The instant of one JSON line of a record."""
    value = line["t"]
    assert isinstance(value, int | float)
    return float(value)


def tick_at(header: Cells, rows: list[Cells], t: float) -> Cells:
    """The row of the tick recorded at ``t`` (to the millisecond)."""
    column = header.index("t")
    (found,) = [row for row in rows if abs(float(row[column]) - t) < 1e-9]
    return found


def reseal(folder: Path) -> None:
    """Write ``checksums.sha256`` again, as the writer does when it closes a record.

    For a test that stands for "the same session, as another runtime would
    have recorded it": the record is whole and closed, only its content differs.
    """
    index = folder / "checksums.sha256"
    index.unlink(missing_ok=True)
    members = sorted(path for path in folder.rglob("*") if path.is_file())
    index.write_text(
        "".join(
            f"{hashlib.sha256(member.read_bytes()).hexdigest()}  "
            f"{member.relative_to(folder).as_posix()}\n"
            for member in members
        ),
        encoding="utf-8",
    )


def as_library(folder: Path, name: str) -> Path:
    """A simulated record folder, as :func:`simulation.real_records.export` leaves it.

    No passenger pseudonym, the library's identifiers for ``name``, the folder
    renamed after them, the checksums written again.
    """
    identity = library_identity(name)
    local_ref = identity[:LOCAL_REF_LENGTH]

    def identify(manifest: Line) -> None:
        manifest.update(subject_id=None, record_id=identity, local_ref=local_ref)

    edit_manifest(folder, identify)
    reseal(folder)
    stamp = folder.name.partition("_")[0]
    return folder.rename(folder.with_name(f"{stamp}_{local_ref}"))


def as_real(folder: Path, name: str) -> Path:
    """A simulated record folder, rewritten as the anonymous form of a real session ``name``.

    Nothing in the library is a real session yet. This is what one looks like
    once anonymised: nobody named in the manifest or the events, the library's
    identifiers, the folder renamed after them, the checksums written again.
    """
    identity = library_identity(name)
    local_ref = identity[:LOCAL_REF_LENGTH]

    def anonymise(manifest: Line) -> None:
        manifest.update(
            machine_id=ANONYMIZED,
            organization_id=ANONYMIZED,
            operator=ANONYMIZED,
            subject_id=None,
            record_id=identity,
            local_ref=local_ref,
        )

    def rename(lines: list[Line]) -> list[Line]:
        for line in lines:
            if line["actor"] not in {"system", "remote"}:
                line["actor"] = ANONYMIZED
        return lines

    edit_manifest(folder, anonymise)
    edit_lines(folder, "events.jsonl", rename)
    reseal(folder)
    stamp = folder.name.partition("_")[0]
    return folder.rename(folder.with_name(f"{stamp}_{local_ref}"))
