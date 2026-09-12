"""The drive seam: everything the system knows about the ATV320 except how to talk to it.

This module is pure. It imports no ``pymodbus``, no ``serial``, nothing that
touches a wire - ``tests/test_drive_contract.py`` parses its imports and fails
if that ever changes. Two things implement :class:`DriveBackend`: the real
Modbus driver (``src/motor/atv320.py``) and the simulator (``src/sim/``). The
safety layer above is written against this protocol only, so the closed-loop
safety tests run with no drive attached while still exercising the same types
the hardware path uses.

What lives here:

* the CiA402 state machine as the drive reports it (:class:`DriveState`,
  :func:`decode_status_word`),
* the command words that move it (:class:`ControlWord`),
* the fault vocabulary an operator reads (:class:`DriveFault`,
  :func:`describe_fault`),
* the register map and its one configurable offset (:class:`RegisterMap`),
* the closed error union every fallible drive operation returns
  (:data:`DriveError`).

What deliberately does NOT live here: retries, timeouts, keepalive, ramp
shaping, speed clamps, fault-reset policy. Those are decisions with a person
inside the centrifuge attached to them, and they belong in the safety layer
where they can be tested against a simulated plant - not hidden inside a
decoding helper where nobody looks.

Hardware, from the commissioning notes: ATV320U04M2C (0.37 kW, 200-240 V single
phase in) driving a SEW KA37 DRS71S4 (0.37 kW, 1380 rpm at 50 Hz, 2.15 A,
delta 230 V) through i = 49.79, so ~27.7 output rpm at full motor speed. Modbus
RTU on the drive's embedded RJ45: address 1, 19200 baud, 8E1.

Two facts from those notes shape every docstring below:

1. The drive's STO safety input is **jumpered**. There is no independent means
   of removing torque, so "the software commanded a stop" is the only stop
   there is, and nothing this module reports may be stronger than what was
   actually observed.
2. The DC bus can absorb ~11 J against ~420 J of rotating kinetic energy
   (~2.5%), so a stop commanded faster than the measured 3-4 s trips ObF into
   FREEWHEEL - worse than a slow ramp. Nothing here may promise a fast stop.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum, auto, unique
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from src.result import Result
from src.units import (
    Amperes,
    MotorRpm,
    OutOfRange,
    RawRegister,
    RegisterAddress,
    Seconds,
    StatusWord,
    register_to_signed,
)

# =========================================================================
# The state the drive reports
# =========================================================================


@unique
class DriveState(Enum):
    """Where the drive says it is, decoded from the ETA status word.

    These are the CiA402 states, not our own idea of the machine's state. The
    distinction matters: this is *evidence*, and deciding what to do about it
    is the safety layer's job.

    ``COMM_LOST`` is the one member :func:`decode_status_word` can never
    return, because a lost link delivers no status word to decode. The
    transport produces it instead, and it means "the drive's state is
    unknown" - which is NOT "the motor is stopped". With STO jumpered and the
    keepalive gone, the drive's own ``ttO`` timeout is what ramps the motor
    down; until that has demonstrably happened, a machine reporting COMM_LOST
    must be assumed to be turning.
    """

    NOT_READY = auto()
    # Powering up, or in a transient none of the patterns below name. CiA402
    # quick-stop-active lands here too (see decode_status_word), which is
    # exactly why NOT_READY must never be read as "stopped".

    SWITCH_ON_DISABLED = auto()
    # DC bus not ready, or STO open. The only state that says anything
    # reassuring about torque - and on this machine STO is jumpered, so in
    # practice it means the bus is down.

    READY = auto()
    # Ready to switch on: bus charged, no command accepted yet.

    SWITCHED_ON = auto()
    # Switched on, output stage not enabled. The motor cannot turn, but it is
    # one command word away from being able to.

    OPERATION_ENABLED = auto()
    # The motor is commanded and may be turning at anything up to the
    # setpoint. Seeing this at startup means a previous process died with the
    # motor running: command zero, disable, latch, and require an operator
    # acknowledgement. Never resume motion automatically.

    FAULT = auto()
    # A fault is latched, or the drive is reacting to one. The motor may still
    # be turning: a fault reaction is a ramp or a freewheel, not an
    # instantaneous stop.

    COMM_LOST = auto()
    # Set by the transport, never by the decoder. See the class docstring.


# --- ETA bit patterns ----------------------------------------------------
#
# From the commissioning notes, which match the CiA402 profile:
#     bit 3 (& 0x08)   fault present
#     & 0x6F == 0x27   operation enabled (running)
#     & 0x6F == 0x23   switched on
#     & 0x4F == 0x40   switch-on disabled (DC bus not ready / STO)
#
# The notes glossed 0x23 as "ready to start (switched on)", collapsing two
# distinct CiA402 states into one label. DriveState has both, so READY uses the
# profile's own "ready to switch on" pattern, 0x21 (bits 0 and 5), and
# SWITCHED_ON keeps 0x23 (bits 0, 1 and 5). The difference between them is
# whether the output stage is switched on, i.e. the difference between "the
# motor definitely cannot turn" and "one command word away from turning".

FAULT_BIT: Final[int] = 0x08
"""ETA bit 3: a fault is latched, or the drive is reacting to one."""

CIA402_STATE_MASK: Final[int] = 0x6F
"""Bits 0-3, 5 and 6: the state bits, ignoring voltage-enabled and warning."""

SWITCH_ON_DISABLED_MASK: Final[int] = 0x4F
"""Narrower mask: switch-on-disabled is defined without the quick-stop bit."""

SWITCH_ON_DISABLED_PATTERN: Final[int] = 0x40
OPERATION_ENABLED_PATTERN: Final[int] = 0x27
SWITCHED_ON_PATTERN: Final[int] = 0x23
READY_TO_SWITCH_ON_PATTERN: Final[int] = 0x21


def decode_status_word(word: StatusWord) -> DriveState:
    """Decode one ETA status word. Pure, total, and it never raises.

    Total by construction: every one of the 65536 possible words maps to a
    state, with :data:`DriveState.NOT_READY` as the honest "I do not recognise
    this" answer. A decoder that could raise would raise inside the control
    loop, with the motor commanded.

    **The order of the tests is load-bearing.** The fault bit is checked
    FIRST, before any of the running patterns, for two reasons:

    * The drive can report a fault together with bits that still look
      operational - 0x2F is the operation-enabled pattern with bit 3 set.
      Checked in any other order such a word decodes as a non-fault state:
      either a bogus "running", or the equally dangerous "NOT_READY", which
      reads as "stopped" to anything that does not know better.
    * Fault-reaction-active (bit 3 set, fault bits 0x0F) is the state the drive
      passes through *while it is still decelerating a loaded centrifuge*. It
      has to report as FAULT so the safety layer reacts, not as a transient
      somebody filtered out.

    Bit 3 alone is tested rather than the profile's ``& 0x4F == 0x08`` so that
    both FAULT and FAULT-REACTION-ACTIVE land on FAULT. Losing that distinction
    is deliberate: the required response is the same either way, and fewer
    states means fewer states a caller can forget.

    Unrecognised words - including CiA402 quick-stop-active, 0x07 - fall
    through to NOT_READY. That is a statement about the drive's *control state*,
    not about the shaft: during a quick stop the motor is still turning.
    Nothing may infer motion from :class:`DriveState` alone; read
    :attr:`DriveStatus.output_rpm`.

    The word arrives already parsed into 0..65535 by ``src.units.raw_register``
    at the Modbus boundary, so there is no range check here; the high bits this
    profile does not define are simply masked away.
    """
    if word & FAULT_BIT != 0:
        return DriveState.FAULT
    if word & SWITCH_ON_DISABLED_MASK == SWITCH_ON_DISABLED_PATTERN:
        return DriveState.SWITCH_ON_DISABLED
    if word & CIA402_STATE_MASK == OPERATION_ENABLED_PATTERN:
        return DriveState.OPERATION_ENABLED
    if word & CIA402_STATE_MASK == SWITCHED_ON_PATTERN:
        return DriveState.SWITCHED_ON
    if word & CIA402_STATE_MASK == READY_TO_SWITCH_ON_PATTERN:
        return DriveState.READY
    return DriveState.NOT_READY


# =========================================================================
# The command words that move the state machine
# =========================================================================


@unique
class ControlWord(IntEnum):
    """CiA402 command words for CMD (register 8501), named by intent.

    ``IntEnum`` because the value really is the integer written to a register.
    The names exist so a call site reads as an intention instead of a magic
    number, and so a typo is a ``NameError`` rather than a different command.

    Sequences, from the commissioning notes:

    * start: SHUTDOWN -> SWITCH_ON -> ENABLE_OPERATION
    * stop:  SWITCH_ON -> SHUTDOWN
    * after a fault: FAULT_RESET, and **only** on an explicit operator action.
      There is no automatic fault reset and no automatic resumption of motion
      anywhere in this system.

    Note what is absent: there is no "fast stop" word. The DC bus absorbs ~11 J
    of the ~420 J stored in the spinning rig, so a stop commanded faster than
    the commissioned 3-4 s ramp trips ObF and drops the drive into FREEWHEEL -
    a longer, uncontrolled coast-down with a person inside. The fastest stop
    available is the ramp the drive is commissioned with.
    """

    SHUTDOWN = 6
    SWITCH_ON = 7
    ENABLE_OPERATION = 15
    FAULT_RESET = 128


# =========================================================================
# Faults
# =========================================================================


@unique
class DriveFault(Enum):
    """A fault the drive latched, named as its own display names it.

    Members carry ``auto()`` values on purpose. The numeric LFT codes live in
    :data:`LFT_FAULT_CODES` (one table, provisional, overridable) and the
    keypad mnemonics in the mapping below, so nothing in this enum depends on a
    number that has not been verified against the drive.
    """

    UNDERVOLTAGE = auto()
    MOTOR_PHASE_LOSS = auto()
    OVERCURRENT = auto()
    MOTOR_OVERLOAD = auto()
    OUTPUT_SHORT = auto()
    MODBUS_COMM_LOSS = auto()
    NO_MOTOR = auto()
    INTERNAL = auto()
    DC_BUS_OVERVOLTAGE = auto()

    NO_FAULT_STORED = auto()
    # LFT read back as 0. Not a fault: the register holds the LAST fault and is
    # empty when none has occurred. Reading this while the status word says
    # FAULT is a contradiction worth logging, not a fault to name.

    UNKNOWN = auto()
    # A code this build does not recognise. Always paired with the raw number
    # in a FaultReport, so an operator can read the mnemonic off the drive
    # instead of being told a fault we cannot actually identify.

    @property
    def mnemonic(self) -> str:
        """The short code the drive shows on its own display.

        The operator is standing in front of the drive. Handing them the same
        four characters it is showing them is worth more than any prose.
        """
        return _FAULT_MNEMONIC[self]

    @property
    def meaning(self) -> str:
        """One operator-facing sentence: what happened, and what it implies."""
        return _FAULT_MEANING[self]


_FAULT_MNEMONIC: Final[Mapping[DriveFault, str]] = MappingProxyType(
    {
        DriveFault.UNDERVOLTAGE: "USF",
        DriveFault.MOTOR_PHASE_LOSS: "OPF",
        DriveFault.OVERCURRENT: "OCF",
        DriveFault.MOTOR_OVERLOAD: "OLF",
        DriveFault.OUTPUT_SHORT: "SCF",
        DriveFault.MODBUS_COMM_LOSS: "SLF",
        # Both of these are spelled "nOF" in the sources available here, with
        # two different meanings. See the NOTE above LFT_FAULT_CODES: the
        # ambiguity is preserved rather than resolved by guesswork.
        DriveFault.NO_MOTOR: "nOF",
        DriveFault.NO_FAULT_STORED: "nOF",
        DriveFault.INTERNAL: "InF",
        DriveFault.DC_BUS_OVERVOLTAGE: "ObF",
        DriveFault.UNKNOWN: "?",
    }
)
"""Keypad mnemonic per fault. Total over ``DriveFault``; a test proves it."""

_FAULT_MEANING: Final[Mapping[DriveFault, str]] = MappingProxyType(
    {
        DriveFault.UNDERVOLTAGE: (
            "mains undervoltage: the 230 V supply dipped or was removed. The motor "
            "coasts down on its own; check the supply before restarting."
        ),
        DriveFault.MOTOR_PHASE_LOSS: (
            "motor phase loss: one of the three output phases is open. Check the "
            "motor cable and the delta terminal links before restarting."
        ),
        DriveFault.OVERCURRENT: (
            "overcurrent on the output: the drive tripped instantaneously. Suspect a "
            "mechanical jam or a shorted winding; do not reset without inspecting."
        ),
        DriveFault.MOTOR_OVERLOAD: (
            "motor thermal overload: the drive's thermal model of the motor says it "
            "is too hot. Let it cool. Repeated trips mean the load is too high, not "
            "that the threshold is wrong."
        ),
        DriveFault.OUTPUT_SHORT: (
            "short circuit on the drive output. Do not reset: find the short first."
        ),
        DriveFault.MODBUS_COMM_LOSS: (
            "Modbus communication loss: the drive stopped hearing this controller and "
            "applied its own ttO timeout response. Check the RJ45 cable and that the "
            "port is still 19200 8E1, address 1."
        ),
        DriveFault.NO_MOTOR: (
            "no motor detected on the output. Check the motor cable; the drive cannot "
            "control a load it cannot see."
        ),
        DriveFault.INTERNAL: (
            "internal drive fault: the drive has failed its own self-check. Power it "
            "down completely. If it returns, the drive is faulty - do not keep "
            "resetting it with somebody in the machine."
        ),
        DriveFault.DC_BUS_OVERVOLTAGE: (
            "DC bus overvoltage while braking: the deceleration ramp asked the bus to "
            "absorb more than the ~11 J it can hold, against the ~420 J stored in the "
            "spinning rig. The drive has dropped to freewheel, so the centrifuge is "
            "coasting down uncontrolled. Lengthen the ramp; shortening it makes this "
            "worse, not faster."
        ),
        DriveFault.NO_FAULT_STORED: (
            "no fault stored: the drive's LFT register is empty. If the status word "
            "reported a fault at the same time then the two disagree - treat the "
            "status word as authoritative and log the disagreement."
        ),
        DriveFault.UNKNOWN: (
            "unrecognised fault code. Read the mnemonic from the drive's display and "
            "look it up in the ATV320 programming manual."
        ),
    }
)
"""Operator-facing meaning per fault. Total over ``DriveFault``; a test proves it."""


# -------------------------------------------------------------------------
# NOTE ON THE NUMERIC LFT CODES - READ THIS BEFORE TRUSTING THEM
# -------------------------------------------------------------------------
# The commissioning notes give fault MNEMONICS (USF, OPF, OCF, ...) but no
# numeric LFT values, so the numbers below are the ATV32/ATV320 enumeration as
# best known here and are PROVISIONAL. They must be verified against the LFT
# enumeration in the drive's own programming manual, or by triggering each
# fault on the bench and reading the register, before this machine carries a
# person.
#
# The design is arranged so a MISSING entry degrades safely: any code absent
# from this table yields DriveFault.UNKNOWN carrying the raw number, never a
# fault name we cannot justify. The residual risk is a WRONG entry - telling an
# operator "motor overload" when the drive is showing something else - which is
# why describe_fault() takes the table as a parameter: commissioning can
# correct it without editing this module.
#
# One entry is not a guess and must not be removed: code 0 is "no fault
# stored". LFT holds the LAST fault, so it reads 0 on a healthy drive, and
# mapping 0 to a real fault would make every faultless drive report one.
#
# The notes also gloss "nOF" as "no motor detected", whereas in Altivar
# parameter tables nOF reads as "no fault". Both meanings are kept as separate
# enum members (NO_MOTOR, NO_FAULT_STORED) and only the defensible one is given
# a code here. Resolve this from the manual; do not pick one by feel.
# -------------------------------------------------------------------------

LFT_FAULT_CODES: Final[Mapping[RawRegister, DriveFault]] = MappingProxyType(
    {
        RawRegister(0): DriveFault.NO_FAULT_STORED,
        RawRegister(2): DriveFault.INTERNAL,
        RawRegister(3): DriveFault.OUTPUT_SHORT,
        RawRegister(11): DriveFault.MOTOR_OVERLOAD,
        RawRegister(14): DriveFault.DC_BUS_OVERVOLTAGE,
        RawRegister(15): DriveFault.OVERCURRENT,
        RawRegister(16): DriveFault.MOTOR_PHASE_LOSS,
        RawRegister(19): DriveFault.MODBUS_COMM_LOSS,
        RawRegister(22): DriveFault.UNDERVOLTAGE,
    }
)
"""LFT register value -> fault. PROVISIONAL: see the note above."""


@dataclass(frozen=True, slots=True)
class FaultReport:
    """A decoded LFT register: the fault, the raw code, and a line for a human.

    A frozen record rather than a ``(fault, message)`` pair so the raw code
    stays machine-readable. An operator needs the sentence; a log line and
    :class:`DriveFaulted` need the number, and re-parsing a number out of prose
    is how numbers get lost.
    """

    fault: DriveFault
    raw_code: RawRegister
    message: str


def describe_fault(
    raw: RawRegister,
    codes: Mapping[RawRegister, DriveFault] = LFT_FAULT_CODES,
) -> FaultReport:
    """Turn a raw LFT register value into something an operator can act on.

    An unrecognised code degrades to :data:`DriveFault.UNKNOWN` and carries the
    raw number in both the report and the message. This module never claims a
    fault it cannot identify: "unknown drive fault code 27" sends someone to
    look at the drive, whereas a plausible-but-wrong name sends them to fix the
    wrong thing.

    ``codes`` is a parameter so commissioning can correct the provisional table
    (see the note above it) without a code change.
    """
    fault = codes.get(raw)
    if fault is None:
        return FaultReport(
            fault=DriveFault.UNKNOWN,
            raw_code=raw,
            message=f"unknown drive fault code {raw} (0x{raw:04X}): {DriveFault.UNKNOWN.meaning}",
        )
    return FaultReport(
        fault=fault,
        raw_code=raw,
        message=f"{fault.mnemonic} (LFT {raw}): {fault.meaning}",
    )


# =========================================================================
# One observation of the drive
# =========================================================================


@dataclass(frozen=True, slots=True)
class DriveStatus:
    """One consistent read of the drive: ETA, LFRD echo, RFRD, LCR, LFT.

    Frozen because a decision taken from an observation must not be able to
    change under the decision; slotted because this is built at the polling
    rate.

    ``setpoint_echo_rpm`` is LFRD read back, not the value we believe we wrote.
    That read-back is the only evidence a write actually landed: a Modbus write
    response echoes the request, so a write to a wrong address is "acked" while
    the speed reference never moves. Compare it against the intended setpoint
    every cycle.

    There is deliberately **no timestamp field**. A ``DriveStatus`` is not
    self-certifying: whoever reads it knows ``now`` and stamps it, and nothing
    may treat one as fresh merely because it exists. A stale status regulated
    on as if it were current is one of the silent failures this whole project
    is organised around.

    Note what a status does NOT tell you. ``state`` is the drive's control
    state, not the shaft's: FAULT, NOT_READY and COMM_LOST are all compatible
    with a centrifuge still turning. ``output_rpm`` is the field that speaks
    about motion.

    There is no constructor here for "comms lost", and that omission is
    deliberate: it would have to invent values for the speed fields, and a
    fabricated 0 rpm is precisely the lie that gets someone hurt. A caller that
    has lost the link has no status - it has the last one, and the knowledge
    that it is old.
    """

    state: DriveState
    status_word: StatusWord
    setpoint_echo_rpm: MotorRpm
    """LFRD read back, for write verification. Signed rpm at the MOTOR shaft."""

    output_rpm: MotorRpm
    """RFRD: measured output speed, signed, at the MOTOR shaft (not the load)."""

    current: Amperes
    """LCR, scaled. Nameplate is 2.15 A, so a healthy figure is single-digit."""

    fault: DriveFault | None
    """The named fault, or None when no LFT value was read or none applies."""

    @property
    def fault_present(self) -> bool:
        """Whether the drive is signalling a fault.

        Earns its place by settling the authority question in one spot: the
        STATUS WORD decides whether a fault exists, ``fault`` only names it. A
        named fault with a non-FAULT state means the fault was read and then
        cleared, or that LFT still holds a historical value - neither of which
        is "a fault now", and each of which would get a different answer if
        every caller decided for itself.
        """
        return self.state is DriveState.FAULT


# =========================================================================
# Register decoding (still no hardware: these are pure integer transforms)
# =========================================================================

LCR_AMPS_PER_COUNT: Final[float] = 0.1
"""LCR (3204) is in units of 0.1 A."""


def decode_speed(raw: RawRegister) -> MotorRpm:
    """Decode LFRD or RFRD: **signed** rpm at the motor shaft.

    Read as unsigned, a reverse speed of -1 rpm looks like 65535 rpm, which
    would sail through any upper clamp expressed as "less than 900". The
    two's-complement reinterpretation itself lives in ``src.units``, which owns
    every conversion.
    """
    return MotorRpm(register_to_signed(raw))


def decode_current(raw: RawRegister) -> Amperes:
    """Decode LCR into amperes.

    No plausibility ceiling is applied here, deliberately: the sane maximum is
    a property of this motor (2.15 A nameplate) and belongs with the other
    machine limits in the safety layer, not buried in a decoder. What this
    function guarantees is that the scale is applied exactly once.
    """
    return Amperes(raw * LCR_AMPS_PER_COUNT)


# =========================================================================
# The register map
# =========================================================================
#
# DANGER - a misaddressed write is not a failed write.
#
# Most Altivar parameters are writable while the drive is running. An address
# off by one does not bounce; it writes the speed setpoint into whatever
# parameter is next door. ACC/DEC (the ramp times), HSP (the high-speed
# ceiling) and the protection thresholds are all within reach, so a wrong
# offset can silently remove the very ceiling the software is relying on, and
# every subsequent read still looks perfectly normal.
#
# Hence: the addresses below are the LOGICAL addresses from the commissioning
# notes, the offset is applied in exactly ONE place (RegisterMap.resolve), and
# it is configuration rather than a constant, because Altivar documentation and
# Modbus masters disagree about whether these are 0- or 1-based and the only
# way to know is to try it on the bench.
#
# Establish the offset with READS ONLY. ETA (3201) on a healthy drive returns a
# word that decodes; a wrong offset returns one that does not. Never calibrate
# an addressing offset by writing.

CMD_LOGICAL: Final[RegisterAddress] = RegisterAddress(8501)
"""Command word, write. The CiA402 control word; see :class:`ControlWord`."""

LFRD_LOGICAL: Final[RegisterAddress] = RegisterAddress(8602)
"""Speed setpoint, write. SIGNED rpm at the motor shaft."""

ETA_LOGICAL: Final[RegisterAddress] = RegisterAddress(3201)
"""Status word, read. See :func:`decode_status_word`."""

RFRD_LOGICAL: Final[RegisterAddress] = RegisterAddress(8604)
"""Output speed, read. SIGNED rpm at the motor shaft."""

LCR_LOGICAL: Final[RegisterAddress] = RegisterAddress(3204)
"""Motor current, read, in 0.1 A units."""

LFT_LOGICAL: Final[RegisterAddress] = RegisterAddress(7121)
"""Last fault code, read. See :func:`describe_fault`."""

# The only offsets that can be right: a 0/1-based indexing disagreement.
# Anything outside this band is a typo or a unit mix-up, and the consequence of
# accepting one is writing a speed reference into an unrelated parameter. The
# band is checked once, at construction, so a bad value cannot reach a write.
REGISTER_OFFSET_MIN: Final[int] = -1
REGISTER_OFFSET_MAX: Final[int] = 1


@dataclass(frozen=True, slots=True)
class RegisterMap:
    """The logical register addresses plus the single indexing offset.

    Frozen so the addressing cannot change after an operator approved it, and
    so the poller and the writer can share one map without either being able to
    shift the other's addresses.

    Construction raises ``ValueError`` on a nonsensical offset rather than
    returning a ``Result``. That is the boundary ``src/config.py`` already
    draws: ``Result`` is mandatory in the drive path, where a dropped error
    leaves a motor commanded, but this object is built at startup with nothing
    spinning, and refusing to start is the correct response to an addressing
    configuration nobody can vouch for.
    """

    offset: int = 0
    """Added to every logical address. A delta, deliberately not a
    ``RegisterAddress``: adding two addresses together is meaningless, and the
    type should say so."""

    def __post_init__(self) -> None:
        if self.offset < REGISTER_OFFSET_MIN or self.offset > REGISTER_OFFSET_MAX:
            raise ValueError(
                f"register offset {self.offset} is outside "
                f"{REGISTER_OFFSET_MIN}..{REGISTER_OFFSET_MAX}; the only legitimate "
                "offset is a 0/1-based indexing difference, and a larger one would "
                "address an unrelated drive parameter"
            )

    def resolve(self, logical: RegisterAddress) -> RegisterAddress:
        """Apply the offset. The ONE place it is ever applied."""
        return RegisterAddress(logical + self.offset)

    @property
    def cmd(self) -> RegisterAddress:
        """CMD, write: the command word."""
        return self.resolve(CMD_LOGICAL)

    @property
    def lfrd(self) -> RegisterAddress:
        """LFRD, write: the signed rpm setpoint."""
        return self.resolve(LFRD_LOGICAL)

    @property
    def eta(self) -> RegisterAddress:
        """ETA, read: the status word."""
        return self.resolve(ETA_LOGICAL)

    @property
    def rfrd(self) -> RegisterAddress:
        """RFRD, read: the signed output speed."""
        return self.resolve(RFRD_LOGICAL)

    @property
    def lcr(self) -> RegisterAddress:
        """LCR, read: motor current in 0.1 A units."""
        return self.resolve(LCR_LOGICAL)

    @property
    def lft(self) -> RegisterAddress:
        """LFT, read: the last fault code."""
        return self.resolve(LFT_LOGICAL)


# =========================================================================
# The closed error union
# =========================================================================
#
# Each variant carries the context a log line or an operator screen needs. Bare
# sentinels were rejected deliberately: "comms timed out" without the duration
# cannot be told apart from a tuning problem at 2am, and "unexpected state"
# without both states is unactionable.
#
# Adding a variant here breaks every incomplete `match` in the codebase at
# check time, naming the variant that was forgotten. That is the point: an
# unhandled drive error must not reach the bench, let alone a session with a
# person in the machine.


@dataclass(frozen=True, slots=True)
class CommTimeout:
    """The drive did not answer in time.

    ``after`` is how long was actually waited, not the configured budget: a
    timeout firing at 0.6 s when 0.5 s was allowed says something different
    from one firing at 5 s, and only the measured value distinguishes them.
    """

    after: Seconds


@dataclass(frozen=True, slots=True)
class BadResponse:
    """A frame arrived but made no sense: bad CRC, wrong function code, short read.

    ``detail`` is free-form diagnostic text for a human, never something to
    branch on. Decisions are made on the variant, so that adding a new failure
    mode is a type error rather than a string nobody ever parsed.
    """

    detail: str


@dataclass(frozen=True, slots=True)
class UnexpectedState:
    """The drive was in a state the operation could not be performed from.

    Carries both states because the pair is the diagnosis: expecting
    SWITCHED_ON and finding SWITCH_ON_DISABLED is a power problem, whereas
    finding OPERATION_ENABLED when expecting READY means something else is
    already commanding this drive.
    """

    expected: DriveState
    actual: DriveState


@dataclass(frozen=True, slots=True)
class DriveFaulted:
    """The drive is in fault, so the requested operation cannot proceed.

    Carries the raw LFT code alongside the decoded fault so that even an
    unrecognised fault is fully reported. See :func:`describe_fault`.
    """

    fault: DriveFault
    raw_code: RawRegister


type DriveError = CommTimeout | BadResponse | UnexpectedState | DriveFaulted | OutOfRange
"""Every way a drive operation can fail. Closed; match it with the nested form:

    match result:
        case Ok(value):
            ...
        case Err(error):
            match error:
                case CommTimeout():
                    ...
                case _ as unreachable:
                    assert_never(unreachable)

``OutOfRange`` is reused from ``src.units`` rather than redefined, because a
value outside its physical domain is the same failure whether it was noticed
while parsing an ADC sample or a drive register.
"""


# =========================================================================
# The seam itself
# =========================================================================


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

    async def open(self) -> Result[None, DriveError]:
        """Acquire the link. Must NOT enable the drive or command a speed.

        Never assume the drive's state afterwards - read it. If ETA reports
        OPERATION_ENABLED, a previous process died with the motor turning.
        """
        ...

    async def close(self) -> Result[None, DriveError]:
        """Release the link.

        Must leave the drive in a state it can be left in, not merely drop the
        port. Closing stops the keepalive, which arms the drive's own ttO
        timeout response; that is a backstop, not a stop command.
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

    def emergency_disable_blocking(self, timeout: Seconds) -> None:
        """Remove the run command, synchronously, best effort.

        **Synchronous on purpose.** This has to be callable from ``atexit``,
        from an OS signal handler, and from an ``except`` branch - places where
        there may be no running event loop, where the loop may be the thing
        that died, and where ``await`` is simply not available. A disable that
        only exists as a coroutine is a disable that does not happen on the
        paths that need it most.

        Contract for implementations:

        * Give up after ``timeout`` and return. A blocking call with no bound
          hangs process exit, and a Pi that will not shut down is a Pi somebody
          power-cycles mid-session.
        * Never raise. There is nobody left up the stack to handle it: log and
          return.
        * Do the minimum that removes torque demand - write the stop command
          word - and nothing that needs a reply to be interpreted.
        * Returning does NOT mean the motor has stopped. It means the attempt
          was made. With STO jumpered there is no independent torque removal,
          and the ramp-down takes seconds (a faster stop trips ObF into
          freewheel, which is slower still). Nothing may report "stopped" on
          the strength of this call returning.
        """
        ...
