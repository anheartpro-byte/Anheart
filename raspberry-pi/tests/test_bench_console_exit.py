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
SIGKILL. It still could when the last line had nowhere to go, the terminal
closed or the pipe broken: the write failed before the process was ended.

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

import errno
import io
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from http import HTTPStatus
from pathlib import Path
from types import FrameType
from typing import Final, Literal, assert_never, cast, override

import httpx
import pytest
import uvicorn

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


async def test_once_the_exit_has_begun_every_motion_route_is_refused_and_the_stop_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANH-218, in process: an armed console, then the exit. Enable, speed and arming are
    409 at once, nothing is queued, and the bus thread has been asked for the stop."""
    rig = _rig(monkeypatch)
    monkeypatch.setattr(bench, "worker", rig.worker)
    monkeypatch.setattr(bench, "LEAVING", threading.Event())
    asked: list[str] = []
    queued: list[str] = []

    def shutdown(_worker: bench.BusWorker) -> None:
        asked.append("stop")

    def submit(_worker: bench.BusWorker, command: bench.Command) -> None:
        queued.append(command.action)

    monkeypatch.setattr(bench.BusWorker, "shutdown", shutdown)
    monkeypatch.setattr(bench.BusWorker, "submit", submit)
    page = httpx.AsyncClient(transport=httpx.ASGITransport(app=bench.app), base_url="http://bench")
    async with page:
        before = await page.post("/api/speed", json={"rpm": 30})
        assert before.status_code == HTTPStatus.OK
        rig.worker.snap.armed = True
        bench.disarm_and_stop()  # what the exit signal calls
        refused = [
            await page.post("/api/enable"),
            await page.post("/api/speed", json={"rpm": 60}),
            await page.post("/api/arm", json={"motor_uncoupled": True}),
        ]
        disarmed = await page.post("/api/arm", json={"motor_uncoupled": False})
    assert [answer.status_code for answer in refused] == [HTTPStatus.CONFLICT] * 3
    assert all(bench.LEAVING_REFUSAL in answer.text for answer in refused)
    assert disarmed.status_code == HTTPStatus.OK, "disarming stays possible"
    assert rig.worker.snap.armed is False
    assert asked == ["stop"]
    assert queued == ["speed"], "only the speed asked for before the exit was queued"


def test_a_command_still_queued_when_the_stop_is_asked_for_is_never_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bus thread drains its queue only while no stop is asked for (ANH-218)."""
    rig = _rig(monkeypatch)
    rig.worker.submit(bench.Command("speed", 60))
    rig.worker.shutdown()
    # This stand-in has no speed to write: run, the speed would raise out of the thread.
    rig.worker.run()
    assert rig.drive.stops == 1
    assert rig.worker.snap.setpoint_rpm == 0


async def test_an_unarmed_console_refuses_the_motion_routes_before_any_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arming's own refusal, the second reason ``_motion_refusal`` gives: nothing queued."""
    rig = _rig(monkeypatch)
    rig.worker.snap.armed = False
    monkeypatch.setattr(bench, "worker", rig.worker)
    monkeypatch.setattr(bench, "LEAVING", threading.Event())
    queued: list[str] = []

    def submit(_worker: bench.BusWorker, command: bench.Command) -> None:
        queued.append(command.action)

    monkeypatch.setattr(bench.BusWorker, "submit", submit)
    page = httpx.AsyncClient(transport=httpx.ASGITransport(app=bench.app), base_url="http://bench")
    async with page:
        refused = [
            await page.post("/api/enable"),
            await page.post("/api/speed", json={"rpm": 60}),
        ]
    assert [answer.status_code for answer in refused] == [HTTPStatus.CONFLICT] * 2
    assert all("console non armee" in answer.text for answer in refused)
    assert queued == []


type _Dispatch = Callable[[bench.BusWorker, bench.Command], Awaitable[str]]


def _dispatching(
    actions: list[str], then: Callable[[bench.BusWorker], None] | None = None
) -> _Dispatch:
    """The bus thread's actions, recorded in ``actions`` in place of the drive's. The first
    asks for the stop, then runs ``then`` on the bus thread, as a request handler would
    submit in that instant."""

    async def dispatch(worker: bench.BusWorker, command: bench.Command) -> str:
        actions.append(command.action)
        if len(actions) == 1:
            worker.shutdown()
            if then is not None:
                then(worker)
        return command.action

    return dispatch


def test_a_command_queued_behind_the_one_that_asks_for_the_stop_is_never_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The drain itself stops at the stop (ANH-218): the bus thread is taking its queue when
    the stop is asked for, and the command behind is left there."""
    rig = _rig(monkeypatch)
    actions: list[str] = []
    monkeypatch.setattr(bench.BusWorker, "_dispatch", _dispatching(actions))
    rig.worker.submit(bench.Command("speed", 30))
    rig.worker.submit(bench.Command("enable"))
    rig.worker.run()
    assert actions == ["speed"], "the command behind was run after the stop"
    assert rig.drive.stops == 1


def test_a_command_submitted_once_the_stop_is_asked_for_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2's race (ANH-218): the bus thread checks for the stop, is pre-empted, and a request
    queues a command in that instant. Dropped at the queue, whatever check the bus thread
    passed, there is nothing for it to take."""
    rig = _rig(monkeypatch)
    commands: queue.Queue[bench.Command] = queue.Queue()
    monkeypatch.setattr(rig.worker, "_commands", commands)

    def submit_late(worker: bench.BusWorker) -> None:
        worker.submit(bench.Command("enable"))
        worker.submit(bench.Command("speed", 60))

    actions: list[str] = []
    monkeypatch.setattr(bench.BusWorker, "_dispatch", _dispatching(actions, submit_late))
    rig.worker.submit(bench.Command("speed", 30))
    rig.worker.run()
    assert commands.empty(), "a command was queued after the stop was asked for"
    assert actions == ["speed"]
    assert rig.drive.stops == 1


def test_the_server_s_exit_goes_ahead_when_the_disarm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 of ANH-218: the exit signal's own handling goes on whatever the disarm does. It
    raises here, and uvicorn is still told to exit."""

    def broken() -> None:
        raise RuntimeError("the disarm went wrong")

    monkeypatch.setattr(bench, "disarm_and_stop", broken)
    server = bench.StoppingServer(uvicorn.Config(bench.app))
    with pytest.raises(RuntimeError, match="the disarm went wrong"):
        server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit, "uvicorn's own exit was skipped"


class _HeldFlag:
    """The stop flag, whose drain check on the bus thread is answered, then held.

    The ``held_at``-th check made on the bus thread gives its answer, then waits
    for :attr:`released` before returning it: the bus thread pre-empted between
    that check and its next step, as R2 forced it. Every other check, and the
    flag itself, is a plain event's.
    """

    def __init__(self, thread: threading.Thread, held_at: int) -> None:
        self._flag: threading.Event = threading.Event()
        self._thread: threading.Thread = thread
        self._held_at: int = held_at
        self._checks: int = 0
        self.paused: threading.Event = threading.Event()
        self.released: threading.Event = threading.Event()

    def is_set(self) -> bool:
        answer = self._flag.is_set()
        if threading.current_thread() is self._thread:
            self._checks += 1
            if self._checks == self._held_at:
                self.paused.set()
                assert self.released.wait(5), "the test never let the bus thread go"
        return answer

    def set(self) -> None:
        self._flag.set()


class _SignalInsidePut(queue.Queue[bench.Command]):
    """A queue whose put lets the exit signal land first: inside submit, after its check."""

    def __init__(self, worker: bench.BusWorker) -> None:
        super().__init__()
        self._worker: bench.BusWorker = worker

    @override
    def put(self, item: bench.Command, block: bool = True, timeout: float | None = None) -> None:
        self._worker.shutdown()
        super().put(item, block, timeout)


def test_a_command_put_after_the_stop_by_a_submit_already_past_its_check_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2's interleaving (ANH-218), forced. The bus thread checks for the stop (no stop yet)
    and is pre-empted; a request's submit passes its own check, the exit signal sets the
    flag, then the submit puts its speed. Taken from the queue, the speed is dropped by the
    bus thread's re-check; the driver's stop follows."""
    rig = _rig(monkeypatch)
    worker = rig.worker
    flag = _HeldFlag(worker, held_at=2)  # 1: the cycle's check, 2: the drain's
    monkeypatch.setattr(worker, "_stop_requested", flag)
    monkeypatch.setattr(worker, "_commands", _SignalInsidePut(worker))
    actions: list[str] = []

    async def record(_worker: bench.BusWorker, command: bench.Command) -> str:
        actions.append(command.action)
        return "ok"

    monkeypatch.setattr(bench.BusWorker, "_dispatch", record)
    worker.start()
    try:
        assert flag.paused.wait(5), "the bus thread never reached its drain check"
        worker.submit(bench.Command("speed", 200))
        assert flag.is_set(), "the signal landed inside submit"
    finally:
        flag.released.set()
        worker.join(10)
    assert not worker.is_alive()
    assert actions == [], "the speed put after the stop reached the drive"
    assert rig.drive.stops == 1


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

ENDED_WELL: Final[list[str]] = ["stopping", "stopped", "signals-back"]
"""How the stand-in's report ends when the stop ran once, to its end, before ``main`` was over,
and every exit signal then did again what it did before ``main``."""

STOP_UNFINISHED: Final[int] = 4
"""The exit status of a console that ended without having seen the stop finish. Written here
as the number the guide gives, not read from the script: zero would say all went well."""

type Output = Literal["a file", "a pipe", "a terminal", "a file, no error output"]
"""Where the console's standard output and error go. A pipe and a terminal can be closed
under it; with ``a file, no error output`` it starts with its standard error closed."""

WITHOUT_ERROR_OUTPUT: Final[str] = (
    "import os, sys; os.close(2); os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
)
"""A process that closes its standard error, then becomes the command its arguments give.
Python then has ``None`` for ``sys.stderr``: what ``2>&-`` does in a shell."""


@dataclass(frozen=True, slots=True)
class _Console:
    """The bench console running in its own process, and what it leaves behind."""

    process: subprocess.Popen[bytes]
    report: Path
    log: Path
    closing: Callable[[], None] | None = None
    """What takes the console's output away, when it is not a file."""

    def lose_output(self) -> None:
        """Take the console's output away from under it: its next write there fails."""
        closing = self.closing
        assert closing is not None, "this console writes to a file, which nothing takes away"
        closing()

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
        """What the console printed, when its output was a file."""
        if not self.log.exists():
            return ""
        return self.log.read_text(encoding="utf-8", errors="replace")


def _console(
    tmp_path: Path,
    *,
    stop: str = "0",
    wait: float | None = None,
    output: Output = "a file",
    http_port: int | None = None,
) -> _Console:
    """Start the real ``main()`` over the stand-in drive. The caller ends it with a signal.

    ``stop`` is how long the stand-in's stop takes, in seconds, or ``stuck``
    for one that never returns; ``wait`` replaces the 35 s the script waits
    for the stop. ``output`` is where the console writes: a file the test
    reads afterwards, or something ``lose_output`` can take away, a pipe
    (whose reader then is gone) or a terminal (which then is closed).
    ``http_port`` is where its page listens, for a test that has to reach it.
    """
    report = tmp_path / "report.txt"
    log = tmp_path / "console.log"
    command = [sys.executable, str(WORKER), str(report), stop]
    if wait is not None or http_port is not None:
        command.append("-" if wait is None else str(wait))
    if http_port is not None:
        command.append(str(http_port))
    home = WORKER.parent.parent
    match output:
        case "a file":
            with log.open("wb") as kept:
                process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
                    command, cwd=home, stdout=kept, stderr=subprocess.STDOUT
                )
            return _Console(process, report, log)
        case "a pipe":
            process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
                command, cwd=home, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            reader = process.stdout
            assert reader is not None
            return _Console(process, report, log, reader.close)
        case "a terminal":
            ours, theirs = os.openpty()
            try:
                process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
                    command, cwd=home, stdout=theirs, stderr=theirs
                )
            finally:
                os.close(theirs)
            return _Console(process, report, log, partial(os.close, ours))
        case "a file, no error output":
            with log.open("wb") as kept:
                process = subprocess.Popen(  # noqa: S603  # this interpreter, a file of this repository
                    [sys.executable, "-c", WITHOUT_ERROR_OUTPUT, *command[1:]],
                    cwd=home,
                    stdout=kept,
                )
            return _Console(process, report, log)
    raise assert_never(output)


def _free_port() -> int:
    """A port of this machine that nothing listens on, for the console's page."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return cast(int, probe.getsockname()[1])


def _held_request(port: int) -> socket.socket:
    """A client that sends a request's headers and one byte of its body, then waits.

    What a tablet that sleeps, or drops its Wi-Fi, halfway through a POST does.
    The page listens once the web server's start-up is over: until then the
    connection is refused, and tried again.
    """
    deadline = time.monotonic() + PATIENCE
    client: socket.socket | None = None
    while client is None:
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=PATIENCE)
        except ConnectionRefusedError:
            assert time.monotonic() < deadline, f"the page never listened on {port}"
            time.sleep(0.02)
    client.sendall(
        b"POST /api/speed HTTP/1.1\r\nHost: bench\r\nContent-Type: application/json\r\n"
        b"Content-Length: 100\r\n\r\n{"
    )
    return client


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
    cancels its loop. And once ``main`` is over each signal does again what it
    did before.
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
    # The stop starts with the signal (ANH-218): the web server closes beside it.
    assert "served" in words, f"the web server never closed: {words}"
    assert [word for word in words if word != "served"][-3:] == ENDED_WELL, (
        f"not one whole stop: {words}"
    )
    assert code == status, said
    assert CONFIRMED_LINE in said


@signals_reach_a_process
@pytest.mark.parametrize(
    ("sent", "status"),
    [
        pytest.param(signal.SIGTERM, BY_SIGTERM, id="SIGTERM"),
        pytest.param(HANG_UP, BY_HANG_UP, id="SIGHUP"),
    ],
)
def test_a_request_left_open_does_not_keep_the_console_from_its_stop(
    tmp_path: Path, sent: signal.Signals, status: int
) -> None:
    """A client is halfway through a request when the signal arrives. One stop, then the end.

    The web server waits for the requests it serves before it lets go. Without
    a bound it waited for good: the process stayed, deaf to SIGTERM and SIGHUP,
    its page closed, the driver's stop never begun and the bus still commanding
    the setpoint. It now cancels the request after ``GRACEFUL_SHUTDOWN_S``.
    """
    port = _free_port()
    console = _console(tmp_path, http_port=port)
    client: socket.socket | None = None
    try:
        console.wait_for("serving")
        client = _held_request(port)
        # The server has the headers, and a request under way, before the signal.
        time.sleep(0.3)
        signalled = time.monotonic()
        console.process.send_signal(sent)
        code = console.ended()
        took = time.monotonic() - signalled
    finally:
        if client is not None:
            client.close()
        _kill_if_left(console)
    words = console.words()

    assert words.count("stopping") == 1, f"the driver's stop ran {words.count('stopping')} times"
    assert [word for word in words if word != "served"][-3:] == ENDED_WELL, (
        f"not one whole stop: {words}"
    )
    # ANH-218: the driver's stop began while the web server still waited for the request.
    assert words.index("stopping") < words.index("served"), f"the stop waited: {words}"
    assert code == status, console.said()
    # Sooner, and the server had no request under way: the bound was never put to the test.
    assert took >= bench.GRACEFUL_SHUTDOWN_S, f"ended {took:.2f} s after the signal"


def _post(port: int, path: str, body: str) -> tuple[int, str]:
    """One whole request to the console's page: its status and its body.

    The page listens once the web server's start-up is over: until then the
    connection is refused, and tried again.
    """
    deadline = time.monotonic() + PATIENCE
    client: socket.socket | None = None
    while client is None:
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=PATIENCE)
        except ConnectionRefusedError:
            assert time.monotonic() < deadline, f"the page never listened on {port}"
            time.sleep(0.02)
    with client:
        payload = body.encode()
        client.sendall(
            f"POST {path} HTTP/1.1\r\nHost: bench\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        return _answer(client)


def _answer(client: socket.socket) -> tuple[int, str]:
    """The status and the body of the one answer a connection gets, read to its close."""
    received = b""
    while chunk := client.recv(4096):
        received += chunk
    head, _, answer = received.partition(b"\r\n\r\n")
    return int(head.split()[1]), answer.decode()


@signals_reach_a_process
@pytest.mark.parametrize("sent", [signal.SIGTERM, HANG_UP], ids=["SIGTERM", "SIGHUP"])
def test_from_the_exit_signal_no_motion_request_reaches_the_drive_and_the_stop_begins(
    tmp_path: Path, sent: signal.Signals
) -> None:
    """ANH-218: the signal disarms the console and starts the driver's stop at once.

    Two clients are halfway through a speed request on an armed console when the
    signal arrives. The web server waits for them, up to ``GRACEFUL_SHUTDOWN_S``.
    Before, the one that completed its request in that window reached the drive.
    Now it is answered 409, nothing is written, and the stop has already begun.
    """
    port = _free_port()
    console = _console(tmp_path, http_port=port)
    holding: socket.socket | None = None
    late: socket.socket | None = None
    try:
        console.wait_for("serving")
        armed = _post(port, "/api/arm", '{"motor_uncoupled": true}')
        assert armed[0] == HTTPStatus.OK, armed
        holding = _held_request(port)
        late = _held_request(port)
        time.sleep(0.3)  # both requests under way on the server
        signalled = time.monotonic()
        console.process.send_signal(sent)
        console.wait_for("stopping")
        began = time.monotonic() - signalled
        # The second client completes its request inside the graceful window.
        late.sendall(b'"rpm": 60}' + b" " * 89)
        status, answer = _answer(late)
        code = console.ended()
    finally:
        for client in (holding, late):
            if client is not None:
                client.close()
        _kill_if_left(console)
    words = console.words()

    assert status == HTTPStatus.CONFLICT, answer
    assert bench.LEAVING_REFUSAL in answer
    assert not [word for word in words if word.startswith("wrote-")], words
    assert "enabled" not in words
    assert began < bench.GRACEFUL_SHUTDOWN_S, f"the stop began {began:.2f} s after the signal"
    assert words.count("stopping") == 1, words
    assert code == (BY_SIGTERM if sent == signal.SIGTERM else BY_HANG_UP), console.said()


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
    assert words[-3:] == ENDED_WELL, words
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
    assert words[-3:] == ENDED_WELL, f"the process ended before the stop had finished: {words}"
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
    assert words[-3:] == ENDED_WELL, f"the stop was cut short: {said}"
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
    * the process ends at once, by itself, with the status the guide gives
      for it, which is not zero. The stop that never returns is blocked in a
      thread the interpreter waits for on its way out: the process used to
      stay there for good, ignoring Ctrl+C, SIGTERM and SIGHUP, the port and
      the lock held, until somebody killed it by force.
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
    assert code == STOP_UNFINISHED == bench.EXIT_STOP_UNFINISHED, said
    assert took < 10.0, f"the process took {took:.1f} s to end once the wait was over"
    assert last.startswith("sortie: ATTENTION: l'arret du pilote n'a pas fini en 1 s."), said
    assert "Arret NON confirme" in last
    assert "Le port et le verrou du cable, encore tenus" in last
    assert "delai de communication du variateur (ttO)" in last


# --- the console's output taken away from under it ------------------------
#
# The last line is written to a terminal that may be gone: closing the
# terminal is how SIGHUP comes, and a Ctrl+C in a pipeline ends the reader of
# the pipe too. The write then fails. It used to fail BEFORE the signals were
# given back and before the process was ended, which were skipped.

GONE: Final = pytest.mark.parametrize(
    ("output", "sent", "status"),
    [
        pytest.param("a pipe", signal.SIGINT, 0, id="the reader of its pipe gone, then Ctrl+C"),
        pytest.param("a terminal", HANG_UP, BY_HANG_UP, id="its terminal closed, then SIGHUP"),
    ],
)


@signals_reach_a_process
@GONE
def test_with_its_output_gone_a_normal_stop_still_gives_the_signals_back_and_keeps_its_status(
    tmp_path: Path, output: Output, sent: signal.Signals, status: int
) -> None:
    """Nobody can read the last line. Everything else happens as if somebody could.

    One stop, to its end; each signal then does again what it did before
    ``main`` (they stayed ignored); and the exit status is the one this exit
    has with a terminal (it was 1, the status of the write that failed, and a
    line left unwritten would make it 120 when the interpreter flushes on its
    way out).
    """
    console = _console(tmp_path, output=output)
    try:
        console.wait_for("serving")
        console.lose_output()
        console.process.send_signal(sent)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()

    assert words.count("stopping") == 1, f"the driver's stop ran {words.count('stopping')} times"
    assert words[-4:] == ["served", *ENDED_WELL], f"not one whole stop, signals given back: {words}"
    assert code == status


@signals_reach_a_process
@GONE
def test_with_its_output_gone_a_stuck_exchange_still_ends_the_process_by_itself(
    tmp_path: Path, output: Output, sent: signal.Signals, status: int
) -> None:
    """The stop never returns AND the last line cannot be written: the process still ends.

    Within the bound (one second here), by itself, with the status of a stop
    that was not seen through, whatever signal began the exit. It used to
    stay for good: the write raised before the forced end, the interpreter
    then waited for the stuck thread, and Ctrl+C, SIGTERM and SIGHUP were
    still ignored.
    """
    console = _console(tmp_path, stop="stuck", wait=1.0, output=output)
    try:
        console.wait_for("serving")
        console.lose_output()
        console.process.send_signal(sent)
        asked = time.monotonic()
        code = console.ended()
        took = time.monotonic() - asked
    finally:
        _kill_if_left(console)
    words = console.words()

    assert status != STOP_UNFINISHED, "the two statuses could not be told apart"
    assert words.count("stopping") == 1, "the driver's stop was not even begun"
    assert "stopped" not in words, "the stop had finished: not this case"
    assert code == STOP_UNFINISHED
    assert took < 10.0, f"the process took {took:.1f} s to end once the wait was over"


class _Gone(io.StringIO):
    """A terminal that is gone: every write to it fails, and it has no descriptor to redirect."""

    @override
    def write(self, text: str, /) -> int:
        raise BrokenPipeError(errno.EPIPE, "nobody is reading")


def test_the_last_line_goes_to_standard_error_when_standard_output_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output piped to a reader that has gone, the terminal still there on standard error.

    The operator can still be told how the machine was left, there. And with
    both gone the line is dropped without a word: nothing is raised.
    """
    still_there = io.StringIO()
    monkeypatch.setattr(sys, "stdout", _Gone())
    monkeypatch.setattr(sys, "stderr", still_there)
    bench._say_last("\nsortie: arret confirme, port libere")  # pyright: ignore[reportPrivateUsage]
    on_error = still_there.getvalue()

    monkeypatch.setattr(sys, "stderr", _Gone())
    bench._say_last("\nsortie: -")  # pyright: ignore[reportPrivateUsage]

    assert on_error == CONFIRMED_LINE


class _ForcedEndError(Exception):
    """Raised in place of the forced end of the process, which the test could not outlive."""


class _StillStopping:
    """A bus thread that was started and has not finished its stop when the wait for it ends."""

    ident: int | None = 1
    exit_report: str = "-"

    def shutdown(self) -> None:
        """Asked to stop: it does not."""

    def join(self, timeout: float | None = None) -> None:
        """Waited for: it does not come."""

    def is_alive(self) -> bool:
        return True


def test_what_follows_the_last_line_happens_whatever_becomes_of_the_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line fails in a way nobody foresaw: the signals are given back, the process ended.

    Writing the last line is replaced by something that raises what the
    script does not catch. Both things that follow are in ``finally`` blocks:
    each exit signal does again what it did before, and the process is ended
    with the status of a stop that was not seen through, which is 4.
    """
    ended_with: list[int] = []

    def garbled(_line: str) -> None:
        raise UnicodeError("the line could not be encoded")

    def forced_end(status: int) -> None:
        ended_with.append(status)
        raise _ForcedEndError

    before = _handlers()
    monkeypatch.setattr(bench, "_say_last", garbled)
    monkeypatch.setattr(os, "_exit", forced_end)
    try:
        bench._let_nothing_cut_the_stop()  # pyright: ignore[reportPrivateUsage]
        shielded = _handlers()
        with pytest.raises(_ForcedEndError):
            bench._stop_and_report(  # pyright: ignore[reportPrivateUsage]
                cast(bench.BusWorker, _StillStopping()), before
            )
        after = _handlers()
    finally:
        for watched, handler in before.items():
            signal.signal(watched, signal.SIG_DFL if handler is None else handler)

    assert set(shielded.values()) == {signal.SIG_IGN}
    assert after == before, "the signals were not given back"
    assert ended_with == [STOP_UNFINISHED], "the process was not ended, or not with that status"


def test_the_process_is_ended_even_when_giving_the_signals_back_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Giving the signals back raises what nobody foresaw: the process is ended all the same.

    The forced end is in a ``finally`` of its own, after the signals are given
    back: a stop that has not finished must never leave the process waiting
    for the thread it is stuck in, whatever happened just before.
    """
    ended_with: list[int] = []

    def broken(_before: object) -> None:
        raise RuntimeError("the handlers could not be set back")

    def forced_end(status: int) -> None:
        ended_with.append(status)
        raise _ForcedEndError

    def unsaid(_line: str) -> None:
        """The last line, written as if all went well."""

    before = _handlers()
    monkeypatch.setattr(bench, "_say_last", unsaid)
    monkeypatch.setattr(bench, "_give_the_signals_back", broken)
    monkeypatch.setattr(os, "_exit", forced_end)
    try:
        with pytest.raises(_ForcedEndError):
            bench._stop_and_report(  # pyright: ignore[reportPrivateUsage]
                cast(bench.BusWorker, _StillStopping()), before
            )
    finally:
        for watched, handler in before.items():
            signal.signal(watched, signal.SIG_DFL if handler is None else handler)

    assert ended_with == [STOP_UNFINISHED], "the process was not ended, or not with that status"


@pytest.mark.parametrize(
    ("output", "error"),
    [(None, None), (io.StringIO(), None), (None, io.StringIO())],
    ids=["neither", "no error output", "no standard output"],
)
def test_a_standard_stream_the_process_was_started_without_is_passed_over(
    monkeypatch: pytest.MonkeyPatch, output: io.StringIO | None, error: io.StringIO | None
) -> None:
    """A stream the process was started without is ``None`` to Python: the last line goes to
    the one that is there, if any, and nothing is raised. It used to raise on the missing
    stream's flush, and a normal stop then ended with the status of an error."""
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(sys, "stderr", error)
    bench._say_last("\nsortie: arret confirme, port libere")  # pyright: ignore[reportPrivateUsage]
    there = output if output is not None else error
    assert there is None or there.getvalue() == CONFIRMED_LINE


@signals_reach_a_process
def test_with_no_error_output_a_normal_stop_keeps_the_status_of_its_signal(
    tmp_path: Path,
) -> None:
    """Started with its standard error closed (``2>&-``), the console stops as ever.

    One whole stop, the signals given back, the last line on its standard
    output, and the status a shell gives a process ended by SIGTERM. It used
    to be 1: the flush of the missing error output raised.
    """
    console = _console(tmp_path, output="a file, no error output")
    try:
        console.wait_for("serving")
        console.process.send_signal(signal.SIGTERM)
        code = console.ended()
    finally:
        _kill_if_left(console)
    words = console.words()

    assert words[-4:] == ["served", *ENDED_WELL], f"not one whole stop: {words}"
    assert console.said().endswith(CONFIRMED_LINE), console.said()
    assert code == BY_SIGTERM
