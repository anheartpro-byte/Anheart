"""Subprocess fixture: the real ``main()`` of ``scripts/bench_console.py`` over a stand-in drive.

Started by ``tests/test_bench_console_exit.py``, which sends it real signals:

    python tests/bench_console_exit_worker.py <report file> <stop> [<wait>]

``<stop>`` is how long the driver's stop takes, in seconds, or ``stuck`` for a
stop that never returns: an exchange blocked in a thread of an executor, as a
driver transaction on a dead link would be, which is a thread the interpreter
waits for on its way out. ``<wait>``, when given, replaces the time the script
waits for the stop before it gives up (35 s on the bench).

Only the drive is a stand-in. The argument parsing, the signal handlers, the
bus thread, the web server and the exit are the script's own, in a process of
its own: what a signal does to a process cannot be shown inside the process
that runs the tests. The stand-in opens no port and holds no lock.

The parent cannot look into this process, so the stand-in writes one word a
line to the report file: ``open`` when the bus thread has connected,
``serving`` when the web server has started (its signal handlers are in place
by then), ``served`` when it has closed what it serves, ``stopping`` when the
driver's stop begins and ``stopped`` when it has finished. Once ``main`` is
over it writes ``signals-back`` if each exit signal does again what it did
before ``main``, ``signals-kept`` if not. The report file is the only thing
written that way: the standard output and error of this process are the
script's own, and a test may have closed them. The web server listens on a
port the system picks: nothing here needs to reach it.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from pathlib import Path
from typing import Final, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.bench_console as bench
from scripts.drive_link import BuiltDrive
from src.clock import Clock
from src.local_config import DriveLink
from src.motor.drive import CommTimeout, DriveError, DriveLimits, DriveStatus
from src.result import Err, Ok, Result
from src.units import Seconds

NO_ANSWER: Final[CommTimeout] = CommTimeout(after=Seconds(0.1))

STUCK: Final[str] = "stuck"
"""The ``<stop>`` argument of a stop that never returns."""


class _Report:
    """The words this process leaves for its parent, appended one a line."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def note(self, word: str) -> None:
        with self._path.open("a", encoding="utf-8") as file:
            file.write(f"{word}\n")


class _Drive:
    """What the bus thread asks of the driver, answered from memory. Its stop takes a while."""

    def __init__(self, report: _Report, stop: str) -> None:
        self.link_lost: bool = False
        self._report: _Report = report
        self._stop: str = stop

    async def open(self) -> Result[None, DriveError]:
        self._report.note("open")
        return Ok(None)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return Err(NO_ANSWER)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        return Err(NO_ANSWER)

    async def close(self) -> Result[None, DriveError]:
        self._report.note("stopping")
        if self._stop == STUCK:
            never = threading.Event()
            await asyncio.get_running_loop().run_in_executor(None, never.wait)
        else:
            await asyncio.sleep(float(self._stop))
        self._report.note("stopped")
        return Ok(None)


class _Built:
    def __init__(self, drive: _Drive) -> None:
        self.drive: _Drive = drive


def main() -> int:
    report = _Report(Path(sys.argv[1]))
    drive = _Drive(report, sys.argv[2])
    patch = pytest.MonkeyPatch()

    def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
        return cast(BuiltDrive, _Built(drive))

    def serving() -> None:
        report.note("serving")

    def served() -> None:
        report.note("served")

    patch.setattr(bench, "build_drive", build)
    if len(sys.argv) > 3:
        patch.setattr(bench, "SHUTDOWN_JOIN_S", float(sys.argv[3]))
    bench.app.router.on_startup.append(serving)
    bench.app.router.on_shutdown.append(served)
    sys.argv = ["bench_console.py", "--port", "COM-NONE", "--http-port", "0"]
    watched = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    before = [signal.getsignal(exit_signal) for exit_signal in watched]
    try:
        return bench.main()
    finally:
        after = [signal.getsignal(exit_signal) for exit_signal in watched]
        report.note("signals-back" if after == before else "signals-kept")


if __name__ == "__main__":
    sys.exit(main())
