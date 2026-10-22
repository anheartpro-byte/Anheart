"""How the web layer is bound, who may talk to it, and what it can reach.

Three things live here, and each of them is a decision rather than plumbing.

**Where it listens.** The default is ``127.0.0.1``, which means a browser on
the Pi itself and nothing else. Moving off loopback puts a start/stop control
for a motor on a network, and this module **refuses to start** in that
configuration without a shared token of at least
:data:`MIN_TOKEN_LENGTH` characters. Not a warning: a warning about an open
control endpoint is read once, during commissioning, and then never again.

**How it is carried.** Off loopback the page, the commands and that token cross
a network, and they cross it inside TLS or not at all: this module **refuses
to start** there without a certificate (:class:`TlsFiles`), exactly as it does
without a token. There is one listener and one transport: a console given a
certificate speaks TLS on its port and nothing else, so there is no clear port
beside it to fall back to, and nothing that answers a clear request with a
redirect. Clear HTTP stays possible on loopback only, where nothing leaves the
machine: development, the simulator, the tests.

**Who may talk to it.** A token in a header for the API, checked with
``hmac.compare_digest``, plus an ``Origin`` allowlist for the WebSocket
handshake (see ``ws.py`` for why the socket needs its own check). The token is
a *shared* secret, not a login: it stops the other things on the clinic wifi,
which is the actual threat, and it does not pretend to identify an operator.
Attribution is a name typed into the attestation and acknowledgement fields,
which the safety supervisor refuses to accept blank.

**What a handler can reach.** :class:`Services` is one frozen record, passed in
at construction and closed over by the route factories. Not ``app.state``:
Starlette's ``State`` resolves attributes dynamically, so every access through
it would be untyped, and the strict gate exists precisely to stop that.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, final, runtime_checkable
from uuid import uuid4

from src.clock import Clock
from src.control_surface import ControlSurface
from src.geometry import MachineGeometry
from src.motor.sto import StoSource
from src.panel_status import PanelSource
from src.presence.monitor import PresenceDecision, PresenceMonitor
from src.record.export import RecordExporter
from src.remote_launch import ConfirmationRefusal, RemoteLaunchView
from src.result import Result
from src.roster import Roster
from src.sensors.base import SensorKind, SensorReading
from src.service_state import NotOutOfService, ServiceStatus
from src.telemetry import TelemetryHub
from src.training.plan import ProfileStore
from src.training.safety import SafetySupervisor
from src.training.types import Occupancy, OccupancyRefused, SafetyVerdict
from src.units import MotorRpm

_logger: logging.Logger = logging.getLogger(__name__)


TOKEN_HEADER: Final[str] = "x-anheart-token"  # noqa: S105  # a header NAME, not a secret
"""Header carrying the shared token. Lowercase: header lookups are case-insensitive."""

TOKEN_QUERY_PARAM: Final[str] = "token"  # noqa: S105  # a parameter NAME, not a secret
"""Query parameter carrying the token on the WebSocket handshake.

A browser cannot set request headers on a WebSocket - the API gives no way to -
so the socket has to take the token somewhere else. A query parameter is that
somewhere, with its known cost: URLs end up in server logs and in browser
history. Acceptable here because the token is a shared LAN secret rather than a
credential, and because the alternative (a cookie) would be sent automatically
by any page on any origin, which is exactly the attack the Origin check exists
to stop.

On the wire that URL is inside TLS wherever the console is off loopback (a
console there has a certificate or does not start), so the token it carries is
never readable on the network. The console itself keeps no access log.
"""

MIN_TOKEN_LENGTH: Final[int] = 16
"""Shortest token accepted for a non-loopback bind.

Sixteen characters of a random token is far past guessable over a LAN; the
point of the floor is to refuse ``"admin"``, not to compute an entropy budget.
"""

LOOPBACK_HOSTNAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})
"""Names that resolve to loopback but are not IP literals."""

DEFAULT_HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8080

MIN_TCP_PORT: Final[int] = 1
MAX_TCP_PORT: Final[int] = 65535

STATIC_DIR: Final[Path] = Path(__file__).resolve().parent / "static"
"""The page, resolved from this file rather than from the working directory.

A systemd unit and a container start with different working directories, and
serving a blank page because ``cwd`` moved is a failure that looks like a
broken dashboard rather than like a path bug.
"""


def is_loopback_host(host: str) -> bool:
    """Whether binding to ``host`` exposes the interface only to this machine.

    Anything that is not demonstrably loopback is treated as a network bind,
    including an unresolvable hostname. Failing closed is the whole point: the
    consequence of guessing wrong is an unauthenticated motor control on a
    clinic network.
    """
    if host.lower() in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class TlsFiles:
    """Where the console's certificate and its private key are, on this machine.

    Two paths and nothing else: the files are read once, when the server is
    built (:func:`src.web.tls.server_context`), and never again. Replacing
    them under a running console changes nothing until its next start.
    """

    certificate_path: Path
    """The console's certificate, PEM (``UI_TLS_CERT``)."""

    key_path: Path
    """Its private key, PEM, without a passphrase (``UI_TLS_KEY``)."""


def token_refusal(host: str, token: str | None) -> str | None:
    """Why a bind to ``host`` is refused for its token; ``None`` when it is not."""
    if is_loopback_host(host):
        return None
    if token is None:
        return (
            f"refusing to serve the operator interface on {host!r} without a "
            "token: this endpoint can start and stop a motor with a person in the "
            "machine, and off loopback it is reachable by everything on the network"
        )
    if len(token) < MIN_TOKEN_LENGTH:
        return (
            f"refusing to serve on {host!r} with a {len(token)}-character "
            f"token: at least {MIN_TOKEN_LENGTH} characters are required off loopback"
        )
    return None


def clear_http_refusal(host: str, tls: TlsFiles | None) -> str | None:
    """Why a bind to ``host`` is refused for its transport; ``None`` when it is not.

    Off loopback there is no clear HTTP: the token that authorises a start and
    a stop would cross the network readable by whoever listens to it.
    """
    if tls is not None or is_loopback_host(host):
        return None
    return (
        f"refusing to serve the operator interface on {host!r} in clear HTTP: off "
        "loopback the page, the commands and the token cross a network, and they "
        "are served over TLS or not at all. Give the console its certificate and "
        "its key (scripts/pi/make_console_tls.sh makes them)"
    )


SECURE_SCHEME: Final[str] = "https"
CLEAR_SCHEME: Final[str] = "http"

_SCHEME_PORTS: Final[Mapping[str, int]] = {CLEAR_SCHEME: 80, SECURE_SCHEME: 443}
"""The port a browser leaves out of an origin: the scheme's own."""


def origin_of(scheme: str, host: str, port: int) -> str:
    """The ``Origin`` a browser sends for a page it opened at that address.

    Without the port when it is the scheme's own: a page opened at
    ``https://10.42.0.1/`` says ``https://10.42.0.1``, never ``...:443``.
    """
    if _SCHEME_PORTS.get(scheme) == port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Where the interface listens, and what it demands of a caller.

    Frozen, so a token or a bind address cannot be changed after the process
    has been started with the operator's knowledge of what it was.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str | None = None
    """Shared secret. ``None`` means "no check", which is only allowed on loopback."""

    extra_origins: tuple[str, ...] = ()
    """Additional origins allowed on the WebSocket handshake.

    The origins for ``host:port`` itself are always allowed and need not be
    listed. This exists for the real case of a tablet at a fixed address, and
    it is an explicit allowlist rather than a wildcard because "any origin" on
    a WebSocket means any page the browser happens to have open.
    """

    static_dir: Path = field(default=STATIC_DIR)

    tls: TlsFiles | None = None
    """The certificate this console serves. ``None`` means clear HTTP, which is
    only allowed on loopback. Given one, the console speaks TLS wherever it
    listens, loopback included."""

    names: tuple[str, ...] = ()
    """Other names and addresses under which a browser reaches this console
    (``UI_TLS_NAMES``): the ones its certificate carries.

    Each gives one more accepted origin, with the scheme and the port this
    console serves. Needed as soon as the address listened on is not the one
    a tablet types: a console that listens on every address is opened at the
    fixed address of the access point, or at a local name.
    """

    def __post_init__(self) -> None:
        """Refuse a configuration that would expose the machine.

        Raises ``ValueError``, not a ``Result``. Startup is not the motor path
        (contract rule 3): no motor is running yet, nothing is commanded, and
        ``src/local_config.py`` turns the exception into a named configuration
        problem, and the console exits with a readable line.
        A ``Result`` here would be a refusal that a caller could ignore.
        """
        if not MIN_TCP_PORT <= self.port <= MAX_TCP_PORT:
            raise ValueError(f"port must be {MIN_TCP_PORT}..{MAX_TCP_PORT}, got {self.port}")
        for refusal in (
            token_refusal(self.host, self.token),
            clear_http_refusal(self.host, self.tls),
            self._clear_origin_refusal(),
        ):
            if refusal is not None:
                raise ValueError(refusal)

    def _clear_origin_refusal(self) -> str | None:
        """Why the extra origins are refused; ``None`` when they are not.

        A console that speaks TLS is never opened from a page that was served
        in clear, so it lists no such page among the origins it accepts.
        """
        if self.tls is None:
            return None
        clear = [
            origin for origin in self.extra_origins if not origin.startswith(f"{SECURE_SCHEME}://")
        ]
        if not clear:
            return None
        return (
            f"refusing the extra origin {clear[0]!r} on a console that serves TLS: "
            "only an https origin can be the page of this console"
        )

    @property
    def is_loopback(self) -> bool:
        """Whether this bind is reachable only from the Pi itself."""
        return is_loopback_host(self.host)

    @property
    def scheme(self) -> str:
        """``https`` when this console serves its certificate, ``http`` otherwise."""
        return CLEAR_SCHEME if self.tls is None else SECURE_SCHEME

    @property
    def requires_token(self) -> bool:
        """Whether callers must present a token."""
        return self.token is not None

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Every ``Origin`` value accepted on a WebSocket handshake.

        A console that serves its certificate accepts ``https`` origins and no
        other: its page is only ever served over TLS, so a page that says
        ``http`` is not its page. That is every console off loopback.

        A console in clear, which is a loopback one, accepts both schemes for
        the bound address, because an operator who put a reverse proxy with
        TLS in front of the Pi still sends ``https://``.

        Both the literal host and ``localhost`` for a loopback bind, because a
        browser sends whichever the operator typed; and every one of
        :attr:`names`.
        """
        hosts = [self.host, *self.names]
        if self.is_loopback:
            hosts.extend(["localhost", "127.0.0.1"])
        schemes = (CLEAR_SCHEME, SECURE_SCHEME) if self.tls is None else (SECURE_SCHEME,)
        origins = [origin_of(scheme, host, self.port) for host in hosts for scheme in schemes]
        origins.extend(self.extra_origins)
        return tuple(dict.fromkeys(origins))

    def origin_allowed(self, origin: str | None) -> bool:
        """Whether a handshake carrying this ``Origin`` may proceed.

        ``None`` - the header absent - is **allowed**, and that is a considered
        decision rather than an oversight. The threat this check addresses is a
        web page on some other origin opening a socket to this one, and a page
        cannot omit ``Origin``: the browser sets it and script cannot touch it.
        An absent header therefore means a non-browser client (a test, ``curl``,
        an operator tool), which is not the CSRF case and is still subject to
        the token check.
        """
        if origin is None:
            return True
        return origin in self.allowed_origins

    def token_matches(self, presented: str | None) -> bool:
        """Whether a presented token is the configured one.

        ``hmac.compare_digest`` rather than ``==``: string comparison returns
        early on the first differing byte, which leaks the length of the
        matching prefix to anyone able to time the response.
        """
        expected = self.token
        if expected is None:
            return True
        if presented is None:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# =========================================================================
# Serial port discovery
# =========================================================================


@dataclass(frozen=True, slots=True)
class SerialPortInfo:
    """One candidate port for the drive's Modbus link.

    ``description`` is whatever the discovery method can say about it - a
    by-id symlink name carries the USB vendor and serial number, which is how
    an operator tells the drive's adapter from the BITalino's.
    """

    device: str
    description: str


@runtime_checkable
class PortLister(Protocol):
    """Something that can enumerate candidate serial ports.

    A protocol, for two reasons. The status page must work in a test with no
    ``/dev`` and no adapters attached; and the contract's isolation rule (rule
    5) reserves ``serial`` for ``src/motor/atv320.py``, so a richer lister
    built on ``serial.tools.list_ports`` belongs behind this seam rather than
    in the web layer.

    ``async`` because a real implementation touches the filesystem or a USB
    subsystem, and nothing in this process may block the event loop.
    """

    @abstractmethod
    async def list_ports(self) -> tuple[SerialPortInfo, ...]:
        """Candidate ports, best-effort. Must not raise and must not block."""


#: Where Linux exposes serial devices by a stable name. Preferred over the
#: ``ttyUSB*`` patterns because these names identify the adapter (vendor plus
#: serial number) rather than the enumeration order - and enumeration order is
#: exactly what changes when somebody unplugs the BITalino and plugs it back in.
BY_ID_DIRECTORY: Final[Path] = Path("/dev/serial/by-id")

_PORT_PATTERNS: Final[tuple[str, ...]] = ("ttyUSB*", "ttyACM*", "ttyAMA*", "ttyS[0-9]")


@final
class FilesystemPortLister:
    """Enumerates serial ports by looking at ``/dev``. Linux and the Pi.

    Deliberately not ``serial.tools.list_ports``, which the contract reserves
    for the drive module: see :class:`PortLister`. The by-id symlinks give the
    same information an operator needs to pick the right adapter, and reading a
    directory needs no vendor library.

    On a platform without ``/dev`` (a Windows bench) this returns nothing,
    which the status page renders as "no ports found" rather than as an error.
    Discovery is a convenience; the port actually used comes from
    configuration.
    """

    __slots__ = ("_by_id", "_root")

    def __init__(self, root: Path = Path("/dev"), by_id: Path = BY_ID_DIRECTORY) -> None:
        self._root: Path = root
        self._by_id: Path = by_id

    async def list_ports(self) -> tuple[SerialPortInfo, ...]:
        """Scan for ports off the event loop.

        The scan is blocking filesystem I/O - a directory read plus a
        ``readlink`` per entry - and a stalled USB subsystem can make it slow,
        so it goes to a worker thread (contract rule 8). It touches nothing the
        control loop touches, which is what makes that safe.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._scan)

    def _scan(self) -> tuple[SerialPortInfo, ...]:
        """The blocking part. Total: an unreadable ``/dev`` yields nothing."""
        found: dict[str, str] = {}
        for entry in _entries(self._by_id):
            found.setdefault(_resolve(entry), entry.name)
        for pattern in _PORT_PATTERNS:
            for entry in _glob(self._root, pattern):
                found.setdefault(str(entry), entry.name)
        return tuple(
            SerialPortInfo(device=device, description=description)
            for device, description in sorted(found.items())
        )


def _entries(directory: Path) -> Iterable[Path]:
    """Directory contents, or nothing at all. Never raises."""
    try:
        return sorted(directory.iterdir())
    except OSError as exc:
        _logger.debug("cannot list %s: %s", directory, exc)
        return ()


def _glob(root: Path, pattern: str) -> Iterable[Path]:
    """Glob under ``root``, or nothing at all. Never raises."""
    try:
        return sorted(root.glob(pattern))
    except OSError as exc:
        _logger.debug("cannot glob %s/%s: %s", root, pattern, exc)
        return ()


def _resolve(link: Path) -> str:
    """Where a by-id symlink points, or the link itself if it cannot be read."""
    try:
        return str(link.resolve())
    except OSError:
        return str(link)


# =========================================================================
# What a handler may reach
# =========================================================================


class VerdictSource(Protocol):
    """What ``/api/status`` reads about the verdicts, whoever latched them.

    The supervisor holds its own verdicts. The runtime can latch one on its
    own behalf (a drive found running, an enable that was not confirmed, a link
    it gave up on), which the supervisor never sees: a status read from the
    supervisor alone said "nothing latched" under such a verdict, on a screen
    that had no live frame to correct it. The console hands in the runtime,
    whose three reads below cover both. :class:`~src.training.safety.SafetySupervisor`
    has the same three, about itself alone, and is what is read when nothing
    else was handed in.

    Reads only, synchronous, no I/O: a request handler calls them between two
    ticks.
    """

    @property
    @abstractmethod
    def standing(self) -> SafetyVerdict | None:
        """The verdict in force, or ``None`` when nothing asks for anything."""

    @property
    @abstractmethod
    def floor(self) -> SafetyVerdict | None:
        """The most severe latched verdict, or ``None``."""

    @property
    @abstractmethod
    def silenced(self) -> SafetyVerdict | None:
        """The latched verdict ``GO_SILENT`` stands over, or ``None``."""


def new_instance() -> str:
    """A name for one run of the console: 32 hexadecimal digits, drawn at random.

    Drawn, not derived. A process identifier comes back after a reboot (a
    service started in the same order gets the same one), and the Pi has no
    clock that survives one. Nothing but chance tells two runs apart.
    """
    return uuid4().hex


@dataclass(frozen=True, slots=True)
class Services:
    """Everything the route handlers are allowed to touch, in one frozen record.

    Frozen because the set of services is fixed when the process starts; the
    objects inside are the mutable ones, and each of them documents its own
    single-event-loop discipline.

    Note what is absent: no drive, no BITalino, no Convex client, no database.
    The web layer states intentions through the control surface and reads state
    through the hub. It never becomes a second writer on a wire, and it never
    performs a database write on the emergency-stop path.
    """

    clock: Clock
    surface: ControlSurface
    hub: TelemetryHub
    supervisor: SafetySupervisor
    store: ProfileStore
    ports: PortLister
    geometry: MachineGeometry

    sto: StoSource | None = None

    motion_enabled: bool = True
    """Whether a route may ask for motion at all.

    ``False`` in the local console's read-only milestone: every route that
    could put the machine in motion answers **403** before touching the
    mailbox. Stopping, the emergency stop, acknowledgement, attestation and
    every read stay available, because a console that could not stop a machine
    somebody else started would be worse than none.
    """

    panel: PanelSource | None = None
    """The console's link report, served at ``/api/panel``; ``None`` = no console."""

    programs_enabled: bool = True
    """Whether a PROGRAMMED session may be started (``/api/session/start``).

    ``False`` on the local console until milestone M5: manual sessions exist,
    programmed ones do not yet, and that route answers **403**.
    """

    medical_refusal: str | None = None
    """Why programmed sessions are refused on account of the medical parameters,
    or ``None``: they are signed (``MedicalStatus.warning``). When set, that
    route's **403** says it rather than the milestone."""

    ceilings: Callable[[Occupancy], Result[MotorRpm, OccupancyRefused]] | None = None
    """The motor-rpm ceiling for an occupancy, or why that occupancy is refused.

    ``LocalConfig.ceiling_for`` on the console. ``None``: this build has no
    manual sessions, and ``/api/manual/start`` answers **403**.
    """

    sensors: SensorSource | None = None
    """Every acquired BITalino channel, processed (``GET /api/sensors``); ``None`` = none."""

    presence: PresenceView | None = None
    """The camera fail-safe (``GET /api/presence``); ``None`` = no camera configured."""

    camera: str = "none"
    """Which camera feeds it (``PRESENCE_SOURCE``), for the page."""

    records: RecordExporter | None = None
    """The session records on disk (``GET /api/records``); ``None`` = nothing is recorded."""

    service: ServiceRestorer | None = None
    """What puts a console out of service back in service (``POST /api/service/restore``);
    ``None`` = this console keeps no restart state and is never out of service."""

    verdicts: VerdictSource | None = None
    """Where ``/api/status`` reads the verdicts in force, whoever latched them.

    The runtime on the console. ``None``: the supervisor alone is read, which
    is everything there is when nothing else can latch.
    """

    roster: Roster | None = None
    """The riders' list (``GET /api/roster``), which a start with a person on board
    names one of. ``None``: this interface has none, and refuses such a start."""

    remote_launch_current: Callable[[], RemoteLaunchView | None] | None = None
    remote_launch_confirm: Callable[[str, str], Result[None, ConfirmationRefusal]] | None = None

    instance: str = field(default_factory=new_instance)
    """What this run of the console calls itself (:func:`new_instance`).

    The same in every ``/api/status`` answer and every telemetry frame for as
    long as this process lives, and another after any restart: it is how a
    screen knows that the console it is talking to is not the one that showed
    it something a moment ago. A restarted console holds nothing of what the
    previous one had latched. Display data: nothing on the machine reads it
    back, and it identifies nothing but this run.
    """


class ServiceRestorer(Protocol):
    """What ``/api/service/restore`` asks of the console's restart state."""

    @abstractmethod
    async def restore(self, operator: str) -> Result[ServiceStatus, NotOutOfService]:
        """Put the console back in service, by name; or say there was nothing to lift."""


class PresenceView(Protocol):
    """What ``/api/presence`` reads from the camera fail-safe."""

    @property
    @abstractmethod
    def last_decision(self) -> PresenceDecision | None:
        """What the last step decided; ``None`` before the first step."""

    @property
    @abstractmethod
    def monitor(self) -> PresenceMonitor:
        """The monitor behind the fail-safe."""


class SensorSource(Protocol):
    """What ``/api/sensors`` reads: the configured channels and their latest readings."""

    @property
    @abstractmethod
    def kinds(self) -> tuple[SensorKind, ...]:
        """The channels processed, in configuration order."""

    @abstractmethod
    def latest(self) -> Mapping[SensorKind, SensorReading]:
        """The most recent reading of every channel processed so far."""


type TokenGuard = Callable[[str | None], Awaitable[None]]
"""What an authenticated route depends on: a check that raises on refusal."""
