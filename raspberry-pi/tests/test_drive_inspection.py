import asyncio
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.motor.simulated import SimulatedDrive
from src.training.drive_inspection import (
    InspectionReady,
    OpenFailed,
    StatusFailed,
    StatusRead,
    begin_inspection,
)
from src.training.idle_recovery import IdleLink as DefinedIdleLink
from src.training.runtime import IdleLink
from src.units import Seconds
from tests.test_failure_rig import make_rig
from tests.test_initial_inspection_cancellation import HeldInspection


def test_idle_link_public_identity_is_preserved() -> None:
    assert IdleLink is DefinedIdleLink


async def test_failed_open_never_reads_status() -> None:
    drive = HeldInspection(SimulatedDrive(ManualClock()))
    drive.fail_open = True
    result = await begin_inspection(drive, reopen=True)
    assert isinstance(result, OpenFailed)
    assert drive.reads == 0
    assert drive.writes == 0


@pytest.mark.parametrize("measured", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_status_reply_and_injected_latency_are_preserved(
    measured: bool, failed: bool
) -> None:
    clock = ManualClock()
    drive = HeldInspection(SimulatedDrive(clock))
    drive.fail_read = failed
    ready = await begin_inspection(drive, reopen=True)
    assert isinstance(ready, InspectionReady)
    task = asyncio.create_task(ready.read_status(clock if measured else None))
    try:
        await asyncio.wait_for(drive.accepted.wait(), 2)
        clock.advance(Seconds(0.25))
        drive.release.set()
        result = await asyncio.wait_for(task, 2)
        if failed:
            assert isinstance(result, StatusFailed)
        else:
            assert isinstance(result, StatusRead)
            assert result.latency == (Seconds(0.25) if measured else None)
        assert drive.reads == 1
        assert drive.writes == 0
    finally:
        drive.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_existing_open_link_skips_reopen() -> None:
    drive = HeldInspection(SimulatedDrive(ManualClock()))
    drive.fail_open = True
    ready = await begin_inspection(drive, reopen=False)
    assert isinstance(ready, InspectionReady)
    assert drive.reads == 0


async def test_idle_open_is_visible_while_status_reply_is_pending(tmp_path: Path) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    task = asyncio.create_task(rig.panel.control_step())
    try:
        await asyncio.wait_for(held.accepted.wait(), 2)
        assert rig.panel.runtime.idle_link.open
        assert rig.panel.reporter.panel_status().drive.open
        assert rig.panel.runtime.idle_link.reads == 0
        held.release.set()
        await asyncio.wait_for(task, 2)
        assert rig.panel.runtime.idle_link.reads == 1
        assert held.writes == 0
    finally:
        held.release.set()
        await asyncio.gather(task, return_exceptions=True)
        await rig.panel.close()
