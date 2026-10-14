"""Who may ride: the passengers the dashboard lists for this machine (ANH-84).

The dashboard serves, at ``GET /api/machine/roster``, the riders holding the
launch right on this machine who are still active members of its
organisation, each with what a manager set on the site: a maximum heart rate
and a birth year. The console offers them on its page, and a session with a
person on board names one of them. Neither value can be typed or changed at
the console: they come from the site or the session does not start.

What the console keeps, and where
---------------------------------
In memory only. A rider's name is shown on the page so that the operator picks
the right person; it is never written to disk (not in a record, not in the
logbook, not in a log line) and never put in a sentence the console says,
since those sentences are written to the record. What the record keeps of a
rider is the dashboard's identifier, ``subject_id`` in the manifest: a
pseudonym. A console that restarts holds no list until the dashboard answers.

Fresh enough, or nobody
-----------------------
The list is asked for when the console starts and every
:data:`ROSTER_PERIOD` while no session runs (:mod:`src.cloud_sync`). Each list
received replaces the previous one whole: a rider the latest list no longer
holds is offered no more, and a start that names them is refused, at the
route and again when the loop arms it.

Without an answer (no network, a server error, an answer that is not a list)
the last list received is kept, offline, for :data:`ROSTER_MAX_AGE`; past
that, nobody is offered until the dashboard answers again. A dashboard that
refuses this machine's key empties the list at once: a machine that is no
longer recognised has nobody to offer.

What this does not do
---------------------
A right revoked after the list was received is not seen until the next list.
The re-check of identity and rights at the arming of a launch is ANH-144, on
the dashboard's side. The dashboard also re-checks the right when it registers
a session started here: a rider no longer authorised is not attributed the
session (ANH-180). Fitness and consent (ANH-143) are not served yet:
:func:`passenger_of` is where their gate goes when they are.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, unique
from typing import Final, assert_never, cast

from src.control_surface import LOCAL_SUBJECT
from src.result import Err, Ok, Result
from src.training.plan import SUBJECT_HR_MAX_MAX, SUBJECT_HR_MAX_MIN
from src.units import Bpm, Monotonic, Seconds, UnixMillis, elapsed

_logger: logging.Logger = logging.getLogger(__name__)

ROSTER_PATH: Final[str] = "/api/machine/roster"
"""The dashboard's route for the list."""

ROSTER_PERIOD: Final[Seconds] = Seconds(60.0)
"""How often the list is asked for while no session runs, answered or not."""

ROSTER_MAX_AGE: Final[Seconds] = Seconds(3600.0)
"""How long a list received is offered without a newer one.

A prudent default: a session or two through a lost network, not a day of
work offline. Past it, the console offers nobody until the dashboard answers.
"""

NAME_LIMIT: Final[int] = 80
"""Characters of a rider's name kept for the page."""

RIDER_LIMIT: Final[int] = 1000
"""Riders one list may hold; a longer one is not a list this console takes."""

OLDEST_RIDER: Final[int] = 120
"""Beyond this age the birth year and the machine's clock do not make an age."""

_USER_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")
"""An identifier the record can carry as it is (``record.codec.IDENTIFIER``)."""

_EARLIEST_BIRTH_YEAR: Final[int] = 1900
_LATEST_BIRTH_YEAR: Final[int] = 2100


# =========================================================================
# A rider, and a passenger
# =========================================================================


@dataclass(frozen=True, slots=True)
class Rider:
    """One rider of the list, as the dashboard served them."""

    user_id: str
    """The dashboard's identifier: the record's ``subject_id``, a pseudonym."""

    name: str
    """For the operator's eyes only. Memory and the page; never a disk, never a log."""

    hr_max: Bpm | None
    """The maximum heart rate the site uses for this rider; ``None`` when it has none."""

    birth_year: int | None
    """Set on the site; ``None`` when it is not."""


@dataclass(frozen=True, slots=True)
class Passenger:
    """A rider a programmed session can be vetted for: both values known."""

    rider: Rider
    hr_max: Bpm
    age: int
    """In whole years at the start, the later birthday assumed: never older."""


# =========================================================================
# Why a start has no passenger
# =========================================================================


@unique
class RosterState(Enum):
    """Whether the list may be offered. String values: they go to the page."""

    UNLINKED = "unlinked"
    """No dashboard is configured: there is no list to offer."""

    WAITING = "waiting"
    """No list has been received since the console started."""

    CURRENT = "current"
    """A list received less than :data:`ROSTER_MAX_AGE` ago: it is offered."""

    STALE = "stale"
    """The last list received is older than :data:`ROSTER_MAX_AGE`: nobody is offered."""

    REFUSED = "refused"
    """The dashboard refused this machine's key: the list was emptied."""


@dataclass(frozen=True, slots=True)
class NoPassenger:
    """A start with a person on board named nobody."""


@dataclass(frozen=True, slots=True)
class OnBench:
    """A bench session (nobody on board) named a passenger."""


@dataclass(frozen=True, slots=True)
class NoList:
    """No list may be offered right now."""

    state: RosterState
    age: Seconds | None
    """How old the last list is, when there is one."""


@dataclass(frozen=True, slots=True)
class NotListed:
    """The rider named is not in the list held now."""


@dataclass(frozen=True, slots=True)
class HrMaxUnknown:
    """The site has no maximum heart rate for this rider."""


@dataclass(frozen=True, slots=True)
class BirthYearUnknown:
    """The site has no birth year for this rider."""


@dataclass(frozen=True, slots=True)
class AgeUnknowable:
    """The birth year and this machine's clock give no plausible age."""


type PassengerRefusal = (
    NoPassenger | OnBench | NoList | NotListed | HrMaxUnknown | BirthYearUnknown | AgeUnknowable
)
"""Closed: :func:`describe_passenger_refusal` says each of them."""


def describe_state(state: RosterState, age: Seconds | None) -> str:
    """Why nobody is offered, in the operator's words; empty for a list that is offered."""
    match state:
        case RosterState.CURRENT:
            return ""
        case RosterState.UNLINKED:
            return (
                "aucun tableau de bord configure sur cette console : "
                "la liste des passagers vient du site"
            )
        case RosterState.WAITING:
            return "liste des passagers pas encore recue du tableau de bord"
        case RosterState.STALE:
            minutes = "?" if age is None else str(math.floor(age / 60))
            limit = round(ROSTER_MAX_AGE / 60)
            return (
                f"liste des passagers perimee : recue il y a {minutes} min, "
                f"au-dela de {limit} min personne n'est propose"
            )
        case RosterState.REFUSED:
            return (
                "le tableau de bord refuse la cle de cette machine : aucun passager n'est propose"
            )
    raise assert_never(state)


def describe_passenger_refusal(refusal: PassengerRefusal) -> str:  # noqa: PLR0911  # one per case
    """One line for a start refused for its passenger. Never the rider's name nor values."""
    match refusal:
        case NoPassenger():
            return (
                "demarrage refuse : passager requis avec une personne a bord, "
                "a choisir dans la liste du tableau de bord"
            )
        case OnBench():
            return "demarrage refuse : une seance de banc n'a personne a bord, donc aucun passager"
        case NoList():
            return f"demarrage refuse : {describe_state(refusal.state, refusal.age)}"
        case NotListed():
            return (
                "demarrage refuse : ce passager n'est pas dans la derniere liste du tableau "
                "de bord (droit retire, ou liste changee) : le choisir de nouveau"
            )
        case HrMaxUnknown():
            return (
                "demarrage refuse : FC max du passager non renseignee sur le site "
                "(a regler par un gestionnaire)"
            )
        case BirthYearUnknown():
            return (
                "demarrage refuse : annee de naissance du passager non renseignee sur le site "
                "(a regler par un gestionnaire)"
            )
        case AgeUnknowable():
            return "demarrage refuse : age du passager incalculable avec l'horloge de cette machine"
    raise assert_never(refusal)


# =========================================================================
# Reading the dashboard's answer
# =========================================================================


@dataclass(frozen=True, slots=True)
class Listing:
    """A list read from an answer, and how many of its entries could not be."""

    riders: tuple[Rider, ...]
    dropped: int


def _whole(value: object) -> int | None:
    """``value`` as a whole number, or ``None``: a JSON number with no fraction, never a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _hr_max(value: object) -> Bpm | None:
    """A maximum heart rate this console believes (``plan.SUBJECT_HR_MAX_*``), or ``None``."""
    whole = _whole(value)
    if whole is None or not SUBJECT_HR_MAX_MIN <= whole <= SUBJECT_HR_MAX_MAX:
        return None
    return Bpm(whole)


def _birth_year(value: object) -> int | None:
    whole = _whole(value)
    if whole is None or not _EARLIEST_BIRTH_YEAR <= whole <= _LATEST_BIRTH_YEAR:
        return None
    return whole


def _rider(raw: object) -> Rider | None:
    """One entry of the list, or ``None`` when it names nobody this console can offer.

    An identifier the record cannot carry as it is, or a blank name the
    operator could not pick by, drops the entry. A value that is not usable
    (out of range, not a whole number) is kept as unknown: the rider is
    offered, and a programmed session for them is refused for that value.
    """
    if not isinstance(raw, dict):
        return None
    entry = cast("Mapping[str, object]", raw)
    user_id = entry.get("userId")
    name = entry.get("name")
    if not isinstance(user_id, str) or _USER_ID.fullmatch(user_id) is None:
        return None
    if user_id == LOCAL_SUBJECT or not isinstance(name, str):
        return None
    shown = " ".join(name.split())[:NAME_LIMIT].strip()
    if not shown:
        return None
    return Rider(
        user_id=user_id,
        name=shown,
        hr_max=_hr_max(entry.get("hrMax")),
        birth_year=_birth_year(entry.get("birthYear")),
    )


def parse_roster(document: Mapping[str, object]) -> Result[Listing, str]:
    """The list in a ``/api/machine/roster`` answer, or why it is none.

    An entry that names nobody is dropped and counted; a second entry for the
    same rider is dropped too. An answer without a list, or with a longer one
    than :data:`RIDER_LIMIT`, is not a list.
    """
    raw = document.get("riders")
    if not isinstance(raw, list):
        return Err("reponse du tableau de bord sans liste de passagers")
    entries = cast("Sequence[object]", raw)
    if len(entries) > RIDER_LIMIT:
        return Err(f"liste de {len(entries)} passagers, au-dela de {RIDER_LIMIT}")
    riders: list[Rider] = []
    seen: set[str] = set()
    for entry in entries:
        rider = _rider(entry)
        if rider is None or rider.user_id in seen:
            continue
        seen.add(rider.user_id)
        riders.append(rider)
    return Ok(Listing(riders=tuple(riders), dropped=len(entries) - len(riders)))


# =========================================================================
# What a programmed session needs of a rider
# =========================================================================


def age_at(birth_year: int, at: UnixMillis) -> int | None:
    """Age in whole years at ``at``, as the site counts it (``training.ageFrom``).

    The later birthday is assumed: never older. ``None`` when this machine's
    clock and the birth year give no plausible age (a clock left in 1970).
    """
    year = datetime.fromtimestamp(at / 1000, UTC).year
    age = year - birth_year - 1
    return age if 0 <= age <= OLDEST_RIDER else None


def passenger_of(rider: Rider, at: UnixMillis) -> Result[Passenger, PassengerRefusal]:
    """``rider`` with what a programmed session is vetted on, or the value that is missing.

    Both come from the site; a missing one is a refusal, as on the site. The
    minimum age is the console's own gate (``MIN_RIDER_AGE``), applied after.
    Fitness and consent (ANH-143) will be checked here.
    """
    hr_max = rider.hr_max
    if hr_max is None:
        return Err(HrMaxUnknown())
    birth_year = rider.birth_year
    if birth_year is None:
        return Err(BirthYearUnknown())
    age = age_at(birth_year, at)
    if age is None:
        return Err(AgeUnknowable())
    return Ok(Passenger(rider=rider, hr_max=hr_max, age=age))


# =========================================================================
# The list the console holds
# =========================================================================


@dataclass(frozen=True, slots=True)
class RosterView:
    """What the page is shown of the list."""

    state: RosterState
    riders: tuple[Rider, ...]
    """Offered now: empty unless the list is current."""

    age: Seconds | None
    """How old the list held is; ``None`` when none is held."""

    problem: str | None
    """Why the last request for a list got none, while that is still the latest news."""


class Roster:
    """The list held, and when it was received. See the module docstring.

    Mutable on purpose and shared, all on the console's one event loop: the
    dashboard task writes it, the page's routes and the control loop read it.
    Every method completes without awaiting.
    """

    __slots__ = ("_heard_at", "_last_try", "_linked", "_problem", "_refused", "_riders")

    def __init__(self, *, linked: bool) -> None:
        """``linked``: a dashboard is configured. Without one, no list is ever held."""
        self._linked: bool = linked
        self._riders: tuple[Rider, ...] = ()
        self._heard_at: Monotonic | None = None
        self._last_try: Monotonic | None = None
        self._problem: str | None = None
        self._refused: bool = False

    # --- what the link tells it ------------------------------------------

    def due(self, now: Monotonic) -> bool:
        """Whether a list is to be asked for now: first, then every :data:`ROSTER_PERIOD`."""
        last = self._last_try
        return self._linked and (last is None or elapsed(last, now) >= ROSTER_PERIOD)

    def heard(self, document: Mapping[str, object], now: Monotonic) -> None:
        """An answer: the list it holds replaces the one held, whole."""
        parsed = parse_roster(document)
        if isinstance(parsed, Err):
            self.missed(parsed.error, now)
            return
        listing = parsed.value
        if len(listing.riders) != len(self._riders) or self._heard_at is None:
            _logger.info("roster: %d riders listed", len(listing.riders))
        if listing.dropped:
            _logger.warning(
                "roster: %d entries not offered (unreadable or repeated)", listing.dropped
            )
        self._riders = listing.riders
        self._heard_at = now
        self._last_try = now
        self._problem = None
        self._refused = False

    def missed(self, reason: str, now: Monotonic) -> None:
        """No list this time: the one held is kept, and ages."""
        if reason != self._problem:
            _logger.warning("roster: no list (%s)", reason)
        self._last_try = now
        self._problem = reason

    def refused(self, reason: str, now: Monotonic) -> None:
        """The dashboard refused this machine's key: nobody is offered until it answers."""
        if not self._refused:
            _logger.warning("roster: emptied, the dashboard refuses this machine (%s)", reason)
        self._riders = ()
        self._heard_at = None
        self._last_try = now
        self._problem = reason
        self._refused = True

    # --- what the page and the starts read ---------------------------------

    def state(self, now: Monotonic) -> RosterState:
        """Whether the list held may be offered at ``now``."""
        heard = self._heard_at
        if not self._linked:
            return RosterState.UNLINKED
        if self._refused:
            return RosterState.REFUSED
        if heard is None:
            return RosterState.WAITING
        if elapsed(heard, now) > ROSTER_MAX_AGE:
            return RosterState.STALE
        return RosterState.CURRENT

    def view(self, now: Monotonic) -> RosterView:
        """What the page shows: the riders offered, or why there are none."""
        state = self.state(now)
        heard = self._heard_at
        return RosterView(
            state=state,
            riders=self._riders if state is RosterState.CURRENT else (),
            age=None if heard is None else elapsed(heard, now),
            problem=self._problem,
        )

    def find(self, user_id: str, now: Monotonic) -> Result[Rider, PassengerRefusal]:
        """The rider ``user_id`` names, if the list offered now holds them."""
        if user_id in ("", LOCAL_SUBJECT):
            return Err(NoPassenger())
        state = self.state(now)
        if state is not RosterState.CURRENT:
            heard = self._heard_at
            return Err(NoList(state, None if heard is None else elapsed(heard, now)))
        for rider in self._riders:
            if rider.user_id == user_id:
                return Ok(rider)
        return Err(NotListed())

    def adopt(self, riders: Sequence[Rider], now: Monotonic) -> None:
        """Hold ``riders`` as if the dashboard had just listed them.

        For a console in a test or a simulation, whose dashboard is a fake: the
        console's own link only ever calls :meth:`heard`.
        """
        self._riders = tuple(riders)
        self._heard_at = now
        self._problem = None
        self._refused = False
