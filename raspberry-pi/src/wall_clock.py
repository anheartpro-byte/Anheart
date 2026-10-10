"""Whether the wall clock can be believed, and within what error.

The monotonic clock rules everything that decides (:mod:`src.clock`): durations,
dwell times, the safety supervisor. The wall clock only stamps what leaves the
machine: a record's ``started_at``, a logbook line, what the dashboard is sent.
A Raspberry Pi has no battery-backed clock. After a power cut it restarts on
the time of its last shutdown, and is wrong by however long it was off until
its time service has reached the network again.

Nothing here corrects the clock, and nothing here is read by a timing decision.
This module answers one question, for whoever stamps a record or arms a session
with a person on board: **is a time service keeping the wall clock right now,
and within what error?**

The answer is the kernel's own (``adjtimex(2)``, asked to change nothing): the
question ``timedatectl`` asks before it prints ``System clock synchronized``,
judged by the same rule. The Pi's time service (chrony, set up by
``scripts/install.sh``) publishes there whether it is synchronised, the error
it estimates and the error it guarantees. Reading it opens no file, no socket
and no device, and waits for nothing.

Three pieces:

* :class:`KernelClock`, the production :class:`WallClockProbe`, on the systems
  that can be asked (:func:`system_probe`: Linux, so the Pi);
* :class:`WallClockWatch`, the last reading, in memory. The console refreshes
  it from a task of its own (never from the control tick); the arming gates,
  the session record and the page only read what it holds;
* the words the operator reads (:func:`label_of`, :func:`detail_of`).

A probe that fails is an unreadable clock, which is not a trusted one: it never
raises into its caller. See ``docs/raspberry-pi.md``, section 12.4.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, assert_never, cast, final

from src.units import Seconds

_logger: Final[logging.Logger] = logging.getLogger(__name__)

MICROSECONDS: Final[int] = 1_000_000

UNSYNCHRONISED_FROM: Final[int] = 16 * MICROSECONDS
"""The kernel's ``maxerror``, in microseconds, of a clock nobody keeps.

It starts there at boot, a time service that loses its sources puts it back
there, and it creeps back there by itself (500 ppm) when a time service stops
publishing. Below it, a time service answers for the clock. ``timedatectl``
applies this rule and no other; the ``STA_UNSYNC`` flag is left out on
purpose, as it is there, because a time service may hold it set for another
reason while it is synchronised."""

TIMEX_BYTES: Final[int] = 256
"""Room for the kernel's ``struct timex`` (208 bytes on a 64-bit Linux), zeroed:
its first field, ``modes``, is then 0, which asks for nothing to be changed."""

_LONG_BYTES: Final[int] = 8
_MAX_ERROR_AT: Final[int] = 24
_ESTIMATED_ERROR_AT: Final[int] = 32
"""Where ``maxerror`` and ``esterror`` sit in ``struct timex`` on a 64-bit Linux
(arm64, x86-64), the only systems the console's image is built for: an
``unsigned int`` and its padding, then ``long offset``, ``long freq``,
``long maxerror``, ``long esterror``."""


# =========================================================================
# What a reading says
# =========================================================================


@dataclass(frozen=True, slots=True)
class Synchronised:
    """A time service keeps the clock, and answers for a bounded error."""

    estimated_error: Seconds | None
    """How far the time service estimates the clock to be from its source.

    ``None`` when it published no estimate: a zero is never taken for a
    measurement. A magnitude, not a signed offset."""

    max_error: Seconds
    """The error the time service guarantees not to be exceeded."""


@dataclass(frozen=True, slots=True)
class Unsynchronised:
    """Nobody answers for the clock: no time source reached since the Pi started,
    or none for too long. Its time may be wrong by however long the Pi was off."""


@dataclass(frozen=True, slots=True)
class Unreadable:
    """The state could not be read at all. Not a trusted clock either."""

    detail: str


type WallClockState = Synchronised | Unsynchronised | Unreadable
"""Closed. Only :class:`Synchronised` is a clock to believe."""


def is_trusted(state: WallClockState) -> bool:
    """Whether ``state`` is a wall clock a record may be stamped with on its word."""
    return isinstance(state, Synchronised)


def clock_offset_s(state: WallClockState) -> Seconds | None:
    """The estimated error of the wall clock, in seconds, when one is known.

    The figure the machine's health report is to carry (``clock_offset_s``).
    ``None`` is unknown, never zero."""
    return state.estimated_error if isinstance(state, Synchronised) else None


def state_name(state: WallClockState) -> str:
    """The wire name of ``state``, for the page."""
    match state:
        case Synchronised():
            return "synchronised"
        case Unsynchronised():
            return "unsynchronised"
        case Unreadable():
            return "unreadable"
    raise assert_never(state)


def label_of(state: WallClockState) -> str:
    """The word the page shows for ``state``."""
    match state:
        case Synchronised():
            return "synchronisee"
        case Unsynchronised():
            return "NON FIABLE"
        case Unreadable():
            return "NON VERIFIEE"
    raise assert_never(state)


def _milliseconds(value: Seconds) -> str:
    return f"{value * 1000:.1f} ms"


def detail_of(state: WallClockState) -> str:
    """Why, in one line of plain text for the operator."""
    match state:
        case Synchronised():
            estimated = state.estimated_error
            bound = f"erreur au plus {_milliseconds(state.max_error)}"
            if estimated is None:
                return bound
            return f"erreur estimee {_milliseconds(estimated)}, {bound}"
        case Unsynchronised():
            return (
                "aucune source de temps jointe : l'heure peut etre fausse de la duree "
                "pendant laquelle le Pi etait eteint"
            )
        case Unreadable():
            return f"etat de l'horloge illisible ({state.detail})"
    raise assert_never(state)


# =========================================================================
# Reading it
# =========================================================================


class WallClockProbe(Protocol):
    """Anything that can say what the wall clock is worth, now."""

    @abstractmethod
    def read(self) -> WallClockState:
        """The state of the wall clock. Returns at once: no file, no socket, no device."""


type Adjtimex = Callable[[ctypes.Array[ctypes.c_char]], int]
"""``adjtimex(2)`` on a zeroed ``struct timex``: fills it, returns the clock state
(0 to 5) or -1. Raises ``AttributeError`` or ``OSError`` where there is no such call."""


def libc_adjtimex(buffer: ctypes.Array[ctypes.c_char]) -> int:
    """The C library's ``adjtimex`` on ``buffer``. Linux only: elsewhere the name is missing.

    A foreign call has no static type: it returns a C ``int``, narrowed here.
    """
    return cast("int", ctypes.CDLL(None).adjtimex(buffer))


def judge(max_error_us: int, estimated_error_us: int) -> WallClockState:
    """What the kernel's two figures, in microseconds, say of the clock."""
    if not 0 <= max_error_us < UNSYNCHRONISED_FROM:
        return Unsynchronised()
    estimated = (
        Seconds(estimated_error_us / MICROSECONDS)
        if 0 < estimated_error_us < UNSYNCHRONISED_FROM
        else None
    )
    return Synchronised(estimated_error=estimated, max_error=Seconds(max_error_us / MICROSECONDS))


def _long_at(raw: bytes, at: int) -> int:
    return int.from_bytes(raw[at : at + _LONG_BYTES], sys.byteorder, signed=True)


@final
class KernelClock:
    """The kernel's account of the wall clock: ``adjtimex(2)``, changing nothing.

    One system call that copies a few kernel variables: it takes no file, no
    socket and no device, and it needs no privilege. The kernel's clock state
    is the machine's, not the container's: the console reads the Pi's from
    inside its image.
    """

    __slots__ = ("_call",)

    def __init__(self, call: Adjtimex = libc_adjtimex) -> None:
        self._call: Adjtimex = call

    def read(self) -> WallClockState:
        """The clock's state, or :class:`Unreadable` where the kernel cannot be asked."""
        buffer = ctypes.create_string_buffer(TIMEX_BYTES)
        try:
            code = self._call(buffer)
        except (AttributeError, OSError) as error:
            return Unreadable(f"adjtimex indisponible sur ce systeme : {type(error).__name__}")
        if code < 0:
            return Unreadable("adjtimex refuse par le systeme")
        raw = buffer.raw
        return judge(_long_at(raw, _MAX_ERROR_AT), _long_at(raw, _ESTIMATED_ERROR_AT))


def system_probe(platform: str = sys.platform) -> WallClockProbe | None:
    """The probe of this system, or ``None`` where the system cannot be asked.

    Linux, so the Pi: the kernel's. Anywhere else (the macOS workstation of the
    bench and of development) there is no such call, and the console then
    judges nothing about its clock rather than refuse, on every start with a
    person on board, for want of a question it has no way to ask. The Pi is
    where a person is on board; a failure to read the clock THERE is
    :class:`Unreadable`, and refuses.
    """
    return KernelClock() if platform == "linux" else None


# =========================================================================
# The last reading
# =========================================================================


@final
class WallClockWatch:
    """The last reading of a probe, kept in memory for everyone who asks.

    :meth:`refresh` is the only call that reaches the probe: once here, when
    the console is built (startup, nothing turning), then from the console's
    own task. Everyone else calls :meth:`current`, which reads an attribute.

    :meth:`refresh` is total. A probe is an observer of the machine: one that
    raises gives an unreadable clock, never an exception in the console.
    """

    __slots__ = ("_probe", "_state")

    def __init__(self, probe: WallClockProbe) -> None:
        self._probe: WallClockProbe = probe
        self._state: WallClockState = self._read()
        self._say()

    def current(self) -> WallClockState:
        """The last reading. Memory only."""
        return self._state

    def refresh(self) -> WallClockState:
        """Ask the probe again, and say in the log when the clock's standing changed."""
        before = self._state
        self._state = self._read()
        if type(self._state) is not type(before):
            self._say()
        return self._state

    def _read(self) -> WallClockState:
        try:
            return self._probe.read()
        except Exception as error:  # an observer: a probe that breaks must not stop the console
            return Unreadable(f"lecture en echec : {type(error).__name__}")

    def _say(self) -> None:
        """One line in the log: a warning unless the clock is one to believe."""
        state = self._state
        words = f"{state_name(state)} ({detail_of(state)})"
        if is_trusted(state):
            _logger.info("wall clock: %s", words)
        else:
            _logger.warning("wall clock: %s", words)
