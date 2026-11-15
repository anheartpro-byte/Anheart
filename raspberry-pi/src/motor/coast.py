"""How long a shaft may still turn once nothing drives it: the coast-down hold (ANH-92).

RFRD, the ATV320's output frequency, reads 0 with the output stage off, a coast
included (docs/securite.md, section 3): nothing the drive reports shows a shaft
freewheeling. So once the stage drops on a shaft last read turning, the console and
the bench tool refuse a start and a fault reset for as long as it may still turn.

The hold is the time a viscous coast, ``rpm(t) = rpm0 exp(-t / tau)``, takes to fall
below the standstill threshold: ``tau ln(rpm0 / standstill)``. Friction only shortens
a real coast, so the hold covers it as long as ``tau`` (``COAST_TAU_S``) is at least
the shaft's own, measured at the bench (docs/securite.md, 9.6). A minimum
deceleration would not: its straight line falls under the coast's long tail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, final

from src.motor.drive import DriveState, DriveStatus
from src.units import MotorRpm, RpmPerSecond, Seconds

PLATE_RPM: Final[MotorRpm] = MotorRpm(1380)
"""The motor's nameplate speed: no shaft of this machine turns faster, so the speed a
hold starts from is clamped to it, and no hold is longer than one from it."""

FASTEST_RAMP: Final[RpmPerSecond] = RpmPerSecond(float(PLATE_RPM) / 4.0)
"""The nameplate in the bench's measured 4 s ramp (``tracking.UNREAD_RAMP``): the most
the drive can have sped the shaft up between a read and the stage dropping."""

DEFAULT_COAST_TAU: Final[Seconds] = Seconds(40.0)
"""``COAST_TAU_S`` unset: twice the simulator's 20 s, nothing having been measured."""

MIN_COAST_TAU: Final[Seconds] = Seconds(5.0)
MAX_COAST_TAU: Final[Seconds] = Seconds(300.0)
"""The bounds ``COAST_TAU_S`` is accepted within (contracts/machine-config.json)."""


def coast_hold(
    speed: MotorRpm, *, since_read: Seconds, tau: Seconds, standstill: MotorRpm
) -> Seconds:
    """The hold for a shaft read at ``speed``, the stage seen dropped ``since_read`` later.

    The shaft may have gained ``since_read`` of the fastest ramp before the drop, and
    is clamped to the nameplate: :func:`hold_cap` is the longest hold there is. A
    shaft that cannot be above ``standstill`` holds nothing.
    """
    reach = min(abs(speed) + FASTEST_RAMP * since_read, float(PLATE_RPM))
    if reach < standstill:
        return Seconds(0.0)
    return Seconds(tau * math.log(reach / standstill))


def hold_cap(tau: Seconds, standstill: MotorRpm) -> Seconds:
    """The longest hold: a coast from the nameplate. 289 s at the default ``tau``."""
    return coast_hold(PLATE_RPM, since_read=Seconds(0.0), tau=tau, standstill=standstill)


@dataclass(frozen=True, slots=True)
class CoastStart:
    """A hold a read starts: how long, and why, for the log."""

    hold: Seconds
    why: str


@final
class CoastWatch:
    """Which reads showed the output stage driving the shaft, and the hold once it drops.

    The stage is on in OPERATION_ENABLED, or wherever RFRD shows motion (a ramp stop, a
    fault reaction that ramps): RFRD is the output frequency. A read with the stage off
    after one that drove the shaft at ``standstill`` or above starts a hold from that
    speed plus one read period of the fastest ramp, however long the link was down
    between the two. The first read there ever is, off with LFRD not zero (another
    master, a process that died), holds from ``|LFRD|``: nothing says how fast the shaft
    was, and the ramp never takes it past its reference.
    """

    __slots__ = ("_on_rpm", "_read_once", "_standstill", "_tau")

    def __init__(self, *, tau: Seconds, standstill: MotorRpm) -> None:
        self._tau: Seconds = tau
        self._standstill: MotorRpm = standstill
        self._on_rpm: MotorRpm | None = None
        self._read_once: bool = False

    def note(self, status: DriveStatus, period: Seconds) -> CoastStart | None:
        """One read, ``period`` after the one before it was due; the hold it starts, if any."""
        speed = MotorRpm(abs(status.output_rpm))
        first = not self._read_once
        self._read_once = True
        if status.state is DriveState.OPERATION_ENABLED or speed >= self._standstill:
            self._on_rpm = speed
            return None
        last = self._on_rpm
        self._on_rpm = None
        if last is not None and last >= self._standstill:
            hold = coast_hold(last, since_read=period, tau=self._tau, standstill=self._standstill)
            return CoastStart(hold, f"the output stage dropped at {last} motor rpm")
        if first and status.setpoint_echo_rpm != 0:
            reference = MotorRpm(abs(status.setpoint_echo_rpm))
            hold = coast_hold(
                reference, since_read=Seconds(0.0), tau=self._tau, standstill=self._standstill
            )
            return CoastStart(hold, f"found with the stage off and LFRD at {reference}")
        return None
