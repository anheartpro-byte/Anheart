from __future__ import annotations

import asyncio
import contextlib
import functools
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from src.clock import Clock
from src.record.deposit_files import (
    DepositRecord,
    FilePayload,
    acknowledge,
    confirm,
    next_record,
    payload,
    remember,
    revoke,
)
from src.record.export import RecordIo
from src.record.rows import JsonValue
from src.record.schema import RecordError
from src.result import Err, Ok, Result
from src.units import Monotonic, Seconds

if TYPE_CHECKING:
    from src.cloud_sync import CloudError, CloudTransport, Document

IO_TIMEOUT: Final[Seconds] = Seconds(30)
MAX_BACKOFF: Final[Seconds] = Seconds(300)
MIN_BACKOFF: Final[Seconds] = Seconds(2)
SUCCESS_DELAY: Final[Seconds] = Seconds(0.25)
FILES_PER_STEP: Final[int] = 64
UPLOAD_CONCURRENCY: Final[int] = 4
MAX_STATUS_PAGES: Final[int] = 80


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="ignore"))
class RemoteFile:
    path: str
    sha256: str
    verified: bool
    rejection: str | None = None


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="ignore"))
class RemoteStatus:
    record: Literal["none", "partial", "complete", "rejected"]
    expected_count: Annotated[int, Field(alias="expectedCount")]
    files: tuple[RemoteFile, ...]
    deleted: bool = False
    cursor: str | None = None


REMOTE_STATUS: Final[TypeAdapter[RemoteStatus]] = TypeAdapter(RemoteStatus)


class RecordDeposit:
    def __init__(
        self,
        root: Path,
        clock: Clock,
        transport: CloudTransport,
        upload: Callable[[str, str, bytes], Awaitable[Result[Document, CloudError]]],
    ) -> None:
        self._root: Path = root
        self._clock: Clock = clock
        self._transport: CloudTransport = transport
        self._upload: Callable[[str, str, bytes], Awaitable[Result[Document, CloudError]]] = upload
        self._io: RecordIo = RecordIo(1)
        self._disk: asyncio.Lock = asyncio.Lock()
        self._network: asyncio.Semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        self._active: asyncio.Task[None] | None = None
        self._next: Monotonic = clock.monotonic()
        self._backoff: Seconds = MIN_BACKOFF
        self._record_path: Path | None = None
        self._after: Path | None = None
        self._verified: dict[str, str] = {}

    def pause(self) -> None:
        if self._active is not None:
            self._active.cancel()

    def schedule(self) -> None:
        if self._active is not None:
            if not self._active.done():
                return
            previous = self._active
            self._active = None
            if not previous.cancelled():
                previous.result()
        if self._clock.monotonic() >= self._next:
            self._active = asyncio.create_task(self.step())

    async def close(self) -> None:
        task = self._active
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(task)
            self._active = None

    async def step(self) -> None:
        succeeded = await self._attempt()
        if not succeeded:
            self._after = self._record_path
        self._backoff = MIN_BACKOFF if succeeded else Seconds(min(MAX_BACKOFF, self._backoff * 2))
        delay = SUCCESS_DELAY if succeeded else self._backoff
        self._next = Monotonic(self._clock.monotonic() + delay)

    async def _work[T](self, work: Callable[[], T]) -> Result[T, RecordError]:
        async with self._disk:
            return await self._io.run(work, IO_TIMEOUT)

    async def _attempt(self) -> bool:
        listed = await self._work(functools.partial(next_record, self._root, self._after))
        if isinstance(listed, Err) or isinstance(listed.value, Err):
            return False
        record = listed.value.value
        if record is None:
            return True
        if self._record_path != record.path:
            self._record_path = record.path
            self._verified = {}
        status = await self._transport.get(
            "/api/machine/records/status", {"sessionId": record.session_id}
        )
        if isinstance(status, Err):
            return False
        try:
            remote = REMOTE_STATUS.validate_python(status.value)
        except ValidationError:
            return False
        return await self._reported(record, remote)

    async def _reported(self, record: DepositRecord, remote: RemoteStatus) -> bool:
        if remote.deleted:
            await self._work(functools.partial(revoke, record))
            return False
        self._verified.update({file.path: file.sha256 for file in remote.files if file.verified})
        verified = self._verified
        acknowledged = await self._work(functools.partial(acknowledge, record, frozenset(verified)))
        if isinstance(acknowledged, Err) or isinstance(acknowledged.value, Err):
            return False
        if remote.record == "complete":
            return await self._complete(record, remote)
        files = record.files
        ordered = ("manifest.json", *(name for name in files if name != "manifest.json"))
        pending: list[tuple[str, bool]] = []
        for name in ordered:
            if name in verified:
                continue
            if name != "manifest.json" and "manifest.json" not in verified:
                break
            rejected = any(
                file.path == name and file.rejection is not None for file in remote.files
            )
            pending.append((name, rejected))
            if len(pending) >= FILES_PER_STEP:
                break
        return all(
            await asyncio.gather(
                *(self._send_bounded(record, name, rejected) for name, rejected in pending)
            )
        )

    async def _complete(self, record: DepositRecord, remote: RemoteStatus) -> bool:
        cursor = remote.cursor
        seen: set[str] = set()
        for _ in range(MAX_STATUS_PAGES):
            if cursor is None:
                break
            if cursor in seen:
                return False
            seen.add(cursor)
            answer = await self._transport.get(
                "/api/machine/records/status", {"sessionId": record.session_id, "cursor": cursor}
            )
            if isinstance(answer, Err):
                return False
            try:
                page = REMOTE_STATUS.validate_python(answer.value)
            except ValidationError:
                return False
            if page.deleted or page.record != "complete":
                return False
            self._verified.update({file.path: file.sha256 for file in page.files if file.verified})
            cursor = page.cursor
        if cursor is not None or len(self._verified) != remote.expected_count:
            return False
        checked = await self._work(
            functools.partial(confirm, record, self._verified, self._clock.unix_millis())
        )
        return isinstance(checked, Ok) and isinstance(checked.value, Ok)

    async def _send_bounded(self, record: DepositRecord, name: str, rejected: bool) -> bool:
        async with self._network:
            return await self._send(record, name, rejected)

    async def _send(self, record: DepositRecord, name: str, rejected: bool) -> bool:
        read = await self._work(functools.partial(payload, record, name))
        if isinstance(read, Err) or isinstance(read.value, Err):
            return False
        file = read.value.value
        metadata: dict[str, JsonValue] = {
            "sessionId": record.session_id,
            "path": name,
            "sha256": file.sha256,
            "size": file.size,
        }
        reserved = await self._transport.post("/api/machine/records/upload-url", metadata)
        if isinstance(reserved, Err):
            return False
        storage_id = reserved.value.get("storageId")
        if isinstance(storage_id, str) and reserved.value.get("verified") is True:
            self._verified[name] = file.sha256
            return True
        if not isinstance(storage_id, str):
            storage_id = (
                None if rejected or reserved.value.get("rejected") is True else file.storage_id
            )
        if storage_id is None:
            storage_id = await self._uploaded(record, file, reserved.value)
        if storage_id is None:
            return False
        committed = await self._transport.post(
            "/api/machine/records/commit",
            {
                **metadata,
                "storageId": storage_id,
            },
        )
        return isinstance(committed, Ok)

    async def _uploaded(
        self,
        record: DepositRecord,
        file: FilePayload,
        reservation: Mapping[str, object],
    ) -> str | None:
        url = reservation.get("uploadUrl")
        content_type = reservation.get("contentType")
        if not isinstance(url, str) or not isinstance(content_type, str):
            return None
        uploaded = await self._upload(url, content_type, file.content)
        if isinstance(uploaded, Err):
            return None
        storage_id = uploaded.value.get("storageId")
        if not isinstance(storage_id, str) or not storage_id:
            return None
        saved = await self._work(
            functools.partial(remember, record, file.path, storage_id),
        )
        return storage_id if isinstance(saved, Ok) and isinstance(saved.value, Ok) else None
