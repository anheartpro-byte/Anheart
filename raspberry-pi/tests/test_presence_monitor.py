"""The presence rules, one by one: every verdict, every refusal, every timing edge.

Driven on a ``ManualClock`` at a 20 fps frame stream (:class:`Stream`), so each
dwell is tested to the frame. The property tests are in
``tests/test_presence_properties.py``; the closed loop with the real runtime is
in ``tests/test_presence_adapter.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.presence.monitor import (
    ALL_PRESENCE_RULES,
    DEFAULT_PRESENCE_LIMITS,
    RULE_PRESENCE_BENCH_OCCUPIED,
    RULE_PRESENCE_CAMERA_LOST,
    RULE_PRESENCE_CAPSULE_UNKNOWN,
    RULE_PRESENCE_INTRUSION,
    RULE_PRESENCE_LATCHED,
    RULE_PRESENCE_LIMB_OUTSIDE,
    RULE_PRESENCE_RIDER_ABSENT,
    RULE_PRESENCE_RIDER_UNBUCKLED,
    RULE_PRESENCE_ZONE_NOT_CLEAR,
    RULE_PRESENCE_ZONE_UNCERTAIN,
    Clear,
    EmergencyStop,
    PresenceDecision,
    PresenceLimits,
    PresenceMonitor,
    PresenceRefusal,
    RampDown,
    StartBlocked,
)
from src.presence.types import (
    CameraHealth,
    CapsuleState,
    Confidence,
    MachineContext,
    MotionState,
    PersonInZone,
    PresenceObservation,
    RiderPosture,
    ZoneClear,
    ZoneView,
)
from src.result import Err, Ok
from src.training.safety import NothingLatched, Unattributed
from src.training.types import Occupancy, SafetyAction, is_rule_id
from src.units import Metres, Monotonic, Seconds

FRAME: Final[Seconds] = Seconds(0.05)
"""20 fps."""

TURNING_BENCH: Final[MachineContext] = MachineContext(MotionState.TURNING, Occupancy.BENCH)
TURNING_OCCUPIED: Final[MachineContext] = MachineContext(MotionState.TURNING, Occupancy.OCCUPIED)
AT_REST: Final[MachineContext] = MachineContext(MotionState.AT_REST, None)
OPERATOR: Final[str] = "Dr Ada Okonkwo"

SEATED: Final[RiderPosture] = RiderPosture(unbuckled=False, limb_outside=False)


def person(confidence: float, distance: float | None = None) -> PersonInZone:
    return PersonInZone(Confidence(confidence), None if distance is None else Metres(distance))


class Stream:
    """A 20 fps frame stream into one monitor, on a manual clock."""

    def __init__(self, limits: PresenceLimits = DEFAULT_PRESENCE_LIMITS) -> None:
        self.clock: ManualClock = ManualClock(Monotonic(100.0))
        self.monitor: PresenceMonitor = PresenceMonitor(clock=self.clock, limits=limits)
        self.seq: int = 0
        self.last: PresenceObservation | None = None
        self.decisions: list[PresenceDecision] = []

    @property
    def now(self) -> Monotonic:
        return self.clock.monotonic()

    def frames(
        self,
        seconds: float,
        context: MachineContext,
        *,
        zone: ZoneView | None = None,
        capsule: CapsuleState = CapsuleState.EMPTY,
        health: CameraHealth = CameraHealth.HEALTHY,
        posture: RiderPosture | None = None,
    ) -> PresenceDecision:
        """Deliver ``seconds`` of identical fresh frames; return the last decision."""
        decision: PresenceDecision = Clear()
        for _ in range(round(seconds / FRAME)):
            self.clock.advance(FRAME)
            self.seq += 1
            self.last = PresenceObservation(
                frame_seq=self.seq,
                at=self.now,
                health=health,
                zone=ZoneClear() if zone is None else zone,
                capsule=capsule,
                posture=posture,
            )
            decision = self.monitor.evaluate(self.now, self.last, context)
            self.decisions.append(decision)
        return decision

    def frozen(self, seconds: float, context: MachineContext) -> PresenceDecision:
        """Evaluate ``seconds`` more at 20 Hz with the SAME last frame (a frozen camera)."""
        decision: PresenceDecision = Clear()
        for _ in range(round(seconds / FRAME)):
            self.clock.advance(FRAME)
            decision = self.monitor.evaluate(self.now, self.last, context)
            self.decisions.append(decision)
        return decision


def _rules(decision: PresenceDecision) -> tuple[str, ...]:
    assert isinstance(decision, StartBlocked), decision
    return tuple(refusal.rule for refusal in decision.refusals)


def _stop(decision: PresenceDecision) -> tuple[SafetyAction, str]:
    assert isinstance(decision, (RampDown, EmergencyStop)), decision
    return decision.verdict.action, decision.verdict.rule


def _ready(stream: Stream, context: MachineContext = AT_REST, **frame: object) -> None:
    """Clear, healthy frames long enough for the start gate to open."""
    stream.frames(2.5, context, **frame)  # type: ignore[arg-type]  # keyword pass-through


# =========================================================================
# Vocabulary and limits
# =========================================================================


def test_every_rule_id_is_well_formed_and_distinct() -> None:
    assert len(set(ALL_PRESENCE_RULES)) == len(ALL_PRESENCE_RULES) == 10
    for rule in ALL_PRESENCE_RULES:
        assert is_rule_id(rule), rule
        assert rule.startswith("presence_"), rule


@pytest.mark.parametrize(
    "build",
    [
        lambda: replace(DEFAULT_PRESENCE_LIMITS, release_confidence=0.0),
        lambda: replace(DEFAULT_PRESENCE_LIMITS, release_confidence=0.5),
        lambda: replace(DEFAULT_PRESENCE_LIMITS, raise_confidence=0.9),
        lambda: replace(DEFAULT_PRESENCE_LIMITS, immediate_confidence=0.5),
    ],
)
def test_thresholds_out_of_order_are_refused(build: Callable[[], PresenceLimits]) -> None:
    with pytest.raises(ValueError, match="release < raise < immediate"):
        build()


def test_an_immediate_threshold_no_detector_can_reach_is_refused() -> None:
    with pytest.raises(ValueError, match="at most 1"):
        replace(DEFAULT_PRESENCE_LIMITS, immediate_confidence=1.5)


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("confirm_window", lambda: replace(DEFAULT_PRESENCE_LIMITS, confirm_window=Seconds(0.0))),
        ("release_after", lambda: replace(DEFAULT_PRESENCE_LIMITS, release_after=Seconds(0.0))),
        ("uncertain_dwell", lambda: replace(DEFAULT_PRESENCE_LIMITS, uncertain_dwell=Seconds(0.0))),
        ("stale_after", lambda: replace(DEFAULT_PRESENCE_LIMITS, stale_after=Seconds(0.0))),
        (
            "clear_before_start",
            lambda: replace(DEFAULT_PRESENCE_LIMITS, clear_before_start=Seconds(0.0)),
        ),
        (
            "capsule_empty_dwell",
            lambda: replace(DEFAULT_PRESENCE_LIMITS, capsule_empty_dwell=Seconds(0.0)),
        ),
        (
            "capsule_unknown_dwell",
            lambda: replace(DEFAULT_PRESENCE_LIMITS, capsule_unknown_dwell=Seconds(0.0)),
        ),
        (
            "bench_occupied_dwell",
            lambda: replace(DEFAULT_PRESENCE_LIMITS, bench_occupied_dwell=Seconds(0.0)),
        ),
        ("unbuckled_dwell", lambda: replace(DEFAULT_PRESENCE_LIMITS, unbuckled_dwell=Seconds(0.0))),
        (
            "limb_outside_dwell",
            lambda: replace(DEFAULT_PRESENCE_LIMITS, limb_outside_dwell=Seconds(0.0)),
        ),
    ],
)
def test_a_non_positive_duration_is_refused(name: str, build: Callable[[], PresenceLimits]) -> None:
    with pytest.raises(ValueError, match=name):
        build()


def test_a_negative_immediate_distance_is_refused_and_zero_is_accepted() -> None:
    with pytest.raises(ValueError, match="immediate_distance"):
        replace(DEFAULT_PRESENCE_LIMITS, immediate_distance=Metres(-0.1))
    assert replace(
        DEFAULT_PRESENCE_LIMITS, immediate_distance=Metres(0.0)
    ).immediate_distance == Metres(0.0)


def test_the_monitor_exposes_its_limits() -> None:
    assert Stream().monitor.limits is DEFAULT_PRESENCE_LIMITS


# =========================================================================
# At rest: the start gate
# =========================================================================


def test_a_camera_that_never_delivered_refuses_every_start() -> None:
    stream = Stream()
    stream.clock.advance(Seconds(1.0))
    decision = stream.monitor.evaluate(stream.now, None, AT_REST)
    assert _rules(decision) == (RULE_PRESENCE_CAMERA_LOST, RULE_PRESENCE_ZONE_NOT_CLEAR)
    assert isinstance(decision, StartBlocked)
    assert "aucune image exploitable" in decision.detail
    assert " ; " in decision.detail
    assert not stream.monitor.camera_ok(stream.now)
    refused = stream.monitor.check_start(stream.now, Occupancy.OCCUPIED)
    assert isinstance(refused, Err)
    # No healthy frame ever: nothing can be said about the capsule either way.
    assert _rules(refused.error) == (RULE_PRESENCE_CAMERA_LOST, RULE_PRESENCE_ZONE_NOT_CLEAR)


def test_the_zone_must_be_seen_clear_for_two_seconds_before_a_start() -> None:
    stream = Stream()
    decision = stream.frames(1.0, AT_REST)
    assert _rules(decision) == (RULE_PRESENCE_ZONE_NOT_CLEAR,)
    assert isinstance(decision, StartBlocked)
    assert "degagee depuis 0.9 s seulement, 2.0 s requises" in decision.detail
    assert isinstance(stream.frames(1.2, AT_REST), Clear)
    assert stream.monitor.check_start(stream.now, Occupancy.BENCH) == Ok(None)
    assert stream.monitor.camera_ok(stream.now)


def test_a_person_at_rest_refuses_the_start_and_never_latches() -> None:
    stream = Stream()
    _ready(stream)
    decision = stream.frames(3.0, AT_REST, zone=person(0.95, 1.24))
    assert _rules(decision) == (RULE_PRESENCE_ZONE_NOT_CLEAR,)
    assert isinstance(decision, StartBlocked)
    assert "personne detectee (confiance 0.95, a 1.2 m du bras)" in decision.detail
    assert stream.monitor.latched is None
    assert stream.monitor.live == ()
    # They step out: the gate reopens only after the clear hysteresis.
    assert isinstance(stream.frames(1.5, AT_REST), StartBlocked)
    assert isinstance(stream.frames(1.0, AT_REST), Clear)


def test_a_low_confidence_detection_at_rest_is_enough_to_refuse() -> None:
    stream = Stream()
    _ready(stream)
    assert _rules(stream.frames(0.1, AT_REST, zone=person(0.35))) == (RULE_PRESENCE_ZONE_NOT_CLEAR,)


def test_a_degraded_image_cannot_clear_the_zone_and_goes_stale() -> None:
    """Nobody seen in a degraded frame is not evidence of nobody there."""
    stream = Stream()
    _ready(stream)
    decision = stream.frames(0.4, AT_REST, health=CameraHealth.DEGRADED)
    assert _rules(decision) == (RULE_PRESENCE_ZONE_NOT_CLEAR,)
    decision = stream.frames(0.3, AT_REST, health=CameraHealth.DEGRADED)
    assert _rules(decision) == (RULE_PRESENCE_CAMERA_LOST, RULE_PRESENCE_ZONE_NOT_CLEAR)
    assert isinstance(decision, StartBlocked)
    assert "derniere image exploitable de la camera il y a 0.7 s" in decision.detail


def test_a_failed_camera_refuses_at_once() -> None:
    stream = Stream()
    _ready(stream)
    decision = stream.frames(0.05, AT_REST, health=CameraHealth.FAILED)
    assert RULE_PRESENCE_CAMERA_LOST in _rules(decision)
    assert isinstance(decision, StartBlocked)
    assert "la camera signale qu'elle ne voit plus" in decision.detail


@pytest.mark.parametrize(
    ("capsule", "posture", "expected"),
    [
        (CapsuleState.OCCUPIED, SEATED, ()),
        (CapsuleState.OCCUPIED, None, ()),
        (CapsuleState.EMPTY, None, (RULE_PRESENCE_RIDER_ABSENT,)),
        (CapsuleState.UNKNOWN, None, (RULE_PRESENCE_CAPSULE_UNKNOWN,)),
        (
            CapsuleState.OCCUPIED,
            RiderPosture(unbuckled=True, limb_outside=True),
            (RULE_PRESENCE_RIDER_UNBUCKLED, RULE_PRESENCE_LIMB_OUTSIDE),
        ),
    ],
)
def test_an_occupied_start_needs_a_seated_buckled_rider(
    capsule: CapsuleState, posture: RiderPosture | None, expected: tuple[str, ...]
) -> None:
    stream = Stream()
    _ready(stream, capsule=capsule, posture=posture)
    gate = stream.monitor.check_start(stream.now, Occupancy.OCCUPIED)
    if not expected:
        assert gate == Ok(None)
        return
    assert isinstance(gate, Err)
    assert _rules(gate.error) == expected
    assert gate.error.detail.startswith("demarrage refuse")


@pytest.mark.parametrize(
    ("capsule", "allowed"),
    [(CapsuleState.EMPTY, True), (CapsuleState.UNKNOWN, True), (CapsuleState.OCCUPIED, False)],
)
def test_a_bench_start_is_refused_only_when_somebody_is_seen_in_the_capsule(
    capsule: CapsuleState, allowed: bool
) -> None:
    stream = Stream()
    _ready(stream, capsule=capsule)
    gate = stream.monitor.check_start(stream.now, Occupancy.BENCH)
    if allowed:
        assert gate == Ok(None)
    else:
        assert isinstance(gate, Err)
        assert _rules(gate.error) == (RULE_PRESENCE_BENCH_OCCUPIED,)


def test_the_rest_decision_judges_the_contexts_declared_occupancy() -> None:
    stream = Stream()
    rest_occupied = MachineContext(MotionState.AT_REST, Occupancy.OCCUPIED)
    decision = stream.frames(2.5, rest_occupied, capsule=CapsuleState.EMPTY)
    assert _rules(decision) == (RULE_PRESENCE_RIDER_ABSENT,)


# =========================================================================
# Motion possible: the intrusion e-stop
# =========================================================================


def test_a_medium_confidence_intrusion_stops_after_the_confirmation_window() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    assert isinstance(stream.frames(0.05, TURNING_BENCH, zone=person(0.6)), Clear)
    decision = stream.frames(0.1, TURNING_BENCH, zone=person(0.6))
    assert isinstance(decision, EmergencyStop)
    verdict = decision.verdict
    assert verdict.action is SafetyAction.QUICK_STOP
    assert verdict.rule == RULE_PRESENCE_INTRUSION
    assert verdict.latched
    assert verdict.since == stream.now
    assert verdict.detail == (
        "personne detectee (confiance 0.60) dans la zone du bras alors que la machine "
        "tourne : arret d'urgence"
    )
    assert stream.monitor.latched == verdict
    assert stream.monitor.live == (verdict,)


@pytest.mark.parametrize(
    ("zone", "immediate"),
    [
        (person(0.85), True),
        (person(0.55, 0.3), True),
        (person(0.55, 0.5), True),
        (person(0.55, 0.8), False),
        (person(0.35, 0.1), False),
    ],
)
def test_a_sure_or_close_detection_skips_the_window(zone: PersonInZone, immediate: bool) -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    decision = stream.frames(0.05, TURNING_BENCH, zone=zone)
    assert isinstance(decision, EmergencyStop) is immediate


def test_a_single_false_positive_frame_does_not_stop_the_machine() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.7))
    stream.frames(1.0, TURNING_BENCH)
    assert all(isinstance(decision, Clear) for decision in stream.decisions[-21:])
    assert stream.monitor.latched is None


def test_a_flickering_detection_is_a_person_and_stops_the_machine() -> None:
    """0.6 / 0.1 on alternate frames: the release hysteresis keeps the episode alive."""
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    for _ in range(3):
        stream.frames(0.05, TURNING_BENCH, zone=person(0.6))
        stream.frames(0.05, TURNING_BENCH, zone=person(0.1))
    assert any(isinstance(decision, EmergencyStop) for decision in stream.decisions[-6:])


def test_a_detection_in_the_hysteresis_band_keeps_an_intrusion_alive() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.6))
    decision = stream.frames(0.1, TURNING_BENCH, zone=person(0.4))
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)


def test_a_band_detection_alone_never_starts_an_intrusion() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    assert isinstance(stream.frames(0.9, TURNING_BENCH, zone=person(0.4)), Clear)


def test_a_degraded_frame_detection_still_stops_the_machine() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    decision = stream.frames(0.15, TURNING_BENCH, zone=person(0.6), health=CameraHealth.DEGRADED)
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)


def test_degraded_frames_cannot_release_an_intrusion() -> None:
    """A person seen, then a camera that cannot see: the person is still there."""
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.6))
    decision = stream.frames(0.1, TURNING_BENCH, health=CameraHealth.DEGRADED)
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)


def test_a_person_seen_once_then_a_frozen_camera_still_stops() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.6))
    decision = stream.frozen(0.1, TURNING_BENCH)
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)


@pytest.mark.parametrize(
    ("motion", "label"),
    [
        (MotionState.ARMED, "est armee"),
        (MotionState.UNKNOWN, "est dans un etat inconnu (vitesse non mesuree)"),
    ],
)
def test_an_armed_or_unknown_machine_is_treated_as_turning(motion: MotionState, label: str) -> None:
    stream = Stream()
    context = MachineContext(motion, None)
    _ready(stream, context)
    decision = stream.frames(0.05, context, zone=person(0.9))
    assert isinstance(decision, EmergencyStop)
    assert f"alors que la machine {label} : arret d'urgence" in decision.verdict.detail


def test_a_fired_episode_at_rest_latches_the_moment_motion_becomes_possible() -> None:
    stream = Stream()
    _ready(stream)
    stream.frames(0.5, AT_REST, zone=person(0.6))
    assert stream.monitor.latched is None
    decision = stream.frames(0.05, TURNING_BENCH, zone=person(0.6))
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)


# =========================================================================
# Motion possible: the controlled stops
# =========================================================================


def test_low_confidence_that_persists_while_turning_ends_the_session() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    # The dwell runs from the first frame, so 20 frames hold it for 0.95 s.
    assert isinstance(stream.frames(1.0, TURNING_BENCH, zone=person(0.35)), Clear)
    decision = stream.frames(0.05, TURNING_BENCH, zone=person(0.35))
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_ZONE_UNCERTAIN)
    assert isinstance(decision, RampDown)
    assert "detection incertaine" in decision.verdict.detail


def test_a_frozen_camera_ends_the_session_past_the_stale_limit() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    assert isinstance(stream.frozen(0.5, TURNING_BENCH), Clear)
    decision = stream.frozen(0.05, TURNING_BENCH)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_CAMERA_LOST)
    assert isinstance(decision, RampDown)
    assert decision.verdict.detail.startswith("derniere image exploitable de la camera il y a")
    assert "arret controle" in decision.verdict.detail


def test_a_dropped_camera_is_the_same_as_a_frozen_one() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.last = None
    decision = stream.frozen(0.55, TURNING_BENCH)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_CAMERA_LOST)


def test_a_failed_camera_ends_the_session_at_once() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    decision = stream.frames(0.05, TURNING_BENCH, health=CameraHealth.FAILED)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_CAMERA_LOST)


def test_motion_with_no_camera_ever_is_stopped_once_the_stale_limit_passes() -> None:
    stream = Stream()
    assert isinstance(stream.frozen(0.5, TURNING_BENCH), Clear)
    decision = stream.frozen(0.05, TURNING_BENCH)
    assert isinstance(decision, RampDown)
    assert decision.verdict.detail.startswith("aucune image exploitable")


def test_frames_stamped_in_the_future_cannot_keep_the_camera_fresh() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.seq += 1
    ahead = PresenceObservation(
        frame_seq=stream.seq,
        at=Monotonic(stream.now + 60.0),
        health=CameraHealth.HEALTHY,
        zone=ZoneClear(),
        capsule=CapsuleState.EMPTY,
    )
    stream.last = ahead
    # Clamped to the instant it was judged at (the first frozen evaluation).
    assert isinstance(stream.frozen(0.55, TURNING_BENCH), Clear)
    decision = stream.frozen(0.05, TURNING_BENCH)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_CAMERA_LOST)


def test_a_restarted_frame_counter_is_not_fresh_evidence() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    restarted = PresenceObservation(
        frame_seq=0,
        at=stream.now,
        health=CameraHealth.HEALTHY,
        zone=ZoneClear(),
        capsule=CapsuleState.EMPTY,
    )
    stream.last = restarted
    decision = stream.frozen(0.55, TURNING_BENCH)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_CAMERA_LOST)


@pytest.mark.parametrize(
    ("frame", "seconds", "rule"),
    [
        ({"capsule": CapsuleState.EMPTY}, 1.0, RULE_PRESENCE_RIDER_ABSENT),
        ({"capsule": CapsuleState.UNKNOWN}, 3.0, RULE_PRESENCE_CAPSULE_UNKNOWN),
        (
            {"capsule": CapsuleState.OCCUPIED, "posture": RiderPosture(True, False)},
            0.5,
            RULE_PRESENCE_RIDER_UNBUCKLED,
        ),
        (
            {"capsule": CapsuleState.OCCUPIED, "posture": RiderPosture(False, True)},
            0.3,
            RULE_PRESENCE_LIMB_OUTSIDE,
        ),
    ],
)
def test_the_rider_rules_end_an_occupied_session_after_their_dwell(
    frame: dict[str, object], seconds: float, rule: str
) -> None:
    stream = Stream()
    _ready(stream, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED, posture=SEATED)
    before = stream.frames(seconds, TURNING_OCCUPIED, **frame)  # type: ignore[arg-type]
    assert isinstance(before, Clear), before
    decision = stream.frames(0.05, TURNING_OCCUPIED, **frame)  # type: ignore[arg-type]
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, rule)
    assert isinstance(decision, RampDown)
    assert decision.verdict.detail.endswith("arret controle")


def test_a_rider_leaning_out_of_view_briefly_is_tolerated() -> None:
    stream = Stream()
    _ready(stream, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    stream.frames(0.5, TURNING_OCCUPIED, capsule=CapsuleState.EMPTY)
    stream.frames(1.0, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    assert isinstance(stream.frames(0.5, TURNING_OCCUPIED, capsule=CapsuleState.EMPTY), Clear)


def test_posture_rules_hold_their_state_when_the_detector_stops_reporting_posture() -> None:
    stream = Stream()
    _ready(stream, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    stream.frames(
        0.25, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED, posture=RiderPosture(True, False)
    )
    decision = stream.frames(0.3, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    assert _stop(decision) == (SafetyAction.RAMP_DOWN, RULE_PRESENCE_RIDER_UNBUCKLED)


def test_degraded_frames_say_nothing_about_the_capsule() -> None:
    stream = Stream()
    _ready(stream, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    stream.frames(0.4, TURNING_OCCUPIED, capsule=CapsuleState.EMPTY, health=CameraHealth.DEGRADED)
    assert stream.monitor.latched is None


def test_somebody_in_a_bench_capsule_is_an_emergency() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    assert isinstance(stream.frames(0.2, TURNING_BENCH, capsule=CapsuleState.OCCUPIED), Clear)
    decision = stream.frames(0.05, TURNING_BENCH, capsule=CapsuleState.OCCUPIED)
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_BENCH_OCCUPIED)
    assert isinstance(decision, EmergencyStop)
    assert "BANC (personne a bord : NON)" in decision.verdict.detail


def test_the_occupancy_rules_apply_only_to_their_own_occupancy() -> None:
    """BENCH ignores an empty or unknown capsule; OCCUPIED expects an occupied one."""
    bench = Stream()
    _ready(bench, TURNING_BENCH)
    bench.frames(4.0, TURNING_BENCH, capsule=CapsuleState.UNKNOWN)
    assert bench.monitor.latched is None
    occupied = Stream()
    _ready(occupied, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    occupied.frames(4.0, TURNING_OCCUPIED, capsule=CapsuleState.OCCUPIED)
    assert occupied.monitor.latched is None
    unknown = MachineContext(MotionState.TURNING, None)
    nobody = Stream()
    _ready(nobody, unknown, capsule=CapsuleState.OCCUPIED, posture=RiderPosture(True, True))
    assert nobody.monitor.latched is None


# =========================================================================
# Latching and acknowledgement
# =========================================================================


def test_a_more_severe_verdict_replaces_the_latch_and_a_lesser_one_does_not() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frozen(0.55, TURNING_BENCH)
    first = stream.monitor.latched
    assert first is not None
    assert first.rule == RULE_PRESENCE_CAMERA_LOST
    decision = stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    assert _stop(decision) == (SafetyAction.QUICK_STOP, RULE_PRESENCE_INTRUSION)
    escalated = stream.monitor.latched
    stream.frames(0.05, TURNING_BENCH, health=CameraHealth.FAILED)
    assert stream.monitor.latched == escalated


def test_nothing_resumes_by_itself_the_latch_outlives_its_evidence() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    latched = stream.monitor.latched
    assert _stop(stream.frames(10.0, TURNING_BENCH)) == (
        SafetyAction.QUICK_STOP,
        RULE_PRESENCE_INTRUSION,
    )
    assert stream.monitor.latched == latched
    decision = stream.frames(3.0, AT_REST)
    assert _rules(decision) == (RULE_PRESENCE_LATCHED,)
    assert isinstance(decision, StartBlocked)
    assert decision.refusals[0] == PresenceRefusal(
        RULE_PRESENCE_LATCHED,
        "demarrage refuse : arret presence verrouille (presence_intrusion), "
        "acquittement nomme requis",
    )
    assert isinstance(stream.monitor.check_start(stream.now, Occupancy.BENCH), Err)


def test_an_acknowledgement_must_be_named_and_must_have_something_to_clear() -> None:
    stream = Stream()
    refused = stream.monitor.acknowledge(OPERATOR)
    assert refused == Err(NothingLatched())
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    unnamed = stream.monitor.acknowledge("   ")
    assert isinstance(unnamed, Err)
    assert isinstance(unnamed.error, Unattributed)
    assert stream.monitor.latched is not None


def test_a_named_acknowledgement_clears_the_latch_and_restarts_nothing() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    stream.frames(3.0, AT_REST)
    record = stream.monitor.acknowledge(OPERATOR)
    assert isinstance(record, Ok)
    assert record.value.operator == OPERATOR
    assert record.value.cleared == (RULE_PRESENCE_INTRUSION,)
    assert record.value.at == stream.now
    assert stream.monitor.latched is None
    assert isinstance(stream.frames(0.05, AT_REST), Clear)


def test_an_acknowledgement_while_the_person_is_still_there_re_fires() -> None:
    stream = Stream()
    _ready(stream, TURNING_BENCH)
    stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    first = stream.monitor.latched
    assert isinstance(stream.monitor.acknowledge(OPERATOR), Ok)
    decision = stream.frames(0.05, TURNING_BENCH, zone=person(0.9))
    assert isinstance(decision, EmergencyStop)
    assert first is not None
    assert decision.verdict.since > first.since


# =========================================================================
# Exhaustiveness guards: a new variant must be handled, not fall through
# =========================================================================


def _alien_frame(stream: Stream, **fields: object) -> PresenceObservation:
    stream.seq += 1
    frame = PresenceObservation(
        frame_seq=stream.seq,
        at=stream.now,
        health=CameraHealth.HEALTHY,
        zone=ZoneClear(),
        capsule=CapsuleState.OCCUPIED,
    )
    return replace(frame, **fields)  # type: ignore[arg-type]  # deliberately outside the union


def test_an_unknown_zone_view_fails_loudly() -> None:
    stream = Stream()
    alien = _alien_frame(stream, zone=cast("ZoneView", object()))
    with pytest.raises(AssertionError):
        stream.monitor.evaluate(stream.now, alien, AT_REST)


def test_an_unknown_capsule_state_fails_loudly_in_the_rules_and_in_the_gate() -> None:
    stream = Stream()
    alien = _alien_frame(stream, capsule=cast("CapsuleState", "levitating"))
    with pytest.raises(AssertionError):
        stream.monitor.evaluate(stream.now, alien, AT_REST)
    with pytest.raises(AssertionError):
        stream.monitor.check_start(stream.now, Occupancy.OCCUPIED)


def test_an_unknown_occupancy_fails_loudly() -> None:
    stream = Stream()
    _ready(stream)
    with pytest.raises(AssertionError):
        stream.monitor.check_start(stream.now, cast("Occupancy", "crew"))
