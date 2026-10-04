"""The camera fail-safe, wired into the real console (``PRESENCE_SOURCE``).

The rules themselves are tested in ``tests/test_presence_*.py``; this file
tests the wiring: the camera is off unless asked for, it gates every start
(manual and programmed), and an intrusion while the arm turns brings the
simulated motor to zero through the real runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from pydantic import TypeAdapter

from src.clock import ManualClock
from src.local_config import CameraSource, LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel, build_presence
from src.presence.adapter import PRESENCE_PERIOD
from src.presence.simulated import Freeze, Intrusion, SimulatedCamera
from src.presence.types import Confidence
from src.result import Err, Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, UnixMillis
from src.web.app import create_app
from src.web.schemas import CameraRow

OPERATOR: Final[str] = "dr. attending"
CAMERA_ROW: Final[TypeAdapter[CameraRow]] = TypeAdapter(CameraRow)
ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "PROGRAMS_ENABLED": "true",
    "OCCUPANCY_OCCUPIED_ENABLED": "true",
}


def config_of(**changes: str) -> LocalConfig:
    loaded = load_local_config({**ENV, **changes})
    assert isinstance(loaded, Ok), loaded
    return loaded.value


def panel_of(tmp_path: Path, camera: str) -> tuple[LocalPanel, ManualClock]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(
        config_of(PRESENCE_SOURCE=camera), clock=clock, profiles_path=tmp_path / "p.json"
    )
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    return panel, clock


async def run(panel: LocalPanel, clock: ManualClock, seconds: float) -> None:
    """The console's steps as its loop interleaves them: camera at 20 Hz, control at 5 Hz."""
    for n in range(round(seconds / PRESENCE_PERIOD)):
        clock.advance(PRESENCE_PERIOD)
        panel.surface.note_presence(OPERATOR)
        await panel.presence_step()
        if n % 4 == 3:
            await panel.ecg_step()
            await panel.control_step()


def state_of(panel: LocalPanel) -> RuntimeState:
    return panel.runtime.state


def camera_of(panel: LocalPanel) -> SimulatedCamera:
    guard = panel.presence
    assert guard is not None
    source = guard.source
    assert isinstance(source, SimulatedCamera)
    return source


def test_no_camera_unless_asked_for() -> None:
    assert config_of().camera is CameraSource.NONE
    loaded = load_local_config({**ENV, "PRESENCE_SOURCE": "webcam"})
    assert isinstance(loaded, Err)
    assert [p.key for p in loaded.error] == ["PRESENCE_SOURCE"]


async def test_an_unconfigured_camera_changes_nothing(tmp_path: Path) -> None:
    panel, clock = panel_of(tmp_path, "none")
    assert panel.presence is None
    await panel.presence_step()
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 1.0)
    assert state_of(panel) is RuntimeState.RUNNING
    await panel.close()


async def test_a_start_before_the_zone_has_been_seen_clear_is_refused(tmp_path: Path) -> None:
    panel, clock = panel_of(tmp_path, "sim_empty")
    assert panel.presence is not None
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 0.2)
    assert state_of(panel) is RuntimeState.IDLE
    await panel.close()


async def test_an_intrusion_while_turning_stops_the_motor(tmp_path: Path) -> None:
    panel, clock = panel_of(tmp_path, "sim_empty")
    await run(panel, clock, 2.5)  # the zone seen clear long enough
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 1.0)
    assert state_of(panel) is RuntimeState.RUNNING
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR), Ok
    )
    await run(panel, clock, 15.0)
    assert panel.runtime.snapshot().measured.motor_rpm > 50

    camera_of(panel).schedule(Intrusion(start=clock.monotonic(), confidence=Confidence(0.95)))
    await run(panel, clock, 20.0)
    snapshot = panel.runtime.snapshot()
    assert snapshot.measured.motor_rpm == 0
    assert not panel.runtime.output_enabled
    standing = panel.runtime.supervisor.standing
    assert standing is not None
    assert standing.latched
    await panel.close()


async def test_a_programme_needs_a_rider_seen_in_the_capsule(tmp_path: Path) -> None:
    empty, clock = panel_of(tmp_path, "sim_empty")
    await run(empty, clock, 2.5)
    assert isinstance(
        empty.surface.submit_start(
            profile_id="standard_30_min", operator=OPERATOR, total_duration_s=None, subject_age=30
        ),
        Ok,
    )
    await run(empty, clock, 1.0)
    assert state_of(empty) is RuntimeState.IDLE
    await empty.close()

    occupied, clock = panel_of(tmp_path / "b", "sim_occupied")
    await run(occupied, clock, 2.5)
    assert isinstance(
        occupied.surface.submit_start(
            profile_id="standard_30_min", operator=OPERATOR, total_duration_s=None, subject_age=30
        ),
        Ok,
    )
    await run(occupied, clock, 1.0)
    assert state_of(occupied) is RuntimeState.RUNNING
    await occupied.close()


def test_an_unknown_camera_source_fails_loudly() -> None:
    """The ``assert_never`` guard: a new source must be wired, not fall through."""
    config = replace(config_of(), camera=cast("CameraSource", "webcam"))
    clock = ManualClock()
    with pytest.raises(AssertionError):
        build_presence(config, clock, build_panel(config_of(), clock=clock).runtime)


async def camera_row(panel: LocalPanel) -> CameraRow:
    app = create_app(services=panel.services, config=config_of().web)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/camera")
    assert response.status_code == 200
    return CAMERA_ROW.validate_json(response.text)


async def test_the_page_is_told_there_is_no_camera(tmp_path: Path) -> None:
    panel, _clock = panel_of(tmp_path, "none")
    row = await camera_row(panel)
    assert (row.configured, row.state, row.latched_rule) == (False, "absent", None)
    await panel.close()


async def test_the_page_follows_the_camera_s_decisions(tmp_path: Path) -> None:
    panel, clock = panel_of(tmp_path, "sim_empty")
    guard = panel.presence
    assert guard is not None
    assert guard.last_decision is None
    assert (await camera_row(panel)).state == "waiting"
    await run(panel, clock, 2.5)
    row = await camera_row(panel)
    assert (row.state, row.camera, row.detail) == ("clear", "sim_empty", "")

    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 1.0)
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR), Ok
    )
    await run(panel, clock, 15.0)
    camera_of(panel).schedule(Intrusion(start=clock.monotonic(), confidence=Confidence(0.95)))
    await run(panel, clock, 0.5)
    row = await camera_row(panel)
    assert row.state == "emergency_stop"  # the arm is still coasting down
    assert row.latched_rule == "presence_intrusion"
    await panel.close()


async def test_a_frozen_camera_ramps_down_and_the_page_says_so(tmp_path: Path) -> None:
    panel, clock = panel_of(tmp_path, "sim_empty")
    await run(panel, clock, 2.5)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 1.0)
    camera_of(panel).schedule(Freeze(start=clock.monotonic()))
    await run(panel, clock, 1.5)
    row = await camera_row(panel)
    # The controlled stop has already happened: at rest, the latched verdict now
    # blocks any start, and the page says why.
    assert row.state == "start_blocked"
    assert row.latched_rule == "presence_camera_lost"
    assert row.detail
    await panel.close()
