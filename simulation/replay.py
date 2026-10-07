"""Replay a recorded session against the runtime of this checkout, and compare.

    python -m simulation.run --replay <record folder or .tar.gz>

A schema-2 record (``docs/enregistrement.md``) holds what the runtime was given:
the instants of its ticks, what the drive answered, the raw ECG, and what
people asked. This module gives all of it back to a fresh, REAL
:class:`~src.training.runtime.TrainingRuntime`, in the recorded order, on a
:class:`~src.clock.ManualClock` paced by the recorded instants:

* the drive is :class:`~simulation.replay_tape.TapeDrive`, which answers each
  call with the recorded answer and refuses a call the record does not hold;
* the ECG goes block by block through the real DSP and the real
  :class:`~src.ecg_pipeline.EcgBridge`, as it did, and reaches the runtime
  through ``observe_ecg`` and nothing else;
* the commands of ``events.jsonl`` (:mod:`src.record.commands`) are
  issued at their instants.

What the runtime then DECIDES at each tick (setpoint, safety action, rule,
phase) is compared with what the record says it decided
(:mod:`simulation.replay_compare`). Three outcomes: the same; different (EX-2);
or the runtime asked the drive for something the record does not hold, at which
point the replay stops, because the recorded answers no longer belong to the
questions being asked (EX-3).

**The order of things.** Ticks and commands are merged by instant, to the
millisecond. During the session (``t`` > 0) a command stamped like a tick
comes BEFORE it, as the console empties its mailbox before it ticks. Up to and
including the start (``t`` <= 0) a tick comes before the commands of the same
instant: ``t`` = 0 is the start command itself, and a tick stamped 0 is the
last tick of the idle console. And ``shutdown`` always comes after the tick of
its instant: it is the console leaving, and nothing ticks after it.

**The clock.** It starts at the first step's instant and moves on by the
recorded intervals (:class:`~simulation.replay_tape.Pace`), not by jumping to rebuilt absolute
instants: a record made on a deterministic clock is replayed on the very same
floats, and its replay is exact rather than within a tick.

Everything here runs on one loop, inline, with no thread and no wall clock:
the same record gives the same report, byte for byte (EX-7).

A record this module cannot replay is refused with the reason
(:class:`ReplayError`), which is a different thing from a divergence: it says
the tool has nothing to judge, not that the runtime changed.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, assert_never, final

from simulation.harness import (
    DSP_OUTPUT_RATE,
    ECG_SAMPLE_RATE,
    PANEL_SAFETY,
    RUNTIME_LIMITS,
    load_motion,
)
from simulation.replay_compare import Decision, RuleKey, SafetyDecision, compare_decisions
from simulation.replay_report import Divergence, ReplayReport
from simulation.replay_tape import (
    INSTANT_EPSILON,
    NO_FURTHER,
    TICK,
    Delivery,
    Exchange,
    Pace,
    TapeDrive,
    TapeSource,
    deliveries,
    located,
    parse_frames,
)
from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import EcgBridge, EcgFrame, Treatment, load_treatment, treat_ecg
from src.geometry import MachineGeometry
from src.record import commands
from src.record.reader import Recording, read
from src.record.rows import Row
from src.record.schema import Manifest, Profile
from src.result import Err, Ok, Result
from src.sensors import ecg as typed_ecg
from src.sensors.base import SensorReading
from src.training.plan import Channel, Program, StoreRev, TrainingProfile
from src.training.runtime import Subject, TrainingRuntime
from src.training.safety import SafetyLimits
from src.training.types import Occupancy, Phase, SafetyAction
from src.units import (
    Bpm,
    GearRatio,
    Hertz,
    Metres,
    Millivolts,
    Monotonic,
    MotorRpm,
    Seconds,
    UnixMillis,
    elapsed,
)

NO_SIGNAL: Final[str] = "no_signal"
"""``hr_quality`` of a tick that had no heart-rate reading at all."""

REPLAYED: Final[str] = "replay"
"""Stands in for the free names a record never carries (profile name, passenger)."""

_MEMBER: Final = re.compile(r"[A-Za-z0-9_./-]{1,80}")


@dataclass(frozen=True, slots=True)
class ReplayError:
    """Why a record cannot be replayed at all. Not a finding about the runtime."""

    detail: str


# =========================================================================
# From the record to a plan
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Tick:
    t: Seconds
    """The recorded instant, to the millisecond: what steps are ordered and paced by."""

    at: float
    """The instant as written."""

    index: int


@dataclass(frozen=True, slots=True)
class _Arm:
    """``start_programme``, with the programme the manifest froze."""

    program: Program


type _Operation = (
    _Arm
    | commands.ConfirmEstopWiring
    | commands.StartManual
    | commands.SetManualTarget
    | commands.Stop
    | commands.EmergencyStop
    | commands.Acknowledge
    | commands.FaultReset
    | commands.Shutdown
    | commands.Attendant
)


@dataclass(frozen=True, slots=True)
class _Order:
    t: Seconds
    at: float
    actor: str
    operation: _Operation


type _Step = _Tick | _Order


@dataclass(frozen=True, slots=True)
class _Plan:
    expected: tuple[Decision, ...]
    steps: tuple[_Step, ...]
    exchanges: tuple[Exchange, ...]
    blocks: tuple[Delivery, ...]
    origin: Monotonic


def _position(step: _Step) -> tuple[float, int]:
    """Where a step stands in the replay (module docstring, "The order of things")."""
    if isinstance(step, _Tick):
        return (step.t, 2 if step.t > 0.0 else 0)
    return (step.t, 3 if isinstance(step.operation, commands.Shutdown) else 1)


def _decision(row: Row) -> Decision | None:
    """What one recorded tick says the runtime decided, or ``None`` if it cannot be read."""
    try:
        phase = Phase(row.phase)
        action = SafetyAction[row.safety_action]
    except (ValueError, KeyError):
        return None
    rule = row.safety_rule
    return Decision(
        t=Seconds(row.t),
        setpoint=MotorRpm(row.setpoint_motor_rpm),
        phase=phase,
        safety=SafetyDecision(action, None if rule is None else RuleKey(rule)),
    )


def _decisions(rows: Sequence[Row]) -> Result[tuple[Decision, ...], str]:
    if not rows:
        return Err("the record has no tick")
    out: list[Decision] = []
    previous: float | None = None
    for index, row in enumerate(rows):
        if previous is not None and row.t <= previous:
            return Err(f"tick {index} does not come after the one before it")
        previous = row.t
        decision = _decision(row)
        if decision is None:
            return Err(f"tick {index} holds a phase or a safety action this build does not know")
        out.append(decision)
    return Ok(tuple(out))


def _program(profile: Profile) -> Result[Program, str]:
    """The programme a ``start_programme`` arms: the manifest's frozen copy, re-validated."""
    try:
        resolved = TrainingProfile(
            profile_id=REPLAYED,
            name=REPLAYED,
            total_duration_s=Seconds(profile.total_duration_s),
            baseline_s=Seconds(profile.baseline_s),
            warmup_max_s=Seconds(profile.warmup_max_s),
            hold_min_s=Seconds(profile.hold_min_s),
            cooldown_s=Seconds(profile.cooldown_s),
            recovery_s=Seconds(profile.recovery_s),
            zone_low_bpm=Bpm(profile.zone_low_bpm),
            zone_high_bpm=Bpm(profile.zone_high_bpm),
            hard_max_bpm=Bpm(profile.hard_max_bpm),
            critical_bpm=Bpm(profile.critical_bpm),
            subject_hr_max=Bpm(profile.subject_hr_max),
            min_run_rpm=MotorRpm(profile.min_run_rpm),
            max_rpm=MotorRpm(profile.max_rpm),
            warmup_rpm_ceiling_fraction=profile.warmup_rpm_ceiling_fraction,
            channels=tuple(Channel(name) for name in profile.channels),
            allow_above_nameplate=profile.allow_above_nameplate,
        )
    except ValueError:
        return Err("the manifest's programme is not one this build accepts")
    return Ok(
        Program(
            profile=resolved,
            source_rev=StoreRev(profile.source_rev),
            resolved_at=UnixMillis(profile.resolved_at),
            total_overridden=profile.total_overridden,
        )
    )


def _orders(recording: Recording) -> Result[tuple[_Order, ...], str]:
    """The commands of ``events.jsonl``, each bound to what it needs to be issued again."""
    profile = recording.manifest.profile
    program: Program | None = None
    if profile is not None:
        built = _program(profile)
        if isinstance(built, Err):
            return built
        program = built.value
    orders: list[_Order] = []
    for event in recording.events:
        if event.kind not in commands.INPUT_KINDS:
            continue
        parsed = commands.parse(event.detail)
        if isinstance(parsed, Err):
            return Err(
                f"the {event.kind.value} event at t={event.t:.3f} s is not replayable: "
                f"{parsed.error}"
            )
        command = parsed.value
        operation: _Operation
        if isinstance(command, commands.StartProgramme):
            if program is None:
                return Err("a start_programme event, and no programme in the manifest")
            operation = _Arm(program)
        else:
            operation = command
        orders.append(_Order(Seconds(round(event.t, 3)), event.t, event.actor, operation))
    return Ok(tuple(orders))


def _plan(recording: Recording) -> Result[_Plan, str]:
    """Everything a replay needs from the record, or why the record is not replayable."""
    expected = _decisions(recording.rows)
    if isinstance(expected, Err):
        return expected
    orders = _orders(recording)
    if isinstance(orders, Err):
        return orders
    exchanges = parse_frames(recording.frames)
    if isinstance(exchanges, Err):
        return exchanges
    ticks = tuple(
        _Tick(Seconds(round(row.t, 3)), row.t, index) for index, row in enumerate(recording.rows)
    )
    steps: tuple[_Step, ...] = tuple(sorted((*ticks, *orders.value), key=_position))
    first = steps[0].t
    if any(exchange.t < first - TICK - INSTANT_EPSILON for exchange in exchanges.value):
        return Err(
            "drive exchanges were recorded before the first tick or command: the record "
            "does not hold the idle ticks that made them (t <= 0)"
        )
    if not recording.raw and any(
        row.hr_raw is not None or row.hr_quality != NO_SIGNAL for row in recording.rows
    ):
        return Err(
            "the ticks show a heart rate and the record holds no raw ECG: the runtime "
            "was given readings a replay cannot give back"
        )
    origin = Monotonic(recording.manifest.clocks.monotonic_start)
    return Ok(
        _Plan(
            expected=expected.value,
            steps=steps,
            exchanges=exchanges.value,
            blocks=deliveries(recording.raw, origin),
            origin=origin,
        )
    )


# =========================================================================
# The rig
# =========================================================================


class _NoWaveform:
    """The waveform goes nowhere: a replay judges decisions, it draws nothing."""

    __slots__ = ()

    def record_ecg(self, values: Sequence[Millivolts]) -> int:
        return len(values)


async def _treat_inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    """The DSP on the loop: nothing here has a control deadline, and no thread means no race."""
    return treat_ecg(treatment, batch)


async def _confirm_inline(
    window: Sequence[float], sample_rate: int, at: Monotonic
) -> SensorReading:
    """The independent ECG processor on the loop, for the same reason."""
    return typed_ecg.make().process(window, sample_rate, at)


def _runtime(
    manifest: Manifest, clock: ManualClock, drive: TapeDrive
) -> Result[TrainingRuntime, str]:
    """The runtime as the producer built it: the record's geometry, this checkout's limits."""
    geometry = manifest.geometry
    if geometry is None:
        return Err("the manifest holds no geometry: the runtime cannot be built as it was")
    profile = manifest.profile
    safety = (
        PANEL_SAFETY
        if profile is None
        else SafetyLimits(
            hard_max_bpm=Bpm(profile.hard_max_bpm), critical_bpm=Bpm(profile.critical_bpm)
        )
    )
    try:
        machine = MachineGeometry(
            radius=Metres(geometry.reference_radius_m),
            ratio=GearRatio(geometry.gear_ratio),
            nominal_rpm=MotorRpm(geometry.nominal_motor_rpm),
            base_hz=Hertz(geometry.base_hz),
        )
        return Ok(
            TrainingRuntime(
                clock=clock,
                drive=drive,
                geometry=machine,
                limits=RUNTIME_LIMITS,
                safety=safety,
                motion=load_motion(),
                limit_radius=Metres(max(geometry.leg_tip_radius_m, geometry.reference_radius_m)),
            )
        )
    except ValueError:
        return Err("this build refuses to build a runtime from the manifest's geometry and tiers")


@final
class _Session:
    """One replay, wired. Mutable, single-use, owned by one event loop."""

    __slots__ = (
        "_attendant",
        "_bridge",
        "_clock",
        "_occupancy",
        "_origin",
        "_runtime",
        "_subject",
    )

    def __init__(
        self, plan: _Plan, manifest: Manifest, runtime: TrainingRuntime, clock: ManualClock
    ) -> None:
        self._clock: ManualClock = clock
        self._origin: Monotonic = plan.origin
        self._runtime: TrainingRuntime = runtime
        self._occupancy: Occupancy = Occupancy(manifest.occupancy)
        self._subject: str = manifest.subject_id or REPLAYED
        self._attendant: bool = False
        self._bridge: EcgBridge = EcgBridge(
            clock=clock,
            source=TapeSource(plan.blocks, clock, plan.origin),
            treatment=load_treatment(ECG_SAMPLE_RATE, DSP_OUTPUT_RATE),
            heart_rate=runtime,
            waveform=_NoWaveform(),
            sample_rate=ECG_SAMPLE_RATE,
            treat=_treat_inline,
            confirm=_confirm_inline,
        )

    def now(self) -> Seconds:
        """The replay instant on the record's axis."""
        return elapsed(self._origin, self._clock.monotonic())

    async def tick(self, t: Seconds) -> Decision:
        """One tick, in the console's order: presence, ECG, then the runtime."""
        now = self._clock.monotonic()
        if self._attendant:
            self._runtime.note_presence(now)
        await self._bridge.pump()
        snapshot = await self._runtime.tick(now)
        safety = snapshot.safety
        return Decision(
            t=t,
            setpoint=snapshot.setpoint.motor_rpm,
            phase=snapshot.phase,
            safety=SafetyDecision(
                snapshot.safety_action, None if safety is None else RuleKey(safety.rule)
            ),
        )

    async def issue(self, order: _Order) -> None:
        """One recorded command, to the entry point it names. What comes back is not judged
        here: a refusal shows in the ticks that follow, which is where it is compared."""
        runtime = self._runtime
        actor = order.actor
        match order.operation:
            case commands.ConfirmEstopWiring():
                runtime.confirm_estop_wiring(actor)
            case _Arm(program):
                await runtime.start(program, Subject(subject_id=self._subject, operator=actor))
            case commands.StartManual(ceiling):
                await runtime.start_manual(self._occupancy, actor, ceiling)
            case commands.SetManualTarget(output_rpm):
                runtime.set_manual_target(output_rpm)
            case commands.Stop():
                runtime.request_stop(f"{actor}: stop")
            case commands.EmergencyStop():
                runtime.request_estop(f"{actor}: e-stop")
            case commands.Acknowledge(estop_released):
                runtime.acknowledge(actor, estop_released=estop_released)
            case commands.FaultReset():
                await runtime.fault_reset()
            case commands.Shutdown():
                await runtime.shutdown("replay: the recorded console exit")
            case commands.Attendant(present):
                self._attendant = present
            case _ as unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class ReplayedSession:
    """What the runtime decided, tick by tick, and where the replay had to stop, if it did."""

    decisions: tuple[Decision, ...]
    divergence: Divergence | None


async def _run(plan: _Plan, session: _Session, tape: TapeDrive, pace: Pace) -> ReplayedSession:
    """Walk the plan until its end or until the tape no longer holds."""
    actual: list[Decision] = []
    raised: str | None = None
    for step in plan.steps:
        pace.to(step.t)
        decision: Decision | None = None
        try:
            if isinstance(step, _Tick):
                decision = await session.tick(plan.expected[step.index].t)
            else:
                await session.issue(step)
        except Exception as error:  # the code under test failing on recorded inputs: a finding
            raised = type(error).__name__
            break
        if tape.mismatch is not None:
            # The step ran to its end on a tape that had stopped answering: what
            # it decided is not a decision about the recorded session, and
            # nothing after it is replayed.
            break
        if decision is not None:
            actual.append(decision)
    pending = tape.pending
    recorded = NO_FURTHER if pending is None else located(pending)
    mismatch = tape.mismatch
    divergence: Divergence | None = None
    # To the millisecond, like every instant of a record: the report is what an
    # accepted difference pins, and float dust has no place in it.
    now = Seconds(round(session.now(), 3))
    if raised is not None:
        divergence = Divergence(
            now, len(actual), f"nothing: the replayed code raised {raised}", recorded
        )
    elif mismatch is not None:
        divergence = Divergence(
            Seconds(round(mismatch.t, 3)), len(actual), mismatch.requested, mismatch.recorded
        )
    elif pending is not None:
        divergence = Divergence(now, len(actual), NO_FURTHER, recorded)
    return ReplayedSession(tuple(actual), divergence)


def integrity_line(file: str, code: str) -> str:
    """One reader warning. The member name comes from the record, so it is vetted."""
    return f"{file if _MEMBER.fullmatch(file) is not None else '<member>'}: {code}"


async def replay_recording(recording: Recording) -> Result[ReplayReport, ReplayError]:
    """Replay one record already read from disk. Must run on a loop of its own."""
    plan = _plan(recording)
    if isinstance(plan, Err):
        return Err(ReplayError(plan.error))
    # The first step's instant AS WRITTEN: the one place an absolute instant is
    # rebuilt, and why the producer does not round the stamp of its first command.
    clock = ManualClock(start=Monotonic(plan.value.origin + plan.value.steps[0].at))
    tape = TapeDrive(plan.value.exchanges, clock, plan.value.origin)
    runtime = _runtime(recording.manifest, clock, tape)
    if isinstance(runtime, Err):
        return Err(ReplayError(runtime.error))
    session = _Session(plan.value, recording.manifest, runtime.value, clock)
    replayed = await _run(plan.value, session, tape, Pace(clock, plan.value.steps[0].t))
    expected = plan.value.expected
    return Ok(
        ReplayReport(
            record=recording.manifest.record_id,
            recorded_ticks=len(expected),
            comparison=compare_decisions(expected[: len(replayed.decisions)], replayed.decisions),
            divergence=replayed.divergence,
            integrity=tuple(integrity_line(w.file, w.code) for w in recording.warnings),
        )
    )


def replay(path: Path) -> Result[ReplayReport, ReplayError]:
    """Read the record folder at ``path`` and replay it."""
    loaded = read(path)
    if isinstance(loaded, Err):
        return Err(ReplayError(f"the record cannot be read ({loaded.error.detail})"))
    return asyncio.run(replay_recording(loaded.value))
