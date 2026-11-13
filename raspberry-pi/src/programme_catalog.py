"""The signed programme catalogue: fetched at rest, verified, revalidated here (ANH-138).

The Anheart team publishes programmes on the dashboard (ANH-137). This module
brings them to the machine, and keeps the machine the authority on them: what
comes down from the cloud is a proposal that this console checks again with
its own rules, never a decision (``docs/README.md``, "Qui décide quoi").

What it does, in this order
---------------------------
1. **At rest only** (:meth:`ProgrammeCatalog.step`): no session, no start
   waiting in the console's mailbox, no launch from the dashboard offered,
   being confirmed or about to be armed. At the console's start, then once per
   :data:`CATALOG_PERIOD` at most. Asked again after the request, before
   anything changes: a session that began meanwhile leaves the programmes as
   they were.
2. **The signature before anything is read** (:func:`signed_text`): the
   answer's ``payload`` is a text, and its exact UTF-8 bytes must carry the
   Ed25519 signature of the Anheart key, checked against the public key
   shipped in this console's image (:data:`PUBLIC_KEY_PATH`). A signature that
   does not verify is said in the console's logbook (``events.jsonl``, a
   ``warning``) and the answer is dropped: the programmes stay as they were.
3. **Every programme, revalidated** (:func:`review`): the medical parameters
   it was validated for are this console's signed ones, then
   :func:`~src.training.plan.parse_profile` (every check of a
   :class:`~src.training.plan.TrainingProfile`, with this console's medical
   limits), then this machine's cardiac tiers, the slowest setpoint of its
   signed motion limits, the motor nameplate and its ceiling with a person on
   board. A programme refused is named with its reason; the others are kept.
4. **The accepted ones replace the store**, in one atomic write
   (:meth:`~src.training.plan.ProfileStore.replace_catalog`, through
   :class:`~src.web.profile_writer.ProfileWriter`, off the loop).
5. **The dashboard is told** with the programmes (:meth:`wire_fields`, read by
   :class:`~src.cloud_sync.CloudSync` for ``POST /api/machine/profiles``):
   the revision accepted, the ids accepted, the ids refused and why.

A session never sees any of this: it runs a frozen copy of its programme
(:class:`~src.training.plan.Program`), and a programme withdrawn from the
catalogue meanwhile goes only at the next update at rest.

What it never does
------------------
Nothing here starts, stops or arms anything. No answer of the dashboard can
make a programme accepted without the signature, nor accepted against this
console's rules. A dashboard that signs nothing (``not_configured``), that
does not know the route (an older one answers 404) or that does not answer
leaves the programmes as they are: the shipped fallback
(``config/profiles.default.json``) on a console that never received a signed
catalogue.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast, final

from src.clock import Clock
from src.cloud_sync import CloudTransport, Document
from src.local_config import CardiacTiers, LocalConfig
from src.medical import CONFIG_DIRECTORY, UNSIGNED_SENTENCE, MedicalVersion, verify_signature
from src.result import Err, Ok, Result
from src.training.plan import (
    NAMEPLATE_MOTOR_RPM,
    CatalogStamp,
    JsonValue,
    ProfileStore,
    StoreContent,
    TrainingProfile,
    parse_profile,
)
from src.training.types import Occupancy
from src.units import Monotonic, MotorRpm, Seconds, elapsed
from src.web.profile_writer import ProfileWriter

_logger: logging.Logger = logging.getLogger(__name__)

CATALOG_PATH: Final[str] = "/api/machine/programmes"
"""The route (``programme_catalog`` of ``contracts/machine-api.json``, contract 1.11)."""

CATALOG_PERIOD: Final[Seconds] = Seconds(60.0)
"""The least time between two requests (``poll_period_s`` of the shared file)."""

PUBLIC_KEY_PATH: Final[Path] = CONFIG_DIRECTORY / "anheart_catalog.pub"
"""The Anheart catalogue's public key, in the image: never the medical one."""

SIGNED: Final[str] = "signed"
NOT_CONFIGURED: Final[str] = "not_configured"

MAX_REPORT_ENTRIES: Final[int] = 200
"""Ids accepted and refused said to the dashboard, at most (``max_report_entries``)."""

MAX_REASON_CHARS: Final[int] = 500
"""Characters of one reason, at most (``max_reason_chars``)."""

MAX_ID_CHARS: Final[int] = 64
"""Characters of one id said to the dashboard, at most: a valid id is never longer."""

DUPLICATE: Final[str] = "identifiant en double dans le catalogue"
"""Why every programme of an id the catalogue holds twice is refused: neither is guessed."""


# =========================================================================
# This machine's rules
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalRules:
    """What a programme of the catalogue is judged against, besides plan.py's own checks."""

    tiers: CardiacTiers
    """The supervisor's tiers (``hr_hard_max_bpm``, ``hr_critical_bpm`` of the signed file)."""

    min_run: MotorRpm
    """The slowest setpoint of the signed motion limits (``min_run_motor_rpm``)."""

    ceiling: Result[MotorRpm, str]
    """The motor-rpm ceiling with a person on board, or why there is none."""

    medical_version: MedicalVersion | None
    """The signed medical version; ``None``: the parameters are not signed (bench mode)."""

    medical_record: str
    """What a record says of them (:attr:`~src.medical.MedicalStatus.recorded_version`)."""


def rules_of(config: LocalConfig) -> LocalRules:
    """This console's rules, from its configuration as loaded at its start."""
    ceiling = config.ceiling_for(Occupancy.OCCUPIED)
    return LocalRules(
        tiers=config.tiers,
        min_run=config.motion.min_run,
        ceiling=Ok(ceiling.value) if isinstance(ceiling, Ok) else Err(ceiling.error.detail),
        medical_version=config.medical.announced_version,
        medical_record=config.medical.recorded_version,
    )


def read_public_key(path: Path = PUBLIC_KEY_PATH) -> bytes | None:
    """The public key's bytes, or ``None`` when it cannot be read. Startup only."""
    try:
        return path.read_bytes()
    except OSError:
        return None


# =========================================================================
# One answer: the signature, then the text, then each programme
# =========================================================================


@dataclass(frozen=True, slots=True)
class RejectedProgramme:
    """A programme of the catalogue this machine refused, and why, in French."""

    profile_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class CatalogPayload:
    """The signed text, read: its revision, and its programmes as documents."""

    revision: int
    unchanged: bool
    programmes: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class CatalogReport:
    """What this machine says of the catalogue with its programmes."""

    revision: int | None
    """The revision whose programmes it holds; ``None``: never one, the shipped fallback."""

    accepted: tuple[str, ...]
    rejected: tuple[RejectedProgramme, ...]


def signed_text(document: Document, public_key: bytes | None) -> Result[str | None, str]:
    """The text of an answer, once its signature verifies; ``None`` when nothing was signed.

    Nothing of the text is read before the signature is checked over its exact
    bytes. An ``Err`` is why the answer is dropped.
    """
    status = document.get("status")
    if status == NOT_CONFIGURED:
        return Ok(None)
    payload = document.get("payload")
    signature = document.get("signature")
    if status != SIGNED or not isinstance(payload, str) or not isinstance(signature, str):
        return Err("reponse du catalogue sans texte signe")
    if public_key is None:
        return Err(f"cle publique {PUBLIC_KEY_PATH.name} absente ou illisible")
    try:
        content = payload.encode("utf-8")
        raw = base64.b64decode(signature, validate=True)
    except (UnicodeEncodeError, binascii.Error):
        return Err("signature du catalogue invalide : texte ou signature illisible")
    verified = verify_signature(content, raw, public_key)
    if isinstance(verified, Err):
        return Err(f"signature du catalogue invalide : {verified.error.detail}")
    return Ok(payload)


def read_payload(text: str) -> Result[CatalogPayload, str]:
    """The signed text as a :class:`CatalogPayload`, or why it is not one.

    The one place its untyped value enters, annotated ``object`` at once.
    """
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return Err("texte du catalogue illisible")
    if not isinstance(parsed, dict):
        return Err("texte du catalogue mal forme")
    document = cast(Mapping[str, object], parsed)
    revision = document.get("catalogRevision")
    unchanged = document.get("unchanged")
    programmes = document.get("programmes")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or not isinstance(unchanged, bool)
        or not isinstance(programmes, list)
    ):
        return Err("texte du catalogue mal forme")
    entries = cast(Sequence[object], programmes)
    documents = tuple(
        cast(Mapping[str, object], entry) for entry in entries if isinstance(entry, dict)
    )
    if len(documents) != len(entries):
        return Err("texte du catalogue mal forme")
    return Ok(CatalogPayload(revision=revision, unchanged=unchanged, programmes=documents))


def review(document: Mapping[str, object], rules: LocalRules) -> Result[TrainingProfile, str]:
    """One programme of the catalogue, judged by this console's rules: kept, or why not.

    In this order: the medical parameters it was validated for, every check of
    :func:`~src.training.plan.parse_profile`, then this machine's limits.
    """
    refused = _medical_refusal(document, rules)
    if refused is not None:
        return Err(refused)
    parsed = parse_profile(document)
    if isinstance(parsed, Err):
        return Err(parsed.error.detail)
    refused = _machine_refusal(parsed.value, rules)
    return Ok(parsed.value) if refused is None else Err(refused)


def _medical_refusal(document: Mapping[str, object], rules: LocalRules) -> str | None:
    """Why the programme was not validated for this console's signed medical parameters."""
    if rules.medical_version is None:
        return f"{UNSIGNED_SENTENCE} : aucun programme du catalogue n'est accepte"
    validated_for = document.get("medical_parameters_version")
    if validated_for != rules.medical_version:
        return (
            f"valide pour les parametres medicaux {validated_for!r}, "
            f"la machine a la version {rules.medical_version}"
        )
    return None


def _machine_refusal(profile: TrainingProfile, rules: LocalRules) -> str | None:
    """Why this machine would not run the profile, whoever rides; ``None`` when it would."""
    tiers = rules.tiers
    if profile.hard_max_bpm != tiers.hard_max_bpm or profile.critical_bpm != tiers.critical_bpm:
        return (
            f"paliers {profile.hard_max_bpm}/{profile.critical_bpm} bpm, ceux de la machine "
            f"sont {tiers.hard_max_bpm}/{tiers.critical_bpm} bpm"
        )
    if profile.min_run_rpm < rules.min_run:
        return (
            f"min_run_rpm {profile.min_run_rpm} sous la plus petite consigne des limites de "
            f"mouvement signees ({rules.min_run} tr/min moteur)"
        )
    if profile.max_rpm > NAMEPLATE_MOTOR_RPM:
        return (
            f"max_rpm {profile.max_rpm} au-dessus de la plaque moteur "
            f"({NAMEPLATE_MOTOR_RPM} tr/min moteur)"
        )
    ceiling = rules.ceiling
    if isinstance(ceiling, Err):
        return ceiling.error
    if profile.max_rpm > ceiling.value:
        return (
            f"max_rpm {profile.max_rpm} tr/min moteur au-dessus du plafond personne a bord "
            f"({ceiling.value} tr/min)"
        )
    return None


def _said_id(document: Mapping[str, object]) -> str:
    """The id a refused programme is named by: its own when it is text, bounded."""
    value = document.get("profile_id")
    named = value if isinstance(value, str) else repr(value)
    return named[:MAX_ID_CHARS]


def _bounded_reason(reason: str) -> str:
    return reason if len(reason) <= MAX_REASON_CHARS else f"{reason[: MAX_REASON_CHARS - 3]}..."


def review_all(
    programmes: Sequence[Mapping[str, object]], rules: LocalRules
) -> tuple[tuple[TrainingProfile, ...], tuple[RejectedProgramme, ...]]:
    """Every programme of the catalogue, kept or refused; an id seen twice is refused twice."""
    reviewed = [(document, review(document, rules)) for document in programmes]
    ids = [r.value.profile_id for _, r in reviewed if isinstance(r, Ok)]
    repeated = {profile_id for profile_id in ids if ids.count(profile_id) > 1}
    accepted: list[TrainingProfile] = []
    rejected: list[RejectedProgramme] = []
    for document, result in reviewed:
        if isinstance(result, Err):
            rejected.append(RejectedProgramme(_said_id(document), _bounded_reason(result.error)))
        elif result.value.profile_id in repeated:
            rejected.append(RejectedProgramme(result.value.profile_id, DUPLICATE))
        else:
            accepted.append(result.value)
    return tuple(accepted), tuple(rejected)


# =========================================================================
# The link
# =========================================================================


@final
class ProgrammeCatalog:
    """The catalogue, asked for from a task of its own. See the module docstring.

    Mutable, owned by the event loop: when it last asked, the revision it
    holds, what it last said of a refused answer, and its report.
    """

    __slots__ = (
        "_asked",
        "_at_rest",
        "_clock",
        "_held",
        "_public_key",
        "_refused",
        "_report",
        "_rules",
        "_say",
        "_store",
        "_transport",
        "_writer",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        transport: CloudTransport,
        store: ProfileStore,
        rules: LocalRules,
        at_rest: Callable[[], bool],
        say: Callable[[str], None],
        public_key: bytes | None,
    ) -> None:
        """``at_rest``: whether the programmes may change now. ``say``: one warning for the
        console's logbook. ``public_key``: the Anheart catalogue key, PEM; ``None``: none
        could be read, and every signature is refused.

        The store is screened here, before anything is asked: profiles accepted under
        other medical parameters than this console's are set aside, in memory, until the
        next catalogue is accepted.
        """
        self._clock: Clock = clock
        self._transport: CloudTransport = transport
        self._store: ProfileStore = store
        self._writer: ProfileWriter = ProfileWriter(store)
        self._rules: LocalRules = rules
        self._at_rest: Callable[[], bool] = at_rest
        self._say: Callable[[str], None] = say
        self._public_key: bytes | None = public_key
        self._asked: Monotonic | None = None
        # Never sent at the start: the first answer is the whole list, revalidated
        # with the rules of this start.
        self._held: int | None = None
        self._refused: tuple[str, object] | None = None
        self._report: CatalogReport = self._screen_stored()

    @property
    def report(self) -> CatalogReport:
        """What this machine says of the catalogue now."""
        return self._report

    def wire_fields(self) -> Mapping[str, JsonValue]:
        """The report, as ``POST /api/machine/profiles`` takes it (contract 1.11), bounded."""
        report = self._report
        accepted = report.accepted[:MAX_REPORT_ENTRIES]
        rejected = report.rejected[: MAX_REPORT_ENTRIES - len(accepted)]
        return {
            "catalogRevision": report.revision,
            "accepted": list(accepted),
            "rejected": [{"profileId": r.profile_id, "reason": r.reason} for r in rejected],
        }

    def _screen_stored(self) -> CatalogReport:
        stamp = self._store.catalog
        if stamp is None:
            return CatalogReport(revision=None, accepted=(), rejected=())
        stored = self._store.list_profiles()
        if stamp.medical == self._rules.medical_record:
            return CatalogReport(
                revision=stamp.revision,
                accepted=tuple(p.profile_id for p in stored),
                rejected=(),
            )
        reason = (
            f"accepte sous les parametres medicaux {stamp.medical}, "
            f"la machine a {self._rules.medical_record}"
        )
        self._store.adopt(StoreContent(rev=self._store.rev, profiles=(), catalog=stamp))
        self._say(f"catalogue {stamp.revision} ecarte jusqu'au prochain : {reason}")
        return CatalogReport(
            revision=stamp.revision,
            accepted=(),
            rejected=tuple(RejectedProgramme(p.profile_id, reason) for p in stored),
        )

    async def step(self) -> None:
        """Ask for the catalogue when it is due and the machine is at rest; never raises."""
        now = self._clock.monotonic()
        if self._asked is not None and elapsed(self._asked, now) < CATALOG_PERIOD:
            return
        if not self._at_rest():
            return
        self._asked = now
        params = None if self._held is None else {"since": str(self._held)}
        fetched = await self._transport.get(CATALOG_PATH, params)
        if isinstance(fetched, Err):
            # An older dashboard answers 404: as no answer, the programmes stay.
            _logger.info("programme catalogue: nothing received (%r)", fetched.error)
            return
        answer = fetched.value
        text = signed_text(answer, self._public_key)
        if isinstance(text, Err):
            self._refuse(text.error, answer.get("signature"))
            return
        if text.value is None:
            _logger.info("programme catalogue: the dashboard signs none")
            return
        payload = read_payload(text.value)
        if isinstance(payload, Err):
            self._refuse(payload.error, answer.get("signature"))
            return
        await self._receive(payload.value)

    async def _receive(self, catalog: CatalogPayload) -> None:
        if catalog.unchanged:
            if catalog.revision != self._held:
                self._refuse(f"catalogue dit inchange a la revision {catalog.revision}", None)
            return
        accepted, rejected = review_all(catalog.programmes, self._rules)
        if not self._at_rest():
            # A session began during the request: asked again at the next rest.
            self._asked = None
            return
        stamp = CatalogStamp(revision=catalog.revision, medical=self._rules.medical_record)
        kept = {p.profile_id: p for p in self._store.list_profiles()}
        if self._store.catalog != stamp or kept != {p.profile_id: p for p in accepted}:
            written = await self._writer.replace_catalog(accepted, stamp)
            if isinstance(written, Err):
                self._say(
                    f"catalogue {catalog.revision} non enregistre, programmes inchanges : "
                    f"{written.error.detail}"
                )
                return
        self._held = catalog.revision
        self._refused = None
        self._report = CatalogReport(
            revision=catalog.revision,
            accepted=tuple(sorted(p.profile_id for p in accepted)),
            rejected=rejected,
        )
        _logger.info(
            "programme catalogue %d: %d accepted, %d refused",
            catalog.revision,
            len(accepted),
            len(rejected),
        )
        for refused in rejected:
            _logger.warning(
                "programme catalogue: %r refused: %r", refused.profile_id, refused.reason
            )

    def _refuse(self, reason: str, signature: object) -> None:
        """Say an answer dropped, once for the same reason and the same signature."""
        key = (reason, signature)
        if key == self._refused:
            return
        self._refused = key
        self._say(f"catalogue refuse, programmes inchanges : {reason}")
