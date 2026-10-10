"""What the REAL console does with a wall clock nobody answers for (ANH-163 EX-1, EX-3).

The composition root (:func:`src.local_panel.build_panel`) over the simulated
drive, spoken to through the operator's HTTP API and ticked as its loop does
(the rig of ``tests/test_failure_rig.py``), with a session journal and a wall
clock whose standing the test decides (``tests/wall_clock_support.py``): "the
network is cut" is a time service that reached no source, which is what the
console reads on a Pi that started without one.

The wall clock dates the records and decides nothing about the machine: no
test here finds a timing, a verdict or a setpoint changed by it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from pydantic import TypeAdapter

from src import local_panel
from src.clock import Clock, ManualClock
from src.local_config import LocalConfig
from src.local_panel import (
    WALL_CLOCK_PERIOD,
    LocalPanel,
    ServiceDesk,
    describe_clock_refusal,
    describe_clock_warning,
    run_console,
)
from src.motor.simulated import SimState
from src.record.journal import Journal
from src.record.reader import read
from src.record.session import SessionRecorder, SessionRequest, manifest_for
from src.result import Ok
from src.sim.physiology import Physiology, SubjectState
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Metres, Monotonic, MotorRpm, Seconds, UnixMillis
from src.wall_clock import Unreadable, WallClockProbe, WallClockState, WallClockWatch
from src.web.schemas import PanelRow, WallClockRow
from tests.record_console_support import RecordedRig, recorded_rig, stop
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    PROGRAMME_ENV,
    attest,
    config_of,
    make_rig,
    start_programme,
)
from tests.test_local_panel import FakeWeb
from tests.test_record_session import STAMP
from tests.test_standstill_console import Electrodes
from tests.wall_clock_support import IN_SYNC, NO_NETWORK, ScriptedClock

PANEL: Final[TypeAdapter[PanelRow]] = TypeAdapter(PanelRow)

CUT: Final[str] = (
    "aucune source de temps jointe : l'heure peut etre fausse de la duree pendant laquelle "
    "le Pi etait eteint"
)
REFUSED: Final[str] = (
    f"demarrage refuse : horloge murale non fiable ({CUT}). Avec une personne a bord, "
    "l'enregistrement de la seance doit etre date par une horloge synchronisee : retablir le "
    "reseau du Pi et attendre que la ligne horloge de la console dise synchronisee"
)
WARNED: Final[str] = (
    f"horloge murale non fiable ({CUT}) : les dates de l'enregistrement de cette seance ne "
    "sont pas sures, et son manifeste le dit. La seance et la securite continuent."
)


def _with_a_rider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, probe: ScriptedClock
) -> RecordedRig:
    """The recording console with a person's heart rate to read, on ``probe``'s clock."""
    electrodes = Electrodes()
    advance = Physiology.advance

    def watched(self: Physiology, now: Monotonic, motor_rpm: MotorRpm) -> SubjectState:
        state = advance(self, now, motor_rpm)
        electrodes.state = state
        return state

    monkeypatch.setattr(Physiology, "advance", watched)
    return recorded_rig(tmp_path, env=PROGRAMME_ENV, treat=electrodes, wall_clock=probe)


async def _clock_row(session: httpx.AsyncClient) -> WallClockRow:
    answered = await session.get("/api/panel")
    assert answered.status_code == httpx.codes.OK, answered.text
    row = PANEL.validate_json(answered.content).wall_clock
    assert row is not None, "this console judges its clock: the page must be told"
    return row


async def _ask(session: httpx.AsyncClient, occupancy: str) -> None:
    asked = await session.post(
        "/api/manual/start", json={"occupancy": occupancy, "operator": OPERATOR}
    )
    assert asked.status_code == httpx.codes.ACCEPTED, asked.text


def _state(recorded: RecordedRig) -> RuntimeState:
    return recorded.rig.panel.runtime.state


def _trusted_in_manifest(record: Path) -> object:
    """``clocks.wall_trusted`` as it is written on the disk, not as a reader fills it in."""
    manifest = cast(
        "dict[str, dict[str, object]]",
        json.loads((record / "manifest.json").read_text(encoding="utf-8")),
    )
    return manifest["clocks"]["wall_trusted"]


# =========================================================================
# EX-3: the network is cut at startup, then comes back
# =========================================================================


async def test_ex3_network_cut_at_startup_then_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of EX-3, on one console.

    Cut: the console starts and serves its page, which says the clock is not
    one to believe; a start with a person on board is refused before the drive
    is touched; a bench session starts, the operator is told, and its manifest
    carries ``wall_trusted = false``. Back: the page says synchronised, the
    same start with a person on board is taken, and its manifest says ``true``.
    """
    probe = ScriptedClock(NO_NETWORK)
    recorded = _with_a_rider(tmp_path, monkeypatch, probe)
    rig = recorded.rig
    async with rig.http() as session:
        # --- The network is cut: the console is up all the same ---
        cut = await _clock_row(session)
        assert (cut.state, cut.trusted, cut.label) == ("unsynchronised", False, "NON FIABLE")
        assert cut.detail == CUT
        assert (cut.estimated_error_s, cut.max_error_s) == (None, None)
        await recorded.tick(10.0)  # the ECG is read, as on the machine before a start
        await attest(session)

        # A person on board: refused, and nothing was asked of the drive.
        await _ask(session, "occupied")
        await recorded.tick(0.4)
        assert rig.refusals() == [REFUSED]
        assert _state(recorded) is RuntimeState.IDLE
        assert not rig.panel.runtime.output_enabled
        assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
        assert recorded.records() == [], "a refused start opens no record"

        # Nobody on board: the session starts, the operator is told, the manifest says so.
        await _ask(session, "bench")
        await recorded.tick(0.4)
        assert _state(recorded) is RuntimeState.RUNNING
        assert recorded.said() == [WARNED]
        bench = recorded.only_record()
        assert _trusted_in_manifest(bench) is False
        await stop(recorded, session)
        assert _state(recorded) is RuntimeState.FINISHED
        assert recorded.recording().manifest.clocks.wall_trusted is False, "kept when closed"

        # --- The network is back: the console's own task reads it ---
        probe.state = IN_SYNC
        stale = await _clock_row(session)
        assert not stale.trusted, "nothing changes until the console asks again"
        await rig.panel.clock_step()
        back = await _clock_row(session)
        assert (back.state, back.trusted, back.label) == ("synchronised", True, "synchronisee")
        assert back.detail == "erreur estimee 0.4 ms, erreur au plus 45.0 ms"
        assert (back.estimated_error_s, back.max_error_s) == (0.0004, 0.045)

        await _ask(session, "occupied")
        await recorded.tick(0.4)
        assert _state(recorded) is RuntimeState.RUNNING, rig.refusals()
        assert rig.refusals() == [REFUSED], "no second refusal"
        assert recorded.said() == [WARNED], "and nothing to warn of on a clock that is kept"
        occupied = recorded.records()[-1]
        assert occupied != bench
        assert _trusted_in_manifest(occupied) is True
        loaded = read(occupied)
        assert isinstance(loaded, Ok)
        assert loaded.value.manifest.occupancy == "occupied"
    await rig.panel.close()


async def test_ex3_a_programme_is_refused_on_a_clock_nobody_answers_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A programme has a person on board by definition: the same refusal, the same words."""
    probe = ScriptedClock(NO_NETWORK)
    recorded = _with_a_rider(tmp_path, monkeypatch, probe)
    rig = recorded.rig
    async with rig.http() as session:
        await recorded.tick(10.0)
        await start_programme(session)
        await recorded.tick(0.4)
        assert rig.refusals() == [REFUSED]
        assert _state(recorded) is RuntimeState.IDLE
        assert recorded.records() == []

        probe.state = IN_SYNC
        await rig.panel.clock_step()
        await start_programme(session)
        await recorded.tick(0.4)
        assert _state(recorded) is RuntimeState.RUNNING, rig.refusals()
    await rig.panel.close()


async def test_ex3_a_clock_that_cannot_be_read_is_treated_as_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that breaks stops nothing: the console runs, warns at the bench, refuses a rider."""
    probe = ScriptedClock(IN_SYNC, fault=RuntimeError("injected: the probe broke"))
    recorded = _with_a_rider(tmp_path, monkeypatch, probe)
    rig = recorded.rig
    unreadable = Unreadable("lecture en echec : RuntimeError")
    async with rig.http() as session:
        row = await _clock_row(session)
        assert (row.state, row.trusted, row.label) == ("unreadable", False, "NON VERIFIEE")
        await rig.panel.clock_step()  # raises nothing, and the console goes on ticking
        await recorded.tick(10.0)
        await attest(session)
        await _ask(session, "occupied")
        await recorded.tick(0.4)
        assert rig.refusals() == [describe_clock_refusal(unreadable)]
        await _ask(session, "bench")
        await recorded.tick(0.4)
        assert _state(recorded) is RuntimeState.RUNNING
        assert recorded.said() == [describe_clock_warning(unreadable)]
        assert _trusted_in_manifest(recorded.only_record()) is False
    await rig.panel.close()


async def test_the_occupied_lock_of_the_configuration_speaks_before_the_clock(
    tmp_path: Path,
) -> None:
    """With ``OCCUPANCY_OCCUPIED_ENABLED=false`` (the default) the configuration refuses first:
    the clock adds a refusal to a mode that is open, it opens nothing."""
    probe = ScriptedClock(NO_NETWORK)
    rig, _ = make_rig(tmp_path, env=BENCH_ENV, wall_clock=probe)
    async with rig.http() as session:
        await attest(session)
        asked = await session.post(
            "/api/manual/start", json={"occupancy": "occupied", "operator": OPERATOR}
        )
        await rig.tick(0.4)
    refused = [asked.text, *rig.refusals()]
    assert not any("horloge" in said for said in refused), refused
    assert any("OCCUPANCY_OCCUPIED_ENABLED" in said for said in refused), refused
    assert rig.panel.runtime.state is RuntimeState.IDLE
    await rig.panel.close()


async def test_a_console_built_without_a_probe_judges_nothing(tmp_path: Path) -> None:
    """What a test that is not about the clock gets: no line on the page, nothing warned,
    and a manifest that says ``null`` rather than an answer nobody gave."""
    recorded = recorded_rig(tmp_path)
    rig = recorded.rig
    assert rig.panel.wall_clock is None
    await rig.panel.clock_step()  # nothing to ask
    async with rig.http() as session:
        answered = await session.get("/api/panel")
        row = PANEL.validate_json(answered.content)
        assert row.wall_clock is None
        await attest(session)
        await _ask(session, "bench")
        await recorded.tick(0.4)
    assert recorded.said() == []
    assert _trusted_in_manifest(recorded.only_record()) is None
    await rig.panel.close()


# =========================================================================
# The record is opened whatever the clock's state does
# =========================================================================


def _recorder(tmp_path: Path, wall_clock: ScriptedClock) -> tuple[SessionRecorder, Journal]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    recorder = SessionRecorder(
        clock=clock,
        journal=journal,
        stamp=STAMP,
        radius=Metres(1.5),
        leg_tip=Metres(2.43),
        wall_clock=wall_clock.read,
    )
    return recorder, journal


def test_a_clock_state_that_raises_costs_no_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The recorder's own fence, behind the watch's: the record is opened and says ``false``."""
    source = ScriptedClock(IN_SYNC, fault=RuntimeError("injected: the state cannot be read"))
    recorder, journal = _recorder(tmp_path, source)
    with caplog.at_level("ERROR", logger="src.record.session"):
        recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    assert recorder.recording
    assert not recorder.degraded, "the record itself is whole: only its clock is not vouched for"
    (record,) = [
        path for path in journal.root.iterdir() if path.is_dir() and path.name[0].isdigit()
    ]
    assert _trusted_in_manifest(record) is False
    assert "the wall clock state could not be read" in caplog.text


@pytest.mark.parametrize(
    ("state", "written"), [(IN_SYNC, True), (NO_NETWORK, False), (Unreadable("x"), False)]
)
def test_the_manifest_says_what_the_clock_was_worth_when_the_record_opened(
    tmp_path: Path, state: WallClockState, *, written: bool
) -> None:
    recorder, journal = _recorder(tmp_path, ScriptedClock(state))
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    (record,) = [
        path for path in journal.root.iterdir() if path.is_dir() and path.name[0].isdigit()
    ]
    assert _trusted_in_manifest(record) is written


def test_the_console_never_writes_an_offset_it_did_not_read() -> None:
    """``ntp_offset_s`` stays ``null`` (unknown, never zero), trusted clock or not."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    request = SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR)
    trusted = manifest_for(STAMP, request, clock, wall_trusted=True)
    unjudged = manifest_for(STAMP, request, clock)
    assert (trusted.clocks.wall_trusted, trusted.clocks.ntp_offset_s) == (True, None)
    assert (unjudged.clocks.wall_trusted, unjudged.clocks.ntp_offset_s) == (None, None)


# =========================================================================
# Production: the kernel is asked, by a task that is not the control tick
# =========================================================================


def _web(_services: object, _config: object) -> FakeWeb:
    """In place of the production web runner: binds nothing."""
    return FakeWeb()


async def test_the_production_console_asks_the_kernel_through_its_own_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_console`` builds the console on the system's probe, and its loop refreshes it."""
    probe = ScriptedClock(NO_NETWORK)
    monkeypatch.setattr(local_panel, "system_probe", lambda: probe)
    monkeypatch.setattr(local_panel, "UvicornRunner", _web)
    stop_event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, stop_event.set)
    env = {**BENCH_ENV, "RECORD_ROOT": str(tmp_path / "records")}
    code = await run_console(config_of(env), stop=stop_event)
    assert code == local_panel.EXIT_OK
    assert probe.reads == 2, "once when the console was built, once by the loop's own task"
    assert Seconds(10.0) == WALL_CLOCK_PERIOD, "then every ten seconds"


async def test_on_a_system_that_cannot_be_asked_the_production_console_says_so_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Anything but Linux (the macOS workstation of the bench): the console judges nothing,
    and says it once in its log instead of refusing every start with a person on board."""
    built: list[LocalPanel] = []
    real = local_panel.build_panel

    def remembered(
        config: LocalConfig,
        *,
        clock: Clock,
        journal: Journal,
        wall_clock: WallClockProbe | None,
        notify_socket: str | None,
        service: ServiceDesk,
    ) -> LocalPanel:
        panel = real(
            config,
            clock=clock,
            journal=journal,
            wall_clock=wall_clock,
            notify_socket=notify_socket,
            service=service,
        )
        built.append(panel)
        return panel

    monkeypatch.setattr(local_panel, "system_probe", lambda: None)
    monkeypatch.setattr(local_panel, "build_panel", remembered)
    monkeypatch.setattr(local_panel, "UvicornRunner", _web)
    stop_event = asyncio.Event()
    stop_event.set()
    env = {**BENCH_ENV, "RECORD_ROOT": str(tmp_path / "records")}
    with caplog.at_level("WARNING", logger="src.local_panel"):
        code = await run_console(config_of(env), stop=stop_event)
    assert code == local_panel.EXIT_OK
    assert built[0].wall_clock is None
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith("wall clock:")]
    assert len(said) == 1
    assert "not judged on this system" in said[0]


async def test_a_control_tick_never_reaches_the_probe(tmp_path: Path) -> None:
    """Ticks, a start, a session: the probe is read once at build and never by the tick."""
    probe = ScriptedClock(NO_NETWORK)
    rig, _ = make_rig(tmp_path, env=BENCH_ENV, wall_clock=probe)
    watch = rig.panel.wall_clock
    assert isinstance(watch, WallClockWatch)
    async with rig.http() as session:
        await attest(session)
        await _ask(session, "bench")
        await rig.tick(3.0)
        await session.get("/api/panel")
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    assert probe.reads == 1
    await rig.panel.close()
