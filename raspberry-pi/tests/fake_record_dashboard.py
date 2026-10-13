from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

from src.cloud_sync import CloudError, Document
from src.record.retention import confirmed_deposit, records
from src.record.rows import JsonValue
from src.record.writer import MANIFEST
from src.result import Ok, Result
from tests.fake_dashboard import FakeDashboard


@dataclass
class RecordingDashboard(FakeDashboard):
    expected_files: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    record_files: dict[tuple[str, str], tuple[str, str, int]] = field(
        default_factory=dict[tuple[str, str], tuple[str, str, int]]
    )
    reservations: dict[str, tuple[str, str, str, int]] = field(
        default_factory=dict[str, tuple[str, str, str, int]]
    )
    received: dict[str, tuple[str, int, tuple[str, ...] | None]] = field(
        default_factory=dict[str, tuple[str, int, tuple[str, ...] | None]]
    )
    receipt_count: int = 0
    health_reports: int = 0
    last_heartbeat_ms: int | None = None
    max_heartbeat_gap_ms: int = 0

    @override
    async def get(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> Result[Document, CloudError]:
        if path != "/api/machine/records/status" or not self.online:
            return await super().get(path, params)
        assert params is not None
        session_id = params["sessionId"]
        rows = [
            {"path": name, "sha256": file[1], "verified": True}
            for (session, name), file in self.record_files.items()
            if session == session_id
        ]
        expected = self.expected_files.get(session_id, ())
        return Ok(
            {
                "record": "complete" if expected and len(rows) == len(expected) else "partial",
                "files": rows,
                "expectedCount": len(expected),
                "deleted": False,
            }
        )

    @override
    async def post(self, path: str, body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        if not path.startswith("/api/machine/records/") or not self.online:
            if self.online and path.endswith("/heartbeat") and isinstance(body.get("health"), dict):
                self.health_reports += 1
                now = self.now()
                if self.last_heartbeat_ms is not None:
                    self.max_heartbeat_gap_ms = max(
                        self.max_heartbeat_gap_ms, now - self.last_heartbeat_ms
                    )
                self.last_heartbeat_ms = now
            return await super().post(path, body)
        session_id, name, digest, size = (
            body["sessionId"],
            body["path"],
            body["sha256"],
            body["size"],
        )
        assert isinstance(session_id, str)
        assert isinstance(name, str)
        assert isinstance(digest, str)
        assert isinstance(size, int)
        assert self.sessions[session_id].status != "active"
        if path.endswith("/upload-url"):
            previous = self.record_files.get((session_id, name))
            if previous is not None:
                return Ok({"storageId": previous[0], "verified": True})
            assert name == "manifest.json" or name in self.expected_files[session_id]
            self.receipt_count += 1
            receipt = f"receipt-{self.receipt_count}"
            self.reservations[receipt] = (session_id, name, digest, size)
            return Ok(
                {
                    "uploadUrl": f"https://storage.test/{receipt}",
                    "contentType": "application/octet-stream",
                }
            )
        committed_id = body["storageId"]
        assert isinstance(committed_id, str)
        received = self.received[committed_id]
        assert (received[0], received[1]) == (digest, size)
        self.record_files[(session_id, name)] = (committed_id, digest, size)
        if received[2] is not None:
            self.expected_files[session_id] = received[2]
        return Ok({"accepted": True})

    async def upload(
        self, url: str, _content_type: str, content: bytes
    ) -> Result[Document, CloudError]:
        receipt = url.rsplit("/", 1)[1]
        session_id, name, digest, size = self.reservations[receipt]
        assert self.sessions[session_id].status != "active"
        actual = hashlib.sha256(content).hexdigest()
        assert (actual, len(content)) == (digest, size)
        manifest = MANIFEST.validate_json(content) if name == "manifest.json" else None
        self.received[receipt] = (
            actual,
            len(content),
            None if manifest is None else manifest.files,
        )
        return Ok({"storageId": receipt})

    def forget_confirmed(self, root: Path) -> None:
        paths = records(root)
        for session_id in list(self.expected_files):
            local_ref = self.sessions[session_id].local_ref
            path = next((path for path in paths if path.name.endswith("_" + local_ref)), None)
            if path is not None and confirmed_deposit(path) is None:
                continue
            self.expected_files.pop(session_id)
            for key in [key for key in self.record_files if key[0] == session_id]:
                del self.record_files[key]
            for receipt in [
                receipt for receipt, held in self.reservations.items() if held[0] == session_id
            ]:
                del self.reservations[receipt]
                self.received.pop(receipt, None)

    def confirmed_or_purged(self, path: Path, session_id: str) -> bool:
        if confirmed_deposit(path) is not None:
            return True
        expected = self.expected_files.get(session_id, ())
        return (
            bool(expected)
            and not path.exists()
            and all((session_id, name) in self.record_files for name in expected)
        )
