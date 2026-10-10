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

And when the stop itself raised, or had not finished when the bounded wait
for it ended, the exit said ``-``, as if no stop had been attempted; in the
second case the process could then stay for good, deaf to every signal but
SIGKILL.

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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
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

BY_SIGINT: Final[int] = -int(signal.SIGINT)
"""How a process that Ctrl+C interrupted outside the web server ends: by that signal."""


def _hang_up() -> signal.Signals:
    """SIGHUP where it exists. Windows has no such signal, and skips the tests that send one."""
    if sys.platform == "win32":
        return signal.SIGTERM
    return signal.SIGHUP


HANG_UP: Final[signal.Signals] = _hang_up()
"""The terminal going away."""

BY_HANG_UP: Final[int] = 128 + int(HANG_UP)
"""The exit status of a process that left because its terminal went away."""

EXIT_SIGNALS: Final[frozenset[signal.Signals]] = frozenset({signal.SIGINT, signal.SIGTERM, HANG_UP})
"""The signals that end the console and that it can catch."""

type _Handler = Callable[[int, FrameType | None], object] | int | signal.Handlers | None
"""What a signal does: a Python handler, or one of the two dispositions of the system."""


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
        return BuiltDrive(drive=cast(ATV320Drive, drive), master=cast(ModbusMaster, master))

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
    """SIGKILL cannot be caught: the operator is told what the net is, where the tool is asked.

    And told the real bound of the wait for the stop, and what the last line
    says when the stop has not finished within it.
    """
    monkeypatch.setattr(sys, "argv", ["bench_console.py", "--help"])
    with pytest.raises(SystemExit) as left:
        bench.main()
    shown = " ".join(capsys.readouterr().out.split())

    assert left.value.code == 0
    assert "SIGKILL (kill -9)" in shown
    assert "delai de communication du variateur (ttO)" in shown
    assert "SIGTERM" in shown
    assert "SIGHUP" in shown
    assert f"{bench.SHUTDOWN_JOIN_S:.0f} s au plus" in shown
    assert "sortie: ATTENTION" in shown


class _StartReachedError(Exception):
    """Raised in place of the bus thread's start: the test stops ``main`` at that point."""


def _handlers() -> dict[signal.Signals, _Handler]:
    """What each exit signal does in this process, right now."""
    return {watched: signal.getsignal(watched) for watched in EXIT_SIGNALS}


def test_the_exit_signals_are_armed_before_the_bus_thread_starts_and_given_back_after(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The order ``main`` relies on, and the shield that does not outlive the stop.

    ``main`` runs in this process up to the start of its bus thread, which is
    replaced by a stand-in that looks at the signal handlers and then raises.

    * At that instant each exit signal already has ``main``'s handler: armed
      after the thread starts instead, a signal in between would end the
      process with the thread running and no stop.
    * The exception leaves through ``main``'s ``finally``, an exit no signal
      asked for. When the bus thread is asked for the stop every exit signal
      is ignored: the shield is up for this exit as for the others. Once the
      ``finally`` is over every signal does again what it did before
      ``main``: the shield is not left up.
    * The report starts on a line of its own, whatever was being printed when
      the exit began, and says the truth of this case: nothing was started.
    """
    armed: dict[signal.Signals, _Handler] = {}
    shielded: dict[signal.Signals, _Handler] = {}

    def start(_self: bench.BusWorker) -> None:
        armed.update(_handlers())
        sys.stdout.write("a line cut sho")
        raise _StartReachedError

    def shutdown(_self: bench.BusWorker) -> None:
        shielded.update(_handlers())

    before = _handlers()
    monkeypatch.setattr(bench.BusWorker, "start", start)
    monkeypatch.setattr(bench.BusWorker, "shutdown", shutdown)
    monkeypatch.setattr(sys, "argv", ["bench_console.py", "--port", "COM-NONE", "--http-port", "0"])
    try:
        with pytest.raises(_StartReachedError):
            bench.main()
        after = _handlers()
    finally:
        for watched, handler in before.items():
            signal.signal(watched, signal.SIG_DFL if handler is None else handler)
    printed = capsys.readouterr().out

    assert set(armed) == set(EXIT_SIGNALS)
    for watched, handler in armed.items():
        assert callable(handler), f"{watched.name} had no handler when the bus thread started"
        assert handler is not before[watched], f"{watched.name} was not armed yet"
    assert set(shielded) == set(EXIT_SIGNALS), "the bus thread was never asked for the stop"
    assert set(shielded.values()) == {signal.SIG_IGN}, "a signal could have cut the stop short"
    assert after == before, "the signals were not given back once the stop was over"
    assert "a line cut sho\nsortie: fil de bus jamais demarre" in printed


def test_an_exit_cut_into_while_the_shield_goes_up_still_reaches_the_stop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exit no signal asked for, and a signal that lands just as ``main``'s ``finally`` begins.

    The handlers are still armed at that instant, so the signal raises from
    inside the ``finally``, while the shield is going up. Raising the shield
    is replaced here by exactly that: the exit a handler would raise. The
    stop is in a ``finally`` of its own and runs all the same: the bus thread
    is asked to stop, the report is printed, the signals are given back.
    """
    asked: list[str] = []

    def start(_self: bench.BusWorker) -> None:
        raise _StartReachedError

    def shutdown(_self: bench.BusWorker) -> None:
        asked.append("stop")

    def cut_into() -> None:
        raise SystemExit(BY_SIGTERM)

    before = _handlers()
    monkeypatch.setattr(bench.BusWorker, "start", start)
    monkeypatch.setattr(bench.BusWorker, "shutdown", shutdown)
    monkeypatch.setattr(bench, "_let_nothing_cut_the_stop", cut_into)
    monkeypatch.setattr(sys, "argv", ["bench_console.py", "--port", "COM-NONE", "--http-port", "0"])
    try:
        with pytest.raises(SystemExit) as left:
            bench.main()
        after = _handlers()
    finally:
        for watched, handler in before.items():
            signal.signal(watched, signal.SIG_DFL if handler is None else handler)
    printed = capsys.readouterr().out

    assert left.value.code == BY_SIGTERM
    assert asked == ["stop"], "the bus thread was never asked for the driver's stop"
    assert "\nsortie: " in printed, "the exit printed no report"
    assert after == before


# =========================================================================
# Real signals, sent to the real main() in a process of its own
# =========================================================================

signals_reach_a_process = pytest.mark.skipif(
    sys.platform == "win32", reason="no SIGHUP, and no signal delivered to a process, on Windows"
)

CONFIRMED_LINE: Final[str] = "\nsortie: arret confirme, port libere\n"
"""The report of a stop that finished and was confirmed, on a line of its own."""


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


def _console(tmp_path: Path, *, stop: str = "0", wait: float | None = None) -> _Console:
    """Start the real ``main()`` over the stand-in drive. The caller ends it with a signal.

    ``stop`` is how long the stand-in's stop takes, in seconds, or ``stuck``
    for one that never returns; ``wait`` replaces the 35 s the script waits
    for the stop.
    """
    report = tmp_path / "report.txt"
    log = tmp_path / "console.log"
    command = [sys.executable, str(WORKER), str(report), stop]
    if wait is not None:
        command.append(str(wait))
    with log.open("wb") as output:
        process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
            command,
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
        pytest.param(HANG_UP, BY_HANG_UP, id="SIGHUP"),
    ],
)
def test_a_signal_to_the_serving_console_ends_in_the_driver_s_stop_once(
    tmp_path: Path, sent: signal.Signals, status: int
) -> None:
    """The console is serving its page when the signal arrives. One stop, then the process ends.

    Ctrl+C always ended this way. SIGTERM and SIGHUP ended the process with no
    stop at all. In the three cases the web server has closed what it serves
    before the stop begins: it takes Ctrl+C and SIGTERM itself, and SIGHUP
    cancels its loop.
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
    assert words[-3:] == ["served", "stopping", "stopped"], "the web server was left open"
    assert code == status, said
    assert CONFIRMED_LINE in said


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
    assert CONFIRMED_LINE in said


@signals_reach_a_process
@pytest.mark.parametrize(
    "together",
    [
        pytest.param((signal.SIGINT, signal.SIGTERM), id="Ctrl+C with SIGTERM"),
        pytest.param((signal.SIGINT, HANG_UP), id="Ctrl+C with SIGHUP"),
    ],
)
def test_two_different_signals_at_the_same_instant_still_end_in_one_stop(
    tmp_path: Path, together: tuple[signal.Signals, signal.Signals]
) -> None:
    """Two exit signals delivered together during the start-up wait, the bus thread connected.

    The process is held while both are sent, and let go once both are
    pending: they reach it at the same instant, every time. Each has its own
    handler. The second one used to raise inside the ``finally`` the first
    had led to, before the shield was up, and the process ended with no stop
    and no report.

    Two things hold now, and the third signal is there to tell them apart.
    The stop is in a ``finally`` of its own, so it runs whatever is raised
    while the shield goes up. And the handler that runs first raises the
    shield itself, before it raises anything else: without that the second
    handler would cut the raising of the shield short, and a signal arriving
    during the stop, as the third one does here, would cut the stop.
    Which of the signals the exit status names is not what is held here.
    """
    console = _console(tmp_path, stop="1.0")
    try:
        console.wait_for("open")
        console.process.send_signal(signal.SIGSTOP)
        for sent in together:
            console.process.send_signal(sent)
        console.process.send_signal(signal.SIGCONT)
        console.wait_for("stopping")
        console.process.send_signal(signal.SIGTERM)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()
    said = console.said()

    assert "serving" not in words, "the web server was already up: not this case"
    assert words.count("stopping") == 1, f"the driver's stop ran {words.count('stopping')} times"
    assert words[-1] == "stopped", "the process ended before the driver's stop had finished"
    assert CONFIRMED_LINE in said
    assert code in (BY_SIGINT, BY_SIGTERM, BY_HANG_UP), said


@signals_reach_a_process
def test_signals_that_arrive_during_the_stop_do_not_cut_it_short(tmp_path: Path) -> None:
    """A second ``kill``, the terminal closed, an impatient Ctrl+C, while the stop is running.

    The stop takes a second and a half here. Each of the three would end the
    process at once, the bus thread in the middle of its stop sequence. They
    are ignored until the stop has finished: it runs once, to its end.
    """
    console = _console(tmp_path, stop="1.5")
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
    assert CONFIRMED_LINE in said


@signals_reach_a_process
@pytest.mark.parametrize(
    "stop",
    [
        pytest.param("6", id="a stop longer than the wait"),
        pytest.param("stuck", id="a stop that never returns"),
    ],
)
def test_a_stop_not_finished_when_the_wait_ends_is_said_and_the_process_ends_by_itself(
    tmp_path: Path, stop: str
) -> None:
    """The wait for the stop is bounded (one second here, 35 s on the bench). Past it:

    * the last line says so, with ATTENTION: the stop is not confirmed, the
      port and the lock on the cable were still held, and the drive's own
      communication timeout is what stops the motor. It used to be ``-``;
    * the process ends at once, by itself. The stop that never returns is
      blocked in a thread the interpreter waits for on its way out: the
      process used to stay there for good, ignoring Ctrl+C, SIGTERM and
      SIGHUP, the port and the lock held, until somebody killed it by force.
    """
    console = _console(tmp_path, stop=stop, wait=1.0)
    try:
        console.wait_for("serving")
        console.process.send_signal(signal.SIGTERM)
        asked = time.monotonic()
        code = console.ended()
        took = time.monotonic() - asked
    finally:
        _kill_if_left(console)
    words = console.words()
    said = console.said()
    last = said.rstrip("\n").rsplit("\n", 1)[-1]

    assert words.count("stopping") == 1, "the driver's stop was not even begun"
    assert "stopped" not in words, "the stop had finished: not this case"
    assert code == bench.EXIT_STOP_UNFINISHED, said
    assert took < 10.0, f"the process took {took:.1f} s to end once the wait was over"
    assert last.startswith("sortie: ATTENTION: l'arret du pilote n'a pas fini en 1 s."), said
    assert "Arret NON confirme" in last
    assert "Le port et le verrou du cable, encore tenus" in last
    assert "delai de communication du variateur (ttO)" in last
