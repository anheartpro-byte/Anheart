"""The vocabulary of the presence fail-safe: what a camera tells us, and in what types.

A detector (a separate process, see ``src/presence/README.md``) looks at the
machine and publishes one :class:`PresenceObservation` per analysed frame. This
module is the shape of that record and nothing else: no rule, no threshold, no
clock read, no I/O.

Two traps are designed out here rather than documented away:

1. **A frozen camera looks alive.** A camera that has stopped delivering new
   images keeps handing the detector the same frame, and the detector keeps
   saying "zone clear" about it, with a perfectly current publication time.
   :attr:`PresenceObservation.frame_seq` is the defence, exactly like
   :attr:`~src.training.types.HeartRateSample.seq`: it increases only when a
   genuinely new frame was analysed, and
   :meth:`PresenceObservation.is_new_evidence_after` is the only comparison
   anybody should make on it.
2. **"Nobody seen" is not "nobody there".** A degraded image (lens obstructed,
   lights off, glare) sees nobody. :class:`CameraHealth` travels with every
   observation so the rules can refuse to read an absence of detections in a
   degraded frame as evidence that the zone is clear - while still acting on a
   detection in that same frame, which is the fail-safe asymmetry.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, NewType, Protocol

from src.result import Err, Ok, Result
from src.training.types import Occupancy
from src.units import Metres, Monotonic

Confidence = NewType("Confidence", float)
"""A detector's confidence that what it reports is real, in ``[0, 1]``.

A ``NewType`` rather than a bare ``float`` so it cannot be confused with a
distance or a duration at a call site. It lives here rather than in
``src/units.py`` because it is not a physical unit and only this package uses
it. Construct one through :func:`parse_confidence` at the boundary where a
detector's number enters; past that, the type carries the range.
"""

CONFIDENCE_MIN: Final[float] = 0.0
CONFIDENCE_MAX: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class ConfidenceOutOfRange:
    """A confidence outside ``[0, 1]``, or not a number at all."""

    value: float


def parse_confidence(value: float) -> Result[Confidence, ConfidenceOutOfRange]:
    """Validate a detector's confidence. Total; never raises.

    ``math.isfinite`` first: every comparison against NaN is false, so a range
    check alone would *accept* a NaN confidence, and a NaN compared against a
    threshold reads as "below it" - a person reported with NaN confidence would
    be ignored.
    """
    if not math.isfinite(value) or not CONFIDENCE_MIN <= value <= CONFIDENCE_MAX:
        return Err(ConfidenceOutOfRange(value))
    return Ok(Confidence(value))


@unique
class CameraHealth(Enum):
    """The detector's own judgement of the image it analysed. Wire strings."""

    HEALTHY = "healthy"
    """The image is usable: an absence of detections means the zone is clear."""

    DEGRADED = "degraded"
    """The image is poor (obstruction, glare, darkness, motion blur).

    A detection in it still counts - fail toward stopping - but "nobody seen"
    in it is not evidence of anything, so it neither clears the zone nor
    refreshes the camera's freshness."""

    FAILED = "failed"
    """The detector says it cannot see at all. Treated as a lost camera at once."""


@unique
class CapsuleState(Enum):
    """What the camera sees in the capsule. Wire strings."""

    OCCUPIED = "occupied"
    """A person is in the seat."""

    EMPTY = "empty"
    """The seat is visibly empty."""

    UNKNOWN = "unknown"
    """The capsule is not visible or not classifiable. Never read as either answer."""


@dataclass(frozen=True, slots=True)
class ZoneClear:
    """Nobody detected in the danger zone around the arm."""


@dataclass(frozen=True, slots=True)
class PersonInZone:
    """A person detected in the danger zone around the arm.

    ``distance`` is from the arm's swept envelope, when the detector can
    estimate it (a depth camera, or a calibrated floor plane); ``None`` when it
    cannot. ``None`` is never read as "far".
    """

    confidence: Confidence
    distance: Metres | None = None


type ZoneView = ZoneClear | PersonInZone
"""The danger zone, as one frame shows it. Closed: match it ending in ``assert_never``."""


@dataclass(frozen=True, slots=True)
class RiderPosture:
    """Optional posture flags about the rider, when the detector provides them."""

    unbuckled: bool
    """The harness is visibly not fastened."""

    limb_outside: bool
    """A head, arm or leg is outside the capsule's envelope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceObservation:
    """One analysed frame: everything the presence rules are allowed to judge.

    Keyword-only, like :class:`~src.training.safety.SafetyObservation`: two
    adjacent fields of one type (``frame_seq`` and a count) are exactly what a
    positional constructor transposes.
    """

    frame_seq: int
    """Increases only when a genuinely new frame was analysed. See the module docstring."""

    at: Monotonic
    """When the frame was CAPTURED, on this machine's monotonic clock.

    Capture, not publication: the age of the evidence is what matters, and a
    detector that takes 300 ms to analyse a frame is reporting on the past.
    """

    health: CameraHealth
    zone: ZoneView
    capsule: CapsuleState
    posture: RiderPosture | None = None
    """``None`` when the detector does not report posture. No posture rule fires then."""

    def is_new_evidence_after(self, previous_seq: int | None) -> bool:
        """Whether this frame is newer than ``previous_seq``. Strictly greater only.

        ``>=`` would accept the frozen camera's repeated frame, and ``!=`` would
        accept a counter that restarted and so treat an old frame as new.
        ``None`` means no frame has been seen yet.
        """
        if previous_seq is None:
            return True
        return self.frame_seq > previous_seq


class PresenceSource(Protocol):
    """Where observations come from: a camera's detector, or the simulator.

    Poll-shaped and non-blocking on purpose. It is called from the event loop
    at the camera's rate, and a read that could wait on a socket or a vision
    library would stall the loop that also carries the drive's keepalive. A
    real source therefore reads from a queue or a shared slot that the
    detector process fills; see ``src/presence/README.md``.
    """

    @abstractmethod
    def latest(self) -> PresenceObservation | None:
        """The most recent observation, or ``None`` if there has never been one.

        Returning the SAME observation twice is allowed and expected - that is
        how a frozen camera looks - and the monitor recognises it by its
        unchanged :attr:`PresenceObservation.frame_seq`.
        """


@unique
class MotionState(Enum):
    """What the machine is doing, as far as the presence rules care. Wire strings."""

    AT_REST = "at_rest"
    """No session armed, output stage off, and the shaft MEASURED stopped."""

    ARMED = "armed"
    """A session is armed (output stage on) but the shaft is measured stopped.

    Treated like turning: an armed machine moves the moment somebody sets a
    target, so a person in the zone of an armed machine is a person in the
    zone of a moving one."""

    TURNING = "turning"
    """The shaft is measured turning, session or not (a coast-down counts)."""

    UNKNOWN = "unknown"
    """No fresh drive observation. Treated as turning: unknown is never "stopped"."""

    @property
    def motion_possible(self) -> bool:
        """Whether the arm may be moving now or at any moment. Only AT_REST says no."""
        return self is not MotionState.AT_REST


@dataclass(frozen=True, slots=True)
class MachineContext:
    """The machine's side of one evaluation.

    ``occupancy`` is the session's declared occupancy, or ``None`` when no
    session exists. A programme always runs OCCUPIED.
    """

    motion: MotionState
    occupancy: Occupancy | None
