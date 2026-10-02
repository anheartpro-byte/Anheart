# Hand-written stub covering ONLY the members src/dsp.py uses (contract rule 5):
# scipy ships no usable types, and src/dsp.py is the one module that imports it.
from typing import Literal

import numpy as np
from numpy.typing import NDArray

def butter(
    N: int,
    Wn: float | tuple[float, float],
    btype: Literal["lowpass", "highpass", "bandpass", "bandstop"] = ...,
    *,
    output: Literal["sos"],
    fs: float,
) -> NDArray[np.float64]: ...
def sosfiltfilt(sos: NDArray[np.float64], x: NDArray[np.float64]) -> NDArray[np.float64]: ...
def find_peaks(
    x: NDArray[np.float64],
    *,
    height: float | None = ...,
    distance: float | None = ...,
    prominence: float | None = ...,
) -> tuple[NDArray[np.intp], dict[str, NDArray[np.float64]]]: ...
def welch(
    x: NDArray[np.float64], *, fs: float, nperseg: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]: ...
