"""ANH-191 EX-4: a record is created private, every directory and every file of it.

Before, only the records directory was (mode 700): a session's directory was
755 and its files 644, protected by their parent alone. Now each directory is
created 700 and each file 600, by the system call that creates it, including
what is added to a record or next to it later (the final manifest, the
checksums, a deposit marker, the logbook, an export's temporary archive).

Every case runs with the process's umask at 0, so that a mode seen here is the
one the code asked for and not one the umask happened to give.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record.codec import Privacy
from src.record.export import build_archive
from src.record.journal import Journal
from src.record.logbook import DIRECTORY
from src.record.retention import confirm_deposit, marker_of
from src.record.writer import PRIVATE_DIRECTORY, PRIVATE_FILE, create_private, write_file
from src.result import Ok
from src.units import Monotonic, UnixMillis
from tests.record_console_support import recorded_rig, start_bench, stop
from tests.record_journal_support import ENDED, batch, clock_at_start, closing, opened, session
from tests.record_support import row, writer


@pytest.fixture(autouse=True)
def no_umask() -> Generator[None]:
    """Nothing is narrowed by the umask: what is private here was asked to be."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def modes(root: Path) -> dict[str, int]:
    """The permission bits of ``root`` and of everything under it, by relative name."""
    found = {".": stat.S_IMODE(root.stat().st_mode)}
    for path in root.rglob("*"):
        found[path.relative_to(root).as_posix()] = stat.S_IMODE(path.stat().st_mode)
    return found


def wider_than_private(root: Path) -> dict[str, str]:
    """Whatever under ``root`` is not exactly 700 (directory) or 600 (file)."""
    wrong: dict[str, str] = {}
    for name, mode in modes(root).items():
        path = root / name
        expected = PRIVATE_DIRECTORY if path.is_dir() else PRIVATE_FILE
        if mode != expected:
            wrong[name] = oct(mode)
    return wrong


def test_ex4_a_file_is_created_private_and_an_existing_one_keeps_its_mode(tmp_path: Path) -> None:
    created = tmp_path / "created"
    create_private(created, b"x")
    assert stat.S_IMODE(created.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        create_private(created, b"y")
    block = tmp_path / "block"
    write_file(block, b"x", "xb")
    stream = tmp_path / "stream"
    write_file(stream, b"a", "ab")
    write_file(stream, b"b", "ab")
    assert stream.read_bytes() == b"ab", "an append is still an append"
    assert stat.S_IMODE(block.stat().st_mode) == 0o600
    assert stat.S_IMODE(stream.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_file(block, b"y", "xb")


def test_ex4_the_shared_writer_creates_a_private_record_open_then_closed(tmp_path: Path) -> None:
    """The writer the simulation uses too: nothing of a record is ever group or world readable."""
    recording = writer(tmp_path)
    ticked = recording.tick(row())
    assert isinstance(ticked, Ok)
    assert wider_than_private(recording.path) == {}, "from its creation"
    closed = recording.close(ManualClock(epoch_millis=ENDED), "operator_stop")
    assert isinstance(closed, Ok)
    found = modes(recording.path)
    assert found["."] == 0o700
    assert found["ecg_raw"] == 0o700
    assert found["manifest.json"] == 0o600, "replaced at the close, and still private"
    assert found["checksums.sha256"] == 0o600
    assert wider_than_private(recording.path) == {}


def test_ex4_the_journal_writes_private_blocks_and_a_private_marker_sits_next_to_the_record(
    tmp_path: Path,
) -> None:
    clock = clock_at_start()
    journal = opened(tmp_path, clock)
    for seq in range(3):
        queued = journal.submit(batch(seq))
        assert queued is True
    journal.close(closing())
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    marked = confirm_deposit(path, "storage-1", UnixMillis(1_791_195_072_000))
    assert isinstance(marked, Ok)
    assert stat.S_IMODE(marker_of(path).stat().st_mode) == 0o600
    assert wider_than_private(journal.root) == {}


async def test_ex4_everything_a_console_session_leaves_under_the_records_directory_is_private(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as http:
        await start_bench(recorded, http)
        await recorded.tick(5.0)
        await stop(recorded, http)
    await recorded.rig.panel.close()
    recorded.drain()
    root = recorded.root
    found = modes(root)
    record = recorded.only_record().name
    assert found["."] == 0o700
    assert found[record] == 0o700
    assert found[f"{record}/ecg_raw"] == 0o700
    assert found[DIRECTORY] == 0o700, "the logbook, written before and after the session"
    assert found[f"{DIRECTORY}/events.jsonl"] == 0o600
    assert len(found) > 30, "blocks, streams, manifest, checksums, logbook"
    assert wider_than_private(root) == {}


def test_ex4_the_temporary_archive_of_an_export_is_private(tmp_path: Path) -> None:
    recording = writer(tmp_path)
    closed = recording.close(ManualClock(epoch_millis=ENDED), "operator_stop")
    assert isinstance(closed, Ok)
    built = build_archive(tmp_path, recording.path.name, UnixMillis(1_791_195_072_000))
    assert isinstance(built, Ok)
    assert stat.S_IMODE(built.value.stat().st_mode) == 0o600


def test_ex4_a_record_already_on_disk_is_left_exactly_as_it_is(tmp_path: Path) -> None:
    """Written before this change: 755 and 644 under a 700 parent. Nothing rewrites it.

    Not at startup, not at the retention pass, not when the next session is
    recorded next to it: tightening it is a deliberate gesture, documented, and
    never a walk through thousands of files on the console's own initiative.
    """
    root = tmp_path / "records"
    root.mkdir(mode=0o700)
    old = root / "2026-09-01T080000Z_old-record"
    # Created the way the writer used to: no mode asked for, under the usual umask.
    usual = os.umask(0o022)
    try:
        old.mkdir()
        (old / "ecg_raw").mkdir()
        for name in ("manifest.json", "ticks.csv", "ecg_raw/000000.bin.gz"):
            (old / name).write_bytes(b"x")
    finally:
        os.umask(usual)
    before = modes(old)
    assert (before["."], before["ecg_raw"], before["manifest.json"]) == (0o755, 0o755, 0o644)

    clock = clock_at_start()
    journal = Journal(root, clock, retention_days=30)
    journal.drain()  # the first idle cycle applies the retention
    journal.open(session(), Privacy())
    journal.close(closing())
    journal.drain()
    assert modes(old) == before
    assert stat.S_IMODE(root.stat().st_mode) == 0o700, "what still protects it"
