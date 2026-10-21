import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.replay import replay_recording
from simulation.tests.test_record_console_parity import ENV, OPERATOR, console_inline, console_ticks
from src.clock import ManualClock
from src.local_config import load_local_config
from src.local_panel import build_panel
from src.motor.drive import DriveError, DriveStatus
from src.motor.simulated import SimulatedDrive
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
from src.record.schema import Event, EventKind
from src.result import Ok, Result
from src.training.runtime import TrainingRuntime
from src.training.types import Occupancy, SafetyAction, TelemetrySnapshot
from src.units import Monotonic, Seconds, UnixMillis


@pytest.mark.parametrize("camera", [False, True], ids=["ecg", "ecg_and_camera"])
async def test_input_during_service_is_visible_in_the_live_tick_but_the_next_replayed_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, camera: bool
) -> None:
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok)
    clock = ManualClock(Monotonic(10), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    panel = build_panel(
        loaded.value,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=console_inline,
        journal=journal,
    )
    attested = panel.surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attested, Ok)
    await console_ticks(panel, journal, clock, 12)
    started = panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(started, Ok)
    await console_ticks(panel, journal, clock, 1)
    waiting = asyncio.Event()
    resume = asyncio.Event()
    read_status = SimulatedDrive.read_status

    async def held(drive: SimulatedDrive) -> Result[DriveStatus, DriveError]:
        waiting.set()
        await resume.wait()
        return await read_status(drive)

    clock.advance(Seconds(0.2))
    tick_at = clock.monotonic()
    with monkeypatch.context() as patch:
        patch.setattr(SimulatedDrive, "read_status", held)
        pending = asyncio.create_task(panel.control_step())
        await asyncio.wait_for(waiting.wait(), timeout=3)
        clock.advance(Seconds(0.015))
        await panel.ecg_step()
        if camera:
            panel.runtime.trip_from_thread("presence_camera_lost", SafetyAction.RAMP_DOWN)
        delivered_at = clock.monotonic()
        resume.set()
        live = await asyncio.wait_for(pending, timeout=3)
    journal.drain()
    await console_ticks(panel, journal, clock, 0.6)
    await panel.close()
    found = records(journal.root)
    assert len(found) == 1
    recorded = read(found[0])
    assert isinstance(recorded, Ok), recorded
    recording = recorded.value
    origin = recording.manifest.clocks.monotonic_start
    relative = round(tick_at - origin, 3)
    events = recording.events
    if camera:
        events = (
            *events,
            Event(
                t=delivered_at - origin,
                kind=EventKind.OPERATOR_ACTION,
                detail="presence_trip rule=presence_camera_lost action=RAMP_DOWN",
            ),
        )
    snapshots: dict[float, TelemetrySnapshot] = {}
    tick = TrainingRuntime.tick

    async def observed(runtime: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        snapshot = await tick(runtime, now)
        snapshots[round(now - origin, 3)] = snapshot
        return snapshot

    monkeypatch.setattr(TrainingRuntime, "tick", observed)
    config = journal.root / "config" / (recording.manifest.config_hash + ".json")
    replayed = await replay_recording(replace(recording, events=events), config)
    assert isinstance(replayed, Ok), replayed
    original_sample = live.heart_rate
    replayed_sample = snapshots[relative].heart_rate
    assert original_sample is not None
    assert replayed_sample is not None
    assert original_sample.seq > replayed_sample.seq
    if camera:
        assert live.safety is not None
        assert live.safety.rule == "presence_camera_lost"
        assert snapshots[relative].safety is None
        assert replayed.value.divergence is not None
        assert "extra replay call" not in replayed.value.divergence.requested
    else:
        following = snapshots[round(relative + 0.215, 3)].heart_rate
        assert following is not None
        assert following.seq >= original_sample.seq
        assert replayed.value.matches, replayed.value.to_text()
