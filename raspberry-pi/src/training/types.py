"""The shared vocabulary of a training session.

Every module that takes part in a session - the plan, the control law, the
safety supervisor, the runtime loop and the web UI - names things from here.
That is the whole purpose: a phase, a safety action, a heart-rate sample and a
telemetry snapshot mean **one** thing in this system, not one thing per module.
Three of those consumers are written independently, so the shapes below are the
contract between them.

What this module is:

* enums, so nothing that carries meaning travels as a bare ``str`` or ``int``
  (contract rule 2);
* frozen, slotted records, so a decision taken from an observation cannot be
  changed under the decision;
* the invariants of those records, written down next to them, and where they
  can be expressed as a predicate, written as one.

What this module deliberately is **not**: control logic, policy, rule
thresholds, I/O, and above all time. Nothing here reads a clock. Every
timestamp and every duration arrives as a parameter (contract rule 4), which is
what lets a 45-minute programme be tested in under a second.

Three traps have their own paragraphs below, because each is silent at runtime:

1. :class:`HeartRateSample` and its ``seq`` - ``src/signal_processing.py``
   **re-emits the previous metrics dict** when extraction fails, so an
   unchanged heart rate is not evidence that the heart rate is unchanged.
2. :class:`SafetyAction` is ordered, and the order *is* the precedence rule:
   the supervisor combines verdicts with ``max``. Reordering the members
   silently rewrites the safety policy.
3. :class:`TelemetrySnapshot` carries the **age** of every measurement, not
   just its value, because a stale number that looks live is the most dangerous
   thing this machine's screen can show.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, IntEnum, unique
from types import MappingProxyType
from typing import Final

from src.motor.drive import DriveState, FaultReport
from src.units import (
    Amperes,
    Bpm,
    GearRatio,
    GLoad,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    OutputRpm,
    ResultantG,
    Seconds,
    UnixMillis,
    elapsed,
    motor_rpm_to_hertz,
    motor_to_output_rpm,
    output_rpm_to_g,
    resultant_g,
)

# =========================================================================
# Where the session is
# =========================================================================


@unique
class Phase(Enum):
    """Which part of the programme the session is in.

    The values are lowercase identifiers on purpose, and they are part of the
    contract: they are what the session log stores and what the web UI receives
    over the wire. ``auto()`` was rejected precisely here - an ``auto()`` phase
    is an integer that renumbers itself the day somebody reorders the members,
    and every historical record then means something else. Renaming a value
    below invalidates stored sessions; adding a member does not.

    The members are declared in the order a normal session visits them, but
    this is **not** an ordering and there is no arithmetic on it. A session can
    leave any phase for any other: a :class:`SafetyAction` of ``RAMP_DOWN``
    sends WARMUP straight to COOLDOWN, and an operator can end a session at any
    moment. Code that infers "later than" from a phase is wrong; code that asks
    "which phase is it" is right.
    """

    BASELINE = "baseline"
    # Measuring the resting heart rate with the motor stopped: no setpoint, no
    # target, nothing turning. The reference every later zone is computed from.

    WARMUP = "warmup"
    # Speed is being raised towards the working point while the heart rate is
    # watched for its response. The plant gain is at its weakest here, because
    # centripetal load goes as the square of speed.

    HOLD = "hold"
    # The working phase: speed is modulated to keep the heart rate inside its
    # target zone. The only phase in which the control law has a target.

    COOLDOWN = "cooldown"
    # Speed is being brought down to zero on a controlled ramp, whether the
    # programme ended normally or a safety verdict cut it short.

    RECOVERY = "recovery"
    # Motor stopped, person still in the machine, heart rate still recorded.
    # Physiologically the most informative phase and the one with the highest
    # vasovagal risk, which is why monitoring continues with nothing turning.

    DONE = "done"
    # The programme is over. No setpoint will be issued again in this session;
    # a new session is a new object. Never a phase the control law acts in.


# =========================================================================
# What the safety supervisor can demand
# =========================================================================


@unique
class Occupancy(Enum):
    """Who is on board, declared by the operator BEFORE any rotation.

    Declared once when motion starts and never changed while the machine is
    turning: a rule set that could be relaxed mid-rotation by a click is a rule
    set that will be. The values are wire strings, like :class:`Phase`.
    """

    BENCH = "bench"
    """Nobody on board. Motor uncoupled, OR arm coupled with the capsule empty.

    The motor-rpm ceiling is the configured ``MOTOR_MAX_RPM`` (default 300,
    never above the 1380 rpm nameplate), and the heart-rate rules are advisory
    because the heart rate on screen does not belong to anyone in the machine.
    """

    OCCUPIED = "occupied"
    """A person in the capsule. Every rule active, heart-rate limiter mandatory.

    Refused by configuration (``OCCUPANCY_OCCUPIED_ENABLED=false``) until the
    engineering and medical sign-offs of milestone M6 exist.
    """

    @property
    def label(self) -> str:
        """The operator-facing French label. Never parsed."""
        return _OCCUPANCY_LABELS[self]


@dataclass(frozen=True, slots=True)
class OccupancyRefused:
    """Motion refused for the declared occupancy, with the reason in French."""

    occupancy: Occupancy
    detail: str


_OCCUPANCY_LABELS: Final[Mapping[Occupancy, str]] = MappingProxyType(
    {
        Occupancy.BENCH: "BANC - personne a bord : NON",
        Occupancy.OCCUPIED: "PERSONNE A BORD",
    }
)


@unique
class SafetyAction(IntEnum):
    """What the safety supervisor demands, ordered by increasing severity.

    **The ordering is the precedence rule, and it is load-bearing.** When
    several rules fire at once the supervisor takes the most severe of their
    verdicts - implemented as a ``max`` over these members (see
    :func:`most_severe`) - so the member order below *is* the policy.
    Reordering the members rewrites the policy silently, which is why
    ``tests/test_training_types.py`` pins the order explicitly rather than
    trusting it to stay right.

    ``IntEnum`` rather than ``Enum`` because a total order is required for
    ``max`` to mean anything, and because "more severe" genuinely is a
    comparison. The integer is a **severity rank and nothing else**: it is not
    a register value, it is not a wire format, and the session log stores
    ``.name`` rather than the number. Compare against members
    (``action is SafetyAction.GO_SILENT``, ``action >= SafetyAction.REDUCE``),
    never against bare integers - ``action > 2`` type-checks and means nothing.

    None of these members describes the machine's state. Each is a demand, and
    with the drive's STO input jumpered there is no action in this enum that
    removes torque instantly. The fastest stop this machine has is a ramp.
    """

    NONE = 0
    # No rule is asking for anything, so the controller's demand stands. Also
    # what an empty set of verdicts combines to (:func:`most_severe`), because
    # "nobody objected" is the only honest reading of no evidence.

    FREEZE = 1
    # Hold the current speed: stop moving the setpoint in either direction.
    # The verdict for "the evidence has gone stale": with no trustworthy heart
    # rate, regulating is guessing, and the last commanded speed is the only
    # value known to have been survivable a moment ago.

    REDUCE = 2
    # Step the setpoint down and keep regulating. The ordinary response to a
    # heart rate above its zone, or rising faster than the bound allows.

    RAMP_DOWN = 3
    # End the session: hand the phase to COOLDOWN and bring the setpoint to
    # zero on the controlled ramp. Deliberate and unhurried - this is a
    # decision to stop, not an emergency.

    QUICK_STOP = 4
    # Stop as fast as this machine actually can: zero the speed reference NOW,
    # skipping the software ramp, and LEAVE THE RUN COMMAND IN PLACE so the
    # drive decelerates on its own commissioned ramp.
    #
    # "Quick" is relative and the name must not be read as "immediate". The DC
    # bus absorbs about 11 J of the ~420 J stored in the spinning rig, so a
    # stop commanded faster than the commissioned 3-4 s ramp trips overvoltage
    # and drops the drive into FREEWHEEL - a longer, uncontrolled coast-down
    # with a person inside. Removing the run command from a turning machine
    # does the same thing (CiA402 transition 8). See
    # ``src.motor.drive.ControlWord``.

    GO_SILENT = 5
    # STOP WRITING TO THE DRIVE. Do not send another keepalive, another command
    # word or another setpoint; let the drive's own ``ttO`` communication
    # timeout expire, which makes the drive ramp the motor down by itself.
    #
    # The most severe action because it is the only one that does not depend on
    # this process continuing to work correctly. It is the answer to "the
    # software may be the problem": corrupt state, a control loop that has lost
    # its evidence, a link that cannot be trusted. Any watchdog written inside
    # this process would run on the same interpreter, in the same loop, behind
    # the same lock as the code it was meant to watch - it would die with it,
    # and silently. Ceasing to write hands the stop to a timer that lives in
    # the drive, on the other side of the serial link, and that is the point.
    #
    # It is one-way. No code path in this system resumes writing after
    # GO_SILENT: recovery is an operator action on a machine that has
    # demonstrably stopped.


def most_severe(actions: Iterable[SafetyAction]) -> SafetyAction:
    """Combine several verdicts' actions into the one the runner applies.

    The precedence rule of the whole safety layer, in one place: the most
    severe demand wins. An empty input yields :data:`SafetyAction.NONE`, which
    is why this exists rather than a bare ``max`` - ``max(())`` raises, and a
    ``ValueError`` out of the supervisor would unwind the tick with the motor
    still commanded.

    To pick the winning *verdict* rather than the winning action, keep the
    objects and sort on the action::

        winner = max(verdicts, key=lambda verdict: verdict.action, default=None)

    :class:`SafetyVerdict` is deliberately not orderable itself: a
    field-by-field dataclass comparison would break ties on ``rule``, i.e.
    alphabetically, which is a coin toss dressed as a decision.
    """
    return max(actions, default=SafetyAction.NONE)


#: A rule id is lowercase ``snake_case``, starts with a letter, and is at most
#: 64 characters. Not enforced at construction (see :class:`SafetyVerdict`),
#: because construction happens inside the control loop and this module must
#: not raise there. Enforced instead by each rule module's own tests, through
#: :func:`is_rule_id`, which is why the pattern is shared rather than copied.
#:
#: Anchored with ``\A`` and ``\Z`` rather than ``^`` and ``$``: ``$`` also
#: matches *before* a trailing newline, so ``"hr_above_zone\n"`` would pass and
#: would then key a dashboard, an alert and a session log under an id whose end
#: nobody can see. A test pins that case.
RULE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")


def is_rule_id(value: str) -> bool:
    """Whether ``value`` is a well-formed rule id. Total, and it never raises."""
    return RULE_ID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """One rule's demand, with the evidence an operator needs to read it.

    Frozen because a verdict is a record of what was decided at a moment; if it
    could be edited afterwards, the session log would stop being evidence.

    ``rule`` is a ``str`` rather than an enum, and that is a considered
    exception to contract rule 2. The set of rules is **open**: it is the one
    extension point of the safety layer, and closing it into an enum here would
    mean every new rule edits this shared file, in a repository where several
    agents work at once. The discipline that replaces the enum is
    :func:`is_rule_id` plus the requirement that ids be *stable*: a dashboard,
    an alert and a session log all key on this string, so renaming one is a
    breaking change to stored data, not a tidy-up.

    ``detail`` is the opposite: prose, for the person standing next to the
    machine. Nothing may parse it. Same reasoning as
    ``src.motor.drive.BadResponse.detail``.
    """

    action: SafetyAction
    """What is being demanded. The runner applies the most severe of these."""

    rule: str
    """Stable machine-readable id of the rule that fired, e.g. ``hr_above_zone``."""

    detail: str
    """One operator-facing sentence: what was seen, and in what numbers."""

    latched: bool
    """Whether this verdict persists until an operator clears it.

    A latched verdict outlives the condition that raised it. That is the point:
    there is no automatic fault reset and no automatic resumption of motion
    anywhere in this system, so a rule that latches has taken the machine out
    of service until a human looks at it. An unlatched verdict is re-evaluated
    every tick and disappears on its own when the evidence does - which also
    means a transient that needs remembering must latch, or it is forgotten.
    """

    since: Monotonic
    """When this verdict first fired, on the monotonic clock.

    First, not most recent: dwell time is what separates a noise spike from a
    trend, so a re-fired verdict keeps the original instant. Monotonic because
    the Pi has no RTC and its wall clock can step, and elapsed-time logic must
    not care.
    """

    def age(self, now: Monotonic) -> Seconds:
        """How long this verdict has been standing, at ``now``.

        Takes ``now`` rather than reading a clock (contract rule 4), and goes
        through ``src.units.elapsed`` so the result is a ``Seconds`` duration
        rather than a bare float that could be mistaken for another instant.
        """
        return elapsed(self.since, now)


# =========================================================================
# What the ECG pipeline gives us
# =========================================================================


@unique
class SignalQuality(Enum):
    """How much the ECG can be trusted, as ``src/signal_processing.py`` grades it.

    The values are the **exact** strings that module's ``_ecg_quality`` emits,
    so :meth:`from_metric` is a parse of a real wire format rather than a
    translation table someone will forget to update. That grader is the one
    piece BioSPPy lacks: without it, mains hum and a flat lead present as a
    heartbeat, and the machine accelerates on noise.

    Only ``GOOD`` permits a heart rate to exist at all - the pipeline does not
    even run beat detection otherwise, so a sample with any other quality
    carries ``bpm=None`` by construction. See :attr:`is_trustworthy`.
    """

    NO_SIGNAL = "no_signal"
    # Flat or near-flat input: electrodes off, lead broken, or not enough
    # samples yet. Also the answer this module gives to anything it does not
    # recognise, because "I do not know" must never read as "fine".

    MAINS_DOMINATED = "mains_dominated"
    # A notch at the mains frequency removes more than 60% of the signal's
    # variance: the electrodes are picking up powerline hum, not a heart.
    # Dangerous because hum has a perfectly steady "rate".

    NOISY = "noisy"
    # More than half the window is clipped at the ADC rails - movement,
    # saturation, a loose electrode under centripetal load.

    GOOD = "good"
    # Beat detection ran and its output may be used. The only quality on which
    # this system regulates a motor.

    @property
    def is_trustworthy(self) -> bool:
        """Whether a heart rate from this grade may be acted on. Only ``GOOD`` may.

        One place answers this question, so a new grade cannot be quietly
        treated as usable by whichever module was written last.
        """
        return self is SignalQuality.GOOD

    @classmethod
    def from_metric(cls, value: str | None) -> SignalQuality:
        """Parse the ``quality`` field of a metrics dict. Total; never raises.

        ``None`` (the key absent, the pipeline not yet warm) and every
        unrecognised string both become :data:`NO_SIGNAL`. **Never optimistic
        on unknown input**: an unknown grade is indistinguishable from a
        renamed grade, a truncated frame, or a version skew between this module
        and the pipeline, and the only reading of those that cannot hurt
        somebody is "no usable signal".

        A mapping lookup rather than ``cls(value)``, because the enum
        constructor raises on an unknown value and this runs on every treated
        batch.
        """
        if value is None:
            return cls.NO_SIGNAL
        return _QUALITY_BY_WIRE.get(value, cls.NO_SIGNAL)


_QUALITY_BY_WIRE: Final[Mapping[str, SignalQuality]] = MappingProxyType(
    {member.value: member for member in SignalQuality}
)
"""Wire string -> grade, derived from the members so the two cannot drift."""


@dataclass(frozen=True, slots=True)
class HeartRateSample:
    """One heart-rate reading, with everything needed to judge whether it counts.

    The three fields besides ``bpm`` exist because the number on its own is not
    evidence. ``quality`` says whether it may be used at all, ``at`` says how
    old it is, and ``seq`` says whether it is *new*.

    **THE SEQUENCE TRAP - read this before writing anything that consumes a
    sample.** ``src/signal_processing.py`` keeps a ``_last_metrics`` dict and
    **re-emits it unchanged** whenever metric extraction fails (too little
    data, a BioSPPy exception, a quality grade below ``good``). So a heart rate
    that has not changed between two reads does **not** mean the heart rate has
    not changed: very often it means *nothing was measured at all*. The last
    number just sits there looking alive, at 1 Hz, for as long as the failure
    lasts.

    ``seq`` is the only defence. It is a counter, supplied by the caller from
    the signal pipeline's own metric sequence, that increases **only when new
    evidence actually arrived**. A repeated ``seq`` means "no new evidence", and
    the consumer must then treat the sample as it would treat no sample at all:
    it is stale by definition, however recent ``at`` may be. Use
    :meth:`is_new_evidence_after` rather than comparing sequence numbers by
    hand - ``!=`` accepts a counter that went backwards after a restart, and
    ``>=`` accepts the re-emitted dict this whole field exists to catch.

    The consequence worth stating plainly: a controller that misses this gate
    regulates a motor on a measurement that may be minutes old, with a person
    inside the machine, and every screen and log looks entirely normal.
    """

    bpm: Bpm | None
    """The rate, or ``None`` when none could be measured.

    ``None`` is the honest answer and appears often: the pipeline reports a
    heart rate only when ``quality`` is ``good``, and it never fabricates one.
    A consumer that substitutes a default here has invented a vital sign.
    """

    quality: SignalQuality
    """The grade this reading was extracted under. See :class:`SignalQuality`."""

    seq: int
    """Freshness counter; increases only on genuinely new evidence. See above."""

    at: Monotonic
    """When the reading was taken, on the monotonic clock.

    Note what this is not: it is not proof of freshness. A re-emitted metrics
    dict gets a perfectly current ``at`` and a stale ``seq``. Both gates apply.
    """

    @property
    def usable_bpm(self) -> Bpm | None:
        """The rate if it may be acted on, otherwise ``None``.

        The single place the two conditions are joined - a rate exists, and its
        grade is trustworthy - so no consumer can accidentally apply only one
        of them. This does **not** consider freshness, which needs a ``now``
        (see :meth:`age`) or a previous sequence number (see
        :meth:`is_new_evidence_after`); all three gates apply before a number
        reaches the control law.
        """
        if self.bpm is None or not self.quality.is_trustworthy:
            return None
        return self.bpm

    def is_new_evidence_after(self, previous_seq: int | None) -> bool:
        """Whether this sample carries evidence that ``previous_seq`` did not.

        Strictly greater, and nothing else: ``>=`` would accept the re-emitted
        metrics dict, and ``!=`` would accept a counter that restarted at zero
        and so treat old numbers as new. ``None`` means nothing has been seen
        yet, so the first sample of a session is new evidence.
        """
        if previous_seq is None:
            return True
        return self.seq > previous_seq

    def age(self, now: Monotonic) -> Seconds:
        """How long ago this reading was taken, at ``now``.

        ``now`` is a parameter, not a clock read (contract rule 4).
        """
        return elapsed(self.at, now)


# =========================================================================
# What the control law decided
# =========================================================================


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """What the control law asked for this tick, and why.

    A record, not a command: the runner applies the **safety verdict first**
    and uses ``desired_rpm`` only when no verdict stands. The reason that must
    be so is in this dataclass's own numbers - see ``error_bpm``.

    Frozen and slotted like every other record here; one is built per tick.
    """

    desired_rpm: MotorRpm
    """The requested speed at the MOTOR shaft, before any safety override.

    Motor rpm, not output rpm: the gearbox ratio is 49.79, so the two differ by
    a factor of fifty and the type is what keeps them apart. This is a demand,
    not a measurement, and not necessarily what was applied.
    """

    phase: Phase
    """The phase this decision was taken in."""

    error_bpm: float
    """``target_bpm - measured_bpm``. Positive means the heart rate is BELOW target.

    **The sign convention is load-bearing, so it is fixed here and nowhere
    else.** Positive error means the control law may speed the machine up.

    Which is exactly the vasovagal case, and why the safety supervisor has
    precedence over this object: a person beginning to faint shows a *falling*
    heart rate, which produces a *large positive* error, which a controller
    reading only this number answers by accelerating. The control law is not
    wrong to compute that; it is wrong to be obeyed. Whatever is in
    ``desired_rpm`` is subordinate to :class:`SafetyVerdict`.

    A bare ``float`` rather than a unit type because ``Bpm`` is an ``int`` and
    this is a signed real-valued difference; the right home for a ``BpmDelta``
    ``NewType`` is ``src/units.py``, which this module does not own. It must
    always be finite - a ``NaN`` here propagates into a setpoint. When
    ``target_bpm`` is ``None`` this is ``0.0``: "no target" is not "an error of
    the last known size".
    """

    in_deadband: bool
    """Whether the error is small enough that no correction is warranted.

    Not derivable from ``error_bpm`` by a consumer, because the deadband width
    is the control law's own parameter; it is recorded so a session log can
    distinguish "held still deliberately" from "held still by accident". When
    there is no target this is ``True``: nothing to correct.
    """

    target_bpm: Bpm | None
    """The heart rate being aimed at, or ``None`` in a phase that has no target.

    ``None`` in BASELINE, COOLDOWN, RECOVERY and DONE. A phase with no target
    is not a phase with a target of zero.
    """

    reason: str
    """One operator-facing sentence explaining this decision. Never parsed."""


# =========================================================================
# What the screen and the session log see
# =========================================================================


@dataclass(frozen=True, slots=True)
class SpeedView:
    """One speed, in all the units anybody needs to see it in.

    The same rotation expressed five ways: at the motor shaft, at the gearbox
    output (the centrifuge itself), as the drive's output frequency, as the
    centripetal load on the person inside, and as the resultant load (that plus
    gravity) the person actually feels. They travel together as one object
    so a screen cannot end up showing the motor's rpm next to the output's g,
    and so the conversions happen exactly once, in :meth:`from_motor_rpm`,
    through the only module allowed to convert units.

    ``g_load`` is unsigned while the rpm fields are signed, and that is
    physics, not a bug: load goes as the square of angular velocity, so
    reversing the direction of rotation does not relieve the person of it.
    """

    motor_rpm: MotorRpm
    """At the motor shaft: what the drive is commanded with and reports."""

    output_rpm: OutputRpm
    """At the gearbox output: what the centrifuge actually turns at."""

    hertz: Hertz
    """The drive's output frequency, via the motor nameplate point."""

    g_load: GLoad
    """Centripetal load (Gc) at the occupant's radius, in multiples of gravity."""

    resultant_g: ResultantG
    """What the occupant feels (Gr): the centripetal load and gravity in quadrature.

    Always >= 1. Motion limits and session steps are written in this, because
    it is the number a person and a physician reason in.
    """

    @classmethod
    def from_motor_rpm(
        cls,
        rpm: MotorRpm,
        *,
        ratio: GearRatio,
        radius: Metres,
        nominal_rpm: MotorRpm,
        base_hz: Hertz,
    ) -> SpeedView:
        """Derive every view from the one number the drive deals in.

        Keyword-only geometry, with no defaults, on purpose: a wrong gear ratio
        or a wrong radius is a silent fifty-fold or quadratic error, and a
        default value is how a wrong one gets used without anybody choosing it.
        The machine's actual figures (SEW KA37 i = 49.79; 1380 rpm at 50 Hz)
        belong to the session's configuration, not to this type.

        Pure arithmetic, delegated entirely to ``src.units``, which owns every
        conversion in this system and has the round-trip tests to prove it.
        """
        output = motor_to_output_rpm(rpm, ratio)
        centripetal = output_rpm_to_g(output, radius)
        return cls(
            motor_rpm=rpm,
            output_rpm=output,
            hertz=motor_rpm_to_hertz(rpm, nominal_rpm, base_hz),
            g_load=centripetal,
            resultant_g=resultant_g(centripetal),
        )


@dataclass(frozen=True, slots=True)
class SpeedEnvelope:
    """Where the measured shaft speed may legitimately be, in MOTOR rpm, this tick.

    The runtime's own statement to ``tracking_error``, replacing the old "the
    setpoint is ramping, so stop judging" flag: between the setpoint that is
    commanded and where a drive following it at its own (derated) commissioned
    ramp could have got to by now. A shaft outside this band is not following
    the command, ramp or no ramp. ``low <= high`` always; both edges are whole
    rpm, rounded outwards by whoever builds one.
    """

    low: MotorRpm
    high: MotorRpm

    def distance(self, rpm: MotorRpm) -> int:
        """How far ``rpm`` lies outside the band, in motor rpm; 0 inside it."""
        if rpm < self.low:
            return int(self.low - rpm)
        if rpm > self.high:
            return int(rpm - self.high)
        return 0


@dataclass(frozen=True, slots=True)
class ZoneCounters:
    """How long the heart rate has spent in, above and below its target zone.

    The three durations that answer "did the session do what it was for?", kept
    as one object because a triple of ``Seconds`` passed around loose is a
    triple that gets reordered.

    These count only ticks with a trustworthy, fresh heart rate: time with no
    usable measurement belongs to none of the three, so they do not add up to
    the elapsed session time (see :attr:`total`). That gap is informative - a
    session whose counters sum to a fraction of its length was a session spent
    mostly blind.
    """

    in_zone: Seconds
    above_zone: Seconds
    below_zone: Seconds

    @property
    def total(self) -> Seconds:
        """The time actually accounted for: the sum of the three.

        Compared against ``TelemetrySnapshot.elapsed`` it gives the share of
        the session that had a usable heart rate at all. Computed here so the
        UI does not re-derive it - and so nobody mistakes it for elapsed time.
        """
        return Seconds(self.in_zone + self.above_zone + self.below_zone)


#: A heart rate older than this must not be presented as current, and must not
#: be regulated on. Four seconds because the pipeline delivers an 8 s median
#: refreshed at 1 Hz: a gap this long means at least four refreshes produced
#: nothing, which is a pipeline that has stopped working rather than jitter.
HEART_RATE_STALE_AFTER: Final[Seconds] = Seconds(4.0)


@unique
class RunMode(Enum):
    """What the machine is doing, in the operator's words. Derived, never stored.

    The console's top banner. Wire strings, like :class:`Phase`.
    """

    REPOS = "repos"
    """Nothing commanded: no session, or the last one is over and the output stage is off."""

    MANUEL = "manuel"
    """A manual session: the operator sets the target, the motion profiler walks to it."""

    SEANCE = "seance"
    """A programmed session is running its timeline."""

    ARRET = "arret"
    """A session is ending: the setpoint is on its way to zero, or the machine is
    being brought out of service. Never "stopped" - read the MEASURED speed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualView:
    """The manual session, as the operator's screen needs it.

    ``target`` is what the operator asked for, ``ceiling`` the highest target
    this occupancy accepts, ``min_run`` the slowest non-zero one. The setpoint
    actually in force is the snapshot's own ``setpoint``; the gap between the
    two is the ramp still to come, and ``ramp_eta`` says how long it will take.
    """

    occupancy: Occupancy
    target: SpeedView
    ceiling: SpeedView
    min_run: SpeedView

    ramping: bool
    """Whether the setpoint is still on its way to the target: "do not move your head"."""

    ramp_eta: Seconds | None
    """Seconds until the setpoint reaches the target at the motion limits; ``None`` unknown."""


#: A drive observation older than this is no longer evidence about the machine.
#: Two seconds is ten ticks of the 5 Hz control loop; by then the link, the
#: loop or the drive has a problem, and the displayed state is a memory.
DRIVE_STATUS_STALE_AFTER: Final[Seconds] = Seconds(2.0)


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """One complete picture of the session: what the UI renders and the log records.

    One object rather than a handful of separately published values, because
    values published separately are values that can disagree: a screen showing
    a heart rate from one instant next to a speed from another is describing a
    machine that never existed.

    **Every measurement here comes with its age, and that is the design.** A
    frozen screen is indistinguishable from a working one if it shows only
    values: the numbers look plausible, they are internally consistent, and
    they are minutes old. Ages make the freeze visible - to the operator
    through the display rule below, and to the log after the fact.

    **The display rule, which the UI must implement.** A heart rate whose age
    exceeds :data:`HEART_RATE_STALE_AFTER` must be greyed out, or replaced by a
    dash, or otherwise made to look unavailable - never rendered as a live
    reading. The same goes for the drive state past
    :data:`DRIVE_STATUS_STALE_AFTER`. :attr:`live_bpm` gives the UI the number
    it is permitted to show as current and returns ``None`` whenever there is
    none; :attr:`heart_rate_is_stale` and :attr:`drive_status_is_stale` answer
    the question directly. A stale number that looks live is the most dangerous
    failure this screen has: it is what makes an operator decide everything is
    fine about a machine nobody is actually watching.

    What is deliberately absent: the :class:`ControlDecision` that produced
    ``setpoint``. The snapshot records what was *commanded* and which verdict,
    if any, overrode the request; the request itself is logged separately, so
    ``phase`` and the setpoint appear here exactly once and cannot contradict a
    copy of themselves.
    """

    at: Monotonic
    """When this snapshot was taken. Every age below is measured against it."""

    wall_clock: UnixMillis
    """The same instant as a wall-clock stamp, for records that leave the machine.

    Both are present because they answer different questions: ``at`` is what
    ages and durations are computed from and survives a clock step, while a
    monotonic reading is meaningless to a server. The Pi has no RTC, so on a
    freshly booted machine this value can be wrong while ``at`` is still
    perfectly good - which is exactly why nothing here computes with it.
    """

    phase: Phase
    """Which part of the programme this snapshot describes."""

    elapsed: Seconds
    """Time since the session started."""

    remaining: Seconds
    """Time left in the programme; ``0.0`` once it is over."""

    heart_rate: HeartRateSample | None
    """The most recent sample, or ``None`` if none has ever arrived.

    ``None`` here and a ``None`` ``heart_rate_age`` mean the same thing and
    occur together; the display rule treats that case as stale, which is
    correct - a reading that has never happened is not a current one.
    """

    heart_rate_age: Seconds | None
    """Age of :attr:`heart_rate` at :attr:`at`; ``None`` exactly when it is ``None``.

    Carried rather than recomputed by each consumer, so that every renderer of
    this snapshot greys out the same readings and the age recorded in the log
    is the one the operator was shown.
    """

    target_bpm: Bpm | None
    """What the heart rate is being aimed at, or ``None`` in a phase with no target."""

    setpoint: SpeedView
    """The speed actually commanded to the drive, after any safety override."""

    measured: SpeedView
    """The speed the drive reports measuring (RFRD), in the same units.

    The field that speaks about motion. ``drive_state`` does not: FAULT,
    NOT_READY and COMM_LOST are all compatible with a centrifuge still turning.
    """

    setpoint_confirmed: bool
    """Whether the drive echoed back the setpoint that was written.

    A Modbus write response echoes the request, so a write to the wrong address
    is "acknowledged" while the speed reference never moves. The only evidence
    a write landed is LFRD read back and compared, every cycle
    (``src.motor.drive.DriveStatus.setpoint_echo_rpm``). ``False`` means the
    commanded speed above may be fiction.
    """

    drive_state: DriveState
    """The drive's own CiA402 state, as it last reported it.

    Before the first successful read, and whenever the link is down, this must
    be :data:`DriveState.COMM_LOST` - never ``NOT_READY``. The distinction is
    the whole point of that member: COMM_LOST says "the drive's state is
    unknown", which is not "the motor is stopped", whereas ``NOT_READY`` reads
    as stopped to anything that does not know better.
    """

    drive_status_age: Seconds | None
    """Age of that observation at :attr:`at`; ``None`` if the drive was never read."""

    current: Amperes | None
    """Measured motor current (LCR), or ``None`` if unknown.

    Worth a place on the screen because it is the only direct evidence of
    mechanical load: nameplate is 2.15 A, so a current climbing at constant
    speed means something is binding, not that the person is working harder.
    """

    fault: FaultReport | None
    """The drive's latched fault, decoded, or ``None`` when none applies.

    Singular because the drive's LFT register holds one code - the last fault -
    and inventing a list would be inventing a shape the hardware does not have.
    A whole :class:`~src.motor.drive.FaultReport` rather than just the enum, so
    the raw number travels with it: the operator is standing in front of the
    drive, and the mnemonic on its display is worth more than our prose.

    A fault may be present while the machine is still turning - a fault
    reaction is a ramp or a freewheel, not an instant stop.
    """

    safety: SafetyVerdict | None
    """The verdict currently standing, or ``None`` when no rule is asking for anything."""

    counters: ZoneCounters
    """Time in, above and below the target zone."""

    mode: RunMode = RunMode.REPOS
    """What the machine is doing, in the operator's words (the console banner)."""

    manual: ManualView | None = None
    """The manual session's target and limits, or ``None`` outside a manual session."""

    @property
    def safety_action(self) -> SafetyAction:
        """The standing demand, with no verdict reading as :data:`SafetyAction.NONE`.

        Saves every consumer an ``if`` over :attr:`safety`, and settles in one
        place what "no verdict" means.
        """
        if self.safety is None:
            return SafetyAction.NONE
        return self.safety.action

    @property
    def heart_rate_is_stale(self) -> bool:
        """Whether the heart rate is too old to present as live or to act on.

        A missing age counts as stale. Unknown freshness is never treated as
        fresh: that assumption is the one that gets somebody hurt.
        """
        if self.heart_rate_age is None:
            return True
        return self.heart_rate_age > HEART_RATE_STALE_AFTER

    @property
    def drive_status_is_stale(self) -> bool:
        """Whether the drive observation is too old to describe the machine.

        A missing age counts as stale, for the same reason as above.
        """
        if self.drive_status_age is None:
            return True
        return self.drive_status_age > DRIVE_STATUS_STALE_AFTER

    @property
    def live_bpm(self) -> Bpm | None:
        """The heart rate the UI is permitted to render as current, or ``None``.

        ``None`` means grey it out or show a dash - there is no usable, fresh,
        trustworthy number. Three gates are applied here: a sample exists, it
        is not stale, and its quality allows it to be used.

        The one gate this cannot apply is the sequence gate: ``seq`` only means
        something relative to the previous sample, which a single snapshot does
        not contain. Whoever builds snapshots must not carry forward a sample
        whose ``seq`` did not advance (see
        :meth:`HeartRateSample.is_new_evidence_after`) - that is stale evidence
        wearing a fresh timestamp.
        """
        if self.heart_rate is None or self.heart_rate_is_stale:
            return None
        return self.heart_rate.usable_bpm
