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


def longest_hold(standstill: MotorRpm) -> Seconds:
    """The longest hold any accepted ``COAST_TAU_S`` gives: 2169 s, at 300 s.

    What a hold kept across a restart is capped at, never the cap of the restarted
    console's own ``tau``: a ``COAST_TAU_S`` lowered meanwhile never shortens a hold that
    a process before it started."""
    return hold_cap(MAX_COAST_TAU, standstill)


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
    after one that showed it on starts a hold from the faster of two bounds: the speed
    that read showed, at ``standstill`` or above, plus one read period of the fastest
    ramp; and the largest ``|LFRD|`` read with either or written by this process in
    between (:meth:`referenced`), which the ramp never takes the shaft past however long
    the reads failed between the two (a reference written in one jump, as the bench's
    slider does). A stage this process enabled (:meth:`driven`) is on at rest from then,
    read or not. The first read there ever is, off with LFRD not zero (another master, a
    process that died), holds from ``|LFRD|``: nothing says how fast the shaft was. A
    hold :meth:`expect` asked for starts at the next read that shows the stage off.
    """

    __slots__ = ("_expected", "_on_reference", "_on_rpm", "_read_once", "_standstill", "_tau")

    def __init__(self, *, tau: Seconds, standstill: MotorRpm) -> None:
        self._tau: Seconds = tau
        self._standstill: MotorRpm = standstill
        self._on_rpm: MotorRpm | None = None
        self._on_reference: MotorRpm = MotorRpm(0)
        self._read_once: bool = False
        self._expected: CoastStart | None = None

    @property
    def stage_on(self) -> bool:
        """Whether the last read showed the stage on: it may still be, or drop unseen."""
        return self._on_rpm is not None

    @property
    def may_coast(self) -> bool:
        """Whether the stage dropping now would leave a coast: on since the last read, at a
        speed or towards a reference above standstill. A stage on at rest with LFRD 0 would
        not: the drive holds it at 0 Hz, and nothing else turns it."""
        last = self._on_rpm
        # The two bounds of _seen: a speed read at standstill or above, a reference above it.
        return last is not None and (
            last >= self._standstill or self._on_reference > self._standstill
        )

    @property
    def expected(self) -> Seconds | None:
        """The hold :meth:`expect` asked for that no read has started yet; ``None``: none."""
        return None if self._expected is None else self._expected.hold

    def driven(self) -> None:
        """This process is about to try the words that enable the stage: on, at rest until a
        read says more, whether they land or are answered or not, and whether a read
        follows or not. Only a read that shows the stage off clears it; one read on keeps
        what it showed."""
        if self._on_rpm is None:
            self._on_rpm = MotorRpm(0)
            self._on_reference = MotorRpm(0)

    def referenced(self, rpm: MotorRpm) -> None:
        """A reference this process is about to write, answered or not (one that lands
        with its read-back lost leads there all the same): while the stage may be on, the
        shaft heads for it before any read shows it. Never lowered by a smaller one before
        the next read."""
        if self._on_rpm is not None:
            self._on_reference = MotorRpm(max(self._on_reference, abs(rpm)))

    def kept_after_exit(self, *, confirmed: bool, left: Seconds | None) -> Seconds | None:
        """What the next start must hold from its first read, once nothing reads any more.

        ``left``: the hold still running. A stop at the exit that was not ``confirmed``,
        a stage that :attr:`may_coast`, or a hold no read started yet leave the longest
        coast, from the nameplate: nothing after this says what the stage did.
        """
        if confirmed and not self.may_coast and self._expected is None:
            return left
        parts = (hold_cap(self._tau, self._standstill), left, self.expected)
        return max(part for part in parts if part is not None)

    def expect(self, hold: Seconds, why: str) -> None:
        """A drop no read here saw may have left a coast: the next read starts ``hold`` if it
        shows the stage off, from that read. One that shows the stage on reads the shaft
        itself, and drops it. Never shortened by a shorter one."""
        if self._expected is None or hold > self._expected.hold:
            self._expected = CoastStart(hold, why)

    def note(self, status: DriveStatus, period: Seconds) -> CoastStart | None:
        """One read, ``period`` after the one before it was due; the hold it starts, if any."""
        expected = self._expected
        self._expected = None
        seen = self._seen(status, period)
        if expected is None or self.stage_on or (seen is not None and seen.hold >= expected.hold):
            return seen
        return expected

    def _seen(self, status: DriveStatus, period: Seconds) -> CoastStart | None:
        speed = MotorRpm(abs(status.output_rpm))
        reference = MotorRpm(abs(status.setpoint_echo_rpm))
        first = not self._read_once
        self._read_once = True
        if status.state is DriveState.OPERATION_ENABLED or speed >= self._standstill:
            self._on_rpm = speed
            self._on_reference = reference
            return None
        last = self._on_rpm
        self._on_rpm = None
        if last is not None:
            towards = MotorRpm(max(self._on_reference, reference))
            ramped = (
                coast_hold(last, since_read=period, tau=self._tau, standstill=self._standstill)
                if last >= self._standstill
                else Seconds(0.0)
            )
            hold = max(ramped, self._from(towards))
            if hold == 0.0:
                return None
            return CoastStart(hold, f"the output stage dropped at {last} motor rpm, LFRD {towards}")
        if first and reference != 0:
            why = f"found with the stage off and LFRD at {reference}"
            return CoastStart(self._from(reference), why)
        return None

    def _from(self, speed: MotorRpm) -> Seconds:
        """The hold for a shaft at ``speed`` when the stage drops."""
        return coast_hold(
            speed, since_read=Seconds(0.0), tau=self._tau, standstill=self._standstill
        )
