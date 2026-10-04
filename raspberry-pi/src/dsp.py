"""Signal processing for every BITalino sensor: the one module that imports scipy.

Contract rule 5: scipy ships no usable types, so it is imported here and only
here, against the hand-written stub in ``stubs/scipy/signal``. Every sensor
processor under ``src/sensors/`` filters, finds peaks and estimates spectra
through these functions and never sees scipy itself.

Every function is **total**: a batch that is too short to filter, a cut-off
above Nyquist, a NaN from a corrupt frame - each comes back as
``Err(DspRefused)`` with a sentence, never as an exception out of a processor.
A sensor that cannot process a batch reports poor quality for it; it does not
take the acquisition loop down with it.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, cast

import numpy as np
from numpy.typing import NDArray
from scipy import signal as _signal

from src.result import Err, Ok, Result
from src.units import Hertz, Seconds

type Signal = NDArray[np.float64]
"""One channel's samples, as float64, in whatever unit the caller converted to."""

DEFAULT_ORDER: Final[int] = 4
"""Butterworth order used by every sensor unless it says otherwise."""

MIN_PEAK_SAMPLES: Final[int] = 3
"""A local maximum needs a neighbour on each side."""

MAX_MAGNITUDE: Final[float] = 1e9
"""No sensor here produces a sample this large (ADC counts are <= 1023, signals a few
thousand units at most): beyond it, the input is corrupt and the arithmetic would
overflow, so it is refused rather than processed."""

MIN_SPECTRUM_SAMPLES: Final[int] = 8
"""Below this a Welch estimate is one bin of noise."""


def values_of(x: Signal) -> list[float]:
    """The samples as Python floats.

    numpy's stubs type ``tolist()`` (and element indexing) as ``Any``; for a
    float64 array it is, by numpy's definition, a flat ``list[float]``. This
    cast states that fact once so no ``Any`` escapes into a sensor.
    """
    return cast("list[float]", x.tolist())


def _magnitude(x: Signal) -> float:
    """The largest absolute sample (0.0 for an empty signal)."""
    return max((abs(v) for v in values_of(x)), default=0.0)


def _indices_of(found: NDArray[np.intp]) -> tuple[int, ...]:
    """Peak indices as Python ints (an intp array's ``tolist`` is ``list[int]``)."""
    return tuple(cast("list[int]", found.tolist()))


@dataclass(frozen=True, slots=True)
class DspRefused:
    """Why a signal could not be processed. Prose for the log and the screen."""

    detail: str


def as_signal(values: Sequence[float]) -> Signal:
    """A float64 array copy of ``values``."""
    return np.asarray(values, dtype=np.float64)


def is_finite(x: Signal) -> bool:
    """Whether every sample is a real number (no NaN, no infinity)."""
    return bool(np.all(np.isfinite(x)))


def _check(x: Signal, fs: Hertz, cutoffs: Sequence[float]) -> DspRefused | None:
    if not np.isfinite(fs) or fs <= 0.0:
        return DspRefused(f"sample rate must be positive, got {fs}")
    if x.size == 0:
        return DspRefused("empty signal")
    if not is_finite(x):
        return DspRefused("signal contains NaN or infinity")
    if _magnitude(x) > MAX_MAGNITUDE:
        return DspRefused(f"signal magnitude above {MAX_MAGNITUDE:g}: corrupt input")
    nyquist = fs / 2.0
    for cutoff in cutoffs:
        if not np.isfinite(cutoff) or not 0.0 < cutoff < nyquist:
            return DspRefused(f"cut-off {cutoff} Hz outside (0, {nyquist}) Hz")
    return None


def _filter(x: Signal, sos_of: Signal, *, what: str) -> Result[Signal, DspRefused]:
    try:
        return Ok(_signal.sosfiltfilt(sos_of, x))
    except ValueError as error:
        # The only failure left once the inputs are checked: a batch shorter
        # than the zero-phase filter's padding.
        return Err(DspRefused(f"{what}: {error}"))


def bandpass(
    x: Signal, fs: Hertz, low: Hertz, high: Hertz, order: int = DEFAULT_ORDER
) -> Result[Signal, DspRefused]:
    """Zero-phase Butterworth band-pass between ``low`` and ``high``."""
    problem = _check(x, fs, (low, high))
    if problem is None and low >= high:
        problem = DspRefused(f"band {low}-{high} Hz is empty")
    if problem is not None:
        return Err(problem)
    sos = _signal.butter(order, (low, high), "bandpass", output="sos", fs=fs)
    return _filter(x, sos, what="band-pass")


def lowpass(
    x: Signal, fs: Hertz, cutoff: Hertz, order: int = DEFAULT_ORDER
) -> Result[Signal, DspRefused]:
    """Zero-phase Butterworth low-pass at ``cutoff``."""
    problem = _check(x, fs, (cutoff,))
    if problem is not None:
        return Err(problem)
    sos = _signal.butter(order, cutoff, "lowpass", output="sos", fs=fs)
    return _filter(x, sos, what="low-pass")


def highpass(
    x: Signal, fs: Hertz, cutoff: Hertz, order: int = DEFAULT_ORDER
) -> Result[Signal, DspRefused]:
    """Zero-phase Butterworth high-pass at ``cutoff``."""
    problem = _check(x, fs, (cutoff,))
    if problem is not None:
        return Err(problem)
    sos = _signal.butter(order, cutoff, "highpass", output="sos", fs=fs)
    return _filter(x, sos, what="high-pass")


def peaks(
    x: Signal,
    fs: Hertz,
    *,
    min_interval: Seconds,
    height: float | None = None,
    prominence: float | None = None,
) -> tuple[int, ...]:
    """Indices of local maxima at least ``min_interval`` apart. Empty when there are none.

    Total: an empty, non-finite or degenerate input simply has no peaks.
    """
    if (
        x.size < MIN_PEAK_SAMPLES
        or not is_finite(x)
        or _magnitude(x) > MAX_MAGNITUDE
        or fs <= 0.0
        or min_interval <= 0.0
    ):
        return ()
    distance = max(1.0, float(min_interval) * float(fs))
    found, _properties = _signal.find_peaks(
        x, height=height, distance=distance, prominence=prominence
    )
    return _indices_of(found)


@dataclass(frozen=True, slots=True)
class Spectrum:
    """A power spectral density: ``power[i]`` at ``frequencies[i]`` Hz."""

    frequencies: Signal
    power: Signal


def spectrum(x: Signal, fs: Hertz, *, segment: int = 256) -> Result[Spectrum, DspRefused]:
    """Welch power spectral density, segment length clamped to the signal."""
    problem = _check(x, fs, ())
    if problem is None and x.size < MIN_SPECTRUM_SAMPLES:
        problem = DspRefused(f"{x.size} samples: too short for a spectrum")
    if problem is not None:
        return Err(problem)
    frequencies, power = _signal.welch(x, fs=fs, nperseg=min(segment, int(x.size)))
    return Ok(Spectrum(frequencies=frequencies, power=power))


def median_frequency(x: Signal, fs: Hertz) -> Result[Hertz, DspRefused]:
    """The frequency splitting the spectrum's power in two halves (EMG fatigue index)."""
    found = spectrum(x, fs)
    if isinstance(found, Err):
        return found
    power = found.value.power
    total = float(np.sum(power))
    if total <= 0.0:
        return Err(DspRefused("no power in the signal"))
    cumulative = np.cumsum(power)
    index = int(np.searchsorted(cumulative, total / 2.0))
    return Ok(Hertz(values_of(found.value.frequencies)[index]))


def dominant_frequency(x: Signal, fs: Hertz, low: Hertz, high: Hertz) -> Result[Hertz, DspRefused]:
    """The strongest frequency within ``[low, high]`` (respiration rate, pulse rate)."""
    found = spectrum(x, fs, segment=int(x.size))
    if isinstance(found, Err):
        return found
    frequencies = values_of(found.value.frequencies)
    power = values_of(found.value.power)
    inside = [(p, f) for f, p in zip(frequencies, power, strict=True) if low <= f <= high]
    if not inside:
        return Err(DspRefused(f"no spectral line within {low}-{high} Hz"))
    best_power, best_frequency = max(inside)
    if best_power <= 0.0:
        return Err(DspRefused(f"no power within {low}-{high} Hz"))
    return Ok(Hertz(best_frequency))


def rms(x: Signal) -> float:
    """Root mean square; 0.0 for an empty or non-finite signal. Never overflows.

    Scaled by the largest magnitude first, so a sum of squares of huge values
    cannot reach infinity.
    """
    if x.size == 0 or not is_finite(x):
        return 0.0
    values = values_of(x)
    scale = max(abs(v) for v in values)
    if scale == 0.0:
        return 0.0
    return scale * math.sqrt(sum((v / scale) ** 2 for v in values) / len(values))
