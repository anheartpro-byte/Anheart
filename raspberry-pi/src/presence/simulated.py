"""A camera that is not there: a scriptable :class:`~src.presence.types.PresenceSource`.

For the tests and the simulation. Time is injected: the frame on offer is
derived from the clock, so a ``ManualClock`` makes every frame exact. Frames
are captured at ``fps`` from the origin; frame ``n`` is captured at
``origin + n / fps``, carries ``frame_seq == n``, and becomes visible
``latency`` later, as a real detector's output does.

A script is a sequence of :data:`CameraEvent` values, each active on
``[start, end)`` of CAPTURE time (``end=None``: forever). Later events override
earlier ones where they touch the same field. The failure modes a real camera
has are all here:

* :class:`Intrusion` - a person in the danger zone, at a confidence;
* :class:`Flicker` - a detector alternating between two confidences frame by frame;
* :class:`CapsuleChange` / :class:`PostureChange` - the rider leaves, unbuckles;
* :class:`HealthChange` - a degraded or failed image;
* :class:`Freeze` - the camera keeps delivering its last frame (same ``frame_seq``);
* :class:`Dropout` - the detector publishes nothing at all (``latest()`` is ``None``).

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, assert_never, final

from src.clock import Clock
from src.presence.types import (
    CameraHealth,
    CapsuleState,
    Confidence,
    PersonInZone,
    PresenceObservation,
    RiderPosture,
    ZoneClear,
    ZoneView,
)
from src.units import Hertz, Metres, Monotonic, Seconds

DEFAULT_FPS: Final[Hertz] = Hertz(20.0)
"""Comfortably above the 15 fps minimum the README requires of a real camera."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Intrusion:
    """A person in the danger zone."""

    start: Monotonic
    end: Monotonic | None = None
    confidence: Confidence
    distance: Metres | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Flicker:
    """A detection alternating between ``high`` (even frames) and ``low`` (odd frames)."""

    start: Monotonic
    end: Monotonic | None = None
    high: Confidence
    low: Confidence


@dataclass(frozen=True, slots=True, kw_only=True)
class CapsuleChange:
    """The capsule reads ``state`` from ``start``."""

    start: Monotonic
    end: Monotonic | None = None
    state: CapsuleState


@dataclass(frozen=True, slots=True, kw_only=True)
class PostureChange:
    """The rider's posture reads ``posture`` from ``start``."""

    start: Monotonic
    end: Monotonic | None = None
    posture: RiderPosture | None


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthChange:
    """The detector grades its image ``health`` from ``start``."""

    start: Monotonic
    end: Monotonic | None = None
    health: CameraHealth


@dataclass(frozen=True, slots=True, kw_only=True)
class Freeze:
    """The camera repeats the last frame captured before ``start``."""

    start: Monotonic
    end: Monotonic | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Dropout:
    """The detector publishes nothing: ``latest()`` answers ``None``."""

    start: Monotonic
    end: Monotonic | None = None


type CameraEvent = (
    Intrusion | Flicker | CapsuleChange | PostureChange | HealthChange | Freeze | Dropout
)
"""Closed: every scripted condition the simulated camera can show."""


def _active(event: CameraEvent, at: Monotonic) -> bool:
    return event.start <= at and (event.end is None or at < event.end)


@final
class SimulatedCamera:
    """A scriptable camera. **Mutable** (its script can grow), like the device it replaces."""

    __slots__ = ("_capsule", "_clock", "_fps", "_latency", "_origin", "_posture", "_script")

    def __init__(
        self,
        clock: Clock,
        *,
        fps: Hertz = DEFAULT_FPS,
        latency: Seconds = Seconds(0.0),
        capsule: CapsuleState = CapsuleState.EMPTY,
        posture: RiderPosture | None = None,
        script: Iterable[CameraEvent] = (),
    ) -> None:
        """Raises ``ValueError`` on a non-positive rate or a negative latency (startup only)."""
        if not math.isfinite(fps) or fps <= 0.0:
            raise ValueError(f"fps must be finite and positive, got {fps}")
        if not math.isfinite(latency) or latency < 0.0:
            raise ValueError(f"latency must be finite and not negative, got {latency}")
        self._clock: Clock = clock
        self._fps: Hertz = fps
        self._latency: Seconds = latency
        self._origin: Monotonic = clock.monotonic()
        self._capsule: CapsuleState = capsule
        self._posture: RiderPosture | None = posture
        self._script: list[CameraEvent] = list(script)

    @property
    def origin(self) -> Monotonic:
        """When frame 0 was captured."""
        return self._origin

    def schedule(self, event: CameraEvent) -> None:
        """Add one event to the script. It overrides every earlier one it overlaps."""
        self._script.append(event)

    def capture_time(self, frame_seq: int) -> Monotonic:
        """When frame ``frame_seq`` was captured."""
        return Monotonic(self._origin + frame_seq / self._fps)

    def latest(self) -> PresenceObservation | None:
        """The most recent frame visible at the clock's ``now``, or ``None``."""
        now = self._clock.monotonic()
        visible = Monotonic(now - self._latency)
        index = math.floor((visible - self._origin) * self._fps)
        if index < 0:
            return None
        for event in self._script:
            if isinstance(event, Dropout) and _active(event, visible):
                return None
        frame = index
        for event in self._script:
            if isinstance(event, Freeze) and _active(event, visible):
                frame = min(frame, math.floor((event.start - self._origin) * self._fps))
        if frame < 0:
            return None
        return self._frame(frame)

    def _frame(self, frame_seq: int) -> PresenceObservation:
        """Frame ``frame_seq`` as the script describes it at its capture time."""
        at = self.capture_time(frame_seq)
        zone: ZoneView = ZoneClear()
        capsule = self._capsule
        posture = self._posture
        health = CameraHealth.HEALTHY
        for event in self._script:
            if not _active(event, at):
                continue
            match event:
                case Intrusion(confidence=confidence, distance=distance):
                    zone = PersonInZone(confidence, distance)
                case Flicker(high=high, low=low):
                    zone = PersonInZone(high if frame_seq % 2 == 0 else low)
                case CapsuleChange(state=state):
                    capsule = state
                case PostureChange(posture=changed):
                    posture = changed
                case HealthChange(health=changed_health):
                    health = changed_health
                case Freeze() | Dropout():
                    pass
                case _ as unreachable:
                    assert_never(unreachable)
        return PresenceObservation(
            frame_seq=frame_seq,
            at=at,
            health=health,
            zone=zone,
            capsule=capsule,
            posture=posture,
        )
