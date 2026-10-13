from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, override

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import CloudError, CloudSync, Document, HttpxTransport, Unreachable
from src.local_config import CloudConfig
from src.local_panel import build_panel
from src.record import deposit_files
from src.record.cursor import Cursor, CursorError, load, store
from src.record.deposit import RecordDeposit
from src.record.deposit_files import (
    MAX_FILE_BYTES,
    DepositRecord,
    confirm,
    next_record,
    payload,
    remember,
)
from src.record.ecg import MAX_RAW_BLOCK_BYTES, Header, RawBlock, encode_block
from src.record.journal import Journal
from src.record.retention import confirmed_deposit
from src.record.rows import JsonValue
from src.record.writer import MANIFEST, Writer
from src.result import Err, Ok, Result
from src.training.runtime import RuntimeState
from src.units import Seconds, UnixMillis
from tests.record_support import manifest as make_manifest
from tests.test_cloud_sync import config_of, manual, rig
from tests.test_record_retention import closed_record


@dataclass
class DepositServer:
    uploads: dict[str, bytes] = field(default_factory=dict[str, bytes])
    files: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    expected: tuple[str, ...] = ()
    offline: bool = False
    commit_lost: bool = False
    hold_upload: asyncio.Event | None = None
    uploading: asyncio.Event = field(default_factory=asyncio.Event)
    uploads_sent: int = 0
    calls: int = 0

    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        assert path == "/api/machine/records/status"
        assert params == {"sessionId": "session-1"}
        self.calls += 1
        if self.offline:
            return Err(Unreachable("offline"))
        state: Literal["partial", "complete"] = (
            "complete" if self.expected and set(self.expected) == set(self.files) else "partial"
        )
        rows: list[dict[str, object]] = [
            {"path": name, "sha256": digest, "verified": True}
            for name, (_id, digest) in self.files.items()
        ]
        return Ok(
            {"record": state, "files": rows, "expectedCount": len(self.expected), "deleted": False}
        )

    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        self.calls += 1
        if self.offline:
            return Err(Unreachable("offline"))
        name = body["path"]
        assert isinstance(name, str)
        if path.endswith("upload-url"):
            if name in self.files:
                return Ok({"storageId": self.files[name][0], "verified": True})
            return Ok(
                {
                    "uploadUrl": "https://test.convex.cloud/upload",
                    "contentType": "application/octet-stream",
                }
            )
        storage_id = body["storageId"]
        digest = body["sha256"]
        assert isinstance(storage_id, str)
        assert isinstance(digest, str)
        assert hashlib.sha256(self.uploads[storage_id]).hexdigest() == digest
        self.files[name] = (storage_id, digest)
        if name == "manifest.json":
            manifest = MANIFEST.validate_json(self.uploads[storage_id])
            assert manifest.files is not None
            self.expected = manifest.files
        if self.commit_lost:
            self.commit_lost = False
            return Err(Unreachable("commit response lost"))
        return Ok({"accepted": True})

    async def upload(
        self, url: str, content_type: str, content: bytes
    ) -> Result[Document, CloudError]:
        assert url == "https://test.convex.cloud/upload"
        assert content_type == "application/octet-stream"
        self.uploading.set()
        if self.hold_upload is not None:
            await self.hold_upload.wait()
        self.uploads_sent += 1
        storage_id = f"storage-{self.uploads_sent}"
        self.uploads[storage_id] = content
        return Ok({"storageId": storage_id})


def ready_record(root: Path) -> Path:
    record = closed_record(root)
    manifest = MANIFEST.validate_json((record / "manifest.json").read_bytes())
    assert isinstance(
        store(
            record,
            Cursor(
                local_ref=manifest.local_ref, session_id="session-1", end="sent", state="complete"
            ),
        ),
        Ok,
    )
    return record


async def test_complete_deposit_survives_a_lost_commit_and_process_restart(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    clock = ManualClock()
    server = DepositServer(commit_lost=True)
    deposit = RecordDeposit(record.parent, clock, server, server.upload)
    await deposit.step()
    assert server.uploads_sent == 1
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert cursor.record_uploads["manifest.json"] == "storage-1"
    assert confirmed_deposit(record) is None
    await deposit.close()
    restarted = RecordDeposit(record.parent, clock, server, server.upload)
    for _ in range(20):
        await restarted.step()
        if confirmed_deposit(record) is not None:
            break
    assert confirmed_deposit(record) is not None
    assert set(server.files) == set(server.expected)
    assert server.uploads_sent == len(server.expected)
    await restarted.close()


async def test_arming_cancels_a_held_upload_and_can_resume_later(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    clock = ManualClock()
    server = DepositServer(hold_upload=asyncio.Event())
    deposit = RecordDeposit(record.parent, clock, server, server.upload)
    deposit.schedule()
    await asyncio.wait_for(server.uploading.wait(), 2)
    deposit.pause()
    await asyncio.sleep(0)
    assert server.uploads_sent == 0
    assert confirmed_deposit(record) is None
    assert server.hold_upload is not None
    assert server.hold_upload is not None
    server.hold_upload.set()
    deposit.schedule()
    await deposit.close()


def test_only_closed_acknowledged_records_are_eligible_and_corruption_never_confirms(
    tmp_path: Path,
) -> None:
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert isinstance(found.value, DepositRecord)
    current = found.value
    read = payload(current, "ticks.csv")
    assert isinstance(read, Ok)
    assert isinstance(confirm(current, {}, UnixMillis(0)), Err)
    (record / "ticks.csv").write_text("changed after close")
    changed = payload(current, "ticks.csv")
    assert isinstance(changed, Err)
    assert changed.error.detail == "local_checksum_mismatch"
    assert isinstance(payload(current, "../outside"), Err)
    assert confirmed_deposit(record) is None
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert isinstance(store(record, replace(cursor, state="pending")), Ok)
    assert next_record(record.parent) == Ok(None)


async def test_upload_uses_only_storage_origin_and_never_sends_machine_credentials() -> None:
    seen: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"storageId": "one"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(response),
        headers={"Authorization": "Bearer private-machine-key"},
    )
    transport = HttpxTransport(
        CloudConfig(url="https://test.convex.site", api_key="private-machine-key"), client=client
    )
    try:
        assert isinstance(
            await transport.upload(
                "https://foreign.example/upload", "application/octet-stream", b"data"
            ),
            Err,
        )
        assert seen == []
        assert await transport.upload(
            "https://test.convex.cloud/upload", "application/octet-stream", b"data"
        ) == Ok({"storageId": "one"})
        assert "authorization" not in seen[0].headers
        assert seen[0].content == b"data"
    finally:
        await transport.close()


def test_oversize_file_is_kept_locally_without_loading_unbounded_data(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert found.value is not None
    with (record / "ticks.csv").open("wb") as handle:
        handle.truncate(MAX_FILE_BYTES + 1)
    assert isinstance(payload(found.value, "ticks.csv"), Err)
    assert (record / "ticks.csv").stat().st_size == MAX_FILE_BYTES + 1
    assert confirmed_deposit(record) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", None),
        ("end", "pending"),
        ("local_ref", "another-record"),
    ],
)
def test_unconfirmed_or_mismatched_cursor_is_not_a_deposit(
    tmp_path: Path,
    field: str,
    value: str | None,
) -> None:
    record = ready_record(tmp_path / "records")
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    if field == "session_id":
        changed = replace(cursor, session_id=value)
    elif field == "end":
        assert value == "pending"
        changed = replace(cursor, end="pending")
    else:
        changed = replace(cursor, local_ref=value)
    saved = store(record, changed)
    assert isinstance(saved, Ok)
    assert next_record(record.parent) == Ok(None)


def test_unreadable_record_or_checksum_does_not_create_confirmation(tmp_path: Path) -> None:
    assert isinstance(next_record(tmp_path / "missing"), Err)
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert found.value is not None
    current = found.value
    assert isinstance(remember(current, "manifest.json", "stored-one"), Ok)
    (record / "checksums.sha256").write_text("broken checksum")
    assert isinstance(next_record(record.parent), Err)
    (record / "checksums.sha256").write_text("a" * 64 + "  missing.csv\n")
    assert isinstance(next_record(record.parent), Err)
    (record / "checksums.sha256").write_text("invalid  missing.csv\n")
    assert isinstance(next_record(record.parent), Err)
    (record / "manifest.json").write_text("truncated")
    assert isinstance(next_record(record.parent), Err)
    assert confirmed_deposit(record) is None


def test_file_disappearing_or_leaving_record_refuses_the_deposit(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert found.value is not None
    current = found.value
    (record / "ticks.csv").unlink()
    assert isinstance(payload(current, "ticks.csv"), Err)
    escaped = replace(current, files=("../outside.csv",))
    assert isinstance(payload(escaped, "../outside.csv"), Err)
    assert confirmed_deposit(record) is None
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert isinstance(store(record, replace(cursor, session_id="another-session")), Ok)
    assert isinstance(remember(current, "ticks.csv", "stored-one"), Err)


async def test_an_outage_backs_off_without_upload_or_confirmation(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    server = DepositServer(offline=True)
    clock = ManualClock()
    deposit = RecordDeposit(record.parent, clock, server, server.upload)
    await deposit.step()
    count = server.calls
    deposit.schedule()
    await asyncio.sleep(0)
    assert server.calls == count
    assert server.uploads_sent == 0
    assert confirmed_deposit(record) is None
    server.offline = False
    clock.advance(Seconds(5))
    deposit.schedule()
    deposit.schedule()
    await asyncio.wait_for(server.uploading.wait(), 2)
    await deposit.close()
    empty = RecordDeposit(tmp_path / "absent", clock, server, server.upload)
    await empty.step()
    await empty.close()


async def test_transport_failure_and_invalid_origin_are_retryable() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("lost", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(unreachable))
    transport = HttpxTransport(
        CloudConfig(url="https://test.convex.site", api_key="key"), client=client
    )
    try:
        assert isinstance(
            await transport.upload(
                "https://test.convex.cloud/upload", "application/octet-stream", b""
            ),
            Err,
        )
        assert isinstance(
            await transport.upload("http://[broken", "application/octet-stream", b""), Err
        )
    finally:
        await transport.close()


@dataclass
class ScriptedDepositServer(DepositServer):
    status_reply: Result[Document, CloudError] | None = None
    reservation_reply: Result[Document, CloudError] | None = None
    upload_reply: Result[Document, CloudError] | None = None

    @override
    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        return (
            self.status_reply if self.status_reply is not None else await super().get(path, params)
        )

    @override
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        if path.endswith("upload-url") and self.reservation_reply is not None:
            return self.reservation_reply
        return await super().post(path, body)

    @override
    async def upload(
        self, url: str, content_type: str, content: bytes
    ) -> Result[Document, CloudError]:
        return (
            self.upload_reply
            if self.upload_reply is not None
            else await super().upload(url, content_type, content)
        )


@pytest.mark.parametrize(
    ("stage", "reply"),
    [
        ("status", Ok[Document]({})),
        (
            "status",
            Ok[Document]({"record": "partial", "expectedCount": 0, "files": [], "deleted": True}),
        ),
        ("reservation", Err(Unreachable("offline"))),
        ("reservation", Ok[Document]({})),
        ("upload", Err(Unreachable("offline"))),
        ("upload", Ok[Document]({})),
        ("upload", Ok({"storageId": ""})),
    ],
)
async def test_invalid_or_failed_server_exchange_never_confirms_and_can_retry(
    tmp_path: Path,
    stage: Literal["status", "reservation", "upload"],
    reply: Result[Document, CloudError],
) -> None:
    record = ready_record(tmp_path / "records")
    server = ScriptedDepositServer(
        status_reply=reply if stage == "status" else None,
        reservation_reply=reply if stage == "reservation" else None,
        upload_reply=reply if stage == "upload" else None,
    )
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert confirmed_deposit(record) is None
    assert server.files == {}
    server.status_reply = server.reservation_reply = server.upload_reply = None
    for _ in range(5):
        await deposit.step()
    assert confirmed_deposit(record) is not None
    await deposit.close()


async def test_rejected_blob_is_replaced_instead_of_reusing_the_deleted_id(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert isinstance(
        store(record, replace(cursor, record_uploads={"manifest.json": "deleted-id"})), Ok
    )
    server = ScriptedDepositServer(
        status_reply=Ok(
            {
                "record": "rejected",
                "expectedCount": 0,
                "files": [
                    {
                        "path": "manifest.json",
                        "sha256": "bad",
                        "verified": False,
                        "rejection": "sha256_mismatch",
                    }
                ],
            }
        )
    )
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert server.files["manifest.json"][0] == "storage-1"
    assert server.uploads_sent == 1
    await deposit.close()


async def test_saved_upload_receipt_is_committed_again_without_sending_file(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    cursor = load(record)
    assert isinstance(cursor, Cursor)
    assert isinstance(
        store(record, replace(cursor, record_uploads={"manifest.json": "old-id"})), Ok
    )
    server = ScriptedDepositServer(uploads={"old-id": (record / "manifest.json").read_bytes()})
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert server.files["manifest.json"][0] == "old-id"
    assert server.uploads_sent == 0
    server.status_reply = Ok({"record": "partial", "expectedCount": 0, "files": []})
    server.reservation_reply = Ok({"storageId": "old-id", "verified": False})
    await deposit.step()
    assert server.uploads_sent == 0
    await deposit.close()


async def test_disk_failure_after_upload_never_claims_a_confirmed_deposit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = ready_record(tmp_path / "records")
    server = DepositServer()

    def cannot_save(_record: Path, _cursor: Cursor) -> Result[None, CursorError]:
        return Err(CursorError("disk full"))

    monkeypatch.setattr(deposit_files, "store", cannot_save)
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert server.uploads_sent == 1
    assert server.files == {}
    assert confirmed_deposit(record) is None
    await deposit.close()


async def test_changed_local_bytes_are_not_uploaded_or_confirmed(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert found.value is not None
    current = found.value
    verified = {
        name: hashlib.sha256((record / name).read_bytes()).hexdigest() for name in current.files
    }
    altered = dict(verified)
    altered[current.files[0]] = "a" * 64
    assert isinstance(confirm(current, altered, UnixMillis(0)), Err)
    (record / "ticks.csv").write_text("altered")
    assert isinstance(confirm(current, verified, UnixMillis(0)), Err)
    server = DepositServer()
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    await deposit.step()
    assert "ticks.csv" not in server.files
    assert confirmed_deposit(record) is None
    await deposit.close()


@pytest.mark.parametrize(
    "files", [None, (), ("a.csv",) * 20_001, ("../outside.csv",), ("a.csv", "a.csv")]
)
def test_legacy_or_invalid_file_inventory_keeps_the_local_record(
    tmp_path: Path,
    files: tuple[str, ...] | None,
) -> None:
    record = ready_record(tmp_path / "records")
    manifest = MANIFEST.validate_json((record / "manifest.json").read_bytes())
    (record / "manifest.json").write_bytes(MANIFEST.dump_json(replace(manifest, files=files)))
    result = next_record(record.parent)
    assert isinstance(result, Err) or result == Ok(None)
    assert confirmed_deposit(record) is None


async def test_completed_background_step_can_be_scheduled_again(tmp_path: Path) -> None:
    finished = asyncio.Event()

    class SignalledDeposit(RecordDeposit):
        @override
        async def step(self) -> None:
            await super().step()
            finished.set()

    server = DepositServer()
    clock = ManualClock()
    deposit = SignalledDeposit(tmp_path, clock, server, server.upload)
    deposit.pause()
    deposit.schedule()
    await asyncio.wait_for(finished.wait(), 2)
    clock.advance(Seconds(3))
    deposit.schedule()
    await deposit.close()


def test_raw_block_limit_refuses_dimensions_before_allocating_payload() -> None:
    block = RawBlock(Header(seq=0, t_first=0, n_samples=MAX_RAW_BLOCK_BYTES, channels=("ECG",)), ())
    with pytest.raises(ValueError, match="upload-safe"):
        encode_block(block)


async def test_many_files_are_sent_in_bounded_batches_without_losing_cursor_receipts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    created = Writer.create(root, make_manifest())
    assert isinstance(created, Ok)
    writer = created.value
    for seq in range(70):
        assert isinstance(
            writer.raw(
                RawBlock(
                    Header(seq=seq, t_first=float(seq), n_samples=1, channels=("ECG",)), ((42,),)
                )
            ),
            Ok,
        )
    assert isinstance(writer.close(ManualClock(), "operator_stop"), Ok)
    assert isinstance(
        store(
            writer.path,
            Cursor(
                local_ref=writer.manifest.local_ref,
                session_id="session-1",
                end="sent",
                state="complete",
            ),
        ),
        Ok,
    )
    server = DepositServer()
    deposit = RecordDeposit(root, ManualClock(), server, server.upload)
    await deposit.step()
    assert server.uploads_sent == 1
    await deposit.step()
    assert server.uploads_sent == 65
    for _ in range(3):
        await deposit.step()
    assert confirmed_deposit(writer.path) is not None
    cursor = load(writer.path)
    assert isinstance(cursor, Cursor)
    assert not cursor.record_uploads
    await deposit.close()


@dataclass
class PagedDepositServer(ScriptedDepositServer):
    pages: dict[str, Result[Document, CloudError]] = field(
        default_factory=dict[str, Result[Document, CloudError]]
    )

    @override
    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        if params is not None and "cursor" in params:
            return self.pages[params["cursor"]]
        return await super().get(path, params)


@pytest.mark.parametrize(
    "outcome",
    ["complete", "offline", "malformed", "deleted", "partial", "repeat", "missing", "endless"],
)
async def test_confirmation_requires_every_verified_page_and_stops_on_invalid_pagination(
    tmp_path: Path,
    outcome: str,
) -> None:
    record = ready_record(tmp_path / "records")
    found = next_record(record.parent)
    assert isinstance(found, Ok)
    assert found.value is not None
    names = found.value.files
    rows: list[dict[str, object]] = [
        {
            "path": name,
            "sha256": hashlib.sha256((record / name).read_bytes()).hexdigest(),
            "verified": True,
        }
        for name in names
    ]
    base: dict[str, object] = {
        "record": "complete",
        "expectedCount": len(names),
        "files": rows[1:],
        "cursor": None,
    }
    page: Result[Document, CloudError] = Ok(base)
    if outcome == "offline":
        page = Err(Unreachable("offline"))
    elif outcome == "malformed":
        page = Ok[Document]({})
    elif outcome == "deleted":
        page = Ok({**base, "deleted": True})
    elif outcome == "partial":
        page = Ok({**base, "record": "partial"})
    elif outcome == "repeat":
        page = Ok({**base, "cursor": "page-0"})
    elif outcome == "missing":
        page = Ok({**base, "files": []})
    server = PagedDepositServer(
        status_reply=Ok({**base, "files": rows[:1], "cursor": "page-0"}), pages={"page-0": page}
    )
    if outcome == "endless":
        server.pages = {
            f"page-{index}": Ok({**base, "cursor": f"page-{index + 1}"}) for index in range(80)
        }
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert (confirmed_deposit(record) is not None) == (outcome == "complete")
    assert server.uploads_sent == 0
    await deposit.close()


async def test_receipt_pruning_failure_keeps_the_record_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = ready_record(tmp_path / "records")
    server = DepositServer()
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()

    def cannot_save(_path: Path, _cursor: Cursor) -> Result[None, CursorError]:
        return Err(CursorError("disk full"))

    monkeypatch.setattr(deposit_files, "store", cannot_save)
    await deposit.step()
    assert server.uploads_sent == 1
    assert confirmed_deposit(record) is None
    await deposit.close()


async def test_server_verified_receipt_fills_progress_when_older_files_are_on_another_page(
    tmp_path: Path,
) -> None:
    record = ready_record(tmp_path / "records")
    server = ScriptedDepositServer(
        reservation_reply=Ok({"storageId": "already-verified", "verified": True})
    )
    deposit = RecordDeposit(record.parent, ManualClock(), server, server.upload)
    await deposit.step()
    assert server.uploads_sent == 0
    assert confirmed_deposit(record) is None
    await deposit.close()


async def test_cloud_session_start_cancels_deposit_without_waiting_for_network(
    tmp_path: Path,
) -> None:
    record = ready_record(tmp_path / "records")
    setup = rig(tmp_path)
    server = DepositServer(hold_upload=asyncio.Event())
    deposit = RecordDeposit(record.parent, setup.clock, server, server.upload)
    sync = CloudSync(
        clock=setup.clock,
        transport=setup.dashboard,
        runtime=setup.runtime,
        surface=setup.panel.surface,
        store=setup.panel.services.store,
        tiers=config_of().tiers,
        programs_enabled=False,
        deposit=deposit,
    )
    await sync.step()
    await asyncio.wait_for(server.uploading.wait(), 2)
    sync.session_started(manual(setup.clock))
    setup.runtime.state = RuntimeState.RUNNING
    await sync.step()
    assert server.uploads_sent == 0
    assert confirmed_deposit(record) is None
    await sync.close()


async def test_cancelled_background_step_can_be_scheduled_again(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    held = asyncio.Event()

    class HeldDeposit(RecordDeposit):
        @override
        async def step(self) -> None:
            entered.set()
            try:
                await held.wait()
            finally:
                cancelled.set()

    server = DepositServer()
    deposit = HeldDeposit(record.parent, ManualClock(), server, server.upload)
    deposit.schedule()
    await asyncio.wait_for(entered.wait(), 2)
    deposit.pause()
    await asyncio.wait_for(cancelled.wait(), 2)
    entered.clear()
    deposit.schedule()
    await asyncio.wait_for(entered.wait(), 2)
    await deposit.close()
    assert confirmed_deposit(record) is None


def test_corrupted_cursor_cannot_acknowledge_verified_receipts(tmp_path: Path) -> None:
    record = ready_record(tmp_path / "records")
    selected = next_record(record.parent)
    assert isinstance(selected, Ok)
    assert selected.value is not None
    record.with_name(record.name + ".sync.json").write_text("invalid", encoding="utf-8")
    assert isinstance(deposit_files.acknowledge(selected.value, frozenset()), Err)
    assert confirmed_deposit(record) is None


async def test_recording_console_owns_and_releases_the_deposit_worker(tmp_path: Path) -> None:
    clock = ManualClock()
    journal = Journal(tmp_path / "records", clock, retention_days=7)
    panel = build_panel(
        config_of(), clock=clock, profiles_path=tmp_path / "profiles.json", journal=journal
    )
    assert panel.cloud is not None
    await panel.close()


async def test_custom_storage_origin_and_malformed_upload_url() -> None:
    seen: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"storageId": "empty-file"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    transport = HttpxTransport(CloudConfig(url="https://custom.test", api_key="key"), client=client)
    try:
        assert isinstance(await transport.upload("http://[invalid", "text/plain", b""), Err)
        assert not seen
        assert await transport.upload("https://custom.test/upload", "text/plain", b"") == Ok(
            {"storageId": "empty-file"}
        )
        assert seen[0].content == b""
    finally:
        await transport.close()
