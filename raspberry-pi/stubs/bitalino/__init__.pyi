"""Hand-written types for the ``bitalino`` vendor module (pinned ``>=1.2.6``).

``bitalino`` ships no ``py.typed``, so every call through it currently returns
an implicit ``Any``. In this repository those calls carry the ADC samples that
become the heart rate that sets the motor speed, so an ``Any`` here is a hole
straight through the safety chain. See
``.claude/skills/anheart-strict-python/SKILL.md`` rule 5.

Scope is deliberately narrow: only the members ``src/bitalino_client.py``
actually touches. There is also deliberately **no** module-level
``__getattr__``: reaching for an unstubbed member must fail the build rather
than silently resolve to ``Any``. Extending this file is the intended cost of
using a new vendor member.

Signatures were read off the installed source
(``.venv/Lib/site-packages/bitalino.py``), not from the docstrings - the
docstrings disagree with the code in places (see :meth:`BITalino.read`).

The vendor's camelCase parameter names are reproduced **exactly**.
``BITalinoClient`` reaches ``start``/``read``/``stop``/``state`` through
``loop.run_in_executor(None, method, *args)``, which passes positionally, so
these must stay positional-or-keyword; but a future caller may use the vendor's
documented keyword names, so renaming them here would make one of the two
wrong.

Nothing here imports from ``src``: this file describes the wire, in plain
builtins and numpy. Wrapping these values into ``AdcCount``/``Hertz`` is the
job of the one isolation module behind the boundary (rule 6).
"""

from collections.abc import Sequence
from typing import Final, TypedDict

import numpy as np
from numpy.typing import NDArray

type SampleMatrix = NDArray[np.int_]
"""What :meth:`BITalino.read` returns: ``numpy.zeros((nSamples, 5 + nChannels), dtype=int)``.

Shape is left as ``NDArray`` rather than a shape-typed ``tuple[int, int]``
because ``numpy.vstack`` in the caller erases the shape immediately anyway. The
load-bearing fact is the COLUMN LAYOUT, which no type can express:

    col 0      sequence number (wraps at 15)
    cols 1..4  digital channels I1 I2 O1 O2 (always present)
    cols 5..   the analog channels, **in the order passed to** :meth:`start` -
               so the Nth *requested* channel is column ``5 + N``, NOT
               ``5 + channel_index``.

Confusing those two is how an ECG trace ends up read from the LUX column, which
is why ``CHANNEL_MAP`` ordering is asserted on the caller's side.

``np.int_`` (not a fixed width) because the vendor writes ``dtype=int``, i.e.
whatever the platform default integer is; under numpy >= 2 that is ``int64`` on
both the Pi and a Windows dev box, and ``np.int_ is np.intp`` there.
"""

class DeviceState(TypedDict):
    """The dict :meth:`BITalino.state` builds. A real ``dict`` at runtime, so a
    ``TypedDict`` is honest here (unlike the BioSPPy return objects, which only
    look like mappings - see ``biosppy/signals/ecg.pyi``).

    Values are typed as ``Sequence[int]`` rather than ``list[int]``: they are
    lists at runtime, but this is a snapshot read back from hardware and
    mutating it would only corrupt the caller's own view of the device.
    """

    analogChannels: Sequence[int]  # [A1..A6], each 0..1023
    battery: int  # ABAT channel, 0..1023
    batteryThreshold: int  # 0..63, as set by battery()
    digitalChannels: Sequence[int]  # [I1, I2, O1, O2], each 0 or 1

class ExceptionCode:
    """The vendor's error-message table.

    Every failure in ``bitalino`` is a bare ``Exception`` carrying one of these
    strings, so string comparison against these constants is the only way to
    tell "lost the device" from "bad parameter". Typed ``Final[str]`` rather
    than ``Final = "..."`` on purpose: pinning the literals here would let a
    vendor bump silently invalidate a comparison that still type-checks.
    """

    INVALID_ADDRESS: Final[str]
    INVALID_PLATFORM: Final[str]
    CONTACTING_DEVICE: Final[str]
    DEVICE_NOT_IDLE: Final[str]
    DEVICE_NOT_IN_ACQUISITION: Final[str]
    INVALID_PARAMETER: Final[str]
    INVALID_VERSION: Final[str]
    IMPORT_FAILED: Final[str]

class BITalino:
    """A connected BITalino / psychoBIT device.

    Every method below raises a bare ``Exception`` (never a subclass) on
    protocol or communication failure, and every one of them BLOCKS on a socket
    or serial port. Both facts are invisible in the signatures, which is why
    the caller runs them in an executor and converts to ``Result`` at its own
    boundary.
    """

    def __init__(self, macAddress: str, timeout: float | None = None) -> None:
        """``macAddress`` is a MAC (``00:0a:95:9d:68:16``), a serial device
        (``COM3`` / ``/dev/rfcomm0``) or ``host:port``; the three are told apart
        by shape, so a typo becomes an "invalid address" Exception.

        ``timeout=None`` means **wait forever** on every read - the vendor
        default, and the reason a hung device shows up as a stuck executor
        thread rather than an error.
        """

    def start(self, SamplingRate: int = 1000, analogChannels: Sequence[int] = ...) -> None:
        """Begin acquisition. Valid rates are exactly 1, 10, 100, 1000 Hz;
        channels are 0..5 (A1..A6), 1 to 6 of them.

        ``analogChannels`` is ``Sequence[int]`` because the vendor accepts a
        list, a tuple or an ``ndarray`` and de-duplicates via ``set()`` - so the
        *requested* order is not necessarily the *column* order. See
        :data:`SampleMatrix`.
        """

    def stop(self) -> None:
        """Leave acquisition. On a BITalino 1.0 this raises if not acquiring."""

    def close(self) -> None:
        """Close the socket or serial port. Does not stop acquisition."""

    def read(self, nSamples: int = 100) -> SampleMatrix:
        """Block until ``nSamples`` frames have arrived, then return them.

        The docstring in the vendor source claims the matrix holds "4 Digital
        Channels" and omits that the analog columns follow the *request* order;
        :data:`SampleMatrix` documents what the code actually writes.
        """

    def version(self) -> str:
        """The firmware banner, e.g. ``BITalino_v5.2``. Raises while acquiring."""

    def state(self) -> DeviceState:
        """One-shot read of all channels. BITalino 2.0+ only, and raises while
        acquiring - so this is a between-sessions call, never a monitor.
        """
