"""The drive seam: everything the system knows about the ATV320 except how to talk to it.

This module is pure. It imports no ``pymodbus``, no ``serial``, nothing that
touches a wire - ``tests/test_drive_contract.py`` parses its imports and fails
if that ever changes. Two things implement :class:`DriveBackend`: the real
Modbus driver (``src/motor/atv320.py``) and the simulator
(``src/motor/simulated.py``). The
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
from typing import Final, assert_never

from src.motor.backend import DriveBackend as _DriveBackend
from src.result import Err, Ok, Result
from src.units import (
    Amperes,
    Hertz,
    MotorRpm,
    OutOfRange,
    RawRegister,
    RegisterAddress,
    Seconds,
    StatusWord,
    register_to_signed,
)

DriveBackend = _DriveBackend

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

    **The stop is two words in that order, and the order is the safety
    property.** ``SWITCH_ON`` out of OPERATION_ENABLED is CiA402 transition 5
    ("disable operation"), which on this drive RAMPS. ``SHUTDOWN`` out of
    OPERATION_ENABLED is transition 8, which DROPS THE OUTPUT STAGE: torque
    goes away instantly and a loaded centrifuge freewheels for minutes while
    the drive reports the reassuring-sounding READY. Writing 6 to a turning
    machine is therefore not a stop, it is the slowest stop there is, and
    zeroing LFRD a few milliseconds earlier does not save it - the ramp has had
    no time to act and is then aborted. Measured against this repo's own plant
    model: standstill at t = 144.6 s for 6, t = 10.0 s when the run command is
    left in place; with the commissioned ``dEC`` of 3-4 s the ratio is worse.

    Note what is absent: there is no "fast stop" word. The DC bus absorbs ~11 J
    of the ~420 J stored in the spinning rig, so a stop commanded faster than
    the commissioned 3-4 s ramp trips ObF and drops the drive into FREEWHEEL -
    a longer, uncontrolled coast-down with a person inside. The fastest stop
    available is the ramp the drive is commissioned with, reached by zeroing
    LFRD and leaving the run command in place.
    """

    SHUTDOWN = 6
    SWITCH_ON = 7
    ENABLE_OPERATION = 15
    FAULT_RESET = 128


# =========================================================================
# Faults
# =========================================================================


@unique
class FaultCategory(Enum):
    """What kind of thing failed. Drives the operator's first move, never the stop:
    every category stops the machine the same way."""

    COMMUNICATION = "communication"
    """A communication link to the drive was lost; the drive itself is healthy."""
    SUPPLY = "supply"
    """The mains supply or the DC bus is out of bounds."""
    MOTOR = "motor"
    """The motor, its cable or its windings."""
    DRIVE_HARDWARE = "drive_hardware"
    """The drive's own electronics failed a self-check."""
    THERMAL = "thermal"
    """Something is too hot."""
    CONFIGURATION = "configuration"
    """The drive's configuration is wrong or was not accepted."""
    EXTERNAL = "external"
    """A fault signalled to the drive from outside."""
    LOAD = "load"
    """The mechanical load behaved unexpectedly."""
    FEEDBACK = "feedback"
    """The drive no longer trusts its speed or position measurement."""
    SAFETY = "safety"
    """An integrated safety function (STO) reported a fault."""
    INPUT = "input"
    """An analog input the drive uses is out of range."""
    BRAKE = "brake"
    """The brake or its control."""
    NONE = "none"
    """No fault (LFT reads 0)."""
    UNIDENTIFIED = "unidentified"
    """A code this build cannot name."""


@dataclass(frozen=True, slots=True)
class FaultSpec:
    """What the drive's manual says about one fault, and this machine's policy for it."""

    mnemonic: str
    """What the drive's own display shows."""

    category: FaultCategory
    resettable: bool
    """Whether an operator may reset it from the console, at standstill, after
    acknowledging. ``False``: power the drive down and inspect; the console refuses."""

    meaning: str
    """French, for the operator standing at the drive: what happened, what to do."""


@unique
class DriveFault(Enum):
    """A fault the drive latched, one member per LFT code of the ATV320.

    COMPLETE: every code of the Schneider enumeration of LFT (parameter 7121,
    "Altivar fault code", ``ATV32_communication_parameters`` A1.2IE03,
    Enumerations sheet; the ATV320 inherits the ATV32 fault set), plus
    :attr:`NO_FAULT_STORED` for LFT = 0 and :attr:`UNKNOWN` for any code this
    table does not list (a newer firmware). Values are ``auto()``: the numbers
    live in :data:`LFT_FAULT_CODES`, overridable at commissioning.

    **Every member stops the machine.** The drive has already applied its own
    fault reaction when this is read; the ``drive_fault`` safety rule latches on
    the FAULT state whatever the code, and nothing here ever resets a fault by
    itself. What the member decides is only what the operator is told and
    whether the console may later reset it (:attr:`resettable`).
    """

    NO_FAULT_STORED = auto()
    # LFT read back as 0. Not a fault: the register holds the LAST fault and is
    # empty when none has occurred.

    INTERNAL = auto()
    CONTROL_EEPROM = auto()
    INCORRECT_CONFIG = auto()
    INVALID_CONFIG = auto()
    MODBUS_COMM_LOSS = auto()
    INTERNAL_COM_LINK = auto()
    COM_NETWORK = auto()
    EXTERNAL_FAULT_INPUT = auto()
    OVERCURRENT = auto()
    PRECHARGE = auto()
    SPEED_FEEDBACK_LOSS = auto()
    DRIVE_OVERHEAT = auto()
    MOTOR_OVERLOAD = auto()
    DC_BUS_OVERVOLTAGE = auto()
    MAINS_OVERVOLTAGE = auto()
    OUTPUT_PHASE_LOSS = auto()
    INPUT_PHASE_LOSS = auto()
    UNDERVOLTAGE = auto()
    MOTOR_SHORT_CIRCUIT = auto()
    OVERSPEED = auto()
    AUTO_TUNING = auto()
    RATING_ERROR = auto()
    POWER_CALIBRATION = auto()
    INTERNAL_SERIAL_LINK = auto()
    INTERNAL_MFG_AREA = auto()
    POWER_EEPROM = auto()
    IMPEDANT_SHORT_CIRCUIT = auto()
    GROUND_SHORT_CIRCUIT = auto()
    THREE_PHASE_LOSS = auto()
    CANOPEN_COMM_LOSS = auto()
    BRAKE_CONTROL = auto()
    EXTERNAL_FAULT_COM = auto()
    BRAKE_FEEDBACK = auto()
    PC_COMM_LOSS = auto()
    ENCODER_COUPLING = auto()
    TORQUE_CURRENT_LIMIT = auto()
    HMI_COMM_LOSS = auto()
    POWER_REMOVAL = auto()
    PTC_PROBE = auto()
    PTC_OVERHEAT = auto()
    INTERNAL_CURRENT_MEASURE = auto()
    INTERNAL_MAINS_CIRCUIT = auto()
    INTERNAL_THERMAL_SENSOR = auto()
    IGBT_OVERHEAT = auto()
    IGBT_SHORT_CIRCUIT = auto()
    MOTOR_SHORT_CIRCUIT_2 = auto()
    TORQUE_TIMEOUT = auto()
    OUTPUT_CONTACTOR_STUCK = auto()
    OUTPUT_CONTACTOR_OPEN = auto()
    AI2_INPUT = auto()
    INPUT_CONTACTOR = auto()
    DIFFERENTIAL_CURRENT = auto()
    IGBT_DESATURATION = auto()
    INTERNAL_OPTION = auto()
    INTERNAL_CPU = auto()
    AI3_CURRENT_LOSS = auto()
    CARDS_PAIRING = auto()
    LOAD_FAULT = auto()
    BAD_CONFIG_TRANSFER = auto()
    CHANNEL_SWITCH = auto()
    PROCESS_UNDERLOAD = auto()
    PROCESS_OVERLOAD = auto()
    ANGLE_ERROR = auto()
    SAFETY_FUNCTION = auto()
    FIELDBUS = auto()
    FIELDBUS_STOP = auto()

    UNKNOWN = auto()
    # A code absent from the table: always paired with the raw number, never
    # resettable from the console (nobody can say what resetting it means).

    @property
    def spec(self) -> FaultSpec:
        """Everything known about this fault. Total: a test proves it."""
        return _FAULT_SPECS[self]

    @property
    def mnemonic(self) -> str:
        """The short code the drive shows on its own display."""
        return self.spec.mnemonic

    @property
    def meaning(self) -> str:
        """One operator-facing sentence (French): what happened, what to do."""
        return self.spec.meaning

    @property
    def category(self) -> FaultCategory:
        """What kind of thing failed."""
        return self.spec.category

    @property
    def resettable(self) -> bool:
        """Whether the console may reset it (at standstill, after acknowledgement)."""
        return self.spec.resettable


_FAULT_SPECS: Final[Mapping[DriveFault, FaultSpec]] = MappingProxyType(
    {
        DriveFault.NO_FAULT_STORED: FaultSpec(
            mnemonic="nOF",
            category=FaultCategory.NONE,
            resettable=False,
            meaning=(
                "aucun defaut memorise (LFT = 0). Si le mot d'etat indique un defaut en meme "
                "temps, les deux sont en desaccord : le mot d'etat fait foi."
            ),
        ),
        DriveFault.INTERNAL: FaultSpec(
            mnemonic="InF",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "erreur de calibration interne du variateur : couper l'alimentation ; si le "
                "defaut revient, le variateur est defaillant."
            ),
        ),
        DriveFault.CONTROL_EEPROM: FaultSpec(
            mnemonic="EEF1",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "memoire EEPROM de controle defaillante : couper l'alimentation, verifier la "
                "configuration ; remplacer le variateur si le defaut persiste."
            ),
        ),
        DriveFault.INCORRECT_CONFIG: FaultSpec(
            mnemonic="CFF",
            category=FaultCategory.CONFIGURATION,
            resettable=False,
            meaning=(
                "configuration incorrecte (carte changee ou parametres incoherents) : ne pas "
                "relancer, verifier et recharger la configuration mise en service."
            ),
        ),
        DriveFault.INVALID_CONFIG: FaultSpec(
            mnemonic="CFI",
            category=FaultCategory.CONFIGURATION,
            resettable=False,
            meaning=(
                "configuration invalide transferee au variateur : recharger la configuration "
                "mise en service avant tout redemarrage."
            ),
        ),
        DriveFault.MODBUS_COMM_LOSS: FaultSpec(
            mnemonic="SLF1",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=(
                "perte de communication Modbus : le variateur n'entendait plus la console et a "
                "applique son arret ttO. Verifier le cable RJ45/RS485 et la liaison 19200 8E1."
            ),
        ),
        DriveFault.INTERNAL_COM_LINK: FaultSpec(
            mnemonic="ILF",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "liaison interne du variateur (carte option) en defaut : couper l'alimentation "
                "et verifier la carte option."
            ),
        ),
        DriveFault.COM_NETWORK: FaultSpec(
            mnemonic="CnF",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=(
                "defaut du reseau de communication (carte de communication) : verifier le "
                "reseau, puis acquitter."
            ),
        ),
        DriveFault.EXTERNAL_FAULT_INPUT: FaultSpec(
            mnemonic="EPF1",
            category=FaultCategory.EXTERNAL,
            resettable=True,
            meaning=(
                "defaut externe signale par une entree logique ou un bit : trouver et lever la "
                "cause externe avant d'acquitter."
            ),
        ),
        DriveFault.OVERCURRENT: FaultSpec(
            mnemonic="OCF",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "surintensite en sortie : declenchement instantane. Suspecter un blocage "
                "mecanique ou un bobinage en court-circuit ; ne pas rearmer sans inspection."
            ),
        ),
        DriveFault.PRECHARGE: FaultSpec(
            mnemonic="CrF",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "defaut du circuit de precharge du bus continu : couper l'alimentation ; defaut "
                "materiel du variateur."
            ),
        ),
        DriveFault.SPEED_FEEDBACK_LOSS: FaultSpec(
            mnemonic="SPF",
            category=FaultCategory.FEEDBACK,
            resettable=False,
            meaning=(
                "perte du retour vitesse : la vitesse mesuree n'est plus fiable. Verifier le "
                "capteur et son cablage avant tout redemarrage."
            ),
        ),
        DriveFault.DRIVE_OVERHEAT: FaultSpec(
            mnemonic="OHF",
            category=FaultCategory.THERMAL,
            resettable=True,
            meaning=(
                "surchauffe du variateur : laisser refroidir, verifier la ventilation et la "
                "temperature ambiante avant d'acquitter."
            ),
        ),
        DriveFault.MOTOR_OVERLOAD: FaultSpec(
            mnemonic="OLF",
            category=FaultCategory.THERMAL,
            resettable=True,
            meaning=(
                "surcharge thermique du moteur : laisser refroidir. Des declenchements repetes "
                "signifient une charge trop forte, pas un seuil faux."
            ),
        ),
        DriveFault.DC_BUS_OVERVOLTAGE: FaultSpec(
            mnemonic="ObF",
            category=FaultCategory.SUPPLY,
            resettable=True,
            meaning=(
                "surtension du bus continu au freinage : le variateur est en roue libre, le "
                "bras ralentit sans controle. Allonger la rampe de deceleration, ne jamais la "
                "raccourcir."
            ),
        ),
        DriveFault.MAINS_OVERVOLTAGE: FaultSpec(
            mnemonic="OSF",
            category=FaultCategory.SUPPLY,
            resettable=True,
            meaning=(
                "surtension du reseau d'alimentation : verifier la tension secteur avant "
                "d'acquitter."
            ),
        ),
        DriveFault.OUTPUT_PHASE_LOSS: FaultSpec(
            mnemonic="OPF1",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "perte d'une phase moteur en sortie : verifier le cable moteur et le couplage "
                "avant tout redemarrage."
            ),
        ),
        DriveFault.INPUT_PHASE_LOSS: FaultSpec(
            mnemonic="PHF",
            category=FaultCategory.SUPPLY,
            resettable=True,
            meaning=(
                "perte d'une phase d'alimentation : verifier l'alimentation et les fusibles "
                "avant d'acquitter."
            ),
        ),
        DriveFault.UNDERVOLTAGE: FaultSpec(
            mnemonic="USF",
            category=FaultCategory.SUPPLY,
            resettable=True,
            meaning=(
                "sous-tension secteur : l'alimentation a chute ou a ete coupee. Le moteur "
                "ralentit seul ; verifier l'alimentation avant de redemarrer."
            ),
        ),
        DriveFault.MOTOR_SHORT_CIRCUIT: FaultSpec(
            mnemonic="SCF1",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "court-circuit moteur : ne pas rearmer. Trouver le court-circuit (cable, "
                "bornier, bobinage) d'abord."
            ),
        ),
        DriveFault.OVERSPEED: FaultSpec(
            mnemonic="SOF",
            category=FaultCategory.FEEDBACK,
            resettable=False,
            meaning=(
                "survitesse : le moteur a depasse sa vitesse maximale. Inspecter la mecanique "
                "et la configuration avant tout redemarrage."
            ),
        ),
        DriveFault.AUTO_TUNING: FaultSpec(
            mnemonic="tnF",
            category=FaultCategory.CONFIGURATION,
            resettable=False,
            meaning=(
                "echec de l'auto-reglage : refaire la mise en service moteur avant toute seance."
            ),
        ),
        DriveFault.RATING_ERROR: FaultSpec(
            mnemonic="InF1",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "calibre du variateur incoherent : defaut interne, couper l'alimentation et "
                "contacter la maintenance."
            ),
        ),
        DriveFault.POWER_CALIBRATION: FaultSpec(
            mnemonic="InF2",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "carte de puissance incompatible ou non calibree : defaut interne, couper "
                "l'alimentation."
            ),
        ),
        DriveFault.INTERNAL_SERIAL_LINK: FaultSpec(
            mnemonic="InF3",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=("liaison serie interne en defaut : defaut interne, couper l'alimentation."),
        ),
        DriveFault.INTERNAL_MFG_AREA: FaultSpec(
            mnemonic="InF4",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "zone de fabrication interne invalide : defaut interne, contacter la maintenance."
            ),
        ),
        DriveFault.POWER_EEPROM: FaultSpec(
            mnemonic="EEF2",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "memoire EEPROM de puissance defaillante : couper l'alimentation ; remplacer le "
                "variateur si le defaut persiste."
            ),
        ),
        DriveFault.IMPEDANT_SHORT_CIRCUIT: FaultSpec(
            mnemonic="SCF2",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "court-circuit impedant en sortie : ne pas rearmer, inspecter le cable et le "
                "moteur."
            ),
        ),
        DriveFault.GROUND_SHORT_CIRCUIT: FaultSpec(
            mnemonic="SCF3",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "court-circuit a la terre : ne pas rearmer, danger electrique. Inspecter "
                "l'isolement du moteur et du cable."
            ),
        ),
        DriveFault.THREE_PHASE_LOSS: FaultSpec(
            mnemonic="OPF2",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "perte des trois phases moteur : moteur deconnecte ou contacteur ouvert. "
                "Verifier le cablage avant tout redemarrage."
            ),
        ),
        DriveFault.CANOPEN_COMM_LOSS: FaultSpec(
            mnemonic="COF",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=("perte de communication CANopen : verifier le bus, puis acquitter."),
        ),
        DriveFault.BRAKE_CONTROL: FaultSpec(
            mnemonic="bLF",
            category=FaultCategory.BRAKE,
            resettable=False,
            meaning=(
                "defaut de commande du frein : inspecter le frein et sa commande avant tout "
                "redemarrage."
            ),
        ),
        DriveFault.EXTERNAL_FAULT_COM: FaultSpec(
            mnemonic="EPF2",
            category=FaultCategory.EXTERNAL,
            resettable=True,
            meaning=(
                "defaut externe signale par le reseau de communication : lever la cause avant "
                "d'acquitter."
            ),
        ),
        DriveFault.BRAKE_FEEDBACK: FaultSpec(
            mnemonic="brF",
            category=FaultCategory.BRAKE,
            resettable=False,
            meaning=(
                "retour du contact de frein incoherent : inspecter le frein avant tout redemarrage."
            ),
        ),
        DriveFault.PC_COMM_LOSS: FaultSpec(
            mnemonic="SLF2",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=(
                "perte de communication avec le logiciel PC : verifier la liaison, puis acquitter."
            ),
        ),
        DriveFault.ENCODER_COUPLING: FaultSpec(
            mnemonic="ECF",
            category=FaultCategory.FEEDBACK,
            resettable=False,
            meaning=("defaut d'accouplement du codeur : inspecter la mecanique du codeur."),
        ),
        DriveFault.TORQUE_CURRENT_LIMIT: FaultSpec(
            mnemonic="SSF",
            category=FaultCategory.LOAD,
            resettable=True,
            meaning=(
                "limitation de couple ou de courant prolongee : verifier que rien ne freine le "
                "bras avant d'acquitter."
            ),
        ),
        DriveFault.HMI_COMM_LOSS: FaultSpec(
            mnemonic="SLF3",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=(
                "perte de communication avec le terminal : verifier le terminal, puis acquitter."
            ),
        ),
        DriveFault.POWER_REMOVAL: FaultSpec(
            mnemonic="PrF",
            category=FaultCategory.SAFETY,
            resettable=False,
            meaning=(
                "defaut de la fonction de securite STO (suppression de puissance) : ne pas "
                "rearmer, faire controler la chaine de securite."
            ),
        ),
        DriveFault.PTC_PROBE: FaultSpec(
            mnemonic="PtFL",
            category=FaultCategory.THERMAL,
            resettable=True,
            meaning=("defaut de la sonde PTC sur LI6 : verifier la sonde et son cablage."),
        ),
        DriveFault.PTC_OVERHEAT: FaultSpec(
            mnemonic="OtFL",
            category=FaultCategory.THERMAL,
            resettable=True,
            meaning=(
                "surchauffe detectee par la sonde PTC du moteur : laisser refroidir avant "
                "d'acquitter."
            ),
        ),
        DriveFault.INTERNAL_CURRENT_MEASURE: FaultSpec(
            mnemonic="InF9",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "mesure de courant interne en defaut : defaut interne, couper l'alimentation."
            ),
        ),
        DriveFault.INTERNAL_MAINS_CIRCUIT: FaultSpec(
            mnemonic="InFA",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=("circuit d'entree interne en defaut : defaut interne, couper l'alimentation."),
        ),
        DriveFault.INTERNAL_THERMAL_SENSOR: FaultSpec(
            mnemonic="InFb",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "capteur thermique interne en defaut : defaut interne, couper l'alimentation."
            ),
        ),
        DriveFault.IGBT_OVERHEAT: FaultSpec(
            mnemonic="tJF",
            category=FaultCategory.THERMAL,
            resettable=True,
            meaning=(
                "surchauffe des IGBT : laisser refroidir, reduire la charge ; des "
                "declenchements repetes signalent un probleme."
            ),
        ),
        DriveFault.IGBT_SHORT_CIRCUIT: FaultSpec(
            mnemonic="SCF4",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=("court-circuit IGBT : defaut materiel grave, ne pas rearmer."),
        ),
        DriveFault.MOTOR_SHORT_CIRCUIT_2: FaultSpec(
            mnemonic="SCF5",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "court-circuit moteur (detection a la mise sous tension) : ne pas rearmer, "
                "inspecter."
            ),
        ),
        DriveFault.TORQUE_TIMEOUT: FaultSpec(
            mnemonic="SrF",
            category=FaultCategory.LOAD,
            resettable=True,
            meaning=("delai de couple depasse : verifier la charge mecanique avant d'acquitter."),
        ),
        DriveFault.OUTPUT_CONTACTOR_STUCK: FaultSpec(
            mnemonic="FCF1",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "contacteur de sortie colle : ne pas rearmer, faire intervenir la maintenance."
            ),
        ),
        DriveFault.OUTPUT_CONTACTOR_OPEN: FaultSpec(
            mnemonic="FCF2",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "contacteur de sortie reste ouvert : verifier le contacteur avant tout redemarrage."
            ),
        ),
        DriveFault.AI2_INPUT: FaultSpec(
            mnemonic="AI2F",
            category=FaultCategory.INPUT,
            resettable=True,
            meaning=("defaut de l'entree analogique AI2 : verifier le signal et son cablage."),
        ),
        DriveFault.INPUT_CONTACTOR: FaultSpec(
            mnemonic="LCF",
            category=FaultCategory.SUPPLY,
            resettable=False,
            meaning=(
                "defaut du contacteur de ligne : verifier le contacteur avant tout redemarrage."
            ),
        ),
        DriveFault.DIFFERENTIAL_CURRENT: FaultSpec(
            mnemonic="dCF",
            category=FaultCategory.MOTOR,
            resettable=False,
            meaning=(
                "courant differentiel (fuite) detecte : danger electrique, ne pas rearmer, "
                "inspecter l'isolement."
            ),
        ),
        DriveFault.IGBT_DESATURATION: FaultSpec(
            mnemonic="HdF",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "desaturation IGBT (court-circuit en sortie) : defaut materiel grave, ne pas "
                "rearmer."
            ),
        ),
        DriveFault.INTERNAL_OPTION: FaultSpec(
            mnemonic="InF6",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=("carte option interne en defaut : couper l'alimentation, verifier la carte."),
        ),
        DriveFault.INTERNAL_CPU: FaultSpec(
            mnemonic="InFE",
            category=FaultCategory.DRIVE_HARDWARE,
            resettable=False,
            meaning=(
                "defaut du processeur interne : couper l'alimentation ; si le defaut revient, "
                "le variateur est defaillant."
            ),
        ),
        DriveFault.AI3_CURRENT_LOSS: FaultSpec(
            mnemonic="LFF3",
            category=FaultCategory.INPUT,
            resettable=True,
            meaning=("perte du signal 4-20 mA sur AI3 : verifier le capteur et son cablage."),
        ),
        DriveFault.CARDS_PAIRING: FaultSpec(
            mnemonic="HCF",
            category=FaultCategory.CONFIGURATION,
            resettable=False,
            meaning=("appairage des cartes incorrect : verifier les cartes et la configuration."),
        ),
        DriveFault.LOAD_FAULT: FaultSpec(
            mnemonic="dLF",
            category=FaultCategory.LOAD,
            resettable=True,
            meaning=(
                "defaut de charge dynamique : verifier la mecanique entrainee avant d'acquitter."
            ),
        ),
        DriveFault.BAD_CONFIG_TRANSFER: FaultSpec(
            mnemonic="CFI2",
            category=FaultCategory.CONFIGURATION,
            resettable=False,
            meaning=(
                "transfert de configuration invalide : recharger la configuration mise en service."
            ),
        ),
        DriveFault.CHANNEL_SWITCH: FaultSpec(
            mnemonic="CSF",
            category=FaultCategory.CONFIGURATION,
            resettable=True,
            meaning=(
                "defaut de commutation de canal de commande : verifier la configuration des canaux."
            ),
        ),
        DriveFault.PROCESS_UNDERLOAD: FaultSpec(
            mnemonic="ULF",
            category=FaultCategory.LOAD,
            resettable=True,
            meaning=(
                "sous-charge du process : le moteur ne rencontre plus la charge attendue "
                "(accouplement ?). Inspecter avant d'acquitter."
            ),
        ),
        DriveFault.PROCESS_OVERLOAD: FaultSpec(
            mnemonic="OLC",
            category=FaultCategory.LOAD,
            resettable=True,
            meaning=(
                "surcharge du process : quelque chose freine le bras. Inspecter avant d'acquitter."
            ),
        ),
        DriveFault.ANGLE_ERROR: FaultSpec(
            mnemonic="ASF",
            category=FaultCategory.FEEDBACK,
            resettable=False,
            meaning=("erreur d'angle (moteur synchrone) : refaire le reglage moteur."),
        ),
        DriveFault.SAFETY_FUNCTION: FaultSpec(
            mnemonic="SAFF",
            category=FaultCategory.SAFETY,
            resettable=False,
            meaning=(
                "defaut d'une fonction de securite integree : ne pas rearmer, faire controler "
                "la chaine de securite."
            ),
        ),
        DriveFault.FIELDBUS: FaultSpec(
            mnemonic="FbE",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=("defaut du module de bus de terrain : verifier le module, puis acquitter."),
        ),
        DriveFault.FIELDBUS_STOP: FaultSpec(
            mnemonic="FbES",
            category=FaultCategory.COMMUNICATION,
            resettable=True,
            meaning=("arret sur defaut du bus de terrain : verifier le reseau, puis acquitter."),
        ),
        DriveFault.UNKNOWN: FaultSpec(
            mnemonic="?",
            category=FaultCategory.UNIDENTIFIED,
            resettable=False,
            meaning=(
                "code de defaut non reconnu : lire le code affiche sur le variateur et le "
                "rechercher dans son manuel. Ne pas rearmer depuis la console."
            ),
        ),
    }
)
"""Manual facts and reset policy per fault. Total over ``DriveFault``; a test proves it."""


LFT_FAULT_CODES: Final[Mapping[RawRegister, DriveFault]] = MappingProxyType(
    {
        RawRegister(0): DriveFault.NO_FAULT_STORED,
        RawRegister(1): DriveFault.INTERNAL,
        RawRegister(2): DriveFault.CONTROL_EEPROM,
        RawRegister(3): DriveFault.INCORRECT_CONFIG,
        RawRegister(4): DriveFault.INVALID_CONFIG,
        RawRegister(5): DriveFault.MODBUS_COMM_LOSS,
        RawRegister(6): DriveFault.INTERNAL_COM_LINK,
        RawRegister(7): DriveFault.COM_NETWORK,
        RawRegister(8): DriveFault.EXTERNAL_FAULT_INPUT,
        RawRegister(9): DriveFault.OVERCURRENT,
        RawRegister(10): DriveFault.PRECHARGE,
        RawRegister(11): DriveFault.SPEED_FEEDBACK_LOSS,
        RawRegister(16): DriveFault.DRIVE_OVERHEAT,
        RawRegister(17): DriveFault.MOTOR_OVERLOAD,
        RawRegister(18): DriveFault.DC_BUS_OVERVOLTAGE,
        RawRegister(19): DriveFault.MAINS_OVERVOLTAGE,
        RawRegister(20): DriveFault.OUTPUT_PHASE_LOSS,
        RawRegister(21): DriveFault.INPUT_PHASE_LOSS,
        RawRegister(22): DriveFault.UNDERVOLTAGE,
        RawRegister(23): DriveFault.MOTOR_SHORT_CIRCUIT,
        RawRegister(24): DriveFault.OVERSPEED,
        RawRegister(25): DriveFault.AUTO_TUNING,
        RawRegister(26): DriveFault.RATING_ERROR,
        RawRegister(27): DriveFault.POWER_CALIBRATION,
        RawRegister(28): DriveFault.INTERNAL_SERIAL_LINK,
        RawRegister(29): DriveFault.INTERNAL_MFG_AREA,
        RawRegister(30): DriveFault.POWER_EEPROM,
        RawRegister(31): DriveFault.IMPEDANT_SHORT_CIRCUIT,
        RawRegister(32): DriveFault.GROUND_SHORT_CIRCUIT,
        RawRegister(33): DriveFault.THREE_PHASE_LOSS,
        RawRegister(34): DriveFault.CANOPEN_COMM_LOSS,
        RawRegister(35): DriveFault.BRAKE_CONTROL,
        RawRegister(38): DriveFault.EXTERNAL_FAULT_COM,
        RawRegister(41): DriveFault.BRAKE_FEEDBACK,
        RawRegister(42): DriveFault.PC_COMM_LOSS,
        RawRegister(43): DriveFault.ENCODER_COUPLING,
        RawRegister(44): DriveFault.TORQUE_CURRENT_LIMIT,
        RawRegister(45): DriveFault.HMI_COMM_LOSS,
        RawRegister(46): DriveFault.POWER_REMOVAL,
        RawRegister(49): DriveFault.PTC_PROBE,
        RawRegister(50): DriveFault.PTC_OVERHEAT,
        RawRegister(51): DriveFault.INTERNAL_CURRENT_MEASURE,
        RawRegister(52): DriveFault.INTERNAL_MAINS_CIRCUIT,
        RawRegister(53): DriveFault.INTERNAL_THERMAL_SENSOR,
        RawRegister(54): DriveFault.IGBT_OVERHEAT,
        RawRegister(55): DriveFault.IGBT_SHORT_CIRCUIT,
        RawRegister(56): DriveFault.MOTOR_SHORT_CIRCUIT_2,
        RawRegister(57): DriveFault.TORQUE_TIMEOUT,
        RawRegister(58): DriveFault.OUTPUT_CONTACTOR_STUCK,
        RawRegister(59): DriveFault.OUTPUT_CONTACTOR_OPEN,
        RawRegister(61): DriveFault.AI2_INPUT,
        RawRegister(64): DriveFault.INPUT_CONTACTOR,
        RawRegister(66): DriveFault.DIFFERENTIAL_CURRENT,
        RawRegister(67): DriveFault.IGBT_DESATURATION,
        RawRegister(68): DriveFault.INTERNAL_OPTION,
        RawRegister(69): DriveFault.INTERNAL_CPU,
        RawRegister(71): DriveFault.AI3_CURRENT_LOSS,
        RawRegister(73): DriveFault.CARDS_PAIRING,
        RawRegister(76): DriveFault.LOAD_FAULT,
        RawRegister(77): DriveFault.BAD_CONFIG_TRANSFER,
        RawRegister(99): DriveFault.CHANNEL_SWITCH,
        RawRegister(100): DriveFault.PROCESS_UNDERLOAD,
        RawRegister(101): DriveFault.PROCESS_OVERLOAD,
        RawRegister(105): DriveFault.ANGLE_ERROR,
        RawRegister(107): DriveFault.SAFETY_FUNCTION,
        RawRegister(108): DriveFault.FIELDBUS,
        RawRegister(109): DriveFault.FIELDBUS_STOP,
    }
)
"""LFT register value -> fault: the complete Schneider enumeration (see :class:`DriveFault`).

Taken from the manufacturer's parameter file, not from memory; the bench
observation "LFT = 5 after a Modbus loss" agrees with it (SLF1 = 5), where the
provisional table this replaced had SLF at 19 (really OSF, mains overvoltage).
"""


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

    ``codes`` is a parameter so commissioning can extend the table (a newer
    firmware's codes) without a code change.
    """
    fault = codes.get(raw)
    if fault is None:
        return FaultReport(
            fault=DriveFault.UNKNOWN,
            raw_code=raw,
            message=f"code de defaut inconnu {raw} (0x{raw:04X}) : {DriveFault.UNKNOWN.meaning}",
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

    fault_code: RawRegister | None = None
    """The raw LFT value behind :attr:`fault`, or ``None`` when none was read.

    Kept so the operator can compare the number with the drive's own display
    (and so a code newer than the table still travels): LFT = 5 was seen on the
    bench right after a Modbus loss, which the complete table now names SLF1.
    """

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

# --- The commissioned limits, READ ONLY -------------------------------------
#
# Measured on the bench (offset 0, address 248): tFr@3103 = 600, HSP@3104 = 500,
# LSP@3105 = 0, ACC@9001 = 30, dEC@9002 = 30, matching the keypad. This code
# never writes any of them: they are the ceilings that survive a software bug,
# and a ceiling the software can move is not one.

TFR_LOGICAL: Final[RegisterAddress] = RegisterAddress(3103)
"""tFr, max output frequency, read, 0.1 Hz per count."""

HSP_LOGICAL: Final[RegisterAddress] = RegisterAddress(3104)
"""HSP, high speed (the reference ceiling), read, 0.1 Hz per count."""

LSP_LOGICAL: Final[RegisterAddress] = RegisterAddress(3105)
"""LSP, low speed (the reference floor), read, 0.1 Hz per count."""

ACC_LOGICAL: Final[RegisterAddress] = RegisterAddress(9001)
"""ACC, acceleration ramp time, read, 0.1 s per count."""

DEC_LOGICAL: Final[RegisterAddress] = RegisterAddress(9002)
"""dEC, deceleration ramp time, read, 0.1 s per count."""

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

    @property
    def tfr(self) -> RegisterAddress:
        """tFr, read only: max output frequency."""
        return self.resolve(TFR_LOGICAL)

    @property
    def hsp(self) -> RegisterAddress:
        """HSP, read only: high speed."""
        return self.resolve(HSP_LOGICAL)

    @property
    def lsp(self) -> RegisterAddress:
        """LSP, read only: low speed."""
        return self.resolve(LSP_LOGICAL)

    @property
    def acc(self) -> RegisterAddress:
        """ACC, read only: acceleration ramp time."""
        return self.resolve(ACC_LOGICAL)

    @property
    def dec(self) -> RegisterAddress:
        """dEC, read only: deceleration ramp time."""
        return self.resolve(DEC_LOGICAL)


# =========================================================================
# The commissioned limits, parsed and judged
# =========================================================================

TENTH_HERTZ_PER_COUNT: Final[float] = 0.1
"""tFr, HSP and LSP are in 0.1 Hz units: 500 is 50.0 Hz."""

TENTH_SECONDS_PER_COUNT: Final[float] = 0.1
"""ACC and dEC are in 0.1 s units (the drive's default ``Inr`` = 0.1): 30 is 3.0 s."""


def decode_tenth_hertz(raw: RawRegister) -> Hertz:
    """A 0.1 Hz register as Hertz. Divided rather than multiplied so 500 is exactly 50.0."""
    return Hertz(raw / 10)


def decode_tenth_seconds(raw: RawRegister) -> Seconds:
    """A 0.1 s register as Seconds. Divided so 30 is exactly 3.0."""
    return Seconds(raw / 10)


@unique
class DriveParameter(Enum):
    """A commissioned drive parameter this system depends on, by keypad mnemonic.

    ``value`` is the mnemonic exactly as the ATV320 display spells it, so a
    refusal names the thing the operator will look for on the keypad.
    """

    TFR = "tFr"
    HSP = "HSP"
    LSP = "LSP"
    ACC = "ACC"
    DEC = "dEC"
    TTO = "ttO"
    SLL = "SLL"


@dataclass(frozen=True, slots=True)
class DriveLimits:
    """The drive's own commissioned limits, read back and parsed into units.

    Read, never written, and read on every arming: these are the limits that
    hold when this software is wrong, so the software must not be armed on a
    belief about them that nobody checked today.
    """

    max_frequency: Hertz
    """tFr: the drive will never output above this."""

    high_speed: Hertz
    """HSP: the reference ceiling. The last line against a software speed bug."""

    low_speed: Hertz
    """LSP: the reference floor. Must be 0, see :class:`LowSpeedNotZero`."""

    acceleration: Seconds
    """ACC: ramp time from 0 to nominal frequency."""

    deceleration: Seconds
    """dEC: ramp time from nominal frequency to 0. Every stop in this system is this ramp."""

    def describe(self) -> str:
        """One line for the arming log, in the keypad's own names and units."""
        return (
            f"tFr={self.max_frequency:.1f} Hz, HSP={self.high_speed:.1f} Hz, "
            f"LSP={self.low_speed:.1f} Hz, ACC={self.acceleration:.1f} s, "
            f"dEC={self.deceleration:.1f} s"
        )


def decode_limits(
    *,
    tfr: RawRegister,
    hsp: RawRegister,
    lsp: RawRegister,
    acc: RawRegister,
    dec: RawRegister,
) -> DriveLimits:
    """Parse the five raw registers. Keyword-only: five same-typed values in a row
    is exactly the call where two get swapped."""
    return DriveLimits(
        max_frequency=decode_tenth_hertz(tfr),
        high_speed=decode_tenth_hertz(hsp),
        low_speed=decode_tenth_hertz(lsp),
        acceleration=decode_tenth_seconds(acc),
        deceleration=decode_tenth_seconds(dec),
    )


DEFAULT_MAX_MOTOR_HZ: Final[Hertz] = Hertz(50.0)
"""The ceiling HSP must not exceed for this software to arm.

50.0 Hz is the motor's nameplate (1380 rpm) and what the owner keeps HSP at on
the bench while the motor is UNCOUPLED from the arm.

**This MUST be lowered before the arm is coupled.** At 50 Hz the output shaft
turns 1380 / 49.79 = 27.7 rpm; with a person in the machine the HSP ceiling has
to match the highest speed any approved profile may reach, so that a software
bug commanding more is clamped by the drive and not by luck. Lower HSP on the
keypad first, then this, never the other way round.
"""


@dataclass(frozen=True, slots=True)
class LowSpeedNotZero:
    """LSP is above 0 Hz, so a zero reference does NOT stop the motor.

    With LSP > 0 the drive clamps every reference up to LSP: LFRD = 0 - the
    emergency path, the close sequence, a safety descent - holds the motor at
    LSP instead of ramping it to rest. Every stop in this system assumes the
    opposite.
    """

    low_speed: Hertz

    @property
    def parameter(self) -> DriveParameter:
        return DriveParameter.LSP


@dataclass(frozen=True, slots=True)
class HighSpeedAboveMaxFrequency:
    """HSP is above tFr: the commissioning is incoherent and HSP is not the real ceiling."""

    high_speed: Hertz
    max_frequency: Hertz

    @property
    def parameter(self) -> DriveParameter:
        return DriveParameter.HSP


@dataclass(frozen=True, slots=True)
class HighSpeedAboveCeiling:
    """HSP is above the ceiling this installation was configured to accept."""

    high_speed: Hertz
    ceiling: Hertz

    @property
    def parameter(self) -> DriveParameter:
        return DriveParameter.HSP


type LimitViolation = LowSpeedNotZero | HighSpeedAboveMaxFrequency | HighSpeedAboveCeiling
"""Every reason :func:`check_limits` refuses. Closed; match it nested."""


def check_limits(limits: DriveLimits, ceiling: Hertz) -> Result[DriveLimits, LimitViolation]:
    """Refuse drive limits this software cannot arm on. Pure.

    In order of how badly each one breaks an assumption the rest of the system
    makes: a zero reference that does not stop the motor, a ceiling that is not
    one, and a ceiling above what this installation accepts. The comparisons
    are exact on values that are whole tenths, so HSP = 50.0 Hz passes a
    50.0 Hz ceiling and 50.1 Hz does not.
    """
    if limits.low_speed != 0.0:
        return Err(LowSpeedNotZero(low_speed=limits.low_speed))
    if limits.high_speed > limits.max_frequency:
        return Err(
            HighSpeedAboveMaxFrequency(
                high_speed=limits.high_speed, max_frequency=limits.max_frequency
            )
        )
    if limits.high_speed > ceiling:
        return Err(HighSpeedAboveCeiling(high_speed=limits.high_speed, ceiling=ceiling))
    return Ok(limits)


def describe_violation(violation: LimitViolation) -> str:
    """One operator-facing sentence per refusal, naming the keypad parameter."""
    detail: str
    match violation:
        case LowSpeedNotZero(low_speed):
            detail = (
                f"LSP is {low_speed:.1f} Hz, not 0: a zero speed reference would hold the "
                "motor at LSP instead of stopping it. Set LSP = 0 on the keypad."
            )
        case HighSpeedAboveMaxFrequency(high_speed, max_frequency):
            detail = (
                f"HSP ({high_speed:.1f} Hz) is above tFr ({max_frequency:.1f} Hz), so HSP is "
                "not the drive's real speed ceiling. Fix the commissioning on the keypad."
            )
        case HighSpeedAboveCeiling(high_speed, ceiling):
            detail = (
                f"HSP is {high_speed:.1f} Hz, above the {ceiling:.1f} Hz this installation "
                "accepts. Lower HSP on the keypad; do not raise the ceiling to match."
            )
        case _ as unreachable:
            assert_never(unreachable)
    return detail


@dataclass(frozen=True, slots=True)
class UnverifiedParameter:
    """A parameter the safety argument relies on that this code cannot read.

    Named rather than guessed: a Modbus address nobody has verified for this
    drive could read a neighbouring parameter and report it as this one, which
    is worse than admitting the gap.
    """

    parameter: DriveParameter
    why_it_matters: str

    def describe(self) -> str:
        return (
            f"{self.parameter.value}: not verified over Modbus - check on the keypad "
            f"({self.why_it_matters})"
        )


# TODO(commissioning): read ttO and SLL over Modbus once their logical
# addresses are verified against the ATV320 communication parameter list AND
# read back on the bench (compare with the keypad). Until then they are listed
# in every arming log instead of being read from an unverified address.
UNVERIFIED_PARAMETERS: Final[tuple[UnverifiedParameter, ...]] = (
    UnverifiedParameter(
        parameter=DriveParameter.TTO,
        why_it_matters=(
            "the Modbus timeout is the only watchdog outside this process; it must be "
            "set, and short (the simulator assumes 3 s)"
        ),
    ),
    UnverifiedParameter(
        parameter=DriveParameter.SLL,
        why_it_matters=(
            "the response to a Modbus loss must be a ramp stop, never freewheel or "
            "'ignore'; otherwise a dead link leaves the motor commanded"
        ),
    ),
)
"""Parameters to check on the keypad before every session, until read over Modbus."""


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


@dataclass(frozen=True, slots=True)
class EnableUnconfirmed:
    """The start sequence failed AT the word that energises the output stage.

    This variant exists because of one specific wrong reading. A
    :class:`CommTimeout` returned by the last step of the start sequence reads
    as "the link died", and a caller that treats it that way is wrong in the
    only direction that matters: ``ENABLE_OPERATION`` may already have landed.
    A Modbus request whose reply was lost was still transmitted, so the output
    stage may be energised right now - and LFRD may still hold a setpoint from
    an earlier session, which means "energised" can mean "turning".

    So this says what is actually known: **the output state is UNKNOWN and
    possibly enabled.** ``reference_zeroed`` and ``run_command_removed`` report
    whether the rollback the driver attempts before returning was
    acknowledged. Both ``False`` means nothing is known to have undone the
    enable, and the drive's own ``ttO`` timeout is the only stop left.

    ``detail`` carries the underlying failure as prose rather than as a nested
    error value, for the same reason :class:`BadResponse` does: the decision is
    taken on the variant and on the two booleans, and nesting the union inside
    one of its own members would make ``DriveError`` recursive for a field
    nothing branches on.
    """

    detail: str
    reference_zeroed: bool
    run_command_removed: bool


@dataclass(frozen=True, slots=True)
class StopUnconfirmed:
    """The shaft could not be shown to have stopped, so the run command stays.

    Returned by a close that zeroed the speed reference, read RFRD until its
    budget ran out and still saw the shaft turning. Removing the run command at
    that point would be CiA402 transition 8 on a moving centrifuge - the output
    stage dropped, minutes of uncontrolled coast-down - so the drive is
    deliberately left in OPERATION_ENABLED with a zero reference, where its own
    ``ttO`` timeout ramps it down. That is the better of the two available
    endings, and this variant is how the caller is told which one it got.

    Carries the measured wait and the last speed actually read, because "it did
    not stop" is unactionable whereas "still 420 rpm after 20 s" names a
    deceleration ramp that is not what the commissioning notes claim.
    """

    waited: Seconds
    last_output_rpm: MotorRpm
    detail: str


type DriveError = (
    CommTimeout
    | BadResponse
    | UnexpectedState
    | DriveFaulted
    | OutOfRange
    | EnableUnconfirmed
    | StopUnconfirmed
)
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

Two of the seven are not transport failures at all. ``EnableUnconfirmed`` and
``StopUnconfirmed`` say "the output state is unknown and the motor may be
turning", which is the fact a caller has to act on and the fact the five
transport variants cannot express: a ``CommTimeout`` from the middle of a start
sequence reads as "the link died" and hides that the motor is now enabled.
"""


# =========================================================================
# What an emergency attempt achieved
# =========================================================================


@unique
class EmergencyStopOutcome(Enum):
    """What one :meth:`DriveBackend.emergency_disable_blocking` call achieved.

    A return value rather than ``None``, because of who calls it: ``atexit``,
    an OS signal handler, an ``except`` branch. Those are the places with no
    way to ask a follow-up question, and the places where escalating - cut the
    mains, tell the operator, refuse to exit quietly - is the only remaining
    option. "The attempt was made" and "no frame ever reached the wire" demand
    different responses and must not look alike to the caller.

    None of these means the machine has stopped. With STO jumpered there is no
    independent torque removal and the ramp takes seconds; every member below
    is a statement about the attempt, never about the shaft.
    """

    ACKNOWLEDGED = auto()
    # The drive answered the zero-reference write with a well-formed ack, so
    # the reference is 0 and the drive is decelerating down its own ramp. The
    # strongest thing this call can ever report.

    SENT_UNCONFIRMED = auto()
    # A frame went out and no usable answer came back. It may well have landed
    # - a Modbus request whose reply is lost was still transmitted - but
    # nothing here can prove it. "Probably ramping, possibly not."

    NOTHING_SENT = auto()
    # No frame reached the wire at all, so nothing was asked of the drive: the
    # motor is still commanded at whatever setpoint it held. This is the member
    # that must escalate, and the one that used to be indistinguishable from
    # success because this method returned nothing.
