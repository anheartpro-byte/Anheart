"""The failure-injection matrix: every way the machine can fail, at every phase, judged alike.

Each :class:`FailureCase` is a scenario DOCUMENT (the same JSON schema as
``scenarios/*.json``, parsed by :func:`~simulation.scenario.parse_scenario`, so
the data format provably covers every case) plus what must be true after it.
:func:`judge` then asserts, for every case:

* every physical invariant of :mod:`simulation.invariants` (no NaN, the
  setpoint domain, the rate limits, safety dominating, and **every exit path:
  shaft at 0 rpm, no torque, LFRD 0 where a frame could be written**);
* the output is disabled, or - when the runtime went silent - the drive's own
  ``ttO`` watchdog visibly took over (the simulator latched a fault reaction
  after the silence);
* the end reason is one of those the case allows, and the rules it names fired;
* the operator was told, in the console's own words (the ``OperatorMessage``
  the harness records: the verdict sentence, the drive's mnemonic, the refusal
  text of ``src.local_panel``), with the substrings the case names;
* for the ECG signal cases, no false heart rate was reported during the
  corruption (a rate the DSP grades usable must be within
  :data:`RATE_TOLERANCE` of the truth);
* the injection landed in the phase the case says it tests.

No exception may escape a run: :func:`run_case` lets one propagate, which fails
the test. ``known_defect`` marks a case whose expectations are correct
behaviour ``raspberry-pi/src`` does not deliver today (strict xfail, evidence
in the text).

Cases the harness cannot express (the web layer failing, the dashboard link
failing, a real SIGTERM through ``LocalPanel.run``) are exercised against the
real composition root in ``raspberry-pi/tests/test_failure_*.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from simulation.harness import RunResult, run_scenario
from simulation.invariants import Violation, check_expectations, check_invariants, rules_seen
from simulation.scenario import Scenario, parse_scenario
from simulation.tracefile import JsonValue
from src.motor.drive import DriveFault, describe_fault
from src.motor.simulated import lft_code_for
from src.result import Err

FAILURE_PROFILE: Final[str] = "jog_150_short"
"""The 13-minute jog (145-155 bpm): baseline 0-60, warmup 60-360, hold 360-660,
cooldown 660-720, recovery 720-780 s."""

AUTO_PHASES: Final[Mapping[str, float]] = {
    "baseline": 30.0,
    "warmup": 200.0,
    "hold": 500.0,
    "cooldown": 690.0,
    "recovery": 750.0,
}
"""Injection instant (s after the start) per programme phase."""

MANUAL_PHASES: Final[Mapping[str, float]] = {"ramp_up": 40.0, "at_speed": 170.0, "ramp_down": 230.0}
"""Injection instant per manual-session stage: climbing to 27 rpm, holding it, stopping."""

MANUAL_PHASE_NAMES: Final[Mapping[str, str]] = {
    "ramp_up": "hold",
    "at_speed": "hold",
    "ramp_down": "cooldown",
}
"""The runtime's own phase at those instants (a manual session is HOLD until it ends)."""

MANUAL_STOP_S: Final[float] = 200.0
MANUAL_DURATION_S: Final[float] = 330.0
AUTO_AFTER_S: Final[float] = 240.0
"""An auto case runs this long after its injection (capped at the programme's end)."""

AUTO_DURATION_S: Final[float] = 800.0
DSP_AT_S: Final[float] = 120.0
DSP_FOR_S: Final[float] = 40.0
DSP_DURATION_S: Final[float] = 300.0

RATE_TOLERANCE: Final[int] = 15
"""A heart rate graded usable during a signal fault must be within this of the truth, bpm."""

REFUSED: Final[str] = "refused"
"""In ``end_reasons``: the start itself was refused (so no session ended)."""

WATCHDOG_STATES: Final[frozenset[str]] = frozenset({"FAULT_REACTION_RAMP_STOP", "FAULT"})


@unique
class Category(Enum):
    DRIVE = "drive"
    ECG = "ecg"
    PROCESS = "process"
    OPERATOR = "operator"


type Document = Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureCase:
    """One injected failure and what must be true afterwards."""

    name: str
    category: Category
    description: str
    document: Document
    end_reasons: frozenset[str]
    """Allowed ``EndReason`` values, or :data:`REFUSED`."""

    rules: tuple[str, ...] = ()
    messages: tuple[str, ...] = ()
    """Each must appear in at least one operator message."""

    silent: bool | None = None
    """Whether the runtime must have gone silent (``None``: either)."""

    at: float | None = None
    phase: str | None = None
    """The runtime's phase at ``at``: the case tests THAT phase."""

    rate_window: tuple[float, float] | None = None
    deadline: float | None = None
    """Every rule in ``rules`` must have fired by this instant (s after the start)."""

    stopped_by: float | None = None
    """By this instant the measured shaft must be at rest, or the runtime silent (ttO's job)."""

    known_defect: str | None = None


# =========================================================================
# Building the documents
# =========================================================================


def _auto(name: str, actions: Sequence[Document], *, at: float, **extra: JsonValue) -> Document:
    return {
        "name": name,
        "kind": "auto",
        "profile": FAILURE_PROFILE,
        "duration_s": min(AUTO_DURATION_S, at + AUTO_AFTER_S),
        "actions": list(actions),
        **extra,
    }


def _manual(
    name: str, actions: Sequence[Document], *, climb: bool = True, **extra: JsonValue
) -> Document:
    """The bench session to 27 rpm, stopped at :data:`MANUAL_STOP_S`; ``climb=False``: no
    target and no stop (for a start that is refused)."""
    base: list[Document] = (
        [
            {"at_s": 2.0, "do": "manual_target", "output_rpm": 27.0},
            {"at_s": MANUAL_STOP_S, "do": "operator_stop"},
        ]
        if climb
        else []
    )
    return {
        "name": name,
        "kind": "manual",
        "duration_s": MANUAL_DURATION_S,
        "actions": [*base, *actions],
        **extra,
    }


def _dsp(name: str, signal: str) -> Document:
    return {
        "name": name,
        "kind": "auto",
        "profile": FAILURE_PROFILE,
        "duration_s": DSP_DURATION_S,
        "ecg": {"mode": "dsp"},
        "actions": [
            {"at_s": DSP_AT_S, "do": "bitalino_signal", "signal": signal, "duration_s": DSP_FOR_S}
        ],
    }


def _fault_label(fault: DriveFault) -> str:
    """What the console shows for ``fault`` as the simulator raises it: ``OCF (LFT 15)``."""
    report = describe_fault(lft_code_for(fault))
    return f"{report.fault.mnemonic} (LFT {report.raw_code})"


VERDICT: Final[frozenset[str]] = frozenset({"safety_verdict"})


def _drive_cases() -> list[FailureCase]:
    cases: list[FailureCase] = []
    for phase, at in AUTO_PHASES.items():
        cases.append(
            FailureCase(
                name=f"drive_comm_timeout_auto_{phase}",
                category=Category.DRIVE,
                description=f"The Modbus link dies for 40 s during {phase.upper()} of the jog.",
                document=_auto(
                    f"drive_comm_timeout_auto_{phase}",
                    [{"at_s": at, "do": "comms_loss", "duration_s": 40.0}],
                    at=at,
                ),
                end_reasons=VERDICT,
                rules=("comms_lost",),
                messages=("comms_lost", "ttO"),
                silent=True,
                at=at,
                phase=phase,
            )
        )
    for stage, at in MANUAL_PHASES.items():
        cases.append(
            FailureCase(
                name=f"drive_comm_timeout_manual_{stage}",
                category=Category.DRIVE,
                description=f"The Modbus link dies for 40 s, manual session, {stage}.",
                document=_manual(
                    f"drive_comm_timeout_manual_{stage}",
                    [{"at_s": at, "do": "comms_loss", "duration_s": 40.0}],
                ),
                end_reasons=frozenset({"operator_stop"}) if at > MANUAL_STOP_S else VERDICT,
                rules=("comms_lost",),
                messages=("comms_lost", "ttO"),
                silent=True,
                at=at,
                phase=MANUAL_PHASE_NAMES[stage],
            )
        )
    for fault in DriveFault:
        if fault is DriveFault.NO_FAULT_STORED:
            continue  # not a fault: LFT reading 0
        label = _fault_label(fault)
        for kind, at in (("auto", AUTO_PHASES["hold"]), ("manual", MANUAL_PHASES["at_speed"])):
            action: Document = {"at_s": at, "do": "drive_fault", "fault": fault.name}
            name = f"drive_fault_{fault.name.lower()}_{kind}"
            cases.append(
                FailureCase(
                    name=name,
                    category=Category.DRIVE,
                    description=f"The drive latches {fault.name} ({label}) at speed ({kind}).",
                    document=(
                        _auto(name, [action], at=at) if kind == "auto" else _manual(name, [action])
                    ),
                    end_reasons=VERDICT,
                    rules=("drive_fault",),
                    messages=(label, "drive_fault"),
                    silent=False,
                    at=at,
                    phase="hold",
                )
            )
    cases += [
        FailureCase(
            name="drive_stuck_enabled_at_startup",
            category=Category.DRIVE,
            description="A crashed predecessor left the drive OPERATION_ENABLED at 900 rpm and "
            "START is pressed at once (no idle pre-roll, so the idle console has not seen it "
            "yet): the start is refused, the reference zeroed, the verdict latched.",
            document=_manual(
                "drive_stuck_enabled_at_startup",
                [],
                climb=False,
                preroll_s=0.0,
                drive={"initial_enabled_rpm": 900},
                expect={"start": "refused"},
            ),
            end_reasons=frozenset({REFUSED}),
            rules=("drive_precommanded",),
            messages=("variateur deja en marche", "drive_precommanded"),
            silent=False,
        ),
        FailureCase(
            name="drive_stuck_enabled_idle_console",
            category=Category.DRIVE,
            description="Same, but the console sits idle for 60 s before anyone presses START: "
            "the idle poll stops the turning drive (zero, disable at standstill), latches "
            "drive_precommanded, and START is then refused until it is acknowledged.",
            document=_manual(
                "drive_stuck_enabled_idle_console",
                [],
                climb=False,
                preroll_s=60.0,
                drive={"initial_enabled_rpm": 900},
                expect={"start": "refused"},
            ),
            end_reasons=frozenset({REFUSED}),
            rules=("drive_precommanded",),
            messages=("drive_precommanded",),
            silent=False,
        ),
        FailureCase(
            name="drive_refuses_enable_at_start",
            category=Category.DRIVE,
            description="The drive answers ENABLE_OPERATION with an exception: the start is "
            "refused and nothing is energised.",
            document=_manual(
                "drive_refuses_enable_at_start",
                [],
                climb=False,
                drive={"refuse_commands": ["ENABLE_OPERATION"]},
                expect={"start": "refused"},
            ),
            end_reasons=frozenset({REFUSED}),
            messages=("demarrage refuse", "ENABLE_OPERATION"),
            silent=False,
        ),
        FailureCase(
            name="drive_refuses_switch_on_at_start",
            category=Category.DRIVE,
            description="The drive answers SWITCH_ON with an exception at the start.",
            document=_manual(
                "drive_refuses_switch_on_at_start",
                [],
                climb=False,
                drive={"refuse_commands": ["SWITCH_ON"]},
                expect={"start": "refused"},
            ),
            end_reasons=frozenset({REFUSED}),
            messages=("demarrage refuse", "SWITCH_ON"),
            silent=False,
        ),
        FailureCase(
            name="drive_refuses_switch_on_at_stop",
            category=Category.DRIVE,
            description="The drive refuses SWITCH_ON (the first stop word) once the manual "
            "session is running: the stop sequence cannot complete.",
            document=_manual(
                "drive_refuses_switch_on_at_stop",
                [{"at_s": 150.0, "do": "drive_refuse_command", "word": "SWITCH_ON"}],
            ),
            end_reasons=frozenset({"operator_stop"}),
            rules=("disable_refused",),
            messages=("SWITCH_ON",),
            silent=False,
        ),
        FailureCase(
            name="drive_setpoint_echo_mismatch",
            category=Category.DRIVE,
            description="The LFRD echo stops following the writes during the climb.",
            document=_manual(
                "drive_setpoint_echo_mismatch",
                [{"at_s": 60.0, "do": "drive_echo_mismatch"}],
            ),
            end_reasons=VERDICT,
            rules=("setpoint_unconfirmed",),
            messages=("echo",),
            silent=False,
            deadline=65.0,
        ),
        FailureCase(
            name="drive_register_offset_at_speed",
            category=Category.DRIVE,
            description="At 27 rpm every write starts landing one register off (acked, "
            "ignored): the speed can no longer be commanded down, so the runtime must give "
            "the stop to the drive's own watchdog. At constant speed a misaddressed keepalive "
            "is indistinguishable from a landed one, so it shows only once the operator's "
            "stop changes the setpoint (the ending is then the operator's).",
            document=_manual(
                "drive_register_offset_at_speed", [{"at_s": 170.0, "do": "register_offset"}]
            ),
            end_reasons=frozenset({"operator_stop", "safety_verdict"}),
            rules=("setpoint_unconfirmed", "tracking_error"),
            messages=("tracking_error", "echo", "ttO"),
            silent=True,
            at=170.0,
            phase="hold",
            deadline=MANUAL_STOP_S + 15.0,
            stopped_by=260.0,
        ),
        FailureCase(
            name="drive_speed_not_following_ramp",
            category=Category.DRIVE,
            description="RFRD stops following during the manual climb (a slipping coupling, "
            "a jammed arm): tracking_error must fire within a few seconds.",
            document=_manual(
                "drive_speed_not_following_ramp",
                [{"at_s": 60.0, "do": "drive_speed_stuck"}],
                expect={"rules": ["tracking_error"]},
            ),
            end_reasons=VERDICT,
            rules=("tracking_error",),
            messages=("tracking_error",),
            silent=False,
            at=60.0,
            deadline=70.0,
        ),
        FailureCase(
            name="drive_speed_not_following_at_speed",
            category=Category.DRIVE,
            description="RFRD stuck at speed, then the operator stops: the measured speed "
            "never falls, so standstill is never shown and the run command must stay.",
            document=_manual(
                "drive_speed_not_following_at_speed",
                [{"at_s": 170.0, "do": "drive_speed_stuck"}],
            ),
            end_reasons=frozenset({"operator_stop", "safety_verdict"}),
            rules=("tracking_error",),
            messages=("tracking_error",),
            silent=False,
            deadline=MANUAL_STOP_S + 15.0,
        ),
        FailureCase(
            name="drive_status_frozen_during_stop",
            category=Category.DRIVE,
            description="The drive's status freezes at speed (Ok, stale content), then the "
            "operator stops: the frozen 1344 rpm must be noticed during the descent. Neither "
            "the echo nor the shaft shows a write landing, so no write can be trusted: the "
            "runtime goes silent and the drive's own ttO stops it.",
            document=_manual(
                "drive_status_frozen_during_stop",
                [{"at_s": 170.0, "do": "drive_status_frozen"}],
            ),
            end_reasons=frozenset({"operator_stop", "safety_verdict"}),
            rules=("tracking_error",),
            messages=("tracking_error", "ttO"),
            silent=True,
            deadline=MANUAL_STOP_S + 15.0,
        ),
        FailureCase(
            name="drive_status_frozen_auto_hold",
            category=Category.DRIVE,
            description="The drive's status freezes in HOLD of the jog: caught as soon as the "
            "setpoint moves away from the frozen echo and RFRD (the control law's next "
            "correction, at the latest the cooldown); the runtime then goes silent.",
            document=_auto(
                "drive_status_frozen_auto_hold",
                [{"at_s": 500.0, "do": "drive_status_frozen"}],
                at=500.0,
            ),
            end_reasons=frozenset({"safety_verdict", "shutdown"}),
            rules=("tracking_error",),
            messages=("tracking_error", "ttO"),
            silent=True,
            at=500.0,
            phase="hold",
            deadline=AUTO_PHASES["cooldown"] - 30.0 + 15.0,
        ),
    ]
    return cases


def _ecg_cases() -> list[FailureCase]:
    cases: list[FailureCase] = []
    for phase, at in AUTO_PHASES.items():
        cases.append(
            FailureCase(
                name=f"ecg_disconnect_{phase}",
                category=Category.ECG,
                description=f"The BITalino link drops during {phase.upper()} and never returns.",
                document=_auto(
                    f"ecg_disconnect_{phase}", [{"at_s": at, "do": "bitalino_disconnect"}], at=at
                ),
                # In RECOVERY the shaft is already stopped: the stale verdicts are
                # shown, and the programme may run out before RAMP_DOWN is reached.
                end_reasons=VERDICT | {"programme_complete"} if phase == "recovery" else VERDICT,
                rules=("hr_stale",),
                messages=("hr_stale",),
                silent=False,
                at=at,
                phase=phase,
            )
        )
    cases += [
        FailureCase(
            name="ecg_connect_failure",
            category=Category.ECG,
            description="The BITalino never connects: BASELINE measures nothing, nothing turns.",
            document=_auto(
                "ecg_connect_failure",
                [],
                at=0.0,
                ecg={"connect_fails": True},
                expect={"max_output_rpm": 0.0},
            ),
            end_reasons=VERDICT,
            rules=("hr_stale",),
            messages=("none has ever arrived",),
            silent=False,
        ),
        FailureCase(
            name="ecg_connect_failure_dsp",
            category=Category.ECG,
            description="Real ECG path: the simulated BITalino refuses to connect.",
            document={
                "name": "ecg_connect_failure_dsp",
                "kind": "auto",
                "profile": FAILURE_PROFILE,
                "duration_s": 120.0,
                "ecg": {"mode": "dsp", "connect_fails": True},
                "expect": {"max_output_rpm": 0.0},
            },
            end_reasons=VERDICT,
            rules=("hr_stale",),
            messages=("none has ever arrived",),
            silent=False,
        ),
        FailureCase(
            name="ecg_frames_stop_silently",
            category=Category.ECG,
            description="Readings stop in HOLD while the link still reports itself up.",
            document=_auto(
                "ecg_frames_stop_silently", [{"at_s": 500.0, "do": "ecg_silent_stop"}], at=500.0
            ),
            end_reasons=VERDICT,
            rules=("hr_stale",),
            messages=("hr_stale",),
            silent=False,
            at=500.0,
            phase="hold",
        ),
        FailureCase(
            name="ecg_sequence_gap",
            category=Category.ECG,
            description="Seven frames lost in HOLD (seq jumps by 7): a gap, not a fault; the "
            "programme completes.",
            document=_auto(
                "ecg_sequence_gap",
                [{"at_s": 500.0, "do": "ecg_seq_gap", "gap": 7}],
                at=500.0,
                duration_s=AUTO_DURATION_S,
            ),
            end_reasons=frozenset({"programme_complete"}),
            silent=False,
            at=500.0,
            phase="hold",
        ),
        FailureCase(
            name="ecg_electrodes_off",
            category=Category.ECG,
            description="Electrodes off (no_signal) for 120 s in HOLD.",
            document=_auto(
                "ecg_electrodes_off",
                [{"at_s": 500.0, "do": "ecg_quality", "quality": "no_signal", "duration_s": 120.0}],
                at=500.0,
            ),
            end_reasons=VERDICT,
            rules=("hr_stale",),
            messages=("hr_stale",),
            silent=False,
            at=500.0,
            phase="hold",
        ),
    ]
    window = (DSP_AT_S, DSP_AT_S + DSP_FOR_S)
    dsp: Sequence[tuple[str, str, str | None]] = (
        ("flat", "a flat line", None),
        ("saturated", "a saturated signal", None),
        ("mains", "50 Hz mains hum", None),
        ("stopped", "frames that stop arriving", None),
        # Both were strict xfails (the DSP graded noise 'good': 116-146 bpm from random
        # counts; 47 then 135 bpm with 3 batches in 4 lost; each ending on a false
        # hr_drop). Fixed in raspberry-pi/src/ecg_pipeline.py: a rate is usable only
        # when the typed processor confirms it on the same unbroken window.
        ("corrupted", "frames that decode into noise", None),
        ("gaps", "three batches in four lost", None),
    )
    for signal, what, defect in dsp:
        name = f"ecg_dsp_{signal}"
        cases.append(
            FailureCase(
                name=name,
                category=Category.ECG,
                description=f"Real ECG path, WARMUP: {what} for 40 s. No false heart rate may "
                "be reported; the rate goes stale instead, its warning walks the arm to a "
                "standstill and the session ends there (ANH-176).",
                document=_dsp(name, signal),
                # One ending only since ANH-176. Forty seconds without a rate are
                # enough for hr_stale's REDUCE to bring the setpoint to zero, and a
                # stopped arm never restarts by itself: the session ends on the
                # latched session_standstill. "shutdown" was accepted here while
                # the arm restarted when the rate came back and the run simply
                # reached the end of the scenario; accepting it again would let
                # that restart come back unnoticed.
                end_reasons=VERDICT,
                rules=("hr_stale", "session_standstill"),
                messages=("hr_stale", "session_standstill"),
                silent=False,
                at=DSP_AT_S,
                phase="warmup",
                rate_window=window,
                known_defect=defect,
            )
        )
    return cases


def _process_cases() -> list[FailureCase]:
    cases: list[FailureCase] = []
    for phase, at in AUTO_PHASES.items():
        cases.append(
            FailureCase(
                name=f"process_sigterm_auto_{phase}",
                category=Category.PROCESS,
                description=f"SIGTERM during {phase.upper()}: emergency zero, link closed.",
                document=_auto(
                    f"process_sigterm_auto_{phase}", [{"at_s": at, "do": "shutdown"}], at=at
                ),
                end_reasons=frozenset({"shutdown"}),
                messages=("emergency zero",),
                silent=False,
                at=at,
                phase=phase,
            )
        )
    cases += [
        FailureCase(
            name=f"process_sigterm_manual_{stage}",
            category=Category.PROCESS,
            description=f"SIGTERM, manual session, {stage}.",
            document=_manual(f"process_sigterm_manual_{stage}", [{"at_s": at, "do": "shutdown"}]),
            end_reasons=frozenset({"shutdown", "operator_stop"}),
            messages=("emergency zero",),
            silent=False,
            at=at,
            phase=MANUAL_PHASE_NAMES[stage],
        )
        for stage, at in MANUAL_PHASES.items()
    ]
    for kind, at in (("auto_warmup", 200.0), ("auto_hold", 500.0), ("manual_ramp_up", 40.0)):
        action: Document = {"at_s": at, "do": "tick_exception"}
        name = f"process_tick_raises_{kind}"
        cases.append(
            FailureCase(
                name=name,
                category=Category.PROCESS,
                description=f"The control tick raises ({kind}): fail closed, go silent, exit.",
                document=_auto(name, [action], at=at)
                if kind.startswith("auto")
                else _manual(name, [action]),
                end_reasons=frozenset({"tick_exception"}),
                messages=("silent",),
                silent=True,
                at=at,
            )
        )
    for kind, at, stall, silent in (
        ("manual_5s", 170.0, 5.0, True),
        ("auto_hold_5s", 500.0, 5.0, True),
        ("manual_1s", 170.0, 1.4, False),
        ("auto_hold_2s", 500.0, 2.0, False),
    ):
        action = {"at_s": at, "do": "loop_stall", "duration_s": stall}
        name = f"process_loop_stall_{kind}"
        over = stall > 3.0  # noqa: PLR2004  # the simulator's ttO
        cases.append(
            FailureCase(
                name=name,
                category=Category.PROCESS,
                description=f"The event loop stalls {stall} s "
                f"({'over' if over else 'under'} the drive's 3 s ttO).",
                document=_auto(name, [action], at=at, duration_s=AUTO_DURATION_S)
                if kind.startswith("auto")
                else _manual(name, [action]),
                end_reasons=VERDICT
                if over
                else frozenset({"operator_stop", "programme_complete", "shutdown"}),
                rules=("loop_stall",),
                messages=(("SLF", "loop_stall") if over else ("loop_stall",)),
                silent=silent,
                at=at,
            )
        )
    for sign, jump in (("forward", 3600.0), ("backward", -3600.0)):
        name = f"process_wall_clock_jump_{sign}"
        cases.append(
            FailureCase(
                name=name,
                category=Category.PROCESS,
                description=f"The wall clock steps {jump:+.0f} s in HOLD (NTP on an RTC-less "
                "Pi): monotonic time rules, the programme completes untouched.",
                document=_auto(
                    name,
                    [{"at_s": 500.0, "do": "clock_jump", "jump_s": jump}],
                    at=500.0,
                    duration_s=AUTO_DURATION_S,
                    expect={"forbid_rules": ["loop_stall", "hr_stale", "comms_lost"]},
                ),
                end_reasons=frozenset({"programme_complete"}),
                silent=False,
                at=500.0,
                phase="hold",
            )
        )
    return cases


def _operator_cases() -> list[FailureCase]:
    return [
        FailureCase(
            name="operator_double_start_manual",
            category=Category.OPERATOR,
            description="START pressed again at 27 rpm: refused, the session carries on.",
            document=_manual(
                "operator_double_start_manual", [{"at_s": 170.0, "do": "start_again"}]
            ),
            end_reasons=frozenset({"operator_stop"}),
            messages=("demarrage refuse", "deja"),
            silent=False,
        ),
        FailureCase(
            name="operator_double_start_auto",
            category=Category.OPERATOR,
            description="START pressed again in HOLD of the jog: refused.",
            document=_auto(
                "operator_double_start_auto", [{"at_s": 500.0, "do": "start_again"}], at=500.0
            ),
            end_reasons=frozenset({"shutdown"}),
            messages=("demarrage refuse", "deja"),
            silent=False,
        ),
        FailureCase(
            name="operator_stop_while_idle",
            category=Category.OPERATOR,
            description="STOP, remote END and E-STOP pressed after the session finished: "
            "harmless, nothing moves.",
            document=_manual(
                "operator_stop_while_idle",
                [
                    {"at_s": 320.0, "do": "operator_stop"},
                    {"at_s": 321.0, "do": "remote_stop"},
                    {"at_s": 322.0, "do": "estop"},
                ],
                duration_s=340.0,
            ),
            end_reasons=frozenset({"operator_stop"}),
            messages=("operator_estop",),
            silent=False,
        ),
        FailureCase(
            name="operator_stop_after_refused_start",
            category=Category.OPERATOR,
            description="The start is refused (drive in OCF) and the operator presses STOP.",
            document={
                "name": "operator_stop_after_refused_start",
                "kind": "manual",
                "duration_s": 30.0,
                "drive": {"initial_fault": "OVERCURRENT"},
                "actions": [{"at_s": 5.0, "do": "operator_stop"}],
                "expect": {"start": "refused"},
            },
            end_reasons=frozenset({REFUSED}),
            messages=("variateur en defaut (OCF",),
            silent=False,
        ),
        FailureCase(
            name="operator_fault_reset_while_turning",
            category=Category.OPERATOR,
            description="OCF at 27 rpm, then FAULT RESET 2 s later while the arm still turns.",
            document=_manual(
                "operator_fault_reset_while_turning",
                [
                    {"at_s": 170.0, "do": "drive_fault", "fault": "OVERCURRENT"},
                    {"at_s": 172.0, "do": "fault_reset", "expect": "refused"},
                ],
            ),
            end_reasons=VERDICT,
            rules=("drive_fault",),
            messages=("reset refuse",),
            silent=False,
        ),
        FailureCase(
            name="operator_fault_reset_after_standstill",
            category=Category.OPERATOR,
            description="OLF (a resettable fault) at 27 rpm; once the arm is at rest and the "
            "verdict acknowledged, the named FAULT RESET is accepted (and nothing starts moving).",
            document=_manual(
                "operator_fault_reset_after_standstill",
                [
                    {"at_s": 170.0, "do": "drive_fault", "fault": "MOTOR_OVERLOAD"},
                    {"at_s": 320.0, "do": "acknowledge"},
                    {"at_s": 325.0, "do": "fault_reset", "expect": "accepted"},
                ],
                duration_s=360.0,
            ),
            end_reasons=VERDICT,
            rules=("drive_fault",),
            messages=(_fault_label(DriveFault.MOTOR_OVERLOAD),),
            silent=False,
        ),
        FailureCase(
            name="operator_fault_reset_forbidden",
            category=Category.OPERATOR,
            description="OCF (not resettable: a short or a jam) at 27 rpm; at rest and "
            "acknowledged, the FAULT RESET is still refused - power off and inspect.",
            document=_manual(
                "operator_fault_reset_forbidden",
                [
                    {"at_s": 170.0, "do": "drive_fault", "fault": "OVERCURRENT"},
                    {"at_s": 320.0, "do": "acknowledge"},
                    {"at_s": 325.0, "do": "fault_reset", "expect": "refused"},
                ],
                duration_s=360.0,
            ),
            end_reasons=VERDICT,
            rules=("drive_fault",),
            messages=(_fault_label(DriveFault.OVERCURRENT), "non rearmable"),
            silent=False,
        ),
        FailureCase(
            name="operator_restart_after_finish",
            category=Category.OPERATOR,
            description="After a finished session, START again: a NEW session is armed from "
            "scratch (accepted), nothing resumes by itself.",
            document=_manual(
                "operator_restart_after_finish",
                [{"at_s": 320.0, "do": "start_again", "expect": "accepted"}],
                duration_s=340.0,
            ),
            end_reasons=frozenset({"shutdown"}),
            messages=("emergency zero",),
            silent=False,
        ),
        FailureCase(
            name="operator_fault_reset_while_running",
            category=Category.OPERATOR,
            description="FAULT RESET at 27 rpm with no fault at all: refused.",
            document=_manual(
                "operator_fault_reset_while_running",
                [{"at_s": 170.0, "do": "fault_reset", "expect": "refused"}],
            ),
            end_reasons=frozenset({"operator_stop"}),
            messages=("reset refuse",),
            silent=False,
        ),
        *(
            FailureCase(
                name=f"operator_target_{label}",
                category=Category.OPERATOR,
                description=f"Target {rpm} output rpm at 100 s: refused, 27 rpm kept.",
                document=_manual(
                    f"operator_target_{label}",
                    [
                        {
                            "at_s": 100.0,
                            "do": "manual_target",
                            "output_rpm": rpm,
                            "expect": "refused",
                        }
                    ],
                    expect={"reaches_output_rpm": 27.0, "max_output_rpm": 27.05},
                ),
                end_reasons=frozenset({"operator_stop"}),
                messages=("consigne refusee",),
                silent=False,
            )
            for label, rpm in (
                ("above_nameplate", 32.0),
                ("in_the_gap", 0.5),
                ("negative", -5.0),
                ("absurd", 1000.0),
            )
        ),
    ]


def cases() -> tuple[FailureCase, ...]:
    """The whole matrix, in a stable order."""
    return (*_drive_cases(), *_ecg_cases(), *_process_cases(), *_operator_cases())


# =========================================================================
# Running and judging
# =========================================================================


def scenario_of(case: FailureCase) -> Scenario:
    """The case's document, through the scenario parser. Raises ``ValueError`` if invalid."""
    parsed = parse_scenario(json.dumps(case.document), where=case.name)
    if isinstance(parsed, Err):
        raise ValueError(parsed.error.detail)
    return parsed.value


async def run_case(case: FailureCase) -> RunResult:
    """Run one case. An exception escaping the harness propagates (and fails the caller)."""
    return await run_scenario(scenario_of(case))


def _check_end(case: FailureCase, result: RunResult) -> list[Violation]:
    end = REFUSED if result.start_refusal is not None else result.trace.final.end_reason
    if end in case.end_reasons:
        return []
    return [
        Violation("failure_end", None, f"ended {end}, expected one of {sorted(case.end_reasons)}")
    ]


def _check_output(case: FailureCase, result: RunResult) -> list[Violation]:
    final = result.trace.final
    found: list[Violation] = []
    if case.silent is not None and final.silent is not case.silent:
        found.append(
            Violation("failure_silent", None, f"silent={final.silent}, expected {case.silent}")
        )
    if final.silent:
        silent_at = next((row.t for row in result.trace.rows if row.silent), None)
        engaged = any(
            row.sim_state in WATCHDOG_STATES
            for row in result.trace.rows
            if silent_at is not None and row.t >= silent_at
        )
        if not engaged and final.sim_state not in WATCHDOG_STATES:
            found.append(Violation("failure_watchdog", None, "silent, but ttO never took over"))
    elif final.runtime_output_enabled:
        found.append(
            Violation("failure_output", None, "the runtime still holds the output enabled")
        )
    return found


def _check_messages(case: FailureCase, result: RunResult) -> list[Violation]:
    texts = [message.text for message in result.messages]
    return [
        Violation("failure_message", None, f"no operator message mentions {wanted!r}: {texts[:6]}")
        for wanted in case.messages
        if not any(wanted in text for text in texts)
    ]


def _check_rules(case: FailureCase, result: RunResult) -> list[Violation]:
    seen = (*rules_seen(result), *result.preroll.rules)
    found = [
        Violation("failure_rule", None, f"rule {rule} never fired (saw {seen})")
        for rule in case.rules
        if rule not in seen
    ]
    deadline = case.deadline
    if deadline is None:
        return found
    for rule in case.rules:
        first = next((row.t for row in result.trace.rows if row.safety_rule == rule), None)
        if first is not None and first > deadline:
            found.append(
                Violation(
                    "failure_late",
                    first,
                    f"rule {rule} first stood at {first:.1f} s, due by {deadline:.1f} s",
                )
            )
    return found


def _check_phase(case: FailureCase, result: RunResult) -> list[Violation]:
    if case.at is None or case.phase is None:
        return []
    at = case.at
    # The last tick BEFORE the injection: the injection itself may end the phase.
    row = next((row for row in reversed(result.trace.rows) if row.t < at - 1e-6), None)
    if row is None or row.phase != case.phase:
        seen = None if row is None else row.phase
        return [Violation("failure_phase", at, f"injected in {seen}, the case tests {case.phase}")]
    return []


def _check_rates(case: FailureCase, result: RunResult) -> list[Violation]:
    window = case.rate_window
    if window is None:
        return []
    start, end = window
    return [
        Violation(
            "false_heart_rate",
            row.t,
            f"a usable {row.hr_live} bpm reported while the truth is {row.hr_true} bpm",
        )
        for row in result.trace.rows
        if start <= row.t <= end
        and row.hr_live is not None
        and row.hr_true is not None
        and abs(row.hr_live - row.hr_true) > RATE_TOLERANCE
    ]


def _check_stopped(case: FailureCase, result: RunResult) -> list[Violation]:
    """``stopped_by``: the shaft at rest (or the stop handed to ttO) by then."""
    deadline = case.stopped_by
    if deadline is None:
        return []
    late = [
        row
        for row in result.trace.rows
        if row.t >= deadline and not row.silent and abs(row.measured_motor_rpm) >= 1
    ]
    if not late:
        return []
    row = late[0]
    return [
        Violation(
            "failure_still_turning",
            row.t,
            f"still {row.measured_motor_rpm} motor rpm, {row.state}, not silent",
        )
    ]


def _check_idle(result: RunResult) -> list[Violation]:
    """A drive found turning must not be left turning, unflagged, by the idle console."""
    if result.scenario.drive.initial_enabled_rpm is None or result.scenario.preroll <= 30.0:  # noqa: PLR2004
        return []
    preroll = result.preroll
    if preroll.final_motor_rpm == 0 or preroll.rules:
        return []
    return [
        Violation(
            "idle_left_turning",
            0.0,
            f"the idle console left the shaft at {preroll.final_motor_rpm} rpm with no verdict "
            f"for {result.scenario.preroll:.0f} s (peak {preroll.peak_motor_rpm})",
        )
    ]


def judge(case: FailureCase, result: RunResult) -> tuple[Violation, ...]:
    """Every invariant, the scenario's own expectations, and the failure-specific checks."""
    return (
        *check_invariants(result),
        *check_expectations(result),
        *_check_end(case, result),
        *_check_output(case, result),
        *_check_rules(case, result),
        *_check_messages(case, result),
        *_check_phase(case, result),
        *_check_rates(case, result),
        *_check_stopped(case, result),
        *_check_idle(result),
    )
