"""ANH-162 EX-1: what the console tells the service manager, and what it can cost.

The notifications are read here on a real local datagram socket, the kind
systemd listens on. Three things are pinned:

* the protocol: the first completed tick says ready and alive, every one
  after it alive, the console's own stop says stopping;
* the cost: a notification nobody takes is dropped and counted, at once,
  whether nobody listens or the listener's queue is full. Nothing is waited
  for and nothing is raised;
* without a service manager nothing is sent, and that is not a failure.

That a notification is only ever the consequence of a completed tick is the
console's wiring: ``tests/test_supervised_console.py``.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import Final

import pytest

from src.clock import RealClock
from src.service_watchdog import (
    ALIVE,
    READY,
    STOPPING,
    ServiceWatchdog,
    datagram_socket,
    destination,
)
from src.units import Monotonic, Seconds, elapsed
from tests.notify_support import Listener

OVERDUE: Final[Seconds] = Seconds(3.0)


def test_ex1_the_first_completed_tick_says_ready_and_alive_and_the_next_ones_alive(
    listener: Listener,
) -> None:
    watchdog = ServiceWatchdog(str(listener.path), overdue=OVERDUE)
    assert listener.heard() == [], "building the console tells the service manager nothing"
    watchdog.tick_completed(Monotonic(10.0))
    watchdog.tick_completed(Monotonic(10.2))
    watchdog.tick_completed(Monotonic(10.4))
    assert listener.heard() == [READY, ALIVE, ALIVE]
    assert READY == b"READY=1\nWATCHDOG=1"
    assert ALIVE == b"WATCHDOG=1"
    status = watchdog.status()
    assert status.enabled
    assert (status.sent, status.failed, status.last_error) == (3, 0, None)


def test_ex1_the_console_says_when_it_stops_by_itself(listener: Listener) -> None:
    watchdog = ServiceWatchdog(str(listener.path), overdue=OVERDUE)
    watchdog.tick_completed(Monotonic(10.0))
    watchdog.stopping()
    assert listener.heard() == [READY, STOPPING]
    assert STOPPING == b"STOPPING=1"


def test_ex1_without_a_service_manager_nothing_is_sent_and_nothing_is_a_failure() -> None:
    for address in (None, ""):
        watchdog = ServiceWatchdog(address, overdue=OVERDUE)
        watchdog.tick_completed(Monotonic(10.0))
        watchdog.stopping()
        status = watchdog.status()
        assert not status.enabled
        assert (status.sent, status.failed, status.last_error) == (0, 0, None)


def test_ex1_a_notification_nobody_listens_to_is_dropped_counted_and_never_raised(
    short_directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Nobody is bound to the path yet: the relay of the unit has not started, or has died."""
    path = short_directory / "notify"
    watchdog = ServiceWatchdog(str(path), overdue=OVERDUE)
    with caplog.at_level(logging.WARNING):
        for tick in range(5):
            watchdog.tick_completed(Monotonic(10.0 + tick))
    status = watchdog.status()
    assert status.enabled
    assert (status.sent, status.failed) == (0, 5)
    assert status.last_error == "FileNotFoundError"
    # Said once for the whole run of failures: the tick runs five times a second.
    assert caplog.text.count("notification not sent") == 1

    listener = Listener(short_directory)
    try:
        with caplog.at_level(logging.WARNING):
            watchdog.tick_completed(Monotonic(16.0))
            watchdog.tick_completed(Monotonic(16.2))
        # Ready was never taken: it is said again, then alive.
        assert listener.heard() == [READY, ALIVE]
    finally:
        listener.close()
    assert watchdog.status().sent == 2
    assert caplog.text.count("sent again") == 1


def test_ex1_the_socket_the_notifications_leave_by_never_waits() -> None:
    """Said of the socket itself, so that one that would wait fails here, by name, and
    not as a test that hangs (Linux) or passes all the same (macOS drops instead)."""
    made = datagram_socket()
    try:
        assert made.getblocking() is False, "a blocking socket would hold the control tick"
        assert made.type == socket.SOCK_DGRAM
        assert made.family == socket.AF_UNIX
    finally:
        made.close()


def test_ex1_a_listener_that_reads_nothing_never_makes_the_tick_wait(listener: Listener) -> None:
    """The receiver's queue fills: the send returns at once, and what does not fit is dropped."""
    used: list[socket.socket] = []

    def remembered() -> socket.socket:
        used.append(datagram_socket())
        return used[0]

    watchdog = ServiceWatchdog(str(listener.path), overdue=OVERDUE, factory=remembered)
    # Before a single send: on a socket that waits, the loop below would not come back.
    assert used[0].getblocking() is False, "the watchdog's socket would hold the control tick"
    clock = RealClock()
    began = clock.monotonic()
    attempts = 20_000
    for tick in range(attempts):
        watchdog.tick_completed(Monotonic(10.0 + tick))
    spent = elapsed(began, clock.monotonic())
    status = watchdog.status()
    assert status.failed > 0, "the queue of a local datagram socket is bounded"
    assert status.sent + status.failed == attempts
    # 20 000 sends, most of them refused: a send that waited even a millisecond
    # for room would take 20 s here.
    assert spent < 5.0, f"{attempts} notifications took {spent:.1f} s"


def test_ex1_a_socket_that_cannot_be_made_costs_the_notifications_not_the_console(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refused() -> socket.socket:
        raise OSError(24, "too many open files")

    with caplog.at_level(logging.ERROR):
        watchdog = ServiceWatchdog("/run/anheart/notify", overdue=OVERDUE, factory=refused)
    watchdog.tick_completed(Monotonic(10.0))
    status = watchdog.status()
    assert not status.enabled
    assert (status.sent, status.failed, status.last_error) == (0, 0, "OSError")
    assert "nothing will be sent" in caplog.text


@pytest.mark.parametrize(
    ("address", "where"),
    [
        (None, None),
        ("", None),
        ("/run/systemd/notify", "/run/systemd/notify"),
        ("@anheart/notify", b"\0anheart/notify"),
        ("@", None),
        ("run/notify", None),
        ("vsock:2:1234", None),
    ],
)
def test_ex1_only_a_path_or_an_abstract_name_is_somewhere_to_send(
    address: str | None, where: str | bytes | None
) -> None:
    found = destination(address)
    assert found == where


def test_ex1_an_address_that_is_neither_is_said_and_nothing_is_sent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        watchdog = ServiceWatchdog("vsock:2:1234", overdue=OVERDUE)
    watchdog.tick_completed(Monotonic(10.0))
    status = watchdog.status()
    assert not status.enabled
    assert status.sent == 0
    assert status.last_error is not None
    assert "neither a path nor an abstract name" in caplog.text


def test_ex1_a_loop_is_stalled_past_the_bound_since_its_last_completed_tick() -> None:
    watchdog = ServiceWatchdog(None, overdue=OVERDUE)
    assert not watchdog.stalled(Monotonic(1_000.0)), "a console still starting has not stalled"
    watchdog.tick_completed(Monotonic(10.0))
    assert not watchdog.stalled(Monotonic(10.2))
    assert not watchdog.stalled(Monotonic(13.0)), "the bound itself is not past the bound"
    assert watchdog.stalled(Monotonic(13.1))
    # A tick that comes back AT the bound is on time: nothing is remembered.
    watchdog.tick_completed(Monotonic(13.0))
    assert not watchdog.stalled(Monotonic(13.2))


def test_ex2_a_tick_that_comes_back_late_is_remembered_whatever_completes_afterwards(
    listener: Listener, caplog: pytest.LogCaptureFixture
) -> None:
    """The freeze that the stop itself ends: the loop wakes, one tick completes, and only
    then is the stop handled. That tick must not make the stop look like a clean one."""
    watchdog = ServiceWatchdog(str(listener.path), overdue=OVERDUE)
    watchdog.tick_completed(Monotonic(10.0))
    watchdog.tick_completed(Monotonic(10.2))
    with caplog.at_level(logging.ERROR):
        watchdog.tick_completed(Monotonic(20.29))  # 10.09 s later: the process was frozen
    assert watchdog.stalled(Monotonic(20.3)), "the late tick is the evidence, not its erasure"
    assert "10.09 s between two completed ticks" in caplog.text
    assert "will not leave as a clean stop" in caplog.text
    # It is a completed tick all the same, and was notified as one: the feed does not lie.
    assert listener.heard() == [READY, ALIVE, ALIVE]
    # Healthy ticks afterwards, for as long as one likes: nothing unsays it.
    for tick in range(1, 3_000):
        watchdog.tick_completed(Monotonic(20.29 + 0.2 * tick))
        assert listener.heard() == [ALIVE], "and each of them is notified, as before"
    assert watchdog.stalled(Monotonic(20.29 + 0.2 * 3_000))
    assert watchdog.status().failed == 0
    # Said once per freeze that is longer than any before it, not at every tick.
    assert caplog.text.count("between two completed ticks") == 1
    with caplog.at_level(logging.ERROR):
        watchdog.tick_completed(Monotonic(700.0))  # another freeze, longer than the first
        watchdog.tick_completed(Monotonic(1_000.0))  # a longer one: 300 s
        watchdog.tick_completed(Monotonic(1_004.0))  # 4 s: late, and shorter than both
    assert caplog.text.count("between two completed ticks") == 3
    assert watchdog.stalled(Monotonic(1_004.2))


def test_ex2_ticks_that_come_back_on_time_are_never_remembered_as_late() -> None:
    watchdog = ServiceWatchdog(None, overdue=OVERDUE)
    for tick in range(5_000):
        watchdog.tick_completed(Monotonic(10.0 + 2.9 * tick))  # slow, and inside the bound
    assert not watchdog.stalled(Monotonic(10.0 + 2.9 * 4_999 + 2.9))
