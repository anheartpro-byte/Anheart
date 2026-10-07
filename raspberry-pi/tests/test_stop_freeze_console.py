"""A stop asked for under a FREEZE, on the REAL console and through each of its doors (ANH-175).

``tests/test_runtime_stop_freeze.py`` proves the rule on the runtime alone,
where every request is the same call. This file proves that each of the three
ways an operator has of asking reaches it and acts, on the real composition
root (:func:`src.local_panel.build_panel`) over the simulated drive, ticked as
its loop does:

* **STOP at the console**: ``POST /api/session/stop``;
* **a manual target of zero**: ``POST /api/manual/target``;
* **a stop sent from the site**: the dashboard answers ``stopRequested`` to the
  console's status poll, and the link forwards it to the same mailbox.

And it reads what the page is given while that happens (``/api/snapshot``): no
ramp and no arrival time over a setpoint a FREEZE holds, a real ramp with its
arrival time once the descent is under way, and ``ARRET`` only over a setpoint
that is coming down.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import Final

import httpx
import pytest

from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_HR_STALE, RULE_SESSION_STANDSTILL
from src.training.types import RunMode, SafetyAction, TelemetrySnapshot
from tests.test_cloud_sync import ok
from tests.test_failure_rig import OPERATOR, TICK, Rig, attest, make_rig, start_manual
from tests.test_standstill_console import TURNING_AT, console

STATUS: Final[str] = "/api/machine/training/status"

TYPED: Final[float] = 4.0
"""The manual target typed at the console, in output rpm: 199 at the motor shaft."""

ASKED: Final[int] = 199

FREEZE: Final[str] = "rig_freeze"
"""The rule tripped from a thread, as the acquisition thread would: a latched FREEZE."""


def _setpoints(snapshots: list[TelemetrySnapshot]) -> list[int]:
    return [int(snapshot.setpoint.motor_rpm) for snapshot in snapshots]


def _applied(rig: Rig) -> int:
    """Read afresh, through a call: a checker would keep a narrowing across an ``await``."""
    return int(rig.panel.runtime.applied_rpm)


def _state(rig: Rig) -> RuntimeState:
    return rig.panel.runtime.state


async def _ticks(rig: Rig, seconds: float) -> list[TelemetrySnapshot]:
    """Tick the console and keep every snapshot it produced."""
    first = len(rig.snapshots)
    await rig.tick(seconds)
    return rig.snapshots[first:]


def _never_rises(setpoints: list[int]) -> bool:
    return all(later <= earlier for earlier, later in pairwise(setpoints))


async def test_at_the_console_a_zero_target_then_stop_act_under_a_freeze_and_the_page_says_so(
    tmp_path: Path,
) -> None:
    """Manual, empty capsule, a latched FREEZE caught in the middle of a climb.

    On ``develop`` the page showed "RAMPE EN COURS, arrivee dans ..." over a
    setpoint that did not move, a target of zero was taken and changed
    nothing, and STOP showed ``ARRET`` over the same held speed.
    """
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await start_manual(rig, session, TYPED)
        await rig.tick(8.0)
        runtime.trip_from_thread(FREEZE, SafetyAction.FREEZE, "under test")
        frozen = await _ticks(rig, 3.0)
        held = _applied(rig)
        assert 55 < held < ASKED, "the FREEZE did not catch the climb"
        assert set(_setpoints(frozen)[1:]) == {held}

        # Held away from its target: no ramp and no arrival time are announced.
        # (`/api/snapshot` is the row the page renders.)
        shown = await session.get("/api/snapshot")
        assert shown.json()["safety_action"] == "freeze"
        assert shown.json()["mode"] == "manuel"
        assert shown.json()["setpoint"]["motor_rpm"] == held
        assert shown.json()["manual"]["target"]["motor_rpm"] == ASKED
        assert shown.json()["manual"]["ramping"] is False
        assert shown.json()["manual"]["ramp_eta_s"] is None

        # A target of zero typed at the console: taken, and followed.
        typed = await session.post(
            "/api/manual/target", json={"output_rpm": 0.0, "operator": OPERATOR}
        )
        assert typed.status_code == httpx.codes.ACCEPTED, typed.text
        lowered = await _ticks(rig, 2.0)
        after_zero = _applied(rig)
        assert after_zero < held, "a target of zero under a FREEZE moved nothing"
        assert _never_rises([held, *_setpoints(lowered)])
        shown = await session.get("/api/snapshot")
        assert shown.json()["safety_action"] == "freeze"
        assert shown.json()["mode"] == "manuel"
        assert shown.json()["manual"]["target"]["motor_rpm"] == 0
        assert shown.json()["manual"]["ramping"] is True
        announced = rig.snapshots[-1].manual
        assert announced is not None
        eta = announced.ramp_eta
        assert eta is not None
        assert shown.json()["manual"]["ramp_eta_s"] == eta > 0.0

        # STOP at the console, the FREEZE still standing: the session ends, and
        # ARRET is shown over a setpoint that goes on coming down.
        stopped = await session.post("/api/session/stop", json={"operator": OPERATOR})
        assert stopped.status_code == httpx.codes.ACCEPTED, stopped.text
        first = (await _ticks(rig, TICK))[0]
        assert first.mode is RunMode.ARRET
        assert first.safety_action is SafetyAction.FREEZE
        assert first.setpoint.motor_rpm < after_zero
        down = await _ticks(rig, eta + 2.0)
        assert _applied(rig) == 0, "the arm never came down"
        assert _never_rises([int(first.setpoint.motor_rpm), *_setpoints(down)])
        assert runtime.end_reason is EndReason.OPERATOR_STOP

        await rig.tick(10.0)
        assert _state(rig) is RuntimeState.FINISHED
        assert RULE_SESSION_STANDSTILL not in {
            snapshot.safety.rule for snapshot in rig.snapshots if snapshot.safety is not None
        }
        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == httpx.codes.OK, acknowledged.text
        assert acknowledged.json()["cleared"] == [FREEZE]
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_stop_sent_from_the_site_under_a_freeze_brings_the_arm_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A programme, the electrode off for twelve seconds (FREEZE ``hr_stale``), the site's stop.

    The dashboard cannot stop the machine by itself: the console polls it, and
    forwards the request to the very mailbox the STOP button writes to. On
    ``develop`` that request was recorded and the arm turned on at the held
    speed until the rule's own REDUCE, eighteen seconds later.
    """
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await attest(session)
        await run.run(10.0)
        run.offer("remote-1")
        await run.run(TURNING_AT + 4.0)
        assert _state(rig) is RuntimeState.RUNNING
        assert runtime.snapshot().measured.motor_rpm > 0, "the arm is not turning yet"

        run.electrodes.off = True
        lost = await run.run(12.0)
        assert lost[-1].safety is not None
        assert (lost[-1].safety.rule, lost[-1].safety.action) == (
            RULE_HR_STALE,
            SafetyAction.FREEZE,
        )
        held = _applied(rig)
        assert held > 55

        run.dashboard.answer(STATUS, ok({"active": True, "stopRequested": True}))
        asked = await run.run(4.0)
        assert runtime.stop_reason == "arret demande depuis le tableau de bord"
        ending = [snapshot for snapshot in asked if snapshot.mode is RunMode.ARRET]
        assert ending, "the stop sent from the site never reached the runtime"
        assert ending[0].safety_action is SafetyAction.FREEZE
        assert ending[0].setpoint.motor_rpm < held, "the stop was recorded and moved nothing"

        down = await run.run(12.0)
        assert _applied(rig) == 0, "the arm did not come down before the rule's own REDUCE"
        walked = [snapshot for snapshot in [*ending, *down] if snapshot.setpoint.motor_rpm != 0]
        assert {snapshot.safety_action for snapshot in walked} == {SafetyAction.FREEZE}
        assert runtime.end_reason is EndReason.OPERATOR_STOP

        # The electrode is refitted on an arm that has stopped; nothing restarts.
        run.electrodes.off = False
        rest = await run.run(80.0)
        assert set(_setpoints(rest)) == {0}
        assert _state(rig) is RuntimeState.FINISHED
        assert run.standing_rule() is None, "an operator stop left something to acknowledge"
    await rig.panel.close()
    assert await rig.left_stopped() == ""
