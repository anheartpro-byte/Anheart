"""The machine's configuration as the dashboard proposes it, revalidated here (ANH-141).

The Anheart team configures a machine on the site (radii, ports, sensors,
ceilings, the two locks) without editing a file on the Pi. What comes down is
a proposal: this module reads it with the keys, types and families of
``contracts/machine-config.json`` (:data:`KEYS`, pinned to that file by
``tests/test_machine_config_contract.py``), turns it into the text a ``.env``
would hold, and hands it with this console's own local keys to
:func:`~src.local_config.load_local_config`, which judges it as it judges any
``.env``. Convex validates the same values the same way before anybody may
approve them (``convex/lib/machineConfig.ts``), and
``contracts/machine-config-cases.json`` holds the two verdicts equal, word for
word (``tests/test_machine_config_parity.py``).

What :func:`review` refuses, every problem at once:

* a key this console does not know, a key missing, a value of the wrong type
  (the contract's ``types``): read here, before anything else;
* whatever :func:`~src.local_config.load_local_config` refuses of the values,
  with this console's local keys (``local_keys`` of the contract: the
  dashboard link, the page's host, token and certificate, the safety relay's
  wiring, the record's identity), which the dashboard never sets;
* ``PROGRAMS_ENABLED`` or ``OCCUPANCY_OCCUPIED_ENABLED`` true on a machine the
  registry has not validated to ``auto`` or ``occupied`` (:func:`lock_refusal`),
  the second barrier of the rule Convex already applies at approval;
* either lock true when this console's medical parameters are not signed: the
  configuration would run with it forced off, which is not what was approved.

Nothing here reads a clock, a file or the network, and nothing applies
anything: :func:`review` is a verdict.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, assert_never, cast

from src.geometry import CONFIRMED_GEAR_RATIO
from src.local_config import (
    DEFAULT_MOTOR_MAX_RPM,
    DEFAULT_RECORD_RETENTION_DAYS,
    KEY_ARM_RADIUS_M,
    KEY_BITALINO_ADDRESS,
    KEY_ECG_SOURCE,
    KEY_GEAR_RATIO,
    KEY_LEG_TIP_RADIUS_M,
    KEY_MODBUS_BAUDRATE,
    KEY_MODBUS_PARITY,
    KEY_MODBUS_TIMEOUT_S,
    KEY_MOTOR_BACKEND,
    KEY_MOTOR_MAX_RPM,
    KEY_MOTOR_PORT,
    KEY_MOTOR_REG_OFFSET,
    KEY_MOTOR_SLAVE_ID,
    KEY_OCCUPIED_ENABLED,
    KEY_PRESENCE_SOURCE,
    KEY_PROGRAMS_ENABLED,
    KEY_RECORD_RETENTION_DAYS,
    KEY_SENSORS,
    KEY_UI_PORT,
    CameraSource,
    ConfigProblem,
    EcgSource,
    LocalConfig,
    MotorBackend,
    load_local_config,
)
from src.medical import MedicalStatus
from src.motor.atv320 import DEFAULT_BAUDRATE, DEFAULT_SLAVE_ADDRESS, DEFAULT_TIMEOUT
from src.motor.ftdi_link import SCHNEIDER_CABLE_URL, Parity
from src.result import Err, Ok, Result
from src.sensors.base import SensorKind
from src.web.deps import DEFAULT_PORT

type ConfigValue = str | int | float | bool | tuple[str, ...] | None
"""A value of the configuration, read: ``None`` is a nullable key left unset."""


@unique
class Family(Enum):
    """``families`` of the contract: who approves a change, and how the console applies it."""

    EXPLOITATION = "exploitation"
    """Applied at rest by itself; one approval on the site."""

    SAFETY = "safety"
    """Two Anheart admins on the site, then an operator signed in at this console."""


@unique
class ValueType(Enum):
    """``types`` of the contract."""

    TEXT = "text"
    TOKEN = "token"  # noqa: S105  # a type NAME, not a secret
    ENUM = "enum"
    ENUM_LIST = "enum_list"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


@unique
class ValidationLevel(Enum):
    """How far the machine registry validated the machine, lowest first (ANH-147)."""

    NONE = "none"
    BENCH = "bench"
    AUTO = "auto"
    """Milestone M5: programmed sessions."""

    OCCUPIED = "occupied"
    """Milestone M6: a person on board."""


_LEVEL_ORDER: Final[tuple[ValidationLevel, ...]] = tuple(ValidationLevel)


@dataclass(frozen=True, slots=True, kw_only=True)
class KeySpec:
    """One key of the contract, as this console reads it."""

    family: Family
    kind: ValueType
    env: str | None
    """The ``.env`` key :func:`~src.local_config.load_local_config` reads; ``None`` for
    ``name`` and ``location``, which only the console's screen shows."""

    nullable: bool
    default: ConfigValue
    values: tuple[str, ...] = ()
    max_chars: int = 0
    requires: ValidationLevel | None = None
    """The validation the registry must have reached for this lock to be true."""


_SENSOR_NAMES: Final[tuple[str, ...]] = tuple(kind.value for kind in SensorKind)

KEYS: Final[Mapping[str, KeySpec]] = {
    "name": KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.TEXT,
        env=None,
        nullable=False,
        default=None,
        max_chars=100,
    ),
    "location": KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.TEXT,
        env=None,
        nullable=True,
        default=None,
        max_chars=200,
    ),
    KEY_SENSORS: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.ENUM_LIST,
        env=KEY_SENSORS,
        nullable=False,
        default=(SensorKind.ECG.value,),
        values=_SENSOR_NAMES,
    ),
    KEY_MOTOR_PORT: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.TOKEN,
        env=KEY_MOTOR_PORT,
        nullable=False,
        default=SCHNEIDER_CABLE_URL,
        max_chars=200,
    ),
    KEY_MOTOR_SLAVE_ID: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.INTEGER,
        env=KEY_MOTOR_SLAVE_ID,
        nullable=False,
        default=DEFAULT_SLAVE_ADDRESS,
    ),
    KEY_MODBUS_BAUDRATE: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.INTEGER,
        env=KEY_MODBUS_BAUDRATE,
        nullable=False,
        default=DEFAULT_BAUDRATE,
    ),
    KEY_MODBUS_PARITY: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.ENUM,
        env=KEY_MODBUS_PARITY,
        nullable=False,
        default=Parity.EVEN.value,
        values=tuple(parity.value for parity in Parity),
    ),
    KEY_MODBUS_TIMEOUT_S: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.NUMBER,
        env=KEY_MODBUS_TIMEOUT_S,
        nullable=False,
        default=float(DEFAULT_TIMEOUT),
    ),
    KEY_MOTOR_REG_OFFSET: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.INTEGER,
        env=KEY_MOTOR_REG_OFFSET,
        nullable=False,
        default=0,
    ),
    KEY_ECG_SOURCE: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.ENUM,
        env=KEY_ECG_SOURCE,
        nullable=False,
        default=None,
        values=tuple(source.value for source in EcgSource),
    ),
    KEY_BITALINO_ADDRESS: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.TOKEN,
        env=KEY_BITALINO_ADDRESS,
        nullable=True,
        default=None,
        max_chars=200,
    ),
    KEY_PRESENCE_SOURCE: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.ENUM,
        env=KEY_PRESENCE_SOURCE,
        nullable=False,
        default=CameraSource.NONE.value,
        values=tuple(source.value for source in CameraSource),
    ),
    KEY_UI_PORT: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.INTEGER,
        env=KEY_UI_PORT,
        nullable=False,
        default=DEFAULT_PORT,
    ),
    KEY_GEAR_RATIO: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.NUMBER,
        env=KEY_GEAR_RATIO,
        nullable=False,
        default=float(CONFIRMED_GEAR_RATIO),
    ),
    KEY_RECORD_RETENTION_DAYS: KeySpec(
        family=Family.EXPLOITATION,
        kind=ValueType.INTEGER,
        env=KEY_RECORD_RETENTION_DAYS,
        nullable=False,
        default=DEFAULT_RECORD_RETENTION_DAYS,
    ),
    KEY_MOTOR_BACKEND: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.ENUM,
        env=KEY_MOTOR_BACKEND,
        nullable=False,
        default=None,
        values=tuple(backend.value for backend in MotorBackend),
    ),
    KEY_MOTOR_MAX_RPM: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.INTEGER,
        env=KEY_MOTOR_MAX_RPM,
        nullable=False,
        default=int(DEFAULT_MOTOR_MAX_RPM),
    ),
    KEY_ARM_RADIUS_M: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.NUMBER,
        env=KEY_ARM_RADIUS_M,
        nullable=False,
        default=None,
    ),
    KEY_LEG_TIP_RADIUS_M: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.NUMBER,
        env=KEY_LEG_TIP_RADIUS_M,
        nullable=True,
        default=None,
    ),
    KEY_PROGRAMS_ENABLED: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.BOOLEAN,
        env=KEY_PROGRAMS_ENABLED,
        nullable=False,
        default=False,
        requires=ValidationLevel.AUTO,
    ),
    KEY_OCCUPIED_ENABLED: KeySpec(
        family=Family.SAFETY,
        kind=ValueType.BOOLEAN,
        env=KEY_OCCUPIED_ENABLED,
        nullable=False,
        default=False,
        requires=ValidationLevel.OCCUPIED,
    ),
}
"""The keys the dashboard configures, in the contract's order."""

CLOUD_ENV_KEYS: Final[frozenset[str]] = frozenset(
    spec.env for spec in KEYS.values() if spec.env is not None
)
"""The ``.env`` keys the dashboard sets: never taken from a local ``.env`` with a proposal."""

MAX_SAFE_INTEGER: Final[int] = 2**53 - 1
"""The largest whole number both sides hold exactly (JavaScript's ``Number.MAX_SAFE_INTEGER``)."""

_TOKEN: Final[re.Pattern[str]] = re.compile(r"[!#-&(-\[\]-~]+")
"""Printable ASCII without space, quote, apostrophe or backslash: a port, an address."""

_CONTROL_OR_LONE_SURROGATE: Final[re.Pattern[str]] = re.compile(
    r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]"
)

REQUIRED: Final[str] = "obligatoire"
MISSING: Final[str] = "cle absente"
UNKNOWN: Final[str] = "cle inconnue : la console ne la revalide pas"
"""The three words a key gets before its value is even read."""


TYPE_WORDS: Final[Mapping[ValueType, str]] = {
    ValueType.TEXT: (
        "attendu un texte de 1 a {chars} caracteres, sans caractere de controle ni espace "
        "en tete ou en fin"
    ),
    ValueType.TOKEN: (
        "attendu 1 a {chars} caracteres ASCII imprimables, sans espace, guillemet, "
        "apostrophe ni barre oblique inverse"
    ),
    ValueType.ENUM: "attendu l'une des valeurs : {values}",
    ValueType.ENUM_LIST: "attendu une liste non vide, sans doublon, parmi : {values}",
    ValueType.INTEGER: "attendu un nombre entier",
    ValueType.NUMBER: "attendu un nombre fini",
    ValueType.BOOLEAN: "attendu true ou false",
}
"""What a value of each type must be, as both sides say it (a test checks every type has one)."""


def type_words(spec: KeySpec) -> str:
    """What a value of ``spec`` must be."""
    return TYPE_WORDS[spec.kind].format(chars=spec.max_chars, values=", ".join(spec.values))


def _string(raw: object, spec: KeySpec) -> str | None:
    """A text, a token or a choice of ``spec``; ``None`` when ``raw`` is not one."""
    if not isinstance(raw, str):
        return None
    if spec.kind is ValueType.ENUM:
        return raw if raw in spec.values else None
    fits = 0 < len(raw) <= spec.max_chars
    if spec.kind is ValueType.TOKEN:
        return raw if fits and _TOKEN.fullmatch(raw) else None
    plain = raw == raw.strip() and _CONTROL_OR_LONE_SURROGATE.search(raw) is None
    return raw if fits and plain else None


def _whole(raw: object) -> int | None:
    """A whole number, from an int or from a float JavaScript would write as one."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and math.isfinite(raw) and raw.is_integer():
        raw = int(raw)
    return raw if isinstance(raw, int) and abs(raw) <= MAX_SAFE_INTEGER else None


def _number(raw: object) -> int | float | None:
    """A finite number, ``-0.0`` read as the ``0`` JavaScript writes; ``None`` otherwise,
    an integer beyond what a float holds included (JavaScript reads it as infinite)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    try:
        finite = math.isfinite(raw)
    except OverflowError:
        return None
    return raw + 0 if finite else None


def _names(raw: object, spec: KeySpec) -> tuple[str, ...] | None:
    if not isinstance(raw, list):
        return None
    entries = cast(Sequence[object], raw)
    names = tuple(entry for entry in entries if isinstance(entry, str) and entry in spec.values)
    if not names or len(names) != len(entries) or len(set(names)) != len(names):
        return None
    return names


def read_value(spec: KeySpec, raw: object) -> ConfigValue:
    """``raw`` as a value of ``spec``; ``None`` when it is not one."""
    match spec.kind:
        case ValueType.TEXT | ValueType.TOKEN | ValueType.ENUM:
            return _string(raw, spec)
        case ValueType.ENUM_LIST:
            return _names(raw, spec)
        case ValueType.INTEGER:
            return _whole(raw)
        case ValueType.NUMBER:
            return _number(raw)
        case ValueType.BOOLEAN:
            return raw if isinstance(raw, bool) else None
    raise assert_never(spec.kind)


@dataclass(frozen=True, slots=True)
class ReadValues:
    """The values read, and the problems of those that could not be."""

    values: Mapping[str, ConfigValue]
    """Every key read, unset ones as ``None``; a key refused is absent."""

    problems: tuple[ConfigProblem, ...]


def read_values(document: Mapping[str, object]) -> ReadValues:
    """Every key of :data:`KEYS` read from ``document``, and every problem: missing,
    wrong type, unknown key (those last, in sorted order)."""
    values: dict[str, ConfigValue] = {}
    problems: list[ConfigProblem] = []
    for key, spec in KEYS.items():
        if key not in document:
            problems.append(ConfigProblem(key, MISSING))
            continue
        raw = document[key]
        if raw is None:
            if spec.nullable:
                values[key] = None
            else:
                problems.append(ConfigProblem(key, REQUIRED))
            continue
        value = read_value(spec, raw)
        if value is None:
            problems.append(ConfigProblem(key, type_words(spec)))
        else:
            values[key] = value
    problems.extend(ConfigProblem(key, UNKNOWN) for key in sorted(set(document) - set(KEYS)))
    return ReadValues(values=values, problems=tuple(problems))


def env_text(value: ConfigValue) -> str | None:
    """What a ``.env`` would say for ``value``; ``None``: the key is left out."""
    match value:
        case None:
            return None
        case bool():
            return "true" if value else "false"
        case str():
            return value
        case int() | float():
            return str(value)
        case tuple():
            return ",".join(value)
    raise assert_never(value)


def environment(values: Mapping[str, ConfigValue], local: Mapping[str, str]) -> dict[str, str]:
    """The ``.env`` :func:`~src.local_config.load_local_config` judges: ``local`` without the
    keys the dashboard sets, then ``values``; an unset value is left out, as a blank line."""
    merged = {key: text for key, text in local.items() if key not in CLOUD_ENV_KEYS}
    for key, value in values.items():
        env = KEYS[key].env
        text = env_text(value)
        if env is not None and text is not None:
            merged[env] = text
    return merged


def lock_refusal(level: ValidationLevel, value: bool, required: ValidationLevel) -> str | None:
    """Why a lock cannot be ``value`` on a machine validated to ``level``; ``None`` when it can.

    Turning a lock off is always allowed. The words are Convex's
    (``configurationLockVerdict``, ``convex/lib/machineRegistry.ts``).
    """
    if not value or _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(required):
        return None
    return f"machine validée jusqu'au niveau {level.value}"


def _locks(values: Mapping[str, ConfigValue], level: ValidationLevel) -> list[ConfigProblem]:
    problems: list[ConfigProblem] = []
    for key, spec in KEYS.items():
        value = values.get(key)
        if spec.requires is None or not isinstance(value, bool):
            continue
        refusal = lock_refusal(level, value, spec.requires)
        if refusal is not None:
            problems.append(ConfigProblem(key, refusal))
    return problems


def locks_above(values: Mapping[str, ConfigValue], level: ValidationLevel) -> tuple[str, ...]:
    """The locks ``values`` sets true that a machine validated to ``level`` may not run."""
    return tuple(problem.key for problem in _locks(values, level))


def _honoured(values: Mapping[str, ConfigValue], config: LocalConfig) -> list[ConfigProblem]:
    """A lock asked true that the console would run false: unsigned medical parameters."""
    asked = (
        (KEY_PROGRAMS_ENABLED, config.programs_enabled),
        (KEY_OCCUPIED_ENABLED, config.occupied_enabled),
    )
    return [
        ConfigProblem(key, f"refusee a true par la console : {config.medical.warning}")
        for key, kept in asked
        if values.get(key) is True and not kept
    ]


@dataclass(frozen=True, slots=True)
class Reviewed:
    """A proposal this console accepts: the values, and the configuration they make here."""

    values: Mapping[str, ConfigValue]
    config: LocalConfig


def review(
    document: Mapping[str, object],
    local: Mapping[str, str],
    level: ValidationLevel,
    medical: MedicalStatus | None = None,
) -> Result[Reviewed, tuple[ConfigProblem, ...]]:
    """The proposal ``document`` judged with this console's ``local`` keys; every problem.

    ``level``: how far the registry validated the machine. ``medical``: as
    :func:`~src.local_config.load_local_config` takes it. A key refused for its
    type is left unset for the rest of the judgement, and what the console
    would say of it unset is not said again.
    """
    read = read_values(document)
    refused = {problem.key for problem in read.problems}
    loaded = load_local_config(environment(read.values, local), medical)
    judged = (
        tuple(problem for problem in loaded.error if problem.key not in refused)
        if isinstance(loaded, Err)
        else ()
    )
    honoured = _honoured(read.values, loaded.value) if isinstance(loaded, Ok) else []
    problems = (*read.problems, *judged, *_locks(read.values, level), *honoured)
    if problems or isinstance(loaded, Err):
        return Err(problems)
    return Ok(Reviewed(values=read.values, config=loaded.value))


# =========================================================================
# What the console runs with now, and what a proposal changes of it
# =========================================================================


def effective_values(config: LocalConfig) -> dict[str, ConfigValue]:
    """The values ``config`` runs with, as the dashboard writes them: the console's own
    ``.env`` read through :func:`~src.local_config.load_local_config`, defaults included.

    ``name`` and ``location`` are not the ``.env``'s: ``None``. The drive link's keys
    are ``None`` with the simulated drive, which does not read them.
    """
    link = config.drive_link
    settings = None if link is None else link.settings
    leg = config.leg_tip_radius
    return {
        "name": None,
        "location": None,
        KEY_SENSORS: tuple(kind.value for kind in config.sensors),
        KEY_MOTOR_PORT: None if settings is None else settings.port,
        KEY_MOTOR_SLAVE_ID: None if settings is None else settings.slave_address,
        KEY_MODBUS_BAUDRATE: None if settings is None else settings.baudrate,
        KEY_MODBUS_PARITY: None if settings is None else settings.parity.value,
        KEY_MODBUS_TIMEOUT_S: None if settings is None else float(settings.timeout),
        KEY_MOTOR_REG_OFFSET: None if link is None else link.registers.offset,
        KEY_ECG_SOURCE: config.ecg_source.value,
        KEY_BITALINO_ADDRESS: config.bitalino_address,
        KEY_PRESENCE_SOURCE: config.camera.value,
        KEY_UI_PORT: config.web.port,
        KEY_GEAR_RATIO: float(config.geometry.ratio),
        KEY_RECORD_RETENTION_DAYS: config.record.retention_days,
        KEY_MOTOR_BACKEND: config.motor_backend.value,
        KEY_MOTOR_MAX_RPM: int(config.motor_max_rpm),
        KEY_ARM_RADIUS_M: float(config.geometry.radius),
        KEY_LEG_TIP_RADIUS_M: None if leg is None else float(leg),
        KEY_PROGRAMS_ENABLED: config.programs_enabled,
        KEY_OCCUPIED_ENABLED: config.occupied_enabled,
    }


@dataclass(frozen=True, slots=True)
class ConfigChange:
    """One key a proposal changes: what the console runs with, and what it would."""

    key: str
    family: Family
    before: ConfigValue
    after: ConfigValue


def changes_of(
    current: Mapping[str, ConfigValue], proposed: Mapping[str, ConfigValue]
) -> tuple[ConfigChange, ...]:
    """Every key whose value ``proposed`` changes from ``current``, in the contract's order.

    Numbers compare by value (``50`` is ``50.0``); a key ``current`` does not hold
    changes.
    """
    return tuple(
        ConfigChange(key, spec.family, current.get(key), proposed.get(key))
        for key, spec in KEYS.items()
        if key not in current or current[key] != proposed.get(key)
    )


# =========================================================================
# The signed text the dashboard sends (``machine_config``, contract 1.14)
# =========================================================================

CONFIG_KIND: Final[str] = "machine_config"
"""What a signed configuration says it is: never a catalogue nor an operator list."""

_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,128}")
_LEVELS: Final[Mapping[str, ValidationLevel]] = {level.value: level for level in ValidationLevel}


@dataclass(frozen=True, slots=True)
class Revision:
    """A revision approved on the dashboard, its values as sent and as read."""

    number: int
    digest: str
    """SHA-256 of ``text``, checked here: the same in the heartbeat and in the ack."""

    text: str
    """The canonical JSON of the values, as Convex hashed it; never written again here."""

    document: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ConfigOffer:
    """One answer of ``GET /api/machine/config``, its signature already verified."""

    machine_id: str
    issued_at: int
    level: ValidationLevel
    revision: Revision | None
    """``None``: nothing is approved for this machine; the console keeps what it has."""


def _object(text: str) -> Mapping[str, object] | None:
    """The JSON object ``text`` holds; ``None`` when it holds none."""
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return None
    return cast(Mapping[str, object], parsed) if isinstance(parsed, dict) else None


def _whole_at_least(value: object, low: int) -> int | None:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= low else None
    )


def _revision(fields: Mapping[str, object]) -> Result[Revision | None, str]:
    raw, digest, text = fields.get("revision"), fields.get("hash"), fields.get("values")
    if raw is None and digest is None and text is None:
        return Ok(None)
    number = _whole_at_least(raw, 1)
    if (
        number is None
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or not isinstance(text, str)
    ):
        return Err("configuration mal formee")
    try:
        content = text.encode("utf-8")
    except UnicodeEncodeError:
        return Err("configuration mal formee")
    if hashlib.sha256(content).hexdigest() != digest:
        return Err(f"revision {number} : l'empreinte n'est pas celle de ses valeurs")
    values = _object(text)
    if values is None:
        return Err(f"revision {number} : valeurs illisibles")
    return Ok(Revision(number, digest, text, values))


def read_offer(text: str, machine_id: str | None) -> Result[ConfigOffer, str]:
    """The signed text as a :class:`ConfigOffer`, or why it is not one.

    The one place its untyped value enters. ``machine_id``: this machine
    (``RECORD_MACHINE_ID``), whose configuration alone is taken; ``None`` when it
    is not set.
    """
    fields = _object(text)
    if fields is None:
        return Err("texte de la configuration illisible")
    if fields.get("kind") != CONFIG_KIND:
        return Err("texte signe qui n'est pas une configuration")
    listed_for = fields.get("machineId")
    issued_at = _whole_at_least(fields.get("issuedAt"), 0)
    level = _LEVELS.get(str(fields.get("validationLevel")))
    if (
        not isinstance(listed_for, str)
        or _IDENTIFIER.fullmatch(listed_for) is None
        or issued_at is None
        or level is None
    ):
        return Err("configuration mal formee")
    if machine_id is not None and machine_id != listed_for:
        return Err(
            f"configuration d'une autre machine ({listed_for!r}, cette console est {machine_id!r})"
        )
    revision = _revision(fields)
    if isinstance(revision, Err):
        return revision
    return Ok(ConfigOffer(listed_for, issued_at, level, revision.value))
