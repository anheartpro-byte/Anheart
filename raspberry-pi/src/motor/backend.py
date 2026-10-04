from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.motor.acquisition import AcquisitionEvidence
from src.result import Result
from src.units import MotorRpm, Seconds

if TYPE_CHECKING:
    from src.motor.drive import (
        ControlWord,
        DriveError,
        DriveLimits,
        DriveStatus,
        EmergencyStopOutcome,
    )


@runtime_checkable
class DriveBackend(Protocol):
    """What a drive must be able to do, whether it is copper or a simulation.

    Runtime-checkable so startup and tests can assert conformance, matching
    ``src.clock.Clock``. Every fallible operation returns a ``Result``: nothing
    here raises, because an exception on this path unwinds while the motor is
    still commanded.

    Implementations own transport, framing, retries and timeouts. They do NOT
    own policy: clamping a setpoint, deciding when to enable, whether to reset
    a fault and how fast to ramp all belong to the safety layer, which is the
    only place they can be tested against a plant model.
    """

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        """Independent evidence retained across failed open, close and wrapper errors."""
        ...

    async def open(self) -> Result[None, DriveError]:
        """Acquire the link. Must NOT enable the drive or command a speed.

        Never assume the drive's state afterwards - read it. If ETA reports
        OPERATION_ENABLED, a previous process died with the motor turning.
        """
        ...

    async def close(self) -> Result[None, DriveError]:
        """Release the link, having first stopped the machine properly.

        Must leave the drive in a state it can be left in, not merely drop the
        port. Closing stops the keepalive, which arms the drive's own ttO
        timeout response; that is a backstop, not a stop command.

        Because this is ``async`` it can afford to WAIT, and waiting is what
        makes the stop a ramp instead of a freewheel. The required order is:

        1. write LFRD = 0 and let the drive decelerate on its own ramp;
        2. read RFRD until the shaft is at standstill, bounded;
        3. only then ``SWITCH_ON`` (transition 5) and ``SHUTDOWN``.

        Dropping the output stage before step 2 has succeeded is CiA402
        transition 8 on a turning centrifuge: see :class:`ControlWord`. An
        implementation that cannot prove standstill must LEAVE THE RUN COMMAND
        IN PLACE and say so (:class:`StopUnconfirmed`), because a zero
        reference plus ttO beats a freewheel by two orders of magnitude.

        Must be idempotent. Teardown paths call close twice - an ``except``
        branch and then a ``finally``, or a shutdown handler and then
        ``atexit`` - and the second call must report the same thing rather than
        raising on a pool that is already gone.
        """
        ...

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        """Write CMD. This can enable the output stage, so the motor may turn."""
        ...

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        """Write LFRD: a signed setpoint in MOTOR-shaft rpm.

        Motor rpm, not output rpm - the gearbox ratio is 49.79, so the two
        differ by a factor of fifty and the type is what keeps them apart.
        Writing is not landing: verify against
        :attr:`DriveStatus.setpoint_echo_rpm` on the next read.
        """
        ...

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        """Read ETA, LFRD, RFRD, LCR (and LFT when faulted) as ONE observation.

        One call rather than five, so the fields of a :class:`DriveStatus`
        describe the same moment: a status assembled from reads seconds apart
        can show a speed that never coexisted with its state.
        """
        ...

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        """Read tFr, HSP, LSP, ACC and dEC. READ ONLY - never writes a parameter.

        Parsed at the boundary into :class:`DriveLimits`; judging them is the
        caller's business (:func:`check_limits`), because the acceptable
        ceiling is a property of the installation, not of the transport.
        """
        ...

    @property
    def emergency_budget(self) -> Seconds:
        """The smallest whole-call bound :meth:`emergency_disable_blocking` can keep.

        A property of the transport, not a number the caller gets to pick: a
        blocking exchange cannot be cut short, so only the backend knows how
        long its one write can really take (on the Modbus link, two worst-case
        transactions - see ``src.motor.atv320.emergency_budget_for``). Callers
        pass this, so the bound they plan around is the one that holds; a
        smaller figure is reported and replaced, never obeyed by writing
        nothing.
        """
        ...

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        """Zero the speed reference, synchronously, best effort.

        **Synchronous on purpose.** This has to be callable from ``atexit``,
        from an OS signal handler, and from an ``except`` branch - places where
        there may be no running event loop, where the loop may be the thing
        that died, and where ``await`` is simply not available. A disable that
        only exists as a coroutine is a disable that does not happen on the
        paths that need it most.

        **It does NOT remove the run command, and that is the whole design.**
        Unlike :meth:`close` this call is budget-bounded, so it cannot wait for
        standstill; and a run command removed from a turning machine is CiA402
        transition 8, i.e. a freewheel (see :class:`ControlWord`). The zeroed
        reference plus the drive's own commissioned ramp is the fastest stop
        actually available here, so that is all this does. The drive is left in
        OPERATION_ENABLED on purpose: once this process stops writing, ``ttO``
        fires and ramps it down for real.

        Contract for implementations:

        * Send the write that matters - LFRD = 0 - **unconditionally**. Spend
          the budget on anything optional, never on that one. A budget check
          that can skip it turns this method into a silent no-op.
        * Make the bound real. ``timeout`` must bound the whole call, including
          however long the transport's own read timeout is; if the two cannot
          be reconciled, say which one won rather than blocking for the larger.
        * Never raise. There is nobody left up the stack to handle it: log,
          and report through the return value.
        * Report what happened (:class:`EmergencyStopOutcome`) so an
          ``atexit`` or signal path can escalate. In particular
          ``NOTHING_SENT`` must be distinguishable from success.
        * Returning does NOT mean the motor has stopped. It means the attempt
          was made. With STO jumpered there is no independent torque removal,
          and the ramp-down takes seconds (a faster stop trips ObF into
          freewheel, which is slower still). Nothing may report "stopped" on
          the strength of this call returning.
        """
        ...
