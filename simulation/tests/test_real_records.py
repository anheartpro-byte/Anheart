"""EX-5, EX-6: the library of recorded sessions, and the gate that judges every one of them.

The second test of this file is the gate itself: it is parametrised over what
is ON DISK in ``simulation/scenarios/real/``, so an archive added to the library
is judged by the next run, and cannot be merged unless it is closed, intact,
anonymous, and replays as the library declares.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from simulation import real_records as library
from simulation import run as cli
from simulation.real_records import (
    ANONYMIZED,
    LOCAL_REF_LENGTH,
    UNVERSIONED,
    Accepted,
    Exported,
    accepted_for,
    archives,
    export,
    found_at,
    identifying,
    judge,
    library_identity,
    name_of,
    pack,
    refusal,
    replay_path,
    simulated,
    unpack,
)
from simulation.replay import ReplayError
from simulation.replay_compare import Decision, SafetyDecision, compare_decisions
from simulation.replay_report import Divergence, Outcome, ReplayReport
from simulation.scenario import EmergencyStop, Expectation, ManualTarget
from simulation.tests.replay_support import (
    Cells,
    Line,
    as_real,
    copy,
    edit_lines,
    edit_manifest,
    edit_ticks,
    record,
    reseal,
    short,
    tick_at,
)
from src.record.codec import JSON, mapping
from src.record.reader import Recording, read
from src.record.rows import JsonValue
from src.record.schema import Manifest
from src.result import Err, Ok
from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, OutputRpm, Seconds

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
    assert judge(archive, tmp_path) is None


# =========================================================================
# Archives
# =========================================================================


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One short recorded session, as a folder: a target, then an e-stop and its verdict."""
    actions = (
        ManualTarget(Seconds(1.0), OutputRpm(27.0), Expectation.ACCEPTED),
        EmergencyStop(Seconds(3.0)),
    )
    return record(
        short("manual_27_rpm", duration=6.0, actions=actions, preroll=0.4),
        tmp_path_factory.mktemp("session"),
    )


def loaded(folder: Path) -> Recording:
    result = read(folder)
    assert isinstance(result, Ok), result
    return result.value


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
    assert loaded(unpacked.value).warnings == ()


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
# Anonymity
# =========================================================================


def test_ex5_a_record_of_the_simulation_must_name_the_simulation_and_nobody_else(
    session: Path,
) -> None:
    recording = loaded(session)
    folder = session.name

    def names(manifest: Manifest) -> str | None:
        return identifying(replace(recording, manifest=manifest), "any_name", folder)

    # As the simulation writes it, the passenger pseudonym is still there.
    assert names(recording.manifest) == "subject_id"
    anonymous = replace(recording.manifest, subject_id=None)
    assert names(anonymous) is None
    assert names(replace(anonymous, session_id="convex-session-1")) == "session_id"
    assert names(replace(anonymous, organization_id="org-3")) == "organization_id"
    assert names(replace(anonymous, organization_id=ANONYMIZED)) == "organization_id"
    assert names(replace(anonymous, operator="op-7")) == "operator"
    spoken = replace(recording.events[0], actor="op-7")
    by_somebody = replace(recording, manifest=anonymous, events=(*recording.events, spoken))
    assert identifying(by_somebody, "any_name", folder) == "actor"
    # The folder is the writer's: named after the local_ref the manifest declares.
    unnamed = replace(recording, manifest=anonymous)
    assert identifying(unnamed, "any_name", "2026-10-07T101112Z_some-session-id") == "folder"


def test_ex5_a_real_session_must_name_nobody_and_carry_the_librarys_identifiers(
    session: Path, tmp_path: Path
) -> None:
    folder = as_real(copy(session, tmp_path), "session_a")
    recording = loaded(folder)
    manifest = recording.manifest
    identity = library_identity("session_a")
    assert (manifest.record_id, manifest.local_ref) == (identity, identity[:LOCAL_REF_LENGTH])
    assert recording.warnings == ()
    assert identifying(recording, "session_a", folder.name) is None

    def names(changed: Manifest) -> str | None:
        return identifying(replace(recording, manifest=changed), "session_a", folder.name)

    assert names(replace(manifest, machine_id="pi-12")) == "machine_id"
    assert names(replace(manifest, organization_id="org-3")) == "organization_id"
    assert names(replace(manifest, organization_id="synthetic")) == "organization_id"
    assert names(replace(manifest, operator="op-7")) == "operator"
    assert names(replace(manifest, operator="sim-operator")) == "operator"
    # The identifiers are the library's, derived from the name the archive bears:
    # the ones the source knows the session by would lead back to it.
    assert names(replace(manifest, record_id="5f2c9a7e41d04b6c8a3e9d1f0b7c2a55")) == "record_id"
    assert names(replace(manifest, local_ref="5f2c9a7e41d04b6c")) == "local_ref"
    assert identifying(recording, "another_name", folder.name) == "record_id"
    spoken = replace(recording.events[0], actor="sim-operator")
    by_somebody = replace(recording, events=(*recording.events, spoken))
    assert identifying(by_somebody, "session_a", folder.name) == "actor"
    assert identifying(recording, "session_a", "2026-10-07T101112Z_convex-session-1") == "folder"
    # What a library identity is: 32 hexadecimal digits that say the name, and are stable.
    assert identity == library_identity("session_a") != library_identity("session_b")
    assert len(identity) == 32
    assert int(identity, 16) >= 0


# =========================================================================
# EX-6: the accepted difference
# =========================================================================

CLEAR = SafetyDecision(SafetyAction.NONE, None)
RECORDED = tuple(
    Decision(Seconds(t), MotorRpm(100), Phase.HOLD, CLEAR) for t in (1.0, 2.0, 3.0, 4.0)
)


def _report(
    *,
    setpoint_off_at: tuple[float, ...] = (),
    phase_off_at: tuple[float, ...] = (),
    divergence_at: float | None = None,
) -> ReplayReport:
    """A replay report whose replayed side differs from four recorded ticks as asked."""
    replayed = tuple(
        replace(
            row,
            setpoint=MotorRpm(150) if row.t in setpoint_off_at else row.setpoint,
            phase=Phase.COOLDOWN if row.t in phase_off_at else row.phase,
        )
        for row in RECORDED
    )
    divergence = (
        None
        if divergence_at is None
        else Divergence(Seconds(divergence_at), 4, "speed 1 motor rpm", "close at t=9.000 s")
    )
    return ReplayReport(
        "record-1", len(RECORDED), compare_decisions(RECORDED, replayed), divergence
    )


def as_json(report: ReplayReport) -> Mapping[str, JsonValue]:
    """The report as ``--replay --json`` prints it, read back."""
    return mapping(JSON.validate_json(report.to_json()))


def _accepting(report: ReplayReport) -> Accepted:
    return Accepted("ANH-000", "explained there", as_json(report))


def test_ex6_without_an_accepted_file_only_a_match_passes() -> None:
    match = _report()
    difference = _report(setpoint_off_at=(3.0,))
    divergence = _report(divergence_at=2.0)
    assert (match.outcome, difference.outcome, divergence.outcome) == (
        Outcome.MATCH,
        Outcome.DIFFERENCE,
        Outcome.DIVERGENCE,
    )
    assert (found_at(match), found_at(difference), found_at(divergence)) == (None, 3.0, 2.0)
    assert refusal(match, None) is None
    assert refusal(difference, None) == "unexplained difference at t=3.000 s"
    assert refusal(divergence, None) == "unexplained divergence at t=2.000 s"


def test_ex6_an_accepted_difference_is_that_whole_report_and_no_other() -> None:
    difference = _report(setpoint_off_at=(2.0,))
    accepted = _accepting(difference)
    assert refusal(difference, accepted) is None
    # The same first difference, same kind, same instant: and one more behind it.
    for other in (
        _report(setpoint_off_at=(2.0, 3.0)),
        _report(setpoint_off_at=(2.0,), phase_off_at=(4.0,)),
        _report(setpoint_off_at=(2.0,), phase_off_at=(2.0,)),
    ):
        assert (other.outcome, found_at(other)) == (difference.outcome, found_at(difference))
        assert refusal(other, accepted) == (
            "the replay is not the accepted one: it differs in comparison"
        )
    # Another kind at the same instant, another instant, a divergence on top.
    assert refusal(_report(phase_off_at=(2.0,)), accepted) is not None
    assert refusal(_report(setpoint_off_at=(3.0,)), accepted) is not None
    assert refusal(_report(setpoint_off_at=(2.0,), divergence_at=4.0), accepted) == (
        "the replay is not the accepted one: it differs in divergence, outcome"
    )
    # The day the difference is gone, its file must go too.
    assert refusal(_report(), accepted) == (
        "the accepted difference no longer shows: remove its accepted file"
    )


def test_ex6_an_accepted_divergence_does_not_cover_a_difference_before_it() -> None:
    divergence = _report(divergence_at=4.0)
    accepted = _accepting(divergence)
    assert refusal(divergence, accepted) is None
    before = _report(setpoint_off_at=(2.0,), divergence_at=4.0)
    assert (before.outcome, found_at(before)) == (divergence.outcome, found_at(divergence))
    assert refusal(before, accepted) == (
        "the replay is not the accepted one: it differs in comparison"
    )
    assert refusal(_report(divergence_at=3.0), accepted) == (
        "the replay is not the accepted one: it differs in divergence"
    )


def test_ex6_a_record_of_the_simulation_is_never_excused() -> None:
    difference = _report(setpoint_off_at=(2.0,))
    accepted = _accepting(difference)
    for report in (difference, _report()):
        assert refusal(report, accepted, simulated=True) == (
            "a record of the simulation is exported again, never excused: remove its accepted file"
        )
    assert refusal(_report(), None, simulated=True) is None


def test_ex6_an_accepted_difference_is_a_file_next_to_the_archive(tmp_path: Path) -> None:
    archive = tmp_path / "session_x.tar.gz"
    assert accepted_for(archive) == Ok(None)
    sidecar = tmp_path / "session_x.accepted.json"
    report = as_json(_report(setpoint_off_at=(2.0,)))
    content: dict[str, object] = {"ticket": "ANH-000", "reason": "why", "report": report}
    sidecar.write_text(json.dumps(content), encoding="utf-8")
    assert accepted_for(archive) == Ok(Accepted("ANH-000", "why", report))
    for broken in (
        {**content, "extra": 1},
        {key: value for key, value in content.items() if key != "ticket"},
        {key: value for key, value in content.items() if key != "report"},
        {**content, "report": "a difference at 2 s"},
    ):
        sidecar.write_text(json.dumps(broken), encoding="utf-8")
        assert accepted_for(archive) == Err(
            "session_x.accepted.json is not an accepted-difference file"
        )
    sidecar.write_text("not json", encoding="utf-8")
    assert isinstance(accepted_for(archive), Err)
    # A report that matches, or says nothing usable of its outcome, accepts nothing.
    for outcome in ("match", ["difference"], None):
        sidecar.write_text(
            json.dumps({**content, "report": {**report, "outcome": outcome}}), encoding="utf-8"
        )
        assert accepted_for(archive) == Err(
            "session_x.accepted.json holds a report that is neither a difference nor a divergence"
        )


# -- the same, through the gate, on a recorded session ---------------------------


def _shelve(folder: Path, shelf: Path, name: str) -> Path:
    """Seal the (altered) record folder and put it in the library ``shelf`` as ``name``."""
    reseal(folder)
    archive = shelf / f"{name}.tar.gz"
    pack(folder, archive)
    return archive


def _replayed(archive: Path) -> ReplayReport:
    replayed = replay_path(archive)
    assert isinstance(replayed, Ok), replayed
    return replayed.value


def _accept(archive: Path) -> ReplayReport:
    """Accept whatever the replay of ``archive`` reports today, as the procedure says to."""
    report = _replayed(archive)
    accepted = {"ticket": "ANH-000", "reason": "why", "report": as_json(report)}
    archive.with_name(name_of(archive) + ".accepted.json").write_text(
        json.dumps(accepted), encoding="utf-8"
    )
    return report


def _nudge(
    column: str, t: float, change: Callable[[str], str]
) -> Callable[[Cells, list[Cells]], list[Cells]]:
    """An edit of ``ticks.csv``: the value of ``column`` at the tick recorded at ``t``."""

    def apply(header: Cells, rows: list[Cells]) -> list[Cells]:
        row = tick_at(header, rows, t)
        row[header.index(column)] = change(row[header.index(column)])
        return rows

    return apply


def test_ex6_the_gate_refuses_a_second_difference_behind_an_accepted_one(
    session: Path, tmp_path: Path
) -> None:
    # Given a real session of the library whose replay differs at 2 s (the setpoint),
    # and that difference accepted as the procedure says.
    shelf = tmp_path / "real"
    folder = as_real(copy(session, tmp_path), "session_a")
    assert judge(_shelve(folder, shelf, "session_a"), tmp_path / "0") is None
    edit_ticks(folder, _nudge("setpoint_motor_rpm", 2.0, lambda rpm: str(int(rpm) + 2)))
    archive = _shelve(folder, shelf, "session_a")
    unexplained = judge(archive, tmp_path / "1")
    assert unexplained is not None
    assert unexplained.startswith("unexplained difference at t=2.000 s\n")
    accepted = _accept(archive)
    assert judge(archive, tmp_path / "2") is None
    # When two more things differ later in the same session: the rule of a
    # verdict on one tick, and a phase.
    edit_ticks(folder, _nudge("safety_rule", 4.0, lambda _rule: "renamed_rule"))
    edit_ticks(folder, _nudge("phase", 5.0, lambda _phase: "baseline"))
    archive = _shelve(folder, shelf, "session_a")
    # Then the replay is still "a difference, first seen at 2 s", the same first one...
    again = _replayed(archive)
    assert (again.outcome, found_at(again)) == (accepted.outcome, found_at(accepted))
    assert again.comparison.first == accepted.comparison.first
    assert again.comparison.counts != accepted.comparison.counts
    # ...and the gate refuses it: it is not the report that was accepted.
    refused = judge(archive, tmp_path / "3")
    assert refused is not None
    assert refused.startswith("the replay is not the accepted one: it differs in comparison\n")
    assert "failed checks by kind:" in refused


def test_ex6_the_gate_refuses_a_difference_before_an_accepted_divergence(
    session: Path, tmp_path: Path
) -> None:
    # Given a real session whose record ends five ticks early: the replay
    # diverges at the end, and that divergence is accepted.
    shelf = tmp_path / "real"
    folder = as_real(copy(session, tmp_path), "session_b")
    edit_ticks(folder, lambda _header, rows: rows[:-5])
    archive = _shelve(folder, shelf, "session_b")
    accepted = _accept(archive)
    assert accepted.outcome is Outcome.DIVERGENCE
    assert accepted.comparison.matches
    assert judge(archive, tmp_path / "1") is None
    # When a setpoint is 50 rpm off, two seconds in, long before the divergence.
    edit_ticks(folder, _nudge("setpoint_motor_rpm", 2.0, lambda rpm: str(int(rpm) + 50)))
    archive = _shelve(folder, shelf, "session_b")
    again = _replayed(archive)
    assert again.divergence == accepted.divergence
    assert found_at(again) == found_at(accepted)
    # Then the gate refuses: the divergence is the accepted one, what precedes it is not.
    refused = judge(archive, tmp_path / "2")
    assert refused is not None
    assert refused.startswith("the replay is not the accepted one: it differs in comparison\n")
    assert "first difference: setpoint at t=2.000 s" in refused


def test_ex6_the_gate_refuses_an_accepted_file_next_to_a_record_of_the_simulation(
    session: Path, tmp_path: Path
) -> None:
    # Given a record of the simulation, anonymous as the export makes it.
    shelf = tmp_path / "real"
    folder = copy(session, tmp_path)

    def anonymous(manifest: Line) -> None:
        manifest["subject_id"] = None

    edit_manifest(folder, anonymous)
    assert judge(_shelve(folder, shelf, "manual_27_rpm"), tmp_path / "0") is None
    # When its replay differs, and somebody excuses it instead of exporting it again.
    edit_ticks(folder, _nudge("setpoint_motor_rpm", 2.0, lambda rpm: str(int(rpm) + 2)))
    archive = _shelve(folder, shelf, "manual_27_rpm")
    assert _accept(archive).outcome is Outcome.DIFFERENCE
    # Then the gate refuses the file itself...
    refused = judge(archive, tmp_path / "1")
    assert refused is not None
    assert refused.startswith(
        "a record of the simulation is exported again, never excused: remove its accepted file\n"
    )
    # ...and still does once the replay it excused matches again.
    edit_ticks(folder, _nudge("setpoint_motor_rpm", 2.0, lambda rpm: str(int(rpm) - 2)))
    archive = _shelve(folder, shelf, "manual_27_rpm")
    assert _replayed(archive).matches
    refused = judge(archive, tmp_path / "2")
    assert refused is not None
    assert refused.startswith("a record of the simulation is exported again, never excused")


def test_ex5_the_gate_says_which_demand_a_record_does_not_meet(
    session: Path, tmp_path: Path
) -> None:
    shelf = tmp_path / "real"
    shelf.mkdir()
    # Not an archive of one record.
    garbage = shelf / "garbage.tar.gz"
    garbage.write_bytes(b"not an archive")
    assert judge(garbage, tmp_path / "0") == (
        "the archive cannot be read as a .tar.gz of a record folder"
    )
    # Unreadable.
    unreadable = copy(session, tmp_path / "unreadable")
    (unreadable / "manifest.json").write_text("{}", encoding="utf-8")
    pack(unreadable, shelf / "unreadable.tar.gz")
    refused = judge(shelf / "unreadable.tar.gz", tmp_path / "1")
    assert refused is not None
    assert refused.startswith("the record cannot be read (")
    # Altered after it was closed: the checksums no longer match.
    altered = copy(session, tmp_path / "altered")
    edit_ticks(altered, _nudge("setpoint_motor_rpm", 2.0, lambda rpm: str(int(rpm) + 2)))
    pack(altered, shelf / "altered.tar.gz")
    assert judge(shelf / "altered.tar.gz", tmp_path / "2") == (
        "the record is not closed and intact: ticks.csv: checksum_mismatch"
    )
    # Never closed.
    unclosed = copy(session, tmp_path / "unclosed")
    (unclosed / "checksums.sha256").unlink()
    pack(unclosed, shelf / "unclosed.tar.gz")
    assert judge(shelf / "unclosed.tar.gz", tmp_path / "3") == (
        "the record is not closed and intact: checksums.sha256: missing_checksums"
    )
    endless = copy(session, tmp_path / "endless")

    def no_end(manifest: Line) -> None:
        manifest["ended_at"] = None

    edit_manifest(endless, no_end)
    assert judge(_shelve(endless, shelf, "endless"), tmp_path / "4") == (
        "the record is not closed: its manifest has no end"
    )
    # Not anonymous: as the simulation writes it, with the passenger's pseudonym.
    pack(session, shelf / "named.tar.gz")
    assert (
        judge(shelf / "named.tar.gz", tmp_path / "5") == "the record is not anonymous: subject_id"
    )
    # An accepted file that is not one.
    real = as_real(copy(session, tmp_path / "real_one"), "session_c")
    archive = _shelve(real, shelf, "session_c")
    (shelf / "session_c.accepted.json").write_text("{}", encoding="utf-8")
    assert judge(archive, tmp_path / "6") == (
        "session_c.accepted.json is not an accepted-difference file"
    )
    # Not replayable: an event that asks something of the runtime in prose.
    prose: Line = {"t": 1.5, "kind": "operator_action", "detail": "pressed", "actor": ANONYMIZED}
    spoken = as_real(copy(session, tmp_path / "spoken"), "session_d")
    edit_lines(spoken, "events.jsonl", lambda lines: [*lines, prose])
    refused = judge(_shelve(spoken, shelf, "session_d"), tmp_path / "7")
    assert refused is not None
    assert refused.startswith("the record is not replayable: the operator_action event at t=1.500")


# =========================================================================
# Records made by the simulation
# =========================================================================

SHORT_SCENARIO: dict[str, object] = {
    "name": "short_bench",
    "kind": "manual",
    "duration_s": 1,
    "preroll_s": 0.4,
    "teardown_s": 0,
    "ecg": {"mode": "dsp"},
    "actions": [{"at_s": 0.4, "do": "manual_target", "output_rpm": 5.0, "expect": "accepted"}],
}


def _scenarios(directory: Path, **scenarios: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in scenarios.items():
        (directory / f"{name}.json").write_text(json.dumps(content), encoding="utf-8")
    return directory


def test_ex5_exporting_a_scenario_twice_gives_the_same_archive_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = _scenarios(tmp_path / "scenarios", short_bench=SHORT_SCENARIO)
    first = export("short_bench", tmp_path / "one", scenarios)
    # Another day, another checkout, a version in the environment: the same record.
    monkeypatch.setenv("ANHEART_SOFTWARE_VERSION", "2.4.1-rc3")
    second = export("short_bench", tmp_path / "two", scenarios)
    assert isinstance(first, Ok)
    assert isinstance(second, Ok)
    assert first.value == Exported(tmp_path / "one" / "short_bench.tar.gz", rewritten=True)
    assert second.value == Exported(tmp_path / "two" / "short_bench.tar.gz", rewritten=True)
    assert first.value.archive.read_bytes() == second.value.archive.read_bytes()
    # What makes it so: identifiers that say the name, no version, the harness's dates.
    unpacked = unpack(first.value.archive, tmp_path / "out")
    assert isinstance(unpacked, Ok)
    manifest = loaded(unpacked.value).manifest
    identity = library_identity("short_bench")
    assert (manifest.record_id, manifest.local_ref) == (identity, identity[:LOCAL_REF_LENGTH])
    assert manifest.software_version == UNVERSIONED
    assert unpacked.value.name == f"2023-11-14T221500Z_{identity[:LOCAL_REF_LENGTH]}"
    # And it is a record the gate accepts as it stands.
    assert judge(first.value.archive, tmp_path / "judged") is None


def test_ex5_exporting_again_leaves_an_unchanged_archive_alone(tmp_path: Path) -> None:
    scenarios = _scenarios(tmp_path / "scenarios", short_bench=SHORT_SCENARIO)
    shelf = tmp_path / "real"
    archive = shelf / "short_bench.tar.gz"
    assert export("short_bench", shelf, scenarios) == Ok(Exported(archive, rewritten=True))
    # The same record packed otherwise, as another zlib would: other bytes, same content.
    original = archive.read_bytes()
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp_path / "unpacked", filter="data")
    (folder,) = (tmp_path / "unpacked").iterdir()
    with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
        tar.add(folder, arcname=folder.name)
    repacked = archive.read_bytes()
    assert repacked != original
    assert export("short_bench", shelf, scenarios) == Ok(Exported(archive, rewritten=False))
    assert archive.read_bytes() == repacked
    # A scenario that changed: its archive is written again, once.
    _scenarios(tmp_path / "scenarios", short_bench={**SHORT_SCENARIO, "duration_s": 1.4})
    assert export("short_bench", shelf, scenarios) == Ok(Exported(archive, rewritten=True))
    changed = archive.read_bytes()
    assert changed not in {original, repacked}
    assert export("short_bench", shelf, scenarios) == Ok(Exported(archive, rewritten=False))
    assert archive.read_bytes() == changed
    # An archive that cannot be read holds nothing worth keeping.
    archive.write_bytes(b"not an archive")
    assert export("short_bench", shelf, scenarios) == Ok(Exported(archive, rewritten=True))
    assert archive.read_bytes() == changed


def test_ex5_a_scenario_is_exported_only_if_it_replays_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenarios = _scenarios(
        tmp_path / "scenarios",
        short_bench=SHORT_SCENARIO,
        short_direct={**SHORT_SCENARIO, "name": "short_direct", "ecg": {"mode": "direct"}},
    )
    shelf = tmp_path / "real"
    exported = export("short_bench", shelf, scenarios)
    assert isinstance(exported, Ok)
    assert _replayed(exported.value.archive).matches
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
        return Ok(_report(setpoint_off_at=(3.0,)))

    monkeypatch.setattr(library, "replay", not_green)
    exported.value.archive.unlink()
    refused = export("short_bench", shelf, scenarios)
    assert isinstance(refused, Err)
    assert refused.error.startswith("the record of short_bench does not replay green:")
    assert not exported.value.archive.exists()


def test_ex6_the_command_line_remakes_the_named_records_or_every_simulated_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    made: list[tuple[str, Path]] = []

    def fake_export(name: str, directory: Path) -> Ok[Exported] | Err[str]:
        made.append((name, directory))
        if name == "broken":
            return Err("does not replay green")
        return Ok(Exported(directory / f"{name}.tar.gz", rewritten=name == "one"))

    monkeypatch.setattr(cli, "export", fake_export)

    def already_there(_directory: Path) -> tuple[str, ...]:
        return ("one", "two")

    monkeypatch.setattr(cli, "simulated", already_there)
    shelf = str(tmp_path)
    assert cli.main(["--export-real", "one", "--library", shelf]) == 0
    assert made == [("one", tmp_path)]
    assert capsys.readouterr().out == f"one: written {tmp_path / 'one.tar.gz'}\n"
    made.clear()
    assert cli.main(["--export-real", "--library", shelf]) == 0
    assert [name for name, _ in made] == ["one", "two"]
    assert capsys.readouterr().out == (
        f"one: written {tmp_path / 'one.tar.gz'}\ntwo: unchanged {tmp_path / 'two.tar.gz'}\n"
    )
    assert cli.main(["--export-real", "two", "broken", "--library", shelf]) == 1
    captured = capsys.readouterr()
    assert captured.err == "broken: does not replay green\n"
    assert captured.out == f"two: unchanged {tmp_path / 'two.tar.gz'}\n"
