"""The rider's feedback after a session with a person on board (ANH-146), at the console.

When a session with somebody on board ends - a manual session declared
occupied, or any programmed session - the console offers the operator a card:
the rider's nausea and discomfort or dizziness, each a whole number from 0 to
10, a comfort, and a comment if any. The operator enters it, or skips it ("non
renseigne"): nothing is ever asked for a bench session.

Where it goes, and where it never goes
--------------------------------------
It is health data. It is held **in memory only**, by :class:`PassengerFeedback`,
until the dashboard has it: never in the session's record, never on disk,
never in a log line, never in an error. A console that restarts before it was
sent has lost it, and says nothing of it: the rider may still give it on the
site, within the same 24 hours.

It goes to the dashboard as the one event of kind ``feedback`` of a request to
the events route (contract 1.7, ``feedback_events`` of
``contracts/machine-api.json``), which the dashboard stores apart from the
session's events. Only to a dashboard whose last heartbeat announced 1.7 or a
later minor of this major (:func:`~src.contract.takes_feedback`): an earlier
one would keep it with the session's events. It waits, in memory, for the
session's identifier on the dashboard (a session started at the machine has
one once declared), for the link, and for a dashboard that takes it; it is
given up 24 hours after the end of the session, when the dashboard would
refuse it anyway, and when more than :data:`MAX_HELD` wait. Each of those
states is said on the card.

The scale and its labels are those of ``contracts/feedback.json``, pinned here
by ``tests/test_passenger_feedback.py``: the console is deployed without that
file.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from abc import abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, NewType, Protocol, assert_never, cast

from src.clock import Clock
from src.contract import ContractVersion, ErrorCode, takes_feedback
from src.record_uplink import Acked, Answer, Held, Refusal, RouteMissing
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.units import Monotonic, Seconds, UnixMillis, elapsed

_logger: logging.Logger = logging.getLogger(__name__)

Score = NewType("Score", int)
"""A whole number of one of the two scales, :data:`SCALE_MIN` to :data:`SCALE_MAX`."""

SCALE_MIN: Final[int] = 0
SCALE_MAX: Final[int] = 10

FREE_TEXT_MAX: Final[int] = 500
"""Characters of comment at most, once trimmed, counted as the dashboard and the page
count them: in UTF-16 code units."""

EDIT_WINDOW: Final[Seconds] = Seconds(24 * 60 * 60)
"""How long after the end of its session a feedback may still reach the dashboard."""

RETRY_PERIOD: Final[Seconds] = Seconds(15.0)
"""The wait before a feedback the link did not deliver is sent again."""

MAX_HELD: Final[int] = 8
"""Sessions whose feedback this console holds at most. Beyond, the oldest is given up."""

FEEDBACK_KIND: Final[str] = "feedback"
OPERATOR_ACTOR: Final[str] = "operator"
"""Who entered it, as the event says: the console's access, not a named person (ANH-152)."""

FIELDS: Final[frozenset[str]] = frozenset(
    {"nausea_score", "dizziness_score", "comfort", "free_text"}
)
"""The keys of a feedback the page sends, and no other."""

_ALLOWED_CONTROLS: Final[frozenset[str]] = frozenset({"\n", "\t"})

SESSION_NOT_ENDED: Final[ErrorCode] = ErrorCode("feedback_session_not_ended")
"""The one refusal that is not final here: the dashboard takes a feedback only once it
has the session's end, which this console sends after its points are acknowledged,
so sometimes after the feedback was entered. It is sent again later."""


@unique
class Comfort(Enum):
    """How the ride felt, in the dashboard's three words."""

    GOOD = "good"
    ACCEPTABLE = "acceptable"
    BAD = "bad"


_COMFORTS: Final[Mapping[str, Comfort]] = MappingProxyType({c.value: c for c in Comfort})


@dataclass(frozen=True, slots=True)
class Scale:
    """One scale: its title and the label of each level, :data:`SCALE_MIN` first."""

    title: str
    labels: tuple[str, ...]


NAUSEA: Final[Scale] = Scale(
    "Nausée",
    (
        "Aucune",
        "Très légère",
        "Légère",
        "Légère à modérée",
        "Modérée",
        "Moyenne",
        "Assez forte",
        "Forte",
        "Très forte",
        "Extrême",
        "Maximale imaginable",
    ),
)
DIZZINESS: Final[Scale] = Scale(
    "Malaise ou vertige",
    (
        "Aucun",
        "Très léger",
        "Léger",
        "Léger à modéré",
        "Modéré",
        "Moyen",
        "Assez fort",
        "Fort",
        "Très fort",
        "Extrême",
        "Maximal imaginable",
    ),
)
COMFORT_LABELS: Final[Mapping[Comfort, str]] = MappingProxyType(
    {Comfort.GOOD: "Bon", Comfort.ACCEPTABLE: "Acceptable", Comfort.BAD: "Mauvais"}
)


@dataclass(frozen=True, slots=True)
class Feedback:
    """One feedback, checked: what the dashboard is sent, and nothing else."""

    nausea: Score
    dizziness: Score
    comfort: Comfort
    free_text: str | None
    """Trimmed, never empty; ``None`` when there is none."""


@dataclass(frozen=True, slots=True)
class InvalidField:
    """A feedback refused: the field named, never its value."""

    field: str


def _score(value: object) -> Score | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return Score(value) if SCALE_MIN <= value <= SCALE_MAX else None


def utf16_length(text: str) -> int:
    """The length the page and the dashboard measure: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _free_text(value: object) -> Result[str | None, InvalidField]:
    if value is None:
        return Ok(None)
    if not isinstance(value, str):
        return Err(InvalidField("free_text"))
    text = value.strip()
    if utf16_length(text) > FREE_TEXT_MAX or any(
        unicodedata.category(character) == "Cc" and character not in _ALLOWED_CONTROLS
        for character in text
    ):
        return Err(InvalidField("free_text"))
    return Ok(text or None)


def _object(raw: object) -> Mapping[str, object] | None:
    """``raw`` as a JSON object of feedback keys only; ``None`` for anything else."""
    if not isinstance(raw, dict):
        return None
    body = cast("Mapping[str, object]", raw)
    return body if body.keys() <= FIELDS else None


def parse_feedback(raw: object) -> Result[Feedback, InvalidField]:
    """The feedback in a request body (a JSON object), or the first field refused.

    Whole scores of the scale, a comfort of the list, a comment of at most
    :data:`FREE_TEXT_MAX` characters once trimmed and with no control character
    but a line break or a tab; an empty or blank comment is none. A key outside
    :data:`FIELDS` refuses the whole body. Nothing here says what was refused.
    """
    body = _object(raw)
    if body is None:
        return Err(InvalidField("body"))
    nausea = _score(body.get("nausea_score"))
    if nausea is None:
        return Err(InvalidField("nausea_score"))
    dizziness = _score(body.get("dizziness_score"))
    if dizziness is None:
        return Err(InvalidField("dizziness_score"))
    named = body.get("comfort")
    comfort = _COMFORTS.get(named) if isinstance(named, str) else None
    if comfort is None:
        return Err(InvalidField("comfort"))
    text = _free_text(body.get("free_text"))
    if isinstance(text, Err):
        return text
    return Ok(Feedback(nausea, dizziness, comfort, text.value))


def feedback_body(session_id: str, feedback: Feedback, at: UnixMillis) -> Mapping[str, JsonValue]:
    """The request of the events route that carries ``feedback``: one event, alone.

    Its ``detail`` is the compact JSON the dashboard reads, with the comment's
    own characters (an escaped accent would count six times against the
    event's 2000 characters). ``seq``, ``t`` and ``actor`` are checked by the
    dashboard as any event's, and then ignored.
    """
    detail: dict[str, JsonValue] = {
        "nauseaScore": int(feedback.nausea),
        "dizzinessScore": int(feedback.dizziness),
        "comfort": feedback.comfort.value,
    }
    if feedback.free_text is not None:
        detail["freeText"] = feedback.free_text
    event: dict[str, JsonValue] = {
        "seq": 0,
        "t": int(at),
        "kind": FEEDBACK_KIND,
        "detail": json.dumps(detail, separators=(",", ":"), ensure_ascii=False),
        "actor": OPERATOR_ACTOR,
    }
    return {"sessionId": session_id, "events": [event]}


# =========================================================================
# Where one session's feedback stands
# =========================================================================


@unique
class Delivery(Enum):
    """Where the feedback of one session stands, as the card says it."""

    TO_FILL = "to_fill"
    """Offered: nothing entered yet."""

    WAITING_LINK = "waiting_link"
    """Entered: waiting for the link, or for the dashboard to say which contract it speaks."""

    WAITING_SESSION = "waiting_session"
    """Entered: waiting for the session's identifier on the dashboard."""

    SERVER_TOO_OLD = "server_too_old"
    """Entered: the dashboard is older than 1.7, and is not sent it."""

    WAITING_END = "waiting_end"
    """Entered: the dashboard does not have the session's end yet (:data:`SESSION_NOT_ENDED`)."""

    SENT = "sent"
    REFUSED = "refused"
    EXPIRED = "expired"
    """Given up: the end of the session was more than :data:`EDIT_WINDOW` ago."""

    NOT_PROVIDED = "not_provided"
    """Skipped by the operator, or never entered before the next session."""


WAITING: Final[frozenset[Delivery]] = frozenset(
    {
        Delivery.WAITING_LINK,
        Delivery.WAITING_SESSION,
        Delivery.SERVER_TOO_OLD,
        Delivery.WAITING_END,
    }
)
OPEN: Final[frozenset[Delivery]] = WAITING | {Delivery.TO_FILL}
"""What may still be entered, or entered again: the new one replaces the old."""


DELIVERY_WORDS: Final[Mapping[Delivery, str]] = MappingProxyType(
    {
        Delivery.TO_FILL: "",
        Delivery.WAITING_LINK: "En attente du tableau de bord : envoi à la reprise de la liaison",
        Delivery.WAITING_SESSION: (
            "En attente de la séance sur le tableau de bord : envoi dès qu'elle y est"
        ),
        Delivery.SERVER_TOO_OLD: "Serveur trop ancien (contrat 1.7 requis) : non envoyé",
        Delivery.WAITING_END: "En attente de la fin de séance sur le serveur : nouvel essai",
        Delivery.SENT: "Envoyé au tableau de bord",
        Delivery.REFUSED: "Refusé par le tableau de bord",
        Delivery.EXPIRED: "Abandonné : plus de 24 h depuis la fin de la séance",
        Delivery.NOT_PROVIDED: "Non renseigné",
    }
)
"""The status line of the card, for each delivery."""


def describe_delivery(delivery: Delivery, code: ErrorCode | None) -> str:
    """The status line of the card; a refusal names the dashboard's code, never more."""
    words = DELIVERY_WORDS[delivery]
    if delivery is not Delivery.REFUSED:
        return words
    return f"{words} ({'sans code' if code is None else code})"


type SessionIdReader = Callable[[], str | None]
"""Reads the dashboard's identifier of one session, which may be learned after its end."""


class FeedbackLink(Protocol):
    """What the sending asks of the dashboard link (:class:`~src.cloud_sync.CloudSync`)."""

    @property
    @abstractmethod
    def server_contract(self) -> ContractVersion | None:
        """The contract the dashboard announced in the last heartbeat it answered."""

    @abstractmethod
    async def send_feedback(self, body: Mapping[str, JsonValue]) -> Answer:
        """POST ``body`` to the events route and say what came back. Never raises."""


@dataclass(slots=True)
class _Entry:
    """One session's feedback. Mutable on purpose, owned by one :class:`PassengerFeedback`:
    its delivery changes as the operator and the link act, all on the event loop."""

    session_id: SessionIdReader
    ended: Monotonic
    ended_at: UnixMillis
    delivery: Delivery = Delivery.TO_FILL
    feedback: Feedback | None = None
    """Held only while it waits to be sent: dropped once sent, refused or given up."""

    code: ErrorCode | None = None
    next_try: Monotonic | None = None


@dataclass(frozen=True, slots=True)
class FeedbackOffer:
    """What the card shows of the session that ended last with a person on board."""

    ended_at: UnixMillis
    delivery: Delivery
    code: ErrorCode | None

    @property
    def can_submit(self) -> bool:
        return self.delivery in OPEN


@dataclass(frozen=True, slots=True)
class FeedbackView:
    current: FeedbackOffer | None
    """``None``: no card. Nothing ended with a person on board since the last start,
    or the operator closed it."""

    given_up: int
    """Feedbacks dropped since the console started because more than :data:`MAX_HELD` waited."""


@dataclass(frozen=True, slots=True)
class NothingToFill:
    """No card is open: no session with a person on board ended since the last start."""


class PassengerFeedback:
    """The feedbacks of this run of the console, in memory only. See the module docstring."""

    __slots__ = ("_clock", "_entries", "_given_up", "_shown")

    def __init__(self, clock: Clock) -> None:
        self._clock: Clock = clock
        # Delivery state, oldest first, mutated only on the event loop: by the
        # routes (submit, skip), the panel (began, ended) and the link's step.
        self._entries: list[_Entry] = []
        self._shown: _Entry | None = None
        self._given_up: int = 0

    # --- what the panel tells it -------------------------------------------

    def began(self) -> None:
        """A session was armed: the card of the one before closes.

        A feedback left to fill is not provided; one entered is still sent.
        """
        self._close_card()
        self._prune()

    def ended(self, session_id: SessionIdReader) -> None:
        """A session with a person on board ended: its feedback is offered."""
        self._close_card()
        self._prune()
        if len(self._entries) >= MAX_HELD:
            # Once the card is closed, what is left all waits to be sent.
            del self._entries[0]
            self._given_up += 1
            _logger.warning(
                "rider feedback given up: more than %d sessions waited to send theirs",
                MAX_HELD,
            )
        entry = _Entry(
            session_id=session_id,
            ended=self._clock.monotonic(),
            ended_at=self._clock.unix_millis(),
        )
        self._entries.append(entry)
        self._shown = entry

    # --- what the routes call -----------------------------------------------

    def view(self) -> FeedbackView:
        self._expire()
        shown = self._shown
        return FeedbackView(
            current=(
                None if shown is None else FeedbackOffer(shown.ended_at, shown.delivery, shown.code)
            ),
            given_up=self._given_up,
        )

    def submit(self, feedback: Feedback) -> Result[None, NothingToFill]:
        """Enter the feedback of the card's session; one entered before replaces it."""
        self._expire()
        shown = self._shown
        if shown is None or shown.delivery not in OPEN:
            return Err(NothingToFill())
        shown.feedback = feedback
        shown.delivery = Delivery.WAITING_LINK
        shown.next_try = None
        return Ok(None)

    def skip(self) -> Result[None, NothingToFill]:
        """Close the card. A feedback not sent yet is not provided: nothing is sent."""
        self._expire()
        shown = self._shown
        if shown is None:
            return Err(NothingToFill())
        if shown.delivery in OPEN:
            shown.delivery = Delivery.NOT_PROVIDED
            shown.feedback = None
        self._shown = None
        self._prune()
        return Ok(None)

    # --- the sending ----------------------------------------------------------

    async def step(self, link: FeedbackLink) -> None:
        """Send what may be sent now, once each; say why the others wait."""
        self._expire()
        now = self._clock.monotonic()
        for entry in tuple(self._entries):
            feedback = entry.feedback
            if (
                feedback is None
                or entry.delivery not in WAITING
                or (entry.next_try is not None and now < entry.next_try)
            ):
                continue
            session_id = entry.session_id()
            if session_id is None:
                entry.delivery = Delivery.WAITING_SESSION
                continue
            announced = link.server_contract
            if not takes_feedback(announced):
                entry.delivery = (
                    Delivery.WAITING_LINK if announced is None else Delivery.SERVER_TOO_OLD
                )
                continue
            answer = await link.send_feedback(
                feedback_body(session_id, feedback, self._clock.unix_millis())
            )
            self._heard(entry, feedback, answer, now)
        self._prune()

    def _heard(self, entry: _Entry, sent: Feedback, answer: Answer, now: Monotonic) -> None:
        """What the dashboard's answer does to ``entry``.

        A feedback entered again while this one travelled goes at the next
        step; a card skipped meanwhile is told what became of the one sent.
        """
        current = entry.feedback
        if current is not None and current is not sent:
            return
        match answer:
            case Acked():
                entry.delivery = Delivery.SENT
                entry.feedback = None
                _logger.info("rider feedback sent to the dashboard")
            case Refusal():
                if answer.code == SESSION_NOT_ENDED:
                    self._later(entry, current, Delivery.WAITING_END, now)
                    return
                entry.delivery = Delivery.REFUSED
                entry.code = answer.code
                entry.feedback = None
                _logger.warning(
                    "rider feedback refused by the dashboard: %s",
                    "sans code" if answer.code is None else answer.code,
                )
            case Held():
                self._later(entry, current, Delivery.WAITING_LINK, now)
            case RouteMissing():
                self._later(entry, current, Delivery.SERVER_TOO_OLD, now)
            case _ as unreachable:
                assert_never(unreachable)

    @staticmethod
    def _later(entry: _Entry, current: Feedback | None, delivery: Delivery, now: Monotonic) -> None:
        """Not taken this time: tried again after :data:`RETRY_PERIOD`, unless skipped meanwhile."""
        if current is None:
            return
        entry.delivery = delivery
        entry.next_try = Monotonic(now + RETRY_PERIOD)

    # --- bookkeeping ----------------------------------------------------------

    def _close_card(self) -> None:
        shown = self._shown
        if shown is not None and shown.delivery is Delivery.TO_FILL:
            shown.delivery = Delivery.NOT_PROVIDED
        self._shown = None

    def _expire(self) -> None:
        now = self._clock.monotonic()
        for entry in self._entries:
            if entry.delivery in OPEN and elapsed(entry.ended, now) > EDIT_WINDOW:
                if entry.feedback is not None:
                    _logger.warning("rider feedback given up: the session ended over 24 h ago")
                entry.delivery = Delivery.EXPIRED
                entry.feedback = None

    def _prune(self) -> None:
        """Forget what is no longer shown and holds nothing to send."""
        self._entries = [
            entry for entry in self._entries if entry is self._shown or entry.delivery in WAITING
        ]
