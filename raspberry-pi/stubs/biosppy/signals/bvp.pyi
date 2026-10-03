"""``biosppy.signals.bvp`` - only ``bvp()``, and only ``heart_rate``.

See ``ecg.pyi`` for why the return type is a Protocol with a typed
``__getitem__`` rather than a ``TypedDict``, and why the parameters are
required and keyword-only.

Note for the caller: this is a PPG-derived pulse rate, computed from pulse
onsets by the same ``tools.get_heart_rate`` the ECG path uses. It is NOT
interchangeable with the ECG heart rate - it lags, it is far more
motion-sensitive, and it is the wrong signal to regulate a centrifuge on.
Nothing in the type system distinguishes them, which is why the caller reports
it under a separate ``pulse`` key.
"""

from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]

class BvpFeatures(Protocol):
    """The subset of ``bvp()``'s output this repository reads.

    ``ts``, ``filtered``, ``onsets`` and ``heart_rate_ts`` are omitted because
    nothing consumes them.
    """

    def __getitem__(self, key: Literal["heart_rate"], /) -> FloatArray:
        """Instantaneous pulse rate in **bpm**, clipped by BioSPPy to 40..200
        and boxcar-smoothed. May be empty; never ``None``. As in the ECG path,
        fewer than two detected onsets raises ``ValueError`` out of
        ``tools.get_heart_rate`` rather than returning an empty array.
        """

def bvp(
    *,
    signal: FloatArray,
    sampling_rate: float,
    show: Literal[False],
) -> BvpFeatures:
    """Band-pass (1-8 Hz) a raw BVP/PPG window and derive a pulse rate.

    ``bvp()`` has no ``units`` parameter, unlike its four siblings.
    """
