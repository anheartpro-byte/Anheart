"""Hand-written types for ``pyusb`` (the ``usb`` package, pinned ``>=1.2``).

Only ``usb.backend.libusb1.get_backend`` is used, from
``src/motor/ftdi_link.py`` alone (contract rule 5), to load libusb from an
explicit location before pyftdi asks for it.
"""
