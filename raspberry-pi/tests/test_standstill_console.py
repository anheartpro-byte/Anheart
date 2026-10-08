"""The detached electrode on the REAL console: nothing restarts, nothing starts (ANH-176).

``tests/test_runtime_standstill.py`` proves the rule on the runtime alone. This
file proves what follows from it at the machine: the real composition root
(:func:`src.local_panel.build_panel`) over the simulated drive and the
simulated BITalino, spoken to through the operator's HTTP API, with a scripted
dashboard on the link (the harness of ``tests/test_cloud_sync.py``).

One scenario, end to end. A programme is running; the heart rate is lost for
50 s; the arm is held, lowered, and stops; the heart rate comes back. Then:

* the arm does not move again, whoever waits;
* the status page shows the ending as the standing verdict AND as the latched
  floor, like any other latched end;
* a START typed at the console is refused at once (409), with the reason in
  the answer;
* a launch waiting on the dashboard is refused and goes back as a failed
  session, naming the verdict to acknowledge;
* a named acknowledgement at the console clears it, and only then does a
  dashboard launch run.

The second and third points fail on the first version of this change (head
``91b0e90``), where the ending was a latch of the runtime's that the status
page and the start gate did not read: the floor showed "none" under a latched
verdict, and a START was answered 202 before the loop refused it.

The heart rate is the simulated subject's own, read once a second in place of
the DSP (as ``tests/test_failure_ecg.py`` does for link-level cases), and every
reading still goes through the bridge's independent confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from src.bitalino_client import SampleBatch
from src.ecg_pipeline import EcgFrame, EcgMetrics, Treatment
from src.sim.physiology import Physiology, SubjectState
from src.training.plan import JsonValue
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_HR_STALE, RULE_SESSION_STANDSTILL, SELF_CLEARING
from src.training.types import RunMode, SafetyAction, SignalQuality, TelemetrySnapshot
from src.units import Monotonic, MotorRpm
from tests.test_cloud_sync import LAUNCH, Dashboard, Reply, launch_answer
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    TICK,
    Rig,
    make_rig,
    start_programme,
)

LINKED_ENV: Final[Mapping[str, str]] = {
    **PROGRAMME_ENV,
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}
"""The failure rig's programme console, with a dashboard link (never a real network)."""

POLL: Final[str] = "/api/machine/training/poll"
END: Final[str] = "/api/machine/training/end"
START: Final[str] = "/api/machine/training/start"

TURNING_AT: Final[float] = 80.0
"""Seconds into ``failure_short`` at which WARMUP has the arm turning."""

BATCHES_PER_READING: Final[int] = 5
"""One fresh reading a second: five 200 ms batches."""


@dataclass
class Electrodes:
    """The treat function: the subject's true rate once a second, or nothing while ``off``.

    Mutable, owned by the one test. ``off`` is the detached electrode: frames
    keep arriving, no reading is new, and the sequence number does not move.
    """

    state: SubjectState | None = None
    off: bool = False
    batches: int = 0
    seq: int = 0

    async def __call__(self, _treatment: Treatment, _batch: SampleBatch) -> EcgFrame | None:
        self.batches += 1
        state = self.state
        if self.off or state is None or self.batches % BATCHES_PER_READING != 0:
            return EcgFrame(
                millivolts=(),
                metrics=EcgMetrics(seq=self.seq, quality=SignalQuality.GOOD, bpm=None),
            )
        self.seq += 1
        return EcgFrame(
            millivolts=(),
            metrics=EcgMetrics(seq=self.seq, quality=SignalQuality.GOOD, bpm=state.heart_rate),
        )


@dataclass
class Console:
    rig: Rig
    electrodes: Electrodes
    dashboard: Dashboard

    async def run(self, seconds: float) -> list[TelemetrySnapshot]:
        """Tick the console as its loop does, the dashboard link once a second."""
        seen: list[TelemetrySnapshot] = []
        for tick in range(round(seconds / TICK)):
            seen.append(await self.rig.tick(TICK))
            if tick % 5 == 4:
                await self.rig.panel.cloud_step()
        return seen

    def state(self) -> RuntimeState:
        """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
        return self.rig.panel.runtime.state

    def standing_rule(self) -> str | None:
        verdict = self.rig.panel.runtime.standing
        return None if verdict is None else verdict.rule

    def offer(self, session_id: str, profile_id: str = SHORT_PROFILE) -> None:
        """Put ONE launch on the dashboard: the next poll gets it, later polls get nothing."""
        launch: dict[str, JsonValue] = {
            **LAUNCH,
            "sessionId": session_id,
            "profileId": profile_id,
        }
        pending = [launch]

        def poll(_body: Mapping[str, object]) -> Reply:
            return launch_answer(pending.pop() if pending else None)

        self.dashboard.handlers[POLL] = poll

    def ended(self, session_id: str) -> list[Mapping[str, object]]:
        return [body for body in self.dashboard.to(END) if body["sessionId"] == session_id]

    def confirmed(self) -> list[object]:
        """The launches whose start was confirmed to the dashboard, in order.

        A confirmation also carries the start the console dated and the age of
        the session: only which launch it confirms is of interest here.
        """
        return [body["sessionId"] for body in self.dashboard.to(START)]


def console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str] = LINKED_ENV
) -> Console:
    """The linked console on the short programme, the heart rate read from the subject.

    ``env`` is the console's configuration when a test needs another one than
    the linked programme console (a lower bench ceiling, say). A profile store
    written at ``tmp_path / "profiles.json"`` before this call is the one the
    console loads, in place of the failure rig's.
    """
    electrodes = Electrodes()
    original_advance = Physiology.advance

    def advance(self: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        state = original_advance(self, now, motor_rpm)
        electrodes.state = state
        return state

    monkeypatch.setattr(Physiology, "advance", advance)
    dashboard = Dashboard()
    rig, _ = make_rig(tmp_path, env=env, treat=electrodes, transport=lambda _config: dashboard)
    return Console(rig=rig, electrodes=electrodes, dashboard=dashboard)


def _moved(snapshots: list[TelemetrySnapshot]) -> list[tuple[float, int]]:
    return [
        (round(float(snapshot.elapsed), 1), int(snapshot.setpoint.motor_rpm))
        for snapshot in snapshots
        if snapshot.setpoint.motor_rpm != 0
    ]


async def test_after_a_standstill_the_console_restarts_nothing_and_refuses_every_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The electrode scenario at the machine, through its HTTP API and its dashboard link."""
    run = console(tmp_path, monkeypatch)
    rig = run.rig
    runtime = rig.panel.runtime
    async with rig.http() as session:
        await run.run(10.0)
        await start_programme(session)
        await run.run(TURNING_AT)
        assert run.state() is RuntimeState.RUNNING
        assert runtime.snapshot().measured.motor_rpm > 0, "the arm is not turning yet"
        assert runtime.snapshot().live_bpm is not None

        # The electrode comes off: 50 s without a fresh heart rate.
        run.electrodes.off = True
        lost = await run.run(50.0)
        shown = {s.safety.rule: s.safety.action for s in lost if s.safety is not None}
        assert shown[RULE_HR_STALE] is SafetyAction.REDUCE, "the warning never lowered the speed"
        assert runtime.applied_rpm == 0, "the REDUCE never reached standstill"
        assert run.standing_rule() == RULE_SESSION_STANDSTILL
        assert runtime.end_reason is EndReason.SAFETY_VERDICT

        # It is refitted, the heart rate is back, and nobody clicks.
        run.electrodes.off = False
        back = await run.run(90.0)
        assert all(s.live_bpm is not None for s in back[-25:]), "no heart rate came back"
        assert not _moved(back), f"the arm restarted by itself: {_moved(back)[:3]}"
        assert rig.simulator.commanded_setpoint == 0
        assert abs(back[-1].measured.motor_rpm) < 1
        assert run.state() is RuntimeState.FINISHED
        assert back[-1].mode is RunMode.REPOS
        assert run.standing_rule() == RULE_SESSION_STANDSTILL
        latest = await session.get("/api/snapshot")
        assert latest.json()["safety"]["rule"] == RULE_SESSION_STANDSTILL
        assert latest.json()["safety"]["latched"] is True
        assert latest.json()["mode"] == "repos"

        # The status page: the standing verdict and the latched floor agree, and
        # nothing on it says that anything lifts by itself.
        status = await session.get("/api/status")
        assert status.status_code == 200, status.text
        for field in ("standing", "floor"):
            assert status.json()[field]["rule"] == RULE_SESSION_STANDSTILL, field
            assert status.json()[field]["latched"] is True
            assert status.json()[field]["action"] == "ramp_down"
            assert f"the warning {RULE_HR_STALE}" in status.json()[field]["detail"]
        assert SELF_CLEARING not in status.text

        # A START typed at the console: refused at once, with the reason.
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
        assert RULE_SESSION_STANDSTILL in typed.json()["detail"]
        assert f"the warning {RULE_HR_STALE}" in typed.json()["detail"]
        assert "acknowledged by name" in typed.json()["detail"]
        manual = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert manual.status_code == 409, manual.text
        assert RULE_SESSION_STANDSTILL in manual.json()["detail"]
        after_start = await run.run(2.0)
        assert run.state() is RuntimeState.FINISHED
        assert not _moved(after_start)

        # A launch waiting on the dashboard: refused, and sent back failed,
        # naming the verdict somebody has to acknowledge at the console.
        run.offer("remote-1")
        polled = await run.run(8.0)
        assert run.state() is RuntimeState.FINISHED
        assert not _moved(polled)
        assert run.dashboard.to(START) == []
        refused = run.ended("remote-1")
        assert len(refused) == 1, run.dashboard.to(END)
        assert refused[0]["failed"] is True
        reason = refused[0]["reason"]
        assert isinstance(reason, str)
        assert "refusee par la machine" in reason
        assert RULE_SESSION_STANDSTILL in reason
        assert "a acquitter a la console" in reason

        # Somebody at the machine acknowledges, by name.
        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["cleared"] == [RULE_SESSION_STANDSTILL]
        assert run.standing_rule() is None

        # Only now does a dashboard launch run.
        run.offer("remote-2")
        await run.run(8.0)
        assert run.state() is RuntimeState.RUNNING
        # Confirmed once, with the start the console dated and the age of the session.
        (confirmed,) = run.dashboard.to(START)
        assert confirmed["sessionId"] == "remote-2"
        assert set(confirmed) == {"sessionId", "startedAt", "sessionAgeMs"}
        assert run.ended("remote-2") == []
    await rig.panel.close()
    assert await rig.left_stopped() == ""
