from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src import local_config
from src.local_config import load_local_config
from src.record.config import (
    RecordingKey,
    RecordingProblemCode,
    RetentionDays,
    load_recording_config,
)
from src.result import Err, Ok
from tests.test_local_config import SIM_ENV


def test_missing_recording_identity_keeps_existing_configuration_available() -> None:
    result = load_local_config(SIM_ENV)
    assert isinstance(result, Ok)
    recording = result.value.recording
    assert recording.machine_id is None
    assert recording.organization_id is None
    assert recording.root == Path("data/records")
    assert recording.retention_days == RetentionDays(30)
    assert recording.software_version == "unversioned"
    assert not result.value.occupied_enabled
    assert not result.value.programs_enabled


def test_provisioned_configuration_is_exact_and_immutable() -> None:
    result = load_recording_config(
        {
            "RECORD_MACHINE_ID": "machine-a",
            "RECORD_ORGANIZATION_ID": "org-b",
            "RECORD_ROOT": "private/records",
            "RECORD_LOCAL_RETENTION_DAYS": "0",
            "ANHEART_SOFTWARE_VERSION": "build-1.2+3",
        }
    )
    assert isinstance(result, Ok)
    assert result.value.machine_id == "machine-a"
    assert result.value.organization_id == "org-b"
    assert result.value.root == Path("private/records")
    assert result.value.retention_days == RetentionDays(0)
    assert result.value.software_version == "build-1.2+3"
    with pytest.raises(FrozenInstanceError):
        result.value.__setattr__("machine_id", "changed")


@pytest.mark.parametrize("key", [RecordingKey.MACHINE, RecordingKey.ORGANIZATION])
@pytest.mark.parametrize("value", ["../outside", "with space", "name@example.invalid", "x" * 129])
def test_invalid_identity_is_rejected_without_echoing_private_input(
    key: RecordingKey, value: str
) -> None:
    result = load_recording_config({key: value})
    assert isinstance(result, Err)
    assert result.error[0].key == key
    assert result.error[0].code == RecordingProblemCode.INVALID_IDENTIFIER
    assert value not in repr(result.error)


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "1.5"])
def test_invalid_retention_is_rejected(value: str) -> None:
    result = load_recording_config({"RECORD_LOCAL_RETENTION_DAYS": value})
    assert isinstance(result, Err)
    assert result.error[0].code == RecordingProblemCode.INVALID_RETENTION


@pytest.mark.parametrize("value", ["\x00", "x" * 4097], ids=["nul", "oversize"])
def test_invalid_root_is_rejected(value: str) -> None:
    result = load_recording_config({"RECORD_ROOT": value})
    assert isinstance(result, Err)
    assert result.error[0].code == RecordingProblemCode.INVALID_ROOT


@pytest.mark.parametrize("value", ["name@example.invalid", "x" * 129])
def test_invalid_version_is_rejected(value: str) -> None:
    result = load_recording_config({"ANHEART_SOFTWARE_VERSION": value})
    assert isinstance(result, Err)
    assert result.error[0].code == RecordingProblemCode.INVALID_VERSION


def test_configuration_problems_join_the_existing_parser_without_sensitive_values() -> None:
    result = load_local_config(
        {
            **SIM_ENV,
            "RECORD_MACHINE_ID": "person@example.invalid",
            "RECORD_LOCAL_RETENTION_DAYS": "bad",
        }
    )
    assert isinstance(result, Err)
    assert {problem.key for problem in result.error} == {
        "RECORD_MACHINE_ID",
        "RECORD_LOCAL_RETENTION_DAYS",
    }
    assert "person@example.invalid" not in repr(result.error)


@pytest.mark.parametrize("key", [RecordingKey.MACHINE, RecordingKey.ORGANIZATION])
def test_partial_or_blank_identity_stays_unknown(key: RecordingKey) -> None:
    result = load_recording_config({key: "   "})
    assert isinstance(result, Ok)
    assert result.value.machine_id is None
    assert result.value.organization_id is None


def test_recording_configuration_result_guard_detects_corrupt_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(_env: Mapping[str, str]) -> None:
        return None

    monkeypatch.setattr(local_config, "load_recording_config", malformed)
    with pytest.raises(AssertionError):
        load_local_config(SIM_ENV)
