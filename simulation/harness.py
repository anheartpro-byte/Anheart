"""Layer 1: drive the REAL training runtime against the REAL simulators, and record it all.

Everything the machine does here is production code from ``raspberry-pi/src``:

* :class:`~src.training.runtime.TrainingRuntime` (control law, safety
  supervisor, motion profiler, every exit path) decides every setpoint,
* :class:`~src.motor.simulated.SimulatedDrive` is the ATV320 (CiA402 state
  machine, ramps, coast-down, the ``ttO`` watchdog), wrapped in
  :class:`~simulation.recording.RecordingDrive` so every frame it receives is
  logged,
* :class:`~src.sim.physiology.Physiology` is the rider's heart, driven by the
  measured speed exactly as ``src.local_panel`` feeds it,
* the heart rate reaches the runtime either through the production ECG path
  (simulated BITalino -> real ``SignalTreatment`` -> ``EcgBridge``) or through
  the :mod:`simulation.sensors` model with the same output contract.

This module adds only the loop, the scenario's actions, and the recording. The
tick order matches ``LocalPanel.control_step``: actions (the mailbox), the
presence ping, the ECG, the plant, then ``runtime.tick``.

Time: the battery runs on :class:`~src.clock.ManualClock` (deterministic, as
``src/clock.py`` prescribes for tests); the live viewer runs the same loop on
:class:`~src.clock.SimClock` at N x real time through :class:`PacedTicker`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, assert_never, final

from simulation.faultdrive import FaultyDrive
from simulation.faultsource import FaultySource
from simulation.recording import RecordingDrive
from simulation.rig import RigGeometry, load_geometry
from simulation.scenario import (
    Acknowledge,
    Action,
    AttendantLeaves,
    BitalinoDisconnect,
    BitalinoSignal,
    ClockJump,
    CommsLoss,
    DriveEchoMismatch,
    DriveLatency,
    DriveRefuseCommand,
    DriveReverse,
    DriveSpeedStuck,
    DriveStatusFrozen,
    EcgDropout,
    EcgMode,
    EcgQuality,
    EcgRepeatSeq,
    EcgSeqGap,
    EcgSilentStop,
    EcgValue,
    EmergencyStop,
    Expectation,
    InjectDriveFault,
    Injection,
    LoopStall,
    ManualTarget,
    OperatorFaultReset,
    OperatorStop,
    RegisterOffset,
    RemoteStop,
    Scenario,
    SessionKind,
    Shutdown,
    StartAgain,
    TickException,
)
from simulation.sensors import DirectSensor
from simulation.tracefile import Event, FinalState, Row, Trace, frame_to_json
from src.bitalino_client import SampleBatch
from src.clock import Clock, SimClock
from src.ecg_pipeline import EcgBridge, EcgFrame, Treatment, load_treatment, treat_ecg
from src.local_config import OCCUPIED_INITIAL_RESULTANT_G
from src.local_panel import (
    describe_reset_refusal,
    describe_start_refusal,
    describe_target_refusal,
)
from src.motor.simulated import SimState, SimulatedDrive, SimulatedDriveConfig
from src.result import Err, Ok, Result
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology, SubjectState
from src.training.motion import MotionLimits, load_motion_limits
from src.training.plan import INITIAL_REV, SHIPPED_DEFAULTS_PATH, Program, TrainingProfile
from src.training.runtime import (
    RuntimeLimits,
    RuntimeState,
    StartRefusal,
    Subject,
    TrainingRuntime,
)
from src.training.safety import SafetyLimits
from src.training.types import Occupancy, TelemetrySnapshot
from src.units import (
    Bpm,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    OutputRpm,
    RpmPerSecond,
    Seconds,
    UnixMillis,
    elapsed,
)

MILLIS_PER_SECOND: Final[int] = 1000

TICK: Final[Seconds] = Seconds(0.2)
"""The control period: ``src.local_panel.CONTROL_PERIOD`` (5 Hz)."""

PREROLL: Final[Seconds] = Seconds(15.0)
"""Idle ticks before the start command: the ECG settles and the idle poll runs,
as on a console that has been open for a moment before anybody presses start."""

OPERATOR: Final[str] = "sim operator"
ORIGIN: Final[Monotonic] = Monotonic(100.0)
EPOCH: Final[UnixMillis] = UnixMillis(1_700_000_000_000)

RUNTIME_LIMITS: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10)
)
"""Mirrors ``src.local_panel.RUNTIME_LIMITS`` (asserted equal in the tests)."""

PANEL_SAFETY: Final[SafetyLimits] = SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
"""Mirrors ``src.local_panel.PANEL_HARD_MAX_BPM`` / ``PANEL_CRITICAL_BPM`` for manual sessions."""

MOTION_LIMITS_PATH: Final = SHIPPED_DEFAULTS_PATH.parent / "motion_limits.json"
"""``raspberry-pi/config/motion_limits.json``: the anti-nausea limits, as shipped."""

ECG_SAMPLE_RATE: Final[int] = 1000
DSP_OUTPUT_RATE: Final[int] = 250
FINISHED_LINGER: Final[Seconds] = Seconds(5.0)
"""Ticks kept after the runtime reports FINISHED, when no action is still pending."""

ENERGISED: Final[frozenset[SimState]] = frozenset(
    {SimState.OPERATION_ENABLED, SimState.FAULT_REACTION_RAMP_STOP, SimState.DISABLING_ON_RAMP}
)
"""The simulator's torque-producing states (``SimulatedDrive._energised``)."""

_ENABLED: Final[SimState] = SimState.OPERATION_ENABLED

DIRECT_ONLY_ACTIONS: Final[tuple[type[object], ...]] = (
    EcgDropout,
    EcgQuality,
    EcgRepeatSeq,
    EcgValue,
    EcgSeqGap,
    EcgSilentStop,
)

DSP_ONLY_ACTIONS: Final[tuple[type[object], ...]] = (BitalinoSignal,)


def load_motion() -> MotionLimits:
    """The shipped motion limits. Raises ``ValueError`` when unusable, as the console does."""
    loaded = load_motion_limits(MOTION_LIMITS_PATH)
    if isinstance(loaded, Err):
        raise ValueError(f"motion limits unusable: {loaded.error}")
    return loaded.value


# =========================================================================
# Time
# =========================================================================


@final
class HarnessClock:
    """The deterministic clock of a run: :class:`~src.clock.ManualClock` plus a wall-clock step.

    Monotonic time moves only by :meth:`advance`; the wall clock is that plus
    an offset :meth:`jump_wall` can step either way, as NTP does to a Pi with no
    RTC. Nothing in the control path may care (contract rule 4 puts every
    timing decision on the monotonic clock); the ``clock_jump`` action checks it.
    """

    __slots__ = ("_epoch_millis", "_now")

    def __init__(self, start: Monotonic = ORIGIN, epoch_millis: UnixMillis = EPOCH) -> None:
        self._now: Monotonic = start
        self._epoch_millis: int = int(epoch_millis)

    def monotonic(self) -> Monotonic:
        return self._now

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(self._epoch_millis + int(self._now * MILLIS_PER_SECOND))

    def advance(self, delta: Seconds) -> Monotonic:
        """Move time forward. Raises ``ValueError`` for a negative step, as ManualClock does."""
        if delta < 0.0:
            raise ValueError(f"cannot advance a monotonic clock by {delta}")
        self._now = Monotonic(self._now + delta)
        return self._now

    def jump_wall(self, delta: Seconds) -> None:
        """Step the wall clock (only) by ``delta``, either way."""
        self._epoch_millis += round(delta * MILLIS_PER_SECOND)


class Ticker(Protocol):
    """Hands out the next tick instant; owns the clock the whole rig reads."""

    @property
    def clock(self) -> Clock: ...

    async def next(self) -> Monotonic: ...


@final
class ManualTicker:
    """Deterministic: each tick moves a :class:`HarnessClock` by exactly :data:`TICK`."""

    __slots__ = ("_clock",)

    def __init__(self, clock: HarnessClock | None = None) -> None:
        self._clock: HarnessClock = HarnessClock() if clock is None else clock

    @property
    def clock(self) -> Clock:
        return self._clock

    async def next(self) -> Monotonic:
        return self._clock.advance(TICK)


@final
class SleepingTicker:
    """Deterministic simulated time, shown at ``speed`` x: exactly :data:`TICK` per tick,
    then a real sleep of ``TICK / speed``. The live viewer's default.

    Unlike :class:`PacedTicker`, a slow host cannot compress a real hiccup into
    simulated seconds: at 200 x on a :class:`SimClock` a 15 ms pause is a 3 s gap
    between ticks, and the runtime's ``loop_stall`` rule rightly goes silent.
    """

    __slots__ = ("_clock", "_speed")

    def __init__(self, speed: float, clock: HarnessClock | None = None) -> None:
        if not speed > 0.0:
            raise ValueError(f"speed must be positive, got {speed}")
        self._speed: float = speed
        self._clock: HarnessClock = HarnessClock() if clock is None else clock

    @property
    def clock(self) -> Clock:
        return self._clock

    async def next(self) -> Monotonic:
        await asyncio.sleep(float(TICK) / self._speed)
        return self._clock.advance(TICK)


@final
class PacedTicker:
    """Real time, scaled: a :class:`SimClock` and a sleep until the next tick is due."""

    __slots__ = ("_clock", "_due")

    def __init__(self, clock: SimClock) -> None:
        self._clock: SimClock = clock
        self._due: Monotonic = clock.monotonic()

    @property
    def clock(self) -> Clock:
        return self._clock

    async def next(self) -> Monotonic:
        self._due = Monotonic(self._due + TICK)
        remaining = elapsed(self._clock.monotonic(), self._due)
        if remaining > 0.0:
            await asyncio.sleep(self._clock.real_seconds_for(remaining))
        return self._clock.monotonic()


# =========================================================================
# The result
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class TargetOutcome:
    """What became of one ``manual_target`` action."""

    at: Seconds
    requested: OutputRpm
    expected: Expectation
    accepted: bool
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestOutcome:
    """What became of one other operator request (a second START, a fault reset)."""

    at: Seconds
    request: str
    expected: Expectation
    accepted: bool
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OperatorMessage:
    """One thing the operator is told, in the console's own words.

    ``source`` is ``verdict`` (a safety verdict's rule and sentence), ``fault``
    (the drive's mnemonic, LFT code and meaning) or ``refusal`` (a refused
    start, target or reset, worded by ``src.local_panel``'s ``describe_*``).
    """

    t: float
    source: str
    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PrerollView:
    """The idle console before the start: what the shaft did while nobody commanded it."""

    peak_motor_rpm: int
    final_motor_rpm: int
    rules: tuple[str, ...]
    """Every safety rule that stood during the pre-roll."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """A finished run: the trace plus everything the checks need to judge it."""

    scenario: Scenario
    rig: RigGeometry
    motion: MotionLimits
    runtime_limits: RuntimeLimits
    profile: TrainingProfile | None
    manual_ceiling: MotorRpm | None
    start_refusal: str | None
    targets: tuple[TargetOutcome, ...]
    trace: Trace
    requests: tuple[RequestOutcome, ...] = ()
    messages: tuple[OperatorMessage, ...] = ()
    preroll: PrerollView = PrerollView(peak_motor_rpm=0, final_motor_rpm=0, rules=())


type RowSink = Callable[[Row], Awaitable[None]]


class _NoWaveform:
    """A waveform sink that drops the waveform: the trace records rates, not samples."""

    __slots__ = ()

    def record_ecg(self, values: Sequence[Millivolts]) -> int:
        return len(values)


async def _treat_inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    """The DSP on the loop: the harness has no control deadline to protect."""
    return treat_ecg(treatment, batch)


def _validate(scenario: Scenario) -> None:
    """Refuse a scenario the harness cannot run faithfully. Raises ``ValueError``."""
    if scenario.ecg.mode is EcgMode.DSP:
        for action in scenario.actions:
            if isinstance(action, DIRECT_ONLY_ACTIONS):
                raise ValueError(
                    f"{type(action).__name__} needs ecg.mode 'direct'; in 'dsp' mode "
                    "use a subject event (electrode_off, mains_burst) instead"
                )
    else:
        for action in scenario.actions:
            if isinstance(action, DSP_ONLY_ACTIONS):
                raise ValueError(f"{type(action).__name__} needs ecg.mode 'dsp'")
    if scenario.kind is SessionKind.AUTO and scenario.profile is None:
        raise ValueError("an auto scenario needs a profile")


def _resolve_rig(scenario: Scenario) -> RigGeometry:
    """The CAD geometry with the scenario's overrides. Raises ``ValueError`` when unusable."""
    loaded = load_geometry(
        reference_radius=scenario.reference_radius, leg_tip_radius=scenario.leg_tip_radius
    )
    if isinstance(loaded, Err):
        raise ValueError(f"geometry unusable: {loaded.error.detail}")
    return loaded.value


def _safety_for(profile: TrainingProfile | None) -> SafetyLimits:
    """A programme's own cardiac tiers, or the console's for a manual session."""
    if profile is None:
        return PANEL_SAFETY
    return SafetyLimits(hard_max_bpm=profile.hard_max_bpm, critical_bpm=profile.critical_bpm)


def _manual_ceiling(scenario: Scenario, rig: RigGeometry) -> MotorRpm | None:
    """``LocalConfig.ceiling_for``: the bench ceiling, or the first-trial 1.2 g if occupied."""
    if scenario.kind is not SessionKind.MANUAL:
        return None
    ceiling = scenario.manual.ceiling
    if scenario.manual.occupancy is Occupancy.OCCUPIED:
        trial = rig.machine.motor_rpm_for(OCCUPIED_INITIAL_RESULTANT_G)
        ceiling = MotorRpm(min(ceiling, trial))
    return ceiling


@dataclass(frozen=True, slots=True, kw_only=True)
class _EcgWiring:
    sensor: DirectSensor | None
    bitalino: SimulatedBitalinoClient | None
    bridge: EcgBridge | None
    source: FaultySource | None


def _wire_ecg(
    scenario: Scenario, rig: RigGeometry, clock: Clock, runtime: TrainingRuntime
) -> _EcgWiring:
    """The DIRECT sensor model, or the production path (BITalino sim -> DSP -> bridge)."""
    origin = clock.monotonic()
    match scenario.ecg.mode:
        case EcgMode.DIRECT:
            sensor = DirectSensor(
                start=origin,
                period=scenario.ecg.period,
                noise_bpm=scenario.ecg.noise_bpm,
                seed=scenario.ecg.seed,
                ectopic_rate=scenario.ecg.ectopic_rate,
                ectopic_bpm=scenario.ecg.ectopic_bpm,
                motion_noise_bpm_per_g=scenario.ecg.motion_noise_bpm_per_g,
            )
            if scenario.ecg.connect_fails:
                sensor.disconnect()
            return _EcgWiring(sensor=sensor, bitalino=None, bridge=None, source=None)
        case EcgMode.DSP:
            client = SimulatedBitalinoClient(
                clock,
                physiology=Physiology(
                    origin=origin,
                    geometry=rig.machine,
                    config=scenario.subject,
                    script=scenario.events,
                ),
                channels=(0,),
                sample_rate=ECG_SAMPLE_RATE,
            )
            if scenario.ecg.connect_fails:
                client.inject_connect_failure()
            source = FaultySource(client, clock, seed=scenario.ecg.seed)
            bridge = EcgBridge(
                clock=clock,
                source=source,
                treatment=load_treatment(ECG_SAMPLE_RATE, DSP_OUTPUT_RATE),
                heart_rate=runtime,
                waveform=_NoWaveform(),
                sample_rate=ECG_SAMPLE_RATE,
                treat=_treat_inline,
            )
            return _EcgWiring(sensor=None, bitalino=client, bridge=bridge, source=source)
        case _ as unreachable:
            assert_never(unreachable)


# =========================================================================
# The session
# =========================================================================


@final
class Session:
    """One scenario, wired and run. Mutable, single-use, owned by one event loop."""

    __slots__ = (
        "_attendant",
        "_bitalino",
        "_bridge",
        "_ceiling",
        "_drive",
        "_events",
        "_faulty",
        "_finished_at",
        "_last_fault",
        "_last_measured",
        "_last_phase",
        "_last_rule",
        "_last_verdict",
        "_latency_until",
        "_messages",
        "_motion",
        "_pending",
        "_preroll_final",
        "_preroll_peak",
        "_preroll_rules",
        "_requests",
        "_rig",
        "_rows",
        "_runtime",
        "_scenario",
        "_sensor",
        "_shutdown_detail",
        "_sim",
        "_sink",
        "_source",
        "_stalled",
        "_start_at",
        "_start_refusal",
        "_subject",
        "_targets",
        "_ticker",
        "_truth",
    )

    def __init__(
        self,
        scenario: Scenario,
        *,
        rig: RigGeometry | None = None,
        ticker: Ticker | None = None,
        sink: RowSink | None = None,
    ) -> None:
        """Wire the rig. Raises ``ValueError`` on a scenario the harness cannot run."""
        _validate(scenario)
        rig = _resolve_rig(scenario) if rig is None else rig
        self._scenario: Scenario = scenario
        self._rig: RigGeometry = rig
        self._ticker: Ticker = ManualTicker() if ticker is None else ticker
        self._sink: RowSink | None = sink
        clock = self._ticker.clock
        self._motion: MotionLimits = load_motion()
        enabled = scenario.drive.initial_enabled_rpm
        self._sim: SimulatedDrive = SimulatedDrive(
            clock,
            SimulatedDriveConfig(
                max_rpm=scenario.drive.hsp_motor_rpm,
                tto=scenario.drive.tto,
                acceleration_time=scenario.drive.acceleration_time,
                # The read-only idle poll must not trip SLF, as on the console.
                reads_reset_watchdog=True,
            ),
            initial_state=SimState.SWITCH_ON_DISABLED if enabled is None else _ENABLED,
            initial_rpm=MotorRpm(0) if enabled is None else enabled,
        )
        if scenario.drive.initial_fault is not None:
            self._sim.inject_fault(scenario.drive.initial_fault)
        self._faulty: FaultyDrive = FaultyDrive(self._sim)
        for word in scenario.drive.refuse_commands:
            self._faulty.refuse(word)
        self._drive: RecordingDrive = RecordingDrive(self._faulty, clock)
        self._runtime: TrainingRuntime = TrainingRuntime(
            clock=clock,
            drive=self._drive,
            geometry=rig.machine,
            limits=RUNTIME_LIMITS,
            safety=_safety_for(scenario.profile),
            motion=self._motion,
            # The anti-nausea g-rate is judged at the rider's farthest point: the
            # feet, or the reference radius if a scenario puts the feet inside it.
            limit_radius=Metres(max(float(rig.leg_tip_radius), float(rig.machine.radius))),
        )
        self._ceiling: MotorRpm | None = _manual_ceiling(scenario, rig)
        origin = clock.monotonic()
        self._subject: Physiology = Physiology(
            origin=origin, geometry=rig.machine, config=scenario.subject, script=scenario.events
        )
        # In DSP mode the client owns a plant of its own; this one is its twin,
        # driven with the same speed at the tick instants, for the truth column.
        self._truth: SubjectState = self._subject.advance(origin, MotorRpm(0))
        ecg = _wire_ecg(scenario, rig, clock, self._runtime)
        self._sensor: DirectSensor | None = ecg.sensor
        self._bitalino: SimulatedBitalinoClient | None = ecg.bitalino
        self._bridge: EcgBridge | None = ecg.bridge
        self._source: FaultySource | None = ecg.source
        self._pending: list[Action] = list(scenario.actions)
        self._attendant: bool = True
        self._last_measured: MotorRpm = MotorRpm(0)
        self._latency_until: Monotonic | None = None
        self._rows: list[Row] = []
        self._events: list[Event] = []
        self._targets: list[TargetOutcome] = []
        self._requests: list[RequestOutcome] = []
        self._messages: list[OperatorMessage] = []
        self._last_fault: str | None = None
        self._preroll_peak: int = 0
        self._preroll_final: int = 0
        self._stalled: bool = False
        self._preroll_rules: list[str] = []
        self._start_at: Monotonic = origin
        self._start_refusal: str | None = None
        self._shutdown_detail: str | None = None
        self._finished_at: Monotonic | None = None
        self._last_phase: str | None = None
        self._last_rule: str | None = None
        self._last_verdict: str | None = None

    # -- the run ----------------------------------------------------------

    async def run(self) -> RunResult:
        """Pre-roll, start, run the horizon, exit like the console, tear down, probe."""
        await self._leave_drive_running()
        if self._bitalino is not None and await self._bitalino.connect():
            await self._bitalino.start_acquisition()
        for _ in range(round(self._scenario.preroll / TICK)):
            await self._step(await self._ticker.next(), record=False)
        self._start_at = self._ticker.clock.monotonic()
        self._preroll_final = abs(int(self._last_measured))
        await self._start()
        alive = True
        while alive:
            now = await self._ticker.next()
            if elapsed(self._start_at, now) > self._scenario.duration + 1e-9:
                break
            alive = await self._step(now, record=True)
            if alive and self._done(now):
                break
        if self._shutdown_detail is None and self._runtime.state is not RuntimeState.IDLE:
            report = await self._runtime.shutdown("simulation: console exit at the horizon")
            self._shutdown_detail = report.detail
            self._event(self._ticker.clock.monotonic(), "shutdown", report.detail)
            self._message(self._ticker.clock.monotonic(), "shutdown", report.detail)
        await self._teardown()
        return await self._result()

    async def _leave_drive_running(self) -> None:
        """``initial_enabled_rpm``: the crashed predecessor's last frame left LFRD there.

        Written on the bare simulator (not recorded: it is not this console's
        frame), and the link dropped at once, as a process dying does.
        """
        enabled = self._scenario.drive.initial_enabled_rpm
        if enabled is None:
            return
        await self._sim.open()
        await self._sim.write_speed(enabled)
        await self._sim.close()

    def _done(self, now: Monotonic) -> bool:
        """FINISHED for a while, with nothing left for the scenario to do."""
        if self._runtime.state is not RuntimeState.FINISHED:
            self._finished_at = None
            return False
        if self._finished_at is None:
            self._finished_at = now
        return not self._pending and elapsed(self._finished_at, now) >= FINISHED_LINGER

    async def _start(self) -> None:
        scenario = self._scenario
        self._runtime.confirm_estop_wiring(OPERATOR)
        started = await self._arm()
        now = self._ticker.clock.monotonic()
        if isinstance(started, Err):
            self._start_refusal = type(started.error).__name__
            self._event(now, "start_refused", repr(started.error))
            self._message(now, "refusal", describe_start_refusal(started.error))
        else:
            self._event(now, "start", f"{scenario.kind.value} session started")

    async def _arm(self) -> Result[object, StartRefusal]:
        """The start command, as the console issues it (programme or manual)."""
        runtime = self._runtime
        scenario = self._scenario
        profile = scenario.profile
        if scenario.kind is SessionKind.AUTO and profile is not None:
            program = Program(
                profile=profile,
                source_rev=INITIAL_REV,
                resolved_at=self._ticker.clock.unix_millis(),
                total_overridden=False,
            )
            return await runtime.start(program, Subject(subject_id="sim", operator=OPERATOR))
        occupancy = scenario.manual.occupancy
        ceiling = self._ceiling if self._ceiling is not None else scenario.manual.ceiling
        return await runtime.start_manual(occupancy, OPERATOR, ceiling)

    async def _step(self, now: Monotonic, *, record: bool) -> bool:
        """One tick in ``LocalPanel.control_step`` order. ``False`` once the process is gone."""
        if record:
            alive = await self._apply_actions(now)
            if not alive:
                return False
            # A loop stall inside the actions moved the clock on: the tick runs late.
            now = self._ticker.clock.monotonic() if self._stalled else now
            self._stalled = False
        if self._attendant:
            self._runtime.note_presence(now)
        await self._feed_ecg(now)
        self._heal(now)
        self._sim.advance(now)
        try:
            snapshot = await self._runtime.tick(now)
        except Exception as error:  # the console's task failing, recorded
            self._event(now, "tick_exception", repr(error))
            report = await self._runtime.shutdown("simulation: console task failed")
            self._shutdown_detail = report.detail
            self._event(now, "shutdown", report.detail)
            self._message(now, "shutdown", report.detail)
            return False
        self._last_measured = snapshot.measured.motor_rpm
        self._note_messages(now, snapshot)
        if not record:
            self._note_preroll(snapshot)
        if record:
            row = self._row(now, snapshot)
            self._rows.append(row)
            self._note_changes(now, snapshot)
            if self._sink is not None:
                await self._sink(row)
        return True

    def _heal(self, now: Monotonic) -> None:
        """End an injected latency window: the link is healthy again."""
        until = self._latency_until
        if until is not None and now >= until:
            self._sim.inject_latency(Seconds(0.0))
            self._latency_until = None

    async def _feed_ecg(self, now: Monotonic) -> None:
        self._truth = self._subject.advance(now, self._last_measured)
        sensor = self._sensor
        if sensor is not None:
            reading = sensor.sample(now, self._truth.heart_rate, float(self._truth.g_load))
            if reading is not None:
                self._runtime.observe_ecg(now, reading.seq, reading.quality, reading.bpm)
        bitalino = self._bitalino
        bridge = self._bridge
        if bitalino is not None and bridge is not None and bitalino.is_acquiring:
            bitalino.set_motor_rpm(self._last_measured)
            await bridge.pump()

    # -- actions ------------------------------------------------------------

    async def _apply_actions(self, now: Monotonic) -> bool:
        at = elapsed(self._start_at, now)
        while self._pending and self._pending[0].at <= at + 1e-9:
            action = self._pending.pop(0)
            if not await self._apply(now, action):
                return False
        return True

    async def _apply(self, now: Monotonic, action: Action) -> bool:  # noqa: PLR0912, PLR0915  # one arm per action
        """One scenario action. ``False`` when it ended the process (shutdown)."""
        runtime = self._runtime
        sim = self._sim
        detail = type(action).__name__
        match action:
            case ManualTarget(at=at, output_rpm=rpm, expect=expect):
                result = runtime.set_manual_target(rpm)
                accepted = isinstance(result, Ok)
                if isinstance(result, Ok):
                    outcome = f"accepted -> {result.value} motor rpm"
                else:
                    outcome = f"refused: {result.error!r}"
                    self._message(now, "refusal", describe_target_refusal(result.error))
                self._targets.append(
                    TargetOutcome(
                        at=at, requested=rpm, expected=expect, accepted=accepted, detail=outcome
                    )
                )
                detail = f"target {rpm} output rpm: {outcome}"
            case OperatorStop():
                runtime.request_stop("operator: stop button")
            case RemoteStop():
                runtime.request_stop("remote: end requested off the machine")
            case EmergencyStop():
                verdict = runtime.request_estop("operator: e-stop")
                detail = f"e-stop: {verdict.rule} {verdict.action.name}"
            case Acknowledge(estop_released=released):
                acknowledged = runtime.acknowledge(OPERATOR, estop_released=released)
                detail = f"acknowledge: {acknowledged!r}"
            case InjectDriveFault(fault=fault):
                sim.inject_fault(fault)
                detail = f"drive fault {fault.name}"
            case CommsLoss(duration=duration):
                sim.inject_comms_loss(duration)
                detail = f"comms loss {duration} s"
            case DriveLatency(latency=latency, duration=duration):
                sim.inject_latency(latency)
                self._latency_until = Monotonic(now + duration)
                detail = f"drive latency {latency} s for {duration} s"
            case DriveReverse():
                sim.inject_reverse()
            case RegisterOffset():
                sim.inject_register_offset_error()
            case EcgDropout(duration=duration):
                self._direct().dropout(Monotonic(now + duration))
                detail = f"ECG dropout {duration} s"
            case EcgQuality(quality=quality, duration=duration):
                self._direct().grade(quality, Monotonic(now + duration))
                detail = f"ECG graded {quality.value} for {duration} s"
            case EcgRepeatSeq(duration=duration):
                self._direct().repeat(Monotonic(now + duration))
                detail = f"ECG metrics re-emitted (same seq) for {duration} s"
            case EcgValue(bpm=bpm, duration=duration):
                self._direct().force(bpm, Monotonic(now + duration))
                detail = f"ECG reports {bpm} bpm for {duration} s"
            case BitalinoDisconnect():
                if self._sensor is not None:
                    self._sensor.disconnect()
                if self._bitalino is not None:
                    await self._bitalino.inject_disconnect()
            case AttendantLeaves():
                self._attendant = False
            case TickException():
                self._drive.raise_on_next_read()
            case Shutdown():
                report = await runtime.shutdown("simulation: SIGTERM")
                self._shutdown_detail = report.detail
                self._event(now, "action", detail)
                self._event(now, "shutdown", report.detail)
                self._message(now, "shutdown", report.detail)
                return False
            case (
                DriveRefuseCommand()
                | DriveEchoMismatch()
                | DriveSpeedStuck()
                | DriveStatusFrozen()
                | LoopStall()
                | ClockJump()
                | EcgSeqGap()
                | EcgSilentStop()
                | BitalinoSignal()
                | StartAgain()
                | OperatorFaultReset()
            ):
                detail = await self._apply_injection(now, action)
            case _ as unreachable:
                assert_never(unreachable)
        self._event(now, "action", detail)
        return True

    async def _apply_injection(self, now: Monotonic, action: Injection) -> str:  # noqa: PLR0911, PLR0912  # one arm per action
        """The failure-injection and operator-error actions. Returns the event detail."""
        faulty = self._faulty
        match action:
            case DriveRefuseCommand(word=word):
                faulty.refuse(word)
                return f"the drive refuses {word.name}"
            case DriveEchoMismatch():
                held = self._sim.commanded_setpoint
                faulty.mismatch_echo(held)
                return f"LFRD echo stuck at {held}"
            case DriveSpeedStuck():
                faulty.stick_speed()
                return "RFRD stuck (the measured speed no longer follows)"
            case DriveStatusFrozen():
                faulty.freeze_status()
                return "the drive's status is frozen"
            case LoopStall(duration=duration):
                await self._stall(duration)
                return f"event loop stalled {duration} s"
            case ClockJump(jump=jump):
                self._harness_clock().jump_wall(jump)
                return f"wall clock stepped {jump:+} s"
            case EcgSeqGap(gap=gap):
                self._direct().skip(gap)
                return f"ECG sequence gap of {gap}"
            case EcgSilentStop():
                self._direct().stop_silently()
                return "ECG readings stop silently"
            case BitalinoSignal(fault=fault, duration=duration):
                self._faulty_source().corrupt(fault, Monotonic(now + duration))
                return f"BITalino signal {fault.value} for {duration} s"
            case StartAgain(at=at, expect=expect):
                started = await self._arm()
                if isinstance(started, Err):
                    self._message(now, "refusal", describe_start_refusal(started.error))
                return self._request(at, "start", expect, started)
            case OperatorFaultReset(at=at, expect=expect):
                reset = await self._runtime.fault_reset()
                if isinstance(reset, Err):
                    self._message(now, "refusal", describe_reset_refusal(reset.error))
                return self._request(at, "fault_reset", expect, reset)
            case _ as unreachable:
                assert_never(unreachable)

    def _request(
        self,
        at: Seconds,
        request: str,
        expect: Expectation,
        result: Result[object, object],
    ) -> str:
        """Record an operator request's outcome; a refused START is worded for the operator."""
        accepted = isinstance(result, Ok)
        detail = "accepted" if isinstance(result, Ok) else f"refused: {result.error!r}"
        self._requests.append(
            RequestOutcome(
                at=at, request=request, expected=expect, accepted=accepted, detail=detail
            )
        )
        return f"{request}: {detail}"

    async def _stall(self, duration: Seconds) -> None:
        """No tick for ``duration``: the plant runs on, nobody services the drive or the ECG."""
        for _ in range(round(duration / TICK)):
            now = await self._ticker.next()
            self._heal(now)
            self._sim.advance(now)
            self._truth = self._subject.advance(now, self._last_measured)
        self._stalled = True

    def _harness_clock(self) -> HarnessClock:
        clock = self._ticker.clock
        if not isinstance(clock, HarnessClock):  # a SimClock (live viewer) cannot step
            raise ValueError("clock_jump needs the deterministic harness clock")
        return clock

    def _faulty_source(self) -> FaultySource:
        source = self._source
        if source is None:  # rejected in __init__; kept as a real check, not an assert
            raise ValueError("bitalino_signal needs ecg.mode 'dsp'")
        return source

    def _direct(self) -> DirectSensor:
        sensor = self._sensor
        if sensor is None:  # rejected in __init__; kept as a real check, not an assert
            raise ValueError("this action needs ecg.mode 'direct'")
        return sensor

    # -- recording ----------------------------------------------------------

    def _event(self, now: Monotonic, kind: str, detail: str) -> None:
        self._events.append(Event(t=elapsed(self._start_at, now), kind=kind, detail=detail))

    def _message(self, now: Monotonic, source: str, text: str) -> None:
        t = elapsed(self._start_at, now)
        self._messages.append(OperatorMessage(t=t, source=source, text=text))
        self._events.append(Event(t=t, kind="operator", detail=text))

    def _note_messages(self, now: Monotonic, snapshot: TelemetrySnapshot) -> None:
        """What the console shows: every new verdict with its sentence, every new drive fault."""
        safety = snapshot.safety
        rule = None if safety is None else f"{safety.rule}:{safety.action.name}"
        if safety is not None and rule != self._last_rule:
            self._message(now, "verdict", f"{safety.rule} ({safety.action.name}): {safety.detail}")
        fault = snapshot.fault
        label = None if fault is None else f"{fault.fault.mnemonic} (LFT {fault.raw_code})"
        if fault is not None and label != self._last_fault:
            self._message(now, "fault", f"{label}: {fault.message}")
        self._last_fault = label
        self._last_rule = rule

    def _note_preroll(self, snapshot: TelemetrySnapshot) -> None:
        self._preroll_peak = max(self._preroll_peak, abs(int(snapshot.measured.motor_rpm)))
        safety = snapshot.safety
        if safety is not None and safety.rule not in self._preroll_rules:
            self._preroll_rules.append(safety.rule)

    def _note_changes(self, now: Monotonic, snapshot: TelemetrySnapshot) -> None:
        phase = snapshot.phase.value
        if phase != self._last_phase:
            self._event(now, "phase", phase)
            self._last_phase = phase
        safety = snapshot.safety
        rule = None if safety is None else f"{safety.rule}:{safety.action.name}"
        if rule != self._last_verdict:
            self._event(now, "verdict", "cleared" if rule is None else rule)
            self._last_verdict = rule

    def _row(self, now: Monotonic, snapshot: TelemetrySnapshot) -> Row:
        rig = self._rig
        measured = snapshot.measured
        output = OutputRpm(abs(measured.output_rpm))
        setpoint = snapshot.setpoint
        setpoint_output = OutputRpm(abs(setpoint.output_rpm))
        safety = snapshot.safety
        current = snapshot.current
        return Row(
            t=elapsed(self._start_at, now),
            state=self._runtime.state.value,
            mode=snapshot.mode.value,
            phase=snapshot.phase.value,
            drive_state=snapshot.drive_state.name,
            sim_state=self._sim.sim_state.name,
            setpoint_motor_rpm=int(setpoint.motor_rpm),
            lfrd_motor_rpm=int(self._sim.commanded_setpoint),
            measured_motor_rpm=int(measured.motor_rpm),
            measured_fresh=not snapshot.drive_status_is_stale,
            output_rpm=float(output),
            hertz=abs(float(measured.hertz)),
            g_reference=float(rig.g_reference(output)),
            g_leg_tip=float(rig.g_leg_tip(output)),
            setpoint_output_rpm=float(setpoint_output),
            setpoint_g_leg_tip=float(rig.g_leg_tip(setpoint_output)),
            manual_target_motor_rpm=int(self._runtime.manual_target),
            hr_true=int(self._truth.heart_rate),
            hr_live=None if snapshot.live_bpm is None else int(snapshot.live_bpm),
            target_bpm=None if snapshot.target_bpm is None else int(snapshot.target_bpm),
            safety_action=snapshot.safety_action.name,
            safety_rule=None if safety is None else safety.rule,
            output_enabled=self._runtime.output_enabled,
            silent=self._runtime.silent,
            current_a=None if current is None else float(current),
        )

    # -- the end --------------------------------------------------------------

    async def _teardown(self) -> None:
        """Let the plant run on, with nobody ticking: the process is gone."""
        clock = self._ticker.clock
        for _ in range(round(self._scenario.teardown / TICK)):
            now = await self._ticker.next()
            self._heal(now)
            self._sim.advance(clock.monotonic())

    async def _probe_shaft(self) -> MotorRpm | None:
        """RFRD read by a fresh connection after teardown, as the e2e tests do."""
        await self._sim.open()
        status = await self._sim.read_status()
        if isinstance(status, Err):
            return None
        return status.value.output_rpm

    async def _result(self) -> RunResult:
        runtime = self._runtime
        sim_state = self._sim.sim_state
        lfrd = self._sim.commanded_setpoint
        shaft = await self._probe_shaft()
        end = runtime.end_reason
        final = FinalState(
            runtime_state=runtime.state.value,
            end_reason=None if end is None else end.value,
            stop_reason=runtime.stop_reason,
            silent=runtime.silent,
            runtime_output_enabled=runtime.output_enabled,
            runtime_applied_rpm=int(runtime.applied_rpm),
            sim_state=sim_state.name,
            energised=sim_state in ENERGISED,
            lfrd_motor_rpm=int(lfrd),
            shaft_motor_rpm=None if shaft is None else int(shaft),
            shutdown_detail=self._shutdown_detail,
        )
        origin = float(self._start_at)
        trace = Trace(
            meta=self.meta(),
            rows=tuple(self._rows),
            frames=tuple(frame_to_json(frame, origin) for frame in self._drive.frames),
            events=tuple(self._events),
            final=final,
        )
        return RunResult(
            scenario=self._scenario,
            rig=self._rig,
            motion=self._motion,
            runtime_limits=RUNTIME_LIMITS,
            profile=self._scenario.profile,
            manual_ceiling=self._ceiling,
            start_refusal=self._start_refusal,
            targets=tuple(self._targets),
            trace=trace,
            requests=tuple(self._requests),
            messages=tuple(self._messages),
            preroll=PrerollView(
                peak_motor_rpm=self._preroll_peak,
                final_motor_rpm=self._preroll_final,
                rules=tuple(self._preroll_rules),
            ),
        )

    def meta(self) -> dict[str, str | float | int | bool | list[str] | None]:
        """The trace header: scenario, geometry, limits. Known before the first tick."""
        scenario = self._scenario
        rig = self._rig
        machine = rig.machine
        profile = scenario.profile
        return {
            "scenario": scenario.name,
            "description": scenario.description,
            "tags": list(scenario.tags),
            "kind": scenario.kind.value,
            "ecg_mode": scenario.ecg.mode.value,
            "tick_s": float(TICK),
            "duration_s": float(scenario.duration),
            "reference_radius_m": float(rig.reference_radius),
            "leg_tip_radius_m": float(rig.leg_tip_radius),
            "leg_tip_measured": rig.leg_tip_measured,
            "arm_tip_radius_m": float(rig.arm_tip_radius),
            "capsule_near_radius_m": float(rig.capsule_near_radius),
            "capsule_far_radius_m": float(rig.capsule_far_radius),
            "counterweight_radius_m": float(rig.counterweight_radius),
            "gear_ratio": float(machine.ratio),
            "nominal_motor_rpm": int(machine.nominal_rpm),
            "base_hz": float(machine.base_hz),
            "hsp_motor_rpm": int(scenario.drive.hsp_motor_rpm),
            "motion_output_accel": float(self._motion.output_accel),
            "motion_g_rate": float(self._motion.g_rate),
            "min_run_motor_rpm": int(self._motion.min_run),
            "runtime_slew_motor_rpm_s": float(RUNTIME_LIMITS.slew),
            "manual_ceiling_motor_rpm": None if self._ceiling is None else int(self._ceiling),
            "zone_low_bpm": None if profile is None else int(profile.zone_low_bpm),
            "zone_high_bpm": None if profile is None else int(profile.zone_high_bpm),
            "hard_max_bpm": None if profile is None else int(profile.hard_max_bpm),
            "critical_bpm": None if profile is None else int(profile.critical_bpm),
            "profile_max_motor_rpm": None if profile is None else int(profile.max_rpm),
            "profile_id": None if profile is None else profile.profile_id,
            "start_refusal": self._start_refusal,
        }


async def run_scenario(
    scenario: Scenario,
    *,
    rig: RigGeometry | None = None,
    ticker: Ticker | None = None,
    sink: RowSink | None = None,
) -> RunResult:
    """Run one scenario to the end and return everything that happened."""
    return await Session(scenario, rig=rig, ticker=ticker, sink=sink).run()
