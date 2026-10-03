"""The sensor: synthetic RAW 10-bit ADC counts, so the real DSP does the work.

This module produces what a BITalino's A1 column produces - integers in
0..1023 - and nothing else. It does **not** produce a heart rate, a filtered
waveform or a quality grade. All three come out of the real
``src/signal_processing.py``, which is the entire point: a simulator that
handed the controller a clean number would prove nothing about the system that
actually exists, where the number arrives through a Butterworth filter, a
BioSPPy segmenter and a hand-written quality classifier, any of which can and
does produce a wrong answer.

**Why the morphology is a sum of Gaussians in cardiac phase.** BioSPPy's
segmenter is tuned for real ECG: it looks for the QRS complex's shape and slope
against a moving threshold. A triangle or square wave would be detected
trivially, or not at all, and either way would validate nothing about the
pipeline that has to work on a person. The five components below are the
ECGSYN parameter set (McSharry et al.), placed in phase rather than in time so
that they follow a changing heart rate without any beat bookkeeping:

    theta (deg)   amplitude   width (rad)
    P     -70        1.2         0.25      width scales with sqrt(RR)
    Q     -15       -5.0         0.10
    R       0       30.0         0.10
    S      15       -7.5         0.10
    T    +100        0.75        0.40      width scales with sqrt(RR)

The whole sum is then scaled so the R peak sits at
:attr:`EcgConfig.r_peak_mv`, ~1 mV, which is where a real lead-I R wave sits.
P and T widen with ``sqrt(RR)`` because at rest the atrial and repolarisation
waves are broad and at 160 bpm they are not; the QRS width is nearly
rate-independent in a real heart, so those three do not scale.

**Contaminants are added to the RAW signal, before the ADC conversion**, and
that is not a detail: ``_ecg_quality`` in the pipeline looks at the raw counts,
grading on standard deviation, on the fraction of samples pinned at the ADC
rails, and on how much variance a 50 Hz notch removes. A simulator that added
noise to a filtered signal, or in millivolts after conversion, would exercise
none of those three tests.

    * baseline wander - slow, respiratory; removed by the pipeline's 3-45 Hz
      band-pass, so it shows up in the quality grade and not in the waveform.
    * 50 Hz mains - small always, large during a ``MAINS_BURST``, which drives
      the grade to ``mains_dominated``.
    * electrode off - dead flat, which drives it to ``no_signal``.
    * clipping - a large enough baseline excursion saturates the front end and
      drives it to ``noisy``.
    * **motion noise that grows with g.** See below.

**MOTION NOISE IS THE LIKELY REAL FAILURE MODE, so it is modelled as a
coupling and not as a knob.** The rig shakes in proportion to the centripetal
load it is carrying, so the amplitude here is ``motion_mv_per_g * g``. The
consequence is a feedback path nobody designs on purpose: ECG quality degrades
*exactly* when the controller is pushing hardest, the heart rate goes missing
at the top of the speed range, and the supervisor freezes the setpoint there -
so the machine's usable range is decided by its own vibration and not by the
programme. With the defaults the grade goes ``noisy`` above about 1200 motor
rpm, which is 87% of nominal.

And the part that is worse than that, measured and pinned in
``tests/test_sim.py``: **well before the grade degrades, the reported heart
rate is already wrong.** A subject truly at 60 bpm is reported at 125 bpm at
600 motor rpm and 133 bpm at 900, both graded ``good``, because the pipeline
runs beat detection on anything it grades ``good`` and motion artifact in the
3-45 Hz pass band looks like beats. So the grade is clean across the entire
band where the number is already more than double the truth. ``quality ==
good`` is therefore necessary and **nowhere near sufficient**: the rate of
change has to be bounded too.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, final

from src.sim.physiology import ScriptedEvent, SubjectState
from src.units import (
    ADC_BASELINE,
    ADC_LEVELS,
    AdcCount,
    Millivolts,
    Seconds,
    millivolts_to_adc,
)

TWO_PI: Final[float] = 2.0 * math.pi

REFERENCE_RR: Final[Seconds] = Seconds(1.0)
"""The beat interval the widths above are quoted at: 1.0 s, i.e. 60 bpm."""

# --- The BITalino ECG transfer function ---------------------------------
#
# Duplicated from src/signal_processing.py rather than imported, and this is
# deliberate in both directions. That module is still outside both type
# checkers (see the migration banner in pyproject.toml), so importing it from
# here would drag its ~500 untyped diagnostics into this module's gate. But a
# second copy of a hardware constant is exactly the kind of thing that drifts
# silently, so tests/test_sim.py reads the real constant out of the pipeline
# and asserts it equals this one EXACTLY. A change there fails here, loudly,
# instead of quietly rescaling every synthetic signal in CI.
BITALINO_VCC_VOLTS: Final[float] = 3.3
ECG_SENSOR_GAIN: Final[float] = 1100.0
MILLIVOLTS_PER_VOLT: Final[float] = 1000.0

ECG_MV_PER_COUNT: Final[float] = (
    BITALINO_VCC_VOLTS / ADC_LEVELS / ECG_SENSOR_GAIN * MILLIVOLTS_PER_VOLT
)
"""Millivolts per ADC count for a DC-removed ECG channel: ~0.00293 mV/count.

So a 1 mV R wave is ~341 counts on a 1024-count scale. Used with
``src.units.millivolts_to_adc``, which is the exact inverse of the
``adc_to_millivolts`` the pipeline applies, so a synthetic signal enters the
real DSP through precisely the transform the DSP will undo."""


# =========================================================================
# Morphology
# =========================================================================


@unique
class WaveComponent(Enum):
    """The five deflections of one cardiac cycle.

    An enum rather than a string key so a component cannot be addressed by a
    typo, and so :data:`MORPHOLOGY` is total by construction.
    """

    P = "p"
    Q = "q"
    R = "r"
    S = "s"
    T = "t"


@dataclass(frozen=True, slots=True)
class GaussianWave:
    """One Gaussian deflection, placed in cardiac phase rather than in time.

    Phase rather than time is what lets the heart rate change continuously: a
    beat is "the phase went round once", so there is no beat schedule to keep
    consistent with a varying RR interval and no discontinuity when it varies.
    """

    centre_deg: float
    """Where in the cycle this deflection sits, degrees, R wave at zero."""

    amplitude: float
    """Signed height, in the arbitrary units of the ECGSYN parameter set. The
    sum is normalised to :attr:`EcgConfig.r_peak_mv` afterwards, so only the
    ratios between these matter."""

    width: float
    """Standard deviation in radians of phase."""

    width_scales_with_rr: bool
    """Whether this deflection widens as the beat interval grows.

    True for P and T, whose durations do track heart rate; false for Q, R and S,
    because the QRS width of a real heart is nearly rate-independent - and
    because a QRS that narrowed with rate would change what BioSPPy's segmenter
    sees, turning a physiology change into a detection change and making every
    downstream test ambiguous."""

    def at(self, phase: float, *, width_scale: float) -> float:
        """This deflection's contribution at ``phase`` radians.

        The phase difference is wrapped into ``[-pi, pi)`` before it is
        squared, so a deflection near the cycle boundary contributes on both
        sides of it rather than being cut in half.
        """
        width = self.width * width_scale if self.width_scales_with_rr else self.width
        delta = _wrapped(phase - math.radians(self.centre_deg))
        return self.amplitude * math.exp(-(delta * delta) / (2.0 * width * width))


MORPHOLOGY: Final[Mapping[WaveComponent, GaussianWave]] = MappingProxyType(
    {
        WaveComponent.P: GaussianWave(-70.0, 1.2, 0.25, width_scales_with_rr=True),
        WaveComponent.Q: GaussianWave(-15.0, -5.0, 0.10, width_scales_with_rr=False),
        WaveComponent.R: GaussianWave(0.0, 30.0, 0.10, width_scales_with_rr=False),
        WaveComponent.S: GaussianWave(15.0, -7.5, 0.10, width_scales_with_rr=False),
        WaveComponent.T: GaussianWave(100.0, 0.75, 0.40, width_scales_with_rr=True),
    }
)
"""The ECGSYN parameter set. Iterated in this order, so the sum is reproducible."""

PEAK_SEARCH_STEPS: Final[int] = 20_000
"""Phase grid used once, at import, to find the summed waveform's peak.

Fine enough that the peak is located to better than 1e-7 of its value: the R
wave's width is 0.10 rad, so 20000 steps over 2*pi put ~320 samples inside one
standard deviation of it."""


def _wrapped(angle: float) -> float:
    """``angle`` wrapped into ``[-pi, pi)``."""
    return (angle + math.pi) % TWO_PI - math.pi


def morphology_at(phase: float, *, width_scale: float) -> float:
    """The whole morphology at one phase, in ECGSYN units (before normalisation).

    Public because it is the one useful thing to evaluate without a
    synthesiser: a test asserting where the peak is, or a plot of the beat
    shape, should not have to render a signal and search it.
    """
    return math.fsum(wave.at(phase, width_scale=width_scale) for wave in MORPHOLOGY.values())


def _reference_peak() -> float:
    """The summed waveform's maximum at :data:`REFERENCE_RR`, in ECGSYN units.

    Computed rather than written down as ~29.59, because it is not simply the R
    wave's 30.0: Q and S are only 15 degrees away with a 0.10 rad width, so they
    subtract ~0.41 from the peak between them. A hand-copied constant here would
    put a silent 1.4% scale error into every synthetic signal.

    Evaluated once at import. It stays valid as the heart rate changes because
    only P and T widen, and at the R peak those two contribute less than 1e-4 of
    the total - which ``tests/test_sim.py`` asserts across 30..200 bpm rather
    than taking this paragraph's word for it.
    """
    return max(
        morphology_at(-math.pi + TWO_PI * step / PEAK_SEARCH_STEPS, width_scale=1.0)
        for step in range(PEAK_SEARCH_STEPS)
    )


MORPHOLOGY_PEAK: Final[float] = _reference_peak()
"""~29.5947 ECGSYN units at the R peak. The normaliser for :attr:`EcgConfig.r_peak_mv`."""


# =========================================================================
# The synthesiser
# =========================================================================


@dataclass(frozen=True, slots=True)
class EcgConfig:
    """Everything about the sensing chain, in one frozen record.

    The contaminant amplitudes are in millivolts at the electrode, i.e. before
    the ADC, so they are directly comparable with :attr:`r_peak_mv` - "the hum
    is a fifth of the R wave" is a sentence you can read off this record.
    """

    sample_rate: int = 1000
    """Hz. The BITalino's rate and the pipeline's ``fs_in``; 1000 is what the
    real client and ``SignalTreatment`` default to."""

    r_peak_mv: Millivolts = Millivolts(1.0)
    """R wave height. ~1 mV is a real lead-I R wave, and it lands at ~341 of
    the 1024 available counts, so a clean signal never approaches the rails."""

    baseline_mv: float = 0.15
    """Baseline wander amplitude. 15% of the R wave: visible on a screen,
    removed entirely by the pipeline's 3 Hz high-pass corner, and therefore
    only ever observable through the quality grade. Raise it to ~3 mV and the
    front end saturates, which is how the ``noisy`` grade is reached."""

    baseline_hz: float = 0.3
    """Wander frequency. Deliberately not equal to :attr:`respiration_hz`: two
    contaminants locked to the same frequency would beat together and hide
    whichever one a test was actually about."""

    mains_hz: float = 50.0
    """European mains. The pipeline's notch is at 50 Hz with Q=30, so this has
    to match it for ``mains_dominated`` to be reachable at all."""

    mains_mv: float = 0.02
    """Ambient hum, always present. Small enough that a clean signal still
    grades ``good``: the notch removes far less than the 60% of variance the
    classifier needs."""

    mains_burst_mv: float = 0.5
    """Hum during a ``MAINS_BURST`` window. Half the R wave, which is enough
    for the notch to remove most of the variance and for the grade to become
    ``mains_dominated``."""

    motion_mv_per_g: float = 3.5
    """Motion-artifact amplitude per g of centripetal load - the coupling this
    module exists to reproduce. Expressed per g rather than per rpm on purpose:
    the rig shakes in proportion to the load it is carrying, so this one number
    stays right when the radius or the gear ratio changes. At the nominal
    0.859 g it gives ~3.0 mV, which pins more than half the samples at the ADC
    rails and forces the grade to ``noisy``."""

    electrode_off_count: AdcCount = AdcCount(ADC_BASELINE)
    """What a detached electrode reads: a constant. Dead flat is what drives
    ``_ecg_quality`` to ``no_signal`` through its standard-deviation test, and a
    constant mid-scale value is also what the real front end floats to."""

    unmodelled_count: AdcCount = AdcCount(ADC_BASELINE)
    """What a non-ECG channel reads. Only the ECG channel is modelled; the
    others exist so a multi-channel frame has the right shape, and they are a
    constant so that nobody can mistake them for data."""

    respiration_hz: float = 0.25
    """Respiratory rate driving the sinus arrhythmia below. 15 breaths/min."""

    rr_variability: float = 0.03
    """Respiratory sinus arrhythmia, as a fraction of the beat interval.

    3% is small and real, and it is here for a specific reason: with a
    perfectly periodic RR the pipeline reports an HRV of exactly zero and a
    heart rate with no error at all, so a test asserting "the rate comes back
    within a few bpm" would be asserting something about arithmetic rather than
    about a beat detector. Set it to 0.0 for a metronome."""

    seed: int = 20_240_912
    """Seeds this synthesiser's own ``random.Random``. Per-instance, never the
    global RNG: a simulator that perturbed global random state would make every
    other test in the suite order-dependent."""

    def __post_init__(self) -> None:
        # Raises rather than returning a Result, for the reason EventWindow
        # does: this is built at startup with nothing spinning.
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {self.sample_rate}")
        if self.r_peak_mv <= 0.0:
            raise ValueError(f"r_peak_mv must be positive, got {self.r_peak_mv}")
        if not 0.0 <= self.rr_variability < 1.0:
            # At 1.0 the modulated beat interval reaches zero and the phase
            # advance divides by it. Below 0 it is a sign error.
            raise ValueError(f"rr_variability must be in [0, 1), got {self.rr_variability}")


DEFAULT_ECG_CONFIG: Final[EcgConfig] = EcgConfig()
"""Shared default. A module-level instance, not a call in a default argument."""


@final
class EcgSynthesizer:
    """Turns a :class:`~src.sim.physiology.SubjectState` into RAW ADC counts.

    **Mutable by design**: it carries the cardiac phase and the sample index
    across calls, which is what makes consecutive :meth:`render` calls
    gap-free and phase-continuous. Rendering the same block twice is therefore
    *not* idempotent, and that is correct - a BITalino does not hand out the
    same second of signal twice either.
    """

    __slots__ = ("_beat_rr", "_config", "_phase", "_rng", "_sample")

    def __init__(self, config: EcgConfig = DEFAULT_ECG_CONFIG) -> None:
        self._config: EcgConfig = config
        self._phase: float = -math.pi
        """Cardiac phase in radians, in [-pi, pi), with the R wave at 0. Starts
        at the beginning of a cycle so the first rendered block opens on a P
        wave rather than mid-QRS."""
        self._beat_rr: float = float(REFERENCE_RR)
        """The beat interval latched at the last cycle boundary, which is what
        the P and T widths scale with. Latched per beat rather than taken
        instantaneously because a deflection's width is a property of the beat
        it belongs to; letting it vary *within* a beat would smear the T wave
        in a way no heart does. The reference value here is a placeholder only:
        :meth:`render` replaces it on its first call, before anything is drawn."""
        self._sample: int = 0
        """Samples rendered since construction. The time base for the
        contaminants, so wander and hum stay continuous across blocks."""
        # S311 warns that Mersenne Twister is not cryptographic, which is
        # correct and irrelevant: this draws motion-artifact amplitudes for a
        # simulation, and a reproducible sequence is the REQUIREMENT rather
        # than a weakness. Nothing here is a secret, a token or a nonce.
        self._rng: random.Random = random.Random(config.seed)  # noqa: S311

    # -- observation -------------------------------------------------------

    @property
    def phase(self) -> float:
        """Current cardiac phase in radians. For tests and simulation logs."""
        return self._phase

    @property
    def samples_rendered(self) -> int:
        """How many samples this synthesiser has produced. For tests and logs."""
        return self._sample

    @property
    def beat_interval(self) -> Seconds:
        """The interval the CURRENT beat's P and T widths are scaled by.

        Exposed because "which beat's interval is this waveform shaped by" is
        the one piece of internal state whose behaviour is a documented promise
        (latched per beat, not per sample) and therefore has to be observable
        for that promise to be worth anything.
        """
        return Seconds(self._beat_rr)

    # -- rendering ---------------------------------------------------------

    def render(self, count: int, subject: SubjectState) -> tuple[AdcCount, ...]:
        """The next ``count`` RAW samples for ``subject``.

        The subject's state is held constant across the block, which is a
        first-order choice like the plant's own step: at the 5 Hz loop rate a
        block is 0.2 s and the fastest thing in the plant has a 2 s time
        constant, so the error is negligible - but a caller rendering minutes
        in one call is asking for a step response it will not get.

        Raises ``ValueError`` on a non-positive count or a non-positive beat
        interval, matching the rest of this package: it is the simulation
        harness, and a zero beat interval is a caller bug (the plant's
        ``hr_floor`` makes it unreachable from a real :class:`SubjectState`),
        not a measurement.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        if subject.rr_interval <= 0.0:
            raise ValueError(f"rr_interval must be positive, got {subject.rr_interval}")

        config = self._config
        if self._sample == 0:
            # The phase starts at a cycle boundary, so the very first beat is
            # opened by this call and its widths belong to this interval. Without
            # this the first beat would be shaped by REFERENCE_RR whatever the
            # subject's rate actually was - one visibly wrong beat at the start
            # of every session, which is exactly the kind of small silent error
            # this package exists to not have. At sample zero the respiratory
            # modulation is sin(0), so the raw and modulated intervals coincide.
            self._beat_rr = float(subject.rr_interval)
        flat = ScriptedEvent.ELECTRODE_OFF in subject.artifacts
        mains_mv = (
            config.mains_burst_mv
            if ScriptedEvent.MAINS_BURST in subject.artifacts
            else config.mains_mv
        )
        # The coupling. g is unsigned, so reverse rotation shakes the rig just
        # as hard - which is the physical truth and the pessimistic direction.
        motion_mv = config.motion_mv_per_g * float(subject.g_load)

        samples: list[AdcCount] = []
        for _ in range(count):
            moment = self._sample / config.sample_rate
            self._sample += 1
            value = (
                config.electrode_off_count
                if flat
                else self._contaminated(moment, mains_mv=mains_mv, motion_mv=motion_mv)
            )
            samples.append(value)
            self._advance_phase(subject.rr_interval, moment)
        return tuple(samples)

    def unmodelled(self, count: int) -> tuple[AdcCount, ...]:
        """``count`` samples for a channel this simulator does not model.

        A constant, so that a consumer which mistakes it for data sees a flat
        line and the pipeline grades it ``no_signal`` - rather than plausible
        noise nobody questions.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        return (self._config.unmodelled_count,) * count

    # -- internals ---------------------------------------------------------

    def _contaminated(self, moment: float, *, mains_mv: float, motion_mv: float) -> AdcCount:
        """One sample in millivolts, contaminated, then converted to a count.

        Conversion is the last step and goes through
        ``src.units.millivolts_to_adc``, which clamps to 0..1023. The clamp is
        not an error path: a real BITalino clips too, and the pipeline's
        quality classifier is *meant* to notice that it did.
        """
        config = self._config
        millivolts = self._beat_millivolts()
        millivolts += config.baseline_mv * math.sin(TWO_PI * config.baseline_hz * moment)
        millivolts += mains_mv * math.sin(TWO_PI * config.mains_hz * moment)
        # Always drawn, even at zero amplitude (gauss returns the mean exactly),
        # so the noise sequence depends on the sample index alone. If the draw
        # were conditional, a change of speed would shift the whole random
        # stream and two runs differing only in rpm would stop being comparable.
        millivolts += self._rng.gauss(0.0, motion_mv)
        return millivolts_to_adc(Millivolts(millivolts), ECG_MV_PER_COUNT)

    def _beat_millivolts(self) -> float:
        """The clean morphology at the current phase, scaled to ``r_peak_mv``."""
        width_scale = math.sqrt(self._beat_rr / REFERENCE_RR)
        summed = morphology_at(self._phase, width_scale=width_scale)
        return summed / MORPHOLOGY_PEAK * float(self._config.r_peak_mv)

    def _advance_phase(self, rr_interval: Seconds, moment: float) -> None:
        """Move one sample forward through the cardiac cycle.

        The beat interval is modulated by respiratory sinus arrhythmia first,
        and the modulated value is what gets latched at the cycle boundary, so
        the P and T widths of each beat match the interval that beat actually
        had.
        """
        config = self._config
        modulated = float(rr_interval) * (
            1.0 + config.rr_variability * math.sin(TWO_PI * config.respiration_hz * moment)
        )
        self._phase += TWO_PI / (modulated * config.sample_rate)
        if self._phase >= math.pi:
            self._phase -= TWO_PI
            self._beat_rr = modulated
