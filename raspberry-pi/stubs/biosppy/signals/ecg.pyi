"""``biosppy.signals.ecg`` - only ``ecg()``, and only the keys read from it.

# Why a Protocol with overloaded ``__getitem__``, and NOT a TypedDict

``ecg()`` returns a ``biosppy.utils.ReturnTuple``, which subclasses ``tuple``
and bolts a name list on the side. It *looks* like a mapping and is not one.
Verified against the installed 2.1.2:

    rt = ReturnTuple((1, 2), ("a", "b"))
    hasattr(rt, "get")    -> False       # AttributeError at runtime
    hasattr(rt, "items")  -> False       # AttributeError at runtime
    "a" in rt             -> False       # tuple.__contains__ tests VALUES

A ``TypedDict`` would make all three of those type-check. The third is the
dangerous one: ``if "heart_rate" in out:`` is silently, permanently ``False``,
so the guarded branch never runs and the heart rate is simply never reported -
no exception, no log line. In this repository every metric call site sits inside
``except Exception: return None``, so ``.get`` / ``.items`` would not crash
loudly either; they would disable metrics for the session. Exactly the silent
failure class this contract exists to prevent.

So the Protocol declares ``__getitem__`` and nothing else. ``len(out)``,
``out[0]``, ``in``, ``.get()``, ``dict(out)`` and ``**out`` are all type errors,
which is a truthful description of what this object safely supports. Integer
indexing is excluded on purpose as well: the key order differs between the five
modules, so ``out[2]`` means a different quantity in each.

Overloads rather than one union return type because the dtypes genuinely
differ - ``rpeaks`` is an integer index array and ``heart_rate`` is float bpm,
and dividing one by the sampling rate expecting the other is the bug this file
is here to catch.

# Parameters

``signal``, ``sampling_rate`` and ``show`` are required and keyword-only, which
is narrower than the vendor signature (all three have defaults). Each
narrowing pays for itself:

* ``signal=None`` raises ``TypeError`` at runtime, so requiring it is strictly
  more honest than a default.
* ``sampling_rate`` defaults to ``1000.0``, which HAPPENS to equal the
  BITalino's rate today. A forgotten argument would therefore work silently now
  and become a 4x heart-rate error the day the input rate changes. Requiring it
  makes that a compile error instead.
* ``show`` defaults to ``True``, which calls into ``matplotlib`` and blocks on a
  GUI window. On a headless Pi, inside the acquisition path, that is a hung
  session. ``Literal[False]`` forbids it outright; plotting belongs in
  ``scripts/``, which is outside the checked tree.

Types stay in plain numpy: converting to ``Bpm`` and ``Hertz`` is the job of
``src/signal_processing.py``, the one module allowed to import biosppy (rule 6).
"""

from typing import Literal, Protocol, overload

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]
type IndexArray = NDArray[np.int_]

class EcgFeatures(Protocol):
    """The subset of ``ecg()``'s output this repository reads.

    The vendor also returns ``ts``, ``filtered``, ``templates_ts`` and
    ``templates``; they are absent because nothing consumes them yet. Adding one
    is an ``@overload`` here plus its verified dtype - not a guess.
    """

    @overload
    def __getitem__(self, key: Literal["heart_rate"], /) -> FloatArray:
        """Instantaneous heart rate in **bpm**, already clipped by BioSPPy to
        40..200 and boxcar-smoothed over 3 samples.

        May be shorter than ``rpeaks`` (one fewer element, minus whatever the
        40..200 filter dropped) and may be EMPTY. Never ``None``: with fewer
        than two R-peaks ``ecg()`` raises ``ValueError`` out of
        ``tools.get_heart_rate`` instead of returning an empty result, so a
        too-short window is an exception, not a zero.
        """

    @overload
    def __getitem__(self, key: Literal["rpeaks"], /) -> IndexArray:
        """R-peak positions as **sample indices into the input signal**, not
        seconds. Converting to intervals means dividing by the sampling rate
        that was passed in - hence ``sampling_rate`` being required above.
        """

def ecg(
    *,
    signal: FloatArray,
    sampling_rate: float,
    show: Literal[False],
) -> EcgFeatures:
    """Filter, segment and rate a raw ECG window. See the module docstring for
    why all three parameters are required and keyword-only.

    Raises (bare, un-typed, all of them): ``ValueError`` from
    ``get_heart_rate`` when the window holds fewer than two usable R-peaks,
    plus whatever the default ``hamilton`` segmenter raises on degenerate input.
    A caller must treat this as fallible; it is not a total function.
    """
