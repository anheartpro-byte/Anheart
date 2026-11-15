"""Pairing a console that has no key yet: the first-power-up screen (ANH-115, contract 1.17).

An admin issues a one-time code on the site for the machine whose registry names this
Pi's serial number. The code typed on the console's page goes, with that serial
number, to ``POST /api/machine/pair`` on the dashboard (``machine_pairing`` of
``contracts/machine-api.json``). The answer is the machine's key, its identifier and
the deployment's address: they are written to ``data/machine.env``, and the console
restarts at rest, linked.

Why not the ``.env``. On an installed Pi it is ``/etc/anheart/anheart.env``: root's,
mode 600, and it carries the limits the console applies (``MOTOR_MAX_RPM``,
``PROGRAMS_ENABLED``, the cardiac tiers). The console must never be able to write
it. ``data/machine.env`` belongs to the console's account, mode 600, and holds three
keys and nothing else: :data:`PAIRED_KEYS`. It is read at startup with a strict
parser (:func:`parse_paired`): a file that holds anything else is ignored, so it can
never set a safety key. Each of the three keys the ``.env`` sets wins over the file's
(:func:`paired_environment`). Keeping the key there is acceptable: the console
process holds and uses its key anyway, in memory, for every request it sends.

Two steps of the commissioning are not this console's yet: the enrolment with the
update service (ANH-169) and the console's own network (ANH-151). They are named on
the screen as steps still to do (:data:`SETUP_STEPS`); each is the function its
ticket fills in.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Final, assert_never, cast

import httpx

from src.console_access import replace_file
from src.contract import CONTRACT_HEADER, CONTRACT_VERSION
from src.kept_file import read_kept
from src.local_config import (
    KEY_CONVEX_URL,
    KEY_MACHINE_API_KEY,
    KEY_RECORD_MACHINE_ID,
    RECORD_IDENTIFIER,
    UNASSIGNED,
)
from src.pairing_view import PairingDone, PairingView, PendingStep
from src.record.writer import describe_os_error
from src.result import Err, Ok, Result

_logger = logging.getLogger(__name__)

MACHINE_ENV_FILE: Final[str] = "machine.env"
"""The file a pairing writes, next to the console's other data (``data/``)."""

PAIRED_KEYS: Final[tuple[str, str, str]] = (
    KEY_MACHINE_API_KEY,
    KEY_CONVEX_URL,
    KEY_RECORD_MACHINE_ID,
)
"""The keys of :data:`MACHINE_ENV_FILE`, in the order they are written: all three, and no other."""

SERIAL_PATH: Final[Path] = Path("/proc/device-tree/serial-number")
"""Where a Raspberry Pi's serial number is read: what the site's registry names."""

PAIR_ROUTE: Final[str] = "/api/machine/pair"

API_KEY: Final[re.Pattern[str]] = re.compile(r"anh1\.[0-9a-f]{32}\.[0-9a-f]{64}")
"""A machine key as the dashboard issues it: ``anh1.<selector>.<secret>``, in hexadecimal."""

KEY_PREFIX: Final[re.Pattern[str]] = re.compile(r"anh1\.[0-9a-f]{8}")
"""What the dashboard shows of a key (``keyPrefix`` of convex/lib/machineKeys.ts): ``anh1.``
and the first 8 characters of its selector, never more of the key."""

SERIAL: Final[re.Pattern[str]] = re.compile(r"[0-9a-z][0-9a-z._:-]{0,63}")
"""A serial number as the dashboard compares it (``normalizedSerial`` of
convex/lib/machinePairing.ts)."""

MAX_CODE_CHARS: Final[int] = 32
"""Longer than any code typed, spaces and dashes included: anything longer is not one."""

MAX_FILE_BYTES: Final[int] = 1024
"""Far above the three lines a pairing writes: a larger file is not one."""

PAIR_TIMEOUT_S: Final[float] = 10.0

_LINE: Final[re.Pattern[str]] = re.compile(
    r"(MACHINE_API_KEY|CONVEX_URL|RECORD_MACHINE_ID)=([^\s'\"`$\\#]+)"
)
"""One line of the file: a key of :data:`PAIRED_KEYS`, ``=``, a value with no space, no
quote and nothing a shell or a dotenv reader would expand."""


@dataclass(frozen=True, slots=True)
class PairedLink:
    """What a pairing gave: the machine's key, the deployment's address, the machine."""

    api_key: str
    convex_url: str
    machine_id: str

    def values(self) -> Mapping[str, str]:
        """The three keys of the file, by name."""
        return {
            KEY_MACHINE_API_KEY: self.api_key,
            KEY_CONVEX_URL: self.convex_url,
            KEY_RECORD_MACHINE_ID: self.machine_id,
        }


def https_origin(url: str) -> str | None:
    """``url`` as the origin it names (``https://host[:port]``), or ``None``.

    Only an https address with no path, query or credentials is one: the console never
    sends its key, nor a code, in clear, nor anywhere but the deployment's root.
    """
    try:
        parsed = httpx.URL(url.strip().rstrip("/"))
    except httpx.InvalidURL:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.host
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.userinfo
    ):
        return None
    # The host as it goes on the wire: an internationalised name encoded, IPv6 in brackets.
    host = parsed.raw_host.decode("ascii")
    named = f"[{host}]" if ":" in host else host
    port = "" if parsed.port is None else f":{parsed.port}"
    return f"https://{named}{port}"


def read_serial(path: Path = SERIAL_PATH) -> Result[str, str]:
    """This Pi's serial number as the dashboard compares it, or why it cannot be read."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        return Err(f"illisible ({describe_os_error(error)})")
    serial = raw.replace(b"\x00", b"").decode("ascii", errors="replace").strip().lower()
    if SERIAL.fullmatch(serial) is None:
        return Err("mal forme")
    return Ok(serial)


def parse_paired(text: str) -> Result[PairedLink, str]:  # noqa: PLR0911  # one per refusal
    """The three keys of :data:`MACHINE_ENV_FILE`, or why the file is not one.

    Exactly three lines, one per key of :data:`PAIRED_KEYS`, each ``KEY=value`` with no
    quote, no space and nothing to expand; a key well formed, an https origin, an
    identifier. The reason is fixed words and line numbers, never a value: the file
    holds the machine's key.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if len(lines) != len(PAIRED_KEYS):
        return Err(f"{len(PAIRED_KEYS)} lignes attendues, {len(lines)} lues")
    values: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        match = _LINE.fullmatch(line)
        if match is None:
            return Err(f"ligne {number} illisible")
        key, value = match.group(1), match.group(2)
        if key in values:
            return Err(f"ligne {number} en double")
        values[key] = value
    if API_KEY.fullmatch(values[KEY_MACHINE_API_KEY]) is None:
        return Err("MACHINE_API_KEY mal formee")
    url = values[KEY_CONVEX_URL]
    if https_origin(url) != url:
        return Err("CONVEX_URL : une adresse https sans chemin attendue")
    if RECORD_IDENTIFIER.fullmatch(values[KEY_RECORD_MACHINE_ID]) is None:
        return Err("RECORD_MACHINE_ID mal forme")
    return Ok(
        PairedLink(
            api_key=values[KEY_MACHINE_API_KEY],
            convex_url=url,
            machine_id=values[KEY_RECORD_MACHINE_ID],
        )
    )


def render_paired(link: PairedLink) -> bytes:
    """The file's text: one line per key, in :data:`PAIRED_KEYS` order."""
    values = link.values()
    return "".join(f"{key}={values[key]}\n" for key in PAIRED_KEYS).encode("ascii")


def read_paired(path: Path) -> Result[PairedLink | None, str]:
    """The file a pairing wrote, ``None`` when there is none, or why it is not read.

    Read as every file the console keeps is (:func:`~src.kept_file.read_kept`):
    opened without following a link and without waiting, then judged on what was opened,
    a regular file of at most :data:`MAX_FILE_BYTES`. Then ASCII, and the three keys of
    :func:`parse_paired`. Never raises.
    """
    try:
        raw = read_kept(path, MAX_FILE_BYTES)
    except FileNotFoundError:
        return Ok(None)
    except OSError as error:
        return Err(describe_os_error(error))
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return Err("pas du texte ASCII")
    parsed = parse_paired(text)
    if isinstance(parsed, Err):
        return parsed
    return Ok(parsed.value)


def write_paired(path: Path, link: PairedLink) -> Result[None, str]:
    """Write the file as every file the console keeps is
    (:func:`~src.console_access.replace_file`): a private temporary file, flushed, renamed
    over ``path``, then its directory flushed. A link at ``path`` is refused. Never raises:
    a key the dashboard issued and the console then lost to an exception would be lost
    for good."""
    try:
        if path.is_symlink():
            return Err("un lien est a la place du fichier")
        return replace_file(path, render_paired(link))
    except (OSError, ValueError) as error:
        return Err(describe_os_error(error) if isinstance(error, OSError) else type(error).__name__)


def _given(env: Mapping[str, str], key: str) -> str | None:
    """The ``.env``'s value of ``key``, or ``None`` when it sets none (``unassigned`` is none)."""
    value = env.get(key, "").strip()
    return None if value in ("", UNASSIGNED) else value


@unique
class LeftOut(Enum):
    """Why the file is left out whole. The words are fixed here, never a value of the file
    or of the ``.env``: they are logged, and the file holds the machine's key."""

    ENV_PAIRED = "the .env sets MACHINE_API_KEY"
    OTHER_MACHINE = "the .env names another RECORD_MACHINE_ID"
    OTHER_ADDRESS = "the .env sets another CONVEX_URL"


def paired_environment(
    env: Mapping[str, str], link: PairedLink | None
) -> tuple[Mapping[str, str], LeftOut | None]:
    """``env`` with the file's three keys, or ``env`` alone and why the file is left out.

    The three together or none: a key of one machine is never sent with the identifier
    of another, nor to another deployment. The ``.env`` wins whenever it disagrees: it
    sets a key of its own, names another machine, or another address.
    """
    if link is None:
        return env, None
    if _given(env, KEY_MACHINE_API_KEY) is not None:
        return env, LeftOut.ENV_PAIRED
    machine = _given(env, KEY_RECORD_MACHINE_ID)
    if machine is not None and machine != link.machine_id:
        return env, LeftOut.OTHER_MACHINE
    url = _given(env, KEY_CONVEX_URL)
    if url is not None and https_origin(url) != link.convex_url:
        return env, LeftOut.OTHER_ADDRESS
    return {**env, **link.values()}, None


def startup_environment(env: Mapping[str, str], path: Path) -> Mapping[str, str]:
    """The environment the console starts on: ``env`` and what a pairing wrote at ``path``.

    A file that is not read is said, and the console starts on ``env`` alone: unpaired,
    unless the ``.env`` gives it a key. What is said is fixed words, an errno's name and
    line numbers (:func:`read_paired`, :class:`LeftOut`): never a value.
    """
    read = read_paired(path)
    if isinstance(read, Err):
        _logger.error(
            "pairing: data/%s ignored (%s): the console starts unpaired unless its .env "
            "holds a key",
            MACHINE_ENV_FILE,
            read.error,
        )
        return env
    merged, left_out = paired_environment(env, read.value)
    if left_out is not None:
        _logger.warning(
            "pairing: data/%s not used (%s): the console starts on its .env alone",
            MACHINE_ENV_FILE,
            left_out.value,
        )
    return merged


# --- the call ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Paired:
    """The dashboard's answer to a code it accepted."""

    machine_id: str
    api_key: str
    key_prefix: str
    convex_url: str


@dataclass(frozen=True, slots=True)
class PairingRefused:
    """401 ``pairing_refused``: no open code of this Pi matches what was typed."""


@dataclass(frozen=True, slots=True)
class PairingThrottled:
    """429: too many pairing requests on the dashboard, from anyone."""


@dataclass(frozen=True, slots=True)
class PairingUnreachable:
    """No answer: the network, the address, a timeout."""

    detail: str


@dataclass(frozen=True, slots=True)
class PairingUnusable:
    """An answer the console cannot take: nothing of it is written."""

    detail: str


type PairingError = PairingRefused | PairingThrottled | PairingUnreachable | PairingUnusable


def describe_pairing_error(error: PairingError) -> str:
    """What the screen says of a failed pairing. Never the code typed."""
    match error:
        case PairingRefused():
            return (
                "code refuse : aucun code en cours pour ce Pi ne correspond (faux, expire, "
                "deja servi, remplace ou revoque) ; en demander un nouveau sur la fiche de la "
                "machine"
            )
        case PairingThrottled():
            return "trop de demandes d'appairage : reessayer dans une minute"
        case PairingUnreachable():
            return f"tableau de bord injoignable ({error.detail}) : verifier le reseau et l'adresse"
        case PairingUnusable():
            return f"reponse inattendue du tableau de bord ({error.detail}) : rien n'est ecrit"
    raise assert_never(error)


type Document = Mapping[str, object]


def _document(text: str) -> Document | None:
    """The one place an untyped value enters: annotated ``object`` at once, then narrowed."""
    try:
        parsed: object = json.loads(text)  # pyright: ignore[reportAny]  # narrowed below
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    # JSON object keys are strings by construction.
    return cast("Document", parsed)


def _text_field(body: Document, name: str) -> str | None:
    value = body.get(name)
    return value if isinstance(value, str) else None


def _paired_of(fields: Document | None, origin: str) -> Result[Paired, PairingError]:
    """The answer of a 200, checked field by field. ``origin``: where it was asked."""
    if fields is None:
        return Err(PairingUnusable("pas un objet JSON"))
    machine_id = _text_field(fields, "machineId")
    api_key = _text_field(fields, "apiKey")
    key_prefix = _text_field(fields, "keyPrefix")
    convex_url = _text_field(fields, "convexUrl")
    if machine_id is None or RECORD_IDENTIFIER.fullmatch(machine_id) is None:
        return Err(PairingUnusable("identifiant de machine"))
    if api_key is None or API_KEY.fullmatch(api_key) is None:
        return Err(PairingUnusable("cle"))
    if (
        key_prefix is None
        or KEY_PREFIX.fullmatch(key_prefix) is None
        or not api_key.startswith(key_prefix)
    ):
        return Err(PairingUnusable("prefixe de la cle"))
    if convex_url is None or https_origin(convex_url) != origin:
        return Err(PairingUnusable("adresse : pas celle qui a repondu"))
    return Ok(Paired(machine_id, api_key, key_prefix, origin))


async def request_pairing(
    client: httpx.AsyncClient, origin: str, code: str, serial: str
) -> Result[Paired, PairingError]:
    """Ask the dashboard at ``origin`` for this Pi's key with ``code``. Never raises."""
    try:
        response = await client.post(
            f"{origin}{PAIR_ROUTE}",
            json={"code": code, "serialPi": serial},
            headers={CONTRACT_HEADER: CONTRACT_VERSION},
        )
    except httpx.HTTPError as error:
        return Err(PairingUnreachable(type(error).__name__))
    body = _document(response.text)
    status = response.status_code
    if status == httpx.codes.OK:
        return _paired_of(body, origin)
    said = None if body is None else body.get("error")
    if status == httpx.codes.UNAUTHORIZED and said == "pairing_refused":
        return Err(PairingRefused())
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return Err(PairingThrottled())
    return Err(PairingUnusable(f"statut {status}"))


# --- the steps the console cannot do yet ------------------------------------------------


def enrol_ota() -> PendingStep:
    """The enrolment with the update service: ANH-169 fills it in."""
    return PendingStep(
        "ota",
        "Inscription au service de mise a jour",
        "pas encore faite par la console (ANH-169)",
    )


def console_network() -> PendingStep:
    """The console's own network and its password: ANH-151 fills it in."""
    return PendingStep(
        "network",
        "Reseau de la console",
        "pas encore cree par la console : son mot de passe s'affichera ici (ANH-151)",
    )


SETUP_STEPS: Final[tuple[Callable[[], PendingStep], ...]] = (enrol_ota, console_network)
"""What follows a pairing, in order: each the seam its ticket replaces."""


# --- the screen's side ------------------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(PAIR_TIMEOUT_S))


@dataclass(frozen=True, slots=True)
class PairingSetup:
    """What a console with no key pairs with: where it writes, which Pi it is, where it asks."""

    path: Path
    serial_path: Path = SERIAL_PATH
    address: str | None = None
    """The ``.env``'s ``CONVEX_URL``, when it is an https origin: the only address a pairing
    asks. ``None``: no pairing (:data:`NO_ADDRESS`)."""

    machine_id: str | None = None
    """The ``.env``'s ``RECORD_MACHINE_ID``: a code of another machine is refused."""

    client: Callable[[], httpx.AsyncClient] = _client

    @classmethod
    def of(cls, env: Mapping[str, str], path: Path) -> PairingSetup:
        """The setup the ``.env`` gives: its address and its machine, if it sets them."""
        given = _given(env, KEY_CONVEX_URL)
        return cls(
            path=path,
            address=None if given is None else https_origin(given),
            machine_id=_given(env, KEY_RECORD_MACHINE_ID),
        )


ALREADY_PAIRED: Final[str] = "cette console vient d'etre appairee : elle redemarre au repos"
NO_ADDRESS: Final[str] = (
    "adresse du tableau de bord non configuree : la poser dans /etc/anheart/anheart.env "
    "(CONVEX_URL) puis redemarrer"
)
"""Why a console whose root ``.env`` sets no https ``CONVEX_URL`` offers no pairing: an
address typed on the screen could be any server, ``.convex.site`` included, which every
customer of Convex shares."""
PAIRING_BUSY: Final[str] = "un appairage est deja en cours"


class PairingDesk:
    """The first-power-up screen of a console with no key: one pairing, then a restart.

    Mutable on the console's one event loop: whether a pairing is under way, and the one
    done. Built only when the console has no key (:func:`~src.local_panel.build_panel`).
    """

    __slots__ = ("_busy", "_done", "_restart", "_serial", "_setup")

    def __init__(self, setup: PairingSetup) -> None:
        self._setup: Final[PairingSetup] = setup
        self._serial: Final[Result[str, str]] = read_serial(setup.serial_path)
        self._busy: bool = False
        self._done: Paired | None = None
        self._restart: Callable[[], None] = lambda: None

    def bind_restart(self, restart: Callable[[], None]) -> None:
        """The console's controlled restart, at rest, taken once a pairing is written."""
        self._restart = restart

    def _steps(self) -> tuple[PendingStep, ...]:
        return tuple(step() for step in SETUP_STEPS)

    def view(self) -> PairingView:
        serial = self._serial
        return PairingView(
            serial=serial.value if isinstance(serial, Ok) else None,
            serial_problem=serial.error if isinstance(serial, Err) else None,
            address=self._setup.address,
            key_prefix=None if self._done is None else self._done.key_prefix,
            steps=self._steps(),
        )

    async def pair(self, code: str) -> Result[PairingDone, str]:  # noqa: PLR0911  # one per refusal
        """Send ``code``, write what the dashboard gives, ask for the restart. Or why not."""
        if self._done is not None:
            return Err(ALREADY_PAIRED)
        if self._busy:
            return Err(PAIRING_BUSY)
        serial = self._serial
        if isinstance(serial, Err):
            return Err(f"numero de serie du Pi {serial.error} : aucun appairage possible")
        origin = self._setup.address
        if origin is None:
            return Err(NO_ADDRESS)
        typed = code.strip()
        if not typed or len(typed) > MAX_CODE_CHARS:
            return Err("code d'appairage attendu : les 8 caracteres affiches sur le site")
        self._busy = True
        try:
            async with self._setup.client() as client:
                answered = await request_pairing(client, origin, typed, serial.value)
        finally:
            self._busy = False
        if isinstance(answered, Err):
            return Err(describe_pairing_error(answered.error))
        paired = answered.value
        expected = self._setup.machine_id
        if expected is not None and expected != paired.machine_id:
            return Err(
                f"le code est celui de la machine {paired.machine_id}, et cette console est "
                f"reglee pour {expected} ({KEY_RECORD_MACHINE_ID}) : rien n'est ecrit ; corriger "
                "le .env puis demander un nouveau code"
            )
        written = write_paired(
            self._setup.path, PairedLink(paired.api_key, paired.convex_url, paired.machine_id)
        )
        if isinstance(written, Err):
            return Err(
                f"la cle recue n'a pas pu etre ecrite ({written.error}) : en demander un "
                "nouveau code"
            )
        self._done = paired
        self._restart()
        return Ok(PairingDone(paired.key_prefix, self._steps()))
