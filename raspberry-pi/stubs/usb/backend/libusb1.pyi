"""Hand-written types for ``usb.backend.libusb1`` (pyusb ``>=1.2``).

``get_backend`` loads libusb-1.0 ONCE and caches it in a module global, which
is why priming it with an explicit ``find_library`` makes every later
parameterless call - including pyftdi's - reuse that library. It returns
``None`` when the library cannot be found or loaded; it does not raise.
"""

from collections.abc import Callable

from usb.backend import IBackend

def get_backend(find_library: Callable[[str], str | None] | None = None) -> IBackend | None: ...
