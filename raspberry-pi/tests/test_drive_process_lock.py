"""Real process ownership scenarios. The synthetic cable never touches hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import pytest

from tests.test_drive_lock_transports import broken_open_master, refused_open, run_contender

READY: Final[bytes] = b"OPEN\n"
"""The line a worker writes once it holds the cable."""


def read_handshake(read: Callable[[int], bytes]) -> bytes:
    """Read the worker's first line, however many pieces it arrives in.

    ``print("OPEN", flush=True)`` is one write when the worker's output is
    buffered and two (``OPEN``, then the newline) when ``PYTHONUNBUFFERED`` is
    set: a single read can return either. So this reads until the line ends,
    or until the worker closes its output. ``read`` is given the most bytes it
    may return and answers ``b""`` at the end of the stream, as ``os.read``
    does.
    """
    received = b""
    while not received.endswith(b"\n"):
        piece = read(max(len(READY) - len(received), 1))
        if not piece:
            break
        received += piece
    return received


def wait_until_open(owner: subprocess.Popen[str]) -> None:
    assert owner.stdout is not None
    descriptor = owner.stdout.fileno()
    with ThreadPoolExecutor(max_workers=1) as executor:
        readiness = executor.submit(read_handshake, lambda size: os.read(descriptor, size))
        try:
            assert readiness.result(timeout=15) == READY
        except TimeoutError:
            owner.kill()
            owner.wait(timeout=15)
            raise


@pytest.mark.parametrize(
    "pieces",
    [[b"OPEN\n"], [b"OPEN", b"\n"], [b"O", b"PE", b"N", b"\n"]],
    ids=["one write", "the line then its end", "byte by byte"],
)
def test_the_handshake_is_read_whole_however_it_was_written(pieces: list[bytes]) -> None:
    """ANH-183 EX-1: what an unbuffered worker writes in two system calls is one line."""
    remaining = list(pieces)
    asked: list[int] = []

    def read(size: int) -> bytes:
        asked.append(size)
        return remaining.pop(0) if remaining else b""

    received = read_handshake(read)
    assert received == READY
    assert not remaining
    # Never asked for more than the line can still hold: nothing after it is consumed.
    assert asked[0] == len(READY)
    assert all(1 <= size <= len(READY) for size in asked)


def test_a_worker_that_closes_its_output_early_is_not_waited_for() -> None:
    pieces = [b"OP"]

    def read(_size: int) -> bytes:
        return pieces.pop(0) if pieces else b""

    received = read_handshake(read)
    assert received == b"OP"


@pytest.mark.parametrize("unbuffered", ["", "1"], ids=["buffered", "PYTHONUNBUFFERED=1"])
def test_the_owner_is_seen_open_whatever_the_buffering_of_its_output(
    tmp_path: Path, unbuffered: str
) -> None:
    """ANH-183 EX-1: the variable is set or cleared here, whatever the runner exports."""
    worker = Path(__file__).with_name("drive_lock_worker.py")
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    if unbuffered:
        environment["PYTHONUNBUFFERED"] = unbuffered
    with subprocess.Popen(  # noqa: S603 - fixed interpreter and repository fixture
        [sys.executable, str(worker), str(tmp_path), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    ) as owner:
        assert owner.stdin is not None
        try:
            wait_until_open(owner)
            assert (tmp_path / "hold-opened").exists()
        finally:
            owner.stdin.close()
            owner.wait(timeout=15)


@pytest.mark.parametrize(
    "backend", ["ftdi", "serial", "write", "read", "probe", "scan", "latency", "console", "bench"]
)
def test_second_process_is_refused_before_transport_open(tmp_path: Path, backend: str) -> None:
    # Given: a separate owner holds a synthetic FTDI cable until stdin closes.
    worker = Path(__file__).with_name("drive_lock_worker.py")
    with subprocess.Popen(  # noqa: S603 - fixed interpreter and repository fixture
        [sys.executable, str(worker), str(tmp_path), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as owner:
        assert owner.stdout is not None
        assert owner.stdin is not None
        try:
            wait_until_open(owner)
            # When: another interpreter attempts to open the same cable.
            contender = subprocess.run(  # noqa: S603 - fixed synthetic fixture
                [sys.executable, str(worker), str(tmp_path), "attempt", backend],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            # Then: it refuses before reaching the physical opener.
            assert contender.returncode == 3, contender.stdout + contender.stderr
            assert str(owner.pid) in contender.stdout + contender.stderr
            assert not (tmp_path / "attempt-opened").exists()
        finally:
            owner.stdin.close()
            owner.wait(timeout=15)


@pytest.mark.parametrize("crash", [False, True])
@pytest.mark.parametrize("backend", ["ftdi", "failed-serial", "failed-ftdi", "failed-ftdi-close"])
def test_process_exit_releases_drive_lock(tmp_path: Path, crash: bool, backend: str) -> None:
    worker = Path(__file__).with_name("drive_lock_worker.py")
    with subprocess.Popen(  # noqa: S603 - synthetic fixture
        [sys.executable, str(worker), str(tmp_path), "hold", backend],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as owner:
        assert owner.stdout is not None
        assert owner.stdin is not None
        try:
            wait_until_open(owner)
            if crash:
                owner.kill()
            else:
                owner.stdin.close()
            owner.wait(timeout=15)
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=15)
    acquired = subprocess.run(  # noqa: S603 - synthetic fixture
        [sys.executable, str(worker), str(tmp_path), "attempt"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert acquired.returncode == 0, acquired.stderr
    assert (tmp_path / "attempt-opened").exists()
    assert (tmp_path / "drive.lock").exists()


def test_failed_ftdi_close_cannot_reuse_the_cached_live_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    master, handle, opens = broken_open_master("ftdi", monkeypatch)
    handle.fail_setup = False
    handle.fail_close = False
    try:
        assert master.connect()
        handle.fail_close = True
        with pytest.raises(OSError, match="synthetic close failed"):
            master.close()
        refused_open(master)
        assert len(opens) == 1
        assert run_contender(tmp_path).returncode == 3
        handle.fail_close = False
        assert master.connect()
        assert len(opens) == 2
        assert handle.successful_closes == 1
    finally:
        handle.fail_close = False
        master.close()
