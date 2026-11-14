"""ANH-239: the sessions are sent to the dashboard by a task of their own.

A record read takes up to its bound, an upload up to the transport's, and a
catching up chains a few of each. Stepped from the dashboard step, the sending
held that step: the heartbeat, the programmes and the poll for a launch waited
behind it. Stepped from a task of its own (:meth:`~src.cloud_sync.CloudSync.send_records`,
:meth:`~src.local_panel.LocalPanel.records_step`), it holds nothing else:

* on the test's clock, a session caught up at rest whose declaration takes 45 s
  to be answered leaves the heartbeat and the poll at their periods;
* the same world, the sending stepped from the dashboard step as before, shows
  the heartbeat and the poll waiting behind it;
* on the real console, a sending step that never returns holds neither the
  dashboard step nor the stop watch, and one still awaited at exit holds
  neither the exit nor the stop of the drive.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from src.clock import ManualClock
from src.cloud_sync import HEARTBEAT_PERIOD, POLL_PERIOD, CloudSync
from src.local_panel import CLOUD_PERIOD, EXIT_OK, STOP_WATCH_PERIOD, LocalPanel
from src.record_uplink import RecordSource
from tests.record_uplink_support import BOOT, Disk, recording, tie
from tests.test_cloud_journal_wiring import SlowDashboard, SlowIo, Waits
from tests.test_cloud_sync import POLL_PATH, Dashboard, StubRuntime, config_of, ok, rig
from tests.test_failure_process import LINKED_ENV, cruising
from tests.test_failure_rig import make_rig
from tests.test_local_panel import FakeWeb

HEARTBEAT: Final[str] = "/api/machine/heartbeat"
LOCAL: Final[str] = "/api/machine/training/local"

DECLARATION_S: Final[float] = 45.0
"""How long the declaration of the session caught up takes: far longer than any period."""


@dataclass(frozen=True)
class Measured:
    """What the link asked for, and when, on the test's clock."""

    made: tuple[tuple[float, str], ...]
    at_end: float

    def at(self, path: str) -> list[float]:
        return [round(at, 3) for at, called in self.made if called == path]


async def measured(tmp_path: Path, *, seconds: int, one_task: bool = False) -> Measured:
    """``seconds`` at rest, a closed session on the disk to catch up, its declaration slow.

    The dashboard step and the sending once a second, the stop watch twenty
    times a second, each a task of its own. With ``one_task`` the sending is
    stepped from the dashboard task, right after its step, as it was before.
    """
    clock = ManualClock()
    waits = Waits(clock)
    dashboard = SlowDashboard(waits, 0.0)
    dashboard.slow[LOCAL] = DECLARATION_S
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    root = tmp_path / "records"
    root.mkdir()
    older = recording(root, clock, "ref-older")
    older.tick(5.0)
    older.close()
    tie(older.path)  # tied to its cursor by an earlier run, never declared
    panel = rig(tmp_path).panel
    sync = CloudSync(
        clock=clock,
        transport=dashboard,
        runtime=StubRuntime(source=panel.runtime.snapshot),
        surface=panel.surface,
        store=panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        records=RecordSource(
            root=root,
            current=Disk(root).in_progress,
            io=SlowIo(waits, 0.0),
            boot_id=BOOT,
            logbook=print,
        ),
    )

    async def dashboard_step() -> None:
        await sync.step()
        if one_task:
            await sync.send_records()

    async def every(period: float, step: Callable[[], Awaitable[None]]) -> None:
        """As ``LocalPanel._every`` does, on the test's clock."""
        while True:
            began = float(clock.monotonic())
            await step()
            await waits.sleep(max(0.0, period - (float(clock.monotonic()) - began)))

    tasks = [
        asyncio.create_task(every(float(CLOUD_PERIOD), dashboard_step)),
        asyncio.create_task(every(float(STOP_WATCH_PERIOD), sync.watch_stop)),
    ]
    if not one_task:
        tasks.append(asyncio.create_task(every(float(CLOUD_PERIOD), sync.send_records)))
    try:
        for _ in range(12):  # every task makes its first pass at 0
            await asyncio.sleep(0)
        for tick in range(seconds * 20):
            # To the exact twentieth: a sum of 0.05 drifts below the second.
            await waits.advance((tick + 1) / 20 - float(clock.monotonic()))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return Measured(tuple(dashboard.made), float(clock.monotonic()))


def every_period(period: float, until: float) -> list[float]:
    """0, ``period``, ``2 * period``... up to ``until``: a cadence kept to the second."""
    return [round(n * period, 3) for n in range(int(until // period) + 1)]


def gaps(instants: list[float]) -> list[float]:
    return [round(later - earlier, 3) for earlier, later in pairwise(instants)]


# =========================================================================
# On the test's clock
# =========================================================================


async def test_a_slow_sending_holds_neither_the_heartbeat_nor_the_poll(tmp_path: Path) -> None:
    seen = await measured(tmp_path, seconds=130)

    declared = seen.at(LOCAL)
    assert declared, "the session on the disk was never declared"
    assert declared[0] < DECLARATION_S, "its declaration was still awaited meanwhile"
    assert seen.at(HEARTBEAT) == every_period(float(HEARTBEAT_PERIOD), seen.at_end)
    assert seen.at(POLL_PATH) == every_period(float(POLL_PERIOD), seen.at_end)


async def test_stepped_from_the_dashboard_step_a_slow_sending_would_hold_them(
    tmp_path: Path,
) -> None:
    """What the task of its own is for: the same world, the sending stepped where it was."""
    seen = await measured(tmp_path, seconds=130, one_task=True)

    beats = seen.at(HEARTBEAT)
    polls = seen.at(POLL_PATH)
    assert max(gaps(beats)) > 2 * float(HEARTBEAT_PERIOD), gaps(beats)
    assert max(gaps(polls)) > 10 * float(POLL_PERIOD), gaps(polls)


# =========================================================================
# On the real console
# =========================================================================


async def test_on_the_console_a_sending_that_never_returns_holds_neither_the_link_nor_the_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = Dashboard()
    rig_, _ = make_rig(tmp_path, env=LINKED_ENV, transport=lambda _config: dashboard)
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    stepping, watching = LocalPanel.cloud_step, LocalPanel.cloud_stop_step
    steps: list[float] = []
    looks: list[float] = []

    async def never_returns(_sync: CloudSync) -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cleaned.set()

    async def counted_step(panel: LocalPanel) -> None:
        steps.append(time.perf_counter())
        await stepping(panel)

    async def counted_look(panel: LocalPanel) -> None:
        looks.append(time.perf_counter())
        await watching(panel)

    monkeypatch.setattr(CloudSync, "send_records", never_returns)
    monkeypatch.setattr(LocalPanel, "cloud_step", counted_step)
    monkeypatch.setattr(LocalPanel, "cloud_stop_step", counted_look)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        hung_at = time.perf_counter()
        await asyncio.sleep(1.5 * float(CLOUD_PERIOD))
        assert not cleaned.is_set()
        assert [at for at in steps if at > hung_at], "a dashboard step while the sending hung"
        assert dashboard.to(HEARTBEAT)
        assert len([at for at in looks if at > hung_at]) >= 10
        stop.set()
        code = await asyncio.wait_for(runner, 2)
        assert code == EXIT_OK
        assert cleaned.is_set(), "the sending was cancelled at exit"
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()


async def test_a_sending_still_awaited_at_exit_holds_neither_the_exit_nor_the_stop_of_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The motor turning, the sending waiting on a request that never ends: the console stops
    the drive and exits without waiting for it."""
    rig_, _ = make_rig(tmp_path, env=LINKED_ENV, transport=lambda _config: Dashboard())
    await cruising(rig_)
    assert rig_.simulator.commanded_setpoint > 0
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def never_answered(_sync: CloudSync) -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(CloudSync, "send_records", never_answered)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop.set()
        code = await asyncio.wait_for(runner, 2)
        assert code == EXIT_OK
        assert cleaned.is_set()
        assert rig_.simulator.commanded_setpoint == 0
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()
