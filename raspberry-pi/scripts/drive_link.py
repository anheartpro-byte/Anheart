"""The drive link as the operator scripts open it: settings from .env or the CLI.

Shared by ``bench_console.py`` and ``bench_comm_latency.py`` so the two tools
and the application cannot disagree about how the ATV320 is reached. Nothing
here talks Modbus or opens a port: it only turns ``MOTOR_PORT`` /
``MOTOR_SLAVE_ID`` / ... (or the matching flags) into the project's own
:class:`~src.motor.atv320.SerialSettings` and :class:`~src.motor.drive.
RegisterMap`, and builds the project's own :class:`~src.motor.atv320.
ATV320Drive` on :func:`~src.motor.atv320.serial_master`.

That last point is the reason this file exists. ``serial_master`` is the one
place a pymodbus client is built, and for ``ftdi://schneider:rs485/1`` it
builds the buffered FTDI transport (``src/motor/ftdi_link.py``) whose
``in_waiting`` really works - tens of milliseconds per register instead of the
2 s every read cost through pyftdi's own port. A script that built its own
``ModbusSerialClient`` would silently get the slow port back, and with
``retries=0`` would get no replies at all (pymodbus 3.7.4 never reads a reply
body then); :class:`SerialSettings` refuses that.

Imported as ``from drive_link import ...``: running ``python scripts/x.py``
puts ``scripts/`` first on ``sys.path``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never, cast

# Run from anywhere: put the project root (parent of scripts/) on the path,
# same convention as the other scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from src.clock import Clock
from src.local_config import DriveLink, build_drive_link
from src.motor.atv320 import (
    DEFAULT_BAUDRATE,
    DEFAULT_SLAVE_ADDRESS,
    DEFAULT_TIMEOUT,
    ATV320Drive,
    ModbusMaster,
    serial_master,
)
from src.motor.drive import (
    BadResponse,
    CommTimeout,
    DriveError,
    DriveFaulted,
    EnableUnconfirmed,
    StopUnconfirmed,
    UnexpectedState,
)
from src.motor.ftdi_link import SCHNEIDER_CABLE_URL, Parity
from src.result import Err, Ok
from src.units import OutOfRange, Seconds

# The project's .env (raspberry-pi/.env), then the process environment. Loaded
# at import so every script using this module sees the same MOTOR_* values the
# application would.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return value or default


def add_link_arguments(parser: argparse.ArgumentParser) -> None:
    """The link flags, each defaulting to its .env variable, then to the bench value."""
    parser.add_argument(
        "--port",
        default=_env("MOTOR_PORT", SCHNEIDER_CABLE_URL),
        help="ftdi://schneider:rs485/1 (cable Schneider, macOS), /dev/ttyUSB0, COM3 ... "
        "[env MOTOR_PORT]",
    )
    parser.add_argument(
        "--slave",
        default=_env("MOTOR_SLAVE_ID", str(DEFAULT_SLAVE_ADDRESS)),
        help="adresse Modbus; 248 = point a point Schneider [env MOTOR_SLAVE_ID]",
    )
    parser.add_argument(
        "--baud",
        default=_env("MODBUS_BAUDRATE", str(DEFAULT_BAUDRATE)),
        help="[env MODBUS_BAUDRATE]",
    )
    parser.add_argument(
        "--parity",
        default=_env("MODBUS_PARITY", Parity.EVEN.value),
        choices=[p.value for p in Parity],
        help="[env MODBUS_PARITY]",
    )
    parser.add_argument(
        "--timeout",
        default=_env("MODBUS_TIMEOUT_S", str(DEFAULT_TIMEOUT)),
        help="timeout serie par lecture, en secondes [env MODBUS_TIMEOUT_S]",
    )
    parser.add_argument(
        "--offset",
        default=_env("MOTOR_REG_OFFSET", "0"),
        help="decalage des registres; 0 mesure au banc [env MOTOR_REG_OFFSET]",
    )


def arg_text(args: argparse.Namespace, name: str) -> str:
    """One flag as text. Flags read through these helpers are declared without ``type=``.

    argparse hands back ``Any``; re-typing its dict as ``Mapping[str, object]``
    (a widening, not a lie) and narrowing with ``isinstance`` keeps the scripts
    free of it.
    """
    flags = cast(Mapping[str, object], vars(args))
    value = flags[name]
    if not isinstance(value, str):
        raise TypeError(f"--{name}: expected text, got {type(value).__name__}")
    return value


def arg_int(args: argparse.Namespace, name: str) -> int:
    text = arg_text(args, name)
    try:
        return int(text, 0)
    except ValueError:
        raise ValueError(f"--{name}={text!r} is not an integer") from None


def arg_seconds(args: argparse.Namespace, name: str) -> Seconds:
    text = arg_text(args, name)
    try:
        return Seconds(float(text))
    except ValueError:
        raise ValueError(f"--{name}={text!r} is not a number of seconds") from None


def link_from_args(args: argparse.Namespace) -> DriveLink:
    """Validate the flags into the project's settings. ``ValueError`` names the bad one.

    The validation itself lives in :func:`src.local_config.build_drive_link`, so
    the local console and these scripts parse the link identically.
    """
    built = build_drive_link(
        port=arg_text(args, "port"),
        slave=arg_text(args, "slave"),
        baud=arg_text(args, "baud"),
        parity=arg_text(args, "parity"),
        timeout=arg_text(args, "timeout"),
        offset=arg_text(args, "offset"),
    )
    match built:
        case Ok(link):
            return link
        case Err(problem):
            raise ValueError(f"{problem.key}: {problem.detail}")
    raise assert_never(built)


@dataclass(frozen=True, slots=True)
class BuiltDrive:
    """The driver, plus the transport under it so a read-only tool can let go of the port."""

    drive: ATV320Drive
    master: ModbusMaster

    def release_port(self) -> None:
        """Close the port WITHOUT the stop sequence. For tools that wrote nothing.

        :meth:`ATV320Drive.close` is a stop: it writes LFRD = 0 and the stop
        words. A read-only tool must not, so it closes the transport directly.
        Swallows errors: a port that will not close is still a port we stopped
        using, and the process is about to exit.
        """
        with contextlib.suppress(Exception):
            self.master.close()


def build_drive(link: DriveLink, clock: Clock) -> BuiltDrive:
    """The application's driver on the application's transport. Opens nothing yet."""
    master = serial_master(link.settings, clock)
    return BuiltDrive(
        drive=ATV320Drive(clock, master, link.settings, link.registers), master=master
    )


def describe_error(error: DriveError) -> str:  # noqa: PLR0911 - one return per variant
    """One line for the operator, per variant of the closed union."""
    match error:
        case CommTimeout():
            return f"pas de reponse du variateur (apres {error.after:.3f} s)"
        case BadResponse(detail=detail):
            return f"reponse invalide: {detail}"
        case UnexpectedState(expected=expected, actual=actual):
            return f"etat {actual.name} au lieu de {expected.name}"
        case DriveFaulted(fault=fault, raw_code=raw_code):
            return f"variateur en defaut {fault.name} (LFT={raw_code})"
        case OutOfRange(quantity=quantity, value=value, low=low, high=high):
            return f"valeur impossible: {quantity}={value} hors de [{low}, {high}]"
        case EnableUnconfirmed(detail=detail):
            return f"activation non confirmee: {detail}"
        case StopUnconfirmed(detail=detail):
            return f"arret non confirme: {detail}"
    raise assert_never(error)
