"""ANH-166 EX-3: the console keeps to the waits the dashboard asks for, and never needs to.

* a 429 carries its wait (``retryAfterMs``), read bounded;
* :class:`~src.cloud_sync.PacedTransport` holds the route for that wait:
  nothing reaches the dashboard on it before the wait is over, whoever asks
  and however often; the hold is logged; the stop question is never held;
* the console's own composition puts every route through it;
* the console's cadences and batch sizes are under the dashboard's limits
  (``contracts/machine-api.json``, ``rate_limits`` and ``max_body_bytes``):
  a console that works as designed is never refused for its rate or its size.

No test here reaches a network: every transport is a fake or a mock.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast, override

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import (
    HEARTBEAT_PERIOD,
    HELD_HERE,
    MAX_RATE_WAIT,
    POLL_PERIOD,
    RATE_WAIT_UNSAID,
    RETRY_PERIOD,
    STATUS_PATH,
    CloudError,
    CloudSync,
    CloudTransport,
    Document,
    PacedTransport,
    Refused,
    Unreachable,
    answer_of,
    retry_after_of,
)
from src.contract import (
    CONTRACT_VERSION,
    PAYLOAD_TOO_LARGE,
    RATE_LIMITED,
    ErrorCode,
    SoftwareVersion,
)
from src.health import HealthContext, HealthWatch
from src.link_state import MAX_DETAIL, UNREACHABLE_AFTER, LinkState
from src.local_panel import CLOUD_PERIOD, build_panel
from src.record.deposit import FILES_PER_STEP
from src.record.upload import MAX_EVENTS, MAX_POINTS, read_events
from src.record_uplink import (
    CATCH_UP_PERIOD,
    LOCAL_PATH,
    START_PATH,
    TELEMETRY_PERIOD,
    Answer,
    Held,
    Refusal,
)
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.types import TelemetrySnapshot
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_uplink_support import armed, bench, recording
from tests.test_cloud_sync import Dashboard, StubRuntime, config_of, ok, transport_answering
from tests.test_contract import shared

HEARTBEAT: Final[str] = "/api/machine/heartbeat"
POLL: Final[str] = "/api/machine/training/poll"
TELEMETRY: Final[str] = "/api/machine/training/telemetry"
EVENTS: Final[str] = "/api/machine/training/events"
UPLOAD_URL: Final[str] = "/api/machine/records/upload-url"
COMMIT: Final[str] = "/api/machine/records/commit"
PROFILES: Final[str] = "/api/machine/profiles"


def limited(wait: Seconds | None) -> Result[Document, CloudError]:
    """The dashboard's 429, as the transport reads it."""
    return Err(Refused(429, "Too many requests", code=RATE_LIMITED, retry_after=wait))


def clock() -> ManualClock:
    return ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))


def sync_over(tmp_path: Path, time: ManualClock, transport: CloudTransport) -> CloudSync:
    panel = build_panel(
        config_of(), clock=time, profiles_path=tmp_path / "p.json", transport=lambda _c: transport
    )
    return CloudSync(
        clock=time,
        transport=transport,
        runtime=StubRuntime(source=panel.runtime.snapshot),
        surface=panel.surface,
        store=panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
    )


# =========================================================================
# The wait a 429 carries
# =========================================================================


async def test_a_429_is_read_with_its_code_and_its_wait() -> None:
    transport = transport_answering(
        lambda _request: httpx.Response(
            429,
            json={"error": "rate_limited", "message": "slow down", "retryAfterMs": 4200},
            headers={"Retry-After": "5"},
        )
    )

    got = await transport.post(HEARTBEAT, {})

    assert got == Err(Refused(429, "slow down", code=RATE_LIMITED))
    assert isinstance(got, Err)
    assert isinstance(got.error, Refused)
    assert got.error.retry_after == pytest.approx(4.2)
    await transport.close()


@pytest.mark.parametrize(
    ("sent", "wait"),
    [
        (4200, 4.2),
        (0, 0.0),
        (2.5, 0.0025),
        (299_999, 299.999),
        (10**12, float(MAX_RATE_WAIT)),
        (-1, None),
        (True, None),
        ("4200", None),
        (None, None),
        (math.nan, None),
        (math.inf, None),
        ([4200], None),
    ],
)
def test_the_wait_is_a_number_of_ms_bounded_by_five_minutes(
    sent: object, wait: float | None
) -> None:
    read = retry_after_of(sent)

    if wait is None:
        assert read is None
    else:
        assert read == pytest.approx(wait)


def test_two_429s_are_the_same_refusal_whatever_their_waits() -> None:
    """The log says a refusal once while it lasts: a wait that changes is not a new one."""
    first = Refused(429, "slow down", code=RATE_LIMITED, retry_after=Seconds(1.0))
    second = Refused(429, "slow down", code=RATE_LIMITED, retry_after=Seconds(2.0))

    assert first == second


def test_a_429_is_held_and_a_413_is_final() -> None:
    """Rate: nothing is dropped, the same request is made later. Size: it never fits."""
    held = answer_of(limited(Seconds(1.0)))
    too_large = answer_of(Err(Refused(413, "too large", code=PAYLOAD_TOO_LARGE)))

    assert isinstance(held, Held)
    assert held.refused
    assert isinstance(too_large, Refusal)
    assert too_large.final


# =========================================================================
# The paced transport
# =========================================================================


async def test_a_route_is_not_asked_again_before_the_wait_is_over(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(HEARTBEAT, limited(Seconds(4.2)))
    paced = PacedTransport(dashboard, time)

    first = await paced.post(HEARTBEAT, {})
    time.advance(Seconds(4.1))
    meanwhile = await paced.post(HEARTBEAT, {})
    other = await paced.post(TELEMETRY, {"sessionId": "s", "points": []})
    assert other == ok()

    assert first == limited(Seconds(4.2))
    assert isinstance(meanwhile, Err)
    assert meanwhile.error == Refused(429, HELD_HERE, code=RATE_LIMITED)
    assert isinstance(meanwhile.error, Refused)
    assert meanwhile.error.retry_after == pytest.approx(0.1)
    assert len(dashboard.to(HEARTBEAT)) == 1, "held here, not sent"
    assert len(dashboard.to(TELEMETRY)) == 1, "another route is not held"
    assert caplog.text.count("refused for its rate") == 1
    assert f"{HEARTBEAT} refused for its rate (HTTP 429, rate_limited)" in caplog.text
    assert "not asked again for 4.2 s" in caplog.text

    time.advance(Seconds(0.1))
    del dashboard.handlers[HEARTBEAT]
    assert await paced.post(HEARTBEAT, {}) == ok({"server_contract_version": CONTRACT_VERSION})
    assert len(dashboard.to(HEARTBEAT)) == 2


async def test_a_get_is_held_like_a_post() -> None:
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(POLL, limited(Seconds(2.0)))
    paced = PacedTransport(dashboard, time)

    await paced.get(POLL)
    time.advance(Seconds(1.0))
    held = await paced.get(POLL)
    time.advance(Seconds(1.0))
    await paced.get(POLL)

    assert isinstance(held, Err)
    assert isinstance(held.error, Refused)
    assert held.error.code == RATE_LIMITED
    assert len(dashboard.to(POLL)) == 2


@pytest.mark.parametrize(
    ("wait", "held_for"),
    [
        (None, float(RATE_WAIT_UNSAID)),
        (MAX_RATE_WAIT, float(MAX_RATE_WAIT)),
        (Seconds(0.0), 0.0),
    ],
)
async def test_a_wait_unsaid_is_the_link_s_own_and_none_is_longer_than_five_minutes(
    wait: Seconds | None, held_for: float
) -> None:
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(EVENTS, limited(wait))
    paced = PacedTransport(dashboard, time)

    await paced.post(EVENTS, {})
    if held_for > 0:
        time.advance(Seconds(held_for - 0.001))
        await paced.post(EVENTS, {})
    sent_while_held = len(dashboard.to(EVENTS))
    time.advance(Seconds(0.001))
    await paced.post(EVENTS, {})

    assert sent_while_held == 1
    assert len(dashboard.to(EVENTS)) == 2


async def test_the_stop_question_is_never_held() -> None:
    """A stop is never the side that waits, whatever the dashboard answered."""
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(STATUS_PATH, limited(Seconds(60.0)))
    paced = PacedTransport(dashboard, time)

    for _ in range(3):
        await paced.get(STATUS_PATH, {"sessionId": "s"})

    assert len(dashboard.to(STATUS_PATH)) == 3


@pytest.mark.parametrize(
    "answer",
    [
        ok(),
        Err(Unreachable("no route to host")),
        Err(Refused(503, "busy")),
        Err(Refused(400, "no", code=ErrorCode("invalid_request"))),
    ],
)
async def test_nothing_else_holds_a_route(answer: Result[Document, CloudError]) -> None:
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(TELEMETRY, answer)
    paced = PacedTransport(dashboard, time)

    first = await paced.post(TELEMETRY, {})
    await paced.post(TELEMETRY, {})

    assert first == answer
    assert len(dashboard.to(TELEMETRY)) == 2


# =========================================================================
# The console: never a tight loop
# =========================================================================


async def test_the_console_keeps_to_the_wait_on_the_routes_it_calls_on_a_cadence(
    tmp_path: Path,
) -> None:
    """Built as the console builds it: two minutes of steps against a dashboard that says wait.

    Without the hold the heartbeat would go every 10 s and the poll every 3 s;
    held, each goes once per wait it was given.
    """
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(HEARTBEAT, limited(Seconds(60.0)))
    dashboard.answer(POLL, limited(Seconds(30.0)))
    panel = build_panel(
        config_of(), clock=time, profiles_path=tmp_path / "p.json", transport=lambda _c: dashboard
    )

    for _ in range(120):
        await panel.cloud_step()
        time.advance(CLOUD_PERIOD)

    assert len(dashboard.to(HEARTBEAT)) == 2  # at 0 s and 60 s
    assert len(dashboard.to(POLL)) == 4  # at 0, 30, 60 and 90 s


@pytest.mark.parametrize("path", [TELEMETRY, START_PATH])
async def test_a_local_hold_is_not_repeated_for_its_changing_wait(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(path, limited(Seconds(60.0)))
    sync = sync_over(tmp_path, time, PacedTransport(dashboard, time))

    for hold in range(2):
        caplog.clear()
        await sync.send(path, {})
        server_line = "dashboard refused a request: rate_limited (HTTP 429)"
        if path != START_PATH:
            server_line += ": 'Too many requests'"
        assert [r.getMessage() for r in caplog.records if "refused a request" in r.message] == [
            server_line
        ]
        assert caplog.text.count("refused for its rate") == 1
        caplog.clear()
        for _ in range(3):
            time.advance(Seconds(1.0))
            await sync.send(path, {})

        assert len(dashboard.to(path)) == hold + 1
        assert "dashboard refused a request" not in caplog.text
        assert caplog.text.count("dashboard: a request held here, not sent (rate_limited)") == 1
        assert HELD_HERE not in caplog.text
        assert "Too many requests" not in caplog.text
        time.advance(Seconds(57.0))


@pytest.mark.parametrize("initially_reachable", [False, True])
async def test_a_local_hold_changes_the_reason_but_keeps_the_server_429_states(
    tmp_path: Path, initially_reachable: bool
) -> None:
    time = clock()
    server = Dashboard()
    paced_server = Dashboard()
    direct = sync_over(tmp_path, time, server)
    paced = sync_over(tmp_path, time, PacedTransport(paced_server, time))
    assert (direct.link.state, paced.link.state) == (LinkState.WAITING, LinkState.WAITING)
    if initially_reachable:
        await direct.send(TELEMETRY, {})
        await paced.send(TELEMETRY, {})
    before = direct.link.state
    assert paced.link.state is before
    server.answer(TELEMETRY, limited(Seconds(60.0)))
    paced_server.answer(TELEMETRY, limited(Seconds(60.0)))
    born = time.monotonic()

    for quiet in (1.0, 2.0, 24.0, 25.0, 60.0):
        time.advance(Seconds(quiet - (time.monotonic() - born)))
        await direct.send(TELEMETRY, {})
        await paced.send(TELEMETRY, {})
        expected = before if quiet < UNREACHABLE_AFTER else LinkState.SERVER_ERROR
        assert direct.link.state is paced.link.state is expected
        if quiet >= UNREACHABLE_AFTER:
            assert direct.link.detail == "HTTP 429 (rate_limited)"
            assert paced.link.detail == "requete retenue ici, non envoyee (rate_limited)"
            assert len(paced.link.detail) <= MAX_DETAIL
        assert direct.link.last_answer_age == paced.link.last_answer_age

    assert len(server.to(TELEMETRY)) == 5 + int(initially_reachable)
    assert len(paced_server.to(TELEMETRY)) == 1 + int(initially_reachable)
    time.advance(Seconds(1.0))
    server.handlers.clear()
    paced_server.handlers.clear()
    await direct.send(TELEMETRY, {})
    await paced.send(TELEMETRY, {})
    assert direct.link == paced.link
    assert paced.link.state is LinkState.REACHABLE


@pytest.mark.parametrize(
    "refused",
    [
        Refused(503, HELD_HERE, code=RATE_LIMITED),
        Refused(429, HELD_HERE, code=ErrorCode("request_failed")),
        Refused(429, HELD_HERE),
        Refused(429, HELD_HERE + "!", code=RATE_LIMITED),
    ],
)
async def test_a_server_refusal_resembling_a_local_hold_keeps_its_log_and_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, refused: Refused
) -> None:
    caplog.set_level(logging.WARNING, logger="src.cloud_sync")
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(TELEMETRY, Err(refused))
    sync = sync_over(tmp_path, time, dashboard)
    time.advance(UNREACHABLE_AFTER)

    await sync.send(TELEMETRY, {})

    assert sync.link.state is LinkState.SERVER_ERROR
    words = f"HTTP {refused.status}"
    if refused.code is not None:
        words += f" ({refused.code})"
    assert sync.link.detail == words
    assert "dashboard refused a request" in caplog.text
    assert "a request held here" not in caplog.text


# =========================================================================
# What the console sends is under the dashboard's limits
# =========================================================================


def limits() -> Mapping[str, Mapping[str, float]]:
    routes = cast("Mapping[str, object]", shared()["rate_limits"])["routes"]
    return cast("Mapping[str, Mapping[str, float]]", routes)


def sizes() -> Mapping[str, int]:
    routes = cast("Mapping[str, object]", shared()["max_body_bytes"])["routes"]
    return cast("Mapping[str, int]", routes)


def least_gap(route: str) -> float:
    """The least time between two requests a route takes in a row, in seconds."""
    limit = limits()[route]
    return limit["period_ms"] / limit["rate"] / 1000


def test_every_cadence_of_the_console_is_under_its_route_s_limit() -> None:
    assert float(HEARTBEAT_PERIOD) >= least_gap(f"POST {HEARTBEAT}")
    assert float(POLL_PERIOD) >= least_gap(f"GET {POLL}")
    # A running session up to date sends every TELEMETRY_PERIOD, a session
    # behind every CATCH_UP_PERIOD, and never two batches closer than that.
    assert float(CATCH_UP_PERIOD) >= least_gap(f"POST {TELEMETRY}")
    assert float(TELEMETRY_PERIOD) >= float(CATCH_UP_PERIOD)
    # Events go once at most in a step of the link.
    assert float(CLOUD_PERIOD) >= least_gap(f"POST {EVENTS}")
    # One step of the deposit asks for its files' reservations together.
    assert limits()[f"POST {UPLOAD_URL}"]["capacity"] >= FILES_PER_STEP
    assert limits()[f"POST {COMMIT}"]["capacity"] >= FILES_PER_STEP
    assert least_gap(f"POST {COMMIT}") <= least_gap(f"POST {UPLOAD_URL}")
    assert float(RETRY_PERIOD) >= least_gap(f"POST {PROFILES}")


@dataclass(frozen=True, slots=True)
class _PacedSender:
    transport: PacedTransport

    async def send(self, path: str, body: Mapping[str, JsonValue]) -> Answer:
        return answer_of(await self.transport.post(path, body))


async def test_a_429_keeps_a_local_session_due_and_redeclares_the_same_reference_after_waiting(
    tmp_path: Path,
) -> None:
    time = clock()
    dashboard = Dashboard()
    dashboard.answer(LOCAL_PATH, limited(Seconds(60.0)))
    b = bench(tmp_path, clock=time, sender=_PacedSender(PacedTransport(dashboard, time)))
    b.uplink.begin(armed(time))
    record = recording(tmp_path, time, "synthetic-local-429")
    b.disk.current = record.path

    await b.uplink.greet()
    assert b.uplink.running
    assert b.following() is None
    assert record.path.exists()
    time.advance(Seconds(59.0))
    await b.uplink.greet()
    assert len(dashboard.to(LOCAL_PATH)) == 1

    dashboard.answer(LOCAL_PATH, ok({"sessionId": "synthetic-cloud-session"}))
    time.advance(Seconds(15.0))
    await b.uplink.greet()

    assert [body["localRef"] for body in dashboard.to(LOCAL_PATH)] == [
        "synthetic-local-429",
        "synthetic-local-429",
    ]
    assert b.following() == "synthetic-cloud-session"
    assert b.uplink.running
    assert record.path.exists()


def test_every_batch_of_the_console_is_under_its_route_s_count() -> None:
    assert MAX_POINTS <= 600
    assert MAX_EVENTS <= 200


def wire_bytes(body: Mapping[str, JsonValue]) -> int:
    """What :class:`~src.cloud_sync.HttpxTransport` sends for ``body``, in bytes."""
    return len(json.dumps(body).encode())


def test_the_largest_batch_of_events_the_console_reads_fits_the_route(tmp_path: Path) -> None:
    """200 events of 2000 characters none of which is ASCII: each written on 6 bytes.

    The console reads a record a bounded chunk at a time, so its batches of
    long events are shorter still; this reads past that bound.
    """
    record = tmp_path / "record"
    record.mkdir()
    line = {"t": 9999.999, "kind": "k" * 64, "detail": "é" * 2500, "actor": "a" * 64}
    (record / "events.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for _ in range(MAX_EVENTS)), encoding="utf-8"
    )

    # Read whole, past the console's own chunk: what it sends is never larger.
    read = read_events(
        record, start_ms=1_700_000_000_000, offset=0, last_seq=999_000, chunk=1 << 30
    )

    assert isinstance(read, Ok)
    events = read.value.events
    assert len(events) == MAX_EVENTS
    body: Mapping[str, JsonValue] = {"sessionId": "s" * 32, "events": list(events)}
    assert wire_bytes(body) <= sizes()[f"POST {EVENTS}"]


def test_the_largest_batch_of_points_fits_the_route() -> None:
    """300 points with every number at its longest and the longest words of the record."""
    longest = -12345.6789012345
    point: Mapping[str, JsonValue] = {
        "t": 1_700_000_000_000_000,
        "elapsedS": longest,
        "phase": "p" * 32,
        "bpm": 250,
        "motorRpm": -99999,
        "outputRpm": longest,
        "setpointMotorRpm": -99999,
        "gLoad": longest,
        "safetyAction": "s" * 32,
    }
    body: Mapping[str, JsonValue] = {"sessionId": "s" * 32, "points": [point] * MAX_POINTS}

    assert wire_bytes(body) <= sizes()[f"POST {TELEMETRY}"]


class _LargestHealth(HealthWatch):
    """The largest health a console reports: every field set, 128 fault codes of 32 characters."""

    @override
    async def sample(
        self,
        snapshot: TelemetrySnapshot,
        context: HealthContext,
        rtt: Seconds | None,
        incidents_open_high: int | None,
    ) -> Mapping[str, JsonValue]:
        big = 9_007_199_254_740_991
        reading = -12345.6789012345
        faults: dict[str, JsonValue] = {f"{index:032d}": big for index in range(128)}
        return {
            "cpu_temp_c": reading,
            "cpu_load_1m": reading,
            "mem_used_pct": reading,
            "disk_free_mb": reading,
            "uptime_s": reading,
            "clock_offset_s": reading,
            "link_rtt_ms": reading,
            "bitalino_battery_pct": reading,
            "bitalino_link": "down",
            "drive_link": "down",
            "drive_thermal_state": reading,
            "records_pending": big,
            "incidents_open_high": big,
            "record_degraded": False,
            "restart_loop": False,
            "preflight_last": "block",
            "counters": {
                "rotation_hours": reading,
                "sessions_total": big,
                "sessions_failed": big,
                "drive_faults_by_code": faults,
            },
        }


async def test_the_largest_heartbeat_the_console_sends_fits_the_route(tmp_path: Path) -> None:
    time = clock()
    dashboard = Dashboard()
    panel = build_panel(
        config_of(), clock=time, profiles_path=tmp_path / "p.json", transport=lambda _c: dashboard
    )
    sync = CloudSync(
        clock=time,
        transport=dashboard,
        runtime=StubRuntime(source=panel.runtime.snapshot),
        surface=panel.surface,
        store=panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        software_version=SoftwareVersion("p" * 64),
        record_degraded=lambda: True,
        health=_LargestHealth(None),
    )

    await sync.step()

    sent = dashboard.to(HEARTBEAT)
    assert len(sent) == 1
    body = cast("Mapping[str, JsonValue]", sent[0])
    assert "drive_faults_by_code" in str(body["health"])
    # With a session running, its identifier twice more: in the body and in `live`.
    session_ids = 2 * len(', "activeSessionId": ""') + 2 * 64
    assert wire_bytes(body) + session_ids <= sizes()[f"POST {HEARTBEAT}"]
