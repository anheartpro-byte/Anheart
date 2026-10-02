"""Hand-written types for pyobjc's ``IOBluetooth`` (pyobjc-framework-IOBluetooth 12.2.x).

Only the members ``src/bitalino_rfcomm_macos.py`` touches (contract rule 5).
Read off the framework metadata shipped with the binding
(``IOBluetooth/_metadata.py``): the first argument of
``openRFCOMMChannelAsync:withChannelID:delegate:`` is an OUT pointer, so pyobjc
takes ``None`` there and returns ``(IOReturn, channel)``; ``writeSync:length:``
takes the bytes and their length. Every ``int`` returned is an ``IOReturn``,
``0`` (``kIOReturnSuccess``) on success.
"""

from Foundation import NSObject

class IOBluetoothRFCOMMChannel(NSObject):
    def writeSync_length_(self, data: bytes, length: int, /) -> int: ...
    def closeChannel(self) -> int: ...

class IOBluetoothDevice(NSObject):
    @classmethod
    def deviceWithAddressString_(cls, address: str, /) -> IOBluetoothDevice | None: ...
    def openConnection(self) -> int: ...
    def closeConnection(self) -> int: ...
    def openRFCOMMChannelAsync_withChannelID_delegate_(
        self, channel: None, channel_id: int, delegate: NSObject, /
    ) -> tuple[int, IOBluetoothRFCOMMChannel | None]: ...
