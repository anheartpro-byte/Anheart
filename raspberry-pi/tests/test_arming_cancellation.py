"""Cancellation after a drive word lands must still remove its output stage."""

import asyncio
from pathlib import Path
from typing import override

import pytest

from src.motor.drive import ControlWord, DriveError
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Ok, Result
from src.units import MotorRpm
from tests.test_failure_rig import Wrapped, attest, make_rig


class HeldCommand(Wrapped):
    """Mutable events represent an accepted word whose reply is still in flight."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.word = ControlWord.ENABLE_OPERATION
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def close(self) -> Result[None, DriveError]:
        status = await self.inner.read_status()
        assert isinstance(status, Ok)
        assert status.value.output_rpm == 0
        if self.inner.sim_state is SimState.OPERATION_ENABLED:
            assert isinstance(await self.inner.write_speed(MotorRpm(0)), Ok)
            assert isinstance(await self.inner.write_command(ControlWord.SWITCH_ON), Ok)
            assert isinstance(await self.inner.write_command(ControlWord.SHUTDOWN), Ok)
        return await super().close()

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        result = await super().write_command(word)
        if word is self.word and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return result


@pytest.mark.parametrize(
    "word", [ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION]
)
async def test_cancel_after_arming_word_stops_and_disables(
    tmp_path: Path, word: ControlWord
) -> None:
    # Given a real simulated drive accepting an arming word before replying.
    rig, held = make_rig(tmp_path, wrap=HeldCommand)
    assert isinstance(held, HeldCommand)
    held.word = word
    async with rig.http() as client:
        await attest(client)
        response = await client.post(
            "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
        )
        assert response.status_code == 202
        task = asyncio.create_task(rig.panel.control_step())
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            # When the task is cancelled during that pending reply.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                _ = await task
            await rig.panel.close()
            # Then no reference or energized output survives the exit.
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            assert await rig.left_stopped() == ""
        finally:
            held.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()
