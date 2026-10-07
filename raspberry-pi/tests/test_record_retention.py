"""ANH-128 EX-6: a record is purged locally only once deposited, confirmed, and kept long enough.

Nothing in the repository writes the confirmation marker yet (the deposit is
another ticket), so in production :func:`purge` removes nothing. These tests
write it through :func:`confirm_deposit`, the function the deposit will call.
"""

from __future__ import annotations

import errno
import json
import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from src.clock import ManualClock
from src.local_panel import open_journal
from src.record.codec import Privacy
from src.record.journal import PURGE_PERIOD, Journal
from src.record.reader import read
from src.record.retention import (
    MARKER_SUFFIX,
    Deposit,
    PurgeReport,
    confirm_deposit,
    confirmed_deposit,
    marker_of,
    purge,
    purgeable,
    records,
)
from src.record.schema import RecordError
from src.record.writer import Writer
from src.result import Err, Ok
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_journal_support import closing, session
from tests.record_support import manifest, row
from tests.test_failure_rig import BENCH_ENV, config_of

DAY: Final[int] = 86_400_000
CONFIRMED: Final[UnixMillis] = UnixMillis(1_791_195_072_000)
"""2026-10-05T10:11:12Z."""

THIRTY_DAYS_LATER: Final[UnixMillis] = UnixMillis(CONFIRMED + 30 * DAY)
TEN_YEARS_LATER: Final[UnixMillis] = UnixMillis(CONFIRMED + 3650 * DAY)


def closed_record(root: Path, index: int = 1) -> Path:
    """A finished, checksummed record: what a deposit would have taken."""
    created = Writer.create(
        root,
        replace(
            manifest(),
            record_id=f"record-{index}",
            local_ref=f"local-{index}",
            started_at=f"2026-10-0{index}T10:11:12Z",
        ),
    )
    assert isinstance(created, Ok)
    recording = created.value
    assert isinstance(recording.tick(row()), Ok)
    assert isinstance(recording.close(ManualClock(), "operator_stop"), Ok)
    return recording.path


def open_record(root: Path) -> Path:
    created = Writer.create(root, replace(manifest(), local_ref="still-open"))
    assert isinstance(created, Ok)
    return created.value.path


# =========================================================================
# The invariant: no confirmed deposit, no purge
# =========================================================================


def test_ex6_a_record_that_was_not_deposited_is_never_purged_whatever_its_age(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path)
    assert confirmed_deposit(record) is None
    assert not purgeable(record, TEN_YEARS_LATER, 30)
    assert not purgeable(record, TEN_YEARS_LATER, 0)
    assert purge(tmp_path, TEN_YEARS_LATER, 0) == PurgeReport(removed=(), kept=1, failed=())
    assert record.is_dir()


def test_ex6_a_confirmed_record_is_purged_after_the_retention_and_not_a_day_before(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path, 1)
    other = closed_record(tmp_path, 2)
    assert isinstance(confirm_deposit(record, "kg2storage_7", CONFIRMED), Ok)
    deposit = confirmed_deposit(record)
    assert deposit is not None
    assert (deposit.storage_id, deposit.confirmed_at) == ("kg2storage_7", "2026-10-05T10:11:12Z")

    just_before = UnixMillis(THIRTY_DAYS_LATER - 1)
    assert purge(tmp_path, just_before, 30) == PurgeReport(removed=(), kept=2, failed=())
    assert record.is_dir()

    report = purge(tmp_path, THIRTY_DAYS_LATER, 30)
    assert report == PurgeReport(removed=(record.name,), kept=1, failed=())
    assert not record.exists()
    assert not marker_of(record).exists(), "the marker goes with its record"
    assert other.is_dir(), "the record that was never deposited is still there"


def test_ex6_the_marker_sits_next_to_the_record_and_leaves_it_as_the_format_says(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path)
    before = sorted(path.name for path in record.iterdir())
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert marker_of(record) == tmp_path / f"{record.name}{MARKER_SUFFIX}"
    assert sorted(path.name for path in record.iterdir()) == before
    loaded = read(record)
    assert isinstance(loaded, Ok)
    assert loaded.value.warnings == (), "a deposited record still reads with no warning"


def test_ex6_a_marker_whose_record_is_gone_is_removed_by_the_next_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = closed_record(tmp_path)
    kept = closed_record(tmp_path, 2)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert isinstance(confirm_deposit(kept, "kg2storage", THIRTY_DAYS_LATER), Ok)
    shutil.rmtree(record)  # as a purge stopped half-way would have left things
    real = Path.unlink

    def refused(self: Path, missing_ok: bool = False) -> None:
        raise OSError(errno.EACCES, f"not allowed (missing_ok={missing_ok}) on {self.name}")

    monkeypatch.setattr(Path, "unlink", refused)
    assert purge(tmp_path, THIRTY_DAYS_LATER, 30) == PurgeReport(removed=(), kept=1, failed=())
    assert marker_of(record).exists(), "refused this time: left for the next purge"

    monkeypatch.setattr(Path, "unlink", real)
    purge(tmp_path, THIRTY_DAYS_LATER, 30)
    assert not marker_of(record).exists()
    assert marker_of(kept).exists(), "the marker of a record that is still there is kept"


def test_ex6_a_retention_of_zero_purges_as_soon_as_the_deposit_is_confirmed(
    tmp_path: Path,
) -> None:
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert purgeable(record, CONFIRMED, 0)
    assert not purgeable(record, UnixMillis(CONFIRMED - 1), 0)


def test_ex6_a_wall_clock_gone_backwards_purges_nothing(tmp_path: Path) -> None:
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    assert not purgeable(record, UnixMillis(0), 30)


# =========================================================================
# The marker: written once, about a closed record, and never trusted in doubt
# =========================================================================


def test_ex6_an_open_record_cannot_be_confirmed_nor_purged(tmp_path: Path) -> None:
    record = open_record(tmp_path)
    refused = confirm_deposit(record, "kg2storage", CONFIRMED)
    assert isinstance(refused, Err)
    assert refused.error == RecordError("close", "FileNotFoundError:ENOENT")
    assert not marker_of(record).exists()
    # Even a marker somebody forged does not make an open record purgeable.
    marker_of(record).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "storage_id": "forged",
                "confirmed_at": "2026-10-05T10:11:12Z",
                "checksum": "00" * 32,
            }
        )
    )
    assert confirmed_deposit(record) is None
    assert purge(tmp_path, TEN_YEARS_LATER, 0).removed == ()


def test_ex6_a_confirmation_is_written_once_with_an_opaque_identifier(tmp_path: Path) -> None:
    record = closed_record(tmp_path)
    for bad in ("", "with space", "../escape", "a@b.fr"):
        refused = confirm_deposit(record, bad, CONFIRMED)
        assert isinstance(refused, Err)
        assert refused.error.detail == "invalid_storage_id"
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    again = confirm_deposit(record, "another", THIRTY_DAYS_LATER)
    assert isinstance(again, Err)
    assert again.error.detail == "FileExistsError:EEXIST"
    deposit = confirmed_deposit(record)
    assert deposit is not None
    assert deposit.storage_id == "kg2storage"


def marker_fields(record: Path) -> dict[str, object]:
    deposit = confirmed_deposit(record)
    assert deposit is not None
    return {
        "schema_version": deposit.schema_version,
        "storage_id": deposit.storage_id,
        "confirmed_at": deposit.confirmed_at,
        "checksum": deposit.checksum,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("confirmed_at", "2026-10-05T10:11:12"),
        ("confirmed_at", "2026-10-05T12:11:12+02:00"),
        ("confirmed_at", "yesterday"),
        ("checksum", "00" * 32),
        ("unexpected", "field"),
    ],
)
def test_ex6_a_marker_in_any_doubt_keeps_the_record(
    tmp_path: Path, field: str, value: object
) -> None:
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    fields = marker_fields(record)
    fields[field] = value
    marker_of(record).write_text(json.dumps(fields))
    assert confirmed_deposit(record) is None
    assert purge(tmp_path, TEN_YEARS_LATER, 0) == PurgeReport(removed=(), kept=1, failed=())


def test_ex6_a_marker_that_is_not_json_keeps_the_record(tmp_path: Path) -> None:
    record = closed_record(tmp_path)
    marker_of(record).write_text("deposited: yes")
    assert confirmed_deposit(record) is None
    assert not purgeable(record, TEN_YEARS_LATER, 0)


def test_ex6_a_record_modified_after_its_deposit_is_not_the_deposited_one(tmp_path: Path) -> None:
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    with (record / "checksums.sha256").open("a") as handle:
        handle.write("0  appended-later\n")
    assert confirmed_deposit(record) is None
    assert purge(tmp_path, TEN_YEARS_LATER, 0).removed == ()


def test_the_deposit_marker_is_a_validated_closed_record() -> None:
    deposit = Deposit(
        schema_version=1,
        storage_id="kg2storage",
        confirmed_at="2026-10-05T10:11:12Z",
        checksum="ab" * 32,
    )
    assert deposit.schema_version == 1


# =========================================================================
# What a purge may touch
# =========================================================================


def test_ex6_only_record_directories_directly_under_the_root_are_ever_candidates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    record = closed_record(root)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    elsewhere = closed_record(tmp_path / "elsewhere")
    assert isinstance(confirm_deposit(elsewhere, "kg2storage", CONFIRMED), Ok)
    (root / "notes.txt").write_text("not a record")
    (root / ".record-tmp").mkdir()
    (root / "2026-10-05T101112Z_a-file").write_text("a file with a record's name")
    (root / "2026-10-09T101112Z_linked").symlink_to(elsewhere, target_is_directory=True)
    assert [path.name for path in records(root)] == [record.name]

    report = purge(root, TEN_YEARS_LATER, 0)
    assert report == PurgeReport(removed=(record.name,), kept=0, failed=())
    assert elsewhere.is_dir(), "a link out of the records directory was not followed"
    assert sorted(path.name for path in root.iterdir()) == [
        ".record-tmp",
        "2026-10-05T101112Z_a-file",
        "2026-10-09T101112Z_linked",
        "notes.txt",
    ]
    assert marker_of(elsewhere).exists()


def test_ex6_a_record_that_cannot_be_removed_stays_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = closed_record(tmp_path)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)

    def refused(_path: Path) -> None:
        raise OSError(errno.EACCES, "not allowed")

    monkeypatch.setattr(shutil, "rmtree", refused)
    assert purge(tmp_path, TEN_YEARS_LATER, 0) == PurgeReport(
        removed=(), kept=0, failed=(record.name,)
    )
    assert record.is_dir()


def test_ex6_a_missing_records_directory_purges_nothing_and_does_not_raise(
    tmp_path: Path,
) -> None:
    assert purge(tmp_path / "missing", TEN_YEARS_LATER, 0) == PurgeReport((), 0, ())


# =========================================================================
# When it runs: on the journal thread, between two sessions
# =========================================================================


def test_ex6_the_journal_applies_the_retention_at_startup_then_every_six_hours_when_idle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    first = closed_record(root, 1)
    second = closed_record(root, 2)
    assert isinstance(confirm_deposit(first, "kg2storage", CONFIRMED), Ok)
    clock = ManualClock(Monotonic(10.0), THIRTY_DAYS_LATER)
    journal = Journal(root, clock, retention_days=30)
    assert first.is_dir(), "nothing is removed before the journal's first cycle"

    with caplog.at_level(logging.WARNING, logger="src.record.journal"):
        journal.drain()
    assert not first.exists()
    assert second.is_dir()
    assert f"removed ['{first.name}']" in caplog.text

    # The second record is confirmed while a session is being recorded.
    assert isinstance(confirm_deposit(second, "kg2storage", UnixMillis(0)), Ok)
    journal.open(session(7), Privacy())
    journal.drain()
    clock.advance(Seconds(PURGE_PERIOD))
    journal.drain()
    assert second.is_dir(), "the retention waits for the session to end"

    journal.close(closing())
    journal.drain()
    assert not second.exists()
    assert len(records(root)) == 1, "the session just recorded was not deposited: it stays"

    # Nothing to do: the next pass comes six hours later, and says nothing.
    caplog.clear()
    clock.advance(Seconds(PURGE_PERIOD))
    journal.drain()
    assert caplog.text == ""


def test_ex6_a_journal_without_a_retention_never_removes_anything(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    record = closed_record(root)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    clock = ManualClock(Monotonic(10.0), TEN_YEARS_LATER)
    journal = Journal(root, clock)
    journal.drain()
    clock.advance(Seconds(PURGE_PERIOD))
    journal.drain()
    assert record.is_dir()


def test_ex6_the_console_gives_the_journal_the_configured_retention(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    record = closed_record(root)
    assert isinstance(confirm_deposit(record, "kg2storage", CONFIRMED), Ok)
    env = {**BENCH_ENV, "RECORD_ROOT": str(root), "RECORD_LOCAL_RETENTION_DAYS": "7"}
    seven_days = UnixMillis(CONFIRMED + 7 * DAY)
    early = open_journal(config_of(env), ManualClock(Monotonic(0.0), UnixMillis(seven_days - 1)))
    early.drain()
    assert record.is_dir()
    due = open_journal(config_of(env), ManualClock(Monotonic(0.0), seven_days))
    due.drain()
    assert not record.exists()
