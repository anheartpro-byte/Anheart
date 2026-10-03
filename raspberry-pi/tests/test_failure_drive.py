"""Motor / variateur failures injected into the REAL console.

Each case builds the console over a ``SimulatedDrive`` (the ATV320's CiA402
machine, ramps, coast-down and ``ttO`` watchdog), optionally behind one faulty
backend of the rig, climbs a bench manual session to 20 output rpm (996 motor
rpm) through the operator's HTTP API, injects the failure, and judges:

* the verdict rule the operator is shown, with its explanation (and, for a
  drive fault, the drive's own mnemonic), and the end reason;
* the machine as LEFT: shaft at 0, output stage off - or, where the runtime
  had to go silent, the drive's own ``ttO`` having latched SLF;
* no NaN in any snapshot.

Comms loss is also injected at every phase of a programme (heart-rate driven),
through the ECG seams of ``tests/test_failure_ecg.py``.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Final, override

import httpx
import pytest

from src.clock import ManualClock
from src.motor.drive import (
    ControlWord,
    DriveError,
    DriveFault,
    DriveState,
    DriveStatus,
    UnexpectedState,
)
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Err, Ok, Result
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Phase, SafetyAction
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from tests.test_failure_ecg import PHASE_AT, programme, run_to
from tests.test_failure_rig import (
    OPERATOR,
    Rig,
    Wrapped,
    attest,
    make_rig,
    precommanded,
    start_manual,
)

CRUISE_OUTPUT_RPM: Final[float] = 20.0
"""996 motor rpm: well inside the bench ceiling, fast enough to matter."""

CLIMB_S: Final[float] = 90.0
"""0 -> 20 output rpm at the 0.25 output rpm/s motion limit takes 80 s."""


def _phase_id(phase: Phase) -> str:
    return phase.value


def _fault_id(fault: DriveFault) -> str:
    return fault.name


async def cruising(tmp_path: Path, wrap: type[Wrapped] | None = None) -> tuple[Rig, Wrapped | None]:
    rig, wrapper = make_rig(tmp_path, wrap=wrap)
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE_OUTPUT_RPM)
        await rig.tick(CLIMB_S)
    assert abs(rig.panel.runtime.snapshot().measured.motor_rpm - 996) <= 2
    return rig, wrapper


def rules_since(rig: Rig, index: int) -> list[str]:
    seen: list[str] = []
    for snapshot in rig.snapshots[index:]:
        if snapshot.safety is not None and snapshot.safety.rule not in seen:
            seen.append(snapshot.safety.rule)
    return seen


# =========================================================================
# Comms timeout, at every phase
# =========================================================================


async def test_comms_loss_while_idle_never_moves_anything_and_is_shown(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    await rig.tick(5.0)
    rig.simulator.inject_comms_loss(Seconds(20.0))
    await rig.tick(10.0)
    link = rig.panel.runtime.idle_link
    assert link.consecutive_failures > 0
    assert link.last_error, "the operator's link panel says nothing"
    await rig.tick(15.0)
    assert rig.panel.runtime.idle_link.consecutive_failures == 0, "the idle link never recovered"
    assert max(abs(s.measured.motor_rpm) for s in rig.snapshots) == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


@pytest.mark.parametrize("moment", ["climbing", "cruising", "stopping"])
async def test_comms_loss_in_a_manual_session_goes_silent_and_tto_stops_it(
    tmp_path: Path, moment: str
) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE_OUTPUT_RPM)
        await rig.tick(30.0 if moment == "climbing" else CLIMB_S)
        if moment == "stopping":
            stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
            assert stop.status_code == httpx.codes.ACCEPTED
            await rig.tick(10.0)
    mark = len(rig.snapshots)
    rig.simulator.inject_comms_loss(Seconds(30.0))
    last = await rig.tick(5.0)
    runtime = rig.panel.runtime
    assert runtime.silent
    assert "comms_lost" in rules_since(rig, mark)
    assert last.safety is not None
    assert last.safety.action is SafetyAction.GO_SILENT
    assert "ttO" in last.safety.detail
    expected = EndReason.OPERATOR_STOP if moment == "stopping" else EndReason.SAFETY_VERDICT
    assert runtime.end_reason is expected
    await rig.tick(40.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""
    assert rig.silent_backstop(), f"ttO did not latch: {rig.simulator.sim_state.name}"


@pytest.mark.parametrize(
    "phase",
    [Phase.BASELINE, Phase.WARMUP, Phase.HOLD, Phase.COOLDOWN, Phase.RECOVERY],
    ids=_phase_id,
)
async def test_comms_loss_at_every_programme_phase_goes_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: Phase
) -> None:
    run = programme(tmp_path, monkeypatch)
    await run_to(run, PHASE_AT[phase], real_dsp=False)
    assert run.panel.runtime.snapshot().phase is phase
    run.rig.simulator.inject_comms_loss(Seconds(30.0))
    last = await run.rig.tick(5.0)
    runtime = run.panel.runtime
    assert runtime.silent
    assert last.safety is not None
    assert last.safety.rule == "comms_lost"
    assert runtime.end_reason in {EndReason.SAFETY_VERDICT, EndReason.PROGRAMME_COMPLETE}
    await run.rig.tick(40.0)
    await run.rig.panel.close()
    assert await run.rig.left_stopped() == ""


# =========================================================================
# Drive FAULT codes
# =========================================================================

INJECTABLE: Final[tuple[DriveFault, ...]] = tuple(
    fault for fault in DriveFault if fault is not DriveFault.NO_FAULT_STORED
)


@pytest.mark.parametrize("fault", INJECTABLE, ids=_fault_id)
async def test_a_drive_fault_at_speed_ends_the_session_with_its_mnemonic(
    tmp_path: Path, fault: DriveFault
) -> None:
    rig, _ = await cruising(tmp_path)
    mark = len(rig.snapshots)
    rig.simulator.inject_fault(fault)
    last = await rig.tick(3.0)
    runtime = rig.panel.runtime
    assert "drive_fault" in rules_since(rig, mark)
    assert runtime.end_reason is EndReason.SAFETY_VERDICT
    assert last.fault is not None
    shown = last.fault.message
    # The drive's own display code, or - for a code this build cannot name - the raw LFT.
    assert fault.mnemonic in shown or str(last.fault.raw_code) in shown, shown
    # No automatic reset, no automatic resumption, whatever happens next.
    await rig.tick(90.0)
    assert rig.simulator.sim_state in {SimState.FAULT, SimState.FAULT_REACTION_FREEWHEEL}
    setpoints = [int(s.setpoint.motor_rpm) for s in rig.snapshots[mark:]]
    assert all(b <= a for a, b in pairwise(setpoints)), "the setpoint rose after a drive fault"
    assert setpoints[-1] == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


# =========================================================================
# Drive stuck enabled at startup
# =========================================================================

PRECOMMANDED_DEFECT: Final[str] = (
    "DEFECT (src/training/runtime.py idle poll / src/local_panel.py): a drive found "
    "OPERATION_ENABLED and turning at 900 motor rpm (18 output rpm) when the console "
    "starts is only READ while the console is idle. After 120 s the shaft still turns at "
    "900 rpm, LFRD is still 900, no verdict is latched and the snapshot shows no safety "
    "message; the 200 idle reads (reads reset ttO on a real ATV320, "
    "reads_reset_watchdog=True) keep the drive's own watchdog fed, so nothing ever stops "
    "it until an operator happens to press START (drive_precommanded). Contract rule 8: "
    "'If ETA reports OPERATION_ENABLED ... command zero, disable, latch'."
)


async def test_a_drive_found_turning_at_startup_is_stopped_without_a_start(
    tmp_path: Path,
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    rig, _ = make_rig(tmp_path, simulator=await precommanded(clock, 900), clock=clock)
    last = await rig.tick(30.0)
    assert last.safety is not None, "the operator is not told the drive was left running"
    assert abs(last.measured.motor_rpm) < 1, f"still turning at {last.measured.motor_rpm} rpm"


async def test_a_drive_found_turning_refuses_the_start_and_is_zeroed(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    rig, _ = make_rig(tmp_path, simulator=await precommanded(clock, 900), clock=clock)
    await rig.tick(2.0)
    async with rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED
        last = await rig.tick(20.0)
    assert last.safety is not None
    assert last.safety.rule == "drive_precommanded"
    assert last.safety.action is SafetyAction.QUICK_STOP
    # The idle console now stops the drive and latches drive_precommanded within
    # its first poll, so START is refused on that standing verdict (it used to be
    # refused as DrivePrecommanded, "deja en marche", because nothing had looked).
    assert any("drive_precommanded" in refusal for refusal in rig.refusals()), rig.refusals()
    assert abs(last.measured.motor_rpm) < 1
    await rig.panel.close()
    assert await rig.left_stopped() == ""


# =========================================================================
# Lies from the drive: echo, misaddressed writes, speed not following, frozen status
# =========================================================================


class EchoMismatch(Wrapped):
    """LFRD reads back 300 rpm above what was written (a wrong register, a stale echo)."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed: bool = False

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        status = await self.inner.read_status()
        if not self.armed or isinstance(status, Err):
            return status
        value = status.value
        return Ok(replace(value, setpoint_echo_rpm=MotorRpm(value.setpoint_echo_rpm + 300)))


ECHO_DEFECT: Final[str] = (
    "DEFECT (src/training/runtime.py): DriveStatus.setpoint_echo_rpm is never checked. "
    "With the LFRD echo reading 300 rpm above every write (1296 vs 996) for 60 s at "
    "20 output rpm, no rule fires, the session stays RUNNING and the operator is shown "
    "nothing; drive.py's contract says 'Writing is not landing: verify against "
    "setpoint_echo_rpm on the next read'. See also the misaddressed-write case."
)


async def test_a_setpoint_echo_that_disagrees_with_the_write_is_acted_on(tmp_path: Path) -> None:
    rig, wrapper = await cruising(tmp_path, EchoMismatch)
    assert isinstance(wrapper, EchoMismatch)
    mark = len(rig.snapshots)
    wrapper.armed = True
    await rig.tick(60.0)
    try:
        assert rules_since(rig, mark), "no verdict on a setpoint that never landed"
    finally:
        await rig.panel.close()
        assert await rig.left_stopped() == ""


async def test_writes_that_never_land_end_with_the_shaft_stopped(tmp_path: Path) -> None:
    """Acknowledged writes landing in the wrong register: the runtime goes silent.

    This was a strict xfail: tracking_error RAMP_DOWN fired, the emergency zero
    reported ACKNOWLEDGED, and the misaddressed keepalives kept feeding the
    drive's ttO while the shaft turned on at 799 motor rpm for the whole 120 s.
    Now the LFRD echo disagreeing with the writes (setpoint_unconfirmed) plus a
    shaft outside its envelope escalates tracking_error to GO_SILENT, and the
    drive's own ttO stops the motor. Once silent the runtime reads nothing, so
    its snapshot keeps the last reading (COMM_LOST, ageing): the stop is judged
    on the drive itself.
    """
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE_OUTPUT_RPM)
        await rig.tick(60.0)
    rig.simulator.inject_register_offset_error()
    last = await rig.tick(120.0)
    try:
        assert last.safety is not None
        assert last.safety.rule == "tracking_error"
        assert last.safety.action is SafetyAction.GO_SILENT
        assert "echo" in last.safety.detail
        assert rig.panel.runtime.silent, "the misaddressed keepalive is still feeding ttO"
        assert rig.simulator.sim_state is SimState.FAULT, rig.simulator.sim_state
    finally:
        await rig.panel.close()
    assert await rig.left_stopped() == ""


class HalfSpeed(Wrapped):
    """The shaft really turns at half the reference: a slipping belt, a jammed load.

    Modelled on the measurement only (RFRD reads half), which is all the runtime
    can ever see of it.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed: bool = False

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        status = await self.inner.read_status()
        if not self.armed or isinstance(status, Err):
            return status
        value = status.value
        return Ok(replace(value, output_rpm=MotorRpm(value.output_rpm // 2)))


async def test_a_measured_speed_that_does_not_follow_trips_tracking_error(tmp_path: Path) -> None:
    rig, wrapper = await cruising(tmp_path, HalfSpeed)
    assert isinstance(wrapper, HalfSpeed)
    mark = len(rig.snapshots)
    wrapper.armed = True
    last = await rig.tick(5.0)
    assert "tracking_error" in rules_since(rig, mark)
    assert last.safety is not None
    assert "measures" in last.safety.detail
    assert rig.panel.runtime.end_reason is EndReason.SAFETY_VERDICT
    await rig.tick(90.0)
    assert rig.simulator.commanded_setpoint == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


class Frozen(Wrapped):
    """The drive keeps answering with the SAME status, forever: a frozen gateway."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed: bool = False
        self.frozen: Result[DriveStatus, DriveError] | None = None

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        status = await self.inner.read_status()
        if not self.armed:
            return status
        if self.frozen is None:
            self.frozen = status
        return self.frozen


async def test_a_frozen_status_at_cruise_is_caught_when_the_speed_should_change(
    tmp_path: Path,
) -> None:
    rig, wrapper = await cruising(tmp_path, Frozen)
    assert isinstance(wrapper, Frozen)
    wrapper.armed = True
    await rig.tick(10.0)
    mark = len(rig.snapshots)
    async with rig.http() as session:
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == httpx.codes.ACCEPTED
    await rig.tick(120.0)
    runtime = rig.panel.runtime
    assert "tracking_error" in rules_since(rig, mark)
    # The runtime cannot prove standstill from a frozen status, so it must not
    # remove the run command; the reference is zero and stays zero.
    assert runtime.state is RuntimeState.ENDING
    assert rig.simulator.commanded_setpoint == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


# =========================================================================
# The drive refusing a command word
# =========================================================================


class RefuseWord(Wrapped):
    """The drive answers one control word with a state error, as a misconfigured drive does."""

    word: ControlWord = ControlWord.ENABLE_OPERATION

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed: bool = True

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        if self.armed and word is self.word:
            return Err(
                UnexpectedState(
                    expected=DriveState.OPERATION_ENABLED, actual=DriveState.SWITCHED_ON
                )
            )
        return await self.inner.write_command(word)


class RefuseSwitchOn(RefuseWord):
    word: ControlWord = ControlWord.SWITCH_ON

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed = False


async def test_a_refused_enable_refuses_the_start_and_moves_nothing(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path, wrap=RefuseWord)
    async with rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED
        await rig.tick(2.0)
        target = await session.post(
            "/api/manual/target", json={"output_rpm": 5.0, "operator": OPERATOR}
        )
        assert target.status_code == httpx.codes.CONFLICT
        assert target.json()["detail"]
    await rig.tick(5.0)
    assert rig.panel.runtime.state is RuntimeState.IDLE
    assert any("demarrage refuse" in refusal for refusal in rig.refusals()), rig.refusals()
    assert max(s.setpoint.motor_rpm for s in rig.snapshots) == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


REFUSED_DISABLE_DEFECT: Final[str] = (
    "DEFECT (src/training/runtime.py _settle): when the drive refuses SWITCH_ON at "
    "standstill after an operator STOP, the runtime retries every tick forever: 120 s "
    "later the session is still ENDING, the output stage is still OPERATION_ENABLED "
    "(holding torque at 0 rpm), no verdict stands and no event tells the operator the "
    "drive would not disable; the failure is only in the log."
)


async def test_a_refused_disable_at_standstill_is_shown_to_the_operator(tmp_path: Path) -> None:
    rig, wrapper = await cruising(tmp_path, RefuseSwitchOn)
    assert isinstance(wrapper, RefuseSwitchOn)
    wrapper.armed = True
    async with rig.http() as session:
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == httpx.codes.ACCEPTED
    last = await rig.tick(120.0)
    try:
        assert abs(last.measured.motor_rpm) < 1
        told = last.safety is not None or len(rig.refusals()) > 0
        assert told or not rig.panel.runtime.output_enabled, (
            "output stage still enabled at standstill and the operator is told nothing"
        )
    finally:
        await rig.panel.close()
        assert await rig.left_stopped() == ""
