"""Serialized profile writes: disk work off-loop, cache adoption on-loop.

Only the signed programme catalogue mutates this store during operation
(:mod:`src.programme_catalog`, ANH-138): the console's own profile editor is
gone. Its native asyncio tasks must finish a durable write before
cancellation releases the writer lock. The staging copy shares read-only
configuration/cache until ProfileStore replaces its cache after a successful
write; the live store is never mutated off-thread.
"""

import asyncio
from collections.abc import Sequence
from copy import copy
from functools import partial

import anyio

from src.result import Result, is_ok
from src.task_completion import complete_owned
from src.training.plan import (
    CatalogStamp,
    ProfileStore,
    StoreContent,
    StoreRev,
    StoreUnwritable,
    TrainingProfile,
)


class ProfileWriter:
    """One mutable writer lock for every writer of a live profile store."""

    def __init__(self, store: ProfileStore) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def replace_catalog(
        self, profiles: Sequence[TrainingProfile], catalog: CatalogStamp
    ) -> Result[StoreRev, StoreUnwritable]:
        """:meth:`~src.training.plan.ProfileStore.replace_catalog`, written off the loop."""
        async with self._lock:
            staged = copy(self._store)
            operation = partial(staged.replace_catalog, profiles, catalog)
            return await complete_owned(asyncio.create_task(self._commit(staged, operation)))

    async def _commit(
        self,
        staged: ProfileStore,
        operation: partial[Result[StoreRev, StoreUnwritable]],
    ) -> Result[StoreRev, StoreUnwritable]:
        result = await anyio.to_thread.run_sync(operation)
        if is_ok(result):
            self._store.adopt(
                StoreContent(
                    rev=result.value, profiles=staged.list_profiles(), catalog=staged.catalog
                )
            )
        return result
