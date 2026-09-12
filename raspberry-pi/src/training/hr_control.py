"""The control law: a heart rate in, a motor-speed demand out.

This is the module whose misbehaviour is felt physically by a person strapped
into a centrifuge, so the arrangement below is chosen for what it makes
impossible rather than for what it makes elegant.

Two objects, in the order the data flows:

* :class:`HeartRateTracker` turns the metric stream into *trustworthy input*.
  It is a filter and a gate, not a controller: it decides which readings count
  as evidence at all. Three of its four rejections exist because of a specific
  way this pipeline lies (see the class docstring).
* :class:`HeartRateController` turns trustworthy input into a setpoint. A PI in
  **velocity (incremental) form**, re-based every tick on the rpm actually
  applied by the drive, with the clamps, the slew limit and the setpoint domain
  applied at the single exit of :meth:`HeartRateController.update`.

Four decisions carry most of the safety here, and each one is a choice against
the obvious alternative:

1. **Velocity form, re-based on the APPLIED rpm.** The output *is* the integral
   state, so there is no separate accumulator to wind up. Each tick computes an
   increment and adds it to what the drive reports it is actually running at.
   If the safety supervisor has frozen the setpoint, overridden it, or clamped
   it, the next increment starts from that frozen reality rather than from a
   fantasy the controller kept to itself - so resumption after a FREEZE is
   bumpless with **no special case anywhere in this file**. A positional PI
   would need explicit anti-windup plus a resume path, i.e. two more code paths
   on the limb that moves the machine.
2. **No derivative term, deliberately.** The input is already an 8 s median
   refreshed at 1 Hz (``src/signal_processing.py``), quantised to whole bpm. A
   D term on that differentiates delay and quantisation, not physiology: one
   bpm of jitter over one sample would ask for a speed jolt, and the person
   feels every jolt. The measurement is smoothed; the actuator must not undo
   that.
3. **A 5 s control period, not the 5 Hz loop rate.** Heart rate lags load with
   a 30-60 s time constant behind 4-12 s of dead time. Deciding faster than 5 s
   cannot observe the consequence of the previous decision, so it does not
   regulate - it amplifies measurement noise into speed changes. The runtime
   loop still calls :meth:`HeartRateController.update` at 5 Hz; ticks inside
   the period re-emit the standing demand unchanged.
4. **An asymmetric deadband.** The no-correction band is inset further from the
   top of the zone (3 bpm) than from the bottom (1 bpm), so the machine starts
   backing off *before* the ceiling and tolerates drift toward the floor.
   Drifting low is the safe direction; drifting high is the one that hurts.

What this module does **not** do, on purpose: it never decides to stop, never
resets a fault, never reads a clock (``now`` is always a parameter, contract
rule 4), and has no authority over the safety supervisor. The verdict wins;
this object only ever proposes. The vasovagal case is why: a person beginning
to faint shows a *falling* heart rate, which this law reads as "below target"
and answers by accelerating. It is not wrong to compute that. It is wrong to be
obeyed.

See .claude/skills/anheart-strict-python/SKILL.md and ``src/training/types.py``.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median_low
from typing import Final, assert_never, final

from src.result import Err, Ok, Result
from src.training.types import (
    HEART_RATE_STALE_AFTER,
    ControlDecision,
    HeartRateSample,
    Phase,
    SignalQuality,
)
from src.units import (
    Bpm,
    BpmPerMinute,
    Monotonic,
    MotorRpm,
    RpmPerSecond,
    Seconds,
    elapsed,
)

# =========================================================================
# Physiological bounds - not configuration
# =========================================================================
#
# These are the limits of a human heart, not a knob on a session plan, so they
# live here as constants and both objects below use the same pair. Their job is
# narrower than it looks: they are the guard that keeps unbounded arithmetic out
# of the speed calculation. An absurd integer arriving as a "heart rate" - a
# mis-scaled metric, a corrupt frame, a test feeding 10**400 - would otherwise
# reach ``int * float`` and raise OverflowError *inside the control path*, which
# is the one place this system may not raise. Bounding the input instead means
# every downstream value is finite by construction, which is why this file has
# no ``isfinite`` guard on its own output: there is no path that can produce
# one. ``tests/test_hr_control.py`` proves that with a property test rather
# than trusting the argument.

MIN_PLAUSIBLE_BPM: Final[Bpm] = Bpm(25)
"""Below this is not a slow heart, it is a failed measurement.

A trained athlete at rest reaches the high 30s; sustained sub-25 bpm in a
conscious person in a centrifuge is a lead problem or a detector artefact.
"""

MAX_PLAUSIBLE_BPM: Final[Bpm] = Bpm(240)
"""Above this is not a fast heart, it is noise counted as beats.

220 minus age bounds any occupant this machine will ever carry; 240 leaves
headroom for a genuine tachycardia while still excluding mains hum (3000 bpm)
and double-counted R peaks.
"""


def _is_plausible(bpm: Bpm) -> bool:
    """Whether ``bpm`` could be a heart rate at all.

    Integer comparisons only - deliberately no float conversion, because the
    values this rejects are exactly the ones ``float()`` cannot represent.
    """
    return MIN_PLAUSIBLE_BPM <= bpm <= MAX_PLAUSIBLE_BPM


# =========================================================================
# The tracker: which readings count as evidence
# =========================================================================

DEFAULT_MAX_JUMP_BPM: Final[Bpm] = Bpm(25)
"""Largest step between two accepted readings that is treated as real.

The input is an 8 s median refreshed at 1 Hz, so consecutive values overlap by
seven eighths of their window. A 25 bpm step between them cannot come from a
heart; it comes from the median's window sliding over an artefact.
"""

DEFAULT_MEDIAN_WINDOW: Final[int] = 3
"""Readings the reported value is the median of. Three kills a lone outlier."""

DEFAULT_RATE_WINDOW: Final[int] = 10
"""Readings the rate of change is fitted over: ~10 s at the 1 Hz refresh."""

MIN_SAMPLES_FOR_RATE: Final[int] = 2
"""Fewest accepted readings a rate of change can be fitted over.

Two, because one point has no slope. Named rather than written inline so the
line that rejects a one-sample history says what it is rejecting.
"""

DEFAULT_RESEED_AFTER: Final[int] = 3
"""Consecutive jump rejections after which the tracker restarts from the newest.

Without this the jump gate can deadlock: a heart rate that genuinely moved more
than the limit while nothing was being measured would be rejected forever, and
a tracker that never accepts anything again reports an ever-growing age. That
is *detected* - the safety layer trips on staleness - but "permanently blind"
is a worse outcome than "accepted a large but repeatedly-confirmed change".
Three independent refreshes all landing more than the limit away from the old
value is no longer the signature of a single-sample outlier; it is the
signature of a real change plus a gap. The event is reported on the reading
(:attr:`TrackerReading.reseeded`) so a session log can show it happened.
"""


@dataclass(frozen=True, slots=True)
class TrackerLimits:
    """How strict the tracker is. Frozen: it cannot be relaxed mid-session."""

    max_jump: Bpm = DEFAULT_MAX_JUMP_BPM
    median_window: int = DEFAULT_MEDIAN_WINDOW
    rate_window: int = DEFAULT_RATE_WINDOW
    reseed_after: int = DEFAULT_RESEED_AFTER
    stale_after: Seconds = HEART_RATE_STALE_AFTER

    def __post_init__(self) -> None:
        """Refuse a configuration that cannot filter.

        Raising at construction rather than returning a ``Result``: this object
        is built at session setup with nothing spinning, and refusing to start
        is the right answer to limits nobody can vouch for. Contrast the drive
        path, where a raise would unwind with the motor commanded.
        """
        if self.max_jump < 1:
            raise ValueError(
                f"max_jump must be at least 1 bpm, got {self.max_jump}; a limit of "
                "zero rejects every change and the tracker would go permanently blind"
            )
        if self.median_window < 1:
            raise ValueError(f"median_window must be at least 1, got {self.median_window}")
        if self.rate_window < self.median_window:
            raise ValueError(
                f"rate_window {self.rate_window} is smaller than median_window "
                f"{self.median_window}; the rate is fitted over the retained history, so "
                "the history must be at least as long as the median window"
            )
        if self.reseed_after < 1:
            raise ValueError(
                f"reseed_after must be at least 1, got {self.reseed_after}; with zero the "
                "jump gate would never reject anything"
            )
        if not self.stale_after > 0.0:
            raise ValueError(f"stale_after must be positive, got {self.stale_after}")


@dataclass(frozen=True, slots=True)
class StaleSequence:
    """The sample repeated a sequence number, so it carries no new evidence.

    The one rejection that looks like healthy data on every screen:
    ``src/signal_processing.py`` re-emits its previous metrics dict when
    extraction fails, with a fresh timestamp and the old number.
    """

    seq: int
    previous_seq: int | None


@dataclass(frozen=True, slots=True)
class UnusableSample:
    """Either no rate was measured, or its signal quality forbids using one."""

    quality: SignalQuality
    bpm: Bpm | None


@dataclass(frozen=True, slots=True)
class ImplausibleRate:
    """The number is outside the range a human heart occupies."""

    bpm: Bpm
    low: Bpm
    high: Bpm


@dataclass(frozen=True, slots=True)
class ImplausibleJump:
    """The step from the previous accepted reading is too large to be real."""

    bpm: Bpm
    previous_bpm: Bpm
    limit: Bpm
    consecutive: int
    """How many jump rejections in a row this is; see :data:`DEFAULT_RESEED_AFTER`."""


type HeartRateRejection = StaleSequence | UnusableSample | ImplausibleRate | ImplausibleJump
"""Closed union. Adding a variant breaks every incomplete ``match`` at check time."""


@dataclass(frozen=True, slots=True)
class TrackerReading:
    """An accepted reading: the filtered value plus what it was derived from."""

    bpm: Bpm
    """The value to regulate on: the median of the last ``median_window`` accepted.

    Always one of the measured values, never an average - see
    :meth:`HeartRateTracker.observe`.
    """

    raw_bpm: Bpm
    """This sample's own value, before the median. For the log, not the law."""

    at: Monotonic
    seq: int

    window: int
    """How many accepted readings the median was taken over (1..median_window)."""

    reseeded: bool
    """Whether the jump gate was re-armed on this sample (history discarded)."""


@dataclass(frozen=True, slots=True)
class _Accepted:
    """One reading that passed every gate. Internal to the tracker's history."""

    bpm: Bpm
    at: Monotonic


@final
class HeartRateTracker:
    """Turns the raw metric stream into input a control law may act on.

    **Mutable, and one of the few mutable objects in this package** - it is a
    filter with history, so state is the point. It is owned by exactly one
    runtime loop and shared with nobody; every field is listed in ``__slots__``
    and changed only in :meth:`observe`.

    Four gates, in this order, and the order matters:

    1. **Freshness by sequence number.** A repeated ``seq`` is discarded
       *entirely* - not averaged in, not counted as a sample, not used to
       refresh the age. This is the gate the whole class exists for: the ECG
       pipeline re-emits its previous metrics dict whenever extraction fails,
       so an unchanged heart rate very often means *nothing was measured*. The
       number sits there looking alive, at 1 Hz, for as long as the failure
       lasts. Regulating a motor on it is regulating on a reading that may be
       minutes old, while every screen and log looks entirely normal.
    2. **Quality.** Only ``GOOD`` yields a usable rate
       (:attr:`~src.training.types.HeartRateSample.usable_bpm`). Nothing here
       ever substitutes a default for a missing vital sign.
    3. **Plausibility.** Outside 25..240 bpm is not a heart rate.
    4. **Jump.** More than ``max_jump`` from the previous accepted reading is an
       artefact, unless it is confirmed ``reseed_after`` times in a row.

    A sample that clears gate 1 always advances the sequence watermark, even if
    a later gate rejects it: the evidence was new, it was simply not good. Not
    advancing would leave the watermark able to accept that same ``seq`` again.

    What it exposes to the layers above is deliberately more than a number:
    :meth:`age` (how old the newest accepted reading is, so staleness is the
    caller's decision rather than a hidden one) and :attr:`rate` in bpm/min,
    which is the quantity the safety supervisor bounds.
    """

    __slots__ = ("_accepted", "_consecutive_jumps", "_last_seq", "_limits")

    def __init__(self, limits: TrackerLimits | None = None) -> None:
        self._limits: TrackerLimits = TrackerLimits() if limits is None else limits
        # Bounded history: the rate is fitted over all of it, the median over
        # its tail. A deque with maxlen cannot grow without bound over a
        # 45-minute session on a Pi.
        self._accepted: deque[_Accepted] = deque(maxlen=self._limits.rate_window)
        self._last_seq: int | None = None
        self._consecutive_jumps: int = 0

    @property
    def limits(self) -> TrackerLimits:
        """The limits this tracker was built with."""
        return self._limits

    def observe(self, sample: HeartRateSample) -> Result[TrackerReading, HeartRateRejection]:
        """Apply the four gates. Returns the accepted reading, or why it was not.

        A ``Result`` over a closed union rather than a bare ``None``: the
        callers of this method are the control law and the safety supervisor,
        and "the rate was rejected" is four materially different situations to
        them. A stale sequence means the pipeline has stopped producing; an
        unusable quality means the electrodes are the problem; an implausible
        rate means the detector is; a jump means one reading was an artefact.
        Collapsing them would throw away the only information that says which.

        Never raises, and takes no clock: the sample carries its own ``at``.
        """
        if not sample.is_new_evidence_after(self._last_seq):
            return Err(StaleSequence(seq=sample.seq, previous_seq=self._last_seq))
        # The watermark advances on new evidence, good or bad. See the class
        # docstring: a rejected sample has still been seen.
        self._last_seq = sample.seq

        usable = sample.usable_bpm
        if usable is None:
            return Err(UnusableSample(quality=sample.quality, bpm=sample.bpm))
        if not _is_plausible(usable):
            return Err(ImplausibleRate(bpm=usable, low=MIN_PLAUSIBLE_BPM, high=MAX_PLAUSIBLE_BPM))

        reseeded = False
        previous = self._accepted[-1] if self._accepted else None
        if previous is not None and abs(usable - previous.bpm) > self._limits.max_jump:
            self._consecutive_jumps += 1
            if self._consecutive_jumps < self._limits.reseed_after:
                return Err(
                    ImplausibleJump(
                        bpm=usable,
                        previous_bpm=previous.bpm,
                        limit=self._limits.max_jump,
                        consecutive=self._consecutive_jumps,
                    )
                )
            # Confirmed often enough to be a real change rather than an
            # artefact. Discard the history: mixing values from either side of
            # a confirmed step into a median would report a number that was
            # never measured.
            self._accepted.clear()
            reseeded = True
        self._consecutive_jumps = 0
        self._accepted.append(_Accepted(bpm=usable, at=sample.at))

        window = list(self._accepted)[-self._limits.median_window :]
        # median_low, not median: it returns one of the *measured* values
        # instead of averaging the two middle ones, so the number handed to the
        # control law is always a reading that actually happened. On an even
        # window it takes the lower of the two, which is the safe direction.
        filtered = Bpm(median_low(item.bpm for item in window))
        return Ok(
            TrackerReading(
                bpm=filtered,
                raw_bpm=usable,
                at=sample.at,
                seq=sample.seq,
                window=len(window),
                reseeded=reseeded,
            )
        )

    @property
    def filtered_bpm(self) -> Bpm | None:
        """The median of the retained window, or ``None`` before any acceptance."""
        if not self._accepted:
            return None
        window = list(self._accepted)[-self._limits.median_window :]
        return Bpm(median_low(item.bpm for item in window))

    @property
    def raw_bpm(self) -> Bpm | None:
        """The newest accepted reading's own value, or ``None``."""
        if not self._accepted:
            return None
        return self._accepted[-1].bpm

    @property
    def last_seq(self) -> int | None:
        """The highest sequence number seen, accepted or not."""
        return self._last_seq

    @property
    def sample_count(self) -> int:
        """How many accepted readings are retained."""
        return len(self._accepted)

    def age(self, now: Monotonic) -> Seconds | None:
        """How old the newest accepted reading is, or ``None`` if there is none.

        ``None`` rather than an enormous number, because "never measured" and
        "measured a long time ago" are different situations and a caller that
        cannot tell them apart will eventually present one as the other.
        """
        if not self._accepted:
            return None
        return elapsed(self._accepted[-1].at, now)

    def is_fresh(self, now: Monotonic) -> bool:
        """Whether the newest accepted reading is recent enough to act on.

        No reading at all is not fresh. Expressed as ``<=`` on a value known to
        be non-``None``, so a missing age can never read as fresh.
        """
        age = self.age(now)
        if age is None:
            return False
        return age <= self._limits.stale_after

    def usable(self, now: Monotonic) -> Bpm | None:
        """The filtered rate if it may be regulated on at ``now``, else ``None``.

        The one place the three gates join for a consumer: accepted, filtered,
        and fresh. This is what :attr:`ControlInput.bpm` should be built from.
        """
        if not self.is_fresh(now):
            return None
        return self.filtered_bpm

    @property
    def rate(self) -> BpmPerMinute | None:
        """Rate of change of the accepted readings, in bpm per minute.

        A least-squares slope over the retained window rather than a difference
        of its endpoints: the readings are whole bpm, so an endpoint difference
        has a 1 bpm quantisation step spread over a couple of seconds - tens of
        bpm/min of pure quantisation noise - and the safety supervisor trips on
        this number.

        ``None`` when there is not enough history to fit a line, when every
        retained reading carries the same timestamp (no time base, so no rate),
        or when the arithmetic could not produce a finite answer. A ``None``
        here means "unknown", and the supervisor must not read it as "zero".
        """
        history = list(self._accepted)
        if len(history) < MIN_SAMPLES_FOR_RATE:
            return None
        origin = history[0].at
        times = [item.at - origin for item in history]
        rates = [float(item.bpm) for item in history]
        count = len(history)
        # Plain summation and plain multiplication, and both choices are
        # deliberate: this method must not raise, and the two obvious
        # alternatives do. `math.fsum` raises ValueError when its terms include
        # both infinities, and `x ** 2` raises OverflowError where `x * x`
        # simply yields infinity. With absurd timestamps - which a test found,
        # and which a corrupt clock could produce - either would throw from
        # inside a method the safety supervisor calls every tick. Float addition
        # and multiplication never raise, so the pathological cases arrive here
        # as a NaN or an infinity and leave as `None` through the guards below.
        # The window holds a few dozen values of similar magnitude, so Kahan
        # summation would buy no accuracy worth that risk.
        mean_time = sum(times) / count
        mean_rate = sum(rates) / count
        variance = sum((time - mean_time) * (time - mean_time) for time in times)
        if not variance > 0.0:
            return None
        covariance = sum(
            (time - mean_time) * (rate - mean_rate) for time, rate in zip(times, rates, strict=True)
        )
        slope = covariance / variance * 60.0
        # One guard for both ways the fit can fail to mean anything. The
        # variance test matters as much as the slope test: with timestamps too
        # large to square, the spread comes out infinite and the slope comes out
        # as a perfectly finite ZERO - "the heart rate is not changing", from an
        # infinitely long window. That is the optimistic reading of "unknown",
        # and the supervisor must never be handed it.
        if not math.isfinite(slope) or not math.isfinite(variance):
            return None
        return BpmPerMinute(slope)


# =========================================================================
# The controller's configuration
# =========================================================================

DEFAULT_KP: Final[float] = 3.0
"""Proportional gain, rpm of motor speed per bpm of error.

**An ESTIMATE pending a plant step test on the real machine**, and stated as
such because a number in a config file acquires authority it has not earned.
Its basis: the commissioning note puts the local plant gain near 0.137 bpm per
motor-rpm at full speed, so Kp = 3 gives a loop gain near 0.4 - deliberately
under unity, so the first closed-loop run on a person is sluggish rather than
oscillatory.

The plant gain **halves at half speed**, because centripetal load goes as the
square of speed. A gain tuned at full speed is therefore roughly twice as
aggressive as it should be down low, which is the direction that matters: the
response is slowest where the machine is safest. No gain scheduling here until
a step test says what the schedule should be - guessing a schedule is two
unmeasured numbers instead of one.

There is no ``RpmPerBpm`` NewType in ``src/units.py``, which this module does
not own, so this is a bare float. See the report accompanying this file.
"""

DEFAULT_TI: Final[Seconds] = Seconds(40.0)
"""Integral time. Ki = Kp / Ti, so 0.075 rpm per bpm per second.

Also an ESTIMATE. Chosen at the scale of the plant's own time constant (30-60 s)
rather than faster: an integral time shorter than the process lag integrates
error the machine has not had time to answer yet, which is how an integrator
buys an overshoot that arrives as a shove.
"""

DEFAULT_PERIOD: Final[Seconds] = Seconds(5.0)
"""How often a new setpoint is computed. See the module docstring, point 3."""

DEFAULT_STEP_CAP: Final[Seconds] = Seconds(10.0)
"""Longest interval a single increment may integrate over: two nominal periods.

The guard against a stalled loop. If the process is descheduled for four
minutes, the true elapsed time would multiply the integral increment by ~50 and
buy a windup the controller never earned, and it would multiply the slew
allowance by the same factor and permit a step the person would feel as a kick.
Capping the interval makes both proportional to a bounded number. The cap makes
the slew guarantee *stronger*, never weaker: the change is bounded by
``slew * min(dt, cap)``, which is at most ``slew * dt``.
"""

RUNNING_RESIDUE_BOUND: Final[float] = 1.0
"""How much unspent increment a *running* controller may carry, in rpm.

The controller proposes a float and the drive takes a whole number, so every
tick leaves a remainder. That remainder has to be carried, because the integral
term is genuinely sub-rpm at the gains above: at Ki = 0.075 rpm per bpm per
second, a 1 bpm error over one 5 s period asks for 0.375 rpm. Rounding that to
zero every tick does not make the controller gentle, it deletes its integral
action entirely - the demand would never move while the error was small, and
the machine would sit still through a whole warmup with nobody able to see why.
The first draft of this file did exactly that, and the plant sweep caught it.

One rpm is the bound while running: enough to hold any rounding remainder
(never more than half an rpm) with room to spare, and small enough that the
carry can never become a shadow integrator. Anything the ceiling or the slew
limiter took away is discarded rather than carried, which is what makes
prolonged saturation windup-free - see :meth:`HeartRateController._carry`.
"""

DEFAULT_LOW_INSET: Final[Bpm] = Bpm(1)
"""How far above the zone floor the no-correction band starts."""

DEFAULT_HIGH_INSET: Final[Bpm] = Bpm(3)
"""How far below the zone ceiling the no-correction band ends.

Larger than the low inset, and that asymmetry is the policy: correction against
a rising heart rate begins three bpm before the ceiling, while a heart rate
sagging toward the floor is tolerated to within one. Drifting low costs the
session some effect; drifting high costs the person.
"""


@dataclass(frozen=True, slots=True)
class TargetBand:
    """The band the control law does not correct inside, in bpm.

    Its centre is what gets reported as the target; its edges are what the
    control error is measured to. Outside the band the error is referenced to
    the *nearer edge*, not the centre, so it passes continuously through zero as
    the heart rate enters the band. A centre-referenced error with a deadband
    would jump by the half-width at each edge, and in velocity form a jump in
    the error is a jump in the speed.
    """

    low: Bpm
    high: Bpm

    @property
    def centre(self) -> Bpm:
        """The number reported as ``target_bpm``. Whole bpm, like every rate here."""
        return Bpm(round((self.low + self.high) / 2))

    def contains(self, bpm: Bpm) -> bool:
        """Whether ``bpm`` is inside the band, edges included."""
        return self.low <= bpm <= self.high

    def control_error(self, bpm: Bpm) -> float:
        """The error the PI acts on: zero inside, edge-referenced outside.

        Sign convention as in :class:`~src.training.types.ControlDecision`:
        positive means the heart rate is below target, so the law may speed up.
        """
        if bpm > self.high:
            return float(self.high - bpm)
        if bpm < self.low:
            return float(self.low - bpm)
        return 0.0


@dataclass(frozen=True, slots=True)
class Zone:
    """The target heart-rate zone and the insets that make its deadband.

    No defaults for the bounds: a training zone is prescribed per person from a
    resting rate and an age, and a default zone is how somebody else's
    prescription gets applied to this occupant without anyone choosing it.
    """

    low: Bpm
    high: Bpm
    low_inset: Bpm = DEFAULT_LOW_INSET
    high_inset: Bpm = DEFAULT_HIGH_INSET

    def __post_init__(self) -> None:
        if not _is_plausible(self.low) or not _is_plausible(self.high):
            raise ValueError(
                f"zone {self.low}..{self.high} bpm falls outside the plausible range "
                f"{MIN_PLAUSIBLE_BPM}..{MAX_PLAUSIBLE_BPM}; a zone the tracker would "
                "never report a rate inside is a session that can never regulate"
            )
        if self.low >= self.high:
            raise ValueError(f"zone low {self.low} must be below zone high {self.high}")
        if self.low_inset < 0 or self.high_inset < 0:
            raise ValueError(
                f"zone insets must not be negative, got {self.low_inset}/{self.high_inset}; "
                "a negative inset would put the no-correction band OUTSIDE the zone"
            )

    def target_band(self) -> TargetBand:
        """The no-correction band: the zone pulled in by its two insets.

        When the insets are wider than the zone the band would invert, and the
        collapse is to the **low** edge rather than to the middle or the high
        edge. That is the conservative reading: referencing the low edge means
        correction downward begins as soon as the heart rate passes it, which
        is the same direction the asymmetry was chosen for.
        """
        low = Bpm(self.low + self.low_inset)
        high = Bpm(self.high - self.high_inset)
        if high < low:
            return TargetBand(low=low, high=low)
        return TargetBand(low=low, high=high)


@dataclass(frozen=True, slots=True)
class SpeedLimits:
    """The rpm envelope, at the MOTOR shaft, and how fast it may be crossed.

    Motor rpm throughout, because that is what the drive is written with; the
    gearbox ratio is 49.79, so an output-shaft number here would be a fiftyfold
    error. No defaults on any field: every one of them is a fact about this
    machine, and a default is how a wrong one gets used without being chosen.
    """

    min_run_rpm: MotorRpm
    """The slowest speed worth running at. Below it the setpoint is zero instead.

    The setpoint domain is ``{0} union [min_run_rpm, max_rpm]``: there is no
    such thing as a useful crawl, and a value in the gap would ask the drive for
    a speed it cannot hold cleanly.
    """

    max_rpm: MotorRpm
    """The absolute ceiling. Enforced at the single exit of ``update``."""

    warmup_max_rpm: MotorRpm
    """Ceiling while in WARMUP.

    The guard against a *persistently wrong low reading*. The control law reads
    "heart rate below target" and accelerates; if the reading is wrong low and
    stays wrong low - a detector artefact, a lead half off - the law will keep
    accelerating with nothing to contradict it. The warmup ceiling bounds how
    far that can go while the plant gain is still weakest and the person has
    not yet adapted to any load at all.
    """

    slew: RpmPerSecond
    """Hardest rate of change of the setpoint, in either direction."""

    start_hysteresis_rpm: MotorRpm
    """Extra demand above ``min_run_rpm`` needed to leave zero.

    Leaving zero costs ``min_run_rpm`` of speed in one step, so without
    hysteresis a demand hovering at the boundary would start and stop the
    machine repeatedly. Stopping needs the demand to fall *below*
    ``min_run_rpm``; starting needs it to exceed ``min_run_rpm`` plus this.
    """

    def __post_init__(self) -> None:
        if self.min_run_rpm < 1:
            raise ValueError(
                f"min_run_rpm must be at least 1, got {self.min_run_rpm}; zero would make "
                "the setpoint domain continuous and the hysteresis meaningless"
            )
        if self.max_rpm < self.min_run_rpm:
            raise ValueError(f"max_rpm {self.max_rpm} is below min_run_rpm {self.min_run_rpm}")
        if not self.min_run_rpm <= self.warmup_max_rpm <= self.max_rpm:
            raise ValueError(
                f"warmup_max_rpm {self.warmup_max_rpm} must lie within "
                f"{self.min_run_rpm}..{self.max_rpm}; a warmup ceiling below the minimum "
                "running speed would forbid warming up at all"
            )
        if not self.slew > 0.0 or not math.isfinite(self.slew):
            raise ValueError(
                f"slew must be finite and positive, got {self.slew}; a non-finite slew "
                "rate is an unbounded speed change"
            )
        if self.start_hysteresis_rpm < 0:
            raise ValueError(
                f"start_hysteresis_rpm must not be negative, got {self.start_hysteresis_rpm}"
            )
        if self.min_run_rpm + self.start_hysteresis_rpm > self.max_rpm:
            raise ValueError(
                f"min_run_rpm {self.min_run_rpm} plus start_hysteresis_rpm "
                f"{self.start_hysteresis_rpm} exceeds max_rpm {self.max_rpm}, so the "
                "machine could never be started"
            )


@dataclass(frozen=True, slots=True)
class Gains:
    """PI tuning and timing. See :data:`DEFAULT_KP` on what these are worth."""

    kp: float = DEFAULT_KP
    ti: Seconds = DEFAULT_TI
    period: Seconds = DEFAULT_PERIOD
    step_cap: Seconds = DEFAULT_STEP_CAP

    def __post_init__(self) -> None:
        """Refuse gains that are not finite and positive.

        This is what lets the rest of the file claim, without an ``isfinite``
        guard on the output, that no non-finite value can reach the setpoint:
        with bounded heart rates, a bounded interval and finite gains, every
        product below is finite by construction.
        """
        if not self.kp > 0.0 or not math.isfinite(self.kp):
            raise ValueError(f"kp must be finite and positive, got {self.kp}")
        if not self.ti > 0.0 or not math.isfinite(self.ti):
            raise ValueError(f"ti must be finite and positive, got {self.ti}")
        if not self.period > 0.0 or not math.isfinite(self.period):
            raise ValueError(f"period must be finite and positive, got {self.period}")
        if not self.step_cap >= self.period or not math.isfinite(self.step_cap):
            raise ValueError(
                f"step_cap {self.step_cap} must be finite and at least the control period "
                f"{self.period}; a cap below the period would truncate every normal tick"
            )


@dataclass(frozen=True, slots=True)
class ControlPlan:
    """Everything the control law needs that does not change during a session."""

    zone: Zone
    speed: SpeedLimits
    warmup: Seconds
    """How long the WARMUP target takes to ramp from resting to the zone floor."""

    gains: Gains = Gains()

    def __post_init__(self) -> None:
        if not self.warmup > 0.0 or not math.isfinite(self.warmup):
            raise ValueError(
                f"warmup must be finite and positive, got {self.warmup}; a zero-length "
                "ramp is a step change in the target"
            )
        # Leaving zero costs min_run_rpm in a single step, and the slew limiter
        # allows floor(slew * dt) per decision. If one nominal period does not
        # buy at least min_run_rpm of allowance, the machine can never start:
        # every candidate above the start threshold gets slew-limited back into
        # the forbidden gap and snapped to zero again, forever. That is a config
        # bug that presents as "the motor never turns", so it fails here where
        # the message can say why.
        allowance = math.floor(self.speed.slew * self.gains.period)
        if allowance < self.speed.min_run_rpm:
            raise ValueError(
                f"one control period of {self.gains.period} s at {self.speed.slew} rpm/s "
                f"allows only {allowance} rpm of change, which is less than min_run_rpm "
                f"{self.speed.min_run_rpm}; the machine could never leave zero"
            )


# =========================================================================
# One tick in, one tick out
# =========================================================================


@dataclass(frozen=True, slots=True)
class ControlInput:
    """What the runtime loop knows at this tick. Frozen: one observation, one tick.

    Everything optional here is optional because it is genuinely sometimes
    unknown, and each ``None`` has its own handling below. None of them is ever
    filled in with a default: a substituted heart rate is an invented vital
    sign, and a substituted applied speed is an invented machine state.
    """

    phase: Phase

    bpm: Bpm | None
    """The tracker's filtered, fresh rate - build it with :meth:`HeartRateTracker.usable`.

    ``None`` means "no rate may be acted on", from any cause: none measured,
    quality too poor, sequence repeated, or too old. The controller does not
    re-derive freshness; it is handed a number that is already allowed.
    """

    applied_rpm: MotorRpm | None
    """The speed the DRIVE reports, read back - not the last value written.

    This is the anti-windup mechanism and the reason resumption after a safety
    FREEZE needs no special case: the increment is added to what is actually
    happening. ``None`` means the drive's state is unknown (no status yet, or a
    stale one), and the controller then declines to move at all - adding a
    demand to an unknown base is precisely how a machine ends up somewhere
    nobody predicted.
    """

    resting_bpm: Bpm | None = None
    """The resting rate measured in BASELINE; the origin of the WARMUP ramp."""


@dataclass(frozen=True, slots=True)
class ControlStep:
    """The decision, plus the working that produced it.

    The decision itself is the contract type
    (:class:`~src.training.types.ControlDecision`); the rest is evidence for a
    session log and for the tests, kept beside it rather than inside it so the
    contract type stays the same shape for every consumer.
    """

    decision: ControlDecision

    base_rpm: MotorRpm
    """What the increment was added to: the clamped applied speed on a regulating
    tick, the standing demand otherwise. The anti-windup claim is visible here."""

    increment_rpm: float
    """The PI increment, before clamping, slew limiting and domain snapping."""

    dt: Seconds
    """The interval this step integrated over, after the step cap."""

    at_ceiling: bool
    """Whether the emitted setpoint sits at this phase's rpm ceiling."""

    slew_limited: bool
    """Whether the slew limiter shortened this step."""

    held: bool
    """``True`` when no decision was taken and the standing demand was re-emitted."""


@dataclass(frozen=True, slots=True)
class _Demand:
    """An unshaped proposal. Never leaves this module: it has no clamps applied."""

    candidate: float
    target_bpm: Bpm | None
    error_bpm: float
    in_deadband: bool
    increment: float
    base_rpm: MotorRpm
    reason: str

    increments: bool
    """Whether this proposal is ``base + increment`` (the PI) or an absolute value.

    Only an incremental proposal carries the unspent remainder forward; an
    absolute one - hold here, or demand zero - would be corrupted by it.
    """


_WAITING_FOR_PERIOD: Final[str] = (
    "holding speed: the control period has not elapsed since the last decision"
)
_NO_TIME_PASSED: Final[str] = "holding speed: no time has passed since the last decision"
_NO_ALLOWANCE: Final[str] = (
    "holding speed: too little time has passed to change the setpoint by a whole rpm"
)
_NO_HEART_RATE: Final[str] = "holding speed: no heart rate may be acted on"
_NO_APPLIED_SPEED: Final[str] = "holding speed: the speed the drive is running at is unknown"
_NO_RESTING_RATE: Final[str] = "holding speed: warmup has no resting heart rate to ramp from"


def is_regulating(phase: Phase) -> bool:
    """Whether the control law has a heart-rate target to track in this phase.

    An exhaustive ``match`` rather than a set membership test, so adding a phase
    to :class:`~src.training.types.Phase` fails the type check here - naming the
    phase nobody classified - instead of defaulting it to "does not regulate"
    and being discovered on the machine.
    """
    match phase:
        case Phase.WARMUP | Phase.HOLD:
            return True
        case Phase.BASELINE | Phase.COOLDOWN | Phase.RECOVERY | Phase.DONE:
            return False
        case _ as unreachable:
            assert_never(unreachable)


def stop_reason(phase: Phase) -> str:
    """The operator-facing sentence for a phase that demands zero speed.

    Exhaustive over every phase, including the two that regulate: a helper whose
    last branch is unreachable is a helper whose last branch is untested, and
    the phase machine above is free to hand any phase to any function here.
    """
    match phase:
        case Phase.BASELINE:
            return "demanding zero: baseline measures the resting heart rate at standstill"
        case Phase.COOLDOWN:
            return "demanding zero: cooling down on the controlled ramp"
        case Phase.RECOVERY:
            return "demanding zero: recovery is monitored with nothing turning"
        case Phase.DONE:
            return "demanding zero: the programme is over"
        case Phase.WARMUP | Phase.HOLD:
            return f"demanding zero: no speed is asked for in {phase.value}"
        case _ as unreachable:
            assert_never(unreachable)


@final
class HeartRateController:
    """A PI in velocity form that turns a heart rate into a motor-speed demand.

    **Mutable, and the mutation is the integral state.** Owned by one runtime
    loop, shared with nobody. Four fields carry everything it remembers: the
    standing demand, the previous control error, when the last decision was
    taken, and where the WARMUP ramp started. There is deliberately **no
    integrator accumulator** - see the module docstring, point 1 - which is why
    "the integrator diverged under saturation" is not a failure mode this class
    has rather than one it defends against.

    The single exit of :meth:`update` applies, in this order: the zero-to-ceiling
    clamp, the slew limit, the setpoint domain with its start hysteresis, and
    ``allow_increase``. Every proposal goes through it, including the ones that
    propose no change, so no code path can bypass the envelope.

    Nothing here reads a clock: ``now`` arrives as a parameter on every call
    (contract rule 4). That is what lets a 45-minute programme be simulated
    against a plant model in a fraction of a second, which is the only evidence
    this law is safe before a person is inside the machine.
    """

    __slots__ = (
        "_decided_at",
        "_output",
        "_phase",
        "_phase_at",
        "_plan",
        "_previous_error",
        "_ramp_origin",
        "_residue",
    )

    def __init__(self, plan: ControlPlan, *, initial_rpm: MotorRpm = MotorRpm(0)) -> None:
        if initial_rpm != 0 and not (plan.speed.min_run_rpm <= initial_rpm <= plan.speed.max_rpm):
            raise ValueError(
                f"initial_rpm {initial_rpm} is outside the setpoint domain "
                f"{{0}} union {plan.speed.min_run_rpm}..{plan.speed.max_rpm}; the standing "
                "demand is the controller's only state, and it must start inside the domain "
                "it is required to stay inside"
            )
        self._plan: ControlPlan = plan
        # The standing demand. This IS the integral state of the velocity-form
        # PI, and the only value this object mutates on a normal tick.
        self._output: MotorRpm = initial_rpm
        # The previous control error, or None when the next increment must
        # re-seed. Cleared by anything that breaks the chain of evidence - a
        # phase change, a missing heart rate, an unknown applied speed - so the
        # proportional part never differences two errors with an unaccounted
        # gap between them.
        self._previous_error: float | None = None
        self._decided_at: Monotonic | None = None
        self._phase: Phase | None = None
        self._phase_at: Monotonic = Monotonic(0.0)
        self._ramp_origin: Bpm | None = None
        # Unspent increment, in rpm, carried between ticks. Bounded at every
        # exit - see RUNNING_RESIDUE_BOUND and _carry - so it is a carry and
        # not an accumulator, and it cannot diverge.
        self._residue: float = 0.0

    @property
    def plan(self) -> ControlPlan:
        """The plan this controller was built with."""
        return self._plan

    @property
    def demand(self) -> MotorRpm:
        """The standing demand: what the last call to :meth:`update` asked for."""
        return self._output

    @property
    def ramp_origin(self) -> Bpm | None:
        """The heart rate the WARMUP ramp starts from, once latched."""
        return self._ramp_origin

    @property
    def residue(self) -> float:
        """The unspent increment carried into the next tick, in rpm.

        Exposed so a test can bound it directly rather than inferring it from
        the setpoint: "the integrator cannot diverge" is a claim about this
        number and about :attr:`demand`, and those two are the whole state.
        """
        return self._residue

    def update(
        self,
        now: Monotonic,
        observed: ControlInput,
        *,
        allow_increase: bool = True,
    ) -> ControlStep:
        """Compute this tick's demand. Never raises, never reads a clock.

        ``allow_increase=False`` lets the supervisor permit decreases only - the
        setpoint may fall or stay, never rise - without taking the controller
        out of the loop, so regulation downward keeps working while an upward
        demand is simply refused.

        It is applied **last**, on the value actually about to be emitted, so
        the promise holds by inspection rather than by argument. Applying it
        before the domain snap would give the same answer today, because the
        snap raises a value only out of the forbidden gap and never above the
        previous setpoint - but that is a property of the current snap rule, and
        a promise that depends on one is a promise the next edit can break.
        """
        self._track_phase(now, observed.phase)
        ceiling = self._ceiling(observed.phase)
        if self._decided_at is None:
            self._decided_at = now
        since = elapsed(self._decided_at, now)

        # The gates are written as `not (x >= y)` rather than `x < y` so that a
        # NaN interval - a corrupted clock reading - lands in the holding
        # branch. With `<`, NaN compares False and would fall THROUGH into the
        # arithmetic, which is the difference between a held setpoint and an
        # undefined one.
        if is_regulating(observed.phase) and not (since >= self._plan.gains.period):
            return self._standing(observed.phase, ceiling, _WAITING_FOR_PERIOD)
        if not (since > 0.0):
            return self._standing(observed.phase, ceiling, _NO_TIME_PASSED)

        step_dt = Seconds(min(since, self._plan.gains.step_cap))
        allowance = math.floor(self._plan.speed.slew * step_dt)
        previous = self._output
        if allowance < self._required_allowance(previous):
            # Not enough elapsed time for this tick to be able to move the
            # setpoint at all. Returning without consuming the interval is what
            # keeps the slew limit exact when a whole step costs more than one
            # tick's worth of allowance: the time keeps accumulating until the
            # step has actually been earned, instead of being rounded away on
            # every tick and never taken.
            return self._standing(observed.phase, ceiling, _NO_ALLOWANCE)

        self._decided_at = now
        demand = self._demand(now, observed, step_dt, ceiling)

        # ---- the single exit: everything below is the envelope -------------
        # The ceiling clamp is load-bearing. The clamp at zero is redundant
        # defence and is kept as such: the domain snap below already has no
        # branch that can emit a negative setpoint, so removing this `max` would
        # change nothing observable - a reversion audit confirmed it. It stays
        # because it makes "the proposal entering the limiter is inside
        # [0, ceiling]" true by reading rather than by argument.
        bounded = min(max(demand.candidate, 0.0), float(ceiling))
        stepped = round(bounded)
        limited = min(max(stepped, previous - allowance), previous + allowance)
        output = self._snap(limited, previous, allowance)
        if not allow_increase:
            output = MotorRpm(min(output, previous))
        self._residue = self._carry(demand, bounded, output)
        self._output = output
        return ControlStep(
            decision=ControlDecision(
                desired_rpm=output,
                phase=observed.phase,
                error_bpm=demand.error_bpm,
                in_deadband=demand.in_deadband,
                target_bpm=demand.target_bpm,
                reason=demand.reason,
            ),
            base_rpm=demand.base_rpm,
            increment_rpm=demand.increment,
            dt=step_dt,
            at_ceiling=output >= ceiling,
            slew_limited=stepped != limited,
            held=False,
        )

    # ---- state bookkeeping -------------------------------------------------

    def _track_phase(self, now: Monotonic, phase: Phase) -> None:
        """Notice a phase change, and forget what the previous phase established.

        Run on every call rather than only on decision ticks, so the WARMUP ramp
        is timed from the moment the phase actually changed and not from up to a
        control period later.
        """
        if phase is self._phase:
            return
        self._phase = phase
        self._phase_at = now
        # A new phase means a new target, so the previous error belongs to a
        # question nobody is asking any more, and the ramp origin to a ramp that
        # is over.
        self._previous_error = None
        self._ramp_origin = None
        self._residue = 0.0

    def _required_allowance(self, previous: MotorRpm) -> int:
        """Smallest slew allowance that lets a decision tick change anything.

        One rpm normally: below that, rounding would discard the whole step.

        **At exactly ``min_run_rpm`` it is ``min_run_rpm``, and that is not an
        optimisation - it is what makes a stop possible at all.** The setpoint
        domain has nothing between zero and the minimum running speed, so the
        last step of a stop is a jump of ``min_run_rpm``, and it may only be
        taken when the slew allowance covers the whole jump. A tick that cannot
        pay for it must therefore leave the interval unspent so the allowance
        can grow, which it does in ``min_run_rpm / slew`` seconds.

        Without this the descent stalls one step short: the setpoint reaches the
        minimum running speed, every subsequent tick proposes zero, every
        subsequent tick is refused for want of allowance, and **the machine
        never stops** - with a person inside it, and with the telemetry showing
        a perfectly obedient controller demanding zero. A probe caught this
        before the tests were written; ``tests/test_hr_control.py`` now asserts
        the stop from every starting speed.
        """
        if previous == self._plan.speed.min_run_rpm:
            return self._plan.speed.min_run_rpm
        return 1

    def _ceiling(self, phase: Phase) -> MotorRpm:
        """The rpm ceiling in force this tick."""
        if phase is Phase.WARMUP:
            return self._plan.speed.warmup_max_rpm
        return self._plan.speed.max_rpm

    # ---- the proposal ------------------------------------------------------

    def _demand(
        self, now: Monotonic, observed: ControlInput, step_dt: Seconds, ceiling: MotorRpm
    ) -> _Demand:
        """Dispatch on the phase. Exhaustive, so a new phase fails the type check."""
        match observed.phase:
            case Phase.HOLD:
                return self._regulate(self._plan.zone.target_band(), observed, step_dt, ceiling)
            case Phase.WARMUP:
                return self._warmup(now, observed, step_dt, ceiling)
            case Phase.BASELINE | Phase.COOLDOWN | Phase.RECOVERY | Phase.DONE:
                # No target, so nothing to regulate: demand zero and let the
                # envelope turn it into a ramp. The previous error is dropped
                # because the next regulating tick must re-seed rather than
                # difference across the gap.
                self._previous_error = None
                return _Demand(
                    candidate=0.0,
                    target_bpm=None,
                    error_bpm=0.0,
                    in_deadband=True,
                    increment=0.0,
                    base_rpm=self._output,
                    increments=False,
                    reason=stop_reason(observed.phase),
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _warmup(
        self, now: Monotonic, observed: ControlInput, step_dt: Seconds, ceiling: MotorRpm
    ) -> _Demand:
        """Track a target that ramps linearly from resting to the zone floor.

        One law, not two: the ramp only changes what the target *is*, and the
        same PI that holds the zone tracks it. A second control law for warmup
        would be a second thing to get wrong, in the phase where the plant gain
        is weakest and the occupant has adapted to nothing yet.

        The ramp origin is latched on entry - from the measured resting rate
        when the runner has one, from the current reading otherwise - and is not
        revised afterwards: a target that moves because its own origin moved is
        not a ramp.
        """
        origin = self._ramp_origin
        if origin is None:
            origin = observed.resting_bpm if observed.resting_bpm is not None else observed.bpm
        if origin is None or not _is_plausible(origin):
            self._previous_error = None
            return self._hold(_NO_RESTING_RATE)
        self._ramp_origin = origin

        # Clamped to 0..1 at BOTH ends. The upper clamp is the ramp finishing.
        # The lower one says a target before the ramp began is the ramp's start,
        # which is both the only sensible reading and the guard that keeps a
        # nonsensical clock out of the arithmetic: `max(0.0, x)` returns 0.0 for
        # a NaN or a negative-infinite interval, where `round()` on the product
        # would otherwise raise OverflowError inside the control path.
        fraction = min(1.0, max(0.0, elapsed(self._phase_at, now) / self._plan.warmup))
        target = Bpm(round(origin + (self._plan.zone.low - origin) * fraction))
        # A degenerate band: warmup TRACKS a moving reference, and a deadband
        # around a moving reference only delays the ramp behind it.
        return self._regulate(TargetBand(low=target, high=target), observed, step_dt, ceiling)

    def _regulate(
        self,
        band: TargetBand,
        observed: ControlInput,
        step_dt: Seconds,
        ceiling: MotorRpm,
    ) -> _Demand:
        """The PI itself, in velocity form.

        ``du = Kp * (e - e_prev) + (Kp / Ti) * e * dt``, added to the speed the
        drive reports it is applying. Two consequences worth naming:

        * The proportional part telescopes across ticks, so the *sum* of the
          increments carries it; when the error enters the deadband and becomes
          zero, that part unwinds itself. Nothing has to remember to remove it.
        * The integral part is the only thing that persists, and it persists as
          the setpoint - which the drive, the clamps and the safety supervisor
          have all already had their say over. That is the whole anti-windup
          arrangement: there is nothing else to wind up.
        """
        bpm = observed.bpm
        if bpm is None or not _is_plausible(bpm):
            self._previous_error = None
            return self._hold(_NO_HEART_RATE)
        applied = observed.applied_rpm
        if applied is None:
            self._previous_error = None
            return self._hold(_NO_APPLIED_SPEED)

        # Integer clamp before any float arithmetic: an absurd read-back must
        # not become an absurd float, and a reverse or over-ceiling reading is
        # not a base to add a demand to.
        base = MotorRpm(min(max(applied, 0), ceiling))
        control_error = band.control_error(bpm)
        previous_error = self._previous_error
        if previous_error is None:
            # Re-seeding: with e_prev = e the proportional part contributes
            # nothing this tick, so resuming after a gap cannot produce a step
            # out of differencing errors from either side of it.
            previous_error = control_error
        gains = self._plan.gains
        increment = (
            gains.kp * (control_error - previous_error)
            + (gains.kp / gains.ti) * control_error * step_dt
        )
        self._previous_error = control_error

        target = band.centre
        in_deadband = band.contains(bpm)
        return _Demand(
            candidate=base + increment + self._residue,
            target_bpm=target,
            # The RAW error to the reported target, per the ControlDecision
            # contract - not the deadband-squashed error the PI acted on.
            # `in_deadband` is what says whether a correction was applied, which
            # is exactly why that flag is not derivable from this number.
            error_bpm=float(target - bpm),
            in_deadband=in_deadband,
            increment=increment,
            base_rpm=base,
            increments=True,
            reason=(
                f"holding inside the target band {band.low}-{band.high} bpm at {bpm} bpm"
                if in_deadband
                else f"correcting {bpm} bpm toward the band {band.low}-{band.high} bpm"
            ),
        )

    def _carry(self, demand: _Demand, bounded: float, output: MotorRpm) -> float:
        """How much of this tick's proposal to carry into the next, in rpm.

        ``bounded`` is the proposal *after* the ceiling clamp, so nothing the
        ceiling removed is ever carried: under prolonged saturation the
        proposal equals the ceiling, the output equals the ceiling, and the
        carry is zero. That is the anti-windup property, and it holds because
        of which value is subtracted here - never because of a test on how long
        saturation has lasted.

        Two bounds, and the difference between them is the whole point:

        * **While running, one rpm.** Only the rounding remainder needs
          carrying, so anything larger - a slew-limited climb, say - is
          discarded. A slew limit that accumulated what it withheld would
          release it as a lurch the moment the limit stopped biting.
        * **While stopped, the start threshold.** The setpoint domain has a hole
          in it: a demand of 30 rpm on a machine whose minimum is 60 comes out
          as zero. Discarding that would mean a genuine, slowly-rising demand
          could never cross the threshold and the machine would never start,
          which is the defect this whole mechanism exists to fix. Carrying it is
          bounded by the threshold itself, so it is a deadzone compensator and
          not a windup: the most it can ever hide is one start's worth of speed.
        """
        if not demand.increments:
            # An absolute proposal - hold here, or demand zero - has no unspent
            # increment by definition, and adding a stale one to it next tick
            # would move a setpoint nobody asked to move.
            return 0.0
        if output == 0:
            bound = float(self._plan.speed.min_run_rpm + self._plan.speed.start_hysteresis_rpm)
        else:
            bound = RUNNING_RESIDUE_BOUND
        return min(bound, max(-bound, bounded - output))

    def _hold(self, reason: str) -> _Demand:
        """A proposal to stay where we are, still routed through the envelope."""
        return _Demand(
            candidate=float(self._output),
            target_bpm=None,
            error_bpm=0.0,
            in_deadband=True,
            increment=0.0,
            base_rpm=self._output,
            increments=False,
            reason=reason,
        )

    # ---- the envelope ------------------------------------------------------

    def _snap(self, limited: int, previous: MotorRpm, allowance: int) -> MotorRpm:
        """Force the setpoint into ``{0} union [min_run_rpm, max_rpm]``.

        Hysteresis on the way out of zero: starting needs the demand to exceed
        ``min_run_rpm + start_hysteresis_rpm``, stopping needs it to fall below
        ``min_run_rpm``. So the bottom of the range cannot chatter.

        Every branch here respects the slew allowance, including the two that
        move the value rather than pass it through, and that is not an accident
        of the numbers:

        * snapping a small demand back to zero while stopped changes nothing;
        * snapping to zero while running happens only when zero is within the
          allowance, which is checked;
        * otherwise a running machine whose demand fell below the minimum parks
          at ``min_run_rpm`` and stops on a later tick. ``min_run_rpm`` is
          always reachable: the demand got below the minimum, so
          ``previous - allowance < min_run_rpm``, so the distance down to
          ``min_run_rpm`` is smaller than the allowance.
        """
        min_run = self._plan.speed.min_run_rpm
        if previous == 0:
            if limited >= min_run + self._plan.speed.start_hysteresis_rpm:
                return MotorRpm(limited)
            return MotorRpm(0)
        if limited >= min_run:
            return MotorRpm(limited)
        if previous <= allowance:
            return MotorRpm(0)
        return min_run

    def _standing(self, phase: Phase, ceiling: MotorRpm, reason: str) -> ControlStep:
        """Re-emit the standing demand: this tick took no decision.

        The one exit that does not pass through the envelope, and it does not
        need to: the value it re-emits came out of the envelope on an earlier
        tick and has not changed since. Reporting no target rather than the last
        one is deliberate - a tick that decided nothing has no target to claim.

        Nothing is mutated here, the previous error included. A tick inside the
        control period has not broken the chain of evidence; clearing the error
        on each of the two dozen 5 Hz calls between decisions would mean the
        proportional term never acted at all.

        ``dt`` is reported as zero rather than as the interval since the last
        decision, because a held tick **integrated nothing** - and because the
        interval is the one number here that can be nonsense. A first call
        stamped at infinity yields an interval of ``inf - inf``, and reporting
        that NaN would put a value in a decision field that compares False
        against every bound the layers above test it with.
        """
        return ControlStep(
            decision=ControlDecision(
                desired_rpm=self._output,
                phase=phase,
                error_bpm=0.0,
                in_deadband=True,
                target_bpm=None,
                reason=reason,
            ),
            base_rpm=self._output,
            increment_rpm=0.0,
            dt=Seconds(0.0),
            at_ceiling=self._output >= ceiling,
            slew_limited=False,
            held=True,
        )
