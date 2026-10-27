"""ANH-239: the riders' list is asked for by a task of its own.

The list can take as long as the transport allows to come, or not come at
all. Asked from the sending step, it held that step: the heartbeat and the
poll for a launch waited behind it. Asked from a task of its own
(:meth:`~src.cloud_sync.CloudSync.refresh_roster`,
:meth:`~src.local_panel.LocalPanel.roster_step`), it holds nothing else:

* on the test's clock, a list that takes 45 s, or that never comes until the
  test lets it, leaves the heartbeat at its period, the poll at its period,
  and a session armed meanwhile is declared and its stop asked for at the
  stop watch's cadence; one request for the list is in flight at a time;
* the same world, the list asked from the sending step as before, shows the
  heartbeat and the poll waiting behind it;
* on the real console, a list that never comes holds neither the sending nor
  the stop watch, a sending step that never returns does not keep the list
  from being asked for, and a list still awaited at exit holds neither the
  exit nor the stop of the drive.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, override

import pytest

from src.clock import ManualClock
from src.cloud_sync import HEARTBEAT_PERIOD, POLL_PERIOD, STATUS_PERIOD, CloudSync
from src.local_panel import CLOUD_PERIOD, EXIT_OK, STOP_WATCH_PERIOD, LocalPanel
from src.record_uplink import RecordSource
from src.roster import ROSTER_PATH, ROSTER_PERIOD, Roster, RosterState
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.units import Monotonic
from tests.record_uplink_support import BOOT, Disk, Recorded, recording
from tests.roster_support import RIDER
from tests.test_cloud_journal_wiring import SlowIo, Waits
from tests.test_cloud_sync import (
    POLL_PATH,
    STATUS,
    Dashboard,
    Reply,
    StubRuntime,
    config_of,
    manual,
    ok,
    rig,
    status_answer,
)
from tests.test_failure_process import LINKED_ENV, cruising
from tests.test_failure_rig import make_rig
from tests.test_local_panel import FakeWeb

HEARTBEAT: Final[str] = "/api/machine/heartbeat"
LOCAL: Final[str] = "/api/machine/training/local"

LIST_S: Final[float] = 45.0
"""How long the slow list takes: far longer than any period of the link."""

LOOK: Final[float] = float(STOP_WATCH_PERIOD) + 0.011
"""How long after it may, at the latest, the stop watch does a thing: its next look."""


class Paced(Dashboard):
    """Every request answered at once on the test's clock, but the list: after ``list_s``,
    or, with ``list_s`` ``None``, once the test sets :attr:`held`. When each was made is kept.
    """

    def __init__(self, waits: Waits, list_s: float | None) -> None:
        super().__init__()
        self.waits: Waits = waits
        self.list_s: float | None = list_s
        self.held: asyncio.Event = asyncio.Event()
        self.made: list[tuple[float, str]] = []

    async def _answer_after(self, path: str) -> None:
        self.made.append((float(self.waits.clock.monotonic()), path))
        if path != ROSTER_PATH:
            await self.waits.sleep(0.0)
        elif self.list_s is None:
            await self.held.wait()
        else:
            await self.waits.sleep(self.list_s)

    @override
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        await self._answer_after(path)
        return await super().get(path, params)

    @override
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        await self._answer_after(path)
        return await super().post(path, body)


@dataclass(frozen=True)
class Measured:
    """What the link asked for, and when, on the test's clock; the list it ended with."""

    made: tuple[tuple[float, str], ...]
    roster: Roster
    at_end: Monotonic

    def at(self, path: str) -> list[float]:
        return [round(at, 3) for at, called in self.made if called == path]


async def measured(
    tmp_path: Path,
    *,
    list_s: float | None,
    seconds: int,
    release_at: float | None = None,
    armed_at: float | None = None,
    one_task: bool = False,
) -> Measured:
    """``seconds`` of the link, its tasks run as the console runs them, on the test's clock.

    The sending and the list once a second, the stop watch twenty times a
    second, each a task of its own. With ``one_task`` the list is asked from
    the sending task instead, just before its step, as it was asked from
    inside the step before. ``release_at``: when a held list is let through.
    ``armed_at``: when a session started at the machine is armed.
    """
    clock = ManualClock()
    waits = Waits(clock)
    dashboard = Paced(waits, list_s)
    dashboard.answer(STATUS, status_answer())
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    root = tmp_path / "records"
    root.mkdir()
    disk = Disk(root)
    panel = rig(tmp_path).panel
    runtime = StubRuntime(source=panel.runtime.snapshot)
    roster = Roster(linked=True)
    sync = CloudSync(
        clock=clock,
        transport=dashboard,
        runtime=runtime,
        surface=panel.surface,
        store=panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        records=RecordSource(
            root=root, current=disk.in_progress, io=SlowIo(waits, 0.0), boot_id=BOOT
        ),
        roster=roster,
    )

    async def sending() -> None:
        if one_task:
            await sync.refresh_roster()
        await sync.step()

    async def every(period: float, step: Callable[[], Awaitable[None]]) -> None:
        """As ``LocalPanel._every`` does, on the test's clock."""
        while True:
            began = float(clock.monotonic())
            await step()
            await waits.sleep(max(0.0, period - (float(clock.monotonic()) - began)))

    tasks = [
        asyncio.create_task(every(float(CLOUD_PERIOD), sending)),
        asyncio.create_task(every(float(STOP_WATCH_PERIOD), sync.watch_stop)),
    ]
    if not one_task:
        tasks.append(asyncio.create_task(every(float(CLOUD_PERIOD), sync.refresh_roster)))
    live: Recorded | None = None
    try:
        for _ in range(12):  # every task makes its first pass at 0
            await asyncio.sleep(0)
        for tick in range(seconds * 20):
            if armed_at is not None and live is None and tick >= round(armed_at * 20):
                # The runtime arms a session, and the journal opens its record.
                runtime.state = RuntimeState.RUNNING
                sync.session_started(manual(clock))
                live = recording(root, clock, "ref-now")
                disk.current = live.path
            if release_at is not None and tick == round(release_at * 20):
                dashboard.held.set()
                for _ in range(12):  # answered at this instant, before the clock moves
                    await asyncio.sleep(0)
            if live is not None and tick % 20 == 0:
                live.tick(1.0)
            # To the exact twentieth: a sum of 0.05 drifts below the second, and the
            # link compares its periods exactly.
            await waits.advance((tick + 1) / 20 - float(clock.monotonic()))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return Measured(tuple(dashboard.made), roster, clock.monotonic())


def every_period(period: float, until: float) -> list[float]:
    """0, ``period``, ``2 * period``... up to ``until``: a cadence kept to the second."""
    return [round(n * period, 3) for n in range(int(until // period) + 1)]


def gaps(instants: list[float]) -> list[float]:
    return [round(later - earlier, 3) for earlier, later in pairwise(instants)]


# =========================================================================
# On the test's clock
# =========================================================================


@pytest.mark.parametrize(
    ("list_s", "release_at", "asked"),
    [
        pytest.param(LIST_S, None, [0.0, 60.0, 120.0], id="a list that takes 45 s"),
        pytest.param(None, 100.0, [0.0, 100.0], id="a list held until 100 s"),
    ],
)
async def test_a_slow_list_holds_neither_the_heartbeat_nor_the_poll(
    tmp_path: Path, list_s: float | None, release_at: float | None, asked: list[float]
) -> None:
    """At rest, 130 s: the heartbeat every 10 s and the poll every 3 s, to the second."""
    seen = await measured(tmp_path, list_s=list_s, release_at=release_at, seconds=130)

    assert seen.at(HEARTBEAT) == every_period(float(HEARTBEAT_PERIOD), seen.at_end)
    assert seen.at(POLL_PATH) == every_period(float(POLL_PERIOD), seen.at_end)
    # One request for the list at a time, and the next one a period after the last.
    assert seen.at(ROSTER_PATH) == asked
    assert all(gap >= float(ROSTER_PERIOD) for gap in gaps(asked))
    view = seen.roster.view(seen.at_end)
    assert (view.state, view.riders) == (RosterState.CURRENT, (RIDER,))


@pytest.mark.parametrize(
    ("list_s", "release_at"),
    [
        pytest.param(LIST_S, None, id="a list that takes 45 s"),
        pytest.param(None, 60.0, id="a list held until 60 s"),
    ],
)
async def test_while_a_list_is_awaited_a_session_is_declared_and_its_stop_asked_for_in_time(
    tmp_path: Path, list_s: float | None, release_at: float | None
) -> None:
    """The list asked for at 0, a session armed at 30 s while it is still awaited."""
    seen = await measured(tmp_path, list_s=list_s, release_at=release_at, armed_at=30.0, seconds=90)

    assert seen.at(ROSTER_PATH) == [0.0], "never asked during a session"
    assert seen.at(HEARTBEAT) == every_period(float(HEARTBEAT_PERIOD), seen.at_end)
    # Polled at rest only, every 3 s: the last at the very instant of the arming, which
    # the sending's step at 30 s sees before the runtime is armed.
    assert seen.at(POLL_PATH) == every_period(float(POLL_PERIOD), 30.0)
    # The stop watch: the session declared at its next look, the question at its cadence.
    (declared,) = seen.at(LOCAL)
    assert 30.0 <= declared <= 30.0 + LOOK
    asked = seen.at(STATUS)
    assert declared <= asked[0] <= declared + LOOK
    assert len(asked) == 20
    assert all(
        float(STATUS_PERIOD) <= gap <= float(STATUS_PERIOD) + float(STOP_WATCH_PERIOD)
        for gap in gaps(asked)
    ), gaps(asked)
    # The list that came during the session is held, offered once the session is over.
    assert seen.roster.view(seen.at_end).riders == (RIDER,)


async def test_asked_from_the_sending_step_a_slow_list_would_hold_the_heartbeat_and_the_poll(
    tmp_path: Path,
) -> None:
    """What the task of its own is for: the same world, the list asked where it was."""
    seen = await measured(tmp_path, list_s=LIST_S, seconds=130, one_task=True)

    beats = seen.at(HEARTBEAT)
    polls = seen.at(POLL_PATH)
    assert beats[0] == LIST_S, "the first heartbeat waited for the list"
    assert max(gaps(beats)) > 2 * float(HEARTBEAT_PERIOD), gaps(beats)
    assert polls[0] == LIST_S
    assert max(gaps(polls)) > 10 * float(POLL_PERIOD), gaps(polls)


# =========================================================================
# On the real console
# =========================================================================


class HungList(Dashboard):
    """A dashboard whose list never comes until :attr:`release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()
        self.cancelled: asyncio.Event = asyncio.Event()

    @override
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        if path == ROSTER_PATH:
            self.asked.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await super().get(path, params)


async def test_on_the_console_a_list_that_never_comes_holds_neither_the_sending_nor_the_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = HungList()
    rig_, _ = make_rig(tmp_path, env=LINKED_ENV, transport=lambda _config: dashboard)
    sending, watching = LocalPanel.cloud_step, LocalPanel.cloud_stop_step
    steps: list[float] = []
    looks: list[float] = []

    async def counted_step(panel: LocalPanel) -> None:
        steps.append(time.perf_counter())
        await sending(panel)

    async def counted_look(panel: LocalPanel) -> None:
        looks.append(time.perf_counter())
        await watching(panel)

    monkeypatch.setattr(LocalPanel, "cloud_step", counted_step)
    monkeypatch.setattr(LocalPanel, "cloud_stop_step", counted_look)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(dashboard.asked.wait(), 2)
        asked_at = time.perf_counter()
        await asyncio.sleep(1.5 * float(CLOUD_PERIOD))
        # The list still awaited, the sending went on, and so did the stop watch.
        assert not dashboard.cancelled.is_set()
        assert [at for at in steps if at > asked_at], "a sending step while the list hung"
        assert dashboard.to(HEARTBEAT)
        assert len([at for at in looks if at > asked_at]) >= 10
        stop.set()
        assert await asyncio.wait_for(runner, 2) == EXIT_OK
        assert dashboard.cancelled.is_set(), "the request was cancelled at exit"
    finally:
        stop.set()
        web.request_exit()
        dashboard.release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()


async def test_on_the_console_a_sending_step_that_never_returns_does_not_keep_the_list_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dashboard = Dashboard()
    rig_, _ = make_rig(tmp_path, env=LINKED_ENV, transport=lambda _config: dashboard)
    entered, release = asyncio.Event(), asyncio.Event()

    async def stuck(_panel: LocalPanel) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(LocalPanel, "cloud_step", stuck)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0.2)
        assert len(dashboard.to(ROSTER_PATH)) == 1
        held = rig_.panel.roster.view(rig_.clock.monotonic())
        assert (held.state, held.riders) == (RosterState.CURRENT, (RIDER,))
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()


async def test_a_list_still_awaited_at_exit_holds_neither_the_exit_nor_the_stop_of_the_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The motor turning, the list's task waiting on a request that never ends: the
    console stops the drive and exits without waiting for it."""
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

    monkeypatch.setattr(CloudSync, "refresh_roster", never_answered)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stop.set()
        assert await asyncio.wait_for(runner, 2) == EXIT_OK
        assert cleaned.is_set()
        assert rig_.simulator.commanded_setpoint == 0
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()
