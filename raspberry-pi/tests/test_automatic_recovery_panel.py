from collections.abc import AsyncIterator
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest

from src.clock import ManualClock
from src.local_panel import DriveSide, LocalPanel, build_panel
from src.motor.drive import ControlWord, RegisterMap
from src.training.runtime import IDLE_POLL_PERIOD, RULE_DRIVE_PRECOMMANDED
from src.training.safety import RULE_COMMS_LOST
from src.web.app import create_app
from src.web.schemas import PanelRow, SnapshotRow, StatusRow
from tests import test_atv320
from tests.test_atv320 import (
    ETA_OPERATION_ENABLED,
    Behave,
    FakeBus,
    build_drive,
    parameter_exception,
)
from tests.test_failure_rig import BENCH_ENV, config_of, no_dsp
from tests.test_web_api import StubPorts, parse

clock = test_atv320.clock
bus = test_atv320.bus
make_bus = test_atv320.make_bus


@pytest.fixture
def panel(tmp_path: Path, clock: ManualClock, bus: FakeBus) -> LocalPanel:
    return build_panel(
        config_of(BENCH_ENV),
        clock=clock,
        profiles_path=tmp_path / "profiles.json",
        treat=no_dsp,
        drive=DriveSide(backend=build_drive(clock, bus), simulator=None, release=bus.close),
    )


@pytest.fixture
async def client(panel: LocalPanel) -> AsyncIterator[httpx.AsyncClient]:
    services = replace(panel.services, ports=StubPorts(ports=()))
    app = create_app(services=services, config=config_of(BENCH_ENV).web)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as session:
            response = await session.post(
                "/api/safety/attest",
                json={
                    "operator": "synthetic bench operator",
                    "sto_jumper_removed": True,
                    "mushroom_wired_nc": True,
                },
            )
            assert response.status_code == HTTPStatus.OK
            yield session
    finally:
        await panel.close()


async def step(panel: LocalPanel, clock: ManualClock) -> None:
    clock.advance(IDLE_POLL_PERIOD)
    await panel.control_step()


async def test_zero_frame_outage_remains_retryable_and_recovers_on_http_panel(
    panel: LocalPanel, client: httpx.AsyncClient, clock: ManualClock, bus: FakeBus
) -> None:
    # Given
    bus.port_opens = False
    limit = panel.runtime.supervisor.limits.comms_lost_failures
    for _ in range(limit + 2):
        await step(panel, clock)
    retrying = parse(PanelRow, await client.get("/api/panel"))
    assert retrying.drive.consecutive_failures == limit + 2
    assert not retrying.drive.open
    assert retrying.drive.last_error is not None
    assert parse(StatusRow, await client.get("/api/status")).standing is None
    assert panel.drive.backend.acquisition_evidence.possible_frames == 0
    assert bus.log == []
    # When
    bus.port_opens = True
    await step(panel, clock)
    # Then
    recovered = parse(PanelRow, await client.get("/api/panel"))
    snapshot = parse(SnapshotRow, await client.get("/api/snapshot"))
    assert recovered.drive.open
    assert recovered.drive.consecutive_failures == 0
    assert recovered.drive.reads == 1
    assert snapshot.drive_state == "switch_on_disabled"
    assert not snapshot.drive_status_stale
    assert snapshot.manual is None
    assert snapshot.setpoint.motor_rpm == 0
    assert not panel.runtime.silent
    assert bus.writes() == []


@pytest.mark.parametrize("previous_proof", [False, True])
async def test_http_terminal_recovery_retains_unknown_output_and_stops_all_traffic(
    panel: LocalPanel,
    client: httpx.AsyncClient,
    clock: ManualClock,
    bus: FakeBus,
    previous_proof: bool,
) -> None:
    # Given
    if previous_proof:
        await step(panel, clock)
    bus.sticky_reads = parameter_exception()
    limit = panel.runtime.supervisor.limits.comms_lost_failures
    # When
    for attempt in range(limit):
        await step(panel, clock)
        assert panel.runtime.silent is (attempt + 1 == limit)
        unknown = parse(SnapshotRow, await client.get("/api/snapshot"))
        assert unknown.drive_state == "comm_lost"
        assert unknown.drive_status_stale
    # Then
    link = parse(PanelRow, await client.get("/api/panel"))
    status = parse(StatusRow, await client.get("/api/status"))
    snapshot = parse(SnapshotRow, await client.get("/api/snapshot"))
    assert link.drive.consecutive_failures == limit
    assert not link.drive.open
    assert snapshot.safety is not None
    assert snapshot.safety.rule == RULE_COMMS_LOST
    assert snapshot.safety.action == "go_silent"
    assert snapshot.drive_state == "comm_lost"
    assert snapshot.drive_status_stale
    assert snapshot.current_a is None
    assert panel.runtime.output_enabled
    assert panel.drive.backend.acquisition_evidence.address_proven is previous_proof
    assert bus.writes() == ([(RegisterMap().lfrd, 0)] if previous_proof else [])
    before = panel.drive.backend.acquisition_evidence
    trace = tuple(bus.log)
    connects = bus.connect_calls
    bus.sticky_reads = Behave.NORMALLY
    for _ in range(limit + 2):
        await step(panel, clock)
    start = await client.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": "synthetic bench operator"}
    )
    assert start.status_code == HTTPStatus.ACCEPTED
    await step(panel, clock)
    refused_start = parse(StatusRow, await client.get("/api/status"))
    assert refused_start.pending is None
    assert refused_start.counters[1] == status.counters[1] + 1
    reset = await client.post(
        "/api/drive/fault-reset", json={"operator": "synthetic bench operator"}
    )
    assert reset.status_code == HTTPStatus.ACCEPTED
    await step(panel, clock)
    refused_reset = parse(StatusRow, await client.get("/api/status"))
    assert refused_reset.pending is None
    assert refused_reset.counters[1] == status.counters[1] + 2
    acknowledged = await client.post(
        "/api/safety/acknowledge",
        json={"operator": "synthetic bench operator", "estop_released": True},
    )
    assert acknowledged.status_code == HTTPStatus.CONFLICT
    report = await panel.runtime.shutdown("synthetic panel terminal shutdown")
    assert report.silent
    assert not report.output_disabled
    await panel.close()
    assert panel.drive.backend.acquisition_evidence == before
    assert tuple(bus.log) == trace
    assert bus.connect_calls == connects
    assert panel.runtime.manual is None


@pytest.mark.parametrize("failed_attempts", [1, 2])
async def test_recovered_precommanded_status_stops_and_latches_without_resuming(
    panel: LocalPanel,
    client: httpx.AsyncClient,
    clock: ManualClock,
    bus: FakeBus,
    failed_attempts: int,
) -> None:
    # Given
    regs = RegisterMap()
    bus.registers[regs.eta] = ETA_OPERATION_ENABLED
    bus.registers[regs.lfrd] = 300
    bus.registers[regs.rfrd] = 300
    bus.script_reads.extend(parameter_exception() for _ in range(failed_attempts))
    for _ in range(failed_attempts):
        await step(panel, clock)
    assert bus.writes() == []
    # When
    await step(panel, clock)
    # Then
    snapshot = parse(SnapshotRow, await client.get("/api/snapshot"))
    assert snapshot.safety is not None
    assert snapshot.safety.rule == RULE_DRIVE_PRECOMMANDED
    assert snapshot.safety.action == "quick_stop"
    assert snapshot.manual is None
    assert snapshot.measured.motor_rpm == 300
    assert panel.runtime.manual is None
    assert bus.setpoint() == 0
    assert set(bus.writes()) == {(regs.lfrd, 0)}
    bus.registers[regs.rfrd] = 0
    await step(panel, clock)
    await panel.close()
    assert all(value == 0 for address, value in bus.writes() if address == regs.lfrd)
    assert ControlWord.ENABLE_OPERATION.value not in bus.command_words()
    assert ControlWord.FAULT_RESET.value not in bus.command_words()
