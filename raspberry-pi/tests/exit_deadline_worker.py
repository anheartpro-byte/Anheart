"""Subprocess fixture: the REAL console, one record export stuck on a disk that never answers.

``python tests/exit_deadline_worker.py <records directory> <port> <bounded|unbounded>``
runs the console through its real entry point (:func:`src.local_panel.main`:
real clock, real uvicorn server, ``MOTOR_BACKEND=sim ECG_SOURCE=sim``) and makes
ONE thing fail, the way a dead SD card does: once an archive has been sent,
removing its temporary file never returns. That removal runs on a worker
thread of the web server, like every file the server reads or sends, and such
a thread is not a daemon.

The parent (``tests/test_exit_deadline.py``) asks for the archive, waits for
``STUCK`` on standard output, sends SIGTERM, and measures what happens next.

``unbounded`` runs the same console with its exit deadline switched off: the
measurement of what the deadline is for.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import local_panel
from src.clock import Clock
from src.local_config import LocalConfig
from src.local_panel import LocalPanel, build_panel
from src.record.journal import Journal
from src.web import routes

_never = threading.Event()


def never_returns(_path: Path) -> None:
    """The card stopped answering: this call is still waiting when the console exits."""
    sys.stdout.write("STUCK\n")
    sys.stdout.flush()
    _never.wait()


def no_deadline(_code: int) -> None:
    """The console as it was before the deadline existed."""


def main(root: Path, port: str, mode: str) -> int:
    def build(config: LocalConfig, *, clock: Clock, journal: Journal) -> LocalPanel:
        # The console's own profile store stays out of the repository's ``data/``.
        return build_panel(
            config, clock=clock, journal=journal, profiles_path=root.parent / "profiles.json"
        )

    with contextlib.ExitStack() as patched:
        patched.enter_context(mock.patch.object(routes, "discard", never_returns))
        patched.enter_context(mock.patch.object(local_panel, "build_panel", build))
        if mode == "unbounded":
            patched.enter_context(mock.patch.object(local_panel, "bound_exit", no_deadline))
        return local_panel.main(
            {
                "MOTOR_BACKEND": "sim",
                "ECG_SOURCE": "sim",
                "ARM_RADIUS_M": "1.5",
                "UI_HOST": "127.0.0.1",
                "UI_PORT": port,
                "RECORD_ROOT": str(root),
            }
        )


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
