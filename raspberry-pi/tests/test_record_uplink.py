"""Sending sessions to the dashboard from their records on disk (ANH-129).

Requirement by requirement:

* EX-1: the telemetry sent is the record's, at 1 Hz, and its events go to the
  events route with their rank;
* EX-2: the cursor moves when the dashboard acknowledges, and is on disk then;
* EX-4: at startup, what an earlier run left owed is taken up again, oldest
  first, after the session that is running;
* EX-5: the running session goes first, and catching up is paced at one batch
  of at most 300 points every 2 s;
* EX-7: nothing waits in memory: what a lost link, a refused contract or a
  restart leaves unsent is read from disk when the dashboard answers again;
* EX-8: a session started at the machine is declared before anything is sent,
  under the reference its record carries, whenever the dashboard answers.

Plus what each answer of the link does to the cursor, and what a disk that
does not answer costs (the sending, nothing else).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Callable, Mapping
from itertools import pairwise
from pathlib import Path
from typing import cast, override

import pytest

from src.clock import ManualClock
from src.record.cursor import BASELINE_NAME, Cursor, NoCursor, cursor_of, load, store
from src.record.export import BUSY, TIMEOUT, RecordIo
from src.record.logbook import ACTIVE as LOGBOOK_ACTIVE
from src.record.logbook import DIRECTORY as LOGBOOK
from src.record.logbook import PREVIOUS as LOGBOOK_PREVIOUS
from src.record.rows import JsonValue
from src.record.schema import RecordError
from src.record.upload import MAX_POINTS, READ_CHUNK, TAIL_CHUNK, Batch, read_batch, read_head
from src.record_uplink import (
    BIND_GRACE,
    CATCH_UP_PERIOD,
    CLOSE_GRACE,
    CURSOR_UNWRITTEN,
    DECLARED_WITHOUT_RECORD,
    END_PATH,
    EVENTS_PATH,
    EVENTS_RETRY_PERIOD,
    LIST_LOST,
    LOCAL_PATH,
    MAX_REFUSALS,
    RECORD_UNREADABLE,
    RETRY_PERIOD,
    START_PATH,
    START_UNKNOWN,
    TELEMETRY_PATH,
    TELEMETRY_PERIOD,
    UNOBSERVED_END,
    Acked,
    Answer,
    Held,
    RecordUplink,
    Refusal,
    RouteMissing,
    RuntimeEnd,
    declaration_from,
)
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_uplink_support import (
    BOOT,
    EPOCH_MS,
    Bench,
    Link,
    armed,
    bench,
    cursor_of_record,
    launched_programme,
    recording,
    tie,
)

START = EPOCH_MS + 100_000
"""When a session armed at the first instant of a test started, on the machine's clock."""

DOWN = Held("no route to host")
ENDED = RuntimeEnd(failed=False, reason="operator_stop: fini")


def stored(count: int = 0, *, rejected: int = 0) -> Acked:
    return Acked({"stored": count, "duplicates": 0, "rejected": rejected})


# =========================================================================
# EX-8: declared first, under the reference of its record
# =========================================================================


async def test_ex8_a_session_started_at_the_machine_is_declared_before_anything_is_sent(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-live")
    b.disk.current = record.path
    record.tick(1.0)
    record.event(2)

    await b.step()

    assert b.link.paths() == ["local", "telemetry", "events"]
    assert b.link.to(LOCAL_PATH) == [
        {
            # The reference the record carries, not one made up in memory.
            "localRef": "ref-live",
            "kind": "manual",
            "startedAt": START,
            "operatorName": "dr. attending",
            "sessionAgeMs": 1000,
            "occupancy": "bench",
        }
    ]
    assert b.link.to(TELEMETRY_PATH)[0]["sessionId"] == "cloud-1"
    assert b.uplink.session_id == "cloud-1"
    assert b.uplink.following == "cloud-1"
    assert b.uplink.running


async def test_ex8_a_programme_started_at_the_machine_is_declared_with_its_programme(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    b.uplink.begin(armed(b.clock, kind="auto"))

    await b.step()

    declared = b.link.to(LOCAL_PATH)[0]
    assert declared["kind"] == "auto"
    assert (declared["profileId"], declared["profileName"]) == ("standard_30_min", "30 min")
    assert (declared["zoneLowBpm"], declared["zoneHighBpm"]) == (118, 138)
    assert (declared["totalDurationS"], declared["subjectHrMax"]) == (1800.0, 162)
    assert "occupancy" not in declared


async def test_ex8_unreachable_the_record_goes_on_and_the_session_is_declared_when_it_answers(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(LOCAL_PATH, DOWN)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-live")
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    assert b.link.paths() == ["local"]
    # Before any answer, the record has its cursor, which names it: a restart
    # takes it up where the dashboard stopped, with the age of its session.
    assert load(record.path) == Cursor(local_ref="ref-live", boot_id=BOOT)

    for _ in range(int(RETRY_PERIOD) - 1):
        record.tick(1.0)
        await b.step()
    assert b.link.paths() == ["local"], "nothing is asked while the link is held"
    assert b.uplink.session_id is None
    assert b.uplink.following is None

    del b.link.answers[LOCAL_PATH]
    record.tick(1.0)
    await b.step()

    declared = b.link.to(LOCAL_PATH)
    assert [body["localRef"] for body in declared] == ["ref-live", "ref-live"]
    assert declared[1]["sessionAgeMs"] == 16_000
    assert declared[1]["startedAt"] == START
    # Everything recorded meanwhile is sent, from the first second, in one batch.
    assert b.link.seconds() == [float(second) for second in range(16)]


async def test_ex8_a_session_waits_for_its_record_to_be_declared_under_its_reference(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    await b.run(int(BIND_GRACE) - 1)
    assert b.link.sent == []

    record = recording(tmp_path, b.clock, "ref-late")
    b.disk.current = record.path
    await b.step()

    assert b.link.to(LOCAL_PATH)[0]["localRef"] == "ref-late"


async def test_ex8_a_session_that_gets_no_record_in_time_is_declared_without_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The disk gave no record in time: the dashboard still learns of the session."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    await b.run(int(BIND_GRACE))

    declared = b.link.to(LOCAL_PATH)
    assert len(declared) == 1
    reference = declared[0]["localRef"]
    assert isinstance(reference, str)
    assert re.fullmatch(r"[0-9a-f]{32}", reference)
    assert declared[0]["startedAt"] == START
    assert declared[0]["sessionAgeMs"] == 10_000
    # Said, in the log and on the console: its measurements may never arrive.
    assert "declared without its record" in caplog.text
    assert b.said == [DECLARED_WITHOUT_RECORD]

    b.uplink.finished(ENDED)
    await b.step()
    assert b.link.to(END_PATH) == [
        {
            "sessionId": "cloud-1",
            "failed": False,
            "reason": "operator_stop: fini",
            "endedAt": START + 10_000,
        }
    ]
    assert b.uplink.owed == 0
    assert b.said == [DECLARED_WITHOUT_RECORD], "said once"


async def test_ex8_a_record_that_appears_after_the_session_was_declared_is_tied_then_and_sent(
    tmp_path: Path,
) -> None:
    """Late, the record is still this session's: what the dashboard was told stands."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    await b.run(int(BIND_GRACE))
    assert len(b.link.to(LOCAL_PATH)) == 1
    assert b.link.to(TELEMETRY_PATH) == []

    late = recording(tmp_path, b.clock, "ref-late")
    b.disk.current = late.path
    late.tick(3.0)
    await b.run(6)

    # Not declared a second time: the session the dashboard named goes on.
    assert len(b.link.to(LOCAL_PATH)) == 1
    assert b.link.seconds("cloud-1") == [0.0, 1.0, 2.0]
    cursor = cursor_of_record(late.path)
    assert (cursor.local_ref, cursor.session_id) == ("ref-late", "cloud-1")

    late.close()
    b.uplink.finished(ENDED)
    await b.run(2)
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-1"]
    assert cursor_of_record(late.path).state == "complete"
    assert b.uplink.owed == 0


async def test_a_console_that_records_nothing_declares_at_once_and_only_ends(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    b.uplink.begin(armed(b.clock))

    await b.step()
    await b.run(6)

    assert b.link.paths() == ["local"]
    b.uplink.finished(RuntimeEnd(failed=True, reason="safety_verdict"))
    await b.step()
    assert b.link.to(END_PATH) == [
        {
            "sessionId": "cloud-1",
            "failed": True,
            "reason": "safety_verdict",
            "endedAt": START + 7000,
        }
    ]


@pytest.mark.parametrize("document", [{}, {"sessionId": 7}, {"sessionId": None}])
async def test_a_declaration_answered_without_an_identifier_is_made_again_later(
    tmp_path: Path, document: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path, recording=False)
    b.link.answer(LOCAL_PATH, Acked(document))
    b.uplink.begin(armed(b.clock))

    await b.step()
    await b.step()

    assert len(b.link.to(LOCAL_PATH)) == 1
    assert "registration answered without a session id" in caplog.text
    del b.link.answers[LOCAL_PATH]
    await b.step(float(RETRY_PERIOD))
    assert b.uplink.session_id == "cloud-1"


async def test_a_declaration_answered_without_an_identifier_holds_back_nothing_else(
    tmp_path: Path,
) -> None:
    """The dashboard answered: the link works. Only that session waits to be declared again."""
    b = bench(tmp_path)
    b.link.answer(LOCAL_PATH, Acked({}))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    await b.step()
    record.close()
    b.uplink.finished(ENDED)
    await b.step()
    assert b.link.to(END_PATH) == []

    # A launch refused at the machine is owed its end, and gets it at once.
    b.uplink.owe_refusal("remote-9", "refusee par la machine : verdict a acquitter")
    await b.step()

    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["remote-9"]
    # And the session is declared, sent and closed once the dashboard names it.
    del b.link.answers[LOCAL_PATH]
    await b.step(float(RETRY_PERIOD))
    await b.run(3)
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["remote-9", "cloud-1"]
    assert cursor_of_record(record.path).state == "complete"
    assert b.uplink.owed == 0


async def test_a_declaration_refused_for_good_closes_the_record_and_sends_nothing_of_it(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(LOCAL_PATH, Refusal("machine_not_found (HTTP 400): Machine not found", True))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)

    await b.step()
    await b.run(8)

    assert b.link.paths() == ["local"]
    assert cursor_of_record(record.path).state == "complete"
    assert cursor_of_record(record.path).refused == 1
    assert b.uplink.owed == 0


# =========================================================================
# A launch from the dashboard
# =========================================================================


async def test_a_launch_is_confirmed_with_the_start_the_machine_dated_then_sent(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock, remote="k17remote"))
    assert b.uplink.session_id == "k17remote"
    assert b.following() is None, "not before the dashboard holds it started"
    record = recording(tmp_path, b.clock, "ref-remote", remote="k17remote")
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()

    assert b.link.paths() == ["start", "telemetry"]
    assert b.link.to(START_PATH) == [
        {"sessionId": "k17remote", "startedAt": START, "sessionAgeMs": 1000}
    ]
    assert b.following() == "k17remote"
    assert cursor_of_record(record.path).start_confirmed is True


async def test_a_start_the_dashboard_cannot_be_asked_about_holds_everything(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(START_PATH, DOWN)
    b.uplink.begin(armed(b.clock, remote="k17remote"))
    record = recording(tmp_path, b.clock, remote="k17remote")
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()
    await b.step()

    assert b.link.paths() == ["start"]
    assert cursor_of_record(record.path).start_confirmed is False
    assert b.cancelled == []


@pytest.mark.parametrize("final", [True, False])
async def test_a_start_the_dashboard_refuses_has_the_running_session_stopped_at_once(
    tmp_path: Path, final: bool
) -> None:
    """Cancelled on the dashboard between the poll and the arm: nobody wants this session."""
    b = bench(tmp_path, recording=False)
    b.link.answer(START_PATH, Refusal("session_not_pending (HTTP 400): cancelled", final))
    b.uplink.begin(armed(b.clock, remote="k17remote"))

    await b.step()
    await b.step(float(RETRY_PERIOD))

    assert b.cancelled == ["session_not_pending (HTTP 400): cancelled"]
    assert len(b.link.to(START_PATH)) == 1
    assert b.following() is None, "stopped: no stop is looked for after"


@pytest.mark.parametrize(
    ("answer", "words"),
    [
        (
            Held("contract_unsupported (HTTP 426): no", refused=True),
            "contract_unsupported (HTTP 426): no",
        ),
        (Held("sans code (HTTP 401): key", refused=True), "sans code (HTTP 401): key"),
        (Held("sans code (HTTP 503): later", refused=True), "sans code (HTTP 503): later"),
        (RouteMissing(), START_UNKNOWN),
    ],
)
async def test_a_start_the_dashboard_does_not_take_for_its_own_reasons_stops_the_session_once(
    tmp_path: Path, answer: Held | RouteMissing, words: str
) -> None:
    """Its contract, its key, an error of its own: the machine must not run on unconfirmed.

    Nobody on the dashboard could stop a session it does not hold started.
    The confirmation stays owed: it is made again, and the record sent, once
    the dashboard takes it.
    """
    b = bench(tmp_path)
    b.link.answer(START_PATH, answer)
    b.uplink.begin(armed(b.clock, remote="k17remote"))
    record = recording(tmp_path, b.clock, remote="k17remote")
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()
    assert b.cancelled == [words]
    assert b.following() is None
    for _ in range(3):
        await b.step(float(RETRY_PERIOD))

    assert b.cancelled == [words], "stopped once, however often the start is asked again"
    assert b.link.paths() == ["start"] * 4
    assert cursor_of_record(record.path).start_confirmed is False

    del b.link.answers[START_PATH]
    await b.step(float(RETRY_PERIOD))
    assert cursor_of_record(record.path).start_confirmed is True
    assert b.link.paths()[4:6] == ["start", "telemetry"]
    assert b.cancelled == [words]


async def test_a_record_taken_up_after_a_restart_whose_start_is_not_taken_stops_nothing(
    tmp_path: Path,
) -> None:
    """Nothing runs any more: there is nothing to stop, only a confirmation still owed."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, remote="k17remote")
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="k17remote", start_confirmed=False))
    old.link.answer(START_PATH, Held("sans code (HTTP 503): later", refused=True))

    b = old.restarted()
    await b.step()

    assert b.link.paths() == ["start"]
    assert b.cancelled == []


# =========================================================================
# The running session is told to the dashboard before anything is read
# =========================================================================


async def test_a_session_started_at_the_machine_is_declared_without_reading_any_file(
    tmp_path: Path,
) -> None:
    """Its reference is in the name of its record's directory: the disk is not asked."""
    io = Scripted()
    b = bench(tmp_path, io=io)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-early")
    b.disk.current = record.path

    await b.uplink.greet()

    assert b.link.paths() == ["local"]
    assert b.link.to(LOCAL_PATH)[0]["localRef"] == "ref-early"
    assert b.following() == "cloud-1", "its stop can be asked for from now on"
    assert io.calls == 0
    # Said once: greeted, there is nothing more to say.
    await b.uplink.greet()
    assert b.link.paths() == ["local"]


async def test_a_launch_from_the_dashboard_is_confirmed_without_reading_any_file(
    tmp_path: Path,
) -> None:
    io = Scripted()
    b = bench(tmp_path, io=io)
    b.uplink.begin(armed(b.clock, remote="k17remote"))

    await b.uplink.greet()

    assert b.link.paths() == ["start"]
    assert b.following() == "k17remote"
    assert io.calls == 0


@pytest.mark.parametrize("remote", [None, "k17remote"])
async def test_the_sending_step_neither_declares_nor_confirms_the_running_session(
    tmp_path: Path, remote: str | None
) -> None:
    """That is the greeting's, from the stop watch's task: the step only waits for it."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock, remote=remote))
    record = recording(tmp_path, b.clock, remote=remote)
    b.disk.current = record.path
    record.tick(3.0)

    for _ in range(4):
        b.clock.advance(Seconds(1.0))
        await b.uplink.step()

    assert b.link.sent == []
    await b.uplink.greet()
    await b.uplink.step()
    assert b.link.paths() == ["local" if remote is None else "start", "telemetry"]


async def test_nothing_is_greeted_when_nothing_runs_or_while_the_link_is_held(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    await b.uplink.greet()
    assert b.link.sent == []

    b.link.answer(LOCAL_PATH, DOWN)
    b.uplink.begin(armed(b.clock))
    for _ in range(int(RETRY_PERIOD)):
        b.clock.advance(Seconds(1.0))
        await b.uplink.greet()
    assert b.link.paths() == ["local"], "held: the same declaration is made again later"

    del b.link.answers[LOCAL_PATH]
    b.clock.advance(Seconds(1.0))
    await b.uplink.greet()
    assert b.link.paths() == ["local", "local"]
    assert b.uplink.session_id == "cloud-1"


async def test_a_declaration_the_dashboard_may_take_later_is_not_made_again_before_its_time(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    b.link.answer(LOCAL_PATH, Refusal("request_failed (HTTP 400): try again", final=False))
    b.uplink.begin(armed(b.clock))

    for _ in range(int(RETRY_PERIOD)):
        await b.uplink.greet()
        b.clock.advance(Seconds(1.0))
    assert len(b.link.to(LOCAL_PATH)) == 1

    await b.uplink.greet()
    assert len(b.link.to(LOCAL_PATH)) == 2


async def test_a_session_whose_declaration_was_refused_for_good_is_not_greeted_again(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    b.link.answer(LOCAL_PATH, Refusal("invalid_request (HTTP 400): no", final=True))
    b.uplink.begin(armed(b.clock))

    for _ in range(3):
        await b.step()

    assert len(b.link.to(LOCAL_PATH)) == 1


@pytest.mark.parametrize(
    "held",
    [Held("sans code (HTTP 503): later", refused=True), Held("no route to host")],
    ids=["a 503", "no answer"],
)
@pytest.mark.parametrize("remote", [None, "k17remote"], ids=["at the machine", "launched"])
async def test_the_first_words_of_a_session_do_not_wait_for_a_hold_the_sending_set(
    tmp_path: Path, held: Held, remote: str | None
) -> None:
    """A batch of an older session got no answer that can be used: the sending waits
    fifteen seconds. A session armed meanwhile is confirmed or declared at once."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(3.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    old.clock.advance(Seconds(3600.0))
    b = old.restarted()
    b.link.answer(TELEMETRY_PATH, held)
    await b.step()
    assert b.link.paths() == ["telemetry"], "the catching up is held from now on"

    b.clock.advance(Seconds(1.0))
    b.uplink.begin(armed(b.clock, remote=remote))
    record = recording(tmp_path, b.clock, "ref-now", remote=remote)
    b.disk.current = record.path
    await b.uplink.greet()

    assert b.link.paths() == ["telemetry", "local" if remote is None else "start"]
    assert b.following() == ("cloud-1" if remote is None else "k17remote")
    # The sending is still held: nothing else goes before its fifteen seconds.
    await b.step()
    assert len(b.link.sent) == 2


async def test_first_words_that_get_no_answer_wait_on_their_own_and_hold_the_sending_too(
    tmp_path: Path,
) -> None:
    """No answer at all: the watch's task asks again fifteen seconds later, not at its
    every look, and the sending does not ask for more meanwhile."""
    b = bench(tmp_path)
    b.link.answer(START_PATH, DOWN)
    b.uplink.begin(armed(b.clock, remote="k17remote"))

    await b.uplink.greet()
    for _ in range(40):
        b.clock.advance(Seconds(0.05))
        await b.uplink.greet()
    assert b.link.paths() == ["start"], "its own wait: not asked at every look"

    # The session ends unconfirmed: the sending, which takes it over, waits as long.
    b.uplink.finished(ENDED)
    await b.run(int(RETRY_PERIOD) - 4)
    assert b.link.paths() == ["start"]
    await b.run(3)
    assert b.link.paths() == ["start", "start"]


async def test_a_declaration_answered_without_a_name_is_made_again_after_its_own_wait(
    tmp_path: Path,
) -> None:
    """An answer, and no session in it: asked again fifteen seconds later, by the watch's
    task while the session runs, by the sending once it has ended."""
    b = bench(tmp_path)
    b.link.answer(LOCAL_PATH, Acked({}))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = record.path

    await b.uplink.greet()
    for _ in range(40):
        b.clock.advance(Seconds(0.05))
        await b.uplink.greet()
    assert b.link.paths() == ["local"], "its own wait: not asked at every look"

    record.close()
    b.uplink.finished(ENDED)
    await b.run(int(RETRY_PERIOD) - 4)
    assert b.link.paths() == ["local"]
    await b.run(3)
    assert b.link.paths() == ["local", "local"]


@pytest.mark.parametrize("remote", [None, "k17remote"], ids=["at the machine", "launched"])
async def test_a_running_session_whose_record_cannot_be_read_is_said_to_the_operator(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, remote: str | None
) -> None:
    """Its directory is there and its manifest never reads: told to the dashboard,
    followed for a stop, nothing of it sent, and no cursor on disk for a restart to take
    it up from. The operator reads it once, ten seconds into the session."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock, remote=remote))
    record = recording(tmp_path, b.clock, "ref-now", remote=remote)
    b.disk.current = record.path
    record.tick(2.0)
    (record.path / "manifest.json").rename(record.path / "manifest.away")
    session = "cloud-1" if remote is None else remote

    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(int(BIND_GRACE) - 1)
        assert b.said == []
        await b.run(3)

    assert b.said == [RECORD_UNREADABLE], "once"
    assert caplog.text.count("the record of the running session cannot be read") == 1
    assert b.link.paths() == ["local" if remote is None else "start"]
    assert b.following() == session
    assert load(record.path) == NoCursor()

    # Readable in the end: tied then, and its measurements follow.
    (record.path / "manifest.away").rename(record.path / "manifest.json")
    await b.run(3)
    assert b.link.seconds(session) == [0.0, 1.0]
    assert b.said == [RECORD_UNREADABLE]


async def test_a_launch_that_gets_no_record_at_all_is_said_to_the_operator(
    tmp_path: Path,
) -> None:
    """No directory was ever opened for it: the same words, at the same time."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock, remote="k17remote"))

    await b.run(int(BIND_GRACE) - 1)
    assert b.said == []
    await b.run(2)

    assert b.said == [RECORD_UNREADABLE]
    assert b.link.paths() == ["start"]


async def test_a_session_declared_without_its_record_is_said_in_those_words_only(
    tmp_path: Path,
) -> None:
    """Started at the machine with no directory to name it: its declaration says that it
    goes without its record, whichever of the two tasks looks first, and nothing is added."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    await b.run(int(BIND_GRACE) - 1)

    b.clock.advance(Seconds(1.0))
    await b.uplink.step()
    assert b.said == [], "left to the declaration"
    await b.uplink.greet()
    await b.run(3)

    assert b.said == [DECLARED_WITHOUT_RECORD]


@pytest.mark.parametrize("remote", [None, "k17remote"], ids=["at the machine", "launched"])
async def test_a_running_session_tied_to_its_record_says_nothing_of_it(
    tmp_path: Path, remote: str | None
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock, remote=remote))
    record = recording(tmp_path, b.clock, "ref-now", remote=remote)
    b.disk.current = record.path

    await b.run(int(BIND_GRACE) + 2)

    assert b.said == []
    assert isinstance(load(record.path), Cursor)


async def test_a_console_that_records_nothing_says_nothing_of_a_record(tmp_path: Path) -> None:
    b = bench(tmp_path, recording=False)
    b.uplink.begin(armed(b.clock, remote="k17remote"))

    await b.run(int(BIND_GRACE) + 2)

    assert b.said == []


class Late:
    """A link whose answer to the first request to one path waits until the test lets it go."""

    def __init__(self, inner: Link, path: str, answer: Answer) -> None:
        self.inner: Link = inner
        self.path: str = path
        self.answer: Answer = answer
        self.asked: asyncio.Event = asyncio.Event()
        self.release: asyncio.Event = asyncio.Event()

    async def send(self, path: str, body: Mapping[str, JsonValue]) -> Answer:
        if path == self.path and not self.asked.is_set():
            self.inner.sent.append((path, body))
            self.asked.set()
            await self.release.wait()
            return self.answer
        return await self.inner.send(path, body)


@pytest.mark.parametrize(
    "answer",
    [
        Refusal("session_not_pending (HTTP 400): cancelled", final=True),
        Held("sans code (HTTP 503): later", refused=True),
        RouteMissing(),
    ],
)
async def test_an_answer_to_a_confirmation_that_arrives_after_its_session_ended_stops_no_other(
    answer: Answer,
) -> None:
    """The first launch ends and a second is armed while the dashboard answers the first.

    The answer is about the session it was sent for: the second is neither
    stopped nor marked as stopped, and its own stop goes on being looked for.
    """
    clock = ManualClock(Monotonic(100.0), UnixMillis(EPOCH_MS))
    link = Link()
    late = Late(link, START_PATH, answer)
    cancelled: list[str] = []
    uplink = RecordUplink(
        clock=clock, sender=late, source=None, start_refused=cancelled.append, said=cancelled.append
    )
    uplink.begin(armed(clock, remote="first"))
    greeting = asyncio.create_task(uplink.greet())
    await late.asked.wait()

    uplink.finished(ENDED)
    uplink.begin(armed(clock, remote="second"))
    late.release.set()
    await asyncio.gather(greeting)

    assert cancelled == [], "nothing is stopped: the session that answer is about is over"
    clock.advance(RETRY_PERIOD)
    await uplink.greet()
    assert uplink.following == "second", "the second session's stop is looked for"


async def test_a_session_that_ends_while_it_is_being_declared_is_not_declared_twice() -> None:
    """The sending takes it over once the greeting has its answer, not while it waits for it."""
    clock = ManualClock(Monotonic(100.0), UnixMillis(EPOCH_MS))
    link = Link()
    late = Late(link, LOCAL_PATH, Acked({"sessionId": "cloud-1"}))
    said: list[str] = []
    uplink = RecordUplink(
        clock=clock, sender=late, source=None, start_refused=said.append, said=said.append
    )
    uplink.begin(armed(clock))
    greeting = asyncio.create_task(uplink.greet())
    await late.asked.wait()

    uplink.finished(ENDED)
    clock.advance(Seconds(1.0))
    await uplink.step()
    assert link.paths() == ["local"], "one declaration is on its way: no second one"

    late.release.set()
    await asyncio.gather(greeting)
    clock.advance(Seconds(1.0))
    await uplink.step()
    assert link.paths() == ["local", "end"]
    assert uplink.owed == 0


async def test_a_stop_forwarded_is_kept_with_its_session_and_gone_with_it(tmp_path: Path) -> None:
    """It neither hides a stop asked for in the next session nor stands for one."""
    b = bench(tmp_path, recording=False)
    b.uplink.stop_forwarded()  # nothing runs: nothing to mark
    b.uplink.begin(armed(b.clock, remote="first"))
    await b.step()
    assert b.following() == "first"

    b.uplink.stop_forwarded()
    assert b.following() is None, "asked once: no stop is looked for again"
    assert b.uplink.session_id == "first"

    b.uplink.finished(ENDED)
    b.uplink.begin(armed(b.clock, remote="second"))
    await b.step()
    assert b.following() == "second", "the next session's stop is looked for"


# =========================================================================
# EX-1, EX-2: what is sent is the record's; the cursor follows the acknowledgements
# =========================================================================


async def test_ex1_the_telemetry_is_the_record_s_at_1_hz_and_the_events_carry_their_rank(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    for second in range(12):
        record.tick(1.0)
        if second % 4 == 0:
            record.event()
        await b.step()

    # One point per second of the record, each once, in order.
    assert b.link.seconds() == [float(second) for second in range(11)]
    first = b.link.to(TELEMETRY_PATH)[0]
    assert first["points"] == (
        {
            "t": START,
            "elapsedS": 0.0,
            "phase": "hold",
            "motorRpm": 0,
            "outputRpm": 2.4,
            "setpointMotorRpm": 123,
            "gLoad": 0.01,
            "safetyAction": "none",
            "bpm": 73,
        },
    )
    assert b.link.ranks() == [0, 1, 2]
    assert b.link.to(EVENTS_PATH)[0]["events"] == (
        {"seq": 0, "t": START, "kind": "phase", "detail": "phase 0", "actor": "system"},
    )


async def test_ex1_a_session_that_is_up_to_date_sends_every_five_seconds(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    for _ in range(1 + 3 * int(TELEMETRY_PERIOD)):
        record.tick(1.0)
        await b.step()

    sizes = [len(batch) for batch in b.link.batches()]
    assert sizes == [1, 5, 5, 5]


async def test_ex2_the_cursor_is_written_when_the_dashboard_acknowledges(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(3.0)
    record.event(2)

    await b.step()

    cursor = cursor_of_record(record.path)
    assert cursor == Cursor(
        local_ref="ref-1",
        session_id="cloud-1",
        boot_id=BOOT,
        ticks_offset=(record.path / "ticks.csv").stat().st_size,
        last_t=START + 2000,
        events_offset=(record.path / "events.jsonl").stat().st_size,
        last_seq=1,
    )
    assert cursor_of(record.path).stat().st_mode & 0o777 == 0o600


async def test_ex2_a_batch_the_link_holds_moves_nothing_and_is_sent_again_whole(
    tmp_path: Path,
) -> None:
    """A lost link, a contract the server refuses, a key it does not accept: nothing dropped."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    before = cursor_of_record(record.path)

    b.link.answer(TELEMETRY_PATH, Held("contract_unsupported (HTTP 426): Unsupported"))
    for _ in range(2 * int(RETRY_PERIOD)):
        record.tick(1.0)
        await b.step()
    held = b.link.to(TELEMETRY_PATH)[1:]
    assert len(held) == 2, "asked again once per retry period, not at every step"
    assert cursor_of_record(record.path) == before

    del b.link.answers[TELEMETRY_PATH]
    record.tick(1.0)
    await b.step(float(RETRY_PERIOD))

    # Every second since the last acknowledgement goes now, in one batch, from disk.
    last = record.ticks // 5 - 1
    assert b.link.batches()[-1][0] == 1.0
    assert b.link.seconds()[-last:] == [float(second) for second in range(1, last + 1)]
    assert cursor_of_record(record.path).last_t == START + last * 1000


async def test_ex7_nothing_waits_in_memory_a_long_outage_is_sent_from_disk_in_batches(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    b.link.answer(TELEMETRY_PATH, DOWN)
    record.tick(2 * MAX_POINTS + 40.0)
    await b.step(float(TELEMETRY_PERIOD))

    del b.link.answers[TELEMETRY_PATH]
    await b.step(float(RETRY_PERIOD))
    await b.step(float(CATCH_UP_PERIOD))
    await b.step(float(CATCH_UP_PERIOD))

    sizes = [len(batch) for batch in b.link.batches()]
    assert sizes == [1, MAX_POINTS, MAX_POINTS, MAX_POINTS, 40]
    sent = b.link.seconds()
    assert sent[1 + MAX_POINTS :] == [float(second) for second in range(1, 2 * MAX_POINTS + 41)]


async def test_what_the_dashboard_did_not_store_is_said_counted_and_passed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path)
    b.link.answer(TELEMETRY_PATH, stored(1, rejected=2))
    b.link.answer(EVENTS_PATH, stored(1, rejected=1))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(3.0)
    record.event(2)

    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.step()

    assert "2 points of session cloud-1 acknowledged but not stored" in caplog.text
    assert "1 events of session cloud-1 acknowledged but not stored" in caplog.text
    cursor = cursor_of_record(record.path)
    assert (cursor.rejected_points, cursor.rejected_events) == (2, 1)
    # They are behind the cursor: sending them again would get the same answer.
    assert cursor.last_t == START + 2000
    record.tick(5.0)
    await b.run(5)
    assert b.link.seconds() == [float(second) for second in range(8)]
    assert cursor_of_record(record.path).rejected_points == 4


async def test_lines_of_a_record_that_cannot_be_read_are_passed_over_and_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """They do not hold back what follows them, and they do not vanish in silence."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    record.event()
    with (record.path / "ticks.csv").open("ab") as ticks:
        ticks.write(b"not,a,tick\n\x00\x00\n")
    with (record.path / "events.jsonl").open("ab") as events:
        events.write(b"{not an event}\n")
    record.tick(2.0)
    record.event()

    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.step()

    assert "2 lines of ticks of the record of session cloud-1 cannot be read" in caplog.text
    assert "1 lines of events of the record of session cloud-1 cannot be read" in caplog.text
    assert b.link.seconds() == [0.0, 1.0, 2.0, 3.0]
    # The line that is no event keeps its rank: the one after it is the third.
    assert b.link.ranks() == [0, 2]
    caplog.clear()
    with (record.path / "events.jsonl").open("ab") as events:
        events.write(b"[]\n")
    await b.run(int(TELEMETRY_PERIOD))
    assert "1 lines of events of the record of session cloud-1 cannot be read" in caplog.text
    assert cursor_of_record(record.path).last_seq == 3


@pytest.mark.parametrize("rejected", [0, -1, True, "many", None, 2.5])
async def test_only_a_positive_count_is_a_count_of_what_was_not_stored(
    tmp_path: Path, rejected: object, caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path)
    b.link.answer(TELEMETRY_PATH, Acked({"stored": 1, "rejected": rejected}))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()

    assert cursor_of_record(record.path).rejected_points == 0
    assert "not stored" not in caplog.text


async def test_a_batch_refused_for_good_is_counted_and_passed_and_what_follows_goes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path)
    b.link.answer(
        TELEMETRY_PATH, Refusal("invalid_request (HTTP 400): Malformed telemetry point", True)
    )
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    record.event()

    await b.step()

    assert "telemetry refused for good" in caplog.text
    cursor = cursor_of_record(record.path)
    assert (cursor.refused, cursor.last_t, cursor.last_seq) == (1, START + 1000, 0)
    del b.link.answers[TELEMETRY_PATH]
    record.tick(5.0)
    await b.run(5)
    assert b.link.seconds()[2:] == [2.0, 3.0, 4.0, 5.0, 6.0]


async def test_a_batch_refused_without_a_final_code_is_tried_three_times_then_passed(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(TELEMETRY_PATH, Refusal("request_failed (HTTP 400): try again", False))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    record.event()

    await b.step()
    assert cursor_of_record(record.path).last_t is None
    assert b.link.to(EVENTS_PATH) == [], "what follows waits for the retry"
    await b.run(int(RETRY_PERIOD) - 1)
    assert len(b.link.to(TELEMETRY_PATH)) == 1, "not before the retry period"
    for _ in range(MAX_REFUSALS - 1):
        await b.step(float(RETRY_PERIOD))

    assert len(b.link.to(TELEMETRY_PATH)) == MAX_REFUSALS
    cursor = cursor_of_record(record.path)
    assert (cursor.refused, cursor.last_t) == (1, START + 1000)
    assert b.link.ranks() == [0]


async def test_a_refusal_that_clears_is_not_counted(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.link.answer(TELEMETRY_PATH, Refusal("request_failed (HTTP 400): try again", False))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    await b.step()

    del b.link.answers[TELEMETRY_PATH]
    await b.step(float(RETRY_PERIOD))

    cursor = cursor_of_record(record.path)
    assert (cursor.refused, cursor.last_t) == (0, START + 16_000 - 15_000 + 0)


async def test_events_refused_for_good_are_counted_and_passed(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.link.answer(EVENTS_PATH, Refusal("invalid_request (HTTP 400): Malformed event", True))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    record.event(3)

    await b.step()

    cursor = cursor_of_record(record.path)
    assert (cursor.refused, cursor.last_seq) == (1, 2)


@pytest.mark.parametrize("answer", [DOWN, Refusal("request_failed (HTTP 400): later", False)])
async def test_events_the_link_does_not_take_now_are_sent_again_later(
    tmp_path: Path, answer: Held | Refusal
) -> None:
    b = bench(tmp_path)
    b.link.answer(EVENTS_PATH, answer)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    record.event(2)
    await b.step()
    assert cursor_of_record(record.path).last_seq == -1

    del b.link.answers[EVENTS_PATH]
    await b.step(float(RETRY_PERIOD))

    assert b.link.ranks() == [0, 1, 0, 1]
    assert cursor_of_record(record.path).last_seq == 1


async def test_a_dashboard_older_than_the_events_route_gets_the_rest_and_the_events_stay_owed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path)
    b.link.answer(EVENTS_PATH, RouteMissing())
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(3.0)
    record.event(2)
    await b.step()
    assert "does not take events" in caplog.text
    assert b.link.seconds() == [0.0, 1.0, 2.0]

    # The telemetry goes on, and the route is asked again only once a minute.
    for _ in range(int(EVENTS_RETRY_PERIOD) - 1):
        record.tick(1.0)
        await b.step()
    assert len(b.link.to(EVENTS_PATH)) == 1
    assert b.link.seconds()[-1] >= 55.0
    record.tick(1.0)
    await b.step()
    assert len(b.link.to(EVENTS_PATH)) == 2
    assert caplog.text.count("does not take events") == 1

    # The session ends: its end is sent without waiting for the events.
    record.close()
    b.uplink.finished(ENDED)
    await b.run(int(CATCH_UP_PERIOD))
    assert len(b.link.to(END_PATH)) == 1
    cursor = cursor_of_record(record.path)
    assert (cursor.end, cursor.state, cursor.last_seq) == ("sent", "pending", -1)
    assert b.uplink.owed == 0, "set aside until the next start, not asked for ever"


async def test_the_events_owed_to_an_older_dashboard_are_sent_once_it_takes_them(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(EVENTS_PATH, RouteMissing())
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    record.event(3)
    await b.step()
    record.close()
    await b.run(int(TELEMETRY_PERIOD))
    assert cursor_of_record(record.path).end == "sent"
    assert b.uplink.owed == 1, "the running session still owes its events"

    del b.link.answers[EVENTS_PATH]
    again = b.restarted()
    await again.run(2)

    assert b.link.ranks() == [0, 1, 2, 0, 1, 2]
    assert len(b.link.to(END_PATH)) == 1, "the end is not sent twice"
    assert cursor_of_record(record.path).state == "complete"


async def test_a_dashboard_that_does_not_know_the_telemetry_route_holds_the_sending(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(TELEMETRY_PATH, RouteMissing())
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()
    await b.step()

    assert len(b.link.to(TELEMETRY_PATH)) == 1
    assert cursor_of_record(record.path).last_t is None


# =========================================================================
# The end
# =========================================================================


async def test_the_end_is_the_record_s_sent_after_its_last_point_and_closes_the_cursor(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(4.0)
    await b.step()
    record.tick(2.0)
    record.event()
    record.close("operator_stop", stop="operator pressed STOP")
    b.uplink.finished(RuntimeEnd(failed=True, reason="what the runtime said, unused"))

    # Its last points wait their turn, two seconds after the batch before them.
    await b.step()
    assert b.link.paths() == ["local", "telemetry"]
    await b.step()

    assert b.link.paths() == ["local", "telemetry", "telemetry", "events", "end"]
    assert b.link.to(END_PATH) == [
        {
            "sessionId": "cloud-1",
            "failed": False,
            "reason": "operator_stop: operator pressed STOP",
            # On the session's own axis: its start plus the time the record ran.
            "endedAt": START + 6000,
        }
    ]
    cursor = cursor_of_record(record.path)
    assert (cursor.end, cursor.state) == ("sent", "complete")
    assert b.uplink.owed == 0
    assert not b.uplink.running


@pytest.mark.parametrize(
    ("reason", "failed"),
    [
        ("programme_complete", False),
        ("operator_stop", False),
        ("emergency_stop", True),
        ("safety_verdict", True),
        ("tick_exception", True),
        ("shutdown", True),
        ("interrupted", True),
        ("superseded", True),
    ],
)
async def test_a_record_s_end_is_a_failure_unless_it_completed_or_the_operator_ended_it(
    tmp_path: Path, reason: str, failed: bool
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    record.close(reason, stop=None)
    b.uplink.finished(ENDED)

    await b.step()

    end = b.link.to(END_PATH)[0]
    assert (end["failed"], end["reason"]) == (failed, reason)


async def test_a_record_that_closes_before_the_runtime_is_done_ends_the_session_then(
    tmp_path: Path,
) -> None:
    """Behind a latched fault the runtime waits for an operator; the session is over."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    await b.step()
    record.close("safety_verdict", stop=None)

    await b.run(int(TELEMETRY_PERIOD))

    assert [body["reason"] for body in b.link.to(END_PATH)] == ["safety_verdict"]
    assert b.uplink.running, "the runtime has not finished: the machine is still busy"
    assert b.uplink.session_id == "cloud-1"
    # The dashboard no longer holds it active: asked, it would seem to want a stop.
    assert b.following() is None
    assert b.uplink.owed == 0
    requests = len(b.link.sent)
    await b.run(12)
    b.uplink.finished(RuntimeEnd(failed=True, reason="safety_verdict"))
    await b.run(3)
    assert len(b.link.sent) == requests, "nothing more is owed, nothing more is sent"
    assert not b.uplink.running


async def test_an_end_the_link_holds_is_sent_again_and_the_cursor_says_pending(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(END_PATH, DOWN)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    record.close()
    b.uplink.finished(ENDED)

    await b.step()

    assert cursor_of_record(record.path).end == "pending"
    assert b.uplink.owed == 1
    del b.link.answers[END_PATH]
    await b.step(float(RETRY_PERIOD))
    assert len(b.link.to(END_PATH)) == 2
    assert cursor_of_record(record.path).state == "complete"
    assert len(b.link.to(TELEMETRY_PATH)) == 1, "what was acknowledged is not sent again"


async def test_an_end_refused_for_good_is_counted_and_the_record_is_closed(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.link.answer(END_PATH, Refusal("session_not_found (HTTP 400): Session not found", True))
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    await b.step()
    record.close()
    b.uplink.finished(ENDED)

    await b.step()

    cursor = cursor_of_record(record.path)
    assert (cursor.end, cursor.state, cursor.refused) == ("sent", "complete", 1)


async def test_an_ended_session_whose_record_is_not_closed_yet_waits_then_ends_as_the_runtime_said(
    tmp_path: Path,
) -> None:
    """The journal closes a record a moment after the runtime ends; a disk may never do it."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(3.0)
    await b.step()
    b.uplink.finished(RuntimeEnd(failed=True, reason="emergency_stop: e-stop"))

    await b.run(int(CLOSE_GRACE) - 1)
    assert b.link.to(END_PATH) == []
    await b.run(2)

    assert b.link.to(END_PATH) == [
        {
            "sessionId": "cloud-1",
            "failed": True,
            "reason": "emergency_stop: e-stop",
            "endedAt": START + 1000,
        }
    ]
    assert cursor_of_record(record.path).state == "complete"


async def test_a_record_closed_in_the_grace_ends_as_the_record_says(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(3.0)
    await b.step()
    b.uplink.finished(RuntimeEnd(failed=True, reason="what the runtime said"))
    await b.run(2)
    assert b.link.to(END_PATH) == []

    record.close("programme_complete", stop=None)
    await b.run(int(CATCH_UP_PERIOD))

    assert [body["reason"] for body in b.link.to(END_PATH)] == ["programme_complete"]


async def test_the_end_of_a_session_the_link_did_not_see_end_is_the_record_s(
    tmp_path: Path,
) -> None:
    """A second session starts before the link saw the first end: the record knows how."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    first = recording(tmp_path, b.clock, "ref-first")
    b.disk.current = first.path
    first.tick(2.0)
    await b.step()
    first.close("operator_stop", stop=None)

    b.uplink.begin(armed(b.clock))
    second = recording(tmp_path, b.clock, "ref-second")
    b.disk.current = second.path
    second.tick(1.0)
    await b.step()
    await b.run(int(CATCH_UP_PERIOD))

    ends = b.link.to(END_PATH)
    assert [(body["sessionId"], body["reason"]) for body in ends] == [("cloud-1", "operator_stop")]
    assert b.uplink.session_id == "cloud-2"


async def test_a_session_without_a_record_that_nobody_saw_end_says_so(tmp_path: Path) -> None:
    b = bench(tmp_path, recording=False)
    b.uplink.begin(armed(b.clock, remote="first"))
    b.uplink.begin(armed(b.clock, remote="second"))

    await b.step()

    ends = b.link.to(END_PATH)
    assert [(body["sessionId"], body["reason"]) for body in ends] == [("first", UNOBSERVED_END)]
    assert b.uplink.session_id == "second"
    assert b.cancelled == []


async def test_a_refused_launch_is_owed_as_a_failed_session_the_dashboard_dates(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    assert not b.refusal_owed()
    b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")
    assert b.refusal_owed()
    assert b.uplink.owed == 1

    await b.step()

    assert b.link.sent == [
        (
            END_PATH,
            {
                "sessionId": "k17pending",
                "failed": True,
                "reason": "refusee par la machine : console occupee",
            },
        )
    ]
    assert not b.refusal_owed()
    assert b.uplink.owed == 0


async def test_a_session_the_dashboard_cannot_be_told_of_does_not_stop_launches_being_asked_for(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a refused launch does: the dashboard would hand the same one out again."""
    b = bench(tmp_path, recording=False)
    b.link.answer(LOCAL_PATH, Acked({}))
    b.uplink.begin(armed(b.clock))
    await b.step()
    b.uplink.finished(ENDED)
    await b.step()
    assert b.uplink.owed == 1
    assert not b.refusal_owed()

    # A launch is then refused at the machine: its end takes the one place there is.
    b.uplink.owe_refusal("k17pending", "refusee par la machine : verdict a acquitter")
    assert b.refusal_owed()
    await b.step()

    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["k17pending"]
    assert "could not be delivered and is given up" in caplog.text
    assert not b.refusal_owed()


async def test_finishing_when_nothing_runs_does_nothing(tmp_path: Path) -> None:
    b = bench(tmp_path)
    b.uplink.finished(ENDED)
    await b.step()
    assert b.link.sent == []
    assert b.uplink.owed == 0
    assert b.uplink.session_id is None


# =========================================================================
# EX-4: after a restart
# =========================================================================


async def left_by_a_killed_console(tmp_path: Path) -> tuple[Bench, Path]:
    """A session of 40 s; the link is lost at 20 s; the console is killed at 40 s."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-killed")
    b.disk.current = record.path
    for second in range(40):
        if second == 20:
            b.link.answer(TELEMETRY_PATH, DOWN)
            b.link.answer(EVENTS_PATH, DOWN)
            b.link.answer(END_PATH, DOWN)
        record.tick(1.0)
        if second % 10 == 0:
            record.event()
        await b.step()
    with (record.path / "ticks.csv").open("ab") as handle:
        handle.write(b"40.0,active,manual,hold,OPER")  # the write the kill interrupted
    return b, record.path


async def test_ex4_a_record_a_killed_console_left_is_taken_up_where_the_dashboard_stopped(
    tmp_path: Path,
) -> None:
    killed, record = await left_by_a_killed_console(tmp_path)
    acknowledged = cursor_of_record(record)
    assert acknowledged.last_t is not None
    last_second = (acknowledged.last_t - START) // 1000
    assert 10 <= last_second < 20
    before = len(killed.link.sent)
    for path in (TELEMETRY_PATH, EVENTS_PATH, END_PATH):
        del killed.link.answers[path]

    restarted = killed.restarted()
    restarted.clock.advance(Seconds(60.0))
    await restarted.run(4)

    after = restarted.link.sent[before:]
    assert [path.rsplit("/", 1)[-1] for path, _body in after] == ["telemetry", "events", "end"]
    # Not declared again: the cursor holds the session's identifier.
    assert after[0][1]["sessionId"] == "cloud-1"
    sent = restarted.link.seconds()
    assert sent[-(39 - last_second) :] == [float(s) for s in range(last_second + 1, 40)]
    # Every second of the record reached the dashboard, the acknowledged ones once.
    acknowledged_before = [s for s in sent[: -(39 - last_second)] if s <= last_second]
    assert sorted(set(acknowledged_before)) == [float(s) for s in range(last_second + 1)]
    assert restarted.link.ranks()[-2:] == [2, 3]
    assert after[2][1] == {
        "sessionId": "cloud-1",
        "failed": True,
        "reason": "interrupted",
        # The last tick the disk holds: the last instant the console ran the session.
        "endedAt": START + 39_800,
    }
    assert cursor_of_record(record).state == "complete"
    assert restarted.uplink.owed == 0


SENT_FROM = frozenset({"manifest.json", "ticks.csv", "events.jsonl"})
"""The three files of a record the dashboard is sent from."""


async def test_nothing_of_a_record_is_opened_but_its_manifest_its_ticks_and_its_events(
    tmp_path: Path,
) -> None:
    """The drive's frames, the sensors and the ECG blocks travel with the whole record, not here.

    Each is replaced by a pipe nobody writes to: opening one would never
    return, and the record would never be delivered. So are the two files of
    the logbook, next to the records: what the console writes of itself while
    no session is recorded is no session's, and is neither read nor sent.
    """
    killed, record = await left_by_a_killed_console(tmp_path)
    for path in (TELEMETRY_PATH, EVENTS_PATH, END_PATH):
        del killed.link.answers[path]
    others = sorted(entry.name for entry in record.iterdir() if entry.name not in SENT_FROM)
    assert "drive_frames.jsonl" in others
    for name in others:
        entry = record / name
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        os.mkfifo(entry)
    logbook = tmp_path / LOGBOOK
    logbook.mkdir()
    for name in (LOGBOOK_ACTIVE, LOGBOOK_PREVIOUS):
        os.mkfifo(logbook / name)

    restarted = killed.restarted()
    restarted.clock.advance(Seconds(60.0))
    await restarted.run(4)

    assert restarted.link.seconds()[-1] == 39.0
    assert restarted.link.paths()[-1] == "end"
    assert cursor_of_record(record).state == "complete"
    assert restarted.uplink.owed == 0
    assert sorted(entry.name for entry in logbook.iterdir()) == [LOGBOOK_PREVIOUS, LOGBOOK_ACTIVE]


async def test_a_record_the_retention_removes_while_it_is_owed_holds_nothing_back(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A purge runs on the journal's thread whatever the sending is doing. The record that
    is gone is left, and said in the log; the next one is sent; the cursor of the first,
    which the retention does not remove, goes at the next start."""
    old = bench(tmp_path)
    purged = recording(tmp_path, old.clock, "ref-purged")
    purged.tick(400.0, hertz=1)
    purged.close()
    tie(purged.path, Cursor(session_id="cloud-purged"))
    old.clock.advance(Seconds(3600.0))
    kept = recording(tmp_path, old.clock, "ref-kept")
    kept.tick(3.0)
    kept.close()
    tie(kept.path, Cursor(session_id="cloud-kept"))
    old.clock.advance(Seconds(3600.0))
    b = old.restarted()
    await b.step()
    assert len(b.link.seconds("cloud-purged")) == MAX_POINTS, "its first batch has gone"

    shutil.rmtree(purged.path)
    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(6)

    assert "the record of session cloud-purged cannot be read" in caplog.text
    assert len(b.link.seconds("cloud-purged")) == MAX_POINTS
    assert b.link.seconds("cloud-kept") == [0.0, 1.0, 2.0]
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-kept"]
    assert b.said == []
    assert cursor_of(purged.path).exists(), "not the retention's to remove, nor this run's"

    again = b.restarted()
    await again.run(3)

    assert not cursor_of(purged.path).exists()
    assert again.uplink.owed == 0
    assert len(again.link.sent) == len(b.link.sent), "nothing is sent of a record that is gone"
    assert cursor_of_record(kept.path).state == "complete"


async def test_ex4_what_earlier_runs_left_is_taken_up_oldest_first_after_the_running_session(
    tmp_path: Path,
) -> None:
    old = bench(tmp_path)
    clock = old.clock
    names: list[str] = []
    for index in range(3):
        clock.advance(Seconds(3600.0))
        left = recording(tmp_path, clock, f"ref-{index}")
        left.tick(3.0)
        left.close("operator_stop", stop=None)
        tie(left.path, Cursor(boot_id="an-earlier-boot"))
        names.append(left.path.name)
    # Never opened by the synchronisation, and already whole: both left alone.
    clock.advance(Seconds(3600.0))
    unopened = recording(tmp_path, clock, "ref-unopened")
    unopened.tick(3.0)
    clock.advance(Seconds(3600.0))
    whole = recording(tmp_path, clock, "ref-whole")
    whole.tick(3.0)
    tie(whole.path, Cursor(state="complete", end="sent"))
    clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    live.tick(1.0)
    await b.step()
    for _ in range(8):
        live.tick(1.0)
        await b.step()

    declared = [body["localRef"] for body in b.link.to(LOCAL_PATH)]
    assert declared == ["ref-now", "ref-0", "ref-1", "ref-2"]
    assert b.link.paths()[:2] == ["local", "telemetry"], "the running session first"
    # A record of an earlier start of the system cannot say how long ago it began.
    assert all("sessionAgeMs" not in body for body in b.link.to(LOCAL_PATH)[1:])
    assert [cursor_of_record(tmp_path / name).state for name in names] == ["complete"] * 3
    assert load(unopened.path) == NoCursor()
    assert b.uplink.owed == 1


async def test_ex4_a_record_declared_after_a_restart_is_described_by_its_manifest_alone(
    tmp_path: Path,
) -> None:
    """The record holds no name: the operator is its alias, and the session is dated by its age."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-undeclared")
    left.tick(5.0)
    tie(left.path, Cursor(boot_id=BOOT))
    old.clock.advance(Seconds(300.0))

    b = old.restarted()
    await b.run(3)

    assert b.link.to(LOCAL_PATH) == [
        {
            "localRef": "ref-undeclared",
            "kind": "manual",
            "startedAt": START,
            "operatorName": "op-5",
            # Same start of the system: its monotonic clock still dates the record.
            "sessionAgeMs": 301_000,
            "occupancy": "bench",
        }
    ]
    assert b.link.to(END_PATH)[0]["reason"] == "interrupted"


@pytest.mark.parametrize("boot_now", ["another-boot-0001", None])
async def test_ex4_after_a_restart_of_the_system_the_age_of_a_session_is_not_claimed(
    tmp_path: Path, boot_now: str | None
) -> None:
    old = bench(tmp_path, boot_id=boot_now)
    left = recording(tmp_path, old.clock, "ref-undeclared")
    left.tick(2.0)
    tie(left.path, Cursor(boot_id=boot_now and BOOT))

    b = old.restarted(boot_id=boot_now)
    await b.run(3)

    assert "sessionAgeMs" not in b.link.to(LOCAL_PATH)[0]


async def test_ex4_a_clock_that_reads_before_the_record_s_start_claims_no_age(
    tmp_path: Path,
) -> None:
    later = ManualClock(Monotonic(1000.0), UnixMillis(EPOCH_MS))
    left = recording(tmp_path, later, "ref-undeclared")
    left.tick(2.0)
    tie(left.path, Cursor(boot_id=BOOT))

    b = bench(tmp_path)
    await b.run(3)

    assert "sessionAgeMs" not in b.link.to(LOCAL_PATH)[0]


async def test_ex4_a_record_whose_cursor_is_unusable_is_sent_again_from_its_beginning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Truncated by a power cut, say. The dashboard stores once what it already has."""
    killed, record = await left_by_a_killed_console(tmp_path)
    cursor_of(record).write_bytes(b'{"schema_version":1,"session_id":"cloud-1","ticks_of')
    before = len(killed.link.to(TELEMETRY_PATH))
    for path in (TELEMETRY_PATH, EVENTS_PATH, END_PATH):
        del killed.link.answers[path]
    killed.link.answer(LOCAL_PATH, Acked({"sessionId": "cloud-1"}))

    restarted = killed.restarted()
    await restarted.run(4)

    assert "cannot be used (invalid): sent again from its start" in caplog.text
    # Declared again under the same reference: the dashboard answers the same session.
    assert [body["localRef"] for body in restarted.link.to(LOCAL_PATH)] == [
        "ref-killed",
        "ref-killed",
    ]
    again = restarted.link.batches()[before:]
    assert [second for batch in again for second in batch] == [float(s) for s in range(40)]
    assert restarted.link.ranks()[-4:] == [0, 1, 2, 3]
    assert cursor_of_record(record).state == "complete"


async def test_ex4_a_launch_taken_up_without_a_cursor_is_confirmed_again_and_nothing_is_stopped(
    tmp_path: Path,
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-remote", remote="k17remote")
    left.tick(3.0)
    left.close("programme_complete", stop=None)
    cursor_of(left.path).write_bytes(b"")
    old.link.answer(START_PATH, Refusal("session_not_pending (HTTP 400): active", True))

    b = old.restarted()
    await b.run(3)

    assert b.link.paths() == ["start", "telemetry", "end"]
    assert b.link.to(START_PATH)[0]["sessionId"] == "k17remote"
    assert b.cancelled == [], "no session is running: nothing to stop"
    assert b.link.to(END_PATH)[0]["sessionId"] == "k17remote"


async def test_a_session_tied_to_its_record_while_the_link_is_held_has_its_cursor_at_once(
    tmp_path: Path,
) -> None:
    """Written when the two are tied, whatever the link says: not at the first answer.

    With the cursor on disk a console that restarts takes the record up where
    the dashboard stopped, and can still say how long ago the session began.
    """
    b = bench(tmp_path)
    b.link.answer(END_PATH, DOWN)
    b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")
    await b.step()  # the link holds: nothing is sent for a while
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-held")
    b.disk.current = record.path
    record.tick(1.0)

    await b.step()

    # Declared all the same, by the watch's task; the sending itself is held.
    assert b.link.paths() == ["end", "local"]
    assert load(record.path) == Cursor(local_ref="ref-held", session_id="cloud-1", boot_id=BOOT)


async def test_ex4_a_record_never_tied_to_a_cursor_is_sent_whole_at_the_next_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The console was killed in the session's first second, or its disk refused the cursor."""
    first = bench(tmp_path)
    await first.step()  # the directory is listed once: nothing older is there
    lost = recording(tmp_path, first.clock, "ref-lost")
    lost.tick(4.0)
    lost.event()
    assert load(lost.path) == NoCursor()
    first.clock.advance(Seconds(600.0))

    b = first.restarted()
    await b.run(4)

    assert "was never tied to a cursor: sent from its start" in caplog.text
    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-lost"]
    assert b.link.seconds() == [0.0, 1.0, 2.0, 3.0]
    assert b.link.ranks() == [0]
    # Nobody closed it: the console stopped before the session did.
    assert [body["reason"] for body in b.link.to(END_PATH)] == ["interrupted"]
    assert cursor_of_record(lost.path).state == "complete"
    assert b.uplink.owed == 0


async def test_the_record_open_now_is_not_listed_with_those_of_before(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Not tied to its session yet (its manifest cannot be read): it is the running one's."""
    first = bench(tmp_path)
    await first.step()  # the directory was listed once before: nothing older is there

    b = first.restarted()
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = record.path
    record.tick(2.0)
    (record.path / "manifest.json").rename(record.path / "manifest.away")
    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(4)
    # Declared all the same, under the reference its directory is named after.
    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-now"]
    assert b.said == []
    assert "left until the next start" not in caplog.text
    assert b.uplink.owed == 1, "the running session, and nothing taken for an older record"

    (record.path / "manifest.away").rename(record.path / "manifest.json")
    record.tick(2.0)
    await b.run(3)

    assert len(b.link.to(LOCAL_PATH)) == 1
    assert b.link.seconds("cloud-1") == [0.0, 1.0, 2.0, 3.0]


async def test_ex4_a_record_older_than_the_synchronisation_is_never_sent(tmp_path: Path) -> None:
    """There, without a cursor, the first time the directory is listed: left alone for good."""
    old = bench(tmp_path)
    older = recording(tmp_path, old.clock, "ref-older-software")
    older.tick(3.0)
    older.close()

    for _ in range(2):
        b = old.restarted()
        await b.run(4)
        assert b.link.sent == []
        assert load(older.path) == NoCursor()


async def test_a_complete_cursor_of_another_record_does_not_hide_the_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A cursor file copied or renamed by hand says nothing of the record it sits next to."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-mine")
    left.tick(3.0)
    left.close()
    foreign = Cursor(local_ref="ref-other", session_id="cloud-9", state="complete", end="sent")
    assert isinstance(store(left.path, foreign), Ok)

    b = old.restarted()
    await b.run(4)

    assert "cannot be used (of another record): sent again from its start" in caplog.text
    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-mine"]
    assert b.link.seconds("cloud-1") == [0.0, 1.0, 2.0]
    assert cursor_of_record(left.path).local_ref == "ref-mine"


async def test_a_record_with_a_line_too_long_is_sent_whole_and_does_not_hold_back_the_next(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """More zeroes than one reading holds, with no end of line: passed over, said, and on."""
    old = bench(tmp_path)
    torn = recording(tmp_path, old.clock, "ref-torn")
    torn.tick(2.0)
    with (torn.path / "ticks.csv").open("ab") as handle:
        handle.write(b"\x00" * (READ_CHUNK * 2 + 77) + b"\n")
    torn.tick(2.0)
    torn.close()
    tie(torn.path, Cursor(session_id="cloud-torn"))
    old.clock.advance(Seconds(3600.0))
    behind = recording(tmp_path, old.clock, "ref-behind")
    behind.tick(2.0)
    behind.close()
    tie(behind.path, Cursor(session_id="cloud-behind"))

    b = old.restarted()
    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(12)

    assert b.link.seconds("cloud-torn") == [0.0, 1.0, 2.0, 3.0]
    assert "1 lines of ticks of the record of session cloud-torn cannot be read" in caplog.text
    assert "does not match its record" not in caplog.text
    assert b.link.seconds("cloud-behind") == [0.0, 1.0]
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-torn", "cloud-behind"]
    assert b.uplink.owed == 0


async def test_a_record_with_an_event_line_too_long_is_sent_whole_with_the_ranks_that_follow(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """As for the ticks: passed over across several readings, its rank kept, and on."""
    old = bench(tmp_path)
    torn = recording(tmp_path, old.clock, "ref-torn")
    torn.tick(2.0)
    torn.event(2)
    with (torn.path / "events.jsonl").open("ab") as handle:
        handle.write(b"\x00" * (READ_CHUNK * 2 + 77) + b"\n")
    torn.events += 1  # the zeroes are a line of the file: they take a rank
    torn.event()
    torn.close()
    tie(torn.path, Cursor(session_id="cloud-torn"))

    b = old.restarted()
    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(8)

    assert b.link.ranks() == [0, 1, 3]
    assert "1 lines of events of the record of session cloud-torn cannot be read" in caplog.text
    assert "does not match its record" not in caplog.text
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-torn"]
    assert cursor_of_record(torn.path).state == "complete"


async def test_a_record_the_console_still_has_open_is_not_ended_as_interrupted(
    tmp_path: Path,
) -> None:
    """Taken up again from disk in this run while the journal has not closed it yet.

    The runtime ended the session, the link then had a refused launch to
    report, and the record went back among those waiting. Read again, it is
    not closed and nothing in memory says how it ended: it is still the
    record the console has open, so its end is waited for, not made up.
    """
    b = bench(tmp_path)
    b.link.answer(END_PATH, DOWN)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    await b.step()
    b.uplink.finished(ENDED)  # the journal has not closed the record yet
    b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")
    await b.step()
    del b.link.answers[END_PATH]

    b.clock.advance(RETRY_PERIOD)
    await b.run(int(CLOSE_GRACE) + 5)
    assert [body["sessionId"] for body in b.link.to(END_PATH)][1:] == ["k17pending"]

    record.close()
    await b.run(3)
    ends = b.link.to(END_PATH)[2:]
    assert [(body["sessionId"], body["reason"]) for body in ends] == [
        ("cloud-1", "operator_stop: fini")
    ]
    assert b.uplink.owed == 0


async def test_a_record_cut_short_whose_last_bytes_are_no_line_ends_at_its_last_point(
    tmp_path: Path,
) -> None:
    """Killed, then zeroes where the last ticks were: the tail of the file dates nothing.

    The end is dated at the last point the record gave, the first tick of its
    last second, and not at the start of the session.
    """
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-cut")
    left.tick(4.0)
    with (left.path / "ticks.csv").open("ab") as handle:
        handle.write(b"\x00" * (TAIL_CHUNK + 4096))
    tie(left.path, Cursor(session_id="cloud-cut"))

    b = old.restarted()
    await b.run(3)

    assert b.link.seconds("cloud-cut") == [0.0, 1.0, 2.0, 3.0]
    assert b.link.to(END_PATH) == [
        {"sessionId": "cloud-cut", "failed": True, "reason": "interrupted", "endedAt": START + 3000}
    ]


async def test_a_record_cut_short_with_no_point_at_all_ends_at_its_start(tmp_path: Path) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-empty")
    with (left.path / "ticks.csv").open("ab") as handle:
        handle.write(b"\x00" * (TAIL_CHUNK + 4096))
    tie(left.path, Cursor(session_id="cloud-empty"))

    b = old.restarted()
    await b.run(3)

    assert b.link.to(END_PATH) == [
        {"sessionId": "cloud-empty", "failed": True, "reason": "interrupted", "endedAt": START}
    ]


async def test_a_lost_list_of_older_records_is_said_with_what_it_set_aside(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Deleted by hand, say: a record that was waiting, cursor-less, is not sent, and it is said."""
    first = bench(tmp_path)
    done = recording(tmp_path, first.clock, "ref-done")
    done.tick(1.0)
    done.close()
    tie(done.path, Cursor(session_id="cloud-done", state="complete", end="sent"))
    await first.step()
    first.clock.advance(Seconds(60.0))
    waiting = recording(tmp_path, first.clock, "ref-waiting")
    waiting.tick(2.0)
    (tmp_path / BASELINE_NAME).unlink()

    b = first.restarted()
    with caplog.at_level(logging.WARNING, logger="src.record_uplink"):
        await b.run(3)

    assert b.link.sent == []
    assert b.said == [LIST_LOST.format(count=1)]
    assert "1 records without a cursor were set aside with the list of older records" in caplog.text
    # Once: the list is there again.
    again = b.restarted()
    await again.run(3)
    assert again.said == []


async def test_the_first_list_of_older_records_says_how_many_it_sets_aside(
    tmp_path: Path,
) -> None:
    """The first start of this version on a directory that holds records, with no cursor
    anywhere: they are not sent, and the operator is told how many."""
    old = bench(tmp_path)
    for index in range(3):
        older = recording(tmp_path, old.clock, f"ref-older-{index}")
        older.tick(1.0)
        older.close()
        old.clock.advance(Seconds(60.0))

    b = old.restarted()
    await b.run(2)

    assert b.link.sent == []
    assert b.said == [LIST_LOST.format(count=3)]


async def test_a_cursor_that_does_not_match_its_record_starts_again_from_the_beginning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(4.0)
    left.event(2)
    left.close()
    wrong = Cursor(session_id="cloud-9", ticks_offset=7, last_t=START + 2000, last_seq=5)
    tie(left.path, wrong)

    b = old.restarted()
    await b.run(5)

    assert "does not match its record" in caplog.text
    assert b.link.seconds() == [0.0, 1.0, 2.0, 3.0]
    assert b.link.ranks() == [0, 1]
    assert cursor_of_record(left.path).state == "complete"


# =========================================================================
# EX-5: the running session first, and a paced catch-up
# =========================================================================


async def test_ex5_catching_up_sends_at_most_one_batch_of_300_points_every_2_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(4 * MAX_POINTS + 10.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-1"))
    readings: list[int] = []

    def counted(
        record: Path, *, start_ms: int, cursor: Cursor, with_last_tick: bool
    ) -> Result[Batch, RecordError]:
        readings.append(cursor.ticks_offset)
        return read_batch(record, start_ms=start_ms, cursor=cursor, with_last_tick=with_last_tick)

    monkeypatch.setattr("src.record_uplink.read_batch", counted)

    b = old.restarted()
    times: list[float] = []
    for _ in range(12):
        before = len(b.link.to(TELEMETRY_PATH))
        await b.step()
        if len(b.link.to(TELEMETRY_PATH)) > before:
            assert len(b.link.to(TELEMETRY_PATH)) == before + 1
            times.append(float(b.clock.monotonic()))

    sizes = [len(batch) for batch in b.link.batches()]
    assert sizes == [MAX_POINTS] * 4 + [10]
    assert [later - earlier for earlier, later in pairwise(times)] == [float(CATCH_UP_PERIOD)] * 4
    assert len(b.link.to(END_PATH)) == 1
    # A record that is behind is not even read before its turn: one reading a batch.
    assert len(readings) == len(sizes)
    assert len(set(readings)) == len(readings)


async def test_ex5_nothing_of_an_older_session_is_sent_while_the_running_one_is_behind(
    tmp_path: Path,
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(MAX_POINTS + 50.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    live.tick(2 * MAX_POINTS + 20.0, hertz=1)
    await b.run(10)

    order = [body["sessionId"] for body in b.link.to(TELEMETRY_PATH)]
    assert order == ["cloud-1", "cloud-1", "cloud-1", "cloud-old", "cloud-old"]
    assert b.link.seconds("cloud-1") == [float(s) for s in range(2 * MAX_POINTS + 20)]


async def test_ex5_the_session_that_just_ended_goes_before_the_older_ones(tmp_path: Path) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(2 * MAX_POINTS + 5.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    await b.step()
    assert [body["sessionId"] for body in b.link.to(TELEMETRY_PATH)] == ["cloud-old"]
    # A session runs and ends while the older record is being caught up.
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    live.tick(3.0)
    await b.step()
    live.close()
    b.uplink.finished(ENDED)
    await b.run(8)

    ends = [body["sessionId"] for body in b.link.to(END_PATH)]
    assert ends == ["cloud-1", "cloud-old"]
    assert b.uplink.owed == 0
    assert b.link.seconds("cloud-old") == [float(s) for s in range(2 * MAX_POINTS + 5)]


async def test_ex5_a_running_session_that_waits_to_send_again_holds_back_the_older_ones(
    tmp_path: Path,
) -> None:
    """The running session first, even while it only waits for its next try.

    Its batch was refused in a way the dashboard may lift: until it has gone,
    nothing of an older session is sent, not even its declaration or its end.
    """
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(3.0)
    left.close()
    tie(left.path, Cursor(boot_id="an-earlier-boot"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.link.answer(TELEMETRY_PATH, Refusal("request_failed (HTTP 400): try again", final=False))
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    for _ in range(int(RETRY_PERIOD) - 1):
        live.tick(1.0)
        await b.step()

    assert b.link.paths() == ["local", "telemetry"]
    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-now"]

    del b.link.answers[TELEMETRY_PATH]
    for _ in range(6):
        live.tick(1.0)
        await b.step()
    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-now", "ref-old"]
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-2"]


async def test_ex5_sessions_that_ended_while_the_link_was_down_wait_oldest_first(
    tmp_path: Path,
) -> None:
    """The one that just ended first; every other in the order they were run, whichever run."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(3.0)
    left.close()
    tie(left.path, Cursor(boot_id="an-earlier-boot"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.link.answer(LOCAL_PATH, DOWN)
    await b.step()  # the record of before is found, and held with everything else
    for ref in ("ref-first", "ref-second", "ref-third"):
        b.clock.advance(Seconds(600.0))
        b.uplink.begin(armed(b.clock))
        live = recording(tmp_path, b.clock, ref)
        b.disk.current = live.path
        live.tick(3.0)
        await b.step()
        live.close()
        b.uplink.finished(ENDED)
    assert b.uplink.owed == 4
    assert b.link.to(TELEMETRY_PATH) == []

    del b.link.answers[LOCAL_PATH]
    b.clock.advance(RETRY_PERIOD)
    await b.run(12)

    declared = [body["localRef"] for body in b.link.to(LOCAL_PATH)]
    # Held, each was declared once in vain; served, each once more, in this order.
    assert declared[-4:] == ["ref-third", "ref-old", "ref-first", "ref-second"]
    assert len(b.link.to(END_PATH)) == 4
    assert b.uplink.owed == 0


async def batch_times(b: Bench, steps: int) -> list[tuple[float, str, int]]:
    """Step ``steps`` times; when each batch of telemetry went, for which session, how large."""
    seen: list[tuple[float, str, int]] = []
    for _ in range(steps):
        before = len(b.link.to(TELEMETRY_PATH))
        await b.step()
        for body in b.link.to(TELEMETRY_PATH)[before:]:
            points = cast("tuple[object, ...]", body["points"])
            seen.append((float(b.clock.monotonic()), str(body["sessionId"]), len(points)))
    return seen


async def test_ex5_never_two_batches_less_than_2_s_apart_when_the_running_session_catches_up(
    tmp_path: Path,
) -> None:
    """The last batch of a session that was behind, then an older record: not in one step."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(MAX_POINTS + 40.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    live.tick(MAX_POINTS + 110.0, hertz=1)
    seen = await batch_times(b, 12)

    assert [(session, size) for _at, session, size in seen] == [
        ("cloud-1", MAX_POINTS),
        ("cloud-1", 110),
        ("cloud-old", MAX_POINTS),
        ("cloud-old", 40),
    ]
    gaps = [later - earlier for (earlier, _s, _n), (later, _t, _m) in pairwise(seen)]
    assert gaps == [float(CATCH_UP_PERIOD)] * 3


async def test_ex5_never_two_batches_less_than_2_s_apart_when_a_session_ends_and_is_handed_over(
    tmp_path: Path,
) -> None:
    """Its points of the last seconds wait two seconds after the batch it sent while running."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    seen: list[tuple[float, str, int]] = []
    for _ in range(6):
        record.tick(1.0)
        seen += await batch_times(b, 1)
    record.tick(1.0)
    record.close()
    b.uplink.finished(ENDED)
    seen += await batch_times(b, 4)

    assert [size for _at, _session, size in seen] == [1, 5, 1]
    gaps = [later - earlier for (earlier, _s, _n), (later, _t, _m) in pairwise(seen)]
    assert gaps == [float(TELEMETRY_PERIOD), float(CATCH_UP_PERIOD)]
    assert len(b.link.to(END_PATH)) == 1


async def test_ex5_a_running_session_up_to_date_waits_its_turn_behind_a_batch_of_catching_up(
    tmp_path: Path,
) -> None:
    """One pace for all: its five seconds of points go two seconds after an older batch."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(4 * MAX_POINTS + 0.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    old.clock.advance(Seconds(3600.0))

    b = old.restarted()
    b.uplink.begin(armed(b.clock))
    live = recording(tmp_path, b.clock, "ref-now")
    b.disk.current = live.path
    seen: list[tuple[float, str, int]] = []
    for _ in range(14):
        live.tick(1.0)
        seen += await batch_times(b, 1)

    gaps = [later - earlier for (earlier, _s, _n), (later, _t, _m) in pairwise(seen)]
    assert min(gaps) >= float(CATCH_UP_PERIOD)
    sessions = [session for _at, session, _size in seen]
    assert sessions.count("cloud-old") == 4
    assert sessions.count("cloud-1") >= 2, "the running session is not starved"
    assert b.link.seconds("cloud-1") == [float(s) for s in range(len(b.link.seconds("cloud-1")))]


# =========================================================================
# A disk that does not answer costs the sending, and nothing else
# =========================================================================


class Scripted(RecordIo):
    """The record I/O threads, with what the test wants to happen around one operation."""

    def __init__(self) -> None:
        super().__init__(1)
        self.fail: str | None = None
        self.before: Callable[[], None] | None = None
        self.calls: int = 0

    @override
    async def run[T](
        self,
        work: Callable[[], T],
        timeout: Seconds,
        late: Callable[[T], None] | None = None,
    ) -> Result[T, RecordError]:
        self.calls += 1
        if self.before is not None:
            self.before()
        if self.fail is not None:
            return Err(RecordError("read", self.fail))
        return await super().run(work, timeout, late)


@pytest.mark.parametrize("failure", [BUSY, TIMEOUT])
async def test_a_slow_disk_delays_the_measurements_of_the_running_session_not_its_declaration(
    tmp_path: Path, failure: str
) -> None:
    """Declared from the name of its record's directory: no file is read for that."""
    io = Scripted()
    b = bench(tmp_path, io=io)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    io.fail = failure

    await b.run(3)
    assert b.link.paths() == ["local"]
    assert b.link.to(LOCAL_PATH)[0]["localRef"] == "ref-1"
    assert b.following() == "cloud-1", "its stop can be asked for at once"

    io.fail = None
    await b.step()
    assert b.link.paths() == ["local", "telemetry"]


async def test_a_record_that_cannot_be_read_while_it_runs_is_asked_again_and_the_end_still_goes(
    tmp_path: Path,
) -> None:
    io = Scripted()
    b = bench(tmp_path, io=io)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(2.0)
    await b.step()
    io.fail = "OSError:EIO"
    record.tick(5.0)

    await b.run(int(TELEMETRY_PERIOD))
    assert len(b.link.to(TELEMETRY_PATH)) == 1
    b.uplink.finished(RuntimeEnd(failed=True, reason="safety_verdict"))
    await b.run(int(CLOSE_GRACE) - 1)
    assert b.link.to(END_PATH) == []
    await b.run(2)

    # The disk never gave the record back: the dashboard is told the session is over.
    assert b.link.to(END_PATH) == [
        {
            "sessionId": "cloud-1",
            "failed": True,
            "reason": "safety_verdict",
            "endedAt": START + 6000,
        }
    ]
    assert b.uplink.owed == 0


async def test_a_cursor_the_disk_refuses_is_said_once_and_written_when_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock)
    b.disk.current = record.path
    record.tick(1.0)
    real = store

    def refuse(_record: Path, _cursor: Cursor) -> object:
        return Err(type("E", (), {"detail": "OSError:ENOSPC"})())

    monkeypatch.setattr("src.record_uplink.store", refuse)
    for _ in range(7):
        record.tick(1.0)
        await b.step()

    assert load(record.path) == NoCursor()
    assert caplog.text.count("could not be written (OSError:ENOSPC)") == 1
    # On the console too, once: after a restart it would be sent again from its start.
    assert b.said == [CURSOR_UNWRITTEN]
    # The sending went on all the same: every second sent once, none twice.
    sent = b.link.seconds()
    assert sent == [float(second) for second in range(len(sent))]
    assert len(sent) >= 6

    monkeypatch.setattr("src.record_uplink.store", real)
    for _ in range(5):
        record.tick(1.0)
        await b.step()
    assert cursor_of_record(record.path).last_t == START + int(b.link.seconds()[-1]) * 1000


async def test_a_listing_that_fails_at_startup_is_made_again_later(tmp_path: Path) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-1"))
    io = Scripted()
    io.fail = TIMEOUT

    b = old.restarted(io=io)
    await b.run(int(RETRY_PERIOD))
    assert io.calls == 1, "not at every step"
    assert b.link.sent == []

    io.fail = None
    await b.run(2)
    assert b.link.paths() == ["telemetry", "end"]


@pytest.mark.parametrize("failure", [BUSY, TIMEOUT])
async def test_an_older_record_a_slow_disk_does_not_give_keeps_its_place(
    tmp_path: Path, failure: str
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-1"))
    io = Scripted()

    b = old.restarted(io=io)

    def slow_after_the_listing() -> None:
        if io.calls > 1:
            io.fail = failure

    io.before = slow_after_the_listing
    await b.run(4)
    assert b.link.sent == []
    assert b.uplink.owed == 1

    io.before = None
    io.fail = None
    await b.run(2)
    assert b.link.paths() == ["telemetry", "end"]


async def test_an_older_record_that_cannot_be_read_is_left_until_the_next_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    old = bench(tmp_path)
    broken = recording(tmp_path, old.clock, "ref-broken")
    broken.tick(2.0)
    tie(broken.path, Cursor(session_id="cloud-1"))
    (broken.path / "manifest.json").write_bytes(b"{")
    old.clock.advance(Seconds(60.0))
    good = recording(tmp_path, old.clock, "ref-good")
    good.tick(2.0)
    good.close()
    tie(good.path, Cursor(session_id="cloud-2"))

    b = old.restarted()
    await b.run(6)

    assert f"record {broken.path.name} cannot be read" in caplog.text
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["cloud-2"]
    assert cursor_of_record(broken.path).state == "pending"
    assert b.uplink.owed == 0


async def test_an_older_record_whose_streams_cannot_be_read_is_left_until_the_next_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-1"))
    (left.path / "events.jsonl").unlink()

    b = old.restarted()
    await b.run(4)

    assert (
        "the record of session cloud-1 cannot be read (FileNotFoundError:ENOENT): "
        "left until the next start"
    ) in caplog.text
    assert b.link.sent == []
    assert b.uplink.owed == 0


@pytest.mark.parametrize("failure", [BUSY, TIMEOUT])
async def test_an_older_record_being_sent_waits_for_a_slow_disk(
    tmp_path: Path, failure: str
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock)
    left.tick(2 * MAX_POINTS + 0.0, hertz=1)
    left.close()
    tie(left.path, Cursor(session_id="cloud-1"))
    io = Scripted()

    b = old.restarted(io=io)
    await b.run(2)
    assert len(b.link.to(TELEMETRY_PATH)) == 1
    io.fail = failure
    await b.run(4)
    assert len(b.link.to(TELEMETRY_PATH)) == 1
    assert b.uplink.owed == 1

    io.fail = None
    await b.run(4)
    assert len(b.link.to(END_PATH)) == 1


async def test_a_session_that_ends_while_the_disk_is_read_goes_first(tmp_path: Path) -> None:
    """The list of what waits may move during a read: nothing is lost, nothing given up."""
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))
    io = Scripted()

    b = old.restarted(io=io)

    def a_launch_is_refused_meanwhile() -> None:
        if io.calls == 2:  # the listing is done: this is the opening of the record
            b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")

    io.before = a_launch_is_refused_meanwhile
    await b.run(5)

    ends = [body["sessionId"] for body in b.link.to(END_PATH)]
    assert ends == ["k17pending", "cloud-old"]
    assert b.uplink.owed == 0


async def test_an_end_that_arrives_during_a_request_takes_the_place_and_the_record_keeps_its_turn(
    tmp_path: Path,
) -> None:
    old = bench(tmp_path)
    left = recording(tmp_path, old.clock, "ref-old")
    left.tick(2.0)
    left.close()
    tie(left.path, Cursor(session_id="cloud-old"))

    b = old.restarted()

    def refused_during_the_request(_body: object) -> Acked:
        del b.link.answers[TELEMETRY_PATH]
        b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")
        return stored(2)

    b.link.answer(TELEMETRY_PATH, refused_during_the_request)
    await b.run(5)

    ends = [body["sessionId"] for body in b.link.to(END_PATH)]
    assert ends == ["cloud-old", "k17pending"]
    assert cursor_of_record(left.path).state == "complete"
    assert b.uplink.owed == 0


async def test_ex4_a_record_of_an_earlier_boot_dated_by_a_clock_never_set_is_dated_at_the_latest(
    tmp_path: Path,
) -> None:
    """Its true date is lost. It ended before this console started: no later than that."""
    never_set = ManualClock(Monotonic(50.0), UnixMillis(0))
    left = recording(tmp_path, never_set, "ref-1970")
    left.tick(5.0)
    tie(left.path, Cursor(boot_id="an-earlier-boot"))

    b = bench(tmp_path)
    await b.run(3)

    declared = b.link.to(LOCAL_PATH)[0]
    # The date its clock wrote, in 1970, is still what its points are dated from...
    assert declared["startedAt"] == 50_000
    # ...and the dashboard is told an age: the time this console has been up,
    # the 4.8 s the record lasted, and a minute. Never "just now".
    assert declared["sessionAgeMs"] == 1000 + 4800 + 60_000
    assert b.link.to(END_PATH)[0]["endedAt"] == 50_000 + 4800


async def test_a_session_whose_cursor_never_reached_the_disk_is_sent_whole_when_its_turn_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sent again from its beginning: the dashboard stores once what it already has."""
    b = bench(tmp_path)

    def refuse(_record: Path, _cursor: Cursor) -> object:
        return Err(type("E", (), {"detail": "OSError:EROFS"})())

    monkeypatch.setattr("src.record_uplink.store", refuse)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-unwritten")
    b.disk.current = record.path
    record.tick(3.0)
    await b.step()
    assert b.link.seconds() == [0.0, 1.0, 2.0]
    record.close()
    b.uplink.finished(ENDED)
    # A refused launch takes the place: the record goes back to wait, by its name.
    b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")
    assert load(record.path) == NoCursor()
    monkeypatch.undo()

    await b.run(4)

    assert [body["localRef"] for body in b.link.to(LOCAL_PATH)] == ["ref-unwritten"] * 2
    assert b.link.seconds() == [0.0, 1.0, 2.0, 0.0, 1.0, 2.0]
    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["k17pending", "cloud-1"]
    assert cursor_of_record(record.path).state == "complete"


async def test_a_session_that_ends_before_the_link_saw_its_record_is_still_sent_from_it(
    tmp_path: Path,
) -> None:
    """A few seconds of session, on a link too slow to step meanwhile."""
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-short")
    b.disk.current = record.path
    record.tick(3.0)
    record.event()
    record.close("emergency_stop", stop=None)
    b.uplink.finished(RuntimeEnd(failed=True, reason="emergency_stop"))
    # The next session's record is already the one the console has open.
    b.disk.current = None

    await b.step()

    assert b.link.paths() == ["local", "telemetry", "events", "end"]
    assert b.link.to(LOCAL_PATH)[0]["localRef"] == "ref-short"
    assert b.link.seconds() == [0.0, 1.0, 2.0]
    assert cursor_of_record(record.path).state == "complete"


async def test_a_session_that_ends_unbound_with_an_unreadable_record_ends_as_the_runtime_said(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.uplink.begin(armed(b.clock))
    record = recording(tmp_path, b.clock, "ref-broken")
    b.disk.current = record.path
    (record.path / "manifest.json").write_bytes(b"{")
    b.uplink.finished(ENDED)

    await b.step()

    assert b.link.paths() == ["local", "end"]
    assert b.link.to(END_PATH)[0]["reason"] == "operator_stop: fini"
    assert load(record.path) == NoCursor()


@pytest.mark.parametrize("remote", [None, "k17remote"])
async def test_an_answer_of_an_unknown_kind_fails_loudly(
    tmp_path: Path, remote: str | None
) -> None:
    """To a declaration as to the confirmation of a launch."""
    b = bench(tmp_path, recording=False)
    b.link.answer(LOCAL_PATH if remote is None else START_PATH, cast("Acked", object()))
    b.uplink.begin(armed(b.clock, remote=remote))

    with pytest.raises(AssertionError):
        await b.step()


async def test_the_end_of_a_refused_launch_that_the_link_holds_is_made_again(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path)
    b.link.answer(END_PATH, DOWN)
    b.uplink.owe_refusal("k17pending", "refusee par la machine : console occupee")

    await b.step()
    assert b.refusal_owed(), "still owed: no other launch is asked for"
    del b.link.answers[END_PATH]
    await b.step(float(RETRY_PERIOD))

    assert [body["sessionId"] for body in b.link.to(END_PATH)] == ["k17pending"] * 2
    assert not b.refusal_owed()


def test_a_manifest_alone_describes_a_programme_without_its_name(tmp_path: Path) -> None:
    head = read_head(launched_programme(tmp_path))
    assert isinstance(head, Ok)

    declaration = declaration_from(head.value)

    assert (declaration.kind, declaration.operator, declaration.occupancy) == ("auto", "op-5", None)
    assert (declaration.profile_id, declaration.profile_name) == (None, None)
    assert (declaration.zone_low_bpm, declaration.zone_high_bpm) == (118, 138)
    assert (declaration.total_duration_s, declaration.subject_hr_max) == (600.0, 162)
