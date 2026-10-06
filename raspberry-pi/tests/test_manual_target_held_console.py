"""A manual target held over a stopped arm, on the REAL console (ANH-178).

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

What holds the arm is named in both: a verdict, or, with no verdict standing,
the heart rate of the person on board or a drive that did not confirm the
first step (decision of 2026-10-06, ``docs/securite.md`` 7.6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, cast, override

import httpx
import pytest

from src.local_panel import describe_target_refusal, describe_withdrawn_target
from src.motor.drive import BadResponse, DriveError
from src.motor.simulated import SimulatedDrive
from src.result import Err, Result
from src.sim.physiology import Physiology, SubjectState
from src.training.runtime import (
    HeldAtStandstill,
    Holding,
    RiseHold,
    RuntimeState,
    WithdrawnTarget,
)
from src.training.types import SafetyAction, SafetyVerdict
from src.units import Monotonic, MotorRpm
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    TICK,
    Rig,
    Wrapped,
    attest,
    make_rig,
)
from tests.test_standstill_console import Electrodes

TYPED: Final[float] = 4.0
"""The target typed at the console, in output rpm."""

ASKED: Final[int] = 199
"""The same target at the motor shaft (ratio 49.79), which is what the runtime holds."""

HELD: Final[str] = "le verdict rig_freeze tient le bras a l'arret"
ASK_AGAIN: Final[str] = "L'acquitter une fois sa cause levee, puis redonner la cible"

WORDS: Final[dict[RiseHold, str]] = {
    RiseHold.NO_HEART_RATE: (
        "pas de frequence cardiaque utilisable, rien ne monte depuis l'arret. "
        "Attendre une frequence cardiaque fiable"
    ),
    RiseHold.TREND_UNKNOWN: (
        "tendance de la frequence cardiaque pas encore connue, rien ne monte depuis l'arret. "
        "Attendre quelques secondes de lecture"
    ),
    RiseHold.HEART_RATE_FALLING: (
        "la frequence cardiaque baisse trop vite, rien ne monte depuis l'arret. "
        "Attendre qu'elle se stabilise"
    ),
    RiseHold.WRITE_UNACKNOWLEDGED: (
        "le variateur n'a pas confirme la consigne, le bras reste a l'arret. Verifier la liaison"
    ),
}
"""What the operator reads for each hold that is not a verdict: the reason, then what to do."""


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
    taken_back = describe_withdrawn_target(WithdrawnTarget(MotorRpm(200), verdict))
    assert taken_back == (
        "cible de 200 tr/min moteur remise a 0 : le verdict hr_stale tient le bras a l'arret. "
        f"{what_to_do}"
    )
    # The verdict's own sentence is the screen's, not the refusal's: one clause is enough.
    assert verdict.detail not in refused + taken_back


@pytest.mark.parametrize("hold", list(RiseHold), ids=[hold.value for hold in RiseHold])
def test_the_refusal_and_the_withdrawal_name_what_holds_when_it_is_no_verdict(
    hold: RiseHold,
) -> None:
    """The same two lines with no verdict to name: the reason itself, and what to do about it."""
    said = WORDS[hold]
    assert describe_target_refusal(HeldAtStandstill(hold)) == (
        f"consigne refusee : {said}, puis redonner la cible"
    )
    assert describe_withdrawn_target(WithdrawnTarget(target=MotorRpm(200), by=hold)) == (
        f"cible de 200 tr/min moteur remise a 0 : {said}, puis redonner la cible"
    )
    assert "verdict" not in said, "no verdict stands: the operator must not look for one"


def test_every_hold_that_is_no_verdict_has_words_of_its_own() -> None:
    """A reason nobody can tell from another is a reason nobody can act on."""
    assert set(WORDS) == set(RiseHold)
    assert len(set(WORDS.values())) == len(RiseHold)


def test_a_hold_that_is_not_one_fails_loudly() -> None:
    """A hold the runtime gains later, with no words written for it, is an error and not a blank."""
    unknown = cast("Holding", "not-a-hold")
    with pytest.raises(AssertionError):
        describe_target_refusal(HeldAtStandstill(unknown))
    with pytest.raises(AssertionError):
        describe_withdrawn_target(WithdrawnTarget(MotorRpm(200), unknown))


async def _type(session: httpx.AsyncClient, output_rpm: float) -> httpx.Response:
    return await session.post(
        "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
    )


def _refusals(rig: Rig, start: str) -> list[str]:
    return [refusal for refusal in rig.refusals() if refusal.startswith(start)]


def _state(rig: Rig) -> RuntimeState:
    """Read afresh, through a call: a checker would keep a narrowing across an ``await``."""
    return rig.panel.runtime.state


def _setpoints(rig: Rig, first: int) -> set[int]:
    return {int(snapshot.setpoint.motor_rpm) for snapshot in rig.snapshots[first:]}


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
        assert _setpoints(rig, first) == {0}, "the acknowledgement started the arm"
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


def _with_a_rider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Rig, Electrodes]:
    """The console with a person on board: the simulated subject's own rate, once a second.

    The electrodes of ``tests/test_standstill_console.py``, read in place of
    the DSP; every reading still goes through the bridge's confirmation.
    """
    electrodes = Electrodes()
    advance = Physiology.advance

    def watched(self: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        state = advance(self, now, motor_rpm)
        electrodes.state = state
        return state

    monkeypatch.setattr(Physiology, "advance", watched)
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV, treat=electrodes)
    return rig, electrodes


async def test_with_a_rider_the_console_follows_a_first_target_and_refuses_one_without_a_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person on board, at the console: the ordinary start, then a gap in the heart rate.

    The ECG has been read for ten seconds when the session is armed, as on the
    machine. The first target is taken and the arm goes to it: nothing is
    refused in a normal start. The operator stops the arm; an electrode comes
    off for six seconds, which is not yet a warning; a target typed then gets
    202 from the mailbox and one ``refused`` event that says there is no usable
    heart rate and what to do. The electrode is refitted and nobody types
    anything: nothing moves. Typed again, the target is followed. On
    ``da5efa9`` the target typed during the gap was accepted without a word
    and the arm left when the heart rate came back.
    """
    rig, electrodes = _with_a_rider(tmp_path, monkeypatch)
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await rig.tick(10.0)
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await rig.tick(1.0)
        assert _state(rig) is RuntimeState.RUNNING

        # The ordinary start: the first target, taken and followed.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(40.0)
        assert runtime.applied_rpm == ASKED, "the first target of a normal session was not followed"
        assert rig.refusals() == []

        # The operator's own zero, then six seconds without a reading: no verdict yet.
        assert (await _type(session, 0.0)).status_code == httpx.codes.ACCEPTED
        await rig.tick(40.0)
        assert runtime.applied_rpm == 0
        electrodes.off = True
        await rig.tick(6.0)
        assert runtime.standing is None, "a warning stands: this is not the case under test"
        first = len(rig.snapshots)

        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(1.0)
        said = f"consigne refusee : {WORDS[RiseHold.NO_HEART_RATE]}, puis redonner la cible"
        assert _refusals(rig, "consigne refusee") == [said]
        assert runtime.manual_target == 0
        shown = await session.get("/api/snapshot")
        assert shown.json()["manual"]["target"]["motor_rpm"] == 0

        # Refitted. Nobody types anything: nothing moves.
        electrodes.off = False
        await rig.tick(60.0)
        assert runtime.standing is None
        assert {s.safety for s in rig.snapshots[first:]} == {None}, "a verdict stood"
        assert _setpoints(rig, first) == {0}, "the arm left when the heart rate came back"
        assert rig.simulator.commanded_setpoint == 0
        assert _state(rig) is RuntimeState.RUNNING

        # Typed again with nothing holding: followed.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(40.0)
        assert runtime.applied_rpm == ASKED
        assert _refusals(rig, "consigne refusee") == [said]

        await session.post("/api/session/stop", json={"operator": OPERATOR})
    await rig.tick(120.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""


class DeafToARise(Wrapped):
    """A drive that confirms every frame but a non-zero reference, while ``deaf``.

    The keepalive of a stopped arm (a zero reference) and every status read
    are answered, so the link is alive and ``comms_lost`` has no run of
    failures to count: only the first step is not confirmed.
    """

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.deaf: bool = False
        self.refused: int = 0

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        if self.deaf and rpm != 0:
            self.refused += 1
            return Err(BadResponse(detail="under test: the reference was not confirmed"))
        return await self.inner.write_speed(rpm)


async def test_the_console_says_when_the_drive_did_not_confirm_the_first_step(
    tmp_path: Path,
) -> None:
    """A first step the drive does not confirm: the target comes back, with the reason.

    Nothing can refuse this target beforehand, so the mailbox and the loop
    take it. The tick asks the drive once; the drive does not confirm. What
    the page receives: the applied target back at zero and one ``refused``
    event naming the target and the drive. Ten seconds later the drive
    confirms again, and nothing is asked of it, because nobody has typed
    anything. On ``da5efa9`` the target stayed on the screen, the first step
    was asked again on every tick, and the arm left the moment one was
    confirmed.
    """
    rig, wrapper = make_rig(tmp_path, wrap=DeafToARise)
    assert isinstance(wrapper, DeafToARise)
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == httpx.codes.ACCEPTED, started.text
        await rig.tick(1.0)
        assert _state(rig) is RuntimeState.RUNNING

        wrapper.deaf = True
        first = len(rig.snapshots)
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        caught = await rig.tick(TICK)
        assert wrapper.refused == 1, "the first step was not asked of the drive on this tick"
        assert caught.safety is None, "a verdict stood: this is not the case under test"
        assert caught.manual is not None
        assert caught.manual.target.motor_rpm == 0, "the page still shows the target as applied"
        said = (
            f"cible de {ASKED} tr/min moteur remise a 0 : "
            f"{WORDS[RiseHold.WRITE_UNACKNOWLEDGED]}, puis redonner la cible"
        )
        assert _refusals(rig, "cible de") == [said]

        await rig.tick(10.0)
        wrapper.deaf = False
        await rig.tick(30.0)
        assert {s.safety for s in rig.snapshots[first:]} == {None}, "a verdict stood"
        assert _setpoints(rig, first) == {0}, "the arm left when the drive confirmed again"
        assert wrapper.refused == 1, "the first step was asked again with nobody clicking"
        assert rig.simulator.commanded_setpoint == 0
        assert _refusals(rig, "cible de") == [said], "one withdrawal was said twice"

        # Typed again: the operator's own command, and the drive confirms it.
        assert (await _type(session, TYPED)).status_code == httpx.codes.ACCEPTED
        await rig.tick(40.0)
        assert runtime.applied_rpm == ASKED

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
        assert _setpoints(rig, first) == {0}

        await session.post("/api/session/stop", json={"operator": OPERATOR})
    await rig.tick(60.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""
