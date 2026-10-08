"""The ECG quality grade failing in the REAL console, during a programme.

``tests/test_failure_ecg.py`` corrupts the samples. Here the samples are a
clean heart and it is the grader that cannot do its work: the mains filter of
``src/signal_processing.py`` raises on every window. That failure used to be
swallowed and the window graded ``good``, so BioSPPy went on extracting a
heart rate from windows nobody had graded and the programme carried on
regulating on it.

What must follow instead is what follows any lost heart rate, and it is
judged by the same :func:`~tests.test_failure_ecg.judged_end`: no rate reaches
the runtime, ``hr_stale`` freezes the setpoint at 10 s, reduces it at 30 s and
ramps down at 60 s, the session ends on a ``safety_verdict``, the speed never
rises meanwhile, the shaft is left at 0 and the operator is told why.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from types import ModuleType
from typing import NoReturn, Protocol, cast

import pytest

from src.training.types import Phase, SignalQuality
from tests.test_failure_ecg import PHASE_AT, judged_end, programme, run_to

LEGACY: str = "src.signal_processing"
ZERO_PHASE_FILTER: str = "filtfilt"


class LegacyModule(Protocol):
    """``src.signal_processing`` (outside both type checkers): the name it filters through."""

    sps: ModuleType


class Refusals:
    """How many windows the grader's mains filter was asked for, and refused."""

    def __init__(self) -> None:
        self.count: int = 0

    def refuse(self, *args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        self.count += 1
        raise ValueError("the test made the mains filter refuse")


def refusing_library(real: ModuleType, refusals: Refusals) -> ModuleType:
    """``scipy.signal`` as the legacy DSP sees it, with a zero-phase filter that refuses.

    Every other member is the real one, so the display filter keeps running.
    BioSPPy and the typed processor import scipy for themselves and are not
    touched: the grader alone fails.
    """
    library = ModuleType(real.__name__)
    library.__dict__.update(real.__dict__)
    library.__dict__[ZERO_PHASE_FILTER] = refusals.refuse
    return library


async def test_a_quality_grade_that_cannot_be_computed_ends_on_hr_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """EX-2, with the verdict of the supervisor that follows."""
    caplog.set_level(logging.WARNING, logger=LEGACY)
    run = programme(tmp_path, monkeypatch)
    at = PHASE_AT[Phase.WARMUP]
    await run_to(run, at, real_dsp=True)
    assert run.panel.runtime.snapshot().live_bpm is not None, "the real DSP never locked on"

    legacy = cast("LegacyModule", importlib.import_module(LEGACY))
    refusals = Refusals()
    monkeypatch.setattr(legacy, "sps", refusing_library(legacy.sps, refusals))

    await run.rig.tick(2.0)
    handed = run.panel.reporter.panel_status().ecg.bridge.last_metrics
    assert handed is not None
    assert (handed.quality, handed.bpm) == (SignalQuality.NO_SIGNAL, None)
    assert refusals.count > 0
    assert "ECG window not graded: the mains notch failed" in caplog.text

    await judged_end(run, injected_at=at)
