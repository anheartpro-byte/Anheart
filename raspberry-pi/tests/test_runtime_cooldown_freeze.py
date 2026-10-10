"""Under a FREEZE the setpoint follows the programme's own descent (ANH-189).

A FREEZE holds a speed somebody still wants. From the entry into ``COOLDOWN``
a programme wants its arm down, and no phase after that one asks for speed
again. So from that tick the setpoint walks to zero at the motion limits,
under a FREEZE exactly as with no verdict, latched or not, and the session
then ends as a programme run to its end. ``tests/test_runtime_stop_freeze.py``
is the same rule for a stop somebody asked for (ANH-175); this file is the
descent nobody has to ask for.

Four groups, and the split matters when reading a failure:

* **the descent**: these fail on the code before the change, where the
  setpoint was held through the cooldown and the recovery;
* **what follows it**: the recovery, the end at the planned time, and nothing
  that restarts. These fail there too;
* **the walk itself**: what the descent may and may not do. Two bounds hold
  wherever the control period falls: never faster than the motion limits,
  and never behind the cooldown of a twin rig that no FREEZE touches. It is
  NOT always that twin's cooldown tick for tick: an ordinary cooldown is also
  bound by the control law's own ramp, and once that ramp's head start is
  spent the twin is the slower of the two, with the shipped limits too;
* **unchanged on purpose**: before the cooldown a FREEZE holds exactly as
  before, a manual session has no cooldown of its own, every stronger
  verdict still decides first, and a cause that lasts still reaches its
  rule's own later levels. These pass before and after.

The matrix is every source of a FREEZE the supervisor has: ``hr_stale`` and
``attendant_absent`` (not latched), ``loop_stall`` and a trip from a thread
(latched). The fake drive and the manual clock of ``tests/test_runtime.py``;
never the hardware.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from typing import Final, Literal, assert_never

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.local_panel import RUNTIME_LIMITS
from src.result import Err, Ok, is_ok
from src.training.motion import DEFAULT_MOTION_LIMITS, MotionLimits, motor_rate_limit
from src.training.plan import TrainingProfile
from src.training.runtime import EndReason, RuntimeLimits, RuntimeState, SafetyStanding
from src.training.safety import RULE_SESSION_OVERRUN, RULE_SESSION_STANDSTILL, SafetySupervisor
from src.training.types import Occupancy, Phase, RunMode, SafetyAction, TelemetrySnapshot
from src.units import Bpm, MotorRpm, RpmPerSecond, Seconds
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    REAL_PROFILE,
    RIG_MOTION,
    TICK,
    Occupant,
    Rig,
    _Imposed,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
    _imposing,  # pyright: ignore[reportPrivateUsage]
    _profile,  # pyright: ignore[reportPrivateUsage]
    _rig,  # pyright: ignore[reportPrivateUsage]
    _running_rig,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_session_overrun import (
    _a_new_programme_is_accepted,  # pyright: ignore[reportPrivateUsage]  # the rest after a session
    _at_rest,  # pyright: ignore[reportPrivateUsage]
    _observations,  # pyright: ignore[reportPrivateUsage]
    _overrun,  # pyright: ignore[reportPrivateUsage]
    _since_start,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _console_rig,  # pyright: ignore[reportPrivateUsage]  # the shipped programme's rig
    _manual,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_stop_freeze import (
    RULES,
    SHIPPED,
    THREAD_RULE,
    Source,
    Standing,
    Walk,
    _applied,  # pyright: ignore[reportPrivateUsage]  # the readers and the judge of a walk
    _assert_an_ordinary_walk,  # pyright: ignore[reportPrivateUsage]
    _end,  # pyright: ignore[reportPrivateUsage]
    _rules_shown,  # pyright: ignore[reportPrivateUsage]
    _setpoints,  # pyright: ignore[reportPrivateUsage]
    _verdict,  # pyright: ignore[reportPrivateUsage]
    _walk_down,  # pyright: ignore[reportPrivateUsage]
    _walk_of,  # pyright: ignore[reportPrivateUsage]
)

SOURCES: Final[tuple[Source, ...]] = ("hr_stale", "attendant_absent", "loop_stall", "thread")
"""Every source of a FREEZE in ``src/training/safety.py``."""

LONG: Final[TrainingProfile] = _profile(total_duration_s=Seconds(200.0))
"""The accelerated rig's programme with 100 s of HOLD: BASELINE to 10, WARMUP to
30, HOLD to 130, COOLDOWN to 140, RECOVERY to 200."""

COOLDOWN_AT: Final[float] = 130.0
PLANNED: Final[float] = 200.0
DEADLINE: Final[float] = PLANNED + 30.0
"""Past this ``session_overrun`` ends a session that is still in progress."""

FREEZE_AT: Final[dict[Source, float]] = {
    "hr_stale": 118.0,
    "attendant_absent": 65.0,
    "loop_stall": 100.0,
    "thread": 100.0,
}
"""When each cause begins, so that its FREEZE stands at the entry into COOLDOWN
and through the whole descent: ``hr_stale`` freezes from 10 s to 30 s without a
heart rate, ``attendant_absent`` from 60 s to 120 s without a ping."""

CLIMBING_AT: Final[float] = 17.0
"""Seconds into the accelerated rig's programme at which its WARMUP climb is half done."""

SHORT_COOLDOWN: Final[TrainingProfile] = _profile(
    total_duration_s=Seconds(200.0), cooldown_s=Seconds(4.0)
)
"""A cooldown of 4 s, shorter than the descent at the shipped motion limits:
COOLDOWN from 136 to 140, and the arm still coming down when RECOVERY begins."""

SHORT_COOLDOWN_AT: Final[float] = 136.0

SHIPPED_COOLDOWN_AT: Final[float] = 1260.0
SHIPPED_PLANNED: Final[float] = float(REAL_PROFILE.total_duration_s)
PLATEAU_AT: Final[float] = 900.0
"""Seconds into the shipped programme, in the middle of its HOLD."""

ALIGNMENTS: Final[range] = range(27)
"""Ticks added to HOLD so that COOLDOWN begins at every point of a control period.

The shipped period is five seconds. On the manual clock a decision falls
every 25 or 26 ticks (the gate is ``since >= period``, and 25 ticks may
measure a hair under five seconds), so 27 shifts cover every alignment.
"""

MOST_AHEAD: Final[int] = 17
"""The shift at which the shipped programme's COOLDOWN begins one tick after
the control law's last decision of HOLD, measured on this rig: its head start
is then 3 rpm, the smallest there is."""

SLOW_LAW: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(14.0), start_hysteresis_rpm=MotorRpm(10)
)
"""The shipped control law with a ramp of 14 rpm/s instead of 15.

Its demand comes down by ``floor(slew x dt)`` whole rpm a tick. At 15 rpm/s
and 5 Hz that is 2 or 3 rpm, whichever side of 0.2 s the measured tick falls
on. At 14 it is 2 rpm on every tick, so 10 rpm/s: for certain under the
12.4 rpm/s of the shipped motion limits, whatever the clock reads. This is
the rig on which the two bounds of an ordinary cooldown differ at every run.
"""

Stronger = Literal["reduce", "ramp_down", "quick_stop", "go_silent"]
STRONGER: Final[dict[Stronger, SafetyAction]] = {
    "reduce": SafetyAction.REDUCE,
    "ramp_down": SafetyAction.RAMP_DOWN,
    "quick_stop": SafetyAction.QUICK_STOP,
    "go_silent": SafetyAction.GO_SILENT,
}

NOTHING: Final[Standing] = Standing("", latched=False, feed=True, ping=True)
"""How a rig with no verdict standing is ticked."""


# =========================================================================
# Rigs
# =========================================================================


async def _until(
    rig: Rig, seconds: float, *, feed: bool = True, ping: bool = True
) -> list[TelemetrySnapshot]:
    """Tick until ``seconds`` after the session's start. Returns the snapshots produced."""
    seen: list[TelemetrySnapshot] = []
    while _since_start(rig) < seconds - TICK / 2:
        seen.append(await rig.step(feed=feed, ping=ping))
    return seen


def _cause(rig: Rig, source: Source) -> Standing:
    """Begin the cause of ``source`` now, and say how the rig is ticked while it lasts."""
    rule, latched = RULES[source]
    feed, ping = True, True
    match source:
        case "hr_stale":
            feed = False
        case "attendant_absent":
            ping = False
        case "loop_stall":
            rig.clock.advance(Seconds(1.0))
        case "thread":
            rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
        case _ as unreachable:
            assert_never(unreachable)
    return Standing(rule=rule, latched=latched, feed=feed, ping=ping)


async def _frozen_in_hold(source: Source) -> tuple[Rig, Standing, list[TelemetrySnapshot]]:
    """The long programme under the FREEZE of ``source``, on the last tick of its HOLD.

    Returns the rig, how to tick it, and every snapshot taken under the FREEZE
    so far: the part of the session this change must leave alone.
    """
    rig = await _running_rig(profile=LONG)
    await _until(rig, FREEZE_AT[source])
    standing = _cause(rig, source)
    since = await _until(rig, COOLDOWN_AT - TICK, feed=standing.feed, ping=standing.ping)
    frozen = [snapshot for snapshot in since if snapshot.safety is not None]
    assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, standing.latched)
    assert rig.phase() is Phase.HOLD
    assert _applied(rig) > 0, "the arm is not turning"
    return rig, standing, frozen


async def _twins(
    profile: TrainingProfile,
    at: float,
    *,
    limits: RuntimeLimits = LIMITS,
    motion: MotionLimits = RIG_MOTION,
) -> tuple[Rig, Rig]:
    """Two identical rigs on ``profile``, ``at`` seconds in, turning, no verdict on either."""
    witness = await _started(profile, limits=limits, motion=motion)
    rig = await _started(profile, limits=limits, motion=motion)
    for twin in (witness, rig):
        await _until(twin, at)
        assert _verdict(twin) is None
    assert _applied(rig) == _applied(witness) > 0
    return witness, rig


async def _started(
    profile: TrainingProfile, *, limits: RuntimeLimits = LIMITS, motion: MotionLimits = RIG_MOTION
) -> Rig:
    """A programme just past its BASELINE, fed as ``_running_rig`` feeds it.

    82 bpm through BASELINE, then 65 so that the control law accelerates, but
    without that builder's wait for a turning arm: the shipped control law
    takes longer to start one than the accelerated rig's.
    """
    rig = _rig(profile=profile, limits=limits, motion=motion)
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.fed_bpm = Bpm(65)
    return rig


async def _climbing() -> Rig:
    """The long programme caught in the middle of its WARMUP climb, no verdict standing."""
    rig = await _started(LONG)
    climb = await _until(rig, CLIMBING_AT)
    assert rig.phase() is Phase.WARMUP
    assert _setpoints(climb)[-2] < _setpoints(climb)[-1], "the setpoint is not climbing"
    assert _verdict(rig) is None
    return rig


async def _shipped_before_cooldown(shift: int) -> Rig:
    """The shipped programme with ``shift`` more ticks of HOLD, two ticks before its COOLDOWN.

    The console's limits, the shipped motion limits and a simulated person on
    board, as ``_console_rig`` builds it. Lengthening HOLD moves the entry
    into COOLDOWN against the control law's five-second period.
    """
    profile = replace(REAL_PROFILE, total_duration_s=Seconds(SHIPPED_PLANNED + shift * TICK))
    rig = _rig(
        profile=profile, limits=RUNTIME_LIMITS, motion=DEFAULT_MOTION_LIMITS, occupant=Occupant()
    )
    assert is_ok(await rig.start())
    await rig.run(1.0)
    await _until(rig, SHIPPED_COOLDOWN_AT + shift * TICK - 2 * TICK)
    assert _verdict(rig) is None
    return rig


async def _descents(
    witness: Rig, rig: Rig
) -> tuple[int, list[TelemetrySnapshot], list[TelemetrySnapshot]]:
    """Freeze ``rig`` on the last tick of HOLD, then walk both twins down to zero.

    Returns the setpoint both start from, the descent under the FREEZE, and
    the witness's ordinary cooldown, each from the first tick of COOLDOWN to
    its first zero.
    """
    standing = _freeze(rig)
    for twin in (witness, rig):
        await twin.step()
    start = _applied(rig)
    assert start == _applied(witness) > 0, "the twins do not start from the same setpoint"
    assert rig.phase() is Phase.HOLD
    assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)
    assert _verdict(witness) is None

    under = await _walk_down(rig, standing)
    free = await _walk_down(witness, NOTHING)
    assert under[0].phase is Phase.COOLDOWN
    assert {snapshot.safety_action for snapshot in under} == {SafetyAction.FREEZE}
    assert _rules_shown(free) == set()
    return start, under, free


def _assert_never_behind(under: list[TelemetrySnapshot], free: list[TelemetrySnapshot]) -> None:
    """Tick for tick the setpoint under the FREEZE is at most the ordinary one's.

    And it is at zero no later. Past its own zero there is nothing left to
    compare: it stays there.
    """
    mine, theirs = _setpoints(under), _setpoints(free)
    assert len(mine) <= len(theirs), (
        f"under the FREEZE the arm was down after {len(mine)} ticks, {len(theirs)} without"
    )
    behind = [
        (tick, frozen, ordinary)
        for tick, (frozen, ordinary) in enumerate(zip(mine, theirs, strict=False))
        if frozen > ordinary
    ]
    assert not behind, f"above the ordinary cooldown at (tick, frozen, ordinary) {behind[:3]}"


def _freeze(rig: Rig) -> Standing:
    """Latch a FREEZE from a thread; it stands from the next tick, however the rig is fed."""
    rig.runtime.trip_from_thread(THREAD_RULE, SafetyAction.FREEZE, "under test")
    return Standing(THREAD_RULE, latched=True, feed=True, ping=True)


async def _both(witness: Rig, rig: Rig, seconds: float) -> tuple[list[int], list[int]]:
    """Tick the twins together; the setpoints of each, tick for tick."""
    free: list[int] = []
    under: list[int] = []
    for _ in range(round(seconds / TICK)):
        free.append(int((await witness.step()).setpoint.motor_rpm))
        under.append(int((await rig.step()).setpoint.motor_rpm))
    return free, under


async def _taken_over(action: SafetyAction, *, lead: float) -> None:
    """Twins, one frozen two ticks before COOLDOWN; ``lead`` later both get ``action``.

    From there the same verdict is in force on both, with the same setpoints
    and the same frames on the wire.
    """
    witness, rig = await _twins(LONG, COOLDOWN_AT - 2 * TICK)
    _freeze(rig)
    free, under = await _both(witness, rig, lead)
    assert under == free, "the premise: at this alignment the two descents are the same so far"
    assert _applied(rig) > 0

    for twin in (witness, rig):
        twin.runtime.trip_from_thread("rig_stronger", action, "under test")
        twin.drive.trace.clear()
    free, under = await _both(witness, rig, 20.0)
    assert under == free
    assert rig.drive.trace == witness.drive.trace
    for twin in (witness, rig):
        assert twin.standing_action() is action
    assert rig.runtime.silent == (action is SafetyAction.GO_SILENT)
    if action is not SafetyAction.GO_SILENT:
        assert under[-1] == 0
    if action is SafetyAction.QUICK_STOP:
        assert under[0] == 0, "QUICK_STOP waited for a ramp"


def _never_rises(setpoints: list[int]) -> bool:
    return all(later <= earlier for earlier, later in pairwise(setpoints))


def _ended_at(snapshots: list[TelemetrySnapshot]) -> float:
    """Seconds after the start at which the phase machine first said DONE."""
    return float(next(snapshot for snapshot in snapshots if snapshot.phase is Phase.DONE).elapsed)


# =========================================================================
# THE DESCENT: fails before the change, passes here
# =========================================================================


@pytest.mark.parametrize("source", SOURCES)
async def test_under_a_freeze_the_setpoint_comes_down_from_the_entry_into_cooldown(
    source: Source,
) -> None:
    """EX-1, EX-4: every source of a FREEZE, latched or not.

    The first tick of COOLDOWN lowers the setpoint, and the walk that follows
    is the ordinary one: never up, never faster than the motion limits, the
    last step waiting as a programme's does, zero in the time that walk takes.
    Nothing but the FREEZE stands while it lasts, and no ending is opened: the
    programme is on its own timeline.
    """
    rig, standing, _ = await _frozen_in_hold(source)
    held = _applied(rig)

    first = await rig.step(feed=standing.feed, ping=standing.ping)
    assert first.phase is Phase.COOLDOWN
    assert first.safety_action is SafetyAction.FREEZE, "the entry was not judged under the FREEZE"
    assert first.setpoint.motor_rpm < held, "the FREEZE held the setpoint into the cooldown"
    assert first.mode is RunMode.SEANCE

    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(held, down, _walk_of("programme"))
    assert {snapshot.safety_action for snapshot in down} == {SafetyAction.FREEZE}, (
        "something other than the programme brought the setpoint down"
    )
    assert _rules_shown(down) == {standing.rule}
    assert {snapshot.phase for snapshot in down} == {Phase.COOLDOWN}
    assert rig.runtime.ending is None
    assert _end(rig) is None


async def test_the_shipped_programme_frozen_on_its_plateau_comes_down_and_ends_on_time() -> None:
    """The acceptance criterion, on the machine's own numbers.

    The shipped 30-minute programme on the console's limits, a simulated
    person on board. The loop misses a second in the middle of HOLD:
    ``loop_stall``, FREEZE, latched, and nobody acknowledges it. The setpoint
    is held to the end of HOLD, starts down on the first tick of COOLDOWN, and
    the session is over at its planned duration, as a programme run to its
    end. The rule that used to end such a session, thirty seconds after its
    planned duration, has nothing to say.
    """
    rig = await _console_rig(PLATEAU_AT)
    assert rig.phase() is Phase.HOLD
    standing = _cause(rig, "loop_stall")
    held_for = await _until(rig, SHIPPED_COOLDOWN_AT - TICK)
    held = _applied(rig)
    assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)
    assert set(_setpoints(held_for)) == {held}
    assert rig.phase() is Phase.HOLD

    first = await rig.step()
    assert first.phase is Phase.COOLDOWN
    assert first.setpoint.motor_rpm < held, "the FREEZE held the setpoint into the cooldown"
    down = [first, *await _walk_down(rig, standing)]
    _assert_an_ordinary_walk(held, down, SHIPPED)
    assert {snapshot.safety_action for snapshot in down} == {SafetyAction.FREEZE}

    rest = await _until(rig, SHIPPED_PLANNED + 60.0)
    assert set(_setpoints(rest)) == {0}
    assert abs(_ended_at(rest) - SHIPPED_PLANNED) <= TICK
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert not rig.drive.is_enabled()
    assert _rules_shown(rig.snapshots) == {standing.rule}, "another rule spoke in this session"


async def test_a_freeze_taken_during_the_rise_comes_down_from_the_held_speed_and_never_up() -> None:
    """ "Never rises" has a real case: the cooldown of a programme begins above a held speed.

    A FREEZE latched in the middle of the WARMUP climb holds the setpoint
    below the plateau the programme then reaches on a twin rig. At the entry
    into COOLDOWN the twin comes down from its plateau, and the frozen rig
    from where it was held: at every tick its setpoint is at most the held
    speed AND at most the twin's. It never goes up to meet a profile.
    """
    witness, rig = await _climbing(), await _climbing()
    climbing = _applied(rig)
    assert climbing == _applied(witness)
    standing = _freeze(rig)
    for twin in (witness, rig):
        await _until(twin, COOLDOWN_AT - TICK)
    held, plateau = _applied(rig), _applied(witness)
    assert held == climbing, "the FREEZE did not catch the climb where it was"
    assert int(LONG.min_run_rpm) < held < int(LONG.warmup_rpm_ceiling) < plateau, (
        "the premise: held in mid-climb, below a plateau the twin has reached"
    )
    assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)

    free, under = await _both(witness, rig, 12.0)
    assert under[0] < held, "the FREEZE held the setpoint into the cooldown"
    assert _never_rises([held, *under])
    assert all(mine <= theirs for mine, theirs in zip(under, free, strict=True)), (
        "the frozen rig was above the ordinary cooldown at some tick"
    )
    assert free[-1] == under[-1] == 0
    assert under.index(0) < free.index(0), "the lower start did not arrive first"
    walked = [snapshot for snapshot in rig.snapshots[-len(under) :] if snapshot.setpoint.motor_rpm]
    assert {snapshot.safety_action for snapshot in walked} == {SafetyAction.FREEZE}


async def test_a_descent_still_under_way_at_the_recovery_goes_on_under_a_freeze() -> None:
    """The descent is the programme's for as long as it lasts, not for one phase.

    A cooldown of 4 s at the shipped motion limits: the timeline enters
    RECOVERY with the arm still coming down. Under a FREEZE the walk crosses
    that boundary without a pause, with the setpoints of a twin that no
    FREEZE touches. (The same setpoints, and not only never behind: on this
    rig the control law's own ramp is 70 rpm/s, far above these motion limits,
    so the twin's cooldown is bound by them alone wherever the period falls.)
    """
    witness, rig = await _twins(
        SHORT_COOLDOWN, SHORT_COOLDOWN_AT - 2 * TICK, motion=DEFAULT_MOTION_LIMITS
    )
    _freeze(rig)
    for twin in (witness, rig):
        await twin.step()
    held = _applied(rig)
    assert held == _applied(witness)
    assert rig.phase() is Phase.HOLD

    free, under = await _both(witness, rig, 40.0)
    assert under[0] < held
    assert under == free, "the FREEZE changed the descent"
    assert under[-1] == 0
    descent = rig.snapshots[-len(under) :]
    turning = [snapshot for snapshot in descent if snapshot.setpoint.motor_rpm != 0]
    assert {snapshot.phase for snapshot in turning} == {Phase.COOLDOWN, Phase.RECOVERY}, (
        "the arm was down before RECOVERY: not this case"
    )
    assert {snapshot.safety_action for snapshot in turning} == {SafetyAction.FREEZE}


async def test_a_freeze_that_appears_during_the_cooldown_does_not_pause_it() -> None:
    """The descent begun with no verdict goes on when a FREEZE arrives: never behind, never up.

    A second into the cooldown one twin is frozen. From there its setpoint is
    at every tick at most the other's, it is at zero no later, and the rest
    of its walk stays inside the motion limits.
    """
    witness, rig = await _twins(LONG, COOLDOWN_AT + 1.0)
    assert rig.phase() is Phase.COOLDOWN
    reached = _applied(rig)
    assert 0 < reached < int(LONG.max_rpm), "the descent was not under way"

    standing = _freeze(rig)
    under = await _walk_down(rig, standing)
    free = await _walk_down(witness, NOTHING)
    _assert_never_behind(under, free)
    _assert_an_ordinary_walk(reached, under, _walk_of("programme"))
    assert {snapshot.safety_action for snapshot in under} == {SafetyAction.FREEZE}
    assert _rules_shown(witness.snapshots) == set()


# =========================================================================
# WHAT FOLLOWS IT: the recovery, the end, and nothing that restarts
# =========================================================================


@pytest.mark.parametrize("source", SOURCES)
async def test_a_session_that_came_down_under_a_freeze_ends_as_a_programme_run_to_its_end(
    source: Source,
) -> None:
    """EX-3: the planned duration, ``programme_complete``, and no ``session_overrun``.

    The cause of a FREEZE that is not latched ends once the arm has stopped
    (the electrode is back, the attendant too), so that what is judged is the
    descent and not the rule's own later levels. A latched one is never
    acknowledged while the session runs: it still stands at rest, refuses a
    start in its own name, and one named acknowledgement clears it. The
    setpoint is zero on every tick from the end of the descent, through the
    acknowledgement and for three programme lengths of rest.
    """
    rig, standing, _ = await _frozen_in_hold(source)
    with _observations() as seen:
        down = await _walk_down(rig, standing)
        assert {snapshot.safety_action for snapshot in down} == {SafetyAction.FREEZE}, (
            "the arm came down with a later level of the rule, not with the programme"
        )
        assert not any(observation.session_over for observation in seen), (
            "the session was called over while its arm was still coming down"
        )
        rest = await _until(rig, DEADLINE + 10.0)
        assert set(_setpoints(rest)) == {0}, "something moved after the descent"
        assert abs(_ended_at(rest) - PLANNED) <= TICK
        assert _end(rig) is EndReason.PROGRAMME_COMPLETE
        assert rig.state() is RuntimeState.FINISHED
        assert rig.runtime.mode is RunMode.REPOS
        assert abs(rig.drive.shaft_rpm) < 1.0
        assert not rig.drive.is_enabled()
        shown = _rules_shown(rig.snapshots)
        assert RULE_SESSION_OVERRUN not in shown
        assert RULE_SESSION_STANDSTILL not in shown
        assert shown == {standing.rule}, "another rule spoke in this session"
    done = next(index for index, observation in enumerate(seen) if observation.phase is Phase.DONE)
    assert all(observation.session_over for observation in seen[done:])

    if standing.latched:
        assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)
        refused = await rig.start()
        assert isinstance(refused, Err)
        assert isinstance(refused.error, SafetyStanding)
        assert refused.error.verdict.rule == standing.rule
        acknowledged = rig.runtime.acknowledge(OPERATOR)
        assert isinstance(acknowledged, Ok)
        assert acknowledged.value.cleared == (standing.rule,)
    assert _verdict(rig) is None, "a FREEZE that lifts by itself left something to acknowledge"
    await _at_rest(rig, 3 * PLANNED)
    await _a_new_programme_is_accepted(rig)


async def test_acknowledging_the_freeze_during_the_descent_does_not_raise_or_pause_it() -> None:
    """A latched FREEZE cleared a second into the descent: the ordinary cooldown takes over.

    Nothing rises when the verdict goes, the whole descent seen from end to
    end is still one ordinary walk, and the session ends on its timeline.
    """
    rig, standing, _ = await _frozen_in_hold("thread")
    held = _applied(rig)
    down = await rig.run(1.0)
    assert 0 < _applied(rig) < held

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    down += await _walk_down(rig, NOTHING)
    _assert_an_ordinary_walk(held, down, _walk_of("programme"))
    assert _rules_shown(down[6:]) == set(), f"{standing.rule} came back after the acknowledgement"
    rest = await _until(rig, PLANNED + 2.0)
    assert set(_setpoints(rest)) == {0}
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED


async def test_acknowledging_the_freeze_during_the_recovery_moves_nothing() -> None:
    """The arm is down and the latched FREEZE is cleared before the session is over.

    Nothing was waiting behind it: the setpoint stays at zero on every tick to
    the end of the programme, and after it.
    """
    rig, standing, _ = await _frozen_in_hold("thread")
    await _walk_down(rig, standing)
    await _until(rig, COOLDOWN_AT + 20.0)
    assert rig.phase() is Phase.RECOVERY
    assert _applied(rig) == 0

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    rest = await _until(rig, PLANNED + 30.0)
    assert set(_setpoints(rest)) == {0}, "the acknowledgement let something move"
    assert _rules_shown(rest) == set()
    assert abs(_ended_at(rest) - PLANNED) <= TICK
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED


async def test_a_stop_asked_for_during_the_descent_changes_its_cause_and_not_its_walk() -> None:
    """The programme's descent under a FREEZE, then the operator's STOP on top of it.

    The walk is already the one a stop takes: the setpoints are those of a
    twin that is frozen too and that nobody stops. What changes is the
    ending: it is the operator's from that tick.
    """
    witness, rig = await _twins(LONG, COOLDOWN_AT - 2 * TICK)
    for twin in (witness, rig):
        _freeze(twin)
    free, under = await _both(witness, rig, 1.0 + TICK)
    assert 0 < under[-1] == free[-1]

    rig.runtime.request_stop("operator: stop button")
    free, under = await _both(witness, rig, 8.0)
    assert under == free, "the STOP changed the descent"
    assert under[-1] == 0
    assert _end(rig) is EndReason.OPERATOR_STOP
    assert rig.runtime.mode is RunMode.ARRET
    assert _end(witness) is None


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ticks=st.integers(min_value=100, max_value=975))
async def test_whenever_a_freeze_is_latched_the_programme_ends_on_time_and_never_rises(
    ticks: int,
) -> None:
    """The requirement as a property: any instant of the session, from the climb to the rest.

    A FREEZE latched ``ticks`` control periods in (20 s to 195 s), never
    acknowledged. From there the setpoint never rises; it is exactly the held
    one up to the entry into COOLDOWN; it is zero a descent after that entry
    at the latest; and the session is over at its planned duration, as a
    programme run to its end, with ``session_overrun`` silent past its
    deadline.
    """
    rig = await _running_rig(profile=LONG)
    await _until(rig, ticks * float(TICK))
    held = _applied(rig)
    _freeze(rig)
    after = await _until(rig, DEADLINE + 5.0)

    assert _never_rises([held, *_setpoints(after)])
    before_cooldown = [s for s in after if float(s.elapsed) < COOLDOWN_AT - TICK / 2]
    assert set(_setpoints(before_cooldown)) <= {held}
    late = [s for s in after if float(s.elapsed) > COOLDOWN_AT + 6.0]
    assert set(_setpoints(late)) == {0}
    assert _rules_shown(after) == {THREAD_RULE}
    assert _overrun(rig) is None
    assert _end(rig) is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED
    assert abs(_ended_at(after) - PLANNED) <= TICK


# =========================================================================
# THE WALK ITSELF
# =========================================================================


@pytest.mark.parametrize(
    ("shift", "ahead"),
    [
        pytest.param(0, False, id="cooldown two seconds after a decision"),
        pytest.param(MOST_AHEAD, True, id="cooldown one tick after a decision"),
    ],
)
async def test_on_the_shipped_programme_the_descent_under_a_freeze_is_never_behind_the_ordinary_one(
    shift: int, ahead: bool
) -> None:
    """EX-1, "the usual ramp", on the machine's own numbers, at two places of the control period.

    The shipped 30-minute programme, the console's limits and the shipped
    motion limits, a simulated person on board. One rig reaches its COOLDOWN
    with nothing standing; its twin is frozen on the last tick of HOLD (a
    latched trip from a thread). What holds at both places: the descent under
    the FREEZE stays inside the motion limits, and at every tick its setpoint
    is at most the ordinary cooldown's.

    What differs is the ordinary cooldown, which is also bound by the control
    law's own ramp. That ramp starts ``slew x`` the age of the law's last
    decision ahead, then comes down 2 rpm a tick on this rig, where the motion
    limits pay for 2.48:

    * the programme as shipped enters COOLDOWN two seconds after a decision.
      The head start (29 rpm) outlasts the descent, the motion limits are all
      that binds, and the two write the same setpoints, tick for tick;
    * with HOLD seventeen ticks longer the entry falls one tick after a
      decision. The head start is 3 rpm, the ordinary cooldown goes at the
      control law's pace, and it reaches zero later than the frozen twin
      (88 ticks against 75 when this was measured, 2.6 s).

    So "the ordinary cooldown, tick for tick" is true at the first place and
    not at the second: under a FREEZE the descent can be AHEAD of an ordinary
    one, with the shipped limits.
    """
    witness = await _shipped_before_cooldown(shift)
    rig = await _shipped_before_cooldown(shift)
    start, under, free = await _descents(witness, rig)

    _assert_an_ordinary_walk(start, under, SHIPPED)
    _assert_never_behind(under, free)
    if ahead:
        assert len(free) > len(under), "at this alignment the ordinary cooldown was not the slower"
    else:
        assert _setpoints(under) == _setpoints(free), "at this alignment the two were the same"
    rate = motor_rate_limit(MotorRpm(0), DEFAULT_MOTION_LIMITS, GEOMETRY)
    assert RUNTIME_LIMITS.slew > rate, "the shipped law's nominal ramp is above the motion limits"


async def test_at_every_alignment_the_descent_is_never_behind_and_never_too_fast() -> None:
    """The two bounds that are true at every alignment, on a rig where the two ramps differ.

    The shipped motion limits, and a control law whose ramp is 10 rpm/s on
    every tick (:data:`SLOW_LAW`): an ordinary cooldown is then the slower one
    as soon as its head start is spent. The entry into COOLDOWN is moved
    through a whole control period, one tick at a time. At every place:

    * the descent under the FREEZE is an ordinary walk at the motion limits,
      never faster, and the same walk whatever the place: it does not read
      the control law;
    * at every tick its setpoint is at most the ordinary cooldown's, and it
      is at zero no later.

    And the two are the same only where the head start outlasts the descent:
    elsewhere the ordinary cooldown arrives later, by up to several seconds.
    """
    walk = Walk(motion=DEFAULT_MOTION_LIMITS, passage_slew=float(SLOW_LAW.slew))
    frozen: set[tuple[int, ...]] = set()
    later: list[int] = []
    for shift in ALIGNMENTS:
        profile = replace(LONG, total_duration_s=Seconds(PLANNED + shift * TICK))
        witness, rig = await _twins(
            profile,
            COOLDOWN_AT + shift * TICK - 2 * TICK,
            limits=SLOW_LAW,
            motion=DEFAULT_MOTION_LIMITS,
        )
        start, under, free = await _descents(witness, rig)
        assert start == int(LONG.max_rpm), "the setpoint is not parked at the ceiling"
        _assert_an_ordinary_walk(start, under, walk)
        _assert_never_behind(under, free)
        frozen.add(tuple(_setpoints(under)))
        later.append(len(free) - len(under))

    assert len(frozen) == 1, "the descent under the FREEZE depended on the control period"
    assert min(later) == 0, "nowhere did the head start outlast the descent: not this rig"
    assert max(later) * TICK > 2.0, "the control law's ramp never bound the ordinary cooldown"


async def test_on_the_accelerated_rig_the_descent_under_a_freeze_is_never_behind_either() -> None:
    """The same two bounds through the accelerated rig's own control period (one second).

    There the control law's ramp (70 rpm/s) is a hair above the motion limits
    (69.7), and on this clock it pays for 14 rpm a tick: the two descents
    come out the same at every place. Asserted as the bounds all the same,
    which is what must hold.
    """
    for shift in range(6):
        profile = replace(LONG, total_duration_s=Seconds(PLANNED + shift * TICK))
        witness, rig = await _twins(profile, COOLDOWN_AT + shift * TICK - 2 * TICK)
        start, under, free = await _descents(witness, rig)
        assert start == int(LONG.max_rpm)
        _assert_an_ordinary_walk(start, under, _walk_of("programme"))
        _assert_never_behind(under, free)


async def test_the_descent_after_a_long_freeze_spends_one_tick_of_motion_not_the_hold() -> None:
    """The hold banks nothing: frozen for thirty seconds, and the first step is one tick's worth."""
    rig, standing, frozen = await _frozen_in_hold("thread")
    assert len(frozen) * TICK > 29.0
    held = _applied(rig)
    first = await rig.step(feed=standing.feed, ping=standing.ping)
    step = held - int(first.setpoint.motor_rpm)
    rate = motor_rate_limit(MotorRpm(held), RIG_MOTION, GEOMETRY)
    assert 1 <= step <= rate * TICK + 1, f"a first step of {step} rpm after a 30 s hold"


async def test_a_freeze_that_lifts_during_the_descent_hands_it_over_without_a_pause_or_a_jump() -> (
    None
):
    """The descent begins under a FREEZE that then lifts: one ordinary walk from end to end."""
    rig = await _running_rig(profile=LONG)
    await _until(rig, COOLDOWN_AT - 5.0)
    imposed = _Imposed()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.FREEZE
        held_for = await _until(rig, COOLDOWN_AT - TICK)
        held = _applied(rig)
        assert set(_setpoints(held_for)) == {held}

        down = await rig.run(1.0)
        assert down[0].phase is Phase.COOLDOWN
        assert 0 < _applied(rig) < held
        imposed.action = None
        down += await _walk_down(rig, NOTHING)
    _assert_an_ordinary_walk(held, down, _walk_of("programme"))
    assert _end(rig) is None


@pytest.mark.parametrize("stronger", ["reduce", "ramp_down", "quick_stop", "go_silent"])
async def test_a_stronger_verdict_takes_over_the_descent_under_a_freeze_as_it_takes_over_any(
    stronger: Stronger,
) -> None:
    """EX-2, during the walk: a stronger verdict finds it exactly as it finds an ordinary cooldown.

    A second into the descent, one twin under a FREEZE and the other under
    nothing, both get the stronger verdict on the same tick.
    """
    await _taken_over(STRONGER[stronger], lead=1.0 + TICK)


# =========================================================================
# UNCHANGED ON PURPOSE (EX-2): passes before the change and here
# =========================================================================


@pytest.mark.parametrize("source", SOURCES)
async def test_until_the_cooldown_a_freeze_holds_the_setpoint_exactly(source: Source) -> None:
    """EX-2: every tick under the FREEZE before COOLDOWN writes the held setpoint and no other."""
    rig = await _running_rig(profile=LONG)
    await _until(rig, FREEZE_AT[source])
    frames = len(rig.drive.writes)
    before = _applied(rig)
    standing = _cause(rig, source)
    since = await _until(rig, COOLDOWN_AT - TICK, feed=standing.feed, ping=standing.ping)

    frozen = [snapshot for snapshot in since if snapshot.safety is not None]
    assert len(frozen) * TICK > 2.0, "the FREEZE hardly stood before the cooldown"
    assert {snapshot.safety_action for snapshot in frozen} == {SafetyAction.FREEZE}
    assert {snapshot.phase for snapshot in since} == {Phase.HOLD}
    assert set(_setpoints(since)) == {before}
    assert set(rig.drive.writes[frames:]) == {MotorRpm(before)}, "another reference was written"
    assert _end(rig) is None
    assert rig.state() is RuntimeState.RUNNING


async def test_a_freeze_taken_in_the_warmup_is_held_through_the_hold_that_follows() -> None:
    """EX-2 across a phase boundary: WARMUP gives way to HOLD, and the setpoint does not move."""
    rig = await _climbing()
    _freeze(rig)
    since = await _until(rig, COOLDOWN_AT - TICK)
    assert {snapshot.phase for snapshot in since} == {Phase.WARMUP, Phase.HOLD}
    assert len(set(_setpoints(since))) == 1, "the setpoint moved under the FREEZE before COOLDOWN"
    assert {snapshot.safety_action for snapshot in since} == {SafetyAction.FREEZE}


@pytest.mark.parametrize("occupancy", [Occupancy.BENCH, Occupancy.OCCUPIED])
async def test_a_manual_session_has_no_descent_of_its_own_a_freeze_holds_it_as_before(
    occupancy: Occupancy,
) -> None:
    """A manual session follows its operator: no phase of it asks for the arm to come down.

    Five minutes under a latched FREEZE, longer than any cooldown here: the
    setpoint is the held one on every tick, the phase is HOLD, the mode
    MANUEL, and nothing ends.
    """
    rig = await _manual(occupancy, 200)
    standing = _freeze(rig)
    held = await rig.run(300.0)
    assert set(_setpoints(held)) == {200}
    assert {snapshot.phase for snapshot in held} == {Phase.HOLD}
    assert {snapshot.mode for snapshot in held} == {RunMode.MANUEL}
    assert _verdict(rig) == (standing.rule, SafetyAction.FREEZE, True)
    assert _end(rig) is None


@pytest.mark.parametrize("source", ["hr_stale", "attendant_absent"])
async def test_a_cause_that_lasts_still_ends_the_session_on_the_later_level_of_its_rule(
    source: Source,
) -> None:
    """A FREEZE that is not latched is the first level of a rule that goes on counting.

    The heart rate stays lost, or the attendant away, through the cooldown
    and after it. The rule reaches its own RAMP_DOWN (60 s without a heart
    rate, 120 s without a ping), which latches and ends the session as a
    safety verdict, to be acknowledged: as before this change. "Ends as a
    programme run to its end" is said of a latched FREEZE, and of a cause
    that ends before that level.
    """
    rig, standing, _ = await _frozen_in_hold(source)
    held = _applied(rig)
    after = await _until(rig, COOLDOWN_AT + 62.0, feed=standing.feed, ping=standing.ping)
    assert _never_rises([held, *_setpoints(after)])
    assert _applied(rig) == 0
    assert _verdict(rig) == (standing.rule, SafetyAction.RAMP_DOWN, True)
    assert _end(rig) is EndReason.SAFETY_VERDICT
    assert rig.runtime.mode is RunMode.ARRET
    assert _rules_shown(after) == {standing.rule}


@pytest.mark.parametrize("stronger", ["reduce", "ramp_down", "quick_stop", "go_silent"])
async def test_a_stronger_verdict_that_arrives_with_the_cooldown_decides_exactly_as_before(
    stronger: Stronger,
) -> None:
    """EX-2: REDUCE and everything above it still decide first, in their own arm.

    Two twins reach the last tick of HOLD, one of them frozen. Both get the
    stronger verdict before the first tick of COOLDOWN: its arm runs on both,
    the walk this change adds on neither. The same setpoints and the same
    frames on the wire.
    """
    await _taken_over(STRONGER[stronger], lead=TICK)
