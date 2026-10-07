"""ANH-128 EX-8: one session record as a ``.tar.gz``, behind the console's token.

The archive builder first (``src/record/export.py``), then the two routes on
the real console: the list, and the download the page's « Exporter
l'enregistrement » button asks for.
"""

from __future__ import annotations

import asyncio
import errno
import io
import os
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Final

import pytest

import src.record.export as export_module
from src.clock import ManualClock
from src.record.export import (
    BUSY,
    EXPORT_PREFIX,
    STALE_AFTER_MS,
    TIMEOUT,
    UNKNOWN_RECORD,
    RecordEntry,
    RecordExporter,
    RecordIo,
    build_archive,
    discard,
    listing,
    locate,
)
from src.record.reader import read
from src.record.schema import RecordError
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_console_support import recorded_rig, set_target, start_bench, stop
from tests.record_journal_support import wait_for
from tests.test_failure_rig import BENCH_ENV, make_rig
from tests.test_record_retention import closed_record, open_record

NOW: Final[UnixMillis] = UnixMillis(1_791_195_072_000)
TOKEN: Final[str] = "a-sixteen-char-token"  # noqa: S105 - a test value


def exports(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir() if path.name.startswith(EXPORT_PREFIX))


# =========================================================================
# The list, and what may be named
# =========================================================================


def test_ex8_the_records_are_listed_newest_first_with_whether_they_were_closed(
    tmp_path: Path,
) -> None:
    older = closed_record(tmp_path, 1)
    newer = closed_record(tmp_path, 2)
    interrupted = open_record(tmp_path)
    (tmp_path / "notes.txt").write_text("not a record")
    # By name, which starts with the UTC start: the open one began on the 5th.
    assert listing(tmp_path) == (
        RecordEntry(interrupted.name, closed=False),
        RecordEntry(newer.name, closed=True),
        RecordEntry(older.name, closed=True),
    )
    assert listing(tmp_path / "missing") == ()


def test_ex8_only_a_record_directly_under_the_root_can_be_named(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    record = closed_record(root)
    outside = closed_record(tmp_path / "outside")
    (root / "2026-10-08T101112Z_linked").symlink_to(outside, target_is_directory=True)
    (root / "2026-10-09T101112Z_a-file").write_text("a file")
    assert locate(root, record.name) == record
    for name in (
        "",
        "..",
        f"../outside/{outside.name}",
        f"{record.name}/ecg_raw",
        "2026-10-08T101112Z_linked",
        "2026-10-09T101112Z_a-file",
        "2026-10-07T101112Z_never-existed",
        ".export-abc.tar.gz",
    ):
        assert locate(root, name) is None, name
        refused = build_archive(root, name, NOW)
        assert isinstance(refused, Err)
        assert refused.error == RecordError("read", UNKNOWN_RECORD)
    assert exports(root) == []


# =========================================================================
# The archive
# =========================================================================


def test_ex8_the_archive_holds_the_whole_record_and_reads_back_identically(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path)
    built = build_archive(tmp_path, record.name, NOW)
    assert isinstance(built, Ok)
    archive_path = built.value
    assert archive_path.parent == tmp_path
    assert archive_path.name.startswith(EXPORT_PREFIX)
    assert archive_path.name.endswith(".tar.gz")

    unpacked = tmp_path / "unpacked"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        archive.extractall(unpacked, filter="data")
    names = sorted(member.name for member in members)
    assert names == sorted(
        [record.name]
        + [f"{record.name}/{path.relative_to(record).as_posix()}" for path in record.rglob("*")]
    )
    # Nothing about the account the console runs under.
    assert {(m.uid, m.gid, m.uname, m.gname) for m in members} == {(0, 0, "", "")}
    original = read(record)
    copy = read(unpacked / record.name)
    assert isinstance(original, Ok)
    assert isinstance(copy, Ok)
    assert copy.value == original.value
    assert copy.value.warnings == ()

    discard(archive_path)
    discard(archive_path)  # already gone: nothing to do, nothing raised
    assert exports(tmp_path) == []


def test_ex8_an_archive_abandoned_by_its_download_is_removed_by_a_later_export(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path)
    abandoned = tmp_path / f"{EXPORT_PREFIX}abandoned.tar.gz"
    recent = tmp_path / f"{EXPORT_PREFIX}recent.tar.gz"
    dangling = tmp_path / f"{EXPORT_PREFIX}dangling.tar.gz"
    abandoned.write_bytes(b"left behind")
    recent.write_bytes(b"still being sent")
    dangling.symlink_to(tmp_path / "nowhere")
    old = (NOW - STALE_AFTER_MS - 1_000) / 1000
    fresh = (NOW - 5_000) / 1000
    os.utime(abandoned, (old, old))
    os.utime(recent, (fresh, fresh))

    built = build_archive(tmp_path, record.name, NOW)
    assert isinstance(built, Ok)
    assert not abandoned.exists()
    assert recent.exists()
    assert dangling.is_symlink(), "what cannot even be measured is left alone"
    assert len(exports(tmp_path)) == 3


def test_ex8_a_disk_that_refuses_the_archive_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = closed_record(tmp_path)

    def full(self: tarfile.TarFile, name: Path, arcname: str, *, filter: object) -> None:  # noqa: A002 - tarfile's own keyword
        raise OSError(errno.ENOSPC, f"no space for {arcname} from {name} ({self.mode}, {filter})")

    monkeypatch.setattr(tarfile.TarFile, "add", full)
    refused = build_archive(tmp_path, record.name, NOW)
    assert isinstance(refused, Err)
    assert refused.error == RecordError("read", "OSError:ENOSPC")
    assert exports(tmp_path) == []


def test_ex8_a_directory_that_refuses_the_temporary_file_is_an_error_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = closed_record(tmp_path)

    def refused(**_kwargs: object) -> io.BytesIO:
        raise PermissionError(errno.EACCES, "read-only records directory")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refused)
    built = build_archive(tmp_path, record.name, NOW)
    assert isinstance(built, Err)
    assert built.error == RecordError("read", "PermissionError:EACCES")


def test_ex8_discarding_an_archive_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stuck(_self: Path, missing_ok: bool = False) -> None:
        raise OSError(errno.EBUSY, f"busy (missing_ok={missing_ok})")

    monkeypatch.setattr(Path, "unlink", stuck)
    discard(tmp_path / "anything")


async def test_ex8_the_exporter_works_off_the_event_loop(tmp_path: Path) -> None:
    record = closed_record(tmp_path)
    exporter = RecordExporter(tmp_path, ManualClock(Monotonic(0.0), NOW))
    listed = await exporter.listing()
    assert isinstance(listed, Ok)
    assert listed.value == (RecordEntry(record.name, closed=True),)
    built = await exporter.archive(record.name)
    assert isinstance(built, Ok)
    assert tarfile.is_tarfile(built.value)
    assert exporter.io.active == 0


# =========================================================================
# Record reads have threads of their own, a few, and never a queue
# =========================================================================


class Disk:
    """A disk that answers only when told to. Every call is counted with its thread."""

    def __init__(self) -> None:
        self.answer: threading.Event = threading.Event()
        self.threads: list[str] = []
        self.late: list[int] = []

    def read(self) -> int:
        self.threads.append(threading.current_thread().name)
        if not self.answer.wait(30.0):
            raise AssertionError("the test never let the disk answer")
        return len(self.threads)

    def came_back(self) -> bool:
        return not any(thread.name == "record-io" for thread in threading.enumerate())


async def test_ex3_a_record_read_runs_on_a_record_thread_never_on_the_loop_s_pool() -> None:
    disk = Disk()
    disk.answer.set()
    io = RecordIo()
    assert await io.run(disk.read, Seconds(5.0)) == Ok(1)
    assert disk.threads == ["record-io"]
    assert io.active == 0


async def test_ex3_past_its_few_threads_a_record_read_is_refused_at_once_never_queued() -> None:
    disk = Disk()
    io = RecordIo(limit=2)
    first = asyncio.create_task(io.run(disk.read, Seconds(30.0)))
    second = asyncio.create_task(io.run(disk.read, Seconds(30.0)))
    await asyncio.sleep(0)
    assert io.active == 2
    for _ in range(50):
        assert await io.run(disk.read, Seconds(30.0)) == Err(RecordError("read", BUSY))
    # Both threads are on the disk, however late a loaded machine starts them.
    wait_for(lambda: len(disk.threads) >= 2)
    await asyncio.sleep(0.05)
    assert disk.threads == ["record-io", "record-io"], "a refused read never reached a thread"
    assert not first.done()

    disk.answer.set()
    assert {(await first), (await second)} == {Ok(2)}
    assert io.active == 0
    assert await io.run(disk.read, Seconds(5.0)) == Ok(3)


async def test_ex3_a_caller_gives_up_on_a_silent_disk_and_the_slot_stays_taken() -> None:
    disk = Disk()
    io = RecordIo(limit=1)
    assert await io.run(disk.read, Seconds(0.05), disk.late.append) == Err(
        RecordError("read", TIMEOUT)
    )
    assert io.active == 1, "the thread is still on the disk"
    assert await io.run(disk.read, Seconds(0.05)) == Err(RecordError("read", BUSY))

    disk.answer.set()
    wait_for(disk.came_back)
    await asyncio.sleep(0)
    assert io.active == 0
    assert disk.late == [1], "what came back after its caller left was handed to the cleanup"


async def test_ex3_a_result_that_arrives_in_time_is_not_treated_as_late() -> None:
    disk = Disk()
    disk.answer.set()
    io = RecordIo()
    assert await io.run(disk.read, Seconds(5.0), disk.late.append) == Ok(1)
    assert disk.late == []


async def test_ex3_a_bug_in_a_record_read_is_an_answer_and_frees_its_slot() -> None:
    def broken() -> int:
        raise RuntimeError("injected: a bug in a record read")

    io = RecordIo(limit=1)
    assert await io.run(broken, Seconds(5.0)) == Err(RecordError("read", "RuntimeError"))
    assert io.active == 0


async def test_ex3_a_cleanup_that_fails_still_hands_its_slot_back() -> None:
    def broken(_value: int) -> None:
        raise RuntimeError("injected: a bug in the cleanup of a late result")

    disk = Disk()
    io = RecordIo(limit=1)
    assert await io.run(disk.read, Seconds(0.05), broken) == Err(RecordError("read", TIMEOUT))
    disk.answer.set()
    wait_for(disk.came_back)
    await asyncio.sleep(0)
    assert io.active == 0
    assert await io.run(disk.read, Seconds(5.0)) == Ok(2)


def test_ex3_a_record_thread_that_outlives_the_console_ends_without_a_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop is closed before the disk answers: nobody is left to tell, and nothing is raised."""
    disk = Disk()
    unraised: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", unraised.append)

    async def console() -> Result[int, RecordError]:
        return await RecordIo().run(disk.read, Seconds(0.05))

    assert asyncio.run(console()) == Err(RecordError("read", TIMEOUT))
    disk.answer.set()
    wait_for(disk.came_back)
    assert unraised == []


async def test_ex8_an_archive_finished_after_its_request_gave_up_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = closed_record(tmp_path)
    proceed = threading.Event()
    real = export_module.build_archive

    def slow(root: Path, name: str, now: UnixMillis) -> Result[Path, RecordError]:
        proceed.wait(30.0)
        return real(root, name, now)

    monkeypatch.setattr(export_module, "build_archive", slow)
    monkeypatch.setattr(export_module, "ARCHIVE_TIMEOUT", Seconds(0.05))
    exporter = RecordExporter(tmp_path, ManualClock(Monotonic(0.0), NOW))
    assert await exporter.archive(record.name) == Err(RecordError("read", TIMEOUT))
    # A second one, for a record that does not exist: late too, with nothing to remove.
    unknown = await exporter.archive("2026-01-01T000000Z_missing")
    assert unknown == Err(RecordError("read", TIMEOUT))
    assert exporter.io.active == 2
    proceed.set()
    wait_for(lambda: not any(t.name == "record-io" for t in threading.enumerate()))
    assert exports(tmp_path) == [], "nobody will send it: it does not stay on the disk"


# =========================================================================
# The routes, on the real console
# =========================================================================


async def test_ex8_the_console_lists_its_records_and_exports_the_latest_at_rest(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        empty = await session.get("/api/records")
        assert empty.json() == {"recording": True, "records": []}

        await start_bench(recorded, session)
        await set_target(session, 5.0)
        await recorded.tick(5.0)
        name = recorded.only_record().name
        during = await session.get(f"/api/records/{name}/archive")
        assert during.status_code == 409
        assert during.json()["detail"] == (
            "export refuse pendant une seance : attendre le retour au repos"
        )
        # The list reads the disk too: at rest only, like the archive.
        listing_during = await session.get("/api/records")
        assert listing_during.status_code == 409
        assert listing_during.json()["detail"] == (
            "liste des enregistrements refusee pendant une seance : attendre le retour au repos"
        )
        exporter = recorded.rig.panel.services.records
        assert exporter is not None
        assert exporter.io.active == 0, "a refused request reads nothing"

        await stop(recorded, session, 30.0)
        listed = await session.get("/api/records")
        assert listed.json() == {"recording": True, "records": [{"name": name, "closed": True}]}

        exported = await session.get(f"/api/records/{name}/archive")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/gzip"
        assert exported.headers["content-disposition"] == f'attachment; filename="{name}.tar.gz"'
        with tarfile.open(fileobj=io.BytesIO(exported.content), mode="r:gz") as archive:
            names = archive.getnames()
        assert f"{name}/manifest.json" in names
        assert f"{name}/ticks.csv" in names
        assert f"{name}/checksums.sha256" in names
        assert exports(recorded.root) == [], "the temporary archive was removed once sent"

        for unknown in ("2026-01-01T000000Z_nothing", "not-a-record", ".export-x.tar.gz"):
            missing = await session.get(f"/api/records/{unknown}/archive")
            assert missing.status_code == 404
            assert missing.json()["detail"] == "enregistrement inconnu sur cette machine"
        # A path is not a name: it does not even reach the handler.
        escaping = await session.get("/api/records/..%2F..%2Fetc/archive")
        assert escaping.status_code == 404


async def test_ex8_a_failed_archive_is_a_500_with_its_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = recorded_rig(tmp_path)

    def failing(_root: Path, _name: str, _now: UnixMillis) -> Result[Path, RecordError]:
        return Err(RecordError("read", "OSError:ENOSPC"))

    monkeypatch.setattr(export_module, "build_archive", failing)
    async with recorded.rig.http() as session:
        failed = await session.get("/api/records/2026-10-05T101112Z_local-1/archive")
    assert failed.status_code == 500
    assert failed.json()["detail"] == "lecture des enregistrements impossible (OSError:ENOSPC)"


async def test_ex3_a_records_disk_that_never_answers_costs_two_threads_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """However many requests arrive, two wait and the others are told at once."""
    recorded = recorded_rig(tmp_path)
    answer = threading.Event()

    def never(_root: Path) -> tuple[RecordEntry, ...]:
        answer.wait(30.0)
        return ()

    monkeypatch.setattr(export_module, "listing", never)
    monkeypatch.setattr(export_module, "LISTING_TIMEOUT", Seconds(0.3))
    exporter = recorded.rig.panel.services.records
    assert exporter is not None
    async with recorded.rig.http() as session:
        answers = await asyncio.gather(*(session.get("/api/records") for _ in range(40)))
        statuses = sorted(answer_.status_code for answer_ in answers)
        assert statuses == [503] * 38 + [504] * 2
        busy = next(a for a in answers if a.status_code == 503)
        assert busy.json()["detail"] == (
            "lecture des enregistrements deja en cours : reessayer dans un instant"
        )
        slow = next(a for a in answers if a.status_code == 504)
        assert slow.json()["detail"] == "le disque des enregistrements ne repond pas"
        assert exporter.io.active == 2, "the two threads are still on the disk"
        assert len([t for t in threading.enumerate() if t.name == "record-io"]) == 2
        again = await session.get("/api/records")
        assert again.status_code == 503

        answer.set()
        for _ in range(500):  # the two threads hand their slots back through the loop
            if exporter.io.active == 0:
                break
            await asyncio.sleep(0.01)
        monkeypatch.undo()
        back = await session.get("/api/records")
        assert back.json() == {"recording": True, "records": []}


async def test_ex8_a_console_that_records_nothing_says_so(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        listed = await session.get("/api/records")
        assert listed.json() == {"recording": False, "records": []}
        refused = await session.get("/api/records/2026-10-05T101112Z_local-1/archive")
        assert refused.status_code == 404
        assert refused.json()["detail"] == "cette console n'enregistre pas"


async def test_ex8_both_routes_are_behind_the_token(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path, env={**BENCH_ENV, "UI_TOKEN": TOKEN})
    record = closed_record(recorded.root)
    async with recorded.rig.http() as session:
        for path in ("/api/records", f"/api/records/{record.name}/archive"):
            refused = await session.get(path)
            assert refused.status_code == 401, path
            wrong = await session.get(path, headers={"X-Anheart-Token": "not-the-token-at-all"})
            assert wrong.status_code == 401, path
            allowed = await session.get(path, headers={"X-Anheart-Token": TOKEN})
            assert allowed.status_code == 200, path
