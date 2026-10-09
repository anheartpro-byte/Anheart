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

Two rules decide everything below.

The state is what the routes that carry the session last said
-------------------------------------------------------------
The heartbeat, the launches and the sending of the sessions go through routes
the dashboard serves only to a console whose key it accepts and whose contract
it serves. What the operator reads is what THOSE routes last said, for as long
as they keep saying it:

* ``REACHABLE``: one of them answered in the dashboard's own shape and took
  what was sent: a success, or the refusal of one request under one of the
  dashboard's stable codes. Nothing else ever reads reachable;
* ``KEY_REFUSED``: one of them refused this machine's key (401, 403);
* ``INCOMPATIBLE``: one of them refused this console's contract (426 with its
  stable code), or a poll answer announced another major, which stands until
  the next poll answer says otherwise;
* ``WAITING``: none of them has said anything yet, and the console started
  less than :data:`UNREACHABLE_AFTER` ago.

A refused key and a refused contract are said at the first answer that states
them: they are statements, not hiccups. The latest statement is the one read:
the dashboard checks the key before the contract, so a 426 after a 401 means
the key is now taken.

The stop question is the one route outside this rule. The dashboard answers
it under every contract (a stop must cross them all), every three seconds
while a session runs. Its answers say the dashboard is there, and so keep the
age of the last answer fresh; they change nothing of the state. They are no
proof that the contract is served, and no proof that anything the console
sends is taken.

What is not the dashboard's answer proves nothing
-------------------------------------------------
A silence, an error of the dashboard's own (5xx, a request to wait), and an
answer that is not in the dashboard's shape (a 404 or a 400 with no stable
code, a page of HTML) never make the link read reachable, and never unsay a
refusal. They change what is read only once the routes that carry the session
have said nothing recognisable for :data:`UNREACHABLE_AFTER`: one request lost
or slow does not show, two heartbeats in a row do. Then:

* ``SERVER_ERROR`` when the dashboard still answers something (the stop
  question, or errors of its own): it is there, and takes nothing;
* ``UNREACHABLE`` when nothing recognisable comes back at all.

Either way with the reason of the last exchange that failed. The way back is
immediate: the first answer that takes what was sent reads reachable again.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, assert_never

from src.units import Monotonic, Seconds, elapsed

UNREACHABLE_AFTER: Final[Seconds] = Seconds(25.0)
"""How long the routes that carry the session may say nothing recognisable before the
state they last gave no longer stands.

Two and a half heartbeat periods (:data:`~src.cloud_sync.HEARTBEAT_PERIOD`, the
slowest exchange the link always makes): one heartbeat lost or slow never
changes what the operator reads, two in a row do. Well inside the 90 s after
which the dashboard itself shows the machine offline.
"""

MAX_DETAIL: Final[int] = 160
"""The most characters of a reason the page is given. Some of it is the dashboard's own words."""

NO_EXCHANGE: Final[str] = "aucun envoi au tableau de bord n'a abouti"
"""The reason of a silence in which no request was even seen to fail."""

NO_KEY: Final[str] = "aucune cle de machine (MACHINE_API_KEY) : rien n'est echange"
"""The reason of a link that is not configured."""

NOT_THE_DASHBOARD: Final[str] = "pas une reponse du tableau de bord"
"""What the reason says of an answer that is not in the dashboard's own shape."""


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
    """How long ago the dashboard last answered anything in its own shape, the stop
    question included; ``None`` when it never has."""


NOT_CONFIGURED: Final[LinkStatus] = LinkStatus(LinkState.NOT_CONFIGURED, NO_KEY, None)
"""The link of a console that has no machine key."""


@dataclass(frozen=True, slots=True)
class _Said:
    """The last thing a route that carries the session said in the dashboard's own shape."""

    at: Monotonic
    state: LinkState
    """``REACHABLE``, ``KEY_REFUSED`` or ``INCOMPATIBLE``: what that answer states."""

    detail: str


@dataclass(frozen=True, slots=True)
class _Trouble:
    """The last exchange on those routes that brought nothing recognisable back, since then."""

    state: LinkState
    """``UNREACHABLE`` or ``SERVER_ERROR``: what it reads once nothing else answers either."""

    detail: str


class LinkHealth:
    """What the link's own exchanges last said of it. See the module docstring.

    Mutable on purpose: it is the memory of the link. Written by the dashboard
    link alone, read by the console's page; both run on the event loop, so no
    reader ever sees a half-written state.

    Every entry but :meth:`announced` takes ``contract``: whether the exchange
    was made on a route that carries the heartbeat or the session, which the
    dashboard serves only under this console's contract. It is ``False`` for
    the stop question alone, whose answers keep the last answer's age fresh
    and change nothing else.
    """

    __slots__ = ("_born", "_foreign", "_heard", "_said", "_trouble")

    def __init__(self, born: Monotonic) -> None:
        """``born``: when the console started; a link is not unreachable before it was tried."""
        self._born: Monotonic = born
        self._heard: Monotonic | None = None
        self._said: _Said | None = None
        self._foreign: str | None = None
        self._trouble: _Trouble | None = None

    # --- what the link tells it ------------------------------------------

    def answered(self, now: Monotonic, *, contract: bool) -> None:
        """The dashboard took what was sent: a success, or one request refused under a
        stable code of its own."""
        self._hear(now, LinkState.REACHABLE, "", contract=contract)

    def key_refused(self, now: Monotonic, detail: str, *, contract: bool) -> None:
        """An answer that this machine's key is not accepted (401, 403)."""
        self._hear(now, LinkState.KEY_REFUSED, bounded(detail), contract=contract)

    def contract_refused(self, now: Monotonic, sentence: str, *, contract: bool) -> None:
        """The dashboard answered that it does not serve this console's contract (426)."""
        self._hear(now, LinkState.INCOMPATIBLE, bounded(sentence), contract=contract)

    def announced(self, refusal: str | None) -> None:
        """What a poll answer says of the dashboard's contract.

        ``refusal``: why the console takes nothing from it
        (:func:`~src.contract.server_refusal`), or ``None`` when it is of this
        console's major.
        """
        self._foreign = None if refusal is None else bounded(refusal)

    def silent(self, detail: str, *, contract: bool) -> None:
        """An exchange brought nothing back: no network, a timeout, a reply that is no answer."""
        self._fail(LinkState.UNREACHABLE, detail, contract=contract)

    def errored(self, detail: str, *, contract: bool) -> None:
        """The dashboard answered with an error of its own, or asked to wait."""
        self._fail(LinkState.SERVER_ERROR, detail, contract=contract)

    def unrecognised(self, detail: str, *, contract: bool) -> None:
        """An answer that is not in the dashboard's shape: it proves nothing, from anybody."""
        self._fail(LinkState.UNREACHABLE, detail, contract=contract)

    def _hear(self, now: Monotonic, state: LinkState, detail: str, *, contract: bool) -> None:
        self._heard = now
        if contract:
            self._said = _Said(now, state, detail)
            self._trouble = None

    def _fail(self, state: LinkState, detail: str, *, contract: bool) -> None:
        if contract:
            self._trouble = _Trouble(state, bounded(detail))

    # --- what the page reads ---------------------------------------------

    def status(self, now: Monotonic) -> LinkStatus:
        """The state of the link at ``now``. Reads only; computed from what was last heard."""
        heard = self._heard
        said = self._said
        trouble = self._trouble
        age = None if heard is None else elapsed(heard, now)
        if elapsed(self._born if said is None else said.at, now) < UNREACHABLE_AFTER:
            # What the routes that carry the session last said still stands.
            if said is None:
                return LinkStatus(LinkState.WAITING, "" if trouble is None else trouble.detail, age)
            if said.state is LinkState.REACHABLE and self._foreign is not None:
                return LinkStatus(LinkState.INCOMPATIBLE, self._foreign, age)
            return LinkStatus(said.state, said.detail, age)
        # They have said nothing recognisable for too long.
        detail = NO_EXCHANGE if trouble is None else trouble.detail
        if age is not None and age < UNREACHABLE_AFTER:
            # Something of the dashboard still answers: it is there, and takes nothing.
            return LinkStatus(LinkState.SERVER_ERROR, detail, age)
        return LinkStatus(LinkState.UNREACHABLE if trouble is None else trouble.state, detail, age)
