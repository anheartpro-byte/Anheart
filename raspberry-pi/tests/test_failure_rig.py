"""The shared rig of the failure-injection tests: the REAL console, on a manual clock.

Every ``tests/test_failure_*.py`` file builds the real composition root
(:func:`src.local_panel.build_panel`) over a ``SimulatedDrive`` (optionally
wrapped by one of the faulty backends below) and a ``SimulatedBitalinoClient``,
speaks to it through the operator's HTTP API, ticks it as its loop does, and
then judges how the machine was LEFT:

* the shaft at 0 rpm, read from the simulator after the plant ran on alone;
* no torque (the output stage off), or - when the runtime went silent - the
  drive's own ``ttO`` watchdog latched (SLF), which is what stopped it;
* no NaN in any snapshot the operator was shown;
* an actionable message the operator could read: a verdict with its detail,
  a drive fault mnemonic, a refusal event, or an HTTP error with a detail.

This module holds no test of the machine; it holds the rig and one self-check.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, final, override

import httpx

from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.control_surface import EventKind, SessionEvent
from src.ecg_pipeline import EcgFrame, TreatFunction, Treatment, treat_ecg
from src.local_config import LocalConfig, load_local_config
from src.local_panel import CloudTransportFactory, DriveSide, LocalPanel, build_panel
from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    ControlWord,
    DriveBackend,
    DriveError,
    DriveLimits,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.simulated import SimState, SimulatedDrive, SimulatedDriveConfig
from src.record.journal import Journal
from src.result import Err, Ok, Result
from src.telemetry import PayloadKind, TelemetryClient
from src.training.types import TelemetrySnapshot
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from src.web.app import create_app

TICK: Final[Seconds] = Seconds(0.2)
OPERATOR: Final[str] = "dr. attending"
RATIO: Final[float] = 49.79
MIN_RUN: Final[int] = 55

RUN_ON_S: Final[float] = 240.0
"""How long the plant runs on alone before the verdict: a freewheel from 1000 rpm
on the simulator's 20 s coast constant needs ~140 s to fall below 1 rpm."""

ENERGISED: Final[frozenset[SimState]] = frozenset(
    {SimState.OPERATION_ENABLED, SimState.FAULT_REACTION_RAMP_STOP, SimState.DISABLING_ON_RAMP}
)
"""The simulator's torque-producing states."""

BENCH_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
    "MOTOR_MAX_RPM": "1380",
}

PROGRAMME_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    "PROGRAMS_ENABLED": "true",
    "OCCUPANCY_OCCUPIED_ENABLED": "true",
}

SHORT_PROFILE: Final[str] = "failure_short"
"""A short programme (20 s baseline) so a fault can be injected while turning."""

SHORT_STORE: Final[str] = """{
  "version": 1,
  "rev": 0,
  "profiles": [
    {
      "profile_id": "failure_short",
      "name": "failure injection, short",
      "total_duration_s": 400.0,
      "baseline_s": 20.0,
      "warmup_max_s": 120.0,
      "hold_min_s": 60.0,
      "cooldown_s": 60.0,
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


def config_of(env: Mapping[str, str]) -> LocalConfig:
    loaded = load_local_config(env)
    if isinstance(loaded, Err):
        raise TypeError(f"configuration refused: {loaded.error}")
    return loaded.value


def finite_snapshot(snapshot: TelemetrySnapshot) -> bool:
    """Whether every number the operator is shown in ``snapshot`` is finite."""
    measured = snapshot.measured
    setpoint = snapshot.setpoint
    values: list[float] = [
        float(measured.output_rpm),
        float(measured.hertz),
        float(measured.g_load),
        float(measured.resultant_g),
        float(setpoint.output_rpm),
        float(setpoint.hertz),
        float(setpoint.g_load),
    ]
    if snapshot.current is not None:
        values.append(float(snapshot.current))
    return all(math.isfinite(value) for value in values)


# =========================================================================
# Faulty backends: a real SimulatedDrive behind one injected lie
# =========================================================================


class Wrapped:
    """Delegates every ``DriveBackend`` call to ``inner``; subclasses override one."""

    def __init__(self, inner: SimulatedDrive) -> None:
        self.inner: SimulatedDrive = inner

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return self.inner.acquisition_evidence

    async def open(self) -> Result[None, DriveError]:
        return await self.inner.open()

    async def close(self) -> Result[None, DriveError]:
        return await self.inner.close()

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        return await self.inner.write_command(word)

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        return await self.inner.write_speed(rpm)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        return await self.inner.read_status()

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        return await self.inner.read_limits()

    @property
    def emergency_budget(self) -> Seconds:
        return self.inner.emergency_budget

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        return self.inner.emergency_disable_blocking(timeout)


class TickRaises(Wrapped):
    """``read_status`` raises once armed: a bug inside the control tick."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.armed: bool = False

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self.armed:
            raise RuntimeError("injected: a bug inside the tick")
        return await self.inner.read_status()


def backend(drive: DriveBackend) -> DriveBackend:
    """Typed identity: proves a wrapper satisfies the protocol."""
    return drive


# =========================================================================
# The rig
# =========================================================================


class SteppedClock(Protocol):
    """A clock the test moves: ``ManualClock``, or :class:`JumpClock`."""

    def monotonic(self) -> Monotonic: ...

    def unix_millis(self) -> UnixMillis: ...

    def advance(self, delta: Seconds) -> Monotonic: ...


@final
class JumpClock:
    """A manual monotonic clock whose WALL clock can be stepped, as NTP does on a Pi."""

    def __init__(self) -> None:
        self.inner: ManualClock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
        self.wall_offset_ms: int = 0

    def monotonic(self) -> Monotonic:
        return self.inner.monotonic()

    def unix_millis(self) -> UnixMillis:
        return UnixMillis(self.inner.unix_millis() + self.wall_offset_ms)

    def advance(self, delta: Seconds) -> Monotonic:
        return self.inner.advance(delta)


@dataclass
class Rig:
    """The console, its simulator, the operator's HTTP client and what they saw."""

    clock: SteppedClock
    panel: LocalPanel
    simulator: SimulatedDrive
    watcher: TelemetryClient
    config: LocalConfig
    snapshots: list[TelemetrySnapshot] = field(default_factory=list[TelemetrySnapshot])
    events: list[SessionEvent] = field(default_factory=list[SessionEvent])
    presence: bool = True

    def http(self) -> httpx.AsyncClient:
        app = create_app(services=self.panel.services, config=self.config.web)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    async def drain(self) -> None:
        """Read every event the operator's screen would have received so far."""
        while True:
            try:
                payload = await asyncio.wait_for(self.watcher.next_payload(), 0.001)
            except TimeoutError:
                return
            if payload.kind is PayloadKind.EVENT and payload.event is not None:
                self.events.append(payload.event)
            if payload.kind is PayloadKind.RESYNC:
                return

    async def tick(self, seconds: float) -> TelemetrySnapshot:
        """Tick the console as its loop does; returns the last snapshot."""
        last = self.panel.runtime.snapshot()
        for _ in range(max(1, round(seconds / TICK))):
            self.clock.advance(TICK)
            if self.presence:
                self.panel.surface.note_presence(OPERATOR)
            await self.panel.ecg_step()
            last = await self.panel.control_step()
            self.snapshots.append(last)
            await self.drain()
        return last

    def run_on(self, seconds: float) -> None:
        """Let the plant run on with nobody ticking (the process is gone or stuck)."""
        for _ in range(round(seconds / TICK)):
            self.simulator.advance(self.clock.advance(TICK))

    def refusals(self) -> list[str]:
        return [event.detail for event in self.events if event.kind is EventKind.REFUSED]

    def all_finite(self) -> bool:
        return all(finite_snapshot(snapshot) for snapshot in self.snapshots)

    async def left_stopped(self) -> str:
        """Judge how the machine was left; an empty string means stopped and safe.

        The shaft is read from the simulator directly (a fresh probe, never the
        silent runtime's link), after the plant has run on for 60 s.
        """
        self.run_on(RUN_ON_S)
        sim = self.simulator
        problems: list[str] = []
        if sim.sim_state in ENERGISED:
            problems.append(f"drive left energised in {sim.sim_state.name}")
        await sim.open()
        status = await sim.read_status()
        if isinstance(status, Ok):
            if abs(status.value.output_rpm) >= 1:
                problems.append(f"shaft still at {status.value.output_rpm} motor rpm")
        else:
            problems.append(f"drive did not answer the probe: {status.error!r}")
        runtime = self.panel.runtime
        if not runtime.silent and sim.commanded_setpoint != 0:
            problems.append(f"LFRD left at {sim.commanded_setpoint}")
        if not runtime.silent and runtime.output_enabled:
            problems.append("runtime still believes the output is enabled")
        if not self.all_finite():
            problems.append("a snapshot carried a non-finite number")
        return "; ".join(problems)

    def silent_backstop(self) -> bool:
        """When the runtime went silent: the drive's own ttO latched SLF."""
        fault = self.simulator.sim_state
        return fault in {SimState.FAULT, SimState.FAULT_REACTION_RAMP_STOP}


def make_rig(
    tmp_path: Path,
    *,
    env: Mapping[str, str] = BENCH_ENV,
    simulator: SimulatedDrive | None = None,
    wrap: type[Wrapped] | None = None,
    dsp: bool = False,
    treat: TreatFunction | None = None,
    clock: SteppedClock | None = None,
    transport: CloudTransportFactory | None = None,
    journal: Journal | None = None,
) -> tuple[Rig, Wrapped | None]:
    """Build the console. ``wrap`` puts one faulty backend between runtime and simulator.

    ``journal`` makes it record its sessions (the tests of the session record);
    without one nothing is written, as before.
    """
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000)) if clock is None else clock
    config = config_of(env)
    sim = (
        SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
        if simulator is None
        else simulator
    )
    wrapper = None if wrap is None else wrap(sim)
    side = DriveSide(
        backend=sim if wrapper is None else backend(wrapper), simulator=sim, release=_nothing
    )
    defaults = tmp_path / "defaults.json"
    defaults.write_text(SHORT_STORE, encoding="utf-8")
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        defaults_path=defaults,
        treat=(treat_inline if dsp else no_dsp) if treat is None else treat,
        drive=side,
        transport=transport,
        journal=journal,
    )
    rig = Rig(clock=clock, panel=panel, simulator=sim, watcher=panel.hub.subscribe(), config=config)
    return rig, wrapper


async def treat_inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    """The REAL DSP, run on the loop (a test has no control deadline to protect)."""
    return treat_ecg(treatment, batch)


async def no_dsp(_treatment: Treatment, _batch: SampleBatch) -> EcgFrame | None:
    """No DSP at all: for bench sessions, where no heart rate is supervised."""
    return None


def _nothing() -> None:
    """The simulator has no port to release."""


async def precommanded(clock: SteppedClock, rpm: int) -> SimulatedDrive:
    """A drive a crashed process left ENABLED and turning at ``rpm``, reference still set."""
    sim = SimulatedDrive(
        clock,
        SimulatedDriveConfig(reads_reset_watchdog=True),
        initial_state=SimState.OPERATION_ENABLED,
        initial_rpm=MotorRpm(rpm),
    )
    await sim.open()
    await sim.write_speed(MotorRpm(rpm))
    await sim.close()
    return sim


async def attest(session: httpx.AsyncClient) -> None:
    response = await session.post(
        "/api/safety/attest",
        json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
    )
    if response.status_code != 200:
        raise AssertionError(response.text)


async def start_manual(rig: Rig, session: httpx.AsyncClient, output_rpm: float) -> None:
    """Attest, start a bench manual session and climb to ``output_rpm``."""
    await attest(session)
    started = await session.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    if started.status_code != 202:
        raise AssertionError(started.text)
    await rig.tick(1.0)
    target = await session.post(
        "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
    )
    if target.status_code != 202:
        raise AssertionError(target.text)


async def start_programme(session: httpx.AsyncClient) -> None:
    """Attest and start the short programme."""
    await attest(session)
    started = await session.post(
        "/api/session/start",
        json={
            "profile_id": SHORT_PROFILE,
            "operator": OPERATOR,
            "total_duration_s": None,
            "subject_age": 30,
        },
    )
    if started.status_code != 202:
        raise AssertionError(started.text)


async def test_the_rig_builds_an_idle_console_that_is_left_stopped(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    await rig.tick(2.0)
    assert rig.panel.runtime.snapshot().measured.motor_rpm == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""
