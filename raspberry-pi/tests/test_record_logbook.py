"""ANH-191 EX-3: what is asked at the console while no record is open goes to the logbook.

A session record closes when its phase reaches ``DONE`` and never changes
again. The acknowledgements, fault resets and refused starts that follow were
in no record; they are now lines of ``<records root>/logbook/events.jsonl``,
written by the journal thread like everything else, bounded in memory by the
journal's queue and on disk by a rotation.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Final, Literal

import pytest

import src.record.logbook as logbook_module
import src.record.writer as writer_module
from src.control_surface import EventKind as SurfaceEvent
from src.motor.drive import DriveFault
from src.record.codec import Privacy
from src.record.journal import FSYNC_PERIOD, Cause, Journal, Limits
from src.record.logbook import ACTIVE, DIRECTORY, PREVIOUS, Logbook, Note, note
from src.record.reader import read
from src.record.retention import RECORD_NAME, records
from src.record.schema import EventKind, RecordError
from src.record.session import SessionRequest, operator_alias
from src.result import Err, Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_console_support import RecordedRig, recorded_rig, set_target, start_bench, stop
from tests.record_journal_support import clock_at_start, count_fsync, session
from tests.test_failure_rig import OPERATOR, PROGRAMME_ENV, SHORT_PROFILE
from tests.test_record_session import bare_recorder, surface_event

WALL: Final[UnixMillis] = UnixMillis(1_791_195_072_345)
"""2026-10-05T10:11:12.345Z."""


def line(detail: str = "refused: demarrage refuse", at: float = 12.3456) -> Note:
    return note(
        wall_clock=WALL, at=Monotonic(at), kind=EventKind.REFUSAL, detail=detail, actor="op-1"
    )


def add(book: Logbook, entry: Note) -> None:
    """Add one line that the disk must take."""
    added = book.append(entry)
    assert isinstance(added, Ok), added


def lines_of(path: Path) -> list[dict[str, object]]:
    return [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]


# =========================================================================
# The two files
# =========================================================================


def test_ex3_a_line_says_when_on_both_clocks_what_and_who(tmp_path: Path) -> None:
    book = Logbook(tmp_path)
    assert book.directory == tmp_path / DIRECTORY
    assert not book.directory.exists(), "nothing is created before a first line"
    add(book, line())
    assert lines_of(book.directory / ACTIVE) == [
        {
            "at": "2026-10-05T10:11:12.345Z",
            "monotonic": 12.346,
            "kind": "refusal",
            "detail": "refused: demarrage refuse",
            "actor": "op-1",
        }
    ]
    add(book, line("refused: encore"))
    assert [entry["detail"] for entry in lines_of(book.directory / ACTIVE)] == [
        "refused: demarrage refuse",
        "refused: encore",
    ]


def test_ex3_the_directory_of_the_logbook_cannot_be_taken_for_a_record_or_a_marker() -> None:
    assert RECORD_NAME.fullmatch(DIRECTORY) is None
    assert not DIRECTORY.endswith(".json")
    assert "." not in DIRECTORY, "a marker is <record>.<something>.json: this is neither"


def test_ex3_the_logbook_is_bounded_on_disk_by_a_rotation(tmp_path: Path) -> None:
    """Two files at most, each under the bound, and the most recent lines always kept."""
    book = Logbook(tmp_path, limit=400)
    for index in range(40):
        add(book, line(f"refused: {index:04d}"))
    assert {path.name for path in book.directory.iterdir()} == {ACTIVE, PREVIOUS}
    active = lines_of(book.directory / ACTIVE)
    previous = lines_of(book.directory / PREVIOUS)
    assert (book.directory / ACTIVE).stat().st_size <= 400
    assert (book.directory / PREVIOUS).stat().st_size <= 400
    kept = [str(entry["detail"]) for entry in (*previous, *active)]
    assert kept[-1] == "refused: 0039", "the newest line is there"
    assert kept == [f"refused: {index:04d}" for index in range(40 - len(kept), 40)], (
        "consecutive, ending with the newest: only the oldest were let go"
    )


def test_ex3_a_line_longer_than_the_bound_still_goes_into_an_empty_file(tmp_path: Path) -> None:
    book = Logbook(tmp_path, limit=10)
    add(book, line("refused: a"))
    add(book, line("refused: b"))
    assert [e["detail"] for e in lines_of(book.directory / ACTIVE)] == ["refused: b"]
    assert [e["detail"] for e in lines_of(book.directory / PREVIOUS)] == ["refused: a"]


def test_ex3_an_actor_that_is_not_an_opaque_identifier_is_refused(tmp_path: Path) -> None:
    book = Logbook(tmp_path)
    named = note(
        wall_clock=WALL, at=Monotonic(1.0), kind=EventKind.REFUSAL, detail="x", actor="Dr Who"
    )
    refused = book.append(named)
    assert refused == Err(RecordError("event", "invalid_actor"))
    assert not book.directory.exists()


def test_ex3_an_address_never_reaches_the_logbook(tmp_path: Path) -> None:
    book = Logbook(tmp_path)
    add(book, line("refused: ecrire a jean.dupont@example.org"))
    assert b"example.org" not in (book.directory / ACTIVE).read_bytes()


def test_ex3_a_line_the_disk_refuses_half_way_is_taken_back_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = Logbook(tmp_path)
    add(book, line("refused: first"))
    real = writer_module.write_file
    full = [True]

    def torn(path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        if full[0]:
            real(path, content[:9], mode)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        real(path, content, mode)

    monkeypatch.setattr(logbook_module, "write_file", torn)
    refused = book.append(line("refused: torn"))
    assert refused == Err(RecordError("append", "OSError:ENOSPC"))
    full[0] = False
    add(book, line("refused: third"))
    assert [e["detail"] for e in lines_of(book.directory / ACTIVE)] == [
        "refused: first",
        "refused: third",
    ], "no half line in the middle of the file"


def test_ex3_a_logbook_whose_length_cannot_be_read_is_never_cut_on_a_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = Logbook(tmp_path)
    add(book, line("refused: kept"))
    before = (book.directory / ACTIVE).read_bytes()

    def unreadable(_path: Path) -> int:
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(logbook_module, "_size", unreadable)
    refused = book.append(line("refused: lost"))
    assert refused == Err(RecordError("append", "PermissionError:EACCES"))
    assert (book.directory / ACTIVE).read_bytes() == before


def test_ex3_a_torn_line_that_cannot_be_taken_back_is_left_and_nothing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = Logbook(tmp_path)
    add(book, line("refused: first"))
    real = writer_module.write_file

    def torn(path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        real(path, content[:9], mode)
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    def no_truncate(_path: Path, _length: int) -> None:
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(logbook_module, "write_file", torn)
    monkeypatch.setattr(os, "truncate", no_truncate)
    outcome = book.append(line("refused: torn"))
    assert outcome == Err(RecordError("append", "OSError:EIO"))


def test_ex3_a_refused_write_that_left_nothing_behind_truncates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = Logbook(tmp_path)
    add(book, line("refused: first"))
    cut: list[int] = []
    real = os.truncate

    def counted(path: Path, length: int) -> None:
        cut.append(length)
        real(path, length)

    def read_only(_path: Path, _content: bytes, _mode: Literal["ab", "xb"]) -> None:
        raise OSError(errno.EROFS, os.strerror(errno.EROFS))

    monkeypatch.setattr(logbook_module, "write_file", read_only)
    monkeypatch.setattr(os, "truncate", counted)
    outcome = book.append(line("refused: no"))
    assert outcome == Err(RecordError("append", "OSError:EROFS"))
    assert cut == []


def test_ex3_the_logbook_is_flushed_only_when_something_was_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsyncs = count_fsync(monkeypatch)
    book = Logbook(tmp_path)
    flushed = book.sync()
    assert isinstance(flushed, Ok), flushed
    assert fsyncs == [], "nothing was added: nothing is flushed"
    add(book, line())
    flushed = book.sync()
    assert isinstance(flushed, Ok), flushed
    assert len(fsyncs) == 2, "the file, and the directory that holds it"
    flushed = book.sync()
    assert isinstance(flushed, Ok), flushed
    assert len(fsyncs) == 2
    add(book, line())
    flushed = book.sync(directories=False)
    assert isinstance(flushed, Ok), flushed
    assert len(fsyncs) == 3, "where a directory cannot be flushed, the file alone"


def test_ex3_a_flush_the_disk_refuses_stays_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = Logbook(tmp_path)
    add(book, line())
    real = os.fsync
    refusing = [True]

    def fsync(descriptor: int) -> None:
        if refusing[0]:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    refused = book.sync()
    assert refused == Err(RecordError("sync", "OSError:EIO"))
    refusing[0] = False
    calls = count_fsync(monkeypatch)
    flushed = book.sync()
    assert isinstance(flushed, Ok), flushed
    assert calls, "it was still owed"


# =========================================================================
# Through the journal: bounded in memory, written off the loop
# =========================================================================


def test_ex3_a_line_is_queued_session_or_not_and_written_by_the_journal(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    queued = journal.log(line("refused: idle"))
    assert queued is True
    assert not (journal.root / DIRECTORY).exists(), "queued: the producer wrote nothing"
    journal.drain()
    journal.open(session(), Privacy())
    queued = journal.log(line("refused: during"))
    assert queued is True
    journal.drain()
    assert [e["detail"] for e in lines_of(journal.root / DIRECTORY / ACTIVE)] == [
        "refused: idle",
        "refused: during",
    ]
    status = journal.status(clock.monotonic())
    assert (status.degraded, status.pending) == (False, 0)


def test_ex3_a_line_refused_for_lack_of_room_is_counted_and_the_logbook_says_so(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock, limits=Limits(entries=2))
    queued = journal.log(line("refused: 1"))
    assert queued is True
    queued = journal.log(line("refused: 2"))
    assert queued is True
    queued = journal.log(line("refused: 3"))
    assert queued is False, "the queue is bounded: this one is refused"
    queued = journal.log(line("refused: 4"))
    assert queued is False
    status = journal.status(clock.monotonic())
    assert (status.dropped, status.cause) == (2, Cause.QUEUE_FULL)
    journal.drain()
    queued = journal.log(line("refused: 5"))
    assert queued is True
    journal.drain()
    written = lines_of(journal.root / DIRECTORY / ACTIVE)
    # Said before the next line the logbook takes, whichever that is.
    assert [(e["kind"], e["detail"]) for e in written] == [
        ("warning", "logbook: dropped=2"),
        ("refusal", "refused: 1"),
        ("refusal", "refused: 2"),
        ("refusal", "refused: 5"),
    ]
    assert written[0]["actor"] == "system"
    # Said once per change, and the count starts again with the next session.
    queued = journal.log(line("refused: 6"))
    assert queued is True
    journal.drain()
    assert len(lines_of(journal.root / DIRECTORY / ACTIVE)) == 5
    journal.open(session(), Privacy())
    assert journal.status(clock.monotonic()).dropped == 0


def test_ex3_a_missed_line_is_announced_again_until_the_logbook_takes_the_announcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock, limits=Limits(entries=1))
    queued = journal.log(line("refused: 1"))
    assert queued is True
    queued = journal.log(line("refused: 2"))
    assert queued is False
    real = writer_module.write_file
    refusing = [True]

    def write(path: Path, content: bytes, mode: Literal["ab", "xb"]) -> None:
        if refusing[0]:
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        real(path, content, mode)

    monkeypatch.setattr(logbook_module, "write_file", write)
    journal.drain()
    status = journal.status(clock.monotonic())
    assert status.failures == 1, "the line the disk refused"
    refusing[0] = False
    queued = journal.log(line("refused: 3"))
    assert queued is True
    journal.drain()
    written = lines_of(journal.root / DIRECTORY / ACTIVE)
    assert [e["detail"] for e in written] == ["logbook: dropped=1", "refused: 3"]


def test_ex3_the_logbook_is_flushed_every_two_seconds_and_never_more_often(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path / "records", clock)
    journal.drain()
    fsyncs = count_fsync(monkeypatch)
    for index in range(22):
        queued = journal.log(line(f"refused: {index}"))
        assert queued is True
        clock.advance(Seconds(0.2))
        journal.drain()
    # 4.4 s of lines, one every 0.2 s: two flushes, each of the file and of its directory.
    assert FSYNC_PERIOD == 2.0
    assert len(fsyncs) == 2 * 2


# =========================================================================
# The recorder: where a console event goes
# =========================================================================

ALIAS: Final[str] = operator_alias(OPERATOR)


def test_ex3_outside_a_session_a_console_event_is_a_line_of_the_logbook(tmp_path: Path) -> None:
    recorder, journal, _clock = bare_recorder(tmp_path)
    for kind, operator, detail in (
        (SurfaceEvent.START_REQUESTED, OPERATOR, "start secret_programme"),
        (SurfaceEvent.REFUSED, OPERATOR, f"demarrage refuse par {OPERATOR}"),
        (SurfaceEvent.ACKNOWLEDGED, OPERATOR, "operator_estop"),
        (SurfaceEvent.FAULT_RESET_REQUESTED, OPERATOR, "reset defaut"),
        (SurfaceEvent.END_REQUESTED, "Dr Remote (tableau de bord)", "arret"),
        (SurfaceEvent.DASHBOARD, "", "serveur incompatible (contrat 1.0 vs 2.0)"),
        (SurfaceEvent.SESSION_IDLE, "", "session idle"),
        (SurfaceEvent.RECORDING, "", "enregistrement retabli"),
    ):
        recorder.note_event(surface_event(kind, operator, detail, 10.5))
    journal.drain()
    assert records(journal.root) == (), "no session: no record"
    written = lines_of(journal.root / DIRECTORY / ACTIVE)
    assert [(e["kind"], e["detail"], e["actor"]) for e in written] == [
        # A start request names a programme: that it was asked is all that is kept.
        ("operator_action", "start_requested", ALIAS),
        ("refusal", "refused: demarrage refuse par [redacted]", ALIAS),
        ("verdict_ack", "acknowledged: operator_estop", ALIAS),
        ("operator_action", "fault_reset_requested: reset defaut", ALIAS),
        ("remote_command", "end_requested: arret", "remote"),
        ("warning", "dashboard: serveur incompatible (contrat 1.0 vs 2.0)", "system"),
    ]
    assert {e["at"] for e in written} == {"2023-11-14T22:13:20.000Z"}
    assert {e["monotonic"] for e in written} == {10.5}
    assert not recorder.degraded


def test_ex3_a_word_withheld_stays_out_of_the_logbook_like_a_name(tmp_path: Path) -> None:
    recorder, journal, _clock = bare_recorder(tmp_path)
    recorder.withhold("secret_programme", "  ")
    recorder.withhold("")
    recorder.note_event(
        surface_event(
            SurfaceEvent.REFUSED,
            OPERATOR,
            "programme 'secret_programme' inconnu sur cette machine",
            1,
        )
    )
    journal.drain()
    written = lines_of(journal.root / DIRECTORY / ACTIVE)
    assert [e["detail"] for e in written] == [
        "refused: programme '[redacted]' inconnu sur cette machine"
    ]


def test_ex3_between_sessions_the_most_recent_names_are_kept_and_the_text_is_still_written(
    tmp_path: Path,
) -> None:
    """A console stays idle for weeks: the names it remembers are bounded, its logbook goes on."""
    recorder, journal, _clock = bare_recorder(tmp_path)
    for index in range(50):
        recorder.note_event(
            surface_event(SurfaceEvent.REFUSED, f"operateur {index}", f"refus {index}", 1.0)
        )
    recorder.note_event(
        surface_event(
            SurfaceEvent.REFUSED, "operateur 49", "dit par operateur 49 puis operateur 48", 2
        )
    )
    journal.drain()
    written = lines_of(journal.root / DIRECTORY / ACTIVE)
    assert written[-1]["detail"] == "refused: dit par [redacted] puis [redacted]"
    assert written[10]["detail"] == "refused: refus 10", "never withheld for having seen too many"


def test_ex3_a_name_withheld_during_a_session_follows_the_sessions_own_bound(
    tmp_path: Path,
) -> None:
    recorder, journal, _clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    recorder.withhold("secret_programme")
    recorder.withhold("secret_programme")
    recorder.note_event(
        surface_event(SurfaceEvent.REFUSED, OPERATOR, "cible refusee pour secret_programme", 11.0)
    )
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    assert loaded.value.events[-1].detail == "refused: cible refusee pour [redacted]"
    assert not (journal.root / DIRECTORY).exists(), "during a session, the session's record"


# =========================================================================
# The REAL console: after the record is closed
# =========================================================================


def logbook_of(recorded: RecordedRig) -> list[dict[str, object]]:
    return lines_of(recorded.root / DIRECTORY / ACTIVE)


async def test_ex3_acceptance_what_is_asked_after_the_record_closed_is_in_the_logbook(
    tmp_path: Path,
) -> None:
    """A session ends on a drive fault and its record closes. Then the operator acts."""
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await set_target(http, 5.0)
        await recorded.tick(15.0)
        recorded.rig.simulator.inject_fault(DriveFault.OVERCURRENT)
        await recorded.tick(180.0)
        assert recorded.state() is RuntimeState.ENDING
        record = recorded.only_record()
        assert (record / "checksums.sha256").is_file(), "the phase reached DONE: closed"
        closed = recorded.everything_under(record)
        before = len(logbook_of(recorded))

        # A start while the fault verdict stands: refused.
        refused = await http.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert refused.status_code == 409
        # The acknowledgement of the verdict, then the reset of the drive fault.
        acknowledged = await http.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        reset = await http.post("/api/drive/fault-reset", json={"operator": OPERATOR})
        assert reset.status_code == 202, reset.text
        await recorded.tick(2.0)
        # And a start the machine refuses for a reason only the loop knows.
        again = await http.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        await recorded.tick(1.0)

    after = logbook_of(recorded)[before:]
    kinds = [(entry["kind"], str(entry["detail"]).split(":")[0]) for entry in after]
    assert ("verdict_ack", "acknowledged") in kinds
    assert ("operator_action", "fault_reset_requested") in kinds
    assert all(entry["actor"] in (ALIAS, "system") for entry in after)
    assert again.status_code in (202, 409)
    # The closed record did not change by one byte, and still reads without a warning.
    assert recorded.everything_under(record) == closed
    assert recorded.recording().warnings == ()
    assert recorded.records() == [record], "the logbook is not a record"
    assert OPERATOR.encode() not in b"".join(
        path.read_bytes() for path in (recorded.root / DIRECTORY).iterdir()
    )


async def test_ex3_a_start_the_machine_refuses_is_in_the_logbook_with_its_reason(
    tmp_path: Path,
) -> None:
    """Nothing was ever armed: before this, such a refusal was written nowhere."""
    recorded = recorded_rig(tmp_path, env=PROGRAMME_ENV)
    async with recorded.rig.http() as http:
        attested = await http.post(
            "/api/safety/attest",
            json={"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
        )
        assert attested.status_code == 200
        started = await http.post(
            "/api/session/start",
            json={
                "profile_id": SHORT_PROFILE,
                "operator": OPERATOR,
                "total_duration_s": None,
                "subject_age": 9,
            },
        )
        assert started.status_code == 202, started.text
        await recorded.tick(1.0)
    assert recorded.records() == []
    refusals = recorded.rig.refusals()
    assert len(refusals) == 1
    # The operator is shown the age, as before.
    assert refusals[0].startswith("demarrage refuse : passager de 9 ans, minimum ")
    written = [(e["kind"], e["detail"], e["actor"]) for e in logbook_of(recorded)]
    # The logbook keeps the reason and not the rider's age.
    kept = refusals[0].replace("passager de 9 ans", "[redacted]")
    assert kept.startswith("demarrage refuse : [redacted], minimum ")
    assert written[1:] == [
        ("operator_action", "start_requested", ALIAS),
        ("refusal", f"refused: {kept}", ALIAS),
    ]
    assert b"9 ans" not in recorded.everything_on_disk()
    assert written[0][0] == "operator_action"
    assert str(written[0][1]).startswith("attested: ")
    on_disk = recorded.everything_on_disk()
    assert SHORT_PROFILE.encode() not in on_disk, "the programme's identifier is not written"
    assert OPERATOR.encode() not in on_disk


async def test_ex3_a_refused_start_never_writes_the_identifier_of_the_programme_it_asked_for(
    tmp_path: Path,
) -> None:
    """A launch for a programme this machine does not hold: the refusal quotes its identifier.

    The operator is shown it. The logbook is not given it: neither in the line
    of the request, nor in the line of the refusal.
    """
    programme = "confidential_programme_7"
    recorded = recorded_rig(tmp_path, env=PROGRAMME_ENV)
    surface = recorded.rig.panel.surface
    attested = surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attested, Ok)
    asked = surface.submit_start(
        profile_id=programme, operator=OPERATOR, total_duration_s=None, subject_age=30
    )
    assert isinstance(asked, Ok), asked
    await recorded.tick(1.0)

    assert recorded.rig.refusals() == [
        f"demarrage refuse : programme '{programme}' inconnu sur cette machine"
    ]
    written = [(e["kind"], e["detail"]) for e in logbook_of(recorded)]
    assert written[1:] == [
        ("operator_action", "start_requested"),
        ("refusal", "refused: demarrage refuse : programme '[redacted]' inconnu sur cette machine"),
    ]
    assert programme.encode() not in recorded.everything_on_disk()
    assert recorded.records() == []


async def test_ex3_the_same_event_during_a_session_goes_to_the_sessions_record_only(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        before = len(logbook_of(recorded))
        too_slow = await http.post(
            "/api/manual/target", json={"output_rpm": 0.5, "operator": OPERATOR}
        )
        assert too_slow.status_code == 202
        await recorded.tick(1.0)
        assert len(logbook_of(recorded)) == before, "a session is recorded: nothing new here"
        await stop(recorded, http)
    refusals = [e for e in recorded.recording().events if e.kind is EventKind.REFUSAL]
    assert len(refusals) == 1
