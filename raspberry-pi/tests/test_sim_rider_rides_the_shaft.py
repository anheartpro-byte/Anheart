"""The console's simulated rider rides the simulated drive's own shaft, never RFRD (ANH-92).

RFRD reads 0 with the stage off, as on the ATV320, and a coasting arm still loads the
rider. The shaft is read from the drive side (``DriveSide.simulator``), present only when
the drive is the simulator; the runtime decides on RFRD alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.motor.drive import DriveFault
from src.sim.bitalino import SimulatedBitalinoClient
from src.units import MotorRpm
from tests.test_failure_rig import make_rig, start_manual


async def test_through_a_coast_the_simulated_rider_is_fed_the_shaft_rfrd_does_not_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fed: list[MotorRpm] = []
    original = SimulatedBitalinoClient.set_motor_rpm

    def noted(client: SimulatedBitalinoClient, rpm: MotorRpm) -> None:
        fed.append(rpm)
        original(client, rpm)

    monkeypatch.setattr(SimulatedBitalinoClient, "set_motor_rpm", noted)
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await start_manual(rig, session, 10.0)
    await rig.tick(45.0)
    rig.simulator.inject_fault(DriveFault.OVERCURRENT)  # a freewheel: the stage drops
    fed.clear()
    last = await rig.tick(5.0)
    assert last.measured.motor_rpm == 0  # RFRD: nothing of the coast
    shaft = rig.simulator.shaft_rpm
    assert shaft > 100
    assert fed[-1] == shaft
    assert all(rpm > 100 for rpm in fed)
    await rig.panel.close()
