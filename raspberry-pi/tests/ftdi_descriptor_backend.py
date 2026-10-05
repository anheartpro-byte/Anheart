"""Synthetic USB backend for actual vendor resource-lifetime regressions."""

from __future__ import annotations

from array import array
from types import SimpleNamespace

from usb.backend import IBackend
from usb.core import USBError


class DescriptorBackend(IBackend):
    """Mutable backend records real PyUSB handle opens and successful disposals."""

    def __init__(self) -> None:
        self.devices: tuple[int, ...] = (1,)
        self.fail_descriptor: int | None = 0
        self.fail_close: bool = False
        self.fail_after_closes: int | None = None
        self.fail_read: int | None = None
        self.reads: int = 0
        self.live: set[int] = set()
        self.opens: list[int] = []
        self.closes: list[int] = []

    def enumerate_devices(self) -> tuple[int, ...]:
        return self.devices

    def get_device_descriptor(self, dev: int) -> SimpleNamespace:
        return SimpleNamespace(
            bLength=18,
            bDescriptorType=1,
            bcdUSB=0x0200,
            bDeviceClass=0,
            bDeviceSubClass=0,
            bDeviceProtocol=0,
            bMaxPacketSize0=64,
            bcdDevice=0x0600,
            iManufacturer=0,
            idVendor=0x16DE,
            idProduct=3,
            iProduct=2,
            iSerialNumber=1,
            bNumConfigurations=1,
            bus=77,
            address=dev,
            port_number=1,
            port_numbers=(1,),
            speed=2,
        )

    def get_configuration_descriptor(self, _dev: int, _configuration: int) -> SimpleNamespace:
        return SimpleNamespace(
            bLength=9,
            bDescriptorType=2,
            wTotalLength=9,
            bNumInterfaces=1,
            bConfigurationValue=1,
            iConfiguration=0,
            bmAttributes=0x80,
            bMaxPower=50,
            extra_descriptors=b"",
        )

    def open_device(self, dev: int) -> int:
        handle = 100 + dev
        self.live.add(handle)
        self.opens.append(handle)
        return handle

    def get_interface_descriptor(
        self, _dev: int, _interface: int, _alternate: int, _configuration: int
    ) -> SimpleNamespace:
        return SimpleNamespace(
            bLength=9,
            bDescriptorType=4,
            bInterfaceNumber=0,
            bAlternateSetting=0,
            bNumEndpoints=2,
            bInterfaceClass=0xFF,
            bInterfaceSubClass=0xFF,
            bInterfaceProtocol=0xFF,
            iInterface=0,
            extra_descriptors=b"",
        )

    def get_endpoint_descriptor(
        self, _dev: int, endpoint: int, _interface: int, _alternate: int, _configuration: int
    ) -> SimpleNamespace:
        return SimpleNamespace(
            bLength=7,
            bDescriptorType=5,
            bEndpointAddress=(0x01, 0x81)[endpoint],
            bmAttributes=2,
            wMaxPacketSize=64,
            bInterval=0,
            bRefresh=0,
            bSynchAddress=0,
            extra_descriptors=b"",
        )

    def is_kernel_driver_active(self, _handle: int, _interface: int) -> bool:
        return False

    def close_device(self, handle: int) -> None:
        if self.fail_close or self.fail_after_closes == len(self.closes):
            raise USBError("synthetic backend disposal failed")
        self.live.remove(handle)
        self.closes.append(handle)

    def ctrl_transfer(
        self,
        handle: int,
        _request_type: int,
        request: int,
        value: int,
        _index: int,
        data: array[int],
        _timeout: int,
    ) -> int:
        assert handle in self.live
        if request != 6:
            return len(data)
        self.reads += 1
        descriptor = value & 0xFF
        if self.fail_descriptor == descriptor or self.fail_read == self.reads:
            raise USBError("synthetic descriptor transfer failed after managed_open")
        payload = b"\x09\x04" if descriptor == 0 else f"usb-{handle}".encode("utf-16-le")
        response = bytes((len(payload) + 2, 3)) + payload
        data[: len(response)] = array("B", response)
        return len(response)

    def get_configuration(self, _handle: int) -> int:
        return 1

    def set_configuration(self, _handle: int, _configuration: int) -> None:
        return None

    def attach_kernel_driver(self, _handle: int, _interface: int) -> None:
        return None
