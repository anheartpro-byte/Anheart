"""Subprocess fixture: the real ``main()`` of ``scripts/bench_console.py`` over a stand-in drive.

Started by ``tests/test_bench_console_exit.py``, which sends it real signals:

    python tests/bench_console_exit_worker.py <report file> <seconds the stop takes>

Only the drive is a stand-in. The argument parsing, the signal handlers, the
bus thread, the web server and the exit are the script's own, in a process of
its own: what a signal does to a process cannot be shown inside the process
that runs the tests. The stand-in opens no port and holds no lock.

The parent cannot look into this process, so the stand-in writes one word a
line to the report file: ``open`` when the bus thread has connected,
``serving`` when the web server has started (its signal handlers are in place
by then), ``stopping`` when the driver's stop begins and ``stopped`` when it
has finished. The web server listens on a port the system picks: nothing here
needs to reach it.
"""

from __future__ import annotations

import asyncio
import sys
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


class _Report:
    """The words this process leaves for its parent, appended one a line."""

    def __init__(self, path: Path) -> None:
        self._path: Path = path

    def note(self, word: str) -> None:
        with self._path.open("a", encoding="utf-8") as file:
            file.write(f"{word}\n")


class _Drive:
    """What the bus thread asks of the driver, answered from memory. Its stop takes a while."""

    def __init__(self, report: _Report, stop_takes: float) -> None:
        self.link_lost: bool = False
        self._report: _Report = report
        self._stop_takes: float = stop_takes

    async def open(self) -> Result[None, DriveError]:
        self._report.note("open")
        return Ok(None)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return Err(NO_ANSWER)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        return Err(NO_ANSWER)

    async def close(self) -> Result[None, DriveError]:
        self._report.note("stopping")
        await asyncio.sleep(self._stop_takes)
        self._report.note("stopped")
        return Ok(None)


class _Built:
    def __init__(self, drive: _Drive) -> None:
        self.drive: _Drive = drive


def main() -> int:
    report = _Report(Path(sys.argv[1]))
    drive = _Drive(report, float(sys.argv[2]))

    def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
        return cast("BuiltDrive", _Built(drive))

    def serving() -> None:
        report.note("serving")

    pytest.MonkeyPatch().setattr(bench, "build_drive", build)
    bench.app.router.on_startup.append(serving)
    sys.argv = ["bench_console.py", "--port", "COM-NONE", "--http-port", "0"]
    return bench.main()


if __name__ == "__main__":
    sys.exit(main())
