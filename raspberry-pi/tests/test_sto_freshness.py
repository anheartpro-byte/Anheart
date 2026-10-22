from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from src import local_panel as panel_module
from src.local_panel import LocalPanel
from src.motor import sto as sto_module
from src.motor.drive import LOGIC_INPUTS_LOGICAL, CommTimeout, DriveError, DriveState
from src.motor.simulated import SimulatedDrive
from src.motor.sto import (
    STO_POLL_PERIOD,
    STO_STALE_AFTER,
    DriveInputStoSource,
    StoReading,
    StoState,
)
from src.result import Err, Ok, Result
from src.training.runtime import RULE_STO_UNREADABLE, EndReason
from src.training.safety import RULE_LOOP_STALL
from src.training.types import Occupancy, Phase, SafetyAction, TelemetrySnapshot
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


@pytest.mark.parametrize(
    ("intervals", "deadline"),
    [
        ((0.7,) * 4, Seconds(2.8)),
        ((0.2, 0.7, 0.7, 0.7, 0.7), Seconds(2.3)),
        ((0.2, 0.2, 0.2, 0.2, 0.2, 2.5), Seconds(3.5)),
        ((0.2, 0.2, 0.2, 0.8) * 2, Seconds(2.8)),
    ],
    ids=["every_tick_0_7s", "slow_ticks_after_failure", "single_pause", "interleaved_pauses"],
)
async def test_failed_sto_reads_are_not_excused_by_control_pauses(
    live_sto: LiveSto,
    monkeypatch: pytest.MonkeyPatch,
    intervals: tuple[float, ...],
    deadline: Seconds,
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    clock = live.rig.clock
    last = live.source.latest(clock.monotonic()).last_success_at
    assert last is not None
    original = SimulatedDrive.read_register
    failures = 0

    async def failed_read(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        nonlocal failures
        if address == LOGIC_INPUTS_LOGICAL:
            failures += 1
            return Err(CommTimeout(after=Seconds(0.1)))
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", failed_read)
    for interval in intervals:
        snapshot = await live.step(Seconds(interval))
        assert snapshot.drive_state is DriveState.OPERATION_ENABLED
        assert snapshot.measured.motor_rpm > 0
        assert not runtime.silent
        verdict = runtime.standing
        if verdict is not None and verdict.rule == RULE_STO_UNREADABLE:
            break
        reading = await live.poll()
        assert reading.state is StoState.UNKNOWN
        assert reading.last_success_at == last
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_UNREADABLE,
        SafetyAction.RAMP_DOWN,
        True,
    )
    assert failures > 0
    assert clock.monotonic() - last <= deadline + 1e-9
    assert runtime.end_reason is EndReason.SAFETY_VERDICT
    for _ in range(100):
        await live.step()
        if runtime.snapshot().phase is Phase.DONE:
            break
    assert runtime.snapshot().phase is Phase.DONE
    assert runtime.applied_rpm == 0
    assert live.rig.simulator.commanded_setpoint == 0


@pytest.mark.parametrize("hang", [False, True], ids=["failed", "pending"])
async def test_pause_with_an_unresolved_sto_attempt_counts_toward_freshness(
    live_sto: LiveSto, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    entered = asyncio.Event()
    original = SimulatedDrive.read_register

    async def unresolved_read(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        if address == LOGIC_INPUTS_LOGICAL:
            entered.set()
            if hang:
                await asyncio.Event().wait()
            return Err(CommTimeout(after=Seconds(0.1)))
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", unresolved_read)
    await live.step()
    live.polls.put_nowait(None)
    await asyncio.wait_for(entered.wait(), 3)
    if not hang:
        assert (await asyncio.wait_for(live.readings.get(), 3)).state is StoState.UNKNOWN
    await live.step(Seconds(2.5))
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action) == (RULE_STO_UNREADABLE, SafetyAction.RAMP_DOWN)
    assert runtime.end_reason is EndReason.SAFETY_VERDICT
    supervisor = runtime.supervisor.standing
    assert supervisor is not None
    assert supervisor.rule == RULE_LOOP_STALL
    assert not runtime.silent


async def test_success_restores_pause_deduction_after_a_failed_attempt(
    live_sto: LiveSto, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    original = SimulatedDrive.read_register

    async def failed_read(
        _drive: SimulatedDrive, _address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        return Err(CommTimeout(after=Seconds(0.1)))

    monkeypatch.setattr(SimulatedDrive, "read_register", failed_read)
    await live.step()
    assert (await live.poll()).state is StoState.UNKNOWN
    monkeypatch.setattr(SimulatedDrive, "read_register", original)
    await live.step()
    assert (await live.poll()).state is StoState.READY
    # Each success resets both the failed attempt and the previous pause credit.
    for _ in range(3):
        await live.step(Seconds(2.5))
        await live.step()
        verdict = runtime.standing
        assert verdict is not None
        assert (verdict.rule, verdict.action) == (RULE_LOOP_STALL, SafetyAction.FREEZE)
        assert (await live.poll()).state is StoState.READY
    assert RULE_STO_UNREADABLE not in caplog.text


async def test_success_during_pause_starts_its_own_freshness_budget(
    live_sto: LiveSto, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = live_sto
    clock = live.rig.clock
    runtime = live.rig.panel.runtime
    # The last tick's token can still complete a successful read during a pause.
    await live.step()
    clock.advance(Seconds(2.4))
    reading = await live.poll()
    last = reading.last_success_at
    assert last == clock.monotonic()
    assert reading.state is StoState.READY
    await live.step(Seconds(0.1))
    original = SimulatedDrive.read_register

    async def failed_read(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        if address == LOGIC_INPUTS_LOGICAL:
            return Err(CommTimeout(after=Seconds(0.1)))
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", failed_read)
    assert (await live.poll()).state is StoState.UNKNOWN
    for _ in range(11):
        await live.step(Seconds(0.19))
        verdict = runtime.standing
        if verdict is not None and verdict.rule == RULE_STO_UNREADABLE:
            break
        await live.poll()
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action) == (RULE_STO_UNREADABLE, SafetyAction.RAMP_DOWN)
    assert last is not None
    assert clock.monotonic() - last <= STO_STALE_AFTER + 0.19 + 1e-9


@pytest.mark.parametrize("hang", [False, True], ids=["failed", "pending"])
async def test_earned_pause_credit_cannot_grow_after_an_unresolved_attempt(
    live_sto: LiveSto, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    live = live_sto
    runtime = live.rig.panel.runtime
    clock = live.rig.clock
    last = live.source.latest(clock.monotonic()).last_success_at
    assert last is not None
    await live.step(Seconds(0.8))
    entered = asyncio.Event()
    original = SimulatedDrive.read_register

    async def unresolved_read(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        if address == LOGIC_INPUTS_LOGICAL:
            entered.set()
            if hang:
                await asyncio.Event().wait()
            return Err(CommTimeout(after=Seconds(0.1)))
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", unresolved_read)
    began = clock.monotonic()
    live.polls.put_nowait(None)
    await asyncio.wait_for(entered.wait(), 3)
    if not hang:
        assert (await asyncio.wait_for(live.readings.get(), 3)).state is StoState.UNKNOWN
    # No further attempts: even later pauses must count after this unresolved read.
    for _ in range(2):
        await live.step(Seconds(0.7))
        verdict = runtime.standing
        assert verdict is not None
        assert verdict.rule == RULE_LOOP_STALL
    await live.step(Seconds(0.7))
    verdict = runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action) == (RULE_STO_UNREADABLE, SafetyAction.RAMP_DOWN)
    assert clock.monotonic() - began <= STO_STALE_AFTER + 0.7 + 1e-9
    assert clock.monotonic() - last <= STO_STALE_AFTER + 0.8 + 0.7 + 1e-9
