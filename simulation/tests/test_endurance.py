"""ANH-164: the endurance scenario itself, on runs short enough for every gate.

The 24 simulated hours are the nightly job's (``python -m simulation.endurance
simulated``). Here the same code runs a few simulated minutes: a manual
session, a programmed session the simulated rider completes, a second manual
session the end of the run cuts short, with every measure taken on the way.
Then the same console for a few real seconds, on the real clock and a real
loopback port. And each thing the scenario must notice rather than survive: a
session that does not finish, a start that is not taken, a dashboard that
stops answering.

One test per run, on purpose: the gate deals the tests out to several
processes, and a run shared by several tests would be made again in each of
them. Each thing established about a run is a function named for what it says.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from tests.fake_record_dashboard import RecordingDashboard

from simulation.endurance import (
    CLOSING_LIMIT,
    ENV,
    EPOCH_MS,
    FINISH_LIMIT,
    IDLE_PORT,
    NOTE_GATED_DSP,
    AnomalyError,
    Console,
    GatedDsp,
    Operator,
    Plan,
    RealWorld,
    Screen,
    build_console,
    main,
    simulated,
)
from simulation.endurance_measures import CSV_HEADER, Kind, Mode, Run, Status, judge
from simulation.tests.conftest import document, num, obj, seq
from src.clock import ManualClock, RealClock
from src.record.session import PRESTART_SECONDS
from src.units import Monotonic, Seconds, UnixMillis

SHORT_DAY = Plan(manual=30, rest=25, sample_every=15, programme=320.0)
"""A manual session of half a minute and the shortest programme the profile allows."""

SHORT_DAY_S = 440
"""Long enough for the programme to end by itself and a second manual session to start."""


def statuses(run: Run) -> dict[str, Status]:
    return {check.name: check.status for check in judge(run)}


# =========================================================================
# EX-1: the simulated day
# =========================================================================


async def test_ex1_a_short_simulated_day_chains_manual_and_programmed_sessions_and_measures_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = await simulated(SHORT_DAY_S, tmp_path / "out", SHORT_DAY)

    # One line a reading as the day goes: a run cut short still leaves what it measured.
    said = [line for line in capsys.readouterr().err.splitlines() if " résidents, " in line]
    assert len(said) == len(run.samples)
    assert said[0].startswith("0 h 00 : ")
    assert "descripteurs" in said[-1]
    the_day_covers_what_was_asked_and_the_last_session_is_let_to_finish(run)
    a_manual_session_a_programme_and_a_second_manual_session_ended_as_planned(run)
    every_session_reached_the_simulated_dashboard_whole(run)
    every_tick_of_the_day_was_timed(run)
    every_reading_holds_the_four_measures(run)
    the_data_folder_is_bounded_by_confirmed_deposits_and_rotation(run)
    the_console_s_data_is_removed_once_measured(tmp_path / "out")


def the_day_covers_what_was_asked_and_the_last_session_is_let_to_finish(run: Run) -> None:
    assert run.mode is Mode.SIMULATED
    assert run.anomaly is None
    assert run.requested == SHORT_DAY_S
    # The session running when the time is up is stopped, comes down and is delivered.
    assert SHORT_DAY_S < run.covered < SHORT_DAY_S + 180
    # Simulated time: the day costs seconds, not minutes.
    assert run.cpu > 0.0
    assert NOTE_GATED_DSP in run.notes


def a_manual_session_a_programme_and_a_second_manual_session_ended_as_planned(run: Run) -> None:
    kinds = [session.kind for session in run.sessions]
    ends = [session.end for session in run.sessions]

    assert kinds == [Kind.MANUAL, Kind.PROGRAMME, Kind.MANUAL]
    # The programme ran to its own end: the heart rate came from the real DSP.
    assert ends == ["operator_stop", "programme_complete", "operator_stop"]
    assert all(session.as_planned for session in run.sessions)
    assert [session.index for session in run.sessions] == [1, 2, 3]
    assert 320 <= run.sessions[1].recorded <= 330
    assert statuses(run)["sessions"] is Status.PASS


def every_session_reached_the_simulated_dashboard_whole(run: Run) -> None:
    sync = run.sync
    assert sync is not None
    assert all(session.delivered for session in run.sessions)
    for session in run.sessions:
        # One point a second of record, give or take the seconds the scenario counts whole.
        assert session.recorded - 5 <= session.points <= session.recorded + 5
    assert sync.points == sum(session.points for session in run.sessions)
    assert (sync.rejected, sync.owed) == (0, 0)
    # One heartbeat every ten seconds, session or not.
    assert sync.heartbeats == sync.health_reports
    assert sync.max_heartbeat_gap_ms <= 11_000
    assert sync.heartbeats >= run.covered // 11 - 1


def every_tick_of_the_day_was_timed(run: Run) -> None:
    tick = run.tick
    assert tick is not None
    assert tick.count == round(run.covered) * 5
    assert 0.0 < tick.p50 <= tick.p99 <= tick.highest
    assert statuses(run)["tick"] is Status.PASS


def every_reading_holds_the_four_measures(run: Run) -> None:
    samples = run.samples
    assert [sample.at for sample in samples] == [15.0 * (n + 1) for n in range(len(samples))]
    assert len(samples) == round(run.covered) // 15
    for sample in samples:
        assert sample.rss > 0
        assert sample.descriptors >= 3
        assert sample.blocks is not None
        assert sample.tick is not None
        assert sample.tick.count == 75
    entries = [sample.data_entries for sample in samples]
    assert max(entries) > min(entries)
    assert {"rss", "descriptors"} <= set(statuses(run))


def the_data_folder_is_bounded_by_confirmed_deposits_and_rotation(run: Run) -> None:
    data = run.data
    assert data is not None
    assert data.closed + data.purged == 3
    assert data.deposited == data.closed
    assert data.purged == 0
    assert data.stray == ()
    assert data.configuration.entries == 2
    assert data.configuration.size > 0
    for session in run.sessions:
        # Five raw blocks a second, and the handful of files every record has.
        assert (
            5 * (session.recorded - 3)
            <= session.record.entries
            <= 5 * (session.recorded + PRESTART_SECONDS + 5) + 8
        )
        assert session.record.allocated >= session.record.size > 0
    verdicts = statuses(run)
    assert verdicts["data"] is Status.PASS
    assert verdicts["rotation"] is Status.PASS


def the_console_s_data_is_removed_once_measured(out: Path) -> None:
    assert list(out.iterdir()) == []


# =========================================================================
# EX-1: the same console in real time, and the command line
# =========================================================================


def test_ex1_the_console_runs_in_real_time_on_a_loopback_port_and_stops_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real loop, the real web server, the real threads: six seconds asked, one session."""
    out = tmp_path / "out"

    rhythm = ["--manual", "3", "--rest", "2", "--sample-every", "2"]
    code = main(["real-time", "--minutes", "0.1", *rhythm, "--out", str(out)])
    printed = capsys.readouterr().out
    report = document((out / "endurance.json").read_text(encoding="utf-8"))

    assert code in (0, 1), "the verdict of so short a run is not the point"
    assert "## Endurance de la console (real-time)" in printed
    assert (out / "endurance.md").read_text(encoding="utf-8") in printed
    assert (report["mode"], report["anomaly"], report["requested_s"]) == ("real-time", None, 6.0)
    # The session running when the time was up was stopped, came down and was delivered.
    sessions = [obj(line) for line in seq(report["sessions"])]
    assert [(line["kind"], line["end"]) for line in sessions] == [("manual", "operator_stop")]
    assert all(line["as_planned"] is True and line["delivered"] is True for line in sessions)
    assert 6.0 < num(report["covered_s"]) < 60.0
    # The loop's own cadence, seen from outside it: a tick every 200 ms.
    period = obj(report["tick_period_ms"])
    tick = obj(report["tick_ms"])
    assert 150.0 <= num(period["p50"]) <= 260.0
    assert num(tick["count"]) >= 25
    assert 0.0 < num(tick["p50"]) < 150.0
    assert len(seq(report["samples"])) >= 3
    assert obj(report["sync"])["owed_at_the_end"] == 0
    # Nothing is left behind: the console's data is removed, its log is next to the report.
    left = {path.name for path in out.iterdir()}
    assert left - {"console.log"} == {"endurance.json", "endurance.md"}


def test_ex1_the_command_line_runs_a_simulated_day_with_the_dsp_on_all_along(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"

    rhythm = ["--manual", "3", "--rest", "2", "--sample-every", "5", "--dsp", "always"]
    code = main(["simulated", "--hours", "0.002", *rhythm, "--keep-data", "--out", str(out)])
    printed = capsys.readouterr().out
    report = document((out / "endurance.json").read_text(encoding="utf-8"))

    assert code in (0, 1), "the verdict of so short a run is not the point"
    assert "## Endurance de la console (simulated)" in printed
    assert (report["mode"], report["requested_s"]) == ("simulated", 7.0)
    assert obj(seq(report["sessions"])[0])["kind"] == "manual"
    assert NOTE_GATED_DSP not in seq(report["notes"])
    kept = [path for path in out.iterdir() if path.name.startswith("data-")]
    assert len(kept) == 1, "--keep-data leaves the console's folder to be looked at"
    assert (kept[0] / "records").is_dir()


def test_ex4_readings_taken_on_a_bench_are_judged_by_the_same_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    readings = tmp_path / "banc.csv"
    lines = [
        f"{1_790_000_000 + 60 * n},{90_000 + 7 * (n % 3)},{17 + n % 2},{90 * n},{300 * n}"
        for n in range(480)
    ]
    readings.write_text("\n".join([CSV_HEADER, *lines]) + "\n", encoding="utf-8")
    leaking = tmp_path / "fuite.csv"
    lines = [f"{1_790_000_000 + 60 * n},{90_000 + 40 * n},{17 + n // 60},0,1" for n in range(480)]
    leaking.write_text("\n".join(lines) + "\n", encoding="utf-8")

    steady = main(["judge", str(readings), "--out", str(tmp_path / "a")])
    first = capsys.readouterr().out
    leak = main(["judge", str(leaking), "--out", str(tmp_path / "b")])
    second = capsys.readouterr().out

    assert steady == 0
    assert "## Endurance de la console (observed) : PASS" in first
    assert "Durée couverte : 7 h 59" in first
    assert leak == 1
    assert "| `rss` | FAIL |" in second
    assert "| `descriptors` | FAIL |" in second
    report = document((tmp_path / "b" / "endurance.json").read_text(encoding="utf-8"))
    assert [obj(check)["name"] for check in seq(report["checks"])] == ["rss", "descriptors"]


# =========================================================================
# What the scenario must notice
# =========================================================================


async def test_ex1_a_session_that_does_not_finish_stops_the_scenario_and_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One second to come down from a stop is not enough: the scenario says so and stops."""
    monkeypatch.setattr("simulation.endurance.FINISH_LIMIT", 1)

    run = await simulated(60, tmp_path / "out", Plan(manual=6, rest=5, sample_every=5))

    assert run.anomaly == "session still ending 1 s after it should have ended"
    assert run.sessions == ()
    verdict = {check.name: check for check in judge(run)}["sessions"]
    assert verdict.status is Status.FAIL
    assert "arrêt du scénario : session still ending" in verdict.measured


async def test_ex1_a_programme_running_when_the_time_is_up_is_stopped_by_the_operator(
    tmp_path: Path,
) -> None:
    """The run does not wait six minutes for a programme to end by itself, nor leave it running."""
    run = await simulated(25, tmp_path / "out", Plan(manual=3, rest=2, sample_every=10))

    assert run.anomaly is None
    assert [(session.kind, session.end) for session in run.sessions] == [
        (Kind.MANUAL, "operator_stop"),
        (Kind.PROGRAMME, "operator_stop"),
    ]
    assert all(session.as_planned and session.delivered for session in run.sessions)
    # Stopped when the time was up, it came down and finished: it did not run its 400 s.
    assert run.sessions[1].started_at < 25
    assert run.sessions[1].recorded < FINISH_LIMIT
    assert statuses(run)["sessions"] is Status.PASS


async def test_ex1_a_start_the_console_has_not_taken_stops_the_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("simulation.endurance.COMMAND_SETTLE", 0)

    run = await simulated(60, tmp_path / "out", Plan(manual=6, rest=5, sample_every=5))

    assert run.anomaly == (
        "manual session not running 0 s after its start (state idle; last refusal: None)"
    )


async def test_ex1_a_dashboard_that_stops_answering_leaves_the_session_undelivered_and_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(now: Callable[[], int]) -> RecordingDashboard:
        return RecordingDashboard(now=now, online=False)

    monkeypatch.setattr("simulation.endurance.RecordingDashboard", unreachable)

    run = await simulated(10, tmp_path / "out", Plan(manual=3, rest=2, sample_every=20))

    sync = run.sync
    assert sync is not None
    assert run.anomaly is None
    assert [session.delivered for session in run.sessions] == [False]
    assert sync.owed >= 1
    assert (sync.points, sync.heartbeats) == (0, 0)
    # The scenario waited for it, two simulated minutes, before it gave up.
    assert run.covered >= 10 + CLOSING_LIMIT
    verdict = {check.name: check for check in judge(run)}["sessions"]
    assert verdict.status is Status.FAIL
    assert "séances non livrées : [1]" in verdict.measured
    assert "envois encore dus" in verdict.measured


async def test_ex1_a_day_of_no_length_runs_no_session_and_judges_nothing_green(
    tmp_path: Path,
) -> None:
    run = await simulated(0, tmp_path / "out", Plan())

    assert (run.sessions, run.samples, run.covered, run.anomaly) == ((), (), 0.0, None)
    verdicts = statuses(run)
    assert verdicts["rss"] is Status.FAIL
    assert verdicts["sessions"] is Status.FAIL
    assert "rotation" not in verdicts, "no record, nothing to say of the rotation"


async def test_ex1_the_operator_waits_for_the_console_and_reports_what_it_refuses() -> None:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/healthz" and len(calls) == 1:
            raise httpx.ConnectError("not listening yet")
        if request.url.path == "/api/manual/start":
            return httpx.Response(409, json={"detail": "une seance est deja en cours"})
        if request.url.path == "/api/presence":
            raise httpx.ReadError("the console is gone")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="http://console"
    ) as http:
        operator = Operator(http)
        await operator.ready(attempts=3, wait=0.0)
        await operator.attest()
        with pytest.raises(AnomalyError, match=r"POST /api/manual/start: HTTP 409 .*deja en cours"):
            await operator.start_manual()
        with pytest.raises(AnomalyError, match="POST /api/presence: the console did not answer"):
            await operator.ping()
    assert calls[:3] == ["/healthz", "/healthz", "/api/safety/attest"]

    def silent(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(silent), base_url="http://console"
    ) as http:
        with pytest.raises(AnomalyError, match="never answered on /healthz"):
            await Operator(http).ready(attempts=2, wait=0.0)


def console_on_a_manual_clock(tmp_path: Path) -> tuple[Console, ManualClock]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(EPOCH_MS))
    return build_console(tmp_path, clock, treat=GatedDsp(on=False), port=IDLE_PORT), clock


async def test_ex1_the_page_reads_every_event_and_remembers_what_the_console_refused(
    tmp_path: Path,
) -> None:
    console, clock = console_on_a_manual_clock(tmp_path)
    screen = Screen(console.panel.hub.subscribe())
    console.panel.surface.note_withdrawn("demarrage refuse : essai")
    clock.advance(Seconds(0.2))
    published = await console.panel.control_step()

    shown = await screen.read()

    assert shown is published
    assert screen.refusal is not None
    assert "demarrage refuse : essai" in screen.refusal
    await console.panel.close()


async def test_ex1_the_tick_meter_times_what_the_page_is_shown_and_ends_when_the_page_is_evicted(
    tmp_path: Path,
) -> None:
    console, clock = console_on_a_manual_clock(tmp_path)
    world = RealWorld(console, RealClock())
    meter = asyncio.create_task(world.meter())
    for _ in range(3):
        clock.advance(Seconds(0.2))
        await console.panel.control_step()
        await asyncio.sleep(0.01)
    # Nine events the page does not read: the hub evicts it, as it would a browser left behind.
    for index in range(9):
        console.panel.surface.note_withdrawn(f"refus {index}")

    await asyncio.wait_for(meter, 5.0)

    ticks = world.whole.summary()
    periods = world.periods.summary()
    assert ticks is not None
    assert ticks.count == 3
    assert periods is not None
    assert (periods.count, round(periods.highest)) == (2, 200)
    await console.panel.close()


def test_ex1_the_console_is_refused_without_its_dashboard_link_or_with_a_bad_configuration(
    tmp_path: Path,
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(EPOCH_MS))
    unlinked = {key: value for key, value in ENV.items() if key != "MACHINE_API_KEY"}

    with pytest.raises(ValueError, match="needs its dashboard link"):
        build_console(tmp_path, clock, treat=GatedDsp(on=False), port=IDLE_PORT, env=unlinked)
    with pytest.raises(ValueError, match="configuration refused"):
        build_console(
            tmp_path,
            clock,
            treat=GatedDsp(on=False),
            port=IDLE_PORT,
            env={**ENV, "MOTOR_BACKEND": "vapeur"},
        )
