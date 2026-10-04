"""Where the shaft may legitimately be: the envelope ``tracking_error`` judges against.

``tracking_error`` used to be switched OFF while the setpoint ramped, on the
argument that commanded and measured speed are *supposed* to differ during a
ramp. That argument holds for a step the drive has to chase for seconds; it
does not hold for this machine's normal motion, where the setpoint moves at the
anti-nausea limit (12.4 motor rpm/s) and the drive's own commissioned ramp is
an order of magnitude faster (138 rpm/s in the simulator, ~345 rpm/s at the
bench's 4 s dEC). A manual climb or stop at the motion limits ramps for about a
hundred seconds, and the rule was blind for all of them: a measured speed stuck
during a climb was caught 52 s late, a frozen status during a stop 110 s late.

So the rule is now judged against an ENVELOPE rather than switched off: the
band between the commanded setpoint and where a drive following it at its own
ramp could have got to by now. :class:`SpeedFollower` computes the second edge
- a rate-limited follower of the commanded setpoint - and
:class:`~src.training.types.SpeedEnvelope` carries the band to the supervisor.

The follower rate is the drive's commissioned ramp read back at arming (ACC and
dEC, the slower of the two), DERATED by :data:`TRACKING_RAMP_MARGIN`. A follower
slower than the real drive only widens the band on the side the drive is moving
towards, which can make the rule later, never falser; a follower faster than
the real drive would report a perfectly healthy deceleration as a tracking
failure. The margin covers what makes a real ramp slower than its parameter:
the ATV320's deceleration ramp adaptation (brA) stretching dEC so that ~420 J of
rotating rig does not trip the DC bus, current limiting under load, and RFRD's
own filtering. For every NON-emergency setpoint change the derated follower is
still several times faster than the setpoint itself, so on those it coincides
with the setpoint and the band is simply the tolerance around it.

Pure and clock-free: time arrives as ``now`` on every call (contract rule 4).

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from typing import Final, final

from src.motor.drive import DriveLimits
from src.training.types import SpeedEnvelope
from src.units import Monotonic, MotorRpm, RpmPerSecond, Seconds, elapsed

TRACKING_RAMP_MARGIN: Final[float] = 3.0
"""The follower runs at a third of the drive's commissioned ramp. See the module docstring.

Three rather than two because brA can roughly double a deceleration of this rig
on its own, and the follower must stay slower than the real drive through every
emergency zero, or a healthy stop reads as a tracking failure on top of it.
"""

UNREAD_RAMP: Final[Seconds] = Seconds(4.0)
"""The ramp time assumed before any ACC/dEC was read: the bench's measured 4 s dEC.

Also the FLOOR under a ramp time read back: a drive commissioned faster than
this only sits more comfortably inside the band, and flooring keeps a read of
0.0 s (instant ramps) from becoming a follower that teleports.
"""


def follow_rate(limits: DriveLimits | None, nominal_rpm: MotorRpm) -> RpmPerSecond:
    """The follower's rate, motor rpm/s: the slower commissioned ramp, derated.

    ACC and dEC are both "0 <-> nominal frequency" times, so the drive covers
    ``nominal_rpm`` motor rpm in that time. ``None`` (nothing read yet) and any
    ramp shorter than :data:`UNREAD_RAMP` both use :data:`UNREAD_RAMP`.
    """
    ramp = float(UNREAD_RAMP)
    if limits is not None:
        ramp = max(ramp, float(limits.acceleration), float(limits.deceleration))
    return RpmPerSecond(float(nominal_rpm) / ramp / TRACKING_RAMP_MARGIN)


@final
class SpeedFollower:
    """A rate-limited follower of the commanded setpoint: the envelope's far edge.

    **Mutable**: it remembers where a drive following the command would be, and
    when it last moved. Owned by one runtime loop, shared with nobody.

    Anchored to the MEASURED speed whenever the drive is not following the
    reference at all (not OPERATION_ENABLED: disabled, coasting, faulted), and
    on the very first observation, so an envelope never starts from a speed the
    shaft was not at. While the drive says it is enabled the follower is NOT
    re-anchored - that is what lets a frozen or stuck RFRD fall outside the band.
    """

    __slots__ = ("_at", "_expected", "_rate")

    def __init__(self, rate: RpmPerSecond) -> None:
        self._rate: RpmPerSecond = rate
        self._expected: float | None = None
        self._at: Monotonic | None = None

    @property
    def rate(self) -> RpmPerSecond:
        """The follower's rate, motor rpm per second."""
        return self._rate

    def retune(self, rate: RpmPerSecond) -> None:
        """Adopt a new rate (the drive's ramp was read back at arming)."""
        self._rate = rate

    def update(
        self,
        now: Monotonic,
        commanded: MotorRpm,
        measured: MotorRpm | None,
        *,
        following: bool,
    ) -> SpeedEnvelope:
        """Advance to ``now`` towards ``commanded`` and return this tick's band.

        ``following`` is whether the drive is working to the reference at all
        (OPERATION_ENABLED). A non-finite or negative interval moves nothing:
        the follower never runs ahead on a clock it cannot trust.
        """
        expected = self._expected
        if expected is None or (not following and measured is not None):
            expected = float(commanded if measured is None else measured)
        else:
            previous = self._at
            dt = 0.0 if previous is None else float(elapsed(previous, now))
            step = self._rate * dt if math.isfinite(dt) and dt > 0.0 else 0.0
            expected += max(-step, min(step, float(commanded) - expected))
        self._expected = expected
        self._at = now
        low = min(expected, float(commanded))
        high = max(expected, float(commanded))
        return SpeedEnvelope(low=MotorRpm(math.floor(low)), high=MotorRpm(math.ceil(high)))
