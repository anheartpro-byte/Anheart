"""What the presence monitor guarantees for ANY observation sequence (hypothesis).

Coverage says every branch ran; these say what holds across all of them:

1. a person in the danger zone while motion is possible always yields an
   emergency stop by the end of the confirmation window, whatever came before;
2. a camera with no fresh healthy frame never lets a machine that may be
   moving continue past the stale limit;
3. no presence verdict clears without an acknowledgement: once a stop was
   decided, every later decision is at least as severe while motion is
   possible, and a refusal naming the latch at rest;
4. at rest nothing ever latches and no stop is ever decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.presence.monitor import (
    DEFAULT_PRESENCE_LIMITS,
    RULE_PRESENCE_LATCHED,
    EmergencyStop,
    PresenceDecision,
    PresenceLimits,
    PresenceMonitor,
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
from src.result import Err
from src.training.types import Occupancy, SafetyAction
from src.units import Monotonic, Seconds

LIMITS: Final[PresenceLimits] = DEFAULT_PRESENCE_LIMITS
ORIGIN: Final[Monotonic] = Monotonic(1000.0)
MOVING: Final[tuple[MotionState, ...]] = (
    MotionState.ARMED,
    MotionState.TURNING,
    MotionState.UNKNOWN,
)


@dataclass(frozen=True, slots=True)
class Step:
    """One evaluation: how long after the previous one, what the source offers, the machine."""

    dt: float
    kind: str
    """``frame`` (a new frame), ``repeat`` (the previous one again), ``none``, ``regress``."""
    lag: float
    zone: ZoneView
    health: CameraHealth
    capsule: CapsuleState
    posture: RiderPosture | None
    context: MachineContext


def _person(confidence: float) -> ZoneView:
    return PersonInZone(Confidence(confidence))


zones: st.SearchStrategy[ZoneView] = st.one_of(
    st.just(ZoneClear()),
    st.builds(_person, st.floats(0.0, 1.0)),
)
contexts = st.builds(
    MachineContext,
    st.sampled_from(list(MotionState)),
    st.sampled_from([None, Occupancy.BENCH, Occupancy.OCCUPIED]),
)
postures = st.one_of(st.none(), st.builds(RiderPosture, st.booleans(), st.booleans()))


def steps(context: st.SearchStrategy[MachineContext] = contexts) -> st.SearchStrategy[Step]:
    return st.builds(
        Step,
        dt=st.floats(0.001, 0.4),
        kind=st.sampled_from(["frame", "frame", "frame", "repeat", "none", "regress"]),
        lag=st.floats(0.0, 0.3),
        zone=zones,
        health=st.sampled_from(list(CameraHealth)),
        capsule=st.sampled_from(list(CapsuleState)),
        posture=postures,
        context=context,
    )


class Driver:
    """Feeds steps into one monitor and keeps the oracle's own freshness record."""

    def __init__(self) -> None:
        self.clock: ManualClock = ManualClock(ORIGIN)
        self.monitor: PresenceMonitor = PresenceMonitor(clock=self.clock)
        self.seq: int = 0
        self.previous: PresenceObservation | None = None
        self.last_good: Monotonic | None = None
        self.failed: bool = False

    def apply(self, step: Step) -> PresenceDecision:
        now = self.clock.advance(Seconds(step.dt))
        offered: PresenceObservation | None
        match step.kind:
            case "repeat":
                offered = self.previous
            case "none":
                offered = None
            case _:
                # A "regress" frame reuses the last number, so it is new only when
                # nothing at all has been delivered yet: the first frame is news.
                fresh = step.kind == "frame" or self.previous is None
                if step.kind == "frame":
                    self.seq += 1
                offered = PresenceObservation(
                    frame_seq=self.seq,
                    at=Monotonic(now - step.lag),
                    health=step.health,
                    zone=step.zone,
                    capsule=step.capsule,
                    posture=step.posture,
                )
                if fresh:
                    self.previous = offered
                    self.failed = step.health is CameraHealth.FAILED
                    if step.health is CameraHealth.HEALTHY:
                        self.last_good = offered.at
        return self.monitor.evaluate(now, offered, step.context)

    def camera_age(self) -> float:
        since = ORIGIN if self.last_good is None else self.last_good
        return self.clock.monotonic() - since


def _severity(decision: PresenceDecision) -> SafetyAction:
    if isinstance(decision, (RampDown, EmergencyStop)):
        return decision.verdict.action
    return SafetyAction.NONE


@settings(max_examples=300, deadline=None)
@given(
    prefix=st.lists(steps(), max_size=30),
    intrusion=st.lists(
        st.tuples(
            st.floats(0.001, 0.1),
            st.floats(0.0, 0.05),
            st.floats(LIMITS.raise_confidence, 1.0),
            st.sampled_from([CameraHealth.HEALTHY, CameraHealth.DEGRADED]),
            contexts.filter(lambda context: context.motion.motion_possible),
        ),
        min_size=1,
        max_size=30,
    ),
)
def test_a_person_in_the_zone_while_moving_is_always_stopped_within_the_window(
    prefix: list[Step],
    intrusion: list[tuple[float, float, float, CameraHealth, MachineContext]],
) -> None:
    driver = Driver()
    for step in prefix:
        driver.apply(step)
    started: Monotonic | None = None
    for dt, lag, confidence, health, context in intrusion:
        step = Step(
            dt=dt,
            kind="frame",
            lag=lag,
            zone=PersonInZone(Confidence(confidence)),
            health=health,
            capsule=CapsuleState.UNKNOWN,
            posture=None,
            context=context,
        )
        decision = driver.apply(step)
        now = driver.clock.monotonic()
        if started is None:
            started = Monotonic(now - lag)
        if now - started >= LIMITS.confirm_window:
            assert isinstance(decision, EmergencyStop), (now - started, decision)
            assert decision.verdict.action is SafetyAction.QUICK_STOP


@settings(max_examples=300, deadline=None)
@given(sequence=st.lists(steps(), min_size=1, max_size=60))
def test_a_stale_camera_never_lets_a_moving_machine_continue(sequence: list[Step]) -> None:
    driver = Driver()
    for step in sequence:
        decision = driver.apply(step)
        stale = driver.failed or driver.camera_age() > LIMITS.stale_after
        if step.context.motion.motion_possible and stale:
            assert _severity(decision) >= SafetyAction.RAMP_DOWN, (driver.camera_age(), decision)


@settings(max_examples=300, deadline=None)
@given(sequence=st.lists(steps(), min_size=1, max_size=80))
def test_no_presence_verdict_clears_without_an_acknowledgement(sequence: list[Step]) -> None:
    driver = Driver()
    worst = SafetyAction.NONE
    for step in sequence:
        decision = driver.apply(step)
        now = driver.clock.monotonic()
        if worst is not SafetyAction.NONE:
            assert driver.monitor.latched is not None
            if step.context.motion.motion_possible:
                assert _severity(decision) >= worst, decision
            else:
                assert isinstance(decision, StartBlocked), decision
                assert decision.refusals[0].rule == RULE_PRESENCE_LATCHED
            for occupancy in Occupancy:
                assert isinstance(driver.monitor.check_start(now, occupancy), Err)
        worst = max(worst, _severity(decision))


@settings(max_examples=200, deadline=None)
@given(
    sequence=st.lists(
        steps(
            st.builds(
                MachineContext,
                st.just(MotionState.AT_REST),
                st.sampled_from([None, Occupancy.BENCH, Occupancy.OCCUPIED]),
            )
        ),
        min_size=1,
        max_size=60,
    )
)
def test_at_rest_nothing_latches_and_nothing_is_stopped(sequence: list[Step]) -> None:
    driver = Driver()
    for step in sequence:
        decision = driver.apply(step)
        assert _severity(decision) is SafetyAction.NONE
        assert driver.monitor.latched is None
        assert driver.monitor.live == ()
