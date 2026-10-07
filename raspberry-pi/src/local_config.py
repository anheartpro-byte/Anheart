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

Three keys gate what the machine may do, and all three default to "no":

* ``PROGRAMS_ENABLED`` (default false): programmed sessions, where the heart
  rate drives the speed. Milestone M5. A programmed session always has a
  person on board, so it also needs the occupied ceiling
  (``OCCUPANCY_OCCUPIED_ENABLED``, milestone M6) and must fit under it.
* ``HR_HARD_MAX_BPM`` / ``HR_CRITICAL_BPM`` (default 148 / 158): the cardiac
  tiers of the ONE safety supervisor. They come from the medical screening of
  the riders this machine is set up for, and every programme must carry the
  same two numbers (the runtime refuses a mismatch). A jog zone around
  150 bpm needs higher tiers than the defaults: that is a medical decision,
  made here, deliberately, and never by a dashboard.
* ``MACHINE_API_KEY`` + ``CONVEX_URL``: the dashboard link. Blank key, no link,
  and the console runs exactly as it does offline.

The session record (the local black box) has its own keys, all optional:
``RECORD_ROOT`` (default ``data/records``, relative to ``raspberry-pi/``),
``RECORD_LOCAL_RETENTION_DAYS`` (default 30: how long a record already
deposited AND confirmed is kept; one that was not is never purged),
``RECORD_MACHINE_ID`` / ``RECORD_ORGANIZATION_ID`` (opaque identifiers written
in the manifest, ``unassigned`` when unset) and ``ANHEART_SOFTWARE_VERSION``.

Nothing here opens a port or reads a clock. ``env`` is passed in, so the whole
module is testable with a dict.
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
from src.motor.atv320 import (
    DEFAULT_BAUDRATE,
    DEFAULT_SLAVE_ADDRESS,
    DEFAULT_TIMEOUT,
    SerialSettings,
)
from src.motor.drive import RegisterMap
from src.motor.ftdi_link import SCHNEIDER_CABLE_URL, Parity
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind, parse_kind
from src.training.plan import (
    MAX_SUBJECT_AGE_YEARS,
    MIN_SUBJECT_AGE_YEARS,
    NAMEPLATE_MOTOR_RPM,
    SUBJECT_HR_MAX_MAX,
    SUBJECT_HR_MAX_MIN,
)
from src.training.types import Occupancy, OccupancyRefused
from src.units import Bpm, GearRatio, Metres, MotorRpm, ResultantG, Seconds
from src.web.deps import DEFAULT_HOST, DEFAULT_PORT, WebConfig

# --- Keys ----------------------------------------------------------------
KEY_MOTOR_BACKEND: Final[str] = "MOTOR_BACKEND"
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
KEY_SOFTWARE_VERSION: Final[str] = "ANHEART_SOFTWARE_VERSION"

# --- Defaults and bounds -------------------------------------------------
DEFAULT_MOTOR_MAX_RPM: Final[MotorRpm] = MotorRpm(300)
"""The bench ceiling, deliberately far below the nameplate (about 6 output rpm)."""

MAX_ARM_RADIUS_M: Final[float] = 5.0
"""Typo guard, not physics: the arm is 1.5 m, and "15" must not become 15 m."""

BENCH_CONSOLE_PORT: Final[int] = 8123
"""``scripts/bench_console.py`` listens here; the console must not collide with it."""

DEFAULT_MOTION_LIMITS_PATH: Final[Path] = Path("config/motion_limits.json")

DEFAULT_RECORD_ROOT: Final[Path] = Path("data/records")
"""One directory per session, under ``raspberry-pi/`` (``data/`` is git-ignored)."""

DEFAULT_RECORD_RETENTION_DAYS: Final[int] = 30
MAX_RECORD_RETENTION_DAYS: Final[int] = 3650
"""Typo guard: ten years is not a retention anybody chose on a Pi's disk."""

UNASSIGNED: Final[str] = "unassigned"
"""The manifest's machine and organisation until somebody names them."""

UNVERSIONED: Final[str] = "unversioned"
"""The manifest's software version when the deployment did not state one."""

RECORD_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""An opaque, path-safe identifier: the record format refuses anything else."""

RECORD_VERSION: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.+-]{1,128}")

OCCUPIED_INITIAL_RESULTANT_G: Final[ResultantG] = ResultantG(1.2)
"""First-trial ceiling with a person on board, about 990 motor rpm at 1.5 m.

[MED][ING] placeholder: unreachable until ``OCCUPANCY_OCCUPIED_ENABLED`` is set,
which milestone M6 gates on written engineering and medical sign-off.
"""

DEFAULT_HR_HARD_MAX_BPM: Final[Bpm] = Bpm(148)
DEFAULT_HR_CRITICAL_BPM: Final[Bpm] = Bpm(158)
"""The tiers of ``config/profiles.default.json``, screened for a 65-year-old."""

DEFAULT_MIN_RIDER_AGE: Final[int] = 18
"""Youngest rider a PROGRAMMED session accepts. [MED] A conservative default: the
simulation battery showed a 10-year-old accepted on the adult ceiling with nothing
to stop it. Lowering it is a medical decision, made here, never by a dashboard."""

_LOCAL_HOSTS: Final[tuple[str, ...]] = ("http://localhost", "http://127.0.0.1")

RFCOMM_ADDRESS: Final[re.Pattern[str]] = re.compile(
    r"^rfcomm:[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$", re.IGNORECASE
)
"""``rfcomm:98-d3-91-fe-4e-9f``: the macOS IOBluetooth transport's address scheme."""

RFCOMM_PREFIX: Final[str] = "rfcomm:"

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_FALSE: Final[frozenset[str]] = frozenset({"0", "false", "no", "off"})


@unique
class MotorBackend(Enum):
    """Which drive the console commands."""

    SIM = "sim"
    """``SimulatedDrive``: a full dry run, nothing opened."""

    SERIAL = "serial"
    """The ATV320 over Modbus RTU, through ``src/motor/atv320.py``."""


@unique
class EcgSource(Enum):
    """Where the ECG comes from."""

    SIM = "sim"
    """``SimulatedBitalinoClient`` fed by the physiology plant."""

    SERIAL = "serial"
    """A serial device (``/dev/rfcomm0`` on the Pi, ``COM4`` on Windows)."""

    RFCOMM = "rfcomm"
    """macOS IOBluetooth RFCOMM, address ``rfcomm:XX-XX-XX-XX-XX-XX``."""


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


DEFAULT_TIERS: Final[CardiacTiers] = CardiacTiers(
    hard_max_bpm=DEFAULT_HR_HARD_MAX_BPM, critical_bpm=DEFAULT_HR_CRITICAL_BPM
)


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
    software_version: str = UNVERSIONED


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
    web: WebConfig
    motion_limits_path: Path
    programs_enabled: bool = False
    """Programmed (heart-rate-driven) sessions. See the module docstring."""

    tiers: CardiacTiers = DEFAULT_TIERS
    cloud: CloudConfig | None = None
    """``None``: no dashboard link, the console is purely local."""

    sensors: tuple[SensorKind, ...] = (SensorKind.ECG,)
    """The BITalino channels acquired and shown (``SENSORS``). Always contains ECG."""

    camera: CameraSource = CameraSource.NONE
    min_rider_age: int = DEFAULT_MIN_RIDER_AGE
    """Programmed sessions refuse a rider younger than this, or of unknown age."""

    leg_tip_radius: Metres | None = None
    """The rider's farthest point from the axis (``LEG_TIP_RADIUS_M``); the anti-nausea
    g-rate limit is judged there. ``None``: judged at ``ARM_RADIUS_M``, which
    understates the load at the feet (the CAD bounds the leg tip at 2.43 m)."""

    record: RecordConfig = DEFAULT_RECORD_CONFIG
    """The local black box: where it writes and what it stamps."""

    def ceiling_for(self, occupancy: Occupancy) -> Result[MotorRpm, OccupancyRefused]:
        """The motor-rpm ceiling for ``occupancy``, or why motion is refused.

        BENCH: ``MOTOR_MAX_RPM``, which is never above the nameplate. OCCUPIED:
        refused while ``OCCUPANCY_OCCUPIED_ENABLED`` is false; once enabled, the
        lower of ``MOTOR_MAX_RPM`` and the first-trial resultant-g ceiling.
        """
        match occupancy:
            case Occupancy.BENCH:
                return Ok(self.motor_max_rpm)
            case Occupancy.OCCUPIED:
                if not self.occupied_enabled:
                    return Err(
                        OccupancyRefused(
                            occupancy,
                            "personne a bord refusee : OCCUPANCY_OCCUPIED_ENABLED=false "
                            "(jalon M6, accords ingenierie et medical requis)",
                        )
                    )
                trial = self.geometry.motor_rpm_for(OCCUPIED_INITIAL_RESULTANT_G)
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


def _bpm(env: Mapping[str, str], key: str, default: Bpm) -> Result[Bpm, ConfigProblem]:
    value = _int(_text(env, key, str(default)), key)
    if isinstance(value, Err):
        return value
    if not SUBJECT_HR_MAX_MIN <= value.value <= SUBJECT_HR_MAX_MAX:
        return Err(
            ConfigProblem(
                key, f"{value.value} bpm hors de {SUBJECT_HR_MAX_MIN}..{SUBJECT_HR_MAX_MAX}"
            )
        )
    return Ok(Bpm(value.value))


def _tiers(env: Mapping[str, str]) -> Result[CardiacTiers, ConfigProblem]:
    hard = _bpm(env, KEY_HR_HARD_MAX_BPM, DEFAULT_HR_HARD_MAX_BPM)
    if isinstance(hard, Err):
        return hard
    critical = _bpm(env, KEY_HR_CRITICAL_BPM, DEFAULT_HR_CRITICAL_BPM)
    if isinstance(critical, Err):
        return critical
    if critical.value <= hard.value:
        return Err(
            ConfigProblem(
                KEY_HR_CRITICAL_BPM,
                f"{critical.value} bpm doit etre au-dessus de {KEY_HR_HARD_MAX_BPM} "
                f"({hard.value} bpm)",
            )
        )
    return Ok(CardiacTiers(hard_max_bpm=hard.value, critical_bpm=critical.value))


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


def _min_rider_age(env: Mapping[str, str]) -> Result[int, ConfigProblem]:
    value = _int(_text(env, KEY_MIN_RIDER_AGE, str(DEFAULT_MIN_RIDER_AGE)), KEY_MIN_RIDER_AGE)
    if isinstance(value, Err):
        return value
    if not MIN_SUBJECT_AGE_YEARS <= value.value <= MAX_SUBJECT_AGE_YEARS:
        return Err(
            ConfigProblem(
                KEY_MIN_RIDER_AGE,
                f"{value.value} ans hors de {MIN_SUBJECT_AGE_YEARS}..{MAX_SUBJECT_AGE_YEARS}",
            )
        )
    return Ok(value.value)


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
    version = _record_identifier(env, KEY_SOFTWARE_VERSION, RECORD_VERSION, UNVERSIONED)
    if (
        isinstance(retention, Err)
        or isinstance(machine, Err)
        or isinstance(organization, Err)
        or isinstance(version, Err)
    ):
        return Err(
            tuple(
                result.error
                for result in (retention, machine, organization, version)
                if isinstance(result, Err)
            )
        )
    return Ok(
        RecordConfig(
            root=Path(_text(env, KEY_RECORD_ROOT, str(DEFAULT_RECORD_ROOT))),
            retention_days=retention.value,
            machine_id=machine.value,
            organization_id=organization.value,
            software_version=version.value,
        )
    )


def _web(env: Mapping[str, str]) -> Result[WebConfig, ConfigProblem]:
    port = _int(_text(env, KEY_UI_PORT, str(DEFAULT_PORT)), KEY_UI_PORT)
    if isinstance(port, Err):
        return port
    if port.value == BENCH_CONSOLE_PORT:
        return Err(
            ConfigProblem(KEY_UI_PORT, f"{port.value} est le port de scripts/bench_console.py")
        )
    token = _text(env, KEY_UI_TOKEN, "") or None
    try:
        return Ok(
            WebConfig(host=_text(env, KEY_UI_HOST, DEFAULT_HOST), port=port.value, token=token)
        )
    except ValueError as error:
        return Err(ConfigProblem(f"{KEY_UI_HOST}/{KEY_UI_PORT}/{KEY_UI_TOKEN}", str(error)))


def load_local_config(env: Mapping[str, str]) -> Result[LocalConfig, tuple[ConfigProblem, ...]]:
    """Every key validated; every problem reported, not only the first."""
    backend = _backend(env)
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
    tiers = _tiers(env)
    cloud = _cloud(env)
    sensors = _sensors(env)
    presence = _presence(env)
    min_age = _min_rider_age(env)
    leg_tip = _leg_tip(env, radius.value if isinstance(radius, Ok) else None)
    record = _record(env)

    problems = tuple(
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
            web,
            programs,
            tiers,
            cloud,
            sensors,
            presence,
            min_age,
            leg_tip,
        )
        if isinstance(result, Err)
    ) + (record.error if isinstance(record, Err) else ())
    if (
        problems
        or isinstance(record, Err)
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
        or isinstance(tiers, Err)
        or isinstance(cloud, Err)
        or isinstance(sensors, Err)
        or isinstance(presence, Err)
        or isinstance(min_age, Err)
        or isinstance(leg_tip, Err)
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
            occupied_enabled=occupied.value,
            web=web.value,
            motion_limits_path=Path(
                _text(env, KEY_MOTION_LIMITS_PATH, str(DEFAULT_MOTION_LIMITS_PATH))
            ),
            programs_enabled=programs.value,
            tiers=tiers.value,
            cloud=cloud.value,
            sensors=sensors.value,
            camera=presence.value,
            min_rider_age=min_age.value,
            leg_tip_radius=leg_tip.value,
            record=record.value,
        )
    )
