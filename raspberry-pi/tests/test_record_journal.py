"""The session journal: the bounded queue, its one writing thread, and what a bad disk costs.

ANH-128 EX-2 (no write in the producer, a bounded queue, ``fsync`` every 2 s and
never more often) and EX-3 (a write error costs the record, never the session).
Most cases run the journal WITHOUT its thread, one :meth:`Journal.drain` at a
time on a manual clock, so every cadence is exact; the thread itself has its
own cases at the end.
"""

from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import src.record.journal as journal_module
import src.record.writer as writer_module
from src.clock import ManualClock, RealClock
from src.record.codec import Privacy
from src.record.journal import (
    DRAIN_PERIOD,
    FSYNC_PERIOD,
    MIN_FREE_BYTES,
    PROBE_PERIOD,
    PUBLISH_PERIOD,
    STALL_AFTER,
    STOP_TIMEOUT,
    Cause,
    Entry,
    Item,
    Journal,
    Limits,
    Progress,
    RawBatch,
    Scribe,
    Sensors,
    Storage,
    measure,
    raw_block,
)
from src.record.reader import read
from src.record.rows import Row
from src.record.schema import DriveFrame, Event, EventKind, RecordError
from src.record.writer import Writer, describe_os_error
from src.result import Err, Ok, Result
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.types import SignalQuality
from src.units import Monotonic, Seconds
from tests.record_journal_support import (
    batch,
    clock_at_start,
    closing,
    count_fsync,
    journal_threads,
    opened,
    record_of,
    refuse_writes,
    session,
    wait_for,
    watch_tree,
)
from tests.record_support import manifest, row, writer

# =========================================================================
# EX-2: the producer never touches the disk, and the queue is bounded
# =========================================================================


def test_ex2_submit_writes_nothing_until_the_journal_drains(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    assert journal.submit(row())
    assert journal.submit(Event(t=0.2, kind=EventKind.PHASE, detail="hold"))
    assert list(journal.root.iterdir()) == []
    assert journal.status(clock.monotonic()).pending == 3

    journal.drain()

    status = journal.status(clock.monotonic())
    assert status.pending == 0
    assert status.recording
    assert not status.degraded
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert [r.t for r in loaded.value.rows] == [0.2]
    assert [e.kind for e in loaded.value.events] == [EventKind.PHASE]


def test_ex2_the_producer_touches_no_file_and_calls_no_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    fsyncs = count_fsync(monkeypatch)
    with watch_tree(journal.root) as touches:
        journal.open(session(), Privacy())
        for index in range(50):
            journal.submit(replace(row(), t=index * 0.2))
            journal.submit(batch(index))
            journal.status(clock.monotonic())
        journal.close(closing())
        assert touches.seen == []
        assert fsyncs == []
        journal.drain()
        assert touches.seen != []
        assert fsyncs != []


def test_ex2_the_queue_is_bounded_in_entries_and_the_newest_is_refused(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock, Limits(entries=5, samples=1000))
    accepted = [journal.submit(replace(row(), t=index * 0.2)) for index in range(10)]
    # The opening counts: four rows fit behind it.
    assert accepted == [True] * 4 + [False] * 6
    status = journal.status(clock.monotonic())
    assert status.pending == 5
    assert status.dropped == 6
    assert status.cause is Cause.QUEUE_FULL

    journal.drain()

    assert journal.submit(replace(row(), t=9.0))
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert [r.t for r in loaded.value.rows] == [0.0, 0.2, 0.4, 0.6, 9.0]


def test_ex2_raw_samples_have_their_own_bound(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock, Limits(entries=100, samples=500))
    assert journal.submit(batch(0, samples=200))
    assert journal.submit(batch(1, samples=200))
    assert not journal.submit(batch(2, samples=200))
    assert journal.submit(row())
    journal.drain()
    assert journal.submit(batch(3, samples=200))
    journal.drain()
    names = sorted(path.name for path in (record_of(journal) / "ecg_raw").iterdir())
    assert names == ["000000.bin.gz", "000001.bin.gz", "000003.bin.gz"]


def test_ex2_an_opening_and_a_closing_are_never_refused(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock, limits=Limits(entries=1, samples=0))
    journal.open(session(), Privacy())
    assert not journal.submit(row())
    journal.close(closing())
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.end_reason == "operator_stop"
    assert loaded.value.events[0].detail == "record_degraded: dropped=1 failures=0"


def test_ex2_nothing_is_accepted_outside_a_session(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    assert not journal.submit(row())
    journal.open(session(), Privacy())
    journal.close(closing())
    assert not journal.submit(row())
    status = journal.status(clock.monotonic())
    assert not status.recording
    assert status.dropped == 0


def test_ex2_fsync_happens_at_opening_then_every_two_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    fsyncs = count_fsync(monkeypatch)
    journal.drain()
    at_opening = len(fsyncs)
    assert at_opening > 0

    for _ in range(9):
        clock.advance(Seconds(0.2))
        journal.submit(row())
        journal.drain()
    assert len(fsyncs) == at_opening

    clock.advance(Seconds(0.25))  # 2.05 s after the opening
    journal.submit(row())
    journal.drain()
    after_first = len(fsyncs)
    assert after_first > at_opening

    # Nothing written since: the next checkpoint has nothing to flush.
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    assert len(fsyncs) == after_first


@settings(max_examples=40, deadline=None)
@given(steps=st.lists(st.floats(min_value=0.01, max_value=3.0), min_size=1, max_size=40))
def test_ex2_two_fsync_passes_are_never_closer_than_two_seconds(steps: list[float]) -> None:
    clock = clock_at_start()
    passes: list[float] = []
    real = writer_module.fsync_path

    def timed(path: Path) -> None:
        if not passes or passes[-1] != clock.monotonic():
            passes.append(clock.monotonic())
        real(path)

    with tempfile.TemporaryDirectory() as scratch, pytest.MonkeyPatch.context() as patched:
        patched.setattr(writer_module, "fsync_path", timed)
        journal = opened(Path(scratch), clock)
        journal.drain()
        for step in steps:
            clock.advance(Seconds(step))
            journal.submit(row())
            journal.drain()
    assert passes[0] == clock_at_start().monotonic()
    assert all(later - earlier >= FSYNC_PERIOD for earlier, later in pairwise(passes))


# =========================================================================
# The records directory: private, measured, and costing only the record
# =========================================================================


def test_ex7_the_records_directory_is_created_private_to_the_service_user(tmp_path: Path) -> None:
    root = tmp_path / "data" / "records"
    Journal(root, clock_at_start())
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert root.stat().st_uid == os.geteuid()


def test_ex7_an_existing_directory_is_made_private_again_at_every_session(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    root.chmod(0o755)
    journal = Journal(root, clock_at_start())
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    root.chmod(0o755)
    journal.open(session(), Privacy())
    journal.drain()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_ex4_the_free_space_is_measured_at_startup_and_every_five_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = [MIN_FREE_BYTES + 1]

    def measured(_root: Path, at: Monotonic) -> Storage:
        return Storage(free[0], at)

    monkeypatch.setattr(journal_module, "measure", measured)
    clock = clock_at_start()
    started = clock.monotonic()
    journal = Journal(tmp_path / "records", clock)
    assert journal.storage == Storage(MIN_FREE_BYTES + 1, started)

    free[0] = 123
    clock.advance(Seconds(PROBE_PERIOD - 0.1))
    journal.drain()
    assert journal.storage == Storage(MIN_FREE_BYTES + 1, started), "not yet due"
    clock.advance(Seconds(0.1))
    journal.drain()
    # The measurement says when it was taken: whoever reads it judges its age.
    assert journal.storage == Storage(123, clock.monotonic())
    assert journal.status(clock.monotonic()).free_bytes == 123


def test_ex4_the_real_measurement_reads_the_disk_and_says_when_it_cannot(tmp_path: Path) -> None:
    measured = measure(tmp_path, Monotonic(7.0))
    assert measured.free_bytes is not None
    assert measured.free_bytes > 0
    assert measured.measured_at == 7.0
    assert measure(tmp_path / "missing", Monotonic(8.0)) == Storage(None, Monotonic(8.0))


def test_ex3_a_records_path_that_is_a_file_is_unavailable_and_costs_only_the_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "records"
    root.write_text("not a directory")
    clock = clock_at_start()
    journal = Journal(root, clock)
    assert journal.storage == Storage(None, clock.monotonic())
    assert "records directory unusable: FileExistsError:EEXIST" in caplog.text
    idle = journal.status(clock.monotonic())
    assert idle.degraded
    assert idle.cause is Cause.STORAGE_UNAVAILABLE

    journal.open(session(), Privacy())
    assert journal.submit(row())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.cause is Cause.WRITE_FAILED
    assert status.error == RecordError("create", "FileExistsError:EEXIST")
    assert status.failures == 2  # the creation refused, then the row with nowhere to go
    assert status.path is None
    journal.close(closing())
    journal.drain()
    assert root.read_text() == "not a directory"


# =========================================================================
# EX-3: a refused write costs the record, never anything else
# =========================================================================


def test_ex3_a_full_disk_degrades_the_record_and_is_said_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.submit(replace(row(), t=0.0))
    journal.drain()
    disk = refuse_writes(monkeypatch, "ticks.csv", errno.ENOSPC, torn=b"0.2,act")

    for index in range(1, 4):
        clock.advance(Seconds(0.2))
        assert journal.submit(replace(row(), t=index * 0.2))
        journal.drain()

    assert disk.refused == 3
    status = journal.status(clock.monotonic())
    assert status.degraded
    assert status.cause is Cause.WRITE_FAILED
    assert status.failures == 3
    assert status.error == RecordError("append", "OSError:ENOSPC")
    # The torn half-line was cut back each time: the file still ends on a full line.
    ticks = record_of(journal) / "ticks.csv"
    assert ticks.read_bytes().endswith(b"\n")
    assert len(ticks.read_text().splitlines()) == 2

    disk.heal()
    clock.advance(FSYNC_PERIOD)
    journal.submit(replace(row(), t=9.0))
    journal.drain()
    journal.close(closing())
    journal.drain()

    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert [r.t for r in loaded.value.rows] == [0.0, 9.0]
    warnings = [e for e in loaded.value.events if e.kind is EventKind.WARNING]
    assert [w.detail for w in warnings] == [
        "record_degraded: dropped=0 failures=3 last=append:OSError:ENOSPC"
    ]


def test_ex3_the_warning_waits_for_a_disk_that_accepts_it_and_is_said_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    events = record_of(journal) / "events.jsonl"
    disk = refuse_writes(monkeypatch, "ticks.csv", errno.EIO)
    journal.submit(row())
    journal.drain()
    disk.name = "events.jsonl"
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    assert events.read_text() == ""

    disk.heal()
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    assert "record_degraded: dropped=0 failures=1" in events.read_text()
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    assert events.read_text().count("record_degraded") == 1


def test_ex3_a_refused_creation_loses_that_session_only(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    # Somebody else's directory already holds the name this session would take.
    (journal.root / "2026-10-05T101112Z_local-1").mkdir()
    journal.open(session(1), Privacy())
    journal.submit(row())
    journal.close(closing())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.cause is Cause.WRITE_FAILED
    assert status.error == RecordError("create", "FileExistsError:EEXIST")
    assert list((journal.root / "2026-10-05T101112Z_local-1").iterdir()) == []

    journal.open(session(2), Privacy())
    assert not journal.status(clock.monotonic()).degraded
    journal.submit(row())
    journal.close(closing())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert not status.degraded
    assert status.failures == 0
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert len(loaded.value.rows) == 1


def test_ex3_a_fractional_adc_count_is_refused_not_rounded(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.submit(RawBatch(seq=0, t_first=0.0, channels=("ECG",), samples=([1.0, 2.5],)))
    journal.submit(RawBatch(seq=1, t_first=0.2, channels=(), samples=()))
    journal.submit(RawBatch(seq=2, t_first=0.4, channels=("ECG",), samples=([1.0, 2.0],)))
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.failures == 2
    assert status.error is not None
    assert status.error.operation == "raw"
    assert [p.name for p in (record_of(journal) / "ecg_raw").iterdir()] == ["000002.bin.gz"]
    with pytest.raises(ValueError, match="integer ADC counts"):
        raw_block(RawBatch(seq=0, t_first=0.0, channels=("ECG",), samples=([0.5],)))


def test_ex3_a_bug_while_writing_one_value_costs_that_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    real = Writer.tick
    calls = [0]

    def buggy(self: Writer, value: Row) -> Result[None, RecordError]:
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("injected: a bug in the writer")
        return real(self, value)

    monkeypatch.setattr(Writer, "tick", buggy)
    journal.submit(row())
    journal.submit(row())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.failures == 1
    assert status.error == RecordError("append", "RuntimeError")
    assert "one value could not be written" in caplog.text
    assert len((record_of(journal) / "ticks.csv").read_text().splitlines()) == 2


def test_a_value_outside_the_closed_union_costs_itself_and_is_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The ``assert_never`` guard of the writing thread: refused loudly, the thread goes on."""
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.submit(cast("Entry", "not a value of any stream"))
    journal.submit(row())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.failures == 1
    assert status.error == RecordError("append", "AssertionError")
    assert "one value could not be written" in caplog.text
    assert len((record_of(journal) / "ticks.csv").read_text().splitlines()) == 2


def test_ex3_a_failed_fsync_is_a_failure_and_stays_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    journal.submit(row())
    real = os.fsync
    failing = [True]
    flushed: list[int] = []

    def refused(descriptor: int) -> None:
        if failing[0]:
            raise OSError(errno.EIO, "fsync failed")
        flushed.append(descriptor)
        real(descriptor)

    monkeypatch.setattr(os, "fsync", refused)
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.cause is Cause.WRITE_FAILED
    assert status.error == RecordError("sync", "OSError:EIO")

    failing[0] = False
    clock.advance(FSYNC_PERIOD)
    journal.drain()
    assert flushed  # what could not be flushed then is flushed now


def test_ex3_a_closing_that_fails_is_counted_and_the_next_session_still_opens(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    first = record_of(journal)
    (first / "checksums.sha256").write_text("already here")
    journal.close(closing())
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.error == RecordError("close", "FileExistsError:EEXIST")

    journal.open(session(2), Privacy())
    journal.close(closing())
    journal.drain()
    assert not journal.status(clock.monotonic()).degraded
    assert record_of(journal) != first


def test_ex1_a_session_left_open_is_finalised_when_the_next_one_opens(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.submit(row())
    journal.drain()
    first = record_of(journal)
    journal.open(session(2), Privacy())
    journal.drain()
    loaded = read(first)
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.end_reason == "superseded"
    assert loaded.value.warnings == ()
    assert record_of(journal) != first


def test_ex1_every_stream_of_the_format_goes_through_the_queue(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    reading = SensorReading(
        SensorKind.ECG,
        Monotonic(0),
        1,
        (),
        SignalQuality.GOOD,
        "",
        (Metric("rate", "Rate", 61.0, "bpm"),),
    )
    journal.submit(row())
    journal.submit(Event(t=0.2, kind=EventKind.VERDICT, detail="cleared"))
    journal.submit(
        DriveFrame(t=0.2, kind="modbus_read", register=3201, value=39, ok=True, latency_ms=4.0)
    )
    journal.submit(batch(0, samples=4, channels=2))
    journal.submit(Sensors(at=1.0, readings=(reading,)))
    journal.close(closing("programme_complete"))
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    recording = loaded.value
    assert recording.warnings == ()
    assert len(recording.rows) == 1
    assert len(recording.events) == 1
    assert len(recording.frames) == 1
    assert recording.raw[0].header.channels == ("ECG", "EDA")
    assert recording.raw[0].samples == ((0, 1, 2, 3), (0, 1, 2, 3))
    assert recording.sensors[0].value == 61.0
    assert recording.manifest.ended_at == "2026-10-05T10:11:12Z"
    assert recording.manifest.end_reason == "programme_complete"


# =========================================================================
# A disk that does not answer
# =========================================================================


def test_ex3_values_waiting_with_nothing_consumed_are_reported_as_a_stall(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    assert journal.status(clock.monotonic()).cause is None

    journal.submit(row())
    assert journal.status(clock.monotonic()).cause is None
    clock.advance(STALL_AFTER)
    assert journal.status(clock.monotonic()).cause is None
    clock.advance(Seconds(0.2))
    stalled = journal.status(clock.monotonic())
    assert stalled.degraded
    assert stalled.cause is Cause.STALLED
    assert stalled.pending == 1

    journal.drain()
    assert journal.status(clock.monotonic()).cause is None


# =========================================================================
# The writer's own additions
# =========================================================================


def test_writer_sync_flushes_each_written_file_once_and_its_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[str] = []
    real = writer_module.fsync_path

    def seen(path: Path) -> None:
        flushed.append(path.name)
        real(path)

    monkeypatch.setattr(writer_module, "fsync_path", seen)
    recording = writer(tmp_path)
    assert isinstance(recording.sync(), Ok)
    assert sorted(flushed) == sorted(
        [
            "manifest.json",
            "ticks.csv",
            "sensors.csv",
            "events.jsonl",
            "drive_frames.jsonl",
            recording.path.name,
        ]
    )
    flushed.clear()
    assert isinstance(recording.sync(), Ok)
    assert flushed == []

    recording.tick(row())
    recording.raw(raw_block(batch(0, samples=2)))
    assert isinstance(recording.sync(directories=False), Ok)
    assert sorted(flushed) == ["000000.bin.gz", "ticks.csv"]
    flushed.clear()

    recording.raw(raw_block(batch(1, samples=2)))
    assert isinstance(recording.sync(), Ok)
    assert sorted(flushed) == ["000001.bin.gz", "ecg_raw"]
    flushed.clear()

    closed = recording.close(ManualClock(), "done")
    assert isinstance(closed, Ok)
    assert isinstance(recording.sync(), Ok)
    assert sorted(flushed) == sorted(["checksums.sha256", "manifest.json", recording.path.name])


def test_writer_abandons_a_stream_whose_torn_tail_cannot_be_cut_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.tick(row()), Ok)
    disk = refuse_writes(monkeypatch, "ticks.csv", errno.EIO, torn=b"0.4,torn")

    def no_truncate(_path: Path, _length: int) -> None:
        raise OSError(errno.EIO, "truncate failed")

    monkeypatch.setattr(os, "truncate", no_truncate)
    first = recording.tick(row())
    assert isinstance(first, Err)
    assert first.error.detail == "OSError:EIO"
    disk.heal()
    second = recording.tick(row())
    assert isinstance(second, Err)
    assert second.error.detail == "abandoned"
    # The other streams go on, and the torn tail is read as a truncation.
    assert isinstance(recording.event(Event(t=0.4, kind=EventKind.WARNING, detail="x")), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.rows) == 1
    assert [(w.file, w.code) for w in loaded.value.warnings if w.code == "truncated"] == [
        ("ticks.csv", "truncated")
    ]


def test_writer_removes_a_partial_block_and_reads_past_one_it_cannot_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    block = raw_block(batch(0, samples=4))
    refuse_writes(monkeypatch, "000000.bin.gz", errno.ENOSPC, torn=b"\x1f\x8b")
    assert isinstance(recording.raw(block), Err)
    assert list((recording.path / "ecg_raw").iterdir()) == []

    def no_unlink(_self: Path, missing_ok: bool = False) -> None:
        raise OSError(errno.EIO, f"unlink failed (missing_ok={missing_ok})")

    monkeypatch.setattr(Path, "unlink", no_unlink)
    assert isinstance(recording.raw(block), Err)
    assert [p.name for p in (recording.path / "ecg_raw").iterdir()] == ["000000.bin.gz"]
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.raw == ()
    assert ("000000.bin.gz", "truncated") in {(w.file, w.code) for w in loaded.value.warnings}


def test_an_os_error_is_named_by_its_class_and_errno_never_by_its_path() -> None:
    assert describe_os_error(OSError(errno.ENOSPC, "No space", "/secret/path")) == "OSError:ENOSPC"
    assert describe_os_error(PermissionError(errno.EACCES, "x")) == "PermissionError:EACCES"
    assert describe_os_error(OSError("no errno at all")) == "OSError"
    assert describe_os_error(OSError(99_999, "unknown")) == "OSError:99999"


def test_writer_reports_a_refused_creation_by_errno(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the root should be")
    created = Writer.create(blocked, manifest())
    assert isinstance(created, Err)
    assert created.error == RecordError("create", "NotADirectoryError:ENOTDIR")


# =========================================================================
# The thread
# =========================================================================


def test_ex2_the_dedicated_thread_does_every_write_and_every_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = RealClock()
    journal = Journal(tmp_path / "records", clock, period=Seconds(0.01))
    fsyncs = count_fsync(monkeypatch)
    with watch_tree(journal.root) as touches:
        journal.start()
        journal.start()  # a second call starts no second thread
        assert len(journal_threads()) == 1
        writing = journal_threads()[0].ident
        assert writing is not None

        journal.open(session(), Privacy())
        journal.submit(row())
        wait_for(lambda: journal.status(clock.monotonic()).pending == 0)
        path = record_of(journal)
        assert len((path / "ticks.csv").read_text().splitlines()) == 2

        journal.submit(replace(row(), t=0.4))
        journal.close(closing())
        assert journal.stop()
        assert journal_threads() == []

    assert set(fsyncs) == {writing}
    assert touches.by(writing) != []
    # This thread only read the result back (the assertions above).
    assert set(touches.by(threading.get_ident())) <= {"open"}
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert [r.t for r in loaded.value.rows] == [0.2, 0.4]
    assert loaded.value.manifest.end_reason == "operator_stop"


def test_ex2_a_journal_that_was_never_started_still_writes_its_last_drain_on_its_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.submit(row())
    journal.close(closing())
    assert not journal.stopped, "never asked to stop"
    fsyncs = count_fsync(monkeypatch)
    with watch_tree(journal.root) as touches:
        # Asking is all the caller does: this returns at once and touches nothing.
        journal.request_stop()
        wait_for(lambda: journal.stopped)
    assert touches.by(threading.get_ident()) == []
    assert threading.get_ident() not in fsyncs
    assert touches.seen != []
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert loaded.value.manifest.end_reason == "operator_stop"
    assert journal.stop(), "already stopped: nothing left to wait for"


def test_ex3_a_failed_cycle_does_not_raise_and_the_stall_detector_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    real = Scribe.cycle
    failing = [True]

    def broken(
        self: Scribe, queue: deque[Item], dropped: int, publish: Callable[[Progress], None]
    ) -> None:
        if failing[0]:
            raise RuntimeError("injected: a bug in the cycle")
        real(self, queue, dropped, publish)

    monkeypatch.setattr(Scribe, "cycle", broken)
    journal.submit(row())
    assert journal.stop()  # the last cycle fails on its thread, and says so, without raising
    assert "session journal cycle failed" in caplog.text
    journal.status(clock.monotonic())
    clock.advance(Seconds(STALL_AFTER + 0.2))
    assert journal.status(clock.monotonic()).cause is Cause.STALLED

    failing[0] = False
    journal.drain()
    assert journal.status(clock.monotonic()).cause is None


def test_ex2_a_blocked_disk_blocks_the_thread_and_never_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = RealClock()
    journal = Journal(
        tmp_path / "records", clock, limits=Limits(entries=20, samples=0), period=Seconds(0.01)
    )
    release = threading.Event()
    entered = threading.Event()
    real = Writer.tick

    def blocked(self: Writer, value: Row) -> Result[None, RecordError]:
        entered.set()
        release.wait(10.0)
        return real(self, value)

    monkeypatch.setattr(Writer, "tick", blocked)
    journal.start()
    journal.open(session(), Privacy())
    journal.submit(row())
    assert entered.wait(5.0)

    started = time.perf_counter()
    results = [journal.submit(row()) for _ in range(200)]
    journal.status(clock.monotonic())
    spent = time.perf_counter() - started
    assert spent < 0.5, f"200 submissions took {spent:.3f} s behind a blocked disk"
    assert results.count(True) <= 20
    assert journal.status(clock.monotonic()).dropped >= 180

    # A stop cannot wait for a disk that never answers: it gives up, and says so.
    assert journal.stop(Seconds(0.05)) is False
    release.set()
    wait_for(lambda: journal_threads() == [])


def test_ex3_a_long_backlog_on_a_slow_disk_is_progress_not_a_stall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One long cycle publishes its progress as it goes: the producer sees it consume."""
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    journal.drain()
    for index in range(20):
        journal.submit(replace(row(), t=index * 0.2))
    real = Writer.tick
    seen: list[tuple[int, Cause | None]] = []

    def slow(self: Writer, value: Row) -> Result[None, RecordError]:
        # What the loop sees while the disk takes a whole second per row.
        status = journal.status(clock.monotonic())
        seen.append((status.pending, status.cause))
        clock.advance(PUBLISH_PERIOD)
        return real(self, value)

    monkeypatch.setattr(Writer, "tick", slow)
    journal.drain()
    assert [pending for pending, _cause in seen] == list(range(20, 0, -1))
    assert {cause for _pending, cause in seen} == {None}, "twenty seconds of work, no stall"
    assert journal.status(clock.monotonic()).pending == 0


def test_the_documented_periods_and_floor() -> None:
    assert (DRAIN_PERIOD, FSYNC_PERIOD, STOP_TIMEOUT) == (0.2, 2.0, 5.0)
    assert MIN_FREE_BYTES == 500_000_000
