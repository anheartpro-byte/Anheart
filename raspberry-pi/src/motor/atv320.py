"""The real Modbus RTU driver for the ATV320. The ONLY module that imports pymodbus.

This is the isolation module required by contract rule 5: ``pymodbus`` and
``serial`` are touched here and nowhere else, so everything above
:mod:`src.motor.drive` sees domain types only. It implements
``drive.DriveBackend`` and adds :meth:`ATV320Drive.enable`, which is a
mechanism (the CiA402 start sequence) rather than a policy.

Hardware, from the commissioning notes: ATV320U04M2C driving a SEW KA37
DRS71S4 through i = 49.79. Modbus RTU on the drive's embedded RJ45,
**address 1, 19200 baud, 8E1** - which is where the constructor defaults come
from. Nothing is hardcoded: every transport parameter arrives in
:class:`SerialSettings` so the bench can change one without editing a safety
module.

What this driver guarantees, and what it refuses to
---------------------------------------------------

* **Nothing blocks the event loop.** Every pymodbus call is blocking, and the
  same loop serves the operator's emergency-stop endpoint. A 0.3 s serial read
  taken on the loop is 0.3 s during which the button does nothing, so all of
  them go through ``run_in_executor`` on a private single-thread pool.
* **One transaction at a time.** RS-485 is half duplex: two overlapping
  transactions do not take turns, they transmit over each other and each gets
  the other's reply. An ``asyncio.Lock`` enforces single flight, and the
  executor has exactly one worker so the invariant survives anything that
  finds a way around the lock.
* **A write is not a landing.** Every speed write is read back (see
  :meth:`ATV320Drive.write_speed`). Most Altivar parameters are writable while
  running, so a misaddressed write does not bounce - it is acked while landing
  in ACC/DEC/HSP next door, and every later read still looks normal.
* **No internal software watchdog, deliberately.** The independent watchdog is
  the drive's own ``ttO`` Modbus timeout, configured on the drive and therefore
  outside this process. Any watchdog written here would die with the event loop
  it was supposed to be watching, which is precisely the failure it claims to
  cover. What this code offers instead is the complementary half: after
  :data:`DEFAULT_FAILURE_THRESHOLD` consecutive failed transactions it latches
  the link down and **stops writing**, so the drive stops hearing a keepalive
  and applies ``ttO`` for real. Only :meth:`ATV320Drive.open` clears that
  latch, because there is no automatic resumption of motion anywhere in this
  system.
* **No policy.** No speed clamp, no ramp shaping, no retry loop, no fault
  reset. Those decisions have a person inside the centrifuge attached to them
  and belong in the safety layer, where they can be tested against a plant
  model.

pymodbus 3.7.4 facts this module is pinned to (verified against the installed
package, not assumed - the keyword names moved across 3.x)
---------------------------------------------------------

* ``ModbusSerialClient(port, framer=FramerType.RTU, baudrate, bytesize,
  parity, stopbits, timeout, retries)``; constructing it does **not** open the
  port, ``connect()`` does.
* The slave-address keyword is ``slave``:
  ``read_holding_registers(address, count=1, slave=1)`` and
  ``write_register(address, value, slave=1)``. Not ``unit``, not ``device_id``.
* The package ships ``py.typed``, so no hand-written stub is needed - but its
  annotations are **not honest about failure**. ``read_holding_registers`` is
  annotated ``-> ModbusPDU`` while ``SyncModbusTransactionManager.execute``
  *returns* a ``ModbusIOException`` instance when the drive does not answer,
  and ``ModbusBaseSyncClient.execute`` *raises* ``ConnectionException`` when
  the port will not open. Both shapes are handled here, which is why
  :class:`ModbusMaster` types replies as ``object``: the lie has to be
  narrowed, and an ``object`` cannot be dereferenced by accident.
* ``ModbusPDU`` declares ``registers: list[int]`` in ``__init__`` without ever
  assigning it, so a PDU that carries no register block (a write echo, say)
  raises ``AttributeError`` on access rather than answering empty. Guarded.
* ``serial.SerialException`` derives from ``OSError`` while ``ModbusException``
  does not, so catching ``OSError`` covers pyserial without importing it.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol, final, runtime_checkable

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException
from pymodbus.framer import FramerType
from pymodbus.pdu import ExceptionResponse, ModbusPDU

from src.clock import Clock
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    DriveStatus,
    RegisterMap,
    UnexpectedState,
    decode_current,
    decode_speed,
    decode_status_word,
    describe_fault,
)
from src.result import Err, Ok, Result, err_of
from src.units import (
    Monotonic,
    MotorRpm,
    RawRegister,
    RegisterAddress,
    Seconds,
    StatusWord,
    elapsed,
    raw_register,
    signed_to_register,
)

logger = logging.getLogger(__name__)


# =========================================================================
# The pymodbus seam
# =========================================================================


@runtime_checkable
class ModbusMaster(Protocol):
    """Exactly the four pymodbus operations this driver performs.

    Narrowing the library to four methods buys two things. It is the whole
    surface a test double has to implement, so the driver's error handling can
    be exercised without a serial port; and it lets the replies be typed
    ``object``.

    That ``object`` is the important part. pymodbus annotates both calls
    ``-> ModbusPDU``, but its synchronous transaction manager **returns** a
    ``ModbusIOException`` when the drive does not answer, and returns ``None``
    when a caller sets ``no_response_expected``. Typing the reply the way the
    library promises would let this module dereference a value that is not a
    PDU; typing it as ``object`` forces the narrowing reality requires.
    """

    def connect(self) -> bool:
        """Open the serial port. ``False`` means it did not open."""
        ...

    def close(self) -> None:
        """Release the serial port."""
        ...

    def read_holding_registers(self, address: int, count: int = 1, slave: int = 1) -> object:
        """Modbus function 3."""
        ...

    def write_register(self, address: int, value: int, slave: int = 1) -> object:
        """Modbus function 6."""
        ...


@unique
class Parity(Enum):
    """Serial parity, in the single-character spelling pymodbus and pyserial use.

    An enum rather than a bare ``str`` because "E" is a value from a fixed set
    with meaning attached, and this drive is commissioned for 8**E**1: a silent
    "N" gives a port that opens and then fails every CRC.
    """

    NONE = "N"
    EVEN = "E"
    ODD = "O"


# --- Transport defaults, all from the commissioning notes ---------------
DEFAULT_BAUDRATE: Final[int] = 19200
DEFAULT_BYTESIZE: Final[int] = 8
DEFAULT_PARITY: Final[Parity] = Parity.EVEN
DEFAULT_STOPBITS: Final[int] = 1

DEFAULT_TIMEOUT: Final[Seconds] = Seconds(0.3)
"""Per-transaction serial timeout.

Sized against the control budget, not against the drive: the loop runs at 5 Hz
(200 ms), a status read costs four transactions, and a timeout long enough to
feel "generous" turns one missing reply into a missed control cycle.
"""

DEFAULT_SLAVE_ADDRESS: Final[int] = 1
DEFAULT_RETRIES: Final[int] = 1
"""No pymodbus-internal retransmission.

A retry is a policy decision - it depends on whether somebody is in the
machine - and a hidden one multiplies the timeout inside a single ``await``,
silently spending a budget the layer above thinks it still has.
"""

MODBUS_SLAVE_MIN: Final[int] = 1
MODBUS_SLAVE_MAX: Final[int] = 247
"""Modbus RTU unit-address range. 0 is the BROADCAST address and is rejected
below: a broadcast write commands every drive on the bus and gets no reply, so
it is both wider than intended and impossible to verify."""


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Everything needed to open the drive's port, and nothing else.

    Frozen so the link cannot be re-parameterised under a running session, and
    a plain record so the operator-visible configuration is one object to log.

    Two fields are validated at construction, in the style
    :class:`src.motor.drive.RegisterMap` already sets (startup boundary,
    nothing spinning, refusing to start is the correct answer). The rest are
    not: a wrong baud rate or parity fails loudly on the first CRC, whereas a
    wrong unit address talks confidently to the wrong device.
    """

    port: str
    """OS device name: ``/dev/ttyUSB0`` on the Pi, ``COM3`` on a bench laptop."""

    baudrate: int = DEFAULT_BAUDRATE
    bytesize: int = DEFAULT_BYTESIZE
    parity: Parity = DEFAULT_PARITY
    stopbits: int = DEFAULT_STOPBITS
    timeout: Seconds = DEFAULT_TIMEOUT
    slave_address: int = DEFAULT_SLAVE_ADDRESS
    retries: int = DEFAULT_RETRIES

    def __post_init__(self) -> None:
        if self.slave_address < MODBUS_SLAVE_MIN or self.slave_address > MODBUS_SLAVE_MAX:
            raise ValueError(
                f"Modbus slave address {self.slave_address} is outside "
                f"{MODBUS_SLAVE_MIN}..{MODBUS_SLAVE_MAX}; address 0 is the broadcast "
                "address, which commands every drive on the bus and cannot be verified"
            )
        if self.timeout <= 0.0:
            raise ValueError(
                f"serial timeout {self.timeout} s must be positive; a zero timeout "
                "makes every read return nothing, which this driver would report as "
                "a dead drive"
            )


def serial_master(settings: SerialSettings) -> ModbusMaster:
    """Build the real pymodbus RTU master. The ONE place the client is constructed.

    Constructing does not touch the hardware - ``ModbusSerialClient`` opens the
    port in ``connect()`` - so this is safe at startup and testable with a port
    name that does not exist.
    """
    return ModbusSerialClient(
        port=settings.port,
        framer=FramerType.RTU,
        baudrate=settings.baudrate,
        bytesize=settings.bytesize,
        parity=settings.parity.value,
        stopbits=settings.stopbits,
        timeout=settings.timeout,
        retries=settings.retries,
    )


# =========================================================================
# Driver tuning
# =========================================================================

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
"""Consecutive failed transactions before the link is latched down.

More than one, because a single lost frame on an RS-485 pair is normal and
tripping on it would stop sessions for noise. Small, because every failure
costs a whole serial timeout while the layer above waits.
"""

DEFAULT_SETTLE_ATTEMPTS: Final[int] = 3
"""ETA reads allowed per :meth:`ATV320Drive.enable` step before giving up.

A command word takes effect on the drive's next internal scan, so the first
read after a write can legitimately still show the previous state. One read
would make the start sequence fail on the bench for a few milliseconds'
reason. Bounded, and it never waits out a FAULT.
"""

DEFAULT_SETTLE_DELAY: Final[Seconds] = Seconds(0.02)
"""Pause between those ETA reads. Roughly one drive scan cycle."""

ENABLE_SEQUENCE: Final[tuple[tuple[ControlWord, DriveState], ...]] = (
    (ControlWord.SHUTDOWN, DriveState.READY),
    (ControlWord.SWITCH_ON, DriveState.SWITCHED_ON),
    (ControlWord.ENABLE_OPERATION, DriveState.OPERATION_ENABLED),
)
"""The CiA402 start sequence, each command word paired with the state the drive
must reach before the next one is sent. 6 -> 7 -> 15, from the commissioning
notes. Writing all three blind is the failure this pairing prevents: if the
drive did not reach SWITCHED_ON, sending 15 asks a drive in an unknown state to
energise its output."""


def _new_executor() -> ThreadPoolExecutor:
    """One worker thread, so the half-duplex bus is serialised by construction.

    A second worker would let two transactions overlap on the wire the moment
    anything bypassed the ``asyncio.Lock``. The thread is created lazily on the
    first submission, so building this is free.
    """
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="atv320")


# Why this module uses `isinstance(outcome, Err)` early returns rather than
# `match Ok()/Err()` throughout: it never BRANCHES on which DriveError it got,
# it only propagates one. The nested-match-plus-`assert_never` discipline of
# contract rule 3 exists for callers that choose a different response per
# variant - the safety layer - and it earns its keep there. Here it would buy
# nothing, and every such match leaves an unreachable "no case matched"
# fall-through arc that the 100%-branch gate cannot close without a `pragma`.
# `isinstance` narrows in BOTH directions, so the success value stays fully
# typed with no sentinel and no possibly-undefined local.


@final
class ATV320Drive:
    """``DriveBackend`` over Modbus RTU. Never raises on the drive path.

    Construction is inert: no port is opened and no thread is started until
    :meth:`open` and the first transaction. The ``master`` is injected rather
    than built here so the whole error-mapping path can be tested against a
    fake bus, and so production has exactly one call to :func:`serial_master`
    to audit.

    ``registers`` has no default on purpose. The addressing offset is the one
    piece of configuration that can turn a correct speed write into a write
    into ACC/DEC/HSP, so it must be stated by the caller rather than defaulted
    to a value nobody vouched for.
    """

    __slots__ = (
        "_clock",
        "_consecutive_failures",
        "_executor",
        "_failed_since",
        "_failure_threshold",
        "_lock",
        "_lost_since",
        "_master",
        "_registers",
        "_settings",
        "_settle_attempts",
        "_settle_delay",
    )

    def __init__(
        self,
        clock: Clock,
        master: ModbusMaster,
        settings: SerialSettings,
        registers: RegisterMap,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        settle_attempts: int = DEFAULT_SETTLE_ATTEMPTS,
        settle_delay: Seconds = DEFAULT_SETTLE_DELAY,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(
                f"failure_threshold {failure_threshold} must be at least 1; a "
                "threshold of 0 would latch the link down before any transaction "
                "had been attempted"
            )
        if settle_attempts < 1:
            raise ValueError(
                f"settle_attempts {settle_attempts} must be at least 1; with 0 the "
                "enable sequence would never read ETA and so would verify nothing"
            )
        self._clock: Clock = clock
        self._master: ModbusMaster = master
        self._settings: SerialSettings = settings
        self._registers: RegisterMap = registers
        self._failure_threshold: int = failure_threshold
        self._settle_attempts: int = settle_attempts
        self._settle_delay: Seconds = settle_delay

        # WHY a lock and not just careful call ordering: RS-485 is half duplex.
        # Two coroutines that each write-then-read do not interleave politely,
        # they transmit over each other and read each other's replies.
        self._lock: asyncio.Lock = asyncio.Lock()
        self._executor: ThreadPoolExecutor = _new_executor()

        # Failure accounting. `_failed_since` is the first failure of the
        # current run (None while healthy); `_lost_since` is set when a run
        # reaches the threshold and is cleared ONLY by open(), because a link
        # that healed on its own is not permission to command a motor again.
        self._consecutive_failures: int = 0
        self._failed_since: Monotonic | None = None
        self._lost_since: Monotonic | None = None

    # --- Observable state ------------------------------------------------

    @property
    def link_lost(self) -> bool:
        """Whether the link is latched down and this driver has stopped writing.

        Exposed so an operator screen can say "link latched, open() required"
        instead of showing a generic timeout for ever.
        """
        return self._lost_since is not None

    # =====================================================================
    # DriveBackend
    # =====================================================================

    async def open(self) -> Result[None, DriveError]:
        """Acquire the link and prove the addressing. Commands nothing.

        The ETA read at the end is the only evidence that the unit address and
        ``RegisterMap.offset`` are both usable, and it is a **read**: an offset
        discovered to be wrong by writing has already written a speed into a
        neighbouring parameter. An address the drive does not implement answers
        with exception code 2, which is what makes this a real check.

        This is also the only place the comms latch is cleared, so re-arming a
        link that dropped is an explicit act.
        """
        async with self._lock:
            # A previous close() shut the pool down; a fresh one keeps this
            # object reusable without a branch only production would take.
            self._executor.shutdown(wait=False)
            self._executor = _new_executor()
            self._consecutive_failures = 0
            self._failed_since = None
            self._lost_since = None

            connected = await self._transact(self._blocking_connect)
            if isinstance(connected, Err):
                return Err(connected.error)
            return await self._confirm_addressing()

    async def close(self) -> Result[None, DriveError]:
        """Remove the run command, then release the port. Latches the link down.

        Dropping the port while the drive is in OPERATION_ENABLED would leave a
        commanded motor with nothing talking to it, relying entirely on the
        drive's ``ttO`` timeout. So the stop is attempted first - and the port
        is released either way, because a driver that will not let go of a
        serial port because a write failed is a driver nobody can restart.

        The error returned is the stop attempt's, not the port's: the caller
        needs to know the motor may still be commanded, which matters strictly
        more than how tidily the file descriptor went away.
        """
        async with self._lock:
            stop_error = await self._attempt_stop()
            await self._run(self._blocking_close)
            self._executor.shutdown(wait=False)
            self._lost_since = self._clock.monotonic()
            if stop_error is not None:
                return Err(stop_error)
            return Ok(None)

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        """Write CMD. Enabling the output stage means the motor may turn.

        Unverified on purpose: CMD has no meaningful echo of its own, and the
        evidence that a command word landed is the state the drive moves to.
        :meth:`enable` is the verified path; a caller using this primitive is
        responsible for reading ETA afterwards.
        """
        async with self._lock:
            refusal = self._refusal()
            if refusal is not None:
                return Err(refusal)
            return await self._write(self._registers.cmd, RawRegister(word.value))

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        """Write LFRD, a signed MOTOR-shaft setpoint, and read it back.

        The read-back exists because a Modbus write response echoes the
        *request*: a write the drive did not honour - a value it clamped or
        refused, or an address that is not actually the speed reference - is
        acked exactly like a write that landed, and every later read still
        looks normal. Reading LFRD back is the only thing that can tell the
        difference.

        Its limit, stated plainly because overstating it would be worse than
        not having it: if the offset is wrong by the SAME amount everywhere,
        the write and the read-back go to the same wrong register, agree, and
        this check passes. A uniform offset is caught by the ETA read in
        :meth:`open` and by exception responses from addresses the drive does
        not implement - and finally only by the bench. A test pins that limit,
        so nobody reads more into this guard than it actually gives.

        The comparison is exact, deliberately. If the bench shows the drive
        clamping LFRD to HSP (so the echo legitimately differs from a setpoint
        above the ceiling), the fix is to keep the software ceiling below HSP -
        which the safety layer does anyway - and NOT to loosen this check: a
        tolerance wide enough to absorb clamping is wide enough to absorb a
        write that never landed.
        """
        async with self._lock:
            refusal = self._refusal()
            if refusal is not None:
                return Err(refusal)
            # Signed 16-bit at the boundary: -1 rpm goes out as 0xFFFF, and
            # read back as unsigned it would look like 65535 rpm and sail
            # through any ceiling expressed as "less than 900".
            encoded = signed_to_register(rpm)
            if isinstance(encoded, Err):
                return Err(encoded.error)
            return await self._write_verified_speed(rpm, encoded.value)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        """Read ETA, LFRD, RFRD and LCR (plus LFT when faulted) as one observation.

        Every register is parsed and range-checked here, at the boundary, so
        nothing downstream re-validates: that is contract rule 6.
        """
        async with self._lock:
            refusal = self._refusal()
            if refusal is not None:
                return Err(refusal)
            return await self._assemble_status()

    def emergency_disable_blocking(self, timeout: Seconds) -> None:
        """Zero the setpoint and remove the run command, synchronously. Never raises.

        **No asyncio.** This runs in the calling thread so it works from
        ``atexit``, from an OS signal handler, and from an ``except`` branch -
        places where the loop may be gone or may be the thing that died. It
        does not take the ``asyncio.Lock`` either, for the same reason.

        Two blind writes: LFRD = 0 first, then CMD = SHUTDOWN. In that order
        because if only one lands it should be the one that removes the speed
        demand. Neither reply is interpreted: there is nobody left to act on a
        diagnosis, and waiting to parse one only spends the budget.

        Bounded by ``timeout``, checked before each write. The honest caveat: if
        a transaction is already in flight on the executor thread, pymodbus's
        own internal transaction lock makes this call wait it out, so the real
        worst case is ``timeout`` plus one serial timeout. Still finite, which
        is the property that matters - an unbounded call here hangs process
        exit, and a Pi that will not shut down gets power-cycled mid-session.

        Returning does **not** mean the motor stopped. With STO jumpered there
        is no independent torque removal and the ramp takes seconds; this
        reports only that the attempt was made. It then latches the link down
        so the async side cannot re-command a speed on the next tick of a loop
        that is still alive.
        """
        deadline = Monotonic(self._clock.monotonic() + timeout)
        self._blind_write(self._registers.lfrd, RawRegister(0), deadline)
        self._blind_write(self._registers.cmd, RawRegister(ControlWord.SHUTDOWN.value), deadline)
        self._lost_since = self._clock.monotonic()

    # =====================================================================
    # The CiA402 start sequence
    # =====================================================================

    async def enable(self) -> Result[None, DriveError]:
        """Take the drive to OPERATION_ENABLED, verifying ETA at every step.

        6 -> 7 -> 15, and after each word the drive must actually report the
        state that word asks for before the next one is sent. Writing the three
        blind would mean asking a drive in an unknown state to energise its
        output, and a drive that did not follow would be indistinguishable from
        one that did.

        A fault seen at any point returns :class:`DriveFaulted` with the LFT
        code rather than ``UnexpectedState(..., FAULT)``: the operator needs the
        mnemonic, and "unexpected state" understates a latched fault.

        Holds the single-flight lock for the whole sequence (six transactions,
        ~120 ms). Letting another command interleave between 7 and 15 would be
        worse than the wait; the emergency path does not use this lock.

        **What this does NOT do:** it does not zero the setpoint first. If LFRD
        still holds a non-zero value from an earlier session, writing 15 starts
        the motor at that speed. Whether to refuse that is policy - it depends
        on whether somebody is in the machine - so the safety layer must write
        speed 0 before calling this. A test pins the fact that ``enable``
        writes no speed of its own, so the omission stays a decision rather
        than becoming an oversight.
        """
        async with self._lock:
            refusal = self._refusal()
            if refusal is not None:
                return Err(refusal)
            return await self._run_enable_sequence()

    async def _run_enable_sequence(self) -> Result[None, DriveError]:
        """The sequence itself. Lock already held."""
        # Look before writing anything: a faulted drive cannot be enabled, and
        # sending SHUTDOWN to it would only be the first of three pointless
        # writes.
        observed = await self._observe_state()
        if isinstance(observed, Err):
            return Err(observed.error)
        if observed.value is DriveState.FAULT:
            return Err(await self._fault_error())

        for word, expected in ENABLE_SEQUENCE:
            step = await self._enable_step(word, expected)
            if isinstance(step, Err):
                return step
        return Ok(None)

    async def _enable_step(
        self, word: ControlWord, expected: DriveState
    ) -> Result[None, DriveError]:
        """Write one command word and confirm the drive reached ``expected``."""
        written = await self._write(self._registers.cmd, RawRegister(word.value))
        if isinstance(written, Err):
            return written
        settled = await self._settled_state(expected)
        if isinstance(settled, Err):
            return Err(settled.error)
        if settled.value is expected:
            return Ok(None)
        if settled.value is DriveState.FAULT:
            return Err(await self._fault_error())
        return Err(UnexpectedState(expected=expected, actual=settled.value))

    async def _settled_state(self, expected: DriveState) -> Result[DriveState, DriveError]:
        """Read ETA until it reports ``expected``, at most ``settle_attempts`` times.

        Returns the LAST state observed so the caller can report what it
        actually saw. A FAULT returns immediately: there is nothing to wait for,
        and sleeping through a fault reaction is sleeping while a loaded
        centrifuge decelerates.
        """
        outcome = await self._observe_state()
        for _ in range(self._settle_attempts - 1):
            if isinstance(outcome, Err):
                return outcome
            if outcome.value is expected or outcome.value is DriveState.FAULT:
                return outcome
            await asyncio.sleep(self._settle_delay)
            outcome = await self._observe_state()
        return outcome

    async def _fault_error(self) -> DriveError:
        """Name the latched fault from LFT so the caller gets a mnemonic, not a state."""
        outcome = await self._read(self._registers.lft)
        if isinstance(outcome, Err):
            # The link died while we were asking which fault it was. That
            # failure is now the more urgent one, and inventing a fault name
            # here would be worse than reporting the link.
            return outcome.error
        report = describe_fault(outcome.value)
        return DriveFaulted(fault=report.fault, raw_code=report.raw_code)

    # =====================================================================
    # Composite operations (the single-flight lock is already held)
    # =====================================================================

    async def _confirm_addressing(self) -> Result[None, DriveError]:
        """One ETA read, purely as evidence that the link and addressing work."""
        outcome = await self._read(self._registers.eta)
        if isinstance(outcome, Err):
            return Err(outcome.error)
        # Logged rather than judged: seeing OPERATION_ENABLED here means a
        # previous process died with the motor turning, and what to do about
        # that (command zero, disable, latch, require an operator
        # acknowledgement) is the safety layer's decision, not the transport's.
        logger.info(
            "ATV320 link open on %s: ETA=0x%04X -> %s",
            self._settings.port,
            outcome.value,
            decode_status_word(StatusWord(outcome.value)).name,
        )
        return Ok(None)

    async def _attempt_stop(self) -> DriveError | None:
        """Zero the setpoint and remove the run command. ``None`` when both landed."""
        refusal = self._refusal()
        if refusal is not None:
            # Nothing to write to. Saying so is more useful than two writes
            # into a link this driver has already declared dead.
            return refusal
        zeroed = err_of(await self._write(self._registers.lfrd, RawRegister(0)))
        # SHUTDOWN is attempted even if zeroing failed: removing the run command
        # is what removes torque demand, and one lost frame is no reason to skip
        # it. The first error is the one reported, because it happened first.
        removed = err_of(
            await self._write(self._registers.cmd, RawRegister(ControlWord.SHUTDOWN.value))
        )
        if zeroed is not None:
            return zeroed
        return removed

    async def _write_verified_speed(
        self, rpm: MotorRpm, value: RawRegister
    ) -> Result[None, DriveError]:
        """Write LFRD then read it back. See :meth:`write_speed` for why."""
        written = await self._write(self._registers.lfrd, value)
        if isinstance(written, Err):
            return written
        echo = await self._read(self._registers.lfrd)
        if isinstance(echo, Err):
            return Err(echo.error)
        landed = decode_speed(echo.value)
        if landed == rpm:
            return Ok(None)
        return Err(
            BadResponse(
                detail=(
                    f"LFRD write-verify failed: wrote {rpm} rpm (register "
                    f"0x{value:04X}) to address {self._registers.lfrd}, read back "
                    f"{landed} rpm (0x{echo.value:04X}). The setpoint did NOT land. "
                    "Suspect a value the drive refused or clamped, or an address that "
                    "is not the speed reference at all."
                )
            )
        )

    async def _assemble_status(self) -> Result[DriveStatus, DriveError]:
        """Build one :class:`DriveStatus`. Lock already held."""
        readings = await self._read_status_registers()
        if isinstance(readings, Err):
            return Err(readings.error)
        eta_raw, lfrd_raw, rfrd_raw, lcr_raw = readings.value
        word = StatusWord(eta_raw)
        state = decode_status_word(word)
        named = await self._read_fault(state)
        if isinstance(named, Err):
            return Err(named.error)
        return Ok(
            DriveStatus(
                state=state,
                status_word=word,
                setpoint_echo_rpm=decode_speed(lfrd_raw),
                output_rpm=decode_speed(rfrd_raw),
                current=decode_current(lcr_raw),
                fault=named.value,
            )
        )

    async def _read_status_registers(
        self,
    ) -> Result[tuple[RawRegister, RawRegister, RawRegister, RawRegister], DriveError]:
        """Read ETA, LFRD, RFRD and LCR in that order, stopping at the first failure.

        ETA first so the state is the oldest thing in the observation rather
        than the newest: a state read after the speeds could name a state that
        never coexisted with them.

        Short-circuits, because on a dead link four reads would spend four
        serial timeouts - more than the whole 200 ms control budget - learning
        the same thing four times.

        Four separate single-register transactions, not Modbus block reads.
        ETA(3201)..LCR(3204) and LFRD(8602)..RFRD(8604) look contiguous, but the
        addresses in between are unrelated parameters, and a block read spanning
        one the drive does not implement is answered with an exception response
        for the WHOLE block - so the optimisation costs the entire status, not
        one field. It becomes available once the bench has confirmed those
        addresses read, and never by assumption. At 19200 8E1 a transaction is
        ~20 ms, so a status is ~80 ms of the 200 ms available at 5 Hz, ~100 ms
        when faulted.
        """
        values: list[RawRegister] = []
        for address in (
            self._registers.eta,
            self._registers.lfrd,
            self._registers.rfrd,
            self._registers.lcr,
        ):
            outcome = await self._read(address)
            if isinstance(outcome, Err):
                return Err(outcome.error)
            values.append(outcome.value)
        # Exactly four appends in the order above. The indices never leave this
        # function; the caller unpacks named values.
        return Ok((values[0], values[1], values[2], values[3]))

    async def _read_fault(self, state: DriveState) -> Result[DriveFault | None, DriveError]:
        """Name the latched fault, but only when the status word says there is one.

        LFT holds the LAST fault, so reading it unconditionally would hang a
        historical name on a healthy status - and spend a fifth transaction out
        of the control budget every cycle to do it.
        """
        if state is not DriveState.FAULT:
            return Ok(None)
        outcome = await self._read(self._registers.lft)
        if isinstance(outcome, Err):
            return Err(outcome.error)
        return Ok(describe_fault(outcome.value).fault)

    async def _observe_state(self) -> Result[DriveState, DriveError]:
        """One ETA read, decoded."""
        outcome = await self._read(self._registers.eta)
        if isinstance(outcome, Err):
            return Err(outcome.error)
        return Ok(decode_status_word(StatusWord(outcome.value)))

    # =====================================================================
    # One transaction
    # =====================================================================

    async def _read(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        return await self._transact(lambda: self._blocking_read(address))

    async def _write(
        self, address: RegisterAddress, value: RawRegister
    ) -> Result[None, DriveError]:
        return await self._transact(lambda: self._blocking_write(address, value))

    async def _transact[T](self, fn: Callable[[], Result[T, DriveError]]) -> Result[T, DriveError]:
        """Run one blocking transaction off the loop and account for the outcome.

        ``run_in_executor`` here is not an optimisation. The same event loop
        serves the operator's emergency-stop endpoint, and a 0.3 s serial read
        taken on it is 0.3 s in which the button does nothing.
        """
        outcome = await self._run(fn)
        now = self._clock.monotonic()
        if isinstance(outcome, Err):
            return Err(self._note_failure(outcome.error, now))
        self._note_success()
        return outcome

    async def _run[T](self, fn: Callable[[], T]) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn)

    # =====================================================================
    # Failure accounting
    # =====================================================================

    def _refusal(self) -> DriveError | None:
        """The error to return instead of touching a latched-down link.

        This is the "stops writing" half of the watchdog argument: once this
        driver has decided the link is gone it sends nothing more, so the drive
        stops hearing a keepalive and its own ``ttO`` timeout - the watchdog
        that does not live in this process - ramps the motor down.
        """
        if self._lost_since is None:
            return None
        return CommTimeout(after=elapsed(self._lost_since, self._clock.monotonic()))

    def _note_failure(self, error: DriveError, now: Monotonic) -> DriveError:
        """Count a failed transaction; latch the link down at the threshold.

        Below the threshold the caller gets the specific diagnosis, because a
        library that refused the transaction and a drive that went silent need
        different responses. At the threshold it gets :class:`CommTimeout`
        measured from the first observed failure of the run, which is the
        number that says how long the drive has been out of contact.
        """
        self._consecutive_failures += 1
        first = self._failed_since if self._failed_since is not None else now
        self._failed_since = first
        if self._consecutive_failures < self._failure_threshold:
            logger.warning(
                "ATV320 transaction failed (%d/%d consecutive): %r",
                self._consecutive_failures,
                self._failure_threshold,
                error,
            )
            return error
        self._lost_since = first
        logger.error(
            "ATV320 link declared LOST after %d consecutive failures over %.3f s; this "
            "driver will send nothing further until open() is called again, so the "
            "drive's own ttO timeout applies. Last error: %r",
            self._consecutive_failures,
            elapsed(first, now),
            error,
        )
        return CommTimeout(after=elapsed(first, now))

    def _note_success(self) -> None:
        """A completed transaction ends the current failure run.

        It does NOT clear ``_lost_since``. A link that came back on its own is
        not permission to resume commanding a motor; only :meth:`open` is.
        """
        self._consecutive_failures = 0
        self._failed_since = None

    # =====================================================================
    # The blocking half: everything below runs in the executor thread
    # =====================================================================

    def _blocking_connect(self) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        try:
            opened = self._master.connect()
        except Exception as exc:
            return Err(self._classify(exc, started))
        if not opened:
            return Err(CommTimeout(after=elapsed(started, self._clock.monotonic())))
        return Ok(None)

    def _blocking_close(self) -> None:
        """Release the port. Swallows everything: there is no useful failure here.

        A port that will not close cleanly is still a port this process has
        stopped using, and the caller's real question - "is the motor still
        commanded?" - is answered by the stop attempt, not by this.
        """
        try:
            self._master.close()
        except Exception:
            logger.exception("ATV320: releasing %s failed", self._settings.port)

    def _blocking_read(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        started = self._clock.monotonic()
        try:
            reply: object = self._master.read_holding_registers(
                address, count=1, slave=self._settings.slave_address
            )
        except Exception as exc:
            return Err(self._classify(exc, started))
        return self._interpret_read(reply, address, started)

    def _blocking_write(
        self, address: RegisterAddress, value: RawRegister
    ) -> Result[None, DriveError]:
        started = self._clock.monotonic()
        try:
            reply: object = self._master.write_register(
                address, value, slave=self._settings.slave_address
            )
        except Exception as exc:
            return Err(self._classify(exc, started))
        return self._interpret_write(reply, address, started)

    def _blind_write(
        self, address: RegisterAddress, value: RawRegister, deadline: Monotonic
    ) -> None:
        """One fire-and-forget write for the emergency path. Never raises.

        The reply is neither read nor interpreted: an emergency disable has
        nobody to hand a diagnosis to, and a returned error object is not an
        exception, so there is nothing to catch either.
        """
        if self._clock.monotonic() >= deadline:
            logger.error(
                "ATV320 emergency disable: out of budget before writing register %d; "
                "the drive may still be commanded",
                address,
            )
            return
        try:
            self._master.write_register(address, value, slave=self._settings.slave_address)
        except Exception:
            logger.exception(
                "ATV320 emergency disable: write of 0x%04X to register %d failed; "
                "the drive may still be commanded",
                value,
                address,
            )

    # --- Reply interpretation (pure apart from the clock) ----------------

    def _interpret_read(
        self, reply: object, address: RegisterAddress, started: Monotonic
    ) -> Result[RawRegister, DriveError]:
        """Turn whatever pymodbus handed back into one register value or a DriveError."""
        if isinstance(reply, ModbusException):
            # pymodbus 3.7.4's synchronous transaction manager RETURNS the
            # exception object when no reply arrives, while annotating the call
            # `-> ModbusPDU`. Same failure as a raised one, so same classifier.
            return Err(self._classify(reply, started))
        if isinstance(reply, ExceptionResponse):
            return Err(
                BadResponse(
                    detail=(
                        f"drive answered a read of register {address} with Modbus "
                        f"exception code {reply.exception_code}. Code 2 (illegal data "
                        "address) means the address does not exist on this drive: fix "
                        "RegisterMap.offset, do not retry."
                    )
                )
            )
        if not isinstance(reply, ModbusPDU):
            return Err(
                BadResponse(
                    detail=(
                        f"transport returned {type(reply).__name__} for register "
                        f"{address}, which is neither a Modbus PDU nor a ModbusException"
                    )
                )
            )
        try:
            registers: list[int] = reply.registers
        except AttributeError:
            # ModbusPDU declares `registers: list[int]` without ever assigning
            # it, so a PDU carrying no register block raises here instead of
            # answering empty. Reachable on a noisy half-duplex bus, where the
            # framer can decode a stale write echo as the reply to this read.
            return Err(
                BadResponse(
                    detail=(
                        f"reply to register {address} is a {type(reply).__name__}, which "
                        "carries no register data - the framer decoded the wrong PDU"
                    )
                )
            )
        if len(registers) != 1:
            return Err(
                BadResponse(
                    detail=f"expected exactly 1 register from {address}, got {len(registers)}"
                )
            )
        # Contract rule 6: this is the boundary, so the range is checked HERE
        # and never again. An out-of-domain value means a framing or decode
        # problem, not a measurement.
        return raw_register(registers[0])

    def _interpret_write(
        self, reply: object, address: RegisterAddress, started: Monotonic
    ) -> Result[None, DriveError]:
        """Confirm a write was acked. NOT that it landed where it was meant to.

        The echoed address and value are deliberately not compared: a Modbus
        write response echoes the *request*, so a write to the wrong address
        echoes back perfectly. Only a read of the register that was supposed to
        change can tell - see :meth:`_write_verified_speed`.
        """
        if isinstance(reply, ModbusException):
            return Err(self._classify(reply, started))
        if isinstance(reply, ExceptionResponse):
            return Err(
                BadResponse(
                    detail=(
                        f"drive rejected a write to register {address} with Modbus "
                        f"exception code {reply.exception_code}. Code 2 (illegal data "
                        "address) means the address does not exist on this drive; code 3 "
                        "means the value is out of range for that parameter."
                    )
                )
            )
        if not isinstance(reply, ModbusPDU):
            return Err(
                BadResponse(
                    detail=(
                        f"transport returned {type(reply).__name__} for a write to register "
                        f"{address}, which is neither a Modbus PDU nor a ModbusException"
                    )
                )
            )
        return Ok(None)

    def _classify(self, exc: BaseException, started: Monotonic) -> DriveError:
        """Map one transport failure onto the closed ``DriveError`` union.

        ``after`` is measured, never the configured budget: a timeout firing at
        0.6 s when 0.3 s was allowed is a different problem from one firing at
        0.3 s, and only the measurement separates them.
        """
        waited = elapsed(started, self._clock.monotonic())
        if isinstance(exc, ConnectionException | ModbusIOException):
            # ConnectionException: the port would not open, or pymodbus closed
            # it after an I/O error. ModbusIOException: no reply, or a reply
            # that would not decode. Both mean "the drive did not answer".
            return CommTimeout(after=waited)
        if isinstance(exc, ModbusException):
            # ParameterException, NoSuchSlaveException, InvalidMessageReceived:
            # the library refused the transaction rather than the wire losing
            # it, so retrying is not the answer and the text is the diagnosis.
            return BadResponse(detail=f"pymodbus rejected the transaction: {exc}")
        if isinstance(exc, OSError):
            # serial.SerialException derives from OSError, as does a yanked USB
            # adapter. Caught by base class so pyserial need not be imported.
            return CommTimeout(after=waited)
        return BadResponse(
            detail=f"unexpected {type(exc).__name__} from the Modbus transport: {exc}"
        )
