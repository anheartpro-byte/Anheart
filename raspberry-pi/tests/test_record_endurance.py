"""ANH-128 EX-10: hours of console in simulation with the writer active, no growth, no drift.

The real console (its control loop on the simulated drive, its recorder, the
session journal writing real files) runs session after session on a manual
clock: ten minutes of bench manual session, the operator's stop, five minutes
at rest, again. At the end of every cycle three things are measured: the
Python heap (``tracemalloc``, after a collection), the number of objects the
collector tracks, and what a control tick cost during the cycle (its first
decile: the tick itself, whatever else the machine was doing).

From the third cycle to the last:

* **nothing accumulates**: the number of tracked objects does not grow. A
  queue that is never emptied, a row kept per tick, a session never released
  would each add thousands of objects per cycle;
* **the heap does not grow cycle after cycle**: the median growth per cycle
  stays under :data:`PER_CYCLE` bytes. The median, because a leak grows on
  EVERY cycle, whereas the interpreter resizes one of its own tables once in a
  while, in one step of megabytes that is not a leak (measured here: the table
  of interned strings, which ``pathlib`` feeds with every path component, 3.8 MB
  in one allocation);
* **the tick does not slow down**.

What remains, and is accepted: about 1 kB per SESSION, strings the interpreter
keeps (a path component interned by ``pathlib`` is immortal on CPython 3.12:
one record directory name per session). A year of twenty sessions a day is
7 MB.

What is NOT run here is the acquisition's own computation: the simulated
BITalino renders its ECG sample by sample in Python and the sensor processors
filter ten-second windows, about 8 ms of CPU per simulated tick between them,
an hour for a simulated day. Neither is the writer's. The test hands the
recorder what they would have produced, at their cadence (one raw batch of two
channels per tick, one sensor publication per second), so every stream of the
record is written for the whole run. The whole console's endurance, with the
acquisition, is ANH-164.

The 24-hour run is the nightly one: it is marked ``slow`` and skipped unless
``ANHEART_ENDURANCE_HOURS`` is set (the CI's scheduled run sets it to 24). The
same loop runs for a few simulated minutes on every gate, so the nightly test
cannot rot unseen.

The journal is drained inline, between two ticks, not by its thread: on a
manual clock that runs hundreds of times faster than real time the thread
would fall behind by design, and what it would show is its backlog, not a
leak. The thread's own behaviour under the real clock is
``test_record_tick_isolation`` and ``test_record_abrupt_stop``.
"""

from __future__ import annotations

import gc
import logging
import os
import shutil
import statistics
import time
import tracemalloc
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from src.bitalino_client import ChannelData, SampleBatch
from src.clock import ManualClock
from src.local_panel import DriveSide, LocalPanel, build_panel
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
from src.result import Ok
from src.sensors.base import Metric, SensorKind, SensorReading
from src.training.runtime import RuntimeState
from src.training.types import Occupancy, SignalQuality
from src.units import Monotonic, OutputRpm, UnixMillis
from tests.test_failure_rig import BENCH_ENV, OPERATOR, TICK, config_of, no_dsp

HOURS_KEY: Final[str] = "ANHEART_ENDURANCE_HOURS"
NIGHTLY_HOURS: Final[float] = float(os.environ.get(HOURS_KEY, "0") or "0")

WARM_UP: Final[int] = 2
"""Cycles left out of every comparison: caches fill, the first records are created."""

PER_CYCLE: Final[int] = 5_000
"""Bytes. The median growth of the heap from one cycle to the next stays under this."""

OBJECTS: Final[int] = 2_000
"""Tracked objects. One object leaked per tick would be thousands per cycle."""

DRIFT: Final[float] = 1.5
"""The last cycles' tick may be this many times the first cycles', plus 100 us."""

RAW_ECG: Final[tuple[float, ...]] = tuple(float(500 + index % 24) for index in range(200))
RAW_EDA: Final[tuple[float, ...]] = tuple(float(300 + index % 7) for index in range(200))
"""One tick of acquisition, 200 samples a channel at 1000 Hz, in ADC counts."""

READINGS: Final[tuple[SensorReading, ...]] = (
    SensorReading(
        SensorKind.ECG,
        Monotonic(0.0),
        1,
        (),
        SignalQuality.GOOD,
        "",
        (Metric("heart_rate", "FC", 62.0, "bpm"), Metric("rr_mean", "RR", 968.0, "ms")),
    ),
    SensorReading(SensorKind.EDA, Monotonic(0.0), 1, (), SignalQuality.NO_SIGNAL, "", ()),
)
"""One publication of the processed channels, as the sensor hub hands them over."""


@dataclass(frozen=True, slots=True)
class Endurance:
    """What one run measured, one value per cycle."""

    heap: tuple[int, ...]
    """Traced Python heap after each cycle, bytes, after a collection."""

    objects: tuple[int, ...]
    """Objects tracked by the collector after each cycle."""

    tick: tuple[float, ...]
    """First decile of the control tick's duration during each cycle, seconds.

    The low end, not the median: another process taking the processor can only
    make a tick longer, so the fastest tenth is what the tick itself costs.
    """

    ticks: int

    @property
    def growth(self) -> float:
        """Median growth of the heap from one cycle to the next, after the warm-up, bytes."""
        return statistics.median(b - a for a, b in pairwise(self.heap[WARM_UP:]))

    @property
    def first_tick(self) -> float:
        return statistics.median(self.tick[WARM_UP : WARM_UP + 3])

    @property
    def last_tick(self) -> float:
        return statistics.median(self.tick[-3:])

    def describe(self) -> str:
        settled = self.objects[WARM_UP:]
        return (
            f"{len(self.heap)} sessions, {self.ticks} ticks; heap "
            f"{self.heap[WARM_UP] / 1e6:.2f} MB after warm-up, "
            f"{self.heap[-1] / 1e6:.2f} MB at the end, median growth "
            f"{self.growth:.0f} B per session; tracked objects {settled[0]} after warm-up, "
            f"{settled[-1]} at the end (highest {max(settled)}); tick "
            f"{self.first_tick * 1e6:.0f} us at first, {self.last_tick * 1e6:.0f} us at the end"
        )


class Console:
    """The real console on a manual clock, ticked as its loop does. Keeps nothing per tick."""

    def __init__(self, tmp_path: Path) -> None:
        self.clock: ManualClock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
        self.journal: Journal = Journal(tmp_path / "records", self.clock)
        sim = SimulatedDrive(self.clock, SimulatedDriveConfig(reads_reset_watchdog=True))
        self.panel: LocalPanel = build_panel(
            config_of(BENCH_ENV),
            clock=self.clock,
            profiles_path=tmp_path / "profiles.json",
            treat=no_dsp,
            drive=DriveSide(backend=sim, simulator=sim, release=lambda: None),
            journal=self.journal,
        )
        self.ticks: int = 0

    async def run(self, seconds: float, spent: list[float]) -> None:
        panel = self.panel
        recorder = panel.recorder
        assert recorder is not None
        for _ in range(round(seconds / TICK)):
            self.clock.advance(TICK)
            panel.surface.note_presence(OPERATOR)
            recorder.note_batch(
                SampleBatch(
                    timestamp=UnixMillis(self.clock.unix_millis() - 200),
                    channels=(ChannelData("ECG", RAW_ECG), ChannelData("EDA", RAW_EDA)),
                )
            )
            if self.ticks % 5 == 0:
                recorder.note_sensors(READINGS)
            started = time.perf_counter()
            await panel.control_step()
            spent.append(time.perf_counter() - started)
            self.journal.drain()
            self.ticks += 1

    def state(self) -> RuntimeState:
        """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
        return self.panel.runtime.state

    async def one_session(self, seconds: float, spent: list[float]) -> None:
        surface = self.panel.surface
        started = surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
        assert isinstance(started, Ok), started
        await self.run(1.0, spent)
        assert self.state() is RuntimeState.RUNNING
        assert isinstance(
            surface.submit_manual_target(output_rpm=OutputRpm(5.0), operator=OPERATOR), Ok
        )
        await self.run(seconds, spent)
        assert isinstance(surface.submit_end(operator=OPERATOR, reason="cycle"), Ok)
        for _ in range(30):
            await self.run(2.0, spent)
            if self.state() is RuntimeState.FINISHED:
                break
        assert self.state() is RuntimeState.FINISHED


def checked_and_removed(root: Path, *, read_back: bool) -> int:
    """The cycle's record is closed (and readable); it is then removed, so the disk stays small.

    Returns the number of ticks the record holds when it was read back, else 0.
    """
    (record,) = records(root)
    assert (record / "checksums.sha256").exists()
    rows = 0
    if read_back:
        loaded = read(record)
        assert isinstance(loaded, Ok), loaded
        assert loaded.value.warnings == ()
        assert loaded.value.manifest.end_reason == "operator_stop"
        assert len(loaded.value.raw) >= len(loaded.value.rows) - 1
        assert loaded.value.sensors
        rows = len(loaded.value.rows)
    shutil.rmtree(record)
    return rows


async def endure(tmp_path: Path, *, cycles: int, session_s: float, idle_s: float) -> Endurance:
    console = Console(tmp_path)
    assert isinstance(console.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    recorder = console.panel.recorder
    assert recorder is not None
    heap: list[int] = []
    objects: list[int] = []
    tick: list[float] = []
    # pytest keeps every log record of a test until it ends: with the console's
    # few lines per session, that is the one thing here that would grow.
    logging.disable(logging.CRITICAL)
    tracemalloc.start()
    try:
        for cycle in range(cycles):
            spent: list[float] = []
            await console.one_session(session_s, spent)
            await console.run(idle_s, spent)
            assert not recorder.degraded, recorder.status
            rows = checked_and_removed(console.journal.root, read_back=cycle in (0, cycles - 1))
            assert rows == 0 or rows >= session_s * 5
            tick.append(statistics.quantiles(spent, n=10)[0])
            del spent
            gc.collect()
            heap.append(tracemalloc.get_traced_memory()[0])
            objects.append(len(gc.get_objects()))
    finally:
        tracemalloc.stop()
        logging.disable(logging.NOTSET)
    await console.panel.close()
    return Endurance(
        heap=tuple(heap), objects=tuple(objects), tick=tuple(tick), ticks=console.ticks
    )


def judge(result: Endurance) -> None:
    summary = result.describe()
    print(summary)  # noqa: T201 - the measurement, for whoever runs this with -s
    settled = result.objects[WARM_UP:]
    assert max(settled[-3:]) - min(settled) < OBJECTS, summary
    assert result.growth < PER_CYCLE, summary
    assert result.last_tick < result.first_tick * DRIFT + 100e-6, summary


async def test_ex10_a_few_sessions_show_no_accumulation_and_no_drift(tmp_path: Path) -> None:
    """The nightly loop, short: six ten-second sessions with five seconds of rest between."""
    result = await endure(tmp_path, cycles=WARM_UP + 4, session_s=10.0, idle_s=5.0)
    assert len(result.heap) == WARM_UP + 4
    judge(result)


@pytest.mark.slow
@pytest.mark.skipif(NIGHTLY_HOURS <= 0, reason=f"nightly endurance: set {HOURS_KEY}=24")
async def test_ex10_a_day_of_console_in_simulation_shows_no_accumulation_and_no_drift(
    tmp_path: Path,
) -> None:
    session_s, idle_s = 600.0, 300.0
    cycles = max(WARM_UP + 4, round(NIGHTLY_HOURS * 3600 / (session_s + idle_s + 12.0)))
    result = await endure(tmp_path, cycles=cycles, session_s=session_s, idle_s=idle_s)
    assert result.ticks * float(TICK) >= NIGHTLY_HOURS * 3600 * 0.95, result.describe()
    judge(result)


def test_ex10_a_leak_and_a_slowing_tick_would_be_seen() -> None:
    """The judgement itself: it passes a steady run and refuses each of the three failures."""
    steady = Endurance(
        heap=(900_000, 990_000, 1_000_000, 1_000_900, 4_800_000, 4_800_800, 4_801_500),
        objects=(170_000, 171_100, 171_000, 171_000, 171_050, 171_000, 171_000),
        tick=(300e-6, 60e-6, 60e-6, 62e-6, 61e-6, 60e-6, 63e-6),
        ticks=7 * 3000,
    )
    judge(steady)  # one table resized in one step: not a leak
    leaking = Endurance(
        heap=tuple(1_000_000 + 40_000 * cycle for cycle in range(7)),
        objects=steady.objects,
        tick=steady.tick,
        ticks=steady.ticks,
    )
    with pytest.raises(AssertionError, match="median growth 40000 B"):
        judge(leaking)
    accumulating = Endurance(
        heap=steady.heap,
        objects=tuple(171_000 + 3_000 * cycle for cycle in range(7)),
        tick=steady.tick,
        ticks=steady.ticks,
    )
    with pytest.raises(AssertionError, match="tracked objects"):
        judge(accumulating)
    slowing = Endurance(
        heap=steady.heap,
        objects=steady.objects,
        tick=(300e-6, 60e-6, 60e-6, 60e-6, 200e-6, 400e-6, 800e-6),
        ticks=steady.ticks,
    )
    with pytest.raises(AssertionError, match="400 us at the end"):
        judge(slowing)
