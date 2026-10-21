import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from simulation import run as cli
from simulation.replay import DELIVERY_ADMISSION_MARGIN, replay_recording
from simulation.replay_tape import INSTANT_EPSILON
from simulation.tests.replay_support import copy, edit_manifest, reseal
from simulation.tests.test_record_console_parity import console_record
from simulation.tests.test_replay_modbus import console_native
from src.record import commands
from src.record.codec import JSON, mapping
from src.record.ecg import RuntimeDelivery
from src.record.reader import Recording, read
from src.record.schema import DriveFrame, Event, EventKind
from src.result import Err, Ok, Result
from src.training.runtime import TrainingRuntime
from src.training.types import SafetyAction, TelemetrySnapshot
from src.units import Monotonic


@pytest.fixture(scope="module", params=[False, True], ids=["calls", "native"])
def interleaved_session(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Recording, Path]:
    root = tmp_path_factory.mktemp("interleaving")
    native = cast(bool, request.param)
    if native:
        folder, config = asyncio.run(console_native(root))
    else:
        folder = asyncio.run(console_record(root, programme=False))
        config = root / "records" / "config"
    loaded = read(folder)
    assert isinstance(loaded, Ok), loaded
    if not native:
        config = config / (loaded.value.manifest.config_hash + ".json")
    return loaded.value, config


@pytest.mark.parametrize("entry", ["reading", "ping", "both"])
def test_inputs_during_serial_exchanges_do_not_split_the_emitting_tick(
    interleaved_session: tuple[Recording, Path], entry: str
) -> None:
    recording, config = interleaved_session
    frames = list(recording.frames)
    indices = [index for index, frame in enumerate(frames) if round(frame.t, 3) == 3.2]
    assert len(indices) >= 2
    for offset, index in enumerate(indices):
        frames[index] = replace(frames[index], t=3.2 + offset * 0.01, latency_ms=10)
    blocks = list(recording.raw)
    if entry in ("reading", "both"):
        index = next(
            index
            for index, block in enumerate(blocks)
            if block.header.runtime_delivery is not None
            and round(block.header.runtime_delivery.t, 3) == 3.2
        )
        blocks[index] = replace(
            blocks[index],
            header=replace(
                blocks[index].header,
                runtime_delivery=RuntimeDelivery(t=3.215, t_observed=3.2),
            ),
        )
    events = recording.events
    if entry in ("ping", "both"):
        events = (*events, Event(t=3.215, kind=EventKind.OPERATOR_ACTION, detail="attendant_ping"))
    replayed = asyncio.run(
        replay_recording(
            replace(recording, frames=tuple(frames), raw=tuple(blocks), events=events), config
        )
    )
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.matches, replayed.value.to_text()
    assert replayed.value.comparison.deviating_ticks == 0


@pytest.mark.parametrize("arrival", [-1e12, 1e12, -5.001, 5.001])
def test_a_delivery_outside_the_recorded_window_is_refused_before_pacing(
    interleaved_session: tuple[Recording, Path], arrival: float
) -> None:
    recording, config = interleaved_session
    if abs(arrival) < 10:
        arrival += recording.rows[0].t if arrival < 0 else recording.rows[-1].t
    block = recording.raw[0]
    altered = replace(
        block,
        header=replace(
            block.header,
            t_received=arrival - 0.1,
            runtime_delivery=RuntimeDelivery(t=arrival, t_observed=arrival),
        ),
    )
    replayed = asyncio.run(replay_recording(replace(recording, raw=(altered,)), config))
    assert isinstance(replayed, Err), replayed
    assert replayed.error.detail == "an ECG delivery lies outside the recorded tick window"


@pytest.mark.parametrize("boundary", ["start", "done"])
def test_deliveries_around_slow_boundary_ticks_replay_without_a_false_refusal(
    interleaved_session: tuple[Recording, Path], boundary: str
) -> None:
    recording, config = interleaved_session
    at_start = boundary == "start"
    tick = recording.rows[0].t if at_start else recording.rows[-1].t
    assert recording.rows[-1].phase == "done"
    frames = list(recording.frames)
    indices = [index for index, frame in enumerate(frames) if round(frame.t, 3) == tick]
    assert indices
    exchange_seconds = 0.01 if at_start else 0.05
    for offset, index in enumerate(indices):
        frames[index] = replace(
            frames[index], t=tick + offset * exchange_seconds, latency_ms=exchange_seconds * 1000
        )
    blocks = list(recording.raw)
    index = 0 if at_start else len(blocks) - 1
    block = blocks[index]
    arrival = tick + (-0.35 if at_start else 0.35)
    blocks[index] = replace(
        block,
        header=replace(
            block.header,
            t_received=min(arrival, block.header.t_received or arrival),
            runtime_delivery=RuntimeDelivery(t=arrival, t_observed=arrival),
        ),
    )
    manifest = recording.manifest
    observation = manifest.end_observation
    assert observation is not None
    manifest = replace(manifest, end_observation=replace(observation, t=recording.rows[-1].t))
    replayed = asyncio.run(
        replay_recording(
            replace(recording, manifest=manifest, frames=tuple(frames), raw=tuple(blocks)), config
        )
    )
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.matches, replayed.value.to_text()
    assert replayed.value.comparison.deviating_ticks == 0


@pytest.mark.parametrize("at_start", [False, True])
def test_the_delivery_admission_boundary_includes_the_instant_epsilon(
    interleaved_session: tuple[Recording, Path], at_start: bool
) -> None:
    recording, config = interleaved_session
    tick = recording.rows[0].t if at_start else recording.rows[-1].t
    direction = -1 if at_start else 1
    arrival = tick + direction * (DELIVERY_ADMISSION_MARGIN + INSTANT_EPSILON / 2)
    block = recording.raw[0]
    altered = replace(
        block,
        header=replace(
            block.header,
            t_received=arrival,
            runtime_delivery=RuntimeDelivery(t=arrival, t_observed=arrival),
        ),
    )
    planned = asyncio.run(replay_recording(replace(recording, raw=(altered,)), config))
    assert isinstance(planned, Ok), planned


def test_an_optional_final_observation_is_not_required_to_replay_a_valid_delivery_window(
    interleaved_session: tuple[Recording, Path],
) -> None:
    recording, config = interleaved_session
    legacy = replace(recording, manifest=replace(recording.manifest, end_observation=None))
    replayed = asyncio.run(replay_recording(legacy, config))
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.matches, replayed.value.to_text()
    assert replayed.value.comparison.deviating_ticks == 0


def test_a_driver_refusal_of_a_verified_native_document_is_a_named_cli_code_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder, config = asyncio.run(console_native(tmp_path / "original"))
    document = dict(mapping(JSON.validate_json(config.read_bytes())))
    native = dict(mapping(document["native_drive"]))
    native["timeout"] = 10.0
    document["native_drive"] = native
    content = json.dumps(document).encode()
    target = tmp_path / "rejected.json"
    target.write_bytes(content)
    source = copy(folder, tmp_path / "copy")
    edit_manifest(
        source, lambda manifest: manifest.update(config_hash=hashlib.sha256(content).hexdigest())
    )
    reseal(source)
    code = cli.main(["--replay", str(source), "--config", str(target), "--json"])
    output = capsys.readouterr()
    assert code == 2
    assert "this build refuses the native drive configuration" in output.err
    assert "Traceback" not in output.err


def test_a_new_command_variant_cannot_silently_lose_its_drive_classification(
    interleaved_session: tuple[Recording, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    recording, config = interleaved_session

    def unknown(_detail: str) -> Result[commands.Command, str]:
        return Ok(cast(commands.Command, object()))

    monkeypatch.setattr(commands, "parse", unknown)
    with pytest.raises(AssertionError):
        asyncio.run(replay_recording(recording, config))


def test_a_camera_verdict_during_native_exchanges_keeps_the_emitting_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tick = TrainingRuntime.tick
    tripped: list[Monotonic] = []

    async def camera(runtime: TrainingRuntime, now: Monotonic) -> TelemetrySnapshot:
        if abs(now - 27.0) < 1e-6:
            runtime.trip_from_thread("presence_camera_lost", SafetyAction.RAMP_DOWN)
            tripped.append(now)
        return await tick(runtime, now)

    with monkeypatch.context() as patch:
        patch.setattr(TrainingRuntime, "tick", camera)
        folder, config = asyncio.run(console_native(tmp_path))
    loaded = read(folder)
    assert isinstance(loaded, Ok), loaded
    recording = loaded.value
    assert len(tripped) == 1
    assert any(row.t > 5.2 for row in recording.rows)
    assert any(row.t == 5.2 and row.safety_rule == "presence_camera_lost" for row in recording.rows)
    frames = list(recording.frames)
    indices = [index for index, frame in enumerate(frames) if round(frame.t, 3) == 5.2]
    assert len(indices) >= 2
    for offset, index in enumerate(indices):
        frames[index] = replace(frames[index], t=5.2 + offset * 0.01, latency_ms=10)
    detail = "presence_trip rule=presence_camera_lost action=RAMP_DOWN"
    events = (
        *recording.events,
        *(
            Event(t=t, kind=EventKind.OPERATOR_ACTION, detail=detail)
            for t in (tripped[0] - recording.manifest.clocks.monotonic_start, 5.215)
        ),
    )
    replayed = asyncio.run(
        replay_recording(replace(recording, frames=tuple(frames), events=events), config)
    )
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.matches, replayed.value.to_text()
    assert replayed.value.comparison.deviating_ticks == 0


@pytest.mark.parametrize(
    "command", ["estop", "fault_reset", "start_manual ceiling_motor_rpm=300", "shutdown"]
)
def test_a_drive_command_interleaved_with_a_tick_names_the_unsupported_order(
    interleaved_session: tuple[Recording, Path], command: str
) -> None:
    recording, config = interleaved_session
    frames = list(recording.frames)
    indices = [index for index, frame in enumerate(frames) if round(frame.t, 3) == 3.2]
    assert len(indices) >= 3
    for offset, index in enumerate(indices):
        frames[index] = replace(frames[index], t=3.2 + offset * 0.01, latency_ms=10)
    native = frames[indices[0]].kind.startswith("modbus_")
    emergency = DriveFrame(
        t=3.215,
        kind="modbus_write" if native else "emergency_zero",
        register=8601 if native else 8602,
        value=0,
        ok=True,
        latency_ms=1,
    )
    frames.insert(indices[2], emergency)
    order = Event(t=3.215, kind=EventKind.OPERATOR_ACTION, detail=command)
    replayed = asyncio.run(
        replay_recording(
            replace(recording, frames=tuple(frames), events=(*recording.events, order)),
            config,
        )
    )
    assert isinstance(replayed, Ok), replayed
    assert not replayed.value.matches
    divergence = replayed.value.divergence
    assert divergence is not None, replayed.value.to_text()
    assert 3.2 <= divergence.t <= 3.23
    assert "extra replay call" in divergence.requested
    assert "t=3.215 s" in divergence.recorded
    assert "raised" not in divergence.requested
