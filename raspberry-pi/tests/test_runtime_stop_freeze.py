"""A FREEZE holds a speed, never a stop: a stop somebody asked for always comes down (ANH-175).

The defect, measured on ``develop`` on this same rig (fake drive, manual clock,
never the hardware): with a FREEZE standing, an operator's STOP was recorded,
the screen said ARRET, and the setpoint did not move. 165 motor rpm held for
17 s under ``hr_stale`` until its own REDUCE; held without limit under a
latched ``loop_stall``; a manual target of zero accepted and without effect.
Only the emergency stop acted.

Now a stop that has been asked for walks the setpoint to zero at the motion
limits from the next tick, whatever FREEZE stands, and a FREEZE that appears
during the walk does not pause it. Asked for means: an operator's STOP (at the
console or from the site, both are ``request_stop``), any ending already
begun, a manual target of zero.

Four groups of tests, and the split matters when reading a failure:

* **the defect**: these fail on ``develop`` (the setpoint is held) and pass
  here. One of them is about the ENDING rather than the descent, and for the
  FREEZEs that become a REDUCE or a RAMP_DOWN by themselves it held on
  ``develop`` too, once the warning had brought the arm down;
* **the walk itself**: what the descent may and may not do, whatever arm of
  the verdict ran on the tick before it. There is no such walk on
  ``develop``, so these fail there as well;
* **unchanged on purpose**: with no stop asked for a FREEZE holds exactly as
  before, and every stronger verdict still decides first. These pass on both.
  (One more thing brings the setpoint down under a FREEZE since: a programme
  reaching its own cooldown, ``tests/test_runtime_cooldown_freeze.py``);
* **what the screen is told**: no ramp and no arrival time over a setpoint
  that is held.

The matrix is every source of a FREEZE the supervisor has (``hr_stale``,
``attendant_absent``, a latched ``loop_stall``, a trip from a thread) against a
programme, a manual session with an empty capsule and one with a person
declared on board. One pair is absent because it does not exist: with nobody
on board the heart-rate rules are off, so ``hr_stale`` never freezes a
``bench`` session (pinned below).
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import Final, Literal, assert_never

import pytest

from src.local_panel import RUNTIME_LIMITS
from src.result import Err, Ok, is_ok
from src.training import runtime as runtime_module
from src.training.motion import DEFAULT_MOTION_LIMITS, MotionLimits, motor_rate_limit, ramp_duration
from src.training.plan import MIN_RECOVERY_S, TrainingProfile
from src.training.runtime import EndReason, RuntimeState, SafetyStanding
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_HR_STALE,
    RULE_LOOP_STALL,
    RULE_SESSION_STANDSTILL,
    SafetySupervisor,
)
from src.training.types import Occupancy, Phase, RunMode, SafetyAction, TelemetrySnapshot
from src.units import Bpm, MotorRpm, OutputRpm, Seconds, motor_to_output_rpm
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    REAL_PROFILE,
    RIG_MOTION,
    TICK,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _profile,  # pyright: ignore[reportPrivateUsage]
    _running_rig,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_session_overrun import (
    DEADLINE,
    SHORT_LIMIT,
    TOTAL,
    _a_new_programme_is_accepted,  # pyright: ignore[reportPrivateUsage]  # the rest after a session
    _at_rest,  # pyright: ignore[reportPrivateUsage]
    _observations,  # pyright: ignore[reportPrivateUsage]
    _overrun,  # pyright: ignore[reportPrivateUsage]
    _programme,  # pyright: ignore[reportPrivateUsage]
    _since_start,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _console_rig,  # pyright: ignore[reportPrivateUsage]  # the shipped programme's rig
    _manual,  # pyright: ignore[reportPrivateUsage]
)

Mode = Literal["programme", "bench", "occupied"]
Source = Literal["hr_stale", "attendant_absent", "loop_stall", "thread"]

THREAD_RULE: Final[str] = "rig_freeze"
"""The rule a test trips from a thread, as the acquisition thread would."""

RULES: Final[dict[Source, tuple[str, bool]]] = {
    "hr_stale": (RULE_HR_STALE, False),
    "attendant_absent": (RULE_ATTENDANT_ABSENT, False),
    "loop_stall": (RULE_LOOP_STALL, True),
    "thread": (THREAD_RULE, True),
}
"""Each source of a FREEZE: the rule that stands, and whether it is latched."""

CASES: Final[tuple[tuple[Mode, Source], ...]] = (
    ("programme", "hr_stale"),
    ("programme", "attendant_absent"),
    ("programme", "loop_stall"),
    ("programme", "thread"),
    ("bench", "attendant_absent"),
    ("bench", "loop_stall"),
    ("bench", "thread"),
    ("occupied", "hr_stale"),
    ("occupied", "attendant_absent"),
    ("occupied", "loop_stall"),
    ("occupied", "thread"),
)
"""Every FREEZE a session can be under. ``bench`` has no ``hr_stale``: see the module docstring."""

MANUAL_CASES: Final[tuple[tuple[Mode, Source], ...]] = tuple(
    case for case in CASES if case[0] != "programme"
)

LONG_HOLD: Final[TrainingProfile] = _profile(total_duration_s=Seconds(600.0))
"""The accelerated rig's profile with a HOLD of several minutes: room for a 60 s absence."""

MANUAL_SPEED: Final[int] = 200
"""Motor rpm of the manual sessions here: 4 output rpm, a walk of 12 s down to zero."""

AT_CEILING: Final[float] = 420.0
"""Seconds into the shipped programme at which the setpoint is parked at its WARMUP ceiling."""


@dataclass(frozen=True, slots=True)
class Standing:
    """A FREEZE that stands, and how the rig must be ticked for it to go on standing."""

    rule: str
    latched: bool
    feed: bool
    ping: bool


# =========================================================================
# Reading the runtime through calls (see ``Rig.phase`` in test_runtime.py)
# =========================================================================


def _applied(rig: Rig) -> int:
    return int(rig.runtime.applied_rpm)


def _verdict(rig: Rig) -> tuple[str, SafetyAction, bool] | None:
    """The standing verdict as ``(rule, action, latched)``, or ``None``."""
    verdict = rig.runtime.standing
    return None if verdict is None else (verdict.rule, verdict.action, verdict.latched)


def _end(rig: Rig) -> EndReason | None:
    return rig.runtime.end_reason


def _mode(rig: Rig) -> RunMode:
    return rig.runtime.mode


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _rules_shown(snapshots: list[TelemetrySnapshot]) -> set[str]:
    return {snapshot.safety.rule for snapshot in snapshots if snapshot.safety is not None}


@dataclass(frozen=True, slots=True)
class Walk:
    """What bounds an ordinary stop on a rig: its motion limits, and a programme's last step."""

    motion: MotionLimits
    passage_slew: float | None
    """The slew a programme's step across ``(0, min_run)`` waits for; ``None`` in manual."""


def _walk_of(mode: Mode) -> Walk:
    """The accelerated rig for a programme, the machine's own limits for a manual session."""
    if mode == "programme":
        return Walk(motion=RIG_MOTION, passage_slew=float(LIMITS.slew))
    return Walk(motion=DEFAULT_MOTION_LIMITS, passage_slew=None)


SHIPPED: Final[Walk] = Walk(motion=DEFAULT_MOTION_LIMITS, passage_slew=float(RUNTIME_LIMITS.slew))
"""The shipped programme on the console's limits (the rig of ``_console_rig``)."""


def _zero() -> OutputRpm:
    return OutputRpm(0.0)


def _out(rpm: int) -> OutputRpm:
    return motor_to_output_rpm(MotorRpm(rpm), GEOMETRY.ratio)


# =========================================================================
# Rigs
# =========================================================================


async def _turning(mode: Mode) -> Rig:
    """A session of ``mode`` with the arm turning and no verdict standing."""
    rig: Rig
    match mode:
        case "programme":
            rig = await _running_rig(profile=LONG_HOLD)
            await rig.run(4.0)
        case "bench":
            rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
        case "occupied":
            rig = await _manual(Occupancy.OCCUPIED, MANUAL_SPEED)
        case _ as unreachable:
            assert_never(unreachable)
    assert _applied(rig) > 0, "the arm is not turning"
    assert _verdict(rig) is None
    return rig


async def _frozen(rig: Rig, source: Source) -> Standing:
    """Make ``source`` freeze the session, and say how to tick it from here."""
    rule, latched = RULES[source]
    feed, ping = True, True
    match source:
        case "hr_stale":
            feed = False
            await rig.run(12.0, feed=False)
        case "attendant_absent":
            ping = False
            await rig.run(62.0, ping=False)
        case "loop_stall":
            rig.clock.advance(Seconds(1.0))
            await rig.step()
        case "thread":
            rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
            await rig.step()
        case _ as unreachable:
            assert_never(unreachable)
    assert _verdict(rig) == (rule, SafetyAction.FREEZE, latched)
    assert _applied(rig) > 0, "the arm stopped before the stop was asked for"
    return Standing(rule=rule, latched=latched, feed=feed, ping=ping)


async def _walk_down(
    rig: Rig, standing: Standing, *, limit: float = 60.0
) -> list[TelemetrySnapshot]:
    """Tick under ``standing`` until the setpoint is zero. Fails if it never gets there."""
    seen: list[TelemetrySnapshot] = []
    for _ in range(round(limit / TICK)):
        if _applied(rig) == 0:
            return seen
        seen.append(await rig.step(feed=standing.feed, ping=standing.ping))
    assert _applied(rig) == 0, f"the setpoint never reached zero: held at {_applied(rig)}"
    return seen


def _assert_an_ordinary_walk(start: int, down: list[TelemetrySnapshot], walk: Walk) -> None:
    """``down`` is the ordinary stop's walk from ``start``: at once, never up, never too fast.

    * the very first tick lowers the setpoint;
    * no tick raises it, and none lowers it by more than the motion limits
      pay for since the setpoint last moved (plus the one carried rpm), the
      passage between the minimum running speed and zero apart;
    * a programme's passage waits for its slew to have paid for it;
    * it reaches zero in the time the motion profiler gives for that walk
      (plus that wait), no later.
    """
    motion = walk.motion
    minimum = int(motion.min_run)
    setpoints = [start, *_setpoints(down)]
    assert setpoints[1] < start, f"the setpoint did not move on the tick after the stop: {start}"
    last_change = 0
    for index, (before, after) in enumerate(pairwise(setpoints), start=1):
        assert after <= before, f"the setpoint rose from {before} to {after}"
        assert after == 0 or after >= minimum, f"{after} rpm is outside the domain"
        if after == before:
            continue
        if (before, after) != (minimum, 0):
            rate = motor_rate_limit(MotorRpm(after), motion, GEOMETRY)
            window = float(index - last_change) * TICK
            assert before - after <= rate * window + 1 + 1e-9, (before, after, window)
        last_change = index
    assert setpoints[-1] == 0
    expected = ramp_duration(MotorRpm(start), MotorRpm(0), motion, GEOMETRY)
    assert expected is not None
    passage = 0.0
    if walk.passage_slew is not None:
        passage = float(minimum) / walk.passage_slew
        parked = setpoints.count(minimum) * TICK
        assert parked >= passage - TICK, f"the last step was taken after {parked} s at {minimum}"
    assert len(down) * TICK <= expected + passage + 2 * TICK, (len(down) * TICK, expected)


# =========================================================================
# THE DEFECT: fails on develop, passes here
# =========================================================================


@pytest.mark.parametrize(("mode", "source"), CASES)
async def test_a_stop_under_a_freeze_lowers_the_setpoint_on_the_next_tick_and_reaches_zero(
    mode: Mode, source: Source
) -> None:
    """EX-1, EX-4: STOP under every FREEZE, in every kind of session.

    On ``develop`` the tick after the stop left the setpoint where it was, and
    under the two latched sources it stayed there until an emergency stop.
    """
    rig = await _turning(mode)
    standing = await _frozen(rig, source)
    before = _applied(rig)

    rig.runtime.request_stop("operator: stop button")
    first = await rig.step(feed=standing.feed, ping=standing.ping)
    assert first.safety_action is SafetyAction.FREEZE, "the stop was not judged under the FREEZE"
    assert first.setpoint.motor_rpm < before, "STOP under a FREEZE left the setpoint where it was"
    assert first.mode is RunMode.ARRET

    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(before, down, _walk_of(mode))
    assert {snapshot.safety_action for snapshot in down} == {SafetyAction.FREEZE}, (
        "something other than the FREEZE brought the setpoint down"
    )
    assert _rules_shown(down) == {standing.rule}
    assert _end(rig) is EndReason.OPERATOR_STOP


@pytest.mark.parametrize(("mode", "source"), CASES)
async def test_a_stop_under_a_freeze_ends_the_session_as_an_operator_stop_and_latches_nothing(
    mode: Mode, source: Source
) -> None:
    """The zero is the operator's: the ending keeps its cause, and no standstill is latched.

    The interplay with ANH-176: the setpoint comes back to zero under a
    standing warning, which is what ``session_standstill`` exists to catch,
    but this session was already ending, by request. So nothing new is
    latched; a FREEZE that was latched before the stop still is, and it is the
    only thing left to acknowledge.
    """
    rig = await _turning(mode)
    standing = await _frozen(rig, source)
    rig.fed_bpm = Bpm(82)  # the rig's resting rate: no fall for hr_drop once the load is gone
    rig.runtime.request_stop("operator: stop button")
    await _walk_down(rig, standing)
    rest = await rig.run(float(MIN_RECOVERY_S) + 70.0)

    assert _applied(rig) == 0
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    assert RULE_SESSION_STANDSTILL not in _rules_shown(rig.snapshots)
    assert set(_setpoints(rest)) == {0}
    if standing.latched:
        assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)
        acknowledged = rig.runtime.acknowledge(OPERATOR)
        assert isinstance(acknowledged, Ok)
        assert acknowledged.value.cleared == (standing.rule,)
    assert _verdict(rig) is None, (
        "an operator stop under a warning that lifts needs no acknowledgement"
    )


@pytest.mark.parametrize(("mode", "source"), MANUAL_CASES)
async def test_a_manual_target_of_zero_under_a_freeze_is_followed_on_the_next_tick(
    mode: Mode, source: Source
) -> None:
    """EX-1, EX-4: the target was accepted and without effect; now the setpoint follows it.

    The session is not ending while it walks down: the mode is still MANUEL,
    and the walk is the one a target of zero takes with no verdict standing.
    """
    rig = await _turning(mode)
    standing = await _frozen(rig, source)
    before = _applied(rig)

    assert rig.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
    first = await rig.step(feed=standing.feed, ping=standing.ping)
    assert first.safety_action is SafetyAction.FREEZE
    assert first.setpoint.motor_rpm < before, "a target of zero under a FREEZE moved nothing"
    assert first.mode is RunMode.MANUEL

    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(before, down, _walk_of(mode))
    assert {snapshot.safety_action for snapshot in down} == {SafetyAction.FREEZE}
    assert {snapshot.mode for snapshot in down} == {RunMode.MANUEL}
    assert _end(rig) is None


@pytest.mark.parametrize("occupancy", [Occupancy.BENCH, Occupancy.OCCUPIED])
async def test_a_zero_reached_by_a_manual_target_under_a_freeze_ends_the_session_and_says_whose(
    occupancy: Occupancy,
) -> None:
    """The interplay with ANH-176, pinned so that it is a choice and not an accident.

    A manual target of zero ends nothing when NO warning stands. Followed
    while a warning stands, the zero ends the session on the latched
    ``session_standstill``, as a zero typed under a REDUCE already did: one
    rule, for one acknowledgement. What differs is what the verdict says. A
    FREEZE lowers nothing, so this zero cannot be the warning's: it names the
    operator's target, and the warning it was followed under.
    """
    rig = await _manual(occupancy, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    assert rig.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
    await _walk_down(rig, standing)
    assert _end(rig) is None, "the tick that writes the zero only states the fact"

    await rig.step()
    assert _verdict(rig) == (RULE_SESSION_STANDSTILL, SafetyAction.RAMP_DOWN, True)
    reached = rig.runtime.standing
    assert reached is not None
    assert (
        f"the operator's manual target of zero, followed under the warning {THREAD_RULE}, "
        "brought the setpoint to zero"
    ) in reached.detail
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert _mode(rig) is RunMode.ARRET

    after = await rig.run(float(MIN_RECOVERY_S) + 30.0)
    assert set(_setpoints(after)) == {0}
    assert rig.state() is RuntimeState.FINISHED
    again = await rig.runtime.start_manual(occupancy, OPERATOR, MotorRpm(300))
    assert isinstance(again, Err)
    assert isinstance(again.error, SafetyStanding)
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    assert _verdict(rig) is None
    assert is_ok(await rig.runtime.start_manual(occupancy, OPERATOR, MotorRpm(300)))
    assert rig.runtime.manual_target == 0


@pytest.mark.parametrize("mode", ["programme", "bench", "occupied"])
async def test_a_freeze_that_appears_during_a_stop_does_not_pause_the_descent(mode: Mode) -> None:
    """EX-2: the descent begun with no verdict goes on, tick for tick, when a FREEZE arrives.

    On ``develop`` the ticket's fifth sequence: 300 motor rpm, STOP, down to
    226, then held at 226 for some 18 s when ``hr_stale`` froze, until its
    REDUCE. Here the walk is compared with the same walk on a twin rig that no
    FREEZE ever touches: the two sequences of setpoints are the same.
    """
    witness = await _turning(mode)
    rig = await _turning(mode)
    before = _applied(rig)
    for twin in (witness, rig):
        twin.runtime.request_stop("operator: stop button")
        await twin.run(1.0)
    assert 0 < _applied(rig) < before, "the descent was not under way"
    assert _applied(rig) == _applied(witness)

    rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
    under = await _walk_down(rig, Standing(THREAD_RULE, latched=True, feed=True, ping=True))
    free = await _walk_down(witness, Standing("", latched=False, feed=True, ping=True))
    assert {snapshot.safety_action for snapshot in under} == {SafetyAction.FREEZE}
    assert _rules_shown(free) == set()
    assert _setpoints(under) == _setpoints(free), "the FREEZE changed the descent"
    assert _end(rig) is EndReason.OPERATOR_STOP


@pytest.mark.parametrize("occupancy", [Occupancy.BENCH, Occupancy.OCCUPIED])
async def test_a_freeze_that_appears_while_a_target_of_zero_is_followed_does_not_pause_it(
    occupancy: Occupancy,
) -> None:
    """EX-2, the other request: a manual target of zero, then the FREEZE."""
    witness = await _manual(occupancy, MANUAL_SPEED)
    rig = await _manual(occupancy, MANUAL_SPEED)
    for twin in (witness, rig):
        assert twin.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
        await twin.run(3.0)
    assert 0 < _applied(rig) < MANUAL_SPEED
    assert _applied(rig) == _applied(witness)

    rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
    under = await _walk_down(rig, Standing(THREAD_RULE, latched=True, feed=True, ping=True))
    free = await _walk_down(witness, Standing("", latched=False, feed=True, ping=True))
    assert {snapshot.safety_action for snapshot in under} == {SafetyAction.FREEZE}
    assert _setpoints(under) == _setpoints(free), "the FREEZE changed the descent"


async def test_the_ticket_s_fifth_sequence_no_longer_stalls_when_hr_stale_freezes() -> None:
    """Person on board at 300 motor rpm, STOP three seconds after the heart rate is lost.

    Measured on ``develop``: 300 down to 226 in seven seconds, then held at
    226 from the FREEZE of ``hr_stale`` at 10 s to its REDUCE at 30 s, zero
    40.2 s after the stop. Here the walk never pauses: zero after 20 s.
    """
    rig = await _manual(Occupancy.OCCUPIED, 300)
    await rig.run(3.0, feed=False)
    assert _verdict(rig) is None
    rig.runtime.request_stop("operator: stop button")
    down = await _walk_down(rig, Standing(RULE_HR_STALE, latched=False, feed=False, ping=True))
    _assert_an_ordinary_walk(300, down, _walk_of("occupied"))
    frozen = [snapshot for snapshot in down if snapshot.safety_action is SafetyAction.FREEZE]
    assert len(frozen) > 25, "the FREEZE of hr_stale never stood during the descent"
    assert all(after < before for before, after in pairwise(_setpoints(frozen)[::5])), (
        "the descent paused under the FREEZE"
    )
    assert len(down) * TICK < 21.0
    assert _end(rig) is EndReason.OPERATOR_STOP


# =========================================================================
# THE WALK ITSELF
# =========================================================================


async def test_on_the_shipped_programme_a_stop_under_a_freeze_is_the_ordinary_stop() -> None:
    """EX-1, "the usual software ramp", on the machine's own numbers.

    The shipped 30-min programme, the console's limits and the shipped motion
    limits, the setpoint parked at its WARMUP ceiling. One rig is stopped with
    nothing standing; its twin is frozen first (a latched trip from a thread),
    then stopped. Same setpoints, tick for tick, down to zero.
    """
    witness = await _console_rig(AT_CEILING)
    rig = await _console_rig(AT_CEILING)
    rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
    await rig.step()
    await witness.step()
    start = _applied(rig)
    assert start == _applied(witness)
    assert _verdict(rig) == (THREAD_RULE, SafetyAction.FREEZE, True)
    assert _verdict(witness) is None

    for twin in (witness, rig):
        twin.runtime.request_stop("operator: stop button")
    under = await _walk_down(rig, Standing(THREAD_RULE, latched=True, feed=True, ping=True))
    free = await _walk_down(witness, Standing("", latched=False, feed=True, ping=True))
    assert {snapshot.safety_action for snapshot in under} == {SafetyAction.FREEZE}
    assert _rules_shown(free) == set()
    assert _setpoints(under) == _setpoints(free)
    _assert_an_ordinary_walk(start, under, SHIPPED)
    rate = motor_rate_limit(MotorRpm(0), DEFAULT_MOTION_LIMITS, GEOMETRY)
    minimum = int(REAL_PROFILE.min_run_rpm)
    reached = (_setpoints(under).index(minimum) + 1) * TICK
    assert reached >= (start - minimum) / rate, "faster than the anti-nausea limits allow"
    assert RUNTIME_LIMITS.slew > rate, "the premise: the controller's own ramp is not what binds"


async def test_a_stop_after_a_long_freeze_spends_one_tick_of_motion_not_the_hold() -> None:
    """The hold banks nothing: twenty seconds frozen, and the first step is one tick's worth."""
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    held = await rig.run(20.0)
    assert set(_setpoints(held)) == {MANUAL_SPEED}

    rig.runtime.request_stop("operator: stop button")
    first = await rig.step()
    step = MANUAL_SPEED - int(first.setpoint.motor_rpm)
    rate = motor_rate_limit(MotorRpm(MANUAL_SPEED), DEFAULT_MOTION_LIMITS, GEOMETRY)
    assert 1 <= step <= rate * TICK + 1, f"a first step of {step} rpm after a 20 s hold"
    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(MANUAL_SPEED, down, _walk_of("bench"))


async def test_a_stop_meeting_a_freeze_after_a_reduce_does_not_spend_the_reduce() -> None:
    """A REDUCE on a programme walks its own ramp and leaves the profiler's time base behind.

    One second of REDUCE, a STOP, and the REDUCE gives way to a FREEZE. The
    profiler's base dates from before the REDUCE: followed blindly it pays for
    a second of motion at once (a first step of 69 rpm on this rig, measured
    with the guard taken out) where one tick pays for 14. The walk under the
    FREEZE starts from the previous tick instead.
    """
    rig = await _running_rig(profile=LONG_HOLD)
    await rig.run(30.0)
    assert _applied(rig) == LONG_HOLD.max_rpm, "the setpoint is not parked at the ceiling"
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.REDUCE
        lowered = await rig.run(1.0)
        assert lowered[-1].setpoint.motor_rpm < lowered[0].setpoint.motor_rpm
        before = _applied(rig)
        assert before > 150, "no room left to tell one tick from one second"

        # The imposed verdict decides the command; the snapshot goes on showing
        # the supervisor's own, so the FREEZE is not read back from it here.
        rig.runtime.request_stop("operator: stop button")
        imposed.action = SafetyAction.FREEZE
        first = await rig.step()
        step = before - int(first.setpoint.motor_rpm)
        rate = motor_rate_limit(MotorRpm(before), RIG_MOTION, GEOMETRY)
        assert 1 <= step <= rate * TICK + 1, f"a first step of {step} rpm"
        down = [first, *await _walk_down(rig, Standing("rig_imposed", False, feed=True, ping=True))]
    _assert_an_ordinary_walk(before, down, _walk_of("programme"))
    assert _end(rig) is EndReason.OPERATOR_STOP


@pytest.mark.parametrize("mode", ["programme", "bench", "occupied"])
async def test_a_freeze_that_lifts_during_the_walk_hands_it_over_without_a_pause_or_a_jump(
    mode: Mode,
) -> None:
    """The other way round from EX-2: the walk begins under a FREEZE, which then lifts.

    The ordinary stop takes over where the walk under the FREEZE left the
    setpoint: the whole descent, seen from end to end, is still one ordinary
    walk. Nothing rises when the verdict goes, and the session still ends as
    the operator's stop.
    """
    rig = await _turning(mode)
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.FREEZE
        held = await rig.run(2.0)
        before = _applied(rig)
        assert set(_setpoints(held)) == {before}

        rig.runtime.request_stop("operator: stop button")
        down = await rig.run(1.0)
        assert 0 < _applied(rig) < before
        imposed.action = None
        down += await _walk_down(rig, Standing("", latched=False, feed=True, ping=True))
    _assert_an_ordinary_walk(before, down, _walk_of(mode))
    assert _end(rig) is EndReason.OPERATOR_STOP


async def test_an_ending_acknowledged_during_its_descent_keeps_descending_under_a_freeze() -> None:
    """Any ending already begun is a stop asked for, not only the operator's.

    A RAMP_DOWN tripped from a thread ends the session and starts the descent;
    the operator acknowledges it before the arm has stopped, while the FREEZE
    of ``hr_stale`` stands underneath. On ``develop`` the FREEZE then held the
    setpoint of a session that was over, until its own REDUCE.
    """
    rig = await _turning("programme")
    standing = await _frozen(rig, "hr_stale")
    rig.runtime.trip_from_thread("rig_ramp_down", SafetyAction.RAMP_DOWN, "under test")
    await rig.step(feed=False)
    assert _end(rig) is EndReason.SAFETY_VERDICT
    before = _applied(rig)
    assert before > 0

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    first = await rig.step(feed=False)
    assert first.safety_action is SafetyAction.FREEZE
    assert first.setpoint.motor_rpm < before, (
        "the FREEZE held the setpoint of a session that is over"
    )
    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(before, down, _walk_of("programme"))
    assert _end(rig) is EndReason.SAFETY_VERDICT


async def test_a_new_target_typed_during_the_walk_to_zero_is_held_again_by_the_freeze() -> None:
    """Only zero is a stop. The operator takes the zero back: the FREEZE holds where the arm is."""
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    assert rig.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
    await rig.run(3.0)
    reached = _applied(rig)
    assert 0 < reached < MANUAL_SPEED

    assert rig.runtime.set_manual_target(_out(MANUAL_SPEED)) == Ok(MotorRpm(MANUAL_SPEED))
    held = await rig.run(5.0, feed=standing.feed, ping=standing.ping)
    assert set(_setpoints(held)) == {reached}
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING


@pytest.mark.parametrize("mode", ["programme", "bench", "occupied"])
@pytest.mark.parametrize(
    "action",
    [SafetyAction.REDUCE, SafetyAction.RAMP_DOWN, SafetyAction.QUICK_STOP, SafetyAction.GO_SILENT],
)
async def test_a_stronger_verdict_takes_over_the_walk_under_a_freeze_as_it_takes_over_any_stop(
    mode: Mode, action: SafetyAction
) -> None:
    """EX-3, during the walk: a stronger verdict finds it exactly as it finds an ordinary stop.

    Two twins are stopped and walk down for a second; one of them was frozen
    first, so its walk is the one this change adds. Both then get the stronger
    verdict on the same tick. From there: the same verdict in force, the same
    setpoints, and the same frames on the wire. (The same comparison with the
    verdict arriving on the tick of the stop is further down, with what did
    not change: that one holds on ``develop`` too.)
    """
    witness = await _turning(mode)
    rig = await _turning(mode)
    await _frozen(rig, "thread")
    await witness.step()
    for twin in (witness, rig):
        twin.fed_bpm = Bpm(82)  # the rig's resting rate: no hr_drop once the load is gone
        twin.runtime.request_stop("operator: stop button")
        await twin.run(1.0)
    assert 0 < _applied(rig) == _applied(witness)

    for twin in (witness, rig):
        twin.runtime.trip_from_thread("rig_stronger", action, "under test")
        twin.drive.trace.clear()
    under = await rig.run(20.0)
    free = await witness.run(20.0)
    assert {snapshot.safety_action for snapshot in under} == {action}
    assert {snapshot.safety_action for snapshot in free} == {action}
    assert _setpoints(under) == _setpoints(free)
    assert rig.drive.trace == witness.drive.trace
    if action is SafetyAction.GO_SILENT:
        assert rig.runtime.silent
    else:
        assert _applied(rig) == 0
    if action is SafetyAction.QUICK_STOP:
        assert under[0].setpoint.motor_rpm == 0, "QUICK_STOP waited for a ramp"


# =========================================================================
# UNCHANGED ON PURPOSE (EX-3): passes on develop and here
# =========================================================================


@pytest.mark.parametrize(("mode", "source"), CASES)
async def test_with_no_stop_asked_for_a_freeze_still_holds_the_setpoint(
    mode: Mode, source: Source
) -> None:
    rig = await _turning(mode)
    standing = await _frozen(rig, source)
    before = _applied(rig)
    held = await rig.run(4.0, feed=standing.feed, ping=standing.ping)
    assert set(_setpoints(held)) == {before}
    assert {snapshot.safety_action for snapshot in held} == {SafetyAction.FREEZE}
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING


async def test_with_nobody_on_board_a_lost_heart_rate_freezes_nothing() -> None:
    """Why the matrix has no ``bench`` under ``hr_stale``: the heart-rate rules are off there."""
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    lost = await rig.run(40.0, feed=False)
    assert _rules_shown(lost) == set()
    assert _applied(rig) == MANUAL_SPEED


async def test_a_lower_manual_target_that_is_not_zero_is_still_held_by_a_freeze() -> None:
    """A slower speed is not a stop: under a FREEZE the setpoint stays where it is."""
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    assert rig.runtime.set_manual_target(_out(100)) == Ok(MotorRpm(100))
    held = await rig.run(5.0, feed=standing.feed, ping=standing.ping)
    assert set(_setpoints(held)) == {MANUAL_SPEED}

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    await rig.run(15.0)
    assert _applied(rig) == 100, "the target was not followed once the FREEZE was gone"


@pytest.mark.parametrize("mode", ["programme", "bench", "occupied"])
@pytest.mark.parametrize(
    "action",
    [SafetyAction.REDUCE, SafetyAction.RAMP_DOWN, SafetyAction.QUICK_STOP, SafetyAction.GO_SILENT],
)
async def test_a_stronger_verdict_that_arrives_with_the_stop_decides_exactly_as_before(
    mode: Mode, action: SafetyAction
) -> None:
    """EX-3: REDUCE and everything above it still decide first, stop or no stop.

    Two frozen twins get the stronger verdict on the same tick; one of them
    has a STOP pending on that tick as well. The stronger verdict's own arm
    runs on both, the walk this change adds on neither: same setpoints, tick
    for tick.
    """
    witness = await _turning(mode)
    rig = await _turning(mode)
    for twin in (witness, rig):
        await _frozen(twin, "thread")
        twin.fed_bpm = Bpm(82)  # the rig's resting rate: no hr_drop once the load is gone
    assert _applied(rig) == _applied(witness)

    rig.runtime.request_stop("operator: stop button")
    for twin in (witness, rig):
        twin.runtime.trip_from_thread("rig_stronger", action, "under test")
    under = await rig.run(20.0)
    free = await witness.run(20.0)
    assert under[0].safety_action is action
    assert free[0].safety_action is action
    assert _setpoints(under) == _setpoints(free)
    assert rig.runtime.silent == (action is SafetyAction.GO_SILENT)
    if action is SafetyAction.QUICK_STOP:
        assert under[0].setpoint.motor_rpm == 0, "QUICK_STOP waited for a ramp"


# =========================================================================
# WHAT THE SCREEN IS TOLD (EX-5)
# =========================================================================


async def test_no_ramp_and_no_arrival_time_are_announced_for_a_target_a_freeze_holds() -> None:
    """The banner said "ramp in progress, arrival in ..." over a speed that did not move.

    A target of 300 is being climbed to when the FREEZE arrives at some 130
    motor rpm. Nothing walks towards 300 any more: ``ramping`` is false and
    there is no arrival time, while target and setpoint still differ, which is
    how a page tells "held" from "reached". Once the FREEZE is acknowledged
    the ramp and its arrival time are back.
    """
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    assert rig.runtime.set_manual_target(_out(300)) == Ok(MotorRpm(300))
    climbing = await rig.run(2.0)
    view = climbing[-1].manual
    assert view is not None
    assert view.ramping
    assert view.ramp_eta is not None

    standing = await _frozen(rig, "thread")
    held = await rig.run(3.0, feed=standing.feed, ping=standing.ping)
    assert len(set(_setpoints(held))) == 1
    for snapshot in held:
        shown = snapshot.manual
        assert shown is not None
        assert not shown.ramping, "a ramp is announced over a setpoint that is held"
        assert shown.ramp_eta is None, "an arrival time is announced over a setpoint that is held"
        assert shown.target.motor_rpm == 300
        assert snapshot.setpoint.motor_rpm != shown.target.motor_rpm

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    resumed = await rig.run(1.0)
    back = resumed[-1].manual
    assert back is not None
    assert back.ramping
    assert back.ramp_eta is not None


@pytest.mark.parametrize("request_kind", ["stop", "zero"])
async def test_a_descent_under_a_freeze_announces_its_ramp_and_a_true_arrival_time(
    request_kind: Literal["stop", "zero"],
) -> None:
    """Under a FREEZE a descent to zero is a real ramp: it is announced, with the time it takes."""
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    at_target = rig.runtime.snapshot().manual
    assert at_target is not None
    assert not at_target.ramping, "at its target under a FREEZE: nothing to announce"
    assert at_target.ramp_eta == 0.0

    if request_kind == "stop":
        rig.runtime.request_stop("operator: stop button")
    else:
        assert rig.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
    first = await rig.step()
    shown = first.manual
    assert shown is not None
    assert shown.ramping
    assert shown.target.motor_rpm == 0
    announced = shown.ramp_eta
    assert announced is not None
    down = await _walk_down(rig, standing)
    assert abs(len(down) * TICK - announced) <= 2 * TICK, (len(down) * TICK, announced)
    last = down[-1].manual
    assert last is not None
    assert not last.ramping


# =========================================================================
# WITH ANH-181: a session stopped under a FREEZE is over, and no longer judged
# =========================================================================
#
# ``session_overrun`` judges a session only until the runtime states it over:
# its phase machine has reached DONE and the setpoint in force is zero
# (``tests/test_runtime_session_overrun.py``). A stop under a FREEZE now walks
# the setpoint to zero, so it must lead there like any other stop. (A FREEZE
# no longer holds an arm past the end of its programme for a stop to find
# there: ``tests/test_runtime_cooldown_freeze.py``.)


@pytest.mark.parametrize("source", ["thread", "hr_stale"])
async def test_a_programme_stopped_under_a_freeze_is_over_and_the_rest_latches_nothing(
    source: Source,
) -> None:
    """STOP under a FREEZE, the walk to zero, the recovery, then three programme lengths of rest.

    The runtime does not call the session over while the walk is under way; it
    does from the first DONE tick, with the setpoint at zero, and on every
    tick after it. So the deadline of a programme that no longer runs passes
    with nothing firing, and the next start is taken.
    """
    rig = await _programme()
    await rig.run(40.0)
    standing = await _frozen(rig, source)
    before = _applied(rig)
    with _observations() as seen:
        rig.runtime.request_stop("operator: stop button")
        down = await _walk_down(rig, standing)
        assert down[0].setpoint.motor_rpm < before
        assert not any(observation.session_over for observation in seen), (
            "the session was called over while its arm was still coming down"
        )
        await rig.run(float(rig.program.profile.recovery_s) + 2.0)
        assert rig.phase() is Phase.DONE
        assert rig.state() is RuntimeState.FINISHED
        assert _end(rig) is EndReason.OPERATOR_STOP
        assert _since_start(rig) < DEADLINE, "the session ended after the deadline: not this case"
        if standing.latched:
            assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
        await _at_rest(rig, 3 * TOTAL)
    done = next(index for index, observation in enumerate(seen) if observation.phase is Phase.DONE)
    assert all(observation.session_over for observation in seen[done:])
    await _a_new_programme_is_accepted(rig)


async def test_a_manual_session_stopped_under_a_freeze_is_over_and_the_rest_latches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same for a manual session, measured against its own limit (100 s here)."""
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", SHORT_LIMIT)
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    rig.runtime.request_stop("operator: stop button")
    await _walk_down(rig, standing)
    await rig.run(10.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert _since_start(rig) < float(SHORT_LIMIT) + 30.0
    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)

    await _at_rest(rig, 3 * float(SHORT_LIMIT))
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300)))


async def test_a_session_ended_by_a_zero_target_under_a_freeze_gains_no_overrun_at_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The latched standstill of a zero target stays the only ending, however long it waits.

    Left unacknowledged for three times the manual limit: ``session_overrun``
    never joins it, one named acknowledgement clears what is latched, and it
    stays cleared.
    """
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", SHORT_LIMIT)
    rig = await _manual(Occupancy.BENCH, MANUAL_SPEED)
    standing = await _frozen(rig, "thread")
    assert rig.runtime.set_manual_target(_zero()) == Ok(MotorRpm(0))
    await _walk_down(rig, standing)
    await rig.run(10.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _verdict(rig) == (RULE_SESSION_STANDSTILL, SafetyAction.RAMP_DOWN, True)

    while _since_start(rig) < 3 * float(SHORT_LIMIT):
        snapshot = await rig.step()
        assert _overrun(rig) is None, f"session_overrun joined in, {_since_start(rig):.1f} s in"
        assert snapshot.setpoint.motor_rpm == 0
    assert _verdict(rig) == (RULE_SESSION_STANDSTILL, SafetyAction.RAMP_DOWN, True)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert RULE_SESSION_STANDSTILL in acknowledged.value.cleared
    await rig.run(5.0)
    assert _verdict(rig) is None, "the acknowledgement did not hold"
    assert is_ok(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(300)))
