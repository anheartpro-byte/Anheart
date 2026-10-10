"""Tests for the control law.

What these tests are for: this is the module a person feels. A heart rate goes
in and a motor speed comes out, and every way that mapping can be wrong is
silent - a stale reading regulated on as if it were live, a setpoint that
crosses its slew limit, an integrator that has to unwind before it will slow
down, a demand that parks at the minimum running speed and never reaches zero.
Coverage alone does not exclude any of those. The properties below do.

Five guarantees carry the file, each written as a ``hypothesis`` property over
arbitrary input sequences rather than as an example:

1. the setpoint is always in ``{0} union [min_run_rpm, max_rpm]``;
2. between two successive *changes* of the setpoint, the change never exceeds
   ``slew * dt``;
3. no ``NaN`` or ``inf`` reaches any field of a decision, from any input -
   including an absurd heart rate, a missing one, and a corrupted clock;
4. the controller's whole state stays bounded under prolonged saturation, and
   a reversal of the error is answered on the very next decision;
5. with ``allow_increase=False`` the setpoint is monotonically non-increasing.

Plus a plant sweep: a first-order-plus-dead-time occupant over
``K in [0.05, 0.20] x tau in [20, 70] x theta in [4, 12]``, driven through the
real tracker and the real controller, asserting no overshoot past
``zone_high + 5`` and no sustained oscillation.

**On the clock, because the pattern is easy to get wrong.** Everything here
uses :class:`~src.clock.ManualClock`, and that is not a shortcut: nothing in
``src/training/hr_control.py`` reads a clock, so *every* guarantee it makes is
a function of the ``now`` it is handed. A control period of 5 s means "5 s of
the caller's monotonic time", and a manual clock measures exactly that. There
is no wall-clock property in this module to measure, so there is none faked.
Where a bound would have needed real elapsed time, it would have been measured
in real elapsed time.

**Two defects this file exists because of.** Both were found by probing the
module before the tests were written, and both are now pinned:

* the integral term is sub-rpm at these gains, and the first draft rounded it
  away every tick, so the setpoint never moved while the error was small and
  the machine sat still through an entire warmup. See the residue tests.
* the descent stalled one step short of zero - the setpoint reached
  ``min_run_rpm``, the domain forbids anything between that and zero, and the
  slew allowance of a single 5 Hz tick could not pay for the jump, so the
  machine **never stopped**. See ``test_a_stop_always_reaches_zero_from``.
"""

from __future__ import annotations

import ast
import itertools
import math
from collections import deque
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Final, assert_never, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.result import Err, Ok
from src.training.hr_control import (
    DEFAULT_HIGH_INSET,
    DEFAULT_KP,
    DEFAULT_LOW_INSET,
    DEFAULT_MAX_JUMP_BPM,
    DEFAULT_PERIOD,
    DEFAULT_TI,
    MAX_PLAUSIBLE_BPM,
    MIN_PLAUSIBLE_BPM,
    RUNNING_RESIDUE_BOUND,
    ControlInput,
    ControlPlan,
    ControlStep,
    Gains,
    HeartRateController,
    HeartRateRejection,
    HeartRateTracker,
    ImplausibleJump,
    ImplausibleRate,
    SpeedLimits,
    StaleSequence,
    TargetBand,
    TrackerLimits,
    TrackerReading,
    UnusableSample,
    Zone,
    is_regulating,
    stop_reason,
)
from src.training.types import ControlDecision, HeartRateSample, Phase, SignalQuality
from src.units import Bpm, Monotonic, MotorRpm, RpmPerSecond, Seconds

# =========================================================================
# The machine under test
# =========================================================================
#
# One plan, used by nearly every test, with the commissioning figures scaled to
# a plausible session: a 60 rpm minimum at the motor shaft, a 900 rpm ceiling,
# 450 rpm while warming up, 15 rpm/s of slew and 10 rpm of start hysteresis.
# floor(15 * 5) = 75 >= 60, so one control period buys enough allowance to
# leave zero - which ControlPlan checks, and which the tests below rely on.

ZONE_LOW: Final[Bpm] = Bpm(105)
ZONE_HIGH: Final[Bpm] = Bpm(125)
MIN_RUN: Final[MotorRpm] = MotorRpm(60)
MAX_RPM: Final[MotorRpm] = MotorRpm(900)
WARMUP_MAX: Final[MotorRpm] = MotorRpm(450)
SLEW: Final[RpmPerSecond] = RpmPerSecond(15.0)
HYSTERESIS: Final[MotorRpm] = MotorRpm(10)
WARMUP_SECONDS: Final[Seconds] = Seconds(300.0)
RESTING: Final[Bpm] = Bpm(65)

#: The no-correction band the plan below produces. Written out rather than
#: derived from Zone, so a change to the insets fails here by name.
BAND_LOW: Final[Bpm] = Bpm(106)
BAND_HIGH: Final[Bpm] = Bpm(122)

TICK: Final[Seconds] = Seconds(0.2)
"""The runtime loop's period: 5 Hz, as ``src/local_panel.py`` drives it."""


def speed_limits(**overrides: object) -> SpeedLimits:
    """The machine's rpm envelope, with named overrides for the edge cases."""
    values: dict[str, object] = {
        "min_run_rpm": MIN_RUN,
        "max_rpm": MAX_RPM,
        "warmup_max_rpm": WARMUP_MAX,
        "slew": SLEW,
        "start_hysteresis_rpm": HYSTERESIS,
    }
    values.update(overrides)
    return SpeedLimits(**values)  # type: ignore[arg-type]  # test-only keyword splat


def make_plan(
    *,
    zone: Zone | None = None,
    speed: SpeedLimits | None = None,
    warmup: Seconds = WARMUP_SECONDS,
    gains: Gains | None = None,
) -> ControlPlan:
    """The plan under test. Every field overridable, none of them guessed."""
    return ControlPlan(
        zone=Zone(low=ZONE_LOW, high=ZONE_HIGH) if zone is None else zone,
        speed=speed_limits() if speed is None else speed,
        warmup=warmup,
        gains=Gains() if gains is None else gains,
    )


def make_controller(**kwargs: object) -> HeartRateController:
    """A controller on the standard plan."""
    plan = kwargs.pop("plan", None)
    initial = kwargs.pop("initial_rpm", MotorRpm(0))
    assert not kwargs, kwargs
    return HeartRateController(
        make_plan() if plan is None else cast(ControlPlan, plan),
        initial_rpm=cast(MotorRpm, initial),
    )


def sample(
    bpm: int | None,
    seq: int,
    at: float,
    quality: SignalQuality = SignalQuality.GOOD,
) -> HeartRateSample:
    """One heart-rate sample, spelled compactly."""
    return HeartRateSample(
        bpm=None if bpm is None else Bpm(bpm),
        quality=quality,
        seq=seq,
        at=Monotonic(at),
    )


def observation(
    phase: Phase = Phase.HOLD,
    bpm: int | None = None,
    applied_rpm: int | None = 0,
    resting_bpm: int | None = None,
) -> ControlInput:
    """One tick's worth of knowledge, spelled compactly."""
    return ControlInput(
        phase=phase,
        bpm=None if bpm is None else Bpm(bpm),
        applied_rpm=None if applied_rpm is None else MotorRpm(applied_rpm),
        resting_bpm=None if resting_bpm is None else Bpm(resting_bpm),
    )


def drive(
    controller: HeartRateController,
    clock: ManualClock,
    duration: Seconds,
    *,
    phase: Phase = Phase.HOLD,
    bpm: int | None = None,
    resting_bpm: int | None = None,
    applied_follows: bool = True,
    frozen_applied: int | None = None,
    allow_increase: bool = True,
) -> list[ControlStep]:
    """Run the 5 Hz loop for ``duration``, with the drive obeying instantly.

    ``applied_follows`` models a drive that applies exactly what it was asked
    for by the next tick - the honest simplification for the unit tests, since
    the point being tested is the controller's own arithmetic. The plant sweep
    at the end of this file uses the commissioned ramp instead.
    """
    steps: list[ControlStep] = []
    applied: int | None = controller.demand
    for _ in range(round(duration / TICK)):
        if not applied_follows or frozen_applied is not None:
            applied = frozen_applied
        step = controller.update(
            Monotonic(clock.monotonic()),
            observation(phase=phase, bpm=bpm, applied_rpm=applied, resting_bpm=resting_bpm),
            allow_increase=allow_increase,
        )
        steps.append(step)
        if applied_follows and frozen_applied is None:
            applied = step.decision.desired_rpm
        clock.advance(TICK)
    return steps


# =========================================================================
# Configuration: a plan nobody can vouch for must not start a session
# =========================================================================


def test_the_standard_plan_is_accepted_and_keeps_its_numbers() -> None:
    """The happy path, and the one place the defaults are pinned."""
    plan = make_plan()
    assert plan.gains.kp == pytest.approx(DEFAULT_KP)
    assert plan.gains.ti == pytest.approx(DEFAULT_TI)
    assert plan.gains.period == pytest.approx(DEFAULT_PERIOD)
    assert plan.zone.low_inset == DEFAULT_LOW_INSET
    assert plan.zone.high_inset == DEFAULT_HIGH_INSET
    assert plan.speed.min_run_rpm == MIN_RUN


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_jump": Bpm(0)}, "max_jump must be at least 1"),
        ({"median_window": 0}, "median_window must be at least 1"),
        ({"rate_window": 2, "median_window": 3}, "smaller than median_window"),
        ({"reseed_after": 0}, "reseed_after must be at least 1"),
        ({"stale_after": Seconds(0.0)}, "stale_after must be positive"),
    ],
)
def test_tracker_limits_that_cannot_filter_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    """Each rejected limit names itself, because "bad config" at 2am is useless."""
    with pytest.raises(ValueError, match=message):
        TrackerLimits(**overrides)  # type: ignore[arg-type]  # test-only keyword splat


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"low": Bpm(10), "high": Bpm(120)}, "outside the plausible range"),
        ({"low": Bpm(100), "high": Bpm(400)}, "outside the plausible range"),
        ({"low": Bpm(120), "high": Bpm(120)}, "must be below zone high"),
        ({"low": Bpm(130), "high": Bpm(120)}, "must be below zone high"),
        ({"low": Bpm(100), "high": Bpm(120), "low_inset": Bpm(-1)}, "must not be negative"),
        ({"low": Bpm(100), "high": Bpm(120), "high_inset": Bpm(-1)}, "must not be negative"),
    ],
)
def test_an_impossible_zone_is_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Zone(**kwargs)  # type: ignore[arg-type]  # test-only keyword splat


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"min_run_rpm": MotorRpm(0)}, "min_run_rpm must be at least 1"),
        ({"max_rpm": MotorRpm(30)}, "is below min_run_rpm"),
        ({"warmup_max_rpm": MotorRpm(10)}, "must lie within"),
        ({"warmup_max_rpm": MotorRpm(2000)}, "must lie within"),
        ({"slew": RpmPerSecond(0.0)}, "must be finite and positive"),
        ({"slew": RpmPerSecond(float("inf"))}, "must be finite and positive"),
        ({"slew": RpmPerSecond(float("nan"))}, "must be finite and positive"),
        ({"start_hysteresis_rpm": MotorRpm(-1)}, "must not be negative"),
        (
            {
                "min_run_rpm": MotorRpm(890),
                "warmup_max_rpm": MotorRpm(895),
                "start_hysteresis_rpm": MotorRpm(50),
            },
            "could never be started",
        ),
    ],
)
def test_an_impossible_speed_envelope_is_refused(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        speed_limits(**overrides)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"kp": 0.0}, "kp must be finite and positive"),
        ({"kp": float("nan")}, "kp must be finite and positive"),
        ({"kp": float("inf")}, "kp must be finite and positive"),
        ({"ti": Seconds(0.0)}, "ti must be finite and positive"),
        ({"ti": Seconds(float("inf"))}, "ti must be finite and positive"),
        ({"period": Seconds(-1.0)}, "period must be finite and positive"),
        ({"period": Seconds(float("nan"))}, "period must be finite and positive"),
        ({"step_cap": Seconds(1.0)}, "at least the control period"),
        ({"step_cap": Seconds(float("inf"))}, "at least the control period"),
    ],
)
def test_gains_that_are_not_finite_and_positive_are_refused(
    overrides: dict[str, object], message: str
) -> None:
    """The guard the "no NaN can reach the setpoint" claim rests on.

    With the heart rate bounded to 25..240, the interval bounded by the step
    cap, and these gains finite, every product in the control law is finite by
    construction - so the module needs no ``isfinite`` guard on its own output.
    """
    with pytest.raises(ValueError, match=message):
        Gains(**overrides)  # type: ignore[arg-type]  # test-only keyword splat


@pytest.mark.parametrize("warmup", [0.0, -10.0, float("nan"), float("inf")])
def test_a_warmup_that_is_not_a_ramp_is_refused(warmup: float) -> None:
    with pytest.raises(ValueError, match="warmup must be finite and positive"):
        make_plan(warmup=Seconds(warmup))


def test_a_slew_too_slow_to_ever_leave_zero_is_refused() -> None:
    """A config bug that would present as "the motor never turns"."""
    with pytest.raises(ValueError, match="could never leave zero"):
        make_plan(speed=speed_limits(slew=RpmPerSecond(1.0)))


@pytest.mark.parametrize("initial", [-1, 1, MIN_RUN - 1, MAX_RPM + 1])
def test_a_standing_demand_outside_the_domain_is_refused(initial: int) -> None:
    """The controller's only state has to start inside the domain it must keep."""
    with pytest.raises(ValueError, match="outside the setpoint domain"):
        make_controller(initial_rpm=MotorRpm(initial))


@pytest.mark.parametrize("initial", [0, MIN_RUN, 300, MAX_RPM])
def test_a_standing_demand_inside_the_domain_is_accepted(initial: int) -> None:
    assert make_controller(initial_rpm=MotorRpm(initial)).demand == initial


def test_the_controller_exposes_the_plan_it_is_actually_enforcing() -> None:
    """Not a copy of it: the screen must show the limits in force, not the ones asked for."""
    plan = make_plan()
    controller = HeartRateController(plan)
    assert controller.plan is plan
    assert controller.plan.speed.max_rpm == MAX_RPM
    assert controller.plan.zone.target_band() == TargetBand(low=BAND_LOW, high=BAND_HIGH)


def test_the_plausible_heart_rate_range_is_the_one_documented() -> None:
    """Pinned, because both objects gate on it and the gate is what bounds the maths."""
    assert (MIN_PLAUSIBLE_BPM, MAX_PLAUSIBLE_BPM) == (25, 240)


# =========================================================================
# The tracker: the sequence trap first, because it is the silent one
# =========================================================================


def test_a_re_emitted_metrics_dict_is_rejected_and_does_not_refresh_the_age() -> None:
    """THE test this class exists for.

    ``src/signal_processing.py`` re-emits its previous metrics dict when
    extraction fails, with a **fresh timestamp and the old number**. If that
    counted, the tracker's age would keep resetting and the safety layer would
    never see the pipeline stop. So the rejection has to leave the age alone,
    not merely decline to change the value.
    """
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(80, seq=1, at=100.0)), Ok)
    assert tracker.age(Monotonic(101.0)) == pytest.approx(1.0)

    # the pipeline fails for six seconds and re-emits the same metrics, each
    # time with a perfectly current timestamp
    for at in (101.0, 102.0, 103.0, 104.0, 105.0, 106.0):
        rejected = tracker.observe(sample(80, seq=1, at=at))
        assert isinstance(rejected, Err)
        assert isinstance(rejected.error, StaleSequence)
        assert rejected.error.previous_seq == 1

    # the age still counts from the LAST REAL measurement, six seconds ago, so
    # the staleness horizon of four seconds has been crossed and the safety
    # layer can see the pipeline has stopped
    assert tracker.age(Monotonic(106.0)) == pytest.approx(6.0)
    assert tracker.is_fresh(Monotonic(106.0)) is False
    assert tracker.usable(Monotonic(106.0)) is None
    assert tracker.sample_count == 1


def test_a_sequence_counter_that_restarted_is_rejected() -> None:
    """Strictly greater, so a pipeline restart cannot pass old numbers as new."""
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(80, seq=7, at=0.0)), Ok)
    rejected = tracker.observe(sample(80, seq=0, at=1.0))
    assert isinstance(rejected, Err)
    assert isinstance(rejected.error, StaleSequence)
    assert rejected.error.seq == 0


def test_the_first_sample_of_a_session_is_new_evidence() -> None:
    tracker = HeartRateTracker()
    assert tracker.last_seq is None
    assert isinstance(tracker.observe(sample(80, seq=41, at=0.0)), Ok)
    assert tracker.last_seq == 41


@pytest.mark.parametrize(
    "quality",
    [SignalQuality.NO_SIGNAL, SignalQuality.MAINS_DOMINATED, SignalQuality.NOISY],
)
def test_only_good_quality_yields_a_usable_rate(quality: SignalQuality) -> None:
    """A number graded anything but GOOD is not a heart rate, whatever it says."""
    tracker = HeartRateTracker()
    rejected = tracker.observe(sample(80, seq=1, at=0.0, quality=quality))
    assert isinstance(rejected, Err)
    assert isinstance(rejected.error, UnusableSample)
    assert rejected.error.quality is quality
    assert tracker.filtered_bpm is None
    assert tracker.raw_bpm is None
    assert tracker.age(Monotonic(1.0)) is None


def test_a_good_grade_with_no_rate_is_still_unusable() -> None:
    """The pipeline should not produce this, but "should not" is not a type."""
    tracker = HeartRateTracker()
    rejected = tracker.observe(sample(None, seq=1, at=0.0))
    assert isinstance(rejected, Err)
    assert isinstance(rejected.error, UnusableSample)
    assert rejected.error.bpm is None


@pytest.mark.parametrize("bpm", [0, -5, 24, 241, 3000, 10**400])
def test_a_rate_no_heart_could_produce_is_rejected(bpm: int) -> None:
    """Including one no ``float`` can hold: the gate compares integers only."""
    tracker = HeartRateTracker()
    rejected = tracker.observe(sample(bpm, seq=1, at=0.0))
    assert isinstance(rejected, Err)
    assert isinstance(rejected.error, ImplausibleRate)
    assert rejected.error.bpm == bpm
    assert (rejected.error.low, rejected.error.high) == (MIN_PLAUSIBLE_BPM, MAX_PLAUSIBLE_BPM)


@pytest.mark.parametrize("bpm", [MIN_PLAUSIBLE_BPM, 80, MAX_PLAUSIBLE_BPM])
def test_the_plausible_boundaries_are_inclusive(bpm: int) -> None:
    assert isinstance(HeartRateTracker().observe(sample(bpm, seq=1, at=0.0)), Ok)


def test_a_rejected_sample_still_advances_the_sequence_watermark() -> None:
    """Evidence that was new but bad has still been seen, and cannot arrive twice."""
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(None, seq=5, at=0.0)), Err)
    assert tracker.last_seq == 5
    again = tracker.observe(sample(80, seq=5, at=1.0))
    assert isinstance(again, Err)
    assert isinstance(again.error, StaleSequence)


@pytest.mark.parametrize(
    ("second", "accepted"),
    [(105, True), (104, True), (106, False), (55, True), (54, False)],
)
def test_the_jump_gate_is_exactly_twenty_five_bpm(second: int, accepted: bool) -> None:
    """80 -> 105 is a 25 bpm step and passes; 80 -> 106 is 26 and does not."""
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(80, seq=1, at=0.0)), Ok)
    result = tracker.observe(sample(second, seq=2, at=1.0))
    assert isinstance(result, Ok) is accepted
    if isinstance(result, Err):
        assert isinstance(result.error, ImplausibleJump)
        assert result.error.previous_bpm == 80
        assert result.error.limit == DEFAULT_MAX_JUMP_BPM
        assert result.error.consecutive == 1


def test_a_single_outlier_is_rejected_and_the_series_continues() -> None:
    """One artefact does not reseed, and does not enter the median."""
    tracker = HeartRateTracker()
    for index, bpm in enumerate((80, 81, 82), start=1):
        assert isinstance(tracker.observe(sample(bpm, seq=index, at=float(index))), Ok)
    outlier = tracker.observe(sample(180, seq=4, at=4.0))
    assert isinstance(outlier, Err)
    assert tracker.filtered_bpm == 81
    back = tracker.observe(sample(83, seq=5, at=5.0))
    assert isinstance(back, Ok)
    assert back.value.reseeded is False
    assert tracker.filtered_bpm == 82


def test_three_confirmed_jumps_reseed_rather_than_going_blind_forever() -> None:
    """A real change plus a gap must not deadlock the gate.

    Rejecting forever is *detected* - the age grows and the safety layer trips
    on staleness - but a tracker that can never accept anything again is worse
    than one that accepts a change three independent refreshes agreed on. The
    reseed is reported so a session log shows it happened.
    """
    tracker = HeartRateTracker()
    for index, bpm in enumerate((80, 81, 82), start=1):
        assert isinstance(tracker.observe(sample(bpm, seq=index, at=float(index))), Ok)

    for offset, seq in enumerate((4, 5), start=1):
        rejected = tracker.observe(sample(140, seq=seq, at=float(seq)))
        assert isinstance(rejected, Err)
        assert isinstance(rejected.error, ImplausibleJump)
        assert rejected.error.consecutive == offset

    reseeded = tracker.observe(sample(140, seq=6, at=6.0))
    assert isinstance(reseeded, Ok)
    assert reseeded.value.reseeded is True
    # the history from before the step is gone: a median across it would report
    # a number that was never measured
    assert reseeded.value.window == 1
    assert tracker.sample_count == 1
    assert tracker.filtered_bpm == 140


def test_a_plausible_sample_clears_the_consecutive_jump_count() -> None:
    """Two artefacts separated by a good reading are not a confirmed change."""
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(80, seq=1, at=1.0)), Ok)
    assert isinstance(tracker.observe(sample(140, seq=2, at=2.0)), Err)
    assert isinstance(tracker.observe(sample(81, seq=3, at=3.0)), Ok)
    second = tracker.observe(sample(140, seq=4, at=4.0))
    assert isinstance(second, Err)
    assert isinstance(second.error, ImplausibleJump)
    assert second.error.consecutive == 1


def test_the_reported_rate_is_the_median_of_the_last_three() -> None:
    tracker = HeartRateTracker()
    readings = [
        tracker.observe(sample(bpm, seq=index, at=float(index)))
        for index, bpm in enumerate((70, 90, 80, 85), start=1)
    ]
    assert [r.value.bpm for r in readings if isinstance(r, Ok)] == [70, 70, 80, 85]
    assert [r.value.window for r in readings if isinstance(r, Ok)] == [1, 2, 3, 3]
    assert [r.value.raw_bpm for r in readings if isinstance(r, Ok)] == [70, 90, 80, 85]


def test_the_raw_reading_is_reported_beside_the_filtered_one() -> None:
    """Both, because they answer different questions, and they differ.

    The median is what the control law regulates on; the newest raw value is
    what a session log needs in order to show that the filter did something.
    Reporting only one of them would make an outlier invisible.
    """
    # a fresh tracker, so no narrowing from the empty case leaks into the
    # assertions below (the checkers would otherwise call them unreachable)
    assert HeartRateTracker().raw_bpm is None
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(70, seq=1, at=0.0)), Ok)
    assert isinstance(tracker.observe(sample(90, seq=2, at=1.0)), Ok)
    assert tracker.raw_bpm == 90, "the newest measurement"
    assert tracker.filtered_bpm == 70, "the filtered value the law acts on"


def test_the_history_is_bounded_by_the_rate_window() -> None:
    """A 45-minute session at 1 Hz must not grow a list of 2700 samples."""
    tracker = HeartRateTracker(TrackerLimits(rate_window=5))
    for index in range(40):
        assert isinstance(tracker.observe(sample(80, seq=index + 1, at=float(index))), Ok)
    assert tracker.sample_count == 5


def test_a_window_of_two_takes_the_lower_middle_value() -> None:
    """``median_low``, so the filtered number is always one that was measured."""
    tracker = HeartRateTracker()
    assert isinstance(tracker.observe(sample(70, seq=1, at=0.0)), Ok)
    second = tracker.observe(sample(90, seq=2, at=1.0))
    assert isinstance(second, Ok)
    assert second.value.bpm == 70


def test_freshness_is_the_callers_decision_and_never_optimistic() -> None:
    tracker = HeartRateTracker()
    assert tracker.is_fresh(Monotonic(0.0)) is False
    assert isinstance(tracker.observe(sample(80, seq=1, at=10.0)), Ok)
    assert tracker.age(Monotonic(13.9)) == pytest.approx(3.9)
    assert tracker.is_fresh(Monotonic(14.0)) is True
    assert tracker.usable(Monotonic(14.0)) == 80
    assert tracker.is_fresh(Monotonic(14.01)) is False
    assert tracker.usable(Monotonic(14.01)) is None


def test_the_limits_are_readable_and_default() -> None:
    assert HeartRateTracker().limits == TrackerLimits()
    strict = TrackerLimits(max_jump=Bpm(5))
    assert HeartRateTracker(strict).limits is strict


def test_the_rate_of_change_is_a_slope_over_the_retained_window() -> None:
    """Ten readings rising 1 bpm/s must read as 60 bpm/min, not as noise."""
    tracker = HeartRateTracker()
    for index in range(10):
        assert isinstance(tracker.observe(sample(80 + index, seq=index + 1, at=float(index))), Ok)
    rate = tracker.rate
    assert rate is not None
    assert rate == pytest.approx(60.0)


def test_the_recent_rate_sees_a_turn_the_whole_window_still_hides() -> None:
    """The vasovagal gate's trend: the last five readings only.

    Seven flat readings then three falling 1.5 bpm/s (the scripted collapse):
    the ten-point slope has barely moved, the five-point one already reads the
    fall - which is why the runtime's gate uses it.
    """
    tracker = HeartRateTracker()
    values = [145] * 7 + [144, 142, 141]
    for index, bpm in enumerate(values):
        assert isinstance(tracker.observe(sample(bpm, seq=index + 1, at=float(index))), Ok)
    whole = tracker.rate
    recent = tracker.recent_rate(5)
    assert whole is not None
    assert recent is not None
    assert recent < -40.0 < whole


def test_the_recent_rate_is_unknown_without_enough_readings() -> None:
    tracker = HeartRateTracker()
    for index in range(4):
        assert isinstance(tracker.observe(sample(80, seq=index + 1, at=float(index))), Ok)
    assert tracker.recent_rate(5) is None
    assert tracker.recent_rate(1) is None, "one point has no slope, whatever is retained"
    assert tracker.recent_rate(4) == pytest.approx(0.0)


def test_a_falling_rate_reads_negative() -> None:
    """The vasovagal direction. The supervisor bounds this number, so its sign matters."""
    tracker = HeartRateTracker()
    for index in range(6):
        assert isinstance(
            tracker.observe(sample(100 - 2 * index, seq=index + 1, at=float(index))), Ok
        )
    rate = tracker.rate
    assert rate is not None
    assert rate == pytest.approx(-120.0)


def test_an_unknown_rate_of_change_is_none_and_never_zero() -> None:
    """Three ways it is unknown; none of them may read as "not changing"."""
    empty = HeartRateTracker()
    assert empty.rate is None
    assert isinstance(empty.observe(sample(80, seq=1, at=0.0)), Ok)
    assert empty.rate is None, "one point has no slope"

    # every reading stamped at the same instant: values but no time base
    same_instant = HeartRateTracker()
    for index in range(4):
        assert isinstance(same_instant.observe(sample(80 + index, seq=index + 1, at=5.0)), Ok)
    assert same_instant.rate is None

    # timestamps so large the fit cannot produce a finite answer
    absurd = HeartRateTracker()
    assert isinstance(absurd.observe(sample(80, seq=1, at=0.0)), Ok)
    assert isinstance(absurd.observe(sample(81, seq=2, at=1e308)), Ok)
    assert isinstance(absurd.observe(sample(82, seq=3, at=-1e308)), Ok)
    assert absurd.rate is None, "an infinite window must not read as a zero slope"


def test_every_rejection_is_handled_and_the_union_is_closed() -> None:
    """The nested-match discipline, applied to this module's own error union.

    ``assert_never`` here is what a new rejection variant collides with: adding
    one to ``HeartRateRejection`` fails this match at check time, naming the
    variant that was forgotten, instead of being silently ignored by whichever
    consumer was written first.
    """
    rejections: tuple[HeartRateRejection, ...] = (
        StaleSequence(seq=1, previous_seq=1),
        UnusableSample(quality=SignalQuality.NOISY, bpm=None),
        ImplausibleRate(bpm=Bpm(3000), low=MIN_PLAUSIBLE_BPM, high=MAX_PLAUSIBLE_BPM),
        ImplausibleJump(
            bpm=Bpm(140), previous_bpm=Bpm(80), limit=DEFAULT_MAX_JUMP_BPM, consecutive=1
        ),
    )
    seen: list[str] = []
    for rejection in rejections:
        match rejection:
            case StaleSequence():
                seen.append("stale")
            case UnusableSample():
                seen.append("unusable")
            case ImplausibleRate():
                seen.append("implausible")
            case ImplausibleJump():
                seen.append("jump")
            case _ as unreachable:
                assert_never(unreachable)
    assert seen == ["stale", "unusable", "implausible", "jump"]


# ---- tracker properties -------------------------------------------------


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=-500, max_value=100_000),
            st.integers(min_value=-5, max_value=40),
            st.sampled_from(list(SignalQuality)),
        ),
        max_size=60,
    )
)
def test_a_repeated_sequence_number_is_never_accepted(
    stream: list[tuple[int, int, SignalQuality]],
) -> None:
    """Over any stream at all: the watermark only ever moves forward.

    The property that makes the whole gate meaningful, stated without reference
    to what the pipeline happens to emit.
    """
    tracker = HeartRateTracker()
    watermark: int | None = None
    for index, (bpm, seq, quality) in enumerate(stream):
        result = tracker.observe(
            HeartRateSample(bpm=Bpm(bpm), quality=quality, seq=seq, at=Monotonic(float(index)))
        )
        if watermark is not None and seq <= watermark:
            assert isinstance(result, Err), "a repeated sequence number was accepted"
            assert isinstance(result.error, StaleSequence)
        else:
            watermark = seq
        assert tracker.last_seq == watermark


@given(
    st.lists(
        st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM),
        min_size=1,
        max_size=50,
    )
)
def test_the_filtered_rate_is_always_a_rate_that_was_measured(rates: list[int]) -> None:
    """``median_low`` never invents a number: no averaged half-beats."""
    tracker = HeartRateTracker()
    accepted: list[int] = []
    for index, bpm in enumerate(rates, start=1):
        result = tracker.observe(sample(bpm, seq=index, at=float(index)))
        if isinstance(result, Ok):
            accepted.append(bpm)
            assert result.value.bpm in accepted
            assert result.value.raw_bpm == bpm
    assert tracker.filtered_bpm is None or tracker.filtered_bpm in accepted


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM),
            st.floats(min_value=0.0, max_value=1e6, allow_nan=False),
        ),
        max_size=40,
    )
)
def test_the_rate_of_change_is_never_nan_or_infinite(
    stream: list[tuple[int, float]],
) -> None:
    """The safety supervisor trips on this number; a NaN would compare False."""
    tracker = HeartRateTracker()
    for index, (bpm, at) in enumerate(stream, start=1):
        tracker.observe(sample(bpm, seq=index, at=at))
        rate = tracker.rate
        assert rate is None or math.isfinite(rate)


# =========================================================================
# The deadband: the asymmetry is the policy
# =========================================================================


def test_the_no_correction_band_is_inset_one_bpm_low_and_three_high() -> None:
    """Drifting toward the floor is tolerated; drifting toward the ceiling is not.

    The whole asymmetry, as a number: correction against a rising heart rate
    starts three bpm before the zone ceiling, while a heart rate sagging toward
    the floor is tolerated to within one.
    """
    band = Zone(low=ZONE_LOW, high=ZONE_HIGH).target_band()
    assert (band.low, band.high) == (BAND_LOW, BAND_HIGH)
    assert ZONE_LOW + DEFAULT_LOW_INSET == BAND_LOW
    assert ZONE_HIGH - DEFAULT_HIGH_INSET == BAND_HIGH
    # stated as the guarantee rather than as arithmetic: the machine gives
    # itself more room above than below
    assert DEFAULT_HIGH_INSET > DEFAULT_LOW_INSET


@pytest.mark.parametrize(
    ("bpm", "inside", "error"),
    [
        (100, False, 6.0),
        (105, False, 1.0),
        (106, True, 0.0),
        (114, True, 0.0),
        (122, True, 0.0),
        (123, False, -1.0),
        (130, False, -8.0),
    ],
)
def test_the_control_error_is_zero_inside_the_band_and_edge_referenced_outside(
    bpm: int, inside: bool, error: float
) -> None:
    """Continuous through both edges, and positive means "may speed up".

    Edge-referenced rather than centre-referenced on purpose: a centre-referenced
    error with a deadband jumps by the half-width at each edge, and in velocity
    form a jump in the error is a jump in the speed.
    """
    band = Zone(low=ZONE_LOW, high=ZONE_HIGH).target_band()
    assert band.contains(Bpm(bpm)) is inside
    assert band.control_error(Bpm(bpm)) == pytest.approx(error)


def test_the_band_centre_is_the_reported_target() -> None:
    band = Zone(low=ZONE_LOW, high=ZONE_HIGH).target_band()
    assert band.centre == 114
    assert TargetBand(low=Bpm(110), high=Bpm(110)).centre == 110
    assert TargetBand(low=Bpm(110), high=Bpm(111)).centre == 110


def test_insets_wider_than_the_zone_collapse_to_the_low_edge() -> None:
    """The conservative collapse: correcting downward starts at the low edge."""
    band = Zone(low=Bpm(100), high=Bpm(104), low_inset=Bpm(3), high_inset=Bpm(9)).target_band()
    assert (band.low, band.high) == (103, 103)
    assert band.control_error(Bpm(104)) == pytest.approx(-1.0)


# =========================================================================
# The controller: when it decides at all
# =========================================================================


def test_the_first_call_only_seeds_and_cannot_move_the_machine() -> None:
    """No elapsed time means no slew allowance, so no movement. Nothing special-cased.

    The alternative - inventing a nominal interval for the first tick - would
    make the slew guarantee conditional on a constant, which is exactly the kind
    of exception this file is arranged to avoid.
    """
    controller = make_controller()
    step = controller.update(Monotonic(0.0), observation(bpm=60, applied_rpm=0))
    assert step.held is True
    assert step.decision.desired_rpm == 0
    assert step.decision.target_bpm is None
    assert step.decision.error_bpm == pytest.approx(0.0)
    assert step.decision.in_deadband is True
    assert step.dt == pytest.approx(0.0), "a held tick integrated nothing"
    # in a regulating phase the control-period gate is what catches the first
    # call, because a zero interval is also less than one period
    assert "control period has not elapsed" in step.decision.reason

    # in a phase with no target there is no period to wait for, so the
    # zero-interval gate is the one that answers
    resting = make_controller(initial_rpm=MotorRpm(300))
    first = resting.update(Monotonic(0.0), observation(phase=Phase.COOLDOWN))
    assert first.held is True
    assert first.decision.desired_rpm == 300
    assert "no time has passed" in first.decision.reason


def test_ticks_inside_the_control_period_re_emit_the_standing_demand() -> None:
    """Five Hz in, one decision every five seconds out.

    Stated as the two properties rather than as a list of tick indexes: no two
    decisions closer together than the control period (deciding faster than the
    physiology amplifies noise into speed changes), and none further apart than
    two (a controller that quietly stopped deciding would look identical to one
    that had nothing to correct).
    """
    controller = make_controller()
    clock = ManualClock()
    stamps: list[tuple[float, ControlStep]] = []
    for _ in range(round(30.0 / TICK)):
        now = clock.monotonic()
        stamps.append(
            (
                now,
                controller.update(
                    Monotonic(now), observation(bpm=60, applied_rpm=controller.demand)
                ),
            )
        )
        clock.advance(TICK)

    decided_at = [at for at, step in stamps if not step.held]
    held = [step for _, step in stamps if step.held]
    assert len(decided_at) >= 5, "six decisions fit in thirty seconds"
    for earlier, later in itertools.pairwise(decided_at):
        gap = later - earlier
        assert gap >= DEFAULT_PERIOD - 1e-9, f"decided again after only {gap} s"
        assert gap <= 2 * DEFAULT_PERIOD, f"stopped deciding for {gap} s"

    assert len(held) == len(stamps) - len(decided_at)
    assert all("control period has not elapsed" in step.decision.reason for step in held[1:])
    assert all(step.increment_rpm == pytest.approx(0.0) for step in held)
    assert all(step.dt == pytest.approx(0.0) for step in held)
    assert all(step.slew_limited is False for step in held)


def test_a_phase_with_no_target_is_not_rate_limited_to_the_control_period() -> None:
    """Descending to zero is shaped only by the slew limit, so the ramp is smooth.

    A cooldown that stepped once every five seconds would be felt as a series of
    surges; there is nothing to regulate in these phases, so there is nothing to
    wait for.
    """
    controller = make_controller(initial_rpm=MotorRpm(900))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(2.0), phase=Phase.COOLDOWN)
    assert steps[0].held is True, "the first tick still has no elapsed time"
    assert all(step.held is False for step in steps[1:])
    assert steps[-1].decision.desired_rpm < 900


@pytest.mark.parametrize("phase", list(Phase))
@pytest.mark.parametrize("now", [float("nan"), -5.0, 0.0])
def test_a_clock_that_makes_no_sense_holds_the_setpoint(phase: Phase, now: float) -> None:
    """NaN, backwards and standing still all land in the holding branch.

    The gates are written ``not (x >= y)`` rather than ``x < y`` precisely for
    the NaN case: with ``<``, a NaN interval compares False and falls THROUGH
    into the arithmetic, which is the difference between a held setpoint and an
    undefined one.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(Monotonic(100.0), observation(phase=phase, bpm=110, applied_rpm=300))
    step = controller.update(
        Monotonic(100.0 + now if math.isfinite(now) else now),
        observation(phase=phase, bpm=110, applied_rpm=300),
    )
    assert step.held is True
    assert step.decision.desired_rpm == 300


def test_too_little_elapsed_time_to_move_a_whole_rpm_waits_instead_of_rounding_away() -> None:
    """The exact-rate property: a fractional allowance accumulates, it is not lost.

    At 2 rpm/s a 5 Hz tick earns 0.4 rpm, which ``floor`` would discard every
    time. Leaving the interval unspent turns the limiter into an honest 2 rpm/s
    instead of a dead one.
    """
    plan = make_plan(
        speed=speed_limits(min_run_rpm=MotorRpm(1), slew=RpmPerSecond(2.0)),
        gains=Gains(period=Seconds(0.5), step_cap=Seconds(10.0)),
    )
    controller = HeartRateController(plan, initial_rpm=MotorRpm(400))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(5.2), phase=Phase.COOLDOWN)
    assert any("to change the setpoint by a whole rpm" in s.decision.reason for s in steps)
    descent = 400 - controller.demand
    assert descent > 0, "a fractional allowance must accumulate, not be rounded to nothing"
    assert descent <= 2.0 * 5.0, "and it must still not exceed the slew limit"
    # pinned: one rpm every 0.6 s, the first whole rpm the 2 rpm/s limit earns
    assert controller.demand == 392


# =========================================================================
# The controller: what it decides
# =========================================================================


def test_a_heart_rate_below_the_band_speeds_the_machine_up() -> None:
    """And the sign convention that says so: positive error means below target."""
    controller = make_controller()
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(90.0), bpm=90)
    decision = next(step for step in steps if not step.held).decision
    assert decision.error_bpm > 0.0, "114 - 90 is positive: the law may speed up"
    assert decision.in_deadband is False
    assert decision.target_bpm == 114
    assert "correcting 90 bpm toward the band 106-122" in decision.reason
    assert controller.demand > 0


def test_a_heart_rate_above_the_band_slows_the_machine_down() -> None:
    controller = make_controller(initial_rpm=MotorRpm(600))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(60.0), bpm=140)
    decision = next(step for step in steps if not step.held).decision
    assert decision.error_bpm < 0.0
    assert decision.in_deadband is False
    assert controller.demand < 600


def test_inside_the_band_the_setpoint_is_held_and_the_raw_error_is_still_reported() -> None:
    """``in_deadband`` is what says a correction was withheld, not ``error_bpm``.

    ``ControlDecision.error_bpm`` is documented as ``target_bpm - measured_bpm``
    and ``in_deadband`` as "not derivable from error_bpm by a consumer". So the
    error reported is the RAW error to the reported target, and the deadband is
    a separate fact about whether it was acted on. A session log has to be able
    to tell "held still deliberately" from "held still by accident".
    """
    controller = make_controller(initial_rpm=MotorRpm(400))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(120.0), bpm=110)
    decisions = [step.decision for step in steps if not step.held]
    assert decisions
    for decision in decisions:
        assert decision.in_deadband is True
        assert decision.error_bpm == pytest.approx(4.0), "114 - 110, not squashed to zero"
        assert "holding inside the target band 106-122 bpm at 110 bpm" in decision.reason
    assert controller.demand == 400


@pytest.mark.parametrize("bpm", [None, 10, 300, 10**400])
def test_no_usable_heart_rate_holds_the_setpoint_rather_than_guessing(bpm: int | None) -> None:
    """Missing and absurd are handled the same way, and neither invents a number."""
    controller = make_controller(initial_rpm=MotorRpm(300))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(30.0), bpm=bpm)
    decisions = [step for step in steps if not step.held]
    assert decisions
    for step in decisions:
        assert step.decision.desired_rpm == 300
        assert step.decision.target_bpm is None
        assert step.increment_rpm == pytest.approx(0.0)
        assert "no heart rate may be acted on" in step.decision.reason
    assert controller.demand == 300


def test_an_unknown_applied_speed_holds_the_setpoint() -> None:
    """A demand added to an unknown base is how a machine ends up somewhere nobody predicted.

    ``None`` here is the honest state after a comms loss or before the first
    status read - and ``DriveState.COMM_LOST`` is not ``NOT_READY``: "state
    unknown" is not "motor stopped". So the controller declines to move.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    clock = ManualClock()
    decisions = [
        step
        for step in drive(
            controller, clock, Seconds(30.0), bpm=90, applied_follows=False, frozen_applied=None
        )
        if not step.held
    ]
    assert decisions, "the control period must still have elapsed"
    for step in decisions:
        assert step.decision.desired_rpm == 300
        assert step.increment_rpm == pytest.approx(0.0)
        assert step.decision.target_bpm is None
        assert "the speed the drive is running at is unknown" in step.decision.reason
    assert controller.demand == 300


@pytest.mark.parametrize("phase", [Phase.BASELINE, Phase.COOLDOWN, Phase.RECOVERY, Phase.DONE])
def test_a_phase_with_no_target_demands_zero_and_says_why(phase: Phase) -> None:
    controller = make_controller(initial_rpm=MotorRpm(300))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(1.0), phase=phase, bpm=110)
    decision = steps[-1].decision
    assert decision.target_bpm is None
    assert decision.in_deadband is True
    assert decision.error_bpm == pytest.approx(0.0)
    assert decision.reason == stop_reason(phase)
    assert decision.desired_rpm < 300


@pytest.mark.parametrize(
    ("phase", "regulates"),
    [
        (Phase.BASELINE, False),
        (Phase.WARMUP, True),
        (Phase.HOLD, True),
        (Phase.COOLDOWN, False),
        (Phase.RECOVERY, False),
        (Phase.DONE, False),
    ],
)
def test_exactly_two_phases_regulate(phase: Phase, regulates: bool) -> None:
    """Pinned member by member: a new phase must be classified deliberately."""
    assert is_regulating(phase) is regulates


@pytest.mark.parametrize("phase", list(Phase))
def test_every_phase_has_a_stop_sentence(phase: Phase) -> None:
    """Including the two that regulate: the phase machine may hand any phase here."""
    assert stop_reason(phase).startswith("demanding zero")


@pytest.mark.parametrize("helper", [is_regulating, stop_reason])
def test_a_value_that_is_not_a_phase_fails_loudly(
    helper: Callable[[Phase], object],
) -> None:
    """What ``assert_never`` does at runtime, as opposed to at check time.

    Both matches are statically exhaustive - mypy and basedpyright both refuse a
    missing member - so this branch cannot be reached by any well-typed caller.
    The test exists for two reasons: the coverage claim on this module has to be
    honest rather than waived with a pragma, and a corrupted phase must fail
    loudly instead of being silently classified as "does not regulate", which
    would be a phase that quietly demands zero forever.
    """
    with pytest.raises(AssertionError):
        helper(cast(Phase, "not-a-phase"))


def test_a_phase_that_is_not_a_phase_fails_loudly_in_the_dispatch() -> None:
    """The same guard on the controller's own phase dispatch.

    Reached directly because ``update`` classifies the phase first and would
    raise there; this asserts the dispatch itself is exhaustive at runtime too.
    """
    controller = make_controller()
    with pytest.raises(AssertionError):
        controller._demand(  # pyright: ignore[reportPrivateUsage] - no public route
            Monotonic(0.0),
            observation(phase=cast(Phase, "not-a-phase"), bpm=110, applied_rpm=0),
            Seconds(5.0),
            MAX_RPM,
        )


# =========================================================================
# The envelope: clamps, slew, and the hole at the bottom of the domain
# =========================================================================


@pytest.mark.parametrize("initial", [MIN_RUN, MIN_RUN + 1, 61, 70, 200, 900])
def test_a_stop_always_reaches_zero_from(initial: int) -> None:
    """THE regression test. The first draft of this module never stopped.

    The setpoint domain has nothing between zero and ``min_run_rpm``, so the
    last step of a stop is a jump of the whole minimum speed, and a single 5 Hz
    tick's slew allowance (three rpm here) cannot pay for it. The first draft
    therefore descended to ``min_run_rpm``, proposed zero on every subsequent
    tick, was refused for want of allowance on every subsequent tick, and left
    the machine turning forever - with the telemetry showing a controller
    dutifully demanding zero.

    The fix is that a tick which cannot pay leaves its interval unspent, so the
    allowance grows until the jump is affordable. This asserts the outcome from
    every starting speed, including exactly ``min_run_rpm``, which is the value
    the deadlock sat on.
    """
    controller = make_controller(initial_rpm=MotorRpm(initial))
    clock = ManualClock()
    drive(controller, clock, Seconds(180.0), phase=Phase.COOLDOWN)
    assert controller.demand == 0


def test_the_stop_from_the_minimum_speed_still_respects_the_slew_rate() -> None:
    """It waits for the allowance rather than taking the jump early.

    ``min_run_rpm / slew`` is 4 s, so the jump may not happen before then - a
    60 rpm step inside one 0.2 s tick would be 300 rpm/s, twenty times the limit.
    """
    controller = make_controller(initial_rpm=MIN_RUN)
    clock = ManualClock()
    stopped_at: float | None = None
    for _ in range(round(20.0 / TICK)):
        now = clock.monotonic()
        step = controller.update(
            Monotonic(now), observation(phase=Phase.COOLDOWN, applied_rpm=controller.demand)
        )
        if stopped_at is None and step.decision.desired_rpm == 0:
            stopped_at = now
        clock.advance(TICK)
    assert stopped_at is not None
    assert stopped_at >= MIN_RUN / SLEW, "the machine stopped faster than the slew limit allows"


def test_leaving_zero_needs_the_start_threshold_and_cannot_chatter() -> None:
    """Hysteresis at the bottom: start above min_run + hysteresis, stop below min_run.

    Driven by a heart rate one bpm under the band, which asks for 0.375 rpm per
    decision - a demand that rises far too slowly to clear the threshold for a
    long while. What must not happen is that it is discarded each tick, because
    then it could never clear the threshold at all.
    """
    controller = make_controller()
    clock = ManualClock()
    assert MIN_RUN + HYSTERESIS == 70

    steps = drive(controller, clock, Seconds(120.0), bpm=105)
    assert all(step.decision.desired_rpm == 0 for step in steps), (
        "a demand under the start threshold must not start the machine"
    )
    assert controller.residue > 0.0, "the unspent demand is carried, not discarded"

    # Keep going until it starts, and catch the FIRST non-zero setpoint. It has
    # to be the start threshold and not the minimum running speed: a machine
    # that left zero as soon as the demand reached 60 rpm would stop again on
    # the next wobble below 60, which is the chatter the hysteresis exists to
    # prevent.
    first_non_zero: int | None = None
    for _ in range(round(1200.0 / TICK)):
        step = controller.update(
            Monotonic(clock.monotonic()), observation(bpm=105, applied_rpm=controller.demand)
        )
        if first_non_zero is None and step.decision.desired_rpm != 0:
            first_non_zero = step.decision.desired_rpm
        clock.advance(TICK)
    assert first_non_zero is not None, "the demand must eventually clear the threshold"
    assert first_non_zero >= MIN_RUN + HYSTERESIS, (
        f"left zero at {first_non_zero} rpm, below the {MIN_RUN + HYSTERESIS} rpm start threshold"
    )


@given(
    st.lists(
        st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM),
        min_size=1,
        max_size=40,
    ),
    st.sampled_from([0, MIN_RUN, 300, 900]),
)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_setpoint_never_lands_in_the_forbidden_gap(rates: list[int], initial: int) -> None:
    """PROPERTY 1: the output is always in ``{0} union [min_run, max_rpm]``.

    Over any sequence of heart rates, from any legal starting speed, with the
    drive sometimes lagging and the reading sometimes missing.
    """
    controller = make_controller(initial_rpm=MotorRpm(initial))
    clock = ManualClock()
    applied = initial
    for index, bpm in enumerate(rates):
        for phase in (Phase.WARMUP, Phase.HOLD, Phase.COOLDOWN):
            step = controller.update(
                Monotonic(clock.monotonic()),
                observation(
                    phase=phase,
                    bpm=None if index % 7 == 0 else bpm,
                    applied_rpm=None if index % 11 == 0 else applied,
                    resting_bpm=RESTING,
                ),
            )
            rpm = step.decision.desired_rpm
            assert rpm == 0 or MIN_RUN <= rpm <= MAX_RPM, f"{rpm} is in the forbidden gap"
            assert controller.demand == rpm
            applied = rpm if index % 5 else applied
            clock.advance(Seconds(6.0))


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM),
            st.floats(min_value=0.05, max_value=400.0, allow_nan=False),
            st.sampled_from(list(Phase)),
        ),
        min_size=2,
        max_size=60,
    ),
    st.sampled_from([0, MIN_RUN, 300, 900]),
)
@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_setpoint_never_moves_faster_than_the_slew_limit(
    stream: list[tuple[int, float, Phase]], initial: int
) -> None:
    """PROPERTY 2: between two successive CHANGES, ``|delta| <= slew * dt``. Ever.

    Stated between changes rather than between calls, because that is the
    physical claim a rate limiter makes: a tick that holds the setpoint still
    changes nothing, and the interval a change is measured over is the interval
    since the setpoint last moved. Measuring between calls instead would make
    the bound trivially true on every held tick and say nothing about the ones
    that move.
    """
    controller = make_controller(initial_rpm=MotorRpm(initial))
    clock = ManualClock()
    applied = initial
    last_change_at = clock.monotonic()
    last_value = initial
    for bpm, gap, phase in stream:
        step = controller.update(
            Monotonic(clock.monotonic()),
            observation(phase=phase, bpm=bpm, applied_rpm=applied, resting_bpm=RESTING),
        )
        rpm = step.decision.desired_rpm
        if rpm != last_value:
            span = clock.monotonic() - last_change_at
            assert abs(rpm - last_value) <= SLEW * span + 1e-9, (
                f"{last_value} -> {rpm} in {span} s exceeds {SLEW} rpm/s"
            )
            last_change_at = clock.monotonic()
            last_value = rpm
        applied = rpm
        clock.advance(Seconds(gap))


def test_the_ceiling_is_the_one_the_phase_carries() -> None:
    """Full speed in HOLD, the warmup ceiling in WARMUP, and never past either."""
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(2000.0), phase=Phase.WARMUP, bpm=60, resting_bpm=RESTING)
    assert controller.demand == WARMUP_MAX, "a persistently low reading must not reach full speed"

    drive(controller, clock, Seconds(3000.0), phase=Phase.HOLD, bpm=60)
    assert controller.demand == MAX_RPM


def test_the_slew_limiter_reports_when_it_shortened_a_step() -> None:
    """A step change in the heart rate asks for more than one period can pay for.

    The proportional part of a velocity-form increment is ``Kp * (e - e_prev)``,
    so an 81 bpm jump in the error asks for 243 rpm where the allowance is 75.
    That is precisely the case the limiter exists for.
    """
    controller = make_controller(initial_rpm=MotorRpm(400))
    clock = ManualClock()
    drive(controller, clock, Seconds(11.0), bpm=114)
    step = controller.update(
        Monotonic(clock.monotonic() + 5.0), observation(bpm=25, applied_rpm=controller.demand)
    )
    assert step.held is False
    assert step.dt >= DEFAULT_PERIOD
    allowance = math.floor(SLEW * step.dt)
    assert step.increment_rpm > allowance, "the increment asked for more than one step"
    assert step.slew_limited is True
    assert step.decision.desired_rpm == 400 + allowance, "and got exactly one step"
    assert math.isfinite(step.increment_rpm)


def test_the_ceiling_flag_reports_saturation() -> None:
    controller = make_controller(initial_rpm=MotorRpm(880))
    clock = ManualClock()
    steps = drive(controller, clock, Seconds(30.0), bpm=60)
    decisions = [step for step in steps if not step.held]
    assert controller.demand == MAX_RPM
    assert decisions[-1].at_ceiling is True
    assert decisions[0].at_ceiling is False


def test_a_stalled_loop_buys_neither_windup_nor_a_lurch() -> None:
    """The step cap bounds both the integral interval and the slew allowance.

    Four minutes of interval would multiply the increment by fifty and permit a
    step of 3600 rpm; capped at two nominal periods it permits 150, which the
    slew limit then applies honestly over a ten-second interval.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(Monotonic(0.0), observation(bpm=60, applied_rpm=300))
    step = controller.update(Monotonic(240.0), observation(bpm=60, applied_rpm=300))
    assert step.held is False
    assert step.dt == pytest.approx(10.0), "the interval is capped at two periods"
    assert step.decision.desired_rpm - 300 <= math.floor(SLEW * 10.0)


# =========================================================================
# Anti-windup, which is the whole reason the base is the APPLIED rpm
# =========================================================================


def test_the_increment_is_added_to_what_the_drive_reports_not_to_our_last_output() -> None:
    """The claim, as a single observable field."""
    controller = make_controller(initial_rpm=MotorRpm(500))
    controller.update(Monotonic(0.0), observation(bpm=90, applied_rpm=500))
    step = controller.update(Monotonic(5.0), observation(bpm=90, applied_rpm=200))
    assert step.base_rpm == 200, "the base is the read-back, not the standing demand"
    assert step.decision.desired_rpm < 500


def test_resumption_after_a_safety_freeze_is_bumpless_with_no_special_case() -> None:
    """Two minutes frozen at 200 rpm, and the demand comes back at 200, not at 700.

    A positional PI would have accumulated the whole freeze into its integrator
    and released it as a surge the moment the freeze lifted. Nothing in this
    module knows what a FREEZE is; the property falls out of basing each
    increment on the applied speed.
    """
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(600.0), bpm=90)
    free_running = controller.demand
    assert free_running > 500

    drive(controller, clock, Seconds(120.0), bpm=90, frozen_applied=200)
    assert controller.demand == pytest.approx(206, abs=25), (
        f"the demand should resume from the frozen 200 rpm, not from {free_running}"
    )


def test_prolonged_saturation_leaves_nothing_to_unwind() -> None:
    """PROPERTY 4: the state stays bounded, and a reversal is answered at once.

    Twenty minutes pinned at the ceiling by a heart rate that will not rise.
    There is no accumulator to inspect, which is the point - so the assertion is
    on the two values that ARE the state: the standing demand and the carry.
    """
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(1200.0), bpm=60)
    assert controller.demand == MAX_RPM
    assert controller.residue == pytest.approx(0.0), (
        "nothing the ceiling removed may be carried forward"
    )

    # the heart rate finally answers, hard. The very next decision must descend.
    before = controller.demand
    first = None
    for _ in range(round(10.0 / TICK)):
        step = controller.update(
            Monotonic(clock.monotonic()),
            observation(bpm=200, applied_rpm=controller.demand),
        )
        if first is None and not step.held:
            first = step
        clock.advance(TICK)
    assert first is not None
    assert first.decision.desired_rpm < before, (
        "the first decision after the reversal must slow down"
    )


@given(st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM))
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_the_carry_is_bounded_however_long_the_session_runs(bpm: int) -> None:
    """The carry is a carry, not a shadow integrator."""
    controller = make_controller()
    clock = ManualClock()
    bound = float(MIN_RUN + HYSTERESIS)
    for _ in range(400):
        controller.update(
            Monotonic(clock.monotonic()), observation(bpm=bpm, applied_rpm=controller.demand)
        )
        assert abs(controller.residue) <= bound
        if controller.demand != 0:
            assert abs(controller.residue) <= RUNNING_RESIDUE_BOUND
        clock.advance(Seconds(5.0))


def test_an_absolute_demand_never_carries_an_increment() -> None:
    """Adding a stale carry to "hold here" or "demand zero" would move a still setpoint.

    Run from a speed the cooldown actually has to descend from, so the demand
    and the emitted setpoint differ by hundreds of rpm: that gap is exactly what
    a carry computed from an absolute proposal would pick up and hand to the
    next tick.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    clock = ManualClock()
    drive(controller, clock, Seconds(60.0), bpm=105)
    assert controller.residue > 0.0, "a regulating tick carries its rounding remainder"

    drive(controller, clock, Seconds(2.0), phase=Phase.COOLDOWN)
    assert controller.demand < 300, "the cooldown is descending, so demand and output differ"
    assert controller.residue == pytest.approx(0.0)

    # and the same for "hold here" when the heart rate has gone
    running = make_controller(initial_rpm=MotorRpm(300))
    other = ManualClock()
    drive(running, other, Seconds(60.0), bpm=105)
    assert running.residue > 0.0
    drive(running, other, Seconds(20.0), bpm=None)
    assert running.residue == pytest.approx(0.0)


def test_the_proportional_term_does_not_jump_across_a_gap_in_the_evidence() -> None:
    """A re-seed after a missing reading, so the P term differences nothing.

    The proportional part of a velocity-form increment is ``Kp * (e - e_prev)``.
    If ``e_prev`` survives a stretch with no usable heart rate, the first
    reading back gets differenced against an error from before the gap, and the
    whole change arrives as one speed step - felt by the occupant as a shove for
    which there is no reason in the data. Clearing the error on any tick that
    takes no decision makes the first tick back integral-only.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    clock = ManualClock()

    # two decisions two bpm under the band: e_prev settles at +2
    drive(controller, clock, Seconds(12.0), bpm=104)

    # the pipeline goes quiet for twenty seconds
    drive(controller, clock, Seconds(20.0), bpm=None)

    # and comes back with a reading inside the band, i.e. an error of zero
    back = controller.update(
        Monotonic(clock.monotonic() + DEFAULT_PERIOD),
        observation(bpm=110, applied_rpm=controller.demand),
    )
    assert back.held is False
    assert back.decision.in_deadband is True
    assert back.increment_rpm == pytest.approx(0.0), (
        "the first tick back must be integral-only, not Kp * (0 - 2)"
    )


def test_a_phase_change_drops_everything_the_old_phase_established() -> None:
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(60.0), phase=Phase.WARMUP, bpm=70, resting_bpm=RESTING)
    latched = controller.ramp_origin
    assert latched == RESTING
    drive(controller, clock, Seconds(0.2), phase=Phase.HOLD, bpm=70)
    dropped = controller.ramp_origin
    assert dropped is None
    assert controller.residue == pytest.approx(0.0)


def test_a_reverse_or_over_ceiling_read_back_is_not_a_base_to_build_on() -> None:
    """Clamped as integers, before any float arithmetic could overflow on it."""
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(Monotonic(0.0), observation(bpm=110, applied_rpm=-500))
    reverse = controller.update(Monotonic(5.0), observation(bpm=110, applied_rpm=-500))
    assert reverse.base_rpm == 0

    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(Monotonic(0.0), observation(bpm=110, applied_rpm=10**400))
    absurd = controller.update(Monotonic(5.0), observation(bpm=110, applied_rpm=10**400))
    assert absurd.base_rpm == MAX_RPM


# =========================================================================
# WARMUP: one law tracking a moving target
# =========================================================================


def test_the_warmup_target_ramps_from_the_measured_resting_rate_to_the_zone_floor() -> None:
    """Linear in time, and it is the TARGET that moves - the control law does not change."""
    controller = make_controller()
    targets: list[tuple[float, int]] = []
    clock = ManualClock()
    for _ in range(round(WARMUP_SECONDS / TICK) + 100):
        step = controller.update(
            Monotonic(clock.monotonic()),
            observation(
                phase=Phase.WARMUP, bpm=RESTING, applied_rpm=controller.demand, resting_bpm=RESTING
            ),
        )
        target = step.decision.target_bpm
        if not step.held and target is not None:
            targets.append((clock.monotonic(), target))
        clock.advance(TICK)

    assert controller.ramp_origin == RESTING
    assert targets[0][1] == RESTING + 1, "the ramp starts at the resting rate"
    halfway = next(t for at, t in targets if at >= WARMUP_SECONDS / 2)
    assert halfway == pytest.approx((RESTING + ZONE_LOW) / 2, abs=1)
    assert targets[-1][1] == ZONE_LOW, "and ends at the zone floor, not past it"
    assert all(t <= ZONE_LOW for _, t in targets)


def test_the_warmup_ramp_origin_falls_back_to_the_current_reading() -> None:
    """No baseline measurement yet is not a reason to aim at somebody else's resting rate."""
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(10.0), phase=Phase.WARMUP, bpm=72)
    assert controller.ramp_origin == 72


def test_the_warmup_ramp_origin_is_latched_and_not_revised() -> None:
    """A target that moves because its own origin moved is not a ramp."""
    controller = make_controller()
    clock = ManualClock()
    drive(controller, clock, Seconds(10.0), phase=Phase.WARMUP, bpm=70, resting_bpm=RESTING)
    assert controller.ramp_origin == RESTING
    drive(controller, clock, Seconds(10.0), phase=Phase.WARMUP, bpm=90, resting_bpm=Bpm(90))
    assert controller.ramp_origin == RESTING


@pytest.mark.parametrize(
    ("bpm", "resting"), [(None, None), (None, 10), (300, 300), (None, 10**400)]
)
def test_warmup_with_no_usable_ramp_origin_holds(bpm: int | None, resting: int | None) -> None:
    """Better to sit still than to ramp from a number nobody measured."""
    controller = make_controller(initial_rpm=MotorRpm(200))
    clock = ManualClock()
    steps = drive(
        controller, clock, Seconds(30.0), phase=Phase.WARMUP, bpm=bpm, resting_bpm=resting
    )
    decisions = [step for step in steps if not step.held]
    assert decisions
    assert controller.ramp_origin is None
    assert controller.demand == 200
    assert any("warmup has no resting heart rate" in s.decision.reason for s in decisions)


def test_an_unmeasurable_ramp_fraction_reads_as_the_ramp_start_not_its_end() -> None:
    """The warmup ramp clamped at BOTH ends, and the lower clamp is the dangerous one.

    A phase stamped with a nonsense instant makes the in-phase interval NaN, and
    ``min(1.0, nan)`` is ``1.0`` in CPython - so without the lower clamp the
    ramp would read as **complete** the moment it began. The target would jump
    straight to the zone floor, the error would jump with it, and the machine
    would accelerate on a ramp nobody had walked. With the clamp, an interval
    that cannot be measured reads as "the ramp has not started", which is the
    only reading of no evidence that cannot hurt anybody.

    Reachable through ``update`` because the phase stamp and the decision stamp
    are taken on different ticks: the phase can change on a tick that decides
    nothing.
    """
    controller = make_controller()
    # a real tick first, so the decision clock is finite
    controller.update(Monotonic(0.0), observation(phase=Phase.HOLD, bpm=RESTING, applied_rpm=0))
    # the phase changes on a tick with a nonsense clock: it holds, but the phase
    # stamp is now unusable
    wedged = controller.update(
        Monotonic(float("nan")),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    assert wedged.held is True
    # a later, perfectly sane tick does decide, and must read the ramp as unstarted
    step = controller.update(
        Monotonic(10.0),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    assert step.held is False
    assert step.decision.target_bpm == RESTING, (
        "an unmeasurable ramp fraction must read as the ramp's start, not its end"
    )
    assert math.isfinite(step.increment_rpm)


def test_a_warmup_interval_too_large_to_be_real_saturates_the_ramp() -> None:
    """The upper clamp: a finished ramp, not an overflowed one."""
    controller = make_controller()
    controller.update(
        Monotonic(0.0),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    forward = controller.update(
        Monotonic(1e308),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    assert forward.decision.target_bpm == ZONE_LOW
    assert math.isfinite(forward.increment_rpm)


def test_a_clock_that_ran_backwards_holds_the_setpoint_in_warmup_too() -> None:
    """And the interval gates catch it before the ramp arithmetic ever sees it."""
    controller = make_controller()
    controller.update(
        Monotonic(1000.0),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    step = controller.update(
        Monotonic(float("-inf")),
        observation(phase=Phase.WARMUP, bpm=RESTING, applied_rpm=0, resting_bpm=RESTING),
    )
    assert step.held is True


# =========================================================================
# allow_increase=False: the supervisor may permit decreases only
# =========================================================================


def test_decreases_still_work_while_increases_are_refused() -> None:
    """Not the same as taking the controller out of the loop."""
    controller = make_controller(initial_rpm=MotorRpm(500))
    clock = ManualClock()
    drive(controller, clock, Seconds(60.0), bpm=60, allow_increase=False)
    assert controller.demand == 500, "a heart rate below the band must not speed anything up"
    drive(controller, clock, Seconds(120.0), bpm=200, allow_increase=False)
    assert controller.demand < 500, "but a heart rate above it must still slow it down"


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=MIN_PLAUSIBLE_BPM, max_value=MAX_PLAUSIBLE_BPM),
            st.sampled_from(list(Phase)),
        ),
        min_size=1,
        max_size=60,
    ),
    st.sampled_from([0, MIN_RUN, 300, 900]),
)
@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_with_increases_refused_the_setpoint_is_monotonically_non_increasing(
    stream: list[tuple[int, Phase]], initial: int
) -> None:
    """PROPERTY 5. Including across the domain snap, which is the step that can move up."""
    controller = make_controller(initial_rpm=MotorRpm(initial))
    clock = ManualClock()
    previous = initial
    for bpm, phase in stream:
        step = controller.update(
            Monotonic(clock.monotonic()),
            observation(phase=phase, bpm=bpm, applied_rpm=controller.demand, resting_bpm=RESTING),
            allow_increase=False,
        )
        rpm = step.decision.desired_rpm
        assert rpm <= previous, f"{previous} -> {rpm} is an increase"
        assert rpm == 0 or MIN_RUN <= rpm <= MAX_RPM
        previous = rpm
        clock.advance(Seconds(6.0))


# =========================================================================
# Nothing non-finite, from anything
# =========================================================================


@given(
    st.lists(
        st.tuples(
            st.one_of(
                st.none(),
                st.integers(min_value=-(10**60), max_value=10**60),
                st.sampled_from([10**400, -(10**400)]),
            ),
            st.one_of(st.none(), st.integers(min_value=-(10**400), max_value=10**400)),
            st.floats(allow_nan=True, allow_infinity=True),
            st.sampled_from(list(Phase)),
        ),
        min_size=1,
        max_size=40,
    )
)
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_nothing_non_finite_ever_reaches_a_decision(
    stream: list[tuple[int | None, int | None, float, Phase]],
) -> None:
    """PROPERTY 3, and it also asserts that nothing raises.

    Absurd heart rates, absurd read-backs, a clock producing NaN and both
    infinities, and arbitrary phase order. Nothing in the control path may raise
    - an exception here unwinds the tick with the motor commanded - and no
    non-finite number may reach a field of a decision, because a NaN compares
    False against every bound the layers above test it with.
    """
    controller = make_controller()
    now = 0.0
    for bpm, applied, jump, phase in stream:
        now = now + jump if math.isfinite(jump) else jump
        step = controller.update(
            Monotonic(now),
            ControlInput(
                phase=phase,
                bpm=None if bpm is None else Bpm(bpm),
                applied_rpm=None if applied is None else MotorRpm(applied),
                resting_bpm=None if bpm is None else Bpm(bpm),
            ),
        )
        assert math.isfinite(step.decision.error_bpm)
        assert math.isfinite(step.increment_rpm)
        assert math.isfinite(step.dt)
        assert math.isfinite(controller.residue)
        rpm = step.decision.desired_rpm
        assert isinstance(rpm, int)
        assert rpm == 0 or MIN_RUN <= rpm <= MAX_RPM


# =========================================================================
# The plant sweep: a simulated occupant, the real tracker, the real law
# =========================================================================
#
# A first-order-plus-dead-time person: the heart rate approaches
# `resting + K * rpm` with time constant tau, after theta seconds of dead time.
# K in [0.05, 0.20] bpm per motor-rpm brackets the commissioning figure of
# 0.137 at full speed; tau in [20, 70] s and theta in [4, 12] s bracket the
# physiology. The drive is modelled with its commissioned ramp rather than as
# instantaneous, and the heart rate reaches the controller the way it really
# does: whole bpm, at 1 Hz, through HeartRateTracker.
#
# Observed worst cases across the grid, so the bounds below can be read against
# something rather than taken on trust:
#
#     peak heart rate      109.66 bpm  (bound: zone_high + 5 = 130)
#     final heart rate     105.50 .. 109.66 bpm  (bound: inside the zone)
#     last-quarter rpm      19 rpm peak-to-peak   (bound: 60)
#     last-quarter bpm       0.935 peak-to-peak   (bound: 3.0)
#     realised slew          17.9% of the limit   (bound: 100%)
#
# Those are the worst values over BOTH variants below, measured against this
# file's own simulate_occupant, so they can be re-derived rather than believed.
# The margins on overshoot are wide, and the reason is the design rather than
# the tuning: an asymmetric deadband corrects only on leaving the band, so the
# occupant is delivered to the floor of the zone by the warmup ramp and parks
# there. A re-tune that aimed mid-zone would eat most of that margin, which is
# what makes the bound worth asserting.
#
# The controller parks near the FLOOR of the zone, which is the designed
# behaviour of an asymmetric deadband: it corrects only on leaving the band, and
# the warmup ramp delivers the occupant to the floor.

SWEEP_GAINS: Final[tuple[float, ...]] = (0.05, 0.10, 0.15, 0.20)
SWEEP_TAUS: Final[tuple[float, ...]] = (20.0, 45.0, 70.0)
SWEEP_DEAD_TIMES: Final[tuple[float, ...]] = (4.0, 8.0, 12.0)

BASELINE_SECONDS: Final[float] = 30.0
HOLD_SECONDS: Final[float] = 1200.0
DRIVE_RAMP_RPM_PER_S: Final[float] = 900.0 / 3.5
"""The commissioned deceleration ramp: full range in 3.5 s. See src/motor/drive.py."""

OVERSHOOT_MARGIN: Final[float] = 5.0
SETTLED_RPM_BAND: Final[float] = 60.0
SETTLED_BPM_BAND: Final[float] = 3.0


def _phase_at(elapsed_s: float) -> Phase:
    if elapsed_s < BASELINE_SECONDS:
        return Phase.BASELINE
    if elapsed_s < BASELINE_SECONDS + WARMUP_SECONDS:
        return Phase.WARMUP
    return Phase.HOLD


def simulate_occupant(
    gain: float, tau: float, dead_time: float, *, quadratic: bool = False
) -> tuple[list[tuple[float, int, float]], list[tuple[float, int]]]:
    """Run a whole programme against a simulated occupant. Returns trace and changes."""
    clock = ManualClock()
    controller = make_controller()
    tracker = HeartRateTracker()
    heart_rate = float(RESTING)
    lag = max(1, round(dead_time / TICK))
    pipeline: deque[int] = deque([0] * lag, maxlen=lag)
    applied = 0
    seq = 0
    next_sample = 0.0
    trace: list[tuple[float, int, float]] = []
    changes: list[tuple[float, int]] = [(0.0, 0)]
    total = BASELINE_SECONDS + WARMUP_SECONDS + HOLD_SECONDS

    for index in range(round(total / TICK)):
        elapsed_s = index * TICK
        now = Monotonic(clock.monotonic())
        if elapsed_s >= next_sample:
            seq += 1
            tracker.observe(sample(round(heart_rate), seq=seq, at=now))
            next_sample += 1.0
        step = controller.update(
            now,
            observation(
                phase=_phase_at(elapsed_s),
                bpm=tracker.usable(now),
                applied_rpm=applied,
                resting_bpm=RESTING,
            ),
        )
        setpoint = step.decision.desired_rpm
        if changes[-1][1] != setpoint:
            changes.append((float(now), setpoint))
        ramp = DRIVE_RAMP_RPM_PER_S * TICK
        applied = round(applied + max(-ramp, min(ramp, setpoint - applied)))
        delayed = pipeline[0]
        pipeline.append(applied)
        # the quadratic variant is the real plant shape: load goes as speed
        # squared, so the local gain halves at half speed
        local_gain = gain * (delayed / MAX_RPM) * 2.0 if quadratic else gain
        heart_rate += (RESTING + local_gain * delayed - heart_rate) * TICK / tau
        trace.append((elapsed_s, setpoint, heart_rate))
        clock.advance(TICK)
    return trace, changes


def assert_well_behaved(
    trace: list[tuple[float, int, float]], changes: list[tuple[float, int]], label: str
) -> None:
    """No overshoot past the zone ceiling plus five, no sustained oscillation."""
    peak = max(hr for _, _, hr in trace)
    assert peak <= ZONE_HIGH + OVERSHOOT_MARGIN, f"{label}: overshot to {peak:.1f} bpm"

    tail = trace[int(len(trace) * 0.75) :]
    rpm_swing = max(rpm for _, rpm, _ in tail) - min(rpm for _, rpm, _ in tail)
    bpm_swing = max(hr for _, _, hr in tail) - min(hr for _, _, hr in tail)
    assert rpm_swing <= SETTLED_RPM_BAND, f"{label}: setpoint still swinging {rpm_swing} rpm"
    assert bpm_swing <= SETTLED_BPM_BAND, f"{label}: heart rate still swinging {bpm_swing:.2f} bpm"

    final = trace[-1][2]
    assert ZONE_LOW <= final <= ZONE_HIGH, f"{label}: settled at {final:.1f} bpm, outside the zone"

    for (at_a, value_a), (at_b, value_b) in itertools.pairwise(changes):
        span = at_b - at_a
        assert abs(value_b - value_a) <= SLEW * span + 1e-9, (
            f"{label}: {value_a} -> {value_b} in {span} s exceeds the slew limit"
        )


@pytest.mark.parametrize("dead_time", SWEEP_DEAD_TIMES)
@pytest.mark.parametrize("tau", SWEEP_TAUS)
@pytest.mark.parametrize("gain", SWEEP_GAINS)
def test_the_plant_sweep_neither_overshoots_nor_oscillates(
    gain: float, tau: float, dead_time: float
) -> None:
    """Thirty-six occupants, one control law, no overshoot and no limit cycle.

    This is the only evidence the gains are safe before a person is in the
    machine, which is why the grid brackets the plant rather than sampling the
    nominal case.
    """
    trace, changes = simulate_occupant(gain, tau, dead_time)
    assert_well_behaved(trace, changes, f"K={gain} tau={tau} theta={dead_time}")


@pytest.mark.parametrize(
    ("gain", "tau", "dead_time"),
    [(0.05, 20.0, 4.0), (0.20, 70.0, 12.0), (0.10, 45.0, 8.0), (0.20, 20.0, 12.0)],
)
def test_the_real_quadratic_plant_shape_behaves_too(
    gain: float, tau: float, dead_time: float
) -> None:
    """The gain halving at half speed is not a detail the law may depend on.

    Centripetal load goes as the square of speed, so the local plant gain is
    weakest exactly where the machine is slowest. The fixed-gain sweep above
    brackets that with constants; this runs the shape itself on the corners.
    """
    trace, changes = simulate_occupant(gain, tau, dead_time, quadratic=True)
    assert_well_behaved(trace, changes, f"quadratic K={gain} tau={tau} theta={dead_time}")


def test_the_warmup_spends_its_opening_at_standstill_with_these_gains() -> None:
    """A characterisation test, not a guarantee - and that is the point of it.

    With Kp = 3 rpm/bpm and Ti = 40 s, the target has to lead the heart rate for
    a while before the accumulated demand clears the 70 rpm start threshold, so
    the machine does not move for the first ~90 s of a 300 s warmup. That is
    conservative rather than wrong, and it is exactly the kind of thing the
    plant step test these gains are waiting on should settle. Pinned here so a
    re-tune shows up as a diff with a number in it rather than as a surprise on
    the machine.
    """
    controller = make_controller()
    clock = ManualClock()
    started_at: float | None = None
    for _ in range(round(WARMUP_SECONDS / TICK)):
        now = clock.monotonic()
        step = controller.update(
            Monotonic(now),
            observation(
                phase=Phase.WARMUP,
                bpm=RESTING,
                applied_rpm=controller.demand,
                resting_bpm=RESTING,
            ),
        )
        if started_at is None and step.decision.desired_rpm > 0:
            started_at = now
        clock.advance(TICK)
    assert started_at is not None, "the machine must start at some point during warmup"
    assert 60.0 <= started_at <= 120.0, f"observed 91 s, got {started_at:.1f} s"


# =========================================================================
# Shape: the records are the contract with three other modules
# =========================================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HR_CONTROL_SOURCE = PROJECT_ROOT / "src" / "training" / "hr_control.py"


def record_id(record: type) -> str:
    """Name a parametrized record. A named function, not a lambda, so it is typed."""
    return record.__name__


def shape_id(value: object) -> str:
    """Name a parametrized (record, shape) pair by the record."""
    return value.__name__ if isinstance(value, type) else ""


RECORDS: Final[tuple[type, ...]] = (
    TrackerLimits,
    StaleSequence,
    UnusableSample,
    ImplausibleRate,
    ImplausibleJump,
    TrackerReading,
    TargetBand,
    Zone,
    SpeedLimits,
    Gains,
    ControlPlan,
    ControlInput,
    ControlStep,
)


@pytest.mark.parametrize("record", RECORDS, ids=record_id)
def test_every_record_is_frozen_and_slotted(record: type) -> None:
    """An observation must not be able to change under a decision taken from it."""
    instances: dict[str, object] = {
        "TrackerLimits": TrackerLimits(),
        "StaleSequence": StaleSequence(seq=1, previous_seq=None),
        "UnusableSample": UnusableSample(quality=SignalQuality.NOISY, bpm=None),
        "ImplausibleRate": ImplausibleRate(bpm=Bpm(3000), low=Bpm(25), high=Bpm(240)),
        "ImplausibleJump": ImplausibleJump(
            bpm=Bpm(140), previous_bpm=Bpm(80), limit=Bpm(25), consecutive=1
        ),
        "TrackerReading": TrackerReading(
            bpm=Bpm(80), raw_bpm=Bpm(80), at=Monotonic(0.0), seq=1, window=1, reseeded=False
        ),
        "TargetBand": TargetBand(low=Bpm(106), high=Bpm(122)),
        "Zone": Zone(low=ZONE_LOW, high=ZONE_HIGH),
        "SpeedLimits": speed_limits(),
        "Gains": Gains(),
        "ControlPlan": make_plan(),
        "ControlInput": observation(),
        "ControlStep": make_controller().update(Monotonic(0.0), observation()),
    }
    instance = instances[record.__name__]
    field_name = fields(record)[0].name
    with pytest.raises(FrozenInstanceError):
        setattr(instance, field_name, None)
    assert not hasattr(instance, "__dict__"), "slots=True keeps these cheap at 5 Hz"
    # Three exception types are accepted because CPython's generated
    # __setattr__ for a frozen SLOTTED dataclass picks between them by name: a
    # declared field raises FrozenInstanceError (above), while a name it does
    # not know falls through to a super() call that raises TypeError. The
    # contract is that neither assignment succeeds, not which error says so.
    invented = "field_" + record.__name__.lower()
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        setattr(instance, invented, 1)
    assert not hasattr(instance, invented)


@pytest.mark.parametrize(
    ("record", "shape"),
    [
        (
            TrackerReading,
            (
                ("bpm", "Bpm"),
                ("raw_bpm", "Bpm"),
                ("at", "Monotonic"),
                ("seq", "int"),
                ("window", "int"),
                ("reseeded", "bool"),
            ),
        ),
        (
            ControlInput,
            (
                ("phase", "Phase"),
                ("bpm", "Bpm | None"),
                ("applied_rpm", "MotorRpm | None"),
                ("resting_bpm", "Bpm | None"),
            ),
        ),
        (
            ControlStep,
            (
                ("decision", "ControlDecision"),
                ("base_rpm", "MotorRpm"),
                ("increment_rpm", "float"),
                ("dt", "Seconds"),
                ("at_ceiling", "bool"),
                ("slew_limited", "bool"),
                ("held", "bool"),
            ),
        ),
        (
            SpeedLimits,
            (
                ("min_run_rpm", "MotorRpm"),
                ("max_rpm", "MotorRpm"),
                ("warmup_max_rpm", "MotorRpm"),
                ("slew", "RpmPerSecond"),
                ("start_hysteresis_rpm", "MotorRpm"),
            ),
        ),
    ],
    ids=shape_id,
)
def test_the_field_shape_is_the_contract(record: type, shape: tuple[tuple[str, str], ...]) -> None:
    """Name, order and annotation text of every field, pinned.

    This is what catches "an optional stopped being optional" and "a MotorRpm
    became an OutputRpm" for the three modules built against this one.
    """
    actual = tuple((field.name, str(field.type)) for field in fields(record))
    assert actual == shape


def test_the_decision_is_the_shared_contract_type() -> None:
    """Not a copy of it: the runner and the UI must see one ControlDecision."""
    step = make_controller().update(Monotonic(0.0), observation())
    assert type(step.decision) is ControlDecision


def test_the_module_reads_no_clock_and_depends_on_nothing_in_training() -> None:
    """Contract rule 4, checked at the import level rather than by inspection.

    ``now`` arrives as a parameter everywhere, which is what lets a 45-minute
    programme be simulated in a second. Importing ``time`` at all would be the
    first step toward losing that, so the import list is the thing asserted.
    """
    tree = ast.parse(HR_CONTROL_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert "time" not in imported
    assert {name for name in imported if name.startswith("src.")} == {
        "src.result",
        "src.training.types",
        "src.units",
    }


# =========================================================================
# rebase: a verdict decided the output, and the controller must adopt it
# =========================================================================


def test_rebase_adopts_the_applied_speed_as_the_standing_demand() -> None:
    """The review finding, at unit level: a capped demand must not survive the cap.

    A REDUCE walked the machine from 900 rpm down to 120 while the controller
    kept wanting 900. Without the rebase, the first tick after the verdict lifts
    re-emits 900 in one write.
    """
    clock = ManualClock(Monotonic(0.0))
    controller = make_controller(initial_rpm=MotorRpm(900))
    controller.rebase(clock.monotonic(), MotorRpm(120))
    assert controller.demand == 120
    assert controller.residue == 0.0
    step = controller.update(clock.monotonic(), observation(bpm=90, applied_rpm=120))
    assert step.decision.desired_rpm == 120


def test_rebase_restarts_the_decision_interval() -> None:
    """The verdict's own step already spent the allowance up to now.

    Keeping the old interval would let the first decision after the verdict move
    by ``slew * step_cap`` one tick after the verdict itself moved the setpoint.
    """
    clock = ManualClock(Monotonic(0.0))
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(clock.monotonic(), observation(bpm=90, applied_rpm=300))
    clock.advance(Gains().period)
    rebased_at = clock.monotonic()
    controller.rebase(rebased_at, MotorRpm(200))
    first: tuple[float, MotorRpm] | None = None
    for _ in range(round(Gains().period * 2 / TICK)):
        clock.advance(TICK)
        step = controller.update(clock.monotonic(), observation(bpm=90, applied_rpm=200))
        if not step.held:
            first = (clock.monotonic() - rebased_at, step.decision.desired_rpm)
            break
    assert first is not None, "the controller never decided again after the rebase"
    waited, rpm = first
    assert waited >= Gains().period - 1e-9, "the pre-rebase interval was spent after the rebase"
    assert 200 < rpm <= 200 + SLEW * waited + 1


def test_rebase_to_the_standing_demand_changes_nothing() -> None:
    """A verdict that is not actually biting must leave the controller regulating.

    If the no-op rebase restarted the interval, a REDUCE whose cap sat above
    the controller's own demand would stop it deciding at all.
    """
    clock = ManualClock(Monotonic(0.0))
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.update(clock.monotonic(), observation(bpm=140, applied_rpm=300))
    clock.advance(Gains().period)
    controller.rebase(clock.monotonic(), MotorRpm(300))
    step = controller.update(clock.monotonic(), observation(bpm=140, applied_rpm=300))
    assert not step.held
    assert step.decision.desired_rpm < 300


@pytest.mark.parametrize(
    ("applied", "adopted"),
    [
        pytest.param(MotorRpm(MAX_RPM + 50), MAX_RPM, id="above-max-snaps-to-max"),
        pytest.param(MotorRpm(MIN_RUN - 1), MotorRpm(0), id="gap-snaps-to-zero"),
        pytest.param(MotorRpm(-20), MotorRpm(0), id="negative-snaps-to-zero"),
        pytest.param(MIN_RUN, MIN_RUN, id="minimum-is-kept"),
    ],
)
def test_rebase_snaps_down_into_the_setpoint_domain(applied: MotorRpm, adopted: MotorRpm) -> None:
    """Total, and wrong only toward a slower machine.

    The standing demand must stay inside ``{0} union [min_run, max]`` - the
    envelope's promise depends on it - so a value outside is snapped, and always
    downward: the belief may undershoot the machine, never overshoot it.
    """
    controller = make_controller(initial_rpm=MotorRpm(300))
    controller.rebase(Monotonic(0.0), applied)
    assert controller.demand == adopted
