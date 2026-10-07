"""Tests for the session runtime.

The headline test is :func:`test_no_exit_path_leaves_the_motor_running`,
parametrized over every way a session can end. Everything else in this file
exists to make that test's fake drive trustworthy enough for its assertion to
mean something, or to bound one behaviour of the runtime that the exit-path test
does not see.

Three things about the fake drive are worth reading before the tests:

1. **It models the drive's own ``ttO`` timeout.** That is not decoration. Two
   exit paths - a dead link and an exception inside the tick - end by the
   runtime *ceasing to write*, and the only thing that stops the motor after
   that is a timer on the other side of the serial link. A fake without ``ttO``
   would let those two paths assert "the runtime stopped talking" and call it a
   stop. With it, they assert the motor actually ended up disabled.

2. **Command word 6 freewheels and word 7 ramps**, as on the real drive. A fake
   in which both stop the shaft would silently bless a teardown sequence that
   drops the output stage on a turning centrifuge.

3. **Every call is recorded, in order.** The tick's ordering guarantee - the
   keepalive before anything is decided - is a claim about the sequence of
   frames, so it is asserted on the sequence of frames.

Nothing here touches hardware and nothing reads the system clock: every test
runs on a :class:`~src.clock.ManualClock`, and the plant is integrated from the
instants the test hands it.
"""

from __future__ import annotations

import ast
import math
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import FrozenInstanceError, dataclass, field, replace
from enum import Enum, auto, unique
from itertools import pairwise
from pathlib import Path
from typing import Final, assert_never, cast, final

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.geometry import MachineGeometry
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
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
    DriveParameter,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    FaultReport,
    HighSpeedAboveCeiling,
    HighSpeedAboveMaxFrequency,
    LowSpeedNotZero,
    StopUnconfirmed,
    UnexpectedState,
    decode_status_word,
    describe_fault,
)
from src.result import Err, Ok, Result, is_err, is_ok
from src.training.hr_control import (
    Gains,
    HeartRateTracker,
    StaleSequence,
    TrackerLimits,
)
from src.training.motion import DEFAULT_MOTION_LIMITS, MotionLimits, motor_rate_limit
from src.training.plan import INITIAL_REV, Channel, Program, TrainingProfile
from src.training.runtime import (
    IDLE_POLL_PERIOD,
    RULE_DISABLE_REFUSED,
    RULE_DRIVE_PRECOMMANDED,
    RULE_ENABLE_UNCONFIRMED,
    RULE_TICK_EXCEPTION,
    AlreadyStarted,
    DriveFailure,
    DriveInFault,
    DriveParameterRefused,
    DrivePrecommanded,
    DriveUnavailable,
    Ending,
    EndReason,
    IdleLink,
    LimitsMismatch,
    NotAttested,
    PlanUnusable,
    RecordStorageLow,
    RuntimeLimits,
    RuntimeState,
    SafetyStanding,
    ShutdownReport,
    StartRefusal,
    Subject,
    TrainingRuntime,
    describe_drive_error,
    fault_report,
    motion_geometry,
    motion_is_over,
)
from src.training.safety import (
    RULE_ATTENDANT_ABSENT,
    RULE_COMMS_LOST,
    RULE_HR_STALE,
    RULE_SESSION_STANDSTILL,
    AcknowledgeRefusal,
    EmergencyStopStillLatched,
    GoSilentIsTerminal,
    NothingLatched,
    SafetyAcknowledgement,
    SafetyLimits,
    SafetyObservation,
    SafetySupervisor,
    Unattributed,
)
from src.training.types import (
    HeartRateSample,
    Phase,
    SafetyAction,
    SafetyVerdict,
    SignalQuality,
    TelemetrySnapshot,
    is_rule_id,
)
from src.units import (
    Amperes,
    Bpm,
    BpmPerMinute,
    GearRatio,
    GLoadPerSecond,
    Hertz,
    Metres,
    Monotonic,
    MotorRpm,
    OutOfRange,
    OutputRpmPerSecond,
    RawRegister,
    RpmPerSecond,
    Seconds,
    StatusWord,
    UnixMillis,
)

RUNTIME_SOURCE: Final[Path] = (
    Path(__file__).resolve().parent.parent / "src" / "training" / "runtime.py"
)

TICK: Final[Seconds] = Seconds(0.2)
"""The control period: 5 Hz, as the safety limits' own default says."""

OPERATOR: Final[str] = "Dr Ada Okonkwo"

GEOMETRY: Final[MachineGeometry] = MachineGeometry(ratio=GearRatio(49.79), radius=Metres(1.5))

# Gains tuned for an accelerated rig, NOT this machine's tuning. The machine's
# tuning is exercised by tests/test_hr_control.py's plant sweep; here the point
# is to make a two-minute session move at all, so the control period is one
# second rather than five and the proportional gain is larger to match.
RIG_GAINS: Final[Gains] = Gains(
    kp=8.0, ti=Seconds(10.0), period=Seconds(1.0), step_cap=Seconds(2.0)
)

LIMITS: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(70.0), start_hysteresis_rpm=MotorRpm(10), gains=RIG_GAINS
)
"""The accelerated rig's limits.

``slew`` is 70 rpm/s rather than 60 for a reason worth recording: leaving zero
costs ``min_run_rpm + start_hysteresis_rpm`` = 65 rpm in a single step, and one
control period may not authorise less than that or the machine can never start.
``ControlPlan`` checks the weaker condition (one period must buy
``min_run_rpm``), so a configuration in the 10 rpm gap between the two passes
validation and then never turns the motor. Reported upstream; here the rig
simply stays out of the gap.
"""

RIG_MOTION: Final[MotionLimits] = MotionLimits(
    output_accel=OutputRpmPerSecond(1.4),
    g_rate=GLoadPerSecond(0.5),
    min_run=MotorRpm(55),
)
"""The accelerated rig's anti-nausea limits: every programme setpoint change now walks them.

1.4 output rpm/s is 69.7 motor rpm/s, just under the rig's 70 rpm/s slew, so the
motion limiter never outruns the slew guarantee these tests assert and the rig
still reaches its ceiling in seconds. The machine's own limits (0.25 output
rpm/s, 0.03 g/s) are exercised by the motion tests further down.
"""

REAL_LIMITS: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10)
)
"""This machine's limits, with the control law's own default gains.

Used by the whole-session tests, so those run the tuning that will actually be
commissioned - a five-second control period and a 15 rpm/s ramp - rather than
the accelerated rig's.
"""


# =========================================================================
# The fake drive
# =========================================================================


@unique
class FakeState(Enum):
    """The CiA402 states this fake passes through."""

    SWITCH_ON_DISABLED = auto()
    READY = auto()
    SWITCHED_ON = auto()
    OPERATION_ENABLED = auto()
    FAULT = auto()


#: The ETA word each state reports. Real words, decoded by the real decoder, so
#: the state the runtime sees is produced the way the hardware path produces it.
ETA_WORDS: Final[dict[FakeState, StatusWord]] = {
    FakeState.SWITCH_ON_DISABLED: StatusWord(0x0040),
    FakeState.READY: StatusWord(0x0021),
    FakeState.SWITCHED_ON: StatusWord(0x0023),
    FakeState.OPERATION_ENABLED: StatusWord(0x0027),
    FakeState.FAULT: StatusWord(0x0038),
}

_LEGAL: Final[dict[tuple[FakeState, ControlWord], FakeState]] = {
    (FakeState.SWITCH_ON_DISABLED, ControlWord.SHUTDOWN): FakeState.READY,
    (FakeState.READY, ControlWord.SHUTDOWN): FakeState.READY,
    (FakeState.SWITCHED_ON, ControlWord.SHUTDOWN): FakeState.READY,
    # Transition 8: the hazard. Out of OPERATION_ENABLED word 6 DROPS the
    # output stage, so the shaft coasts (see _integrate).
    (FakeState.OPERATION_ENABLED, ControlWord.SHUTDOWN): FakeState.READY,
    (FakeState.READY, ControlWord.SWITCH_ON): FakeState.SWITCHED_ON,
    (FakeState.SWITCHED_ON, ControlWord.SWITCH_ON): FakeState.SWITCHED_ON,
    # Transition 5: this drive ramps, keeping control of the shaft.
    (FakeState.OPERATION_ENABLED, ControlWord.SWITCH_ON): FakeState.SWITCHED_ON,
    (FakeState.SWITCHED_ON, ControlWord.ENABLE_OPERATION): FakeState.OPERATION_ENABLED,
    (FakeState.OPERATION_ENABLED, ControlWord.ENABLE_OPERATION): FakeState.OPERATION_ENABLED,
    (FakeState.FAULT, ControlWord.FAULT_RESET): FakeState.SWITCH_ON_DISABLED,
}

_REQUIRES: Final[dict[ControlWord, DriveState]] = {
    ControlWord.SHUTDOWN: DriveState.SWITCH_ON_DISABLED,
    ControlWord.SWITCH_ON: DriveState.READY,
    ControlWord.ENABLE_OPERATION: DriveState.SWITCHED_ON,
    ControlWord.FAULT_RESET: DriveState.FAULT,
}


BENCH_LIMITS: Final[DriveLimits] = DriveLimits(
    max_frequency=Hertz(60.0),
    high_speed=Hertz(50.0),
    low_speed=Hertz(0.0),
    acceleration=Seconds(3.0),
    deceleration=Seconds(3.0),
)
"""tFr/HSP/LSP/ACC/dEC exactly as the bench read them today."""


FAKE_EMERGENCY_BUDGET: Final[Seconds] = Seconds(0.37)
"""What :class:`FakeDrive` says its emergency write can be bounded by."""


@final
class FakeDrive:
    """A ``DriveBackend`` with a state machine, a shaft, and a ``ttO`` timeout.

    Deliberately not a second copy of ``src/motor/simulated.py``: it records
    every call so the tick's ordering can be asserted on the frames themselves,
    and it lets a test make any single exchange fail, which is what the
    comms-loss and enable-unconfirmed paths need.

    Where its behaviour is a guess it guesses the *stricter* option, so software
    that satisfies this model also satisfies the hardware and never the reverse:
    word 6 on a turning shaft freewheels, and only writes feed ``ttO``.
    """

    __slots__ = (
        "_address_proven",
        "_clock",
        "_coast_tau",
        "_comms_down",
        "_established",
        "_fault",
        "_last_at",
        "_last_write_at",
        "_link_open",
        "_possible_frames",
        "_ramp",
        "_rpm",
        "_setpoint",
        "_state",
        "_tto",
        "close_error",
        "command_errors",
        "commands",
        "emergency_budgets",
        "emergency_calls",
        "emergency_override",
        "limits",
        "limits_error",
        "raise_on_status",
        "speed_error",
        "status_error",
        "swallow_writes",
        "trace",
        "writes",
    )

    def __init__(
        self,
        clock: ManualClock,
        *,
        state: FakeState = FakeState.SWITCH_ON_DISABLED,
        rpm: MotorRpm = MotorRpm(0),
        ramp: float = 300.0,
        tto: Seconds = Seconds(2.0),
        coast_tau: Seconds = Seconds(60.0),
    ) -> None:
        self._clock: ManualClock = clock
        self._state: FakeState = state
        self._rpm: float = float(rpm)
        self._setpoint: MotorRpm = rpm
        self._ramp: float = ramp
        self._tto: Seconds = tto
        self._coast_tau: Seconds = coast_tau
        self._last_at: Monotonic = clock.monotonic()
        self._last_write_at: Monotonic = clock.monotonic()
        self._established: bool = False
        self._link_open: bool = False
        self._possible_frames: int = 0
        self._address_proven: bool = False
        self._comms_down: bool = False
        self._fault: FaultReport | None = None

        # --- what a test injects ----------------------------------------
        self.speed_error: DriveError | None = None
        self.command_errors: dict[ControlWord, DriveError] = {}
        self.status_error: DriveError | None = None
        self.close_error: DriveError | None = None
        self.emergency_override: EmergencyStopOutcome | None = None
        self.limits: DriveLimits = BENCH_LIMITS
        self.limits_error: DriveError | None = None
        self.raise_on_status: bool = False
        self.swallow_writes: bool = False

        # --- what a test reads ------------------------------------------
        self.trace: list[str] = []
        self.writes: list[MotorRpm] = []
        self.commands: list[ControlWord] = []
        self.emergency_calls: int = 0
        self.emergency_budgets: list[Seconds] = []

    # -- what the tests assert on -----------------------------------------

    @property
    def output_enabled(self) -> bool:
        """Whether the output stage is on, i.e. whether the motor can be driven."""
        return self._state is FakeState.OPERATION_ENABLED

    def is_enabled(self) -> bool:
        """The same fact through a CALL rather than a property access.

        A test that asserts "it was enabled, then it was not" is exactly the
        test worth writing, and mypy narrows a property to the literal the first
        assertion established and then calls the second one unreachable. Reading
        through a method re-widens the type on every call.
        """
        return self.output_enabled

    @property
    def commanded_rpm(self) -> MotorRpm:
        """The speed reference the drive currently holds."""
        return self._setpoint

    @property
    def shaft_rpm(self) -> float:
        """The shaft speed, which is not the reference and not the state."""
        return self._rpm

    @property
    def state(self) -> FakeState:
        return self._state

    def break_comms(self) -> None:
        """Every exchange from now on fails, as a severed link does."""
        self._comms_down = True

    def inject_fault(self, fault: DriveFault) -> None:
        """Latch a fault. The reaction is a ramp to stop, keeping control."""
        code = next(raw for raw, named in LFT_FAULT_CODES.items() if named is fault)
        self._fault = describe_fault(code)
        self._state = FakeState.FAULT

    # -- the plant ---------------------------------------------------------

    def advance(self, now: Monotonic) -> None:
        """Integrate every modelled process up to ``now``. Idempotent, never raises."""
        if now <= self._last_at:
            return
        dt = float(now - self._last_at)
        self._last_at = now
        self._integrate(dt)
        self._check_watchdog()

    def _integrate(self, dt: float) -> None:
        target = float(self._setpoint) if self.output_enabled else 0.0
        if self.output_enabled or self._fault_ramping():
            step = self._ramp * dt
            self._rpm += max(-step, min(step, target - self._rpm))
        else:
            # No torque: ~420 J of rotating mass against a bus that absorbs ~11 J.
            self._rpm *= math.exp(-dt / self._coast_tau)

    def _fault_ramping(self) -> bool:
        """A ramp-to-stop fault reaction keeps driving the shaft down."""
        return self._state is FakeState.FAULT and self._fault is not None

    def _check_watchdog(self) -> None:
        """The drive's own ttO: silence for ``tto`` latches SLF and stops the motor.

        Only *writes* feed it, which is stricter than an ATV320 (any frame
        resets the real one) and therefore safe: software that keeps a write
        keepalive alive satisfies both.
        """
        if not self._established or self._state is FakeState.FAULT:
            return
        if float(self._last_at - self._last_write_at) < self._tto:
            return
        self.inject_fault(DriveFault.MODBUS_COMM_LOSS)

    def _note(self, label: str, *, is_write: bool) -> None:
        self._possible_frames += 1
        self.trace.append(label)
        if is_write or not self._established:
            self._last_write_at = self._last_at
        self._established = True

    def _transport(self) -> DriveError | None:
        if not self._link_open:
            return BadResponse(detail="the Modbus link is not open")
        if self._comms_down:
            return CommTimeout(after=Seconds(0.5))
        return None

    # -- DriveBackend ------------------------------------------------------

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return AcquisitionEvidence(self._possible_frames, self._address_proven)

    async def open(self) -> Result[None, DriveError]:
        self.advance(self._clock.monotonic())
        self._link_open = True
        self.trace.append("open")
        if self._comms_down:
            return Err(CommTimeout(after=Seconds(0.5)))
        self._note("open:ack", is_write=False)
        self._address_proven = True
        return Ok(None)

    async def close(self) -> Result[None, DriveError]:
        """Stop properly, then release the link. Idempotent.

        Models a *conforming* close, which the seam requires: zero the
        reference, wait for standstill, and only then remove the run command.
        The bounded wait is modelled by driving the shaft to rest here - the
        point being tested is that a conforming close leaves the output stage
        off, not how long it took.
        """
        self.advance(self._clock.monotonic())
        self.trace.append("close")
        error = self.close_error
        if error is not None:
            return Err(error)
        self._setpoint = MotorRpm(0)
        self._rpm = 0.0
        if self._state is FakeState.OPERATION_ENABLED:
            self._state = FakeState.SWITCHED_ON
        if self._state is FakeState.SWITCHED_ON:
            self._state = FakeState.READY
        self._link_open = False
        return Ok(None)

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self.advance(self._clock.monotonic())
        self.trace.append(f"cmd:{word.name}")
        error = self.command_errors.get(word) or self._transport()
        if error is not None:
            return Err(error)
        self._note(f"cmd:{word.name}:ack", is_write=True)
        latched = self._fault
        if latched is not None and (word is not ControlWord.FAULT_RESET or abs(self._rpm) >= 1.0):
            return Err(DriveFaulted(fault=latched.fault, raw_code=latched.raw_code))
        target = _LEGAL.get((self._state, word))
        if target is None:
            return Err(UnexpectedState(expected=_REQUIRES[word], actual=self._drive_state()))
        self._state = target
        self.commands.append(word)
        if word is ControlWord.FAULT_RESET:
            self._fault = None
        return Ok(None)

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self.advance(self._clock.monotonic())
        self.trace.append(f"lfrd:{rpm}")
        error = self.speed_error or self._transport()
        if error is not None:
            return Err(error)
        self._note(f"lfrd:{rpm}:ack", is_write=True)
        self.writes.append(rpm)
        if self.swallow_writes:
            # A Modbus write response echoes the request, so a write to the
            # WRONG ADDRESS is acknowledged while the speed reference never
            # moves. This is that failure, and it is the reason the runtime
            # takes its control base from LFRD read back.
            return Ok(None)
        self._setpoint = rpm
        return Ok(None)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self.advance(self._clock.monotonic())
        self.trace.append("eta")
        if self.raise_on_status:
            raise RuntimeError("the transport blew up mid-read")
        error = self.status_error or self._transport()
        if error is not None:
            return Err(error)
        self._note("eta:ack", is_write=False)
        latched = self._fault
        word = ETA_WORDS[self._state]
        return Ok(
            DriveStatus(
                state=decode_status_word(word),
                status_word=word,
                setpoint_echo_rpm=self._setpoint,
                output_rpm=MotorRpm(round(self._rpm)),
                current=self._current(),
                fault=None if latched is None else latched.fault,
            )
        )

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        self.advance(self._clock.monotonic())
        self.trace.append("limits")
        error = self.limits_error or self._transport()
        if error is not None:
            return Err(error)
        self._note("limits:ack", is_write=False)
        return Ok(self.limits)

    @property
    def emergency_budget(self) -> Seconds:
        """A figure no module constant happens to equal, so a test can tell whose it is."""
        return FAKE_EMERGENCY_BUDGET

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self.advance(self._clock.monotonic())
        self.emergency_calls += 1
        self.emergency_budgets.append(timeout)
        self.trace.append("estop")
        override = self.emergency_override
        if override is not None:
            return override
        if not self._link_open:
            return EmergencyStopOutcome.NOTHING_SENT
        if self._comms_down or timeout <= 0.0:
            return EmergencyStopOutcome.SENT_UNCONFIRMED
        self._note("estop:ack", is_write=True)
        self._setpoint = MotorRpm(0)
        return EmergencyStopOutcome.ACKNOWLEDGED

    # -- internals ---------------------------------------------------------

    def _drive_state(self) -> DriveState:
        return decode_status_word(ETA_WORDS[self._state])

    def _current(self) -> Amperes:
        """Magnetising plus load. Zero with the output stage off, including coasting."""
        if not self.output_enabled:
            return Amperes(0.0)
        return Amperes(0.5 + 1.6 * abs(self._rpm) / 1380.0)


# =========================================================================
# The occupant
# =========================================================================


@final
class Occupant:
    """A first-order heart-rate response to centrifuge speed.

    ``hr -> resting + gain * rpm`` with a time constant, which is the shape the
    physiology notes describe (heart rate lags load by 30-60 s). Quantised to
    whole bpm and emitted at 1 Hz, because that is what the ECG pipeline
    actually delivers - an 8 s median refreshed once a second - and a controller
    tested against a continuous signal is a controller tested against a signal
    it will never see.
    """

    __slots__ = ("_bpm", "_gain", "_resting", "_tau")

    def __init__(self, *, resting: float = 82.0, gain: float = 0.20, tau: float = 90.0) -> None:
        self._resting: float = resting
        self._gain: float = gain
        self._tau: float = tau
        self._bpm: float = resting

    @property
    def bpm(self) -> Bpm:
        return Bpm(round(self._bpm))

    def advance(self, dt: float, rpm: float) -> None:
        target = self._resting + self._gain * abs(rpm)
        self._bpm += (target - self._bpm) * (1.0 - math.exp(-dt / self._tau))


# =========================================================================
# Profiles, programmes, rigs
# =========================================================================


def _profile(**overrides: object) -> TrainingProfile:
    """A valid profile. Overrides are splatted, so a test states only its point."""
    base: dict[str, object] = {
        "profile_id": "rig",
        "name": "rig",
        "total_duration_s": Seconds(130.0),
        "baseline_s": Seconds(10.0),
        "warmup_max_s": Seconds(20.0),
        "hold_min_s": Seconds(10.0),
        "cooldown_s": Seconds(10.0),
        "recovery_s": Seconds(60.0),
        "zone_low_bpm": Bpm(118),
        "zone_high_bpm": Bpm(138),
        "hard_max_bpm": Bpm(148),
        "critical_bpm": Bpm(158),
        "subject_hr_max": Bpm(162),
        "min_run_rpm": MotorRpm(55),
        "max_rpm": MotorRpm(276),
        "warmup_rpm_ceiling_fraction": 0.6,
        "channels": (Channel.ECG,),
    }
    base.update(overrides)
    return TrainingProfile(**base)  # type: ignore[arg-type]  # splat into a frozen record


def _program(profile: TrainingProfile | None = None) -> Program:
    return Program(
        profile=profile if profile is not None else _profile(),
        source_rev=INITIAL_REV,
        resolved_at=UnixMillis(1_700_000_000_000),
        total_overridden=False,
    )


def _safety_for(profile: TrainingProfile) -> SafetyLimits:
    return SafetyLimits(hard_max_bpm=profile.hard_max_bpm, critical_bpm=profile.critical_bpm)


def _lenient_comms() -> SafetyLimits:
    """The default profile's limits with ``comms_lost`` pushed out of reach.

    For the tests that isolate what one failing kind of exchange does to the
    *setpoint*. With the real limit, three consecutive failures of either kind
    end the session, which is the behaviour the comms tests assert - and which
    would end these tests before the thing they measure could happen.
    """
    profile = _profile()
    return SafetyLimits(
        hard_max_bpm=profile.hard_max_bpm,
        critical_bpm=profile.critical_bpm,
        comms_lost_failures=1000,
    )


@dataclass(slots=True)
class Rig:
    """One runtime, its fake drive, its clock, and the loop that drives them."""

    clock: ManualClock
    drive: FakeDrive
    runtime: TrainingRuntime
    program: Program
    occupant: Occupant | None = None
    fed_bpm: Bpm | None = None
    seq: int = 0
    last_feed: float = -1.0
    snapshots: list[TelemetrySnapshot] = field(default_factory=list)

    @property
    def now(self) -> Monotonic:
        return self.clock.monotonic()

    def phase(self) -> Phase:
        """Read the phase through a CALL rather than a property access.

        mypy narrows a property to the literal a previous assertion established
        and then reports the next assertion about the same expression as
        unreachable - so a test that checks "it was WARMUP, then it became HOLD"
        fails to type-check while being exactly the test worth writing. Reading
        through a method re-widens the type on every call.
        """
        return self.runtime.phase

    def state(self) -> RuntimeState:
        """Read the runtime state through a call, for the reason :meth:`phase` gives."""
        return self.runtime.state

    def standing_action(self) -> SafetyAction:
        """Read the standing action through a call, for the reason :meth:`phase` gives."""
        return self.runtime.standing_action

    def last_failure(self) -> DriveFailure | None:
        """Read the last drive failure through a call, for the reason :meth:`phase` gives."""
        return self.runtime.last_failure

    async def start(self, *, attest: bool = True) -> Result[TelemetrySnapshot, StartRefusal]:
        if attest:
            assert is_ok(self.runtime.confirm_estop_wiring(OPERATOR))
        return await self.runtime.start(self.program, Subject(subject_id="s-1", operator=OPERATOR))

    async def run(
        self, seconds: float, *, feed: bool = True, ping: bool = True
    ) -> list[TelemetrySnapshot]:
        """Tick for ``seconds`` of simulated time. Returns the snapshots produced."""
        return [await self.step(feed=feed, ping=ping) for _ in range(round(seconds / TICK))]

    async def step(self, *, feed: bool = True, ping: bool = True) -> TelemetrySnapshot:
        """One tick: advance the clock, the plant, the metric stream, then the runtime."""
        self.clock.advance(TICK)
        now = self.now
        occupant = self.occupant
        if occupant is not None:
            occupant.advance(float(TICK), self.drive.shaft_rpm)
        self.drive.advance(now)
        if feed and now - self.last_feed >= 1.0:
            self.last_feed = float(now)
            self.seq += 1
            self.runtime.observe_ecg(now, self.seq, SignalQuality.GOOD, self._bpm())
        if ping:
            self.runtime.presence_ping()
        snapshot = await self.runtime.tick(now)
        self.snapshots.append(snapshot)
        return snapshot

    def _bpm(self) -> Bpm | None:
        if self.fed_bpm is not None:
            return self.fed_bpm
        occupant = self.occupant
        return None if occupant is None else occupant.bpm

    def feed(self, bpm: Bpm | None, *, quality: SignalQuality = SignalQuality.GOOD) -> None:
        """Push one metric refresh out of band, advancing the sequence number."""
        self.seq += 1
        self.runtime.observe_ecg(self.now, self.seq, quality, bpm)


def _rig(
    *,
    profile: TrainingProfile | None = None,
    limits: RuntimeLimits = LIMITS,
    state: FakeState = FakeState.SWITCH_ON_DISABLED,
    rpm: MotorRpm = MotorRpm(0),
    tracker_limits: TrackerLimits | None = None,
    occupant: Occupant | None = None,
    tto: Seconds = Seconds(2.0),
    safety: SafetyLimits | None = None,
    shared_supervisor: bool = False,
    motion: MotionLimits = RIG_MOTION,
    limit_radius: Metres | None = None,
) -> Rig:
    clock = ManualClock(Monotonic(1000.0), UnixMillis(1_700_000_000_000))
    drive = FakeDrive(clock, state=state, rpm=rpm, tto=tto)
    resolved = profile if profile is not None else _profile()
    resolved_safety = _safety_for(resolved) if safety is None else safety
    runtime = TrainingRuntime(
        clock=clock,
        drive=drive,
        geometry=GEOMETRY,
        limits=limits,
        safety=resolved_safety,
        tracker_limits=tracker_limits,
        supervisor=(
            SafetySupervisor(clock=clock, limits=resolved_safety) if shared_supervisor else None
        ),
        motion=motion,
        limit_radius=limit_radius,
    )
    return Rig(
        clock=clock, drive=drive, runtime=runtime, program=_program(resolved), occupant=occupant
    )


async def _running_rig(**kwargs: object) -> Rig:
    """A rig started and driven until the setpoint is genuinely non-zero.

    The exit-path tests all need a machine that is actually turning, or "the
    motor was commanded to zero" is satisfied by a motor that was never
    commanded at all. The fed rate steps down at the end of BASELINE so the
    control law sees a real error and accelerates immediately.
    """
    rig = _rig(**kwargs)  # type: ignore[arg-type]  # keyword pass-through
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.fed_bpm = Bpm(65)
    await rig.run(9.0)
    assert rig.runtime.applied_rpm > 0, "the rig never started the motor"
    return rig


# =========================================================================
# The vocabulary this module adds
# =========================================================================


def test_the_rule_ids_are_well_formed() -> None:
    """Rule ids key a dashboard, an alert and a session log. Malformed is forever."""
    for rule in (
        RULE_DRIVE_PRECOMMANDED,
        RULE_TICK_EXCEPTION,
        RULE_ENABLE_UNCONFIRMED,
        RULE_DISABLE_REFUSED,
    ):
        assert is_rule_id(rule), rule


def test_the_fault_code_table_is_injective() -> None:
    """Inverting it is only sound if no two codes name the same fault.

    :func:`fault_report` recovers the raw LFT number by inverting
    ``LFT_FAULT_CODES``. If two codes mapped to one fault, the inversion would
    silently keep one of them and the operator would be shown a number the drive
    is not displaying.
    """
    faults = list(LFT_FAULT_CODES.values())
    assert len(faults) == len(set(faults))


@pytest.mark.parametrize("code", sorted(LFT_FAULT_CODES))
def test_every_named_fault_round_trips_to_its_own_code(code: RawRegister) -> None:
    """The report a status implies is the report the drive layer would have built."""
    expected = describe_fault(code)
    assert fault_report(expected.fault) == expected


@pytest.mark.parametrize("fault", [None, DriveFault.UNKNOWN])
def test_a_fault_with_no_code_reports_nothing_rather_than_a_made_up_number(
    fault: DriveFault | None,
) -> None:
    """An invented LFT number on the screen is worse than no number at all."""
    assert fault_report(fault) is None


@pytest.mark.parametrize(
    ("phase", "over"),
    [
        (Phase.BASELINE, False),
        (Phase.WARMUP, False),
        (Phase.HOLD, False),
        (Phase.COOLDOWN, True),
        (Phase.RECOVERY, True),
        (Phase.DONE, True),
    ],
)
def test_motion_is_over_classifies_every_phase(phase: Phase, over: bool) -> None:
    """BASELINE is on the "motion still to come" side, which is what keeps it enabled."""
    assert motion_is_over(phase) is over


@pytest.mark.parametrize(
    ("error", "unknown"),
    [
        (CommTimeout(after=Seconds(0.5)), False),
        (BadResponse(detail="bad crc"), False),
        (
            UnexpectedState(expected=DriveState.SWITCHED_ON, actual=DriveState.READY),
            False,
        ),
        (DriveFaulted(fault=DriveFault.OVERCURRENT, raw_code=RawRegister(15)), False),
        (OutOfRange(quantity="signed16", value=70000.0, low=-32768.0, high=32767.0), False),
        (
            EnableUnconfirmed(detail="ack lost", reference_zeroed=False, run_command_removed=False),
            True,
        ),
        (
            StopUnconfirmed(waited=Seconds(20.0), last_output_rpm=MotorRpm(420), detail="slow"),
            True,
        ),
    ],
)
def test_every_drive_error_is_classified_and_only_two_mean_the_motor_may_be_turning(
    error: DriveError, unknown: bool
) -> None:
    """The ``output_unknown`` reading is the whole reason this classifier exists."""
    failure = describe_drive_error(error)
    assert failure.output_unknown is unknown
    assert failure.detail


def test_the_classifier_names_the_thing_that_happened() -> None:
    """A detail that does not carry its numbers is unactionable at 2am."""
    assert "0.750" in describe_drive_error(CommTimeout(after=Seconds(0.75))).detail
    assert (
        "420 rpm"
        in describe_drive_error(
            StopUnconfirmed(waited=Seconds(20.0), last_output_rpm=MotorRpm(420), detail="slow")
        ).detail
    )


def test_the_end_reasons_are_stable_strings_not_ordinals() -> None:
    """An ``auto()`` value renumbers on a reorder and rewrites every stored session."""
    for reason in EndReason:
        assert isinstance(reason.value, str)
    for state in RuntimeState:
        assert isinstance(state.value, str)


def _assign(target: object, name: str, value: object) -> None:
    """Write an attribute through a parameter, so no checker can resolve it statically."""
    setattr(target, name, value)


@pytest.mark.parametrize(
    ("record", "sample"),
    [
        (Subject, Subject(subject_id="s", operator="o")),
        (MachineGeometry, GEOMETRY),
        (RuntimeLimits, LIMITS),
        (DriveFailure, DriveFailure(detail="d", output_unknown=False)),
        (
            Ending,
            Ending(
                reason=EndReason.SHUTDOWN,
                action=SafetyAction.NONE,
                detail="",
                at=Monotonic(0.0),
            ),
        ),
    ],
)
def test_the_published_records_are_frozen_and_slotted(record: type, sample: object) -> None:
    """A decision taken from a record must not be able to change under the decision."""
    name = next(iter(record.__annotations__))
    with pytest.raises(FrozenInstanceError):
        _assign(sample, name, None)
    # A slotted frozen dataclass raises TypeError rather than AttributeError for
    # an unknown name, because `slots=True` rebuilds the class and the generated
    # __setattr__'s zero-argument super() still closes over the original. The
    # property being asserted is that the write does not land, which the last
    # line checks directly.
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        _assign(sample, "invented_field", 1)
    assert not hasattr(sample, "__dict__")
    assert not hasattr(sample, "invented_field")


def test_the_runtime_depends_on_the_layers_below_it_and_never_reads_a_clock() -> None:
    """Parsed rather than imported, so the assertion is about the source itself.

    Two claims. The runtime sits on top of every other training module, which is
    why its import list is the longest in the package - but it must not reach
    past the drive *seam* into a transport, and it must not read the system
    clock (contract rules 4 and 5).
    """
    tree = ast.parse(RUNTIME_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert "time" not in imported
    assert not {name for name in imported if name.endswith(("atv320", "simulated", "pymodbus"))}
    assert "src.motor.drive" in imported


# =========================================================================
# Configuration
# =========================================================================


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
@pytest.mark.parametrize("name", ["ratio", "radius", "base_hz"])
def test_geometry_refuses_a_quantity_that_cannot_describe_a_machine(name: str, bad: float) -> None:
    """NaN is rejected FIRST: every comparison against it is false, so ``<= 0`` accepts it."""
    fields: dict[str, object] = {"ratio": GearRatio(49.79), "radius": Metres(1.5)}
    fields[name] = bad
    with pytest.raises(ValueError, match=name):
        MachineGeometry(**fields)  # type: ignore[arg-type]  # splat into a frozen record


def test_geometry_refuses_a_zero_nameplate_speed() -> None:
    """The nameplate point divides, so zero would make every frequency infinite."""
    with pytest.raises(ValueError, match="nominal_rpm"):
        MachineGeometry(ratio=GearRatio(49.79), radius=Metres(1.5), nominal_rpm=MotorRpm(0))


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("slew", RpmPerSecond(0.0), "slew"),
        ("slew", RpmPerSecond(math.nan), "slew"),
        ("ramp_settle", Seconds(-1.0), "ramp_settle"),
        ("status_stale_after", Seconds(0.0), "status_stale_after"),
        ("start_hysteresis_rpm", MotorRpm(-1), "start_hysteresis_rpm"),
        ("standstill_rpm", MotorRpm(0), "standstill_rpm"),
        ("falling_trend", BpmPerMinute(0.0), "falling_trend"),
        ("falling_trend", BpmPerMinute(5.0), "falling_trend"),
        ("falling_trend", BpmPerMinute(math.nan), "falling_trend"),
        ("trend_samples", 1, "trend_samples"),
        ("disable_attempts", 0, "disable_attempts"),
    ],
)
def test_runtime_limits_refuse_a_budget_that_would_disable_what_it_bounds(
    field_name: str, value: object, message: str
) -> None:
    """A standstill threshold below one rpm can never be met, so the stop never completes."""
    fields: dict[str, object] = {
        "slew": RpmPerSecond(60.0),
        "start_hysteresis_rpm": MotorRpm(10),
    }
    fields[field_name] = value
    with pytest.raises(ValueError, match=message):
        RuntimeLimits(**fields)  # type: ignore[arg-type]  # splat into a frozen record


def test_the_fake_drive_conforms_to_the_seam() -> None:
    """If the fake is not a ``DriveBackend``, nothing this file asserts is about the real path."""
    assert isinstance(FakeDrive(ManualClock()), DriveBackend)


# =========================================================================
# Starting
# =========================================================================


async def test_a_session_cannot_start_without_an_attested_emergency_stop() -> None:
    """While STO is jumpered, this attestation is the only evidence a real stop exists."""
    rig = _rig()
    refusal = await rig.start(attest=False)
    assert is_err(refusal)
    assert isinstance(refusal.error, NotAttested)
    assert not rig.drive.trace, "a refused start must not touch the drive"


async def test_starting_arms_the_drive_with_a_zero_reference_before_any_command_word() -> None:
    """Energising with a stale setpoint would start a machine at last session's speed."""
    rig = _rig()
    assert is_ok(await rig.start())
    assert rig.drive.commands == [
        ControlWord.SHUTDOWN,
        ControlWord.SWITCH_ON,
        ControlWord.ENABLE_OPERATION,
    ]
    assert rig.drive.trace.index("lfrd:0") < rig.drive.trace.index("cmd:SHUTDOWN")
    assert rig.drive.is_enabled()
    assert rig.state() is RuntimeState.RUNNING
    assert rig.phase() is Phase.BASELINE


async def test_a_second_start_is_refused() -> None:
    """A new session is a new object: the supervisor is one instance per session."""
    rig = _rig()
    assert is_ok(await rig.start())
    refusal = await rig.start(attest=False)
    assert is_err(refusal)
    assert isinstance(refusal.error, AlreadyStarted)
    assert refusal.error.state is RuntimeState.RUNNING


async def test_a_start_after_shutdown_is_refused() -> None:
    """Shutdown released the link; restarting through this object would command a closed port."""
    rig = _rig()
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    await rig.runtime.shutdown("test")
    refusal = await rig.start(attest=False)
    assert is_err(refusal)
    assert isinstance(refusal.error, AlreadyStarted)


async def test_a_standing_verdict_refuses_a_start_until_it_is_acknowledged() -> None:
    """No automatic resumption of motion, anywhere: a latch needs a name against it."""
    rig = _rig()
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    rig.runtime.request_estop("the button")
    refusal = await rig.start(attest=False)
    assert is_err(refusal)
    assert isinstance(refusal.error, SafetyStanding)
    assert refusal.error.verdict.action is SafetyAction.QUICK_STOP


@pytest.mark.parametrize("threshold", ["hard_max_bpm", "critical_bpm"])
async def test_a_programme_screened_for_somebody_else_is_refused(threshold: str) -> None:
    """Both numbers are plausible; the ceiling protecting the occupant is another person's."""
    rig = _rig()
    other = _profile(**{threshold: Bpm(150 if threshold == "hard_max_bpm" else 200)})
    rig.program = _program(other)
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, LimitsMismatch)
    assert refusal.error.threshold == threshold
    assert refusal.error.profile_bpm != refusal.error.supervisor_bpm


async def test_limits_that_could_never_leave_zero_are_refused_with_a_reason() -> None:
    """One control period of slew must buy ``min_run_rpm``, or the motor never turns."""
    rig = _rig(limits=RuntimeLimits(slew=RpmPerSecond(1.0), start_hysteresis_rpm=MotorRpm(10)))
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, PlanUnusable)
    assert "could never leave zero" in refusal.error.detail


async def test_a_drive_that_cannot_be_opened_refuses_the_start() -> None:
    rig = _rig()
    rig.drive.break_comms()
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert "could not be opened" in refusal.error.detail


async def test_a_drive_that_cannot_be_read_refuses_the_start() -> None:
    """The state has to be read. A start that assumed it would be the bug this prevents."""
    rig = _rig()
    rig.drive.status_error = BadResponse(detail="short read")
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert "could not be read" in refusal.error.detail


# --- The drive's own limits, read back at every arming ---------------------


@pytest.mark.parametrize(
    ("limits", "parameter", "violation"),
    [
        (
            DriveLimits(
                max_frequency=Hertz(60.0),
                high_speed=Hertz(50.0),
                low_speed=Hertz(5.0),
                acceleration=Seconds(3.0),
                deceleration=Seconds(3.0),
            ),
            DriveParameter.LSP,
            LowSpeedNotZero,
        ),
        (
            DriveLimits(
                max_frequency=Hertz(40.0),
                high_speed=Hertz(45.0),
                low_speed=Hertz(0.0),
                acceleration=Seconds(3.0),
                deceleration=Seconds(3.0),
            ),
            DriveParameter.HSP,
            HighSpeedAboveMaxFrequency,
        ),
        (
            DriveLimits(
                max_frequency=Hertz(60.0),
                high_speed=Hertz(55.0),
                low_speed=Hertz(0.0),
                acceleration=Seconds(3.0),
                deceleration=Seconds(3.0),
            ),
            DriveParameter.HSP,
            HighSpeedAboveCeiling,
        ),
    ],
)
async def test_drive_limits_this_machine_cannot_arm_on_refuse_the_start(
    limits: DriveLimits, parameter: DriveParameter, violation: type[object]
) -> None:
    """Refused BEFORE a single word is written: nothing is energised, nothing latched."""
    rig = _rig()
    rig.drive.limits = limits
    refusal = await rig.start()
    assert is_err(refusal)
    error = refusal.error
    assert isinstance(error, DriveParameterRefused)
    assert isinstance(error.violation, violation)
    assert error.violation.parameter is parameter
    assert error.limits == limits
    assert parameter.value in error.detail
    assert rig.drive.commands == []
    assert rig.drive.writes == []
    assert rig.runtime.output_enabled is False
    assert rig.runtime.standing is None


async def test_a_lowered_ceiling_refuses_the_uncoupled_bench_hsp() -> None:
    """The coupling procedure: lower max_motor_hz and the 50 Hz bench HSP stops arming."""
    rig = _rig(
        limits=RuntimeLimits(
            slew=LIMITS.slew,
            start_hysteresis_rpm=LIMITS.start_hysteresis_rpm,
            gains=LIMITS.gains,
            max_motor_hz=Hertz(20.0),
        )
    )
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveParameterRefused)
    assert refusal.error.violation == HighSpeedAboveCeiling(
        high_speed=Hertz(50.0), ceiling=Hertz(20.0)
    )


def test_the_default_ceiling_is_the_uncoupled_bench_hsp() -> None:
    assert LIMITS.max_motor_hz == 50.0


def test_a_ceiling_that_is_not_positive_is_refused() -> None:
    with pytest.raises(ValueError, match="max_motor_hz"):
        RuntimeLimits(
            slew=LIMITS.slew,
            start_hysteresis_rpm=LIMITS.start_hysteresis_rpm,
            max_motor_hz=Hertz(0.0),
        )


async def test_drive_limits_that_cannot_be_read_refuse_the_start() -> None:
    """An unread ceiling is not an accepted one."""
    rig = _rig()
    rig.drive.limits_error = CommTimeout(after=Seconds(0.1))
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert "limits could not be read" in refusal.error.detail
    assert rig.drive.commands == []
    assert rig.drive.writes == []


async def test_arming_reads_the_limits_and_names_what_it_could_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Accepted limits are logged, and ttO/SLL are named for a keypad check every time."""
    rig = _rig()
    with caplog.at_level("INFO", logger="src.training.runtime"):
        started = await rig.start()
    assert is_ok(started)
    assert "limits" in rig.drive.trace
    assert rig.drive.trace.index("limits") < rig.drive.trace.index("lfrd:0")
    assert "HSP=50.0 Hz" in caplog.text
    for unverified in UNVERIFIED_PARAMETERS:
        assert unverified.describe() in caplog.text


async def test_a_precommanded_drive_is_stopped_before_its_limits_are_even_read() -> None:
    """The stop comes first: judging HSP must never delay zeroing a turning motor."""
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(240))
    rig.drive.limits_error = CommTimeout(after=Seconds(0.1))
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DrivePrecommanded)
    assert "limits" not in rig.drive.trace


async def test_a_drive_found_already_enabled_is_stopped_latched_and_refused() -> None:
    """``restart: unless-stopped`` guarantees this happens for real, with a person inside.

    The reference goes to zero and the run command STAYS: that is the fastest
    stop this machine has. The ramp-stop and shutdown words come later, once
    RFRD shows standstill, because word 6 on a turning shaft is transition 8 and
    coasts for minutes.
    """
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(240))
    refusal = await rig.start()
    assert is_err(refusal)
    error = refusal.error
    assert isinstance(error, DrivePrecommanded)
    assert error.state is DriveState.OPERATION_ENABLED
    assert error.output_rpm == 240
    assert rig.drive.commanded_rpm == 0
    assert ControlWord.SHUTDOWN not in rig.drive.commands
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_DRIVE_PRECOMMANDED
    assert standing.latched


async def test_a_precommanded_drive_is_only_startable_after_a_named_acknowledgement() -> None:
    """The latch is what makes somebody look at the machine before it runs again."""
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(240))
    assert is_err(await rig.start())
    assert is_err(await rig.start(attest=False))
    acknowledged = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert is_ok(acknowledged)
    assert RULE_DRIVE_PRECOMMANDED in acknowledged.value.cleared
    assert rig.runtime.standing is None


async def test_a_latched_fault_refuses_the_start_and_is_never_reset_automatically() -> None:
    """ "Reset it and see" with a person inside the machine is how a short becomes a fire."""
    rig = _rig()
    rig.drive.inject_fault(DriveFault.OVERCURRENT)
    refusal = await rig.start()
    assert is_err(refusal)
    error = refusal.error
    assert isinstance(error, DriveInFault)
    assert error.report is not None
    assert error.report.fault is DriveFault.OVERCURRENT
    assert ControlWord.FAULT_RESET not in rig.drive.commands


async def test_a_transport_failure_during_the_enable_leaves_the_output_believed_off() -> None:
    """A refused command word never energised anything, so nothing is claimed live."""
    rig = _rig()
    rig.drive.speed_error = BadResponse(detail="no route")
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert rig.runtime.output_enabled is False
    assert rig.runtime.standing is None


async def test_an_unconfirmed_enable_is_treated_as_a_possibly_turning_motor() -> None:
    """A request whose reply was lost was still transmitted: "enabled" can mean "turning"."""
    rig = _rig()
    rig.drive.speed_error = EnableUnconfirmed(
        detail="reply lost", reference_zeroed=False, run_command_removed=False
    )
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert rig.runtime.output_enabled is True, "an unknown output state must read as live"
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_ENABLE_UNCONFIRMED
    ending = rig.runtime.ending
    assert ending is not None
    assert ending.reason is EndReason.EMERGENCY_STOP


def test_every_start_refusal_is_handled_somewhere() -> None:
    """An exhaustive match over the closed union, so a new variant fails this build.

    The value of this test is not what it asserts at runtime; it is that
    ``assert_never`` here stops type-checking the day ``StartRefusal`` grows a
    variant nobody classified.
    """
    cases: tuple[StartRefusal, ...] = (
        AlreadyStarted(RuntimeState.RUNNING),
        NotAttested("statement"),
        SafetyStanding(
            SafetyVerdict(
                action=SafetyAction.FREEZE,
                rule="rule",
                detail="d",
                latched=False,
                since=Monotonic(0.0),
            )
        ),
        LimitsMismatch(threshold="hard_max_bpm", profile_bpm=Bpm(1), supervisor_bpm=Bpm(2)),
        PlanUnusable("detail"),
        DriveUnavailable("detail"),
        DriveParameterRefused(violation=LowSpeedNotZero(low_speed=Hertz(1.0)), limits=BENCH_LIMITS),
        DrivePrecommanded(state=DriveState.OPERATION_ENABLED, output_rpm=MotorRpm(1)),
        DriveInFault(report=None, state=DriveState.FAULT),
        RecordStorageLow(free_bytes=None, required_bytes=1, where="data/records"),
    )
    assert len({_classify_refusal(case) for case in cases}) == len(cases)


def _classify_refusal(refusal: StartRefusal) -> str:
    """One exhaustive match over ``StartRefusal``. Adding a variant breaks the build."""
    label: str
    match refusal:
        case AlreadyStarted():
            label = "already_started"
        case NotAttested():
            label = "not_attested"
        case SafetyStanding():
            label = "safety_standing"
        case LimitsMismatch():
            label = "limits_mismatch"
        case PlanUnusable():
            label = "plan_unusable"
        case DriveUnavailable():
            label = "drive_unavailable"
        case DriveParameterRefused():
            label = "drive_parameter_refused"
        case DrivePrecommanded():
            label = "drive_precommanded"
        case DriveInFault():
            label = "drive_in_fault"
        case RecordStorageLow():
            label = "record_storage_low"
        case _ as unreachable:
            assert_never(unreachable)
    return label


# =========================================================================
# The order inside the tick
# =========================================================================


async def test_the_keepalive_is_written_before_anything_is_decided() -> None:
    """The frame that feeds ``ttO`` goes out before the status read and before any decision.

    Asserted on the frame sequence rather than on a flag, because the guarantee
    is about frames: if the loop stalls after the decision but before the write,
    the drive hears nothing and its own timeout is what stops the motor.
    """
    rig = _rig()
    rig.fed_bpm = Bpm(65)
    assert is_ok(await rig.start())
    rig.drive.trace.clear()
    await rig.step()
    trace = rig.drive.trace
    assert trace[0].startswith("lfrd:"), trace
    assert trace[1] == "lfrd:0:ack"
    assert trace[2] == "eta"


async def test_a_stalled_loop_stops_writing_and_the_drives_own_timeout_stops_the_motor() -> None:
    """The watchdog is outside this process on purpose: one written here would die with it."""
    rig = await _running_rig(tto=Seconds(1.0))
    assert rig.drive.is_enabled()
    # The loop stalls: time passes and no tick runs, so no keepalive is written.
    rig.clock.advance(Seconds(3.0))
    rig.drive.advance(rig.now)
    assert rig.drive.state is FakeState.FAULT
    assert not rig.drive.is_enabled()


async def test_a_steady_setpoint_costs_one_write_and_a_changing_one_costs_two() -> None:
    """The keepalive already carried the value, so a redundant second frame is skipped."""
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.drive.trace.clear()
    await rig.step()
    assert sum(1 for entry in rig.drive.trace if entry.startswith("lfrd:") and ":ack" in entry) == 1


# =========================================================================
# Safety beats the controller
# =========================================================================


@pytest.mark.parametrize(
    "action",
    [
        SafetyAction.FREEZE,
        SafetyAction.REDUCE,
        SafetyAction.RAMP_DOWN,
        SafetyAction.QUICK_STOP,
        SafetyAction.GO_SILENT,
    ],
)
async def test_a_verdict_never_lets_the_setpoint_rise(action: SafetyAction) -> None:
    """The control law is asking to accelerate on every one of these ticks.

    The fed rate is far below the zone, so the demand is unambiguously "go
    faster". Every action above must refuse it, and none may end in a setpoint
    above the one that was already in force.
    """
    rig = await _running_rig()
    before = rig.runtime.applied_rpm
    rig.runtime.trip_from_thread("rig_rule", action, "under test")
    for _ in range(10):
        await rig.step()
        assert rig.runtime.applied_rpm <= before, action


async def test_freeze_holds_the_setpoint_exactly() -> None:
    """FREEZE means stop moving it in either direction, not "slow down".

    The freeze is applied in HOLD, the moment the ceiling rises and the control
    law starts climbing again. Freezing a controller that is already saturated
    at its ceiling would prove nothing: it was not going to move anyway.
    """
    rig = await _running_rig()
    while rig.phase() is not Phase.HOLD:
        await rig.step()
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.step()
    held = rig.runtime.applied_rpm
    assert 0 < held < REAL_PROFILE.max_rpm, "no headroom left for a freeze to be observable"
    await rig.run(5.0)
    assert rig.runtime.applied_rpm == held


async def test_reduce_steps_the_setpoint_down_while_the_control_law_asks_to_accelerate() -> None:
    """REDUCE must *move* the setpoint down, not merely decline to raise it.

    The distinction is the whole test. On this rig the fed rate is far below the
    zone, so the control law is asking to accelerate and is already saturated at
    the phase ceiling. A REDUCE implemented as "let the controller regulate but
    forbid increases" would simply hold there and achieve nothing - which is
    exactly what ``current_high`` and ``hr_unresponsive`` would get, since both
    can fire while the control law is perfectly happy.
    """
    rig = await _running_rig()
    before = rig.runtime.applied_rpm
    assert before > 0
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    await rig.run(2.0)
    assert rig.runtime.applied_rpm < before


async def test_quick_stop_zeroes_the_reference_at_once_and_keeps_the_run_command() -> None:
    """Removing the run command from a turning shaft is transition 8: minutes of coasting."""
    rig = await _running_rig()
    assert rig.drive.shaft_rpm > 0
    rig.runtime.trip_from_thread("rig_quick", SafetyAction.QUICK_STOP, "under test")
    await rig.step()
    assert rig.runtime.applied_rpm == 0
    assert rig.drive.commanded_rpm == 0
    assert rig.drive.is_enabled(), "the run command must stay while the shaft is turning"


async def test_a_freeze_resumes_from_the_speed_the_drive_reports_not_from_the_demand() -> None:
    """What "bumpless" does and does not promise, measured rather than asserted.

    During a ``hr_stale`` freeze the control law is asking to accelerate and is
    not consulted at all, so the question on release is what its increment is
    added to. It is added to LFRD read back - the speed the machine is actually
    running at - which is why a freeze does not end by jumping to the demand
    that was standing when it began.

    The bound asserted is the honest one. The controller's own interval keeps
    accumulating while it is not called, so its first decision afterwards
    integrates over ``step_cap``; the slew limit is stated between successive
    *changes* of the setpoint, and that is the form checked here. Asserting "one
    small tick" instead would be asserting something the design does not
    provide.
    """
    rig = await _running_rig()
    await rig.run(2.0)
    frozen_at = rig.runtime.applied_rpm
    steady_since = float(rig.now)
    await rig.run(2.0)
    assert rig.runtime.applied_rpm == frozen_at, "the rig was still ramping when the freeze began"

    # Ten seconds without a metric refresh is hr_stale's FREEZE level.
    await rig.run(12.0, feed=False)
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_HR_STALE
    assert standing.action is SafetyAction.FREEZE
    assert rig.runtime.applied_rpm == frozen_at, "the setpoint moved during the freeze"

    rig.fed_bpm = Bpm(65)
    await rig.run(6.0)
    changes: list[tuple[float, MotorRpm]] = [(steady_since, frozen_at)]
    for snapshot in rig.snapshots[-30:]:
        if snapshot.setpoint.motor_rpm != changes[-1][1]:
            changes.append((float(snapshot.at), snapshot.setpoint.motor_rpm))
    assert len(changes) > 1, "the freeze never released"
    # The span is measured from the last time the setpoint actually moved, which
    # is before the freeze - that is what "between successive changes" means, and
    # it is the only form of the slew bound that says anything about a gap.
    for (before_at, before_rpm), (after_at, after_rpm) in pairwise(changes):
        span = after_at - before_at
        assert abs(after_rpm - before_rpm) <= LIMITS.slew * span + 1, (
            before_rpm,
            after_rpm,
            span,
        )
    assert changes[1][1] < REAL_PROFILE.max_rpm, "the resume jumped to the free-running demand"
    assert changes[1][1] > frozen_at, "the resume never regained any speed"


async def test_the_reported_target_does_not_flicker_between_control_decisions() -> None:
    """A held tick has no target to claim, and publishing that would flash the screen.

    The control period is seconds and the loop runs at 5 Hz, so most ticks decide
    nothing. The band they would have aimed at has not moved, so the published
    target is the last one actually decided - not ``None`` four ticks out of
    five.
    """
    rig = await _running_rig()
    await rig.run(11.0)
    assert rig.phase() is Phase.HOLD
    await rig.run(4.0)
    targets = [snapshot.target_bpm for snapshot in rig.snapshots[-15:]]
    assert None not in targets, targets
    assert len(set(targets)) == 1, targets


async def test_go_silent_sends_nothing_further_of_any_kind() -> None:
    """A read resets a real drive's ``ttO`` too, so silence has to mean silence."""
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    await rig.step()
    assert rig.runtime.silent
    rig.drive.trace.clear()
    await rig.run(3.0)
    assert rig.drive.trace == []


async def test_a_ramp_down_walks_the_setpoint_down_rather_than_dropping_it() -> None:
    """RAMP_DOWN is a decision to stop, not an emergency: the software ramp shapes it."""
    rig = await _running_rig()
    # Let the setpoint go steady first. A descent that inherited the allowance
    # accrued while the setpoint was NOT descending could take its whole first
    # step in one tick, and this is the four seconds that would pay for it.
    await rig.run(4.0)
    settled = rig.runtime.applied_rpm
    changes: list[tuple[float, MotorRpm]] = [(float(rig.now), settled)]
    rig.runtime.trip_from_thread("rig_ramp", SafetyAction.RAMP_DOWN, "under test")
    for _ in range(60):
        await rig.step()
        if rig.runtime.applied_rpm != changes[-1][1]:
            changes.append((float(rig.now), rig.runtime.applied_rpm))
        if rig.runtime.applied_rpm == 0:
            break
    assert changes[-1][1] == 0, changes
    assert len(changes) > 2, "the setpoint dropped in one step"
    # The slew bound, between successive CHANGES and with no exemption for the
    # last one. The final step out of the domain is a jump of min_run_rpm, and
    # the limiter pays for it by leaving earlier intervals unspent - so it is
    # bounded by the elapsed time, which is exactly what this measures.
    for (before_at, before_rpm), (after_at, after_rpm) in pairwise(changes):
        span = after_at - before_at
        assert before_rpm - after_rpm <= LIMITS.slew * span + 1, (
            before_rpm,
            after_rpm,
            span,
        )


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rates=st.lists(st.integers(min_value=30, max_value=200), min_size=20, max_size=60),
    action=st.sampled_from(
        [SafetyAction.FREEZE, SafetyAction.REDUCE, SafetyAction.RAMP_DOWN, SafetyAction.QUICK_STOP]
    ),
)
async def test_a_verdict_dominates_the_controller_for_any_heart_rate_sequence(
    rates: list[int], action: SafetyAction
) -> None:
    """The property the whole two-layer design exists for, over arbitrary evidence.

    A falling heart rate reads to the control law as "below the zone" and is
    answered by accelerating. Whatever the rates do - including the vasovagal
    shape - the commanded setpoint must never rise once a verdict stands.
    """
    assume(len(set(rates)) > 1)
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_property", action, "under test")
    ceiling = rig.runtime.applied_rpm
    for rate in rates:
        rig.fed_bpm = Bpm(rate)
        await rig.step()
        assert rig.runtime.applied_rpm <= ceiling
        ceiling = rig.runtime.applied_rpm


# =========================================================================
# THE HEADLINE: no exit path leaves the motor running
# =========================================================================


async def _exit_programme_complete(rig: Rig) -> None:
    """The nominal timeline runs to its end."""
    await rig.run(112.0)


async def _exit_operator_stop(rig: Rig) -> None:
    rig.runtime.request_stop("the operator pressed stop")
    await rig.run(12.0)


async def _exit_emergency_stop(rig: Rig) -> None:
    rig.runtime.request_estop("the browser button")
    await rig.run(12.0)


async def _exit_drive_fault(rig: Rig) -> None:
    rig.drive.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await rig.run(12.0)


async def _exit_comms_lost(rig: Rig) -> None:
    """The link dies. Nothing can be written, so the drive's own timeout stops it."""
    rig.drive.break_comms()
    await rig.run(12.0)


async def _exit_tick_exception(rig: Rig) -> None:
    """Something inside the tick raises. The state of this process is no longer trusted."""
    rig.drive.raise_on_status = True
    with pytest.raises(RuntimeError):
        await rig.step()
    rig.drive.raise_on_status = False
    await rig.run(12.0)


async def _exit_shutdown(rig: Rig) -> None:
    await rig.runtime.shutdown("SIGTERM")


async def _exit_critical_heart_rate(rig: Rig) -> None:
    """The occupant's heart rate goes past the critical tier: QUICK_STOP."""
    rig.fed_bpm = Bpm(159)
    await rig.run(12.0)


async def _exit_vasovagal_drop(rig: Rig) -> None:
    """THE rule the two-layer design exists for: a fall of >25 bpm inside 30 s."""
    for rate in (100, 120, 140, 150):
        rig.fed_bpm = Bpm(rate)
        await rig.run(3.0)
    rig.fed_bpm = Bpm(115)
    await rig.run(4.0)
    rig.fed_bpm = Bpm(95)
    await rig.run(14.0)


async def _exit_standstill_inside_the_session(rig: Rig) -> None:
    """A warning lowers the speed all the way to zero: the session ends there (ANH-176).

    The one ending that is reached with the setpoint ALREADY at zero: the
    verdict that records it lands on the tick after. Asserted here, so that
    this case cannot quietly become "the programme's own cooldown got there
    first" if the rig's timeline is ever shortened.
    """
    rig.fed_bpm = Bpm(82)  # the resting rate: no fall for hr_drop once the load is gone
    rig.runtime.trip_from_thread("rig_reduce", SafetyAction.REDUCE, "under test")
    await rig.run(12.0)
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_SESSION_STANDSTILL
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT


ExitPath = Callable[[Rig], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ExitCase:
    """One way a session can end, and whether a frame could still reach the drive.

    ``reachable`` is not a convenience flag. On a severed link nothing can set
    the speed reference, so asserting "the reference is zero" there would be
    asserting a fiction - the register keeps its last value and the drive's own
    ``ttO`` timeout is what removes the torque. The test therefore asserts the
    physical outcome on every path and the *commanded* value only where a
    command could actually be delivered.
    """

    name: str
    run: ExitPath
    reachable: bool = True


EXIT_CASES: Final[tuple[ExitCase, ...]] = (
    ExitCase("normal end", _exit_programme_complete),
    ExitCase("operator stop", _exit_operator_stop),
    ExitCase("emergency stop", _exit_emergency_stop),
    ExitCase("drive fault", _exit_drive_fault),
    ExitCase("comms lost", _exit_comms_lost, reachable=False),
    ExitCase("tick exception", _exit_tick_exception),
    ExitCase("shutdown", _exit_shutdown),
    ExitCase("critical heart rate", _exit_critical_heart_rate),
    ExitCase("vasovagal drop", _exit_vasovagal_drop),
    ExitCase("standstill inside the session", _exit_standstill_inside_the_session),
)


@pytest.mark.parametrize("case", EXIT_CASES, ids=[case.name for case in EXIT_CASES])
async def test_no_exit_path_leaves_the_motor_running(case: ExitCase) -> None:
    """**The most important test in this file.**

    Every way a session can end, and in every one of them the motor must end up
    with no torque and a stopped shaft. Three of these do not get there by
    writing: a dead link, an exception inside the tick and a silent shutdown all
    end by the runtime *ceasing to write*, and the fake drive's ``ttO`` timeout
    is what finishes the stop - which is exactly the arrangement the control loop
    is built around, and the reason the fake models that timeout at all.

    Note the shape of the assertions. They are made against the FAKE DRIVE, not
    against the runtime's beliefs: the runtime saying "I disabled it" is not
    evidence, and on the silent paths the runtime deliberately claims nothing at
    all. And on the one path where no frame can be delivered, the assertion is
    about what was *attempted* plus what physically happened, because a claim
    about the reference register would be a claim about a write that never left.
    """
    rig = await _running_rig(tto=Seconds(1.0))
    assert rig.drive.is_enabled()
    assert rig.runtime.applied_rpm > 0
    await case.run(rig)
    # Give the drive's own timeout room to act on the paths that rely on it.
    rig.clock.advance(Seconds(5.0))
    rig.drive.advance(rig.now)

    assert not rig.drive.is_enabled(), "the output stage was left energised"
    assert abs(rig.drive.shaft_rpm) < 1.0, "the shaft was left turning"
    if case.reachable:
        assert rig.drive.commanded_rpm == 0, "the speed reference was left non-zero"
        assert rig.runtime.applied_rpm == 0, "the runtime still believes it commands a speed"
    else:
        assert rig.drive.emergency_calls >= 1, "no attempt was made to zero the reference"
        assert rig.runtime.silent, "the runtime kept writing, which restarts the ttO timeout"


async def test_a_tick_exception_fails_closed_before_it_propagates() -> None:
    """The zero goes out first: the motor is not waiting for the traceback to unwind."""
    rig = await _running_rig()
    rig.drive.raise_on_status = True
    with pytest.raises(RuntimeError, match="blew up"):
        await rig.step()
    assert rig.drive.commanded_rpm == 0
    assert rig.runtime.silent
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_TICK_EXCEPTION
    assert standing.action is SafetyAction.GO_SILENT
    assert rig.runtime.end_reason is EndReason.TICK_EXCEPTION
    assert rig.runtime.snapshot().safety is standing


async def test_a_tick_exception_cannot_be_acknowledged_away() -> None:
    """Silence is one-way: recovery is an operator act on a machine known to have stopped."""
    rig = await _running_rig()
    rig.drive.raise_on_status = True
    with pytest.raises(RuntimeError):
        await rig.step()
    refusal = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert is_err(refusal)
    assert isinstance(refusal.error, GoSilentIsTerminal)


# =========================================================================
# A whole session
# =========================================================================

REAL_PROFILE: Final[TrainingProfile] = _profile(
    profile_id="standard_30_min",
    name="30 min",
    total_duration_s=Seconds(1800.0),
    baseline_s=Seconds(180.0),
    warmup_max_s=Seconds(300.0),
    hold_min_s=Seconds(300.0),
    cooldown_s=Seconds(240.0),
    recovery_s=Seconds(300.0),
)


async def test_a_whole_session_walks_the_phases_in_order_and_on_the_timeline() -> None:
    """A thirty-minute programme, at 5 Hz, against a plant, in a fraction of a second.

    That is the point of the injected clock: this is the only evidence the phase
    machine and the control law behave over a full session before a person is
    inside the machine.
    """
    rig = _rig(profile=REAL_PROFILE, limits=REAL_LIMITS, occupant=Occupant())
    assert is_ok(await rig.start())
    await rig.run(1802.0)

    order = [snapshot.phase for snapshot in rig.snapshots]
    visited = [order[0], *(new for old, new in pairwise(order) if old is not new)]
    assert visited == [
        Phase.BASELINE,
        Phase.WARMUP,
        Phase.HOLD,
        Phase.COOLDOWN,
        Phase.RECOVERY,
        Phase.DONE,
    ], visited

    # Every boundary falls inside one tick of the profile's own timeline.
    expected = {span.phase: span.start for span in REAL_PROFILE.timeline}
    for snapshot in rig.snapshots:
        start = expected.get(snapshot.phase)
        if start is not None:
            assert snapshot.elapsed >= start - TICK, snapshot.phase
    assert rig.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert rig.state() is RuntimeState.FINISHED


async def test_a_whole_session_actually_holds_the_occupant_in_the_zone() -> None:
    """The closed loop, end to end, with numbers rather than a phase list.

    The phase-order test would pass on a session that never moved the motor at
    all, so this is the one that says the session did what it was for: the heart
    rate reached the zone, it never went above the zone ceiling, and both the
    heart rate and the setpoint are settled by the end of HOLD rather than
    hunting.

    The bounds are deliberately loose - this is the runtime's arrangement under
    test, not the control law's tuning, which
    ``tests/test_hr_control.py``'s plant sweep covers over 36 occupants. What
    would fail here is a runtime that fought its own controller: a keepalive that
    overwrote the setpoint, a base taken from the wrong field, a verdict applied
    on the wrong tick.
    """
    rig = _rig(profile=REAL_PROFILE, limits=REAL_LIMITS, occupant=Occupant())
    assert is_ok(await rig.start())
    await rig.run(1802.0)

    holding = [snapshot for snapshot in rig.snapshots if snapshot.phase is Phase.HOLD]
    rates = [snapshot.live_bpm for snapshot in holding if snapshot.live_bpm is not None]
    speeds = [snapshot.setpoint.motor_rpm for snapshot in holding]
    assert len(rates) > 100, "the session spent HOLD without a usable heart rate"

    assert rig.runtime.counters.in_zone > 60.0, rig.runtime.counters
    assert max(rates) >= REAL_PROFILE.zone_low_bpm, max(rates)
    assert max(rates) <= REAL_PROFILE.zone_high_bpm, "the heart rate overshot the zone ceiling"
    assert rates[-1] >= REAL_PROFILE.zone_low_bpm, "HOLD ended below the zone"

    settled_rates = rates[len(rates) * 3 // 4 :]
    settled_speeds = speeds[len(speeds) * 3 // 4 :]
    assert max(settled_rates) - min(settled_rates) <= 3, settled_rates[:20]
    assert max(settled_speeds) - min(settled_speeds) <= REAL_PROFILE.min_run_rpm, (
        max(settled_speeds),
        min(settled_speeds),
    )


async def test_the_setpoint_stays_inside_its_domain_for_a_whole_session() -> None:
    """``{0} union [min_run_rpm, max_rpm]``: there is no such thing as a useful crawl."""
    rig = _rig(profile=REAL_PROFILE, limits=REAL_LIMITS, occupant=Occupant())
    assert is_ok(await rig.start())
    await rig.run(1800.0)
    minimum = REAL_PROFILE.min_run_rpm
    for snapshot in rig.snapshots:
        rpm = snapshot.setpoint.motor_rpm
        assert rpm == 0 or minimum <= rpm <= REAL_PROFILE.max_rpm, rpm
        assert math.isfinite(snapshot.setpoint.g_load)
        assert math.isfinite(snapshot.setpoint.output_rpm)


async def test_the_warmup_ceiling_holds_for_the_whole_of_the_warmup() -> None:
    """The guard against a persistently wrong low reading, while the plant gain is weakest."""
    rig = _rig(profile=REAL_PROFILE, limits=REAL_LIMITS, occupant=Occupant())
    assert is_ok(await rig.start())
    await rig.run(600.0)
    warming = [s for s in rig.snapshots if s.phase is Phase.WARMUP]
    assert warming
    assert max(s.setpoint.motor_rpm for s in warming) <= REAL_PROFILE.warmup_rpm_ceiling


async def test_a_phase_transition_does_not_step_the_setpoint() -> None:
    """Bumpless: the standing demand IS the integrator, so it must survive the boundary.

    A controller rebuilt per phase would reset the output to zero and the
    occupant would feel every transition. Asserted as continuity across each
    boundary, bounded by what the slew limiter would have allowed anyway.
    """
    rig = _rig(profile=REAL_PROFILE, limits=REAL_LIMITS, occupant=Occupant())
    assert is_ok(await rig.start())
    await rig.run(1200.0)
    limit = REAL_LIMITS.slew * TICK
    boundaries = 0
    for previous, following in pairwise(rig.snapshots):
        if previous.phase is following.phase:
            continue
        boundaries += 1
        step = abs(following.setpoint.motor_rpm - previous.setpoint.motor_rpm)
        assert step <= limit, (previous.phase, following.phase, step)
    assert boundaries >= 2


async def test_the_warmup_ends_early_once_the_heart_rate_reaches_the_zone() -> None:
    """ "Max" in ``warmup_max_s`` means max: reaching the zone ends the warmup."""
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    assert rig.phase() is Phase.WARMUP
    rig.fed_bpm = Bpm(120)
    await rig.run(4.0)
    assert rig.phase() is Phase.HOLD
    assert rig.runtime.snapshot().elapsed < REAL_PROFILE.baseline_s


async def test_the_resting_rate_is_measured_in_baseline_and_never_invented() -> None:
    """A substituted resting rate is an invented vital sign the control law would ramp against."""
    rig = _rig()
    assert is_ok(await rig.start())
    await rig.run(4.0, feed=False)
    assert rig.runtime.resting_bpm is None
    rig.fed_bpm = Bpm(77)
    await rig.run(5.0)
    assert rig.runtime.resting_bpm == 77


async def test_a_session_with_no_resting_rate_never_leaves_standstill() -> None:
    """No origin for the WARMUP ramp means no ramp, which is the safe answer."""
    rig = _rig()
    assert is_ok(await rig.start())
    await rig.run(40.0, feed=False)
    assert rig.runtime.applied_rpm == 0


async def test_the_zone_counters_only_count_ticks_with_a_usable_heart_rate() -> None:
    """A session whose counters sum to a fraction of its length was spent mostly blind."""
    rig = _rig()
    rig.fed_bpm = Bpm(127)
    assert is_ok(await rig.start())
    await rig.run(10.0)
    accounted = rig.runtime.counters.total
    await rig.run(10.0, feed=False)
    # The tracker's own staleness window keeps a few ticks counting after the
    # feed stops; nothing like the ten seconds that elapsed.
    assert rig.runtime.counters.total < accounted + 6.0
    rig.fed_bpm = Bpm(200)
    await rig.run(6.0)
    assert rig.runtime.counters.above_zone > 0.0
    rig.fed_bpm = Bpm(60)
    await rig.run(6.0)
    assert rig.runtime.counters.below_zone > 0.0
    assert rig.runtime.counters.in_zone > 0.0


# =========================================================================
# Evidence handling
# =========================================================================


async def test_a_repeated_sequence_number_is_not_carried_forward_as_fresh_evidence() -> None:
    """``signal_processing`` re-emits its previous metrics dict with a current timestamp.

    The re-emitted dict is the pipeline saying "I measured nothing". Carrying it
    forward would put a stale heart rate on the screen wearing a fresh age, and
    would let the control law regulate on it.
    """
    rig = _rig()
    assert is_ok(await rig.start())
    rig.feed(Bpm(100))
    await rig.step(feed=False)
    first = rig.runtime.snapshot()
    assert first.heart_rate is not None
    stamped = first.heart_rate.at

    rig.clock.advance(Seconds(3.0))
    rejected = rig.runtime.observe_ecg(rig.now, rig.seq, SignalQuality.GOOD, Bpm(100))
    assert is_err(rejected)
    assert isinstance(rejected.error, StaleSequence)
    await rig.step(feed=False)
    carried = rig.runtime.snapshot().heart_rate
    assert carried is not None
    assert carried.at == stamped, "a repeated seq refreshed the sample's timestamp"


async def test_an_untrustworthy_grade_is_recorded_but_never_shown_as_a_live_rate() -> None:
    """A heart rate is reported only on a good signal. Never fabricate one."""
    rig = _rig()
    assert is_ok(await rig.start())
    rig.feed(Bpm(112), quality=SignalQuality.MAINS_DOMINATED)
    await rig.step(feed=False)
    snapshot = rig.runtime.snapshot()
    assert snapshot.heart_rate is not None
    assert snapshot.heart_rate.quality is SignalQuality.MAINS_DOMINATED
    assert snapshot.live_bpm is None


async def test_a_stale_drive_observation_reads_as_state_unknown_and_not_as_stopped() -> None:
    """COMM_LOST, never NOT_READY: "unknown" is not "the motor is stopped"."""
    rig = await _running_rig()
    rig.drive.break_comms()
    await rig.run(4.0)
    snapshot = rig.runtime.snapshot()
    assert snapshot.drive_state is DriveState.COMM_LOST
    assert snapshot.drive_status_is_stale
    assert snapshot.current is None
    assert snapshot.fault is None


async def test_a_runtime_that_has_never_read_the_drive_says_so() -> None:
    """A measured zero with no age is the one thing a consumer must not mistake for stopped."""
    rig = _rig()
    snapshot = rig.runtime.snapshot()
    assert snapshot.drive_state is DriveState.COMM_LOST
    assert snapshot.drive_status_age is None
    assert snapshot.drive_status_is_stale
    assert snapshot.measured.motor_rpm == 0
    assert snapshot.heart_rate is None
    assert snapshot.heart_rate_is_stale
    assert snapshot.phase is Phase.DONE
    assert rig.state() is RuntimeState.IDLE


async def test_a_setpoint_that_never_landed_is_reported_as_unconfirmed() -> None:
    """The commanded speed on the screen may be fiction, and the screen has to say so.

    A Modbus write response echoes the request, so a write to a wrong address is
    acknowledged while the speed reference never moves. LFRD read back and
    compared is the only evidence a write landed, and this flag is that
    comparison.
    """
    rig = await _running_rig()
    while rig.phase() is not Phase.HOLD:
        await rig.step()
    assert rig.runtime.snapshot().setpoint_confirmed
    rig.drive.swallow_writes = True
    await rig.run(3.0)
    assert rig.runtime.applied_rpm != rig.drive.commanded_rpm, "the fake did not swallow the write"
    assert not rig.runtime.snapshot().setpoint_confirmed


async def test_an_idle_runtime_still_proves_the_link_and_commands_nothing() -> None:
    """A fault or a dead link should be visible before anybody is in the machine."""
    rig = _rig()
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    # Open the link without starting a session, the way a UI's health check would.
    assert is_ok(await rig.drive.open())
    await rig.step(feed=False)
    assert rig.runtime.applied_rpm == 0
    assert rig.phase() is Phase.DONE


# =========================================================================
# Out-of-band requests
# =========================================================================


async def test_an_operator_stop_is_recorded_once_and_ramps_on_the_next_tick() -> None:
    """Deliberate and unhurried: the setpoint walks down like any cooldown."""
    rig = await _running_rig()
    rig.runtime.request_stop("first")
    rig.runtime.request_stop("second")
    await rig.step()
    ending = rig.runtime.ending
    assert ending is not None
    assert ending.reason is EndReason.OPERATOR_STOP
    assert ending.action is SafetyAction.NONE
    assert ending.detail == "operator stop"
    assert rig.phase() is Phase.COOLDOWN
    assert rig.runtime.stop_reason == "first", "a later request rewrote why the session ended"


async def test_an_emergency_stop_zeroes_the_reference_without_waiting_for_a_tick() -> None:
    """A stop that waits for whatever the loop is awaiting is not a stop."""
    rig = await _running_rig()
    verdict = rig.runtime.request_estop("the mushroom")
    assert verdict.action is SafetyAction.QUICK_STOP
    assert rig.drive.commanded_rpm == 0, "the reference was still set after the e-stop returned"
    assert rig.runtime.applied_rpm == 0
    assert rig.drive.emergency_calls == 1


def test_the_runtime_exposes_the_supervisor_it_judges_with() -> None:
    """One instance, reachable, so a composition root can hand it to the web e-stop."""
    rig = _rig()
    assert isinstance(rig.runtime.supervisor, SafetySupervisor)
    assert rig.runtime.supervisor is rig.runtime.supervisor


def test_an_injected_supervisor_is_the_one_the_runtime_uses() -> None:
    clock = ManualClock()
    safety = _safety_for(_profile())
    shared = SafetySupervisor(clock=clock, limits=safety)
    runtime = TrainingRuntime(
        clock=clock,
        drive=FakeDrive(clock),
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=safety,
        supervisor=shared,
    )
    assert runtime.supervisor is shared


def test_a_supervisor_built_with_other_limits_is_refused() -> None:
    """The programme checks read ``safety``; a supervisor judging other numbers would lie."""
    clock = ManualClock()
    safety = _safety_for(_profile())
    other = SafetySupervisor(
        clock=clock,
        limits=SafetyLimits(
            hard_max_bpm=Bpm(safety.hard_max_bpm - 1), critical_bpm=safety.critical_bpm
        ),
    )
    with pytest.raises(ValueError, match="different SafetyLimits"):
        TrainingRuntime(
            clock=clock,
            drive=FakeDrive(clock),
            geometry=GEOMETRY,
            limits=LIMITS,
            safety=safety,
            supervisor=other,
        )


async def test_an_estop_latched_on_the_shared_supervisor_stops_the_motor_on_the_next_tick() -> None:
    """The web path: the handler latches the SHARED supervisor, before any tick.

    The runtime reports the latch at once (it reads the same instance) and
    commands zero on its next tick. This is what makes the web e-stop real
    rather than a latch on a second supervisor nobody reads.
    """
    rig = await _running_rig(shared_supervisor=True)
    verdict = rig.runtime.supervisor.latch_estop("the browser button")
    assert verdict.action is SafetyAction.QUICK_STOP
    assert rig.runtime.standing_action is SafetyAction.QUICK_STOP
    await rig.step()
    assert rig.runtime.applied_rpm == 0
    assert rig.drive.commanded_rpm == 0


async def test_an_emergency_stop_after_going_silent_sends_nothing() -> None:
    """A frame now would restart the timeout that is carrying out the stop."""
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    await rig.step()
    calls = rig.drive.emergency_calls
    rig.runtime.request_estop("second thoughts")
    assert rig.drive.emergency_calls == calls


@pytest.mark.parametrize(
    "outcome",
    [
        EmergencyStopOutcome.ACKNOWLEDGED,
        EmergencyStopOutcome.SENT_UNCONFIRMED,
        EmergencyStopOutcome.NOTHING_SENT,
    ],
)
async def test_only_an_acknowledged_emergency_zero_is_credited_with_a_zero(
    outcome: EmergencyStopOutcome,
) -> None:
    """An unconfirmed write may have landed; believing it did would be inventing evidence."""
    rig = await _running_rig()
    before = rig.runtime.applied_rpm
    rig.drive.emergency_override = outcome
    rig.runtime.request_estop("under test")
    if outcome is EmergencyStopOutcome.ACKNOWLEDGED:
        assert rig.runtime.applied_rpm == 0
    else:
        assert rig.runtime.applied_rpm == before


async def test_a_presence_ping_records_an_instant_and_keeps_the_attendant_rule_quiet() -> None:
    """This rig must never run unattended, and "never" is enforced in code."""
    rig = await _running_rig()
    stamp = rig.runtime.presence_ping()
    assert stamp == rig.now
    await rig.run(80.0, ping=False)
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_ATTENDANT_ABSENT


async def test_a_thread_trip_reaches_the_standing_verdict_within_one_tick() -> None:
    """The acquisition thread must never be made to wait on the control loop."""
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_thread", SafetyAction.REDUCE, "from the reader")
    await rig.step()
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == "rig_thread"
    assert standing.action is SafetyAction.REDUCE


# =========================================================================
# Acknowledging
# =========================================================================


async def test_an_unattributable_acknowledgement_is_refused() -> None:
    """An unattributable safety record is not a safety record."""
    rig = _rig()
    refusal = rig.runtime.acknowledge("   ")
    assert is_err(refusal)
    assert isinstance(refusal.error, Unattributed)


async def test_acknowledging_nothing_is_refused() -> None:
    rig = _rig()
    refusal = rig.runtime.acknowledge(OPERATOR)
    assert is_err(refusal)
    assert isinstance(refusal.error, NothingLatched)


async def test_forgetting_whether_the_mushroom_was_released_fails_closed() -> None:
    """Software cannot see the contact, so the default answer is the unsafe one refused."""
    rig = _rig()
    rig.runtime.request_estop("the button")
    refusal = rig.runtime.acknowledge(OPERATOR)
    assert is_err(refusal)
    assert isinstance(refusal.error, EmergencyStopStillLatched)
    assert is_ok(rig.runtime.acknowledge(OPERATOR, estop_released=True))


async def test_a_runtime_latch_clears_even_when_the_supervisor_has_nothing_to_clear() -> None:
    """The precommanded latch is this module's own: no rule and no e-stop produced it."""
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(120))
    assert is_err(await rig.start())
    record = rig.runtime.acknowledge(OPERATOR)
    assert is_ok(record)
    assert record.value.cleared == (RULE_DRIVE_PRECOMMANDED,)
    assert rig.runtime.standing is None


async def test_an_acknowledgement_names_every_latch_it_cleared() -> None:
    """The record is what somebody reads afterwards; a partial list is a misleading one."""
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(120))
    assert is_err(await rig.start())
    rig.runtime.request_estop("also the button")
    record = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert is_ok(record)
    assert RULE_DRIVE_PRECOMMANDED in record.value.cleared
    assert len(record.value.cleared) >= 2


def test_every_acknowledgement_refusal_is_handled() -> None:
    """An exhaustive match over the supervisor's closed union, borrowed unchanged."""
    cases: tuple[AcknowledgeRefusal, ...] = (
        Unattributed("d"),
        NothingLatched(),
        GoSilentIsTerminal("rule"),
        EmergencyStopStillLatched(since=Monotonic(0.0)),
    )
    assert len({_classify_ack(case) for case in cases}) == len(cases)


def _classify_ack(refusal: AcknowledgeRefusal) -> str:
    """One exhaustive match over the supervisor's refusal union."""
    label: str
    match refusal:
        case Unattributed():
            label = "unattributed"
        case NothingLatched():
            label = "nothing"
        case GoSilentIsTerminal():
            label = "silent"
        case EmergencyStopStillLatched():
            label = "estop"
        case _ as unreachable:
            assert_never(unreachable)
    return label


# =========================================================================
# Stopping and disabling
# =========================================================================


async def test_a_stop_always_reaches_zero_from_the_minimum_running_speed() -> None:
    """The domain has nothing between zero and ``min_run_rpm``, so the last step is a jump.

    A limiter that refused to pay for that jump would park the setpoint at the
    minimum running speed forever, with the telemetry showing a controller
    dutifully demanding zero and the machine never stopping. A probe found
    exactly that defect in the control law; this is the same guarantee for the
    safety-driven descent.
    """
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_ramp", SafetyAction.RAMP_DOWN, "under test")
    for _ in range(200):
        await rig.step()
        if rig.runtime.applied_rpm == 0:
            break
    assert rig.runtime.applied_rpm == 0
    assert rig.drive.commanded_rpm == 0


async def test_the_run_command_is_removed_only_after_standstill_is_confirmed() -> None:
    """Word 6 on a moving centrifuge drops the output stage and coasts for minutes."""
    rig = await _running_rig()
    rig.runtime.request_stop("stop")
    seen_zero_while_turning = False
    for _ in range(400):
        await rig.step()
        if rig.runtime.applied_rpm == 0 and abs(rig.drive.shaft_rpm) >= 1.0:
            seen_zero_while_turning = True
            assert ControlWord.SHUTDOWN not in rig.drive.commands[3:]
        if not rig.runtime.output_enabled:
            break
    assert seen_zero_while_turning, "the shaft stopped too fast to test the ordering"
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commands[-2:] == [ControlWord.SWITCH_ON, ControlWord.SHUTDOWN]


async def test_the_output_stage_stays_energised_through_baseline() -> None:
    """BASELINE sits at zero rpm for minutes; disabling there would stall the first warmup tick."""
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(8.0)
    assert rig.phase() is Phase.BASELINE
    assert rig.runtime.applied_rpm == 0
    assert rig.runtime.output_enabled
    assert rig.drive.is_enabled()


async def test_a_shaft_that_will_not_stop_keeps_the_run_command_and_says_so() -> None:
    """A zero reference plus ``ttO`` beats a freewheel by two orders of magnitude."""
    rig = await _running_rig(tto=Seconds(600.0))
    rig.drive.break_comms()
    await rig.run(30.0)
    assert rig.runtime.silent
    assert ControlWord.SHUTDOWN not in rig.drive.commands[3:]
    # And the session still moves on. Standstill can never be confirmed from
    # here - no frame will be read again - so the cooldown deadline is the only
    # thing that stops the session sitting in COOLDOWN for ever with the
    # heart-rate rules aimed at a machine nobody is still deciding for.
    assert rig.phase() is Phase.RECOVERY


async def test_the_disable_sequence_gives_up_on_a_refused_word_rather_than_pressing_on() -> None:
    """Writing word 6 after word 7 was refused would remove a run command nobody ramped."""
    rig = await _running_rig()
    rig.drive.inject_fault(DriveFault.INTERNAL)
    before = list(rig.drive.commands)
    await rig.run(20.0)
    assert rig.runtime.applied_rpm == 0
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commands == before, "a word landed while the drive refused them"
    assert rig.runtime.output_enabled, "the runtime claimed a disable it never achieved"


# =========================================================================
# Shutdown
# =========================================================================


async def test_shutdown_zeroes_then_closes_and_is_idempotent() -> None:
    """Teardown paths call it twice: an ``except`` branch and then a ``finally``."""
    rig = await _running_rig()
    first = await rig.runtime.shutdown("SIGTERM")
    assert first.emergency is EmergencyStopOutcome.ACKNOWLEDGED
    assert first.closed
    assert first.output_disabled
    assert rig.drive.trace.index("estop") < rig.drive.trace.index("close")
    second = await rig.runtime.shutdown("again")
    assert second is first
    assert rig.drive.trace.count("close") == 1


async def test_a_silent_shutdown_sends_nothing_at_all() -> None:
    """Its stop is already in the drive's hands, and a conforming close writes a stop sequence."""
    rig = await _running_rig()
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    await rig.step()
    rig.drive.trace.clear()
    report = await rig.runtime.shutdown("SIGTERM")
    assert report.silent
    assert report.emergency is None
    assert report.closed is False
    assert rig.drive.trace == []


async def test_a_close_that_cannot_prove_standstill_leaves_the_output_believed_live() -> None:
    """``StopUnconfirmed`` says the motor may be turning. It must not read as disabled."""
    rig = await _running_rig()
    rig.drive.close_error = StopUnconfirmed(
        waited=Seconds(20.0), last_output_rpm=MotorRpm(400), detail="ramp too slow"
    )
    report = await rig.runtime.shutdown("SIGTERM")
    assert report.closed is False
    assert report.output_disabled is False
    assert "400 rpm" in report.detail


async def test_a_close_that_merely_failed_does_not_claim_the_output_is_live() -> None:
    """A refused frame energised nothing, so the previous belief stands."""
    rig = _rig()
    assert is_ok(await rig.start())
    rig.drive.close_error = CommTimeout(after=Seconds(0.5))
    report = await rig.runtime.shutdown("SIGTERM")
    assert report.closed is False
    assert report.output_disabled is False
    assert isinstance(report, ShutdownReport)


async def test_shutting_down_a_runtime_that_never_started_is_harmless() -> None:
    """``atexit`` runs whether or not a session ever existed."""
    rig = _rig()
    report = await rig.runtime.shutdown("atexit")
    assert report.emergency is EmergencyStopOutcome.NOTHING_SENT
    assert rig.runtime.end_reason is EndReason.SHUTDOWN


# =========================================================================
# Ticking outside a session
# =========================================================================


async def test_the_emergency_zero_uses_the_budget_the_drive_says_it_can_keep() -> None:
    """The drive owns the bound, because only the transport knows what one write costs.

    A constant chosen here (it used to be 0.5 s) was a bound the ATV320 driver
    silently overran: one Modbus transaction can spend its serial timeout
    several times, and the emergency write may first wait out one in flight.
    """
    rig = _rig()
    rig.runtime.request_estop("test")
    assert rig.drive.emergency_budgets == [FAKE_EMERGENCY_BUDGET]
    assert rig.drive.emergency_budgets == [rig.drive.emergency_budget]


def _phases(snapshots: Sequence[TelemetrySnapshot]) -> Iterator[Phase]:
    for snapshot in snapshots:
        yield snapshot.phase


async def test_an_aborted_session_still_monitors_its_recovery_window() -> None:
    """RECOVERY carries the highest vasovagal risk. Monitoring does not stop with the machine."""
    rig = await _running_rig()
    rig.runtime.request_stop("stop")
    await rig.run(20.0)
    assert rig.phase() is Phase.RECOVERY
    await rig.run(30.0)
    assert rig.phase() is Phase.RECOVERY
    assert rig.state() is RuntimeState.ENDING
    await rig.run(25.0)
    assert rig.phase() is Phase.DONE
    assert rig.state() is RuntimeState.FINISHED
    assert set(_phases(rig.snapshots)) >= {Phase.COOLDOWN, Phase.RECOVERY, Phase.DONE}


# =========================================================================
# The guards that cannot be reached by a well-typed caller
# =========================================================================
#
# Every match in the runtime ends in `case _ as unreachable: assert_never(...)`,
# and both checkers refuse an incomplete one - so no well-typed caller can reach
# these arms. They are tested anyway, for the two reasons the contract is
# explicit about: the coverage claim on this module has to be honest rather than
# waived with a pragma, and a corrupted value must fail LOUDLY rather than fall
# into whichever branch happens to be last. A safety action that quietly
# classified itself as "no objection" would hand the machine straight back to the
# control law.


def test_a_value_that_is_not_a_drive_error_fails_loudly() -> None:
    """Silently classifying an unknown error as "the output stage is fine" is the hazard."""
    with pytest.raises(AssertionError):
        describe_drive_error(cast("DriveError", "not-an-error"))


def test_a_value_that_is_not_a_phase_fails_loudly() -> None:
    """Defaulting to "motion still to come" would keep the output stage energised."""
    with pytest.raises(AssertionError):
        motion_is_over(cast("Phase", "not-a-phase"))


async def test_a_safety_action_that_is_not_one_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted action must not fall through the command match into "no objection".

    Falling through would hand the machine straight back to the control law,
    which is the one outcome this whole two-layer arrangement exists to prevent.

    The corruption is injected at the supervisor's own exit rather than through
    ``trip_from_thread``, because a bogus action handed to the supervisor fails
    inside *its* own logging first and the runtime never sees it. What is under
    test here is the runtime's command match.
    """
    rig = await _running_rig()

    def _bogus(_self: SafetySupervisor, _observation: object) -> SafetyVerdict | None:
        return SafetyVerdict(
            action=cast("SafetyAction", "not-an-action"),
            rule="rig_bogus",
            detail="corrupt",
            latched=False,
            since=Monotonic(0.0),
        )

    monkeypatch.setattr(SafetySupervisor, "evaluate", _bogus)
    with pytest.raises(AssertionError):
        await rig.step()
    assert rig.drive.commanded_rpm == 0, "the reference was not zeroed on the way out"
    assert rig.runtime.silent


async def test_a_repeated_failure_keeps_the_instant_of_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An equally severe latch does not replace the one already standing.

    Dwell time is what separates a spike from a trend, so the older instant is
    the more informative one - and "when did this machine go out of service?"
    must not be answered with "a moment ago" forever, one tick at a time.

    A supervisor that raises is the honest way to make the tick fail twice: once
    the runtime is silent it stops talking to the drive, so a drive that raises
    can only produce the first failure.
    """
    rig = await _running_rig()

    def _boom(_self: SafetySupervisor, _observation: object) -> SafetyVerdict | None:
        raise RuntimeError("the supervisor's state is corrupt")

    monkeypatch.setattr(SafetySupervisor, "evaluate", _boom)
    with pytest.raises(RuntimeError):
        await rig.step()
    first = rig.runtime.standing
    assert first is not None
    assert first.action is SafetyAction.GO_SILENT
    assert first.rule == RULE_TICK_EXCEPTION

    with pytest.raises(RuntimeError):
        await rig.step()
    again = rig.runtime.standing
    assert again is not None
    assert again.since == first.since, "an equally severe latch replaced the original"
    ending = rig.runtime.ending
    assert ending is not None
    assert ending.at == first.since, "the ending was re-dated by the second failure"


async def test_an_emergency_outcome_that_is_not_one_fails_loudly() -> None:
    """No third thing may be confused with either "sent" or "nothing sent"."""
    rig = await _running_rig()
    rig.drive.emergency_override = cast("EmergencyStopOutcome", "not-an-outcome")
    with pytest.raises(AssertionError):
        rig.runtime.request_estop("under test")


async def test_an_acknowledge_refusal_that_is_not_one_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor's refusal union is matched exhaustively here too."""
    rig = _rig()

    def _bogus(
        _self: SafetySupervisor,
        _operator: str,
        *,
        estop_released: bool = False,  # noqa: ARG001 - the name is part of the signature
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        return Err(cast("AcknowledgeRefusal", "not-a-refusal"))

    monkeypatch.setattr(SafetySupervisor, "acknowledge", _bogus)
    with pytest.raises(AssertionError):
        rig.runtime.acknowledge(OPERATOR)


# =========================================================================
# The remaining reads and the remaining failure corners
# =========================================================================


async def test_the_startup_gate_is_pollable_before_and_after_the_attestation() -> None:
    """A UI polls it, so it must be pure and silent in both answers."""
    rig = _rig()
    assert is_err(rig.runtime.require_estop_confirmed())
    assert is_ok(rig.runtime.confirm_estop_wiring(OPERATOR))
    attested = rig.runtime.require_estop_confirmed()
    assert is_ok(attested)
    assert attested.value.operator == OPERATOR


async def test_the_standing_action_reads_as_none_when_nobody_is_asking() -> None:
    """Saves every consumer an ``if``, and settles in one place what "no verdict" means."""
    rig = await _running_rig()
    assert rig.standing_action() is SafetyAction.NONE
    rig.runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
    await rig.step()
    assert rig.standing_action() is SafetyAction.FREEZE


async def test_the_last_drive_failure_is_kept_for_the_operator_screen() -> None:
    """ "Out of range" alone is useless at 2am; the classified detail carries the numbers."""
    rig = await _running_rig()
    assert rig.last_failure() is None
    rig.drive.speed_error = CommTimeout(after=Seconds(0.75))
    await rig.step()
    failure = rig.last_failure()
    assert failure is not None
    assert "0.750" in failure.detail
    assert failure.output_unknown is False


async def test_a_command_word_refused_mid_sequence_abandons_the_start() -> None:
    """The sequence is 6 -> 7 -> 15; a caller that pressed on would enable blind."""
    rig = _rig()
    rig.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.5))
    refusal = await rig.start()
    assert is_err(refusal)
    assert isinstance(refusal.error, DriveUnavailable)
    assert rig.drive.commands == [ControlWord.SHUTDOWN]
    assert rig.runtime.output_enabled is False


async def test_a_refused_shutdown_word_leaves_the_output_believed_live() -> None:
    """Word 7 landed and word 6 did not, so the run command is still there. Say so.

    And say it to the operator, not only to the log: after
    ``RuntimeLimits.disable_attempts`` refusals no word removes the output
    stage, so the runtime latches ``disable_refused`` GO_SILENT and stops
    writing - the drive's own ttO then drops the stage. It used to retry every
    tick for the rest of the session with no verdict at all.
    """
    rig = await _running_rig()
    rig.drive.command_errors[ControlWord.SHUTDOWN] = CommTimeout(after=Seconds(0.5))
    rig.runtime.request_stop("stop")
    await rig.run(20.0)
    assert rig.runtime.applied_rpm == 0
    assert ControlWord.SWITCH_ON in rig.drive.commands[3:]
    assert ControlWord.SHUTDOWN not in rig.drive.commands[3:]
    assert rig.runtime.output_enabled, "a disable was claimed that never landed"
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_DISABLE_REFUSED
    assert standing.action is SafetyAction.GO_SILENT
    assert "SHUTDOWN" in standing.detail
    assert rig.runtime.silent
    refused = [entry for entry in rig.drive.trace if entry == "cmd:SHUTDOWN"]
    assert len(refused) == 1 + LIMITS.disable_attempts, "retried past the bound (arming +5)"


async def test_going_silent_with_no_link_sends_nothing_and_claims_nothing() -> None:
    """There is no frame to send down a link that has already been released."""
    rig = _rig()
    assert is_ok(await rig.start())
    await rig.runtime.shutdown("SIGTERM")
    calls = rig.drive.emergency_calls
    rig.runtime.trip_from_thread("rig_silent", SafetyAction.GO_SILENT, "under test")
    await rig.step()
    assert rig.runtime.silent
    assert rig.drive.emergency_calls == calls


async def test_an_idle_runtime_accumulates_no_zone_time() -> None:
    """With no programme there is no zone, so a heart rate belongs to none of the three."""
    rig = _rig()
    rig.feed(Bpm(127))
    await rig.step(feed=False)
    await rig.step(feed=False)
    assert rig.runtime.counters.total == 0.0
    assert rig.runtime.snapshot().live_bpm == 127


# =========================================================================
# Evidence the runtime must not fabricate, and beliefs it must not hold
# =========================================================================


async def test_the_safety_observation_never_reports_a_stale_reading_as_live() -> None:
    """What reaches the rules is asserted directly, by recording the observation.

    Three fields at once, because they fail together: past the staleness window
    the drive's state must read ``COMM_LOST`` - "unknown", never ``NOT_READY``,
    which reads as "stopped" to anything that does not know better - and the
    measured speed, the current and the fault must be ``None`` rather than a
    fabricated zero. A made-up 0 rpm is what would make ``no_load`` and
    ``reverse_rotation`` judge a machine that is not there.
    """
    seen: list[SafetyObservation] = []
    rig = await _running_rig()
    real = SafetySupervisor.evaluate

    def _record(self: SafetySupervisor, observation: SafetyObservation) -> SafetyVerdict | None:
        seen.append(observation)
        return real(self, observation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _record)
        # Reads fail while writes keep succeeding, so the status goes stale
        # without the failure run length ever reaching the comms-lost limit.
        rig.drive.status_error = BadResponse(detail="short read")
        await rig.run(5.0)
    latest = seen[-1]
    assert latest.drive_state is DriveState.COMM_LOST
    assert latest.measured_rpm is None
    assert latest.current is None
    assert latest.fault is None


async def test_the_run_command_is_never_removed_on_a_remembered_standstill() -> None:
    """An old reading of zero is not evidence that the shaft is stopped now.

    This is the one decision that must never be taken on a memory: removing the
    run command from a shaft that is still turning is CiA402 transition 8, which
    drops the output stage and coasts for minutes.
    """
    rig = _rig()
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    # Two seconds of BASELINE: the setpoint is zero and the shaft is at rest, so
    # the LAST status says standstill. Then reads stop arriving.
    await rig.run(2.0)
    assert rig.runtime.applied_rpm == 0
    rig.drive.status_error = BadResponse(detail="short read")
    await rig.run(3.0)
    assert rig.runtime.snapshot().drive_status_is_stale
    rig.runtime.request_stop("stop")
    await rig.run(1.0)
    assert rig.phase() is not Phase.BASELINE
    assert ControlWord.SHUTDOWN not in rig.drive.commands[3:]
    assert rig.runtime.output_enabled


async def test_a_refused_ramp_stop_word_is_retried_a_bounded_number_of_times_then_forced() -> None:
    """Word 7 refused at standstill: a few more tries, then word 6 - and the operator is told.

    Word 6 straight after a refused word 7 would be transition 8, which on a
    TURNING shaft drops the output stage into a freewheel; so within the bound
    only word 7 is retried. This test used to assert that word 6 was never sent
    at all, which is the defect the failure matrix found
    (drive_refuses_switch_on_at_stop): ~480 refused frames, the stage left
    enabled, no verdict, nothing on the screen. At the bound the shaft is
    confirmed stopped by a fresh RFRD with a zero reference, where transition 8
    lets nothing coast, so word 6 removes the stage and a latched
    ``disable_refused`` names the refused word.
    """
    rig = await _running_rig()
    rig.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.5))
    mark = len(rig.drive.trace)
    rig.runtime.request_stop("stop")
    await rig.run(20.0)
    assert rig.runtime.applied_rpm == 0
    assert abs(rig.drive.shaft_rpm) < 1.0
    words = [e for e in rig.drive.trace[mark:] if e in {"cmd:SWITCH_ON", "cmd:SHUTDOWN"}]
    assert words == ["cmd:SWITCH_ON"] * LIMITS.disable_attempts + ["cmd:SHUTDOWN"]
    assert not rig.runtime.output_enabled
    assert rig.drive.state is FakeState.READY
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_DISABLE_REFUSED
    assert standing.action is SafetyAction.RAMP_DOWN
    assert standing.latched
    assert "SWITCH_ON" in standing.detail
    assert not rig.runtime.silent


async def test_a_disable_refused_fewer_times_than_the_bound_is_simply_retried() -> None:
    """A transient refusal: the next tick's word 7 lands, nothing is latched."""
    rig = await _running_rig()
    # Back to the resting rate before the stop: the rig's 65 bpm lure, 17 below the
    # measured 82 once the load is off, is exactly what hr_drop now calls a collapse.
    rig.fed_bpm = Bpm(82)
    rig.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.5))
    rig.runtime.request_stop("stop")
    while not rig.drive.trace.count("cmd:SWITCH_ON") > 1 + LIMITS.disable_attempts - 2:
        await rig.step()
    del rig.drive.command_errors[ControlWord.SWITCH_ON]
    await rig.run(2.0)
    assert not rig.runtime.output_enabled
    assert rig.runtime.standing is None


async def test_the_runtimes_own_latch_outranks_a_milder_supervisor_verdict() -> None:
    """Two verdicts stand at once and the more severe one must win.

    The precommanded latch is ``QUICK_STOP`` and nothing but an acknowledgement
    will ever produce it again, so a milder rule firing afterwards must not
    displace it on the operator's screen or in the command path.
    """
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(240))
    assert is_err(await rig.start())
    rig.runtime.trip_from_thread("rig_mild", SafetyAction.FREEZE, "a milder demand")
    await rig.step()
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.action is SafetyAction.QUICK_STOP
    assert standing.rule == RULE_DRIVE_PRECOMMANDED


async def test_a_write_that_is_acknowledged_but_never_lands_cannot_run_the_speed_away() -> None:
    """A Modbus write to a wrong address is acked while the reference never moves.

    The control base is LFRD read back for exactly this case. With the echo as
    the base the increment is added to what the drive is really working to, so a
    setpoint that is not landing simply fails to climb - and the operator is told
    so. With the believed value as the base the controller would add to its own
    fiction every tick and demand the ceiling.
    """
    rig = _rig()
    rig.drive.swallow_writes = True
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    rig.fed_bpm = Bpm(65)
    await rig.run(15.0)
    assert rig.drive.commanded_rpm == 0, "the fake accepted a write it was told to swallow"
    reached = max(snapshot.setpoint.motor_rpm for snapshot in rig.snapshots)
    assert reached < 100, reached


async def test_an_unknown_applied_speed_holds_the_setpoint_rather_than_rebuilding_it() -> None:
    """Adding an increment to an unknown base is how a machine ends up somewhere else.

    With no fresh observation the controller declines to move at all. The
    dangerous alternative is a fabricated zero base: the increment would then be
    the whole demand, and a machine at 165 rpm would be commanded to something
    near its minimum instead of holding.

    ``comms_lost`` is pushed out of reach here on purpose. With its real limit a
    status read that fails on every tick ends the session within three ticks -
    long before the status goes stale - which is the point of counting reads
    apart from writes. What this test isolates is the controller's behaviour
    on a stale base, so it needs a link that stays up long enough to go stale.
    """
    rig = await _running_rig(safety=_lenient_comms())
    held = rig.runtime.applied_rpm
    rig.drive.status_error = BadResponse(detail="short read")
    await rig.run(6.0)
    assert rig.runtime.snapshot().drive_status_is_stale
    assert rig.runtime.applied_rpm == held


async def test_a_failed_write_does_not_advance_the_believed_setpoint() -> None:
    """The value KNOWN to be in force is the previous one, and the keepalive re-asserts it.

    A failed write may still have landed, so the believed setpoint could be
    wrong in either direction - but only one of the two is safe to assume. If the
    belief advanced, the next keepalive would assert a speed nothing ever
    acknowledged, and the telemetry would show a commanded speed the drive never
    agreed to.

    The writes are failed while the control law is climbing in HOLD, so the ones
    that fail are writes that would have CHANGED the value. A test that failed
    only the keepalive would pass on a runtime with no such rule at all, which is
    why the attempted frames are checked too.

    ``comms_lost`` is out of reach here (see :func:`_lenient_comms`): writes
    that fail on every tick for four seconds would otherwise, correctly, end
    the session within three ticks.
    """
    rig = await _running_rig(safety=_lenient_comms())
    while rig.phase() is not Phase.HOLD:
        await rig.step()
    before = rig.runtime.applied_rpm
    assert 0 < before < REAL_PROFILE.max_rpm, "no headroom left for a changed write"
    rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
    await rig.run(4.0)
    attempted = {entry for entry in rig.drive.trace if entry.startswith("lfrd:")}
    assert len(attempted) > 1, "no write of a changed value was ever attempted"
    assert rig.runtime.applied_rpm == before
    assert rig.drive.commanded_rpm == before


async def test_transient_failures_do_not_accumulate_into_a_lost_link() -> None:
    """``consecutive_comm_failures`` is a run length, not a total.

    A link that drops one frame now and then is not a link that has gone away,
    and a counter that never reset would take three dropped frames over a whole
    session as grounds for handing the stop to the drive's timeout.
    """
    rig = await _running_rig()
    for _ in range(6):
        rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
        await rig.step()
        rig.drive.speed_error = None
        await rig.run(1.0)
    assert not rig.runtime.silent
    assert rig.runtime.standing is None


# =========================================================================
# Comms loss is judged per kind of exchange
# =========================================================================


def _comms_limit() -> int:
    """The ``comms_lost`` threshold the standard rig runs with."""
    return _safety_for(_profile()).comms_lost_failures


async def test_a_status_read_failing_on_every_tick_is_comms_lost_while_writes_succeed() -> None:
    """THE review finding: an acknowledged keepalive is not proof the drive is watched.

    The keepalive goes out first on every tick and its acknowledgement used to
    reset the one shared failure counter, so a status read failing on every
    tick never got past a run length of one and ``comms_lost`` could not fire.
    Only a successful READ may shorten the read run.
    """
    rig = await _running_rig()
    limit = _comms_limit()
    rig.drive.status_error = BadResponse(detail="short read")
    acknowledged = len(rig.drive.writes)
    for _ in range(limit):
        assert not rig.runtime.silent
        await rig.step()
    assert len(rig.drive.writes) > acknowledged, "the keepalive writes were not succeeding"
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_COMMS_LOST
    assert standing.action is SafetyAction.GO_SILENT
    assert rig.runtime.silent


async def test_writes_failing_on_every_tick_are_comms_lost_while_reads_succeed() -> None:
    """The mirror image: a drive that can be read but not commanded is not in hand."""
    rig = await _running_rig()
    limit = _comms_limit()
    rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
    for _ in range(limit):
        assert not rig.runtime.silent
        await rig.step()
    assert not rig.runtime.snapshot().drive_status_is_stale, "the reads were not succeeding"
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_COMMS_LOST
    assert rig.runtime.silent


async def test_a_dead_tick_counts_once_not_once_per_exchange() -> None:
    """The run length judged is the longer of the two, never their sum.

    A tick on a dead link fails its write and its read. Summing them would fire
    ``comms_lost`` at half its configured number of ticks, and a brief glitch
    shorter than the configured tolerance would end a session.
    """
    rig = await _running_rig()
    limit = _comms_limit()
    rig.drive.speed_error = CommTimeout(after=Seconds(0.5))
    rig.drive.status_error = CommTimeout(after=Seconds(0.5))
    for _ in range(limit - 1):
        await rig.step()
    assert not rig.runtime.silent
    rig.drive.speed_error = None
    rig.drive.status_error = None
    await rig.run(2.0)
    assert not rig.runtime.silent
    assert rig.runtime.standing is None


# =========================================================================
# A verdict that lifts must not release the controller's stale demand
# =========================================================================


@dataclass(slots=True)
class _Imposed:
    """A verdict a test forces onto the supervisor's exit, or ``None`` for none.

    Mutable on purpose: the test flips it between ticks to make a non-latched
    verdict appear and lift on demand, which no heart-rate script can do
    precisely enough.
    """

    action: SafetyAction | None = None


def _imposing(
    imposed: _Imposed,
) -> Callable[[SafetySupervisor, SafetyObservation], SafetyVerdict | None]:
    """Wrap the real ``evaluate`` so ``imposed`` wins while it is set.

    The real supervisor still runs on every tick, so its own rules (and their
    latches) stay live underneath the imposed verdict.
    """
    original = SafetySupervisor.evaluate

    def evaluate(self: SafetySupervisor, observation: SafetyObservation) -> SafetyVerdict | None:
        real = original(self, observation)
        action = imposed.action
        if action is None:
            return real
        return SafetyVerdict(
            action=action,
            rule="rig_imposed",
            detail="under test",
            latched=False,
            since=observation.now,
        )

    return evaluate


def _changes(
    trace: Sequence[tuple[float, MotorRpm, SafetyAction]],
) -> list[tuple[float, MotorRpm, SafetyAction]]:
    """Keep only the ticks on which the setpoint actually moved (plus the first)."""
    kept = [trace[0]]
    for entry in trace[1:]:
        if entry[1] != kept[-1][1]:
            kept.append(entry)
    return kept


def _assert_slew_between_changes(
    trace: Sequence[tuple[float, MotorRpm, SafetyAction]], slew: float
) -> None:
    """``|delta setpoint| <= slew * dt`` between successive changes, verdicts or not.

    ``dt`` is the time since the setpoint last *moved*, which is the form of the
    bound the rest of this file states (see the freeze test): the control law
    decides once per period and may spend the whole period's allowance in one
    write. The only exemption is the one the design names: a QUICK_STOP (or
    anything more severe) zeroes the reference at once.
    """
    for (before_at, before_rpm, _), (after_at, after_rpm, action) in pairwise(_changes(trace)):
        if after_rpm == 0 and action >= SafetyAction.QUICK_STOP:
            continue
        span = after_at - before_at
        assert abs(after_rpm - before_rpm) <= slew * span + 1, (before_rpm, after_rpm, span, action)


def _history(rig: Rig) -> list[tuple[float, MotorRpm, SafetyAction]]:
    """Every tick so far, so the first change measured has its true predecessor.

    Seeding the trace with only "now" would date the previous change to the
    moment recording began, when the setpoint may have last moved seconds
    earlier - and the bound is stated from the last *change*.
    """
    return [
        (float(snapshot.at), snapshot.setpoint.motor_rpm, SafetyAction.NONE)
        for snapshot in rig.snapshots
    ]


async def _record(
    rig: Rig, imposed: _Imposed, ticks: int, trace: list[tuple[float, MotorRpm, SafetyAction]]
) -> None:
    for _ in range(ticks):
        await rig.step()
        standing = rig.runtime.standing_action
        forced = SafetyAction.NONE if imposed.action is None else imposed.action
        trace.append((float(rig.now), rig.runtime.applied_rpm, max(standing, forced)))


async def test_a_lifted_reduce_does_not_jump_back_to_the_old_demand() -> None:
    """THE review finding, reproduced: 60 -> 276 rpm in one write when REDUCE lifted.

    The controller ran the rig up to its ceiling, a non-latched REDUCE walked the
    setpoint down, and while it did so the controller kept its standing demand
    at the ceiling. The tick the verdict lifted re-emitted that demand.
    """
    rig = await _running_rig()
    await rig.run(14.0)
    high = rig.runtime.applied_rpm
    assert high == REAL_PROFILE.max_rpm, "the rig never reached its ceiling"
    imposed = _Imposed()
    trace = _history(rig)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        imposed.action = SafetyAction.REDUCE
        while rig.runtime.applied_rpm > 60:
            await _record(rig, imposed, 1, trace)
        low = rig.runtime.applied_rpm
        imposed.action = None
        await _record(rig, imposed, 1, trace)
        assert rig.runtime.applied_rpm <= low, (low, rig.runtime.applied_rpm)
        await _record(rig, imposed, 20, trace)
    _assert_slew_between_changes(trace, LIMITS.slew)
    assert max(rpm for _, rpm, _ in trace[-20:]) < high, "the old demand came back"


_VERDICTS: Final[tuple[SafetyAction | None, ...]] = (
    None,
    None,
    SafetyAction.FREEZE,
    SafetyAction.REDUCE,
    SafetyAction.REDUCE,
    SafetyAction.RAMP_DOWN,
    SafetyAction.QUICK_STOP,
)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    segments=st.lists(
        st.tuples(
            st.sampled_from(_VERDICTS),
            st.integers(min_value=40, max_value=150),
            st.integers(min_value=1, max_value=30),
        ),
        min_size=2,
        max_size=10,
    )
)
async def test_the_setpoint_respects_the_slew_limit_across_verdict_transitions(
    segments: list[tuple[SafetyAction | None, int, int]],
) -> None:
    """The slew bound, over arbitrary appearances and liftings of verdicts.

    Every tick a verdict decides the output the controller is rebased onto what
    was actually applied, so no transition - into a verdict, out of one, or
    from one to another - can release a demand the machine is not running at.
    """
    rig = await _running_rig()
    imposed = _Imposed()
    trace = _history(rig)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SafetySupervisor, "evaluate", _imposing(imposed))
        for action, rate, ticks in segments:
            imposed.action = action
            rig.fed_bpm = Bpm(rate)
            await _record(rig, imposed, ticks, trace)
    _assert_slew_between_changes(trace, LIMITS.slew)


# =========================================================================
# Idle, read-only polling
# =========================================================================


def _writes_in(trace: Sequence[str]) -> list[str]:
    """Every frame in ``trace`` that is a write of any kind."""
    return [entry for entry in trace if entry.startswith(("lfrd", "cmd", "estop"))]


async def test_an_idle_runtime_reads_the_drive_at_two_hertz_and_writes_nothing() -> None:
    """Invariant 7 of the console plan: at rest, ``read_status`` and nothing else.

    At REST. This test used to run on a drive found OPERATION_ENABLED at 300 rpm
    and assert that it was only read - which is the defect the failure matrix
    found (drive_stuck_enabled_idle_console): the read-only poll fed the drive's
    ttO and a crashed predecessor's motor kept turning for as long as the
    console sat idle. A drive found enabled or turning is now stopped; see
    ``test_a_drive_found_enabled_while_idle_is_stopped_latched_and_disabled``.
    """
    rig = _rig(state=FakeState.SWITCHED_ON, rpm=MotorRpm(0))
    snapshots = await rig.run(1.0, feed=False, ping=False)
    assert rig.drive.trace[0] == "open"
    assert rig.drive.trace.count("open") == 1
    # Five ticks at 5 Hz, one read every 0.5 s: the ticks at 0.2, 0.8 (and not 0.4, 0.6).
    assert rig.drive.trace.count("eta") == 2
    assert _writes_in(rig.drive.trace) == []
    last = snapshots[-1]
    assert last.measured.motor_rpm == 0
    assert last.drive_state is DriveState.SWITCHED_ON
    assert last.drive_status_age is not None
    assert last.setpoint.motor_rpm == 0
    link = rig.runtime.idle_link
    assert link == IdleLink(open=True, reads=2, last_latency=Seconds(0.0))
    assert rig.state() is RuntimeState.IDLE
    assert rig.runtime.standing is None


async def test_a_drive_found_enabled_while_idle_is_stopped_latched_and_disabled() -> None:
    """Contract rule 8 on the IDLE console: zero, ramp-stop, disable, latch, tell. Never resume.

    The failure matrix's evidence: a drive left OPERATION_ENABLED at 900 motor
    rpm stayed there for the whole 60 s idle pre-roll (120 s on the panel) with
    no verdict, because the idle poll only read it - and the reads fed its ttO.
    """
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(300))
    first = await rig.step(feed=False, ping=False)
    # The first idle read found it; the reference was zeroed in the same tick.
    assert rig.drive.commanded_rpm == 0
    assert rig.drive.writes[0] == 0
    standing = first.safety
    assert standing is not None
    assert standing.rule == RULE_DRIVE_PRECOMMANDED
    assert standing.action is SafetyAction.QUICK_STOP
    assert standing.latched
    assert "idle console" in standing.detail
    # No SHUTDOWN (transition 8) while it turns: the drive ramps on its own ramp.
    assert ControlWord.SHUTDOWN not in rig.drive.commands
    assert rig.drive.is_enabled()
    await rig.run(8.0, feed=False, ping=False)
    # At standstill: word 7 then word 6, and the output stage is off.
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert rig.drive.commands == [ControlWord.SWITCH_ON, ControlWord.SHUTDOWN]
    assert not rig.drive.is_enabled()
    assert not rig.runtime.output_enabled
    assert rig.state() is RuntimeState.FINISHED
    # Nothing starts again until a named operator acknowledges.
    refused = await rig.start()
    assert is_err(refused)
    assert isinstance(refused.error, SafetyStanding)
    assert max(rig.drive.writes) == 0
    assert is_ok(rig.runtime.acknowledge(OPERATOR))
    assert rig.runtime.standing is None


async def test_a_shaft_found_coasting_while_idle_is_flagged_but_never_energised() -> None:
    """Turning with the output stage OFF: the zero is written, the stage is never switched on.

    Only the output stage can brake a coasting shaft, and energising it would be
    starting a machine nobody asked to start. So the verdict latches and the
    operator is told; the shaft coasts down on its own.
    """
    rig = _rig(state=FakeState.READY, rpm=MotorRpm(400))
    await rig.step(feed=False, ping=False)
    standing = rig.runtime.standing
    assert standing is not None
    assert standing.rule == RULE_DRIVE_PRECOMMANDED
    assert "found the drive READY at" in standing.detail
    assert not rig.runtime.output_enabled
    await rig.run(10.0, feed=False, ping=False)
    assert rig.drive.commands == []
    assert rig.drive.state is FakeState.READY
    assert set(rig.drive.writes) == {0}


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rpm=st.integers(min_value=0, max_value=1380),
    idle=st.floats(min_value=0.0, max_value=5.0),
)
async def test_a_drive_seen_enabled_while_idle_always_ends_at_zero_disabled_and_latched(
    rpm: int, idle: float
) -> None:
    """PROPERTY: whatever speed the idle console finds an ENABLED drive at, it ends stopped.

    At zero, with the output stage off, a latched ``drive_precommanded`` and not a
    single non-zero reference written, however long the console had been idle -
    including an enabled drive at 0 rpm, which is one word away from turning.
    """
    rig = _rig(state=FakeState.OPERATION_ENABLED, rpm=MotorRpm(rpm))
    rig.drive.advance(Monotonic(rig.now + idle))
    await rig.run(90.0, feed=False, ping=False)
    assert abs(rig.drive.shaft_rpm) < 1.0
    assert not rig.drive.is_enabled()
    assert not rig.runtime.output_enabled
    assert set(rig.drive.writes) == {0}
    floor = rig.runtime.standing
    assert floor is not None
    assert floor.rule == RULE_DRIVE_PRECOMMANDED
    assert floor.latched


async def test_an_idle_fault_is_shown_but_ends_nothing_it_never_started() -> None:
    """The fake's ttO trips on reads-only polling; the raw LFT code reaches the screen.

    The idle observation never reaches the supervisor, so a fault on a drive
    nobody started cannot end a session, latch a verdict or go silent.
    """
    rig = _rig()
    snapshots = await rig.run(3.0, feed=False, ping=False)
    last = snapshots[-1]
    assert last.drive_state is DriveState.FAULT
    assert last.fault is not None
    assert last.fault.fault is DriveFault.MODBUS_COMM_LOSS
    assert rig.state() is RuntimeState.IDLE
    assert rig.runtime.standing is None
    assert not rig.runtime.silent
    assert _writes_in(rig.drive.trace) == []


async def test_a_failed_idle_open_is_counted_shown_and_retried_on_the_next_poll() -> None:
    rig = _rig()
    rig.drive.break_comms()
    await rig.step(feed=False, ping=False)
    link = rig.runtime.idle_link
    assert not link.open
    assert link.failures == 1
    assert link.consecutive_failures == 1
    assert link.last_error is not None
    assert rig.runtime.snapshot().drive_state is DriveState.COMM_LOST
    # Polls at +0.2 (above), then +0.8 and +1.4: one open attempt each.
    await rig.run(1.2, feed=False, ping=False)
    assert rig.drive.trace.count("open") == 3
    assert rig.runtime.idle_link.consecutive_failures == 3
    # Never counted towards comms_lost: nothing latched, nothing silent.
    assert rig.runtime.standing is None
    assert not rig.runtime.silent


async def test_a_failed_idle_read_reopens_and_a_later_success_clears_the_run() -> None:
    rig = _rig()
    await rig.step(feed=False, ping=False)
    rig.drive.status_error = CommTimeout(after=Seconds(0.5))
    await rig.run(0.6, feed=False, ping=False)
    failed = rig.runtime.idle_link
    assert failed.failures == 1
    assert not failed.open
    rig.drive.status_error = None
    await rig.run(0.6, feed=False, ping=False)
    healed = rig.runtime.idle_link
    assert healed.open
    assert healed.consecutive_failures == 0
    assert healed.failures == 1
    assert rig.drive.trace.count("open") == 2


async def test_an_idle_reading_goes_stale_and_is_then_shown_as_unknown() -> None:
    """Past ``status_stale_after`` the idle reading is a memory: COMM_LOST, no current."""
    rig = _rig()
    await rig.step(feed=False, ping=False)
    rig.drive.status_error = CommTimeout(after=Seconds(0.5))
    snapshots = await rig.run(3.0, feed=False, ping=False)
    last = snapshots[-1]
    assert last.drive_state is DriveState.COMM_LOST
    assert last.current is None
    assert last.drive_status_age is not None
    assert last.drive_status_age > LIMITS.status_stale_after


async def test_a_silent_idle_runtime_stops_polling() -> None:
    """A frame after silence restarts ``ttO``, reads included."""
    rig = _rig()
    rig.drive.raise_on_status = True
    with pytest.raises(RuntimeError):
        await rig.step(feed=False, ping=False)
    assert rig.runtime.silent
    before = len(rig.drive.trace)
    rig.drive.raise_on_status = False
    rig.clock.advance(IDLE_POLL_PERIOD)
    await rig.runtime.tick(rig.now)
    assert len(rig.drive.trace) == before


async def test_a_start_refused_after_opening_leaves_the_idle_poll_to_the_session_path() -> None:
    """A refused start keeps its link; the idle poll does not open a second one."""
    rig = _rig()
    rig.drive.inject_fault(DriveFault.OVERCURRENT)
    assert is_err(await rig.start())
    opens = rig.drive.trace.count("open")
    snapshot = await rig.step(feed=False, ping=False)
    assert rig.drive.trace.count("open") == opens
    assert rig.runtime.idle_link == IdleLink()
    assert snapshot.drive_state is DriveState.FAULT


async def test_the_idle_poll_stops_once_a_session_has_started() -> None:
    rig = await _running_rig()
    assert rig.runtime.idle_link == IdleLink()


async def test_the_heart_rate_trend_is_the_trackers_own() -> None:
    rig = _rig()
    before = rig.runtime.heart_rate_trend
    assert before is None
    for bpm in (70, 72, 74, 76, 78):
        rig.clock.advance(Seconds(1.0))
        rig.feed(Bpm(bpm))
    after = _trend(rig)
    assert after is not None
    assert after > 0.0


def _trend(rig: Rig) -> BpmPerMinute | None:
    """Read through a call, so mypy does not keep the earlier ``None`` narrowing."""
    return rig.runtime.heart_rate_trend


# =========================================================================
# The vasovagal gate: no rise while the heart rate falls fast
# =========================================================================


def _short_trend(readings: Sequence[tuple[float, int]]) -> float | None:
    """The gate's own trend, recomputed from the readings fed (instant, bpm): the last five."""
    tracker = HeartRateTracker()
    for seq, (at, bpm) in enumerate(readings, start=1):
        tracker.observe(
            HeartRateSample(bpm=Bpm(bpm), quality=SignalQuality.GOOD, seq=seq, at=Monotonic(at))
        )
    return tracker.recent_rate(LIMITS.trend_samples)


async def test_a_falling_heart_rate_holds_the_setpoint_before_any_verdict() -> None:
    """THE finding: 968 -> 1039 motor rpm while a collapse took the rate 145 -> 121.

    The control law reads a falling rate as "below the zone" and accelerates;
    hr_drop only trips once the fall reaches 25 bpm. Here the rate falls 9 bpm
    in six seconds - far below hr_drop - and the setpoint does not rise at all,
    with no verdict standing: it is the gate, not the supervisor, that holds it.
    When the fall stops, the controller climbs again.
    """
    rig = await _running_rig()
    rig.fed_bpm = Bpm(70)
    await rig.run(10.0)
    start = rig.runtime.applied_rpm
    held: list[MotorRpm] = []
    for bpm in (68, 66, 65, 63, 62, 61):
        rig.fed_bpm = Bpm(bpm)
        for _ in range(5):
            await rig.step()
            held.append(rig.runtime.applied_rpm)
            assert rig.runtime.standing is None
    assert all(b <= a for a, b in pairwise([start, *held])), held
    rig.fed_bpm = Bpm(61)
    await rig.run(15.0)
    assert rig.runtime.applied_rpm > held[-1], "the controller never resumed after the fall"


async def test_an_unknown_trend_permits_no_rise() -> None:
    """Too few readings to fit a slope is no licence to accelerate."""
    rig = _rig(tracker_limits=TrackerLimits(max_jump=Bpm(1), reseed_after=1))
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(11.0)
    # Every reading now jumps and reseeds the tracker: one accepted reading at a time.
    for bpm in (60, 40, 60, 40, 60, 40, 60, 40):
        rig.fed_bpm = Bpm(bpm)
        await rig.run(1.0)
    assert rig.runtime.applied_rpm == 0


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(steps=st.lists(st.integers(min_value=-6, max_value=4), min_size=10, max_size=40))
async def test_the_setpoint_never_rises_while_the_heart_rate_trend_is_falling_fast(
    steps: list[int],
) -> None:
    """PROPERTY: for any heart-rate sequence, no tick with a trend below the gate rises.

    The trend is recomputed here, independently of the runtime, from the
    readings the rig actually fed (their instants and values), with the tracker
    the runtime uses; whenever it is below ``falling_trend`` the applied
    setpoint at the end of the tick is no higher than at its start. The
    verdicts are free to do what they like - they only ever lower it.
    """
    rig = await _running_rig()
    fed: list[tuple[float, int]] = []
    bpm = 65
    for step in steps:
        bpm = min(160, max(40, bpm + step))
        rig.fed_bpm = Bpm(bpm)
        for _ in range(5):
            before, seq = rig.runtime.applied_rpm, rig.seq
            await rig.step()
            if rig.seq > seq:
                fed.append((rig.last_feed, bpm))
            trend = _short_trend(fed)
            if trend is not None and trend < LIMITS.falling_trend:
                assert rig.runtime.applied_rpm <= before, (fed[-6:], trend)


# =========================================================================
# Programmes walk the anti-nausea motion limits too
# =========================================================================

LEG_TIP: Final[Metres] = Metres(2.43)
"""The rider's leg tip on the CAD upper bound: where the g-rate limit must hold."""


def _climbing_profile(**overrides: object) -> TrainingProfile:
    """A programme that asks for the nameplate: a heart far below its zone, a long HOLD."""
    fields: dict[str, object] = {
        "total_duration_s": Seconds(220.0),
        "warmup_max_s": Seconds(20.0),
        "hold_min_s": Seconds(10.0),
        "cooldown_s": Seconds(10.0),
        "recovery_s": Seconds(60.0),
        "max_rpm": MotorRpm(1380),
        "hard_max_bpm": Bpm(148),
        "critical_bpm": Bpm(158),
    }
    fields.update(overrides)
    return _profile(**fields)


async def _climbing_rig(
    *, limit_radius: Metres | None = None, **overrides: object
) -> tuple[Rig, list[tuple[float, MotorRpm, SafetyAction]]]:
    rig = _rig(
        profile=_climbing_profile(**overrides),
        motion=DEFAULT_MOTION_LIMITS,
        limit_radius=limit_radius,
    )
    rig.fed_bpm = Bpm(82)
    assert is_ok(await rig.start())
    await rig.run(10.0)
    rig.fed_bpm = Bpm(60)
    trace: list[tuple[float, MotorRpm, SafetyAction]] = []
    return rig, trace


async def _trace(rig: Rig, trace: list[tuple[float, MotorRpm, SafetyAction]], s: float) -> None:
    for _ in range(round(s / TICK)):
        await rig.step()
        trace.append((float(rig.now), rig.runtime.applied_rpm, rig.runtime.standing_action))


def _assert_motion_limited(
    trace: Sequence[tuple[float, MotorRpm, SafetyAction]], geometry: MachineGeometry
) -> None:
    """Every change outside a verdict within the motion limits at ``geometry``'s radius.

    Speed: ``rate x window + 1`` (one carried rpm), the rate taken at the faster
    end, the window capped at the limiter's own accrual cap. Load: the g-rate at
    that radius, with one rpm of slack. The 0 <-> min_run passage is exempt, as
    the motion module documents.
    """
    motion = DEFAULT_MOTION_LIMITS
    last_change = trace[0][0]
    for (_, a, _), (t, b, action) in pairwise(trace):
        if a == b:
            continue
        window = t - last_change
        last_change = t
        if action is not SafetyAction.NONE or 0 in (a, b):
            continue
        rate = motor_rate_limit(MotorRpm(max(a, b)), motion, geometry)
        capped = min(window, float(motion.max_interval) + 1.0 / rate)
        assert abs(b - a) <= rate * capped + 1 + 1e-9, (a, b, window)
        g_a, g_b = (geometry.view(rpm).g_load for rpm in (a, b))
        slack = geometry.view(MotorRpm(max(a, b) + 1)).g_load - geometry.view(max(a, b)).g_load
        assert abs(g_b - g_a) <= float(motion.g_rate) * capped + slack + 1e-9, (a, b, window)


async def test_a_programme_climbs_at_the_anti_nausea_limits_not_in_controller_steps() -> None:
    """THE finding: the control law stepped up to 75 rpm per 5 s period and the drive ran it.

    The measured arm then accelerated at up to 0.54 output rpm/s against a 0.25
    limit, and the g-dot limit was not applied at all. Now the controller's
    demand, the warm-up and the cooldown are all walked by the motion profiler.
    """
    rig, trace = await _climbing_rig()
    await _trace(rig, trace, 150.0)
    assert max(rpm for _, rpm, _ in trace) == 1380, "the rig never reached the nameplate"
    _assert_motion_limited(trace, GEOMETRY)
    await _trace(rig, trace, 60.0)
    _assert_motion_limited(trace, GEOMETRY)


async def test_the_g_rate_limit_can_be_judged_at_the_leg_tip() -> None:
    """``limit_radius``: 0.03 g/s at the reference radius is 0.049 g/s at 2.43 m.

    Judged at the leg tip the climb above ~22 output rpm is slower, and every
    change stays inside the g-rate limit AT the leg tip.
    """
    rig, trace = await _climbing_rig(limit_radius=LEG_TIP)
    await _trace(rig, trace, 150.0)
    tip = replace(GEOMETRY, radius=LEG_TIP)
    _assert_motion_limited(trace, tip)
    fast, fast_trace = await _climbing_rig()
    await _trace(fast, fast_trace, 150.0)
    reached = next(t for t, rpm, _ in trace if rpm == 1380)
    reached_fast = next(t for t, rpm, _ in fast_trace if rpm == 1380)
    # Above ~22 output rpm the g-dot at 2.43 m binds (0.20 output rpm/s against 0.25):
    # the last 5.7 output rpm take ~26 s instead of ~23.
    assert reached > reached_fast + 2.0, (reached, reached_fast)


@pytest.mark.parametrize("radius", [Metres(1.0), Metres(math.nan), Metres(math.inf)])
def test_a_limit_radius_that_would_loosen_the_limit_is_refused(radius: Metres) -> None:
    with pytest.raises(ValueError, match="limit_radius"):
        _rig(limit_radius=radius)


def test_the_limit_radius_defaults_to_the_reference_radius() -> None:
    assert motion_geometry(GEOMETRY, None) is GEOMETRY
    assert motion_geometry(GEOMETRY, GEOMETRY.radius).radius == GEOMETRY.radius
    assert motion_geometry(GEOMETRY, LEG_TIP).radius == LEG_TIP
    assert motion_geometry(GEOMETRY, LEG_TIP).ratio == GEOMETRY.ratio


@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    rates=st.lists(st.integers(min_value=50, max_value=160), min_size=5, max_size=30),
    radius=st.sampled_from((None, LEG_TIP)),
)
async def test_every_programme_setpoint_change_stays_inside_the_motion_limits(
    rates: list[int], radius: Metres | None
) -> None:
    """PROPERTY: for any heart-rate history, outside a verdict, speed and g-dot are bounded.

    At the limit radius when one is given, at the reference radius otherwise.
    """
    rig, trace = await _climbing_rig(limit_radius=radius)
    for bpm in rates:
        rig.fed_bpm = Bpm(bpm)
        await _trace(rig, trace, 4.0)
    geometry = GEOMETRY if radius is None else replace(GEOMETRY, radius=radius)
    _assert_motion_limited(trace, geometry)


# =========================================================================
# DONE only once the setpoint is zero
# =========================================================================


async def test_the_timeline_ending_before_the_cooldown_keeps_supervising_in_recovery() -> None:
    """A motion-limited cooldown can outlast the programme's timeline; DONE would stop the rules."""
    rig, trace = await _climbing_rig()
    await _trace(rig, trace, 130.0)
    assert rig.runtime.applied_rpm == 1380
    # The timeline: HOLD ends at 150 s, COOLDOWN is 10 s, RECOVERY ends at 220 s.
    await _trace(rig, trace, 85.0)
    assert rig.runtime.applied_rpm > 0
    assert rig.phase() is Phase.RECOVERY
    await _trace(rig, trace, 120.0)
    assert rig.runtime.applied_rpm == 0
    assert rig.phase() is Phase.DONE


async def test_an_ending_that_outlasts_its_recovery_is_not_done_while_it_turns() -> None:
    rig, trace = await _climbing_rig(total_duration_s=Seconds(400.0))
    await _trace(rig, trace, 145.0)
    rig.runtime.request_stop("stop")
    await _trace(rig, trace, 75.0)
    assert rig.runtime.applied_rpm > 0
    assert rig.phase() is Phase.RECOVERY
    await _trace(rig, trace, 60.0)
    assert rig.runtime.applied_rpm == 0
    assert rig.phase() is Phase.DONE
    assert rig.runtime.end_reason is EndReason.OPERATOR_STOP
