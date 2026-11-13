"""The catalogue's writes to the profile store (ANH-138): off the loop, whole, one at a time."""

import asyncio
import os
import threading
from pathlib import Path

import pytest

from src.result import Ok
from src.training.plan import CatalogStamp, parse_profile, parse_store
from src.web.profile_writer import ProfileWriter
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig
from tests.test_web_api import profile_document


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_storage_wait_keeps_motor_responsive_and_serializes_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_first: bool,
) -> None:
    # Given a running motor and storage paused before its atomic commit.
    rig, _ = make_rig(tmp_path)
    await cruising(rig)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    threads: list[int] = []
    fsync = os.fsync

    def blocked_fsync(fd: int) -> None:
        threads.append(threading.get_ident())
        loop.call_soon_threadsafe(entered.set)
        if threads[-1] != loop_thread and not release.wait(5):
            raise TimeoutError("test did not release the storage worker")
        fsync(fd)

    monkeypatch.setattr(os, "fsync", blocked_fsync)
    store = rig.panel.services.store
    writer = ProfileWriter(store)

    def at_rest() -> bool:
        return True

    initial = (store.rev, store.list_profiles(), store.catalog)
    parsed = parse_profile(profile_document("bench_short"))
    assert isinstance(parsed, Ok)
    first = asyncio.create_task(
        writer.replace_catalog(
            (parsed.value,), CatalogStamp(revision=7, medical="0.1.0"), may_adopt=at_rest
        )
    )
    last = CatalogStamp(revision=8, medical="0.1.0")
    second = None
    try:
        # When persistence is blocked, ticks still run on the loop, and the
        # store in memory is the one before the write.
        await asyncio.wait_for(entered.wait(), 2)
        assert threads[0] != loop_thread
        await asyncio.wait_for(rig.tick(0.2), 2)
        assert rig.simulator.commanded_setpoint > 0
        assert (store.rev, store.list_profiles(), store.catalog) == initial
        if cancel_first:
            first.cancel()
        second = asyncio.create_task(writer.replace_catalog((), last, may_adopt=at_rest))
        release.set()
        if cancel_first:
            with pytest.raises(asyncio.CancelledError):
                _ = await first
        else:
            landed = await first
            assert landed == Ok(True)
        landed = await second
        assert landed == Ok(True)

        # Then both writes landed whole, one after the other, a cancelled one too.
        assert len(threads) == 2
        assert store.rev == initial[0] + 2
        assert store.catalog == last
        assert store.list_profiles() == ()
        on_disk = parse_store((tmp_path / "profiles.json").read_text(encoding="utf-8"))
        assert isinstance(on_disk, Ok)
        assert on_disk.value.catalog == last
        assert on_disk.value.profiles == ()
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await rig.panel.close()
