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
* the link keeps no queue (EX-7).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from src import cloud_sync, record_uplink
from src.record.cursor import Cursor, load
from src.record.export import RecordIo
from src.record.journal import Journal
from src.record.reader import read
from src.record.retention import records
from src.record.schema import RecordError
from src.record.upload import Head, read_head
from src.record_uplink import RETRY_PERIOD, TELEMETRY_PERIOD, RecordSource
from src.result import Ok, Result
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import OutputRpm, Seconds
from tests.record_uplink_support import BOOT, Disk, armed, bench, recording
from tests.test_cloud_contract import refuse_everything_but_the_status
from tests.test_cloud_sync import (
    OPERATOR,
    TICK,
    Dashboard,
    Linked,
    manual,
    ok,
    rig,
    status_answer,
)
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
    assert b.link.sent == []
    assert threading.active_count() < 12, "one thread waits on the disk, not one per step"

    stuck.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        await b.step(0.01)
        if b.link.sent:
            break
    # Bound to its record once the disk answers, and declared under its reference.
    assert b.link.paths()[:2] == ["local", "telemetry"]
    assert b.link.sent[0][1]["localRef"] == "ref-1"


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
