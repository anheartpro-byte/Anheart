"""An STO opening that no reading shows is latched all the same, from the drive's own word.

The relay's contact is read after the ticks, about every 0.6 s with
``STO_SOURCE=drive_input``, and a simulated chain at each tick: an opening
shorter than that, closed again before the next reading, is never read OPEN.
The drive's status word, read by every tick, still shows the output stage it
removed by itself while this console holds the run command, and ``sto_open``
latches at the next tick (``TrainingRuntime._deduce_sto_open``), in a manual
session and through a programme, stop or not. Then the usual way back: the
setpoint at zero, a new start refused until a named acknowledgement, itself
refused until a READY reading taken since the stop.

A FREEZE that walks a programme's setpoint down to zero is such a stop: no
reading is taken then, and the drive's word is what shows an opening. A
programme's own descent with no verdict standing is regulation, and is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

import httpx
import pytest

from src.motor.simulated import SimState
from src.motor.sto import DriveInputStoSource, StoState
from src.result import Err, Ok
from src.sim.physiology import Physiology, SubjectState
from src.training.preflight import StoNotReady
from src.training.runtime import (
    RULE_STO_OPEN,
    STO_OPEN_DEDUCED,
    EndReason,
    RuntimeState,
    SafetyStanding,
)
from src.training.safety import RULE_LOOP_STALL
from src.training.types import Phase, SafetyAction, SafetyVerdict
from src.units import Monotonic, MotorRpm, OutputRpm, Seconds
from tests.test_cooldown_freeze_console import START
from tests.test_failure_rig import MIN_RUN, OPERATOR, PROGRAMME_ENV, start_programme
from tests.test_standstill_console import Electrodes
from tests.test_sto_open_during_stop import (
    DRIVE_INPUT,
    SIM_READY,
    TICK,
    Scene,
    acknowledged,
    standing,
    stopping,
)

PROGRAMME_DRIVE_INPUT: Final[Mapping[str, str]] = {
    **PROGRAMME_ENV,
    "STO_SOURCE": "drive_input",
    "STO_DRIVE_INPUT": "LI1",
}
PROGRAMME_SIM_READY: Final[Mapping[str, str]] = {**PROGRAMME_ENV, "STO_SOURCE": "sim_ready"}


PULSE: Final[Seconds] = Seconds(0.1)
"""An opening shorter than a tick, so shorter than the interval between two readings."""


async def pulse(scene: Scene, *, across_a_tick: bool) -> float:
    """Open the chain just after a tick and its reading; close it before the next reading.

    For :data:`PULSE` between two ticks, or for the length of one tick whose
    reading is not yet due. Returns the instant it opened.
    """
    opened = scene.now
    scene.chain.open(scene.rig.clock.monotonic())
    if across_a_tick:
        await scene.step()
        scene.chain.close(scene.rig.clock.monotonic())
        return opened
    scene.rig.clock.advance(PULSE)
    scene.chain.close(scene.rig.clock.monotonic())
    await scene.step(after=Seconds(TICK - PULSE))
    return opened


def assert_latched_unread(scene: Scene, opened: float) -> None:
    """``sto_open`` from the drive's word, at the tick after the opening; never read OPEN."""
    verdict = standing(scene.runtime)
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_OPEN,
        SafetyAction.QUICK_STOP,
        True,
    )
    assert verdict.detail == STO_OPEN_DEDUCED
    assert verdict.since - opened <= TICK + 1e-9
    assert scene.reading().state is StoState.READY  # the chain was never read open
    assert scene.runtime.applied_rpm == 0  # zeroed in the tick that latched


async def until_the_stop_is_over(scene: Scene) -> None:
    """Tick to the end of the stop; nothing is commanded from the latch on."""
    for _ in range(1000):
        await scene.step()
        assert scene.runtime.applied_rpm == 0
        assert scene.rig.simulator.commanded_setpoint == 0
        if not stopping(scene.runtime):
            return
        await scene.read()  # refused by the guard: no attempt while the stop is under way
    raise AssertionError("the stop never ended")


def state_of(scene: Scene) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return scene.runtime.state


def refused_until_read_since(scene: Scene) -> None:
    """At the end of the stop, the acknowledgement waits for a READY reading taken since.

    The relay's contact was last read before the stop; a simulated chain is read
    at the instant asked, so it always has one.
    """
    if isinstance(scene.panel.sto, DriveInputStoSource):
        early = acknowledged(scene)
        assert isinstance(early, Err)
        assert isinstance(early.error, StoNotReady)
        assert early.error.state is StoState.UNKNOWN


async def acknowledge_once_read(scene: Scene) -> None:
    """A READY reading, then the named acknowledgement clears the verdict."""
    await scene.read()
    assert scene.reading().state is StoState.READY
    assert isinstance(acknowledged(scene), Ok)
    assert standing(scene.runtime) is None


PULSES: Final[Mapping[str, tuple[Mapping[str, str], bool]]] = {
    "drive_input_between_ticks": (DRIVE_INPUT, False),
    "drive_input_across_a_tick": (DRIVE_INPUT, True),
    "sim_ready_between_ticks": (SIM_READY, False),
}


@pytest.mark.parametrize("case", list(PULSES))
async def test_a_short_opening_in_a_manual_session_latches_at_the_next_tick_until_acknowledged(
    tmp_path: Path, case: str
) -> None:
    env, across_a_tick = PULSES[case]
    scene = Scene(tmp_path, env)
    runtime = scene.runtime
    await scene.cruise()
    assert not stopping(runtime)
    opened = await pulse(scene, across_a_tick=across_a_tick)
    assert_latched_unread(scene, opened)
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Err)  # no new target
    await until_the_stop_is_over(scene)
    assert runtime.state is RuntimeState.FINISHED
    refused = await scene.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == RULE_STO_OPEN
    refused_until_read_since(scene)
    await acknowledge_once_read(scene)
    assert isinstance(await scene.start(), Ok)
    await scene.panel.close()


class ProgrammeScene(Scene):
    """The same console on the failure rig's short programme, the rider's rate read each second."""

    def __init__(
        self, tmp_path: Path, env: Mapping[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        electrodes = Electrodes()
        original = Physiology.advance

        def advance(physiology: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
            state = original(physiology, now, motor_rpm)
            electrodes.state = state
            return state

        monkeypatch.setattr(Physiology, "advance", advance)
        super().__init__(tmp_path, env, treat=electrodes)

    @override
    async def step(self, after: Seconds = TICK) -> None:
        """One control tick as the console's loop runs it, the ECG taken first."""
        self.rig.clock.advance(after)
        self.panel.surface.note_presence(OPERATOR)
        await self.panel.ecg_step()
        await self.control()

    async def turning_in(self, phase: Phase) -> None:
        """Tick until the programme turns the arm in ``phase``, no verdict standing."""
        for _ in range(2000):
            await self.run(1)
            measured = self.runtime.snapshot().measured.motor_rpm
            if self.runtime.phase is phase and self.runtime.applied_rpm > MIN_RUN and measured > 0:
                assert standing(self.runtime) is None
                return
        raise AssertionError(f"the programme never turned the arm in {phase.name}")


PROGRAMMES: Final[Mapping[str, tuple[Mapping[str, str], Phase]]] = {
    "drive_input_warmup": (PROGRAMME_DRIVE_INPUT, Phase.WARMUP),
    "drive_input_hold": (PROGRAMME_DRIVE_INPUT, Phase.HOLD),
    "sim_ready_warmup": (PROGRAMME_SIM_READY, Phase.WARMUP),
}


@pytest.mark.parametrize("case", list(PROGRAMMES))
async def test_a_short_opening_during_a_programme_latches_and_nothing_starts_unacknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """The programme is ended by the verdict; at rest a START waits for the acknowledgement."""
    env, phase = PROGRAMMES[case]
    scene = ProgrammeScene(tmp_path, env, monkeypatch)
    runtime = scene.runtime
    async with scene.rig.http() as session:
        await scene.run(50)
        await start_programme(session)
        await scene.turning_in(phase)
        assert not stopping(runtime)
        opened = await pulse(scene, across_a_tick=False)
        assert_latched_unread(scene, opened)
        await until_the_stop_is_over(scene)
        refused_until_read_since(scene)
        for _ in range(2000):  # the programme's recovery, at rest, to its end
            if runtime.state is RuntimeState.FINISHED:
                break
            await scene.run(1)
            assert runtime.applied_rpm == 0
        assert runtime.state is RuntimeState.FINISHED
        assert runtime.end_reason is EndReason.SAFETY_VERDICT
        # A START typed now is taken by the loop and refused there: nothing is armed.
        typed = await session.post("/api/session/start", json=START)
        assert typed.status_code == httpx.codes.ACCEPTED, typed.text
        await scene.run(5)
        assert runtime.state is RuntimeState.FINISHED
        assert runtime.end_reason is EndReason.SAFETY_VERDICT
        assert scene.rig.simulator.sim_state is SimState.SWITCH_ON_DISABLED
        verdict = standing(runtime)
        assert verdict is not None
        assert verdict.rule == RULE_STO_OPEN
        await acknowledge_once_read(scene)
        await start_programme(session)
        await scene.run(5)
        assert state_of(scene) is RuntimeState.RUNNING
    await scene.panel.close()


# =========================================================================
# A FREEZE walking a programme's setpoint down to zero is a stop: no reading
# =========================================================================


@dataclass(frozen=True, slots=True)
class FrozenDescent:
    """A programme's descent under a FREEZE, from its first COOLDOWN tick to the end of the stop."""

    writes: tuple[tuple[float, str, int], ...]
    ended: float
    attempts: tuple[int, int]
    """STO read attempts at the first COOLDOWN tick, and at the end of the stop."""
    opened: float | None
    latched: SafetyVerdict | None


async def frozen_descent(scene: ProgrammeScene, *, opening: bool) -> FrozenDescent:
    """HOLD under a latched ``loop_stall``, then COOLDOWN; the chain opens on the way, or not."""
    async with scene.rig.http() as session:
        await scene.run(50)
        await start_programme(session)
    await scene.turning_in(Phase.HOLD)
    await scene.step(after=Seconds(TICK + 1.0))  # the loop misses a second: a FREEZE latches
    await scene.read()
    frozen = standing(scene.runtime)
    assert frozen is not None
    assert (frozen.rule, frozen.action) == (RULE_LOOP_STALL, SafetyAction.FREEZE)
    for _ in range(2000):
        await scene.step()
        if scene.runtime.phase is Phase.COOLDOWN:
            break
        assert not stopping(scene.runtime)  # a FREEZE that holds a speed stops nothing
        await scene.read()
    cooling = scene.now
    assert scene.runtime.phase is Phase.COOLDOWN
    assert scene.runtime.applied_rpm > 0
    assert stopping(scene.runtime)  # the walk down to zero is a stop from its first tick
    attempts = scene.attempts()
    opened = None
    if opening:
        await scene.run(5)
        opened = scene.now
        scene.chain.open(scene.rig.clock.monotonic())
    for _ in range(2000):
        await scene.step()
        if not stopping(scene.runtime):
            return FrozenDescent(
                writes=tuple(w for w in scene.writes if cooling <= w[0] <= scene.now),
                ended=scene.now,
                attempts=(attempts, scene.attempts()),
                opened=opened,
                latched=scene.sto_open[0] if scene.sto_open else None,
            )
        await scene.read()  # refused by the guard: no attempt while the stop is under way
    raise AssertionError("the stop never ended")


@pytest.mark.parametrize("opening", [False, True], ids=["walked_down", "opened_on_the_way"])
async def test_a_freeze_walking_a_programme_down_is_a_stop_that_no_read_delays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opening: bool
) -> None:
    """No 5202 read from the first COOLDOWN tick to the end of the stop; the drive's word judges.

    The writes are those of the same programme with a chain read at once, at the
    same instants, and an opening on the way down latches at the same tick.
    """
    reference = ProgrammeScene(tmp_path / "instant", PROGRAMME_SIM_READY, monkeypatch)
    instant = await frozen_descent(reference, opening=opening)
    await reference.panel.close()
    monkeypatch.undo()
    scene = ProgrammeScene(tmp_path / "reading", PROGRAMME_DRIVE_INPUT, monkeypatch)
    descent = await frozen_descent(scene, opening=opening)
    assert descent.attempts[1] == descent.attempts[0]
    assert descent.writes
    assert descent.writes == instant.writes
    assert descent.ended == instant.ended
    if descent.opened is None:
        assert descent.latched is None
        assert scene.runtime.applied_rpm == 0
        assert state_of(scene) is RuntimeState.RUNNING  # walked down, still under its FREEZE
        await scene.run(5)
        assert scene.attempts() > descent.attempts[1]  # the reads resume at the end of the stop
    else:
        assert descent.latched is not None
        assert instant.latched is not None
        assert (descent.latched.rule, descent.latched.action) == (
            RULE_STO_OPEN,
            SafetyAction.QUICK_STOP,
        )
        assert descent.latched.detail == STO_OPEN_DEDUCED
        assert descent.latched.since - descent.opened <= TICK + 1e-9
        assert descent.latched.since == instant.latched.since
    await scene.panel.close()


async def test_a_programme_s_own_descent_with_no_verdict_is_regulation_and_the_reads_go_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """COOLDOWN with no verdict standing: the controller brings the arm down, and is read."""
    scene = ProgrammeScene(tmp_path, PROGRAMME_DRIVE_INPUT, monkeypatch)
    async with scene.rig.http() as session:
        await scene.run(50)
        await start_programme(session)
    await scene.turning_in(Phase.HOLD)
    for _ in range(2000):
        await scene.run(1)
        if scene.runtime.phase is Phase.COOLDOWN:
            break
    descending = 0
    for _ in range(20):
        await scene.step()
        assert scene.runtime.phase is Phase.COOLDOWN
        assert standing(scene.runtime) is None
        assert scene.runtime.applied_rpm > 0
        assert not stopping(scene.runtime)
        before = scene.attempts()
        await scene.read()
        assert scene.attempts() == before + 1
        descending += 1
    assert descending == 20
    await scene.panel.close()
