"""The bench console's bus thread leaves through the driver's stop, whatever ended it.

``scripts/bench_console.py`` is the operator's bench tool for a bare,
uncoupled motor. One thread owns the drive; on its way out it runs the
driver's own stop (``ATV320Drive.close``: LFRD = 0, the shaft read at
standstill, then the stop words) and releases the port.

An exception raised by an operator action, or by a poll, used to end that
thread WITHOUT the stop: the motor it had enabled was left to the drive's
``ttO`` timeout alone, and the page went on showing the last snapshot as if it
were current. These tests hold that the stop runs on every way out of the
cycle, once, and that the page is told the thread is gone.

The drive here is a stand-in that counts: nothing of this file opens a port.
"""

from __future__ import annotations

from typing import Final, cast

import pytest

import scripts.bench_console as bench
from scripts.bench_console import BusWorker, Command
from scripts.drive_link import BuiltDrive
from src.clock import Clock
from src.local_config import DriveLink, load_local_config
from src.motor.drive import BadResponse, CommTimeout, DriveError, DriveLimits, DriveStatus
from src.result import Err, Ok, Result
from src.units import Seconds

NO_ANSWER: Final[CommTimeout] = CommTimeout(after=Seconds(0.1))

CONFIRMED: Final[Result[None, DriveError]] = Ok(None)
"""The driver's stop when it read the shaft at standstill."""


class _Drive:
    """What the bus thread asks of the driver, answered from memory, the stop counted."""

    def __init__(self, *, stop: Result[None, DriveError]) -> None:
        self.link_lost: bool = False
        self.stops: int = 0
        self.polls: int = 0
        self._stop: Result[None, DriveError] = stop

    async def open(self) -> Result[None, DriveError]:
        return Ok(None)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return Err(NO_ANSWER)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self.polls += 1
        return Err(NO_ANSWER)

    async def close(self) -> Result[None, DriveError]:
        self.stops += 1
        return self._stop


class _Built:
    def __init__(self, drive: _Drive) -> None:
        self.drive: _Drive = drive


def _link() -> DriveLink:
    loaded = load_local_config(
        {
            "MOTOR_BACKEND": "serial",
            "MOTOR_PORT": "COM-NONE",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
            "UI_PORT": "8090",
        }
    )
    assert isinstance(loaded, Ok), loaded
    link = loaded.value.drive_link
    assert link is not None
    return link


def _worker(
    monkeypatch: pytest.MonkeyPatch, *, stop: Result[None, DriveError] = CONFIRMED
) -> tuple[BusWorker, _Drive]:
    """A bus worker over the counting stand-in. Not started: the test runs it itself."""
    drive = _Drive(stop=stop)

    def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
        return cast("BuiltDrive", _Built(drive))

    monkeypatch.setattr(bench, "build_drive", build)
    worker = BusWorker(_link(), max_rpm=300)
    worker.snap.armed = True
    return worker, drive


async def _raising(_self: BusWorker, _command: Command) -> str:
    raise RuntimeError("an action went wrong")


async def _raising_poll(_self: BusWorker) -> None:
    raise OSError("a poll went wrong")


def test_an_action_that_raises_still_leaves_through_the_driver_s_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect: the thread died on the exception and the stop sequence never ran."""
    worker, drive = _worker(monkeypatch)
    monkeypatch.setattr(BusWorker, "_dispatch", _raising)
    worker.submit(Command("speed", 100))

    with pytest.raises(RuntimeError, match="an action went wrong"):
        worker.run()
    snapshot = worker.snap

    assert drive.stops == 1, "the bus thread ended without the driver's stop"
    assert worker.exit_report == "arret confirme, port libere"
    assert snapshot.armed is False
    assert snapshot.link_ok is False
    assert snapshot.status_word is None
    assert snapshot.enabled is None
    assert snapshot.output_rpm is None
    assert snapshot.last_error == "fil de bus arrete: RuntimeError: an action went wrong"


def test_a_poll_that_raises_still_leaves_through_the_driver_s_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not an action this time: the cycle's own read. The same way out."""
    worker, drive = _worker(monkeypatch)
    monkeypatch.setattr(BusWorker, "_poll", _raising_poll)

    with pytest.raises(OSError, match="a poll went wrong"):
        worker.run()

    assert drive.stops == 1
    assert worker.snap.armed is False
    assert worker.snap.last_error == "fil de bus arrete: OSError: a poll went wrong"


def test_a_stop_the_driver_could_not_confirm_is_reported_after_an_exception_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop ran and did not confirm standstill: the exit says so, never "ok"."""
    refused: Result[None, DriveError] = Err(BadResponse(detail="RFRD never read 0"))
    worker, drive = _worker(monkeypatch, stop=refused)
    monkeypatch.setattr(BusWorker, "_dispatch", _raising)
    worker.submit(Command("stop"))

    with pytest.raises(RuntimeError):
        worker.run()

    assert drive.stops == 1
    assert worker.exit_report.startswith("ATTENTION: ")
    assert "RFRD never read 0" in worker.exit_report


def test_an_ordinary_exit_runs_the_driver_s_stop_once_and_blames_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged: asked to stop, the thread leaves through the same stop, once, with no error."""
    worker, drive = _worker(monkeypatch)
    worker.shutdown()

    worker.run()

    assert drive.stops == 1
    assert drive.polls == 0
    assert worker.exit_report == "arret confirme, port libere"
    assert worker.snap.armed is True, "an ordinary exit disarmed the snapshot"
    assert worker.snap.last_error is not None
    assert not worker.snap.last_error.startswith("fil de bus arrete")
