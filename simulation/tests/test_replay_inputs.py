from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.replay import replay, replay_recording
from simulation.replay_tape import TapeDrive, parse_frames
from simulation.tests.replay_support import record, short
from src.clock import ManualClock
from src.motor.drive import ControlWord, DriveError, EnableUnconfirmed
from src.motor.simulated import SimulatedDrive
from src.record import commands
from src.record.ecg import Header, RawBlock, RuntimeDelivery
from src.record.reader import read
from src.record.schema import Event, EventKind, TimeoutFailure
from src.result import Err, Ok, Result
from src.training.hr_control import HeartRateRejection, TrackerReading
from src.training.runtime import TrainingRuntime
from src.training.types import SignalQuality, TelemetrySnapshot
from src.units import Bpm, Monotonic, Seconds


def test_failed_enable_is_recorded_and_replayed_with_the_same_safety_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = SimulatedDrive.write_command

    async def unconfirmed(drive: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
        result = await original(drive, word)
        if word is ControlWord.ENABLE_OPERATION:
            return Err(EnableUnconfirmed("private diagnostic", False, False))
        return result

    monkeypatch.setattr(SimulatedDrive, "write_command", unconfirmed)
    folder = record(short("manual_27_rpm", duration=2.0, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    named = [frame for frame in recording.frames if frame.error is not None]
    assert len(named) == 1
    assert named[0].error is not None
    assert named[0].error.type == "EnableUnconfirmed"
    assert any(row.safety_rule == "enable_unconfirmed" for row in recording.rows)
    report = replay(folder)
    assert isinstance(report, Ok)
    assert report.value.matches, report.value.to_text()
    assert report.value.comparison.deviating_ticks == 0
    legacy = replace(
        recording, frames=tuple(replace(frame, error=None) for frame in recording.frames)
    )
    changed = asyncio.run(replay_recording(legacy))
    assert isinstance(changed, Ok)
    assert not changed.value.matches
    assert changed.value.divergence is not None


def test_tape_restores_named_error_and_keeps_the_legacy_latency(tmp_path: Path) -> None:
    folder = record(short("manual_27_rpm", duration=0.4, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    frames = loaded.value.frames
    failure = replace(frames[0], ok=False, error=TimeoutFailure(after=Seconds(0.7)), latency_ms=25)
    exchanges = parse_frames((failure,))
    assert isinstance(exchanges, Ok)
    origin = loaded.value.manifest.clocks.monotonic_start
    clock = ManualClock(start=Monotonic(origin + failure.t))
    tape = TapeDrive(exchanges.value, clock, origin)
    answer = asyncio.run(tape.open())
    assert isinstance(answer, Err)
    assert answer.error == exchanges.value[0].error
    assert clock.monotonic() == pytest.approx(origin + failure.t + 0.025)


def test_discrete_presence_pings_prevent_absence_and_missing_pings_fire_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = record(short("manual_27_rpm", duration=65.0, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    events = tuple(event for event in recording.events if not event.detail.startswith("attendant "))
    pings = tuple(
        Event(t=t, kind=EventKind.OPERATOR_ACTION, detail=commands.encode(commands.AttendantPing()))
        for t in (20.0, 40.0, 60.0)
    )
    with_pings = asyncio.run(replay_recording(replace(recording, events=(*events, *pings))))
    assert isinstance(with_pings, Ok)
    assert with_pings.value.matches, with_pings.value.to_text()
    actual: list[tuple[float, str | None]] = []
    tick = TrainingRuntime.tick
    origin = recording.manifest.clocks.monotonic_start

    async def observed(runtime: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        snapshot = await tick(runtime, now)
        safety = snapshot.safety
        actual.append((round(now - origin, 3), None if safety is None else safety.rule))
        return snapshot

    monkeypatch.setattr(TrainingRuntime, "tick", observed)
    without = asyncio.run(replay_recording(replace(recording, events=events)))
    assert isinstance(without, Ok)
    assert not without.value.matches
    assert next(t for t, rule in actual if rule == "attendant_absent") == 60.2
    assert all(rule != "attendant_absent" for t, rule in actual if t <= 60.0)


@pytest.mark.parametrize("arrival", [0.45, 0.4504, 0.6])
def test_delayed_reading_is_invisible_to_the_next_tick_and_visible_to_the_following_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arrival: float
) -> None:
    folder = record(short("manual_27_rpm", duration=1.0, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    block = RawBlock(
        Header(
            0,
            0.0,
            200,
            ("ECG",),
            t_received=0.2,
            runtime_delivery=RuntimeDelivery(t=arrival, t_observed=0.25),
        ),
        ((500,) * 200,),
    )
    seen: list[float] = []
    ticks: dict[float, int] = {}
    snapshots: dict[float, TelemetrySnapshot] = {}
    observe = TrainingRuntime.observe_ecg
    tick = TrainingRuntime.tick
    origin = recording.manifest.clocks.monotonic_start

    def delivered(
        runtime: TrainingRuntime,
        now: Monotonic,
        seq: int,
        quality: SignalQuality,
        heart_rate: Bpm | None,
    ) -> Result[TrackerReading, HeartRateRejection]:
        seen.append(round(now - origin, 3))
        return observe(runtime, now, seq, quality, heart_rate)

    async def observed(runtime: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        ticks[round(now - origin, 3)] = len(seen)
        snapshot = await tick(runtime, now)
        snapshots[round(now - origin, 3)] = snapshot
        return snapshot

    monkeypatch.setattr(TrainingRuntime, "observe_ecg", delivered)
    monkeypatch.setattr(TrainingRuntime, "tick", observed)
    replayed = asyncio.run(replay_recording(replace(recording, raw=(block,))))
    assert isinstance(replayed, Ok)
    assert replayed.value.matches, replayed.value.to_text()
    assert ticks[0.4] == 0
    assert ticks[0.6] == 1
    assert seen == [0.25]
    assert snapshots[0.4].heart_rate is None
    sample = snapshots[0.6].heart_rate
    assert sample is not None
    assert sample.at == pytest.approx(origin + 0.25)
    assert snapshots[0.6].heart_rate_age == pytest.approx(0.35)


def test_malformed_presence_command_is_a_named_replay_refusal(tmp_path: Path) -> None:
    folder = record(short("manual_27_rpm", duration=0.4, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    malformed = Event(t=0.2, kind=EventKind.OPERATOR_ACTION, detail="attendant_ping present=true")
    refused = asyncio.run(replay_recording(replace(loaded.value, events=(malformed,))))
    assert isinstance(refused, Err)
    assert "not replayable: takes no argument" in refused.error.detail


@pytest.mark.parametrize("instant", [-0.2, 0.0, 0.2])
def test_reading_command_and_tick_at_the_same_instant_follow_the_record_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instant: float
) -> None:
    folder = record(short("manual_27_rpm", duration=0.6, preroll=0.4), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    origin = recording.manifest.clocks.monotonic_start
    block = RawBlock(
        Header(
            0,
            instant - 0.2,
            200,
            ("ECG",),
            t_received=instant - 0.1,
            runtime_delivery=RuntimeDelivery(t=instant, t_observed=instant - 0.05),
        ),
        ((500,) * 200,),
    )
    calls: list[str] = []
    observe = TrainingRuntime.observe_ecg
    tick = TrainingRuntime.tick
    presence = TrainingRuntime.note_presence

    def delivered(
        runtime: TrainingRuntime,
        now: Monotonic,
        seq: int,
        quality: SignalQuality,
        heart_rate: Bpm | None,
    ) -> Result[TrackerReading, HeartRateRejection]:
        calls.append("reading")
        return observe(runtime, now, seq, quality, heart_rate)

    def ping(runtime: TrainingRuntime, at: Monotonic) -> None:
        calls.append("command")
        presence(runtime, at)

    async def observed(runtime: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        if round(now - origin, 3) == instant:
            calls.append("tick")
        return await tick(runtime, now)

    monkeypatch.setattr(TrainingRuntime, "observe_ecg", delivered)
    monkeypatch.setattr(TrainingRuntime, "tick", observed)
    monkeypatch.setattr(TrainingRuntime, "note_presence", ping)
    events = tuple(event for event in recording.events if not event.detail.startswith("attendant "))
    event = Event(t=instant, kind=EventKind.OPERATOR_ACTION, detail="attendant_ping")
    report = asyncio.run(
        replay_recording(replace(recording, raw=(block,), events=(*events, event)))
    )
    assert isinstance(report, Ok)
    assert report.value.matches, report.value.to_text()
    assert calls == (
        ["command", "reading", "tick"] if instant > 0 else ["reading", "tick", "command"]
    )


def test_a_legacy_backlog_cannot_delay_an_explicit_runtime_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = record(short("manual_27_rpm", duration=0.8, preroll=0.0), tmp_path)
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    recording = loaded.value
    backlog = tuple(
        RawBlock(Header(seq, seq / 1000, 1, ("EDA",), t_received=0.2), ((500,),))
        for seq in range(31)
    )
    arrival = RawBlock(
        Header(
            31,
            0.0,
            200,
            ("ECG",),
            t_received=0.2,
            runtime_delivery=RuntimeDelivery(t=0.45, t_observed=0.25),
        ),
        ((500,) * 200,),
    )
    readings: list[Monotonic] = []
    visible: list[int] = []
    observe = TrainingRuntime.observe_ecg
    presence = TrainingRuntime.note_presence
    origin = recording.manifest.clocks.monotonic_start

    def delivered(
        runtime: TrainingRuntime,
        now: Monotonic,
        seq: int,
        quality: SignalQuality,
        heart_rate: Bpm | None,
    ) -> Result[TrackerReading, HeartRateRejection]:
        readings.append(now)
        return observe(runtime, now, seq, quality, heart_rate)

    def ping(runtime: TrainingRuntime, at: Monotonic) -> None:
        if round(at - origin, 3) == 0.5:
            visible.append(len(readings))
        presence(runtime, at)

    monkeypatch.setattr(TrainingRuntime, "observe_ecg", delivered)
    monkeypatch.setattr(TrainingRuntime, "note_presence", ping)
    event = Event(t=0.5, kind=EventKind.OPERATOR_ACTION, detail="attendant_ping")
    result = asyncio.run(
        replay_recording(
            replace(recording, raw=(*backlog, arrival), events=(*recording.events, event))
        )
    )
    assert isinstance(result, Ok)
    assert result.value.matches, result.value.to_text()
    assert visible == [1]
    assert readings == [Monotonic(origin + 0.25)]
