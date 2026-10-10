"""Scenarios as data: one JSON file per scenario under ``simulation/scenarios/``.

A scenario says WHAT happens (which session, which subject, which faults, when)
and what must be TRUE afterwards. How it is run lives in
:mod:`simulation.harness`; the physical invariants every run must satisfy live
in :mod:`simulation.invariants`. Adding a scenario is adding a file.

The schema (every key is checked; an unknown key is an error, so a typo cannot
silently turn a fault scenario into a nominal one)::

    {
      "name": "jog_150_nominal",
      "description": "...",
      "tags": ["auto", "nominal"],
      "kind": "auto" | "manual",
      "duration_s": 900,
      "teardown_s": 60,
      "geometry": {"reference_radius_m": 1.5, "leg_tip_radius_m": 2.4254},
      "profile": "standard_30_min" | { ...a full profile document... },
      "profile_overrides": {"total_duration_s": 900, ...},
      "manual": {"occupancy": "bench", "ceiling_motor_rpm": 1380},
      "subject": {"hr_rest": 70, "k_g": 110, "tau_up": 30, "tau_down": 55,
                  "drift_max": 10, "tau_drift": 600, "fatigue": 0, "hr_max": 185},
      "subject_events": [{"event": "vasovagal_drop", "at_s": 600, "duration_s": 20}],
      "ecg": {"mode": "direct" | "dsp", "period_s": 1.0, "noise_bpm": 0, "seed": 1},
      "drive": {"tto_s": 3.0, "acceleration_time_s": 10.0, "hsp_motor_rpm": 1380},
      "actions": [{"at_s": 5, "do": "manual_target", "output_rpm": 27, "expect": "accepted"}],
      "expect": {"start": "accepted", "end_reason": "operator_stop",
                 "final_state": "finished", "rules": ["hr_drop"], "forbid_rules": [],
                 "reaches_output_rpm": 27.0, "max_output_rpm": 28.0,
                 "min_in_zone_fraction": 0.5}
    }

Failure injection adds ``preroll_s``, ``ecg.{ectopic_rate, ectopic_bpm,
motion_noise_bpm_per_g, connect_fails}``, ``drive.{initial_enabled_rpm,
refuse_commands}`` and the :data:`Injection` actions.

See README.md for the list of ``do`` actions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, unique
from pathlib import Path
from typing import Final

from simulation.jsondoc import Reader, load_object
from src.motor.drive import ControlWord, DriveFault
from src.result import Err, Ok, Result
from src.sim.physiology import DEFAULT_PHYSIOLOGY, EventWindow, PhysiologyConfig, ScriptedEvent
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    TrainingProfile,
    parse_profile,
    parse_store,
    profile_to_document,
)
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy, SignalQuality
from src.units import Bpm, Metres, MotorRpm, OutputRpm, Seconds

SCENARIO_DIR: Final[Path] = Path(__file__).resolve().parent / "scenarios"

SIM_PROFILES_PATH: Final[Path] = SCENARIO_DIR / "_profiles.json"
"""Profiles the battery adds to the shipped ones (same store format), e.g. the ~150 bpm jog."""

DEFAULT_MANUAL_CEILING: Final[MotorRpm] = MotorRpm(1380)
"""The highest bench ceiling ``MOTOR_MAX_RPM`` accepts (the nameplate)."""

DEFAULT_TEARDOWN: Final[Seconds] = Seconds(60.0)
DEFAULT_PREROLL: Final[Seconds] = Seconds(15.0)


@unique
class SessionKind(Enum):
    """A programmed (heart-rate driven) session, or an operator-driven manual one."""

    AUTO = "auto"
    MANUAL = "manual"


@unique
class EcgMode(Enum):
    """Where the heart rate the runtime sees comes from.

    ``DSP`` is the production path: the simulated BITalino renders an ECG from
    the physiology, the REAL ``SignalTreatment`` (BioSPPy) extracts the rate,
    and ``EcgBridge`` hands it to the runtime - about 50 ms of CPU per
    simulated second. ``DIRECT`` is a sensor model for long sessions: the
    ground-truth rate, rounded, delivered at ``period_s`` with an advancing
    sequence number, plus scripted faults (dropout, quality, repeated seq).
    """

    DIRECT = "direct"
    DSP = "dsp"


@unique
class Expectation(Enum):
    """What an operator request is expected to meet."""

    ACCEPTED = "accepted"
    REFUSED = "refused"
    ANY = "any"


# =========================================================================
# Actions: a closed union, one record per thing a scenario can make happen
# =========================================================================


@dataclass(frozen=True, slots=True)
class ManualTarget:
    at: Seconds
    output_rpm: OutputRpm
    expect: Expectation


@dataclass(frozen=True, slots=True)
class OperatorStop:
    at: Seconds


@dataclass(frozen=True, slots=True)
class RemoteStop:
    """An end requested from off the machine (the path ``EndSession`` takes)."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class EmergencyStop:
    at: Seconds


@dataclass(frozen=True, slots=True)
class Acknowledge:
    at: Seconds
    estop_released: bool


@dataclass(frozen=True, slots=True)
class InjectDriveFault:
    at: Seconds
    fault: DriveFault


@dataclass(frozen=True, slots=True)
class CommsLoss:
    at: Seconds
    duration: Seconds


@dataclass(frozen=True, slots=True)
class DriveLatency:
    """Every exchange takes ``latency`` for ``duration`` (then the link is healthy again)."""

    at: Seconds
    latency: Seconds
    duration: Seconds


@dataclass(frozen=True, slots=True)
class DriveReverse:
    at: Seconds


@dataclass(frozen=True, slots=True)
class RegisterOffset:
    at: Seconds


@dataclass(frozen=True, slots=True)
class EcgDropout:
    """No heart-rate refresh at all for ``duration`` (a lost ECG)."""

    at: Seconds
    duration: Seconds


@dataclass(frozen=True, slots=True)
class EcgQuality:
    """The DSP grades the signal ``quality`` for ``duration`` (DIRECT mode)."""

    at: Seconds
    quality: SignalQuality
    duration: Seconds


@dataclass(frozen=True, slots=True)
class EcgRepeatSeq:
    """The DSP re-emits its previous metrics (same seq) for ``duration`` (DIRECT mode)."""

    at: Seconds
    duration: Seconds


@dataclass(frozen=True, slots=True)
class EcgValue:
    """The sensor reports ``bpm`` (GOOD quality) for ``duration``, whatever the heart does."""

    at: Seconds
    bpm: Bpm
    duration: Seconds


@dataclass(frozen=True, slots=True)
class BitalinoDisconnect:
    """The BITalino link drops and is not re-established."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class AttendantLeaves:
    """The attendant stops pinging the console."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class TickException:
    """The next status read raises inside the tick; the loop then exits (console close)."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class Shutdown:
    """The process is told to exit (SIGTERM): ``runtime.shutdown``."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class DriveRefuseCommand:
    """From now on the drive answers ``word`` with a Modbus exception (``BadResponse``)."""

    at: Seconds
    word: ControlWord


@dataclass(frozen=True, slots=True)
class DriveEchoMismatch:
    """The LFRD echo stops following what is written (it keeps its value at ``at``)."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class DriveSpeedStuck:
    """RFRD (the measured speed) stays at its value at ``at``: the speed no longer follows."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class DriveStatusFrozen:
    """Every status read answers ``Ok`` with the status seen at ``at``: a frozen drive."""

    at: Seconds


@dataclass(frozen=True, slots=True)
class LoopStall:
    """The event loop stops for ``duration``: no tick, no keepalive, no ECG; the plant runs on."""

    at: Seconds
    duration: Seconds


@dataclass(frozen=True, slots=True)
class ClockJump:
    """The WALL clock steps by ``jump`` (NTP, an RTC-less Pi). Monotonic time is unaffected."""

    at: Seconds
    jump: Seconds


@dataclass(frozen=True, slots=True)
class EcgSeqGap:
    """The next reading's sequence number jumps by ``gap`` (lost frames) (DIRECT mode)."""

    at: Seconds
    gap: int


@dataclass(frozen=True, slots=True)
class EcgSilentStop:
    """Readings stop for good while the link still reports itself up (DIRECT mode)."""

    at: Seconds


@unique
class SignalFault(Enum):
    """What happens to the raw BITalino samples on their way to the real DSP (DSP mode)."""

    FLAT = "flat"
    """A dead-flat line (a lead lying on the table, an amplifier stuck)."""

    SATURATED = "saturated"
    """Every sample pinned at the ADC's full scale."""

    CORRUPTED = "corrupted"
    """Frames that decode into noise: uniformly random counts."""

    MAINS = "mains"
    """A strong 50 Hz hum riding on the signal."""

    STOPPED = "stopped"
    """No batch at all: the frames stop arriving, the link still says it is up."""

    GAPS = "gaps"
    """Three batches in four are lost (sequence gaps)."""


@dataclass(frozen=True, slots=True)
class BitalinoSignal:
    """The raw samples suffer ``fault`` for ``duration`` (DSP mode)."""

    at: Seconds
    fault: SignalFault
    duration: Seconds


@dataclass(frozen=True, slots=True)
class StartAgain:
    """The operator presses START again while the session is live (double start)."""

    at: Seconds
    expect: Expectation


@dataclass(frozen=True, slots=True)
class OperatorFaultReset:
    """The operator asks for a drive fault reset (``runtime.fault_reset``)."""

    at: Seconds
    expect: Expectation


type Injection = (
    DriveRefuseCommand
    | DriveEchoMismatch
    | DriveSpeedStuck
    | DriveStatusFrozen
    | LoopStall
    | ClockJump
    | EcgSeqGap
    | EcgSilentStop
    | BitalinoSignal
    | StartAgain
    | OperatorFaultReset
)
"""The failure-injection and operator-error actions (the failure battery's vocabulary)."""

type Action = (
    ManualTarget
    | OperatorStop
    | RemoteStop
    | EmergencyStop
    | Acknowledge
    | InjectDriveFault
    | CommsLoss
    | DriveLatency
    | DriveReverse
    | RegisterOffset
    | EcgDropout
    | EcgQuality
    | EcgRepeatSeq
    | EcgValue
    | BitalinoDisconnect
    | AttendantLeaves
    | TickException
    | Shutdown
    | Injection
)


# =========================================================================
# The scenario
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class EcgSpec:
    mode: EcgMode = EcgMode.DIRECT
    period: Seconds = Seconds(1.0)
    noise_bpm: float = 0.0
    seed: int = 0
    ectopic_rate: float = 0.0
    """Probability that one reading carries an ectopic-beat artefact (DIRECT mode)."""

    ectopic_bpm: float = 0.0
    """Size of that artefact, bpm, either sign (DIRECT mode)."""

    motion_noise_bpm_per_g: float = 0.0
    """Extra Gaussian noise per g at the reference radius: motion artefact (DIRECT mode)."""

    connect_fails: bool = False
    """The BITalino never connects (an unpaired device, a flat battery)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveSpec:
    tto: Seconds = Seconds(3.0)
    acceleration_time: Seconds = Seconds(10.0)
    hsp_motor_rpm: MotorRpm = MotorRpm(1380)
    initial_fault: DriveFault | None = None
    """A fault already latched when the console opens the link (before the start)."""

    refuse_commands: tuple[ControlWord, ...] = ()
    """Command words the drive answers with a Modbus exception from the very first frame."""

    initial_enabled_rpm: MotorRpm | None = None
    """The drive is found OPERATION_ENABLED, LFRD and shaft at this speed: a previous
    process died with the motor commanded and this console was started over it."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualSpec:
    occupancy: Occupancy = Occupancy.BENCH
    ceiling: MotorRpm = DEFAULT_MANUAL_CEILING


@dataclass(frozen=True, slots=True, kw_only=True)
class Expect:
    start: Expectation = Expectation.ACCEPTED
    end_reason: EndReason | None = None
    final_state: RuntimeState | None = None
    rules: tuple[str, ...] = ()
    forbid_rules: tuple[str, ...] = ()
    reaches_output_rpm: OutputRpm | None = None
    max_output_rpm: OutputRpm | None = None
    min_in_zone_fraction: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Scenario:
    name: str
    description: str
    tags: tuple[str, ...]
    kind: SessionKind
    duration: Seconds
    teardown: Seconds = DEFAULT_TEARDOWN
    preroll: Seconds = DEFAULT_PREROLL
    """Idle console time before the start command (read-only polling only)."""

    reference_radius: Metres | None = None
    leg_tip_radius: Metres | None = None
    profile: TrainingProfile | None = None
    """The programme, for an AUTO scenario."""

    manual: ManualSpec = field(default_factory=ManualSpec)
    subject: PhysiologyConfig = DEFAULT_PHYSIOLOGY
    events: tuple[EventWindow, ...] = ()
    ecg: EcgSpec = field(default_factory=EcgSpec)
    drive: DriveSpec = field(default_factory=DriveSpec)
    actions: tuple[Action, ...] = ()
    expect: Expect = field(default_factory=Expect)
    known_defect: str | None = None
    """Set when the expectations describe CORRECT behaviour that ``raspberry-pi/src``
    does not deliver today: the battery marks the scenario xfail(strict), so it
    stays visible and turns red the day the defect is fixed (remove the key then)."""


@dataclass(frozen=True, slots=True)
class ScenarioInvalid:
    """The file is not a runnable scenario. Every problem, in reading order."""

    problems: tuple[str, ...]

    @property
    def detail(self) -> str:
        return "; ".join(self.problems)


_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "description",
        "tags",
        "kind",
        "duration_s",
        "teardown_s",
        "preroll_s",
        "geometry",
        "profile",
        "profile_overrides",
        "manual",
        "subject",
        "subject_events",
        "ecg",
        "drive",
        "actions",
        "expect",
        "known_defect",
    }
)


def _enum[E: Enum](reader: Reader, key: str, kind: type[E], default: E | None) -> E | None:
    """An enum member by its value; ``default`` when absent, a problem when unknown."""
    if not reader.has(key):
        if default is None:
            reader.note(f"missing '{key}'")
        return default
    text = reader.text(key)
    try:
        return kind(text)
    except ValueError:
        reader.note(f"'{key}' = {text!r} is not a valid {kind.__name__}")
        return default


def _store(path: Path) -> tuple[TrainingProfile, ...]:
    """One profile store file, parsed read-only. Raises if it is unusable."""
    parsed = parse_store(path.read_text(encoding="utf-8"))
    if isinstance(parsed, Err):
        raise ValueError(f"{path.name} unusable: {parsed.error.detail}")
    return parsed.value.profiles


def shipped_profile_ids() -> tuple[str, ...]:
    """Every profile id in ``raspberry-pi/config/profiles.default.json``."""
    return tuple(profile.profile_id for profile in _store(SHIPPED_DEFAULTS_PATH))


def _shipped_profiles() -> Mapping[str, TrainingProfile]:
    """The shipped defaults plus the battery's own profiles (shipped ids win)."""
    extra = {profile.profile_id: profile for profile in _store(SIM_PROFILES_PATH)}
    return {**extra, **{profile.profile_id: profile for profile in _store(SHIPPED_DEFAULTS_PATH)}}


def _profile(reader: Reader) -> TrainingProfile | None:
    raw = reader.raw("profile")
    base: Mapping[str, object]
    if isinstance(raw, str):
        found = _shipped_profiles().get(raw)
        if found is None:
            reader.note(f"'profile' = {raw!r} is not a known profile {sorted(_shipped_profiles())}")
            return None
        base = profile_to_document(found)
    else:
        base = reader.mapping("profile")
        if not base:
            reader.note("'profile' must be a shipped profile id or a profile object")
            return None
    document = {**base, **reader.mapping("profile_overrides")}
    parsed = parse_profile(document, where="profile")
    if isinstance(parsed, Err):
        reader.note(f"profile refused: {parsed.error.detail}")
        return None
    return parsed.value


def _subject(reader: Reader) -> PhysiologyConfig:
    sub = Reader(reader.mapping("subject"), "subject")
    sub.unknown_keys(
        frozenset(
            {"hr_rest", "hr_max", "k_g", "tau_up", "tau_down", "drift_max", "tau_drift", "fatigue"}
        )
    )
    d = DEFAULT_PHYSIOLOGY
    try:
        config = PhysiologyConfig(
            hr_rest=Bpm(sub.integer("hr_rest", d.hr_rest)),
            hr_max=Bpm(sub.integer("hr_max", d.hr_max)),
            k_g=sub.number("k_g", d.k_g),
            tau_up=Seconds(sub.number("tau_up", d.tau_up)),
            tau_down=Seconds(sub.number("tau_down", d.tau_down)),
            drift_max=sub.number("drift_max", d.drift_max),
            tau_drift=Seconds(sub.number("tau_drift", d.tau_drift)),
            fatigue=sub.number("fatigue", d.fatigue),
        )
    except ValueError as error:
        sub.note(str(error))
        config = d
    for problem in sub.problems:
        reader.note(problem)
    return config


def _events(reader: Reader) -> tuple[EventWindow, ...]:
    windows: list[EventWindow] = []
    for index, entry in enumerate(reader.objects("subject_events")):
        sub = Reader(entry, f"subject_events[{index}]")
        sub.unknown_keys(frozenset({"event", "at_s", "duration_s"}))
        event = _enum(sub, "event", ScriptedEvent, None)
        at = sub.number("at_s")
        duration = sub.number("duration_s")
        if event is not None and not sub.problems:
            try:
                windows.append(EventWindow(event, Seconds(at), Seconds(duration)))
            except ValueError as error:
                sub.note(str(error))
        for problem in sub.problems:
            reader.note(problem)
    return tuple(windows)


def _positive(sub: Reader, key: str) -> Seconds:
    value = sub.number(key)
    if value <= 0.0:
        sub.note(f"'{key}' must be positive")
    return Seconds(value)


def _action_target(sub: Reader, at: Seconds) -> Action:
    expect = _enum(sub, "expect", Expectation, Expectation.ACCEPTED) or Expectation.ACCEPTED
    return ManualTarget(at, OutputRpm(sub.number("output_rpm")), expect)


def _action_fault(sub: Reader, at: Seconds) -> Action | None:
    name = sub.text("fault", "MOTOR_OVERLOAD")
    fault = DriveFault.__members__.get(name)
    if fault is None:
        sub.note(f"'fault' = {name!r} is not a DriveFault {list(DriveFault.__members__)}")
        return None
    return InjectDriveFault(at, fault)


def _action_refuse(sub: Reader, at: Seconds) -> Action | None:
    name = sub.text("word", "ENABLE_OPERATION")
    word = ControlWord.__members__.get(name)
    if word is None:
        sub.note(f"'word' = {name!r} is not a ControlWord {list(ControlWord.__members__)}")
        return None
    return DriveRefuseCommand(at, word)


def _action_signal(sub: Reader, at: Seconds) -> Action | None:
    fault = _enum(sub, "signal", SignalFault, None)
    duration = _positive(sub, "duration_s")
    return None if fault is None else BitalinoSignal(at, fault, duration)


def _expected(sub: Reader, default: Expectation) -> Expectation:
    return _enum(sub, "expect", Expectation, default) or default


def _action_quality(sub: Reader, at: Seconds) -> Action | None:
    quality = _enum(sub, "quality", SignalQuality, None)
    duration = _positive(sub, "duration_s")
    return None if quality is None else EcgQuality(at, quality, duration)


_ACTION_BUILDERS: Final[Mapping[str, Callable[[Reader, Seconds], Action | None]]] = {
    "manual_target": _action_target,
    "operator_stop": lambda _sub, at: OperatorStop(at),
    "remote_stop": lambda _sub, at: RemoteStop(at),
    "estop": lambda _sub, at: EmergencyStop(at),
    "acknowledge": lambda sub, at: Acknowledge(at, sub.flag("estop_released", default=True)),
    "drive_fault": _action_fault,
    "comms_loss": lambda sub, at: CommsLoss(at, _positive(sub, "duration_s")),
    "drive_latency": lambda sub, at: DriveLatency(
        at, _positive(sub, "latency_s"), _positive(sub, "duration_s")
    ),
    "drive_reverse": lambda _sub, at: DriveReverse(at),
    "register_offset": lambda _sub, at: RegisterOffset(at),
    "ecg_dropout": lambda sub, at: EcgDropout(at, _positive(sub, "duration_s")),
    "ecg_quality": _action_quality,
    "ecg_repeat_seq": lambda sub, at: EcgRepeatSeq(at, _positive(sub, "duration_s")),
    "ecg_value": lambda sub, at: EcgValue(
        at, Bpm(sub.integer("bpm")), _positive(sub, "duration_s")
    ),
    "bitalino_disconnect": lambda _sub, at: BitalinoDisconnect(at),
    "attendant_leaves": lambda _sub, at: AttendantLeaves(at),
    "tick_exception": lambda _sub, at: TickException(at),
    "shutdown": lambda _sub, at: Shutdown(at),
    "drive_refuse_command": _action_refuse,
    "drive_echo_mismatch": lambda _sub, at: DriveEchoMismatch(at),
    "drive_speed_stuck": lambda _sub, at: DriveSpeedStuck(at),
    "drive_status_frozen": lambda _sub, at: DriveStatusFrozen(at),
    "loop_stall": lambda sub, at: LoopStall(at, _positive(sub, "duration_s")),
    "clock_jump": lambda sub, at: ClockJump(at, Seconds(sub.number("jump_s"))),
    "ecg_seq_gap": lambda sub, at: EcgSeqGap(at, sub.integer("gap", 5)),
    "ecg_silent_stop": lambda _sub, at: EcgSilentStop(at),
    "bitalino_signal": _action_signal,
    "start_again": lambda sub, at: StartAgain(at, _expected(sub, Expectation.REFUSED)),
    "fault_reset": lambda sub, at: OperatorFaultReset(at, _expected(sub, Expectation.ANY)),
}

_ACTION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "at_s",
        "do",
        "output_rpm",
        "expect",
        "estop_released",
        "fault",
        "duration_s",
        "latency_s",
        "quality",
        "bpm",
        "word",
        "jump_s",
        "gap",
        "signal",
    }
)

ACTION_NAMES: Final[tuple[str, ...]] = tuple(_ACTION_BUILDERS)


def _actions(reader: Reader) -> tuple[Action, ...]:
    actions: list[Action] = []
    for index, entry in enumerate(reader.objects("actions")):
        sub = Reader(entry, f"actions[{index}]")
        sub.unknown_keys(_ACTION_KEYS)
        at = Seconds(sub.number("at_s"))
        if at < 0.0:
            sub.note("'at_s' must not be negative")
        name = sub.text("do")
        builder = _ACTION_BUILDERS.get(name)
        if builder is None:
            sub.note(f"'do' = {name!r} is not one of {list(ACTION_NAMES)}")
        else:
            action = builder(sub, at)
            if action is not None and not sub.problems:
                actions.append(action)
        for problem in sub.problems:
            reader.note(problem)
    return tuple(sorted(actions, key=lambda action: action.at))


def _expect(reader: Reader) -> Expect:
    sub = Reader(reader.mapping("expect"), "expect")
    sub.unknown_keys(
        frozenset(
            {
                "start",
                "end_reason",
                "final_state",
                "rules",
                "forbid_rules",
                "reaches_output_rpm",
                "max_output_rpm",
                "min_in_zone_fraction",
            }
        )
    )
    reaches = sub.optional_number("reaches_output_rpm")
    ceiling = sub.optional_number("max_output_rpm")
    expect = Expect(
        start=_enum(sub, "start", Expectation, Expectation.ACCEPTED) or Expectation.ACCEPTED,
        end_reason=_enum(sub, "end_reason", EndReason, None) if sub.has("end_reason") else None,
        final_state=(
            _enum(sub, "final_state", RuntimeState, None) if sub.has("final_state") else None
        ),
        rules=sub.strings("rules"),
        forbid_rules=sub.strings("forbid_rules"),
        reaches_output_rpm=None if reaches is None else OutputRpm(reaches),
        max_output_rpm=None if ceiling is None else OutputRpm(ceiling),
        min_in_zone_fraction=sub.optional_number("min_in_zone_fraction"),
    )
    for problem in sub.problems:
        reader.note(problem)
    return expect


def _geometry(reader: Reader) -> tuple[Metres | None, Metres | None]:
    sub = Reader(reader.mapping("geometry"), "geometry")
    sub.unknown_keys(frozenset({"reference_radius_m", "leg_tip_radius_m"}))
    reference = sub.optional_number("reference_radius_m")
    leg_tip = sub.optional_number("leg_tip_radius_m")
    for problem in sub.problems:
        reader.note(problem)
    return (
        None if reference is None else Metres(reference),
        None if leg_tip is None else Metres(leg_tip),
    )


def _ecg(reader: Reader) -> EcgSpec:
    sub = Reader(reader.mapping("ecg"), "ecg")
    sub.unknown_keys(
        frozenset(
            {
                "mode",
                "period_s",
                "noise_bpm",
                "seed",
                "ectopic_rate",
                "ectopic_bpm",
                "motion_noise_bpm_per_g",
                "connect_fails",
            }
        )
    )
    spec = EcgSpec(
        mode=_enum(sub, "mode", EcgMode, EcgMode.DIRECT) or EcgMode.DIRECT,
        period=Seconds(sub.number("period_s", 1.0)),
        noise_bpm=sub.number("noise_bpm", 0.0),
        seed=sub.integer("seed", 0),
        ectopic_rate=sub.number("ectopic_rate", 0.0),
        ectopic_bpm=sub.number("ectopic_bpm", 0.0),
        motion_noise_bpm_per_g=sub.number("motion_noise_bpm_per_g", 0.0),
        connect_fails=sub.flag("connect_fails", default=False),
    )
    if spec.period <= 0.0:
        sub.note("'period_s' must be positive")
    if spec.noise_bpm < 0.0:
        sub.note("'noise_bpm' must not be negative")
    if min(spec.ectopic_bpm, spec.motion_noise_bpm_per_g) < 0.0:
        sub.note("'ectopic_bpm' and 'motion_noise_bpm_per_g' must not be negative")
    if not 0.0 <= spec.ectopic_rate <= 1.0:
        sub.note("'ectopic_rate' must be a probability in [0, 1]")
    for problem in sub.problems:
        reader.note(problem)
    return spec


def _drive(reader: Reader) -> DriveSpec:
    sub = Reader(reader.mapping("drive"), "drive")
    sub.unknown_keys(
        frozenset(
            {
                "tto_s",
                "acceleration_time_s",
                "hsp_motor_rpm",
                "initial_fault",
                "initial_enabled_rpm",
                "refuse_commands",
            }
        )
    )
    initial: DriveFault | None = None
    if sub.has("initial_fault"):
        name = sub.text("initial_fault")
        initial = DriveFault.__members__.get(name)
        if initial is None:
            sub.note(f"'initial_fault' = {name!r} is not a DriveFault")
    enabled = sub.integer("initial_enabled_rpm") if sub.has("initial_enabled_rpm") else None
    refused: list[ControlWord] = []
    for name in sub.strings("refuse_commands"):
        word = ControlWord.__members__.get(name)
        if word is None:
            sub.note(f"'refuse_commands' holds {name!r}, not a ControlWord")
        else:
            refused.append(word)
    spec = DriveSpec(
        tto=Seconds(sub.number("tto_s", 3.0)),
        acceleration_time=Seconds(sub.number("acceleration_time_s", 10.0)),
        hsp_motor_rpm=MotorRpm(sub.integer("hsp_motor_rpm", 1380)),
        initial_fault=initial,
        initial_enabled_rpm=None if enabled is None else MotorRpm(enabled),
        refuse_commands=tuple(refused),
    )
    if spec.tto <= 0.0 or spec.acceleration_time <= 0.0 or spec.hsp_motor_rpm <= 0:
        sub.note("every drive number must be positive")
    if enabled is not None and not 0 < enabled <= spec.hsp_motor_rpm:
        sub.note("'initial_enabled_rpm' must be in (0, hsp_motor_rpm]")
    if enabled is not None and initial is not None:
        sub.note("'initial_enabled_rpm' and 'initial_fault' are exclusive")
    for problem in sub.problems:
        reader.note(problem)
    return spec


def _manual(reader: Reader) -> ManualSpec:
    sub = Reader(reader.mapping("manual"), "manual")
    sub.unknown_keys(frozenset({"occupancy", "ceiling_motor_rpm"}))
    spec = ManualSpec(
        occupancy=_enum(sub, "occupancy", Occupancy, Occupancy.BENCH) or Occupancy.BENCH,
        ceiling=MotorRpm(sub.integer("ceiling_motor_rpm", DEFAULT_MANUAL_CEILING)),
    )
    for problem in sub.problems:
        reader.note(problem)
    return spec


def parse_scenario(raw: str, *, where: str = "scenario") -> Result[Scenario, ScenarioInvalid]:
    """Parse one scenario document, reporting every problem at once."""
    document = load_object(raw)
    if isinstance(document, str):
        return Err(ScenarioInvalid((f"{where}: {document}",)))
    reader = Reader(document, where)
    reader.unknown_keys(_TOP_KEYS)
    kind = _enum(reader, "kind", SessionKind, None)
    duration = Seconds(reader.number("duration_s"))
    if duration <= 0.0:
        reader.note("'duration_s' must be positive")
    teardown = Seconds(reader.number("teardown_s", DEFAULT_TEARDOWN))
    if teardown < 0.0:
        reader.note("'teardown_s' must not be negative")
    preroll = Seconds(reader.number("preroll_s", DEFAULT_PREROLL))
    if preroll < 0.0:
        reader.note("'preroll_s' must not be negative")
    reference, leg_tip = _geometry(reader)
    profile = _profile(reader) if kind is SessionKind.AUTO else None
    if kind is SessionKind.MANUAL and reader.has("profile"):
        reader.note("'profile' belongs to an auto scenario")
    scenario = Scenario(
        name=reader.text("name"),
        description=reader.text("description", ""),
        tags=reader.strings("tags"),
        kind=kind or SessionKind.MANUAL,
        duration=duration,
        teardown=teardown,
        preroll=preroll,
        reference_radius=reference,
        leg_tip_radius=leg_tip,
        profile=profile,
        manual=_manual(reader),
        subject=_subject(reader),
        events=_events(reader),
        ecg=_ecg(reader),
        drive=_drive(reader),
        actions=_actions(reader),
        expect=_expect(reader),
        known_defect=reader.text("known_defect") if reader.has("known_defect") else None,
    )
    if reader.problems:
        return Err(ScenarioInvalid(reader.problems))
    return Ok(scenario)


def load_scenario(path: Path) -> Result[Scenario, ScenarioInvalid]:
    """Read and parse one scenario file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        return Err(ScenarioInvalid((f"{path}: {error}",)))
    return parse_scenario(raw, where=path.name)


def scenario_paths(directory: Path = SCENARIO_DIR) -> tuple[Path, ...]:
    """Every scenario file, sorted by name."""
    return tuple(path for path in sorted(directory.glob("*.json")) if not path.name.startswith("_"))


def resolve(name_or_path: str, directory: Path = SCENARIO_DIR) -> Path:
    """A scenario given by name (``jog_150_nominal``) or by path."""
    candidate = Path(name_or_path)
    if candidate.suffix == ".json" or candidate.exists():
        return candidate
    return directory / f"{name_or_path}.json"


def with_duration(scenario: Scenario, duration: Seconds) -> Scenario:
    """The same scenario over a different horizon (for quick property runs)."""
    return replace(scenario, duration=duration)
