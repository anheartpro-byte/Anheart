"""The simulated rider rides the plant's own shaft, not RFRD (ANH-92).

RFRD reads 0 with the stage off, as on the ATV320. A rider driven by it would feel no
g-load through a whole coast, and his heart and the ECG's motion artefact would be
quieter than on the machine: the simulation would be optimistic on the sensor side.
"""

from __future__ import annotations

import asyncio
from typing import Final

import pytest

from simulation.failures import cases, run_case
from simulation.harness import TICK
from src.motor.simulated import DEFAULT_SIM_CONFIG
from src.sim.physiology import Physiology, SubjectState
from src.units import Monotonic, MotorRpm

STEP_RPM: Final[float] = (
    float(DEFAULT_SIM_CONFIG.nominal_rpm) / float(DEFAULT_SIM_CONFIG.acceleration_time) * TICK + 1.0
)
"""What the shaft can move in one tick on the drive's ramp (138 rpm/s), more than a coast does."""


def test_through_a_coast_the_rider_is_fed_the_shaft_rfrd_does_not_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fed: list[tuple[int, int]] = []
    original = Physiology.advance

    def noted(physiology: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        fed.append((id(physiology), int(motor_rpm)))
        return original(physiology, now, motor_rpm)

    monkeypatch.setattr(Physiology, "advance", noted)
    case = next(case for case in cases() if case.name == "sto_open_auto_hold")
    result = asyncio.run(run_case(case))
    assert len({who for who, _ in fed}) == 1, "one rider, the harness's: no BITalino here"
    rows, shaft = result.trace.rows, result.trace.shaft
    coasting = [
        tick
        for tick, (row, rpm) in enumerate(zip(rows, shaft, strict=True))
        if tick > 0 and row.measured_motor_rpm == 0 and rpm > 100
    ]
    assert len(coasting) > 100, "RFRD shows nothing of a coast the shaft makes"
    # One feed per tick, before the tick moves the plant: tick k feeds what tick k - 1 left.
    during = [rpm for _, rpm in fed[len(fed) - len(rows) :]]
    assert all(abs(during[tick] - shaft[tick - 1]) <= STEP_RPM for tick in coasting)
