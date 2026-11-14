"""The machine's configuration from the dashboard, applied at rest; safety by an operator (ANH-141).

The Anheart team configures a machine on the site; Convex approves a revision
(two Anheart admins when a safety key changes) and sends it signed
(``GET /api/machine/config``, ``machine_config`` of
``contracts/machine-api.json``). This module brings it to the machine and keeps
the console the authority on it: what comes down is a proposal.

What it does, in this order
---------------------------
1. **At rest only** (:meth:`ConfigSync.step`): the catalogue's rest (no session,
   no start waiting, no launch from the dashboard in hand). At the console's
   start, then once per :data:`CONFIG_PERIOD` at most.
2. **The signature before anything is read**, with the catalogue's public key,
   then the text (:func:`~src.machine_config.read_offer`): a configuration, of
   this machine, not older than the last one taken, whose ``hash`` is that of
   its values, and not a revision older than the one this console applied.
3. **Revalidated by this console** (:func:`~src.machine_config.review`): every
   key through :func:`~src.local_config.load_local_config` with this console's
   local keys, and the two locks against the level the registry signed with
   it. A revision refused leaves the configuration in force as it is, and is
   said back to the dashboard with why.
4. **Applied by what it changes** (:func:`~src.machine_config.changes_of`, from
   what the console runs with): a revision that changes no safety key is
   applied at once; one that changes a safety key waits, shown on the console
   with what changes, until an operator signed in at it confirms
   (:meth:`ConfigSync.confirm`: « Appliquer la configuration de sûreté »).
5. **Applying** is writing the answer as it came, signature included, to
   ``data/config.applied.json`` in one atomic replace, then asking the console
   for its controlled restart at rest (``EXIT_RESTART`` of
   :mod:`src.local_panel`): every task starts again on the new values, through
   the same startup checks as a ``.env``. The process that runs a revision says
   so to the dashboard (``POST /api/machine/config/applied {revision, hash}``)
   once it is up, and announces its hash in every heartbeat.

What it never does
------------------
Nothing here starts, stops or arms a session, and nothing changes during one:
the restart waits for the next rest. No answer can make a revision applied
without the signature, against this console's rules, or, for a safety key,
without a person at the machine. A dashboard that signs nothing, does not know
the route or does not answer leaves the configuration as it is.

Every answer carries the registry's level, whatever revision it is for: when
it no longer allows a lock of the revision applied, the answer is kept next to
the file (:data:`LEVEL_FILE`) and the console restarts at rest on that level,
the lock run false. A level raised again gives it back only as a safety change
an operator confirms. A console whose machine is not named
(``RECORD_MACHINE_ID``) takes no configuration at all (:data:`MACHINE_UNASSIGNED`).
The files are verified again at every start (:func:`load_applied`): the
console's account may write its data, it cannot sign for Anheart.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Final, final

import anyio

from src.clock import Clock
from src.cloud_sync import TOO_LARGE_ANSWER, CloudTransport, Unreachable
from src.config_view import ChangeView, ConfigView, PendingView
from src.console_access import STORED_LIST, StoredList, replace_file, signed_text
from src.local_config import (
    KEY_OCCUPIED_ENABLED,
    KEY_PROGRAMS_ENABLED,
    ConfigProblem,
    LocalConfig,
    MachineConfigRef,
)
from src.machine_config import (
    CLOUD_ENV_KEYS,
    ConfigChange,
    ConfigOffer,
    ConfigValue,
    Family,
    Revision,
    ValidationLevel,
    changes_of,
    env_text,
    environment,
    locks_above,
    read_offer,
    read_values,
    review,
)
from src.medical import MedicalStatus
from src.record.writer import describe_os_error
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, elapsed

_logger: logging.Logger = logging.getLogger(__name__)

CONFIG_PATH: Final[str] = "/api/machine/config"
"""The route (``machine_config`` of ``contracts/machine-api.json``, contract 1.15)."""

APPLIED_PATH: Final[str] = "/api/machine/config/applied"
"""Where the console says what it applied, or refused."""

CONFIG_PERIOD: Final[Seconds] = Seconds(60.0)
"""The least time between two requests (``poll_period_s``)."""

MAX_PAYLOAD_BYTES: Final[int] = 16_384
"""The largest signed text Convex sends (``max_payload_bytes``)."""

MAX_REFUSAL_CHARS: Final[int] = 500
"""Characters of a refusal said to the dashboard, at most (``max_reason_chars``)."""

APPLIED_FILE: Final[str] = "config.applied.json"
"""The configuration applied, next to the programmes and the operator list."""

LEVEL_FILE: Final[str] = "config.level.json"
"""Next to it: the newest answer whose signed level changed what the revision applied may
lock, whatever revision that answer was for (:meth:`ConfigSync._relevel`)."""

MACHINE_UNASSIGNED: Final[str] = (
    "RECORD_MACHINE_ID non pose : aucune configuration du site n'est demandee ni "
    "appliquee, la console tourne sur son .env"
)
"""Why a console whose machine is not named takes no configuration: it could not tell its
own from another machine's, signed by the same key."""

SUBJECT: Final[str] = "configuration"
"""What the refusals of a signature call the text."""


@dataclass(frozen=True, slots=True)
class AppliedConfig:
    """The revision this console runs with, as it was received and verified."""

    revision: Revision
    values: Mapping[str, ConfigValue]
    issued_at: int
    level: ValidationLevel
    """The validation level signed with the newest answer kept (this revision's, or a
    later one in :data:`LEVEL_FILE`): a lock it does not allow is run false
    (:func:`held_to_level`)."""

    @property
    def ref(self) -> MachineConfigRef:
        """What the records and the heartbeat say of it."""
        return MachineConfigRef(self.revision.number, self.revision.digest)


@dataclass(frozen=True, slots=True)
class PendingConfig:
    """A revision accepted by this console that waits for an operator to confirm it."""

    revision: Revision
    changes: tuple[ConfigChange, ...]
    stored: StoredList


def _stored(path: Path) -> Result[StoredList | None, str]:
    """What ``path`` holds, as written; ``None`` when there is no file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Ok(None)
    except OSError as error:
        return Err(f"illisible ({describe_os_error(error)})")
    try:
        return Ok(STORED_LIST.validate_json(raw))
    except ValueError:
        return Err("mal forme")


def _offer_at(
    path: Path, public_key: bytes | None, machine_id: str
) -> Result[ConfigOffer | None, str]:
    """The answer kept at ``path``, its signature verified again; ``None``: no file."""
    kept = _stored(path)
    if isinstance(kept, Err):
        return kept
    stored = kept.value
    if stored is None:
        return Ok(None)
    verified = signed_text(
        {"status": "signed", "payload": stored.payload, "signature": stored.signature},
        public_key,
        SUBJECT,
    )
    offer = verified if isinstance(verified, Err) else read_offer(stored.payload, machine_id)
    if isinstance(offer, Err):
        return Err(f"refuse : {offer.error}")
    return Ok(offer.value)


def load_applied(
    path: Path, public_key: bytes | None, machine_id: str | None
) -> Result[AppliedConfig | None, str]:
    """The configuration kept at ``path``, verified again; ``None`` when there is none.

    Startup only. Anything else than a file absent is refused with its reason:
    the console then does not start (a configuration problem, said, never
    restarted on), because its ``.env`` no longer holds what it applied.
    Removing the file takes the console back to its ``.env``. Its level is
    that of the newest answer kept (:func:`_leveled`). ``machine_id`` ``None``
    (``RECORD_MACHINE_ID`` not set): no file is read, whatever is there, and
    the console runs on its ``.env``.
    """
    if machine_id is None:
        if path.exists():
            _logger.warning("configuration: data/%s ignore : %s", APPLIED_FILE, MACHINE_UNASSIGNED)
        return Ok(None)
    found = _offer_at(path, public_key, machine_id)
    if isinstance(found, Err):
        return found
    offer = found.value
    if offer is None:
        return Ok(None)
    revision = offer.revision
    if revision is None:
        return Err("refuse : aucune revision")
    read = read_values(revision.document)
    if read.problems:
        return Err(f"refuse : {_words(read.problems)}")
    applied = AppliedConfig(revision, read.values, offer.issued_at, offer.level)
    return Ok(_leveled(applied, path.with_name(LEVEL_FILE), public_key, machine_id))


def _leveled(
    applied: AppliedConfig, path: Path, public_key: bytes | None, machine_id: str
) -> AppliedConfig:
    """``applied`` at the level of the answer kept at ``path`` when it is the newer.

    A file there that does not verify holds every lock false (level ``none``):
    it was only ever written because a level dropped, and the safe reading of a
    level unknown is the lowest. A level dropping is the barrier, not a change,
    and the only thing that holds a lock false without a confirmation; any change
    of a lock, off or back on, is a safety change an operator confirms.
    """
    found = _offer_at(path, public_key, machine_id)
    if isinstance(found, Err):
        _logger.warning("configuration: data/%s %r : verrous tenus a faux", LEVEL_FILE, found.error)
        return replace(applied, level=ValidationLevel.NONE)
    later = found.value
    if later is None or later.issued_at <= applied.issued_at:
        return applied
    return replace(applied, issued_at=later.issued_at, level=later.level)


def held_to_level(
    config: LocalConfig, applied: AppliedConfig | None
) -> tuple[LocalConfig, tuple[str, ...]]:
    """``config`` with every lock its level does not allow run false, and those locks.

    The level is the one signed with the newest answer kept: a level lowered on
    the registry after the approval reaches the console with the next answer,
    for this revision or a later one, which is kept, and the console restarts on
    it (:meth:`ConfigSync._relevel`), nobody asked. A level raised again gives a
    held lock back only as a safety change an operator confirms at rest.
    """
    if applied is None:
        return config, ()
    refused = locks_above(applied.values, applied.level)
    held = replace(
        config,
        programs_enabled=config.programs_enabled and KEY_PROGRAMS_ENABLED not in refused,
        occupied_enabled=config.occupied_enabled and KEY_OCCUPIED_ENABLED not in refused,
    )
    return held, refused


def applied_environment(
    env: Mapping[str, str], applied: AppliedConfig | None
) -> tuple[Mapping[str, str], tuple[str, ...]]:
    """The ``.env`` the console loads, and the keys of ``env`` it does not take.

    No configuration applied: ``env`` as it is, as before ANH-141. One applied:
    its values in place of the ``.env``'s for every key the dashboard sets; the
    ``.env``'s are left out, and named so that the console says it once.
    """
    if applied is None:
        return env, ()
    merged = environment(applied.values, env)
    ignored = tuple(
        sorted(key for key, text in env.items() if key in CLOUD_ENV_KEYS and text.strip())
    )
    return merged, ignored


def _words(problems: tuple[ConfigProblem, ...]) -> str:
    return "; ".join(f"{problem.key}: {problem.detail}" for problem in problems)


def _shown(change: ConfigChange) -> ChangeView:
    before, after = env_text(change.before) or "-", env_text(change.after) or "-"
    return ChangeView(change.key, change.family is Family.SAFETY, before, after)


def running_values(applied: AppliedConfig) -> dict[str, ConfigValue]:
    """The values the revision applied runs with: its own, the locks its level holds false."""
    return {
        **applied.values,
        **dict.fromkeys(locks_above(applied.values, applied.level), False),
    }


def _given_back(applied: AppliedConfig, level: ValidationLevel) -> tuple[ConfigChange, ...]:
    """The locks the revision applied sets, held false at its level, that ``level`` allows:
    each one a safety change, ``false -> true``, for an operator to confirm."""
    allowed = locks_above(applied.values, level)
    return tuple(
        ConfigChange(key, Family.SAFETY, False, True)
        for key in locks_above(applied.values, applied.level)
        if key not in allowed
    )


def view_of(
    applied: AppliedConfig | None,
    current: Mapping[str, ConfigValue],
    *,
    pending: PendingView | None = None,
    refusal: str | None = None,
    restarting: bool = False,
) -> ConfigView:
    """The page's view of a console running ``current``, from ``applied`` or its ``.env``."""
    name, location = current.get("name"), current.get("location")
    return ConfigView(
        revision=None if applied is None else applied.revision.number,
        config_hash=None if applied is None else applied.revision.digest,
        name=name if isinstance(name, str) else None,
        location=location if isinstance(location, str) else None,
        pending=pending,
        refusal=refusal,
        restarting=restarting,
    )


def _keys(changes: tuple[ConfigChange, ...]) -> str:
    return ", ".join(change.key for change in changes) or "aucune cle"


def _safety(changes: tuple[ConfigChange, ...]) -> bool:
    return any(change.family is Family.SAFETY for change in changes)


@final
class ConfigSync:
    """Asks for the configuration from a task of its own, at rest; applies what it accepts.

    Mutable, owned by the event loop: when it last asked, the newest text taken,
    what waits for an operator, and what was said to the dashboard.
    """

    __slots__ = (
        "_acked",
        "_applied",
        "_asked",
        "_at_rest",
        "_clock",
        "_current",
        "_hold",
        "_issued_at",
        "_local",
        "_machine_id",
        "_medical",
        "_path",
        "_pending",
        "_public_key",
        "_refusal",
        "_reported",
        "_restart",
        "_restarting",
        "_say",
        "_transport",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        transport: CloudTransport,
        public_key: bytes | None,
        path: Path,
        at_rest: Callable[[], bool],
        say: Callable[[str], None],
        machine_id: str,
        local: Mapping[str, str],
        medical: MedicalStatus,
        current: Mapping[str, ConfigValue],
        applied: AppliedConfig | None,
        hold: Callable[[bool], None],
        restart: Callable[[], None],
    ) -> None:
        """``local``: this console's ``.env`` (its local keys are judged with every
        revision). ``current``: the values it runs with. ``applied``: the revision they
        came from, ``None`` on a ``.env``. ``hold``: refuse every start (``True``), or
        no longer (``False``, a write that failed). ``restart``: the console's
        controlled restart, at rest."""
        self._clock: Clock = clock
        self._transport: CloudTransport = transport
        self._public_key: bytes | None = public_key
        self._path: Path = path
        self._at_rest: Callable[[], bool] = at_rest
        self._say: Callable[[str], None] = say
        self._machine_id: str = machine_id
        self._local: Mapping[str, str] = local
        self._medical: MedicalStatus = medical
        self._current: Mapping[str, ConfigValue] = current
        self._applied: AppliedConfig | None = applied
        self._hold: Callable[[bool], None] = hold
        self._restart: Callable[[], None] = restart
        self._asked: Monotonic | None = None
        self._issued_at: int = -1 if applied is None else applied.issued_at
        self._pending: PendingConfig | None = None
        self._refusal: str | None = None
        self._reported: tuple[int, str] | None = None
        self._acked: bool = applied is None
        self._restarting: bool = False

    def view(self) -> ConfigView:
        """What the page shows; synchronous, no I/O."""
        pending = self._pending
        return view_of(
            self._applied,
            self._current,
            pending=None
            if pending is None
            else PendingView(pending.revision.number, tuple(map(_shown, pending.changes))),
            refusal=self._refusal,
            restarting=self._restarting,
        )

    async def step(self) -> None:
        """Ask for the configuration when it is due and the machine is at rest; never raises."""
        now = self._clock.monotonic()
        if self._restarting or (
            self._asked is not None and elapsed(self._asked, now) < CONFIG_PERIOD
        ):
            return
        if not self._at_rest():
            return
        self._asked = now
        await self._acknowledge()
        fetched = await self._fetch()
        if isinstance(fetched, Err):
            self._refuse(fetched.error)
        elif fetched.value is not None:
            await self._receive(*fetched.value)

    async def _fetch(self) -> Result[tuple[ConfigOffer, StoredList] | None, str]:
        """The signed answer, verified and read; ``None`` when there is nothing to read."""
        fetched = await self._transport.get(CONFIG_PATH, None)
        if isinstance(fetched, Err):
            error = fetched.error
            if isinstance(error, Unreachable) and error.detail == TOO_LARGE_ANSWER:
                return Err("reponse de la configuration trop grande pour etre lue")
            _logger.info("machine configuration: nothing received (%r)", error)
            return Ok(None)
        answer = fetched.value
        signature = answer.get("signature")
        text = signed_text(answer, self._public_key, SUBJECT)
        if isinstance(text, Err):
            return text
        if text.value is None or not isinstance(signature, str):
            _logger.info("machine configuration: the dashboard signs none")
            return Ok(None)
        offer = (
            Err(f"configuration au-dela de {MAX_PAYLOAD_BYTES} octets")
            if len(text.value.encode("utf-8")) > MAX_PAYLOAD_BYTES
            else read_offer(text.value, self._machine_id)
        )
        if isinstance(offer, Err):
            return offer
        return Ok((offer.value, StoredList(payload=text.value, signature=signature)))

    async def _receive(self, offer: ConfigOffer, stored: StoredList) -> None:
        if offer.issued_at < self._issued_at:
            self._refuse("configuration plus ancienne que celle de la console")
            return
        self._issued_at = offer.issued_at
        revision = offer.revision
        applied = self._applied
        if applied is not None and set(locks_above(applied.values, offer.level)) - set(
            locks_above(applied.values, applied.level)
        ):
            # Every answer carries the registry's level now, whatever revision it is for: a
            # lock the revision this console runs may no longer hold drops before anything.
            await self._relevel(applied, offer.level, stored)
        elif revision is None:
            # Nothing approved: the console keeps what it has, nothing waits.
            self._pending = None
            self._refusal = None
        elif applied is not None and revision == applied.revision:
            # What this console runs: nothing waits, unless a level raised again allows a
            # lock held false, which only an operator gives back.
            self._refusal = None
            given = _given_back(applied, offer.level)
            if given:
                self._wait(revision, given, stored)
            else:
                self._pending = None
        else:
            await self._receive_revision(revision, offer.level, stored)

    async def _receive_revision(
        self, revision: Revision, level: ValidationLevel, stored: StoredList
    ) -> None:
        """Another revision than the one applied: refused, applied, or waiting."""
        applied = self._applied
        if applied is not None and revision.number <= applied.revision.number:
            self._pending = None
            self._refuse(
                f"revision {revision.number} : la console applique la revision "
                f"{applied.revision.number}"
            )
            return
        reviewed = review(revision.document, self._local, level, self._medical)
        if isinstance(reviewed, Err):
            self._pending = None
            await self._refuse_revision(revision, reviewed.error)
            return
        self._refusal = None
        changes = changes_of(self._current, reviewed.value.values)
        if not _safety(changes):
            self._pending = None
            _ = await self._apply(revision, changes, stored, None)
            return
        self._wait(revision, changes, stored)

    def _wait(
        self, revision: Revision, changes: tuple[ConfigChange, ...], stored: StoredList
    ) -> None:
        """A safety change waits for an operator; said once per revision."""
        if self._pending is None or (self._pending.revision, self._pending.changes) != (
            revision,
            changes,
        ):
            self._say(
                f"configuration revision {revision.number} en attente : cles de surete "
                f"changees ({_keys(changes)}), a confirmer a la console par un operateur "
                "connecte"
            )
        self._pending = PendingConfig(revision, changes, stored)

    async def confirm(self, operator: str) -> Result[int, str]:
        """An operator signed in at the console applies the revision waiting. Never raises.

        Refused with nothing waiting, and away from rest: a session in progress
        keeps the configuration it started with. The dashboard is asked once
        more first, and the revision is applied only as it answers now: still
        in force, its locks allowed by the level signed with this very answer
        (a level lowered since the revision came down refuses them), never
        from an answer older than the last one taken. Offline, nothing is
        applied.
        """
        pending = self._pending
        if pending is None:
            return Err("aucune configuration de surete en attente")
        if not self._at_rest():
            return Err("console pas au repos : terminer la seance avant d'appliquer")
        fetched = await self._fetch()
        if isinstance(fetched, Err) or fetched.value is None:
            words = "sans reponse signee" if isinstance(fetched, Ok) else fetched.error
            return Err(f"tableau de bord {words} : rien n'est applique")
        await self._receive(*fetched.value)
        current = self._pending
        if current is None or current.revision != pending.revision:
            return Err(
                f"la revision {pending.revision.number} n'est plus celle du tableau de bord, "
                "ou plus permise : rien n'est applique"
            )
        return await self._apply(current.revision, current.changes, current.stored, operator)

    async def _apply(
        self,
        revision: Revision,
        changes: tuple[ConfigChange, ...],
        stored: StoredList,
        operator: str | None,
    ) -> Result[int, str]:
        """Write the answer as it came, then ask for the controlled restart."""
        number = revision.number
        who = "" if operator is None else f", confirmee par {operator}"
        kept = await self._keep(
            self._path, stored, f"configuration revision {number} appliquee ({_keys(changes)}){who}"
        )
        if isinstance(kept, Err):
            return Err(f"configuration non enregistree : {kept.error}")
        return Ok(number)

    async def _relevel(
        self, applied: AppliedConfig, level: ValidationLevel, stored: StoredList
    ) -> None:
        """The level signed with this answer refuses a lock the revision applied runs:
        keep the answer next to it (:data:`LEVEL_FILE`), and restart on it at rest, its
        locks held to the level. The revision applied stays what it is."""
        refused = ", ".join(locks_above(applied.values, level))
        _ = await self._keep(
            self._path.with_name(LEVEL_FILE),
            stored,
            f"configuration : niveau de validation signe abaisse, {refused} au dessus du "
            f"niveau {level.value} : mis a faux",
        )

    async def _keep(self, path: Path, stored: StoredList, words: str) -> Result[None, str]:
        """Write ``stored`` as it came at ``path``, then restart the console at rest. Never
        raises.

        Still at rest after the answer was awaited (no session, and no start arming),
        or nothing is written. Every start is refused from before the write until the
        process ends for its restart: no session arms on the values the console is
        leaving. A write that does not complete, whatever stopped it, lifts it.
        """
        if not self._at_rest():
            self._say(f"{words} ; non enregistre : la console n'est plus au repos")
            return Err("console plus au repos")
        self._hold(True)
        content = STORED_LIST.dump_json(stored) + b"\n"
        written: Result[None, str] = Err("ecriture interrompue")
        try:
            written = await anyio.to_thread.run_sync(partial(replace_file, path, content))
        finally:
            if isinstance(written, Err):
                self._hold(False)
        if isinstance(written, Err):
            self._say(
                f"{words} ; non enregistre ({written.error}) : la configuration en vigueur reste"
            )
            return written
        self._pending = None
        self._restarting = True
        # Asked before it is said: a line that fails to be written leaves no hold behind.
        self._restart()
        self._say(f"{words} : redemarrage controle de la console au repos")
        return Ok(None)

    async def _acknowledge(self) -> None:
        """Say once that this process runs the revision it applied."""
        applied = self._applied
        if self._acked or applied is None:
            return
        sent = await self._transport.post(
            APPLIED_PATH,
            {"revision": applied.revision.number, "hash": applied.revision.digest},
        )
        if isinstance(sent, Ok):
            self._acked = True
            return
        _logger.info("machine configuration: applied revision not said yet (%r)", sent.error)

    async def _refuse_revision(
        self, revision: Revision, problems: tuple[ConfigProblem, ...]
    ) -> None:
        """Refuse a revision this console does not revalidate; say it to the dashboard once."""
        words = _words(problems)
        self._refuse(f"revision {revision.number} refusee : {words}")
        key = (revision.number, revision.digest)
        if key == self._reported:
            return
        sent = await self._transport.post(
            APPLIED_PATH,
            {
                "revision": revision.number,
                "hash": revision.digest,
                "refusal": words[:MAX_REFUSAL_CHARS],
            },
        )
        if isinstance(sent, Ok):
            self._reported = key

    def _refuse(self, reason: str) -> None:
        """Say an answer dropped, once for the same reason: the configuration in force stays."""
        if reason == self._refusal:
            return
        self._refusal = reason
        self._say(f"configuration refusee, la console garde la sienne : {reason}")
