"""Tests for the safety supervisor.

What these tests are evidence for, in the order the module can hurt somebody:

1. **Every rule fires when it should and not when it should not**, each in
   isolation, with its dwell measured exactly on a
   :class:`~src.clock.ManualClock` so nothing here is timing-dependent and
   nothing is flaky.
2. **The dwells are real.** A rule with a five-second dwell is checked at
   4.9 s (silent) and at 5.0 s (firing), by advancing exactly those amounts -
   not by sleeping and hoping.
3. **The hysteresis is real, and its purpose is safety rather than tidiness.**
   ``test_hard_max_still_fires_when_the_rate_oscillates_across_the_limit`` is
   the load-bearing one: without a release band, a heart rate hovering on the
   limit restarts the dwell on every dip and the rule never fires at all.
4. **Precedence is ``max`` over :class:`SafetyAction`**, including ties, and
   the latched floor never decreases without a human acknowledgement.
   Property-tested over arbitrary interleavings of evidence, e-stops and
   thread trips.
5. **The vasovagal inversion is caught.** ``hr_drop`` ends the session on
   exactly the evidence a naive controller answers by accelerating, and a test
   states that in those terms so the next person to read it cannot mistake it
   for a duplicate of ``hr_hard_max``.
6. **The out-of-band entry points behave as claimed.** ``latch_estop`` is
   visible with no tick and no clock advance; ``trip_from_thread`` survives
   being called from a real thread with a clock that fails the test if it is
   read at all.
7. **The startup gate blocks.** No attestation, no motion, per boot, with the
   attestation logged and timestamped.

Two honesty notes, because "100% of branches" is not the same as "the
guarantee holds":

* **The monotonicity property is scoped to what is actually true.** The
  *latched floor* never decreases without :meth:`acknowledge`, and the standing
  verdict is always at least as severe as the floor. An *unlatched* advisory
  (``FREEZE``/``REDUCE``) does clear when its evidence clears - that is
  deliberate, it is what stops alarm fatigue, and
  ``test_an_advisory_clears_but_never_below_the_floor`` pins the exact
  behaviour rather than letting a vaguer claim stand in for it.
* **Where a property is about real time, real time is used.**
  ``test_latching_the_estop_does_not_block`` measures wall-clock duration,
  because a ManualClock cannot say anything about whether a call blocks. Every
  other timing test uses the ManualClock, where it is measuring session time
  and exactness is the point.
"""

from __future__ import annotations

import ast
import inspect
import logging
import math
import re
import threading
import time
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from typing import Final, assert_never, final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.geometry import MachineGeometry
from src.motor.drive import LFT_FAULT_CODES, DriveFault, DriveState, FaultReport, describe_fault
from src.result import Err, Ok
from src.training import safety
from src.training.safety import (
    ALL_RULES,
    ESTOP_ATTESTATION,
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_CURRENT_HIGH,
    RULE_DRIVE_FAULT,
    RULE_HR_CRITICAL,
    RULE_HR_DROP,
    RULE_HR_HARD_MAX,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    RULE_LOOP_STALL,
    RULE_NO_LOAD,
    RULE_OPERATOR_ESTOP,
    RULE_REVERSE_ROTATION,
    RULE_SESSION_OVERRUN,
    RULE_SETPOINT_UNCONFIRMED,
    RULE_TRACKING_ERROR,
    THREAD_TRIP_DETAIL,
    AcknowledgeRefusal,
    EmergencyStopStillLatched,
    EstopAttestation,
    EstopUnattested,
    GoSilentIsTerminal,
    NothingLatched,
    SafetyAcknowledgement,
    SafetyLimits,
    SafetyObservation,
    SafetySupervisor,
    ThreadTrip,
    Unattributed,
)
from src.training.types import (
    HeartRateSample,
    Phase,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    SpeedEnvelope,
    is_rule_id,
    most_severe,
)
from src.units import (
    Amperes,
    Bpm,
    BpmPerMinute,
    GearRatio,
    GLoad,
    Metres,
    Monotonic,
    MotorRpm,
    Seconds,
    UnixMillis,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAFETY_SOURCE = PROJECT_ROOT / "src" / "training" / "safety.py"

#: The person-specific limits used throughout. Chosen so the two heart-rate
#: levels are far apart and neither is a round multiple of the other, which
#: makes an accidental swap of the two visible rather than coincidentally fine.
HARD_MAX = Bpm(150)
CRITICAL = Bpm(170)

LIMITS: Final[SafetyLimits] = SafetyLimits(hard_max_bpm=HARD_MAX, critical_bpm=CRITICAL)

CONFIRMING: Final[int] = LIMITS.hr_drop_confirm_samples // 2 + LIMITS.hr_drop_persist_samples
"""Fresh readings at a new level before hr_drop fires: three for the median of the
last five to reach it, then held on two more for the three-reading persistence."""

#: The same limits with the loop-stall watch pushed out of reach.
#:
#: Most rules here have dwells of 5 to 300 seconds, and the honest way to test a
#: dwell boundary is to advance exactly to it - but an advance of 5 s is, quite
#: correctly, a stalled control loop, and ``loop_stall`` would latch a FREEZE
#: over whatever the test was measuring. So the rule that objects to time jumps
#: is disabled in the tests that jump time, and it is tested on its own with the
#: real defaults (see the ``loop_stall`` section). Nothing else is relaxed.
PATIENT: Final[SafetyLimits] = replace(
    LIMITS,
    loop_stall_freeze_periods=1.0e9,
    loop_stall_silent_periods=1.0e10,
)

EPOCH = UnixMillis(1_700_000_000_000)
START = Monotonic(1_000.0)

GEOMETRY: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.5), ratio=GearRatio(49.79))
"""The machine's geometry, for rendering a commanded speed as the load it puts on somebody."""


# =========================================================================
# Helpers
# =========================================================================


@final
class _ForbiddenClock:
    """A clock that fails the test if anything reads it.

    Used to prove :meth:`SafetySupervisor.trip_from_thread` reads no clock -
    a claim that a passing test with a working clock could never distinguish
    from luck.
    """

    def monotonic(self) -> Monotonic:
        raise AssertionError("the clock must not be read here")

    def unix_millis(self) -> UnixMillis:
        raise AssertionError("the clock must not be read here")


@final
class Rig:
    """A supervisor plus the inputs of one tick, mutable so a test can perturb one.

    Deliberately a mutable object rather than a fixture returning a frozen
    observation: nearly every test here is "the machine was fine, then one
    thing changed, and N seconds later the verdict is X", and that reads badly
    when every tick has to restate thirteen unchanged fields.

    The baseline is quiet - ``test_a_quiet_session_produces_no_verdict`` proves
    it, and that test is what stops every other test in this file from passing
    for the wrong reason.
    """

    def __init__(self, limits: SafetyLimits = PATIENT) -> None:
        self.limits: SafetyLimits = limits
        self.clock: ManualClock = ManualClock(start=START, epoch_millis=EPOCH)
        self.supervisor: SafetySupervisor = SafetySupervisor(clock=self.clock, limits=limits)
        self.elapsed: Seconds = Seconds(0.0)
        self.seq: int = 0
        self.fresh: bool = True
        self.heart_rate_present: bool = True
        self.bpm: Bpm | None = Bpm(100)
        self.quality: SignalQuality = SignalQuality.GOOD
        self.phase: Phase = Phase.HOLD
        self.total_duration: Seconds = Seconds(2_700.0)
        self.commanded: MotorRpm = MotorRpm(0)
        self.measured: MotorRpm | None = MotorRpm(0)
        self.current: Amperes | None = Amperes(1.0)
        self.drive_state: DriveState = DriveState.OPERATION_ENABLED
        self.fault: FaultReport | None = None
        self.ramping: bool = False
        self.comm_failures: int = 0
        self.attendant_present: bool = True
        self.attendant_last_seen: Monotonic | None = None
        """Where the presence ping is pinned once ``attendant_present`` is False.

        ``None`` means "no ping has ever arrived", which the rule measures from
        the start of the session; setting it to an instant models an attendant
        who was there and then left.
        """
        self.load_known: bool = True
        """Whether the observation states the load ``commanded`` puts on the occupant."""
        self.resting: Bpm | None = None
        """The resting rate BASELINE measured, or ``None`` (a manual session)."""
        self.echo: MotorRpm | None = None
        """LFRD read back this tick, or ``None`` when no read followed the write."""
        self.envelope: SpeedEnvelope | None = None
        """The band the runtime says the shaft may be in, or ``None`` (the older ``ramping``)."""

    def observation(self) -> SafetyObservation:
        """Build the observation for the current clock instant."""
        now = self.clock.monotonic()
        sample: HeartRateSample | None = None
        if self.heart_rate_present:
            sample = HeartRateSample(bpm=self.bpm, quality=self.quality, seq=self.seq, at=now)
        return SafetyObservation(
            now=now,
            phase=self.phase,
            elapsed=self.elapsed,
            total_duration=self.total_duration,
            commanded_rpm=self.commanded,
            ramping=self.ramping,
            heart_rate=sample,
            drive_state=self.drive_state,
            measured_rpm=self.measured,
            current=self.current,
            fault=self.fault,
            consecutive_comm_failures=self.comm_failures,
            attendant_last_seen=now if self.attendant_present else self.attendant_last_seen,
            commanded_g=GEOMETRY.view(self.commanded).g_load if self.load_known else None,
            resting_bpm=self.resting,
            setpoint_echo_rpm=self.echo,
            envelope=self.envelope,
        )

    def tick(self, advance: Seconds | None = None) -> SafetyVerdict | None:
        """Advance the clock by one control period (or ``advance``) and evaluate.

        ``fresh`` controls the sequence number, which is the whole point of
        having it: a tick with ``fresh=False`` reproduces the ECG pipeline
        re-emitting its previous metrics dict - a current timestamp, a good
        quality grade, a plausible rate, and no new evidence whatsoever.
        """
        step = self.limits.control_period if advance is None else advance
        self.clock.advance(step)
        self.elapsed = Seconds(self.elapsed + step)
        if self.fresh:
            self.seq += 1
        return self.supervisor.evaluate(self.observation())

    def run(self, duration: Seconds) -> SafetyVerdict | None:
        """Tick at the control period until ``duration`` of session time has passed."""
        target = Seconds(self.elapsed + duration)
        verdict = self.supervisor.standing
        while self.elapsed < target:
            verdict = self.tick()
        return verdict

    def standing(self) -> SafetyVerdict | None:
        """The standing verdict, read as a CALL rather than as a property.

        Not a pass-through property, and the reason is a type-checker
        behaviour worth knowing about: mypy narrows a property access and then
        treats the next read of the same expression as the already-narrowed
        type. For a value that changes every tick that is wrong, and it turns
        "it was a REDUCE, then it became a FREEZE" - which is exactly what the
        hysteresis and latching tests are about - into an
        unreachable-statement error instead of a test. A method call is
        re-widened each time.
        """
        return self.supervisor.standing

    def standing_action(self) -> SafetyAction:
        """The applied demand. A call, for the reason given in :meth:`standing`."""
        return self.supervisor.standing_action

    def floor(self) -> SafetyVerdict | None:
        """The latched floor. A call, for the reason given in :meth:`standing`."""
        return self.supervisor.floor

    def live(self) -> tuple[SafetyVerdict, ...]:
        """The rules firing right now. A call, for the reason in :meth:`standing`."""
        return self.supervisor.live

    def verdict_for(self, rule: str) -> SafetyVerdict | None:
        """The live verdict from one named rule, ignoring every other rule.

        Rule isolation is asserted through this rather than through
        :attr:`SafetySupervisor.standing`, so a test about one rule cannot be
        satisfied - or spoiled - by another rule that happened to fire.
        """
        for verdict in self.supervisor.live:
            if verdict.rule == rule:
                return verdict
        return None

    def action_for(self, rule: str) -> SafetyAction:
        """The named rule's action, with "not firing" reading as ``NONE``."""
        verdict = self.verdict_for(rule)
        return SafetyAction.NONE if verdict is None else verdict.action


def _assign(target: object, name: str, value: object) -> None:
    """Attempt an attribute assignment the type checker knows is illegal.

    Exists so the immutability tests can try the mutation at runtime without a
    suppression comment, the same way ``tests/test_training_types.py`` does:
    ``verdict.action = ...`` is a type error, which is the *static* half of the
    guarantee, while these tests check the runtime half still holds for code
    that got past the checkers some other way.
    """
    setattr(target, name, value)


def _imported_modules(path: Path) -> frozenset[str]:
    """Every module name imported by a source file, from its AST."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return frozenset(names)


# =========================================================================
# Limits: an incoherent set must not exist
# =========================================================================


def test_the_default_limits_are_coherent() -> None:
    """The machine-derived defaults must pass their own validation."""
    limits = SafetyLimits(hard_max_bpm=HARD_MAX, critical_bpm=CRITICAL)
    assert limits.hard_max_bpm == HARD_MAX
    assert limits.critical_bpm == CRITICAL


def test_the_person_specific_limits_have_no_defaults() -> None:
    """A heart-rate limit must be chosen for the person, never inherited.

    This is the same argument as ``SpeedView.from_motor_rpm`` having no default
    geometry, with a person attached: a default ceiling of 150 bpm is wrong for
    somebody on beta blockers and wrong for an athlete, and a default is how a
    wrong value gets used without anybody choosing it.
    """
    # Both checkers are right to reject this call. The point of the test is
    # that the interpreter rejects it too, so a missing person-specific limit
    # cannot be papered over at runtime by a default nobody chose.
    with pytest.raises(TypeError):
        SafetyLimits()  # type: ignore[call-arg]  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        pytest.param({"critical_bpm": Bpm(140)}, "critical_bpm", id="critical-below-hard-max"),
        pytest.param({"critical_bpm": Bpm(150)}, "critical_bpm", id="critical-equals-hard-max"),
        pytest.param({"hard_max_bpm": Bpm(0)}, "hard_max_bpm", id="hard-max-zero"),
        pytest.param({"hr_release_bpm": Bpm(0)}, "hr_release_bpm", id="no-hysteresis-band"),
        pytest.param({"hr_release_bpm": Bpm(200)}, "hard_max_bpm", id="band-wider-than-limit"),
        pytest.param(
            {"hr_rise_release": BpmPerMinute(30.0)}, "hr_rise_limit", id="rate-release-above-limit"
        ),
        pytest.param(
            {"hr_rate_min_span": Seconds(90.0)}, "hr_rate_window", id="span-exceeds-window"
        ),
        pytest.param(
            {"hr_stale_reduce_after": Seconds(5.0)},
            "hr_stale_reduce_after",
            id="stale-reduce-before-freeze",
        ),
        pytest.param(
            {"hr_stale_ramp_after": Seconds(20.0)},
            "hr_stale_ramp_after",
            id="stale-ramp-before-reduce",
        ),
        pytest.param({"no_load_floor_a": Amperes(3.0)}, "current_warn_a", id="floor-above-warning"),
        pytest.param({"current_trip_a": Amperes(1.0)}, "current_trip_a", id="trip-below-warning"),
        pytest.param(
            {"current_release_a": Amperes(5.0)}, "current_warn_a", id="current-band-too-wide"
        ),
        pytest.param(
            {"loop_stall_freeze_periods": 1.0},
            "loop_stall_freeze_periods",
            id="stall-at-one-period",
        ),
        pytest.param(
            {"loop_stall_silent_periods": 2.0},
            "loop_stall_silent_periods",
            id="silent-before-freeze",
        ),
        pytest.param(
            {"attendant_ramp_after": Seconds(30.0)},
            "attendant_ramp_after",
            id="attendant-ramp-before-freeze",
        ),
        pytest.param(
            {"unresponsive_min_points": 1}, "unresponsive_min_points", id="one-point-per-half"
        ),
        pytest.param({"comms_lost_failures": 0}, "comms_lost_failures", id="no-failures-needed"),
        pytest.param({"hr_drop_bpm": Bpm(0)}, "hr_drop_bpm", id="zero-drop"),
        pytest.param({"hr_drop_window": Seconds(0.0)}, "hr_drop_window", id="zero-drop-window"),
        pytest.param(
            {"unresponsive_window": Seconds(0.0)},
            "unresponsive_window",
            id="zero-unresponsive-window",
        ),
        pytest.param(
            {"unresponsive_g_rise": GLoad(0.0)},
            "unresponsive_g_rise",
            id="zero-g-rise",
        ),
        pytest.param(
            {"unresponsive_min_g": GLoad(0.0)},
            "unresponsive_min_g",
            id="zero-min-load",
        ),
        pytest.param(
            {"hr_drop_load_window": Seconds(0.0)},
            "hr_drop_load_window",
            id="zero-load-window",
        ),
        pytest.param(
            {"hr_drop_confirm_samples": 2},
            "hr_drop_confirm_samples",
            id="fall-confirmed-on-too-few",
        ),
        pytest.param(
            {"hr_drop_peak_samples": 5},
            "hr_drop_peak_samples",
            id="peak-no-longer-than-the-confirmation",
        ),
        pytest.param(
            {"hr_drop_rest_margin_bpm": Bpm(0)},
            "hr_drop_rest_margin_bpm",
            id="zero-rest-margin",
        ),
        pytest.param(
            {"hr_drop_rest_margin_bpm": Bpm(26)},
            "hr_drop_rest_margin_bpm",
            id="rest-margin-above-the-drop",
        ),
        pytest.param(
            {"hr_drop_persist_samples": 1},
            "hr_drop_persist_samples",
            id="fall-held-on-one-reading",
        ),
        pytest.param(
            {"hr_rate_median_samples": 2},
            "hr_rate_median_samples",
            id="rate-median-too-narrow",
        ),
        pytest.param(
            {"hr_rate_median_samples": 4},
            "hr_rate_median_samples",
            id="rate-median-even",
        ),
        pytest.param(
            {"setpoint_echo_dwell": Seconds(-1.0)},
            "setpoint_echo_dwell",
            id="negative-echo-dwell",
        ),
        pytest.param(
            {"unresponsive_bpm_rise": Bpm(0)}, "unresponsive_bpm_rise", id="zero-bpm-rise"
        ),
        pytest.param({"no_load_min_rpm": MotorRpm(0)}, "no_load_min_rpm", id="zero-no-load-rpm"),
        pytest.param(
            {"tracking_error_rpm": MotorRpm(0)}, "tracking_error_rpm", id="zero-tracking-band"
        ),
        pytest.param({"reverse_rpm": MotorRpm(0)}, "reverse_rpm", id="zero-reverse-band"),
        pytest.param({"control_period": Seconds(0.0)}, "control_period", id="zero-period"),
        pytest.param(
            {"hr_hard_max_dwell": Seconds(-1.0)}, "hr_hard_max_dwell", id="negative-hard-max-dwell"
        ),
        pytest.param(
            {"current_warn_dwell": Seconds(-1.0)}, "current_warn_dwell", id="negative-warn-dwell"
        ),
        pytest.param(
            {"no_load_dwell": Seconds(-1.0)}, "no_load_dwell", id="negative-no-load-dwell"
        ),
        pytest.param(
            {"tracking_error_dwell": Seconds(-1.0)},
            "tracking_error_dwell",
            id="negative-tracking-dwell",
        ),
        pytest.param({"overrun_grace": Seconds(-1.0)}, "overrun_grace", id="negative-grace"),
    ],
)
def test_an_incoherent_limit_set_is_refused(override: object, expected: str) -> None:
    """Every invariant is checked individually, and names the field it is about.

    Parametrized one case per invariant rather than one case per validation
    helper: the helpers share their branches, so a single failing case would
    cover the code while proving nothing about the other twenty-nine
    thresholds. The message must name the offending field, because a limit set
    is edited by whoever is commissioning the machine, not by whoever wrote
    this module.
    """
    assert isinstance(override, dict)
    with pytest.raises(ValueError, match=re.escape(expected)):
        replace(LIMITS, **override)


def test_the_derived_release_levels_sit_below_their_limits() -> None:
    """The hysteresis levels and stall gaps are derived, so they cannot drift apart."""
    assert LIMITS.hr_hard_max_release_bpm == HARD_MAX - LIMITS.hr_release_bpm
    assert LIMITS.current_warn_release_a == pytest.approx(
        LIMITS.current_warn_a - LIMITS.current_release_a
    )
    assert LIMITS.loop_stall_freeze_gap == pytest.approx(LIMITS.control_period * 3.0)
    assert LIMITS.loop_stall_silent_gap == pytest.approx(LIMITS.control_period * 15.0)


# =========================================================================
# Structure: what the supervisor is allowed to know
# =========================================================================


def test_the_observation_carries_no_demand_from_the_control_law() -> None:
    """The vasovagal separation, asserted as a shape rather than a comment.

    A person beginning to faint shows a FALLING heart rate, which the control
    law reads as "below the target zone" and answers by SPEEDING UP. The
    supervisor must not be able to weigh that opinion, so there is no field
    through which it could arrive: no ``desired_rpm``, no ``ControlDecision``,
    no ``error_bpm``, no ``in_deadband``, no ``target_bpm``. If somebody later
    adds one, this test is where they have to justify it.
    """
    names = {field.name for field in fields(SafetyObservation)}
    forbidden = {"desired_rpm", "decision", "control", "error_bpm", "in_deadband", "target_bpm"}
    assert names.isdisjoint(forbidden)
    assert "commanded_rpm" in names


def test_the_supervisor_touches_no_hardware() -> None:
    """A verdict is a pure function of evidence, so there is nothing to mock.

    No ``pymodbus``, no ``serial``, no ``atv320``, no ``asyncio``, no socket.
    That is what lets the whole safety layer be tested with no drive attached,
    and it is also why :meth:`SafetySupervisor.latch_estop` can promise not to
    block: there is nothing in here that could.
    """
    imported = _imported_modules(SAFETY_SOURCE)
    assert imported.isdisjoint({"pymodbus", "serial", "asyncio", "socket", "src.motor.atv320"})
    project = {name for name in imported if name.startswith("src.")}
    assert project == {
        "src.clock",
        "src.motor.drive",
        "src.result",
        "src.training.types",
        "src.units",
    }


def test_the_supervisor_never_reads_the_system_clock() -> None:
    """Contract rule 4. There is a repo-wide grep test; this forbids the import too."""
    assert "time" not in _imported_modules(SAFETY_SOURCE)


def test_no_entry_point_is_a_coroutine() -> None:
    """The synchronous entry points must be callable where there is no event loop.

    ``latch_estop`` is called from a request handler, ``trip_from_thread`` from
    the BITalino reader thread, and ``acknowledge`` from either. A disable that
    only exists as a coroutine is a disable that does not happen on the paths
    that need it most.
    """
    assert not inspect.iscoroutinefunction(SafetySupervisor.latch_estop)
    assert not inspect.iscoroutinefunction(SafetySupervisor.trip_from_thread)
    assert not inspect.iscoroutinefunction(SafetySupervisor.acknowledge)
    assert not inspect.iscoroutinefunction(SafetySupervisor.evaluate)
    assert not inspect.iscoroutinefunction(SafetySupervisor.confirm_estop_wiring)


def test_every_rule_id_is_well_formed_and_unique() -> None:
    """Rule ids are stored data: a dashboard, an alert and the log all key on them."""
    assert len(set(ALL_RULES)) == len(ALL_RULES)
    for rule in ALL_RULES:
        assert is_rule_id(rule), rule


def test_all_rules_lists_every_rule_constant_in_the_module() -> None:
    """A rule constant that never reaches ALL_RULES would have no dwell tracker.

    Read out of the module source with a regex rather than from the imported
    module, for two reasons: the failure being guarded against is "somebody
    added ``RULE_X`` and forgot the tuple", which a hand-maintained copy in
    this file would share; and scanning the text keeps the untyped
    ``ast.Constant.value`` out of the test, the same way
    ``tests/test_training_types.py`` does when it cross-checks the ECG
    pipeline.

    The consequence of getting this wrong is not subtle: trackers are
    allocated from ALL_RULES, so a rule firing on an id that is not in it
    would raise ``KeyError`` inside the control loop, with the motor
    commanded.
    """
    source = SAFETY_SOURCE.read_text(encoding="utf-8")
    pattern = re.compile(r'^RULE_[A-Z_]+: Final\[str\] = "([a-z0-9_]+)"$', re.MULTILINE)
    declared = frozenset(pattern.findall(source))

    assert declared == frozenset(ALL_RULES)
    assert len(declared) == len(ALL_RULES)
    assert safety.RULE_OPERATOR_ESTOP in declared


@pytest.mark.parametrize(
    ("record", "field_name"),
    [
        pytest.param(LIMITS, "hard_max_bpm", id="SafetyLimits"),
        pytest.param(Rig().observation(), "now", id="SafetyObservation"),
        pytest.param(ThreadTrip("r", SafetyAction.FREEZE, "d"), "rule", id="ThreadTrip"),
        pytest.param(
            EstopAttestation("op", "stmt", Monotonic(1.0), UnixMillis(2)),
            "operator",
            id="EstopAttestation",
        ),
        pytest.param(
            SafetyAcknowledgement("op", Monotonic(1.0), UnixMillis(2), ()),
            "operator",
            id="SafetyAcknowledgement",
        ),
        pytest.param(EstopUnattested("stmt"), "statement", id="EstopUnattested"),
        pytest.param(Unattributed("d"), "detail", id="Unattributed"),
        pytest.param(GoSilentIsTerminal("r"), "rule", id="GoSilentIsTerminal"),
        pytest.param(EmergencyStopStillLatched(Monotonic(1.0)), "since", id="EstopStillLatched"),
    ],
)
def test_records_are_frozen_and_slotted(record: object, field_name: str) -> None:
    """A record of what was decided must not be editable after the decision.

    Frozen so the session log stays evidence; slotted so a typo creates an
    attribute error rather than a field nothing reads. Assigning a *declared*
    field gives ``FrozenInstanceError``; assigning an undeclared one raises
    whichever of three errors the interpreter reaches first, which is why the
    second assertion accepts a tuple - the property under test is "it does not
    happen", not which exception says so.
    """
    with pytest.raises(FrozenInstanceError):
        _assign(record, field_name, None)
    assert not hasattr(record, "__dict__")
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        _assign(record, "invented_field", 1)
    assert not hasattr(record, "invented_field")


def test_a_quiet_session_produces_no_verdict() -> None:
    """The baseline every other test perturbs must itself be silent.

    Without this, a test that "proves" a rule fires could be reading a verdict
    the baseline was producing all along.
    """
    rig = Rig()
    assert rig.run(Seconds(60.0)) is None
    assert rig.standing() is None
    assert rig.standing_action() is SafetyAction.NONE
    assert rig.floor() is None
    assert rig.live() == ()


# =========================================================================
# drive_fault
# =========================================================================


def test_a_drive_fault_ends_the_session_and_surfaces_the_lft_code() -> None:
    """RAMP_DOWN, not QUICK_STOP: the drive has already applied its own reaction.

    By the time this rule sees a fault, torque is gone and the mass is
    coasting, so there is no stop left to command - only a session to end and
    a code to put in front of the operator. The mnemonic and the raw LFT number
    both have to survive into the detail, because the operator is standing in
    front of a drive that is showing them those four characters.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    code = next(raw for raw, fault in LFT_FAULT_CODES.items() if fault is DriveFault.MOTOR_OVERLOAD)
    rig.fault = describe_fault(code)
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.rule == RULE_DRIVE_FAULT
    assert verdict.latched is True
    assert "OLF" in verdict.detail
    assert f"LFT {code}" in verdict.detail
    assert rig.fault.fault is DriveFault.MOTOR_OVERLOAD


def test_a_drive_fault_with_no_code_read_says_so_rather_than_naming_one() -> None:
    """The status word decides a fault exists; LFT only names it.

    When LFT was not read there is no name to give, and inventing a plausible
    one sends somebody to fix the wrong thing.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.rule == RULE_DRIVE_FAULT
    assert "no LFT code was read" in verdict.detail


def test_a_drive_fault_stays_on_the_floor_after_the_state_clears() -> None:
    """No automatic fault reset, and no automatic resumption of motion.

    A drive whose state went back to OPERATION_ENABLED has not made the fault
    not have happened. The live set drops the rule - the evidence really is
    gone - and the floor keeps the verdict until a human clears it.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    rig.drive_state = DriveState.OPERATION_ENABLED
    rig.tick()

    assert rig.verdict_for(RULE_DRIVE_FAULT) is None
    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_DRIVE_FAULT
    assert rig.standing_action() is SafetyAction.RAMP_DOWN


# =========================================================================
# comms_lost
# =========================================================================


def test_comms_lost_goes_silent_only_at_the_configured_failure_count() -> None:
    """Two failures are a retry; three are a lost link.

    GO_SILENT is the most severe action in the enum because it is the only one
    that does not depend on this process working: writing stops, the drive's
    own ttO timeout expires, and the drive ramps the motor down from the other
    side of the serial link.
    """
    rig = Rig()
    rig.comm_failures = LIMITS.comms_lost_failures - 1
    assert rig.tick() is None

    rig.comm_failures = LIMITS.comms_lost_failures
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.GO_SILENT
    assert verdict.rule == RULE_COMMS_LOST
    assert verdict.latched is True
    assert "ttO" in verdict.detail


def test_comms_lost_stays_silent_after_the_link_returns() -> None:
    """One-way. Nothing in this system resumes writing after GO_SILENT."""
    rig = Rig()
    rig.comm_failures = 5
    rig.tick()
    rig.comm_failures = 0
    rig.tick()

    assert rig.verdict_for(RULE_COMMS_LOST) is None
    assert rig.standing_action() is SafetyAction.GO_SILENT


# =========================================================================
# hr_drop - the vasovagal rule
# =========================================================================


def test_a_falling_heart_rate_ends_the_session_although_it_reads_as_below_zone() -> None:
    """THE VASOVAGAL RULE, and the reason the supervisor is a separate layer.

    The evidence here - a heart rate that has fallen 30 bpm - is exactly what
    the control law reads as "below the target zone" and answers by speeding
    up. The supervisor sees the same numbers and ends the session. Nothing in
    this file matters more than the fact that these two cannot be traded off
    against each other.
    """
    rig = Rig()
    rig.bpm = Bpm(130)
    rig.run(Seconds(10.0))
    assert rig.standing() is None

    rig.bpm = Bpm(100)
    for _ in range(CONFIRMING - 1):
        rig.tick()
        assert rig.verdict_for(RULE_HR_DROP) is None, "one or two readings ended a session"
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.rule == RULE_HR_DROP
    assert verdict.latched is True
    assert "presyncope" in verdict.detail
    assert "accelerating" in verdict.detail


def test_a_fall_smaller_than_the_limit_does_not_end_the_session() -> None:
    """A 20 bpm fall against a 25 bpm limit. Heart rates move on their own."""
    rig = Rig()
    rig.bpm = Bpm(130)
    rig.run(Seconds(10.0))
    rig.bpm = Bpm(110)
    rig.run(Seconds(5.0))

    assert rig.verdict_for(RULE_HR_DROP) is None
    assert rig.floor() is None


def test_a_fall_of_exactly_the_limit_ends_the_session() -> None:
    """The limit is a fall to be reached, not exceeded: 25 bpm fires, 24 does not.

    Pinned because the two comparisons differ by one heartbeat of a person who
    may be losing consciousness, and nothing else in the suite would notice
    which one was written.
    """
    rig = Rig()
    rig.bpm = Bpm(130)
    rig.run(Seconds(10.0))

    rig.bpm = Bpm(130 - LIMITS.hr_drop_bpm + 1)
    for _ in range(LIMITS.hr_drop_confirm_samples):
        rig.tick()
    assert rig.verdict_for(RULE_HR_DROP) is None

    rig.bpm = Bpm(130 - LIMITS.hr_drop_bpm)
    for _ in range(CONFIRMING):
        rig.tick()
    assert rig.action_for(RULE_HR_DROP) is SafetyAction.RAMP_DOWN


def test_the_fall_is_measured_from_the_peak_and_not_from_the_start_of_the_window() -> None:
    """A fall is a fall from wherever the rate actually was.

    The shape here is the one that matters clinically and the one that tells
    the two implementations apart: a heart rate that climbed under load from
    100 to 130 and then dropped to 105. Measured from the peak that is a 25 bpm
    fall and the session ends. Measured from the oldest sample in the window it
    is a 5 bpm *rise*, and a person on their way to fainting reads as somebody
    warming up nicely.
    """
    rig = Rig()
    for bpm in (100, 110, 120, 130):
        rig.bpm = Bpm(bpm)
        rig.run(Seconds(5.0))
    assert rig.verdict_for(RULE_HR_DROP) is None

    rig.bpm = Bpm(105)
    for _ in range(CONFIRMING):
        rig.tick()

    verdict = rig.verdict_for(RULE_HR_DROP)
    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert "from 130 to 105" in verdict.detail


def test_a_slow_decline_across_more_than_the_window_does_not_fire() -> None:
    """The window is real, and so is the pruning that keeps it a window.

    A fall of 30 bpm spread evenly over 60 s never exceeds 15 bpm inside any
    30 s window, and it is not presyncope - it is a heart rate coming down
    because the load did. If the history were never pruned, the peak would stay
    in view forever and this would fire, which is why this test watches every
    tick rather than only the last one.
    """
    rig = Rig()
    start = Bpm(140)
    total = Seconds(60.0)
    steps = int(total / rig.limits.control_period)
    for step in range(steps + 1):
        rig.bpm = Bpm(start - step * 30 // steps)
        rig.tick()
        assert rig.verdict_for(RULE_HR_DROP) is None

    # The self-check that stops this from being vacuous: the total decline is
    # larger than the limit, so it is only the WINDOW that keeps the rule quiet.
    final = rig.bpm
    assert final is not None
    assert start - final >= LIMITS.hr_drop_bpm
    assert rig.floor() is None


def test_one_or_two_artefact_readings_never_end_the_session() -> None:
    """An ectopic beat counted into a rate, a motion spike: the fall is confirmed or it is not.

    The cohort's ectopic subjects never left BASELINE: one reading 99 -> 71 bpm
    at t = 0 was a "fall" of 28 bpm. Now the level is the median of the last
    five fresh readings, so two artefacts in a row move nothing - downwards, or
    upwards into a false peak.
    """
    rig = Rig()
    rig.bpm = Bpm(80)
    rig.run(Seconds(10.0))
    for spike in (Bpm(40), Bpm(110)):
        rig.bpm = spike
        rig.tick()
        rig.tick()
        rig.bpm = Bpm(80)
        rig.run(Seconds(5.0))
    rig.bpm = Bpm(110)
    rig.tick()
    rig.bpm = Bpm(80)
    rig.run(Seconds(5.0))
    assert rig.verdict_for(RULE_HR_DROP) is None
    assert rig.floor() is None


def test_a_burst_of_three_artefacts_does_not_end_the_session() -> None:
    """S27: 97 -> 71, 73, 72 bpm with the true rate steady at 97. Moves the median, briefly.

    Three lows in a row put the median of five at the low level for exactly
    three readings; the fall must hold on four. Four lows in a row hold it on
    four readings, and that fires.
    """
    rig = Rig()
    rig.bpm = Bpm(97)
    rig.run(Seconds(12.0))
    for low in (71, 72, 71):
        rig.bpm = Bpm(low)
        rig.tick()
    rig.bpm = Bpm(97)
    rig.run(Seconds(5.0))
    assert rig.verdict_for(RULE_HR_DROP) is None
    assert rig.floor() is None
    for low in (71, 72, 71, 72):
        rig.bpm = Bpm(low)
        rig.tick()
    rig.bpm = Bpm(97)
    rig.run(Seconds(2.0))
    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_HR_DROP


def test_a_peak_that_is_one_reading_long_is_no_peak_at_all() -> None:
    """With fewer fresh samples in the window than the peak median needs, nothing is judged."""
    rig = Rig()
    rig.bpm = Bpm(140)
    for _ in range(LIMITS.hr_drop_peak_samples - 2):
        rig.tick()
    rig.bpm = Bpm(60)
    rig.tick()
    assert rig.verdict_for(RULE_HR_DROP) is None


def test_the_planned_unload_of_a_cooldown_is_not_presyncope() -> None:
    """THE finding: hr_drop fired in the programme's own COOLDOWN for 10 of 25 subjects.

    A heart recovering from a load that is being removed falls fast - 25 bpm in
    30 s for a fast responder. It LAGS the steady state of the load still on,
    ``rest + (peak - rest) * g_now / g_ref``, so the same fall is judged against
    what the remaining load explains and it does not fire.
    """
    rig = Rig()
    rig.resting = Bpm(80)
    rig.commanded = MotorRpm(1000)
    rig.measured = rig.commanded
    rig.bpm = Bpm(140)
    rig.run(Seconds(40.0))
    # The load comes off over 30 s; the heart follows it down 50 bpm, lagging above.
    for step in range(150):
        fraction = 1.0 - (step + 1) / 150
        rig.commanded = MotorRpm(round(1000 * math.sqrt(fraction)))
        rig.measured = rig.commanded
        explained = 80 + 60 * fraction
        rig.bpm = Bpm(round(explained + 10))
        rig.tick()
        assert rig.verdict_for(RULE_HR_DROP) is None, (step, rig.commanded, rig.bpm)
    assert rig.floor() is None


def test_a_collapse_during_the_unload_is_still_caught() -> None:
    """Below what the remaining load explains, by the margin, is still presyncope."""
    rig = Rig()
    rig.resting = Bpm(80)
    rig.commanded = MotorRpm(1000)
    rig.bpm = Bpm(140)
    rig.run(Seconds(40.0))
    rig.commanded = MotorRpm(700)  # about half the load (g goes as rpm squared)
    # Explained: 80 + 60 x 0.49 = 109 bpm; the margin there is 15 + 10 x 0.49 = 20.
    rig.bpm = Bpm(90)
    for _ in range(CONFIRMING):
        rig.tick()
    assert rig.verdict_for(RULE_HR_DROP) is None, "within what the unloading explains"
    rig.bpm = Bpm(85)
    for _ in range(CONFIRMING):
        rig.tick()
    verdict = rig.verdict_for(RULE_HR_DROP)
    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN


def test_a_collapse_in_recovery_below_the_resting_rate_is_caught() -> None:
    """RECOVERY: the load is gone, the motor stopped - and the highest vasovagal risk.

    A fall to 15 bpm below the resting rate, 20 bpm inside the window, fires
    although it is smaller than the 25 bpm the loaded rule asks for.
    """
    rig = Rig()
    rig.phase = Phase.RECOVERY
    rig.resting = Bpm(80)
    rig.commanded = MotorRpm(800)
    rig.bpm = Bpm(85)
    rig.run(Seconds(10.0))
    rig.commanded = MotorRpm(0)
    rig.run(Seconds(20.0))
    assert rig.verdict_for(RULE_HR_DROP) is None
    rig.bpm = Bpm(65)
    for _ in range(CONFIRMING):
        rig.tick()
    verdict = rig.verdict_for(RULE_HR_DROP)
    assert verdict is not None
    assert "0% of the recent peak" in verdict.detail


def test_a_heart_that_settles_below_an_anxious_baseline_is_no_collapse() -> None:
    """Below the resting rate but no FALL: a baseline measured high is not presyncope later."""
    rig = Rig()
    rig.phase = Phase.RECOVERY
    rig.resting = Bpm(95)
    rig.commanded = MotorRpm(0)
    rig.bpm = Bpm(72)
    rig.run(Seconds(60.0))
    rig.bpm = Bpm(70)
    rig.run(Seconds(10.0))
    assert rig.verdict_for(RULE_HR_DROP) is None


def test_with_no_load_recorded_the_fall_is_judged_unconditionally() -> None:
    """No load stated, or none ever applied: the 25 bpm rule, whatever the resting rate."""
    for known in (False, True):
        rig = Rig()
        rig.load_known = known
        rig.resting = Bpm(80)
        rig.commanded = MotorRpm(0)
        rig.bpm = Bpm(130)
        rig.run(Seconds(10.0))
        rig.bpm = Bpm(106)
        rig.run(Seconds(2.0))
        assert rig.verdict_for(RULE_HR_DROP) is None, known
        rig.bpm = Bpm(105)
        rig.run(Seconds(2.0))
        assert rig.action_for(RULE_HR_DROP) is SafetyAction.RAMP_DOWN, known


@settings(max_examples=200, deadline=None)
@given(
    base=st.integers(min_value=50, max_value=170),
    outliers=st.lists(
        st.tuples(st.integers(min_value=0, max_value=59), st.integers(min_value=-60, max_value=60)),
        max_size=12,
    ),
)
def test_isolated_outliers_on_a_steady_heart_never_end_the_session(
    base: int, outliers: list[tuple[int, int]]
) -> None:
    """PROPERTY: a steady heart with any outliers no two of which fall within three readings.

    However large they are, in either direction, at any load and with or without
    a resting rate, they can be neither a confirmed level nor a confirmed peak.
    """
    kept: dict[int, int] = {}
    for index, offset in sorted(outliers):
        if all(abs(index - other) > 2 for other in kept):
            kept[index] = offset
    rig = Rig()
    rig.resting = Bpm(base)
    rig.commanded = MotorRpm(600)
    for index in range(60):
        rig.bpm = Bpm(max(25, base + kept.get(index, 0)))
        rig.tick(Seconds(1.0))
        assert rig.verdict_for(RULE_HR_DROP) is None, (index, kept)


def test_the_trend_rules_need_more_than_one_sample() -> None:
    """A fall and a slope both need two points. One sample is not a trend."""
    rig = Rig()
    rig.bpm = Bpm(140)
    rig.tick()

    assert rig.verdict_for(RULE_HR_DROP) is None
    assert rig.verdict_for(RULE_HR_RATE) is None


# =========================================================================
# hr_hard_max
# =========================================================================


def test_the_hard_maximum_dwell_is_exactly_five_seconds() -> None:
    """Checked at 4.5 s (silent) and 5.0 s (firing), by advancing exactly those.

    Both values are exact in binary, so this measures the dwell rather than
    floating-point luck.
    """
    rig = Rig()
    rig.bpm = Bpm(160)
    rig.tick(Seconds(0.5))
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None

    rig.tick(Seconds(4.5))
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_HR_HARD_MAX)

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched is True
    assert "160" in verdict.detail
    assert "5.0 s" in verdict.detail


def test_a_rate_exactly_at_the_hard_maximum_does_not_fire() -> None:
    """The limit is a ceiling to be exceeded, not to be reached."""
    rig = Rig()
    rig.bpm = HARD_MAX
    rig.run(Seconds(60.0))

    assert rig.verdict_for(RULE_HR_HARD_MAX) is None
    assert rig.floor() is None


def test_the_hard_maximum_still_fires_when_the_rate_oscillates_across_the_limit() -> None:
    """The hysteresis band exists for this case, and it is a safety case.

    Without a release band, every dip below the limit resets the dwell, and a
    heart rate hovering on its ceiling - which is precisely what a heart rate
    at its ceiling does - would never accumulate five continuous seconds. The
    rule would fail silent, on the one signal that most needs it. With the
    band, the dwell accumulates from the first crossing and only restarts when
    the rate genuinely comes down.
    """
    rig = Rig()
    high = Bpm(155)
    inside_band = Bpm(HARD_MAX - LIMITS.hr_release_bpm + 2)
    assert inside_band < HARD_MAX
    assert inside_band > LIMITS.hr_hard_max_release_bpm

    for step in range(40):
        rig.bpm = high if step % 2 == 0 else inside_band
        rig.tick(Seconds(0.25))

    verdict = rig.verdict_for(RULE_HR_HARD_MAX)
    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN


def test_the_hard_maximum_dwell_restarts_when_the_rate_genuinely_comes_down() -> None:
    """Below the release level is a release: the next five seconds start again.

    The mirror of the test above. The band must not be so generous that a rate
    which really recovered keeps its old dwell - "for five seconds" has to mean
    five continuous seconds of being above the limit.
    """
    rig = Rig()
    rig.bpm = Bpm(160)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(4.0))
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None

    rig.bpm = Bpm(LIMITS.hr_hard_max_release_bpm - 5)
    rig.tick(Seconds(0.5))
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None

    rig.bpm = Bpm(160)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(4.0))
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None

    rig.tick(Seconds(1.0))
    assert rig.action_for(RULE_HR_HARD_MAX) is SafetyAction.RAMP_DOWN


# =========================================================================
# hr_critical
# =========================================================================


def test_the_critical_rate_zeroes_the_reference_with_no_dwell() -> None:
    """No dwell, because at this rate a dwell is more exposure.

    The reading is already an 8 s median refreshed at 1 Hz, so the averaging a
    dwell would add has been done upstream. QUICK_STOP means zero the speed
    reference now and leave the run command in place - a faster demand trips
    the DC bus into freewheel, which is slower.
    """
    rig = Rig()
    rig.bpm = CRITICAL
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.QUICK_STOP
    assert verdict.rule == RULE_HR_CRITICAL
    assert verdict.latched is True
    assert "own ramp" in verdict.detail


def test_a_rate_just_below_critical_does_not_quick_stop() -> None:
    rig = Rig()
    rig.bpm = Bpm(CRITICAL - 1)
    rig.tick()

    assert rig.verdict_for(RULE_HR_CRITICAL) is None


def test_a_re_emitted_reading_still_counts_for_the_critical_level() -> None:
    """Conservative on purpose, and the asymmetry is deliberate.

    A re-emitted metrics dict is not new evidence, and the trend rules ignore
    it. But a dangerously high heart rate that we have merely stopped being
    able to confirm is not evidence of recovery, so the level rules keep acting
    on the last usable reading - and ``hr_stale`` is what escalates the fact
    that it is no longer fresh.
    """
    rig = Rig()
    rig.bpm = CRITICAL
    rig.fresh = False
    rig.tick()

    assert rig.action_for(RULE_HR_CRITICAL) is SafetyAction.QUICK_STOP


def test_an_untrustworthy_grade_yields_no_heart_rate_at_all() -> None:
    """Only ``good`` permits a rate to be acted on. Never fabricate a vital sign.

    The pipeline does not report a rate below ``good`` in the first place, so
    this pairing cannot arise from it - which is exactly why the gate is
    asserted here rather than assumed.
    """
    rig = Rig()
    rig.bpm = Bpm(200)
    rig.quality = SignalQuality.NOISY
    rig.tick()

    assert rig.verdict_for(RULE_HR_CRITICAL) is None
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None


def test_a_missing_sample_yields_no_heart_rate_verdict() -> None:
    """No sample at all is not a rate of zero, and not a rate of anything else."""
    rig = Rig()
    rig.heart_rate_present = False
    rig.tick()

    assert rig.verdict_for(RULE_HR_CRITICAL) is None
    assert rig.verdict_for(RULE_HR_HARD_MAX) is None


def test_no_heart_rate_or_attendant_rule_fires_once_the_session_is_done() -> None:
    """DONE is the one phase that suspends them, and RECOVERY is not DONE.

    RECOVERY keeps the person in the machine with the motor stopped, which is
    physiologically the highest-risk phase; it stays fully supervised. DONE
    means the programme is over, and a rig that alarms forever about an absent
    heart rate teaches its operator to ignore the alarm that matters.
    """
    rig = Rig()
    rig.phase = Phase.DONE
    rig.heart_rate_present = False
    rig.attendant_present = False
    rig.run(Seconds(300.0))

    assert rig.live() == ()
    assert rig.standing() is None


def test_the_recovery_phase_is_still_supervised() -> None:
    """The motor is stopped but the person is not out of the machine."""
    rig = Rig()
    rig.phase = Phase.RECOVERY
    rig.bpm = CRITICAL
    rig.tick()

    assert rig.action_for(RULE_HR_CRITICAL) is SafetyAction.QUICK_STOP


# =========================================================================
# hr_rate
# =========================================================================


def test_a_heart_rate_rising_too_fast_reduces_the_speed() -> None:
    """Half a bpm per second is 30 bpm/min, against a bound of 25."""
    rig = Rig()
    steps = int(Seconds(30.0) / rig.limits.control_period)
    for step in range(steps):
        rig.bpm = Bpm(100 + (step * 5 // 50))
        rig.tick()

    verdict = rig.verdict_for(RULE_HR_RATE)
    assert verdict is not None
    assert verdict.action is SafetyAction.REDUCE
    assert verdict.latched is False
    assert "bpm/min" in verdict.detail
    assert rig.floor() is None


def test_a_heart_rate_rising_slowly_does_not_reduce_the_speed() -> None:
    """Six bpm/min is what a warm-up looks like."""
    rig = Rig()
    steps = int(Seconds(60.0) / rig.limits.control_period)
    for step in range(steps):
        rig.bpm = Bpm(100 + (step // 50))
        rig.tick()

    assert rig.verdict_for(RULE_HR_RATE) is None


def _ramp(rig: Rig, *, ticks: int, step: int, every: Seconds) -> None:
    """``ticks`` fresh readings, each ``step`` bpm above the last, ``every`` seconds apart."""
    for _ in range(ticks):
        bpm = rig.bpm
        assert bpm is not None
        rig.bpm = Bpm(bpm + step)
        rig.tick(every)


def test_the_rate_rule_waits_for_a_long_enough_span_but_does_not_forget() -> None:
    """A slope needs a span, and the guard delays the conclusion without discarding it.

    A sustained rise of 2 bpm every 3 s is 40 bpm/min. The medians of five
    readings trail the raw ones by two readings, so their span reaches the
    20 s minimum on the twelfth reading (7 medians x 3 s = 21 s) - not before,
    and then it fires at once: the fitted slope of a straight line is exact.

    This used to be a 12 bpm STEP read as a 29 bpm/min rate once 25 s had
    passed: the endpoint difference that let one reading be the whole rate.
    """
    rig = Rig()
    rig.bpm = Bpm(100)
    rig.tick(Seconds(3.0))
    _ramp(rig, ticks=10, step=2, every=Seconds(3.0))
    assert rig.verdict_for(RULE_HR_RATE) is None
    _ramp(rig, ticks=1, step=2, every=Seconds(3.0))
    verdict = rig.verdict_for(RULE_HR_RATE)
    assert verdict is not None
    assert verdict.action is SafetyAction.REDUCE
    assert "40.0 bpm/min" in verdict.detail


def test_a_single_step_is_not_a_rate_of_rise() -> None:
    """The old rule's arithmetic: 100 -> 112 once, then flat, read as 29 bpm/min. Not any more."""
    rig = Rig()
    rig.tick()
    rig.bpm = Bpm(112)
    rig.run(Seconds(25.0))
    assert rig.verdict_for(RULE_HR_RATE) is None


def test_the_rate_rule_holds_inside_its_release_band_and_clears_below_it() -> None:
    """Hysteresis on an advisory, with every slope an exact ratio of integers.

    Readings 3 s apart, so the fitted line through a straight run of them is
    exact and derived here rather than read back from the module:

    * +2 bpm per reading = 40 bpm/min, above the 25 bpm/min bound: fires;
    * then +1 per reading = 20 bpm/min for more than the whole 60 s window:
      BELOW the bound and ABOVE the 15 bpm/min release level, so it holds;
    * then flat for more than the window: 0 bpm/min, and it clears.

    A fresh rig that only ever sees the 20 bpm/min ramp never fires at all.
    """
    rig = Rig()
    rig.bpm = Bpm(100)
    rig.tick(Seconds(3.0))
    _ramp(rig, ticks=12, step=2, every=Seconds(3.0))
    assert rig.action_for(RULE_HR_RATE) is SafetyAction.REDUCE
    band = 1 / 3.0 * 60.0
    assert LIMITS.hr_rise_release < band < LIMITS.hr_rise_limit
    for _ in range(24):
        _ramp(rig, ticks=1, step=1, every=Seconds(3.0))
        assert rig.action_for(RULE_HR_RATE) is SafetyAction.REDUCE
    _ramp(rig, ticks=22, step=0, every=Seconds(3.0))
    assert rig.action_for(RULE_HR_RATE) is SafetyAction.NONE
    assert rig.floor() is None

    fresh = Rig()
    fresh.bpm = Bpm(100)
    fresh.tick(Seconds(3.0))
    _ramp(fresh, ticks=30, step=1, every=Seconds(3.0))
    assert fresh.verdict_for(RULE_HR_RATE) is None


@settings(max_examples=200, deadline=None)
@given(
    base=st.integers(min_value=60, max_value=150),
    slope_per_min=st.floats(min_value=-30.0, max_value=20.0),
    outliers=st.lists(
        st.tuples(st.integers(min_value=0, max_value=89), st.integers(min_value=-40, max_value=40)),
        max_size=20,
    ),
)
def test_isolated_outliers_never_trip_the_rate_rule(
    base: int, slope_per_min: float, outliers: list[tuple[int, int]]
) -> None:
    """PROPERTY: a heart rising slower than the release level, with any isolated outliers.

    Outliers no two of which fall within a median's width of each other (the
    ectopic and motion-spike case), of any size and sign, at any of 90 readings
    1 s apart, never raise hr_rate on a heart whose own trend is below the
    release level: every median is a clean reading, so the fitted line is the
    heart's.
    """
    width = LIMITS.hr_rate_median_samples
    kept: dict[int, int] = {}
    for index, offset in sorted(outliers):
        if all(abs(index - other) >= width for other in kept):
            kept[index] = offset
    rig = Rig()
    for index in range(90):
        clean = base + slope_per_min * index / 60.0
        rig.bpm = Bpm(max(25, round(clean) + kept.get(index, 0)))
        rig.tick(Seconds(1.0))
        assert rig.verdict_for(RULE_HR_RATE) is None, (index, kept)


@settings(max_examples=100, deadline=None)
@given(
    base=st.integers(min_value=60, max_value=110),
    slope_per_min=st.floats(min_value=27.0, max_value=90.0),
)
def test_a_sustained_rise_beyond_the_limit_always_trips_within_its_span(
    base: int, slope_per_min: float
) -> None:
    """PROPERTY: a sustained rise faster than the limit trips within N = 20 + 5 readings.

    Readings 1 s apart rising at more than 25 bpm/min (27+: whole-bpm rounding
    can take a little off a fitted slope): the medians' span reaches 20 s on the
    25th reading at the latest, and the rule has fired by then.
    """
    rig = Rig()
    fired_at: int | None = None
    for index in range(40):
        rig.bpm = Bpm(round(base + slope_per_min * index / 60.0))
        rig.tick(Seconds(1.0))
        if fired_at is None and rig.verdict_for(RULE_HR_RATE) is not None:
            fired_at = index + 1
    assert fired_at is not None
    assert fired_at <= 20 + LIMITS.hr_rate_median_samples, fired_at


def test_a_rate_over_no_spread_of_time_or_absurd_time_is_unknown_not_zero() -> None:
    """The fit's own guards: no spread in time, or a spread too large to square."""
    slope = safety._least_squares_slope  # pyright: ignore[reportPrivateUsage]  # the guard itself
    assert slope((5.0, 5.0, 5.0), (1.0, 2.0, 3.0)) is None
    assert slope((0.0, 1e200, 2e200), (1.0, 2.0, 3.0)) is None
    assert slope((0.0, 1.0, 2.0), (1.0, 2.0, 3.0)) == pytest.approx(1.0)


# =========================================================================
# hr_stale
# =========================================================================


def test_staleness_escalates_at_exactly_ten_thirty_and_sixty_seconds() -> None:
    """FREEZE, then REDUCE, then RAMP_DOWN, on exact boundaries.

    FREEZE first because with no trustworthy heart rate, regulating is
    guessing and the last commanded speed is the only value known to have been
    survivable a moment ago. Only the third level latches: deciding to end a
    session is not a decision that un-makes itself, while the first two release
    as soon as the pipeline comes back.
    """
    rig = Rig()
    rig.tick()
    rig.heart_rate_present = False

    rig.tick(Seconds(10.0))
    assert rig.verdict_for(RULE_HR_STALE) is None

    rig.tick(Seconds(0.5))
    freeze = rig.verdict_for(RULE_HR_STALE)
    assert freeze is not None
    assert freeze.action is SafetyAction.FREEZE
    assert freeze.latched is False
    assert "re-emitted" in freeze.detail

    rig.tick(Seconds(19.5))
    assert rig.action_for(RULE_HR_STALE) is SafetyAction.FREEZE
    rig.tick(Seconds(0.5))
    reduce = rig.verdict_for(RULE_HR_STALE)
    assert reduce is not None
    assert reduce.action is SafetyAction.REDUCE
    assert reduce.latched is False
    assert rig.floor() is None

    rig.tick(Seconds(29.5))
    assert rig.action_for(RULE_HR_STALE) is SafetyAction.REDUCE
    rig.tick(Seconds(0.5))
    ramp = rig.verdict_for(RULE_HR_STALE)
    assert ramp is not None
    assert ramp.action is SafetyAction.RAMP_DOWN
    assert ramp.latched is True

    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_HR_STALE


def test_the_verdict_instant_does_not_move_while_a_rule_keeps_firing() -> None:
    """``since`` is when the verdict first stood, not when it was last re-checked.

    Dwell time is what separates a spike from a trend, so a re-fired verdict
    has to keep its original instant - and an escalating one keeps it too,
    which is how an operator can see that a FREEZE became a RAMP_DOWN rather
    than that two unrelated things happened.
    """
    rig = Rig()
    rig.tick()
    rig.heart_rate_present = False
    rig.tick(Seconds(10.5))

    first = rig.verdict_for(RULE_HR_STALE)
    assert first is not None
    rig.tick(Seconds(5.0))
    later = rig.verdict_for(RULE_HR_STALE)
    assert later is not None

    assert later.since == first.since
    assert later.age(rig.clock.monotonic()) == pytest.approx(5.0)


def test_a_re_emitted_metrics_dict_is_not_fresh_evidence() -> None:
    """THE input trap of this system, and it looks exactly like healthy data.

    ``src/signal_processing.py`` re-emits its previous metrics dict whenever
    extraction fails. Every field of the sample below is impeccable - a
    plausible rate, a ``good`` grade, a timestamp from this very instant - and
    the only thing wrong with it is that the sequence number did not move,
    which means nothing was measured. A supervisor that trusted the timestamp
    would report a healthy pipeline for as long as the failure lasted.
    """
    rig = Rig()
    rig.tick()
    rig.fresh = False

    rig.tick(Seconds(10.0))
    assert rig.verdict_for(RULE_HR_STALE) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_HR_STALE)

    assert verdict is not None
    assert verdict.action is SafetyAction.FREEZE
    # The sample really did look perfect the whole time.
    sample = rig.observation().heart_rate
    assert sample is not None
    assert sample.usable_bpm == Bpm(100)
    assert sample.at == rig.clock.monotonic()


def test_an_untrustworthy_grade_is_not_fresh_evidence_either() -> None:
    """The sequence number advances on a bad grade; the evidence still is not usable."""
    rig = Rig()
    rig.tick()
    rig.quality = SignalQuality.MAINS_DOMINATED

    rig.tick(Seconds(10.5))

    assert rig.action_for(RULE_HR_STALE) is SafetyAction.FREEZE


def test_staleness_is_measured_from_the_session_start_when_no_reading_ever_arrives() -> None:
    """A reading that has never happened is not a recent one."""
    rig = Rig()
    rig.heart_rate_present = False
    rig.tick(Seconds(10.5))

    verdict = rig.verdict_for(RULE_HR_STALE)
    assert verdict is not None
    assert verdict.action is SafetyAction.FREEZE
    assert "none has ever arrived" in verdict.detail


def test_a_returning_heart_rate_releases_the_freeze() -> None:
    """An advisory disappears with its evidence. That is what unlatched means."""
    rig = Rig()
    rig.tick()
    rig.heart_rate_present = False
    rig.tick(Seconds(10.5))
    assert rig.action_for(RULE_HR_STALE) is SafetyAction.FREEZE

    rig.heart_rate_present = True
    rig.tick()

    assert rig.verdict_for(RULE_HR_STALE) is None
    assert rig.standing() is None
    assert rig.floor() is None


def test_a_returning_heart_rate_does_not_release_the_ramp_down() -> None:
    """A minute without a heart rate ended the session. It stays ended."""
    rig = Rig()
    rig.tick()
    rig.heart_rate_present = False
    rig.tick(Seconds(60.5))
    assert rig.action_for(RULE_HR_STALE) is SafetyAction.RAMP_DOWN

    rig.heart_rate_present = True
    rig.tick()

    assert rig.verdict_for(RULE_HR_STALE) is None
    assert rig.standing_action() is SafetyAction.RAMP_DOWN


# =========================================================================
# hr_unresponsive
# =========================================================================


def _ramp_the_speed(rig: Rig, *, duration: Seconds, top: MotorRpm, bpm_per_minute: float) -> None:
    """Drive a linear speed ramp with the measured speed following it.

    The measured speed tracks the command so ``tracking_error`` stays quiet:
    this helper is for the rules that judge the heart rate against the speed,
    and a rig that also tripped the tracking rule would be testing two things.
    """
    steps = int(duration)
    for step in range(steps):
        fraction = (step + 1) / steps
        rig.commanded = MotorRpm(int(top * fraction))
        rig.measured = rig.commanded
        rig.bpm = Bpm(100 + int(bpm_per_minute * (step + 1) / 60.0))
        rig.tick(Seconds(1.0))


def test_a_heart_rate_that_ignores_the_speed_reduces_it() -> None:
    """A non-responder, or a sensor reporting something unrelated to the person.

    Either way the control loop is open while believing it is closed, and an
    open loop looking for a response it will never see keeps increasing speed.

    To 1200 motor rpm (24 output rpm, ~1 g at 1.5 m): the rule is judged
    against the LOAD now, and the 400 rpm this used to ramp to is 0.1 g - below
    the minimum load at which a flat heart rate means anything.
    """
    rig = Rig()
    _ramp_the_speed(rig, duration=Seconds(300.0), top=MotorRpm(1200), bpm_per_minute=0.0)

    verdict = rig.verdict_for(RULE_HR_UNRESPONSIVE)
    assert verdict is not None
    assert verdict.action is SafetyAction.REDUCE
    assert verdict.latched is False
    assert "non-responder" in verdict.detail
    assert rig.floor() is None


def test_a_heart_rate_that_does_follow_the_speed_is_left_alone() -> None:
    """Twelve bpm of response across five minutes is a response."""
    rig = Rig()
    _ramp_the_speed(rig, duration=Seconds(300.0), top=MotorRpm(1200), bpm_per_minute=6.0)

    assert rig.verdict_for(RULE_HR_UNRESPONSIVE) is None
    assert rig.standing() is None


def test_a_flat_heart_at_a_low_load_is_physiology_not_a_non_responder() -> None:
    """THE finding: every nominal jog warm-up fired this rule at ~350 s and its REDUCE reversed it.

    The first 150 motor rpm of a warm-up are a few hundredths of a g. A heart
    that has not moved yet at that load is physiology; judged in rpm it read as
    unresponsive. Judged in g, a rise to 400 rpm (0.1 g) never qualifies.
    """
    rig = Rig()
    _ramp_the_speed(rig, duration=Seconds(300.0), top=MotorRpm(400), bpm_per_minute=0.0)
    assert rig.verdict_for(RULE_HR_UNRESPONSIVE) is None


def test_a_high_load_that_did_not_rise_is_not_judged_unresponsive() -> None:
    """Above the minimum load but steady: nothing was asked of the heart, nothing is concluded."""
    rig = Rig()
    rig.commanded = MotorRpm(1200)
    rig.measured = rig.commanded
    rig.run(Seconds(300.0))
    assert rig.verdict_for(RULE_HR_UNRESPONSIVE) is None


def test_an_observation_that_states_no_load_leaves_the_rule_unjudged() -> None:
    """No load stated, no response to judge: the rule releases rather than guess one."""
    rig = Rig()
    rig.load_known = False
    _ramp_the_speed(rig, duration=Seconds(300.0), top=MotorRpm(1200), bpm_per_minute=0.0)
    assert rig.verdict_for(RULE_HR_UNRESPONSIVE) is None


def test_the_unresponsive_rule_will_not_conclude_from_a_short_window() -> None:
    """Heart rate lags load by 30-60 s, so a short window would libel physiology."""
    rig = Rig()
    _ramp_the_speed(rig, duration=Seconds(100.0), top=MotorRpm(400), bpm_per_minute=0.0)

    assert rig.verdict_for(RULE_HR_UNRESPONSIVE) is None


def test_evidence_older_than_the_longest_window_is_forgotten() -> None:
    """Retention is bounded by time, not only by the deque's own maxlen.

    Three rules filter the retained history on every tick, at 5 Hz, on a Pi.
    Without pruning, a 45-minute session would leave them walking three
    thousand samples to answer a question about the last thirty seconds - and
    the memory would grow for as long as the session ran. Pruning is what keeps
    the cost of a window proportional to the window.

    Asserted through :attr:`SafetySupervisor.retained_samples` because the
    behaviour it protects is a bound rather than a verdict: every rule already
    filters by age, so a test that only watched verdicts could not tell a
    pruned history from one that had kept everything.
    """
    rig = Rig()
    longest = max(LIMITS.hr_drop_window, LIMITS.hr_rate_window, LIMITS.unresponsive_window)
    rig.run(Seconds(30.0))
    assert rig.supervisor.retained_samples > 1

    rig.tick(Seconds(longest + 1.0))

    assert rig.supervisor.retained_samples == 1
    assert rig.live() == ()


# =========================================================================
# current_high
# =========================================================================


def test_current_above_the_warning_level_reduces_after_ten_seconds() -> None:
    """The most valuable single number on the screen, and the cheapest.

    A centrifuge beginning to rub its enclosure appears in the current long
    before it appears in the speed, in the heart rate, or to anybody watching.
    Ten seconds of dwell tolerates an acceleration transient and not a rub.
    """
    rig = Rig()
    rig.current = Amperes(2.5)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(9.5))
    assert rig.verdict_for(RULE_CURRENT_HIGH) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_CURRENT_HIGH)

    assert verdict is not None
    assert verdict.action is SafetyAction.REDUCE
    assert verdict.latched is False
    assert "10.0 s" in verdict.detail
    assert rig.floor() is None


def test_current_above_the_trip_level_ends_the_session_at_once() -> None:
    """No dwell at one and a half times nameplate: that is not a transient."""
    rig = Rig()
    rig.current = Amperes(3.5)
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.rule == RULE_CURRENT_HIGH
    assert verdict.latched is True
    assert "2.15 A" in verdict.detail


def test_the_current_rule_holds_inside_its_release_band_and_clears_below_it() -> None:
    """2.3 A is below the 2.4 A warning level and above the 2.2 A release level."""
    rig = Rig()
    rig.current = Amperes(2.5)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(10.0))
    assert rig.action_for(RULE_CURRENT_HIGH) is SafetyAction.REDUCE

    rig.current = Amperes(2.3)
    rig.tick()
    assert LIMITS.current_warn_release_a < 2.3 < LIMITS.current_warn_a
    assert rig.action_for(RULE_CURRENT_HIGH) is SafetyAction.REDUCE

    rig.current = Amperes(2.1)
    rig.tick()
    assert rig.verdict_for(RULE_CURRENT_HIGH) is None
    assert rig.floor() is None


def test_an_unreadable_current_releases_the_current_rule() -> None:
    """A failed read is an absence of evidence, and ``comms_lost`` owns that."""
    rig = Rig()
    rig.current = Amperes(2.5)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(10.0))
    assert rig.action_for(RULE_CURRENT_HIGH) is SafetyAction.REDUCE

    rig.current = None
    rig.tick()

    assert rig.verdict_for(RULE_CURRENT_HIGH) is None


# =========================================================================
# no_load
# =========================================================================


def test_no_current_while_the_machine_should_be_turning_ends_the_session() -> None:
    """An open phase, a disconnected motor, or the wrong current register.

    All three mean the software is commanding a machine it is not driving, and
    regulating on feedback that describes nothing.
    """
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(500)
    rig.current = Amperes(0.05)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(2.5))
    assert rig.verdict_for(RULE_NO_LOAD) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_NO_LOAD)

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched is True
    assert "open" in verdict.detail
    assert "wrong current register" in verdict.detail


def test_no_load_does_not_fire_below_the_speed_at_which_current_is_expected() -> None:
    """At 50 motor rpm there is nothing to conclude from a small current."""
    rig = Rig()
    rig.commanded = MotorRpm(50)
    rig.measured = MotorRpm(50)
    rig.current = Amperes(0.05)
    rig.run(Seconds(60.0))

    assert rig.verdict_for(RULE_NO_LOAD) is None
    assert rig.floor() is None


def test_no_load_does_not_fire_while_the_output_stage_is_not_enabled() -> None:
    """No torque is commanded, so no current is expected. Nothing is wrong here."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(0)
    rig.current = Amperes(0.0)
    rig.drive_state = DriveState.SWITCHED_ON
    rig.run(Seconds(30.0))

    assert rig.verdict_for(RULE_NO_LOAD) is None
    assert rig.verdict_for(RULE_TRACKING_ERROR) is None


def test_an_unknown_current_cannot_prove_there_is_no_load() -> None:
    """Absence of evidence is not evidence of no load."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(500)
    rig.current = None
    rig.run(Seconds(30.0))

    assert rig.verdict_for(RULE_NO_LOAD) is None


# =========================================================================
# tracking_error
# =========================================================================


def test_commanded_and_measured_speed_diverging_ends_the_session() -> None:
    """Stalled, overloaded - or writing the setpoint into the wrong register.

    Most Altivar parameters are writable while the drive runs, so an address
    off by one does not bounce: it writes the speed reference into whatever
    parameter is next door, and every subsequent read looks normal. The shaft
    not following the command is how that becomes visible.
    """
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(100)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(1.5))
    assert rig.verdict_for(RULE_TRACKING_ERROR) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_TRACKING_ERROR)

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched is True
    assert "400 rpm" in verdict.detail


def test_tracking_error_is_suppressed_while_the_runtime_says_it_is_ramping() -> None:
    """During a ramp the two speeds are supposed to differ. That is what a ramp is."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(0)
    rig.ramping = True
    rig.run(Seconds(60.0))

    assert rig.verdict_for(RULE_TRACKING_ERROR) is None
    assert rig.floor() is None


def test_tracking_error_tolerates_a_small_divergence() -> None:
    """Fifty motor rpm is one output rpm through i = 49.79. It is not a stall."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(450)
    rig.run(Seconds(60.0))

    assert rig.verdict_for(RULE_TRACKING_ERROR) is None


def test_tracking_is_judged_against_the_envelope_during_a_ramp() -> None:
    """THE finding: a stuck RFRD during a motion-limited climb was caught 52 s late.

    The rule used to be switched off while the runtime said "ramping", which at
    the motion limits is the whole ~100 s of a climb. With an envelope it is
    judged throughout: far from the setpoint but inside the band the drive's
    ramp allows is fine, outside it for the dwell is not - ramping or not.
    """
    rig = Rig()
    rig.ramping = True
    rig.commanded = MotorRpm(900)
    rig.measured = MotorRpm(500)
    rig.envelope = SpeedEnvelope(low=MotorRpm(450), high=MotorRpm(900))
    rig.run(Seconds(10.0))
    assert rig.verdict_for(RULE_TRACKING_ERROR) is None, "inside the band the drive may be"

    rig.envelope = SpeedEnvelope(low=MotorRpm(880), high=MotorRpm(900))
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(1.9))
    assert rig.verdict_for(RULE_TRACKING_ERROR) is None
    rig.tick(Seconds(0.1))
    verdict = rig.verdict_for(RULE_TRACKING_ERROR)
    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert "380 rpm outside the band 880..900" in verdict.detail


def test_a_shaft_above_its_envelope_is_a_tracking_failure_too() -> None:
    """Overspeed against the command is as much "not following" as a stall."""
    rig = Rig()
    rig.commanded = MotorRpm(300)
    rig.measured = MotorRpm(500)
    rig.envelope = SpeedEnvelope(low=MotorRpm(300), high=MotorRpm(320))
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(2.0))
    assert rig.action_for(RULE_TRACKING_ERROR) is SafetyAction.RAMP_DOWN


def test_a_shaft_and_an_echo_that_both_disagree_hand_the_stop_to_the_drive() -> None:
    """Misaddressed writes: neither the register nor the shaft shows the command. GO_SILENT.

    The failure matrix: tracking_error RAMP_DOWN, the emergency zero
    "ACKNOWLEDGED", and the shaft still at 1344 rpm 90 s later - because every
    misaddressed write, the ramp-down's and the keepalive's, fed the drive's
    ttO. The only stop that does not need a write to land is to stop writing.
    """
    rig = Rig()
    rig.commanded = MotorRpm(1200)
    rig.measured = MotorRpm(1344)
    rig.echo = MotorRpm(1344)
    rig.envelope = SpeedEnvelope(low=MotorRpm(1200), high=MotorRpm(1210))
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(2.0))
    verdict = rig.verdict_for(RULE_TRACKING_ERROR)
    assert verdict is not None
    assert verdict.action is SafetyAction.GO_SILENT
    assert verdict.latched
    assert "echo reads 1344" in verdict.detail
    assert "ttO" in verdict.detail
    assert rig.standing_action() is SafetyAction.GO_SILENT


def test_an_echo_that_agrees_keeps_a_tracking_failure_at_ramp_down() -> None:
    """The writes are landing: a controlled descent is still a command the drive obeys."""
    rig = Rig()
    rig.commanded = MotorRpm(1200)
    rig.measured = MotorRpm(700)
    rig.echo = MotorRpm(1200)
    rig.envelope = SpeedEnvelope(low=MotorRpm(1190), high=MotorRpm(1200))
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(2.0))
    assert rig.action_for(RULE_TRACKING_ERROR) is SafetyAction.RAMP_DOWN


def test_an_echo_disagreeing_for_less_than_its_dwell_does_not_escalate_tracking() -> None:
    """One garbled read-back is not evidence the writes are going elsewhere."""
    rig = Rig()
    rig.commanded = MotorRpm(1200)
    rig.measured = MotorRpm(700)
    rig.envelope = SpeedEnvelope(low=MotorRpm(1190), high=MotorRpm(1200))
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(1.6))
    rig.echo = MotorRpm(1344)
    rig.tick(Seconds(0.4))
    assert rig.action_for(RULE_TRACKING_ERROR) is SafetyAction.RAMP_DOWN


def test_an_echo_that_disagrees_with_the_write_ends_the_session() -> None:
    """LFRD read back is not LFRD written: the commanded speed may be fiction.

    It used to be shown on the screen (``setpoint_confirmed``) and acted on
    nowhere; a manual session ran to its stop on unconfirmed writes.
    """
    rig = Rig()
    rig.commanded = MotorRpm(1000)
    rig.measured = rig.commanded
    rig.echo = MotorRpm(774)
    rig.tick()
    rig.tick(Seconds(0.8))
    assert rig.verdict_for(RULE_SETPOINT_UNCONFIRMED) is None
    rig.tick(Seconds(0.2))
    verdict = rig.verdict_for(RULE_SETPOINT_UNCONFIRMED)
    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched
    assert "echo reads 774" in verdict.detail
    rig.echo = rig.commanded
    rig.tick()
    assert rig.verdict_for(RULE_SETPOINT_UNCONFIRMED) is None
    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_SETPOINT_UNCONFIRMED


def test_an_echo_that_was_not_read_says_nothing_about_the_write() -> None:
    """No read followed the write this tick: the rule releases, in either direction."""
    rig = Rig()
    rig.commanded = MotorRpm(1000)
    rig.measured = rig.commanded
    rig.echo = MotorRpm(774)
    rig.tick()
    rig.echo = None
    rig.run(Seconds(5.0))
    assert rig.verdict_for(RULE_SETPOINT_UNCONFIRMED) is None
    assert rig.floor() is None


def test_an_unknown_measured_speed_releases_the_tracking_rule() -> None:
    """A failed RFRD read says nothing about the shaft, in either direction."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = None
    rig.run(Seconds(60.0))

    assert rig.verdict_for(RULE_TRACKING_ERROR) is None


# =========================================================================
# reverse_rotation
# =========================================================================


def test_rotation_against_the_command_zeroes_the_reference_at_once() -> None:
    """The motor is wired backwards, or a sign was lost in the setpoint path.

    With a person inside, the machine is then not doing what the software
    believes, and no further command from that software can be trusted to help.
    """
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(-50)
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.QUICK_STOP
    assert verdict.rule == RULE_REVERSE_ROTATION
    assert verdict.latched is True
    assert "wired" in verdict.detail


def test_rotation_against_a_reverse_command_also_fires() -> None:
    """The test is the sign of the product, so it works in both directions."""
    rig = Rig()
    rig.commanded = MotorRpm(-500)
    rig.measured = MotorRpm(50)
    rig.tick()

    assert rig.action_for(RULE_REVERSE_ROTATION) is SafetyAction.QUICK_STOP


def test_dither_about_zero_is_not_reverse_rotation() -> None:
    """RFRD dithers at standstill; a sign flip inside that noise is not evidence."""
    rig = Rig()
    rig.commanded = MotorRpm(500)
    rig.measured = MotorRpm(-5)
    rig.tick()

    assert rig.verdict_for(RULE_REVERSE_ROTATION) is None


def test_a_turning_shaft_with_nothing_commanded_is_a_tracking_failure() -> None:
    """The division of labour between two rules that could both have claimed this.

    A shaft turning with a zero setpoint is a freewheel or a lost setpoint, not
    a direction error, and the operator needs to be told the right one of those
    two sentences. ``reverse_rotation`` therefore tests the sign of the product,
    which a commanded zero cannot satisfy.
    """
    rig = Rig()
    rig.commanded = MotorRpm(0)
    rig.measured = MotorRpm(-500)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(2.0))
    rig.tick(Seconds(0.5))

    assert rig.verdict_for(RULE_REVERSE_ROTATION) is None
    assert rig.action_for(RULE_TRACKING_ERROR) is SafetyAction.RAMP_DOWN


# =========================================================================
# session_overrun
# =========================================================================


def test_running_past_the_programme_plus_its_grace_ends_the_session() -> None:
    """A session that outlived its own programme is running to no plan at all."""
    rig = Rig()
    rig.total_duration = Seconds(100.0)

    rig.tick(Seconds(130.0))
    assert rig.verdict_for(RULE_SESSION_OVERRUN) is None

    rig.tick(Seconds(0.5))
    verdict = rig.verdict_for(RULE_SESSION_OVERRUN)

    assert verdict is not None
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched is True
    assert "lost track" in verdict.detail


# =========================================================================
# loop_stall - tested at the real 5 Hz defaults
# =========================================================================


def test_the_real_control_period_does_not_trip_its_own_stall_rule() -> None:
    """The rule has to be quiet at the rate the loop actually runs at.

    Every other test in this file relaxes the stall thresholds so that time can
    be jumped; this one uses the shipped defaults, so a change to the period or
    to the multiplier that made normal operation look like a stall would fail
    here.
    """
    rig = Rig(LIMITS)
    assert rig.run(Seconds(30.0)) is None
    assert rig.floor() is None


def test_a_gap_of_more_than_three_periods_freezes_and_latches() -> None:
    """A recovered stall is invisible unless something remembers it.

    This rule can only ever fire AFTER the loop has recovered, because it is
    evaluated by the loop it is watching. That is why both of its levels latch:
    an unlatched verdict would vanish on the next healthy tick and nobody would
    ever act on it. The independent watchdog is deliberately outside this
    process - with no keepalive, the drive's own ttO timeout stops the motor.
    """
    rig = Rig(LIMITS)
    rig.tick()
    rig.tick()
    assert rig.verdict_for(RULE_LOOP_STALL) is None

    verdict = rig.tick(Seconds(0.7))

    assert verdict is not None
    assert verdict.action is SafetyAction.FREEZE
    assert verdict.rule == RULE_LOOP_STALL
    assert verdict.latched is True
    assert "0.70 s" in verdict.detail


def test_a_long_gap_stops_writing_to_the_drive_altogether() -> None:
    """GO_SILENT is the only action that does not depend on this process working.

    Which is the right answer to a process that has just demonstrated it may
    not be.
    """
    rig = Rig(LIMITS)
    rig.tick()
    verdict = rig.tick(Seconds(5.0))

    assert verdict is not None
    assert verdict.action is SafetyAction.GO_SILENT
    assert verdict.rule == RULE_LOOP_STALL
    assert "ttO" in verdict.detail


def test_the_first_tick_of_a_session_cannot_be_a_stall() -> None:
    """There is no previous tick, so there is no interval to judge."""
    rig = Rig(LIMITS)
    rig.tick(Seconds(100.0))

    assert rig.verdict_for(RULE_LOOP_STALL) is None
    assert rig.live() == ()


def test_a_recovered_stall_leaves_the_live_set_but_stays_on_the_floor() -> None:
    rig = Rig(LIMITS)
    rig.tick()
    rig.tick(Seconds(0.7))
    rig.tick()

    assert rig.verdict_for(RULE_LOOP_STALL) is None
    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_LOOP_STALL
    assert rig.standing_action() is SafetyAction.FREEZE


# =========================================================================
# attendant_absent
# =========================================================================


def test_an_attendant_who_leaves_freezes_the_speed_then_ends_the_session() -> None:
    """This rig must never run unattended, and never is enforced here.

    Every other rule in this module assumes somebody is there to be told about
    it: an acknowledgement is a human act, a QUICK_STOP still takes seconds of
    ramp, and the one safety-rated stop on the machine is a mushroom somebody
    has to press. A session with nobody watching has none of that.
    """
    rig = Rig()
    rig.tick()
    rig.attendant_last_seen = rig.clock.monotonic()
    rig.attendant_present = False

    rig.tick(Seconds(60.0))
    assert rig.verdict_for(RULE_ATTENDANT_ABSENT) is None

    rig.tick(Seconds(0.5))
    freeze = rig.verdict_for(RULE_ATTENDANT_ABSENT)
    assert freeze is not None
    assert freeze.action is SafetyAction.FREEZE
    assert freeze.latched is False

    rig.tick(Seconds(60.0))
    ramp = rig.verdict_for(RULE_ATTENDANT_ABSENT)
    assert ramp is not None
    assert ramp.action is SafetyAction.RAMP_DOWN
    assert ramp.latched is True
    assert "nobody watching" in ramp.detail


def test_a_session_that_never_had_an_attendant_escalates_from_its_start() -> None:
    """A ping that has never arrived is not a recent ping.

    A closed laptop, a dropped WiFi link and a browser tab discarded to save
    memory are all indistinguishable from an operator who walked away, and the
    safe reading of all of them is the same one.
    """
    rig = Rig()
    rig.attendant_present = False
    rig.tick(Seconds(60.5))

    verdict = rig.verdict_for(RULE_ATTENDANT_ABSENT)
    assert verdict is not None
    assert verdict.action is SafetyAction.FREEZE
    assert "none has ever arrived" in verdict.detail


def test_an_attendant_who_comes_back_releases_the_freeze() -> None:
    rig = Rig()
    rig.attendant_present = False
    rig.tick(Seconds(60.5))
    assert rig.action_for(RULE_ATTENDANT_ABSENT) is SafetyAction.FREEZE

    rig.attendant_present = True
    rig.tick()

    assert rig.verdict_for(RULE_ATTENDANT_ABSENT) is None
    assert rig.floor() is None


# =========================================================================
# Precedence: max over SafetyAction, including ties
# =========================================================================


def test_the_most_severe_of_several_verdicts_is_the_one_that_stands() -> None:
    """Precedence is a ``max`` over the severity order, and nothing else.

    Two rules fire here with different demands. The more severe one is applied
    and the less severe one stays visible in the live set, because an operator
    needs to know everything that is true, not only the winning sentence.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.comm_failures = 5
    verdict = rig.tick()

    assert verdict is not None
    assert verdict.action is SafetyAction.GO_SILENT
    assert verdict.rule == RULE_COMMS_LOST
    assert {fired.rule for fired in rig.live()} == {RULE_DRIVE_FAULT, RULE_COMMS_LOST}
    assert rig.action_for(RULE_DRIVE_FAULT) is SafetyAction.RAMP_DOWN


def test_a_quiet_rule_cannot_dilute_a_severe_one() -> None:
    """An advisory firing alongside a decision to stop changes nothing."""
    rig = Rig()
    rig.current = Amperes(2.5)
    rig.tick(Seconds(0.5))
    rig.tick(Seconds(10.0))
    assert rig.standing_action() is SafetyAction.REDUCE

    rig.drive_state = DriveState.FAULT
    rig.tick()

    assert rig.standing_action() is SafetyAction.RAMP_DOWN


def test_the_operator_estop_wins_a_tie_of_equal_severity() -> None:
    """Both demands are QUICK_STOP, so the machine does the same thing either way.

    What differs is the sentence on the screen. Somebody who has just hit the
    button should see their own action named, not a rule that happened to reach
    the same severity at the same moment.
    """
    rig = Rig()
    rig.bpm = CRITICAL
    rig.tick()
    assert rig.standing_action() is SafetyAction.QUICK_STOP

    rig.supervisor.latch_estop("hit the button")
    standing = rig.standing()

    assert standing is not None
    assert standing.action is SafetyAction.QUICK_STOP
    assert standing.rule == RULE_OPERATOR_ESTOP


def test_the_standing_action_and_the_standing_verdict_never_disagree() -> None:
    """Two accessors, one candidate set. A UI reading either must see the same thing."""
    rig = Rig()
    assert rig.standing() is None
    assert rig.standing_action() is SafetyAction.NONE

    rig.drive_state = DriveState.FAULT
    rig.tick()
    standing = rig.standing()

    assert standing is not None
    assert standing.action is rig.standing_action()


# =========================================================================
# Latching and the floor
# =========================================================================


def test_the_floor_keeps_the_verdict_that_fired_first_on_an_equal_tie() -> None:
    """Dwell is what separates a spike from a trend, so the older instant wins.

    Two latched RAMP_DOWN verdicts, one after the other. An equally severe
    newcomer must not displace the first: its ``since`` is the more
    informative one, and a floor that kept overwriting itself would report the
    machine as having just gone wrong for as long as anything kept firing.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    first = rig.floor()
    assert first is not None

    rig.drive_state = DriveState.OPERATION_ENABLED
    rig.total_duration = Seconds(1.0)
    rig.tick(Seconds(60.0))
    assert rig.action_for(RULE_SESSION_OVERRUN) is SafetyAction.RAMP_DOWN

    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_DRIVE_FAULT
    assert floor.since == first.since


def test_the_floor_rises_to_a_more_severe_verdict() -> None:
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    assert rig.standing_action() is SafetyAction.RAMP_DOWN

    rig.comm_failures = 5
    rig.tick()

    floor = rig.floor()
    assert floor is not None
    assert floor.action is SafetyAction.GO_SILENT
    assert floor.rule == RULE_COMMS_LOST


def test_an_advisory_clears_but_never_below_the_floor() -> None:
    """The exact scope of the monotonicity claim, pinned rather than glossed.

    A latched FREEZE sits on the floor while an unlatched REDUCE stands above
    it. When the current settles, the REDUCE goes - that is what unlatched
    means, and it is what keeps an operator from learning to click through
    alarms - but the standing verdict falls only as far as the floor, never to
    NONE. Nothing but :meth:`acknowledge` moves the floor.
    """
    rig = Rig(LIMITS)
    rig.tick()
    rig.tick(Seconds(0.7))
    assert rig.standing_action() is SafetyAction.FREEZE

    rig.current = Amperes(2.5)
    rig.run(Seconds(11.0))
    assert rig.standing_action() is SafetyAction.REDUCE

    rig.current = Amperes(2.0)
    rig.tick()

    assert rig.standing_action() is SafetyAction.FREEZE
    floor = rig.floor()
    assert floor is not None
    assert floor.rule == RULE_LOOP_STALL


# =========================================================================
# Acknowledgement - the only thing that lowers the floor
# =========================================================================


def _refusal_kind(refusal: AcknowledgeRefusal) -> str:
    """Match the refusal union exhaustively, ending in ``assert_never``.

    Exists to prove the union is closed: adding a refusal variant without
    handling it here fails the type check by name, which is the whole point of
    the ``Result`` arrangement. The nested form (``case Err(error):`` then
    ``match error:``) is used at the call sites below, because matching
    variants directly inside ``Err(...)`` does not narrow the type argument
    and would silently buy nothing.
    """
    match refusal:
        case Unattributed():
            return "unattributed"
        case GoSilentIsTerminal():
            return "go-silent"
        case EmergencyStopStillLatched():
            return "estop-latched"
        case NothingLatched():
            return "nothing-latched"
        case _ as unreachable:
            assert_never(unreachable)


def test_acknowledging_clears_the_floor_and_the_estop_together() -> None:
    """The record names the operator, both clocks, and every rule it cleared."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    rig.supervisor.latch_estop("checking something")

    result = rig.supervisor.acknowledge("dr-mensah", estop_released=True)

    match result:
        case Ok(record):
            assert record.operator == "dr-mensah"
            assert record.at == rig.clock.monotonic()
            assert record.wall_clock == rig.clock.unix_millis()
            assert set(record.cleared) == {RULE_OPERATOR_ESTOP, RULE_DRIVE_FAULT}
        case Err(error):
            pytest.fail(f"acknowledgement refused: {_refusal_kind(error)}")

    rig.drive_state = DriveState.OPERATION_ENABLED
    rig.tick()
    assert rig.standing() is None
    assert rig.floor() is None


def test_acknowledging_a_condition_that_is_still_true_re_fires_it() -> None:
    """Clearing a latch is not fixing a fault, and the rule says so again at once."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    original = rig.floor()
    assert original is not None

    assert isinstance(rig.supervisor.acknowledge("dr-mensah"), Ok)
    assert rig.floor() is None

    rig.tick()
    floor = rig.floor()

    assert floor is not None
    assert floor.rule == RULE_DRIVE_FAULT
    # The condition never lapsed, so the verdict keeps the instant it first
    # stood rather than pretending the fault is new.
    assert floor.since == original.since


def test_go_silent_cannot_be_acknowledged() -> None:
    """One-way by construction, and the refusal names the rule that made it so.

    Going silent hands the stop to a timer inside the drive precisely because
    this process may be the problem. Resuming writes would take it back, so
    recovery is an operator action on a machine that has demonstrably stopped -
    which means restarting this process, not clicking a button in it.
    """
    rig = Rig()
    rig.comm_failures = 5
    rig.tick()

    result = rig.supervisor.acknowledge("dr-mensah", estop_released=True)

    match result:
        case Ok(_):
            pytest.fail("GO_SILENT was acknowledged")
        case Err(error):
            match error:
                case GoSilentIsTerminal(rule):
                    assert rule == RULE_COMMS_LOST
                case _:
                    pytest.fail(f"wrong refusal: {_refusal_kind(error)}")

    assert rig.standing_action() is SafetyAction.GO_SILENT


def test_a_latched_estop_must_be_released_before_anything_is_acknowledged() -> None:
    """Software cannot see a mushroom contact, so the operator states it.

    The keyword defaults to ``False`` so that forgetting the question fails
    closed: an acknowledgement that silently cleared a stop which is still
    physically pressed would be the worst possible reading of a click.
    """
    rig = Rig()
    verdict = rig.supervisor.latch_estop("hit the button")

    result = rig.supervisor.acknowledge("dr-mensah")

    match result:
        case Ok(_):
            pytest.fail("a latched estop was cleared without being released")
        case Err(error):
            match error:
                case EmergencyStopStillLatched(since):
                    assert since == verdict.since
                case _:
                    pytest.fail(f"wrong refusal: {_refusal_kind(error)}")

    assert rig.standing_action() is SafetyAction.QUICK_STOP
    assert isinstance(rig.supervisor.acknowledge("dr-mensah", estop_released=True), Ok)
    assert rig.standing() is None


@pytest.mark.parametrize("operator", ["", "   ", "\t\n"])
def test_an_unattributed_acknowledgement_is_refused(operator: str) -> None:
    """An unattributable safety record is not a safety record."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()

    result = rig.supervisor.acknowledge(operator, estop_released=True)

    match result:
        case Ok(_):
            pytest.fail("an anonymous acknowledgement was accepted")
        case Err(error):
            assert _refusal_kind(error) == "unattributed"

    assert rig.standing_action() is SafetyAction.RAMP_DOWN


def test_acknowledging_nothing_is_refused() -> None:
    """Distinguishable from success on purpose: a UI must not report a clearance."""
    rig = Rig()
    rig.tick()

    result = rig.supervisor.acknowledge("dr-mensah", estop_released=True)

    match result:
        case Ok(_):
            pytest.fail("an empty acknowledgement reported success")
        case Err(error):
            assert _refusal_kind(error) == "nothing-latched"


def test_acknowledging_does_not_discard_a_trip_raised_from_a_thread() -> None:
    """A trip raised microseconds before the click must not vanish into it.

    Pending trips are drained on the next tick and raise the floor again, which
    is the fail-safe direction: the worst case is an operator having to click
    twice, and the alternative worst case is a demand nobody ever saw.
    """
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()
    rig.supervisor.trip_from_thread("sensor_failed", SafetyAction.RAMP_DOWN, "reader died")

    assert isinstance(rig.supervisor.acknowledge("dr-mensah"), Ok)
    rig.drive_state = DriveState.OPERATION_ENABLED
    rig.tick()

    floor = rig.floor()
    assert floor is not None
    assert floor.rule == "sensor_failed"
    assert floor.action is SafetyAction.RAMP_DOWN


def test_the_acknowledgement_is_logged_with_a_wall_clock_stamp(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Who cleared what, and when by a clock a server can read."""
    rig = Rig()
    rig.drive_state = DriveState.FAULT
    rig.tick()

    with caplog.at_level(logging.WARNING, logger="src.training.safety"):
        assert isinstance(rig.supervisor.acknowledge("dr-mensah"), Ok)

    assert "dr-mensah" in caplog.text
    assert str(rig.clock.unix_millis()) in caplog.text
    assert RULE_DRIVE_FAULT in caplog.text


# =========================================================================
# The operator emergency stop
# =========================================================================


def test_latching_the_estop_is_visible_with_no_tick_and_no_clock_advance() -> None:
    """The requirement, stated exactly: the stop must not wait for the next tick.

    No :meth:`evaluate` call and no clock advance happen between the latch and
    the read below. A supervisor that only published the verdict from inside
    its tick would make the web layer wait for whatever the control loop is
    currently doing, which is precisely the situation in which somebody is
    reaching for the button.
    """
    rig = Rig()
    before = rig.clock.monotonic()
    assert rig.standing() is None

    verdict = rig.supervisor.latch_estop("subject asked to stop")
    standing = rig.standing()

    assert standing is verdict
    assert rig.standing_action() is SafetyAction.QUICK_STOP
    assert verdict.rule == RULE_OPERATOR_ESTOP
    assert verdict.latched is True
    assert verdict.since == before
    assert rig.clock.monotonic() == before
    assert "subject asked to stop" in verdict.detail


def test_latching_the_estop_does_not_block() -> None:
    """Measured in real wall time, because a ManualClock cannot say this.

    The number is a blocking detector rather than a latency specification: the
    call does one construction, one attribute write and one log call, so
    anything on the order of a millisecond means it acquired a lock, waited on
    a loop, or touched I/O. The allowance is generous so that a coverage-traced
    run on a loaded machine cannot make it flap.
    """
    rig = Rig()
    started = time.perf_counter()
    rig.supervisor.latch_estop("hit the button")
    duration = time.perf_counter() - started

    assert duration < 0.05
    assert rig.standing_action() is SafetyAction.QUICK_STOP


def test_the_estop_outlives_every_tick_and_every_rule() -> None:
    """It is held in its own slot, not in the live set, so no tick can clear it."""
    rig = Rig()
    rig.supervisor.latch_estop("hit the button")
    rig.run(Seconds(30.0))

    assert rig.standing_action() is SafetyAction.QUICK_STOP
    assert {fired.rule for fired in rig.live()} == set()


def test_latching_the_estop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    with caplog.at_level(logging.ERROR, logger="src.training.safety"):
        rig.supervisor.latch_estop("subject asked to stop")

    assert [(record.name, record.levelno) for record in caplog.records] == [
        ("src.training.safety", logging.ERROR)
    ]
    assert "subject asked to stop" not in caplog.text


# =========================================================================
# Trips raised from another thread
# =========================================================================


def test_a_trip_from_a_thread_is_picked_up_on_the_next_tick_and_latches() -> None:
    """Stamped when the loop learned of it, which is the honest instant.

    The acquisition thread cannot be made to wait on the control loop - if the
    reader blocks, samples are lost, and lost samples are what the heart rate
    is computed from - so the trip is queued and stamped on pickup. Every trip
    latches, because the thread that raised it will not be there to re-assert
    it next tick.
    """
    rig = Rig()
    rig.supervisor.trip_from_thread("sensor_failed", SafetyAction.RAMP_DOWN, "reader died")
    assert rig.standing() is None

    verdict = rig.tick()

    assert verdict is not None
    assert verdict.rule == "sensor_failed"
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched is True
    assert verdict.detail == "reader died"
    assert verdict.since == rig.clock.monotonic()
    assert is_rule_id("sensor_failed")


def test_a_trip_from_a_real_thread_arrives() -> None:
    """Called from an actual thread, because that is where it will be called from."""
    rig = Rig()
    thread = threading.Thread(
        target=rig.supervisor.trip_from_thread,
        args=("sensor_failed", SafetyAction.GO_SILENT, "bitalino stopped responding"),
    )
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()

    rig.tick()

    assert rig.standing_action() is SafetyAction.GO_SILENT


def test_tripping_from_a_thread_reads_no_clock() -> None:
    """Proved with a clock that fails the test if anything touches it.

    A passing test with a working clock could never tell "does not read the
    clock" from "read it and got away with it". The instant is stamped on
    pickup instead, which keeps the clock out of the one place that must not
    need one.
    """
    supervisor = SafetySupervisor(clock=_ForbiddenClock(), limits=PATIENT)
    supervisor.trip_from_thread("sensor_failed", SafetyAction.FREEZE, "no clock here")

    assert supervisor.standing is None


def test_two_trips_raised_inside_one_tick_are_both_kept() -> None:
    """Why this is a bounded queue rather than a single attribute slot.

    A slot would silently drop the first of two trips raised inside one 200 ms
    tick, and discarding a safety demand to save an allocation is not a trade
    this machine gets to make. The more severe one wins the floor, and both are
    recorded.
    """
    rig = Rig()
    rig.supervisor.trip_from_thread("sensor_failed", SafetyAction.FREEZE, "first")
    rig.supervisor.trip_from_thread("sensor_unplugged", SafetyAction.RAMP_DOWN, "second")

    rig.tick()

    floor = rig.floor()
    assert floor is not None
    assert floor.rule == "sensor_unplugged"
    assert floor.action is SafetyAction.RAMP_DOWN


def test_a_trip_with_no_detail_still_says_something() -> None:
    """A blank detail would reach the screen as a display bug, not a demand."""
    rig = Rig()
    rig.supervisor.trip_from_thread("sensor_failed", SafetyAction.REDUCE)

    verdict = rig.tick()

    assert verdict is not None
    assert verdict.detail == THREAD_TRIP_DETAIL


@given(st.lists(st.sampled_from(SafetyAction), min_size=1, max_size=8))
def test_the_standing_action_dominates_every_trip_raised(actions: list[SafetyAction]) -> None:
    """For any set of trips, the applied demand is at least the worst of them.

    The precedence rule of the whole layer, over arbitrary input: a quiet trip
    can never dilute a severe one, whatever order they arrive in.
    """
    rig = Rig()
    for index, action in enumerate(actions):
        rule = f"thread_rule_{index}"
        assert is_rule_id(rule)
        rig.supervisor.trip_from_thread(rule, action, "property")
    rig.tick()

    assert rig.standing_action() >= most_severe(actions)


# =========================================================================
# The startup gate
# =========================================================================


def test_the_attestation_statement_names_the_wiring_and_the_jumper() -> None:
    """The operator has to be told exactly what they are attesting to."""
    assert "P24" in ESTOP_ATTESTATION
    assert "STO" in ESTOP_ATTESTATION
    assert "normally-closed" in ESTOP_ATTESTATION
    assert "jumper" in ESTOP_ATTESTATION


def test_motion_is_blocked_until_the_estop_wiring_is_attested() -> None:
    """While STO is jumpered there is no independent way to remove torque.

    So the only emergency stop that exists is one wired NC into P24 -> STO with
    the jumper gone, software cannot see whether that is true, and it blocks
    until a human says so by name. A blocking defect rather than a warning: a
    warning about a jumper is read once and then lives in a commissioning file
    forever.
    """
    rig = Rig()

    match rig.supervisor.require_estop_confirmed():
        case Ok(_):
            pytest.fail("a session was allowed to command motion with no attestation")
        case Err(EstopUnattested(statement)):
            assert statement == ESTOP_ATTESTATION

    assert isinstance(rig.supervisor.confirm_estop_wiring("dr-mensah"), Ok)

    match rig.supervisor.require_estop_confirmed():
        case Ok(attestation):
            assert attestation.operator == "dr-mensah"
            assert attestation.statement == ESTOP_ATTESTATION
        case Err(EstopUnattested(_)):
            pytest.fail("the gate stayed shut after a valid attestation")


def test_the_attestation_is_logged_with_a_timestamp(caplog: pytest.LogCaptureFixture) -> None:
    """A forgotten jumper becomes a visible defect with a name and a time on it."""
    rig = Rig()
    with caplog.at_level(logging.INFO, logger="src.training.safety"):
        result = rig.supervisor.confirm_estop_wiring("dr-mensah")

    assert isinstance(result, Ok)
    assert "dr-mensah" in caplog.text
    assert str(rig.clock.unix_millis()) in caplog.text
    assert "STO" in caplog.text


def test_the_attestation_carries_both_clocks() -> None:
    """Monotonic for durations, wall clock for records that leave the machine.

    The Pi has no RTC, so on a freshly booted machine the wall clock can be
    wrong while the monotonic reading is perfectly good - which is why nothing
    computes with the former.
    """
    rig = Rig()
    before_millis = rig.clock.unix_millis()
    rig.clock.advance(Seconds(5.0))

    match rig.supervisor.confirm_estop_wiring("dr-mensah"):
        case Ok(attestation):
            assert attestation.at == Monotonic(START + 5.0)
            assert attestation.wall_clock == UnixMillis(before_millis + 5_000)
        case Err(error):
            pytest.fail(f"attestation refused: {error.detail}")


@pytest.mark.parametrize("operator", ["", "   "])
def test_an_unattributed_attestation_is_refused(operator: str) -> None:
    """Who said the jumper was gone is the question that matters afterwards."""
    rig = Rig()

    match rig.supervisor.confirm_estop_wiring(operator):
        case Ok(_):
            pytest.fail("an anonymous attestation was recorded")
        case Err(error):
            assert "operator" in error.detail

    assert isinstance(rig.supervisor.require_estop_confirmed(), Err)


def test_re_attesting_replaces_the_record() -> None:
    """Operators hand over, and the current attestation must name whoever is there."""
    rig = Rig()
    assert isinstance(rig.supervisor.confirm_estop_wiring("dr-mensah"), Ok)
    assert isinstance(rig.supervisor.confirm_estop_wiring("dr-okafor"), Ok)

    match rig.supervisor.require_estop_confirmed():
        case Ok(attestation):
            assert attestation.operator == "dr-okafor"
        case Err(_):
            pytest.fail("the gate shut after a handover")


def test_the_startup_gate_is_silent_so_a_user_interface_may_poll_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gate that logged on every check would bury the attestation it recorded."""
    rig = Rig()
    with caplog.at_level(logging.DEBUG, logger="src.training.safety"):
        for _ in range(5):
            assert isinstance(rig.supervisor.require_estop_confirmed(), Err)

    assert caplog.text == ""


# =========================================================================
# Properties over arbitrary interleavings
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Step:
    """One arbitrary tick: every input a rule reads, plus the two out-of-band ones."""

    advance: float
    sample_present: bool
    bpm: int | None
    quality: SignalQuality
    fresh: bool
    commanded: int
    measured: int | None
    current: float | None
    drive_state: DriveState
    ramping: bool
    comm_failures: int
    phase: Phase
    attendant: bool
    estop: bool
    trip: SafetyAction | None


_steps: Final[st.SearchStrategy[_Step]] = st.builds(
    _Step,
    advance=st.floats(min_value=0.0, max_value=90.0, allow_nan=False, allow_infinity=False),
    sample_present=st.booleans(),
    bpm=st.one_of(st.none(), st.integers(min_value=0, max_value=300)),
    quality=st.sampled_from(SignalQuality),
    fresh=st.booleans(),
    commanded=st.integers(min_value=-900, max_value=900),
    measured=st.one_of(st.none(), st.integers(min_value=-900, max_value=900)),
    current=st.one_of(
        st.none(),
        st.floats(min_value=-1.0, max_value=12.0),
        st.just(math.nan),
    ),
    drive_state=st.sampled_from(DriveState),
    ramping=st.booleans(),
    comm_failures=st.integers(min_value=0, max_value=8),
    phase=st.sampled_from(Phase),
    attendant=st.booleans(),
    estop=st.booleans(),
    trip=st.one_of(st.none(), st.sampled_from(SafetyAction)),
)


def _apply(rig: Rig, step: _Step) -> None:
    """Feed one arbitrary step to the rig, out-of-band inputs included."""
    rig.heart_rate_present = step.sample_present
    rig.bpm = None if step.bpm is None else Bpm(step.bpm)
    rig.quality = step.quality
    rig.fresh = step.fresh
    rig.commanded = MotorRpm(step.commanded)
    rig.measured = None if step.measured is None else MotorRpm(step.measured)
    rig.current = None if step.current is None else Amperes(step.current)
    rig.drive_state = step.drive_state
    rig.ramping = step.ramping
    rig.comm_failures = step.comm_failures
    rig.phase = step.phase
    rig.attendant_present = step.attendant
    if step.estop:
        rig.supervisor.latch_estop("property test")
    if step.trip is not None:
        rig.supervisor.trip_from_thread("thread_rule", step.trip, "property test")
    rig.tick(Seconds(step.advance))


@settings(max_examples=200, deadline=None)
@given(st.lists(_steps, min_size=1, max_size=30))
def test_the_latched_floor_never_falls_and_the_verdict_never_undercuts_it(
    steps: list[_Step],
) -> None:
    """The safety guarantee of the whole layer, over arbitrary interleavings.

    Four invariants, asserted after every one of an arbitrary sequence of
    ticks - arbitrary heart rates, qualities, sequence numbers, speeds,
    currents, drive states, phases, comms failures, time jumps, operator
    e-stops and thread trips, in any order:

    1. **the latched floor never decreases.** No acknowledgement happens
       anywhere in this test, so nothing is entitled to lower it;
    2. **the standing verdict is never less severe than the floor.** This is
       the precise form of "a verdict never decreases without an explicit
       acknowledgement": an unlatched advisory may come and go above the floor
       (see ``test_an_advisory_clears_but_never_below_the_floor``), and nothing
       can go below it;
    3. **the standing action dominates every rule currently firing**, so no
       rule can ever be quietly outvoted by a quieter one;
    4. **the two accessors agree**, and every verdict carries a finite instant,
       so no NaN or infinity can reach an age calculation or a screen.

    And, throughout: nothing raises. An exception out of ``evaluate`` would
    unwind the tick with the motor still commanded, so the absence of one is
    itself part of the guarantee. ``deadline=None`` because coverage tracing
    makes per-example wall time meaningless; this test is about a logical
    invariant, not about speed.
    """
    rig = Rig()
    floor_action = SafetyAction.NONE
    estop_latched = False

    for step in steps:
        _apply(rig, step)
        estop_latched = estop_latched or step.estop

        floor = rig.floor()
        current_floor = SafetyAction.NONE if floor is None else floor.action
        assert current_floor >= floor_action
        floor_action = current_floor

        standing = rig.standing_action()
        assert standing >= floor_action
        assert standing >= most_severe(fired.action for fired in rig.live())
        if estop_latched:
            assert standing >= SafetyAction.QUICK_STOP

        verdict = rig.standing()
        if verdict is None:
            assert standing is SafetyAction.NONE
        else:
            assert verdict.action is standing
            assert math.isfinite(verdict.since)
            assert verdict.detail != ""
        for fired in rig.live():
            assert fired.rule in ALL_RULES


@settings(max_examples=100, deadline=None)
@given(st.lists(_steps, min_size=1, max_size=20))
def test_a_go_silent_verdict_is_never_walked_back(steps: list[_Step]) -> None:
    """Once silent, always silent - including across an acknowledgement.

    GO_SILENT is the only action that does not depend on this process
    continuing to work, so taking it back would be taking back the one
    guarantee that does not rest on the software being correct. The
    acknowledgement attempted on every tick here must refuse for as long as it
    stands.
    """
    rig = Rig()
    gone_silent = False

    for step in steps:
        _apply(rig, step)
        if rig.standing_action() is SafetyAction.GO_SILENT:
            gone_silent = True
        if gone_silent:
            result = rig.supervisor.acknowledge("dr-mensah", estop_released=True)
            assert isinstance(result, Err)
            assert rig.standing_action() is SafetyAction.GO_SILENT
