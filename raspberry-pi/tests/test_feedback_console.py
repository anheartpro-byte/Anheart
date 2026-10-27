"""ANH-146 on the real console: the rider's feedback after a session with somebody on board.

Each test runs the REAL console in simulation (simulated drive, real runtime,
real session record on disk, real dashboard link) against
:class:`~tests.fake_dashboard.FakeDashboard`, which keeps a feedback apart
from the session's events as the real dashboard does since contract 1.7.
The operator acts through the console's HTTP routes, as the page does.

* P1: a manual session declared occupied ends: the console offers the
  feedback. This is the acceptance criterion's "in simulation";
* P2: a bench session ends: nothing is offered, and a feedback is refused;
* P3: a programmed session ends: offered;
* P4: entered, it reaches the dashboard as exactly one request carrying one
  ``feedback`` event, and never among the session's events;
* P5: a dashboard of 1.6 is sent nothing; a lost link sends it on its return;
  a session declared only after the entry gets it once declared; a refusal is
  said with its code;
* P6: skipped, nothing is sent and nothing more is offered;
* P8: the comment and the scores are in no log line, no file, no event of the
  record and no error answer.

The module alone (bounds, labels, the 24 h, the bound of what is held) is
``tests/test_passenger_feedback.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src.cloud_sync import Refused
from src.contract import CONTRACT_VERSION, ErrorCode
from src.log_format import JsonLines
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.training.types import SignalQuality
from src.units import Bpm
from tests.fake_dashboard import EVENTS, FakeDashboard
from tests.record_console_support import RecordedRig, recorded_rig
from tests.roster_support import RIDER, RIDER_ID, roster_document
from tests.test_failure_rig import OPERATOR, PROGRAMME_ENV, attest
from tests.test_panel_rider import FITTING, start_body

LINKED_ENV: Final[Mapping[str, str]] = {
    **PROGRAMME_ENV,
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

SENTINEL: Final[str] = "synthetic-private-feedback-sentinel"
ENTERED: Final[Mapping[str, JsonValue]] = {
    "nausea_score": 7,
    "dizziness_score": 9,
    "comfort": "bad",
    "free_text": f"  {SENTINEL}  ",
}
DETAIL: Final[Mapping[str, object]] = {
    "nauseaScore": 7,
    "dizzinessScore": 9,
    "comfort": "bad",
    "freeText": SENTINEL,
}


@dataclass
class Linked:
    """A recording console linked to a dashboard in memory, and a heart rate for its rider."""

    recorded: RecordedRig
    dashboard: FakeDashboard
    seq: int = 0

    async def seconds(self, count: int, *, bpm: int | None = 97) -> None:
        """``count`` seconds of the console's life: its ticks, its record, its link."""
        panel = self.recorded.rig.panel
        for _ in range(count):
            if bpm is not None:
                self.seq += 1
                panel.runtime.observe_ecg(
                    self.recorded.clock.monotonic(), self.seq, SignalQuality.GOOD, Bpm(bpm)
                )
            await self.recorded.tick(1.0)
            await panel.cloud_stop_step()
            await panel.cloud_step()

    async def until_finished(self, session: httpx.AsyncClient) -> None:
        stopped = await session.post(
            "/api/session/stop", json={"operator": OPERATOR, "reason": "fin de la seance"}
        )
        assert stopped.status_code == httpx.codes.ACCEPTED, stopped.text
        # A person on board is watched through a recovery after the stop: a minute at least.
        for _ in range(400):
            await self.seconds(1)
            if self.recorded.state() is RuntimeState.FINISHED:
                return
        raise AssertionError("the session did not finish")

    def feedback_requests(self) -> list[Mapping[str, JsonValue]]:
        """Every request of the events route that carried a feedback event, as sent."""
        return [
            exchange.body
            for exchange in self.dashboard.trace
            if exchange.path == EVENTS
            and any(
                cast("Mapping[str, JsonValue]", event).get("kind") == "feedback"
                for event in cast("list[JsonValue]", exchange.body["events"])
            )
        ]


def linked(
    tmp_path: Path,
    *,
    server_version: str = CONTRACT_VERSION,
    refuse_feedback: Refused | None = None,
    env: Mapping[str, str] = LINKED_ENV,
) -> Linked:
    """A console linked to a dashboard in memory that lists the two riders used here."""
    clocks: list[RecordedRig] = []
    fake = FakeDashboard(
        now=lambda: int(clocks[0].clock.unix_millis()),
        roster=roster_document(RIDER, FITTING),
        server_version=server_version,
        refuse_feedback=refuse_feedback,
    )
    recorded = recorded_rig(tmp_path, env=env, transport=lambda _config: fake)
    clocks.append(recorded)
    return Linked(recorded=recorded, dashboard=fake)


async def occupied(console: Linked, session: httpx.AsyncClient, *, run_s: int = 5) -> None:
    """A manual session with the synthetic rider on board, run, then ended by the operator."""
    await console.seconds(2)
    await attest(session)
    started = await session.post(
        "/api/manual/start",
        json={"occupancy": "occupied", "operator": OPERATOR, "subject_id": RIDER_ID},
    )
    assert started.status_code == httpx.codes.ACCEPTED, started.text
    await console.seconds(run_s)
    assert console.recorded.state() is RuntimeState.RUNNING, console.recorded.rig.refusals()
    await console.until_finished(session)


async def card(session: httpx.AsyncClient) -> Mapping[str, object] | None:
    read = await session.get("/api/feedback")
    assert read.status_code == httpx.codes.OK, read.text
    return cast("Mapping[str, object] | None", read.json()["current"])


# =========================================================================
# P1, P2, P3: when the console offers it
# =========================================================================


async def test_p1_an_occupied_manual_session_that_ends_offers_the_feedback(
    tmp_path: Path,
) -> None:
    console = linked(tmp_path)
    cloud = console.recorded.rig.panel.cloud
    assert cloud is not None
    assert cloud.session_id_reader() is None, "no session runs: nothing to follow"
    async with console.recorded.rig.http() as session:
        assert await card(session) is None
        await occupied(console, session)
        current = await card(session)
        assert current is not None
        assert (current["status"], current["can_submit"], current["code"]) == (
            "to_fill",
            True,
            None,
        )
        read = cast("Mapping[str, JsonValue]", (await session.get("/api/feedback")).json())
        nausea = cast("Mapping[str, Mapping[str, list[str]]]", read["scales"])["nausea"]
        assert nausea["labels"][3] == "Légère à modérée"
        assert read["free_text_max"] == 500
    await console.recorded.rig.panel.close()


async def test_p2_a_bench_session_that_ends_offers_nothing(tmp_path: Path) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await console.seconds(3, bpm=None)
        await console.until_finished(session)
        assert await card(session) is None
        refused = await session.post("/api/feedback", json=ENTERED)
        assert refused.status_code == httpx.codes.CONFLICT
        assert refused.json()["code"] == "nothing_to_fill"
        skipped = await session.post("/api/feedback/skip", json={})
        assert skipped.status_code == httpx.codes.CONFLICT
    assert console.feedback_requests() == []
    await console.recorded.rig.panel.close()


async def test_p3_a_programmed_session_that_ends_offers_the_feedback(tmp_path: Path) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await console.seconds(2)
        await attest(session)
        started = await session.post("/api/session/start", json=start_body(FITTING.user_id))
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await console.seconds(5)
        assert console.recorded.state() is RuntimeState.RUNNING, console.recorded.rig.refusals()
        await console.until_finished(session)
        current = await card(session)
        assert current is not None
        assert current["status"] == "to_fill"
    await console.recorded.rig.panel.close()


async def test_a_console_without_a_dashboard_offers_nothing(tmp_path: Path) -> None:
    console = linked(tmp_path, env=PROGRAMME_ENV)
    assert console.recorded.rig.panel.cloud is None
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        assert await card(session) is None
        refused = await session.post("/api/feedback", json=ENTERED)
        assert refused.status_code == httpx.codes.CONFLICT
    await console.recorded.rig.panel.close()


# =========================================================================
# P4, P6: entered and sent, or skipped
# =========================================================================


async def test_p4_an_entered_feedback_reaches_the_dashboard_once_as_one_feedback_event(
    tmp_path: Path,
) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        (server,) = console.dashboard.sessions.values()
        entered = await session.post("/api/feedback", json=ENTERED)
        assert entered.status_code == httpx.codes.ACCEPTED, entered.text
        assert entered.json()["current"]["can_submit"] is True
        await console.seconds(2)
        assert console.dashboard.feedback == {server.session_id: DETAIL}
        (request,) = console.feedback_requests()
        assert request["sessionId"] == server.session_id
        (event,) = cast("list[Mapping[str, JsonValue]]", request["events"])
        assert (event["seq"], event["kind"], event["actor"]) == (0, "feedback", "operator")
        assert json.loads(cast("str", event["detail"])) == DETAIL
        assert isinstance(event["t"], int)
        # Never among the session's events.
        assert all(
            stored.item.get("kind") != "feedback" for stored in console.dashboard.events.values()
        )
        current = await card(session)
        assert current is not None
        assert (current["status"], current["can_submit"]) == ("sent", False)
        await console.seconds(20)
        assert len(console.feedback_requests()) == 1
        # Closing the card of a sent feedback only closes it.
        closed = await session.post("/api/feedback/skip", json={})
        assert closed.status_code == httpx.codes.OK
        assert closed.json()["current"] is None
    await console.recorded.rig.panel.close()


async def test_p6_a_skipped_card_sends_nothing_and_offers_nothing_more(tmp_path: Path) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        skipped = await session.post("/api/feedback/skip", json={})
        assert skipped.status_code == httpx.codes.OK
        assert skipped.json()["current"] is None
        await console.seconds(20)
        assert await card(session) is None
        refused = await session.post("/api/feedback", json=ENTERED)
        assert refused.status_code == httpx.codes.CONFLICT
    assert console.feedback_requests() == []
    assert console.dashboard.feedback == {}
    await console.recorded.rig.panel.close()


async def test_a_second_entry_before_the_sending_replaces_the_first(tmp_path: Path) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        first = {**ENTERED, "nausea_score": 2}
        assert (await session.post("/api/feedback", json=first)).status_code == 202
        assert (await session.post("/api/feedback", json=ENTERED)).status_code == 202
        await console.seconds(2)
    assert len(console.feedback_requests()) == 1
    assert list(console.dashboard.feedback.values()) == [DETAIL]
    await console.recorded.rig.panel.close()


# =========================================================================
# P5: what the link does to it
# =========================================================================


async def test_p5_a_dashboard_of_1_6_is_sent_nothing(tmp_path: Path) -> None:
    console = linked(tmp_path, server_version="1.6")
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        await session.post("/api/feedback", json=ENTERED)
        await console.seconds(20)
        current = await card(session)
        assert current is not None
        assert current["status"] == "server_too_old"
        assert "1.7" in cast("str", current["status_text"])
    assert console.feedback_requests() == []
    assert all(
        stored.item.get("kind") != "feedback" for stored in console.dashboard.events.values()
    )
    await console.recorded.rig.panel.close()


async def test_p5_a_lost_link_sends_it_when_it_returns(tmp_path: Path) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        console.dashboard.online = False
        await session.post("/api/feedback", json=ENTERED)
        await console.seconds(5)
        current = await card(session)
        assert current is not None
        assert current["status"] == "waiting_link"
        console.dashboard.online = True
        await console.seconds(20)
        current = await card(session)
        assert current is not None
        assert current["status"] == "sent"
    assert list(console.dashboard.feedback.values()) == [DETAIL]
    await console.recorded.rig.panel.close()


async def test_p5_a_session_declared_after_the_entry_gets_it_once_declared(
    tmp_path: Path,
) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await console.seconds(2)
        # No network for the whole session: the dashboard never heard of it.
        console.dashboard.online = False
        await occupied(console, session)
        assert console.dashboard.sessions == {}
        entered = await session.post("/api/feedback", json=ENTERED)
        assert entered.status_code == 202
        await console.seconds(2)
        current = await card(session)
        assert current is not None
        assert current["status"] == "waiting_session"
        console.dashboard.online = True
        await console.seconds(40)
        (server,) = console.dashboard.sessions.values()
        current = await card(session)
        assert current is not None
        assert current["status"] == "sent"
    assert console.dashboard.feedback == {server.session_id: DETAIL}
    await console.recorded.rig.panel.close()


@pytest.mark.parametrize("status", [409, 400])
async def test_p5_a_refusal_is_said_with_its_code(tmp_path: Path, status: int) -> None:
    console = linked(
        tmp_path,
        refuse_feedback=Refused(
            status,
            "The feedback window of the session is closed",
            ErrorCode("feedback_window_closed"),
        ),
    )
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        await session.post("/api/feedback", json=ENTERED)
        await console.seconds(20)
        current = await card(session)
        assert current is not None
        assert (current["status"], current["code"], current["can_submit"]) == (
            "refused",
            "feedback_window_closed",
            False,
        )
        assert cast("str", current["status_text"]).endswith("(feedback_window_closed)")
    assert len(console.feedback_requests()) == 1
    await console.recorded.rig.panel.close()


# =========================================================================
# P7 at the route, P8: nothing of it anywhere else
# =========================================================================


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({**ENTERED, "nausea_score": -1}, "nausea_score"),
        ({**ENTERED, "nausea_score": 11}, "nausea_score"),
        ({**ENTERED, "dizziness_score": 2.5}, "dizziness_score"),
        ({**ENTERED, "comfort": "great"}, "comfort"),
        ({**ENTERED, "free_text": "x" * 501}, "free_text"),
        ({**ENTERED, "rider": "Synthetic Rider"}, "body"),
    ],
)
async def test_p7_the_route_refuses_off_the_scale_naming_the_field(
    tmp_path: Path, body: Mapping[str, JsonValue], field: str
) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        refused = await session.post("/api/feedback", json=body)
        assert refused.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        assert refused.json()["code"] == "invalid_feedback"
        assert field in refused.json()["detail"]
        current = await card(session)
        assert current is not None
        assert current["status"] == "to_fill"
    await console.recorded.rig.panel.close()


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b"null", b"x" * 9000])
async def test_a_body_that_is_not_a_json_object_is_refused_without_reading_further(
    tmp_path: Path, raw: bytes
) -> None:
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        refused = await session.post(
            "/api/feedback", content=raw, headers={"Content-Type": "application/json"}
        )
        assert refused.status_code in (httpx.codes.BAD_REQUEST, httpx.codes.UNPROCESSABLE_ENTITY)
    await console.recorded.rig.panel.close()


async def test_p8_the_feedback_is_in_no_log_no_file_no_record_and_no_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    console = linked(tmp_path)
    async with console.recorded.rig.http() as session:
        await occupied(console, session)
        refusals = [
            (await session.post("/api/feedback", json=body)).text
            for body in (
                {**ENTERED, "free_text": SENTINEL * 20},
                {**ENTERED, "nausea_score": 77},
                {**ENTERED, "comfort": SENTINEL},
            )
        ]
        console.dashboard.online = False
        answers = [(await session.post("/api/feedback", json=ENTERED)).text]
        await console.seconds(5)
        console.dashboard.online = True
        await console.seconds(20)
        answers.append((await session.get("/api/feedback")).text)
    assert console.dashboard.feedback != {}, "it was sent"
    await console.recorded.rig.panel.close()
    console.recorded.drain()

    formatter = JsonLines()
    logged = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [formatter.format(record) for record in caplog.records]
    )
    record = console.recorded.recording()
    for where, text in (
        ("the log", logged),
        ("the refusals", "\n".join(refusals)),
        ("the answers", "\n".join(answers)),
        ("the record's events", "\n".join(repr(event) for event in record.events)),
    ):
        assert SENTINEL not in text, where
        # The keys of what was sent: a refusal names a field of the form, never more.
        for key in ("nauseaScore", "dizzinessScore", "freeText"):
            assert key not in text, f"{key} in {where}"
    # The score refused is not quoted back either.
    assert "77" not in "\n".join(refusals)
    assert SENTINEL.encode() not in console.recorded.everything_on_disk()
    assert SENTINEL.encode() not in console.recorded.everything_under(tmp_path)
