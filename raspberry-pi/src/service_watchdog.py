"""The console's side of the service manager's watchdog: said by a healthy tick, never waited for.

On an installed Pi the console runs under systemd (``scripts/anheart.service``,
``WatchdogSec=``). systemd expects to hear ``WATCHDOG=1`` at least that often,
and stops and restarts the unit when it does not: a console whose control loop
no longer turns must not stay there looking alive. This module is what says it.

The one rule: a notification is the consequence of a tick that completed
--------------------------------------------------------------------------
:meth:`ServiceWatchdog.tick_completed` is called by the control task, on the
line after the tick returned (:meth:`~src.local_panel.LocalPanel.control_cycle`),
and by nothing else. No timer sends, no other task sends, no thread sends. A
tick that raises never reaches that line; a tick that waits for ever never
reaches it; a loop that no longer runs never runs it. The feeding therefore
stops exactly when the control loop stops completing ticks, which is the only
thing it is evidence of. A watchdog fed from anywhere else would go on being
fed by a process whose control loop is stuck.

What "healthy" means here, and what it does not: the tick ran to its end. It
is a statement about the loop, not about the machine. A console that has
latched a verdict, gone silent on the drive, or is out of service still
completes its ticks and is still fed: it is alive, it shows why it stopped,
and restarting it is an operator's decision, never the watchdog's.

What it costs the tick
----------------------
One datagram on a local socket (the ``sd_notify`` protocol: ``NOTIFY_SOCKET``
names the socket, the payload is a few ASCII bytes). The socket is
non-blocking: the call returns at once whether or not anybody listens. A
datagram that cannot be sent (nobody bound to the path, the receiver's queue
full) is counted and forgotten; the next tick sends its own. Nothing here
retries, sleeps, or raises.

Without ``NOTIFY_SOCKET`` (a development machine, the tests, an image run by
hand) nothing is sent and nothing is counted as a failure. The time of the last
completed tick is kept all the same: :meth:`ServiceWatchdog.stalled` is how
the console's exit knows it was asked to stop while its loop was not turning.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, final

from src.units import Monotonic, Seconds, elapsed

_logger: Final[logging.Logger] = logging.getLogger(__name__)

NOTIFY_SOCKET: Final[str] = "NOTIFY_SOCKET"
"""The environment variable that names the service manager's socket (``sd_notify``)."""

READY: Final[bytes] = b"READY=1\nWATCHDOG=1"
"""The first completed tick: the console is up, and alive."""

ALIVE: Final[bytes] = b"WATCHDOG=1"
"""Every completed tick after it."""

STOPPING: Final[bytes] = b"STOPPING=1"
"""The console has begun its own stop: the time it takes is not a frozen loop."""

type SocketFactory = Callable[[], socket.socket]
"""Makes the datagram socket; the tests pass one that fails."""


@dataclass(frozen=True, slots=True)
class WatchdogStatus:
    """What the feeding has done so far, for the console's page."""

    enabled: bool
    """Whether there is a service manager to tell (``NOTIFY_SOCKET`` was usable)."""

    sent: int
    failed: int
    """Notifications that could not be sent. Each was dropped, none was waited for."""

    last_error: str | None
    """The last failure, as the system named it; ``None`` when there was none."""


def _datagram_socket() -> socket.socket:
    """A local datagram socket that never waits. Raises ``OSError``."""
    made = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    made.setblocking(False)
    return made


def destination(address: str | None) -> str | bytes | None:
    """Where ``NOTIFY_SOCKET`` says to send, or ``None`` when there is nowhere.

    A path, or ``@name`` for a socket of the abstract namespace (a leading NUL
    on the wire). Unset, empty, or anything else: nowhere.
    """
    if not address:
        return None
    if address.startswith("/"):
        return address
    if address.startswith("@") and len(address) > 1:
        return b"\0" + address[1:].encode("utf-8")
    return None


@final
class ServiceWatchdog:
    """Says the control loop is alive, when it is. See the module docstring.

    Mutable, owned by the event loop thread: the control task calls
    :meth:`tick_completed`, the exit path :meth:`stalled` and :meth:`stopping`,
    the page's route :meth:`status`.
    """

    __slots__ = (
        "_failed",
        "_failing",
        "_last_error",
        "_last_tick",
        "_link",
        "_overdue",
        "_ready",
        "_sent",
    )

    def __init__(
        self,
        address: str | None,
        *,
        overdue: Seconds,
        factory: SocketFactory = _datagram_socket,
    ) -> None:
        """``address``: the value of ``NOTIFY_SOCKET``, or ``None``.

        ``overdue``: past this long without a completed tick, the loop is said
        stalled (:meth:`stalled`). The console gives the supervisor's own
        figure for a stall it goes silent on.
        """
        self._overdue: Seconds = overdue
        # The socket and where it sends, together or not at all.
        self._link: tuple[socket.socket, str | bytes] | None = None
        self._last_error: str | None = None
        self._sent: int = 0
        self._failed: int = 0
        self._failing: bool = False
        self._ready: bool = False
        self._last_tick: Monotonic | None = None
        where = destination(address)
        if where is None:
            if address:
                self._last_error = "NOTIFY_SOCKET is neither a path nor an abstract name"
                _logger.error("service watchdog: %s; nothing will be sent", self._last_error)
            return
        try:
            self._link = (factory(), where)
        except OSError as error:
            self._last_error = type(error).__name__
            _logger.exception("service watchdog: no socket; nothing will be sent")

    def tick_completed(self, now: Monotonic) -> None:
        """A control tick ran to its end at ``now``: say so. Never waits, never raises.

        The only place a liveness notification is sent from. The first one
        that gets through also says the console is ready; until one does, it
        is said again at each tick.
        """
        self._last_tick = now
        if self._send(ALIVE if self._ready else READY):
            self._ready = True

    def stopping(self) -> None:
        """The console is stopping by itself: the stop is not a stall. Best effort."""
        self._send(STOPPING)

    def stalled(self, now: Monotonic) -> bool:
        """Whether no tick has completed for longer than the stall bound.

        ``False`` before the first tick: a console that is still starting has
        not stalled.
        """
        last = self._last_tick
        return last is not None and elapsed(last, now) > self._overdue

    def status(self) -> WatchdogStatus:
        """What was sent and what was not."""
        return WatchdogStatus(
            enabled=self._link is not None,
            sent=self._sent,
            failed=self._failed,
            last_error=self._last_error,
        )

    def _send(self, state: bytes) -> bool:
        """One datagram, or nothing. Returns whether it left."""
        link = self._link
        if link is None:
            return False
        sender, where = link
        try:
            sender.sendto(state, where)
        except OSError as error:
            # Nobody bound to the path, or a receiver that reads nothing: the
            # service manager will draw its own conclusion. Said once per run
            # of failures, not five times a second.
            self._failed += 1
            self._last_error = type(error).__name__
            if not self._failing:
                self._failing = True
                _logger.warning("service watchdog: notification not sent (%s)", error)
            return False
        self._sent += 1
        if self._failing:
            self._failing = False
            _logger.warning("service watchdog: notifications are sent again")
        return True
