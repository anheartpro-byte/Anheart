import errno
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import assert_never

import pytest

from src.clock import ManualClock
from src.record import service as service_module
from src.record import worker as worker_module
from src.record import writer as writer_module
from src.record.ecg import Header, RawBlock
from src.record.messages import Ending, Loss, Message, Payload, Sensors, SequenceNumber, Stream
from src.record.reader import read
from src.record.rows import Row
from src.record.schema import DriveFrame, Event, EventKind
from src.record.service import Service
from src.record.storage import CapacityRefusal, Generation
from src.record.worker import Worker
from src.result import Err, Ok
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.types import SignalQuality
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_service_support import message, prepared_service
from tests.record_support import manifest, row, writer


@pytest.mark.parametrize(
    "payload",
    [
        row(),
        Event(t=0, kind=EventKind.WARNING, detail="test"),
        DriveFrame(t=0, kind="read", register=10, value=20, ok=True, latency_ms=2),
        RawBlock(Header(0, 0, 1, ("ECG",)), ((456,),)),
        Sensors(
            0,
            (
                SensorReading(
                    SensorKind.ECG,
                    Monotonic(0),
                    1,
                    (),
                    SignalQuality.GOOD,
                    "",
                    (Metric("hr", "HR", 72.0, "bpm"),),
                ),
            ),
        ),
    ],
)
def test_worker_persists_each_schema_stream(tmp_path: Path, payload: Payload) -> None:
    with prepared_service(tmp_path) as service:
        assert service.try_submit(Message(SequenceNumber(0), Monotonic(0), payload))
        service.close(Ending(Monotonic(1), UnixMillis(1000), "done"))
        assert service.join(Seconds(5))
        prepared = service.prepared
        assert prepared is not None
        assert prepared.path is not None
        loaded = read(Path(prepared.path))
        assert isinstance(loaded, Ok)
        assert not service.status().degraded
        assert loaded.value.warnings == ()
        match payload:
            case Row():
                assert loaded.value.rows == (payload,)
            case Event():
                assert loaded.value.events == (payload,)
            case DriveFrame():
                assert loaded.value.frames == (payload,)
            case RawBlock():
                assert loaded.value.raw == (payload,)
            case Sensors():
                assert len(loaded.value.sensors) == 1
                assert loaded.value.sensors[0].channel == "ECG"
                assert loaded.value.sensors[0].value == 72.0
            case _ as unreachable:
                assert_never(unreachable)


def test_capacity_low_prevents_preparation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def low(_fd: int) -> os.statvfs_result:
        return os.statvfs_result((1, 1, 0, 0, 499_999_999, 0, 0, 0, 0, 255))

    monkeypatch.setattr(os, "fstatvfs", low)
    worker = Worker(tmp_path, ManualClock())
    result = worker.prepare(manifest(), Generation(1))
    assert result.capacity.refusal == CapacityRefusal.LOW
    assert result.path is None
    assert isinstance(worker.write(message(0)), Err)
    assert isinstance(worker.checkpoint(()), Err)


def test_known_capacity_root_creation_failure_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_path: str, mode: int, *, dir_fd: int) -> None:
        raise OSError(errno.EACCES, f"synthetic {mode} {dir_fd}")

    monkeypatch.setattr(os, "mkdir", denied)
    result = Worker(tmp_path / "records", ManualClock()).prepare(manifest(), Generation(1))
    assert result.capacity.refusal is None
    assert result.degraded
    assert result.path is None


def test_failed_data_and_warning_writes_latch_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prepared_service(tmp_path) as service:

        def failed(_fd: int, _data: bytes | memoryview) -> int:
            raise OSError(errno.ENOSPC, "synthetic")

        monkeypatch.setattr(os, "write", failed)
        assert service.try_submit(message(0))
        service.close(Ending(Monotonic(1), UnixMillis(1000), "done"))
        assert service.join(Seconds(5))
        assert service.status().degraded
        assert service.status().losses[0].count == 1


def test_encoding_failure_is_isolated_and_healthy_append_remains_possible(tmp_path: Path) -> None:
    worker = Worker(tmp_path, ManualClock())
    worker.prepare(manifest(), Generation(1))

    frame = DriveFrame(t=0, kind="\ud800", register=0, value=1, ok=True, latency_ms=0)
    assert isinstance(worker.write(Message(SequenceNumber(0), Monotonic(0), frame)), Err)
    assert isinstance(worker.write(message(1)), Ok)


def test_writer_closed_raw_and_result_contract_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = writer(tmp_path)
    assert isinstance(recording.close(ManualClock(), "done"), Ok)
    assert isinstance(recording.raw(RawBlock(Header(0, 0, 1, ("ECG",)), ((345,),))), Err)
    other = writer(tmp_path / "other")

    def appended(_name: str, _content: str) -> Ok[None]:
        return Ok(None)

    def checkpointed() -> Ok[None]:
        return Ok(None)

    monkeypatch.setattr(other, "_append", appended)
    monkeypatch.setattr(other, "checkpoint", checkpointed)
    monkeypatch.setattr(writer_module, "Ok", Err)
    with pytest.raises(AssertionError):
        other.sensors(0, ())
    with pytest.raises(AssertionError):
        other.close(ManualClock(), "done")


def test_worker_exhaustiveness_guards_runtime_contract_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = Worker(tmp_path, ManualClock())
    worker.prepare(manifest(), Generation(1))
    with monkeypatch.context() as patch:
        patch.setattr(worker_module, "Row", Event)
        with pytest.raises(AssertionError):
            worker.write(message(0))
    monkeypatch.setattr(worker_module, "Ok", Err)
    with pytest.raises(AssertionError):
        worker.checkpoint(((Stream.TICK, Loss(count=1)),))
    with pytest.raises(AssertionError):
        Worker(tmp_path / "other", ManualClock()).prepare(manifest(), Generation(1))


def test_service_result_corruption_cannot_make_shutdown_wait_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = threading.Event()
    failures: list[type[BaseException]] = []

    def exception(args: threading.ExceptHookArgs) -> None:
        failures.append(args.exc_type)
        observed.set()

    monkeypatch.setattr(threading, "excepthook", exception)
    service = Service(tmp_path, ManualClock())
    assert service.prepare(manifest(), Generation(1))
    assert service.preparation_done.wait(5)
    monkeypatch.setattr(service_module, "Ok", Err)
    assert service.try_submit(message(0))
    assert observed.wait(5)
    assert not service.join(Seconds(1))
    assert failures == [AssertionError]
    assert service.status().degraded


def test_invalid_loss_coordinates_are_bounded(tmp_path: Path) -> None:
    with prepared_service(tmp_path) as service:
        assert not service.try_submit(
            replace(message(0), sequence=SequenceNumber(1 << 10000), at=Monotonic(float("nan")))
        )
        loss = service.status().losses[0]
        assert loss.last_sequence == (1 << 63) - 1
        assert loss.last_at == Monotonic(0)
