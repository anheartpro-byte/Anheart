"""A stopped arm never restarts by itself: a standstill inside a session ends it (ANH-176).

The product decisions of 2026-10-05 and 2026-10-06 (``docs/securite.md``).
Once the arm has moved in a session, a setpoint that comes back to zero without
anybody having asked for it ends the session, latched: nothing moves again
without an acknowledgement by name and a new start. Two things brought the arm
to such a standstill and then took it out again with nobody clicking:

* **an unlatched warning** (a REDUCE). The detached electrode, measured on
  ``develop`` on the shipped 30 min programme with the console's own limits:
  12 s without a heart rate, 164 motor rpm held; 32 s, 122 rpm under REDUCE;
  42 s, standstill, the mode still SEANCE; the heart rate returns at 50 s; 45 s
  after that the setpoint is 69 rpm, and two minutes after it 168. The operator
  saw the arm stopped, walked to the capsule to refit the electrode, and it
  started beside them;
* **the heart-rate regulation itself**, which writes zero when the rate is
  above the zone and leaves zero again when it has come back down, with no
  verdict on the screen at any point. It also takes the LAST step when a
  warning lifts at the minimum running speed with the rate just above the
  zone: the same sequence for the person watching, and the first version of
  this change (head ``91b0e90``) missed it.

A zero somebody asked for is not a standstill "by itself": an operator's stop,
a manual target of zero typed with no warning standing, the programme's own
cooldown. And a warning before the arm has moved at all (BASELINE) is left as
it was: what follows it is the programme's normal start.

Three groups of tests, and the split matters when reading a failure:

* **the decision**: these fail on ``develop`` (the arm restarts, or nothing is
  latched) and pass here. Those marked "also on 91b0e90" in their docstring
  fail on the first version of this change as well;
* **unchanged on purpose**: a held speed and a speed lowered part way still
  resume by themselves, the regulation still lowers and raises a turning arm,
  and what the decision leaves out behaves exactly as before. These pass on
  both;
* **what the screen is told** while the speed can still resume by itself.

Every test runs on the fake drive and the manual clock of
``tests/test_runtime.py``. Where the machine's own numbers matter the rig uses
the shipped profile, the console's ``RUNTIME_LIMITS`` and the shipped motion
limits; elsewhere the accelerated rig keeps the walk to standstill short.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

import pytest

from src.local_panel import RUNTIME_LIMITS, describe_start_refusal
from src.motor.drive import CommTimeout, ControlWord
from src.result import Err, Ok, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.plan import MIN_RECOVERY_S, TrainingProfile
from src.training.runtime import (
    RULE_DISABLE_REFUSED,
    AlreadyStarted,
    Ending,
    EndReason,
    ManualEnding,
    RuntimeState,
    SafetyStanding,
)
from src.training.safety import (
    ALL_RULES,
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_CURRENT_HIGH,
    RULE_HR_CRITICAL,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    RULE_OPERATOR_ESTOP,
    RULE_SESSION_STANDSTILL,
    SELF_CLEARING,
    GoSilentIsTerminal,
    SafetyLimits,
    SafetySupervisor,
    Unattributed,
)
from src.training.types import (
    Occupancy,
    Phase,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    TelemetrySnapshot,
    is_rule_id,
)
from src.units import Amperes, Bpm, MotorRpm, OutputRpm, Seconds, motor_to_output_rpm
from src.web.schemas import SnapshotRow
from tests.test_runtime import (
    GEOMETRY,
    OPERATOR,
    REAL_PROFILE,
    Occupant,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _lenient_comms,  # pyright: ignore[reportPrivateUsage]
    _profile,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
    _running_rig,  # pyright: ignore[reportPrivateUsage]
    _safety_for,  # pyright: ignore[reportPrivateUsage]
)

AT_SPEED: Final[float] = 420.0
"""Seconds into the shipped programme at which the arm turns at its WARMUP ceiling."""

IN_HOLD: Final[float] = 500.0
"""Seconds into the shipped programme at which HOLD is under way, with speed still to gain."""

LONG_HOLD: Final[TrainingProfile] = _profile(total_duration_s=Seconds(600.0))
"""The accelerated rig's profile with a HOLD of several minutes (the default one is 30 s)."""

MANUAL_CEILING: Final[MotorRpm] = MotorRpm(300)
MIN_RUN: Final[MotorRpm] = REAL_PROFILE.min_run_rpm

BY_A_WARNING: Final[str] = "the warning {} brought the setpoint to zero"
BY_THE_REGULATION: Final[str] = "the heart-rate regulation brought the setpoint to zero"
"""The two causes the latched ending can name, as the operator reads them."""


# =========================================================================
# Reading the runtime through calls
# =========================================================================
#
# mypy narrows a property to what a previous assertion established and then
# calls the next assertion about the same expression unreachable. Every one of
# these tests is "it was X, then it became Y", so each fact is read through a
# call, which is re-widened every time (see ``Rig.phase`` in test_runtime.py).


def _standing(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.standing


def _demand(rig: Rig) -> tuple[str, SafetyAction, bool] | None:
    """The standing verdict as ``(rule, action, latched)``, or ``None``."""
    verdict = rig.runtime.standing
    return None if verdict is None else (verdict.rule, verdict.action, verdict.latched)


def _floor(rig: Rig) -> tuple[str, SafetyAction, bool] | None:
    """The SUPERVISOR's latched floor: what the status page and the start gate read."""
    verdict = rig.runtime.supervisor.floor
    return None if verdict is None else (verdict.rule, verdict.action, verdict.latched)


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _end(rig: Rig) -> EndReason | None:
    return rig.runtime.end_reason


def _ending(rig: Rig) -> Ending | None:
    return rig.runtime.ending


def _mode(rig: Rig) -> RunMode:
    return rig.runtime.mode


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _moved(snapshots: list[TelemetrySnapshot]) -> list[tuple[float, int]]:
    """Every non-zero setpoint, as ``(session time, motor rpm)``."""
    return [
        (float(snapshot.elapsed), int(snapshot.setpoint.motor_rpm))
        for snapshot in snapshots
        if snapshot.setpoint.motor_rpm != 0
    ]


def _rules_shown(snapshots: list[TelemetrySnapshot]) -> set[str]:
    return {snapshot.safety.rule for snapshot in snapshots if snapshot.safety is not None}


def _live(rig: Rig) -> set[tuple[str, SafetyAction, bool]]:
    return {(v.rule, v.action, v.latched) for v in rig.runtime.supervisor.live}


def _live_rules(rig: Rig) -> set[str]:
    return {verdict.rule for verdict in rig.runtime.supervisor.live}


STANDSTILL: Final[tuple[str, SafetyAction, bool]] = (
    RULE_SESSION_STANDSTILL,
    SafetyAction.RAMP_DOWN,
    True,
)
"""What stands once the arm has stopped by itself inside a session: the latched ending."""


# =========================================================================
# Rigs
# =========================================================================


async def _console_rig(seconds: float = AT_SPEED) -> Rig:
    """The shipped programme on the console's own limits, a simulated occupant, the arm turning."""
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    await rig.run(seconds)
    assert _applied(rig) > MIN_RUN, "the arm is not turning yet"
    assert _standing(rig) is None
    return rig


async def _scripted_rig(climb: float) -> Rig:
    """The shipped programme with a heart rate the test writes itself, the arm turning in HOLD.

    82 bpm through BASELINE, then 100 bpm (below the 118-138 zone, so the
    programme climbs) for ``climb`` seconds. No simulated occupant: what the
    heart does next is the test's to say, one bpm at a time.
    """
    rig = _rig(profile=REAL_PROFILE, limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(181.0)
    rig.fed_bpm = Bpm(100)
    await rig.run(climb)
    assert rig.phase() is Phase.HOLD
    assert _applied(rig) > MIN_RUN, "the arm is not turning yet"
    assert _standing(rig) is None
    return rig


async def _drift(
    rig: Rig, start: int, stop: int, *, per_minute: float = 15.0
) -> list[TelemetrySnapshot]:
    """Walk the fed heart rate from ``start`` to ``stop`` one bpm at a time, and tick through it.

    Slow on purpose: 15 bpm/min is under the 25 bpm/min of ``hr_rate`` and far
    from the 25 bpm in 30 s of ``hr_drop``, so no rule has anything to say and
    the control law is the only thing deciding.
    """
    seen: list[TelemetrySnapshot] = []
    step = 1 if stop >= start else -1
    for bpm in range(start + step, stop + step, step):
        rig.fed_bpm = Bpm(bpm)
        seen += await rig.run(60.0 / per_minute)
    return seen


async def _until_zero(rig: Rig, *, feed: bool = True, limit: float = 120.0) -> None:
    """Tick until the setpoint is zero. Fails if it never gets there within ``limit`` seconds."""
    for _ in range(round(limit / 0.2)):
        if _applied(rig) == 0:
            return
        await rig.step(feed=feed)
    assert _applied(rig) == 0, f"the setpoint never reached zero: {_applied(rig)}"


async def _until_standstill(rig: Rig, *, feed: bool = True, limit: float = 120.0) -> None:
    """Tick until the setpoint is zero, and ONE tick more: the one the verdict lands on.

    The tick that writes the zero states the fact; the supervisor judges it on
    the next one. Nothing can move in between: the setpoint is zero, and the
    tick that could raise it is the tick the verdict decides.
    """
    await _until_zero(rig, feed=feed, limit=limit)
    await rig.step(feed=feed)


async def _stopped_by_a_lost_heart_rate() -> Rig:
    """The electrode scenario up to the standstill: no heart rate, the REDUCE has reached zero."""
    rig = await _console_rig()
    await _until_standstill(rig, feed=False)
    return rig


async def _manual(occupancy: Occupancy, target: int = 200) -> Rig:
    """A manual session at ``target`` motor rpm, a heart rate on the wire either way."""
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(80)
    rig.feed(Bpm(80))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(occupancy, OPERATOR, MANUAL_CEILING))
    wanted = motor_to_output_rpm(MotorRpm(target), GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MotorRpm(target))
    await rig.run(40.0)
    assert _applied(rig) == target
    return rig


async def _occupied_manual(target: int = 200) -> Rig:
    """A manual session with a person declared on board, at ``target`` motor rpm."""
    return await _manual(Occupancy.OCCUPIED, target)


def _low_current_warning(profile: TrainingProfile) -> SafetyLimits:
    """The profile's limits with the current WARNING within the fake drive's reach.

    The fake drive draws 0.5 A at standstill and 0.82 A at the rig's ceiling, so
    the machine's 2.4 A warning can never be reached on it. Lowered here (0.7 A,
    released at 0.55 A) the real rule fires at speed and necessarily clears at
    standstill, which is what a binding arm does: the current falls with the
    speed. No threshold of the machine is changed.
    """
    return replace(
        _safety_for(profile),
        current_warn_a=Amperes(0.7),
        current_release_a=Amperes(0.15),
    )


async def _until_finished(rig: Rig, profile: TrainingProfile = REAL_PROFILE) -> None:
    """Run the monitored recovery out: the session is over, the console back to REPOS."""
    await rig.run(float(profile.recovery_s) + 2.0)
    assert rig.phase() is Phase.DONE
    assert rig.state() is RuntimeState.FINISHED


# =========================================================================
# THE DECISION: fails on develop, passes here
# =========================================================================


async def test_an_arm_stopped_by_a_lost_heart_rate_does_not_restart_when_it_returns() -> None:
    """THE ticket's scenario, on the machine's own numbers: the detached electrode.

    The heart rate is lost for 50 s and then comes back, and nobody clicks
    anything. The arm is held (FREEZE at 10 s), lowered (REDUCE at 30 s) and
    reaches standstill. From there it must not move again, for as long as
    anybody cares to wait: on ``develop`` the first non-zero setpoint came 40 s
    after the heart rate returned, and it was back to 69 motor rpm 5 s later.
    """
    rig = await _console_rig()
    running = _applied(rig)

    held = await rig.run(12.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert _setpoints(held)[-1] == running, "FREEZE did not hold the speed"

    await rig.run(20.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert 0 < _applied(rig) < running, "REDUCE did not lower the speed"

    await rig.run(18.0, feed=False)
    assert _applied(rig) == 0, "the REDUCE never reached standstill"
    assert abs(rig.drive.shaft_rpm) < 1.0

    # 50 s after it was lost, the heart rate is back. Nobody clicks.
    frames = len(rig.drive.writes)
    back = await rig.run(180.0)
    assert not _moved(back), f"the arm restarted by itself (session time, rpm): {_moved(back)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}, "a non-zero reference was written"
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commanded_rpm == 0
    assert not rig.drive.is_enabled(), "the output stage was left energised at standstill"
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert {snapshot.mode for snapshot in back} <= {RunMode.ARRET, RunMode.REPOS}
    assert all(snapshot.live_bpm is not None for snapshot in back[-25:]), "no heart rate came back"


async def test_the_standstill_is_a_latched_ending_that_names_what_stopped_the_arm() -> None:
    """What the machine is, and says, on the tick the zero lands and on the one after."""
    rig = await _console_rig()
    await _until_zero(rig, feed=False)

    # The tick that wrote the zero: the warning still stands, nothing has ended
    # yet. The shaft is still coming down from the minimum running speed and
    # the run command stays: word 6 on a turning shaft is CiA402 transition 8
    # (a freewheel).
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert rig.drive.shaft_rpm >= 1.0
    assert rig.drive.is_enabled()

    # The next tick: the supervisor's own verdict, latched, the same demand as the 60 s end.
    await rig.step(feed=False)
    reached = rig.snapshots[-1]
    assert is_rule_id(RULE_SESSION_STANDSTILL)
    assert RULE_SESSION_STANDSTILL in ALL_RULES
    assert _demand(rig) == STANDSTILL
    assert _floor(rig) == STANDSTILL, "the status page and the start gate would not see it"
    shown = reached.safety
    assert shown is not None
    assert (shown.rule, shown.action, shown.latched) == STANDSTILL
    assert BY_A_WARNING.format(RULE_HR_STALE) in shown.detail
    assert "a stopped arm never restarts by itself" in shown.detail
    assert SELF_CLEARING not in shown.detail

    # The ending: recorded once, on that tick, with that verdict as its cause.
    ending = _ending(rig)
    assert ending is not None
    assert ending.reason is EndReason.SAFETY_VERDICT
    assert ending.action is SafetyAction.RAMP_DOWN
    assert ending.detail.startswith(f"{RULE_SESSION_STANDSTILL}: ")
    assert ending.at == reached.at
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert rig.state() is RuntimeState.ENDING
    assert reached.mode is RunMode.ARRET
    assert reached.phase is Phase.COOLDOWN

    # The run command goes once the shaft is shown stopped, by the ordinary
    # two words, and not before.
    await rig.run(2.0, feed=False)
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commands[-2:] == [ControlWord.SWITCH_ON, ControlWord.SHUTDOWN]
    assert not rig.drive.is_enabled()
    assert not rig.runtime.output_enabled

    # RECOVERY is kept, with the heart-rate rules still live, then DONE.
    assert rig.phase() is Phase.RECOVERY
    assert RULE_HR_STALE in _live_rules(rig)
    assert _ending(rig) == ending, "a later tick rewrote the ending"
    rig.fed_bpm = Bpm(82)
    await _until_finished(rig)
    assert _mode(rig) is RunMode.REPOS
    assert _demand(rig) == STANDSTILL, "the latch did not outlive the session"
    assert RULE_SESSION_STANDSTILL not in _live_rules(rig), "the rule still fires on a session over"
    assert _applied(rig) == 0


async def test_no_start_is_accepted_until_the_standstill_is_acknowledged_by_name() -> None:
    """A programme, a manual session: nothing arms until a named operator has acknowledged."""
    rig = await _stopped_by_a_lost_heart_rate()
    rig.fed_bpm = Bpm(82)
    await rig.run(5.0)

    # Still ENDING: the latch is what refuses, not merely "busy".
    busy = await rig.start()
    assert isinstance(busy, Err)
    assert isinstance(busy.error, SafetyStanding)
    assert busy.error.verdict.rule == RULE_SESSION_STANDSTILL
    assert RULE_SESSION_STANDSTILL in describe_start_refusal(busy.error)

    await _until_finished(rig)
    assert _mode(rig) is RunMode.REPOS
    programme = await rig.start()
    assert isinstance(programme, Err)
    assert isinstance(programme.error, SafetyStanding)
    manual = await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING)
    assert isinstance(manual, Err)
    assert isinstance(manual.error, SafetyStanding)
    assert manual.error.verdict.rule == RULE_SESSION_STANDSTILL
    assert max(rig.drive.writes[-50:]) == 0

    unnamed = rig.runtime.acknowledge("   ")
    assert isinstance(unnamed, Err)
    assert isinstance(unnamed.error, Unattributed)
    assert _demand(rig) == STANDSTILL

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.operator == OPERATOR
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL,)
    assert _standing(rig) is None
    await rig.run(3.0)
    assert _standing(rig) is None, "the session is over: the acknowledgement must hold"

    # A NEW session, from scratch: BASELINE at standstill, as any start. What
    # stopped the previous one is forgotten with it.
    assert is_ok(await rig.start())
    assert rig.state() is RuntimeState.RUNNING
    assert rig.phase() is Phase.BASELINE
    assert _end(rig) is None
    assert _ending(rig) is None
    fresh = await rig.run(10.0)
    assert set(_setpoints(fresh)) == {0}
    assert {snapshot.mode for snapshot in fresh} == {RunMode.SEANCE}
    assert _rules_shown(fresh) == set(), "the new session inherited the old standstill"


async def test_an_acknowledgement_before_the_session_is_over_is_taken_back() -> None:
    """Acknowledged on the tick of the latch: it comes back, as the 60 s latch does.

    Also on ``91b0e90``, where this ending was cleared for good on the very
    tick it was raised. The latched end ``hr_stale`` reaches at 60 s,
    acknowledged while the heart rate is still missing, is raised again on the
    next tick: this one behaves the same for as long as the session it ended
    is on its way out. An acknowledgement resumes nothing either way, and it
    holds once the session is over.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert rig.state() is RuntimeState.ENDING
    early = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(early, Ok)
    assert early.value.cleared == (RULE_SESSION_STANDSTILL,)
    assert _floor(rig) is None

    blind = await rig.run(4.0, feed=False)
    assert _floor(rig) == STANDSTILL, "the latch did not come back"
    assert _rules_shown(blind) == {RULE_SESSION_STANDSTILL}
    assert set(_setpoints(blind)) == {0}
    assert _ending(rig) == ending
    assert rig.state() is RuntimeState.ENDING

    rig.fed_bpm = Bpm(82)
    back = await rig.run(120.0)
    assert set(_setpoints(back)) == {0}, "the acknowledgement resumed the session"
    assert {snapshot.mode for snapshot in back} == {RunMode.ARRET}
    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)

    await _until_finished(rig)
    late = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(late, Ok)
    assert late.value.cleared == (RULE_SESSION_STANDSTILL,)
    after = await rig.run(5.0)
    assert _standing(rig) is None
    assert _rules_shown(after) == set()
    assert is_ok(await rig.start())


async def test_a_manual_session_with_a_person_on_board_ends_at_the_standstill_too() -> None:
    """MANUEL, occupied: on ``develop`` the arm was back at its 200 rpm target 15 s later."""
    rig = await _occupied_manual(200)
    await rig.run(12.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert _applied(rig) == 200
    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _mode(rig) is RunMode.ARRET
    assert rig.runtime.manual_target == 0, "after ANY stop the target is zero"

    target = motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)
    refused = rig.runtime.set_manual_target(target)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, ManualEnding)

    back = await rig.run(float(MIN_RECOVERY_S) + 30.0)
    assert set(_setpoints(back)) == {0}, "the arm went back to the operator's target"
    assert all(snapshot.manual is not None for snapshot in back)
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    again = await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING)
    assert isinstance(again, Err)
    assert isinstance(again.error, SafetyStanding)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING))
    assert rig.runtime.manual_target == 0


async def test_an_empty_capsule_session_stopped_by_a_warning_ends_there_too() -> None:
    """MANUEL, BENCH: covered since the decision of 2026-10-06. Also on ``91b0e90``.

    The heart-rate rules are off with nobody on board, so the one warning that
    can walk a bench session to zero is ``current_high``: something is binding.
    The current falls with the speed, so the warning always lifts at
    standstill, and on ``develop`` and on ``91b0e90`` the arm then climbed back
    towards the operator's target with nobody clicking: the very warning that
    invites somebody to walk up and look. The person at risk is that one, not
    the one the capsule does not hold.
    """
    rig = _rig(motion=DEFAULT_MOTION_LIMITS, safety=_low_current_warning(_profile()))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    wanted = motor_to_output_rpm(MANUAL_CEILING, GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MANUAL_CEILING)
    await rig.run(21.0, feed=False)
    assert _demand(rig) == (RULE_CURRENT_HIGH, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    assert rig.runtime.manual_target == MANUAL_CEILING, "a REDUCE is not a stop while the arm turns"

    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_A_WARNING.format(RULE_CURRENT_HIGH) in reached.detail
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _mode(rig) is RunMode.ARRET
    assert rig.runtime.manual_target == 0

    after = await rig.run(float(MIN_RECOVERY_S) + 30.0, feed=False)
    assert RULE_CURRENT_HIGH not in _live_rules(rig), "the current warning never lifted"
    assert not _moved(after), f"the bench arm restarted by itself: {_moved(after)[:3]}"
    assert rig.state() is RuntimeState.FINISHED
    again = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING)
    assert isinstance(again, Err)
    assert isinstance(again.error, SafetyStanding)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))


async def test_a_target_of_zero_typed_during_the_descent_does_not_exempt_the_standstill() -> None:
    """MANUEL, occupied: the operator asks for zero while the warning is lowering the speed.

    Under REDUCE the verdict's own step IS the setpoint, whatever the target,
    so the standstill is still the warning's and the session ends there. The
    cautious reading of the decision, pinned so that it is a choice and not an
    accident: it costs one acknowledgement, and it keeps one rule.
    """
    rig = await _occupied_manual(200)
    await rig.run(32.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    assert rig.runtime.set_manual_target(OutputRpm(0.0)) == Ok(MotorRpm(0))
    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL
    assert _end(rig) is EndReason.SAFETY_VERDICT


# --- the heart-rate regulation takes the arm to zero -----------------------


async def test_the_last_step_taken_by_the_regulation_after_a_warning_ends_the_session() -> None:
    """The reviewer's sequence (B1). Also on ``91b0e90``: there the arm restarted at 269 s.

    Shipped profile, 204 motor rpm in HOLD, the heart rate at 128 in its
    118-138 zone. The electrode comes off: FREEZE, then REDUCE down to the
    minimum running speed. It is refitted THERE, and the rate reads 140, two
    above the zone. The warning lifts, nothing is on the screen, and five
    seconds later the control law takes the last step to zero. For whoever is
    watching the arm this is the ticket's own sequence: a warning, the arm
    slowing, the arm stopped. On ``91b0e90`` the rule looked at the REDUCE arm
    only, so nothing ended: when the rate had drifted back under the zone the
    setpoint was 55 again, and 203 two minutes later.
    """
    rig = await _scripted_rig(climb=330.0)
    await _drift(rig, 100, 128)
    await rig.run(30.0)
    top = _applied(rig)
    assert top > 150, f"the arm is not at speed: {top}"
    assert _standing(rig) is None

    lowered: list[TelemetrySnapshot] = []
    for _ in range(round(80.0 / 0.2)):
        if _applied(rig) == MIN_RUN:
            break
        lowered.append(await rig.step(feed=False))
    assert _applied(rig) == MIN_RUN
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _rules_shown(lowered) == {RULE_HR_STALE}

    # Refitted at the minimum running speed; the rate is just above the zone.
    rig.fed_bpm = Bpm(140)
    await rig.step()
    assert _standing(rig) is None, "the warning did not lift"
    await _until_zero(rig, limit=30.0)
    assert _standing(rig) is None, "a verdict wrote this zero: not the sequence under test"
    assert _end(rig) is None
    await rig.step()
    assert _demand(rig) == STANDSTILL
    assert _floor(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_THE_REGULATION in reached.detail
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert rig.state() is RuntimeState.ENDING
    assert _mode(rig) is RunMode.ARRET

    # The rate drifts back under the zone. Nobody clicks, for ten minutes.
    frames = len(rig.drive.writes)
    after = await rig.run(60.0)
    after += await _drift(rig, 140, 105, per_minute=10.0)
    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    after += await rig.run(330.0)
    assert not _moved(after), f"the arm restarted by itself (time, rpm): {_moved(after)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}, "a non-zero reference was written"
    assert not rig.drive.is_enabled()
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.state() is RuntimeState.FINISHED

    # No start before a named acknowledgement; a new session after it.
    still = await rig.start()
    assert isinstance(still, Err)
    assert isinstance(still.error, SafetyStanding)
    assert still.error.verdict.rule == RULE_SESSION_STANDSTILL
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL,)
    assert is_ok(await rig.start())
    assert rig.phase() is Phase.BASELINE


async def test_a_heart_rate_drifting_above_the_zone_until_standstill_ends_the_session() -> None:
    """The regulation alone, no warning at any time. Also on ``91b0e90``.

    The rate drifts from 100 to 145 at 15 bpm/min and stays there: above the
    zone (138), under the hard maximum (148), too slow for ``hr_rate``. The
    control law walks the setpoint down and writes zero about two minutes
    after the zone was left. On ``develop`` and on ``91b0e90`` the mode stayed
    SEANCE with no verdict, the output stage energised, and when the rate came
    back under the zone 150 s later the arm left again: 55 motor rpm, then 133
    a minute after. Decision of 2026-10-06: that standstill ends the session
    like any other, and the regulation no longer rests at zero and resumes.
    """
    rig = await _scripted_rig(climb=300.0)
    quiet = await _drift(rig, 100, 145)
    for _ in range(round(240.0 / 0.2)):
        if _applied(rig) == 0:
            break
        quiet.append(await rig.step())
    assert _applied(rig) == 0, "the regulation never reached standstill"
    assert _rules_shown(quiet) == set(), "a rule spoke: this is not the regulation alone"
    assert min(_setpoints(quiet[:-1])) >= MIN_RUN
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING

    await rig.step()
    assert _demand(rig) == STANDSTILL
    assert _floor(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_THE_REGULATION in reached.detail
    assert SELF_CLEARING not in reached.detail
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _mode(rig) is RunMode.ARRET

    frames = len(rig.drive.writes)
    after = await rig.run(150.0)
    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    after += await _drift(rig, 145, 105)
    after += await rig.run(120.0)
    assert not _moved(after), f"the arm restarted by itself (time, rpm): {_moved(after)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert not rig.drive.is_enabled(), "the output stage was left energised at standstill"
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert _rules_shown(after) == {RULE_SESSION_STANDSTILL}

    await _until_finished(rig)
    again = await rig.start()
    assert isinstance(again, Err)
    assert isinstance(again.error, SafetyStanding)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert is_ok(await rig.start())


# --- every other warning that lowers the speed without latching ----------


async def test_a_heart_rate_rising_too_fast_that_reaches_standstill_ends_the_session() -> None:
    """``hr_rate``: REDUCE while the rate rises faster than 25 bpm/min, released below 15.

    The rate climbs 30 bpm/min for 50 s and then settles. On ``develop`` the
    warning lifted about 20 s after the standstill and the arm restarted 50 s
    later, into a heart that had just been judged to be rising too fast.
    """
    rig = _rig(profile=REAL_PROFILE, limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(80)
    assert is_ok(await rig.start())
    await rig.run(AT_SPEED)
    assert _applied(rig) > MIN_RUN
    for second in range(50):
        rig.fed_bpm = Bpm(80 + (second + 1) // 2)
        await rig.run(1.0)
    assert _demand(rig) == (RULE_HR_RATE, SafetyAction.REDUCE, False)
    assert _applied(rig) > 0
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_A_WARNING.format(RULE_HR_RATE) in reached.detail

    settled = await rig.run(150.0)
    assert RULE_HR_RATE not in _live_rules(rig), "the warning never lifted: nothing was proven"
    assert set(_setpoints(settled)) == {0}, "the arm restarted when the rise settled"
    assert _end(rig) is EndReason.SAFETY_VERDICT


async def test_a_heart_that_ignores_the_load_and_reaches_standstill_ends_the_session() -> None:
    """``hr_unresponsive``: REDUCE when the load rose and the heart rate did not move.

    A flat 100 bpm under a programme that climbs to 0.33 g. The warning walks
    the arm to zero and, with the load gone, eventually lifts by itself. On
    ``develop`` the arm then climbed straight back to its ceiling, under the
    very load the heart had just been shown not to answer.
    """
    profile = _profile(
        total_duration_s=Seconds(1200.0),
        baseline_s=Seconds(60.0),
        hold_min_s=Seconds(900.0),
        max_rpm=MotorRpm(700),
    )
    rig = _rig(profile=profile)
    rig.fed_bpm = Bpm(100)
    assert is_ok(await rig.start())
    for _ in range(round(300.0 / 0.2)):
        await rig.step()
        if _standing(rig) is not None:
            break
    assert _demand(rig) == (RULE_HR_UNRESPONSIVE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_A_WARNING.format(RULE_HR_UNRESPONSIVE) in reached.detail

    after = await rig.run(320.0)
    assert RULE_HR_UNRESPONSIVE not in _live_rules(rig)
    assert set(_setpoints(after)) == {0}, "the arm climbed back under the load"
    assert rig.state() is RuntimeState.FINISHED


async def test_a_motor_current_warning_that_reaches_standstill_ends_the_session() -> None:
    """``current_high`` at its warning level: the one that ALWAYS clears at standstill.

    A current above the warning level means something is binding. The current
    falls with the speed, so at standstill the warning lifts by construction:
    on ``develop`` the arm was back at 83 rpm two seconds after it stopped, and
    cycled like that for as long as the session lasted.
    """
    profile = LONG_HOLD
    rig = await _running_rig(profile=profile, safety=_low_current_warning(profile))
    for _ in range(round(60.0 / 0.2)):
        await rig.step()
        if _standing(rig) is not None:
            break
    assert _demand(rig) == (RULE_CURRENT_HIGH, SafetyAction.REDUCE, False)
    assert rig.phase() is Phase.HOLD
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_A_WARNING.format(RULE_CURRENT_HIGH) in reached.detail

    after = await rig.run(30.0)
    assert RULE_CURRENT_HIGH not in _live_rules(rig), "the current warning never lifted"
    assert set(_setpoints(after)) == {0}, "the arm restarted as soon as the current fell"


async def test_a_latched_reduce_ends_at_standstill_so_an_acknowledgement_restarts_nothing() -> None:
    """A REDUCE that latches (a trip from a thread) is covered by the same test.

    No caller emits one today. If one ever does: on ``develop`` it held the
    arm at zero until somebody acknowledged it, and the acknowledgement then
    restarted the arm inside the same session, which is the resumption on an
    operator's gesture the decision of 2026-10-05 rejected.
    """
    rig = await _running_rig(profile=LONG_HOLD)
    # Back to the resting rate the rig measured in BASELINE: still far below the
    # zone, so the control law wants speed, and no fall for hr_drop to judge
    # once the load is gone.
    rig.fed_bpm = Bpm(82)
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    await rig.step()
    assert _floor(rig) == ("rig_reduce", SafetyAction.REDUCE, True)
    await _until_standstill(rig)
    assert _demand(rig) == STANDSTILL
    reached = _standing(rig)
    assert reached is not None
    assert BY_A_WARNING.format("rig_reduce") in reached.detail

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    after = await rig.run(20.0)
    assert set(_setpoints(after)) == {0}, "the acknowledgement restarted the arm"
    assert _end(rig) is EndReason.SAFETY_VERDICT
    await _until_finished(rig, LONG_HOLD)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert _standing(rig) is None


# --- the last step of the descent ----------------------------------------


async def test_a_zero_the_drive_did_not_acknowledge_ends_nothing_until_it_lands() -> None:
    """The ending is taken on an ACKNOWLEDGED zero, never on one that was only sent.

    The setpoint believed in force advances on an acknowledged write and on
    nothing else. While the last step of the descent is refused, the arm is
    still commanded at its minimum running speed: the session is not over, and
    the next tick tries again.
    """
    rig = await _running_rig(profile=LONG_HOLD, safety=_lenient_comms())
    minimum = LONG_HOLD.min_run_rpm
    rig.fed_bpm = Bpm(82)
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    for _ in range(100):
        if _applied(rig) == minimum:
            break
        await rig.step()
    assert _applied(rig) == minimum
    # Shorter than the fake drive's own ttO (2 s), so the drive does not
    # fault for want of a keepalive: the only thing failing is the write.
    rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
    rig.drive.trace.clear()
    refused = await rig.run(1.4)
    assert f"lfrd:{MotorRpm(0)}" in rig.drive.trace, "the last step was never attempted"
    assert f"lfrd:{MotorRpm(0)}:ack" not in rig.drive.trace
    assert set(_setpoints(refused)) == {int(minimum)}
    assert rig.drive.commanded_rpm == minimum
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _rules_shown(refused) == {"rig_reduce"}

    rig.drive.speed_error = None
    await rig.step()
    assert _applied(rig) == 0
    assert rig.drive.commanded_rpm == 0
    await rig.step()
    assert _demand(rig) == STANDSTILL
    assert _end(rig) is EndReason.SAFETY_VERDICT


# --- what may still happen after the standstill --------------------------


async def test_a_refused_stop_word_after_the_standstill_is_reported_and_acknowledged() -> None:
    """The reviewer's B2. Also on ``91b0e90``, where ``disable_refused`` was dropped.

    At confirmed standstill the drive refuses ``SWITCH_ON`` five times in a
    row: the runtime escalates to ``SHUTDOWN`` and latches ``disable_refused``,
    so that somebody learns the drive refused a word. On ``91b0e90`` the
    standstill ending sat in the runtime's single latch, which drops a second
    latch of equal severity: nothing was reported. Now that ending is the
    supervisor's, the runtime's latch is free, and the two are reported and
    cleared exactly as after the latched end ``hr_stale`` reaches at 60 s.
    """
    rig = await _console_rig()
    rig.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.5))
    await _until_standstill(rig, feed=False)
    assert _demand(rig) == STANDSTILL

    await rig.run(6.0, feed=False)
    assert _demand(rig) == (RULE_DISABLE_REFUSED, SafetyAction.RAMP_DOWN, True)
    assert _floor(rig) == STANDSTILL
    shown = rig.snapshots[-1].safety
    assert shown is not None
    assert shown.rule == RULE_DISABLE_REFUSED, "the operator is not told the drive refused a word"
    assert rig.drive.commands[-1] is ControlWord.SHUTDOWN
    assert not rig.drive.is_enabled()

    rig.fed_bpm = Bpm(82)
    await _until_finished(rig)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL, RULE_DISABLE_REFUSED)
    assert _standing(rig) is None
    assert _floor(rig) is None


async def test_the_rule_s_own_latched_end_at_sixty_seconds_stands_beside_the_standstill() -> None:
    """The heart rate never returns: ``hr_stale`` latches its own RAMP_DOWN at 60 s.

    Two latched verdicts of the supervisor then fire, of equal severity. The
    floor keeps the first, the ending keeps the cause it was recorded with, and
    both are listed as firing. Nothing moves at any point.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    await rig.run(25.0, feed=False)
    assert _floor(rig) == STANDSTILL
    assert _demand(rig) == STANDSTILL
    assert {STANDSTILL, (RULE_HR_STALE, SafetyAction.RAMP_DOWN, True)} <= _live(rig)
    assert _ending(rig) == ending
    assert _applied(rig) == 0

    rig.fed_bpm = Bpm(82)
    await _until_finished(rig)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL,)
    after = await rig.run(30.0)
    assert _standing(rig) is None
    assert set(_setpoints(after)) == {0}


async def test_an_emergency_stop_after_the_standstill_takes_over_and_rewrites_nothing() -> None:
    """QUICK_STOP outranks the standstill latch; the ending keeps its first cause."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    rig.runtime.request_estop("under test")
    await rig.run(2.0, feed=False)
    assert _demand(rig) == (RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP, True)
    assert _ending(rig) == ending
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _applied(rig) == 0
    still_pressed = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(still_pressed, Err)
    assert _demand(rig) == (RULE_OPERATOR_ESTOP, SafetyAction.QUICK_STOP, True)
    released = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(released, Ok)
    assert released.value.cleared == (RULE_OPERATOR_ESTOP, RULE_SESSION_STANDSTILL)


async def test_a_stop_asked_for_after_the_standstill_changes_nothing() -> None:
    """The session has already ended: an operator stop on top of it is recorded, and no more."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert ending is not None
    rig.runtime.request_stop("operator: stop button")
    after = await rig.run(3.0, feed=False)
    assert _ending(rig) == ending
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _demand(rig) == STANDSTILL
    assert rig.runtime.stop_reason == "operator: stop button"
    assert set(_setpoints(after)) == {0}


async def test_a_lost_link_after_the_standstill_goes_silent_and_is_terminal() -> None:
    """GO_SILENT after the standstill: no further frame, and nothing can be acknowledged away."""
    rig = await _stopped_by_a_lost_heart_rate()
    ending = _ending(rig)
    assert ending is not None
    rig.drive.break_comms()
    await rig.run(2.0, feed=False)
    assert rig.runtime.silent
    assert _demand(rig) == (RULE_COMMS_LOST, SafetyAction.GO_SILENT, True)
    assert _ending(rig) == ending, "the later cause rewrote the ending"
    assert _applied(rig) == 0
    refused = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, GoSilentIsTerminal)
    again = await rig.start()
    assert isinstance(again, Err)
    assert isinstance(again.error, AlreadyStarted)


# =========================================================================
# UNCHANGED ON PURPOSE: passes on develop and here
# =========================================================================


async def test_a_held_speed_resumes_by_itself_when_the_heart_rate_returns() -> None:
    """FREEZE only: 20 s without a heart rate, the speed held, then regulation carries on."""
    rig = await _console_rig(IN_HOLD)
    assert rig.phase() is Phase.HOLD
    held = _applied(rig)
    assert held < REAL_PROFILE.max_rpm, "no speed left to gain: a resume would not show"
    frozen = await rig.run(20.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert set(_setpoints(frozen)[-40:]) == {int(held)}

    resumed = await rig.run(40.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _mode(rig) is RunMode.SEANCE
    assert _applied(rig) > held, "regulation did not resume"
    assert RULE_SESSION_STANDSTILL not in _rules_shown(frozen + resumed)


async def test_a_page_closed_for_a_hundred_seconds_holds_then_regulation_resumes() -> None:
    """``attendant_absent``: FREEZE at 60 s without a presence ping, released when it returns."""
    rig = await _console_rig()
    away = await rig.run(100.0, ping=False)
    assert _demand(rig) == (RULE_ATTENDANT_ABSENT, SafetyAction.FREEZE, False)
    held = _applied(rig)
    assert set(_setpoints(away)[-190:]) == {int(held)}, "the speed moved while nobody watched"
    assert held < REAL_PROFILE.max_rpm

    back = await rig.run(45.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _applied(rig) > held, "regulation did not resume when the page reopened"
    assert RULE_SESSION_STANDSTILL not in _rules_shown(away + back)


async def test_a_speed_lowered_part_way_resumes_by_itself_when_the_heart_rate_returns() -> None:
    """REDUCE part way: 36 s without a heart rate, the arm slowed but never stopped."""
    rig = await _console_rig()
    running = _applied(rig)
    lost = await rig.run(36.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    low = _applied(rig)
    assert MIN_RUN <= low < running
    assert min(_setpoints(lost)) == low

    back = await rig.run(60.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert min(_setpoints(back)) >= MIN_RUN, "the arm stopped after all"
    assert _applied(rig) > low, "regulation did not resume"
    assert RULE_SESSION_STANDSTILL not in _rules_shown(lost + back)


async def test_a_warning_that_lifts_at_the_minimum_running_speed_resumes() -> None:
    """The drive's minimum is not zero: parked at 55 motor rpm, the arm is still turning.

    The descent stops at the minimum running speed until it has earned the
    whole jump to zero (3.7 s at 15 rpm/s). A warning that lifts there has not
    stopped the arm, so regulation resumes as from any other speed: here the
    rate comes back under the zone, and the control law wants speed.
    """
    rig = await _console_rig()
    for _ in range(round(60.0 / 0.2)):
        if _applied(rig) == MIN_RUN:
            break
        await rig.step(feed=False)
    assert _applied(rig) == MIN_RUN
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)

    back = await rig.run(60.0)
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert min(_setpoints(back)) >= MIN_RUN
    assert _applied(rig) > MIN_RUN


async def test_the_regulation_still_lowers_and_raises_the_speed_of_a_turning_arm() -> None:
    """Regulating in both directions is the control law's job, and it keeps it above zero.

    The rate drifts to 142, four above the zone, for long enough to cost the
    arm a good part of its speed, then back to 108. The setpoint goes down and
    comes back up with no verdict and no ending: the arm never stopped.
    """
    rig = await _scripted_rig(climb=330.0)
    seen = await _drift(rig, 100, 128)
    seen += await rig.run(30.0)
    top = _applied(rig)
    seen += await _drift(rig, 128, 142)
    seen += await rig.run(20.0)
    low = _applied(rig)
    assert MIN_RUN <= low < top - 20, f"the regulation did not lower the speed: {top} to {low}"
    seen += await _drift(rig, 142, 108)
    seen += await rig.run(60.0)
    assert min(_setpoints(seen)) >= MIN_RUN, "the arm stopped: not the case under test"
    assert _applied(rig) > low, "the regulation did not raise the speed again"
    assert _rules_shown(seen) == set()
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert _mode(rig) is RunMode.SEANCE


# --- a zero somebody asked for is not a standstill by itself ---------------


@pytest.mark.parametrize("occupancy", [Occupancy.BENCH, Occupancy.OCCUPIED])
async def test_a_target_of_zero_the_operator_typed_with_no_warning_ends_nothing(
    occupancy: Occupancy,
) -> None:
    """MANUEL: the operator brings the arm to zero, then asks for speed again.

    Both are the operator's own commands, so neither is "by itself": the
    session goes on at standstill, nothing latches, and the next target is
    followed like the first.
    """
    rig = await _manual(occupancy, 200)
    assert rig.runtime.set_manual_target(OutputRpm(0.0)) == Ok(MotorRpm(0))
    down = await rig.run(40.0)
    assert _applied(rig) == 0
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert _rules_shown(down) == set()
    assert _standing(rig) is None
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    assert {snapshot.mode for snapshot in down} == {RunMode.MANUEL}

    wanted = motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MotorRpm(200))
    up = await rig.run(40.0)
    assert _applied(rig) == 200
    assert _rules_shown(up) == set()
    assert _end(rig) is None


async def test_an_operator_stop_is_not_a_standstill_by_itself() -> None:
    """STOP, with nothing else going on: the operator's ending, and no acknowledgement to give."""
    rig = await _running_rig(profile=LONG_HOLD)
    rig.fed_bpm = Bpm(82)  # the rig's resting rate: no fall for hr_drop once the load is gone
    rig.runtime.request_stop("operator: stop button")
    seen = await rig.run(80.0)
    assert _applied(rig) == 0
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert _rules_shown(seen) == set()
    assert rig.state() is RuntimeState.FINISHED
    assert is_ok(await rig.start()), "an operator stop must not need an acknowledgement"


async def test_an_operator_stop_under_a_warning_keeps_its_own_ending() -> None:
    """A stop was asked for first: the standstill is the operator's, and nothing latches."""
    rig = await _running_rig(profile=LONG_HOLD)
    rig.fed_bpm = Bpm(82)  # the rig's resting rate: no fall for hr_drop once the load is gone
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.REDUCE
        await rig.step()
        assert _applied(rig) > 0
        rig.runtime.request_stop("operator: stop button")
        await _until_standstill(rig)
        # Reached under the imposed REDUCE, and nothing latched anywhere.
        assert _standing(rig) is None
        assert _floor(rig) is None
        assert _end(rig) is EndReason.OPERATOR_STOP
        imposed.action = None
        seen = await rig.run(75.0)
    assert _standing(rig) is None
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert rig.state() is RuntimeState.FINISHED
    assert RULE_SESSION_STANDSTILL not in _rules_shown(seen)
    assert is_ok(await rig.start()), "an operator stop must not need an acknowledgement"


async def test_the_programme_s_own_cooldown_and_recovery_are_not_a_standstill_by_itself() -> None:
    """The timeline brings the arm to zero at its end: that is the programme, completing."""
    rig = await _running_rig()
    seen = await rig.run(112.0)
    assert Phase.COOLDOWN in {snapshot.phase for snapshot in seen}
    assert _applied(rig) == 0
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert _ending(rig) is None
    assert rig.state() is RuntimeState.FINISHED
    assert RULE_SESSION_STANDSTILL not in _rules_shown(seen)
    assert _floor(rig) is None


async def test_a_warning_during_the_programme_s_own_cooldown_lets_it_complete() -> None:
    """COOLDOWN on the timeline: the programme was going to zero anyway, and nothing latches."""
    rig = await _running_rig()
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        for _ in range(round(60.0 / 0.2)):
            if rig.phase() is Phase.COOLDOWN:
                break
            await rig.step()
        assert rig.phase() is Phase.COOLDOWN
        assert _applied(rig) > 0, "the cooldown was already over"
        imposed.action = SafetyAction.REDUCE
        await _until_standstill(rig)
        assert rig.phase() is Phase.COOLDOWN
        assert _end(rig) is None
        imposed.action = None
        seen = await rig.run(75.0)
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert _ending(rig) is None
    assert rig.state() is RuntimeState.FINISHED
    assert _standing(rig) is None
    assert set(_setpoints(seen)) == {0}
    assert RULE_SESSION_STANDSTILL not in _rules_shown(seen)


# --- overtaken before standstill: the other verdict owns the ending -------


async def test_a_critical_heart_rate_during_the_descent_ends_it_as_before() -> None:
    """QUICK_STOP before the REDUCE reaches zero: the zero is the emergency's, not the warning's."""
    rig = await _console_rig()
    await rig.run(33.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _applied(rig) > MIN_RUN
    rig.fed_bpm = REAL_PROFILE.critical_bpm
    after = await rig.run(20.0)
    assert _demand(rig) == (RULE_HR_CRITICAL, SafetyAction.QUICK_STOP, True)
    assert _applied(rig) == 0
    ending = _ending(rig)
    assert ending is not None
    assert ending.detail.startswith(f"{RULE_HR_CRITICAL}: ")
    assert RULE_SESSION_STANDSTILL not in _rules_shown(after)
    assert RULE_SESSION_STANDSTILL not in _live_rules(rig)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_HR_CRITICAL,)


async def test_a_descent_too_long_to_reach_zero_ends_on_the_rule_s_own_latch() -> None:
    """From 700 motor rpm the REDUCE cannot reach zero before ``hr_stale`` latches at 60 s."""
    profile = _profile(
        total_duration_s=Seconds(900.0),
        hold_min_s=Seconds(600.0),
        max_rpm=MotorRpm(700),
    )
    rig = _rig(profile=profile, limits=RUNTIME_LIMITS)
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.fed_bpm = Bpm(65)
    await rig.run(200.0)
    assert _applied(rig) == 700
    seen = await rig.run(61.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.RAMP_DOWN, True)
    assert _applied(rig) > 0, "the descent was not supposed to be over yet"
    seen += await rig.run(40.0, feed=False)
    assert _applied(rig) == 0
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.RAMP_DOWN, True)
    assert RULE_SESSION_STANDSTILL not in _rules_shown(seen)
    assert RULE_SESSION_STANDSTILL not in _live_rules(rig)


# --- left as it was, by decision ------------------------------------------


async def test_a_warning_during_baseline_before_the_arm_has_moved_ends_nothing() -> None:
    """Decision of 2026-10-06: left unchanged, because nothing has moved yet.

    No heart rate for the first 45 s of BASELINE (electrodes still being
    fitted): FREEZE at 10 s, REDUCE at 30 s, at a setpoint that has been zero
    since the start. The warning then lifts, the programme carries on, and the
    arm makes its FIRST motion at the start of WARMUP with nobody clicking:
    the programme's normal start, as in a session with no warning at all.
    """
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    blind = await rig.run(45.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert rig.phase() is Phase.BASELINE
    assert set(_setpoints(blind)) == {0}
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING

    later = await rig.run(255.0)
    assert _standing(rig) is None
    assert _floor(rig) is None
    assert _end(rig) is None
    assert rig.phase() is Phase.WARMUP
    assert _applied(rig) >= MIN_RUN, "the programme never made its first motion"
    assert _rules_shown(blind + later) == {RULE_HR_STALE}


async def test_a_target_typed_while_a_warning_holds_a_stopped_arm_is_followed_later() -> None:
    """OPEN QUESTION for the product owner: an operator's command that had to wait.

    MANUEL, occupied. The operator brings the arm to zero (their own command,
    so the session goes on), the heart rate is lost, and the warning holds the
    setpoint where it is: at zero. The operator types 200 rpm during the hold.
    Nothing moves while the warning stands; when the heart rate is back the
    setpoint climbs to that target, and nobody clicks at that moment. The
    motion was asked for by the operator, so it is not "by itself" in the
    sense of the decision, and it is unchanged by ANH-176: this test passes on
    ``develop`` too.
    """
    rig = await _occupied_manual(200)
    assert rig.runtime.set_manual_target(OutputRpm(0.0)) == Ok(MotorRpm(0))
    await rig.run(40.0)
    assert _applied(rig) == 0
    assert _standing(rig) is None

    await rig.run(12.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    wanted = motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)
    assert rig.runtime.set_manual_target(wanted) == Ok(MotorRpm(200))
    held = await rig.run(6.0, feed=False)
    assert set(_setpoints(held)) == {0}, "the hold did not hold"
    assert _rules_shown(held) == {RULE_HR_STALE}

    back = await rig.run(60.0)
    assert _standing(rig) is None
    assert _applied(rig) == 200, "the waiting target was not followed: update this test"
    assert _end(rig) is None
    assert RULE_SESSION_STANDSTILL not in _rules_shown(held + back)


# =========================================================================
# WHAT THE SCREEN IS TOLD while the speed can still resume by itself
# =========================================================================


async def test_the_screen_is_told_for_as_long_as_the_speed_can_resume_by_itself() -> None:
    """EX-3, from what the page already renders: the verdict's sentence, and ``latched``.

    The console shows a verdict's ``detail`` verbatim on its Seance and
    Securite pages, and ``verrouille: non`` beside it. For every tick a speed
    is held or lowered by a warning, that sentence says the warning lifts by
    itself and that the speed then follows the programme again; once the
    standstill is reached it is replaced by the latched ending, which says the
    opposite. The three fields a page needs to raise its own banner
    (``mode``, ``safety.latched``, ``safety_rank``) are pinned on the wire row.
    """
    rig = await _console_rig()
    lost = await rig.run(36.0, feed=False)
    blind = [snapshot for snapshot in lost if snapshot.safety is not None]
    assert len(blind) >= round(24.0 / 0.2), "the warning stood for less time than expected"
    assert min(_setpoints(blind)) >= MIN_RUN, "the arm stopped: the sentence would be untrue"
    warned = [SnapshotRow.of(snapshot) for snapshot in blind]
    for row in warned:
        assert row.mode == "seance"
        assert row.safety is not None
        assert row.safety.rule == RULE_HR_STALE
        assert row.safety.latched is False
        assert row.safety.detail.endswith(SELF_CLEARING)
    # Held first, then lowered: the rank the page colours its pill with.
    ranks = [row.safety_rank for row in warned]
    assert ranks == sorted(ranks)
    assert set(ranks) == {int(SafetyAction.FREEZE), int(SafetyAction.REDUCE)}
    assert {row.safety_action for row in warned} == {"freeze", "reduce"}

    await _until_standstill(rig, feed=False)
    stopped = SnapshotRow.of(rig.snapshots[-1])
    assert stopped.mode == "arret"
    assert stopped.safety is not None
    assert stopped.safety.rule == RULE_SESSION_STANDSTILL
    assert stopped.safety.latched is True
    assert stopped.safety_rank == int(SafetyAction.RAMP_DOWN)
    assert SELF_CLEARING not in stopped.safety.detail


def _may_speed_up_alone(row: SnapshotRow) -> bool:
    """The banner's predicate, from the fields the page already receives and nothing else.

    A warning that is not latched, in a session that is running (not ending,
    not at rest) and in a phase that can still be asked for speed. ``mode``
    alone is not enough: a programme stays ``seance`` through its own cooldown
    and recovery, where nothing rises any more. In ``manuel`` a page would add
    "and the target is above the setpoint".
    """
    return (
        row.safety is not None
        and not row.safety.latched
        and row.mode in ("seance", "manuel")
        and row.phase in ("baseline", "warmup", "hold")
    )


async def test_a_page_can_tell_from_the_wire_row_alone_when_the_speed_may_rise_again() -> None:
    """EX-3 remains open: this is what the banner it still needs can be built on.

    The sentence is prose and nothing may parse it. So the same fact is pinned
    on the wire row: across the electrode sequence (held, lowered, stopped,
    ended, heart rate back) and across a programme that loses its heart rate
    during its own recovery, the predicate is true on exactly the ticks whose
    verdict carries the sentence.
    """
    stopped = await _console_rig()
    seen = await stopped.run(60.0, feed=False)
    stopped.fed_bpm = Bpm(82)
    seen += await stopped.run(30.0)

    ending = await _running_rig()
    ending.fed_bpm = Bpm(82)  # the rig's resting rate: no fall for hr_drop once the load is gone
    for _ in range(round(120.0 / 0.2)):
        if ending.phase() is Phase.RECOVERY:
            break
        seen.append(await ending.step())
    assert ending.phase() is Phase.RECOVERY
    lost = await ending.run(20.0, feed=False)
    assert _demand(ending) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert {snapshot.mode for snapshot in lost} == {RunMode.SEANCE}
    seen += lost

    told = 0
    quiet_warnings = 0
    for row in (SnapshotRow.of(snapshot) for snapshot in seen):
        says_so = row.safety is not None and row.safety.detail.endswith(SELF_CLEARING)
        assert _may_speed_up_alone(row) is says_so, (row.mode, row.phase, row.safety)
        told += says_so
        quiet_warnings += row.safety is not None and not row.safety.latched and not says_so
    assert told >= round(30.0 / 0.2), "the sentence was hardly ever shown: nothing was compared"
    assert quiet_warnings >= round(5.0 / 0.2), "no unlatched warning on a session that cannot rise"


async def test_the_sentence_is_gone_once_the_session_can_no_longer_speed_up() -> None:
    """Only where it is true. Also on ``91b0e90``, where it stayed on an ended session.

    After the standstill the heart rate is still missing, so ``hr_stale`` is
    still firing, unlatched, underneath the latched ending. Nothing follows
    the programme upwards from an ended session: the sentence is not on it,
    in the status list or anywhere else.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    await rig.run(3.0, feed=False)
    assert rig.phase() in (Phase.COOLDOWN, Phase.RECOVERY)
    still = [verdict for verdict in rig.runtime.supervisor.live if verdict.rule == RULE_HR_STALE]
    assert len(still) == 1
    assert still[0].latched is False
    assert SELF_CLEARING not in still[0].detail
    assert all(SELF_CLEARING not in verdict.detail for verdict in rig.runtime.supervisor.live)


async def test_a_refused_start_quotes_the_warning_without_the_sentence() -> None:
    """A START typed while a warning holds the speed: refused, and told why, in one clause.

    Also on ``91b0e90``, where the refusal read "verdict hr_stale a acquitter
    (... NOT LATCHED: ...)". The sentence is for the live screen of a running
    session; inside "start refused" it only blurs the reason.
    """
    rig = await _console_rig(IN_HOLD)
    await rig.run(12.0, feed=False)
    standing = _standing(rig)
    assert standing is not None
    assert (standing.rule, standing.latched) == (RULE_HR_STALE, False)
    assert standing.detail.endswith(SELF_CLEARING)

    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    line = describe_start_refusal(refused.error)
    assert line.startswith(f"demarrage refuse : verdict {RULE_HR_STALE} a acquitter (")
    assert "NOT LATCHED" not in line
    assert SELF_CLEARING not in line
    assert standing.detail.removesuffix(SELF_CLEARING) in line


async def test_the_screen_is_told_before_the_first_motion_too() -> None:
    """EX-3 says "including with the arm stopped": the one case that is left, the baseline.

    A warning during BASELINE ends nothing, so the programme still makes its
    first motion by itself when the baseline is over. For as long as that
    warning stands, at a setpoint of zero, the sentence on the screen says so.
    """
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    blind = [s for s in await rig.run(45.0, feed=False) if s.safety is not None]
    assert len(blind) >= round(33.0 / 0.2)
    assert set(_setpoints(blind)) == {0}
    for row in (SnapshotRow.of(snapshot) for snapshot in blind):
        assert row.mode == "seance"
        assert row.safety is not None
        assert row.safety.latched is False
        assert row.safety.detail.endswith(SELF_CLEARING)
    assert {snapshot.safety_action for snapshot in blind} == {
        SafetyAction.FREEZE,
        SafetyAction.REDUCE,
    }
