"""EX-5: the library of recorded sessions, and the gate that replays every one of them.

The first test of this file is the gate itself: it is parametrised over what is
ON DISK in ``simulation/scenarios/real/``, so an archive added to the library is
replayed by the next run, and cannot be merged unless its replay is the one the
library declares.
"""

from __future__ import annotations

import asyncio
import json
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from simulation import real_records as library
from simulation import run as cli
from simulation.real_records import (
    Accepted,
    accepted_for,
    archives,
    export,
    found_at,
    identifying,
    name_of,
    pack,
    refusal,
    replay_path,
    simulated,
    unpack,
)
from simulation.replay import ReplayError, replay, replay_recording
from simulation.replay_compare import Decision, SafetyDecision, compare_decisions
from simulation.replay_report import Divergence, Outcome, ReplayReport
from simulation.tests.replay_support import record, short
from src.record.reader import read
from src.result import Err, Ok
from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, Seconds

LIBRARY = archives()

# =========================================================================
# The gate
# =========================================================================


def test_ex5_the_library_holds_at_least_three_scenarios_of_the_battery() -> None:
    assert len(simulated()) >= 3
    assert [name_of(archive) for archive in LIBRARY] == sorted(name_of(a) for a in LIBRARY)


@pytest.mark.parametrize("archive", LIBRARY, ids=name_of)
def test_ex5_a_library_record_is_closed_anonymous_and_replays_as_the_library_says(
    archive: Path, tmp_path: Path
) -> None:
    unpacked = unpack(archive, tmp_path)
    assert isinstance(unpacked, Ok), unpacked
    loaded = read(unpacked.value)
    assert isinstance(loaded, Ok), loaded
    recording = loaded.value
    # Closed and unaltered: every member is listed in the checksums and matches.
    assert recording.warnings == ()
    assert recording.manifest.ended_at is not None
    # No passenger, no session, no organisation.
    assert identifying(recording.manifest) is None
    accepted = accepted_for(archive)
    assert isinstance(accepted, Ok), accepted
    report = asyncio.run(replay_recording(recording))
    assert isinstance(report, Ok), report
    assert refusal(report.value, accepted.value) is None, report.value.to_text()


# =========================================================================
# Archives
# =========================================================================


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One short recorded session, as a folder."""
    return record(
        short("manual_27_rpm", duration=1.0, preroll=0.4), tmp_path_factory.mktemp("session")
    )


def test_ex5_a_record_survives_its_archive_byte_for_byte(session: Path, tmp_path: Path) -> None:
    archive = tmp_path / "library" / "one.tar.gz"
    pack(session, archive)
    again = tmp_path / "again.tar.gz"
    pack(session, again)
    assert archive.read_bytes() == again.read_bytes()
    unpacked = unpack(archive, tmp_path / "out")
    assert isinstance(unpacked, Ok)
    assert unpacked.value.name == session.name
    original = {p.relative_to(session): p.read_bytes() for p in session.rglob("*") if p.is_file()}
    restored = {
        p.relative_to(unpacked.value): p.read_bytes()
        for p in unpacked.value.rglob("*")
        if p.is_file()
    }
    assert restored == original
    loaded = read(unpacked.value)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()


def test_ex5_what_is_not_the_archive_of_one_record_is_refused(
    session: Path, tmp_path: Path
) -> None:
    garbage = tmp_path / "garbage.tar.gz"
    garbage.write_bytes(b"\x1f\x8b not gzip at all")
    assert unpack(garbage, tmp_path / "a") == Err(
        "the archive cannot be read as a .tar.gz of a record folder"
    )
    assert isinstance(unpack(tmp_path / "absent.tar.gz", tmp_path / "b"), Err)
    empty = tmp_path / "empty.tar.gz"
    with tarfile.open(empty, "w:gz"):
        pass
    assert unpack(empty, tmp_path / "c") == Err("the archive must hold exactly one record folder")
    twice = tmp_path / "twice.tar.gz"
    with tarfile.open(twice, "w:gz") as tar:
        tar.add(session, arcname="first")
        tar.add(session, arcname="second")
    assert unpack(twice, tmp_path / "d") == Err("the archive must hold exactly one record folder")
    escaping = tmp_path / "escaping.tar.gz"
    with tarfile.open(escaping, "w:gz") as tar:
        tar.add(session / "manifest.json", arcname="../outside/manifest.json")
    assert isinstance(unpack(escaping, tmp_path / "e"), Err)
    assert not (tmp_path / "outside").exists()


def test_ex1_a_record_replays_the_same_from_its_folder_and_from_its_archive(
    session: Path, tmp_path: Path
) -> None:
    archive = tmp_path / "one.tar.gz"
    pack(session, archive)
    from_folder, from_archive = replay_path(session), replay_path(archive)
    assert isinstance(from_folder, Ok)
    assert isinstance(from_archive, Ok)
    assert from_folder.value.matches
    assert from_folder.value == from_archive.value
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"")
    assert isinstance(replay_path(broken), Err)


# =========================================================================
# What the gate demands
# =========================================================================


def test_ex5_a_manifest_that_still_names_somebody_is_not_anonymous(session: Path) -> None:
    loaded = read(session)
    assert isinstance(loaded, Ok)
    manifest = loaded.value.manifest
    # As the simulation writes it, the passenger pseudonym is still there.
    assert identifying(manifest) == "subject_id"
    anonymous = replace(manifest, subject_id=None)
    assert identifying(anonymous) is None
    assert identifying(replace(anonymous, session_id="convex-session-1")) == "session_id"
    assert identifying(replace(anonymous, organization_id="org-3")) == "organization_id"
    assert identifying(replace(anonymous, organization_id="anonymized")) is None


def _report(
    *, difference_at: float | None = None, divergence_at: float | None = None
) -> ReplayReport:
    clear = SafetyDecision(SafetyAction.NONE, None)
    expected = tuple(
        Decision(Seconds(t), MotorRpm(100), Phase.HOLD, clear) for t in (1.0, 2.0, 3.0, 4.0)
    )
    actual = tuple(
        replace(row, setpoint=MotorRpm(150)) if row.t == difference_at else row for row in expected
    )
    divergence = (
        None
        if divergence_at is None
        else Divergence(Seconds(divergence_at), 2, "speed 1 motor rpm", "close at t=9.000 s")
    )
    return ReplayReport("record-1", len(expected), compare_decisions(expected, actual), divergence)


def test_ex6_an_unexplained_difference_is_refused_and_an_accepted_one_must_be_that_one() -> None:
    match = _report()
    difference = _report(difference_at=3.0)
    divergence = _report(divergence_at=2.0)
    assert (match.outcome, difference.outcome, divergence.outcome) == (
        Outcome.MATCH,
        Outcome.DIFFERENCE,
        Outcome.DIVERGENCE,
    )
    assert (found_at(match), found_at(difference), found_at(divergence)) == (None, 3.0, 2.0)
    # Nothing accepted: only a match passes.
    assert refusal(match, None) is None
    assert refusal(difference, None) == "unexplained difference at t=3.000 s"
    assert refusal(divergence, None) == "unexplained divergence at t=2.000 s"
    # A difference accepted in ANH-000 at 3 s: that one passes, and only that one.
    accepted = Accepted("ANH-000", "explained there", "difference", 3.0)
    assert refusal(difference, accepted) is None
    assert refusal(difference, replace(accepted, t=3.0004)) is None
    assert refusal(_report(difference_at=2.0), accepted) == (
        "difference at t=2.000 s, accepted at t=3.000 s"
    )
    assert refusal(_report(divergence_at=3.0), accepted) == (
        "a divergence where a difference was accepted"
    )
    assert refusal(divergence, replace(accepted, outcome="divergence", t=2.0)) is None
    # The day the difference is gone, its file must go too.
    assert refusal(match, accepted) == (
        "the accepted difference no longer shows: remove its accepted file"
    )


def test_ex6_an_accepted_difference_is_a_file_next_to_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "session_x.tar.gz"
    assert accepted_for(archive) == Ok(None)
    sidecar = tmp_path / "session_x.accepted.json"
    document = {"ticket": "ANH-000", "reason": "why", "outcome": "divergence", "t": 12.4}
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    assert accepted_for(archive) == Ok(Accepted("ANH-000", "why", "divergence", 12.4))
    for broken in (
        {**document, "outcome": "match"},
        {**document, "extra": 1},
        {key: value for key, value in document.items() if key != "ticket"},
    ):
        sidecar.write_text(json.dumps(broken), encoding="utf-8")
        assert accepted_for(archive) == Err(
            "session_x.accepted.json is not an accepted-difference file"
        )
    sidecar.write_text("not json", encoding="utf-8")
    assert isinstance(accepted_for(archive), Err)


# =========================================================================
# Records made by the simulation
# =========================================================================

SHORT_SCENARIO = {
    "name": "short_bench",
    "kind": "manual",
    "duration_s": 1,
    "preroll_s": 0.4,
    "teardown_s": 0,
    "ecg": {"mode": "dsp"},
    "actions": [{"at_s": 0.4, "do": "manual_target", "output_rpm": 5.0, "expect": "accepted"}],
}


def _scenarios(tmp_path: Path, **scenarios: object) -> Path:
    directory = tmp_path / "scenarios"
    directory.mkdir()
    for name, document in scenarios.items():
        (directory / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
    return directory


def test_ex5_a_scenario_is_exported_anonymous_and_only_if_it_replays_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = _scenarios(
        tmp_path,
        short_bench=SHORT_SCENARIO,
        short_direct={**SHORT_SCENARIO, "name": "short_direct", "ecg": {"mode": "direct"}},
    )
    shelf = tmp_path / "real"
    exported = export("short_bench", shelf, scenarios)
    assert exported == Ok(shelf / "short_bench.tar.gz")
    unpacked = unpack(shelf / "short_bench.tar.gz", tmp_path / "out")
    assert isinstance(unpacked, Ok)
    loaded = read(unpacked.value)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert identifying(loaded.value.manifest) is None
    report = replay(unpacked.value)
    assert isinstance(report, Ok)
    assert report.value.matches
    # The library knows which of its records the simulation can make again.
    (shelf / "a_real_session.tar.gz").write_bytes(b"")
    assert [name_of(archive) for archive in archives(shelf)] == ["a_real_session", "short_bench"]
    assert simulated(shelf, scenarios) == ("short_bench",)
    # Not a scenario; not replayable (bpm injected with no acquisition behind them).
    assert export("no_such", shelf, scenarios) == Err("no_such is not a scenario of the battery")
    refused = export("short_direct", shelf, scenarios)
    assert isinstance(refused, Err)
    assert refused.error.startswith("short_direct is not replayable: ")
    assert not (shelf / "short_direct.tar.gz").exists()

    # A record whose own replay is not green is never written.
    def not_green(_folder: Path) -> Ok[ReplayReport] | Err[ReplayError]:
        return Ok(_report(difference_at=3.0))

    monkeypatch.setattr(library, "replay", not_green)
    (shelf / "short_bench.tar.gz").unlink()
    refused = export("short_bench", shelf, scenarios)
    assert isinstance(refused, Err)
    assert refused.error.startswith("the record of short_bench does not replay green:")
    assert not (shelf / "short_bench.tar.gz").exists()


def test_ex6_the_command_line_remakes_the_named_records_or_every_simulated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    made: list[tuple[str, Path]] = []

    def fake_export(name: str, directory: Path) -> Ok[Path] | Err[str]:
        made.append((name, directory))
        if name == "broken":
            return Err("does not replay green")
        return Ok(directory / f"{name}.tar.gz")

    monkeypatch.setattr(cli, "export", fake_export)

    def already_there(_directory: Path) -> tuple[str, ...]:
        return ("one", "two")

    monkeypatch.setattr(cli, "simulated", already_there)
    shelf = str(tmp_path)
    assert cli.main(["--export-real", "one", "--library", shelf]) == 0
    assert made == [("one", tmp_path)]
    assert capsys.readouterr().out == f"one: {tmp_path / 'one.tar.gz'}\n"
    made.clear()
    assert cli.main(["--export-real", "--library", shelf]) == 0
    assert [name for name, _ in made] == ["one", "two"]
    capsys.readouterr()
    assert cli.main(["--export-real", "one", "broken", "--library", shelf]) == 1
    captured = capsys.readouterr()
    assert captured.err == "broken: does not replay green\n"
    assert captured.out == f"one: {tmp_path / 'one.tar.gz'}\n"
