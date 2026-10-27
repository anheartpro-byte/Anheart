"""ANH-239 on the real console: how old the riders' list is, and what a stale one says.

The age of the list held is counted on the console's monotonic clock, never on
its wall clock: a wall clock that jumps (a macOS bench running ahead, a Pi with
no time service yet) neither ages the list nor makes it younger. Past
:data:`~src.roster.ROSTER_MAX_AGE` the list offers nobody, and the sentence
that says so gives its age to the second, rounded up, so a stale list never
reads as 15 minutes old or less.

Each test runs the REAL console in simulation, through the HTTP routes the
page reads and posts to, and through the loop's own check where it applies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest

from src.clock import ManualClock
from src.result import Ok
from src.training.runtime import RuntimeState
from src.units import Monotonic, Seconds, UnixMillis
from src.web.schemas import RosterRow
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    JumpClock,
    Rig,
    SteppedClock,
    attest,
    make_rig,
)
from tests.test_panel_rider import FITTING, start_body
from tests.test_web_api import detail, parse

TWO_HOURS_MS: Final[int] = 2 * 3600 * 1000

STALE_AT_901: Final[str] = (
    "liste des passagers perimee : recue il y a 15 min 1 s, "
    "au-dela de 15 min personne n'est propose"
)
"""What a list held 901 s says, or one held a thousandth of a second past 900 s."""


def console(tmp_path: Path, clock: SteppedClock) -> Rig:
    """The console on ``clock``, programmes and a person on board enabled, holding
    :data:`FITTING` from now."""
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV, clock=clock)
    rig.panel.roster.adopt((FITTING,), clock.monotonic())
    return rig


def offered(roster: RosterRow) -> list[str]:
    return [rider.user_id for rider in roster.riders]


# =========================================================================
# The monotonic clock, never the wall clock
# =========================================================================


async def test_a_wall_clock_that_jumps_neither_ages_nor_rejuvenates_the_list(
    tmp_path: Path,
) -> None:
    """Two hours ahead would make it stale, two hours back would make it unborn: neither."""
    clock = JumpClock()
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV, clock=clock)
    # A list received ten minutes ago on the monotonic clock.
    rig.panel.roster.adopt((FITTING,), Monotonic(clock.monotonic() - 600.0))
    async with rig.http() as session:
        await attest(session)
        before = parse(RosterRow, await session.get("/api/roster"))
        for offset in (TWO_HOURS_MS, -TWO_HOURS_MS, TWO_HOURS_MS):
            clock.wall_offset_ms = offset
            jumped = parse(RosterRow, await session.get("/api/roster"))
            assert (jumped.state, jumped.detail) == ("current", "")
            assert jumped.age_s == before.age_s == 600.0
            assert offered(jumped) == [FITTING.user_id]
        # Wall clock two hours ahead: the start naming the rider is not refused for the list.
        started = await session.post("/api/session/start", json=start_body(FITTING.user_id))
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await rig.tick(1.0)
    assert rig.refusals() == []
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    await rig.panel.close()


async def test_the_monotonic_clock_alone_makes_the_list_stale(tmp_path: Path) -> None:
    """901 s on the monotonic clock with the wall clock held still: stale, at the route
    and in the loop."""
    clock = JumpClock()
    rig = console(tmp_path, clock)
    wall = clock.unix_millis()
    clock.advance(Seconds(901.0))
    clock.wall_offset_ms -= 901_000
    assert clock.unix_millis() == wall, "the wall clock has not moved"
    async with rig.http() as session:
        await attest(session)
        roster = parse(RosterRow, await session.get("/api/roster"))
        refused = await session.post("/api/session/start", json=start_body(FITTING.user_id))
    assert (roster.state, roster.age_s, roster.detail) == ("stale", 901.0, STALE_AT_901)
    assert offered(roster) == []
    assert refused.status_code == httpx.codes.CONFLICT
    assert detail(refused) == f"demarrage refuse : {STALE_AT_901}"
    # Past the route, the loop reads the same list at the same instant.
    await rig.drain()
    assert rig.refusals() == [], "the route's refusal is an answer, not an event"
    asked = rig.panel.surface.submit_start(
        profile_id=SHORT_PROFILE,
        operator=OPERATOR,
        total_duration_s=None,
        subject_id=FITTING.user_id,
    )
    assert isinstance(asked, Ok)
    await rig.panel.control_step()
    await rig.drain()
    assert rig.refusals() == [f"demarrage refuse : {STALE_AT_901}"]
    assert rig.panel.runtime.state is RuntimeState.IDLE
    await rig.panel.close()


# =========================================================================
# The instant a list goes stale, said to the second
# =========================================================================


@pytest.mark.parametrize("age", [899.0, 900.0], ids=["899s", "900s"])
async def test_up_to_fifteen_minutes_the_list_is_offered_with_no_word_of_staleness(
    tmp_path: Path, age: float
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    rig = console(tmp_path, clock)
    clock.advance(Seconds(age))
    async with rig.http() as session:
        await attest(session)
        roster = parse(RosterRow, await session.get("/api/roster"))
        started = await session.post("/api/session/start", json=start_body(FITTING.user_id))
    assert (roster.state, roster.detail, roster.age_s) == ("current", "", age)
    assert offered(roster) == [FITTING.user_id]
    assert started.status_code == httpx.codes.ACCEPTED, started.text
    await rig.panel.close()


@pytest.mark.parametrize(
    ("age", "age_s", "ago"),
    [(900.001, 900.0, "15 min 1 s"), (901.0, 901.0, "15 min 1 s"), (959.5, 959.5, "16 min 0 s")],
    ids=["900.001s", "901s", "959.5s"],
)
async def test_past_fifteen_minutes_the_list_offers_nobody_and_says_its_age_to_the_second(
    tmp_path: Path, age: float, age_s: float, ago: str
) -> None:
    """Rounded up to the second: never "15 min" for a list past 15 minutes."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    rig = console(tmp_path, clock)
    clock.advance(Seconds(age))
    async with rig.http() as session:
        await attest(session)
        roster = parse(RosterRow, await session.get("/api/roster"))
        refused = await session.post("/api/session/start", json=start_body(FITTING.user_id))
    said = (
        f"liste des passagers perimee : recue il y a {ago}, "
        "au-dela de 15 min personne n'est propose"
    )
    assert (roster.state, roster.age_s, roster.detail) == ("stale", age_s, said)
    assert offered(roster) == []
    assert refused.status_code == httpx.codes.CONFLICT
    assert detail(refused) == f"demarrage refuse : {said}"
    await rig.panel.close()
