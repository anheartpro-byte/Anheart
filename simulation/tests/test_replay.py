"""ANH-131: a recorded session, replayed against the real runtime, and compared.

Every record here comes out of the real harness on the real ECG path, and is
replayed by the code under test. The altered records are copies with one thing
changed by hand: the replay must find that thing, at its instant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simulation import run as cli
from simulation.real_records import pack
from simulation.replay import ReplayError, replay
from simulation.replay_compare import Decision, SafetyDecision, compare_decisions
from simulation.replay_report import DifferenceCode, Outcome, ReplayReport
from simulation.replay_tape import NO_FURTHER
from simulation.scenario import (
    SCENARIO_DIR,
    Acknowledge,
    AttendantLeaves,
    EmergencyStop,
    Expectation,
    ManualTarget,
    OperatorFaultReset,
    OperatorStop,
    RemoteStop,
    StartAgain,
)
from simulation.tests.conftest import document, load, num, obj, run
from simulation.tests.replay_support import (
    Cells,
    Line,
    at,
    copy,
    edit_lines,
    edit_manifest,
    edit_ticks,
    record,
    short,
    tick_at,
)
from src.record.codec import Privacy
from src.record.reader import read
from src.record.schema import EventKind
from src.result import Err, Ok
from src.training.runtime import TrainingRuntime
from src.training.types import Phase, SafetyAction, TelemetrySnapshot
from src.units import Monotonic, MotorRpm, OutputRpm, Seconds

TARGET = OutputRpm(27.0)


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A manual bench session: a target, the attendant walks away, a stop, the console exits."""
    actions = (
        ManualTarget(Seconds(1.0), TARGET, Expectation.ACCEPTED),
        AttendantLeaves(Seconds(2.0)),
        OperatorStop(Seconds(5.0)),
    )
    return record(
        short("manual_27_rpm", duration=9.0, actions=actions), tmp_path_factory.mktemp("bench")
    )


@pytest.fixture(scope="module")
def stopped(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A manual session with every other command: e-stop, both acknowledgements, a fault
    reset, an end asked off the machine, a second START."""
    actions = (
        ManualTarget(Seconds(1.0), TARGET, Expectation.ACCEPTED),
        EmergencyStop(Seconds(3.0)),
        Acknowledge(Seconds(5.0), estop_released=False),
        Acknowledge(Seconds(6.0), estop_released=True),
        OperatorFaultReset(Seconds(6.4), Expectation.REFUSED),
        RemoteStop(Seconds(7.0)),
        StartAgain(Seconds(7.4), Expectation.REFUSED),
    )
    return record(
        short("manual_27_rpm", duration=9.0, actions=actions), tmp_path_factory.mktemp("stopped")
    )


@pytest.fixture(scope="module")
def programme(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The first seconds of a programme: the heart rate matters from the start."""
    return record(short("auto_jog_150_dsp", duration=15.0), tmp_path_factory.mktemp("programme"))


def replayed(folder: Path) -> ReplayReport:
    result = replay(folder)
    assert isinstance(result, Ok), result
    return result.value


def refused(folder: Path) -> str:
    result = replay(folder)
    assert isinstance(result, Err), result
    assert isinstance(result.error, ReplayError)
    return result.error.detail


# =========================================================================
# EX-1: the record goes back in, the same decisions come out
# =========================================================================


def test_ex1_a_recorded_manual_session_replays_tick_for_tick(bench: Path) -> None:
    loaded = read(bench)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    # The record really holds the three kinds of input: idle ticks before the
    # start, ECG the DSP made a heart rate of, and the commands by name.
    assert recording.rows[0].t < 0.0
    assert any(row.hr_raw is not None for row in recording.rows)
    assert all(block.header.t_received is not None for block in recording.raw)
    commands = [event.detail for event in recording.events if event.kind is not EventKind.PHASE]
    assert commands[:5] == [
        "attendant present=true",
        "confirm_estop_wiring",
        "start_manual ceiling_motor_rpm=1380",
        "manual_target output_rpm=27.0",
        "attendant present=false",
    ]
    assert "stop" in commands
    assert "shutdown" in commands
    report = replayed(bench)
    assert report.outcome is Outcome.MATCH
    assert report.matches
    assert report.record == recording.manifest.record_id
    assert report.recorded_ticks == len(recording.rows) == report.comparison.actual_ticks
    assert report.divergence is None
    assert report.integrity == ()
    # Exact, not merely within the tolerances.
    assert report.comparison.tolerated_rpm == (0, 0)
    assert report.comparison.shifted_transitions == (0, 0)


def test_ex1_every_command_of_the_vocabulary_is_issued_again(stopped: Path) -> None:
    loaded = read(stopped)
    assert isinstance(loaded, Ok)
    details = {event.detail for event in loaded.value.events}
    assert {
        "estop",
        "acknowledge estop_released=false",
        "acknowledge estop_released=true",
        "fault_reset",
        "stop",
    } <= details
    kinds = {event.kind for event in loaded.value.events if event.detail == "stop"}
    assert kinds == {EventKind.REMOTE_COMMAND}
    # The e-stop really was decided in the session being replayed.
    assert any(row.safety_rule == "operator_estop" for row in loaded.value.rows)
    assert replayed(stopped).matches


def test_ex1_a_recorded_programme_replays_with_its_heart_rate(programme: Path) -> None:
    loaded = read(programme)
    assert isinstance(loaded, Ok)
    assert "start_programme" in {event.detail for event in loaded.value.events}
    assert loaded.value.manifest.profile is not None
    assert replayed(programme).matches


def test_ex1_the_heart_rate_reaches_the_runtime_through_the_recorded_ecg_only(
    programme: Path, tmp_path: Path
) -> None:
    # Given the same session with no ECG block from 2 s on (the blocks, not the ticks).
    folder = copy(programme, tmp_path)
    kept = 0
    loaded = read(programme)
    assert isinstance(loaded, Ok)
    for block in loaded.value.raw:
        if block.header.t_first >= 2.0:
            (folder / "ecg_raw" / f"{block.header.seq:06d}.bin.gz").unlink()
        else:
            kept += 1
    assert kept > 0
    # When
    report = replayed(folder)
    # Then the replayed runtime loses the heart rate the recorded one kept.
    assert report.outcome is Outcome.DIFFERENCE
    first = report.comparison.first
    assert first is not None
    assert first.expected is not None
    assert first.actual is not None
    assert first.expected.action is SafetyAction.NONE
    assert (first.actual.action, first.actual.rule) == (SafetyAction.FREEZE, "hr_stale")
    assert 11.5 <= first.t <= 13.0


# =========================================================================
# EX-2: tick by tick, with the documented tolerances
# =========================================================================


def test_ex2_a_setpoint_two_rpm_off_is_reported_at_its_instant(
    bench: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = copy(bench, tmp_path)

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        row = tick_at(header, rows, 3.0)
        column = header.index("setpoint_motor_rpm")
        row[column] = str(int(row[column]) + 2)
        return rows

    edit_ticks(folder, change)
    report = replayed(folder)
    assert report.outcome is Outcome.DIFFERENCE
    first = report.comparison.first
    assert first is not None
    assert (first.code, first.t) == (DifferenceCode.SETPOINT, Seconds(3.0))
    assert first.expected is not None
    assert first.actual is not None
    assert first.expected.setpoint - first.actual.setpoint == 2
    assert report.comparison.counts == ((DifferenceCode.SETPOINT, 1),)
    # The altered file is named: a record changed after it was closed says so.
    assert report.integrity == ("ticks.csv: checksum_mismatch",)
    # The same thing, as the command line tells it: readable, and a non-zero exit code.
    assert cli.main(["--replay", str(folder)]) == cli.EXIT_DIFFERENT
    text = capsys.readouterr().out
    assert "DIFFERENCE" in text
    assert "first difference: setpoint at t=3.000 s" in text
    assert f"recorded: setpoint {first.expected.setpoint} motor rpm, phase hold" in text
    assert f"replayed: setpoint {first.actual.setpoint} motor rpm, phase hold" in text
    assert "integrity warning: ticks.csv: checksum_mismatch" in text
    assert cli.main(["--replay", str(folder), "--json"]) == cli.EXIT_DIFFERENT
    parsed = document(capsys.readouterr().out)
    assert parsed["outcome"] == "difference"
    assert obj(parsed["tolerances"]) == {"setpoint_motor_rpm": 1, "verdict_ticks": 1}
    assert num(obj(obj(parsed["comparison"])["first"])["t"]) == 3.0


@pytest.mark.parametrize("by", [-1, 1])
def test_ex2_a_setpoint_one_rpm_off_is_within_the_tolerance(
    bench: Path, tmp_path: Path, by: int
) -> None:
    folder = copy(bench, tmp_path)

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        row = tick_at(header, rows, 3.0)
        column = header.index("setpoint_motor_rpm")
        row[column] = str(int(row[column]) + by)
        return rows

    edit_ticks(folder, change)
    report = replayed(folder)
    assert report.matches
    assert sum(report.comparison.tolerated_rpm) == 1


def _move_verdict(folder: Path, ticks: int) -> float:
    """Make the record say the e-stop verdict came ``ticks`` ticks later. Returns its real t."""
    found: list[float] = []

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        action, rule, t = (
            header.index("safety_action"),
            header.index("safety_rule"),
            header.index("t"),
        )
        first = next(index for index, row in enumerate(rows) if row[rule] == "operator_estop")
        found.append(float(rows[first][t]))
        for row in rows[first : first + ticks]:
            row[action], row[rule] = rows[first - 1][action], rows[first - 1][rule]
        return rows

    edit_ticks(folder, change)
    return found[0]


def test_ex2_a_verdict_moved_by_two_ticks_is_reported_at_its_second(
    stopped: Path, tmp_path: Path
) -> None:
    folder = copy(stopped, tmp_path)
    decided_at = _move_verdict(folder, 2)
    assert decided_at == 3.0
    report = replayed(folder)
    assert report.outcome is Outcome.DIFFERENCE
    first = report.comparison.first
    assert first is not None
    assert (first.code, first.t) == (DifferenceCode.TRANSITION_TIMING, Seconds(3.0))
    assert first.expected is not None
    assert first.actual is not None
    assert first.expected.action is SafetyAction.NONE
    assert first.actual.rule == "operator_estop"
    assert "transition_timing at t=3.000 s" in report.to_text()


def test_ex2_a_verdict_moved_by_one_tick_is_within_the_tolerance(
    stopped: Path, tmp_path: Path
) -> None:
    folder = copy(stopped, tmp_path)
    _move_verdict(folder, 1)
    report = replayed(folder)
    assert report.matches
    assert report.comparison.shifted_transitions == (1, 0)
    assert "1 verdicts one tick apart" in report.to_text()


def test_ex2_the_phase_is_compared_exactly(bench: Path, tmp_path: Path) -> None:
    folder = copy(bench, tmp_path)

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        tick_at(header, rows, 2.0)[header.index("phase")] = "cooldown"
        return rows

    edit_ticks(folder, change)
    first = replayed(folder).comparison.first
    assert first is not None
    assert (first.code, first.t) == (DifferenceCode.PHASE, Seconds(2.0))


# =========================================================================
# EX-3: the runtime asks for a frame the record does not hold
# =========================================================================


def _speed_frame(lines: list[Line], t: float) -> Line:
    """The first speed write of the tick at ``t``: its keepalive."""
    return next(line for line in lines if line["kind"] == "speed" and abs(at(line) - t) < 1e-9)


def test_ex3_a_modified_frame_stops_the_replay_and_names_the_instant_and_the_difference(
    bench: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given the record with ONE drive frame changed: the speed written at 3 s.
    folder = copy(bench, tmp_path)
    written: list[int] = []

    def change(lines: list[Line]) -> list[Line]:
        frame = _speed_frame(lines, 3.0)
        value = frame["value"]
        assert isinstance(value, int)
        written.append(value)
        frame["value"] = value + 5
        return lines

    edit_lines(folder, "drive_frames.jsonl", change)
    # When
    report = replayed(folder)
    # Then the replay stops THERE, and says what it asked and what the record holds.
    assert report.outcome is Outcome.DIVERGENCE
    divergence = report.divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(3.0)
    assert divergence.requested == f"speed {written[0]} motor rpm"
    assert divergence.recorded == f"speed {written[0] + 5} motor rpm at t=3.000 s"
    # Nothing after the divergence was replayed; what came before still compares equal.
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    before = sum(row.t < 3.0 for row in loaded.value.rows)
    assert divergence.tick == before == report.comparison.actual_ticks
    assert report.comparison.matches
    assert report.recorded_ticks == len(loaded.value.rows) > before
    # A divergence is a finding, with its own exit code: not a failure of the tool.
    assert cli.main(["--replay", str(folder)]) == cli.EXIT_DIFFERENT
    text = capsys.readouterr().out
    assert "DIVERGENCE" in text
    assert f"divergence at t=3.000 s, after {before} ticks" in text
    assert cli.main(["--replay", str(folder), "--json"]) == cli.EXIT_DIFFERENT
    parsed = document(capsys.readouterr().out)
    assert parsed["outcome"] == "divergence"
    assert num(obj(parsed["divergence"])["t"]) == pytest.approx(3.0)


def test_ex3_a_frame_missing_from_the_record_is_a_divergence(bench: Path, tmp_path: Path) -> None:
    folder = copy(bench, tmp_path)
    edit_lines(
        folder,
        "drive_frames.jsonl",
        lambda lines: [line for line in lines if line is not _speed_frame(lines, 4.0)],
    )
    divergence = replayed(folder).divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(4.0)
    assert divergence.requested.startswith("speed ")
    assert divergence.recorded == "read_status at t=4.000 s"


def test_ex3_a_record_cut_short_is_a_divergence_at_its_last_frame(
    bench: Path, tmp_path: Path
) -> None:
    folder = copy(bench, tmp_path)
    edit_lines(
        folder, "drive_frames.jsonl", lambda lines: [line for line in lines if at(line) < 6.0]
    )
    divergence = replayed(folder).divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(6.0)
    assert divergence.recorded == NO_FURTHER


def test_ex3_a_frame_the_runtime_never_asks_for_is_a_divergence_too(
    bench: Path, tmp_path: Path
) -> None:
    folder = copy(bench, tmp_path)
    loaded = read(bench)
    assert isinstance(loaded, Ok)
    end = loaded.value.frames[-1].t

    def change(lines: list[Line]) -> list[Line]:
        return [*lines, {**lines[0], "t": end}]

    edit_lines(folder, "drive_frames.jsonl", change)
    report = replayed(folder)
    assert report.outcome is Outcome.DIVERGENCE
    divergence = report.divergence
    assert divergence is not None
    assert divergence.requested == NO_FURTHER
    assert divergence.recorded == f"open at t={end:.3f} s"
    # Every tick was replayed, and every one of them matched.
    assert divergence.tick == report.recorded_ticks
    assert report.comparison.matches


def test_ex3_a_drive_that_answers_otherwise_makes_the_runtime_decide_otherwise(
    bench: Path, tmp_path: Path
) -> None:
    # Given a status read at 3 s that now reports a drive fault.
    folder = copy(bench, tmp_path)

    def change(lines: list[Line]) -> list[Line]:
        (frame,) = [
            line for line in lines if line["kind"] == "read_status" and abs(at(line) - 3.0) < 1e-9
        ]
        values = frame["value"]
        assert isinstance(values, list)
        frame["value"] = [0x0648, *values[1:4], 9]
        return lines

    edit_lines(folder, "drive_frames.jsonl", change)
    # When
    report = replayed(folder)
    # Then the runtime reacts to the fault within that tick: it takes the speed
    # down, which is not the write the record holds next. Nothing is raised, no
    # decision of that broken tick is kept, and every tick before it matched.
    assert report.outcome is Outcome.DIVERGENCE
    divergence = report.divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(3.0)
    assert divergence.requested.startswith("speed ")
    assert divergence.recorded.startswith("speed ")
    assert divergence.recorded.endswith(" at t=3.000 s")
    assert divergence.requested not in divergence.recorded
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    assert divergence.tick == sum(row.t < 3.0 for row in loaded.value.rows)
    assert report.comparison.actual_ticks == divergence.tick
    assert report.comparison.matches


def test_ex3_a_mismatch_found_by_the_emergency_zero_is_kept_without_raising(
    stopped: Path, tmp_path: Path
) -> None:
    # The e-stop zeroes the reference synchronously: that call may not raise.
    folder = copy(stopped, tmp_path)
    edit_lines(
        folder,
        "drive_frames.jsonl",
        lambda lines: [line for line in lines if line["kind"] != "emergency_zero"],
    )
    divergence = replayed(folder).divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(3.0)
    assert divergence.requested == "emergency_zero"


def test_ex3_code_that_raises_on_recorded_inputs_is_reported_not_raised(
    bench: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Monotonic] = []
    real = TrainingRuntime.tick

    async def tick(self: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        calls.append(now)
        if len(calls) == 4:
            raise RuntimeError("synthetic")
        return await real(self, now)

    monkeypatch.setattr(TrainingRuntime, "tick", tick)
    report = replayed(bench)
    assert report.outcome is Outcome.DIVERGENCE
    divergence = report.divergence
    assert divergence is not None
    assert divergence.tick == 3
    assert divergence.requested == "nothing: the replayed code raised RuntimeError"
    assert "synthetic" not in report.to_text()


# =========================================================================
# EX-7: the same record, the same report
# =========================================================================


def test_ex7_replaying_the_same_record_twice_gives_the_same_report(
    bench: Path, programme: Path, tmp_path: Path
) -> None:
    for folder in (bench, programme):
        first, second = replayed(folder), replayed(folder)
        assert first == second
        assert first.to_json().encode() == second.to_json().encode()
        assert first.to_text().encode() == second.to_text().encode()
    # A report of a difference is as reproducible as a report of a match.
    altered = copy(bench, tmp_path)
    edit_lines(
        altered, "drive_frames.jsonl", lambda lines: [line for line in lines if at(line) < 6.0]
    )
    first, second = replayed(altered), replayed(altered)
    assert first.outcome is Outcome.DIVERGENCE
    assert first.to_json().encode() == second.to_json().encode()
    assert first.to_text().encode() == second.to_text().encode()


def test_ex7_the_json_report_is_one_sorted_line(bench: Path) -> None:
    text = replayed(bench).to_json()
    assert text.endswith("\n")
    assert text.count("\n") == 1
    parsed = document(text)
    assert list(parsed) == sorted(parsed)
    assert parsed["outcome"] == "match"
    assert parsed["divergence"] is None
    assert parsed["integrity"] == []


# =========================================================================
# The command line
# =========================================================================


def test_ex1_the_command_line_replays_a_folder_or_its_archive(
    bench: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--replay", str(bench)]) == cli.EXIT_MATCH
    text = capsys.readouterr().out
    assert text.startswith("replay of record ")
    assert "MATCH" in text.splitlines()[0]
    assert "tolerances: setpoint +/-1 motor rpm, verdict instant +/-1 tick" in text
    archive = tmp_path / "bench.tar.gz"
    pack(bench, archive)
    assert cli.main(["--replay", str(archive), "--json"]) == cli.EXIT_MATCH
    assert document(capsys.readouterr().out)["outcome"] == "match"


def test_ex1_a_record_that_cannot_be_replayed_is_exit_code_two_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--replay", str(tmp_path)]) == cli.EXIT_UNUSABLE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("replay impossible: the record cannot be read")
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not an archive")
    assert cli.main(["--replay", str(broken)]) == cli.EXIT_UNUSABLE
    assert "cannot be read as a .tar.gz" in capsys.readouterr().err


# =========================================================================
# What is refused, with the reason (the tool has nothing to judge)
# =========================================================================


def test_a_session_whose_heart_rate_did_not_come_from_raw_ecg_is_refused(tmp_path: Path) -> None:
    # The battery's ``direct`` mode hands the runtime bpm with no acquisition behind them.
    given = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    assert given.ecg.mode.value == "direct"
    folder = run(given).trace.write_record(tmp_path, Privacy())
    assert "holds no raw ECG" in refused(folder)


def test_an_event_that_is_no_command_is_refused_by_instant_and_kind(
    bench: Path, tmp_path: Path
) -> None:
    folder = copy(bench, tmp_path)
    prose: Line = {
        "t": 1.5,
        "kind": "operator_action",
        "detail": "PassengerSentinel pressed something",
        "actor": "op",
    }
    edit_lines(folder, "events.jsonl", lambda lines: [*lines, prose])
    reason = refused(folder)
    assert "the operator_action event at t=1.500 s is not replayable" in reason
    assert "PassengerSentinel" not in reason


def test_a_start_programme_without_a_programme_is_refused(bench: Path, tmp_path: Path) -> None:
    folder = copy(bench, tmp_path)
    start: Line = {"t": 0.0, "kind": "operator_action", "detail": "start_programme", "actor": "op"}
    edit_lines(folder, "events.jsonl", lambda lines: [*lines, start])
    assert "no programme in the manifest" in refused(folder)


def test_a_programme_this_build_refuses_is_refused(programme: Path, tmp_path: Path) -> None:
    folder = copy(programme, tmp_path)

    def change(manifest: Line) -> None:
        profile = dict(obj(manifest["profile"]))
        profile["zone_low_bpm"], profile["zone_high_bpm"] = 155, 145
        manifest["profile"] = profile

    edit_manifest(folder, change)
    assert "programme is not one this build accepts" in refused(folder)


def test_a_record_without_its_idle_ticks_is_refused(bench: Path, tmp_path: Path) -> None:
    # What a record looked like before it kept the ticks of before the start.
    folder = copy(bench, tmp_path)
    edit_ticks(
        folder, lambda header, rows: [row for row in rows if float(row[header.index("t")]) > 0.0]
    )
    edit_lines(folder, "events.jsonl", lambda lines: [line for line in lines if at(line) >= 0.0])
    assert "does not hold the idle ticks" in refused(folder)


def test_a_record_with_no_tick_or_ticks_out_of_order_is_refused(
    bench: Path, tmp_path: Path
) -> None:
    empty = copy(bench, tmp_path / "empty")
    edit_ticks(empty, lambda _header, _rows: [])
    assert "has no tick" in refused(empty)
    doubled = copy(bench, tmp_path / "doubled")
    edit_ticks(doubled, lambda _header, rows: [rows[0], rows[0], *rows[1:]])
    assert "tick 1 does not come after the one before it" in refused(doubled)


@pytest.mark.parametrize(("column", "value"), [("phase", "warp"), ("safety_action", "PANIC")])
def test_a_decision_this_build_does_not_know_is_refused(
    bench: Path, tmp_path: Path, column: str, value: str
) -> None:
    folder = copy(bench, tmp_path)

    def change(header: Cells, rows: list[Cells]) -> list[Cells]:
        rows[7][header.index(column)] = value
        return rows

    edit_ticks(folder, change)
    assert "tick 7 holds a phase or a safety action this build does not know" in refused(folder)


def test_native_modbus_exchanges_are_refused_not_half_replayed(bench: Path, tmp_path: Path) -> None:
    folder = copy(bench, tmp_path)

    def change(lines: list[Line]) -> list[Line]:
        lines[3]["kind"] = "modbus_read"
        return lines

    edit_lines(folder, "drive_frames.jsonl", change)
    reason = refused(folder)
    assert reason.startswith("drive frame 3 (t=")
    assert "native driver's Modbus exchanges are not replayable" in reason


def test_a_manifest_the_runtime_cannot_be_built_from_is_refused(
    bench: Path, tmp_path: Path
) -> None:
    bare = copy(bench, tmp_path / "bare")

    def drop(manifest: Line) -> None:
        manifest["geometry"] = None

    edit_manifest(bare, drop)
    assert "holds no geometry" in refused(bare)
    absurd = copy(bench, tmp_path / "absurd")

    def break_ratio(manifest: Line) -> None:
        manifest["geometry"] = {**obj(manifest["geometry"]), "gear_ratio": -1.0}

    edit_manifest(absurd, break_ratio)
    assert "refuses to build a runtime" in refused(absurd)


def test_an_odd_tick_interval_and_an_unvetted_member_name_are_handled(
    bench: Path, tmp_path: Path
) -> None:
    # A tick 50 ms late (a real console's ticks are not on a grid), and a
    # checksum line naming a member no report should repeat.
    folder = copy(bench, tmp_path)

    def late(header: Cells, rows: list[Cells]) -> list[Cells]:
        row = tick_at(header, rows, 4.0)
        row[header.index("t")] = "4.05"
        return rows

    edit_ticks(folder, late)
    with (folder / "checksums.sha256").open("a", encoding="utf-8") as handle:
        handle.write(f"{'0' * 64}  Passenger Sentinel.txt\n")
    report = replayed(folder)
    assert report.recorded_ticks == report.comparison.actual_ticks
    assert "<member>: missing_file" in report.integrity
    assert "Sentinel" not in report.to_text() + report.to_json()


def test_a_report_with_a_tick_missing_on_one_side_names_the_side_that_has_it() -> None:
    clear = SafetyDecision(SafetyAction.NONE, None)
    rows = tuple(Decision(Seconds(t), MotorRpm(100), Phase.HOLD, clear) for t in (0.2, 0.4))
    for expected, actual, absent in ((rows, rows[:1], "replayed:"), (rows[:1], rows, "recorded:")):
        text = ReplayReport("record-1", 2, compare_decisions(expected, actual), None).to_text()
        assert "first difference: tick_count at t=0.400 s" in text
        assert absent not in text
        assert ("recorded:" in text) != ("replayed:" in text)
