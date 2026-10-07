"""The console's session recorder: what the event loop sees, turned into the shared format.

One object, owned by the event loop, called from the console's tasks. It
decides nothing about the machine. Every method here reads memory, builds
frozen values and hands them to :class:`~src.record.journal.Journal`, whose
``submit`` never waits: no file is opened, written or flushed from this module.
The disk belongs to the journal thread.

A record is opened when a session has just been armed (:meth:`SessionRecorder.begin`),
fed once per control tick (:meth:`SessionRecorder.observe`), and closed on the
tick that sees the session's phase reach ``DONE``, or at the console's exit
(:meth:`SessionRecorder.finish`).

``DONE`` and not the runtime's FINISHED state, on purpose. After an ordinary
stop they are the same tick. Behind a latched drive fault the runtime stays
ENDING until an operator resets the fault, which can be the next morning: the
descent and the monitored recovery are over, the phase says ``DONE``, and a
record still open would grow by five rows a second with nobody in the machine
until the disk refused the next start.

Every entry point is **total**: it returns ``None`` and never raises. The
recorder is an observer of the session, called from the control task and the
acquisition task; a bug here must cost the record (it is then reported as
degraded), never unwind a tick with the motor commanded.

What is NOT written, on purpose:

* the operator's name. The manifest and the events carry
  :func:`operator_alias`, a stable opaque stand-in; a name typed at the console
  is also removed from every event's text, with any e-mail address;
* the drive's Modbus frames: ``drive_frames.jsonl`` stays empty on the console
  until the exchange log is bounded and drained (follow-up ticket);
* the geometry snapshot: the console knows its reference radius and the leg
  tip, not the capsule and counterweight radii the snapshot requires, and
  nothing here invents them. The applied geometry is part of ``config_hash``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Concatenate, Final, assert_never
from uuid import uuid4

from pydantic import TypeAdapter

from src.bitalino_client import LinkStats, SampleBatch
from src.clock import Clock
from src.cloud_sync import DASHBOARD_OPERATOR
from src.control_surface import LOCAL_SUBJECT, SessionEvent
from src.control_surface import EventKind as SurfaceEvent
from src.local_config import LocalConfig
from src.motor.drive import DriveStatus
from src.record.codec import IDENTIFIER, Privacy
from src.record.journal import (
    MIN_FREE_BYTES,
    STORAGE_STALE_AFTER,
    Cause,
    Closing,
    Journal,
    JournalStatus,
    RawBatch,
    Sensors,
)
from src.record.rows import JsonValue, Row
from src.record.schema import Clocks, EndObservation, Event, EventKind, Manifest, Profile
from src.sensors.base import SensorReading
from src.training.motion import MotionLimits
from src.training.plan import Program
from src.training.runtime import RecordStorageLow, RuntimeLimits, TrainingRuntime
from src.training.safety import SafetyLimits
from src.training.types import Occupancy, Phase, TelemetrySnapshot
from src.units import Metres, Monotonic, OutputRpm, elapsed, output_rpm_to_g

_logger: Final[logging.Logger] = logging.getLogger(__name__)

CONTRACT_VERSION: Final[str] = "2"

MAX_NAMES: Final[int] = 32
"""Operator names remembered per session for redaction. Past that, event text is withheld."""

MAX_TEXT: Final[int] = 500
"""Characters of free text kept per event: a message, not a document."""

WITHHELD: Final[str] = "[redacted]"

SYSTEM: Final[str] = "system"
REMOTE: Final[str] = "remote"
UNATTRIBUTED: Final[str] = "unattributed"

INTERRUPTED: Final[str] = "interrupted"
"""The end reason of a record closed while the runtime had not ended its session."""

BROKEN: Final[str] = (
    "enregistrement de seance degrade : erreur interne de l'enregistreur (voir le journal "
    "de la console). La seance et la securite continuent."
)

_EMAILS: Final[Privacy] = Privacy()
_SAFETY: Final[TypeAdapter[SafetyLimits]] = TypeAdapter(SafetyLimits)
_RUNTIME: Final[TypeAdapter[RuntimeLimits]] = TypeAdapter(RuntimeLimits)
_MOTION: Final[TypeAdapter[MotionLimits]] = TypeAdapter(MotionLimits)


# =========================================================================
# Identity: who and what, without a name
# =========================================================================


def operator_alias(operator: str) -> str:
    """A stable, opaque stand-in for a name typed at the console. Never the name.

    The same name gives the same alias on every session of every machine, so
    records can be lined up by operator without any of them holding a name.
    It is a pseudonym, not anonymity: whoever holds the list of operators can
    recompute it.
    """
    name = " ".join(operator.split()).casefold()
    if not name:
        return UNATTRIBUTED
    return "op-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def opaque(prefix: str, value: str) -> str:
    """``value`` when it already is an opaque identifier, else a digest of it."""
    if IDENTIFIER.fullmatch(value) is not None:
        return value
    return f"{prefix}-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def names_of(operator: str) -> tuple[str, ...]:
    """Every spelling of ``operator`` to keep out of the record's text.

    A launch from the dashboard is attributed to ``<name> (tableau de bord)``:
    the name alone is one of them too.
    """
    whole = " ".join(operator.split())
    alone = whole.removesuffix(f"({DASHBOARD_OPERATOR})").strip()
    return tuple(dict.fromkeys(name for name in (whole, alone) if name))


def redact(text: str, names: tuple[str, ...]) -> str:
    """``text`` without e-mail addresses and without any of ``names`` as a whole word."""
    cleaned = _EMAILS.text(text)
    for name in names:
        cleaned = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", WITHHELD, cleaned, flags=re.IGNORECASE)
    return cleaned


def utc_stamp(clock: Clock) -> str:
    """The clock's wall time, ISO 8601 UTC with a ``Z``."""
    return (
        datetime.fromtimestamp(clock.unix_millis() / 1000, UTC).isoformat().replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Stamp:
    """What every manifest written by this console carries. Computed once, at startup."""

    machine_id: str
    organization_id: str
    software_version: str
    config_hash: str
    medical_parameters_version: str


def _digest(document: JsonValue) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stamp_for(
    config: LocalConfig, limits: RuntimeLimits, safety: SafetyLimits, motion: MotionLimits
) -> Stamp:
    """Hash the configuration actually applied. No secret enters either hash.

    ``medical_parameters_version`` covers what a medical decision set: the
    cardiac tiers and every other safety limit, the minimum rider age and the
    motion (anti-nausea) limits. ``config_hash`` covers that and the rest of
    what shapes a session: geometry, ceilings, gates, sources, the drive link.
    The dashboard key and the console token are in neither.
    """
    medical: JsonValue = {
        "safety": _SAFETY.dump_json(safety).decode("utf-8"),
        "motion": _MOTION.dump_json(motion).decode("utf-8"),
        "min_rider_age": config.min_rider_age,
    }
    link = config.drive_link
    applied: JsonValue = {
        "medical": medical,
        "runtime": _RUNTIME.dump_json(limits).decode("utf-8"),
        "geometry": (
            config.geometry.radius,
            config.geometry.ratio,
            config.geometry.nominal_rpm,
            config.geometry.base_hz,
            config.leg_tip_radius,
        ),
        "motor_max_rpm": config.motor_max_rpm,
        "occupied_enabled": config.occupied_enabled,
        "programs_enabled": config.programs_enabled,
        "sensors": tuple(kind.value for kind in config.sensors),
        "motor_backend": config.motor_backend.value,
        "ecg_source": config.ecg_source.value,
        "camera": config.camera.value,
        "drive_link": None if link is None else link.describe(),
    }
    record = config.record
    return Stamp(
        machine_id=record.machine_id,
        organization_id=record.organization_id,
        software_version=record.software_version,
        config_hash=_digest(applied),
        medical_parameters_version=_digest(medical),
    )


def frozen_profile(program: Program) -> Profile:
    """The resolved programme, numbers only: its display name stays out of the record."""
    profile = program.profile
    return Profile(
        total_duration_s=profile.total_duration_s,
        baseline_s=profile.baseline_s,
        warmup_max_s=profile.warmup_max_s,
        hold_min_s=profile.hold_min_s,
        cooldown_s=profile.cooldown_s,
        recovery_s=profile.recovery_s,
        zone_low_bpm=profile.zone_low_bpm,
        zone_high_bpm=profile.zone_high_bpm,
        hard_max_bpm=profile.hard_max_bpm,
        critical_bpm=profile.critical_bpm,
        subject_hr_max=profile.subject_hr_max,
        min_run_rpm=profile.min_run_rpm,
        max_rpm=profile.max_rpm,
        warmup_rpm_ceiling_fraction=profile.warmup_rpm_ceiling_fraction,
        channels=tuple(channel.value for channel in profile.channels),
        allow_above_nameplate=profile.allow_above_nameplate,
        source_rev=program.source_rev,
        resolved_at=program.resolved_at,
        total_overridden=program.total_overridden,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionRequest:
    """The session the runtime has just armed, as the console knows it."""

    occupancy: Occupancy
    operator: str
    program: Program | None = None
    """``None``: a manual session."""

    subject_id: str = LOCAL_SUBJECT
    cloud_session_id: str | None = None


def manifest_for(stamp: Stamp, request: SessionRequest, clock: Clock) -> Manifest:
    """The opening manifest of one session. Identifiers only, never a name."""
    identity = uuid4().hex
    started = utc_stamp(clock)
    subject = request.subject_id.strip()
    remote = request.cloud_session_id
    return Manifest(
        schema_version=2,
        record_id=identity,
        machine_id=stamp.machine_id,
        organization_id=stamp.organization_id,
        session_id=None if remote is None else opaque("session", remote),
        local_ref=identity[:16],
        kind="manual" if request.program is None else "auto",
        occupancy="bench" if request.occupancy is Occupancy.BENCH else "occupied",
        operator=operator_alias(request.operator),
        subject_id=None if subject in ("", LOCAL_SUBJECT) else opaque("subject", subject),
        profile=None if request.program is None else frozen_profile(request.program),
        config_hash=stamp.config_hash,
        software_version=stamp.software_version,
        contract_version=CONTRACT_VERSION,
        medical_parameters_version=stamp.medical_parameters_version,
        clocks=Clocks(monotonic_start=clock.monotonic(), utc_start=started, ntp_offset_s=None),
        started_at=started,
        ended_at=None,
        end_reason=None,
        preflight=None,
    )


# =========================================================================
# In words, for the operator
# =========================================================================


def describe_status(status: JournalStatus, root: Path) -> str:
    """One line for the console's event list when the record's health changes."""
    safe = "La seance et la securite continuent."
    match status.cause:
        case None:
            return "enregistrement de seance retabli"
        case Cause.STALLED:
            return (
                "enregistrement de seance degrade : le disque ne repond plus "
                f"({status.pending} elements en attente). {safe}"
            )
        case Cause.WRITE_FAILED:
            return f"enregistrement de seance degrade : {_why(status)}. {safe}"
        case Cause.QUEUE_FULL:
            return (
                "enregistrement de seance degrade : file d'ecriture pleine, des mesures "
                f"sont perdues. {safe}"
            )
        case Cause.STORAGE_UNAVAILABLE:
            return (
                f"enregistrement de seance indisponible : dossier {root} inutilisable "
                "(creation, droits ou mesure de l'espace libre)"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _why(status: JournalStatus) -> str:
    error = status.error
    if error is None:
        return "aucun dossier d'enregistrement ouvert pour cette seance"
    if "ENOSPC" in error.detail or "EDQUOT" in error.detail:
        return f"disque plein ({error.operation})"
    if "EACCES" in error.detail or "EPERM" in error.detail or "EROFS" in error.detail:
        return f"ecriture refusee, droits insuffisants ({error.operation})"
    return f"erreur d'ecriture ({error.operation} : {error.detail})"


def storage_gate(journal: Journal, clock: Clock) -> Callable[[], RecordStorageLow | None]:
    """The arming gate: at least 500 MB free under the records directory, measured lately.

    Refuses under 500 MB, when the space could not be measured, and when the
    measurement is older than :data:`~src.record.journal.STORAGE_STALE_AFTER`:
    a journal thread stuck on a dead disk leaves its last number behind, and
    that number would otherwise go on saying there is room.

    It reads the journal thread's last measurement and the clock, and returns
    at once: no file-system call, the gate runs in the control task.
    """

    def gate() -> RecordStorageLow | None:
        storage = journal.storage
        free = storage.free_bytes
        age = elapsed(storage.measured_at, clock.monotonic())
        where = str(journal.root)
        if age > STORAGE_STALE_AFTER:
            return RecordStorageLow(free, MIN_FREE_BYTES, where, stale_for=age)
        if free is None or free < MIN_FREE_BYTES:
            return RecordStorageLow(free, MIN_FREE_BYTES, where)
        return None

    return gate


def record_kind(kind: SurfaceEvent) -> EventKind | None:
    """Where a console event goes in the record's closed vocabulary, or ``None``: nowhere."""
    match kind:
        case (
            SurfaceEvent.START_REQUESTED
            | SurfaceEvent.FAULT_RESET_REQUESTED
            | SurfaceEvent.END_REQUESTED
            | SurfaceEvent.EMERGENCY_STOP
            | SurfaceEvent.ATTESTED
        ):
            return EventKind.OPERATOR_ACTION
        case SurfaceEvent.ACKNOWLEDGED:
            return EventKind.VERDICT_ACK
        case SurfaceEvent.REFUSED:
            return EventKind.REFUSAL
        case SurfaceEvent.DASHBOARD:
            # News of the dashboard link (a server of another contract, a launch
            # refused on that account). The operator was shown it, so it is
            # kept; it answers nothing asked at this console, so it is not a
            # refusal. The record's vocabulary is closed and has no kind of its
            # own for it: a warning, told apart by its ``dashboard:`` prefix.
            return EventKind.WARNING
        case SurfaceEvent.SESSION_RUNNING | SurfaceEvent.SESSION_IDLE | SurfaceEvent.RECORDING:
            # States the ticks already carry, and the recorder's own messages.
            return None
        case _ as unreachable:
            assert_never(unreachable)


# =========================================================================
# The recorder
# =========================================================================


@dataclass(slots=True)
class _Live:
    """The open session. Mutable, owned by the event loop like the recorder itself."""

    origin: Monotonic
    names: tuple[str, ...]
    phase: str | None = None
    verdict: str | None = None
    fault: str | None = None
    raw_seq: int = 0
    link: LinkStats | None = None


def _total[**P](
    method: Callable[Concatenate[SessionRecorder, P], None],
) -> Callable[Concatenate[SessionRecorder, P], None]:
    """Make an entry point total: what it raises is logged and counted, never propagated."""

    @functools.wraps(method)
    def fenced(self: SessionRecorder, /, *args: P.args, **kwargs: P.kwargs) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception:  # an observer: a recording bug must not stop the machine
            self.note_fault(method.__name__)

    return fenced


class SessionRecorder:
    """Feeds the journal from the console's loop. Mutable, single-threaded, never awaited."""

    __slots__ = (
        "_clock",
        "_faults",
        "_journal",
        "_leg_tip",
        "_link_stats",
        "_live",
        "_notice",
        "_radius",
        "_said",
        "_sim_state",
        "_stamp",
        "_status",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        journal: Journal,
        stamp: Stamp,
        radius: Metres,
        leg_tip: Metres,
        sim_state: Callable[[], str] | None = None,
        link_stats: Callable[[], LinkStats] | None = None,
    ) -> None:
        """``sim_state``: the simulated drive's internal state, when there is a simulator.
        ``link_stats``: the acquisition link's loss counters, when it has any."""
        self._clock: Clock = clock
        self._journal: Journal = journal
        self._stamp: Stamp = stamp
        self._radius: Metres = radius
        self._leg_tip: Metres = leg_tip
        self._sim_state: Callable[[], str] | None = sim_state
        self._link_stats: Callable[[], LinkStats] | None = link_stats
        self._live: _Live | None = None
        self._status: JournalStatus = journal.status(clock.monotonic())
        self._said: tuple[Cause | None, str | None] = (None, None)
        self._notice: str | None = None
        self._faults: int = 0

    @property
    def journal(self) -> Journal:
        """The queue and its thread."""
        return self._journal

    @property
    def recording(self) -> bool:
        """Whether a session's record is open."""
        return self._live is not None

    @property
    def degraded(self) -> bool:
        """Whether the record is incomplete or cannot be written, as of the last tick."""
        return self._status.degraded or self._faults > 0

    def is_degraded(self) -> bool:
        """:attr:`degraded`, as a callable for the dashboard heartbeat."""
        return self.degraded

    @property
    def status(self) -> JournalStatus:
        """The journal's health as of the last :meth:`refresh`."""
        return self._status

    def note_fault(self, where: str) -> None:
        """An entry point raised. Called from the ``except`` of :func:`_total` only."""
        if self._faults == 0:
            # Once, with its traceback: a fault that repeats every tick must not flood the log.
            _logger.exception("session recorder: %s failed; the session is unaffected", where)
            self._notice = BROKEN
        self._faults += 1

    # --- the session's life --------------------------------------------------

    @_total
    def begin(self, request: SessionRequest) -> None:
        """The runtime has just armed a session: open its record."""
        clock = self._clock
        if self._live is not None:
            # The previous session's end was never observed; close what is open.
            self._journal.close(Closing(clock.unix_millis(), INTERRUPTED, None))
        live = _Live(origin=clock.monotonic(), names=names_of(request.operator))
        self._live = live
        self._journal.open(manifest_for(self._stamp, request, clock), _EMAILS)
        kind = "manual" if request.program is None else "auto"
        self._event(
            live,
            live.origin,
            EventKind.OPERATOR_ACTION,
            f"{kind} session started",
            operator_alias(request.operator),
        )

    @_total
    def observe(
        self, now: Monotonic, snapshot: TelemetrySnapshot, runtime: TrainingRuntime
    ) -> None:
        """One control tick: its row, what changed, and the end when the runtime is done."""
        live = self._live
        if live is None:
            return
        self._journal.submit(self._row(live, now, snapshot, runtime))
        self._note_changes(live, now, snapshot)
        if snapshot.phase is Phase.DONE:
            # The descent and its monitored recovery are over (see the module docstring).
            self._end(live, now, snapshot, runtime, None)

    @_total
    def finish(self, now: Monotonic, runtime: TrainingRuntime, detail: str) -> None:
        """The console is exiting: close the open record, if any, with how it left the drive."""
        live = self._live
        if live is not None:
            self._end(live, now, runtime.snapshot(), runtime, detail)

    def _end(
        self,
        live: _Live,
        now: Monotonic,
        snapshot: TelemetrySnapshot,
        runtime: TrainingRuntime,
        detail: str | None,
    ) -> None:
        reason = runtime.end_reason
        words = INTERRUPTED if reason is None else reason.value
        status = _fresh_status(snapshot, runtime)
        stop = runtime.stop_reason
        self._event(live, now, EventKind.END, words, SYSTEM)
        self._journal.close(
            Closing(
                ended_at=self._clock.unix_millis(),
                end_reason=words,
                observation=EndObservation(
                    t=_seconds(live, now),
                    runtime_state=runtime.state.value,
                    drive_state=snapshot.drive_state.name,
                    runtime_output_enabled=runtime.output_enabled,
                    runtime_applied_rpm=int(runtime.applied_rpm),
                    lfrd_motor_rpm=None if status is None else int(status.setpoint_echo_rpm),
                    shaft_motor_rpm=None if status is None else int(status.output_rpm),
                    # Torque presence is not something this console observes.
                    energised=None,
                    silent=runtime.silent,
                    stop_reason=None if stop is None else self._text(live, stop),
                    shutdown_detail=None if detail is None else self._text(live, detail),
                ),
            )
        )
        self._live = None

    # --- what the other tasks see --------------------------------------------

    @_total
    def note_event(self, event: SessionEvent) -> None:
        """A console event (request, refusal, acknowledgement) while a session is recorded."""
        live = self._live
        if live is None:
            return
        fresh = tuple(name for name in names_of(event.operator) if name not in live.names)
        if fresh and len(live.names) <= MAX_NAMES:
            live.names = (*live.names, *fresh)
        kind = record_kind(event.kind)
        if kind is None:
            return
        actor = operator_alias(event.operator) if event.operator.strip() else SYSTEM
        if DASHBOARD_OPERATOR in event.operator:
            actor = REMOTE
            if kind is EventKind.OPERATOR_ACTION:
                kind = EventKind.REMOTE_COMMAND
        self._event(live, event.at, kind, f"{event.kind.value}: {event.detail}", actor)

    @_total
    def note_batch(self, batch: SampleBatch) -> None:
        """One acquisition batch, as received: queued whole, with the next sequence number.

        The number is taken even when the queue refuses the batch, so a hole in
        the numbering on disk is a block this recorder lost.
        """
        live = self._live
        if live is None or not batch.channels or not batch.channels[0].values:
            return
        clock = self._clock
        first = clock.monotonic() - (clock.unix_millis() - batch.timestamp) / 1000
        seq = live.raw_seq
        live.raw_seq = seq + 1
        self._journal.submit(
            RawBatch(
                seq=seq,
                t_first=round(first - live.origin, 3),
                channels=tuple(channel.channel for channel in batch.channels),
                samples=tuple(channel.values for channel in batch.channels),
            )
        )

    @_total
    def note_link(self) -> None:
        """The acquisition link's loss counters: any increase becomes a ``warning`` event."""
        live = self._live
        read = self._link_stats
        if live is None or read is None:
            return
        stats = read()
        before = live.link
        live.link = stats
        if before is None or stats == before:
            return
        self._event(
            live,
            self._clock.monotonic(),
            EventKind.WARNING,
            "bitalino_loss:"
            f" filled_samples=+{stats.filled_samples - before.filled_samples}"
            f" dropped_backlog_samples="
            f"+{stats.dropped_backlog_samples - before.dropped_backlog_samples}"
            f" sync_losses=+{stats.sync_losses - before.sync_losses}"
            f" skipped_bytes=+{stats.skipped_bytes - before.skipped_bytes}"
            f" reconnects=+{stats.reconnects - before.reconnects}"
            f" totals={stats.filled_samples}/{stats.dropped_backlog_samples}"
            f"/{stats.sync_losses}/{stats.skipped_bytes}/{stats.reconnects}",
            SYSTEM,
        )

    @_total
    def note_sensors(self, readings: tuple[SensorReading, ...]) -> None:
        """The 1 Hz publication of every processed channel."""
        live = self._live
        if live is None:
            return
        self._journal.submit(Sensors(_seconds(live, self._clock.monotonic()), readings))

    @_total
    def refresh(self, now: Monotonic) -> None:
        """Read the journal's health. Called every tick: it runs the stall detector,
        refreshes :attr:`degraded`, and prepares a sentence when the health changed."""
        status = self._journal.status(now)
        self._status = status
        said = (status.cause, None if status.error is None else status.error.detail)
        if said != self._said:
            self._said = said
            self._notice = describe_status(status, self._journal.root)

    def take_notice(self) -> str | None:
        """The sentence for the operator since the last call, or ``None``. Read once."""
        notice = self._notice
        self._notice = None
        return notice

    # --- internals -----------------------------------------------------------

    def _text(self, live: _Live, text: str) -> str:
        if len(live.names) > MAX_NAMES:
            return WITHHELD
        return redact(text[:MAX_TEXT], live.names)

    def _event(self, live: _Live, at: Monotonic, kind: EventKind, detail: str, actor: str) -> None:
        self._journal.submit(
            Event(
                t=_seconds(live, at),
                kind=kind,
                detail=self._text(live, detail),
                actor=actor,
            )
        )

    def _note_changes(self, live: _Live, now: Monotonic, snapshot: TelemetrySnapshot) -> None:
        phase = snapshot.phase.value
        if phase != live.phase:
            live.phase = phase
            self._event(live, now, EventKind.PHASE, phase, SYSTEM)
        safety = snapshot.safety
        verdict = None if safety is None else f"{safety.rule} ({safety.action.name})"
        if verdict != live.verdict:
            live.verdict = verdict
            said = "cleared" if safety is None else f"{verdict}: {safety.detail}"
            self._event(live, now, EventKind.VERDICT, said, SYSTEM)
        report = snapshot.fault
        fault = None if report is None else f"{report.fault.mnemonic} (LFT {report.raw_code})"
        if fault != live.fault:
            live.fault = fault
            said = "cleared" if report is None else f"{fault}: {report.message}"
            self._event(live, now, EventKind.DRIVE_FAULT, said, SYSTEM)

    def _row(
        self,
        live: _Live,
        now: Monotonic,
        snapshot: TelemetrySnapshot,
        runtime: TrainingRuntime,
    ) -> Row:
        measured = snapshot.measured
        setpoint = snapshot.setpoint
        output = OutputRpm(abs(measured.output_rpm))
        setpoint_output = OutputRpm(abs(setpoint.output_rpm))
        safety = snapshot.safety
        sample = snapshot.heart_rate
        status = runtime.drive_status
        fresh = _fresh_status(snapshot, runtime)
        sim_state = self._sim_state
        return Row(
            t=elapsed(live.origin, now),
            state=runtime.state.value,
            mode=snapshot.mode.value,
            phase=snapshot.phase.value,
            drive_state=snapshot.drive_state.name,
            sim_state="" if sim_state is None else sim_state(),
            setpoint_motor_rpm=int(setpoint.motor_rpm),
            lfrd_motor_rpm=0 if status is None else int(status.setpoint_echo_rpm),
            measured_motor_rpm=int(measured.motor_rpm),
            measured_fresh=fresh is not None,
            output_rpm=float(output),
            hertz=abs(float(measured.hertz)),
            g_reference=float(output_rpm_to_g(output, self._radius)),
            g_leg_tip=float(output_rpm_to_g(output, self._leg_tip)),
            setpoint_output_rpm=float(setpoint_output),
            setpoint_g_leg_tip=float(output_rpm_to_g(setpoint_output, self._leg_tip)),
            manual_target_motor_rpm=int(runtime.manual_target),
            # The truth of a physiological model: there is none on real data.
            hr_true=None,
            hr_live=None if snapshot.live_bpm is None else int(snapshot.live_bpm),
            target_bpm=None if snapshot.target_bpm is None else int(snapshot.target_bpm),
            safety_action=snapshot.safety_action.name,
            safety_rule=None if safety is None else safety.rule,
            output_enabled=runtime.output_enabled,
            silent=runtime.silent,
            current_a=None if snapshot.current is None else float(snapshot.current),
            hr_raw=None if sample is None else sample.bpm,
            hr_confirmed=snapshot.live_bpm,
            hr_quality="no_signal" if sample is None else sample.quality.value,
            drive_status_word=None if fresh is None else int(fresh.status_word),
        )


def _seconds(live: _Live, at: Monotonic) -> float:
    """``at`` on the session's own time axis, to the millisecond like the ticks."""
    return round(elapsed(live.origin, at), 3)


def _fresh_status(snapshot: TelemetrySnapshot, runtime: TrainingRuntime) -> DriveStatus | None:
    """The drive status behind ``snapshot``, or ``None`` once it is too old to vouch for."""
    return None if snapshot.drive_status_is_stale else runtime.drive_status
