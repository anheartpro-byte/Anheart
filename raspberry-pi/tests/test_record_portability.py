import errno
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from src.clock import ManualClock
from src.record.ecg import Header, RawBlock
from src.record.messages import Ending
from src.record.reader import read
from src.record.schema import Event, EventKind
from src.record.service import Service
from src.record.storage import Generation, private_supported
from src.record.writer import Writer
from src.result import Err, Ok
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_support import manifest, row


@pytest.mark.parametrize("nested", [False, True])
def test_shared_writer_accepts_actual_temporary_directory(nested: bool) -> None:
    with TemporaryDirectory(prefix="anh128-shared-") as temporary:
        root = Path(temporary) / "new_parent" / "records" if nested else Path(temporary)
        created = Writer.create(root, manifest())
        assert isinstance(created, Ok)
        assert isinstance(created.value.tick(row()), Ok)
        assert isinstance(created.value.close(ManualClock(), "done"), Ok)
        loaded = read(created.value.path)
        assert isinstance(loaded, Ok)
        assert loaded.value.rows == (row(),)
        assert loaded.value.warnings == ()


def test_shared_writer_creates_missing_nested_root(tmp_path: Path) -> None:
    created = Writer.create(tmp_path / "new_parent" / "records", manifest())
    assert isinstance(created, Ok)
    assert isinstance(created.value.tick(row()), Ok)
    assert isinstance(created.value.close(ManualClock(), "done"), Ok)
    loaded = read(created.value.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)
    assert loaded.value.warnings == ()


@pytest.mark.parametrize(
    "capability", ["O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "geteuid", "fchmod", "fstatvfs"]
)
def test_shared_writer_roundtrip_without_posix_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    monkeypatch.delattr(os, capability)
    result = Writer.create(tmp_path / "nested" / "records", manifest())
    assert isinstance(result, Ok)
    writer = result.value
    event = Event(t=1, kind=EventKind.WARNING, detail="synthetic")
    block = RawBlock(Header(1, 1, 2, ("ECG",)), ((345, 678),))
    assert isinstance(writer.tick(row()), Ok)
    assert isinstance(writer.event(event), Ok)
    assert isinstance(writer.raw(block), Ok)
    assert isinstance(writer.raw(block), Err)
    assert isinstance(Writer.create(tmp_path / "nested" / "records", manifest()), Err)
    assert isinstance(writer.close(ManualClock(), "done"), Ok)
    loaded = read(writer.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.rows == (row(),)
    assert loaded.value.events == (event,)
    assert loaded.value.raw == (block,)
    assert loaded.value.manifest.end_reason == "done"
    assert loaded.value.warnings == ()


@pytest.mark.parametrize(
    "capability", ["O_DIRECTORY", "O_NOFOLLOW", "geteuid", "fchmod", "fstatvfs"]
)
def test_private_service_refuses_missing_capability_without_creating_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    monkeypatch.delattr(os, capability)
    root = tmp_path / "records"
    direct = Writer.create(root, manifest(), require_private=True)
    assert isinstance(direct, Err)
    assert direct.error.detail == "private_storage_unavailable"
    service = Service(root, ManualClock())
    try:
        assert service.prepare(manifest(), Generation(7))
        assert service.preparation_done.wait(5)
        prepared = service.prepared
        assert prepared is not None
        assert prepared.path is None
        assert prepared.degraded
        assert prepared.capacity.available is None
        assert prepared.error is not None
        assert prepared.error.detail == "private_storage_unavailable"
        assert service.status().degraded
        assert not root.exists()
    finally:
        service.close(Ending(Monotonic(0), UnixMillis(0), "done"))
        assert service.join(Seconds(1))


@pytest.mark.parametrize("capability", ["supports_dir_fd", "supports_fd"])
def test_private_descriptor_support_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    unsupported: set[Callable[[], None]] = set()
    monkeypatch.setattr(os, capability, unsupported)
    assert not private_supported()
    result = Writer.create(tmp_path, manifest(), require_private=True)
    assert isinstance(result, Err)
    assert result.error.detail == "private_storage_unavailable"


def test_portable_write_failure_keeps_result_and_abandoned_tail_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "O_DIRECTORY")
    created = Writer.create(tmp_path, manifest())
    assert isinstance(created, Ok)

    def failed(_fd: int, _content: bytes | memoryview) -> int:
        raise OSError(errno.ENOSPC, "synthetic")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", failed)
        assert isinstance(created.value.tick(row()), Err)
    assert isinstance(created.value.tick(row()), Err)
    assert isinstance(created.value.close(ManualClock(), "done"), Err)


def test_private_writer_never_downgrades_after_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = Writer.create(tmp_path, manifest(), require_private=True)
    assert isinstance(created, Ok)
    monkeypatch.delattr(os, "O_DIRECTORY")
    result = created.value.tick(row())
    assert isinstance(result, Err)
    assert result.error.detail.endswith(f":{errno.ENOTSUP}")
    assert (created.value.path / "ticks.csv").read_text().count("\n") == 1
