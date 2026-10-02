r"""The training programme: what the operator picks, and the timeline it implies.

Three objects and one file format:

* :class:`TrainingProfile` - a **template** an operator edits between sessions.
  It cannot exist in an invalid state: every refusal in :class:`Violation` is
  checked in ``__post_init__``, so a profile object anywhere in this system has
  already been vouched for.
* :class:`Program` - the **frozen resolved instance** that actually ran. A
  profile is a document that changes over time; a programme is a fact. If an
  operator edits the profile at minute twelve, the session record must still
  describe the thirty minutes that were actually commanded, which is why the
  programme carries a *copy* of the profile plus the revision it came from.
* :class:`ProfileStore` - profiles on disk, with a monotonic revision for
  optimistic concurrency, atomic writes, and recovery from a corrupt file.

# The subject ceiling is a safety input, not decoration

A target zone of 145-160 bpm is unremarkable for a thirty-year-old and is
**above maximum** for a sixty-five-year-old: the Tanaka estimate of maximum
heart rate at 65 is about 162 bpm, so 160 bpm is 99% of it. The same JSON, the
same machine, the same person in the centrifuge - and one of those two sessions
is a cardiac stress test nobody consented to.

So ``subject_hr_max`` is a required field, it is bounded to a plausible human
range (a typo of ``16`` or ``1620`` would otherwise switch the check off), and
the zone ceiling, the hard maximum and it are checked against each other at
construction. See :data:`ZONE_CEILING_FRACTION`.

# The rpm ceiling starts deliberately low

``max_rpm`` in the shipped profiles is 276 motor rpm - 20% of the 1380 rpm
nameplate, about 5.5 output rpm, about 0.05 g at a 1.5 m radius. That is not a
guess at a useful working speed; it is the first step of commissioning.

The ceiling is raised **one deliberate step at a time**, and at every step the
drive's own HSP parameter is re-set to match the new software ceiling, in Hz
(:attr:`TrainingProfile.hsp_hertz` computes exactly that number for whoever is
standing at the keypad). The reason is blunt: HSP is the ceiling that survives
a software bug. A mistake in this file, in the control law, or in a hand-edited
JSON cannot make the drive exceed HSP, because the drive clamps the frequency
reference itself, with no help from this process. A software-only ceiling
protects the person in the machine exactly as long as the software is correct,
which is not a guarantee anyone should accept on their behalf.

``allow_above_nameplate`` exists so that going above 1380 rpm is a recorded,
deliberate act rather than a plausible typo. Above the nameplate point the
drive is field-weakening: torque falls away and nothing here wants that.

# The override rule: HOLD absorbs the delta

``hold_s`` is **derived**, never stored::

    hold = total_duration_s - baseline_s - warmup_max_s - cooldown_s - recovery_s

That is structural, not a convention somebody has to remember. A 5-minute
warmup is a clinical decision about how fast a body is asked to adapt, so
changing the session length from 30 to 45 minutes must not stretch it to 7.5
minutes. Because hold is the remainder,
:meth:`TrainingProfile.with_total_duration` cannot get this wrong - and a total
that leaves less than ``hold_min_s`` of hold is refused rather than silently
squeezed.

# What this module does not do

No clock (contract rule 4): :meth:`TrainingProfile.phase_at` is pure and
``resolved_at`` arrives as a parameter. No motor commands, no thresholds beyond
the ones a profile declares, and no policy about what to do when the heart rate
leaves the zone - that belongs to the control law and the safety supervisor.

:meth:`TrainingProfile.phase_at` describes the **nominal** timeline. It is what
a UI draws and what "remaining" counts down, but it is not the authority on
which phase a session is in: the runner leaves WARMUP early when the zone is
reached, and a ``RAMP_DOWN`` verdict sends any phase straight to COOLDOWN. Code
that needs the phase the session is actually in must ask the runner.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum, unique
from pathlib import Path
from types import MappingProxyType
from typing import Final, NewType, cast, final

from src.result import Err, Ok, Result
from src.training.types import Phase
from src.units import (
    Bpm,
    Hertz,
    MotorRpm,
    OutOfRange,
    Seconds,
    UnixMillis,
    motor_rpm_to_hertz,
)

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


# =========================================================================
# Hardware and physiology limits this module refuses to cross
# =========================================================================

NAMEPLATE_MOTOR_RPM: Final[MotorRpm] = MotorRpm(1380)
"""SEW KA37 DRS71S4 nameplate speed at 50 Hz.

``max_rpm`` above this needs the explicit ``allow_above_nameplate`` flag.
"""

NAMEPLATE_BASE_HERTZ: Final[Hertz] = Hertz(50.0)
"""The frequency at which the nameplate speed is reached.

Used only to turn a software rpm ceiling into the HSP value an operator types
into the drive.
"""

COMMISSIONED_DECEL_S: Final[Seconds] = Seconds(4.0)
"""The measured deceleration ramp, and a floor under ``cooldown_s``.

The DC bus absorbs about 11 J of the ~420 J stored in the spinning rig - about
2.5% - so a stop commanded faster than this trips DC-bus overvoltage and drops
the drive into FREEWHEEL: a longer, uncontrolled coast-down with a person
inside. A profile whose cooldown is shorter than the ramp the machine actually
has is a promise the machine cannot keep, so it is refused.
"""

MIN_RECOVERY_S: Final[Seconds] = Seconds(60.0)
"""Shortest recovery phase a profile may declare.

RECOVERY is the motor-stopped phase with the person still in the machine, and
it is the highest vasovagal-risk window of the whole session - blood pressure
falls when the centripetal load goes away. A profile that ends monitoring the
moment the shaft stops is refused.
"""

ZONE_CEILING_FRACTION: Final[float] = 0.9
"""``zone_high_bpm`` may not exceed this fraction of ``subject_hr_max``.

A target the subject cannot sustain is not a target; it is an instruction to
the control law to keep accelerating. 90% of maximum is the conventional
ceiling for the hard end of an aerobic zone, and it leaves the hard-maximum and
critical tiers somewhere to live above it.
"""

SUBJECT_HR_MAX_MIN: Final[Bpm] = Bpm(100)
"""Lowest maximum heart rate this module will believe. See below."""

SUBJECT_HR_MAX_MAX: Final[Bpm] = Bpm(220)
"""Highest maximum heart rate this module will believe.

Bounded because every other cardiac check in this file is expressed as a
fraction of this number. ``subject_hr_max = 16`` (a dropped digit) would make
every zone "above maximum" and refuse the profile, which is safe; but
``subject_hr_max = 1620`` would switch the ceiling check off entirely, which is
not. A bound on the input is what keeps the derived checks meaningful.
"""

MIN_SUBJECT_AGE_YEARS: Final[int] = 10
"""Youngest age the estimate below is defensible for."""

MAX_SUBJECT_AGE_YEARS: Final[int] = 100
"""Oldest age the estimate below is defensible for."""

TANAKA_INTERCEPT: Final[float] = 208.0
"""Intercept of ``HRmax = 208 - 0.7 x age`` (Tanaka et al., 2001)."""

TANAKA_SLOPE: Final[float] = 0.7
"""Slope of the same estimate.

Preferred over the familiar ``220 - age``, which overestimates the maximum for
young adults - 190 against 187 at age 30 - and underestimates it by about 7 bpm
at 65. Overestimating is the direction that lets an unsafe zone through, so the
better-fitting formula is the one used. Either way it remains an estimate: see
:func:`hr_max_from_age`.
"""


def hr_max_from_age(age_years: int) -> Result[Bpm, OutOfRange]:
    """Estimate a maximum heart rate from an age, refusing implausible ages.

    Provided because an operator knows the subject's age and usually does not
    know their measured maximum, and because computing it in the UI instead
    would put the formula somewhere it could disagree with this one.

    It is an **estimate**, with a standard deviation of roughly 10 bpm in the
    original population: a measured maximum from a supervised test always wins,
    which is why :class:`TrainingProfile` stores the number rather than the age
    (see :func:`parse_profile`). ``Result`` rather than a clamp, because
    clamping an age of 300 to 100 answers a question nobody asked.
    """
    if age_years < MIN_SUBJECT_AGE_YEARS or age_years > MAX_SUBJECT_AGE_YEARS:
        return Err(
            OutOfRange(
                quantity="subject_age_years",
                value=float(age_years),
                low=float(MIN_SUBJECT_AGE_YEARS),
                high=float(MAX_SUBJECT_AGE_YEARS),
            )
        )
    return Ok(Bpm(round(TANAKA_INTERCEPT - TANAKA_SLOPE * age_years)))


#: A profile id is lowercase ``snake_case``, starts with a letter, and is at
#: most 64 characters, because it is a **stored-data key**: the session record,
#: the operator's history and any exported report all key on it, so renaming
#: one is a breaking change rather than a tidy-up.
#:
#: Anchored with ``\A``/``\Z`` rather than ``^``/``$``: ``$`` also matches
#: *before* a trailing newline, so a pasted ``"standard_30_min\n"`` would
#: otherwise be accepted as an id whose end nobody can see. The same trap is
#: documented on ``src.training.types.RULE_ID_PATTERN``; the two patterns are
#: kept separate because rule ids and profile ids are different namespaces that
#: must stay free to diverge.
PROFILE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")


def is_profile_id(value: str) -> bool:
    """Whether ``value`` is a well-formed profile id. Total, and it never raises."""
    return PROFILE_ID_PATTERN.fullmatch(value) is not None


# =========================================================================
# What a profile records from
# =========================================================================


@unique
class Channel(Enum):
    """A BITalino analog input, by the name the acquisition layer uses.

    The values are **exactly** the keys of ``CHANNEL_MAP`` in
    ``src/bitalino_client.py``, which is the single source of truth mapping a
    sensor name to an analog column (A1-A6 -> 0-5). They are duplicated here
    rather than imported because that module imports the ``bitalino`` vendor
    package, which this one must not depend on; ``tests/test_plan.py`` parses
    its source and fails if the two ever disagree.

    Why that test matters more than it looks: the name selects the **column** a
    sample is read from. A silent rename would not produce missing data, it
    would produce an EMG or a light reading arriving where the ECG belongs, and
    the machine would then regulate its speed on it.
    """

    ECG = "ECG"
    # A1. The only channel this system can regulate on: it is where the heart
    # rate comes from. A profile without it is refused.

    EDA = "EDA"
    # A2. Electrodermal activity. Recorded, never regulated on.

    SPO2 = "SpO2"
    # A3. Pulse oximetry via a finger clip. Note the mixed case: the wire name
    # is "SpO2", and this enum's job is to match the wire, not to be tidy.

    RESP = "RESP"
    # A4. Respiration band.

    EMG = "EMG"
    # A5. Electromyography.

    LUX = "LUX"
    # A6. Light, or whatever else is wired to the sixth input.


_CHANNEL_BY_WIRE: Final[Mapping[str, Channel]] = MappingProxyType(
    {member.value: member for member in Channel}
)
"""Wire name -> channel, derived from the members so the two cannot drift."""


# =========================================================================
# Why a profile can be refused
# =========================================================================


@unique
class Violation(Enum):
    """A reason a set of profile fields is not a usable training programme.

    String values because they are stored and displayed: a refusal is shown to
    the operator who has to fix it, and logged so that "the machine would not
    start this morning" is answerable afterwards. ``auto()`` would renumber on
    a reorder and rewrite the meaning of every stored refusal.

    Every member is checked at construction, so this enum is also the complete
    list of what holding a :class:`TrainingProfile` guarantees.
    """

    BAD_PROFILE_ID = "bad_profile_id"
    # Not lowercase snake_case, or empty, or over 64 characters. See
    # PROFILE_ID_PATTERN: this string keys stored session records.

    EMPTY_NAME = "empty_name"
    # An operator picks a profile by name off a screen. Two profiles both
    # showing as "" is a choice made by accident.

    NON_POSITIVE_DURATION = "non_positive_duration"
    # Some phase duration, or the total, is zero, negative, NaN or infinite. A
    # NaN duration would propagate into every "remaining" the UI shows and into
    # the phase timeline; it is refused here so that it cannot.

    COOLDOWN_FASTER_THAN_DRIVE_RAMP = "cooldown_faster_than_drive_ramp"
    # cooldown_s is shorter than COMMISSIONED_DECEL_S. See that constant: a
    # faster stop is not available on this machine, and commanding one buys a
    # freewheel instead of a ramp.

    RECOVERY_TOO_SHORT = "recovery_too_short"
    # recovery_s is below MIN_RECOVERY_S. The vasovagal window is minutes long
    # and the person is still in the machine.

    HOLD_TOO_SHORT = "hold_too_short"
    # The derived hold is shorter than hold_min_s. Either the total is too
    # small or the fixed phases are too long; the fixed phases win, because a
    # warmup is a clinical decision and a total is an operator's preference.

    BPM_NOT_POSITIVE = "bpm_not_positive"
    # A heart-rate threshold is zero or negative. Almost always a missing key
    # read as a default rather than a considered value.

    SUBJECT_HR_MAX_IMPLAUSIBLE = "subject_hr_max_implausible"
    # Outside SUBJECT_HR_MAX_MIN..SUBJECT_HR_MAX_MAX. The number every cardiac
    # check below is a fraction of, so a typo here would disable them.

    ZONE_NOT_ASCENDING = "zone_not_ascending"
    # zone_low_bpm >= zone_high_bpm: an empty or inverted zone. The control law
    # would compute a target from it and the deadband would never close.

    ZONE_ABOVE_SUBJECT_CEILING = "zone_above_subject_ceiling"
    # zone_high_bpm > ZONE_CEILING_FRACTION * subject_hr_max. THE check this
    # module exists for. See the module docstring.

    HARD_MAX_ABOVE_SUBJECT_MAX = "hard_max_above_subject_max"
    # hard_max_bpm > subject_hr_max: the "back off now" line is above anything
    # this subject can reach, so it would never fire.

    CRITICAL_NOT_ABOVE_HARD_MAX = "critical_not_above_hard_max"
    # critical_bpm <= hard_max_bpm. The two tiers would invert or coincide, and
    # the emergency response would pre-empt the ordinary one.

    RPM_NOT_POSITIVE = "rpm_not_positive"
    # min_run_rpm or max_rpm is zero or negative. This module refuses to
    # describe reverse rotation: nothing about the rig asks for it, and a
    # negative ceiling would defeat every clamp written against it.

    MIN_RUN_ABOVE_MAX = "min_run_above_max"
    # min_run_rpm > max_rpm: the lowest speed that turns is above the highest
    # speed allowed, so the only in-range output left is zero.

    RPM_ABOVE_NAMEPLATE = "rpm_above_nameplate"
    # max_rpm > NAMEPLATE_MOTOR_RPM without allow_above_nameplate.

    WARMUP_FRACTION_OUT_OF_RANGE = "warmup_fraction_out_of_range"
    # warmup_rpm_ceiling_fraction is not in (0, 1], or is not finite.

    WARMUP_CEILING_BELOW_MIN_RUN = "warmup_ceiling_below_min_run"
    # floor(max_rpm * fraction) < min_run_rpm, so the warmup's own ceiling sits
    # below the lowest speed the shaft actually turns at: the warmup could only
    # ever command zero, and the session would reach HOLD having warmed nobody.

    ECG_CHANNEL_MISSING = "ecg_channel_missing"
    # channels does not contain Channel.ECG (the empty list included). No ECG
    # is no heart rate, and no heart rate is nothing to regulate on.

    DUPLICATE_CHANNEL = "duplicate_channel"
    # The same channel listed twice. Harmless-looking, and it would be read
    # twice, double-counted, and stored twice under one name.


class ProfileRefusedError(ValueError):
    """Raised by :class:`TrainingProfile` construction when a field set is refused.

    An exception rather than a ``Result`` **on the constructor** is a
    considered exception to contract rule 3, following the precedent of
    ``src.motor.drive.RegisterMap``: making the refusal unavoidable is what
    lets every other module treat "I hold a TrainingProfile" as "these limits
    have been checked", with no re-validation and no defensive branch anywhere
    downstream.

    Nothing on the motor path ever catches this, because nothing on the motor
    path builds a profile: profiles are built at startup and when an operator
    saves one, both with the shaft stopped. The two places that need a value
    rather than a traceback - :func:`parse_profile` and
    :meth:`TrainingProfile.with_total_duration` - convert it into
    ``Err(Rejected(...))`` through one shared bridge (:func:`_attempt`), so the
    structured ``violations`` survive and the checks are never duplicated.
    """

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        self.violations: tuple[Violation, ...] = violations
        super().__init__(
            "training profile refused: " + ", ".join(item.value for item in violations)
        )


# =========================================================================
# The nominal timeline
# =========================================================================


@dataclass(frozen=True, slots=True)
class PhaseSpan:
    """One phase's place on the nominal timeline, as a half-open interval.

    Half-open ``[start, end)`` so the phases tile the session with no instant
    belonging to two of them and none belonging to nothing. The alternative -
    inclusive ends - puts every boundary in two phases at once, and "which
    phase is it" then depends on the order the intervals happen to be tested
    in.
    """

    phase: Phase
    start: Seconds
    end: Seconds

    @property
    def duration(self) -> Seconds:
        """``end - start``.

        For RECOVERY this can differ from the profile's ``recovery_s`` by a
        float epsilon, because the last span ends exactly at
        ``total_duration_s`` by definition rather than by accumulation.
        """
        return Seconds(self.end - self.start)


def _is_positive_finite(value: float) -> bool:
    """Whether ``value`` is a usable positive quantity.

    ``math.isfinite`` first, and deliberately: every comparison against NaN is
    ``False``, so a plain ``value <= 0.0`` guard **accepts** NaN. A NaN
    duration would then reach the timeline and make every phase boundary
    compare false; a NaN rpm would reach a setpoint.
    """
    return math.isfinite(value) and value > 0.0


@dataclass(frozen=True, slots=True)
class TrainingProfile:
    """A training programme template, valid by construction.

    Frozen and slotted like every record in this system, and validated in
    ``__post_init__``: there is no such thing as an invalid
    :class:`TrainingProfile`, so no consumer needs a defensive check and none
    of them can disagree about what "valid" means.

    ``hold_min_s`` is a *floor on the derived hold*, not a phase duration. See
    :attr:`hold_s` and the module docstring for why hold is the remainder.
    """

    profile_id: str
    """Stable stored-data key, lowercase snake_case. See :data:`PROFILE_ID_PATTERN`."""

    name: str
    """What the operator reads on the screen. Prose; never parsed."""

    total_duration_s: Seconds
    """Whole session, baseline through recovery.

    The one duration an operator is expected to change, because HOLD absorbs
    the difference.
    """

    baseline_s: Seconds
    """Resting measurement with nothing turning: the reference the zone is
    judged against."""

    warmup_max_s: Seconds
    """Longest the warmup may last.

    "Max" because the runner leaves WARMUP early once the heart rate reaches
    the zone; the nominal timeline uses the full span. Clinically fixed - a
    longer session does not get a longer warmup.
    """

    hold_min_s: Seconds
    """Shortest useful hold.

    A total that leaves less than this is refused rather than run: a two-minute
    hold is not a shorter session, it is a session that did not happen.
    """

    cooldown_s: Seconds
    """Controlled ramp to zero. Floored by :data:`COMMISSIONED_DECEL_S`."""

    recovery_s: Seconds
    """Motor stopped, person still in the machine, monitoring continuing.

    Floored by :data:`MIN_RECOVERY_S`.
    """

    zone_low_bpm: Bpm
    """Bottom of the target zone: below it the control law may speed up."""

    zone_high_bpm: Bpm
    """Top of the target zone.

    Bounded by :data:`ZONE_CEILING_FRACTION` of ``subject_hr_max``.
    """

    hard_max_bpm: Bpm
    """Above this the safety supervisor reduces speed.

    The first tier that is not the control law's business.
    """

    critical_bpm: Bpm
    """Above this the session ends.

    Must be strictly above ``hard_max_bpm`` so the two tiers cannot invert.
    """

    subject_hr_max: Bpm
    """THIS subject's maximum heart rate.

    Measured if it is known, otherwise estimated from their age
    (:func:`hr_max_from_age`). Per-profile rather than global, so a profile
    cloned for a different person re-validates its zone against the new ceiling
    instead of inheriting one that was safe for somebody else. The shipped
    defaults carry the conservative figure for a 65-year-old; an operator must
    set the real one.
    """

    min_run_rpm: MotorRpm
    """Lowest speed at which the shaft actually turns.

    Below it the only honest output is zero, so the control law's range is
    ``{0} u [min_run_rpm, max_rpm]`` rather than ``[0, max_rpm]``.
    """

    max_rpm: MotorRpm
    """Software speed ceiling at the MOTOR shaft.

    Set the drive's HSP to :attr:`hsp_hertz` to match it - see the module
    docstring.
    """

    warmup_rpm_ceiling_fraction: float
    """Fraction of ``max_rpm`` the warmup may reach, in ``(0, 1]``.

    A dimensionless ratio, so not a unit type.
    """

    channels: tuple[Channel, ...]
    """What to record. Must contain :data:`Channel.ECG` and no duplicates.

    A tuple, not a set: the order is the order the acquisition layer requests
    the analog columns in, and a set would reorder it between runs.
    """

    allow_above_nameplate: bool = False
    """Deliberate consent to a ``max_rpm`` above :data:`NAMEPLATE_MOTOR_RPM`.

    Defaulted, and the default is the safe one. Its only purpose is to make
    exceeding the nameplate something somebody typed rather than something that
    happened.
    """

    def __post_init__(self) -> None:
        violations = _violations(self)
        if violations:
            raise ProfileRefusedError(violations)

    # --- derived quantities ---------------------------------------------

    @property
    def fixed_phases_s(self) -> Seconds:
        """Everything except HOLD: the part a total-duration change must not move."""
        return Seconds(self.baseline_s + self.warmup_max_s + self.cooldown_s + self.recovery_s)

    @property
    def hold_s(self) -> Seconds:
        """The working phase: the total, less every fixed phase.

        Derived rather than stored so the override rule cannot be got wrong -
        see the module docstring. It computes as negative on a field set that
        is about to be refused for exactly that reason, which is why
        ``__post_init__`` runs the checks rather than trusting this.
        """
        return Seconds(self.total_duration_s - self.fixed_phases_s)

    @property
    def warmup_rpm_ceiling(self) -> MotorRpm:
        """Highest speed the warmup may command, at the motor shaft.

        ``floor``, not ``round``: rounding a ceiling upwards would permit a
        speed above the fraction that was actually authorised.
        """
        return MotorRpm(math.floor(self.max_rpm * self.warmup_rpm_ceiling_fraction))

    @property
    def hsp_hertz(self) -> Hertz:
        """The drive HSP value that matches :attr:`max_rpm`, in Hz.

        This number is the point of the commissioning procedure in the module
        docstring: it is what an operator types into the drive so the hardware
        ceiling tracks the software one. Computed through ``src.units``, which
        owns every conversion in this system.
        """
        return motor_rpm_to_hertz(self.max_rpm, NAMEPLATE_MOTOR_RPM, NAMEPLATE_BASE_HERTZ)

    @property
    def timeline(self) -> tuple[PhaseSpan, ...]:
        """The five timed phases, in order, tiling ``[0, total_duration_s)``.

        DONE is deliberately absent: it is not a span but everything at or
        after the end, so giving it an interval would mean inventing an
        infinite one. :meth:`phase_at` returns it by falling off the end.

        The last span ends at ``total_duration_s`` **exactly** rather than at
        the accumulated sum, so ``phase_at(total)`` is DONE and ``phase_at``
        just below it is RECOVERY, with no float slop at the one boundary a UI
        counts down to.

        Rebuilt per call: five small frozen records at 5 Hz is free, and
        caching would mean mutable state inside a frozen record.
        """
        baseline_end = self.baseline_s
        warmup_end = Seconds(baseline_end + self.warmup_max_s)
        hold_end = Seconds(warmup_end + self.hold_s)
        cooldown_end = Seconds(hold_end + self.cooldown_s)
        return (
            PhaseSpan(Phase.BASELINE, Seconds(0.0), baseline_end),
            PhaseSpan(Phase.WARMUP, baseline_end, warmup_end),
            PhaseSpan(Phase.HOLD, warmup_end, hold_end),
            PhaseSpan(Phase.COOLDOWN, hold_end, cooldown_end),
            PhaseSpan(Phase.RECOVERY, cooldown_end, self.total_duration_s),
        )

    def phase_at(self, elapsed: Seconds) -> tuple[Phase, Seconds, Seconds]:
        """Where the nominal programme is at ``elapsed``.

        Returns ``(phase, phase_elapsed, phase_remaining)``. Pure: no clock and
        no state, so a 45-minute programme can be walked end to end in a
        millisecond (contract rule 4).

        Intervals are half-open, so a boundary belongs to the phase it opens:
        at ``elapsed == baseline_s`` the answer is WARMUP with zero elapsed,
        not BASELINE with zero remaining.

        Two inputs that should not happen are answered rather than rejected,
        because this is called from the render path as well as the runner:

        * a **negative** elapsed is treated as zero. It cannot arise from
          ``src.units.elapsed`` on a monotonic clock, and of the available
          answers only "the session has not started" cannot command motion.
        * a **non-finite** elapsed yields DONE with zeros. NaN compares false
          against every boundary, so a bare fall-through would hand out DONE
          with a NaN ``phase_elapsed`` and put the NaN on the screen. DONE is
          also the only phase in which no setpoint is ever issued again, which
          is the right answer to arithmetic that has stopped making sense.
        """
        if not math.isfinite(elapsed):
            return (Phase.DONE, Seconds(0.0), Seconds(0.0))
        position = max(0.0, elapsed)
        for span in self.timeline:
            if position < span.end:
                return (
                    span.phase,
                    Seconds(position - span.start),
                    Seconds(span.end - position),
                )
        return (Phase.DONE, Seconds(position - self.total_duration_s), Seconds(0.0))

    def with_total_duration(self, total: Seconds) -> Result[TrainingProfile, Rejected]:
        """The same programme over a different total, with HOLD absorbing the change.

        The one supported override, and the reason ``hold_s`` is derived: every
        fixed phase keeps its declared length and the working phase takes the
        difference. A total that would leave less than ``hold_min_s`` of hold is
        refused, which is the whole point of having a floor.

        ``Result`` rather than the raising constructor, because an operator
        changing the session length on a screen must get a message and not a
        traceback. See :class:`ProfileRefusedError` for why both exist.
        """
        return _attempt(lambda: replace(self, total_duration_s=total))


# =========================================================================
# The checks, one function each
# =========================================================================
#
# Split one-per-violation rather than written as a single validating block for
# three reasons: each is individually testable, the reported order is fixed by
# the order of _CHECKS below (so a refusal message is reproducible), and no
# single function accumulates the branch count of twenty conditions.


def _check_profile_id(profile: TrainingProfile) -> Violation | None:
    if is_profile_id(profile.profile_id):
        return None
    return Violation.BAD_PROFILE_ID


def _check_name(profile: TrainingProfile) -> Violation | None:
    if profile.name.strip():
        return None
    return Violation.EMPTY_NAME


def _check_durations_positive(profile: TrainingProfile) -> Violation | None:
    durations = (
        profile.total_duration_s,
        profile.baseline_s,
        profile.warmup_max_s,
        profile.hold_min_s,
        profile.cooldown_s,
        profile.recovery_s,
    )
    if all(_is_positive_finite(value) for value in durations):
        return None
    return Violation.NON_POSITIVE_DURATION


def _check_cooldown_not_faster_than_drive(profile: TrainingProfile) -> Violation | None:
    if profile.cooldown_s >= COMMISSIONED_DECEL_S:
        return None
    return Violation.COOLDOWN_FASTER_THAN_DRIVE_RAMP


def _check_recovery_long_enough(profile: TrainingProfile) -> Violation | None:
    if profile.recovery_s >= MIN_RECOVERY_S:
        return None
    return Violation.RECOVERY_TOO_SHORT


def _check_hold_long_enough(profile: TrainingProfile) -> Violation | None:
    if profile.hold_s >= profile.hold_min_s:
        return None
    return Violation.HOLD_TOO_SHORT


def _check_bpm_positive(profile: TrainingProfile) -> Violation | None:
    thresholds = (
        profile.zone_low_bpm,
        profile.zone_high_bpm,
        profile.hard_max_bpm,
        profile.critical_bpm,
    )
    if all(value > 0 for value in thresholds):
        return None
    return Violation.BPM_NOT_POSITIVE


def _check_subject_hr_max_plausible(profile: TrainingProfile) -> Violation | None:
    if SUBJECT_HR_MAX_MIN <= profile.subject_hr_max <= SUBJECT_HR_MAX_MAX:
        return None
    return Violation.SUBJECT_HR_MAX_IMPLAUSIBLE


def _check_zone_ascends(profile: TrainingProfile) -> Violation | None:
    if profile.zone_low_bpm < profile.zone_high_bpm:
        return None
    return Violation.ZONE_NOT_ASCENDING


def _check_zone_below_subject_ceiling(profile: TrainingProfile) -> Violation | None:
    if profile.zone_high_bpm <= ZONE_CEILING_FRACTION * profile.subject_hr_max:
        return None
    return Violation.ZONE_ABOVE_SUBJECT_CEILING


def _check_hard_max_within_subject_max(profile: TrainingProfile) -> Violation | None:
    if profile.hard_max_bpm <= profile.subject_hr_max:
        return None
    return Violation.HARD_MAX_ABOVE_SUBJECT_MAX


def _check_critical_above_hard_max(profile: TrainingProfile) -> Violation | None:
    if profile.critical_bpm > profile.hard_max_bpm:
        return None
    return Violation.CRITICAL_NOT_ABOVE_HARD_MAX


def _check_rpm_positive(profile: TrainingProfile) -> Violation | None:
    if profile.min_run_rpm > 0 and profile.max_rpm > 0:
        return None
    return Violation.RPM_NOT_POSITIVE


def _check_min_run_within_max(profile: TrainingProfile) -> Violation | None:
    if profile.min_run_rpm <= profile.max_rpm:
        return None
    return Violation.MIN_RUN_ABOVE_MAX


def _check_max_rpm_within_nameplate(profile: TrainingProfile) -> Violation | None:
    if profile.allow_above_nameplate or profile.max_rpm <= NAMEPLATE_MOTOR_RPM:
        return None
    return Violation.RPM_ABOVE_NAMEPLATE


def _check_warmup_fraction(profile: TrainingProfile) -> Violation | None:
    fraction = profile.warmup_rpm_ceiling_fraction
    if _is_positive_finite(fraction) and fraction <= 1.0:
        return None
    return Violation.WARMUP_FRACTION_OUT_OF_RANGE


def _check_warmup_ceiling_turns(profile: TrainingProfile) -> Violation | None:
    # Guarded on the two fields it derives from, because this check runs even
    # when they are themselves refused: floor(-inf) raises OverflowError, and a
    # refusal must never turn into a traceback.
    if not _is_positive_finite(profile.warmup_rpm_ceiling_fraction) or profile.max_rpm <= 0:
        return None
    if profile.warmup_rpm_ceiling >= profile.min_run_rpm:
        return None
    return Violation.WARMUP_CEILING_BELOW_MIN_RUN


def _check_ecg_channel(profile: TrainingProfile) -> Violation | None:
    if Channel.ECG in profile.channels:
        return None
    return Violation.ECG_CHANNEL_MISSING


def _check_channels_unique(profile: TrainingProfile) -> Violation | None:
    if len(set(profile.channels)) == len(profile.channels):
        return None
    return Violation.DUPLICATE_CHANNEL


_CHECKS: Final[tuple[Callable[[TrainingProfile], Violation | None], ...]] = (
    _check_profile_id,
    _check_name,
    _check_durations_positive,
    _check_cooldown_not_faster_than_drive,
    _check_recovery_long_enough,
    _check_hold_long_enough,
    _check_bpm_positive,
    _check_subject_hr_max_plausible,
    _check_zone_ascends,
    _check_zone_below_subject_ceiling,
    _check_hard_max_within_subject_max,
    _check_critical_above_hard_max,
    _check_rpm_positive,
    _check_min_run_within_max,
    _check_max_rpm_within_nameplate,
    _check_warmup_fraction,
    _check_warmup_ceiling_turns,
    _check_ecg_channel,
    _check_channels_unique,
)
"""Every check, in the order refusals are reported.

Order is part of the contract only in that it is *stable*: a refusal message
must read the same way twice so an operator fixing a file is not chasing a
moving target.
"""


def _violations(profile: TrainingProfile) -> tuple[Violation, ...]:
    """Every refusal that applies, rather than the first one.

    All of them, because a hand-edited JSON usually has more than one problem
    and a store that reports them one save at a time wastes an operator's
    afternoon. The checks are independent and none of them raises.
    """
    return tuple(found for check in _CHECKS if (found := check(profile)) is not None)


# =========================================================================
# The resolved programme
# =========================================================================

StoreRev = NewType("StoreRev", int)
"""A monotonic revision of the whole profile store, for optimistic concurrency.

A ``NewType`` even though it is not a physical quantity, and defined here
rather than in ``src/units.py`` because it is this module's own concept: that
file holds domain units, and a store revision is not one. The reason it is not
a bare ``int`` is the same reason the units exist - it sits next to a heart
rate, a profile count and a metric sequence number in the same code, and the
compiler should be the thing that keeps them apart.
"""

INITIAL_REV: Final[StoreRev] = StoreRev(0)
"""The revision of a store that has never been written."""


@dataclass(frozen=True, slots=True)
class Program:
    """The resolved programme a session actually ran. Frozen, and that is the point.

    A :class:`TrainingProfile` is a document an operator can edit at any
    moment, including at minute twelve of a thirty-minute session. If the
    session record referred to the profile *by id*, that edit would silently
    rewrite history: the stored session would claim a zone, a ceiling and a
    duration that were never commanded. So this carries a **copy** of the
    profile as it stood when the session started, plus the revision it was
    copied from.

    ``profile`` here is the *resolved* profile: if the operator overrode the
    total duration, it is the profile with that total and the re-derived hold,
    already re-validated. There is therefore exactly one total duration in this
    object and one timeline, and they cannot contradict each other.
    """

    profile: TrainingProfile
    """The programme as run: frozen, validated, and not a reference to a
    document that can still change."""

    source_rev: StoreRev
    """The store revision this was copied from.

    Provenance, and the field that makes the copy auditable: it says which
    revision of the profile set was in force, so a stored session can be lined
    up against the file's history.
    """

    resolved_at: UnixMillis
    """Wall-clock instant the programme was frozen.

    A parameter of :meth:`ProfileStore.resolve`, never read from a clock here
    (contract rule 4). Wall-clock rather than monotonic because this stamp
    leaves the machine: a monotonic reading means nothing to a server. Elapsed
    time inside the session is measured monotonically by the runner, which is
    the arrangement that survives the Pi having no RTC.
    """

    total_overridden: bool
    """Whether the operator replaced the profile's stored total duration.

    Genuinely new information rather than a derived flag: it distinguishes "the
    45-minute profile" from "the 30-minute profile stretched to 45", which read
    identically from the resolved fields alone and mean different things when
    somebody reviews the session.
    """

    @property
    def source_profile_id(self) -> str:
        """Which stored profile this came from.

        A property, not a field: ``profile.profile_id`` is already that string,
        and storing it twice would create two places that can disagree - the
        trap ``src.training.types.TelemetrySnapshot`` documents for phase and
        setpoint. Provenance that is *not* recoverable from the copy lives in
        :attr:`source_rev`.
        """
        return self.profile.profile_id

    @property
    def total_duration_s(self) -> Seconds:
        """The resolved total. One number, in one place."""
        return self.profile.total_duration_s

    @property
    def timeline(self) -> tuple[PhaseSpan, ...]:
        """The nominal timeline of the programme as run."""
        return self.profile.timeline

    def phase_at(self, elapsed: Seconds) -> tuple[Phase, Seconds, Seconds]:
        """Delegates to :meth:`TrainingProfile.phase_at` on the resolved profile.

        Here so the runner asks the programme rather than reaching through it,
        and so nothing is tempted to consult a different profile than the one
        that was frozen.
        """
        return self.profile.phase_at(elapsed)


# =========================================================================
# The closed error unions
# =========================================================================
#
# Small, purpose-built unions rather than one big one, so that each operation's
# signature says exactly what can go wrong with it and an exhaustive match on
# one does not have to handle failures that operation cannot produce.


@dataclass(frozen=True, slots=True)
class Malformed:
    """A document did not have the shape a profile store needs.

    ``problems`` is the machine-readable list, in document order; ``detail`` is
    the same thing joined into one line for a log. Both, because an operator
    editing JSON wants every problem at once and a log line wants one string.
    """

    detail: str
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Rejected:
    """The fields were well-formed but are not a safe programme.

    Distinct from :class:`Malformed` because the two need different responses:
    malformed is a typo, rejected is a limit. ``violations`` carries the enum
    so a UI can highlight the offending field rather than print prose.
    """

    violations: tuple[Violation, ...]
    detail: str


@dataclass(frozen=True, slots=True)
class UnknownProfile:
    """No profile with that id is in the store.

    ``known`` is carried because the answer to "unknown profile
    'standard_30min'" is almost always visible in the list of ids that do
    exist, and an operator at 2am should not have to go and find the file.
    """

    profile_id: str
    known: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RevMismatch:
    """Somebody else changed the store since this caller read it.

    The whole optimistic-concurrency scheme in one variant: two browser tabs,
    or a tab and the API, editing the same profile set. Last-write-wins would
    silently discard an edit, and the edit in question changes heart-rate
    limits.
    """

    expected: StoreRev
    actual: StoreRev


@dataclass(frozen=True, slots=True)
class StoreUnwritable:
    """The store file could not be replaced, so nothing was changed.

    Carries the path as a ``str`` rather than a ``Path``: an error value is
    evidence to be logged and shown, not a handle for the recipient to do more
    I/O with.
    """

    path: str
    detail: str


@dataclass(frozen=True, slots=True)
class DefaultsUnusable:
    """The shipped defaults themselves are missing or invalid.

    The one failure :meth:`ProfileStore.load` cannot recover from, because the
    defaults *are* the recovery. It means the installation is broken rather
    than the data, so the correct response is to refuse to start - which is
    also why it is a separate variant from :class:`Malformed`.
    """

    path: str
    detail: str


type ProfileParseError = Malformed | Rejected
"""Why one profile document could not become a :class:`TrainingProfile`."""

type UpsertError = RevMismatch | StoreUnwritable
"""Why a profile could not be saved.

No ``Rejected`` here: :meth:`ProfileStore.upsert` takes a
:class:`TrainingProfile`, which cannot be invalid. Validation happens where the
untrusted document is parsed, once.
"""

type DeleteError = UnknownProfile | RevMismatch | StoreUnwritable
"""Why a profile could not be deleted."""

type ResolveError = UnknownProfile | Rejected
"""Why a programme could not be resolved.

``Rejected`` is reachable only through a total-duration override: the stored
profile is valid by construction, but a total that leaves too little hold is
not.
"""


def _attempt(build: Callable[[], TrainingProfile]) -> Result[TrainingProfile, Rejected]:
    """The one bridge from the raising constructor to the ``Result`` world.

    Exists so the checks live in exactly one place. The alternative - a
    separate non-raising validator - would be a second copy of every limit in
    this file, free to drift from the one that actually guards construction,
    which is how a profile ends up accepted by the API and refused at startup.

    Only :class:`ProfileRefusedError` is caught. Anything else from a
    constructor is a bug in this module and must not be turned into a tidy
    ``Err`` that a caller shrugs off.
    """
    try:
        profile = build()
    except ProfileRefusedError as refusal:
        return Err(Rejected(violations=refusal.violations, detail=str(refusal)))
    else:
        return Ok(profile)


# =========================================================================
# Reading an untrusted document
# =========================================================================
#
# "Parse, don't validate" (contract rule 6) applied to a JSON file an operator
# may have edited by hand. Exactly one layer turns `object` into domain types,
# and past it the static types carry the guarantee.

_MISSING: Final[object] = object()
"""Sentinel for "the key was not there", so a present ``null`` and an absent
key can be told apart in a message without a second lookup."""


def _kind(value: object) -> str:
    """What a value is, in words an operator can match against their JSON."""
    if value is _MISSING:
        return "nothing"
    if value is None:
        return "null"
    return type(value).__name__


@final
class _Reader:
    """A typed, error-accumulating reader over one parsed JSON object.

    Accumulating rather than short-circuiting on purpose: a hand-edited profile
    usually has several problems, and reporting them one save at a time turns a
    two-minute fix into twenty. Every accessor is total - it records a problem
    and returns a placeholder - and the caller checks :attr:`problems` once
    before building anything.

    The placeholders are safe precisely because of that ordering: no caller in
    this module constructs a :class:`TrainingProfile` while ``problems`` is
    non-empty, and a test pins that.
    """

    __slots__ = ("_doc", "_problems", "_where")

    def __init__(self, document: Mapping[str, object], where: str) -> None:
        self._doc: Mapping[str, object] = document
        self._where: str = where
        # MUTABLE, deliberately, and the only mutable state in this module
        # besides the store's cache: this list is the accumulator described
        # above. It is per-parse and never shared.
        self._problems: list[str] = []

    @property
    def problems(self) -> tuple[str, ...]:
        """Every problem found so far, in the order the keys were read."""
        return tuple(self._problems)

    def note(self, problem: str) -> None:
        """Record a problem this reader's accessors cannot see themselves."""
        self._problems.append(f"{self._where}: {problem}")

    def has(self, key: str) -> bool:
        """Whether the key is present at all, without reading it."""
        return key in self._doc

    def text(self, key: str) -> str:
        """A string, or ``""`` with a problem recorded."""
        value = self._doc.get(key, _MISSING)
        if isinstance(value, str):
            return value
        self.note(f"{key!r} must be a string, got {_kind(value)}")
        return ""

    def number(self, key: str) -> float:
        """A finite-or-not real number, or ``0.0`` with a problem recorded.

        ``bool`` is rejected before ``int``, because ``bool`` is a subclass of
        ``int`` and ``float(True)`` is ``1.0``: a stray ``true`` where a
        duration belongs would otherwise be accepted as one second.

        Non-finite values (JSON's non-standard ``NaN`` and ``Infinity``, which
        ``json.loads`` accepts by default) are passed through rather than
        rejected here, and refused by :data:`Violation.NON_POSITIVE_DURATION`
        at construction. One place decides what a usable quantity is.
        """
        value = self._doc.get(key, _MISSING)
        if isinstance(value, bool):
            self.note(f"{key!r} must be a number, got bool")
            return 0.0
        if isinstance(value, int | float):
            return float(value)
        self.note(f"{key!r} must be a number, got {_kind(value)}")
        return 0.0

    def integer(self, key: str) -> int:
        """An integer, or ``0`` with a problem recorded. Rejects ``bool`` and floats.

        A float is refused rather than truncated: ``"zone_high_bpm": 138.7`` is
        somebody's arithmetic, and silently reading it as 138 would lower a
        heart-rate limit by rounding.
        """
        value = self._doc.get(key, _MISSING)
        if isinstance(value, bool):
            self.note(f"{key!r} must be a whole number, got bool")
            return 0
        if isinstance(value, int):
            return value
        self.note(f"{key!r} must be a whole number, got {_kind(value)}")
        return 0

    def flag(self, key: str, *, default: bool) -> bool:
        """A boolean, defaulted when absent, with a problem recorded when wrong."""
        value = self._doc.get(key, _MISSING)
        if value is _MISSING:
            return default
        if isinstance(value, bool):
            return value
        self.note(f"{key!r} must be true or false, got {_kind(value)}")
        return default

    def channels(self, key: str) -> tuple[Channel, ...]:
        """A list of channel names, skipping (and reporting) any that is unknown.

        An unknown name is never guessed at. The name chooses which analog
        column a sample is read from, so accepting a near-miss would feed the
        control law somebody's muscle activity.
        """
        value = self._doc.get(key, _MISSING)
        if not isinstance(value, list):
            self.note(f"{key!r} must be a list of channel names, got {_kind(value)}")
            return ()
        # JSON arrays hold JSON values; narrowing `list` to `Sequence[object]`
        # states that without inviting an `Unknown` element type in.
        names = cast("Sequence[object]", value)
        parsed: list[Channel] = []
        for index, name in enumerate(names):
            if not isinstance(name, str):
                self.note(f"{key}[{index}] must be a channel name, got {_kind(name)}")
                continue
            channel = _CHANNEL_BY_WIRE.get(name)
            if channel is None:
                self.note(f"{key}[{index}] is not a known channel: {name!r}")
                continue
            parsed.append(channel)
        return tuple(parsed)


def _read_subject_hr_max(reader: _Reader) -> Bpm:
    """The subject ceiling, stored directly or derived from an age.

    ``subject_hr_max`` wins when both are present, and is what
    :func:`profile_to_document` writes back. Storing the number rather than the
    age is deliberate: the age-based figure is an estimate, and if a later
    version adopted a different formula, every profile that had stored only an
    age would silently re-zone itself - quietly widening somebody's target zone
    without anyone editing anything.
    """
    if reader.has("subject_hr_max"):
        return Bpm(reader.integer("subject_hr_max"))
    if not reader.has("subject_age_years"):
        reader.note("needs either 'subject_hr_max' or 'subject_age_years'")
        return Bpm(0)
    estimate = hr_max_from_age(reader.integer("subject_age_years"))
    if isinstance(estimate, Err):
        limit = estimate.error
        reader.note(f"'subject_age_years' {limit.value:g} is outside {limit.low:g}..{limit.high:g}")
        return Bpm(0)
    return estimate.value


def parse_profile(
    document: Mapping[str, object], *, where: str = "profile"
) -> Result[TrainingProfile, ProfileParseError]:
    """Turn one untrusted JSON object into a validated profile.

    The boundary this module draws: everything above it deals in
    :class:`TrainingProfile`, which cannot be invalid. ``where`` is a label for
    the problem messages, so a store with four profiles can say which one.

    Two distinct failures, and they are not interchangeable. :class:`Malformed`
    means the document is not a profile - a key missing, a string where a
    number belongs - and the operator has a typo. :class:`Rejected` means it is
    a perfectly well-formed profile that this machine will not run, and the
    operator has a limit to reconsider. Collapsing them would make "you typed
    the wrong thing" and "that zone is above this subject's maximum" look the
    same on screen.
    """
    reader = _Reader(document, where)
    profile_id = reader.text("profile_id")
    name = reader.text("name")
    total_duration_s = Seconds(reader.number("total_duration_s"))
    baseline_s = Seconds(reader.number("baseline_s"))
    warmup_max_s = Seconds(reader.number("warmup_max_s"))
    hold_min_s = Seconds(reader.number("hold_min_s"))
    cooldown_s = Seconds(reader.number("cooldown_s"))
    recovery_s = Seconds(reader.number("recovery_s"))
    zone_low_bpm = Bpm(reader.integer("zone_low_bpm"))
    zone_high_bpm = Bpm(reader.integer("zone_high_bpm"))
    hard_max_bpm = Bpm(reader.integer("hard_max_bpm"))
    critical_bpm = Bpm(reader.integer("critical_bpm"))
    subject_hr_max = _read_subject_hr_max(reader)
    min_run_rpm = MotorRpm(reader.integer("min_run_rpm"))
    max_rpm = MotorRpm(reader.integer("max_rpm"))
    fraction = reader.number("warmup_rpm_ceiling_fraction")
    channels = reader.channels("channels")
    allow_above_nameplate = reader.flag("allow_above_nameplate", default=False)

    problems = reader.problems
    if problems:
        return Err(Malformed(detail="; ".join(problems), problems=problems))
    return _attempt(
        lambda: TrainingProfile(
            profile_id=profile_id,
            name=name,
            total_duration_s=total_duration_s,
            baseline_s=baseline_s,
            warmup_max_s=warmup_max_s,
            hold_min_s=hold_min_s,
            cooldown_s=cooldown_s,
            recovery_s=recovery_s,
            zone_low_bpm=zone_low_bpm,
            zone_high_bpm=zone_high_bpm,
            hard_max_bpm=hard_max_bpm,
            critical_bpm=critical_bpm,
            subject_hr_max=subject_hr_max,
            min_run_rpm=min_run_rpm,
            max_rpm=max_rpm,
            warmup_rpm_ceiling_fraction=fraction,
            channels=channels,
            allow_above_nameplate=allow_above_nameplate,
        )
    )


type JsonValue = str | int | float | bool | Sequence[JsonValue] | Mapping[str, JsonValue] | None
"""What ``json.dumps`` is given, spelled out.

A typed document model rather than ``object``, so that building the file is
checked rather than hoped for. Recursive, which PEP 695 aliases allow.
"""


def profile_to_document(profile: TrainingProfile) -> Mapping[str, JsonValue]:
    """The exact inverse of :func:`parse_profile`, key for key.

    Keys are the field names, deliberately: a stored file is greppable against
    the dataclass, and adding a field without adding it here shows up as a
    round-trip test failure rather than as a silently defaulted value in
    somebody's stored profile.

    ``subject_age_years`` is never written. It is an input convenience only -
    see :func:`_read_subject_hr_max` for why the derived number is what gets
    stored.
    """
    return {
        "profile_id": profile.profile_id,
        "name": profile.name,
        "total_duration_s": float(profile.total_duration_s),
        "baseline_s": float(profile.baseline_s),
        "warmup_max_s": float(profile.warmup_max_s),
        "hold_min_s": float(profile.hold_min_s),
        "cooldown_s": float(profile.cooldown_s),
        "recovery_s": float(profile.recovery_s),
        "zone_low_bpm": int(profile.zone_low_bpm),
        "zone_high_bpm": int(profile.zone_high_bpm),
        "hard_max_bpm": int(profile.hard_max_bpm),
        "critical_bpm": int(profile.critical_bpm),
        "subject_hr_max": int(profile.subject_hr_max),
        "min_run_rpm": int(profile.min_run_rpm),
        "max_rpm": int(profile.max_rpm),
        "warmup_rpm_ceiling_fraction": float(profile.warmup_rpm_ceiling_fraction),
        "channels": [channel.value for channel in profile.channels],
        "allow_above_nameplate": profile.allow_above_nameplate,
    }


# =========================================================================
# The store file
# =========================================================================

SCHEMA_VERSION: Final[int] = 1
"""Version of the store file's own shape.

Checked, and an unrecognised version is refused rather than read hopefully. A
newer build writing a field this one does not understand is exactly the
situation where guessing produces a profile that looks fine and is not the one
the operator saved.
"""

DEFAULT_STORE_FILENAME: Final[str] = "profiles.json"
"""Name of the working store, alongside the shipped defaults."""

SHIPPED_DEFAULTS_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent / "config" / "profiles.default.json"
)
"""The read-only profiles that ship with the application.

Resolved from this file's location rather than from the working directory,
because the recovery path in :meth:`ProfileStore.load` must work regardless of
where the process was started from - including from an ``atexit`` handler or a
systemd unit with a different cwd.
"""


@unique
class LoadSource(Enum):
    """Where the profiles in the store actually came from.

    Reported rather than inferred, because two of the three mean the operator's
    own profiles are not loaded, and a screen that does not say so is a screen
    that lets somebody start a session against the wrong limits.
    """

    STORE_FILE = "store_file"
    # The normal case: the working store parsed cleanly.

    DEFAULTS_NO_STORE = "defaults_no_store"
    # First boot. The shipped defaults are in memory and the store file has
    # deliberately NOT been created: the first upsert writes it. One less write
    # to an SD card at startup, and nothing to clean up if the unit never runs.

    DEFAULTS_AFTER_CORRUPTION = "defaults_after_corruption"
    # The store file existed and could not be used. It has been renamed out of
    # the way and the defaults are in memory. LOUD: see LoadReport.detail.


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What :meth:`ProfileStore.load` found, for the log and the operator screen."""

    source: LoadSource
    rev: StoreRev
    profile_ids: tuple[str, ...]
    quarantined: Path | None
    """Where a corrupt store file was moved, or ``None``.

    ``None`` after a *successful* recovery only when the rename itself failed;
    the profiles are still the defaults either way, and ``source`` is what says
    so. Kept as a ``Path`` because an operator is going to be told to go and
    look at this file.
    """

    detail: str
    """One line for the log. Prose; never parsed."""


@dataclass(frozen=True, slots=True)
class StoreContent:
    """A parsed store file: the revision and the profiles, nothing else.

    Public because :func:`parse_store` is: an operator tool that checks a
    profiles file before it is deployed needs to say what it found, and a
    ``(rev, profiles)`` pair passed around loose is a pair that gets swapped.
    """

    rev: StoreRev
    profiles: tuple[TrainingProfile, ...]


@dataclass(frozen=True, slots=True)
class _Absent:
    """The store file is not there. Normal on first boot."""


@dataclass(frozen=True, slots=True)
class _Unreadable:
    """The store file is there and could not be read. Treated as corruption."""

    detail: str


type _ReadFailure = _Absent | _Unreadable


def _read_text(path: Path) -> Result[str, _ReadFailure]:
    """Read a UTF-8 file, distinguishing "not there" from "went wrong".

    The distinction drives the whole recovery decision: an absent file is a
    first boot and must not raise an alarm, while a file that is present and
    unreadable is treated exactly like a corrupt one - it gets moved aside and
    the defaults take over.
    """
    try:
        return Ok(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Err(_Absent())
    except (OSError, UnicodeDecodeError) as failure:
        return Err(_Unreadable(detail=f"{type(failure).__name__}: {failure}"))


def _load_json_object(raw: str) -> Result[Mapping[str, object], Malformed]:
    """Parse JSON and insist the top level is an object.

    The single point where an untyped value enters this module. It is annotated
    as ``object`` immediately, exactly as ``tests/test_typing_contract.py``
    does with ``tomllib``, so nothing downstream is ``Any``-typed and every
    field goes through :class:`_Reader`.
    """
    try:
        parsed: object = json.loads(raw)  # pyright: ignore[reportAny]  # narrowed below
    except (json.JSONDecodeError, ValueError) as failure:
        detail = f"not valid JSON: {failure}"
        return Err(Malformed(detail=detail, problems=(detail,)))
    if not isinstance(parsed, dict):
        detail = f"top level must be an object, got {_kind(parsed)}"
        return Err(Malformed(detail=detail, problems=(detail,)))
    # JSON object keys are strings by construction, so this cast is a statement
    # of the format rather than an assumption about the data.
    return Ok(cast("Mapping[str, object]", parsed))


def _read_profile_entries(
    reader: _Reader, document: Mapping[str, object]
) -> tuple[Mapping[str, object], ...]:
    """The ``profiles`` array, as objects, reporting anything that is not one."""
    value = document.get("profiles", _MISSING)
    if not isinstance(value, list):
        reader.note(f"'profiles' must be a list, got {_kind(value)}")
        return ()
    items = cast("Sequence[object]", value)
    entries: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            reader.note(f"profiles[{index}] must be an object, got {_kind(item)}")
            continue
        entries.append(cast("Mapping[str, object]", item))
    return tuple(entries)


def _parse_profiles(
    reader: _Reader, entries: tuple[Mapping[str, object], ...]
) -> tuple[TrainingProfile, ...]:
    """Parse every entry, reporting problems rather than stopping at the first.

    A duplicate id is a problem and not a merge: one of the two would shadow
    the other, and which one won would depend on iteration order - so a file
    that contains two profiles called ``standard_30_min`` is refused rather
    than half-loaded.
    """
    profiles: list[TrainingProfile] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        outcome = parse_profile(entry, where=f"profiles[{index}]")
        if isinstance(outcome, Err):
            reader.note(_describe_parse_error(outcome.error, index))
            continue
        profile = outcome.value
        if profile.profile_id in seen:
            reader.note(f"profiles[{index}] repeats the id {profile.profile_id!r}")
            continue
        seen.add(profile.profile_id)
        profiles.append(profile)
    return tuple(profiles)


def _describe_parse_error(error: ProfileParseError, index: int) -> str:
    """One line naming which entry failed and why.

    ``isinstance`` rather than ``match``, following the note in
    ``src/motor/atv320.py``: a ``match`` over a fully-covered union still
    leaves an unreachable "no case matched" fall-through arc, which the
    100%-branch gate cannot close without a ``pragma`` - and a ``pragma`` is
    not an accepted waiver in the safety chain. ``isinstance`` narrows in BOTH
    directions, so :class:`Rejected` below is fully typed with no sentinel.

    The exhaustiveness that contract rule 3 is about is not given up: the
    nested-match-plus-``assert_never`` proof for every union this module owns
    lives in ``tests/test_plan.py``, where the unreachable arm costs no
    coverage, and it is the type checkers that enforce it either way.
    """
    if isinstance(error, Malformed):
        return f"profiles[{index}] is malformed ({error.detail})"
    named = ", ".join(item.value for item in error.violations)
    return f"profiles[{index}] was refused ({named})"


def parse_store(raw: str) -> Result[StoreContent, Malformed]:
    """Parse a whole store file, reporting every problem in it at once.

    Public alongside :func:`parse_profile` so a file can be checked without
    being loaded - before a deploy, or from a diagnostic script - and so the
    shape failures below can be tested for what they say rather than only
    through the recovery path, which reports success after falling back.
    """
    document = _load_json_object(raw)
    if isinstance(document, Err):
        return document
    reader = _Reader(document.value, "store")
    version = reader.integer("version")
    if version != SCHEMA_VERSION:
        reader.note(f"unsupported version {version}, this build reads {SCHEMA_VERSION}")
    rev = reader.integer("rev")
    if rev < 0:
        reader.note(f"'rev' must not be negative, got {rev}")
    profiles = _parse_profiles(reader, _read_profile_entries(reader, document.value))
    problems = reader.problems
    if problems:
        return Err(Malformed(detail="; ".join(problems), problems=problems))
    return Ok(StoreContent(rev=StoreRev(rev), profiles=profiles))


def _store_document(rev: StoreRev, profiles: Sequence[TrainingProfile]) -> Mapping[str, JsonValue]:
    """The whole file, ready for ``json.dumps``."""
    return {
        "version": SCHEMA_VERSION,
        "rev": int(rev),
        "profiles": [profile_to_document(profile) for profile in profiles],
    }


def _quarantine_path(path: Path, raw: str | None) -> Path:
    """Where a corrupt store file gets moved to.

    Named after a hash of its own contents rather than a timestamp, for two
    reasons. There is no clock in this module (contract rule 4), and a
    content-addressed name is **idempotent**: a unit that reboots into the same
    corrupt file ten times leaves one quarantine file, not ten, which matters
    on an SD card with a finite number of writes and a finite amount of room.
    """
    if raw is None:
        return path.with_name(f"{path.name}.corrupt-unreadable")
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return path.with_name(f"{path.name}.corrupt-{digest}")


@final
class ProfileStore:
    """Profiles on disk: revisioned, written atomically, and never fatal to start.

    Three properties it exists for, in the order they matter.

    **A power cut must not brick startup.** Every write goes to a temporary
    file which is flushed and ``fsync``-ed and only then renamed over the real
    one, so an interrupted write leaves either the whole old file or the whole
    new one. There is no instant at which the store is half a JSON document.
    The directory itself is deliberately not ``fsync``-ed: that would only
    affect whether the *rename* survives the cut, and losing the rename leaves
    the previous complete revision, which is safe. Claiming more than that
    would be claiming more than this code does.

    **A corrupt file must not be fatal either.** If the store cannot be parsed
    it is renamed out of the way and the shipped defaults are loaded, loudly.
    Refusing to start would leave an operator with a machine that will not run
    and no profiles to fix it with; starting on the conservative shipped limits
    is strictly better, as long as nobody can miss that it happened - hence
    :class:`LoadReport` and the ``error``-level log line.

    **Two editors must not silently overwrite each other.** Every mutation
    states the revision it was based on and fails with :class:`RevMismatch` if
    the store has moved on. The values being edited are heart-rate limits and a
    speed ceiling, so last-write-wins is not an acceptable default.

    The in-memory profiles and revision are **mutable state**, which is the
    exception in this codebase and is confined to this object. The invariant
    that makes it safe: they are replaced only *after* a write has been
    confirmed, so memory never claims a revision the disk does not have.
    """

    __slots__ = ("_defaults_path", "_path", "_profiles", "_rev")

    def __init__(self, path: Path, defaults_path: Path = SHIPPED_DEFAULTS_PATH) -> None:
        """Inert: no file is touched until :meth:`load`.

        A constructor that reads a file cannot be constructed in a test without
        a file, and cannot report what it found. ``load`` is separate so that
        both are possible.
        """
        self._path: Path = path
        self._defaults_path: Path = defaults_path
        # Mutable cache; see the class docstring. Empty until load().
        self._profiles: dict[str, TrainingProfile] = {}
        self._rev: StoreRev = INITIAL_REV

    # --- reading ---------------------------------------------------------

    @property
    def rev(self) -> StoreRev:
        """The revision currently in memory, which is the one on disk."""
        return self._rev

    def list_profiles(self) -> tuple[TrainingProfile, ...]:
        """Every profile, ordered by id.

        Sorted rather than in file order so the operator's list does not
        reshuffle itself when somebody saves an unrelated profile.
        """
        return tuple(self._profiles[key] for key in sorted(self._profiles))

    def get(self, profile_id: str) -> Result[TrainingProfile, UnknownProfile]:
        """One profile by id."""
        profile = self._profiles.get(profile_id)
        if profile is None:
            return Err(UnknownProfile(profile_id=profile_id, known=self._ids()))
        return Ok(profile)

    def load(self) -> Result[LoadReport, DefaultsUnusable]:
        """Read the store, recovering from a corrupt or missing file.

        The only failure is :class:`DefaultsUnusable`, which means the
        installation is broken rather than the data - there is nothing left to
        fall back to, so the caller should refuse to start.
        """
        raw = _read_text(self._path)
        if isinstance(raw, Err):
            return self._recover_from_read_failure(raw.error)
        parsed = parse_store(raw.value)
        if isinstance(parsed, Err):
            return self._recover_from_corruption(reason=parsed.error.detail, raw=raw.value)
        content = parsed.value
        self._adopt(content)
        detail = f"loaded {len(content.profiles)} profile(s) at rev {content.rev} from {self._path}"
        _LOG.info("%s", detail)
        return Ok(
            LoadReport(
                source=LoadSource.STORE_FILE,
                rev=content.rev,
                profile_ids=self._ids(),
                quarantined=None,
                detail=detail,
            )
        )

    # --- writing ---------------------------------------------------------

    def upsert(
        self, profile: TrainingProfile, *, expected_rev: StoreRev
    ) -> Result[StoreRev, UpsertError]:
        """Add or replace one profile, bumping the revision.

        Takes a :class:`TrainingProfile` rather than a document, so there is no
        way to save an invalid profile: validation happened where the untrusted
        JSON was parsed. The revision advances only after the file has been
        replaced - see :meth:`_commit`.
        """
        if expected_rev != self._rev:
            return Err(RevMismatch(expected=expected_rev, actual=self._rev))
        updated = dict(self._profiles)
        updated[profile.profile_id] = profile
        return self._commit(updated)

    def delete(self, profile_id: str, *, expected_rev: StoreRev) -> Result[StoreRev, DeleteError]:
        """Remove one profile, bumping the revision.

        Deleting the last profile is allowed and leaves an empty store, which
        is a deliberate state and not a corrupt one: an empty ``profiles``
        array parses cleanly and is loaded as-is. Falling back to the defaults
        here instead would make the last profile undeletable and would look,
        to whoever tried, like the delete had silently failed.
        """
        if expected_rev != self._rev:
            return Err(RevMismatch(expected=expected_rev, actual=self._rev))
        if profile_id not in self._profiles:
            return Err(UnknownProfile(profile_id=profile_id, known=self._ids()))
        updated = dict(self._profiles)
        del updated[profile_id]
        return self._commit(updated)

    # --- resolving -------------------------------------------------------

    def resolve(
        self,
        profile_id: str,
        *,
        at: UnixMillis,
        total_duration_s: Seconds | None = None,
        subject_hr_max: Bpm | None = None,
    ) -> Result[Program, ResolveError]:
        """Freeze a profile into the :class:`Program` a session will run.

        ``at`` is a parameter rather than a clock read (contract rule 4); the
        caller passes ``clock.unix_millis()``.

        ``total_duration_s`` is the one supported override, and HOLD absorbs
        it - see :meth:`TrainingProfile.with_total_duration`. Passing the value
        the profile already has still counts as an override, because the
        operator typed it: ``total_overridden`` records what was *asked for*,
        which is what somebody reviewing the session wants to know.

        ``subject_hr_max`` is the maximum heart rate of the person actually
        about to ride, when it is known (a launch from the dashboard always
        carries it). The profile is **rebuilt** with it, so every cardiac check
        in :class:`TrainingProfile` is re-run against this rider rather than
        against whoever the preset was written for: a zone that is fine for a
        thirty-year-old and above maximum for this person is refused here, as
        :class:`Rejected`, before anything turns.
        """
        found = self.get(profile_id)
        if isinstance(found, Err):
            return found
        profile = found.value
        if subject_hr_max is not None and subject_hr_max != profile.subject_hr_max:
            base = profile
            fitted = _attempt(lambda: replace(base, subject_hr_max=subject_hr_max))
            if isinstance(fitted, Err):
                return fitted
            profile = fitted.value
        if total_duration_s is None:
            return Ok(
                Program(
                    profile=profile,
                    source_rev=self._rev,
                    resolved_at=at,
                    total_overridden=False,
                )
            )
        adjusted = profile.with_total_duration(total_duration_s)
        if isinstance(adjusted, Err):
            return adjusted
        return Ok(
            Program(
                profile=adjusted.value,
                source_rev=self._rev,
                resolved_at=at,
                total_overridden=True,
            )
        )

    # --- internals -------------------------------------------------------

    def _ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._profiles))

    def _adopt(self, content: StoreContent) -> None:
        """Replace the cache. Called only with content that has been parsed."""
        self._profiles = {profile.profile_id: profile for profile in content.profiles}
        self._rev = content.rev

    def _commit(self, updated: Mapping[str, TrainingProfile]) -> Result[StoreRev, StoreUnwritable]:
        """Write a new revision, and adopt it **only** if the write landed.

        The ordering is the invariant: if the file could not be replaced, the
        in-memory revision does not move. Bumping it first would leave a store
        whose ``rev`` says the disk holds an edit it does not, so the next
        caller's optimistic-concurrency check would pass against a revision
        that never existed.
        """
        candidate = StoreRev(self._rev + 1)
        ordered = tuple(updated[key] for key in sorted(updated))
        written = self._write_atomically(_store_document(candidate, ordered))
        if isinstance(written, Err):
            return written
        self._profiles = dict(updated)
        self._rev = candidate
        return Ok(candidate)

    def _write_atomically(self, document: Mapping[str, JsonValue]) -> Result[None, StoreUnwritable]:
        """Write via a temporary file and one rename. See the class docstring.

        ``sort_keys`` and an explicit ``newline`` make the bytes deterministic,
        so an unchanged store is an unchanged file: no spurious diffs, and no
        write at all when a deploy re-serialises the same content.
        """
        text = json.dumps(document, indent=2, sort_keys=True) + "\n"
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                # The bytes must be on the medium BEFORE the rename makes them
                # the live file; otherwise a power cut can publish an empty or
                # partial file under the real name.
                os.fsync(handle.fileno())
            tmp.replace(self._path)
        except OSError as failure:
            # Leave nothing behind: a stale .tmp is never read (the loader only
            # ever opens the real name) but it would confuse the next person to
            # look at the directory after an incident.
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            return Err(
                StoreUnwritable(
                    path=str(self._path),
                    detail=f"{type(failure).__name__}: {failure}",
                )
            )
        return Ok(None)

    def _recover_from_read_failure(
        self, failure: _ReadFailure
    ) -> Result[LoadReport, DefaultsUnusable]:
        """First boot, or a present-but-unreadable file.

        The distinction is the whole reason :func:`_read_text` returns a union:
        an absent store is a first boot and must not raise an alarm, while a
        store that is present and unreadable is a store that cannot be trusted
        and goes down the corruption path. ``isinstance`` rather than ``match``
        for the coverage reason given on :func:`_describe_parse_error`.
        """
        if isinstance(failure, _Absent):
            return self._adopt_defaults(
                source=LoadSource.DEFAULTS_NO_STORE,
                quarantined=None,
                detail=(
                    f"no store at {self._path}; loaded the shipped defaults. "
                    "The store file is written by the first save."
                ),
                loud=False,
            )
        return self._recover_from_corruption(reason=failure.detail, raw=None)

    def _recover_from_corruption(
        self, *, reason: str, raw: str | None
    ) -> Result[LoadReport, DefaultsUnusable]:
        """Move the bad file aside and fall back to the shipped defaults.

        The rename is attempted and is allowed to fail: if it does, the
        defaults are still loaded and the failure is reported. Refusing to
        start because a *rename* failed would be letting a tidying step decide
        whether the machine works.
        """
        quarantined = self._quarantine(raw)
        where = "" if quarantined is None else f" moved to {quarantined.name};"
        return self._adopt_defaults(
            source=LoadSource.DEFAULTS_AFTER_CORRUPTION,
            quarantined=quarantined,
            detail=(
                f"the profile store at {self._path} is unusable ({reason});"
                f"{where} running on the shipped defaults. "
                "Operator profiles are NOT loaded - check the limits before starting a session."
            ),
            loud=True,
        )

    def _quarantine(self, raw: str | None) -> Path | None:
        """Rename the unusable store out of the way, or report that we could not."""
        target = _quarantine_path(self._path, raw)
        try:
            self._path.replace(target)
        except OSError as failure:
            _LOG.error(
                "could not move the unusable profile store %s aside: %s: %s",
                self._path,
                type(failure).__name__,
                failure,
            )
            return None
        return target

    def _adopt_defaults(
        self,
        *,
        source: LoadSource,
        quarantined: Path | None,
        detail: str,
        loud: bool,
    ) -> Result[LoadReport, DefaultsUnusable]:
        """Load the shipped defaults, or give up.

        This is the end of the line: if the defaults are unusable there is
        nothing else to fall back to, and the honest answer is to refuse to
        start rather than to run a session with no limits at all.
        """
        raw = _read_text(self._defaults_path)
        if isinstance(raw, Err):
            return Err(
                DefaultsUnusable(
                    path=str(self._defaults_path),
                    detail=_describe_read_failure(raw.error),
                )
            )
        parsed = parse_store(raw.value)
        if isinstance(parsed, Err):
            return Err(DefaultsUnusable(path=str(self._defaults_path), detail=parsed.error.detail))
        self._adopt(parsed.value)
        if loud:
            _LOG.error("%s", detail)
        else:
            _LOG.info("%s", detail)
        return Ok(
            LoadReport(
                source=source,
                rev=self._rev,
                profile_ids=self._ids(),
                quarantined=quarantined,
                detail=detail,
            )
        )


def _describe_read_failure(failure: _ReadFailure) -> str:
    """One line for a failed read. ``isinstance``; see :func:`_describe_parse_error`."""
    if isinstance(failure, _Absent):
        return "the file is not there"
    return failure.detail
