"""How long a fault reset keeps a SWITCH_ON_DISABLED from being read as an STO opening.

A SWITCH_ON_DISABLED that follows a fault, or a reset attempt, is the reset's
doing, not the chain's, and is not taken for an opening
(``TrainingRuntime._deduce_sto_open``). That exclusion takes a latch away, so
how long it lasts is a safety property: until a tick reads another state, or
until the next arming is acknowledged. The drive was inspected out of fault for
that arming, so a SWITCH_ON_DISABLED read under its run command follows no
reset: an opening too short for any reading, just after the arming, or a cut
between the safety relay's contact and the drive's STO terminal, which no
reading shows at all, latches ``sto_open`` at the next tick, whichever way the
reset before it went. A drive that resets its own fault is excluded all the
same, and the reading judges the chain then. On the real console, with
``STO_SOURCE=drive_input``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final, override

import pytest

from src.motor.drive import ControlWord, DriveError, DriveFault, DriveState, UnexpectedState
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import StoState
from src.result import Err, Ok, Result
from src.training.preflight import StoNotReady
from src.training.runtime import RULE_STO_OPEN, STO_OPEN_DEDUCED, RuntimeState, SafetyStanding
from src.training.safety import RULE_DRIVE_FAULT
from src.training.types import SafetyAction
from src.units import OutputRpm, RawRegister, RegisterAddress
from tests.test_failure_rig import OPERATOR
from tests.test_sto_open_during_stop import (
    DRIVE_INPUT,
    TICK,
    Noted,
    ResetLandsUnanswered,
    Scene,
    acknowledged,
    enabled,
    sim_state,
    standing,
    state,
    until_at_rest,
    until_finished,
)
from tests.test_sto_open_unread import (
    acknowledge_once_read,
    assert_latched_unread,
    pulse,
    until_the_stop_is_over,
)


async def reset_after_a_session(scene: Scene) -> None:
    """A session, a drive fault, the shaft at rest, the acknowledgement, the operator's reset."""
    await scene.cruise()
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await until_at_rest(scene)
    await scene.run(5)
    assert isinstance(scene.panel.surface.acknowledge(OPERATOR), Ok)
    assert isinstance(scene.panel.surface.submit_fault_reset(operator=OPERATOR), Ok)
    await scene.run(10)


async def reset_at_rest(scene: Scene) -> None:
    """A drive found in fault before any session, reset at rest: no tick reads it after."""
    await scene.run(1)
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await scene.run(10)
    assert isinstance(scene.panel.surface.submit_fault_reset(operator=OPERATOR), Ok)
    await scene.run(10)


RESETS: Final[Mapping[str, tuple[type[Noted], Callable[[Scene], Awaitable[None]], SimState]]] = {
    # No SHUTDOWN follows a reset left unanswered: the drive stays SWITCH_ON_DISABLED.
    "answer_lost": (ResetLandsUnanswered, reset_after_a_session, SimState.SWITCH_ON_DISABLED),
    "answered": (Noted, reset_after_a_session, SimState.READY_TO_SWITCH_ON),
    "at_rest_before_any_session": (Noted, reset_at_rest, SimState.READY_TO_SWITCH_ON),
}


@pytest.mark.parametrize("case", list(RESETS))
async def test_an_opening_just_after_the_arming_that_follows_a_reset_latches_until_acknowledged(
    tmp_path: Path, case: str
) -> None:
    """The reset's exclusion ends at the next arming: a 0.1 s opening there is deduced."""
    wrap, reset, left_in = RESETS[case]
    scene = Scene(tmp_path, DRIVE_INPUT, wrap=wrap)
    runtime = scene.runtime
    await reset(scene)
    assert sim_state(scene) is left_in
    assert scene.sto_open == []
    assert standing(runtime) is None
    assert isinstance(await scene.start(), Ok)
    opened = await pulse(scene, across_a_tick=False)
    assert_latched_unread(scene, opened)
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Err)
    await until_the_stop_is_over(scene)
    await until_finished(scene)
    refused = await scene.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == RULE_STO_OPEN
    await acknowledge_once_read(scene)
    assert isinstance(await scene.start(), Ok)
    await scene.run(5)
    assert sim_state(scene) is SimState.OPERATION_ENABLED
    await scene.panel.close()


class CutBelowTheContact(ResetLandsUnanswered):
    """Once ``cut``: 5202 still reads the relay's contact closed, and the drive takes every word.

    A wire cut between the contact and the drive's STO terminal. The drive's
    state machine does not move with STO open; a real ATV320 acknowledges the
    command word all the same, where the simulator refuses it.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        # Set and cleared by the test alone.
        self.cut: bool = False

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        written = await super().write_command(word)
        if self.cut and isinstance(written, Err) and isinstance(written.error, UnexpectedState):
            return Ok(None)
        return written

    @override
    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        read = await super().read_register(address)
        return Ok(RawRegister(1)) if self.cut and isinstance(read, Ok) else read


@pytest.mark.parametrize("before_the_start", [True, False], ids=["before_start", "after_arming"])
async def test_a_cut_below_the_relay_s_contact_after_a_lost_reset_latches_at_the_first_tick(
    tmp_path: Path, before_the_start: bool
) -> None:
    """No reading ever shows such a cut: the drive's word does, at the first tick of the session."""
    scene = Scene(tmp_path, DRIVE_INPUT, wrap=CutBelowTheContact)
    runtime, wire = scene.runtime, scene.noted
    assert isinstance(wire, CutBelowTheContact)
    await reset_after_a_session(scene)
    assert sim_state(scene) is SimState.SWITCH_ON_DISABLED
    if before_the_start:
        wire.cut = True
        scene.chain.open(scene.rig.clock.monotonic())
        await scene.run(5)
        assert scene.reading().state is StoState.READY
    assert isinstance(await scene.start(), Ok)
    armed = scene.now
    if not before_the_start:
        wire.cut = True
        scene.chain.open(scene.rig.clock.monotonic())
    await scene.run(1)
    verdict = standing(runtime)
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_OPEN,
        SafetyAction.QUICK_STOP,
        True,
    )
    assert verdict.detail == STO_OPEN_DEDUCED
    assert verdict.since - armed <= TICK + 1e-9
    assert scene.reading().state is StoState.READY  # the contact never showed it
    assert runtime.applied_rpm == 0
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Err)
    await until_the_stop_is_over(scene)
    await until_finished(scene)
    assert sim_state(scene) is SimState.SWITCH_ON_DISABLED
    wire.cut = False  # the wire is repaired
    scene.chain.close(scene.rig.clock.monotonic())
    await scene.run(5)
    refused = await scene.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == RULE_STO_OPEN
    await acknowledge_once_read(scene)
    assert isinstance(await scene.start(), Ok)
    await scene.run(5)
    assert sim_state(scene) is SimState.OPERATION_ENABLED
    await scene.panel.close()


@pytest.mark.parametrize("opens", [False, True], ids=["chain_ready", "chain_opens_after"])
async def test_a_drive_that_resets_its_own_fault_is_no_sto_opening_and_the_reading_judges(
    tmp_path: Path, opens: bool
) -> None:
    """FAULT then SWITCH_ON_DISABLED under the run command, no reset from this console.

    An automatic restart would do that. No ``sto_open`` is deduced from it; the
    run command is removed at standstill, ``drive_fault`` stands, and the
    reading alone says whether the chain lets anything start again.
    """
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime, surface = scene.runtime, scene.panel.surface
    await scene.cruise()
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await until_at_rest(scene)
    await scene.run(5)
    assert enabled(runtime)  # the ending waits for a reset
    # The drive resets its own fault: not a word from this console.
    assert isinstance(await scene.rig.simulator.write_command(ControlWord.FAULT_RESET), Ok)
    await scene.run(10)
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.SWITCH_ON_DISABLED
    assert ("command", ControlWord.FAULT_RESET.value) not in [w[1:] for w in scene.writes]
    assert scene.sto_open == []
    assert not enabled(runtime)
    assert state(runtime) is RuntimeState.FINISHED
    verdict = standing(runtime)
    assert verdict is not None
    assert verdict.rule == RULE_DRIVE_FAULT
    if opens:
        scene.chain.open(scene.rig.clock.monotonic())
        await scene.run(5)
        assert scene.reading().state is StoState.OPEN
        not_ready = acknowledged(scene)
        assert isinstance(not_ready, Err)
        assert isinstance(not_ready.error, StoNotReady)
        assert not_ready.error.state is StoState.OPEN
        scene.chain.close(scene.rig.clock.monotonic())
        await scene.run(5)
    assert scene.reading().state is StoState.READY
    assert isinstance(surface.acknowledge(OPERATOR, estop_released=True), Ok)
    assert isinstance(await scene.start(), Ok)
    await scene.run(5)
    assert sim_state(scene) is SimState.OPERATION_ENABLED
    assert scene.sto_open == []
    await scene.panel.close()
