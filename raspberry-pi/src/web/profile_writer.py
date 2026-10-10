"""Serialized profile writes: disk work off-loop, cache adoption on-loop.

Only the HTTP editor mutates this store during operation. Its native asyncio
tasks must finish a durable write before cancellation releases the writer lock.
The staging copy shares read-only configuration/cache until ProfileStore replaces
its cache after a successful write; the live store is never mutated off-thread.
"""

import asyncio
from collections.abc import Callable
from copy import copy
from functools import partial

import anyio

from src.result import Result, is_ok
from src.task_completion import complete_owned
from src.training.plan import (
    DeleteError,
    ProfileStore,
    StoreContent,
    StoreRev,
    TrainingProfile,
    UpsertError,
)


class ProfileWriter:
    """One mutable writer lock for all editors of a live profile store."""

    def __init__(self, store: ProfileStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def upsert(
        self, profile: TrainingProfile, expected_rev: StoreRev
    ) -> Result[StoreRev, UpsertError]:
        async with self._lock:
            staged = copy(self._store)
            operation = partial(staged.upsert, profile, expected_rev=expected_rev)
            return await complete_owned(asyncio.create_task(self._commit(staged, operation)))

    async def delete(
        self, profile_id: str, expected_rev: StoreRev
    ) -> Result[StoreRev, DeleteError]:
        async with self._lock:
            staged = copy(self._store)
            operation = partial(staged.delete, profile_id, expected_rev=expected_rev)
            return await complete_owned(asyncio.create_task(self._commit(staged, operation)))

    async def _commit[E](
        self, staged: ProfileStore, operation: Callable[[], Result[StoreRev, E]]
    ) -> Result[StoreRev, E]:
        result = await anyio.to_thread.run_sync(operation)
        if is_ok(result):
            self._store.adopt(StoreContent(rev=result.value, profiles=staged.list_profiles()))
        return result
