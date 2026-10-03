"""The five BioSPPy signal modules this repository uses.

Re-exported explicitly (the redundant ``as`` form) because ``mypy`` runs with
``no_implicit_reexport``. The real ``biosppy/signals/__init__.py`` does the same
``from . import ...`` for these names, so this mirrors runtime rather than
inventing attributes.

``acc``, ``abp``, ``pcg``, ``ppg``, ``eeg`` and ``tools`` exist at runtime and
are intentionally NOT re-exported: nothing in this repository uses them, and a
missing name is a build failure rather than an ``Any``.

Each submodule stub declares its own ``FloatArray`` / ``IndexArray`` aliases
instead of importing shared ones from here. Two duplicated lines per file buys
a stub tree where every declared name also exists at runtime, so ``stubtest``
stays usable as a cross-check.
"""

from . import bvp as bvp
from . import ecg as ecg
from . import eda as eda
from . import emg as emg
from . import resp as resp
