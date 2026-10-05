import tracemalloc
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from src.record import budget as budget_module
from src.record import messages as messages_module
from src.record.budget import (
    FIXED_OVERHEAD,
    MAX_MESSAGE,
    bounded_ending,
    bounded_manifest,
    retained_charge,
)
from src.record.ecg import Header, RawBlock
from src.record.messages import (
    Ending,
    Message,
    Payload,
    Sensors,
    SequenceNumber,
    Stream,
)
from src.record.schema import DriveFrame, EndObservation, Event, EventKind, Preflight
from src.record.service import DEFAULT_LIMITS
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.types import SignalQuality
from src.units import Monotonic, UnixMillis
from tests.record_service_support import (
    large_raw,
    message,
    prepared_service,
    profile,
    stalled_write,
)
from tests.record_support import manifest, row
from tests.test_record_context import geometry


def test_real_distinct_raw_allocations_fit_conservative_charge(
    record_property: Callable[[str, int], None],
) -> None:
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    block = RawBlock(
        Header(1234, 1.234, 1000, ("ECG", "EDA", "SpO2", "RESP", "EMG", "LUX")),
        tuple(
            tuple(int(str(1000 + channel * 1000 + i)) for i in range(1000)) for channel in range(6)
        ),
    )
    message = Message(SequenceNumber(1234), Monotonic(1.234), block)
    retained, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    charge = retained_charge(message)
    assert len({id(value) for channel in block.samples for value in channel}) == 6000
    assert retained - baseline <= charge <= MAX_MESSAGE
    record_property("retained_bytes", retained - baseline)
    record_property("peak_bytes", peak - baseline)
    record_property("charged_bytes", charge)


@pytest.mark.parametrize(
    ("payload", "stream"),
    [
        (row(), Stream.TICK),
        (Event(t=0, kind=EventKind.WARNING, detail="z" * 256, actor="a" * 256), Stream.EVENT),
        (
            DriveFrame(t=0, kind="read", register=(1, 2), value=(3, None), ok=True, latency_ms=1),
            Stream.FRAME,
        ),
        (RawBlock(Header(0, 0, 1, ("ECG",)), ((456,),)), Stream.RAW),
        (
            Sensors(
                0,
                (
                    SensorReading(
                        SensorKind.ECG,
                        Monotonic(0),
                        1,
                        (1.2,),
                        SignalQuality.GOOD,
                        "",
                        (Metric("hr", "HR", 70.0, "bpm"),),
                    ),
                ),
            ),
            Stream.SENSOR,
        ),
    ],
)
def test_payload_charges_and_streams(payload: Payload, stream: Stream) -> None:
    message = Message(SequenceNumber(0), Monotonic(0), payload)
    assert message.stream == stream
    assert 0 < retained_charge(message) <= MAX_MESSAGE


@pytest.mark.parametrize(
    "payload",
    [
        replace(row(), phase="x" * 257),
        DriveFrame(t=0, kind="read", register=tuple(range(17)), value=0, ok=True, latency_ms=1),
        DriveFrame(t=0, kind="read", register=0, value=tuple(range(17)), ok=True, latency_ms=1),
        RawBlock(Header(0, 0, 1, ("ECG",)), ((456,),) * 7),
        RawBlock(Header(0, 0, 1, ("ECG",) * 7), ((456,),)),
        RawBlock(Header(0, 0, 1001, ("ECG",)), ((456,) * 1001,)),
        Sensors(
            0, (SensorReading(SensorKind.ECG, Monotonic(0), 1, (), SignalQuality.GOOD, "", ()),) * 7
        ),
        Sensors(
            0,
            (
                SensorReading(
                    SensorKind.ECG, Monotonic(0), 1, (1.0,) * 1001, SignalQuality.GOOD, "", ()
                ),
            ),
        ),
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
                    (Metric("x", "X", 0.0, ""),) * 17,
                ),
            ),
        ),
    ],
)
def test_oversized_payloads_are_refused(payload: Payload) -> None:
    assert retained_charge(Message(SequenceNumber(0), Monotonic(0), payload)) > MAX_MESSAGE


@pytest.mark.parametrize(("sequence", "at"), [(-1, 0.0), (1 << 70, 0.0), (0, float("nan"))])
def test_invalid_envelope_is_refused(sequence: int, at: float) -> None:
    assert retained_charge(Message(SequenceNumber(sequence), Monotonic(at), row())) > MAX_MESSAGE


def test_lifecycle_metadata_limits() -> None:
    assert bounded_manifest(manifest())
    assert bounded_manifest(replace(manifest(), preflight=(Preflight(check="x", passed=True),)))
    assert not bounded_manifest(
        replace(manifest(), preflight=(Preflight(check="x", passed=True),) * 17)
    )
    assert not bounded_manifest(
        replace(manifest(), preflight=(Preflight(check="x" * 257, passed=True),))
    )
    end = Ending(Monotonic(1), UnixMillis(1000), "done", EndObservation(t=1, shutdown_detail="x"))
    assert bounded_ending(end)
    assert not bounded_ending(
        replace(end, observation=EndObservation(t=1, shutdown_detail="x" * 257))
    )
    assert not bounded_ending(replace(end, at=Monotonic(float("nan"))))
    assert end.monotonic() == Monotonic(1)


def test_preparation_profile_geometry_and_open_state_bounds() -> None:
    base = replace(manifest(), profile=profile(), geometry=geometry())
    assert bounded_manifest(base)
    assert not bounded_manifest(replace(base, profile=replace(profile(), channels=("ECG",) * 7)))
    assert not bounded_manifest(replace(base, profile=replace(profile(), channels=("x" * 257,))))
    assert not bounded_manifest(replace(base, profile=replace(profile(), resolved_at=1 << 70)))
    assert not bounded_manifest(
        replace(base, geometry=replace(geometry(), nominal_motor_rpm=1 << 70))
    )
    assert not bounded_manifest(replace(base, end_observation=EndObservation(t=0)))
    assert not bounded_manifest(replace(base, ended_at="2026-10-05T10:12:00Z"))
    assert not bounded_manifest(replace(base, end_reason="done"))


def test_payload_exhaustiveness_guards_runtime_contract_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = Message(SequenceNumber(0), Monotonic(0), row())
    monkeypatch.setattr(messages_module, "Row", Event)
    monkeypatch.setattr(budget_module, "Row", Event)
    with pytest.raises(AssertionError):
        retained_charge(message)
    with pytest.raises(AssertionError):
        _ = message.stream


def test_stalled_full_queue_actual_allocations_fit_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, int], None]
) -> None:
    with prepared_service(tmp_path) as service, stalled_write(monkeypatch) as (entered, release):
        assert service.try_submit(message(0))
        assert entered.wait(5)
        tracemalloc.start()
        baseline, _ = tracemalloc.get_traced_memory()
        accepted = sum(service.try_submit(large_raw(index)) for index in range(64))
        retained, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        status = service.status()
        assert 0 < accepted < 64
        assert status.losses[list(Stream).index(Stream.RAW)].count == 64 - accepted
        assert retained - baseline <= status.resident_bytes
        assert peak - baseline <= DEFAULT_LIMITS.resident_bytes + FIXED_OVERHEAD
        record_property("queue_accepted", accepted)
        record_property("queue_retained_bytes", retained - baseline)
        record_property("queue_peak_bytes", peak - baseline)
        record_property("declared_resident_bound", DEFAULT_LIMITS.resident_bytes + FIXED_OVERHEAD)
        release.set()


def test_maximum_unicode_metadata_allocations_are_charged(
    record_property: Callable[[str, int], None],
) -> None:
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    event = Event(
        t=0,
        kind=EventKind.WARNING,
        detail="".join(chr(0x10000 + index) for index in range(256)),
        actor="".join(chr(65 + index % 26) for index in range(256)),
    )
    message = Message(SequenceNumber(0), Monotonic(0), event)
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    charge = retained_charge(message)
    assert retained - baseline <= charge <= MAX_MESSAGE
    record_property("metadata_retained_bytes", retained - baseline)
    record_property("metadata_charge", charge)
