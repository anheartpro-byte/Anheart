"""A stop keeps the link: what counts as one, and what withholding reads costs nothing.

The console starts no STO read while a stop is under way or asked for
(``LocalPanel._stop_pending``, from ``TrainingRuntime.stop_in_progress``), so
no stop write ever waits behind a new read. The time it withholds is not held
against the STO reading's freshness, unless a read was already left unresolved.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.local_panel import build_sto
from src.motor.atv320 import AUXILIARY_READ_BUDGET
from src.motor.drive import DriveFault, DriveState, LogicInput, RegisterMap
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import DriveInputStoSource, StoReading, StoSourceKind, StoState
from src.presence.monitor import RULE_PRESENCE_INTRUSION, RampDown
from src.presence.simulated import Dropout, SimulatedCamera
from src.result import Ok
from src.training.runtime import (
    RULE_STO_OPEN,
    RULE_STO_UNREADABLE,
    RuntimeState,
    TrainingRuntime,
)
from src.training.safety import RULE_HR_RATE, RULE_OPERATOR_ESTOP
from src.training.types import Occupancy, Phase, SafetyAction
from src.units import Monotonic, MotorRpm, OutputRpm, RawRegister, Seconds
from tests.test_atv320 import FakeBus, build_drive
from tests.test_failure_rig import OPERATOR, config_of, make_rig
from tests.test_runtime import Rig
from tests.test_runtime import sto_rig as runtime_rig
from tests.test_sto import RegisterPort
from tests.test_sto_poll_guard import STO_ENV


def stopping(rig: Rig) -> bool:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return rig.runtime.stop_in_progress


async def cruising(rig: Rig) -> None:
    """A manual bench session, its setpoint above zero, no verdict."""
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert not stopping(rig)  # at rest nothing is commanded
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert not stopping(rig)  # armed, target 0, setpoint 0
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(3)), Ok)
    await rig.run(4)
    assert rig.runtime.applied_rpm > 0
    assert rig.standing() is None
    assert not stopping(rig)


# =========================================================================
# What counts as a stop under way (the runtime)
# =========================================================================


@pytest.mark.parametrize(
    ("action", "state", "stops"),
    [
        (SafetyAction.FREEZE, RuntimeState.RUNNING, False),
        (SafetyAction.REDUCE, RuntimeState.RUNNING, False),
        (SafetyAction.RAMP_DOWN, RuntimeState.ENDING, True),
        (SafetyAction.QUICK_STOP, RuntimeState.ENDING, True),
    ],
)
async def test_a_verdict_that_stops_the_machine_is_a_stop_freeze_and_reduce_are_not(
    action: SafetyAction, state: RuntimeState, stops: bool
) -> None:
    """FREEZE holds the speed and REDUCE lowers it while the session regulates on."""
    rig = runtime_rig()
    await cruising(rig)
    rig.runtime.trip_from_thread("presence_camera_lost", action, "injected")
    await rig.step()
    verdict = rig.standing()
    assert verdict is not None
    assert verdict.action is action
    assert rig.state() is state
    assert stopping(rig) is stops


async def test_a_manual_target_of_zero_is_a_stop_until_the_setpoint_reaches_zero() -> None:
    rig = runtime_rig()
    await cruising(rig)
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(0)), Ok)
    await rig.step()
    assert rig.runtime.applied_rpm > 0
    assert stopping(rig)
    for _ in range(100):
        await rig.step()
        if rig.runtime.applied_rpm == 0:
            break
    assert rig.runtime.applied_rpm == 0
    assert rig.state() is RuntimeState.RUNNING
    assert not stopping(rig)


async def test_an_ending_is_a_stop_until_its_run_command_is_removed() -> None:
    rig = runtime_rig()
    await cruising(rig)
    rig.runtime.request_stop("operator")
    await rig.step()
    assert rig.state() is RuntimeState.ENDING
    assert stopping(rig)
    for _ in range(400):
        await rig.step()
        if rig.state() is RuntimeState.FINISHED:
            break
    assert rig.phase() is Phase.DONE
    assert not rig.runtime.output_enabled
    assert rig.state() is RuntimeState.FINISHED
    assert not stopping(rig)


async def test_a_state_no_arm_names_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = runtime_rig()

    def impossible(_runtime: TrainingRuntime) -> object:
        return object()

    monkeypatch.setattr(TrainingRuntime, "state", property(impossible))
    with pytest.raises(AssertionError):
        _ = rig.runtime.stop_in_progress


# =========================================================================
# What withholding reads during a stop costs the freshness budget: nothing
# =========================================================================


class HeldSto:
    """A cached reading the test refreshes while the console may read, and holds during a stop."""

    def __init__(self, reading: StoReading) -> None:
        # Changed only by the test, between cooperative ticks.
        self.reading: StoReading = reading

    def latest(self, now: Monotonic) -> StoReading:
        del now
        return self.reading


def fresh(at: Monotonic) -> StoReading:
    return StoReading(
        state=StoState.READY,
        source=StoSourceKind.DRIVE_INPUT,
        last_success_at=at,
        detail="logic input relay contact",
    )


@pytest.mark.parametrize("unresolved", [False, True], ids=["resolved", "read_left_unresolved"])
async def test_reads_withheld_by_a_stop_age_no_reading_into_a_verdict(unresolved: bool) -> None:
    clock = ManualClock(Monotonic(1000.0))
    held = HeldSto(fresh(clock.monotonic()))
    rig = runtime_rig(sto=held)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(20):
        await rig.step()
        held.reading = fresh(rig.now)  # the console reads after each tick
    rig.runtime.request_stop("operator")
    # From here the console withholds every read; one may already be unresolved.
    held.reading = replace(held.reading, read_unresolved=unresolved)
    last = held.reading.last_success_at
    assert last is not None
    latched_at: Monotonic | None = None
    for _ in range(40):
        await rig.step()
        verdict = rig.standing()
        if verdict is not None and verdict.rule == RULE_STO_UNREADABLE:
            latched_at = rig.now
            break
    assert rig.state() is not RuntimeState.RUNNING
    if unresolved:
        # A read left unresolved earns no credit: detected within the budget.
        assert latched_at is not None
        assert latched_at - last <= 2.0 + 0.2 + 1e-9
    else:
        assert latched_at is None
        assert rig.now - last > 2.0 + 0.2  # well past the budget, all of it withheld


def opened_at(at: Monotonic) -> StoReading:
    return replace(fresh(at), state=StoState.OPEN)


@pytest.mark.parametrize(
    ("after_the_stop", "latches"),
    [("ready_reading", False), ("ready_reading_from_before", True), ("open_reading", True)],
)
async def test_after_a_stop_an_open_reading_latches_at_rest_until_a_later_ready_one(
    after_the_stop: str, latches: bool
) -> None:
    """Reads were withheld while the shaft could turn: rest excuses nothing until READY again."""
    clock = ManualClock(Monotonic(1000.0))
    held = HeldSto(fresh(clock.monotonic()))
    rig = runtime_rig(sto=held)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(20):
        await rig.step()
        held.reading = fresh(rig.now)  # the console reads after each tick
    assert rig.runtime.applied_rpm > 0
    before = rig.now
    rig.runtime.request_stop("operator")
    for _ in range(400):  # the console withholds every read meanwhile
        await rig.step()
        if rig.state() is RuntimeState.FINISHED:
            break
    assert rig.state() is RuntimeState.FINISHED
    assert not rig.runtime.output_enabled
    assert held.reading.last_success_at == before  # the last success predates the stop
    if after_the_stop == "ready_reading":
        held.reading = fresh(rig.now)  # the first read after the stop
    if after_the_stop != "open_reading":
        await rig.step()  # READY at rest: no verdict either way
        assert rig.standing() is None
    held.reading = opened_at(rig.now)
    await rig.step()
    verdict = rig.standing()
    assert (verdict is not None and verdict.rule == RULE_STO_OPEN) is latches


# =========================================================================
# The console's guard: a stop asked for withholds the read too
# =========================================================================


@pytest.mark.parametrize(
    ("asked", "withheld"),
    [("end", True), ("target_zero", True), ("other_target", False)],
)
async def test_a_stop_waiting_in_the_mailbox_withholds_the_next_read(
    tmp_path: Path, asked: str, withheld: bool
) -> None:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    source = panel.sto
    assert isinstance(source, DriveInputStoSource)
    await panel.control_step()
    await source.poll()
    assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
    )
    assert isinstance(panel.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(20):
        rig.clock.advance(Seconds(0.2))
        await panel.control_step()
        await source.poll()
    assert panel.runtime.applied_rpm > 0
    rig.clock.advance(Seconds(0.2))
    await panel.control_step()  # a completed tick permits one read
    if asked == "end":
        assert isinstance(panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)
    else:
        target = OutputRpm(0) if asked == "target_zero" else OutputRpm(2)
        assert isinstance(
            panel.surface.submit_manual_target(output_rpm=target, operator=OPERATOR), Ok
        )
    attempts = source.attempts
    await source.poll()
    assert source.attempts == attempts + (0 if withheld else 1)
    await panel.close()


async def test_a_camera_verdict_not_yet_applied_withholds_the_next_read(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path, env={**STO_ENV, "PRESENCE_SOURCE": "sim_empty"})
    panel = rig.panel
    source = panel.sto
    presence = panel.presence
    assert isinstance(source, DriveInputStoSource)
    assert presence is not None
    camera = presence.source
    assert isinstance(camera, SimulatedCamera)
    await panel.control_step()
    await source.poll()
    assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
    )
    assert isinstance(panel.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(20):
        rig.clock.advance(Seconds(0.2))
        await panel.control_step()
        await panel.presence_step()
        await source.poll()
    camera.schedule(Dropout(start=rig.clock.monotonic()))
    withheld = False
    for _ in range(10):
        rig.clock.advance(Seconds(0.2))
        await panel.control_step()  # a completed tick permits one read
        await panel.presence_step()  # the camera's ramp-down reaches the runtime next tick
        attempts = source.attempts
        await source.poll()
        if isinstance(presence.last_decision, RampDown):
            assert panel.runtime.state is RuntimeState.RUNNING
            assert not panel.runtime.stop_in_progress
            assert source.attempts == attempts
            withheld = True
            break
        assert source.attempts == attempts + 1
    assert withheld
    await panel.close()


# =========================================================================
# A faulted drive at standstill waits for its reset alone: the reads resume
# =========================================================================


@pytest.mark.parametrize(
    ("other", "rule"),
    [
        ("screen_estop", RULE_OPERATOR_ESTOP),
        ("sto_open", RULE_STO_OPEN),
        ("supervisor_verdict", RULE_PRESENCE_INTRUSION),
    ],
)
async def test_a_fault_at_standstill_lets_the_reads_resume_then_acknowledgement_and_reset_pass(
    tmp_path: Path, other: str, rule: str
) -> None:
    """No stop write is left once a faulted drive is stopped: the read must not wait for one.

    Behind another verdict the fault reset waits for that verdict's acknowledgement, and
    the acknowledgement waits for a fresh READY reading: reads withheld for as long as the
    run command is held would refuse both until the console restarts.
    """
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    runtime = panel.runtime
    source = panel.sto
    chain = rig.simulator.sto_chain
    assert isinstance(source, DriveInputStoSource)
    assert chain is not None

    async def tick(count: int = 1) -> None:
        for _ in range(count):
            rig.clock.advance(Seconds(0.2))
            panel.surface.note_presence(OPERATOR)
            await panel.control_step()
            await source.poll()

    await tick()
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Ok)
    await tick(75)
    if other == "sto_open":
        chain.open(rig.clock.monotonic())  # read while the session runs: latched at once
        await tick(4)
    rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await tick()
    if other == "screen_estop":
        panel.surface.submit_estop(operator=OPERATOR, reason="the drive faulted")
    elif other == "supervisor_verdict":
        runtime.trip_from_thread(RULE_PRESENCE_INTRUSION, SafetyAction.QUICK_STOP, "injected")
    await tick()
    attempts = source.attempts
    for _ in range(1000):
        assert stopping_now(runtime)  # the shaft still turns: the stop keeps the link
        assert source.attempts == attempts
        rig.clock.advance(Seconds(0.2))
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()
        if not stopping_now(runtime):
            break
        await source.poll()
    # Stopped, faulted, run command held: the ending waits for the reset alone.
    assert state_now(runtime) is RuntimeState.ENDING
    assert enabled_now(runtime)
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.FAULT
    verdict = runtime.standing
    assert verdict is not None
    assert verdict.rule == rule
    if other == "sto_open":
        await tick(5)
        assert source.latest(rig.clock.monotonic()).state is StoState.OPEN
        chain.close(rig.clock.monotonic())
    await tick(5)
    assert source.attempts > attempts
    assert source.latest(rig.clock.monotonic()).state is StoState.READY
    assert isinstance(panel.surface.acknowledge(OPERATOR, estop_released=True), Ok)
    assert isinstance(panel.surface.submit_fault_reset(operator=OPERATOR), Ok)
    await tick(10)  # the reset, then its SHUTDOWN once settled
    assert rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    assert not enabled_now(runtime)
    assert runtime.standing is None  # the acknowledgement cleared the fault's latch too
    assert state_now(runtime) is RuntimeState.FINISHED
    await panel.close()


def stopping_now(runtime: TrainingRuntime) -> bool:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.stop_in_progress


def enabled_now(runtime: TrainingRuntime) -> bool:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.output_enabled


def state_now(runtime: TrainingRuntime) -> RuntimeState:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.state


# =========================================================================
# REDUCE is no stop: a chain that opens under it is read, and latches
# =========================================================================


@pytest.mark.parametrize("reduce", [False, True], ids=["no_verdict", "under_reduce"])
async def test_a_chain_opening_under_a_reduce_latches_sto_open_in_the_tick_that_judges_it(
    tmp_path: Path, reduce: bool
) -> None:
    rig, _ = make_rig(tmp_path, env=STO_ENV)
    panel = rig.panel
    source = panel.sto
    chain = rig.simulator.sto_chain
    assert isinstance(source, DriveInputStoSource)
    assert chain is not None
    await panel.control_step()
    await source.poll()
    assert isinstance(panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        await panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
    )
    assert isinstance(panel.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(20):
        rig.clock.advance(Seconds(0.2))
        await panel.control_step()
        await source.poll()
    if reduce:
        panel.runtime.trip_from_thread(RULE_HR_RATE, SafetyAction.REDUCE, "injected")
    rig.clock.advance(Seconds(0.2))
    await panel.control_step()  # takes the REDUCE, if any, and permits one read
    standing = SafetyAction.REDUCE if reduce else SafetyAction.NONE
    assert panel.runtime.state is RuntimeState.RUNNING
    assert panel.runtime.standing_action is standing
    assert panel.runtime.applied_rpm > 0
    chain.open(rig.clock.monotonic())  # the mushroom, just after the tick
    attempts = source.attempts
    await source.poll()
    assert source.attempts == attempts + 1
    assert source.latest(rig.clock.monotonic()).state is StoState.OPEN
    rig.clock.advance(Seconds(0.2))
    await panel.control_step()  # the tick that judges the open reading
    verdict = panel.runtime.standing
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_OPEN,
        SafetyAction.QUICK_STOP,
        True,
    )
    assert panel.runtime.applied_rpm == 0
    await panel.close()


# =========================================================================
# The polling task's pacing, and each port's budget
# =========================================================================


@pytest.mark.parametrize(("paced", "permitted"), [(False, False), (True, True), (True, False)])
async def test_the_polling_task_waits_out_the_period_after_a_read_then_for_the_next_tick(
    monkeypatch: pytest.MonkeyPatch, paced: bool, permitted: bool
) -> None:
    clock, port = ManualClock(), RegisterPort()
    port.result = Ok(RawRegister(1))
    source = DriveInputStoSource(
        clock=clock, drive=port, address=RegisterMap().logic_inputs, input_=LogicInput.LI1
    )
    source.guard_polling(lambda: permitted)
    sleeps: list[float] = []
    ticks: list[float] = []

    async def sleep(period: float) -> None:
        sleeps.append(period)
        if not paced and len(sleeps) == 2:  # unpaced, the loop comes round once more
            raise asyncio.CancelledError

    async def next_tick(bound: Seconds) -> None:
        ticks.append(bound)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    if paced:
        source.pace_polling(next_tick)
    with pytest.raises(asyncio.CancelledError):
        await source.run()
    # A read is followed by the rest of its period; a refused poll waits for a tick only,
    # or, with no console pacing it, for a period.
    if paced:
        assert source.attempts == (1 if permitted else 0)
        assert sleeps == ([0.5] if permitted else [])
        assert ticks == [0.5]
    else:
        assert source.attempts == 0
        assert sleeps == [0.5, 0.5]
        assert ticks == []


def test_each_port_declares_the_link_time_one_read_is_planned_for() -> None:
    clock = ManualClock()
    config = config_of(STO_ENV)
    simulated = build_sto(config, clock, SimulatedDrive(clock), None)
    real = build_sto(config, clock, build_drive(clock, FakeBus(clock, RegisterMap())), None)
    assert isinstance(simulated, DriveInputStoSource)
    assert isinstance(real, DriveInputStoSource)
    assert simulated.read_budget == 0.0  # the simulator's exchanges never suspend
    assert real.read_budget == AUXILIARY_READ_BUDGET
