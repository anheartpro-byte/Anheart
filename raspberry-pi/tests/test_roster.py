"""The riders' list (ANH-84): what is read of the dashboard's answer, and when it is offered.

The list decides who a start with a person on board may name, and the maximum
heart rate and age a programme is vetted on. So each rule below is a test: an
entry that names nobody is not offered, a value the site has not set is not
replaced by another, a list replaces the previous one whole, a list too old
offers nobody, and nothing of a rider but counts reaches the log.
"""

from __future__ import annotations

import ast
import logging
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

import pytest

from src.clock import ManualClock
from src.control_surface import LOCAL_SUBJECT
from src.result import Err, Ok
from src.roster import (
    NAME_LIMIT,
    OLDEST_RIDER,
    RIDER_LIMIT,
    ROSTER_MAX_AGE,
    ROSTER_PERIOD,
    AgeUnknowable,
    BirthYearUnknown,
    HrMaxUnknown,
    Listing,
    NoList,
    NoPassenger,
    NotListed,
    OnBench,
    Passenger,
    PassengerRefusal,
    Rider,
    Roster,
    RosterState,
    age_at,
    age_of,
    describe_passenger_refusal,
    describe_state,
    parse_roster,
    passenger_of,
)
from src.units import Bpm, Monotonic, Seconds, UnixMillis
from tests.roster_support import RIDER, RIDER_ID, RIDER_NAME, roster_document

NOVEMBER_2023: Final[UnixMillis] = UnixMillis(1_700_000_000_000)
"""The wall clock of the console rigs: 14 November 2023."""

SRC: Final[Path] = Path(__file__).resolve().parent.parent / "src"

OTHER: Final[Rider] = Rider(
    user_id="k17other0synthetic", name="Other Rider", hr_max=Bpm(175), birth_year=1985
)


def clock_at(start: float = 10.0) -> ManualClock:
    return ManualClock(Monotonic(start), NOVEMBER_2023)


def entry(**fields: object) -> dict[str, object]:
    """One rider as the dashboard serves them, with ``fields`` changed (``None`` leaves out)."""
    row: dict[str, object] = {
        "userId": RIDER_ID,
        "name": RIDER_NAME,
        "hrMax": 190,
        "birthYear": 1940,
    }
    for key, value in fields.items():
        if value is None:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def listing_of(*rows: object) -> Listing:
    parsed = parse_roster({"riders": list(rows)})
    assert isinstance(parsed, Ok), parsed
    return parsed.value


def heard(roster: Roster, clock: ManualClock, *riders: Rider) -> None:
    roster.heard(roster_document(*riders), clock.monotonic())


# =========================================================================
# Reading the dashboard's answer
# =========================================================================


def test_a_rider_is_read_with_the_values_the_site_set() -> None:
    assert listing_of(entry()) == Listing(riders=(RIDER,), dropped=0)


def test_a_whole_number_written_as_a_float_is_the_same_number() -> None:
    (rider,) = listing_of(entry(hrMax=190.0, birthYear=1940.0)).riders
    assert (rider.hr_max, rider.birth_year) == (190, 1940)


@pytest.mark.parametrize(
    "value",
    [True, 99, 221, 180.5, "180", math.nan, math.inf, [], None],
    ids=["bool", "below", "above", "fraction", "text", "nan", "inf", "list", "absent"],
)
def test_a_max_heart_rate_that_is_not_believable_is_unknown_never_replaced(value: object) -> None:
    """Unknown, and the rider still offered: a programme for them is refused for it."""
    (rider,) = listing_of(entry(hrMax=value)).riders
    assert rider.hr_max is None
    assert rider.birth_year == 1940


@pytest.mark.parametrize(
    "value",
    [False, 1899, 2101, 1990.5, "1990", None],
    ids=["bool", "before", "after", "fraction", "text", "absent"],
)
def test_a_birth_year_that_is_not_a_year_is_unknown(value: object) -> None:
    (rider,) = listing_of(entry(birthYear=value)).riders
    assert rider.birth_year is None
    assert rider.hr_max == 190


def test_the_bounds_of_a_believable_max_heart_rate_are_kept() -> None:
    low, high = listing_of(entry(userId="low", hrMax=100), entry(userId="high", hrMax=220)).riders
    assert (low.hr_max, high.hr_max) == (100, 220)


def test_a_name_is_shown_on_one_line_and_bounded() -> None:
    (rider,) = listing_of(entry(name="  Synthetic \n\t  Rider  ")).riders
    assert rider.name == "Synthetic Rider"
    (long,) = listing_of(entry(name="x" * (NAME_LIMIT + 20))).riders
    assert long.name == "x" * NAME_LIMIT


@pytest.mark.parametrize(
    "row",
    [
        "not an object",
        entry(userId=None),
        entry(userId=42),
        entry(userId=""),
        entry(userId="has space"),
        entry(userId="x" * 65),
        entry(userId="../etc"),
        entry(userId=LOCAL_SUBJECT),
        entry(name=None),
        entry(name=7),
        entry(name="   "),
    ],
    ids=[
        "not-object",
        "no-id",
        "number-id",
        "empty-id",
        "space-id",
        "long-id",
        "path-id",
        "local-id",
        "no-name",
        "number-name",
        "blank-name",
    ],
)
def test_an_entry_that_names_nobody_the_console_can_offer_is_dropped_and_counted(
    row: object,
) -> None:
    assert listing_of(row, entry(userId="kept")) == Listing(
        riders=(Rider(user_id="kept", name=RIDER_NAME, hr_max=Bpm(190), birth_year=1940),),
        dropped=1,
    )


def test_a_rider_listed_twice_is_offered_once_with_the_first_values() -> None:
    assert listing_of(entry(), entry(hrMax=150)) == Listing(riders=(RIDER,), dropped=1)


@pytest.mark.parametrize(
    "document",
    [{}, {"riders": None}, {"riders": {"userId": RIDER_ID}}, {"riders": "everyone"}],
    ids=["absent", "null", "object", "text"],
)
def test_an_answer_without_a_list_is_not_a_list(document: Mapping[str, object]) -> None:
    assert parse_roster(document) == Err("reponse du tableau de bord sans liste de passagers")


def test_a_list_longer_than_the_console_takes_is_not_a_list() -> None:
    rows = [entry(userId=f"r{index}") for index in range(RIDER_LIMIT + 1)]
    assert parse_roster({"riders": rows}) == Err(
        f"liste de {RIDER_LIMIT + 1} passagers, au-dela de {RIDER_LIMIT}"
    )
    assert isinstance(parse_roster({"riders": rows[:RIDER_LIMIT]}), Ok)


# =========================================================================
# The list held
# =========================================================================


def test_a_console_without_a_dashboard_never_asks_and_offers_nobody() -> None:
    clock = clock_at()
    roster = Roster(linked=False)
    now = clock.monotonic()
    assert not roster.due(now)
    assert roster.state(now) is RosterState.UNLINKED
    assert roster.find(RIDER_ID, now) == Err(NoList(RosterState.UNLINKED, None))
    assert roster.view(now).riders == ()


def test_a_linked_console_asks_at_once_and_offers_nobody_until_it_is_answered() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    now = clock.monotonic()
    assert roster.due(now)
    assert roster.state(now) is RosterState.WAITING
    assert roster.find(RIDER_ID, now) == Err(NoList(RosterState.WAITING, None))


def test_an_answer_is_offered_and_asked_again_a_period_later() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock, RIDER, OTHER)
    now = clock.monotonic()
    assert roster.state(now) is RosterState.CURRENT
    assert roster.find(RIDER_ID, now) == Ok(RIDER)
    assert roster.find(OTHER.user_id, now) == Ok(OTHER)
    assert roster.view(now).riders == (RIDER, OTHER)
    assert not roster.due(now)
    clock.advance(Seconds(ROSTER_PERIOD - 0.1))
    assert not roster.due(clock.monotonic())
    clock.advance(Seconds(0.1))
    assert roster.due(clock.monotonic())


@pytest.mark.parametrize("named", ["", LOCAL_SUBJECT])
def test_a_start_that_names_nobody_has_no_passenger(named: str) -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    assert roster.find(named, clock.monotonic()) == Err(NoPassenger())


def test_a_rider_the_list_does_not_hold_is_not_offered() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    assert roster.find(OTHER.user_id, clock.monotonic()) == Err(NotListed())


def test_a_list_replaces_the_one_held_whole() -> None:
    """A rider the latest list no longer holds is offered no more."""
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock, RIDER, OTHER)
    clock.advance(ROSTER_PERIOD)
    heard(roster, clock, OTHER)
    now = clock.monotonic()
    assert roster.find(RIDER_ID, now) == Err(NotListed())
    assert roster.view(now).riders == (OTHER,)
    roster.heard({"riders": []}, now)
    assert roster.find(OTHER.user_id, now) == Err(NotListed())
    assert roster.state(now) is RosterState.CURRENT


def test_without_an_answer_the_list_held_is_kept_and_ages() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    clock.advance(Seconds(120.0))
    roster.missed("tableau de bord injoignable : timeout", clock.monotonic())
    view = roster.view(clock.monotonic())
    assert view.state is RosterState.CURRENT
    assert view.riders == (RIDER,)
    assert view.age == pytest.approx(120.0)
    assert view.problem == "tableau de bord injoignable : timeout"
    assert not roster.due(clock.monotonic()), "a miss is a request: the next one waits a period"


def test_an_answer_that_is_not_a_list_is_a_miss() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    roster.heard({"active": True}, clock.monotonic())
    view = roster.view(clock.monotonic())
    assert view.riders == (RIDER,)
    assert view.problem == "reponse du tableau de bord sans liste de passagers"


def test_a_list_too_old_offers_nobody_until_the_dashboard_answers() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    clock.advance(ROSTER_MAX_AGE)
    assert roster.state(clock.monotonic()) is RosterState.CURRENT, "at the limit: still offered"
    clock.advance(Seconds(1.0))
    now = clock.monotonic()
    age = Seconds(ROSTER_MAX_AGE + 1.0)
    assert roster.state(now) is RosterState.STALE
    assert roster.find(RIDER_ID, now) == Err(NoList(RosterState.STALE, age))
    assert roster.view(now).riders == ()
    assert roster.view(now).age == pytest.approx(age)
    heard(roster, clock)
    assert roster.find(RIDER_ID, clock.monotonic()) == Ok(RIDER)


def test_a_refused_key_empties_the_list_until_the_dashboard_answers() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    heard(roster, clock)
    roster.refused("sans code (HTTP 401): Unauthorized", clock.monotonic())
    now = clock.monotonic()
    assert roster.state(now) is RosterState.REFUSED
    assert roster.find(RIDER_ID, now) == Err(NoList(RosterState.REFUSED, None))
    assert roster.view(now).age is None
    roster.missed("tableau de bord injoignable : timeout", now)
    assert roster.state(now) is RosterState.REFUSED, "a silence takes nothing back"
    heard(roster, clock)
    assert roster.state(clock.monotonic()) is RosterState.CURRENT
    assert roster.view(clock.monotonic()).problem is None


def test_a_list_given_by_a_test_is_held_as_if_heard() -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    roster.refused("sans code (HTTP 403): Forbidden", clock.monotonic())
    roster.adopt((OTHER,), clock.monotonic())
    view = roster.view(clock.monotonic())
    assert (view.state, view.riders, view.problem) == (RosterState.CURRENT, (OTHER,), None)


def test_the_log_gets_counts_and_reasons_never_a_rider(caplog: pytest.LogCaptureFixture) -> None:
    clock = clock_at()
    roster = Roster(linked=True)
    with caplog.at_level(logging.DEBUG, logger="src.roster"):
        heard(roster, clock, RIDER, OTHER)
        heard(roster, clock, RIDER, OTHER)
        roster.heard({"riders": [entry(), entry(), "junk"]}, clock.monotonic())
        roster.missed("tableau de bord injoignable : timeout", clock.monotonic())
        roster.missed("tableau de bord injoignable : timeout", clock.monotonic())
        roster.refused("sans code (HTTP 401): Unauthorized", clock.monotonic())
        roster.refused("sans code (HTTP 401): Unauthorized", clock.monotonic())
    said = [record.getMessage() for record in caplog.records]
    assert said == [
        "roster: 2 riders listed",
        "roster: 1 riders listed",
        "roster: 2 entries not offered (unreadable or repeated)",
        "roster: no list (tableau de bord injoignable : timeout)",
        "roster: emptied, the dashboard refuses this machine (sans code (HTTP 401): Unauthorized)",
    ]
    for line in said:
        for rider in (RIDER, OTHER):
            assert rider.name not in line
            assert rider.user_id not in line


# =========================================================================
# What a programmed session needs of a rider
# =========================================================================


def test_the_age_is_counted_as_the_site_counts_it() -> None:
    """The later birthday assumed: born in 1990, 32 in November 2023, never 33."""
    assert age_at(1990, NOVEMBER_2023) == 32
    assert age_at(2022, NOVEMBER_2023) == 0
    assert age_at(2023 - 1 - OLDEST_RIDER, NOVEMBER_2023) == OLDEST_RIDER


@pytest.mark.parametrize("birth_year", [2023, 2030, 2023 - 2 - OLDEST_RIDER])
def test_a_birth_year_the_clock_makes_no_age_of_gives_none(birth_year: int) -> None:
    assert age_at(birth_year, NOVEMBER_2023) is None


def test_a_clock_left_in_1970_gives_no_age_for_a_rider_born_after() -> None:
    assert age_at(1990, UnixMillis(0)) is None


def test_a_clock_left_in_the_past_makes_a_rider_younger_never_older() -> None:
    """Born in 1940: 29 on a clock left in 1970, 82 in 2023. The error is on the side
    that refuses (the minimum age), and a clock nobody keeps refuses a person on
    board anyway (wall clock gate)."""
    assert age_at(1940, UnixMillis(0)) == 29
    assert age_at(1940, NOVEMBER_2023) == 82


def test_the_age_alone_is_what_a_manual_session_on_board_is_vetted_on() -> None:
    """No maximum heart rate is needed to count an age: a manual session fits nothing to it."""
    assert age_of(Rider(RIDER_ID, RIDER_NAME, None, 1990), NOVEMBER_2023) == Ok(32)
    assert age_of(Rider(RIDER_ID, RIDER_NAME, Bpm(190), None), NOVEMBER_2023) == Err(
        BirthYearUnknown()
    )
    assert age_of(Rider(RIDER_ID, RIDER_NAME, Bpm(190), 2030), NOVEMBER_2023) == Err(
        AgeUnknowable()
    )


def test_a_list_is_offered_fifteen_minutes_without_a_newer_one() -> None:
    """The product owner's decision of 15 October 2026, a constant of the code."""
    assert ROSTER_MAX_AGE == 900.0
    assert ROSTER_PERIOD == 60.0


def test_nothing_in_the_console_holds_a_list_it_was_not_given() -> None:
    """``Roster.adopt`` is for tests and the simulation: no source file of the console calls
    it. The only ``adopt`` calls under ``src/`` are the profile store's own."""
    calls: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls.extend(
            f"{path.relative_to(SRC.parent)}: {ast.unparse(node.func)}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "adopt"
        )
    assert calls == [
        "src/training/plan.py: self.adopt",
        "src/training/plan.py: self.adopt",
        "src/web/profile_writer.py: self._store.adopt",
    ]


def test_a_rider_with_both_values_is_a_passenger() -> None:
    assert passenger_of(RIDER, NOVEMBER_2023) == Ok(Passenger(rider=RIDER, hr_max=Bpm(190), age=82))


@pytest.mark.parametrize(
    ("rider", "refusal"),
    [
        (Rider(RIDER_ID, RIDER_NAME, None, 1990), HrMaxUnknown()),
        (Rider(RIDER_ID, RIDER_NAME, None, None), HrMaxUnknown()),
        (Rider(RIDER_ID, RIDER_NAME, Bpm(190), None), BirthYearUnknown()),
        (Rider(RIDER_ID, RIDER_NAME, Bpm(190), 2030), AgeUnknowable()),
    ],
    ids=["no-hr-max", "neither", "no-birth-year", "future-birth-year"],
)
def test_a_value_missing_on_the_site_is_a_refusal(rider: Rider, refusal: PassengerRefusal) -> None:
    assert passenger_of(rider, NOVEMBER_2023) == Err(refusal)


# =========================================================================
# In words
# =========================================================================


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (
            NoPassenger(),
            "demarrage refuse : passager requis avec une personne a bord, "
            "a choisir dans la liste du tableau de bord",
        ),
        (
            OnBench(),
            "demarrage refuse : une seance de banc n'a personne a bord, donc aucun passager",
        ),
        (
            NoList(RosterState.WAITING, None),
            "demarrage refuse : liste des passagers pas encore recue du tableau de bord",
        ),
        (
            NoList(RosterState.STALE, Seconds(965.0)),
            "demarrage refuse : liste des passagers perimee : recue il y a 16 min, "
            "au-dela de 15 min personne n'est propose",
        ),
        (
            NoList(RosterState.UNLINKED, None),
            "demarrage refuse : aucun tableau de bord configure sur cette console : "
            "la liste des passagers vient du site",
        ),
        (
            NoList(RosterState.REFUSED, None),
            "demarrage refuse : le tableau de bord refuse la cle de cette machine : "
            "aucun passager n'est propose",
        ),
        (
            NotListed(),
            "demarrage refuse : ce passager n'est pas dans la derniere liste du tableau de bord "
            "(droit retire, ou liste changee) : le choisir de nouveau",
        ),
        (
            HrMaxUnknown(),
            "demarrage refuse : FC max du passager non renseignee sur le site "
            "(a regler par un gestionnaire)",
        ),
        (
            BirthYearUnknown(),
            "demarrage refuse : annee de naissance du passager non renseignee sur le site "
            "(a regler par un gestionnaire)",
        ),
        (
            AgeUnknowable(),
            "demarrage refuse : age du passager incalculable avec l'horloge de cette machine",
        ),
    ],
)
def test_each_refusal_is_said_without_the_rider(refusal: PassengerRefusal, words: str) -> None:
    assert describe_passenger_refusal(refusal) == words


def test_a_current_list_needs_no_words_and_an_undated_stale_one_says_so() -> None:
    assert describe_state(RosterState.CURRENT, Seconds(10.0)) == ""
    assert "recue il y a ? min" in describe_state(RosterState.STALE, None)


def test_an_unknown_refusal_or_state_fails_loudly() -> None:
    with pytest.raises(AssertionError):
        describe_passenger_refusal(cast("PassengerRefusal", object()))
    with pytest.raises(AssertionError):
        describe_state(cast("RosterState", "lost"), None)
