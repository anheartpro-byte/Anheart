"""A start asked for under a verdict the runtime latched itself is refused with the click.

The surface's gate reads the supervisor's verdicts alone. The runtime latches
some on its own behalf, here ``sto_open`` deduced from the drive's word after
an opening no reading showed, and a start was then accepted (202) and refused
a tick later by the loop. Each start route now reads the latched floor where
``/api/status`` reads it and refuses before submitting, with the 409 the
surface gives a supervisor's latched verdict: nothing in the mailbox, nothing
armed. After a named acknowledgement on a READY reading the start is taken.

A supervisor's own verdict is still answered by the surface, as before, and
the loop still refuses a start handed to the surface directly under the
runtime's latch.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx
import pytest

from src.control_surface import Command
from src.motor.simulated import SimState
from src.motor.sto import DriveInputStoSource, StoState
from src.presence.monitor import RULE_PRESENCE_CAMERA_LOST
from src.result import Ok
from src.training.runtime import RULE_STO_OPEN, RuntimeState
from src.training.safety import RULE_OPERATOR_ESTOP
from src.training.types import Occupancy, SafetyAction, SafetyVerdict
from src.web.schemas import StatusRow
from tests.roster_support import RIDER_ID
from tests.test_cooldown_freeze_console import START
from tests.test_failure_rig import OPERATOR, SHORT_PROFILE, attest
from tests.test_sto_open_during_stop import Scene, enabled, sim_state, standing
from tests.test_sto_open_unread import (
    PROGRAMME_DRIVE_INPUT,
    PROGRAMME_SIM_READY,
    acknowledge_once_read,
    assert_latched_unread,
    pulse,
    state_of,
    until_the_stop_is_over,
)
from tests.test_web_api import parse

STARTS: Final[Mapping[str, tuple[str, Mapping[str, object]]]] = {
    "programme": ("/api/session/start", START),
    "manual": ("/api/manual/start", {"occupancy": "bench", "operator": OPERATOR}),
}
SOURCES: Final[Mapping[str, Mapping[str, str]]] = {
    "drive_input": PROGRAMME_DRIVE_INPUT,
    "sim_ready": PROGRAMME_SIM_READY,
}


def latched_words(verdict: SafetyVerdict) -> str:
    """The sentence of a start refused under ``verdict``, as the surface's own refusal says it."""
    return (
        f"a latched safety verdict stands ({verdict.rule}): {verdict.detail} "
        "- it must be acknowledged by name first"
    )


def refusal(verdict: SafetyVerdict) -> Mapping[str, object]:
    return {"detail": latched_words(verdict), "code": "verdict_latched", "waiting_request_id": None}


def commands_since(scene: Scene, written: int) -> list[tuple[float, str, int]]:
    """The control words written to the drive since ``written`` writes: an arming writes some."""
    return [write for write in scene.writes[written:] if write[1] == "command"]


def waiting(scene: Scene) -> Command | None:
    """The mailbox, read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return scene.panel.surface.pending


async def latched_at_rest(scene: Scene) -> SafetyVerdict:
    """A manual session, an opening no reading shows, the stop it latches: at rest, unacknowledged.

    The runtime holds ``sto_open`` and the supervisor nothing. No reading is
    taken during the stop: with ``drive_input`` the chain reads UNKNOWN until
    the next one (:func:`read_ready`).
    """
    await scene.cruise()
    opened = await pulse(scene, across_a_tick=False)
    assert_latched_unread(scene, opened)
    await until_the_stop_is_over(scene)
    assert state_of(scene) is RuntimeState.FINISHED
    runtime = scene.runtime
    latched = runtime.floor
    assert latched is not None
    assert latched.rule == RULE_STO_OPEN
    assert runtime.supervisor.floor is None
    assert runtime.supervisor.standing is None
    return latched


async def read_ready(scene: Scene) -> None:
    """The reading after the stop: READY, and the surface's gate alone would take a start."""
    await scene.read()
    assert scene.reading().state is StoState.READY
    assert isinstance(scene.panel.surface.check_start(), Ok)


@pytest.mark.parametrize("source", list(SOURCES))
@pytest.mark.parametrize("start", list(STARTS))
async def test_a_start_under_the_runtime_s_own_latch_is_refused_at_once_until_acknowledged(
    tmp_path: Path, start: str, source: str
) -> None:
    """409 ``verdict_latched`` with the click: nothing in the mailbox, nothing armed; then taken."""
    path, body = STARTS[start]
    scene = Scene(tmp_path, SOURCES[source])
    runtime = scene.runtime
    surface = scene.panel.surface
    latched = await latched_at_rest(scene)
    async with scene.rig.http() as session:
        if isinstance(scene.panel.sto, DriveInputStoSource):
            # Before the next reading the surface refuses for the chain itself, as it always has.
            unread = await session.post(path, json=body)
            assert unread.status_code == httpx.codes.PRECONDITION_FAILED, unread.text
            assert unread.json()["code"] == "sto_not_ready"
        await read_ready(scene)
        shown = parse(StatusRow, await session.get("/api/status")).floor
        assert shown is not None
        assert shown.rule == RULE_STO_OPEN  # the verdict the page shows
        counters = surface.counters
        written = len(scene.writes)
        refused = await session.post(path, json=body)
        assert refused.status_code == httpx.codes.CONFLICT, refused.text
        assert refused.json() == refusal(latched)
        assert surface.pending is None  # nothing was put in the mailbox
        assert surface.counters == counters
        await scene.run(5)
        assert state_of(scene) is RuntimeState.FINISHED
        assert not enabled(runtime)
        assert sim_state(scene) is SimState.SWITCH_ON_DISABLED
        assert commands_since(scene, written) == []
        assert standing(runtime) == latched
        await acknowledge_once_read(scene)
        taken = await session.post(path, json=body)
        assert taken.status_code == httpx.codes.ACCEPTED, taken.text
        await scene.run(5)
        assert state_of(scene) is RuntimeState.RUNNING
    await scene.panel.close()


@pytest.mark.parametrize("start", list(STARTS))
async def test_a_supervisor_s_latched_verdict_alone_is_answered_by_the_surface_as_before(
    tmp_path: Path, start: str
) -> None:
    """The attestation first, then the verdict the surface names, counted by it."""
    path, body = STARTS[start]
    scene = Scene(tmp_path, PROGRAMME_SIM_READY)
    runtime = scene.runtime
    surface = scene.panel.surface
    supervisor = runtime.supervisor
    await scene.run(1)
    runtime.trip_from_thread(RULE_PRESENCE_CAMERA_LOST, SafetyAction.RAMP_DOWN, "under test")
    await scene.run(1)
    floor = supervisor.floor
    assert floor is not None
    assert floor.rule == RULE_PRESENCE_CAMERA_LOST
    assert runtime.floor == floor  # nothing of the runtime's own
    async with scene.rig.http() as session:
        unattested = await session.post(path, json=body)
        assert unattested.status_code == httpx.codes.PRECONDITION_FAILED, unattested.text
        assert unattested.json()["code"] == "estop_unattested"
        await attest(session)
        accepted, refused_count, published = surface.counters
        refused = await session.post(path, json=body)
        assert refused.status_code == httpx.codes.CONFLICT, refused.text
        assert refused.json() == refusal(floor)
        assert surface.counters == (accepted, refused_count + 1, published)  # the surface's answer
        assert surface.pending is None
        estop = await session.post(
            "/api/session/estop", json={"operator": OPERATOR, "reason": "test"}
        )
        assert estop.status_code == httpx.codes.OK, estop.text
        held = supervisor.standing
        assert held is not None
        assert held.rule == RULE_OPERATOR_ESTOP
        assert runtime.floor == floor
        named = await session.post(path, json=body)
        assert named.status_code == httpx.codes.CONFLICT, named.text
        assert named.json() == refusal(held)  # the verdict the surface stands on, not the floor
        assert surface.pending is None
    await scene.panel.close()


@pytest.mark.parametrize("start", list(STARTS))
async def test_the_loop_still_refuses_a_start_handed_to_the_surface_under_the_runtime_s_latch(
    tmp_path: Path, start: str
) -> None:
    """Past the routes, the loop's own refusal stands: REFUSED, nothing armed."""
    scene = Scene(tmp_path, PROGRAMME_DRIVE_INPUT)
    runtime = scene.runtime
    surface = scene.panel.surface
    rig = scene.rig
    await latched_at_rest(scene)
    await read_ready(scene)
    rig.watcher = scene.panel.hub.subscribe()  # a fresh inbox: the scene ticks without reading one
    counters = surface.counters
    written = len(scene.writes)
    # The surface's gate sees no verdict of its own and takes it.
    if start == "manual":
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
    else:
        assert isinstance(
            surface.submit_start(
                profile_id=SHORT_PROFILE,
                operator=OPERATOR,
                total_duration_s=None,
                subject_id=RIDER_ID,
            ),
            Ok,
        )
    assert waiting(scene) is not None
    for _ in range(5):
        await scene.step()
        await scene.read()
        await rig.drain()
    assert waiting(scene) is None
    assert surface.counters[1] == counters[1] + 1
    assert [said for said in rig.refusals() if f"verdict {RULE_STO_OPEN} a acquitter" in said]
    assert state_of(scene) is RuntimeState.FINISHED
    assert not enabled(runtime)
    assert sim_state(scene) is SimState.SWITCH_ON_DISABLED
    assert commands_since(scene, written) == []
    await scene.panel.close()
