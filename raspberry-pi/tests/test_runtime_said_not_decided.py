"""What the runtime says for the screen, on the real tick, and that it decides nothing (ANH-190).

Four statements, each of them read by a screen and by no decision of the tick:

* ``nothing_waits``, handed to the supervisor for the wording of an unlatched
  verdict: a manual arm at rest with a target of zero has nothing to follow
  when a warning lifts, so the verdict no longer says that the speed climbs
  again by itself (the rule and its verdict are untouched:
  ``tests/test_safety_said_not_decided.py``);
* :attr:`~src.training.runtime.TrainingRuntime.floor`, the most severe latched
  verdict whoever holds it, for a status page that read the supervisor alone
  and showed none under a latch of the runtime's own;
* :attr:`~src.training.runtime.TrainingRuntime.silenced`, what ``GO_SILENT``
  stands over, wherever it is held;
* the manual view under a REDUCE: the ramp in force leads to zero, not to the
  target the arm is moving away from.

The rig is the runtime rig of ``tests/test_runtime.py`` with the machine's own
motion limits, as in ``tests/test_runtime_manual.py``.
"""

from __future__ import annotations

import pytest

from src.motor.drive import CommTimeout, ControlWord, DriveFault
from src.result import Err, Ok, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS, Ramp
from src.training.runtime import (
    RULE_DISABLE_REFUSED,
    RULE_DRIVE_PRECOMMANDED,
    RULE_TICK_EXCEPTION,
    HeldAtStandstill,
)
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_DRIVE_FAULT,
    RULE_HR_STALE,
    SELF_CLEARING,
    GoSilentIsTerminal,
)
from src.training.types import (
    Phase,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    TelemetrySnapshot,
)
from src.units import Bpm, MotorRpm, Seconds
from src.web.schemas import ManualRow
from tests.test_runtime import (
    GEOMETRY,
    OPERATOR,
    TICK,
    FakeState,
    Rig,
)
from tests.test_runtime import (
    _rig as _programme_rig,  # pyright: ignore[reportPrivateUsage]  # the shared rig builder
)
from tests.test_runtime_manual import (
    CEILING,
    _manual,  # pyright: ignore[reportPrivateUsage]
    _occupied,  # pyright: ignore[reportPrivateUsage]
    _out,  # pyright: ignore[reportPrivateUsage]
    _target,  # pyright: ignore[reportPrivateUsage]
)


def _rig(**kwargs: object) -> Rig:
    return _programme_rig(motion=DEFAULT_MOTION_LIMITS, **kwargs)  # type: ignore[arg-type]  # keyword pass-through


def _standing(rig: Rig) -> SafetyVerdict | None:
    """Read through a call, so a checker does not narrow one assertion into the next."""
    return rig.runtime.standing


def _floor(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.floor


def _silenced(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.silenced


def _warned(snapshots: list[TelemetrySnapshot], rule: str) -> list[SafetyVerdict]:
    """The unlatched verdicts of ``rule`` the snapshots carry, in order."""
    found = [snapshot.safety for snapshot in snapshots if snapshot.safety is not None]
    assert found, "no verdict was ever in force"
    assert {verdict.rule for verdict in found} == {rule}
    assert not any(verdict.latched for verdict in found)
    return found


# =========================================================================
# EX-7: a manual arm at rest with a target of zero has nothing to follow
# =========================================================================


async def test_at_rest_a_lost_heart_rate_is_not_said_to_lift_into_a_climb() -> None:
    """EX-7, as the ticket found it: ``hr_stale`` over a manual arm held at rest."""
    rig = await _occupied()
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime.manual_target == 0
    held = _warned(await rig.run(25.0, feed=False), RULE_HR_STALE)
    assert len(held) >= round(12.0 / TICK)
    assert {verdict.action for verdict in held} == {SafetyAction.FREEZE}
    for verdict in held:
        assert SELF_CLEARING not in verdict.detail
        assert "NOT LATCHED" not in verdict.detail
    assert rig.runtime.mode is RunMode.MANUEL
    assert rig.phase() is Phase.HOLD
    # And it is true: a target asked for now is refused, and when the heart rate is back
    # nothing has moved and nothing is waiting.
    refused = rig.runtime.set_manual_target(_out(200))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, HeldAtStandstill)
    back = await rig.run(20.0)
    assert back[-1].safety is None
    assert {int(snapshot.setpoint.motor_rpm) for snapshot in back} == {0}
    assert rig.runtime.manual_target == 0


async def test_an_empty_capsule_at_rest_is_not_said_to_climb_either() -> None:
    """The same over a bench arm: ``attendant_absent``, the one warning it can raise at rest."""
    rig = await _manual()
    held = _warned(await rig.run(70.0, ping=False), RULE_ATTENDANT_ABSENT)
    assert len(held) >= round(8.0 / TICK)
    assert all(SELF_CLEARING not in verdict.detail for verdict in held)


async def test_over_an_arm_that_turns_the_warning_still_says_it_lifts_by_itself() -> None:
    """Only where it was untrue: a turning arm follows its target again when the warning goes."""
    rig = await _manual()
    _target(rig, 200)
    await rig.run(30.0)
    assert rig.runtime.applied_rpm == 200
    held = _warned(await rig.run(70.0, ping=False), RULE_ATTENDANT_ABSENT)
    assert all(verdict.detail.endswith(SELF_CLEARING) for verdict in held)


async def test_an_arm_stopped_with_a_target_left_above_it_still_says_so() -> None:
    """Setpoint zero is not enough: the statement is made of the target too.

    The operator's own zero, then a target typed with nothing standing: the
    profiler has not written its first step when the tick is taken. The arm
    is at rest and something IS waiting, so nothing is stated.
    """
    rig = await _manual()
    assert rig.runtime.set_manual_target(_out(200)) == Ok(MotorRpm(200))
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime._nothing_waits() is False  # pyright: ignore[reportPrivateUsage]
    rig.runtime.set_manual_target(_out(0))
    assert rig.runtime._nothing_waits() is True  # pyright: ignore[reportPrivateUsage]


async def test_a_target_caught_by_a_warning_is_said_to_wait_for_that_tick() -> None:
    """The one tick on which a target does wait behind a warning over a stopped arm.

    A target accepted with nothing standing; the warning appears on the very
    next tick, before the first step. On that tick the target is still there
    when the supervisor is told, so the sentence is said, and it is true: the
    runtime takes the target back at the end of that same tick. From the next
    one nothing waits, and nothing says otherwise.
    """
    witness = await _manual()
    quiet = 0
    while _standing(witness) is None:
        await witness.step(ping=False)
        quiet += 1
    assert _standing(witness) is not None

    rig = await _manual()
    for _ in range(quiet - 1):
        await rig.step(ping=False)
    assert _standing(rig) is None
    assert rig.runtime.set_manual_target(_out(200)) == Ok(MotorRpm(200))
    caught = (await rig.step(ping=False)).safety
    assert caught is not None
    assert (caught.rule, caught.latched) == (RULE_ATTENDANT_ABSENT, False)
    assert caught.detail.endswith(SELF_CLEARING)
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime.manual_target == 0, "the target was left waiting behind the warning"
    assert rig.runtime.take_withdrawn_target() is not None
    after = (await rig.step(ping=False)).safety
    assert after is not None
    assert SELF_CLEARING not in after.detail


async def test_a_programme_at_its_baseline_is_never_stated_to_have_nothing_waiting() -> None:
    """Setpoint zero, no manual target at all: the programme starts the arm by itself."""
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(2.0)
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime._nothing_waits() is False  # pyright: ignore[reportPrivateUsage]


async def test_an_idle_runtime_states_nothing() -> None:
    rig = _rig()
    await rig.run(1.0)
    assert rig.runtime._nothing_waits() is False  # pyright: ignore[reportPrivateUsage]


# =========================================================================
# The latched floor, whoever holds it
# =========================================================================


async def _found_running() -> Rig:
    """A drive left enabled and turning: the idle console stops it and latches its own verdict."""
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(120))
    await rig.run(1.0)
    latched = _standing(rig)
    assert latched is not None
    assert (latched.rule, latched.action, latched.latched) == (
        RULE_DRIVE_PRECOMMANDED,
        SafetyAction.QUICK_STOP,
        True,
    )
    return rig


async def test_a_latch_of_the_runtime_s_own_is_on_its_floor_and_not_on_the_supervisor_s() -> None:
    """Item 1: the status page read the supervisor's floor, which shows nothing here."""
    rig = await _found_running()
    assert rig.runtime.supervisor.floor is None
    assert rig.runtime.supervisor.standing is None
    assert _floor(rig) == _standing(rig)


async def test_with_nothing_latched_the_floor_is_empty() -> None:
    rig = await _manual()
    await rig.run(1.0)
    assert _floor(rig) is None
    assert _silenced(rig) is None


async def test_a_latch_of_the_supervisor_s_is_on_the_runtime_s_floor_as_it_is_on_its_own() -> None:
    rig = await _manual()
    _target(rig, 200)
    await rig.run(30.0)
    rig.drive.inject_fault(DriveFault.DC_BUS_OVERVOLTAGE)
    await rig.run(1.0)
    held = rig.runtime.supervisor.floor
    assert held is not None
    assert held.rule == RULE_DRIVE_FAULT
    assert _floor(rig) == held


async def test_of_two_latches_the_floor_is_the_more_severe_and_the_runtime_s_on_a_tie() -> None:
    rig = await _found_running()
    own = _standing(rig)
    # Less severe, held by the supervisor: the runtime's stop is the floor.
    rig.runtime.trip_from_thread("rig_ramp", SafetyAction.RAMP_DOWN, "under test")
    await rig.step()
    assert rig.runtime.supervisor.floor is not None
    assert _floor(rig) == own
    # As severe: still the runtime's own, as for the verdict in force.
    rig.runtime.trip_from_thread("rig_stop", SafetyAction.QUICK_STOP, "under test")
    await rig.step()
    assert _floor(rig) == own
    assert _standing(rig) == own
    # More severe: the supervisor's.
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    await rig.step()
    assert _floor(rig) == rig.runtime.supervisor.floor
    assert rig.standing_action() is SafetyAction.GO_SILENT


# =========================================================================
# What GO_SILENT stands over, wherever it is held
# =========================================================================


async def _stopped_by_a_rule() -> tuple[Rig, SafetyVerdict]:
    """A latched QUICK_STOP on the supervisor's floor, over a manual arm."""
    rig = await _manual()
    rig.runtime.trip_from_thread("rig_stop", SafetyAction.QUICK_STOP, "under test")
    await rig.run(1.0)
    stopped = rig.runtime.supervisor.floor
    assert stopped is not None
    assert (stopped.rule, stopped.action) == ("rig_stop", SafetyAction.QUICK_STOP)
    assert _silenced(rig) is None, "nothing hides a stop that stands"
    return rig, stopped


async def _lose_the_link(rig: Rig) -> None:
    rig.drive.break_comms()
    await rig.run(2.0)
    silent = rig.runtime.supervisor.floor
    assert silent is not None
    assert (silent.rule, silent.action) == (RULE_COMMS_LOST, SafetyAction.GO_SILENT)


async def _raise_in_the_tick(rig: Rig) -> None:
    rig.drive.raise_on_status = True
    with pytest.raises(RuntimeError):
        await rig.step()
    silent = _standing(rig)
    assert silent is not None
    assert (silent.rule, silent.action) == (RULE_TICK_EXCEPTION, SafetyAction.GO_SILENT)


async def test_the_supervisor_s_stop_replaced_by_the_supervisor_s_go_silent_is_named() -> None:
    """Item 2. A rule stops the arm, the link is then lost: both are the supervisor's."""
    rig, stopped = await _stopped_by_a_rule()
    await _lose_the_link(rig)
    assert _floor(rig) == rig.runtime.supervisor.floor
    assert rig.standing_action() is SafetyAction.GO_SILENT
    assert _silenced(rig) == stopped


async def test_the_supervisor_s_stop_under_the_runtime_s_own_go_silent_is_named() -> None:
    """The floor still holds the stop; the runtime's latch stands over it."""
    rig, stopped = await _stopped_by_a_rule()
    await _raise_in_the_tick(rig)
    assert rig.runtime.supervisor.floor == stopped
    assert rig.runtime.supervisor.silenced is None
    assert _silenced(rig) == stopped


async def test_the_runtime_s_own_stop_replaced_by_its_own_go_silent_is_named() -> None:
    """A drive found running, then a bug in the tick: one latch, which kept what it replaced."""
    rig = await _found_running()
    own = _standing(rig)
    await _raise_in_the_tick(rig)
    assert rig.runtime.supervisor.floor is None
    assert _silenced(rig) == own


async def test_the_runtime_s_own_stop_under_the_supervisor_s_go_silent_is_named() -> None:
    """A drive found running, then the link lost: the runtime's latch is still its own."""
    rig = await _found_running()
    own = _standing(rig)
    await _lose_the_link(rig)
    assert rig.runtime.supervisor.silenced is None
    assert _silenced(rig) == own


async def test_go_silent_with_nothing_latched_behind_it_silences_nothing() -> None:
    rig = await _manual()
    _target(rig, 200)
    await rig.run(10.0)
    await _lose_the_link(rig)
    assert _silenced(rig) is None


async def test_the_most_severe_of_what_stands_behind_go_silent_is_the_one_named() -> None:
    """A refused stop word (the runtime's, RAMP_DOWN) and a rule's stop: the stop is named."""
    rig = await _manual()
    _target(rig, 100)
    await rig.run(15.0)
    rig.runtime.request_stop("operator: stop button")
    rig.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.5))
    await rig.run(40.0)
    own = _standing(rig)
    assert own is not None
    assert (own.rule, own.action) == (RULE_DISABLE_REFUSED, SafetyAction.RAMP_DOWN)
    rig.runtime.trip_from_thread("rig_stop", SafetyAction.QUICK_STOP, "under test")
    await rig.step()
    stopped = rig.runtime.supervisor.floor
    assert stopped is not None
    await _lose_the_link(rig)
    assert _silenced(rig) == stopped
    assert _floor(rig) == rig.runtime.supervisor.floor


async def test_what_is_silenced_cannot_be_acknowledged_away() -> None:
    """Nothing is acknowledged under GO_SILENT: the stop behind it stays named."""
    rig, stopped = await _stopped_by_a_rule()
    await _lose_the_link(rig)
    refused = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, GoSilentIsTerminal)
    await rig.run(5.0)
    assert _silenced(rig) == stopped


# =========================================================================
# The manual view under a REDUCE: the ramp in force leads to zero
# =========================================================================


async def test_under_a_reduce_the_ramp_announced_is_the_descent_to_zero() -> None:
    """Item 4. The view said "ramp in progress" with the time to climb back to 300."""
    rig = await _manual()
    _target(rig, 300)
    await rig.run(25.0)
    assert rig.runtime.applied_rpm == 300
    at_target = rig.runtime.snapshot().manual
    assert at_target is not None
    assert not at_target.ramping
    assert at_target.ramp_to == at_target.target

    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    down = [snapshot for snapshot in await rig.run(12.0) if snapshot.setpoint.motor_rpm != 0]
    assert len(down) >= round(8.0 / TICK), "the arm stopped before anything was read"
    for snapshot in down:
        view = snapshot.manual
        assert view is not None
        assert snapshot.safety_action is SafetyAction.REDUCE
        # The operator's target is kept, and is not where the setpoint is going.
        assert view.target.motor_rpm == 300
        assert view.ramp_to is not None
        assert view.ramp_to.motor_rpm == 0
        assert view.ramping, "the setpoint is moving: the head must not"
        assert view.ramp == Ramp(
            start=snapshot.setpoint.motor_rpm,
            target=MotorRpm(0),
            limits=DEFAULT_MOTION_LIMITS,
            geometry=GEOMETRY,
        )
    # The arrival time is the time to zero, and it is a true one: it shrinks by a tick a tick.
    first = down[0].manual
    last = down[-1].manual
    assert first is not None
    assert last is not None
    announced = first.ramp_eta
    later = last.ramp_eta
    assert announced is not None
    assert later is not None
    assert abs((announced - later) - (len(down) - 1) * TICK) <= 2 * TICK
    row = ManualRow.of(first)
    assert row is not None
    assert row.ramp_to is not None
    assert row.ramp_to.motor_rpm == 0
    assert row.ramp_eta_s == announced


async def test_a_reduce_over_a_target_of_zero_announces_the_same_ramp_it_always_did() -> None:
    """Target zero: the descent IS the walk to the target, and the view says nothing new."""
    rig = await _manual()
    _target(rig, 300)
    await rig.run(25.0)
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    await rig.step()
    assert rig.runtime.set_manual_target(_out(0)) == Ok(MotorRpm(0))
    view = (await rig.step()).manual
    assert view is not None
    assert view.ramping
    assert view.ramp_to == view.target
    assert view.target.motor_rpm == 0


async def test_with_no_verdict_the_ramp_leads_to_the_target() -> None:
    rig = await _manual()
    _target(rig, 200)
    climbing = (await rig.run(5.0))[-1].manual
    assert climbing is not None
    assert climbing.ramping
    assert climbing.ramp_to == climbing.target
    assert climbing.ramp_to is not None
    assert climbing.ramp_to.motor_rpm == 200


async def test_a_freeze_that_holds_the_setpoint_names_no_destination() -> None:
    """Nothing is walking: no ramp, no arrival time, and nowhere it is going."""
    rig = await _manual()
    _target(rig, 300)
    await rig.run(10.0)
    assert 0 < rig.runtime.applied_rpm < 300
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    held = (await rig.run(2.0))[-1].manual
    assert held is not None
    assert not held.ramping
    assert held.ramp is None
    assert held.ramp_to is None
    row = ManualRow.of(held)
    assert row is not None
    assert row.ramp_to is None
    assert row.ramp_eta_s is None


def test_the_ceiling_of_these_sessions_is_the_bench_one() -> None:
    """The cases above climb to 300 motor rpm: the ceiling the shared helper arms with."""
    assert MotorRpm(300) == CEILING
