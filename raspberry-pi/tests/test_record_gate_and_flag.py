"""ANH-191 EX-5 (console side) and EX-6: a flag that clears, and a gate that says what is true.

EX-6. The arming gate refuses on a free-space measurement older than 15 s,
because a journal thread stuck on a dead disk leaves its last number behind.
But that thread measured only between two cycles, and one cycle can be long
without any disk being dead: closing a record reads every one of its files
back, the retention removes whole records. A start was then refused with "the
disk no longer answers", which was not true. Now:

* the thread measures and publishes between two files of anything long, so a
  long operation on a healthy disk refuses nothing;
* when one step alone outlasts the measurement, the refusal names what the
  thread had said it was starting.

EX-5. ``recordDegraded`` stayed true until the console was restarted once the
recorder itself had failed. It is now about one record, like every other count
behind it, and clears when the next record is opened.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from src.clock import ManualClock
from src.local_panel import describe_record_storage, describe_start_refusal
from src.record import journal as journal_module
from src.record.codec import Privacy
from src.record.journal import (
    MIN_FREE_BYTES,
    PROBE_PERIOD,
    STORAGE_STALE_AFTER,
    Activity,
    Journal,
    Storage,
)
from src.record.retention import confirm_deposit
from src.record.rows import Row
from src.record.schema import RecordError
from src.record.session import BROKEN, RecordJournalBusy, storage_gate
from src.record.writer import Writer
from src.result import Ok, Result
from src.training.runtime import RecordStorageLow, RuntimeState
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_console_support import recorded_rig, set_target, start_bench, stop
from tests.record_journal_support import batch, clock_at_start, closing, opened, session
from tests.record_support import row

ROOMY: Final[int] = 10 * MIN_FREE_BYTES


class Probe:
    """Counts the measurements of the disk, and answers that there is room."""

    def __init__(self) -> None:
        self.at: list[float] = []

    def __call__(self, _root: Path, at: Monotonic) -> Storage:
        self.at.append(float(at))
        return Storage(ROOMY, at)


class SlowDisk:
    """Every file read back takes ``step`` seconds on the manual clock, and the gate is asked."""

    def __init__(self, clock: ManualClock, gate: Callable[[], RecordStorageLow | None]) -> None:
        self.clock: ManualClock = clock
        self.gate: Callable[[], RecordStorageLow | None] = gate
        self.step: Seconds = Seconds(0.5)
        self.refusals: list[RecordStorageLow | None] = []
        self.activities: list[Activity] = []
        self.journal: Journal | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """From here on, reading a file back is slow: what indexing a record does."""
        real = Path.read_bytes
        disk = self

        def slow(path: Path) -> bytes:
            disk.clock.advance(disk.step)
            disk.refusals.append(disk.gate())
            journal = disk.journal
            if journal is not None:
                disk.activities.append(journal.activity)
            return real(path)

        monkeypatch.setattr(Path, "read_bytes", slow)


def doing(journal: Journal) -> Activity:
    """Read afresh: a checker would otherwise keep a narrowing across a drain."""
    return journal.activity


def long_record(tmp_path: Path, clock: ManualClock, blocks: int) -> Journal:
    """A journal with one session open and ``blocks`` raw blocks written: a long record."""
    journal = opened(tmp_path, clock)
    queued = journal.submit(row())
    assert queued is True
    for seq in range(blocks):
        queued = journal.submit(batch(seq, samples=10))
        assert queued is True
    journal.drain()
    return journal


# =========================================================================
# EX-6: measured during a long close, and no start refused for it
# =========================================================================


def test_ex6_the_disk_is_measured_during_a_long_close_and_no_start_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two hundred files at half a second each: a close of a hundred seconds."""
    probe = Probe()
    monkeypatch.setattr(journal_module, "measure", probe)
    clock = clock_at_start()
    journal = long_record(tmp_path, clock, blocks=200)
    gate = storage_gate(journal, clock)
    disk = SlowDisk(clock, gate)
    disk.journal = journal
    disk.install(monkeypatch)
    before = len(probe.at)
    started = float(clock.monotonic())

    journal.close(closing())
    journal.drain()

    lasted = float(clock.monotonic()) - started
    assert lasted > 6 * STORAGE_STALE_AFTER, "far longer than a measurement stays good"
    assert disk.refusals, "the gate was asked all along"
    assert all(refusal is None for refusal in disk.refusals), "and never refused"
    during = probe.at[before:]
    assert len(during) >= lasted / PROBE_PERIOD - 1, "measured every five seconds of it"
    assert set(disk.activities) == {Activity.CLOSING}, "and the thread had said what it was at"
    assert journal.activity is Activity.IDLE
    assert gate() is None


def test_ex6_a_step_that_outlasts_the_measurement_is_refused_in_words_that_are_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One read of twenty seconds in the middle of a close: slow, and said to be a close."""
    monkeypatch.setattr(journal_module, "measure", Probe())
    clock = clock_at_start()
    journal = long_record(tmp_path, clock, blocks=3)
    gate = storage_gate(journal, clock)
    disk = SlowDisk(clock, gate)
    disk.step = Seconds(20.0)
    disk.install(monkeypatch)

    journal.close(closing())
    journal.drain()

    refusal = disk.refusals[0]
    assert refusal is not None
    assert refusal == RecordJournalBusy(
        ROOMY, MIN_FREE_BYTES, str(journal.root), stale_for=Seconds(20.0), doing=Activity.CLOSING
    )
    assert isinstance(refusal, RecordStorageLow), "the runtime refuses on it as on any other"
    assert describe_start_refusal(refusal) == (
        "demarrage refuse : enregistrement de seance impossible pour l'instant : la fermeture "
        f"d'un enregistrement est en cours sous {journal.root}, derniere mesure de l'espace "
        "libre il y a 20 s. Reessayer dans un instant"
    )
    assert "le disque ne repond plus" not in describe_start_refusal(refusal)
    # The close ends, the thread measures, and the refusal lifts by itself.
    assert gate() is None


def test_ex6_a_stale_measurement_with_nothing_announced_is_still_a_disk_that_does_not_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "measure", Probe())
    clock = ManualClock(Monotonic(50.0))
    journal = Journal(tmp_path / "records", clock)
    clock.advance(Seconds(16.0))
    refusal = storage_gate(journal, clock)()
    assert refusal is not None
    assert refusal == RecordStorageLow(
        ROOMY, MIN_FREE_BYTES, str(journal.root), stale_for=Seconds(16.0)
    )
    assert describe_start_refusal(refusal).endswith("mesure il y a 16 s : le disque ne repond plus")


def test_ex6_every_announced_activity_has_its_words() -> None:
    said = {
        doing: describe_record_storage(
            RecordJournalBusy(ROOMY, MIN_FREE_BYTES, "/r", stale_for=Seconds(31.4), doing=doing)
        )
        for doing in Activity
    }
    assert said[Activity.CLOSING].startswith(
        "enregistrement de seance impossible pour l'instant : la fermeture d'un enregistrement "
        "est en cours sous /r, derniere mesure de l'espace libre il y a 31 s."
    )
    assert "la purge des enregistrements deposes est en cours sous /r" in said[Activity.PURGING]
    assert "l'ecriture des enregistrements est occupee sous /r" in said[Activity.IDLE]
    assert all(words.endswith("Reessayer dans un instant") for words in said.values())


def test_ex6_a_long_backlog_is_measured_as_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forty rows at one second each on a slow card: one cycle of forty seconds."""
    probe = Probe()
    monkeypatch.setattr(journal_module, "measure", probe)
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    gate = storage_gate(journal, clock)
    asked: list[RecordStorageLow | None] = []
    real = Writer.tick

    def slow(self: Writer, entry: Row) -> Result[None, RecordError]:
        clock.advance(Seconds(1.0))
        asked.append(gate())
        return real(self, entry)

    monkeypatch.setattr(Writer, "tick", slow)
    for _ in range(40):
        queued = journal.submit(row())
        assert queued is True
    before = len(probe.at)
    journal.drain()
    assert len(asked) == 40
    assert all(refusal is None for refusal in asked)
    assert len(probe.at) - before >= 7


def test_ex6_the_retention_is_announced_and_measured_between_two_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two records due for removal, each twenty seconds to remove."""
    probe = Probe()
    monkeypatch.setattr(journal_module, "measure", probe)
    root = tmp_path / "records"
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_791_195_000_000))
    first = Journal(root, clock)
    for index in (1, 2):
        first.open(session(index), Privacy())
        first.close(closing())
        first.drain()
        clock.advance(Seconds(2.0))
    confirmed_at = UnixMillis(1_791_195_000_000 - 40 * 86_400_000)
    for record in sorted(path for path in root.iterdir() if path.is_dir()):
        marked = confirm_deposit(record, "storage-1", confirmed_at)
        assert isinstance(marked, Ok)

    journal = Journal(root, clock, retention_days=30)
    gate = storage_gate(journal, clock)
    seen: list[tuple[RecordStorageLow | None, Activity]] = []
    real = shutil.rmtree

    def slow(path: Path) -> None:
        clock.advance(Seconds(20.0))
        seen.append((gate(), journal.activity))
        real(path)

    monkeypatch.setattr(shutil, "rmtree", slow)
    before = len(probe.at)
    journal.drain()

    assert [activity for _refusal, activity in seen] == [Activity.PURGING, Activity.PURGING]
    refusal = seen[0][0]
    assert isinstance(refusal, RecordJournalBusy)
    assert refusal.doing is Activity.PURGING
    assert describe_start_refusal(refusal) == (
        "demarrage refuse : enregistrement de seance impossible pour l'instant : la purge des "
        f"enregistrements deposes est en cours sous {root}, derniere mesure de l'espace libre "
        "il y a 20 s. Reessayer dans un instant"
    )
    assert len(probe.at) - before >= 2, "measured after each record removed"
    assert journal.activity is Activity.IDLE
    assert gate() is None
    assert sorted(path.name for path in root.iterdir()) == []


def test_ex6_an_activity_never_outlives_a_purge_that_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in the retention costs that pass; the thread does not stay "purging" for ever."""

    def broken(*_args: object) -> object:
        raise RuntimeError("injected: a bug in the retention")

    monkeypatch.setattr(journal_module, "purge", broken)
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock, retention_days=30)
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError):
        journal.drain()
    assert doing(journal) is Activity.PURGING, "published when it began, not yet withdrawn"
    journal.drain()
    assert doing(journal) is Activity.IDLE


# =========================================================================
# EX-5, console side: the flag is about one record
# =========================================================================


async def test_ex5_a_recorder_fault_marks_its_record_and_the_flag_clears_with_the_next_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorded = recorded_rig(tmp_path)
    flag = recorded.recorder.is_degraded  # what the dashboard heartbeat calls

    def broken(_self: Journal, _entry: journal_module.Entry) -> bool:
        raise RuntimeError("injected: a bug in the recorder's path")

    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await set_target(http, 5.0)
        with monkeypatch.context() as injected, caplog.at_level(logging.ERROR):
            injected.setattr(Journal, "submit", broken)
            await recorded.tick(2.0)
        assert flag() is True
        # The fault is gone, but the record it cost stays incomplete: still said.
        await recorded.tick(2.0)
        assert flag() is True
        await stop(recorded, http)
        assert recorded.state() is RuntimeState.FINISHED
        assert flag() is True, "between two sessions it is about the last record"

        # A new session, a new record, a recorder that works: the flag clears.
        await start_bench(recorded, http)
        assert recorded.state() is RuntimeState.RUNNING
        await recorded.tick(2.0)
        assert flag() is False
        assert not recorded.degraded()
        await stop(recorded, http)
    assert flag() is False
    assert recorded.said() == [BROKEN], "said once, for the record it was about"
    assert len(recorded.records()) == 2
