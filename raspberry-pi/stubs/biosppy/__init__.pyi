"""Hand-written types for the ``biosppy`` vendor package (pinned ``>=2.1.0``).

Only ``biosppy.signals`` is stubbed, and within it only the five top-level
processing functions ``src/signal_processing.py`` calls. See
``.claude/skills/anheart-strict-python/SKILL.md`` rule 5.

Deliberately empty beyond that, and deliberately WITHOUT a ``__getattr__``
fallback: ``biosppy.hrv``, ``biosppy.tools``, the segmenters and the plotting
helpers are all still untyped, and a use of any of them must fail the build
instead of quietly becoming ``Any``. The real package re-exports a great deal
here (``from .signals import ecg, eda, ...``, plus ``synthesizers`` and
``features``); none of it is reachable through this stub on purpose.
"""
