"""``biosppy.signals.eda`` - only ``eda()``, and only ``peaks``.

See ``ecg.pyi`` for why the return type is a Protocol with a typed
``__getitem__`` rather than a ``TypedDict``, and why the parameters are
required and keyword-only.
"""

from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type IndexArray = NDArray[np.int_]

class EdaFeatures(Protocol):
    """The subset of ``eda()``'s output this repository reads.

    The vendor also returns ``ts``, ``filtered``, ``edr``, ``edl``, ``onsets``,
    ``amplitudes``, ``phasic_rate``, ``rise_times``, ``half_rec`` and
    ``six_rec``; all omitted because nothing consumes them.
    """

    def __getitem__(self, key: Literal["peaks"], /) -> IndexArray:
        """SCR peak positions as **sample indices**, from the default
        ``emotiphai`` detector. Non-empty whenever ``eda()`` returns at all:
        ``eda_events`` raises ``ValueError("Could not find SCR pulses.")`` when
        it finds none, so "no response" arrives as an exception rather than a
        zero-length array.
        """

def eda(
    *,
    signal: FloatArray,
    sampling_rate: float,
    show: Literal[False],
) -> EdaFeatures:
    """Low-pass, smooth and decompose a raw EDA window into SCR events.

    ``min_amplitude`` (0.1) and ``size`` (0.9) are left out of this stub: they
    are detector-tuning knobs, and the repository has not chosen values for
    them. Add them here, with their vendor defaults, when it does.
    """
