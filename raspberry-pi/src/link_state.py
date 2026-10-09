"""The state of the link with the dashboard, as one word an operator can read.

The dashboard link (:mod:`src.cloud_sync`) already hears, at every exchange it
makes for its own reasons, how the link is doing: an answer, a silence, a key
that is not accepted, a contract that is not served. :class:`LinkHealth` keeps
what was last heard, and :meth:`LinkHealth.status` turns it into one of the
states of :class:`LinkState` whenever the console's page asks.

Nothing here asks the dashboard anything. No request is made for the
indicator, no timer runs for it, and nothing of it is read or written by the
control tick: the link writes from the dashboard task, the page's route reads,
both on the event loop, both in a few assignments.

What each state means
---------------------
* ``NOT_CONFIGURED``: this console has no machine key. It exchanges nothing.
* ``WAITING``: a key is set, the console has just started, and the dashboard
  has not answered yet. Not said to be unreachable before
  :data:`UNREACHABLE_AFTER`.
* ``REACHABLE``: the dashboard answers, takes this machine's key and serves
  this console's contract.
* ``KEY_REFUSED``: the dashboard answers and refuses this machine's key (401,
  403). Said at the first such answer: it is a statement, not a hiccup.
* ``INCOMPATIBLE``: the dashboard answers and is of another contract. Either it
  said so (426), or the console read it in the version a poll answer announces.
  Said at once, for the same reason.
* ``UNREACHABLE``: nothing usable has come back for :data:`UNREACHABLE_AFTER`.
* ``SERVER_ERROR``: the same, and what last came back was an error of the
  dashboard's own (5xx, or a request to wait).

Why a silence is not said at once
---------------------------------
One request lost or slow says nothing of the link: the next one, a few seconds
later, usually goes through. So a silence, or an error of the dashboard's own,
changes what the operator reads only once NOTHING usable has come back for
:data:`UNREACHABLE_AFTER`; until then the state stands and the age of the last
answer, which the page shows next to it, says how old it is. The way back is
immediate: the first answer makes the link reachable again.

A statement that stands until it is contradicted
------------------------------------------------
A refused key and a refused contract are cleared by what proves the contrary,
and by nothing else:

* any answer that is not about the key (a success, or a refusal of what was
  sent) shows the key is accepted;
* a success, or a refusal of what was sent, on a route the dashboard serves
  only to a console whose major it serves shows the contract is served. The
  status route answers under every contract (a stop must cross them all), so
  its answers prove nothing of the contract: without this, a session running
  under a refused contract would read reachable every three seconds;
* a contract the console itself read in a poll answer is cleared by the next
  poll answer of its own major. An older dashboard accepts everything this
  console sends and names no version: its successes are no proof either.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, assert_never

from src.units import Monotonic, Seconds, elapsed

UNREACHABLE_AFTER: Final[Seconds] = Seconds(25.0)
"""How long the dashboard may answer nothing usable before the link reads unreachable.

Two and a half heartbeat periods (:data:`~src.cloud_sync.HEARTBEAT_PERIOD`, the
slowest exchange the link always makes): one heartbeat lost or slow never
changes what the operator reads, two in a row do. Well inside the 90 s after
which the dashboard itself shows the machine offline.
"""

MAX_DETAIL: Final[int] = 160
"""The most characters of a reason the page is given. Some of it is the dashboard's own words."""

NO_EXCHANGE: Final[str] = "aucun echange avec le tableau de bord n'a abouti"
"""The reason of a silence in which no request was even seen to fail."""

NO_KEY: Final[str] = "aucune cle de machine (MACHINE_API_KEY) : rien n'est echange"
"""The reason of a link that is not configured."""


@unique
class LinkState(Enum):
    """What the link with the dashboard is, for the operator. Wire strings."""

    NOT_CONFIGURED = "not_configured"
    WAITING = "waiting"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    INCOMPATIBLE = "incompatible"
    KEY_REFUSED = "key_refused"
    SERVER_ERROR = "server_error"


def label_of(state: LinkState) -> str:  # noqa: PLR0911  # one return per state
    """The word the console's page shows for ``state``. Exhaustive."""
    match state:
        case LinkState.NOT_CONFIGURED:
            return "non configure"
        case LinkState.WAITING:
            return "en attente"
        case LinkState.REACHABLE:
            return "joignable"
        case LinkState.UNREACHABLE:
            return "injoignable"
        case LinkState.INCOMPATIBLE:
            return "incompatible"
        case LinkState.KEY_REFUSED:
            return "cle refusee"
        case LinkState.SERVER_ERROR:
            return "en erreur"
    raise assert_never(state)


def bounded(text: str) -> str:
    """``text`` on one line, at most :data:`MAX_DETAIL` characters, nothing unprintable.

    What a reason goes through before it is kept: part of it may be words the
    dashboard sent, or the text of a network error, and the page is given
    neither a wall of text nor a control character.
    """
    line = " ".join("".join(char if char.isprintable() else " " for char in text).split())
    return line if len(line) <= MAX_DETAIL else f"{line[: MAX_DETAIL - 3]}..."


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """The link with the dashboard at one instant, as the console's page shows it."""

    state: LinkState
    detail: str
    """Why, in a few words; one line of at most :data:`MAX_DETAIL` characters. May be empty."""

    last_answer_age: Seconds | None
    """How long ago the dashboard last answered; ``None`` when it never has."""


NOT_CONFIGURED: Final[LinkStatus] = LinkStatus(LinkState.NOT_CONFIGURED, NO_KEY, None)
"""The link of a console that has no machine key."""


@dataclass(frozen=True, slots=True)
class _Trouble:
    """The last exchange that brought nothing usable back, and which kind it was."""

    state: LinkState
    detail: str


class LinkHealth:
    """What the link's own exchanges last said of it. See the module docstring.

    Mutable on purpose: it is the memory of the link. Written by the dashboard
    link alone, read by the console's page; both run on the event loop, so no
    reader ever sees a half-written state.
    """

    __slots__ = ("_born", "_foreign", "_heard", "_key", "_trouble", "_unsupported")

    def __init__(self, born: Monotonic) -> None:
        """``born``: when the console started; a link is not unreachable before it was tried."""
        self._born: Monotonic = born
        self._heard: Monotonic | None = None
        self._trouble: _Trouble | None = None
        self._key: str | None = None
        self._unsupported: str | None = None
        self._foreign: str | None = None

    # --- what the link tells it ------------------------------------------

    def answered(self, now: Monotonic, *, contract: bool) -> None:
        """The dashboard answered, and not about the key: a success, or a refusal of what was sent.

        ``contract``: the route is one the dashboard serves only to a console
        whose major it serves, so this answer shows the contract is served.
        """
        self._hear(now)
        self._key = None
        if contract:
            self._unsupported = None

    def key_refused(self, now: Monotonic, detail: str) -> None:
        """The dashboard answered that it does not accept this machine's key."""
        self._hear(now)
        self._key = bounded(detail)

    def contract_refused(self, now: Monotonic, sentence: str) -> None:
        """The dashboard answered that it does not serve this console's contract (426)."""
        self._hear(now)
        self._unsupported = bounded(sentence)

    def announced(self, refusal: str | None) -> None:
        """What a poll answer says of the dashboard's contract.

        ``refusal``: why the console takes nothing from it
        (:func:`~src.contract.server_refusal`), or ``None`` when it is of this
        console's major.
        """
        self._foreign = None if refusal is None else bounded(refusal)

    def silent(self, detail: str) -> None:
        """An exchange brought nothing back: no network, a timeout, a reply that is no answer."""
        self._trouble = _Trouble(LinkState.UNREACHABLE, bounded(detail))

    def errored(self, detail: str) -> None:
        """The dashboard answered with an error of its own, or asked to wait."""
        self._trouble = _Trouble(LinkState.SERVER_ERROR, bounded(detail))

    def _hear(self, now: Monotonic) -> None:
        self._heard = now
        self._trouble = None

    # --- what the page reads ---------------------------------------------

    def status(self, now: Monotonic) -> LinkStatus:
        """The state of the link at ``now``. Reads only; computed from what was last heard."""
        heard = self._heard
        trouble = self._trouble
        age = None if heard is None else elapsed(heard, now)
        if elapsed(self._born if heard is None else heard, now) >= UNREACHABLE_AFTER:
            if trouble is None:
                return LinkStatus(LinkState.UNREACHABLE, NO_EXCHANGE, age)
            return LinkStatus(trouble.state, trouble.detail, age)
        if heard is None:
            return LinkStatus(LinkState.WAITING, "" if trouble is None else trouble.detail, None)
        if self._key is not None:
            return LinkStatus(LinkState.KEY_REFUSED, self._key, age)
        incompatible = self._foreign if self._unsupported is None else self._unsupported
        if incompatible is not None:
            return LinkStatus(LinkState.INCOMPATIBLE, incompatible, age)
        return LinkStatus(LinkState.REACHABLE, "", age)
