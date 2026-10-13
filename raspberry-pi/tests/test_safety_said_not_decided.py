"""What the supervisor SAYS for the screen, and that none of it decides anything (ANH-190).

Two additions, both for the operator's screen and for nothing else:

* :attr:`~src.training.safety.SafetyObservation.nothing_waits`: the runtime's
  statement that a manual arm is at rest with a target of zero. Over such an
  arm an unlatched verdict no longer says that the speed follows the manual
  target again, upwards too, with nobody clicking
  (:data:`~src.training.safety.SELF_CLEARING`): nothing is waiting, so that
  sentence was untrue there.
* :attr:`~src.training.safety.SafetySupervisor.silenced`: the latched verdict
  the floor held when ``GO_SILENT`` took its place. It is still latched, and
  the floor no longer shows it.

The cases run on the supervisor rig of ``tests/test_safety.py``. Each addition
has its property: over arbitrary evidence, a supervisor that is told
``nothing_waits`` raises the verdicts of one that is not, to the action, the
rule, the latch and the instant; and ``silenced`` is whatever the floor held
when ``GO_SILENT`` first stood, while everything that decides is unchanged.
When the runtime states ``nothing_waits`` is proved on the real tick, in
``tests/test_runtime_said_not_decided.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.motor.drive import DriveState
from src.result import Err
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_DRIVE_FAULT,
    RULE_HR_STALE,
    RULE_REVERSE_ROTATION,
    SELF_CLEARING,
    GoSilentIsTerminal,
    SafetyObservation,
)
from src.training.types import Phase, SafetyAction, SafetyVerdict
from src.units import Amperes, Bpm, MotorRpm, Seconds
from tests.test_safety import LIMITS, Rig

OPERATOR = "dr-mensah"


def _tick(rig: Rig, *, nothing_waits: bool, fresh: bool = True) -> SafetyVerdict | None:
    """One control period, with the runtime's statement set to ``nothing_waits``."""
    rig.clock.advance(rig.limits.control_period)
    rig.elapsed = Seconds(rig.elapsed + rig.limits.control_period)
    if fresh:
        rig.seq += 1
    observation: SafetyObservation = replace(rig.observation(), nothing_waits=nothing_waits)
    return rig.supervisor.evaluate(observation)


def _raised(rig: Rig, rule: str) -> SafetyVerdict:
    verdict = rig.verdict_for(rule)
    assert verdict is not None, f"{rule} is not firing: {rig.live()}"
    return verdict


def _heart_rate_gone(rig: Rig, seconds: float, *, nothing_waits: bool) -> None:
    """``seconds`` with no fresh heart rate: hr_stale holds past 10 s, lowers past 30 s."""
    _tick(rig, nothing_waits=nothing_waits)
    for _ in range(round(seconds / rig.limits.control_period)):
        _tick(rig, nothing_waits=nothing_waits, fresh=False)


# =========================================================================
# nothing_waits: the sentence, and only the sentence
# =========================================================================


def test_an_observation_says_something_may_wait_unless_the_runtime_states_otherwise() -> None:
    """The default is ``False``: an observation built without the field is worded as before."""
    rig = Rig()
    assert rig.observation().nothing_waits is False
    rig.tick()
    rig.fresh = False
    rig.run(Seconds(12.0))
    assert _raised(rig, RULE_HR_STALE).detail.endswith(SELF_CLEARING)


@pytest.mark.parametrize("seconds", [12.0, 32.0], ids=["held by a freeze", "lowered by a reduce"])
def test_over_a_manual_arm_at_rest_a_warning_no_longer_says_the_speed_will_climb_again(
    seconds: float,
) -> None:
    """EX-7. Target zero, setpoint zero: nothing is waiting, and the sentence was untrue."""
    rig = Rig()
    _heart_rate_gone(rig, seconds, nothing_waits=True)
    held = _raised(rig, RULE_HR_STALE)
    assert held.latched is False
    assert SELF_CLEARING not in held.detail
    assert "NOT LATCHED" not in held.detail
    # The rule's own sentence is there, whole.
    assert held.detail.startswith("no fresh trustworthy heart rate for ")
    assert held.detail.endswith("however current its timestamp looks")
    standing = rig.standing()
    assert standing is not None
    assert SELF_CLEARING not in standing.detail


def test_the_sentence_is_the_only_thing_the_statement_changes() -> None:
    """Same evidence, tick for tick, told and not told: the same verdict but for its words."""
    told, untold = Rig(), Rig()
    _heart_rate_gone(told, 32.0, nothing_waits=True)
    _heart_rate_gone(untold, 32.0, nothing_waits=False)
    quiet = _raised(told, RULE_HR_STALE)
    said = _raised(untold, RULE_HR_STALE)
    assert said.detail == quiet.detail + SELF_CLEARING
    assert replace(said, detail=quiet.detail) == quiet
    assert quiet.action is SafetyAction.REDUCE


def test_the_sentence_comes_back_with_a_target_that_waits() -> None:
    """Tick by tick: it is said exactly on the ticks the runtime does not state it."""
    rig = Rig()
    _heart_rate_gone(rig, 12.0, nothing_waits=True)
    assert SELF_CLEARING not in _raised(rig, RULE_HR_STALE).detail
    _tick(rig, nothing_waits=False, fresh=False)
    assert _raised(rig, RULE_HR_STALE).detail.endswith(SELF_CLEARING)
    _tick(rig, nothing_waits=True, fresh=False)
    assert SELF_CLEARING not in _raised(rig, RULE_HR_STALE).detail


def test_a_latched_verdict_reads_the_same_whatever_the_statement() -> None:
    """Nothing resumes behind a latch: it never carried the sentence, told or not."""
    told, untold = Rig(), Rig()
    _heart_rate_gone(told, 62.0, nothing_waits=True)
    _heart_rate_gone(untold, 62.0, nothing_waits=False)
    assert _raised(told, RULE_HR_STALE) == _raised(untold, RULE_HR_STALE)
    assert _raised(told, RULE_HR_STALE).latched is True
    assert told.floor() == untold.floor()


@dataclass(frozen=True, slots=True)
class _Evidence:
    """What one tick shows two supervisors alike; only ``nothing_waits`` differs between them."""

    bpm: int | None
    fresh: bool
    present: bool
    current: float
    commanded: int
    measured: int | None
    drive_state: DriveState
    comm_failures: int
    phase: Phase
    advance: float
    nothing_waits: bool


_evidence = st.builds(
    _Evidence,
    bpm=st.one_of(st.none(), st.integers(min_value=30, max_value=200)),
    fresh=st.booleans(),
    present=st.booleans(),
    current=st.floats(min_value=0.0, max_value=4.0),
    commanded=st.sampled_from([0, 0, 60, 300, 900]),
    measured=st.one_of(st.none(), st.sampled_from([-80, 0, 0, 60, 300, 900])),
    drive_state=st.sampled_from(
        [DriveState.OPERATION_ENABLED, DriveState.OPERATION_ENABLED, DriveState.FAULT]
    ),
    comm_failures=st.sampled_from([0, 0, 0, 1, 5]),
    phase=st.sampled_from(list(Phase)),
    advance=st.sampled_from([0.2, 0.2, 0.2, 1.0, 5.0]),
    nothing_waits=st.booleans(),
)


def _show(rig: Rig, evidence: _Evidence, *, nothing_waits: bool) -> SafetyVerdict | None:
    rig.bpm = None if evidence.bpm is None else Bpm(evidence.bpm)
    rig.fresh = evidence.fresh
    rig.attendant_present = evidence.present
    rig.current = Amperes(evidence.current)
    rig.commanded = MotorRpm(evidence.commanded)
    rig.measured = None if evidence.measured is None else MotorRpm(evidence.measured)
    rig.drive_state = evidence.drive_state
    rig.comm_failures = evidence.comm_failures
    rig.phase = evidence.phase
    step = Seconds(evidence.advance)
    rig.clock.advance(step)
    rig.elapsed = Seconds(rig.elapsed + step)
    if rig.fresh:
        rig.seq += 1
    return rig.supervisor.evaluate(replace(rig.observation(), nothing_waits=nothing_waits))


def _decided(verdict: SafetyVerdict | None) -> tuple[object, ...] | None:
    """Everything of a verdict that the runtime, the start gate or an acknowledgement reads."""
    if verdict is None:
        return None
    return (verdict.action, verdict.rule, verdict.latched, verdict.since)


def _words(verdict: SafetyVerdict | None) -> str | None:
    return None if verdict is None else verdict.detail.removesuffix(SELF_CLEARING)


@settings(max_examples=300, deadline=None)
@given(st.lists(_evidence, min_size=1, max_size=80))
def test_no_verdict_depends_on_the_statement_over_any_evidence(history: list[_Evidence]) -> None:
    """PROPERTY: told ``nothing_waits`` on any ticks, the supervisor decides what it would untold.

    Two supervisors are shown the same evidence, tick for tick. One is told
    ``nothing_waits`` on the ticks the history says, the other never. After
    every tick the verdict returned, the standing one, the floor and every
    rule firing agree on the action, the rule, the latch and the instant, and
    on the words up to the one sentence. And that sentence is on the told
    supervisor's unlatched verdicts exactly when it is on the other's and the
    tick was not told.
    """
    told, untold = Rig(), Rig()
    for evidence in history:
        returned = _show(told, evidence, nothing_waits=evidence.nothing_waits)
        expected = _show(untold, evidence, nothing_waits=False)
        assert _decided(returned) == _decided(expected)
        assert _decided(told.standing()) == _decided(untold.standing())
        assert _decided(told.floor()) == _decided(untold.floor())
        assert told.standing_action() is untold.standing_action()
        assert _words(told.standing()) == _words(untold.standing())
        pairs = list(zip(told.live(), untold.live(), strict=True))
        for quiet, said in pairs:
            assert _decided(quiet) == _decided(said)
            assert _words(quiet) == _words(said)
            says = said.detail.endswith(SELF_CLEARING)
            assert quiet.detail.endswith(SELF_CLEARING) is (says and not evidence.nothing_waits)
        assert told.supervisor.silenced == untold.supervisor.silenced


# =========================================================================
# silenced: what GO_SILENT took the place of
# =========================================================================


def _reverse(rig: Rig) -> SafetyVerdict:
    """The shaft turning backwards under a forward command: a latched QUICK_STOP of a rule."""
    rig.commanded = MotorRpm(300)
    rig.measured = MotorRpm(-80)
    rig.run(Seconds(2.0))
    stopped = rig.floor()
    assert stopped is not None
    assert (stopped.rule, stopped.action, stopped.latched) == (
        RULE_REVERSE_ROTATION,
        SafetyAction.QUICK_STOP,
        True,
    )
    return stopped


def _lose_the_link(rig: Rig) -> SafetyVerdict:
    rig.comm_failures = rig.limits.comms_lost_failures
    rig.tick()
    silent = rig.floor()
    assert silent is not None
    assert (silent.rule, silent.action) == (RULE_COMMS_LOST, SafetyAction.GO_SILENT)
    return silent


def test_nothing_is_silenced_while_go_silent_does_not_stand() -> None:
    rig = Rig()
    assert rig.supervisor.silenced is None
    _reverse(rig)
    assert rig.supervisor.silenced is None, "a latched stop is on the floor: nothing hides it"


def test_the_stop_go_silent_replaces_is_kept_and_named() -> None:
    """Item 2: a stop latched by a rule, then the link lost in the same console."""
    rig = Rig()
    stopped = _reverse(rig)
    silent = _lose_the_link(rig)
    # The floor and the standing verdict show GO_SILENT alone, as they always have.
    assert rig.floor() == silent
    assert rig.standing() == silent
    # What they no longer show is still there to be read, whole: its rule, its instant.
    assert rig.supervisor.silenced == stopped


def test_what_was_silenced_lasts_as_long_as_go_silent_does() -> None:
    """Nothing is acknowledged under GO_SILENT, so the stop behind it is never cleared either."""
    rig = Rig()
    stopped = _reverse(rig)
    _lose_the_link(rig)
    refused = rig.supervisor.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, GoSilentIsTerminal)
    # The link comes back, the shaft stops, hours pass: GO_SILENT is one-way.
    rig.comm_failures = 0
    rig.commanded = MotorRpm(0)
    rig.measured = MotorRpm(0)
    rig.run(Seconds(30.0))
    assert rig.standing_action() is SafetyAction.GO_SILENT
    assert rig.supervisor.silenced == stopped


def test_go_silent_over_an_empty_floor_silences_nothing() -> None:
    rig = Rig()
    _lose_the_link(rig)
    assert rig.supervisor.silenced is None


def test_a_lesser_latch_under_go_silent_is_named_too() -> None:
    """Whatever the floor held: a drive fault (RAMP_DOWN) is said as a stop would be."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    faulted = rig.floor()
    assert faulted is not None
    assert faulted.rule == RULE_DRIVE_FAULT
    _lose_the_link(rig)
    assert rig.supervisor.silenced == faulted


def test_a_stop_that_replaces_a_lesser_latch_silences_nothing() -> None:
    """Only GO_SILENT hides what cannot be acknowledged any more; a QUICK_STOP can be."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    rig.drive_state = DriveState.OPERATION_ENABLED
    _reverse(rig)
    assert rig.supervisor.silenced is None


def test_the_emergency_stop_is_not_what_was_silenced() -> None:
    """It has a slot of its own (``estop``), which nothing replaces: it is read there."""
    rig = Rig()
    pressed = rig.supervisor.latch_estop("replay")
    rig.tick()
    _lose_the_link(rig)
    assert rig.supervisor.silenced is None
    assert rig.supervisor.estop == pressed


def test_an_unlatched_warning_under_go_silent_is_not_what_was_silenced() -> None:
    """Only what was LATCHED is kept: a warning that was merely firing held nothing."""
    rig = Rig()
    rig.attendant_last_seen = rig.clock.monotonic()
    rig.attendant_present = False
    rig.run(Seconds(62.0))
    assert _raised(rig, RULE_ATTENDANT_ABSENT).latched is False
    assert rig.floor() is None
    _lose_the_link(rig)
    assert rig.supervisor.silenced is None


@settings(max_examples=300, deadline=None)
@given(st.lists(_evidence, min_size=1, max_size=80), st.integers(min_value=0, max_value=79))
def test_what_is_silenced_is_what_the_floor_held_when_go_silent_came(
    history: list[_Evidence], pressed_at: int
) -> None:
    """PROPERTY: over any evidence, with an emergency stop pressed at any tick.

    ``silenced`` is ``None`` for as long as the floor is not ``GO_SILENT``.
    On the tick ``GO_SILENT`` takes the floor it is the most severe of what
    the floor held ahead of it: the floor of the tick before, or a rule that
    latched on this very tick and is judged before the one that goes silent.
    (A rule judged after it never reaches the floor at all: nothing replaces
    ``GO_SILENT``.) It never changes again. It is never ``GO_SILENT`` itself,
    never the emergency stop, and always latched.
    """
    rig = Rig(LIMITS)
    before: SafetyVerdict | None = None
    kept: SafetyVerdict | None = None
    silent = False
    for index, evidence in enumerate(history):
        if index == pressed_at:
            rig.supervisor.latch_estop("replay")
        _show(rig, evidence, nothing_waits=evidence.nothing_waits)
        floor = rig.floor()
        silenced = rig.supervisor.silenced
        if floor is None or floor.action is not SafetyAction.GO_SILENT:
            assert silenced is None
            before = floor
            continue
        if not silent:
            silent = True
            kept = silenced
            firing = rig.live()
            went_silent = next(
                place
                for place, verdict in enumerate(firing)
                if verdict.action is SafetyAction.GO_SILENT
            )
            under = [
                verdict
                for verdict in (before, *firing[:went_silent])
                if verdict is not None and verdict.latched
            ]
            if not under:
                assert silenced is None
            else:
                assert silenced in under
                assert silenced.action is max(verdict.action for verdict in under)
        assert silenced == kept
        if silenced is not None:
            assert silenced.latched is True
            assert silenced.action < SafetyAction.GO_SILENT
            assert silenced != rig.supervisor.estop
