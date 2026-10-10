import asyncio
import os
import threading
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from src.web.schemas import ProfileListRow
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig
from tests.test_web_api import profile_document


@pytest.mark.parametrize("delete_first", [False, True])
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_storage_wait_keeps_motor_responsive_and_serializes_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_first: bool,
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
    async with rig.http() as client:
        parser = TypeAdapter(ProfileListRow)
        initial = parser.validate_json((await client.get("/api/profiles")).text)
        profile_id = initial.profiles[0].profile_id
        url = f"/api/profiles/{profile_id}?rev={initial.rev}"
        mutation = (
            client.delete(url)
            if delete_first
            else client.put(url, json=profile_document(profile_id))
        )
        first = asyncio.create_task(mutation)
        second = None
        try:
            # When persistence is blocked, ticks and reads still run on the loop.
            await asyncio.wait_for(entered.wait(), 2)
            assert threads[0] != loop_thread
            await asyncio.wait_for(rig.tick(0.2), 2)
            assert rig.simulator.commanded_setpoint > 0
            during = parser.validate_json((await client.get("/api/profiles")).text)
            assert during == initial
            if cancel_first:
                first.cancel()
            second = asyncio.create_task(client.delete(url))
            release.set()
            if cancel_first:
                with pytest.raises(asyncio.CancelledError):
                    _ = await first
            else:
                assert (await first).status_code == 200

            # Then only the first writer commits; its competitor is stale.
            assert (await second).status_code == 409
            final = parser.validate_json((await client.get("/api/profiles")).text)
            assert final.rev == initial.rev + 1
            assert len(threads) == 1
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            if second is not None:
                await asyncio.gather(second, return_exceptions=True)
            await rig.panel.close()
