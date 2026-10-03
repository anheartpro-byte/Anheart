"""Tests for the tracking envelope: where the shaft may legitimately be.

The envelope replaced "switch ``tracking_error`` off while the setpoint ramps",
which left the rule blind for the ~100 s of every motion-limited climb and stop.
What these tests pin: the follower moves towards the command at its rate and no
faster, it re-anchors on the measured speed only while the drive is not working
to the reference, a clock it cannot trust moves nothing, the band always holds
both the command and the follower, and the rate is the drive's own commissioned
ramp, derated and floored.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.motor.drive import DriveLimits
from src.training.tracking import (
    TRACKING_RAMP_MARGIN,
    UNREAD_RAMP,
    SpeedFollower,
    follow_rate,
)
from src.training.types import SpeedEnvelope
from src.units import Hertz, Monotonic, MotorRpm, RpmPerSecond, Seconds

NOMINAL = MotorRpm(1380)


def _limits(acc: float, dec: float) -> DriveLimits:
    return DriveLimits(
        max_frequency=Hertz(60.0),
        high_speed=Hertz(50.0),
        low_speed=Hertz(0.0),
        acceleration=Seconds(acc),
        deceleration=Seconds(dec),
    )


def test_the_rate_is_the_slower_commissioned_ramp_derated() -> None:
    """The simulator's 10 s ACC and dEC: 138 rpm/s at the drive, 46 rpm/s modelled."""
    assert follow_rate(_limits(10.0, 12.0), NOMINAL) == pytest.approx(1380 / 12.0 / 3.0)
    assert follow_rate(_limits(12.0, 10.0), NOMINAL) == pytest.approx(1380 / 12.0 / 3.0)


def test_a_ramp_faster_than_the_bench_one_is_floored_and_nothing_read_uses_it() -> None:
    """A read of 0.0 s must not become a follower that teleports."""
    floored = float(NOMINAL) / float(UNREAD_RAMP) / TRACKING_RAMP_MARGIN
    assert follow_rate(_limits(0.0, 0.0), NOMINAL) == pytest.approx(floored)
    assert follow_rate(None, NOMINAL) == pytest.approx(floored)


def test_the_first_band_starts_where_the_shaft_is() -> None:
    follower = SpeedFollower(RpmPerSecond(50.0))
    band = follower.update(Monotonic(0.0), MotorRpm(0), MotorRpm(900), following=True)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(900))


def test_the_first_band_with_nothing_measured_starts_at_the_command() -> None:
    follower = SpeedFollower(RpmPerSecond(50.0))
    band = follower.update(Monotonic(0.0), MotorRpm(300), None, following=True)
    assert band == SpeedEnvelope(low=MotorRpm(300), high=MotorRpm(300))


def test_the_follower_moves_towards_the_command_at_its_rate() -> None:
    follower = SpeedFollower(RpmPerSecond(50.0))
    follower.update(Monotonic(0.0), MotorRpm(0), MotorRpm(1000), following=True)
    band = follower.update(Monotonic(2.0), MotorRpm(0), MotorRpm(500), following=True)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(900))
    band = follower.update(Monotonic(40.0), MotorRpm(0), MotorRpm(0), following=True)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(0))


def test_a_setpoint_slower_than_the_follower_is_simply_tracked() -> None:
    """At the motion limits the follower coincides with the setpoint: the band is tight."""
    follower = SpeedFollower(RpmPerSecond(46.0))
    follower.update(Monotonic(0.0), MotorRpm(500), MotorRpm(500), following=True)
    for tick in range(1, 50):
        commanded = MotorRpm(500 + round(12.4 * 0.2 * tick))
        band = follower.update(Monotonic(0.2 * tick), commanded, None, following=True)
        assert band.high - band.low <= 1, band


def test_a_drive_not_following_re_anchors_the_follower_on_the_shaft() -> None:
    """Disabled, coasting or faulted: the band starts again from the real speed."""
    follower = SpeedFollower(RpmPerSecond(50.0))
    follower.update(Monotonic(0.0), MotorRpm(0), MotorRpm(0), following=True)
    band = follower.update(Monotonic(1.0), MotorRpm(0), MotorRpm(700), following=False)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(700))
    # With nothing measured it keeps integrating towards the command instead.
    band = follower.update(Monotonic(2.0), MotorRpm(0), None, following=False)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(650))


@pytest.mark.parametrize("later", [math.nan, -5.0, 0.0])
def test_an_interval_it_cannot_trust_moves_nothing(later: float) -> None:
    follower = SpeedFollower(RpmPerSecond(50.0))
    follower.update(Monotonic(0.0), MotorRpm(0), MotorRpm(800), following=True)
    band = follower.update(Monotonic(later), MotorRpm(0), MotorRpm(0), following=True)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(800))


def test_a_retuned_follower_uses_its_new_rate() -> None:
    follower = SpeedFollower(RpmPerSecond(50.0))
    follower.retune(RpmPerSecond(100.0))
    assert follower.rate == 100.0
    follower.update(Monotonic(0.0), MotorRpm(0), MotorRpm(500), following=True)
    band = follower.update(Monotonic(1.0), MotorRpm(0), None, following=True)
    assert band.high == 400


def test_the_band_is_rounded_outwards() -> None:
    follower = SpeedFollower(RpmPerSecond(0.5))
    follower.update(Monotonic(0.0), MotorRpm(10), MotorRpm(0), following=True)
    band = follower.update(Monotonic(1.0), MotorRpm(10), None, following=True)
    assert band == SpeedEnvelope(low=MotorRpm(0), high=MotorRpm(10))


def test_the_distance_outside_a_band_is_zero_inside_and_signed_by_side() -> None:
    band = SpeedEnvelope(low=MotorRpm(100), high=MotorRpm(200))
    assert band.distance(MotorRpm(150)) == 0
    assert band.distance(MotorRpm(100)) == 0
    assert band.distance(MotorRpm(40)) == 60
    assert band.distance(MotorRpm(260)) == 60


@settings(max_examples=200, deadline=None)
@given(
    rate=st.floats(min_value=1.0, max_value=500.0),
    start=st.integers(min_value=0, max_value=1500),
    steps=st.lists(
        st.tuples(
            st.floats(min_value=0.0, max_value=3.0),
            st.integers(min_value=0, max_value=1500),
        ),
        min_size=1,
        max_size=40,
    ),
)
def test_the_band_always_holds_the_command_and_the_follower_moves_no_faster_than_its_rate(
    rate: float, start: int, steps: list[tuple[float, int]]
) -> None:
    """PROPERTY: for any command history, the band contains the command, and its far
    edge moves at most ``rate * dt`` (plus the one rpm of outward rounding)."""
    follower = SpeedFollower(RpmPerSecond(rate))
    now = 0.0
    previous = follower.update(Monotonic(now), MotorRpm(start), MotorRpm(start), following=True)
    far = float(start)
    for dt, commanded in steps:
        now += dt
        band = follower.update(Monotonic(now), MotorRpm(commanded), None, following=True)
        assert band.low <= commanded <= band.high
        edge = band.low if band.high == commanded else band.high
        assert abs(edge - far) <= rate * dt + 2.0, (far, edge, rate, dt)
        far = float(edge)
        previous = band
    assert previous.low <= previous.high
