"""ANH-129, the acceptance test: a session, a lost link, a killed console, and nothing missing.

The scenario, on the REAL console in simulation (simulated drive, real runtime,
real session record on disk, real dashboard link) and in simulated time:

* a session meant to last ten minutes is started at the machine;
* **at 3 min** the network is lost. The last batch before the cut is received
  by the dashboard, but its answer never reaches the console;
* **at 5 min** the console is killed (SIGKILL): nothing is closed, nothing is
  said, and the write in progress is cut in the middle of a line;
* the console restarts, same system, still without a network;
* **at 8 min** the network returns, and with it the right time: the machine,
  which has no real-time clock, had been dating everything from 1970.

Then the dashboard must hold every 1 Hz point of the record and every event,
each exactly once, and the end of the session with its reason: ``interrupted``.
And, because the machine said how long ago the session started: the session
dated on the dashboard's own clock, never in 1970, and not one point refused
for the correction of the machine's clock.

The dashboard is :class:`~tests.fake_dashboard.FakeDashboard`, which applies
the real dashboard's rules in memory. Every request it received is also
written to ``contracts/fixtures/journal-sync-trace.json``, which
``convex/journalTrace.test.ts`` replays against the real Convex functions: the
acceptance test fails if that file is no longer what this console sends.

Ten simulated minutes; a few seconds of wall time on a workstation, about half
a minute on a loaded runner. A second, shorter scenario steps the machine's
clock while a session runs.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import pytest

from src.record.cursor import Cursor, cursor_of, load
from src.record.journal import Journal
from src.record.reader import Recording, read
from src.record.retention import records
from src.result import Ok
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import OutputRpm
from tests.fake_dashboard import (
    END,
    LOCAL,
    TELEMETRY,
    Exchange,
    FakeDashboard,
    ServerSession,
)
from tests.test_failure_rig import BENCH_ENV, OPERATOR, TICK, JumpClock, Rig, make_rig

CUT_S: Final[int] = 180
KILL_S: Final[int] = 300
BACK_S: Final[int] = 480

CLOCK_EPOCH_MS: Final[int] = 1_700_000_000_000
"""The wall clock of :class:`JumpClock` when its offset is zero."""

TRUE_EPOCH_MS: Final[int] = 1_790_000_000_000
"""The right time (September 2026): what the dashboard reads, and the machine once corrected."""

NEVER_SET_MS: Final[int] = 600_000
"""What the machine's clock says when it starts without a network: ten past midnight, 1970."""

BOOT: Final[str] = "5d1e7c3a-9b2f-4e8d-a6c1-0123456789ab"

LINKED_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

FIXTURE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "journal-sync-trace.json"
)
REWRITE: Final[str] = "ANHEART_WRITE_SYNC_TRACE"
"""Set to 1 to rewrite the fixture from this run instead of comparing it."""


@dataclass(frozen=True)
class Outcome:
    """What the scenario left behind."""

    dashboard: FakeDashboard
    record: Path
    recording: Recording
    owed_at_the_end: int
    paths_while_cut: tuple[str, ...]
    state_before_the_kill: RuntimeState
    wall_seconds: float

    @property
    def session(self) -> ServerSession:
        (only,) = self.dashboard.sessions.values()
        return only

    @property
    def true_start_ms(self) -> int:
        """When the session was armed, on the right clock: the dashboard's."""
        armed_at = float(self.recording.manifest.clocks.monotonic_start)
        return TRUE_EPOCH_MS + round(armed_at * 1000)


def console(
    root: Path, clock: JumpClock, dashboard: FakeDashboard, where: Path
) -> tuple[Rig, Journal]:
    """A console on ``root``, as one start of the program builds it. No thread: the test drains."""
    where.mkdir()
    journal = Journal(root, clock)
    rig, _ = make_rig(
        where, env=LINKED_ENV, clock=clock, transport=lambda _config: dashboard, journal=journal
    )
    return rig, journal


async def one_second(rig: Rig, journal: Journal) -> None:
    """One second of the console's life: five control ticks, the disk catching up, one link step."""
    for _ in range(5):
        await rig.tick(float(TICK))
        journal.drain()
        await rig.panel.cloud_stop_step()
    await rig.panel.cloud_step()


async def scenario(tmp_path: Path) -> Outcome:
    began = time.perf_counter()
    clock = JumpClock()
    # No real-time clock, no network yet: the machine believes it is 1970.
    clock.wall_offset_ms = NEVER_SET_MS - CLOCK_EPOCH_MS
    right = TRUE_EPOCH_MS - CLOCK_EPOCH_MS
    dashboard = FakeDashboard(now=lambda: int(clock.inner.unix_millis()) + right)
    root = tmp_path / "records"

    rig, journal = console(root, clock, dashboard, tmp_path / "first")
    surface = rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    for second in range(KILL_S):
        if second == 5:
            assert isinstance(
                surface.submit_manual_target(output_rpm=OutputRpm(8.0), operator=OPERATOR), Ok
            )
        if second in (60, 240):
            # Under the slowest running speed: the machine refuses, and records that it did.
            # Once while the dashboard listens, once after the network is lost.
            assert isinstance(
                surface.submit_manual_target(output_rpm=OutputRpm(0.5), operator=OPERATOR), Ok
            )
        if second == 120:
            assert isinstance(
                surface.submit_manual_target(output_rpm=OutputRpm(12.0), operator=OPERATOR), Ok
            )
        if second == CUT_S - 6:
            # The next batch of telemetry is the last exchange before the cut:
            # the dashboard receives it, and its answer never reaches the console.
            dashboard.cut_at = TELEMETRY
        await one_second(rig, journal)
    assert not dashboard.online, "the network was lost around the third minute"
    state = rig.panel.runtime.state
    cut_from = len(dashboard.paths)

    # SIGKILL. Nothing is closed; the journal thread was in the middle of a line.
    (record,) = records(root)
    with (record / "ticks.csv").open("ab") as handle:
        handle.write(b"300.2,active,manual,hold,OPERATION_ENA")
    del rig, journal, surface

    # The console starts again, on the same system, still without a network.
    rig, journal = console(root, clock, dashboard, tmp_path / "second")
    for _ in range(BACK_S - KILL_S):
        await one_second(rig, journal)
    while_cut = tuple(dashboard.paths[cut_from:])

    # The network returns, and the machine's clock is corrected by 56 years.
    clock.wall_offset_ms = right
    dashboard.online = True
    for _ in range(60):
        await one_second(rig, journal)
    cloud = rig.panel.cloud
    assert cloud is not None
    owed = cloud.owed
    await rig.panel.close()

    loaded = read(record)
    assert isinstance(loaded, Ok)
    return Outcome(
        dashboard=dashboard,
        record=record,
        recording=loaded.value,
        owed_at_the_end=owed,
        paths_while_cut=while_cut,
        state_before_the_kill=state,
        wall_seconds=time.perf_counter() - began,
    )


async def test_acceptance_a_session_cut_at_3_min_and_killed_at_5_is_whole_on_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario of the ticket, run once, and everything it must have left behind.

    One test on purpose: the gate deals the tests out to several processes,
    and a scenario shared by several tests would be run again in each of them.
    Each thing established is a function below, named for what it says.
    """
    # One start of the system for both runs of the console, wherever the tests run.
    monkeypatch.setattr("src.local_panel.boot_identity", lambda: BOOT)
    outcome = await scenario(tmp_path)

    the_scenario_is_the_one_of_the_ticket(outcome)
    every_1_hz_point_of_the_record_is_on_the_dashboard_once(outcome)
    every_event_of_the_record_is_on_the_dashboard_once(outcome)
    the_session_ends_on_the_dashboard_with_the_right_reason(outcome)
    the_dashboard_dates_the_session_on_its_own_clock_never_in_1970(outcome)
    the_correction_of_the_machine_s_clock_moved_no_point(outcome)
    ten_simulated_minutes_are_not_ten_real_ones(outcome)
    the_trace_the_real_dashboard_is_replayed_is_what_this_console_sends(outcome)


def seconds_of(recording: Recording) -> list[int]:
    """Every whole second of the session the record holds a tick of."""
    return sorted({round(row.t * 1000) // 1000 for row in recording.rows if row.t >= 0})


def stored_points(outcome: Outcome) -> list[Mapping[str, JsonValue]]:
    return [stored.item for _key, stored in sorted(outcome.dashboard.telemetry.items())]


def the_scenario_is_the_one_of_the_ticket(outcome: Outcome) -> None:
    """Five minutes recorded, the console killed while the session ran, the link cut for five."""
    assert outcome.state_before_the_kill is RuntimeState.RUNNING
    seconds = seconds_of(outcome.recording)
    assert seconds[0] == 0
    assert seconds[-1] in (KILL_S - 1, KILL_S)
    assert outcome.recording.manifest.ended_at is None, "nobody closed the record"
    assert {warning.code for warning in outcome.recording.warnings} >= {"truncated"}
    # While the link was cut nothing reached the dashboard at all.
    assert outcome.paths_while_cut == ()
    # And the machine was dated 1970 from the start to the return of the network.
    assert outcome.recording.manifest.started_at.startswith("1970-01-01T00:10:")


def every_1_hz_point_of_the_record_is_on_the_dashboard_once(
    outcome: Outcome,
) -> None:
    expected = seconds_of(outcome.recording)
    points = stored_points(outcome)

    assert [int(cast("float", point["elapsedS"])) for point in points] == expected
    assert len(points) == len(expected) >= KILL_S
    assert outcome.dashboard.rejected == 0
    # Sent twice, stored once: the batch whose answer was lost before the cut.
    assert outcome.dashboard.duplicates >= 1
    sent = sum(
        len(cast("list[object]", exchange.body["points"]))
        for exchange in outcome.dashboard.trace
        if exchange.path == TELEMETRY
    )
    assert sent == len(points) + outcome.dashboard.duplicates


def every_event_of_the_record_is_on_the_dashboard_once(outcome: Outcome) -> None:
    events = [stored.item for _key, stored in sorted(outcome.dashboard.events.items())]

    assert [event["seq"] for event in events] == list(range(len(outcome.recording.events)))
    assert [(event["kind"], event["detail"], event["actor"]) for event in events] == [
        (event.kind.value, event.detail, event.actor) for event in outcome.recording.events
    ]
    kinds = [event["kind"] for event in events]
    assert kinds[:2] == ["operator_action", "phase"]
    assert kinds.count("refusal") == 2, "one refusal before the cut, one after it"


def the_session_ends_on_the_dashboard_with_the_right_reason(
    outcome: Outcome,
) -> None:
    session = outcome.session

    assert (session.status, session.end_reason) == ("failed", "interrupted")
    assert outcome.owed_at_the_end == 0
    cursor = load(outcome.record)
    assert isinstance(cursor, Cursor)
    assert (cursor.end, cursor.state) == ("sent", "complete")
    assert (cursor.rejected_points, cursor.rejected_events, cursor.refused) == (0, 0, 0)
    assert stat.S_IMODE(cursor_of(outcome.record).stat().st_mode) == 0o600
    # One declaration, one end: the restart declared nothing again.
    paths = [exchange.path for exchange in outcome.dashboard.trace]
    assert paths.count(LOCAL) == 1
    assert paths.count(END) == 1
    assert paths[0] == LOCAL, "declared before anything is sent"
    assert paths[-1] == END or paths[-2] == END


def the_dashboard_dates_the_session_on_its_own_clock_never_in_1970(outcome: Outcome) -> None:
    session = outcome.session
    true_start = outcome.true_start_ms
    last_tick = max(round(row.t * 1000) for row in outcome.recording.rows)

    # What the machine wrote is kept, as the origin of its own axis...
    assert session.machine_started_at is not None
    assert session.machine_started_at < NEVER_SET_MS + 60_000
    # ...and the session is dated by the dashboard, from the age the machine said:
    # to the millisecond the test clock truncates.
    assert abs(session.started_at - true_start) <= 1
    assert session.ended_at == session.started_at + last_tick
    # Every point, placed on the dashboard's clock, is inside the session it shows.
    served = [cast("int", point["t"]) + session.shift for point in stored_points(outcome)]
    assert served[0] == session.started_at
    assert all(session.started_at <= t <= session.started_at + last_tick for t in served)
    assert served[0] > TRUE_EPOCH_MS, "not in 1970"


def the_correction_of_the_machine_s_clock_moved_no_point(outcome: Outcome) -> None:
    """Start plus elapsed, on the monotonic clock: the wall clock is read once, at the start."""
    session = outcome.session
    assert session.machine_started_at is not None
    for point in stored_points(outcome):
        elapsed_ms = round(cast("float", point["elapsedS"]) * 1000)
        assert abs(cast("int", point["t"]) - session.machine_started_at - elapsed_ms) <= 5
    # The points sent after the correction are received then, and measured before.
    late = [
        stored
        for stored in outcome.dashboard.telemetry.values()
        if stored.received_at >= outcome.true_start_ms + (BACK_S - 1) * 1000
    ]
    assert len(late) >= KILL_S - CUT_S
    assert all(
        stored.received_at - (cast("int", stored.item["t"]) + session.shift)
        >= (BACK_S - KILL_S) * 1000
        for stored in late
    )


async def test_a_clock_corrected_while_the_session_runs_moves_no_point_and_loses_none(
    tmp_path: Path,
) -> None:
    """Forward by an hour at 30 s, back by ten minutes at 60 s: the session runs and sends on.

    The machine dates a point as the start it read once plus the time elapsed
    on its monotonic clock. Neither step moves a point, the dashboard refuses
    none as dated outside its session, and the session ends where it ended,
    not an hour later nor ten minutes earlier.
    """
    clock = JumpClock()
    right = TRUE_EPOCH_MS - CLOCK_EPOCH_MS
    clock.wall_offset_ms = right
    dashboard = FakeDashboard(now=lambda: int(clock.inner.unix_millis()) + right)
    root = tmp_path / "records"
    rig, journal = console(root, clock, dashboard, tmp_path / "console")
    surface = rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    for second in range(90):
        if second == 30:
            clock.wall_offset_ms = right + 3_600_000
        if second == 60:
            clock.wall_offset_ms = right - 600_000
        await one_second(rig, journal)
    assert isinstance(surface.submit_end(operator=OPERATOR, reason="fini"), Ok)
    for _ in range(40):
        await one_second(rig, journal)
    await rig.panel.close()

    (record,) = records(root)
    loaded = read(record)
    assert isinstance(loaded, Ok)
    (session,) = dashboard.sessions.values()
    points = [stored.item for _key, stored in sorted(dashboard.telemetry.items())]
    # Every second of the record, each once, none refused.
    assert [int(cast("float", point["elapsedS"])) for point in points] == seconds_of(loaded.value)
    assert len(points) >= 90
    assert (dashboard.rejected, dashboard.duplicates) == (0, 0)
    # Each where the monotonic clock puts it, before and after both steps.
    assert session.machine_started_at is not None
    for point in points:
        elapsed_ms = round(cast("float", point["elapsedS"]) * 1000)
        assert abs(cast("int", point["t"]) - session.machine_started_at - elapsed_ms) <= 5
    # And the end too: a second after its last tick at most.
    last_tick = max(round(row.t * 1000) for row in loaded.value.rows)
    assert (session.status, session.end_reason) == ("completed", "operator_stop: fini")
    assert session.ended_at is not None
    assert abs(session.ended_at - (session.started_at + last_tick)) <= 1000


def ten_simulated_minutes_are_not_ten_real_ones(outcome: Outcome) -> None:
    """Simulated time: seconds on a workstation, about half a minute on a loaded runner."""
    assert outcome.wall_seconds < 300.0


# =========================================================================
# What the real dashboard is replayed
# =========================================================================


def trace_of(outcome: Outcome) -> dict[str, object]:
    """The requests of the session's delivery, as the Convex test replays them.

    The one thing that differs from a run to the next, the reference the
    record draws at random, is replaced by a fixed word.
    """
    session = outcome.session

    def normalised(exchange: Exchange) -> dict[str, object]:
        body = dict(exchange.body)
        if "localRef" in body:
            body["localRef"] = "LOCAL-REF"
        return {
            "at": exchange.at,
            "path": exchange.path,
            "body": body,
            "status": exchange.status,
            "answer": dict(exchange.answer),
        }

    return {
        "description": (
            "Ce que la console du Pi envoie pendant le test d'acceptation d'ANH-129 "
            "(raspberry-pi/tests/test_cloud_journal_e2e.py) : seance, coupure reseau a "
            "3 min, SIGKILL a 5 min, redemarrage, retour du reseau a 8 min. Fichier "
            "genere : ANHEART_WRITE_SYNC_TRACE=1 pytest tests/test_cloud_journal_e2e.py. "
            "Rejoue contre les vraies fonctions Convex par convex/journalTrace.test.ts."
        ),
        "scenario": {"cut_s": CUT_S, "kill_s": KILL_S, "back_s": BACK_S},
        "expected": {
            "points": len(outcome.dashboard.telemetry),
            "events": len(outcome.dashboard.events),
            "duplicates": outcome.dashboard.duplicates,
            "end": {"status": session.status, "reason": session.end_reason},
            "machine_started_at": session.machine_started_at,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
        },
        "exchanges": [normalised(exchange) for exchange in outcome.dashboard.trace],
    }


def serialised(trace: Mapping[str, object]) -> str:
    """JSON, one exchange per line: a change of the trace reads in a diff."""
    exchanges = cast("list[object]", trace["exchanges"])
    head = {key: value for key, value in trace.items() if key != "exchanges"}
    lines = [json.dumps(exchange, separators=(",", ":"), sort_keys=True) for exchange in exchanges]
    opening = json.dumps(head, indent=2, sort_keys=True, ensure_ascii=False)[:-2]
    return opening + ',\n  "exchanges": [\n    ' + ",\n    ".join(lines) + "\n  ]\n}\n"


def the_trace_the_real_dashboard_is_replayed_is_what_this_console_sends(
    outcome: Outcome,
) -> None:
    """``convex/journalTrace.test.ts`` proves the real functions on this very exchange."""
    produced = serialised(trace_of(outcome))
    assert json.loads(produced)["exchanges"], "a trace that cannot be read back proves nothing"
    if os.environ.get(REWRITE) == "1":
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(produced, encoding="utf-8")
    committed = FIXTURE.read_text(encoding="utf-8")
    assert produced == committed, (
        "contracts/fixtures/journal-sync-trace.json is no longer what the console sends. "
        f"Regenerate it ({REWRITE}=1 pytest tests/test_cloud_journal_e2e.py) and run "
        "the Convex tests on it (npm run test:convex)."
    )
