"""Hand-written types for ``pyftdi.usbtools`` (pinned ``>=0.57,<0.58``).

Only the lifecycle members ``src/motor/ftdi_link.py`` touches. No module-level
``__getattr__``: an unstubbed member must fail the build. Read off the
installed source (``pyftdi/usbtools.py``, 0.57.2): ``flush_cache`` clears the
per-vendor/product enumeration cache that ``Ftdi.create_from_url`` otherwise
reuses forever.
"""

from threading import RLock
from typing import ClassVar

from usb.core import Device

class UsbToolsError(Exception): ...

class UsbDeviceDescriptor:
    def __init__(
        self,
        vid: int,
        pid: int,
        bus: int | None,
        address: int | None,
        sn: str | None,
        index: int | None,
        description: str | None,
    ) -> None: ...

class UsbTools:
    Lock: ClassVar[RLock]
    UsbDevices: ClassVar[dict[tuple[int, int], set[Device]]]
    @classmethod
    def flush_cache(cls) -> None: ...
    @classmethod
    def release_device(cls, usb_dev: Device) -> None: ...
