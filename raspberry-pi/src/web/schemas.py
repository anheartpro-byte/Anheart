"""The wire shapes, and the one place domain types become JSON.

This module is the serialization boundary, and it is deliberately the mirror
image of the hardware boundary in ``src/units.py``. There, untyped numbers
arriving from copper are parsed into domain types once and trusted afterwards.
Here, domain types are converted into plain JSON once, on the way out, and the
page is written against these shapes rather than against the internals.

Why the conversion is explicit rather than automatic
----------------------------------------------------
Three of the domain's enums would serialize into something actively misleading
if handed to a generic encoder. ``DriveState`` and ``DriveFault`` carry
``auto()`` values, so a session log would record ``5`` for a fault whose number
renumbers itself the day somebody reorders the members; ``SafetyAction`` is an
``IntEnum``, so it would arrive as a bare severity rank with no name. Every one
of those is converted by hand below, into a **name**, which is the part that
does not change meaning.

Non-finite floats are converted to ``null``. JSON has no ``NaN``, and a page
that received one would render "NaN bpm" or, worse, silently draw it as zero. A
dash is the honest rendering of a number that has stopped making sense.

Why frozen dataclasses rather than pydantic models
--------------------------------------------------
``pydantic.BaseModel`` declares ``Any`` in its own constructor signature, so
inheriting from it trips the strict gate on the class statement itself and
would blind the checker to real leaks in this module (the same reasoning as
``src/config.py``). FastAPI builds a response model out of a stdlib dataclass
perfectly well, and a frozen dataclass also means a response cannot be edited
after it was built from a snapshot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, assert_never

from pydantic import TypeAdapter

from src.bitalino_client import LinkStats
from src.control_surface import (
    Command,
    EndSession,
    EstopReceipt,
    FaultReset,
    SessionEvent,
    SetManualTarget,
    StartManual,
    StartSession,
)
from src.motor.drive import FaultReport
from src.panel_status import EcgLinkStatus, PanelStatus
from src.presence.monitor import Clear, EmergencyStop, RampDown, StartBlocked
from src.sensors.base import Metric, SensorReading, SensorSpec
from src.telemetry import EcgWindow, Payload, PayloadKind
from src.training.plan import PhaseSpan, Program, TrainingProfile
from src.training.runtime import IdleLink
from src.training.safety import EstopAttestation, SafetyAcknowledgement
from src.training.types import (
    HeartRateSample,
    ManualView,
    SafetyVerdict,
    SignalQuality,
    SpeedView,
    TelemetrySnapshot,
    ZoneCounters,
)
from src.units import Monotonic, Seconds
from src.web.deps import PresenceView, SerialPortInfo, WebConfig


def _finite(value: float | None) -> float | None:
    """A float the page can render, or ``None``.

    Total: ``None`` in, ``None`` out; ``NaN`` and infinities out as ``None``.
    Called on every float that leaves this process, because JSON cannot carry
    the non-finite ones and a browser given one draws a chart with no y-axis.
    """
    if value is None or not math.isfinite(value):
        return None
    return value


def _enum_name(name: str) -> str:
    """Lowercase an ``auto()`` enum member's name for the wire.

    The NAME, never the value: ``DriveState`` and ``DriveFault`` use ``auto()``,
    so their integers renumber on a reorder and would rewrite the meaning of
    every stored record.
    """
    return name.lower()


# =========================================================================
# Requests
# =========================================================================


@dataclass(frozen=True, slots=True)
class StartBody:
    """Start a session.

    ``operator`` is required and must not be blank. Every safety record in this
    system carries a name, because an unattributable one is not a record, and
    the supervisor already refuses blank attributions - refusing here too means
    the operator finds out at the field rather than at the stop.
    """

    profile_id: str
    operator: str
    total_duration_s: float | None = None
    subject_age: int | None = None
    """The rider's age in years; a programmed session refuses unknown or under MIN_RIDER_AGE."""


@dataclass(frozen=True, slots=True)
class ManualStartBody:
    """Start a manual session. ``occupancy`` is declared now and frozen for the rotation.

    ``bench``: nobody on board (motor uncoupled, or arm coupled with the
    capsule empty). ``occupied``: a person in the capsule - refused by
    configuration until milestone M6.
    """

    occupancy: str
    operator: str


@dataclass(frozen=True, slots=True)
class ManualTargetBody:
    """A manual target in OUTPUT rpm: 0, or between the minimum running speed and the ceiling."""

    output_rpm: float
    operator: str


@dataclass(frozen=True, slots=True)
class FaultResetBody:
    """Reset a drive fault. Always a named, explicit operator action."""

    operator: str


@dataclass(frozen=True, slots=True)
class EndBody:
    """End the running session on the controlled ramp."""

    operator: str
    reason: str = "operator ended the session"


@dataclass(frozen=True, slots=True)
class EstopBody:
    """Latch the emergency stop.

    ``operator`` and ``reason`` are both optional, and that is the one place in
    this API where attribution is not required. An emergency stop must never be
    refused for a missing form field - not by validation, not by a confirmation
    dialog, not by anything. A nameless stop that happened beats a named one
    that was rejected.
    """

    operator: str = ""
    reason: str = "operator pressed the emergency stop"


@dataclass(frozen=True, slots=True)
class AckBody:
    """Clear the latched safety verdicts.

    ``estop_released`` defaults to ``False`` all the way down to the
    supervisor: forgetting to ask whether the mushroom has been pulled back out
    must fail closed rather than quietly assume it has.
    """

    operator: str
    estop_released: bool = False


@dataclass(frozen=True, slots=True)
class AttestBody:
    """Confirm, by name, that a real emergency stop is wired in.

    Two separate confirmations, because they are two separate facts about the
    machine and an operator who is only sure of one of them must not be able to
    tick "yes" once and pass both. Both must be ``True``; the route refuses
    otherwise, without recording anything.
    """

    operator: str
    sto_jumper_removed: bool = False
    mushroom_wired_nc: bool = False


@dataclass(frozen=True, slots=True)
class PresenceBody:
    """An attendant's screen saying it is still there.

    Feeds the ``attendant_absent`` rule. It proves a browser tab is open and
    reachable; it does not prove a human is looking at it, and nothing in
    software can.
    """

    operator: str = ""


@dataclass(frozen=True, slots=True)
class PreviewBody:
    """Ask what a profile would do, without starting anything.

    A dry run: it resolves the profile exactly as a start would - including the
    total-duration override - and returns the phase plan. Nothing is
    submitted, no motor is commanded, and it works while a session is running.
    """

    profile_id: str
    total_duration_s: float | None = None


# =========================================================================
# Pieces of a snapshot
# =========================================================================


@dataclass(frozen=True, slots=True)
class SpeedRow:
    """One speed, expressed five ways at once.

    **All four, always, and that is a display requirement rather than a
    convenience.** The gearbox ratio is 49.79, so a motor-shaft figure and an
    output-shaft figure differ by a factor of fifty; showing one of them alone
    makes a fifty-fold confusion invisible, and 276 motor rpm looks perfectly
    reasonable as an output speed until somebody works out that it would be
    2.8 g at the head. Shown together, the mistake is obvious on sight.
    """

    motor_rpm: float | None
    output_rpm: float | None
    hertz: float | None
    g_load: float | None
    """Centripetal load at the radius (Gc), gravity NOT included."""

    resultant_g: float | None
    """What the occupant feels (Gr): centripetal and gravity combined, sqrt(Gc^2 + 1)."""

    @classmethod
    def of(cls, view: SpeedView) -> SpeedRow:
        """Render a :class:`~src.training.types.SpeedView`."""
        return cls(
            motor_rpm=_finite(view.motor_rpm),
            output_rpm=_finite(view.output_rpm),
            hertz=_finite(view.hertz),
            g_load=_finite(view.g_load),
            resultant_g=_finite(view.resultant_g),
        )


@dataclass(frozen=True, slots=True)
class HeartRateRow:
    """The heart rate, its grade, and how old it is.

    ``age_s`` and ``stale`` travel with the number and are not optional. The
    number alone is the dangerous form: an 8-second median refreshed at 1 Hz
    that stopped refreshing four seconds ago looks exactly like a current one.
    """

    bpm: int | None
    quality: str
    age_s: float | None
    stale: bool
    seq: int

    @classmethod
    def of(
        cls, sample: HeartRateSample | None, age: Seconds | None, *, stale: bool
    ) -> HeartRateRow | None:
        """Render a sample, or ``None`` when there has never been one."""
        if sample is None:
            return None
        return cls(
            bpm=sample.usable_bpm,
            quality=sample.quality.value,
            age_s=_finite(age),
            stale=stale,
            seq=sample.seq,
        )


@dataclass(frozen=True, slots=True)
class ManualRow:
    """The manual session: target, ceiling and the ramp still to come.

    ``ramping`` is what lights the "do not move your head" banner: the
    setpoint is still on its way to the target.
    """

    occupancy: str
    occupancy_label: str
    target: SpeedRow
    ceiling: SpeedRow
    min_run: SpeedRow
    ramping: bool
    ramp_eta_s: float | None

    @classmethod
    def of(cls, view: ManualView | None) -> ManualRow | None:
        """Render the manual view, or ``None`` outside a manual session."""
        if view is None:
            return None
        return cls(
            occupancy=view.occupancy.value,
            occupancy_label=view.occupancy.label,
            target=SpeedRow.of(view.target),
            ceiling=SpeedRow.of(view.ceiling),
            min_run=SpeedRow.of(view.min_run),
            ramping=view.ramping,
            ramp_eta_s=_finite(view.ramp_eta),
        )


@dataclass(frozen=True, slots=True)
class SafetyRow:
    """The standing safety verdict.

    ``action`` is the member NAME and ``rank`` its severity, both sent: the
    name is what an operator reads and the rank is what a screen can compare
    without hard-coding the order of the enum.
    """

    action: str
    rank: int
    rule: str
    detail: str
    latched: bool
    age_s: float | None

    @classmethod
    def of(cls, verdict: SafetyVerdict | None, now: Monotonic | None) -> SafetyRow | None:
        """Render a verdict, or ``None`` when no rule is asking for anything."""
        if verdict is None:
            return None
        age = None if now is None else verdict.age(now)
        return cls(
            action=_enum_name(verdict.action.name),
            rank=int(verdict.action),
            rule=verdict.rule,
            detail=verdict.detail,
            latched=verdict.latched,
            age_s=_finite(age),
        )


@dataclass(frozen=True, slots=True)
class FaultRow:
    """A drive fault, named three ways.

    The keypad mnemonic is included because that is what is on the drive's own
    display, and an operator standing at the machine is comparing the screen
    with the drive, not with a source file.
    """

    name: str
    mnemonic: str
    meaning: str
    raw_code: int
    message: str

    @classmethod
    def of(cls, report: FaultReport | None) -> FaultRow | None:
        """Render a fault report, or ``None`` when the drive reports no fault."""
        if report is None:
            return None
        return cls(
            name=_enum_name(report.fault.name),
            mnemonic=report.fault.mnemonic,
            meaning=report.fault.meaning,
            raw_code=int(report.raw_code),
            message=report.message,
        )


@dataclass(frozen=True, slots=True)
class CountersRow:
    """How long the heart rate spent in, above and below its zone."""

    in_zone_s: float | None
    above_zone_s: float | None
    below_zone_s: float | None
    total_s: float | None

    @classmethod
    def of(cls, counters: ZoneCounters) -> CountersRow:
        """Render the zone counters."""
        return cls(
            in_zone_s=_finite(counters.in_zone),
            above_zone_s=_finite(counters.above_zone),
            below_zone_s=_finite(counters.below_zone),
            total_s=_finite(counters.total),
        )


# =========================================================================
# The snapshot
# =========================================================================


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One telemetry snapshot, as the page receives it.

    Field for field a rendering of :class:`~src.training.types.TelemetrySnapshot`,
    with two properties of that record preserved deliberately:

    * every measurement carries its **age**, and the derived ``*_stale`` flags
      are computed here by the snapshot itself rather than by the page, so two
      screens cannot disagree about whether a number is live;
    * ``measured`` is a separate field from ``setpoint``. The page is required
      to use ``measured`` as its "is it stopped" indicator, because a commanded
      zero on a coasting mass is not a measured zero and the difference is
      minutes of rotation.
    """

    at: float
    wall_clock: int
    phase: str
    elapsed_s: float | None
    remaining_s: float | None

    heart_rate: HeartRateRow | None
    target_bpm: int | None
    live_bpm: int | None

    setpoint: SpeedRow
    measured: SpeedRow
    setpoint_confirmed: bool

    drive_state: str
    drive_status_age_s: float | None
    drive_status_stale: bool
    current_a: float | None
    fault: FaultRow | None

    safety: SafetyRow | None
    safety_action: str
    safety_rank: int
    counters: CountersRow

    mode: str
    """REPOS, MANUEL, SEANCE or ARRET: the console's top banner."""

    manual: ManualRow | None

    @classmethod
    def of(cls, snapshot: TelemetrySnapshot) -> SnapshotRow:
        """Render a snapshot for the wire."""
        return cls(
            at=snapshot.at,
            wall_clock=snapshot.wall_clock,
            phase=snapshot.phase.value,
            elapsed_s=_finite(snapshot.elapsed),
            remaining_s=_finite(snapshot.remaining),
            heart_rate=HeartRateRow.of(
                snapshot.heart_rate,
                snapshot.heart_rate_age,
                stale=snapshot.heart_rate_is_stale,
            ),
            target_bpm=snapshot.target_bpm,
            live_bpm=snapshot.live_bpm,
            setpoint=SpeedRow.of(snapshot.setpoint),
            measured=SpeedRow.of(snapshot.measured),
            setpoint_confirmed=snapshot.setpoint_confirmed,
            drive_state=_enum_name(snapshot.drive_state.name),
            drive_status_age_s=_finite(snapshot.drive_status_age),
            drive_status_stale=snapshot.drive_status_is_stale,
            current_a=_finite(snapshot.current),
            fault=FaultRow.of(snapshot.fault),
            safety=SafetyRow.of(snapshot.safety, snapshot.at),
            safety_action=_enum_name(snapshot.safety_action.name),
            safety_rank=int(snapshot.safety_action),
            counters=CountersRow.of(snapshot.counters),
            mode=snapshot.mode.value,
            manual=ManualRow.of(snapshot.manual),
        )


# =========================================================================
# The console's link panel
# =========================================================================


@dataclass(frozen=True, slots=True)
class DriveLinkRow:
    """The idle, read-only drive polling, as the link panel shows it."""

    description: str | None
    open: bool
    reads: int
    failures: int
    consecutive_failures: int
    latency_ms: float | None
    last_error: str | None

    @classmethod
    def of(cls, link: IdleLink, description: str | None) -> DriveLinkRow:
        """Render an :class:`~src.training.runtime.IdleLink`."""
        latency = link.last_latency
        return cls(
            description=description,
            open=link.open,
            reads=link.reads,
            failures=link.failures,
            consecutive_failures=link.consecutive_failures,
            latency_ms=None if latency is None else _finite(latency * 1000.0),
            last_error=link.last_error,
        )


@dataclass(frozen=True, slots=True)
class LinkStatsRow:
    """The BITalino decoder's counters, field for field."""

    frames: int
    skipped_bytes: int
    sync_losses: int
    filled_samples: int
    dropped_backlog_samples: int
    reconnects: int

    @classmethod
    def of(cls, stats: LinkStats | None) -> LinkStatsRow | None:
        """Render :class:`~src.bitalino_client.LinkStats`, or ``None`` (simulator)."""
        if stats is None:
            return None
        return cls(
            frames=stats.frames,
            skipped_bytes=stats.skipped_bytes,
            sync_losses=stats.sync_losses,
            filled_samples=stats.filled_samples,
            dropped_backlog_samples=stats.dropped_backlog_samples,
            reconnects=stats.reconnects,
        )


@dataclass(frozen=True, slots=True)
class EcgLinkRow:
    """The BITalino link and the DSP bridge behind it."""

    source: str
    address: str | None
    connected: bool
    acquiring: bool
    connect_attempts: int
    last_error: str | None
    link: LinkStatsRow | None
    batches: int
    samples: int
    missing_channel: int
    last_batch_age_s: float | None
    dsp_seq: int | None
    dsp_quality: str | None

    @classmethod
    def of(cls, status: EcgLinkStatus, now: Monotonic) -> EcgLinkRow:
        """Render an :class:`~src.panel_status.EcgLinkStatus` at ``now``."""
        bridge = status.bridge
        metrics = bridge.last_metrics
        last = bridge.last_batch_at
        return cls(
            source=status.source.value,
            address=status.address,
            connected=status.connected,
            acquiring=status.acquiring,
            connect_attempts=status.connect_attempts,
            last_error=status.last_error,
            link=LinkStatsRow.of(status.link),
            batches=bridge.batches,
            samples=bridge.samples,
            missing_channel=bridge.missing_channel,
            last_batch_age_s=None if last is None else _finite(max(0.0, now - last)),
            dsp_seq=None if metrics is None else metrics.seq,
            dsp_quality=None if metrics is None else metrics.quality.value,
        )


@dataclass(frozen=True, slots=True)
class PanelRow:
    """The local console's link panel. ``GET /api/panel``."""

    motion_enabled: bool
    programs_enabled: bool
    motor_backend: str
    drive: DriveLinkRow
    ecg: EcgLinkRow
    heart_rate_trend_bpm_per_min: float | None
    radius_m: float | None
    gear_ratio: float | None
    motor_max_rpm: int

    @classmethod
    def of(cls, status: PanelStatus) -> PanelRow:
        """Render a :class:`~src.panel_status.PanelStatus`."""
        return cls(
            motion_enabled=status.motion_enabled,
            programs_enabled=status.programs_enabled,
            motor_backend=status.motor_backend.value,
            drive=DriveLinkRow.of(status.drive, status.drive_link),
            ecg=EcgLinkRow.of(status.ecg, status.at),
            heart_rate_trend_bpm_per_min=_finite(status.heart_rate_trend),
            radius_m=_finite(status.radius),
            gear_ratio=_finite(status.ratio),
            motor_max_rpm=int(status.motor_max_rpm),
        )


@dataclass(frozen=True, slots=True)
class MetricRow:
    """One derived number of a sensor; ``value`` null when it cannot honestly be computed."""

    key: str
    label: str
    value: float | None
    unit: str

    @classmethod
    def of(cls, metric: Metric) -> MetricRow:
        return cls(
            key=metric.key,
            label=metric.label,
            value=None if metric.value is None else _finite(metric.value),
            unit=metric.unit,
        )


@dataclass(frozen=True, slots=True)
class SensorRow:
    """One BITalino channel: what it is, its latest window, quality and metrics."""

    kind: str
    channel: int
    label: str
    unit: str
    description: str
    display_rate: int
    at: float | None
    """Monotonic time of the window's last sample; null before the first reading."""

    waveform: tuple[float, ...]
    quality: str
    detail: str
    metrics: tuple[MetricRow, ...]

    @classmethod
    def of(cls, spec: SensorSpec, reading: SensorReading | None) -> SensorRow:
        if reading is None:
            return cls(
                kind=spec.kind.value,
                channel=spec.kind.channel,
                label=spec.label,
                unit=spec.unit,
                description=spec.description,
                display_rate=spec.display_rate,
                at=None,
                waveform=(),
                quality=SignalQuality.NO_SIGNAL.value,
                detail="pas encore de lecture",
                metrics=(),
            )
        return cls(
            kind=spec.kind.value,
            channel=spec.kind.channel,
            label=spec.label,
            unit=spec.unit,
            description=spec.description,
            display_rate=reading.display_rate,
            at=float(reading.at),
            waveform=tuple(v if math.isfinite(v) else 0.0 for v in reading.waveform),
            quality=reading.quality.value,
            detail=reading.detail,
            metrics=tuple(MetricRow.of(m) for m in reading.metrics),
        )


@dataclass(frozen=True, slots=True)
class SensorsRow:
    """``GET /api/sensors``: every configured channel, in configuration order."""

    sensors: tuple[SensorRow, ...]


@dataclass(frozen=True, slots=True)
class CameraRow:
    """``GET /api/camera``: the camera fail-safe, as the Securite page shows it."""

    configured: bool
    camera: str
    state: str
    """"absent", "waiting" (no frame judged yet), "clear", "start_blocked", "ramp_down",
    "emergency_stop"."""

    detail: str
    latched_rule: str | None

    @classmethod
    def of(cls, view: PresenceView | None, camera: str) -> CameraRow:
        if view is None:
            return cls(
                configured=False,
                camera=camera,
                state="absent",
                detail="aucune camera configuree (PRESENCE_SOURCE=none)",
                latched_rule=None,
            )
        latched = view.monitor.latched
        rule = None if latched is None else latched.rule
        decision = view.last_decision
        state: str
        detail: str
        match decision:
            case None:
                state, detail = "waiting", "aucune image encore jugee"
            case Clear():
                state, detail = "clear", ""
            case StartBlocked():
                state, detail = "start_blocked", decision.detail
            case RampDown(verdict=verdict):
                state, detail = "ramp_down", verdict.detail
            case EmergencyStop(verdict=verdict):
                state, detail = "emergency_stop", verdict.detail
            case _ as unreachable:
                assert_never(unreachable)
        return cls(configured=True, camera=camera, state=state, detail=detail, latched_rule=rule)


@dataclass(frozen=True, slots=True)
class EventRow:
    """One session event."""

    kind: str
    at: float
    wall_clock: int
    operator: str
    detail: str

    @classmethod
    def of(cls, event: SessionEvent) -> EventRow:
        """Render an event."""
        return cls(
            kind=event.kind.value,
            at=event.at,
            wall_clock=event.wall_clock,
            operator=event.operator,
            detail=event.detail,
        )


@dataclass(frozen=True, slots=True)
class EcgRow:
    """A slice of the live ECG trace, in millivolts.

    ``gap`` is sent so the page can break the line instead of joining two ends
    of a discontinuity, which draws a vertical stroke that looks exactly like a
    QRS complex.
    """

    seq: int
    fs_hz: int
    gap: bool
    samples: tuple[float, ...]

    @classmethod
    def of(cls, window: EcgWindow) -> EcgRow:
        """Render an ECG window."""
        return cls(
            seq=window.seq,
            fs_hz=window.fs_hz,
            gap=window.gap,
            samples=tuple(float(value) for value in window.samples),
        )


# =========================================================================
# Plans and profiles
# =========================================================================


@dataclass(frozen=True, slots=True)
class PhaseSpanRow:
    """One phase's place on the nominal timeline, half-open ``[start, end)``."""

    phase: str
    start_s: float
    end_s: float
    duration_s: float

    @classmethod
    def of(cls, span: PhaseSpan) -> PhaseSpanRow:
        """Render a phase span."""
        return cls(
            phase=span.phase.value,
            start_s=span.start,
            end_s=span.end,
            duration_s=span.duration,
        )


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """A training profile, as the setup view shows and edits it.

    Includes the derived quantities - ``hold_s``, ``warmup_rpm_ceiling``,
    ``hsp_hertz`` - because they are what an operator actually checks, and
    recomputing them in JavaScript would put a second implementation of the
    gearbox arithmetic in the browser.
    """

    profile_id: str
    name: str
    total_duration_s: float
    baseline_s: float
    warmup_max_s: float
    hold_min_s: float
    cooldown_s: float
    recovery_s: float
    hold_s: float
    zone_low_bpm: int
    zone_high_bpm: int
    hard_max_bpm: int
    critical_bpm: int
    subject_hr_max: int
    min_run_rpm: int
    max_rpm: int
    warmup_rpm_ceiling_fraction: float
    warmup_rpm_ceiling: int
    hsp_hertz: float
    channels: tuple[str, ...]
    allow_above_nameplate: bool

    @classmethod
    def of(cls, profile: TrainingProfile) -> ProfileRow:
        """Render a profile."""
        return cls(
            profile_id=profile.profile_id,
            name=profile.name,
            total_duration_s=profile.total_duration_s,
            baseline_s=profile.baseline_s,
            warmup_max_s=profile.warmup_max_s,
            hold_min_s=profile.hold_min_s,
            cooldown_s=profile.cooldown_s,
            recovery_s=profile.recovery_s,
            hold_s=profile.hold_s,
            zone_low_bpm=profile.zone_low_bpm,
            zone_high_bpm=profile.zone_high_bpm,
            hard_max_bpm=profile.hard_max_bpm,
            critical_bpm=profile.critical_bpm,
            subject_hr_max=profile.subject_hr_max,
            min_run_rpm=profile.min_run_rpm,
            max_rpm=profile.max_rpm,
            warmup_rpm_ceiling_fraction=profile.warmup_rpm_ceiling_fraction,
            warmup_rpm_ceiling=profile.warmup_rpm_ceiling,
            hsp_hertz=profile.hsp_hertz,
            channels=tuple(channel.value for channel in profile.channels),
            allow_above_nameplate=profile.allow_above_nameplate,
        )


@dataclass(frozen=True, slots=True)
class PlanPreviewRow:
    """What a session would do, without doing any of it.

    The speeds are rendered as full :class:`SpeedRow`s rather than as bare rpm,
    so an operator approving a programme sees the g-load it implies before a
    person is in the machine rather than after.
    """

    profile: ProfileRow
    source_rev: int
    resolved_at: int
    total_overridden: bool
    spans: tuple[PhaseSpanRow, ...]
    ceiling: SpeedRow
    warmup_ceiling: SpeedRow
    min_run: SpeedRow

    @classmethod
    def of(
        cls,
        program: Program,
        *,
        ceiling: SpeedView,
        warmup_ceiling: SpeedView,
        min_run: SpeedView,
    ) -> PlanPreviewRow:
        """Render a resolved programme."""
        return cls(
            profile=ProfileRow.of(program.profile),
            source_rev=int(program.source_rev),
            resolved_at=program.resolved_at,
            total_overridden=program.total_overridden,
            spans=tuple(PhaseSpanRow.of(span) for span in program.timeline),
            ceiling=SpeedRow.of(ceiling),
            warmup_ceiling=SpeedRow.of(warmup_ceiling),
            min_run=SpeedRow.of(min_run),
        )


@dataclass(frozen=True, slots=True)
class ProfileListRow:
    """Every stored profile, with the revision they were read at.

    The revision is what an edit must send back: the store uses optimistic
    concurrency, so two browsers editing the same profile produce a refusal for
    the second one rather than a silent overwrite.
    """

    rev: int
    profiles: tuple[ProfileRow, ...]


# =========================================================================
# Commands and their answers
# =========================================================================


@dataclass(frozen=True, slots=True)
class CommandRow:
    """What was accepted into the mailbox."""

    kind: str
    operator: str
    detail: str
    at: float

    @classmethod
    def of(cls, command: Command) -> CommandRow:
        """Render a command. Closed union; matched with the nested form."""
        match command:
            case StartSession():
                override = command.total_duration_s
                detail = (
                    command.profile_id
                    if override is None
                    else f"{command.profile_id} over {override:.0f} s"
                )
                return cls(kind="start", operator=command.operator, detail=detail, at=command.at)
            case EndSession():
                return cls(
                    kind="end", operator=command.operator, detail=command.reason, at=command.at
                )
            case StartManual():
                return cls(
                    kind="manual_start",
                    operator=command.operator,
                    detail=command.occupancy.value,
                    at=command.at,
                )
            case SetManualTarget():
                return cls(
                    kind="manual_target",
                    operator=command.operator,
                    detail=f"{command.output_rpm:.2f} output rpm",
                    at=command.at,
                )
            case FaultReset():
                return cls(kind="fault_reset", operator=command.operator, detail="", at=command.at)


@dataclass(frozen=True, slots=True)
class EstopRow:
    """Proof the emergency stop was latched.

    ``stopped`` is deliberately absent, and no field here claims the machine
    has stopped. With STO jumpered there is no independent removal of torque
    and the fastest stop available is the commissioned ramp; the measured
    output speed in the next snapshot is the only thing that speaks about
    motion.
    """

    operator: str
    reason: str
    at: float
    wall_clock: int
    action: str
    rule: str
    detail: str

    @classmethod
    def of(cls, receipt: EstopReceipt) -> EstopRow:
        """Render an emergency-stop receipt."""
        return cls(
            operator=receipt.operator,
            reason=receipt.reason,
            at=receipt.at,
            wall_clock=receipt.wall_clock,
            action=_enum_name(receipt.verdict.action.name),
            rule=receipt.verdict.rule,
            detail=receipt.verdict.detail,
        )


@dataclass(frozen=True, slots=True)
class AckRow:
    """What an acknowledgement cleared, and who is answerable for it."""

    operator: str
    at: float
    wall_clock: int
    cleared: tuple[str, ...]

    @classmethod
    def of(cls, record: SafetyAcknowledgement) -> AckRow:
        """Render an acknowledgement."""
        return cls(
            operator=record.operator,
            at=record.at,
            wall_clock=record.wall_clock,
            cleared=record.cleared,
        )


@dataclass(frozen=True, slots=True)
class AttestationRow:
    """The standing emergency-stop attestation, with the statement made."""

    operator: str
    statement: str
    at: float
    wall_clock: int

    @classmethod
    def of(cls, attestation: EstopAttestation) -> AttestationRow:
        """Render an attestation."""
        return cls(
            operator=attestation.operator,
            statement=attestation.statement,
            at=attestation.at,
            wall_clock=attestation.wall_clock,
        )


# =========================================================================
# System status
# =========================================================================


@dataclass(frozen=True, slots=True)
class PortRow:
    """One candidate serial port."""

    device: str
    description: str

    @classmethod
    def of(cls, port: SerialPortInfo) -> PortRow:
        """Render a port."""
        return cls(device=port.device, description=port.description)


@dataclass(frozen=True, slots=True)
class BindRow:
    """How the interface is bound, so the page can say so out loud.

    ``loopback`` is shown in the UI. An operator who reaches the page from
    another machine and sees "loopback: true" is looking at a reverse proxy
    they did not know about, which changes what the token is protecting.
    """

    host: str
    port: int
    loopback: bool
    token_required: bool
    allowed_origins: tuple[str, ...]

    @classmethod
    def of(cls, config: WebConfig) -> BindRow:
        """Render the bind configuration. Never includes the token itself."""
        return cls(
            host=config.host,
            port=config.port,
            loopback=config.is_loopback,
            token_required=config.requires_token,
            allowed_origins=config.allowed_origins,
        )


@dataclass(frozen=True, slots=True)
class StatusRow:
    """Everything the setup view needs before a session, in one request.

    Including the negatives: no attestation, a latched verdict, no ECG yet.
    Those are what the start button is disabled on, and a page that had to
    infer them from a missing field would infer wrongly.
    """

    run_state: str
    estop_latched: bool
    attested: bool
    attestation: AttestationRow | None
    attestation_statement: str
    standing: SafetyRow | None
    floor: SafetyRow | None
    live: tuple[SafetyRow, ...]
    retained_hr_samples: int
    pending: CommandRow | None
    attendant_last_seen: float | None
    clients: int
    evictions: int
    ecg_fs_hz: int
    ecg_seq: int
    profile_rev: int
    profile_ids: tuple[str, ...]
    ports: tuple[PortRow, ...]
    bind: BindRow
    counters: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class PresenceRow:
    """The instant a presence ping was recorded at.

    Echoed back so the page can tell a ping that reached the supervisor from
    one that reached a proxy: if this value stops advancing, the attendant rule
    is not being fed and the ``attendant_absent`` freeze is coming.
    """

    at: float


@dataclass(frozen=True, slots=True)
class HealthRow:
    """The container healthcheck's answer. Unauthenticated, and says nothing.

    Deliberately carries no state: it is reachable without a token, so it must
    not report whether a session is running, who the operator is, or anything
    else about the person in the machine. "This process is answering HTTP" is
    the entire question a healthcheck asks.
    """

    status: str
    service: str


# =========================================================================
# WebSocket envelope
# =========================================================================


@dataclass(frozen=True, slots=True)
class WsEnvelope:
    """One WebSocket frame: exactly one of a snapshot, an event, a notice.

    A single tagged shape rather than several, so the browser has one parse
    path and the ordering of events against snapshots survives the trip.
    """

    kind: str
    snapshot: SnapshotRow | None = None
    event: EventRow | None = None
    ecg: EcgRow | None = None
    notice: str = ""

    @classmethod
    def of(cls, payload: Payload) -> WsEnvelope:
        """Render one hub payload. Total over :class:`~src.telemetry.PayloadKind`."""
        snapshot = payload.snapshot
        event = payload.event
        return cls(
            kind=payload.kind.value,
            snapshot=None if snapshot is None else SnapshotRow.of(snapshot),
            event=None if event is None else EventRow.of(event),
            ecg=None,
            notice=payload.notice,
        )

    @classmethod
    def of_ecg(cls, window: EcgWindow) -> WsEnvelope:
        """Render an ECG slice as its own frame."""
        return cls(kind=_ECG_KIND, ecg=EcgRow.of(window))


_ECG_KIND: Final[str] = "ecg"
"""Envelope kind for an ECG slice.

Not a :class:`~src.telemetry.PayloadKind` member: the ECG is pulled by the
socket task rather than published through the hub, so it is not one of the
things a client can be evicted for failing to read.
"""

_ENVELOPE: Final[TypeAdapter[WsEnvelope]] = TypeAdapter(WsEnvelope)
"""Built once at import: constructing a validator per frame would be 5 Hz of waste."""


def encode_envelope(envelope: WsEnvelope) -> str:
    """Serialize one frame to JSON text.

    Through pydantic's ``TypeAdapter`` rather than ``json.dumps`` of a
    ``dataclasses.asdict``, because ``asdict`` is typed as returning
    ``dict[str, Any]`` and that ``Any`` would be the one hole in an otherwise
    fully-checked module.
    """
    return _ENVELOPE.dump_json(envelope).decode("utf-8")


def payload_is_terminal(kind: PayloadKind) -> bool:
    """Whether this payload is the last thing a client may be sent.

    Only RESYNC is: the client has been evicted, so the socket closes after it.
    A function rather than a comparison at the call site, so the answer lives
    with the enum it is about.
    """
    return kind is PayloadKind.RESYNC
