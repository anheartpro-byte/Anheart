"""No manual target waits over a stopped arm, whatever holds the rise (ANH-178).

A manual target is a destination. While a rise is held it is not followed, so
one entered over a setpoint of zero used to WAIT, and the arm left by itself
when the hold went. Measured by the independent review of ANH-176, on
``develop``:

* the operator brings the arm to zero (the session goes on, nothing stands);
  the heart rate is lost (FREEZE ``hr_stale``); 200 rpm is typed and accepted;
  thirty seconds with nothing moving; the heart rate returns, and the first
  non-zero setpoint is written 0.2 s later with nobody clicking;
* the same in a manual session that had not moved yet, first motion 3.2 s
  after the heart rate returns;
* with a LATCHED verdict (a recovered ``loop_stall``) over an arm the operator
  stopped, the target waited for the acknowledgement and the arm moved 0.4 s
  after it: acknowledging was what started motion.

And measured on the first version of this change (head ``da5efa9``), which
judged verdicts only: with a person on board the heart rate holds a rise with
NO verdict at all, and a target waited behind it just the same. Six seconds
without a reading, 200 rpm typed, the reading back: the arm left. A heart rate
coming down 30 bpm/min after an effort: the target waited ninety seconds and
the arm left when the fall ended. Decision of 2026-10-06: the same treatment.

The rule, in two halves that close on each other:

* **refused**: while anything holds a rise over a setpoint of zero, a non-zero
  target is refused, and the refusal names what holds: the standing verdict,
  latched or not, or the heart rate's reason;
* **taken back**: a target already entered goes back to zero on the tick
  something holds the arm at standstill, and the console is told once. That
  covers a drive that did not acknowledge the first step, which no refusal
  can foresee.

So a stopped arm is given a speed by one thing only: a target entered while
nothing holds it. Nothing changes for an arm that is turning.

Three groups, and the split matters when reading a failure:

* **the rule, for a verdict**: these fail on ``develop`` (the target is
  accepted, or kept, and the arm then moves) and pass on ``da5efa9`` and here;
* **the rule, with no verdict**: these fail on ``da5efa9`` and pass here;
* **unchanged on purpose**: a turning arm, a target of zero, a start, an
  ordinary first target. These pass everywhere.

Every test runs on the fake drive and the manual clock of
``tests/test_runtime.py``, with the shipped motion limits.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.local_panel import RUNTIME_LIMITS
from src.motor.drive import BadResponse, DriveError
from src.result import Err, Ok, Result, is_ok
from src.training.hr_control import HeartRateTracker
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import (
    EndReason,
    HeldAtStandstill,
    Holding,
    ManualEnding,
    ManualTargetRefusal,
    RiseHold,
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
from src.training.types import (
    HeartRateSample,
    Occupancy,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    TelemetrySnapshot,
)
from src.units import Bpm, Monotonic, MotorRpm, Seconds, motor_to_output_rpm
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    FakeDrive,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _manual,  # pyright: ignore[reportPrivateUsage]  # the manual rig of ANH-176
    _read_before_the_start,  # pyright: ignore[reportPrivateUsage]
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


@dataclass(frozen=True, slots=True)
class Closing:
    """Readings that close the heart-rate gate the instant they arrive, and its reason then.

    Pushed out of band, between two ticks, on a rate that was steady at 80: one
    reading three beats lower puts the five-reading trend far below -20
    bpm/min; three readings thirty beats away are a confirmed jump, after
    which the tracker starts its history again and has no trend to give. (A
    second after such a jump ``hr_rate`` comes to stand as well: the tests
    that use it judge the tick before.)
    """

    readings: tuple[int, ...]
    hold: RiseHold


CLOSINGS: Final[dict[str, Closing]] = {
    "a falling rate": Closing(readings=(77,), hold=RiseHold.HEART_RATE_FALLING),
    "a confirmed jump": Closing(readings=(110, 110, 110), hold=RiseHold.TREND_UNKNOWN),
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


def _verdicts(snapshots: list[TelemetrySnapshot]) -> set[str]:
    """Every rule that stood on any of these ticks. Empty: no verdict at any time."""
    return {snapshot.safety.rule for snapshot in snapshots if snapshot.safety is not None}


def _shown_target(rig: Rig) -> int:
    """The target the page shows as applied: the last snapshot's manual view."""
    manual = rig.snapshots[-1].manual
    assert manual is not None
    return int(manual.target.motor_rpm)


def _ask(rig: Rig, rpm: int) -> Result[MotorRpm, ManualTargetRefusal]:
    """Type a target, in motor rpm, as the console does (it sends output rpm)."""
    return rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(rpm), GEOMETRY.ratio))


def _held_by(rig: Rig, rpm: int) -> Holding:
    """Type a target that must be refused because something holds the arm; return what."""
    refused = _ask(rig, rpm)
    assert isinstance(refused, Err), "the target was accepted: it would wait for the hold to go"
    assert isinstance(refused.error, HeldAtStandstill), refused.error
    return refused.error.by


def _refused_by(rig: Rig, rpm: int) -> SafetyVerdict:
    """Type a target that must be refused because a VERDICT holds the arm; return that verdict."""
    by = _held_by(rig, rpm)
    assert isinstance(by, SafetyVerdict), f"refused, but not in a verdict's name: {by}"
    return by


def _refused_for(rig: Rig, rpm: int) -> RiseHold:
    """Type a target that must be refused although NO verdict stands; return the reason."""
    assert _standing(rig) is None, "a verdict stands: not this case"
    by = _held_by(rig, rpm)
    assert isinstance(by, RiseHold), f"refused in a verdict's name with none standing: {by}"
    return by


def _taken_by(rig: Rig) -> tuple[int, str, bool]:
    """The target a VERDICT took back, as ``(target, rule, latched)``. Reads it, so once."""
    taken = _taken_back(rig)
    assert taken is not None, "the console is not told the target was taken back"
    assert isinstance(taken.by, SafetyVerdict), taken.by
    return int(taken.target), taken.by.rule, taken.by.latched


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


async def _on_the_console_s_limits(bpm: int) -> Rig:
    """A person on board on the CONSOLE's own limits, armed, the ECG read before the start.

    What a real start looks like to the heart-rate gate: the ECG has been
    running, so the tracker holds more readings than its trend needs.
    """
    rig = _rig(limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    _read_before_the_start(rig, bpm)
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING))
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
    """With nothing holding, a target typed now is accepted and the arm goes to it."""
    assert _standing(rig) is None
    assert _ask(rig, rpm) == Ok(MotorRpm(rpm))
    await rig.run(40.0)
    assert _applied(rig) == rpm, "a target entered with nothing holding was not followed"
    assert _end(rig) is None


@dataclass
class _Deaf:
    """A drive that, while ``on``, acknowledges no reference above the one it holds.

    Every other frame is acknowledged, the keepalive included, so no run of
    failures ever builds up and ``comms_lost`` has nothing to count: a rise
    that is not acknowledged is not a verdict. From standstill, "above the one
    it holds" is every first step.
    """

    on: bool = False
    holds: MotorRpm = MotorRpm(0)
    refused: list[MotorRpm] = field(default_factory=list[MotorRpm])


def _deafened(deaf: _Deaf) -> Callable[[FakeDrive, MotorRpm], Awaitable[Result[None, DriveError]]]:
    """Wrap the fake drive's speed write so that ``deaf`` decides which rises it takes."""
    real = FakeDrive.write_speed

    async def write_speed(drive: FakeDrive, rpm: MotorRpm) -> Result[None, DriveError]:
        if deaf.on and rpm > deaf.holds:
            deaf.refused.append(rpm)
            return Err(BadResponse(detail="under test: the rise was not acknowledged"))
        outcome = await real(drive, rpm)
        if isinstance(outcome, Ok):
            deaf.holds = rpm
        return outcome

    return write_speed


# =========================================================================
# THE RULE, FOR A VERDICT: fails on develop, passes on da5efa9 and here
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
    assert _taken_by(rig) == (200, "rig_freeze", True)
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


async def _ticks_until_nobody_is_watching() -> int:
    """How many ticks without a ping bring ``attendant_absent`` to stand over an empty capsule.

    Found by ticking, not computed from the rule's sixty seconds: the test
    below needs the one tick before the warning, whichever side of a float
    comparison that falls on.
    """
    scout = await _not_moved_yet(Occupancy.BENCH)
    for ticks in range(1, 400):
        await scout.step(ping=False)
        if _standing(scout) is not None:
            return ticks
    raise AssertionError("attendant_absent never came to stand")


async def test_a_real_warning_on_the_tick_after_a_target_takes_it_back() -> None:
    """A warning of the supervisor's own, not latched, arriving on a target's first tick.

    Empty capsule; the page stops pinging. One tick before ``attendant_absent``
    nothing stands, so 200 rpm is accepted. The next tick brings the warning
    before the arm has moved: the target is zero from that tick, the console
    is told which warning took it, and when the pings return nothing moves.
    On ``develop`` the target was kept and the arm left with the pings.
    """
    quiet = await _ticks_until_nobody_is_watching()
    rig = await _not_moved_yet(Occupancy.BENCH)
    for _ in range(quiet - 1):
        await rig.step(ping=False)
    assert _standing(rig) is None
    assert _ask(rig, 200) == Ok(MotorRpm(200))

    caught = [await rig.step(ping=False)]
    assert _demand(rig) == (RULE_ATTENDANT_ABSENT, SafetyAction.FREEZE, False)
    assert (_applied(rig), _target(rig)) == (0, 0)
    assert _taken_by(rig) == (200, RULE_ATTENDANT_ABSENT, False)

    back = await rig.run(120.0)
    assert _standing(rig) is None
    assert not _moved(caught + back), "the arm left when the pings came back"
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
            assert isinstance(taken.by, SafetyVerdict)
            assert (taken.target, taken.by.action) == (200, action)
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
    warning: refused, and in the warning's name, which comes before the heart
    rate's own reason whenever both hold. One tick later nothing stands, and
    the same target, typed now, is the operator's own command: taken, and
    followed.
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
    stop, and neither is a heart rate that lets nothing rise). On the tick
    the setpoint lands on zero it is zeroed at once, with no notice to the
    console: the session is over on the next tick and that ending says it. In
    between, a target is refused in the warning's name. On ``develop`` the 200
    stayed in place for that tick and a new target was accepted, to be zeroed
    only by the ending.
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


# =========================================================================
# THE RULE, WITH NO VERDICT: fails on da5efa9, passes here
# =========================================================================


async def test_a_target_typed_during_a_gap_in_the_heart_rate_is_refused() -> None:
    """The shorter wait measured on ``da5efa9``: six seconds without a reading.

    Person on board, nothing has moved. No fresh reading for six seconds,
    which is not yet a warning. 200 rpm is typed: refused, and the refusal
    says there is no usable heart rate. The reading comes back two seconds
    later, and for two minutes nobody types anything: nothing moves, and no
    verdict has stood at any time. Asked again, the target is taken and
    followed. On ``da5efa9`` it was accepted and the arm left with the reading.
    """
    rig = await _not_moved_yet(Occupancy.OCCUPIED)
    gap = await rig.run(6.0, feed=False)
    frames = len(rig.drive.writes)

    assert _refused_for(rig, 200) is RiseHold.NO_HEART_RATE
    assert _target(rig) == 0
    gap += await rig.run(2.0, feed=False)
    assert _refused_for(rig, 200) is RiseHold.NO_HEART_RATE, "refused once, then accepted"

    back = await rig.run(120.0)
    assert not _verdicts(gap + back), "a verdict stood: this is not the case under test"
    assert not _moved(gap + back), f"the arm left with the heart rate: {_moved(back)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}, "a non-zero reference was written"
    assert _taken_back(rig) is None, "nothing had been accepted: there was nothing to take back"
    assert rig.state() is RuntimeState.RUNNING

    await _followed(rig)


async def test_a_target_typed_while_the_heart_rate_falls_is_refused() -> None:
    """The longer wait measured on ``da5efa9``: ninety seconds, and no verdict at any time.

    Person on board, on the console's own limits. The operator stops the arm;
    the heart rate comes down 30 bpm/min, as it does after an effort. Ten
    seconds into the fall 200 rpm is typed: refused, because the rate falls
    faster than the vasovagal gate allows (20 bpm/min). Refused again at the
    end of the fall, ninety seconds later. Then the rate settles and for two
    minutes nobody types anything: nothing moves. Asked again, the target is
    taken and followed. On ``da5efa9`` the first one was accepted, waited
    ninety seconds, and the arm left when the fall ended.
    """
    rig = await _on_the_console_s_limits(130)
    await _followed(rig)
    assert _ask(rig, 0) == Ok(MotorRpm(0))
    await rig.run(40.0)
    assert _applied(rig) == 0
    frames = len(rig.drive.writes)

    falling: list[TelemetrySnapshot] = []
    for bpm in range(129, 124, -1):
        rig.fed_bpm = Bpm(bpm)
        falling += await rig.run(2.0)
    assert _refused_for(rig, 200) is RiseHold.HEART_RATE_FALLING
    for bpm in range(124, 79, -1):
        rig.fed_bpm = Bpm(bpm)
        falling += await rig.run(2.0)
    assert _refused_for(rig, 200) is RiseHold.HEART_RATE_FALLING, "refused once, then accepted"

    settled = await rig.run(120.0)
    assert not _verdicts(falling + settled), "a verdict stood: this is not the case under test"
    assert not _moved(falling + settled), f"the arm left when the fall ended: {_moved(settled)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert _taken_back(rig) is None

    await _followed(rig)


async def test_a_first_target_before_the_trend_is_known_is_refused_then_taken() -> None:
    """The first seconds of an ECG: a rate to start on, and no trend yet to rise on.

    One reading is enough for the start (a person on board needs a fresh
    heart rate) and too few for the five-reading trend, so the gate lets
    nothing rise. A first target typed then is refused, and says so. A minute
    passes, the trend long known: nothing has moved. Typed again, the target
    is taken and followed at once. On ``da5efa9`` the first one was accepted
    and the arm left four seconds later, when the fifth reading came.
    """
    rig = _rig(limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(75)
    rig.feed(Bpm(75))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MANUAL_CEILING))
    frames = len(rig.drive.writes)

    assert _refused_for(rig, 200) is RiseHold.TREND_UNKNOWN
    quiet = await rig.run(60.0)
    assert not _verdicts(quiet)
    assert not _moved(quiet), f"the arm left when the trend became known: {_moved(quiet)[:3]}"
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}

    assert _ask(rig, 200) == Ok(MotorRpm(200))
    assert _moved(await rig.run(0.4)), "typed with nothing holding, the target did not start it"


@pytest.mark.parametrize("closing", CLOSINGS.values(), ids=CLOSINGS.keys())
async def test_a_target_the_heart_rate_holds_before_its_first_step_is_taken_back(
    closing: Closing,
) -> None:
    """The gate closes between a target and its first step: the tick takes the target back.

    Steady rate, nothing holds: 200 rpm is accepted. Before the next tick a
    reading arrives that closes the gate. That tick writes nothing, puts the
    target back to zero, the page's "applied target" with it, and the console
    is told once which target and why. The rate then settles and nothing
    moves. On ``da5efa9`` the target was kept and followed when the gate
    opened again.
    """
    rig = await _not_moved_yet(Occupancy.OCCUPIED)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    for bpm in closing.readings:
        rig.feed(Bpm(bpm))
    rig.fed_bpm = Bpm(closing.readings[-1])

    caught = [await rig.step(feed=False)]
    assert not _verdicts(caught), "a verdict stood: this is not the case under test"
    assert _applied(rig) == 0
    assert _target(rig) == 0, "the target is still waiting behind the heart rate"
    assert _shown_target(rig) == 0
    assert _taken_back(rig) == WithdrawnTarget(target=MotorRpm(200), by=closing.hold)
    assert _taken_back(rig) is None, "one withdrawal was reported twice"

    after = await rig.run(120.0)
    assert not _moved(caught + after), f"the arm left by itself: {_moved(after)[:3]}"
    assert _taken_back(rig) is None
    await _followed(rig)


async def _ticks_a_reading_stays_usable() -> int:
    """How many ticks without a reading still leave the heart rate usable to a target.

    Found by asking, not computed from the tracker's freshness window: the
    boundary is a float comparison, and the test below needs the last tick on
    the usable side of it, wherever that is. A target accepted here is put
    back to zero before any tick can follow it.
    """
    scout = await _not_moved_yet(Occupancy.OCCUPIED)
    for ticks in range(60):
        asked = _ask(scout, 200)
        if isinstance(asked, Err):
            assert isinstance(asked.error, HeldAtStandstill), asked.error
            assert asked.error.by is RiseHold.NO_HEART_RATE, (
                f"the heart rate alone never refused anything: {asked.error.by}"
            )
            return ticks - 1
        assert _ask(scout, 0) == Ok(MotorRpm(0))
        await scout.step(feed=False)
    raise AssertionError("a target was accepted for twelve seconds without a reading")


async def test_a_target_whose_first_step_finds_no_heart_rate_is_taken_back() -> None:
    """The reading goes stale between the target and its first step.

    The last instant at which the heart rate is still usable: 200 rpm is
    accepted. One tick later the reading is too old, and no verdict stands
    yet (``hr_stale`` is six seconds away). The target goes back to zero and
    the console is told why. The readings resume: nothing moves. On
    ``da5efa9`` the target waited and the arm left with the next reading.
    """
    usable = await _ticks_a_reading_stays_usable()
    rig = await _not_moved_yet(Occupancy.OCCUPIED)
    for _ in range(usable):
        await rig.step(feed=False)
    assert _ask(rig, 200) == Ok(MotorRpm(200)), "the reading was already too old"

    caught = [await rig.step(feed=False)]
    assert not _verdicts(caught), "a verdict stood: this is not the case under test"
    assert (_applied(rig), _target(rig)) == (0, 0), "the target waits for the next reading"
    assert _taken_back(rig) == WithdrawnTarget(target=MotorRpm(200), by=RiseHold.NO_HEART_RATE)

    back = await rig.run(120.0)
    assert not _verdicts(back)
    assert not _moved(caught + back), f"the arm left with the heart rate: {_moved(back)[:3]}"
    await _followed(rig)


@pytest.mark.parametrize("occupancy", BOTH)
async def test_a_first_step_the_drive_does_not_acknowledge_takes_the_target_back(
    occupancy: Occupancy,
) -> None:
    """A third way a rise is held over a stopped arm, and it is no verdict either.

    The drive acknowledges the keepalive and refuses the first step. Nothing
    can refuse the target beforehand: it is accepted. The tick asks the drive
    once, is not acknowledged, and takes the target back, telling the console
    why. Twenty seconds later the drive hears again: nothing is asked of it,
    because nobody has typed anything. On ``da5efa9`` the target stayed, the
    first step was asked again five times a second with no verdict ever
    standing, and the arm left the moment the drive acknowledged one.
    """
    deaf = _Deaf()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeDrive, "write_speed", _deafened(deaf))
        rig = await _stopped_by_the_operator(occupancy)
        frames = len(rig.drive.writes)
        deaf.on = True
        assert _ask(rig, 200) == Ok(MotorRpm(200)), "nothing is known to hold: it is taken"

        caught = [await rig.step()]
        assert len(deaf.refused) == 1, "the first step was not asked of the drive on this tick"
        assert (_applied(rig), _target(rig)) == (0, 0), "the target is still waiting for the drive"
        assert _shown_target(rig) == 0
        taken = WithdrawnTarget(target=MotorRpm(200), by=RiseHold.WRITE_UNACKNOWLEDGED)
        assert _taken_back(rig) == taken
        assert _taken_back(rig) is None, "one withdrawal was reported twice"

        refusing = await rig.run(20.0)
        deaf.on = False
        hearing = await rig.run(60.0)
        seen = caught + refusing + hearing
        assert not _verdicts(seen), "a verdict stood: this is not the case under test"
        assert not _moved(seen), f"the arm left when the drive heard again: {_moved(seen)[:3]}"
        assert len(deaf.refused) == 1, "the first step was asked again with nobody clicking"
        assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}

        await _followed(rig)


async def test_a_verdict_takes_a_target_back_in_its_own_name_whatever_held_before() -> None:
    """What held the tick before is not what holds now: a verdict speaks in its own name.

    The drive does not acknowledge a first step, and the target is taken back
    in the drive's name. Typed again, it is accepted; before the next tick a
    verdict latches. That tick does not follow the target at all, so nothing
    rewrites the runtime's account of the tick before, and the target must
    still be taken back in the VERDICT's name, which is the one the operator
    has to acknowledge.
    """
    deaf = _Deaf()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeDrive, "write_speed", _deafened(deaf))
        rig = await _not_moved_yet(Occupancy.BENCH)
        deaf.on = True
        assert _ask(rig, 200) == Ok(MotorRpm(200))
        await rig.step()
        assert _target(rig) == 0, "the target is still waiting for the drive"
        taken = WithdrawnTarget(target=MotorRpm(200), by=RiseHold.WRITE_UNACKNOWLEDGED)
        assert _taken_back(rig) == taken

        assert _ask(rig, 200) == Ok(MotorRpm(200))
        rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
        await rig.step()
        assert _taken_by(rig) == (200, "rig_freeze", True)
        assert len(deaf.refused) == 1, "a first step was asked of the drive under a verdict"


# --- the guarantee, over arbitrary interleavings ---------------------------

_OPERATIONS: Final = st.one_of(
    st.tuples(st.just("target"), st.sampled_from([0, 60, 120, 200])),
    st.tuples(st.just("ticks"), st.integers(min_value=1, max_value=80)),
    st.tuples(st.just("heart rate"), st.sampled_from([0, 1])),
    st.tuples(st.just("pulse"), st.sampled_from([-2, -1, 0, 1])),
    st.tuples(st.just("presence"), st.sampled_from([0, 1])),
    st.tuples(st.just("drive"), st.sampled_from([0, 1])),
    st.tuples(st.just("stalled tick"), st.just(0)),
    st.tuples(st.just("trip"), st.sampled_from([1, 2])),
    st.tuples(st.just("acknowledge"), st.just(0)),
)
"""What an operator, a rider's heart, a drive and a loop can do to a manual session, in any order.

``trip`` latches a FREEZE (1) or a REDUCE (2) from another thread, as a
detector would; ``heart rate`` and ``presence`` switch a feed off (0) or on
(1), which raises and lifts the two warnings that are not latched and, well
before ``hr_stale``, leaves no usable heart rate; ``pulse`` makes every
following reading differ from the one before by that many beats (-1 a second
is a fall of 60 bpm/min); ``drive`` makes the drive refuse (1) or take (0)
any rise.
"""


@dataclass
class _Session:
    """One arbitrary manual session, and what the test knows about its target.

    ``shadow`` is a tracker of the test's own, given every reading the rig
    feeds and nothing else: the heart-rate gate is recomputed from it, so the
    property does not ask the runtime what it holds a rise for.

    ``clean`` says how the target in force was entered. ``None``: no speed is
    being asked for. ``True``: entered while nothing held a rise. ``False``:
    entered over a turning arm while something did. The arm may only leave
    standstill on ``True``. ``waited`` counts the ticks a non-zero target has
    spent over a stopped arm.
    """

    rig: Rig
    occupancy: Occupancy
    deaf: _Deaf
    shadow: HeartRateTracker = field(default_factory=HeartRateTracker)
    feed: bool = True
    ping: bool = True
    drift: int = 0
    clean: bool | None = None
    waited: int = 0

    def heart_rate_holds(self) -> bool:
        """The gate of a person on board, from the readings fed: no rate, no trend, or a fall."""
        if self.occupancy is not Occupancy.OCCUPIED:
            return False
        if self.shadow.usable(self.rig.now) is None:
            return True
        trend = self.shadow.recent_rate(LIMITS.trend_samples)
        return trend is None or trend < LIMITS.falling_trend

    def held(self) -> bool:
        """Whether a verdict or the heart rate holds a rise at this instant."""
        return _standing(self.rig) is not None or self.heart_rate_holds()

    def ask(self, rpm: int) -> None:
        stopped = _applied(self.rig) == 0
        held = self.held()
        asked = _ask(self.rig, rpm)
        if isinstance(asked, Err):
            return
        if rpm == 0:
            self.clean = None
            return
        assert not (stopped and held), (
            f"{rpm} rpm was accepted over a stopped arm while something held a rise "
            f"(verdict {_standing(self.rig)}, heart rate holding: {self.heart_rate_holds()})"
        )
        self.clean = not held
        self.waited = 0

    async def tick(self, *, late: bool = False) -> None:
        rig = self.rig
        was = _applied(rig)
        fed = rig.seq
        if late:
            rig.clock.advance(Seconds(0.9))
        snapshot = await rig.step(feed=self.feed, ping=self.ping)
        if rig.seq != fed:
            self._mirror()
        at = f"at {float(snapshot.elapsed):.1f} s"
        if was == 0 and _applied(rig) != 0:
            assert self.clean is True, (
                f"the arm left standstill {at} for a target that was not entered with "
                "nothing holding a rise"
            )
        stopped = _applied(rig) == 0
        if stopped and (snapshot.safety is not None or self.held()):
            assert _target(rig) == 0, (
                f"{at} the arm is at zero, a rise is held (verdict {snapshot.safety}, heart "
                f"rate holding: {self.heart_rate_holds()}) and {_target(rig)} rpm is waiting"
            )
        if stopped and _target(rig) != 0:
            self.waited += 1
            assert self.waited <= 1, (
                f"{at} a target has waited {self.waited} ticks over a stopped arm, held by "
                "nothing this test knows of"
            )
        if _target(rig) == 0:
            self.clean = None

    def _mirror(self) -> None:
        """Give the shadow tracker the reading the rig has just fed, then let the pulse drift."""
        rig = self.rig
        bpm = rig.fed_bpm
        assert bpm is not None
        self.shadow.observe(
            HeartRateSample(
                bpm=bpm, quality=SignalQuality.GOOD, seq=rig.seq, at=Monotonic(rig.last_feed)
            )
        )
        rig.fed_bpm = Bpm(min(120, max(50, bpm + self.drift)))


async def _session(occupancy: Occupancy, deaf: _Deaf) -> _Session:
    """A manual session armed after six seconds of ECG, every reading mirrored from the first.

    The readings before the start are pushed out of band, a second apart, as
    ``_read_before_the_start`` does, and each is given to the shadow tracker.
    """
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(80)
    session = _Session(rig=rig, occupancy=occupancy, deaf=deaf)
    for _ in range(6):
        rig.clock.advance(Seconds(1.0))
        rig.feed(Bpm(80))
        session.shadow.observe(
            HeartRateSample(bpm=Bpm(80), quality=SignalQuality.GOOD, seq=rig.seq, at=rig.now)
        )
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(occupancy, OPERATOR, MANUAL_CEILING))
    return session


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    occupancy=st.sampled_from(BOTH),
    operations=st.lists(_OPERATIONS, min_size=4, max_size=40),
)
async def test_a_stopped_arm_only_leaves_for_a_target_entered_with_nothing_holding(
    occupancy: Occupancy, operations: list[tuple[str, int]]
) -> None:
    """The acceptance criterion, as a property: whatever happens, in whatever order.

    Four statements, checked at every command and after every tick:

    * no non-zero target is accepted over a stopped arm while a verdict
      stands or the heart rate holds a rise;
    * THE INVARIANT: whenever the arm is at zero and anything (a verdict, or
      the heart rate) would hold a rise, the target is zero;
    * the setpoint never leaves zero for a target that was entered while
      something held a rise;
    * and, knowing nothing of WHY a rise might be held: no target ever spends
      more than one tick over a stopped arm. One tick is the motion profiler
      taking its time base; a second one would be a wait.

    On ``develop`` the first already fails on the simplest sequence (a
    warning, then a target); on ``da5efa9`` it fails on a gap in the heart
    rate, then a target.
    """
    deaf = _Deaf()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeDrive, "write_speed", _deafened(deaf))
        session = await _session(occupancy, deaf)
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
                case "pulse":
                    session.drift = value
                case "presence":
                    session.ping = bool(value)
                case "drive":
                    deaf.on = bool(value)
                case "stalled tick":
                    await session.tick(late=True)
                case "trip":
                    rig.runtime.trip_from_thread("rig_trip", SafetyAction(value), "under test")
                case _:
                    rig.runtime.acknowledge(OPERATOR)
        for _ in range(100):
            await session.tick()


# =========================================================================
# UNCHANGED ON PURPOSE: passes on develop, on da5efa9 and here
# =========================================================================


async def test_a_first_target_on_a_steady_heart_rate_is_taken_and_followed_at_once() -> None:
    """A normal start with a person on board: nothing to wait for, nothing refused.

    On the console's own limits, the ECG running before the session as it
    does on the machine, the rate steady. The first target is accepted and
    the first non-zero setpoint is written within two ticks; the arm reaches
    the target; no verdict, no withdrawal.
    """
    rig = await _on_the_console_s_limits(72)
    first = len(rig.snapshots)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    assert _moved(await rig.run(0.4)), "the first target of a normal session did not start the arm"
    await rig.run(40.0)
    assert _applied(rig) == 200
    assert _taken_back(rig) is None
    assert not _verdicts(rig.snapshots[first:])


async def test_an_empty_capsule_has_no_heart_rate_to_wait_for() -> None:
    """Nobody on board: no reading has ever been fed, and a target is taken and followed."""
    rig = _rig(motion=DEFAULT_MOTION_LIMITS)
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MANUAL_CEILING))
    await rig.run(10.0, feed=False)
    assert _ask(rig, 200) == Ok(MotorRpm(200))
    await rig.run(40.0, feed=False)
    assert _applied(rig) == 200
    assert _taken_back(rig) is None


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


async def test_a_rise_typed_during_a_gap_in_the_heart_rate_over_a_turning_arm_waits() -> None:
    """The same gap as above, over an arm that TURNS: held, kept, and resumed, as before.

    Six seconds without a reading and no verdict yet. 250 rpm is typed over
    an arm at 200: accepted. Nothing rises without a usable heart rate, so
    the setpoint stays at 200 and the target stays 250; the reading returns
    and the arm goes to 250. Nothing is refused or taken back from an arm
    that is turning.
    """
    rig = await _manual(Occupancy.OCCUPIED, target=200)
    await rig.run(6.0, feed=False)
    assert _standing(rig) is None

    assert _ask(rig, 250) == Ok(MotorRpm(250))
    held = await rig.run(2.0, feed=False)
    assert not _verdicts(held), "a verdict stood: this is not the case under test"
    assert _setpoints(held) == {200}, "the heart rate did not hold the rise"
    assert (_target(rig), _shown_target(rig)) == (250, 250)
    assert _taken_back(rig) is None

    await rig.run(40.0)
    assert _applied(rig) == 250, "the rise held over a turning arm was not resumed"
    assert _end(rig) is None


async def test_a_rise_a_falling_heart_rate_holds_over_a_turning_arm_waits_and_resumes() -> None:
    """The gate closes on a rise under way: the setpoint stops where it is, then goes on.

    The falling reading of :data:`CLOSINGS`, arriving between a target and its
    first step, over an arm at 200 rpm. At standstill that target is taken
    back; here it is kept, the setpoint holds at 200 while the trend falls,
    and the arm goes on to 250 when the rate has settled, with no verdict at
    any time.
    """
    closing = CLOSINGS["a falling rate"]
    rig = await _manual(Occupancy.OCCUPIED, target=200)
    assert _ask(rig, 250) == Ok(MotorRpm(250))
    for bpm in closing.readings:
        rig.feed(Bpm(bpm))
    rig.fed_bpm = Bpm(closing.readings[-1])

    held = await rig.run(1.0, feed=False)
    assert _setpoints(held) == {200}, "the heart rate did not hold the rise"
    assert _target(rig) == 250, "the target was taken back from a turning arm"
    assert _taken_back(rig) is None

    resumed = await rig.run(60.0)
    assert not _verdicts(held + resumed), "a verdict stood: this is not the case under test"
    assert _applied(rig) == 250, "the rise held over a turning arm was not resumed"
    assert _end(rig) is None


async def test_a_rise_the_drive_does_not_acknowledge_over_a_turning_arm_is_asked_again() -> None:
    """The deaf drive, over an arm that TURNS: the rise is asked again every tick, as before."""
    deaf = _Deaf()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeDrive, "write_speed", _deafened(deaf))
        rig = await _manual(Occupancy.BENCH, target=200)
        deaf.on = True
        assert _ask(rig, 250) == Ok(MotorRpm(250))
        refusing = await rig.run(5.0)
        assert not _verdicts(refusing), "a rise that is not acknowledged became a verdict"
        assert _setpoints(refusing) == {200}
        assert len(deaf.refused) > 1, "the rise was not asked again"
        assert _target(rig) == 250, "the target was taken back from a turning arm"
        assert _taken_back(rig) is None

        deaf.on = False
        await rig.run(40.0)
        assert _applied(rig) == 250
        assert _end(rig) is None


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


async def test_a_target_of_zero_is_taken_whatever_the_heart_rate_does() -> None:
    """Zero is never refused by the heart rate either: stopped, and while turning."""
    stopped = await _not_moved_yet(Occupancy.OCCUPIED)
    await stopped.run(6.0, feed=False)
    assert _standing(stopped) is None
    assert _ask(stopped, 0) == Ok(MotorRpm(0))
    assert _taken_back(stopped) is None

    turning = await _manual(Occupancy.OCCUPIED, target=200)
    await turning.run(6.0, feed=False)
    assert _standing(turning) is None
    assert _ask(turning, 0) == Ok(MotorRpm(0))
    lowered = await turning.run(2.0, feed=False)
    assert _applied(turning) < 200, "a descent waited for the heart rate"
    assert not _verdicts(lowered)


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
