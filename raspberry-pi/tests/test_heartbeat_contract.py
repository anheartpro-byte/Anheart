from pathlib import Path

import pytest

from src.cloud_sync import HEARTBEAT_PERIOD, STATUS_PERIOD, Refused
from src.contract import CONTRACT_VERSION, SERVER_VERSION_FIELD, ErrorCode
from src.link_state import LinkState
from src.result import Err
from src.training.runtime import RuntimeState
from tests.test_cloud_contract import HEARTBEAT, STATUS
from tests.test_cloud_sync import DOWN, manual, ok, rig


@pytest.mark.parametrize("programs", [True, False])
async def test_empty_heartbeat_cannot_be_made_reachable_by_other_successes(
    tmp_path: Path, programs: bool
) -> None:
    r = rig(tmp_path, programs=programs)
    r.dashboard.answer(HEARTBEAT, ok())

    await r.step()

    assert r.sync.link.state is not LinkState.REACHABLE


async def test_heartbeat_failure_stays_visible_while_polling_succeeds(tmp_path: Path) -> None:
    r = rig(tmp_path)
    await r.step()
    r.dashboard.answer(HEARTBEAT, DOWN)

    await r.step(HEARTBEAT_PERIOD)

    assert r.sync.link.detail


async def test_heartbeat_recovery_clears_its_failure(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(HEARTBEAT, DOWN)
    await r.step()
    r.dashboard.answer(HEARTBEAT, ok({SERVER_VERSION_FIELD: CONTRACT_VERSION}))

    await r.step(HEARTBEAT_PERIOD)

    assert r.sync.link.state is LinkState.REACHABLE
    assert r.sync.link.detail == ""


async def test_stop_question_401_does_not_declare_the_machine_key_refused(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.sync.session_started(manual(r.clock, remote="cloud-1"))
    r.runtime.state = RuntimeState.RUNNING
    await r.step()
    r.dashboard.answer(STATUS, Err(Refused(401, "refused", ErrorCode("unauthorized"))))

    await r.step(STATUS_PERIOD)

    assert r.sync.link.state is LinkState.REACHABLE
