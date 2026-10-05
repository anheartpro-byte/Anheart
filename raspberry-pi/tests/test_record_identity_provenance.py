import hashlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.local_config import CloudConfig
from src.local_panel import build_panel, load_panel_motion_limits
from src.record import context as context_module
from src.record.config import DEFAULT_RECORDING, MachineId, OrganizationId, RecordingConfig
from src.record.context import ContextUnavailable, SessionRequest, medical_version, session_context
from src.record.rows import JsonValue
from src.record.writer import MANIFEST
from src.result import Err, Ok
from src.training.plan import Program, StoreRev
from src.training.types import Occupancy
from src.units import Hertz, Monotonic, Seconds, UnixMillis
from tests.record_identity_support import (
    configured,
    encoded,
    medical_path,
    parameters_for,
    source_for,
)


def test_provisioned_offline_context_has_real_ids_and_unknown_facts() -> None:
    source = source_for(configured())
    context = session_context(
        source, SessionRequest(Occupancy.BENCH), ManualClock(Monotonic(10), UnixMillis(1000))
    )
    assert isinstance(context.manifest, Ok)
    manifest = context.manifest.value
    assert manifest.machine_id == "machine-a"
    assert manifest.organization_id == "organization-b"
    assert manifest.local_ref == context.local_ref == manifest.record_id
    assert manifest.started_at == "1970-01-01T00:00:11Z"
    assert manifest.clocks.monotonic_start == Monotonic(10)
    assert manifest.clocks.ntp_offset_s is None
    assert manifest.preflight is None
    assert manifest.geometry is None
    assert manifest.subject_id is None
    assert manifest.session_id is None
    assert manifest.profile is None
    assert MANIFEST.validate_json(encoded(context)) == manifest
    with pytest.raises(FrozenInstanceError):
        context.__setattr__("local_ref", "changed")


@pytest.mark.parametrize(
    "configuration",
    [
        DEFAULT_RECORDING,
        replace(DEFAULT_RECORDING, machine_id=MachineId("machine-a")),
        replace(DEFAULT_RECORDING, organization_id=OrganizationId("org-b")),
    ],
)
def test_missing_identity_is_unavailable_without_placeholder_values(
    configuration: RecordingConfig,
) -> None:
    source = replace(source_for(configured()), configuration=configuration)
    context = session_context(source, SessionRequest(Occupancy.BENCH), ManualClock())
    assert isinstance(context.manifest, Err)
    assert context.manifest.error.code == ContextUnavailable.IDENTITY_MISSING
    assert context.local_ref


def test_absent_source_remains_unknown() -> None:
    context = session_context(None, SessionRequest(Occupancy.BENCH), ManualClock())
    assert isinstance(context.manifest, Err)
    assert context.manifest.error.code == ContextUnavailable.SOURCE_MISSING


def test_numeric_config_counterexamples_change_fingerprint_but_secrets_do_not() -> None:
    config = configured()
    baseline = source_for(config).config_hash
    assert source_for(configured(ARM_RADIUS_M="1.7")).config_hash != baseline
    assert source_for(configured(HR_HARD_MAX_BPM="147")).config_hash != baseline
    sensitive = "private-token-sentinel"
    secret = replace(
        config,
        cloud=CloudConfig("https://example.invalid", "private-key-sentinel"),
        web=replace(config.web, token=sensitive),
    )
    assert source_for(secret).config_hash == baseline
    content = encoded(
        session_context(source_for(secret), SessionRequest(Occupancy.BENCH), ManualClock())
    )
    assert sensitive not in content
    assert "private-key-sentinel" not in content


def test_numeric_transport_and_acquisition_rates_affect_fingerprint() -> None:
    config = configured(MOTOR_BACKEND="serial", MOTOR_PORT="/dev/synthetic-one")
    base = source_for(config).config_hash
    assert (
        source_for(
            configured(
                MOTOR_BACKEND="serial", MOTOR_PORT="/dev/synthetic-one", MODBUS_BAUDRATE="38400"
            )
        ).config_hash
        != base
    )
    assert (
        source_for(
            configured(MOTOR_BACKEND="serial", MOTOR_PORT="/dev/synthetic-other")
        ).config_hash
        == base
    )
    changed = replace(parameters_for(config), dsp_output_rate=Hertz(200))
    assert context_module.context_source(config, changed, medical_path(config)).config_hash != base


def test_selected_medical_artifact_changes_both_version_and_numeric_fingerprint(
    tmp_path: Path,
) -> None:
    original = configured()
    payload = medical_path(original).read_bytes().replace(b'"g_per_s": 0.03', b'"g_per_s": 0.04')
    chosen = tmp_path / "selected-motion.json"
    chosen.write_bytes(payload)
    configured_source = source_for(replace(original, motion_limits_path=chosen))
    assert configured_source.medical_version == hashlib.sha256(payload).hexdigest()
    assert configured_source.medical_version != source_for(original).medical_version
    assert configured_source.config_hash != source_for(original).config_hash
    assert medical_version(chosen, load_panel_motion_limits(original)) is None


@pytest.mark.parametrize(
    "payload", [b"{broken", b"[]", b" " * 65537], ids=["malformed", "wrong-shape", "oversize"]
)
def test_invalid_or_oversize_medical_source_is_never_labeled_as_defaults(
    tmp_path: Path, payload: bytes
) -> None:
    path = tmp_path / "parameters.json"
    path.write_bytes(payload)
    source = source_for(configured())
    version = medical_version(path, load_panel_motion_limits(configured()))
    assert version is None
    context = session_context(
        replace(source, medical_version=version), SessionRequest(Occupancy.BENCH), ManualClock()
    )
    assert isinstance(context.manifest, Err)
    assert context.manifest.error.code == ContextUnavailable.MEDICAL_UNAVAILABLE


def test_missing_medical_artifact_has_no_fabricated_version(tmp_path: Path) -> None:
    assert medical_version(tmp_path / "missing", load_panel_motion_limits(configured())) is None


@pytest.mark.parametrize(
    "session_request",
    [
        SessionRequest(Occupancy.OCCUPIED, subject_id="name@example.invalid"),
        SessionRequest(Occupancy.OCCUPIED, cloud_session_id="../outside"),
    ],
)
def test_invalid_subject_or_remote_identity_is_not_encoded(session_request: SessionRequest) -> None:
    result = session_context(source_for(configured()), session_request, ManualClock()).manifest
    assert isinstance(result, Err)
    assert result.error.code == ContextUnavailable.IDENTIFIER_INVALID


def test_profile_privacy_exact_resolution_and_bounded_encoding(
    tmp_path: Path, record_property: Callable[[str, int], None]
) -> None:
    config = configured(RECORD_MACHINE_ID="m" * 128, RECORD_ORGANIZATION_ID="o" * 128)
    panel = build_panel(config, clock=ManualClock(), profiles_path=tmp_path / "profiles.json")
    native = panel.services.store.list_profiles()[0]
    distinct = replace(
        native,
        baseline_s=Seconds(native.baseline_s + 1),
        warmup_max_s=Seconds(native.warmup_max_s + 2),
        hold_min_s=Seconds(native.hold_min_s + 3),
        cooldown_s=Seconds(native.cooldown_s + 4),
        recovery_s=Seconds(native.recovery_s + 5),
    )
    program = Program(
        profile=replace(distinct, name="Private Patient name@example.invalid"),
        source_rev=StoreRev(17),
        resolved_at=UnixMillis(12345),
        total_overridden=True,
    )
    context = session_context(
        source_for(config),
        SessionRequest(Occupancy.OCCUPIED, program, "subject-7", "remote-8"),
        ManualClock(),
    )
    assert isinstance(context.manifest, Ok)
    profile = context.manifest.value.profile
    assert profile is not None
    assert profile.total_duration_s == native.total_duration_s
    assert profile.zone_low_bpm == native.zone_low_bpm
    assert profile.zone_high_bpm == native.zone_high_bpm
    assert (
        profile.baseline_s,
        profile.warmup_max_s,
        profile.hold_min_s,
        profile.cooldown_s,
        profile.recovery_s,
    ) == (
        distinct.baseline_s,
        distinct.warmup_max_s,
        distinct.hold_min_s,
        distinct.cooldown_s,
        distinct.recovery_s,
    )
    assert (
        profile.hard_max_bpm,
        profile.critical_bpm,
        profile.subject_hr_max,
        profile.min_run_rpm,
        profile.max_rpm,
        profile.warmup_rpm_ceiling_fraction,
    ) == (
        native.hard_max_bpm,
        native.critical_bpm,
        native.subject_hr_max,
        native.min_run_rpm,
        native.max_rpm,
        native.warmup_rpm_ceiling_fraction,
    )
    assert profile.channels == tuple(channel.value for channel in native.channels)
    assert profile.allow_above_nameplate == native.allow_above_nameplate
    assert profile.source_rev == 17
    assert profile.resolved_at == 12345
    assert profile.total_overridden
    assert context.manifest.value.subject_id == "subject-7"
    assert context.manifest.value.session_id == "remote-8"
    content = encoded(context)
    assert "Private Patient" not in content
    assert "name@example.invalid" not in content
    (tmp_path / "encoded-manifest.json").write_text(content, encoding="utf-8")
    assert len(content.encode()) < 8192
    record_property("encoded_manifest_bytes", len(content.encode()))
    assert MANIFEST.validate_json(content) == context.manifest.value
    again = session_context(source_for(config), SessionRequest(Occupancy.BENCH), ManualClock())
    assert isinstance(again.manifest, Ok)
    assert again.local_ref != context.local_ref
    assert again.manifest.value.operator != context.manifest.value.operator
    oversized = replace(program, source_rev=StoreRev(1 << 70))
    rejected = session_context(
        source_for(config), SessionRequest(Occupancy.OCCUPIED, oversized), ManualClock()
    )
    assert isinstance(rejected.manifest, Err)
    assert rejected.manifest.error.code == ContextUnavailable.METADATA_LIMIT


def test_medical_result_guard_detects_runtime_contract_corruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(_value: JsonValue) -> None:
        return None

    monkeypatch.setattr(context_module, "parse_motion_limits", malformed)
    with pytest.raises(AssertionError):
        medical_version(medical_path(configured()), load_panel_motion_limits(configured()))
