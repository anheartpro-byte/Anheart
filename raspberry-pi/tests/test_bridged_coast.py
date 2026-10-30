"""The coast a mains cut leaves under ``STO_SOURCE=bridged_declared``, on the simulated drive.

On a bridged STO the only emergency stop is unplugging the mains: the drive goes dark,
torque is gone at once and the arm coasts with no brake. The figure the docs quote
(docs/raspberry-pi.md, docs/securite.md) is measured here, from the nameplate 1380 motor
rpm to the simulator's ``standstill_rpm``.

Its source is the simulator's ``tau_coast = 20 s``, a placeholder that no coast-down on the
machine has ever measured: ``rpm(t) = 1380 exp(-t / 20)`` falls below 1 rpm after
``20 ln(1380)``, 144.6 s. A real coast can be longer or shorter; this is a model, not a
measurement of the machine.
"""

from __future__ import annotations

import math
from typing import Final

from src.clock import ManualClock
from src.motor.drive import ControlWord, DriveFault, DriveStatus
from src.motor.simulated import DEFAULT_SIM_CONFIG, SimState, SimulatedDrive
from src.result import Ok
from src.units import MotorRpm, Seconds, elapsed

STEP: Final[Seconds] = Seconds(0.1)
"""The resolution of the measurement: the plant is stepped this often, as a tick would."""

UP_TO: Final[Seconds] = Seconds(300.0)
"""Longer than any coast this model can produce from 1380 rpm, so the loop always ends."""


def state_of(drive: SimulatedDrive) -> SimState:
    """Read afresh: a checker would otherwise keep a narrowing across a call."""
    return drive.sim_state


async def status_of(drive: SimulatedDrive) -> DriveStatus:
    read = await drive.read_status()
    assert isinstance(read, Ok), read
    return read.value


async def at_nameplate_speed(clock: ManualClock) -> SimulatedDrive:
    """The drive enabled and turning at 1380 motor rpm, reached on its own ramp."""
    drive = SimulatedDrive(clock)
    opened = await drive.open()
    assert isinstance(opened, Ok), opened
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await drive.write_command(word), Ok)
    assert isinstance(await drive.write_speed(DEFAULT_SIM_CONFIG.max_rpm), Ok)
    for _ in range(round(DEFAULT_SIM_CONFIG.acceleration_time / STEP) + 1):
        drive.advance(clock.advance(STEP))
        assert isinstance(await drive.write_command(ControlWord.ENABLE_OPERATION), Ok)
    assert (await status_of(drive)).output_rpm == MotorRpm(1380)
    return drive


def test_the_figure_rests_on_the_simulator_s_provisional_constants() -> None:
    """If a coast-down is ever measured and the constant changes, the docs must change too."""
    assert DEFAULT_SIM_CONFIG.tau_coast == Seconds(20.0)
    assert DEFAULT_SIM_CONFIG.standstill_rpm == MotorRpm(1)
    assert DEFAULT_SIM_CONFIG.max_rpm == DEFAULT_SIM_CONFIG.nominal_rpm == MotorRpm(1380)
    assert round(20.0 * math.log(1380.0), 1) == 144.6


async def test_a_mains_cut_at_nameplate_speed_coasts_about_145_s_to_standstill() -> None:
    clock = ManualClock()
    drive = await at_nameplate_speed(clock)
    # The mains goes: the simulator's freewheel reaction, torque removed at once.
    drive.inject_fault(DriveFault.UNDERVOLTAGE)
    assert state_of(drive) is SimState.FAULT_REACTION_FREEWHEEL
    cut = clock.monotonic()
    coasted: Seconds | None = None
    a_minute_later: MotorRpm | None = None
    for _ in range(round(UP_TO / STEP)):
        drive.advance(clock.advance(STEP))
        if a_minute_later is None and elapsed(cut, clock.monotonic()) >= 60.0:
            a_minute_later = (await status_of(drive)).output_rpm
        if state_of(drive) is SimState.FAULT:
            coasted = elapsed(cut, clock.monotonic())
            break
    assert coasted is not None, "the shaft never came to rest"
    # Nothing slows the arm but its own losses: still turning a minute after the cut.
    assert a_minute_later is not None
    assert a_minute_later == round(1380.0 * math.exp(-3.0))
    # rpm(t) = 1380 exp(-t / tau_coast) < standstill_rpm, first reached on a step.
    law = DEFAULT_SIM_CONFIG.tau_coast * math.log(1380.0 / DEFAULT_SIM_CONFIG.standstill_rpm)
    assert law <= coasted < law + STEP
    assert round(coasted, 1) == 144.6  # the figure the docs quote, about 145 s
    assert (await status_of(drive)).output_rpm == MotorRpm(0)
