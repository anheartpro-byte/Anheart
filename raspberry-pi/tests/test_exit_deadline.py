"""ANH-191 EX-7: a send stuck on a dead disk must not hold the process once the drive is stopped.

Established by measurement, in a separate process (``tests/exit_deadline_worker.py``):
the real console, its real web server, one archive sent, and a disk that stops
answering before the server has finished with the file. SIGTERM, then the clock.

* without the exit deadline the drive is stopped and the process does not
  leave: the web server's worker thread that is on the disk is not a daemon,
  and the interpreter waits for it (45 s and counting when first measured,
  until it was killed);
* with it, the process is gone at most :data:`~src.local_panel.EXIT_GRACE`
  after the console has stopped, with the exit code it would have had.

In both the order is the same, and it is asserted from the console's own log:
the drive is stopped first, and only then does anything wait.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from src.clock import ManualClock
from src.local_panel import EXIT_GRACE, EXIT_OK
from src.record.codec import Privacy
from src.record.writer import Writer
from src.result import Ok
from src.units import UnixMillis
from tests.record_support import manifest

WORKER: Final[Path] = Path(__file__).with_name("exit_deadline_worker.py")

STOPPED: Final[str] = "console stopped:"
"""What the console logs once the drive is stopped and the session record closed."""

FORCED: Final[str] = "console exit forced"

HELD_FOR: Final[float] = 2.0
"""Seconds past the grace during which the unbounded console is watched still alive."""


@dataclass
class Console:
    """The worker process and where it writes."""

    process: subprocess.Popen[str]
    said: Path
    log: Path

    def logged(self) -> str:
        return self.log.read_text(encoding="utf-8")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]  # pyright: ignore[reportAny]  # the socket API's own
        return port


def closed_record(root: Path) -> str:
    """One finalised record under ``root``, and its name."""
    created = Writer.create(root, manifest(), Privacy())
    assert isinstance(created, Ok)
    writer = created.value
    closed = writer.close(ManualClock(epoch_millis=UnixMillis(1_791_195_072_000)), "x")
    assert isinstance(closed, Ok), closed
    return writer.path.name


def wait_for(condition: str, read: Path, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        if condition in read.read_text(encoding="utf-8"):
            return
        time.sleep(0.02)
    process.kill()
    raise AssertionError(f"never saw {condition!r}; the worker said:\n{read.read_text()}")


def ask_for_archive(port: int, name: str, process: subprocess.Popen[str]) -> socket.socket:
    """Send the request and keep the connection open while the console is asked to stop."""
    link = connect(port, process)
    link.sendall(
        f"GET /api/records/{name}/archive HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
    )
    return link


def connect(port: int, process: subprocess.Popen[str]) -> socket.socket:
    """A connection to the console's web server, once it listens."""
    deadline = time.monotonic() + 30.0
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1.0)
        except OSError:
            if time.monotonic() > deadline or process.poll() is not None:
                process.kill()
                raise
            time.sleep(0.05)


def stuck_console(tmp_path: Path, mode: str) -> tuple[Console, socket.socket]:
    """The console with one export hung on the disk, ready for its SIGTERM."""
    root = tmp_path / "records"
    root.mkdir()
    name = closed_record(root)
    port = free_port()
    said = tmp_path / "worker.out"
    log = tmp_path / "worker.log"
    with said.open("w", encoding="utf-8") as output, log.open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(  # noqa: S603 - fixed interpreter and repository fixture
            [sys.executable, str(WORKER), str(root), str(port), mode],
            stdout=output,
            stderr=errors,
            text=True,
            cwd=WORKER.parent.parent,
        )
    link = ask_for_archive(port, name, process)
    wait_for("STUCK", said, process, 30.0)
    return Console(process=process, said=said, log=log), link


def test_ex7_measured_without_the_deadline_a_send_stuck_on_the_disk_holds_the_exit(
    tmp_path: Path,
) -> None:
    """The measurement: the drive is stopped, and the process stays."""
    console, link = stuck_console(tmp_path, "unbounded")
    process = console.process
    try:
        process.send_signal(signal.SIGTERM)
        wait_for(STOPPED, console.log, process, 30.0)
        stopped_at = time.monotonic()
        time.sleep(float(EXIT_GRACE) + HELD_FOR)
        assert process.poll() is None, "still there, well past the grace the deadline gives"
        held = time.monotonic() - stopped_at
        print(  # noqa: T201 - the measurement, for whoever runs this with -s
            f"\nEX-7 without the deadline: drive stopped, process still alive {held:.1f} s later"
        )
        assert FORCED not in console.logged()
    finally:
        link.close()
        process.kill()
        process.wait(10.0)


def test_ex7_with_the_deadline_the_process_leaves_within_the_grace_once_the_drive_is_stopped(
    tmp_path: Path,
) -> None:
    console, link = stuck_console(tmp_path, "bounded")
    process = console.process
    try:
        process.send_signal(signal.SIGTERM)
        wait_for(STOPPED, console.log, process, 30.0)
        stopped_at = time.monotonic()
        code = process.wait(float(EXIT_GRACE) + 15.0)
        took = time.monotonic() - stopped_at
    finally:
        link.close()
        process.kill()
        process.wait(10.0)
    print(  # noqa: T201 - the measurement, for whoever runs this with -s
        f"\nEX-7 with the deadline: process gone {took:.1f} s after the drive was stopped"
    )
    assert code == EXIT_OK, "the code the console would have exited with"
    # The grace runs from the moment the console has finished stopping, web
    # server included (it gives its own requests 2 s); the rest is a loaded machine.
    assert took <= float(EXIT_GRACE) + 6.0
    logged = console.logged()
    assert FORCED in logged
    # Drive first, then everything else: the forced exit is said after the stop.
    assert logged.index(STOPPED) < logged.index(FORCED)
