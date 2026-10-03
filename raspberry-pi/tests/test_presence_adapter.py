"""The presence adapter: machine state read from the runtime, decisions applied through it.

Two halves. The first drives :class:`~src.presence.adapter.PresenceGuard` and
:class:`~src.presence.adapter.PresenceAcknowledger` against a recording fake of
the runtime port, so every branch is pinned. The second closes the loop: the
REAL :class:`~src.training.runtime.TrainingRuntime` over a ``SimulatedDrive``
plant, the simulated camera, and the guard stepped at 20 Hz between 5 Hz
control ticks - and proves the motor reaches 0 after an intrusion, a frozen
camera, and a person in a capsule declared empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.geometry import MachineGeometry
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.presence.adapter import (
    ESTOP_SOURCE_PREFIX,
    PRESENCE_PERIOD,
    PresenceAcknowledger,
    PresenceGuard,
    RuntimePort,
    motion_state,
    session_occupancy,
)
from src.presence.monitor import (
    DEFAULT_PRESENCE_LIMITS,
    RULE_PRESENCE_BENCH_OCCUPIED,
    RULE_PRESENCE_CAMERA_LOST,
    RULE_PRESENCE_INTRUSION,
    RULE_PRESENCE_LATCHED,
    RULE_PRESENCE_ZONE_NOT_CLEAR,
    Clear,
    EmergencyStop,
    PresenceDecision,
    PresenceMonitor,
    RampDown,
    StartBlocked,
)
from src.presence.simulated import CapsuleChange, Freeze, Intrusion, SimulatedCamera
from src.presence.types import CapsuleState, Confidence, MotionState
from src.result import Err, Ok, Result
from src.training.runtime import (
    EndReason,
    ManualSession,
    RuntimeLimits,
    RuntimeState,
    TrainingRuntime,
)
from src.training.safety import (
    RULE_OPERATOR_ESTOP,
    AcknowledgeRefusal,
    EmergencyStopStillLatched,
    GoSilentIsTerminal,
    NothingLatched,
    SafetyAcknowledgement,
    SafetyLimits,
    Unattributed,
)
from src.training.types import (
    Occupancy,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    SpeedView,
    TelemetrySnapshot,
)
from src.units import (
    Bpm,
    Metres,
    Monotonic,
    MotorRpm,
    RpmPerSecond,
    Seconds,
    UnixMillis,
    motor_to_output_rpm,
)

OPERATOR: Final[str] = "Dr Ada Okonkwo"
GEOMETRY: Final[MachineGeometry] = MachineGeometry(radius=Metres(1.5))
SAFETY: Final[SafetyLimits] = SafetyLimits(hard_max_bpm=Bpm(170), critical_bpm=Bpm(185))
LIMITS: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10)
)
CEILING: Final[MotorRpm] = MotorRpm(300)
TICK_EVERY: Final[int] = 4
"""One control tick (0.2 s) every four presence steps (0.05 s), as the console runs them."""


def _runtime(clock: ManualClock) -> tuple[TrainingRuntime, SimulatedDrive]:
    drive = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    runtime = TrainingRuntime(
        clock=clock, drive=drive, geometry=GEOMETRY, limits=LIMITS, safety=SAFETY
    )
    return runtime, drive


# =========================================================================
# A recording fake of the runtime port
# =========================================================================


def _base_snapshot() -> TelemetrySnapshot:
    """A real snapshot, from a real runtime that has never read its drive (stale)."""
    runtime, _ = _runtime(ManualClock(Monotonic(50.0)))
    return runtime.snapshot()


BASE: Final[TelemetrySnapshot] = _base_snapshot()


def _snapshot(rpm: int, *, fresh: bool = True) -> TelemetrySnapshot:
    measured = SpeedView.from_motor_rpm(
        MotorRpm(rpm),
        ratio=GEOMETRY.ratio,
        radius=GEOMETRY.radius,
        nominal_rpm=GEOMETRY.nominal_rpm,
        base_hz=GEOMETRY.base_hz,
    )
    return replace(BASE, measured=measured, drive_status_age=Seconds(0.0) if fresh else None)


def _manual(occupancy: Occupancy) -> ManualSession:
    return ManualSession(
        occupancy=occupancy, operator=OPERATOR, ceiling=CEILING, cooldown=Seconds(5.0)
    )


@dataclass
class FakeRuntime:
    """Records what the guard asks of the runtime. Satisfies RuntimePort structurally."""

    state_now: RuntimeState = RuntimeState.IDLE
    enabled: bool = False
    session: ManualSession | None = None
    picture: TelemetrySnapshot = field(default_factory=lambda: _snapshot(0))
    ack: Result[SafetyAcknowledgement, AcknowledgeRefusal] = field(
        default_factory=lambda: Err(NothingLatched())
    )
    estops: list[str] = field(default_factory=list[str])
    trips: list[tuple[str, SafetyAction, str]] = field(
        default_factory=list[tuple[str, SafetyAction, str]]
    )

    @property
    def state(self) -> RuntimeState:
        return self.state_now

    @property
    def output_enabled(self) -> bool:
        return self.enabled

    @property
    def manual(self) -> ManualSession | None:
        return self.session

    def snapshot(self) -> TelemetrySnapshot:
        return self.picture

    def request_estop(self, source: str) -> SafetyVerdict:
        self.estops.append(source)
        return SafetyVerdict(
            action=SafetyAction.QUICK_STOP,
            rule=RULE_OPERATOR_ESTOP,
            detail=source,
            latched=True,
            since=Monotonic(0.0),
        )

    def trip_from_thread(self, rule: str, action: SafetyAction, detail: str = "") -> None:
        self.trips.append((rule, action, detail))

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        del operator, estop_released
        return self.ack


@dataclass
class Rig:
    """The guard over the fake runtime, a simulated camera and a manual clock."""

    clock: ManualClock
    runtime: FakeRuntime
    camera: SimulatedCamera
    guard: PresenceGuard

    def step(self, seconds: float) -> PresenceDecision:
        decision: PresenceDecision = Clear()
        for _ in range(round(seconds / PRESENCE_PERIOD)):
            self.clock.advance(PRESENCE_PERIOD)
            decision = self.guard.step(self.clock.monotonic())
        return decision


def _rig(runtime: FakeRuntime | None = None) -> Rig:
    clock = ManualClock(Monotonic(10.0))
    fake = FakeRuntime() if runtime is None else runtime
    camera = SimulatedCamera(clock)
    port: RuntimePort = fake
    guard = PresenceGuard(runtime=port, source=camera, monitor=PresenceMonitor(clock=clock))
    return Rig(clock=clock, runtime=fake, camera=camera, guard=guard)


# =========================================================================
# Reading the machine
# =========================================================================


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (FakeRuntime(picture=_snapshot(0, fresh=False)), MotionState.UNKNOWN),
        (FakeRuntime(picture=_snapshot(400, fresh=False)), MotionState.UNKNOWN),
        (FakeRuntime(picture=_snapshot(120)), MotionState.TURNING),
        (FakeRuntime(picture=_snapshot(-3)), MotionState.TURNING),
        (FakeRuntime(state_now=RuntimeState.RUNNING), MotionState.ARMED),
        (FakeRuntime(state_now=RuntimeState.ENDING), MotionState.ARMED),
        (FakeRuntime(state_now=RuntimeState.FINISHED, enabled=True), MotionState.ARMED),
        (FakeRuntime(state_now=RuntimeState.FINISHED), MotionState.AT_REST),
        (FakeRuntime(), MotionState.AT_REST),
    ],
)
def test_the_motion_state_comes_from_the_measured_speed(
    runtime: FakeRuntime, expected: MotionState
) -> None:
    assert motion_state(runtime) is expected


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        (FakeRuntime(session=_manual(Occupancy.BENCH)), Occupancy.BENCH),
        (
            FakeRuntime(state_now=RuntimeState.RUNNING, session=_manual(Occupancy.OCCUPIED)),
            Occupancy.OCCUPIED,
        ),
        (FakeRuntime(state_now=RuntimeState.RUNNING), Occupancy.OCCUPIED),
        (FakeRuntime(state_now=RuntimeState.ENDING), Occupancy.OCCUPIED),
        (FakeRuntime(state_now=RuntimeState.FINISHED), None),
        (FakeRuntime(), None),
    ],
)
def test_a_programme_is_occupied_and_a_manual_session_says_what_it_declared(
    runtime: FakeRuntime, expected: Occupancy | None
) -> None:
    assert session_occupancy(runtime) is expected


def test_a_fresh_runtime_that_never_read_its_drive_is_unknown_not_stopped() -> None:
    runtime, _ = _runtime(ManualClock())
    assert motion_state(runtime) is MotionState.UNKNOWN


# =========================================================================
# Applying decisions
# =========================================================================


def test_nothing_is_applied_while_nothing_is_wrong() -> None:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200)))
    assert isinstance(rig.step(1.0), Clear)
    assert rig.runtime.estops == []
    assert rig.runtime.trips == []
    assert rig.guard.monitor.latched is None


def test_a_refused_start_at_rest_touches_nothing() -> None:
    rig = _rig()
    assert isinstance(rig.step(0.5), StartBlocked)
    assert rig.runtime.estops == []
    assert rig.runtime.trips == []


def test_an_emergency_stop_is_applied_once_estop_first_then_the_rule_id() -> None:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200)))
    rig.step(0.5)
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    decision = rig.step(0.05)
    assert isinstance(decision, EmergencyStop)
    detail = decision.verdict.detail
    assert rig.runtime.estops == [f"{ESTOP_SOURCE_PREFIX}: {detail}"]
    assert rig.runtime.trips == [(RULE_PRESENCE_INTRUSION, SafetyAction.QUICK_STOP, detail)]
    rig.step(2.0)
    assert len(rig.runtime.estops) == 1, "a standing e-stop must not re-send the emergency zero"
    assert len(rig.runtime.trips) == 1


def test_a_ramp_down_is_one_trip_and_an_escalation_is_applied_on_top() -> None:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200)))
    rig.step(0.5)
    now = rig.clock.monotonic()
    rig.camera.schedule(Freeze(start=now, end=Monotonic(now + 1.0)))
    decision = rig.step(0.6)
    assert isinstance(decision, RampDown)
    assert rig.runtime.trips == [
        (RULE_PRESENCE_CAMERA_LOST, SafetyAction.RAMP_DOWN, decision.verdict.detail)
    ]
    assert rig.runtime.estops == []
    rig.step(0.5)
    assert len(rig.runtime.trips) == 1
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    escalated = rig.step(0.1)
    assert isinstance(escalated, EmergencyStop)
    assert len(rig.runtime.estops) == 1
    assert [trip[0] for trip in rig.runtime.trips] == [
        RULE_PRESENCE_CAMERA_LOST,
        RULE_PRESENCE_INTRUSION,
    ]


def test_after_an_acknowledgement_a_verdict_that_re_fires_is_applied_again() -> None:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200)))
    rig.step(0.5)
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    rig.step(0.1)
    assert isinstance(rig.guard.acknowledge(OPERATOR), Ok)
    rig.step(0.05)
    assert len(rig.runtime.estops) == 2


def test_a_refused_presence_acknowledgement_keeps_the_record_of_what_was_applied() -> None:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200)))
    rig.step(0.5)
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    rig.step(0.1)
    assert isinstance(rig.guard.acknowledge(""), Err)
    rig.step(0.5)
    assert len(rig.runtime.estops) == 1


def test_the_start_gate_steps_first_so_it_judges_the_current_frame() -> None:
    rig = _rig()
    rig.step(2.5)
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    # One new frame is on offer and nobody has stepped: the gate must ingest it itself.
    now = rig.clock.advance(PRESENCE_PERIOD)
    gate = rig.guard.start_gate(now, Occupancy.BENCH)
    assert isinstance(gate, Err)
    assert [refusal.rule for refusal in gate.error.refusals] == [RULE_PRESENCE_ZONE_NOT_CLEAR]


# =========================================================================
# The console's acknowledger
# =========================================================================


def _record(*cleared: str) -> SafetyAcknowledgement:
    return SafetyAcknowledgement(
        operator=OPERATOR, at=Monotonic(1.0), wall_clock=UnixMillis(1000), cleared=cleared
    )


def _latched_rig(ack: Result[SafetyAcknowledgement, AcknowledgeRefusal]) -> Rig:
    rig = _rig(FakeRuntime(state_now=RuntimeState.RUNNING, picture=_snapshot(200), ack=ack))
    rig.step(0.5)
    rig.camera.schedule(Intrusion(start=rig.clock.monotonic(), confidence=Confidence(0.9)))
    rig.step(0.1)
    assert rig.guard.monitor.latched is not None
    return rig


def _acknowledger(rig: Rig) -> PresenceAcknowledger:
    return PresenceAcknowledger(rig.runtime, rig.guard)


def test_an_accepted_acknowledgement_clears_the_presence_latch_too() -> None:
    rig = _latched_rig(Ok(_record(RULE_OPERATOR_ESTOP)))
    outcome = _acknowledger(rig).acknowledge(OPERATOR, estop_released=True)
    assert isinstance(outcome, Ok)
    assert outcome.value.cleared == (RULE_OPERATOR_ESTOP, RULE_PRESENCE_INTRUSION)
    assert rig.guard.monitor.latched is None


def test_an_accepted_acknowledgement_with_no_presence_latch_is_passed_through() -> None:
    record = _record(RULE_OPERATOR_ESTOP)
    rig = _rig(FakeRuntime(ack=Ok(record)))
    assert _acknowledger(rig).acknowledge(OPERATOR) == Ok(record)


def test_nothing_latched_in_the_runtime_still_clears_a_presence_latch() -> None:
    rig = _latched_rig(Err(NothingLatched()))
    outcome = _acknowledger(rig).acknowledge(OPERATOR)
    assert isinstance(outcome, Ok)
    assert outcome.value.cleared == (RULE_PRESENCE_INTRUSION,)
    assert rig.guard.monitor.latched is None


def test_nothing_latched_anywhere_is_refused() -> None:
    rig = _rig()
    assert _acknowledger(rig).acknowledge(OPERATOR) == Err(NothingLatched())


@pytest.mark.parametrize(
    "refusal",
    [
        Unattributed("no operator"),
        GoSilentIsTerminal("comms_lost"),
        EmergencyStopStillLatched(Monotonic(3.0)),
    ],
)
def test_a_runtime_refusal_leaves_the_presence_latch_standing(
    refusal: AcknowledgeRefusal,
) -> None:
    rig = _latched_rig(Err(refusal))
    assert _acknowledger(rig).acknowledge(OPERATOR) == Err(refusal)
    assert rig.guard.monitor.latched is not None


# =========================================================================
# The closed loop: the real runtime, the plant, the camera
# =========================================================================


@dataclass
class Loop:
    """The console's two loops, interleaved as the panel runs them."""

    clock: ManualClock
    runtime: TrainingRuntime
    drive: SimulatedDrive
    camera: SimulatedCamera
    guard: PresenceGuard
    acknowledger: PresenceAcknowledger
    steps: int = 0

    @property
    def now(self) -> Monotonic:
        return self.clock.monotonic()

    async def step(self) -> PresenceDecision:
        self.clock.advance(PRESENCE_PERIOD)
        self.drive.advance(self.now)
        decision = self.guard.step(self.now)
        if self.steps % TICK_EVERY == 0:
            await self.runtime.tick(self.now)
        self.steps += 1
        return decision

    async def run(self, seconds: float) -> None:
        for _ in range(round(seconds / PRESENCE_PERIOD)):
            await self.step()

    def measured(self) -> int:
        return int(self.runtime.snapshot().measured.motor_rpm)

    async def spin_up(self, occupancy: Occupancy = Occupancy.BENCH) -> None:
        await self.run(2.5)
        assert self.guard.start_gate(self.now, occupancy) == Ok(None)
        started = await self.runtime.start_manual(occupancy, OPERATOR, CEILING)
        assert isinstance(started, Ok), started
        target = self.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(250), GEOMETRY.ratio))
        assert target == Ok(MotorRpm(250))
        for _ in range(round(40.0 / PRESENCE_PERIOD)):
            await self.step()
            if self.measured() >= 200:
                return
        pytest.fail("the machine never spun up")

    async def until_stopped(self) -> None:
        for _ in range(round(60.0 / PRESENCE_PERIOD)):
            await self.step()
            if self.measured() == 0 and self.runtime.state is RuntimeState.FINISHED:
                return
        pytest.fail(f"the motor did not stop: {self.measured()} rpm, {self.runtime.state}")


def _latched(loop: Loop) -> SafetyVerdict | None:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return loop.guard.monitor.latched


def _loop() -> Loop:
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_700_000_000_000))
    runtime, drive = _runtime(clock)
    camera = SimulatedCamera(clock)
    guard = PresenceGuard(runtime=runtime, source=camera, monitor=PresenceMonitor(clock=clock))
    acknowledger = PresenceAcknowledger(runtime, guard)
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    return Loop(clock, runtime, drive, camera, guard, acknowledger)


async def test_an_intrusion_while_turning_brings_the_motor_to_zero_and_stays_latched() -> None:
    loop = _loop()
    await loop.spin_up()
    t0 = loop.now
    loop.camera.schedule(Intrusion(start=t0, end=Monotonic(t0 + 15.0), confidence=Confidence(0.6)))
    decision: PresenceDecision = Clear()
    while not isinstance(decision, EmergencyStop):
        decision = await loop.step()
        assert loop.now - t0 <= 1.0, "no e-stop a second after the intrusion"
    budget = DEFAULT_PRESENCE_LIMITS.confirm_window + 2 * PRESENCE_PERIOD
    assert loop.now - t0 <= budget + 1e-9
    # request_estop zeroed the reference synchronously, before any tick.
    assert loop.drive.commanded_setpoint == 0
    assert loop.runtime.end_reason is EndReason.EMERGENCY_STOP

    await loop.until_stopped()
    assert loop.runtime.applied_rpm == 0
    assert loop.runtime.output_enabled is False
    standing = loop.runtime.standing
    assert standing is not None
    assert standing.action is SafetyAction.QUICK_STOP
    floor = loop.runtime.supervisor.floor
    assert floor is not None
    assert floor.rule == RULE_PRESENCE_INTRUSION

    # Nothing restarts: the gate refuses on the latch and on the person still there.
    gate = loop.guard.start_gate(loop.now, Occupancy.BENCH)
    assert isinstance(gate, Err)
    rules = [refusal.rule for refusal in gate.error.refusals]
    assert rules == [RULE_PRESENCE_LATCHED, RULE_PRESENCE_ZONE_NOT_CLEAR]

    # The mushroom question fails closed, and then the presence latch stays too.
    refused = loop.acknowledger.acknowledge(OPERATOR)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, EmergencyStopStillLatched)
    assert _latched(loop) is not None
    cleared = loop.acknowledger.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(cleared, Ok)
    assert RULE_OPERATOR_ESTOP in cleared.value.cleared
    assert RULE_PRESENCE_INTRUSION in cleared.value.cleared
    assert _latched(loop) is None
    assert loop.runtime.standing is None

    # Still somebody there: still refused, at rest, with nothing latched again.
    await loop.run(1.0)
    gate = loop.guard.start_gate(loop.now, Occupancy.BENCH)
    assert isinstance(gate, Err)
    assert [refusal.rule for refusal in gate.error.refusals] == [RULE_PRESENCE_ZONE_NOT_CLEAR]
    assert loop.guard.monitor.latched is None

    # They leave; after the clear hysteresis a NEW, explicit start is possible.
    await loop.run(t0 + 15.0 - loop.now + 2.5)
    assert loop.runtime.mode is RunMode.REPOS
    assert loop.runtime.applied_rpm == 0
    assert loop.guard.start_gate(loop.now, Occupancy.BENCH) == Ok(None)
    assert isinstance(await loop.runtime.start_manual(Occupancy.BENCH, OPERATOR, CEILING), Ok)


async def test_a_frozen_camera_while_turning_ends_the_session_on_the_ramp() -> None:
    loop = _loop()
    await loop.spin_up()
    t0 = loop.now
    loop.camera.schedule(Freeze(start=t0))
    decision: PresenceDecision = Clear()
    while not isinstance(decision, RampDown):
        decision = await loop.step()
        assert loop.now - t0 <= 2.0, "a frozen camera was allowed to keep the machine turning"
    assert decision.verdict.rule == RULE_PRESENCE_CAMERA_LOST
    assert loop.now - t0 <= DEFAULT_PRESENCE_LIMITS.stale_after + 3 * PRESENCE_PERIOD
    await loop.until_stopped()
    assert loop.runtime.end_reason is EndReason.SAFETY_VERDICT
    assert loop.runtime.applied_rpm == 0
    assert loop.runtime.output_enabled is False
    floor = loop.runtime.supervisor.floor
    assert floor is not None
    assert floor.rule == RULE_PRESENCE_CAMERA_LOST
    assert floor.action is SafetyAction.RAMP_DOWN
    # No mushroom was involved: the plain acknowledgement clears both latches.
    assert isinstance(loop.acknowledger.acknowledge(OPERATOR), Ok)
    assert loop.guard.monitor.latched is None


async def test_somebody_in_a_capsule_declared_empty_is_an_emergency_stop() -> None:
    loop = _loop()
    await loop.spin_up(Occupancy.BENCH)
    t0 = loop.now
    loop.camera.schedule(CapsuleChange(start=t0, state=CapsuleState.OCCUPIED))
    decision: PresenceDecision = Clear()
    while not isinstance(decision, EmergencyStop):
        decision = await loop.step()
        assert loop.now - t0 <= 1.0
    assert decision.verdict.rule == RULE_PRESENCE_BENCH_OCCUPIED
    await loop.until_stopped()
    assert loop.runtime.applied_rpm == 0
    assert loop.runtime.output_enabled is False
    gate = loop.guard.start_gate(loop.now, Occupancy.BENCH)
    assert isinstance(gate, Err)
    assert RULE_PRESENCE_BENCH_OCCUPIED in [refusal.rule for refusal in gate.error.refusals]


# =========================================================================
# Exhaustiveness guards: a new variant must be handled, not fall through
# =========================================================================


def test_an_unknown_decision_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _rig()

    def alien(
        _self: PresenceMonitor, now: Monotonic, observation: object, context: object
    ) -> PresenceDecision:
        del now, observation, context
        return cast("PresenceDecision", object())

    monkeypatch.setattr(PresenceMonitor, "evaluate", alien)
    with pytest.raises(AssertionError):
        rig.guard.step(rig.clock.monotonic())


def test_an_unknown_acknowledgement_outcome_fails_loudly() -> None:
    rig = _rig(FakeRuntime(ack=cast("Result[SafetyAcknowledgement, AcknowledgeRefusal]", object())))
    with pytest.raises(AssertionError):
        _acknowledger(rig).acknowledge(OPERATOR)


def test_an_unknown_acknowledgement_refusal_fails_loudly() -> None:
    rig = _rig(FakeRuntime(ack=Err(cast("AcknowledgeRefusal", object()))))
    with pytest.raises(AssertionError):
        _acknowledger(rig).acknowledge(OPERATOR)
