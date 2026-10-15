"""Riders for the tests: the list a console offers, and the dashboard's answer that gives it.

A programmed session, and a manual one with a person on board, name a rider
of the list the dashboard served (:mod:`src.roster`). A test that starts one
either has its fake dashboard answer :func:`roster_document`, or gives the
console a list already held (:func:`listed`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from src.clock import Clock
from src.roster import Rider, Roster
from src.training.plan import JsonValue
from src.units import Bpm, UnixMillis

RIDER_ID: Final[str] = "k17rider0synthetic"
"""The dashboard's identifier of the synthetic rider: what the record writes as ``subject_id``."""

RIDER_NAME: Final[str] = "Synthetic Rider"
"""Their name: shown on the page, never written."""

RIDER: Final[Rider] = Rider(
    user_id=RIDER_ID,
    name=RIDER_NAME,
    hr_max=Bpm(190),
    birth_year=1940,
    fit=True,
    fitness_valid_until=UnixMillis(4_102_444_800_000),
    consent=True,
    questionnaire_version="0.1.0",
)
"""An adult with both values set, whom every programme of the shipped store fits.

Born in 1940 so as to be an adult on every clock the tests run (29 on a clock
left at 1970, the default of :class:`~src.clock.ManualClock`, and under the
oldest age the console believes today)."""

CHILD: Final[Rider] = Rider(
    user_id="k17child0synthetic",
    name="Synthetic Child",
    hr_max=Bpm(200),
    birth_year=2013,
    fit=True,
    fitness_valid_until=RIDER.fitness_valid_until,
    consent=True,
    questionnaire_version="0.1.0",
)
"""A rider of the list who is 9 on the clock the console rigs run (November 2023):
a programme for them is refused for their age, by the loop."""


def listed(clock: Clock, *riders: Rider) -> Roster:
    """A list held as if the dashboard had just given it: :data:`RIDER` unless others are named."""
    roster = Roster(linked=True)
    roster.adopt(riders or (RIDER,), clock.monotonic())
    return roster


def rider_row(rider: Rider) -> Mapping[str, JsonValue]:
    """One rider as ``GET /api/machine/roster`` serves them."""
    return {
        "userId": rider.user_id,
        "name": rider.name,
        "hrMax": None if rider.hr_max is None else int(rider.hr_max),
        "birthYear": rider.birth_year,
        "fit": rider.fit,
        "fitnessValidUntil": rider.fitness_valid_until,
        "consent": rider.consent,
        "fitnessQuestionnaireVersion": rider.questionnaire_version,
    }


def roster_document(*riders: Rider) -> Mapping[str, JsonValue]:
    """The dashboard's answer listing ``riders``: :data:`RIDER` unless others are named."""
    return {"riders": [dict(rider_row(rider)) for rider in riders or (RIDER,)]}
