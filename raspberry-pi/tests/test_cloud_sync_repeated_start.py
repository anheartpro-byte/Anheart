"""ANH-240: a start whose answer was lost may be confirmed again, for a minute at most.

Convex holds a launch active once its start reached it, and no longer offers it
to the poll; if its 200 is lost, the console reads no answer and stays unarmed.
Convex answers a repeated confirmation 200 while the machine has sent nothing of
the session (convex/repeatedStartConfirmation.test.ts). So the console keeps the
offer whose last confirmation went unanswered when the poll no longer offers it,
for :data:`~src.cloud_sync.LAUNCH_TIMEOUT` after the click of the first
confirmation left unanswered, and never for another launch nor once the
dashboard has refused it.

The scripted dashboard below behaves as Convex does: from the first request for
the start on, its poll offers nothing (:func:`like_convex`).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from src.cloud_sync import ARMING_REFUSAL_WORDS, LAUNCH_TIMEOUT, POLL_PERIOD, Document, Refused
from src.contract import ErrorCode
from src.remote_launch import ConfirmationCode, LaunchStage
from src.result import Err, Ok
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.units import Monotonic, Seconds
from tests.test_cloud_sync import (
    DOWN,
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    Dashboard,
    Reply,
    Rig,
    confirm_linked,
    launch_answer,
    linked,
    ok,
    pending_start,
    rig,
    state_of,
)

END: Final[str] = "/api/machine/training/end"
NOT_FOUND: Final[Reply] = Err(Refused(404, "Not found"))


def like_convex(dashboard: Dashboard, replies: Sequence[Reply]) -> None:
    """Answer the starts with ``replies`` in turn, the last one again for any more.

    From the first request on, the poll offers nothing: Convex holds the launch
    active once its start reached it, whatever became of the answer.
    """
    answers = list(replies)

    def start(_body: object) -> Reply:
        dashboard.answer(POLL_PATH, launch_answer(None))
        return answers.pop(0) if len(answers) > 1 else answers[0]

    dashboard.handle(START, start)


async def held(r: Rig, replies: Sequence[Reply]) -> Monotonic:
    """The launch offered, confirmed, and its start answered by the first of ``replies``,
    the others kept for the confirmations after it. Returns the instant of the click."""
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    like_convex(r.dashboard, replies)
    await r.step()
    clicked = r.clock.monotonic()
    r.confirm_offer()
    await r.step()
    assert stage(r) is LaunchStage.UNCONFIRMED
    assert len(r.dashboard.to(START)) == 1
    return clicked


def stage(r: Rig) -> LaunchStage | None:
    offer = r.sync.current_remote_launch()
    return None if offer is None else offer.stage


def refused_code(r: Rig, session_id: str = "remote-1") -> ConfirmationCode | None:
    """The code of a click's refusal; ``None`` when the click is accepted."""
    clicked = r.sync.confirm_remote_launch(session_id, OPERATOR)
    return None if isinstance(clicked, Ok) else clicked.error.code


async def until(r: Rig, at: Monotonic) -> None:
    """Step once, so that the step happens at ``at``."""
    await r.step(at - r.clock.monotonic())


# =========================================================================
# EX-4: a lost 200, then a repeated confirmation answered 200, arms once
# =========================================================================


async def test_a_lost_200_then_a_repeat_answered_200_arms_the_launch_once(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await held(r, (DOWN, ok()))
    polls = len(r.dashboard.to(POLL_PATH))
    for _ in range(6):
        await r.step()
    assert len(r.dashboard.to(POLL_PATH)) >= polls + 2  # null polls since the first start
    assert stage(r) is LaunchStage.UNCONFIRMED

    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 2
    armed = pending_start(r.panel.surface)
    assert armed is not None
    assert armed.cloud_session_id == "remote-1"
    assert refused_code(r) is ConfirmationCode.IN_PROGRESS
    for _ in range(6):
        await r.step()
    # Answered, it is held no more: the null poll takes the offer away, as after any 2xx.
    assert r.sync.current_remote_launch() is None
    assert len(r.dashboard.to(START)) == 2
    assert pending_start(r.panel.surface) == armed
    assert r.dashboard.to(END) == []


async def test_on_the_real_panel_a_repeat_after_a_lost_200_runs_one_session(
    tmp_path: Path,
) -> None:
    r = linked(tmp_path, record_root=tmp_path / "records")
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    like_convex(r.dashboard, (DOWN, ok()))
    await r.run(0.2)
    confirm_linked(r.panel)
    await r.run(1.8)
    cloud = r.panel.cloud
    assert cloud is not None
    assert state_of(r.panel) is RuntimeState.IDLE
    await r.run(6.0)
    offer = cloud.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.UNCONFIRMED

    confirm_linked(r.panel)
    await r.run(1.8)

    assert state_of(r.panel) is RuntimeState.RUNNING
    assert cloud.current_session_id == "remote-1"
    assert isinstance(cloud.confirm_remote_launch("remote-1", OPERATOR), Err)
    await r.run(6.0)
    assert len(r.dashboard.to(START)) == 2
    assert r.dashboard.to("/api/machine/training/local") == []
    assert r.dashboard.to(END) == []
    await r.panel.close()
    recorder = r.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    sessions = [
        path
        for path in recorder.journal.root.rglob("events.jsonl")
        if path.parent.name != "logbook"
    ]
    assert len(sessions) == 1


async def test_two_lost_answers_then_a_200_within_the_minute_arm_once(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await held(r, (DOWN, DOWN, ok()))
    await r.step(10.0)
    r.confirm_offer()
    await r.step()
    assert len(r.dashboard.to(START)) == 2
    assert stage(r) is LaunchStage.UNCONFIRMED
    assert pending_start(r.panel.surface) is None
    await r.step(10.0)

    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 3
    assert pending_start(r.panel.surface) is not None
    assert refused_code(r) is ConfirmationCode.IN_PROGRESS
    for _ in range(6):
        await r.step()
    assert len(r.dashboard.to(START)) == 3
    assert r.dashboard.to(END) == []


# =========================================================================
# Never later than LAUNCH_TIMEOUT after the click
# =========================================================================


async def test_a_minute_after_the_click_the_offer_is_gone_and_a_click_is_refused(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN, ok()))
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT - 0.5))
    assert stage(r) is LaunchStage.UNCONFIRMED

    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT))

    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    for _ in range(4):
        await r.step()
    assert len(r.dashboard.to(START)) == 1
    assert pending_start(r.panel.surface) is None
    assert r.panel.surface.pending is None
    assert r.dashboard.to(END) == []


async def test_with_the_polls_failing_nothing_is_sent_a_minute_after_the_click(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN, ok()))
    await r.step(POLL_PERIOD)
    r.dashboard.answer(POLL_PATH, DOWN)
    await until(r, Monotonic(clicked + 30.0))
    assert stage(r) is LaunchStage.UNCONFIRMED

    # Between two steps already, the offer is neither shown nor confirmable.
    r.clock.advance(Seconds(LAUNCH_TIMEOUT - 30.0))
    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    for _ in range(4):
        await r.step()

    assert len(r.dashboard.to(START)) == 1
    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    assert r.panel.surface.pending is None
    assert r.dashboard.to(END) == []


async def test_a_click_queued_before_the_minute_is_not_sent_after_it(tmp_path: Path) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN, ok()))
    r.dashboard.answer(POLL_PATH, DOWN)
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT - 0.5))
    assert refused_code(r) is None  # accepted, queued for the next step

    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT + 0.5))

    assert len(r.dashboard.to(START)) == 1
    assert r.sync.current_remote_launch() is None
    assert r.panel.surface.pending is None
    assert r.dashboard.to(END) == []


async def test_a_second_unanswered_confirmation_does_not_extend_the_minute(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN, DOWN, ok()))
    await until(r, Monotonic(clicked + 50.0))
    r.confirm_offer()
    await r.step()
    assert len(r.dashboard.to(START)) == 2
    assert stage(r) is LaunchStage.UNCONFIRMED

    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT - 0.5))
    assert stage(r) is LaunchStage.UNCONFIRMED
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT + 0.5))

    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    assert len(r.dashboard.to(START)) == 2


async def test_a_200_answered_after_the_minute_is_abandoned_and_its_refusal_owed(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN,))
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT - 1.0))
    r.confirm_offer()

    def answered_late(_body: object) -> Reply:
        r.clock.advance(Seconds(2.0))  # sent within the minute, answered after it
        return ok()

    r.dashboard.handle(START, answered_late)
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT - 0.5))

    assert len(r.dashboard.to(START)) == 2
    assert pending_start(r.panel.surface) is None
    assert r.sync.current_remote_launch() is None
    reasons = [body["reason"] for body in r.dashboard.to(END)]
    assert reasons == [
        "refusee par la machine : "
        "confirmation distante abandonnee avant la reponse de training/start"
    ]


@pytest.mark.parametrize(
    ("offered", "offered_stage"),
    [
        pytest.param(dict(LAUNCH), LaunchStage.UNCONFIRMED, id="the-same-launch"),
        pytest.param(
            {**LAUNCH, "totalDurationS": 2700},
            LaunchStage.WAITING_CONFIRMATION,
            id="the-launch-changed",
        ),
    ],
)
async def test_a_launch_the_poll_still_offers_is_confirmed_as_before_after_a_minute(
    tmp_path: Path, offered: dict[str, JsonValue], offered_stage: LaunchStage
) -> None:
    """The first request never reached the dashboard, whose poll offers the launch
    still: no hold runs for it, and a click 70 s later is sent and arms it, once."""
    r = rig(tmp_path)
    clicked = await held(r, (DOWN, ok()))
    r.dashboard.answer(POLL_PATH, launch_answer(offered))
    await until(r, Monotonic(clicked + 70.0))
    assert stage(r) is offered_stage

    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 2
    assert pending_start(r.panel.surface) is not None
    assert refused_code(r) is ConfirmationCode.IN_PROGRESS
    for _ in range(6):
        await r.step()
    assert len(r.dashboard.to(START)) == 2
    assert r.dashboard.to(END) == []


async def test_once_the_poll_offered_it_again_the_minute_runs_from_the_next_unanswered_click(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    first = await held(r, (DOWN, DOWN, ok()))
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await until(r, Monotonic(first + 40.0))
    assert stage(r) is LaunchStage.UNCONFIRMED

    second = r.clock.monotonic()
    r.confirm_offer()  # unanswered again, and this one reached the dashboard
    await r.step()
    assert len(r.dashboard.to(START)) == 2

    await until(r, Monotonic(first + LAUNCH_TIMEOUT + 5.0))
    assert stage(r) is LaunchStage.UNCONFIRMED
    await until(r, Monotonic(second + LAUNCH_TIMEOUT - 0.5))
    assert stage(r) is LaunchStage.UNCONFIRMED
    await until(r, Monotonic(second + LAUNCH_TIMEOUT))
    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    assert len(r.dashboard.to(START)) == 2


# =========================================================================
# Never for another launch, never once the dashboard has refused it
# =========================================================================


@pytest.mark.parametrize(
    ("refusal", "sentence"),
    [
        pytest.param(
            Refused(
                400, "Session is not pending (status: active)", ErrorCode("session_not_pending")
            ),
            "depart distant refuse (HTTP 400, session_not_pending)",
            id="400-not-pending",
        ),
        pytest.param(
            Refused(400, "Session not found", ErrorCode("session_not_found")),
            "depart distant refuse (HTTP 400, session_not_found)",
            id="400-not-found",
        ),
        pytest.param(
            Refused(404, "Session not found", ErrorCode("session_not_found")),
            "depart distant refuse (HTTP 404, session_not_found)",
            id="404-not-found",
        ),
        pytest.param(
            Refused(409, "Declined", ErrorCode("hr_max_changed_before_start")),
            f"{ARMING_REFUSAL_WORDS[ErrorCode('hr_max_changed_before_start')]}"
            " (hr_max_changed_before_start)",
            id="409-arming",
        ),
    ],
)
async def test_a_repeat_the_dashboard_refuses_arms_nothing_and_the_offer_goes(
    tmp_path: Path, refusal: Refused, sentence: str
) -> None:
    r = rig(tmp_path)
    await held(r, (DOWN, Err(refusal)))
    r.confirm_offer()
    await r.step()

    assert len(r.dashboard.to(START)) == 2
    assert r.panel.surface.pending is None
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.stage is LaunchStage.REFUSED
    assert offer.reason == sentence
    retry = r.sync.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(retry, Err)
    assert retry.error.code is ConfirmationCode.REFUSED
    assert retry.error.detail == sentence

    await r.step(POLL_PERIOD)

    assert r.sync.current_remote_launch() is None
    assert len(r.dashboard.to(START)) == 2
    assert r.dashboard.to(END) == []


async def test_another_launch_replaces_the_held_one_and_its_minute(tmp_path: Path) -> None:
    r = rig(tmp_path)
    clicked = await held(r, (DOWN,))
    other: dict[str, JsonValue] = {**LAUNCH, "sessionId": "remote-2"}
    r.dashboard.answer(POLL_PATH, launch_answer(other))
    await until(r, Monotonic(clicked + 30.0))

    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.session_id == "remote-2"
    assert offer.stage is LaunchStage.WAITING_CONFIRMATION
    assert refused_code(r, "remote-1") is ConfirmationCode.NOT_PENDING

    # The minute of the new one runs from its own click, not from the old one's.
    assert refused_code(r, "remote-2") is None
    await r.step()
    assert [body["sessionId"] for body in r.dashboard.to(START)] == ["remote-1", "remote-2"]
    await until(r, Monotonic(clicked + LAUNCH_TIMEOUT + 2.0))
    kept = r.sync.current_remote_launch()
    assert kept is not None
    assert kept.session_id == "remote-2"
    assert kept.stage is LaunchStage.UNCONFIRMED
    assert refused_code(r, "remote-1") is ConfirmationCode.NOT_PENDING


@pytest.mark.parametrize(
    "replies",
    [
        pytest.param((NOT_FOUND,), id="first-confirmation"),
        pytest.param((DOWN, NOT_FOUND), id="repeated-confirmation"),
    ],
)
async def test_a_missing_route_keeps_no_offer_once_the_poll_drops_it(
    tmp_path: Path, replies: Sequence[Reply]
) -> None:
    r = rig(tmp_path)
    await held(r, replies)
    if len(replies) > 1:
        r.confirm_offer()
        await r.step()
        assert len(r.dashboard.to(START)) == 2
    offer = r.sync.current_remote_launch()
    assert offer is not None
    assert offer.reason == "route training/start inconnue"

    await r.step(POLL_PERIOD)

    assert r.sync.current_remote_launch() is None
    assert refused_code(r) is ConfirmationCode.NOT_PENDING
    assert r.dashboard.to(END) == []


def test_the_scripted_start_answers_in_turn_then_repeats_the_last() -> None:
    dashboard = Dashboard()
    first: Document = {"n": 1}
    like_convex(dashboard, (ok(first), ok()))
    answers = [dashboard.reply("POST", START, {}) for _ in range(3)]
    assert answers == [ok(first), ok(), ok()]
    assert dashboard.reply("GET", POLL_PATH, {}) == launch_answer(None)
