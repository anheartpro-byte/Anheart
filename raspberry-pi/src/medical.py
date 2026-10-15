"""The medical parameters: one versioned file, signed, checked at every start of the console.

Every number a medical decision sets is in ``config/medical_parameters.json``:
the two cardiac tiers of the safety supervisor, the youngest rider a
programme accepts, the resultant-g ceiling with a person on board, the
fraction of a subject's maximum heart rate a zone may reach and the formula
that estimates that maximum, the four anti-nausea motion limits, and the
shortest recovery and cool-down a profile may declare. The modules that use
one read it from here (:data:`SHIPPED_PARAMETERS`); none of them holds a
copy. The file names the formula (``tanaka``); :mod:`src.training.plan`
writes it out.

Signed, and proven so
---------------------
Next to it, ``config/medical_parameters.sig`` is a detached Ed25519 signature
of its exact bytes, and ``config/medical.pub`` the public key it is checked
against. That key is the medical one and no other: the programme catalogue
will be signed with a key of its own, and a key that signs one never signs
the other. Its private half is never on a machine; the signer runs
``scripts/sign_medical_parameters.py`` on their own computer.

The check is made when the console starts (:func:`shipped_status`, through
:func:`src.local_config.load_local_config`). A signature that is missing or
does not verify, or a public key that is missing or unreadable, does not stop
the console: it starts in **bench mode only** (:class:`MedicalStatus`), where
programmed sessions and a person on board are refused, the page shows a
banner that does not go away, every session record carries a warning, and a
launch from the dashboard is refused. A file that cannot be read or parsed at
all is another matter: there is then no value to run even a bench session
with, since every manual ramp walks the motion limits, and the console does
not start, as it does not start without its shipped profile defaults.

Read once
---------
The file is read when this module is first imported, and its bytes are kept
(:attr:`MedicalDocument.content`). The values every module uses and the bytes
the signature is checked against are therefore the same bytes, whatever
happens to the file on disk afterwards.

Parse, don't validate (contract rule 6): this module is the one layer that
turns the file into domain types. It checks what it can check alone: the
shape, the types, positive and finite numbers, a fraction at most 1, a
critical tier above the hard one. The limits the rest of the console imposes
on every value it is given (the plausible heart rates, the ages the formula
holds for, the motion limiter's own bounds) are checked by
:func:`src.local_config.load_local_config`, which reports them like any other
configuration problem.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum, unique
from pathlib import Path
from typing import Final, NewType, NoReturn, cast

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from src.result import Err, Ok, Result
from src.units import Bpm, GLoadPerSecond, MotorRpm, OutputRpmPerSecond, ResultantG, Seconds

MedicalVersion = NewType("MedicalVersion", str)
"""A version of the medical parameters, ``MAJOR.MINOR.PATCH`` (semver, nothing after)."""

CONFIG_DIRECTORY: Final[Path] = Path(__file__).resolve().parent.parent / "config"
"""``raspberry-pi/config`` in a checkout, ``/app/config`` in the image."""

PARAMETERS_PATH: Final[Path] = CONFIG_DIRECTORY / "medical_parameters.json"
SIGNATURE_PATH: Final[Path] = CONFIG_DIRECTORY / "medical_parameters.sig"
PUBLIC_KEY_PATH: Final[Path] = CONFIG_DIRECTORY / "medical.pub"

UNSIGNED_SENTENCE: Final[str] = "parametres medicaux non signes"
"""The words every refusal, banner, record and log line of the bench mode starts with."""

BENCH_ONLY: Final[str] = (
    "console en mode banc seulement, aucune seance programmee ni personne a bord"
)

UNSIGNED_RECORD_PREFIX: Final[str] = "unsigned-sha256:"
"""What a record's manifest says instead of a version when nothing vouches for the file."""

_VERSION: Final[re.Pattern[str]] = re.compile(
    r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})"
)
_SIGNER: Final[re.Pattern[str]] = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
"""An identifier of the signer, never a free name: ``cle-de-test``, ``med-2026-01``."""

_DATE: Final[re.Pattern[str]] = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_TOP: Final[tuple[str, ...]] = ("version", "issued_at", "signed_by", "parameters")
_COMMENT: Final[str] = "_comment"
"""The one optional member, at the top only: words for the reader, signed like the rest."""

_PARAMETERS: Final[tuple[str, ...]] = (
    "hr_hard_max_bpm",
    "hr_critical_bpm",
    "min_rider_age",
    "occupied_ceiling_g",
    "zone_ceiling_fraction",
    "hr_max_formula",
    "motion_limits",
    "recovery_min_s",
    "cooldown_min_s",
)
_MOTION: Final[tuple[str, ...]] = (
    "output_rpm_per_s",
    "g_per_s",
    "min_run_motor_rpm",
    "max_interval_s",
)

_MISSING: Final[object] = object()
"""Sentinel for a member that is not there: said once, by :func:`_members`."""


@unique
class HrMaxFormula(Enum):
    """How a maximum heart rate is estimated from an age. :mod:`src.training.plan` writes it out."""

    TANAKA = "tanaka"
    """``208 - 0.7 x age`` (Tanaka et al., 2001)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionLimitValues:
    """The four anti-nausea numbers, as signed. :mod:`src.training.motion` builds on them."""

    output_rpm_per_s: OutputRpmPerSecond
    """Angular acceleration of the arm, output rpm per second."""

    g_per_s: GLoadPerSecond
    """Rate of change of the centripetal load, g per second."""

    min_run_motor_rpm: MotorRpm
    """The slowest non-zero setpoint, motor rpm."""

    max_interval_s: Seconds
    """The longest interval one step of the setpoint may be paid for with."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MedicalParameters:
    """Every value a medical decision sets, typed. Frozen: one per process."""

    hr_hard_max_bpm: Bpm
    """Above this the safety supervisor reduces speed. Every programme carries the same."""

    hr_critical_bpm: Bpm
    """Above this the session ends. Strictly above :attr:`hr_hard_max_bpm`."""

    min_rider_age: int
    """Youngest rider, in years, a programmed session accepts."""

    occupied_ceiling_g: ResultantG
    """Resultant-g ceiling with a person on board (first trials)."""

    zone_ceiling_fraction: float
    """The top of a target zone may not exceed this fraction of the subject's maximum."""

    hr_max_formula: HrMaxFormula
    """How that maximum is estimated when only an age is known."""

    motion_limits: MotionLimitValues
    recovery_min_s: Seconds
    """Shortest recovery a profile may declare: the motor stopped, the person still inside."""

    cooldown_min_s: Seconds
    """Shortest cool-down a profile may declare. The drive's own ramp is a floor as well."""


@dataclass(frozen=True, slots=True, kw_only=True)
class MedicalDocument:
    """The file, parsed, with the exact bytes it was parsed from."""

    version: MedicalVersion
    issued_at: str
    """``AAAA-MM-JJ``: the day the decision was signed."""

    signed_by: str
    """An identifier of the signer (see :data:`_SIGNER`), never a free name."""

    parameters: MedicalParameters
    sha256: str
    """Hexadecimal SHA-256 of :attr:`content`."""

    content: bytes = field(repr=False)
    """The bytes the values were read from, and the bytes the signature covers."""


@unique
class SignatureProblem(Enum):
    """Why the file is not proven signed. Each one leaves the console in bench mode."""

    SIGNATURE_MISSING = "signature_missing"
    """``medical_parameters.sig`` absent or unreadable."""

    SIGNATURE_INVALID = "signature_invalid"
    """Read, and it does not verify: the file, the signature or the key was changed."""

    PUBLIC_KEY_MISSING = "public_key_missing"
    """``medical.pub`` absent or unreadable."""

    PUBLIC_KEY_INVALID = "public_key_invalid"
    """Read, and it is not an Ed25519 public key in PEM."""


@dataclass(frozen=True, slots=True)
class Unsigned:
    """Why nothing vouches for the file, in a code and in words for the operator."""

    problem: SignatureProblem
    detail: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MedicalStatus:
    """The medical parameters this console runs with, and whether their signature holds.

    ``unsigned`` is ``None`` exactly when the signature was verified against
    the embedded key. Anything else is the bench mode: see the module
    docstring and :class:`src.local_config.LocalConfig`.
    """

    document: MedicalDocument
    unsigned: Unsigned | None

    @property
    def signed(self) -> bool:
        """Whether the signature of the file was verified."""
        return self.unsigned is None

    @property
    def parameters(self) -> MedicalParameters:
        """The values, signed or not: in bench mode they only govern bench sessions."""
        return self.document.parameters

    @property
    def version(self) -> MedicalVersion:
        """The version the file declares. Vouched for only when :attr:`signed`."""
        return self.document.version

    @property
    def announced_version(self) -> MedicalVersion | None:
        """What the console tells the dashboard: the version when signed, ``None`` otherwise.

        An unsigned file's version is whatever it says, and says nothing of
        what a medical decision signed: the dashboard is told there is none.
        """
        if self.unsigned is None:
            return self.document.version
        return None

    @property
    def recorded_version(self) -> str:
        """What a session record's manifest says: the version, or the file's digest when unsigned.

        The digest is what identifies, afterwards, the values that applied: an
        unsigned file may declare any version.
        """
        if self.unsigned is None:
            return self.document.version
        return f"{UNSIGNED_RECORD_PREFIX}{self.document.sha256}"

    @property
    def warning(self) -> str | None:
        """The one sentence of the bench mode, or ``None`` when the file is signed.

        Shown on the page, written in every session record, logged at startup,
        and given as the reason of every programmed start refused here or
        from the dashboard.
        """
        if self.unsigned is None:
            return None
        return f"{UNSIGNED_SENTENCE} ({self.unsigned.detail}) : {BENCH_ONLY}"


class MedicalParametersUnusableError(ValueError):
    """The shipped file cannot be read or parsed: the installation is broken.

    Raised once, when this module is imported, because every module that uses
    a medical value needs one to exist. A file that parses but is not signed
    does not raise: it is the bench mode.
    """

    def __init__(self, problems: tuple[str, ...]) -> None:
        self.problems: tuple[str, ...] = problems
        super().__init__("parametres medicaux inutilisables : " + " ; ".join(problems))


# =========================================================================
# Parsing
# =========================================================================


def _kind(value: object) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _members(
    value: object,
    where: str,
    keys: tuple[str, ...],
    problems: list[str],
    *,
    optional: tuple[str, ...] = (),
) -> Mapping[str, object]:
    """The members of a JSON object; every missing or unknown key is said once, here.

    An unknown key is refused rather than ignored: a member that nothing reads
    would sit in a signed file looking like a decision. An object that is not
    there at all was already said absent by its parent, and is not said again.
    """
    if value is _MISSING:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} : objet JSON attendu, trouve {_kind(value)}")
        return {}
    # json.loads builds str keys; the values are still unknown, and narrowed by the readers.
    members = cast("dict[str, object]", value)
    problems.extend(f"{where}.{key} : absent" for key in keys if key not in members)
    problems.extend(
        f"{where}.{key} : membre inconnu"
        for key in members
        if key not in keys and key not in optional
    )
    return members


def _whole(members: Mapping[str, object], key: str, where: str, problems: list[str]) -> int:
    """A strictly positive JSON integer; ``0`` with a problem said otherwise.

    A boolean is refused before the integer test (``True`` is an ``int``), and
    a float is refused rather than truncated: 148.7 bpm is somebody's
    arithmetic, and reading it as 148 lowers a limit by rounding.
    """
    value = members.get(key, _MISSING)
    if value is _MISSING:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        problems.append(f"{where}.{key} : entier strictement positif attendu, trouve {value!r}")
        return 0
    return value


def _positive(members: Mapping[str, object], key: str, where: str, problems: list[str]) -> float:
    """A finite, strictly positive JSON number; ``0.0`` with a problem said otherwise."""
    value = members.get(key, _MISSING)
    if value is _MISSING:
        return 0.0
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        problems.append(
            f"{where}.{key} : nombre fini strictement positif attendu, trouve {value!r}"
        )
        return 0.0
    return float(value)


def _text(
    members: Mapping[str, object],
    key: str,
    where: str,
    problems: list[str],
    *,
    pattern: re.Pattern[str],
    expected: str,
) -> str:
    """A string that matches ``pattern`` whole; ``""`` with a problem said otherwise."""
    value = members.get(key, _MISSING)
    if value is _MISSING:
        return ""
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        problems.append(f"{where}.{key} : {expected} attendu, trouve {value!r}")
        return ""
    return value


def _issued_at(members: Mapping[str, object], where: str, problems: list[str]) -> str:
    """``AAAA-MM-JJ``, and a day that exists."""
    text = _text(members, "issued_at", where, problems, pattern=_DATE, expected="date AAAA-MM-JJ")
    if not text:
        return ""
    try:
        date.fromisoformat(text)
    except ValueError:
        problems.append(f"{where}.issued_at : {text!r} n'est pas une date du calendrier")
        return ""
    return text


def _formula(members: Mapping[str, object], where: str, problems: list[str]) -> HrMaxFormula:
    value = members.get("hr_max_formula", _MISSING)
    for formula in HrMaxFormula:
        if value == formula.value:
            return formula
    if value is not _MISSING:
        known = ", ".join(repr(formula.value) for formula in HrMaxFormula)
        problems.append(f"{where}.hr_max_formula : {known} attendu, trouve {value!r}")
    return HrMaxFormula.TANAKA


def _motion(value: object, where: str, problems: list[str]) -> MotionLimitValues:
    members = _members(value, where, _MOTION, problems)
    return MotionLimitValues(
        output_rpm_per_s=OutputRpmPerSecond(
            _positive(members, "output_rpm_per_s", where, problems)
        ),
        g_per_s=GLoadPerSecond(_positive(members, "g_per_s", where, problems)),
        min_run_motor_rpm=MotorRpm(_whole(members, "min_run_motor_rpm", where, problems)),
        max_interval_s=Seconds(_positive(members, "max_interval_s", where, problems)),
    )


def _parameters(value: object, where: str, problems: list[str]) -> MedicalParameters:
    members = _members(value, where, _PARAMETERS, problems)
    hard = _whole(members, "hr_hard_max_bpm", where, problems)
    critical = _whole(members, "hr_critical_bpm", where, problems)
    if hard and critical and critical <= hard:
        problems.append(
            f"{where}.hr_critical_bpm : {critical} bpm doit etre au-dessus de "
            f"hr_hard_max_bpm ({hard} bpm)"
        )
    fraction = _positive(members, "zone_ceiling_fraction", where, problems)
    if fraction > 1.0:
        problems.append(f"{where}.zone_ceiling_fraction : {fraction} au-dessus de 1")
    return MedicalParameters(
        hr_hard_max_bpm=Bpm(hard),
        hr_critical_bpm=Bpm(critical),
        min_rider_age=_whole(members, "min_rider_age", where, problems),
        occupied_ceiling_g=ResultantG(_positive(members, "occupied_ceiling_g", where, problems)),
        zone_ceiling_fraction=fraction,
        hr_max_formula=_formula(members, where, problems),
        motion_limits=_motion(
            members.get("motion_limits", _MISSING), f"{where}.motion_limits", problems
        ),
        recovery_min_s=Seconds(_positive(members, "recovery_min_s", where, problems)),
        cooldown_min_s=Seconds(_positive(members, "cooldown_min_s", where, problems)),
    )


def _unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """A JSON object whose keys are all different.

    ``json`` keeps the last of two equal keys, and another reader might keep
    the first: a signed file must not say two things depending on who reads it.
    """
    members: dict[str, object] = {}
    for key, value in pairs:
        if key in members:
            raise ValueError(f"membre en double : {key!r}")
        members[key] = value
    return members


def _no_constant(name: str) -> NoReturn:
    """``NaN`` and ``Infinity`` are not JSON, whatever ``json`` accepts by default."""
    raise ValueError(f"{name} n'est pas un nombre JSON")


def parse_document(
    content: bytes, *, where: str = PARAMETERS_PATH.name
) -> Result[MedicalDocument, tuple[str, ...]]:
    """The file's bytes as a :class:`MedicalDocument`, or every problem found in them.

    Every problem, not only the first, as for the rest of the configuration.
    ``where`` names the file in the messages.
    """
    try:
        decoded: object = json.loads(  # pyright: ignore[reportAny]  # narrowed below
            content.decode("utf-8"),
            object_pairs_hook=_unique_members,
            parse_constant=_no_constant,
        )
    except ValueError as error:  # also UnicodeDecodeError and json.JSONDecodeError
        return Err((f"{where} : JSON UTF-8 illisible ({error})",))
    problems: list[str] = []
    members = _members(decoded, where, _TOP, problems, optional=(_COMMENT,))
    comment = members.get(_COMMENT, "")
    if not isinstance(comment, str):
        problems.append(f"{where}.{_COMMENT} : texte attendu, trouve {_kind(comment)}")
    version = _text(
        members, "version", where, problems, pattern=_VERSION, expected="MAJEUR.MINEUR.CORRECTIF"
    )
    issued_at = _issued_at(members, where, problems)
    signed_by = _text(
        members, "signed_by", where, problems, pattern=_SIGNER, expected="identifiant du signataire"
    )
    parameters = _parameters(members.get("parameters", _MISSING), f"{where}.parameters", problems)
    if problems:
        return Err(tuple(problems))
    return Ok(
        MedicalDocument(
            version=MedicalVersion(version),
            issued_at=issued_at,
            signed_by=signed_by,
            parameters=parameters,
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
    )


def read_document(path: Path) -> Result[MedicalDocument, tuple[str, ...]]:
    """Read and parse the file at ``path``. Startup only."""
    try:
        content = path.read_bytes()
    except OSError as error:
        return Err((f"{path.name} illisible ({error})",))
    return parse_document(content, where=path.name)


def shipped_document(path: Path = PARAMETERS_PATH) -> MedicalDocument:
    """The file the console ships with. Raises :class:`MedicalParametersUnusableError`."""
    read = read_document(path)
    if isinstance(read, Err):
        raise MedicalParametersUnusableError(read.error)
    return read.value


# =========================================================================
# The signature
# =========================================================================


def verify_signature(content: bytes, signature: bytes, public_key: bytes) -> Result[None, Unsigned]:
    """Whether ``signature`` is the Ed25519 signature of ``content`` by ``public_key`` (PEM).

    The whole of ``content``, byte for byte: one byte changed anywhere, a
    space or a line ending included, and the signature no longer verifies.
    """
    try:
        key = load_pem_public_key(public_key)
    except (ValueError, UnsupportedAlgorithm):
        # The library's words name a web page and the parser's state: the
        # operator is told what the file must be instead.
        return Err(
            Unsigned(
                SignatureProblem.PUBLIC_KEY_INVALID,
                "cle publique illisible : une cle publique Ed25519 en PEM est attendue",
            )
        )
    if not isinstance(key, Ed25519PublicKey):
        return Err(
            Unsigned(
                SignatureProblem.PUBLIC_KEY_INVALID,
                f"cle publique {type(key).__name__}, une cle Ed25519 est attendue",
            )
        )
    try:
        key.verify(signature, content)
    except InvalidSignature:
        return Err(
            Unsigned(
                SignatureProblem.SIGNATURE_INVALID, "la signature ne correspond pas au fichier"
            )
        )
    return Ok(None)


def status_of(
    document: MedicalDocument,
    *,
    signature_path: Path = SIGNATURE_PATH,
    public_key_path: Path = PUBLIC_KEY_PATH,
) -> MedicalStatus:
    """``document`` with the verdict of its signature. Reads the two small files, startup only."""
    try:
        public_key = public_key_path.read_bytes()
    except OSError:
        return MedicalStatus(
            document=document,
            unsigned=Unsigned(
                SignatureProblem.PUBLIC_KEY_MISSING,
                f"cle publique {public_key_path.name} absente ou illisible",
            ),
        )
    try:
        signature = signature_path.read_bytes()
    except OSError:
        return MedicalStatus(
            document=document,
            unsigned=Unsigned(
                SignatureProblem.SIGNATURE_MISSING,
                f"signature {signature_path.name} absente ou illisible",
            ),
        )
    verified = verify_signature(document.content, signature, public_key)
    return MedicalStatus(
        document=document, unsigned=verified.error if isinstance(verified, Err) else None
    )


SHIPPED_DOCUMENT: Final[MedicalDocument] = shipped_document()
"""The shipped file, read once, at import (see the module docstring)."""

SHIPPED_PARAMETERS: Final[MedicalParameters] = SHIPPED_DOCUMENT.parameters
"""Its values: what every module that applies a medical value reads."""


def shipped_status() -> MedicalStatus:
    """The shipped file and the verdict of its signature, as found now. Console start only."""
    return status_of(SHIPPED_DOCUMENT)
