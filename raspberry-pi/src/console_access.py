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
does not say it is an operator list, and refuses a list older than the one it
holds (an old answer replayed would bring back a disabled operator). What it
accepts is written to ``data/operators.json`` as it was received, signature
included, and verified again at the next start (:func:`load_list`).

**A console that never accepted a list opens no action.** Reads, the STOP and
the emergency stop stay reachable with a device token (``src/web/routes.py``);
nothing else does. Offline, the last list accepted keeps working, for as long
as it is the last one.

**The sign-in** (:meth:`ConsoleAccess.sign_in`). An operator of the list, their
code checked offline against the hash, off the event loop, one sign-in at a
time. Five wrong codes in a row lock that operator out for
:data:`LOCKOUT_PERIOD`; the count is written to ``data/operator_lockout.json``
and a restart does not lift a lock: an operator found locked at the start is
locked for a whole period from it. A 6-digit code is a weak secret, and this
lockout is what protects it on the console; anyone holding the list itself
can try every code offline (``docs/securite.md``, section 17).

**The session** (:meth:`ConsoleAccess.session`). Bound to the device that
signed in, ended by an explicit sign-out, after :data:`SESSION_LIFETIME`, after
:data:`SESSION_IDLE` without an action, or when a new list no longer holds its
operator or its tablet. In memory only: a restart signs everyone out.

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

LOCKOUT_PERIOD: Final[Seconds] = Seconds(15 * 60.0)
"""How long a locked operator stays locked."""

MAX_SCRYPT_MEMORY: Final[int] = 64 * 1024 * 1024
"""The most memory one code's parameters may ask for: a list cannot exhaust the Pi."""

DEVICE_ACTOR_PREFIX: Final[str] = "device:"
"""Who an event names when a tablet acted with nobody signed in (``device_actor_prefix``)."""

LOCAL_DEVICE: Final[str] = "local"
"""The tablet of the console's own ``DEVICE_TOKEN`` (``UI_TOKEN`` before ANH-152)."""

LOOPBACK_DEVICE: Final[str] = "loopback"
"""Any browser on the Pi itself, when no device token is configured (loopback only)."""

_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHA256_HEX: Final = re.compile(r"[0-9a-f]{64}")
_MAX_NAME_CHARS: Final[int] = 100
_MIN_N: Final[int] = 2
_DIGEST_BYTES: Final[range] = range(16, 65)


SESSION_DROPPED: Final[str] = "console: session fermee, retiree de la liste"
"""What the logbook says of a session a new list ended."""


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
        or n < _MIN_N
        or n & (n - 1) != 0
        or r < 1
        or p < 1
        or scrypt_memory(n, r, p) > MAX_SCRYPT_MEMORY
        or not salt
        or len(digest) not in _DIGEST_BYTES
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


def read_list(text: str) -> Result[OperatorList, str]:
    """The signed text as an :class:`OperatorList`, or why it is not one.

    The one place its untyped value enters, annotated ``object`` at once. Every
    entry must be well formed and every id appear once: a list is taken whole
    or not at all.
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
    machine_id = _identifier(document.get("machineId"))
    issued_at = _whole(document.get("issuedAt"))
    operators = _entries(document.get("operators"))
    devices = _entries(document.get("devices"))
    if machine_id is None or issued_at is None or operators is None or devices is None:
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
    return Ok(
        OperatorList(
            machine_id=machine_id,
            issued_at=UnixMillis(issued_at),
            operators=kept,
            devices=kept_devices,
        )
    )


def signed_text(
    document: Mapping[str, object], public_key: bytes | None
) -> Result[str | None, str]:
    """The text of an answer, once its signature verifies; ``None`` when nothing is signed.

    As the catalogue's (:func:`src.programme_catalog.signed_text`): nothing of
    the text is read before the signature is checked over its exact bytes.
    """
    status = document.get("status")
    if status == "not_configured":
        return Ok(None)
    payload = document.get("payload")
    signature = document.get("signature")
    if status != "signed" or not isinstance(payload, str) or not isinstance(signature, str):
        return Err("reponse sans liste signee")
    if public_key is None:
        return Err("cle publique du catalogue absente ou illisible")
    raw = _b64(signature)
    try:
        content = payload.encode("utf-8")
    except UnicodeEncodeError:
        content = None
    if raw is None or content is None:
        return Err("signature de la liste invalide : texte ou signature illisible")
    verified = verify_signature(content, raw, public_key)
    if isinstance(verified, Err):
        return Err(f"signature de la liste invalide : {verified.error.detail}")
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
    """``data/operator_lockout.json``: wrong codes in a row, per operator."""

    schema_version: Literal[1] = 1
    failures: dict[str, int]
    """A validated wire shape, replaced whole: never mutated."""


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


def load_list(path: Path, public_key: bytes | None) -> Result[OperatorList | None, str]:
    """The list kept at ``path``, verified again; ``None`` when there is none. Startup only."""
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
    read = read_list(stored.payload)
    return read if isinstance(read, Err) else Ok(read.value)


def load_failures(path: Path) -> Mapping[str, int]:
    """The lockout counts kept at ``path``; none when absent or unreadable. Startup only.

    An unreadable file is said and read as no failure: the console then still
    locks after five wrong codes, counted again from this start.
    """
    try:
        return _STORED_LOCKOUT.validate_json(path.read_bytes()).failures
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        _logger.warning(
            "operator lockout counts unreadable, counted again: %s", type(error).__name__
        )
        return {}


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
    """Too many wrong codes: no code is checked until ``retry_after`` has passed."""

    retry_after: Seconds


@dataclass(frozen=True, slots=True)
class WrongCode:
    """The code is not this operator's. ``remaining``: wrong codes before the lock."""

    remaining: int


type SignInRefusal = NoList | UnknownOperator | Locked | WrongCode


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
                f"trop de codes faux : operateur verrouille encore "
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
        failures: Mapping[str, int] | None = None,
        note: Callable[[AccessNote], None] = lambda _note: None,
        verify: Callable[[str, ScryptCode], bool] = code_matches,
    ) -> None:
        """``held``: the list accepted before this start, or ``None``. ``lockout_path``:
        where the counts are kept; ``None``: in memory only (a test). ``failures``: the
        counts read from it (:func:`load_failures`); an operator found locked is locked
        for a whole :data:`LOCKOUT_PERIOD` from now. ``note``: a line for the logbook.
        ``verify``: whether a code is the hash's (:func:`code_matches`), run off the loop.
        """
        self._clock: Clock = clock
        self._list: OperatorList | None = held
        self._lockout_path: Path | None = lockout_path
        self._note: Callable[[AccessNote], None] = note
        self._verify: Callable[[str, ScryptCode], bool] = verify
        self._sessions: dict[str, SignedIn] = {}
        self._failures: dict[str, int] = dict(failures or {})
        now = clock.monotonic()
        self._locked_since: dict[str, Monotonic] = {
            user_id: now for user_id, count in self._failures.items() if count >= LOCKOUT_FAILURES
        }
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
        since = self._locked_since.get(user_id)
        if since is None:
            return None
        left = Seconds(LOCKOUT_PERIOD - elapsed(since, self._clock.monotonic()))
        return left if left > 0 else None

    def adopt(self, accepted: OperatorList) -> None:
        """Put a new list in force: a session whose operator or tablet it no longer
        holds ends. The caller verified and wrote it; this touches memory only."""
        self._list = accepted
        devices = {d.device_id for d in accepted.devices} | {LOCAL_DEVICE, LOOPBACK_DEVICE}
        for cookie, session in tuple(self._sessions.items()):
            if accepted.operator(session.user_id) is None or session.device_id not in devices:
                del self._sessions[cookie]
                self._note(AccessNote(SESSION_DROPPED, session.user_id, False))

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
            left = self.locked(user_id)
            if left is not None:
                return Err(Locked(retry_after=left))
            if user_id in self._locked_since:
                # The period is over: five more codes before the next lock.
                del self._locked_since[user_id]
                self._failures.pop(user_id, None)
            matches = await anyio.to_thread.run_sync(partial(self._verify, code, listed.code))
            if not matches:
                return Err(await self._failed(user_id, device_id))
            if self._failures.pop(user_id, None) is not None:
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

    async def _failed(self, user_id: str, device_id: str) -> SignInRefusal:
        count = self._failures.get(user_id, 0) + 1
        self._failures[user_id] = count
        await self._persist()
        if count >= LOCKOUT_FAILURES:
            self._locked_since[user_id] = self._clock.monotonic()
            self._note(
                AccessNote(
                    f"console: {user_id} verrouille {round(LOCKOUT_PERIOD / 60)} min "
                    f"apres {count} codes faux",
                    device_actor(device_id),
                    True,
                )
            )
            return Locked(retry_after=LOCKOUT_PERIOD)
        self._note(AccessNote(f"console: code faux pour {user_id}", device_actor(device_id), True))
        return WrongCode(remaining=LOCKOUT_FAILURES - count)

    async def _persist(self) -> None:
        path = self._lockout_path
        if path is None:
            return
        content = _STORED_LOCKOUT.dump_json(StoredLockout(failures=dict(self._failures))) + b"\n"
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
