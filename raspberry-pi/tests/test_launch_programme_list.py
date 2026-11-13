"""Contract 1.12 on the console: an automatic launch tied to the programme list it was made from.

The console sends the digest of its list with its programmes, sends the one
it holds with the start it confirms, and never arms an automatic launch made
from another list, nor one made without a digest, whatever the dashboard
answers: such a launch is failed, to be launched again. A bench launch
carries no programme and is confirmed as before.
"""

from __future__ import annotations

from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Final

from src.cloud_sync import ARMING_REFUSAL_WORDS
from src.contract import PROGRAMME_CHANGED
from src.remote_launch import LaunchStage
from src.training.plan import JsonValue, StoreContent, profiles_digest
from src.training.runtime import RuntimeState
from src.units import Bpm
from tests.test_cloud_sync import (
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    confirm_linked,
    launch_answer,
    linked,
)
from tests.test_remote_bench_launch import BENCH, confirm_over_http
from tests.test_web_api import SHIPPED_DIGEST

PROFILES: Final[str] = "/api/machine/profiles"
END: Final[str] = "/api/machine/training/end"


async def test_the_programmes_go_with_the_digest_of_their_list_and_go_again_when_it_changes(
    tmp_path: Path,
) -> None:
    r = linked(tmp_path)
    await r.run(0.2)
    (pushed,) = r.dashboard.to(PROFILES)
    assert pushed["profilesDigest"] == SHIPPED_DIGEST
    # The same revision, another content (a programme set aside in memory, as a
    # start's screening does): its digest is new, and said.
    store = r.panel.services.store
    kept = store.list_profiles()[:1]
    store.adopt(StoreContent(rev=store.rev, profiles=kept, catalog=store.catalog))
    await r.run(20.0)
    assert [body["profilesDigest"] for body in r.dashboard.to(PROFILES)] == [
        SHIPPED_DIGEST,
        profiles_digest(kept),
    ]
    await r.panel.close()


async def test_a_launch_made_from_the_list_held_is_confirmed_with_it_and_runs(
    tmp_path: Path,
) -> None:
    r = linked(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(1.8)
    (confirmed,) = r.dashboard.to(START)
    assert confirmed["profilesDigest"] == SHIPPED_DIGEST
    assert r.panel.runtime.state is RuntimeState.RUNNING
    assert r.dashboard.to(END) == []
    await r.panel.close()


async def _armed_by_the_dashboard(
    tmp_path: Path, launch: dict[str, JsonValue], *, before_poll: bool = False
) -> None:
    """A dashboard that arms whatever it is sent: the console alone stands between."""
    r = linked(tmp_path)
    if before_poll:
        # R2's case: the console takes another content under the same id at rest,
        # after the launch was made and before its poll brings it.
        store = r.panel.services.store
        changed = tuple(
            replace(p, zone_low_bpm=Bpm(int(p.zone_low_bpm) + 2)) for p in store.list_profiles()
        )
        store.adopt(StoreContent(rev=store.rev, profiles=changed, catalog=store.catalog))
    r.dashboard.answer(POLL_PATH, launch_answer(launch))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(2.8)
    assert r.panel.runtime.state is RuntimeState.IDLE
    assert r.panel.surface.pending is None
    (confirmed,) = r.dashboard.to(START)
    assert confirmed["profilesDigest"] == profiles_digest(r.panel.services.store.list_profiles())
    words = ARMING_REFUSAL_WORDS[PROGRAMME_CHANGED]
    (ended,) = r.dashboard.to(END)
    assert (ended["sessionId"], ended["failed"]) == ("remote-1", True)
    assert ended["reason"] == f"refusee par la machine : {words}"
    cloud = r.panel.cloud
    assert cloud is not None
    offer = cloud.current_remote_launch()
    assert offer is not None
    assert (offer.stage, offer.reason) == (LaunchStage.REFUSED, words)
    await r.panel.close()


async def test_a_launch_made_from_another_list_is_never_armed(tmp_path: Path) -> None:
    await _armed_by_the_dashboard(tmp_path, {**LAUNCH, "profilesDigest": "0" * 64})


async def test_a_launch_made_before_the_console_took_another_content_is_never_armed(
    tmp_path: Path,
) -> None:
    await _armed_by_the_dashboard(tmp_path, dict(LAUNCH), before_poll=True)


async def test_a_launch_made_without_a_digest_is_never_armed(tmp_path: Path) -> None:
    """Made before the deployment of 1.12, or for a console that sent none: refused."""
    for n, launch in enumerate(
        (
            {key: value for key, value in LAUNCH.items() if key != "profilesDigest"},
            {**LAUNCH, "profilesDigest": None},
            {**LAUNCH, "profilesDigest": 12},
        )
    ):
        folder = tmp_path / f"launch-{n}"
        folder.mkdir()
        await _armed_by_the_dashboard(folder, launch)


async def test_a_bench_launch_carries_no_programme_and_is_confirmed_with_no_list(
    tmp_path: Path,
) -> None:
    r = linked(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.run(0.2)
    attested = await confirm_over_http(r, OPERATOR, attested=True)
    assert attested.status_code == HTTPStatus.ACCEPTED
    await r.run(1.8)
    (confirmed,) = r.dashboard.to(START)
    assert "profilesDigest" not in confirmed
    assert r.panel.runtime.state is RuntimeState.RUNNING
    await r.panel.close()
