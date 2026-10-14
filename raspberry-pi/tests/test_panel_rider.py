"""ANH-84 on the real console: who rides is a rider of the dashboard's list.

A programmed session started at the console names a rider of the list held
when the loop arms it, and is vetted on what the site set for them: their
maximum heart rate (the programme is fitted to it) and their age (from their
birth year). Nothing of either is typed at the console. A manual session with
a person on board names a rider of the list too; a bench session names nobody.
The record carries the rider's identifier, a pseudonym, and never their name.

Each test runs the REAL console in simulation with its session record
(:mod:`tests.record_console_support`), through its HTTP routes where the
operator would click, and through the surface where the route's own check
would answer first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from src.control_surface import LOCAL_SUBJECT
from src.result import Ok
from src.roster import ROSTER_MAX_AGE, Rider
from src.training.runtime import RuntimeState
from src.training.types import Occupancy, SignalQuality
from src.units import Bpm, Seconds
from tests.record_console_support import RecordedRig, recorded_rig
from tests.roster_support import RIDER, RIDER_ID, RIDER_NAME
from tests.test_failure_rig import OPERATOR, PROGRAMME_ENV, SHORT_PROFILE, attest

FITTING: Final[Rider] = Rider(
    user_id="k17fitting0synthetic", name="Fitting Rider", hr_max=Bpm(160), birth_year=1980
)
"""A rider whose maximum heart rate the short programme fits: 90 % of 160 is 144, over its 138."""

TOO_LOW: Final[Rider] = Rider(
    user_id="k17toolow0synthetic", name="Too Low Rider", hr_max=Bpm(150), birth_year=1980
)
"""A rider whose maximum heart rate is under the short programme's zone: 90 % of 150 is 135."""

NO_HR_MAX: Final[Rider] = Rider(
    user_id="k17nohr0synthetic", name="No Hr Rider", hr_max=None, birth_year=1980
)
NO_BIRTH_YEAR: Final[Rider] = Rider(
    user_id="k17noyear0synthetic", name="No Year Rider", hr_max=Bpm(180), birth_year=None
)

EVERYONE: Final[tuple[Rider, ...]] = (RIDER, FITTING, TOO_LOW, NO_HR_MAX, NO_BIRTH_YEAR)


def console(tmp_path: Path) -> RecordedRig:
    """The recording console, programmes and a person on board enabled, holding every rider."""
    recorded = recorded_rig(tmp_path, env=PROGRAMME_ENV)
    recorded.rig.panel.roster.adopt(EVERYONE, recorded.clock.monotonic())
    return recorded


def start_body(rider: str) -> dict[str, object]:
    return {
        "profile_id": SHORT_PROFILE,
        "operator": OPERATOR,
        "total_duration_s": None,
        "subject_id": rider,
    }


def state_of(recorded: RecordedRig) -> RuntimeState:
    return recorded.rig.panel.runtime.state


@dataclass
class Pulse:
    """A heart rate handed to the runtime once a second, as the ECG bridge would."""

    recorded: RecordedRig
    seq: int = 0

    async def beat(self, seconds: int, bpm: int = 97) -> None:
        for _ in range(seconds):
            self.seq += 1
            self.recorded.rig.panel.runtime.observe_ecg(
                self.recorded.clock.monotonic(), self.seq, SignalQuality.GOOD, Bpm(bpm)
            )
            await self.recorded.tick(1.0)


# =========================================================================
# A programmed session at the console
# =========================================================================


async def test_a_programme_runs_for_the_rider_named_and_the_record_names_them_by_pseudonym(
    tmp_path: Path,
) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        started = await session.post("/api/session/start", json=start_body(FITTING.user_id))
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await recorded.tick(1.0)
    assert state_of(recorded) is RuntimeState.RUNNING, recorded.rig.refusals()
    manifest = recorded.recording().manifest
    # The dashboard's identifier, written as it is: the record's pseudonym.
    assert manifest.subject_id == FITTING.user_id
    # The programme was fitted to the rider's maximum heart rate, from the site.
    assert manifest.profile is not None
    assert manifest.profile.subject_hr_max == 160
    on_disk = recorded.everything_on_disk()
    for rider in EVERYONE:
        assert rider.name.encode() not in on_disk, "a rider's name is never written"
    await recorded.rig.panel.close()


async def test_a_programme_whose_zone_is_too_high_for_the_rider_is_refused(
    tmp_path: Path,
) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        answered = await session.post("/api/session/start", json=start_body(TOO_LOW.user_id))
    assert answered.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert "zone_above_subject_ceiling" in answered.text
    assert state_of(recorded) is RuntimeState.IDLE
    await recorded.rig.panel.close()


async def test_the_loop_refits_the_programme_to_the_rider_and_refuses_one_too_high(
    tmp_path: Path,
) -> None:
    """Past the route, the loop resolves the programme for this rider on its own."""
    recorded = console(tmp_path)
    surface = recorded.rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    asked = surface.submit_start(
        profile_id=SHORT_PROFILE,
        operator=OPERATOR,
        total_duration_s=None,
        subject_id=TOO_LOW.user_id,
    )
    assert isinstance(asked, Ok)
    await recorded.tick(1.0)
    assert state_of(recorded) is RuntimeState.IDLE
    (refusal,) = recorded.rig.refusals()
    assert refusal.startswith("demarrage refuse : programme inadapte a ce passager")
    assert recorded.records() == []
    assert TOO_LOW.name.encode() not in recorded.everything_on_disk()
    await recorded.rig.panel.close()


async def test_a_value_the_site_has_not_set_refuses_the_programme_at_once(
    tmp_path: Path,
) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        no_hr = await session.post("/api/session/start", json=start_body(NO_HR_MAX.user_id))
        no_year = await session.post("/api/session/start", json=start_body(NO_BIRTH_YEAR.user_id))
    assert no_hr.status_code == httpx.codes.CONFLICT
    assert "FC max du passager non renseignee sur le site" in no_hr.text
    assert no_year.status_code == httpx.codes.CONFLICT
    assert "annee de naissance du passager non renseignee sur le site" in no_year.text
    assert recorded.rig.panel.surface.pending is None
    await recorded.rig.panel.close()


async def test_past_the_route_the_loop_says_the_value_missing_on_the_site(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    surface = recorded.rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    for rider in (NO_HR_MAX, NO_BIRTH_YEAR):
        asked = surface.submit_start(
            profile_id=SHORT_PROFILE,
            operator=OPERATOR,
            total_duration_s=None,
            subject_id=rider.user_id,
        )
        assert isinstance(asked, Ok)
        await recorded.tick(0.4)
    assert recorded.rig.refusals() == [
        "demarrage refuse : FC max du passager non renseignee sur le site "
        "(a regler par un gestionnaire)",
        "demarrage refuse : annee de naissance du passager non renseignee sur le site "
        "(a regler par un gestionnaire)",
    ]
    assert state_of(recorded) is RuntimeState.IDLE
    await recorded.rig.panel.close()


async def test_a_programme_naming_nobody_is_refused(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        nobody = await session.post(
            "/api/session/start",
            json={"profile_id": SHORT_PROFILE, "operator": OPERATOR, "total_duration_s": None},
        )
    assert nobody.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert "passager requis" in nobody.text
    surface = recorded.rig.panel.surface
    asked = surface.submit_start(profile_id=SHORT_PROFILE, operator=OPERATOR, total_duration_s=None)
    assert isinstance(asked, Ok)
    assert asked.value.subject_id == LOCAL_SUBJECT
    await recorded.tick(0.4)
    assert recorded.rig.refusals()[-1].startswith("demarrage refuse : passager requis")
    await recorded.rig.panel.close()


async def test_an_age_typed_at_the_console_is_refused_and_never_used(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        typed = await session.post(
            "/api/session/start", json={**start_body(RIDER_ID), "subject_age": 30}
        )
    assert typed.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert "ne se saisit plus a la console" in typed.text
    assert recorded.rig.panel.surface.pending is None
    await recorded.rig.panel.close()


async def test_a_rider_of_a_list_too_old_is_refused_until_the_dashboard_answers(
    tmp_path: Path,
) -> None:
    recorded = console(tmp_path)
    recorded.clock.advance(Seconds(ROSTER_MAX_AGE + 60.0))
    async with recorded.rig.http() as session:
        await attest(session)
        stale = await session.post("/api/session/start", json=start_body(RIDER_ID))
        listed = await session.get("/api/roster")
    assert stale.status_code == httpx.codes.CONFLICT
    assert "liste des passagers perimee : recue il y a 61 min" in stale.text
    assert listed.json()["state"] == "stale"
    assert listed.json()["riders"] == []
    await recorded.rig.panel.close()


# =========================================================================
# A manual session
# =========================================================================


async def test_a_manual_session_with_a_person_on_board_names_its_rider_in_the_record(
    tmp_path: Path,
) -> None:
    recorded = console(tmp_path)
    pulse = Pulse(recorded)
    async with recorded.rig.http() as session:
        await pulse.beat(10)
        await attest(session)
        started = await session.post(
            "/api/manual/start",
            json={"occupancy": "occupied", "operator": OPERATOR, "subject_id": RIDER_ID},
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await pulse.beat(2)
    assert state_of(recorded) is RuntimeState.RUNNING, recorded.rig.refusals()
    manifest = recorded.recording().manifest
    assert (manifest.occupancy, manifest.subject_id) == ("occupied", RIDER_ID)
    assert RIDER_NAME.encode() not in recorded.everything_on_disk()
    await recorded.rig.panel.close()


async def test_a_bench_session_names_nobody(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        named = await session.post(
            "/api/manual/start",
            json={"occupancy": "bench", "operator": OPERATOR, "subject_id": RIDER_ID},
        )
        assert named.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        assert "une seance de banc n'a personne a bord" in named.text
        bench = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert bench.status_code == httpx.codes.ACCEPTED, bench.text
        await recorded.tick(0.4)
    assert state_of(recorded) is RuntimeState.RUNNING
    manifest = recorded.recording().manifest
    assert (manifest.occupancy, manifest.subject_id) == ("bench", None)
    await recorded.rig.panel.close()


async def test_the_loop_refuses_a_bench_session_that_names_a_rider(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    surface = recorded.rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    asked = surface.submit_start_manual(
        occupancy=Occupancy.BENCH, operator=OPERATOR, subject_id=RIDER_ID
    )
    assert isinstance(asked, Ok)
    await recorded.tick(0.4)
    assert recorded.rig.refusals() == [
        "demarrage refuse : une seance de banc n'a personne a bord, donc aucun passager"
    ]
    assert state_of(recorded) is RuntimeState.IDLE
    await recorded.rig.panel.close()


async def test_a_person_on_board_must_be_a_rider_the_list_holds(tmp_path: Path) -> None:
    recorded = console(tmp_path)
    async with recorded.rig.http() as session:
        await attest(session)
        nobody = await session.post(
            "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
        )
        stranger = await session.post(
            "/api/manual/start",
            json={"occupancy": "occupied", "operator": OPERATOR, "subject_id": "k17stranger"},
        )
    assert nobody.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert stranger.status_code == httpx.codes.CONFLICT
    assert "n'est pas dans la derniere liste" in stranger.text
    surface = recorded.rig.panel.surface
    asked = surface.submit_start_manual(
        occupancy=Occupancy.OCCUPIED, operator=OPERATOR, subject_id="k17stranger"
    )
    assert isinstance(asked, Ok)
    await recorded.tick(0.4)
    assert recorded.rig.refusals()[-1].startswith(
        "demarrage refuse : ce passager n'est pas dans la derniere liste"
    )
    await recorded.rig.panel.close()
