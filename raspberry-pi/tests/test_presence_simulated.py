"""The simulated camera: frame timing, and every scripted failure mode."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.presence.simulated import (
    CameraEvent,
    CapsuleChange,
    Dropout,
    Flicker,
    Freeze,
    HealthChange,
    Intrusion,
    PostureChange,
    SimulatedCamera,
)
from src.presence.types import (
    CameraHealth,
    CapsuleState,
    Confidence,
    PersonInZone,
    PresenceObservation,
    RiderPosture,
    ZoneClear,
)
from src.units import Hertz, Metres, Monotonic, Seconds

FRAME: Final[Seconds] = Seconds(0.05)


def _at(clock: ManualClock, camera: SimulatedCamera, when: float) -> PresenceObservation | None:
    clock.advance(Seconds(when - clock.monotonic()))
    return camera.latest()


def _seen(clock: ManualClock, camera: SimulatedCamera, when: float) -> PresenceObservation:
    frame = _at(clock, camera, when)
    assert frame is not None
    return frame


@pytest.mark.parametrize("fps", [0.0, -1.0, math.nan, math.inf])
def test_a_non_positive_or_non_finite_rate_is_refused(fps: float) -> None:
    with pytest.raises(ValueError, match="fps"):
        SimulatedCamera(ManualClock(), fps=Hertz(fps))


@pytest.mark.parametrize("latency", [-0.1, math.nan])
def test_a_negative_or_nan_latency_is_refused(latency: float) -> None:
    with pytest.raises(ValueError, match="latency"):
        SimulatedCamera(ManualClock(), latency=Seconds(latency))


def test_frames_are_numbered_from_the_origin_and_stamped_with_their_capture_time() -> None:
    clock = ManualClock(Monotonic(10.0))
    camera = SimulatedCamera(clock, fps=Hertz(20.0))
    assert camera.origin == 10.0
    first = _seen(clock, camera, 10.0)
    assert first.frame_seq == 0
    assert first.at == 10.0
    later = _seen(clock, camera, 10.52)
    assert later.frame_seq == 10
    assert later.at == pytest.approx(10.5)
    assert camera.capture_time(10) == pytest.approx(10.5)
    assert later.health is CameraHealth.HEALTHY
    assert later.zone == ZoneClear()
    assert later.capsule is CapsuleState.EMPTY
    assert later.posture is None


def test_nothing_is_visible_until_the_first_frame_has_been_analysed() -> None:
    clock = ManualClock()
    camera = SimulatedCamera(clock, latency=Seconds(0.15))
    assert camera.latest() is None
    frame = _seen(clock, camera, 0.2)
    assert frame.frame_seq == 1  # captured at 0.05, visible at 0.20


def test_an_intrusion_is_seen_only_inside_its_window() -> None:
    clock = ManualClock()
    person = Intrusion(
        start=Monotonic(1.0), end=Monotonic(2.0), confidence=Confidence(0.7), distance=Metres(1.2)
    )
    camera = SimulatedCamera(clock, script=(person,))
    assert _seen(clock, camera, 0.9).zone == ZoneClear()
    assert _seen(clock, camera, 1.0).zone == PersonInZone(Confidence(0.7), Metres(1.2))
    assert _seen(clock, camera, 2.0).zone == ZoneClear()


def test_a_flicker_alternates_between_its_two_confidences() -> None:
    clock = ManualClock()
    flicker = Flicker(start=Monotonic(0.0), high=Confidence(0.9), low=Confidence(0.1))
    camera = SimulatedCamera(clock, script=(flicker,))
    assert _seen(clock, camera, 0.0).zone == PersonInZone(Confidence(0.9))
    assert _seen(clock, camera, 0.05).zone == PersonInZone(Confidence(0.1))
    assert _seen(clock, camera, 0.1).zone == PersonInZone(Confidence(0.9))


def test_capsule_posture_and_health_follow_the_script_and_later_events_override() -> None:
    clock = ManualClock()
    unbuckled = RiderPosture(unbuckled=True, limb_outside=False)
    camera = SimulatedCamera(
        clock,
        capsule=CapsuleState.OCCUPIED,
        posture=RiderPosture(unbuckled=False, limb_outside=False),
        script=(
            CapsuleChange(start=Monotonic(1.0), state=CapsuleState.EMPTY),
            PostureChange(start=Monotonic(1.0), posture=unbuckled),
            HealthChange(start=Monotonic(1.0), health=CameraHealth.DEGRADED),
        ),
    )
    before = _seen(clock, camera, 0.5)
    assert before.capsule is CapsuleState.OCCUPIED
    assert before.posture == RiderPosture(unbuckled=False, limb_outside=False)
    after = _seen(clock, camera, 1.0)
    assert after.capsule is CapsuleState.EMPTY
    assert after.posture == unbuckled
    assert after.health is CameraHealth.DEGRADED
    camera.schedule(PostureChange(start=Monotonic(2.0), posture=None))
    camera.schedule(HealthChange(start=Monotonic(2.0), health=CameraHealth.FAILED))
    last = _seen(clock, camera, 2.0)
    assert last.posture is None
    assert last.health is CameraHealth.FAILED


def test_a_frozen_camera_repeats_its_last_frame_with_the_same_sequence_number() -> None:
    clock = ManualClock()
    camera = SimulatedCamera(clock, script=(Freeze(start=Monotonic(1.0), end=Monotonic(3.0)),))
    frozen = _seen(clock, camera, 1.5)
    assert frozen.frame_seq == 20
    assert _seen(clock, camera, 2.9) == frozen
    assert _seen(clock, camera, 3.0).frame_seq == 60


def test_a_freeze_from_before_the_origin_shows_nothing() -> None:
    clock = ManualClock(Monotonic(5.0))
    camera = SimulatedCamera(clock, script=(Freeze(start=Monotonic(4.0)),))
    assert _at(clock, camera, 6.0) is None


def test_a_dropped_camera_publishes_nothing_until_it_comes_back() -> None:
    clock = ManualClock()
    camera = SimulatedCamera(
        clock,
        script=(Dropout(start=Monotonic(1.0), end=Monotonic(2.0)), Freeze(start=Monotonic(9.0))),
    )
    assert _at(clock, camera, 0.5) is not None
    assert _at(clock, camera, 1.5) is None
    assert _seen(clock, camera, 2.0).frame_seq == 40


@dataclass(frozen=True, slots=True)
class Meteor:
    """Shaped like a camera event, and in no union."""

    start: Monotonic
    end: Monotonic | None = None


def test_an_unknown_scripted_event_fails_loudly() -> None:
    clock = ManualClock()
    camera = SimulatedCamera(clock)
    camera.schedule(cast(CameraEvent, Meteor(start=Monotonic(0.0))))
    with pytest.raises(AssertionError):
        camera.latest()
