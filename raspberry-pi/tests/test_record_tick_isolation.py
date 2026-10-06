"""The control tick never waits on the session record: ANH-128 EX-2, measured and proven.

Three angles on the one safety requirement of the black box:

* **measured**: the same console, ticked with and without the writer active,
  takes the same time per tick (the difference of the medians is far under a
  millisecond; the supervisor's ``loop_stall`` rule acts at 600 ms);
* **structural**: while a recorded session ticks, the thread that runs the
  loop opens, creates, renames, truncates and flushes NOTHING under the
  records directory. Every one of those is the journal thread's;
* **adversarial**: with a disk that does not answer at all, the real task
  loop goes on ticking at 5 Hz, no ``loop_stall`` is ever raised, the operator
  is told the record is degraded, and the session ends when asked.
"""

from __future__ import annotations

import asyncio
import statistics
import threading
import time
from itertools import pairwise
from pathlib import Path
from typing import Final, Literal

import pytest

import src.record.journal as journal_module
import src.record.writer as writer_module
from src.clock import ManualClock, RealClock
from src.control_surface import EventKind
from src.local_panel import EXIT_OK, DriveSide, LocalPanel, build_panel
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.journal import Cause, Journal, Limits
from src.record.reader import read
from src.result import Ok
from src.telemetry import PayloadKind
from src.training.runtime import EndReason, RuntimeState
from src.training.safety import RULE_LOOP_STALL
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, Seconds, UnixMillis
from tests.record_journal_support import count_fsync, journal_threads, wait_for, watch_tree
from tests.test_failure_process import FakeWeb
from tests.test_failure_rig import BENCH_ENV, OPERATOR, TICK, Rig, config_of, make_rig, no_dsp

BLOCKS: Final[int] = 6
TICKS_PER_BLOCK: Final[int] = 250

SAME: Final[float] = 0.001
"""Seconds. "The tick does not change": the medians differ by less than this."""

NEVER_NEAR_A_STALL: Final[float] = 0.3
"""Seconds. No single tick, writer or not, may come near the 0.6 s ``loop_stall`` FREEZE."""


async def cruising(tmp_path: Path, name: str, *, record: bool) -> tuple[Rig, Journal | None]:
    """A console climbing in a bench manual session; with its journal THREAD when ``record``."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    # This test ticks hundreds of times faster than real time: the bounds that fit a
    # real session are raised so the writer has every tick to write, not a sample of them.
    roomy = Limits(entries=1_000_000, samples=100_000_000)
    journal = (
        Journal(tmp_path / name / "records", clock, limits=roomy, period=Seconds(0.005))
        if record
        else None
    )
    rig, _ = make_rig(tmp_path / name, clock=clock, journal=journal)
    surface = rig.panel.surface
    assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await rig.tick(1.0)
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(20.0), operator=OPERATOR), Ok
    )
    await rig.tick(2.0)
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    return rig, journal


async def timed_ticks(rig: Rig, count: int) -> list[float]:
    """``count`` control ticks as the loop runs them; the duration of each ``control_step``."""
    panel = rig.panel
    spent: list[float] = []
    for index in range(count):
        rig.clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.ecg_step()
        if index % 5 == 0:
            await panel.sensor_step()
        started = time.perf_counter()
        await panel.control_step()
        spent.append(time.perf_counter() - started)
    return spent


async def test_ex2_the_tick_takes_the_same_time_with_the_writer_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A manual clock runs far ahead of the journal thread's real one: without
    # this the stall detector would call a few milliseconds of backlog a stall.
    monkeypatch.setattr(journal_module, "STALL_AFTER", Seconds(1e9))
    (tmp_path / "plain").mkdir()
    (tmp_path / "recorded").mkdir()
    plain, _ = await cruising(tmp_path, "plain", record=False)
    recorded, journal = await cruising(tmp_path, "recorded", record=True)
    assert journal is not None
    journal.start()

    without: list[float] = []
    with_writer: list[float] = []
    for _ in range(BLOCKS):  # interleaved, so a slow moment of the machine hits both
        without.extend(await timed_ticks(plain, TICKS_PER_BLOCK))
        with_writer.extend(await timed_ticks(recorded, TICKS_PER_BLOCK))

    wait_for(lambda: journal.status(recorded.clock.monotonic()).pending == 0, timeout=30.0)
    status = journal.status(recorded.clock.monotonic())
    assert journal.stop()
    assert not status.degraded, status
    path = status.path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert len(loaded.value.rows) >= BLOCKS * TICKS_PER_BLOCK, "the writer really was active"
    assert len(loaded.value.raw) >= BLOCKS * TICKS_PER_BLOCK

    median_without = statistics.median(without)
    median_with = statistics.median(with_writer)
    p99_without = statistics.quantiles(without, n=100)[98]
    p99_with = statistics.quantiles(with_writer, n=100)[98]
    summary = (
        f"control tick, {len(without)} ticks each: median {median_without * 1e6:.0f} us without "
        f"the writer, {median_with * 1e6:.0f} us with; p99 {p99_without * 1e6:.0f} us without, "
        f"{p99_with * 1e6:.0f} us with; max {max(without) * 1e6:.0f} us without, "
        f"{max(with_writer) * 1e6:.0f} us with"
    )
    print(summary)  # noqa: T201 - the measurement the ticket asks for, shown with -s
    assert median_with - median_without < SAME, summary
    assert max(with_writer) < NEVER_NEAR_A_STALL, summary
    await plain.panel.close()
    await recorded.panel.close()


async def test_ex2_no_tick_touches_the_records_directory_or_calls_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(journal_module, "STALL_AFTER", Seconds(1e9))
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock, period=Seconds(0.005))
    rig, _ = make_rig(tmp_path, clock=clock, journal=journal)
    surface = rig.panel.surface
    loop_thread = threading.get_ident()
    fsyncs = count_fsync(monkeypatch)

    with watch_tree(journal.root) as touches:
        journal.start()
        writing = journal_threads()[0].ident
        assert writing is not None
        assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        await rig.tick(1.0)
        assert isinstance(
            surface.submit_manual_target(output_rpm=OutputRpm(5.0), operator=OPERATOR), Ok
        )
        for _ in range(100):
            await rig.tick(0.2)
            await rig.panel.sensor_step()
        surface.submit_end(operator=OPERATOR, reason="done")
        await rig.tick(40.0)
        assert rig.panel.runtime.state is RuntimeState.FINISHED
        wait_for(lambda: journal.status(clock.monotonic()).pending == 0, timeout=30.0)
        await rig.panel.close()  # stops the journal thread, off the loop

    assert touches.by(loop_thread) == [], "the loop's thread touched the records directory"
    assert loop_thread not in fsyncs
    assert set(fsyncs) == {writing}
    by_journal = touches.by(writing)
    assert "os.mkdir" in by_journal
    assert by_journal.count("open") > 500, "rows, events and raw blocks were all written there"
    path = journal.status(clock.monotonic()).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == ()
    assert loaded.value.manifest.end_reason == "operator_stop"


# =========================================================================
# A disk that does not answer, under the real task loop
# =========================================================================


class StuckDisk:
    """Every write blocks until released: an SD card that stopped answering."""

    def __init__(self) -> None:
        self.release: threading.Event = threading.Event()
        self.blocked: threading.Event = threading.Event()
        self._real = writer_module.write_file

    def write(self, path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        self.blocked.set()
        self.release.wait(60.0)
        self._real(path, content, mode)


async def test_ex3_a_disk_that_never_answers_stalls_the_record_and_never_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = RealClock()
    config = config_of(BENCH_ENV)
    sim = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    journal = Journal(tmp_path / "records", clock)
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=sim, simulator=sim, release=lambda: None),
        journal=journal,
    )
    disk = StuckDisk()
    monkeypatch.setattr(writer_module, "write_file", disk.write)
    watcher = panel.hub.subscribe()
    stop = asyncio.Event()
    seen: list[float] = []
    said: list[str] = []

    async def watch() -> None:
        """What the operator's page receives: every snapshot's instant, every record message."""
        while True:
            payload = await watcher.next_payload()
            if payload.kind is PayloadKind.SNAPSHOT and payload.snapshot is not None:
                seen.append(float(payload.snapshot.at))
            if payload.kind is PayloadKind.EVENT and payload.event is not None:
                event = payload.event
                if event.kind is EventKind.RECORDING:
                    said.append(event.detail)

    async def operate(panel: LocalPanel) -> tuple[RuntimeState, Cause | None, bool]:
        surface = panel.surface
        await asyncio.sleep(0.3)
        assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
        assert isinstance(
            surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
        )
        for _ in range(35):  # 7 s: longer than the 5 s after which a stall is declared
            surface.note_presence(OPERATOR)
            await asyncio.sleep(0.2)
        recorder = panel.recorder
        assert recorder is not None
        state = panel.runtime.state
        cause = recorder.status.cause
        stuck = disk.blocked.is_set()
        surface.submit_end(operator=OPERATOR, reason="done")
        for _ in range(15):
            surface.note_presence(OPERATOR)
            await asyncio.sleep(0.2)
        stop.set()
        return state, cause, stuck

    watching = asyncio.create_task(watch())
    try:
        (state, cause, stuck), code = await asyncio.gather(
            operate(panel), panel.run(stop, FakeWeb(None))
        )
    finally:
        watching.cancel()
        disk.release.set()

    assert stuck, "the journal thread never reached the stuck disk"
    assert state is RuntimeState.RUNNING, "the session stopped because of the record"
    assert cause is Cause.STALLED
    assert len(said) == 1
    assert said[0].startswith("enregistrement de seance degrade : le disque ne repond plus")
    # The loop never stalled: 5 Hz throughout, and the supervisor never said otherwise.
    gaps = [later - earlier for earlier, later in pairwise(seen)]
    assert len(seen) > 40
    assert max(gaps) < 0.5, f"a tick came {max(gaps):.3f} s after the previous one"
    assert panel.runtime.end_reason is EndReason.OPERATOR_STOP
    standing = panel.runtime.standing
    assert standing is None or standing.rule != RULE_LOOP_STALL
    assert code == EXIT_OK
    # The console's exit waited 5 s for the record at most, then left: the thread is a daemon.
    assert "session record not finalised: the disk did not answer in time" in caplog.text
    wait_for(lambda: journal_threads() == [], timeout=30.0)
