"""The commands a record names so that a replay can issue them again.

The runtime decides from four things: the clock, what the drive answers, the
ECG, and what people ask of it. The first three are streams of the record. The
fourth is ``events.jsonl``, and it is replayable only if an event says exactly
which entry point was called, with which arguments. So for the three kinds that
are INPUTS of the runtime (:data:`INPUT_KINDS`) the ``detail`` is one command of
this closed vocabulary, written by :func:`encode` and read by :func:`parse`:

    confirm_estop_wiring
    start_programme
    start_manual ceiling_motor_rpm=1380
    manual_target output_rpm=27.0
    stop
    estop
    acknowledge estop_released=true
    fault_reset
    shutdown
    attendant present=false

Who asked is the event's ``actor``; what the runtime answered is not here (a
refusal is its own ``refusal`` event, and the decisions are in ``ticks.csv``).
Anything else found under those three kinds makes the record unreplayable, said
as such: a command the replay silently skipped would be a replay of some other
session. ``docs/enregistrement.md`` carries the same table.

It lives here, next to the writer, because both producers must write the same
words: the simulation harness does, and the console's recorder will.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Final, assert_never

from src.record.schema import EventKind
from src.result import Err, Ok, Result
from src.units import MotorRpm, OutputRpm


@dataclass(frozen=True, slots=True)
class ConfirmEstopWiring:
    """The operator attests that a wired emergency stop exists."""


@dataclass(frozen=True, slots=True)
class StartProgramme:
    """Arm the programme frozen in the manifest's ``profile``."""


@dataclass(frozen=True, slots=True)
class StartManual:
    """Arm a manual session, with the ceiling the console resolved for the occupancy."""

    ceiling: MotorRpm


@dataclass(frozen=True, slots=True)
class SetManualTarget:
    output_rpm: OutputRpm


@dataclass(frozen=True, slots=True)
class Stop:
    """The unhurried end: a stop button, or an end requested off the machine."""


@dataclass(frozen=True, slots=True)
class EmergencyStop:
    """The software emergency stop."""


@dataclass(frozen=True, slots=True)
class Acknowledge:
    estop_released: bool


@dataclass(frozen=True, slots=True)
class FaultReset:
    """An operator's reset of a latched drive fault."""


@dataclass(frozen=True, slots=True)
class Shutdown:
    """The console process leaving (a signal, a failed task, the end of a simulated run)."""


@dataclass(frozen=True, slots=True)
class Attendant:
    """Whether somebody is watching: while ``present``, every tick notes the presence."""

    present: bool


type Command = (
    ConfirmEstopWiring
    | StartProgramme
    | StartManual
    | SetManualTarget
    | Stop
    | EmergencyStop
    | Acknowledge
    | FaultReset
    | Shutdown
    | Attendant
)

INPUT_KINDS: Final[frozenset[EventKind]] = frozenset(
    {EventKind.OPERATOR_ACTION, EventKind.REMOTE_COMMAND, EventKind.VERDICT_ACK}
)
"""The event kinds whose ``detail`` is a command."""

_BOOLEANS: Final = {"true": True, "false": False}


def encode(command: Command) -> str:  # noqa: PLR0911  # one return per command
    """The ``detail`` of the event that records ``command``."""
    match command:
        case ConfirmEstopWiring():
            return "confirm_estop_wiring"
        case StartProgramme():
            return "start_programme"
        case StartManual(ceiling):
            return f"start_manual ceiling_motor_rpm={int(ceiling)}"
        case SetManualTarget(output_rpm):
            return f"manual_target output_rpm={float(output_rpm)!r}"
        case Stop():
            return "stop"
        case EmergencyStop():
            return "estop"
        case Acknowledge(estop_released):
            return f"acknowledge estop_released={str(estop_released).lower()}"
        case FaultReset():
            return "fault_reset"
        case Shutdown():
            return "shutdown"
        case Attendant(present):
            return f"attendant present={str(present).lower()}"
    raise assert_never(command)


def kind_of(command: Command, *, remote: bool = False) -> EventKind:
    """The event kind ``command`` is recorded under."""
    if isinstance(command, Acknowledge):
        return EventKind.VERDICT_ACK
    return EventKind.REMOTE_COMMAND if remote else EventKind.OPERATOR_ACTION


def _argument(words: list[str], name: str) -> Result[str, str]:
    """The value of the single ``name=value`` argument a command takes."""
    if len(words) != 1:
        return Err(f"expected exactly one argument, {name}=...")
    key, separator, value = words[0].partition("=")
    if key != name or not separator:
        return Err(f"expected {name}=...")
    return Ok(value)


def _boolean(words: list[str], name: str) -> Result[bool, str]:
    value = _argument(words, name)
    if isinstance(value, Err):
        return value
    parsed = _BOOLEANS.get(value.value)
    if parsed is None:
        return Err(f"{name} must be true or false")
    return Ok(parsed)


def _number(words: list[str], name: str) -> Result[float, str]:
    value = _argument(words, name)
    if isinstance(value, Err):
        return value
    try:
        parsed = float(value.value)
    except ValueError:
        return Err(f"{name} must be a number")
    if not isfinite(parsed):
        return Err(f"{name} must be finite")
    return Ok(parsed)


def _bare(words: list[str], command: Command) -> Result[Command, str]:
    if words:
        return Err("takes no argument")
    return Ok(command)


def _start_manual(words: list[str]) -> Result[Command, str]:
    ceiling = _number(words, "ceiling_motor_rpm")
    if isinstance(ceiling, Err):
        return ceiling
    if not ceiling.value.is_integer():
        return Err("ceiling_motor_rpm must be a whole number of rpm")
    return Ok(StartManual(MotorRpm(int(ceiling.value))))


def _manual_target(words: list[str]) -> Result[Command, str]:
    rpm = _number(words, "output_rpm")
    if isinstance(rpm, Err):
        return rpm
    return Ok(SetManualTarget(OutputRpm(rpm.value)))


def _acknowledge(words: list[str]) -> Result[Command, str]:
    released = _boolean(words, "estop_released")
    if isinstance(released, Err):
        return released
    return Ok(Acknowledge(released.value))


def _attendant(words: list[str]) -> Result[Command, str]:
    present = _boolean(words, "present")
    if isinstance(present, Err):
        return present
    return Ok(Attendant(present.value))


def parse(detail: str) -> Result[Command, str]:  # noqa: PLR0911  # one return per command
    """The command ``detail`` records, or why it is none. Total: never raises."""
    words = detail.split(" ")
    verb, arguments = words[0], words[1:]
    match verb:
        case "confirm_estop_wiring":
            return _bare(arguments, ConfirmEstopWiring())
        case "start_programme":
            return _bare(arguments, StartProgramme())
        case "start_manual":
            return _start_manual(arguments)
        case "manual_target":
            return _manual_target(arguments)
        case "stop":
            return _bare(arguments, Stop())
        case "estop":
            return _bare(arguments, EmergencyStop())
        case "acknowledge":
            return _acknowledge(arguments)
        case "fault_reset":
            return _bare(arguments, FaultReset())
        case "shutdown":
            return _bare(arguments, Shutdown())
        case "attendant":
            return _attendant(arguments)
        case _:
            pass
    return Err("not a command of the replay vocabulary")
