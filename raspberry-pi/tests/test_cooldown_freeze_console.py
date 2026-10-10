"""A programme's own descent under a FREEZE, on the REAL console and as its page is told (ANH-189).

``tests/test_runtime_cooldown_freeze.py`` proves the rule on the runtime alone.
This file proves it at the machine: the real composition root
(:func:`src.local_panel.build_panel`) over the simulated drive and the
simulated BITalino, spoken to through the operator's HTTP API, ticked as its
loop does (the console of ``tests/test_standstill_console.py``).

One sequence, on the 400 s programme of the failure rig (BASELINE to 20 s,
WARMUP to 140, HOLD to 280, COOLDOWN to 340, RECOVERY to 400):

* the loop misses a second in the middle of HOLD: ``loop_stall``, a FREEZE
  that latches, raised by the supervisor itself and acknowledged by nobody;
* the setpoint is held to the end of HOLD, comes down from the first tick of
  COOLDOWN, and the session is over at its planned duration, as a programme
  run to its end;
* thirty seconds later, where ``session_overrun`` used to end such a session,
  nothing happens: the latched FREEZE is all that stands, a START is refused
  in its name, one named acknowledgement clears it, and a START is taken.

And it reads what the page is given while the setpoint comes down
(``/api/snapshot``, the row the page renders, and ``/api/status``): the page
prints a verdict's sentence as it is, so the sentence of this FREEZE must not
say that the setpoint is held and stop there.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Final

import httpx
import pytest

from src.training.runtime import EndReason, RuntimeState
from src.training.safety import (
    RULE_LOOP_STALL,
    RULE_SESSION_OVERRUN,
    RULE_SESSION_STANDSTILL,
    SELF_CLEARING,
)
from src.training.types import Phase, RunMode, SafetyAction, TelemetrySnapshot
from src.units import Seconds
from tests.test_failure_rig import MIN_RUN, OPERATOR, SHORT_PROFILE, TICK, Rig, start_programme
from tests.test_standstill_console import Console, console

STALL_AT: Final[float] = 200.0
"""Seconds into ``failure_short`` at which the loop stalls: the middle of HOLD."""

COOLDOWN_AT: Final[float] = 280.0
PLANNED: Final[float] = 400.0
GRACE: Final[float] = 30.0
"""The grace of ``session_overrun``: it judges a session still in progress past it."""

STILL_COMES_DOWN: Final[str] = (
    "the setpoint is held where it was (it still comes down on a stop asked for, "
    "and on the programme's own descent)"
)
"""What the FREEZE of ``loop_stall`` says for as long as it is latched."""

START: Final[dict[str, object]] = {
    "profile_id": SHORT_PROFILE,
    "operator": OPERATOR,
    "total_duration_s": None,
    "subject_age": 30,
}


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _applied(rig: Rig) -> int:
    """Read afresh, through a call: a checker would keep a narrowing across an ``await``."""
    return int(rig.panel.runtime.applied_rpm)


def _phase(rig: Rig) -> Phase:
    return rig.panel.runtime.phase


def _mode(rig: Rig) -> RunMode:
    return rig.panel.runtime.mode


async def _until(run: Console, seconds: float) -> list[TelemetrySnapshot]:
    """Tick the console until ``seconds`` after the session's start.

    A second at a time, and the time left is read again from the last
    snapshot: a stall of the loop moves the session on without a tick.
    """
    seen: list[TelemetrySnapshot] = []
    while (left := seconds - float(run.rig.snapshots[-1].elapsed)) > TICK / 2:
        seen += await run.run(min(left, 1.0))
    return seen


def _never_rises(setpoints: list[int]) -> bool:
    return all(later <= earlier for earlier, later in pairwise(setpoints))


async def test_at_the_console_a_programme_frozen_on_its_plateau_comes_down_and_ends_on_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion at the machine, and the sentence its page shows meanwhile."""
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await run.run(10.0)
        await start_programme(session)
        await run.run(2.0)
        await _until(run, STALL_AT)
        assert _phase(rig) is Phase.HOLD
        assert _applied(rig) > MIN_RUN, "the arm is not turning"
        assert run.standing_rule() is None

        # The loop misses a second: a FREEZE that latches, and nobody acknowledges it.
        rig.clock.advance(Seconds(1.0))
        held_for = await _until(run, COOLDOWN_AT - 1.0)
        held = _applied(rig)
        assert set(_setpoints(held_for)) == {held}, "the FREEZE did not hold the speed in HOLD"
        assert {snapshot.safety_action for snapshot in held_for} == {SafetyAction.FREEZE}
        assert _phase(rig) is Phase.HOLD
        shown = await session.get("/api/snapshot")
        assert shown.json()["mode"] == "seance"
        assert shown.json()["phase"] == "hold"
        assert shown.json()["safety_action"] == "freeze"
        assert shown.json()["safety"]["rule"] == RULE_LOOP_STALL
        assert shown.json()["safety"]["latched"] is True
        assert shown.json()["setpoint"]["motor_rpm"] == held

        # The entry into COOLDOWN: held on the last tick of HOLD, lower on the
        # first tick of COOLDOWN, still under the FREEZE.
        entering = await run.run(3.0)
        last_of_hold = [snapshot for snapshot in entering if snapshot.phase is Phase.HOLD]
        down = [snapshot for snapshot in entering if snapshot.phase is Phase.COOLDOWN]
        assert len(last_of_hold) + len(down) == len(entering)
        assert set(_setpoints(last_of_hold)) == {held}
        first = down[0]
        assert abs(float(first.elapsed) - COOLDOWN_AT) <= TICK, f"COOLDOWN at {first.elapsed} s"
        assert first.safety_action is SafetyAction.FREEZE
        assert first.setpoint.motor_rpm < held, "the FREEZE held the setpoint into the cooldown"
        assert len(down) > 5, "the descent has hardly begun"

        # What the page is given while it comes down: no sentence there says "held" and stops.
        shown = await session.get("/api/snapshot")
        assert shown.json()["mode"] == "seance"
        assert shown.json()["phase"] == "cooldown"
        assert shown.json()["safety_action"] == "freeze"
        assert 0 < shown.json()["setpoint"]["motor_rpm"] < first.setpoint.motor_rpm
        assert shown.json()["safety"]["rule"] == RULE_LOOP_STALL
        assert shown.json()["safety"]["latched"] is True
        assert STILL_COMES_DOWN in shown.json()["safety"]["detail"], shown.text
        assert shown.json()["manual"] is None, "a programme has no manual card to say anything"
        status = await session.get("/api/status")
        assert status.status_code == httpx.codes.OK, status.text
        for field in ("standing", "floor"):
            assert status.json()[field]["rule"] == RULE_LOOP_STALL, field
            assert status.json()[field]["latched"] is True
            assert status.json()[field]["action"] == "freeze"
            assert STILL_COMES_DOWN in status.json()[field]["detail"], field
        assert SELF_CLEARING not in status.text
        assert SELF_CLEARING not in shown.text

        # Down to zero on the ordinary ramp, never up, and nothing but the FREEZE standing.
        down += await _until(run, COOLDOWN_AT + 40.0)
        assert _never_rises([held, *_setpoints(down)])
        assert _applied(rig) == 0, "the arm never came down"
        turning = [snapshot for snapshot in down if snapshot.setpoint.motor_rpm != 0]
        assert {snapshot.safety_action for snapshot in turning} == {SafetyAction.FREEZE}
        assert {snapshot.mode for snapshot in turning} == {RunMode.SEANCE}
        assert {snapshot.phase for snapshot in turning} == {Phase.COOLDOWN}

        # The end, at the planned duration, and the deadline of the overrun rule after it.
        rest = await _until(run, PLANNED + GRACE + 10.0)
        assert set(_setpoints(rest)) == {0}, "something moved after the descent"
        over = next(snapshot for snapshot in rest if snapshot.phase is Phase.DONE)
        assert abs(float(over.elapsed) - PLANNED) <= 2 * TICK
        assert runtime.end_reason is EndReason.PROGRAMME_COMPLETE
        assert run.state() is RuntimeState.FINISHED
        assert _mode(rig) is RunMode.REPOS
        assert rig.simulator.commanded_setpoint == 0
        spoken = {snapshot.safety.rule for snapshot in rig.snapshots if snapshot.safety is not None}
        assert RULE_SESSION_OVERRUN not in spoken
        assert RULE_SESSION_STANDSTILL not in spoken
        assert run.standing_rule() == RULE_LOOP_STALL

        # At rest the latched FREEZE is all that stands: it refuses a start, by name.
        typed = await session.post("/api/session/start", json=START)
        assert typed.status_code == httpx.codes.CONFLICT, typed.text
        assert RULE_LOOP_STALL in typed.json()["detail"]
        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == httpx.codes.OK, acknowledged.text
        assert acknowledged.json()["cleared"] == [RULE_LOOP_STALL]
        after = await run.run(10.0)
        assert set(_setpoints(after)) == {0}, "the acknowledgement let something move"
        assert all(snapshot.safety is None for snapshot in after)
        assert run.state() is RuntimeState.FINISHED

        typed = await session.post("/api/session/start", json=START)
        assert typed.status_code == httpx.codes.ACCEPTED, typed.text
        await run.run(3.0)
        assert run.state() is RuntimeState.RUNNING
        assert run.standing_rule() is None
    await rig.panel.close()
    assert await rig.left_stopped() == ""
