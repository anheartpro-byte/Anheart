"""The sending of the records, wired into the real console (ANH-129).

Three things only the whole console can show:

* a session that runs, and ends, while the dashboard refuses this console's
  contract (426) loses nothing: once the dashboard serves the contract again,
  the session is declared, sent whole and closed with its reason;
* a records directory that does not answer holds the sending and nothing
  else: the control tick goes on at its pace while the dashboard step waits,
  the next steps do not pile up behind it, and a step that is cancelled, as
  every dashboard step is when the console exits, returns at once;
* once the dashboard holds the end of a session, its stop is no longer asked
  for, even while the machine is still winding the session down;
* the stop asked for from the dashboard is looked for by a task of its own:
  the question keeps its cadence while record reads and uploads are slow, and
  while a long backlog is caught up;
* a launch from the dashboard whose start it does not take, whatever it
  answers, leaves RUNNING at once;
* the link keeps no queue (EX-7).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final, cast, override

import pytest

from src import cloud_sync, record_uplink
from src.clock import ManualClock
from src.cloud_sync import STATUS_PERIOD, CloudSync, Refused, Unreachable
from src.contract import ErrorCode
from src.local_panel import CLOUD_PERIOD, STOP_WATCH_PERIOD, LocalPanel
from src.record.cursor import Cursor, load
from src.record.export import RecordIo
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
from src.record.schema import RecordError
from src.record.upload import Head, read_head
from src.record_uplink import RETRY_PERIOD, TELEMETRY_PERIOD, RecordSource
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import OutputRpm, Seconds
from tests.record_uplink_support import BOOT, Disk, Recorded, armed, bench, recording, tie
from tests.test_cloud_contract import refuse_everything_but_the_status
from tests.test_cloud_sync import (
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    TICK,
    Dashboard,
    Linked,
    Reply,
    StubRuntime,
    config_of,
    launch_answer,
    manual,
    ok,
    rig,
    status_answer,
)
from tests.test_failure_rig import make_rig
from tests.test_local_panel import FakeWeb
from tests.test_record_wiring import recording_linked

LOCAL = "/api/machine/training/local"
TELEMETRY = "/api/machine/training/telemetry"
EVENTS = "/api/machine/training/events"
END = "/api/machine/training/end"
STATUS = "/api/machine/training/status"


async def seconds(rig: Linked, journal: Journal, count: int) -> None:
    """``count`` seconds of the console, the disk catching up after each."""
    for _ in range(count):
        await rig.run(1.0)
        journal.drain()


def state_of(rig: Linked) -> RuntimeState:
    """Read afresh: a checker keeps a narrowing across an ``await``."""
    return rig.panel.runtime.state


def sent_seconds(dashboard: Dashboard) -> list[int]:
    return [
        int(cast("float", point["elapsedS"]))
        for body in dashboard.to(TELEMETRY)
        for point in cast("list[dict[str, object]]", body["points"])
    ]


async def test_a_session_under_a_refused_contract_is_sent_whole_and_closed_once_it_is_served(
    tmp_path: Path,
) -> None:
    """Nothing is dropped under a 426: the record holds it, the cursor does not move."""
    rig, dashboard, journal = recording_linked(tmp_path)
    refuse_everything_but_the_status(dashboard)
    dashboard.answer("/api/machine/training/status", status_answer())
    surface = rig.panel.surface
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await seconds(rig, journal, 3)
    assert isinstance(
        surface.submit_manual_target(output_rpm=OutputRpm(6.0), operator=OPERATOR), Ok
    )
    await seconds(rig, journal, int(RETRY_PERIOD) + 2)
    assert state_of(rig) is RuntimeState.RUNNING, "the session goes on under the local supervisor"

    # Refused, the declaration is made again once per retry period; nothing else is sent.
    assert 2 <= len(dashboard.to(LOCAL)) <= 4
    assert dashboard.to(TELEMETRY) == []
    assert dashboard.to(END) == []
    (record,) = records(journal.root)
    waiting = load(record)
    assert isinstance(waiting, Cursor)
    assert (waiting.session_id, waiting.ticks_offset, waiting.state) == (None, 0, "pending")

    # The session ends at the console, still under the refusal.
    assert isinstance(surface.submit_end(operator=OPERATOR, reason="fini"), Ok)
    for _ in range(40):
        await seconds(rig, journal, 1)
        if state_of(rig) is not RuntimeState.ENDING:
            break
    await seconds(rig, journal, 2)
    assert state_of(rig) in (RuntimeState.FINISHED, RuntimeState.IDLE)
    assert dashboard.to(END) == []
    cloud = rig.panel.cloud
    assert cloud is not None
    assert cloud.owed == 1

    # The dashboard serves this console's contract again.
    dashboard.handlers.clear()
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    await seconds(rig, journal, int(RETRY_PERIOD) + 6)

    recording_ = read(record)
    assert isinstance(recording_, Ok)
    expected = sorted({round(row.t * 1000) // 1000 for row in recording_.value.rows})
    assert sent_seconds(dashboard) == expected
    ranks = [
        event["seq"]
        for body in dashboard.to(EVENTS)
        for event in cast("list[dict[str, object]]", body["events"])
    ]
    assert ranks == list(range(len(recording_.value.events)))
    ends = dashboard.to(END)
    assert len(ends) == 1
    assert (ends[0]["sessionId"], ends[0]["failed"], ends[0]["reason"]) == (
        "cloud-1",
        False,
        "operator_stop: fini",
    )
    assert cloud.owed == 0
    closed = load(record)
    assert isinstance(closed, Cursor)
    assert closed.state == "complete"
    await rig.panel.close()


@pytest.fixture
def stuck(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """A records directory that does not answer: reading a manifest never returns.

    Until the test sets the event it gets. It is set at the end in any case:
    no thread is left blocked behind a test.
    """
    release = threading.Event()

    def never(record: Path) -> Result[Head, RecordError]:
        release.wait(30.0)
        return read_head(record)

    monkeypatch.setattr("src.record_uplink.read_head", never)
    yield release
    release.set()


async def test_a_disk_that_does_not_answer_holds_the_sending_and_no_step_piles_up(
    tmp_path: Path, stuck: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record_uplink, "READ_TIMEOUT", Seconds(0.2))
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)

    began = time.perf_counter()
    for _ in range(40):
        await b.step(0.1)
    waited = time.perf_counter() - began

    # The first step waits its bound; every other finds the one thread taken
    # and returns at once. Forty steps on a dead disk cost one bound (a fifth
    # of a second), not forty (eight seconds).
    assert waited < 4.0
    # The session is declared all the same, under the reference of its record:
    # that needs no file. Its measurements wait for the disk.
    assert b.link.paths() == ["local"]
    assert b.link.sent[0][1]["localRef"] == "ref-1"
    assert threading.active_count() < 12, "one thread waits on the disk, not one per step"

    stuck.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        await b.step(0.01)
        if len(b.link.sent) > 1:
            break
    # Tied to its record once the disk answers.
    assert b.link.paths()[:2] == ["local", "telemetry"]


async def test_a_step_waiting_on_the_disk_returns_at_once_when_it_is_cancelled(
    tmp_path: Path, stuck: threading.Event
) -> None:
    """Every dashboard step is cancelled first when the console exits: the exit never waits."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    b.disk.current = recording(tmp_path, b.clock).path
    assert not stuck.is_set()

    step = asyncio.create_task(b.step())
    await asyncio.sleep(0.05)
    assert not step.done(), "it waits for the disk, up to its two seconds"
    began = time.perf_counter()
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.gather(step)

    # Its bound is two seconds: it did not wait for it.
    assert time.perf_counter() - began < 1.0


async def test_the_control_tick_keeps_its_pace_while_the_dashboard_step_waits_on_the_disk(
    tmp_path: Path, stuck: threading.Event
) -> None:
    rig, dashboard, journal = recording_linked(tmp_path)
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    surface = rig.panel.surface
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    rig.clock.advance(TICK)
    await rig.panel.control_step()
    journal.drain()

    waiting = asyncio.create_task(rig.panel.cloud_step())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    longest = 0.0
    for _ in range(25):
        rig.clock.advance(TICK)
        began = time.perf_counter()
        await rig.panel.control_step()
        longest = max(longest, time.perf_counter() - began)
        journal.drain()
        await asyncio.sleep(0)

    assert not waiting.done(), "the dashboard step is still waiting for the disk"
    assert state_of(rig) is RuntimeState.RUNNING
    # The supervisor acts on a loop stalled for 600 ms, and the disk is waited for
    # two seconds: a tick here takes milliseconds, even on a loaded machine.
    assert longest < 0.5
    stuck.set()
    await asyncio.gather(waiting)
    await rig.panel.close()


async def test_no_stop_is_asked_once_the_dashboard_holds_the_end_of_a_session_winding_down(
    tmp_path: Path,
) -> None:
    """Behind a latched fault the record closes before the runtime is done with the session.

    The dashboard then holds the session ended. Asked, it would answer that it
    no longer holds it active, which the link reads as a request to stop: the
    question is no longer asked.
    """
    root = tmp_path / "records"
    root.mkdir()
    disk = Disk(root)
    source = RecordSource(root=root, current=disk.in_progress, io=RecordIo(1), boot_id=BOOT)
    r = rig(tmp_path, records=source)
    r.dashboard.answer(STATUS, status_answer())
    r.sync.session_started(manual(r.clock, remote="remote-1"))
    record = recording(root, r.clock, remote="remote-1")
    disk.current = record.path
    record.tick(2.0)
    await r.step()
    assert len(r.dashboard.to(STATUS)) == 1, "asked while the dashboard holds it running"

    record.close("safety_verdict", stop=None)
    await r.step(float(TELEMETRY_PERIOD))
    assert [body["reason"] for body in r.dashboard.to(END)] == ["safety_verdict"]
    asked = len(r.dashboard.to(STATUS))
    r.dashboard.answer(STATUS, status_answer(active=False))
    for _ in range(4):
        await r.step(5.0)

    assert len(r.dashboard.to(STATUS)) == asked
    assert r.sync.current_session_id == "remote-1", "the runtime has not finished"
    await r.panel.close()


# =========================================================================
# The stop asked for from the dashboard never waits behind the sending
# =========================================================================


class Waits:
    """Waits counted on the test's clock: one ends when the clock has been moved past it."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock: ManualClock = clock
        self.pending: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        over: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.pending.append((float(self.clock.monotonic()) + seconds, over))
        await asyncio.gather(over)

    async def advance(self, seconds: float) -> None:
        """Move the clock, end the waits that are over, and let every task run to its next one."""
        self.clock.advance(Seconds(seconds))
        now = float(self.clock.monotonic()) + 1e-9
        over = [wait for until, wait in self.pending if until <= now]
        self.pending = [(until, wait) for until, wait in self.pending if until > now]
        for wait in over:
            wait.set_result(None)
        for _ in range(12):
            await asyncio.sleep(0)


class SlowDashboard(Dashboard):
    """Every request takes ``delay`` seconds to be answered, or what ``slow`` says for its
    route. When each was made is kept."""

    def __init__(self, waits: Waits, delay: float) -> None:
        super().__init__()
        self.waits: Waits = waits
        self.delay: float = delay
        self.slow: dict[str, float] = {}
        self.made: list[tuple[float, str]] = []

    @override
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        self.made.append((float(self.waits.clock.monotonic()), path))
        await self.waits.sleep(self.slow.get(path, self.delay))
        return await super().get(path, params)

    @override
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Reply:
        self.made.append((float(self.waits.clock.monotonic()), path))
        await self.waits.sleep(self.slow.get(path, self.delay))
        return await super().post(path, body)


class SlowIo(RecordIo):
    """Every read of a record and every cursor write takes ``delay`` seconds."""

    def __init__(self, waits: Waits, delay: float) -> None:
        super().__init__(1)
        self.waits: Waits = waits
        self.delay: float = delay

    @override
    async def run[T](
        self,
        work: Callable[[], T],
        timeout: Seconds,
        late: Callable[[T], None] | None = None,
    ) -> Result[T, RecordError]:
        await self.waits.sleep(self.delay)
        return Ok(work())


@dataclass(frozen=True)
class Watched:
    """What a session's link did in a slow world, on the test's clock."""

    armed_at: float
    """When the runtime armed the session."""

    made: tuple[tuple[float, str], ...]
    """Every request, when it was made."""

    slowest_step: float
    """The longest sending step."""

    def at(self, path: str) -> list[float]:
        """When each request to ``path`` was made, counted from the arming of the session."""
        return [round(at - self.armed_at, 3) for at, called in self.made if called == path]


async def watched(
    tmp_path: Path,
    *,
    request_s: float,
    read_s: float,
    backlog_points: int,
    one_task: bool,
    session_s: int = 80,
    armed_after_s: float = 0.0,
    launched: bool = True,
    batches: tuple[float, Reply] | None = None,
) -> Watched:
    """``session_s`` seconds of a link, a session being armed ``armed_after_s`` into them.

    The link's work is run as the console runs it: the sending once a second
    and the stop watch twenty times a second, each a task of its own. With
    ``one_task`` the watch is instead called from the sending task, before
    and after the step, as it was before it had its own. ``launched``: the
    session comes from the dashboard; else it is started at the machine.
    ``batches``: what every batch of telemetry is answered and after how
    long, while the other routes answer as ``request_s`` says.
    """
    clock = ManualClock()
    waits = Waits(clock)
    dashboard = SlowDashboard(waits, request_s)
    dashboard.answer(STATUS, status_answer())
    if batches is not None:
        dashboard.slow[TELEMETRY], reply = batches
        dashboard.answer(TELEMETRY, reply)
    root = tmp_path / "records"
    root.mkdir()
    disk = Disk(root)
    if backlog_points:
        left = recording(root, clock, "ref-old")
        left.tick(float(backlog_points), hertz=1)
        left.close()
        tie(left.path, Cursor(session_id="cloud-old"))
        clock.advance(Seconds(3600.0))
    panel = rig(tmp_path).panel
    runtime = StubRuntime(source=panel.runtime.snapshot, state=RuntimeState.RUNNING)
    sync = CloudSync(
        clock=clock,
        transport=dashboard,
        runtime=runtime,
        surface=panel.surface,
        store=panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=True,
        records=RecordSource(
            root=root, current=disk.in_progress, io=SlowIo(waits, read_s), boot_id=BOOT
        ),
    )
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    remote = "remote-1" if launched else None
    live: Recorded | None = None
    armed_at = 0.0
    steps: list[float] = []

    async def sending() -> None:
        began = float(clock.monotonic())
        if one_task:
            await sync.watch_stop()
        await sync.step()
        if one_task:
            await sync.watch_stop()
        steps.append(float(clock.monotonic()) - began)

    async def every(period: float, step: Callable[[], Awaitable[None]]) -> None:
        """As ``LocalPanel._every`` does, on the test's clock."""
        while True:
            began = float(clock.monotonic())
            await step()
            await waits.sleep(max(0.0, period - (float(clock.monotonic()) - began)))

    tasks = [asyncio.create_task(every(float(CLOUD_PERIOD), sending))]
    if not one_task:
        tasks.append(asyncio.create_task(every(float(STOP_WATCH_PERIOD), sync.watch_stop)))
    try:
        for tick in range(session_s * 20):
            if live is None and tick >= round(armed_after_s * 20):
                # The runtime arms the session, and the journal opens its record.
                armed_at = float(clock.monotonic())
                sync.session_started(manual(clock, remote=remote))
                live = recording(root, clock, "ref-now", remote=remote)
                disk.current = live.path
            if live is not None and tick % 20 == 0:
                live.tick(1.0)
            await waits.advance(0.05)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return Watched(armed_at, tuple(dashboard.made), max(steps))


@pytest.mark.parametrize(
    ("request_s", "read_s", "backlog_points"),
    [
        pytest.param(0.01, 0.0, 0, id="healthy"),
        pytest.param(0.01, 1.8, 0, id="reads at 1.8 s"),
        pytest.param(2.5, 0.0, 6000, id="requests at 2.5 s, a backlog"),
        pytest.param(2.5, 1.8, 6000, id="reads at 1.8 s, requests at 2.5 s, a backlog"),
    ],
)
async def test_the_stop_question_keeps_its_cadence_while_reads_and_uploads_are_slow(
    tmp_path: Path, request_s: float, read_s: float, backlog_points: int
) -> None:
    """Asked every three seconds whatever the sending waits for: a task of its own."""
    seen = await watched(
        tmp_path,
        request_s=request_s,
        read_s=read_s,
        backlog_points=backlog_points,
        one_task=False,
        session_s=45,
    )

    asked = seen.at(STATUS)
    gaps = [later - earlier for earlier, later in pairwise(asked)]
    assert len(asked) >= 12
    # Never later than the watch's own look, a quarter of a second, after it is due.
    assert max(gaps) <= float(STATUS_PERIOD) + float(STOP_WATCH_PERIOD) + 0.06, sorted(gaps)[-3:]


async def test_asked_from_the_sending_task_the_stop_question_would_wait_behind_it(
    tmp_path: Path,
) -> None:
    """What the task of its own is for: the same slow world, measured the other way.

    Called from the task that sends, before and after its step, the question
    waits for the reads and the uploads of that step.
    """
    seen = await watched(
        tmp_path, request_s=2.5, read_s=1.8, backlog_points=6000, one_task=True, session_s=45
    )

    gaps = [later - earlier for earlier, later in pairwise(seen.at(STATUS))]
    assert seen.slowest_step > 2 * float(STATUS_PERIOD), "the sending step really is slow here"
    assert max(gaps) > 2 * float(STATUS_PERIOD)


LOOK: float = float(STOP_WATCH_PERIOD) + 0.011
"""How long after it may, at the latest, the watch does a thing: its next look."""


@pytest.mark.parametrize(
    ("request_s", "read_s", "backlog_points"),
    [
        pytest.param(0.01, 0.0, 0, id="healthy"),
        pytest.param(0.01, 1.8, 6000, id="a backlog, reads at 1.8 s"),
        pytest.param(2.5, 1.8, 6000, id="a backlog, reads at 1.8 s, requests at 2.5 s"),
    ],
)
async def test_a_launch_is_confirmed_and_its_stop_asked_for_before_any_reading_or_catching_up(
    tmp_path: Path, request_s: float, read_s: float, backlog_points: int
) -> None:
    """Armed in the middle of a catch-up on a slow disk, as early as on a healthy link.

    The confirmation leaves at the watch's next look after the arming, and the
    first stop question at its next look after the confirmation is answered:
    neither waits for the sending step, a record read or a batch of an older
    session.
    """
    seen = await watched(
        tmp_path,
        request_s=request_s,
        read_s=read_s,
        backlog_points=backlog_points,
        one_task=False,
        session_s=22,
        armed_after_s=10.3,
    )

    confirmed, asked = seen.at(START)[0], seen.at(STATUS)[0]
    assert confirmed <= LOOK
    assert asked <= confirmed + request_s + LOOK
    if backlog_points:
        assert seen.slowest_step > 3.0, "the sending really was busy catching up"


@pytest.mark.parametrize(
    ("request_s", "read_s", "backlog_points"),
    [
        pytest.param(0.01, 0.0, 0, id="healthy"),
        pytest.param(2.5, 1.8, 6000, id="a backlog, reads at 1.8 s, requests at 2.5 s"),
    ],
)
async def test_a_session_started_at_the_machine_is_declared_before_any_reading_or_catching_up(
    tmp_path: Path, request_s: float, read_s: float, backlog_points: int
) -> None:
    """Declared at the watch's next look after its record's directory exists, whatever
    the sending is waiting for; its stop is asked for as soon as the dashboard names it."""
    seen = await watched(
        tmp_path,
        request_s=request_s,
        read_s=read_s,
        backlog_points=backlog_points,
        one_task=False,
        session_s=22,
        armed_after_s=10.3,
        launched=False,
    )

    declared, asked = seen.at(LOCAL)[0], seen.at(STATUS)[0]
    assert declared <= LOOK
    assert asked <= declared + request_s + LOOK


HELD_BATCHES: Final[Mapping[str, tuple[float, Reply]]] = {
    "answered 503": (0.01, Err(Refused(503, "HTTP 503"))),
    "left unanswered": (3.0, Err(Unreachable("ReadTimeout"))),
}
"""A dashboard that takes no batch of telemetry and answers every other route at once:
what each batch gets, and after how long (the transport gives up after three seconds)."""

ARMINGS: Final[tuple[float, ...]] = (0.3, 5.3, 10.3, 14.3)
"""When a session is armed, in seconds after the first batch was tried: across the fifteen
seconds the sending then waits."""


@pytest.mark.parametrize("held", HELD_BATCHES)
@pytest.mark.parametrize("armed_after_s", ARMINGS)
async def test_a_launch_is_confirmed_and_its_stop_asked_for_while_the_catching_up_is_held(
    tmp_path: Path, held: str, armed_after_s: float
) -> None:
    """A hundred minutes to catch up and no batch taken: the sending waits fifteen
    seconds after each. A launch armed anywhere in that wait is confirmed, and its stop
    asked for, as early as on a healthy link: its first words wait on their own failures
    only, never on what another request was answered.
    """
    seen = await watched(
        tmp_path,
        request_s=0.01,
        read_s=0.0,
        backlog_points=6000,
        one_task=False,
        session_s=round(armed_after_s) + 18,
        armed_after_s=armed_after_s,
        batches=HELD_BATCHES[held],
    )

    confirmed, asked = seen.at(START)[0], seen.at(STATUS)[0]
    assert confirmed <= LOOK
    assert asked <= confirmed + 0.01 + LOOK
    assert len(seen.at(START)) == 1, "said once"
    # The catching up really was held all along: a batch every fifteen seconds, no more.
    tried = [at + seen.armed_at for at in seen.at(TELEMETRY)]
    assert len(tried) >= 2
    assert min(later - earlier for earlier, later in pairwise(tried)) >= float(RETRY_PERIOD)


@pytest.mark.parametrize("held", HELD_BATCHES)
@pytest.mark.parametrize("armed_after_s", ARMINGS)
async def test_a_session_started_at_the_machine_is_declared_while_the_catching_up_is_held(
    tmp_path: Path, held: str, armed_after_s: float
) -> None:
    """The same wait of the sending, and a session started at the machine: declared at the
    watch's next look after its record's directory exists, its stop asked for at once."""
    seen = await watched(
        tmp_path,
        request_s=0.01,
        read_s=0.0,
        backlog_points=6000,
        one_task=False,
        session_s=round(armed_after_s) + 18,
        armed_after_s=armed_after_s,
        launched=False,
        batches=HELD_BATCHES[held],
    )

    declared, asked = seen.at(LOCAL)[0], seen.at(STATUS)[0]
    assert declared <= LOOK
    assert asked <= declared + 0.01 + LOOK
    assert len(seen.at(LOCAL)) == 1, "said once"


async def test_told_from_the_sending_task_a_launch_would_wait_behind_the_catching_up(
    tmp_path: Path,
) -> None:
    """What the task of its own is for, measured the other way in the same slow world."""
    seen = await watched(
        tmp_path,
        request_s=2.5,
        read_s=1.8,
        backlog_points=6000,
        one_task=True,
        session_s=30,
        armed_after_s=10.3,
    )

    assert seen.at(START)[0] > 2.0
    assert seen.at(STATUS)[0] > 5.0


async def test_the_console_runs_the_stop_watch_as_a_task_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sending step never returns here: the watch goes on looking all the same."""
    rig_, _ = make_rig(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    looks: list[float] = []

    async def stuck(_panel: LocalPanel) -> None:
        entered.set()
        await release.wait()

    async def look(_panel: LocalPanel) -> None:
        looks.append(time.perf_counter())

    monkeypatch.setattr(LocalPanel, "cloud_step", stuck)
    monkeypatch.setattr(LocalPanel, "cloud_stop_step", look)
    stop = asyncio.Event()
    web = FakeWeb()
    runner = asyncio.create_task(rig_.panel.run(stop, web))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(6 * float(STOP_WATCH_PERIOD))
        assert len(looks) >= 3
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, return_exceptions=True)
        await rig_.panel.close()


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            Refused(426, "Unsupported machine contract", ErrorCode("contract_unsupported")),
            id="426",
        ),
        pytest.param(Refused(401, "Invalid API key", ErrorCode("unauthorized")), id="401"),
        pytest.param(Refused(503, "HTTP 503"), id="503"),
    ],
)
async def test_on_the_console_a_launch_whose_start_is_not_taken_leaves_running_at_once(
    tmp_path: Path, refusal: Refused
) -> None:
    """Whatever the dashboard answers to the confirmation, the machine does not run on."""
    rig_, dashboard, journal = recording_linked(tmp_path)
    dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    dashboard.answer(START, Err(refusal))
    dashboard.answer(STATUS, status_answer(stop=True))

    await seconds(rig_, journal, 3)

    assert state_of(rig_) is not RuntimeState.RUNNING
    reason = rig_.panel.runtime.stop_reason
    assert reason is not None
    assert reason.startswith("annulee au tableau de bord (")
    await rig_.panel.close()


async def test_the_end_of_a_session_stopped_from_the_dashboard_says_so_in_the_dashboard_s_words(
    tmp_path: Path,
) -> None:
    """The record keeps those words whole: the dashboard's own label is nobody's name."""
    rig_, dashboard, journal = recording_linked(tmp_path)
    dashboard.answer(LOCAL, ok({"sessionId": "cloud-1"}))
    surface = rig_.panel.surface
    assert isinstance(surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok)
    await seconds(rig_, journal, 3)
    assert state_of(rig_) is RuntimeState.RUNNING

    dashboard.answer(STATUS, status_answer(stop=True))
    for _ in range(40):
        await seconds(rig_, journal, 1)
        if dashboard.to(END):
            break

    assert rig_.panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    (end,) = dashboard.to(END)
    assert (end["sessionId"], end["failed"]) == ("cloud-1", False)
    assert end["reason"] == "operator_stop: arret demande depuis le tableau de bord"
    await rig_.panel.close()


def test_ex7_the_link_keeps_no_queue() -> None:
    """Neither a bounded queue of points nor a list of ended sessions: the records hold them."""
    for gone in ("MAX_QUEUED_POINTS", "MAX_FINISHED", "MAX_BATCH", "telemetry_point", "_Tracked"):
        assert not hasattr(cloud_sync, gone), gone
    for module in (cloud_sync, record_uplink):
        source = inspect.getsource(module)
        assert "deque" not in source, module.__name__
        assert "import queue" not in source, module.__name__
    # What a session being sent holds in memory is its cursor and a few flags:
    # no field of it is a container.
    owed = inspect.getsource(cast("type[object]", vars(record_uplink)["_Owed"]))
    assert re.search(r":\s*(list|dict|set|tuple|Sequence|Mapping)\[", owed) is None
