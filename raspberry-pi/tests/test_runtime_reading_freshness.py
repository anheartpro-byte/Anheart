from __future__ import annotations

import math
from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.result import Err, is_ok
from src.training import runtime as runtime_module
from src.training.hr_control import ControlPlan, HeartRateController, HeartRateTracker
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import HeldAtStandstill, RiseHold
from src.training.types import HEART_RATE_STALE_AFTER, Occupancy, SafetyAction, SignalQuality
from src.units import Bpm, Monotonic, MotorRpm, Seconds, motor_to_output_rpm
from tests.test_runtime import (
    GEOMETRY,
    LIMITS,
    OPERATOR,
    REAL_LIMITS,
    TICK,
    Rig,
    build_rig,
    climbing_profile,
)


async def programme() -> Rig:
    rig = build_rig(
        profile=climbing_profile(),
        limits=REAL_LIMITS,
        motion=DEFAULT_MOTION_LIMITS,
    )
    rig.fed_bpm = Bpm(70)
    assert is_ok(await rig.start())
    return rig


async def first_step(rig: Rig) -> float:
    for _ in range(750):
        await rig.step()
        if rig.runtime.applied_rpm > 0:
            return float(rig.now)
    pytest.fail("the reference programme never moved")


async def turning() -> Rig:
    rig = await programme()
    await first_step(rig)
    await rig.run(3)
    return rig


async def occupied() -> Rig:
    rig = build_rig(motion=DEFAULT_MOTION_LIMITS)
    rig.fed_bpm = Bpm(70)
    rig.feed(Bpm(70))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MotorRpm(1000)))
    await rig.run(6)
    return rig


def target(rig: Rig, rpm: int) -> None:
    assert is_ok(rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(rpm), GEOMETRY.ratio)))


async def missing(rig: Rig, seconds: float) -> list[int]:
    values = [int(rig.runtime.applied_rpm)]
    for _ in range(round(seconds / TICK)):
        rig.feed(None, quality=SignalQuality.NO_SIGNAL)
        await rig.step(feed=False)
        values.append(int(rig.runtime.applied_rpm))
    return values


async def assert_lost_climb_is_held(rig: Rig) -> None:
    rig.feed(Bpm(70))
    last = rig.now
    before = rig.runtime.applied_rpm
    for _ in range(35):
        rig.feed(None, quality=SignalQuality.NO_SIGNAL)
        snapshot = await rig.step(feed=False)
        after = rig.runtime.applied_rpm
        if rig.now - last > REAL_LIMITS.rise_reading_within:
            assert after <= before, (rig.now - last, before, after)
        assert snapshot.safety_action is SafetyAction.NONE
        before = after


async def test_a_programme_at_rest_does_not_take_its_first_step_without_a_recent_reading() -> None:
    nominal = await programme()
    starts = await first_step(nominal)
    rig = await programme()
    await rig.run(starts - float(rig.now) - 3.4)
    assert rig.runtime.applied_rpm == 0
    await assert_lost_climb_is_held(rig)
    assert rig.runtime.applied_rpm == 0


async def test_a_turning_programme_holds_the_applied_setpoint_once_no_reading_is_recent() -> None:
    await assert_lost_climb_is_held(await turning())


async def test_a_manual_climb_with_a_person_on_board_holds_once_no_reading_is_recent() -> None:
    rig = await occupied()
    target(rig, 1000)
    await rig.run(6)
    assert rig.runtime.applied_rpm > 0
    await assert_lost_climb_is_held(rig)
    assert rig.runtime.manual_rise_hold() is RiseHold.NO_HEART_RATE


async def test_a_manual_target_over_a_stopped_arm_is_refused_once_no_reading_is_recent() -> None:
    rig = await occupied()
    rig.feed(Bpm(70))
    await missing(rig, 2.6)
    result = rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio))
    assert result == Err(HeldAtStandstill(RiseHold.NO_HEART_RATE))
    assert rig.runtime.manual_rise_hold() is RiseHold.NO_HEART_RATE
    assert rig.runtime.applied_rpm == 0


async def test_an_isolated_reading_without_rate_does_not_hold_the_climb() -> None:
    rig = await occupied()
    target(rig, 1000)
    await rig.run(6)
    rig.feed(Bpm(70))
    before = rig.runtime.applied_rpm
    values = await missing(rig, 2.2)
    assert rig.runtime.manual_rise_hold() is None
    assert values[-1] > before
    await missing(rig, 0.4)
    assert rig.runtime.manual_rise_hold() is RiseHold.NO_HEART_RATE
    rig.feed(Bpm(70))
    await rig.step(feed=False)
    assert rig.runtime.applied_rpm > values[-1]


@pytest.mark.parametrize("at_rest", [True, False])
async def test_the_climb_resumes_without_a_jump_when_a_reading_returns(at_rest: bool) -> None:
    rig = await occupied()
    if not at_rest:
        target(rig, 1000)
        await rig.run(6)
    rig.feed(Bpm(70))
    await missing(rig, 2.6)
    held = rig.runtime.applied_rpm
    if at_rest:
        assert rig.runtime.set_manual_target(
            motor_to_output_rpm(MotorRpm(1000), GEOMETRY.ratio)
        ) == Err(HeldAtStandstill(RiseHold.NO_HEART_RATE))
    await missing(rig, 4)
    assert rig.runtime.applied_rpm == held
    rig.feed(Bpm(70))
    await rig.step(feed=False)
    if at_rest:
        assert rig.runtime.applied_rpm == 0
        target(rig, 1000)
        await rig.run(1)
        assert rig.runtime.applied_rpm > 0
    else:
        assert held < rig.runtime.applied_rpm <= held + 4


@pytest.mark.parametrize("delay", [0, -1, math.nan, math.inf, HEART_RATE_STALE_AFTER + 0.1])
def test_the_rise_gate_delay_is_refused_when_not_positive_or_looser_than_the_regulation_freshness(
    delay: float,
) -> None:
    with pytest.raises(ValueError, match="rise_reading_within"):
        replace(LIMITS, rise_reading_within=Seconds(delay))


@pytest.mark.parametrize("age", [None, Seconds(math.nan), Seconds(math.inf), Seconds(-math.inf)])
async def test_an_unknown_or_non_finite_reading_age_holds_an_occupied_manual_target(
    monkeypatch: pytest.MonkeyPatch, age: Seconds | None
) -> None:
    rig = await occupied()

    def unavailable(_self: HeartRateTracker, _now: Monotonic) -> Seconds | None:
        return age

    monkeypatch.setattr(HeartRateTracker, "age", unavailable)
    assert rig.runtime.set_manual_target(motor_to_output_rpm(MotorRpm(200), GEOMETRY.ratio)) == Err(
        HeldAtStandstill(RiseHold.NO_HEART_RATE)
    )


@pytest.mark.parametrize("delay", [Seconds(1.5), Seconds(2.5), HEART_RATE_STALE_AFTER])
async def test_a_reading_at_the_configured_rise_age_limit_still_permits_a_manual_target(
    delay: Seconds,
) -> None:
    rig = build_rig(limits=replace(LIMITS, rise_reading_within=delay))
    rig.fed_bpm = Bpm(70)
    rig.feed(Bpm(70))
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    assert is_ok(await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, MotorRpm(1000)))
    await rig.run(6)
    rig.feed(Bpm(70))
    rig.clock.advance(delay)
    target(rig, 200)
    assert rig.runtime.manual_rise_hold() is None
    rig.clock.advance(Seconds(0.001))
    assert rig.runtime.manual_rise_hold() is RiseHold.NO_HEART_RATE


@pytest.mark.parametrize("at_rest", [True, False])
async def test_the_regulation_residue_stays_bounded_while_the_rise_gate_is_closed(
    monkeypatch: pytest.MonkeyPatch, at_rest: bool
) -> None:
    controllers: list[HeartRateController] = []

    def capture(plan: ControlPlan, *, initial_rpm: MotorRpm) -> HeartRateController:
        controller = HeartRateController(plan, initial_rpm=initial_rpm)
        controllers.append(controller)
        return controller

    monkeypatch.setattr(runtime_module, "HeartRateController", capture)
    if at_rest:
        nominal = await programme()
        starts = await first_step(nominal)
        rig = await programme()
        await rig.run(starts - float(rig.now) - 3.4)
    else:
        rig = await programme()
        await first_step(rig)
        await rig.run(3)
    controller = controllers[-1]
    rig.feed(Bpm(70))
    last = rig.now
    previous = rig.runtime.applied_rpm
    closed_residues: list[float] = []
    for _ in range(35):
        rig.feed(None, quality=SignalQuality.NO_SIGNAL)
        await rig.step(feed=False)
        applied = rig.runtime.applied_rpm
        bound = (
            controller.plan.speed.min_run_rpm + controller.plan.speed.start_hysteresis_rpm
            if applied == 0
            else 1
        )
        assert abs(controller.residue) <= bound
        if rig.now - last > REAL_LIMITS.rise_reading_within:
            assert applied <= previous
            assert controller.demand == applied
            closed_residues.append(controller.residue)
        previous = applied
    assert closed_residues
    if at_rest:
        assert max(closed_residues) == (
            controller.plan.speed.min_run_rpm + controller.plan.speed.start_hysteresis_rpm
        )
    else:
        assert set(closed_residues) == {0.0}
    held = rig.runtime.applied_rpm
    rig.feed(Bpm(70))
    await rig.step(feed=False)
    assert rig.runtime.applied_rpm - held <= (
        controller.plan.speed.min_run_rpm + controller.plan.speed.start_hysteresis_rpm
        if held == 0
        else 4
    )
    await rig.run(80 if at_rest else 10)
    assert rig.runtime.applied_rpm > held


@settings(max_examples=35, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    readings=st.lists(st.tuples(st.booleans(), st.integers(4, 6)), min_size=15, max_size=35),
    manual=st.booleans(),
)
async def test_no_runtime_tick_rises_after_the_accepted_reading_budget_and_verdicts_dominate(
    readings: list[tuple[bool, int]], manual: bool
) -> None:
    rig = await occupied() if manual else await turning()
    if manual:
        target(rig, 1000)
        await rig.run(6)
    rig.feed(Bpm(70))
    last = rig.now
    for good, ticks in readings:
        rig.feed(
            Bpm(70) if good else None,
            quality=SignalQuality.GOOD if good else SignalQuality.NO_SIGNAL,
        )
        if good:
            last = rig.now
        for _ in range(ticks):
            previous = rig.runtime.applied_rpm
            snapshot = await rig.step(feed=False)
            if (
                rig.now - last > REAL_LIMITS.rise_reading_within
                or snapshot.safety_action is not SafetyAction.NONE
            ):
                assert rig.runtime.applied_rpm <= previous
