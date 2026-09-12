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
   controller is reached from exactly two of the seven arms of one ``match``
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

* **The drive's state at startup.** ``restart: unless-stopped`` in
  ``docker-compose`` means a crashed process comes back to a machine it did not
  leave. If ETA reports ``OPERATION_ENABLED``, a previous process died with the
  motor commanded: zero the reference, ramp-stop, latch, refuse to start, and
  require a named operator acknowledgement. There is no automatic fault reset
  and no automatic resumption of motion anywhere in this file.
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

ONE RUNTIME PER SESSION
-----------------------

A :class:`TrainingRuntime` runs one programme and then reports ``FINISHED``.
That follows from the objects it owns: the safety supervisor is documented as
one instance per session, and ``Phase.DONE`` says a new session is a new
object. :meth:`TrainingRuntime.acknowledge` clears the latches so the machine
can be put back into service - it never resumes the session it cleared.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, assert_never, final

from src.clock import Clock
from src.motor.drive import (
    LFT_FAULT_CODES,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    FaultReport,
    StopUnconfirmed,
    UnexpectedState,
    describe_fault,
)
from src.result import Err, Ok, Result
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
from src.training.plan import (
    COMMISSIONED_DECEL_S,
    MIN_RECOVERY_S,
    NAMEPLATE_BASE_HERTZ,
    NAMEPLATE_MOTOR_RPM,
    Program,
)
from src.training.safety import (
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
from src.training.types import (
    DRIVE_STATUS_STALE_AFTER,
    ControlDecision,
    HeartRateSample,
    Phase,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    SpeedView,
    TelemetrySnapshot,
    ZoneCounters,
)
from src.units import (
    Bpm,
    GearRatio,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    OutOfRange,
    RawRegister,
    RpmPerSecond,
    Seconds,
    elapsed,
)

_logger: Final[logging.Logger] = logging.getLogger(__name__)


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


DEFAULT_EMERGENCY_BUDGET: Final[Seconds] = Seconds(0.5)
"""How long :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking` may take.

Half a second is two and a half ticks of the 5 Hz loop, and it is spent
blocking the event loop on purpose: this call *is* the stop, and handing it to
an executor would hand it to the same loop that may be the thing that failed.
"""

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
class MachineGeometry:
    """The rotating geometry every displayed speed is derived through.

    Keyword-only, and with no defaults for the two that matter, for exactly the
    reason :meth:`~src.training.types.SpeedView.from_motor_rpm` gives: a wrong
    gear ratio is a fiftyfold error and a wrong radius is a quadratic one, and a
    default value is how a wrong one gets used without anybody choosing it.

    The nameplate point *does* default, because it is a property of the motor
    that is bolted on (SEW KA37 DRS71S4: 1380 rpm at 50 Hz) rather than of the
    installation.
    """

    ratio: GearRatio
    """Gearbox reduction, motor turns per output turn. SEW KA37: 49.79."""

    radius: Metres
    """Distance from the axis to the occupant, where the g load is quoted."""

    nominal_rpm: MotorRpm = NAMEPLATE_MOTOR_RPM
    base_hz: Hertz = NAMEPLATE_BASE_HERTZ

    def __post_init__(self) -> None:
        """Refuse geometry that cannot describe a machine.

        Raises rather than returning a ``Result``: this is built at setup with
        nothing spinning, and refusing to start is the right answer to numbers
        nobody can vouch for. Contrast the drive path, where a raise would
        unwind with the motor commanded.
        """
        _require_positive("ratio", self.ratio)
        _require_positive("radius", self.radius)
        _require_positive("nominal_rpm", float(self.nominal_rpm))
        _require_positive("base_hz", self.base_hz)


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

    emergency_budget: Seconds = DEFAULT_EMERGENCY_BUDGET
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

    def __post_init__(self) -> None:
        """Refuse budgets that would silently disable the thing they bound."""
        _require_positive("slew", self.slew)
        _require_positive("emergency_budget", self.emergency_budget)
        _require_positive("ramp_settle", self.ramp_settle)
        _require_positive("status_stale_after", self.status_stale_after)
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


type StartRefusal = (
    AlreadyStarted
    | NotAttested
    | SafetyStanding
    | LimitsMismatch
    | PlanUnusable
    | DriveUnavailable
    | DrivePrecommanded
    | DriveInFault
)
"""Every way :meth:`TrainingRuntime.start` can refuse. Closed; match it nested."""


# =========================================================================
# Drive failures, classified once
# =========================================================================


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


def describe_drive_error(error: DriveError) -> DriveFailure:
    """Classify a drive error. Exhaustive, so a new variant fails the build here.

    One match for the whole module. The alternative - a match per call site -
    is seven places to update and seven places to get the ``output_unknown``
    reading wrong.
    """
    failure: DriveFailure
    match error:
        case CommTimeout(after):
            failure = DriveFailure(
                detail=f"the drive did not answer within {after:.3f} s",
                output_unknown=False,
            )
        case BadResponse(detail):
            failure = DriveFailure(
                detail=f"the drive's answer made no sense: {detail}",
                output_unknown=False,
            )
        case UnexpectedState(expected, actual):
            failure = DriveFailure(
                detail=f"the drive is in {actual.name} and the operation needed {expected.name}",
                output_unknown=False,
            )
        case DriveFaulted(fault, raw_code):
            failure = DriveFailure(
                detail=f"the drive is in fault: {fault.mnemonic} (LFT {raw_code})",
                output_unknown=False,
            )
        case OutOfRange(quantity, value, low, high):
            failure = DriveFailure(
                detail=f"{quantity} {value} is outside its domain {low}..{high}",
                output_unknown=False,
            )
        case EnableUnconfirmed(detail, reference_zeroed, run_command_removed):
            # The word that energises the output stage may have landed even
            # though its reply did not, and LFRD may still hold a setpoint from
            # an earlier session - so "energised" can mean "turning".
            failure = DriveFailure(
                detail=(
                    f"the output stage may be energised: {detail} "
                    f"(reference zeroed: {reference_zeroed}, "
                    f"run command removed: {run_command_removed})"
                ),
                output_unknown=True,
            )
        case StopUnconfirmed(waited, last_output_rpm, detail):
            failure = DriveFailure(
                detail=(
                    f"the shaft could not be shown to have stopped: still {last_output_rpm} rpm "
                    f"after {waited:.1f} s ({detail}); the run command was left in place so the "
                    "drive's own ttO timeout ramps it down"
                ),
                output_unknown=True,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return failure


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
    (:data:`~src.motor.drive.DriveFault.UNKNOWN`, ``NO_MOTOR``). That is the
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
        case _ as unreachable:
            assert_never(unreachable)


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
        "_attendant_last_seen",
        "_clock",
        "_comm_failures",
        "_controller",
        "_counters",
        "_decision",
        "_descent_from",
        "_drive",
        "_enabled",
        "_end_reason",
        "_ending",
        "_geometry",
        "_last_failure",
        "_last_sample",
        "_last_status",
        "_last_status_at",
        "_latched",
        "_limits",
        "_link_open",
        "_phase",
        "_previous_tick_at",
        "_program",
        "_recovery_from",
        "_resting_bpm",
        "_safety_limits",
        "_setpoint_changed_at",
        "_shutdown",
        "_silent",
        "_snapshot",
        "_started_at",
        "_stop_requested",
        "_subject",
        "_supervisor",
        "_tracker",
        "_warmup_satisfied",
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
    ) -> None:
        self._clock: Clock = clock
        self._drive: DriveBackend = drive
        self._geometry: MachineGeometry = geometry
        self._limits: RuntimeLimits = limits
        self._safety_limits: SafetyLimits = safety
        self._supervisor: SafetySupervisor = SafetySupervisor(clock=clock, limits=safety)
        self._tracker: HeartRateTracker = HeartRateTracker(tracker_limits)

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
        self._warmup_satisfied: bool = False
        self._resting_bpm: Bpm | None = None

        # --- what is believed about the drive ----------------------------
        self._link_open: bool = False
        self._enabled: bool = False
        self._silent: bool = False
        # The setpoint believed to be in force. Advanced only when a write was
        # acknowledged, so a failed write leaves the previous value standing -
        # which is what it is - and the next keepalive re-asserts it.
        self._applied_rpm: MotorRpm = MotorRpm(0)
        self._last_status: DriveStatus | None = None
        self._last_status_at: Monotonic | None = None
        self._comm_failures: int = 0
        self._last_failure: DriveFailure | None = None
        self._setpoint_changed_at: Monotonic | None = None
        self._descent_from: Monotonic | None = None

        # --- evidence and output ----------------------------------------
        self._last_sample: HeartRateSample | None = None
        self._decision: ControlDecision | None = None
        self._counters: ZoneCounters = ZoneCounters(
            in_zone=Seconds(0.0), above_zone=Seconds(0.0), below_zone=Seconds(0.0)
        )
        self._attendant_last_seen: Monotonic | None = None
        self._previous_tick_at: Monotonic | None = None
        self._latched: SafetyVerdict | None = None
        self._shutdown: ShutdownReport | None = None
        self._snapshot: TelemetrySnapshot = self._build_snapshot(clock.monotonic())

    # =====================================================================
    # Reads
    # =====================================================================

    @property
    def state(self) -> RuntimeState:
        """Where this runtime is. Derived from the fields, never stored twice."""
        if self._end_reason is None:
            return RuntimeState.IDLE if self._program is None else RuntimeState.RUNNING
        if self._phase is Phase.DONE and not self._enabled:
            return RuntimeState.FINISHED
        return RuntimeState.ENDING

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

        **The drive's state is read, never assumed.** ``restart:
        unless-stopped`` in ``docker-compose`` guarantees that a crashed process
        comes back to a machine it did not leave; if ETA reports
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
            "session started: profile=%s rev=%d subject=%s operator=%s total=%.0f s",
            program.source_profile_id,
            program.source_rev,
            subject.subject_id,
            subject.operator,
            program.total_duration_s,
        )
        return Ok(self._snapshot)

    def _refuse_start(self, program: Program) -> StartRefusal | None:
        """Every precondition that can be judged without touching the drive."""
        if self._program is not None or self._shutdown is not None:
            return AlreadyStarted(self.state)
        attested = self._supervisor.require_estop_confirmed()
        if isinstance(attested, Err):
            return NotAttested(attested.error.statement)
        standing = self.standing
        if standing is not None:
            return SafetyStanding(standing)
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
        opened = await self._drive.open()
        if isinstance(opened, Err):
            failure = self._note_failure(opened.error)
            return Err(DriveUnavailable(f"the drive link could not be opened: {failure.detail}"))
        self._link_open = True
        self._note_success()
        status = await self._drive.read_status()
        if isinstance(status, Err):
            failure = self._note_failure(status.error)
            return Err(DriveUnavailable(f"the drive could not be read: {failure.detail}"))
        self._note_success()
        self._last_status = status.value
        self._last_status_at = now
        return Ok(status.value)

    async def _arm(self, now: Monotonic, status: DriveStatus) -> StartRefusal | None:
        """Judge what was read, then energise the output stage with a zero reference."""
        if status.state is DriveState.OPERATION_ENABLED:
            return await self._refuse_precommanded(now, status)
        if status.fault_present:
            # No automatic fault reset, anywhere. "Reset it and see" with a
            # person inside the machine is how a short circuit becomes a fire.
            return DriveInFault(report=fault_report(status.fault), state=status.state)
        return await self._energise(now)

    async def _refuse_precommanded(self, now: Monotonic, status: DriveStatus) -> StartRefusal:
        """A previous process died with the motor commanded. Stop it, latch, refuse.

        The reference goes to zero and the run command is *kept*: that is the
        fastest stop this machine has, because a stop commanded faster than the
        commissioned ramp trips overvoltage into freewheel, and removing the run
        command from a turning shaft is the same hazard by another route. The
        ramp-stop word and then the shutdown word are issued later, by
        :meth:`_settle`, once RFRD shows standstill.
        """
        self._enabled = True
        await self._exchange_write_speed(now, MotorRpm(0))
        self._latch(
            now,
            RULE_DRIVE_PRECOMMANDED,
            SafetyAction.QUICK_STOP,
            (
                f"the drive was already in OPERATION_ENABLED at {status.output_rpm} rpm: a "
                "previous process died with the motor commanded. The reference has been zeroed "
                "and the run command left in place so the drive ramps it down; a named operator "
                "must acknowledge this before any session runs"
            ),
        )
        self._begin_ending(now, EndReason.EMERGENCY_STOP, self._latched)
        return DrivePrecommanded(state=status.state, output_rpm=status.output_rpm)

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
        interval = self._interval(now)
        # 1. THE KEEPALIVE FIRST.
        await self._service_drive(now)
        self._advance_phase(now)
        # 2. Safety, and its verdict.
        verdict = self._supervisor.evaluate(self._observe(now))
        standing = self._worst(self._latched, verdict)
        # 3. The command. The verdict decides; it reaches the controller only
        #    through the two arms that cap it.
        await self._command(now, standing)
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
        """
        if self._silent or not self._link_open:
            return
        await self._exchange_write_speed(now, self._applied_rpm)
        status = await self._drive.read_status()
        if isinstance(status, Err):
            self._note_failure(status.error)
            return
        self._note_success()
        self._last_status = status.value
        self._last_status_at = now

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
        """
        if self._stop_requested is not None:
            self._begin_ending(now, EndReason.OPERATOR_STOP, None)
        ending = self._ending
        if ending is not None:
            self._phase = self._ending_phase(now, ending)
        elif self._program is not None:
            self._phase = self._nominal_phase(now, self._program)
        self._latch_resting_rate(now)

    def _nominal_phase(self, now: Monotonic, program: Program) -> Phase:
        """Where the programme's own timeline says this session is.

        The timeline is the *nominal* answer, and this method is allowed to leave
        it in one direction only: WARMUP ends early once the heart rate has
        reached the zone, which is what ``warmup_max_s`` means by "max". Leaving
        early lengthens HOLD rather than shortening the session, because HOLD
        still ends where the timeline says it does.
        """
        phase, _, _ = program.phase_at(self._session_elapsed(now))
        if phase is Phase.WARMUP:
            if self._warmup_satisfied:
                return Phase.HOLD
            reached = self._tracker.usable(now)
            if reached is not None and reached >= program.profile.zone_low_bpm:
                self._warmup_satisfied = True
                _logger.info("warmup ended early at %d bpm: the zone was reached", reached)
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

    def _observe(self, now: Monotonic) -> SafetyObservation:
        """Everything the rules are allowed to judge, for this instant.

        Note what is absent, and that it is absent by construction rather than by
        omission: no desired speed, no ``ControlDecision``, no error. There is no
        field through which the control law's opinion can reach a rule, which is
        the whole reason the two layers exist.

        ``measured_rpm`` and ``current`` go to ``None`` the moment the status is
        stale, never to a fabricated zero - a made-up 0 rpm is exactly the lie
        that would make ``no_load`` and ``reverse_rotation`` judge a machine that
        is not there.
        """
        status = self._readable_status(now)
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
            fault=None if status is None else fault_report(status.fault),
            consecutive_comm_failures=self._comm_failures,
            attendant_last_seen=self._attendant_last_seen,
        )

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
        _logger.error("runtime latched %s by rule %s: %s", action.name, rule, detail)

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
            case _ as unreachable:
                assert_never(unreachable)

    # =====================================================================
    # Commanding
    # =====================================================================

    async def _command(self, now: Monotonic, standing: SafetyVerdict | None) -> None:
        """Apply the standing verdict, which is the only thing that sets a setpoint.

        Exhaustive over :class:`~src.training.types.SafetyAction`, so a new
        action fails the type check here rather than falling into whichever
        branch happened to be last.
        """
        action = SafetyAction.NONE if standing is None else standing.action
        match action:
            case SafetyAction.NONE:
                self._descent_from = None
                await self._follow_controller(now, allow_increase=True, cap=None)
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
                self._decision = None
                await self._apply_setpoint(now, MotorRpm(0))
            case SafetyAction.GO_SILENT:
                ending = self._begin_ending(now, EndReason.SAFETY_VERDICT, standing)
                self._decision = None
                self._go_silent(now, ending.detail)
            case _ as unreachable:
                assert_never(unreachable)

    async def _follow_controller(
        self, now: Monotonic, *, allow_increase: bool, cap: MotorRpm | None
    ) -> None:
        """Step 3: the controller's demand, capped at what the verdict permits.

        With no programme there is no controller, and the only honest demand is
        zero - so an idle runtime still writes a zero reference every tick, and
        still proves the link.
        """
        controller = self._controller
        if controller is None:
            self._decision = None
            await self._apply_setpoint(now, MotorRpm(0) if cap is None else cap)
            return
        step = controller.update(now, self._control_input(now), allow_increase=allow_increase)
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
        await self._apply_setpoint(now, demand if cap is None else MotorRpm(min(demand, cap)))

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
        """The next step of a controlled descent, at the configured slew rate.

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
        # Transition 5 (ramps, keeps control of the shaft) and only then
        # transition 2.
        if await self._exchange_command(ControlWord.SWITCH_ON) is not None:
            return
        if await self._exchange_command(ControlWord.SHUTDOWN) is not None:
            return
        self._enabled = False
        _logger.info("output stage disabled at standstill")

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
            _logger.warning("operator stop requested: %s", reason)

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

        Blocks the event loop for up to the configured budget, on purpose: this
        call *is* the stop, and handing it to an executor would hand it to the
        same loop that may be the thing that failed.

        The run command is deliberately not removed - see
        :meth:`~src.motor.drive.DriveBackend.emergency_disable_blocking`. The
        outcome is matched exhaustively because ``NOTHING_SENT`` must never look
        like success: it means the motor is still commanded at whatever setpoint
        it held, and somebody has to be told.
        """
        outcome = self._drive.emergency_disable_blocking(self._limits.emergency_budget)
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
        failure = self._note_failure(closed.error)
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
            return self._note_failure(outcome.error)
        self._note_success()
        if rpm != self._applied_rpm:
            self._applied_rpm = rpm
            self._setpoint_changed_at = now
        return None

    async def _exchange_command(self, word: ControlWord) -> DriveFailure | None:
        """Write CMD, and record what the exchange said about the link."""
        outcome = await self._drive.write_command(word)
        if isinstance(outcome, Err):
            return self._note_failure(outcome.error)
        self._note_success()
        return None

    def _note_failure(self, error: DriveError) -> DriveFailure:
        """Count a failed exchange and classify it once."""
        failure = describe_drive_error(error)
        self._comm_failures += 1
        self._last_failure = failure
        _logger.warning(
            "drive exchange failed (%d consecutive): %s", self._comm_failures, failure.detail
        )
        return failure

    def _note_success(self) -> None:
        """Reset the failure run length. It is a run length, not a total."""
        self._comm_failures = 0

    # =====================================================================
    # Derived facts about the session and the machine
    # =====================================================================

    def _session_elapsed(self, now: Monotonic) -> Seconds:
        """Time since the session started; zero before it has."""
        started = self._started_at
        return Seconds(0.0) if started is None else elapsed(started, now)

    def _total_duration(self) -> Seconds:
        """The programme's intended length; zero when there is no programme."""
        program = self._program
        return Seconds(0.0) if program is None else program.total_duration_s

    def _cooldown_s(self) -> Seconds:
        """How long an abort may spend bringing the machine down.

        The profile's own cooldown length, or the commissioned deceleration when
        there is no profile - an abort can happen before any programme exists,
        and this machine's stop still takes as long as it takes.
        """
        program = self._program
        return COMMISSIONED_DECEL_S if program is None else program.profile.cooldown_s

    def _recovery_s(self) -> Seconds:
        """How long monitoring continues with nothing turning."""
        program = self._program
        return MIN_RECOVERY_S if program is None else program.profile.recovery_s

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
        geometry = self._geometry
        return SpeedView.from_motor_rpm(
            rpm,
            ratio=geometry.ratio,
            radius=geometry.radius,
            nominal_rpm=geometry.nominal_rpm,
            base_hz=geometry.base_hz,
        )

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
        status = self._last_status
        readable = self._readable_status(now)
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
            drive_status_age=self._status_age(now),
            current=None if readable is None else readable.current,
            fault=None if readable is None else fault_report(readable.fault),
            safety=self.standing,
            counters=self._counters,
        )
