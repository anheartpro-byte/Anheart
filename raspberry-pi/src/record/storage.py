"""Private descriptor operations and portable shared-writer binary I/O."""

import errno
import hashlib
import os
import stat
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Final, Literal, NewType, assert_never

from src.clock import Clock
from src.units import Monotonic

ByteCount = NewType("ByteCount", int)
Generation = NewType("Generation", int)
MIN_FREE: Final = ByteCount(500_000_000)
HASH_BLOCK: Final = 65_536


def private_supported() -> bool:
    return (
        all(
            hasattr(os, name)
            for name in (
                "O_DIRECTORY",
                "O_NOFOLLOW",
                "O_NONBLOCK",
                "geteuid",
                "fchmod",
                "fstatvfs",
                "supports_dir_fd",
                "supports_fd",
            )
        )
        and {"open", "mkdir", "link", "unlink", "rename"}
        <= {function.__name__ for function in os.supports_dir_fd}
        and "scandir" in {function.__name__ for function in os.supports_fd}
    )


def failure_detail(error: OSError | ValueError | OverflowError) -> str:
    match error:
        case OSError():
            return f"{type(error).__name__}:{error.errno}"
        case ValueError() | OverflowError():
            return type(error).__name__
        case _ as unreachable:
            assert_never(unreachable)


class CapacityRefusal(StrEnum):
    UNKNOWN = "recording_capacity_unknown"
    LOW = "recording_disk_low"


@dataclass(frozen=True, slots=True)
class Capacity:
    generation: Generation
    observed_at: Monotonic
    available: ByteCount | None

    @property
    def refusal(self) -> CapacityRefusal | None:
        if self.available is None:
            return CapacityRefusal.UNKNOWN
        if self.available < MIN_FREE:
            return CapacityRefusal.LOW
        return None


@contextmanager
def directory(path: Path) -> Generator[int]:
    """Walk without following links or modifying ancestor permissions."""
    if not private_supported():
        raise OSError(errno.ENOTSUP, "private_storage_unavailable")
    absolute = path.absolute()
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid():
            raise PermissionError(errno.EACCES, "foreign directory")
        yield fd
    finally:
        os.close(fd)


def make_directory(path: Path, *, private: bool = True) -> None:
    """Private creation verifies the parent without altering ancestor permissions."""
    if not private:
        path.mkdir()
        return
    with directory(path.parent) as parent:
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        os.fsync(parent)


def private_root(path: Path, *, private: bool = True) -> None:
    if not private:
        path.mkdir(parents=True, exist_ok=True)
        return
    if not path.exists():
        make_directory(path)
    with directory(path) as fd:
        os.fchmod(fd, 0o700)


def capacity(root: Path, clock: Clock, generation: Generation) -> Capacity:
    try:
        with directory(root) as fd:
            observed = os.fstatvfs(fd)
            available = ByteCount(observed.f_bavail * observed.f_frsize)
    except OSError:
        available = None
    return Capacity(generation, clock.monotonic(), available)


@contextmanager
def file_handle(path: Path, flags: int, *, private: bool = True) -> Generator[BinaryIO]:
    """Private handles reject links/foreign owners; shared handles use portable binary I/O."""
    if not private:
        mode: Literal["x+b", "a+b", "r+b"] = (
            "x+b" if flags & os.O_EXCL else "a+b" if flags & os.O_APPEND else "r+b"
        )
        with path.open(mode, buffering=0) as handle:
            yield handle
        return
    with directory(path.parent) as parent:
        fd = os.open(path.name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        with os.fdopen(fd, "r+b", buffering=0) as handle:
            info = os.fstat(handle.fileno())
            if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PermissionError(errno.EACCES, "unsafe recording file")
            os.fchmod(handle.fileno(), 0o600)
            yield handle


def write_all(handle: BinaryIO, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(handle.fileno(), memoryview(content)[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "incomplete recording write")
        offset += written


def sync(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())


def publish(path: Path, content: bytes, *, replace: bool = False, private: bool = True) -> None:
    """Private publication is atomic and directory-synced; shared creation is portable."""
    temporary = path.with_name("." + path.name + ".pending")
    destination = temporary if private or replace else path
    with file_handle(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, private=private) as handle:
        write_all(handle, content)
        sync(handle)
    if not private:
        if replace:
            temporary.replace(path)
        return
    with directory(path.parent) as parent:
        if replace:
            with file_handle(path, os.O_RDWR):
                pass
            os.replace(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        else:
            os.link(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.unlink(temporary.name, dir_fd=parent)
        os.fsync(parent)


def digest(path: Path, *, private: bool = True) -> str:
    checksum = hashlib.sha256()
    with file_handle(path, os.O_RDWR, private=private) as handle:
        while block := handle.read(HASH_BLOCK):
            checksum.update(block)
    return checksum.hexdigest()


@contextmanager
def members(path: Path, *, private: bool) -> Generator[Iterator[os.DirEntry[str]]]:
    if private:
        with directory(path) as fd, os.scandir(fd) as entries:
            yield entries
    else:
        with os.scandir(path) as entries:
            yield entries


def checksums(path: Path, *, private: bool = True) -> None:
    """Stream the index and each file; never retain the growing raw-path history."""
    with file_handle(
        path / "checksums.sha256", os.O_RDWR | os.O_CREAT | os.O_EXCL, private=private
    ) as handle:
        for folder in (path, path / "ecg_raw"):
            with members(folder, private=private) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.name in {"ecg_raw", "checksums.sha256"}:
                        continue
                    member = folder / entry.name
                    checksum = digest(member, private=private)
                    line = f"{checksum}  {member.relative_to(path).as_posix()}\n"
                    write_all(handle, line.encode("utf-8"))
        sync(handle)
    if private:
        with directory(path) as fd:
            os.fsync(fd)
