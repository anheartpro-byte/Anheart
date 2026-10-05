import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.capture import Capture
from simulation.harness import HarnessClock, run_scenario
from simulation.record_view import view_record
from simulation.scenario import SCENARIO_DIR, EcgMode
from simulation.tests.conftest import document, load, run_file
from simulation.tracefile import RecordingWriteError
from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.ecg import decode_block
from src.record.reader import read
from src.record.writer import Writer
from src.result import Err, Ok
from src.sensors.base import SensorKind
from src.units import Seconds, UnixMillis


def test_ex11_battery_scenario_writes_shared_v2_and_viewer_reads_the_same_rows(
    tmp_path: Path,
) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    directory = trace.write_record(tmp_path, Privacy())
    readback = read(directory)
    assert isinstance(readback, Ok)
    assert len(readback.value.rows) == len(trace.rows)
    assert readback.value.rows[0].hr_raw == trace.rows[0].hr_raw
    assert readback.value.rows[0].drive_status_word == trace.rows[0].drive_status_word
    stream = view_record(tmp_path, directory.name)
    assert isinstance(stream, Ok)
    lines = [document(line) for line in stream.value.splitlines()]
    assert lines[0]["schema"] == 2
    assert lines[0]["scenario"] == trace.manifest.record_id
    assert sum(line["type"] == "row" for line in lines) == len(trace.rows)
    assert lines[-1]["end_reason"] == trace.final.end_reason


def test_ex11_legacy_jsonl_export_still_contains_all_rows(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    path = trace.write_jsonl(tmp_path / "legacy.jsonl")
    lines = [document(line) for line in path.read_text().splitlines()]
    assert lines[0]["schema"] == 1
    assert sum(line["type"] == "row" for line in lines) == len(trace.rows)


def test_ex6_ex7_capture_uses_every_actual_channel_and_keeps_lost_slots_absent() -> None:
    clock = HarnessClock()
    capture = Capture(clock, (SensorKind.ECG, SensorKind.EDA))
    capture.accept(SampleBatch(clock.unix_millis(), ()))
    capture.accept(SampleBatch(clock.unix_millis(), (ChannelData("ECG", ()),)))
    for offset in (0, 4):
        capture.accept(
            SampleBatch(
                UnixMillis(clock.unix_millis() + offset),
                (
                    ChannelData("ECG", (101.0, 102.0)),
                    ChannelData("EDA", (201.0, 202.0)),
                ),
            )
        )
    blocks = tuple(decode_block(raw) for raw in capture.blocks)
    assert [block.header.seq for block in blocks] == [0, 2]
    assert blocks[1].samples == ((101, 102), (201, 202))
    asyncio.run(capture.refresh())
    asyncio.run(capture.refresh())
    clock.advance(Seconds(1))
    asyncio.run(capture.refresh())
    assert [at for at, _ in capture.sensors] == [0, 1]
    assert [r.kind for r in capture.sensors[0][1]] == [SensorKind.ECG, SensorKind.EDA]


def test_ex10_resolved_profile_preserves_clinical_values_but_never_passenger_name(
    tmp_path: Path,
) -> None:
    given = load(SCENARIO_DIR / "auto_jog_150_nominal.json")
    assert given.profile is not None
    passenger = "NamedPassengerSentinel"
    named = replace(given.profile, name=passenger)
    scenario = replace(
        given, profile=named, duration=Seconds(1), preroll=Seconds(0), teardown=Seconds(0)
    )
    result = asyncio.run(run_scenario(scenario))
    path = result.trace.write_record(tmp_path, Privacy((passenger,)))
    loaded = read(path)
    assert isinstance(loaded, Ok)
    profile = loaded.value.manifest.profile
    assert profile is not None
    assert profile.zone_high_bpm == named.zone_high_bpm
    assert profile.total_duration_s == named.total_duration_s
    for member in path.rglob("*"):
        assert passenger not in str(member)
        if member.is_file():
            assert passenger.encode() not in member.read_bytes()


def test_ex6_actual_dsp_run_produces_raw_and_sensor_streams(tmp_path: Path) -> None:
    given = load(SCENARIO_DIR / "manual_32_rpm_refused.json")
    scenario = replace(
        given,
        ecg=replace(given.ecg, mode=EcgMode.DSP),
        actions=(),
        duration=Seconds(3),
        preroll=Seconds(0),
        teardown=Seconds(0),
    )
    result = asyncio.run(run_scenario(scenario))
    path = result.trace.write_record(tmp_path, Privacy())
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.raw) > 1
    assert {block.header.sample_rate for block in loaded.value.raw} == {1000}
    assert len({sensor.t for sensor in loaded.value.sensors}) == 3
    assert not loaded.value.warnings


def test_ex8_viewer_reports_missing_checksums_and_refuses_outside_root(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    path = trace.write_record(tmp_path, Privacy())
    (path / "checksums.sha256").unlink()
    result = view_record(tmp_path, path.name)
    assert isinstance(result, Ok)
    lines = [document(line) for line in result.value.splitlines()]
    assert lines[-1]["code"] == "missing_checksums"
    assert isinstance(view_record(tmp_path, "../outside"), Err)
    assert isinstance(view_record(tmp_path, "missing"), Err)


def test_ex12_writer_error_reaches_cli_adapter_without_silent_success(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    trace.write_record(tmp_path, Privacy())
    with pytest.raises(RecordingWriteError, match="create"):
        trace.write_record(tmp_path, Privacy())


def test_event_detail_is_prose_and_cannot_override_shared_context(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    path = trace.write_record(tmp_path, Privacy())
    (path / "events.jsonl").write_text(
        '{"t":0,"kind":"end","detail":"simulation:invalid","actor":"system"}\n'
    )
    result = view_record(tmp_path, path.name)
    assert isinstance(result, Ok)
    meta = document(result.value.splitlines()[0])
    assert trace.manifest.geometry is not None
    assert meta["reference_radius_m"] == trace.manifest.geometry.reference_radius_m


def test_empty_trace_exports_a_readable_folder(tmp_path: Path) -> None:
    trace = run_file(SCENARIO_DIR / "manual_32_rpm_refused.json").trace
    path = replace(trace, rows=()).write_record(tmp_path, Privacy())
    result = read(path)
    assert isinstance(result, Ok)
    assert result.value.rows == ()


def test_ex6_capture_rejects_fractional_counts_instead_of_rounding_the_archive() -> None:
    clock = HarnessClock()
    capture = Capture(clock, (SensorKind.ECG,))
    with pytest.raises(ValueError, match="integer ADC"):
        capture.accept(SampleBatch(clock.unix_millis(), (ChannelData("ECG", (12.5,)),)))
    assert capture.blocks == []


def test_ex2_ex6_wall_clock_step_does_not_rename_or_retime_acquired_blocks() -> None:
    clock = HarnessClock()
    capture = Capture(clock, (SensorKind.ECG,))
    for step in (Seconds(0), Seconds(-3600)):
        clock.jump_wall(step)
        capture.accept(SampleBatch(clock.unix_millis(), (ChannelData("ECG", (1.0, 2.0)),)))
        clock.advance(Seconds(0.002))
    blocks = [decode_block(value) for value in capture.blocks]
    assert [block.header.seq for block in blocks] == [0, 1]
    assert [block.header.t_first for block in blocks] == [0.0, 0.002]


def test_ex11_generic_shared_record_does_not_invent_missing_geometry_or_final_drive_state(
    tmp_path: Path,
) -> None:
    given = load(SCENARIO_DIR / "auto_jog_150_nominal.json")
    scenario = replace(given, duration=Seconds(1), preroll=Seconds(0), teardown=Seconds(0))
    trace = asyncio.run(run_scenario(scenario)).trace
    created = Writer.create(tmp_path, replace(trace.manifest, geometry=None))
    assert isinstance(created, Ok)
    for row in trace.rows:
        assert isinstance(created.value.tick(row), Ok)
    assert isinstance(created.value.close(ManualClock(), "operator_stop"), Ok)
    result = view_record(tmp_path, created.value.path.name)
    assert isinstance(result, Ok)
    lines = [document(line) for line in result.value.splitlines()]
    assert lines[0]["zone_high_bpm"] == trace.meta["zone_high_bpm"]
    assert "reference_radius_m" not in lines[0]
    final = next(line for line in lines if line["type"] == "final")
    assert final["shaft_motor_rpm"] is None
    assert final["energised"] is None
    assert {line["code"] for line in lines if line["type"] == "warning"} == {
        "missing_viewer_geometry",
        "missing_final_drive_state",
    }
