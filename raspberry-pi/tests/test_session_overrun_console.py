"""The end of a session on the REAL console: what latches, and what the operator is shown.

``tests/test_runtime_session_overrun.py`` and
``tests/test_runtime_ending_alerts.py`` prove the rules on the runtime alone.
This file proves what the operator and the dashboard see: the real composition
root (:func:`src.local_panel.build_panel`) over the simulated drive and the
simulated BITalino, spoken to through the operator's HTTP API, with a scripted
dashboard on the link (the console of ``tests/test_standstill_console.py``).

Two defects are covered, on one console process per sequence, never restarted.

**ANH-181.** A programme run to its end left the console in REPOS for 30 s.
Then ``session_overrun`` latched with nobody touching anything, the console
went back to ARRET for a whole recovery, and from there every acknowledgement
was taken back on the next tick and every start was refused until the console
process was restarted.

**ANH-185.** An alert latched at the end of a session must say something
true. A STOP typed late re-opens a whole recovery that outlives the programme,
and ``session_overrun`` latched over it; and an emergency stop pressed at rest
after a programme that had ended by itself put the console back to ARRET for a
whole recovery, with the heart-rate rules judging somebody who had left.

Three sequences:

* a programme launched from the site, run to its end, then left at rest for
  twice its planned duration with the electrodes off: no verdict on any
  snapshot, REPOS throughout, nothing standing on the status page. Then the
  emergency stop is pressed at rest: REPOS still, the phase ``done``, the stop
  latched and every start refused in its name, no other rule firing; one
  acknowledgement, and a START typed at the console is taken;
* a programme started at the console and stopped late, in its own recovery:
  the recovery it re-opens outlives the programme's deadline and no verdict
  appears on any snapshot. At rest past the instant at which ``develop``
  latched, a manual session typed at the console is taken, and after it a
  launch from the site, confirmed to the dashboard;
* a real overrun, raised DURING a session: a manual session whose descent is
  blocked at its limit (the guard that follows a stop under a FREEZE is forced
  off, as in the runtime tests). The rule brings the arm down; a START is
  refused in its name; an acknowledgement given before REPOS is taken back,
  and once the console is at REPOS one named acknowledgement clears it for
  good.

**What these cost.** They ran the 400 s programme of the failure rig and
1100 s of rest, ticking the whole ECG chain throughout: 318 s under coverage.
Nothing they prove depends on those lengths, nor on the arm turning during
the programme: what is judged here is the end of a session and the rest that
follows. So the programme lasts 80 s, with its five phases and a whole 60 s
recovery; the rests are counted against it; and the electrodes are off while
nobody is on board, which is what happens at a machine and spares the
confirmation of a heart rate nobody reads. In 80 s the control law does not
get as far as asking for the slowest running speed, so the arm stays at rest
through the two programmes: the sequence that needs motion is the third, a
manual session at 199 motor rpm, and it is the simulated drive that shows the
arm brought down.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest

from src.training import runtime as runtime_module
from src.training.runtime import EndReason, RuntimeState, TrainingRuntime
from src.training.safety import RULE_OPERATOR_ESTOP, RULE_SESSION_OVERRUN
from src.training.types import RunMode, SafetyAction, TelemetrySnapshot
from src.units import Seconds
from tests.test_failure_rig import OPERATOR, attest, start_manual
from tests.test_runtime_session_overrun import (
    _nothing_asks_for_the_descent,  # pyright: ignore[reportPrivateUsage]
)
from tests.test_standstill_console import END, LINKED_ENV, START, Console, console

QUICK_PROFILE: Final[str] = "ending_quick"
"""A whole programme in 80 s: its five phases, and the shortest recovery a profile may have."""

QUICK_STORE: Final[str] = """{
  "version": 1,
  "rev": 0,
  "profiles": [
    {
      "profile_id": "ending_quick",
      "name": "end of session, quick",
      "total_duration_s": 80.0,
      "baseline_s": 5.0,
      "warmup_max_s": 5.0,
      "hold_min_s": 5.0,
      "cooldown_s": 5.0,
      "recovery_s": 60.0,
      "zone_low_bpm": 118,
      "zone_high_bpm": 138,
      "hard_max_bpm": 148,
      "critical_bpm": 158,
      "subject_hr_max": 162,
      "min_run_rpm": 55,
      "max_rpm": 276,
      "warmup_rpm_ceiling_fraction": 0.6,
      "channels": ["ECG"],
      "allow_above_nameplate": false
    }
  ]
}
"""

PLANNED: Final[float] = 80.0
"""The planned duration of ``ending_quick``, in seconds. RECOVERY from 20 s."""

GRACE: Final[float] = 30.0
"""The rule's grace: ``develop`` latched this long after the programme's end."""

START_BODY: Final[dict[str, object]] = {
    "profile_id": QUICK_PROFILE,
    "operator": OPERATOR,
    "total_duration_s": None,
    "subject_age": 30,
}

MANUAL_LIMIT: Final[Seconds] = Seconds(30.0)
"""The manual session limit of the third sequence (3600 s on the machine)."""

BENCH_CEILING: Final[int] = 300
"""Its bench ceiling, motor rpm."""

EXPECTED_DESCENT: Final[float] = 15.8
"""What that session's ending is given to reach zero from the 199 motor rpm it is held at:
the 11.8 s of the motion-limited walk from that speed, and the drive's 4 s ramp."""


def _console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bench_ceiling: int | None = None
) -> Console:
    """The linked console of the standstill tests, on the 80 s programme."""
    (tmp_path / "profiles.json").write_text(QUICK_STORE, encoding="utf-8")
    env = dict(LINKED_ENV)
    if bench_ceiling is not None:
        env["MOTOR_MAX_RPM"] = str(bench_ceiling)
    return console(tmp_path, monkeypatch, env)


def _mode(run: Console) -> RunMode:
    """Read through a call: a checker would otherwise keep a narrowing across an ``await``."""
    return run.rig.panel.runtime.mode


def _elapsed(run: Console) -> float:
    """Seconds since the session started, as the runtime counts them."""
    return float(run.rig.panel.runtime.snapshot().elapsed)


async def _until(run: Console, elapsed: float) -> list[TelemetrySnapshot]:
    """Tick until the session has run ``elapsed`` seconds; every snapshot on the way."""
    return await run.run(elapsed - _elapsed(run))


async def _type_start(session: httpx.AsyncClient) -> httpx.Response:
    return await session.post("/api/session/start", json=START_BODY)


async def _to_its_end(run: Console) -> None:
    """Tick until the programme has finished by itself: REPOS, nothing standing."""
    await _until(run, PLANNED - 2.0)
    assert run.state() is RuntimeState.RUNNING, "the programme ended before its planned duration"
    await run.run(4.0)
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
    """ANH-181 at the machine, then the emergency stop at rest of ANH-185.

    The measured sequence: a launch from the site, the programme run to its
    own end, the end reported to the dashboard as a success. Then twice the
    planned duration with nobody at the console and the electrodes off. On
    ``develop`` ``session_overrun`` latched 30 s after the end.

    Then somebody presses the emergency stop. After ANH-181 that put the
    console back to ARRET and to a whole recovery, and ``hr_stale`` latched
    with it, at once here, over electrodes nobody had worn for minutes. Now
    the console stays in REPOS,
    the phase ``done``; the stop is latched and shown, no other rule fires,
    a START is refused in its name, and the finished session is not reported
    again. One acknowledgement, with the mushroom released, and a START typed
    at the console is answered 202 and the programme runs.
    """
    run = _console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await run.run(10.0)
        await attest(session)
        run.offer("remote-1", QUICK_PROFILE)
        await run.run(5.0)
        assert run.state() is RuntimeState.RUNNING
        assert run.dashboard.to(START) == [{"sessionId": "remote-1"}]

        await _to_its_end(run)
        reported = run.ended("remote-1")
        assert len(reported) == 1, run.dashboard.to(END)
        assert reported[0]["failed"] is False

        run.electrodes.off = True
        rest = await _at_rest(run, 2 * PLANNED + GRACE)
        assert float(rest[-1].elapsed) > 3 * PLANNED
        await _nothing_stands_on_the_status_page(session)
        assert rig.refusals() == []

        pressed = await session.post("/api/session/estop", json={"operator": OPERATOR})
        assert pressed.status_code == 200, pressed.text
        for snapshot in await run.run(10.0):
            at = f"{float(snapshot.elapsed):.1f} s after the start"
            assert snapshot.mode is RunMode.REPOS, f"the e-stop at rest left REPOS, {at}"
            assert snapshot.phase.value == "done", f"a finished session went back to recovery, {at}"
            assert snapshot.safety is not None
            assert snapshot.safety.rule == RULE_OPERATOR_ESTOP, f"{snapshot.safety.rule}, {at}"
            assert snapshot.setpoint.motor_rpm == 0
        assert {verdict.rule for verdict in runtime.supervisor.live} == set()
        assert runtime.ending is None
        assert runtime.end_reason is EndReason.PROGRAMME_COMPLETE
        status = await session.get("/api/status")
        assert status.json()["standing"]["rule"] == RULE_OPERATOR_ESTOP
        assert status.json()["standing"]["latched"] is True
        assert status.json()["floor"] is None
        latest = await session.get("/api/snapshot")
        assert (latest.json()["mode"], latest.json()["phase"]) == ("repos", "done")
        refused = await _type_start(session)
        assert refused.status_code == 409, refused.text
        assert RULE_OPERATOR_ESTOP in refused.json()["detail"]
        assert len(run.ended("remote-1")) == 1, "the finished session was reported again"

        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["cleared"] == [RULE_OPERATOR_ESTOP]
        await _at_rest(run, 2.0)
        await _nothing_stands_on_the_status_page(session)

        run.electrodes.off = False
        typed = await _type_start(session)
        assert typed.status_code == 202, typed.text
        await run.run(3.0)
        assert run.state() is RuntimeState.RUNNING
        assert _mode(run) is RunMode.SEANCE
        assert run.standing_rule() is None
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_programme_stopped_late_at_the_console_latches_nothing_and_takes_the_next_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANH-185 at the machine, then the two other starts of ANH-181.

    STOP is typed 25 s before the end, while the programme is in its own
    recovery with the arm at rest. It is accepted and re-opens a whole 60 s
    recovery, which ends 35 s after the planned duration: the session is still
    on its way out when the programme's 30 s of grace run out. On ``develop``
    ``session_overrun`` latched there, over an ending going exactly as it
    should. No verdict appears on any snapshot, and the console is back to
    REPOS with nothing to acknowledge.

    Then, at rest past the instant at which ``develop`` latched: a manual
    session typed at the console is taken; it is stopped, and a launch from
    the site is taken and confirmed to the dashboard. On ``develop`` both were
    refused, the launch going back as a failed session naming a verdict to
    acknowledge at the console.
    """
    run = _console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await run.run(10.0)
        await attest(session)
        started = await _type_start(session)
        assert started.status_code == 202, started.text
        await run.run(2.0)
        assert run.state() is RuntimeState.RUNNING

        await _until(run, PLANNED - 25.0)
        assert run.state() is RuntimeState.RUNNING
        assert runtime.applied_rpm == 0, "the programme is not in its own recovery yet"
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == 202, stop.text

        ending = await _until(run, PLANNED + 37.0)
        assert all(snapshot.safety is None for snapshot in ending), "a verdict over a normal ending"
        outlived = [s for s in ending if float(s.elapsed) > PLANNED + GRACE + 1.0]
        assert outlived[0].mode is RunMode.ARRET, "over before the old deadline: not this case"
        assert run.state() is RuntimeState.FINISHED
        assert runtime.end_reason is EndReason.OPERATOR_STOP
        assert _mode(run) is RunMode.REPOS

        run.electrodes.off = True
        await _at_rest(run, 20.0)
        await _nothing_stands_on_the_status_page(session)

        manual = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert manual.status_code == 202, manual.text
        await run.run(2.0)
        assert run.state() is RuntimeState.RUNNING
        assert _mode(run) is RunMode.MANUEL
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stop.status_code == 202, stop.text
        # The next rider is fitted: a programme refuses to run on a heart rate
        # that has not been read for a minute.
        run.electrodes.off = False
        await run.run(4.0)
        assert run.state() is RuntimeState.FINISHED
        assert run.standing_rule() is None

        run.offer("remote-2", QUICK_PROFILE)
        await run.run(5.0)
        assert run.state() is RuntimeState.RUNNING, rig.refusals()
        assert _mode(run) is RunMode.SEANCE
        assert run.dashboard.to(START) == [{"sessionId": "remote-2"}]
        assert run.ended("remote-2") == []
        assert rig.refusals() == []
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_an_overrun_raised_during_a_session_is_acknowledged_for_good_once_at_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real overrun at the machine: raised, shown, refused against, then cleared for good.

    A manual session, the capsule empty, a ceiling of 300 motor rpm and a
    limit of 30 s. A FREEZE latches while the arm turns at 199 motor rpm, and
    the guard that follows a stop under a FREEZE is forced off: at its limit
    the session ends itself and its descent does not happen. The ending is
    given the 15.8 s a descent from that speed is expected to take, and the
    rule's 30 s of grace. Until then nothing but the FREEZE stands; past it
    ``session_overrun`` latches, its RAMP_DOWN outranks the FREEZE, and the
    reference comes down to zero on the simulated drive.

    An acknowledgement given while the arm is coming down is taken back on
    the next tick. Back at REPOS the verdict is latched and no longer firing:
    a START is refused in its name, one named acknowledgement clears it, it
    stays cleared, and a START is taken (on ``develop``, before ANH-181, it
    came back for good).
    """
    monkeypatch.setattr(runtime_module, "MANUAL_SESSION_LIMIT", MANUAL_LIMIT)
    monkeypatch.setattr(TrainingRuntime, "_stop_asked", _nothing_asks_for_the_descent)
    run = _console(tmp_path, monkeypatch, bench_ceiling=BENCH_CEILING)
    run.electrodes.off = True
    rig = run.rig
    runtime = rig.panel.runtime
    limit = float(MANUAL_LIMIT)
    async with rig.http() as session:
        await run.run(2.0)
        await start_manual(rig, session, 4.0)
        await _until(run, 20.0)
        held = int(runtime.applied_rpm)
        assert held == 199, "the manual session is not at speed"
        runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")

        await _until(run, limit + 1.0)
        assert runtime.end_reason is EndReason.PROGRAMME_COMPLETE, "the limit ended nothing"
        assert _mode(run) is RunMode.ARRET
        before = await _until(run, limit + EXPECTED_DESCENT + GRACE - 1.0)
        assert {s.safety.rule for s in before if s.safety is not None} == {"rig_freeze"}, (
            "the rule fired before the ending had run out of its budget"
        )
        assert {int(s.setpoint.motor_rpm) for s in before} == {held}, "the descent was not blocked"

        await run.run(2.0)
        assert run.standing_rule() == RULE_SESSION_OVERRUN
        assert run.state() is RuntimeState.ENDING
        early = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert early.status_code == 200, early.text
        await run.run(1.0)
        assert run.standing_rule() == RULE_SESSION_OVERRUN, "cleared before the session was over"
        assert 0 < runtime.applied_rpm < held, "RAMP_DOWN is not bringing the held arm down"

        # The session is over: REPOS, the verdict latched, the rule silent.
        await run.run(25.0)
        assert run.state() is RuntimeState.FINISHED
        assert _mode(run) is RunMode.REPOS
        assert rig.simulator.commanded_setpoint == 0
        assert {verdict.rule for verdict in runtime.supervisor.live} == set()
        status = await session.get("/api/status")
        for field in ("standing", "floor"):
            assert status.json()[field]["rule"] == RULE_SESSION_OVERRUN, field
            assert status.json()[field]["latched"] is True
        typed = await _type_start(session)
        assert typed.status_code == 409, typed.text
        assert RULE_SESSION_OVERRUN in typed.json()["detail"]
        assert "acknowledged by name" in typed.json()["detail"]

        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["cleared"] == [RULE_SESSION_OVERRUN]
        await _at_rest(run, 10.0)
        await _nothing_stands_on_the_status_page(session)

        typed = await _type_start(session)
        assert typed.status_code == 202, typed.text
        await run.run(3.0)
        assert run.state() is RuntimeState.RUNNING
        assert run.standing_rule() is None
    await rig.panel.close()
    assert await rig.left_stopped() == ""
