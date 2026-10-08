"""The legacy ECG treatment (``src/signal_processing.py``): its quality grade and its metrics.

The heart rate that regulates the motor is extracted here, and only from a
window this module grades ``good``. So the grade must fail on the safe side:
``good`` is never the answer to a window that could not be judged.

What the tests pin, in the order of the file:

* the grade of a window nobody can judge: a non-finite sample (NaN, infinity),
  a window that is not one-dimensional or too short, a mains test that cannot
  be designed or that scipy refuses, a variance that overflows. Each is
  ``no_signal``, said in the log, and a property states it for any window
  holding a non-finite value;
* the log itself: one line when a stretch of ungradable windows begins, one
  when it is over, never a line per window, whatever the windows are (a
  property bounds it);
* the grades that did not change: clean, flat, clipped, mains hum;
* what follows through ``process``: a fresh ``no_signal`` reading with no heart
  rate while the bad sample is in the window, and the rate back once it has
  left;
* the arithmetic on what BioSPPy returns (median rate, RMSSD), with a scripted
  extractor so each number is derived by hand, and what a refusal of BioSPPy
  leaves behind: nothing fresh;
* the other channels, the streaming filter and decimation, and the module
  without BioSPPy.

The module is outside both type checkers, so it is loaded through ``importlib``
and seen through the Protocols below (the reason ``tests/test_sim.py`` gives: a
static import would pull an unchecked module's diagnostics into this file).

What the supervisor then does with a window that cannot be graded is shown on
the real console in ``tests/test_failure_ecg.py``.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import math
import sys
from abc import abstractmethod
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Final, Protocol, cast, override

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src import dsp
from src.ecg_pipeline import ECG_CHANNEL, HEART_RATE_KEY, QUALITY_KEY, load_treatment
from src.sim.ecg import EcgConfig, EcgSynthesizer
from src.sim.physiology import SubjectState
from src.units import Bpm, GLoad, Monotonic, MotorRpm, OutputRpm, Seconds

# =========================================================================
# The legacy module, as these tests see it
# =========================================================================


class Processor(Protocol):
    """The members of ``ChannelProcessor`` these tests use."""

    @property
    @abstractmethod
    def metric_seq(self) -> int:
        """How many fresh metric results the channel has produced."""

    @abstractmethod
    def process(self, raw: Sequence[float]) -> tuple[Sequence[float], Mapping[str, object]]:
        """Treat one batch of raw counts: the treated values and the metrics."""

    @abstractmethod
    def _ecg_quality(self, raw: dsp.Signal, mains_hz: float = 50.0) -> str:
        """Grade one window of raw ECG counts."""

    @abstractmethod
    def _mains_share(self, raw: dsp.Signal, mains_hz: float) -> float | None:
        """Share of the window's variance a mains notch removes, when it can be established."""


class ProcessorFactory(Protocol):
    """``ChannelProcessor`` itself."""

    @abstractmethod
    def __call__(self, channel: str, fs_in: int = 1000, fs_out: int = 250) -> Processor:
        """A new processor for one channel."""


class SpecFactory(Protocol):
    """``ChannelSpec`` itself."""

    @abstractmethod
    def __call__(
        self,
        filt_band: str,
        filt_cutoff: object,
        unit: str,
        mv_per_count: float | None = None,
        metric: str | None = None,
    ) -> object:
        """A channel's treatment configuration."""


class Legacy(Protocol):
    """The members of ``src.signal_processing`` these tests use."""

    BIOSPPY_AVAILABLE: bool
    ECG_MV_PER_COUNT: float
    UNGRADED_CLEAR_WINDOWS: int
    SENSOR_SPECS: MutableMapping[str, object]
    ChannelProcessor: ProcessorFactory
    ChannelSpec: SpecFactory
    sps: ModuleType
    """``scipy.signal``, under the name the module filters through."""


SOURCE: Final[Path] = Path(__file__).resolve().parent.parent / "src" / "signal_processing.py"
MODULE_NAME: Final[str] = "src.signal_processing"
LEGACY: Final[Legacy] = cast("Legacy", importlib.import_module(MODULE_NAME))

FS: Final[int] = 1000
"""The console's acquisition rate (``ECG_SAMPLE_RATE``)."""

BLOCK: Final[int] = 200
"""One batch as the bridge reads it: 200 ms."""

WINDOW: Final[int] = 8 * FS
"""The rolling window the metrics are computed over."""

DECIMATION: Final[int] = 4
"""1000 Hz in, 250 Hz out."""

GOOD: Final[str] = "good"
NO_SIGNAL: Final[str] = "no_signal"
NOISY: Final[str] = "noisy"
MAINS_DOMINATED: Final[str] = "mains_dominated"

ADC_MID: Final[float] = 512.0


# =========================================================================
# Signals
# =========================================================================


def _subject(bpm: float) -> SubjectState:
    """A hand-built subject at rest: only ``rr_interval`` is rendered."""
    return SubjectState(
        at=Monotonic(0.0),
        motor_rpm=MotorRpm(0),
        output_rpm=OutputRpm(0.0),
        g_load=GLoad(0.0),
        heart_rate=Bpm(round(bpm)),
        rr_interval=Seconds(60.0 / bpm),
        steady_state=Bpm(round(bpm)),
        drift_bpm=0.0,
        artifacts=frozenset(),
    )


def _ecg(bpm: float, count: int, fs: int = FS) -> list[float]:
    """``count`` raw counts of a clean synthetic ECG, as the BITalino would send them."""
    synth = EcgSynthesizer(EcgConfig(sample_rate=fs))
    return [float(v) for v in synth.render(count, _subject(bpm))]


def _sine(hz: float, amplitude: float, count: int, *, mid: float = ADC_MID) -> list[float]:
    return [mid + amplitude * math.sin(2.0 * math.pi * hz * i / FS) for i in range(count)]


CLEAN: Final[tuple[float, ...]] = tuple(_ecg(70.0, WINDOW))
"""Eight seconds of a clean 70 bpm heart: the window every corruption below starts from."""


def _with(values: Sequence[float], replaced: Mapping[int, float]) -> list[float]:
    """``values`` with the samples at the given indices replaced."""
    out = list(values)
    for index, value in replaced.items():
        out[index] = value
    return out


def _grade_array(processor: Processor, window: dsp.Signal, mains_hz: float = 50.0) -> str:
    """The grader's verdict on one array, whatever its shape.

    The one place the private grader is called: its only public route is
    ``process``, which never hands it less than three seconds, a table of
    samples or another mains frequency.
    """
    return processor._ecg_quality(window, mains_hz)  # pyright: ignore[reportPrivateUsage] - no public route


def _grade(processor: Processor, raw: Sequence[float], mains_hz: float = 50.0) -> str:
    """The grader's verdict on one window of samples."""
    return _grade_array(processor, dsp.as_signal(raw), mains_hz)


def _share(processor: Processor, raw: Sequence[float], mains_hz: float = 50.0) -> float | None:
    """The mains share of one window, asked of the helper itself.

    The grader only reaches it with a window that is neither flat nor
    clipped: what it answers to any other window has no other route.
    """
    return processor._mains_share(dsp.as_signal(raw), mains_hz)  # pyright: ignore[reportPrivateUsage] - no public route


class ZeroPhaseFilter(Protocol):
    """A stand-in for ``scipy.signal.filtfilt``, called as ``filtfilt(b, a, signal)``."""

    @abstractmethod
    def __call__(self, *filter_and_signal: object) -> dsp.Signal:
        """What the notch gives back."""


def _zero_phase_filter(monkeypatch: pytest.MonkeyPatch, answer: ZeroPhaseFilter) -> None:
    """Make the module's zero-phase filter ``answer``, and nothing else differ.

    The module gets a copy of ``scipy.signal`` whose ``filtfilt`` is
    ``answer``. BioSPPy and the typed processors import scipy for themselves
    and keep the real one.
    """
    library = ModuleType(LEGACY.sps.__name__)
    library.__dict__.update(LEGACY.sps.__dict__)
    library.__dict__["filtfilt"] = answer
    monkeypatch.setattr(LEGACY, "sps", library)


def _ecg_processor(fs: int = FS) -> Processor:
    return LEGACY.ChannelProcessor("ECG", fs, 250)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == MODULE_NAME and record.levelno == logging.WARNING
    ]


# =========================================================================
# The grade of a window that cannot be judged: never good
# =========================================================================

NON_FINITE_WINDOWS: Final[Mapping[str, Sequence[float]]] = {
    "one_nan": _with(CLEAN, {4000: math.nan}),
    "nan_first": _with(CLEAN, {0: math.nan}),
    "nan_last": _with(CLEAN, {WINDOW - 1: math.nan}),
    "all_nan": [math.nan] * WINDOW,
    "one_plus_infinity": _with(CLEAN, {4000: math.inf}),
    "one_minus_infinity": _with(CLEAN, {4000: -math.inf}),
    "nan_and_infinity": _with(CLEAN, {10: math.inf, 7000: math.nan}),
    "both_infinities": _with(CLEAN, {10: math.inf, 7000: -math.inf}),
    "exactly_one_second_with_a_nan": _with(CLEAN[:FS], {500: math.nan}),
}

RAILED: Final[tuple[float, ...]] = (0.0, 1023.0) * (WINDOW // 2)
"""Every sample at a rail: ``noisy`` when it is finite."""

RAILED_NON_FINITE_WINDOWS: Final[Mapping[str, Sequence[float]]] = {
    "railed_with_a_nan": _with(RAILED, {4000: math.nan}),
    "railed_with_an_infinity": _with(RAILED, {4000: math.inf}),
    "all_plus_infinity": [math.inf] * WINDOW,
    "all_minus_infinity": [-math.inf] * WINDOW,
    "three_infinities_in_four_over_a_heart": _with(
        CLEAN, dict.fromkeys((i for i in range(WINDOW) if i % 4), math.inf)
    ),
}


@pytest.mark.parametrize("name", list(NON_FINITE_WINDOWS))
def test_a_window_holding_a_non_finite_sample_is_no_signal(name: str) -> None:
    """EX-2. Each of these was graded ``good``: every comparison against NaN is false."""
    grade = _grade(_ecg_processor(), NON_FINITE_WINDOWS[name])
    assert grade == NO_SIGNAL


@pytest.mark.parametrize("name", list(RAILED_NON_FINITE_WINDOWS))
def test_a_corrupt_window_is_no_signal_even_when_most_of_it_sits_at_the_rails(name: str) -> None:
    """A corrupt frame is not a saturated amplifier: it is refused before the clipping test.

    This is what the explicit refusal of a non-finite sample decides and the
    variance check behind it does not: an infinity reads as "at the top rail",
    so without that refusal these windows are ``noisy``, a grade that tells
    the operator to look at the electrodes when the samples themselves are
    not numbers.
    """
    grade = _grade(_ecg_processor(), RAILED_NON_FINITE_WINDOWS[name])
    assert grade == NO_SIGNAL


def test_a_non_finite_window_is_said_in_the_log_without_any_sample(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), _with(CLEAN, {10: math.inf, 7000: math.nan, 7001: math.nan}))
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == ["ECG window not graded: 3 of its 8000 samples are not finite"]


NON_FINITE: Final[st.SearchStrategy[float]] = st.sampled_from([math.nan, math.inf, -math.inf])

SMALL_FS: Final[int] = 112
"""The lowest whole rate the 50 Hz notch exists at: a window is 112 samples and up."""

ANY_FINITE_SAMPLE: Final[st.SearchStrategy[float]] = st.one_of(
    st.floats(min_value=4.0, max_value=1019.0),
    st.floats(min_value=0.0, max_value=1023.0),
    st.floats(allow_nan=False, allow_infinity=False),
)
"""Mostly counts clear of the rails (what the old grader let through), then anything finite."""

AT_A_RAIL: Final[st.SearchStrategy[float]] = st.sampled_from([0.0, 1.0, 3.0, 1020.0, 1023.0])

ANY_FINITE_COUNTS: Final[st.SearchStrategy[list[float]]] = st.one_of(
    st.lists(ANY_FINITE_SAMPLE, max_size=40),
    st.lists(ANY_FINITE_SAMPLE, min_size=SMALL_FS, max_size=2 * SMALL_FS),
    st.lists(AT_A_RAIL, min_size=SMALL_FS, max_size=2 * SMALL_FS),
)
"""Any finite samples, in or out of the ADC range: too few for a window, enough for one,
or a whole window at the rails (which is ``noisy`` until a sample of it is not a number)."""


def _stretch(start: int, length: int) -> list[float]:
    return list(CLEAN[start : start + length])


CLEAN_SLICES: Final[st.SearchStrategy[list[float]]] = st.builds(
    _stretch,
    st.integers(min_value=0, max_value=WINDOW - FS),
    st.integers(min_value=0, max_value=WINDOW),
)
"""A stretch of the clean heart: what the grader calls ``good`` when it is long enough."""


@st.composite
def _corrupted(draw: st.DrawFn, base: st.SearchStrategy[list[float]]) -> list[float]:
    """A window from ``base`` with at least one non-finite sample put in, anywhere."""
    values = list(draw(base))
    spots = draw(st.lists(st.tuples(st.floats(0.0, 1.0), NON_FINITE), min_size=1, max_size=4))
    for where, bad in spots:
        values.insert(int(where * len(values)), bad)
    return values


@settings(deadline=None, max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(window=_corrupted(CLEAN_SLICES))
def test_property_no_stretch_of_a_clean_heart_with_a_non_finite_sample_is_good(
    window: list[float],
) -> None:
    """EX-2: whatever else the window holds, one NaN or infinity and it is never ``good``."""
    grade = _grade(_ecg_processor(), window)
    assert grade != GOOD
    assert grade == NO_SIGNAL


@settings(deadline=None, max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(window=_corrupted(ANY_FINITE_COUNTS))
def test_property_no_window_with_a_non_finite_sample_is_good(window: list[float]) -> None:
    """The same for arbitrary samples, at a rate where a short list already fills a window."""
    grade = _grade(_ecg_processor(SMALL_FS), window)
    assert grade != GOOD
    assert grade == NO_SIGNAL


@pytest.mark.parametrize("shape", [(2, WINDOW // 2), (WINDOW, 1), (1, WINDOW)])
def test_a_window_that_is_not_one_dimensional_is_no_signal_and_is_said(
    caplog: pytest.LogCaptureFixture, shape: tuple[int, int]
) -> None:
    """A table of samples is not a signal: (8000, 1) made the mains filter raise, graded good."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    table = dsp.as_signal(CLEAN).reshape(shape)
    grade = _grade_array(_ecg_processor(), table)
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == ["ECG window not graded: 2 dimensions where a signal has one"]


@pytest.mark.parametrize(("count", "told"), [(FS - 1, "999"), (1, "1"), (0, "0")])
def test_a_window_under_one_second_is_no_signal_and_is_said(
    caplog: pytest.LogCaptureFixture, count: int, told: str
) -> None:
    """Every refusal has its line, this one too: no window is refused in silence."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), CLEAN[:count])
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == [f"ECG window not graded: {told} samples are less than a second at 1000 Hz"]


@pytest.mark.parametrize("mains_hz", [1e-9, 1e-320])
def test_a_mains_filter_that_scipy_refuses_is_no_signal_and_is_said(
    caplog: pytest.LogCaptureFixture, mains_hz: float
) -> None:
    """EX-2. scipy raises on this notch (unstable): the grade used to fall back to ``good``."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), CLEAN, mains_hz)
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert len(said) == 1
    assert said[0].startswith("ECG window not graded: the mains notch failed: ")


@pytest.mark.parametrize("mains_hz", [0.0, -50.0, math.nan, math.inf, 450.0, 600.0])
def test_a_mains_frequency_no_notch_can_be_designed_for_is_no_signal(
    caplog: pytest.LogCaptureFixture, mains_hz: float
) -> None:
    """The mains test was skipped in silence, NaN included, and the grade was ``good``."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), CLEAN, mains_hz)
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == [
        f"ECG window not graded: no mains notch at {mains_hz} Hz for a signal sampled at 1000 Hz"
    ]


def test_a_sampling_rate_too_low_for_the_mains_test_is_no_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """At 100 Hz the 50 Hz notch does not exist: hum cannot be told from a heart."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    slow = 100
    grade = _grade(_ecg_processor(slow), _ecg(70.0, 8 * slow, fs=slow))
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == [
        "ECG window not graded: no mains notch at 50.0 Hz for a signal sampled at 100 Hz"
    ]


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
def test_a_window_whose_variance_overflows_is_no_signal(caplog: pytest.LogCaptureFixture) -> None:
    """Finite samples, a variance that is not: ``inf / inf > 0.6`` is false, it read ``good``."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), _with(CLEAN, {4000: 1e200}))
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == ["ECG window not graded: its variance is not a finite positive number"]


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
@pytest.mark.parametrize(
    "given_back",
    [[math.nan] * WINDOW, [1e200, -1e200] * (WINDOW // 2), _with(CLEAN, {4000: math.inf})],
    ids=["all_nan", "a_variance_that_overflows", "one_infinity"],
)
def test_a_mains_filter_that_gives_back_non_finite_values_is_no_signal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, given_back: list[float]
) -> None:
    """The window is a clean heart: it is what the notch gives back that is not a number.

    Its variance is then NaN or infinite, the share NaN or minus infinity,
    and neither is above 0.6: without the check on THAT variance the window
    is graded ``good`` on a mains test that established nothing.
    """
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    notched = dsp.as_signal(given_back)

    def answer(*_filter_and_signal: object) -> dsp.Signal:
        return notched

    _zero_phase_filter(monkeypatch, answer)
    grade = _grade(_ecg_processor(), CLEAN)
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == ["ECG window not graded: its variance is not a finite positive number"]


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
def test_a_window_whose_variance_overflows_is_no_signal_whatever_the_notch_gives_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The variance that is divided by is infinite, and what the notch gives back is finite.

    The share would then be exactly 1: "all of it is hum", read off an
    overflow. The check on the window's own variance is what refuses it; the
    real notch hides that, because it gives the overflow back.
    """
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    notched = dsp.as_signal(CLEAN)

    def answer(*_filter_and_signal: object) -> dsp.Signal:
        return notched

    _zero_phase_filter(monkeypatch, answer)
    grade = _grade(_ecg_processor(), _with(CLEAN, {4000: 1e200}))
    said = _warnings(caplog)
    assert grade == NO_SIGNAL
    assert said == ["ECG window not graded: its variance is not a finite positive number"]


def test_a_window_without_variance_has_no_mains_share_and_nothing_is_divided_by_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asked of the helper itself: the grader stops a flat window before it gets there.

    A constant window has a variance of exactly zero. The helper answers that
    the share cannot be established; dividing by it would raise instead.
    """
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    share = _share(_ecg_processor(), [ADC_MID] * WINDOW)
    said = _warnings(caplog)
    assert share is None
    assert said == ["ECG window not graded: its variance is not a finite positive number"]


def test_the_mains_share_is_what_the_notch_removes() -> None:
    """Nearly all of pure hum, nearly nothing of a clean heart: 0.6 sits far from both."""
    processor = _ecg_processor()
    of_hum = _share(processor, _sine(50.0, 200.0, WINDOW))
    of_a_heart = _share(processor, CLEAN)
    assert of_hum is not None
    assert of_a_heart is not None
    assert of_hum > 0.99
    assert 0.0 <= of_a_heart < 0.05


# =========================================================================
# The log: bounded, whatever the windows are
# =========================================================================

CLEAR: Final[int] = 25
"""Judged windows in a row that close a stretch: five seconds at five batches a second."""

NOT_FINITE: Final[dsp.Signal] = dsp.as_signal(_with(CLEAN, {4000: math.nan}))
"""A window the grade cannot judge, already an array: these tests grade hundreds."""

JUDGED: Final[Mapping[str, dsp.Signal]] = {
    "good": dsp.as_signal(CLEAN),
    "flat": dsp.as_signal([ADC_MID] * WINDOW),
    "railed": dsp.as_signal(RAILED),
    "hum": dsp.as_signal(_sine(50.0, 200.0, WINDOW)),
}
"""One window per grade the grader reaches by judging: good, no_signal, noisy, mains_dominated."""

BEGAN: Final[str] = "ECG window not graded: 1 of its 8000 samples are not finite"


def _over(count: int) -> str:
    return f"ECG windows graded again after {count} that could not be"


def _grade_many(processor: Processor, window: dsp.Signal, times: int) -> None:
    for _ in range(times):
        _grade_array(processor, window)


def test_a_stretch_is_over_after_five_seconds_of_judged_windows() -> None:
    """The figure the bound below rests on, read from the module."""
    assert LEGACY.UNGRADED_CLEAR_WINDOWS == CLEAR


def test_a_long_stretch_of_ungradable_windows_writes_two_lines_not_one_per_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """70 s of it at five windows a second were 350 lines on the SD card: they are two.

    One when the stretch begins, with its reason. One when it is over, with
    the count, and only once 25 windows in a row were judged: not at 24.
    """
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    _grade_many(processor, NOT_FINITE, 350)
    during = _warnings(caplog)
    _grade_many(processor, JUDGED["good"], CLEAR - 1)
    not_yet = _warnings(caplog)
    _grade_many(processor, JUDGED["good"], 1)
    after = _warnings(caplog)
    assert during == [BEGAN]
    assert not_yet == [BEGAN]
    assert after == [BEGAN, _over(350)]


@pytest.mark.parametrize("kind", list(JUDGED))
def test_every_window_that_was_judged_counts_towards_the_end_of_a_stretch(
    caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    """A flat lead, clipping and hum are judgements too: any of them closes the stretch."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    _grade_many(processor, NOT_FINITE, 1)
    _grade_many(processor, JUDGED[kind], CLEAR)
    said = _warnings(caplog)
    assert said == [BEGAN, _over(1)]


def test_an_ungradable_window_among_judged_ones_starts_the_count_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """24 judged, one that is not, 24 judged: one stretch still, and nothing flickers in the log."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    _grade_many(processor, NOT_FINITE, 1)
    _grade_many(processor, JUDGED["good"], CLEAR - 1)
    _grade_many(processor, NOT_FINITE, 1)
    _grade_many(processor, JUDGED["good"], CLEAR - 1)
    still_open = _warnings(caplog)
    _grade_many(processor, JUDGED["good"], 1)
    closed = _warnings(caplog)
    assert still_open == [BEGAN]
    assert closed == [BEGAN, _over(2)]


@pytest.mark.filterwarnings("ignore:overflow encountered:RuntimeWarning")
@pytest.mark.filterwarnings("ignore:invalid value encountered:RuntimeWarning")
def test_a_stretch_writes_its_first_reason_and_counts_every_window_whatever_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four refusals in a row for four reasons: one line, the first reason, and a count of 4."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    _grade_array(processor, NOT_FINITE)
    _grade(processor, CLEAN, 1e-9)
    _grade(processor, _with(CLEAN, {4000: 1e200}))
    _grade(processor, CLEAN[: FS - 1])
    during = _warnings(caplog)
    _grade_many(processor, JUDGED["good"], CLEAR)
    after = _warnings(caplog)
    assert during == [BEGAN]
    assert after == [BEGAN, _over(4)]


def test_a_stretch_that_is_over_leaves_the_next_one_its_own_two_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    _grade_many(processor, NOT_FINITE, 3)
    _grade_many(processor, JUDGED["good"], CLEAR)
    _grade(processor, CLEAN[: FS - 1])
    _grade_many(processor, JUDGED["flat"], CLEAR)
    said = _warnings(caplog)
    assert said == [
        BEGAN,
        _over(3),
        "ECG window not graded: 999 samples are less than a second at 1000 Hz",
        _over(1),
    ]


def test_judged_windows_alone_write_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """No stretch, no line: not after 25 judged windows either."""
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    processor = _ecg_processor()
    for window in JUDGED.values():
        _grade_many(processor, window, CLEAR + 5)
    said = _warnings(caplog)
    assert said == []


class Lines(logging.Handler):
    """What the module writes at WARNING, for a property that cannot use ``caplog``."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.lines: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


SHORT_GOOD: Final[dsp.Signal] = dsp.as_signal(CLEAN[:FS])
SHORT_NOT_FINITE: Final[dsp.Signal] = dsp.as_signal(_with(CLEAN[:FS], {500: math.nan}))
"""One-second windows, the shortest the grader judges: the property grades thousands."""


@settings(deadline=None, max_examples=80, suppress_health_check=[HealthCheck.too_slow])
@given(pattern=st.lists(st.booleans(), max_size=160))
def test_property_the_log_holds_at_most_two_lines_per_twenty_six_windows(
    pattern: list[bool],
) -> None:
    """Whatever the order of gradable and ungradable windows, the log is bounded.

    A stretch that was closed took at least one ungradable window and 25
    judged ones, and wrote two lines; at most one stretch is still open, with
    one line. The lines alternate, open then close, and every ungradable
    window is counted exactly once by the line that closes its stretch.
    """
    module_log = logging.getLogger(MODULE_NAME)
    written = Lines()
    module_log.addHandler(written)
    try:
        processor = _ecg_processor()
        for ungradable in pattern:
            _grade_array(processor, SHORT_NOT_FINITE if ungradable else SHORT_GOOD)
    finally:
        module_log.removeHandler(written)

    lines = written.lines
    opened, closed = lines[0::2], lines[1::2]
    counted = sum(int(line.split()[5]) for line in closed)
    refused = pattern.count(True)
    assert len(lines) <= 2 * (len(pattern) // (CLEAR + 1)) + 1
    assert all(line.startswith("ECG window not graded: ") for line in opened)
    assert all(line.startswith("ECG windows graded again after ") for line in closed)
    if len(lines) % 2 == 0:
        assert counted == refused
    else:
        assert counted < refused


# =========================================================================
# The grades that did not change
# =========================================================================


def test_a_clean_heart_is_good_and_nothing_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    grade = _grade(_ecg_processor(), CLEAN)
    said = _warnings(caplog)
    assert grade == GOOD
    assert said == []


def test_one_second_is_enough_to_grade_and_one_sample_less_is_not() -> None:
    processor = _ecg_processor()
    enough = _grade(processor, CLEAN[:FS])
    short = _grade(processor, CLEAN[: FS - 1])
    empty = _grade(processor, [])
    assert (enough, short, empty) == (GOOD, NO_SIGNAL, NO_SIGNAL)


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ([ADC_MID] * WINDOW, NO_SIGNAL),
        ([510.0, 514.0] * (WINDOW // 2), NO_SIGNAL),
        (_with([ADC_MID] * WINDOW, {4000: 540.0}), NO_SIGNAL),
        ([0.0, 1023.0] * (WINDOW // 2), NOISY),
        ([1.0 if i % 3 else ADC_MID + (i % 50) for i in range(WINDOW)], NOISY),
        (_sine(50.0, 200.0, WINDOW), MAINS_DOMINATED),
        (
            [c + 300.0 * math.sin(2.0 * math.pi * 50.0 * i / FS) for i, c in enumerate(CLEAN)],
            MAINS_DOMINATED,
        ),
        ([c + 5.0 * math.sin(2.0 * math.pi * 50.0 * i / FS) for i, c in enumerate(CLEAN)], GOOD),
    ],
    ids=[
        "flat",
        "span_of_four_counts",
        "one_blip_on_a_flat_line",
        "railed",
        "two_thirds_at_the_low_rail",
        "pure_hum",
        "hum_over_a_heart",
        "a_little_hum_over_a_heart",
    ],
)
def test_the_grade_of_a_finite_window_is_what_it_was(window: list[float], expected: str) -> None:
    """EX-4: flat lead, clipping and mains hum are graded as before."""
    grade = _grade(_ecg_processor(), window)
    assert grade == expected


@pytest.mark.parametrize("mains_hz", [60.0, 449.9])
def test_another_mains_frequency_the_notch_exists_for_still_grades(mains_hz: float) -> None:
    grade = _grade(_ecg_processor(), CLEAN, mains_hz)
    assert grade == GOOD


def test_hum_at_the_frequency_asked_for_is_what_is_looked_for() -> None:
    hum_60 = _sine(60.0, 200.0, WINDOW)
    processor = _ecg_processor()
    at_60 = _grade(processor, hum_60, 60.0)
    at_50 = _grade(processor, hum_60, 50.0)
    assert (at_60, at_50) == (MAINS_DOMINATED, GOOD)


# =========================================================================
# What follows through process(): the reading the bridge is handed
# =========================================================================


def _feed(processor: Processor, raw: Sequence[float]) -> list[tuple[int, Mapping[str, object]]]:
    """``raw`` in 200 ms batches: after each, the freshness counter and the metrics."""
    seen: list[tuple[int, Mapping[str, object]]] = []
    for start in range(0, len(raw), BLOCK):
        _treated, metrics = processor.process(list(raw[start : start + BLOCK]))
        seen.append((processor.metric_seq, metrics))
    return seen


def _rate(metrics: Mapping[str, object]) -> int:
    rate = metrics.get(HEART_RATE_KEY)
    assert isinstance(rate, int), metrics
    return rate


def test_a_clean_heart_gives_a_fresh_good_reading_with_its_rate() -> None:
    """EX-4: nothing before 3 s, BioSPPy needs 4.6 s, then one fresh reading per batch."""
    seen = _feed(_ecg_processor(), CLEAN)
    assert all(seq == 0 and metrics == {} for seq, metrics in seen[:22])
    fresh = seen[22:]
    assert [seq for seq, _ in fresh] == list(range(1, len(fresh) + 1))
    assert all(metrics[QUALITY_KEY] == GOOD for _, metrics in fresh)
    assert all(abs(_rate(metrics) - 70) <= 2 for _, metrics in fresh)
    assert all(isinstance(metrics.get("hrv"), int) for _, metrics in fresh)


def test_a_non_finite_sample_gives_fresh_no_signal_readings_until_it_leaves_the_window(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """EX-2, as the bridge sees it. One NaN at 6 s stays 8 s in the window.

    Before: the grade stayed ``good``, BioSPPy raised on the poisoned window,
    the exception was swallowed, and the previous ``good`` reading with its
    rate was re-emitted for 8 s with a frozen freshness counter. Now each of
    those 40 batches is a fresh reading that says ``no_signal`` and carries no
    rate; the rate is back with the first window the sample has left.

    The log holds two lines for the whole episode, not forty: one for the
    first window refused (6.2 s long then, the window was still filling),
    one five seconds of judged windows after the last, with the count.
    """
    caplog.set_level(logging.WARNING, logger=MODULE_NAME)
    stream = _with(_ecg(70.0, (71 + CLEAR) * BLOCK), {30 * BLOCK + 50: math.nan})
    seen = _feed(_ecg_processor(), stream)
    said = _warnings(caplog)

    before_seq, before = seen[29]
    assert before[QUALITY_KEY] == GOOD
    assert abs(_rate(before) - 70) <= 2

    poisoned = seen[30:70]
    assert [metrics for _, metrics in poisoned] == [{QUALITY_KEY: NO_SIGNAL}] * 40
    assert [seq for seq, _ in poisoned] == list(range(before_seq + 1, before_seq + 41))

    after_seq, after = seen[70]
    assert after_seq == before_seq + 41
    assert after[QUALITY_KEY] == GOOD
    assert abs(_rate(after) - 70) <= 2

    assert said == ["ECG window not graded: 1 of its 6200 samples are not finite", _over(40)]


def test_a_window_that_cannot_be_graded_never_reaches_biosppy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rate is extracted under any grade but ``good``: the extractor is not even called."""
    scripted = ScriptedEcg(heart_rate=[70.0], rpeaks=[100, 950, 1800])
    monkeypatch.setattr(LEGACY, "bio_ecg", scripted)
    processor = _ecg_processor()
    _treated, metrics = processor.process(_with(CLEAN, {4000: math.inf}))
    assert metrics == {QUALITY_KEY: NO_SIGNAL}
    assert scripted.calls == 0
    _treated, metrics = processor.process([ADC_MID] * WINDOW)
    assert metrics == {QUALITY_KEY: NO_SIGNAL}
    assert scripted.calls == 0


# =========================================================================
# The arithmetic on what BioSPPy returns, and what its refusal leaves
# =========================================================================


class ScriptedEcg:
    """Stands in for ``biosppy.signals.ecg``: answers what the test scripted, or raises it."""

    def __init__(
        self,
        heart_rate: Sequence[float] = (),
        rpeaks: Sequence[int] = (),
        error: Exception | None = None,
    ) -> None:
        self.heart_rate: Sequence[float] = heart_rate
        self.rpeaks: Sequence[int] = rpeaks
        self.error: Exception | None = error
        self.calls: int = 0
        self.rates: list[int] = []

    def ecg(self, *, signal: object, sampling_rate: int, show: bool) -> Mapping[str, object]:
        del signal
        assert show is False, "the extractor must never open a plot"
        self.calls += 1
        self.rates.append(sampling_rate)
        if self.error is not None:
            raise self.error
        return {"heart_rate": self.heart_rate, "rpeaks": self.rpeaks}


def _scripted(
    monkeypatch: pytest.MonkeyPatch, scripted: ScriptedEcg
) -> tuple[Processor, Mapping[str, object]]:
    """One clean window through a processor whose extractor is ``scripted``."""
    monkeypatch.setattr(LEGACY, "bio_ecg", scripted)
    processor = _ecg_processor()
    _treated, metrics = processor.process(list(CLEAN))
    return processor, metrics


def test_the_rate_is_the_rounded_median_and_the_hrv_the_rmssd_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R peaks at 100, 950, 1820 and 2650 ms: RR 850, 870, 830; differences 20, -40.

    RMSSD = sqrt((400 + 1600) / 2) = 31.6 ms. The median of 69.2, 70.4 and 71.9 is 70.4.
    """
    scripted = ScriptedEcg(heart_rate=[71.9, 69.2, 70.4], rpeaks=[100, 950, 1820, 2650])
    processor, metrics = _scripted(monkeypatch, scripted)
    assert metrics == {QUALITY_KEY: GOOD, HEART_RATE_KEY: 70, "hrv": 32}
    assert processor.metric_seq == 1
    assert scripted.rates == [FS]


def test_two_beats_give_a_rate_and_no_hrv(monkeypatch: pytest.MonkeyPatch) -> None:
    """One RR interval has no successive difference: no HRV is made up from it."""
    _, metrics = _scripted(monkeypatch, ScriptedEcg(heart_rate=[71.6], rpeaks=[100, 938]))
    assert metrics == {QUALITY_KEY: GOOD, HEART_RATE_KEY: 72}


def test_beats_without_a_rate_give_an_hrv_and_no_heart_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BioSPPy drops rates outside 40-200 bpm: RR 2000 and 2020 ms, RMSSD 20 ms, no rate."""
    scripted = ScriptedEcg(heart_rate=[], rpeaks=[100, 2100, 4120])
    processor, metrics = _scripted(monkeypatch, scripted)
    assert metrics == {QUALITY_KEY: GOOD, "hrv": 20}
    assert processor.metric_seq == 1


def test_a_good_window_with_nothing_to_measure_says_good_and_no_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, metrics = _scripted(monkeypatch, ScriptedEcg(heart_rate=[], rpeaks=[100, 2100]))
    assert metrics == {QUALITY_KEY: GOOD}


@pytest.mark.parametrize("rate", [math.nan, math.inf, -math.inf])
def test_a_rate_that_is_not_a_number_never_becomes_a_heart_rate(
    monkeypatch: pytest.MonkeyPatch, rate: float
) -> None:
    """A NaN median cannot be rounded: nothing fresh comes out, least of all a number."""
    scripted = ScriptedEcg(heart_rate=[70.0, rate, rate], rpeaks=[100, 950, 1820])
    processor, metrics = _scripted(monkeypatch, scripted)
    assert metrics == {}
    assert processor.metric_seq == 0
    assert scripted.calls == 1


@pytest.mark.parametrize(
    "error",
    [ValueError("Not enough beats to compute heart rate."), IndexError("index 0"), KeyError("x")],
    ids=["value_error", "index_error", "key_error"],
)
def test_biosppy_refusing_a_window_leaves_nothing_fresh(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    """The freshness contract the runtime relies on: a failed extraction is NOT a new reading.

    The previous dict is re-emitted (which is why the runtime gates on the
    counter, never on the dict), the counter stays where it was, and the
    reason is in the debug log.
    """
    caplog.set_level(logging.DEBUG, logger=MODULE_NAME)
    scripted = ScriptedEcg(heart_rate=[70.2], rpeaks=[100, 950, 1820])
    processor, first = _scripted(monkeypatch, scripted)
    assert first == {QUALITY_KEY: GOOD, HEART_RATE_KEY: 70, "hrv": 20}

    scripted.error = error
    _treated, again = processor.process(list(CLEAN[:BLOCK]))
    assert again == first
    assert processor.metric_seq == 1
    assert scripted.calls == 2
    assert f"Metric extraction failed for ECG: {error}" in caplog.text


def test_real_biosppy_refuses_a_window_under_four_and_a_half_seconds() -> None:
    """Graded good from 3 s on, but BioSPPy's filter needs 4.5 s: nothing fresh until then."""
    processor = _ecg_processor()
    _treated, metrics = processor.process(list(CLEAN[: 4 * FS]))
    assert metrics == {}
    assert processor.metric_seq == 0


def test_nothing_is_computed_before_three_seconds_of_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted = ScriptedEcg(heart_rate=[70.0], rpeaks=[100, 950, 1800])
    monkeypatch.setattr(LEGACY, "bio_ecg", scripted)
    processor = _ecg_processor()
    _treated, metrics = processor.process(list(CLEAN[: 3 * FS - 1]))
    assert (metrics, processor.metric_seq, scripted.calls) == ({}, 0, 0)
    _treated, metrics = processor.process([CLEAN[3 * FS - 1]])
    assert (metrics[QUALITY_KEY], processor.metric_seq, scripted.calls) == (GOOD, 1, 1)


def test_an_empty_batch_re_emits_the_previous_metrics_and_is_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processor, first = _scripted(monkeypatch, ScriptedEcg(heart_rate=[70.0], rpeaks=[100, 950]))
    treated, again = processor.process([])
    assert treated == []
    assert again == first
    assert again is not first
    assert processor.metric_seq == 1


# =========================================================================
# The other channels
# =========================================================================


DEAD: Final[tuple[float, ...]] = (0.0,) * WINDOW
"""A dead input, for the tests that hand BioSPPy nothing to find.

Zero and not mid-scale, on purpose. A constant of 512 counts leaves each
filter a residue of the order of 1e-13 whose pattern depends on the platform,
and BioSPPy's detectors put their threshold on that residue: the same flat
line counted no muscle activation on macOS and one on the Linux runner. Zeros
filter to exact zeros everywhere, so what is asserted is the extractor's
answer to "nothing there", not the rounding of a machine.
"""


def _once(channel: str, raw: Sequence[float]) -> tuple[int, Mapping[str, object]]:
    """One window through a new processor for ``channel``: the counter and the metrics."""
    processor = LEGACY.ChannelProcessor(channel, FS, 250)
    _treated, metrics = processor.process(list(raw))
    return processor.metric_seq, metrics


def test_respiration_reports_a_rate_in_breaths_per_minute() -> None:
    """Two breaths at 15 per minute in the window: BioSPPy reads 13.4 from their zero crossings."""
    seq, metrics = _once("RESP", _sine(0.25, 200.0, WINDOW))
    rate = metrics.get("respRate")
    assert seq == 1
    assert isinstance(rate, float)
    assert 12.0 <= rate <= 16.0


def test_respiration_without_a_breath_reports_nothing() -> None:
    """No zero crossing, so BioSPPy answers an empty rate: no number is made of it."""
    seq, metrics = _once("RESP", DEAD)
    assert (seq, metrics) == (0, {})


def test_skin_conductance_counts_its_responses() -> None:
    """A slow rise carrying one sharp response at 3 s and one at 6 s."""
    raw = [
        300.0
        + 0.002 * i
        + (80.0 * math.exp(-(i - 3000) / 400.0) if i >= 3000 else 0.0)
        + (60.0 * math.exp(-(i - 6000) / 400.0) if i >= 6000 else 0.0)
        for i in range(WINDOW)
    ]
    seq, metrics = _once("EDA", raw)
    count = metrics.get("scrCount")
    assert seq == 1
    assert isinstance(count, int)
    assert count >= 1


def test_muscle_activity_counts_activations_and_none_on_a_dead_input() -> None:
    bursts = [
        ADC_MID
        + (150.0 if 2000 <= i < 3000 or 5000 <= i < 6000 else 0.0)
        * math.sin(2.0 * math.pi * 70.0 * i / FS)
        for i in range(WINDOW)
    ]
    active_seq, active = _once("EMG", bursts)
    dead_seq, dead = _once("EMG", DEAD)
    found = active.get("activations")
    assert isinstance(found, int)
    assert found >= 1
    assert (active_seq, dead_seq, dead) == (1, 1, {"activations": 0})


def test_the_pulse_wave_reports_a_pulse() -> None:
    seq, metrics = _once("SpO2", _sine(1.2, 200.0, WINDOW))
    assert (seq, metrics) == (1, {"pulse": 72})


def test_a_pulse_wave_too_slow_to_be_a_pulse_reports_nothing() -> None:
    """30 beats a minute is under BioSPPy's 40 bpm floor: beats, an empty rate, no number."""
    seq, metrics = _once("SpO2", _sine(0.5, 200.0, WINDOW))
    assert (seq, metrics) == (0, {})


def test_a_dead_pulse_wave_makes_biosppy_raise_and_reports_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BioSPPy raises ``IndexError`` here, not ``ValueError``: why the catch stays broad."""
    caplog.set_level(logging.DEBUG, logger=MODULE_NAME)
    seq, metrics = _once("SpO2", DEAD)
    assert (seq, metrics) == (0, {})
    assert "Metric extraction failed for SpO2: index 0 is out of bounds" in caplog.text


@pytest.mark.parametrize("channel", ["LUX", "ACC"])
def test_a_channel_without_an_extractor_reports_no_metric(channel: str) -> None:
    """The light sensor has none; a channel nobody described gets the default treatment."""
    seq, metrics = _once(channel, _sine(1.0, 100.0, WINDOW))
    assert (seq, metrics) == (0, {})


def test_an_extractor_key_nobody_implements_reports_no_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = LEGACY.ChannelSpec("lowpass", 5.0, "raw", None, "temperature")
    monkeypatch.setitem(LEGACY.SENSOR_SPECS, "TEMP", spec)
    seq, metrics = _once("TEMP", _sine(1.0, 100.0, WINDOW))
    assert (seq, metrics) == (0, {})


# =========================================================================
# The treated waveform: streaming filter, units, decimation
# =========================================================================


def test_the_ecg_waveform_is_in_millivolts_with_its_baseline_removed() -> None:
    """341 counts of a 10 Hz wave are 1 mV at the electrode; the 512-count offset is gone."""
    one_millivolt = 1.0 / LEGACY.ECG_MV_PER_COUNT
    treated, _metrics = _ecg_processor().process(_sine(10.0, one_millivolt, 4 * FS))
    settled = treated[len(treated) // 2 :]
    assert len(treated) == 4 * FS // DECIMATION
    assert max(settled) == pytest.approx(1.0, abs=0.05)
    assert min(settled) == pytest.approx(-1.0, abs=0.05)


def test_a_steady_input_gives_a_steady_output_from_the_first_sample() -> None:
    """The filter is primed with the first sample: no startup step on either kind of filter."""
    light, _metrics = LEGACY.ChannelProcessor("LUX", FS, 250).process([300.0] * FS)
    heart, _metrics = _ecg_processor().process([300.0] * FS)
    assert all(value == pytest.approx(300.0, abs=1e-6) for value in light)
    assert all(value == pytest.approx(0.0, abs=1e-6) for value in heart)


def test_batches_of_any_size_give_the_waveform_one_batch_would() -> None:
    """Filter state and decimation phase are carried: no gap, no repeat, no edge artefact."""
    raw = list(CLEAN[:1000])
    whole, _metrics = _ecg_processor().process(raw)
    pieces = _ecg_processor()
    streamed: list[float] = []
    start = 0
    for size in (3, 5, 1, 7, 200, 2, 400, 382):
        treated, _metrics = pieces.process(raw[start : start + size])
        streamed.extend(treated)
        start += size
    assert start == len(raw)
    assert len(whole) == len(raw) // DECIMATION
    assert streamed == pytest.approx(list(whole), abs=1e-9)


@pytest.mark.parametrize(("fs_out", "kept"), [(250, 250), (0, 1), (1, 1), (4000, 1000), (300, 334)])
def test_the_output_rate_is_a_whole_division_of_the_input_rate(fs_out: int, kept: int) -> None:
    """Never faster than the input, never a rate of zero: 300 Hz asked gives every third sample."""
    treated, _metrics = LEGACY.ChannelProcessor("ECG", FS, fs_out).process(list(CLEAN[:FS]))
    assert len(treated) == kept


# =========================================================================
# SignalTreatment: one processor per channel
# =========================================================================


def test_a_treatment_keeps_one_processor_per_channel_and_labels_what_it_returns() -> None:
    treatment = load_treatment(FS, 250)
    before = treatment.metric_seq(ECG_CHANNEL)
    batch: Sequence[Mapping[str, object]] = [
        {"channel": ECG_CHANNEL, "values": list(CLEAN)},
        {"channel": "RESP", "values": _sine(0.25, 200.0, WINDOW)},
        {"channel": "LUX"},
    ]
    treated, metrics = treatment.treat_batch(batch)
    after = {name: treatment.metric_seq(name) for name in (ECG_CHANNEL, "RESP", "LUX", "EDA")}
    labels = [(entry["channel"], entry["unit"]) for entry in treated]
    lengths = [len(cast("list[float]", entry["values"])) for entry in treated]
    assert before == 0
    assert labels == [(ECG_CHANNEL, "mV"), ("RESP", "raw"), ("LUX", "raw")]
    assert lengths == [2000, 2000, 0]
    assert set(metrics) == {ECG_CHANNEL, "RESP"}
    assert metrics[ECG_CHANNEL][QUALITY_KEY] == GOOD
    assert after == {ECG_CHANNEL: 1, "RESP": 1, "LUX": 0, "EDA": 0}

    _treated, metrics = treatment.treat_batch(
        [{"channel": ECG_CHANNEL, "values": [ADC_MID] * WINDOW}]
    )
    later = treatment.metric_seq(ECG_CHANNEL)
    assert metrics == {ECG_CHANNEL: {QUALITY_KEY: NO_SIGNAL}}
    assert later == 2


# =========================================================================
# Without BioSPPy
# =========================================================================


def test_without_biosppy_the_waveform_still_flows_and_no_metric_is_ever_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A machine without BioSPPy never has a heart rate: it says so once, and stays closed.

    The source file is executed a second time under another name, with the
    import made to fail, so the module every other test shares is left alone.
    """
    caplog.set_level(logging.WARNING)
    monkeypatch.setitem(sys.modules, "biosppy.signals", None)
    name = "signal_processing_without_biosppy"
    spec = importlib.util.spec_from_file_location(name, SOURCE)
    assert spec is not None
    assert spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, loaded)  # a dataclass looks its own module up there
    spec.loader.exec_module(loaded)
    bare = cast("Legacy", loaded)

    assert bare.BIOSPPY_AVAILABLE is False
    said = [record.getMessage() for record in caplog.records if record.name == name]
    assert len(said) == 1
    assert said[0].startswith("BioSPPy unavailable, metrics disabled: ")

    processor = bare.ChannelProcessor("ECG", FS, 250)
    treated, metrics = processor.process(list(CLEAN))
    assert len(treated) == WINDOW // DECIMATION
    assert (metrics, processor.metric_seq) == ({}, 0)
    assert LEGACY.BIOSPPY_AVAILABLE is True
