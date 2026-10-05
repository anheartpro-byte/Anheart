"""Real process ownership scenarios. The synthetic cable never touches hardware."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


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
def test_process_exit_releases_drive_lock(tmp_path: Path, crash: bool) -> None:
    worker = Path(__file__).with_name("drive_lock_worker.py")
    with subprocess.Popen(  # noqa: S603 - synthetic fixture
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
