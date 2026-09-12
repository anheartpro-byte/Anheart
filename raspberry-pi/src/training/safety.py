"""The safety supervisor: independent rules, latched verdicts, absolute precedence.

This module decides when the machine must hold, slow down, end the session, or
stop being written to at all. It is a **separate concern from the control law
and it wins**: the runtime applies the verdict returned here, and uses the
controller's demand only when there is no verdict.

WHY THAT SEPARATION IS STRUCTURAL AND NOT A STYLE CHOICE
--------------------------------------------------------
The failure mode this machine actually has is a **vasovagal episode**: a person
under centripetal load beginning to faint. Their heart rate *falls*. A falling
heart rate reads to the control law as "below the target zone", and the control
law's correct answer to "below zone" is to **speed up**. The two layers
therefore demand opposite things from the same evidence, and the control law is
not wrong to compute that - it is wrong to be obeyed.

Mixing them would bury that inversion inside an ``if`` in the controller, one
refactor away from disappearing. So the separation is encoded in the types
rather than written in a comment:

* **The supervisor cannot see the controller's demand.**
  :class:`SafetyObservation` carries no ``ControlDecision`` and no
  ``desired_rpm``. It carries ``commanded_rpm``, which is a *measurement* of
  what was last written to the drive. There is no field through which the
  control law's opinion could reach a rule, so no rule can weigh it.
* **The supervisor never touches the wire.** No drive, no Modbus, no socket, no
  ``await``, nothing to mock. A verdict is a pure function of the evidence in
  one :class:`SafetyObservation` plus this object's own latched history, which
  is what lets every dwell in a 45-minute session be tested exactly, on a
  :class:`~src.clock.ManualClock`, in milliseconds.
* ``hr_drop`` is the rule that catches the inversion, and it is the reason this
  file exists.

THE TWO LEVELS OF PERSISTENCE - read this before adding a rule
--------------------------------------------------------------
Every rule fires a :class:`~src.training.types.SafetyVerdict`, and those
verdicts are combined with ``max`` over
:class:`~src.training.types.SafetyAction` - the member order of that enum *is*
the precedence policy. What differs between rules is how long a verdict
outlives its evidence:

* **latched** (``verdict.latched is True``): the verdict is pushed into this
  supervisor's *floor*, a high-water mark that **never decreases** until
  :meth:`SafetySupervisor.acknowledge` is called by a human. Every rule at or
  above ``RAMP_DOWN`` latches, as do ``comms_lost``, ``operator_estop``,
  ``loop_stall`` and every trip arriving from a thread. There is no automatic
  fault reset and no automatic resumption of motion anywhere in this system, so
  a latched verdict has taken the machine out of service.
* **unlatched**: an advisory (``FREEZE``/``REDUCE``) that is re-evaluated every
  tick and disappears when its evidence does - a heart rate that comes back, a
  current that settles, an attendant who returns.

Both levels exist on purpose, and the reason is a real failure mode rather than
convenience. If a ten-second electrode dropout required an operator click, the
operator would learn to click reflexively, and would then click through the
drive fault as well. Alarm fatigue is how a latch that matters gets ignored. So
an advisory clears itself and a decision to stop does not.

What that buys, and what the tests prove: :meth:`SafetySupervisor.floor` is
monotonically non-decreasing for a whole session unless a human acknowledges,
:meth:`SafetySupervisor.standing` is always at least as severe as every rule
currently firing, and the only code path that can lower either is
:meth:`SafetySupervisor.acknowledge` - which refuses outright once the standing
verdict is ``GO_SILENT``.

WHAT THIS MODULE PROMISES ABOUT STOPPING - and what it must not
--------------------------------------------------------------
Nothing here removes torque. The drive's STO input is **jumpered**: there is no
independent means of removing torque on this machine, so "the software
commanded a stop" is the only stop there is. The DC bus absorbs ~11 J of the
~420 J stored in the spinning rig, so a stop commanded faster than the
commissioned 3-4 s ramp trips overvoltage and drops the drive into freewheel -
a longer, uncontrolled coast-down with a person inside. ``QUICK_STOP``
therefore means "zero the speed reference now and leave the run command in
place", not "immediate", and :meth:`SafetySupervisor.require_estop_confirmed`
exists because a jumpered STO must be a visible blocking defect rather than a
line in a commissioning file.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import itertools
import logging
from collections import deque
from dataclasses import dataclass
from statistics import fmean
from typing import Final, final

from src.clock import Clock
from src.motor.drive import DriveState, FaultReport
from src.result import Err, Ok, Result
from src.training.types import (
    HeartRateSample,
    Phase,
    SafetyAction,
    SafetyVerdict,
    most_severe,
)
from src.units import (
    Amperes,
    Bpm,
    BpmPerMinute,
    Monotonic,
    MotorRpm,
    Seconds,
    UnixMillis,
    elapsed,
)

_logger: Final[logging.Logger] = logging.getLogger(__name__)


# =========================================================================
# Rule ids
# =========================================================================
#
# Rule ids are stored data: a dashboard, an alert and the session log all key
# on these strings, so renaming one is a breaking change and not a tidy-up (see
# src.training.types.SafetyVerdict). They are constants rather than literals at
# the fire site so a typo is a NameError here instead of an unknown key in
# somebody's database, and ALL_RULES is what the tests enumerate.

RULE_OPERATOR_ESTOP: Final[str] = "operator_estop"
RULE_DRIVE_FAULT: Final[str] = "drive_fault"
RULE_COMMS_LOST: Final[str] = "comms_lost"
RULE_HR_DROP: Final[str] = "hr_drop"
RULE_HR_HARD_MAX: Final[str] = "hr_hard_max"
RULE_HR_CRITICAL: Final[str] = "hr_critical"
RULE_HR_RATE: Final[str] = "hr_rate"
RULE_HR_STALE: Final[str] = "hr_stale"
RULE_HR_UNRESPONSIVE: Final[str] = "hr_unresponsive"
RULE_CURRENT_HIGH: Final[str] = "current_high"
RULE_NO_LOAD: Final[str] = "no_load"
RULE_TRACKING_ERROR: Final[str] = "tracking_error"
RULE_REVERSE_ROTATION: Final[str] = "reverse_rotation"
RULE_SESSION_OVERRUN: Final[str] = "session_overrun"
RULE_LOOP_STALL: Final[str] = "loop_stall"
RULE_ATTENDANT_ABSENT: Final[str] = "attendant_absent"

ALL_RULES: Final[tuple[str, ...]] = (
    RULE_OPERATOR_ESTOP,
    RULE_DRIVE_FAULT,
    RULE_COMMS_LOST,
    RULE_HR_DROP,
    RULE_HR_HARD_MAX,
    RULE_HR_CRITICAL,
    RULE_HR_RATE,
    RULE_HR_STALE,
    RULE_HR_UNRESPONSIVE,
    RULE_CURRENT_HIGH,
    RULE_NO_LOAD,
    RULE_TRACKING_ERROR,
    RULE_REVERSE_ROTATION,
    RULE_SESSION_OVERRUN,
    RULE_LOOP_STALL,
    RULE_ATTENDANT_ABSENT,
)
"""Every rule this supervisor can fire. One dwell tracker is allocated per entry."""


# =========================================================================
# Module constants
# =========================================================================

NO_DWELL: Final[Seconds] = Seconds(0.0)
"""For a rule that fires on the first tick its condition holds."""

SECONDS_PER_MINUTE: Final[float] = 60.0

MIN_TREND_POINTS: Final[int] = 2
"""The fewest points from which a fall or a slope can be computed at all."""

HISTORY_LIMIT: Final[int] = 2048
"""Hard cap on retained heart-rate evidence.

The history is pruned by AGE (see :meth:`SafetySupervisor._ingest`); this bound
covers the case age-pruning cannot - a clock that is not advancing while
sequence numbers keep arriving - so a stuck caller cannot turn a memory leak
into the failure mode. At 1 Hz of genuinely new evidence it holds half an hour.
"""

WINDOW_HALVES: Final[int] = 2
"""``hr_unresponsive`` compares the early half of its window against the late half."""

THREAD_TRIP_LIMIT: Final[int] = 64
"""Bound on trips queued from other threads between two ticks."""

THREAD_TRIP_DETAIL: Final[str] = (
    "raised from a thread outside the control loop, with no detail given"
)
"""Stand-in prose for a trip whose caller supplied none.

A verdict with an empty ``detail`` would reach the operator screen as a blank
line, which reads as a display bug rather than as a safety demand.
"""

ESTOP_ATTESTATION: Final[str] = (
    "a latching mushroom emergency stop is wired normally-closed into P24 -> STO "
    "and the STO jumper has been removed"
)
"""The exact statement an operator attests to, per boot, before any motion.

Stored verbatim in :class:`EstopAttestation` and in the log line, because an
attestation whose wording nobody recorded is not evidence of anything.
"""


# =========================================================================
# Limits
# =========================================================================


def _require_positive(name: str, value: float, why: str) -> None:
    """Reject a threshold that is zero or negative, at construction time."""
    if value <= 0.0:
        raise ValueError(f"{name} must be positive, got {value}: {why}")


def _require_non_negative(name: str, value: float, why: str) -> None:
    """Reject a negative duration. Zero is legitimate: it means no dwell."""
    if value < 0.0:
        raise ValueError(f"{name} cannot be negative, got {value}: {why}")


def _require_increasing(why: str, *named: tuple[str, float]) -> None:
    """Each value must be strictly greater than the one before it."""
    for (lower_name, lower), (upper_name, upper) in itertools.pairwise(named):
        if upper <= lower:
            raise ValueError(
                f"{upper_name} ({upper}) must be greater than {lower_name} ({lower}): {why}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class SafetyLimits:
    """Every threshold the rules compare against, in one frozen, validated object.

    Keyword-only and frozen: a positional constructor for thirty numbers in
    five different units is a transposition waiting to happen, and a limit set
    that could be edited after a session started would make the session log
    stop being evidence of what the machine was actually allowed to do.

    **The two person-specific limits have no defaults.** ``hard_max_bpm`` and
    ``critical_bpm`` come from the medical screening of the individual who is
    about to sit in the machine - age, fitness, medication, condition - and a
    default value is how a wrong one gets used without anybody choosing it. The
    same argument that keeps geometry out of
    :meth:`~src.training.types.SpeedView.from_motor_rpm` applies here, with a
    person attached.

    Everything else is defaulted, and every default is a property of **this
    machine** rather than of a person: the 8 s median refreshed at 1 Hz that
    the ECG pipeline emits, the 5 Hz control loop, the 2.15 A motor nameplate,
    the i = 49.79 gearbox. Each is quoted next to its field so a later reader
    can tell a derived value from a guess.

    ``__post_init__`` raises ``ValueError`` on an incoherent set rather than
    returning a ``Result``. That is the boundary
    ``src.motor.drive.RegisterMap`` already draws: ``Result`` is mandatory
    where a dropped error leaves a motor commanded, but this object is built at
    startup with nothing spinning, and refusing to start is the right answer to
    limits nobody can vouch for.
    """

    # --- Person-specific. No defaults, on purpose. -----------------------
    hard_max_bpm: Bpm
    """Above this for :attr:`hr_hard_max_dwell`, the session ends (RAMP_DOWN)."""

    critical_bpm: Bpm
    """At or above this, the speed reference is zeroed at once (QUICK_STOP)."""

    # --- Heart-rate rule shaping -----------------------------------------
    hr_release_bpm: Bpm = Bpm(5)
    """Hysteresis band below :attr:`hard_max_bpm`.

    Without a band, a heart rate oscillating across the limit restarts the
    five-second dwell on every dip and the rule never fires at all - the
    dangerous direction. With it, the dwell accumulates. A test pins that.
    """

    hr_hard_max_dwell: Seconds = Seconds(5.0)
    """How long above the hard maximum before ending the session.

    Five seconds of a signal that is already an 8 s median refreshed at 1 Hz,
    so this is several fresh refreshes agreeing rather than one spike.
    """

    hr_drop_bpm: Bpm = Bpm(25)
    """A fall of this many bpm inside :attr:`hr_drop_window`: the vasovagal rule.

    A magnitude of a *difference*, not a rate. ``Bpm`` is the closest available
    type; the right one is a ``BpmDelta`` ``NewType`` in ``src/units.py``,
    which this module does not own.
    """

    hr_drop_window: Seconds = Seconds(30.0)
    """Window the fall is measured over. A presyncopal fall is seconds, not minutes."""

    hr_rise_limit: BpmPerMinute = BpmPerMinute(25.0)
    """Above this rate of rise, back the speed off (REDUCE)."""

    hr_rise_release: BpmPerMinute = BpmPerMinute(15.0)
    """Hysteresis: the rise must fall back below this before the rule clears."""

    hr_rate_window: Seconds = Seconds(60.0)
    """Window the rate of rise is measured over."""

    hr_rate_min_span: Seconds = Seconds(20.0)
    """Minimum spread of evidence before a slope is believed.

    Dividing a bpm difference by a two-second span turns one noisy median into
    an alarming rate. Twenty seconds is two and a half of the pipeline's own
    8 s windows.
    """

    hr_stale_freeze_after: Seconds = Seconds(10.0)
    """No fresh trustworthy heart rate for this long: hold the speed (FREEZE)."""

    hr_stale_reduce_after: Seconds = Seconds(30.0)
    """Still none: back the speed off (REDUCE)."""

    hr_stale_ramp_after: Seconds = Seconds(60.0)
    """Still none: end the session (RAMP_DOWN).

    Ten seconds is ten missed refreshes of a 1 Hz signal, so it is a pipeline
    that has stopped rather than jitter. A minute with no heart rate is a
    session that cannot be supervised at all, whatever the machine is doing.
    """

    unresponsive_window: Seconds = Seconds(300.0)
    """Window for "the heart rate does not follow the speed".

    Five minutes because heart rate lags load with a time constant of 30-60 s;
    any shorter window would report normal physiology as a fault.
    """

    unresponsive_min_span: Seconds = Seconds(240.0)
    """Minimum spread of retained evidence before the rule will conclude anything.

    Smaller than :attr:`unresponsive_window` and necessarily so: evidence is
    pruned *at* the window, so the oldest retained sample is always slightly
    younger than the window and a span equal to it could never be reached. Four
    minutes against a five-minute window is still four heart-rate time
    constants.
    """

    unresponsive_rpm_rise: MotorRpm = MotorRpm(150)
    """Rise in MEAN commanded motor rpm between the halves of the window.

    Means rather than endpoints: a single spike in the setpoint moves a mean by
    only its own share of it, while a sustained increase moves it fully. The
    endpoint-to-endpoint comparison this replaced could be satisfied by one
    stray sample, and its mirror - comparing the minimum of the later half
    against the maximum of the earlier one - was blind to a smooth ramp, which
    is exactly how the setpoint actually moves.
    """

    unresponsive_bpm_rise: Bpm = Bpm(3)
    """If the mean heart rate rose by less than this across the halves, it fires.

    A magnitude of a difference, like :attr:`hr_drop_bpm`. Three bpm is inside
    the noise of an 8 s median, which is the point: this is the threshold for
    "did not move at all".
    """

    unresponsive_min_points: int = 10
    """Fewest samples required in each half of the window. A count, not a quantity."""

    # --- Drive and electrical --------------------------------------------
    comms_lost_failures: int = 3
    """Consecutive failed drive exchanges before GO_SILENT. A count, not a quantity."""

    current_warn_a: Amperes = Amperes(2.4)
    """Above this for :attr:`current_warn_dwell`, reduce. Nameplate is 2.15 A."""

    current_trip_a: Amperes = Amperes(3.2)
    """Above this, end the session at once: roughly 1.5x nameplate."""

    current_release_a: Amperes = Amperes(0.2)
    """Hysteresis band below :attr:`current_warn_a`."""

    current_warn_dwell: Seconds = Seconds(10.0)
    """Ten seconds tolerates an acceleration transient but not a rub."""

    no_load_floor_a: Amperes = Amperes(0.2)
    """Below this while the machine should be turning: open phase, or no motor."""

    no_load_min_rpm: MotorRpm = MotorRpm(100)
    """Commanded motor rpm above which current must be measurable."""

    no_load_dwell: Seconds = Seconds(3.0)
    """Long enough for the drive to build current after a step, no longer."""

    tracking_error_rpm: MotorRpm = MotorRpm(60)
    """Commanded-minus-measured motor rpm that counts as divergence.

    Motor shaft, so 60 rpm is a little over one output rpm through i = 49.79.
    """

    tracking_error_dwell: Seconds = Seconds(2.0)
    """Only applied while the runtime says it is NOT ramping."""

    reverse_rpm: MotorRpm = MotorRpm(10)
    """Measured motor rpm against the commanded direction that counts as reverse.

    A band rather than zero because RFRD dithers about zero at standstill, and
    a sign flip in that noise must not fire a QUICK_STOP.
    """

    # --- Session, loop and attendant -------------------------------------
    overrun_grace: Seconds = Seconds(30.0)
    """Allowance past the programme's total duration before ending the session."""

    control_period: Seconds = Seconds(0.2)
    """The control loop's nominal period: 5 Hz."""

    loop_stall_freeze_periods: float = 3.0
    """Missed periods before FREEZE. A dimensionless multiple of :attr:`control_period`."""

    loop_stall_silent_periods: float = 15.0
    """Missed periods before GO_SILENT. Dimensionless, as above."""

    attendant_freeze_after: Seconds = Seconds(60.0)
    """No presence ping for this long: hold the speed."""

    attendant_ramp_after: Seconds = Seconds(120.0)
    """Still no presence ping: end the session. This rig never runs unattended."""

    @property
    def hr_hard_max_release_bpm(self) -> Bpm:
        """The hysteresis release level for :attr:`hard_max_bpm`."""
        return Bpm(self.hard_max_bpm - self.hr_release_bpm)

    @property
    def current_warn_release_a(self) -> Amperes:
        """The hysteresis release level for :attr:`current_warn_a`."""
        return Amperes(self.current_warn_a - self.current_release_a)

    @property
    def loop_stall_freeze_gap(self) -> Seconds:
        """Inter-tick gap that means the control loop stalled."""
        return Seconds(self.control_period * self.loop_stall_freeze_periods)

    @property
    def loop_stall_silent_gap(self) -> Seconds:
        """Inter-tick gap after which this process stops being trusted to write."""
        return Seconds(self.control_period * self.loop_stall_silent_periods)

    def __post_init__(self) -> None:
        """Refuse an incoherent limit set. Startup only, with nothing spinning."""
        _require_increasing(
            "a critical rate below the hard maximum would mean the QUICK_STOP rule "
            "can never fire before the RAMP_DOWN one",
            ("zero", 0.0),
            ("hard_max_bpm", self.hard_max_bpm),
            ("critical_bpm", self.critical_bpm),
        )
        _require_increasing(
            "a hysteresis band wider than the limit it releases from would never release",
            ("zero", 0.0),
            ("hr_release_bpm", self.hr_release_bpm),
            ("hard_max_bpm", self.hard_max_bpm),
        )
        _require_increasing(
            "the rule must release below the level it raises at, or it cannot clear",
            ("zero", 0.0),
            ("hr_rise_release", self.hr_rise_release),
            ("hr_rise_limit", self.hr_rise_limit),
        )
        _require_increasing(
            "a slope needs a minimum span of evidence smaller than its own window",
            ("zero", 0.0),
            ("hr_rate_min_span", self.hr_rate_min_span),
            ("hr_rate_window", self.hr_rate_window),
        )
        _require_increasing(
            "the staleness escalation must go FREEZE, then REDUCE, then RAMP_DOWN",
            ("zero", 0.0),
            ("hr_stale_freeze_after", self.hr_stale_freeze_after),
            ("hr_stale_reduce_after", self.hr_stale_reduce_after),
            ("hr_stale_ramp_after", self.hr_stale_ramp_after),
        )
        _require_increasing(
            "a current floor at or above the warning level would fire both rules at once",
            ("zero", 0.0),
            ("no_load_floor_a", self.no_load_floor_a),
            ("current_warn_a", self.current_warn_a),
            ("current_trip_a", self.current_trip_a),
        )
        _require_increasing(
            "a release band wider than the warning level would never release",
            ("zero", 0.0),
            ("current_release_a", self.current_release_a),
            ("current_warn_a", self.current_warn_a),
        )
        _require_increasing(
            "a stall is more than one missed period, and FREEZE must precede GO_SILENT",
            ("one period", 1.0),
            ("loop_stall_freeze_periods", self.loop_stall_freeze_periods),
            ("loop_stall_silent_periods", self.loop_stall_silent_periods),
        )
        _require_increasing(
            "the attendant escalation must hold the speed before it ends the session",
            ("zero", 0.0),
            ("attendant_freeze_after", self.attendant_freeze_after),
            ("attendant_ramp_after", self.attendant_ramp_after),
        )
        _require_increasing(
            "a trend needs at least two points in each half of its window",
            ("one", 1.0),
            ("unresponsive_min_points", self.unresponsive_min_points),
        )
        _require_increasing(
            "evidence is pruned at the window, so a span equal to it is unreachable",
            ("zero", 0.0),
            ("unresponsive_min_span", self.unresponsive_min_span),
            ("unresponsive_window", self.unresponsive_window),
        )
        _require_increasing(
            "at least one failed exchange is needed before the link is declared lost",
            ("zero", 0.0),
            ("comms_lost_failures", self.comms_lost_failures),
        )
        for name, value in (
            ("hr_drop_bpm", self.hr_drop_bpm),
            ("hr_drop_window", self.hr_drop_window),
            ("unresponsive_window", self.unresponsive_window),
            ("unresponsive_rpm_rise", self.unresponsive_rpm_rise),
            ("unresponsive_bpm_rise", self.unresponsive_bpm_rise),
            ("no_load_min_rpm", self.no_load_min_rpm),
            ("tracking_error_rpm", self.tracking_error_rpm),
            ("reverse_rpm", self.reverse_rpm),
            ("control_period", self.control_period),
        ):
            _require_positive(name, value, "a zero or negative threshold disables its rule")
        for name, value in (
            ("hr_hard_max_dwell", self.hr_hard_max_dwell),
            ("current_warn_dwell", self.current_warn_dwell),
            ("no_load_dwell", self.no_load_dwell),
            ("tracking_error_dwell", self.tracking_error_dwell),
            ("overrun_grace", self.overrun_grace),
        ):
            _require_non_negative(name, value, "a negative dwell is not a shorter dwell")


# =========================================================================
# What one tick shows the supervisor
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class SafetyObservation:
    """Everything the rules are allowed to judge, for one instant.

    Frozen, slotted and keyword-only. Keyword-only matters more here than
    anywhere else in this package: ``commanded_rpm`` and ``measured_rpm`` are
    the same type and adjacent in meaning, and a positional constructor would
    let them be swapped - which would make ``tracking_error`` and
    ``reverse_rotation`` judge the wrong number without any checker noticing.

    **No field carries a demand.** There is no ``desired_rpm``, no
    :class:`~src.training.types.ControlDecision`, nothing from the control law.
    ``commanded_rpm`` is what was last *written to the drive*, i.e. a
    measurement of the machine, not a request. That omission is the whole point
    of the module: see its docstring on the vasovagal inversion.

    ``measured_rpm``, ``current`` and ``fault`` are optional because a failed
    read has no value. They must be ``None`` in that case and never a
    fabricated zero - ``src.motor.drive`` is explicit that a fabricated 0 rpm
    is exactly the lie that gets somebody hurt, and the rules here treat
    ``None`` as "no evidence" rather than as "no load" or "not turning".
    """

    now: Monotonic
    """The tick's instant. Every rule in one evaluation uses this same value, so
    the verdicts of a tick cannot disagree about when they were taken."""

    phase: Phase
    """Which part of the programme this is. Only ``DONE`` suspends the heart-rate
    and attendant rules: the person is out of the machine and no setpoint will
    be issued again, so alarming forever would be noise. Every other phase -
    including ``RECOVERY``, which carries the highest vasovagal risk with the
    motor stopped - is fully supervised."""

    elapsed: Seconds
    """Time since the session started."""

    total_duration: Seconds
    """The programme's intended total length; ``session_overrun`` measures past it."""

    commanded_rpm: MotorRpm
    """Signed motor-shaft rpm last written to the drive (LFRD). A measurement."""

    ramping: bool
    """Whether the runtime is deliberately moving the setpoint right now.

    ``tracking_error`` is suppressed while this is true, because commanded and
    measured speed are *supposed* to differ during a ramp; that is what a ramp
    is. It must be the runtime's own statement, not something inferred here
    from two successive setpoints, or a stalled shaft would look like a ramp.
    """

    heart_rate: HeartRateSample | None
    """The latest sample, or ``None`` if none has arrived yet.

    Carries its own ``seq``, which is the only defence against the ECG
    pipeline's re-emitted metrics dict. See
    :meth:`SafetySupervisor._ingest` for how the two are separated.
    """

    drive_state: DriveState
    """The drive's own CiA402 state. Before the first successful read this must
    be ``COMM_LOST``, never ``NOT_READY``: "state unknown" is not "stopped"."""

    measured_rpm: MotorRpm | None
    """RFRD, signed motor-shaft rpm, or ``None`` when the last read failed.

    The only field that speaks about motion: FAULT, NOT_READY and COMM_LOST are
    all compatible with a centrifuge still turning.
    """

    current: Amperes | None
    """LCR in amperes, or ``None`` when unknown. Nameplate is 2.15 A.

    Disproportionately valuable evidence: a centrifuge beginning to rub its
    enclosure shows up here long before it shows up anywhere else.
    """

    fault: FaultReport | None
    """The decoded LFT fault, when one was read. Surfaced verbatim by ``drive_fault``."""

    consecutive_comm_failures: int
    """How many drive exchanges have failed in a row. Reset to 0 by the caller on
    any success, so this is a run length and not a total."""

    attendant_last_seen: Monotonic | None
    """When the UI last reported a human present, or ``None`` if never.

    ``None`` is not treated as "fine": the age is then measured from the start
    of the session, so a session that never had an attendant escalates exactly
    like one that lost theirs.
    """


# =========================================================================
# Out-of-band inputs and operator records
# =========================================================================


@dataclass(frozen=True, slots=True)
class ThreadTrip:
    """A demand raised from a thread that is not the control loop.

    Deliberately tiny and clock-free: :meth:`SafetySupervisor.trip_from_thread`
    is called from the BITalino acquisition thread, where the only operations
    that are provably non-blocking are constructing a small object and handing
    it to a queue. The instant is stamped when the control loop picks the trip
    up, at most one tick later, and that is the honest stamp: it is when the
    supervisor actually learned of it.
    """

    rule: str
    action: SafetyAction
    detail: str


@dataclass(frozen=True, slots=True)
class EstopAttestation:
    """An operator's per-boot statement that a real emergency stop is wired in.

    Carries both clocks on purpose. ``at`` is what ages and durations are
    computed from and survives a clock step; ``wall_clock`` is what a record
    leaving the machine needs. The Pi has no RTC, so on a freshly booted
    machine the wall clock can be wrong while ``at`` is still perfectly good -
    which is why nothing computes with the former.
    """

    operator: str
    statement: str
    at: Monotonic
    wall_clock: UnixMillis


@dataclass(frozen=True, slots=True)
class SafetyAcknowledgement:
    """The record of a human clearing latched verdicts. The only way they clear."""

    operator: str
    at: Monotonic
    wall_clock: UnixMillis
    cleared: tuple[str, ...]
    """Rule ids whose latched verdicts this acknowledgement removed."""


# --- Closed error unions -------------------------------------------------


@dataclass(frozen=True, slots=True)
class EstopUnattested:
    """No operator has attested this boot that a real emergency stop is wired in.

    Carries the statement that has to be attested, so the blocking message can
    name it exactly rather than sending somebody to read the source.
    """

    statement: str


@dataclass(frozen=True, slots=True)
class Unattributed:
    """An attestation or acknowledgement arrived with no operator named.

    Refused rather than stored: an unattributable safety record is not a safety
    record, and "who said the jumper was gone" is the question that matters
    afterwards.
    """

    detail: str


@dataclass(frozen=True, slots=True)
class GoSilentIsTerminal:
    """GO_SILENT cannot be acknowledged, by design.

    Going silent hands the stop to a timer inside the drive, on the other side
    of the serial link, precisely because this process may be the problem.
    Resuming writes would take it back. Recovery is an operator action on a
    machine that has demonstrably stopped, which means restarting this process.
    """

    rule: str


@dataclass(frozen=True, slots=True)
class EmergencyStopStillLatched:
    """The mushroom is still latched, so nothing may be cleared yet.

    A latching emergency stop stays in until somebody pulls it back out, and
    software cannot see that. So the operator states it explicitly, through
    ``estop_released=True``; refusing until then is what stops an
    acknowledgement from clearing a stop that is physically still pressed.
    """

    since: Monotonic


@dataclass(frozen=True, slots=True)
class NothingLatched:
    """There was no latched verdict to acknowledge."""


type AcknowledgeRefusal = (
    Unattributed | GoSilentIsTerminal | EmergencyStopStillLatched | NothingLatched
)
"""Every way :meth:`SafetySupervisor.acknowledge` can refuse. Closed; match it
with the nested form (``case Err(error):`` then ``match error:``) ending in
``assert_never``, so a new refusal fails the build at every call site."""


type AttestationRefusal = Unattributed
"""Every way :meth:`SafetySupervisor.confirm_estop_wiring` can refuse. Closed."""


type MotionRefusal = EstopUnattested
"""Every way the startup gate can block a session that would command motion."""


# =========================================================================
# Dwell and hysteresis
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Firing:
    """A rule is firing: when its verdict started, and how long its condition has held.

    Two different durations, and conflating them would mislead an operator.
    ``since`` is when the *verdict* first stood, which is when the dwell
    expired - it is what :meth:`~src.training.types.SafetyVerdict.age` measures
    and it must not move while the rule keeps firing. ``held`` is how long the
    *condition* has been true, which is the number worth printing on a screen.
    """

    since: Monotonic
    held: Seconds


@final
class _RuleTracker:
    """Per-rule dwell and hysteresis state.

    **This is the mutable state of this module**, and it is deliberately the
    only mutable thing here besides the supervisor's own latch and history. A
    rule needs to remember two instants that no single observation contains:
    when its condition became true (for the dwell, and for the hysteresis
    decision on the next tick) and when its verdict first stood (so a re-fired
    verdict keeps its original instant rather than looking new every tick).
    """

    __slots__ = ("_fired_since", "_held_since")

    def __init__(self) -> None:
        self._held_since: Monotonic | None = None
        self._fired_since: Monotonic | None = None

    @property
    def held(self) -> bool:
        """Whether the condition was true at the last update.

        Read by the rules to widen a threshold into a band: a rule that is
        already raised stays raised until the value crosses the *release*
        level, which is what stops a value hovering on the limit from
        restarting the dwell on every dip.
        """
        return self._held_since is not None

    def update(self, *, condition: bool, now: Monotonic, dwell: Seconds) -> _Firing | None:
        """Fold one tick of evidence in, and report whether the rule is firing.

        A false condition releases completely: both the dwell and the fired
        instant are forgotten, so a condition that comes back starts its dwell
        again. That is the correct reading of "for 5 s" - five *continuous*
        seconds - and the hysteresis band above is what keeps that honest in
        the presence of noise rather than making it unachievable.
        """
        if not condition:
            self._held_since = None
            self._fired_since = None
            return None
        if self._held_since is None:
            self._held_since = now
        held = elapsed(self._held_since, now)
        if self._fired_since is None and held >= dwell:
            self._fired_since = now
        if self._fired_since is None:
            return None
        return _Firing(since=self._fired_since, held=held)

    def release(self) -> None:
        """Forget the dwell because the rule's evidence is absent, not merely below limit.

        Distinct from a false condition only in intent: a rule whose input is
        ``None`` (no heart rate at all, no current read back) has no evidence
        to dwell on, and pretending its condition is false is the same
        arithmetic but a different sentence.
        """
        self._held_since = None
        self._fired_since = None


@dataclass(frozen=True, slots=True)
class _HrPoint:
    """One retained piece of heart-rate evidence, with the speed it was taken at.

    Only samples that passed both gates are stored - genuinely new evidence
    (``seq`` advanced) and a trustworthy grade - so the trend rules cannot be
    flattened by the pipeline re-emitting its previous metrics dict at 5 Hz.
    The commanded speed travels with the sample because ``hr_unresponsive``
    compares the two, and comparing two separately-kept histories would mean
    comparing numbers from different instants.
    """

    at: Monotonic
    bpm: Bpm
    commanded_rpm: MotorRpm


def _severity(verdict: SafetyVerdict) -> SafetyAction:
    """Sort key for picking the winning verdict.

    A named function rather than a lambda, and it exists at all because
    :class:`~src.training.types.SafetyVerdict` is deliberately not orderable:
    a dataclass comparison would break ties on ``rule``, i.e. alphabetically,
    which is a coin toss dressed as a decision.
    """
    return verdict.action


def _fault_detail(report: FaultReport | None) -> str:
    """One operator-facing sentence about a drive fault, naming the LFT code.

    The operator is standing in front of the drive, so the mnemonic it is
    showing them is worth more than our prose - and when LFT was not read, say
    that instead of inventing a fault.
    """
    if report is None:
        return (
            "the drive reports a fault in its status word; no LFT code was read, "
            "so read the mnemonic from the drive's own display"
        )
    return f"the drive is in fault: {report.message}"


# =========================================================================
# The supervisor
# =========================================================================


@final
class SafetySupervisor:
    """Independent rules, combined by severity, latched until a human clears them.

    One instance per session. It holds mutable state, which this contract
    treats as the exception rather than the default, so here is the whole of
    it and why each piece cannot be a parameter:

    * the **floor**: the most severe latched verdict so far. This is the
      no-automatic-resumption rule made concrete - nothing but
      :meth:`acknowledge` lowers it, and :class:`GoSilentIsTerminal` means
      even that cannot always.
    * the **operator e-stop** slot, written synchronously by
      :meth:`latch_estop` from outside the control loop.
    * a bounded queue of **trips from other threads** (:meth:`trip_from_thread`).
    * one **dwell tracker per rule**, because "above the limit for five
      seconds" is not a property of any single observation.
    * the **heart-rate history**, for the three rules that judge a trend.
    * the last **sequence number** and the instant of the last trustworthy
      sample, which is what makes the pipeline's re-emitted metrics dict
      visible as the absence of evidence that it is.
    * the previous tick's instant, for ``loop_stall``.

    Everything else arrives in a :class:`SafetyObservation`.

    **Threading.** Only :meth:`trip_from_thread` may be called from another
    thread. Everything else - :meth:`evaluate`, :meth:`latch_estop`,
    :meth:`acknowledge` and the read accessors - lives on the event loop, and
    since none of them awaits, none of them can interleave with another. That
    is a design decision rather than an oversight: a lock around the
    supervisor would be a lock the acquisition thread could be made to wait
    on, and a lock the control loop could deadlock against while a motor is
    commanded.

    **Injected clock.** :meth:`evaluate` uses the instant carried by the
    observation, so every rule in one tick agrees exactly and the tick is
    reproducible from its record alone. The injected clock is used only by the
    out-of-band entry points - :meth:`latch_estop`, :meth:`confirm_estop_wiring`
    and :meth:`acknowledge` - which have no tick to borrow an instant from and
    must not wait for one.
    """

    __slots__ = (
        "_attestation",
        "_clock",
        "_estop",
        "_floor",
        "_history",
        "_last_good_at",
        "_last_seq",
        "_last_tick_at",
        "_limits",
        "_live",
        "_thread_trips",
        "_tick_gap",
        "_trackers",
    )

    def __init__(self, *, clock: Clock, limits: SafetyLimits) -> None:
        self._clock: Clock = clock
        self._limits: SafetyLimits = limits
        self._attestation: EstopAttestation | None = None
        self._estop: SafetyVerdict | None = None
        self._floor: SafetyVerdict | None = None
        self._live: tuple[SafetyVerdict, ...] = ()
        self._thread_trips: deque[ThreadTrip] = deque(maxlen=THREAD_TRIP_LIMIT)
        self._history: deque[_HrPoint] = deque(maxlen=HISTORY_LIMIT)
        self._trackers: dict[str, _RuleTracker] = {rule: _RuleTracker() for rule in ALL_RULES}
        self._last_tick_at: Monotonic | None = None
        self._tick_gap: Seconds | None = None
        self._last_seq: int | None = None
        self._last_good_at: Monotonic | None = None

    # =====================================================================
    # The startup gate
    # =====================================================================

    def confirm_estop_wiring(self, operator: str) -> Result[EstopAttestation, AttestationRefusal]:
        """Record an operator's attestation that a real emergency stop is wired in.

        Per boot, not per install: the attestation lives in this process and
        dies with it, so a Pi that has rebooted is a Pi whose wiring nobody has
        vouched for since. Re-attesting is allowed and replaces the record -
        operators hand over.

        Logged with both clocks, because the whole point of the gate is to
        leave a name and a time against the claim that the jumper is gone.
        """
        if not operator.strip():
            return Err(
                Unattributed(
                    "an attestation must name the operator making it: "
                    "an unattributable safety record is not a safety record"
                )
            )
        attestation = EstopAttestation(
            operator=operator,
            statement=ESTOP_ATTESTATION,
            at=self._clock.monotonic(),
            wall_clock=self._clock.unix_millis(),
        )
        self._attestation = attestation
        _logger.info(
            "emergency-stop wiring attested by %r at unix_millis=%d (monotonic=%.3f): %s",
            operator,
            attestation.wall_clock,
            attestation.at,
            attestation.statement,
        )
        return Ok(attestation)

    def require_estop_confirmed(self) -> Result[EstopAttestation, MotionRefusal]:
        """The startup gate: no session may command motion without an attestation.

        While the drive's STO input is jumpered there is no independent way to
        remove torque, so the only emergency stop that exists on this machine
        is a latching mushroom wired normally-closed into P24 -> STO with the
        jumper gone. Software cannot see whether that is true, so it blocks
        until a human says so, by name, every boot.

        Deliberately a blocking defect rather than a warning: a warning about a
        jumper is read once and then lives in a commissioning file forever, and
        a refusal to start gets fixed.

        Note what this gate is not. A button in a browser is a **convenience**
        stop - it depends on a network, a web server, an event loop and this
        process, any of which can be the thing that failed - and it is never
        safety-rated. :meth:`latch_estop` serves that button. This gate is
        about the one stop that does not depend on software at all.

        Pure and silent, so a UI may poll it.
        """
        attestation = self._attestation
        if attestation is None:
            return Err(EstopUnattested(ESTOP_ATTESTATION))
        return Ok(attestation)

    # =====================================================================
    # Entry points that do not wait for a tick
    # =====================================================================

    def latch_estop(self, reason: str) -> SafetyVerdict:
        """Latch the operator emergency stop. Synchronous, non-blocking, no I/O.

        Callable from a FastAPI request handler, an ``except`` branch, an OS
        signal handler or ``atexit``: it takes no lock, awaits nothing, and
        does not wait for the control loop to reach its next tick. The verdict
        is built complete and then published in a single attribute write, so no
        reader can observe a half-built latch, and :attr:`standing` reports
        ``QUICK_STOP`` on the very next read, from any code path, with no tick
        in between. That is the whole requirement: a stop that waits for
        whatever the main loop is currently awaiting is not a stop.

        The write happens BEFORE the log call, deliberately. A logging handler
        can block - a full disk, a slow syslog socket - and the latch must not
        sit behind it.

        **This does not stop the machine, and cannot.** The supervisor never
        touches the wire, which is what makes it non-blocking and testable, so
        the caller must also invoke
        :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`
        itself. Note also what a browser button is: a **convenience** stop that
        depends on a network, a web server, an event loop and this process, any
        one of which can be the thing that has failed. It is never
        safety-rated. The safety-rated stop is the wired mushroom attested
        through :meth:`confirm_estop_wiring` - and while STO is jumpered even
        that one is a ramp, not a removal of torque.
        """
        verdict = SafetyVerdict(
            action=SafetyAction.QUICK_STOP,
            rule=RULE_OPERATOR_ESTOP,
            detail=f"operator emergency stop: {reason}",
            latched=True,
            since=self._clock.monotonic(),
        )
        self._estop = verdict
        _logger.error("operator emergency stop latched: %s", reason)
        return verdict

    def trip_from_thread(self, rule: str, action: SafetyAction, detail: str = "") -> None:
        """Raise a demand from a thread that is not the control loop.

        Called from the BITalino acquisition thread, which must never be made
        to wait on the control loop: if the reader blocks, samples are lost,
        and lost samples are what the heart rate is computed from.

        So this does the least that can be done - build a small frozen record
        and append it to a bounded queue. No lock, no clock read, no Modbus, no
        socket, no logging: nothing that can block, and nothing that can raise.
        ``collections.deque.append`` with a ``maxlen`` is used in preference to
        a single attribute slot because a slot silently drops the first of two
        trips raised inside one 200 ms tick, and discarding a safety demand to
        save an allocation is not a trade this machine gets to make.

        The instant is stamped when the control loop picks the trip up, at most
        one tick later. That is the honest stamp - it is when the supervisor
        actually learned of the trip - and it keeps the clock out of the one
        place that must not need one. For the stop that cannot afford a tick,
        use :meth:`latch_estop`, which publishes its verdict immediately.

        Every trip latches, because the thread that raised it will not be there
        to re-assert it next tick, and a demand that quietly expires is worse
        than one that was never made.

        Nothing is validated here, on purpose: a malformed rule id arriving
        from a thread must not raise inside the acquisition loop. Rule ids are
        asserted in the tests instead, through
        :func:`~src.training.types.is_rule_id`.
        """
        self._thread_trips.append(ThreadTrip(rule=rule, action=action, detail=detail))

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Clear the latched verdicts. The only thing in this system that can.

        Never called automatically from anywhere - not on a fault, not on a
        reconnect, not at the start of a session. A latched verdict has taken
        the machine out of service, and putting it back into service is a human
        act with a name against it.

        ``estop_released`` is that human stating explicitly that the latching
        mushroom has been pulled back out. Software cannot see the contact, and
        the default is ``False`` so that forgetting the question fails closed.

        Acknowledging while a condition is still true is allowed and harmless:
        the rule re-fires on the next tick and raises the floor again. What is
        not allowed is clearing ``GO_SILENT``, which is one-way by
        construction - see :class:`GoSilentIsTerminal`.

        Pending trips from other threads are deliberately **not** cleared: one
        raised microseconds before this call would otherwise vanish. It is
        drained on the next tick and raises the floor again, which is the
        fail-safe direction.
        """
        if not operator.strip():
            return Err(
                Unattributed(
                    "an acknowledgement must name the operator making it: "
                    "an unattributable safety record is not a safety record"
                )
            )
        estop = self._estop
        floor = self._floor
        if estop is None and floor is None:
            return Err(NothingLatched())
        if floor is not None and floor.action is SafetyAction.GO_SILENT:
            return Err(GoSilentIsTerminal(floor.rule))
        if estop is not None and not estop_released:
            return Err(EmergencyStopStillLatched(estop.since))
        cleared = tuple(verdict.rule for verdict in (estop, floor) if verdict is not None)
        self._estop = None
        self._floor = None
        record = SafetyAcknowledgement(
            operator=operator,
            at=self._clock.monotonic(),
            wall_clock=self._clock.unix_millis(),
            cleared=cleared,
        )
        _logger.warning(
            "safety latches acknowledged by %r at unix_millis=%d: %s",
            operator,
            record.wall_clock,
            ", ".join(cleared),
        )
        return Ok(record)

    # =====================================================================
    # Reading the verdict
    # =====================================================================

    @property
    def standing(self) -> SafetyVerdict | None:
        """The verdict the runtime must apply, or ``None`` if no rule is asking.

        The most severe of the latched floor, the operator e-stop, and every
        rule firing as of the last :meth:`evaluate`. Pure, so it is safe to
        read from a web handler between ticks, and it reflects a
        :meth:`latch_estop` from the same event loop immediately.

        Ties are broken by the order the candidates are considered - e-stop,
        then floor, then live rules - so an operator who pressed the button
        sees their own action named on the screen rather than whichever rule
        happened to reach the same severity.
        """
        return max(self._candidates(), key=_severity, default=None)

    @property
    def standing_action(self) -> SafetyAction:
        """The standing action, with "no verdict" reading as ``NONE``.

        Goes through :func:`~src.training.types.most_severe` rather than a bare
        ``max``, because ``max(())`` raises and a ``ValueError`` out of the
        supervisor would unwind the tick with the motor still commanded.
        """
        return most_severe(verdict.action for verdict in self._candidates())

    @property
    def floor(self) -> SafetyVerdict | None:
        """The most severe latched verdict so far.

        Never decreases without an :meth:`acknowledge`, and a property test
        proves that over arbitrary interleavings of evidence.
        """
        return self._floor

    @property
    def live(self) -> tuple[SafetyVerdict, ...]:
        """Every rule that was firing at the last :meth:`evaluate`, latched or not.

        For the operator screen and the session log: the floor says what the
        machine is being held to, and this says what is true right now,
        including advisories that have already cleared from the floor.
        """
        return self._live

    @property
    def retained_samples(self) -> int:
        """How much heart-rate evidence is currently retained for the trend rules.

        Public because the bound matters in two directions. Three rules filter
        this history on every tick at 5 Hz on a Pi, so it must not grow with
        the length of the session; and a session log that recorded it can tell
        a session that was well measured from one that was mostly blind, which
        is the same question :attr:`~src.training.types.ZoneCounters.total`
        answers from the other side.
        """
        return len(self._history)

    def _candidates(self) -> tuple[SafetyVerdict, ...]:
        """Everything that could be the standing verdict, in tie-breaking order."""
        held = (self._estop, self._floor)
        return (*(verdict for verdict in held if verdict is not None), *self._live)

    # =====================================================================
    # The tick
    # =====================================================================

    def evaluate(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Judge one observation and return the verdict the runtime must apply.

        The order inside is load-bearing:

        1. note the tick, which is also how ``loop_stall`` learns the interval
           since the previous one;
        2. ingest the heart rate, so the trend rules see this tick's evidence;
        3. evaluate every rule independently - no rule can see another's
           verdict, so none can be talked out of firing;
        4. push every latched verdict into the floor;
        5. drain trips raised from other threads, which also latch;
        6. return the most severe thing standing.

        Never raises. Not for a missing measurement, not for a phase with no
        target, not for a heart rate that is absurd: an exception here would
        unwind the tick with the motor still commanded, which is the failure
        this whole arrangement exists to prevent.
        """
        self._note_tick(observation.now)
        self._ingest(observation)
        self._live = self._live_verdicts(observation)
        for verdict in self._live:
            if verdict.latched:
                self._raise_floor(verdict)
        self._drain_thread_trips(observation.now)
        return self.standing

    def _note_tick(self, now: Monotonic) -> None:
        """Record this tick and the gap since the previous one."""
        previous = self._last_tick_at
        self._last_tick_at = now
        self._tick_gap = None if previous is None else elapsed(previous, now)

    def _ingest(self, observation: SafetyObservation) -> None:
        """Retain this tick's heart rate, if and only if it is genuinely new.

        **The gate that matters in this whole module.**
        ``src/signal_processing.py`` keeps a ``_last_metrics`` dict and
        re-emits it unchanged whenever extraction fails - too little data, a
        BioSPPy exception, a quality grade below ``good``. A re-emitted dict
        carries a perfectly current timestamp and an unchanged sequence number,
        so a heart rate that has not changed between two reads very often
        means nothing was measured at all.

        Two consequences are implemented here and nowhere else:

        * the trend rules (``hr_drop``, ``hr_rate``, ``hr_unresponsive``) only
          ever see samples whose ``seq`` strictly advanced. Without that gate
          the same sample would be appended five times a second, flattening
          every slope and making a fall impossible to see.
        * ``hr_stale`` measures age from the last sample that passed BOTH this
          gate and ``usable_bpm``, so a dead pipeline shows up as an absence of
          evidence rather than as a stable heart rate.

        Note the asymmetry, which is deliberate: the *level* rules
        (``hr_hard_max``, ``hr_critical``) judge the latest usable reading
        without the sequence gate. A dangerously high heart rate that we have
        merely stopped being able to confirm is not evidence of recovery, so
        the conservative direction there is to keep acting on it - and
        ``hr_stale`` is what escalates the fact that it is no longer fresh.
        """
        self._prune(observation.now)
        sample = observation.heart_rate
        if sample is None:
            return
        if not sample.is_new_evidence_after(self._last_seq):
            return
        self._last_seq = sample.seq
        bpm = sample.usable_bpm
        if bpm is None:
            return
        self._last_good_at = sample.at
        self._history.append(
            _HrPoint(at=sample.at, bpm=bpm, commanded_rpm=observation.commanded_rpm)
        )

    def _prune(self, now: Monotonic) -> None:
        """Drop evidence older than the longest window any rule asks for."""
        limits = self._limits
        longest = max(limits.hr_drop_window, limits.hr_rate_window, limits.unresponsive_window)
        while self._history and elapsed(self._history[0].at, now) > longest:
            self._history.popleft()

    def _points_within(self, now: Monotonic, window: Seconds) -> tuple[_HrPoint, ...]:
        """Retained evidence no older than ``window``, oldest first."""
        return tuple(point for point in self._history if elapsed(point.at, now) <= window)

    def _raise_floor(self, verdict: SafetyVerdict) -> None:
        """Raise the latched high-water mark. It is never lowered here.

        A strictly-more-severe verdict replaces the floor; an equally severe one
        does not, so the floor keeps the verdict that fired FIRST and its
        original ``since``. Dwell time is what separates a spike from a trend,
        so the older instant is the more informative one.
        """
        current = self._floor
        if current is not None and verdict.action <= current.action:
            return
        self._floor = verdict
        _logger.error(
            "safety floor raised to %s by rule %s: %s",
            verdict.action.name,
            verdict.rule,
            verdict.detail,
        )

    def _drain_thread_trips(self, now: Monotonic) -> None:
        """Turn trips raised from other threads into latched verdicts."""
        while self._thread_trips:
            trip = self._thread_trips.popleft()
            detail = trip.detail or THREAD_TRIP_DETAIL
            self._raise_floor(
                SafetyVerdict(
                    action=trip.action,
                    rule=trip.rule,
                    detail=detail,
                    latched=True,
                    since=now,
                )
            )

    def _live_verdicts(self, observation: SafetyObservation) -> tuple[SafetyVerdict, ...]:
        """Every rule that fires on this observation.

        Each rule is evaluated independently and sees only the observation and
        its own dwell tracker. None of them can see another rule's verdict, so
        no rule can suppress another - the combination happens afterwards, by
        severity, in :attr:`standing`. ``operator_estop`` is absent from this
        list because it is not a function of an observation: it is latched
        synchronously by :meth:`latch_estop`.
        """
        candidates = (
            self._rule_drive_fault(observation),
            self._rule_comms_lost(observation),
            self._rule_hr_drop(observation),
            self._rule_hr_hard_max(observation),
            self._rule_hr_critical(observation),
            self._rule_hr_rate(observation),
            self._rule_hr_stale(observation),
            self._rule_hr_unresponsive(observation),
            self._rule_current_high(observation),
            self._rule_no_load(observation),
            self._rule_tracking_error(observation),
            self._rule_reverse_rotation(observation),
            self._rule_session_overrun(observation),
            self._rule_loop_stall(observation),
            self._rule_attendant_absent(observation),
        )
        return tuple(verdict for verdict in candidates if verdict is not None)

    # =====================================================================
    # Shared evidence helpers
    # =====================================================================

    @staticmethod
    def _session_start(observation: SafetyObservation) -> Monotonic:
        """When the session began, derived from this tick's own elapsed time.

        Taken from the observation rather than remembered, so the supervisor
        cannot hold a second opinion about when the session started - and so
        the rules that measure an age against it stay pure functions of the
        evidence they were handed.
        """
        return Monotonic(observation.now - observation.elapsed)

    @staticmethod
    def _heart_rate_supervised(observation: SafetyObservation) -> bool:
        """Whether the heart-rate rules apply in this phase.

        Every phase but ``DONE``. ``RECOVERY`` in particular stays fully
        supervised: the motor is stopped but the person is still in the
        machine, and that is physiologically the phase with the highest
        vasovagal risk. ``DONE`` opts out because the programme is over, no
        setpoint will be issued again, and a rig alarming forever about an
        absent heart rate teaches an operator to ignore the alarm that matters.
        """
        return observation.phase is not Phase.DONE

    def _usable_bpm(self, observation: SafetyObservation) -> Bpm | None:
        """The heart rate the level rules may act on, or ``None``.

        Three conditions, all of which must hold: the phase is supervised, a
        sample exists, and its own :attr:`~src.training.types.HeartRateSample.usable_bpm`
        allows it - a rate was measured and its quality grade is trustworthy.
        Freshness is deliberately not among them; see :meth:`_ingest`.
        """
        if not self._heart_rate_supervised(observation):
            return None
        sample = observation.heart_rate
        if sample is None:
            return None
        return sample.usable_bpm

    # =====================================================================
    # The rules
    # =====================================================================
    #
    # One method each, all with the same shape: decide the condition from the
    # observation, hand it to this rule's tracker with this rule's dwell, and
    # build a verdict if the tracker says it is firing. Nothing else. A rule
    # that needed to know about another rule would be a precedence decision
    # smuggled out of `standing`, where it is written down.

    def _rule_drive_fault(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """The drive is in fault: torque is already gone and the mass is coasting.

        ``RAMP_DOWN`` rather than ``QUICK_STOP``, and the difference is not a
        nuance. By the time this rule sees a fault the drive has already
        applied its own fault reaction - a ramp, or a freewheel - so there is
        no stop left for software to command; a faster demand would be a
        promise about a machine that is no longer listening. What remains is to
        end the session and to surface the LFT code to whoever is standing in
        front of the drive.

        Latched, and **never acknowledged automatically anywhere**. There is no
        automatic fault reset in this system: "reset it and see" with a person
        inside the machine is how a short circuit becomes a fire.

        The status word decides that a fault exists; ``fault`` only names it.
        That authority split is settled in ``src.motor.drive``, which is why
        this rule reads ``drive_state`` and not the presence of a report.
        """
        firing = self._trackers[RULE_DRIVE_FAULT].update(
            condition=observation.drive_state is DriveState.FAULT,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_DRIVE_FAULT,
            detail=_fault_detail(observation.fault),
            latched=True,
            since=firing.since,
        )

    def _rule_comms_lost(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """N consecutive failed exchanges: stop writing, and let the drive stop it.

        ``GO_SILENT`` is the most severe action in the enum because it is the
        only one that does not depend on this process continuing to work
        correctly. With the link failing, every other verdict is a request
        there is no evidence was delivered. Ceasing to write lets the drive's
        own ``ttO`` communication timeout expire, which makes the drive ramp
        the motor down by itself - a timer on the other side of the serial
        link, which is exactly the point. Any watchdog written inside this
        process would die with the loop it was watching, and silently.

        One-way. No code path in this system resumes writing after
        ``GO_SILENT``, which is also why :meth:`acknowledge` refuses to clear
        it: recovery is an operator action on a machine that has demonstrably
        stopped.
        """
        failures = observation.consecutive_comm_failures
        limit = self._limits.comms_lost_failures
        firing = self._trackers[RULE_COMMS_LOST].update(
            condition=failures >= limit,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.GO_SILENT,
            rule=RULE_COMMS_LOST,
            detail=(
                f"{failures} consecutive drive exchanges failed (limit {limit}); "
                "no further writes will be sent, so the drive's own ttO timeout "
                "ramps the motor down"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_hr_drop(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """THE VASOVAGAL RULE: a fall of more than 25 bpm inside 30 s ends the session.

        This is the rule the entire two-layer design exists for. A person
        beginning to faint under centripetal load shows a **falling** heart
        rate. The control law reads that as "below the target zone" and answers
        by speeding up - it is not miscomputing, it is answering a different
        question - and here the same evidence ends the session. Anything that
        let one of those two be weighed against the other would be a place
        where the wrong one could win.

        Measured against the **peak** inside the window rather than the single
        sample at its start: a fall is a fall from wherever the rate actually
        was, and pinning it to whichever 30-second-old median happened to land
        there would make the rule depend on one sample.

        Latched, so a rate that recovers does not resume the session. Presyncope
        that resolves because the load came off is not evidence that more load
        would be safe. No hysteresis band either, for the same reason: a
        latched verdict is never withdrawn, so there is nothing to chatter.
        """
        tracker = self._trackers[RULE_HR_DROP]
        if not self._heart_rate_supervised(observation):
            tracker.release()
            return None
        points = self._points_within(observation.now, self._limits.hr_drop_window)
        if len(points) < MIN_TREND_POINTS:
            tracker.release()
            return None
        peak = max(point.bpm for point in points)
        latest = points[-1].bpm
        fall = peak - latest
        firing = tracker.update(
            condition=fall >= self._limits.hr_drop_bpm,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_HR_DROP,
            detail=(
                f"heart rate fell {fall} bpm (from {peak} to {latest}) within "
                f"{self._limits.hr_drop_window:.0f} s: a falling rate under load is "
                "presyncope, and the control law would answer it by accelerating"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_hr_hard_max(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Above the hard maximum for five continuous seconds: end the session.

        The hysteresis band below the limit is not cosmetic here. Without it, a
        heart rate oscillating across the limit restarts the five-second dwell
        on every dip, and the rule would never fire at all - failing in the
        dangerous direction, silently, on exactly the signal that most needs
        it. With the band, the dwell accumulates once the rate has crossed the
        limit and only releases when it has genuinely come back down.
        """
        tracker = self._trackers[RULE_HR_HARD_MAX]
        bpm = self._usable_bpm(observation)
        if bpm is None:
            tracker.release()
            return None
        limits = self._limits
        above = bpm > limits.hard_max_bpm or (tracker.held and bpm > limits.hr_hard_max_release_bpm)
        firing = tracker.update(
            condition=above,
            now=observation.now,
            dwell=limits.hr_hard_max_dwell,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_HR_HARD_MAX,
            detail=(
                f"heart rate {bpm} bpm has been above the hard maximum of "
                f"{limits.hard_max_bpm} bpm for {firing.held:.1f} s"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_hr_critical(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """At or above the critical rate: zero the speed reference now.

        No dwell, because at this rate every second of dwell is another second
        of exposure - and the reading is already an 8 s median refreshed at
        1 Hz, so the averaging that a dwell would provide has been done
        upstream.

        ``QUICK_STOP`` is not "immediate": it means zero LFRD at once and
        **leave the run command in place**, so the drive decelerates on its own
        commissioned ramp. Removing the run command from a turning machine is
        CiA402 transition 8, which drops the output stage and freewheels a
        loaded centrifuge for minutes.
        """
        tracker = self._trackers[RULE_HR_CRITICAL]
        bpm = self._usable_bpm(observation)
        if bpm is None:
            tracker.release()
            return None
        limit = self._limits.critical_bpm
        firing = tracker.update(condition=bpm >= limit, now=observation.now, dwell=NO_DWELL)
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.QUICK_STOP,
            rule=RULE_HR_CRITICAL,
            detail=(
                f"heart rate {bpm} bpm is at or above the critical limit of {limit} bpm: "
                "the speed reference is zeroed immediately and the drive stops on its "
                "own ramp"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_hr_rate(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Rising faster than the bound allows: back the speed off.

        A rate of rise, so it needs two points and a span between them. The
        minimum span exists because dividing one bpm difference by a two-second
        gap turns a single noisy median into an alarming slope; twenty seconds
        is two and a half of the pipeline's own 8 s windows.

        Unlatched with a release band, because this is genuinely advisory: a
        heart rate settling back inside its bound is evidence the reduction
        worked, and holding the machine down until a human clicked would train
        that human to click without reading.
        """
        tracker = self._trackers[RULE_HR_RATE]
        limits = self._limits
        if not self._heart_rate_supervised(observation):
            tracker.release()
            return None
        points = self._points_within(observation.now, limits.hr_rate_window)
        if len(points) < MIN_TREND_POINTS:
            tracker.release()
            return None
        span = elapsed(points[0].at, points[-1].at)
        if span < limits.hr_rate_min_span:
            tracker.release()
            return None
        rise = BpmPerMinute((points[-1].bpm - points[0].bpm) / span * SECONDS_PER_MINUTE)
        rising = rise > limits.hr_rise_limit or (tracker.held and rise > limits.hr_rise_release)
        firing = tracker.update(condition=rising, now=observation.now, dwell=NO_DWELL)
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.REDUCE,
            rule=RULE_HR_RATE,
            detail=(
                f"heart rate is rising at {rise:.1f} bpm/min over the last {span:.0f} s "
                f"(limit {limits.hr_rise_limit:.1f} bpm/min)"
            ),
            latched=False,
            since=firing.since,
        )

    def _rule_hr_stale(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """No fresh, trustworthy heart rate: FREEZE at 10 s, REDUCE at 30 s, RAMP_DOWN at 60 s.

        ``FREEZE`` first because with no trustworthy heart rate, regulating is
        guessing: the last commanded speed is the only value known to have
        been survivable a moment ago, so holding it beats moving in either
        direction. Then ``REDUCE``, because a guess that persists should at
        least be made at a lower load. Then ``RAMP_DOWN``, because a minute
        with no heart rate is a session that cannot be supervised at all,
        whatever the machine happens to be doing.

        The age is measured from the last sample that passed both the sequence
        gate and the quality gate (see :meth:`_ingest`), and from the start of
        the session when no such sample has ever arrived - a reading that has
        never happened is not a recent one.

        The first two levels are unlatched, so a pipeline that comes back
        releases them; the third latches, because deciding to end a session is
        not a decision that un-makes itself.
        """
        tracker = self._trackers[RULE_HR_STALE]
        limits = self._limits
        if not self._heart_rate_supervised(observation):
            tracker.release()
            return None
        last_good = self._last_good_at
        reference = last_good if last_good is not None else self._session_start(observation)
        age = elapsed(reference, observation.now)
        firing = tracker.update(
            condition=age > limits.hr_stale_freeze_after,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        if age > limits.hr_stale_ramp_after:
            action = SafetyAction.RAMP_DOWN
            latched = True
        elif age > limits.hr_stale_reduce_after:
            action = SafetyAction.REDUCE
            latched = False
        else:
            action = SafetyAction.FREEZE
            latched = False
        seen = "none has ever arrived" if last_good is None else f"{age:.1f} s ago"
        return SafetyVerdict(
            action=action,
            rule=RULE_HR_STALE,
            detail=(
                f"no fresh trustworthy heart rate for {age:.1f} s ({seen}); "
                "a re-emitted metrics dict is not new evidence, however current "
                "its timestamp looks"
            ),
            latched=latched,
            since=firing.since,
        )

    def _rule_hr_unresponsive(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """The heart rate does not follow the speed over a long window: reduce.

        Either a non-responder - somebody whose heart rate simply does not
        track this load, in whom regulating speed by heart rate is
        meaningless - or a sensor that is reporting a plausible number with no
        relation to the person. Both make the control loop an open loop, and an
        open loop that believes it is closed will keep increasing speed looking
        for a response it will never see.

        Five minutes, because heart rate lags load with a time constant of
        30-60 s: any shorter window would report normal physiology as a fault.
        The window itself is the dwell.

        The comparison is between the **means** of the two halves of the
        window, for both signals. A mean is moved only fractionally by one
        stray sample and fully by a sustained change, which is the
        discrimination this rule needs in both directions: it must not fire
        because the setpoint blipped, and it must not stay quiet because one
        heart-rate median happened to land high.
        """
        tracker = self._trackers[RULE_HR_UNRESPONSIVE]
        limits = self._limits
        if not self._heart_rate_supervised(observation):
            tracker.release()
            return None
        points = self._points_within(observation.now, limits.unresponsive_window)
        if len(points) < limits.unresponsive_min_points * WINDOW_HALVES:
            tracker.release()
            return None
        if elapsed(points[0].at, points[-1].at) < limits.unresponsive_min_span:
            tracker.release()
            return None
        half = len(points) // WINDOW_HALVES
        early = points[:half]
        late = points[half:]
        rpm_rise = fmean(point.commanded_rpm for point in late) - fmean(
            point.commanded_rpm for point in early
        )
        bpm_rise = fmean(point.bpm for point in late) - fmean(point.bpm for point in early)
        unresponsive = (
            rpm_rise >= limits.unresponsive_rpm_rise and bpm_rise < limits.unresponsive_bpm_rise
        )
        firing = tracker.update(condition=unresponsive, now=observation.now, dwell=NO_DWELL)
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.REDUCE,
            rule=RULE_HR_UNRESPONSIVE,
            detail=(
                f"mean commanded speed rose {rpm_rise:.0f} motor rpm across "
                f"{limits.unresponsive_window:.0f} s while the mean heart rate moved only "
                f"{bpm_rise:.1f} bpm: a non-responder, or a sensor unrelated to the person"
            ),
            latched=False,
            since=firing.since,
        )

    def _rule_current_high(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Motor current above its warning level, or above its trip level.

        Disproportionately valuable evidence, and the reason this rule exists
        with only a 2.15 A nameplate to compare against: a centrifuge beginning
        to rub its enclosure, a bearing starting to seize, a load that has
        shifted - all of them appear in the current long before they appear in
        the speed, in the heart rate, or to anybody watching. A current
        climbing at constant speed means something is binding, not that the
        person is working harder.

        Two levels in one rule, because they are the same evidence: above the
        warning level for ten seconds is a ``REDUCE`` (ten seconds tolerates an
        acceleration transient but not a rub), while above the trip level is a
        ``RAMP_DOWN`` with no dwell at all - it jumps the queue by being given
        a zero dwell rather than by a separate rule, so the hysteresis and the
        dwell state stay in one place.

        ``None`` current releases the rule: a failed read is an absence of
        evidence, and ``comms_lost`` owns that failure.
        """
        tracker = self._trackers[RULE_CURRENT_HIGH]
        current = observation.current
        if current is None:
            tracker.release()
            return None
        limits = self._limits
        above_trip = current > limits.current_trip_a
        above_warn = current > limits.current_warn_a or (
            tracker.held and current > limits.current_warn_release_a
        )
        firing = tracker.update(
            condition=above_warn,
            now=observation.now,
            dwell=NO_DWELL if above_trip else limits.current_warn_dwell,
        )
        if firing is None:
            return None
        if above_trip:
            return SafetyVerdict(
                action=SafetyAction.RAMP_DOWN,
                rule=RULE_CURRENT_HIGH,
                detail=(
                    f"motor current {current:.2f} A is above the trip level of "
                    f"{limits.current_trip_a:.2f} A (nameplate 2.15 A): something is binding"
                ),
                latched=True,
                since=firing.since,
            )
        return SafetyVerdict(
            action=SafetyAction.REDUCE,
            rule=RULE_CURRENT_HIGH,
            detail=(
                f"motor current {current:.2f} A has been above the warning level of "
                f"{limits.current_warn_a:.2f} A for {firing.held:.1f} s"
            ),
            latched=False,
            since=firing.since,
        )

    def _rule_no_load(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Current below the floor while the machine should be turning: end the session.

        An open motor phase, a disconnected motor, a blown output stage, or a
        current register that is not the one we think it is. Every one of those
        means the software is commanding a machine it is not actually driving,
        and the feedback it is regulating on describes nothing.

        Guarded three ways so it cannot cry wolf: the setpoint has to be high
        enough that current would be measurable, the drive has to say its
        output stage is enabled, and the current has to have been below the
        floor continuously for the dwell - long enough for the drive to build
        current after a step in the setpoint, and no longer.

        A ``None`` current releases the rule rather than firing it. Absence of
        evidence is not evidence of no load.
        """
        tracker = self._trackers[RULE_NO_LOAD]
        current = observation.current
        if current is None:
            tracker.release()
            return None
        limits = self._limits
        should_turn = (
            abs(observation.commanded_rpm) >= limits.no_load_min_rpm
            and observation.drive_state is DriveState.OPERATION_ENABLED
        )
        firing = tracker.update(
            condition=should_turn and current < limits.no_load_floor_a,
            now=observation.now,
            dwell=limits.no_load_dwell,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_NO_LOAD,
            detail=(
                f"commanded {observation.commanded_rpm} motor rpm with the output stage "
                f"enabled, but the drive reports only {current:.2f} A for "
                f"{firing.held:.1f} s (floor {limits.no_load_floor_a:.2f} A): an open "
                "phase, no motor, or the wrong current register"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_tracking_error(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Commanded and measured speed diverging while not ramping: end the session.

        Stalled, overloaded, or writing to the wrong register. The last of
        those is the one that makes this rule worth its complexity: most
        Altivar parameters are writable while the drive is running, so an
        address off by one does not bounce - it writes the speed setpoint into
        whatever parameter is next door, and every subsequent read looks
        perfectly normal. The shaft not following the command is how that
        becomes visible.

        Only judged while the runtime says it is not ramping, because during a
        ramp commanded and measured speed are *supposed* to differ - that is
        what a ramp is. The flag has to be the runtime's own statement rather
        than something inferred here from successive setpoints, or a stalled
        shaft would look like a ramp that never finished.
        """
        tracker = self._trackers[RULE_TRACKING_ERROR]
        measured = observation.measured_rpm
        if measured is None:
            tracker.release()
            return None
        limits = self._limits
        divergence = abs(observation.commanded_rpm - measured)
        firing = tracker.update(
            condition=(
                not observation.ramping
                and observation.drive_state is DriveState.OPERATION_ENABLED
                and divergence > limits.tracking_error_rpm
            ),
            now=observation.now,
            dwell=limits.tracking_error_dwell,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_TRACKING_ERROR,
            detail=(
                f"commanded {observation.commanded_rpm} motor rpm but the drive measures "
                f"{measured} rpm, a divergence of {divergence} rpm for {firing.held:.1f} s "
                f"while not ramping (tolerance {limits.tracking_error_rpm} rpm)"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_reverse_rotation(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """The shaft is turning against the commanded direction: zero the reference.

        With a person inside, rotation opposite to the command means either the
        motor is wired backwards or a sign was lost somewhere in the setpoint
        path. Either way the machine is not doing what the software believes,
        and no further command from that software can be trusted to help.

        The test is the sign of the product, so a commanded zero cannot fire
        this rule: a shaft turning with nothing commanded is a freewheel or a
        tracking failure, which other rules own and which would be a different
        sentence to the operator. The magnitude band exists because RFRD
        dithers about zero at standstill, and a sign flip inside that noise
        must not fire a ``QUICK_STOP``.
        """
        tracker = self._trackers[RULE_REVERSE_ROTATION]
        measured = observation.measured_rpm
        if measured is None:
            tracker.release()
            return None
        limits = self._limits
        opposed = observation.commanded_rpm * measured < 0
        firing = tracker.update(
            condition=opposed and abs(measured) > limits.reverse_rpm,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.QUICK_STOP,
            rule=RULE_REVERSE_ROTATION,
            detail=(
                f"commanded {observation.commanded_rpm} motor rpm but the drive measures "
                f"{measured} rpm, against the commanded direction: the motor is wired "
                "backwards, or a sign was lost in the setpoint path"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_session_overrun(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """Past the programme's total duration plus its grace: end the session.

        A session that has outlived its own programme means the phase machine
        above has lost track - a phase that never advanced, a plan that was
        replaced, a clock that stepped. The machine is then running to no plan
        at all, and the only safe response to "nobody is deciding what this
        should be doing" is to stop deciding to continue.
        """
        limits = self._limits
        deadline = Seconds(observation.total_duration + limits.overrun_grace)
        firing = self._trackers[RULE_SESSION_OVERRUN].update(
            condition=observation.elapsed > deadline,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        return SafetyVerdict(
            action=SafetyAction.RAMP_DOWN,
            rule=RULE_SESSION_OVERRUN,
            detail=(
                f"the session has run {observation.elapsed:.0f} s against a programme of "
                f"{observation.total_duration:.0f} s plus {limits.overrun_grace:.0f} s of "
                "grace: the phase machine has lost track"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_loop_stall(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """The control loop missed its period: FREEZE, then GO_SILENT.

        **Read this before trusting it.** This rule can only ever fire *after*
        the loop has recovered, because it is evaluated by the loop it is
        watching: it measures the interval between two consecutive
        :meth:`evaluate` calls, and while the loop is stalled there is no call
        to measure anything. A watchdog written inside this process would run
        on the same interpreter, in the same loop, behind the same lock as the
        code it was meant to watch - it would die with it, and silently. The
        real backstop is outside: with no keepalive, the drive's own ``ttO``
        timeout ramps the motor down.

        So what this rule is for is the recovered stall, which is otherwise
        invisible. Both levels latch, because by the time the verdict exists
        the evidence is already gone - an unlatched verdict would vanish on the
        next healthy tick and nobody would ever act on it. A transient that
        needs remembering must latch, or it is forgotten.

        ``GO_SILENT`` at the longer gap is the one action that does not depend
        on this process working correctly, which is the right answer to "this
        process has demonstrated that it may not be".
        """
        gap = self._tick_gap
        if gap is None:
            return None
        limits = self._limits
        tracker = self._trackers[RULE_LOOP_STALL]
        firing = tracker.update(
            condition=gap > limits.loop_stall_freeze_gap,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        if gap > limits.loop_stall_silent_gap:
            action = SafetyAction.GO_SILENT
            consequence = "no further writes will be sent; the drive's ttO timeout stops it"
        else:
            action = SafetyAction.FREEZE
            consequence = "the setpoint is held where it was"
        return SafetyVerdict(
            action=action,
            rule=RULE_LOOP_STALL,
            detail=(
                f"the control loop took {gap:.2f} s between ticks against a period of "
                f"{limits.control_period:.2f} s: {consequence}"
            ),
            latched=True,
            since=firing.since,
        )

    def _rule_attendant_absent(self, observation: SafetyObservation) -> SafetyVerdict | None:
        """No presence ping from the UI: FREEZE at 60 s, then RAMP_DOWN.

        **This rig must never run unattended, and "never" is worth enforcing in
        code rather than in a procedure.** Every other rule in this file assumes
        somebody is there to be told about it: an acknowledgement is a human
        act, a ``QUICK_STOP`` still takes seconds of ramp, and the one
        safety-rated stop on the machine is a mushroom that somebody has to
        press. A session with nobody watching has none of that.

        A UI that has stopped pinging is indistinguishable from an operator who
        walked away - a closed laptop, a dropped WiFi link, a browser tab
        discarded to save memory - and the safe reading of all of them is the
        same one.

        Measured from the start of the session when no ping has ever arrived, so
        a session that never had an attendant escalates exactly like one that
        lost theirs. The ``FREEZE`` level is unlatched, so an operator who
        comes back releases it; the ``RAMP_DOWN`` latches.
        """
        tracker = self._trackers[RULE_ATTENDANT_ABSENT]
        if observation.phase is Phase.DONE:
            tracker.release()
            return None
        limits = self._limits
        seen = observation.attendant_last_seen
        reference = seen if seen is not None else self._session_start(observation)
        age = elapsed(reference, observation.now)
        firing = tracker.update(
            condition=age > limits.attendant_freeze_after,
            now=observation.now,
            dwell=NO_DWELL,
        )
        if firing is None:
            return None
        if age > limits.attendant_ramp_after:
            action = SafetyAction.RAMP_DOWN
            latched = True
        else:
            action = SafetyAction.FREEZE
            latched = False
        never = " (none has ever arrived)" if seen is None else ""
        return SafetyVerdict(
            action=action,
            rule=RULE_ATTENDANT_ABSENT,
            detail=(
                f"no attendant presence ping for {age:.0f} s{never}: this machine is "
                "not permitted to run with nobody watching it"
            ),
            latched=latched,
            since=firing.since,
        )
