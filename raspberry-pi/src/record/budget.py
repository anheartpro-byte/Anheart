"""Bounded producer admission; allocator sizes are measured once at import."""

from math import isfinite
from sys import getsizeof
from typing import Final, assert_never

from src.record.ecg import RawBlock
from src.record.messages import Ending, Message, Sensors
from src.record.rows import JsonScalar, Row
from src.record.schema import DriveFrame, Event, Manifest
from src.record.storage import ByteCount

MAX_SEQUENCE: Final = (1 << 63) - 1
MAX_TEXT: Final = 256
MAX_SAMPLES: Final = 1000
MAX_CHANNELS: Final = 6
MAX_METRICS: Final = 16
MAX_MESSAGE: Final = 512 * 1024
FIXED_OVERHEAD: Final = 2 * 1024 * 1024
TUPLE_BYTES: Final = getsizeof(())
REFERENCE_BYTES: Final = getsizeof((None,)) - TUPLE_BYTES
SCALAR_BYTES: Final = max(getsizeof(MAX_SEQUENCE), getsizeof(0.0)) + 16
TEXT_BYTES: Final = getsizeof("\U00010000")


def scalar_charge(values: tuple[JsonScalar, ...]) -> int:
    if any(
        (isinstance(v, str) and len(v) > MAX_TEXT) or (isinstance(v, int) and abs(v) > MAX_SEQUENCE)
        for v in values
    ):
        return MAX_MESSAGE + 1
    return (
        TUPLE_BYTES
        + len(values) * (SCALAR_BYTES + REFERENCE_BYTES)
        + sum(TEXT_BYTES + 4 * len(v) for v in values if isinstance(v, str))
    )


def retained_charge(message: Message) -> ByteCount:
    """Overcount shared objects, references and allocator/queue bookkeeping."""
    base = 2048
    size = 0
    match message.payload:
        case Row() as r:
            size = scalar_charge(
                (
                    r.t,
                    r.state,
                    r.mode,
                    r.phase,
                    r.drive_state,
                    r.sim_state,
                    r.setpoint_motor_rpm,
                    r.lfrd_motor_rpm,
                    r.measured_motor_rpm,
                    r.measured_fresh,
                    r.output_rpm,
                    r.hertz,
                    r.g_reference,
                    r.g_leg_tip,
                    r.setpoint_output_rpm,
                    r.setpoint_g_leg_tip,
                    r.manual_target_motor_rpm,
                    r.hr_true,
                    r.hr_live,
                    r.target_bpm,
                    r.safety_action,
                    r.safety_rule,
                    r.output_enabled,
                    r.silent,
                    r.current_a,
                    r.hr_raw,
                    r.hr_confirmed,
                    r.hr_quality,
                    r.drive_status_word,
                )
            )
        case Event() as event:
            size = scalar_charge((event.t, event.kind, event.detail, event.actor))
        case DriveFrame() as frame:
            registers = frame.register if isinstance(frame.register, tuple) else (frame.register,)
            values = frame.value if isinstance(frame.value, tuple) else (frame.value,)
            if len(registers) > MAX_METRICS or len(values) > MAX_METRICS:
                return ByteCount(MAX_MESSAGE + 1)
            size = scalar_charge((frame.t, frame.kind, frame.ok, frame.latency_ms, frame.raw_hex))
            size += scalar_charge(registers) + scalar_charge(values)
        case RawBlock() as raw:
            if (
                len(raw.samples) > MAX_CHANNELS
                or len(raw.header.channels) > MAX_CHANNELS
                or any(len(channel) > MAX_SAMPLES for channel in raw.samples)
            ):
                return ByteCount(MAX_MESSAGE + 1)
            size = scalar_charge(
                (
                    *raw.header.channels,
                    raw.header.seq,
                    raw.header.t_first,
                    raw.header.n_samples,
                    raw.header.sample_rate,
                )
            )
            size += (
                TUPLE_BYTES
                + REFERENCE_BYTES * len(raw.samples)
                + sum(scalar_charge(channel) for channel in raw.samples)
            )
        case Sensors() as sensors:
            if len(sensors.readings) > MAX_CHANNELS:
                return ByteCount(MAX_MESSAGE + 1)
            size = TUPLE_BYTES + REFERENCE_BYTES * len(sensors.readings)
            for reading in sensors.readings:
                if len(reading.metrics) > MAX_METRICS or len(reading.waveform) > MAX_SAMPLES:
                    return ByteCount(MAX_MESSAGE + 1)
                size += 1024 + scalar_charge((reading.at, reading.display_rate, reading.detail))
                size += scalar_charge(reading.waveform)
                for metric in reading.metrics:
                    size += 256 + scalar_charge(
                        (metric.key, metric.label, metric.value, metric.unit)
                    )
        case _ as unreachable:
            assert_never(unreachable)
    valid = 0 <= message.sequence <= MAX_SEQUENCE and isfinite(message.at)
    return ByteCount(base + size if valid else MAX_MESSAGE + 1)


def bounded_ending(ending: Ending) -> bool:
    observation = ending.observation
    values: tuple[JsonScalar, ...] = (ending.at, ending.utc, ending.reason)
    if observation is not None:
        values += (
            observation.t,
            observation.runtime_state,
            observation.drive_state,
            observation.runtime_output_enabled,
            observation.runtime_applied_rpm,
            observation.lfrd_motor_rpm,
            observation.shaft_motor_rpm,
            observation.energised,
            observation.silent,
            observation.stop_reason,
            observation.shutdown_detail,
        )
    return isfinite(ending.at) and scalar_charge(values) <= MAX_MESSAGE


def bounded_manifest(manifest: Manifest) -> bool:
    if (
        manifest.end_observation is not None
        or manifest.ended_at is not None
        or manifest.end_reason is not None
    ):
        return False
    texts = (
        manifest.record_id,
        manifest.machine_id,
        manifest.organization_id,
        manifest.session_id,
        manifest.local_ref,
        manifest.operator,
        manifest.subject_id,
        manifest.config_hash,
        manifest.software_version,
        manifest.contract_version,
        manifest.medical_parameters_version,
        manifest.started_at,
        manifest.ended_at,
        manifest.end_reason,
        manifest.clocks.utc_start,
    )
    if scalar_charge(texts) > MAX_MESSAGE:
        return False
    if manifest.preflight is not None and (
        len(manifest.preflight) > MAX_METRICS
        or any(len(item.check) > MAX_TEXT for item in manifest.preflight)
    ):
        return False
    profile = manifest.profile
    if profile is not None:
        integers = (
            profile.zone_low_bpm,
            profile.zone_high_bpm,
            profile.hard_max_bpm,
            profile.critical_bpm,
            profile.subject_hr_max,
            profile.min_run_rpm,
            profile.max_rpm,
            profile.source_rev,
            profile.resolved_at,
        )
        if (
            len(profile.channels) > MAX_CHANNELS
            or scalar_charge(profile.channels) > MAX_MESSAGE
            or any(abs(value) > MAX_SEQUENCE for value in integers)
        ):
            return False
    return manifest.geometry is None or abs(manifest.geometry.nominal_motor_rpm) <= MAX_SEQUENCE
