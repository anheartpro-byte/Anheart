"""Host-wide drive ownership. Kernel locks, never PID-file deletion, arbitrate access."""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, assert_never

from src.result import Err, Ok, Result

LOCK_PATH: Final[Path] = (
    Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "anheart-drive.lock"
    if os.name == "nt"
    else Path("/tmp/anheart-drive.lock")  # noqa: S108 - shared across users and checkouts
)


@dataclass(frozen=True, slots=True)
class DriveBusy:
    owner: str


@dataclass(frozen=True, slots=True)
class LockUnavailable:
    detail: str


type LockError = DriveBusy | LockUnavailable


class DriveOwnershipError(RuntimeError):
    """Transport-boundary refusal, classified as BadResponse by ATV320Drive."""

    def __init__(self, error: LockError) -> None:
        self.error: LockError = error
        match error:
            case DriveBusy(owner):
                super().__init__(
                    f"drive cable already owned ({owner}); close the other console/tool"
                )
            case LockUnavailable(detail=reason):
                super().__init__(f"drive ownership unavailable: {reason}")
            case _ as unreachable:
                assert_never(unreachable)


def lock_file(file: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt  # noqa: PLC0415 - Windows-only standard library

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl  # noqa: PLC0415 - POSIX-only standard library

        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def open_lock_file() -> BinaryIO:
    flags = os.O_CREAT | os.O_RDWR
    if sys.platform != "win32":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    return os.fdopen(os.open(LOCK_PATH, flags, 0o600), "r+b", buffering=0)


class DriveLease:
    """Own one kernel lock until close. Mutable state tracks the descriptor lifetime.

    The file remains in place forever: unlinking it could create two locked inodes.
    PID text is diagnostic only; even stale or malformed text never authorizes stealing.
    """

    def __init__(self, file: BinaryIO) -> None:
        self._file: BinaryIO = file
        self._pid: int = os.getpid()

    def require_active(self) -> None:
        if self._file.closed or self._pid != os.getpid():
            raise DriveOwnershipError(LockUnavailable("closed or inherited ownership"))

    def close(self) -> None:
        self._file.close()

    @classmethod
    def acquire(cls) -> Result[DriveLease, LockError]:
        try:
            file = open_lock_file()
        except OSError as error:
            return Err(LockUnavailable(str(error)))
        transferred = False
        try:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return Err(LockUnavailable("lock file must be regular with exactly one link"))
            try:
                lock_file(file)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    return Err(LockUnavailable(str(error)))
                file.seek(1)
                return Err(DriveBusy(file.read(256).decode("utf-8", errors="replace")))
            file.seek(0)
            file.truncate()
            file.write(f"\0PID {os.getpid()}\n".encode())
            file.flush()
            lease = cls(file)
            transferred = True
            return Ok(lease)
        except OSError as error:
            return Err(LockUnavailable(str(error)))
        finally:
            if not transferred:
                file.close()

    @classmethod
    def claim(cls) -> DriveLease:
        acquired = cls.acquire()
        match acquired:
            case Ok(lease):
                return lease
            case Err(error):
                raise DriveOwnershipError(error)
        raise assert_never(acquired)


class DriveConnection:
    """Mutable ownership paired with an OS transport's connect/close lifecycle."""

    def __init__(self, lease: DriveLease | None = None) -> None:
        self._lease: DriveLease | None = lease

    def require_active(self) -> None:
        """Called under the port I/O lock; cleanup takes that lock after its own."""
        lease = self._lease
        if lease is None or self in _PENDING_CLOSES:
            raise DriveOwnershipError(LockUnavailable("transport closing or closed"))
        lease.require_active()

    def connect(
        self, open_transport: Callable[[], bool], close_transport: Callable[[], None]
    ) -> bool:
        with _CLEANUP_LOCK:
            pending = _PENDING_CLOSES.get(self)
            if pending is not None:
                self.close(pending)
            if self._lease is None:
                self._lease = DriveLease.claim()
            self._lease.require_active()
            opened = False
            try:
                opened = open_transport()
                return opened
            finally:
                if not opened:
                    self.close(close_transport)

    def close(self, close_transport: Callable[[], None]) -> None:
        with _CLEANUP_LOCK:
            _PENDING_CLOSES[self] = close_transport
            close_transport()
            del _PENDING_CLOSES[self]
            # Retain ownership if transport.close raises: another master must not
            # acquire a cable whose old handle may still transmit.
            if self._lease is not None:
                self._lease.close()
                self._lease = None


# A failed close must keep both the handle and lease reachable even after an
# opener raises and its caller drops the exception or client. Success removes it.
_PENDING_CLOSES: Final[dict[DriveConnection, Callable[[], None]]] = {}
_CLEANUP_LOCK: Final[threading.RLock] = threading.RLock()


def retry_failed_drive_closes() -> None:
    """Retry failed physical cleanup; failure retains ownership and propagates."""
    with _CLEANUP_LOCK:
        for connection, close_transport in tuple(_PENDING_CLOSES.items()):
            connection.close(close_transport)
