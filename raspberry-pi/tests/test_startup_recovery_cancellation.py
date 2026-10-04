import asyncio
from pathlib import Path
from typing import override

import pytest

from src.motor.drive import ControlWord, DriveError
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Ok, Result
from src.units import MotorRpm
from tests.test_arming_cancellation import HeldCommand
from tests.test_failure_rig import attest, make_rig
from tests.test_local_panel import FakeWeb


class HeldRecovery(HeldCommand):
    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.after_transmission = False

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        if rpm == 0 and not self.accepted.is_set():
            if self.after_transmission:
                result = await super().write_speed(rpm)
                assert isinstance(result, Ok)
            self.accepted.set()
            await self.release.wait()
        return await super().write_speed(rpm)


@pytest.mark.parametrize("after_transmission", [False, True])
@pytest.mark.parametrize("parent_run", [False, True])
@pytest.mark.parametrize("requested_start", [False, True])
async def test_cancel_startup_recovery_owns_the_stop(
    tmp_path: Path, after_transmission: bool, parent_run: bool, requested_start: bool
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldRecovery)
    assert isinstance(held, HeldRecovery)
    held.after_transmission = after_transmission
    assert isinstance(await rig.simulator.open(), Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await rig.simulator.write_command(word), Ok)
    assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(
            rig.panel.run(stop, web) if parent_run else rig.panel.control_step()
        )
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            await rig.panel.close()
            assert await rig.left_stopped() == ""
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()
