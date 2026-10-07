import asyncio
from pathlib import Path
from typing import override

import pytest

from src.motor.drive import ControlWord, DriveError, StopUnconfirmed
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Err, Ok, Result
from src.units import MotorRpm, Seconds
from tests.test_failure_rig import SteppedClock, attest, make_rig
from tests.test_initial_inspection_cancellation import HeldInspection


class CloseUnconfirmed(HeldInspection):
    @override
    async def close(self) -> Result[None, DriveError]:
        return Err(StopUnconfirmed(Seconds(1.0), MotorRpm(45), "standstill unreadable"))


class SettlingInspection(HeldInspection):
    """An injected plant clock lets close wait for real simulated standstill."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.clock: SteppedClock | None = None

    @override
    async def close(self) -> Result[None, DriveError]:
        clock = self.clock
        assert clock is not None
        for _ in range(100):
            status = await self.inner.read_status()
            assert isinstance(status, Ok)
            if status.value.output_rpm == 0:
                return await super().close()
            self.inner.advance(clock.advance(Seconds(0.2)))
        raise AssertionError("the shaft did not reach standstill before close")


@pytest.mark.parametrize("requested_start", [False, True])
async def test_failed_acquisition_never_authorizes_a_write(
    tmp_path: Path, requested_start: bool
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.hold_open = True
    held.fail_before_open = True
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
            assert not held.acquisition_evidence.address_proven
            assert held.acquisition_evidence.possible_frames == 0
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


async def test_normal_close_stops_an_acquired_drive_whose_initial_status_was_unreadable(
    tmp_path: Path,
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.fail_read = True
    held.release.set()
    opened = await rig.simulator.open()
    assert isinstance(opened, Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await rig.simulator.write_command(word), Ok)
    assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    try:
        await rig.panel.control_step()
        await rig.panel.close()
        assert rig.simulator.commanded_setpoint == 0
        assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
    finally:
        await rig.panel.close()


@pytest.mark.parametrize("requested_start", [False, True])
async def test_failed_retry_cannot_erase_an_acquired_drives_unknown_state(
    tmp_path: Path, requested_start: bool
) -> None:
    # Given an acquired drive whose old command could not be inspected.
    rig, held = make_rig(tmp_path, wrap=SettlingInspection)
    assert isinstance(held, SettlingInspection)
    held.clock = rig.clock
    held.fail_read = True
    held.release.set()
    opened = await rig.simulator.open()
    assert isinstance(opened, Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await rig.simulator.write_command(word), Ok)
    assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    try:
        await rig.panel.control_step()
        assert held.reads == 1
        held.fail_open = True
        rig.clock.advance(Seconds(1.0))
        async with rig.http() as client:
            if requested_start:
                await attest(client)
                response = await client.post(
                    "/api/manual/start",
                    json={"operator": "synthetic operator", "occupancy": "bench"},
                )
                assert response.status_code == 202
            # When a later acquisition fails and the process exits.
            await rig.panel.control_step()
            await rig.panel.close()
        # Then failed acquisition is not new evidence that the output is off.
        assert rig.simulator.commanded_setpoint == 0
        assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
    finally:
        await rig.panel.close()


async def test_an_unconfirmed_stop_never_claims_the_unknown_output_is_disabled(
    tmp_path: Path,
) -> None:
    rig, held = make_rig(tmp_path, wrap=CloseUnconfirmed)
    assert isinstance(held, CloseUnconfirmed)
    held.fail_read = True
    held.release.set()
    opened = await rig.simulator.open()
    assert isinstance(opened, Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await rig.simulator.write_command(word), Ok)
    assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    try:
        await rig.panel.control_step()
        await rig.panel.close()
        report = await rig.panel.runtime.shutdown("already closing")
        assert report.output_disabled is False
        assert rig.panel.runtime.output_enabled
        assert rig.simulator.commanded_setpoint == 0
        assert rig.simulator.sim_state is SimState.OPERATION_ENABLED
        exchanges = (held.reads, held.writes)
        await rig.panel.control_step()
        assert (held.reads, held.writes) == exchanges
    finally:
        await rig.panel.close()
