"""Hand-written types for ``pyftdi.usbtools`` (pinned ``>=0.57,<0.58``).

Only the one member ``src/motor/ftdi_link.py`` touches. No module-level
``__getattr__``: an unstubbed member must fail the build. Read off the
installed source (``pyftdi/usbtools.py``, 0.57.2): ``flush_cache`` clears the
per-vendor/product enumeration cache that ``Ftdi.create_from_url`` otherwise
reuses forever.
"""

class UsbTools:
    @classmethod
    def flush_cache(cls) -> None: ...
