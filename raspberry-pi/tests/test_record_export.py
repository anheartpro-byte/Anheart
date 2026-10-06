"""ANH-128 EX-8: one session record as a ``.tar.gz``, behind the console's token.

The archive builder first (``src/record/export.py``), then the two routes on
the real console: the list, and the download the page's « Exporter
l'enregistrement » button asks for.
"""

from __future__ import annotations

import errno
import io
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Final

import pytest

import src.record.export as export_module
from src.clock import ManualClock
from src.record.export import (
    EXPORT_PREFIX,
    STALE_AFTER_MS,
    UNKNOWN_RECORD,
    RecordEntry,
    RecordExporter,
    build_archive,
    discard,
    listing,
    locate,
)
from src.record.reader import read
from src.record.schema import RecordError
from src.result import Err, Ok, Result
from src.units import Monotonic, UnixMillis
from tests.record_console_support import recorded_rig, set_target, start_bench, stop
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
    assert await exporter.listing() == (RecordEntry(record.name, closed=True),)
    built = await exporter.archive(record.name)
    assert isinstance(built, Ok)
    assert tarfile.is_tarfile(built.value)


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
    assert failed.json()["detail"] == "archive impossible (OSError:ENOSPC)"


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
