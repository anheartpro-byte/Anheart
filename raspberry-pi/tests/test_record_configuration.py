from __future__ import annotations

import asyncio
import errno
import hashlib
import stat
import threading
from pathlib import Path

import pytest

from src.contract import SoftwareVersion
from src.local_panel import RUNTIME_LIMITS
from src.record import configuration
from src.record import journal as journal_module
from src.record.codec import Privacy
from src.record.configuration import DIRECTORY, MAX_CONFIG_BYTES, WARNING_PREFIX, persist
from src.record.cursor import sweep
from src.record.deposit import RecordDeposit
from src.record.deposit_files import next_record
from src.record.export import build_archive, listing, locate
from src.record.journal import Journal
from src.record.logbook import Logbook, note
from src.record.reader import read
from src.record.retention import purge, records
from src.record.schema import EventKind
from src.record.session import applied_configuration, stamp_for
from src.record.upload import owed_records
from src.result import Err, Ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.runtime import RuntimeState
from src.training.safety import SafetyLimits
from src.units import Bpm, UnixMillis
from tests.record_console_support import recorded_rig, start_bench
from tests.record_journal_support import clock_at_start, closing, record_of, session, watch_tree
from tests.test_failure_rig import BENCH_ENV, OPERATOR, attest, config_of
from tests.test_record_deposit import DepositServer

DOCUMENT = b'{"configuration":1}'


def _path(root: Path, document: bytes = DOCUMENT) -> Path:
    return root / DIRECTORY / (hashlib.sha256(document).hexdigest() + ".json")


def test_document_hash_stays_identical_and_excludes_secrets_identities_and_health() -> None:
    sentinel = "SentinelleConfidentielle"
    config = config_of(
        {
            **BENCH_ENV,
            "MOTOR_MAX_RPM": "300",
            "UI_TOKEN": sentinel,
            "MACHINE_API_KEY": sentinel,
            "CONVEX_URL": "https://example.convex.site",
            "RECORD_MACHINE_ID": sentinel,
            "RECORD_ORGANIZATION_ID": sentinel,
        }
    )
    safety = SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
    content = applied_configuration(config, RUNTIME_LIMITS, safety, DEFAULT_MOTION_LIMITS)
    stamp = stamp_for(
        config, RUNTIME_LIMITS, safety, DEFAULT_MOTION_LIMITS, SoftwareVersion("reference")
    )
    assert hashlib.sha256(content).hexdigest() == stamp.config_hash
    assert stamp.config_hash == "84b15fbb12301f57aa0e7407eaa6b38dc3bd3afd50f2fa6c52c10bb260cdcff6"
    assert sentinel.encode() not in content
    for forbidden in (
        b"subject",
        b"operator",
        b"token",
        b"api_key",
        b"address",
        b"questionnaire",
        b"bpm_live",
    ):
        assert forbidden not in content
    assert b"\n" not in content
    assert len(content) < MAX_CONFIG_BYTES


def test_one_private_file_per_hash_is_never_rewritten(tmp_path: Path) -> None:
    assert persist(tmp_path, DOCUMENT) is None
    path = _path(tmp_path)
    assert path.read_bytes() == DOCUMENT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    before = path.stat()
    assert persist(tmp_path, DOCUMENT) is None
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(path.parent.iterdir()) == [path]
    path.write_bytes(b"different")
    assert persist(tmp_path, DOCUMENT) == f"{WARNING_PREFIX} existing_document_differs"
    assert path.read_bytes() == b"different"


def test_document_size_is_bounded_before_any_disk_access(tmp_path: Path) -> None:
    with watch_tree(tmp_path) as touches:
        assert (
            persist(tmp_path, b"x" * (MAX_CONFIG_BYTES + 1))
            == f"{WARNING_PREFIX} document_exceeds_limit"
        )
        assert touches.seen == []
    assert persist(tmp_path, b"x" * MAX_CONFIG_BYTES) is None


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EROFS])
async def test_a_configuration_write_failure_never_prevents_a_console_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    recorded = recorded_rig(tmp_path)

    def refuse(_path: Path, _content: bytes) -> None:
        raise OSError(error_number, "private path and text must not be logged")

    monkeypatch.setattr(configuration, "create_private", refuse)
    async with recorded.rig.http() as client:
        await start_bench(recorded, client)
        assert recorded.state() is RuntimeState.RUNNING
        warnings = [
            event.detail for event in recorded.recording().events if event.kind is EventKind.WARNING
        ]
        assert f"{WARNING_PREFIX} OSError:{errno.errorcode[error_number]}" in warnings
        assert list((recorded.root / DIRECTORY).iterdir()) == []
        assert not any("private path" in warning for warning in warnings)


def test_an_unexpected_configuration_failure_costs_only_the_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_root: Path, _content: bytes) -> str | None:
        raise RuntimeError("confidential exception")

    clock = clock_at_start()
    journal = Journal(tmp_path, clock)
    journal.configure(DOCUMENT)
    monkeypatch.setattr(journal_module, "persist", broken)
    journal.open(session(), Privacy())
    journal.close(closing())
    journal.drain()
    loaded = read(record_of(journal))
    assert isinstance(loaded, Ok)
    assert [event.detail for event in loaded.value.events] == ["replay_config: RuntimeError"]


def test_startup_only_queues_the_document_and_never_writes_from_a_tick(tmp_path: Path) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path, clock)
    with watch_tree(tmp_path) as touches:
        journal.configure(DOCUMENT)
        journal.open(session(), Privacy())
        journal.status(clock.monotonic())
        assert touches.seen == []
    assert not _path(tmp_path).exists()
    journal.drain()
    assert _path(tmp_path).read_bytes() == DOCUMENT
    journal.close(closing())
    journal.drain()


async def test_config_is_ignored_by_retention_sync_deposit_export_and_logbook(
    tmp_path: Path,
) -> None:
    assert persist(tmp_path, DOCUMENT) is None
    directory = tmp_path / DIRECTORY
    decoy = directory / "2026-10-05T101112Z_decoy"
    decoy.mkdir()
    for name in (
        "manifest.json",
        "checksums.sha256",
        "events.jsonl",
        "decoy.sync.json",
        "decoy.deposit.json",
    ):
        (decoy / name).write_bytes(b"sentinel")
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert records(tmp_path) == ()
    assert listing(tmp_path) == ()
    assert locate(tmp_path, DIRECTORY) is None
    assert isinstance(build_archive(tmp_path, DIRECTORY, UnixMillis(0)), Err)
    assert sweep(tmp_path) == ()
    owed = owed_records(tmp_path)
    assert isinstance(owed, Ok)
    assert owed.value.names == ()
    assert next_record(tmp_path) == Ok(None)
    assert purge(tmp_path, UnixMillis(9_000_000_000_000), 1).removed == ()
    clock = clock_at_start()
    server = DepositServer()
    deposit = RecordDeposit(tmp_path, clock, server, server.upload)
    await deposit.step()
    await deposit.close()
    assert server.calls == 0
    logbook = Logbook(tmp_path)
    assert isinstance(
        logbook.append(
            note(
                wall_clock=clock.unix_millis(),
                at=clock.monotonic(),
                kind=EventKind.WARNING,
                detail="machine_event",
                actor="system",
            )
        ),
        Ok,
    )
    assert isinstance(logbook.sync(), Ok)
    assert {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("conflict", [False, True])
def test_document_refusals_are_written_in_every_session(tmp_path: Path, conflict: bool) -> None:
    clock = clock_at_start()
    journal = Journal(tmp_path, clock)
    if conflict:
        assert persist(tmp_path, DOCUMENT) is None
        _path(tmp_path).write_bytes(b"different")
        content = DOCUMENT
        reason = "existing_document_differs"
    else:
        content = b"x" * (MAX_CONFIG_BYTES + 1)
        reason = "document_exceeds_limit"
    journal.configure(content)
    for index in (1, 2):
        journal.open(session(index), Privacy())
        journal.close(closing())
        journal.drain()
        loaded = read(record_of(journal))
        assert isinstance(loaded, Ok)
        assert [event.detail for event in loaded.value.events] == [f"{WARNING_PREFIX} {reason}"]


def test_directory_sync_is_skipped_on_systems_that_cannot_sync_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(configuration, "DIRECTORY_SYNC", False)
    assert persist(tmp_path, DOCUMENT) is None
    assert _path(tmp_path).read_bytes() == DOCUMENT


async def test_a_hung_configuration_write_does_not_delay_a_console_start_or_ticks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = recorded_rig(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    caller = threading.get_ident()
    writers: list[int] = []

    def hold(root: Path, content: bytes) -> str | None:
        writers.append(threading.get_ident())
        entered.set()
        if not release.wait(5):
            raise AssertionError("the test did not release the document writer")
        return persist(root, content)

    monkeypatch.setattr(journal_module, "persist", hold)
    async with recorded.rig.http() as client:
        recorded.journal.start()
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert response.status_code == 202
            for _ in range(5):
                await asyncio.wait_for(recorded.tick(0.2, drain=False), timeout=0.4)
            assert recorded.state() is RuntimeState.RUNNING
            assert writers == [writers[0]]
            assert writers[0] != caller
            assert recorded.journal.status(recorded.clock.monotonic()).pending > 0
        finally:
            release.set()
            assert recorded.journal.stop()
