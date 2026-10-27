"""An STO chain that opens during a stop is latched, though no STO read is taken then.

The console starts no STO read while a stop is under way or asked for
(``LocalPanel._stop_pending``), so that no stop write waits behind one. The
opening is not lost: the drive's own status word, read by every tick, shows
the output stage removed while this console holds the run command, and
``sto_open`` latches in that tick, whether the chain is still open or already
closed again (``TrainingRuntime._deduce_sto_open``). Every category of stop,
on the real console with ``STO_SOURCE=drive_input``; the same scene with a
simulated chain read at once gives the reference the stop writes must match.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override

import pytest

from src.cloud_sync import DASHBOARD_OPERATOR
from src.ecg_pipeline import TreatFunction
from src.local_panel import LocalPanel
from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFault,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    UnexpectedState,
)
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import (
    STO_STALE_AFTER,
    DriveInputStoSource,
    SimulatedStoChain,
    StoReading,
    StoState,
)
from src.presence.monitor import RULE_PRESENCE_CAMERA_LOST
from src.result import Err, Ok, Result
from src.training import runtime as runtime_module
from src.training.preflight import StoNotReady
from src.training.runtime import (
    RULE_DISABLE_REFUSED,
    RULE_STO_OPEN,
    STO_OPEN_DEDUCED,
    RuntimeState,
    SafetyStanding,
    TrainingRuntime,
)
from src.training.safety import RULE_OPERATOR_ESTOP, NothingLatched
from src.training.types import Occupancy, SafetyAction, SafetyVerdict
from src.units import MotorRpm, OutputRpm, RawRegister, RegisterAddress, Seconds
from tests.test_failure_rig import BENCH_ENV, OPERATOR, Rig, Wrapped, make_rig

TICK: Final[Seconds] = Seconds(0.2)
DRIVE_INPUT: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "STO_SOURCE": "drive_input",
    "STO_DRIVE_INPUT": "LI1",
}
SIM_READY: Final[Mapping[str, str]] = {**BENCH_ENV, "STO_SOURCE": "sim_ready"}


class Noted(Wrapped):
    """The simulator, each command write noted in order; 5202 is read through it too."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        # Appended by the one console on this drive, emptied by Scene.step between ticks.
        self.writes: list[tuple[str, int]] = []

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self.writes.append(("speed", rpm))
        return await self.inner.write_speed(rpm)

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self.writes.append(("command", word.value))
        return await self.inner.write_command(word)

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self.writes.append(("emergency", 0))
        return self.inner.emergency_disable_blocking(timeout)

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        return await self.inner.read_register(address)


class Scene:
    """The real console over a noted simulator, ticked as its loop does."""

    def __init__(
        self,
        tmp_path: Path,
        env: Mapping[str, str],
        *,
        wrap: type[Noted] = Noted,
        treat: TreatFunction | None = None,
    ) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        rig, wrapper = make_rig(tmp_path, env=env, wrap=wrap, treat=treat)
        assert isinstance(wrapper, Noted)
        chain = rig.simulator.sto_chain
        assert chain is not None
        self.rig: Rig = rig
        self.panel: LocalPanel = rig.panel
        self.runtime: TrainingRuntime = rig.panel.runtime
        self.noted: Noted = wrapper
        self.chain: SimulatedStoChain = chain
        # Appended by step() alone: each command write, at the instant of its tick.
        self.writes: list[tuple[float, str, int]] = []
        # Appended by step() alone: every sto_open verdict that stood after a tick.
        self.sto_open: list[SafetyVerdict] = []

    @property
    def now(self) -> float:
        return float(self.rig.clock.monotonic())

    async def step(self, after: Seconds = TICK) -> None:
        """One control tick ``after`` the last instant, without the STO read that may follow it."""
        self.rig.clock.advance(after)
        self.panel.surface.note_presence(OPERATOR)
        await self.control()

    async def control(self) -> None:
        """The control step at the current instant: its command writes and any sto_open noted."""
        await self.panel.control_step()
        self.writes.extend((self.now, kind, value) for kind, value in self.noted.writes)
        self.noted.writes.clear()
        verdict = self.runtime.standing
        if verdict is not None and verdict.rule == RULE_STO_OPEN:
            self.sto_open.append(verdict)

    async def read(self) -> None:
        """The polling task's attempt after a tick, when the source is read at all."""
        source = self.panel.sto
        if isinstance(source, DriveInputStoSource):
            await source.poll()

    async def run(self, ticks: int) -> None:
        for _ in range(ticks):
            await self.step()
            await self.read()

    def attempts(self) -> int:
        source = self.panel.sto
        return source.attempts if isinstance(source, DriveInputStoSource) else 0

    def reading(self) -> StoReading:
        return self.panel.sto.latest(self.rig.clock.monotonic())

    async def cruise(self) -> None:
        """A manual bench session at 3 output rpm, no verdict."""
        await self.run(1)
        assert isinstance(self.runtime.confirm_estop_wiring(OPERATOR), Ok)
        started = await self.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(started, Ok)
        assert isinstance(self.runtime.set_manual_target(OutputRpm(3)), Ok)
        await self.run(75)
        assert self.runtime.applied_rpm > 0
        assert self.runtime.standing is None

    async def start(self) -> Result[object, object]:
        assert isinstance(self.runtime.confirm_estop_wiring(OPERATOR), Ok)
        started = await self.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        return Ok(None) if isinstance(started, Ok) else Err(started.error)


def stopping(runtime: TrainingRuntime) -> bool:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return runtime.stop_in_progress


def standing(runtime: TrainingRuntime) -> SafetyVerdict | None:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.standing


def enabled(runtime: TrainingRuntime) -> bool:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.output_enabled


def state(runtime: TrainingRuntime) -> RuntimeState:
    """Read afresh, for the reason :func:`stopping` gives."""
    return runtime.state


def sim_state(scene: Scene) -> SimState:
    """The simulated drive's state, read afresh for the reason :func:`stopping` gives."""
    return scene.rig.simulator.sim_state


# =========================================================================
# Every category of stop
# =========================================================================

Ask = Callable[[LocalPanel, pytest.MonkeyPatch], None]


def operator_stop(panel: LocalPanel, _monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)


def dashboard_stop(panel: LocalPanel, _monkeypatch: pytest.MonkeyPatch) -> None:
    ended = panel.surface.submit_end(operator=DASHBOARD_OPERATOR, reason="arret depuis le site")
    assert isinstance(ended, Ok)


def session_limit(_panel: LocalPanel, monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual session's limit, reached at the next tick."""
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", Seconds(0.0))


def target_zero(panel: LocalPanel, _monkeypatch: pytest.MonkeyPatch) -> None:
    asked = panel.surface.submit_manual_target(output_rpm=OutputRpm(0), operator=OPERATOR)
    assert isinstance(asked, Ok)


def ramp_down_verdict(panel: LocalPanel, _monkeypatch: pytest.MonkeyPatch) -> None:
    panel.runtime.trip_from_thread(RULE_PRESENCE_CAMERA_LOST, SafetyAction.RAMP_DOWN, "injected")


STOPS: Final[Mapping[str, Ask]] = {
    "operator_stop": operator_stop,
    "dashboard_stop": dashboard_stop,
    "manual_session_limit": session_limit,
    "manual_target_zero": target_zero,
    "ramp_down_verdict": ramp_down_verdict,
}


@dataclass(frozen=True, slots=True)
class Stopped:
    """What one stop with an opening did, from the request to its end."""

    writes: tuple[tuple[float, str, int], ...]
    opened: float
    latched: SafetyVerdict
    ended: float
    attempts: tuple[int, int]
    """STO read attempts when the stop was asked for, and when it ended."""


async def stop_with_an_opening(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, category: str, reclosed: bool
) -> Stopped:
    """Ask for the stop; open the chain during it; close it again 2 s later, or not."""
    await scene.cruise()
    asked = scene.now
    attempts = scene.attempts()
    STOPS[category](scene.panel, monkeypatch)
    await scene.run(2)
    assert stopping(scene.runtime)
    opened = scene.now
    scene.chain.open(scene.rig.clock.monotonic())
    await scene.run(10)
    if reclosed:
        scene.chain.close(scene.rig.clock.monotonic())
    ended = None
    for _ in range(1000):
        await scene.step()
        if not stopping(scene.runtime):
            ended = scene.now
            break
        await scene.read()  # refused by the guard: no attempt while the stop is under way
    assert ended is not None
    assert scene.sto_open, "no sto_open verdict"
    return Stopped(
        writes=tuple(write for write in scene.writes if asked <= write[0] <= ended),
        opened=opened,
        latched=scene.sto_open[0],
        ended=ended,
        attempts=(attempts, scene.attempts()),
    )


@pytest.mark.parametrize("reclosed", [False, True], ids=["left_open", "closed_again"])
@pytest.mark.parametrize("category", list(STOPS))
async def test_a_chain_opening_during_any_stop_latches_at_the_next_tick_until_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, category: str, reclosed: bool
) -> None:
    """No STO read during the stop, yet the opening latches within one tick, then holds."""
    reference = Scene(tmp_path / "instant", SIM_READY)
    instant = await stop_with_an_opening(reference, monkeypatch, category, reclosed)
    await reference.panel.close()
    monkeypatch.undo()
    scene = Scene(tmp_path / "reading", DRIVE_INPUT)
    stopped = await stop_with_an_opening(scene, monkeypatch, category, reclosed)
    runtime, surface = scene.runtime, scene.panel.surface
    # No 5202 read from the request to the end of the stop, and every stop write at
    # the instant the chain read at once gives it: the same tick latched both.
    assert stopped.attempts[1] == stopped.attempts[0]
    assert stopped.writes == instant.writes
    assert (stopped.latched.action, stopped.latched.latched) == (SafetyAction.QUICK_STOP, True)
    assert stopped.latched.detail == STO_OPEN_DEDUCED
    assert stopped.latched.since - stopped.opened <= TICK + 1e-9
    assert instant.latched.since == stopped.latched.since
    # After the stop: refused until a named acknowledgement, itself refused until READY.
    await scene.run(5)
    assert scene.attempts() > stopped.attempts[1]  # the reads resume at the end of the stop
    refused = await scene.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == RULE_STO_OPEN
    if not reclosed:
        assert scene.reading().state is StoState.OPEN
        not_ready = surface.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(not_ready, Err)
        assert isinstance(not_ready.error, StoNotReady)
        assert not_ready.error.state is StoState.OPEN
        scene.chain.close(scene.rig.clock.monotonic())
        await scene.run(5)
    assert scene.reading().state is StoState.READY
    assert runtime.applied_rpm == 0
    assert scene.rig.simulator.commanded_setpoint == 0
    assert isinstance(surface.acknowledge(OPERATOR, estop_released=True), Ok)
    assert runtime.standing is None
    assert isinstance(await scene.start(), Ok)
    await scene.panel.close()


async def test_an_opening_just_before_a_stop_that_settles_at_once_still_latches(
    tmp_path: Path,
) -> None:
    """Armed at standstill: the stop is taken and the run command removed in one tick.

    The chain opens after the last read and the STOP comes before the next one, so
    no reading and no withholding tick ever shows it: only the drive's word, judged
    before the run command is cleared, does.
    """
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime = scene.runtime
    await scene.cruise()
    assert isinstance(runtime.set_manual_target(OutputRpm(0)), Ok)
    for _ in range(1000):
        await scene.run(1)
        status = runtime.drive_status
        if not stopping(runtime) and status is not None and status.output_rpm == 0:
            break
    await scene.run(5)
    assert runtime.state is RuntimeState.RUNNING
    assert runtime.applied_rpm == 0
    assert enabled(runtime)  # armed at standstill, the run command held
    assert standing(runtime) is None
    scene.chain.open(scene.rig.clock.monotonic())  # just after a tick and its read
    assert isinstance(scene.panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)
    await scene.run(1)  # takes the stop, and removes the run command in the same tick
    assert not enabled(runtime)
    assert not stopping(runtime)
    verdict = standing(runtime)
    assert verdict is not None
    assert (verdict.rule, verdict.detail) == (RULE_STO_OPEN, STO_OPEN_DEDUCED)
    refused = await scene.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    await scene.panel.close()


async def test_a_speed_asked_after_an_opening_during_a_walk_down_to_zero_is_refused(
    tmp_path: Path,
) -> None:
    """The opening latched during the walk-down: nothing is applied to a disabled drive."""
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime = scene.runtime
    await scene.cruise()
    assert isinstance(runtime.set_manual_target(OutputRpm(0)), Ok)
    await scene.run(1)
    assert stopping(runtime)
    assert runtime.state is RuntimeState.RUNNING
    scene.chain.open(scene.rig.clock.monotonic())
    await scene.run(3)
    scene.chain.close(scene.rig.clock.monotonic())
    verdict = runtime.standing
    assert verdict is not None
    assert verdict.rule == RULE_STO_OPEN
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Err)
    for _ in range(600):
        await scene.run(1)
        assert runtime.applied_rpm == 0
        assert scene.rig.simulator.commanded_setpoint == 0
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.SWITCH_ON_DISABLED
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Err)
    await scene.panel.close()


# =========================================================================
# A drive fault: the drive keeps FAULT, and the reads, which go on, see the chain
# =========================================================================


@pytest.mark.parametrize("reclosed", [False, True], ids=["left_open", "closed_again"])
async def test_a_chain_opening_during_a_faulted_drive_s_coast_is_read_and_latches_while_it_turns(
    tmp_path: Path, reclosed: bool
) -> None:
    """A faulted drive shows FAULT, not its output stage: the reading names the opening.

    No stop write acts on a faulted drive, so no read is withheld through its coast,
    and the chain is read open before the shaft stops, whether it closes again 2 s
    later or not. Then the usual way back: the named acknowledgement once READY, the
    operator's fault reset, a new start.
    """
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime = scene.runtime
    await scene.cruise()
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await scene.run(1)
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.FAULT
    assert not stopping(runtime)
    attempts = scene.attempts()
    opened = scene.now
    scene.chain.open(scene.rig.clock.monotonic())
    for _ in range(10):
        await scene.run(1)
        if scene.sto_open:
            break
    assert scene.sto_open, "no sto_open verdict"
    verdict = scene.sto_open[0]
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_OPEN,
        SafetyAction.QUICK_STOP,
        True,
    )
    assert verdict.detail != STO_OPEN_DEDUCED  # read: the faulted drive kept its word
    # The first read after the opening, then the tick that judges it.
    assert verdict.since - opened <= 2 * TICK + 1e-9
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.FAULT
    assert status.output_rpm > 0  # the shaft still coasts: no wait for the stop
    assert scene.attempts() > attempts
    await scene.run(10)
    if reclosed:
        scene.chain.close(scene.rig.clock.monotonic())
    await until_at_rest(scene)
    await scene.run(5)
    assert state(runtime) is RuntimeState.ENDING  # the run command waits for the reset
    assert scene.sto_open[0] == verdict
    surface = scene.panel.surface
    if not reclosed:
        assert scene.reading().state is StoState.OPEN
        not_ready = surface.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(not_ready, Err)
        assert isinstance(not_ready.error, StoNotReady)
        assert not_ready.error.state is StoState.OPEN
        scene.chain.close(scene.rig.clock.monotonic())
        await scene.run(5)
    assert scene.reading().state is StoState.READY
    assert isinstance(surface.acknowledge(OPERATOR, estop_released=True), Ok)
    assert isinstance(surface.submit_fault_reset(operator=OPERATOR), Ok)
    await scene.run(10)
    assert scene.rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    assert state(runtime) is RuntimeState.FINISHED
    assert standing(runtime) is None
    assert isinstance(await scene.start(), Ok)
    await scene.panel.close()


# =========================================================================
# Outside a stop too, the drive's word latches an opening no reading has shown
# =========================================================================


async def test_outside_a_stop_the_drive_word_latches_an_opening_before_any_reading_shows_it(
    tmp_path: Path,
) -> None:
    """The reads go on outside a stop, and the drive's word does not wait for the next one."""
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime = scene.runtime
    await scene.cruise()
    opened = scene.now
    scene.chain.open(scene.rig.clock.monotonic())
    await scene.step()  # the drive shows its output stage off; the reading is not refreshed yet
    status = runtime.drive_status
    assert status is not None
    assert status.state is DriveState.SWITCH_ON_DISABLED
    assert scene.reading().state is StoState.READY
    verdict = standing(runtime)
    assert verdict is not None
    assert (verdict.rule, verdict.action) == (RULE_STO_OPEN, SafetyAction.QUICK_STOP)
    assert verdict.detail == STO_OPEN_DEDUCED
    assert verdict.since - opened <= TICK + 1e-9
    assert runtime.applied_rpm == 0
    await scene.panel.close()


# =========================================================================
# Every path on which the console itself leaves the output stage: no sto_open
# =========================================================================


class RefusesTheRampStop(Noted):
    """A drive that will not take word 7 out of OPERATION_ENABLED: the console escalates."""

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        if word is ControlWord.SWITCH_ON and self.inner.sim_state is SimState.OPERATION_ENABLED:
            self.writes.append(("command", word.value))
            return Err(
                UnexpectedState(expected=DriveState.READY, actual=DriveState.OPERATION_ENABLED)
            )
        return await super().write_command(word)


class LosesTheReadAfterArming(Noted):
    """The first status read after the arming goes unanswered.

    The status the start read before arming the drive (SWITCH_ON_DISABLED, as a
    drive just powered up shows) then stands as the last one read.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        # Set by the arming's last word, cleared by the one read it then fails.
        self.armed: bool = False

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        written = await super().write_command(word)
        self.armed = self.armed or word is ControlWord.ENABLE_OPERATION
        return written

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self.armed:
            self.armed = False
            return Err(CommTimeout(after=Seconds(0.05)))
        return await self.inner.read_status()


async def until_at_rest(scene: Scene) -> None:
    """Tick until the drive's RFRD shows the shaft stopped."""
    for _ in range(1000):
        await scene.run(1)
        status = scene.runtime.drive_status
        if status is not None and status.output_rpm == 0:
            return
    raise AssertionError("the shaft never stopped")


async def until_finished(scene: Scene) -> None:
    for _ in range(1000):
        await scene.run(1)
        if scene.runtime.state is RuntimeState.FINISHED:
            return
    raise AssertionError("the session never finished")


async def normal_stop(scene: Scene) -> str | None:
    assert isinstance(scene.panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)
    await until_finished(scene)
    assert scene.rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    return None


async def screen_estop(scene: Scene) -> str | None:
    scene.panel.surface.submit_estop(operator=OPERATOR, reason="test")
    await until_finished(scene)
    assert scene.rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    return RULE_OPERATOR_ESTOP


async def go_silent(scene: Scene) -> str | None:
    scene.rig.simulator.inject_comms_loss(Seconds(60.0))
    await scene.run(100)
    assert scene.runtime.silent
    verdict = scene.runtime.standing
    assert verdict is not None
    assert verdict.action is SafetyAction.GO_SILENT
    return verdict.rule


async def fault_reset(scene: Scene) -> str | None:
    """FAULT_RESET leaves the drive SWITCH_ON_DISABLED, the run command cleared first."""
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await until_at_rest(scene)
    await scene.run(5)
    assert isinstance(scene.panel.surface.acknowledge(OPERATOR), Ok)
    assert isinstance(scene.panel.surface.submit_fault_reset(operator=OPERATOR), Ok)
    reset_disabled = False
    for _ in range(10):
        await scene.run(1)
        status = scene.runtime.drive_status
        reset_disabled = reset_disabled or (
            status is not None and status.state is DriveState.SWITCH_ON_DISABLED
        )
    assert reset_disabled  # read by the ticks between the reset and its SHUTDOWN
    assert scene.rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    return None


async def disable_escalation(scene: Scene) -> str | None:
    assert isinstance(scene.panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)
    await until_finished(scene)
    assert scene.rig.simulator.sim_state is SimState.READY_TO_SWITCH_ON
    return RULE_DISABLE_REFUSED


async def console_close(scene: Scene) -> str | None:
    """The console's exit: emergency zero, then the drive's own stop and disable."""
    report = await scene.runtime.shutdown("console exit")
    assert report.output_disabled
    assert not scene.runtime.drive_reads_permitted
    return None


EXPECTED_ENDS: Final[Mapping[str, tuple[type[Noted], Callable[[Scene], Awaitable[str | None]]]]] = {
    "normal_stop": (Noted, normal_stop),
    "arming_read_lost": (LosesTheReadAfterArming, normal_stop),
    "screen_estop": (Noted, screen_estop),
    "go_silent": (Noted, go_silent),
    "fault_reset": (Noted, fault_reset),
    "disable_escalation": (RefusesTheRampStop, disable_escalation),
    "console_close": (Noted, console_close),
}


@pytest.mark.parametrize("case", list(EXPECTED_ENDS))
async def test_the_console_leaving_the_output_stage_itself_latches_no_sto_open(
    tmp_path: Path, case: str
) -> None:
    """Arming, then each way this console removes the run command or stops writing.

    The drive's word is judged outside a stop as during one, so the arming and
    the session that follows it are judged too, the arming whose first read
    after the last word goes unanswered included.
    """
    wrap, scenario = EXPECTED_ENDS[case]
    scene = Scene(tmp_path, DRIVE_INPUT, wrap=wrap)
    await scene.cruise()  # the arming sequence: SHUTDOWN, SWITCH_ON, ENABLE_OPERATION
    assert scene.rig.simulator.sim_state is SimState.OPERATION_ENABLED
    remaining = await scenario(scene)
    assert scene.sto_open == []
    verdict = scene.runtime.standing
    assert (None if verdict is None else verdict.rule) == remaining
    await scene.panel.close()


class ResetLandsUnanswered(Noted):
    """FAULT_RESET reaches the drive, and its answer is lost."""

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        if word is not ControlWord.FAULT_RESET:
            return await super().write_command(word)
        self.writes.append(("command", word.value))
        landed = await self.inner.write_command(word)
        assert isinstance(landed, Ok)  # the drive took it
        return Err(CommTimeout(after=Seconds(0.5)))


async def test_a_fault_reset_whose_answer_is_lost_is_no_sto_opening(tmp_path: Path) -> None:
    """The reset leaves the drive SWITCH_ON_DISABLED with the run command still held.

    That is the reset's doing, not the chain's: no ``sto_open``. The session ends,
    the reading is READY, nothing is left to acknowledge, and the next start goes
    through.
    """
    scene = Scene(tmp_path, DRIVE_INPUT, wrap=ResetLandsUnanswered)
    runtime, surface = scene.runtime, scene.panel.surface
    await scene.cruise()
    scene.rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await until_at_rest(scene)
    await scene.run(5)
    assert enabled(runtime)  # the ending waits for the reset
    assert isinstance(surface.acknowledge(OPERATOR), Ok)
    assert isinstance(surface.submit_fault_reset(operator=OPERATOR), Ok)
    shown: list[DriveState] = []
    for _ in range(10):
        await scene.run(1)
        status = runtime.drive_status
        if status is not None:
            shown.append(status.state)
    assert ("command", ControlWord.FAULT_RESET.value) in [w[1:] for w in scene.writes]
    assert DriveState.SWITCH_ON_DISABLED in shown
    assert scene.rig.simulator.sim_state is SimState.SWITCH_ON_DISABLED  # no SHUTDOWN followed
    assert scene.sto_open == []
    assert not enabled(runtime)
    assert state(runtime) is RuntimeState.FINISHED
    assert scene.reading().state is StoState.READY
    assert standing(runtime) is None  # the fault's acknowledgement was the last one needed
    nothing = surface.acknowledge(OPERATOR)
    assert isinstance(nothing, Err)
    assert isinstance(nothing.error, NothingLatched)
    assert isinstance(await scene.start(), Ok)
    await scene.run(5)
    assert scene.sto_open == []
    assert sim_state(scene) is SimState.OPERATION_ENABLED
    await scene.panel.close()


# =========================================================================
# The acknowledgement after a stop needs a reading taken since
# =========================================================================


def acknowledged(scene: Scene) -> Result[object, object]:
    answer = scene.panel.surface.acknowledge(OPERATOR, estop_released=True)
    return Ok(None) if isinstance(answer, Ok) else Err(answer.error)


async def test_after_a_stop_only_a_reading_taken_since_lets_an_acknowledgement_through(
    tmp_path: Path,
) -> None:
    """The deduced opening is not acknowledged on the reading from before the stop."""
    scene = Scene(tmp_path, DRIVE_INPUT)
    runtime = scene.runtime
    await scene.cruise()
    assert isinstance(scene.panel.surface.submit_end(operator=OPERATOR, reason="stop"), Ok)
    await scene.run(1)  # the tick takes the stop: no read from here
    scene.chain.open(scene.rig.clock.monotonic())
    await scene.run(1)
    verdict = standing(runtime)
    assert verdict is not None
    assert (verdict.rule, verdict.detail) == (RULE_STO_OPEN, STO_OPEN_DEDUCED)
    before = scene.reading()
    last = before.last_success_at
    assert before.state is StoState.READY  # read before the stop, still recent
    assert last is not None
    assert scene.now - last < STO_STALE_AFTER
    refused = acknowledged(scene)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, StoNotReady)
    assert refused.error.state is StoState.UNKNOWN
    assert standing(runtime) == verdict
    scene.chain.close(scene.rig.clock.monotonic())  # closed again before the stop is over
    ended = False
    for _ in range(1000):
        await scene.step()
        if not stopping(runtime):
            ended = True
            break
        await scene.read()
        assert isinstance(acknowledged(scene), Err)
    assert ended
    assert isinstance(acknowledged(scene), Err)  # over, but nothing read since
    await scene.read()  # the first read after the stop
    after = scene.reading()
    assert after.state is StoState.READY
    assert after.last_success_at is not None
    assert after.last_success_at > last
    assert isinstance(acknowledged(scene), Ok)
    assert standing(runtime) is None
    await scene.panel.close()


async def test_a_chain_read_at_once_is_never_too_old_to_acknowledge_during_a_stop(
    tmp_path: Path,
) -> None:
    """A simulated chain is read at the instant asked: the requirement changes nothing for it."""
    scene = Scene(tmp_path, SIM_READY)
    await scene.cruise()
    scene.panel.surface.submit_estop(operator=OPERATOR, reason="test")
    await scene.run(2)
    assert stopping(scene.runtime)
    verdict = standing(scene.runtime)
    assert verdict is not None
    assert verdict.rule == RULE_OPERATOR_ESTOP
    assert isinstance(acknowledged(scene), Ok)
    await scene.panel.close()
