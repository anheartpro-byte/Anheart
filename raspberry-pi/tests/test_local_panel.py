"""The local console's composition root: every branch of the wiring and the exits.

No real device is opened anywhere here. The serial and RFCOMM builders are
constructed (construction is inert by contract: no port, no thread, no radio)
and never connected; the drive and BITalino that actually run are simulators.
The one socket bound is a loopback uvicorn port for the production web runner,
told to exit before it serves a single request.
"""

from __future__ import annotations

import asyncio
import os
import runpy
import signal
import stat
import sys
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest

from src import local_panel
from src.bitalino_client import BITalinoClient, LinkStats, SampleBatch
from src.clock import Clock, ManualClock
from src.control_surface import ControlSurface, RunState, StartSession
from src.ecg_pipeline import EcgBridge, EcgBridgeStats
from src.local_config import EcgSource, LocalConfig, MotorBackend, load_local_config
from src.local_panel import (
    ECG_RETRY,
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_OK,
    DriveSide,
    EcgLink,
    LocalPanel,
    UvicornRunner,
    build_drive,
    build_ecg_client,
    build_panel,
    describe_config,
    describe_reset_refusal,
    describe_start_refusal,
    describe_target_refusal,
    install_stop_signals,
    load_environment,
    main,
    run_console,
)
from src.motor.atv320 import ATV320Drive, FtdiModbusClient
from src.motor.drive import DriveFault, DriveState, LowSpeedNotZero, describe_fault
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.journal import Journal
from src.result import Ok
from src.sim.bitalino import SimulatedBitalinoClient
from src.telemetry import TelemetryClient
from src.training.runtime import (
    AlreadyStarted,
    DriveInFault,
    DriveParameterRefused,
    DrivePrecommanded,
    DriveUnavailable,
    FaultResetRefusal,
    LimitsMismatch,
    ManualEnding,
    ManualTargetRefusal,
    NoFaultToReset,
    NoManualSession,
    NotAttested,
    PlanUnusable,
    ResetBehindVerdict,
    ResetUndelivered,
    ResetWhileCommanded,
    RuntimeState,
    SafetyStanding,
    ShaftStillTurning,
    StartRefusal,
    TargetOutOfRange,
    TrainingRuntime,
)
from src.training.types import Occupancy, Phase, SafetyAction, SafetyVerdict, TelemetrySnapshot
from src.units import Bpm, Hertz, Monotonic, MotorRpm, OutputRpm, RawRegister, Seconds
from src.web.deps import Services
from tests.test_ecg_pipeline import FakeTreatment
from tests.test_runtime import FakeDrive
from tests.test_web_api import SERVE_TEST_PORT

BASE_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
}

SERIAL_ENV: Final[Mapping[str, str]] = {
    **BASE_ENV,
    "MOTOR_BACKEND": "serial",
    "MOTOR_PORT": "ftdi://schneider:rs485/1",
    "MOTOR_SLAVE_ID": "248",
    "ECG_SOURCE": "rfcomm",
    "BITALINO_ADDRESS": "rfcomm:98-d3-91-fe-4e-9f",
}

TICK: Final[Seconds] = Seconds(0.2)


def config_from(env: Mapping[str, str]) -> LocalConfig:
    loaded = load_local_config(env)
    assert isinstance(loaded, Ok), loaded
    return loaded.value


def sim_panel(tmp_path: Path, clock: ManualClock | None = None) -> tuple[LocalPanel, ManualClock]:
    used = ManualClock() if clock is None else clock
    panel = build_panel(config_from(BASE_ENV), clock=used, profiles_path=tmp_path / "p.json")
    return panel, used


# =========================================================================
# Building
# =========================================================================


def test_the_simulated_drive_is_built_like_the_hardware_for_idle_reads() -> None:
    side = build_drive(config_from(BASE_ENV), ManualClock())
    assert isinstance(side.backend, SimulatedDrive)
    assert side.simulator is side.backend
    side.release()  # nothing to release; must not raise


def test_the_serial_drive_is_the_atv320_and_its_release_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built inert (no port opened); release closes the transport, never the stop sequence."""
    side = build_drive(config_from(SERIAL_ENV), ManualClock())
    assert isinstance(side.backend, ATV320Drive)
    assert side.simulator is None
    side.release()  # the master was never connected: closing it is a no-op

    def explode(_self: object) -> None:
        raise OSError("the USB interface is gone")

    monkeypatch.setattr(FtdiModbusClient, "close", explode)
    side.release()  # swallowed: the process is exiting anyway


def test_a_serial_backend_without_a_link_is_refused() -> None:
    config = replace(config_from(SERIAL_ENV), drive_link=None)
    with pytest.raises(ValueError, match="drive link"):
        build_drive(config, ManualClock())


def test_the_ecg_client_follows_the_source() -> None:
    clock = ManualClock()
    simulated = build_ecg_client(config_from(BASE_ENV), clock)
    assert isinstance(simulated.client, SimulatedBitalinoClient)
    assert simulated.simulator is simulated.client
    assert simulated.link_stats is None

    rfcomm = build_ecg_client(config_from(SERIAL_ENV), clock)
    assert isinstance(rfcomm.client, BITalinoClient)
    assert not rfcomm.client.auto_pair
    assert rfcomm.client.channels == (0,)
    assert rfcomm.link_stats is not None
    assert rfcomm.link_stats() == LinkStats()

    serial = build_ecg_client(
        config_from({**BASE_ENV, "ECG_SOURCE": "serial", "BITALINO_ADDRESS": "/dev/rfcomm0"}),
        clock,
    )
    assert isinstance(serial.client, BITalinoClient)
    assert serial.client.auto_pair


def test_a_real_ecg_source_without_an_address_is_refused() -> None:
    config = replace(config_from(SERIAL_ENV), bitalino_address=None)
    with pytest.raises(ValueError, match="BITALINO_ADDRESS"):
        build_ecg_client(config, ManualClock())


def test_unusable_profile_defaults_refuse_the_console(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="profile store"):
        build_panel(
            config_from(BASE_ENV),
            clock=ManualClock(),
            profiles_path=tmp_path / "absent.json",
            defaults_path=tmp_path / "no-defaults.json",
        )


def test_the_serial_console_builds_without_opening_anything(tmp_path: Path) -> None:
    panel = build_panel(
        config_from(SERIAL_ENV), clock=ManualClock(), profiles_path=tmp_path / "p.json"
    )
    status = panel.reporter.panel_status()
    assert status.motor_backend is MotorBackend.SERIAL
    assert status.drive_link is not None
    assert "248" in status.drive_link
    assert status.ecg.source is EcgSource.RFCOMM
    assert status.ecg.link == LinkStats()
    assert not status.ecg.connected


def test_describe_config_names_the_link_or_the_simulator() -> None:
    assert "simulateur" in describe_config(config_from(BASE_ENV))
    banner = describe_config(config_from(SERIAL_ENV))
    assert "ftdi://schneider:rs485/1" in banner
    assert "rfcomm:98-d3-91-fe-4e-9f" in banner
    assert "MANUEL BANC (plafond 300 tr/min moteur; paliers 148/158 bpm)" in banner
    assert "tableau de bord: aucun" in banner
    linked = describe_config(
        config_from(
            {
                **BASE_ENV,
                "PROGRAMS_ENABLED": "true",
                "MACHINE_API_KEY": "k",
                "CONVEX_URL": "https://x.convex.site",
            }
        )
    )
    assert "MANUEL + PROGRAMMES" in linked
    assert "tableau de bord: https://x.convex.site" in linked


# =========================================================================
# The ECG link
# =========================================================================


class FakeEcgClient:
    """A scripted acquisition client."""

    def __init__(self, *, connect_ok: bool = True, start_ok: bool = True) -> None:
        self.is_connected: bool = False
        self.is_acquiring: bool = False
        self.connect_ok: bool = connect_ok
        self.start_ok: bool = start_ok
        self.connects: int = 0
        self.reads: int = 0
        self.disconnects: int = 0

    async def connect(self, timeout: float = 30.0) -> bool:  # noqa: ASYNC109
        del timeout
        self.connects += 1
        self.is_connected = self.connect_ok
        return self.connect_ok

    async def start_acquisition(self) -> bool:
        self.is_acquiring = self.start_ok
        return self.start_ok

    async def read_samples(self, count: int = 1000) -> SampleBatch | None:
        del count
        self.reads += 1
        return None

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.is_connected = False
        self.is_acquiring = False


class NullRuntime:
    def observe_ecg(self, now: Monotonic, seq: int, quality: object, heart_rate: object) -> object:
        return (now, seq, quality, heart_rate)


class NullRing:
    def record_ecg(self, values: Sequence[float]) -> int:
        return len(values)


def ecg_link(
    client: FakeEcgClient, clock: ManualClock, *, stats: LinkStats | None = None
) -> EcgLink:
    bridge = EcgBridge(
        clock=clock,
        source=client,
        treatment=FakeTreatment((), {}, seq=0),
        heart_rate=NullRuntime(),
        waveform=NullRing(),
        sample_rate=1000,
    )
    return EcgLink(
        clock=clock,
        source=EcgSource.RFCOMM,
        address="rfcomm:98-d3-91-fe-4e-9f",
        client=client,
        bridge=bridge,
        link_stats=None if stats is None else (lambda: stats),
    )


async def test_a_failed_connection_is_reported_and_retried_no_faster_than_the_retry() -> None:
    clock = ManualClock()
    client = FakeEcgClient(connect_ok=False)
    link = ecg_link(client, clock)
    await link.step()
    status = link.status()
    assert status.connect_attempts == 1
    assert status.last_error is not None
    assert "rfcomm:98-d3-91-fe-4e-9f" in status.last_error
    await link.step()  # inside the retry window: nothing attempted
    assert client.connects == 1
    clock.advance(ECG_RETRY)
    await link.step()
    assert client.connects == 2


async def test_a_connected_client_that_will_not_start_is_reported() -> None:
    clock = ManualClock()
    client = FakeEcgClient(start_ok=False)
    link = ecg_link(client, clock, stats=LinkStats(frames=3))
    await link.step()
    status = link.status()
    assert status.connected
    assert not status.acquiring
    assert status.last_error is not None
    assert "acquisition" in status.last_error
    assert status.link == LinkStats(frames=3)
    # Already connected: the next attempt goes straight to starting.
    client.start_ok = True
    clock.advance(ECG_RETRY)
    await link.step()
    assert client.connects == 1
    assert link.status().acquiring
    assert link.status().last_error is None


async def test_an_acquiring_client_is_pumped() -> None:
    clock = ManualClock()
    client = FakeEcgClient()
    link = ecg_link(client, clock)
    await link.step()
    assert client.is_acquiring
    await link.step()
    assert client.reads == 1
    assert link.status().bridge == EcgBridgeStats()
    assert link.client is client


# =========================================================================
# The control step
# =========================================================================


async def test_a_web_estop_latches_the_shared_supervisor_and_is_forwarded(
    tmp_path: Path,
) -> None:
    panel, clock = sim_panel(tmp_path)
    clock.advance(TICK)
    await panel.control_step()
    panel.surface.submit_estop(operator="dr. attending", reason="test")
    # Latched on the runtime's own supervisor before any tick.
    assert panel.runtime.standing_action is SafetyAction.QUICK_STOP
    clock.advance(TICK)
    await panel.control_step()
    assert panel.runtime.state is not RuntimeState.IDLE
    ending = panel.runtime.ending
    assert ending is not None
    # Exit after an e-stop goes through the runtime's own shutdown.
    detail = await panel.close()
    assert "without a write" not in detail


async def test_an_end_request_reaches_the_runtime(tmp_path: Path) -> None:
    panel, clock = sim_panel(tmp_path)
    assert isinstance(panel.surface.attest_estop_wiring("dr. attending"), Ok)
    assert isinstance(
        panel.surface.submit_start(
            profile_id="standard_30_min", operator="dr. attending", total_duration_s=None
        ),
        Ok,
    )
    assert isinstance(panel.surface.submit_end(operator="dr. attending", reason="done"), Ok)
    clock.advance(TICK)
    await panel.control_step()
    assert panel.runtime.stop_reason == "done"
    # The ending runs through its phases; the surface returns to idle once finished.
    for _ in range(200):
        clock.advance(Seconds(5.0))
        await panel.control_step()
        if panel.runtime.state is RuntimeState.FINISHED:
            break
    assert panel.runtime.state is RuntimeState.FINISHED
    assert panel.surface.run_state.value == "idle"


# =========================================================================
# Running and stopping
# =========================================================================


class FakeWeb:
    """A web runner that serves until told to exit, optionally failing."""

    def __init__(self, *, fail_at_start: bool = False, fail_at_exit: bool = False) -> None:
        self.exit: asyncio.Event = asyncio.Event()
        self.fail_at_start: bool = fail_at_start
        self.fail_at_exit: bool = fail_at_exit
        self.exits: int = 0

    async def serve(self) -> None:
        if self.fail_at_start:
            raise OSError("address already in use")
        await self.exit.wait()
        if self.fail_at_exit:
            raise RuntimeError("the server failed on the way out")

    def request_exit(self) -> None:
        self.exits += 1
        self.exit.set()


async def test_run_stops_every_task_on_the_stop_event_and_releases_the_drive(
    tmp_path: Path,
) -> None:
    panel, _ = sim_panel(tmp_path)
    stop = asyncio.Event()
    web = FakeWeb()
    asyncio.get_running_loop().call_later(0.5, stop.set)
    assert await panel.run(stop, web) == EXIT_OK
    assert web.exits == 1
    assert panel.runtime.state is RuntimeState.IDLE
    assert panel.runtime.idle_link.reads >= 1
    assert panel.ecg.status().acquiring is False  # disconnected on the way out


async def test_a_web_server_that_cannot_start_stops_the_console(tmp_path: Path) -> None:
    panel, _ = sim_panel(tmp_path)
    assert await panel.run(asyncio.Event(), FakeWeb(fail_at_start=True)) == EXIT_FAILED


async def test_a_task_failing_on_the_way_out_is_still_a_failure(tmp_path: Path) -> None:
    panel, _ = sim_panel(tmp_path)
    stop = asyncio.Event()
    stop.set()
    assert await panel.run(stop, FakeWeb(fail_at_exit=True)) == EXIT_FAILED


async def test_a_tick_that_raises_stops_the_console_failed_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime fails closed (silent) and re-raises; the console stops, sending nothing."""
    panel, _ = sim_panel(tmp_path)

    async def boom(_self: SimulatedDrive) -> None:
        raise RuntimeError("the transport blew up mid-read")

    monkeypatch.setattr(SimulatedDrive, "read_status", boom)
    assert await panel.run(asyncio.Event(), FakeWeb()) == EXIT_FAILED
    assert panel.runtime.silent
    assert panel.runtime.state is not RuntimeState.IDLE


async def test_the_production_web_runner_binds_and_exits(tmp_path: Path) -> None:
    """Loopback only, on a free port, told to exit before serving anything."""
    config = config_from({**BASE_ENV, "UI_PORT": str(SERVE_TEST_PORT)})
    panel = build_panel(config, clock=ManualClock(), profiles_path=tmp_path / "p.json")
    runner = UvicornRunner(panel.services, config)
    runner.request_exit()
    await asyncio.wait_for(runner.serve(), 10.0)
    await panel.close()


def _fake_runner(_services: Services, _config: LocalConfig) -> FakeWeb:
    return FakeWeb()


def _failing_runner(_services: Services, _config: LocalConfig) -> FakeWeb:
    return FakeWeb(fail_at_start=True)


def _recording_env(tmp_path: Path) -> Mapping[str, str]:
    """The production console records: keep its records out of the repository."""
    return {**BASE_ENV, "RECORD_ROOT": str(tmp_path / "records")}


async def test_run_console_builds_on_the_real_clock_and_undoes_its_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_panel, "UvicornRunner", _fake_runner)
    stop = asyncio.Event()
    stop.set()
    assert await run_console(config_from(_recording_env(tmp_path)), stop=stop) == EXIT_OK
    assert not sys.platform.startswith("win")


async def test_run_console_makes_its_own_stop_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_panel, "UvicornRunner", _failing_runner)
    assert await run_console(config_from(_recording_env(tmp_path))) == EXIT_FAILED


async def test_the_production_console_records_its_sessions_and_stops_its_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ANH-128 EX-1: ``run_console`` is what wires the black box; nothing else may forget it."""
    built: list[LocalPanel] = []
    real = build_panel

    def remembered(config: LocalConfig, *, clock: Clock, journal: Journal) -> LocalPanel:
        panel = real(config, clock=clock, journal=journal, profiles_path=tmp_path / "p.json")
        built.append(panel)
        return panel

    monkeypatch.setattr(local_panel, "build_panel", remembered)
    monkeypatch.setattr(local_panel, "UvicornRunner", _fake_runner)
    stop = asyncio.Event()
    stop.set()
    assert await run_console(config_from(_recording_env(tmp_path)), stop=stop) == EXIT_OK
    recorder = built[0].recorder
    assert recorder is not None
    assert recorder.journal.root == tmp_path / "records"
    assert stat.S_IMODE(recorder.journal.root.stat().st_mode) == 0o700
    assert [t.name for t in threading.enumerate() if t.name == "record-journal"] == []


async def test_the_stop_signals_are_routed_to_the_event_and_removed() -> None:
    stop = asyncio.Event()
    undo = install_stop_signals(stop)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(stop.wait(), 2.0)
    undo()


async def test_a_loop_without_signal_support_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()

    def unsupported(_sig: int, _callback: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", unsupported)
    undo = install_stop_signals(asyncio.Event())
    undo()


# =========================================================================
# The entry point
# =========================================================================


def test_the_environment_file_is_read_and_the_process_environment_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ARM_RADIUS_M=1.5\nUI_PORT=8090\nEMPTY\n", encoding="utf-8")
    monkeypatch.setenv("UI_PORT", "8091")
    merged = load_environment(env_file)
    assert merged["ARM_RADIUS_M"] == "1.5"
    assert merged["UI_PORT"] == "8091"
    assert "EMPTY" not in merged


def test_main_refuses_a_bad_configuration_with_every_problem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert main({"MOTOR_BACKEND": "warp"}) == EXIT_CONFIG
    logged = caplog.text
    assert "MOTOR_BACKEND" in logged
    assert "ARM_RADIUS_M" in logged


def test_main_runs_the_console_on_a_good_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[LocalConfig] = []

    async def fake(config: LocalConfig) -> int:
        seen.append(config)
        return EXIT_OK

    monkeypatch.setattr(local_panel, "run_console", fake)
    assert main(BASE_ENV) == EXIT_OK
    assert seen[0].motor_backend is MotorBackend.SIM


def test_main_reads_the_environment_when_none_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_panel, "load_environment", lambda: {"ARM_RADIUS_M": "x"})
    assert main() == EXIT_CONFIG


def test_the_module_entry_exits_with_mains_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m src.local_panel``: a bad radius in the process environment exits 2."""
    monkeypatch.setenv("ARM_RADIUS_M", "not-a-number")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as raised:
            runpy.run_module("src.local_panel", run_name="__main__")
    assert raised.value.code == EXIT_CONFIG


# =========================================================================
# The exhaustiveness guards and the non-simulated wiring
# =========================================================================


def test_an_unknown_motor_backend_hits_the_guard() -> None:
    config = replace(config_from(BASE_ENV), motor_backend=cast("MotorBackend", "warp"))
    with pytest.raises(AssertionError):
        build_drive(config, ManualClock())


def test_an_unknown_ecg_source_hits_the_guard() -> None:
    config = replace(config_from(BASE_ENV), ecg_source=cast("EcgSource", "telepathy"))
    with pytest.raises(AssertionError):
        build_ecg_client(config, ManualClock())


async def test_an_unknown_command_in_the_mailbox_hits_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel, _ = sim_panel(tmp_path)

    def bogus(_self: ControlSurface) -> object:
        return object()

    monkeypatch.setattr(ControlSurface, "take_command", bogus)
    with pytest.raises(AssertionError):
        await panel.control_step()


async def test_a_console_on_real_devices_steps_no_simulator(tmp_path: Path) -> None:
    """Neither plant is advanced when neither device is simulated.

    The drive here is the runtime tests' recording fake (no copper), the
    BITalino a real client that is never connected: ``control_step`` touches
    only the drive, and only to read it.
    """
    clock = ManualClock()
    fake = FakeDrive(clock)
    released: list[bool] = []
    side = DriveSide(backend=fake, simulator=None, release=lambda: released.append(True))
    config = replace(config_from(SERIAL_ENV), motor_backend=MotorBackend.SIM, drive_link=None)
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json", drive=side)
    assert panel.drive is side
    assert panel.hub is panel.services.hub
    for _ in range(3):
        clock.advance(TICK)
        await panel.control_step()
    assert fake.writes == []
    assert fake.commands == []
    assert "eta" in fake.trace
    await panel.close()
    assert released == [True]


# =========================================================================
# Milestone M3: the manual commands through the mailbox
# =========================================================================

OPERATOR: Final[str] = "dr. attending"


async def _tick(panel: LocalPanel, clock: ManualClock, seconds: float = 0.2) -> None:
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()


def _state(panel: LocalPanel) -> RuntimeState:
    """Read through a call, so mypy does not narrow one assertion into the next."""
    return panel.runtime.state


def _run_state(panel: LocalPanel) -> RunState:
    return panel.surface.run_state


class EventSpy:
    """Subscribes to the hub like a page does, and keeps every event's words."""

    def __init__(self, panel: LocalPanel) -> None:
        self.client: TelemetryClient = panel.hub.subscribe()
        self.details: list[str] = []

    async def drain(self) -> list[str]:
        while True:
            try:
                payload = await asyncio.wait_for(self.client.next_payload(), 0.01)
            except TimeoutError:
                return self.details
            if payload.event is not None:
                self.details.append(payload.event.detail)


async def test_a_manual_start_through_the_mailbox_runs_and_its_refusals_are_published(
    tmp_path: Path,
) -> None:
    panel, clock = sim_panel(tmp_path)
    spy = EventSpy(panel)
    surface = panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await _tick(panel, clock)
    assert panel.runtime.state is RuntimeState.RUNNING
    assert _run_state(panel) is RunState.RUNNING
    # A target outside the domain: refused by the runtime, said on the page.
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(0.5), operator=OPERATOR), Ok
    )
    await _tick(panel, clock)
    assert any("consigne refusee" in detail for detail in await spy.drain())
    assert panel.runtime.manual_target == 0
    # A good one is taken.
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(2.0), operator=OPERATOR), Ok
    )
    await _tick(panel, clock)
    assert panel.runtime.manual_target == round(2.0 * 49.79)
    # A fault reset while turning is the runtime's refusal, published.
    assert isinstance(surface.submit_fault_reset(operator=OPERATOR), Ok)
    await _tick(panel, clock)
    assert any("reset refuse" in detail for detail in await spy.drain())
    # The ending tick reports neither idle nor running.
    assert isinstance(surface.submit_end(operator=OPERATOR, reason="done"), Ok)
    await _tick(panel, clock)
    assert _state(panel) is RuntimeState.ENDING
    await _tick(panel, clock, 15.0)
    assert _state(panel) is RuntimeState.FINISHED
    assert _run_state(panel) is RunState.IDLE
    await panel.close()


async def test_an_occupied_start_that_reached_the_mailbox_is_refused_by_the_loop(
    tmp_path: Path,
) -> None:
    """The second gate behind the route's 403: the configuration refuses OCCUPIED."""
    panel, clock = sim_panel(tmp_path)
    spy = EventSpy(panel)
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.OCCUPIED, operator=OPERATOR), Ok
    )
    await _tick(panel, clock)
    assert panel.runtime.state is RuntimeState.IDLE
    assert panel.surface.run_state is RunState.IDLE
    assert any("OCCUPANCY_OCCUPIED_ENABLED" in detail for detail in await spy.drain())
    await panel.close()


async def test_a_manual_start_the_runtime_refuses_is_published(tmp_path: Path) -> None:
    """The drive cannot be armed: the start is refused in words and the surface is idle."""
    clock = ManualClock()
    fake = FakeDrive(clock)
    fake.break_comms()
    side = DriveSide(backend=fake, simulator=None, release=lambda: None)
    panel = build_panel(
        config_from(BASE_ENV), clock=clock, profiles_path=tmp_path / "p.json", drive=side
    )
    spy = EventSpy(panel)
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await _tick(panel, clock)
    assert panel.surface.run_state is RunState.IDLE
    assert any("demarrage refuse" in detail for detail in await spy.drain())


async def test_an_idle_fault_is_reset_through_the_mailbox(tmp_path: Path) -> None:
    panel, clock = sim_panel(tmp_path)
    spy = EventSpy(panel)
    simulator = panel.drive.simulator
    assert simulator is not None
    simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
    await _tick(panel, clock, 1.0)
    assert isinstance(panel.surface.submit_fault_reset(operator=OPERATOR), Ok)
    await _tick(panel, clock, 1.0)
    assert simulator.sim_state.name != "FAULT"
    assert not any("reset refuse" in detail for detail in await spy.drain())
    await panel.close()


def test_unusable_motion_limits_refuse_to_build_the_console(tmp_path: Path) -> None:
    config = replace(config_from(BASE_ENV), motion_limits_path=tmp_path / "absent.json")
    with pytest.raises(ValueError, match="motion limits"):
        build_panel(config, clock=ManualClock(), profiles_path=tmp_path / "p.json")
    absolute = tmp_path / "limits.json"
    absolute.write_text(
        '{"output_rpm_per_s": 0.5, "g_per_s": 0.03, "min_run_motor_rpm": 55}', encoding="utf-8"
    )
    panel = build_panel(
        replace(config, motion_limits_path=absolute),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
    )
    assert panel.runtime.manual is None


def _verdict(rule: str) -> SafetyVerdict:
    return SafetyVerdict(
        action=SafetyAction.RAMP_DOWN, rule=rule, detail="d", latched=True, since=Monotonic(0.0)
    )


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (AlreadyStarted(RuntimeState.RUNNING), "deja running"),
        (NotAttested("statement"), "non atteste"),
        (SafetyStanding(_verdict("drive_fault")), "drive_fault"),
        (
            LimitsMismatch(threshold="hard_max_bpm", profile_bpm=Bpm(1), supervisor_bpm=Bpm(2)),
            "hard_max_bpm",
        ),
        (PlanUnusable("too slow"), "too slow"),
        (DriveUnavailable("no link"), "no link"),
        (
            DriveParameterRefused(
                violation=LowSpeedNotZero(low_speed=Hertz(1.0)),
                limits=SimulatedDriveConfig().limits,
            ),
            "LSP",
        ),
        (DrivePrecommanded(state=DriveState.OPERATION_ENABLED, output_rpm=MotorRpm(900)), "900"),
        (DriveInFault(report=None, state=DriveState.FAULT), "(?)"),
        (DriveInFault(report=describe_fault(RawRegister(5)), state=DriveState.FAULT), "LFT 5"),
    ],
)
def test_every_start_refusal_has_words(refusal: StartRefusal, words: str) -> None:
    line = describe_start_refusal(refusal)
    assert line.startswith("demarrage refuse : ")
    assert words in line


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (NoManualSession(RuntimeState.IDLE), "idle"),
        (ManualEnding("ending"), "ending"),
        (
            TargetOutOfRange(requested=OutputRpm(9.0), min_run=MotorRpm(55), ceiling=MotorRpm(300)),
            "9.00",
        ),
    ],
)
def test_every_target_refusal_has_words(refusal: ManualTargetRefusal, words: str) -> None:
    assert words in describe_target_refusal(refusal)


@pytest.mark.parametrize(
    ("refusal", "words"),
    [
        (ResetWhileCommanded(state=RuntimeState.RUNNING, phase=Phase.HOLD), "hold"),
        (ResetBehindVerdict(_verdict("operator_estop")), "operator_estop"),
        (NoFaultToReset(None), "non lu"),
        (NoFaultToReset(DriveState.READY), "ready"),
        (ShaftStillTurning(MotorRpm(40)), "40"),
        (ResetUndelivered("silent"), "silent"),
    ],
)
def test_every_reset_refusal_has_words(refusal: FaultResetRefusal, words: str) -> None:
    assert words in describe_reset_refusal(refusal)


@pytest.mark.parametrize(
    "describe", [describe_start_refusal, describe_target_refusal, describe_reset_refusal]
)
def test_a_refusal_that_is_not_one_fails_loudly(describe: Callable[[object], str]) -> None:
    with pytest.raises(AssertionError):
        describe(cast("StartRefusal", "not-a-refusal"))


async def test_a_command_that_is_not_one_fails_loudly(tmp_path: Path) -> None:
    panel, clock = sim_panel(tmp_path)
    object.__setattr__(panel.surface, "_pending", cast(StartSession, "not-a-command"))
    clock.advance(TICK)
    with pytest.raises(AssertionError):
        await panel.control_step()


def _bogus_state(_self: TrainingRuntime) -> RuntimeState:
    return cast("RuntimeState", "bogus")


async def test_a_runtime_state_that_is_not_one_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the tick: the console's own match over the state, not the runtime's."""
    panel, clock = sim_panel(tmp_path)
    await _tick(panel, clock)
    snapshot = panel.runtime.snapshot()

    async def _tick_only(_self: TrainingRuntime, _now: Monotonic) -> TelemetrySnapshot:
        return snapshot

    monkeypatch.setattr(TrainingRuntime, "tick", _tick_only)
    monkeypatch.setattr(TrainingRuntime, "state", property(_bogus_state))
    clock.advance(TICK)
    with pytest.raises(AssertionError):
        await panel.control_step()
