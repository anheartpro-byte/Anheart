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
import hashlib
from collections.abc import Mapping
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest
from tests.roster_support import RIDER_ID, listed

from simulation import replay_config
from simulation import run as cli
from simulation.real_records import judge, pack, replay_path
from simulation.recording import FrameKind
from simulation.replay import replay
from simulation.replay_config import sidecar
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import document, load, obj, run
from simulation.tests.replay_support import as_real
from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.contract import UNKNOWN_SOFTWARE_VERSION, read_software_version
from src.ecg_pipeline import EcgFrame, Treatment, treat_ecg
from src.local_config import load_local_config
from src.local_panel import LocalPanel, build_panel
from src.medical import shipped_status
from src.record import commands
from src.record.codec import Privacy
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
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
        roster=listed(clock),
    )
    surface = panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    await _ticks(panel, journal, clock, 12.0)
    if programme:
        started = surface.submit_start(
            profile_id="standard_30_min",
            operator=OPERATOR,
            total_duration_s=None,
            subject_id=RIDER_ID,
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
    found = records(journal.root)
    assert len(found) == 1
    return found[0]


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


@pytest.mark.parametrize("programme", [False, True], ids=["manual", "programme"])
def test_console_sessions_replay_exactly_with_the_applied_configuration(
    tmp_path: Path,
    programme: bool,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ("programme" if programme else "manual")
    home.mkdir()
    folder = asyncio.run(_console_record(home, programme=programme))
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    record = loaded.value
    inputs = [event for event in record.events if event.kind in commands.INPUT_KINDS]
    assert inputs
    assert all(isinstance(commands.parse(event.detail), Ok) for event in inputs)
    start = next(event for event in inputs if event.detail.startswith("start_"))
    assert start.t == pytest.approx(0.2)
    idle = [row for row in record.rows if row.t <= 0]
    assert len(idle) >= 40
    assert idle[-1].t == 0
    assert all(row.state == "idle" for row in idle)
    idle_frames = [frame for frame in record.frames if frame.t <= 0]
    assert idle_frames[0].t == idle[0].t
    assert {frame.kind for frame in idle_frames} == {"open", "read_status"}
    reads = [frame for frame in idle_frames if frame.kind == "read_status"]
    assert len(reads) >= 16
    assert all(any(abs(frame.t - row.t) < 0.001 for row in idle) for frame in reads)
    assert all(later.t - earlier.t <= 0.601 for earlier, later in pairwise(reads))
    raw = [block for block in record.raw if block.header.t_first < 0]
    assert sum(block.header.n_samples for block in raw) >= 8000
    assert all(block.header.t_received is not None for block in record.raw)
    assert all(block.header.runtime_delivery is not None for block in record.raw)
    config = folder.parent / "config" / (record.manifest.config_hash + ".json")
    replayed = replay(folder, config)
    assert isinstance(replayed, Ok), replayed
    report = replayed.value
    assert report.matches, report.to_text()
    assert report.comparison.deviating_ticks == 0
    assert report.comparison.tolerated_rpm == (0, 0)
    assert report.comparison.shifted_transitions == (0, 0)
    assert report.configuration.config_hash_verified
    assert report.configuration.current_configuration_differs is False
    assert report.configuration.differing_parts == ()
    assert "config_hash verified; current repository differences: none" in report.to_text()
    assert any(event.detail == "attendant_ping" for event in inputs)
    if not programme:
        assert any(event.detail.startswith("manual_target") for event in inputs)
        assert any(event.detail == "stop" for event in inputs)
    with capsys.disabled():
        print(report.to_json(), end="")  # noqa: T201  # retained cross-test evidence
    assert cli.main(["--replay", str(folder), "--config", str(config), "--json"]) == 0
    assert '"config_hash_verified":true' in capsys.readouterr().out
    sidecar(folder).write_bytes(config.read_bytes())
    archive = tmp_path / "console.tar.gz"
    pack(folder, archive)
    sidecar(archive).write_bytes(config.read_bytes())
    if programme:
        archived = replay_path(archive)
        assert isinstance(archived, Ok), archived
        assert archived.value.to_json() == report.to_json()
    else:
        adjacent = replay(folder)
        assert isinstance(adjacent, Ok), adjacent
        assert adjacent.value.to_json() == report.to_json()
    sidecar(folder).unlink()
    assert cli.main(["--replay", str(folder), "--json"]) == 2
    absent = capsys.readouterr().err
    assert "configuration document is absent" in absent
    with capsys.disabled():
        print(absent, end="")  # noqa: T201  # retained refusal evidence
    changed = tmp_path / "changed.json"
    changed.write_bytes(config.read_bytes() + b" ")
    assert cli.main(["--replay", str(archive), "--config", str(changed)]) == 2
    refused = capsys.readouterr().err
    assert "configuration hash mismatch" in refused
    assert record.manifest.config_hash[:16] in refused
    assert hashlib.sha256(changed.read_bytes()).hexdigest()[:16] in refused
    with capsys.disabled():
        print(refused, end="")  # noqa: T201  # retained refusal evidence
    if programme:
        current = shipped_status()
        different = replace(current, document=replace(current.document, sha256="0" * 64))
        monkeypatch.setattr(replay_config, "shipped_status", lambda: different)
        result = replay(folder, config)
        assert isinstance(result, Ok), result
        assert result.value.matches, result.value.to_text()
        assert result.value.comparison.deviating_ticks == 0
        assert result.value.configuration.current_configuration_differs is True
        assert result.value.configuration.differing_parts == ("medical_file",)
        assert "current repository differences: medical_file" in result.value.to_text()
        with capsys.disabled():
            print(result.value.to_json(), end="")  # noqa: T201  # retained cross-test evidence
        monkeypatch.undo()
        anonymous = as_real(folder, "console")
        pack(anonymous, archive)
        assert judge(archive, tmp_path / "judged") is None


type _Shape = tuple[int | tuple[int, ...] | None, int]
"""The registers a frame names, and how many values it carries."""


def _frame_shapes(record: Path) -> dict[str, set[_Shape]]:
    """Per kind of drive frame in ``record``: every shape it was written with."""
    loaded = read(record)
    assert isinstance(loaded, Ok), loaded
    seen: dict[str, set[_Shape]] = {}
    for frame in loaded.value.frames:
        width = len(frame.value) if isinstance(frame.value, tuple) else 1
        seen.setdefault(frame.kind, set()).add((frame.register, width))
    return seen


def test_anh191_ex2_the_console_writes_the_drive_calls_the_harness_writes(tmp_path: Path) -> None:
    """The same file, by two producers: same keys on every line, same vocabulary of calls.

    The console's tap (``src.record.drive_tap``) and the harness's
    ``RecordingDrive`` are two pieces of code; this is what keeps them one format.
    """
    console = asyncio.run(_console_record(tmp_path, programme=False))
    simulation = _simulation_record(tmp_path, "manual_27_rpm.json", 30.0)
    keys = {("kind", "latency_ms", "ok", "raw_hex", "register", "t", "value")}
    assert _line_keys(console / "drive_frames.jsonl") == keys
    assert _line_keys(simulation / "drive_frames.jsonl") == keys

    ours, theirs = _frame_shapes(console), _frame_shapes(simulation)
    assert set(ours) <= {kind.value for kind in FrameKind}
    shared = set(ours) & set(theirs)
    assert {"read_status", "speed", "command"} <= shared
    for kind in shared:
        assert ours[kind] == theirs[kind], kind


def test_ex9_a_manual_session_has_the_same_structure_on_the_console_and_in_simulation(
    tmp_path: Path,
) -> None:
    console = asyncio.run(_console_record(tmp_path, programme=False))
    simulation = _simulation_record(tmp_path, "manual_27_rpm.json", 30.0)
    _assert_same_structure(console, simulation)
    assert _manifest(console)["profile"] is None
    assert _manifest(simulation)["profile"] is None
    assert (_manifest(console)["kind"], _manifest(simulation)["kind"]) == ("manual", "manual")
    for record in (console, simulation):
        assert _manifest(record)["subject_fit"] is None
        assert _manifest(record)["fitness_questionnaire_version"] is None


def test_anh210_a_scenario_record_is_stamped_with_the_version_of_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``software_version`` is ``raspberry-pi/VERSION``, as on the console, in any environment.

    The harness used to read ``ANHEART_SOFTWARE_VERSION`` and to write
    ``unversioned`` without it. A value set for it now changes nothing. (The
    console's side of this is ``raspberry-pi/tests/test_link_indicator.py``.)
    """
    monkeypatch.setenv("ANHEART_SOFTWARE_VERSION", "set-by-hand")
    built = read_software_version()
    assert built != UNKNOWN_SOFTWARE_VERSION
    simulation = _simulation_record(tmp_path, "manual_27_rpm.json", 5.0)
    assert _manifest(simulation)["software_version"] == built


def test_anh163_a_scenario_record_does_not_judge_a_wall_clock_and_older_records_still_read(
    tmp_path: Path,
) -> None:
    """``clocks.wall_trusted`` follows the rule of the fields added to schema 2.

    The writer always emits it; the simulation, whose clock is deterministic,
    writes ``null`` (it judges no wall clock). A record written before the
    field existed has no such key: it is read as before, and says ``None``.
    (What the console writes there is ``raspberry-pi/tests/test_wall_clock_console.py``.)
    """
    simulation = _simulation_record(tmp_path, "manual_27_rpm.json", 5.0)
    clocks = obj(_manifest(simulation)["clocks"])
    assert "wall_trusted" in clocks
    assert clocks["wall_trusted"] is None
    assert clocks["ntp_offset_s"] is None
    written = read(simulation)
    assert isinstance(written, Ok)
    assert written.value.manifest.clocks.wall_trusted is None
    assert written.value.warnings == ()

    # The same record as a writer of before this field left it.
    manifest = simulation / "manifest.json"
    today = manifest.read_text(encoding="utf-8")
    before = today.replace(',"wall_trusted":null', "")
    assert before != today
    assert "wall_trusted" not in before
    manifest.write_text(before, encoding="utf-8")
    older = read(simulation)
    assert isinstance(older, Ok), older
    assert older.value.manifest.clocks.wall_trusted is None
    assert older.value.manifest.clocks.utc_start == written.value.manifest.clocks.utc_start
    assert len(older.value.rows) == len(written.value.rows)


def test_ex9_a_programme_has_the_same_profile_keys_and_raw_block_headers(tmp_path: Path) -> None:
    console = asyncio.run(_console_record(tmp_path, programme=True))
    simulation = _simulation_record(tmp_path, "auto_jog_150_dsp.json", 12.0)
    _assert_same_structure(console, simulation)
    ours, theirs = _manifest(console), _manifest(simulation)
    assert (ours["kind"], theirs["kind"]) == ("auto", "auto")
    assert ours["subject_fit"] is True
    assert ours["fitness_questionnaire_version"] == "0.1.0"
    assert theirs["subject_fit"] is None
    assert theirs["fitness_questionnaire_version"] is None
    assert _keys(ours["profile"]) == _keys(theirs["profile"])
    block_keys = ("channels", "n_samples", "sample_rate", "seq", "t_first")
    assert _block_header_keys(console) == {
        tuple(sorted((*block_keys, "runtime_delivery", "t_received")))
    }
    assert _block_header_keys(simulation) == {(*block_keys, "t_received")}
    console_rows = read(console)
    assert isinstance(console_rows, Ok)
    assert console_rows.value.sensors, "the console wrote the 1 Hz sensor stream too"
