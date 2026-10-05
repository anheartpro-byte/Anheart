"""Subprocess fixture: a synthetic transport with real process lifetimes."""

from __future__ import annotations

import asyncio
import runpy
import sys
from enum import StrEnum
from pathlib import Path
from typing import assert_never

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import src.motor.drive_process_lock as ownership
from src.clock import ManualClock
from src.local_config import LocalConfig, load_local_config
from src.motor.atv320 import SerialSettings, serial_master
from src.motor.drive import BadResponse
from src.motor.drive_process_lock import DriveOwnershipError
from src.motor.ftdi_link import ConfigurableFtdi, DeviceFactory, open_ftdi_port
from src.result import Err, Ok
from src.units import Seconds
from tests.test_ftdi_link import FRAME, ConfigurableChip, FakeChip, Sleeps


class SyntheticSerial:
    inter_byte_timeout: float | None = None

    def close(self) -> None:
        return


class Backend(StrEnum):
    FTDI = "ftdi"
    SERIAL = "serial"
    WRITE = "write"
    READ = "read"
    PROBE = "probe"
    SCAN = "scan"
    LATENCY = "latency"
    CONSOLE = "console"
    BENCH = "bench"


class Action(StrEnum):
    HOLD = "hold"
    ATTEMPT = "attempt"


def synthetic_config() -> LocalConfig:
    result = load_local_config(
        {
            "MOTOR_BACKEND": "serial",
            "MOTOR_PORT": "COM3",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
            "UI_PORT": "8090",
        }
    )
    assert isinstance(result, Ok)
    return result.value


def run_ftdi(action: Action, create: DeviceFactory) -> int:
    clock = ManualClock()
    port = open_ftdi_port(
        "ftdi://schneider:rs485/1",
        FRAME,
        timeout=Seconds(0.1),
        clock=clock,
        create=create,
        sleep=Sleeps(clock),
    )
    try:
        print("OPEN", flush=True)  # noqa: T201 - parent readiness handshake
        match action:
            case Action.HOLD:
                sys.stdin.read(1)
            case Action.ATTEMPT:
                return 0
            case _ as unreachable:
                assert_never(unreachable)
    finally:
        port.close()
    return 0


def main() -> int:
    root = Path(sys.argv[1])
    action = Action(sys.argv[2])
    patch = pytest.MonkeyPatch()
    patch.setattr(ownership, "LOCK_PATH", root / "drive.lock")
    backend = Backend(sys.argv[3]) if len(sys.argv) > 3 else Backend.FTDI

    def create(_url: str) -> ConfigurableFtdi:
        (root / f"{action}-opened").touch()
        return ConfigurableChip(FakeChip())

    clock = ManualClock()

    def create_serial(
        _url: str,
        *,
        timeout: float,
        bytesize: int,
        stopbits: int,
        baudrate: int,
        parity: str,
        exclusive: bool,
    ) -> SyntheticSerial:
        del timeout, bytesize, stopbits, baudrate, parity, exclusive
        (root / f"{action}-opened").touch()
        return SyntheticSerial()

    patch.setattr("serial.serial_for_url", create_serial)
    match backend:
        case Backend.PROBE | Backend.SCAN | Backend.LATENCY:
            script_names = {
                "probe": "probe_atv320.py",
                "scan": "scan_modbus.py",
                "latency": "bench_comm_latency.py",
            }
            scripts = Path(__file__).resolve().parent.parent / "scripts"
            script = scripts / script_names[backend]
            sys.path.insert(0, str(scripts))
            arguments = {
                Backend.PROBE: ["COM3"],
                Backend.SCAN: ["COM3"],
                Backend.LATENCY: ["--port", "COM3"],
            }
            sys.argv = [str(script), *arguments[backend]]
            runpy.run_path(str(script), run_name="__main__")
            return 0
        case Backend.BENCH:
            scripts = Path(__file__).resolve().parent.parent / "scripts"
            sys.path.insert(0, str(scripts))
            from scripts.bench_console import BusWorker  # noqa: PLC0415 - script search path

            link = synthetic_config().drive_link
            assert link is not None
            worker = BusWorker(link, max_rpm=0)
            worker.shutdown()
            worker.run()
            assert not worker.snap.link_ok
            assert worker.snap.last_error is not None
            print(worker.snap.last_error, file=sys.stderr)  # noqa: T201 - bench UI error
            return 3
        case Backend.CONSOLE:
            from src.local_panel import build_drive  # noqa: PLC0415 - only the console scenario

            side = build_drive(synthetic_config(), clock)
            try:
                result = asyncio.run(side.backend.open())
                assert isinstance(result, Err)
                assert isinstance(result.error, BadResponse)
                print(result.error.detail, file=sys.stderr)  # noqa: T201 - refusal observable
                return 3
            finally:
                side.release()
        case Backend.SERIAL | Backend.WRITE | Backend.READ:
            master = serial_master(SerialSettings(port="COM3"), clock)
            try:
                match backend:
                    case Backend.WRITE:
                        master.write_register(8501, 0, slave=248)
                    case Backend.READ:
                        master.read_holding_registers(3201, count=1, slave=248)
                    case Backend.SERIAL:
                        master.connect()
                    case _ as unreachable:
                        assert_never(unreachable)
            finally:
                master.close()
            return 0
        case Backend.FTDI:
            return run_ftdi(action, create)
        case _ as unreachable:
            assert_never(unreachable)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriveOwnershipError as error:
        print(str(error), file=sys.stderr)  # noqa: T201 - operator refusal observable
        raise SystemExit(3) from error
