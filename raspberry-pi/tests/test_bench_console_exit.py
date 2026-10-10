"""Every way out of the bench console that Python gets to see ends in the driver's stop.

``scripts/bench_console.py`` is the operator's bench tool for a bare,
uncoupled motor. One thread owns the drive; on its way out it runs the
driver's own stop (``ATV320Drive.close``: LFRD = 0, the shaft read at
standstill, then the stop words) and releases the port.

Three ways out went round that stop, each leaving a motor that had been
enabled to the drive's ``ttO`` timeout alone:

* an exception raised by an operator action or by a poll ended the bus thread
  without it, and the page went on showing the last snapshot as if it were
  current;
* SIGTERM ended the PROCESS without it: the web server takes the signal, shuts
  itself down, then hands the signal back to whatever handled it before, which
  by default ends the process on the spot, before any ``finally``;
* SIGHUP did the same without even the web server noticing.

And when the stop itself raised, the exit said ``-``, as if no stop had been
attempted.

The first group of tests runs the bus thread in this process, over a stand-in
drive that counts. The second sends real signals to the real ``main()``, in a
process of its own (``tests/bench_console_exit_worker.py``): what a signal
does to a process cannot be shown from inside the process that runs the tests.
Nothing in this file opens a port.

What no test here can cover, because no code runs then: SIGKILL, the power
going, the interpreter crashing. The drive's own communication timeout is the
net for those, and the script says so.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest

import scripts.bench_console as bench
from scripts.drive_link import BuiltDrive
from src.clock import Clock
from src.local_config import DriveLink, load_local_config
from src.motor.atv320 import ATV320Drive, ModbusMaster
from src.motor.drive import BadResponse, CommTimeout, DriveError, DriveLimits, DriveStatus
from src.result import Err, Ok, Result
from src.units import Seconds

NO_ANSWER: Final[CommTimeout] = CommTimeout(after=Seconds(0.1))

CONFIRMED: Final[Result[None, DriveError]] = Ok(None)
"""The driver's stop when it read the shaft at standstill."""

WORKER: Final[Path] = Path(__file__).with_name("bench_console_exit_worker.py")

PATIENCE: Final[float] = 30.0
"""Seconds a step of the other process is waited for before the test gives up on it."""

BY_SIGTERM: Final[int] = 128 + int(signal.SIGTERM)
"""The exit status of a process that left because it was asked to terminate."""


def _hang_up() -> signal.Signals:
    """SIGHUP where it exists. Windows has no such signal, and skips the tests that send one."""
    if sys.platform == "win32":
        return signal.SIGTERM
    return signal.SIGHUP


HANG_UP: Final[signal.Signals] = _hang_up()
"""The terminal going away."""


# =========================================================================
# The bus thread, in this process
# =========================================================================


class _Drive:
    """What the bus thread asks of the driver, answered from memory, the stop counted."""

    def __init__(self, *, stop: Result[None, DriveError] | BaseException) -> None:
        self.link_lost: bool = False
        self.stops: int = 0
        self.polls: int = 0
        self._stop: Result[None, DriveError] | BaseException = stop

    async def open(self) -> Result[None, DriveError]:
        return Ok(None)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return Err(NO_ANSWER)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self.polls += 1
        return Err(NO_ANSWER)

    async def close(self) -> Result[None, DriveError]:
        self.stops += 1
        stop = self._stop
        if isinstance(stop, BaseException):
            raise stop
        return stop


class _Master:
    """The transport under the driver: closing it is what lets go of the port and of its lock."""

    def __init__(self) -> None:
        self.closed: int = 0

    def close(self) -> None:
        self.closed += 1


@dataclass(frozen=True, slots=True)
class _Rig:
    worker: bench.BusWorker
    drive: _Drive
    master: _Master


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


def _rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stop: Result[None, DriveError] | BaseException = CONFIRMED,
) -> _Rig:
    """A bus worker over the counting stand-ins. Not started: the test runs it itself.

    The record that holds the driver and its transport is the script's own,
    so that letting go of the port is its real ``release_port``.
    """
    drive = _Drive(stop=stop)
    master = _Master()

    def build(_link: DriveLink, _clock: Clock) -> BuiltDrive:
        return BuiltDrive(drive=cast("ATV320Drive", drive), master=cast("ModbusMaster", master))

    monkeypatch.setattr(bench, "build_drive", build)
    worker = bench.BusWorker(_link(), max_rpm=300)
    worker.snap.armed = True
    return _Rig(worker=worker, drive=drive, master=master)


async def _raising(_self: bench.BusWorker, _command: bench.Command) -> str:
    raise RuntimeError("an action went wrong")


async def _raising_poll(_self: bench.BusWorker) -> None:
    raise OSError("a poll went wrong")


def test_an_action_that_raises_still_leaves_through_the_driver_s_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect: the thread died on the exception and the stop sequence never ran."""
    rig = _rig(monkeypatch)
    worker = rig.worker
    monkeypatch.setattr(bench.BusWorker, "_dispatch", _raising)
    worker.submit(bench.Command("speed", 100))

    with pytest.raises(RuntimeError, match="an action went wrong"):
        worker.run()
    snapshot = worker.snap

    assert rig.drive.stops == 1, "the bus thread ended without the driver's stop"
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
    rig = _rig(monkeypatch)
    worker = rig.worker
    monkeypatch.setattr(bench.BusWorker, "_poll", _raising_poll)

    with pytest.raises(OSError, match="a poll went wrong"):
        worker.run()

    assert rig.drive.stops == 1
    assert worker.snap.armed is False
    assert worker.snap.last_error == "fil de bus arrete: OSError: a poll went wrong"


def test_a_stop_the_driver_could_not_confirm_is_reported_after_an_exception_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop ran and did not confirm standstill: the exit says so, never "ok"."""
    refused: Result[None, DriveError] = Err(BadResponse(detail="RFRD never read 0"))
    rig = _rig(monkeypatch, stop=refused)
    worker = rig.worker
    monkeypatch.setattr(bench.BusWorker, "_dispatch", _raising)
    worker.submit(bench.Command("stop"))

    with pytest.raises(RuntimeError):
        worker.run()

    assert rig.drive.stops == 1
    assert worker.exit_report.startswith("ATTENTION: ")
    assert "RFRD never read 0" in worker.exit_report


def test_an_ordinary_exit_runs_the_driver_s_stop_once_and_blames_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged: asked to stop, the thread leaves through the same stop, once, with no error.

    The driver let go of the port itself, as the last step of its stop:
    nothing closes the transport behind its back.
    """
    rig = _rig(monkeypatch)
    worker = rig.worker
    worker.shutdown()

    worker.run()

    assert rig.drive.stops == 1
    assert rig.drive.polls == 0
    assert rig.master.closed == 0
    assert worker.exit_report == "arret confirme, port libere"
    assert worker.snap.armed is True, "an ordinary exit disarmed the snapshot"
    assert worker.snap.last_error is not None
    assert not worker.snap.last_error.startswith("fil de bus arrete")


def test_a_stop_that_raises_is_reported_as_it_is_and_the_port_is_let_go_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop itself raised: the exit said "-", as if no stop had been attempted.

    Nothing was confirmed, and the report says what was raised. The driver
    releases the port, and the lock on the drive cable with it, as the last
    step of a stop it did not finish, and it will not try again; so the
    transport is closed directly, once.
    """
    rig = _rig(monkeypatch, stop=RuntimeError("the port vanished"))
    worker = rig.worker
    worker.shutdown()

    with pytest.raises(RuntimeError, match="the port vanished"):
        worker.run()

    assert rig.drive.stops == 1
    assert rig.master.closed == 1, "the port, and the lock on the cable, were left held"
    assert worker.exit_report.startswith(
        "ATTENTION: l'arret du pilote a leve RuntimeError: the port vanished."
    )
    assert "Arret NON confirme" in worker.exit_report
    assert "ttO" in worker.exit_report


def test_a_stop_that_raises_after_an_exception_of_the_cycle_is_still_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failures in a row, the cycle's then the stop's: the report is the stop's."""
    rig = _rig(monkeypatch, stop=OSError("write failed"))
    worker = rig.worker
    monkeypatch.setattr(bench.BusWorker, "_poll", _raising_poll)

    with pytest.raises(OSError, match="write failed"):
        worker.run()

    assert rig.drive.stops == 1
    assert rig.master.closed == 1
    assert worker.exit_report.startswith("ATTENTION: l'arret du pilote a leve OSError: ")
    assert worker.snap.last_error == "fil de bus arrete: OSError: a poll went wrong"


def test_the_help_says_what_no_exit_path_can_cover(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """SIGKILL cannot be caught: the operator is told what the net is, where the tool is asked."""
    monkeypatch.setattr(sys, "argv", ["bench_console.py", "--help"])
    with pytest.raises(SystemExit) as left:
        bench.main()
    shown = " ".join(capsys.readouterr().out.split())

    assert left.value.code == 0
    assert "SIGKILL (kill -9)" in shown
    assert "delai de communication du variateur (ttO)" in shown
    assert "SIGTERM" in shown
    assert "SIGHUP" in shown


# =========================================================================
# Real signals, sent to the real main() in a process of its own
# =========================================================================

signals_reach_a_process = pytest.mark.skipif(
    sys.platform == "win32", reason="no SIGHUP, and no signal delivered to a process, on Windows"
)


@dataclass(frozen=True, slots=True)
class _Console:
    """The bench console running in its own process, and what it leaves behind."""

    process: subprocess.Popen[bytes]
    report: Path
    log: Path

    def words(self) -> list[str]:
        """What the stand-in drive has written so far, one word a line."""
        if not self.report.exists():
            return []
        return self.report.read_text(encoding="utf-8").split()

    def wait_for(self, word: str) -> None:
        """Until the stand-in has written ``word``. Fails if the process ends, or takes too long."""
        deadline = time.monotonic() + PATIENCE
        while word not in self.words():
            ended = self.process.poll()
            now = time.monotonic()
            assert ended is None, f"the console ended ({ended}) before {word!r}: {self.said()}"
            assert now < deadline, f"no {word!r} after {PATIENCE} s: {self.said()}"
            time.sleep(0.02)

    def ended(self) -> int:
        """The exit status, once the process has ended by itself."""
        return self.process.wait(timeout=PATIENCE)

    def said(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


def _console(tmp_path: Path, *, stop_takes: float = 0.0) -> _Console:
    """Start the real ``main()`` over the stand-in drive. The caller ends it with a signal."""
    report = tmp_path / "report.txt"
    log = tmp_path / "console.log"
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
            [sys.executable, str(WORKER), str(report), str(stop_takes)],
            cwd=WORKER.parent.parent,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    return _Console(process=process, report=report, log=log)


def _kill_if_left(console: _Console) -> None:
    """A test that failed must not leave its console running behind it."""
    if console.process.poll() is None:
        console.process.kill()
        console.process.wait(timeout=PATIENCE)


@signals_reach_a_process
@pytest.mark.parametrize(
    ("sent", "status"),
    [
        pytest.param(signal.SIGINT, 0, id="Ctrl+C"),
        pytest.param(signal.SIGTERM, BY_SIGTERM, id="SIGTERM"),
        pytest.param(HANG_UP, BY_SIGTERM, id="SIGHUP"),
    ],
)
def test_a_signal_to_the_serving_console_ends_in_the_driver_s_stop_once(
    tmp_path: Path, sent: signal.Signals, status: int
) -> None:
    """The console is serving its page when the signal arrives. One stop, then the process ends.

    Ctrl+C always ended this way. SIGTERM and SIGHUP ended the process with no
    stop at all. SIGHUP takes SIGTERM's road, hence its exit status.
    """
    console = _console(tmp_path)
    try:
        console.wait_for("serving")
        console.process.send_signal(sent)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()
    said = console.said()

    assert words.count("stopping") == 1, f"the driver's stop ran {words.count('stopping')} times"
    assert words[-1] == "stopped", "the process ended before the driver's stop had finished"
    assert code == status, said
    assert "sortie: arret confirme, port libere" in said


@signals_reach_a_process
def test_a_sigterm_during_the_start_up_wait_ends_in_the_driver_s_stop_too(tmp_path: Path) -> None:
    """Before the web server exists: the bus thread is connected, ``main`` is still starting."""
    console = _console(tmp_path)
    try:
        console.wait_for("open")
        console.process.send_signal(signal.SIGTERM)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()
    said = console.said()

    assert "serving" not in words, "the web server was already up: not this case"
    assert words.count("stopping") == 1
    assert words[-1] == "stopped"
    assert code == BY_SIGTERM, said
    assert "sortie: arret confirme, port libere" in said


@signals_reach_a_process
def test_signals_that_arrive_during_the_stop_do_not_cut_it_short(tmp_path: Path) -> None:
    """A second ``kill``, the terminal closed, an impatient Ctrl+C, while the stop is running.

    The stop takes a second and a half here. Each of the three would end the
    process at once, the bus thread in the middle of its stop sequence. They
    are ignored until the stop has finished: it runs once, to its end.
    """
    console = _console(tmp_path, stop_takes=1.5)
    try:
        console.wait_for("serving")
        console.process.send_signal(signal.SIGTERM)
        console.wait_for("stopping")
        for again in (signal.SIGTERM, HANG_UP, signal.SIGINT):
            console.process.send_signal(again)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()
    said = console.said()

    assert words.count("stopping") == 1
    assert words.count("stopped") == 1, f"the stop was cut short: {said}"
    assert code == BY_SIGTERM, said
    assert "sortie: arret confirme, port libere" in said
