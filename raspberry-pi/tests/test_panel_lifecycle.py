"""A run owns every child and stops the drive before waiting for observers."""

import asyncio
from pathlib import Path

import pytest

from src.local_panel import EXIT_FAILED, EXIT_OK, LocalPanel
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig
from tests.test_local_panel import FakeWeb


@pytest.mark.parametrize("ending", ["cancel", "stop", "web_failure"])
async def test_run_stops_motor_and_children_when_observer_is_hung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    # Given a real running motor and an observer blocked on external work.
    rig, _ = make_rig(tmp_path)
    await cruising(rig)
    assert rig.simulator.commanded_setpoint > 0
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_sensor(_panel: LocalPanel) -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(LocalPanel, "sensor_step", blocked_sensor)
    before = asyncio.all_tasks()
    stop = asyncio.Event()
    web = FakeWeb(fail_at_start=ending == "web_failure")
    runner = asyncio.create_task(rig.panel.run(stop, web))
    await asyncio.wait_for(entered.wait(), 2)
    children = asyncio.all_tasks() - before - {runner}
    try:
        # When the run ends, even though the observer never supplies its result.
        if ending == "cancel":
            runner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(runner, 2)
        else:
            if ending == "stop":
                stop.set()
            assert await asyncio.wait_for(runner, 2) == (
                EXIT_FAILED if ending == "web_failure" else EXIT_OK
            )

        # Then no child can write again and the motor has received its zero.
        assert stop.is_set()
        assert web.exits == 1
        assert cleaned.is_set()
        assert all(child.done() for child in children)
        assert rig.simulator.commanded_setpoint == 0
        assert not rig.panel.ecg.status().acquiring
    finally:
        stop.set()
        web.request_exit()
        release.set()
        await asyncio.gather(runner, *children, return_exceptions=True)
        await rig.panel.close()
