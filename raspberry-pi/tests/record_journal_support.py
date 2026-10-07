"""Shared pieces of the session-journal tests: a bad disk, a counted fsync, a watched tree."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal

import pytest

import src.record.writer as writer_module
from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.journal import Closing, Journal, Limits, RawBatch
from src.record.schema import Manifest
from src.units import Monotonic, UnixMillis
from tests.record_support import manifest

START: Final[Monotonic] = Monotonic(10.0)
"""``record_support.manifest`` starts its clocks at 10 s."""

ENDED: Final[UnixMillis] = UnixMillis(1_791_195_072_000)
"""2026-10-05T10:11:12Z."""

CHANNELS: Final[tuple[str, ...]] = ("ECG", "EDA", "RESP", "EMG", "SpO2", "LUX")


def clock_at_start() -> ManualClock:
    return ManualClock(START, UnixMillis(1_791_195_000_000))


def session(index: int = 1) -> Manifest:
    """Distinct records for successive sessions (a directory is never reused)."""
    return replace(manifest(), record_id=f"record-{index}", local_ref=f"local-{index}")


def opened(tmp_path: Path, clock: ManualClock, limits: Limits | None = None) -> Journal:
    """A journal WITHOUT its thread, one session open: the test drains it by hand."""
    root = tmp_path / "records"
    journal = Journal(root, clock) if limits is None else Journal(root, clock, limits=limits)
    journal.open(session(), Privacy())
    return journal


def record_of(journal: Journal) -> Path:
    path = journal.status(Monotonic(0.0)).path
    if path is None:
        raise AssertionError("no record directory was created")
    return path


def closing(reason: str = "operator_stop") -> Closing:
    return Closing(ended_at=ENDED, end_reason=reason, observation=None)


def batch(seq: int, samples: int = 200, channels: int = 1) -> RawBatch:
    names = CHANNELS[:channels]
    return RawBatch(
        seq=seq,
        t_first=seq * 0.2,
        channels=names,
        samples=tuple([float(index % 1024) for index in range(samples)] for _ in names),
    )


def count_fsync(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every ``os.fsync`` from here on, as the identity of the thread that called it."""
    calls: list[int] = []
    real = os.fsync

    def counted(descriptor: int) -> None:
        calls.append(threading.get_ident())
        real(descriptor)

    monkeypatch.setattr(os, "fsync", counted)
    return calls


@dataclass
class BadDisk:
    """Writes to one file fail with ``code`` until :meth:`heal`. Mutable, one test's."""

    name: str
    code: int
    torn: bytes = b""
    active: bool = True
    refused: int = 0

    def heal(self) -> None:
        self.active = False


def refuse_writes(
    monkeypatch: pytest.MonkeyPatch, name: str, code: int, *, torn: bytes = b""
) -> BadDisk:
    """Make every write to the file called ``name`` fail, after leaving ``torn`` behind."""
    disk = BadDisk(name=name, code=code, torn=torn)
    real = writer_module.write_file

    def failing(path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        if disk.active and path.name == disk.name:
            disk.refused += 1
            if disk.torn:
                real(path, disk.torn, mode)
            raise OSError(disk.code, os.strerror(disk.code))
        real(path, content, mode)

    monkeypatch.setattr(writer_module, "write_file", failing)
    return disk


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll ``condition`` on the real clock; a test failure names the wait that expired."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the journal thread did not get there in time")
        time.sleep(0.005)


def journal_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "record-journal"]


# =========================================================================
# Who touches the records directory: an audit hook, installed once
# =========================================================================

FILE_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "open",
        "os.mkdir",
        "os.rename",
        "os.remove",
        "os.rmdir",
        "os.truncate",
        "os.chmod",
        "os.scandir",
        "os.listdir",
        "shutil.rmtree",
    }
)


@dataclass
class Touches:
    """Filesystem operations seen under ``root`` while watched: ``(event, thread identity)``."""

    root: str
    seen: list[tuple[str, int]]

    def by(self, thread: int) -> list[str]:
        return [event for event, owner in self.seen if owner == thread]


_watching: list[Touches] = []
_installed: list[bool] = []


def _audit(event: str, args: tuple[object, ...]) -> None:
    if not _watching or event not in FILE_EVENTS or not args:
        return
    target = args[0]
    if isinstance(target, str):
        name = target
    elif isinstance(target, Path):
        name = str(target)
    elif isinstance(target, bytes):
        name = os.fsdecode(target)
    else:  # a file descriptor: already opened, and that opening was seen
        return
    for touches in _watching:
        if name.startswith(touches.root):
            touches.seen.append((event, threading.get_ident()))


@contextmanager
def watch_tree(root: Path) -> Generator[Touches]:
    """Record every open, create, rename, truncate or removal under ``root``, with its thread.

    An audit hook sees them whatever API made them (``open``, ``pathlib``,
    ``os``, ``shutil``). A hook cannot be removed, so one is installed for the
    whole test run and does nothing while no test is watching.
    """
    if not _installed:
        sys.addaudithook(_audit)
        _installed.append(True)
    touches = Touches(root=str(root), seen=[])
    _watching.append(touches)
    try:
        yield touches
    finally:
        _watching.remove(touches)
