"""A manual target held by a verdict, on the REAL console (ANH-178).

``tests/test_manual_target_held.py`` proves the rule on the runtime alone. This
file proves what the operator is told: the real composition root
(:func:`src.local_panel.build_panel`) over the simulated drive, spoken to
through the operator's HTTP API, ticked as its loop does (the rig of
``tests/test_failure_rig.py``).

A manual target reaches the runtime one way only: the page posts it, the
mailbox takes it (202), and the loop hands it to
:meth:`~src.training.runtime.TrainingRuntime.set_manual_target` on its next
tick. So the refusal is the loop's, like the refusal of a target out of range:
it comes back to the page as a ``refused`` event carrying the reason, and the
page's "applied target" stays what the snapshot says. A target the runtime
takes back comes the same way, with nobody's name on it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
import pytest

from src.local_panel import describe_target_refusal, describe_withdrawn_target
from src.training.runtime import HeldAtStandstill, RuntimeState, WithdrawnTarget
from src.training.types import SafetyAction, SafetyVerdict
from src.units import Monotonic, MotorRpm
from tests.test_failure_rig import OPERATOR, TICK, Rig, attest, make_rig

TYPED: Final[float] = 4.0
"""The target typed at the console, in output rpm."""

ASKED: Final[int] = 199
"""The same target at the motor shaft (ratio 49.79), which is what the runtime holds."""

HELD: Final[str] = "le verdict rig_freeze tient le bras a l'arret"
ASK_AGAIN: Final[str] = "L'acquitter une fois sa cause levee, puis redonner la cible"


def _verdict(*, latched: bool) -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.FREEZE,
        rule="hr_stale",
        detail="no fresh trustworthy heart rate for 12.0 s",
        latched=latched,
        since=Monotonic(1.0),
    )


@pytest.mark.parametrize(
    ("latched", "what_to_do"),
    [
        pytest.param(False, "Attendre qu'il soit leve, puis redonner la cible", id="a warning"),
        pytest.param(True, ASK_AGAIN, id="a latched verdict"),
    ],
)
def test_the_refusal_and_the_withdrawal_name_the_verdict_and_say_what_to_do(
    *, latched: bool, what_to_do: str
) -> None:
    """Two lines the operator reads: which verdict, and what has to happen before asking again."""
    verdict = _verdict(latched=latched)
    refused = describe_target_refusal(HeldAtStandstill(verdict))
    assert refused == (
        f"consigne refusee : le verdict hr_stale tient le bras a l'arret. {what_to_do}"
    )
    taken_back = describe_withdrawn_target(WithdrawnTarget(target=MotorRpm(200), verdict=verdict))
    assert taken_back == (
        "cible de 200 tr/min moteur remise a 0 : le verdict hr_stale tient le bras a l'arret. "
        f"{what_to_do}"
    )
    # The verdict's own sentence is the screen's, not the refusal's: one clause is enough.
    assert verdict.detail not in refused + taken_back


async def _type(session: httpx.AsyncClient, output_rpm: float) -> httpx.Response:
    return await session.post(
        "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
    )


def _refusals(rig: Rig, start: str) -> list[str]:
    return [refusal for refusal in rig.refusals() if refusal.startswith(start)]


def _state(rig: Rig) -> RuntimeState:
    """Read afresh, through a call: a checker would keep a narrowing across an ``await``."""
    return rig.panel.runtime.state


async def test_the_console_takes_a_waiting_target_back_says_so_and_refuses_the_next(
    tmp_path: Path,
) -> None:
    """Typed with nothing standing, caught by a verdict before the first step; then typed again.

    What the page receives, in order: the applied target back at zero in the
    snapshot, one ``refused`` event saying which target was taken back and by
    which verdict; for the target typed while the verdict stands, 202 from the
    mailbox and then one ``refused`` event naming the verdict; an
    acknowledgement that starts nothing; and a target typed with nothing
    standing, which the arm follows.
    """
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await rig.tick(1.0)
        assert _state(rig) is RuntimeState.RUNNING

        # The target is in the mailbox, nothing stands, and a verdict is on its
        # way to the same tick: the loop hands the target over (accepted), then
        # the tick sees the verdict before the arm has taken its first step.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
        first = len(rig.snapshots)
        caught = await rig.tick(TICK)
        assert caught.safety is not None
        assert caught.safety.rule == "rig_freeze"
        assert caught.manual is not None
        assert caught.manual.target.motor_rpm == 0, "the page still shows the target as applied"
        shown = await session.get("/api/snapshot")
        assert shown.json()["manual"]["target"]["motor_rpm"] == 0
        assert shown.json()["manual"]["ramping"] is False
        said = f"cible de {ASKED} tr/min moteur remise a 0 : {HELD}. {ASK_AGAIN}"
        assert _refusals(rig, "cible de") == [said]

        # Typed again while the verdict stands: the mailbox takes it, the loop refuses it.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(2.0)
        assert _refusals(rig, "consigne refusee") == [f"consigne refusee : {HELD}. {ASK_AGAIN}"]
        assert runtime.manual_target == 0

        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == httpx.codes.OK, acknowledged.text
        assert acknowledged.json()["cleared"] == ["rig_freeze"]
        await rig.tick(15.0)
        assert runtime.standing is None
        assert {int(s.setpoint.motor_rpm) for s in rig.snapshots[first:]} == {0}, (
            "the acknowledgement started the arm"
        )
        assert rig.simulator.commanded_setpoint == 0
        assert _refusals(rig, "cible de") == [said], "one withdrawal was said twice"

        # Typed with nothing standing: the operator's own command, followed.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(40.0)
        assert runtime.applied_rpm == ASKED
        assert len(_refusals(rig, "consigne refusee")) == 1

        await session.post("/api/session/stop", json={"operator": OPERATOR})
    await rig.tick(60.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_manual_start_under_a_latched_verdict_is_refused_at_the_console(
    tmp_path: Path,
) -> None:
    """The other door into the same state: a START cannot stand in for a target.

    A verdict stands over a stopped machine. The start is refused at once with
    the verdict's name. After the acknowledgement it is taken, and the session
    it arms holds a target of zero: nothing turns until a target is typed.
    """
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await attest(session)
        runtime.trip_from_thread("rig_freeze", SafetyAction.FREEZE, "under test")
        await rig.tick(1.0)
        refused = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert refused.status_code == httpx.codes.CONFLICT, refused.text
        assert "rig_freeze" in refused.json()["detail"]
        await rig.tick(1.0)
        assert _state(rig) is RuntimeState.IDLE

        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == httpx.codes.OK, acknowledged.text
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        first = len(rig.snapshots)
        armed = await rig.tick(20.0)
        assert _state(rig) is RuntimeState.RUNNING
        assert armed.manual is not None
        assert armed.manual.target.motor_rpm == 0
        assert {int(s.setpoint.motor_rpm) for s in rig.snapshots[first:]} == {0}

        await session.post("/api/session/stop", json={"operator": OPERATOR})
    await rig.tick(60.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""
