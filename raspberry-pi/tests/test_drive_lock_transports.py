"""Hardware-free adapter lifecycle regressions for drive ownership.

Also the synthetic serial handle and the helpers that the other drive
ownership tests and the subprocess worker share: this module imports none of
them back.
"""

from __future__ import annotations

import gc
import logging
import subprocess
import sys
from pathlib import Path
from typing import Literal, assert_never

import pytest

from src.clock import ManualClock
from src.motor.atv320 import ModbusMaster, SerialSettings, serial_master
from src.motor.drive_process_lock import DriveLease, DriveOwnershipError, retry_failed_drive_closes
from src.motor.ftdi_link import ConfigurableFtdi, open_ftdi_port, open_schneider_device
from src.result import Err
from src.units import Seconds
from tests.test_ftdi_link import FRAME, ConfigurableChip, FakeChip, Sleeps


class SyntheticSerial:
    def __init__(self, *, fail_setup: bool = False, fail_close: bool = False) -> None:
        self.fail_setup: bool = fail_setup
        self.fail_close: bool = fail_close
        self.closed: bool = False
        self.successful_closes: int = 0

    @property
    def inter_byte_timeout(self) -> float | None:
        return None

    @inter_byte_timeout.setter
    def inter_byte_timeout(self, value: float | None) -> None:
        del value
        if self.fail_setup:
            raise OSError("synthetic configuration failed with a live handle")

    def close(self) -> None:
        if self.fail_close:
            raise OSError("synthetic close failed; handle remains live")
        if not self.closed:
            self.successful_closes += 1
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


def broken_open_master(
    backend: Literal["serial", "ftdi"], patch: pytest.MonkeyPatch
) -> tuple[ModbusMaster, SyntheticSerial, list[str]]:
    handle = SyntheticSerial(fail_setup=True, fail_close=True)
    opens: list[str] = []

    def open_serial(url: str, **_settings: float | str) -> SyntheticSerial:
        opens.append(url)
        handle.closed = False
        return handle

    def open_ftdi(url: str) -> ConfigurableFtdi:
        opens.append(url)
        handle.closed = False
        raw = FakeChip()
        patch.setattr(raw, "close", handle.close)
        chip = ConfigurableChip(raw)
        if handle.fail_setup:
            chip.fail_on = "set_baudrate"
        return chip

    match backend:
        case "serial":
            patch.setattr("serial.serial_for_url", open_serial)
            return serial_master(SerialSettings(port="COM3"), ManualClock()), handle, opens
        case "ftdi":
            master = serial_master(
                SerialSettings(port="ftdi://schneider:rs485/1"),
                ManualClock(),
                open_device=open_ftdi,
            )
            return master, handle, opens
    raise assert_never(backend)


def refused_open(master: ModbusMaster) -> None:
    try:
        opened = master.connect()
    except OSError:
        opened = False
    assert not opened


def hold_failed_master(
    backend: Literal["serial", "ftdi"], patch: pytest.MonkeyPatch, after_open: bool
) -> int:
    master, handle, opens = broken_open_master(backend, patch)
    try:
        if after_open:
            handle.fail_setup = False
            handle.fail_close = False
            assert master.connect()
            handle.fail_close = True
            with pytest.raises(OSError, match="synthetic close failed"):
                master.close()
        else:
            refused_open(master)
        assert not handle.is_closed()
        assert len(opens) == 1
        print("OPEN", flush=True)  # noqa: T201 - live-handle readiness handshake
        sys.stdin.read(1)
    finally:
        handle.fail_close = False
        master.close()
    return 0


def run_contender(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed synthetic fixture
        [
            sys.executable,
            str(Path(__file__).with_name("drive_lock_worker.py")),
            str(root),
            "attempt",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("backend", ["serial", "ftdi"])
def test_failed_open_and_close_keeps_second_process_out_until_cleanup(
    backend: Literal["serial", "ftdi"], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    master, handle, opens = broken_open_master(backend, monkeypatch)
    try:
        refused_open(master)
        assert not handle.is_closed()
        contender = run_contender(tmp_path)
        assert contender.returncode == 3, contender.stdout + contender.stderr
        assert not (tmp_path / "attempt-opened").exists()
        refused_open(master)
        assert len(opens) == 1
        handle.fail_close = False
        master.close()
        assert handle.is_closed()
        acquired = run_contender(tmp_path)
        assert acquired.returncode == 0, acquired.stderr
    finally:
        handle.fail_close = False
        master.close()
        handle.close()


@pytest.mark.parametrize("backend", ["serial", "ftdi"])
def test_failed_cleanup_survives_dropped_client_until_explicit_retry(
    backend: Literal["serial", "ftdi"],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A retained pytest exception traceback must not keep the lease alive for this test.
    caplog.set_level(logging.CRITICAL, logger="src.motor.atv320")
    master, handle, _ = broken_open_master(backend, monkeypatch)
    try:
        refused_open(master)
        del master
        gc.collect()
        contender = run_contender(tmp_path)
        assert contender.returncode == 3, contender.stdout + contender.stderr
        assert not (tmp_path / "attempt-opened").exists()
        handle.fail_close = False
        retry_failed_drive_closes()
        assert handle.is_closed()
        assert run_contender(tmp_path).returncode == 0
    finally:
        handle.fail_close = False
        retry_failed_drive_closes()


@pytest.mark.parametrize("backend", ["serial", "ftdi"])
def test_cleanup_retry_precedes_reopening_failed_transport(
    backend: Literal["serial", "ftdi"], monkeypatch: pytest.MonkeyPatch
) -> None:
    master, handle, opens = broken_open_master(backend, monkeypatch)
    try:
        refused_open(master)
        handle.fail_setup = False
        handle.fail_close = False
        assert master.connect()
        assert len(opens) == 2
        assert handle.successful_closes == 1
        assert not handle.is_closed()
    finally:
        handle.fail_close = False
        master.close()


@pytest.mark.parametrize("failure", ["open", "configuration", "port"])
def test_ftdi_failed_open_releases_ownership(failure: str) -> None:
    raw = FakeChip()
    chip = ConfigurableChip(raw)
    clock = ManualClock()

    def create(_url: str) -> ConfigurableFtdi:
        if failure == "open":
            raise OSError("synthetic device open failure")
        if failure == "configuration":
            chip.fail_on = "set_baudrate"
        return chip

    with pytest.raises((OSError, ValueError)):
        open_ftdi_port(
            "ftdi://schneider:rs485/1",
            FRAME,
            timeout=Seconds(-1 if failure == "port" else 0.1),
            clock=clock,
            create=create,
            sleep=Sleeps(clock),
        )
    assert raw.close_calls == (0 if failure == "open" else 1)
    lease = DriveLease.claim()
    lease.close()


def test_raw_ftdi_opener_refuses_an_expired_lease_before_usb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_usb() -> None:
        pytest.fail("expired lease reached USB setup")

    monkeypatch.setattr("src.motor.ftdi_link.register_schneider_cable", forbid_usb)
    lease = DriveLease.claim()
    lease.close()
    with pytest.raises(DriveOwnershipError):
        open_schneider_device("ftdi://schneider:rs485/1", lease)


def test_ftdi_master_preserves_the_ownership_refusal() -> None:
    opened: list[str] = []

    def create(url: str) -> ConfigurableFtdi:
        opened.append(url)
        return ConfigurableChip(FakeChip())

    master = serial_master(
        SerialSettings(port="ftdi://schneider:rs485/1"), ManualClock(), open_device=create
    )
    lease = DriveLease.claim()
    try:
        with pytest.raises(DriveOwnershipError):
            master.connect()
        assert opened == []
    finally:
        master.close()
        lease.close()


def test_ftdi_close_failure_retains_the_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = FakeChip()
    clock = ManualClock()
    port = open_ftdi_port(
        "ftdi://schneider:rs485/1",
        FRAME,
        timeout=Seconds(0.1),
        clock=clock,
        create=lambda _: ConfigurableChip(raw),
        sleep=Sleeps(clock),
    )

    def fail_close() -> None:
        raise OSError("synthetic close failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(raw, "close", fail_close)
            with pytest.raises(OSError, match="synthetic"):
                port.close()
        assert isinstance(DriveLease.acquire(), Err)
        with pytest.raises(DriveOwnershipError):
            port.write(b"refused")
    finally:
        port.close()
    with pytest.raises(DriveOwnershipError):
        port.read(1)
    lease = DriveLease.claim()
    lease.close()


def test_serial_client_releases_ownership_on_close_and_reacquires_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def create(
        port: str,
        *,
        timeout: float,
        bytesize: int,
        stopbits: int,
        baudrate: int,
        parity: str,
        exclusive: bool,
    ) -> SyntheticSerial:
        del timeout, bytesize, stopbits, baudrate, parity, exclusive
        opened.append(port)
        return SyntheticSerial()

    monkeypatch.setattr("serial.serial_for_url", create)
    master = serial_master(SerialSettings(port="COM3"), ManualClock())
    assert master.connect()
    assert master.connect()
    assert opened == ["COM3"]
    assert isinstance(DriveLease.acquire(), Err)
    master.close()
    competitor = DriveLease.claim()
    try:
        with pytest.raises(DriveOwnershipError):
            master.connect()
        assert opened == ["COM3"]
    finally:
        competitor.close()
    assert master.connect()
    assert opened == ["COM3", "COM3"]
    master.close()
