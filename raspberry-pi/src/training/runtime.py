"""The session runtime: one cooperative tick, and the order inside it.

This is the seam the web UI and the acquisition layer talk to. It owns no
transport, no DSP and no clock: it calls the injected
:class:`~src.motor.drive.DriveBackend`, is handed heart-rate metrics through
:meth:`TrainingRuntime.observe_ecg`, and takes ``now`` as a parameter on every
tick (contract rule 4). That is what lets a 30-minute session be walked end to
end, against a plant model, in a fraction of a second - which is the only
evidence this arrangement is safe before a person is inside the machine.

THE ORDER INSIDE THE TICK IS THE DESIGN
---------------------------------------

1. **Service the drive first.** A speed write goes out before anything else is
   decided, carrying the setpoint that is already in force. It is idempotent,
   it can never raise the speed, and its real job is to be the **keepalive**:
   if this loop ever stalls, the writes stop, the drive's own ``ttO``
   communication timeout expires, and the drive ramps the motor down by itself.
   That watchdog lives on the other side of the serial link on purpose. Any
   watchdog written in this file would run on the same interpreter, in the same
   event loop, behind the same state as the code it was meant to watch - it
   would die with it, and silently. Then the status is read, as one
   observation, so every field of the tick describes the same instant.

2. **Evaluate safety, and apply its verdict.** The verdict decides the
   setpoint. Nothing else may.

3. **Only if there is no verdict, apply the controller's demand.** Step 3 is
   subordinate to step 2 and there is no path that reverses them: the
   controller is reached from exactly two of the eight arms of one ``match``
   over :class:`~src.training.types.SafetyAction`, and both of those arms cap
   its output at what the verdict permits.

The reason is the vasovagal inversion, written out in
``src.training.types.ControlDecision.error_bpm``: a person beginning to faint
shows a *falling* heart rate, which the control law reads as "below the zone"
and answers by accelerating. It is not wrong to compute that. It is wrong to be
obeyed.

NO PATH LEAVES THE MOTOR RUNNING
--------------------------------

Every ending funnels through one of three mechanisms, and each is named on the
path that uses it:

* the **cooperative stop**: the setpoint is walked to zero on the software
  ramp, standstill is confirmed from RFRD, and only then is the run command
  removed (``SWITCH_ON`` then ``SHUTDOWN``). Removing it earlier is CiA402
  transition 8 on a turning centrifuge - the output stage drops and ~420 J of
  rotating mass coasts for minutes while the drive reports the reassuring
  ``READY``. See :class:`~src.motor.drive.ControlWord`.
* the **emergency zero**:
  :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`, synchronous
  so it works from a request handler, an ``except`` branch or ``atexit``. It
  zeroes the speed reference and deliberately leaves the run command in place,
  because the fastest stop this machine has is the drive's own commissioned
  ramp; a faster demand trips overvoltage into freewheel.
* **silence**: stop writing entirely and let ``ttO`` finish it. One-way. This
  is the only ending that does not depend on this process continuing to work
  correctly, which is why it is the answer to "this process may be the
  problem".

Note what follows from the third one: **after going silent this runtime never
sends another frame, including a read.** A real ATV320 resets ``ttO`` on any
frame it receives, reads included, so a "harmless" poll would restart the very
timeout the stop is now resting on.

WHAT THIS MODULE REFUSES TO ASSUME
----------------------------------

* **The drive's state at startup.** ``Restart=on-failure`` in
  ``scripts/anheart.service`` means a crashed process comes back to a machine it
  did not leave. If ETA reports ``OPERATION_ENABLED``, a previous process died with the
  motor commanded: zero the reference, ramp-stop, latch, refuse to start, and
  require a named operator acknowledgement. There is no automatic fault reset
  anywhere in this file, and nothing resumes by itself behind a latched
  verdict; what does resume by itself, and where that stops, is in the next
  section.
* **That an unchanged heart rate is a measured one.**
  ``src/signal_processing.py`` re-emits its previous metrics dict when
  extraction fails, so :meth:`TrainingRuntime.observe_ecg` carries the sample
  forward only when its ``seq`` strictly advanced - the gate
  ``src.training.types.TelemetrySnapshot.live_bpm`` says it cannot apply for
  itself.
* **That a status is fresh because it exists.** Past
  :data:`~src.training.types.DRIVE_STATUS_STALE_AFTER` the reported state
  becomes ``COMM_LOST`` - "unknown", never ``NOT_READY``, which reads as
  "stopped" to anything that does not know better.

ONE SESSION AT A TIME, NEVER RESUMED
------------------------------------

A :class:`TrainingRuntime` runs one session - a programme, or a manual session
(:meth:`TrainingRuntime.start_manual`) - and then reports ``FINISHED``: the
output stage shown off at standstill. From ``FINISHED`` (and only from there,
or from ``IDLE``) it may be armed again for a NEW session, from scratch, with
every start gate re-applied; that is what returns the local console to REPOS
after a stop. Nothing is carried into the new session but the facts about the
machine: the link, the last drive observation, the heart-rate tracker.
:meth:`TrainingRuntime.acknowledge` clears the latches so the machine can be
put back into service - it never resumes the session it cleared, and a runtime
that went silent or shut down can never be armed again.

Manual sessions follow the operator's target through the motion profiler
(:mod:`src.training.motion`), inside the same ``match`` over the verdict as the
control law: the verdict decides first, always. A programme's setpoint walks the
same profiler - the controller only chooses where to - and may not rise at all
while the heart rate is falling fast (the vasovagal gate,
``RuntimeLimits.falling_trend``).

A warning that is not latched (FREEZE, REDUCE) does not end the session: when
its cause clears, the setpoint follows the controller, or the operator's
target, again with nobody clicking; and the control law itself regulates in
both directions. Both are kept while the arm is turning. Neither may take the
arm out of a standstill: **a stopped arm never restarts by itself.** Once the
arm has moved in a session, a setpoint that comes back to zero without anybody
having asked for it ends the session (product decisions of 2026-10-05 and
2026-10-06, ``docs/securite.md``). ONE place sees every such zero, whichever
arm of the verdict wrote it: the end of :meth:`TrainingRuntime._command`, which
states the fact (:meth:`TrainingRuntime._note_standstill`) and leaves the
verdict to the supervisor's ``session_standstill`` rule, so that this ending is
reported, refused against and acknowledged like every other latched one. Not a
standstill "by itself": an operator's stop, an operator's manual target of zero
with no warning standing, the programme's own cooldown. Left as it was: a
warning before the arm has moved at all (BASELINE), where the motion that
follows is the programme's normal start.

The same goes for an arm the OPERATOR stopped, or that has not moved yet, in a
manual session: nothing may hold a target in waiting over it (ANH-178,
decisions of 2026-10-06). While anything holds a rise over a setpoint of zero
- any verdict, or with a person on board a heart rate that is not usable, whose
trend is unknown or that falls fast (:class:`RiseHold`) - a non-zero manual
target is refused (:meth:`TrainingRuntime.set_manual_target`), and one already
entered is taken back (:meth:`TrainingRuntime._withdraw_waiting_target`), as is
one whose first step the drive did not acknowledge. The operator asks again
once nothing holds, so neither a warning that lifts, nor an acknowledgement,
nor a heart rate that settles ever starts the arm. A rise held the same way
over an arm that is TURNING waits and resumes, as it always has.

And a FREEZE holds a speed, never a stop (ANH-175). Once somebody has asked
for the arm to come down - an operator's STOP at the console or from the site,
any ending already begun, a manual target of zero - the setpoint walks to zero
at the motion limits from the next tick, under a FREEZE exactly as with no
verdict (:meth:`TrainingRuntime._stop_under_freeze`), and a FREEZE that appears
during that walk does not pause it. A programme asks for the same thing by
itself when its timeline enters COOLDOWN, and is followed the same way, from
that tick (ANH-189). With no such request the FREEZE arm is what it was, and
every stronger verdict still decides first.

The idle console is read-only until it finds the drive enabled or turning with
no session running; then it stops it exactly as a start would
(:meth:`TrainingRuntime._judge_idle`).

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import math
from asyncio import CancelledError, Task, create_task, shield
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, assert_never, final

from src.clock import Clock
from src.geometry import MachineGeometry
from src.motor.drive import (
    DEFAULT_MAX_MOTOR_HZ,
    LFT_FAULT_CODES,
    UNVERIFIED_PARAMETERS,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveLimits,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    FaultReport,
    LimitViolation,
    StopUnconfirmed,
    UnexpectedState,
    check_limits,
    describe_fault,
    describe_violation,
)
from src.result import Err, Ok, Result
from src.task_completion import complete_owned
from src.training.drive_inspection import OpenFailed, StatusFailed, begin_inspection
from src.training.hr_control import (
    ControlInput,
    ControlPlan,
    Gains,
    HeartRateController,
    HeartRateRejection,
    HeartRateTracker,
    SpeedLimits,
    TrackerLimits,
    TrackerReading,
    Zone,
)
from src.training.idle_recovery import IdleLink as _IdleLink
from src.training.idle_recovery import UnknownEpisode
from src.training.motion import (
    DEFAULT_MOTION_LIMITS,
    MotionLimits,
    carry_after,
    next_setpoint,
    ramp_duration,
)
from src.training.plan import (
    COMMISSIONED_DECEL_S,
    MIN_RECOVERY_S,
    Program,
)
from src.training.safety import (
    RULE_COMMS_LOST,
    RULE_DRIVE_FAULT,
    AcknowledgeRefusal,
    AttestationRefusal,
    EmergencyStopStillLatched,
    EstopAttestation,
    GoSilentIsTerminal,
    MotionRefusal,
    NothingLatched,
    SafetyAcknowledgement,
    SafetyLimits,
    SafetyObservation,
    SafetySupervisor,
    Unattributed,
)
from src.training.tracking import SpeedFollower, follow_rate
from src.training.types import (
    DRIVE_STATUS_STALE_AFTER,
    ControlDecision,
    HeartRateSample,
    ManualView,
    Occupancy,
    Phase,
    RunMode,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    SpeedEnvelope,
    SpeedView,
    TelemetrySnapshot,
    ZoneCounters,
)
from src.units import (
    Bpm,
    BpmPerMinute,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    OutOfRange,
    OutputRpm,
    RawRegister,
    RpmPerSecond,
    Seconds,
    elapsed,
    hertz_to_motor_rpm,
    output_to_motor_rpm,
)

_logger: Final[logging.Logger] = logging.getLogger(__name__)
IdleLink = _IdleLink


# =========================================================================
# Rule ids this module raises on its own behalf
# =========================================================================
#
# The safety layer's rule set is its one open extension point (see
# src/training/safety.py on why a verdict's `rule` is a `str`), and these are
# conditions only the runtime can observe. They are stored-data keys: a
# dashboard, an alert and a session log get filed under them, so renaming one
# is a breaking change. tests/test_runtime.py asserts each against
# src.training.types.is_rule_id.

RULE_DRIVE_PRECOMMANDED: Final[str] = "drive_precommanded"
"""The drive was already enabled when this process arrived. See :meth:`TrainingRuntime.start`."""

RULE_TICK_EXCEPTION: Final[str] = "tick_exception"
"""Something inside the tick raised. This process's state is no longer trusted."""

RULE_ENABLE_UNCONFIRMED: Final[str] = "enable_unconfirmed"
"""The start sequence failed at the word that energises the output stage."""

RULE_DISABLE_REFUSED: Final[str] = "disable_refused"
"""The drive kept refusing the words that remove the output stage at standstill.

See :meth:`TrainingRuntime._settle`: bounded retries, then SHUTDOWN at confirmed
standstill, then silence.
"""


MIN_TREND_SAMPLES: Final[int] = 2
"""The fewest readings a trend can be fitted over at all: one point has no slope."""

DEFAULT_STANDSTILL_RPM: Final[MotorRpm] = MotorRpm(1)
"""Below this the shaft counts as stopped.

RFRD reports whole motor rpm, so one rpm is the smallest speed the drive can
report and there is nothing observable below it. This threshold gates removing
the run command, so it is deliberately the strictest value the instrument can
support rather than a comfortable margin.
"""


# =========================================================================
# Why a session ended
# =========================================================================


@unique
class EndReason(Enum):
    """Why the programme stopped. The values are stable log and wire identifiers.

    String values rather than ``auto()``, for the reason
    :class:`~src.training.types.Phase` gives: an ``auto()`` ordinal renumbers
    itself the day somebody reorders the members, and every stored session then
    means something else.

    ``SAFETY_VERDICT`` covers every rule in the safety layer - drive fault,
    comms loss, the vasovagal heart-rate drop, an overrun - because the verdict
    it is recorded with already names the rule, and a second, coarser
    vocabulary here could only disagree with the first.
    """

    PROGRAMME_COMPLETE = "programme_complete"
    # The nominal timeline reached its end: the only ending that is not an
    # interruption.

    OPERATOR_STOP = "operator_stop"
    # A human asked for the session to end. Deliberate and unhurried: the
    # setpoint walks down the software ramp like any cooldown.

    EMERGENCY_STOP = "emergency_stop"
    # Latched out of band, reference zeroed synchronously. Distinct from
    # SAFETY_VERDICT because no observation produced it and no tick had to
    # happen for it to take effect.

    SAFETY_VERDICT = "safety_verdict"
    # A safety rule demanded an end. The verdict names which one.

    TICK_EXCEPTION = "tick_exception"
    # The tick raised, so this process's state is not trusted and the stop is
    # handed to the drive's own timeout.

    SHUTDOWN = "shutdown"
    # The process is going away: SIGTERM, an ``except`` branch, ``atexit``.


@unique
class RuntimeState(Enum):
    """Where the runtime is in its one-session life. Derived, never stored twice."""

    IDLE = "idle"
    # No programme yet. The drive is still serviced while the link is open, so
    # a fault or a dead link is visible before anybody is in the machine.

    RUNNING = "running"
    # A programme is running its nominal timeline.

    ENDING = "ending"
    # An ending has begun: the setpoint is on its way to zero, or the machine
    # is down and the recovery window is still being monitored.

    FINISHED = "finished"
    # The programme is over, the output stage is off, and this object will
    # never issue another setpoint.


# =========================================================================
# What the runtime is configured with
# =========================================================================


def _require_positive(name: str, value: float) -> None:
    """Guard for the configuration records below.

    ``math.isfinite`` first, and deliberately: every comparison against NaN is
    false, so a bare ``value <= 0.0`` test *accepts* NaN. A NaN gear ratio would
    put a NaN on the operator's screen where a speed belongs, and a NaN slew
    rate is an unbounded speed change.
    """
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError(f"{name} must be finite and positive, got {value}")


@dataclass(frozen=True, slots=True)
class Subject:
    """Who is in the machine, and who is watching them.

    Deliberately thin. Every physiological threshold - the zone, the hard
    maximum, the critical rate, the subject's own maximum heart rate - lives on
    the :class:`~src.training.plan.TrainingProfile`, which validates them
    against each other. A second copy here would be a second place they can
    disagree.

    There is deliberately **no declared resting heart rate**. If BASELINE
    measures none, the WARMUP ramp has no origin and the machine stays at
    standstill, which is the correct outcome: a substituted resting rate is an
    invented vital sign, and the control law would then ramp a real person
    against a number nobody measured.
    """

    subject_id: str
    """Stable key for the session record. Prose is not this field's job."""

    operator: str
    """The attendant present at the start, by name.

    Used for the session record and as the first attendant presence stamp: a
    named human starting the session *is* evidence that somebody was there, so
    ``attendant_absent`` measures from that moment rather than treating a
    just-started session as unattended.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeLimits:
    """The machine facts the profile does not carry, plus the runtime's own budgets.

    ``slew`` and ``start_hysteresis_rpm`` are here rather than on the
    :class:`~src.training.plan.TrainingProfile` because they are properties of
    this installation, not of a training prescription - and they are here
    *together* on purpose. :class:`~src.training.hr_control.ControlPlan` refuses
    a configuration in which one control period of slew does not buy
    ``min_run_rpm`` of change (the machine could then never leave zero), and
    that cross-check only bites if one object owns both numbers.
    """

    slew: RpmPerSecond
    """Hardest rate of change of the setpoint, in either direction.

    Used by the control law *and* by this module's own descents, from this one
    field, so a safety ramp and a control ramp cannot end up with two different
    ideas of how fast the setpoint may move.
    """

    start_hysteresis_rpm: MotorRpm
    """Extra demand above ``min_run_rpm`` needed to leave zero."""

    gains: Gains = field(default_factory=Gains)
    """PI tuning and the control period. The defaults are this machine's.

    A ``default_factory`` rather than ``Gains()`` written inline: ruff can only
    prove a dataclass default is immutable when the class is defined in the same
    file, and ``Gains`` is imported. The factory says the same thing without
    asking for an exemption (contract rule 9: do not weaken a check locally).
    """

    standstill_rpm: MotorRpm = DEFAULT_STANDSTILL_RPM

    ramp_settle: Seconds = COMMISSIONED_DECEL_S
    """How long after the setpoint last moved the shaft may still be lagging.

    ``SafetyObservation.ramping`` suppresses ``tracking_error`` while the
    runtime is deliberately moving the setpoint, and the drive keeps following
    its own commissioned ramp for seconds after the last change - so the
    suppression has to outlast the change by about that ramp, or a perfectly
    normal ramp reports itself as a tracking failure.
    """

    status_stale_after: Seconds = DRIVE_STATUS_STALE_AFTER
    """Past this age a drive observation is a memory, and the state reads COMM_LOST."""

    max_motor_hz: Hertz = DEFAULT_MAX_MOTOR_HZ
    """The highest drive HSP this runtime will arm on, read back at every start.

    Defaults to 50.0 Hz because the owner keeps HSP at 50 Hz while the motor is
    UNCOUPLED from the arm. **Lower it before the arm is coupled**: see
    :data:`~src.motor.drive.DEFAULT_MAX_MOTOR_HZ`.
    """

    falling_trend: BpmPerMinute = BpmPerMinute(-20.0)
    """Below this heart-rate trend the setpoint may NOT rise, whatever the controller wants.

    The vasovagal gate. A collapse falls 60-120 bpm/min (the scripted one: 90,
    i.e. 1.5 bpm/s); before this gate the controller kept raising the setpoint
    for ~16 s of one (968 -> 1039 motor rpm while 145 -> 121 bpm) until
    ``hr_drop`` tripped, because a falling rate reads as "below the zone".
    -20 bpm/min is a third of a bpm per second: a heart recovering from a
    speed the controller itself just trimmed falls a few bpm/min, and the
    slope of five whole-bpm readings carries about +/-6 bpm/min of
    quantisation noise, so neither reaches it. A trend that is UNKNOWN also
    blocks a rise. The cost of a false block is one skipped increase - never a
    faster or a longer anything - so the threshold errs towards blocking.
    The safety verdict still dominates: this only restricts the controller.
    """

    trend_samples: int = 5
    """How many of the newest accepted readings the gate's trend is fitted over.

    Five (~5 s at the 1 Hz refresh) because the controller decides every 5 s
    and a collapse must show before its next decision: three seconds into the
    scripted fall a 5-point fit reads -66 bpm/min while the tracker's 10-point
    one still reads about -10. The price is noise - with +/-4 bpm of i.i.d.
    measurement noise about two increases in five are skipped - and a skipped
    increase is only a slower climb.
    """

    disable_attempts: int = 5
    """Consecutive refused disable sequences before :meth:`TrainingRuntime._settle` escalates.

    One second at 5 Hz. A word refused that many times in a row at confirmed
    standstill is a drive that will not take it, not a transient; retrying it
    every tick for the rest of the session (~480 frames in the failure matrix)
    left the output stage enabled with nobody told.
    """

    def __post_init__(self) -> None:
        """Refuse budgets that would silently disable the thing they bound."""
        _require_positive("slew", self.slew)
        _require_positive("ramp_settle", self.ramp_settle)
        _require_positive("status_stale_after", self.status_stale_after)
        _require_positive("max_motor_hz", self.max_motor_hz)
        _require_positive("-falling_trend", -self.falling_trend)
        if self.trend_samples < MIN_TREND_SAMPLES:
            raise ValueError(
                f"trend_samples must be at least {MIN_TREND_SAMPLES}, got {self.trend_samples}"
            )
        if self.disable_attempts < 1:
            raise ValueError(f"disable_attempts must be at least 1, got {self.disable_attempts}")
        if self.start_hysteresis_rpm < 0:
            raise ValueError(
                f"start_hysteresis_rpm must not be negative, got {self.start_hysteresis_rpm}"
            )
        if self.standstill_rpm < 1:
            raise ValueError(
                f"standstill_rpm must be at least 1, got {self.standstill_rpm}; RFRD reports "
                "whole rpm, so a threshold below one rpm can never be met and the run command "
                "would never be removed"
            )


# =========================================================================
# Why a start was refused
# =========================================================================
#
# A closed union, one variant per reason, each carrying what the operator
# screen needs to say. Adding a variant breaks every incomplete match at check
# time, which is the point.


@dataclass(frozen=True, slots=True)
class AlreadyStarted:
    """This runtime has already run a programme. A new session is a new object."""

    state: RuntimeState


@dataclass(frozen=True, slots=True)
class NotAttested:
    """Nobody has attested that a real emergency stop is wired in, this boot."""

    statement: str


@dataclass(frozen=True, slots=True)
class SafetyStanding:
    """A safety verdict is standing. It must be acknowledged by name first."""

    verdict: SafetyVerdict


@dataclass(frozen=True, slots=True)
class LimitsMismatch:
    """The programme's cardiac tiers are not the ones the supervisor holds.

    The failure this catches is a profile prepared for one person being run
    under another person's screening limits: both numbers are plausible, the
    session runs, and the ceiling protecting the occupant is somebody else's.
    """

    threshold: str
    profile_bpm: Bpm
    supervisor_bpm: Bpm


@dataclass(frozen=True, slots=True)
class PlanUnusable:
    """The profile and this machine's limits do not make a runnable control plan."""

    detail: str


@dataclass(frozen=True, slots=True)
class DriveUnavailable:
    """The drive could not be opened, read, or armed."""

    detail: str


@dataclass(frozen=True, slots=True)
class DriveParameterRefused:
    """A commissioned drive parameter, read back at arming, is not one this runtime accepts.

    ``violation`` names the parameter (``violation.parameter``) and carries the
    values read; ``limits`` is everything that was read, for the log. Nothing
    was energised: the check runs before the start sequence.
    """

    violation: LimitViolation
    limits: DriveLimits

    @property
    def detail(self) -> str:
        return describe_violation(self.violation)


@dataclass(frozen=True, slots=True)
class DrivePrecommanded:
    """The drive was already enabled: a previous process died with the motor commanded.

    Never a recoverable condition. The reference is zeroed before this is
    returned, a verdict is latched, and a named operator has to acknowledge it.
    """

    state: DriveState
    output_rpm: MotorRpm


@dataclass(frozen=True, slots=True)
class DriveInFault:
    """The drive has a fault latched. There is no automatic reset in this system."""

    report: FaultReport | None
    state: DriveState


@dataclass(frozen=True, slots=True)
class RecordStorageLow:
    """The session record (the local black box) has nowhere to go.

    ``free_bytes`` is the last measured free space under the records
    directory, below ``required_bytes``; ``None`` when it could not be
    measured at all, which refuses just the same: a session nobody can record
    is not started on the strength of a number nobody read.

    Nor on a number nobody read LATELY: ``stale_for`` is the age of that
    measurement when it is too old to be evidence (whoever measures is stuck,
    most likely on a disk that stopped answering), whatever it said.
    """

    free_bytes: int | None
    required_bytes: int
    where: str
    """The records directory, for the operator's message."""

    stale_for: Seconds | None = None
    """How old the measurement is, when that is why it is refused; else ``None``."""


type StartRefusal = (
    AlreadyStarted
    | NotAttested
    | SafetyStanding
    | LimitsMismatch
    | PlanUnusable
    | DriveUnavailable
    | DriveParameterRefused
    | DrivePrecommanded
    | DriveInFault
    | RecordStorageLow
)
"""Every way :meth:`TrainingRuntime.start` can refuse. Closed; match it nested."""

type ArmingGate = Callable[[], RecordStorageLow | None]
"""Asked at every arming, before the drive is touched. It reads memory and
returns at once: the gate runs inside the control task, where nothing may wait
(the free space is measured by the journal thread, never here)."""


# =========================================================================
# Drive failures, classified once
# =========================================================================


@unique
class Exchange(Enum):
    """Which kind of drive transaction a success or failure belongs to.

    The two are counted apart because they prove different things. A write
    acknowledgement echoes the request, so an acknowledged keepalive shows that
    *some* frame made a round trip - not that the drive's status is still being
    observed. Only a successful READ is proof that the drive is reachable and
    being watched; letting a write reset the read run would hide a status read
    that fails on every tick behind a keepalive that succeeds on every tick,
    and ``comms_lost`` would never fire.
    """

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class DriveFailure:
    """One drive error, reduced to the two things every call site here needs.

    ``detail`` is prose for a log line or a screen and is never branched on.
    ``output_unknown`` is the decision: two of the seven ``DriveError`` variants
    mean *the output stage may be energised and the motor may be turning*, and a
    caller that treats those like a transport timeout is wrong in the only
    direction that matters.
    """

    detail: str
    output_unknown: bool


def describe_drive_error(error: DriveError) -> DriveFailure:  # noqa: PLR0911  # one return per variant
    """Classify a drive error. Exhaustive, so a new variant fails the build here.

    One match for the whole module. The alternative - a match per call site -
    is seven places to update and seven places to get the ``output_unknown``
    reading wrong.
    """
    match error:
        case CommTimeout(after):
            return DriveFailure(
                detail=f"the drive did not answer within {after:.3f} s",
                output_unknown=False,
            )
        case BadResponse(detail):
            return DriveFailure(
                detail=f"the drive's answer made no sense: {detail}",
                output_unknown=False,
            )
        case UnexpectedState(expected, actual):
            return DriveFailure(
                detail=f"the drive is in {actual.name} and the operation needed {expected.name}",
                output_unknown=False,
            )
        case DriveFaulted(fault, raw_code):
            return DriveFailure(
                detail=f"the drive is in fault: {fault.mnemonic} (LFT {raw_code})",
                output_unknown=False,
            )
        case OutOfRange(quantity, value, low, high):
            return DriveFailure(
                detail=f"{quantity} {value} is outside its domain {low}..{high}",
                output_unknown=False,
            )
        case EnableUnconfirmed(detail, reference_zeroed, run_command_removed):
            # The word that energises the output stage may have landed even
            # though its reply did not, and LFRD may still hold a setpoint from
            # an earlier session - so "energised" can mean "turning".
            return DriveFailure(
                detail=(
                    f"the output stage may be energised: {detail} "
                    f"(reference zeroed: {reference_zeroed}, "
                    f"run command removed: {run_command_removed})"
                ),
                output_unknown=True,
            )
        case StopUnconfirmed(waited, last_output_rpm, detail):
            return DriveFailure(
                detail=(
                    f"the shaft could not be shown to have stopped: still {last_output_rpm} rpm "
                    f"after {waited:.1f} s ({detail}); the run command was left in place so the "
                    "drive's own ttO timeout ramps it down"
                ),
                output_unknown=True,
            )
    raise assert_never(error)


_LFT_CODE_BY_FAULT: Final[Mapping[DriveFault, RawRegister]] = MappingProxyType(
    {fault: code for code, fault in LFT_FAULT_CODES.items()}
)
"""Fault -> LFT code, inverted from the one table so the two cannot drift.

``tests/test_runtime.py`` asserts ``LFT_FAULT_CODES`` is injective, because an
inversion that silently collapsed two codes would make :func:`fault_report`
name a fault the drive is not showing.
"""


def fault_report(fault: DriveFault | None) -> FaultReport | None:
    """Recover the operator-facing report for a fault named by a ``DriveStatus``.

    ``DriveStatus`` carries ``fault: DriveFault | None``, while both
    :class:`~src.training.types.TelemetrySnapshot` and
    :class:`~src.training.safety.SafetyObservation` want a
    :class:`~src.motor.drive.FaultReport`, which also carries the raw LFT
    number. That number is not recoverable from the enum, so it is looked up
    through the inverse of the one code table and the report is rebuilt by
    :func:`~src.motor.drive.describe_fault` - the same function the drive layer
    used, so the mnemonic and the sentence are identical rather than merely
    similar.

    Returns ``None`` for a fault the table cannot number
    (:data:`~src.motor.drive.DriveFault.UNKNOWN`). That is the
    honest answer and not a shortcut: inventing a code here would print a
    number on the operator's screen that the drive is not showing them. The
    fault is still reported either way - ``drive_fault`` fires on
    ``DriveState.FAULT``, never on the presence of a report.
    """
    if fault is None:
        return None
    code = _LFT_CODE_BY_FAULT.get(fault)
    if code is None:
        return None
    return describe_fault(code)


def status_fault_report(status: DriveStatus) -> FaultReport | None:
    """The operator-facing report for a status, keeping the RAW LFT code when one was read.

    Preferred over :func:`fault_report` wherever a whole status is at hand:
    the raw number read off the drive is the one the operator compares with its
    display (LFT = 5 is SLF1, the Modbus loss the bench saw), and a code the
    table does not know still travels as itself, named UNKNOWN.
    """
    code = status.fault_code
    if code is None:
        return fault_report(status.fault)
    return describe_fault(code)


def _supervisor_for(
    clock: Clock, safety: SafetyLimits, shared: SafetySupervisor | None
) -> SafetySupervisor:
    """The runtime's supervisor: the shared one, checked, or a new one. Raises on a mismatch."""
    if shared is None:
        return SafetySupervisor(clock=clock, limits=safety)
    if shared.limits != safety:
        raise ValueError("the shared supervisor was built with different SafetyLimits")
    return shared


def motion_geometry(geometry: MachineGeometry, limit_radius: Metres | None) -> MachineGeometry:
    """The geometry the motion limits are evaluated through. Raises ``ValueError`` if unsafe.

    ``geometry`` itself when ``limit_radius`` is ``None``; otherwise the same
    machine with its radius moved out to ``limit_radius``, where the g-rate
    limit then binds. Refused when not finite or below ``geometry.radius``: a
    smaller radius would LOOSEN the limit at the radius the screen quotes it
    for. Startup only, nothing spinning.
    """
    if limit_radius is None:
        return geometry
    if not (math.isfinite(limit_radius) and limit_radius >= geometry.radius):
        raise ValueError(
            f"limit_radius {limit_radius} m must be finite and at least the reference "
            f"radius {geometry.radius} m: a smaller one would loosen the g-rate limit"
        )
    return replace(geometry, radius=limit_radius)


def motion_is_over(phase: Phase) -> bool:
    """Whether the programme will ask for motion again after this phase.

    Exhaustive rather than a set membership test, so a new phase fails the type
    check here - naming the phase nobody classified - instead of defaulting to
    "motion may still be commanded" and keeping the output stage energised
    through a phase that never needed it.

    This is what gates removing the run command, which is why BASELINE is on
    the other side of it: BASELINE sits at zero rpm and at standstill for
    minutes, and disabling there would put a three-word handshake inside the
    first tick that needs speed.
    """
    match phase:
        case Phase.COOLDOWN | Phase.RECOVERY | Phase.DONE:
            return True
        case Phase.BASELINE | Phase.WARMUP | Phase.HOLD:
            return False
    raise assert_never(phase)


# =========================================================================
# Records the runtime publishes
# =========================================================================


@dataclass(frozen=True, slots=True)
class Ending:
    """An ending in progress: why, how severe, and when it began.

    Frozen and recorded once. A second cause arriving later - a comms loss
    during an operator stop, say - does not replace it: the thing that ended the
    session is what somebody reviewing the record needs to see, and an ending
    that rewrote its own cause would make that record unreadable.
    """

    reason: EndReason
    action: SafetyAction
    """The severity that caused it; ``NONE`` for a normal or operator ending."""

    detail: str
    at: Monotonic


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    """What :meth:`TrainingRuntime.shutdown` actually achieved.

    A return value rather than ``None``, for the reason
    :class:`~src.motor.drive.EmergencyStopOutcome` exists: the callers are
    ``atexit``, a signal handler and an ``except`` branch, which cannot ask a
    follow-up question, and "the attempt was made" must not look like "no frame
    ever reached the wire".

    **Nothing here means the machine has stopped.** With STO jumpered there is
    no independent torque removal and the ramp takes seconds.
    """

    reason: str
    at: Monotonic

    silent: bool
    """Whether the runtime was already silent, in which case nothing was sent."""

    emergency: EmergencyStopOutcome | None
    """What the synchronous zero achieved, or ``None`` when it was not attempted."""

    closed: bool
    """Whether the backend reported a clean close."""

    output_disabled: bool
    """This runtime's belief about the output stage. ``False`` means assume it is live."""

    detail: str


IDLE_POLL_PERIOD: Final[Seconds] = Seconds(0.5)
"""How often an idle runtime reads the drive: 2 Hz, reads only.

Fast enough that the operator sees a shaft that somebody else set turning
within half a second, slow enough to leave the link almost idle (one
``read_status`` is four registers, about 140 ms on the bench link).
"""


# =========================================================================
# The manual session
# =========================================================================

MANUAL_SESSION_LIMIT: Final[Seconds] = Seconds(3600.0)
"""The longest a manual session runs before it ends itself on the ramp.

A manual session has no timeline, so ``session_overrun`` would otherwise judge
it against a programme of zero seconds. An hour is the ceiling the operator
works under; past it the setpoint walks to zero exactly like a STOP.
"""

BENCH_RECOVERY: Final[Seconds] = Seconds(0.0)
"""RECOVERY after a BENCH session: none, nobody was on board to recover.

With a person on board a manual session keeps :data:`MIN_RECOVERY_S` of
monitored RECOVERY, like any programme.
"""

FAULT_RESET_SETTLE: Final[Seconds] = Seconds(0.2)
"""Wait between ``FAULT_RESET`` and ``SHUTDOWN``: the bench console's own sequence.

Taken across ticks rather than slept: this runtime never waits on a clock.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualSession:
    """A manual session, as armed: who is on board, who runs it, how fast it may go.

    Frozen at the start and never changed while the machine turns: an
    occupancy that could be relaxed mid-rotation by a click is one that will
    be. The target is the one mutable thing, and it lives on the runtime.
    """

    occupancy: Occupancy
    operator: str
    ceiling: MotorRpm
    """The highest target accepted, motor rpm. From the occupancy, never above HSP."""

    cooldown: Seconds
    """How long an ending may take to reach standstill: the motion-limited
    descent from the ceiling, plus the drive's own commissioned ramp."""


@dataclass(frozen=True, slots=True)
class NoManualSession:
    """There is no manual session to take a target: start one first."""

    state: RuntimeState


@dataclass(frozen=True, slots=True)
class ManualEnding:
    """The manual session is ending. A target now would be a resumption."""

    detail: str


@dataclass(frozen=True, slots=True)
class TargetOutOfRange:
    """The target is not 0 and not inside ``[min_run, ceiling]``.

    Refused rather than clamped: an operator who asked for more than the
    ceiling must be told so, not quietly given less, and one who asked for less
    than the slowest running speed may well have meant zero.
    """

    requested: OutputRpm
    min_run: MotorRpm
    ceiling: MotorRpm


@unique
class RiseHold(Enum):
    """Why a manual setpoint may not rise although NO verdict stands.

    A verdict is not the only thing that holds a rise. With a person on board
    the heart rate has a say of its own, below any rule of the supervisor, and
    a drive can decline the write. None of these shows as a verdict, so a
    target entered over a stopped arm used to wait behind them exactly as it
    waited behind a verdict (ANH-178): ninety seconds, measured, on a heart
    rate coming down after an effort, and then the arm left with nobody
    clicking. String values because they are logged.
    """

    NO_HEART_RATE = "no_heart_rate"
    """Person on board, and no fresh, trustworthy heart rate to rise on."""

    TREND_UNKNOWN = "trend_unknown"
    """Person on board, and too few readings to say whether the rate is falling.

    Fewer than ``RuntimeLimits.trend_samples`` since the tracker began its
    history: the first seconds of an ECG, or those after a confirmed jump.
    """

    HEART_RATE_FALLING = "heart_rate_falling"
    """Person on board, and the rate falling faster than ``RuntimeLimits.falling_trend``."""

    WRITE_UNACKNOWLEDGED = "write_unacknowledged"
    """The drive did not acknowledge the step written to it: from standstill, the first step."""


type Holding = SafetyVerdict | RiseHold
"""What holds an arm at standstill against a manual target: a verdict, or a :class:`RiseHold`."""


@dataclass(frozen=True, slots=True)
class HeldAtStandstill:
    """Something holds the arm at standstill: a speed asked for now would wait for it.

    The target would be accepted, nothing would move, and the arm would leave
    by itself the moment the hold went, with nobody clicking at that instant
    (ANH-178). So a non-zero target is refused for as long as ANY verdict
    stands, latched or not, over a setpoint of zero, and for as long as the
    heart rate of a person on board lets nothing rise (:class:`RiseHold`). The
    operator asks again once nothing holds. A target of zero is never refused
    on this ground, and neither is any target while the arm turns.
    """

    by: Holding
    """What held the arm when the target was asked for; it is named to the operator."""


type ManualTargetRefusal = NoManualSession | ManualEnding | TargetOutOfRange | HeldAtStandstill
"""Every way :meth:`TrainingRuntime.set_manual_target` can refuse. Closed."""


@dataclass(frozen=True, slots=True)
class WithdrawnTarget:
    """A manual target this runtime took back before the arm had left standstill for it.

    Not a refusal: the target HAD been accepted, with nothing holding. Then
    something came to hold the arm at zero before the first step towards it,
    and a target left in place would have been followed when that hold went
    (see :meth:`TrainingRuntime._withdraw_waiting_target`). Reported once, so
    the console can say that the target on its screen is no longer in force.
    """

    target: MotorRpm
    """What the operator had asked for, motor rpm. Never zero."""

    by: Holding
    """What held the arm at standstill when the target was taken back."""


@dataclass(frozen=True, slots=True)
class ResetWhileCommanded:
    """Motion may still be commanded: a fault reset is only for a machine at rest."""

    state: RuntimeState
    phase: Phase


@dataclass(frozen=True, slots=True)
class ResetBehindVerdict:
    """A safety verdict stands. It is acknowledged, by name, before the drive is reset."""

    verdict: SafetyVerdict


@dataclass(frozen=True, slots=True)
class NoFaultToReset:
    """The drive, freshly read, is not in fault. ``None``: it has not been read at all."""

    state: DriveState | None


@dataclass(frozen=True, slots=True)
class ShaftStillTurning:
    """The drive reports the shaft turning: you cannot reset your way out of a spinning mass."""

    output_rpm: MotorRpm


@dataclass(frozen=True, slots=True)
class ResetForbidden:
    """The fault is not one the console may reset: power the drive down and inspect it.

    Short circuits, the drive's own hardware and memory, STO, overspeed - and
    any code that was not read or that the table cannot name - are not reset
    from a screen with a person anywhere near the machine
    (:attr:`~src.motor.drive.DriveFault.resettable`). ``report`` is ``None``
    when no LFT code was read.
    """

    report: FaultReport | None


@dataclass(frozen=True, slots=True)
class ResetUndelivered:
    """The reset word could not be written. Nothing changed that is known of."""

    detail: str


type FaultResetRefusal = (
    ResetWhileCommanded
    | ResetBehindVerdict
    | NoFaultToReset
    | ShaftStillTurning
    | ResetForbidden
    | ResetUndelivered
)
"""Every way :meth:`TrainingRuntime.fault_reset` can refuse. Closed."""


# =========================================================================
# The runtime
# =========================================================================


@final
class TrainingRuntime:
    """One training session, driven one tick at a time.

    **Mutable, single-threaded, and owned by one loop.** Everything except
    :meth:`trip_from_thread` runs on the event loop, and no method here awaits
    between reading a field and writing it, so no two entry points can
    interleave. That is deliberate rather than an oversight: a lock around this
    object would be a lock the acquisition thread could be made to wait on, and
    one the control loop could deadlock against with a motor commanded.

    The clock is injected and :meth:`tick` takes its own ``now``. The
    out-of-band entry points - :meth:`start`, :meth:`request_estop`,
    :meth:`presence_ping`, :meth:`acknowledge`, :meth:`shutdown` - have no tick
    to borrow an instant from and must not wait for one, so they read the
    injected clock, exactly as :class:`~src.training.safety.SafetySupervisor`
    does.
    """

    __slots__ = (
        "_applied_rpm",
        "_arming_gate",
        "_attendant_last_seen",
        "_clock",
        "_controller",
        "_counters",
        "_decision",
        "_descent_from",
        "_disable_failures",
        "_drive",
        "_enabled",
        "_end_reason",
        "_ending",
        "_failures",
        "_fault_reset_at",
        "_follower",
        "_geometry",
        "_idle_link",
        "_idle_next_at",
        "_idle_status",
        "_idle_status_at",
        "_inspection_unconfirmed",
        "_last_failure",
        "_last_sample",
        "_last_status",
        "_last_status_at",
        "_latched",
        "_limits",
        "_link_open",
        "_manual",
        "_manual_target",
        "_motion",
        "_motion_at",
        "_motion_from",
        "_motion_geometry",
        "_phase",
        "_previous_tick_at",
        "_program",
        "_recovery_from",
        "_resting_bpm",
        "_rise_held",
        "_safety_limits",
        "_session_over",
        "_setpoint_changed_at",
        "_shutdown",
        "_silent",
        "_snapshot",
        "_started_at",
        "_stop_requested",
        "_stopped_by",
        "_subject",
        "_supervisor",
        "_tracker",
        "_unknown_episode",
        "_warmup_satisfied",
        "_withdrawn",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        drive: DriveBackend,
        geometry: MachineGeometry,
        limits: RuntimeLimits,
        safety: SafetyLimits,
        tracker_limits: TrackerLimits | None = None,
        supervisor: SafetySupervisor | None = None,
        motion: MotionLimits = DEFAULT_MOTION_LIMITS,
        limit_radius: Metres | None = None,
        arming_gate: ArmingGate | None = None,
    ) -> None:
        """Build an idle runtime. Raises ``ValueError`` on a mismatched supervisor.

        ``supervisor`` lets a composition root share ONE supervisor between
        this runtime and the web control surface, so an e-stop latched by an
        HTTP handler is the same latch this runtime judges on its next tick -
        not a second supervisor nobody reads. Omitted, the runtime builds its
        own, which is what every test that does not wire a web layer wants.
        An injected supervisor must have been built with exactly ``safety``:
        the programme checks (``hard_max``/``critical`` equal to the
        supervisor's) read ``safety``, so a supervisor judging different
        thresholds would make those checks vouch for numbers nobody enforces.
        Raising is right here: this runs at startup with nothing spinning.

        ``motion`` bounds EVERY non-emergency setpoint change - a manual
        session's walk to its target, and a programme's controller demand,
        warm-up and cooldown alike: see :mod:`src.training.motion`. Only the
        safety descents (a verdict's REDUCE or RAMP_DOWN on a programme, at
        ``RuntimeLimits.slew``) and the emergency zero (QUICK_STOP, the drive's
        own commissioned ramp) move faster; see :meth:`_descend`.

        ``limit_radius`` is where the g-rate limit of ``motion`` is evaluated,
        when that is further out than ``geometry.radius`` (the reference radius
        every g on the screen is quoted at). The load changes ``radius / r``
        times faster at radius ``radius`` than at ``r``, so the anti-nausea
        g-dot is honoured by the whole rider only if it is judged at the
        furthest body part - the leg tip, 2.43 m on the CAD upper bound against
        a 1.5 m reference. ``None`` keeps today's behaviour (the reference
        radius). A value below ``geometry.radius`` is refused: it would loosen
        the limit at the very radius the screen promises it for.

        ``arming_gate`` is asked at every arming, programme or manual, once
        every other precondition has passed and before the drive is touched:
        the console passes the session record's free-space check. ``None``:
        nothing records, nothing to ask.
        """
        self._clock: Clock = clock
        self._arming_gate: ArmingGate | None = arming_gate
        self._drive: DriveBackend = drive
        self._geometry: MachineGeometry = geometry
        self._limits: RuntimeLimits = limits
        self._safety_limits: SafetyLimits = safety
        self._supervisor: SafetySupervisor = _supervisor_for(clock, safety, supervisor)
        self._tracker: HeartRateTracker = HeartRateTracker(tracker_limits)
        self._motion: MotionLimits = motion
        # The geometry every motion limit is evaluated through: the machine's,
        # with the radius moved out to `limit_radius` when one was given. Only
        # the g-rate term depends on the radius. Display keeps `_geometry`.
        self._motion_geometry: MachineGeometry = motion_geometry(geometry, limit_radius)
        # Where a drive following the command at its (derated) ramp would be:
        # the far edge of the envelope tracking_error judges against. Retuned
        # from ACC/dEC read back at every arming.
        self._follower: SpeedFollower = SpeedFollower(follow_rate(None, geometry.nominal_rpm))
        # Consecutive disable sequences the drive refused at standstill.
        self._disable_failures: int = 0

        # --- the session -------------------------------------------------
        self._program: Program | None = None
        self._subject: Subject | None = None
        self._controller: HeartRateController | None = None
        self._started_at: Monotonic | None = None
        # DONE before a programme exists: it is the only phase in which no
        # setpoint is ever issued, which is the truth about a machine nobody
        # has started. There is deliberately no IDLE phase in the vocabulary.
        self._phase: Phase = Phase.DONE
        self._ending: Ending | None = None
        self._end_reason: EndReason | None = None
        self._recovery_from: Monotonic | None = None
        self._stop_requested: str | None = None
        self._initialise_session_statements()
        self._warmup_satisfied: bool = False
        self._resting_bpm: Bpm | None = None
        # A manual session instead of a programme; at most one of the two.
        self._manual: ManualSession | None = None
        # What the operator asked for. Zeroed by every ending, and by anything
        # that comes to hold the arm at standstill; never raised by anything
        # but set_manual_target.
        self._manual_target: MotorRpm = MotorRpm(0)
        # The last target taken back that way, until the console has read it.
        self._withdrawn: WithdrawnTarget | None = None
        # What, other than a verdict, kept a manual setpoint from rising on the
        # last tick that followed the target: _follow_manual's own account. It
        # is read on such a tick only, after _follow_manual has rewritten it,
        # so nothing ever resets it.
        self._rise_held: RiseHold | None = None
        self._forget_motion_base()
        # An operator's fault reset between its two words, or None.
        self._fault_reset_at: Monotonic | None = None

        # --- what is believed about the drive ----------------------------
        self._link_open: bool = False
        # Loop-owned evidence: acquisition succeeded, but its status did not.
        self._inspection_unconfirmed: bool = False
        self._enabled: bool = False
        self._silent: bool = False
        # The setpoint believed to be in force. Advanced only when a write was
        # acknowledged, so a failed write leaves the previous value standing -
        # which is what it is - and the next keepalive re-asserts it.
        self._applied_rpm: MotorRpm = MotorRpm(0)
        self._last_status: DriveStatus | None = None
        self._last_status_at: Monotonic | None = None
        # Run lengths of failed exchanges, one per kind (see ``Exchange``).
        # Mutable, owned by this loop alone. A read run is reset only by a
        # successful read, a write run only by an acknowledged write, and the
        # safety layer judges the longest. Keyed by the enum rather than held
        # in one field per kind so that a new kind is counted, and judged, by
        # construction.
        self._failures: dict[Exchange, int] = dict.fromkeys(Exchange, 0)
        self._last_failure: DriveFailure | None = None
        self._setpoint_changed_at: Monotonic | None = None
        self._descent_from: Monotonic | None = None

        self._initialise_evidence()

        # --- idle, read-only polling (see _poll_idle) --------------------
        # Separate from _last_status on purpose: the safety observation never
        # sees an idle read, so an idle machine is judged exactly as it was
        # before polling existed, and a stale fault on an idle drive cannot
        # end a session that never started.
        self._initialise_idle_observation()
        self._snapshot: TelemetrySnapshot = self._build_snapshot(clock.monotonic())

    def _initialise_session_statements(self) -> None:
        """What this runtime states to the supervisor about the session itself.

        Facts only the runtime knows, each stated once and kept until the next
        start (:meth:`_reset_session`).
        """
        # What brought this session's arm to a standstill by itself, in words,
        # or None: stated once by _note_standstill, read by the supervisor.
        self._stopped_by: str | None = None
        # Whether THIS session's phase machine has reached DONE: stated once by
        # _advance_phase, read (with the setpoint in force) by session_overrun.
        self._session_over: bool = False

    def _forget_motion_base(self) -> None:
        """The motion profiler's time base, as a new session finds it: none."""
        # Since when the motion profiler's allowance accrues (see motion.py).
        self._motion_from: Monotonic | None = None
        # The tick the motion profiler last ran on (_motion_step), or None. A
        # stop that begins under a FREEZE trusts _motion_from only when that is
        # the previous tick (_stop_under_freeze).
        self._motion_at: Monotonic | None = None

    def _initialise_idle_observation(self) -> None:
        self._idle_link: IdleLink = IdleLink()
        self._idle_status: DriveStatus | None = None
        self._idle_status_at: Monotonic | None = None
        self._idle_next_at: Monotonic | None = None
        self._unknown_episode: UnknownEpisode = UnknownEpisode()

    def _initialise_evidence(self) -> None:
        """Evidence and output, as a runtime nobody has started holds them."""
        self._last_sample: HeartRateSample | None = None
        self._decision: ControlDecision | None = None
        self._counters: ZoneCounters = ZoneCounters(
            in_zone=Seconds(0.0), above_zone=Seconds(0.0), below_zone=Seconds(0.0)
        )
        self._attendant_last_seen: Monotonic | None = None
        self._previous_tick_at: Monotonic | None = None
        self._latched: SafetyVerdict | None = None
        self._shutdown: ShutdownReport | None = None

    # =====================================================================
    # Reads
    # =====================================================================

    @property
    def state(self) -> RuntimeState:
        """Where this runtime is. Derived from the fields, never stored twice."""
        if self._end_reason is None:
            idle = self._program is None and self._manual is None
            return RuntimeState.IDLE if idle else RuntimeState.RUNNING
        if self._phase is Phase.DONE and not self._enabled:
            return RuntimeState.FINISHED
        return RuntimeState.ENDING

    @property
    def supervisor(self) -> SafetySupervisor:
        """The ONE safety supervisor this runtime judges every tick with.

        Handed to the web control surface by the composition root, so that the
        web e-stop latches this instance synchronously, before any tick.
        """
        return self._supervisor

    @property
    def manual(self) -> ManualSession | None:
        """The manual session as armed, or ``None`` (no session, or a programme)."""
        return self._manual

    @property
    def manual_target(self) -> MotorRpm:
        """The operator's target, motor rpm. 0 outside a manual session and after any stop."""
        return self._manual_target

    @property
    def mode(self) -> RunMode:
        """What the machine is doing, in the operator's words. Derived from :attr:`state`."""
        state = self.state
        match state:
            case RuntimeState.IDLE | RuntimeState.FINISHED:
                return RunMode.REPOS
            case RuntimeState.ENDING:
                return RunMode.ARRET
            case RuntimeState.RUNNING:
                return RunMode.MANUEL if self._manual is not None else RunMode.SEANCE
        raise assert_never(state)

    @property
    def idle_link(self) -> IdleLink:
        """What the idle, read-only polling of the drive has seen."""
        return self._idle_link

    @property
    def heart_rate_trend(self) -> BpmPerMinute | None:
        """The tracker's trend of accepted readings, bpm per minute; ``None`` = unknown."""
        return self._tracker.rate

    @property
    def phase(self) -> Phase:
        """Which part of the programme this tick belongs to."""
        return self._phase

    @property
    def applied_rpm(self) -> MotorRpm:
        """The setpoint believed to be in force at the drive, in MOTOR rpm."""
        return self._applied_rpm

    @property
    def output_enabled(self) -> bool:
        """This runtime's belief about the output stage.

        A belief, not a measurement: after a failure that leaves the output
        state unknown this stays ``True``, because "we cannot tell" has to read
        as "assume the motor can turn".
        """
        return self._enabled

    @property
    def needs_stop_before_release(self) -> bool:
        """Unverified acquisition permits no write; an unreadable acquired drive is unknown."""
        return self.state is not RuntimeState.IDLE or self._inspection_unconfirmed

    @property
    def silent(self) -> bool:
        """Whether this runtime has stopped writing to the drive. One-way."""
        return self._silent

    @property
    def ending(self) -> Ending | None:
        """The ending in progress, or ``None``."""
        return self._ending

    @property
    def end_reason(self) -> EndReason | None:
        """Why the session ended, or ``None`` while it has not."""
        return self._end_reason

    @property
    def stop_reason(self) -> str | None:
        """The reason given by the FIRST operator stop request, or ``None``.

        The first one, not the latest: an operator who presses stop and then
        says something else about it has not changed why the session ended, and
        a record that kept the later reason would attribute the ending to
        whatever was said last.
        """
        return self._stop_requested

    @property
    def resting_bpm(self) -> Bpm | None:
        """The resting rate measured during BASELINE, or ``None`` if none was."""
        return self._resting_bpm

    @property
    def counters(self) -> ZoneCounters:
        """Time in, above and below the target zone."""
        return self._counters

    @property
    def last_failure(self) -> DriveFailure | None:
        """The most recent drive failure, classified, or ``None`` if there has been none."""
        return self._last_failure

    @property
    def drive_status(self) -> DriveStatus | None:
        """The last status the session path read, HOWEVER OLD; ``None`` before the first.

        For the session record only, which writes it next to its freshness.
        Nothing may decide anything from it: decisions read
        :meth:`_readable_status`, which forgets an observation once it is stale.
        """
        return self._last_status

    @property
    def standing(self) -> SafetyVerdict | None:
        """The verdict in force: the supervisor's, or this module's, whichever is worse.

        Ties go to this module's own latch, because it is the one the supervisor
        cannot re-derive from an observation - nothing but an acknowledgement
        will ever produce it again.
        """
        return self._worst(self._latched, self._supervisor.standing)

    @property
    def standing_action(self) -> SafetyAction:
        """The standing demand, with "nobody is asking for anything" reading as NONE."""
        verdict = self.standing
        return SafetyAction.NONE if verdict is None else verdict.action

    def snapshot(self) -> TelemetrySnapshot:
        """The last published picture of the session. Pure; safe to read between ticks."""
        return self._snapshot

    @staticmethod
    def _worst(*verdicts: SafetyVerdict | None) -> SafetyVerdict | None:
        """The most severe of the given verdicts, earliest argument winning a tie."""
        present = tuple(verdict for verdict in verdicts if verdict is not None)
        return max(present, key=lambda verdict: verdict.action, default=None)

    # =====================================================================
    # The startup gate
    # =====================================================================

    def confirm_estop_wiring(self, operator: str) -> Result[EstopAttestation, AttestationRefusal]:
        """Record that a named operator attests a real emergency stop is wired in.

        Delegated verbatim to the supervisor, which owns the statement and logs
        the claim against a name and two clocks. It is a precondition of
        :meth:`start`, and while the drive's STO input is jumpered it is the only
        evidence that any stop exists which does not depend on software.
        """
        return self._supervisor.confirm_estop_wiring(operator)

    def require_estop_confirmed(self) -> Result[EstopAttestation, MotionRefusal]:
        """The gate itself: pure, silent, pollable by a UI."""
        return self._supervisor.require_estop_confirmed()

    # =====================================================================
    # Feeding the metric stream
    # =====================================================================

    def observe_ecg(
        self,
        now: Monotonic,
        seq: int,
        quality: SignalQuality,
        heart_rate: Bpm | None,
    ) -> Result[TrackerReading, HeartRateRejection]:
        """Hand one ECG metric refresh to the tracker.

        ``quality`` arrives as an enum: the wire string is parsed exactly once,
        at the acquisition boundary, through
        :meth:`~src.training.types.SignalQuality.from_metric`, which is total and
        never optimistic. Behind that boundary nothing in this system sees a
        grade as a ``str`` (contract rules 2 and 6).

        **The sequence number is the freshness gate, not the timestamp.**
        ``src/signal_processing.py`` re-emits its previous metrics dict when
        extraction fails, so a repeated ``seq`` arrives wearing a perfectly
        current instant. The sample is carried into telemetry only when its
        ``seq`` strictly advanced - the one gate
        :attr:`~src.training.types.TelemetrySnapshot.live_bpm` cannot apply for
        itself, because a single snapshot has no previous sample to compare
        against.

        The tracker's rejection is returned rather than swallowed: "the rate was
        rejected" is four materially different situations to whoever is watching
        the screen - the pipeline stopped, the electrodes are the problem, the
        detector is, or one reading was an artefact.
        """
        sample = HeartRateSample(bpm=heart_rate, quality=quality, seq=seq, at=now)
        # Read the watermark BEFORE observing: observe() advances it on any new
        # evidence, accepted or not.
        is_new = sample.is_new_evidence_after(self._tracker.last_seq)
        outcome = self._tracker.observe(sample)
        if is_new:
            self._last_sample = sample
        return outcome

    def presence_ping(self) -> Monotonic:
        """Record that a human is watching, and return the instant recorded.

        The UI calls this; ``attendant_absent`` escalates from its age. Reads the
        injected clock because a browser request does not arrive on a tick
        boundary and must not wait for one.
        """
        stamp = self._clock.monotonic()
        self._attendant_last_seen = stamp
        return stamp

    def note_presence(self, at: Monotonic) -> None:
        """Record an attendant ping that arrived elsewhere (the web surface) at ``at``.

        Never moves the record backwards: an old ping forwarded late must not
        make a present attendant look absent, nor a stale one look present.
        """
        seen = self._attendant_last_seen
        if seen is None or at > seen:
            self._attendant_last_seen = at

    def trip_from_thread(self, rule: str, action: SafetyAction, detail: str = "") -> None:
        """Raise a safety demand from a thread that is not the control loop.

        Forwarded to the supervisor unchanged, and for its reasons: the
        acquisition thread must never be made to wait on this loop, so the call
        takes no lock, reads no clock and does no I/O. It latches, and the
        control loop picks it up within one tick.
        """
        self._supervisor.trip_from_thread(rule, action, detail)

    # =====================================================================
    # Starting
    # =====================================================================

    async def start(
        self, program: Program, subject: Subject
    ) -> Result[TelemetrySnapshot, StartRefusal]:
        """Arm the machine for one programme, or refuse and say why.

        The checks run in order of increasing commitment: nothing touches the
        drive until every precondition that can be judged without it has passed,
        so a refusal for a mis-specified programme never leaves a half-armed
        drive behind.

        **The drive's state is read, never assumed.** ``Restart=on-failure``
        in ``scripts/anheart.service`` guarantees that a crashed process comes
        back to a machine it did not leave; if ETA reports
        ``OPERATION_ENABLED`` then the motor is commanded right now, possibly
        with a person in the machine.

        The output stage is energised here, with a zero reference, rather than
        lazily at the first non-zero setpoint. Three reasons: an enable that is
        going to fail fails now, before anybody is under load; a zero reference
        with the run command in place is this machine's documented resting
        state, which is exactly what
        :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`
        deliberately leaves behind; and the alternative puts a three-word
        handshake inside the first tick that needs speed.

        Refuses rather than raising: an operator changing a setting on a screen
        must get a sentence, not a traceback.
        """
        refusal = self._refuse_start(program)
        if refusal is not None:
            return Err(refusal)
        now = self._clock.monotonic()
        self._reset_session()
        inspected = await self._open_and_inspect(now)
        if isinstance(inspected, Err):
            return Err(inspected.error)
        armed = await self._arm(now, inspected.value)
        if armed is not None:
            return Err(armed)
        self._program = program
        self._subject = subject
        self._started_at = now
        self._phase = Phase.BASELINE
        # A named operator starting the session is evidence that somebody was
        # there. attendant_absent would otherwise measure from the session
        # start - the same instant - so this records the fact rather than
        # changing the arithmetic.
        self._attendant_last_seen = now
        self._controller = self._build_controller(program)
        self._snapshot = self._build_snapshot(now)
        _logger.info(
            "programmed session started: rev=%d total=%.0f s",
            program.source_rev,
            program.total_duration_s,
        )
        return Ok(self._snapshot)

    async def start_manual(
        self, occupancy: Occupancy, operator: str, ceiling: MotorRpm
    ) -> Result[TelemetrySnapshot, StartRefusal]:
        """Arm the machine for a manual session, target 0, or refuse and say why.

        The same gates and the same arming as :meth:`start` - attestation, no
        standing verdict, the drive read and never assumed, its limits read
        back, the output stage energised with a zero reference - and then
        NOTHING moves until :meth:`set_manual_target` asks for a speed.

        ``occupancy`` is declared here and frozen for the session. With
        :attr:`~src.training.types.Occupancy.BENCH` (nobody on board) the
        heart-rate rules are off and every other rule is live; with
        ``OCCUPIED`` a fresh, trustworthy heart rate is required to start and
        every rule applies. ``ceiling`` comes from the configuration for that
        occupancy (``LocalConfig.ceiling_for``) and is refused above the HSP
        this runtime arms against.
        """
        refusal = self._refuse_arming() or self._refuse_manual(occupancy, operator, ceiling)
        if refusal is not None:
            return Err(refusal)
        now = self._clock.monotonic()
        self._reset_session()
        inspected = await self._open_and_inspect(now)
        if isinstance(inspected, Err):
            return Err(inspected.error)
        armed = await self._arm(now, inspected.value)
        if armed is not None:
            return Err(armed)
        descent = ramp_duration(ceiling, MotorRpm(0), self._motion, self._motion_geometry)
        self._manual = ManualSession(
            occupancy=occupancy,
            operator=operator,
            ceiling=ceiling,
            # Checked by _refuse_manual: the descent from the ceiling exists.
            cooldown=Seconds((0.0 if descent is None else descent) + COMMISSIONED_DECEL_S),
        )
        self._started_at = now
        self._phase = Phase.HOLD
        self._attendant_last_seen = now
        self._snapshot = self._build_snapshot(now)
        _logger.info(
            "manual session started: occupancy=%s ceiling=%d motor rpm",
            occupancy.value,
            ceiling,
        )
        return Ok(self._snapshot)

    def _refuse_manual(
        self, occupancy: Occupancy, operator: str, ceiling: MotorRpm
    ) -> StartRefusal | None:
        """What refuses a manual session in particular, before the drive is touched."""
        if not operator.strip():
            return PlanUnusable("a manual session must name the operator running it")
        hsp = hertz_to_motor_rpm(
            self._limits.max_motor_hz, self._geometry.nominal_rpm, self._geometry.base_hz
        )
        minimum = self._motion.min_run
        if not minimum <= ceiling <= hsp:
            return PlanUnusable(
                f"the ceiling {ceiling} motor rpm is outside [{minimum}, {hsp}]: below the "
                "slowest running speed nothing can be asked for, and above the drive HSP "
                "this runtime arms against the drive would refuse to follow"
            )
        if ramp_duration(MotorRpm(0), ceiling, self._motion, self._motion_geometry) is None:
            return PlanUnusable(
                "the motion limits are too slow for this geometry to ever reach the ceiling: "
                "one control interval never pays for a whole rpm"
            )
        heart_rate = self._tracker.usable(self._clock.monotonic())
        if occupancy is Occupancy.OCCUPIED and heart_rate is None:
            return PlanUnusable(
                "a person on board needs a fresh, trustworthy heart rate before anything turns"
            )
        return None

    def set_manual_target(self, target: OutputRpm) -> Result[MotorRpm, ManualTargetRefusal]:
        """Set the manual target, in OUTPUT rpm; the motion profiler walks the setpoint to it.

        Never a setpoint: the value is only a destination, reached tick by tick
        at the motion limits, and every safety verdict still overrides it. 0,
        or ``[min_run, ceiling]`` at the motor shaft; anything else is refused
        rather than clamped. Refused once the session is ending: a target then
        would be a resumption nobody asked for.

        Refused too, when it is not zero, while anything holds a setpoint of
        zero (:class:`HeldAtStandstill`): a verdict, or the heart rate of a
        person on board (:meth:`_heart_rate_hold`). The arm would not move
        now, and would move later, when the hold went or the verdict was
        acknowledged, with nobody clicking at that moment. A verdict is judged
        on what the last tick left in force, which is all there is between two
        ticks: one that is about to lift still refuses, and one that is about
        to appear is dealt with by the tick that sees it
        (:meth:`_withdraw_waiting_target`). The heart rate is read as it is at
        this instant, and the tick judges it again before the first step. An
        arm that is turning takes a target as it always has.
        """
        manual = self._manual
        if manual is None:
            return Err(NoManualSession(self.state))
        if self._ending is not None or self._stop_requested is not None:
            return Err(ManualEnding("the manual session is ending; start a new one to move again"))
        minimum = self._motion.min_run
        if not math.isfinite(target) or target < 0.0:
            return Err(TargetOutOfRange(requested=target, min_run=minimum, ceiling=manual.ceiling))
        motor = output_to_motor_rpm(target, self._geometry.ratio)
        if motor != 0 and not minimum <= motor <= manual.ceiling:
            return Err(TargetOutOfRange(requested=target, min_run=minimum, ceiling=manual.ceiling))
        if motor != 0 and self._applied_rpm == 0:
            held: Holding | None = self.standing
            if held is None:
                held = self._heart_rate_hold(self._clock.monotonic())
            if held is not None:
                return Err(HeldAtStandstill(held))
        self._manual_target = motor
        _logger.info("manual target set to %d motor rpm (%.2f output rpm)", motor, target)
        return Ok(motor)

    def _heart_rate_hold(self, now: Monotonic) -> RiseHold | None:
        """Why the heart rate lets a manual setpoint rise no further at ``now``, or ``None``.

        The one statement of the gate, read by :meth:`_follow_manual` in the
        tick and by :meth:`set_manual_target` at the console, so that what is
        refused and what is held cannot drift apart. Nobody on board: no gate.
        A person on board: no rise without a fresh, trustworthy heart rate,
        nor while the vasovagal gate is closed (:meth:`_increase_permitted`,
        the very test a programme's rise passes). The DECISION is those two
        tests and nothing else, exactly as before this method had a name; the
        trend is read a second time only to say which of its two ways the
        gate is closed. None of this is a verdict.
        """
        if not self._occupied():
            return None
        if self._tracker.usable(now) is None:
            return RiseHold.NO_HEART_RATE
        if self._increase_permitted():
            return None
        trend = self._tracker.recent_rate(self._limits.trend_samples)
        return RiseHold.TREND_UNKNOWN if trend is None else RiseHold.HEART_RATE_FALLING

    def take_withdrawn_target(self) -> WithdrawnTarget | None:
        """The manual target taken back since the last call, or ``None``. Read once.

        For the console: a target it showed as accepted is no longer in force,
        and the operator has to be told why (:class:`WithdrawnTarget`). Reading
        it forgets it, so one withdrawal is said once.
        """
        withdrawn = self._withdrawn
        self._withdrawn = None
        return withdrawn

    def manual_rise_hold(self) -> RiseHold | None:
        """Why the heart rate would hold a manual rise at this instant, or ``None``. Reads only.

        For the console, which has to show it BEFORE a target is typed: the
        answer :meth:`set_manual_target` gives a non-zero target over a stopped
        arm with no verdict standing, and the gate :meth:`_follow_manual`
        applies to a setpoint that would rise, read from the one statement of
        both (:meth:`_heart_rate_hold`) and at this instant, as the refusal
        is. ``None`` unless a manual session is running (:attr:`mode`), since
        there is no target to type otherwise, and always ``None`` with nobody
        on board. A verdict is not this method's business: it is on every
        screen already.
        """
        if self.mode is not RunMode.MANUEL:
            return None
        return self._heart_rate_hold(self._clock.monotonic())

    async def fault_reset(self) -> Result[None, FaultResetRefusal]:
        """Reset a drive fault, on an operator's explicit request. Never called automatically.

        Only on a machine at rest (phase DONE, setpoint zero), only once every
        OTHER verdict has been acknowledged by name, only when the drive -
        freshly read - says FAULT and shows the shaft stopped, and only for a
        fault the console may reset at all (``DriveFault.resettable``: never a
        short circuit, the drive's own hardware, STO, overspeed, or a code that
        was not read or is not known - those need the power off and an
        inspection). The
        ``drive_fault`` verdict itself is the one exception, and necessarily:
        its rule re-latches on every tick for as long as the drive shows the
        fault, so it can only be acknowledged AFTER the reset - which the next
        start then insists on. Then ``FAULT_RESET``
        now and ``SHUTDOWN`` :data:`FAULT_RESET_SETTLE` later, on a later tick:
        the bench console's own sequence. Nothing is energised and nothing
        moves: the next start arms the drive from scratch.
        """
        now = self._clock.monotonic()
        checked = self._check_fault_reset(now)
        if isinstance(checked, Err):
            return Err(checked.error)
        readable = checked.value
        written = await self._drive.write_command(ControlWord.FAULT_RESET)
        if isinstance(written, Err):
            return Err(ResetUndelivered(describe_drive_error(written.error).detail))
        # A drive in FAULT has no torque, and FAULT_RESET leaves it in
        # SWITCH_ON_DISABLED: the output stage is off, which is what lets an
        # ending that could not remove the run command from a faulted drive
        # finish.
        self._enabled = False
        self._fault_reset_at = now
        _logger.warning("drive fault reset by the operator (LFT was %s)", readable.fault_code)
        return Ok(None)

    def _check_fault_reset(self, now: Monotonic) -> Result[DriveStatus, FaultResetRefusal]:
        """Every precondition of a fault reset; the fresh faulted status when all hold."""
        state = self.state
        if self._shutdown is not None or self._silent:
            return Err(ResetUndelivered("this runtime no longer writes to the drive"))
        commanded = self._phase is not Phase.DONE or self._applied_rpm != 0
        if state is RuntimeState.RUNNING or commanded:
            return Err(ResetWhileCommanded(state=state, phase=self._phase))
        standing = self.standing
        if standing is not None and standing.rule != RULE_DRIVE_FAULT:
            return Err(ResetBehindVerdict(standing))
        return self._check_faulted_drive(now)

    def _check_faulted_drive(self, now: Monotonic) -> Result[DriveStatus, FaultResetRefusal]:
        """The drive's half of the reset preconditions: faulted, stopped, resettable."""
        _, readable, _ = self._shown_status(now)
        if readable is None or readable.state is not DriveState.FAULT:
            return Err(NoFaultToReset(None if readable is None else readable.state))
        if abs(readable.output_rpm) >= self._limits.standstill_rpm:
            return Err(ShaftStillTurning(readable.output_rpm))
        report = status_fault_report(readable)
        if report is None or not report.fault.resettable:
            # An unread or unnamed code is not evidence the fault is benign.
            return Err(ResetForbidden(report))
        return Ok(readable)

    async def _finish_fault_reset(self, now: Monotonic) -> None:
        """The second word of an operator's fault reset, once it has settled."""
        started = self._fault_reset_at
        if started is None or elapsed(started, now) < FAULT_RESET_SETTLE:
            return
        self._fault_reset_at = None
        if self._silent:
            return
        written = await self._drive.write_command(ControlWord.SHUTDOWN)
        if isinstance(written, Err):
            _logger.warning(
                "fault reset: SHUTDOWN after the reset failed: %s",
                describe_drive_error(written.error).detail,
            )

    def _refuse_arming(self) -> StartRefusal | None:
        """What refuses ANY start, programme or manual, before the drive is touched.

        A runtime that has FINISHED may be armed again - that is what puts the
        console back to REPOS after a stop - but never one that is still
        running or ending, one that has shut down (its link is released), or
        one that went silent (it never sends another frame).
        """
        state = self.state
        if self._shutdown is not None or self._silent:
            return AlreadyStarted(state)
        attested = self._supervisor.require_estop_confirmed()
        if isinstance(attested, Err):
            return NotAttested(attested.error.statement)
        standing = self.standing
        if standing is not None:
            return SafetyStanding(standing)
        if state not in (RuntimeState.IDLE, RuntimeState.FINISHED):
            return AlreadyStarted(state)
        # Last, so the operator is first told what only they can clear.
        return None if self._arming_gate is None else self._arming_gate()

    def _reset_session(self) -> None:
        """Forget the previous session before arming a new one. Link and tracker survive.

        Called only after :meth:`_refuse_arming` passed, so nothing is turning,
        nothing is latched and the output stage is off. What survives is what
        describes the machine rather than the session: the link, the drive
        observation and its failure runs, the heart-rate tracker, the
        attendant's last ping and the tick clock.
        """
        self._program = None
        self._subject = None
        self._controller = None
        self._manual = None
        self._manual_target = MotorRpm(0)
        self._withdrawn = None
        self._forget_motion_base()
        self._fault_reset_at = None
        self._started_at = None
        self._phase = Phase.DONE
        self._ending = None
        self._end_reason = None
        self._recovery_from = None
        self._stop_requested = None
        self._stopped_by = None
        self._session_over = False
        self._warmup_satisfied = False
        self._resting_bpm = None
        self._decision = None
        self._descent_from = None
        self._disable_failures = 0
        self._counters = ZoneCounters(
            in_zone=Seconds(0.0), above_zone=Seconds(0.0), below_zone=Seconds(0.0)
        )

    def _refuse_start(self, program: Program) -> StartRefusal | None:
        """Every precondition that can be judged without touching the drive."""
        arming = self._refuse_arming()
        if arming is not None:
            return arming
        mismatch = self._limits_mismatch(program)
        if mismatch is not None:
            return mismatch
        try:
            self._build_control_plan(program)
        except ValueError as error:
            return PlanUnusable(str(error))
        return None

    def _limits_mismatch(self, program: Program) -> LimitsMismatch | None:
        """Refuse a programme whose cardiac tiers are not the supervisor's own.

        The profile and the supervisor both carry ``hard_max_bpm`` and
        ``critical_bpm`` because they answer to different owners - one is the
        prescription, the other is the screening - and this is the one place they
        are required to agree. Without it, a profile cloned for a different
        person runs under limits nobody re-checked.
        """
        profile = program.profile
        pairs = (
            ("hard_max_bpm", profile.hard_max_bpm, self._safety_limits.hard_max_bpm),
            ("critical_bpm", profile.critical_bpm, self._safety_limits.critical_bpm),
        )
        for name, from_profile, from_supervisor in pairs:
            if from_profile != from_supervisor:
                return LimitsMismatch(
                    threshold=name, profile_bpm=from_profile, supervisor_bpm=from_supervisor
                )
        return None

    def _build_control_plan(self, program: Program) -> ControlPlan:
        """Adapt the profile plus this machine's limits into a control plan.

        Raises ``ValueError`` from the plan's own validators. The one bridge that
        turns that into a refusal is :meth:`_refuse_start`, so holding a plan
        means the limits have been checked and nothing below re-checks them.
        """
        profile = program.profile
        return ControlPlan(
            zone=Zone(low=profile.zone_low_bpm, high=profile.zone_high_bpm),
            speed=SpeedLimits(
                min_run_rpm=profile.min_run_rpm,
                max_rpm=profile.max_rpm,
                # The profile's own property, which floors rather than rounds:
                # rounding a ceiling up permits a speed above the fraction that
                # was actually authorised.
                warmup_max_rpm=profile.warmup_rpm_ceiling,
                slew=self._limits.slew,
                start_hysteresis_rpm=self._limits.start_hysteresis_rpm,
            ),
            warmup=profile.warmup_max_s,
            gains=self._limits.gains,
        )

    def _build_controller(self, program: Program) -> HeartRateController:
        """Build the one controller this session uses.

        One instance for the whole session, never rebuilt per phase, and that is
        what makes phase transitions bumpless: the standing demand *is* the
        integral state of the velocity-form PI, so a new controller at a boundary
        would reset the output to zero and step the setpoint. The controller
        clears its own previous error and carry on a phase change, which is the
        reset that is wanted - the output stays where it was, and the next
        increment re-seeds instead of differencing two errors that belong to
        different questions.
        """
        return HeartRateController(self._build_control_plan(program), initial_rpm=self._applied_rpm)

    async def _open_and_inspect(self, now: Monotonic) -> Result[DriveStatus, StartRefusal]:
        """Acquire the link and read the drive once. Assumes nothing about either."""
        return await self._own_inspection(now, create_task(self._inspect_for_start(now)))

    async def _own_inspection[E](
        self, now: Monotonic, inspection: Task[Result[DriveStatus, E]]
    ) -> Result[DriveStatus, E]:
        try:
            return await shield(inspection)
        except CancelledError:
            await complete_owned(create_task(self._finish_cancelled_inspection(now, inspection)))
            raise

    async def _finish_cancelled_inspection[E](
        self, now: Monotonic, inspection: Task[Result[DriveStatus, E]]
    ) -> None:
        """Own the actual initial reply and stop observed motion before the caller exits."""
        status = await inspection
        if isinstance(status, Ok):
            await self._judge_idle(now, status.value)
            if self._enabled:
                await self.shutdown("initial inspection cancelled")
        elif self._inspection_unconfirmed:
            await self.shutdown("unreadable initial inspection cancelled")
        elif self._unknown_episode.failures > 0:
            self._end_unknown_episode(
                now,
                "initial inspection cancelled with unknown drive state after possible traffic; "
                "no further frames will be sent. Verify standstill before an operator restarts "
                "this process; no session will resume automatically",
                address_proven=self._drive.acquisition_evidence.address_proven,
            )

    async def _inspect_for_start(self, now: Monotonic) -> Result[DriveStatus, StartRefusal]:
        opened = await begin_inspection(self._drive, reopen=True)
        if isinstance(opened, OpenFailed):
            self._link_open = False
            failure = self._note_failure(Exchange.READ, opened.error)
            self._inspection_failed(now, opened)
            return Err(DriveUnavailable(f"the drive link could not be opened: {failure.detail}"))
        self._link_open = True
        status = await opened.read_status()
        if isinstance(status, StatusFailed):
            self._link_open = False
            failure = self._note_failure(Exchange.READ, status.error)
            self._inspection_failed(now, status)
            return Err(DriveUnavailable(f"the drive could not be read: {failure.detail}"))
        self._inspection_unconfirmed = False
        self._unknown_episode = UnknownEpisode()
        self._enabled = status.status.state is DriveState.OPERATION_ENABLED
        self._note_success(Exchange.READ)
        self._last_status = status.status
        self._last_status_at = now
        return Ok(status.status)

    async def _arm(self, now: Monotonic, status: DriveStatus) -> StartRefusal | None:
        """Judge what was read, then energise the output stage with a zero reference."""
        if status.state is DriveState.OPERATION_ENABLED:
            return await self._refuse_precommanded(now, status)
        if status.fault_present:
            # No automatic fault reset, anywhere. "Reset it and see" with a
            # person inside the machine is how a short circuit becomes a fire.
            return DriveInFault(report=status_fault_report(status), state=status.state)
        refused = await self._check_drive_limits()
        if refused is not None:
            return refused
        try:
            return await self._energise(now)
        except CancelledError:
            self._enabled = True
            await complete_owned(create_task(self.shutdown("arming cancelled")))
            raise

    async def _check_drive_limits(self) -> StartRefusal | None:
        """Read tFr/HSP/LSP/ACC/dEC back and refuse limits this machine cannot arm on.

        Read at every arming rather than trusted from a commissioning note: HSP
        is the ceiling that holds when this software is wrong, and LSP = 0 is
        what makes every zero reference in this module a stop. Runs before a
        single word is written, so a refusal leaves nothing energised.

        ttO and SLL cannot be read yet (their Modbus addresses are unverified),
        so every arming says so in the log instead of reading a guessed address.
        """
        read = await self._drive.read_limits()
        if isinstance(read, Err):
            failure = self._note_failure(Exchange.READ, read.error)
            return DriveUnavailable(f"the drive's limits could not be read: {failure.detail}")
        self._note_success(Exchange.READ)
        limits = read.value
        # The drive's own ramp, as commissioned today: what the tracking
        # envelope's follower is derated from (src/training/tracking.py).
        self._follower.retune(follow_rate(limits, self._geometry.nominal_rpm))
        checked = check_limits(limits, self._limits.max_motor_hz)
        if isinstance(checked, Err):
            refusal = DriveParameterRefused(violation=checked.error, limits=limits)
            _logger.error(
                "arming refused on %s: %s (read: %s)",
                checked.error.parameter.value,
                refusal.detail,
                limits.describe(),
            )
            return refusal
        _logger.info(
            "arming: drive limits %s, accepted against a %.1f Hz ceiling",
            limits.describe(),
            self._limits.max_motor_hz,
        )
        for unverified in UNVERIFIED_PARAMETERS:
            _logger.warning("arming: %s", unverified.describe())
        return None

    async def _refuse_precommanded(self, now: Monotonic, status: DriveStatus) -> StartRefusal:
        """A previous process died with the motor commanded. Stop it, latch, refuse.

        See :meth:`_stop_found_running`, which the idle poll shares.
        """
        await self._stop_found_running(
            now,
            status,
            (
                f"the drive was already in OPERATION_ENABLED at {status.output_rpm} rpm: a "
                "previous process died with the motor commanded"
            ),
        )
        return DrivePrecommanded(state=status.state, output_rpm=status.output_rpm)

    async def _stop_found_running(self, now: Monotonic, status: DriveStatus, found: str) -> None:
        """A drive this runtime did not command is enabled or turning: zero, latch, end.

        Contract rule 8: never assume the drive's state. The reference goes to
        zero and the run command is *kept*: that is the fastest stop this
        machine has, because a stop commanded faster than the commissioned ramp
        trips overvoltage into freewheel, and removing the run command from a
        turning shaft is the same hazard by another route. The ramp-stop word
        and then the shutdown word are issued later, by :meth:`_settle`, once
        RFRD shows standstill. A shaft turning with the output stage NOT
        enabled (coasting) cannot be braked by anything but that stage, which
        this runtime will not energise: the zero is still written, so nothing
        resumes, and the operator is told. Either way the verdict latches: a
        named operator acknowledges it before any session runs, and nothing
        here resumes motion.
        """
        self._enabled = status.state is DriveState.OPERATION_ENABLED
        try:
            await self._exchange_write_speed(now, MotorRpm(0))
        except CancelledError:
            await complete_owned(create_task(self.shutdown("startup recovery cancelled")))
            raise
        self._latch(
            now,
            RULE_DRIVE_PRECOMMANDED,
            SafetyAction.QUICK_STOP,
            (
                f"{found}. The reference has been zeroed and the run command left in place so "
                "the drive ramps it down; a named operator must acknowledge this before any "
                "session runs"
            ),
        )
        self._begin_ending(now, EndReason.EMERGENCY_STOP, self._latched)

    async def _energise(self, now: Monotonic) -> StartRefusal | None:
        """Zero the reference, then SHUTDOWN -> SWITCH_ON -> ENABLE_OPERATION.

        The reference is written first, unconditionally, so that energising the
        output stage cannot start motion at a setpoint left over from an earlier
        session. The three words are the commissioning notes' start sequence in
        that order; a caller that skips one gets ``UnexpectedState`` rather than
        a drive that happens to tolerate it.
        """
        failure = await self._exchange_write_speed(now, MotorRpm(0))
        if failure is None:
            for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
                failure = await self._exchange_command(word)
                if failure is not None:
                    break
        if failure is None:
            self._enabled = True
            return None
        return await self._abandon_enable(now, failure)

    async def _abandon_enable(self, now: Monotonic, failure: DriveFailure) -> StartRefusal:
        """Back out of a failed start, pessimistically about what landed.

        ``EnableUnconfirmed`` means the word that energises the output stage may
        already have been transmitted even though its reply was lost - so the
        motor may be turning right now. Reading "the enable failed" as "nothing
        is enabled" is wrong in the only direction that matters, so the output is
        recorded as live, a verdict is latched, and the ending machine brings it
        down over the following ticks.
        """
        await self._exchange_write_speed(now, MotorRpm(0))
        detail = f"the drive could not be armed: {failure.detail}"
        if failure.output_unknown:
            self._enabled = True
            self._latch(now, RULE_ENABLE_UNCONFIRMED, SafetyAction.QUICK_STOP, failure.detail)
            self._begin_ending(now, EndReason.EMERGENCY_STOP, self._latched)
        return DriveUnavailable(detail)

    # =====================================================================
    # The tick
    # =====================================================================

    async def tick(self, now: Monotonic) -> TelemetrySnapshot:
        """One cooperative step: service the drive, judge safety, then command.

        The body is wrapped because **an exception here is an exit path**, not a
        bug report: a failure unwinding through this method leaves the motor
        commanded while the traceback travels. So the handler fails closed first
        - latch, zero the reference synchronously, go silent - and only then lets
        the exception continue.

        It is re-raised rather than swallowed because a tick that raised has told
        us this process's state is not to be trusted, and the honest consequence
        is for the loop to stop: once it does, no keepalive goes out and the
        drive's own ``ttO`` timeout ramps the motor down. A runtime that quietly
        carried on would be a runtime deciding a person's speed from state it
        has just admitted it cannot vouch for.

        ``BaseException`` and not ``Exception``: a cancellation, a ``SIGINT`` or
        a ``SystemExit`` arriving mid-tick leaves the motor exactly as commanded
        as an ``IndexError`` does.
        """
        try:
            await self._tick(now)
        except BaseException as error:
            _logger.exception("the control tick raised; failing closed")
            self._fail_closed(now, f"{type(error).__name__}: {error}")
            raise
        return self._snapshot

    async def _tick(self, now: Monotonic) -> None:
        """The tick proper. Read the module docstring before reordering anything."""
        previous = self._previous_tick_at
        interval = self._interval(now)
        # 0. Idle only: read the drive, write nothing. A no-op once a session
        #    has been started, because from then on step 1 owns the link.
        await self._poll_idle(now)
        # 1. THE KEEPALIVE FIRST.
        await self._service_drive(now)
        await self._finish_fault_reset(now)
        self._advance_phase(now)
        # 2. Safety, and its verdict.
        verdict = self._supervisor.evaluate(self._observe(now, self._envelope(now)))
        standing = self._worst(self._latched, verdict)
        # 3. The command. The verdict decides; it reaches the controller only
        #    through the two arms that cap it.
        await self._command(now, standing, previous)
        await self._settle(now)
        self._accumulate(now, interval)
        self._snapshot = self._build_snapshot(now)

    def _interval(self, now: Monotonic) -> Seconds | None:
        """Time since the previous tick, or ``None`` on the first one.

        ``None`` rather than zero: the first tick has no interval, and "no
        previous tick" is a different fact from "no time passed", which is the
        distinction a fabricated zero throws away.

        Worth being honest about the strength of that: the only consumer today
        is :meth:`_accumulate`, which adds nothing for either value, so
        substituting ``0.0`` would change no observable behaviour. The type is
        the point - the next consumer of an interval must decide what "there was
        no previous tick" means for it rather than inheriting a zero that
        happened to be harmless here.
        """
        previous = self._previous_tick_at
        self._previous_tick_at = now
        return None if previous is None else elapsed(previous, now)

    async def _service_drive(self, now: Monotonic) -> None:
        """Keepalive, then one status read. Nothing is sent once silent.

        The keepalive is a speed write carrying the setpoint already in force.
        It cannot raise the speed, it is idempotent, and it re-asserts our intent
        against a write that was acknowledged but never landed. A command word
        would be the wrong choice: ``ENABLE_OPERATION`` is idempotent only
        *while* enabled, and issued from ``SWITCHED_ON`` it would energise the
        output stage - a keepalive must not be able to start a machine.

        After going silent this sends nothing, reads included: a real ATV320
        resets ``ttO`` on any frame it receives, so a poll would restart the
        timeout the stop is now resting on.

        Once a session has armed the drive the zero keepalive continues after
        it has FINISHED, as it always has: the drive has been talking to a
        master, and falling silent would arm its communication-loss fault (SLF)
        on a machine nobody is driving. Only a runtime that never armed is
        read-only (:meth:`_poll_idle`).

        Only the status read may count as proof the drive is reachable: the
        keepalive's acknowledgement resets the *write* run and nothing else, so
        a read that fails on every tick reaches ``comms_lost`` however well the
        writes are going.
        """
        if self._silent or not self._link_open:
            return
        await self._exchange_write_speed(now, self._applied_rpm)
        status = await self._drive.read_status()
        if isinstance(status, Err):
            self._note_failure(Exchange.READ, status.error)
            return
        self._note_success(Exchange.READ)
        self._last_status = status.value
        self._last_status_at = now

    async def _poll_idle(self, now: Monotonic) -> None:
        """Read the drive at :data:`IDLE_POLL_PERIOD` while no session was ever started.

        **Reads only - until the drive is found enabled or turning.** A machine
        nobody has started is shown, not driven: the operator sees measured
        rpm, state, current and the raw LFT code, and the drive hears nothing
        but ETA/LFRD/RFRD/LCR reads. ``DriveBackend.open`` is itself
        contractually read-only (it proves the addressing with a read). The one
        exception is a drive this console did not command that is enabled or
        turning: that is stopped at once (:meth:`_judge_idle`), because a
        read-only poll feeds the drive's ``ttO`` and would otherwise keep it
        turning for as long as the console sat idle.

        Skipped once a programme exists (the keepalive owns the link then),
        once this runtime is silent (a frame after silence restarts ``ttO``),
        and while the session path has the link open (a start that was
        refused after opening it keeps servicing it through step 1).

        A failure is recorded in :attr:`idle_link` and the link is re-opened on
        the next poll, because the ATV320 driver latches a lost link until
        ``open`` is called again. Total no-frame outages remain ordinary
        retryable idle failures. Possibly traffic-fed unknown episodes are
        bounded separately by the supervisor's communication-loss count.
        """
        armed = self._program is not None or self._manual is not None
        if armed or self._silent or self._link_open or self._shutdown is not None:
            return
        due = self._idle_next_at
        if due is not None and now < due:
            return
        self._idle_next_at = Monotonic(now + IDLE_POLL_PERIOD)
        status = await self._own_inspection(now, create_task(self._read_idle(now)))
        if isinstance(status, Ok):
            await self._judge_idle(now, status.value)

    async def _read_idle(self, now: Monotonic) -> Result[DriveStatus, DriveError]:
        opened = await begin_inspection(self._drive, reopen=not self._idle_link.open)
        if isinstance(opened, OpenFailed):
            self._idle_failed(opened.error)
            self._inspection_failed(now, opened)
            return Err(opened.error)
        self._idle_link = replace(self._idle_link, open=True)
        status = await opened.read_status(self._clock)
        if isinstance(status, StatusFailed):
            self._idle_failed(status.error)
            self._inspection_failed(now, status)
            return Err(status.error)
        self._inspection_unconfirmed = False
        self._unknown_episode = UnknownEpisode()
        self._enabled = status.status.state is DriveState.OPERATION_ENABLED
        link = self._idle_link
        self._idle_link = replace(
            link,
            reads=link.reads + 1,
            consecutive_failures=0,
            last_latency=status.latency,
        )
        self._idle_status = status.status
        self._idle_status_at = now
        return Ok(status.status)

    def _inspection_failed(self, now: Monotonic, inspection: OpenFailed | StatusFailed) -> None:
        self._idle_next_at = Monotonic(now + IDLE_POLL_PERIOD)
        possible_frames = inspection.after.possible_frames > inspection.before.possible_frames
        self._unknown_episode = self._unknown_episode.failed(possible_frames=possible_frames)
        if self._unknown_episode.failures == 0:
            return
        self._enabled = True
        self._inspection_unconfirmed = (
            self._inspection_unconfirmed or inspection.after.address_proven
        )
        self._snapshot = self._build_snapshot(now)
        if self._unknown_episode.failures < self._supervisor.limits.comms_lost_failures:
            return
        detail = (
            f"drive state remains unknown after {self._unknown_episode.failures} automatic "
            "recovery attempts that may have refreshed its watchdog; recovery exhausted. "
            "No further frames will be sent. Verify the machine has stopped before an "
            "operator restarts this process; no session will resume automatically"
        )
        self._end_unknown_episode(now, detail, address_proven=inspection.after.address_proven)

    def _end_unknown_episode(self, now: Monotonic, detail: str, *, address_proven: bool) -> None:
        self._link_open = address_proven
        self._latch(now, RULE_COMMS_LOST, SafetyAction.GO_SILENT, detail)
        self._begin_ending(now, EndReason.SAFETY_VERDICT, self._latched)
        self._go_silent(now, detail)
        self._snapshot = self._build_snapshot(now)

    async def _judge_idle(self, now: Monotonic, status: DriveStatus) -> None:
        """A drive found enabled, or turning, while nothing is commanded: stop it now.

        Reading and showing it was not enough, and it was the worst of both: a
        drive left OPERATION_ENABLED at 900 motor rpm by a crashed process kept
        turning for the whole idle period (60 s in the failure matrix, 120 s on
        the panel) with no verdict, and the read-only poll was what kept the
        drive's own ``ttO`` fed, so not even the drive stopped it. Only an
        operator's START noticed.

        So the idle console now does exactly what a start does on the same
        evidence (:meth:`_stop_found_running`): it takes the link over (from
        here on the keepalive and the status read of step 1 run every tick, so
        the stop is watched and the output stage is removed at standstill by
        :meth:`_settle`), zeroes the reference, latches ``drive_precommanded``
        and ends. It never resumes anything, and nothing starts until a named
        operator acknowledges it.
        """
        turning = abs(status.output_rpm) >= self._limits.standstill_rpm
        if status.state is not DriveState.OPERATION_ENABLED and not turning:
            return
        _logger.critical(
            "idle console: the drive is %s at %d motor rpm with no session running",
            status.state.name,
            status.output_rpm,
        )
        self._link_open = True
        self._last_status = status
        self._last_status_at = now
        await self._stop_found_running(
            now,
            status,
            (
                f"the idle console found the drive {status.state.name} at {status.output_rpm} "
                "motor rpm with no session running: something else commanded it (a process "
                "that died, another master)"
            ),
        )

    def _idle_failed(self, error: DriveError) -> None:
        """Record a failed idle exchange and mark the link for re-opening."""
        failure = describe_drive_error(error)
        link = self._idle_link
        self._idle_link = replace(
            link,
            open=False,
            failures=link.failures + 1,
            consecutive_failures=link.consecutive_failures + 1,
            last_error=failure.detail,
        )
        _logger.warning(
            "idle drive read failed (%d consecutive): %s",
            self._idle_link.consecutive_failures,
            failure.detail,
        )

    # =====================================================================
    # The phase machine
    # =====================================================================

    def _advance_phase(self, now: Monotonic) -> None:
        """Settle the phase for this tick, before any evidence is judged.

        Settled once and then used by the safety observation, the control input
        and the snapshot alike, so those three cannot disagree about which part
        of the programme the tick belongs to.

        An operator stop is honoured here rather than in :meth:`request_stop`, so
        the ending begins on a tick boundary with a real instant and the
        controller sees ``COOLDOWN`` for the whole of it.

        This is also where a session is seen to be OVER: the first tick on
        which its phase machine says ``DONE``. That fact is kept until the next
        start, because the phase does not keep it: a verdict arriving at rest
        after a programme completed by itself opens an ending
        (:meth:`_begin_ending`), and the phase is ``RECOVERY`` again over a
        session that finished minutes ago. Only a started session can be over,
        so a tick that falls while a start is still arming states nothing.
        """
        if self._stop_requested is not None:
            self._begin_ending(now, EndReason.OPERATOR_STOP, None)
        ending = self._ending
        if ending is not None:
            self._phase = self._ending_phase(now, ending)
        elif self._program is not None:
            self._phase = self._nominal_phase(now, self._program)
        elif self._manual is not None:
            self._phase = self._manual_phase(now)
        if self._phase is Phase.DONE and self._started_at is not None:
            self._session_over = True
        self._latch_resting_rate(now)

    def _manual_phase(self, now: Monotonic) -> Phase:
        """HOLD for as long as the operator drives, until :data:`MANUAL_SESSION_LIMIT`.

        HOLD because it is the phase in which motion is commanded and every
        rule is live. At the limit the session ends itself exactly as a STOP
        does - target zero, the motion-limited ramp, standstill - rather than
        running into ``session_overrun``, which would latch.
        """
        if self._session_elapsed(now) < MANUAL_SESSION_LIMIT:
            return Phase.HOLD
        _logger.warning("the manual session reached its %.0f s limit", MANUAL_SESSION_LIMIT)
        self._begin_ending(now, EndReason.PROGRAMME_COMPLETE, None)
        return Phase.COOLDOWN

    def _nominal_phase(self, now: Monotonic, program: Program) -> Phase:
        """Where the programme's own timeline says this session is.

        The timeline is the *nominal* answer, and this method is allowed to leave
        it in one direction only: WARMUP ends early once the heart rate has
        reached the zone, which is what ``warmup_max_s`` means by "max". Leaving
        early lengthens HOLD rather than shortening the session, because HOLD
        still ends where the timeline says it does.
        """
        phase, _, _ = program.phase_at(self._session_elapsed(now))
        if phase is Phase.DONE and self._applied_rpm != 0:
            # The timeline is over but the motion-limited cooldown is not: DONE
            # would switch the heart-rate rules off over a turning arm.
            return Phase.RECOVERY
        if phase is Phase.WARMUP:
            if self._warmup_satisfied:
                return Phase.HOLD
            reached = self._tracker.usable(now)
            if reached is not None and reached >= program.profile.zone_low_bpm:
                self._warmup_satisfied = True
                _logger.info("warmup ended early: the zone was reached")
                return Phase.HOLD
        if phase is Phase.DONE and self._end_reason is None:
            self._end_reason = EndReason.PROGRAMME_COMPLETE
            _logger.info("the programme reached its end")
        return phase

    def _ending_phase(self, now: Monotonic, ending: Ending) -> Phase:
        """COOLDOWN until the machine is down, then RECOVERY, then DONE.

        RECOVERY is kept on an aborted session on purpose. It is physiologically
        the most informative phase and the one with the highest vasovagal risk,
        and every heart-rate rule stays live through it: monitoring does not stop
        because the machine did.

        COOLDOWN ends on *confirmed* standstill, or on a deadline of the
        profile's own cooldown length. The deadline covers the two cases where
        confirmation can never arrive - a runtime that has gone silent, and a
        shaft that will not stop - neither of which may leave the session parked
        in COOLDOWN forever with the heart-rate rules aimed at a machine nobody
        is still deciding for.
        """
        recovery_from = self._recovery_from
        if recovery_from is None:
            settled = self._applied_rpm == 0 and self._at_standstill(now)
            if not settled and elapsed(ending.at, now) <= self._cooldown_s():
                return Phase.COOLDOWN
            self._recovery_from = now
            return Phase.RECOVERY
        if elapsed(recovery_from, now) < self._recovery_s():
            return Phase.RECOVERY
        if self._applied_rpm != 0 and not self._silent:
            # Still descending at the motion limits: not DONE, which would stop
            # supervising the heart rate over a turning arm. A silent runtime
            # never writes again, so its belief cannot reach zero; ttO owns it.
            return Phase.RECOVERY
        return Phase.DONE

    def _latch_resting_rate(self, now: Monotonic) -> None:
        """Keep the resting rate up to date for as long as BASELINE lasts.

        Updated on every BASELINE tick that has a fresh, trustworthy rate, so the
        value standing when BASELINE ends is the last one actually measured at
        standstill - the settled rate, rather than an average that includes the
        first agitated minute. Never invented: if BASELINE measured nothing this
        stays ``None``, the WARMUP ramp has no origin, and the machine stays at
        standstill.
        """
        if self._phase is not Phase.BASELINE:
            return
        measured = self._tracker.usable(now)
        if measured is not None:
            self._resting_bpm = measured

    # =====================================================================
    # Safety
    # =====================================================================

    def _observe(self, now: Monotonic, envelope: SpeedEnvelope | None) -> SafetyObservation:
        """Everything the rules are allowed to judge, for this instant.

        Note what is absent, and that it is absent by construction rather than by
        omission: no desired speed, no ``ControlDecision``, no error. There is no
        field through which the control law's opinion can reach a rule, which is
        the whole reason the two layers exist. ``commanded_g`` is the applied
        setpoint - a measurement - rendered through the geometry, and
        ``envelope`` is derived from it and from the drive's ramp.
        ``stopped_by`` is a fact about the applied setpoint too (it came back
        to zero, and nobody had asked): see :meth:`_note_standstill`.

        ``session_over`` makes one rule quieter and no other
        (``session_overrun`` stops judging a session that has ended), so it is
        made of two facts and both must hold: this session's phase machine has
        reached ``DONE`` (:meth:`_advance_phase`), and the setpoint in force is
        zero. ``elapsed`` goes on counting from the start until the next start;
        it is this statement, not a stopped clock, that says there is no
        session left to outlive its programme (ANH-181).

        ``measured_rpm`` and ``current`` go to ``None`` the moment the status is
        stale, never to a fabricated zero - a made-up 0 rpm is exactly the lie
        that would make ``no_load`` and ``reverse_rotation`` judge a machine that
        is not there. The LFRD echo is passed only when it was read in THIS
        tick, after the keepalive wrote the setpoint it is compared with.
        """
        status = self._readable_status(now)
        echo = None
        if status is not None and self._last_status_at == now:
            echo = status.setpoint_echo_rpm
        return SafetyObservation(
            now=now,
            phase=self._phase,
            elapsed=self._session_elapsed(now),
            total_duration=self._total_duration(),
            commanded_rpm=self._applied_rpm,
            ramping=self._is_ramping(now),
            heart_rate=self._last_sample,
            drive_state=DriveState.COMM_LOST if status is None else status.state,
            measured_rpm=None if status is None else status.output_rpm,
            current=None if status is None else status.current,
            fault=None if status is None else status_fault_report(status),
            consecutive_comm_failures=self._comm_failures(),
            attendant_last_seen=self._attendant_last_seen,
            commanded_g=self._geometry.view(self._applied_rpm).g_load,
            resting_bpm=self._resting_bpm,
            setpoint_echo_rpm=echo,
            envelope=envelope,
            heart_rate_supervised=self._occupied(),
            stopped_by=self._stopped_by,
            session_over=self._session_over and self._applied_rpm == 0,
        )

    def _envelope(self, now: Monotonic) -> SpeedEnvelope | None:
        """Where the shaft may be this tick, or ``None`` while nothing talks to the drive.

        See :mod:`src.training.tracking`. The follower is advanced towards the
        setpoint in force, and re-anchored on the measured speed whenever the
        drive is not working to the reference (disabled, coasting, faulted).
        """
        if not self._link_open:
            return None
        status = self._readable_status(now)
        return self._follower.update(
            now,
            self._applied_rpm,
            None if status is None else status.output_rpm,
            following=status is not None and status.state is DriveState.OPERATION_ENABLED,
        )

    def _occupied(self) -> bool:
        """Whether a person may be in the machine: always, except a BENCH manual session.

        A programme is always taken to have somebody on board; so is a runtime
        with no session at all (it judges nothing but ``DONE`` then anyway).
        """
        manual = self._manual
        return manual is None or manual.occupancy is not Occupancy.BENCH

    def _latch(self, now: Monotonic, rule: str, action: SafetyAction, detail: str) -> None:
        """Latch a verdict this module raised on its own behalf.

        Kept here rather than pushed into the supervisor because the supervisor
        latches only what a rule or an e-stop produced, and draining a thread
        trip needs a tick - which :meth:`start` does not have. A more severe
        latch replaces a less severe one; an equally severe one does not, so the
        *first* thing that took the machine out of service keeps its instant.
        """
        current = self._latched
        if current is not None and action <= current.action:
            return
        self._latched = SafetyVerdict(
            action=action, rule=rule, detail=detail, latched=True, since=now
        )
        _logger.error("runtime latched %s by rule %s", action.name, rule)

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Clear the latched verdicts, by name. The only thing that can.

        Never called automatically from anywhere in this file - not on a fault,
        not on a reconnect, not at the start of a session. And clearing a latch
        is **not** a resumption: it puts the machine back into service so a new
        session object can be created, and nothing here restarts the session it
        cleared. ``GO_SILENT`` cannot be cleared at all, here or in the
        supervisor: recovery from silence is an operator action on a machine that
        has demonstrably stopped.

        ``estop_released`` is the operator stating that the latching mushroom has
        been pulled back out. Software cannot see that contact, so the default is
        ``False`` and forgetting the question fails closed.
        """
        if not operator.strip():
            return Err(
                Unattributed(
                    "an acknowledgement must name the operator making it: "
                    "an unattributable safety record is not a safety record"
                )
            )
        mine = self._latched
        if mine is not None and mine.action is SafetyAction.GO_SILENT:
            return Err(GoSilentIsTerminal(mine.rule))
        outcome = self._supervisor.acknowledge(operator, estop_released=estop_released)
        if isinstance(outcome, Ok):
            self._latched = None
            return Ok(self._merge_acknowledgement(outcome.value, mine))
        return self._refuse_or_clear(operator, outcome.error, mine)

    def _merge_acknowledgement(
        self, record: SafetyAcknowledgement, mine: SafetyVerdict | None
    ) -> SafetyAcknowledgement:
        """Add this module's own cleared rule to the supervisor's record."""
        if mine is None:
            return record
        return SafetyAcknowledgement(
            operator=record.operator,
            at=record.at,
            wall_clock=record.wall_clock,
            cleared=(*record.cleared, mine.rule),
        )

    def _refuse_or_clear(
        self, operator: str, refusal: AcknowledgeRefusal, mine: SafetyVerdict | None
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Handle the supervisor's refusal, exhaustively.

        Only one of the four is interesting here: the supervisor having nothing
        latched is not a refusal when *this* module does, so that case clears our
        own latch and reports it. The other three are the supervisor's judgements
        and pass through unchanged.
        """
        match refusal:
            case NothingLatched():
                if mine is None:
                    return Err(refusal)
                self._latched = None
                return Ok(
                    SafetyAcknowledgement(
                        operator=operator,
                        at=self._clock.monotonic(),
                        wall_clock=self._clock.unix_millis(),
                        cleared=(mine.rule,),
                    )
                )
            case Unattributed() | GoSilentIsTerminal() | EmergencyStopStillLatched():
                return Err(refusal)
        raise assert_never(refusal)

    # =====================================================================
    # Commanding
    # =====================================================================

    async def _command(
        self, now: Monotonic, standing: SafetyVerdict | None, previous: Monotonic | None
    ) -> None:
        """Apply the standing verdict, which is the only thing that sets a setpoint.

        Exhaustive over :class:`~src.training.types.SafetyAction`, so a new
        action fails the type check here rather than falling into whichever
        branch happened to be last.

        ``previous`` is the instant of the tick before this one (``None`` on
        the first): the time base of a stop that begins under a FREEZE
        (:meth:`_stop_under_freeze`).

        The last lines are the ONE place that sees a setpoint come back to
        zero, whichever arm wrote it: see :meth:`_note_standstill`.
        """
        action = SafetyAction.NONE if standing is None else standing.action
        before = self._applied_rpm
        match action:
            case SafetyAction.NONE:
                self._descent_from = None
                await self._follow_controller(now, allow_increase=True, cap=None)
            case SafetyAction.FREEZE if self._stop_asked():
                # A FREEZE holds a speed somebody still wants. It never holds
                # one the operator has asked to bring down (ANH-175), nor one
                # the programme itself is bringing down (ANH-189).
                await self._stop_under_freeze(now, previous)
            case SafetyAction.FREEZE:
                # Hold the last commanded speed and do not consult the controller
                # at all. That is what makes the resume bumpless in the sense
                # that matters: the controller's next increment is added to the
                # speed read back from the drive, so a freeze does not end by
                # jumping to whatever the control law was demanding when it
                # began.
                #
                # It does NOT mean the first tick after a freeze is a small one.
                # The controller is not called during the freeze, so its own
                # interval keeps accumulating, and its first decision afterwards
                # integrates over `Gains.step_cap` - two control periods. The
                # slew guarantee is stated between successive *changes* of the
                # setpoint, and it still holds: a step of at most
                # `slew * step_cap` after a gap of at least `step_cap` is inside
                # `slew * dt`. What it does not promise is a gentle single tick,
                # and the test that names this asserts the property that is true
                # rather than the one that sounds better.
                self._descent_from = None
                self._motion_from = None
                self._decision = None
                await self._apply_setpoint(now, self._applied_rpm)
            case SafetyAction.REDUCE:
                # Step down, and keep regulating within that cap: a heart rate
                # above its zone is still a control problem, it is just no longer
                # allowed to end in an increase.
                await self._follow_controller(now, allow_increase=False, cap=self._descend(now))
            case SafetyAction.RAMP_DOWN:
                self._begin_ending(now, EndReason.SAFETY_VERDICT, standing)
                self._decision = None
                await self._apply_setpoint(now, self._descend(now))
            case SafetyAction.QUICK_STOP:
                # Zero the reference NOW, skipping the software ramp, and LEAVE
                # THE RUN COMMAND IN PLACE so the drive decelerates on its own
                # commissioned ramp. Not faster than that ramp, and it must not
                # be read as immediate: a faster demand trips overvoltage and
                # drops the drive into freewheel, which is slower still.
                self._begin_ending(now, EndReason.SAFETY_VERDICT, standing)
                self._descent_from = None
                self._motion_from = None
                self._decision = None
                await self._apply_setpoint(now, MotorRpm(0))
            case SafetyAction.GO_SILENT:
                ending = self._begin_ending(now, EndReason.SAFETY_VERDICT, standing)
                self._decision = None
                self._go_silent(now, ending.detail)
            case _ as unreachable:
                assert_never(unreachable)
        if action is not SafetyAction.NONE:
            self._rebase_controller(now)
        # This tick's step, ACKNOWLEDGED by the drive, took the setpoint to zero
        # (`_applied_rpm` advances on an acknowledged write and on nothing else).
        if before != 0 and self._applied_rpm == 0:
            self._note_standstill(standing)
        self._withdraw_waiting_target(standing)

    def _stop_asked(self) -> bool:
        """Whether an arm that is turning has been asked to come down to zero.

        Asked by an ending, whoever began it: the operator's STOP at the
        console, a stop sent from the site (both are :meth:`request_stop`,
        turned into the ending by :meth:`_advance_phase` earlier in this very
        tick), the manual session reaching its own limit, or a verdict that
        ended the session and has been acknowledged since. Or asked by a
        manual target of zero, which only the operator sets while the arm
        turns: :meth:`_withdraw_waiting_target` zeroes a target over a
        setpoint that is already zero, and nothing else touches one outside
        an ending.

        Or asked by the programme itself (ANH-189): from the entry into
        ``COOLDOWN`` its timeline wants the arm down, and no phase after that
        one asks for speed again (:func:`motion_is_over`, the test
        :meth:`_note_standstill` already reads to call that zero the
        programme's own). A FREEZE is there to keep a speed somebody still
        wants from being raised or regulated; from there on nobody wants one.
        An ending always shows one of those phases too, and a manual session
        shows one only while it is ending.

        With the setpoint already at zero there is nothing to bring down, and
        the hold arm keeps it there exactly as it always has.
        """
        if self._applied_rpm == 0:
            return False
        if self._ending is not None or motion_is_over(self._phase):
            return True
        return self._manual is not None and self._manual_target == 0

    async def _stop_under_freeze(self, now: Monotonic, previous: Monotonic | None) -> None:
        """A stop asked for while a FREEZE stands: walk the setpoint to zero all the same.

        The defect (ANH-175): the FREEZE arm re-applied the setpoint and looked
        at nothing else, so a STOP was recorded, the screen said ARRET, and the
        arm went on turning at session speed: twenty seconds under ``hr_stale``
        until its REDUCE, without limit under a latched ``loop_stall``. Only
        the emergency stop acted. A FREEZE is there to keep the speed from
        being raised or regulated on evidence nobody trusts. Coming down is
        not that, and it needs no evidence at all: the destination is zero.

        So the setpoint takes the ordinary stop's own walk, at the motion
        limits (:meth:`_motion_step`, as :meth:`_follow_manual` and a
        programme's cooldown do), and a programme's last step across
        ``(0, min_run)`` waits as it does there (:meth:`_passage_too_soon`).
        The destination is zero and nothing else, so nothing here can raise
        the setpoint, and neither the controller nor the heart rate is
        consulted. A manual descent is step for step the one
        :meth:`_follow_manual` walks with no verdict.

        A programme's ordinary descent (its cooldown, or a stop with no
        verdict) is bounded twice, and this one only once. It walks at the
        motion limits towards the controller's demand, and that demand comes
        down on the control law's own ramp: at once by ``slew x`` the age of
        its last decision (a head start of 3 to 78 rpm with the shipped
        15 rpm/s and 5 s period), then by ``floor(slew x dt)`` whole rpm a
        tick, which at 5 Hz is 2 or 3 rpm according to the tick's measured
        length, against 2.48 for the shipped motion limits. While the head
        start lasts the motion limits are what binds and the two descents are
        the same, tick for tick. Once it is spent the ordinary one goes at
        the control law's pace. So this walk is never faster than the motion
        limits and never behind an ordinary descent, and it can be AHEAD of
        one, with the shipped limits too: by up to 2.6 s from 193 rpm on the
        shipped programme, in 9 of the 26 places the control period can fall
        (measured on the software rig, never on the machine). It is also the
        walk a programme's own cooldown takes when the timeline reaches it,
        or goes on with it, under a FREEZE (ANH-189).

        The time base. The hold arm leaves the profiler none, and a REDUCE on
        a programme walks its own ramp and leaves a stale one. Followed
        blindly, the first gives a stop that begins one tick late and the
        second a first step as long as the REDUCE lasted. So unless the
        profiler ran on the previous tick, its allowance starts at that tick:
        one tick's worth, which is what a setpoint parked at its target gets
        when it is given a new one with no verdict standing. A FREEZE that
        appears during a descent already begun finds the profiler running and
        keeps its time base: the descent does not even pause.

        Every stronger verdict still decides first, in its own arm: this one
        is reached under FREEZE only. What the zero means once it is reached
        is :meth:`_note_standstill`'s to say.
        """
        self._descent_from = None
        self._decision = None
        if self._motion_at != previous:
            self._motion_from = previous
        moved = self._motion_step(now, MotorRpm(0))
        if self._manual is None and self._passage_too_soon(now, moved):
            moved = self._applied_rpm
        await self._apply_setpoint(now, moved)

    def _withdraw_waiting_target(self, standing: SafetyVerdict | None) -> None:
        """Something holds the arm at standstill: take back the manual target that waits.

        A manual target is a destination, and while a rise is held it is not
        followed. Left in place over a setpoint of zero it WAITED: the
        operator's own zero, then a lost heart rate, then 200 rpm typed at the
        console; thirty seconds with nothing moving; the heart rate back, and
        the first non-zero setpoint 0.2 s later with nobody clicking. With a
        latched verdict it was the acknowledgement that started the arm. And
        with no verdict at all, behind the heart rate of a person on board: a
        rate coming down after an effort held a target for ninety seconds, and
        the arm left when the fall ended (ANH-178, decisions of 2026-10-06,
        ``docs/securite.md`` 7.6).

        So, at the end of every command step: if the setpoint is zero, a target
        is waiting and anything holds the rise, the target goes back to zero.
        "Anything" is the standing verdict, whatever it is - latched or not,
        FREEZE or REDUCE, already standing or appearing on this very tick, and
        the tick a REDUCE has just walked the setpoint to zero - or, with no
        verdict, what :meth:`_follow_manual` found on this tick
        (:class:`RiseHold`): the heart rate, or a drive that did not
        acknowledge the first step. Together with the refusal in
        :meth:`set_manual_target` this leaves one way for a stopped arm to be
        given a speed: a target entered while nothing holds it. After every
        tick: setpoint zero and a rise held, then target zero.

        The console is told (:meth:`take_withdrawn_target`), so that the
        operator knows to ask again, with one exception: on the tick a warning
        itself has just stopped the arm, the session ends on the next one
        (:meth:`_note_standstill`), there is no asking again, and the ending
        says everything there is to say. The target is zeroed all the same.

        Nothing here touches an arm that is turning: its setpoint is not zero,
        so its target is kept and followed when the hold goes, as before. And
        an ending needs nothing from this: it has already zeroed the target
        (:meth:`_begin_ending`). A programme never has a manual target.

        What is NOT a hold: the motion profiler earning its first step. It
        depends on nothing but time. With the shipped limits and a 0.2 s tick
        the passage from zero is written on the first tick that follows the
        target, or on the second when the profiler has no time base yet (a
        session's first tick, the tick after a FREEZE).
        """
        target = self._manual_target
        if self._applied_rpm != 0 or target == 0:
            return
        held: Holding | None = standing if standing is not None else self._rise_held
        if held is None:
            return
        self._manual_target = MotorRpm(0)
        if self._stopped_by is None:
            self._withdrawn = WithdrawnTarget(target=target, by=held)
        _logger.warning(
            "manual target of %d motor rpm withdrawn: the arm is held at standstill (%s)",
            target,
            held.rule if isinstance(held, SafetyVerdict) else held.value,
        )

    def _note_standstill(self, standing: SafetyVerdict | None) -> None:
        """The setpoint has just come back to zero: was that by itself? If so, say it.

        Product decisions of 2026-10-05 and 2026-10-06 (``docs/securite.md``):
        once the arm has moved in a session, any return of the setpoint to zero
        that nobody asked for ends the session, latched. **A stopped arm never
        restarts by itself.** Before this, two things took the arm out of a
        standstill with nobody clicking. An unlatched warning that lifted: on
        the shipped profile a heart rate lost for 50 s stopped the arm at 42 s,
        and 45 s after it came back the setpoint was 69 motor rpm, the mode
        still SEANCE, the operator beside the capsule refitting the electrode.
        And the control law, which regulates in both directions: it writes
        zero when the heart rate is above the zone and leaves zero again when
        it has come back down, with no verdict on screen at any point.

        Called from the end of :meth:`_command` and from nowhere else, on the
        tick a non-zero setpoint became an acknowledged zero, so every arm is
        covered by one test instead of one copy per arm, and a zero that never
        landed ends nothing: the next tick tries again. "After the arm has
        moved" is that same test: the step started above zero.

        This method only STATES what happened (:attr:`_stopped_by`, handed to
        the supervisor in :meth:`_observe`). The verdict is the supervisor's
        ``session_standstill``, on the next tick: it lands in the supervisor's
        latched floor, so the status page, the start gate and the
        acknowledgement see this ending exactly as they see every other latched
        one, and this module's own single latch stays free for a refused stop
        word (``disable_refused``) that may follow. Nothing moves in between:
        the setpoint is zero, and the tick that could raise it is the tick the
        verdict decides.

        A zero somebody asked for is not a standstill "by itself", and two of
        them come through here:

        * the session is already ending (:func:`motion_is_over`): an operator
          stop, the programme's own cooldown and recovery, a verdict that
          ended the session. That ending keeps its own cause, and nothing
          restarts from there;
        * a manual session with no warning standing: the setpoint follows the
          operator's target and nothing else, so the operator typed that zero
          and may type another speed.

        The baseline never comes here at all: nothing has moved yet, so no step
        can start above zero. A warning that comes and goes there is followed
        by the programme's normal start, and the decision leaves that as it is.

        A manual target of zero typed WHILE a warning is lowering the speed is
        not told apart from the warning's own descent: the cautious reading,
        the session ends. And nobody on board (a BENCH manual session) changes
        nothing here: the decision of 2026-10-06 covers the empty capsule too.

        The same reading holds for a manual target of zero followed WHILE a
        warning only holds the speed (a FREEZE, :meth:`_stop_under_freeze`): the
        exemption above is for a zero typed with NO warning standing, so this
        session ends too, for one acknowledgement. There the cause can be told
        apart, and it is: a FREEZE lowers nothing, so that zero is the
        operator's own, and the verdict says so instead of blaming the
        warning. An operator's STOP under the same FREEZE never comes this
        far: its session is already ending.
        """
        if motion_is_over(self._phase):
            return
        if standing is not None and standing.action is SafetyAction.FREEZE:
            self._stopped_by = (
                f"the operator's manual target of zero, followed under the warning {standing.rule},"
            )
        elif standing is not None:
            self._stopped_by = f"the warning {standing.rule}"
        elif self._manual is None:
            self._stopped_by = "the heart-rate regulation"

    def _rebase_controller(self, now: Monotonic) -> None:
        """Hand the controller the setpoint a verdict put in force.

        Every tick a verdict decided the output, the controller's standing
        demand is replaced by the speed actually applied. Otherwise the tick the
        verdict lifts re-emits whatever the controller last wanted: a REDUCE
        that walked the machine down to 60 rpm would end by writing the 276 rpm
        the controller was still holding, in one step, with a person inside.

        ``_applied_rpm`` and not the capped value that was asked for, because a
        write that failed left the previous value in force - and that is the
        value the next increment must be added to.
        """
        controller = self._controller
        if controller is not None:
            controller.rebase(now, self._applied_rpm)

    async def _follow_controller(
        self, now: Monotonic, *, allow_increase: bool, cap: MotorRpm | None
    ) -> None:
        """Step 3: the controller's demand, capped at what the verdict permits.

        With no programme there is no controller, and the only honest demand is
        zero - so an idle runtime still writes a zero reference every tick, and
        still proves the link.
        """
        if self._manual is not None:
            await self._follow_manual(now, allow_increase=allow_increase, cap=cap)
            return
        controller = self._controller
        if controller is None:
            self._decision = None
            await self._apply_setpoint(now, MotorRpm(0) if cap is None else cap)
            return
        rise = self._increase_permitted()
        step = controller.update(
            now, self._control_input(now), allow_increase=allow_increase and rise
        )
        if not step.held:
            # Only a tick that actually decided replaces the published decision.
            # The control period is five seconds and the loop runs at 5 Hz, so
            # twenty-four ticks out of twenty-five are held ones - and a held
            # tick reports no target, because a tick that decided nothing has no
            # target to claim. Publishing that would make the operator's target
            # flash on and off five times a second while the band it comes from
            # has not moved at all.
            self._decision = step.decision
        demand = step.decision.desired_rpm
        if cap is not None:
            # REDUCE: the verdict's own descent IS the setpoint, at the safety
            # ramp (_descend), which may be faster than the motion limits. Not
            # min(demand, cap): the demand runs ahead of the motion-limited
            # setpoint, and jumping down to it would outrun every ramp there is.
            await self._apply_setpoint(now, cap)
            return
        # Every non-emergency change of a programme's setpoint - the controller's
        # demand, the warm-up, the cooldown - is walked at the anti-nausea
        # limits exactly as a manual target is. The controller decides once per
        # five-second period and may ask for up to slew x period (75 rpm) at
        # once; the drive would execute that on its own ACC ramp.
        moved = self._motion_step(now, demand)
        applied = self._applied_rpm
        if moved > applied and not (rise and self._readable_status(now) is not None):
            # The vasovagal gate holds the setpoint where it is, including a
            # climb towards a demand decided before the heart began to fall -
            # and so does a drive whose state is no longer known. The
            # controller adopts the speed actually held.
            moved = applied
            self._motion_from = now
            controller.rebase(now, moved)
        elif self._passage_too_soon(now, moved):
            moved = applied
        await self._apply_setpoint(now, moved)

    def _passage_too_soon(self, now: Monotonic, moved: MotorRpm) -> bool:
        """Whether a step across the gap ``(0, min_run)`` would outrun the programme's slew.

        The motion limiter takes the passage between 0 and ``min_run`` on any
        tick that has earned one rpm (see :mod:`src.training.motion`), because
        its own allowance is capped below a whole ``min_run``. The control law
        promises more for a programme: every change of the setpoint within
        ``slew x`` the time since the previous one. So a programme's passage
        waits until that much time has passed since the setpoint last moved -
        at 15 rpm/s, under four seconds at 1.1 output rpm - and never deadlocks,
        because that time is not capped.
        """
        applied = self._applied_rpm
        if (moved == 0) == (applied == 0):
            return False
        changed = self._setpoint_changed_at
        if changed is None:
            return False
        return self._limits.slew * elapsed(changed, now) < abs(moved - applied)

    async def _follow_manual(
        self, now: Monotonic, *, allow_increase: bool, cap: MotorRpm | None
    ) -> None:
        """Step 3 for a manual session: the operator's target, at the motion limits.

        Under REDUCE the verdict's descent (``cap``) IS the setpoint: the
        target no longer matters when the only direction allowed is down. With
        nobody asking for anything, the setpoint steps towards the target -
        which is 0 once the session is ending - and it may not rise while the
        heart rate of a person on board holds it (:meth:`_heart_rate_hold`).

        What kept the setpoint from rising on this tick, if anything did, is
        left in ``_rise_held`` for :meth:`_withdraw_waiting_target`: the heart
        rate's hold, or a drive that did not acknowledge the write. Every
        frame sent is exactly the one sent before that account was kept.
        """
        self._decision = None
        if cap is not None:
            await self._apply_setpoint(now, cap)
            return
        target = MotorRpm(0) if self._ending is not None else self._manual_target
        moved = self._motion_step(now, target)
        held = self._heart_rate_hold(now)
        before = self._applied_rpm
        if not (allow_increase and held is None) and moved > before:
            moved = before
        await self._apply_setpoint(now, moved)
        if self._applied_rpm != moved:
            # Asked of the drive and not acknowledged: the setpoint in force is
            # still the old one, and the next tick would ask again. Only a
            # first step matters to the target; a turning arm keeps its own.
            held = RiseHold.WRITE_UNACKNOWLEDGED
        self._rise_held = held

    def _increase_permitted(self) -> bool:
        """Whether the heart rate allows the setpoint to RISE this tick: the vasovagal gate.

        No rise while the short trend (``RuntimeLimits.trend_samples``) falls
        faster than ``RuntimeLimits.falling_trend``, nor while it is unknown. A
        falling heart rate reads to the control law as "below the zone", and
        its answer is to accelerate; before this gate it did so for ~16 s of a
        collapse until ``hr_drop`` tripped. Restricts the controller only:
        every safety verdict still decides first.
        """
        trend = self._tracker.recent_rate(self._limits.trend_samples)
        return trend is not None and trend >= self._limits.falling_trend

    def _motion_step(self, now: Monotonic, target: MotorRpm) -> MotorRpm:
        """One motion-limited step towards ``target``, with the accrual of motion.py.

        The allowance accrues from the instant the setpoint last moved (minus
        the carry), and restarts whenever the setpoint is where it is asked to
        be, so a setpoint parked for a minute does not bank a minute of motion.
        """
        self._motion_at = now
        applied = self._applied_rpm
        started = self._motion_from
        if started is None or applied == target:
            self._motion_from = now
            return applied
        interval = elapsed(started, now)
        moved = next_setpoint(applied, target, self._motion, self._motion_geometry, interval)
        if moved != applied:
            carried = carry_after(applied, moved, self._motion, self._motion_geometry, interval)
            self._motion_from = Monotonic(now - carried)
        return moved

    def _control_input(self, now: Monotonic) -> ControlInput:
        """What the control law is allowed to know this tick.

        ``bpm`` comes from
        :meth:`~src.training.hr_control.HeartRateTracker.usable` and from nothing
        else: that call is where accepted, filtered and fresh are joined, and it
        returns ``None`` for every reason a rate may not be acted on.
        ``applied_rpm`` is LFRD read back - the reference the drive is actually
        working to - and ``None`` whenever the status is stale, because adding an
        increment to an unknown base is how a machine ends up somewhere nobody
        predicted.
        """
        return ControlInput(
            phase=self._phase,
            bpm=self._tracker.usable(now),
            applied_rpm=self._applied_readback(now),
            resting_bpm=self._resting_bpm,
        )

    def _applied_readback(self, now: Monotonic) -> MotorRpm | None:
        """The setpoint echo, or ``None`` when there is no fresh observation.

        LFRD read back rather than the value we believe we wrote: a Modbus write
        response echoes the request, so a write to a wrong address is
        acknowledged while the speed reference never moves. This is the field
        that notices.
        """
        status = self._readable_status(now)
        return None if status is None else status.setpoint_echo_rpm

    def _descend(self, now: Monotonic) -> MotorRpm:
        """The next step of a SAFETY descent (a REDUCE or RAMP_DOWN verdict).

        On a programme this is the configured slew (15 rpm/s), deliberately
        faster than the anti-nausea motion limits every ordinary setpoint change
        now obeys (12.4 rpm/s or less): a verdict that asks for less speed is
        a safety demand, and a slightly faster, still controlled, descent is
        the right trade there. Only QUICK_STOP is faster (the drive's own
        commissioned ramp). A manual session descends at the motion limits
        even under a verdict, as it always has.

        Mirrors the control law's own limiter rather than inventing a second one,
        including the part that is easy to get wrong: the setpoint domain is
        ``{0} union [min_run_rpm, max_rpm]``, so the last step of a stop is a
        jump of ``min_run_rpm`` and may only be taken when the accumulated
        allowance covers the whole jump.

        A tick that cannot pay for its step leaves the interval **unspent**, so
        the allowance keeps growing. Without that the descent stalls one step
        short: the setpoint parks at the minimum running speed, every tick
        proposes zero, every tick is refused for want of allowance, and the
        machine never stops - with a person inside it, and with the telemetry
        showing a controller dutifully demanding zero.
        """
        if self._manual is not None:
            # Deceleration exactly as acceleration: see src/training/motion.py.
            return self._motion_step(now, MotorRpm(0))
        previous = self._applied_rpm
        if previous == 0:
            self._descent_from = None
            return previous
        started = self._descent_from
        if started is None:
            # First tick of this descent: the allowance starts accruing now, so
            # nothing is spent that was not earned inside the descent itself.
            self._descent_from = now
            return previous
        allowance = math.floor(self._limits.slew * elapsed(started, now))
        minimum = self._min_run_rpm()
        required = minimum if previous <= minimum else 1
        if allowance < required:
            return previous
        candidate = previous - allowance
        if candidate < minimum:
            candidate = minimum if previous > minimum else 0
        self._descent_from = now
        return MotorRpm(candidate)

    async def _apply_setpoint(self, now: Monotonic, rpm: MotorRpm) -> None:
        """Write a setpoint, unless it is already the one the keepalive just sent.

        Skipping the redundant write is safe precisely because the keepalive went
        out first carrying this same value: the frame that feeds ``ttO`` has
        already been sent this tick, so a steady setpoint costs one write and a
        changing one costs two.

        The silence test here is **redundant defence today, and is kept as
        such.** Every action that reaches this method is one that would also
        have returned before ``_silent`` could be true - ``GO_SILENT`` latches,
        so no lower action follows it - and a reversion audit confirmed that
        removing this clause changes nothing observable. It stays because the
        guarantee "a silent runtime sends no frame" should hold by reading every
        write site, not by tracing which actions can follow which.
        """
        if self._silent or not self._link_open or rpm == self._applied_rpm:
            return
        await self._exchange_write_speed(now, rpm)

    async def _settle(self, now: Monotonic) -> None:
        """Remove the run command, but only from a machine shown to be stopped.

        Every condition here is necessary. The phase must be one after which no
        motion is asked for. The setpoint must be zero. And RFRD must show
        standstill - not the setpoint, not the state, RFRD, because FAULT,
        NOT_READY and COMM_LOST are all compatible with a centrifuge still
        turning.

        If standstill cannot be shown, nothing is sent and the drive is left in
        OPERATION_ENABLED with a zero reference, where its own ``ttO`` timeout
        ramps it down. That is the better of the two available endings: word 6
        here would be CiA402 transition 8 on a moving centrifuge, which drops the
        output stage and coasts for minutes.
        """
        if self._silent or not self._link_open or not self._enabled:
            return
        if not motion_is_over(self._phase):
            return
        if self._applied_rpm != 0 or not self._at_standstill(now):
            return
        status = self._readable_status(now)
        if status is not None and status.state is DriveState.FAULT:
            # No torque at standstill, and every word but FAULT_RESET is refused
            # by design: the ending waits for the operator's fault reset.
            return
        # Transition 5 (ramps, keeps control of the shaft) and only then
        # transition 2.
        word = ControlWord.SWITCH_ON
        failure = await self._exchange_command(word)
        if failure is None:
            word = ControlWord.SHUTDOWN
            failure = await self._exchange_command(word)
        if failure is None:
            self._enabled = False
            self._disable_failures = 0
            _logger.info("output stage disabled at standstill")
            return
        self._disable_failures += 1
        if self._disable_failures >= self._limits.disable_attempts:
            await self._escalate_disable(now, word, failure)

    async def _escalate_disable(
        self, now: Monotonic, word: ControlWord, failure: DriveFailure
    ) -> None:
        """The drive keeps refusing to have its output stage removed: say so, then force it.

        Retrying forever was the defect: ~480 refused frames until the console
        exited, the output stage left enabled, no verdict and nothing on the
        operator's screen. After ``RuntimeLimits.disable_attempts`` refusals in
        a row:

        * if it was SWITCH_ON (transition 5) that was refused, SHUTDOWN is sent
          directly. That is transition 8, which on a TURNING shaft drops the
          output stage into a freewheel - but this is only ever reached at a
          standstill confirmed from a fresh RFRD with a zero reference, where
          removing the stage lets nothing coast. A latched ``disable_refused``
          RAMP_DOWN tells the operator which word was refused;
        * if SHUTDOWN is refused too (or was the word refused in the first
          place), no word this runtime has removes the output stage, and a
          drive that will not take its words cannot be trusted with any: the
          verdict is GO_SILENT, and the drive's own ``ttO`` drops the stage.
        """
        attempts = self._disable_failures
        refused = (
            f"the drive refused {word.name} {attempts} times in a row at confirmed standstill "
            f"({failure.detail})"
        )
        if (
            word is ControlWord.SWITCH_ON
            and await self._exchange_command(ControlWord.SHUTDOWN) is None
        ):
            self._enabled = False
            self._disable_failures = 0
            self._latch(
                now,
                RULE_DISABLE_REFUSED,
                SafetyAction.RAMP_DOWN,
                (
                    f"{refused}: the output stage was removed with SHUTDOWN instead, which is "
                    "harmless only because the shaft is confirmed stopped. Have the drive "
                    "checked before the next session"
                ),
            )
            return
        detail = (
            f"{refused}, and SHUTDOWN too: no word removes the output stage, so no further "
            "frame will be sent and the drive's own ttO timeout drops it"
        )
        self._latch(now, RULE_DISABLE_REFUSED, SafetyAction.GO_SILENT, detail)
        self._go_silent(now, detail)

    # =====================================================================
    # Out-of-band requests
    # =====================================================================

    def request_stop(self, reason: str) -> None:
        """Ask for the session to end. Honoured on the next tick.

        Deliberate and unhurried, which is what an operator pressing "stop"
        means: the phase becomes COOLDOWN and the setpoint walks down the
        software ramp exactly as a normal cooldown does. For the stop that cannot
        wait for a tick, use :meth:`request_estop`, which zeroes the reference
        synchronously.
        """
        if self._stop_requested is None:
            self._stop_requested = reason
            _logger.warning("operator stop requested")

    def request_estop(self, source: str) -> SafetyVerdict:
        """Latch the emergency stop and zero the reference, synchronously.

        Callable from a request handler, a signal handler or an ``except``
        branch: it takes no lock, awaits nothing, and does not wait for the
        control loop to reach its next tick. A stop that waits for whatever the
        loop is currently awaiting is not a stop.

        **A button in a browser is a convenience stop.** It depends on a network,
        a web server, an event loop and this process, any of which can be the
        thing that failed, and it is never safety-rated. The safety-rated stop is
        the wired mushroom attested through :meth:`confirm_estop_wiring` - and
        while STO is jumpered even that one is a ramp, not a removal of torque.

        Nothing is sent if this runtime is already silent: the reference is
        already zero there and ``ttO`` is already counting, so a fresh frame
        would buy nothing and would restart the timeout.
        """
        verdict = self._supervisor.latch_estop(source)
        now = self._clock.monotonic()
        self._begin_ending(now, EndReason.EMERGENCY_STOP, verdict)
        if not self._silent:
            self._emergency_zero(now)
        return verdict

    def _emergency_zero(self, now: Monotonic) -> EmergencyStopOutcome:
        """Zero the speed reference synchronously, best effort, never raising.

        Blocks the event loop for up to the drive's own
        :attr:`~src.motor.drive.DriveBackend.emergency_budget`, on purpose: this
        call *is* the stop, and handing it to an executor would hand it to the
        same loop that may be the thing that failed.

        The budget is the backend's, not a constant of this module. A figure
        chosen here (it used to be 0.5 s) cannot know that one Modbus
        transaction may spend its serial timeout several times over, so it
        would be a bound the drive silently overruns - and one the ATV320
        driver would have to replace with its real floor anyway.

        The run command is deliberately not removed - see
        :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`. The
        outcome is matched exhaustively because ``NOTHING_SENT`` must never look
        like success: it means the motor is still commanded at whatever setpoint
        it held, and somebody has to be told.
        """
        outcome = self._drive.emergency_disable_blocking(self._drive.emergency_budget)
        match outcome:
            case EmergencyStopOutcome.ACKNOWLEDGED:
                # The drive acked the zero write, so the reference IS zero and it
                # is decelerating on its own ramp. Not "it has stopped".
                if self._applied_rpm != 0:
                    self._applied_rpm = MotorRpm(0)
                    self._setpoint_changed_at = now
                _logger.error("emergency zero acknowledged: the drive is ramping down")
            case EmergencyStopOutcome.SENT_UNCONFIRMED:
                # A frame went out and no usable answer came back. It may well
                # have landed, so the believed setpoint is left alone rather than
                # credited with a zero nobody confirmed.
                _logger.error("emergency zero sent but unconfirmed: probably ramping, possibly not")
            case EmergencyStopOutcome.NOTHING_SENT:
                _logger.critical(
                    "emergency zero reached no frame on the wire: the motor is still commanded "
                    "at %d rpm; escalate to cutting mains power",
                    self._applied_rpm,
                )
            case _ as unreachable:
                assert_never(unreachable)
        return outcome

    def _go_silent(self, now: Monotonic, detail: str) -> None:
        """Stop writing to the drive, for good, and let ``ttO`` finish the stop.

        One-way by construction: nothing in this file clears ``_silent``.

        The emergency zero is attempted once on the way in, and that order is
        deliberate. It does restart ``ttO`` - any frame does - but it buys a
        *commanded* zero immediately instead of one that only begins when the
        timeout expires, and after it no frame goes out at all, so the timeout
        then runs to completion from that moment. A zero reference now plus
        ``ttO`` afterwards is strictly better than ``ttO`` alone.
        """
        if self._silent:
            return
        self._silent = True
        _logger.critical("going silent; the drive's ttO timeout now owns the stop: %s", detail)
        if self._link_open:
            self._emergency_zero(now)

    def _fail_closed(self, now: Monotonic, detail: str) -> None:
        """The exit path for an exception inside the tick. Synchronous; cannot await.

        Order matters: the reference is zeroed before anything else, because
        everything after it is bookkeeping and the motor is not waiting for
        bookkeeping. Then the ending is recorded, and the snapshot is rebuilt so
        that a UI reading :meth:`snapshot` after the exception sees the truth
        rather than the last healthy tick.
        """
        self._latch(now, RULE_TICK_EXCEPTION, SafetyAction.GO_SILENT, detail)
        self._go_silent(now, detail)
        self._begin_ending(now, EndReason.TICK_EXCEPTION, self._latched)
        self._snapshot = self._build_snapshot(now)

    def _begin_ending(
        self, now: Monotonic, reason: EndReason, verdict: SafetyVerdict | None
    ) -> Ending:
        """Record that the session is ending, once, and return what was recorded."""
        existing = self._ending
        if existing is not None:
            return existing
        ending = Ending(
            reason=reason,
            action=SafetyAction.NONE if verdict is None else verdict.action,
            detail="operator stop" if verdict is None else f"{verdict.rule}: {verdict.detail}",
            at=now,
        )
        self._ending = ending
        self._end_reason = reason
        self._phase = Phase.COOLDOWN
        # After ANY stop the target is zero: moving again takes a new start.
        self._manual_target = MotorRpm(0)
        _logger.warning("session ending (%s) at %.3f s", reason.value, self._session_elapsed(now))
        return ending

    async def shutdown(self, reason: str) -> ShutdownReport:
        """The hard exit: the process is going away and cannot wait for ticks.

        Two steps, in this order. First the synchronous emergency zero, because
        it is the one write that matters and it works with no event loop. Then
        :meth:`~src.motor.drive.DriveBackend.close`, which is ``async`` and can
        therefore afford to wait - and waiting is what makes the stop a ramp
        instead of a freewheel: zero the reference, read RFRD until standstill,
        and only then remove the run command.

        A silent runtime sends neither. Its stop is already in the drive's hands,
        and a conforming ``close`` writes a stop sequence - a frame after silence
        would restart the very timeout that is carrying the stop out.

        Idempotent, because teardown paths call it twice: an ``except`` branch
        and then a ``finally``, or a shutdown handler and then ``atexit``. The
        second call reports the same thing rather than doing it again.
        """
        existing = self._shutdown
        if existing is not None:
            return existing
        if self._inspection_unconfirmed:
            self._enabled = True
            self._link_open = True
        now = self._clock.monotonic()
        if self._end_reason is None:
            self._end_reason = EndReason.SHUTDOWN
        if self._silent:
            report = self._silent_shutdown(reason, now)
        else:
            report = await self._speaking_shutdown(reason, now)
        self._shutdown = report
        self._snapshot = self._build_snapshot(now)
        _logger.warning("shutdown complete: %s", report.detail)
        return report

    def _silent_shutdown(self, reason: str, now: Monotonic) -> ShutdownReport:
        """Shut down without sending anything. See :meth:`shutdown`."""
        return ShutdownReport(
            reason=reason,
            at=now,
            silent=True,
            emergency=None,
            closed=False,
            output_disabled=not self._enabled,
            detail=(
                "already silent: nothing was sent, because a frame now would restart the "
                "drive's ttO timeout, which is what is carrying out the stop"
            ),
        )

    async def _speaking_shutdown(self, reason: str, now: Monotonic) -> ShutdownReport:
        """Emergency zero, then a close that is allowed to wait for standstill."""
        outcome = self._emergency_zero(now)
        closed = await self._drive.close()
        self._link_open = False
        if isinstance(closed, Ok):
            # close() is contractually a stop and a disable, not merely a dropped
            # port, so a clean close is evidence the output stage is off.
            self._enabled = False
            detail = f"emergency zero {outcome.name}; the drive link closed cleanly"
            return ShutdownReport(
                reason=reason,
                at=now,
                silent=False,
                emergency=outcome,
                closed=True,
                output_disabled=True,
                detail=detail,
            )
        failure = self._note_failure(Exchange.WRITE, closed.error)
        self._enabled = self._enabled or failure.output_unknown
        return ShutdownReport(
            reason=reason,
            at=now,
            silent=False,
            emergency=outcome,
            closed=False,
            output_disabled=not self._enabled,
            detail=f"emergency zero {outcome.name}; the close did not complete: {failure.detail}",
        )

    # =====================================================================
    # Drive exchanges
    # =====================================================================

    async def _exchange_write_speed(self, now: Monotonic, rpm: MotorRpm) -> DriveFailure | None:
        """Write LFRD, and record what the exchange said about the link.

        ``_applied_rpm`` advances only on an acknowledged write. A failed write
        may still have landed, but the value that is *known* to be in force is
        the previous one, and the next keepalive re-asserts it.
        """
        outcome = await self._drive.write_speed(rpm)
        if isinstance(outcome, Err):
            return self._note_failure(Exchange.WRITE, outcome.error)
        self._note_success(Exchange.WRITE)
        if rpm != self._applied_rpm:
            self._applied_rpm = rpm
            self._setpoint_changed_at = now
        return None

    async def _exchange_command(self, word: ControlWord) -> DriveFailure | None:
        """Write CMD, and record what the exchange said about the link."""
        outcome = await self._drive.write_command(word)
        if isinstance(outcome, Err):
            return self._note_failure(Exchange.WRITE, outcome.error)
        self._note_success(Exchange.WRITE)
        return None

    def _note_failure(self, kind: Exchange, error: DriveError) -> DriveFailure:
        """Count a failed exchange of one kind and classify it once."""
        failure = describe_drive_error(error)
        self._failures[kind] += 1
        self._last_failure = failure
        _logger.warning(
            "drive %s failed (%d consecutive): %s",
            kind.value,
            self._failures[kind],
            failure.detail,
        )
        return failure

    def _note_success(self, kind: Exchange) -> None:
        """Reset the run length of this kind only. It is a run length, not a total."""
        self._failures[kind] = 0

    def _comm_failures(self) -> int:
        """The run length the safety layer judges: the worse of the two kinds.

        The longer run, not the sum: one tick of a dead link fails both a write
        and a read, and counting that as two would make ``comms_lost`` fire at
        half its configured number of ticks.
        """
        return max(self._failures.values())

    # =====================================================================
    # Derived facts about the session and the machine
    # =====================================================================

    def _session_elapsed(self, now: Monotonic) -> Seconds:
        """Time since the session started; zero before it has."""
        started = self._started_at
        return Seconds(0.0) if started is None else elapsed(started, now)

    def _total_duration(self) -> Seconds:
        """The programme's intended length, the manual limit, or zero with neither."""
        program = self._program
        if program is not None:
            return program.total_duration_s
        return Seconds(0.0) if self._manual is None else MANUAL_SESSION_LIMIT

    def _cooldown_s(self) -> Seconds:
        """How long an abort may spend bringing the machine down.

        The profile's own cooldown length, or the commissioned deceleration when
        there is no profile - an abort can happen before any programme exists,
        and this machine's stop still takes as long as it takes.
        """
        program = self._program
        if program is not None:
            return program.profile.cooldown_s
        manual = self._manual
        return COMMISSIONED_DECEL_S if manual is None else manual.cooldown

    def _recovery_s(self) -> Seconds:
        """How long monitoring continues with nothing turning.

        None with nobody on board: after a BENCH session, and after an ending
        with no session at all (an e-stop pressed at rest), which would
        otherwise hold the console out of service for a minute of monitoring
        a person who was never in the machine.
        """
        program = self._program
        if program is not None:
            return program.profile.recovery_s
        manual = self._manual
        if manual is None or manual.occupancy is Occupancy.BENCH:
            return BENCH_RECOVERY
        return MIN_RECOVERY_S

    def _min_run_rpm(self) -> MotorRpm:
        """The slowest speed worth running at: the size of the last step of a stop.

        Taken from the control plan when there is one. Without a programme the
        only setpoint this runtime ever issues is zero, so any positive value
        would do, and one rpm is the one that claims least.
        """
        controller = self._controller
        return MotorRpm(1) if controller is None else controller.plan.speed.min_run_rpm

    def _readable_status(self, now: Monotonic) -> DriveStatus | None:
        """The last drive observation, or ``None`` once it is too old to be evidence."""
        status = self._last_status
        stamped = self._last_status_at
        if status is None or stamped is None:
            return None
        if elapsed(stamped, now) > self._limits.status_stale_after:
            return None
        return status

    def _status_age(self, now: Monotonic) -> Seconds | None:
        """Age of the last observation, or ``None`` if the drive was never read."""
        stamped = self._last_status_at
        return None if stamped is None else elapsed(stamped, now)

    def _at_standstill(self, now: Monotonic) -> bool:
        """Whether RFRD currently shows the shaft stopped.

        Requires a *fresh* observation. An old reading of zero is not evidence of
        standstill now, and this predicate gates removing the run command - the
        one decision that must never be taken on a memory.
        """
        status = self._readable_status(now)
        if status is None:
            return False
        return abs(status.output_rpm) < self._limits.standstill_rpm

    def _is_ramping(self, now: Monotonic) -> bool:
        """Whether the runtime is deliberately moving the setpoint, plus settling time.

        The runtime's own statement, as ``SafetyObservation.ramping`` requires:
        inferring it from two successive setpoints would make a stalled shaft
        look like a ramp, which is the case ``tracking_error`` exists to catch.
        """
        changed = self._setpoint_changed_at
        if changed is None:
            return False
        return elapsed(changed, now) <= self._limits.ramp_settle

    def _accumulate(self, now: Monotonic, interval: Seconds | None) -> None:
        """Add this tick to whichever zone counter the heart rate belongs in.

        Only ticks with a usable, fresh rate count, so the three do not add up to
        the session length - and that gap is informative: a session whose counters
        sum to a fraction of its length was a session spent mostly blind.
        """
        if interval is None:
            return
        program = self._program
        if program is None:
            return
        bpm = self._tracker.usable(now)
        if bpm is None:
            return
        counters = self._counters
        profile = program.profile
        if bpm < profile.zone_low_bpm:
            self._counters = ZoneCounters(
                in_zone=counters.in_zone,
                above_zone=counters.above_zone,
                below_zone=Seconds(counters.below_zone + interval),
            )
        elif bpm > profile.zone_high_bpm:
            self._counters = ZoneCounters(
                in_zone=counters.in_zone,
                above_zone=Seconds(counters.above_zone + interval),
                below_zone=counters.below_zone,
            )
        else:
            self._counters = ZoneCounters(
                in_zone=Seconds(counters.in_zone + interval),
                above_zone=counters.above_zone,
                below_zone=counters.below_zone,
            )

    # =====================================================================
    # Telemetry
    # =====================================================================

    def _speed_view(self, rpm: MotorRpm) -> SpeedView:
        """One speed in all four units, through the only module that converts."""
        return self._geometry.view(rpm)

    def _shown_status(
        self, now: Monotonic
    ) -> tuple[DriveStatus | None, DriveStatus | None, Seconds | None]:
        """The drive observation to DISPLAY: last, still-fresh, and its age.

        The session's own observation whenever the session path owns the
        link; the idle poll's before any session was started. Display only:
        the safety observation reads :meth:`_readable_status` and never this.
        """
        armed = self._program is not None or self._manual is not None
        if self._unknown_episode.failures > 0:
            status = self._last_status if self._last_status_at is not None else self._idle_status
            stamped = (
                self._last_status_at if self._last_status_at is not None else self._idle_status_at
            )
            return status, None, None if stamped is None else elapsed(stamped, now)
        if armed or self._link_open:
            return self._last_status, self._readable_status(now), self._status_age(now)
        stamped = self._idle_status_at
        if stamped is None:
            return None, None, None
        age = elapsed(stamped, now)
        status = self._idle_status
        return status, (None if age > self._limits.status_stale_after else status), age

    def _build_snapshot(self, now: Monotonic) -> TelemetrySnapshot:
        """One complete, internally consistent picture of the session.

        Every measurement carries its age, because a frozen screen is
        indistinguishable from a working one if it shows only values.

        The measured speed falls back to the last observation, and to zero before
        there has ever been one. ``drive_status_age`` of ``None`` and a
        ``drive_state`` of ``COMM_LOST`` are what make that zero honest, and they
        travel with it precisely so a consumer cannot mistake "never read" for
        "stopped" - which is what the field docstrings in
        :class:`~src.training.types.TelemetrySnapshot` warn about.
        """
        status, readable, status_age = self._shown_status(now)
        sample = self._last_sample
        decision = self._decision
        total = self._total_duration()
        session_elapsed = self._session_elapsed(now)
        return TelemetrySnapshot(
            at=now,
            wall_clock=self._clock.unix_millis(),
            phase=self._phase,
            elapsed=session_elapsed,
            remaining=Seconds(max(0.0, total - session_elapsed)),
            heart_rate=sample,
            heart_rate_age=None if sample is None else sample.age(now),
            target_bpm=None if decision is None else decision.target_bpm,
            setpoint=self._speed_view(self._applied_rpm),
            measured=self._speed_view(MotorRpm(0) if status is None else status.output_rpm),
            setpoint_confirmed=(
                readable is not None and readable.setpoint_echo_rpm == self._applied_rpm
            ),
            drive_state=DriveState.COMM_LOST if readable is None else readable.state,
            drive_status_age=status_age,
            current=None if readable is None else readable.current,
            fault=None if readable is None else status_fault_report(readable),
            safety=self.standing,
            counters=self._counters,
            mode=self.mode,
            manual=self._manual_view(),
        )

    def _manual_view(self) -> ManualView | None:
        """The manual session for the screen: target, ceiling, and the ramp still to come.

        No ramp and no arrival time are announced for a target a FREEZE is
        holding the setpoint away from: nothing is walking towards it, and the
        banner that says "ramp in progress, arrival in 0:20" over a speed that
        does not move was the screen's half of ANH-175. A FREEZE holds every
        target but zero (:meth:`_stop_asked`), so a descent to zero keeps its
        ramp and its arrival time under one, because it is really under way.
        """
        manual = self._manual
        if manual is None:
            return None
        applied = self._applied_rpm
        target = MotorRpm(0) if self._ending is not None else self._manual_target
        frozen = self.standing_action is SafetyAction.FREEZE
        held = frozen and target not in (0, applied)
        return ManualView(
            occupancy=manual.occupancy,
            target=self._speed_view(target),
            ceiling=self._speed_view(manual.ceiling),
            min_run=self._speed_view(self._motion.min_run),
            ramping=applied != target and not held,
            ramp_eta=(
                None
                if held
                else ramp_duration(applied, target, self._motion, self._motion_geometry)
            ),
        )
