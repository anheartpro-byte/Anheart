import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from tests.sto_support import ready_sto

from simulation import run as cli
from simulation.modbus import FailedExchange, InlineExecutor, SimulatedMaster
from simulation.replay import replay, replay_recording
from simulation.replay_modbus import SDK_KINDS
from simulation.tests.test_record_console_parity import ENV, OPERATOR, console_inline, console_ticks
from src import local_panel
from src.clock import Clock, ManualClock
from src.local_config import LocalConfig, load_local_config
from src.local_panel import DriveSide, build_panel
from src.motor.atv320 import ATV320Drive
from src.motor.drive import DriveBackend
from src.motor.observation import (
    ExceptionReply,
    ExchangeKind,
    NoResponse,
)
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import SimulatedStoChain, StoSource, StoState
from src.record.codec import JSON, mapping
from src.record.journal import Journal
from src.record.reader import Recording, read
from src.record.retention import records
from src.record.schema import DriveFrame
from src.result import Err, Ok
from src.training.preflight import StoNotReady
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, UnixMillis


async def console_native(root: Path, *, failed: bool = False) -> tuple[Path, Path]:
    loaded = load_local_config(
        {**ENV, "MOTOR_BACKEND": "serial", "MOTOR_PORT": "simulated", "MOTOR_REG_OFFSET": "-1"}
    )
    assert isinstance(loaded, Ok), loaded
    config = loaded.value
    link = config.drive_link
    assert link is not None
    clock = ManualClock(Monotonic(10), UnixMillis(1_700_000_000_000))
    model = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    master = SimulatedMaster(model, clock, link.registers)
    drive = ATV320Drive(
        clock,
        master,
        link.settings,
        link.registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    journal = Journal(root / "records", clock)

    def ready_source(
        _config: LocalConfig, _clock: Clock, _drive: DriveBackend, _chain: SimulatedStoChain | None
    ) -> StoSource:
        return ready_sto()

    # This fixture records and replays native Modbus exchanges. Its STO chain
    # is explicitly ready: drive_input polling happens outside the tick and
    # native replay does not yet reproduce those exchanges (ANH-192 follow-up).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(local_panel, "build_sto", ready_source)
        panel = build_panel(
            config,
            clock=clock,
            profiles_path=root / "profiles.json",
            treat=console_inline,
            journal=journal,
            drive=DriveSide(backend=drive, simulator=None, release=master.close),
        )
    attest_estop_wiring_result = panel.surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attest_estop_wiring_result, Ok)
    await console_ticks(panel, journal, clock, 12)
    submit_start_manual_result = panel.surface.submit_start_manual(
        occupancy=Occupancy.BENCH, operator=OPERATOR
    )
    assert isinstance(submit_start_manual_result, Ok)
    await console_ticks(panel, journal, clock, 1)
    submit_manual_target_result = panel.surface.submit_manual_target(
        output_rpm=OutputRpm(3), operator=OPERATOR
    )
    assert isinstance(submit_manual_target_result, Ok)
    await console_ticks(panel, journal, clock, 3)
    assert panel.runtime.state is RuntimeState.RUNNING
    if failed:
        master.failures.append(FailedExchange(ExchangeKind.READ, NoResponse(), link.registers.eta))
    await console_ticks(panel, journal, clock, 4)
    submit_end_result = panel.surface.submit_end(operator=OPERATOR, reason="done")
    assert isinstance(submit_end_result, Ok)
    await console_ticks(panel, journal, clock, 60)
    await panel.close()
    assert model.commanded_setpoint == 0
    found = records(journal.root)
    assert len(found) == 1
    result = read(found[0])
    assert isinstance(result, Ok), result
    assert result.value.warnings == ()
    assert result.value.manifest.end_reason is not None
    return found[0], journal.root / "config" / (result.value.manifest.config_hash + ".json")


async def test_native_console_without_sto_replacement_refuses_arming(tmp_path: Path) -> None:
    loaded = load_local_config(
        {**ENV, "MOTOR_BACKEND": "serial", "MOTOR_PORT": "simulated", "MOTOR_REG_OFFSET": "-1"}
    )
    assert isinstance(loaded, Ok), loaded
    config = loaded.value
    link = config.drive_link
    assert link is not None
    clock = ManualClock(Monotonic(10), UnixMillis(1_700_000_000_000))
    model = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    master = SimulatedMaster(model, clock, link.registers)
    drive = ATV320Drive(
        clock,
        master,
        link.settings,
        link.registers,
        executor_factory=InlineExecutor,
        wait=master.wait,
    )
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=console_inline,
        drive=DriveSide(backend=drive, simulator=None, release=master.close),
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    refused = panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, StoNotReady)
    assert refused.error.state is StoState.NOT_CONFIGURED
    assert model.acquisition_evidence.possible_frames == 0
    await panel.close()


@pytest.fixture(scope="module", params=[False, True], ids=["healthy", "failed_read"])
def native_session(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> tuple[Path, Path, bool]:
    failed = cast(bool, request.param)
    root = tmp_path_factory.mktemp("native")
    folder, config = asyncio.run(console_native(root, failed=failed))
    return folder, config, failed


def test_native_console_session_replays_exactly_and_report_bytes_repeat(
    native_session: tuple[Path, Path, bool],
    capsys: pytest.CaptureFixture[str],
) -> None:
    folder, config, failed = native_session
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    record = loaded.value
    assert {frame.kind for frame in record.frames} == {
        "modbus_connect",
        "modbus_read",
        "modbus_write",
    }
    assert any(frame.register == 3200 for frame in record.frames)
    assert any(not frame.ok for frame in record.frames) is failed
    assert any(row.measured_motor_rpm > 0 for row in record.rows)
    one = replay(folder, config)
    assert isinstance(one, Ok), one
    assert one.value.matches, one.value.to_text()
    assert one.value.comparison.deviating_ticks == 0
    assert one.value.comparison.tolerated_rpm == (0, 0)
    assert one.value.comparison.shifted_transitions == (0, 0)
    two = replay(folder, config)
    assert isinstance(two, Ok), two
    assert one.value.to_json().encode() == two.value.to_json().encode()
    exit_code = cli.main(["--replay", str(folder), "--config", str(config), "--json"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == one.value.to_json()
    with capsys.disabled():
        print(one.value.to_json(), end="")  # noqa: T201  # retained replay evidence


def test_native_perturbations_name_the_instant_and_register(
    native_session: tuple[Path, Path, bool],
) -> None:
    folder, config, failed = native_session
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    record = loaded.value
    frames = list(record.frames)
    index = next(
        i
        for i, frame in enumerate(frames)
        if frame.t > 2 and frame.kind == "modbus_read" and frame.register == 3200
    )
    frame = frames[index]
    frames[index] = replace(frame, value=0x0638)
    changed = replace(record, frames=tuple(frames))
    assert_diverges(changed, config, frame.t, 7120)
    removed = replace(record, frames=record.frames[:index] + record.frames[index + 1 :])
    assert_diverges(removed, config, frame.t, 3200)
    if failed:
        index = next(i for i, frame in enumerate(record.frames) if not frame.ok)
        frames = list(record.frames)
        frames[index] = replace(frames[index], error=ExceptionReply(code=2))
        tolerated = asyncio.run(replay_recording(replace(record, frames=tuple(frames)), config))
        assert isinstance(tolerated, Ok)
        assert tolerated.value.matches


def assert_diverges(record: Recording, config: Path, at: float, register: int) -> None:
    result = asyncio.run(replay_recording(record, config))
    assert isinstance(result, Ok), result
    divergence = result.value.divergence
    assert divergence is not None, result.value.to_text()
    assert divergence.t == pytest.approx(at, abs=0.2)
    assert str(register) in divergence.requested + divergence.recorded
    assert "raised" not in divergence.requested


def test_sdk_fragments_are_ignored_without_changing_the_report(
    native_session: tuple[Path, Path, bool],
) -> None:
    folder, config, _ = native_session
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    record = loaded.value
    baseline = asyncio.run(replay_recording(record, config))
    assert isinstance(baseline, Ok)
    frames: list[DriveFrame] = []
    for exchange in record.frames:
        frames.extend(
            DriveFrame(
                t=exchange.t,
                kind=kind,
                register=None,
                value=None,
                ok=True,
                latency_ms=123,
                raw_hex="abcd",
            )
            for kind in sorted(SDK_KINDS)
        )
        frames.append(exchange)
    replayed = asyncio.run(replay_recording(replace(record, frames=tuple(frames)), config))
    assert isinstance(replayed, Ok)
    assert replayed.value.to_json() == baseline.value.to_json()


def test_native_record_without_structured_link_is_refused(
    native_session: tuple[Path, Path, bool],
    tmp_path: Path,
) -> None:
    folder, config, _ = native_session
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    document = dict(mapping(JSON.validate_json(config.read_bytes())))
    document.pop("native_drive")
    legacy = json.dumps(document).encode()
    path = tmp_path / "legacy-config.json"
    path.write_bytes(legacy)
    record = replace(
        loaded.value,
        manifest=replace(loaded.value.manifest, config_hash=hashlib.sha256(legacy).hexdigest()),
    )
    replayed = asyncio.run(replay_recording(record, path))
    assert isinstance(replayed, Err)
    assert (
        replayed.error.detail
        == "native Modbus replay requires native_drive in the verified configuration document"
    )


def test_changed_feedback_names_the_decision_in_the_tick(
    native_session: tuple[Path, Path, bool],
) -> None:
    folder, config, _ = native_session
    loaded = read(folder)
    assert isinstance(loaded, Ok)
    record = loaded.value
    frames = list(record.frames)
    index = next(
        i for i, exchange in enumerate(frames) if exchange.t > 2 and exchange.register == 8603
    )
    at = frames[index].t
    frames[index] = replace(frames[index], value=65500)
    stopping = next(i for i in range(index + 1, len(frames)) if frames[i].kind == "modbus_write")
    assert frames[stopping].register == 8601
    assert frames[stopping].t == at
    frames[stopping] = replace(frames[stopping], value=0)
    frames[stopping + 1] = replace(frames[stopping + 1], value=0)
    replayed = asyncio.run(replay_recording(replace(record, frames=tuple(frames)), config))
    assert isinstance(replayed, Ok)
    assert not replayed.value.matches
    first = replayed.value.comparison.first
    assert first is not None, replayed.value.to_text()
    assert first.t == pytest.approx(at)
