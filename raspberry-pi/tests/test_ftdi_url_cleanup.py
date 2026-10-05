"""Actual USB URL resolution and resource disposal over a synthetic backend."""

from __future__ import annotations

import gc
from collections.abc import Iterator
from pathlib import Path

import pytest
from pyftdi.usbtools import UsbTools, UsbToolsError
from usb.core import USBError
from usb.util import dispose_resources

import src.motor.ftdi_link as link
from src.clock import ManualClock
from src.motor.drive_process_lock import DriveConnection, DriveLease, retry_failed_drive_closes
from src.units import Seconds
from tests.ftdi_descriptor_backend import DescriptorBackend
from tests.test_drive_process_lock import run_contender
from tests.test_ftdi_link import FRAME, Sleeps


@pytest.fixture
def descriptor_backend(monkeypatch: pytest.MonkeyPatch) -> Iterator[DescriptorBackend]:
    backend = DescriptorBackend()
    monkeypatch.setattr(link, "load_libusb_backend", lambda: None)
    monkeypatch.setattr(UsbTools, "_load_backend", lambda: backend)
    monkeypatch.setattr(UsbTools, "Devices", {})
    monkeypatch.setattr(UsbTools, "UsbDevices", {})
    yield backend
    backend.fail_close = False
    backend.fail_after_closes = None
    retry_failed_drive_closes()
    for devices in UsbTools.UsbDevices.values():
        for device in devices:
            dispose_resources(device)


def fail_url_open(url: str) -> None:
    clock = ManualClock()
    try:
        link.open_ftdi_port(url, FRAME, timeout=Seconds(0.1), clock=clock, sleep=Sleeps(clock))
    except (ValueError, OSError, UsbToolsError):
        return
    pytest.fail("synthetic descriptor lookup must fail")


@pytest.mark.parametrize("descriptor", [0, 1, 2])
def test_descriptor_failure_disposes_before_unlock(
    descriptor_backend: DescriptorBackend, tmp_path: Path, descriptor: int
) -> None:
    # Given: a real control transfer opens the handle before descriptor failure.
    descriptor_backend.fail_descriptor = descriptor
    # When: URL lookup fails and the caller drops the exception.
    fail_url_open(link.SCHNEIDER_CABLE_URL)
    gc.collect()
    contender = run_contender(tmp_path)
    # Then: the handle is disposed before another interpreter reaches its opener.
    assert descriptor_backend.opens == [101]
    assert descriptor_backend.closes == [101]
    assert not descriptor_backend.live
    assert contender.returncode == 0, contender.stdout + contender.stderr
    assert (tmp_path / "attempt-opened").exists()


def test_failed_descriptor_disposal_survives_gc_until_retry(
    descriptor_backend: DescriptorBackend, tmp_path: Path
) -> None:
    # Given: URL lookup and actual backend close both fail after managed_open.
    descriptor_backend.fail_close = True
    # When: no caller or exception retains the failure.
    fail_url_open(link.SCHNEIDER_CABLE_URL)
    gc.collect()
    contender = run_contender(tmp_path)
    # Then: a durable owner refuses contenders until actual disposal succeeds.
    assert descriptor_backend.live == {101}
    assert contender.returncode == 3, contender.stdout + contender.stderr
    assert not (tmp_path / "attempt-opened").exists()
    with pytest.raises(USBError, match="disposal failed"):
        retry_failed_drive_closes()
    assert run_contender(tmp_path).returncode == 3
    descriptor_backend.fail_close = False
    retry_failed_drive_closes()
    assert descriptor_backend.closes == [101]
    assert not descriptor_backend.live
    assert run_contender(tmp_path).returncode == 0


@pytest.mark.parametrize("read", [4, 5])
def test_get_device_descriptor_failure_disposes_before_acquired_callback(
    descriptor_backend: DescriptorBackend, tmp_path: Path, read: int
) -> None:
    descriptor_backend.fail_descriptor = None
    descriptor_backend.fail_read = read
    fail_url_open(link.SCHNEIDER_CABLE_URL)
    assert descriptor_backend.reads == read
    assert descriptor_backend.closes == [101]
    assert not descriptor_backend.live
    assert run_contender(tmp_path).returncode == 0


def test_selected_interface_failure_disposes_selected_and_other_candidates(
    descriptor_backend: DescriptorBackend, tmp_path: Path
) -> None:
    descriptor_backend.devices = (1, 2)
    descriptor_backend.fail_descriptor = None
    fail_url_open("ftdi://schneider:rs485:usb-101/2")
    assert sorted(descriptor_backend.opens) == [101, 102]
    assert sorted(descriptor_backend.closes) == [101, 102]
    assert not descriptor_backend.live
    assert run_contender(tmp_path).returncode == 0


def test_retry_retains_remaining_candidates_after_partial_disposal(
    descriptor_backend: DescriptorBackend, tmp_path: Path
) -> None:
    descriptor_backend.devices = (1, 2)
    descriptor_backend.fail_descriptor = None
    descriptor_backend.fail_after_closes = 1
    fail_url_open(link.SCHNEIDER_CABLE_URL)
    gc.collect()
    assert len(descriptor_backend.closes) == 1
    assert len(descriptor_backend.live) == 1
    assert run_contender(tmp_path).returncode == 3
    assert not (tmp_path / "attempt-opened").exists()
    descriptor_backend.fail_after_closes = None
    retry_failed_drive_closes()
    assert sorted(descriptor_backend.closes) == [101, 102]
    assert not descriptor_backend.live
    assert run_contender(tmp_path).returncode == 0


def test_ambiguous_url_disposes_every_enumerated_handle(
    descriptor_backend: DescriptorBackend, tmp_path: Path
) -> None:
    # Given: two real candidates each need descriptors before URL ambiguity is known.
    descriptor_backend.devices = (1, 2)
    descriptor_backend.fail_descriptor = None
    # When: vendor URL parsing refuses to select an ambiguous cable.
    fail_url_open(link.SCHNEIDER_CABLE_URL)
    # Then: both enumeration-only handles are physically closed.
    assert sorted(descriptor_backend.opens) == [101, 102]
    assert sorted(descriptor_backend.closes) == [101, 102]
    assert not descriptor_backend.live
    assert run_contender(tmp_path).returncode == 0


@pytest.mark.parametrize("fail_close", [False, True])
def test_resolved_cable_keeps_only_selected_handle_until_actual_close(
    descriptor_backend: DescriptorBackend, tmp_path: Path, fail_close: bool
) -> None:
    descriptor_backend.devices = (1, 2)
    descriptor_backend.fail_descriptor = None
    lease = DriveLease.claim()
    device = link.open_schneider_device("ftdi://schneider:rs485:usb-101/1", lease)
    connection = DriveConnection(lease)
    try:
        assert sorted(descriptor_backend.opens) == [101, 102]
        assert descriptor_backend.live == {101}
        assert descriptor_backend.closes == [102]
        assert run_contender(tmp_path).returncode == 3
        descriptor_backend.fail_close = fail_close
        try:
            connection.close(device.close)
        except USBError:
            assert fail_close
        del connection, device, lease
        gc.collect()
        if fail_close:
            assert descriptor_backend.live == {101}
            assert run_contender(tmp_path).returncode == 3
            assert not (tmp_path / "attempt-opened").exists()
            descriptor_backend.fail_close = False
            retry_failed_drive_closes()
        assert sorted(descriptor_backend.closes) == [101, 102]
        assert not descriptor_backend.live
        assert run_contender(tmp_path).returncode == 0
    finally:
        descriptor_backend.fail_close = False
        retry_failed_drive_closes()
