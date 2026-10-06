"""Short recorded sessions for the replay tests, and the tools to alter a copy of one.

Each record is made by the real harness (real runtime, real DSP) and written by
the shared writer. A test never alters the record a fixture built: it takes a
copy under its own ``tmp_path`` and edits that.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

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
