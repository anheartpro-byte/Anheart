"""The presence rules: camera observations plus machine state in, latched decisions out.

Pure and clock-injected, like :class:`~src.training.safety.SafetySupervisor`.
:meth:`PresenceMonitor.evaluate` judges the instant it is handed; the injected
clock is read only by :meth:`PresenceMonitor.acknowledge`, which has no tick to
borrow an instant from. Nothing here touches the drive or the camera: the
adapter (:mod:`src.presence.adapter`) reads the source and applies decisions.

THE TWO KINDS OF ANSWER
-----------------------
* While motion is possible (:attr:`~src.presence.types.MotionState.motion_possible`:
  turning, armed, or unknown) a rule that fires produces a **verdict** - a
  :class:`~src.training.types.SafetyVerdict` of ``QUICK_STOP`` or
  ``RAMP_DOWN`` - and **every verdict latches**. The latch is a high-water
  mark exactly like the supervisor's floor: more severe replaces less severe,
  an equal one keeps the first, and only :meth:`PresenceMonitor.acknowledge`,
  by a named operator, lowers it. There is no automatic resumption: a person
  who steps back out of the zone does not restart anything.
* At rest nothing moves, so nothing latches: the rules become a **start gate**
  (:meth:`PresenceMonitor.check_start`) that refuses while the evidence says a
  start would be unsafe, and says why in French. A latched verdict still
  standing at rest is itself a refusal (``presence_latched``).

Not latching at rest is deliberate. People are *supposed* to be next to the arm
at rest - the rider is helped in, the harness is checked - so a latch there
would demand an acknowledgement at every boarding, and an operator who clicks
"acknowledge" twenty times a day clicks it through the one that mattered.

EVIDENCE, AND THE FAIL-SAFE ASYMMETRY
-------------------------------------
* Only a frame whose ``frame_seq`` advanced is evidence (the frozen camera).
* A detection counts in any frame, degraded or not. An ABSENCE of detection
  counts only in a HEALTHY frame. So a degraded camera can stop the machine
  but can never clear the zone.
* Every episode (person in the zone, capsule empty, ...) is released only after
  :attr:`PresenceLimits.release_after` of contrary healthy evidence, so a
  detector flickering between "person" and "nobody" on alternate frames keeps
  the episode alive rather than restarting its dwell on every frame - the
  dangerous direction for a debounced rule.
* Dwell is measured from the CAPTURE time of the first frame of the episode,
  clamped to ``now`` (a detector stamping frames in the future must not make
  evidence look younger than the moment it is judged at).

See ``src/presence/README.md`` and .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, assert_never, final

from src.clock import Clock
from src.presence.types import (
    CameraHealth,
    CapsuleState,
    MachineContext,
    MotionState,
    PersonInZone,
    PresenceObservation,
    ZoneClear,
    ZoneView,
)
from src.result import Err, Ok, Result
from src.training.safety import NothingLatched, SafetyAcknowledgement, Unattributed
from src.training.types import Occupancy, SafetyAction, SafetyVerdict
from src.units import Metres, Monotonic, Seconds, elapsed

_logger: Final[logging.Logger] = logging.getLogger(__name__)


# =========================================================================
# Rule ids
# =========================================================================
#
# Stored data, like the supervisor's: a dashboard and the session log key on
# these strings, so renaming one is a breaking change. All are prefixed
# ``presence_`` so none can collide with a supervisor rule.

RULE_PRESENCE_INTRUSION: Final[str] = "presence_intrusion"
RULE_PRESENCE_BENCH_OCCUPIED: Final[str] = "presence_bench_occupied"
RULE_PRESENCE_CAMERA_LOST: Final[str] = "presence_camera_lost"
RULE_PRESENCE_ZONE_UNCERTAIN: Final[str] = "presence_zone_uncertain"
RULE_PRESENCE_RIDER_ABSENT: Final[str] = "presence_rider_absent"
RULE_PRESENCE_CAPSULE_UNKNOWN: Final[str] = "presence_capsule_unknown"
RULE_PRESENCE_RIDER_UNBUCKLED: Final[str] = "presence_rider_unbuckled"
RULE_PRESENCE_LIMB_OUTSIDE: Final[str] = "presence_limb_outside"
RULE_PRESENCE_LATCHED: Final[str] = "presence_latched"
RULE_PRESENCE_ZONE_NOT_CLEAR: Final[str] = "presence_zone_not_clear"

MOTION_RULES: Final[tuple[str, ...]] = (
    RULE_PRESENCE_INTRUSION,
    RULE_PRESENCE_BENCH_OCCUPIED,
    RULE_PRESENCE_CAMERA_LOST,
    RULE_PRESENCE_ZONE_UNCERTAIN,
    RULE_PRESENCE_RIDER_ABSENT,
    RULE_PRESENCE_CAPSULE_UNKNOWN,
    RULE_PRESENCE_RIDER_UNBUCKLED,
    RULE_PRESENCE_LIMB_OUTSIDE,
)
"""Every rule that can produce a latched verdict while motion is possible."""

START_RULES: Final[tuple[str, ...]] = (
    RULE_PRESENCE_LATCHED,
    RULE_PRESENCE_CAMERA_LOST,
    RULE_PRESENCE_ZONE_NOT_CLEAR,
    RULE_PRESENCE_RIDER_ABSENT,
    RULE_PRESENCE_CAPSULE_UNKNOWN,
    RULE_PRESENCE_RIDER_UNBUCKLED,
    RULE_PRESENCE_LIMB_OUTSIDE,
    RULE_PRESENCE_BENCH_OCCUPIED,
)
"""Every rule the start gate can refuse under."""

_MOTION_LABELS: Final[Mapping[MotionState, str]] = MappingProxyType(
    {
        MotionState.AT_REST: "est a l'arret",
        MotionState.ARMED: "est armee",
        MotionState.TURNING: "tourne",
        MotionState.UNKNOWN: "est dans un etat inconnu (vitesse non mesuree)",
    }
)
"""How each motion state reads in an operator sentence (``la machine ...``)."""


# =========================================================================
# Limits
# =========================================================================


def _require_increasing(why: str, *named: tuple[str, float]) -> None:
    """Each value must be strictly greater than the one before it."""
    for index in range(1, len(named)):
        lower_name, lower = named[index - 1]
        upper_name, upper = named[index]
        if upper <= lower:
            raise ValueError(
                f"{upper_name} ({upper}) must be greater than {lower_name} ({lower}): {why}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class PresenceLimits:
    """Every threshold the presence rules compare against. Frozen and validated.

    Raises ``ValueError`` on an incoherent set, at startup with nothing
    spinning, for the reason :class:`~src.training.safety.SafetyLimits` gives.
    Every default is argued next to its field; ``src/presence/README.md``
    tabulates them.
    """

    confirm_window: Seconds = Seconds(0.10)
    """How long a medium-confidence intrusion must persist before the e-stop.

    100 ms is two frames at 20 fps and at least one full frame interval at the
    15 fps minimum (``README.md``), so a single-frame false positive - a
    reflection, a shadow crossing the floor - cannot stop the machine, while a
    real person, who does not vanish in 67 ms, is confirmed at once. Against
    the stop itself it costs almost nothing: the drive decelerates on a 3-4 s
    commissioned ramp, so 100 ms is under 3% of the stopping time, and a person
    walking at 1.5 m/s covers 15 cm in it. Anything the detector is sure of
    (:attr:`immediate_confidence`) or that is already close
    (:attr:`immediate_distance`) skips the window entirely."""

    raise_confidence: float = 0.50
    """A detection at or above this starts an intrusion episode."""

    release_confidence: float = 0.30
    """Hysteresis: an episode is kept alive by anything at or above this.

    Also the floor of the ``presence_zone_uncertain`` rule: a detection between
    this and :attr:`raise_confidence` is "something may be there", and a
    turning machine is not allowed to ignore that for long."""

    immediate_confidence: float = 0.80
    """At or above this, one frame is enough: no confirmation window."""

    immediate_distance: Metres = Metres(0.5)
    """A person this close to the swept envelope skips the window (at raise confidence)."""

    release_after: Seconds = Seconds(0.20)
    """Contrary healthy evidence needed before an episode ends: 3-4 frames at 15-20 fps.

    What defeats detector flicker: a person reported on alternate frames is a
    person, and must not restart a dwell every other frame."""

    uncertain_dwell: Seconds = Seconds(1.0)
    """How long a low-confidence detection may persist while turning before RAMP_DOWN."""

    stale_after: Seconds = Seconds(0.5)
    """No fresh HEALTHY frame for this long: the camera is lost.

    Half a second is 7 frames at 15 fps and more than three times the 150 ms
    capture-to-observation latency budget, so it is a camera that has stopped,
    not jitter. While motion is possible that is a RAMP_DOWN: a machine that
    cannot see its danger zone must not keep turning, but a camera fault is not
    an emergency, so the stop is the controlled one."""

    clear_before_start: Seconds = Seconds(2.0)
    """The zone must have been seen clear, on healthy frames, this long before a start.

    Hysteresis for the start gate: a person walking round the machine is
    briefly occluded by the arm, and a start granted in that gap is a start
    with somebody in the zone."""

    capsule_empty_dwell: Seconds = Seconds(1.0)
    """OCCUPIED session, capsule seen empty this long: the rider left. RAMP_DOWN.

    One second tolerates a rider leaning out of the camera's view of the seat,
    but not a rider who has left it."""

    capsule_unknown_dwell: Seconds = Seconds(3.0)
    """OCCUPIED session, capsule unclassifiable this long: RAMP_DOWN.

    Longer than the empty dwell because UNKNOWN is the absence of an answer
    rather than a wrong one - the arm may hide the capsule for part of a turn -
    but a rider the camera cannot see for three seconds is not supervised."""

    bench_occupied_dwell: Seconds = Seconds(0.2)
    """BENCH session, capsule seen occupied this long: QUICK_STOP.

    Short, because a person in a machine declared empty is unharnessed and
    unexpected; 200 ms (three frames at 15 fps) is only there to reject a
    single misclassified frame."""

    unbuckled_dwell: Seconds = Seconds(0.5)
    """OCCUPIED session, harness seen unfastened this long: RAMP_DOWN."""

    limb_outside_dwell: Seconds = Seconds(0.3)
    """OCCUPIED session, a limb seen outside the capsule this long: RAMP_DOWN.

    RAMP_DOWN rather than QUICK_STOP: the stop is gentle on the rider, and a
    limb outside the capsule is a person who is conscious and moving."""

    def __post_init__(self) -> None:
        """Refuse an incoherent limit set. Startup only, with nothing spinning."""
        _require_increasing(
            "the thresholds must go release < raise < immediate, inside (0, 1]",
            ("zero", 0.0),
            ("release_confidence", self.release_confidence),
            ("raise_confidence", self.raise_confidence),
            ("immediate_confidence", self.immediate_confidence),
        )
        if self.immediate_confidence > 1.0:
            raise ValueError(
                f"immediate_confidence must be at most 1, got {self.immediate_confidence}: "
                "a threshold no detector can reach disables the immediate stop"
            )
        for name, value in (
            ("confirm_window", self.confirm_window),
            ("release_after", self.release_after),
            ("uncertain_dwell", self.uncertain_dwell),
            ("stale_after", self.stale_after),
            ("clear_before_start", self.clear_before_start),
            ("capsule_empty_dwell", self.capsule_empty_dwell),
            ("capsule_unknown_dwell", self.capsule_unknown_dwell),
            ("bench_occupied_dwell", self.bench_occupied_dwell),
            ("unbuckled_dwell", self.unbuckled_dwell),
            ("limb_outside_dwell", self.limb_outside_dwell),
        ):
            if value <= 0.0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.immediate_distance < 0.0:
            raise ValueError(
                f"immediate_distance cannot be negative, got {self.immediate_distance}"
            )


DEFAULT_PRESENCE_LIMITS: Final[PresenceLimits] = PresenceLimits()

DWELL_TOLERANCE: Final[Seconds] = Seconds(1e-6)
"""Slack on every dwell comparison, one microsecond.

Frame timestamps are sums of binary floats, so two frames exactly 0.1 s apart
at 20 fps can differ by 0.0999999...: without slack a 100 ms window would need a
third frame. The slack only ever makes a rule fire EARLIER, by a microsecond -
the fail-safe direction - and is applied to nothing that opens a gate or ends
an episode."""


# =========================================================================
# Decisions
# =========================================================================


@dataclass(frozen=True, slots=True)
class PresenceRefusal:
    """One reason a start is refused: a rule id and one French sentence."""

    rule: str
    detail: str


@dataclass(frozen=True, slots=True)
class Clear:
    """Nothing to do: no presence rule objects, and nothing is latched."""


@dataclass(frozen=True, slots=True)
class StartBlocked:
    """At rest: a start would be refused now, for these reasons (never empty)."""

    refusals: tuple[PresenceRefusal, ...]

    @property
    def detail(self) -> str:
        """The refusals as one operator sentence, in the order they were judged."""
        return " ; ".join(refusal.detail for refusal in self.refusals)


@dataclass(frozen=True, slots=True)
class RampDown:
    """Motion possible: end the session on the controlled ramp. Latched."""

    verdict: SafetyVerdict


@dataclass(frozen=True, slots=True)
class EmergencyStop:
    """Motion possible: stop now (zero the reference, the drive's own ramp). Latched."""

    verdict: SafetyVerdict


type PresenceDecision = Clear | StartBlocked | RampDown | EmergencyStop
"""Closed: match it ending in ``assert_never``, so a new decision fails the build."""

type PresenceAckRefusal = Unattributed | NothingLatched
"""Every way :meth:`PresenceMonitor.acknowledge` refuses. Closed."""


# =========================================================================
# Episodes: dwell with time hysteresis
# =========================================================================


@final
class _Episode:
    """One condition's episode: since when it has held, and whether it is ending.

    **Mutable state**, the only kind in this module besides the monitor's own
    latch and freshness record: a dwell is not a property of any one frame.

    ``observe`` takes a tri-state. ``True`` is evidence FOR the condition,
    ``False`` evidence AGAINST it, ``None`` no evidence either way (a degraded
    frame that saw nothing, a detection in the hysteresis band of an episode
    that has not started). Against-evidence ends the episode only once it has
    lasted :attr:`PresenceLimits.release_after`; any for-evidence in between
    cancels the ending.
    """

    __slots__ = ("_clear_since", "_held_since", "_immediate")

    def __init__(self) -> None:
        self._held_since: Monotonic | None = None
        self._clear_since: Monotonic | None = None
        self._immediate: bool = False

    @property
    def held(self) -> bool:
        return self._held_since is not None

    def observe(
        self, evidence: bool | None, at: Monotonic, release_after: Seconds, *, immediate: bool
    ) -> None:
        """Fold one frame of evidence in, stamped with its (clamped) capture time."""
        if evidence is None:
            return
        if evidence:
            if self._held_since is None:
                self._held_since = at
                self._immediate = False
            elif at < self._held_since:
                # A newer frame captured EARLIER (capture stamps are the
                # detector's, and need not be monotonic across its pipeline):
                # the condition held from the earliest capture that showed it.
                # Moving the start back only ever fires sooner.
                self._held_since = at
            self._clear_since = None
            self._immediate = self._immediate or immediate
            return
        if self._held_since is None:
            return
        if self._clear_since is None:
            self._clear_since = at
        if elapsed(self._clear_since, at) >= release_after:
            self._held_since = None
            self._clear_since = None
            self._immediate = False

    def held_for(self, now: Monotonic) -> Seconds | None:
        """How long the episode has held at ``now``, or ``None`` when it is not held."""
        if self._held_since is None:
            return None
        return elapsed(self._held_since, now)

    def firing(self, now: Monotonic, dwell: Seconds) -> bool:
        """Held, not currently contradicted, and immediate or held for ``dwell``.

        "Not currently contradicted" is what keeps the confirmation window
        meaningful. The release hysteresis keeps an episode alive for
        :attr:`PresenceLimits.release_after` after the evidence turns, so
        without this clause one false-positive frame followed by clear frames
        would still fire once the window elapsed - every single-frame glitch
        would stop the machine. With it, the glitch never fires, and a person
        the detector keeps losing and re-finding fires the moment they are
        re-found, with the dwell they had already accumulated.
        """
        held = self.held_for(now)
        if held is None or self._clear_since is not None:
            return False
        return self._immediate or held >= dwell - DWELL_TOLERANCE


# =========================================================================
# The monitor
# =========================================================================


def _level(zone: ZoneView) -> float:
    """The zone as one number: the detection's confidence, or 0 when clear."""
    match zone:
        case ZoneClear():
            return 0.0
        case PersonInZone(confidence=confidence):
            return confidence
    raise assert_never(zone)


def _describe_person(person: PersonInZone | None) -> str:
    """The last detection, in operator French."""
    if person is None:
        return "personne detectee"
    distance = person.distance
    where = "" if distance is None else f", a {distance:.1f} m du bras"
    return f"personne detectee (confiance {person.confidence:.2f}{where})"


@final
class PresenceMonitor:
    """The presence rules, with their latch. One per console process.

    Mutable state, all of it listed: one :class:`_Episode` per condition, the
    freshness record of the camera (last sequence number, last healthy capture,
    whether the detector reported FAILED, since when the zone has been clear),
    the last healthy observation (for the start gate), the latched floor, and
    the verdicts that fired at the last evaluation.

    Single-threaded, on the event loop, like the supervisor: nothing here
    awaits, so no two calls interleave.
    """

    __slots__ = (
        "_bench_occupied",
        "_camera_failed",
        "_capsule_empty",
        "_capsule_unknown",
        "_clock",
        "_floor",
        "_intrusion",
        "_last_good_at",
        "_last_healthy",
        "_last_person",
        "_last_seq",
        "_limb_outside",
        "_limits",
        "_live",
        "_origin",
        "_unbuckled",
        "_uncertain",
        "_zone_clear_since",
    )

    def __init__(self, *, clock: Clock, limits: PresenceLimits = DEFAULT_PRESENCE_LIMITS) -> None:
        """``clock`` is read here once (the origin) and by :meth:`acknowledge` only.

        The origin matters for a camera that never delivers a frame: its
        staleness is measured from the moment this monitor started, so a
        camera that was never there escalates exactly like one that died.
        """
        self._clock: Clock = clock
        self._limits: PresenceLimits = limits
        self._origin: Monotonic = clock.monotonic()
        self._intrusion: _Episode = _Episode()
        self._uncertain: _Episode = _Episode()
        self._capsule_empty: _Episode = _Episode()
        self._capsule_unknown: _Episode = _Episode()
        self._bench_occupied: _Episode = _Episode()
        self._unbuckled: _Episode = _Episode()
        self._limb_outside: _Episode = _Episode()
        self._last_seq: int | None = None
        self._last_good_at: Monotonic | None = None
        self._camera_failed: bool = False
        self._zone_clear_since: Monotonic | None = None
        self._last_healthy: PresenceObservation | None = None
        self._last_person: PersonInZone | None = None
        self._floor: SafetyVerdict | None = None
        self._live: tuple[SafetyVerdict, ...] = ()

    # --- reads -----------------------------------------------------------

    @property
    def limits(self) -> PresenceLimits:
        return self._limits

    @property
    def latched(self) -> SafetyVerdict | None:
        """The latched presence verdict, or ``None``. Lowered only by :meth:`acknowledge`."""
        return self._floor

    @property
    def live(self) -> tuple[SafetyVerdict, ...]:
        """Every presence rule that fired at the last evaluation with motion possible."""
        return self._live

    def camera_ok(self, now: Monotonic) -> bool:
        """Whether a fresh, healthy frame is on hand at ``now``."""
        return not self._camera_lost(now)

    # --- the tick --------------------------------------------------------

    def evaluate(
        self, now: Monotonic, observation: PresenceObservation | None, context: MachineContext
    ) -> PresenceDecision:
        """Judge one instant. Never raises.

        1. ingest the observation if - and only if - it is a new frame;
        2. with motion possible, fire every rule, latch every verdict, and
           answer the latched floor as a stop;
        3. at rest, answer the start gate for the context's occupancy.
        """
        self._ingest(now, observation)
        if not context.motion.motion_possible:
            self._live = ()
            refusals = self._start_refusals(now, context.occupancy)
            return StartBlocked(refusals) if refusals else Clear()
        self._live = self._fire(now, context)
        for verdict in self._live:
            self._raise_floor(verdict)
        return self._as_decision(self._floor)

    def check_start(self, now: Monotonic, occupancy: Occupancy) -> Result[None, StartBlocked]:
        """The start gate for a session about to be declared ``occupancy``. Pure."""
        refusals = self._start_refusals(now, occupancy)
        if refusals:
            return Err(StartBlocked(refusals))
        return Ok(None)

    def acknowledge(self, operator: str) -> Result[SafetyAcknowledgement, PresenceAckRefusal]:
        """Clear the latched presence verdict, by name. The only thing that can.

        Allowed while the condition is still true, as in the supervisor: the
        rule re-fires at the next evaluation with motion possible, and at rest
        the start gate keeps refusing on the live evidence. What an
        acknowledgement never does is restart anything.
        """
        if not operator.strip():
            return Err(
                Unattributed(
                    "an acknowledgement must name the operator making it: "
                    "an unattributable safety record is not a safety record"
                )
            )
        floor = self._floor
        if floor is None:
            return Err(NothingLatched())
        self._floor = None
        record = SafetyAcknowledgement(
            operator=operator,
            at=self._clock.monotonic(),
            wall_clock=self._clock.unix_millis(),
            cleared=(floor.rule,),
        )
        _logger.warning("presence latch %s acknowledged by %r", floor.rule, operator)
        return Ok(record)

    # --- evidence --------------------------------------------------------

    def _ingest(self, now: Monotonic, observation: PresenceObservation | None) -> None:
        """Fold a new frame into every episode. A repeated frame is no evidence at all."""
        if observation is None or not observation.is_new_evidence_after(self._last_seq):
            return
        self._last_seq = observation.frame_seq
        at = Monotonic(min(observation.at, now))
        healthy = observation.health is CameraHealth.HEALTHY
        self._camera_failed = observation.health is CameraHealth.FAILED
        if healthy:
            self._last_good_at = at
            self._last_healthy = observation
        zone = observation.zone
        if isinstance(zone, PersonInZone):
            self._last_person = zone
        level = _level(zone)
        self._observe_zone(at, level, zone, healthy=healthy)
        if healthy:
            self._observe_capsule(at, observation)

    def _observe_zone(self, at: Monotonic, level: float, zone: ZoneView, *, healthy: bool) -> None:
        """The two zone episodes and the clear-since record, with the asymmetry."""
        limits = self._limits
        release = limits.release_after
        detected = level >= limits.release_confidence
        against: bool | None = False if healthy else None
        threshold = limits.release_confidence if self._intrusion.held else limits.raise_confidence
        if level >= threshold:
            intrusion: bool | None = True
        elif detected:
            intrusion = None
        else:
            intrusion = against
        close = (
            isinstance(zone, PersonInZone)
            and zone.distance is not None
            and zone.distance <= limits.immediate_distance
            and level >= limits.raise_confidence
        )
        immediate = level >= limits.immediate_confidence or close
        self._intrusion.observe(intrusion, at, release, immediate=immediate)
        self._uncertain.observe(True if detected else against, at, release, immediate=False)
        if detected or not healthy:
            self._zone_clear_since = None
        elif self._zone_clear_since is None:
            self._zone_clear_since = at

    def _observe_capsule(self, at: Monotonic, observation: PresenceObservation) -> None:
        """Capsule and posture episodes. Healthy frames only; UNKNOWN is no evidence of either."""
        release = self._limits.release_after
        capsule = observation.capsule
        empty: bool | None
        occupied: bool | None
        match capsule:
            case CapsuleState.EMPTY:
                empty, occupied = True, False
            case CapsuleState.OCCUPIED:
                empty, occupied = False, True
            case CapsuleState.UNKNOWN:
                empty, occupied = None, None
            case _ as unreachable:
                assert_never(unreachable)
        self._capsule_empty.observe(empty, at, release, immediate=False)
        self._bench_occupied.observe(occupied, at, release, immediate=False)
        self._capsule_unknown.observe(capsule is CapsuleState.UNKNOWN, at, release, immediate=False)
        posture = observation.posture
        if posture is not None:
            self._unbuckled.observe(posture.unbuckled, at, release, immediate=False)
            self._limb_outside.observe(posture.limb_outside, at, release, immediate=False)

    def _camera_age(self, now: Monotonic) -> Seconds:
        """Age of the last healthy frame, measured from the origin if there never was one."""
        since = self._origin if self._last_good_at is None else self._last_good_at
        return elapsed(since, now)

    def _camera_lost(self, now: Monotonic) -> bool:
        return self._camera_failed or self._camera_age(now) > self._limits.stale_after

    # --- rules with motion possible --------------------------------------

    def _fire(self, now: Monotonic, context: MachineContext) -> tuple[SafetyVerdict, ...]:
        """Every presence rule that fires now. Each is independent of the others."""
        limits = self._limits
        motion = _MOTION_LABELS[context.motion]
        occupancy = context.occupancy
        occupied = occupancy is Occupancy.OCCUPIED
        candidates: list[SafetyVerdict] = []

        def fire(rule: str, action: SafetyAction, detail: str) -> None:
            candidates.append(
                SafetyVerdict(action=action, rule=rule, detail=detail, latched=True, since=now)
            )

        if self._intrusion.firing(now, limits.confirm_window):
            fire(
                RULE_PRESENCE_INTRUSION,
                SafetyAction.QUICK_STOP,
                f"{_describe_person(self._last_person)} dans la zone du bras alors que la "
                f"machine {motion} : arret d'urgence",
            )
        if occupancy is Occupancy.BENCH and self._bench_occupied.firing(
            now, limits.bench_occupied_dwell
        ):
            fire(
                RULE_PRESENCE_BENCH_OCCUPIED,
                SafetyAction.QUICK_STOP,
                "une personne est vue dans la capsule alors que la seance est declaree "
                f"BANC (personne a bord : NON) et que la machine {motion} : arret d'urgence",
            )
        if self._camera_lost(now):
            fire(
                RULE_PRESENCE_CAMERA_LOST,
                SafetyAction.RAMP_DOWN,
                f"{self._describe_camera(now)} alors que la machine {motion} : "
                "arret controle, la zone du bras n'est plus surveillee",
            )
        if self._uncertain.firing(now, limits.uncertain_dwell):
            fire(
                RULE_PRESENCE_ZONE_UNCERTAIN,
                SafetyAction.RAMP_DOWN,
                f"detection incertaine dans la zone du bras depuis plus de "
                f"{limits.uncertain_dwell:.1f} s ({_describe_person(self._last_person)}) "
                f"alors que la machine {motion} : arret controle",
            )
        if occupied and self._capsule_empty.firing(now, limits.capsule_empty_dwell):
            fire(
                RULE_PRESENCE_RIDER_ABSENT,
                SafetyAction.RAMP_DOWN,
                "la capsule est vue vide alors qu'une PERSONNE A BORD est declaree : "
                "arret controle",
            )
        if occupied and self._capsule_unknown.firing(now, limits.capsule_unknown_dwell):
            fire(
                RULE_PRESENCE_CAPSULE_UNKNOWN,
                SafetyAction.RAMP_DOWN,
                f"la capsule n'est plus visible par la camera depuis plus de "
                f"{limits.capsule_unknown_dwell:.0f} s avec une PERSONNE A BORD : arret controle",
            )
        if occupied and self._unbuckled.firing(now, limits.unbuckled_dwell):
            fire(
                RULE_PRESENCE_RIDER_UNBUCKLED,
                SafetyAction.RAMP_DOWN,
                "le harnais du passager est vu detache : arret controle",
            )
        if occupied and self._limb_outside.firing(now, limits.limb_outside_dwell):
            fire(
                RULE_PRESENCE_LIMB_OUTSIDE,
                SafetyAction.RAMP_DOWN,
                "un membre du passager est vu hors de la capsule : arret controle",
            )
        return tuple(candidates)

    def _describe_camera(self, now: Monotonic) -> str:
        if self._camera_failed:
            return "la camera signale qu'elle ne voit plus"
        if self._last_good_at is None:
            return f"aucune image exploitable de la camera depuis {self._camera_age(now):.1f} s"
        return f"derniere image exploitable de la camera il y a {self._camera_age(now):.1f} s"

    def _raise_floor(self, verdict: SafetyVerdict) -> None:
        """Raise the latched high-water mark; an equal verdict keeps the first."""
        current = self._floor
        if current is not None and verdict.action <= current.action:
            return
        self._floor = verdict
        _logger.error(
            "presence latched %s by rule %s: %s", verdict.action.name, verdict.rule, verdict.detail
        )

    @staticmethod
    def _as_decision(floor: SafetyVerdict | None) -> PresenceDecision:
        """The latched floor as a decision. Only QUICK_STOP and RAMP_DOWN are ever latched."""
        if floor is None:
            return Clear()
        if floor.action is SafetyAction.QUICK_STOP:
            return EmergencyStop(floor)
        return RampDown(floor)

    # --- the start gate --------------------------------------------------

    def _start_refusals(
        self, now: Monotonic, occupancy: Occupancy | None
    ) -> tuple[PresenceRefusal, ...]:
        """Every reason a start is refused now, in a fixed order."""
        limits = self._limits
        refusals: list[PresenceRefusal] = []
        floor = self._floor
        if floor is not None:
            refusals.append(
                PresenceRefusal(
                    RULE_PRESENCE_LATCHED,
                    f"demarrage refuse : arret presence verrouille ({floor.rule}), "
                    "acquittement nomme requis",
                )
            )
        if self._camera_lost(now):
            refusals.append(
                PresenceRefusal(
                    RULE_PRESENCE_CAMERA_LOST,
                    f"demarrage refuse : {self._describe_camera(now)}",
                )
            )
        clear_since = self._zone_clear_since
        clear_for = None if clear_since is None else elapsed(clear_since, now)
        if clear_for is None or clear_for < limits.clear_before_start:
            refusals.append(
                PresenceRefusal(RULE_PRESENCE_ZONE_NOT_CLEAR, self._describe_not_clear(clear_for))
            )
        refusals.extend(self._occupancy_refusals(occupancy))
        return tuple(refusals)

    def _describe_not_clear(self, clear_for: Seconds | None) -> str:
        required = self._limits.clear_before_start
        if clear_for is None:
            return (
                "demarrage refuse : la zone du bras n'est pas vue degagee "
                f"({_describe_person(self._last_person)} ou image inexploitable)"
            )
        return (
            f"demarrage refuse : zone du bras degagee depuis {clear_for:.1f} s seulement, "
            f"{required:.1f} s requises"
        )

    def _occupancy_refusals(self, occupancy: Occupancy | None) -> tuple[PresenceRefusal, ...]:
        """What the capsule and the rider say against the declared occupancy."""
        seen = self._last_healthy
        if occupancy is None or seen is None:
            return ()
        match occupancy:
            case Occupancy.BENCH:
                if seen.capsule is CapsuleState.OCCUPIED:
                    return (
                        PresenceRefusal(
                            RULE_PRESENCE_BENCH_OCCUPIED,
                            "demarrage refuse : une personne est vue dans la capsule alors "
                            "que BANC (personne a bord : NON) est declare",
                        ),
                    )
                return ()
            case Occupancy.OCCUPIED:
                return self._rider_refusals(seen)
        raise assert_never(occupancy)

    @staticmethod
    def _rider_refusals(seen: PresenceObservation) -> tuple[PresenceRefusal, ...]:
        refusals: list[PresenceRefusal] = []
        match seen.capsule:
            case CapsuleState.EMPTY:
                refusals.append(
                    PresenceRefusal(
                        RULE_PRESENCE_RIDER_ABSENT,
                        "demarrage refuse : la capsule est vue vide alors que PERSONNE A "
                        "BORD est declare",
                    )
                )
            case CapsuleState.UNKNOWN:
                refusals.append(
                    PresenceRefusal(
                        RULE_PRESENCE_CAPSULE_UNKNOWN,
                        "demarrage refuse : la camera ne voit pas la capsule, le passager "
                        "ne peut pas etre confirme",
                    )
                )
            case CapsuleState.OCCUPIED:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        posture = seen.posture
        if posture is not None and posture.unbuckled:
            refusals.append(
                PresenceRefusal(
                    RULE_PRESENCE_RIDER_UNBUCKLED,
                    "demarrage refuse : le harnais du passager est vu detache",
                )
            )
        if posture is not None and posture.limb_outside:
            refusals.append(
                PresenceRefusal(
                    RULE_PRESENCE_LIMB_OUTSIDE,
                    "demarrage refuse : un membre du passager est vu hors de la capsule",
                )
            )
        return tuple(refusals)
