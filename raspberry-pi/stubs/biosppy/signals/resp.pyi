"""``biosppy.signals.resp`` - only ``resp()``, and only ``resp_rate``.

See ``ecg.pyi`` for why the return type is a Protocol with a typed
``__getitem__`` rather than a ``TypedDict``, and why the parameters are
required and keyword-only.

# The one place BioSPPy changes the TYPE of a return value

``resp()`` does not always return an array. From the installed 2.1.2 source:

    if len(beats) < 2:
        rate_idx = []
        rate = []          # <- a plain list, returned as-is
    else:
        ...
        rate = sampling_rate * (1. / np.diff(beats))

Confirmed by running it: a flat 3 s input gives ``resp_rate`` as ``[]``
(``list``), while a real breathing signal gives ``ndarray`` of ``float64``.
Fewer than two zero crossings is not an edge case on a respiration band - it is
what a held breath, a detached band or a too-short window looks like.

So the declared type is the honest union. A stub that promised ``NDArray`` here
would be a lie that only shows up as an ``AttributeError`` on the quietest
input, inside the caller's ``except Exception: return None``.
"""

from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]

type RespRate = FloatArray | list[float]
"""Respiration rate, or the empty ``list`` the vendor substitutes for it.

``list[float]`` rather than ``Sequence[float]`` because the runtime value really
is a ``list`` and because numpy's ``ArrayLike`` accepts it without argument.
Both members support ``len()`` and ``np.median``, which is all the caller needs
- but the union forces the caller to notice that "no rate" is representable.

Calling ``np.median`` on the union directly costs an ``Any``: numpy's overloads
resolve a union argument to the ``ArrayLike`` fallback. Normalise first, which
is the right thing to do anyway -

    rate = np.asarray(out["resp_rate"], dtype=np.float64)
    if rate.size == 0:
        return None

- and both the array and the empty-list case collapse into one typed path.
"""

class RespFeatures(Protocol):
    """The subset of ``resp()``'s output this repository reads.

    ``ts``, ``filtered``, ``zeros`` and ``resp_rate_ts`` are omitted because
    nothing consumes them. Note that ``resp_rate_ts`` is NOT affected by the
    list/array split above: it is always ``ts[rate_idx]``, hence always an array.
    """

    def __getitem__(self, key: Literal["resp_rate"], /) -> RespRate:
        """Instantaneous respiration rate in **Hz**, not breaths per minute -
        BioSPPy filters it to ``<= 0.35 Hz`` and smooths it over 3 samples. The
        caller multiplies by 60 exactly once; doing it twice is a 60x error that
        no type here can catch, which is why the unit is stated.

        May be empty, and may be a ``list`` rather than an array. See
        :data:`RespRate`.
        """

def resp(
    *,
    signal: FloatArray,
    sampling_rate: float,
    show: Literal[False],
) -> RespFeatures:
    """Band-pass (0.1-0.35 Hz) a raw respiration window and rate it."""
