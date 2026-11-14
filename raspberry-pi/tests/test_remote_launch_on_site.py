"""ANH-85: without a confirmation at the machine, no launch from the dashboard moves the motor.

The REAL console (:func:`src.local_panel.build_panel`, ``MOTOR_BACKEND=sim``), its
runtime and its simulated drive, linked to a scripted dashboard that offers a
launch nobody confirms at the console. Every command and every speed written to
the drive is observed (:func:`~tests.test_cloud_sync.observe_dashboard_and_drive`):
none is, while the launch is offered, once the dashboard no longer offers it (it
expired there, ``expired_before_start``), nor after the console restarts with
the launch still offered. A confirmation that reaches the dashboard after the
expiry is answered 409 and moves nothing either
(``test_a_409_never_commands_the_simulated_drive``).

The two waits compose: the wait for the confirmation is the dashboard's
(``PENDING_SESSION_TTL_S``, counted from the launch on the site); the console's
:data:`~src.cloud_sync.LAUNCH_TIMEOUT` for the loop to arm runs only from the
confirmation, so a launch confirmed late in its wait still arms at once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from src.remote_launch import ConfirmationCode, LaunchStage
from src.result import Err, Ok
from src.training.runtime import RuntimeState
from src.units import Seconds
from tests.test_cloud_sync import (
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    Linked,
    confirm_linked,
    launch_answer,
    linked,
    observe_dashboard_and_drive,
    state_of,
)

END: Final[str] = "/api/machine/training/end"

WAITED: Final[Seconds] = Seconds(130.0)
"""Longer than the ticket's delay (120 s) and than the console's own hold of a start
left unanswered (60 s): nothing on the console starts a launch by waiting."""


def offered(r: Linked) -> LaunchStage | None:
    cloud = r.panel.cloud
    assert cloud is not None
    offer = cloud.current_remote_launch()
    return None if offer is None else offer.stage


def assert_nothing_moved(r: Linked, writes: list[str]) -> None:
    assert writes == []
    assert r.dashboard.to(START) == []
    assert r.dashboard.to(END) == []
    assert r.panel.surface.pending is None
    assert state_of(r.panel) is RuntimeState.IDLE
    assert r.panel.runtime.snapshot().measured.motor_rpm == 0


async def test_an_unconfirmed_launch_waits_then_expires_without_a_drive_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = observe_dashboard_and_drive(monkeypatch, [])
    r = linked(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(WAITED)
    assert offered(r) is LaunchStage.WAITING_CONFIRMATION
    assert_nothing_moved(r, writes)

    # It expires on the dashboard, which no longer offers it and fails it itself.
    r.dashboard.answer(POLL_PATH, launch_answer(None))
    await r.run(5.0)
    assert offered(r) is None
    cloud = r.panel.cloud
    assert cloud is not None
    late = cloud.confirm_remote_launch("remote-1", OPERATOR)
    assert isinstance(late, Err)
    assert late.error.code is ConfirmationCode.NOT_PENDING
    await r.run(5.0)
    assert_nothing_moved(r, writes)
    await r.panel.close()


async def test_a_restarted_console_needs_a_new_confirmation_and_moves_nothing_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = observe_dashboard_and_drive(monkeypatch, [])
    before = linked(tmp_path / "before")
    before.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await before.run(1.0)
    cloud = before.panel.cloud
    assert cloud is not None
    # Confirmed, then the console stops before its link sends the start.
    assert isinstance(cloud.confirm_remote_launch("remote-1", OPERATOR), Ok)
    assert_nothing_moved(before, writes)
    await before.panel.close()

    # The same launch offered to the console started again: the click did not survive.
    after = linked(tmp_path / "after")
    after.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await after.run(WAITED)
    assert offered(after) is LaunchStage.WAITING_CONFIRMATION
    assert_nothing_moved(after, writes)
    after.dashboard.answer(POLL_PATH, launch_answer(None))
    await after.run(5.0)
    assert offered(after) is None
    assert_nothing_moved(after, writes)
    await after.panel.close()


async def test_a_launch_confirmed_late_in_its_wait_arms_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    writes = observe_dashboard_and_drive(monkeypatch, order)
    r = linked(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.run(WAITED)
    assert_nothing_moved(r, writes)
    confirm_linked(r.panel)
    await r.run(2.0)
    assert state_of(r.panel) is RuntimeState.RUNNING
    assert len(r.dashboard.to(START)) == 1
    assert order.index("training/start") < order.index("drive")
    assert r.dashboard.to(END) == []
    await r.panel.close()
