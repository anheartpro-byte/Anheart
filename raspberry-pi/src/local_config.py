"""Configuration of the local operator console, from the environment.

No Convex credentials are required: a bench session with nobody's cloud account
must be able to start. This module is the console's only configuration: it
reads the ``.env`` keys the console needs, validates every one of them, and
reports **every** problem at once rather than the first, because an operator at
a bench fixes a ``.env`` in one go, not one line per restart.

Two keys deserve their paragraph:

* ``ARM_RADIUS_M`` is **required, with no default**. Every g on the screen and
  every session step written in g goes through it, g is linear in it, and a
  default is how a wrong one gets used without anybody choosing it.
* ``MOTOR_MAX_RPM`` (default 300, 0..1380) is the motor-rpm ceiling with nobody
  on board (:attr:`Occupancy.BENCH`: motor uncoupled, or arm coupled with an
  empty capsule). It can never exceed the 1380 rpm nameplate here: going above
  the plate is a commissioning decision with its own flag on the training
  profile, not a number in a ``.env``.

The drive-link keys (``MOTOR_PORT``, ``MOTOR_SLAVE_ID``, ``MODBUS_*``,
``MOTOR_REG_OFFSET``) are parsed by :func:`build_drive_link`, which the bench
scripts (``scripts/drive_link.py``) call too, so the console and the scripts
cannot disagree about how the ATV320 is reached.

Two keys gate what the machine may do, and both default to "no":

* ``PROGRAMS_ENABLED`` (default false): programmed sessions, where the heart
  rate drives the speed. Milestone M5. A programmed session always has a
  person on board, so it also needs the occupied ceiling
  (``OCCUPANCY_OCCUPIED_ENABLED``, milestone M6) and must fit under it.
* ``MACHINE_API_KEY`` + ``CONVEX_URL``: the dashboard link. Blank key, no link,
  and the console runs exactly as it does offline.

No key sets a medical value. The cardiac tiers of the ONE safety supervisor,
the youngest rider a programme accepts, the resultant-g ceiling with a person
on board and the anti-nausea motion limits come from
``config/medical_parameters.json``, read and verified by :mod:`src.medical`
(:attr:`LocalConfig.medical`). The keys that used to set some of them
(:data:`MEDICAL_KEYS`) are a configuration error when still set: two sources
for one medical value is how the unsigned one ends up applied. When the
file's signature does not verify, the console starts in **bench mode only**:
``PROGRAMS_ENABLED`` and ``OCCUPANCY_OCCUPIED_ENABLED`` are forced to false,
whatever the ``.env`` says (:class:`LocalConfig` refuses the other
combination). The values are checked here against the bounds the rest of the
console holds them to, and a value outside them is a configuration problem,
signed or not.

The session record (the local black box) has its own keys, all optional:
``RECORD_ROOT`` (default ``data/records``, relative to ``raspberry-pi/``),
``RECORD_LOCAL_RETENTION_DAYS`` (default 30: how long a record already
deposited AND confirmed is kept; one that was not is never purged),
``RECORD_MACHINE_ID`` / ``RECORD_ORGANIZATION_ID`` (opaque identifiers written
in the manifest, ``unassigned`` when unset), and ``RECORD_DRIVE_SDK_FRAMES``
(default ``false``: a bench diagnostic that also records the Modbus SDK's
transport calls, at about four times the lines). The software version the
manifest carries is not a key: it is a fact about the build, read from
``raspberry-pi/VERSION`` (:func:`src.contract.read_software_version`), the
same value the console shows and announces to the dashboard. The key that
used to set it is listed in :data:`RETIRED_KEYS`.

How the page is carried has three keys: ``UI_TLS_CERT`` and ``UI_TLS_KEY`` (the
console's certificate and its private key, PEM: both or neither) and
``UI_TLS_NAMES`` (the addresses and names a tablet opens the console at, the
ones the certificate carries). All three are optional on loopback. Off
loopback the first two are required: there the console serves TLS or does not
start, as it does not start without ``UI_TOKEN``. Only the two paths are read
here; the files are opened when the console starts (:mod:`src.web.tls`).

Nothing here opens a port or reads a clock. ``env`` is passed in, so the whole
module is testable with a dict; so is the medical status, which is otherwise
the shipped file's, verified when :func:`load_local_config` is called (the
two small files of the signature are the only thing it reads).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum, unique
from pathlib import Path
from typing import Final, assert_never

from src.geometry import CONFIRMED_GEAR_RATIO, MachineGeometry
from src.medical import PARAMETERS_PATH, MedicalStatus, shipped_status
from src.motor.atv320 import (
    DEFAULT_BAUDRATE,
    DEFAULT_SLAVE_ADDRESS,
    DEFAULT_TIMEOUT,
    SerialSettings,
)
from src.motor.drive import LogicInput, RegisterMap
from src.motor.ftdi_link import SCHNEIDER_CABLE_URL, Parity
from src.motor.sto import StoSourceKind
from src.panel_status import EcgSource as _EcgSource
from src.panel_status import MotorBackend as _MotorBackend
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind, parse_kind
from src.training.motion import MotionLimits, limits_of
from src.training.plan import (
    MAX_SUBJECT_AGE_YEARS,
    MIN_SUBJECT_AGE_YEARS,
    NAMEPLATE_MOTOR_RPM,
    SUBJECT_HR_MAX_MAX,
    SUBJECT_HR_MAX_MIN,
)
from src.training.types import Occupancy, OccupancyRefused
from src.units import Bpm, GearRatio, Metres, MotorRpm, Seconds
from src.web.deps import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    TlsFiles,
    WebConfig,
    clear_http_refusal,
    token_refusal,
)

# --- Keys ----------------------------------------------------------------
KEY_MOTOR_BACKEND: Final[str] = "MOTOR_BACKEND"
KEY_STO_SOURCE: Final[str] = "STO_SOURCE"
KEY_STO_DRIVE_INPUT: Final[str] = "STO_DRIVE_INPUT"
KEY_MOTOR_PORT: Final[str] = "MOTOR_PORT"
KEY_MOTOR_SLAVE_ID: Final[str] = "MOTOR_SLAVE_ID"
KEY_MODBUS_BAUDRATE: Final[str] = "MODBUS_BAUDRATE"
KEY_MODBUS_PARITY: Final[str] = "MODBUS_PARITY"
KEY_MODBUS_TIMEOUT_S: Final[str] = "MODBUS_TIMEOUT_S"
KEY_MOTOR_REG_OFFSET: Final[str] = "MOTOR_REG_OFFSET"
KEY_MOTOR_MAX_RPM: Final[str] = "MOTOR_MAX_RPM"
KEY_ECG_SOURCE: Final[str] = "ECG_SOURCE"
KEY_BITALINO_ADDRESS: Final[str] = "BITALINO_ADDRESS"
KEY_ARM_RADIUS_M: Final[str] = "ARM_RADIUS_M"
KEY_GEAR_RATIO: Final[str] = "GEAR_RATIO"
KEY_UI_HOST: Final[str] = "UI_HOST"
KEY_UI_PORT: Final[str] = "UI_PORT"
KEY_UI_TOKEN: Final[str] = "UI_TOKEN"  # noqa: S105  # a key NAME, not a secret
KEY_UI_TLS_CERT: Final[str] = "UI_TLS_CERT"
KEY_UI_TLS_KEY: Final[str] = "UI_TLS_KEY"
KEY_UI_TLS_NAMES: Final[str] = "UI_TLS_NAMES"
KEY_OCCUPIED_ENABLED: Final[str] = "OCCUPANCY_OCCUPIED_ENABLED"
KEY_MOTION_LIMITS_PATH: Final[str] = "MOTION_LIMITS_PATH"
KEY_PROGRAMS_ENABLED: Final[str] = "PROGRAMS_ENABLED"
KEY_HR_HARD_MAX_BPM: Final[str] = "HR_HARD_MAX_BPM"
KEY_HR_CRITICAL_BPM: Final[str] = "HR_CRITICAL_BPM"
KEY_CONVEX_URL: Final[str] = "CONVEX_URL"
KEY_MACHINE_API_KEY: Final[str] = "MACHINE_API_KEY"
KEY_SENSORS: Final[str] = "SENSORS"
KEY_PRESENCE_SOURCE: Final[str] = "PRESENCE_SOURCE"
KEY_MIN_RIDER_AGE: Final[str] = "MIN_RIDER_AGE"
KEY_LEG_TIP_RADIUS_M: Final[str] = "LEG_TIP_RADIUS_M"
KEY_RECORD_ROOT: Final[str] = "RECORD_ROOT"
KEY_RECORD_RETENTION_DAYS: Final[str] = "RECORD_LOCAL_RETENTION_DAYS"
KEY_RECORD_MACHINE_ID: Final[str] = "RECORD_MACHINE_ID"
KEY_RECORD_ORGANIZATION_ID: Final[str] = "RECORD_ORGANIZATION_ID"
KEY_RECORD_DRIVE_SDK_FRAMES: Final[str] = "RECORD_DRIVE_SDK_FRAMES"

MEDICAL_FILE: Final[str] = f"config/{PARAMETERS_PATH.name}"
"""How a problem with a medical value names where it is: ``<this>:<member>``."""

MEDICAL_KEYS: Final[Mapping[str, str]] = {
    KEY_HR_HARD_MAX_BPM: "parameters.hr_hard_max_bpm",
    KEY_HR_CRITICAL_BPM: "parameters.hr_critical_bpm",
    KEY_MIN_RIDER_AGE: "parameters.min_rider_age",
    KEY_MOTION_LIMITS_PATH: "parameters.motion_limits",
}
"""Keys that used to set a medical value, each with the member of the signed file that does now.

Unlike :data:`RETIRED_KEYS`, a value still set for one of them is a
configuration error, not a warning: the console does not start while a
``.env`` says a medical value of its own.
"""

RETIRED_KEYS: Final[Mapping[str, str]] = {
    "ANHEART_SOFTWARE_VERSION": (
        "the session records are stamped with the VERSION file of the build, like the "
        "version the console shows and announces"
    ),
}
"""Keys this console used to read and no longer does, each with what replaced it.

A value still set for one of them changes nothing; the console says so once at
startup (:func:`retired_keys`) instead of leaving somebody to believe it took.
"""

# --- Defaults and bounds -------------------------------------------------
DEFAULT_MOTOR_MAX_RPM: Final[MotorRpm] = MotorRpm(300)
"""The bench ceiling, deliberately far below the nameplate (about 6 output rpm)."""

MAX_ARM_RADIUS_M: Final[float] = 5.0
"""Typo guard, not physics: the arm is 1.5 m, and "15" must not become 15 m."""

BENCH_CONSOLE_PORT: Final[int] = 8123
"""``scripts/bench_console.py`` listens here; the console must not collide with it."""

DEFAULT_RECORD_ROOT: Final[Path] = Path("data/records")
"""One directory per session, under ``raspberry-pi/`` (``data/`` is git-ignored)."""

DEFAULT_RECORD_RETENTION_DAYS: Final[int] = 30
MAX_RECORD_RETENTION_DAYS: Final[int] = 3650
"""Typo guard: ten years is not a retention anybody chose on a Pi's disk."""

UNASSIGNED: Final[str] = "unassigned"
"""The manifest's machine and organisation until somebody names them."""

RECORD_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""An opaque, path-safe identifier: the record format refuses anything else."""

TLS_NAME: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)
"""An IPv4 address or a host name, in lower case: what a tablet types before
the port, and nothing else (no scheme, no port, no path)."""

_LOCAL_HOSTS: Final[tuple[str, ...]] = ("http://localhost", "http://127.0.0.1")

RFCOMM_ADDRESS: Final[re.Pattern[str]] = re.compile(
    r"^rfcomm:[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$", re.IGNORECASE
)
"""``rfcomm:98-d3-91-fe-4e-9f``: the macOS IOBluetooth transport's address scheme."""

RFCOMM_PREFIX: Final[str] = "rfcomm:"

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


MotorBackend = _MotorBackend
"""Which drive the console commands (``MOTOR_BACKEND``).

Defined in :mod:`src.panel_status`, whose records report it, and named here
because choosing it is this module's business: both names are the same class.
"""

EcgSource = _EcgSource
"""Where the ECG comes from (``ECG_SOURCE``). Defined in :mod:`src.panel_status` likewise."""


@unique
class CameraSource(Enum):
    """Where the camera fail-safe's observations come from (``PRESENCE_SOURCE``).

    ``NONE`` is the default and means NO camera fail-safe: the console runs as
    it always has. A simulated camera is never the default, because a camera
    that reports "zone clear" without looking is worse than no camera.
    """

    NONE = "none"
    SIM_EMPTY = "sim_empty"
    """Simulated camera, capsule empty (bench sessions)."""

    SIM_OCCUPIED = "sim_occupied"
    """Simulated camera, a rider buckled in (occupied and programmed sessions)."""


@dataclass(frozen=True, slots=True)
class ConfigProblem:
    """One refused key, named, with a sentence an operator can act on."""

    key: str
    detail: str


@dataclass(frozen=True, slots=True)
class DriveLink:
    """Everything needed to reach the drive, validated by the project's own types."""

    settings: SerialSettings
    registers: RegisterMap

    def describe(self) -> str:
        """One line for a log or a console banner."""
        s = self.settings
        return (
            f"{s.port} @ {s.baudrate} {s.bytesize}{s.parity.value}{s.stopbits}, "
            f"esclave {s.slave_address}, timeout {s.timeout} s, "
            f"decalage registres {self.registers.offset:+d}"
        )


@dataclass(frozen=True, slots=True)
class CardiacTiers:
    """The supervisor's two person-specific limits. ``critical_bpm > hard_max_bpm``."""

    hard_max_bpm: Bpm
    critical_bpm: Bpm


@dataclass(frozen=True, slots=True)
class CloudConfig:
    """Where the dashboard lives and this machine's key. The key never appears in a repr."""

    url: str
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordConfig:
    """Where the session records go and what their manifests are stamped with."""

    root: Path = DEFAULT_RECORD_ROOT
    """Relative to ``raspberry-pi/`` unless absolute."""

    retention_days: int = DEFAULT_RECORD_RETENTION_DAYS
    """How long a record deposited AND confirmed is kept locally. One that was
    not deposited is never purged, whatever this says."""

    machine_id: str = UNASSIGNED
    organization_id: str = UNASSIGNED

    drive_sdk_frames: bool = False
    """Also record the Modbus SDK's own transport calls (each request sent, each
    chunk of an answer received, with their bytes), on top of the register
    transactions. A bench diagnostic for the link itself: about four times the
    lines. Only the native driver has such calls; the simulator is unaffected."""


DEFAULT_RECORD_CONFIG: Final[RecordConfig] = RecordConfig()


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalConfig:
    """Everything the local console needs, validated. Build with :func:`load_local_config`."""

    motor_backend: MotorBackend
    drive_link: DriveLink | None
    """``None`` exactly when ``motor_backend`` is ``SIM``."""

    ecg_source: EcgSource
    bitalino_address: str | None
    """``None`` exactly when ``ecg_source`` is ``SIM``."""

    geometry: MachineGeometry
    motor_max_rpm: MotorRpm
    occupied_enabled: bool
    """A person on board may be accepted. Never ``True`` with unsigned medical parameters."""

    web: WebConfig
    medical: MedicalStatus
    """The medical parameters and the verdict of their signature (:mod:`src.medical`)."""

    motion: MotionLimits
    """The anti-nausea limits of the medical file, checked against the limiter's bounds."""

    programs_enabled: bool = False
    """Programmed (heart-rate-driven) sessions. Never ``True`` with unsigned medical parameters."""

    cloud: CloudConfig | None = None
    """``None``: no dashboard link, the console is purely local."""

    sensors: tuple[SensorKind, ...] = (SensorKind.ECG,)
    """The BITalino channels acquired and shown (``SENSORS``). Always contains ECG."""

    camera: CameraSource = CameraSource.NONE
    sto_source: StoSourceKind = StoSourceKind.NONE
    sto_drive_input: LogicInput | None = None
    leg_tip_radius: Metres | None = None
    """The rider's farthest point from the axis (``LEG_TIP_RADIUS_M``); the anti-nausea
    g-rate limit is judged there. ``None``: judged at ``ARM_RADIUS_M``, which
    understates the load at the feet (the CAD bounds the leg tip at 2.43 m)."""

    record: RecordConfig = DEFAULT_RECORD_CONFIG
    """The local black box: where it writes and what it stamps."""

    def __post_init__(self) -> None:
        """Refuse a configuration that would let unsigned medical parameters near a person.

        :func:`load_local_config` never builds one; this makes the bench mode a
        property of every :class:`LocalConfig`, not of one function. Raises
        ``ValueError``, at startup, with nothing turning.
        """
        if not self.medical.signed and (self.programs_enabled or self.occupied_enabled):
            raise ValueError(
                f"{self.medical.warning} : PROGRAMS_ENABLED et "
                "OCCUPANCY_OCCUPIED_ENABLED doivent etre faux"
            )

    @property
    def tiers(self) -> CardiacTiers:
        """The supervisor's two cardiac tiers, from the medical file."""
        parameters = self.medical.parameters
        return CardiacTiers(
            hard_max_bpm=parameters.hr_hard_max_bpm, critical_bpm=parameters.hr_critical_bpm
        )

    @property
    def min_rider_age(self) -> int:
        """Programmed sessions refuse a rider younger than this, or of unknown age."""
        return self.medical.parameters.min_rider_age

    def ceiling_for(self, occupancy: Occupancy) -> Result[MotorRpm, OccupancyRefused]:
        """The motor-rpm ceiling for ``occupancy``, or why motion is refused.

        BENCH: ``MOTOR_MAX_RPM``, which is never above the nameplate. OCCUPIED:
        refused while the medical parameters are not signed, and while
        ``OCCUPANCY_OCCUPIED_ENABLED`` is false; once enabled, the lower of
        ``MOTOR_MAX_RPM`` and the medical file's resultant-g ceiling with a
        person on board.
        """
        match occupancy:
            case Occupancy.BENCH:
                return Ok(self.motor_max_rpm)
            case Occupancy.OCCUPIED:
                warning = self.medical.warning
                if warning is not None:
                    return Err(OccupancyRefused(occupancy, f"personne a bord refusee : {warning}"))
                if not self.occupied_enabled:
                    return Err(
                        OccupancyRefused(
                            occupancy,
                            "personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false "
                            "(jalon M6, accords ingenierie et medical requis)",
                        )
                    )
                trial = self.geometry.motor_rpm_for(self.medical.parameters.occupied_ceiling_g)
                return Ok(MotorRpm(min(self.motor_max_rpm, trial)))
        raise assert_never(occupancy)


# =========================================================================
# Parsing
# =========================================================================


def _text(env: Mapping[str, str], key: str, default: str) -> str:
    """The stripped value, or ``default`` when unset or blank (as dotenv leaves it)."""
    value = env.get(key, "").strip()
    return value or default


def _int(text: str, key: str) -> Result[int, ConfigProblem]:
    try:
        return Ok(int(text, 0))
    except ValueError:
        return Err(ConfigProblem(key, f"{text!r} n'est pas un entier"))


def _finite_float(text: str, key: str) -> Result[float, ConfigProblem]:
    try:
        value = float(text)
    except ValueError:
        return Err(ConfigProblem(key, f"{text!r} n'est pas un nombre"))
    if not math.isfinite(value):
        return Err(ConfigProblem(key, f"{text!r} n'est pas un nombre fini"))
    return Ok(value)


@dataclass(frozen=True, slots=True)
class _LinkNumbers:
    slave_address: int
    baudrate: int
    timeout: Seconds
    offset: int


def _link_numbers(
    slave: str, baud: str, timeout: str, offset: str
) -> Result[_LinkNumbers, ConfigProblem]:
    slave_address = _int(slave, KEY_MOTOR_SLAVE_ID)
    if isinstance(slave_address, Err):
        return slave_address
    baudrate = _int(baud, KEY_MODBUS_BAUDRATE)
    if isinstance(baudrate, Err):
        return baudrate
    seconds = _finite_float(timeout, KEY_MODBUS_TIMEOUT_S)
    if isinstance(seconds, Err):
        return seconds
    register_offset = _int(offset, KEY_MOTOR_REG_OFFSET)
    if isinstance(register_offset, Err):
        return register_offset
    return Ok(
        _LinkNumbers(
            slave_address=slave_address.value,
            baudrate=baudrate.value,
            timeout=Seconds(seconds.value),
            offset=register_offset.value,
        )
    )


def build_drive_link(
    *, port: str, slave: str, baud: str, parity: str, timeout: str, offset: str
) -> Result[DriveLink, ConfigProblem]:
    """Validate the link settings into the project's own types. Shared with the scripts."""
    parities = {member.value: member for member in Parity}
    if parity not in parities:
        return Err(ConfigProblem(KEY_MODBUS_PARITY, f"{parity!r} n'est pas N, E ou O"))
    numbers = _link_numbers(slave, baud, timeout, offset)
    if isinstance(numbers, Err):
        return numbers
    try:
        return Ok(
            DriveLink(
                settings=SerialSettings(
                    port=port,
                    baudrate=numbers.value.baudrate,
                    parity=parities[parity],
                    timeout=numbers.value.timeout,
                    slave_address=numbers.value.slave_address,
                ),
                registers=RegisterMap(offset=numbers.value.offset),
            )
        )
    except ValueError as error:
        return Err(ConfigProblem("MOTOR_*", str(error)))


def drive_link_from_env(env: Mapping[str, str]) -> Result[DriveLink, ConfigProblem]:
    """The link from ``MOTOR_*``/``MODBUS_*``, each defaulting to the measured bench value."""
    return build_drive_link(
        port=_text(env, KEY_MOTOR_PORT, SCHNEIDER_CABLE_URL),
        slave=_text(env, KEY_MOTOR_SLAVE_ID, str(DEFAULT_SLAVE_ADDRESS)),
        baud=_text(env, KEY_MODBUS_BAUDRATE, str(DEFAULT_BAUDRATE)),
        parity=_text(env, KEY_MODBUS_PARITY, Parity.EVEN.value).upper(),
        timeout=_text(env, KEY_MODBUS_TIMEOUT_S, str(DEFAULT_TIMEOUT)),
        offset=_text(env, KEY_MOTOR_REG_OFFSET, "0"),
    )


def _backend(env: Mapping[str, str]) -> Result[MotorBackend, ConfigProblem]:
    text = _text(env, KEY_MOTOR_BACKEND, "").lower()
    for member in MotorBackend:
        if member.value == text:
            return Ok(member)
    return Err(ConfigProblem(KEY_MOTOR_BACKEND, f"{text!r} : attendu 'sim' ou 'serial'"))


def _ecg_source(env: Mapping[str, str]) -> Result[EcgSource, ConfigProblem]:
    text = _text(env, KEY_ECG_SOURCE, "").lower()
    for member in EcgSource:
        if member.value == text:
            return Ok(member)
    return Err(ConfigProblem(KEY_ECG_SOURCE, f"{text!r} : attendu 'sim', 'serial' ou 'rfcomm'"))


def bitalino_address(
    env: Mapping[str, str], source: EcgSource
) -> Result[str | None, ConfigProblem]:
    """``BITALINO_ADDRESS`` checked against the scheme ``source`` needs; ``None`` for SIM."""
    address = _text(env, KEY_BITALINO_ADDRESS, "")
    match source:
        case EcgSource.SIM:
            return Ok(None)
        case EcgSource.RFCOMM:
            if RFCOMM_ADDRESS.match(address) is None:
                return Err(
                    ConfigProblem(
                        KEY_BITALINO_ADDRESS,
                        f"{address!r} : ECG_SOURCE=rfcomm attend rfcomm:XX-XX-XX-XX-XX-XX",
                    )
                )
            return Ok(address.lower())
        case EcgSource.SERIAL:
            if not address or address.lower().startswith(RFCOMM_PREFIX):
                return Err(
                    ConfigProblem(
                        KEY_BITALINO_ADDRESS,
                        f"{address!r} : ECG_SOURCE=serial attend un port serie "
                        "(/dev/rfcomm0, COM4) ou une adresse MAC",
                    )
                )
            return Ok(address)
    raise assert_never(source)


def _radius(env: Mapping[str, str]) -> Result[Metres, ConfigProblem]:
    text = _text(env, KEY_ARM_RADIUS_M, "")
    if not text:
        return Err(
            ConfigProblem(
                KEY_ARM_RADIUS_M,
                "obligatoire, sans valeur par defaut : rayon mesure de l'axe a l'occupant, en m",
            )
        )
    value = _finite_float(text, KEY_ARM_RADIUS_M)
    if isinstance(value, Err):
        return value
    if value.value <= 0.0 or value.value > MAX_ARM_RADIUS_M:
        return Err(
            ConfigProblem(KEY_ARM_RADIUS_M, f"{value.value} m hors de ]0, {MAX_ARM_RADIUS_M}]")
        )
    return Ok(Metres(value.value))


def _gear_ratio(env: Mapping[str, str]) -> Result[GearRatio, ConfigProblem]:
    value = _finite_float(_text(env, KEY_GEAR_RATIO, str(CONFIRMED_GEAR_RATIO)), KEY_GEAR_RATIO)
    if isinstance(value, Err):
        return value
    if value.value <= 0.0:
        return Err(ConfigProblem(KEY_GEAR_RATIO, f"{value.value} doit etre positif"))
    return Ok(GearRatio(value.value))


def _max_rpm(env: Mapping[str, str]) -> Result[MotorRpm, ConfigProblem]:
    value = _int(_text(env, KEY_MOTOR_MAX_RPM, str(DEFAULT_MOTOR_MAX_RPM)), KEY_MOTOR_MAX_RPM)
    if isinstance(value, Err):
        return value
    if value.value < 0 or value.value > NAMEPLATE_MOTOR_RPM:
        return Err(
            ConfigProblem(
                KEY_MOTOR_MAX_RPM,
                f"{value.value} tr/min hors de 0..{NAMEPLATE_MOTOR_RPM} (plaque signaletique)",
            )
        )
    return Ok(MotorRpm(value.value))


def _flag(env: Mapping[str, str], key: str) -> Result[bool, ConfigProblem]:
    text = _text(env, key, "false").lower()
    if text in _TRUE:
        return Ok(True)
    if text in _FALSE:
        return Ok(False)
    return Err(ConfigProblem(key, f"{text!r} : attendu true ou false"))


def _cloud(env: Mapping[str, str]) -> Result[CloudConfig | None, ConfigProblem]:
    """No key, no link. A key needs an https URL (plain http only to this host)."""
    key = _text(env, KEY_MACHINE_API_KEY, "")
    if not key:
        return Ok(None)
    url = _text(env, KEY_CONVEX_URL, "").rstrip("/")
    if not (url.startswith("https://") or url.startswith(_LOCAL_HOSTS)):
        return Err(
            ConfigProblem(
                KEY_CONVEX_URL,
                f"{url!r} : attendu https://<deploiement>.convex.site avec {KEY_MACHINE_API_KEY}",
            )
        )
    return Ok(CloudConfig(url=url, api_key=key))


def _sensors(env: Mapping[str, str]) -> Result[tuple[SensorKind, ...], ConfigProblem]:
    """``SENSORS=ECG,EDA,RESP``: known names, no duplicates, ECG always present."""
    text = _text(env, KEY_SENSORS, SensorKind.ECG.value)
    kinds: list[SensorKind] = []
    for name in (part for part in text.split(",") if part.strip()):
        kind = parse_kind(name)
        if kind is None:
            known = ", ".join(k.value for k in SensorKind)
            return Err(ConfigProblem(KEY_SENSORS, f"{name.strip()!r} inconnu (connus : {known})"))
        if kind not in kinds:
            kinds.append(kind)
    if SensorKind.ECG not in kinds:
        return Err(
            ConfigProblem(KEY_SENSORS, "ECG obligatoire : la frequence cardiaque pilote le moteur")
        )
    return Ok(tuple(kinds))


def _medical_keys(env: Mapping[str, str]) -> tuple[ConfigProblem, ...]:
    """Every :data:`MEDICAL_KEYS` still given a value: one problem each."""
    return tuple(
        ConfigProblem(
            key,
            f"n'est plus lue : la valeur est {member} de {MEDICAL_FILE}, signe ; "
            "retirer cette cle du .env",
        )
        for key, member in MEDICAL_KEYS.items()
        if env.get(key, "").strip()
    )


def _medical_values(medical: MedicalStatus) -> Result[MotionLimits, tuple[ConfigProblem, ...]]:
    """The medical values held to the bounds the console holds every value to.

    The same bounds the ``.env`` keys had: a heart rate the profile checks can
    believe, an age the formula holds for, and the motion limiter's own
    limits. Signed or not: a signature vouches for who decided, not that the
    number was typed right.
    """
    parameters = medical.parameters
    problems = [
        ConfigProblem(
            f"{MEDICAL_FILE}:{member}",
            f"{bpm} bpm hors de {SUBJECT_HR_MAX_MIN}..{SUBJECT_HR_MAX_MAX}",
        )
        for member, bpm in (
            ("hr_hard_max_bpm", parameters.hr_hard_max_bpm),
            ("hr_critical_bpm", parameters.hr_critical_bpm),
        )
        if not SUBJECT_HR_MAX_MIN <= bpm <= SUBJECT_HR_MAX_MAX
    ]
    if not MIN_SUBJECT_AGE_YEARS <= parameters.min_rider_age <= MAX_SUBJECT_AGE_YEARS:
        problems.append(
            ConfigProblem(
                f"{MEDICAL_FILE}:min_rider_age",
                f"{parameters.min_rider_age} ans hors de "
                f"{MIN_SUBJECT_AGE_YEARS}..{MAX_SUBJECT_AGE_YEARS}",
            )
        )
    try:
        motion = limits_of(parameters.motion_limits)
    except ValueError as error:
        problems.append(ConfigProblem(f"{MEDICAL_FILE}:motion_limits", str(error)))
        return Err(tuple(problems))
    if problems:
        return Err(tuple(problems))
    return Ok(motion)


def _leg_tip(env: Mapping[str, str], arm: Metres | None) -> Result[Metres | None, ConfigProblem]:
    text = _text(env, KEY_LEG_TIP_RADIUS_M, "")
    if not text:
        return Ok(None)
    value = _finite_float(text, KEY_LEG_TIP_RADIUS_M)
    if isinstance(value, Err):
        return value
    low = 0.0 if arm is None else float(arm)
    if value.value < low or value.value <= 0.0 or value.value > MAX_ARM_RADIUS_M:
        return Err(
            ConfigProblem(
                KEY_LEG_TIP_RADIUS_M,
                f"{value.value} m hors de [{KEY_ARM_RADIUS_M}, {MAX_ARM_RADIUS_M}] : la pointe "
                "du pied est au moins aussi loin de l'axe que le point de reference",
            )
        )
    return Ok(Metres(value.value))


def default_sto_source(backend: MotorBackend) -> StoSourceKind:
    """Provisional policy: simulation ready, real hardware unconfigured."""
    match backend:
        case MotorBackend.SIM:
            return StoSourceKind.SIM_READY
        case MotorBackend.SERIAL:
            return StoSourceKind.NONE
    raise assert_never(backend)


def _sto(
    env: Mapping[str, str], backend: MotorBackend
) -> Result[tuple[StoSourceKind, LogicInput | None], tuple[ConfigProblem, ...]]:
    text = _text(env, KEY_STO_SOURCE, default_sto_source(backend).value).lower()
    source = next((source for source in StoSourceKind if source.value == text), None)
    input_text = _text(env, KEY_STO_DRIVE_INPUT, "")
    input_ = next((input_ for input_ in LogicInput if input_.name == input_text), None)
    problems: list[ConfigProblem] = []
    if source is None:
        known = ", ".join(source.value for source in StoSourceKind)
        problems.append(ConfigProblem(KEY_STO_SOURCE, f"{text!r} inconnu (connus : {known})"))
    if input_text and input_ is None:
        problems.append(
            ConfigProblem(KEY_STO_DRIVE_INPUT, "attendu LI1, LI2, LI3, LI4, LI5 ou LI6")
        )
    if source is StoSourceKind.DRIVE_INPUT and not input_text:
        problems.append(
            ConfigProblem(KEY_STO_DRIVE_INPUT, "obligatoire avec STO_SOURCE=drive_input")
        )
    if input_text and source is not StoSourceKind.DRIVE_INPUT:
        problems.append(ConfigProblem(KEY_STO_DRIVE_INPUT, "reserve a STO_SOURCE=drive_input"))
    if backend is MotorBackend.SERIAL and source in (
        StoSourceKind.SIM_READY,
        StoSourceKind.SIM_OPEN,
    ):
        problems.append(ConfigProblem(KEY_STO_SOURCE, "sim_* interdit avec MOTOR_BACKEND=serial"))
    if problems or source is None:
        return Err(tuple(problems))
    return Ok((source, input_))


def _presence(env: Mapping[str, str]) -> Result[CameraSource, ConfigProblem]:
    text = _text(env, KEY_PRESENCE_SOURCE, CameraSource.NONE.value).lower()
    for source in CameraSource:
        if source.value == text:
            return Ok(source)
    known = ", ".join(s.value for s in CameraSource)
    return Err(ConfigProblem(KEY_PRESENCE_SOURCE, f"{text!r} inconnu (connus : {known})"))


def _record_identifier(
    env: Mapping[str, str], key: str, pattern: re.Pattern[str], default: str
) -> Result[str, ConfigProblem]:
    text = _text(env, key, default)
    if pattern.fullmatch(text) is None:
        return Err(
            ConfigProblem(
                key,
                f"{text!r} : identifiant opaque attendu (lettres ASCII, chiffres, _ et -, "
                "jamais un nom)",
            )
        )
    return Ok(text)


def _record_retention(env: Mapping[str, str]) -> Result[int, ConfigProblem]:
    days = _int(
        _text(env, KEY_RECORD_RETENTION_DAYS, str(DEFAULT_RECORD_RETENTION_DAYS)),
        KEY_RECORD_RETENTION_DAYS,
    )
    if isinstance(days, Err):
        return days
    if not 0 <= days.value <= MAX_RECORD_RETENTION_DAYS:
        return Err(
            ConfigProblem(
                KEY_RECORD_RETENTION_DAYS,
                f"{days.value} jours hors de 0..{MAX_RECORD_RETENTION_DAYS}",
            )
        )
    return days


def _record(env: Mapping[str, str]) -> Result[RecordConfig, tuple[ConfigProblem, ...]]:
    """The black box's keys. Every problem, not only the first."""
    retention = _record_retention(env)
    machine = _record_identifier(env, KEY_RECORD_MACHINE_ID, RECORD_IDENTIFIER, UNASSIGNED)
    organization = _record_identifier(
        env, KEY_RECORD_ORGANIZATION_ID, RECORD_IDENTIFIER, UNASSIGNED
    )
    sdk_frames = _flag(env, KEY_RECORD_DRIVE_SDK_FRAMES)
    if (
        isinstance(retention, Err)
        or isinstance(machine, Err)
        or isinstance(organization, Err)
        or isinstance(sdk_frames, Err)
    ):
        return Err(
            tuple(
                result.error
                for result in (retention, machine, organization, sdk_frames)
                if isinstance(result, Err)
            )
        )
    return Ok(
        RecordConfig(
            root=Path(_text(env, KEY_RECORD_ROOT, str(DEFAULT_RECORD_ROOT))),
            retention_days=retention.value,
            machine_id=machine.value,
            organization_id=organization.value,
            drive_sdk_frames=sdk_frames.value,
        )
    )


def _port(env: Mapping[str, str]) -> Result[int, ConfigProblem]:
    port = _int(_text(env, KEY_UI_PORT, str(DEFAULT_PORT)), KEY_UI_PORT)
    if isinstance(port, Err):
        return port
    if port.value == BENCH_CONSOLE_PORT:
        return Err(
            ConfigProblem(KEY_UI_PORT, f"{port.value} est le port de scripts/bench_console.py")
        )
    return port


def _tls(env: Mapping[str, str]) -> Result[TlsFiles | None, ConfigProblem]:
    """The certificate and its key: both, or neither. The files are not opened here."""
    certificate_path = _text(env, KEY_UI_TLS_CERT, "")
    key_path = _text(env, KEY_UI_TLS_KEY, "")
    if not certificate_path and not key_path:
        return Ok(None)
    if not certificate_path or not key_path:
        present, absent = (
            (KEY_UI_TLS_CERT, KEY_UI_TLS_KEY)
            if certificate_path
            else (KEY_UI_TLS_KEY, KEY_UI_TLS_CERT)
        )
        return Err(
            ConfigProblem(
                absent,
                f"obligatoire avec {present} : le certificat de la console et sa cle privee "
                "vont ensemble",
            )
        )
    return Ok(TlsFiles(certificate_path=Path(certificate_path), key_path=Path(key_path)))


def _tls_names(env: Mapping[str, str], *, served: bool) -> Result[tuple[str, ...], ConfigProblem]:
    """``UI_TLS_NAMES=10.42.0.1,anheart.internal``: IPv4 addresses and host names."""
    names: list[str] = []
    for part in (part.strip().lower() for part in _text(env, KEY_UI_TLS_NAMES, "").split(",")):
        if not part or part in names:
            continue
        if TLS_NAME.fullmatch(part) is None:
            return Err(
                ConfigProblem(
                    KEY_UI_TLS_NAMES,
                    f"{part!r} : attendu une adresse IPv4 ou un nom d'hote, ceux que porte "
                    "le certificat, separes par des virgules",
                )
            )
        names.append(part)
    if names and not served:
        return Err(
            ConfigProblem(
                KEY_UI_TLS_NAMES,
                f"sans effet sans certificat : renseigner {KEY_UI_TLS_CERT} et "
                f"{KEY_UI_TLS_KEY}, ou vider cette cle",
            )
        )
    return Ok(tuple(names))


def _web(env: Mapping[str, str]) -> Result[WebConfig, tuple[ConfigProblem, ...]]:
    """Where the page listens and how it is carried. Every problem, not only the first."""
    host = _text(env, KEY_UI_HOST, DEFAULT_HOST)
    token = _text(env, KEY_UI_TOKEN, "") or None
    port = _port(env)
    tls = _tls(env)
    # Half a certificate is said once, as that: the names are not blamed for it.
    names = _tls_names(env, served=isinstance(tls, Err) or tls.value is not None)
    refusals = [
        ConfigProblem(key, refusal)
        for key, refusal in (
            (f"{KEY_UI_HOST}/{KEY_UI_PORT}/{KEY_UI_TOKEN}", token_refusal(host, token)),
            (
                f"{KEY_UI_HOST}/{KEY_UI_TLS_CERT}/{KEY_UI_TLS_KEY}",
                clear_http_refusal(host, tls.value) if isinstance(tls, Ok) else None,
            ),
        )
        if refusal is not None
    ]
    if isinstance(port, Err) or isinstance(tls, Err) or isinstance(names, Err) or refusals:
        return Err(
            (
                *(result.error for result in (port, tls, names) if isinstance(result, Err)),
                *refusals,
            )
        )
    try:
        return Ok(
            WebConfig(host=host, port=port.value, token=token, tls=tls.value, names=names.value)
        )
    except ValueError as error:
        return Err((ConfigProblem(f"{KEY_UI_HOST}/{KEY_UI_PORT}/{KEY_UI_TOKEN}", str(error)),))


def retired_keys(env: Mapping[str, str]) -> tuple[str, ...]:
    """The :data:`RETIRED_KEYS` that ``env`` still gives a value to. Nothing reads them."""
    return tuple(key for key in RETIRED_KEYS if env.get(key, "").strip())


def load_local_config(
    env: Mapping[str, str], medical: MedicalStatus | None = None
) -> Result[LocalConfig, tuple[ConfigProblem, ...]]:
    """Every key validated; every problem reported, not only the first.

    ``medical``: the medical parameters and their verdict; ``None``, which is
    what the console passes, verifies the shipped file now
    (:func:`~src.medical.shipped_status`). Unsigned, the configuration is the
    bench mode's: programmes and a person on board off, whatever ``env`` says.
    """
    status = shipped_status() if medical is None else medical
    backend = _backend(env)
    sto = _sto(env, backend.value if isinstance(backend, Ok) else MotorBackend.SERIAL)
    link: Result[DriveLink | None, ConfigProblem] = (
        drive_link_from_env(env)
        if isinstance(backend, Ok) and backend.value is MotorBackend.SERIAL
        else Ok(None)
    )
    source = _ecg_source(env)
    address: Result[str | None, ConfigProblem] = (
        bitalino_address(env, source.value) if isinstance(source, Ok) else Ok(None)
    )
    radius = _radius(env)
    ratio = _gear_ratio(env)
    max_rpm = _max_rpm(env)
    occupied = _flag(env, KEY_OCCUPIED_ENABLED)
    web = _web(env)
    programs = _flag(env, KEY_PROGRAMS_ENABLED)
    cloud = _cloud(env)
    sensors = _sensors(env)
    presence = _presence(env)
    leg_tip = _leg_tip(env, radius.value if isinstance(radius, Ok) else None)
    record = _record(env)
    motion = _medical_values(status)

    problems = (
        tuple(
            result.error
            for result in (
                backend,
                link,
                source,
                address,
                radius,
                ratio,
                max_rpm,
                occupied,
                programs,
                cloud,
                sensors,
                presence,
                leg_tip,
            )
            if isinstance(result, Err)
        )
        + (web.error if isinstance(web, Err) else ())
        + (record.error if isinstance(record, Err) else ())
        + (sto.error if isinstance(sto, Err) else ())
        + _medical_keys(env)
        + (motion.error if isinstance(motion, Err) else ())
    )
    if (
        problems
        or isinstance(record, Err)
        or isinstance(sto, Err)
        or isinstance(backend, Err)
        or isinstance(link, Err)
        or isinstance(source, Err)
        or isinstance(address, Err)
        or isinstance(radius, Err)
        or isinstance(ratio, Err)
        or isinstance(max_rpm, Err)
        or isinstance(occupied, Err)
        or isinstance(web, Err)
        or isinstance(programs, Err)
        or isinstance(cloud, Err)
        or isinstance(sensors, Err)
        or isinstance(presence, Err)
        or isinstance(leg_tip, Err)
        or isinstance(motion, Err)
    ):
        return Err(problems)
    return Ok(
        LocalConfig(
            motor_backend=backend.value,
            drive_link=link.value,
            ecg_source=source.value,
            bitalino_address=address.value,
            geometry=MachineGeometry(radius=radius.value, ratio=ratio.value),
            motor_max_rpm=max_rpm.value,
            # The bench mode: unsigned medical parameters switch both gates off.
            occupied_enabled=occupied.value and status.signed,
            web=web.value,
            medical=status,
            motion=motion.value,
            programs_enabled=programs.value and status.signed,
            cloud=cloud.value,
            sensors=sensors.value,
            camera=presence.value,
            sto_source=sto.value[0],
            sto_drive_input=sto.value[1],
            leg_tip_radius=leg_tip.value,
            record=record.value,
        )
    )
