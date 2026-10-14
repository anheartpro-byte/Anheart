"""ANH-84 at the HTTP routes: the riders' list the page offers, and what a start may name.

The page reads the list at ``GET /api/roster``: who the dashboard lists, with
the age and the maximum heart rate the site set, and for each whether a
programme can be vetted for them. A start names a rider of that list; a
preview names one to see the programme fitted to them. An interface with no
list offers nobody, and refuses a start with somebody on board.
"""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx

from src.control_surface import StartManual, StartSession
from src.result import Ok, Result
from src.roster import Rider, Roster
from src.training.types import Occupancy, OccupancyRefused
from src.units import Bpm, MotorRpm
from src.web.app import create_app
from src.web.deps import Services
from src.web.schemas import PlanPreviewRow, RosterRow
from tests.roster_support import RIDER, RIDER_ID, RIDER_NAME, listed
from tests.test_web_api import (
    GEOMETRY,
    OPERATOR,
    PROFILE_ID,
    Rig,
    StubPorts,
    auth,
    build_rig,
    detail,
    parse,
)

UNSET: Final[Rider] = Rider(
    user_id="k17unset0synthetic", name="Unset Rider", hr_max=None, birth_year=1950
)
TOO_LOW: Final[Rider] = Rider(
    user_id="k17toolow0synthetic", name="Too Low Rider", hr_max=Bpm(150), birth_year=1950
)


def _ceiling(_occupancy: Occupancy) -> Result[MotorRpm, OccupancyRefused]:
    return Ok(MotorRpm(300))


def client(rig: Rig, roster: Roster | None) -> httpx.AsyncClient:
    """The routes of ``rig``, over ``roster``: a person on board allowed, programmes too."""
    services = Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=GEOMETRY,
        ceilings=_ceiling,
        roster=roster,
    )
    app = create_app(services=services, config=rig.config)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=auth()
    )


async def test_the_list_is_served_with_the_site_s_values_and_what_a_programme_needs(
    tmp_path: Path,
) -> None:
    rig = build_rig(tmp_path)
    async with client(rig, listed(rig.clock, RIDER, UNSET)) as session:
        served = parse(RosterRow, await session.get("/api/roster"))
    assert (served.state, served.detail, served.problem) == ("current", "", None)
    assert served.age_s == 0.0
    adult, unset = served.riders
    # The rig's clock is left in 1970: born in 1940, 29 there.
    assert (adult.user_id, adult.name, adult.hr_max, adult.age, adult.programme) == (
        RIDER_ID,
        RIDER_NAME,
        190,
        29,
        None,
    )
    assert (unset.hr_max, unset.age) == (None, 19)
    assert unset.programme == (
        "demarrage refuse : FC max du passager non renseignee sur le site "
        "(a regler par un gestionnaire)"
    )


async def test_a_rider_without_a_birth_year_is_served_without_an_age(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    no_year = Rider(user_id="k17noyear", name="No Year", hr_max=Bpm(180), birth_year=None)
    async with client(rig, listed(rig.clock, no_year)) as session:
        served = parse(RosterRow, await session.get("/api/roster"))
    (row,) = served.riders
    assert (row.age, row.hr_max) == (None, 180)
    assert row.programme is not None
    assert "annee de naissance" in row.programme


async def test_an_interface_without_a_list_offers_nobody(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    rig.attest()
    async with client(rig, None) as session:
        served = parse(RosterRow, await session.get("/api/roster"))
        programme = await session.post(
            "/api/session/start",
            json={"profile_id": PROFILE_ID, "operator": OPERATOR, "subject_id": RIDER_ID},
        )
        occupied = await session.post(
            "/api/manual/start",
            json={"occupancy": "occupied", "operator": OPERATOR, "subject_id": RIDER_ID},
        )
        bench = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
    assert (served.state, served.riders, served.age_s) == ("unlinked", (), None)
    assert served.detail.startswith("aucun tableau de bord configure")
    for refused in (programme, occupied):
        assert refused.status_code == HTTPStatus.CONFLICT
        assert detail(refused).startswith(
            "demarrage refuse : aucun tableau de bord configure sur cette console"
        )
    # The bench names nobody: it starts without a list.
    assert bench.status_code == HTTPStatus.ACCEPTED, bench.text


async def test_a_preview_for_a_rider_is_the_programme_fitted_to_their_max_heart_rate(
    tmp_path: Path,
) -> None:
    rig = build_rig(tmp_path)
    async with client(rig, listed(rig.clock, RIDER, UNSET, TOO_LOW)) as session:
        stored = parse(
            PlanPreviewRow,
            await session.post("/api/plan/preview", json={"profile_id": PROFILE_ID}),
        )
        fitted = parse(
            PlanPreviewRow,
            await session.post(
                "/api/plan/preview", json={"profile_id": PROFILE_ID, "subject_id": RIDER_ID}
            ),
        )
        unset = await session.post(
            "/api/plan/preview", json={"profile_id": PROFILE_ID, "subject_id": UNSET.user_id}
        )
        too_low = await session.post(
            "/api/plan/preview", json={"profile_id": PROFILE_ID, "subject_id": TOO_LOW.user_id}
        )
        stranger = await session.post(
            "/api/plan/preview", json={"profile_id": PROFILE_ID, "subject_id": "k17stranger"}
        )
    assert stored.profile.subject_hr_max == 162
    assert fitted.profile.subject_hr_max == 190
    assert unset.status_code == HTTPStatus.CONFLICT
    assert "FC max du passager non renseignee" in detail(unset)
    assert too_low.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "zone_above_subject_ceiling" in detail(too_low)
    assert stranger.status_code == HTTPStatus.CONFLICT
    assert "n'est pas dans la derniere liste" in detail(stranger)


async def test_a_start_names_its_rider_and_nothing_else_of_them(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    rig.attest()
    async with client(rig, listed(rig.clock)) as session:
        started = await session.post(
            "/api/session/start",
            json={"profile_id": PROFILE_ID, "operator": OPERATOR, "subject_id": RIDER_ID},
        )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    pending = rig.surface.pending
    assert isinstance(pending, StartSession)
    assert pending.request_id == started.json()["request_id"]
    # The rider by identifier; the maximum heart rate and the age are read by the
    # loop from the list when it arms, never carried from the route.
    assert (pending.subject_id, pending.subject_hr_max, pending.subject_age) == (
        RIDER_ID,
        None,
        None,
    )
    assert RIDER_NAME not in started.text


async def test_an_occupied_manual_start_carries_its_rider(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    rig.attest()
    async with client(rig, listed(rig.clock)) as session:
        started = await session.post(
            "/api/manual/start",
            json={"occupancy": "occupied", "operator": OPERATOR, "subject_id": RIDER_ID},
        )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    pending = rig.surface.pending
    assert isinstance(pending, StartManual)
    assert (pending.occupancy, pending.subject_id) == (Occupancy.OCCUPIED, RIDER_ID)
