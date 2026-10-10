"""``python -m simulation.endurance``: the whole console, alive for a day (ANH-164).

    python -m simulation.endurance simulated                 # 24 simulated hours
    python -m simulation.endurance simulated --hours 2
    python -m simulation.endurance real-time --minutes 120   # the same console, on the real clock
    python -m simulation.endurance judge readings.csv        # readings taken by hand on a bench

The REAL console (:func:`src.local_panel.build_panel`: the runtime, the
simulated drive and BITalino, the acquisition, the session record on a real
disk, the dashboard link) chains bench manual sessions and programmed
sessions, with rests between them, while four things are read from the
process (:mod:`simulation.endurance_measures`): resident memory, the duration
of the control tick, open file descriptors, the size of the data folder. The
run is then judged against the thresholds of the ticket, and a report is
written (``endurance.json``, ``endurance.md``). Exit code 0 when every
threshold is PASS or a documented known defect (XFAIL).

Nothing here changes the console. The operator's commands go through its HTTP
API; the dashboard is :class:`tests.fake_dashboard.FakeDashboard`, the double
the acceptance test of the synchronisation uses, given to the console in place
of its HTTP transport: **no Convex deployment is ever contacted**, whatever
``CONVEX_URL`` says.

``simulated``
    A :class:`~src.clock.ManualClock` moved 200 ms at a time. Each simulated
    second is five control ticks (each timed), five acquisition steps, the
    journal written through, one sensor refresh and one dashboard step: what
    the console's own tasks do in a real second. The heart-rate DSP runs on
    the loop, and only around the programmed sessions (``--dsp programmes``):
    it costs four times everything else the console does in a tick, and a
    bench manual session supervises no heart rate. ``--dsp always`` runs it
    the whole day.

``real-time``
    :meth:`~src.local_panel.LocalPanel.run` itself, on the real clock: the
    console's own tasks, its web server on a free loopback port, its journal
    thread, its DSP on worker threads. The tick is timed from outside the
    loop: from the instant a tick reads to the moment its snapshot reaches a
    subscriber of the telemetry hub, which is slightly more than the tick.

``judge``
    Applies the same memory and descriptor thresholds to readings taken from
    outside a console somebody else runs (the bench day, docs/pi-image.md).

See .claude/skills/anheart-strict-python/SKILL.md and
docs/framework-de-test.md, section 17.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import socket
import sys
import tempfile
import time
from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path
from typing import Final, Protocol

import httpx
from tests.fake_dashboard import FakeDashboard

from simulation.endurance_measures import (
    DataFolder,
    Footprint,
    Histogram,
    Kind,
    Mode,
    Run,
    Sample,
    Sampler,
    Session,
    Sync,
    judge,
    markdown,
    observed,
    passed,
    progress,
    samples_from_csv,
    write_report,
)
from src.bitalino_client import SampleBatch
from src.clock import Clock, ManualClock, RealClock
from src.control_surface import EventKind
from src.ecg_pipeline import EcgFrame, TreatFunction, Treatment, treat_ecg, treat_off_loop
from src.local_config import LocalConfig, load_local_config
from src.local_panel import LocalPanel, UvicornRunner, build_panel
from src.record.journal import Journal
from src.result import Err
from src.telemetry import TelemetryClient
from src.training.runtime import EndReason, RuntimeState
from src.training.types import TelemetrySnapshot
from src.units import Monotonic, Seconds, UnixMillis
from src.web.app import create_app

OUT_DIR: Final[Path] = Path(__file__).resolve().parent / "out" / "endurance"

OPERATOR: Final[str] = "endurance"
TICK: Final[Seconds] = Seconds(0.2)
TICKS_PER_SECOND: Final[int] = 5

EPOCH_MS: Final[int] = 1_790_000_000_000
"""Where the manual clock starts its wall clock: September 2026."""

PROFILE_ID: Final[str] = "endurance_court"

PROFILE_STORE: Final[str] = """{
  "version": 1,
  "rev": 0,
  "profiles": [
    {
      "profile_id": "endurance_court",
      "name": "endurance, programme court",
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
"""The one programme the scenario runs: 400 s, every phase, which the simulated
rider completes. The shipped programmes last 30 and 45 minutes."""

ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "MOTOR_MAX_RPM": "1380",
    "PROGRAMS_ENABLED": "true",
    "OCCUPANCY_OCCUPIED_ENABLED": "true",
    "UI_HOST": "127.0.0.1",
    # What makes the console build its dashboard link. The link is handed the
    # simulated dashboard as its transport: this address is never contacted.
    "MACHINE_API_KEY": "endurance",
    "CONVEX_URL": "https://example.convex.site",
}

IDLE_PORT: Final[int] = 8090
"""The port written in the configuration of a simulated run, which opens no socket."""

HEARTBEAT: Final[str] = "/api/machine/heartbeat"

MANUAL_TARGETS: Final[tuple[float, ...]] = (8.0, 12.0, 5.0)
"""Output rpm a manual session is walked through, a third of its time each."""

COMMAND_SETTLE: Final[int] = 3
"""Seconds the loop is given to take a start command before its effect is looked for."""

FINISH_LIMIT: Final[int] = 300
"""Seconds a stopped session may take to come down and finish."""

PROGRAMME_LIMIT: Final[int] = 900
"""Seconds the 400 s programme may take from its start to finished."""

DSP_LEAD: Final[int] = 20
"""Seconds of heart-rate DSP before a programme starts: its window is 8 s long."""

CLOSING_LIMIT: Final[int] = 120
"""Seconds the last session is given to reach the dashboard once the run is over."""

POINTS_MISSING: Final[int] = 5
"""1 Hz points a session may lack on the dashboard against the seconds the scenario timed."""

READY_ATTEMPTS: Final[int] = 100
READY_WAIT: Final[float] = 0.1
"""The console's page is asked this many times, this far apart, before the run starts."""

NOTE_HEALTH: Final[str] = (
    "Santé de la machine dans le heartbeat : absente (ANH-148 non fait). Les heartbeats "
    "partent et sont comptés, sans objet de santé."
)

NOTE_DASHBOARD: Final[str] = (
    "Tableau de bord simulé en mémoire (tests/fake_dashboard.py) : aucun déploiement Convex, "
    "aucun réseau."
)

NOTE_HARDWARE: Final[str] = (
    "Variateur et BITalino simulés, pas de caméra, pas de page ouverte dans un navigateur "
    "(un abonné du hub de télémétrie lit ce que la page lirait)."
)

NOTE_DEPOSIT: Final[str] = (
    "Dépôt hors machine des enregistrements : inexistant, donc aucune purge (seuil rotation)."
)

NOTE_MEMORY: Final[str] = (
    "Mémoire résidente : VmRSS de /proc sous Linux ; sur macOS, l'empreinte physique du "
    "processus, qui compte aussi les pages que le système a compressées."
)

NOTES: Final[tuple[str, ...]] = (
    NOTE_HEALTH,
    NOTE_DASHBOARD,
    NOTE_HARDWARE,
    NOTE_DEPOSIT,
    NOTE_MEMORY,
)
"""What every run of the scenario leaves out, said in its report."""

NOTE_GATED_DSP: Final[str] = (
    "Traitement du signal cardiaque (BioSPPy) exécuté autour des séances programmées "
    "seulement ; pendant les séances manuelles et les repos, l'acquisition, l'enregistrement "
    "brut et les indicateurs des capteurs tournent, sans calcul de fréquence cardiaque "
    "(--dsp always pour le faire tourner toute la journée)."
)

NOTE_MANUAL_CLOCK: Final[str] = (
    "Horloge simulée : la durée du tic est celle de son calcul, journal écrit entre deux "
    "tics. La régularité du tic en temps réel se mesure avec le mode real-time."
)

NOTE_REAL_TICK: Final[str] = (
    "Durée du tic mesurée de l'instant que le tic lit à l'arrivée de son instantané chez un "
    "abonné du hub : elle majore la durée réelle du tic."
)

NOTE_OBSERVED: Final[str] = (
    "Relevés pris de l'extérieur d'une console : ni durée du tic, ni contenu du dossier de "
    "données, ni séances ne sont jugés ici."
)


class AnomalyError(Exception):
    """The scenario met something it does not expect of a console in good health."""


@unique
class Dsp(Enum):
    """When a simulated run executes the heart-rate DSP."""

    PROGRAMMES = "programmes"
    ALWAYS = "always"


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """The rhythm of a run, in seconds of the console's life."""

    manual: int = 900
    """How long a manual session stays at its targets before the operator stops it."""

    rest: int = 300
    """The rest after each session."""

    sample_every: int = 300
    dsp: Dsp = Dsp.PROGRAMMES
    programme: float | None = None
    """The total duration asked for the programme; ``None``: the 400 s of its profile."""


REAL_TIME_PLAN: Final[Plan] = Plan(manual=120, rest=45, sample_every=30)
"""Shorter sessions for a run counted in minutes: about eleven minutes a cycle."""


# =========================================================================
# The console, and the doubles around it
# =========================================================================


class GatedDsp:
    """The real heart-rate DSP, run on the loop, and only while it is switched on.

    Switched off it answers what the console's own tests answer for a bench
    session (no frame): no heart rate is produced, none is invented.
    Mutable, owned by the scenario's loop.
    """

    __slots__ = ("on",)

    def __init__(self, *, on: bool) -> None:
        self.on: bool = on

    async def __call__(self, treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
        return treat_ecg(treatment, batch) if self.on else None


@dataclass(frozen=True, slots=True, kw_only=True)
class Console:
    """The real console as built for a run, with its journal and its simulated dashboard."""

    panel: LocalPanel
    config: LocalConfig
    clock: Clock
    journal: Journal
    dashboard: FakeDashboard
    folder: DataFolder


def build_console(
    work: Path,
    clock: Clock,
    *,
    treat: TreatFunction,
    port: int,
    env: Mapping[str, str] = ENV,
) -> Console:
    """The console of ``env`` on ``clock``, recording under ``work``. Opens no socket.

    Raises ``ValueError`` when the configuration is refused or names no
    dashboard: an endurance run without the synchronisation proves less than
    its report would say.
    """
    loaded = load_local_config({**env, "UI_PORT": str(port)})
    if isinstance(loaded, Err):
        raise ValueError(f"configuration refused: {loaded.error}")
    config = loaded.value
    if config.cloud is None:
        raise ValueError("the endurance console needs its dashboard link (MACHINE_API_KEY)")
    defaults = work / "profiles.default.json"
    defaults.write_text(PROFILE_STORE, encoding="utf-8")
    dashboard = FakeDashboard(now=lambda: int(clock.unix_millis()))
    journal = Journal(work / "records", clock, retention_days=config.record.retention_days)
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=work / "profiles.json",
        defaults_path=defaults,
        treat=treat,
        transport=lambda _config: dashboard,
        journal=journal,
    )
    return Console(
        panel=panel,
        config=config,
        clock=clock,
        journal=journal,
        dashboard=dashboard,
        folder=DataFolder(journal.root),
    )


class Screen:
    """What an operator's page takes from the telemetry hub: every event, the latest snapshot.

    Mutable, owned by the loop that reads it.
    """

    __slots__ = ("_client", "refusal")

    def __init__(self, client: TelemetryClient) -> None:
        self._client: TelemetryClient = client
        self.refusal: str | None = None
        """The last thing the console refused, in its own words."""

    async def read(self) -> TelemetrySnapshot | None:
        """Read up to the next snapshot and return it; ``None`` when the hub evicted this page."""
        while True:
            payload = await self._client.next_payload()
            event = payload.event
            if event is None:
                return payload.snapshot
            if event.kind is EventKind.REFUSED:
                self.refusal = event.detail


class Operator:
    """The person at the console: every command through its HTTP API."""

    __slots__ = ("_http",)

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http: httpx.AsyncClient = http

    async def ready(self, attempts: int = READY_ATTEMPTS, wait: float = READY_WAIT) -> None:
        """Wait for the console's page to answer; :class:`AnomalyError` when it never does."""
        for _ in range(attempts):
            try:
                answered = (await self._http.get("/healthz")).is_success
            except httpx.TransportError:
                answered = False
            if answered:
                return
            await asyncio.sleep(wait)
        raise AnomalyError("the console never answered on /healthz")

    async def _post(self, path: str, body: Mapping[str, object]) -> None:
        try:
            answered = await self._http.post(path, json=dict(body))
        except httpx.TransportError as error:
            raise AnomalyError(f"POST {path}: the console did not answer ({error!r})") from error
        if answered.is_error:
            raise AnomalyError(f"POST {path}: HTTP {answered.status_code} {answered.text[:300]}")

    async def attest(self) -> None:
        """Attest the wiring of the emergency stop, as a start requires since this boot."""
        await self._post(
            "/api/safety/attest",
            {"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
        )

    async def start_manual(self) -> None:
        """Ask for a bench manual session."""
        await self._post("/api/manual/start", {"occupancy": "bench", "operator": OPERATOR})

    async def target(self, output_rpm: float) -> None:
        """Give the manual session a target."""
        await self._post("/api/manual/target", {"output_rpm": output_rpm, "operator": OPERATOR})

    async def start_programme(self, total: float | None) -> None:
        """Ask for the scenario's programmed session, for an adult rider.

        ``total``: the duration the operator chooses for it, or ``None`` for its own.
        """
        await self._post(
            "/api/session/start",
            {
                "profile_id": PROFILE_ID,
                "operator": OPERATOR,
                "total_duration_s": total,
                "subject_age": 30,
            },
        )

    async def stop(self, reason: str) -> None:
        """End the running session on its controlled ramp."""
        await self._post("/api/session/stop", {"operator": OPERATOR, "reason": reason})

    async def ping(self) -> None:
        """Say the attendant is at the console, as the open page does."""
        await self._post("/api/presence", {"operator": OPERATOR})


# =========================================================================
# Time: simulated, or real
# =========================================================================


class World(Protocol):
    """What gives the console a second of life, and reads it."""

    @abstractmethod
    async def second(self) -> None:
        """One second of the console's life."""

    @abstractmethod
    async def sample(self, at: float) -> Sample:
        """One reading of the process, with the ticks timed since the previous one."""

    @abstractmethod
    async def records(self) -> Mapping[str, Footprint]:
        """What every record directory takes on the disk, by name."""


class SimulatedWorld:
    """A manual clock: the scenario runs every step the console's tasks would run.

    The journal is written through between two ticks, not by its thread: on a
    clock that runs faster than real time the thread would fall behind by
    design, and what it would show is its backlog.
    """

    __slots__ = ("_clock", "_console", "_sampler", "_screen", "_window", "whole")

    def __init__(self, console: Console, clock: ManualClock) -> None:
        self._console: Console = console
        self._clock: ManualClock = clock
        self._screen: Screen = Screen(console.panel.hub.subscribe())
        self._sampler: Sampler = Sampler(console.folder)
        self._window: Histogram = Histogram()
        self.whole: Histogram = Histogram()
        """Every control tick of the run."""

    @property
    def screen(self) -> Screen:
        """The page that reads the hub."""
        return self._screen

    async def second(self) -> None:
        """Five ticks 200 ms apart, then what the console does once a second."""
        panel = self._console.panel
        for _ in range(TICKS_PER_SECOND):
            self._clock.advance(TICK)
            await panel.ecg_step()
            began = time.perf_counter()
            await panel.control_step()
            spent = (time.perf_counter() - began) * 1000.0
            self._window.add(spent)
            self.whole.add(spent)
            await self._screen.read()
            self._console.journal.drain()
            await panel.cloud_stop_step()
        await panel.sensor_step()
        await panel.cloud_step()

    async def sample(self, at: float) -> Sample:
        """One reading, taken between two seconds: nothing else runs meanwhile."""
        tick = self._window.summary()
        self._window.reset()
        return self._sampler.take(at, tick)

    async def records(self) -> Mapping[str, Footprint]:
        """The records, walked between two seconds."""
        return self._console.folder.measure().records


class RealWorld:
    """The real clock: the console runs itself, the scenario only waits and reads.

    The readings are taken on a worker thread: walking the data folder on the
    loop would delay the very tick this run times.
    """

    __slots__ = ("_clock", "_console", "_due", "_sampler", "_screen", "_window", "periods", "whole")

    def __init__(self, console: Console, clock: RealClock) -> None:
        self._console: Console = console
        self._clock: RealClock = clock
        self._screen: Screen = Screen(console.panel.hub.subscribe())
        self._sampler: Sampler = Sampler(console.folder)
        self._window: Histogram = Histogram()
        self._due: float = time.monotonic()
        self.whole: Histogram = Histogram()
        """Every control tick the page was shown."""

        self.periods: Histogram = Histogram()
        """The time between two of them."""

    @property
    def screen(self) -> Screen:
        """The page that reads the hub."""
        return self._screen

    async def meter(self) -> None:
        """Time every tick from the snapshot it publishes, until the page is evicted."""
        previous: Monotonic | None = None
        while True:
            snapshot = await self._screen.read()
            if snapshot is None:
                return
            spent = (self._clock.monotonic() - snapshot.at) * 1000.0
            self._window.add(spent)
            self.whole.add(spent)
            if previous is not None:
                self.periods.add((snapshot.at - previous) * 1000.0)
            previous = snapshot.at

    async def second(self) -> None:
        """Wait for the next whole second of the run: the seconds counted do not drift."""
        self._due += 1.0
        await asyncio.sleep(max(0.0, self._due - time.monotonic()))

    async def sample(self, at: float) -> Sample:
        """One reading, off the loop."""
        tick = self._window.summary()
        self._window.reset()
        return await asyncio.to_thread(self._sampler.take, at, tick)

    async def records(self) -> Mapping[str, Footprint]:
        """The records, walked off the loop."""
        return (await asyncio.to_thread(self._console.folder.measure)).records


# =========================================================================
# The day
# =========================================================================


@dataclass(frozen=True, slots=True)
class Ended:
    """A session that finished, waiting for the dashboard to hold it."""

    index: int
    kind: Kind
    started_at: int
    recorded: int
    end: str
    as_planned: bool
    record: str | None
    """The name of its record directory."""


class Day:
    """Sessions chained until the run is over, and what became of each.

    Mutable, owned by the scenario's loop.
    """

    __slots__ = (
        "_console",
        "_dsp",
        "_ended",
        "_operator",
        "_plan",
        "_requested",
        "_screen",
        "_world",
        "duplicates",
        "elapsed",
        "heartbeats",
        "points",
        "samples",
        "sessions",
    )

    def __init__(
        self,
        console: Console,
        operator: Operator,
        world: World,
        screen: Screen,
        plan: Plan,
        *,
        requested: int,
        dsp: GatedDsp | None,
    ) -> None:
        """``dsp``: the switch of the heart-rate DSP when the scenario owns it, else ``None``."""
        self._console: Console = console
        self._operator: Operator = operator
        self._world: World = world
        self._screen: Screen = screen
        self._plan: Plan = plan
        self._requested: int = requested
        self._dsp: GatedDsp | None = dsp
        self._ended: Ended | None = None
        self.elapsed: int = 0
        self.samples: list[Sample] = []
        self.sessions: list[Session] = []
        self.heartbeats: int = 0
        self.points: int = 0
        self.duplicates: int = 0

    def over(self) -> bool:
        """Whether the time asked for has been covered. A method: read afresh after every wait."""
        return self.elapsed >= self._requested

    async def attempt(self) -> str | None:
        """Run the day. Returns what stopped it short, or ``None`` when nothing did."""
        try:
            await self._operator.ready()
            await self._operator.attest()
            while not self.over():
                await self._manual()
                await self._rest(before_programme=True)
                if self.over():
                    break
                await self._programme()
                await self._rest(before_programme=False)
            await self._closing()
        except AnomalyError as error:
            return str(error)
        return None

    async def _second(self) -> None:
        await self._world.second()
        await self._operator.ping()
        self.elapsed += 1
        if self.elapsed % self._plan.sample_every == 0:
            sample = await self._world.sample(float(self.elapsed))
            self.samples.append(sample)
            # Said as the run goes: hours of silence would hide a run that is stuck, and a
            # run stopped at its time limit would leave nothing of what it had measured.
            sys.stderr.write(progress(sample))

    async def _wait(self, seconds: int) -> None:
        """Up to ``seconds`` of the console's life: fewer when the run is over first."""
        for _ in range(seconds):
            if self.over():
                return
            await self._second()

    def _state(self) -> RuntimeState:
        """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
        return self._console.panel.runtime.state

    async def _started(self, what: str) -> None:
        """Give the loop time to take a start, then require the session to be running."""
        for _ in range(COMMAND_SETTLE):
            await self._second()
        if self._state() is not RuntimeState.RUNNING:
            raise AnomalyError(
                f"{what} not running {COMMAND_SETTLE} s after its start "
                f"(state {self._state().value}; last refusal: {self._screen.refusal})"
            )

    async def _finished(self, limit: int, *, cut: bool) -> bool:
        """Wait for the session to finish. Returns whether the operator had to cut it short.

        ``cut``: stop it once the run is over, instead of waiting for its own end.
        """
        stopped = False
        for _ in range(limit):
            if self._state() is RuntimeState.FINISHED:
                return stopped
            if cut and not stopped and self.over():
                await self._operator.stop("fin de l'essai")
                stopped = True
            await self._second()
        raise AnomalyError(
            f"session still {self._state().value} {limit} s after it should have ended"
        )

    async def _manual(self) -> None:
        started = self.elapsed
        await self._operator.start_manual()
        await self._started("manual session")
        for output_rpm in MANUAL_TARGETS:
            if self.over():
                break
            await self._operator.target(output_rpm)
            await self._wait(self._plan.manual // len(MANUAL_TARGETS))
        await self._operator.stop("fin de la séance manuelle")
        await self._finished(FINISH_LIMIT, cut=False)
        self._note_end(Kind.MANUAL, started, EndReason.OPERATOR_STOP)

    async def _programme(self) -> None:
        started = self.elapsed
        await self._operator.start_programme(self._plan.programme)
        await self._started("programmed session")
        cut = await self._finished(PROGRAMME_LIMIT, cut=True)
        self._note_end(
            Kind.PROGRAMME,
            started,
            EndReason.OPERATOR_STOP if cut else EndReason.PROGRAMME_COMPLETE,
        )
        self._heart_rate(on=False)

    def _heart_rate(self, *, on: bool) -> None:
        dsp = self._dsp
        if dsp is not None:
            dsp.on = on

    def _note_end(self, kind: Kind, started: int, planned: EndReason) -> None:
        runtime = self._console.panel.runtime
        reason = runtime.end_reason
        record = self._console.journal.status(self._console.clock.monotonic()).path
        self._ended = Ended(
            index=len(self.sessions) + 1,
            kind=kind,
            started_at=started,
            recorded=self.elapsed - started,
            end="none" if reason is None else reason.value,
            as_planned=reason is planned,
            record=None if record is None else record.name,
        )

    async def _rest(self, *, before_programme: bool) -> None:
        """The rest after a session; the session is then looked for on the dashboard."""
        lead = min(DSP_LEAD, self._plan.rest) if before_programme else 0
        await self._wait(self._plan.rest - lead)
        if before_programme:
            self._heart_rate(on=True)
        await self._wait(lead)
        ended = self._ended
        if ended is not None and not self.over():
            await self._deliver(ended)

    def _held(self, ended: Ended) -> tuple[bool, int]:
        """Whether the dashboard holds ``ended`` closed and whole, and how many points of it."""
        dashboard = self._console.dashboard
        name = f"SESSION-{ended.index}"
        session = dashboard.sessions.get(name)
        points = sum(1 for key in dashboard.telemetry if key[0] == name)
        closed = session is not None and session.status != "active"
        return closed and points >= ended.recorded - POINTS_MISSING, points

    async def _deliver(self, ended: Ended) -> None:
        """Write the line of the session that last ended, then empty the simulated dashboard."""
        self._ended = None
        delivered, points = self._held(ended)
        records = await self._world.records()
        self.points += points
        self.sessions.append(
            Session(
                index=ended.index,
                kind=ended.kind,
                started_at=float(ended.started_at),
                recorded=float(ended.recorded),
                end=ended.end,
                as_planned=ended.as_planned,
                delivered=delivered,
                points=points,
                record=records.get(ended.record or "", Footprint()),
            )
        )
        self._prune()

    def _prune(self) -> None:
        """Forget what the dashboard holds of closed sessions: the double must not grow.

        A real dashboard is another machine. This one lives in the process
        whose memory is being judged, and keeps every point and every request.
        """
        dashboard = self._console.dashboard
        self.heartbeats += dashboard.paths.count(HEARTBEAT)
        self.duplicates = dashboard.duplicates
        dashboard.paths.clear()
        dashboard.trace.clear()
        closed = {name for name, held in dashboard.sessions.items() if held.status != "active"}
        for store in (dashboard.telemetry, dashboard.events):
            for key in [key for key in store if key[0] in closed]:
                del store[key]

    async def _closing(self) -> None:
        """Once the run is over: give the last session the time to reach the dashboard."""
        ended = self._ended
        if ended is None:
            return
        # No programme will follow: the heart-rate DSP the last rest switched on is not needed.
        self._heart_rate(on=False)
        for _ in range(CLOSING_LIMIT):
            if self._held(ended)[0] and self.owed == 0:
                break
            await self._second()
        await self._deliver(ended)

    @property
    def owed(self) -> int:
        """Sessions or refusals the console still owes the dashboard."""
        cloud = self._console.panel.cloud
        return 0 if cloud is None else cloud.owed

    def sync(self) -> Sync:
        """What reached the dashboard, to be read once the day is over."""
        return Sync(
            heartbeats=self.heartbeats,
            points=self.points,
            rejected=self._console.dashboard.rejected,
            duplicates=self.duplicates,
            owed=self.owed,
        )


# =========================================================================
# The two runs
# =========================================================================


def free_port() -> int:
    """A loopback port the system says is free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]  # pyright: ignore[reportAny]
    return port


def working_folder(out: Path) -> Path:
    """A new folder under ``out`` for the console's data: nothing of a previous run is in it."""
    out.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="data-", dir=out))


def leave(work: Path, *, keep: bool) -> None:
    """Remove the console's data once it has been measured, unless asked to keep it."""
    if not keep:
        shutil.rmtree(work, ignore_errors=True)


async def simulated(seconds: int, out: Path, plan: Plan, *, keep_data: bool = False) -> Run:
    """``seconds`` of the console's life on a manual clock. See the module docstring."""
    began = time.perf_counter()
    spent = time.process_time()
    work = working_folder(out)
    clock = ManualClock(Monotonic(10.0), UnixMillis(EPOCH_MS))
    always = plan.dsp is Dsp.ALWAYS
    dsp = GatedDsp(on=always)
    console = build_console(work, clock, treat=dsp, port=IDLE_PORT)
    world = SimulatedWorld(console, clock)
    app = create_app(services=console.panel.services, config=console.config.web)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://console"
    ) as http:
        day = Day(
            console,
            Operator(http),
            world,
            world.screen,
            plan,
            requested=seconds,
            dsp=None if always else dsp,
        )
        anomaly = await day.attempt()
    sync = day.sync()
    await console.panel.close()
    data = console.folder.measure()
    leave(work, keep=keep_data)
    return Run(
        mode=Mode.SIMULATED,
        requested=float(seconds),
        covered=float(day.elapsed),
        wall=time.perf_counter() - began,
        cpu=time.process_time() - spent,
        samples=tuple(day.samples),
        tick=world.whole.summary(),
        sessions=tuple(day.sessions),
        sync=sync,
        data=data,
        anomaly=anomaly,
        notes=(*NOTES, NOTE_MANUAL_CLOCK)
        if always
        else (*NOTES, NOTE_MANUAL_CLOCK, NOTE_GATED_DSP),
    )


async def real_time(seconds: int, out: Path, plan: Plan, *, keep_data: bool = False) -> Run:
    """``seconds`` of the console running itself on the real clock. See the module docstring."""
    began = time.perf_counter()
    spent = time.process_time()
    work = working_folder(out)
    clock = RealClock()
    port = free_port()
    console = build_console(work, clock, treat=treat_off_loop, port=port)
    world = RealWorld(console, clock)
    stop = asyncio.Event()
    panel = console.panel
    served = asyncio.create_task(panel.run(stop, UvicornRunner(panel.services, console.config)))
    meter = asyncio.create_task(world.meter())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            day = Day(
                console, Operator(http), world, world.screen, plan, requested=seconds, dsp=None
            )
            anomaly = await day.attempt()
        sync = day.sync()
    finally:
        # The console's own exit: it stops the drive, closes the record, ends its tasks.
        stop.set()
        code = await served
        meter.cancel()
        await asyncio.gather(meter, return_exceptions=True)
    data = console.folder.measure()
    leave(work, keep=keep_data)
    left = None if code == 0 else f"the console left with exit code {code}"
    return Run(
        mode=Mode.REAL_TIME,
        requested=float(seconds),
        covered=float(day.elapsed),
        wall=time.perf_counter() - began,
        cpu=time.process_time() - spent,
        samples=tuple(day.samples),
        tick=world.whole.summary(),
        period=world.periods.summary(),
        sessions=tuple(day.sessions),
        sync=sync,
        data=data,
        anomaly=anomaly or left,
        notes=(*NOTES, NOTE_REAL_TICK),
    )


# =========================================================================
# The command line
# =========================================================================


class _Args(argparse.Namespace):
    """The parsed command line, typed (a bare ``Namespace`` is all ``Any``)."""

    mode: str = ""
    hours: float = 24.0
    minutes: float = 120.0
    manual: int | None = None
    rest: int | None = None
    sample_every: int | None = None
    dsp: str = Dsp.PROGRAMMES.value
    out: Path = OUT_DIR
    keep_data: bool = False
    readings: Path = Path()


def _plan(args: _Args, default: Plan) -> Plan:
    """``default`` with what the command line overrides."""
    return Plan(
        manual=default.manual if args.manual is None else args.manual,
        rest=default.rest if args.rest is None else args.rest,
        sample_every=default.sample_every if args.sample_every is None else args.sample_every,
        dsp=Dsp(args.dsp),
    )


def _run(args: _Args) -> Run:
    if args.mode == "simulated":
        seconds = round(args.hours * 3600)
        return asyncio.run(
            simulated(seconds, args.out, _plan(args, Plan()), keep_data=args.keep_data)
        )
    if args.mode == "real-time":
        seconds = round(args.minutes * 60)
        return asyncio.run(
            real_time(seconds, args.out, _plan(args, REAL_TIME_PLAN), keep_data=args.keep_data)
        )
    return observed(samples_from_csv(args.readings.read_text(encoding="utf-8")), (NOTE_OBSERVED,))


def _rhythm(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manual", type=int, help="seconds a manual session stays at its targets")
    parser.add_argument("--rest", type=int, help="seconds of rest after each session")
    parser.add_argument("--sample-every", type=int, help="seconds between two readings")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="where the report is written")
    parser.add_argument(
        "--keep-data", action="store_true", help="leave the console's data folder in place"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simulation.endurance",
        description="Keep the real console alive for hours and judge what it cost.",
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    sim = modes.add_parser("simulated", help="hours on a manual clock")
    sim.add_argument("--hours", type=float, default=24.0, help="simulated hours (default 24)")
    sim.add_argument(
        "--dsp",
        choices=[choice.value for choice in Dsp],
        default=Dsp.PROGRAMMES.value,
        help="when the heart-rate DSP runs (default: around the programmed sessions)",
    )
    _rhythm(sim)
    real = modes.add_parser("real-time", help="minutes on the real clock, on a loopback port")
    real.add_argument("--minutes", type=float, default=120.0, help="real minutes (default 120)")
    _rhythm(real)
    bench = modes.add_parser("judge", help="judge readings taken from outside a console")
    bench.add_argument(
        "readings", type=Path, help="a CSV: epoch_s,rss_kb,descriptors,data_kb,data_inodes"
    )
    bench.add_argument("--out", type=Path, default=OUT_DIR, help="where the report is written")
    args = parser.parse_args(argv, namespace=_Args())
    args.out.mkdir(parents=True, exist_ok=True)
    # What the console says goes next to the report, not between its lines.
    logging.basicConfig(
        filename=args.out / "console.log", level=logging.WARNING, format="%(asctime)s %(message)s"
    )
    run = _run(args)
    checks = judge(run)
    data, summary = write_report(run, checks, args.out)
    sys.stdout.write(markdown(run, checks))
    sys.stdout.write(f"\nreport: {summary}  data: {data}\n")
    return 0 if passed(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
