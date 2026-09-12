"""A simulated ATV320: the only drive that exists until the hardware does.

This is not a mock. Every rule the safety supervisor will enforce - when it is
legal to enable the output stage, how long a stop actually takes, what a
coasting centrifuge looks like, what the drive does when this software goes
silent - has to be tested against *something* that behaves like a drive, and it
has to keep being tested that way forever, because CI has no ATV320 in it.
Anything this module fakes loosely becomes a class of bug that is first
discovered with a person sitting inside the machine.

What it models, in order of how much it matters:

1. **The CiA402 state machine, strictly.** Out-of-order command words are
   REFUSED, with the state that was required and the state that was found. A
   caller that writes 6 then 15, skipping 7, gets ``Err(UnexpectedState(...))``
   here and gets a drive that silently never starts on the bench. Catching that
   in CI is the single most valuable thing this module does.
2. **Time, through an injected clock only.** The plant advances in
   :meth:`SimulatedDrive.advance`, which takes ``now``; the protocol methods
   bring it up to date from the injected :class:`~src.clock.Clock`. Nothing
   here reads the system clock, so a 45-minute session runs in milliseconds
   (contract rule 4, enforced repo-wide by ``tests/test_clock.py``).
3. **The stop that is not instant, and the two stops that are not the same.**
   Torque exists while the drive is commanded, while a ramp-stop fault reaction
   is in progress, and while a commanded ramp-stop (CiA402 transition 5, command
   word 7) is bringing the shaft down. Everywhere else - including after command
   word 6, which is transition 8 and drops the output stage - the shaft coasts
   on ``exp(-dt / tau_coast)``, because that is what ~420 J of rotating mass
   does against a DC bus that can absorb ~11 J of it. A simulator that snapped
   to 0 rpm would quietly bless a safety rule that assumes a stop command stops
   the machine; a simulator in which 6 and 7 behave alike - which this one was -
   quietly blesses a stop sequence that freewheels.
4. **The drive's own ttO watchdog.** Go silent for ``tto`` seconds and the
   drive latches SLF and stops the motor itself. That backstop is the reason
   ``drive.service()`` is called first in the control tick, and this is the
   only place it can be tested.

Deliberate asymmetries, all in the same direction: **where the real drive's
behaviour is unknown or configurable, the simulator picks the stricter or
slower option.** Software that satisfies this model therefore also satisfies
the hardware, never the other way round. Each one is commented where it lives,
and they are the first thing to revisit once the bench answers the question.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto, unique
from types import MappingProxyType
from typing import Final, final

from src.clock import Clock
from src.motor.drive import (
    LFT_FAULT_CODES,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    FaultReport,
    UnexpectedState,
    decode_status_word,
    describe_fault,
)
from src.result import Err, Ok, Result, is_err
from src.units import (
    Amperes,
    Monotonic,
    MotorRpm,
    RawRegister,
    RpmPerSecond,
    Seconds,
    StatusWord,
    elapsed,
    signed_to_register,
)

# =========================================================================
# The drive's own state, which is finer than DriveState
# =========================================================================


@unique
class SimState(Enum):
    """Where the simulated drive really is, as CiA402 defines it.

    Finer than :class:`~src.motor.drive.DriveState` on purpose. ``DriveState``
    is what a caller can *observe* through one ETA word; this is what the drive
    knows about itself. The gap between the two is not an accident of the
    profile, it is a fact the safety layer has to live with:

    * ``FAULT_REACTION_RAMP_STOP`` and ``FAULT_REACTION_FREEWHEEL`` emit the
      **same** status word and both decode to ``DriveState.FAULT``. One is
      still driving the motor down a ramp; the other has dropped the output
      stage and left a loaded centrifuge coasting for minutes. No consumer of
      ETA can tell them apart, which is exactly why nothing may infer motion
      from a state and everything must read ``output_rpm``.
    * ``NOT_READY_TO_SWITCH_ON`` and CiA402 quick-stop-active both decode to
      ``DriveState.NOT_READY``, a value that reads as "stopped" to anything
      that does not know better. Quick stop is not modelled (see
      :data:`LEGAL_TRANSITIONS`).
    * ``DISABLING_ON_RAMP`` emits the **same** word as ``SWITCHED_ON``, so a
      machine decelerating from 1380 rpm and a machine sitting still with its
      output stage off are indistinguishable through ETA. That is the whole
      reason nothing may infer motion from a state.
    """

    NOT_READY_TO_SWITCH_ON = auto()
    # The drive is powering up and running its self-test. Rejects every
    # command. Reachable as a starting condition so the "we opened the link
    # before the drive finished booting" path can be tested.

    SWITCH_ON_DISABLED = auto()
    # Self-test done, output stage off, awaiting the shutdown command. This is
    # where a healthy powered drive rests, so it is the default start state.

    READY_TO_SWITCH_ON = auto()
    SWITCHED_ON = auto()
    # One command word away from torque.

    OPERATION_ENABLED = auto()
    # Commanded, and producing torque. See _energised.

    DISABLING_ON_RAMP = auto()
    # CiA402 transition 5 in progress: the run command has been removed with
    # command word 7 and this drive is commissioned to RAMP rather than to
    # coast, so the output stage is STILL DRIVING - down to zero. It reports
    # itself as SWITCHED_ON throughout, which is the profile's own answer and
    # the reason a stop must be verified against RFRD and never against ETA.
    #
    # Modelling this at all is what makes command word 7 distinguishable from
    # command word 6. Before it existed, torque lived in OPERATION_ENABLED
    # alone, both words dropped straight into a coast, and the difference
    # between a 10 s ramp-stop and a 145 s freewheel was invisible to every
    # test in the suite.

    FAULT_REACTION_RAMP_STOP = auto()
    # A fault whose commissioned reaction is a controlled deceleration (ttO ->
    # SLF is the one that matters here). Still driving the motor.

    FAULT_REACTION_FREEWHEEL = auto()
    # A fault that removed torque instantly - the mains went, a phase opened,
    # the bus tripped ObF. The centrifuge is coasting and nothing is
    # controlling it.

    FAULT = auto()
    # Fault latched and the shaft has come to rest. Only a fault reset leaves.


#: Bits 9 and 10, which the ATV320 sets in ETA and the CiA402 profile does not
#: define. The simulator sets them on every word it emits, so a consumer that
#: forgot to mask the high bits fails in CI instead of on the bench. These are
#: the same 0x06xx words ``tests/test_drive_contract.py`` calls realistic.
ATV320_UNDEFINED_BITS: Final[int] = 0x0600

ETA_WORDS: Final[Mapping[SimState, StatusWord]] = MappingProxyType(
    {
        # bits, low byte: 0 ready-to-switch-on, 1 switched-on, 2 operation-
        # enabled, 3 fault, 4 voltage-enabled, 5 quick-stop-inactive,
        # 6 switch-on-disabled.
        SimState.NOT_READY_TO_SWITCH_ON: StatusWord(ATV320_UNDEFINED_BITS | 0x00),
        SimState.SWITCH_ON_DISABLED: StatusWord(ATV320_UNDEFINED_BITS | 0x40),
        SimState.READY_TO_SWITCH_ON: StatusWord(ATV320_UNDEFINED_BITS | 0x31),
        SimState.SWITCHED_ON: StatusWord(ATV320_UNDEFINED_BITS | 0x33),
        SimState.OPERATION_ENABLED: StatusWord(ATV320_UNDEFINED_BITS | 0x37),
        # Deliberately the SWITCHED_ON word: CiA402 transition 5 lands in
        # Switched On, and the profile offers no bit for "still ramping". A
        # caller polling ETA therefore sees the reassuring "switched on" while
        # the centrifuge is at hundreds of rpm - faithful, and the pessimistic
        # reading for anything written against this model.
        SimState.DISABLING_ON_RAMP: StatusWord(ATV320_UNDEFINED_BITS | 0x33),
        # 0x3F is the profile's fault-reaction-active pattern (& 0x4F == 0x0F).
        # Both reactions emit it: the word cannot distinguish them.
        SimState.FAULT_REACTION_RAMP_STOP: StatusWord(ATV320_UNDEFINED_BITS | 0x3F),
        SimState.FAULT_REACTION_FREEWHEEL: StatusWord(ATV320_UNDEFINED_BITS | 0x3F),
        SimState.FAULT: StatusWord(ATV320_UNDEFINED_BITS | 0x38),
    }
)
"""The ETA word the drive reports in each state. Total over ``SimState``.

The simulator never invents a :class:`~src.motor.drive.DriveState`: it emits a
word and lets :func:`~src.motor.drive.decode_status_word` decode it, so the
decoding used in tests is the decoding used on the hardware path.
"""

#: The two states in which a fault is latched and the shaft is still turning.
REACTION_STATES: Final[frozenset[SimState]] = frozenset(
    {SimState.FAULT_REACTION_RAMP_STOP, SimState.FAULT_REACTION_FREEWHEEL}
)

#: Every state in which the drive is signalling a fault.
FAULT_STATES: Final[frozenset[SimState]] = REACTION_STATES | {SimState.FAULT}

SETTLES_INTO: Final[Mapping[SimState, SimState]] = MappingProxyType(
    {
        SimState.FAULT_REACTION_RAMP_STOP: SimState.FAULT,
        SimState.FAULT_REACTION_FREEWHEEL: SimState.FAULT,
        SimState.DISABLING_ON_RAMP: SimState.SWITCHED_ON,
    }
)
"""States that are transients, and the state each becomes once the shaft stops.

A transient here is a state whose *reason to exist* is that the machine is still
moving. Nothing leaves one on a timer: they end when the speed reaches zero and
not a moment before, which is the only definition that cannot be satisfied by a
machine that is still turning.
"""


LEGAL_TRANSITIONS: Final[Mapping[tuple[SimState, ControlWord], SimState]] = MappingProxyType(
    {
        # --- SHUTDOWN (6): CiA402 transitions 2, 6 and 8 ------------------
        (SimState.SWITCH_ON_DISABLED, ControlWord.SHUTDOWN): SimState.READY_TO_SWITCH_ON,
        (SimState.READY_TO_SWITCH_ON, ControlWord.SHUTDOWN): SimState.READY_TO_SWITCH_ON,
        (SimState.SWITCHED_ON, ControlWord.SHUTDOWN): SimState.READY_TO_SWITCH_ON,
        # TRANSITION 8, and the reason this table is worth reading twice. Out of
        # OPERATION_ENABLED, word 6 DROPS THE OUTPUT STAGE. The centrifuge does
        # not stop, it coasts - for minutes - while the drive reports the
        # reassuring READY. This entry is not a convenience; it is the hazard,
        # modelled so that software which stops with word 6 fails here.
        (SimState.OPERATION_ENABLED, ControlWord.SHUTDOWN): SimState.READY_TO_SWITCH_ON,
        # Word 6 during a ramp-stop abandons the ramp the same way.
        (SimState.DISABLING_ON_RAMP, ControlWord.SHUTDOWN): SimState.READY_TO_SWITCH_ON,
        # --- SWITCH_ON (7): transition 3, and transition 5 (disable op) ----
        (SimState.READY_TO_SWITCH_ON, ControlWord.SWITCH_ON): SimState.SWITCHED_ON,
        (SimState.SWITCHED_ON, ControlWord.SWITCH_ON): SimState.SWITCHED_ON,
        # TRANSITION 5. This drive is commissioned to ramp, so it keeps control
        # of the motor all the way down instead of letting go of it. THIS is the
        # stop, and the difference from the line above is two orders of
        # magnitude of coast-down.
        (SimState.OPERATION_ENABLED, ControlWord.SWITCH_ON): SimState.DISABLING_ON_RAMP,
        (SimState.DISABLING_ON_RAMP, ControlWord.SWITCH_ON): SimState.DISABLING_ON_RAMP,
        # --- ENABLE_OPERATION (15): transition 4, from SWITCHED_ON ONLY ----
        (SimState.SWITCHED_ON, ControlWord.ENABLE_OPERATION): SimState.OPERATION_ENABLED,
        # Re-enabling mid-ramp is legal: the drive never stopped driving, so it
        # simply goes back to tracking the reference. Accepted rather than
        # refused because a drive that refused it would make a caller that
        # changed its mind look like a caller with a sequencing bug.
        (SimState.DISABLING_ON_RAMP, ControlWord.ENABLE_OPERATION): SimState.OPERATION_ENABLED,
        # Re-issuing 15 while enabled is the keepalive the ttO watchdog is fed
        # with, so it MUST stay legal.
        (SimState.OPERATION_ENABLED, ControlWord.ENABLE_OPERATION): SimState.OPERATION_ENABLED,
        # --- FAULT_RESET (128): transition 15, from a settled fault only ---
        (SimState.FAULT, ControlWord.FAULT_RESET): SimState.SWITCH_ON_DISABLED,
    }
)
"""Every command word this drive accepts, per state. Anything absent is refused.

Three choices in here are stricter than an ATV320 might be, and all three are
strict in the safe direction - software that works against this table works
against a more permissive drive, never the reverse:

* **6 -> 15 is refused.** Some drives collapse transitions 3 and 4 and start on
  a bare 15 from READY_TO_SWITCH_ON. The commissioning notes give the start
  sequence as 6 -> 7 -> 15, so that is what is accepted, and a caller that
  skips a step fails in CI rather than working here and stalling on the bench.
* **A command in NOT_READY_TO_SWITCH_ON is refused**, including SHUTDOWN. A
  drive that has not finished its self-test has not agreed to anything yet.
* **Nothing is accepted while a fault is latched except a reset of a settled
  fault** (see :meth:`SimulatedDrive.write_command`).

Quick stop (control word 2) is absent because :class:`ControlWord` has no
member for it, so this drive cannot be commanded into quick-stop-active. If the
design ever adds one, ``DriveState`` needs a member for it first: today it
would decode to ``NOT_READY`` with the motor still turning.
"""


COMMAND_REQUIRES: Final[Mapping[ControlWord, DriveState]] = MappingProxyType(
    {
        ControlWord.SHUTDOWN: DriveState.SWITCH_ON_DISABLED,
        ControlWord.SWITCH_ON: DriveState.READY,
        ControlWord.ENABLE_OPERATION: DriveState.SWITCHED_ON,
        ControlWord.FAULT_RESET: DriveState.FAULT,
    }
)
"""The state each command word is issued *from*, for the rejection message.

``UnexpectedState`` carries both states because the pair is the diagnosis:
"expected SWITCHED_ON, actual READY" names the missing step of a start
sequence, which a bare "rejected" does not.
"""


# =========================================================================
# How a fault stops the machine
# =========================================================================


@unique
class FaultReaction(Enum):
    """What the drive does to the shaft when it latches a fault.

    The difference is minutes of coast-down with somebody inside, so it is
    modelled explicitly rather than assumed to be "it stops".
    """

    RAMP_TO_STOP = auto()
    # The drive keeps control and decelerates down its ramp. This is what a
    # Modbus timeout (ttO -> SLF) is commissioned to do, and it is the only
    # reason going silent is survivable.

    FREEWHEEL = auto()
    # Torque removed instantly; the load coasts. The honest answer whenever the
    # drive cannot control the motor any more, and the conservative default.


FAULT_REACTIONS: Final[Mapping[DriveFault, FaultReaction]] = MappingProxyType(
    {
        # Nothing here can be ramped: the bus, a phase or the output stage is
        # gone, so the drive has no way to influence the shaft.
        DriveFault.UNDERVOLTAGE: FaultReaction.FREEWHEEL,
        DriveFault.MOTOR_PHASE_LOSS: FaultReaction.FREEWHEEL,
        DriveFault.OVERCURRENT: FaultReaction.FREEWHEEL,
        DriveFault.OUTPUT_SHORT: FaultReaction.FREEWHEEL,
        DriveFault.NO_MOTOR: FaultReaction.FREEWHEEL,
        DriveFault.INTERNAL: FaultReaction.FREEWHEEL,
        # ObF is freewheel by definition: the bus tripped *because* it could
        # not absorb the braking energy, so braking is what stops.
        DriveFault.DC_BUS_OVERVOLTAGE: FaultReaction.FREEWHEEL,
        # The ATV320's factory setting for a motor overload is a freewheel
        # stop, and a drive that thinks the motor is too hot is the last thing
        # that should keep pushing current through it.
        DriveFault.MOTOR_OVERLOAD: FaultReaction.FREEWHEEL,
        # The one that must be a ramp. SLF fires when this software goes
        # silent, and a freewheel there would mean a silent controller leaves a
        # spinning centrifuge uncontrolled. The drive is healthy, so it can and
        # must ramp it down. This assumes the drive is commissioned that way;
        # verify the Modbus-fault-management setting on the bench.
        DriveFault.MODBUS_COMM_LOSS: FaultReaction.RAMP_TO_STOP,
        # Neither of these is a real fault reaction: they are what a fault
        # *report* degrades to when LFT holds 0 or an unrecognised code. If one
        # ever reaches the plant, freewheel is the conservative reading.
        DriveFault.NO_FAULT_STORED: FaultReaction.FREEWHEEL,
        DriveFault.UNKNOWN: FaultReaction.FREEWHEEL,
    }
)
"""Reaction per fault. Total over ``DriveFault``; a test proves it."""


UNMAPPED_FAULT_CODE: Final[RawRegister] = RawRegister(251)
"""The LFT value reported for a fault the code table cannot name.

Deliberately absent from :data:`~src.motor.drive.LFT_FAULT_CODES`, so injecting
a fault that has no number in that (provisional) table produces exactly what
the hardware path would produce for an unrecognised code: ``DriveFault.UNKNOWN``
carrying the raw value. That path has to be exercisable, because the table is
known to be incomplete and the safety layer meets it in production.
"""


def lft_code_for(
    fault: DriveFault,
    codes: Mapping[RawRegister, DriveFault] = LFT_FAULT_CODES,
) -> RawRegister:
    """The LFT register value this drive shows for ``fault``.

    Inverted from the shared table rather than duplicated, so a correction made
    during commissioning reaches the simulator too; a second hardcoded copy of
    a table already marked PROVISIONAL is how a simulator starts testing a
    fiction. Linear scan over nine entries, at injection time only.
    """
    for code, mapped in codes.items():
        if mapped is fault:
            return code
    return UNMAPPED_FAULT_CODE


# =========================================================================
# Configuration
# =========================================================================


@dataclass(frozen=True, slots=True)
class SimulatedDriveConfig:
    """The plant's numbers, all in one frozen record.

    Every default is either from the commissioning notes (the nameplate speed)
    or is a placeholder marked as such. The ones that are guesses are knobs
    precisely so that measuring them on the bench is a config change and not a
    code change.
    """

    nominal_rpm: MotorRpm = MotorRpm(1380)
    """SEW KA37 DRS71S4 nameplate: 1380 rpm at 50 Hz. Sets the ramp rate and
    normalises the current model."""

    max_rpm: MotorRpm = MotorRpm(1380)
    """The drive's HSP ceiling, as a speed. The shaft is clamped to +/- this
    however large a setpoint is written, which is what HSP does."""

    acceleration_time: Seconds = Seconds(10.0)
    """Drive ACC: seconds from 0 to ``nominal_rpm``. 1380 / 10 = 138 rpm/s.

    One rate is used for accelerating and decelerating, where the drive has
    separate ACC and DEC. DEC was commissioned at 3-4 s, so a modelled
    deceleration is ~3x SLOWER than the real one - the conservative error."""

    tau_coast: Seconds = Seconds(20.0)
    """Freewheel time constant of the loaded centrifuge. A placeholder: the
    real value is a coast-down measurement nobody has taken yet."""

    tto: Seconds = Seconds(3.0)
    """The drive's Modbus timeout. Silence for this long latches SLF."""

    response_timeout: Seconds = Seconds(0.5)
    """How long a caller is modelled to wait for a reply before calling it a
    timeout. A placeholder: a Modbus exchange at 19200 8E1 takes single-digit
    milliseconds, so anything near this is already pathological."""

    power_up_time: Seconds = Seconds(1.0)
    """How long NOT_READY_TO_SWITCH_ON lasts. A placeholder, and nothing safety
    relevant may depend on it: the rule is to read the drive's state, never to
    wait a fixed time and assume."""

    standstill_rpm: MotorRpm = MotorRpm(1)
    """Below this the shaft is treated as stopped. 1 rpm is also the finest
    speed the drive can report, so nothing observable is discarded - and an
    exponential coast never reaches zero without it."""

    no_load_current: Amperes = Amperes(0.9)
    """Magnetising current: what the motor draws turning nothing."""

    load_current_at_nominal: Amperes = Amperes(1.0)
    """Additional current at ``nominal_rpm``, so ~1.9 A total against a 2.15 A
    nameplate."""

    amps_per_rpm_per_second: float = 0.004
    """Extra current per rpm/s of acceleration: torque costs current. At 138
    rpm/s that is 0.55 A on top."""

    current_limit: Amperes = Amperes(4.0)
    """Where the drive's own current limit clips. ~170% of the ATV320U04M2C's
    ~2.3 A rating; a placeholder, since the notes do not give the figure."""

    def __post_init__(self) -> None:
        # Only the knobs that break the arithmetic are checked, and they raise
        # rather than returning a Result: this is built at startup with nothing
        # spinning, and refusing to start is the right answer to a plant model
        # that cannot be integrated. Same boundary RegisterMap draws.
        for name, value in (
            ("nominal_rpm", float(self.nominal_rpm)),
            ("max_rpm", float(self.max_rpm)),
            ("acceleration_time", float(self.acceleration_time)),
            ("tau_coast", float(self.tau_coast)),
            ("tto", float(self.tto)),
        ):
            if value <= 0.0:
                raise ValueError(f"{name} must be positive, got {value}")


DEFAULT_SIM_CONFIG: Final[SimulatedDriveConfig] = SimulatedDriveConfig()
"""Shared default. A module-level instance, not a call in a default argument."""


# =========================================================================
# The simulator
# =========================================================================


@final
class SimulatedDrive:
    """A drive-shaped plant that implements :class:`~src.motor.drive.DriveBackend`.

    **Mutable by design**, which the contract treats as the exception: this
    object *is* a physical machine's state, and a frozen one could not spin up.
    Every field is private and every mutation happens in one of the methods
    below, so the state a caller sees only ever comes from
    :meth:`read_status` - the same seam the hardware path answers through.

    The plant advances only when time is handed to it: :meth:`advance` takes an
    explicit ``now``, and the protocol methods catch up from the injected
    clock. Two idioms, pick one per test:

        clock = ManualClock()
        sim = SimulatedDrive(clock)
        sim.advance(clock.advance(Seconds(0.2)))    # step the clock, then the plant

    Mixing them is safe but pointless: integrating to a ``now`` ahead of the
    clock just means the next clock-driven call finds nothing to do.
    """

    __slots__ = (
        "_clock",
        "_comms_established",
        "_comms_lost_until",
        "_config",
        "_fault",
        "_fault_codes",
        "_last_at",
        "_last_frame_at",
        "_latency",
        "_link_open",
        "_misaddressed",
        "_powered_for",
        "_rate",
        "_reversed",
        "_rpm",
        "_setpoint",
        "_state",
    )

    def __init__(
        self,
        clock: Clock,
        config: SimulatedDriveConfig = DEFAULT_SIM_CONFIG,
        *,
        initial_state: SimState = SimState.SWITCH_ON_DISABLED,
        initial_rpm: MotorRpm = MotorRpm(0),
        fault_codes: Mapping[RawRegister, DriveFault] = LFT_FAULT_CODES,
    ) -> None:
        """Build a drive that is already powered and resting, unless told otherwise.

        ``initial_state`` and ``initial_rpm`` exist for one scenario in
        particular: a drive found in OPERATION_ENABLED at 900 rpm because a
        previous process died with the motor running. The contract requires
        that case to be handled (command zero, disable, latch, demand an
        operator acknowledgement) and it cannot be tested without being able to
        start a simulator there.

        A fault state is refused as a starting point: a latched fault has to
        carry an LFT code, so it is set up with :meth:`inject_fault` - which
        works before the link is even open.
        """
        if initial_state in FAULT_STATES:
            raise ValueError(
                f"{initial_state.name} is not a valid starting state: a latched fault "
                "must carry an LFT code, so use inject_fault() instead"
            )

        self._clock: Clock = clock
        self._config: SimulatedDriveConfig = config
        self._fault_codes: Mapping[RawRegister, DriveFault] = fault_codes

        # --- plant state ---
        self._state: SimState = initial_state
        self._rpm: float = float(initial_rpm)
        """Shaft speed, signed, at the MOTOR shaft. Held as a float because a
        ramp and an exponential coast are continuous; quantised to whole rpm
        only where the drive would report it, in read_status."""
        self._rate: float = 0.0
        """The last step's d(rpm)/dt, for the acceleration term of the current
        model."""
        self._setpoint: MotorRpm = MotorRpm(0)
        """LFRD as the drive holds it: what was last written, NOT what the
        shaft is doing and NOT what the caller believes it wrote."""
        self._fault: FaultReport | None = None
        """Non-None exactly when ``_state in FAULT_STATES``."""
        self._powered_for: Seconds = Seconds(0.0)

        # --- time base ---
        self._last_at: Monotonic = clock.monotonic()
        self._last_frame_at: Monotonic = self._last_at

        # --- link / injected conditions ---
        self._link_open: bool = False
        self._comms_established: bool = False
        self._comms_lost_until: Monotonic | None = None
        self._latency: Seconds = Seconds(0.0)
        self._misaddressed: bool = False
        self._reversed: bool = False

    # -- observation -------------------------------------------------------

    @property
    def sim_state(self) -> SimState:
        """The drive's own CiA402 state, finer than what ETA can express.

        For tests and simulation logs. A caller on the drive path must not use
        it: the hardware backend cannot offer it, and code that reads it stops
        being swappable.
        """
        return self._state

    @property
    def commanded_setpoint(self) -> MotorRpm:
        """LFRD as the drive holds it, readable with the link down.

        Same caveat as :attr:`sim_state`: for tests and simulation logs only. A
        caller on the drive path must read ``DriveStatus.setpoint_echo_rpm``,
        which is what the hardware can actually answer.

        It exists because the interesting question about a failed emergency stop
        - "did the reference actually change?" - has to be answerable precisely
        when the link is broken, which is the one time ``read_status`` cannot
        answer anything.
        """
        return self._setpoint

    @property
    def ramp_rate(self) -> RpmPerSecond:
        """``nominal_rpm / acceleration_time``: 138 rpm/s by default."""
        return RpmPerSecond(self._config.nominal_rpm / self._config.acceleration_time)

    # -- simulation control -----------------------------------------------

    def advance(self, now: Monotonic) -> None:
        """Integrate the plant up to ``now``.

        Raises ``ValueError`` if ``now`` is behind the plant, matching
        ``ManualClock.advance``: a monotonic reading cannot go backwards, so a
        backwards one means two different clocks or a wall-clock value got
        mixed in, and absorbing that silently as "no time passed" would hide
        the bug. This is the simulation harness, not the drive path - the
        ``async`` methods below cannot raise, whatever the clock does.
        """
        if now < self._last_at:
            raise ValueError(f"cannot advance the plant backwards, from {self._last_at} to {now}")
        self._integrate(now)

    # -- injection ---------------------------------------------------------
    #
    # Every injector brings the plant up to date from the clock first, so an
    # injected condition starts exactly when the test says it does rather than
    # retroactively applying to the step before it.

    def inject_fault(self, fault: DriveFault) -> None:
        """Latch ``fault`` now, with the LFT code the real drive would show.

        Works with the link closed, which is how "the drive was already in
        fault when we connected" gets tested.
        """
        self._integrate(self._clock.monotonic())
        self._latch_fault(fault)

    def inject_comms_loss(self, duration: Seconds) -> None:
        """The drive stops answering for ``duration``.

        Every operation returns ``Err(CommTimeout)`` while the window is open,
        and - the point of the whole thing - the drive hears nothing either, so
        its own ttO watchdog runs. Lose comms for longer than ``tto`` and the
        machine comes back reporting SLF with the motor ramped down: the
        "software went silent" path, end to end, with no hardware.
        """
        self._integrate(self._clock.monotonic())
        self._comms_lost_until = Monotonic(self._last_at + duration)

    def inject_latency(self, latency: Seconds) -> None:
        """Make every exchange take ``latency``.

        Modelled as a budget, not as elapsed time: the plant's clock is
        injected and this module must not move it. So a latency at or under
        ``response_timeout`` is invisible, and one above it produces
        ``Err(CommTimeout(after=latency))`` - which is the only part a caller
        can act on anyway. Frames that time out never reach the drive, so they
        do not feed its watchdog.
        """
        self._integrate(self._clock.monotonic())
        self._latency = latency

    def inject_register_offset_error(self) -> None:
        """Address every write one register off, as a wrong ``RegisterMap.offset`` does.

        This is the silent one. A misaddressed Modbus write is not a failed
        write: the drive acknowledges it and stores the value in whatever
        parameter is next door, so the call returns ``Ok``, the frame feeds the
        drive's ttO watchdog, and the speed reference never moves. The only
        evidence is ``DriveStatus.setpoint_echo_rpm`` not following what was
        written - which is why the seam reports the echo at all.

        Reads are left alone deliberately. A wrong offset does corrupt them
        too, but that half announces itself (a status word read from a
        neighbouring parameter does not decode, which is why ``drive.py``
        insists the offset is calibrated with reads only), and modelling it
        would mean inventing register values - a fabricated speed next to a
        spinning shaft is the one lie this simulator must never tell.
        """
        self._integrate(self._clock.monotonic())
        self._misaddressed = True

    def inject_reverse(self) -> None:
        """Swap the output phase order: a positive setpoint turns the shaft negative.

        Not a curiosity. LFRD and RFRD are signed, so a clamp written as
        ``rpm <= max_rpm`` instead of ``abs(rpm) <= max_rpm`` passes a
        centrifuge running backwards at full speed, and the echo still reads
        positive. This makes that bug fail in CI.
        """
        self._integrate(self._clock.monotonic())
        self._reversed = True

    # -- DriveBackend ------------------------------------------------------

    async def open(self) -> Result[None, DriveError]:
        """Open the link and read the drive. Does not enable it or command a speed.

        The port opens locally even when nothing answers, so a failure here is
        the first exchange failing, not the port. A failed open leaves the link
        open and comms unestablished, so the drive's ttO watchdog does not
        start counting against a drive that has never heard from us.
        """
        self._integrate(self._clock.monotonic())
        self._link_open = True
        error = self._wire_check()
        if error is not None:
            return Err(error)
        self._note_frame(is_write=False)
        return Ok(None)

    async def close(self) -> Result[None, DriveError]:
        """Drop the link. Does **not** stop the motor.

        Modelled as the bare minimum a close can be, because that is the
        pessimistic case: the drive is left exactly as it was, still enabled if
        it was enabled, and the only thing that stops it is its own ttO timeout
        ``tto`` seconds later. A real driver may write a stop word first; a
        safety layer that only works because it does is a safety layer that
        breaks the day somebody yanks the cable instead.

        Idempotent: atexit paths close twice.
        """
        self._integrate(self._clock.monotonic())
        self._link_open = False
        return Ok(None)

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        """Write CMD, enforcing the CiA402 sequence.

        Refusals, in the order they are checked:

        * transport first - a command that never reached the drive says nothing
          about the state machine;
        * a misaddressed write is **acknowledged and discarded**, exactly as
          the hardware would;
        * while a fault is latched, only a reset of a *settled* fault is
          accepted. Everything else, including a reset issued while the
          machine is still coasting, returns ``DriveFaulted``: you cannot reset
          your way out of a spinning centrifuge;
        * anything not in :data:`LEGAL_TRANSITIONS` returns ``UnexpectedState``
          naming the state the word needed.

        The two stop words are **not** interchangeable here, and a caller that
        treats them as such gets a different machine: word 7 out of
        OPERATION_ENABLED keeps driving the shaft down its ramp, word 6 drops
        the output stage and leaves it coasting. See :data:`LEGAL_TRANSITIONS`.
        """
        self._integrate(self._clock.monotonic())
        error = self._transport_check()
        if error is not None:
            return Err(error)
        self._note_frame(is_write=True)
        if self._misaddressed:
            return Ok(None)

        latched = self._fault
        if latched is not None and (
            word is not ControlWord.FAULT_RESET or self._state is not SimState.FAULT
        ):
            return Err(DriveFaulted(fault=latched.fault, raw_code=latched.raw_code))

        target = LEGAL_TRANSITIONS.get((self._state, word))
        if target is None:
            return Err(UnexpectedState(expected=COMMAND_REQUIRES[word], actual=self._drive_state()))
        self._state = target
        # A ramp-stop of a shaft that is already at rest is over before it
        # starts, so word 7 at standstill lands in SWITCHED_ON directly rather
        # than parking in a transient no time will ever end.
        self._settle_standstill()
        if word is ControlWord.FAULT_RESET:
            # Reached only when the reset was legal. LFT physically keeps the
            # last fault, but a status only reports one while the status word
            # says fault, so clearing it here keeps the "_fault is non-None
            # exactly in a fault state" invariant tight instead of modelling a
            # register nothing can observe.
            self._fault = None
        return Ok(None)

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        """Write LFRD: a signed setpoint in MOTOR-shaft rpm.

        Encoding is checked before anything is sent, through the same
        ``src.units`` validator the hardware path uses, so a setpoint outside
        signed 16 bits is refused rather than wrapping into a reverse speed.

        A setpoint beyond ``max_rpm`` is **accepted and echoed**, while the
        shaft is clamped - which is what the drive's HSP ceiling does. The
        echo therefore shows what was asked for and ``output_rpm`` shows what
        was delivered, and a controller demanding 9000 rpm is visible instead
        of being quietly tidied away.

        Accepted while a fault is latched: writing a reference to a faulted
        drive is harmless, it just will not be followed.
        """
        self._integrate(self._clock.monotonic())
        encoded = signed_to_register(rpm)
        if is_err(encoded):
            refused: DriveError = encoded.error
            return Err(refused)
        error = self._transport_check()
        if error is not None:
            return Err(error)
        self._note_frame(is_write=True)
        if self._misaddressed:
            return Ok(None)
        self._setpoint = rpm
        return Ok(None)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        """One consistent observation: ETA, the LFRD echo, RFRD, LCR and LFT.

        Built from the plant's state at this instant, not assembled from parts
        of different instants, and stamped by nobody: a ``DriveStatus`` carries
        no time, so whoever reads it owns the question of whether it is fresh.

        When the link is down this returns ``Err``. It never returns a status
        with ``DriveState.COMM_LOST``, and never a fabricated 0 rpm: a caller
        that cannot hear the drive has the last status and the knowledge that
        it is old, which is the truth.

        A read does not feed the drive's ttO watchdog - see
        :meth:`_note_frame`.
        """
        self._integrate(self._clock.monotonic())
        error = self._transport_check()
        if error is not None:
            return Err(error)
        self._note_frame(is_write=False)

        word = ETA_WORDS[self._state]
        latched = self._fault
        return Ok(
            DriveStatus(
                state=decode_status_word(word),
                status_word=word,
                setpoint_echo_rpm=self._setpoint,
                # Whole rpm, because that is the resolution RFRD has. Sub-rpm
                # plant state is real but not observable, here or on copper.
                output_rpm=MotorRpm(round(self._rpm)),
                current=self._current(),
                fault=None if latched is None else latched.fault,
            )
        )

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        """Zero the setpoint, synchronously, best effort. Never raises.

        **The run command is left in place, exactly as the seam requires.** A
        zeroed reference leaves this drive in OPERATION_ENABLED, driving the
        shaft down its own ramp; removing the run command with word 6 would
        drop the output stage and leave 420 J of rotating mass coasting on
        ``tau_coast`` instead - two orders of magnitude slower. So the fastest
        stop available here is the one that keeps the drive in control, and the
        keepalive stopping is what arms ``ttO`` to finish the job.

        Nothing may read a return from this call as "the machine has stopped":
        the ramp still takes seconds, and the outcome describes the *attempt*.

        The attempt can fail silently, and that is modelled rather than
        assumed: with the link closed, during injected comms loss, with latency
        over ``timeout``, or with a register offset error, this returns having
        changed nothing at all. The offset case is the nasty one - it reports
        ``ACKNOWLEDGED``, because that is what the drive really does with a
        write into the wrong parameter, and no caller can tell the difference.
        An emergency stop that is acked and does nothing is the failure mode
        worth having a test for.
        """
        self._integrate(self._clock.monotonic())
        if not self._link_open:
            # No port, so no frame: nothing was asked of the drive at all.
            return EmergencyStopOutcome.NOTHING_SENT
        if self._latency > timeout or self._wire_check() is not None:
            # The frame goes out and the answer does not come back inside the
            # budget. It may have landed; this model cannot say, and neither
            # could the real driver.
            return EmergencyStopOutcome.SENT_UNCONFIRMED
        self._note_frame(is_write=True)
        if self._misaddressed:
            return EmergencyStopOutcome.ACKNOWLEDGED
        self._setpoint = MotorRpm(0)
        return EmergencyStopOutcome.ACKNOWLEDGED

    # -- transport ---------------------------------------------------------

    def _wire_check(self) -> DriveError | None:
        """Whether the drive would answer at all."""
        if self._comms_lost_until is not None:
            return CommTimeout(after=self._config.response_timeout)
        if self._latency > self._config.response_timeout:
            return CommTimeout(after=self._latency)
        return None

    def _transport_check(self) -> DriveError | None:
        """``_wire_check`` plus the link itself."""
        if not self._link_open:
            return BadResponse(detail="the Modbus link is not open")
        return self._wire_check()

    def _note_frame(self, *, is_write: bool) -> None:
        """Record that a frame reached the drive.

        Only writes reset the ttO watchdog here. A real ATV320 resets its
        timeout on any frame it receives, reads included, so this model trips
        sooner than the hardware - which makes software written against it keep
        a *write* keepalive alive, and that is correct under either reading.
        The reverse assumption would let a loop that only polls look healthy in
        CI and lose the motor on the bench.

        The first frame of any kind establishes communication and starts the
        watchdog, because a drive that has never heard from a master does not
        raise SLF - otherwise every ATV320 on a shelf would show one.
        """
        if is_write or not self._comms_established:
            self._last_frame_at = self._last_at
        self._comms_established = True

    # -- the plant ---------------------------------------------------------

    def _integrate(self, now: Monotonic) -> None:
        """Advance every modelled process to ``now``. Cannot raise.

        A non-increasing ``now`` is a no-op rather than an error: this runs
        inside the ``async`` methods, where the contract forbids raising, and
        where the only way to get one is a caller that already stepped the
        plant past its clock.

        One explicit first-order step over the whole ``dt``, so accuracy
        depends on the step size; the control loop runs at 5 Hz, so tests
        should step in 0.2 s or less. The fault-latch check is deliberately
        **last**: a fault detected during this step takes effect from the next
        one, so the shaft keeps whatever behaviour it had for the step in which
        the threshold was crossed. That errs towards the motor being driven
        slightly longer, which is the pessimistic direction for both reactions.
        """
        dt = elapsed(self._last_at, now)
        if dt <= 0.0:
            return
        self._last_at = now
        self._expire_comms_loss(now)
        self._complete_power_up(dt)
        self._step_shaft(dt)
        self._settle_standstill()
        self._check_watchdog()

    def _expire_comms_loss(self, now: Monotonic) -> None:
        """End an injected comms-loss window once its time is up."""
        until = self._comms_lost_until
        if until is not None and now >= until:
            self._comms_lost_until = None

    def _complete_power_up(self, dt: Seconds) -> None:
        """Finish the drive's self-test, CiA402 transition 1.

        Accumulated rather than measured from an origin, so it works whether
        the plant is stepped by a clock or by explicit ``advance`` calls.
        """
        if self._state is not SimState.NOT_READY_TO_SWITCH_ON:
            return
        self._powered_for = Seconds(self._powered_for + dt)
        if self._powered_for >= self._config.power_up_time:
            self._state = SimState.SWITCH_ON_DISABLED

    def _energised(self) -> bool:
        """Whether the output stage is delivering torque.

        Three states: commanded operation, a fault reaction that is still a
        controlled ramp, and a commanded ramp-stop (CiA402 transition 5, which
        this drive is commissioned to ramp rather than coast). Everywhere else -
        ready, switched on, faulted, freewheeling, powering up - there is no
        torque and the load coasts.
        """
        return self._state in (
            SimState.OPERATION_ENABLED,
            SimState.FAULT_REACTION_RAMP_STOP,
            SimState.DISABLING_ON_RAMP,
        )

    def _shaft_target(self) -> float:
        """The speed the drive is currently driving towards, in motor rpm.

        Zero while stopping - a fault reaction or a commanded ramp-stop - since
        the drive is bringing the shaft down, not tracking a reference.
        Otherwise the written setpoint, reversed if the phase order is wrong,
        clamped to the HSP ceiling.
        """
        if self._state in (SimState.FAULT_REACTION_RAMP_STOP, SimState.DISABLING_ON_RAMP):
            return 0.0
        demand = float(-self._setpoint if self._reversed else self._setpoint)
        limit = float(self._config.max_rpm)
        return max(-limit, min(limit, demand))

    def _step_shaft(self, dt: Seconds) -> None:
        """Move the shaft: rate-limited under torque, exponential coast without.

        The rate limiter is the drive's ACC/DEC ramp. The coast is
        ``rpm *= exp(-dt / tau_coast)``, which is the visible form of the
        hazard: a centrifuge that has lost torque at full speed is still
        turning minutes later, and no state word says so.

        **The standstill floor belongs to the coast and to the coast only.** An
        exponential decay never reaches zero, so without it a freewheel would
        report "0.4 rpm" for ever and a fault reaction would never settle. The
        ramp needs no such help - it assigns the target exactly once the gap is
        within one step, so a ramp to zero ends at a true ``0.0`` - and
        applying the floor there was a real defect rather than a tidy-up: it
        let the shaft jump up to ``standstill_rpm`` in one step, which is more
        than ``ramp_rate * dt`` allows and therefore broke the one bound the
        safety layer is entitled to assume. Found by
        ``tests/test_simulated_drive.py`` under a raised hypothesis budget;
        there is a regression test pinning the exact case.
        """
        previous = self._rpm
        if self._energised():
            target = self._shaft_target()
            step = self.ramp_rate * dt
            gap = target - previous
            self._rpm = target if abs(gap) <= step else previous + math.copysign(step, gap)
        else:
            self._rpm = previous * math.exp(-dt / self._config.tau_coast)
            if self._at_standstill():
                # "1 rpm forever" is not a truer answer than "stopped", it is
                # the same lie with extra steps. This is also the finest speed
                # the drive can report.
                self._rpm = 0.0
        self._rate = (self._rpm - previous) / dt

    def _at_standstill(self) -> bool:
        """Whether the shaft is stopped as far as this machine can tell.

        One predicate, used by the coast floor, by the transient settling and by
        the fault latch, because "has it stopped?" must not have three answers.
        A threshold rather than ``== 0.0``: ``standstill_rpm`` is the finest
        speed the drive can report, so below it there is nothing observable left
        to call motion - and an exact comparison would be a comparison against
        the accumulated float error of a fifty-step ramp, which lands a whole
        integration step late and for no physical reason.
        """
        return abs(self._rpm) < self._config.standstill_rpm

    def _settle_standstill(self) -> None:
        """End whichever transient the shaft was still moving for.

        A fault reaction becomes a latched FAULT; a commanded ramp-stop becomes
        plain SWITCHED_ON. Both only once the shaft has actually stopped: a
        transient that ended on a timer would be a transient that lies about
        motion, which is the one thing this module may not do.
        """
        settled = SETTLES_INTO.get(self._state)
        if settled is not None and self._at_standstill():
            self._state = settled

    def _check_watchdog(self) -> None:
        """The drive's own ttO timeout: silence for ``tto`` latches SLF.

        This is the backstop the whole control loop is arranged around - the
        keepalive is written first in every tick precisely so that a stalled
        loop stops writing and this fires. It runs whatever the drive is doing,
        including at standstill, which is faithful (the keypad shows SLF with
        the motor stopped too) and useful: it means the software cannot get
        away with dropping the keepalive whenever it believes the machine is
        idle.
        """
        if not self._comms_established or self._fault is not None:
            return
        if elapsed(self._last_frame_at, self._last_at) < self._config.tto:
            return
        self._latch_fault(DriveFault.MODBUS_COMM_LOSS)

    def _latch_fault(self, fault: DriveFault) -> None:
        """Latch a fault, choosing the state from its reaction and the shaft.

        The reaction is looked up from the fault as the *code table* reports it
        - so an injected fault the table cannot name behaves like the UNKNOWN
        it will be reported as, rather than like something the simulator knows
        privately and the hardware path never would.
        """
        report = describe_fault(lft_code_for(fault, self._fault_codes), self._fault_codes)
        self._fault = report
        if self._at_standstill():
            self._state = SimState.FAULT
        elif FAULT_REACTIONS[report.fault] is FaultReaction.RAMP_TO_STOP:
            self._state = SimState.FAULT_REACTION_RAMP_STOP
        else:
            self._state = SimState.FAULT_REACTION_FREEWHEEL

    def _current(self) -> Amperes:
        """Motor current: magnetising, plus load, plus the cost of accelerating.

        ``I = no_load + load_at_nominal * |rpm| / nominal + k * |drpm/dt|``,
        clamped into ``[0, current_limit]``. Magnitudes, not signed values:
        current does not care which way the shaft turns, and a signed form
        would have a reversed motor drawing *less*.

        Zero whenever the output stage is off, including while the shaft coasts
        - there is no motor current without a drive pushing it. That is what
        makes a no-load rule ("enabled, commanded, and drawing nothing")
        distinguishable from a stopped machine.
        """
        if not self._energised():
            return Amperes(0.0)
        config = self._config
        load = config.load_current_at_nominal * abs(self._rpm) / config.nominal_rpm
        accelerating = config.amps_per_rpm_per_second * abs(self._rate)
        draw = config.no_load_current + load + accelerating
        return Amperes(max(0.0, min(float(config.current_limit), draw)))

    def _drive_state(self) -> DriveState:
        """What a caller would decode from this drive's ETA word right now."""
        return decode_status_word(ETA_WORDS[self._state])
