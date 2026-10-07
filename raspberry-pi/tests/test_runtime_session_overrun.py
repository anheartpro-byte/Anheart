"""A session that is over is never judged as overrunning (ANH-181).

``session_overrun`` ends a session that has outlived its programme by more
than 30 s. The time it measures is the time since the start, and that goes on
counting after the session has ended, until the next start. So on ``develop``
the rule latched over a machine at rest, with nothing left to outlive:

* 30 s after a programme had run to its own end (and it put the console back
  to ARRET for a whole recovery, with nothing turning);
* 1831 s after the start of the shipped programme, however early it had been
  stopped; 3631 s after the start of any manual session;
* and from then on for good: every acknowledgement was accepted and taken
  back on the next tick, every start was refused, from the console and from
  the site, until the console process was restarted.

What changed: the runtime states when the session is over (its phase machine
has reached ``DONE`` since the last start AND the setpoint in force is zero),
and the rule is judged only while it is not.

Three groups, and the split matters when reading a failure:

* **at rest, the session over**: these fail on ``develop`` and pass here;
* **a session in progress, unchanged on purpose**: a recovery pushed past the
  deadline by a late STOP, a silent runtime that cannot take its setpoint
  back. The verdict comes at the same instant as before; these pass on
  ``develop`` too. A third case stood here, a session a latched FREEZE held
  at speed past its end. It no longer happens: under a FREEZE the setpoint
  follows the programme's own descent, and that session ends on time
  (``tests/test_runtime_cooldown_freeze.py``). The rule is now a second
  barrier behind that guard, and one test here takes the guard away to show
  that it still brings a turning arm down;
* **a verdict raised during the session, once it is over**: it can be
  acknowledged and stays acknowledged; fails on ``develop``.

The fake drive and the manual clock of ``tests/test_runtime.py``. The short
programmes are the rig's own (130 s, the same phases as the shipped one); the
acceptance test runs the shipped 30-minute programme.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.local_panel import RUNTIME_LIMITS
from src.motor.drive import ControlWord, DriveError, DriveFault
from src.result import Err, Ok, Result, is_ok
from src.training import runtime as runtime_module
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import EndReason, RuntimeState, SafetyStanding, TrainingRuntime
from src.training.safety import (
    RULE_COMMS_LOST,
    RULE_DRIVE_FAULT,
    RULE_OPERATOR_ESTOP,
    RULE_SESSION_OVERRUN,
    RULE_SESSION_STANDSTILL,
    SafetyObservation,
    SafetySupervisor,
)
from src.training.types import (
    Occupancy,
    Phase,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    TelemetrySnapshot,
)
from src.units import Bpm, MotorRpm, Seconds
from tests.test_runtime import (
    OPERATOR,
    REAL_PROFILE,
    TICK,
    FakeDrive,
    Occupant,
    Rig,
    _rig,  # pyright: ignore[reportPrivateUsage]  # the shared rig builders
)
from tests.test_runtime_manual import (
    CEILING,
    _target,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_manual import (
    _manual as _bench,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_runtime_standstill import (
    _stopped_by_a_lost_heart_rate,  # pyright: ignore[reportPrivateUsage]
)

TOTAL: Final[float] = 130.0
"""The rig's programme, in seconds: BASELINE to 10, WARMUP to 30, HOLD to 60,
COOLDOWN to 70, RECOVERY to 130."""

DEADLINE: Final[float] = TOTAL + 30.0
"""Past this the rule fires on ``develop``, session or no session."""

SHORT_LIMIT: Final[Seconds] = Seconds(100.0)
"""The manual session limit these tests run under (3600 s on the machine)."""


# =========================================================================
# Reading the runtime through calls (see tests/test_runtime_standstill.py)
# =========================================================================


def _standing(rig: Rig) -> SafetyVerdict | None:
    return rig.runtime.standing


def _standing_rule(rig: Rig) -> str | None:
    verdict = rig.runtime.standing
    return None if verdict is None else verdict.rule


def _live_rules(rig: Rig) -> set[str]:
    """Every rule of the supervisor firing as of the last tick."""
    return {verdict.rule for verdict in rig.runtime.supervisor.live}


def _overrun(rig: Rig) -> SafetyVerdict | None:
    """The live ``session_overrun`` verdict, or ``None`` when the rule is not firing."""
    for verdict in rig.runtime.supervisor.live:
        if verdict.rule == RULE_SESSION_OVERRUN:
            return verdict
    return None


def _mode(rig: Rig) -> RunMode:
    return rig.runtime.mode


def _applied(rig: Rig) -> MotorRpm:
    return rig.runtime.applied_rpm


def _since_start(rig: Rig) -> float:
    """Seconds since the session started, as the runtime counts them (it never stops)."""
    return float(rig.snapshots[-1].elapsed)


# =========================================================================
# Rigs
# =========================================================================


async def _programme() -> Rig:
    """The rig's 130 s programme, started, a steady heart rate below the zone."""
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    return rig


async def _run_to_its_end(rig: Rig) -> None:
    """Let the programme finish by itself: REPOS, nothing standing."""
    await rig.run(TOTAL + 2.0 - _since_start(rig) if rig.snapshots else TOTAL + 2.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _standing(rig) is None


async def _at_rest(rig: Rig, until: float) -> list[TelemetrySnapshot]:
    """Tick, nobody touching anything, until ``until`` seconds after the session's start.

    Checked on EVERY tick, not at the end: the rule never fires, nothing
    stands, the console stays in REPOS, and no reference but zero is written.
    """
    seen: list[TelemetrySnapshot] = []
    frames = len(rig.drive.writes)
    while _since_start(rig) < until:
        snapshot = await rig.step()
        seen.append(snapshot)
        at = f"{float(snapshot.elapsed):.1f} s after the start"
        assert _overrun(rig) is None, f"session_overrun fired over a machine at rest, {at}"
        assert snapshot.safety is None, f"{snapshot.safety} stands at rest, {at}"
        assert snapshot.mode is RunMode.REPOS, f"the console left REPOS, {at}"
        assert snapshot.setpoint.motor_rpm == 0
    assert set(rig.drive.writes[frames:]) <= {MotorRpm(0)}
    assert rig.state() is RuntimeState.FINISHED
    return seen


@contextmanager
def _observations() -> Generator[list[SafetyObservation]]:
    """Record every observation the runtime hands its supervisor, and judge it as usual."""
    seen: list[SafetyObservation] = []
    real = SafetySupervisor.evaluate

    def _record(self: SafetySupervisor, observation: SafetyObservation) -> SafetyVerdict | None:
        seen.append(observation)
        return real(self, observation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _record)
        yield seen


async def _a_new_programme_is_accepted(rig: Rig) -> None:
    """A start typed now is taken, and the new session runs: no restart of anything."""
    started = await rig.start()
    assert is_ok(started), f"the start was refused after the rest: {started}"
    assert rig.state() is RuntimeState.RUNNING
    await rig.run(5.0)
    assert _standing(rig) is None
    assert _since_start(rig) < 6.0, "the new session did not start its own count"


# =========================================================================
# AT REST, THE SESSION OVER: fails on develop, passes here
# =========================================================================


async def test_the_runtime_states_its_session_over_from_done_until_the_next_start() -> None:
    """What reaches the rule, asserted directly on the recorded observations.

    Before anything has started, nothing is over. Through a programme the
    statement is false on every tick up to the one on which the phase machine
    says DONE, and true from that tick on, for as long as the console rests.
    It is false again from the first tick of the next session.
    """
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    with _observations() as seen:
        await rig.step()
        assert [(o.phase, o.session_over) for o in seen] == [(Phase.DONE, False)], (
            "an idle console said a session was over before any had started"
        )

        assert is_ok(await rig.start())
        await rig.run(TOTAL + 60.0)
        first = seen[1:]
        assert {o.phase for o in first} == set(Phase), "the programme skipped a phase"
        told = [o for o in first if o.session_over != (o.phase is Phase.DONE)]
        assert not told, f"said otherwise than the phase machine {told[0].elapsed:.1f} s in"
        assert first[-1].session_over is True
        assert first[-1].elapsed > DEADLINE

        again = len(seen)
        assert is_ok(await rig.start())
        await rig.run(5.0)
        assert len(seen) > again
        assert not any(o.session_over for o in seen[again:]), "the new session began as over"


async def test_a_programme_run_to_its_end_then_left_at_rest_is_not_judged() -> None:
    """The first measured sequence, on the short programme. On ``develop``: latched at 160 s.

    The programme finishes by itself at 130 s. Nobody touches anything for
    twice its length more. Nothing fires, nothing stands, the console stays
    in REPOS, and a new start is accepted.
    """
    rig = await _programme()
    await _run_to_its_end(rig)

    await _at_rest(rig, TOTAL + 2 * TOTAL + 30.0)
    assert _since_start(rig) > DEADLINE + 2 * TOTAL

    await _a_new_programme_is_accepted(rig)


async def test_the_shipped_programme_then_twice_its_length_at_rest_and_a_new_start() -> None:
    """The acceptance criterion on the shipped numbers. On ``develop``: latched at 1831 s.

    The 30-minute programme on the console's own limits, a simulated person
    on board, run to its own end; then an hour at rest, which is twice the
    planned duration. No verdict at any tick, and the start that follows is
    accepted with no restart of the console.
    """
    rig = _rig(
        profile=REAL_PROFILE,
        limits=RUNTIME_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
        occupant=Occupant(),
    )
    assert is_ok(await rig.start())
    planned = float(REAL_PROFILE.total_duration_s)
    await rig.run(planned + 2.0)
    assert rig.state() is RuntimeState.FINISHED
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE

    await _at_rest(rig, 3 * planned + 30.0)
    assert _since_start(rig) - planned > 2 * planned

    assert is_ok(await rig.start())
    assert rig.state() is RuntimeState.RUNNING


async def test_a_programme_stopped_early_then_left_at_rest_is_not_judged() -> None:
    """The second measured sequence. On ``develop``: latched 160 s after the START.

    STOP at 40 s. The session is over at about 105 s, descent and monitored
    recovery done. The deadline of the programme it no longer runs passes
    with nothing happening.
    """
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.request_stop("operator pressed STOP")
    await rig.run(70.0)
    assert rig.state() is RuntimeState.FINISHED
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
    assert _since_start(rig) < DEADLINE, "the session ended after the deadline: not this case"

    await _at_rest(rig, 3 * TOTAL)

    await _a_new_programme_is_accepted(rig)


@pytest.mark.parametrize("ended_by", ["a STOP", "its own limit"])
async def test_a_manual_session_that_is_over_then_left_at_rest_is_not_judged(
    monkeypatch: pytest.MonkeyPatch, ended_by: str
) -> None:
    """The third measured sequence. On ``develop``: latched 30 s past the manual limit.

    A manual session has no programme: the rule measures it against the
    manual limit, an hour on the machine and 100 s here. Stopped after 20 s,
    or left to end itself at the limit, the session is over; three times the
    limit later nothing has fired and a new manual session is accepted.
    """
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", SHORT_LIMIT)
    rig = await _bench()
    _target(rig, 150)
    await rig.run(20.0)
    if ended_by == "a STOP":
        rig.runtime.request_stop("operator pressed STOP")
        await rig.run(20.0)
        assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
    else:
        await rig.run(float(SHORT_LIMIT))
        assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED
    assert _since_start(rig) < float(SHORT_LIMIT) + 30.0

    await _at_rest(rig, 3 * float(SHORT_LIMIT))

    started = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING)
    assert is_ok(started), f"the manual start was refused after the rest: {started}"
    assert rig.state() is RuntimeState.RUNNING


async def test_a_session_ended_at_a_standstill_keeps_that_verdict_and_gains_no_other() -> None:
    """The ending of ANH-176, on the shipped programme. On ``develop`` it turned into a dead end.

    A lost heart rate lowers the arm to a standstill at about 470 s:
    ``session_standstill``, latched, the session over after its monitored
    recovery. Nobody acknowledges. The programme's 1830 s pass: the standstill
    verdict is still the only one, the rule has not fired, and one named
    acknowledgement clears the console for a new start. On ``develop``
    ``session_overrun`` joined it at 1831 s and could never be cleared.
    """
    rig = await _stopped_by_a_lost_heart_rate()
    await rig.run(float(REAL_PROFILE.recovery_s) + 2.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _standing_rule(rig) == RULE_SESSION_STANDSTILL

    while _since_start(rig) < float(REAL_PROFILE.total_duration_s) + 120.0:
        await rig.step()
        assert _overrun(rig) is None, f"session_overrun fired {_since_start(rig):.1f} s in"
    assert _standing_rule(rig) == RULE_SESSION_STANDSTILL
    assert _live_rules(rig) == set()

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_SESSION_STANDSTILL,)
    await rig.run(60.0)
    assert _standing(rig) is None, "the acknowledgement was taken back"
    assert is_ok(await rig.start())


@pytest.mark.parametrize("arrives", ["an e-stop pressed at rest", "a drive fault at rest"])
async def test_a_verdict_arriving_at_rest_after_the_end_does_not_wake_the_rule(
    arrives: str,
) -> None:
    """The phase is RECOVERY again, and the session is still over.

    The programme has finished by itself. A minute and a half later
    something latches at rest: the rider is getting out and somebody hits the
    e-stop, or the drive is switched off and reports a fault. The runtime
    opens an ending for it (ARRET, a monitored recovery), so the phase leaves
    DONE over a session that ended long ago. The rule must not take that for
    a session in progress. On ``develop`` it had latched 30 s after the end.
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    await _at_rest(rig, DEADLINE + 60.0)

    if arrives == "an e-stop pressed at rest":
        rig.runtime.request_estop("console web: e-stop")
        expected = RULE_OPERATOR_ESTOP
    else:
        rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
        expected = RULE_DRIVE_FAULT
    reopened: set[Phase] = set()
    for _ in range(round(120.0 / TICK)):
        await rig.step()
        reopened.add(rig.phase())
        assert _overrun(rig) is None, "the rule took a recovery opened at rest for a session"
    assert _standing_rule(rig) == expected
    assert Phase.RECOVERY in reopened, (
        "nothing re-opened a recovery: this is not the case under test"
    )

    if arrives == "a drive fault at rest":
        assert is_ok(await rig.runtime.fault_reset())
        await rig.run(2.0)
    assert isinstance(rig.runtime.acknowledge(OPERATOR, estop_released=True), Ok)
    await rig.run(60.0)
    assert _standing(rig) is None
    await _a_new_programme_is_accepted(rig)


async def test_a_drive_fault_reset_long_after_its_session_meets_no_other_verdict() -> None:
    """A fault during the session, and an operator who comes back after the deadline.

    The drive faults at 40 s: the session ends, and it stays in ARRET at
    standstill because a faulted drive will not take the words that remove
    the run command. Nothing is commanded and the phase is DONE: the session
    is over although the console is not at REPOS. At 240 s the fault is reset
    and acknowledged, and a start is accepted. On ``develop`` the rule had
    latched at 160 s and the acknowledgement of it never held.
    """
    rig = await _programme()
    await rig.run(40.0)
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    while _since_start(rig) < DEADLINE + 80.0:
        await rig.step()
        assert _overrun(rig) is None, f"session_overrun fired {_since_start(rig):.1f} s in"
    assert rig.phase() is Phase.DONE
    assert rig.state() is RuntimeState.ENDING, "the faulted drive let its run command go"
    assert _live_rules(rig) == {RULE_DRIVE_FAULT}

    assert is_ok(await rig.runtime.fault_reset())
    await rig.run(2.0)
    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert acknowledged.value.cleared == (RULE_DRIVE_FAULT,)
    await rig.run(30.0)
    assert _standing(rig) is None
    await _a_new_programme_is_accepted(rig)


# --- whatever the ending and however long the rest --------------------------


@dataclass(frozen=True, slots=True)
class _Ending:
    """How one arbitrary session ends, when, and how long the console then rests."""

    how: str
    at: int
    rest: int


_ENDINGS: Final = st.builds(
    _Ending,
    how=st.sampled_from(["its own end", "a STOP", "an e-stop", "a drive fault"]),
    at=st.integers(min_value=1, max_value=95),
    rest=st.integers(min_value=1, max_value=400),
)
"""Any ending before 95 s leaves the 60 s recovery done before the deadline (160 s)."""


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ending=_ENDINGS)
async def test_whatever_ends_a_session_before_its_deadline_the_rest_latches_nothing_more(
    ending: _Ending,
) -> None:
    """The requirement as a property: a session that is over never triggers the rule.

    Any ending, at any second, then any rest: the rule never fires, what
    stands at the end is only what ended the session (nothing, for an ending
    of its own or a STOP), one named acknowledgement clears it, and a new
    start is accepted.
    """
    rig = await _programme()
    stands: str | None = None
    match ending.how:
        case "its own end":
            await rig.run(TOTAL + 2.0)
        case "a STOP":
            await rig.run(float(ending.at))
            rig.runtime.request_stop("operator pressed STOP")
        case "an e-stop":
            await rig.run(float(ending.at))
            rig.runtime.request_estop("console web: e-stop")
            stands = RULE_OPERATOR_ESTOP
        case _:
            await rig.run(float(ending.at))
            rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
            stands = RULE_DRIVE_FAULT
    while _since_start(rig) < DEADLINE + float(ending.rest):
        await rig.step()
        assert _overrun(rig) is None, f"session_overrun fired {_since_start(rig):.1f} s in"
    assert rig.phase() is Phase.DONE
    assert _standing_rule(rig) == stands

    if stands == RULE_DRIVE_FAULT:
        assert is_ok(await rig.runtime.fault_reset())
        await rig.run(2.0)
    if stands is not None:
        assert isinstance(rig.runtime.acknowledge(OPERATOR, estop_released=True), Ok)
        await rig.run(2.0)
    assert _standing(rig) is None
    assert is_ok(await rig.start())


# =========================================================================
# A SESSION IN PROGRESS, UNCHANGED ON PURPOSE: passes on develop and here
# =========================================================================


async def _stopped_late() -> Rig:
    """STOP five seconds before the end: the monitored recovery will outlive the programme."""
    rig = await _programme()
    await rig.run(TOTAL - 5.0)
    rig.runtime.request_stop("operator pressed STOP")
    return rig


def _nothing_asks_for_the_descent(_runtime: TrainingRuntime) -> bool:
    """``TrainingRuntime._stop_asked`` answering "no", whatever the phase: its guard forced off."""
    return False


async def test_an_arm_still_turning_past_the_deadline_is_brought_to_zero_by_the_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule as a second barrier: it ends a descent that did not happen, on a turning arm.

    The runtime follows a programme's descent under a FREEZE (ANH-189), so a
    latched FREEZE no longer carries a turning arm past the end of its
    programme. That guard is the first barrier. This rule stands behind it,
    and nothing reaches it that way any more, so to show that it still works
    the guard is forced off here: a FREEZE then holds the setpoint whatever
    the phase, as it did before that change.

    A FREEZE latched at 40 s and never acknowledged holds the arm at speed
    through the cooldown, the recovery and the end of the timeline, and the
    phase machine does not call a turning arm DONE. On the first tick past
    the deadline the rule fires, RAMP_DOWN and latched. RAMP_DOWN outranks
    the FREEZE: the setpoint comes down to zero and the shaft follows.
    """
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    rig = await _programme()
    await rig.run(40.0)
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.run(DEADLINE - 40.0 - 1.0)
    held = _applied(rig)
    assert held > 0, "with the guard off the FREEZE did not hold the arm past the end"
    assert rig.phase() is Phase.RECOVERY, "a turning arm was called DONE"
    assert _overrun(rig) is None
    for _ in range(round(1.0 / TICK) - 1):
        await rig.step()
        assert _overrun(rig) is None, f"fired early, {_since_start(rig):.1f} s in"
    assert _applied(rig) == held

    await rig.step()
    verdict = _overrun(rig)
    assert verdict is not None, f"nothing ended a session held {_since_start(rig):.1f} s in"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert "130 s plus 30 s of grace" in verdict.detail
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT

    await rig.run(10.0)
    assert _applied(rig) == 0, "RAMP_DOWN did not bring the held arm down"
    assert abs(rig.drive.shaft_rpm) < 1.0


async def test_a_recovery_pushed_past_the_deadline_by_a_late_stop_is_still_judged() -> None:
    """STOP five seconds before the end: the monitored recovery outlives the programme.

    Nothing turns, and the session is not over: the rider is being watched
    for a minute more. The rule fires at the deadline as it always has. What
    is new is in the next group: it can then be cleared.
    """
    rig = await _stopped_late()
    await rig.run(DEADLINE - (TOTAL - 5.0) - 0.2)
    assert rig.phase() is Phase.RECOVERY
    assert _applied(rig) == 0
    assert _overrun(rig) is None

    await rig.step()
    await rig.step()
    verdict = _overrun(rig)
    assert verdict is not None, "the rule no longer judges a session in its recovery"
    assert (verdict.action, verdict.latched) == (SafetyAction.RAMP_DOWN, True)
    assert rig.state() is RuntimeState.ENDING


async def test_a_silent_runtime_that_cannot_take_its_setpoint_back_is_still_judged() -> None:
    """DONE is not enough: the session is over only with a setpoint of zero in force.

    The link dies at 40 s with the arm at speed. The runtime goes silent and
    writes nothing more, so the setpoint it believes in force stays where it
    was; the phase machine reaches DONE all the same, because nothing more
    will ever be decided here. The rule goes on judging, and fires at the
    deadline as before.
    """
    rig = await _programme()
    await rig.run(40.0)
    assert _applied(rig) > 0
    rig.drive.break_comms()
    await rig.run(DEADLINE - 40.0 - 1.0)
    assert _standing_rule(rig) == RULE_COMMS_LOST
    assert rig.phase() is Phase.DONE
    assert _applied(rig) > 0, "a silent runtime took its setpoint back: not this case"
    assert _overrun(rig) is None

    await rig.run(2.0)
    assert _overrun(rig) is not None, "DONE alone silenced the rule over a commanded setpoint"


async def test_a_new_session_is_judged_again_from_its_own_start() -> None:
    """Over, then started again: the statement belongs to the session, not to the console.

    The first programme runs to its end. The second is stopped five seconds
    before its own end, so its recovery outlives it at a setpoint of zero:
    the rule fires at the second session's deadline, which a statement left
    over from the first would have silenced.
    """
    rig = await _programme()
    await _run_to_its_end(rig)
    await _at_rest(rig, DEADLINE + 20.0)

    assert is_ok(await rig.start())
    await rig.run(TOTAL - 5.0)
    rig.runtime.request_stop("operator pressed STOP")
    await rig.run(DEADLINE - (TOTAL - 5.0) + 1.0)
    assert rig.phase() is Phase.RECOVERY
    assert _overrun(rig) is not None, "the second session was judged as already over"


def _held_at_arming(
    reached: asyncio.Event, release: asyncio.Event
) -> Callable[[FakeDrive, ControlWord], Awaitable[Result[None, DriveError]]]:
    """Wrap the fake drive's command write so the FIRST word waits for ``release``."""
    real = FakeDrive.write_command

    async def write_command(drive: FakeDrive, word: ControlWord) -> Result[None, DriveError]:
        if not reached.is_set():
            reached.set()
            await release.wait()
        return await real(drive, word)

    return write_command


async def test_a_tick_that_falls_while_a_start_is_arming_does_not_end_the_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a STARTED session can be over.

    No console ticks while a start is arming, but nothing in the runtime
    forbids it, and the phase is DONE in that window. A tick there must state
    nothing about the session that is about to begin: the session is then
    stopped late, and the rule fires at its deadline over a setpoint of zero,
    which a statement made during the arming would have silenced.
    """
    reached, release = asyncio.Event(), asyncio.Event()
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    monkeypatch.setattr(FakeDrive, "write_command", _held_at_arming(reached, release))

    starting = asyncio.ensure_future(rig.start())
    await asyncio.wait_for(reached.wait(), timeout=5.0)
    assert rig.state() is RuntimeState.IDLE, "the start was over before the tick"
    await rig.step()
    release.set()
    assert is_ok(await starting)
    assert rig.state() is RuntimeState.RUNNING

    await rig.run(TOTAL - 5.0)
    rig.runtime.request_stop("operator pressed STOP")
    await rig.run(DEADLINE - (TOTAL - 5.0) + 1.0)
    assert rig.phase() is Phase.RECOVERY
    assert _applied(rig) == 0
    assert _overrun(rig) is not None, "a tick during the arming marked the new session as over"


# =========================================================================
# A VERDICT RAISED DURING THE SESSION, ONCE IT IS OVER: fails on develop
# =========================================================================


async def test_an_overrun_acknowledged_before_its_session_is_over_is_taken_back() -> None:
    """Unchanged: while the session is on its way out the condition is still true."""
    rig = await _stopped_late()
    await rig.run(DEADLINE - (TOTAL - 5.0) + 10.0)
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN
    assert rig.phase() is Phase.RECOVERY

    assert isinstance(rig.runtime.acknowledge(OPERATOR), Ok)
    await rig.step()
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN, "cleared while the session was not over"


async def test_an_overrun_raised_during_a_session_is_cleared_for_good_once_it_is_over() -> None:
    """EX-3. On ``develop`` the acknowledgement was accepted and taken back, every time.

    A real overrun, raised while the session ran (a STOP typed late). The
    session ends, its recovery runs out, the console is back to REPOS with the
    verdict latched and the rule no longer firing. A start is refused in the
    verdict's name, as for any latch. One named acknowledgement clears it, it
    stays cleared for as long as anybody waits, and a new start is accepted.
    """
    rig = await _stopped_late()
    await rig.run(100.0)
    assert rig.state() is RuntimeState.FINISHED
    assert _mode(rig) is RunMode.REPOS
    assert _standing_rule(rig) == RULE_SESSION_OVERRUN
    assert _overrun(rig) is None, "the rule still fires over a session that is over"

    refused = await rig.start()
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SafetyStanding)
    assert refused.error.verdict.rule == RULE_SESSION_OVERRUN

    acknowledged = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(acknowledged, Ok)
    assert RULE_SESSION_OVERRUN in acknowledged.value.cleared
    for _ in range(round(300.0 / TICK)):
        await rig.step()
        assert _standing(rig) is None, f"taken back {_since_start(rig):.1f} s in"
    await _a_new_programme_is_accepted(rig)
