"""Actual vendor opening/cleanup, synthetic USB operations, real competing process."""

from __future__ import annotations

import gc
from collections.abc import Iterator
from pathlib import Path
from typing import override

import pytest
from pyftdi.ftdi import Ftdi
from pyftdi.usbtools import UsbDeviceDescriptor, UsbTools
from usb.core import Configuration, Device, USBError

import src.motor.ftdi_link as link
from src.clock import ManualClock
from src.motor.drive_process_lock import retry_failed_drive_closes
from src.units import Seconds
from tests.test_drive_lock_transports import run_contender
from tests.test_ftdi_link import FRAME, Sleeps


class SyntheticConfiguration(Configuration):
    def __init__(self) -> None:
        self.bConfigurationValue = 1


class SyntheticUSB(Device):
    """Mutable USB lifetime oracle, with failures at vendor configuration/disposal."""

    def __init__(self) -> None:
        self.bus = 77
        self.address = 88
        self.calls: int = 0
        self.live: bool = False
        self.fail_close: bool = False
        self.closes: int = 0

    @override
    def get_active_configuration(self) -> Configuration:
        self.calls += 1
        if self.calls > 1:
            raise USBError("synthetic failure after device opened")
        self.live = True
        return SyntheticConfiguration()

    @override
    def set_configuration(self, configuration: int | None = None) -> None:
        self.live = True

    def dispose(self, device: Device) -> None:
        assert device is self
        if self.fail_close:
            raise USBError("synthetic USB disposal failed")
        self.live = False
        self.closes += 1

    def is_live(self) -> bool:
        return self.live


@pytest.fixture
def raw_usb(monkeypatch: pytest.MonkeyPatch) -> Iterator[SyntheticUSB]:
    device = SyntheticUSB()
    descriptor = UsbDeviceDescriptor(0x16DE, 3, None, None, None, None, None)

    def find_devices(_vid: int, _pid: int) -> list[Device]:
        return [device]

    def identifiers(_url: str) -> tuple[UsbDeviceDescriptor, int]:
        return descriptor, 1

    def handle_active(_ftdi: Ftdi) -> bool:
        return False

    def backend(_device: Device) -> None:
        return None

    monkeypatch.setattr(link, "register_schneider_cable", lambda: None)
    monkeypatch.setattr(link, "load_libusb_backend", lambda: None)
    monkeypatch.setattr(UsbTools, "flush_cache", lambda: None)
    monkeypatch.setattr(UsbTools, "_find_devices", find_devices)
    monkeypatch.setattr(UsbTools, "Devices", {})
    monkeypatch.setattr("pyftdi.usbtools.dispose_resources", device.dispose)
    monkeypatch.setattr(Ftdi, "get_identifiers", staticmethod(identifiers))
    # No kernel interface was claimed before the injected configuration failure.
    monkeypatch.setattr(Ftdi, "_is_pyusb_handle_active", handle_active)
    monkeypatch.setattr(Device, "backend", property(backend))
    yield device
    device.fail_close = False
    retry_failed_drive_closes()


def fail_raw_open() -> None:
    clock = ManualClock()
    try:
        link.open_ftdi_port(
            link.SCHNEIDER_CABLE_URL,
            FRAME,
            timeout=Seconds(0.1),
            clock=clock,
            sleep=Sleeps(clock),
        )
    except USBError:
        return
    pytest.fail("raw USB opening must fail")


def test_partial_vendor_open_cleans_usb_before_releasing_lease(
    raw_usb: SyntheticUSB, tmp_path: Path
) -> None:
    # Given: real vendor opening fails after the device has been acquired/cached.
    # When: the caller drops the failure and collects garbage.
    fail_raw_open()
    gc.collect()
    contender = run_contender(tmp_path)
    # Then: USB cleanup completes before another actual interpreter can open.
    assert not raw_usb.is_live()
    assert raw_usb.closes == 1
    assert contender.returncode == 0, contender.stdout + contender.stderr
    assert (tmp_path / "attempt-opened").exists()


def test_failed_raw_cleanup_survives_gc_until_actual_disposal_retry(
    raw_usb: SyntheticUSB, tmp_path: Path
) -> None:
    # Given: the raw vendor failure is followed by USB disposal failure.
    raw_usb.fail_close = True
    # When: no caller, exception or log retains the failed opening.
    fail_raw_open()
    gc.collect()
    contender = run_contender(tmp_path)
    # Then: the live device prevents another process from reaching its opener.
    assert raw_usb.is_live()
    assert contender.returncode == 3, contender.stdout + contender.stderr
    assert not (tmp_path / "attempt-opened").exists()
    with pytest.raises(USBError, match="disposal failed"):
        retry_failed_drive_closes()
    assert run_contender(tmp_path).returncode == 3
    raw_usb.fail_close = False
    retry_failed_drive_closes()
    assert not raw_usb.is_live()
    assert raw_usb.closes == 1
    assert run_contender(tmp_path).returncode == 0
    assert (tmp_path / "attempt-opened").exists()


def test_usb_acquisition_is_owned_before_vendor_stores_its_device(
    raw_usb: SyntheticUSB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_before_assignment(_ftdi: Ftdi, _device: Device, _interface: int = 1) -> None:
        raise USBError("synthetic failure before vendor stores USB device")

    # Given: real get_device succeeds, but vendor dispatch fails before assignment.
    monkeypatch.setattr(Ftdi, "open_from_device", fail_before_assignment)
    raw_usb.fail_close = True
    # When: failed cleanup loses its caller and exception.
    fail_raw_open()
    gc.collect()
    # Then: ownership survives until disposal of that exact acquired device.
    assert raw_usb.is_live()
    assert run_contender(tmp_path).returncode == 3
    assert not (tmp_path / "attempt-opened").exists()
    raw_usb.fail_close = False
    retry_failed_drive_closes()
    assert not raw_usb.is_live()
    assert raw_usb.closes == 1
    assert run_contender(tmp_path).returncode == 0
