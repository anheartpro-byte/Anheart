"""How the web layer is bound, who may talk to it, and what it can reach.

Three things live here, and each of them is a decision rather than plumbing.

**Where it listens.** The default is ``127.0.0.1``, which means a browser on
the Pi itself and nothing else. Moving off loopback puts a start/stop control
for a motor on a network, and this module **refuses to start** in that
configuration without a shared token of at least
:data:`MIN_TOKEN_LENGTH` characters. Not a warning: a warning about an open
control endpoint is read once, during commissioning, and then never again.

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
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, final, runtime_checkable

from src.clock import Clock
from src.control_surface import ControlSurface
from src.geometry import MachineGeometry
from src.panel_status import PanelSource
from src.presence.monitor import PresenceDecision, PresenceMonitor
from src.result import Result
from src.sensors.base import SensorKind, SensorReading
from src.telemetry import TelemetryHub
from src.training.plan import ProfileStore
from src.training.safety import SafetySupervisor
from src.training.types import Occupancy, OccupancyRefused
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
        if is_loopback_host(self.host):
            return
        token = self.token
        if token is None:
            raise ValueError(
                f"refusing to serve the operator interface on {self.host!r} without a "
                "token: this endpoint can start and stop a motor with a person in the "
                "machine, and off loopback it is reachable by everything on the network"
            )
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"refusing to serve on {self.host!r} with a {len(token)}-character "
                f"token: at least {MIN_TOKEN_LENGTH} characters are required off loopback"
            )

    @property
    def is_loopback(self) -> bool:
        """Whether this bind is reachable only from the Pi itself."""
        return is_loopback_host(self.host)

    @property
    def requires_token(self) -> bool:
        """Whether callers must present a token."""
        return self.token is not None

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Every ``Origin`` value accepted on a WebSocket handshake.

        Both schemes for the bound address, because an operator who put a
        reverse proxy with TLS in front of the Pi still sends ``https://``, and
        both the literal host and ``localhost`` for a loopback bind, because a
        browser sends whichever the operator typed.
        """
        hosts = [f"{self.host}:{self.port}"]
        if self.is_loopback:
            hosts.extend([f"localhost:{self.port}", f"127.0.0.1:{self.port}"])
        origins = [f"{scheme}://{authority}" for authority in hosts for scheme in ("http", "https")]
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

    async def list_ports(self) -> tuple[SerialPortInfo, ...]:
        """Candidate ports, best-effort. Must not raise and must not block."""
        ...


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


class PresenceView(Protocol):
    """What ``/api/presence`` reads from the camera fail-safe."""

    @property
    def last_decision(self) -> PresenceDecision | None: ...

    @property
    def monitor(self) -> PresenceMonitor: ...


class SensorSource(Protocol):
    """What ``/api/sensors`` reads: the configured channels and their latest readings."""

    @property
    def kinds(self) -> tuple[SensorKind, ...]: ...

    def latest(self) -> Mapping[SensorKind, SensorReading]: ...


type TokenGuard = Callable[[str | None], Awaitable[None]]
"""What an authenticated route depends on: a check that raises on refusal."""
