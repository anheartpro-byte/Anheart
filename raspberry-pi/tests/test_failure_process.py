"""Process-level failures of the REAL console: the tick, the loop, the signals, the clock,
the web server and the dashboard link.

Each case runs a bench manual session at speed and breaks something around the
runtime rather than in the drive: an exception inside the tick, a loop that
stops ticking for longer than the drive's ``ttO``, SIGTERM, the wall clock
stepping, the web server dying, the dashboard link raising. The judgement is
the rig's: shaft at 0, output off or the drive's own watchdog engaged, the
right end reason, no NaN, and something the operator can read.

Two cases (SIGTERM, web failure) run :meth:`LocalPanel.run` itself, on the
real clock with a fake web server, so the task wiring that production uses is
what is exercised; the drive's ``ttO`` is shortened to keep them to seconds.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx
import pytest

from src.clock import RealClock
from src.cloud_sync import CloudError, CloudTransport, Document, Unreachable
from src.local_config import CloudConfig, LocalConfig
from src.local_panel import (
    EXIT_FAILED,
    EXIT_OK,
    CloudTransportFactory,
    DriveSide,
    LocalPanel,
    build_panel,
    install_stop_signals,
)
from src.motor.simulated import SimState, SimulatedDrive, SimulatedDriveConfig
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy, SafetyAction
from src.units import OutputRpm, Seconds
from tests.test_failure_rig import (
    BENCH_ENV,
    ENERGISED,
    OPERATOR,
    JumpClock,
    Rig,
    TickRaises,
    config_of,
    make_rig,
    no_dsp,
    start_manual,
)

CRUISE: Final[float] = 10.0
"""Output rpm (498 motor rpm): 40 s of climb at the motion limits."""

LINKED_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}


async def cruising(rig: Rig) -> None:
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE)
    await rig.tick(45.0)
    assert abs(rig.panel.runtime.snapshot().measured.motor_rpm - 498) <= 2


# =========================================================================
# The tick raising
# =========================================================================


async def test_an_exception_inside_the_tick_fails_closed_and_is_re_raised(tmp_path: Path) -> None:
    rig, wrapper = make_rig(tmp_path, wrap=TickRaises)
    assert isinstance(wrapper, TickRaises)
    await cruising(rig)
    wrapper.armed = True
    rig.clock.advance(Seconds(0.2))
    with pytest.raises(RuntimeError, match="a bug inside the tick"):
        await rig.panel.control_step()
    runtime = rig.panel.runtime
    assert runtime.silent
    assert runtime.end_reason is EndReason.TICK_EXCEPTION
    # The reference was zeroed synchronously on the way out.
    assert rig.simulator.commanded_setpoint == 0
    shown = runtime.snapshot()
    assert shown.safety is not None
    assert shown.safety.rule == "tick_exception"
    assert "a bug inside the tick" in shown.safety.detail
    # The loop is gone: the console exits without another frame.
    detail = await rig.panel.close()
    assert detail
    assert await rig.left_stopped() == ""
    assert rig.silent_backstop()


# =========================================================================
# The loop stalling
# =========================================================================


async def test_a_short_stall_freezes_the_setpoint(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    await cruising(rig)
    before = rig.panel.runtime.snapshot().setpoint.motor_rpm
    rig.clock.advance(Seconds(1.0))
    rig.simulator.advance(rig.clock.monotonic())
    last = await rig.tick(0.2)
    assert last.safety is not None
    assert last.safety.rule == "loop_stall"
    assert last.safety.action is SafetyAction.FREEZE
    assert last.setpoint.motor_rpm <= before
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_stall_longer_than_tto_is_stopped_by_the_drive_and_goes_silent(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path)
    await cruising(rig)
    rig.clock.advance(Seconds(5.0))
    rig.simulator.advance(rig.clock.monotonic())
    # The drive heard nothing for 5 s (ttO 3 s): its own watchdog has latched.
    assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
    last = await rig.tick(1.0)
    runtime = rig.panel.runtime
    assert runtime.silent
    rules = {s.safety.rule for s in rig.snapshots[-5:] if s.safety is not None}
    assert rules & {"loop_stall", "drive_fault"}, rules
    assert last.safety is not None
    assert last.safety.detail
    assert runtime.end_reason is EndReason.SAFETY_VERDICT
    await rig.panel.close()
    assert await rig.left_stopped() == ""
    assert rig.silent_backstop()


# =========================================================================
# The clock jumping
# =========================================================================


@pytest.mark.parametrize("jump_ms", [3_600_000, -3_600_000, 86_400_000 * 365])
async def test_a_wall_clock_step_changes_nothing_the_machine_does(
    tmp_path: Path, jump_ms: int
) -> None:
    clock = JumpClock()
    rig, _ = make_rig(tmp_path, clock=clock)
    await cruising(rig)
    held = rig.panel.runtime.snapshot().setpoint.motor_rpm
    clock.wall_offset_ms = jump_ms
    last = await rig.tick(20.0)
    assert last.safety is None, last.safety
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    assert last.setpoint.motor_rpm == held
    async with rig.http() as session:
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == httpx.codes.ACCEPTED
    await rig.tick(60.0)
    assert rig.panel.runtime.end_reason is EndReason.OPERATOR_STOP
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_monotonic_jump_forward_is_a_stall_not_a_speed_change(tmp_path: Path) -> None:
    """A suspended VM or an NTP-slewed host: 20 s pass between two ticks."""
    clock = JumpClock()
    rig, _ = make_rig(tmp_path, clock=clock)
    await cruising(rig)
    clock.advance(Seconds(20.0))
    last = await rig.tick(0.2)
    assert rig.panel.runtime.silent
    assert last.safety is not None
    assert last.safety.action is SafetyAction.GO_SILENT
    await rig.panel.close()
    assert await rig.left_stopped() == ""


# =========================================================================
# The dashboard link failing
# =========================================================================


class RaisingTransport:
    """A dashboard transport with a bug: every call raises."""

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        raise RuntimeError(f"bug in the link: GET {path} {params}")

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        raise RuntimeError(f"bug in the link: POST {path} {len(body)}")


class DownTransport:
    """No network: every call is ``Unreachable``."""

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        del path, params
        return Err(Unreachable("no route to host"))

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        del path, body
        return Err(Unreachable("no route to host"))


def _raising(_config: CloudConfig) -> CloudTransport:
    return RaisingTransport()


def _down(_config: CloudConfig) -> CloudTransport:
    return DownTransport()


LINKS: Final[Mapping[str, CloudTransportFactory]] = {"raising": _raising, "unreachable": _down}


@pytest.mark.parametrize("link", list(LINKS))
async def test_a_failing_dashboard_link_never_touches_the_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, link: str
) -> None:
    rig, _ = make_rig(tmp_path, env=LINKED_ENV, transport=LINKS[link])
    assert rig.panel.cloud is not None
    await cruising(rig)
    held = rig.panel.runtime.snapshot().setpoint.motor_rpm
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            await rig.tick(1.0)
            await rig.panel.cloud_step()
    last = rig.panel.runtime.snapshot()
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    assert last.safety is None
    assert last.setpoint.motor_rpm == held
    if link == "raising":
        assert "dashboard link step failed" in caplog.text
    async with rig.http() as session:
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == httpx.codes.ACCEPTED
    await rig.tick(60.0)
    await rig.panel.cloud_step()
    assert rig.panel.runtime.end_reason is EndReason.OPERATOR_STOP
    await rig.panel.close()
    assert await rig.left_stopped() == ""


# =========================================================================
# LocalPanel.run itself: SIGTERM, and the web server dying
# =========================================================================

FAST_TTO: Final[Seconds] = Seconds(1.0)


class FakeWeb:
    """A :class:`~src.local_panel.WebRunner` that serves nothing, or dies after ``fail_after``."""

    def __init__(self, fail_after: float | None) -> None:
        self.fail_after: float | None = fail_after
        self.exit_requested: asyncio.Event = asyncio.Event()

    async def serve(self) -> None:
        if self.fail_after is None:
            await self.exit_requested.wait()
            return
        await asyncio.sleep(self.fail_after)
        raise OSError("[Errno 48] address already in use")

    def request_exit(self) -> None:
        self.exit_requested.set()


def real_panel(tmp_path: Path) -> tuple[LocalPanel, SimulatedDrive, LocalConfig]:
    clock = RealClock()
    config = config_of(BENCH_ENV)
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True, tto=FAST_TTO))
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=sim, simulator=sim, release=lambda: None),
    )
    return panel, sim, config


async def operate(panel: LocalPanel, then: float) -> None:
    """Start a bench session at 2 output rpm through the surface, then wait ``then`` s."""
    surface = panel.surface
    await asyncio.sleep(0.3)
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await asyncio.sleep(0.5)
    surface.note_presence(OPERATOR)
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(2.0), operator=OPERATOR), Ok
    )
    for _ in range(round(then / 0.2)):
        surface.note_presence(OPERATOR)
        await asyncio.sleep(0.2)


async def left_stopped_real(sim: SimulatedDrive) -> str:
    """After the run: wait out the drive's ramp and ttO on the real clock, then probe."""
    await asyncio.sleep(float(FAST_TTO) + 1.5)
    sim.advance(RealClock().monotonic())
    problems: list[str] = []
    if sim.sim_state in ENERGISED:
        problems.append(f"energised in {sim.sim_state.name}")
    await sim.open()
    status = await sim.read_status()
    if isinstance(status, Ok):
        if abs(status.value.output_rpm) >= 1:
            problems.append(f"shaft at {status.value.output_rpm}")
    else:
        problems.append(repr(status.error))
    return "; ".join(problems)


async def test_sigterm_mid_session_shuts_the_drive_down_and_exits_cleanly(tmp_path: Path) -> None:
    panel, sim, _ = real_panel(tmp_path)
    stop = asyncio.Event()
    undo = install_stop_signals(stop)

    async def operator_then_sigterm() -> float:
        await operate(panel, 2.0)
        turning = float(panel.runtime.snapshot().measured.motor_rpm)
        signal.raise_signal(signal.SIGTERM)
        return turning

    try:
        turning, code = await asyncio.gather(
            operator_then_sigterm(), panel.run(stop, FakeWeb(None))
        )
    finally:
        undo()
    assert turning >= 55, f"the session never got moving ({turning} rpm)"
    assert code == EXIT_OK
    runtime = panel.runtime
    assert runtime.end_reason is EndReason.SHUTDOWN
    assert await left_stopped_real(sim) == ""


async def test_the_web_server_dying_stops_the_console_and_the_machine(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    panel, sim, _ = real_panel(tmp_path)
    stop = asyncio.Event()

    async def operator() -> float:
        await operate(panel, 1.5)
        return float(panel.runtime.snapshot().measured.motor_rpm)

    turning, code = await asyncio.gather(operator(), panel.run(stop, FakeWeb(2.5)))
    assert turning >= 55, f"the session never got moving ({turning} rpm)"
    assert code == EXIT_FAILED
    assert stop.is_set()
    assert "address already in use" in caplog.text, "the failure is not reported"
    assert panel.runtime.end_reason is EndReason.SHUTDOWN
    assert await left_stopped_real(sim) == ""
