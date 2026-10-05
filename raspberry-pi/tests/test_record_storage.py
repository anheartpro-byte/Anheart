import errno
import os
import stat
from io import FileIO
from pathlib import Path
from typing import override

import pytest

from src.clock import ManualClock
from src.record import storage
from src.record.ecg import Header, RawBlock
from src.record.reader import read
from src.record.storage import CapacityRefusal, Generation
from src.record.worker import Worker
from src.record.writer import Writer
from src.result import Err, Ok
from tests.record_service_support import private_writer as writer
from tests.record_support import manifest, row


def test_durable_private_permissions_and_readback(tmp_path: Path) -> None:
    recording = writer(tmp_path / "records")
    assert isinstance(recording.tick(row()), Ok)
    assert isinstance(recording.raw(RawBlock(Header(0, 0, 2, ("ECG",)), ((321, 432),))), Ok)
    assert isinstance(recording.checkpoint(), Ok)
    assert isinstance(recording.close(ManualClock(), "done"), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)
    assert loaded.value.raw[0].samples == ((321, 432),)
    for path in (tmp_path / "records", recording.path, *recording.path.rglob("*")):
        info = path.stat()
        assert info.st_uid == os.geteuid()
        assert stat.S_IMODE(info.st_mode) == (0o700 if path.is_dir() else 0o600)


@pytest.mark.parametrize(
    ("available", "reason"), [(500_000_000, None), (499_999_999, CapacityRefusal.LOW)]
)
def test_capacity_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, available: int, reason: CapacityRefusal | None
) -> None:
    def measured(_fd: int) -> os.statvfs_result:
        return os.statvfs_result((1, 1, 0, 0, available, 0, 0, 0, 0, 255))

    monkeypatch.setattr(os, "fstatvfs", measured)
    observed = storage.capacity(tmp_path, ManualClock(), Generation(7))
    assert observed.available == available
    assert observed.refusal == reason
    assert observed.generation == 7
    prepared = Worker(tmp_path, ManualClock()).prepare(manifest(), Generation(7))
    assert (prepared.path is not None) == (reason is None)


def test_capacity_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_fd: int) -> os.statvfs_result:
        raise OSError(errno.EIO, "synthetic")

    monkeypatch.setattr(os, "fstatvfs", fail)
    assert (
        storage.capacity(tmp_path, ManualClock(), Generation(1)).refusal == CapacityRefusal.UNKNOWN
    )


def test_permissions_reject_symlinks_without_changing_ancestors(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    assert isinstance(Writer.create(link, manifest(), require_private=True), Err)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755
    recording = writer(tmp_path / "records")
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    target = recording.path / "ticks.csv"
    target.unlink()
    target.symlink_to(outside / "sentinel")
    assert isinstance(recording.tick(row()), Err)
    assert not (outside / "sentinel").exists()


def test_permissions_reject_foreign_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    actual = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual + 1)
    assert isinstance(Writer.create(tmp_path, manifest(), require_private=True), Err)


def test_permissions_reject_hardlinked_member(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    os.link(recording.path / "ticks.csv", tmp_path / "linked")
    assert isinstance(recording.tick(row()), Err)


@pytest.mark.parametrize(
    "code", [errno.EACCES, errno.EIO, errno.ENOSPC], ids=["eacces", "eio", "enospc"]
)
def test_partial_failure_abandons_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    recording = writer(tmp_path)
    actual = os.write

    def fail(fd: int, content: bytes | memoryview) -> int:
        actual(fd, content[:7])
        raise OSError(code, "synthetic")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail)
        assert isinstance(recording.tick(row()), Err)
    failed = (recording.path / "ticks.csv").read_bytes()
    assert isinstance(recording.tick(row()), Err)
    assert (recording.path / "ticks.csv").read_bytes() == failed
    assert isinstance(recording.close(ManualClock(), "done"), Err)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == ()
    assert any(w.code == "truncated" for w in loaded.value.warnings)


def test_partial_write_completes_and_zero_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    actual = os.write

    def short(fd: int, content: bytes | memoryview) -> int:
        return actual(fd, content[:3])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short)
        assert isinstance(recording.tick(row()), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)

    def zero(_fd: int, _content: bytes | memoryview) -> int:
        return 0

    monkeypatch.setattr(os, "write", zero)
    assert isinstance(recording.tick(row()), Err)


def test_fsync_failure_is_never_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recording = writer(tmp_path)
    recording.tick(row())

    def fail(_fd: int) -> None:
        raise OSError(errno.EIO, "synthetic")

    monkeypatch.setattr(os, "fsync", fail)
    assert isinstance(recording.checkpoint(), Err)
    assert isinstance(recording.close(ManualClock(), "done"), Err)
    assert isinstance(recording.raw(RawBlock(Header(0, 0, 1, ("ECG",)), ((333,),))), Err)
    assert not tuple((recording.path / "ecg_raw").glob("*.bin.gz"))


def test_durable_publish_orders_sync_before_replace_and_parent_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    events: list[str] = []
    original_sync = os.fsync
    original_replace = os.replace

    class TracedFile(FileIO):
        @override
        def flush(self) -> None:
            events.append("flush")
            super().flush()

    def opened(fd: int, mode: str, *, buffering: int) -> FileIO:
        assert buffering == 0
        return TracedFile(fd, mode)

    def synced(fd: int) -> None:
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_sync(fd)

    def replaced(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
        events.append("replace")
        original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    monkeypatch.setattr(os, "fdopen", opened)
    assert isinstance(recording.close(ManualClock(), "done"), Ok)
    assert events.index("flush") < events.index("file") < events.index("replace")
    assert events[events.index("replace") + 1] == "directory"
    assert events[-1] == "directory"


def test_digest_uses_fixed_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recording = writer(tmp_path)
    for _ in range(1000):
        recording.tick(row())

    def forbidden(_path: Path) -> bytes:
        raise AssertionError("whole-file allocation")

    class BoundedFile(FileIO):
        @override
        def read(self, size: int | None = -1) -> bytes:
            assert size == storage.HASH_BLOCK
            return super().read(size)

    def opened(fd: int, mode: str, *, buffering: int) -> FileIO:
        assert buffering == 0
        return BoundedFile(fd, mode)

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(os, "fdopen", opened)
    assert isinstance(recording.close(ManualClock(), "done"), Ok)


def test_manifest_symlink_is_refused_on_close(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("sentinel")
    target = recording.path / "manifest.json"
    target.unlink()
    target.symlink_to(outside)
    assert isinstance(recording.close(ManualClock(), "done"), Err)
    assert outside.read_text() == "sentinel"


def test_error_exhaustiveness_guard_detects_runtime_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage, "OSError", UnicodeError, raising=False)
    with pytest.raises(AssertionError):
        storage.failure_detail(OSError(errno.EIO, "synthetic"))
