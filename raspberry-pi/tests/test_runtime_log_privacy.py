"""Operational events must not copy rider data into the logging channel."""

import pytest

from src.clock import ManualClock
from src.result import Ok
from src.training.plan import INITIAL_REV, Program
from src.training.runtime import Subject, TrainingRuntime
from src.training.safety import SafetyLimits
from src.training.types import Phase
from src.units import Bpm, Monotonic, UnixMillis
from tests.test_runtime import GEOMETRY, LIMITS, REAL_PROFILE, RIG_MOTION, FakeDrive, Rig


@pytest.fixture
def privacy_rig() -> Rig:
    clock = ManualClock(Monotonic(1000), UnixMillis(1_700_000_000_000))
    drive = FakeDrive(clock)
    profile = REAL_PROFILE
    runtime = TrainingRuntime(
        clock=clock,
        drive=drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=SafetyLimits(hard_max_bpm=profile.hard_max_bpm, critical_bpm=profile.critical_bpm),
        motion=RIG_MOTION,
    )
    return Rig(
        clock=clock,
        drive=drive,
        runtime=runtime,
        program=Program(
            profile=profile,
            source_rev=INITIAL_REV,
            resolved_at=clock.unix_millis(),
            total_overridden=False,
        ),
    )


async def test_session_start_keeps_rider_and_profile_out_of_logs(
    privacy_rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a validated programme with distinguishable synthetic identities.
    subject = Subject(subject_id="PRIVATE-RIDER-FIXTURE", operator="PRIVATE-OPERATOR-FIXTURE")
    assert isinstance(privacy_rig.runtime.confirm_estop_wiring(subject.operator), Ok)
    with caplog.at_level("INFO", logger="src.training.runtime"):
        # When that session is started.
        result = await privacy_rig.runtime.start(privacy_rig.program, subject)
    # Then state events remain, but identity and the selected profile do not.
    assert isinstance(result, Ok)
    assert caplog.records
    for private in (subject.subject_id, subject.operator, privacy_rig.program.source_profile_id):
        assert private not in caplog.text
    await privacy_rig.runtime.shutdown("test complete")


async def test_warmup_transition_keeps_measured_bpm_out_of_logs(
    privacy_rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a session entering warmup below the zone.
    privacy_rig.fed_bpm = Bpm(82)
    assert isinstance(await privacy_rig.start(), Ok)
    await privacy_rig.run(181)
    assert privacy_rig.phase() is Phase.WARMUP
    measured = Bpm(127)
    privacy_rig.fed_bpm = measured
    with caplog.at_level("INFO", logger="src.training.runtime"):
        # When the measured heart rate reaches the zone.
        await privacy_rig.run(4)
    # Then the transition is logged without the measured value.
    assert privacy_rig.phase() is Phase.HOLD
    assert caplog.records
    assert str(measured) not in caplog.text
    await privacy_rig.runtime.shutdown("test complete")


def test_emergency_reason_stays_in_authorized_state_not_logs(
    privacy_rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a reason that could contain private clinical information.
    private_reason = "PRIVATE-CLINICAL-REASON-FIXTURE"
    with caplog.at_level("ERROR"):
        # When an emergency stop is latched.
        verdict = privacy_rig.runtime.request_estop(private_reason)
    # Then authorized state retains the reason, but operational logs do not.
    assert private_reason in verdict.detail
    assert caplog.records
    assert private_reason not in caplog.text
