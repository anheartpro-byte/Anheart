from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from src.cloud_sync import HEARTBEAT_PERIOD, live_row
from src.motor.sto import STO_STALE_AFTER, DriveInputStoSource, StoReading, StoSourceKind, StoState
from src.training.preflight import StoNotReady, describe_sto_refusal
from src.training.runtime import RULE_STO_OPEN
from src.units import Monotonic, Seconds
from src.web.app import create_app
from src.web.schemas import SnapshotRow, StatusRow, StoRow, WsEnvelope
from tests import test_cloud_sync as dashboard
from tests.test_failure_rig import BENCH_ENV, OPERATOR, make_rig, start_manual
from tests.test_web_api import make_snapshot, parse, probe_socket


def socket_frames(sent: tuple[MutableMapping[str, object], ...]) -> list[WsEnvelope]:
    frames: list[WsEnvelope] = []
    for message in sent:
        if message["type"] == "websocket.send":
            body = message["text"]
            assert isinstance(body, str)
            frames.append(TypeAdapter(WsEnvelope).validate_json(body))
    return frames


@pytest.mark.parametrize(
    ("source", "state"),
    [
        ("sim_ready", StoState.READY),
        ("sim_open", StoState.OPEN),
        ("none", StoState.NOT_CONFIGURED),
        ("drive_input", StoState.UNKNOWN),
    ],
)
async def test_sto_state_source_and_age_reach_every_local_surface(
    tmp_path: Path,
    source: str,
    state: StoState,
) -> None:
    rig, _ = make_rig(
        tmp_path,
        env={
            **BENCH_ENV,
            "STO_SOURCE": source,
            "STO_DRIVE_INPUT": "LI1" if source == "drive_input" else "",
        },
    )
    try:
        snapshot = await rig.tick(0.2)
        assert snapshot.sto.state is state
        assert snapshot.sto.source.value == source
        assert snapshot.sto_age == (Seconds(0) if source.startswith("sim_") else None)
        row = SnapshotRow.of(snapshot, now=rig.clock.monotonic())
        assert row.sto.state == state.value
        assert row.sto.age_s == snapshot.sto_age
        assert row.sto.unknown_refusal == describe_sto_refusal(
            StoNotReady(state=StoState.UNKNOWN, detail="")
        )
        if state is StoState.READY:
            assert row.sto.refusal is None
        else:
            assert row.sto.refusal == describe_sto_refusal(StoNotReady(state=state, detail=""))
        async with rig.http() as client:
            status = parse(StatusRow, await client.get("/api/status"))
            local = parse(SnapshotRow, await client.get("/api/snapshot"))
        assert status.sto == local.sto == row.sto
        app = create_app(services=rig.panel.services, config=rig.config.web)
        frames = socket_frames(await probe_socket(app, origin=None, token=None, replies=1))
        assert [frame.snapshot.sto for frame in frames if frame.snapshot is not None] == [row.sto]
    finally:
        await rig.panel.close()


async def test_cached_ready_expires_in_snapshot_and_status_even_without_another_tick(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(
        tmp_path, env={**BENCH_ENV, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI1"}
    )
    try:
        source = rig.panel.sto
        assert isinstance(source, DriveInputStoSource)
        await rig.panel.control_step()
        await source.poll()
        fresh = await rig.tick(0.2)
        assert fresh.sto.state is StoState.READY
        rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
        async with rig.http() as client:
            status = parse(StatusRow, await client.get("/api/status"))
            cached = parse(SnapshotRow, await client.get("/api/snapshot"))
        assert status.sto.state == "unknown"
        assert cached.sto == status.sto
        app = create_app(services=rig.panel.services, config=rig.config.web)
        reconnected = socket_frames(await probe_socket(app, origin=None, token=None, replies=1))
        assert [frame.snapshot.sto for frame in reconnected if frame.snapshot is not None] == [
            cached.sto
        ]
        old = await rig.panel.control_step()
        assert old.sto.state is StoState.UNKNOWN
        assert old.sto_age is not None
        assert old.sto_age > STO_STALE_AFTER
        async with rig.http() as client:
            local = parse(SnapshotRow, await client.get("/api/snapshot"))
        assert local.sto == status.sto
        app = create_app(services=rig.panel.services, config=rig.config.web)
        frames = socket_frames(await probe_socket(app, origin=None, token=None, replies=1))
        assert [frame.snapshot.sto for frame in frames if frame.snapshot is not None] == [local.sto]
    finally:
        await rig.panel.close()


@pytest.mark.parametrize("last", [None, Monotonic(5.0)])
def test_wire_never_calls_missing_or_stale_ready_reading_ready(last: Monotonic | None) -> None:
    row = StoRow.of(
        StoReading(
            state=StoState.READY,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=last,
            detail="test",
        ),
        Monotonic(10.0),
    )
    assert row.state == "unknown"
    assert row.refusal == describe_sto_refusal(StoNotReady(state=StoState.UNKNOWN, detail=""))


def test_wire_without_current_time_cannot_attest_readiness() -> None:
    snapshot = replace(
        make_snapshot(),
        sto=StoReading(
            state=StoState.READY,
            source=StoSourceKind.SIM_READY,
            last_success_at=Monotonic(10),
            detail="test",
        ),
    )
    row = SnapshotRow.of(snapshot)
    assert row.sto.state == "unknown"
    assert row.sto.age_s is None


async def test_open_during_session_reaches_socket_status_and_refuses_restart_and_ack(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path)
    try:
        async with rig.http() as client:
            await start_manual(rig, client, 5)
            await rig.tick(2)
            chain = rig.simulator.sto_chain
            assert chain is not None
            chain.open(rig.clock.monotonic())
            snapshot = await rig.tick(0.2)
            assert snapshot.sto.state is StoState.OPEN
            assert snapshot.setpoint.motor_rpm == 0
            app = create_app(services=rig.panel.services, config=rig.config.web)
            frames = socket_frames(await probe_socket(app, origin=None, token=None, replies=1))
            row = next(frame.snapshot for frame in frames if frame.snapshot is not None)
            status = parse(StatusRow, await client.get("/api/status"))
            assert row.sto == status.sto
            assert row.sto.state == "open"
            assert row.safety is not None
            assert status.standing is not None
            assert row.safety.rule == status.standing.rule == RULE_STO_OPEN
            assert row.safety.latched
            assert row.safety.detail
            refused = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert refused.status_code == HTTPStatus.PRECONDITION_FAILED
            ack = await client.post(
                "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
            )
            assert ack.status_code == HTTPStatus.CONFLICT
            assert describe_sto_refusal(StoNotReady(state=StoState.OPEN, detail="")) in ack.text
    finally:
        await rig.panel.close()


@pytest.mark.parametrize("source", ["sim_ready", "sim_open", "drive_input", "none"])
async def test_status_before_first_tick_is_explicitly_not_ready(
    tmp_path: Path, source: str
) -> None:
    env = {**BENCH_ENV, "STO_SOURCE": source}
    if source == "drive_input":
        env["STO_DRIVE_INPUT"] = "LI1"
    rig, _ = make_rig(tmp_path, env=env)
    try:
        assert rig.panel.services.sto is rig.panel.sto
        async with rig.http() as client:
            status = parse(StatusRow, await client.get("/api/status"))
        assert status.sto.state == ("not_configured" if source == "none" else "unknown")
        assert status.sto.source == source
        assert status.sto.age_s is None
    finally:
        await rig.panel.close()


async def test_status_without_a_source_or_tick_is_not_ready(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    try:
        app = create_app(services=replace(rig.panel.services, sto=None), config=rig.config.web)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            status = parse(StatusRow, await client.get("/api/status"))
        assert status.sto.state == "not_configured"
        assert status.sto.source == "none"
    finally:
        await rig.panel.close()


@pytest.mark.parametrize("state", list(StoState))
async def test_sto_is_strictly_local_and_never_changes_live_row_or_heartbeat(
    tmp_path: Path,
    state: StoState,
) -> None:
    rig = dashboard.rig(tmp_path)
    original = make_snapshot()
    local = replace(
        original,
        sto=StoReading(
            state=state,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=original.at,
            detail="local only",
        ),
    )
    try:
        assert live_row(local, "s") == live_row(original, "s")
        rig.runtime.source = lambda: original
        await rig.step()
        (before,) = rig.dashboard.to("/api/machine/heartbeat")
        rig.runtime.source = lambda: local
        await rig.step(HEARTBEAT_PERIOD)
        after = rig.dashboard.to("/api/machine/heartbeat")[-1]
        assert after == before
        assert set(after) == {
            "live",
            "programsEnabled",
            "software_version",
            "contract_version",
            "medical_parameters_version",
            "config_hash",
        }
    finally:
        await rig.panel.close()
