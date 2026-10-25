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
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum, unique
from itertools import pairwise
from typing import Final

from simulation.harness import TICK, RunResult, run_scenario
from simulation.invariants import Violation, check_expectations, check_invariants, rules_seen
from simulation.scenario import Scenario, StoVariant, parse_scenario
from simulation.tracefile import JsonValue, Row
from src.motor.drive import ControlWord, DriveFault, describe_fault
from src.motor.simulated import SimulatedDriveConfig, lft_code_for
from src.motor.sto import STO_POLL_PERIOD, StoState
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
    STO = "sto"


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
        # Under ttO the FREEZE latches and the session still ends as it would
        # have without the stall: a manual one by its own STOP, a programme by
        # its own end, since its cooldown is followed under the FREEZE.
        ends_itself = "programme_complete" if kind.startswith("auto") else "operator_stop"
        cases.append(
            FailureCase(
                name=name,
                category=Category.PROCESS,
                description=f"The event loop stalls {stall} s "
                f"({'over' if over else 'under'} the drive's 3 s ttO).",
                document=_auto(name, [action], at=at, duration_s=AUTO_DURATION_S)
                if kind.startswith("auto")
                else _manual(name, [action]),
                end_reasons=VERDICT if over else frozenset({ends_itself}),
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


def _sto_cases() -> list[FailureCase]:
    result: list[FailureCase] = []
    for kind, phases in (("auto", AUTO_PHASES), ("manual", MANUAL_PHASES)):
        for phase, at in phases.items():
            name = f"sto_open_{kind}_{phase}"
            actions: list[Document] = [{"at_s": at, "do": "sto_open"}]
            result.append(
                FailureCase(
                    name=name,
                    category=Category.STO,
                    description=f"STO opens during {kind} {phase}; torque is removed and latched.",
                    document=(
                        _auto(name, actions, at=at) if kind == "auto" else _manual(name, actions)
                    ),
                    end_reasons=(
                        frozenset({"operator_stop"})
                        if kind == "manual" and at > MANUAL_STOP_S
                        else VERDICT
                    ),
                    rules=("sto_open",),
                    messages=("sto_open (QUICK_STOP)",),
                    silent=False,
                    at=at,
                    phase=phase if kind == "auto" else MANUAL_PHASE_NAMES[phase],
                    deadline=at + STO_POLL_PERIOD + TICK,
                )
            )
    result.extend(
        (
            FailureCase(
                name="sto_idle_start_gate",
                category=Category.STO,
                description="At standstill, OPEN refuses START without a latch; READY permits it.",
                document=_manual(
                    "sto_idle_start_gate",
                    [
                        {"at_s": 1, "do": "operator_stop"},
                        {"at_s": 10, "do": "sto_open"},
                        {"at_s": 11, "do": "start_again", "expect": "refused"},
                        {"at_s": 12, "do": "sto_close"},
                        {"at_s": 13, "do": "start_again", "expect": "accepted"},
                        {"at_s": 14, "do": "operator_stop"},
                    ],
                    climb=False,
                    duration_s=25,
                ),
                end_reasons=frozenset({"operator_stop"}),
                messages=("STO",),
                silent=False,
                at=10,
                phase="done",
            ),
            FailureCase(
                name="sto_close_without_acknowledgement",
                category=Category.STO,
                description="Closing the contact never clears the latch or restarts the session.",
                document=_manual(
                    "sto_close_without_acknowledgement",
                    [
                        {"at_s": 170, "do": "sto_open"},
                        {"at_s": 171, "do": "sto_close"},
                        {"at_s": 172, "do": "start_again", "expect": "refused"},
                        {"at_s": 329, "do": "start_again", "expect": "refused"},
                    ],
                ),
                end_reasons=VERDICT,
                rules=("sto_open",),
                messages=("sto_open (QUICK_STOP)",),
                silent=False,
                at=170,
                phase="hold",
                deadline=170 + STO_POLL_PERIOD + TICK,
            ),
            FailureCase(
                name="sto_acknowledgement_then_new_start",
                category=Category.STO,
                description="Acknowledgement requires READY, followed by a new START.",
                document=_manual(
                    "sto_acknowledgement_then_new_start",
                    [
                        {"at_s": 40, "do": "sto_open"},
                        {"at_s": 41, "do": "acknowledge", "expect": "refused"},
                        {"at_s": 42, "do": "start_again", "expect": "refused"},
                        {"at_s": 43, "do": "sto_close"},
                        {"at_s": 44, "do": "start_again", "expect": "refused"},
                        {"at_s": 190, "do": "acknowledge", "expect": "accepted"},
                        {"at_s": 210, "do": "start_again", "expect": "accepted"},
                        {"at_s": 211, "do": "manual_target", "output_rpm": 3},
                        {"at_s": 240, "do": "operator_stop"},
                    ],
                ),
                end_reasons=frozenset({"operator_stop"}),
                rules=("sto_open",),
                messages=("sto_open (QUICK_STOP)",),
                silent=False,
                at=40,
                phase="hold",
                deadline=40 + STO_POLL_PERIOD + TICK,
            ),
        )
    )
    return result


# -- the STO chain read as the console reads it (``sto_source: drive_input``) --

BENCH_RPM: Final[float] = 3.0
"""The bench speed of the drive_input stop cases, output rpm (149 motor rpm): the
coast after an opening ends inside the case, and the reads that resume are judged."""

DRIVE_INPUT_STOP_S: Final[float] = 60.0
"""When a drive_input bench case asks for its stop (STOP, target zero, a drive fault)."""

OPEN_INTO_STOP_S: Final[float] = 2.1
"""How long after its stop began the chain opens, between two ticks; closed as long after.

Between two ticks, as a contact opens: the tick that follows is the first to see it,
and an instant off the tick grid stays off it however long the case runs."""

READ_LATCH_S: Final[float] = STO_POLL_PERIOD + 2 * TICK
"""The longest an opening seen by a reading takes to latch: the read the console's
cadence (one every three ticks) starts after it, then the tick that judges it."""


def _with_reading(document: Document) -> Document:
    """The same document, with the chain read through register 5202."""
    return {**document, "sto_source": StoVariant.DRIVE_INPUT.value}


def _bench(name: str, actions: Sequence[Document], *, duration: float) -> Document:
    """The bench session to :data:`BENCH_RPM`, its chain read through register 5202."""
    return _with_reading(
        {
            "name": name,
            "kind": "manual",
            "duration_s": duration,
            "actions": [{"at_s": 2.0, "do": "manual_target", "output_rpm": BENCH_RPM}, *actions],
        }
    )


def _programme(name: str, actions: Sequence[Document], *, duration: float) -> Document:
    """The 13-minute jog, its chain read through register 5202."""
    return _with_reading(
        {
            "name": name,
            "kind": "auto",
            "profile": FAILURE_PROFILE,
            "duration_s": duration,
            "actions": list(actions),
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _Stop:
    """One category of stop, the chain opening :data:`OPEN_INTO_STOP_S` into it."""

    name: str
    kind: str
    stop: tuple[Document, ...]
    opens_at: float
    phase: str
    end_reasons: frozenset[str]
    after: float
    """When the stop is over and the reads have resumed: the requests that must be refused."""

    duration: float
    category: Category = Category.STO
    rules: tuple[str, ...] = ("sto_open",)
    deadline: float = TICK
    """After the opening, by when ``sto_open`` must stand."""


DRIVE_INPUT_STOPS: Final[tuple[_Stop, ...]] = (
    _Stop(
        name="operator_stop",
        kind="manual",
        stop=({"at_s": DRIVE_INPUT_STOP_S, "do": "operator_stop"},),
        opens_at=DRIVE_INPUT_STOP_S + OPEN_INTO_STOP_S,
        phase="cooldown",
        end_reasons=frozenset({"operator_stop"}),
        after=200.0,
        duration=240.0,
    ),
    _Stop(
        # The only end of a programme that is a stop: a programme run to the end
        # of its timeline opens no ending, its own descent is regulation.
        name="manual_session_limit",
        kind="manual",
        stop=(),
        opens_at=3600.0 + OPEN_INTO_STOP_S,
        phase="cooldown",
        end_reasons=frozenset({"programme_complete"}),
        after=3740.0,
        duration=3780.0,
    ),
    _Stop(
        name="manual_target_zero",
        kind="manual",
        stop=({"at_s": DRIVE_INPUT_STOP_S, "do": "manual_target", "output_rpm": 0.0},),
        opens_at=DRIVE_INPUT_STOP_S + OPEN_INTO_STOP_S,
        phase="hold",
        end_reasons=VERDICT,
        after=200.0,
        duration=240.0,
    ),
    _Stop(
        # attendant_absent: FREEZE after 60 s with no ping, RAMP_DOWN (latched) after 120 s.
        name="ramp_down_verdict",
        kind="manual",
        stop=({"at_s": 40.0, "do": "attendant_leaves"},),
        opens_at=160.0 + OPEN_INTO_STOP_S,
        phase="cooldown",
        end_reasons=VERDICT,
        after=300.0,
        duration=340.0,
    ),
    _Stop(
        # A faulted drive is under no stop of this console's: its coast is read.
        name="faulted_drive_coast",
        kind="manual",
        stop=({"at_s": DRIVE_INPUT_STOP_S, "do": "drive_fault", "fault": "MOTOR_OVERLOAD"},),
        opens_at=DRIVE_INPUT_STOP_S + OPEN_INTO_STOP_S,
        phase="cooldown",
        end_reasons=VERDICT,
        after=200.0,
        duration=240.0,
        category=Category.DRIVE,
        rules=("drive_fault", "sto_open"),
        deadline=READ_LATCH_S,
    ),
    _Stop(
        # loop_stall latches a FREEZE in HOLD; from COOLDOWN (660 s) it walks the arm down.
        name="freeze_descent",
        kind="auto",
        stop=({"at_s": 500.0, "do": "loop_stall", "duration_s": 1.4},),
        opens_at=660.0 + OPEN_INTO_STOP_S,
        phase="cooldown",
        end_reasons=VERDICT,
        after=830.0,
        duration=860.0,
    ),
)
"""Every category of stop: no 5202 read is taken while one is under way."""


def _stop_case(stop: _Stop, *, closed: bool) -> FailureCase:
    shape = "closed_again" if closed else "left_open"
    name = f"sto_drive_input_{stop.name}_{shape}"
    at = stop.opens_at
    contact: list[Document] = [{"at_s": at, "do": "sto_open"}]
    if closed:
        contact.append({"at_s": at + OPEN_INTO_STOP_S, "do": "sto_close"})
        after: list[Document] = [{"at_s": stop.after, "do": "start_again", "expect": "refused"}]
    else:
        after = [
            {"at_s": stop.after, "do": "acknowledge", "expect": "refused"},
            {"at_s": stop.after + 1.0, "do": "start_again", "expect": "refused"},
        ]
    actions = [*stop.stop, *contact, *after]
    document = (
        _bench(name, actions, duration=stop.duration)
        if stop.kind == "manual"
        else _programme(name, actions, duration=stop.duration)
    )
    return FailureCase(
        name=name,
        category=stop.category,
        description=(
            f"drive_input: the chain opens during a stop ({stop.name}) and is "
            f"{'closed again before it ends' if closed else 'left open'}; no 5202 read during "
            "the stop, sto_open latched, nothing restarts unacknowledged."
        ),
        document=document,
        end_reasons=stop.end_reasons,
        rules=stop.rules,
        messages=("sto_open (QUICK_STOP)",),
        silent=False,
        at=at,
        phase=stop.phase,
        deadline=at + stop.deadline,
    )


def _drive_input_cases() -> list[FailureCase]:
    result = [
        _stop_case(stop, closed=closed) for stop in DRIVE_INPUT_STOPS for closed in (False, True)
    ]
    brief: tuple[tuple[str, str, float, float, float], ...] = (
        # name, kind, opening (between two ticks), when to try a start, duration
        ("manual", "manual", 60.05, 200.0, 240.0),
        ("programme", "auto", 500.05, 700.0, 740.0),
    )
    for label, kind, at, later, duration in brief:
        name = f"sto_drive_input_brief_opening_{label}"
        actions: list[Document] = [
            {"at_s": at, "do": "sto_open"},
            {"at_s": at + 0.1, "do": "sto_close"},
            {"at_s": later, "do": "start_again", "expect": "refused"},
        ]
        result.append(
            FailureCase(
                name=name,
                category=Category.STO,
                description=(
                    f"drive_input: a 0.1 s opening between two ticks of a {label}, outside any "
                    "stop, never read: latched from the drive's state at the next tick."
                ),
                document=(
                    _bench(name, actions, duration=duration)
                    if kind == "manual"
                    else _programme(name, actions, duration=duration)
                ),
                end_reasons=VERDICT,
                rules=("sto_open",),
                messages=("sto_open (QUICK_STOP)",),
                silent=False,
                at=at,
                phase="hold",
                deadline=at + TICK,
            )
        )
    for phase, at in (("cooldown", 690.1), ("recovery", 725.1)):
        # A programme's own descent is regulation, not a stop: the reads go on.
        name = f"sto_drive_input_programme_descent_{phase}"
        actions = [
            {"at_s": at, "do": "sto_open"},
            {"at_s": 850.0, "do": "acknowledge", "expect": "refused"},
            {"at_s": 851.0, "do": "start_again", "expect": "refused"},
        ]
        result.append(
            FailureCase(
                name=name,
                category=Category.STO,
                description=(
                    f"drive_input: the chain opens during the programme's planned {phase}, "
                    "which is no stop (the reads go on): sto_open latched at the next tick."
                ),
                document=_programme(name, actions, duration=880.0),
                end_reasons=VERDICT,
                rules=("sto_open",),
                messages=("sto_open (QUICK_STOP)",),
                silent=False,
                at=at,
                phase=phase,
                deadline=at + TICK,
            )
        )
    existing = {case.name: case for case in _sto_cases()}
    for name in (
        "sto_close_without_acknowledgement",
        "sto_acknowledgement_then_new_start",
        "sto_idle_start_gate",
    ):
        case = existing[name]
        reading = f"sto_drive_input_{name.removeprefix('sto_')}"
        result.append(
            replace(
                case,
                name=reading,
                description=f"drive_input: {case.description}",
                document=_with_reading({**case.document, "name": reading}),
            )
        )
    return result


def cases() -> tuple[FailureCase, ...]:
    """The whole matrix, in a stable order."""
    return (
        *_drive_cases(),
        *_ecg_cases(),
        *_process_cases(),
        *_operator_cases(),
        *_sto_cases(),
        *_drive_input_cases(),
    )


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


def _check_sto(case: FailureCase, result: RunResult) -> list[Violation]:
    if case.category is not Category.STO:
        return []
    at = case.at
    if at is None:
        return [Violation("sto_evidence", None, "missing contact injection instant")]
    rows = [row for row in result.trace.rows if row.t >= at - 1e-6]
    observations = [view for view in result.sto if view.at >= at - 1e-6]
    if not rows or not observations:
        return [Violation("sto_evidence", at, "no post-injection observation")]
    if not case.rules:
        opened = [view for view in observations if view.state is StoState.OPEN]
        refused = [request for request in result.requests if not request.accepted]
        accepted = [request for request in result.requests if request.accepted]
        if (
            not opened
            or any(view.latched for view in opened)
            or not any("StoNotReady" in request.detail for request in refused)
            or not accepted
        ):
            return [
                Violation(
                    "sto_idle_gate", at, "OPEN must refuse START without a latch; READY permits it"
                )
            ]
        return []
    restart = next(
        (
            request.at
            for request in result.requests
            if request.request == "start" and request.accepted
        ),
        float("inf"),
    )
    acknowledge = next(
        (
            request.at
            for request in result.requests
            if request.request == "acknowledge" and request.accepted
        ),
        float("inf"),
    )
    found: list[Violation] = []
    locked = [view for view in observations if view.at < acknowledge - 1e-6]
    if not locked or any(not view.latched or view.end_reason is None for view in locked):
        found.append(
            Violation("sto_latch", at, "OPEN must latch and end the session until acknowledgement")
        )
    stopped = [row for row in rows if row.t < restart - 1e-6]
    if any(
        row.setpoint_motor_rpm != 0
        or row.lfrd_motor_rpm != 0
        or row.sim_state in {"OPERATION_ENABLED", "FAULT_REACTION_RAMP_STOP", "DISABLING_ON_RAMP"}
        or (
            (row.safety_action != "QUICK_STOP" or row.safety_rule != "sto_open")
            and row.t < acknowledge - 1e-6
        )
        for row in stopped
    ):
        found.append(
            Violation("sto_output", at, "zero reference and no torque required before a new START")
        )
    found.extend(_check_sto_frames(result, at, restart))
    first = rows[0]
    if first.sim_state != "SWITCH_ON_DISABLED" or first.current_a != 0:
        found.append(
            Violation("sto_torque", first.t, "STO must disable the output stage with zero current")
        )
    turning = [row for row in stopped if row.measured_motor_rpm > 1]
    tau = SimulatedDriveConfig().tau_coast
    if any(
        abs(
            after.measured_motor_rpm
            - before.measured_motor_rpm * math.exp(-(after.t - before.t) / tau)
        )
        > 1
        for before, after in pairwise(turning)
    ):
        found.append(
            Violation(
                "sto_coast", at, "the turning shaft must follow the simulator's freewheel decay"
            )
        )
    return found


def _check_sto_frames(result: RunResult, at: float, restart: float) -> list[Violation]:
    found: list[Violation] = []
    zeroed = False
    prior = next(
        (row.setpoint_motor_rpm for row in reversed(result.trace.rows) if row.t < at - 1e-6),
        0,
    )
    detection = next(row.t for row in result.trace.rows if row.t >= at - 1e-6)
    for frame in result.trace.frames:
        stamp = frame["t"]
        if not isinstance(stamp, float | int) or not at <= stamp < restart:
            continue
        value = frame["value"]
        if (
            not zeroed
            and frame["kind"] == "speed"
            and value != 0
            and (not isinstance(value, int) or value > prior or stamp > detection + 1e-6)
        ):
            found.append(
                Violation(
                    "sto_keepalive", stamp, "only the previous reference may precede detection"
                )
            )
        if frame["kind"] in {"speed", "emergency_zero"} and frame["value"] == 0:
            if not zeroed and stamp > detection + 1e-6:
                found.append(
                    Violation("sto_zero", stamp, "zero reference missed the detection tick")
                )
            zeroed = True
        if (zeroed and frame["kind"] == "speed" and frame["value"] != 0) or (
            frame["kind"] == "command" and frame["value"] == ControlWord.ENABLE_OPERATION.value
        ):
            found.append(
                Violation("sto_reactivation", stamp, "drive reactivated before a new START")
            )
    if not zeroed:
        found.append(Violation("sto_zero", at, "no zero reference was sent after STO detection"))
    return found


STOPPING_ACTIONS: Final[frozenset[str]] = frozenset({"RAMP_DOWN", "QUICK_STOP", "GO_SILENT"})
PAST_LAST_SPEED: Final[frozenset[str]] = frozenset({"cooldown", "recovery", "done"})


def stop_under_way(row: Row) -> bool:
    """A stop the trace shows under way after this tick, read from its own columns.

    The stops of ``docs/raspberry-pi.md`` 2.2, apart from the runtime's code:
    an ending that walks the setpoint down or still holds the run command; a
    running session under a verdict that stops it (RAMP_DOWN or worse), under
    a FREEZE walking an arm down that nobody wants turning any more (a
    programme past its last speed, a manual target of zero), or with a manual
    target of zero walked down; the run command held outside a session. A
    drive that shows a fault is under none: its coast is read.
    """
    if row.drive_state == "FAULT":
        return False
    turning = row.setpoint_motor_rpm != 0
    target_zero = row.mode == "manuel" and row.manual_target_motor_rpm == 0
    if row.state == "ending":
        return turning or row.output_enabled
    if row.state == "running":
        walked_down = target_zero or (
            row.safety_action == "FREEZE" and row.phase in PAST_LAST_SPEED
        )
        return row.safety_action in STOPPING_ACTIONS or (turning and walked_down)
    return row.output_enabled


def _check_drive_input(case: FailureCase, result: RunResult) -> list[Violation]:
    """``sto_source: drive_input``: the chain is read, never during a stop, and a latch holds.

    Each observation counts the 5202 frames that reached the simulated drive
    in the gap after its tick; the row of that tick says whether a stop was
    under way (:func:`stop_under_way`). Once ``sto_open`` stands it stands,
    with a zero reference, until an acknowledgement is accepted.
    """
    if result.scenario.sto is not StoVariant.DRIVE_INPUT:
        return []
    found: list[Violation] = []
    rows = result.trace.rows
    if not any(view.reads for view in result.sto):
        found.append(Violation("sto_reads", None, "register 5202 was never read"))
    found.extend(
        Violation(
            "sto_read_in_stop",
            row.t,
            f"a 5202 read after a tick under a stop ({row.state}, {row.safety_action})",
        )
        for row, view in zip(rows, result.sto, strict=False)
        if view.reads and stop_under_way(row)
    )
    first = next((row.t for row in rows if row.safety_rule == "sto_open"), None)
    if first is None or "sto_open" not in case.rules:
        return found
    acknowledge = next(
        (
            request.at
            for request in result.requests
            if request.request == "acknowledge" and request.accepted
        ),
        float("inf"),
    )
    held = [row for row in rows if first <= row.t < acknowledge - 1e-6]
    if any(row.safety_rule != "sto_open" or row.setpoint_motor_rpm != 0 for row in held):
        found.append(
            Violation("sto_latch_held", first, "sto_open must stand, at zero, until acknowledged")
        )
    return found


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
        *_check_sto(case, result),
        *_check_drive_input(case, result),
    )
