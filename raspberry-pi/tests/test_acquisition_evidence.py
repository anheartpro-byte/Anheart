from src.clock import ManualClock
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok
from src.training.drive_inspection import (
    InspectionReady,
    OpenFailed,
    StatusFailed,
    begin_inspection,
)
from src.units import MotorRpm, Seconds
from tests.test_failure_rig import Wrapped
from tests.test_initial_inspection_cancellation import HeldInspection


async def test_simulated_proof_and_frame_count_survive_reopen_and_close() -> None:
    drive = SimulatedDrive(ManualClock())
    initial = drive.acquisition_evidence
    assert initial.possible_frames == 0
    assert not initial.address_proven
    assert isinstance(await drive.open(), Ok)
    opened = drive.acquisition_evidence
    assert opened.possible_frames == 1
    assert opened.address_proven
    assert isinstance(await drive.read_status(), Ok)
    assert isinstance(await drive.write_speed(MotorRpm(0)), Ok)
    before_close = drive.acquisition_evidence
    assert before_close.possible_frames == 3
    assert isinstance(await drive.close(), Ok)
    assert drive.acquisition_evidence.possible_frames >= before_close.possible_frames
    assert isinstance(await drive.open(), Ok)
    assert drive.acquisition_evidence.possible_frames > before_close.possible_frames
    assert drive.acquisition_evidence.address_proven
    assert initial.possible_frames == 0
    assert not initial.address_proven


async def test_total_outage_has_no_frames_and_does_not_erase_previous_proof() -> None:
    drive = SimulatedDrive(ManualClock())
    assert isinstance(await drive.open(), Ok)
    before = drive.acquisition_evidence
    drive.inject_comms_loss(Seconds(20))
    for _ in range(5):
        assert isinstance(await drive.open(), Err)
        assert isinstance(await drive.read_status(), Err)
        assert drive.acquisition_evidence == before


async def test_first_total_outage_does_not_manufacture_proof() -> None:
    drive = SimulatedDrive(ManualClock())
    drive.inject_comms_loss(Seconds(20))
    assert isinstance(await drive.open(), Err)
    assert drive.acquisition_evidence.possible_frames == 0
    assert not drive.acquisition_evidence.address_proven


async def test_wrapped_backend_forwards_actual_delegate_evidence() -> None:
    drive = SimulatedDrive(ManualClock())
    wrapped = Wrapped(drive)
    assert wrapped.acquisition_evidence == drive.acquisition_evidence
    assert isinstance(await wrapped.open(), Ok)
    assert wrapped.acquisition_evidence.address_proven
    assert wrapped.acquisition_evidence == drive.acquisition_evidence


async def test_overridden_failed_open_retains_delegate_proof_without_bypassing_override() -> None:
    drive = SimulatedDrive(ManualClock())
    held = HeldInspection(drive)
    held.fail_open = True
    assert isinstance(await held.open(), Err)
    assert held.acquisition_evidence.possible_frames == 1
    assert held.acquisition_evidence.address_proven
    assert held.reads == 0


async def test_failed_open_inspection_keeps_independent_proof() -> None:
    held = HeldInspection(SimulatedDrive(ManualClock()))
    held.fail_open = True
    result = await begin_inspection(held, reopen=True)
    assert isinstance(result, OpenFailed)
    assert result.before.possible_frames == 0
    assert result.after.possible_frames == 1
    assert result.after.address_proven


async def test_failed_status_inspection_counts_the_whole_attempt() -> None:
    held = HeldInspection(SimulatedDrive(ManualClock()))
    held.fail_read = True
    held.release.set()
    ready = await begin_inspection(held, reopen=True)
    assert isinstance(ready, InspectionReady)
    result = await ready.read_status()
    assert isinstance(result, StatusFailed)
    assert result.before.possible_frames == 0
    assert result.after.possible_frames == 2
    assert result.after.address_proven
