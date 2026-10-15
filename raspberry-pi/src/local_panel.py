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

The drive's frames reach that same thread without passing through the loop:
the native driver reports its own exchanges into a bounded list, and a backend
that reports none (the simulator) is wrapped by a tap that notes each call
(:mod:`src.record.drive_tap`). What is asked at the console while no session
is recorded goes to the logbook (:mod:`src.record.logbook`).

The dashboard is told from the record
-------------------------------------

What the dashboard receives of a session (telemetry, events, end) is read back
from that session's record, from the place the dashboard last acknowledged
(:mod:`src.record_uplink`): a lost link, or a console that restarts, loses
nothing of a session that is on disk. Those reads have a thread of their own
(:func:`record_source`), and the dashboard task that asks for them is cancelled
first at exit: neither a tick nor the stop of the drive ever waits for them.

A stop asked for from the dashboard is looked for by another task
(:meth:`LocalPanel.cloud_stop_step`), which does nothing else: the question
keeps its cadence while the sending waits on a slow disk or a slow upload.

The wall clock dates, and decides nothing
-----------------------------------------

Whether a time service keeps the wall clock is asked by a task of its own
(:meth:`LocalPanel.clock_step`, :mod:`src.wall_clock`), never by the control
tick, which reads the monotonic clock and nothing else. The last answer is
shown on the page and written in each record's manifest
(``clocks.wall_trusted``). On a clock nobody answers for, a bench session
starts and the operator is told; a start with a person on board is refused
before the runtime is asked anything.

Under a service manager
-----------------------

On an installed Pi systemd starts this process, expects to hear from it, and
restarts it when it dies or stops answering (``scripts/anheart.service``).
Three things here are for that, and none of them can start a session:

* the control task tells the service manager it is alive on the line after
  each tick that returned (:meth:`LocalPanel.control_cycle`,
  :mod:`src.service_watchdog`), and from nowhere else;
* a console asked to stop while its control loop was completing no tick does
  not report a clean exit (:meth:`LocalPanel.close`);
* a console that keeps restarting comes up out of service: it reads the drive,
  stops and latches one it finds running, shows why, and arms nothing until a
  named operator puts it back in service (:mod:`src.service_state`,
  :class:`ServiceDesk`).

A restarted console is a new process: no attestation, no session, and a drive
it finds enabled or turning is zeroed and latched. That was true before any of
this and stays the rule.

See .claude/skills/anheart-strict-python/SKILL.md and the "Console locale"
section of README.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import signal
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event as ThreadEvent
from threading import Thread
from typing import Final, Protocol, assert_never, final

from dotenv import dotenv_values

from src.bitalino_client import BITalinoClient, LinkStats, SampleBatch
from src.bitalino_rfcomm_macos import device_factory_for
from src.clock import Clock, RealClock
from src.cloud_sync import (
    CloudSync,
    CloudTransport,
    DepositTask,
    HttpxTransport,
    PacedTransport,
    SessionKind,
    SessionListener,
    StartedSession,
    end_is_failure,
)
from src.contract import SoftwareVersion, read_software_version
from src.control_surface import (
    LOCAL_SUBJECT,
    Command,
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
from src.health import HealthContext, HealthWatch
from src.link_state import NOT_CONFIGURED, LinkHealth
from src.local_config import (
    DEFAULT_HR_CRITICAL_BPM,
    DEFAULT_HR_HARD_MAX_BPM,
    KEY_UI_TLS_CERT,
    KEY_UI_TLS_KEY,
    RETIRED_KEYS,
    CameraSource,
    CloudConfig,
    ConfigProblem,
    EcgSource,
    LocalConfig,
    MotorBackend,
    load_local_config,
    retired_keys,
)
from src.log_format import json_handler
from src.motor.atv320 import ATV320Drive, serial_master
from src.motor.drive import DriveBackend
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.panel_lifecycle import PanelTasks, WebRunner
from src.panel_status import EcgLinkStatus, PanelStatus
from src.presence.adapter import PRESENCE_PERIOD, PresenceAcknowledger, PresenceGuard
from src.presence.monitor import PresenceMonitor
from src.presence.simulated import SimulatedCamera
from src.presence.types import CapsuleState, RiderPosture
from src.record.cursor import boot_identity
from src.record.deposit import RecordDeposit
from src.record.drive_tap import tap_drive
from src.record.export import RecordExporter, RecordIo, listing
from src.record.journal import STOP_TIMEOUT, Activity, Journal
from src.record.logbook import note
from src.record.schema import EventKind
from src.record.session import (
    SYSTEM,
    RecordInodesLow,
    RecordJournalBusy,
    SessionRecorder,
    SessionRequest,
    operator_alias,
    stamp_for,
    storage_gate,
)
from src.record_uplink import RecordSource
from src.result import Err, Ok, Result
from src.roster import (
    OnBench,
    PassengerRefusal,
    Rider,
    Roster,
    age_of,
    describe_passenger_refusal,
    passenger_of,
)
from src.sensors.hub import SensorHub
from src.service_state import (
    IN_SERVICE,
    THRESHOLD,
    WINDOW,
    NotOutOfService,
    ServiceGuard,
    ServiceStatus,
    Stored,
    store,
)
from src.service_watchdog import NOTIFY_SOCKET, ServiceWatchdog
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
from src.wall_clock import (
    WallClockProbe,
    WallClockState,
    WallClockWatch,
    clock_offset_s,
    detail_of,
    is_trusted,
    system_probe,
)
from src.web.app import build_server, create_app, serve
from src.web.deps import FilesystemPortLister, Services
from src.web.tls import server_context

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
"""One dashboard step a second. What it sends of a session is read back from its record."""

STOP_WATCH_PERIOD: Final[Seconds] = Seconds(0.05)
"""How often the stop watch looks whether its question is due. The question itself is
asked every :data:`~src.cloud_sync.STATUS_PERIOD`; this only bounds how late it can be,
and a look that finds nothing due reads a clock and returns."""

WALL_CLOCK_PERIOD: Final[Seconds] = Seconds(10.0)
"""How often the console asks what the wall clock is worth: a state that changes when
a network comes back or goes, not within a second. See :meth:`LocalPanel.clock_step`."""

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


@dataclass(frozen=True, slots=True)
class RiderFacts:
    """What a programmed start is vetted on, and where it came from.

    For a launch from the dashboard, what the launch carries. For a start at
    the console, what the riders' list holds for the rider named, read when
    the loop arms it: nothing typed at the console counts.
    """

    hr_max: Bpm | None
    age: int | None
    label: str | None
    """The list's name of the rider, for the dashboard; ``None`` for a launch."""


@dataclass(frozen=True, slots=True)
class Armable:
    """A programme every gate before the runtime's has passed, and who it is for."""

    program: Program
    rider: RiderFacts


EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_CONFIG: Final[int] = 2

EXIT_GRACE: Final[Seconds] = Seconds(5.0)
"""Once the console has stopped (the drive first, then the session record), the
process has this long to leave by itself. See :func:`bound_exit`."""

ESTOP_SOURCE: Final[str] = "console web: e-stop"

SERVICE_SAVE_TIMEOUT: Final[Seconds] = Seconds(5.0)
"""How long putting the console back in service waits for the disk to take it. Past
that the operator is told it holds until the next restart only."""

OUT_OF_SERVICE: Final[str] = (
    f"console hors service : {THRESHOLD} demarrages apres un arret non propre en moins de "
    f"{WINDOW / 60:.0f} min. Faire verifier la machine, puis la remettre en service depuis "
    "cette page, par un operateur nomme"
)
"""Why a console out of service arms nothing, and what lifts it."""

type CloudTransportFactory = Callable[[CloudConfig], CloudTransport]
"""Builds the dashboard transport from the configuration; tests pass a fake."""


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

    __slots__ = (
        "_clock",
        "_config",
        "_dashboard",
        "_ecg",
        "_runtime",
        "_service",
        "_software_version",
        "_wall_clock",
        "_watchdog",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        config: LocalConfig,
        runtime: TrainingRuntime,
        ecg: EcgLink,
        software_version: SoftwareVersion,
        dashboard: LinkHealth | None,
        watchdog: ServiceWatchdog,
        service: ServiceDesk | None = None,
        wall_clock: WallClockWatch | None = None,
    ) -> None:
        """``software_version``: what this build calls itself, read once at startup.
        ``dashboard``: what the dashboard link last heard of itself, which that link
        alone writes; ``None`` on a console that has no machine key.
        ``wall_clock``: the last reading of the wall clock's standing; ``None`` on a
        console built without a probe.
        ``watchdog``: what the control task tells the service manager.
        ``service``: the restart state of this console; ``None`` when it keeps none."""
        self._clock: Clock = clock
        self._config: LocalConfig = config
        self._runtime: TrainingRuntime = runtime
        self._ecg: EcgLink = ecg
        self._software_version: SoftwareVersion = software_version
        self._dashboard: LinkHealth | None = dashboard
        self._wall_clock: WallClockWatch | None = wall_clock
        self._watchdog: ServiceWatchdog = watchdog
        self._service: ServiceDesk | None = service

    def panel_status(self) -> PanelStatus:
        """The link panel, now."""
        config = self._config
        link = config.drive_link
        now = self._clock.monotonic()
        dashboard = self._dashboard
        wall_clock = self._wall_clock
        service = self._service
        return PanelStatus(
            at=now,
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
            software_version=self._software_version,
            dashboard=NOT_CONFIGURED if dashboard is None else dashboard.status(now),
            wall_clock=None if wall_clock is None else wall_clock.current(),
            service=IN_SERVICE if service is None else service.status(),
            watchdog=self._watchdog.status(),
        )


# =========================================================================
# In service, or not: the restart state as the loop and the page use it
# =========================================================================


@final
class ServiceDesk:
    """The console's restart state (:mod:`src.service_state`), wired to the logbook.

    Mutable through its guard, owned by the event loop thread. Nothing here
    touches the disk on that thread: what is said goes to the journal's queue,
    and the two writes a running console makes (:meth:`record_start`, once,
    after the first tick; :meth:`restore`) have a thread of their own, waited
    for a bounded time.
    """

    __slots__ = ("_clock", "_guard", "_io", "_journal")

    def __init__(self, guard: ServiceGuard, journal: Journal, clock: Clock) -> None:
        """``guard`` has already begun: its state is this run's."""
        self._guard: ServiceGuard = guard
        self._journal: Journal = journal
        self._clock: Clock = clock
        self._io: RecordIo = RecordIo(1)

    @property
    def guard(self) -> ServiceGuard:
        """The state itself, and its file."""
        return self._guard

    def status(self) -> ServiceStatus:
        """In service or not, and what this start found. Memory only."""
        return self._guard.status

    def refusal(self) -> str | None:
        """Why nothing may be armed, in words for the operator; ``None`` when in service."""
        return OUT_OF_SERVICE if self._guard.status.out_of_service else None

    def announce(self) -> None:
        """Write what this start found: to the console's journal and to the logbook.

        Called once, at startup. Nothing is written for a start that follows
        a clean stop of a console in service.
        """
        status = self._guard.status
        if status.unclean_stop:
            record = status.open_record
            left = "" if record is None else f" : enregistrement de seance {record} non clos"
            self._say(EventKind.WARNING, f"unclean_stop: demarrage apres arret non propre{left}")
        if status.out_of_service:
            self._say(EventKind.WARNING, f"out_of_service: {OUT_OF_SERVICE}")

    async def record_start(self) -> None:
        """Write this start for the next process to find. Off the loop, bounded, total.

        Called once, after the first control tick: the drive has been read by
        then, and this is the first time this start touches the disk for
        writing. A disk that refuses or does not answer costs the count, never
        the console: the status says the state is not kept.
        """
        await self._write(self._guard.stored)

    async def restore(self, operator: str) -> Result[ServiceStatus, NotOutOfService]:
        """A named operator puts the console back in service. In effect at once.

        The state is then written, off the loop, for at most
        :data:`SERVICE_SAVE_TIMEOUT`. A disk that refuses or does not answer
        changes nothing now: the status says the state is not kept, and the
        console comes up out of service again after its next restart.
        """
        restored = self._guard.restore()
        if isinstance(restored, Err):
            return Err(restored.error)
        self._say(
            EventKind.OPERATOR_ACTION,
            "service_restored: console remise en service",
            operator_alias(operator),
        )
        await self._write(restored.value)
        return Ok(self._guard.status)

    async def _write(self, stored: Stored) -> None:
        """Hand ``stored`` to the disk on this desk's own thread, and note what it answered."""
        write = functools.partial(store, self._guard.path, stored)
        written = await self._io.run(write, SERVICE_SAVE_TIMEOUT)
        self._guard.saved(written.value if isinstance(written, Ok) else Err(written.error.detail))

    def _say(self, kind: EventKind, detail: str, actor: str = SYSTEM) -> None:
        """One line in the console's journal and one in the logbook. Memory only."""
        _logger.warning("%s", detail)
        clock = self._clock
        self._journal.log(
            note(
                wall_clock=clock.unix_millis(),
                at=clock.monotonic(),
                kind=kind,
                detail=detail,
                actor=actor,
            )
        )


# =========================================================================
# The web server, behind a seam (:class:`~src.panel_lifecycle.WebRunner`)
# =========================================================================


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
        "_health",
        "_hub",
        "_last_measured",
        "_listener",
        "_presence",
        "_recorder",
        "_reporter",
        "_roster",
        "_runtime",
        "_sensors",
        "_service",
        "_services",
        "_sim_ecg",
        "_stalled_at_stop",
        "_store",
        "_surface",
        "_ticked",
        "_transport",
        "_wall_clock",
        "_watchdog",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        config: LocalConfig,
        drive: DriveSide,
        runtime: TrainingRuntime,
        watchdog: ServiceWatchdog,
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
        wall_clock: WallClockWatch | None = None,
        service: ServiceDesk | None = None,
        health: HealthWatch | None = None,
        roster: Roster | None = None,
    ) -> None:
        self._clock: Clock = clock
        # The riders' list: the link writes it, the routes and the starts read it.
        self._roster: Roster = Roster(linked=False) if roster is None else roster
        self._watchdog: ServiceWatchdog = watchdog
        self._health: HealthWatch | None = health
        self._service: ServiceDesk | None = service
        self._stalled_at_stop: bool = False
        # Set once the control loop has completed a tick, so read the drive.
        self._ticked: asyncio.Event = asyncio.Event()
        self._recorder: SessionRecorder | None = recorder
        self._wall_clock: WallClockWatch | None = wall_clock
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
    def roster(self) -> Roster:
        """The riders' list a start with a person on board names one of."""
        return self._roster

    @property
    def recorder(self) -> SessionRecorder | None:
        """The session record, or ``None`` when this console was built without one."""
        return self._recorder

    @property
    def watchdog(self) -> ServiceWatchdog:
        """What the control task tells the service manager."""
        return self._watchdog

    @property
    def service(self) -> ServiceDesk | None:
        """The restart state of this console, or ``None`` when it keeps none."""
        return self._service

    # --- the two periodic steps ------------------------------------------

    async def control_cycle(self) -> None:
        """What the control task repeats: one tick, then the service manager is told.

        The second line is reached only when the first returned. A tick that
        raises, that is cancelled or that never comes back tells the service
        manager nothing, and that silence is what gets a frozen console
        restarted (:mod:`src.service_watchdog`). No other line of the console
        sends that notification: not a timer, not another task, not a thread.

        A tick that returned is all it says. A console that has latched a
        verdict, gone silent on the drive or is out of service still ticks,
        and is still told alive: restarting it is an operator's decision.

        The first tick that returns also lets this start be written to the
        disk (:meth:`_record_start`): not before, the drive is read first.
        """
        await self.control_step()
        self._watchdog.tick_completed(self._clock.monotonic())
        self._ticked.set()

    async def control_step(self) -> TelemetrySnapshot:
        """One control tick, in the plan's order. Raises what the tick raises.

        1. a web e-stop is forwarded to the runtime (the supervisor is already
           latched by the route; this zeroes the reference), and the page's
           last presence ping with it;
        2. the mailbox is emptied and its command handed to the runtime; what
           the runtime made of it, taken or refused, goes back to the page as
           an event that carries the request's identifier;
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

        The drive is awaited in steps 2 and 4, and the web server answers
        requests meanwhile: a request can be accepted into the mailbox while a
        tick is under way. It is taken by the NEXT tick, and the surface keeps
        its place until then (:meth:`~src.control_surface.ControlSurface.note_idle`).
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
        if self._health is not None:
            reason = self._runtime.end_reason
            self._health.observe(snapshot, None if reason is None else end_is_failure(reason))
        self._last_measured = snapshot.measured.motor_rpm
        withdrawn = self._runtime.take_withdrawn_target()
        if withdrawn is not None:
            # Nobody's command: the machine took back a target it had accepted.
            self._surface.note_withdrawn(describe_withdrawn_target(withdrawn))
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
        if self._health is not None:
            self._health.started()
        if self._recorder is not None:
            self._recorder.begin(request)

    def _frames_refused(self) -> int | None:
        """What the drive's list has refused so far, for the record of the arming that begins.

        Memory only, and it never raises (:meth:`SessionRecorder.frames_refused`).
        ``None``: nothing is recorded, or nobody can say.
        """
        return None if self._recorder is None else self._recorder.frames_refused()

    async def _dispatch(self) -> None:
        """Hand the mailbox's command to the runtime, and say what became of it.

        Exhaustive over ``Command``. Each command taken here ends one way, said
        to the surface under the request's identifier: taken, or refused with
        the reason. The identifier goes back to the surface as it came; the
        runtime is never shown it, and nothing here decides anything by it.
        """
        command = self._surface.take_command()
        runtime = self._runtime
        match command:
            case None:
                pass
            case StartSession():
                await self._start_programme(command)
            case StartManual():
                await self._start_manual(command)
            case SetManualTarget(output_rpm=output_rpm):
                target = runtime.set_manual_target(output_rpm)
                self._answer(command, target, describe_target_refusal)
            case FaultReset():
                reset = await runtime.fault_reset()
                self._answer(command, reset, describe_reset_refusal)
            case EndSession(reason=reason):
                runtime.request_stop(reason)
                self._surface.note_taken(command)
            case _ as unreachable:
                assert_never(unreachable)

    def _answer[T, E](
        self, command: Command, outcome: Result[T, E], words: Callable[[E], str]
    ) -> None:
        """Tell the surface what the runtime made of ``command``: taken, or why not."""
        if isinstance(outcome, Ok):
            self._surface.note_taken(command)
        else:
            self._surface.note_refused(command, words(outcome.error))

    async def _start_manual(self, command: StartManual) -> None:
        """Resolve the occupancy's ceiling from the configuration, then arm, or say why not.

        Before anything: a console out of service arms nothing. After the
        ceiling, the rider: a person on board is a rider the riders' list
        holds now, at least ``MIN_RIDER_AGE`` (:meth:`_on_board_refusal`), and
        the bench names nobody (:meth:`_manual_rider`).
        """
        occupancy = command.occupancy
        operator = command.operator
        out = self._service_refusal()
        if out is not None:
            self._surface.note_refused(command, out)
            return
        ceiling = self._config.ceiling_for(occupancy)
        if isinstance(ceiling, Err):
            self._surface.note_refused(command, ceiling.error.detail)
            return
        named = self._manual_rider(command)
        if isinstance(named, Err):
            self._surface.note_refused(command, describe_passenger_refusal(named.error))
            return
        rider = named.value
        on_board = None if rider is None else self._on_board_refusal(rider)
        if on_board is not None:
            self._surface.note_refused(command, on_board)
            return
        blocked = self._presence_refusal(occupancy) or self._clock_refusal(occupancy)
        if blocked is not None:
            self._surface.note_refused(command, blocked)
            return
        # Read before the drive is touched: the record's frames start with the arming,
        # and so does the count of those it loses.
        asked = self._clock.monotonic()
        refused = self._frames_refused()
        started = await self._runtime.start_manual(occupancy, operator, ceiling.value)
        if isinstance(started, Err):
            self._surface.note_refused(command, describe_start_refusal(started.error))
            return
        self._surface.note_taken(command)
        subject = LOCAL_SUBJECT if rider is None else rider.user_id
        self._begin_record(
            SessionRequest(
                occupancy=occupancy,
                operator=operator,
                subject_id=subject,
                arming_began=asked,
                arming_refused=refused,
            )
        )
        untrusted = self._untrusted_clock()
        if untrusted is not None:
            # Nobody on board (a person on board was refused above): the session
            # starts, and the operator is told what its record's dates are worth.
            # Said about the record, never refused.
            self._surface.note_recording(describe_clock_warning(untrusted))
        if self._listener is not None:
            self._listener.session_started(
                StartedSession(
                    kind=SessionKind.MANUAL,
                    operator=operator,
                    started_at=self._clock.unix_millis(),
                    subject_id=subject,
                    cloud_session_id=None,
                    occupancy=occupancy,
                    subject_label=None if rider is None else rider.name,
                )
            )

    def _on_board_refusal(self, rider: Rider) -> str | None:
        """Why ``rider`` may not be on board a manual session, or ``None``.

        The minimum age of a programmed session, counted the same way from the
        birth year the site set (:func:`~src.roster.age_of`), decided by the
        product owner on 15 October 2026: no birth year, or too young, is a
        refusal. A manual session needs no maximum heart rate: nothing is
        fitted to it. The rider's name and age are kept out of what is written.
        """
        age = age_of(rider, self._clock.unix_millis())
        if self._recorder is not None:
            self._recorder.withhold(
                rider.name, rider_words(age.value) if isinstance(age, Ok) else ""
            )
        if isinstance(age, Err):
            return describe_passenger_refusal(age.error)
        return rider_age_refusal(age.value, self._config.min_rider_age)

    def _manual_rider(self, command: StartManual) -> Result[Rider | None, PassengerRefusal]:
        """Who rides a manual session: nobody on the bench, a rider of the list on board.

        A bench session that names a rider is refused rather than recorded
        without them: the declaration and the name contradict each other.
        """
        subject = command.subject_id
        if command.occupancy is Occupancy.BENCH:
            return Ok(None) if subject in ("", LOCAL_SUBJECT) else Err(OnBench())
        # Anything but the bench is somebody on board: a rider the list holds now.
        found = self._roster.find(subject, self._clock.monotonic())
        return found if isinstance(found, Err) else Ok(found.value)

    async def _start_programme(self, command: StartSession) -> None:
        """A programmed session: every gate in order of increasing commitment, then arm.

        1. programmes enabled in this build (``PROGRAMS_ENABLED``, milestone M5),
           a rider whose maximum heart rate and age are known (:meth:`_rider_for`:
           for a start at the console, a rider the riders' list holds now, with
           both values set on the site), and an age at least ``MIN_RIDER_AGE``;
        2. a person is on board by definition - the heart rate drives the speed -
           so the OCCUPIED ceiling must exist (``OCCUPANCY_OCCUPIED_ENABLED``,
           milestone M6);
        3. the profile resolves, fitted to this rider's maximum heart rate, so
           every cardiac check re-runs against this person;
        4. its ``max_rpm`` fits under the occupied ceiling;
        5. the runtime's own gates (attestation, verdicts, tiers, the drive).

        A refusal anywhere is said on the console and, for a dashboard launch,
        sent back to the dashboard as a failed session.
        """
        operator = command.operator
        rider = self._rider_for(command)
        if self._recorder is not None:
            # A refusal below may quote the programme's identifier, or the rider's
            # age: the operator is shown both, and neither is written to disk.
            # Nor is the rider's name, which no refusal quotes.
            facts = rider.value if isinstance(rider, Ok) else None
            age = None if facts is None else facts.age
            label = None if facts is None else facts.label
            self._recorder.withhold(
                command.profile_id,
                "" if age is None else rider_words(age),
                "" if label is None else label,
            )
        prepared = self._prepare_programme(command, rider)
        if isinstance(prepared, Err):
            self._refuse_programme(command, prepared.error)
            return
        program = prepared.value.program
        label = prepared.value.rider.label
        subject = Subject(subject_id=command.subject_id, operator=operator)
        # Read before the drive is touched: the record's frames start with the arming,
        # and so does the count of those it loses.
        asked = self._clock.monotonic()
        refused = self._frames_refused()
        started = await self._runtime.start(program, subject)
        if isinstance(started, Err):
            self._refuse_programme(command, describe_start_refusal(started.error))
            return
        self._surface.note_taken(command)
        self._begin_record(
            SessionRequest(
                occupancy=Occupancy.OCCUPIED,
                operator=operator,
                program=program,
                subject_id=command.subject_id,
                cloud_session_id=command.cloud_session_id,
                arming_began=asked,
                arming_refused=refused,
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
                    subject_label=label,
                )
            )

    def _rider_for(self, command: StartSession) -> Result[RiderFacts, str]:
        """What this programmed start is vetted on, or why it has nothing to be vetted on.

        A launch from the dashboard carries the rider's values: the dashboard
        read them when it was launched (ANH-144 re-checks them at the arming).
        A start at the console names a rider and nothing else: the list held
        now must hold them, with a maximum heart rate and a birth year, both
        set on the site. A rider it no longer holds, a list too old, a value
        missing: refused, whatever the page showed when the start was asked.
        """
        if command.cloud_session_id is not None:
            return Ok(RiderFacts(command.subject_hr_max, command.subject_age, None))
        found = self._roster.find(command.subject_id, self._clock.monotonic())
        if isinstance(found, Err):
            return Err(describe_passenger_refusal(found.error))
        passenger = passenger_of(found.value, self._clock.unix_millis())
        if isinstance(passenger, Err):
            return Err(describe_passenger_refusal(passenger.error))
        listed = passenger.value
        return Ok(RiderFacts(listed.hr_max, listed.age, listed.rider.name))

    def _admitted(self, rider: Result[RiderFacts, str]) -> Result[RiderFacts, str]:
        """Gate 1, before the machine is asked anything: the console is in service
        (whether the start is typed here or comes down from the dashboard),
        programmes are enabled, the rider is known and old enough."""
        out = self._service_refusal()
        if out is not None:
            return Err(out)
        if not self._config.programs_enabled:
            return Err(PROGRAMS_DISABLED)
        if isinstance(rider, Err):
            return rider
        too_young = rider_age_refusal(rider.value.age, self._config.min_rider_age)
        return rider if too_young is None else Err(too_young)

    def _prepare_programme(
        self, command: StartSession, rider: Result[RiderFacts, str]
    ) -> Result[Armable, str]:
        """Gates 1-4, the camera and the wall clock: the programme to arm, or why not."""
        admitted = self._admitted(rider)
        if isinstance(admitted, Err):
            return admitted
        ceiling = self._config.ceiling_for(Occupancy.OCCUPIED)
        if isinstance(ceiling, Err):
            return Err(ceiling.error.detail)
        resolved = self._store.resolve(
            command.profile_id,
            at=self._clock.unix_millis(),
            total_duration_s=command.total_duration_s,
            subject_hr_max=admitted.value.hr_max,
        )
        if isinstance(resolved, Err):
            return Err(describe_resolve_error(resolved.error))
        program = resolved.value
        if program.profile.max_rpm > ceiling.value:
            return Err(
                f"demarrage refuse : programme a {program.profile.max_rpm} tr/min moteur, "
                f"au-dessus du plafond personne a bord ({ceiling.value} tr/min)"
            )
        occupied = Occupancy.OCCUPIED
        blocked = self._presence_refusal(occupied) or self._clock_refusal(occupied)
        if blocked is not None:
            return Err(blocked)
        return Ok(Armable(program=program, rider=admitted.value))

    def _service_refusal(self) -> str | None:
        """Why a console out of service refuses every start; ``None`` when it is in service."""
        service = self._service
        refusal = None if service is None else service.refusal()
        return None if refusal is None else f"demarrage refuse : {refusal}"

    def _refuse_programme(self, command: StartSession, detail: str) -> None:
        self._surface.note_refused(command, detail)
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

    @property
    def wall_clock(self) -> WallClockWatch | None:
        """The last reading of the wall clock's standing, or ``None`` without a probe."""
        return self._wall_clock

    async def clock_step(self) -> None:
        """Ask again whether a time service keeps the wall clock. No-op without a probe.

        A task of its own, never the control tick: the tick reads the monotonic
        clock and nothing else. The question is one system call that copies a
        few kernel variables (:class:`~src.wall_clock.KernelClock`); it opens
        nothing and waits for nothing, and the watch never raises. The gates,
        the record and the page read what it left in memory.
        """
        if self._wall_clock is not None:
            self._wall_clock.refresh()

    def _untrusted_clock(self) -> WallClockState | None:
        """The wall clock's standing when nobody answers for it, else ``None``.

        ``None`` too on a console built without a probe: it judges nothing. Reads memory.
        """
        watch = self._wall_clock
        if watch is None:
            return None
        state = watch.current()
        return None if is_trusted(state) else state

    def _clock_refusal(self, occupancy: Occupancy) -> str | None:
        """Why the wall clock refuses a start declared ``occupancy``, or ``None``.

        Only a start with a person on board: its record is evidence, and must
        be dated by a clock somebody answers for. Asked before the runtime is,
        like the camera: nothing has touched the drive when it refuses.
        """
        untrusted = self._untrusted_clock()
        if untrusted is None or occupancy is not Occupancy.OCCUPIED:
            return None
        return describe_clock_refusal(untrusted)

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

    async def cloud_stop_step(self) -> None:
        """Look whether the dashboard wants the running session stopped. No-op without a link.

        A task of its own, apart from :meth:`cloud_step`: that one may wait
        seconds on a record read or on an upload, and a stop asked for from
        the dashboard must not wait behind it. The same fence, for the same
        reason: a bug in the watch must not bring a running session down.
        """
        cloud = self._cloud
        if cloud is None:
            return
        try:
            await cloud.watch_stop()
        except Exception:  # as cloud_step: an observer must not stop the machine
            _logger.exception("dashboard stop watch failed; the session is unaffected")

    # --- running and stopping --------------------------------------------

    async def run(self, stop: asyncio.Event, web: WebRunner) -> int:
        """Run until ``stop`` is set or a task ends; then stop everything. Returns an exit code."""
        if self._recorder is not None:
            self._recorder.journal.start()
        control = asyncio.create_task(self._every(CONTROL_PERIOD, self.control_cycle, stop))
        observers = (
            asyncio.create_task(self._every(ECG_PERIOD, self.ecg_step, stop)),
            asyncio.create_task(self._every(SENSOR_PERIOD, self.sensor_step, stop)),
            asyncio.create_task(self._every(PRESENCE_PERIOD, self.presence_step, stop)),
            asyncio.create_task(self._every(CLOUD_PERIOD, self.cloud_step, stop)),
            asyncio.create_task(self._every(STOP_WATCH_PERIOD, self.cloud_stop_step, stop)),
            asyncio.create_task(self._every(WALL_CLOCK_PERIOD, self.clock_step, stop)),
        )
        started = asyncio.create_task(self._record_start())
        try:
            failed = await PanelTasks(stop, web, self.close).run(control, observers)
        finally:
            # Not one of the console's tasks: it ends by itself, and its end
            # stops nothing. A console that never ticked has nothing to write;
            # one that did is waited for, so that no write of this start is
            # still on its way when the exit writes its own.
            if not self._ticked.is_set():
                started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        return EXIT_FAILED if failed or self._stalled_at_stop else EXIT_OK

    async def _record_start(self) -> None:
        """Once the first tick has read the drive: write this start, off the loop."""
        await self._ticked.wait()
        if self._service is not None:
            await self._service.record_start()

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

        Before anything: whether the control loop went longer than the
        supervisor's own stall bound without completing a tick, in this
        process. It is not completing any as this stop begins, or a tick came
        back that late earlier: a frozen console is woken by the very stop
        that ends it, and may run one late tick before it gets here
        (:meth:`~src.service_watchdog.ServiceWatchdog.stalled`). Either way
        this is not a clean stop, whoever asked for it: :meth:`run` then
        returns a failure. And the service manager is told the console is
        stopping, so that the time the drive takes to come to rest is not
        read as a frozen loop.
        """
        if self._watchdog.stalled(self._clock.monotonic()):
            self._stalled_at_stop = True
            _logger.error(
                "console stopped after its control loop went without a completed tick "
                "for longer than its stall bound: not a clean stop"
            )
        self._watchdog.stopping()
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
        if self._cloud is not None:
            await self._cloud.close()
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
    raise assert_never(config.motor_backend)


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
    raise assert_never(config.ecg_source)


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
    wall_clock: WallClockProbe | None = None,
    notify_socket: str | None = None,
    service: ServiceDesk | None = None,
    health: HealthWatch | None = None,
    deposit: DepositTask | None = None,
    roster: Roster | None = None,
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

    With a journal the drive is listened to (:func:`~src.record.drive_tap.tap_drive`):
    the runtime is given what that returns, which is the backend itself when
    it reports its own exchanges, and a delegating tap otherwise. The SDK's
    transport calls are kept only when ``RECORD_DRIVE_SDK_FRAMES`` asks.

    ``wall_clock`` says whether a time service keeps the wall clock
    (:func:`~src.wall_clock.system_probe` in production; a test gives a fake).
    It is read here once, then by :meth:`LocalPanel.clock_step`. ``None``:
    the console judges nothing about its clock: nothing is warned or refused
    on its account, and a manifest says ``wall_trusted: null``. That is what a
    test that is not about the clock wants, and what a system that cannot be
    asked gets (anything but Linux).

    The build's version is read here, once (``raspberry-pi/VERSION``): startup
    I/O, with nothing turning. The page, the heartbeat and every record's
    manifest are given that one value.

    ``notify_socket`` is the service manager's socket (``NOTIFY_SOCKET``), told
    of every completed tick; ``None``: nobody is told. ``service`` is the
    restart state of this console (:func:`open_service` in production);
    ``None``: it keeps none and is always in service.

    ``roster`` is the riders' list (:mod:`src.roster`); ``None``: an empty one,
    which the dashboard link fills when there is one. A test or a simulation
    gives one it has filled itself.
    """
    version = read_software_version()
    riders = Roster(linked=config.cloud is not None) if roster is None else roster
    watch = None if wall_clock is None else WallClockWatch(wall_clock)
    # What the dashboard link hears of itself, for the page. The link writes
    # it from its own task, the page's route reads it: both on the event loop.
    link_health = None if config.cloud is None else LinkHealth(clock.monotonic())
    motion = load_panel_motion_limits(config)
    drive = build_drive(config, clock) if drive is None else drive
    safety = SafetyLimits(
        hard_max_bpm=config.tiers.hard_max_bpm, critical_bpm=config.tiers.critical_bpm
    )
    # A stop that finds no tick completed for as long as the supervisor itself
    # goes silent on is not a clean one: the same figure, read in one place.
    watchdog = ServiceWatchdog(notify_socket, overdue=safety.loop_stall_silent_gap)
    backend = drive.backend
    if journal is not None:
        tapped = tap_drive(backend, clock, sdk_frames=config.record.drive_sdk_frames)
        backend = tapped.backend
        journal.listen(tapped.source, tapped.refused)
    runtime = TrainingRuntime(
        clock=clock,
        drive=backend,
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
            software_version=version,
            wall_clock=None if watch is None else watch.current,
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
    reporter = PanelReporter(
        clock=clock,
        config=config,
        runtime=runtime,
        ecg=ecg,
        software_version=version,
        dashboard=link_health,
        wall_clock=watch,
        watchdog=watchdog,
        service=service,
    )
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
        service=service,
        # The status reads the verdicts where every latch is seen: the runtime
        # holds the ones it latched itself, which the supervisor never sees.
        verdicts=runtime,
        roster=riders,
    )
    cloud: CloudSync | None = None
    owned: HttpxTransport | None = None
    if config.cloud is not None:
        if health is None:
            health = HealthWatch(None if journal is None else journal.root)

        def health_context() -> HealthContext:
            return HealthContext(
                bitalino_connected=ecg.status().connected,
                clock_offset_s=None if watch is None else clock_offset_s(watch.current()),
                record_degraded=None if recorder is None else recorder.is_degraded(),
                restart_loop=None if service is None else service.status().out_of_service,
            )

        if transport is None:
            owned = HttpxTransport(config.cloud)
            sent: CloudTransport = owned
        else:
            sent = transport(config.cloud)
        # One transport for the link and the deposit: a route the dashboard
        # asks to wait for is held for both (contract 1.4).
        link = PacedTransport(sent, clock)
        cloud = CloudSync(
            clock=clock,
            transport=link,
            runtime=runtime,
            surface=surface,
            store=store,
            tiers=config.tiers,
            programs_enabled=config.programs_enabled,
            software_version=version,
            record_degraded=None if recorder is None else recorder.is_degraded,
            records=None if journal is None else record_source(journal, clock),
            link=link_health,
            health=health,
            health_context=health_context,
            deposit=(
                deposit
                if deposit is not None
                else (
                    None
                    if journal is None or owned is None
                    else RecordDeposit(journal.root, clock, link, owned.upload)
                )
            ),
            roster=riders,
        )
    return LocalPanel(
        clock=clock,
        config=config,
        drive=drive,
        runtime=runtime,
        watchdog=watchdog,
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
        health=health,
        cloud=cloud,
        transport=owned,
        recorder=recorder,
        wall_clock=watch,
        service=service,
        roster=riders,
    )


def open_service(journal: Journal, clock: Clock) -> ServiceDesk:
    """The restart state of this console, read and written next to the session records.

    Startup I/O, with nothing turning and before any command can be taken: what
    the previous process left is read, and the newest session record is
    looked at (:func:`~src.record.export.listing`, which knows what a closed
    record is). What was found is said in the console's journal and in the
    logbook.

    **Reads only.** This start is written to the disk later, once the first
    control tick has read the drive (:meth:`LocalPanel._record_start`): the
    write, with its flush to the device, is the one thing here that a tired SD
    card can make slow, and it never comes before the first look at the drive.
    What does come before it is two reads of the directory the journal has
    just prepared (the state file, the list of records), like the other files
    the console reads to start (its version, its limits, its profiles).

    Nothing here talks to the drive, and until the first tick this console
    sends it nothing at all: a drive left running hears no master and its own
    communication timeout is counting. Its state is read by the first control
    tick, as always, before the mailbox can hold anything.
    """
    guard = ServiceGuard(journal.root, clock)
    records = listing(journal.root)
    newest_open = records[0].name if records and not records[0].closed else None
    guard.begin(boot_id=boot_identity(), open_record=newest_open)
    desk = ServiceDesk(guard, journal, clock)
    desk.announce()
    return desk


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


def record_source(journal: Journal, clock: Clock) -> RecordSource:
    """The session records, as the dashboard link reads them back to send them.

    One thread of its own for those reads and for the cursor it writes, apart
    from the threads of the downloads and from every other thread of the
    console: a records directory that stops answering holds the sending, and
    nothing else. The system's boot identifier is read here, once: startup
    I/O, with nothing turning.
    """

    def current() -> Path | None:
        return journal.status(clock.monotonic()).path

    return RecordSource(root=journal.root, current=current, io=RecordIo(1), boot_id=boot_identity())


def build_recorder(
    config: LocalConfig,
    *,
    clock: Clock,
    journal: Journal,
    safety: SafetyLimits,
    motion: MotionLimits,
    drive: DriveSide,
    ecg: EcgSide,
    software_version: SoftwareVersion,
    wall_clock: Callable[[], WallClockState] | None = None,
) -> SessionRecorder:
    """The session record's loop side, stamped with the configuration actually applied
    and with the version of this build. ``wall_clock``: the last reading of the wall
    clock's standing, for each manifest."""
    simulator = drive.simulator
    radius = config.geometry.radius
    leg_tip = config.leg_tip_radius
    return SessionRecorder(
        clock=clock,
        journal=journal,
        stamp=stamp_for(config, RUNTIME_LIMITS, safety, motion, software_version),
        radius=radius,
        leg_tip=radius if leg_tip is None else Metres(max(float(leg_tip), float(radius))),
        sim_state=None if simulator is None else _sim_state_of(simulator),
        link_stats=ecg.link_stats,
        wall_clock=wall_clock,
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
    return f"demarrage refuse : {_start_refusal_reason(refusal)}"


def _start_refusal_reason(refusal: StartRefusal) -> str:  # noqa: PLR0911  # one return per refusal
    """Why the start was refused, without the words that say it was."""
    match refusal:
        case AlreadyStarted():
            return f"la machine est deja {refusal.state.value}"
        case NotAttested():
            return "cablage de l'arret d'urgence non atteste"
        case SafetyStanding(verdict=verdict):
            said = verdict.detail.removesuffix(SELF_CLEARING)
            return f"verdict {verdict.rule} a acquitter ({said})"
        case LimitsMismatch(threshold=threshold):
            return f"seuil {threshold} different de celui du superviseur"
        case PlanUnusable(detail=detail) | DriveUnavailable(detail=detail):
            return detail
        case DriveParameterRefused():
            return refusal.detail
        case DrivePrecommanded(output_rpm=rpm):
            return f"variateur deja en marche ({rpm} tr/min), arret demande"
        case DriveInFault(report=report):
            code = "?" if report is None else f"{report.fault.mnemonic}, LFT {report.raw_code}"
            return f"variateur en defaut ({code})"
        case RecordStorageLow():
            return describe_record_storage(refusal)
    raise assert_never(refusal)


def describe_record_storage(refusal: RecordStorageLow) -> str:
    """Why the session record refuses an arming, with the numbers and the directory."""
    megabyte = 1_000_000
    where = refusal.where
    free = refusal.free_bytes
    stale = refusal.stale_for
    if stale is not None:
        if isinstance(refusal, RecordJournalBusy):
            return (
                f"enregistrement de seance impossible pour l'instant : {_BUSY[refusal.doing]} "
                f"sous {where}, derniere mesure de l'espace libre il y a {stale:.0f} s. "
                "Reessayer dans un instant"
            )
        return (
            f"enregistrement de seance impossible, espace libre sous {where} mesure il y a "
            f"{stale:.0f} s : le disque ne repond plus"
        )
    if isinstance(refusal, RecordInodesLow):
        return (
            "plus assez de fichiers libres pour l'enregistrement de seance : "
            f"{refusal.free_inodes} inodes libres sous {where}, {refusal.required_inodes} requis "
            "(une seance cree des milliers de petits fichiers). Liberer de l'espace"
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


_BUSY: Final[Mapping[Activity, str]] = {
    Activity.IDLE: "l'ecriture des enregistrements est occupee",
    Activity.CLOSING: "la fermeture d'un enregistrement est en cours",
    Activity.PURGING: "la purge des enregistrements deposes est en cours",
}
"""What the journal thread said it was starting, in words for the operator."""


def describe_clock_refusal(state: WallClockState) -> str:
    """Why a start with a person on board is refused on a wall clock nobody answers for."""
    return (
        f"demarrage refuse : horloge murale non fiable ({detail_of(state)}). Avec une "
        "personne a bord, l'enregistrement de la seance doit etre date par une horloge "
        "synchronisee : retablir le reseau du Pi et attendre que la ligne horloge de la "
        "console dise synchronisee"
    )


def describe_clock_warning(state: WallClockState) -> str:
    """What the operator is told when a bench session starts on such a clock."""
    return (
        f"horloge murale non fiable ({detail_of(state)}) : les dates de l'enregistrement "
        "de cette seance ne sont pas sures, et son manifeste le dit. La seance et la "
        "securite continuent."
    )


def rider_age_refusal(age: int | None, minimum: int) -> str | None:
    """Why a session with a person on board refuses this rider's age, or ``None``.

    Programmed or manual (the latter since 15 October 2026, a product decision).

    Unknown is a refusal, not a pass: the simulation battery found a 10-year-old
    accepted on the adult ceiling because nothing asked. The age comes from the
    rider's birth year on the site: through the riders' list for a start at the
    console, carried by the launch for one from the dashboard. It is never typed
    at the console.
    """
    if age is None:
        return "demarrage refuse : age du passager requis pour une seance programmee"
    if age < minimum:
        return f"demarrage refuse : {rider_words(age)}, minimum {minimum} ans (MIN_RIDER_AGE)"
    return None


def rider_words(age: int) -> str:
    """How a refusal names the rider by their age: the words the logbook is not given."""
    return f"passager de {age} ans"


def describe_resolve_error(error: ResolveError) -> str:
    """One line for a programme that could not be resolved. Exhaustive."""
    match error:
        case UnknownProfile():
            return f"demarrage refuse : programme {error.profile_id!r} inconnu sur cette machine"
        case Rejected(detail=detail):
            return f"demarrage refuse : programme inadapte a ce passager ({detail})"
    raise assert_never(error)


def describe_target_refusal(refusal: ManualTargetRefusal) -> str:
    """One line for a refused manual target."""
    match refusal:
        case NoManualSession():
            return f"consigne refusee : pas de session manuelle ({refusal.state.value})"
        case ManualEnding(detail=detail):
            return f"consigne refusee : {detail}"
        case TargetOutOfRange(requested=requested, min_run=low, ceiling=high):
            return (
                f"consigne refusee : {requested:.2f} tr/min de sortie hors de 0 ou "
                f"[{low}, {high}] tr/min moteur"
            )
        case HeldAtStandstill(by=by):
            return f"consigne refusee : {_held_by(by)}, puis redonner la cible"
    raise assert_never(refusal)


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
        case SafetyVerdict():
            wait = (
                "L'acquitter une fois sa cause levee" if by.latched else "Attendre qu'il soit leve"
            )
            return f"le verdict {by.rule} tient le bras a l'arret. {wait}"
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
    raise assert_never(by)


def describe_reset_refusal(refusal: FaultResetRefusal) -> str:
    """One line for a refused fault reset."""
    match refusal:
        case ResetWhileCommanded():
            return (
                "reset refuse : mouvement encore commande "
                f"({refusal.state.value}, {refusal.phase.value})"
            )
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
    raise assert_never(refusal)


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


def bound_exit(
    code: int, grace: Seconds = EXIT_GRACE, leave: Callable[[int], None] = os._exit
) -> Thread:
    """The console has stopped: let nothing hold the process more than ``grace`` longer.

    Called once :meth:`LocalPanel.run` has returned, so the drive is already
    stopped and the session record closed; nothing here comes before either.

    What is left can still wait for ever, and was measured to
    (``tests/test_exit_deadline.py``): a worker thread of the web server that
    is on a file when the disk stops answering is not a daemon, and the
    interpreter waits for it before it exits. A console that never leaves is
    never restarted by its supervisor. So a daemon thread waits ``grace``,
    says so, and ends the process with the exit code it was going to have.
    When the process leaves by itself first, which is every ordinary exit,
    that thread dies with it and nothing was forced.

    ``leave`` ends the process without running anything else: what is still
    pending at that point is what is stuck.
    """

    def expire() -> None:
        ThreadEvent().wait(grace)
        _logger.error(
            "console exit forced %.0f s after the console stopped: a thread did not come "
            "back (a disk or a link that no longer answers). Exit code %d",
            grace,
            code,
        )
        leave(code)

    deadline = Thread(target=expire, name="exit-deadline", daemon=True)
    deadline.start()
    return deadline


async def run_console(
    config: LocalConfig,
    *,
    stop: asyncio.Event | None = None,
    stopped: Callable[[int], object] | None = None,
    notify_socket: str | None = None,
) -> int:
    """Build the console on the real clock and run it until a signal.

    ``stopped`` is called with the exit code as soon as the console has
    stopped, whatever happens to this coroutine's caller afterwards: the entry
    point gives :func:`bound_exit`. ``None``, which is what a test wants: the
    process is left alone.

    The wall clock is judged where the system can be asked what it is worth
    (:func:`~src.wall_clock.system_probe`): on the Pi. Elsewhere the console
    says once that it does not judge it.

    ``notify_socket``: the service manager's socket, when one started this
    process (``NOTIFY_SOCKET``).

    A stop with exit code 0 is the only clean one, and is written as such
    last of all, once the drive is stopped, the record closed and the exit
    deadline armed: a process that does not get that far is what the next
    start counts as an unclean stop (:mod:`src.service_state`).
    """
    event = asyncio.Event() if stop is None else stop
    clock = RealClock()
    probe = system_probe()
    if probe is None:
        _logger.warning(
            "wall clock: not judged on this system (no way to ask what it is worth): "
            "nothing is warned or refused on its account, and the records say nothing of it"
        )
    journal = open_journal(config, clock)
    service = open_service(journal, clock)
    panel = build_panel(
        config,
        clock=clock,
        journal=journal,
        wall_clock=probe,
        notify_socket=notify_socket,
        service=service,
    )
    undo = install_stop_signals(event)
    code = EXIT_FAILED
    try:
        code = await panel.run(event, UvicornRunner(panel.services, config))
    finally:
        undo()
        if stopped is not None:
            stopped(code)
    if code == EXIT_OK:
        service.guard.stopped()
    return code


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
        f"Console du banc sur {web.scheme}://{web.host}:{web.port}/ - {modes} "
        f"(plafond {config.motor_max_rpm} tr/min moteur; paliers "
        f"{tiers.hard_max_bpm}/{tiers.critical_bpm} bpm) - "
        f"variateur: {drive} - ECG ({config.ecg_source.value}): {ecg} - "
        f"rayon {config.geometry.radius} m, i = {config.geometry.ratio} - "
        f"tableau de bord: {dashboard}"
    )


def tls_problem(config: LocalConfig) -> ConfigProblem | None:
    """Why the console's certificate cannot be served; ``None`` when it can, or there is none.

    The two files are opened here, before anything of the console is built: a
    certificate that cannot be served is a configuration error like the
    others, said with its reason and left with the exit code the service does
    not restart on. It is not judged on its dates: an expired certificate is
    served, and the tablet says so (:mod:`src.web.tls`).
    """
    files = config.web.tls
    if files is None:
        return None
    loaded = server_context(files)
    if isinstance(loaded, Err):
        return ConfigProblem(f"{KEY_UI_TLS_CERT}/{KEY_UI_TLS_KEY}", loaded.error.detail)
    return None


def main(environ: Mapping[str, str] | None = None) -> int:
    """``python -m src.local_panel``. Returns the process exit code.

    Everything the console logs leaves as one JSON object per line
    (:mod:`src.log_format`): on the Pi that is the journal of its service.
    """
    logging.basicConfig(level=logging.INFO, handlers=[json_handler()])
    env = load_environment() if environ is None else environ
    for key in retired_keys(env):
        _logger.warning("configuration: %s is set and no longer read: %s", key, RETIRED_KEYS[key])
    loaded = load_local_config(env)
    if isinstance(loaded, Err):
        for problem in loaded.error:
            _logger.error("configuration: %s: %s", problem.key, problem.detail)
        return EXIT_CONFIG
    config = loaded.value
    unserved = tls_problem(config)
    if unserved is not None:
        _logger.error("configuration: %s: %s", unserved.key, unserved.detail)
        return EXIT_CONFIG
    _logger.warning("%s", describe_config(config))
    return asyncio.run(
        run_console(config, stopped=bound_exit, notify_socket=env.get(NOTIFY_SOCKET))
    )


if __name__ == "__main__":
    raise SystemExit(main())
