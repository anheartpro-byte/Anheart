"""A finished session on the REAL console: no verdict at rest, the next start taken (ANH-181).

``tests/test_runtime_session_overrun.py`` proves the rule on the runtime alone.
This file proves what the operator and the dashboard see: the real composition
root (:func:`src.local_panel.build_panel`) over the simulated drive and the
simulated BITalino, spoken to through the operator's HTTP API, with a scripted
dashboard on the link (the console of ``tests/test_standstill_console.py``).

On ``develop`` a programme run to its end left the console in REPOS for 30 s.
Then ``session_overrun`` latched with nobody touching anything, the console
went back to ARRET for a whole recovery, and from there every acknowledgement
was taken back on the next tick and every start was refused (a START typed at
the console, a manual session, a launch from the site) until the console
process was restarted.

Three sequences, each on the 400 s programme of the failure rig:

* launched from the site, run to its end, then left at rest for twice its
  planned duration: no verdict on any snapshot, REPOS throughout, a status
  page with nothing standing and nothing latched, and a START typed at the
  console is taken;
* started at the console, run to its end, left at rest past the instant at
  which ``develop`` latched: a launch from the site is taken and confirmed to
  the dashboard, and after it a manual session typed at the console;
* a real overrun, raised DURING a session (a STOP typed late re-opens a whole
  recovery, which outlives the programme): the verdict comes at the same
  instant as on ``develop``, a START is refused in its name, and once the
  console is back to REPOS one named acknowledgement clears it for good.

Nothing is restarted anywhere in any of them: one console process throughout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest

from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_SESSION_OVERRUN
from src.training.types import RunMode, TelemetrySnapshot
from tests.test_failure_rig import (
    OPERATOR,
    SHORT_PROFILE,
    attest,
    start_programme,
)
from tests.test_standstill_console import END, START, Console, console

PLANNED: Final[float] = 400.0
"""The planned duration of ``failure_short``, in seconds."""

GRACE: Final[float] = 30.0
"""The rule's grace: ``develop`` latched this long after the programme's end."""


def _mode(run: Console) -> RunMode:
    """Read through a call: a checker would otherwise keep a narrowing across an ``await``."""
    return run.rig.panel.runtime.mode


async def _to_its_end(run: Console) -> None:
    """Tick until the programme has finished by itself: REPOS, nothing standing."""
    await run.run(PLANNED - 10.0)
    assert run.state() is RuntimeState.RUNNING, "the programme ended before its planned duration"
    await run.run(12.0)
    assert run.state() is RuntimeState.FINISHED
    assert run.rig.panel.runtime.end_reason is EndReason.PROGRAMME_COMPLETE
    assert _mode(run) is RunMode.REPOS
    assert run.standing_rule() is None


async def _at_rest(run: Console, seconds: float) -> list[TelemetrySnapshot]:
    """Tick with nobody touching anything; every snapshot is at rest with no verdict."""
    seen = await run.run(seconds)
    for snapshot in seen:
        at = f"{float(snapshot.elapsed):.1f} s after the start"
        assert snapshot.safety is None, f"{snapshot.safety} stands at rest, {at}"
        assert snapshot.mode is RunMode.REPOS, f"the console left REPOS, {at}"
        assert snapshot.setpoint.motor_rpm == 0
    assert run.state() is RuntimeState.FINISHED
    assert run.rig.simulator.commanded_setpoint == 0
    return seen


async def _nothing_stands_on_the_status_page(session: httpx.AsyncClient) -> None:
    status = await session.get("/api/status")
    assert status.status_code == 200, status.text
    assert status.json()["standing"] is None
    assert status.json()["floor"] is None
    assert RULE_SESSION_OVERRUN not in status.text
    latest = await session.get("/api/snapshot")
    assert latest.json()["safety"] is None
    assert latest.json()["mode"] == "repos"


async def test_a_programme_launched_from_the_site_then_twice_its_length_at_rest_takes_a_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion at the machine. On ``develop``: latched 30 s after the end.

    The measured sequence: a launch from the site, the programme run to its
    own end, the end reported to the dashboard as a success. Then twice the
    planned duration with nobody at the console. A START typed there is
    answered 202 and the programme runs.
    """
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    async with rig.http() as session:
        await run.run(10.0)
        await attest(session)
        run.offer("remote-1")
        await run.run(8.0)
        assert run.state() is RuntimeState.RUNNING
        assert run.dashboard.to(START) == [{"sessionId": "remote-1"}]

        await _to_its_end(run)
        reported = run.ended("remote-1")
        assert len(reported) == 1, run.dashboard.to(END)
        assert reported[0]["failed"] is False

        rest = await _at_rest(run, 2 * PLANNED + GRACE)
        assert float(rest[-1].elapsed) > 3 * PLANNED
        await _nothing_stands_on_the_status_page(session)
        assert len(run.ended("remote-1")) == 1, "the finished session was reported again"

        typed = await session.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_age": 30,
            },
        )
        assert typed.status_code == 202, typed.text
        await run.run(5.0)
        assert run.state() is RuntimeState.RUNNING
        assert _mode(run) is RunMode.SEANCE
        assert run.standing_rule() is None
        assert rig.refusals() == []
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_programme_started_at_the_console_and_left_at_rest_takes_a_launch_from_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way round, and then a manual session. On ``develop`` both were refused.

    The launch went back to the dashboard as a failed session naming a
    verdict to acknowledge at the console, and the acknowledgement did not
    hold. Here it is taken and confirmed; it is stopped, and a manual session
    typed at the console is taken after it.
    """
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    async with rig.http() as session:
        await run.run(10.0)
        await start_programme(session)
        await run.run(2.0)
        assert run.state() is RuntimeState.RUNNING
        await _to_its_end(run)

        await _at_rest(run, GRACE + 60.0)
        await _nothing_stands_on_the_status_page(session)

        run.offer("remote-2")
        await run.run(8.0)
        assert run.state() is RuntimeState.RUNNING, rig.refusals()
        assert run.dashboard.to(START) == [{"sessionId": "remote-2"}]
        assert run.ended("remote-2") == []

        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == 202, stop.text
        await run.run(70.0)
        assert run.state() is RuntimeState.FINISHED
        assert run.standing_rule() is None

        manual = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert manual.status_code == 202, manual.text
        await run.run(2.0)
        assert run.state() is RuntimeState.RUNNING
        assert _mode(run) is RunMode.MANUEL
        assert rig.refusals() == []
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_an_overrun_raised_by_a_late_stop_is_acknowledged_for_good_once_at_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EX-3 at the machine. On ``develop`` the acknowledgement was accepted and taken back.

    STOP is typed 20 s before the end, while the programme is in its own
    recovery with the arm at rest. It is accepted and re-opens a whole 60 s
    recovery, which ends 40 s after the planned duration: the session is still
    in progress when its 30 s of grace run out, and the rule fires then, as it
    always has. What is new comes after: back at REPOS the verdict is latched
    and no longer firing, so the acknowledgement holds and a START is taken.
    """
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await run.run(10.0)
        await start_programme(session)
        await run.run(PLANNED - 20.0)
        assert run.state() is RuntimeState.RUNNING
        assert runtime.applied_rpm == 0, "the programme is not in its own recovery yet"
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == 202, stop.text

        # Unchanged: the session outlives its programme, and the rule says so.
        before = await run.run(20.0 + GRACE - 1.0)
        assert all(snapshot.safety is None for snapshot in before), "the rule fired early"
        await run.run(2.0)
        assert run.standing_rule() == RULE_SESSION_OVERRUN
        assert run.state() is RuntimeState.ENDING
        assert _mode(run) is RunMode.ARRET
        early = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert early.status_code == 200, early.text
        await run.run(1.0)
        assert run.standing_rule() == RULE_SESSION_OVERRUN, "cleared before the session was over"

        # The session is over: REPOS, the verdict latched, the rule silent.
        await run.run(20.0)
        assert run.state() is RuntimeState.FINISHED
        assert _mode(run) is RunMode.REPOS
        assert {verdict.rule for verdict in runtime.supervisor.live} == set()
        status = await session.get("/api/status")
        for field in ("standing", "floor"):
            assert status.json()[field]["rule"] == RULE_SESSION_OVERRUN, field
            assert status.json()[field]["latched"] is True
        typed = await session.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_age": 30,
            },
        )
        assert typed.status_code == 409, typed.text
        assert RULE_SESSION_OVERRUN in typed.json()["detail"]
        assert "acknowledged by name" in typed.json()["detail"]

        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["cleared"] == [RULE_SESSION_OVERRUN]
        await _at_rest(run, 60.0)
        await _nothing_stands_on_the_status_page(session)

        typed = await session.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_age": 30,
            },
        )
        assert typed.status_code == 202, typed.text
        await run.run(3.0)
        assert run.state() is RuntimeState.RUNNING
        assert run.standing_rule() is None
    await rig.panel.close()
    assert await rig.left_stopped() == ""
