"""An in-flight initial observation belongs to teardown, not to a cancelled caller."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import override

import pytest

from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveError,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Err, Ok, Result
from src.units import MotorRpm, Seconds
from tests.test_arming_cancellation import HeldCommand
from tests.test_failure_rig import attest, make_rig
from tests.test_local_panel import FakeWeb


class HeldInspection(HeldCommand):
    """Mutable events hold an initial reply; counts observe attempted motion writes."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.hold_open = False
        self.fail_read = False
        self.writes = 0

    @override
    async def open(self) -> Result[None, DriveError]:
        result = await super().open()
        if self.hold_open and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return result

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        result = await self.inner.read_status()
        if self.fail_read:
            result = Err(CommTimeout(Seconds(0.05)))
        if not self.hold_open and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return result

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self.writes += 1
        return await super().write_speed(rpm)

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self.writes += 1
        return await super().write_command(word)

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self.writes += 1
        return super().emergency_disable_blocking(timeout)


@dataclass(frozen=True, slots=True)
class InspectionCase:
    hold_open: bool
    requested_start: bool
    parent_run: bool
    precommanded: bool


@pytest.mark.parametrize(
    "case",
    [
        InspectionCase(
            hold_open=hold_open,
            requested_start=requested_start,
            parent_run=parent_run,
            precommanded=precommanded,
        )
        for hold_open in (False, True)
        for requested_start in (False, True)
        for parent_run in (False, True)
        for precommanded in (False, True)
    ],
)
async def test_cancel_initial_inspection_stops_only_observed_motion(
    tmp_path: Path, case: InspectionCase
) -> None:
    # Given a held initial reply and either a normal idle or precommanded drive.
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.hold_open = case.hold_open
    if case.precommanded:
        assert isinstance(await rig.simulator.open(), Ok)
        for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
            assert isinstance(await rig.simulator.write_command(word), Ok)
        assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if case.requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(
            rig.panel.run(stop, web) if case.parent_run else rig.panel.control_step()
        )
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            # When cancellation races the initial reply, before its caller sees it.
            task.cancel()
            if case.parent_run:
                await asyncio.wait_for(web.exit.wait(), 2)
                asyncio.get_running_loop().call_soon(held.release.set)
            else:
                held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await rig.panel.close()
            # Then observed motion is stopped; confirmed idle still gets no writes.
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            if not case.precommanded:
                assert held.writes == 0
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


@pytest.mark.parametrize("requested_start", [False, True])
async def test_cancelled_unavailable_inspection_does_not_invent_motion(
    tmp_path: Path, requested_start: bool
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.fail_read = True
    async with rig.http() as client:
        if requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(rig.panel.control_step())
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            task.cancel()
            held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await rig.panel.close()
            assert held.writes == 0
            assert rig.simulator.commanded_setpoint == 0
        finally:
            held.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()
