"""ANH-128 EX-9: the simulation and the console write the SAME record, by the same library.

One record comes from the harness (the first seconds of a battery scenario,
written through ``Trace.write_record``), the other from the REAL console built by
:func:`src.local_panel.build_panel` with ``MOTOR_BACKEND=sim ECG_SOURCE=sim``
and its session journal. They are compared as files, not as parsed objects:
the same entries in the directory, the same keys in the manifest (and in its
nested objects), the same columns in both CSV files, the same keys on every
event line and in every raw block header.
"""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import document, load, obj, run
from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import EcgFrame, Treatment, treat_ecg
from src.local_config import load_local_config
from src.local_panel import LocalPanel, build_panel
from src.record.codec import Privacy
from src.record.journal import Journal
from src.record.reader import read
from src.record.schema import EventKind
from src.result import Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, Seconds, UnixMillis

OPERATOR = "sim operator"
TICK = Seconds(0.2)

ENV: Mapping[str, str] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "MOTOR_MAX_RPM": "1380",
    "PROGRAMS_ENABLED": "true",
    "OCCUPANCY_OCCUPIED_ENABLED": "true",
}

ENTRIES = [
    "checksums.sha256",
    "drive_frames.jsonl",
    "ecg_raw",
    "events.jsonl",
    "manifest.json",
    "sensors.csv",
    "ticks.csv",
]


async def _inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    return treat_ecg(treatment, batch)


async def _ticks(panel: LocalPanel, journal: Journal, clock: ManualClock, seconds: float) -> None:
    for index in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        if index % 5 == 0:
            await panel.sensor_step()
        await panel.control_step()
        journal.drain()


def _state(panel: LocalPanel) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return panel.runtime.state


async def _console_record(tmp_path: Path, *, programme: bool) -> Path:
    """One session of the real console in simulation, recorded and closed."""
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok), loaded
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    panel = build_panel(
        loaded.value,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=_inline,
        journal=journal,
    )
    surface = panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    if programme:
        started = surface.submit_start(
            profile_id="standard_30_min",
            operator=OPERATOR,
            total_duration_s=None,
            subject_age=30,
        )
        assert isinstance(started, Ok), started
        await _ticks(panel, journal, clock, 8.0)
    else:
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        await _ticks(panel, journal, clock, 1.0)
        assert isinstance(
            surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR), Ok
        )
        await _ticks(panel, journal, clock, 12.0)
    assert _state(panel) is RuntimeState.RUNNING, surface.run_state
    if not programme:
        # The operator's stop, to the end. (A programme's cooldown and monitored
        # recovery take minutes: that one is closed by the console's exit instead.)
        assert isinstance(surface.submit_end(operator=OPERATOR, reason="done"), Ok)
        await _ticks(panel, journal, clock, 60.0)
        assert _state(panel) is RuntimeState.FINISHED
    await panel.close()
    records = [path for path in journal.root.iterdir() if not path.name.startswith(".")]
    assert len(records) == 1
    return records[0]


def _simulation_record(tmp_path: Path, name: str, seconds: float) -> Path:
    """The first ``seconds`` of a battery scenario, run here and written by the harness.

    Shortened on purpose: the structure of a record does not depend on how long
    the session lasted, and the full signal-processing scenarios take minutes.
    """
    whole = load(SCENARIO_DIR / name)
    scenario = replace(
        whole,
        duration=Seconds(seconds),
        actions=tuple(action for action in whole.actions if action.at <= seconds),
    )
    return run(scenario).trace.write_record(tmp_path / "simulation", Privacy())


def _manifest(record: Path) -> Mapping[str, object]:
    return document((record / "manifest.json").read_text(encoding="utf-8"))


def _keys(value: object) -> list[str]:
    return sorted(obj(value))


def _header(path: Path) -> str:
    return path.read_text(encoding="utf-8").splitlines()[0]


def _line_keys(path: Path) -> set[tuple[str, ...]]:
    return {tuple(sorted(document(line))) for line in path.read_text(encoding="utf-8").splitlines()}


def _block_header_keys(record: Path) -> set[tuple[str, ...]]:
    return {
        tuple(sorted(document(gzip.decompress(block.read_bytes()).split(b"\n", 1)[0].decode())))
        for block in (record / "ecg_raw").iterdir()
    }


def _assert_same_structure(console: Path, simulation: Path) -> None:
    assert sorted(path.name for path in console.iterdir()) == ENTRIES
    assert sorted(path.name for path in simulation.iterdir()) == ENTRIES

    ours, theirs = _manifest(console), _manifest(simulation)
    assert list(ours) == list(theirs), "the manifest keys, in the format's order"
    assert _keys(ours["clocks"]) == _keys(theirs["clocks"])
    assert _keys(ours["end_observation"]) == _keys(theirs["end_observation"])
    assert ours["schema_version"] == theirs["schema_version"] == 2
    assert ours["contract_version"] == theirs["contract_version"]

    assert _header(console / "ticks.csv") == _header(simulation / "ticks.csv")
    assert _header(console / "sensors.csv") == _header(simulation / "sensors.csv")
    event_keys = {("actor", "detail", "kind", "t")}
    assert _line_keys(console / "events.jsonl") == event_keys
    assert _line_keys(simulation / "events.jsonl") == event_keys

    for record in (console, simulation):
        loaded = read(record)
        assert isinstance(loaded, Ok), loaded
        assert loaded.value.warnings == (), "closed, checksummed, complete"
        assert loaded.value.rows
        assert loaded.value.events[-1].kind is EventKind.END


def test_ex9_a_manual_session_has_the_same_structure_on_the_console_and_in_simulation(
    tmp_path: Path,
) -> None:
    console = asyncio.run(_console_record(tmp_path, programme=False))
    simulation = _simulation_record(tmp_path, "manual_27_rpm.json", 30.0)
    _assert_same_structure(console, simulation)
    assert _manifest(console)["profile"] is None
    assert _manifest(simulation)["profile"] is None
    assert (_manifest(console)["kind"], _manifest(simulation)["kind"]) == ("manual", "manual")


def test_ex9_a_programme_has_the_same_profile_keys_and_raw_block_headers(tmp_path: Path) -> None:
    console = asyncio.run(_console_record(tmp_path, programme=True))
    simulation = _simulation_record(tmp_path, "auto_jog_150_dsp.json", 12.0)
    _assert_same_structure(console, simulation)
    ours, theirs = _manifest(console), _manifest(simulation)
    assert (ours["kind"], theirs["kind"]) == ("auto", "auto")
    assert _keys(ours["profile"]) == _keys(theirs["profile"])
    block_keys = {("channels", "n_samples", "sample_rate", "seq", "t_first")}
    assert _block_header_keys(console) == block_keys
    assert _block_header_keys(simulation) == block_keys
    console_rows = read(console)
    assert isinstance(console_rows, Ok)
    assert console_rows.value.sensors, "the console wrote the 1 Hz sensor stream too"
