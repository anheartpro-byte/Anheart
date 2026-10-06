"""The acceptance test of ANH-128: SIGKILL in the middle of a session, then read the record.

A separate process runs the real console in simulation
(``tests/record_kill_worker.py``) and is killed with no warning: no ``finally``
runs, no manifest is finalised, no checksum is written, the journal thread dies
with whatever it had not yet handed to the operating system. What must be left
is a record the shared reader reads, complete to within two seconds of the
kill, for every stream.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Final

from src.record.reader import read
from src.record.schema import EventKind
from src.result import Ok

WORKER: Final[Path] = Path(__file__).with_name("record_kill_worker.py")
RUN_FOR: Final[float] = 4.0
"""Seconds of recorded session before the kill: two fsync periods."""

FRESH: Final[float] = 2.0
"""The acceptance bound: nothing older than this is missing at the kill."""


def wait_until_armed(process: subprocess.Popen[str], said: Path, timeout: float) -> None:
    """Block until the worker has written ``ARMED``; fail with its stderr if it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and process.poll() is None:
        if "ARMED" in said.read_text(encoding="utf-8"):
            return
        time.sleep(0.05)
    process.kill()
    _, stderr = process.communicate()
    raise AssertionError(f"the worker never armed a session:\n{stderr}")


def test_acceptance_a_sigkill_mid_session_leaves_a_record_readable_to_within_two_seconds(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    said = tmp_path / "worker.out"
    with (
        said.open("w", encoding="utf-8") as output,
        subprocess.Popen(  # noqa: S603 - fixed interpreter and repository fixture
            [sys.executable, str(WORKER), str(root)],
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            cwd=WORKER.parent.parent,
        ) as process,
    ):
        wait_until_armed(process, said, 30.0)
        time.sleep(RUN_FOR)
        killed_at = time.time()
        os.kill(process.pid, signal.SIGKILL)
        process.wait(10.0)
        assert process.returncode == -signal.SIGKILL

    records = [path for path in root.iterdir() if not path.name.startswith(".")]
    assert len(records) == 1
    loaded = read(records[0])
    assert isinstance(loaded, Ok), loaded
    recording = loaded.value

    # Nothing was finalised: the manifest is the opening one and there are no checksums.
    assert recording.manifest.ended_at is None
    assert recording.manifest.end_reason is None
    assert not (records[0] / "checksums.sha256").exists()
    assert "missing_checksums" in {warning.code for warning in recording.warnings}
    # At most the last line of a stream, or the last block, was cut by the kill.
    assert {w.code for w in recording.warnings} <= {"missing_checksums", "truncated"}

    started_at = datetime.fromisoformat(recording.manifest.started_at).timestamp()

    def age(t: float) -> float:
        """How long before the kill the instant ``t`` of the session was."""
        return killed_at - (started_at + t)

    rows = recording.rows
    assert len(rows) >= (RUN_FOR - FRESH) * 5, f"only {len(rows)} ticks were written"
    assert age(rows[-1].t) < FRESH, f"the last tick is {age(rows[-1].t):.2f} s old"
    assert rows[0].t == 0.0
    assert all(later.t > earlier.t for earlier, later in pairwise(rows))

    events = recording.events
    refusals = [event for event in events if event.kind is EventKind.REFUSAL]
    assert events[0].detail == "manual session started"
    assert len(refusals) >= 3, "the worker's refused targets are events, twice a second"
    assert age(events[-1].t) < FRESH, f"the last event is {age(events[-1].t):.2f} s old"

    blocks = recording.raw
    assert len(blocks) >= (RUN_FOR - FRESH) * 5
    last = blocks[-1].header
    assert age(last.t_first + last.n_samples / 1000) < FRESH
    assert last.channels == ("ECG", "EDA")

    assert recording.sensors, "the 1 Hz sensor stream was being written too"
    assert age(recording.sensors[-1].t) < FRESH + 1.0
    print(  # noqa: T201 - the measured freshness, for whoever runs this with -s
        f"at the kill: last tick {age(rows[-1].t):.3f} s old, last event "
        f"{age(events[-1].t):.3f} s, last raw sample "
        f"{age(last.t_first + last.n_samples / 1000):.3f} s, {len(rows)} ticks, "
        f"{len(blocks)} raw blocks"
    )
