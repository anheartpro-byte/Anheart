"""Applying presence decisions to the machine, through the runtime's EXISTING API only.

Nothing in ``src/training/`` knows this package exists. This module reads the
runtime (its state, its last snapshot, its manual session) to build the
machine's side of an evaluation, and acts through exactly two calls it already
offers:

* :meth:`~src.training.runtime.TrainingRuntime.request_estop` for an
  :class:`~src.presence.monitor.EmergencyStop` - synchronous: it latches the
  supervisor's e-stop and zeroes the speed reference before returning, with no
  tick in between. That is what an intrusion needs.
* :meth:`~src.training.runtime.TrainingRuntime.trip_from_thread` for every
  verdict, so the supervisor's floor carries the presence rule id and its
  French detail (the session log and the console show *why*), latched until
  the operator's acknowledgement. For a RAMP_DOWN this is the whole action:
  the supervisor drains it at the next tick (at most 200 ms) and the runtime
  ends the session on the controlled ramp.

Each verdict is applied once: the guard remembers the last verdict it applied
and does nothing while the same one stands, so a latched e-stop does not send
an emergency zero every 50 ms.

The start gate (:meth:`PresenceGuard.start_gate`) is a predicate the console
consults before arming; see ``README.md`` for the wiring.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from typing import Final, Protocol, assert_never, final

from src.presence.monitor import (
    Clear,
    EmergencyStop,
    PresenceAckRefusal,
    PresenceDecision,
    PresenceMonitor,
    RampDown,
    StartBlocked,
)
from src.presence.types import MachineContext, MotionState, PresenceSource
from src.result import Err, Ok, Result
from src.training.runtime import ManualSession, RuntimeState
from src.training.safety import (
    AcknowledgeRefusal,
    EmergencyStopStillLatched,
    GoSilentIsTerminal,
    NothingLatched,
    SafetyAcknowledgement,
    Unattributed,
)
from src.training.types import (
    Occupancy,
    SafetyAction,
    SafetyVerdict,
    TelemetrySnapshot,
)
from src.units import Monotonic, MotorRpm, Seconds

PRESENCE_PERIOD: Final[Seconds] = Seconds(0.05)
"""How often the console should step the guard: 20 Hz, at least the camera's rate.

NOT the 5 Hz control tick. A 200 ms poll would add up to 200 ms to every
intrusion stop and make the 100 ms confirmation window meaningless."""

ESTOP_SOURCE_PREFIX: Final[str] = "camera presence"
"""Prefix of the ``source`` handed to ``request_estop``, so the log says who pressed it."""

STANDSTILL_RPM: Final[MotorRpm] = MotorRpm(1)
"""Below this the shaft counts as stopped: the runtime's own standstill threshold."""


class RuntimePort(Protocol):
    """The part of :class:`~src.training.runtime.TrainingRuntime` this module touches.

    A protocol so the dependency is written down and a test can see it; the
    real runtime satisfies it structurally.
    """

    @property
    def state(self) -> RuntimeState: ...

    @property
    def output_enabled(self) -> bool: ...

    @property
    def manual(self) -> ManualSession | None: ...

    def snapshot(self) -> TelemetrySnapshot: ...

    def request_estop(self, source: str) -> SafetyVerdict: ...

    def trip_from_thread(self, rule: str, action: SafetyAction, detail: str = "") -> None: ...

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]: ...


def motion_state(runtime: RuntimePort, standstill: MotorRpm = STANDSTILL_RPM) -> MotionState:
    """What the machine is doing, from the MEASURED speed. Unknown is never "stopped".

    A stale drive observation is UNKNOWN whatever the runtime believes; a
    measured non-zero speed is TURNING even with no session (a coast-down); a
    stopped shaft with an armed session or an enabled output stage is ARMED,
    because it can move at the next click.
    """
    snapshot = runtime.snapshot()
    if snapshot.drive_status_is_stale:
        return MotionState.UNKNOWN
    if abs(snapshot.measured.motor_rpm) >= standstill:
        return MotionState.TURNING
    session = runtime.state in (RuntimeState.RUNNING, RuntimeState.ENDING)
    if session or runtime.output_enabled:
        return MotionState.ARMED
    return MotionState.AT_REST


def session_occupancy(runtime: RuntimePort) -> Occupancy | None:
    """The declared occupancy: the manual session's, OCCUPIED for a programme, else ``None``."""
    manual = runtime.manual
    if manual is not None:
        return manual.occupancy
    if runtime.state in (RuntimeState.RUNNING, RuntimeState.ENDING):
        return Occupancy.OCCUPIED
    return None


@final
class PresenceGuard:
    """Reads the camera, judges it, and applies the decision to the runtime.

    Mutable in one field only: the last verdict applied, so each is applied once.
    """

    __slots__ = ("_applied", "_last", "_monitor", "_runtime", "_source")

    def __init__(
        self, *, runtime: RuntimePort, source: PresenceSource, monitor: PresenceMonitor
    ) -> None:
        self._runtime: RuntimePort = runtime
        self._source: PresenceSource = source
        self._monitor: PresenceMonitor = monitor
        self._applied: SafetyVerdict | None = None
        self._last: PresenceDecision | None = None

    @property
    def monitor(self) -> PresenceMonitor:
        return self._monitor

    @property
    def last_decision(self) -> PresenceDecision | None:
        """What the last :meth:`step` decided; ``None`` before the first step."""
        return self._last

    @property
    def source(self) -> PresenceSource:
        """Where the observations come from (a simulated camera, or a detector link)."""
        return self._source

    def context(self) -> MachineContext:
        """The machine's side of an evaluation, read from the runtime now."""
        runtime = self._runtime
        return MachineContext(motion=motion_state(runtime), occupancy=session_occupancy(runtime))

    def step(self, now: Monotonic) -> PresenceDecision:
        """Read the camera, judge, apply. Synchronous, never awaits; call at PRESENCE_PERIOD."""
        decision = self._monitor.evaluate(now, self._source.latest(), self.context())
        self._last = decision
        match decision:
            case Clear() | StartBlocked():
                pass
            case RampDown(verdict=verdict):
                if verdict != self._applied:
                    self._applied = verdict
                    self._runtime.trip_from_thread(verdict.rule, verdict.action, verdict.detail)
            case EmergencyStop(verdict=verdict):
                if verdict != self._applied:
                    self._applied = verdict
                    # The stop first: request_estop zeroes the reference before
                    # it returns. The trip only records the rule id.
                    self._runtime.request_estop(f"{ESTOP_SOURCE_PREFIX}: {verdict.detail}")
                    self._runtime.trip_from_thread(verdict.rule, verdict.action, verdict.detail)
            case _ as unreachable:
                assert_never(unreachable)
        return decision

    def start_gate(self, now: Monotonic, occupancy: Occupancy) -> Result[None, StartBlocked]:
        """Whether a start declared ``occupancy`` may proceed. Steps first, so it is current."""
        self.step(now)
        return self._monitor.check_start(now, occupancy)

    def acknowledge(self, operator: str) -> Result[SafetyAcknowledgement, PresenceAckRefusal]:
        """Clear the presence latch, by name. See :class:`PresenceAcknowledger` for the console."""
        outcome = self._monitor.acknowledge(operator)
        if isinstance(outcome, Ok):
            self._applied = None
        return outcome


@final
class PresenceAcknowledger:
    """The console's acknowledger: the runtime's latches AND the presence latch, together.

    Satisfies :class:`~src.control_surface.Acknowledger`. The presence latch is
    cleared **only** when the runtime accepted the acknowledgement (or had
    nothing latched): clearing it on a refusal - the mushroom still pressed,
    GO_SILENT - would let the start gate open while the supervisor still holds
    the machine.
    """

    __slots__ = ("_guard", "_runtime")

    def __init__(self, runtime: RuntimePort, guard: PresenceGuard) -> None:
        self._runtime: RuntimePort = runtime
        self._guard: PresenceGuard = guard

    def acknowledge(
        self, operator: str, *, estop_released: bool = False
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        outcome = self._runtime.acknowledge(operator, estop_released=estop_released)
        match outcome:
            case Ok(record):
                presence = self._guard.acknowledge(operator)
                if isinstance(presence, Ok):
                    return Ok(_merge(record, presence.value))
                return outcome
            case Err(error):
                return self._after_refusal(operator, error)
            case _ as unreachable:
                assert_never(unreachable)

    def _after_refusal(
        self, operator: str, error: AcknowledgeRefusal
    ) -> Result[SafetyAcknowledgement, AcknowledgeRefusal]:
        """Only "nothing latched in the runtime" lets the presence latch clear alone."""
        match error:
            case NothingLatched():
                presence = self._guard.acknowledge(operator)
                if isinstance(presence, Ok):
                    return Ok(presence.value)
                return Err(error)
            case Unattributed() | GoSilentIsTerminal() | EmergencyStopStillLatched():
                return Err(error)
            case _ as unreachable:
                assert_never(unreachable)


def _merge(record: SafetyAcknowledgement, presence: SafetyAcknowledgement) -> SafetyAcknowledgement:
    return SafetyAcknowledgement(
        operator=record.operator,
        at=record.at,
        wall_clock=record.wall_clock,
        cleared=(*record.cleared, *presence.cleared),
    )
