"""Serialized profile writes: disk work off-loop, cache adoption on-loop.

Only the signed programme catalogue mutates this store during operation
(:mod:`src.programme_catalog`, ANH-138): the console's own profile editor is
gone. Its native asyncio tasks must finish a durable write before
cancellation releases the writer lock. The staging copy shares read-only
configuration/cache until ProfileStore replaces its cache after a successful
write; the live store is never mutated off-thread.

The cache is replaced only if ``may_adopt`` still allows it when the write
has landed, on the loop: the console may have left its rest during the
write. The disk then holds the new profiles and memory the old ones, which
the next start screens again before offering anything
(:func:`~src.programme_catalog.screen_stored`).
"""

import asyncio
from collections.abc import Callable, Sequence
from copy import copy
from functools import partial

import anyio

from src.result import Err, Ok, Result
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
        self,
        profiles: Sequence[TrainingProfile],
        catalog: CatalogStamp,
        *,
        may_adopt: Callable[[], bool],
    ) -> Result[bool, StoreUnwritable]:
        """:meth:`~src.training.plan.ProfileStore.replace_catalog`, written off the loop.

        ``Ok(True)``: written and in effect. ``Ok(False)``: written, but
        ``may_adopt`` refused when the write landed, so the profiles in memory
        are still those before it.
        """
        async with self._lock:
            staged = copy(self._store)
            operation = partial(staged.replace_catalog, profiles, catalog)
            return await complete_owned(
                asyncio.create_task(self._commit(staged, operation, may_adopt))
            )

    async def _commit(
        self,
        staged: ProfileStore,
        operation: partial[Result[StoreRev, StoreUnwritable]],
        may_adopt: Callable[[], bool],
    ) -> Result[bool, StoreUnwritable]:
        result = await anyio.to_thread.run_sync(operation)
        if isinstance(result, Err):
            return result
        if not may_adopt():
            return Ok(False)
        self._store.adopt(
            StoreContent(rev=result.value, profiles=staged.list_profiles(), catalog=staged.catalog)
        )
        return Ok(True)
