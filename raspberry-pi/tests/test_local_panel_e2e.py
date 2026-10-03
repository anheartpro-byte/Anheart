"""End to end, in simulation: the real composition root, milestone M1 (read-only).

Plan scenario 1: at rest, the ECG, a heart rate and 0 rpm reach the page's
WebSocket, and the drive receives **no write of any kind** - only reads.

Everything is real except the two devices: :func:`src.local_panel.build_panel`
wires the runtime, the shared supervisor, the control surface, the telemetry
hub, the web app, the REAL ``SignalTreatment`` DSP and the ECG bridge, over a
``SimulatedDrive`` and a ``SimulatedBitalinoClient`` driven by the physiology
plant, on a ``ManualClock``. The socket is spoken to through raw ASGI, as in
``tests/test_web_api.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from http import HTTPStatus
from itertools import pairwise
from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import TypeAdapter

from src.clock import ManualClock
from src.local_config import LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel
from src.motor.drive import ControlWord, DriveError, DriveState, EmergencyStopOutcome
from src.motor.simulated import SimulatedDrive
from src.result import Ok, Result
from src.training.runtime import RuntimeState
from src.training.types import SafetyAction, TelemetrySnapshot
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from src.web.app import create_app
from src.web.schemas import EcgRow, SnapshotRow, WsEnvelope
from tests.test_web_api import probe_socket

SIM_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
}

TICK: Final[Seconds] = Seconds(0.2)


def sim_config() -> LocalConfig:
    loaded = load_local_config(SIM_ENV)
    assert isinstance(loaded, Ok)
    return loaded.value


_ENVELOPE: Final[TypeAdapter[WsEnvelope]] = TypeAdapter(WsEnvelope)


def record_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every write the simulated drive offers with a recorder that also fails."""
    writes: list[str] = []

    async def write_speed(_self: SimulatedDrive, rpm: MotorRpm) -> Result[None, DriveError]:
        writes.append(f"lfrd:{rpm}")
        raise AssertionError("the read-only console wrote a speed")

    async def write_command(_self: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
        writes.append(f"cmd:{word.name}")
        raise AssertionError("the read-only console wrote a command word")

    def emergency(_self: SimulatedDrive, timeout: Seconds) -> EmergencyStopOutcome:
        writes.append(f"estop:{timeout}")
        raise AssertionError("the read-only console sent an emergency zero")

    monkeypatch.setattr(SimulatedDrive, "write_speed", write_speed)
    monkeypatch.setattr(SimulatedDrive, "write_command", write_command)
    monkeypatch.setattr(SimulatedDrive, "emergency_disable_blocking", emergency)
    return writes


async def _run(panel: LocalPanel, clock: ManualClock, seconds: float) -> None:
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        await panel.ecg_step()
        await panel.control_step()


def _frames(sent: tuple[MutableMapping[str, object], ...]) -> list[WsEnvelope]:
    """Every frame the socket sent, validated against the envelope the page parses."""
    frames: list[WsEnvelope] = []
    for message in sent:
        if message["type"] != "websocket.send":
            continue
        text = message["text"]
        assert isinstance(text, str)
        frames.append(_ENVELOPE.validate_json(text))
    return frames


async def test_at_rest_ecg_heart_rate_and_zero_rpm_reach_the_socket_with_no_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = record_writes(monkeypatch)
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    config = sim_config()
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "profiles.json")

    # One supervisor, shared: the web e-stop latches the runtime's own.
    assert panel.surface is not None
    assert panel.services.supervisor is panel.runtime.supervisor
    # Motion is possible (manual, M3) but nothing asked for it; programmes are off.
    assert panel.services.motion_enabled
    assert not panel.services.programs_enabled

    await _run(panel, clock, 15.0)

    assert writes == []
    runtime = panel.runtime
    assert runtime.state is RuntimeState.IDLE
    assert runtime.idle_link.reads >= 25  # 2 Hz for 15 s, minus the start
    assert runtime.idle_link.failures == 0

    snapshot = runtime.snapshot()
    assert snapshot.measured.motor_rpm == 0
    assert snapshot.drive_state is not DriveState.COMM_LOST
    assert snapshot.drive_state is not DriveState.FAULT
    assert snapshot.fault is None
    assert snapshot.live_bpm is not None

    status = panel.reporter.panel_status()
    assert status.motion_enabled
    assert not status.programs_enabled
    assert status.ecg.acquiring
    assert status.ecg.link is None
    assert status.ecg.bridge.batches >= 70
    assert status.drive_link is None

    app = create_app(services=panel.services, config=config.web)
    sent = await probe_socket(app, origin=None, token=None, replies=2)
    frames = _frames(sent)
    ecgs = [frame.ecg for frame in frames if frame.ecg is not None]
    rows = [frame.snapshot for frame in frames if frame.snapshot is not None]
    assert ecgs, "no ECG frame reached the socket"
    assert rows, "no snapshot frame reached the socket"
    ecg: EcgRow = ecgs[0]
    assert len(ecg.samples) > 1000  # the 6 s ring, at 250 Hz
    assert ecg.fs_hz == 250

    row: SnapshotRow = rows[0]
    assert row.live_bpm is not None
    assert row.measured.motor_rpm == 0
    assert row.measured.output_rpm == 0
    assert row.measured.g_load == 0
    assert row.measured.resultant_g == pytest.approx(1.0)

    # Exit: nothing was ever started, so the link is released without a write.
    detail = await panel.close()
    assert "without a write" in detail
    assert writes == []


async def test_a_start_from_the_mailbox_is_refused_by_the_loop(tmp_path: Path) -> None:
    """The second gate behind the 403: a start that reached the mailbox starts nothing."""
    clock = ManualClock()
    panel = build_panel(sim_config(), clock=clock, profiles_path=tmp_path / "profiles.json")
    assert isinstance(panel.surface.attest_estop_wiring("dr. attending"), Ok)
    submitted = panel.surface.submit_start(
        profile_id="standard_30_min", operator="dr. attending", total_duration_s=None
    )
    assert isinstance(submitted, Ok)
    clock.advance(TICK)
    await panel.control_step()
    assert panel.surface.pending is None
    assert panel.runtime.state is RuntimeState.IDLE
    assert panel.surface.run_state.value == "idle"
    await panel.close()


# =========================================================================
# Milestone M3: supervised manual, nobody on board (plan scenarios 2 to 4)
# =========================================================================

OPERATOR: Final[str] = "dr. attending"
RATIO: Final[float] = 49.79
MIN_RUN: Final[int] = 55
RATE: Final[float] = 0.25 * RATIO
"""The motion limits of config/motion_limits.json, in motor rpm per second."""


def _client(panel: LocalPanel) -> httpx.AsyncClient:
    app = create_app(services=panel.services, config=sim_config().web)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _ticks(panel: LocalPanel, clock: ManualClock, seconds: float) -> list[TelemetrySnapshot]:
    """Tick the console as its loop does, with the attendant's page pinging."""
    snapshots: list[TelemetrySnapshot] = []
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        snapshots.append(await panel.control_step())
    return snapshots


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _conforms(setpoints: list[int]) -> None:
    """The domain, and the motion limits between changes (+1 carried rpm)."""
    last = 0
    for index, (before, after) in enumerate(pairwise(setpoints), start=1):
        assert after == 0 or after >= MIN_RUN, after
        if after == before:
            continue
        if {before, after} != {0, MIN_RUN}:
            window = min((index - last) * TICK, 1.0 + 1.0 / RATE)
            assert abs(after - before) <= RATE * window + 1 + 1e-9, (before, after)
        last = index


async def _started(session: httpx.AsyncClient, panel: LocalPanel, clock: ManualClock) -> None:
    attested = await session.post(
        "/api/safety/attest",
        json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
    )
    assert attested.status_code == HTTPStatus.OK
    started = await session.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    await _ticks(panel, clock, 1.0)
    assert panel.runtime.state is RuntimeState.RUNNING
    assert panel.surface.run_state.value == "running"


async def _target(session: httpx.AsyncClient, motor_rpm: int) -> None:
    response = await session.post(
        "/api/manual/target", json={"output_rpm": motor_rpm / RATIO, "operator": OPERATOR}
    )
    assert response.status_code == HTTPStatus.ACCEPTED, response.text


def _panel(tmp_path: Path) -> tuple[LocalPanel, ManualClock]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    return build_panel(sim_config(), clock=clock, profiles_path=tmp_path / "p.json"), clock


async def test_manual_0_300_150_0_ramps_conform_then_stop_zeroes_the_target(
    tmp_path: Path,
) -> None:
    """Scenario 2: 0 -> 300 -> 150 -> 0 motor rpm, then STOP: target 0, back to REPOS."""
    panel, clock = _panel(tmp_path)
    simulator = panel.drive.simulator
    assert simulator is not None
    async with _client(panel) as session:
        await _started(session, panel, clock)
        trace: list[int] = [0]
        await _target(session, 300)
        climb = await _ticks(panel, clock, 25.0)
        trace += _setpoints(climb)
        assert trace[-1] == 300
        assert abs(climb[-1].measured.motor_rpm - 300) <= 2
        row = SnapshotRow.of(climb[0])
        assert row.mode == "manuel"
        assert row.manual is not None
        assert row.manual.ramping
        assert row.manual.occupancy_label == "BANC - personne a bord : NON"
        await _target(session, 150)
        trace += _setpoints(await _ticks(panel, clock, 15.0))
        assert trace[-1] == 150
        await _target(session, 0)
        trace += _setpoints(await _ticks(panel, clock, 15.0))
        assert trace[-1] == 0
        # Moving again, then the operator's STOP mid-climb.
        await _target(session, 300)
        trace += _setpoints(await _ticks(panel, clock, 8.0))
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == HTTPStatus.ACCEPTED
        ending = await _ticks(panel, clock, 1.0)
        assert panel.runtime.manual_target == 0
        assert ending[-1].mode.value == "arret"
        late = await session.post(
            "/api/manual/target", json={"output_rpm": 1.0, "operator": OPERATOR}
        )
        # Accepted by the mailbox or refused as stopping; either way nothing moves up.
        assert late.status_code in (HTTPStatus.ACCEPTED, HTTPStatus.CONFLICT)
        down = await _ticks(panel, clock, 30.0)
        trace += _setpoints([*ending, *down])
    _conforms(trace)
    assert panel.runtime.state is RuntimeState.FINISHED
    assert down[-1].mode.value == "repos"
    assert panel.surface.run_state.value == "idle"
    assert panel.runtime.manual_target == 0
    assert simulator.commanded_setpoint == 0
    assert abs(down[-1].measured.motor_rpm) < 1
    assert not panel.runtime.output_enabled
    await panel.close()


async def test_a_web_estop_during_a_ramp_latches_before_the_tick_and_nothing_resumes(
    tmp_path: Path,
) -> None:
    """Scenario 3: the e-stop latches the ONE supervisor at once; the next tick zeroes."""
    panel, clock = _panel(tmp_path)
    simulator = panel.drive.simulator
    assert simulator is not None
    async with _client(panel) as session:
        await _started(session, panel, clock)
        await _target(session, 300)
        await _ticks(panel, clock, 8.0)
        assert panel.runtime.applied_rpm > MIN_RUN
        estop = await session.post("/api/session/estop", json={})
        assert estop.status_code == HTTPStatus.OK
        # Before any tick: the runtime's own supervisor stands at QUICK_STOP.
        assert panel.runtime.standing_action is SafetyAction.QUICK_STOP
        assert panel.surface.estop_latched
        after = await _ticks(panel, clock, 0.2)
        assert simulator.commanded_setpoint == 0
        assert panel.runtime.manual_target == 0
        refused = await session.post(
            "/api/manual/target", json={"output_rpm": 3.0, "operator": OPERATOR}
        )
        assert refused.status_code == HTTPStatus.CONFLICT
        settle = await _ticks(panel, clock, 10.0)
        assert set(_setpoints([*after, *settle])) == {0}
        assert abs(settle[-1].measured.motor_rpm) < 1
        ack = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert ack.status_code == HTTPStatus.OK
        rest = await _ticks(panel, clock, 10.0)
    assert set(_setpoints(rest)) == {0}
    assert panel.runtime.mode.value == "repos"
    assert panel.surface.run_state.value == "idle"
    await panel.close()


async def test_a_comms_loss_goes_silent_and_nothing_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4: the link dies mid-rotation; GO_SILENT; the drive's ttO stops it."""
    panel, clock = _panel(tmp_path)
    simulator = panel.drive.simulator
    assert simulator is not None
    async with _client(panel) as session:
        await _started(session, panel, clock)
        await _target(session, 250)
        await _ticks(panel, clock, 20.0)
        simulator.inject_comms_loss(Seconds(30.0))
        await _ticks(panel, clock, 2.0)
        assert panel.runtime.silent
        assert panel.runtime.standing_action is SafetyAction.GO_SILENT
        writes = record_writes(monkeypatch)
        await _ticks(panel, clock, 40.0)  # the link comes back after 30 s
        assert writes == []
        # The drive's own timeout (SLF) brought the shaft down; read here by the
        # test, never by the silent runtime.
        status = await simulator.read_status()
        assert isinstance(status, Ok)
        assert status.value.state is DriveState.FAULT
        assert status.value.output_rpm == 0
        assert status.value.fault_code is not None
        restart = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert restart.status_code == HTTPStatus.CONFLICT
        reset = await session.post("/api/drive/fault-reset", json={"operator": OPERATOR})
        assert reset.status_code == HTTPStatus.ACCEPTED
        await _ticks(panel, clock, 1.0)
        assert writes == []
    assert panel.runtime.silent
    await panel.close()


async def test_occupied_is_refused_by_configuration(tmp_path: Path) -> None:
    panel, _ = _panel(tmp_path)
    async with _client(panel) as session:
        refused = await session.post(
            "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
        )
    assert refused.status_code == HTTPStatus.FORBIDDEN
    assert "OCCUPANCY_OCCUPIED_ENABLED" in refused.json()["detail"]
    assert panel.surface.pending is None
