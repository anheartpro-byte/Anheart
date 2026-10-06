"""No manual target waits behind a verdict over a stopped arm (ANH-178).

A manual target is a destination. Under a verdict it is not followed upwards,
so one entered over a setpoint of zero used to WAIT, and the arm left by
itself when the verdict went. Measured by the independent review of ANH-176,
on ``develop``:

* the operator brings the arm to zero (the session goes on, nothing stands);
  the heart rate is lost (FREEZE ``hr_stale``); 200 rpm is typed and accepted;
  thirty seconds with nothing moving; the heart rate returns, and the first
  non-zero setpoint is written 0.2 s later with nobody clicking;
* the same in a manual session that had not moved yet, first motion 3.2 s
  after the heart rate returns;
* with a LATCHED verdict (a recovered ``loop_stall``) over an arm the operator
  stopped, the target waited for the acknowledgement and the arm moved 0.4 s
  after it: acknowledging was what started motion.

The rule, in two halves that close on each other:

* **refused**: while any verdict stands, latched or not, over a setpoint of
  zero, a non-zero target is refused, and the refusal names the verdict;
* **taken back**: a target already entered goes back to zero on the tick a
  verdict holds the arm at standstill, and the console is told once.

So a stopped arm is given a speed by one thing only: a target entered while
nothing stands. Nothing changes for an arm that is turning.

Three groups, and the split matters when reading a failure:

* **the rule**: these fail on ``develop`` (the target is accepted, or kept,
  and the arm then moves) and pass here;
* **unchanged on purpose**: a turning arm, a target of zero, a start. These
  pass on both;
* **left as it is**: the heart-rate gate that holds a rise with no verdict.

Every test runs on the fake drive and the manual clock of
``tests/test_runtime.py``, with the shipped motion limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.local_panel import RUNTIME_LIMITS
from src.result import Err, Ok, Result, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import (
    EndReason,
    HeldAtStandstill,
    ManualEnding,
    ManualTargetRefusal,
    RuntimeState,
    SafetyStanding,
    WithdrawnTarget,
)
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_HR_STALE,
    RULE_LOOP_STALL,
    RULE_SESSION_STANDSTILL,
    SafetySupervisor,
)
from src.training.types import Occupancy, SafetyAction, SafetyVerdict, TelemetrySnapshot
from src.units import Bpm, MotorRpm, Seconds, motor_to_output_rpm
from tests.test_runtime import (
    GEOMETRY,
    OPERATOR,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _manual,  # pyright: ignore[reportPrivateUsage]  # the manual rig of ANH-176
)

MANUAL_CEILING: Final[MotorRpm] = MotorRpm(300)
BOTH: Final[tuple[Occupancy, Occupancy]] = (Occupancy.BENCH, Occupancy.OCCUPIED)
"""An empty capsule and a person declared on board: the rule is the same for both."""


@dataclass(frozen=True, slots=True)
class Hold:
    """How a warning that is not latched comes to stand in one occupancy, and its name.

    With a person on board the heart rate is withheld (``hr_stale``: FREEZE at
    10 s, REDUCE at 30 s, a latched end at 60 s). With an empty capsule the
    heart-rate rules are off, so the page stops pinging (``attendant_absent``:
    FREEZE at 60 s, a latched end at 120 s). Either way the warning lifts by
    itself the moment the test feeds and pings again.
    """

    rule: str
    feed: bool
    ping: bool
    after: float


HOLDS: Final[dict[Occupancy, Hold]] = {
    Occupancy.OCCUPIED: Hold(rule=RULE_HR_STALE, feed=False, ping=True, after=12.0),
    Occupancy.BENCH: Hold(rule=RULE_ATTENDANT_ABSENT, feed=True, ping=False, after=62.0),
}


# =========================================================================
# Reading the runtime through calls (see tests/test_runtime_standstill.py)
# =========================================================================


def _standing(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.standing


def _demand(rig: Rig) -> tuple[str, SafetyAction, bool] | None:
    """The standing verdict as ``(rule, action, latched)``, or ``None``."""
    verdict = rig.runtime.standing
    return None if verdict is None else (verdict.rule, verdict.action, verdict.latched)


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _target(rig: Rig) -> MotorRpm:
    return rig.runtime.manual_target


def _end(rig: Rig) -> EndReason | None:
    return rig.runtime.end_reason


def _taken_back(rig: Rig) -> WithdrawnTarget | None:
    return rig.runtime.take_withdrawn_target()


def _moved(snapshots: list[TelemetrySnapshot]) -> list[tuple[float, int]]:
    """Every non-zero setpoint, as ``(session time, motor rpm)``."""
    return [
        (float(snapshot.elapsed), int(snapshot.setpoint.motor_rpm))
        for snapshot in snapshots
        if snapshot.setpoint.motor_rpm != 0
    ]


def _setpoints(snapshots: list[TelemetrySnapshot]) -> set[int]:
    return {int(snapshot.setpoint.motor_rpm) for snapshot in snapshots}


def _shown_target(rig: Rig) -> int:
    """The target the page shows as applied: the last snapshot's manual view."""
    manual = rig.snapshots[-1].manual
    assert manual is not None
    return int(manual.target.motor_rpm)


def _ask(rig: Rig, rpm: int) -> Result[MotorRpm, ManualTargetRefusal]:
    """Type a target, in motor rpm, as the console does (it sends output rpm)."""
    return rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(rpm), GEOMETRY.ratio))


def _refused_by(rig: Rig, rpm: int) -> SafetyVerdict:
    """Type a target that must be refused because a verdict holds the arm; return that verdict."""
    refused = _ask(rig, rpm)
    assert isinstance(refused, Err), "the target was accepted: it would wait for the verdict to go"
    assert isinstance(refused.error, HeldAtStandstill), refused.error
    return refused.error.verdict


# =========================================================================
# Rigs
# =========================================================================


async def _not_moved_yet(occupancy: Occupancy) -> Rig:
    """A manual session just armed: target zero, nothing has turned, nothing stands."""
    rig = await _manual(occupancy, target=0)
    assert _applied(rig) == 0
    assert _standing(rig) is None
    return rig


async def _stopped_by_the_operator(occupancy: Occupancy) -> Rig:
    """At 200 motor rpm, then the operator's own target of zero: stopped, session going on."""
    rig = await _manual(occupancy, target=200)
    assert _ask(rig, 0) == Ok(MotorRpm(0))
    await rig.run(40.0)
    assert _applied(rig) == 0
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert _standing(rig) is None
    assert rig.state() is RuntimeState.RUNNING
    return rig


async def _warn(
    rig: Rig, occupancy: Occupancy, seconds: float | None = None
) -> list[TelemetrySnapshot]:
    """Let the occupancy's warning come to stand (or stand for ``seconds`` more)."""
    hold = HOLDS[occupancy]
    return await rig.run(hold.after if seconds is None else seconds, feed=hold.feed, ping=hold.ping)


async def _stall(rig: Rig) -> None:
    """One tick 1.1 s late: ``loop_stall`` latches a FREEZE, which only a name clears."""
    rig.clock.advance(Seconds(0.9))
    await rig.step()
    assert _demand(rig) == (RULE_LOOP_STALL, SafetyAction.FREEZE, True)


async def _followed(rig: Rig, rpm: int = 200) -> None:
    """With nothing standing, a target typed now is accepted and the arm goes to it."""
    assert _standing(rig) is None
    assert _ask(rig, rpm) == Ok(MotorRpm(rpm))
    await rig.run(40.0)
    assert _applied(rig) == rpm, "a target entered with nothing standing was not followed"
    assert _end(rig) is None


# =========================================================================
# THE RULE: fails on develop, passes here
# =========================================================================


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_target_typed_while_a_warning_holds_an_arm_the_operator_stopped_is_refused(
    occupancy: Occupancy,
) -> None:
    """The first measured sequence. On ``develop`` the arm left 0.2 s after the warning.

    The operator stopped the arm; a warning comes to stand; 200 rpm is typed.
    It is refused and the refusal names the warning. Thirty more seconds under
    the warning, then it lifts, and for two minutes nobody types anything:
    not one non-zero reference is written. Asked again with nothing standing,
    the target is taken and the arm goes to it.
    """
    rig = await _stopped_by_the_operator(occupancy)
    hold = HOLDS[occupancy]
    await _warn(rig, occupancy)
    assert _demand(rig) == (hold.rule, SafetyAction.FREEZE, False)
    frames = len(rig.drive.writes)

    verdict = _refused_by(rig, 200)
    assert (verdict.rule, verdict.latched) == (hold.rule, False)
    assert _target(rig) == 0
    held = await _warn(rig, occupancy, 30.0)
    assert _standing(rig) is not None, "the warning lifted too early: nothing was held"
    assert _refused_by(rig, 200).rule == hold.rule, "refused once, then accepted"

    back = await rig.run(120.0)
    assert _standing(rig) is None, "the warning never lifted: nothing was proven"
    assert not _moved(held + back), f"the arm left by itself: {_moved(held + back)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}, "a non-zero reference was written"
    assert _taken_back(rig) is None, "nothing had been accepted: there was nothing to take back"
    assert rig.state() is RuntimeState.RUNNING
    assert _end(rig) is None

    await _followed(rig)


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_target_typed_while_a_warning_holds_a_session_that_has_not_moved_is_refused(
    occupancy: Occupancy,
) -> None:
    """The second measured sequence. On ``develop``: first motion 3.2 s after the warning."""
    rig = await _not_moved_yet(occupancy)
    hold = HOLDS[occupancy]
    await _warn(rig, occupancy)
    assert _demand(rig) == (hold.rule, SafetyAction.FREEZE, False)
    frames = len(rig.drive.writes)

    assert _refused_by(rig, 200).rule == hold.rule
    held = await _warn(rig, occupancy, 10.0)
    back = await rig.run(120.0)
    assert _standing(rig) is None
    assert not _moved(held + back), f"the arm made its first motion alone: {_moved(back)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}

    await _followed(rig)


@pytest.mark.parametrize("occupancy", BOTH)
async def test_acknowledging_a_latched_verdict_over_a_stopped_arm_starts_nothing(
    occupancy: Occupancy,
) -> None:
    """The third measured sequence. On ``develop`` the arm left 0.4 s after the acknowledgement.

    A recovered loop stall latches a FREEZE over an arm the operator stopped.
    A target typed then is refused, so the acknowledgement finds nothing
    waiting: it clears the verdict and starts nothing.
    """
    rig = await _stopped_by_the_operator(occupancy)
    await _stall(rig)
    frames = len(rig.drive.writes)

    verdict = _refused_by(rig, 200)
    assert (verdict.rule, verdict.latched) == (RULE_LOOP_STALL, True)
    held = await rig.run(20.0)
    assert _demand(rig) == (RULE_LOOP_STALL, SafetyAction.FREEZE, True)

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_LOOP_STALL,)
    after = await rig.run(60.0)
    assert not _moved(held + after), f"the acknowledgement started the arm: {_moved(after)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert _end(rig) is None

    await _followed(rig)


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_target_caught_by_a_verdict_before_its_first_step_is_taken_back(
    occupancy: Occupancy,
) -> None:
    """A verdict appearing on the very tick after a target was typed, nothing standing.

    The target is accepted: nothing stood. The arm has not earned its first
    step yet when a verdict latches. On that tick the target goes back to
    zero, the page's "applied target" with it, and the console is told once
    what was taken back and by which verdict. The acknowledgement then starts
    nothing. On ``develop`` the target was kept and the acknowledgement
    started the arm.
    """
    rig = await _not_moved_yet(occupancy)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    caught = [await rig.step()]
    assert _demand(rig) == ("rig_freeze", SafetyAction.FREEZE, True)
    assert _applied(rig) == 0

    assert _target(rig) == 0, "the target is still waiting behind the verdict"
    assert _shown_target(rig) == 0
    taken = _taken_back(rig)
    assert taken is not None, "the console is not told the target was taken back"
    assert (taken.target, taken.verdict.rule, taken.verdict.latched) == (200, "rig_freeze", True)
    assert _taken_back(rig) is None, "one withdrawal was reported twice"

    caught += await rig.run(10.0)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    after = await rig.run(60.0)
    assert not _moved(caught + after), f"the arm left by itself: {_moved(caught + after)[:3]}"
    assert _taken_back(rig) is None

    await _followed(rig)


async def test_a_verdict_on_a_session_s_first_tick_takes_back_a_target_typed_before_it() -> None:
    """Armed, a target typed before the loop has ticked once, and a verdict on that first tick.

    A start is only taken with nothing standing, so a target typed straight
    after it is accepted. If the session's very first tick then finds a
    verdict, the target is taken back on it, like on any other tick. With
    nothing standing, the same target is followed at once: the second half
    shows the first half is not a session that simply cannot move.
    """
    caught = _rig(motion=DEFAULT_MOTION_LIMITS)
    assert is_ok(caught.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await caught.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    assert _ask(caught, 200) == Ok(MotorRpm(200))
    caught.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    first = await caught.step()
    assert first.safety is not None
    assert _target(caught) == 0, "the first tick left a target waiting behind its verdict"
    assert _taken_back(caught) is not None
    assert isinstance(caught.runtime.acknowledge(OPERATOR), Ok)
    assert not _moved([first, *await caught.run(30.0)])

    free = _rig(motion=DEFAULT_MOTION_LIMITS)
    assert is_ok(free.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await free.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    assert _ask(free, 200) == Ok(MotorRpm(200))
    assert _moved(await free.run(1.0)), "with nothing standing the arm did not start"
    assert _taken_back(free) is None


async def test_a_target_waiting_for_a_heart_rate_is_taken_back_when_the_warning_comes() -> None:
    """A real warning that is not latched, arriving while an accepted target waits.

    Occupied. No fresh reading for six seconds: nothing stands yet, so 200 rpm
    is accepted, and it waits, because nothing rises without a usable heart
    rate. At ten seconds ``hr_stale`` holds the arm. From that tick the target
    is zero, and when the heart rate is back nothing moves. On ``develop`` the
    arm left with the heart rate.
    """
    rig = await _not_moved_yet(Occupancy.OCCUPIED)
    await rig.run(6.0, feed=False)
    assert _standing(rig) is None
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    waiting = await rig.run(3.0, feed=False)
    assert _standing(rig) is None
    assert (_target(rig), _applied(rig)) == (200, 0)
    assert _taken_back(rig) is None

    caught = await rig.run(3.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.FREEZE, False)
    assert _target(rig) == 0
    taken = _taken_back(rig)
    assert taken is not None
    assert (taken.target, taken.verdict.rule, taken.verdict.latched) == (200, RULE_HR_STALE, False)

    back = await rig.run(120.0)
    assert _standing(rig) is None
    assert not _moved(waiting + caught + back), "the arm left when the heart rate came back"
    await _followed(rig)


async def test_the_tick_takes_the_target_back_whatever_the_refusal_saw() -> None:
    """The second half on its own: a verdict that clears and returns, held and lowered.

    The verdict is imposed at the supervisor's exit, where the tick reads it
    and :attr:`TrainingRuntime.standing` does not: the refusal cannot see it,
    so every target here is ACCEPTED, and only the tick stands between it and
    the arm. FREEZE, cleared, then REDUCE on the tick after a new target: each
    time the target is back to zero before any tick could follow it.
    """
    rig = await _stopped_by_the_operator(Occupancy.BENCH)
    imposed = _Imposed()
    seen: list[TelemetrySnapshot] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        for action in (SafetyAction.FREEZE, SafetyAction.REDUCE):
            imposed.action = action
            seen.append(await rig.step())
            assert _standing(rig) is None, "the refusal can see this verdict: not this case"
            assert _ask(rig, 200) == Ok(MotorRpm(200))
            seen.append(await rig.step())
            assert _target(rig) == 0, f"a target was left waiting behind {action.name}"
            taken = _taken_back(rig)
            assert taken is not None
            assert (taken.target, taken.verdict.action) == (200, action)
            imposed.action = None
            seen += await rig.run(15.0)

        # And the other order: typed with nothing standing, the verdict on the next tick.
        assert _ask(rig, 200) == Ok(MotorRpm(200))
        imposed.action = SafetyAction.FREEZE
        seen.append(await rig.step())
        assert _target(rig) == 0
        imposed.action = None
        seen += await rig.run(30.0)
    assert not _moved(seen), f"a waiting target was followed: {_moved(seen)[:3]}"
    await _followed(rig)


async def test_the_refusal_is_judged_on_what_the_last_tick_left_in_force() -> None:
    """Between two ticks there is only the last one: about to lift still refuses.

    The heart rate is about to be read again, but the last tick still saw the
    warning: refused. One tick later nothing stands, and the same target,
    typed now, is the operator's own command: taken, and followed.
    """
    rig = await _stopped_by_the_operator(Occupancy.OCCUPIED)
    await rig.run(12.0, feed=False)
    assert _refused_by(rig, 200).rule == RULE_HR_STALE

    await rig.step()
    assert _standing(rig) is None, "a fresh reading did not lift the warning on its tick"
    await _followed(rig)


async def test_between_a_warning_s_zero_and_its_latch_no_target_is_left_or_taken() -> None:
    """The tick a REDUCE reaches zero, and the one after, on which the session ends.

    The operator's 200 rpm is kept while the arm turns (a REDUCE is not a
    stop). On the tick the setpoint lands on zero it is zeroed at once, with
    no notice to the console: the session is over on the next tick and that
    ending says it. In between, a target is refused in the warning's name. On
    ``develop`` the 200 stayed in place for that tick and a new target was
    accepted, to be zeroed only by the ending.
    """
    rig = await _manual(Occupancy.OCCUPIED, target=200)
    for _ in range(round(120.0 / 0.2)):
        if _applied(rig) == 0:
            break
        assert _target(rig) == 200, "the target was taken back from a turning arm"
        await rig.step(feed=False)
    assert _applied(rig) == 0
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert _end(rig) is None

    assert _target(rig) == 0, "the old target outlived the standstill"
    assert _taken_back(rig) is None, "a withdrawal was announced on a session that is ending"
    assert _refused_by(rig, 200).rule == RULE_HR_STALE

    await rig.step(feed=False)
    assert _demand(rig) == (RULE_SESSION_STANDSTILL, SafetyAction.RAMP_DOWN, True)
    ending = _ask(rig, 200)
    assert isinstance(ending, Err)
    assert isinstance(ending.error, ManualEnding)
    assert _taken_back(rig) is None


async def test_a_new_session_does_not_inherit_a_withdrawal_nobody_read() -> None:
    """What was taken back belongs to its session: a new start forgets it."""
    rig = await _not_moved_yet(Occupancy.BENCH)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.step()
    assert _target(rig) == 0
    rig.runtime.request_stop("operator: stop button")
    await rig.run(75.0)
    assert rig.state() is RuntimeState.FINISHED
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)

    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    assert _taken_back(rig) is None, "the previous session's withdrawal was carried over"
    assert _target(rig) == 0


# --- the guarantee, over arbitrary interleavings ---------------------------

_OPERATIONS: Final = st.one_of(
    st.tuples(st.just("target"), st.sampled_from([0, 60, 120, 200])),
    st.tuples(st.just("ticks"), st.integers(min_value=1, max_value=80)),
    st.tuples(st.just("heart rate"), st.sampled_from([0, 1])),
    st.tuples(st.just("presence"), st.sampled_from([0, 1])),
    st.tuples(st.just("stalled tick"), st.just(0)),
    st.tuples(st.just("trip"), st.sampled_from([1, 2])),
    st.tuples(st.just("acknowledge"), st.just(0)),
)
"""What an operator, a rider's electrodes and a loop can do to a manual session, in any order.

``trip`` latches a FREEZE (1) or a REDUCE (2) from another thread, as a
detector would; ``heart rate`` and ``presence`` switch a feed off (0) or on
(1), which raises and lifts the two warnings that are not latched.
"""


@dataclass
class _Session:
    """One arbitrary manual session, and what the test knows about its target.

    ``clean`` is the whole property. ``None``: no speed is being asked for.
    ``True``: the target in force was entered while nothing stood. ``False``:
    it was entered over a turning arm under a verdict, or a verdict has stood
    over the stopped arm since. The arm may only leave standstill on ``True``.
    """

    rig: Rig
    feed: bool = True
    ping: bool = True
    clean: bool | None = None

    def ask(self, rpm: int) -> None:
        standing = _standing(self.rig)
        stopped = _applied(self.rig) == 0
        asked = _ask(self.rig, rpm)
        if isinstance(asked, Err):
            return
        if rpm == 0:
            self.clean = None
            return
        assert not (stopped and standing is not None), (
            f"{rpm} rpm was accepted over a stopped arm while {standing} stood"
        )
        self.clean = standing is None

    async def tick(self, *, late: bool = False) -> None:
        rig = self.rig
        was = _applied(rig)
        if late:
            rig.clock.advance(Seconds(0.9))
        snapshot = await rig.step(feed=self.feed, ping=self.ping)
        if was == 0 and _applied(rig) != 0:
            assert self.clean is True, (
                f"the arm left standstill at {float(snapshot.elapsed):.1f} s for a target "
                "that was not entered with nothing standing"
            )
        if snapshot.safety is not None and _applied(rig) == 0 and self.clean is not None:
            self.clean = False
        if _target(rig) == 0:
            self.clean = None


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    occupancy=st.sampled_from(BOTH),
    operations=st.lists(_OPERATIONS, min_size=4, max_size=40),
)
async def test_a_stopped_arm_only_leaves_for_a_target_entered_with_nothing_standing(
    occupancy: Occupancy, operations: list[tuple[str, int]]
) -> None:
    """The acceptance criterion, as a property: targets, warnings, latches, acknowledgements.

    Whatever the order: no non-zero target is accepted over a stopped arm
    while a verdict stands, and the setpoint never leaves zero for a target
    that was entered under a verdict or that a verdict has since held at
    standstill. On ``develop`` the first half already fails on the third
    operation of the simplest sequence (a warning, then a target).
    """
    session = _Session(await _manual(occupancy, target=0))
    rig = session.rig
    for kind, value in operations:
        match kind:
            case "target":
                session.ask(value)
            case "ticks":
                for _ in range(value):
                    await session.tick()
            case "heart rate":
                session.feed = bool(value)
            case "presence":
                session.ping = bool(value)
            case "stalled tick":
                await session.tick(late=True)
            case "trip":
                rig.runtime.trip_from_thread("rig_trip", SafetyAction(value), "under test")
            case _:
                rig.runtime.acknowledge(OPERATOR)
    for _ in range(100):
        await session.tick()


# =========================================================================
# UNCHANGED ON PURPOSE: passes on develop and here
# =========================================================================


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_target_typed_while_a_warning_holds_a_turning_arm_is_kept_and_followed(
    occupancy: Occupancy,
) -> None:
    """A verdict stands but the arm turns: the target is taken, kept, and followed after."""
    rig = await _manual(occupancy, target=200)
    hold = HOLDS[occupancy]
    await _warn(rig, occupancy)
    assert _demand(rig) == (hold.rule, SafetyAction.FREEZE, False)
    assert _applied(rig) == 200

    assert _ask(rig, 250) == Ok(MotorRpm(250))
    held = await _warn(rig, occupancy, 5.0)
    assert _setpoints(held) == {200}, "the hold did not hold"
    assert _target(rig) == 250
    assert _shown_target(rig) == 250
    assert _taken_back(rig) is None

    await rig.run(60.0)
    assert _standing(rig) is None
    assert _applied(rig) == 250, "the target typed under the warning was not followed after it"
    assert _end(rig) is None


async def test_a_target_typed_while_a_warning_lowers_a_turning_arm_is_kept_and_followed() -> None:
    """REDUCE part way: the arm slows, a target is typed, the warning lifts before zero."""
    rig = await _manual(Occupancy.OCCUPIED, target=200)
    await rig.run(33.0, feed=False)
    assert _demand(rig) == (RULE_HR_STALE, SafetyAction.REDUCE, False)
    assert 55 <= _applied(rig) < 200

    assert _ask(rig, 150) == Ok(MotorRpm(150))
    assert _target(rig) == 150
    await rig.run(60.0)
    assert _standing(rig) is None
    assert _applied(rig) == 150
    assert _taken_back(rig) is None
    assert _end(rig) is None


async def test_a_target_typed_under_a_latched_hold_of_a_turning_arm_is_kept() -> None:
    """A latched FREEZE over a TURNING arm: the acknowledgement resumes it, as it always has."""
    rig = await _manual(Occupancy.BENCH, target=200)
    await _stall(rig)
    assert _applied(rig) == 200

    assert _ask(rig, 250) == Ok(MotorRpm(250))
    held = await rig.run(10.0)
    assert _setpoints(held) == {200}
    assert _target(rig) == 250
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    await rig.run(40.0)
    assert _applied(rig) == 250
    assert _taken_back(rig) is None


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_target_of_zero_is_taken_whatever_stands(occupancy: Occupancy) -> None:
    """Zero is never refused on this ground: at standstill under a warning, and while turning."""
    stopped = await _stopped_by_the_operator(occupancy)
    await _warn(stopped, occupancy)
    assert _standing(stopped) is not None
    assert _ask(stopped, 0) == Ok(MotorRpm(0))
    assert _taken_back(stopped) is None

    turning = await _manual(occupancy, target=200)
    await _warn(turning, occupancy)
    assert _standing(turning) is not None
    assert _ask(turning, 0) == Ok(MotorRpm(0))
    assert _target(turning) == 0


async def test_a_manual_start_under_a_standing_verdict_is_refused_and_carries_no_target() -> None:
    """The same state through the other door: a START never implies a speed.

    With a verdict standing over a stopped machine a manual start is refused,
    whatever the verdict. Once it is cleared the start is taken, and the
    session it arms asks for nothing: the target is zero until somebody types
    one, so no start can stand in for a target.
    """
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.step()
    refused = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == "rig_freeze"

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    assert _target(rig) == 0
    armed = await rig.run(30.0)
    assert not _moved(armed), "a start moved the arm with no target typed"


# =========================================================================
# LEFT AS IT IS: the heart-rate gate, with no verdict standing
# =========================================================================


async def test_a_target_held_only_by_the_heart_rate_gate_is_followed_when_it_opens() -> None:
    """Outside ANH-178, measured and pinned: a wait behind no verdict at all.

    Occupied. No fresh reading for six seconds, which is not yet a warning.
    200 rpm is typed and accepted: nothing stands. Nothing rises without a
    usable heart rate, so the arm waits; a reading comes back two seconds
    later and the arm leaves, with nobody clicking at that moment. The target
    was entered while no verdict stood, which is what the rule requires, and
    the wait is bounded: at ten seconds ``hr_stale`` stands and takes the
    target back (the test above). Passes on ``develop`` too.
    """
    rig = await _not_moved_yet(Occupancy.OCCUPIED)
    await rig.run(6.0, feed=False)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    waiting = await rig.run(2.0, feed=False)
    assert not _moved(waiting)
    assert {snapshot.safety for snapshot in waiting} == {None}

    back = await rig.run(40.0)
    assert {snapshot.safety for snapshot in back} == {None}, "a verdict stood: not this case"
    assert _applied(rig) == 200, "the gate no longer lets the waiting target through: update this"
    assert _taken_back(rig) is None


async def test_a_target_held_by_a_falling_heart_rate_is_followed_when_the_fall_ends() -> None:
    """Outside ANH-178 too, and the longer wait: over a minute, with no verdict at any time.

    Occupied, on the console's own limits. The operator stops the arm; the
    heart rate comes down 30 bpm/min, as it does after an effort. Ten seconds
    into the fall 200 rpm is typed and accepted: nothing stands. The vasovagal
    gate lets nothing rise while the rate falls faster than 20 bpm/min, so the
    target waits, for ninety seconds here, and the arm leaves when the fall
    ends, with nobody clicking at that moment. No rule speaks at any point, so
    the rule of ANH-178, which is about verdicts, has nothing to refuse or to
    take back. Measured, pinned so that it is known, and reported to the
    product owner. Passes on ``develop`` too.
    """
    rig = _rig(limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(130)
    rig.feed(Bpm(130))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING))
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    await rig.run(40.0)
    assert _ask(rig, 0) == Ok(MotorRpm(0))
    await rig.run(40.0)
    assert _applied(rig) == 0

    falling: list[TelemetrySnapshot] = []
    for bpm in range(129, 124, -1):
        rig.fed_bpm = Bpm(bpm)
        falling += await rig.run(2.0)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    for bpm in range(124, 79, -1):
        rig.fed_bpm = Bpm(bpm)
        falling += await rig.run(2.0)
    assert not _moved(falling), "the gate did not hold the rise while the rate fell"
    assert {snapshot.safety for snapshot in falling} == {None}, "a verdict stood: not this case"
    assert _target(rig) == 200, "the target is no longer waiting: update this test"

    settled = await rig.run(20.0)
    assert {snapshot.safety for snapshot in settled} == {None}
    assert _applied(rig) > 0, "the waiting target was not followed: update this test"
    assert _taken_back(rig) is None
