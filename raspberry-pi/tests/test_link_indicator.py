"""ANH-210: the console says its version and the state of its link with the dashboard.

Three layers:

* the real link (:class:`~src.cloud_sync.CloudSync`) against a scripted
  dashboard: what the operator reads under each thing a dashboard can do
  (answer, stay silent, refuse the key, refuse the contract, announce another
  contract, fail), and that reading it asks the dashboard nothing;
* the real console in simulation, through its HTTP API: the acceptance
  scenario of the ticket (a dashboard that answers 426 in the middle of a
  session), a console with no machine key, and the one version the page, the
  heartbeat and the session record all carry;
* the control tick: nothing of the indicator runs in it.

The state machine itself, instant by instant, is in ``tests/test_link_state.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Final, cast, override

import httpx
import pytest

from src import local_panel
from src.clock import ManualClock
from src.cloud_sync import (
    HEARTBEAT_PERIOD,
    STATUS_PERIOD,
    CloudError,
    Document,
    HttpxTransport,
    Refused,
)
from src.contract import (
    CONTRACT_UNSUPPORTED,
    CONTRACT_VERSION,
    MAX_REFUSAL_SENTENCE,
    SERVER_VERSION_FIELD,
    VERSION_PATH,
    ErrorCode,
    read_software_version,
)
from src.control_surface import EventKind as SurfaceEvent
from src.link_state import MAX_DETAIL, UNREACHABLE_AFTER, LinkHealth, LinkState, LinkStatus
from src.local_config import RETIRED_KEYS, CloudConfig, load_local_config, retired_keys
from src.local_panel import EXIT_CONFIG, main
from src.record.cursor import Cursor, load
from src.record.journal import Journal
from src.record.reader import Recording, read
from src.record.retention import records
from src.record.schema import EventKind
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, Seconds, UnixMillis
from src.web.schemas import PanelRow
from tests.fake_dashboard import END, LOCAL, TELEMETRY, FakeDashboard
from tests.test_cloud_contract import HEARTBEAT, OTHER_MAJOR, STATUS, unsupported
from tests.test_cloud_sync import (
    DOWN,
    LAUNCH,
    NO,
    POLL_PATH,
    Dashboard,
    Reply,
    Rig,
    launch_answer,
    linked,
    manual,
    ok,
    rig,
    status_answer,
    transport_answering,
)
from tests.test_failure_rig import BENCH_ENV, OPERATOR, TICK, attest, make_rig
from tests.test_failure_rig import Rig as Console
from tests.test_record_wiring import recording_linked
from tests.test_web_api import parse

SENTENCE: Final[str] = f"serveur incompatible (contrat {CONTRACT_VERSION} vs 2)"
"""What a dashboard that serves only major 2 makes this console say."""

GATED: Final[tuple[str, ...]] = (
    HEARTBEAT,
    POLL_PATH,
    "/api/machine/profiles",
    LOCAL,
    "/api/machine/training/start",
    TELEMETRY,
    "/api/machine/training/events",
    END,
)
"""Every route the dashboard serves only to a console whose major it serves."""

BUILT: Final[str] = VERSION_PATH.read_text(encoding="utf-8").strip()
"""The version of this checkout: the content of ``raspberry-pi/VERSION``."""


def every_route(dashboard: Dashboard, reply: Reply, *, status: Reply | None = None) -> None:
    """The dashboard answers ``reply`` to everything; ``status`` to the stop question."""
    for path in GATED:
        dashboard.answer(path, reply)
    dashboard.answer(STATUS, reply if status is None else status)


def read_link(r: Rig) -> LinkStatus:
    """Read afresh: a checker keeps a property's narrowing across an ``await``."""
    return r.sync.link


# =========================================================================
# The real link, a scripted dashboard: what the operator reads
# =========================================================================


async def test_configured_and_not_yet_answered_reads_waiting_then_reachable(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    assert read_link(r) == LinkStatus(LinkState.WAITING, "", None)
    await r.step()
    assert read_link(r) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


async def test_a_dashboard_never_reached_reads_waiting_then_unreachable_with_the_reason(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    every_route(r.dashboard, DOWN)
    seen: list[tuple[float, LinkState]] = []
    born = r.clock.monotonic()
    for _ in range(40):
        await r.step()
        seen.append((r.clock.monotonic() - born, read_link(r).state))
    for since_start, state in seen:
        expected = LinkState.UNREACHABLE if since_start >= UNREACHABLE_AFTER else LinkState.WAITING
        assert state is expected, since_start
    last = read_link(r)
    assert last.detail == "no route to host"
    assert last.last_answer_age is None


async def test_one_lost_heartbeat_changes_nothing_and_two_in_a_row_read_unreachable(
    tmp_path: Path,
) -> None:
    """The slowest cadence of the link: no programme, so no poll, one heartbeat per 10 s."""
    r = rig(tmp_path, programs=False)
    lost: list[int] = [2]  # the second heartbeat is lost, and only it
    beats = 0

    def heartbeat(_body: Mapping[str, object]) -> Reply:
        nonlocal beats
        beats += 1
        return DOWN if beats in lost else ok()

    r.dashboard.handlers[HEARTBEAT] = heartbeat
    seen: set[LinkState] = set()
    for _ in range(int(HEARTBEAT_PERIOD) * 2 + 5):
        await r.step()
        seen.add(read_link(r).state)
    assert beats == 3
    assert seen == {LinkState.REACHABLE}, "one request lost never shows"

    # Now two in a row.
    lost.extend([4, 5])
    states: list[LinkState] = []
    for _ in range(int(HEARTBEAT_PERIOD) * 3):
        await r.step()
        states.append(read_link(r).state)
    assert LinkState.UNREACHABLE in states
    first = states.index(LinkState.UNREACHABLE)
    # Not before the delay, and once said it stays until an answer...
    assert set(states[:first]) == {LinkState.REACHABLE}
    assert first >= int(UNREACHABLE_AFTER - HEARTBEAT_PERIOD)
    # ...which makes the link reachable again at once: the sixth heartbeat.
    assert states[-1] is LinkState.REACHABLE
    back = len(states) - 1 - states[::-1].index(LinkState.UNREACHABLE) + 1
    assert set(states[first:back]) == {LinkState.UNREACHABLE}
    assert set(states[back:]) == {LinkState.REACHABLE}
    assert beats == 6


async def test_a_silent_dashboard_reads_unreachable_25_s_after_its_last_answer_and_back_at_once(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    await r.step()
    answered_at = r.clock.monotonic()
    every_route(r.dashboard, DOWN)
    for _ in range(40):
        await r.step()
        quiet = r.clock.monotonic() - answered_at
        status = read_link(r)
        assert (status.state is LinkState.UNREACHABLE) == (quiet >= UNREACHABLE_AFTER), quiet
        assert status.state in (LinkState.REACHABLE, LinkState.UNREACHABLE)
        assert status.last_answer_age == Seconds(quiet)
    assert read_link(r).detail == "no route to host"
    r.dashboard.handlers.clear()
    await r.step(float(HEARTBEAT_PERIOD))
    assert read_link(r) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (
            Refused(401, "<b>Invalid API key</b>", ErrorCode("unauthorized")),
            "HTTP 401 (unauthorized)",
        ),
        (Refused(403, "Forbidden\nby a proxy"), "HTTP 403"),
    ],
)
async def test_a_refused_key_is_read_at_once_without_the_dashboards_own_words(
    tmp_path: Path, refusal: Refused, words: str
) -> None:
    r = rig(tmp_path)
    await r.step()
    every_route(r.dashboard, Err(refusal))
    await r.step(float(HEARTBEAT_PERIOD))
    status = read_link(r)
    assert (status.state, status.detail) == (LinkState.KEY_REFUSED, words)
    # It answers, and that is said too: the age is of that answer.
    assert status.last_answer_age == Seconds(0.0)
    for _ in range(30):
        await r.step()
        assert read_link(r).state is LinkState.KEY_REFUSED
    r.dashboard.handlers.clear()
    await r.step(float(HEARTBEAT_PERIOD))
    assert read_link(r).state is LinkState.REACHABLE


async def test_a_refused_contract_stays_incompatible_while_the_stop_question_is_answered(
    tmp_path: Path,
) -> None:
    """A session runs, everything is refused 426, the status route answers: no flapping."""
    r = rig(tmp_path)
    r.dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    r.sync.session_started(manual(r.clock))
    r.runtime.state = RuntimeState.RUNNING
    await r.step()
    await r.step()
    assert read_link(r).state is LinkState.REACHABLE
    asked = len(r.dashboard.to(STATUS))
    assert asked >= 1, "the session is followed: its stop is asked for"

    every_route(r.dashboard, unsupported("2"), status=status_answer())
    await r.step(float(HEARTBEAT_PERIOD))
    seen: set[LinkState] = set()
    for _ in range(60):
        await r.step()
        seen.add(read_link(r).state)
    assert len(r.dashboard.to(STATUS)) >= asked + 15, "answered about every 3 s meanwhile"
    assert seen == {LinkState.INCOMPATIBLE}
    assert read_link(r).detail == SENTENCE

    # The dashboard serves this console's major again.
    r.dashboard.handlers.clear()
    r.dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    await r.step(float(HEARTBEAT_PERIOD))
    assert read_link(r) == LinkStatus(LinkState.REACHABLE, "", Seconds(0.0))


async def test_the_stop_question_keeps_the_last_answer_fresh_and_proves_nothing_of_the_contract(
    tmp_path: Path,
) -> None:
    """The stop watch asks every 3 s, from its own task: an answer, never a proof.

    After a 426, everything but the stop question goes silent. Its answers
    alone keep the link from reading unreachable, and its last answer is
    never more than a few seconds old; they do not make it read reachable.
    """
    r = rig(tmp_path)
    r.dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    r.sync.session_started(manual(r.clock))
    r.runtime.state = RuntimeState.RUNNING
    await r.step()
    await r.step()
    every_route(r.dashboard, unsupported("2"), status=status_answer())
    await r.step(float(HEARTBEAT_PERIOD))
    assert read_link(r).state is LinkState.INCOMPATIBLE

    every_route(r.dashboard, DOWN, status=status_answer())
    asked = len(r.dashboard.to(STATUS))
    for _ in range(90):
        await r.step()
        status = read_link(r)
        assert status.state is LinkState.INCOMPATIBLE
        assert status.last_answer_age is not None
        assert status.last_answer_age <= STATUS_PERIOD
    assert len(r.dashboard.to(STATUS)) >= asked + 25

    # The stop question falls silent too: now nothing answers, and that is what is read.
    every_route(r.dashboard, DOWN)
    for _ in range(int(UNREACHABLE_AFTER) + int(STATUS_PERIOD) + 1):
        await r.step()
    assert read_link(r).state is LinkState.UNREACHABLE


async def test_a_poll_answer_of_another_major_reads_incompatible_until_a_poll_agrees(
    tmp_path: Path,
) -> None:
    """It accepts everything this console sends: only what it announces says what it is."""
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(None, version="2.0"))
    await r.step()
    seen: set[LinkState] = set()
    for _ in range(30):
        await r.step()
        seen.add(read_link(r).state)
    assert len(r.dashboard.to(HEARTBEAT)) >= 3, "heartbeats were accepted meanwhile"
    assert seen == {LinkState.INCOMPATIBLE}
    assert read_link(r).detail == OTHER_MAJOR
    r.dashboard.answer(POLL_PATH, launch_answer())
    await r.step(3.0)
    assert read_link(r).state is LinkState.REACHABLE


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (Refused(503, "Service Unavailable"), "HTTP 503"),
        (Refused(429, "slow down", ErrorCode("request_failed")), "HTTP 429 (request_failed)"),
        # A 426 that does not carry the contract's stable code is not the dashboard
        # saying which majors it serves: an error, never "incompatible".
        (Refused(426, "Upgrade Required"), "HTTP 426"),
    ],
)
async def test_a_dashboard_that_only_answers_errors_reads_en_erreur_after_the_same_delay(
    tmp_path: Path, refusal: Refused, words: str
) -> None:
    r = rig(tmp_path)
    await r.step()
    answered_at = r.clock.monotonic()
    every_route(r.dashboard, Err(refusal))
    for _ in range(40):
        await r.step()
        quiet = r.clock.monotonic() - answered_at
        state = read_link(r).state
        assert (state is LinkState.SERVER_ERROR) == (quiet >= UNREACHABLE_AFTER), quiet
        assert state in (LinkState.REACHABLE, LinkState.SERVER_ERROR)
    assert read_link(r).detail == words


async def test_a_refusal_of_what_was_sent_is_a_dashboard_that_works(tmp_path: Path) -> None:
    r = rig(tmp_path)
    every_route(r.dashboard, NO)  # 400, about the request
    for _ in range(40):
        await r.step()
        assert read_link(r).state is LinkState.REACHABLE


async def test_a_426_listing_hundreds_of_majors_reaches_the_indicator_as_a_short_sentence(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    majors = tuple(str(major) for major in range(2, 600))
    every_route(r.dashboard, unsupported(*majors))
    await r.step()
    status = read_link(r)
    assert status.state is LinkState.INCOMPATIBLE
    assert status.detail == (
        f"serveur incompatible (contrat {CONTRACT_VERSION} vs 2, 3, 4, 5 et 594 autres)"
    )
    assert len(status.detail) <= MAX_REFUSAL_SENTENCE <= MAX_DETAIL


async def test_reading_the_indicator_asks_the_dashboard_nothing(tmp_path: Path) -> None:
    """EX-2: the state is what the link already knows. Same requests, read or not."""
    watched = rig(tmp_path / "watched")
    alone = rig(tmp_path / "alone")
    for r in (watched, alone):
        r.dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
        r.sync.session_started(manual(r.clock))
        r.runtime.state = RuntimeState.RUNNING
    read = 0
    for second in range(90):
        if second == 30:
            for r in (watched, alone):
                every_route(r.dashboard, unsupported("2"), status=status_answer())
        if second == 60:
            for r in (watched, alone):
                every_route(r.dashboard, DOWN)
        await alone.step()
        await watched.step()
        for _ in range(10):
            assert read_link(watched).state in LinkState
            read += 1
    assert read == 900
    assert len(watched.dashboard.calls) >= 30
    assert [(m, p) for m, p, _b in watched.dashboard.calls] == [
        (m, p) for m, p, _b in alone.dashboard.calls
    ]


# =========================================================================
# The real console, through its HTTP API
# =========================================================================

LINKED_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

TRUE_EPOCH_MS: Final[int] = 1_790_000_000_000
"""September 2026: the machine's clock is right in this scenario."""

type Body = Mapping[str, JsonValue]


@dataclass
class Versioned:
    """A dashboard that can stop serving this console's major, and serve it again.

    While it does not, it answers what the real one answers
    (``contracts/machine-api.json``): 426 with the majors it serves to every
    route, and the stop question as ever, since a stop crosses every contract.
    """

    inner: FakeDashboard
    served: bool = True
    beats: list[Body] = field(default_factory=list[Body])
    refused: list[str] = field(default_factory=list[str])
    status_answers_while_refusing: int = 0

    def _refusal(self, path: str) -> Result[Document, CloudError]:
        self.refused.append(path)
        return Err(Refused(426, "Unsupported machine contract", CONTRACT_UNSUPPORTED, ("2",)))

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        if self.served:
            return await self.inner.get(path, params)
        if path != STATUS:
            return self._refusal(path)
        self.status_answers_while_refusing += 1
        return await self.inner.get(path, params)

    async def post(self, path: str, body: Body) -> Result[Document, CloudError]:
        if not self.served:
            return self._refusal(path)
        if path == HEARTBEAT:
            self.beats.append(body)
        return await self.inner.post(path, body)


@dataclass
class Scene:
    """The console, its records, its dashboard, and the operator's browser."""

    console: Console
    journal: Journal
    dashboard: Versioned
    browser: httpx.AsyncClient
    shown: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """The state and the reason the page was given, once per simulated second."""

    async def panel(self) -> PanelRow:
        """What the page is served when it polls, as it does every second."""
        return parse(PanelRow, await self.browser.get("/api/panel"))

    async def seconds(self, count: int) -> None:
        """``count`` seconds of the console's life, the page polling once in each."""
        for _ in range(count):
            for _tick in range(round(1.0 / TICK)):
                await self.console.tick(float(TICK))
                self.journal.drain()
                await self.console.panel.cloud_stop_step()
            await self.console.panel.cloud_step()
            row = await self.panel()
            self.shown.append((row.dashboard.state, row.dashboard.detail))

    def state(self) -> RuntimeState:
        """Read afresh: a checker keeps a narrowing across an ``await``."""
        return self.console.panel.runtime.state

    def states_since(self, mark: int) -> list[str]:
        return [state for state, _detail in self.shown[mark:]]


def scene(tmp_path: Path, env: Mapping[str, str] = LINKED_ENV) -> Scene:
    clock = ManualClock(Monotonic(10.0), UnixMillis(TRUE_EPOCH_MS))
    dashboard = Versioned(FakeDashboard(now=lambda: int(clock.unix_millis())))
    journal = Journal(tmp_path / "records", clock)

    def to_the_dashboard(_config: CloudConfig) -> Versioned:
        return dashboard

    console, _ = make_rig(
        tmp_path, env=env, clock=clock, transport=to_the_dashboard, journal=journal
    )
    return Scene(console=console, journal=journal, dashboard=dashboard, browser=console.http())


def announced_versions(dashboard: Versioned) -> list[str]:
    """Every distinct ``software_version`` the heartbeats carried, in the order first seen."""
    assert dashboard.beats, "no heartbeat reached the dashboard"
    return list(dict.fromkeys(str(beat["software_version"]) for beat in dashboard.beats))


def recording_of(journal: Journal) -> tuple[Path, Recording]:
    (record,) = records(journal.root)
    loaded = read(record)
    assert isinstance(loaded, Ok), loaded
    return record, loaded.value


async def test_acceptance_a_dashboard_answers_426_in_the_middle_of_a_session(
    tmp_path: Path,
) -> None:
    """The acceptance of the ticket, on the real console in simulation.

    A session is started at the machine and reaches the dashboard. The
    dashboard then stops serving this console's major: the page reads
    ``incompatible`` for as long as that lasts, the session goes on and ends
    under the local supervisor, and nothing of it is lost. When the dashboard
    serves the major again the page reads ``joignable``, and the session is
    whole and closed on the dashboard, with its reason.

    One test on purpose, as the other acceptance scenarios: each thing
    established is a function below, named for what it says.
    """
    s = scene(tmp_path)
    async with s.browser as browser:
        await attest(browser)
        started = await browser.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == 202, started.text
        await s.seconds(3)
        target = await browser.post(
            "/api/manual/target", json={"output_rpm": 6.0, "operator": OPERATOR}
        )
        assert target.status_code == 202, target.text
        await s.seconds(9)
        the_session_is_on_the_dashboard_and_the_link_reads_reachable(s)

        # The dashboard is replaced by one of another major, the session still running.
        s.dashboard.served = False
        refused_from = len(s.shown)
        received = len(s.dashboard.inner.trace)
        await s.seconds(40)
        assert s.state() is RuntimeState.RUNNING, "the session goes on under the local supervisor"
        the_page_reads_incompatible_for_as_long_as_it_lasts(s, refused_from)

        # The session ends at the console, the dashboard still refusing.
        stopped = await browser.post(
            "/api/session/stop", json={"operator": OPERATOR, "reason": "fini"}
        )
        assert stopped.status_code == 202, stopped.text
        for _ in range(60):
            await s.seconds(1)
            if s.state() is not RuntimeState.RUNNING and s.state() is not RuntimeState.ENDING:
                break
        await s.seconds(3)
        assert s.state() in (RuntimeState.FINISHED, RuntimeState.IDLE)
        nothing_reached_the_dashboard_and_nothing_was_dropped(s, received)
        assert set(s.states_since(refused_from)[int(HEARTBEAT_PERIOD) :]) == {"incompatible"}

        # The dashboard serves this console's major again.
        s.dashboard.served = True
        served_from = len(s.shown)
        await s.seconds(40)
        the_page_reads_reachable_again_and_stays_so(s, served_from)
        the_session_is_whole_and_closed_on_the_dashboard(s)
        the_notice_is_in_the_record_of_the_session(s)
        the_record_the_page_and_the_heartbeat_carry_the_version_of_the_build(s, await s.panel())
    await s.console.panel.close()


def the_session_is_on_the_dashboard_and_the_link_reads_reachable(s: Scene) -> None:
    (session,) = s.dashboard.inner.sessions.values()
    assert session.status == "active"
    assert len(s.dashboard.inner.telemetry) >= 5, "its telemetry has begun to arrive"
    assert s.shown[-1] == ("reachable", "")
    assert set(s.states_since(0)) <= {"waiting", "reachable"}
    assert s.dashboard.refused == []


def the_page_reads_incompatible_for_as_long_as_it_lasts(s: Scene, since: int) -> None:
    """EX-2. At the first refused exchange, then every second, whatever else is answered."""
    states = s.states_since(since)
    first = states.index("incompatible")
    # Until an exchange was refused the console had heard nothing new: at most
    # one heartbeat period, in fact one telemetry period.
    assert first <= int(HEARTBEAT_PERIOD)
    assert set(states[:first]) <= {"reachable"}
    assert set(states[first:]) == {"incompatible"}, "a standing state: it does not flap"
    assert len(states) - first >= 30
    assert s.shown[-1] == ("incompatible", SENTENCE)
    # The stop question was answered all along: it crosses every contract, and
    # its answers are not taken for a dashboard that serves this console.
    assert s.dashboard.status_answers_while_refusing >= 10
    assert {HEARTBEAT, TELEMETRY} <= set(s.dashboard.refused)


def nothing_reached_the_dashboard_and_nothing_was_dropped(s: Scene, received: int) -> None:
    """EX-1, first half: the session ended here, and the dashboard still holds it running."""
    inner = s.dashboard.inner
    assert len(inner.trace) == received, "nothing was received under the refusal"
    (session,) = inner.sessions.values()
    assert session.status == "active"
    cloud = s.console.panel.cloud
    assert cloud is not None
    assert cloud.owed == 1
    record, recording = recording_of(s.journal)
    assert recording.manifest.end_reason == "operator_stop"
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert (cursor.end, cursor.state) == ("pending", "pending")


def the_page_reads_reachable_again_and_stays_so(s: Scene, since: int) -> None:
    states = s.states_since(since)
    first = states.index("reachable")
    assert set(states[:first]) == {"incompatible"}
    assert set(states[first:]) == {"reachable"}
    # Idle, the console asks for a launch every 3 s: that answer is the proof.
    assert first <= int(HEARTBEAT_PERIOD)
    assert s.shown[-1] == ("reachable", "")


def the_session_is_whole_and_closed_on_the_dashboard(s: Scene) -> None:
    """EX-1: declared once, every second of the record there once, closed with its reason."""
    inner = s.dashboard.inner
    record, recording = recording_of(s.journal)
    (session,) = inner.sessions.values()
    assert (session.status, session.end_reason) == ("completed", "operator_stop: fini")
    expected = sorted({round(row.t * 1000) // 1000 for row in recording.rows if row.t >= 0})
    stored = [item.item for _key, item in sorted(inner.telemetry.items())]
    assert [int(cast("float", point["elapsedS"])) for point in stored] == expected
    assert len(expected) >= 50
    assert (inner.duplicates, inner.rejected) == (0, 0)
    events = [item.item for _key, item in sorted(inner.events.items())]
    assert [event["seq"] for event in events] == list(range(len(recording.events)))
    paths = [exchange.path for exchange in inner.trace]
    assert (paths.count(LOCAL), paths.count(END)) == (1, 1)
    cloud = s.console.panel.cloud
    assert cloud is not None
    assert cloud.owed == 0
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert (cursor.end, cursor.state, cursor.refused) == ("sent", "complete", 0)


def the_notice_is_in_the_record_of_the_session(s: Scene) -> None:
    """EX-3: what the operator was shown about the dashboard is in ``events.jsonl``."""
    record, recording = recording_of(s.journal)
    assert recording.warnings == ()
    about = [(e.kind, e.detail, e.actor) for e in recording.events if "incompatible" in e.detail]
    assert about, "the notice is in the record"
    assert set(about) == {(EventKind.WARNING, f"dashboard: {SENTENCE}", "system")}
    # Said when it starts and recalled once a minute at most: not once per refused request.
    assert 1 <= len(about) <= 2
    lines = [
        line
        for line in (record / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if SENTENCE in line
    ]
    assert len(lines) == len(about)
    assert all('"kind":"warning"' in line.replace(" ", "") for line in lines)
    # And the dashboard holds it too, now that it has the events of the session.
    stored = [item.item["detail"] for item in s.dashboard.inner.events.values()]
    assert f"dashboard: {SENTENCE}" in stored


def the_record_the_page_and_the_heartbeat_carry_the_version_of_the_build(
    s: Scene, row: PanelRow
) -> None:
    """EX-5 and EX-2: one value, read from ``raspberry-pi/VERSION``."""
    _record, recording = recording_of(s.journal)
    assert read_software_version() == BUILT
    assert recording.manifest.software_version == BUILT
    assert row.software_version == BUILT
    assert announced_versions(s.dashboard) == [BUILT]


MANY: Final[int] = 5000
"""How many majors the dashboard of the next test lists in its 426."""

EVENT_LINE_MAX: Final[int] = 200
"""The most characters the line of ``events.jsonl`` that holds the notice may take."""


def refusing_with_thousands_of_majors(request: httpx.Request) -> httpx.Response:
    """A dashboard whose 426 lists :data:`MANY` majors, each twice, and things that are none."""
    if request.url.path == STATUS:
        return httpx.Response(
            200, json={"active": True, "stopRequested": False, SERVER_VERSION_FIELD: "2.0"}
        )
    majors = [str(major) for major in range(2, 2 + MANY)]
    supported: list[str | int | None] = [*majors, *majors]
    supported.extend(["x" * 5000, 7, None, "<script>alert(1)</script>"])
    return httpx.Response(
        426,
        json={
            "error": "contract_unsupported",
            "message": "Unsupported machine contract " + "!" * 20_000,
            "supported": supported,
        },
    )


async def test_a_426_listing_thousands_of_majors_stays_a_short_line_on_the_page_and_in_the_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Text that comes from the link reaches the operator bounded: the event and the record.

    The whole path, from the bytes of the answer: the real HTTP transport
    reads the 426, the link words it, the console shows it and the session
    record keeps it. The dashboard lists five thousand majors, twice.
    """
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    sentence = (
        f"serveur incompatible (contrat {CONTRACT_VERSION} vs 2, 3, 4, 5 et {MANY - 4} autres)"
    )
    clock = ManualClock(Monotonic(10.0), UnixMillis(TRUE_EPOCH_MS))
    journal = Journal(tmp_path / "records", clock)
    transport = transport_answering(refusing_with_thousands_of_majors)

    def to_the_dashboard(_config: CloudConfig) -> HttpxTransport:
        return transport

    console, _ = make_rig(
        tmp_path, env=LINKED_ENV, clock=clock, transport=to_the_dashboard, journal=journal
    )
    async with console.http() as browser:
        await attest(browser)
        started = await browser.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == 202, started.text
        for _ in range(int(HEARTBEAT_PERIOD) + 3):
            for _tick in range(round(1.0 / TICK)):
                await console.tick(float(TICK))
                journal.drain()
                await console.panel.cloud_stop_step()
            await console.panel.cloud_step()
        row = parse(PanelRow, await browser.get("/api/panel"))
        assert console.panel.runtime.state is RuntimeState.RUNNING, "the session goes on"
        stopped = await browser.post(
            "/api/session/stop", json={"operator": OPERATOR, "reason": "fini"}
        )
        assert stopped.status_code == 202, stopped.text
        for _ in range(200):
            await console.tick(float(TICK))
            journal.drain()
    await console.panel.close()
    await transport.close()

    # What the operator was shown: the event of the list, and the standing chip.
    shown = [event.detail for event in console.events if event.kind is SurfaceEvent.DASHBOARD]
    assert shown, "the notice reached the operator's event list"
    assert set(shown) == {sentence}
    assert len(sentence) <= MAX_REFUSAL_SENTENCE
    assert (row.dashboard.state, row.dashboard.detail) == ("incompatible", sentence)

    # What the record keeps: one short line, whole, and a record that still reads back clean.
    record, recording = recording_of(journal)
    assert recording.warnings == ()
    kept = [event.detail for event in recording.events if "incompatible" in event.detail]
    assert set(kept) == {f"dashboard: {sentence}"}
    lines = [
        line
        for line in (record / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if "serveur incompatible" in line
    ]
    assert len(lines) == len(kept) >= 1
    assert max(len(line) for line in lines) <= EVENT_LINE_MAX
    on_disk = b"".join(p.read_bytes() for p in journal.root.rglob("*") if p.is_file())
    assert b"<script>" not in on_disk
    assert b"!!!!" not in on_disk, "the dashboard's own sentence is not in the record"

    # The log names the sentence too, and what it keeps of the answer is the code, not the list.
    said = [record_.getMessage() for record_ in caplog.records if sentence in record_.getMessage()]
    assert said == [f"dashboard: {sentence}"]
    assert all("2, 3, 4, 5, 6" not in record_.getMessage() for record_ in caplog.records)


async def test_ex2_a_console_without_a_machine_key_shows_its_version_and_a_link_not_configured(
    tmp_path: Path,
) -> None:
    console, _ = make_rig(tmp_path)
    assert console.panel.cloud is None
    async with console.http() as browser:
        await console.tick(1.0)
        row = parse(PanelRow, await browser.get("/api/panel"))
        assert (row.dashboard.state, row.dashboard.label) == ("not_configured", "non configure")
        assert "MACHINE_API_KEY" in row.dashboard.detail
        assert row.dashboard.last_answer_age_s is None
        assert row.software_version == BUILT
        # Not a state that changes with time: an hour later, the same.
        console.clock.advance(Seconds(3600.0))
        later = parse(PanelRow, await browser.get("/api/panel"))
        assert later.dashboard == row.dashboard
    await console.panel.close()


async def test_ex5_the_page_the_heartbeat_and_the_record_take_one_version_from_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EX-5: the version is read once, from the file, and the environment has no say.

    Another build is stood in for by another content of ``VERSION``; the key
    the manifest used to take its version from is set, and changes nothing.
    """
    built = tmp_path / "VERSION"
    built.write_text("pi-9.8.7\n", encoding="utf-8")
    monkeypatch.setattr(local_panel, "read_software_version", lambda: read_software_version(built))
    s = scene(tmp_path / "console", {**LINKED_ENV, "ANHEART_SOFTWARE_VERSION": "set-by-hand"})
    async with s.browser as browser:
        await attest(browser)
        started = await browser.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == 202, started.text
        await s.seconds(3)
        row = await s.panel()
    await s.console.panel.close()
    _record, recording = recording_of(s.journal)
    assert recording.manifest.software_version == "pi-9.8.7"
    assert row.software_version == "pi-9.8.7"
    assert announced_versions(s.dashboard) == ["pi-9.8.7"]
    on_disk = b"".join(p.read_bytes() for p in s.journal.root.rglob("*") if p.is_file())
    assert b"set-by-hand" not in on_disk


def test_ex5_the_retired_key_is_no_longer_a_setting_and_no_longer_stops_the_console() -> None:
    """What happens to ``ANHEART_SOFTWARE_VERSION``: not read, whatever it holds."""
    assert set(RETIRED_KEYS) == {"ANHEART_SOFTWARE_VERSION"}
    plain = load_local_config(BENCH_ENV)
    # A value the old key refused at startup (a space) is no longer looked at.
    set_by_hand = load_local_config({**BENCH_ENV, "ANHEART_SOFTWARE_VERSION": "version one"})
    assert isinstance(plain, Ok)
    assert isinstance(set_by_hand, Ok)
    assert set_by_hand.value == plain.value
    assert not hasattr(plain.value.record, "software_version")
    assert retired_keys(BENCH_ENV) == ()
    assert retired_keys({**BENCH_ENV, "ANHEART_SOFTWARE_VERSION": "  "}) == ()
    assert retired_keys({**BENCH_ENV, "ANHEART_SOFTWARE_VERSION": "pi-1.0.0"}) == (
        "ANHEART_SOFTWARE_VERSION",
    )


def test_ex5_a_console_started_with_the_retired_key_says_once_that_it_is_not_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="src.local_panel")
    # A configuration that is refused anyway: the console says it and does not start.
    exit_code = main({"MOTOR_BACKEND": "warp", "ANHEART_SOFTWARE_VERSION": "set-by-hand"})
    assert exit_code == EXIT_CONFIG
    said = [record.getMessage() for record in caplog.records if "no longer read" in record.message]
    assert len(said) == 1
    assert "ANHEART_SOFTWARE_VERSION" in said[0]
    assert "VERSION file" in said[0]
    assert "set-by-hand" not in caplog.text, "the value is nobody's business any more"
    caplog.clear()
    quiet = main({"MOTOR_BACKEND": "warp"})
    assert quiet == EXIT_CONFIG
    assert "no longer read" not in caplog.text


# =========================================================================
# Nothing of it in the control tick
# =========================================================================


class Counted(LinkHealth):
    """A :class:`LinkHealth` that says which of its entries were called, and does the same."""

    calls: ClassVar[list[str]] = []
    proofs: ClassVar[list[bool]] = []
    """For each ``answered``, in order: whether it was taken as proof of the contract."""

    @override
    def answered(self, now: Monotonic, *, contract: bool) -> None:
        Counted.calls.append("answered")
        Counted.proofs.append(contract)
        super().answered(now, contract=contract)

    @override
    def key_refused(self, now: Monotonic, detail: str) -> None:
        Counted.calls.append("key_refused")
        super().key_refused(now, detail)

    @override
    def contract_refused(self, now: Monotonic, sentence: str) -> None:
        Counted.calls.append("contract_refused")
        super().contract_refused(now, sentence)

    @override
    def announced(self, refusal: str | None) -> None:
        Counted.calls.append("announced")
        super().announced(refusal)

    @override
    def silent(self, detail: str) -> None:
        Counted.calls.append("silent")
        super().silent(detail)

    @override
    def errored(self, detail: str) -> None:
        Counted.calls.append("errored")
        super().errored(detail)

    @override
    def status(self, now: Monotonic) -> LinkStatus:
        Counted.calls.append("status")
        return super().status(now)


FEEDS: Final[frozenset[str]] = frozenset(
    {"answered", "key_refused", "contract_refused", "silent", "errored"}
)
"""The entries of :class:`LinkHealth` an exchange is told through, one per exchange."""


def heard_one_for_one(dashboard: Dashboard) -> list[str]:
    """Check the indicator was told of every exchange ``dashboard`` saw, in order.

    One entry per request, and for a success whether it was taken as proof of
    the contract: every route but the stop question's. The paths asked, each
    once, in the order first met.
    """
    feeds = [name for name in Counted.calls if name in FEEDS]
    assert len(feeds) == len(dashboard.calls), "an exchange the indicator never heard of"
    assert set(feeds) == {"answered"}, "this dashboard answers everything"
    assert len(Counted.proofs) == len(dashboard.calls)
    for (_method, path, _body), proof in zip(dashboard.calls, Counted.proofs, strict=True):
        assert proof == (path != STATUS), path
    return list(dict.fromkeys(path for _method, path, _body in dashboard.calls))


async def test_the_indicator_hears_every_exchange_of_a_session_started_at_the_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The greeting (the declaration), the sending, the stop question, the end: all of them.

    The real console with its record and its two link tasks. Every request the
    dashboard received was told to the indicator once, and only the stop
    question's answers were not taken as proof of the contract.
    """
    monkeypatch.setattr(local_panel, "LinkHealth", Counted)
    Counted.calls.clear()
    Counted.proofs.clear()
    linked_console, dashboard, journal = recording_linked(tmp_path)
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    surface = linked_console.panel.surface
    started = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(started, Ok)
    for _ in range(12):
        await linked_console.run(1.0)
        journal.drain()
    ended = surface.submit_end(operator=OPERATOR, reason="fini")
    assert isinstance(ended, Ok)
    for _ in range(14):
        await linked_console.run(1.0)
        journal.drain()
    await linked_console.panel.close()

    asked = heard_one_for_one(dashboard)
    assert set(asked) >= {
        HEARTBEAT,
        "/api/machine/profiles",
        LOCAL,
        TELEMETRY,
        "/api/machine/training/events",
        STATUS,
        END,
        POLL_PATH,
    }
    assert len(dashboard.to(STATUS)) >= 3, "the stop watch asked all along"


async def test_the_indicator_hears_the_greeting_of_a_session_launched_from_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch: its start is confirmed by the stop watch's task, then its stop is asked."""
    monkeypatch.setattr(local_panel, "LinkHealth", Counted)
    Counted.calls.clear()
    Counted.proofs.clear()
    linked_console = linked(tmp_path)
    dashboard = linked_console.dashboard
    dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await linked_console.run(6.0)
    assert linked_console.panel.runtime.state is RuntimeState.RUNNING
    dashboard.answer(STATUS, status_answer(stop=True))
    await linked_console.run(4.0)
    assert linked_console.panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    await linked_console.panel.close()

    asked = heard_one_for_one(dashboard)
    assert set(asked) >= {POLL_PATH, "/api/machine/training/start", STATUS, HEARTBEAT}


async def test_the_control_tick_neither_feeds_nor_reads_the_indicator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EX-2: the state is written by the dashboard task and read by the page's route.

    Every entry of :class:`~src.link_state.LinkHealth` is counted while the
    console runs a session with its control and acquisition steps alone, then
    with its dashboard steps, then with the page polling.
    """
    monkeypatch.setattr(local_panel, "LinkHealth", Counted)
    calls = Counted.calls
    calls.clear()
    Counted.proofs.clear()
    s = scene(tmp_path)
    async with s.browser as browser:
        await attest(browser)
        started = await browser.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == 202, started.text
        calls.clear()
        # Fifty control ticks of a session in progress, sensors and record included.
        for _ in range(50):
            await s.console.tick(float(TICK))
            await s.console.panel.sensor_step()
            s.journal.drain()
        assert s.state() is RuntimeState.RUNNING
        assert calls == [], "nothing of the indicator ran in the control tick"

        await s.console.panel.cloud_step()
        fed = set(calls)
        assert "answered" in fed
        assert "status" not in fed, "the link writes; it never reads its own indicator"

        calls.clear()
        row = parse(PanelRow, await browser.get("/api/panel"))
        assert calls == ["status"], "one read, in the page's route"
        assert row.dashboard.state == "reachable"
    await s.console.panel.close()
