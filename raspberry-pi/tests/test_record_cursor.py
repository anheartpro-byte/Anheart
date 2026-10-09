"""The synchronisation cursor of a session record (ANH-129 EX-2).

What is established here:

* the cursor is a file NEXT TO the record directory, never inside it: the
  record's own content, and its checksums, are untouched by a synchronisation;
* it is written atomically and privately (mode 600): a reader sees the old
  cursor or the new one, and a write that fails leaves the old one in place;
* a cursor that is truncated, corrupt, of another version or out of range is
  read as unusable, never as a position: the record is then sent again from
  its beginning, and the dashboard's idempotence does the rest;
* a record with no cursor is told apart from one whose cursor is unusable;
* a cursor whose record is gone, and a temporary cursor left by a console
  that was killed, are swept away.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from src.clock import ManualClock
from src.record import cursor as cursor_module
from src.record.cursor import (
    BASELINE_NAME,
    CURSOR_SUFFIX,
    TEMP_PREFIX,
    TEMP_SUFFIX,
    Baseline,
    Cursor,
    NoCursor,
    UnreadableCursor,
    boot_identity,
    cursor_of,
    load,
    load_baseline,
    store,
    store_baseline,
    sweep,
)
from src.record.logbook import ACTIVE, DIRECTORY, PREVIOUS
from src.record.reader import read
from src.record.writer import PRIVATE_FILE, create_private
from src.result import Err, Ok
from tests.record_support import row, writer

RECORD = "2026-10-05T101112Z_local-4"


def record_in(root: Path) -> Path:
    """A real record directory under ``root``, with one tick."""
    made = writer(root)
    assert isinstance(made.tick(row()), Ok)
    assert made.path.name == RECORD
    return made.path


def test_the_cursor_is_next_to_the_record_never_inside(tmp_path: Path) -> None:
    record = record_in(tmp_path)
    before = sorted(path.name for path in record.iterdir())

    stored = store(record, Cursor(session_id="cloud-1", ticks_offset=120, last_t=5))

    assert isinstance(stored, Ok)
    assert cursor_of(record) == tmp_path / f"{RECORD}{CURSOR_SUFFIX}"
    assert cursor_of(record).is_file()
    assert sorted(path.name for path in record.iterdir()) == before


def test_a_synchronised_record_still_reads_without_a_warning_about_the_cursor(
    tmp_path: Path,
) -> None:
    """The record is closed, checksummed, then synchronised: its checksums still hold."""
    made = writer(tmp_path)
    assert isinstance(made.tick(row()), Ok)

    closed = made.close(ManualClock(), "operator_stop")
    assert isinstance(closed, Ok)
    assert isinstance(store(made.path, Cursor(state="complete", end="sent")), Ok)

    recording = read(made.path)

    assert isinstance(recording, Ok)
    assert recording.value.warnings == ()


def test_the_cursor_is_private_to_the_service_user(tmp_path: Path) -> None:
    record = record_in(tmp_path)

    assert isinstance(store(record, Cursor()), Ok)
    assert isinstance(store(record, Cursor(ticks_offset=10)), Ok)

    mode = stat.S_IMODE(cursor_of(record).stat().st_mode)
    assert mode == 0o600


def test_the_cursor_and_the_list_of_older_records_are_created_by_the_writer_s_private_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As every file of a record or next to one: mode 600 from the call that creates it,
    whatever the umask, the first time and each time the cursor is replaced."""
    record = record_in(tmp_path)
    created: list[tuple[bool, int]] = []

    def watched(path: Path, content: bytes) -> None:
        create_private(path, content)
        temporary = path.name.startswith(TEMP_PREFIX) and path.name.endswith(TEMP_SUFFIX)
        created.append((temporary, stat.S_IMODE(path.stat().st_mode)))

    monkeypatch.setattr(cursor_module, "create_private", watched)
    usual = os.umask(0)
    try:
        assert isinstance(store(record, Cursor()), Ok)
        assert isinstance(store(record, Cursor(ticks_offset=10)), Ok)
        assert isinstance(store_baseline(tmp_path, Baseline(left_alone=("older",))), Ok)
    finally:
        os.umask(usual)

    assert created == [(True, PRIVATE_FILE)] * 3
    assert stat.S_IMODE(cursor_of(record).stat().st_mode) == PRIVATE_FILE
    assert stat.S_IMODE((tmp_path / BASELINE_NAME).stat().st_mode) == PRIVATE_FILE
    assert load(record) == Cursor(ticks_offset=10)
    assert load_baseline(tmp_path) == Baseline(left_alone=("older",))
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [RECORD, f"{RECORD}{CURSOR_SUFFIX}", BASELINE_NAME]
    )


def test_what_was_stored_is_what_is_loaded(tmp_path: Path) -> None:
    record = record_in(tmp_path)
    cursor = Cursor(
        session_id="k17abc",
        boot_id="0f8d3c2a-1b4e-4c6d-9e7f-123456789abc",
        start_confirmed=False,
        ticks_offset=4096,
        last_t=1_700_000_123_000,
        events_offset=512,
        last_seq=7,
        end="sent",
        state="complete",
        rejected_points=2,
        rejected_events=1,
        refused=3,
    )

    assert isinstance(store(record, cursor), Ok)

    assert load(record) == cursor


def test_a_new_cursor_owes_everything() -> None:
    fresh = Cursor()
    assert (fresh.ticks_offset, fresh.last_t, fresh.events_offset, fresh.last_seq) == (
        0,
        None,
        0,
        -1,
    )
    assert (fresh.end, fresh.state) == ("pending", "pending")
    assert fresh.session_id is None
    assert fresh.start_confirmed is True


def test_a_record_without_a_cursor_is_told_apart_from_an_unusable_one(tmp_path: Path) -> None:
    record = record_in(tmp_path)

    assert load(record) == NoCursor()

    cursor_of(record).write_bytes(b"")
    assert load(record) == UnreadableCursor("invalid")


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b'{"schema_version":1,"session_id":"cloud-1","ticks_off',
        b"not json at all\n",
        b"[1, 2, 3]\n",
        b'{"schema_version":2}\n',
        b'{"schema_version":1,"unknown_field":true}\n',
        b'{"schema_version":1,"ticks_offset":-1}\n',
        b'{"schema_version":1,"events_offset":-5}\n',
        b'{"schema_version":1,"last_seq":-2}\n',
        b'{"schema_version":1,"rejected_points":-1}\n',
        b'{"schema_version":1,"rejected_events":-1}\n',
        b'{"schema_version":1,"refused":-1}\n',
        b'{"schema_version":1,"ticks_offset":"far"}\n',
        b'{"schema_version":1,"state":"done"}\n',
        b'{"schema_version":1,"end":"maybe"}\n',
        b"\xff\xfe\x00",
    ],
)
def test_a_truncated_or_corrupt_cursor_is_never_read_as_a_position(
    tmp_path: Path, content: bytes
) -> None:
    record = record_in(tmp_path)
    cursor_of(record).write_bytes(content)

    assert load(record) == UnreadableCursor("invalid")


def test_a_cursor_the_disk_refuses_to_give_is_unusable_too(tmp_path: Path) -> None:
    record = record_in(tmp_path)
    cursor_of(record).mkdir()  # reading a directory fails with an errno, not "not found"

    found = load(record)

    assert isinstance(found, UnreadableCursor)
    assert found.detail.startswith(("IsADirectoryError", "PermissionError"))


def test_a_stored_cursor_replaces_the_previous_one_whole(tmp_path: Path) -> None:
    record = record_in(tmp_path)
    assert isinstance(store(record, Cursor(session_id="cloud-1", ticks_offset=10)), Ok)

    assert isinstance(store(record, Cursor(session_id="cloud-1", ticks_offset=2000)), Ok)

    assert json.loads(cursor_of(record).read_text(encoding="utf-8"))["ticks_offset"] == 2000
    # Nothing else is left behind: no temporary file.
    assert sorted(path.name for path in tmp_path.iterdir()) == [RECORD, f"{RECORD}{CURSOR_SUFFIX}"]


def test_a_write_that_fails_leaves_the_previous_cursor_and_no_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk refuses in the middle of the write: the cursor is the one of before."""
    record = record_in(tmp_path)
    previous = Cursor(session_id="cloud-1", ticks_offset=10)
    assert isinstance(store(record, previous), Ok)

    def refuse(_descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", refuse)
    failed = store(record, Cursor(session_id="cloud-1", ticks_offset=2000))

    assert isinstance(failed, Err)
    assert failed.error.detail == "OSError:ENOSPC"
    assert load(record) == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == [RECORD, f"{RECORD}{CURSOR_SUFFIX}"]


def test_a_cursor_that_cannot_even_be_created_is_an_error_not_an_exception(
    tmp_path: Path,
) -> None:
    gone = tmp_path / "no-such-directory" / RECORD

    failed = store(gone, Cursor())

    assert isinstance(failed, Err)
    assert failed.error.detail == "FileNotFoundError:ENOENT"


def test_debris_that_cannot_be_removed_is_left_for_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = record_in(tmp_path)

    def refuse(_descriptor: int) -> None:
        raise OSError(5, "Input/output error")

    def cannot_unlink(_self: Path, **_options: bool) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "fsync", refuse)
    monkeypatch.setattr(Path, "unlink", cannot_unlink)

    failed = store(record, Cursor())

    assert isinstance(failed, Err)
    assert failed.error.detail == "OSError:EIO"


def test_a_cursor_whose_record_is_gone_is_swept_away(tmp_path: Path) -> None:
    kept = record_in(tmp_path)
    assert isinstance(store(kept, Cursor(ticks_offset=1)), Ok)
    orphan = tmp_path / f"2026-10-01T080000Z_purged{CURSOR_SUFFIX}"
    orphan.write_text("{}", encoding="utf-8")
    leftover = tmp_path / f"{TEMP_PREFIX}abc123{TEMP_SUFFIX}"
    leftover.write_text("{", encoding="utf-8")
    # Neither a cursor nor a temporary cursor: never touched.
    marker = tmp_path / f"{RECORD}.deposit.json"
    marker.write_text("{}", encoding="utf-8")
    stranger = tmp_path / f"notes{CURSOR_SUFFIX}"
    stranger.write_text("{}", encoding="utf-8")
    # The marker of a record that is gone is the retention's to remove, not the sweep's.
    widow = tmp_path / "2026-10-01T080000Z_purged.deposit.json"
    widow.write_text("{}", encoding="utf-8")
    # The logbook, both its files, and the directory a record being closed leaves a moment.
    logbook = tmp_path / DIRECTORY
    logbook.mkdir()
    (logbook / ACTIVE).write_text('{"kind":"refusal"}\n', encoding="utf-8")
    (logbook / PREVIOUS).write_text('{"kind":"refusal"}\n', encoding="utf-8")
    closing = tmp_path / ".record-k3j2h1"
    closing.mkdir()
    (closing / "manifest.json").write_text("{}", encoding="utf-8")
    assert isinstance(store_baseline(tmp_path, Baseline()), Ok)

    removed = sweep(tmp_path)

    assert removed == (leftover.name, orphan.name)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [
            RECORD,
            f"{RECORD}{CURSOR_SUFFIX}",
            marker.name,
            stranger.name,
            widow.name,
            DIRECTORY,
            closing.name,
            BASELINE_NAME,
        ]
    )
    assert sorted(path.name for path in logbook.iterdir()) == [PREVIOUS, ACTIVE]
    assert (closing / "manifest.json").is_file()
    assert load(kept) == Cursor(ticks_offset=1)


def test_sweeping_a_directory_that_cannot_be_listed_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="No such file"):
        sweep(tmp_path / "absent")


def test_the_boot_is_named_by_the_system_or_not_at_all(tmp_path: Path) -> None:
    named = tmp_path / "boot_id"
    named.write_text("0f8d3c2a-1b4e-4c6d-9e7f-123456789abc\n", encoding="ascii")
    assert boot_identity(named) == "0f8d3c2a-1b4e-4c6d-9e7f-123456789abc"

    assert boot_identity(tmp_path / "absent") is None
    named.write_text("not an identifier!\n", encoding="ascii")
    assert boot_identity(named) is None
    named.write_bytes(b"\xff\xfe")
    assert boot_identity(named) is None


def test_the_default_boot_identifier_is_the_kernel_s() -> None:
    assert Path("/proc/sys/kernel/random/boot_id") == cursor_module.BOOT_ID_PATH
    found = boot_identity()
    # Linux names its boot; elsewhere nothing does, and nothing is invented.
    assert found is None or len(found) >= 8
