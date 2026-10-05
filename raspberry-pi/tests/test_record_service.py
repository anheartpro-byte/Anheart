import errno
import os
from _thread import LockType
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import pytest

from src.clock import ManualClock
from src.record import service as service_module
from src.record.budget import FIXED_OVERHEAD, retained_charge
from src.record.messages import (
    Ending,
    Message,
    SequenceNumber,
    Stream,
)
from src.record.reader import read
from src.record.schema import Event as RecordEvent
from src.record.schema import EventKind
from src.record.service import Limits, Service
from src.record.storage import ByteCount, Generation
from src.result import Ok
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_service_support import message, prepared_service, stalled_write
from tests.record_support import manifest


def test_overflow_and_stalled_worker_preserve_close_and_count_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prepared_service(tmp_path, Limits(2, ByteCount(100_000))) as service:
        with stalled_write(monkeypatch) as (entered, release):
            assert service.try_submit(message(0))
            assert entered.wait(5)
            assert service.try_submit(message(1))
            assert service.try_submit(message(2))
            assert not service.try_submit(message(3))
            assert not service.try_submit(message(4))
            status = service.status()
            assert status.degraded
            assert status.queued_items == 2
            assert status.resident_bytes <= FIXED_OVERHEAD + 3 * retained_charge(message(0))
            loss = status.losses[list(Stream).index(Stream.TICK)]
            assert (loss.count, loss.first_sequence, loss.last_sequence) == (2, 3, 4)
            assert (loss.first_at, loss.last_at) == (0.6, 0.8)
            assert service.close(Ending(Monotonic(9), UnixMillis(123_000), "stopped"))
            assert not service.join(Seconds(0))
            release.set()
        assert service.join(Seconds(5))
        prepared = service.prepared
        assert prepared is not None
        assert prepared.path is not None
        loaded = read(Path(prepared.path))
        assert isinstance(loaded, Ok)
        assert len(loaded.value.rows) == 3
        assert loaded.value.manifest.ended_at == "1970-01-01T00:02:03Z"
        assert [e.detail for e in loaded.value.events] == ["recording_loss:tick:2:3..4:0.6..0.8"]
        assert service.status().degraded


def test_byte_overflow_and_lock_contention_return_without_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    charge = retained_charge(message(0))
    locks: list[LockType] = []

    def lock() -> LockType:
        created = Lock()
        locks.append(created)
        return created

    monkeypatch.setattr(service_module, "Lock", lock)
    with (
        prepared_service(tmp_path, Limits(10, ByteCount(charge))) as service,
        stalled_write(monkeypatch) as (entered, _release),
    ):
        assert service.try_submit(message(0))
        assert entered.wait(5)
        assert not service.try_submit(message(1))
        with locks[0]:
            assert not service.try_submit(message(2))
        assert service.status().losses[0].count == 2


def test_unsaturated_adjacent_run_has_no_loss(tmp_path: Path) -> None:
    with prepared_service(tmp_path) as service:
        for index in range(10):
            assert service.try_submit(message(index))
        assert service.close(Ending(Monotonic(2), UnixMillis(2000), "done"))
        assert service.join(Seconds(5))
        assert not service.status().degraded
        assert all(loss.count == 0 for loss in service.status().losses)


def test_fake_clock_deadline_latches_while_disk_is_stalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    service = Service(tmp_path, clock)
    try:
        assert service.prepare(manifest(), Generation(8))
        assert service.preparation_done.wait(5)
        with stalled_write(monkeypatch) as (entered, _release):
            assert service.try_submit(message(0))
            assert entered.wait(5)
            clock.advance(Seconds(2))
            assert service.status().degraded
            assert service.status().checkpoint_at == Monotonic(0)
    finally:
        service.close(Ending(clock.monotonic(), clock.unix_millis(), "done"))
        assert service.join(Seconds(5))
    assert service.status().degraded
    assert service.status().checkpoint_at == Monotonic(2)


@pytest.mark.parametrize(
    "code", [errno.EACCES, errno.EIO, errno.ENOSPC], ids=["eacces", "eio", "enospc"]
)
def test_known_capacity_write_failure_returns_degraded_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def fail(_fd: int, _content: bytes | memoryview) -> int:
        raise OSError(code, "synthetic")

    monkeypatch.setattr(os, "write", fail)
    service = Service(tmp_path, ManualClock())
    try:
        assert service.prepare(manifest(), Generation(2))
        assert service.preparation_done.wait(5)
        prepared = service.prepared
        assert prepared is not None
        assert prepared.capacity.refusal is None
        assert prepared.degraded
        assert prepared.path is None
        assert prepared.error is not None
        assert prepared.error.detail.endswith(f":{code}")
        assert service.status().error == prepared.error
        assert service.status().degraded
    finally:
        service.close(Ending(Monotonic(0), UnixMillis(0), "done"))
        assert service.join(Seconds(5))


def test_stalled_preparation_uses_fresh_generation_and_bounded_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = Event(), Event()
    original = os.fstatvfs

    def stalled(fd: int) -> os.statvfs_result:
        entered.set()
        assert release.wait(5)
        return original(fd)

    monkeypatch.setattr(os, "fstatvfs", stalled)
    clock = ManualClock()
    service = Service(tmp_path, clock)
    try:
        assert service.prepare(manifest(), Generation(99))
        assert entered.wait(5)
        pending = service.prepared
        assert pending is None
        clock.advance(Seconds(3))
        assert service.status().degraded
        assert service.close(Ending(Monotonic(3), UnixMillis(3), "cancelled"))
        assert not service.join(Seconds(0))
    finally:
        release.set()
        assert service.join(Seconds(5))
    prepared = service.prepared
    assert prepared is not None
    assert prepared.capacity.generation == 99
    assert prepared.capacity.observed_at == Monotonic(3)


def test_lifecycle_slots_and_invalid_metadata_are_bounded(tmp_path: Path) -> None:
    service = Service(tmp_path, ManualClock())
    try:
        assert not service.try_submit(message(0))
        assert not service.prepare(replace(manifest(), software_version="x" * 257), Generation(1))
        assert service.prepare(manifest(), Generation(1))
        assert not service.prepare(manifest(), Generation(2))
        assert service.preparation_done.wait(5)
        assert not service.close(Ending(Monotonic(0), UnixMillis(0), "x" * 257))
        bad = Message(
            SequenceNumber(1),
            Monotonic(0),
            RecordEvent(t=0, kind=EventKind.WARNING, detail="x" * 257),
        )
        assert not service.try_submit(bad)
    finally:
        assert service.close(Ending(Monotonic(0), UnixMillis(0), "done"))
        assert service.join(Seconds(5))
    assert not service.close(Ending(Monotonic(0), UnixMillis(0), "done"))
    assert not service.try_submit(message(9))


def test_checkpoint_runs_at_one_second_without_another_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    service = Service(tmp_path, clock)
    entered, release = Event(), Event()
    original = os.fsync

    def stalled(fd: int) -> None:
        entered.set()
        assert release.wait(5)
        original(fd)

    try:
        service.prepare(manifest(), Generation(1))
        assert service.preparation_done.wait(5)
        monkeypatch.setattr(os, "fsync", stalled)
        clock.advance(Seconds(1))
        assert service.try_submit(message(0))
        assert entered.wait(5)
        assert not service.status().degraded
    finally:
        release.set()
        service.close(Ending(clock.monotonic(), clock.unix_millis(), "done"))
        assert service.join(Seconds(5))
    assert service.status().checkpoint_at == Monotonic(1)
    assert not service.status().degraded
