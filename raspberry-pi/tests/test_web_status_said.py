"""What ``/api/status`` and the frames now say, and the start a faulted drive is refused (ANH-190).

A screen that receives no frame (its socket is down, its requests are still
answered) has ``/api/status`` and nothing else. Until this change that answer
could say less than the machine knew:

* the verdicts were the supervisor's alone, so a verdict the runtime had
  latched on its own behalf was missing;
* ``GO_SILENT`` hid the stop it had replaced, on that screen and on any screen
  opened afterwards;
* nothing told two runs of the console apart;
* the drive's state was not in it at all, and a drive in fault at rest was only
  learnt from a start that the console accepted and the loop then refused.

The first half runs on the web rig of ``tests/test_web_api.py`` (a supervisor,
a surface, no runtime): the shapes, the identifier, the age of the drive's
state, the refusal of a start. The second half runs on the REAL console
(:func:`src.local_panel.build_panel`, the rig of ``tests/test_failure_rig.py``)
through its HTTP API: there the status is read where every latch is seen.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import TypeAdapter

from src.clock import ManualClock
from src.control_surface import EventKind, RunState
from src.motor.drive import DriveFault, DriveState, describe_fault
from src.motor.simulated import SimState
from src.training.runtime import RULE_DRIVE_PRECOMMANDED, RuntimeState
from src.training.safety import RULE_COMMS_LOST, RULE_REVERSE_ROTATION
from src.training.types import DRIVE_STATUS_STALE_AFTER, Phase, RunMode, SafetyAction
from src.units import Monotonic, RawRegister, Seconds, UnixMillis
from src.web.deps import Services, new_instance
from src.web.routes import DRIVE_FAULT_DETAIL
from src.web.schemas import SnapshotRow, StatusRow, WsEnvelope
from tests.test_failure_rig import OPERATOR as CONSOLE_OPERATOR
from tests.test_failure_rig import TICK, attest, make_rig, precommanded
from tests.test_failure_rig import Rig as ConsoleRig
from tests.test_web_api import (
    GEOMETRY,
    OPERATOR,
    ORIGIN,
    PROFILE_ID,
    TOKEN,
    Rig,
    StubPorts,
    auth,
    build_rig,
    detail,
    make_observation,
    make_snapshot,
    parse,
    probe_socket,
)

INSTANCE: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{32}\Z")
OBF: Final[RawRegister] = RawRegister(18)
"""LFT 18: ObF, the overvoltage on the DC bus the guide shows."""


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return build_rig(tmp_path)


def _client(rig: Rig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver"
    )


async def _status(
    session: httpx.AsyncClient, headers: Mapping[str, str] | None = None
) -> StatusRow:
    return parse(StatusRow, await session.get("/api/status", headers=headers))


def _state(console: ConsoleRig) -> RuntimeState:
    """Read through a call, so a checker does not narrow one assertion into the next."""
    return console.panel.runtime.state


# =========================================================================
# The run of the console, in every answer and every frame
# =========================================================================


def test_a_run_is_named_by_chance_and_two_runs_never_share_a_name() -> None:
    drawn = {new_instance() for _ in range(200)}
    assert len(drawn) == 200
    assert all(INSTANCE.fullmatch(name) for name in drawn)


async def test_the_status_names_the_run_of_the_console_and_keeps_naming_the_same_one(
    rig: Rig,
) -> None:
    """Item 3: a value that changes at each start of the console, and never otherwise."""
    async with _client(rig) as session:
        first = await _status(session, auth())
        rig.clock.advance(Seconds(3600.0))
        rig.surface.publish(make_snapshot())
        later = await _status(session, auth())
    assert INSTANCE.fullmatch(first.instance)
    assert later.instance == first.instance


async def test_another_console_has_another_name(tmp_path: Path) -> None:
    """Two consoles, as before and after a restart: what each holds is its own."""
    before = build_rig(tmp_path / "before")
    after = build_rig(tmp_path / "after")
    async with _client(before) as old, _client(after) as new:
        assert (await _status(old, auth())).instance != (await _status(new, auth())).instance


def test_the_name_is_drawn_once_for_one_set_of_services(rig: Rig) -> None:
    services = Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=GEOMETRY,
    )
    drawn = services.instance
    assert INSTANCE.fullmatch(drawn)
    assert services.instance == drawn


async def test_every_frame_and_the_snapshot_route_carry_the_name_the_status_gives(rig: Rig) -> None:
    """One name for the socket and for the requests: a screen compares the two."""
    rig.surface.publish(make_snapshot())
    sent = await probe_socket(rig.app, origin=ORIGIN, token=TOKEN)
    frames = [message["text"] for message in sent if message["type"] == "websocket.send"]
    envelopes = [TypeAdapter(WsEnvelope).validate_json(str(text)) for text in frames]
    snapshots = [envelope.snapshot for envelope in envelopes if envelope.snapshot is not None]
    assert snapshots, "no snapshot frame was sent"
    async with _client(rig) as session:
        named = (await _status(session, auth())).instance
        latest = parse(SnapshotRow, await session.get("/api/snapshot", headers=auth()))
    assert {snapshot.instance for snapshot in snapshots} == {named}
    assert latest.instance == named


def test_a_snapshot_rendered_with_no_name_says_so_with_an_empty_one() -> None:
    """The record a test or a tool renders by hand: no run is claimed for it."""
    assert SnapshotRow.of(make_snapshot()).instance == ""
    assert SnapshotRow.of(make_snapshot(), "abc").instance == "abc"


# =========================================================================
# What GO_SILENT stands over
# =========================================================================


async def test_the_status_names_the_stop_that_go_silent_replaced(rig: Rig) -> None:
    """Item 2: read from the supervisor alone when no runtime was handed in."""
    supervisor = rig.supervisor
    supervisor.trip_from_thread(RULE_REVERSE_ROTATION, SafetyAction.QUICK_STOP, "under test")
    rig.clock.advance(Seconds(0.2))
    supervisor.evaluate(make_observation(at=rig.clock.monotonic()))
    async with _client(rig) as session:
        stopped = await _status(session, auth())
        assert stopped.silenced is None
        assert stopped.standing is not None
        assert stopped.standing.action == "quick_stop"

        supervisor.trip_from_thread(RULE_COMMS_LOST, SafetyAction.GO_SILENT, "under test")
        rig.clock.advance(Seconds(0.2))
        supervisor.evaluate(make_observation(at=rig.clock.monotonic()))
        silent = await _status(session, auth())
    assert silent.standing is not None
    assert silent.floor is not None
    assert (silent.standing.action, silent.floor.action) == ("go_silent", "go_silent")
    behind = silent.silenced
    assert behind is not None
    assert (behind.rule, behind.action, behind.latched) == (
        RULE_REVERSE_ROTATION,
        "quick_stop",
        True,
    )
    # It is the stop that was shown before, to its instant: its age went on counting.
    assert stopped.standing.age_s is not None
    assert behind.age_s is not None
    assert behind.age_s == pytest.approx(stopped.standing.age_s + 0.2)


async def test_go_silent_alone_names_nothing_behind_it(rig: Rig) -> None:
    """A machine at rest (no rule fires on it), then the trip: nothing was latched before."""
    rig.supervisor.trip_from_thread(RULE_COMMS_LOST, SafetyAction.GO_SILENT, "under test")
    at_rest = replace(make_observation(at=rig.clock.monotonic()), phase=Phase.DONE)
    rig.supervisor.evaluate(at_rest)
    async with _client(rig) as session:
        silent = await _status(session, auth())
    assert silent.standing is not None
    assert silent.standing.action == "go_silent"
    assert silent.silenced is None


# =========================================================================
# The drive's state, for a screen that receives no frame
# =========================================================================


async def test_before_the_first_tick_the_drive_is_unknown_not_healthy(rig: Rig) -> None:
    async with _client(rig) as session:
        status = await _status(session, auth())
    assert status.drive_state == "comm_lost"
    assert status.drive_fault is None


async def test_the_status_carries_the_drive_fault_the_last_tick_read(rig: Rig) -> None:
    """EX-8: where a screen with no frame learns it, before anybody clicks."""
    rig.surface.publish(
        make_snapshot(
            at=rig.clock.monotonic(), drive_state=DriveState.FAULT, fault=describe_fault(OBF)
        )
    )
    async with _client(rig) as session:
        status = await _status(session, auth())
    assert status.drive_state == "fault"
    fault = status.drive_fault
    assert fault is not None
    assert (fault.mnemonic, fault.raw_code) == ("ObF", 18)
    assert fault.name == DriveFault.DC_BUS_OVERVOLTAGE.name.lower()


async def test_a_healthy_drive_is_said_healthy(rig: Rig) -> None:
    rig.surface.publish(
        make_snapshot(at=rig.clock.monotonic(), drive_state=DriveState.SWITCH_ON_DISABLED)
    )
    async with _client(rig) as session:
        status = await _status(session, auth())
    assert status.drive_state == "switch_on_disabled"
    assert status.drive_fault is None


async def test_a_reading_too_old_to_be_evidence_is_said_unknown_fault_or_not(rig: Rig) -> None:
    """A loop that has stopped leaves its last snapshot behind: it is not the present.

    The reading was 0.2 s old when the snapshot was taken. It stays evidence
    for as long as its age, counted to now, is within the bound a snapshot
    itself applies; past it the state is ``comm_lost``, never a fault nobody
    has read lately, and never a healthy state either.
    """
    taken = rig.clock.monotonic()
    rig.surface.publish(
        make_snapshot(
            at=taken,
            drive_state=DriveState.FAULT,
            drive_age=Seconds(0.2),
            fault=describe_fault(OBF),
        )
    )
    async with _client(rig) as session:
        rig.clock.advance(Seconds(DRIVE_STATUS_STALE_AFTER - 0.2))
        still = await _status(session, auth())
        assert still.drive_state == "fault"
        rig.clock.advance(Seconds(0.01))
        gone = await _status(session, auth())
    assert gone.drive_state == "comm_lost"
    assert gone.drive_fault is None


async def test_a_drive_never_read_is_unknown_whatever_the_snapshot_holds(rig: Rig) -> None:
    rig.surface.publish(
        make_snapshot(at=rig.clock.monotonic(), drive_state=DriveState.COMM_LOST, drive_age=None)
    )
    async with _client(rig) as session:
        status = await _status(session, auth())
    assert status.drive_state == "comm_lost"


# =========================================================================
# A start, while the drive shows a fault
# =========================================================================


def _start_body() -> dict[str, object]:
    return {"profile_id": PROFILE_ID, "operator": OPERATOR, "subject_age": 30}


async def test_a_programme_start_is_refused_at_once_while_the_drive_shows_a_fault(
    rig: Rig,
) -> None:
    """EX-8. 409 with the reason, and nothing is put in the mailbox."""
    rig.attest()
    rig.surface.publish(
        make_snapshot(
            at=rig.clock.monotonic(), drive_state=DriveState.FAULT, fault=describe_fault(OBF)
        )
    )
    counted = rig.surface.counters
    async with _client(rig) as session:
        refused = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert refused.status_code == HTTPStatus.CONFLICT
    said = detail(refused)
    assert said == DRIVE_FAULT_DETAIL.format(code="ObF, LFT 18")
    assert said.startswith("demarrage refuse : variateur en defaut (ObF, LFT 18)")
    assert "Rien n'a ete envoye a la machine" in said
    # Nothing was asked of the loop: the surface was not even handed the request.
    assert rig.surface.pending is None
    assert rig.surface.run_state is RunState.IDLE
    assert rig.surface.counters == counted


async def test_a_fault_whose_code_was_not_read_is_refused_all_the_same(rig: Rig) -> None:
    rig.attest()
    rig.surface.publish(
        make_snapshot(at=rig.clock.monotonic(), drive_state=DriveState.FAULT, fault=None)
    )
    async with _client(rig) as session:
        refused = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert refused.status_code == HTTPStatus.CONFLICT
    assert detail(refused).startswith("demarrage refuse : variateur en defaut (?)")


@pytest.mark.parametrize(
    "state",
    [
        DriveState.SWITCH_ON_DISABLED,
        DriveState.READY,
        DriveState.SWITCHED_ON,
        DriveState.NOT_READY,
        DriveState.COMM_LOST,
    ],
)
async def test_a_drive_that_shows_no_fault_never_has_a_start_refused_for_one(
    rig: Rig, state: DriveState
) -> None:
    """A healthy machine is not refused, and neither is one whose state is unknown."""
    rig.attest()
    rig.surface.publish(make_snapshot(at=rig.clock.monotonic(), drive_state=state))
    async with _client(rig) as session:
        taken = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert taken.status_code == HTTPStatus.ACCEPTED, taken.text


async def test_before_the_first_tick_a_start_is_not_refused_for_a_fault_nobody_read(
    rig: Rig,
) -> None:
    rig.attest()
    async with _client(rig) as session:
        taken = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert taken.status_code == HTTPStatus.ACCEPTED, taken.text


async def test_a_fault_read_too_long_ago_refuses_nothing(rig: Rig) -> None:
    """Unknown is left to the loop, which reads the drive itself before arming."""
    rig.attest()
    rig.surface.publish(
        make_snapshot(
            at=rig.clock.monotonic(),
            drive_state=DriveState.FAULT,
            drive_age=Seconds(0.2),
            fault=describe_fault(OBF),
        )
    )
    rig.clock.advance(Seconds(DRIVE_STATUS_STALE_AFTER))
    async with _client(rig) as session:
        taken = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert taken.status_code == HTTPStatus.ACCEPTED, taken.text


async def test_a_fault_that_has_cleared_no_longer_refuses(rig: Rig) -> None:
    rig.attest()
    rig.surface.publish(
        make_snapshot(
            at=rig.clock.monotonic(), drive_state=DriveState.FAULT, fault=describe_fault(OBF)
        )
    )
    async with _client(rig) as session:
        refused = await session.post("/api/session/start", headers=auth(), json=_start_body())
        assert refused.status_code == HTTPStatus.CONFLICT
        rig.clock.advance(Seconds(0.2))
        rig.surface.publish(
            make_snapshot(at=rig.clock.monotonic(), drive_state=DriveState.SWITCH_ON_DISABLED)
        )
        taken = await session.post("/api/session/start", headers=auth(), json=_start_body())
    assert taken.status_code == HTTPStatus.ACCEPTED, taken.text


async def test_what_cannot_be_started_anyway_is_still_told_its_own_reason_first(rig: Rig) -> None:
    """A request that is wrong in itself is answered as it was: the fault comes after."""
    rig.attest()
    rig.surface.publish(
        make_snapshot(
            at=rig.clock.monotonic(), drive_state=DriveState.FAULT, fault=describe_fault(OBF)
        )
    )
    async with _client(rig) as session:
        nameless = await session.post(
            "/api/session/start", headers=auth(), json={"profile_id": PROFILE_ID, "operator": " "}
        )
        unknown = await session.post(
            "/api/session/start", headers=auth(), json={"profile_id": "nope", "operator": OPERATOR}
        )
    assert nameless.status_code == HTTPStatus.BAD_REQUEST
    assert unknown.status_code == HTTPStatus.NOT_FOUND


# =========================================================================
# On the real console
# =========================================================================


async def test_a_fault_at_rest_is_said_and_refused_before_the_loop_is_asked_anything(
    tmp_path: Path,
) -> None:
    """EX-8, end to end, on a console that has armed nothing.

    The fault is injected into the simulated drive at rest. No verdict
    appears, as before: the idle console only reads. What changes is what the
    operator is told. The status says the drive is in fault, with its code. A
    manual start is answered 409 with that reason at once; before, it was
    answered 202 and refused a tick later by the loop, which then left a
    latched ``drive_fault`` to acknowledge on top of the fault. Here nothing
    latches. Once the fault is reset, the very next start is taken.
    """
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await attest(session)
        await rig.tick(1.0)
        healthy = await _status(session)
        assert healthy.drive_state == "switch_on_disabled"
        assert healthy.drive_fault is None

        rig.simulator.inject_fault(DriveFault.DC_BUS_OVERVOLTAGE)
        await rig.tick(1.0)
        faulted = await _status(session)
        assert faulted.drive_state == "fault"
        assert faulted.drive_fault is not None
        assert faulted.drive_fault.mnemonic == "ObF"
        assert faulted.standing is None, "a verdict was raised at rest: a rule was added"
        assert faulted.floor is None

        refused = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": CONSOLE_OPERATOR}
        )
        assert refused.status_code == HTTPStatus.CONFLICT
        assert detail(refused).startswith("demarrage refuse : variateur en defaut (ObF, LFT 18)")
        await rig.tick(2.0)
        runtime = rig.panel.runtime
        assert _state(rig) is RuntimeState.IDLE
        assert runtime.standing is None, "the refused start reached the loop and latched"
        assert rig.refusals() == [], "the loop was asked, and refused"
        assert not [event for event in rig.events if event.kind is EventKind.START_REQUESTED]
        after = await _status(session)
        assert after.pending is None
        assert after.run_state == "idle"

        # The operator resets the fault: the console says so, and the next start is taken.
        reset = await session.post("/api/drive/fault-reset", json={"operator": CONSOLE_OPERATOR})
        assert reset.status_code == HTTPStatus.ACCEPTED
        await rig.tick(1.5)
        assert rig.simulator.sim_state is not SimState.FAULT
        cleared = await _status(session)
        assert cleared.drive_state != "fault"
        assert cleared.drive_fault is None
        assert cleared.standing is None
        taken = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": CONSOLE_OPERATOR}
        )
        assert taken.status_code == HTTPStatus.ACCEPTED, taken.text
        await rig.tick(1.0)
        assert _state(rig) is RuntimeState.RUNNING
        assert runtime.mode is RunMode.MANUEL
    await rig.panel.close()


async def test_a_healthy_console_takes_its_first_start(tmp_path: Path) -> None:
    """The gate reads a fault and nothing else: a console with none starts as it did."""
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await attest(session)
        await rig.tick(0.4)
        taken = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": CONSOLE_OPERATOR}
        )
        assert taken.status_code == HTTPStatus.ACCEPTED, taken.text
        await rig.tick(1.0)
        assert rig.panel.runtime.mode is RunMode.MANUEL
    await rig.panel.close()


async def test_a_stop_the_runtime_latched_itself_is_in_the_status_with_no_frame_to_show_it(
    tmp_path: Path,
) -> None:
    """Item 1. A drive found running at start-up: the runtime's own QUICK_STOP.

    The supervisor holds nothing of it, and the status read the supervisor:
    ``standing`` and ``floor`` were null under a latched stop. They now carry
    it, as the frames always did, so a screen with no live frame lists the
    verdict and raises its banner.
    """
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    rig, _ = make_rig(tmp_path, simulator=await precommanded(clock, 120), clock=clock)
    async with rig.http() as session:
        await rig.tick(2.0)
        runtime = rig.panel.runtime
        own = runtime.standing
        assert own is not None
        assert own.rule == RULE_DRIVE_PRECOMMANDED
        assert runtime.supervisor.standing is None
        assert runtime.supervisor.floor is None
        status = await _status(session)
        frame = parse(SnapshotRow, await session.get("/api/snapshot"))
    for row in (status.standing, status.floor):
        assert row is not None
        assert (row.rule, row.action, row.latched) == (RULE_DRIVE_PRECOMMANDED, "quick_stop", True)
    # The status says what a frame of that instant says.
    shown = frame.safety
    assert shown is not None
    assert (shown.rule, shown.action, shown.latched) == (
        RULE_DRIVE_PRECOMMANDED,
        "quick_stop",
        True,
    )
    assert status.instance == frame.instance
    assert status.silenced is None
    await rig.panel.close()


async def test_a_rule_s_stop_replaced_by_go_silent_is_still_in_the_status_of_that_console(
    tmp_path: Path,
) -> None:
    """Item 2, on the console: reverse rotation, then the link to the drive lost.

    ``GO_SILENT`` takes the standing verdict and the floor, and refuses every
    acknowledgement. A page that watched saw the stop first; one opened now
    reads it in ``silenced``.
    """
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": CONSOLE_OPERATOR}
        )
        assert started.status_code == HTTPStatus.ACCEPTED
        await rig.tick(1.0)
        rig.simulator.inject_reverse()
        target = await session.post(
            "/api/manual/target", json={"output_rpm": 10.0, "operator": CONSOLE_OPERATOR}
        )
        assert target.status_code == HTTPStatus.ACCEPTED
        for _ in range(round(60.0 / TICK)):
            await rig.tick(TICK)
            if rig.panel.runtime.standing is not None:
                break
        stopped = await _status(session)
        assert stopped.standing is not None
        assert (stopped.standing.rule, stopped.standing.action) == (
            RULE_REVERSE_ROTATION,
            "quick_stop",
        )
        assert stopped.silenced is None

        rig.simulator.inject_comms_loss(Seconds(3600.0))
        await rig.tick(3.0)
        silent = await _status(session)
        assert silent.standing is not None
        assert silent.floor is not None
        assert (silent.standing.rule, silent.standing.action) == (RULE_COMMS_LOST, "go_silent")
        assert silent.floor.action == "go_silent"
        behind = silent.silenced
        assert behind is not None
        assert (behind.rule, behind.action, behind.latched) == (
            RULE_REVERSE_ROTATION,
            "quick_stop",
            True,
        )
        refused = await session.post(
            "/api/safety/acknowledge", json={"operator": CONSOLE_OPERATOR, "estop_released": True}
        )
        assert refused.status_code == HTTPStatus.CONFLICT
        await rig.tick(5.0)
        later = await _status(session)
        assert later.silenced is not None
        assert later.silenced.rule == RULE_REVERSE_ROTATION
        assert later.instance == stopped.instance
    await rig.panel.close()
