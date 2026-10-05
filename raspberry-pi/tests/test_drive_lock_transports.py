"""Hardware-free adapter lifecycle regressions for drive ownership."""

from __future__ import annotations

import pytest

from src.clock import ManualClock
from src.motor.atv320 import SerialSettings, serial_master
from src.motor.drive_process_lock import DriveLease, DriveOwnershipError
from src.motor.ftdi_link import ConfigurableFtdi, open_ftdi_port, open_schneider_device
from src.result import Err
from src.units import Seconds
from tests.drive_lock_worker import SyntheticSerial
from tests.test_ftdi_link import FRAME, ConfigurableChip, FakeChip, Sleeps


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
    finally:
        port.close()
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
