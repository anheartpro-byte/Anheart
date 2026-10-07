"""Ownership errors and cleanup, using real temporary lock files."""

from __future__ import annotations

import errno
import gc
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import BinaryIO

import pytest

from src.clock import ManualClock
from src.motor import drive_process_lock as ownership
from src.motor.drive_process_lock import (
    DriveBusy,
    DriveConnection,
    DriveLease,
    DriveOwnershipError,
    LockError,
    LockUnavailable,
)
from src.motor.ftdi_link import open_ftdi_port
from src.result import Err, Ok
from src.units import Seconds
from tests.test_drive_lock_transports import run_contender
from tests.test_ftdi_link import FRAME, ConfigurableChip, FakeChip, Sleeps


def test_stale_pid_is_diagnostic_only_and_lock_file_is_retained() -> None:
    ownership.LOCK_PATH.write_bytes(b"\0PID 999999999\n")
    lease = DriveLease.claim()
    assert ownership.LOCK_PATH.read_bytes() == f"\0PID {os.getpid()}\n".encode()
    lease.close()
    assert ownership.LOCK_PATH.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shared lock namespace")
def test_symlink_lock_path_refuses_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated-data"
    target.write_bytes(b"preserve this file")
    ownership.LOCK_PATH.symlink_to(target)
    result = DriveLease.acquire()
    if isinstance(result, Ok):
        result.value.close()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)
    assert target.read_bytes() == b"preserve this file"


def test_hard_link_lock_path_refuses_without_modifying_target(tmp_path: Path) -> None:
    target = tmp_path / "unrelated-data"
    target.write_bytes(b"preserve this file")
    ownership.LOCK_PATH.hardlink_to(target)
    result = DriveLease.acquire()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)
    assert target.read_bytes() == b"preserve this file"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO fixture")
def test_nonregular_lock_path_is_refused_without_blocking() -> None:
    os.mkfifo(ownership.LOCK_PATH)
    result = DriveLease.acquire()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)


def test_same_process_second_owner_is_refused_without_overwriting_pid() -> None:
    lease = DriveLease.claim()
    try:
        assert DriveLease.acquire() == Err(DriveBusy(f"PID {os.getpid()}\n"))
        with pytest.raises(DriveOwnershipError, match=str(os.getpid())):
            DriveLease.claim()
    finally:
        lease.close()


def test_stale_pid_does_not_allow_stealing_an_active_kernel_lock() -> None:
    lease = DriveLease.claim()
    try:
        with ownership.LOCK_PATH.open("r+b") as metadata:
            metadata.seek(1)
            metadata.write(b"PID 999999999\n")
        assert DriveLease.acquire() == Err(DriveBusy("PID 999999999\n"))
    finally:
        lease.close()


def test_missing_parent_refuses_before_locking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ownership, "LOCK_PATH", tmp_path / "absent" / "lock")
    result = DriveLease.acquire()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)
    with pytest.raises(DriveOwnershipError, match="ownership unavailable"):
        DriveLease.claim()


def test_lock_syscall_error_closes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    files: list[BinaryIO] = []

    def fail(file: BinaryIO) -> None:
        files.append(file)
        raise OSError(errno.EIO, "synthetic lock failure")

    monkeypatch.setattr(ownership, "lock_file", fail)
    result = DriveLease.acquire()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)
    assert files[0].closed


def test_pid_write_error_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    # A read-only descriptor gives a real write failure without faking the lock.
    ownership.LOCK_PATH.touch()
    file = ownership.LOCK_PATH.open("rb")

    def read_only_open() -> BinaryIO:
        return file

    monkeypatch.setattr(ownership, "open_lock_file", read_only_open)
    result = DriveLease.acquire()
    assert isinstance(result, Err)
    assert isinstance(result.error, LockUnavailable)
    assert file.closed


def test_closed_or_inherited_lease_cannot_open_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    lease = DriveLease.claim()
    lease.require_active()
    with monkeypatch.context() as patch:
        patch.setattr(os, "getpid", lambda: -1)
        with pytest.raises(DriveOwnershipError, match="inherited"):
            lease.require_active()
    lease.close()
    with pytest.raises(DriveOwnershipError, match="closed"):
        lease.require_active()


@pytest.mark.parametrize("opened", [True, False])
def test_connection_release_on_close_or_failed_open(opened: bool) -> None:
    connection = DriveConnection()
    assert connection.connect(lambda: opened, lambda: None) is opened
    if opened:
        assert connection.connect(lambda: True, lambda: None)
        assert isinstance(DriveLease.acquire(), Err)
    connection.close(lambda: None)
    lease = DriveLease.claim()
    lease.close()
    connection.close(lambda: None)


def test_connection_open_exception_releases_ownership() -> None:
    def fail() -> bool:
        raise OSError("synthetic open failure")

    connection = DriveConnection()
    with pytest.raises(OSError, match="synthetic"):
        connection.connect(fail, lambda: None)
    lease = DriveLease.claim()
    lease.close()


def test_connection_close_failure_retains_ownership_until_closed() -> None:
    def fail() -> None:
        raise OSError("synthetic close failure")

    connection = DriveConnection()
    assert connection.connect(lambda: True, lambda: None)
    with pytest.raises(OSError, match="synthetic"):
        connection.close(fail)
    assert isinstance(DriveLease.acquire(), Err)
    connection.close(lambda: None)
    result = DriveLease.acquire()
    assert isinstance(result, Ok)
    result.value.close()


def test_windows_lock_uses_nonblocking_one_byte_region(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, int]] = []
    backend = ModuleType("msvcrt")

    def locking(fd: int, mode: int, length: int) -> None:
        calls.append((fd, mode, length))

    monkeypatch.setattr(backend, "LK_NBLCK", 2, raising=False)
    monkeypatch.setattr(backend, "locking", locking, raising=False)
    with ownership.LOCK_PATH.open("a+b") as file:
        file.write(b"\0PID 42\n")
        with monkeypatch.context() as patch:
            patch.setitem(sys.modules, "msvcrt", backend)
            patch.setattr(sys, "platform", "win32")
            ownership.lock_file(file)
        assert calls == [(file.fileno(), 2, 1)]
        assert file.tell() == 0


def test_windows_file_open_uses_portable_descriptor_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "win32")
        with ownership.open_lock_file() as file:
            file.write(b"\0PID 42\n")
    assert ownership.LOCK_PATH.read_bytes() == b"\0PID 42\n"


@pytest.mark.parametrize("invalid_error", ["unknown runtime error variant"])
def test_unknown_runtime_lock_error_is_rejected(invalid_error: LockError) -> None:
    # Pytest supplies deliberately invalid runtime data to exercise the exhaustive guard.
    with pytest.raises(AssertionError):
        DriveOwnershipError(invalid_error)


def test_unknown_acquisition_result_never_opens_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_result() -> str:
        return "unknown runtime result variant"

    opened: list[bool] = []

    def open_transport() -> bool:
        opened.append(True)
        return True

    monkeypatch.setattr(DriveLease, "acquire", invalid_result)
    with pytest.raises(AssertionError):
        DriveConnection().connect(open_transport, lambda: None)
    assert opened == []


def test_reconnect_failure_releases_existing_reservation() -> None:
    connection = DriveConnection()
    assert connection.connect(lambda: True, lambda: None)
    assert not connection.connect(lambda: False, lambda: None)
    lease = DriveLease.claim()
    lease.close()


def test_transport_can_close_itself_during_failed_open() -> None:
    connection = DriveConnection()

    def open_transport() -> bool:
        connection.close(lambda: None)
        return False

    assert not connection.connect(open_transport, lambda: None)
    lease = DriveLease.claim()
    lease.close()


def test_ordinary_ftdi_close_failure_survives_dropped_port_until_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = FakeChip()
    clock = ManualClock()
    port = open_ftdi_port(
        "ftdi://schneider:rs485/1",
        FRAME,
        timeout=Seconds(0.1),
        clock=clock,
        create=lambda _: ConfigurableChip(raw),
        sleep=Sleeps(clock),
    )

    def fail_close() -> None:
        raise OSError("synthetic close failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(raw, "close", fail_close)
            try:
                port.close()
            except OSError:
                assert raw.close_calls == 0
            else:
                pytest.fail("close must fail before the port is discarded")
            del port
            gc.collect()
            contender = run_contender(tmp_path)
            assert contender.returncode == 3, contender.stdout + contender.stderr
            assert not (tmp_path / "attempt-opened").exists()
        ownership.retry_failed_drive_closes()
        assert raw.close_calls == 1
        assert run_contender(tmp_path).returncode == 0
    finally:
        ownership.retry_failed_drive_closes()
        if raw.close_calls == 0:
            raw.close()
