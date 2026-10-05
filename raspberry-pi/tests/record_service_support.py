import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.ecg import Header, RawBlock
from src.record.messages import Ending, Message, SequenceNumber
from src.record.schema import Profile
from src.record.service import DEFAULT_LIMITS, Limits, Service
from src.record.storage import Generation
from src.record.writer import Writer
from src.result import Ok
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_support import manifest, row


def private_writer(root: Path, names: tuple[str, ...] = ()) -> Writer:
    result = Writer.create(root, manifest(), Privacy(names), require_private=True)
    assert isinstance(result, Ok)
    return result.value


def profile() -> Profile:
    return Profile(
        total_duration_s=600,
        baseline_s=30,
        warmup_max_s=60,
        hold_min_s=30,
        cooldown_s=30,
        recovery_s=30,
        zone_low_bpm=70,
        zone_high_bpm=100,
        hard_max_bpm=130,
        critical_bpm=50,
        subject_hr_max=180,
        min_run_rpm=100,
        max_rpm=600,
        warmup_rpm_ceiling_fraction=0.5,
        channels=("ECG",),
        allow_above_nameplate=False,
        source_rev=1,
        resolved_at=1000,
        total_overridden=False,
    )


def message(sequence: int) -> Message:
    return Message(SequenceNumber(sequence), Monotonic(sequence / 5), row())


def large_raw(sequence: int) -> Message:
    block = RawBlock(
        Header(sequence, sequence, 1000, ("ECG", "EDA", "SpO2", "RESP", "EMG", "LUX")),
        tuple(
            tuple(int(str(1000 + channel * 1000 + i)) for i in range(1000)) for channel in range(6)
        ),
    )
    return Message(SequenceNumber(sequence), Monotonic(sequence), block)


@contextmanager
def prepared_service(root: Path, limits: Limits = DEFAULT_LIMITS) -> Generator[Service]:
    service = Service(root, ManualClock(), limits)
    try:
        assert service.prepare(manifest(), Generation(1))
        assert service.preparation_done.wait(5)
        prepared = service.prepared
        assert prepared is not None
        assert prepared.path is not None
        yield service
    finally:
        service.close(Ending(Monotonic(1), UnixMillis(1000), "done"))
        assert service.join(Seconds(5))


@contextmanager
def stalled_write(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[Event, Event]]:
    entered, release = Event(), Event()
    original = os.write

    def wait(fd: int, content: bytes | memoryview) -> int:
        entered.set()
        assert release.wait(5)
        return original(fd, content)

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", wait)
        try:
            yield entered, release
        finally:
            release.set()
