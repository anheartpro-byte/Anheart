"""The local operator console: one process, one event loop, the bench in one page.

Launched with ``python -m src.local_panel`` from ``raspberry-pi/``, natively
(Docker on macOS sees neither the FTDI cable nor the Bluetooth radio). It is the
composition root the rest of the code was written for and never had: the
training runtime, the ATV320 (or its simulator), the BITalino (or its
simulator), the ECG DSP and the operator web page, wired together on ONE
asyncio loop. It is the only entry point of this directory.

Confirmed idle is READ-ONLY
--------------------------

* Before the first session, a confirmed idle drive (REPOS) is polled at 2 Hz
  (:data:`~src.training.runtime.IDLE_POLL_PERIOD`) without writes. That case
  and an unacquired link are released on exit without the stop sequence.
* Observed enabled or turning output is stopped and latched, even before the
  first session. An acquired link with unreadable state remains unknown:
  inspection cancellation and process exit own a stop attempt, and a failed
  close never claims the output is disabled.
* The operator's E-STOP also writes at rest: it latches the shared
  supervisor at once (the web route), and on the next tick the loop forwards
  it to :meth:`~src.training.runtime.TrainingRuntime.request_estop`, which
  zeroes the speed reference. A stop is never refused.

Milestone M3: supervised MANUAL, nobody on board
------------------------------------------------

* ``/api/manual/start`` arms a manual session for a declared occupancy.
  ``bench`` (nobody on board: motor uncoupled, or arm coupled with the capsule
  empty) has the ``MOTOR_MAX_RPM`` ceiling; ``occupied`` is refused by
  configuration until M6. Programmed sessions answer 403 until M5.
* ``/api/manual/target`` sets a target in output rpm; the runtime walks the
  setpoint to it through :mod:`src.training.motion` (angular acceleration and
  g-dot limits, the domain ``{0} union [min_run, ceiling]``), and every safety
  verdict overrides it through the same ``match`` as ever.
* STOP ramps down at the motion limits; E-STOP zeroes the reference on the
  drive's own ramp. After ANY stop the target is 0 and, once the output stage
  is shown off at standstill, the mode is back to REPOS: moving again takes a
  new, explicit start.
* ``/api/drive/fault-reset`` resets a drive fault on an explicit, named click,
  only at rest, only once every verdict is acknowledged.

One supervisor
--------------

The web control surface is built on ``runtime.supervisor``, the very instance
the runtime judges every tick with - so an e-stop latched by an HTTP handler is
the runtime's own latch, synchronously, before any tick. The loop forwarding
``take_estop`` to ``request_estop`` is redundancy, not the mechanism.

The loop
--------

Three tasks on the one loop: the control tick every 0.2 s
(:meth:`LocalPanel.control_step`), the ECG bridge every 0.2 s
(:meth:`LocalPanel.ecg_step`, the DSP itself on a worker thread), and the web
server. When any of them ends - a signal, a crash, the server failing to bind -
all of them are stopped and the drive is released. Confirmed idle or
unacquired links are released without writes; acquired unknown state and
started runtimes go through
:meth:`~src.training.runtime.TrainingRuntime.shutdown`.

The session record
------------------

Every session is written to disk as it runs (``data/records/``, one directory
per session, the format of ``docs/enregistrement.md``): the local black box.
The loop only hands frozen values to a bounded in-memory queue
(:class:`~src.record.session.SessionRecorder`); ONE other thread writes and
flushes them (:class:`~src.record.journal.Journal`). No tick ever waits on the
disk, and a disk that fills up, refuses or hangs costs the record, never the
session: the operator is told, the dashboard heartbeat says so, and every
safety rule goes on as before.

See .claude/skills/anheart-strict-python/SKILL.md and the "Console locale"
section of README.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, assert_never, final

from dotenv import dotenv_values

from src.bitalino_client import BITalinoClient, LinkStats, SampleBatch
from src.bitalino_rfcomm_macos import device_factory_for
from src.clock import Clock, RealClock
from src.cloud_sync import (
    CloudSync,
    CloudTransport,
    HttpxTransport,
    SessionKind,
    SessionListener,
    StartedSession,
)
from src.contract import read_software_version
from src.control_surface import (
    LOCAL_SUBJECT,
    ControlSurface,
    EndSession,
    FaultReset,
    SessionEvent,
    SetManualTarget,
    StartManual,
    StartSession,
)
from src.ecg_pipeline import (
    BatchTap,
    EcgBridge,
    TreatFunction,
    load_treatment,
    treat_off_loop,
)
from src.local_config import (
    DEFAULT_HR_CRITICAL_BPM,
    DEFAULT_HR_HARD_MAX_BPM,
    CameraSource,
    CloudConfig,
    EcgSource,
    LocalConfig,
    MotorBackend,
    load_local_config,
)
from src.motor.atv320 import ATV320Drive, serial_master
from src.motor.drive import DriveBackend
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.panel_lifecycle import PanelTasks
from src.panel_status import EcgLinkStatus, PanelStatus
from src.presence.adapter import PRESENCE_PERIOD, PresenceAcknowledger, PresenceGuard
from src.presence.monitor import PresenceMonitor
from src.presence.simulated import SimulatedCamera
from src.presence.types import CapsuleState, RiderPosture
from src.record.export import RecordExporter
from src.record.journal import STOP_TIMEOUT, Journal
from src.record.session import SessionRecorder, SessionRequest, stamp_for, storage_gate
from src.result import Err, Ok, Result
from src.sensors.hub import SensorHub
from src.sim.bitalino import SimulatedBitalinoClient
from src.sim.physiology import Physiology
from src.sim.signals.base import SignalGenerator
from src.sim.signals.registry import generator_for
from src.telemetry import TelemetryHub
from src.training.motion import MotionLimits, load_motion_limits
from src.training.plan import (
    SHIPPED_DEFAULTS_PATH,
    ProfileStore,
    Program,
    Rejected,
    ResolveError,
    UnknownProfile,
)
from src.training.runtime import (
    AlreadyStarted,
    DriveInFault,
    DriveParameterRefused,
    DrivePrecommanded,
    DriveUnavailable,
    FaultResetRefusal,
    HeldAtStandstill,
    Holding,
    LimitsMismatch,
    ManualEnding,
    ManualTargetRefusal,
    NoFaultToReset,
    NoManualSession,
    NotAttested,
    PlanUnusable,
    RecordStorageLow,
    ResetBehindVerdict,
    ResetForbidden,
    ResetUndelivered,
    ResetWhileCommanded,
    RiseHold,
    RuntimeLimits,
    RuntimeState,
    SafetyStanding,
    ShaftStillTurning,
    StartRefusal,
    Subject,
    TargetOutOfRange,
    TrainingRuntime,
    WithdrawnTarget,
)
from src.training.safety import SELF_CLEARING, SafetyLimits
from src.training.types import Occupancy, SafetyVerdict, TelemetrySnapshot
from src.units import Bpm, Metres, Monotonic, MotorRpm, RpmPerSecond, Seconds, elapsed
from src.web.app import build_server, create_app, serve
from src.web.deps import FilesystemPortLister, Services

_logger: Final[logging.Logger] = logging.getLogger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
"""``raspberry-pi/``: where ``.env`` and ``data/`` live, whatever the working directory."""

ENV_PATH: Final[Path] = PROJECT_ROOT / ".env"
LOCAL_PROFILES_PATH: Final[Path] = PROJECT_ROOT / "data" / "profiles.local.json"
"""The console's own profile store (``data/`` is git-ignored); seeded from the shipped defaults."""

CONTROL_PERIOD: Final[Seconds] = Seconds(0.2)
"""The control tick: 5 Hz, the safety layer's own ``control_period``."""

ECG_PERIOD: Final[Seconds] = Seconds(0.2)
"""One ECG batch every 200 ms (a fifth of a second of samples)."""

ECG_RETRY: Final[Seconds] = Seconds(5.0)
"""Wait between two attempts to connect or start the BITalino."""

ECG_CONNECT_TIMEOUT_S: Final[float] = 30.0
"""Bound on one connection attempt (pairing plus the RFCOMM open)."""

ECG_SAMPLE_RATE: Final[int] = 1000
DSP_OUTPUT_RATE: Final[int] = 250
"""The DSP decimates to 250 Hz, the telemetry ring's own rate."""

PANEL_HARD_MAX_BPM: Final[Bpm] = DEFAULT_HR_HARD_MAX_BPM
PANEL_CRITICAL_BPM: Final[Bpm] = DEFAULT_HR_CRITICAL_BPM
"""The default cardiac tiers, those of ``config/profiles.default.json``. The
supervisor is built with ``LocalConfig.tiers`` (``HR_HARD_MAX_BPM`` /
``HR_CRITICAL_BPM``); a start refuses a profile that disagrees with them."""

SENSOR_PERIOD: Final[Seconds] = Seconds(1.0)
"""Every sensor window is re-processed once a second, off the loop."""

CLOUD_PERIOD: Final[Seconds] = Seconds(1.0)
"""One dashboard step a second: telemetry is sampled at 1 Hz."""

STOP_POLL: Final[Seconds] = Seconds(0.05)
STOP_POLLS: Final[int] = round(STOP_TIMEOUT / STOP_POLL)
"""At exit the console looks this often, this many times, whether the session
record's thread has finished: :data:`~src.record.journal.STOP_TIMEOUT` in all."""

RUNTIME_LIMITS: Final[RuntimeLimits] = RuntimeLimits(
    slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10)
)
"""The machine's commissioning slew (15 motor rpm/s), for a programme's control law.

A manual session moves at the motion limits instead (``config/motion_limits.json``)."""

PROGRAMS_DISABLED: Final[str] = (
    "seances programmees desactivees (PROGRAMS_ENABLED=false, jalon M5) : utiliser MANUEL"
)

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_CONFIG: Final[int] = 2

ESTOP_SOURCE: Final[str] = "console web: e-stop"

type CloudTransportFactory = Callable[[CloudConfig], CloudTransport]
"""Builds the dashboard transport from the configuration; tests pass a fake."""

LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# =========================================================================
# The ECG side
# =========================================================================


class EcgClient(Protocol):
    """What the console uses of ``BITalinoClient`` and ``SimulatedBitalinoClient``."""

    is_connected: bool
    is_acquiring: bool

    @abstractmethod
    async def connect(self, timeout: float = ...) -> bool:  # noqa: ASYNC109  # the clients' own signature
        """Open the device. Returns whether it opened."""

    @abstractmethod
    async def start_acquisition(self) -> bool:
        """Start streaming. Returns whether it started."""

    @abstractmethod
    async def read_samples(self, count: int = ...) -> SampleBatch | None:
        """The queued samples once at least ``count`` per channel are, else ``None``."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Stop acquiring if needed and close the device."""


@final
class EcgLink:
    """Keeps the BITalino connected and acquiring, and pumps the bridge when it is.

    Mutable, owned by the event loop. Nothing here retries faster than
    :data:`ECG_RETRY`, and nothing here invents a sample: a link that is down
    produces no reading, which the runtime's staleness gate then reports.
    """

    __slots__ = (
        "_address",
        "_attempts",
        "_bridge",
        "_client",
        "_clock",
        "_last_error",
        "_link_stats",
        "_retry_at",
        "_source",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        source: EcgSource,
        address: str | None,
        client: EcgClient,
        bridge: EcgBridge,
        link_stats: Callable[[], LinkStats] | None,
    ) -> None:
        self._clock: Clock = clock
        self._source: EcgSource = source
        self._address: str | None = address
        self._client: EcgClient = client
        self._bridge: EcgBridge = bridge
        self._link_stats: Callable[[], LinkStats] | None = link_stats
        self._attempts: int = 0
        self._last_error: str | None = None
        self._retry_at: Monotonic | None = None

    @property
    def client(self) -> EcgClient:
        """The acquisition client."""
        return self._client

    async def step(self) -> None:
        """Connect and start when needed (rate-limited), otherwise pump the bridge."""
        client = self._client
        if client.is_acquiring:
            await self._bridge.pump()
            return
        now = self._clock.monotonic()
        retry_at = self._retry_at
        if retry_at is not None and now < retry_at:
            return
        self._retry_at = Monotonic(now + ECG_RETRY)
        self._attempts += 1
        if not client.is_connected and not await client.connect(ECG_CONNECT_TIMEOUT_S):
            self._last_error = f"connexion au BITalino impossible ({self._address or 'sim'})"
            _logger.warning("%s", self._last_error)
            return
        if not await client.start_acquisition():
            self._last_error = "le BITalino est connecte mais l'acquisition n'a pas demarre"
            _logger.warning("%s", self._last_error)
            return
        self._last_error = None
        _logger.info("BITalino acquiring (%s)", self._source.value)

    def status(self) -> EcgLinkStatus:
        """The link as the panel shows it."""
        stats = self._link_stats
        return EcgLinkStatus(
            source=self._source,
            address=self._address,
            connected=self._client.is_connected,
            acquiring=self._client.is_acquiring,
            connect_attempts=self._attempts,
            last_error=self._last_error,
            link=None if stats is None else stats(),
            bridge=self._bridge.stats,
        )


# =========================================================================
# The session record's two taps
# =========================================================================


@final
class RecordedSink:
    """The telemetry hub, with every event also offered to the session record.

    A :class:`~src.control_surface.TelemetrySink`. Both sides are memory only
    and total: the hub's contract, and the recorder's own (it never raises).
    """

    __slots__ = ("_hub", "_recorder")

    def __init__(self, hub: TelemetryHub, recorder: SessionRecorder) -> None:
        self._hub: TelemetryHub = hub
        self._recorder: SessionRecorder = recorder

    def publish_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        """Snapshots go to the page only: the record writes its own row per tick."""
        self._hub.publish_snapshot(snapshot)

    def publish_event(self, event: SessionEvent) -> None:
        """An event goes to the page, then to the record of the session in progress."""
        self._hub.publish_event(event)
        self._recorder.note_event(event)


def recorded_tap(first: BatchTap, recorder: SessionRecorder) -> BatchTap:
    """``first``, then the session record: every batch as acquired, before any DSP."""

    def tap(batch: SampleBatch) -> None:
        first(batch)
        recorder.note_batch(batch)

    return tap


# =========================================================================
# What the panel reports
# =========================================================================


@final
class PanelReporter:
    """Builds :class:`~src.panel_status.PanelStatus` on request. Reads only."""

    __slots__ = ("_clock", "_config", "_ecg", "_runtime")

    def __init__(
        self, *, clock: Clock, config: LocalConfig, runtime: TrainingRuntime, ecg: EcgLink
    ) -> None:
        self._clock: Clock = clock
        self._config: LocalConfig = config
        self._runtime: TrainingRuntime = runtime
        self._ecg: EcgLink = ecg

    def panel_status(self) -> PanelStatus:
        """The link panel, now."""
        config = self._config
        link = config.drive_link
        return PanelStatus(
            at=self._clock.monotonic(),
            motion_enabled=True,
            programs_enabled=config.programs_enabled,
            motor_backend=config.motor_backend,
            drive_link=None if link is None else link.describe(),
            drive=self._runtime.idle_link,
            ecg=self._ecg.status(),
            heart_rate_trend=self._runtime.heart_rate_trend,
            manual_rise_hold=self._runtime.manual_rise_hold(),
            radius=config.geometry.radius,
            ratio=config.geometry.ratio,
            motor_max_rpm=config.motor_max_rpm,
        )


# =========================================================================
# The web server, behind a seam
# =========================================================================


class WebRunner(Protocol):
    """Serves the page until told to exit."""

    @abstractmethod
    async def serve(self) -> None:
        """Run the server on this loop until it is asked to exit."""

    @abstractmethod
    def request_exit(self) -> None:
        """Ask the server to finish; :meth:`serve` returns shortly after."""


@final
class UvicornRunner:
    """The production :class:`WebRunner`: the loop-safe uvicorn server."""

    __slots__ = ("_server",)

    def __init__(self, services: Services, panel_config: LocalConfig) -> None:
        web = panel_config.web
        self._server = build_server(create_app(services=services, config=web), web)

    async def serve(self) -> None:
        """Run the server on this loop."""
        await serve(self._server)

    def request_exit(self) -> None:
        """Ask uvicorn to finish; it returns from :meth:`serve` shortly after."""
        self._server.should_exit = True


# =========================================================================
# The console
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveSide:
    """The drive as built: the backend, the simulator behind it if any, the port release."""

    backend: DriveBackend
    simulator: SimulatedDrive | None
    release: Callable[[], None]
    """Close the transport WITHOUT the stop sequence: for a drive this process never wrote to."""


@final
class LocalPanel:
    """The console, wired. Build it with :func:`build_panel`."""

    __slots__ = (
        "_clock",
        "_cloud",
        "_config",
        "_drive",
        "_ecg",
        "_hub",
        "_last_measured",
        "_listener",
        "_presence",
        "_recorder",
        "_reporter",
        "_runtime",
        "_sensors",
        "_services",
        "_sim_ecg",
        "_store",
        "_surface",
        "_transport",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        config: LocalConfig,
        drive: DriveSide,
        runtime: TrainingRuntime,
        surface: ControlSurface,
        hub: TelemetryHub,
        ecg: EcgLink,
        sim_ecg: SimulatedBitalinoClient | None,
        reporter: PanelReporter,
        services: Services,
        store: ProfileStore,
        sensors: SensorHub,
        presence: PresenceGuard | None = None,
        listener: SessionListener | None = None,
        cloud: CloudSync | None = None,
        transport: HttpxTransport | None = None,
        recorder: SessionRecorder | None = None,
    ) -> None:
        self._clock: Clock = clock
        self._recorder: SessionRecorder | None = recorder
        self._config: LocalConfig = config
        self._drive: DriveSide = drive
        self._runtime: TrainingRuntime = runtime
        self._surface: ControlSurface = surface
        self._hub: TelemetryHub = hub
        self._ecg: EcgLink = ecg
        self._sim_ecg: SimulatedBitalinoClient | None = sim_ecg
        self._reporter: PanelReporter = reporter
        self._services: Services = services
        self._sensors: SensorHub = sensors
        self._presence: PresenceGuard | None = presence
        self._store: ProfileStore = store
        self._listener: SessionListener | None = listener
        self._cloud: CloudSync | None = cloud
        self._transport: HttpxTransport | None = transport
        self._last_measured: MotorRpm = MotorRpm(0)

    # --- read access, for the tests and the entry point ------------------

    @property
    def runtime(self) -> TrainingRuntime:
        """The one runtime."""
        return self._runtime

    @property
    def surface(self) -> ControlSurface:
        """The web control surface, on the runtime's own supervisor."""
        return self._surface

    @property
    def hub(self) -> TelemetryHub:
        """The telemetry hub the page's socket reads."""
        return self._hub

    @property
    def sensors(self) -> SensorHub:
        """Every acquired channel's latest processed reading."""
        return self._sensors

    @property
    def services(self) -> Services:
        """What the routes may touch."""
        return self._services

    @property
    def drive(self) -> DriveSide:
        """The drive as built."""
        return self._drive

    @property
    def ecg(self) -> EcgLink:
        """The BITalino link."""
        return self._ecg

    @property
    def reporter(self) -> PanelReporter:
        """The link panel source."""
        return self._reporter

    @property
    def cloud(self) -> CloudSync | None:
        """The dashboard link, or ``None`` when this console runs unlinked."""
        return self._cloud

    @property
    def recorder(self) -> SessionRecorder | None:
        """The session record, or ``None`` when this console was built without one."""
        return self._recorder

    # --- the two periodic steps ------------------------------------------

    async def control_step(self) -> TelemetrySnapshot:
        """One control tick, in the plan's order. Raises what the tick raises.

        1. a web e-stop is forwarded to the runtime (the supervisor is already
           latched by the route; this zeroes the reference), and the page's
           last presence ping with it;
        2. the mailbox is emptied and its command handed to the runtime; what
           the runtime refuses goes back to the page as an event;
        3. in simulation, the plant is advanced and the subject told the speed;
        4. the runtime ticks (reads only, while idle); a manual target it took
           back on that tick, because something came to hold the arm at
           standstill (a verdict, the heart rate of a person on board, or a
           first step the drive did not acknowledge), goes back to the page
           as an event, like a refusal;
        5. the tick is handed to the session record (memory only: the disk
           is another thread's), and what the record has to say to the
           operator goes to the page as an event;
        6. the snapshot is published to the surface and, through it, the hub,
           and the surface learns whether a session is running.
        """
        if self._surface.take_estop() is not None:
            self._runtime.request_estop(ESTOP_SOURCE)
        seen = self._surface.attendant_last_seen
        if seen is not None:
            # The page pings the surface; attendant_absent judges the runtime's record.
            self._runtime.note_presence(seen)
        await self._dispatch()
        now = self._clock.monotonic()
        simulator = self._drive.simulator
        if simulator is not None:
            simulator.advance(now)
        sim_ecg = self._sim_ecg
        if sim_ecg is not None:
            sim_ecg.set_motor_rpm(self._last_measured)
        snapshot = await self._runtime.tick(now)
        self._last_measured = snapshot.measured.motor_rpm
        withdrawn = self._runtime.take_withdrawn_target()
        if withdrawn is not None:
            # Nobody's command: the machine took back a target it had accepted.
            self._surface.note_refused("", describe_withdrawn_target(withdrawn))
        self._record(now, snapshot)
        self._surface.publish(snapshot)
        match self._runtime.state:
            case RuntimeState.IDLE | RuntimeState.FINISHED:
                self._surface.note_idle()
            case RuntimeState.RUNNING:
                self._surface.note_running()
            case RuntimeState.ENDING:
                pass
            case _ as unreachable:
                assert_never(unreachable)
        return snapshot

    def _record(self, now: Monotonic, snapshot: TelemetrySnapshot) -> None:
        """Hand the tick to the session record, and its news to the operator.

        Nothing here opens, writes or flushes a file, and nothing here raises:
        the recorder queues frozen values and its entry points are total.
        """
        recorder = self._recorder
        if recorder is None:
            return
        recorder.observe(now, snapshot, self._runtime)
        recorder.refresh(now)
        message = recorder.take_notice()
        if message is not None:
            self._surface.note_recording(message)

    def _begin_record(self, request: SessionRequest) -> None:
        """A session was just armed: open its record."""
        if self._recorder is not None:
            self._recorder.begin(request)

    async def _dispatch(self) -> None:
        """Hand the mailbox's command to the runtime. Exhaustive over ``Command``."""
        command = self._surface.take_command()
        runtime = self._runtime
        match command:
            case None:
                pass
            case StartSession():
                await self._start_programme(command)
            case StartManual(occupancy=occupancy, operator=operator):
                await self._start_manual(occupancy, operator)
            case SetManualTarget(output_rpm=output_rpm, operator=operator):
                target = runtime.set_manual_target(output_rpm)
                if isinstance(target, Err):
                    self._surface.note_refused(operator, describe_target_refusal(target.error))
            case FaultReset(operator=operator):
                reset = await runtime.fault_reset()
                if isinstance(reset, Err):
                    self._surface.note_refused(operator, describe_reset_refusal(reset.error))
            case EndSession(reason=reason):
                runtime.request_stop(reason)
            case _ as unreachable:
                assert_never(unreachable)

    async def _start_manual(self, occupancy: Occupancy, operator: str) -> None:
        """Resolve the occupancy's ceiling from the configuration, then arm, or say why not."""
        ceiling = self._config.ceiling_for(occupancy)
        if isinstance(ceiling, Err):
            self._surface.note_refused(operator, ceiling.error.detail)
            return
        blocked = self._presence_refusal(occupancy)
        if blocked is not None:
            self._surface.note_refused(operator, blocked)
            return
        started = await self._runtime.start_manual(occupancy, operator, ceiling.value)
        if isinstance(started, Err):
            self._surface.note_refused(operator, describe_start_refusal(started.error))
            return
        self._begin_record(SessionRequest(occupancy=occupancy, operator=operator))
        if self._listener is not None:
            self._listener.session_started(
                StartedSession(
                    kind=SessionKind.MANUAL,
                    operator=operator,
                    started_at=self._clock.unix_millis(),
                    subject_id=LOCAL_SUBJECT,
                    cloud_session_id=None,
                    occupancy=occupancy,
                )
            )

    async def _start_programme(self, command: StartSession) -> None:
        """A programmed session: every gate in order of increasing commitment, then arm.

        1. programmes enabled in this build (``PROGRAMS_ENABLED``, milestone M5),
           and a rider of known age at least ``MIN_RIDER_AGE``;
        2. a person is on board by definition - the heart rate drives the speed -
           so the OCCUPIED ceiling must exist (``OCCUPANCY_OCCUPIED_ENABLED``,
           milestone M6);
        3. the profile resolves, fitted to this rider's maximum heart rate when
           it is known, so every cardiac check re-runs against this person;
        4. its ``max_rpm`` fits under the occupied ceiling;
        5. the runtime's own gates (attestation, verdicts, tiers, the drive).

        A refusal anywhere is said on the console and, for a dashboard launch,
        sent back to the dashboard as a failed session.
        """
        operator = command.operator
        prepared = self._prepare_programme(command)
        if isinstance(prepared, Err):
            self._refuse_programme(command, prepared.error)
            return
        program = prepared.value
        subject = Subject(subject_id=command.subject_id, operator=operator)
        started = await self._runtime.start(program, subject)
        if isinstance(started, Err):
            self._refuse_programme(command, describe_start_refusal(started.error))
            return
        self._begin_record(
            SessionRequest(
                occupancy=Occupancy.OCCUPIED,
                operator=operator,
                program=program,
                subject_id=command.subject_id,
                cloud_session_id=command.cloud_session_id,
            )
        )
        if self._listener is not None:
            self._listener.session_started(
                StartedSession(
                    kind=SessionKind.AUTO,
                    operator=operator,
                    started_at=self._clock.unix_millis(),
                    subject_id=command.subject_id,
                    cloud_session_id=command.cloud_session_id,
                    profile=program.profile,
                )
            )

    def _admission_refusal(self, command: StartSession) -> str | None:
        """Gate 1, from the configuration alone: programmes enabled, rider old enough."""
        if not self._config.programs_enabled:
            return PROGRAMS_DISABLED
        return rider_age_refusal(command.subject_age, self._config.min_rider_age)

    def _prepare_programme(self, command: StartSession) -> Result[Program, str]:
        """Gates 1-4 and the camera: the programme to arm, or the sentence saying why not."""
        admission = self._admission_refusal(command)
        if admission is not None:
            return Err(admission)
        ceiling = self._config.ceiling_for(Occupancy.OCCUPIED)
        if isinstance(ceiling, Err):
            return Err(ceiling.error.detail)
        resolved = self._store.resolve(
            command.profile_id,
            at=self._clock.unix_millis(),
            total_duration_s=command.total_duration_s,
            subject_hr_max=command.subject_hr_max,
        )
        if isinstance(resolved, Err):
            return Err(describe_resolve_error(resolved.error))
        program = resolved.value
        if program.profile.max_rpm > ceiling.value:
            return Err(
                f"demarrage refuse : programme a {program.profile.max_rpm} tr/min moteur, "
                f"au-dessus du plafond personne a bord ({ceiling.value} tr/min)"
            )
        blocked = self._presence_refusal(Occupancy.OCCUPIED)
        if blocked is not None:
            return Err(blocked)
        return Ok(program)

    def _refuse_programme(self, command: StartSession, detail: str) -> None:
        self._surface.note_refused(command.operator, detail)
        remote = command.cloud_session_id
        if remote is not None and self._listener is not None:
            self._listener.start_refused(remote, detail)

    async def ecg_step(self) -> None:
        """Keep the BITalino acquiring and move its samples to the page and the runtime."""
        await self._ecg.step()
        if self._recorder is not None:
            # What the link lost and papered over since the last batch, if anything.
            self._recorder.note_link()

    @property
    def presence(self) -> PresenceGuard | None:
        """The camera fail-safe, or ``None`` when no camera is configured."""
        return self._presence

    async def presence_step(self) -> None:
        """Read the camera and apply its verdict. At 20 Hz, NOT the 5 Hz control tick:
        an intrusion must reach the drive within the confirmation window."""
        if self._presence is not None:
            self._presence.step(self._clock.monotonic())

    def _presence_refusal(self, occupancy: Occupancy) -> str | None:
        """Why the camera refuses a start declared ``occupancy``, or ``None``."""
        if self._presence is None:
            return None
        gate = self._presence.start_gate(self._clock.monotonic(), occupancy)
        return gate.error.detail if isinstance(gate, Err) else None

    async def sensor_step(self) -> None:
        """Re-process every sensor window (on a worker thread) and publish the readings."""
        readings = await self._sensors.refresh()
        if self._recorder is not None:
            self._recorder.note_sensors(readings)

    async def cloud_step(self) -> None:
        """One dashboard step. A failure here is logged and never ends the console.

        The only task allowed to swallow an exception, and deliberately so: the
        dashboard is an observer, and a bug in reporting to it must not bring a
        running session down (every other task failing stops the console, which
        ramps the machine down). The link's own calls return ``Result``; this
        fence is for what nobody predicted.
        """
        cloud = self._cloud
        if cloud is None:
            return
        try:
            await cloud.step()
        except Exception:  # see the docstring: an observer must not stop the machine
            _logger.exception("dashboard link step failed; the session is unaffected")

    # --- running and stopping --------------------------------------------

    async def run(self, stop: asyncio.Event, web: WebRunner) -> int:
        """Run until ``stop`` is set or a task ends; then stop everything. Returns an exit code."""
        if self._recorder is not None:
            self._recorder.journal.start()
        control = asyncio.create_task(self._every(CONTROL_PERIOD, self.control_step, stop))
        observers = (
            asyncio.create_task(self._every(ECG_PERIOD, self.ecg_step, stop)),
            asyncio.create_task(self._every(SENSOR_PERIOD, self.sensor_step, stop)),
            asyncio.create_task(self._every(PRESENCE_PERIOD, self.presence_step, stop)),
            asyncio.create_task(self._every(CLOUD_PERIOD, self.cloud_step, stop)),
        )
        failed = await PanelTasks(stop, web, self.close).run(control, observers)
        return EXIT_FAILED if failed else EXIT_OK

    async def _every(
        self, period: Seconds, step: Callable[[], Awaitable[object]], stop: asyncio.Event
    ) -> None:
        """Call ``step`` every ``period`` until ``stop``; the wait is on ``stop`` itself."""
        while not stop.is_set():
            began = self._clock.monotonic()
            await step()
            remaining = max(0.0, period - elapsed(began, self._clock.monotonic()))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), remaining)

    async def close(self) -> str:
        """Release the drive, then the BITalino; confirmed idle links remain read-only.

        A confirmed idle or unverified link is released without the stop
        sequence (``ATV320Drive.close`` writes LFRD = 0 and two command words).
        An acquired link with unreadable state remains unknown. It goes through
        :meth:`~src.training.runtime.TrainingRuntime.shutdown`, whose close
        waits for measured standstill before removing the run command.

        The session record is closed AFTER the drive: the stop never waits on
        a disk. Its last writes are the journal thread's; this waits for that
        thread at most :data:`~src.record.journal.STOP_TIMEOUT`, without
        borrowing a thread from anybody (see :func:`journal_stopped`).
        """
        if not self._runtime.needs_stop_before_release:
            self._drive.release()
            detail = "runtime never started: drive link released without a write"
        else:
            report = await self._runtime.shutdown("console exit")
            detail = report.detail
        recorder = self._recorder
        if recorder is not None:
            recorder.finish(self._clock.monotonic(), self._runtime, detail)
            if not await journal_stopped(recorder.journal):
                _logger.error(
                    "session record not finalised: the disk did not answer in time "
                    "(what was already written stays readable)"
                )
        await self._ecg.client.disconnect()
        if self._transport is not None:
            await self._transport.close()
        return detail


# =========================================================================
# Building it
# =========================================================================


def build_presence(
    config: LocalConfig, clock: Clock, runtime: TrainingRuntime
) -> PresenceGuard | None:
    """The camera fail-safe ``PRESENCE_SOURCE`` asks for, or ``None`` (the default).

    Only simulated cameras exist yet; a real one plugs in as another
    ``PresenceSource`` (see ``src/presence/README.md``).
    """
    camera: SimulatedCamera
    match config.camera:
        case CameraSource.NONE:
            return None
        case CameraSource.SIM_EMPTY:
            camera = SimulatedCamera(clock, capsule=CapsuleState.EMPTY)
        case CameraSource.SIM_OCCUPIED:
            camera = SimulatedCamera(
                clock,
                capsule=CapsuleState.OCCUPIED,
                posture=RiderPosture(unbuckled=False, limb_outside=False),
            )
        case _ as unreachable:
            assert_never(unreachable)
    return PresenceGuard(runtime=runtime, source=camera, monitor=PresenceMonitor(clock=clock))


def build_drive(config: LocalConfig, clock: Clock) -> DriveSide:
    """The simulator, or the ATV320 on the project's one Modbus master. Opens nothing."""
    link = config.drive_link
    match config.motor_backend:
        case MotorBackend.SIM:
            simulator = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
            return DriveSide(backend=simulator, simulator=simulator, release=_nothing)
        case MotorBackend.SERIAL:
            if link is None:
                raise ValueError("MOTOR_BACKEND=serial needs a drive link")
            master = serial_master(link.settings, clock)

            def release() -> None:
                # A read-only console must not run the stop sequence; the port
                # is simply closed. Errors are swallowed: the process is going.
                with contextlib.suppress(Exception):
                    master.close()

            drive = ATV320Drive(clock, master, link.settings, link.registers)
            return DriveSide(backend=drive, simulator=None, release=release)
        case _ as unreachable:
            assert_never(unreachable)


def _nothing() -> None:
    """The simulator has no port to release."""


@dataclass(frozen=True, slots=True, kw_only=True)
class EcgSide:
    """The acquisition client as built, and its link counters if it has any."""

    client: EcgClient
    simulator: SimulatedBitalinoClient | None
    link_stats: Callable[[], LinkStats] | None


def acquired_channels(config: LocalConfig) -> tuple[int, ...]:
    """The analog indices to acquire: every configured sensor, ascending (wire order)."""
    return tuple(sorted({kind.channel for kind in config.sensors}))


def simulated_generators(config: LocalConfig) -> dict[str, SignalGenerator]:
    """A generator for every configured non-ECG channel, by channel name."""
    generators: dict[str, SignalGenerator] = {}
    for kind in config.sensors:
        generator = generator_for(kind)
        if generator is not None:
            generators[kind.value] = generator
    return generators


def link_losses(stats: Callable[[], LinkStats]) -> Callable[[], int]:
    """Samples the real client lost and papered over: held values and dropped backlog.

    Losses a batch's timestamps cannot show, so the ECG bridge treats any
    increase as a break in the stream (no heart rate stitched across it).
    """

    def losses() -> int:
        current = stats()
        return current.filled_samples + current.dropped_backlog_samples

    return losses


def build_ecg_client(config: LocalConfig, clock: Clock) -> EcgSide:
    """The simulated BITalino, or the real client on the transport the address selects.

    Acquires every channel in ``config.sensors``; only the ECG column reaches
    the heart-rate path (``EcgBridge`` strips the rest).
    """
    address = config.bitalino_address
    channels = acquired_channels(config)
    match config.ecg_source:
        case EcgSource.SIM:
            subject = Physiology(origin=clock.monotonic(), geometry=config.geometry)
            simulated = SimulatedBitalinoClient(
                clock,
                physiology=subject,
                channels=channels,
                sample_rate=ECG_SAMPLE_RATE,
                generators=simulated_generators(config),
            )
            return EcgSide(client=simulated, simulator=simulated, link_stats=None)
        case EcgSource.SERIAL | EcgSource.RFCOMM:
            if address is None:
                raise ValueError(f"ECG_SOURCE={config.ecg_source.value} needs BITALINO_ADDRESS")
            client = BITalinoClient(
                address,
                channels=channels,
                sample_rate=ECG_SAMPLE_RATE,
                # bluetoothctl pairing is a Linux/BlueZ affair; the macOS
                # RFCOMM path pairs through the system dialog, once.
                auto_pair=config.ecg_source is EcgSource.SERIAL,
                clock=clock,
                device_factory=device_factory_for(address),
            )
            return EcgSide(client=client, simulator=None, link_stats=client.link_stats)
        case _ as unreachable:
            assert_never(unreachable)


def load_panel_motion_limits(config: LocalConfig) -> MotionLimits:
    """The motion limits file, relative to ``raspberry-pi/``. Raises ``ValueError`` when unusable.

    Refusing to start beats starting with limits nobody signed: every manual
    ramp is walked through these numbers.
    """
    path = config.motion_limits_path
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    loaded = load_motion_limits(resolved)
    if isinstance(loaded, Ok):
        return loaded.value
    raise ValueError(f"the motion limits cannot be used: {loaded.error}")


def build_panel(
    config: LocalConfig,
    *,
    clock: Clock,
    profiles_path: Path = LOCAL_PROFILES_PATH,
    defaults_path: Path = SHIPPED_DEFAULTS_PATH,
    treat: TreatFunction = treat_off_loop,
    drive: DriveSide | None = None,
    transport: CloudTransportFactory | None = None,
    journal: Journal | None = None,
) -> LocalPanel:
    """Wire everything, in the plan's order. Opens no port and starts no task.

    Raises ``ValueError`` when the shipped profile defaults are unusable: the
    installation is broken, and a console that cannot even list a profile is
    better refused at startup than discovered half-working.

    ``drive`` replaces the configured drive; for tests that need a backend
    which is neither the simulator nor copper. ``transport`` replaces the
    dashboard link's HTTP transport, for the same reason; it is used only when
    ``config.cloud`` is set.

    ``journal`` is the session record's queue and thread (:func:`open_journal`
    in production). ``None``: nothing is recorded and no arming is refused for
    lack of disk space, which is what a test that is not about the record
    wants. Its thread is started by :meth:`LocalPanel.run`, not here.
    """
    motion = load_panel_motion_limits(config)
    drive = build_drive(config, clock) if drive is None else drive
    safety = SafetyLimits(
        hard_max_bpm=config.tiers.hard_max_bpm, critical_bpm=config.tiers.critical_bpm
    )
    runtime = TrainingRuntime(
        clock=clock,
        drive=drive.backend,
        geometry=config.geometry,
        limits=RUNTIME_LIMITS,
        safety=safety,
        motion=motion,
        limit_radius=config.leg_tip_radius,
        arming_gate=None if journal is None else storage_gate(journal, clock),
    )
    hub = TelemetryHub(clock=clock)
    ecg_side = build_ecg_client(config, clock)
    recorder = (
        None
        if journal is None
        else build_recorder(
            config,
            clock=clock,
            journal=journal,
            safety=safety,
            motion=motion,
            drive=drive,
            ecg=ecg_side,
        )
    )
    # ONE supervisor: the surface latches the runtime's own. And the runtime
    # acknowledges, so its own latches clear with the supervisor's.
    presence = build_presence(config, clock, runtime)
    surface = ControlSurface(
        clock=clock,
        supervisor=runtime.supervisor,
        sink=hub if recorder is None else RecordedSink(hub, recorder),
        # With a camera, an acknowledgement clears the presence latch too.
        acknowledger=runtime if presence is None else PresenceAcknowledger(runtime, presence),
    )
    store = ProfileStore(profiles_path, defaults_path)
    match store.load():
        case Err(error):
            raise ValueError(f"the profile store cannot start: {error}")
        case _:
            pass

    sensor_hub = SensorHub(clock=clock, kinds=config.sensors, fs=ECG_SAMPLE_RATE)
    bridge = EcgBridge(
        clock=clock,
        source=ecg_side.client,
        treatment=load_treatment(ECG_SAMPLE_RATE, DSP_OUTPUT_RATE),
        heart_rate=runtime,
        waveform=hub,
        sample_rate=ECG_SAMPLE_RATE,
        treat=treat,
        tap=sensor_hub.accept if recorder is None else recorded_tap(sensor_hub.accept, recorder),
        link_gaps=None if ecg_side.link_stats is None else link_losses(ecg_side.link_stats),
    )
    ecg = EcgLink(
        clock=clock,
        source=config.ecg_source,
        address=config.bitalino_address,
        client=ecg_side.client,
        bridge=bridge,
        link_stats=ecg_side.link_stats,
    )
    reporter = PanelReporter(clock=clock, config=config, runtime=runtime, ecg=ecg)
    services = Services(
        clock=clock,
        surface=surface,
        hub=hub,
        supervisor=runtime.supervisor,
        store=store,
        ports=FilesystemPortLister(),
        geometry=config.geometry,
        motion_enabled=True,
        panel=reporter,
        programs_enabled=config.programs_enabled,
        ceilings=config.ceiling_for,
        sensors=sensor_hub,
        presence=presence,
        camera=config.camera.value,
        records=None if journal is None else RecordExporter(journal.root, clock),
    )
    cloud: CloudSync | None = None
    owned: HttpxTransport | None = None
    if config.cloud is not None:
        if transport is None:
            owned = HttpxTransport(config.cloud)
            link: CloudTransport = owned
        else:
            link = transport(config.cloud)
        cloud = CloudSync(
            clock=clock,
            transport=link,
            runtime=runtime,
            surface=surface,
            store=store,
            tiers=config.tiers,
            programs_enabled=config.programs_enabled,
            software_version=read_software_version(),
            record_degraded=None if recorder is None else recorder.is_degraded,
        )
    return LocalPanel(
        clock=clock,
        config=config,
        drive=drive,
        runtime=runtime,
        surface=surface,
        hub=hub,
        ecg=ecg,
        sim_ecg=ecg_side.simulator,
        reporter=reporter,
        services=services,
        store=store,
        sensors=sensor_hub,
        presence=presence,
        listener=cloud,
        cloud=cloud,
        transport=owned,
        recorder=recorder,
    )


async def journal_stopped(journal: Journal) -> bool:
    """Ask the journal thread to finish and wait for it, at most ``STOP_TIMEOUT``.

    No executor: the wait is this coroutine's own, a short sleep at a time.
    The loop's thread pool is shared with the ECG treatment and may be full of
    work that will never return; a console that needed one of its threads to
    stop would then never stop.
    """
    journal.request_stop()
    for _ in range(STOP_POLLS):
        if journal.stopped:
            break
        await asyncio.sleep(STOP_POLL)
    return journal.stopped


def build_recorder(
    config: LocalConfig,
    *,
    clock: Clock,
    journal: Journal,
    safety: SafetyLimits,
    motion: MotionLimits,
    drive: DriveSide,
    ecg: EcgSide,
) -> SessionRecorder:
    """The session record's loop side, stamped with the configuration actually applied."""
    simulator = drive.simulator
    radius = config.geometry.radius
    leg_tip = config.leg_tip_radius
    return SessionRecorder(
        clock=clock,
        journal=journal,
        stamp=stamp_for(config, RUNTIME_LIMITS, safety, motion),
        radius=radius,
        leg_tip=radius if leg_tip is None else Metres(max(float(leg_tip), float(radius))),
        sim_state=None if simulator is None else _sim_state_of(simulator),
        link_stats=ecg.link_stats,
    )


def _sim_state_of(simulator: SimulatedDrive) -> Callable[[], str]:
    def read() -> str:
        return simulator.sim_state.name

    return read


def open_journal(config: LocalConfig, clock: Clock) -> Journal:
    """The session record's queue and thread, on ``RECORD_ROOT`` (under ``raspberry-pi/``).

    Creates the directory, private to the service user, and measures its free
    space: startup I/O, with nothing turning. The thread is not started here.
    """
    record = config.record
    root = record.root if record.root.is_absolute() else PROJECT_ROOT / record.root
    return Journal(root, clock, retention_days=record.retention_days)


# =========================================================================
# Refusals, in words for the operator's event list
# =========================================================================


def describe_start_refusal(refusal: StartRefusal) -> str:
    """One line for a refused start. Exhaustive over the runtime's closed union.

    A standing verdict is quoted without the sentence an unlatched one carries
    on the live screen (:data:`~src.training.safety.SELF_CLEARING`): that
    sentence is about a session that is running, and next to "start refused"
    it only blurs why.
    """
    reason: str
    match refusal:
        case AlreadyStarted(state=state):
            reason = f"la machine est deja {state.value}"
        case NotAttested():
            reason = "cablage de l'arret d'urgence non atteste"
        case SafetyStanding(verdict=verdict):
            said = verdict.detail.removesuffix(SELF_CLEARING)
            reason = f"verdict {verdict.rule} a acquitter ({said})"
        case LimitsMismatch(threshold=threshold):
            reason = f"seuil {threshold} different de celui du superviseur"
        case PlanUnusable(detail=detail) | DriveUnavailable(detail=detail):
            reason = detail
        case DriveParameterRefused():
            reason = refusal.detail
        case DrivePrecommanded(output_rpm=rpm):
            reason = f"variateur deja en marche ({rpm} tr/min), arret demande"
        case DriveInFault(report=report):
            code = "?" if report is None else f"{report.fault.mnemonic}, LFT {report.raw_code}"
            reason = f"variateur en defaut ({code})"
        case RecordStorageLow():
            reason = describe_record_storage(refusal)
        case _ as unreachable:
            assert_never(unreachable)
    return f"demarrage refuse : {reason}"


def describe_record_storage(refusal: RecordStorageLow) -> str:
    """Why the session record refuses an arming, with the numbers and the directory."""
    megabyte = 1_000_000
    where = refusal.where
    free = refusal.free_bytes
    if refusal.stale_for is not None:
        return (
            f"enregistrement de seance impossible, espace libre sous {where} mesure il y a "
            f"{refusal.stale_for:.0f} s : le disque ne repond plus"
        )
    if free is None:
        return (
            f"enregistrement de seance impossible, espace libre illisible sous {where} "
            "(dossier absent, droits, disque)"
        )
    return (
        f"espace disque insuffisant pour l'enregistrement de seance : {free // megabyte} Mo "
        f"libres sous {where}, {refusal.required_bytes // megabyte} Mo requis. Liberer de l'espace"
    )


def rider_age_refusal(age: int | None, minimum: int) -> str | None:
    """Why a programmed session refuses this rider's age, or ``None`` when it is acceptable.

    Unknown is a refusal, not a pass: the simulation battery found a 10-year-old
    accepted on the adult ceiling because nothing asked. The age is declared at
    the console or carried by the dashboard launch (from the rider's birth year).
    """
    if age is None:
        return "demarrage refuse : age du passager requis pour une seance programmee"
    if age < minimum:
        return f"demarrage refuse : passager de {age} ans, minimum {minimum} ans (MIN_RIDER_AGE)"
    return None


def describe_resolve_error(error: ResolveError) -> str:
    """One line for a programme that could not be resolved. Exhaustive."""
    match error:
        case UnknownProfile(profile_id=profile_id):
            return f"demarrage refuse : programme {profile_id!r} inconnu sur cette machine"
        case Rejected(detail=detail):
            return f"demarrage refuse : programme inadapte a ce passager ({detail})"
        case _ as unreachable:
            assert_never(unreachable)


def describe_target_refusal(refusal: ManualTargetRefusal) -> str:
    """One line for a refused manual target."""
    match refusal:
        case NoManualSession(state=state):
            return f"consigne refusee : pas de session manuelle ({state.value})"
        case ManualEnding(detail=detail):
            return f"consigne refusee : {detail}"
        case TargetOutOfRange(requested=requested, min_run=low, ceiling=high):
            return (
                f"consigne refusee : {requested:.2f} tr/min de sortie hors de 0 ou "
                f"[{low}, {high}] tr/min moteur"
            )
        case HeldAtStandstill(by=by):
            return f"consigne refusee : {_held_by(by)}, puis redonner la cible"
        case _ as unreachable:
            assert_never(unreachable)


def describe_withdrawn_target(withdrawn: WithdrawnTarget) -> str:
    """One line for a manual target the runtime took back (see ``WithdrawnTarget``)."""
    return (
        f"cible de {withdrawn.target} tr/min moteur remise a 0 : {_held_by(withdrawn.by)}, "
        "puis redonner la cible"
    )


def _held_by(by: Holding) -> str:
    """What holds the arm at standstill, then what has to happen before a target is taken again.

    Exhaustive over a verdict and every :class:`~src.training.runtime.RiseHold`:
    a hold added later has no words until somebody writes them here.
    """
    match by:
        case SafetyVerdict(rule=rule, latched=latched):
            wait = "L'acquitter une fois sa cause levee" if latched else "Attendre qu'il soit leve"
            return f"le verdict {rule} tient le bras a l'arret. {wait}"
        case RiseHold.NO_HEART_RATE:
            return (
                "pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. "
                "Attendre une frequence cardiaque fiable"
            )
        case RiseHold.TREND_UNKNOWN:
            return (
                "tendance de la frequence cardiaque pas encore connue, rien ne monte depuis "
                "l'arret. Attendre quelques secondes de lecture"
            )
        case RiseHold.HEART_RATE_FALLING:
            return (
                "la frequence cardiaque baisse trop vite, rien ne monte depuis l'arret. "
                "Attendre qu'elle se stabilise"
            )
        case RiseHold.WRITE_UNACKNOWLEDGED:
            # Not "the arm stays stopped": when the frame landed and only its
            # answer was lost, the drive holds the step until the next tick's
            # keepalive writes zero again, and nothing here can tell which.
            return (
                "le variateur n'a pas confirme la consigne, elle n'est pas redemandee. "
                "Verifier la liaison"
            )
        case _ as unreachable:
            assert_never(unreachable)


def describe_reset_refusal(refusal: FaultResetRefusal) -> str:
    """One line for a refused fault reset."""
    match refusal:
        case ResetWhileCommanded(state=state, phase=phase):
            return f"reset refuse : mouvement encore commande ({state.value}, {phase.value})"
        case ResetBehindVerdict(verdict=verdict):
            return f"reset refuse : acquitter d'abord le verdict {verdict.rule}"
        case NoFaultToReset(state=state):
            seen = "variateur non lu" if state is None else state.name.lower()
            return f"reset refuse : aucun defaut a acquitter ({seen})"
        case ShaftStillTurning(output_rpm=rpm):
            return f"reset refuse : l'arbre tourne encore ({rpm} tr/min moteur)"
        case ResetForbidden(report=report):
            named = "inconnu (LFT non lu)" if report is None else report.fault.mnemonic
            return (
                f"reset refuse : defaut {named} non rearmable depuis la console : couper "
                "l'alimentation du variateur et inspecter"
            )
        case ResetUndelivered(detail=detail):
            return f"reset refuse : {detail}"
        case _ as unreachable:
            assert_never(unreachable)


# =========================================================================
# Entry point
# =========================================================================


def load_environment(path: Path = ENV_PATH) -> dict[str, str]:
    """``.env`` values, overridden by the process environment (as ``load_dotenv`` does)."""
    merged = {key: value for key, value in dotenv_values(path).items() if value is not None}
    merged.update(os.environ)
    return merged


def install_stop_signals(stop: asyncio.Event) -> Callable[[], None]:
    """Route SIGINT and SIGTERM to ``stop``; return the undo. A no-op where unsupported.

    The console's shutdown is the one that releases the drive properly, so it
    owns the signals (the web server is built not to take them).
    """
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)

    def undo() -> None:
        for sig in installed:
            loop.remove_signal_handler(sig)

    return undo


async def run_console(config: LocalConfig, *, stop: asyncio.Event | None = None) -> int:
    """Build the console on the real clock and run it until a signal."""
    event = asyncio.Event() if stop is None else stop
    clock = RealClock()
    panel = build_panel(config, clock=clock, journal=open_journal(config, clock))
    undo = install_stop_signals(event)
    try:
        return await panel.run(event, UvicornRunner(panel.services, config))
    finally:
        undo()


def describe_config(config: LocalConfig) -> str:
    """One banner line: where the console listens and what it is wired to."""
    web = config.web
    link = config.drive_link
    drive = "simulateur" if link is None else link.describe()
    ecg = config.bitalino_address or "simulateur"
    modes = "MANUEL + PROGRAMMES" if config.programs_enabled else "MANUEL BANC"
    tiers = config.tiers
    dashboard = "aucun" if config.cloud is None else config.cloud.url
    return (
        f"Console du banc sur http://{web.host}:{web.port}/ - {modes} "
        f"(plafond {config.motor_max_rpm} tr/min moteur; paliers "
        f"{tiers.hard_max_bpm}/{tiers.critical_bpm} bpm) - "
        f"variateur: {drive} - ECG ({config.ecg_source.value}): {ecg} - "
        f"rayon {config.geometry.radius} m, i = {config.geometry.ratio} - "
        f"tableau de bord: {dashboard}"
    )


def main(environ: Mapping[str, str] | None = None) -> int:
    """``python -m src.local_panel``. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    env = load_environment() if environ is None else environ
    loaded = load_local_config(env)
    if isinstance(loaded, Err):
        for problem in loaded.error:
            _logger.error("configuration: %s: %s", problem.key, problem.detail)
        return EXIT_CONFIG
    config = loaded.value
    _logger.warning("%s", describe_config(config))
    return asyncio.run(run_console(config))


if __name__ == "__main__":
    raise SystemExit(main())
