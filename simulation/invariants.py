"""The physical invariants every recorded run must satisfy, and the scenario's own expectations.

Two kinds of check, kept apart on purpose:

* :func:`check_invariants` - true of EVERY run, whatever the scenario: the
  setpoint domain, the rate limits the runtime promises, safety dominating the
  control law, no acceleration on a fainting rider, every exit path leaving the
  motor stopped with no torque, and nothing but ``{0} U [min_run, ceiling]``
  ever written to the drive.
* :func:`check_expectations` - what THIS scenario says must happen (a refusal,
  an end reason, a rule firing, a speed reached).

:func:`measure` computes the numbers a report quotes (peak speed, peak g at the
seat and at the leg tip, peak slew and g-dot, time in zone). Some of those are
compared against the anti-nausea limits *advisorily*: see ``Advisory``.

A violation is data, not an exception: a test asserts the tuple is empty and
prints every entry when it is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, override

from simulation.harness import PREROLL, TICK, RunResult
from simulation.recording import FrameKind
from simulation.scenario import DriveReverse, Expectation, SessionKind
from simulation.tracefile import Row
from src.training.motion import motor_rate_limit
from src.training.types import Phase, SafetyAction
from src.units import MotorRpm, OutputRpm, output_rpm_to_g

SLACK_RPM: Final[int] = 1
"""One carried rpm per step window, as ``src.training.motion.carry_after`` documents."""

DRIVE_RAMP_SLACK: Final[float] = 1.05
"""5 % over the simulator's own ramp rate before a measured change counts as unphysical."""

VASOVAGAL_GUARD: Final[float] = 60.0
"""Seconds after a scripted vasovagal window during which the setpoint must not rise."""

HOLDING_ACTIONS: Final[frozenset[str]] = frozenset(
    action.name for action in SafetyAction if action >= SafetyAction.FREEZE
)
EMERGENCY_ACTIONS: Final[frozenset[str]] = frozenset(
    {SafetyAction.QUICK_STOP.name, SafetyAction.GO_SILENT.name}
)
ENDED_STATES: Final[frozenset[str]] = frozenset({"ending", "finished"})


@dataclass(frozen=True, slots=True)
class Violation:
    """One broken invariant or unmet expectation."""

    check: str
    t: float | None
    detail: str

    @override
    def __str__(self) -> str:
        where = "" if self.t is None else f" at t={self.t:.1f} s"
        return f"[{self.check}]{where}: {self.detail}"


@dataclass(frozen=True, slots=True, kw_only=True)
class Metrics:
    """What a report quotes about one run."""

    peak_output_rpm: float
    peak_motor_rpm: int
    peak_hertz: float
    peak_g_reference: float
    peak_g_leg_tip: float
    peak_setpoint_rate_output_rpm_s: float
    peak_arm_accel_output_rpm_s: float
    peak_g_rate_reference: float
    peak_g_rate_leg_tip: float
    in_zone_fraction: float | None
    rules: tuple[str, ...]
    end_reason: str | None


def _bounds(result: RunResult) -> tuple[int, int]:
    """``(min_run, ceiling)`` of the setpoint domain for this run, motor rpm."""
    profile = result.profile
    if profile is not None:
        return (int(profile.min_run_rpm), int(profile.max_rpm))
    ceiling = result.manual_ceiling
    return (int(result.motion.min_run), 0 if ceiling is None else int(ceiling))


def _check_numbers(rows: Sequence[Row]) -> list[Violation]:
    found: list[Violation] = []
    for row in rows:
        values = (
            row.output_rpm,
            row.hertz,
            row.g_reference,
            row.g_leg_tip,
            row.setpoint_output_rpm,
            row.setpoint_g_leg_tip,
        )
        if not all(math.isfinite(value) for value in values):
            found.append(Violation("finite", row.t, f"non-finite value in {values}"))
    return found


def _check_domain(result: RunResult) -> list[Violation]:
    low, high = _bounds(result)
    hsp = int(result.scenario.drive.hsp_motor_rpm)
    reverse_allowed = any(isinstance(a, DriveReverse) for a in result.scenario.actions)
    found: list[Violation] = []
    for row in result.trace.rows:
        sp = row.setpoint_motor_rpm
        if sp != 0 and not low <= sp <= high:
            found.append(
                Violation("setpoint_domain", row.t, f"{sp} not in {{0}} U [{low}, {high}]")
            )
        if abs(row.measured_motor_rpm) > hsp + SLACK_RPM:
            found.append(
                Violation("measured_bound", row.t, f"|{row.measured_motor_rpm}| > HSP {hsp}")
            )
        if row.measured_motor_rpm < -SLACK_RPM and not reverse_allowed:
            found.append(Violation("reverse", row.t, f"shaft at {row.measured_motor_rpm} rpm"))
    for frame in result.trace.frames:
        if frame.get("kind") != FrameKind.SPEED.value:
            continue
        value = frame.get("value")
        if not isinstance(value, int) or (value != 0 and not low <= value <= high):
            found.append(
                Violation("written_domain", None, f"LFRD written {value}, domain [{low}, {high}]")
            )
    return found


def _allowed_rise(result: RunResult, before: int, after: int, window: float) -> float:
    """The largest rise the runtime promises over ``window`` from ``before`` (motor rpm)."""
    low, _ = _bounds(result)
    if result.scenario.kind is SessionKind.MANUAL:
        rate = motor_rate_limit(MotorRpm(max(before, after)), result.motion, result.rig.machine)
        window = min(window, float(result.motion.max_interval) + 1.0 / rate)
        passage = low if before == 0 else 0
        return max(passage, rate * window + SLACK_RPM)
    slew = float(result.runtime_limits.slew)
    passage = low + int(result.runtime_limits.start_hysteresis_rpm) if before == 0 else 0
    return max(passage + slew * TICK, slew * window + SLACK_RPM)


def _check_slew(result: RunResult) -> list[Violation]:
    """Rises bounded by the promised rate; falls bounded too, except the emergency zero."""
    rows = result.trace.rows
    found: list[Violation] = []
    last_change = 0
    for index, (a, b) in enumerate(pairwise(rows), start=1):
        before, after = a.setpoint_motor_rpm, b.setpoint_motor_rpm
        if before == after:
            continue
        window = (index - last_change) * float(TICK)
        last_change = index
        if after > before:
            allowed = _allowed_rise(result, before, after, window)
            if after - before > allowed + 1e-9:
                found.append(
                    Violation(
                        "slew_up", b.t, f"{before} -> {after} in {window:.1f} s (max {allowed:.1f})"
                    )
                )
            continue
        if b.safety_action in EMERGENCY_ACTIONS or after == 0:
            continue
        allowed = _allowed_rise(result, after, before, window)
        if before - after > allowed + 1e-9:
            found.append(
                Violation(
                    "slew_down", b.t, f"{before} -> {after} in {window:.1f} s (max {allowed:.1f})"
                )
            )
    return found


def _check_measured_rate(result: RunResult) -> list[Violation]:
    """The shaft cannot beat the drive's own ramp: a sanity check on the trace itself."""
    ramp = result.scenario.drive.hsp_motor_rpm / float(result.scenario.drive.acceleration_time)
    nominal = 1380.0 / float(result.scenario.drive.acceleration_time)
    limit = max(ramp, nominal) * DRIVE_RAMP_SLACK
    found: list[Violation] = []
    for a, b in pairwise(result.trace.rows):
        if not (a.measured_fresh and b.measured_fresh):
            continue
        rate = abs(b.measured_motor_rpm - a.measured_motor_rpm) / (b.t - a.t)
        if rate > limit + SLACK_RPM / (b.t - a.t):
            found.append(
                Violation("measured_rate", b.t, f"{rate:.0f} rpm/s > drive ramp {limit:.0f}")
            )
    return found


def _check_safety_dominates(rows: Sequence[Row]) -> list[Violation]:
    """While any verdict at FREEZE or above stands, the setpoint never rises."""
    return [
        Violation(
            "safety_dominates",
            b.t,
            f"{b.safety_rule} {b.safety_action}: {a.setpoint_motor_rpm} -> {b.setpoint_motor_rpm}",
        )
        for a, b in pairwise(rows)
        if b.safety_action in HOLDING_ACTIONS and b.setpoint_motor_rpm > a.setpoint_motor_rpm
    ]


def _check_no_rise_after_end(rows: Sequence[Row]) -> list[Violation]:
    """Once an ending has begun, nothing speeds the machine up again."""
    return [
        Violation("rise_after_end", b.t, f"{a.setpoint_motor_rpm} -> {b.setpoint_motor_rpm}")
        for a, b in pairwise(rows)
        if a.state in ENDED_STATES and b.setpoint_motor_rpm > a.setpoint_motor_rpm
    ]


def _check_vasovagal(result: RunResult) -> list[Violation]:
    """During a scripted heart-rate collapse (and a minute after), the setpoint never rises."""
    windows = [w for w in result.scenario.events if w.event.value == "vasovagal_drop"]
    if not windows:
        return []
    # Event windows are timed from the plant's origin, which is PREROLL before t = 0.
    found: list[Violation] = []
    for window in windows:
        start = float(window.start) - float(PREROLL)
        end = float(window.end) - float(PREROLL) + VASOVAGAL_GUARD
        for a, b in pairwise(result.trace.rows):
            if start <= b.t <= end and b.setpoint_motor_rpm > a.setpoint_motor_rpm:
                found.append(
                    Violation(
                        "vasovagal_no_accel",
                        b.t,
                        f"setpoint rose {a.setpoint_motor_rpm} -> {b.setpoint_motor_rpm} "
                        f"while the heart rate was collapsing (true {b.hr_true} bpm)",
                    )
                )
    return found


def _check_manual_g_rate(result: RunResult) -> list[Violation]:
    """A manual session's g-dot, at the reference radius, stays inside the motion limit."""
    if result.scenario.kind is not SessionKind.MANUAL:
        return []
    limit = float(result.motion.g_rate)
    radius = result.rig.reference_radius
    ratio = float(result.rig.machine.ratio)
    found: list[Violation] = []
    rows = result.trace.rows
    last_change = 0
    for index, (a, b) in enumerate(pairwise(rows), start=1):
        if a.setpoint_motor_rpm == b.setpoint_motor_rpm or 0 in (
            a.setpoint_motor_rpm,
            b.setpoint_motor_rpm,
        ):
            if a.setpoint_motor_rpm != b.setpoint_motor_rpm:
                last_change = index
            continue
        window = (index - last_change) * float(TICK)
        last_change = index
        if b.safety_action in EMERGENCY_ACTIONS:
            continue
        g_a = output_rpm_to_g(OutputRpm(a.setpoint_motor_rpm / ratio), radius)
        g_b = output_rpm_to_g(OutputRpm(b.setpoint_motor_rpm / ratio), radius)
        # One carried rpm is allowed per window, as for the speed itself.
        slack = abs(
            output_rpm_to_g(OutputRpm((b.setpoint_motor_rpm + SLACK_RPM) / ratio), radius) - g_b
        )
        window = min(window, float(result.motion.max_interval) + 1.0)
        if abs(g_b - g_a) > limit * window + slack + 1e-9:
            found.append(
                Violation(
                    "g_rate", b.t, f"dGc {abs(g_b - g_a):.4f} g in {window:.1f} s > {limit} g/s"
                )
            )
    return found


def _check_exit(result: RunResult) -> list[Violation]:
    """Every exit path: shaft stopped, no torque, the reference zero where it could be written."""
    final = result.trace.final
    found: list[Violation] = []
    if final.energised:
        found.append(Violation("exit_energised", None, f"drive left in {final.sim_state}"))
    if final.shaft_motor_rpm is None:
        found.append(Violation("exit_unknown", None, "the drive never answered after teardown"))
    elif abs(final.shaft_motor_rpm) >= 1:
        found.append(Violation("exit_turning", None, f"shaft at {final.shaft_motor_rpm} rpm"))
    if not final.silent and final.lfrd_motor_rpm != 0:
        found.append(Violation("exit_reference", None, f"LFRD left at {final.lfrd_motor_rpm}"))
    if final.runtime_applied_rpm != 0 and not final.silent:
        found.append(
            Violation(
                "exit_belief", None, f"runtime believes it commands {final.runtime_applied_rpm}"
            )
        )
    return found


def _check_silence(result: RunResult) -> list[Violation]:
    """After the runtime goes silent it sends nothing, reads included, until it exits."""
    rows = result.trace.rows
    silent_at = next((row.t for row in rows if row.silent), None)
    if silent_at is None:
        return []
    # Frames sent INSIDE the tick that went silent carry that tick's instant
    # (give or take float rounding); silence is judged from the next tick on.
    silent_at += float(TICK) / 2
    last_row = rows[-1].t
    found: list[Violation] = []
    for frame in result.trace.frames:
        t = frame.get("t")
        if not isinstance(t, float | int) or not silent_at < t <= last_row:
            continue
        found.append(Violation("silent_wrote", float(t), f"{frame.get('kind')} after going silent"))
    return found


def _check_refused_start(result: RunResult) -> list[Violation]:
    """A refused start never set the machine turning."""
    if result.start_refusal is None:
        return []
    moved = [
        frame
        for frame in result.trace.frames
        if frame.get("kind") == FrameKind.SPEED.value and frame.get("value") not in (0, None)
    ]
    rows = [row for row in result.trace.rows if row.setpoint_motor_rpm != 0]
    if moved or rows:
        return [Violation("refused_moved", None, f"refused start still wrote {moved[:3]}")]
    return []


def check_invariants(result: RunResult) -> tuple[Violation, ...]:
    """Every invariant, over the whole trace. Empty means the run is physically sound."""
    rows = result.trace.rows
    return (
        *_check_numbers(rows),
        *_check_domain(result),
        *_check_slew(result),
        *_check_measured_rate(result),
        *_check_safety_dominates(rows),
        *_check_no_rise_after_end(rows),
        *_check_vasovagal(result),
        *_check_manual_g_rate(result),
        *(check_motion_limits(result) if result.scenario.kind is SessionKind.MANUAL else ()),
        *_check_exit(result),
        *_check_silence(result),
        *_check_refused_start(result),
    )


# =========================================================================
# The scenario's own expectations
# =========================================================================


def rules_seen(result: RunResult) -> tuple[str, ...]:
    """Every safety rule that stood at some tick, in first-seen order."""
    seen: list[str] = []
    for row in result.trace.rows:
        if row.safety_rule is not None and row.safety_rule not in seen:
            seen.append(row.safety_rule)
    return tuple(seen)


def in_zone_fraction(result: RunResult) -> float | None:
    """Share of HOLD ticks with a live heart rate inside the programme's zone."""
    profile = result.profile
    if profile is None:
        return None
    hold = [row for row in result.trace.rows if row.phase == Phase.HOLD.value and row.hr_live]
    if not hold:
        return None
    inside = [
        row
        for row in hold
        if row.hr_live is not None and profile.zone_low_bpm <= row.hr_live <= profile.zone_high_bpm
    ]
    return len(inside) / len(hold)


def check_expectations(result: RunResult) -> tuple[Violation, ...]:  # noqa: PLR0912  # one arm per key
    """What the scenario file says must be true of this run."""
    expect = result.scenario.expect
    final = result.trace.final
    found: list[Violation] = []
    started = result.start_refusal is None
    if expect.start is Expectation.ACCEPTED and not started:
        found.append(Violation("expect_start", 0.0, f"start refused: {result.start_refusal}"))
    if expect.start is Expectation.REFUSED and started:
        found.append(Violation("expect_start", 0.0, "start was accepted"))
    if expect.end_reason is not None and final.end_reason != expect.end_reason.value:
        found.append(
            Violation(
                "expect_end", None, f"ended {final.end_reason}, expected {expect.end_reason.value}"
            )
        )
    if expect.final_state is not None and final.runtime_state != expect.final_state.value:
        found.append(
            Violation(
                "expect_state", None, f"{final.runtime_state}, expected {expect.final_state.value}"
            )
        )
    seen = rules_seen(result)
    found.extend(
        Violation("expect_rule", None, f"rule {rule} never fired (saw {seen})")
        for rule in expect.rules
        if rule not in seen
    )
    found.extend(
        Violation("forbid_rule", None, f"rule {rule} fired")
        for rule in expect.forbid_rules
        if rule in seen
    )
    rows = result.trace.rows
    peak = max((row.output_rpm for row in rows), default=0.0)
    if expect.reaches_output_rpm is not None and peak < expect.reaches_output_rpm - 0.05:
        found.append(
            Violation(
                "expect_reach", None, f"peak {peak:.2f} < {expect.reaches_output_rpm} out rpm"
            )
        )
    if expect.max_output_rpm is not None and peak > expect.max_output_rpm + 1e-9:
        found.append(
            Violation("expect_max", None, f"peak {peak:.2f} > {expect.max_output_rpm} out rpm")
        )
    if expect.min_in_zone_fraction is not None:
        fraction = in_zone_fraction(result)
        if fraction is None or fraction < expect.min_in_zone_fraction:
            found.append(
                Violation(
                    "expect_zone", None, f"in zone {fraction} < {expect.min_in_zone_fraction}"
                )
            )
    for target in result.targets:
        if target.expected is Expectation.ACCEPTED and not target.accepted:
            found.append(Violation("expect_target", float(target.at), target.detail))
        if target.expected is Expectation.REFUSED and target.accepted:
            found.append(Violation("expect_target", float(target.at), target.detail))
    for request in result.requests:
        wanted = request.expected
        if (wanted is Expectation.ACCEPTED and not request.accepted) or (
            wanted is Expectation.REFUSED and request.accepted
        ):
            found.append(
                Violation(
                    "expect_request", float(request.at), f"{request.request}: {request.detail}"
                )
            )
    return tuple(found)


# =========================================================================
# Metrics
# =========================================================================


ARM_WINDOW_TICKS: Final[int] = 5
"""The arm's acceleration is judged over 1 s windows of measured speed (RFRD)."""

RFRD_SLACK_RPM: Final[int] = 2
"""RFRD is whole motor rpm: one count of rounding at each end of a window."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ArmWindow:
    """One judged window of the measured arm speed."""

    t: float
    accel_output_rpm_s: float
    g_rate_reference: float
    g_rate_leg_tip: float
    slack_accel: float
    slack_g_reference: float
    slack_g_leg_tip: float


def _judged(row: Row, min_run: int) -> bool:
    """A row whose speed change is the control path's own doing (not a stop, fault or passage)."""
    return (
        row.measured_fresh
        and not row.silent
        and row.safety_action not in EMERGENCY_ACTIONS
        and row.drive_state == "OPERATION_ENABLED"
        and abs(row.measured_motor_rpm) >= min_run
    )


def arm_windows(result: RunResult) -> tuple[ArmWindow, ...]:
    """The measured arm acceleration and g-dot, over every judged 1 s window."""
    rows = result.trace.rows
    rig = result.rig
    ratio = float(rig.machine.ratio)
    low, _ = _bounds(result)
    windows: list[ArmWindow] = []
    for index in range(ARM_WINDOW_TICKS, len(rows)):
        span = rows[index - ARM_WINDOW_TICKS : index + 1]
        if not all(_judged(row, low) for row in span):
            continue
        a, b = span[0], span[-1]
        dt = b.t - a.t
        rpm_a = OutputRpm(abs(a.measured_motor_rpm) / ratio)
        rpm_b = OutputRpm(abs(b.measured_motor_rpm) / ratio)
        top = OutputRpm(
            (max(abs(a.measured_motor_rpm), abs(b.measured_motor_rpm)) + RFRD_SLACK_RPM) / ratio
        )
        below = OutputRpm(max(0.0, top - RFRD_SLACK_RPM / ratio))
        windows.append(
            ArmWindow(
                t=b.t,
                accel_output_rpm_s=abs(rpm_b - rpm_a) / dt,
                g_rate_reference=abs(rig.g_reference(rpm_b) - rig.g_reference(rpm_a)) / dt,
                g_rate_leg_tip=abs(rig.g_leg_tip(rpm_b) - rig.g_leg_tip(rpm_a)) / dt,
                slack_accel=RFRD_SLACK_RPM / ratio / dt,
                slack_g_reference=(rig.g_reference(top) - rig.g_reference(below)) / dt,
                slack_g_leg_tip=(rig.g_leg_tip(top) - rig.g_leg_tip(below)) / dt,
            )
        )
    return tuple(windows)


def check_motion_limits(result: RunResult, *, at_leg_tip: bool = False) -> tuple[Violation, ...]:
    """The anti-nausea limits (config/motion_limits.json), judged on the MEASURED arm.

    Angular acceleration always; g-dot at the reference radius, or - with
    ``at_leg_tip`` - at the leg tip, where the same speed change moves the load
    ``leg_tip / reference`` times as fast.
    """
    accel_limit = float(result.motion.output_accel)
    g_limit = float(result.motion.g_rate)
    found: list[Violation] = []
    for window in arm_windows(result):
        if window.accel_output_rpm_s > accel_limit + window.slack_accel + 1e-9:
            found.append(
                Violation(
                    "arm_accel",
                    window.t,
                    f"{window.accel_output_rpm_s:.3f} output rpm/s > {accel_limit}",
                )
            )
        g_rate, slack, where = (
            (window.g_rate_leg_tip, window.slack_g_leg_tip, "leg tip")
            if at_leg_tip
            else (window.g_rate_reference, window.slack_g_reference, "reference radius")
        )
        if g_rate > g_limit + slack + 1e-9:
            found.append(
                Violation("arm_g_rate", window.t, f"{g_rate:.4f} g/s at the {where} > {g_limit}")
            )
    return tuple(found)


def _peak_setpoint_rate(rows: Sequence[Row], ratio: float, min_run: int) -> float:
    """Peak setpoint rate in OUTPUT rpm/s, each change spread over the time since the last."""
    peak = 0.0
    last_change = 0
    for index, (a, b) in enumerate(pairwise(rows), start=1):
        if a.setpoint_motor_rpm == b.setpoint_motor_rpm:
            continue
        window = (index - last_change) * float(TICK)
        last_change = index
        # The 0 <-> min_run passage is excluded; an emergency zero lands on 0, so it is too.
        if min(a.setpoint_motor_rpm, b.setpoint_motor_rpm) < min_run:
            continue
        peak = max(peak, abs(b.setpoint_motor_rpm - a.setpoint_motor_rpm) / ratio / window)
    return peak


def measure(result: RunResult) -> Metrics:
    """The headline numbers of one run."""
    rows = result.trace.rows
    ratio = float(result.rig.machine.ratio)
    low, _ = _bounds(result)
    windows = arm_windows(result)
    return Metrics(
        peak_output_rpm=max((row.output_rpm for row in rows), default=0.0),
        peak_motor_rpm=max((abs(row.measured_motor_rpm) for row in rows), default=0),
        peak_hertz=max((row.hertz for row in rows), default=0.0),
        peak_g_reference=max((row.g_reference for row in rows), default=0.0),
        peak_g_leg_tip=max((row.g_leg_tip for row in rows), default=0.0),
        peak_setpoint_rate_output_rpm_s=_peak_setpoint_rate(rows, ratio, low),
        peak_arm_accel_output_rpm_s=max((w.accel_output_rpm_s for w in windows), default=0.0),
        peak_g_rate_reference=max((w.g_rate_reference for w in windows), default=0.0),
        peak_g_rate_leg_tip=max((w.g_rate_leg_tip for w in windows), default=0.0),
        in_zone_fraction=in_zone_fraction(result),
        rules=rules_seen(result),
        end_reason=result.trace.final.end_reason,
    )
