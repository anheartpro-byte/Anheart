"""A :class:`~src.motor.drive.DriveBackend` that makes the drive lie, refuse or freeze.

:class:`~src.motor.simulated.SimulatedDrive` already models the honest
failures (a latched fault, a dead link, latency, a register offset, swapped
phases). This wrapper adds the four a drive can commit while the link looks
healthy, each one a way for the runtime to believe something false:

* **a refused command word** - the drive answers one ``ControlWord`` with a
  Modbus exception (``BadResponse``), every time, from the moment it is armed;
* **an echo mismatch** - the LFRD echo stops following what is written (it
  keeps the value it had): the write "landed" as far as the ack says, the
  read-back says otherwise;
* **a stuck speed** - RFRD stays where it was: the measured speed no longer
  follows the setpoint (a slipping coupling, a dead encoder, a jammed arm);
* **a frozen drive** - every status read answers ``Ok`` with the status seen
  when it froze: stale content behind a fresh-looking success.

The plant behind it is left alone: the real simulator still integrates on every
call, so what the shaft really does after teardown is still the truth the
checks read.
"""

from __future__ import annotations

from dataclasses import replace
from typing import final

from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    BadResponse,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.result import Err, Ok, Result
from src.units import MotorRpm, Seconds


@final
class FaultyDrive:
    """Delegates to ``inner``; lies as armed. Mutable, owned by the harness loop."""

    __slots__ = ("_echo_held", "_freeze", "_frozen", "_inner", "_refused", "_speed_held", "_stick")

    def __init__(self, inner: DriveBackend) -> None:
        self._inner: DriveBackend = inner
        self._refused: set[ControlWord] = set()
        self._echo_held: MotorRpm | None = None
        self._stick: bool = False
        self._speed_held: MotorRpm | None = None
        self._freeze: bool = False
        self._frozen: DriveStatus | None = None

    # -- arming -------------------------------------------------------------

    def refuse(self, word: ControlWord) -> None:
        """Answer ``word`` with a Modbus exception from now on."""
        self._refused.add(word)

    def mismatch_echo(self, held: MotorRpm) -> None:
        """Report ``held`` as the LFRD echo from now on, whatever is written."""
        self._echo_held = held

    def stick_speed(self) -> None:
        """Report RFRD at its next-read value from now on."""
        self._stick = True

    def freeze_status(self) -> None:
        """Answer every status read with the next one's content, forever."""
        self._freeze = True

    # -- DriveBackend -------------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self._inner.acquisition_evidence

    async def open(self) -> Result[None, DriveError]:
        return await self._inner.open()

    async def close(self) -> Result[None, DriveError]:
        return await self._inner.close()

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        if word in self._refused:
            return Err(BadResponse(detail=f"injected: the drive refuses {word.name} (exception)"))
        return await self._inner.write_command(word)

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        return await self._inner.write_speed(rpm)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        result = await self._inner.read_status()
        if isinstance(result, Err):
            return result
        status = result.value
        if self._freeze:
            if self._frozen is None:
                self._frozen = status
            return Ok(self._frozen)
        if self._stick:
            if self._speed_held is None:
                self._speed_held = status.output_rpm
            status = replace(status, output_rpm=self._speed_held)
        if self._echo_held is not None:
            status = replace(status, setpoint_echo_rpm=self._echo_held)
        return Ok(status)

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return await self._inner.read_limits()

    @property
    def emergency_budget(self) -> Seconds:
        return self._inner.emergency_budget

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        return self._inner.emergency_disable_blocking(timeout)
