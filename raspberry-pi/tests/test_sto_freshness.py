from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from src import local_panel as panel_module
from src.local_panel import LocalPanel
from src.motor import sto as sto_module
from src.motor.drive import LOGIC_INPUTS_LOGICAL, CommTimeout, DriveError
from src.motor.simulated import SimulatedDrive
from src.motor.sto import (
    STO_POLL_PERIOD,
    STO_STALE_AFTER,
    DriveInputStoSource,
    StoReading,
    StoState,
)
from src.result import Err, Ok, Result
from src.training.runtime import RULE_STO_UNREADABLE
from src.training.safety import RULE_LOOP_STALL
from src.training.types import Occupancy, SafetyAction, TelemetrySnapshot
from src.units import OutputRpm, RawRegister, RegisterAddress, Seconds
from tests.test_failure_rig import OPERATOR, Rig, make_rig
from tests.test_local_panel import FakeWeb
from tests.test_sto_poll_guard import STO_ENV


@dataclass(frozen=True, slots=True)
class LiveSto:
    """Queues schedule real control cycles and poll attempts without timing races."""

    rig: Rig
    source: DriveInputStoSource
    ticks: asyncio.Queue[Seconds]
    completed: asyncio.Queue[TelemetrySnapshot]
    polls: asyncio.Queue[None]
    readings: asyncio.Queue[StoReading]

    async def step(self, interval: Seconds = Seconds(0.2)) -> TelemetrySnapshot:
        self.ticks.put_nowait(interval)
        return await asyncio.wait_for(self.completed.get(), 3)

    async def poll(self) -> StoReading:
        self.polls.put_nowait(None)
        return await asyncio.wait_for(self.readings.get(), 3)


@pytest.fixture
async def live_sto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LiveSto]:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    live = LiveSto(rig, source, asyncio.Queue(), asyncio.Queue(), asyncio.Queue(), asyncio.Queue())
    original_cycle = LocalPanel.control_cycle
    original_poll = DriveInputStoSource.poll

    async def controlled_cycle(self: LocalPanel) -> None:
        rig.clock.advance(await live.ticks.get())
        await original_cycle(self)
        live.completed.put_nowait(self.runtime.snapshot())

    async def controlled_poll(self: DriveInputStoSource) -> None:
        await live.polls.get()
        await original_poll(self)
        live.readings.put_nowait(self.latest(rig.clock.monotonic()))

    monkeypatch.setattr(LocalPanel, "control_cycle", controlled_cycle)
    monkeypatch.setattr(DriveInputStoSource, "poll", controlled_poll)
    monkeypatch.setattr(panel_module, "CONTROL_PERIOD", Seconds(0.001))
    monkeypatch.setattr(sto_module, "STO_POLL_PERIOD", Seconds(0.001))
    stop = asyncio.Event()
    runner = asyncio.create_task(panel.run(stop, FakeWeb()))
    try:
        await live.step()
        assert (await live.poll()).state is StoState.READY
        assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
        assert isinstance(
            panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        await live.step()
        await live.poll()
        assert isinstance(
            panel.surface.submit_manual_target(output_rpm=OutputRpm(3), operator=OPERATOR), Ok
        )
        for _ in range(30):
            await live.step()
            await live.poll()
        assert rig.simulator.commanded_setpoint > 0
        assert panel.runtime.standing is None
        yield live
    finally:
        stop.set()
        live.ticks.put_nowait(Seconds(0))
        await asyncio.wait_for(runner, 3)


async def test_control_pause_only_latches_loop_stall_and_sto_recovers_within_poll_period(
    live_sto: LiveSto, caplog: pytest.LogCaptureFixture
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    await live.step(Seconds(2.5))
    resumed = live.rig.clock.monotonic()
    assert live.source.latest(resumed).state is StoState.UNKNOWN
    # The next poll can fall late in its 0.5 s period after control resumes.
    for _ in range(2):
        await live.step()
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action) == (RULE_LOOP_STALL, SafetyAction.FREEZE)
    assert RULE_STO_UNREADABLE not in caplog.text
    refreshed = await live.poll()
    assert refreshed.state is StoState.READY
    assert refreshed.last_success_at is not None
    assert refreshed.last_success_at - resumed <= STO_POLL_PERIOD
    await live.step()
    assert runtime.standing == verdict


@pytest.mark.parametrize("hang", [False, True])
@pytest.mark.parametrize("pause_first", [False, True])
async def test_normal_control_latches_unreadable_sto_within_budget_and_one_tick(
    live_sto: LiveSto, monkeypatch: pytest.MonkeyPatch, hang: bool, pause_first: bool
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    if pause_first:
        await live.step(Seconds(2.5))
        assert (await live.poll()).state is StoState.READY
    last = live.source.latest(live.rig.clock.monotonic()).last_success_at
    assert last is not None
    entered = asyncio.Event()
    original = SimulatedDrive.read_register

    async def failed_register(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        if address == LOGIC_INPUTS_LOGICAL:
            entered.set()
            if hang:
                await asyncio.Event().wait()
            return Err(CommTimeout(after=Seconds(0.1)))
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", failed_register)
    await live.step()
    live.polls.put_nowait(None)
    await asyncio.wait_for(entered.wait(), 3)
    if not hang:
        assert (await asyncio.wait_for(live.readings.get(), 3)).state is StoState.UNKNOWN
    for _ in range(11):
        await live.step()
        standing = runtime.standing
        if standing is not None and standing.rule == RULE_STO_UNREADABLE:
            break
        if not hang:
            await live.poll()
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_UNREADABLE,
        SafetyAction.RAMP_DOWN,
        True,
    )
    assert live.rig.clock.monotonic() - last <= STO_STALE_AFTER + 0.2 + 1e-9
    supervisor = runtime.supervisor.standing
    if pause_first:
        assert supervisor is not None
        assert supervisor.rule == RULE_LOOP_STALL
    else:
        assert supervisor is None
    assert not runtime.silent
