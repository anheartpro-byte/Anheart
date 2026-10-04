"""Operator errors, through the REAL console's HTTP API.

Every mistake an attendant can make at the page - starting twice, stopping
nothing, resetting a fault on a turning machine, asking for a speed the machine
may not reach - must be answered with a message that says what is wrong, and
must never move the machine. The route answers first (4xx with a ``detail``),
and what the route cannot judge the loop refuses (a REFUSED event the page
shows); either way the setpoint does not change and the machine is left
stopped at the end.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx
import pytest
from pydantic import TypeAdapter

from src.training.runtime import EndReason, RuntimeState
from tests.test_failure_rig import (
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    Rig,
    attest,
    make_rig,
    start_manual,
)

CRUISE: Final[float] = 10.0

BODY: Final[TypeAdapter[Mapping[str, object]]] = TypeAdapter(Mapping[str, object])


def detail_of(response: httpx.Response) -> str:
    body = BODY.validate_json(response.text)
    detail = body.get("detail")
    if not isinstance(detail, str) or not detail.strip():
        raise AssertionError(f"no actionable detail in {response.text}")
    return detail


async def cruising(tmp_path: Path) -> Rig:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE)
    await rig.tick(45.0)
    return rig


async def finish(rig: Rig) -> None:
    async with rig.http() as session:
        await session.post("/api/session/stop", json={"operator": OPERATOR})
    await rig.tick(60.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_second_manual_start_is_refused_and_the_session_keeps_its_speed(
    tmp_path: Path,
) -> None:
    rig = await cruising(tmp_path)
    held = rig.panel.runtime.snapshot().setpoint.motor_rpm
    async with rig.http() as session:
        again = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
    assert again.status_code == httpx.codes.CONFLICT
    assert "running" in detail_of(again)
    last = await rig.tick(5.0)
    assert last.setpoint.motor_rpm == held
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    await finish(rig)


async def test_a_programme_start_during_a_manual_session_is_refused(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV)
    async with rig.http() as session:
        await start_manual(rig, session, CRUISE)
        await rig.tick(20.0)
        started = await session.post(
            "/api/session/start",
            json={"profile_id": SHORT_PROFILE, "operator": OPERATOR, "total_duration_s": None},
        )
    assert started.status_code == httpx.codes.CONFLICT
    detail_of(started)
    await rig.tick(2.0)
    assert rig.panel.runtime.snapshot().mode.value == "manuel"
    await finish(rig)


async def test_a_double_start_before_the_loop_ticks_is_refused(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await attest(session)
        first = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        second = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
    assert first.status_code == httpx.codes.ACCEPTED
    assert second.status_code == httpx.codes.CONFLICT
    assert "starting" in detail_of(second)
    await rig.tick(2.0)
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    await finish(rig)


async def test_stop_while_idle_is_refused_with_a_reason_and_writes_nothing(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    await rig.tick(2.0)
    async with rig.http() as session:
        stop = await session.post("/api/session/stop", json={"operator": OPERATOR})
    assert stop.status_code == httpx.codes.CONFLICT
    assert "no session" in detail_of(stop)
    await rig.tick(2.0)
    assert rig.panel.runtime.state is RuntimeState.IDLE
    detail = await rig.panel.close()
    assert "without a write" in detail
    assert await rig.left_stopped() == ""


async def test_an_estop_while_idle_is_accepted_and_moves_nothing(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        estop = await session.post("/api/session/estop", json={})
    assert estop.status_code == httpx.codes.OK
    await rig.tick(2.0)
    assert max(abs(s.measured.motor_rpm) for s in rig.snapshots) == 0
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_fault_reset_while_turning_is_refused_by_the_loop(tmp_path: Path) -> None:
    rig = await cruising(tmp_path)
    held = rig.panel.runtime.snapshot().setpoint.motor_rpm
    async with rig.http() as session:
        reset = await session.post("/api/drive/fault-reset", json={"operator": OPERATOR})
    assert reset.status_code == httpx.codes.ACCEPTED
    last = await rig.tick(2.0)
    refusals = rig.refusals()
    assert any("reset refuse" in refusal for refusal in refusals), refusals
    assert last.setpoint.motor_rpm == held
    await finish(rig)


async def test_a_fault_reset_on_a_coasting_faulted_drive_is_refused(tmp_path: Path) -> None:
    from src.motor.drive import DriveFault  # noqa: PLC0415  # this case only

    rig = await cruising(tmp_path)
    rig.simulator.inject_fault(DriveFault.OVERCURRENT)
    await rig.tick(3.0)
    assert abs(rig.panel.runtime.snapshot().measured.motor_rpm) > 1
    async with rig.http() as session:
        reset = await session.post("/api/drive/fault-reset", json={"operator": OPERATOR})
    await rig.tick(1.0)
    # The route may take it (202) for the loop to refuse, or refuse it itself.
    if reset.status_code == httpx.codes.ACCEPTED:
        assert any("reset refuse" in refusal for refusal in rig.refusals()), rig.refusals()
    else:
        detail_of(reset)
    assert rig.panel.runtime.end_reason is EndReason.SAFETY_VERDICT
    await rig.tick(90.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""


@pytest.mark.parametrize(
    "output_rpm",
    [40.0, 27.8, 0.5, 1.0],
    ids=["above_hsp", "just_above_nameplate", "in_the_min_run_gap", "min_run_gap_edge"],
)
async def test_a_target_out_of_range_is_refused_and_the_speed_holds(
    tmp_path: Path, output_rpm: float
) -> None:
    rig = await cruising(tmp_path)
    held = rig.panel.runtime.snapshot().setpoint.motor_rpm
    async with rig.http() as session:
        target = await session.post(
            "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
        )
    assert target.status_code == httpx.codes.ACCEPTED
    last = await rig.tick(5.0)
    refusals = rig.refusals()
    assert any("consigne refusee" in refusal for refusal in refusals), refusals
    assert last.setpoint.motor_rpm == held
    await finish(rig)


@pytest.mark.parametrize("value", [-1.0, math.inf], ids=["negative", "infinite"])
async def test_a_target_that_is_not_a_speed_is_refused_by_the_route(
    tmp_path: Path, value: float
) -> None:
    rig = await cruising(tmp_path)
    async with rig.http() as session:
        target = await session.post(
            "/api/manual/target",
            content=f'{{"output_rpm": {"1e999" if math.isinf(value) else value}, '
            f'"operator": "{OPERATOR}"}}',
            headers={"content-type": "application/json"},
        )
    assert target.status_code == httpx.codes.UNPROCESSABLE_ENTITY
    assert target.text
    await rig.tick(2.0)
    assert rig.panel.runtime.state is RuntimeState.RUNNING
    await finish(rig)


async def test_a_target_with_no_session_is_refused(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        target = await session.post(
            "/api/manual/target", json={"output_rpm": 5.0, "operator": OPERATOR}
        )
    assert target.status_code == httpx.codes.CONFLICT
    assert "no manual session" in detail_of(target)
    await rig.tick(2.0)
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_target_after_an_estop_is_refused_and_nothing_resumes(tmp_path: Path) -> None:
    rig = await cruising(tmp_path)
    async with rig.http() as session:
        estop = await session.post("/api/session/estop", json={})
        assert estop.status_code == httpx.codes.OK
        await rig.tick(1.0)
        target = await session.post(
            "/api/manual/target", json={"output_rpm": 5.0, "operator": OPERATOR}
        )
        assert target.status_code == httpx.codes.CONFLICT
        detail_of(target)
    await rig.tick(30.0)
    assert rig.panel.runtime.end_reason is EndReason.EMERGENCY_STOP
    await rig.panel.close()
    assert await rig.left_stopped() == ""


async def test_a_blank_operator_is_refused(tmp_path: Path) -> None:
    rig = await cruising(tmp_path)
    async with rig.http() as session:
        target = await session.post("/api/manual/target", json={"output_rpm": 5.0, "operator": " "})
    assert target.status_code in {httpx.codes.BAD_REQUEST, httpx.codes.UNPROCESSABLE_ENTITY}
    detail_of(target)
    await finish(rig)
