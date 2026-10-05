"""Minimal PyUSB lifecycle surface used by the FTDI ownership boundary."""

class USBError(OSError): ...

class Configuration:
    bConfigurationValue: int  # noqa: N815 - PyUSB descriptor API

class Device:
    bus: int
    address: int
    def get_active_configuration(self) -> Configuration: ...
    def set_configuration(self, configuration: int | None = None) -> None: ...
