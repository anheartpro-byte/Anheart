"""PyUSB deterministic resource disposal used at the FTDI ownership boundary."""

from usb.core import Device

def dispose_resources(device: Device) -> None: ...
