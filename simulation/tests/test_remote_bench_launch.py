"""ANH-250 acceptance: a ballasted bench session launched from the site, end to end.

The REAL console (:func:`src.local_panel.build_panel`, ``MOTOR_BACKEND=sim``,
``ECG_SOURCE=sim``, its session journal) linked to a dashboard in memory that
offers a bench launch of contract 1.9: the console offers it, refuses the
operator's confirmation without the bench attestation, confirms it with it,
arms a MANUAL bench session on the simulated drive, the operator sets a
target at the console, the dashboard asks for a stop, the session ends on the
ramp, and the record's manifest carries the ballast and its events the
attestation. Software evidence only: nothing here is a physical acceptance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, override

from tests.fake_dashboard import FakeDashboard, ServerSession

from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.cloud_sync import CloudError, Document
from src.contract import SERVER_VERSION_FIELD
from src.ecg_pipeline import EcgFrame, Treatment, treat_ecg
from src.local_config import load_local_config
from src.local_panel import LocalPanel, build_panel
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
from src.record.schema import RecordedBallast
from src.record.session import BENCH_ATTESTED, operator_alias
from src.remote_launch import ConfirmationCode, LaunchMode
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.runtime import EndReason, RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, Seconds, UnixMillis

OPERATOR: Final[str] = "sim operator"
TICK: Final[Seconds] = Seconds(0.2)
POLL: Final[str] = "/api/machine/training/poll"
START: Final[str] = "/api/machine/training/start"
STATUS: Final[str] = "/api/machine/training/status"

ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

BENCH: Final[Mapping[str, JsonValue]] = {
    "sessionId": "remote-bench",
    "mode": "bench",
    "occupancy": "ballast",
    "ballastKg": 80,
    "notes": "sac de sable de 80 kg sangle dans la capsule",
    "operatorName": "Gestionnaire du centre",
}


@dataclass
class BenchDashboard(FakeDashboard):
    """The dashboard in memory, with the bench launch of the site waiting for this machine.

    It offers the launch until the machine confirms its start, then holds the
    session as Convex does (``training.markTrainingStarted``), and asks for its
    stop once ``stop`` is set (``training.requestStop``).
    """

    offered: bool = True
    stop: bool = False
    starts: list[str] = field(default_factory=list[str])

    @override
    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        if path == POLL:
            self.paths.append(path)
            launch = dict(BENCH) if self.offered else None
            return Ok({"session": launch, SERVER_VERSION_FIELD: self.server_version})
        answered = await super().get(path, params)
        if path == STATUS and isinstance(answered, Ok):
            return Ok({**answered.value, "stopRequested": self.stop})
        return answered

    @override
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        if path != START:
            return await super().post(path, body)
        self.paths.append(path)
        session_id = str(body["sessionId"])
        self.starts.append(session_id)
        self.offered = False
        self.sessions[session_id] = ServerSession(
            session_id=session_id,
            local_ref=session_id,
            started_at=self.now(),
            machine_started_at=None,
        )
        return Ok({"success": True})


async def console_inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    return treat_ecg(treatment, batch)


async def ticks(panel: LocalPanel, journal: Journal, clock: ManualClock, seconds: float) -> None:
    """The console's tasks, at their own rates: the control loop, the stop watch at each
    tick, the riders' list, the dashboard step and the sending once a second."""
    for index in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        await panel.control_step()
        await panel.cloud_stop_step()
        if index % 5 == 0:
            await panel.roster_step()
            await panel.cloud_step()
            await panel.records_step()
        journal.drain()


def _state(panel: LocalPanel) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return panel.runtime.state


async def _the_bench_launch(tmp_path: Path) -> None:
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok), loaded
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    dashboard = BenchDashboard(now=lambda: int(clock.unix_millis()))
    panel = build_panel(
        loaded.value,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=console_inline,
        journal=journal,
        transport=lambda _config: dashboard,
    )
    surface = panel.surface
    cloud = panel.cloud
    assert cloud is not None
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)

    # The site's launch reaches the console, nobody on board.
    await ticks(panel, journal, clock, 1.0)
    offer = cloud.current_remote_launch()
    assert offer is not None
    assert (offer.mode, offer.passenger_name, offer.ballast_kg) == (LaunchMode.BENCH, None, 80.0)

    # Physically confirmed at the console: without the attestation, nothing is sent.
    refused = cloud.confirm_remote_launch("remote-bench", OPERATOR)
    assert isinstance(refused, Err)
    assert refused.error.code is ConfirmationCode.BENCH_UNATTESTED
    await ticks(panel, journal, clock, 2.0)
    assert dashboard.starts == []
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    surface.note_launch_confirmed(OPERATOR)
    await ticks(panel, journal, clock, 2.0)
    assert dashboard.starts == ["remote-bench"]
    assert _state(panel) is RuntimeState.RUNNING, surface.run_state
    manual = panel.runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    assert manual.ceiling == loaded.value.motor_max_rpm

    # The speed is set at the console; the drive follows at the motion limits.
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR), Ok
    )
    await ticks(panel, journal, clock, 20.0)
    assert panel.runtime.snapshot().measured.motor_rpm > 0

    # The site asks for the stop: the session ends on the ramp, the end reaches it.
    dashboard.stop = True
    await ticks(panel, journal, clock, 60.0)
    assert _state(panel) is RuntimeState.FINISHED
    assert panel.runtime.end_reason is EndReason.OPERATOR_STOP
    assert panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    snapshot = panel.runtime.snapshot()
    assert (snapshot.setpoint.motor_rpm, snapshot.measured.motor_rpm) == (0, 0)
    await ticks(panel, journal, clock, 5.0)
    assert dashboard.sessions["remote-bench"].status == "completed"
    await panel.close()

    # The record: a manual bench session, its ballast, and the attestation under an alias.
    found = records(journal.root)
    assert len(found) == 1
    recording = read(found[0])
    assert isinstance(recording, Ok), recording
    manifest = recording.value.manifest
    assert (manifest.kind, manifest.occupancy, manifest.subject_id) == ("manual", "bench", None)
    assert manifest.ballast == RecordedBallast(mass_kg=80.0)
    attested = [event for event in recording.value.events if event.detail == BENCH_ATTESTED]
    assert [event.actor for event in attested] == [operator_alias(OPERATOR)]
    written = b"\n".join(path.read_bytes() for path in found[0].rglob("*") if path.is_file())
    # The launcher's note is shown at the console and kept nowhere else.
    assert str(BENCH["notes"]).encode() not in written


def test_the_site_launches_a_ballasted_bench_session_the_console_confirms_runs_and_stops(
    tmp_path: Path,
) -> None:
    asyncio.run(_the_bench_launch(tmp_path))
