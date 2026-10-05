"""Real process ownership scenarios. The synthetic cable never touches hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import pytest


def hold_failed_master(
    backend: Literal["serial", "ftdi"], patch: pytest.MonkeyPatch, after_open: bool
) -> int:
    from tests.test_drive_lock_transports import broken_open_master, refused_open  # noqa: PLC0415

    master, handle, opens = broken_open_master(backend, patch)
    try:
        if after_open:
            handle.fail_setup = False
            handle.fail_close = False
            assert master.connect()
            handle.fail_close = True
            with pytest.raises(OSError, match="synthetic close failed"):
                master.close()
        else:
            refused_open(master)
        assert not handle.is_closed()
        assert len(opens) == 1
        print("OPEN", flush=True)  # noqa: T201 - live-handle readiness handshake
        sys.stdin.read(1)
    finally:
        handle.fail_close = False
        master.close()
    return 0


def run_contender(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed synthetic fixture
        [
            sys.executable,
            str(Path(__file__).with_name("drive_lock_worker.py")),
            str(root),
            "attempt",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def wait_until_open(owner: subprocess.Popen[str]) -> None:
    assert owner.stdout is not None
    with ThreadPoolExecutor(max_workers=1) as executor:
        readiness = executor.submit(os.read, owner.stdout.fileno(), 5)
        try:
            assert readiness.result(timeout=15) == b"OPEN\n"
        except TimeoutError:
            owner.kill()
            owner.wait(timeout=15)
            raise


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
    from tests.test_drive_lock_transports import broken_open_master, refused_open  # noqa: PLC0415

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
