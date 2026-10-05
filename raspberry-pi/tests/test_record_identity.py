import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from src.record import context as context_module
from src.record.context import ContextUnavailable
from src.result import Err, Ok, Result
from src.training.runtime import RuntimeState, StartRefusal, TrainingRuntime
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import MotorRpm, Seconds
from tests.record_identity_support import encoded
from tests.test_cloud_sync import DOWN, PROFILE, linked, manual, ok, rig


async def test_provisioned_reference_survives_cloud_registration_retry(tmp_path: Path) -> None:
    setup = rig(tmp_path)
    started = manual(setup.clock)
    setup.runtime.state = RuntimeState.RUNNING
    setup.dashboard.answer("/api/machine/training/local", DOWN)
    setup.sync.session_started(started)
    await setup.step()
    setup.dashboard.answer("/api/machine/training/local", ok({"sessionId": "cloud-id"}))
    await setup.step(20)
    bodies = setup.dashboard.to("/api/machine/training/local")
    assert len(bodies) == 2
    assert [body["localRef"] for body in bodies] == [started.local_ref, started.local_ref]


async def test_provisioned_context_precedes_arm_and_preserves_operator_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = linked(tmp_path, RECORD_MACHINE_ID="machine-a", RECORD_ORGANIZATION_ID="org-b")
    initial_context = setup.panel.recording_context
    assert initial_context is None
    order: list[str] = []

    def generated() -> UUID:
        order.append("identity")
        return uuid4()

    original = TrainingRuntime.start_manual

    async def arm(
        runtime: TrainingRuntime, occupancy: Occupancy, operator: str, ceiling: MotorRpm
    ) -> Result[TelemetrySnapshot, StartRefusal]:
        order.append("arm")
        setup.clock.advance(Seconds(1))
        return await original(runtime, occupancy, operator, ceiling)

    monkeypatch.setattr(context_module, "uuid4", generated)
    monkeypatch.setattr(TrainingRuntime, "start_manual", arm)
    operator = "Private Operator person@example.invalid"
    assert isinstance(
        setup.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=operator), Ok
    )
    before = setup.clock.monotonic()
    try:
        await setup.panel.control_step()
        context = setup.panel.recording_context
        assert context is not None
        assert context.clocks.monotonic_start == before
        assert order == ["identity", "identity", "arm"]
        assert setup.panel.runtime.state == RuntimeState.RUNNING
        await setup.panel.cloud_step()
        body = setup.dashboard.to("/api/machine/training/local")[0]
        assert body["localRef"] == context.local_ref
        assert body["operatorName"] == operator
        assert operator not in encoded(context)
        assert "Private Operator" not in encoded(context)
        assert "person@example.invalid" not in encoded(context)
    finally:
        await setup.panel.close()


async def test_missing_identity_does_not_disable_existing_manual_start(tmp_path: Path) -> None:
    setup = linked(tmp_path)
    assert isinstance(
        setup.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator="Operator"), Ok
    )
    try:
        await setup.panel.control_step()
        context = setup.panel.recording_context
        assert context is not None
        assert isinstance(context.manifest, Err)
        assert context.manifest.error.code == ContextUnavailable.IDENTITY_MISSING
        assert setup.panel.runtime.state == RuntimeState.RUNNING
    finally:
        await setup.panel.close()


@pytest.mark.parametrize("subject_id", ["subject-7", "local"])
async def test_provisioned_auto_context_uses_actual_subject_and_resolved_profile(
    tmp_path: Path, subject_id: str
) -> None:
    setup = linked(tmp_path, RECORD_MACHINE_ID="machine-a", RECORD_ORGANIZATION_ID="org-b")
    assert isinstance(
        setup.panel.surface.submit_start(
            profile_id=PROFILE,
            operator="Operator",
            total_duration_s=Seconds(2400),
            subject_age=30,
            subject_id=subject_id,
        ),
        Ok,
    )
    try:
        await setup.run(2)
        assert setup.panel.runtime.state == RuntimeState.RUNNING
        context = setup.panel.recording_context
        assert context is not None
        assert isinstance(context.manifest, Ok)
        manifest = context.manifest.value
        assert manifest.subject_id == (None if subject_id == "local" else subject_id)
        assert manifest.kind == "auto"
        assert manifest.occupancy == "occupied"
        assert manifest.profile is not None
        assert manifest.profile.total_duration_s == 2400
        assert manifest.profile.total_overridden
        assert manifest.profile.source_rev == setup.panel.services.store.rev
        body = setup.dashboard.to("/api/machine/training/local")[0]
        assert body["localRef"] == context.local_ref
    finally:
        await setup.panel.close()


async def test_start_callback_performs_no_filesystem_or_hash_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = linked(
        tmp_path,
        RECORD_MACHINE_ID="machine-a",
        RECORD_ORGANIZATION_ID="org-b",
        RECORD_ROOT=str(tmp_path / "unused-records"),
    )

    def forbidden(_path: Path, mode: str = "r", *, encoding: str | None = None) -> None:
        raise AssertionError((mode, encoding))

    def forbidden_hash(_data: bytes = b"") -> None:
        raise AssertionError("unexpected start-time context hashing")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(hashlib, "sha256", forbidden_hash)
    try:
        assert isinstance(
            setup.panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator="Operator"),
            Ok,
        )
        await setup.panel.control_step()
        assert setup.panel.runtime.state == RuntimeState.RUNNING
        assert setup.panel.recording_context is not None
        assert not (tmp_path / "unused-records").exists()
    finally:
        await setup.panel.close()
