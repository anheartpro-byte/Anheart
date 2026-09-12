"""``biosppy.signals.emg`` - only ``emg()``, and only ``onsets``.

See ``ecg.pyi`` for why the return type is a Protocol with a typed
``__getitem__`` rather than a ``TypedDict``, and why the parameters are
required and keyword-only.
"""

from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type IndexArray = NDArray[np.int_]

class EmgFeatures(Protocol):
    """The subset of ``emg()``'s output this repository reads.

    ``ts`` and ``filtered`` are omitted because nothing consumes them.
    """

    def __getitem__(self, key: Literal["onsets"], /) -> IndexArray:
        """Muscle-activation onset positions as **sample indices**. May be
        empty (a resting muscle), and unlike the EDA detector this one does not
        raise on an empty result - so a zero count here means "no activation",
        which is a legitimate measurement rather than a failure.
        """

def emg(
    *,
    signal: FloatArray,
    sampling_rate: float,
    show: Literal[False],
) -> EmgFeatures:
    """High-pass (100 Hz) a raw EMG window and find activation onsets.

    The 100 Hz high-pass is above this repository's own 10-120 Hz EMG band, so
    the metric path and the displayed waveform are not filtered alike. That is
    the vendor's default and is left untouched here; it is a treatment
    decision, not a typing one.
    """
