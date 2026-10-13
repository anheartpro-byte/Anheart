"""ANH-190: one request, one fate, and the console says it.

Several screens can ask at once, and the real drive is awaited inside the
control tick: two requests can meet in one tick. Before this, a start accepted
while a tick was under way was forgotten by the surface at the end of that tick
(it read ``idle`` again), a second start was then accepted over the first, and
nobody was told which of the two the machine ran. And nothing the console
published said which request an answer was about.

Three layers are pinned here, each on the real code:

* the mailbox alone (:class:`~src.control_surface.ControlSurface`): the table
  of what a request meets, every pair of requests that can meet, and the one
  fate each accepted request ends with;
* the HTTP layer: the identifier in every 202, the code and the fixed sentence
  of every refusal, and nothing of another operator's request in any of them;
* the REAL console (:func:`src.local_panel.build_panel`) on a simulated drive
  whose exchanges wait where the test says (:class:`Gated`), as an exchange on
  a serial line does: the races are replayed deterministically, with the tick
  suspended inside the drive while the requests arrive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast, override

import httpx
import pytest
from pydantic import TypeAdapter
from starlette.requests import Request

from src.control_surface import (
    CANCELLED_BY_STOP,
    FATES_KEPT,
    Command,
    ControlSurface,
    EndSession,
    EventKind,
    Fate,
    FaultReset,
    NothingRunning,
    RunState,
    SafetyHolding,
    SessionEvent,
    SetManualTarget,
    StartManual,
    StartSession,
    SurfaceBusy,
    is_start,
    words_of,
)
from src.motor.drive import ControlWord, DriveError, DriveFault, DriveLimits, DriveStatus
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok, Result
from src.telemetry import Payload, PayloadKind
from src.training.runtime import RuntimeState
from src.training.safety import EstopUnattested
from src.training.types import Occupancy, RunMode, TelemetrySnapshot
from src.units import OutputRpm, UnixMillis
from src.web.routes import (
    ALREADY_STOPPING,
    ESTOP_UNATTESTED,
    MACHINE_RUNNING,
    MACHINE_STOPPING,
    NO_MANUAL_SESSION,
    NOTHING_RUNNING,
    REQUEST_WAITING,
    REQUEST_WAITING_DETAIL,
    START_PENDING,
    START_PENDING_DETAIL,
    VERDICT_LATCHED,
    Refusal,
    answer_refusal,
)
from src.web.schemas import CommandRow, StatusRow, WsEnvelope, encode_envelope
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    TICK,
    Rig,
    Wrapped,
    make_rig,
)
from tests.test_web_api import (
    PROFILE_ID,
    RecordingSink,
    auth,
    build_rig,
    parse,
    state_of,
    surface_rig,
)
from tests.test_web_api import Rig as WebRig

ALICE: Final[str] = "alice, screen A"
BOB: Final[str] = "bob, screen B"

FATE_KINDS: Final[frozenset[EventKind]] = frozenset(
    {EventKind.TAKEN, EventKind.REFUSED, EventKind.CANCELLED}
)

type Body = Mapping[str, object]
"""A JSON request body."""


@dataclass(frozen=True, slots=True)
class RefusedAtOnce:
    """The whole body of a request the console turns down before the mailbox."""

    detail: str
    code: str
    waiting_request_id: str | None


def refusal_of(response: httpx.Response, status: HTTPStatus = HTTPStatus.CONFLICT) -> RefusedAtOnce:
    """The body of a refusal, which must carry exactly its three fields."""
    assert response.status_code == status, response.text
    return TypeAdapter(RefusedAtOnce).validate_json(response.content, strict=True)


def request_of(response: httpx.Response) -> str:
    """The identifier a 202 gives the request it accepted."""
    assert response.status_code == HTTPStatus.ACCEPTED, response.text
    return parse(CommandRow, response).request_id


# =========================================================================
# The mailbox alone
# =========================================================================


def attested(boot: str | None = None) -> tuple[RecordingSink, ControlSurface]:
    """A surface whose emergency-stop wiring is attested, and what it publishes."""
    clock, supervisor, sink, surface = surface_rig()
    if boot is not None:
        surface = ControlSurface(clock=clock, supervisor=supervisor, sink=sink, boot=boot)
    wired = supervisor.confirm_estop_wiring(OPERATOR)
    assert isinstance(wired, Ok)
    return sink, surface


def ask_start(surface: ControlSurface, operator: str = ALICE) -> StartSession:
    asked = surface.submit_start(profile_id=PROFILE_ID, operator=operator, total_duration_s=None)
    assert isinstance(asked, Ok), asked
    return asked.value


def ask_manual(surface: ControlSurface, operator: str = ALICE) -> StartManual:
    asked = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=operator)
    assert isinstance(asked, Ok), asked
    return asked.value


def ask_target(surface: ControlSurface, operator: str = ALICE) -> SetManualTarget:
    asked = surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=operator)
    assert isinstance(asked, Ok), asked
    return asked.value


def ask_reset(surface: ControlSurface, operator: str = ALICE) -> FaultReset:
    asked = surface.submit_fault_reset(operator=operator)
    assert isinstance(asked, Ok), asked
    return asked.value


def ask_end(surface: ControlSurface, operator: str = ALICE) -> EndSession:
    asked = surface.submit_end(operator=operator, reason="operator pressed STOP")
    assert isinstance(asked, Ok), asked
    return asked.value


def loop_takes(surface: ControlSurface, expected: Command) -> None:
    """The loop empties the mailbox, and finds ``expected`` in it."""
    taken = surface.take_command()
    assert taken is expected


def running(surface: ControlSurface) -> StartManual:
    """A manual session the loop took and reports running; the mailbox is empty."""
    started = ask_manual(surface)
    loop_takes(surface, started)
    surface.note_taken(started)
    surface.note_running()
    assert state_of(surface) is RunState.RUNNING
    return started


def of_kind(sink: RecordingSink, kind: EventKind) -> list[SessionEvent]:
    return [event for event in sink.events if event.kind is kind]


def fates_of(surface: ControlSurface) -> list[tuple[str, Fate]]:
    return [(said.request_id, said.fate) for said in surface.fates]


def test_every_accepted_request_is_given_an_identifier_of_its_own() -> None:
    """Short, opaque, the console's own: this boot's prefix and a count."""
    _sink, surface = attested(boot="b00t")

    start = ask_manual(surface)
    loop_takes(surface, start)
    surface.note_running()
    target = ask_target(surface)
    loop_takes(surface, target)
    reset = ask_reset(surface)
    loop_takes(surface, reset)
    end = ask_end(surface)

    identifiers = [start.request_id, target.request_id, reset.request_id, end.request_id]
    assert identifiers == ["b00t-1", "b00t-2", "b00t-3", "b00t-4"]


def test_an_identifier_says_nothing_of_who_asked_and_differs_from_one_boot_to_the_next() -> None:
    """A screen that outlived a restart must not take the new console's request for its own."""
    _sink, first = attested()
    _sink2, second = attested()

    one = ask_start(first, operator=ALICE)
    other = ask_start(second, operator=ALICE)
    named = ask_start(attested(boot="b00t")[1], operator=ALICE)
    unnamed = ask_start(attested(boot="b00t")[1], operator=BOB)

    assert one.request_id != other.request_id, "two consoles, the same first request"
    assert named.request_id == unnamed.request_id, "who asks changes nothing of the identifier"
    assert len(one.request_id) <= 16


def test_the_event_of_a_request_carries_its_identifier() -> None:
    sink, surface = attested()

    start = ask_manual(surface)
    loop_takes(surface, start)
    surface.note_running()
    reset = ask_reset(surface)
    loop_takes(surface, reset)
    end = ask_end(surface)

    announced = {
        event.kind: event.request_id
        for event in sink.events
        if event.kind
        in {EventKind.START_REQUESTED, EventKind.FAULT_RESET_REQUESTED, EventKind.END_REQUESTED}
    }
    assert announced == {
        EventKind.START_REQUESTED: start.request_id,
        EventKind.FAULT_RESET_REQUESTED: reset.request_id,
        EventKind.END_REQUESTED: end.request_id,
    }


def test_the_surface_does_not_read_idle_while_an_accepted_start_waits() -> None:
    """The defect itself: a tick that ends at rest must not forget the start it never saw."""
    sink, surface = attested()
    first = ask_start(surface, operator=ALICE)

    # The tick during which the start was accepted ends: the loop saw no session.
    surface.note_idle()

    assert state_of(surface) is RunState.STARTING
    assert of_kind(sink, EventKind.SESSION_IDLE) == [], "no session ended: none was running"
    second = surface.submit_start(profile_id=PROFILE_ID, operator=BOB, total_duration_s=None)
    assert isinstance(second, Err)
    assert isinstance(second.error, SurfaceBusy)
    assert second.error.state is RunState.STARTING
    assert second.error.in_the_way == first.request_id
    assert surface.pending is first, "the first start keeps its place"


def test_a_start_is_still_starting_while_the_loop_arms_it() -> None:
    """Taken from the mailbox and not answered for yet: a second start is refused."""
    _sink, surface = attested()
    first = ask_manual(surface, operator=ALICE)
    loop_takes(surface, first)

    second = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=BOB)

    assert isinstance(second, Err)
    assert isinstance(second.error, SurfaceBusy)
    assert second.error.pending is None
    assert second.error.in_the_way == first.request_id


def test_the_refusal_of_another_request_does_not_free_a_start_that_waits() -> None:
    """A reset the loop refuses, a target it takes back: neither is about the start."""
    _sink, surface = attested()
    reset = ask_reset(surface)
    loop_takes(surface, reset)
    # While the loop writes the reset to the drive, a start is accepted.
    start = ask_start(surface)

    surface.note_refused(reset, "reset refuse : aucun defaut a acquitter (ready)")
    surface.note_withdrawn("cible de 150 tr/min moteur remise a 0")
    surface.note_idle()

    assert state_of(surface) is RunState.STARTING
    assert surface.pending is start
    assert surface.session_request == start.request_id


def test_a_start_the_loop_refuses_leaves_the_surface_idle_and_says_which() -> None:
    sink, surface = attested()
    start = ask_start(surface)
    loop_takes(surface, start)

    surface.note_refused(start, "demarrage refuse : variateur en defaut (ObF, LFT 18)")
    surface.note_idle()

    assert state_of(surface) is RunState.IDLE
    assert surface.session_request is None
    refused = of_kind(sink, EventKind.REFUSED)
    assert [(event.request_id, event.operator) for event in refused] == [(start.request_id, ALICE)]
    assert of_kind(sink, EventKind.SESSION_IDLE) == [], "nothing ran"
    retried = surface.submit_start(profile_id=PROFILE_ID, operator=BOB, total_duration_s=None)
    assert isinstance(retried, Ok)


def test_a_request_the_loop_takes_is_said_taken_under_its_identifier() -> None:
    sink, surface = attested()
    start = running(surface)
    target = ask_target(surface, operator=BOB)
    loop_takes(surface, target)

    surface.note_taken(target)

    taken = of_kind(sink, EventKind.TAKEN)
    assert [(event.request_id, event.operator, event.detail) for event in taken] == [
        (start.request_id, ALICE, "manuel bench"),
        (target.request_id, BOB, "cible 3.00 tr/min de sortie"),
    ]
    assert fates_of(surface) == [(start.request_id, Fate.TAKEN), (target.request_id, Fate.TAKEN)]


def test_a_target_the_machine_takes_back_is_nobodys_request() -> None:
    sink, surface = attested()
    running(surface)
    before = surface.fates

    surface.note_withdrawn("cible de 150 tr/min moteur remise a 0")

    withdrawn = of_kind(sink, EventKind.REFUSED)
    assert [(event.request_id, event.operator) for event in withdrawn] == [(None, "")]
    assert surface.fates == before, "no request ended"
    assert surface.counters[1] == 1


def test_the_session_events_name_the_start_the_session_came_from() -> None:
    sink, surface = attested()
    start = running(surface)
    during = surface.session_request

    surface.note_idle()

    after = surface.session_request
    assert (during, after) == (start.request_id, None)
    named = [
        (event.kind, event.request_id)
        for event in sink.events
        if event.kind in {EventKind.SESSION_RUNNING, EventKind.SESSION_IDLE}
    ]
    assert named == [
        (EventKind.SESSION_RUNNING, start.request_id),
        (EventKind.SESSION_IDLE, start.request_id),
    ]


def test_a_session_over_within_its_first_tick_is_said_idle_once() -> None:
    """Armed, then ended before the loop ever reported it running (a verdict, an e-stop)."""
    sink, surface = attested()
    start = ask_manual(surface)
    loop_takes(surface, start)
    surface.note_taken(start)

    surface.note_idle()
    surface.note_idle()

    assert state_of(surface) is RunState.IDLE
    assert [event.request_id for event in of_kind(sink, EventKind.SESSION_IDLE)] == [
        start.request_id
    ]


# --- a stop always wins ------------------------------------------------------


def test_a_stop_takes_the_place_of_a_start_that_waits_and_that_start_is_told() -> None:
    sink, surface = attested()
    start = ask_start(surface, operator=ALICE)

    end = ask_end(surface, operator=BOB)

    assert surface.pending is end
    assert state_of(surface) is RunState.STOPPING
    assert surface.session_request is None, "the start no longer exists"
    cancelled = of_kind(sink, EventKind.CANCELLED)
    assert [(event.request_id, event.operator, event.detail) for event in cancelled] == [
        (start.request_id, ALICE, CANCELLED_BY_STOP)
    ]
    assert fates_of(surface) == [(start.request_id, Fate.CANCELLED)]
    assert BOB not in CANCELLED_BY_STOP
    assert PROFILE_ID not in CANCELLED_BY_STOP

    # The tick under way ends at rest: the stop still waits, the surface is not idle.
    surface.note_idle()
    assert state_of(surface) is RunState.STOPPING
    assert of_kind(sink, EventKind.SESSION_IDLE) == []
    # The next tick takes the stop; nothing was running, and the loop says so.
    loop_takes(surface, end)
    surface.note_taken(end)
    surface.note_idle()
    assert state_of(surface) is RunState.IDLE
    assert [event.request_id for event in of_kind(sink, EventKind.SESSION_IDLE)] == [None]
    assert fates_of(surface) == [
        (start.request_id, Fate.CANCELLED),
        (end.request_id, Fate.TAKEN),
    ]


@pytest.mark.parametrize("ask", [ask_target, ask_reset])
def test_a_stop_takes_the_place_of_a_target_or_a_reset_that_waits(
    ask: Callable[[ControlSurface], Command],
) -> None:
    sink, surface = attested()
    started = running(surface)
    waiting = ask(surface)

    end = ask_end(surface)

    assert surface.pending is end
    assert [event.request_id for event in of_kind(sink, EventKind.CANCELLED)] == [
        waiting.request_id
    ]
    assert surface.session_request == started.request_id, "the session is still that start's"
    assert state_of(surface) is RunState.STOPPING


def test_a_stop_is_accepted_while_the_loop_arms_a_start() -> None:
    """Nothing waits in the mailbox then: nothing is displaced, and both are answered."""
    sink, surface = attested()
    start = ask_manual(surface)
    loop_takes(surface, start)

    end = ask_end(surface)
    surface.note_taken(start)
    surface.note_running()

    assert surface.pending is end
    assert of_kind(sink, EventKind.CANCELLED) == []
    assert state_of(surface) is RunState.STOPPING


def test_a_stop_asked_in_the_tick_the_session_ends_by_itself_keeps_the_surface_stopping() -> None:
    """Idle is said once, when the last accepted request has been taken."""
    sink, surface = attested()
    start = running(surface)
    end = ask_end(surface)

    # That tick ends with the session over by itself; the stop was never taken.
    surface.note_idle()

    assert state_of(surface) is RunState.STOPPING
    assert of_kind(sink, EventKind.SESSION_IDLE) == []
    refused = surface.submit_start(profile_id=PROFILE_ID, operator=BOB, total_duration_s=None)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, SurfaceBusy)
    assert refused.error.state is RunState.STOPPING
    loop_takes(surface, end)
    surface.note_taken(end)
    surface.note_idle()
    assert state_of(surface) is RunState.IDLE
    assert [event.request_id for event in of_kind(sink, EventKind.SESSION_IDLE)] == [
        start.request_id
    ]


def test_an_emergency_stop_is_latched_whatever_waits_and_leaves_it_to_the_loop() -> None:
    """Never refused, never in the mailbox: the start it finds there is the loop's to refuse."""
    sink, surface = attested()
    start = ask_start(surface)

    receipt = surface.submit_estop(operator="", reason="")

    assert surface.estop_latched
    assert receipt.verdict.latched
    assert surface.pending is start, "the mailbox is not the emergency stop's"
    assert of_kind(sink, EventKind.CANCELLED) == []
    assert state_of(surface) is RunState.STOPPING


# --- the table: every pair of requests that can meet ---------------------------


def _waiting_start(surface: ControlSurface) -> Command:
    return ask_start(surface)


def _waiting_manual(surface: ControlSurface) -> Command:
    return ask_manual(surface)


def _waiting_reset_at_rest(surface: ControlSurface) -> Command:
    return ask_reset(surface)


def _waiting_target(surface: ControlSurface) -> Command:
    running(surface)
    return ask_target(surface)


def _waiting_reset_in_session(surface: ControlSurface) -> Command:
    running(surface)
    return ask_reset(surface)


def _waiting_end(surface: ControlSurface) -> Command:
    running(surface)
    return ask_end(surface)


def _arrives_start(surface: ControlSurface) -> Result[Command, object]:
    return surface.submit_start(profile_id=PROFILE_ID, operator=BOB, total_duration_s=None)


def _arrives_manual(surface: ControlSurface) -> Result[Command, object]:
    return surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=BOB)


def _arrives_target(surface: ControlSurface) -> Result[Command, object]:
    return surface.submit_manual_target(output_rpm=OutputRpm(2.0), operator=BOB)


def _arrives_reset(surface: ControlSurface) -> Result[Command, object]:
    return surface.submit_fault_reset(operator=BOB)


def _arrives_end(surface: ControlSurface) -> Result[Command, object]:
    return surface.submit_end(operator=BOB, reason="operator pressed STOP")


WAITING: Final = {
    "start": _waiting_start,
    "manual start": _waiting_manual,
    "reset at rest": _waiting_reset_at_rest,
    "target": _waiting_target,
    "reset in session": _waiting_reset_in_session,
    "stop": _waiting_end,
}
ARRIVES: Final = {
    "start": _arrives_start,
    "manual start": _arrives_manual,
    "target": _arrives_target,
    "reset": _arrives_reset,
    "stop": _arrives_end,
}

KEEPS: Final[str] = "refused, the waiting request keeps its place"
REPLACES: Final[str] = "accepted, the waiting request is cancelled"

#: What each arriving request does when it finds each waiting one. The only
#: request that ever takes a place is the stop. It displaces whatever waits in a
#: session that is starting or running; at rest it has nothing to stop, and
#: during a stop it is not needed.
TABLE: Final = {
    ("start", "start"): KEEPS,
    ("start", "manual start"): KEEPS,
    ("start", "target"): KEEPS,
    ("start", "reset"): KEEPS,
    ("start", "stop"): REPLACES,
    ("manual start", "start"): KEEPS,
    ("manual start", "manual start"): KEEPS,
    ("manual start", "target"): KEEPS,
    ("manual start", "reset"): KEEPS,
    ("manual start", "stop"): REPLACES,
    ("reset at rest", "start"): KEEPS,
    ("reset at rest", "manual start"): KEEPS,
    ("reset at rest", "target"): KEEPS,
    ("reset at rest", "reset"): KEEPS,
    ("reset at rest", "stop"): KEEPS,
    ("target", "start"): KEEPS,
    ("target", "manual start"): KEEPS,
    ("target", "target"): KEEPS,
    ("target", "reset"): KEEPS,
    ("target", "stop"): REPLACES,
    ("reset in session", "start"): KEEPS,
    ("reset in session", "manual start"): KEEPS,
    ("reset in session", "target"): KEEPS,
    ("reset in session", "reset"): KEEPS,
    ("reset in session", "stop"): REPLACES,
    ("stop", "start"): KEEPS,
    ("stop", "manual start"): KEEPS,
    ("stop", "target"): KEEPS,
    ("stop", "reset"): KEEPS,
    ("stop", "stop"): KEEPS,
}


def test_the_table_names_every_pair() -> None:
    assert set(TABLE) == {(waiting, arrives) for waiting in WAITING for arrives in ARRIVES}


@pytest.mark.parametrize(("waiting", "arrives"), sorted(TABLE))
def test_what_a_request_does_to_the_one_that_waits(waiting: str, arrives: str) -> None:
    """No request is ever replaced in silence, and only a stop replaces at all."""
    sink, surface = attested()
    held = WAITING[waiting](surface)
    before = len(sink.events)

    outcome = ARRIVES[arrives](surface)

    published = sink.events[before:]
    if TABLE[waiting, arrives] == KEEPS:
        assert isinstance(outcome, Err), f"{arrives} accepted over a waiting {waiting}"
        assert surface.pending is held
        assert published == [], "a request refused at once publishes nothing"
        return
    assert isinstance(outcome, Ok)
    assert surface.pending is outcome.value
    assert [(event.kind, event.request_id) for event in published] == [
        (EventKind.END_REQUESTED, outcome.value.request_id),
        (EventKind.CANCELLED, held.request_id),
    ]


def test_a_stop_at_rest_is_refused_for_having_nothing_to_stop_not_for_what_waits() -> None:
    """The one pair where a stop does not take the place: there is no session to end."""
    _sink, surface = attested()
    ask_reset(surface)

    refused = surface.submit_end(operator=BOB, reason="operator pressed STOP")

    assert isinstance(refused, Err)
    assert isinstance(refused.error, NothingRunning)


def test_every_request_ends_one_way_and_the_last_ones_are_kept() -> None:
    _sink, surface = attested()
    running(surface)
    asked: list[str] = []
    for _ in range(FATES_KEPT + 3):
        target = ask_target(surface)
        loop_takes(surface, target)
        surface.note_refused(target, "consigne refusee : hors domaine")
        asked.append(target.request_id)

    kept = surface.fates

    assert len(kept) == FATES_KEPT
    assert [said.request_id for said in kept] == asked[-FATES_KEPT:], "oldest first"
    assert {said.fate for said in kept} == {Fate.REFUSED}
    assert len({said.request_id for said in kept}) == FATES_KEPT


def test_what_the_events_say_of_a_request_never_names_who_asked() -> None:
    _sink, surface = attested()
    commands: list[Command] = [ask_start(surface, operator=ALICE)]
    loop_takes(surface, commands[0])
    surface.note_refused(commands[0], "x")
    commands.append(running(surface))
    commands.append(ask_target(surface, operator=ALICE))
    loop_takes(surface, commands[-1])
    commands.append(ask_reset(surface, operator=ALICE))
    loop_takes(surface, commands[-1])
    commands.append(ask_end(surface, operator=ALICE))

    words = [words_of(command) for command in commands]

    assert words == [
        f"start {PROFILE_ID}",
        "manuel bench",
        "cible 3.00 tr/min de sortie",
        "reset defaut",
        "operator pressed STOP",
    ]
    assert [is_start(command) for command in commands] == [True, True, False, False, False]
    assert not is_start(None)


def test_the_identifier_changes_no_decision_of_the_surface() -> None:
    """The same requests under two different boots: the same answers, the same states."""

    def lived(boot: str) -> list[object]:
        sink, surface = attested(boot=boot)
        seen: list[object] = []
        first = ask_start(surface)
        seen.append(type(_arrives_start(surface)).__name__)
        surface.note_idle()
        seen.append(state_of(surface))
        loop_takes(surface, first)
        surface.note_refused(first, "x")
        seen.append(state_of(surface))
        running(surface)
        seen.append(type(_arrives_target(surface)).__name__)
        seen.append(type(_arrives_target(surface)).__name__)
        seen.append(type(_arrives_end(surface)).__name__)
        seen.append(state_of(surface))
        seen.extend(event.kind for event in sink.events)
        return seen

    assert lived("aaaa") == lived("zzzzzzzz")


# =========================================================================
# The HTTP layer
# =========================================================================


@pytest.fixture
def web(tmp_path: Path) -> WebRig:
    rig = build_rig(tmp_path)
    rig.attest()
    return rig


@pytest.fixture
async def screen(web: WebRig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://testserver", headers=auth()
    )


def start_body(operator: str) -> Body:
    return {"profile_id": PROFILE_ID, "operator": operator, "subject_age": 41}


async def test_a_second_start_gets_a_code_a_fixed_sentence_and_nothing_of_the_first(
    web: WebRig, screen: httpx.AsyncClient
) -> None:
    first = await screen.post("/api/session/start", json=start_body(ALICE))

    second = await screen.post("/api/session/start", json=start_body(BOB))

    assert refusal_of(second) == RefusedAtOnce(
        detail=START_PENDING_DETAIL, code=START_PENDING, waiting_request_id=request_of(first)
    )
    # Nothing of the request in the way: not who asked, not when, not the rider's age.
    body_without_opaque_id = second.text.replace(f'"{request_of(first)}"', '""')
    for leaked in (ALICE, "alice", "StartSession", "subject_age", "41", "at="):
        assert leaked not in body_without_opaque_id, leaked
    waiting = web.surface.pending
    assert isinstance(waiting, StartSession)
    assert waiting.operator == ALICE


async def test_a_start_that_finds_a_reset_waiting_is_refused_and_the_reset_keeps_its_place(
    web: WebRig, screen: httpx.AsyncClient
) -> None:
    reset = await screen.post("/api/drive/fault-reset", json={"operator": ALICE})

    start = await screen.post("/api/session/start", json=start_body(BOB))

    assert refusal_of(start) == RefusedAtOnce(
        detail=REQUEST_WAITING_DETAIL, code=REQUEST_WAITING, waiting_request_id=request_of(reset)
    )
    assert ALICE not in start.text
    assert isinstance(web.surface.pending, FaultReset)


async def test_each_refusal_at_once_has_its_code(web: WebRig, screen: httpx.AsyncClient) -> None:
    surface = web.surface
    codes: dict[str, str] = {}

    async def refused(name: str, path: str, body: Body) -> None:
        answer = await screen.post(path, json=body)
        codes[name] = refusal_of(answer).code

    stop: Body = {"operator": BOB}
    target: Body = {"output_rpm": 2.0, "operator": BOB}
    await refused("a stop at rest", "/api/session/stop", stop)
    await refused("a target at rest", "/api/manual/target", target)
    started = ask_start(surface)
    loop_takes(surface, started)
    await refused("a reset while a start is armed", "/api/drive/fault-reset", stop)
    surface.note_taken(started)
    surface.note_running()
    await refused("a start during a session", "/api/session/start", start_body(BOB))
    ask_target(surface)
    await refused("a target behind a target", "/api/manual/target", target)
    await refused("a reset behind a target", "/api/drive/fault-reset", stop)
    ask_end(surface)
    await refused("a start during a stop", "/api/session/start", start_body(BOB))
    await refused("a stop during a stop", "/api/session/stop", stop)

    assert codes == {
        "a stop at rest": NOTHING_RUNNING,
        "a target at rest": NO_MANUAL_SESSION,
        "a reset while a start is armed": START_PENDING,
        "a start during a session": MACHINE_RUNNING,
        "a target behind a target": REQUEST_WAITING,
        "a reset behind a target": REQUEST_WAITING,
        "a start during a stop": MACHINE_STOPPING,
        "a stop during a stop": ALREADY_STOPPING,
    }


async def test_a_start_under_a_latched_verdict_or_before_the_attestation_has_its_code(
    tmp_path: Path,
) -> None:
    rig = build_rig(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app), base_url="http://testserver", headers=auth()
    ) as session:
        unattested = await session.post("/api/session/start", json=start_body(BOB))
        rig.attest()
        rig.surface.submit_estop(operator=ALICE, reason="pain")
        latched = await session.post("/api/session/start", json=start_body(BOB))

    assert refusal_of(unattested, HTTPStatus.PRECONDITION_FAILED).code == ESTOP_UNATTESTED
    under_verdict = refusal_of(latched)
    assert under_verdict.code == VERDICT_LATCHED
    assert under_verdict.waiting_request_id is None
    assert "operator_estop" in under_verdict.detail


def test_a_manual_start_passes_the_same_two_gates_before_the_mailbox_is_looked_at() -> None:
    """Not attested, then under a latched verdict: refused for that, whatever the mailbox holds."""
    _clock, supervisor, _sink, surface = surface_rig()

    unattested = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=BOB)
    wired = supervisor.confirm_estop_wiring(OPERATOR)
    surface.submit_estop(operator=ALICE, reason="pain")
    latched = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=BOB)

    assert isinstance(wired, Ok)
    assert isinstance(unattested, Err)
    assert isinstance(unattested.error, EstopUnattested)
    assert isinstance(latched, Err)
    assert isinstance(latched.error, SafetyHolding)
    assert surface.pending is None
    assert surface.fates == ()


def test_the_words_of_a_command_that_is_not_one_fail_loudly() -> None:
    not_a_command: Command = cast("Command", "bogus")
    with pytest.raises(AssertionError):
        _ = words_of(not_a_command)


async def test_a_run_state_that_is_not_one_fails_loudly_instead_of_getting_a_code(
    web: WebRig, screen: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state added to the surface has no code until somebody writes one."""
    not_a_state: RunState = cast("RunState", "bogus")

    def busy(_surface: ControlSurface, **_asked: object) -> Err[SurfaceBusy]:
        return Err(SurfaceBusy(state=not_a_state, pending=None))

    monkeypatch.setattr(ControlSurface, "submit_start", busy)
    assert web.surface.pending is None
    with pytest.raises(AssertionError):
        _ = await screen.post("/api/session/start", json=start_body(BOB))


async def test_the_status_lists_what_became_of_the_last_requests(
    web: WebRig, screen: httpx.AsyncClient
) -> None:
    """For a screen whose socket was down when the event of its request went out."""
    surface = web.surface
    refused = ask_start(surface, operator=ALICE)
    loop_takes(surface, refused)
    surface.note_refused(refused, "demarrage refuse : variateur en defaut (ObF, LFT 18)")
    started = ask_manual(surface, operator=ALICE)
    waiting = parse(StatusRow, await screen.get("/api/status"))
    loop_takes(surface, started)
    surface.note_taken(started)
    surface.note_running()

    answered = await screen.get("/api/status")
    status = parse(StatusRow, answered)

    assert waiting.pending is not None
    assert waiting.pending.request_id == started.request_id
    assert waiting.session_request == started.request_id
    assert waiting.run_state == "starting"
    assert [(row.request_id, row.fate, row.detail) for row in status.requests] == [
        (refused.request_id, "refused", "demarrage refuse : variateur en defaut (ObF, LFT 18)"),
        (started.request_id, "taken", "manuel bench"),
    ]
    assert status.session_request == started.request_id
    assert status.pending is None
    # What became of a request names no operator: the answer of the status does not either.
    assert ALICE not in answered.text
    surface.note_idle()
    at_rest = parse(StatusRow, await screen.get("/api/status"))
    assert at_rest.session_request is None


def test_an_event_goes_to_the_page_with_the_identifier_of_its_request() -> None:
    sink, surface = attested()
    start = ask_manual(surface)
    surface.note_recording("enregistrement degrade")

    def on_the_wire(event: SessionEvent) -> str:
        payload = Payload(kind=PayloadKind.EVENT, snapshot=None, event=event, notice="")
        return encode_envelope(WsEnvelope.of(payload))

    asked, recording = (on_the_wire(event) for event in sink.events[-2:])

    assert f'"request_id":"{start.request_id}"' in asked
    assert '"request_id":null' in recording
    assert sink.events[-1].wall_clock >= UnixMillis(0)


async def test_the_answer_of_a_refusal_is_built_for_a_refusal_only() -> None:
    refusal = Refusal(HTTPStatus.CONFLICT, code=REQUEST_WAITING, detail="x", waiting="b-1")
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})

    answered = await answer_refusal(request, refusal)

    assert answered.status_code == HTTPStatus.CONFLICT
    assert answered.body == b'{"detail":"x","code":"request_waiting","waiting_request_id":"b-1"}'
    with pytest.raises(RuntimeError, match="not a refusal"):
        await answer_refusal(request, RuntimeError("not a refusal"))


# =========================================================================
# The real console, with a drive whose exchanges wait where the test says
# =========================================================================


class Gated(Wrapped):
    """The simulated drive behind a gate: one exchange waits until the test lets it go.

    The plain simulated drive answers without ever yielding, so a tick on it is
    atomic and no request can arrive inside one. The real drive awaits every
    exchange (``src/motor/atv320.py``), and that is where two requests meet.
    Here the test chooses the exchange and holds it for as long as it needs:
    nothing depends on a delay or on the speed of the machine running the test.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.hold: str | None = None
        """The exchange to hold, once: ``read_status``, ``read_limits`` or ``write_command``."""
        self.inside: asyncio.Event = asyncio.Event()
        """Set while the tick waits inside the held exchange."""
        self.go: asyncio.Event = asyncio.Event()

    async def _gate(self, exchange: str) -> None:
        if self.hold != exchange:
            return
        self.hold = None
        self.inside.set()
        await self.go.wait()
        self.inside.clear()
        self.go.clear()

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        await self._gate("read_status")
        return await self.inner.read_status()

    @override
    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        await self._gate("read_limits")
        return await self.inner.read_limits()

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        await self._gate("write_command")
        return await self.inner.write_command(word)


class Console:
    """The real console on a gated drive, and one screen's view of what it published."""

    def __init__(self, rig: Rig, drive: Gated, session: httpx.AsyncClient) -> None:
        self.rig: Rig = rig
        self.drive: Gated = drive
        self.http: httpx.AsyncClient = session

    @property
    def surface(self) -> ControlSurface:
        return self.rig.panel.surface

    async def tick(self) -> TelemetrySnapshot:
        """One whole tick, nothing held."""
        return await self.rig.tick(TICK)

    async def tick_into(self, exchange: str) -> asyncio.Task[TelemetrySnapshot]:
        """Tick until a tick waits on the drive inside ``exchange``. That tick, unfinished."""
        for _ in range(25):
            self.drive.hold = exchange
            self.rig.clock.advance(TICK)
            self.surface.note_presence(OPERATOR)
            step = asyncio.create_task(self.rig.panel.control_step())
            entered = asyncio.create_task(self.drive.inside.wait())
            await asyncio.wait({step, entered}, return_when=asyncio.FIRST_COMPLETED)
            if self.drive.inside.is_set():
                await entered
                return step
            entered.cancel()
            await asyncio.gather(entered, return_exceptions=True)
            await step
            await self.rig.drain()
        raise AssertionError(f"no tick reached {exchange}")

    async def finish(self, step: asyncio.Task[TelemetrySnapshot]) -> TelemetrySnapshot:
        """Let the held exchange go and the tick end."""
        self.drive.go.set()
        snapshot = await step
        await self.rig.drain()
        return snapshot

    def said(self, *kinds: EventKind) -> list[tuple[EventKind, str | None]]:
        """The events of ``kinds`` this screen received, in order: what, and of which request."""
        wanted = set(kinds) if kinds else None
        return [
            (event.kind, event.request_id)
            for event in self.rig.events
            if wanted is None or event.kind in wanted
        ]

    async def post(self, path: str, body: Body) -> httpx.Response:
        return await self.http.post(path, json=body)

    async def manual_start(self, operator: str) -> httpx.Response:
        return await self.post("/api/manual/start", {"occupancy": "bench", "operator": operator})


async def console(tmp_path: Path, *, programmes: bool = False) -> Console:
    rig, wrapped = (
        make_rig(tmp_path, wrap=Gated, env=PROGRAMME_ENV)
        if programmes
        else make_rig(tmp_path, wrap=Gated)
    )
    assert isinstance(wrapped, Gated)
    session = rig.http()
    attest = await session.post(
        "/api/safety/attest",
        json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
    )
    assert attest.status_code == HTTPStatus.OK, attest.text
    made = Console(rig, wrapped, session)
    await made.tick()
    return made


SESSION_EVENTS: Final = (
    EventKind.START_REQUESTED,
    EventKind.TAKEN,
    EventKind.REFUSED,
    EventKind.CANCELLED,
    EventKind.SESSION_RUNNING,
    EventKind.SESSION_IDLE,
)


async def test_two_starts_within_one_tick_the_second_is_refused_at_once(tmp_path: Path) -> None:
    """The race two operators lived through, replayed with the tick held inside the drive.

    Screen A's start is accepted while the loop awaits the drive's status. That
    tick ends at rest. Screen B then asks. Before: both got a 202, B's request
    replaced A's without a word, one session ran. Now B is refused at once, A's
    start is the one the loop takes, and the console says so under A's identifier.
    """
    made = await console(tmp_path)
    step = await made.tick_into("read_status")

    first = await made.manual_start(ALICE)
    await made.finish(step)
    state_between = made.surface.run_state
    second = await made.manual_start(BOB)

    mine = request_of(first)
    assert state_between is RunState.STARTING, "the tick that never saw the start ended at rest"
    assert second.status_code == HTTPStatus.CONFLICT, "a second start was accepted over the first"
    assert refusal_of(second) == RefusedAtOnce(
        detail=START_PENDING_DETAIL, code=START_PENDING, waiting_request_id=mine
    )
    assert ALICE not in second.text

    snapshot = await made.tick()

    assert snapshot.mode is RunMode.MANUEL
    manual = made.rig.panel.runtime.manual
    assert manual is not None
    assert manual.operator == ALICE, "the session that runs is the one that was told taken"
    assert made.said(*SESSION_EVENTS) == [
        (EventKind.START_REQUESTED, mine),
        (EventKind.TAKEN, mine),
        (EventKind.SESSION_RUNNING, mine),
    ]
    await made.rig.panel.close()
    await made.http.aclose()


async def test_a_start_accepted_during_a_reset_survives_it_and_refuses_the_next_start(
    tmp_path: Path,
) -> None:
    """Three requests in two ticks: a reset, A's start during its write, B's while A is armed.

    Before: the reset's tick ended at rest and freed the surface, B's start was
    accepted while the loop armed A's, and B was refused one tick later by an
    event that did not say whose refusal it was.
    """
    made = await console(tmp_path)
    made.rig.simulator.inject_fault(DriveFault.DC_BUS_OVERVOLTAGE)
    for _ in range(5):
        await made.tick()
    assert made.rig.panel.runtime.snapshot().fault is not None
    reset = await made.post("/api/drive/fault-reset", {"operator": OPERATOR})
    the_reset = request_of(reset)

    writing = await made.tick_into("write_command")
    first = await made.manual_start(ALICE)
    await made.finish(writing)
    mine = request_of(first)
    state_between = made.surface.run_state
    arming = await made.tick_into("read_limits")
    second = await made.manual_start(BOB)
    behind = await made.post("/api/drive/fault-reset", {"operator": BOB})
    await made.finish(arming)

    assert state_between is RunState.STARTING, "the reset's tick ended at rest"
    assert second.status_code == HTTPStatus.CONFLICT, "accepted while the loop armed the first"
    assert refusal_of(second).code == START_PENDING
    assert refusal_of(second).waiting_request_id == mine
    assert refusal_of(behind).code == START_PENDING
    assert made.rig.panel.runtime.state is RuntimeState.RUNNING
    manual = made.rig.panel.runtime.manual
    assert manual is not None
    assert manual.operator == ALICE
    assert made.said(*FATE_KINDS) == [(EventKind.TAKEN, the_reset), (EventKind.TAKEN, mine)]
    assert BOB not in [event.operator for event in made.rig.events]
    await made.rig.panel.close()
    await made.http.aclose()


async def test_a_stop_before_the_loop_took_a_start_cancels_it_and_nothing_is_armed(
    tmp_path: Path,
) -> None:
    """The start is not left unanswered: its screen is told a stop took its place."""
    made = await console(tmp_path)
    step = await made.tick_into("read_status")
    start = await made.manual_start(ALICE)

    stop = await made.post("/api/session/stop", {"operator": BOB, "reason": "STOP"})
    await made.finish(step)
    stopping = made.surface.run_state
    # The runtime, asked to stop with nothing started, shows a stop for a few seconds
    # (as it did before this change) and comes back to rest.
    for _ in range(60):
        await made.tick()
        if made.surface.run_state is RunState.IDLE:
            break

    assert stop.status_code == HTTPStatus.ACCEPTED, "a stop is never refused for what waits"
    asked, stopped = request_of(start), request_of(stop)
    assert stopping is RunState.STOPPING
    assert made.rig.panel.runtime.manual is None, "nothing was armed"
    assert made.rig.simulator.commanded_setpoint == 0
    assert made.surface.run_state is RunState.IDLE
    assert made.said(*SESSION_EVENTS, EventKind.END_REQUESTED) == [
        (EventKind.START_REQUESTED, asked),
        (EventKind.END_REQUESTED, stopped),
        (EventKind.CANCELLED, asked),
        (EventKind.TAKEN, stopped),
        (EventKind.SESSION_IDLE, None),
    ]
    # And the console is not left deaf to the next start.
    later = await made.manual_start(ALICE)
    assert later.status_code == HTTPStatus.ACCEPTED, later.text
    await made.tick()
    assert made.rig.panel.runtime.state is RuntimeState.RUNNING
    await made.rig.panel.close()
    await made.http.aclose()


async def test_an_emergency_stop_over_a_waiting_start_is_latched_and_the_loop_refuses_the_start(
    tmp_path: Path,
) -> None:
    made = await console(tmp_path)
    step = await made.tick_into("read_status")
    start = await made.manual_start(ALICE)

    estop = await made.post("/api/session/estop", {"operator": "", "reason": ""})
    latched_before_the_tick_ended = made.surface.estop_latched
    await made.finish(step)
    await made.tick()

    assert estop.status_code == HTTPStatus.OK
    assert latched_before_the_tick_ended
    assert made.rig.panel.runtime.manual is None
    assert made.said(*FATE_KINDS) == [(EventKind.REFUSED, request_of(start))]
    assert "operator_estop" in made.rig.refusals()[0]
    await made.rig.panel.close()
    await made.http.aclose()


async def test_every_request_of_a_manual_session_is_answered_under_its_identifier(
    tmp_path: Path,
) -> None:
    """Start, a target taken, a target refused, a stop: each said once, by the console."""
    made = await console(tmp_path)
    start = await made.manual_start(ALICE)
    await made.tick()
    target = await made.post("/api/manual/target", {"output_rpm": 3.0, "operator": ALICE})
    await made.tick()
    too_fast = await made.post("/api/manual/target", {"output_rpm": 400.0, "operator": BOB})
    await made.tick()
    stop = await made.post("/api/session/stop", {"operator": ALICE, "reason": "STOP"})
    await made.tick()
    status = parse(StatusRow, await made.http.get("/api/status"))

    identifiers = [request_of(answer) for answer in (start, target, too_fast, stop)]
    assert len(set(identifiers)) == 4
    assert made.said(*FATE_KINDS) == [
        (EventKind.TAKEN, identifiers[0]),
        (EventKind.TAKEN, identifiers[1]),
        (EventKind.REFUSED, identifiers[2]),
        (EventKind.TAKEN, identifiers[3]),
    ]
    assert [(row.request_id, row.fate) for row in status.requests] == [
        (identifiers[0], "taken"),
        (identifiers[1], "taken"),
        (identifiers[2], "refused"),
        (identifiers[3], "taken"),
    ]
    assert status.requests[2].detail.startswith("consigne refusee : 400.00 tr/min de sortie")
    assert status.session_request == identifiers[0]
    await made.rig.panel.close()
    await made.http.aclose()


async def test_a_programme_start_is_answered_under_its_identifier_taken_or_refused(
    tmp_path: Path,
) -> None:
    made = await console(tmp_path, programmes=True)
    body: Body = {"profile_id": SHORT_PROFILE, "operator": ALICE}

    without_age = await made.post("/api/session/start", body)
    await made.tick()
    with_age = await made.post("/api/session/start", {**body, "subject_age": 30})
    await made.tick()

    assert made.said(*FATE_KINDS) == [
        (EventKind.REFUSED, request_of(without_age)),
        (EventKind.TAKEN, request_of(with_age)),
    ]
    assert made.rig.refusals() == [
        "demarrage refuse : age du passager requis pour une seance programmee"
    ]
    assert made.rig.panel.runtime.state is RuntimeState.RUNNING
    await made.rig.panel.close()
    await made.http.aclose()


async def test_a_gate_that_is_not_asked_for_holds_nothing(tmp_path: Path) -> None:
    """The rig's own check: with nothing held, a tick on the gated drive is a tick."""
    made = await console(tmp_path)

    snapshot = await made.tick()
    held = await made.tick_into("read_status")
    inside = made.drive.inside.is_set()
    await made.finish(held)

    assert snapshot.mode is RunMode.REPOS
    assert inside
    assert not made.drive.inside.is_set()
    await made.rig.panel.close()
    await made.http.aclose()
