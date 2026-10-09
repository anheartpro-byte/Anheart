"""ANH-191 EX-1: no session is armed on a file system that cannot hold its record's files.

A record is thousands of small files (one raw block per acquisition batch), and
a file system can run out of inodes with megabytes still free: every write is
then refused, and the 500 MB gate never saw it coming. The journal thread now
measures the free inodes with the free bytes, and the arming gate refuses
under :data:`~src.record.journal.MIN_FREE_INODES`.

No real partition is involved: ``os.statvfs`` is replaced by a file system of
the test's own making.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from typing import Final

import pytest

from src.clock import ManualClock
from src.local_panel import ECG_PERIOD, describe_start_refusal
from src.record.journal import (
    FIXED_ENTRIES,
    LONGEST_SESSION,
    MIN_FREE_BYTES,
    MIN_FREE_INODES,
    RAW_BLOCKS_PER_SECOND,
    SESSION_ENTRIES,
    Journal,
    Storage,
    free_inodes,
    measure,
)
from src.record.session import RecordInodesLow, storage_gate
from src.result import Ok
from src.training.plan import ProfileStore
from src.training.runtime import MANUAL_SESSION_LIMIT, RecordStorageLow, RuntimeState
from src.units import Monotonic, Seconds
from tests.record_console_support import RecordedRig, recorded_rig, start_bench, stop

BLOCK: Final[int] = 4096
PLENTY: Final[int] = 10 * MIN_FREE_BYTES // BLOCK
"""Free blocks of a file system that is nowhere near short of bytes."""


class SmallFs:
    """A file system with a fixed number of inodes, as ``os.statvfs`` describes it.

    Its free inodes fall with every file and directory really created under
    ``root``: what the console writes is what uses it up.
    """

    def __init__(self, root: Path, inodes: int, *, counted: bool = True) -> None:
        self.root: Path = root
        self.inodes: int = inodes
        self.counted: bool = counted
        self.asked: int = 0

    def used(self) -> int:
        return sum(1 for _ in self.root.rglob("*")) + 1 if self.root.exists() else 0

    def free(self) -> int:
        return max(0, self.inodes - self.used())

    def statvfs(self, _path: Path) -> os.statvfs_result:
        self.asked += 1
        total = self.inodes if self.counted else 0
        free = self.free() if self.counted else 0
        return os.statvfs_result(
            (BLOCK, BLOCK, 4 * PLENTY, PLENTY, PLENTY, total, free, free, 0, 255)
        )


# =========================================================================
# The measurement
# =========================================================================


def test_ex1_the_measurement_reads_the_free_inodes_with_the_free_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = SmallFs(tmp_path, inodes=1000)
    monkeypatch.setattr(os, "statvfs", fs.statvfs)
    (tmp_path / "a").write_bytes(b"")
    (tmp_path / "b").mkdir()
    measured = measure(tmp_path, Monotonic(7.0))
    assert measured == Storage(PLENTY * BLOCK, Monotonic(7.0), free_inodes=1000 - 3)
    assert free_inodes(tmp_path) == 997


def test_ex1_a_file_system_that_counts_no_inodes_has_none_to_run_out_of(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some allocate their inodes as they go and report zero in all: that is not "none left"."""
    fs = SmallFs(tmp_path, inodes=0, counted=False)
    monkeypatch.setattr(os, "statvfs", fs.statvfs)
    assert free_inodes(tmp_path) is None
    clock = ManualClock()
    journal = Journal(tmp_path / "records", clock)
    assert journal.storage.free_inodes is None
    assert storage_gate(journal, clock)() is None, "bytes are plenty: nothing refuses"


def test_ex1_where_the_question_cannot_be_asked_it_is_not_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert free_inodes(tmp_path) is None


def test_ex1_inodes_that_cannot_be_read_are_an_unreadable_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def dead(_path: Path) -> os.statvfs_result:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "statvfs", dead)
    assert measure(tmp_path, Monotonic(3.0)) == Storage(None, Monotonic(3.0))


def test_ex1_the_real_file_system_answers(tmp_path: Path) -> None:
    measured = measure(tmp_path, Monotonic(1.0))
    assert measured.free_bytes is not None
    inodes = measured.free_inodes
    assert inodes is None or inodes > 0


# =========================================================================
# The threshold, and where it comes from
# =========================================================================


def test_ex1_the_threshold_is_two_sessions_of_the_longest_kind_the_console_runs() -> None:
    assert round(1 / ECG_PERIOD) == RAW_BLOCKS_PER_SECOND, "one raw block per acquisition batch"
    assert LONGEST_SESSION == MANUAL_SESSION_LIMIT, "the hour a manual session may last"
    assert SESSION_ENTRIES == 5 * 3600 + FIXED_ENTRIES == 18_008
    assert MIN_FREE_INODES == 2 * SESSION_ENTRIES == 36_016


def test_ex1_every_programme_shipped_fits_in_the_session_the_gate_plans_for(
    tmp_path: Path,
) -> None:
    store = ProfileStore(tmp_path / "profiles.json")
    loaded = store.load()
    assert isinstance(loaded, Ok), loaded
    durations = [profile.total_duration_s for profile in store.list_profiles()]
    assert durations
    assert max(durations) <= LONGEST_SESSION


async def test_ex1_a_recorded_session_creates_no_more_entries_than_the_gate_plans_for(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await recorded.tick(20.0)
        await stop(recorded, http)
    record = recorded.only_record()
    entries = sum(1 for _ in record.rglob("*")) + 1
    blocks = sum(1 for _ in (record / "ecg_raw").iterdir())
    assert entries == blocks + FIXED_ENTRIES, "its raw blocks, and eight entries that never grow"
    lasted = recorded.recording().rows[-1].t
    assert blocks <= RAW_BLOCKS_PER_SECOND * (lasted + 1.0)


# =========================================================================
# The gate
# =========================================================================


def gate_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inodes: int
) -> tuple[Journal, RecordStorageLow | None]:
    fs = SmallFs(tmp_path / "records", inodes=inodes)
    monkeypatch.setattr(os, "statvfs", fs.statvfs)
    clock = ManualClock()
    journal = Journal(tmp_path / "records", clock)
    return journal, storage_gate(journal, clock)()


def test_ex1_under_the_threshold_the_gate_refuses_and_says_how_many_are_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One inode is the records directory itself.
    journal, refusal = gate_on(tmp_path, monkeypatch, MIN_FREE_INODES)
    assert refusal == RecordInodesLow(
        PLENTY * BLOCK,
        MIN_FREE_BYTES,
        str(journal.root),
        free_inodes=MIN_FREE_INODES - 1,
        required_inodes=MIN_FREE_INODES,
    )
    assert isinstance(refusal, RecordStorageLow), "the runtime refuses on it as on any other"
    assert describe_start_refusal(refusal) == (
        "demarrage refuse : plus assez de fichiers libres pour l'enregistrement de seance : "
        f"{MIN_FREE_INODES - 1} inodes libres sous {journal.root}, {MIN_FREE_INODES} requis "
        "(une seance cree des milliers de petits fichiers). Liberer de l'espace"
    )
    status = journal.status(Monotonic(0.0))
    assert status.free_inodes == MIN_FREE_INODES - 1, "next to the free bytes, where it is read"


def test_ex1_exactly_the_threshold_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _journal, refusal = gate_on(tmp_path, monkeypatch, MIN_FREE_INODES + 1)
    assert refusal is None


def test_ex1_too_few_bytes_is_said_before_too_few_inodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cramped(_path: Path) -> os.statvfs_result:
        return os.statvfs_result((BLOCK, BLOCK, 1000, 10, 10, 1000, 5, 5, 0, 255))

    monkeypatch.setattr(os, "statvfs", cramped)
    clock = ManualClock()
    journal = Journal(tmp_path / "records", clock)
    refusal = storage_gate(journal, clock)()
    assert refusal == RecordStorageLow(10 * BLOCK, MIN_FREE_BYTES, str(journal.root))


# =========================================================================
# Acceptance: the real console on a file system short of inodes
# =========================================================================


async def session_of(recorded: RecordedRig, seconds: float) -> bool:
    """Ask for a bench session and run it to its end. ``False``: the start was refused."""
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        if recorded.state() is not RuntimeState.RUNNING:
            return False
        await recorded.tick(seconds)
        await stop(recorded, http)
    assert recorded.state() is RuntimeState.FINISHED
    return True


async def test_ex1_acceptance_a_start_is_refused_before_the_record_becomes_impossible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions are run until the file system is too short of inodes for the next one.

    The refusal comes while a whole session of the longest kind would still
    fit: no session was ever armed that the file system could not hold, and
    none lost a write.
    """
    root = tmp_path / "records"
    fs = SmallFs(root, inodes=MIN_FREE_INODES + 150)
    monkeypatch.setattr(os, "statvfs", fs.statvfs)
    recorded = recorded_rig(tmp_path)

    ran = 0
    while await session_of(recorded, 10.0):
        ran += 1
        assert not recorded.degraded(), "a session that was armed was recorded whole"
        assert ran < 20, "the file system never ran short: the gate is not looking at it"
        # The journal thread measures every five seconds; let it see what the session took.
        await recorded.tick(Seconds(6.0))

    assert ran >= 1, "there was room at first, and a session ran"
    left = fs.free()
    assert left < MIN_FREE_INODES
    assert left >= SESSION_ENTRIES, "refused while a whole hour of record would still have fitted"
    runtime = recorded.rig.panel.runtime
    assert runtime.output_enabled is False
    assert len(recorded.records()) == ran, "the refused start created nothing"
    said = recorded.rig.refusals()[-1]
    assert said.startswith(
        "demarrage refuse : plus assez de fichiers libres pour l'enregistrement de seance : "
    )
    assert f"sous {root}, {MIN_FREE_INODES} requis" in said
    assert fs.asked > ran, "measured by the journal thread, again and again"
