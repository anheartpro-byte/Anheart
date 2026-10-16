from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import cast

import pytest

from src.clock import ManualClock
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    EnableUnconfirmed,
    StopUnconfirmed,
    UnexpectedState,
)
from src.record.drive_tap import CallTap
from src.record.ecg import Header, RawBlock, RuntimeDelivery, decode_block, encode_block
from src.record.reader import read
from src.record.schema import DriveFrame, RecordedFailure, record_failure, replay_failure
from src.record.writer import FRAME
from src.result import Err, Ok
from src.units import MotorRpm, OutOfRange, RawRegister, Seconds
from tests.record_support import writer
from tests.test_record_drive_frames import Scripted

ERRORS: tuple[DriveError, ...] = (
    CommTimeout(Seconds(0.7)),
    BadResponse("private diagnostic"),
    UnexpectedState(DriveState.READY, DriveState.FAULT),
    DriveFaulted(DriveFault.OVERCURRENT, RawRegister(9)),
    OutOfRange("private diagnostic", 4.0, 0.0, 3.0),
    EnableUnconfirmed("private diagnostic", reference_zeroed=True, run_command_removed=False),
    StopUnconfirmed(Seconds(2.5), MotorRpm(420), "private diagnostic"),
)


def failed(error: DriveError) -> DriveFrame:
    return DriveFrame(
        t=0.0,
        kind="open",
        register=None,
        value=None,
        ok=False,
        latency_ms=25.0,
        error=record_failure(error),
    )


@pytest.mark.parametrize("error", ERRORS)
def test_named_failure_roundtrip_keeps_decision_fields_and_never_prose(error: DriveError) -> None:
    frame = failed(error)
    raw = FRAME.dump_json(frame)
    assert b"private diagnostic" not in raw
    parsed = FRAME.validate_json(raw)
    assert parsed == frame
    assert parsed.error is not None
    restored = replay_failure(parsed.error)
    assert type(restored) is type(error)
    assert record_failure(restored) == record_failure(error)


@pytest.mark.parametrize("error", ERRORS)
async def test_call_tap_keeps_the_named_failure_for_every_fallible_call(error: DriveError) -> None:
    clock = ManualClock()
    inner = Scripted(clock)
    inner.written = Err(error)
    inner.status = Err(error)
    inner.limits = Err(error)
    tap = CallTap(inner, clock)
    answers = (
        await tap.open(),
        await tap.close(),
        await tap.write_command(ControlWord.SHUTDOWN),
        await tap.write_speed(MotorRpm(0)),
        await tap.read_status(),
        await tap.read_limits(),
    )
    assert all(answer == Err(error) for answer in answers)
    observations = tap.take().observations
    assert len(observations) == 6
    assert all(item.error == record_failure(error) for item in observations)
    assert all(
        item.frame(clock.monotonic()).error == record_failure(error) for item in observations
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "UnknownFailure"},
        {"type": "CommTimeout", "after": "0.7"},
        {"type": "CommTimeout", "after": True},
        {"type": "CommTimeout", "after": -1.0},
        {"type": "CommTimeout", "after": float("inf")},
        {"type": "EnableUnconfirmed", "reference_zeroed": 1, "run_command_removed": False},
        {"type": "EnableUnconfirmed", "reference_zeroed": True},
        {"type": "StopUnconfirmed", "waited": 1.0, "last_output_rpm": 1.5},
        {"type": "DriveFaulted", "fault": "UNKNOWN", "raw_code": 65536},
        {"type": "DriveFaulted", "fault": "unknown", "raw_code": 9},
        {"type": "DriveFaulted", "fault": 1, "raw_code": 9},
        {"type": "UnexpectedState", "expected": "READY", "actual": "unknown"},
        {"type": "UnexpectedState", "expected": 1, "actual": "FAULT"},
        {"type": "BadResponse", "detail": "private diagnostic"},
    ],
)
def test_reader_refuses_unknown_or_malformed_failure(tmp_path: Path, payload: object) -> None:
    recording = writer(tmp_path)
    raw = {
        "t": 0,
        "kind": "open",
        "register": None,
        "value": None,
        "ok": False,
        "latency_ms": 0,
        "error": payload,
    }
    (recording.path / "drive_frames.jsonl").write_text(json.dumps(raw) + "\n", encoding="utf-8")
    assert isinstance(recording.close(ManualClock(), "test"), Ok)
    assert isinstance(read(recording.path), Err)


def test_success_cannot_claim_a_failure_and_absent_field_stays_absent() -> None:
    with pytest.raises(ValueError, match="successful"):
        DriveFrame(
            t=0,
            kind="open",
            register=None,
            value=None,
            ok=True,
            latency_ms=0,
            error=record_failure(ERRORS[0]),
        )
    old = DriveFrame(t=0, kind="open", register=None, value=None, ok=True, latency_ms=0)
    assert b'"error"' not in FRAME.dump_json(old)


def test_exhaustive_failure_matches_reject_ill_typed_callers() -> None:
    with pytest.raises(AssertionError):
        record_failure(cast("DriveError", object()))
    with pytest.raises(AssertionError):
        replay_failure(cast("RecordedFailure", object()))


def test_delivery_header_roundtrip_preserves_both_instants_and_old_bytes() -> None:
    header = Header(
        0,
        0.0,
        2,
        ("ECG",),
        t_received=0.2,
        runtime_delivery=RuntimeDelivery(t=0.45, t_observed=0.25),
    )
    block = RawBlock(header, ((1, 2),))
    assert decode_block(encode_block(block)) == block
    old = encode_block(RawBlock(Header(0, 0.0, 2, ("ECG",)), ((1, 2),)))
    assert b"runtime_delivery" not in gzip.decompress(old)
    assert b"t_received" not in gzip.decompress(old)


@pytest.mark.parametrize(
    "delivery",
    [
        {"t": 0.3},
        {"t_observed": 0.25},
        {"t": "0.3", "t_observed": 0.25},
        {"t": True, "t_observed": 0.25},
        {"t": 0.3, "t_observed": "0.25"},
        {"t": float("nan"), "t_observed": 0.25},
        {"t": 0.3, "t_observed": float("inf")},
        {"t": 0.3, "t_observed": 0.4},
        {"t": 0.3, "t_observed": 0.1},
        {"t": 0.3, "t_observed": 0.25, "extra": 1},
    ],
)
def test_reader_refuses_malformed_or_reversed_delivery(tmp_path: Path, delivery: object) -> None:
    recording = writer(tmp_path)
    header = {
        "seq": 0,
        "t_first": 0.0,
        "n_samples": 1,
        "channels": ["ECG"],
        "t_received": 0.2,
        "runtime_delivery": delivery,
    }
    raw = gzip.compress(json.dumps(header).encode() + b"\n\x01\x00")
    (recording.path / "ecg_raw" / "000000.bin.gz").write_bytes(raw)
    assert isinstance(recording.close(ManualClock(), "test"), Ok)
    assert isinstance(read(recording.path), Err)


def test_delivery_without_acquisition_time_is_valid_and_old_record_is_readable(
    tmp_path: Path,
) -> None:
    recording = writer(tmp_path)
    block = RawBlock(
        Header(0, 0.0, 1, ("ECG",), runtime_delivery=RuntimeDelivery(t=0.4, t_observed=0.3)),
        ((1,),),
    )
    assert isinstance(recording.raw(block), Ok)
    assert isinstance(recording.close(ManualClock(), "test"), Ok)
    loaded = read(recording.path)
    assert isinstance(loaded, Ok)
    assert loaded.value.raw == (block,)
