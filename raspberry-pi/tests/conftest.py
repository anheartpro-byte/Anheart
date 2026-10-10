"""Shared fixtures.

The ``clean_env`` fixture below is autouse. The console merges the process
environment over its ``.env`` (``load_environment`` in ``src/local_panel.py``),
so a developer whose shell exports the dashboard link's keys would otherwise be
feeding real machine credentials into the test run. Clearing them around every
test keeps the suite independent of the shell it is launched from.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import src.motor.drive_process_lock
from tests.notify_support import Listener

#: The dashboard link's keys: the only ones that carry a credential. Listed
#: explicitly rather than pattern-matched, so adding one here is deliberate.
ANHEART_ENV_VARS: tuple[str, ...] = (
    "CONVEX_URL",
    "MACHINE_API_KEY",
)


@pytest.fixture(autouse=True)
def isolate_drive_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(src.motor.drive_process_lock, "LOCK_PATH", tmp_path / "drive.lock")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from the dashboard credentials of the ambient environment."""
    for name in ANHEART_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def short_directory() -> Iterator[Path]:
    """A directory whose path fits a socket's name (about a hundred bytes, less on macOS).

    ``tmp_path`` is too long for one on some systems.
    """
    with tempfile.TemporaryDirectory(prefix="anh") as name:
        yield Path(name)


@pytest.fixture
def listener(short_directory: Path) -> Iterator[Listener]:
    """A service manager's notification socket, listening (``tests/notify_support.py``)."""
    made = Listener(short_directory)
    yield made
    made.close()
