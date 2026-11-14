"""Who may act at the console (ANH-152): the signed operator list, sign-in, sessions, lockout.

Every action of the console carries an authenticated identity, not a name typed
in a field. Three things decide who that is, and all three live here:

**The list** (:class:`OperatorList`). Convex signs, for this machine, its
enabled operators (each with the scrypt hash of the code they chose on the
site, its salt and its parameters) and its tablets (each with the SHA-256 of its
device token), on the programme catalogue's channel: same Ed25519 key, same
public key in the image (``operator_list`` of ``contracts/machine-api.json``,
contract 1.13). :class:`~src.operator_sync.OperatorSync` asks for it at rest only, verifies the
signature over the exact bytes before reading anything, refuses a text that
does not say it is an operator list, a list of another machine than
``RECORD_MACHINE_ID`` when that is set, a code hashed with other parameters than
the contract's, and a list that is not newer than the one it holds (an old
answer replayed would bring back a disabled operator). What it accepts is
written to ``data/operators.json`` as it was received, signature included, and
verified again at the next start (:func:`load_list`).

**A console that never accepted a list opens no action.** Reads, the STOP and
the emergency stop stay reachable with a device token (``src/web/routes.py``);
nothing else does. Offline, the last list accepted keeps working, for as long
as it is the last one.

**The sign-in** (:meth:`ConsoleAccess.sign_in`). An operator of the list, their
code checked offline against the hash, off the event loop, one sign-in at a
time. Three counts of wrong codes, each locking for :data:`LOCKOUT_PERIOD`:
:data:`LOCKOUT_FAILURES` in a row lock that operator out;
:data:`DEVICE_LOCKOUT_FAILURES` from one device, whatever the operator, lock
that device's sign-ins; :data:`CONSOLE_LOCKOUT_FAILURES` on the whole console
lock every sign-in. A right code clears its operator's count; a device's and
the console's clear after a whole period without a wrong code. The counts are
written to ``data/operator_lockout.json`` and a restart does not lift a lock:
what is found locked at the start is locked for a whole period from it. A
6-digit code is a weak secret, and these counts are what protect it on the
console; anyone holding the list itself can try every code offline
(``docs/securite.md``, section 17).

**The session** (:meth:`ConsoleAccess.session`). Bound to the device that
signed in, ended by an explicit sign-out, after :data:`SESSION_LIFETIME`, after
:data:`SESSION_IDLE` without an action, or when a new list no longer holds its
operator or its tablet, or holds another code for its operator. In memory
only: a restart signs everyone out.

Nothing here commands the machine, and nothing here can delay a stop: the
emergency stop needs only :meth:`ConsoleAccess.device_of`, a digest and a
comparison, never the sign-in's lock nor its hash.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, Literal, assert_never, cast, final

import anyio
from pydantic import ConfigDict, TypeAdapter
from pydantic.dataclasses import dataclass as validated

from src.clock import Clock
from src.medical import verify_signature
from src.record.writer import create_private, describe_os_error, fsync_path
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, UnixMillis, elapsed

_logger: logging.Logger = logging.getLogger(__name__)

OPERATORS_PATH: Final[str] = "/api/machine/operators"
"""The route (``operator_list`` of ``contracts/machine-api.json``, contract 1.13)."""

LIST_KIND: Final[str] = "operator_list"
"""What the signed text says it is: a catalogue signed by the same key is not one."""

OPERATORS_PERIOD: Final[Seconds] = Seconds(60.0)
"""The least time between two requests (``poll_period_s`` of the shared file)."""

MAX_PAYLOAD_BYTES: Final[int] = 200_000
"""The largest signed text Convex sends (``max_payload_bytes``)."""

SESSION_LIFETIME: Final[Seconds] = Seconds(8 * 3600.0)
"""How long a session lives, whatever is done with it."""

SESSION_IDLE: Final[Seconds] = Seconds(30 * 60.0)
"""How long a session lives without an action."""

LOCKOUT_FAILURES: Final[int] = 5
"""Wrong codes in a row that lock an operator out."""

DEVICE_LOCKOUT_FAILURES: Final[int] = 10
"""Wrong codes from one device, whatever the operator, that lock that device's sign-ins."""

CONSOLE_LOCKOUT_FAILURES: Final[int] = 20
"""Wrong codes on the whole console that lock every sign-in: the ceiling of a guess."""

LOCKOUT_PERIOD: Final[Seconds] = Seconds(15 * 60.0)
"""How long a lock lasts; and how long without a wrong code clears a device's count, or
the console's."""

SCRYPT_N: Final[int] = 32768
SCRYPT_R: Final[int] = 8
SCRYPT_P: Final[int] = 1
SALT_BYTES: Final[int] = 16
HASH_BYTES: Final[int] = 32
"""The one set of parameters a code may carry (``code`` of the shared file), Convex's.
Any other is refused when the list is read: parameters a list could choose would hold
the sign-in for hours, or have OpenSSL refuse to hash."""

DEVICE_ACTOR_PREFIX: Final[str] = "device:"
"""Who an event names when a tablet acted with nobody signed in (``device_actor_prefix``)."""

LOCAL_DEVICE: Final[str] = "local"
"""The tablet of the console's own ``DEVICE_TOKEN`` (``UI_TOKEN`` before ANH-152)."""

LOOPBACK_DEVICE: Final[str] = "loopback"
"""Any browser on the Pi itself, when no device token is configured (loopback only)."""

_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_MAX_NAME_CHARS: Final[int] = 100


SESSION_DROPPED: Final[str] = "console: session fermee, retiree de la liste"
"""What the logbook says of a session a new list ended."""

CODE_CHANGED: Final[str] = "console: session fermee, code change"
"""What the logbook says of a session a new code for its operator ended."""

type LockScope = Literal["operator", "device", "console"]
"""What a count of wrong codes is about: an operator, a device, or the whole console."""

type _Count = tuple[LockScope, str]
"""One count: its scope, and the operator or the device (empty for the console)."""

_LIMITS: Final[Mapping[LockScope, int]] = {
    "operator": LOCKOUT_FAILURES,
    "device": DEVICE_LOCKOUT_FAILURES,
    "console": CONSOLE_LOCKOUT_FAILURES,
}


def device_actor(device_id: str) -> str:
    """The actor of an event a tablet made with nobody signed in."""
    return f"{DEVICE_ACTOR_PREFIX}{device_id}"


# =========================================================================
# The list
# =========================================================================


@dataclass(frozen=True, slots=True)
class ScryptCode:
    """One operator's code as the list carries it: never the code itself."""

    n: int
    r: int
    p: int
    salt: bytes
    digest: bytes


@dataclass(frozen=True, slots=True)
class ListedOperator:
    user_id: str
    """The account's identifier in Convex: what every record names this operator by."""

    name: str
    """What the sign-in screen shows. Never written in a record."""

    code: ScryptCode


@dataclass(frozen=True, slots=True)
class ListedDevice:
    device_id: str
    name: str
    token_sha256: bytes


@dataclass(frozen=True, slots=True)
class OperatorList:
    """The signed text, read: who may sign in on this machine, and from which tablets."""

    machine_id: str
    issued_at: UnixMillis
    operators: tuple[ListedOperator, ...]
    devices: tuple[ListedDevice, ...]

    def operator(self, user_id: str) -> ListedOperator | None:
        """The listed operator of that account, or ``None``."""
        return next((o for o in self.operators if o.user_id == user_id), None)

    def same_holders(self, other: OperatorList) -> bool:
        """Whether both lists open the console to the same people and tablets."""
        return self.operators == other.operators and self.devices == other.devices


def scrypt_memory(n: int, r: int, p: int) -> int:
    """What OpenSSL needs for these parameters, in bytes."""
    return 128 * r * (n + 2 + p)


def _b64(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error, or a character that is not ASCII
        return None


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _code(value: object) -> ScryptCode | None:
    if not isinstance(value, dict):
        return None
    fields = cast(Mapping[str, object], value)
    n, r, p = _whole(fields.get("n")), _whole(fields.get("r")), _whole(fields.get("p"))
    salt, digest = _b64(fields.get("salt")), _b64(fields.get("hash"))
    if (
        fields.get("algorithm") != "scrypt"
        or n is None
        or r is None
        or p is None
        or salt is None
        or digest is None
        or (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        or len(salt) != SALT_BYTES
        or len(digest) != HASH_BYTES
    ):
        return None
    return ScryptCode(n=n, r=r, p=p, salt=salt, digest=digest)


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_NAME_CHARS else None


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and _ID.fullmatch(value) is not None else None


def _operator(value: object) -> ListedOperator | None:
    if not isinstance(value, dict):
        return None
    fields = cast(Mapping[str, object], value)
    user_id, name = _identifier(fields.get("userId")), _name(fields.get("name"))
    code = _code(fields.get("code"))
    if user_id is None or name is None or code is None:
        return None
    return ListedOperator(user_id=user_id, name=name, code=code)


def _device(value: object) -> ListedDevice | None:
    if not isinstance(value, dict):
        return None
    fields = cast(Mapping[str, object], value)
    device_id, name = _identifier(fields.get("deviceId")), _name(fields.get("name"))
    digest = fields.get("tokenSha256")
    if (
        device_id is None
        or name is None
        or device_id in (LOCAL_DEVICE, LOOPBACK_DEVICE)
        or not isinstance(digest, str)
        or _SHA256_HEX.fullmatch(digest) is None
    ):
        return None
    return ListedDevice(device_id=device_id, name=name, token_sha256=bytes.fromhex(digest))


def _entries(value: object) -> Sequence[object] | None:
    return cast(Sequence[object], value) if isinstance(value, list) else None


def read_list(text: str, machine_id: str | None) -> Result[OperatorList, str]:
    """The signed text as an :class:`OperatorList`, or why it is not one.

    The one place its untyped value enters, annotated ``object`` at once. Every
    entry must be well formed and every id appear once: a list is taken whole
    or not at all. ``machine_id``: this machine (``RECORD_MACHINE_ID``), whose
    list alone is taken; ``None`` when it is not set, and any machine's is.
    """
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return Err("texte de la liste des operateurs illisible")
    if not isinstance(parsed, dict):
        return Err("liste des operateurs mal formee")
    document = cast(Mapping[str, object], parsed)
    if document.get("kind") != LIST_KIND:
        return Err("texte signe qui n'est pas une liste des operateurs")
    listed_for = _identifier(document.get("machineId"))
    issued_at = _whole(document.get("issuedAt"))
    operators = _entries(document.get("operators"))
    devices = _entries(document.get("devices"))
    if listed_for is None or issued_at is None or operators is None or devices is None:
        return Err("liste des operateurs mal formee")
    listed = tuple(_operator(entry) for entry in operators)
    tablets = tuple(_device(entry) for entry in devices)
    kept = tuple(o for o in listed if o is not None)
    kept_devices = tuple(d for d in tablets if d is not None)
    if (
        issued_at < 0
        or len(kept) != len(listed)
        or len(kept_devices) != len(tablets)
        or len({o.user_id for o in kept}) != len(kept)
        or len({d.device_id for d in kept_devices}) != len(kept_devices)
    ):
        return Err("liste des operateurs mal formee")
    held = OperatorList(
        machine_id=listed_for,
        issued_at=UnixMillis(issued_at),
        operators=kept,
        devices=kept_devices,
    )
    return _of_machine(held, machine_id)


def _of_machine(held: OperatorList, machine_id: str | None) -> Result[OperatorList, str]:
    """``held`` when it is this machine's list, or the machine is not known."""
    if machine_id is not None and machine_id != held.machine_id:
        listed_for = held.machine_id
        return Err(f"liste d'une autre machine ({listed_for!r}, cette console est {machine_id!r})")
    return Ok(held)


def signed_text(
    document: Mapping[str, object], public_key: bytes | None, subject: str = "liste"
) -> Result[str | None, str]:
    """The text of an answer, once its signature verifies; ``None`` when nothing is signed.

    As the catalogue's (:func:`src.programme_catalog.signed_text`): nothing of
    the text is read before the signature is checked over its exact bytes.
    ``subject``: what the refusals call the text (the machine's configuration
    uses it too, ANH-141).
    """
    status = document.get("status")
    if status == "not_configured":
        return Ok(None)
    payload = document.get("payload")
    signature = document.get("signature")
    if status != "signed" or not isinstance(payload, str) or not isinstance(signature, str):
        return Err(f"reponse sans {subject} signee")
    if public_key is None:
        return Err("cle publique du catalogue absente ou illisible")
    raw = _b64(signature)
    try:
        content = payload.encode("utf-8")
    except UnicodeEncodeError:
        content = None
    if raw is None or content is None:
        return Err(f"signature de la {subject} invalide : texte ou signature illisible")
    verified = verify_signature(content, raw, public_key)
    if isinstance(verified, Err):
        return Err(f"signature de la {subject} invalide : {verified.error.detail}")
    return Ok(payload)


# =========================================================================
# On the disk: the list as received, and the lockout counts
# =========================================================================


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class StoredList:
    """``data/operators.json``: the answer accepted, verified again at every start."""

    schema_version: Literal[1] = 1
    payload: str
    signature: str


@validated(frozen=True, slots=True, kw_only=True, config=ConfigDict(extra="forbid"))
class StoredLockout:
    """``data/operator_lockout.json``: the counts of wrong codes. Validated wire shapes,
    replaced whole: never mutated."""

    schema_version: Literal[1] = 1
    failures: dict[str, int]
    """In a row, per operator."""

    devices: dict[str, int]
    """Per device, whatever the operator."""

    console: int
    """On the whole console."""


NO_FAILURES: Final[StoredLockout] = StoredLockout(failures={}, devices={}, console=0)


STORED_LIST: Final[TypeAdapter[StoredList]] = TypeAdapter(StoredList)
_STORED_LOCKOUT: Final[TypeAdapter[StoredLockout]] = TypeAdapter(StoredLockout)


def replace_file(path: Path, content: bytes) -> Result[None, str]:
    """Replace ``path``: a private file, flushed, then renamed. Blocking, never raises."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.unlink(missing_ok=True)
        create_private(temporary, content)
        fsync_path(temporary)
        temporary.replace(path)
    except OSError as error:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        return Err(describe_os_error(error))
    return Ok(None)


def load_list(
    path: Path, public_key: bytes | None, machine_id: str | None
) -> Result[OperatorList | None, str]:
    """The list kept at ``path``, verified again; ``None`` when there is none. Startup only.
    ``machine_id``: as :func:`read_list`."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Ok(None)
    except OSError as error:
        return Err(f"liste des operateurs illisible ({describe_os_error(error)})")
    try:
        stored = STORED_LIST.validate_json(raw)
    except ValueError:
        return Err("liste des operateurs enregistree mal formee")
    text = signed_text(
        {"status": "signed", "payload": stored.payload, "signature": stored.signature},
        public_key,
    )
    if isinstance(text, Err):
        return Err(f"liste des operateurs enregistree refusee : {text.error}")
    read = read_list(stored.payload, machine_id)
    if isinstance(read, Err):
        return Err(f"liste des operateurs enregistree refusee : {read.error}")
    return Ok(read.value)


def load_failures(path: Path) -> StoredLockout:
    """The lockout counts kept at ``path``; none when absent or unreadable. Startup only.

    An unreadable file is said and read as no failure: the console then still
    locks after five wrong codes, counted again from this start.
    """
    try:
        return _STORED_LOCKOUT.validate_json(path.read_bytes())
    except FileNotFoundError:
        return NO_FAILURES
    except (OSError, ValueError) as error:
        _logger.warning(
            "operator lockout counts unreadable, counted again: %s", type(error).__name__
        )
        return NO_FAILURES


# =========================================================================
# The sign-in, the sessions and the lockout
# =========================================================================


@dataclass(frozen=True, slots=True)
class SignedIn:
    """One operator signed in on one tablet."""

    user_id: str
    name: str
    device_id: str
    since: Monotonic
    last_active: Monotonic


@dataclass(frozen=True, slots=True)
class NoList:
    """This console never accepted a list: nobody can sign in."""


@dataclass(frozen=True, slots=True)
class UnknownOperator:
    """The account is not an operator of the list this console holds."""


@dataclass(frozen=True, slots=True)
class Locked:
    """Too many wrong codes: no code is checked until ``retry_after`` has passed.
    ``scope``: what is locked, this operator, this device or the whole console."""

    retry_after: Seconds
    scope: LockScope = "operator"


@dataclass(frozen=True, slots=True)
class WrongCode:
    """The code is not this operator's. ``remaining``: wrong codes before the lock."""

    remaining: int


type SignInRefusal = NoList | UnknownOperator | Locked | WrongCode

_LOCKED_WORDS: Final[Mapping[LockScope, str]] = {
    "operator": "operateur verrouille",
    "device": "connexion verrouillee sur cet appareil",
    "console": "toute connexion verrouillee sur cette console",
}


def describe_refusal(refusal: SignInRefusal) -> str:
    """The sentence the page shows for a refused sign-in."""
    match refusal:
        case NoList():
            return (
                "aucune liste d'operateurs recue par cette console : seuls l'E-STOP, "
                "le STOP et les lectures sont ouverts"
            )
        case UnknownOperator():
            return "operateur inconnu de cette console"
        case Locked():
            return (
                f"trop de codes faux : {_LOCKED_WORDS[refusal.scope]} encore "
                f"{max(1, round(refusal.retry_after / 60))} min"
            )
        case WrongCode():
            return f"code faux ({refusal.remaining} essai(s) avant le verrouillage)"
    raise assert_never(refusal)


@dataclass(frozen=True, slots=True)
class AccessNote:
    """A line for the console's logbook: what happened, and who did it."""

    detail: str
    actor: str
    warning: bool


def code_matches(code: str, held: ScryptCode) -> bool:
    """Whether ``code`` hashes to ``held``. Blocking (tens to hundreds of ms): off the loop."""
    computed = hashlib.scrypt(
        code.encode("utf-8"),
        salt=held.salt,
        n=held.n,
        r=held.r,
        p=held.p,
        maxmem=scrypt_memory(held.n, held.r, held.p) + 1024 * 1024,
        dklen=len(held.digest),
    )
    return hmac.compare_digest(computed, held.digest)


_LOCK_SUBJECTS: Final[Mapping[LockScope, str]] = {
    "operator": "{name} verrouille",
    "device": f"connexions depuis {DEVICE_ACTOR_PREFIX}{{name}} verrouillees",
    "console": "toute connexion verrouillee",
}


def _lock_words(key: _Count, count: int) -> str:
    """What the logbook says of a lock: never a code."""
    scope, name = key
    subject = _LOCK_SUBJECTS[scope].format(name=name)
    return f"console: {subject} {round(LOCKOUT_PERIOD / 60)} min apres {count} codes faux"


@final
class ConsoleAccess:
    """The list held, the sessions open, the lockout counts. See the module docstring.

    Mutable, owned by the event loop: every method but :meth:`sign_in` is
    synchronous, reads memory only and returns at once. :meth:`sign_in` hashes
    off the loop, one at a time, under its own lock, which nothing else takes.
    """

    __slots__ = (
        "_clock",
        "_failures",
        "_last_failure",
        "_list",
        "_lock",
        "_locked_since",
        "_lockout_path",
        "_note",
        "_sessions",
        "_verify",
    )

    def __init__(
        self,
        *,
        clock: Clock,
        held: OperatorList | None,
        lockout_path: Path | None,
        failures: StoredLockout | None = None,
        note: Callable[[AccessNote], None] = lambda _note: None,
        verify: Callable[[str, ScryptCode], bool] = code_matches,
    ) -> None:
        """``held``: the list accepted before this start, or ``None``. ``lockout_path``:
        where the counts are kept; ``None``: in memory only (a test). ``failures``: the
        counts read from it (:func:`load_failures`); what is found locked is locked for
        a whole :data:`LOCKOUT_PERIOD` from now, and every count found is a wrong code
        of now. ``note``: a line for the logbook.
        ``verify``: whether a code is the hash's (:func:`code_matches`), run off the loop.
        """
        self._clock: Clock = clock
        self._list: OperatorList | None = held
        self._lockout_path: Path | None = lockout_path
        self._note: Callable[[AccessNote], None] = note
        self._verify: Callable[[str, ScryptCode], bool] = verify
        self._sessions: dict[str, SignedIn] = {}
        found = NO_FAILURES if failures is None else failures
        self._failures: dict[_Count, int] = {
            **{("operator", user_id): count for user_id, count in found.failures.items()},
            **{("device", device_id): count for device_id, count in found.devices.items()},
            **({("console", ""): found.console} if found.console > 0 else {}),
        }
        now = clock.monotonic()
        self._locked_since: dict[_Count, Monotonic] = {
            key: now for key, count in self._failures.items() if count >= _LIMITS[key[0]]
        }
        self._last_failure: dict[_Count, Monotonic] = dict.fromkeys(self._failures, now)
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def held(self) -> OperatorList | None:
        """The list in force, or ``None``: this console never accepted one."""
        return self._list

    def device_of(self, presented: str | None) -> ListedDevice | None:
        """The listed tablet whose token ``presented`` is, or ``None``. Memory only."""
        held = self._list
        if presented is None or held is None:
            return None
        digest = hashlib.sha256(presented.encode("utf-8")).digest()
        return next((d for d in held.devices if hmac.compare_digest(digest, d.token_sha256)), None)

    def locked(self, user_id: str) -> Seconds | None:
        """How long ``user_id`` stays locked out, or ``None`` when they are not."""
        return self._left(("operator", user_id))

    def _left(self, key: _Count) -> Seconds | None:
        since = self._locked_since.get(key)
        if since is None:
            return None
        left = Seconds(LOCKOUT_PERIOD - elapsed(since, self._clock.monotonic()))
        return left if left > 0 else None

    def adopt(self, accepted: OperatorList) -> None:
        """Put a new list in force: a session whose operator or tablet it no longer
        holds ends, and so does one whose operator it holds with another code. The
        caller verified and wrote it; this touches memory only."""
        before = self._list
        codes = {} if before is None else {o.user_id: o.code for o in before.operators}
        self._list = accepted
        devices = {d.device_id for d in accepted.devices} | {LOCAL_DEVICE, LOOPBACK_DEVICE}
        for cookie, session in tuple(self._sessions.items()):
            listed = accepted.operator(session.user_id)
            if listed is None or session.device_id not in devices:
                del self._sessions[cookie]
                self._note(AccessNote(SESSION_DROPPED, session.user_id, False))
            elif listed.code != codes.get(session.user_id):
                del self._sessions[cookie]
                self._note(AccessNote(CODE_CHANGED, session.user_id, False))

    async def sign_in(
        self, user_id: str, code: str, device_id: str
    ) -> Result[tuple[str, SignedIn], SignInRefusal]:
        """Sign ``user_id`` in on ``device_id``: the cookie value and the session, or why not.

        One sign-in at a time: the lockout is checked, the code hashed and the
        count written before the next one starts, so that no burst of guesses
        passes the lock. Never logs the code.
        """
        async with self._lock:
            held = self._list
            if held is None:
                return Err(NoList())
            listed = held.operator(user_id)
            if listed is None:
                return Err(UnknownOperator())
            counts: tuple[_Count, _Count, _Count] = (
                ("operator", user_id),
                ("device", device_id),
                ("console", ""),
            )
            if self._clear_expired(counts):
                await self._persist()
            locked = self._lock_of(counts)
            if locked is not None:
                return Err(locked)
            matches = await anyio.to_thread.run_sync(partial(self._verify, code, listed.code))
            if not matches:
                return Err(await self._failed(counts, device_id))
            if self._failures.pop(counts[0], None) is not None:
                await self._persist()
            now = self._clock.monotonic()
            session = SignedIn(
                user_id=user_id,
                name=listed.name,
                device_id=device_id,
                since=now,
                last_active=now,
            )
            cookie = secrets.token_urlsafe(32)
            self._sessions[cookie] = session
            self._note(AccessNote("console: connexion", user_id, False))
            return Ok((cookie, session))

    def _clear_expired(self, counts: Sequence[_Count]) -> bool:
        """Clear the counts that are over; whether there was one.

        A lock is over after :data:`LOCKOUT_PERIOD`. A device's count and the
        console's are also over after a whole period without a wrong code; an
        operator's runs until their right code.
        """
        now = self._clock.monotonic()
        over = [key for key in counts if key in self._failures and self._over(key, now)]
        for key in over:
            del self._failures[key]
            _ = self._locked_since.pop(key, None)
            _ = self._last_failure.pop(key, None)
        return bool(over)

    def _over(self, key: _Count, now: Monotonic) -> bool:
        since = self._locked_since.get(key)
        if since is not None:
            return elapsed(since, now) >= LOCKOUT_PERIOD
        return key[0] != "operator" and elapsed(self._last_failure[key], now) >= LOCKOUT_PERIOD

    def _lock_of(self, counts: Sequence[_Count]) -> Locked | None:
        """The longest lock among ``counts``, or ``None``."""
        found: list[tuple[Seconds, LockScope]] = [
            (left, key[0]) for key in counts if (left := self._left(key)) is not None
        ]
        if not found:
            return None
        left, scope = max(found)
        return Locked(retry_after=left, scope=scope)

    async def _failed(self, counts: Sequence[_Count], device_id: str) -> SignInRefusal:
        now = self._clock.monotonic()
        actor = device_actor(device_id)
        for key in counts:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            self._last_failure[key] = now
            if count >= _LIMITS[key[0]]:
                self._locked_since[key] = now
                self._note(AccessNote(_lock_words(key, count), actor, True))
        await self._persist()
        locked = self._lock_of(counts)
        if locked is not None:
            return locked
        self._note(AccessNote(f"console: code faux pour {counts[0][1]}", actor, True))
        return WrongCode(remaining=min(_LIMITS[key[0]] - self._failures[key] for key in counts))

    async def _persist(self) -> None:
        path = self._lockout_path
        if path is None:
            return
        counts = self._failures
        stored = StoredLockout(
            failures={name: n for (scope, name), n in counts.items() if scope == "operator"},
            devices={name: n for (scope, name), n in counts.items() if scope == "device"},
            console=counts.get(("console", ""), 0),
        )
        content = _STORED_LOCKOUT.dump_json(stored) + b"\n"
        written = await anyio.to_thread.run_sync(partial(replace_file, path, content))
        if isinstance(written, Err):
            # Kept in memory: a restart before the next write forgets these counts.
            _logger.warning("operator lockout counts not written: %s", written.error)

    def session(self, cookie: str | None, device_id: str, *, touch: bool) -> SignedIn | None:
        """The session of ``cookie`` on ``device_id``, or ``None``: absent, of another
        tablet, expired, or of an operator the list no longer holds. ``touch``: this
        request is an action, which restarts the idle period. Memory only."""
        if cookie is None:
            return None
        session = self._sessions.get(cookie)
        if session is None or session.device_id != device_id:
            return None
        now = self._clock.monotonic()
        if (
            elapsed(session.since, now) >= SESSION_LIFETIME
            or elapsed(session.last_active, now) >= SESSION_IDLE
        ):
            del self._sessions[cookie]
            self._note(AccessNote("console: session expiree", session.user_id, False))
            return None
        if touch:
            session = SignedIn(
                user_id=session.user_id,
                name=session.name,
                device_id=session.device_id,
                since=session.since,
                last_active=now,
            )
            self._sessions[cookie] = session
        return session

    def sign_out(self, cookie: str | None) -> None:
        """End the session of ``cookie``, if there is one."""
        session = None if cookie is None else self._sessions.pop(cookie, None)
        if session is not None:
            self._note(AccessNote("console: deconnexion", session.user_id, False))

    def remaining(self, session: SignedIn) -> tuple[Seconds, Seconds]:
        """How long ``session`` has before it ends on its own: idle, then whole."""
        now = self._clock.monotonic()
        return (
            Seconds(SESSION_IDLE - elapsed(session.last_active, now)),
            Seconds(SESSION_LIFETIME - elapsed(session.since, now)),
        )
