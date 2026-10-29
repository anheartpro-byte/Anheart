"""``STO_SOURCE=bridged_declared``: a declared STO bridge, bench only, nobody on board (ANH-92).

A person declares the STO input bridged on a real drive: no reading, no
hardware emergency stop, the mains plug is the emergency stop. Under it a
manual bench session arms, and nothing with a person on board does, on any
path, and a launch from the dashboard is refused in its own words; the
attestation is the bench one, never ANH-92's two boxes (which are
false there), and ANH-92's never the bench one elsewhere. Every other rule
stays: verdicts, the screen's E-STOP, the deduced ``sto_open``, the named
acknowledgement and the new start it requires.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import describe_surface_refusal, live_row
from src.control_surface import RunState
from src.local_config import (
    ConfigProblem,
    MotorBackend,
    default_sto_source,
    load_local_config,
)
from src.local_panel import (
    EXIT_CONFIG,
    EXIT_OK,
    DriveSide,
    build_panel,
    build_sto,
    describe_start_refusal,
    main,
)
from src.motor.drive import DriveError, DriveLimits, LogicInput
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import (
    STO_STALE_AFTER,
    BridgedDeclaredStoSource,
    DriveInputStoSource,
    SimulatedStoChain,
    SimulatedStoSource,
    StoReading,
    StoSource,
    StoSourceKind,
    StoState,
    UnconfiguredStoSource,
    sto_ready,
)
from src.result import Err, Ok, Result
from src.training.preflight import (
    BRIDGED_BENCH_ONLY,
    BRIDGED_REMOTE_LAUNCH,
    StoBridgedOnBoard,
    StoBridgedRemoteLaunch,
    StoNotReady,
    check_sto_for_start,
    check_sto_ready,
    describe_sto_refusal,
)
from src.training.runtime import (
    RULE_STO_OPEN,
    RULE_STO_UNREADABLE,
    STO_OPEN_DEDUCED,
    NotAttested,
    RuntimeState,
    Subject,
    TrainingRuntime,
)
from src.training.safety import (
    BENCH_BRIDGED_ATTESTATION,
    ESTOP_ATTESTATION,
    RULE_OPERATOR_ESTOP,
    EmergencyStopStillLatched,
    EstopUnattested,
    GoSilentIsTerminal,
    SafetySupervisor,
    Unattributed,
)
from src.training.types import Occupancy, SafetyAction, SafetyVerdict
from src.units import Bpm, Monotonic, MotorRpm, RawRegister, RegisterAddress, Seconds
from src.web.app import create_app
from src.web.routes import (
    BENCH_ATTESTATION_FIELDS,
    BENCH_ESTOP_RELEASED_LABEL,
    ESTOP_RELEASED_LABEL,
    ESTOP_WIRING_FIELDS,
    STO_BRIDGED_BENCH_ONLY,
)
from src.web.schemas import AttestationRow, SnapshotRow, StatusRow
from tests import test_cloud_sync as dashboard_tests
from tests.medical_support import signed
from tests.roster_support import RIDER_ID
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    Rig,
    attest,
    config_of,
    make_rig,
)
from tests.test_local_panel import FakeWeb
from tests.test_runtime import GEOMETRY, LIMITS, FakeDrive, sto_rig
from tests.test_runtime import Rig as RuntimeRig
from tests.test_sto import RegisterPort
from tests.test_sto_runtime import MemorySto
from tests.test_web_api import make_snapshot, parse, probe_socket
from tests.test_web_sto import socket_frames

SERIAL: Final[Mapping[str, str]] = {"MOTOR_BACKEND": "serial", "MOTOR_PORT": "/dev/ttyUSB-test"}
"""A real drive in the configuration; every console below is given a simulated one."""

BRIDGED_ENV: Final[Mapping[str, str]] = {
    **BENCH_ENV,
    **SERIAL,
    "STO_SOURCE": "bridged_declared",
}

BRIDGED_PROGRAMME_ENV: Final[Mapping[str, str]] = {
    **PROGRAMME_ENV,
    **SERIAL,
    "STO_SOURCE": "bridged_declared",
}
"""Programmes and a person on board enabled, over the shipped (signed) medical parameters."""

BENCH_RPM: Final[MotorRpm] = MotorRpm(276)


def bridged_rig() -> RuntimeRig:
    """A runtime over the declared bridge, attested by name."""
    rig = sto_rig(sto=BridgedDeclaredStoSource())
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    return rig


def bridged_reading() -> StoReading:
    return BridgedDeclaredStoSource().latest(Monotonic(0.0))


async def attest_bench(client: httpx.AsyncClient, operator: str = OPERATOR) -> httpx.Response:
    return await client.post(
        "/api/safety/attest", json={"operator": operator, "bench_bridged_declared": True}
    )


async def start_bench(rig: Rig, client: httpx.AsyncClient, output_rpm: float) -> None:
    """Attest the bench, start a bench manual session and climb to ``output_rpm``."""
    attested = await attest_bench(client)
    assert attested.status_code == HTTPStatus.OK, attested.text
    started = await client.post(
        "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
    )
    assert started.status_code == HTTPStatus.ACCEPTED, started.text
    await rig.tick(1.0)
    target = await client.post(
        "/api/manual/target", json={"output_rpm": output_rpm, "operator": OPERATOR}
    )
    assert target.status_code == HTTPStatus.ACCEPTED, target.text


def standing_of(rig: Rig) -> SafetyVerdict | None:
    """Read afresh, for the reason :func:`runtime_state` gives."""
    return rig.panel.runtime.standing


def runtime_state(rig: Rig) -> RuntimeState:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return rig.panel.runtime.state


# =========================================================================
# EX-B1 / D1: an explicit value, never a default, only in front of a real drive
# =========================================================================


def test_bridged_declared_is_accepted_only_with_a_real_drive_and_is_never_a_default() -> None:
    config = config_of(BRIDGED_ENV)
    assert config.sto_source is StoSourceKind.BRIDGED_DECLARED
    assert config.sto_drive_input is None
    assert config_of({**BRIDGED_ENV, "STO_SOURCE": "Bridged_DECLARED"}).sto_source is (
        StoSourceKind.BRIDGED_DECLARED
    )
    simulated = load_local_config({**BRIDGED_ENV, "MOTOR_BACKEND": "sim"})
    assert isinstance(simulated, Err)
    assert simulated.error == (
        ConfigProblem("STO_SOURCE", "bridged_declared interdit avec MOTOR_BACKEND=sim"),
    )
    assert main({**BENCH_ENV, "STO_SOURCE": "bridged_declared"}) == EXIT_CONFIG
    with_input = load_local_config({**BRIDGED_ENV, "STO_DRIVE_INPUT": "LI1"})
    assert isinstance(with_input, Err)
    assert with_input.error == (
        ConfigProblem("STO_DRIVE_INPUT", "reserve a STO_SOURCE=drive_input"),
    )
    # The defaults of ANH-92, unchanged.
    unset = {key: value for key, value in BRIDGED_ENV.items() if key != "STO_SOURCE"}
    assert config_of(unset).sto_source is StoSourceKind.NONE
    assert config_of(BENCH_ENV).sto_source is StoSourceKind.SIM_READY
    assert default_sto_source(MotorBackend.SERIAL) is StoSourceKind.NONE
    assert default_sto_source(MotorBackend.SIM) is StoSourceKind.SIM_READY


def test_the_composition_builds_the_declaration_and_never_for_a_simulated_drive() -> None:
    config = config_of(BRIDGED_ENV)
    clock = ManualClock()
    source = build_sto(config, clock, SimulatedDrive(clock), None)
    assert isinstance(source, BridgedDeclaredStoSource)
    assert source.latest(clock.monotonic()) == StoReading(
        state=StoState.BRIDGED,
        source=StoSourceKind.BRIDGED_DECLARED,
        last_success_at=None,
        detail="bridged, declared by a person",
    )
    assert not sto_ready(source.latest(clock.monotonic()))
    with pytest.raises(ValueError, match="real drive"):
        build_sto(
            replace(config, motor_backend=MotorBackend.SIM), clock, SimulatedDrive(clock), None
        )


# =========================================================================
# EX-B1 / D1: nothing is ever read
# =========================================================================


def count_register_reads(monkeypatch: pytest.MonkeyPatch) -> list[RegisterAddress]:
    reads: list[RegisterAddress] = []
    original = SimulatedDrive.read_register

    async def counted(
        drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        reads.append(address)
        return await original(drive, address)

    monkeypatch.setattr(SimulatedDrive, "read_register", counted)
    return reads


async def test_a_bench_session_reads_no_register_of_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = count_register_reads(monkeypatch)
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        assert isinstance(rig.panel.sto, BridgedDeclaredStoSource)
        async with rig.http() as client:
            await start_bench(rig, client, 5)
            await rig.tick(5.0)
        assert runtime_state(rig) is RuntimeState.RUNNING
        assert rig.panel.runtime.applied_rpm > 0
    finally:
        await rig.panel.close()
    assert reads == []


async def test_the_console_starts_no_polling_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = count_register_reads(monkeypatch)
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    stop = asyncio.Event()
    before = asyncio.all_tasks()
    runner = asyncio.create_task(rig.panel.run(stop, FakeWeb()))
    await asyncio.sleep(0.05)
    names = {task.get_name() for task in asyncio.all_tasks() - before - {runner}}
    stop.set()
    assert await asyncio.wait_for(runner, 5) == EXIT_OK
    assert names  # the console's own tasks were running
    assert "sto-poll" not in names
    assert reads == []


# =========================================================================
# EX-B2 / D3: bench only, on every arming path
# =========================================================================


@pytest.mark.parametrize("state", list(StoState))
@pytest.mark.parametrize("source", list(StoSourceKind))
@pytest.mark.parametrize("occupancy", [None, Occupancy.BENCH, Occupancy.OCCUPIED])
def test_only_the_declared_bridge_admits_a_reading_that_is_not_ready_and_only_the_bench(
    state: StoState, source: StoSourceKind, occupancy: Occupancy | None
) -> None:
    reading = StoReading(state=state, source=source, last_success_at=None, detail="d")
    result = check_sto_for_start(reading, occupancy)
    if state is StoState.BRIDGED and source is StoSourceKind.BRIDGED_DECLARED:
        assert result == (None if occupancy is Occupancy.BENCH else StoBridgedOnBoard())
    else:
        # Every other source keeps ANH-92's rule: READY alone arms, whoever is on board.
        assert result == check_sto_ready(reading)
        assert (result is None) is (state is StoState.READY)


def test_the_bench_rule_has_one_sentence_everywhere() -> None:
    refusal = StoBridgedOnBoard()
    assert describe_start_refusal(refusal) == f"demarrage refuse : {BRIDGED_BENCH_ONLY}"
    assert describe_surface_refusal(refusal) == BRIDGED_BENCH_ONLY
    assert describe_sto_refusal(StoNotReady(state=StoState.BRIDGED, detail="")) == (
        BRIDGED_BENCH_ONLY
    )
    assert "capsule vide ou lestee, aucune personne a bord" in BRIDGED_BENCH_ONLY
    assert "aucun arret d'urgence materiel" in BRIDGED_BENCH_ONLY
    # The dashboard's launch has its own reason, apart from the console's.
    assert describe_surface_refusal(StoBridgedRemoteLaunch()) == BRIDGED_REMOTE_LAUNCH
    assert BRIDGED_REMOTE_LAUNCH == (
        "STO ponte : lancement depuis le site refuse, seances de banc a la console seulement"
    )
    assert BRIDGED_REMOTE_LAUNCH != BRIDGED_BENCH_ONLY


async def test_a_bench_manual_session_arms_under_the_declared_bridge() -> None:
    rig = bridged_rig()
    started = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, BENCH_RPM)
    assert isinstance(started, Ok)
    assert rig.state() is RuntimeState.RUNNING
    assert rig.runtime.output_enabled


@pytest.mark.parametrize("programme", [True, False])
async def test_a_person_on_board_is_refused_before_the_drive_is_touched(programme: bool) -> None:
    rig = bridged_rig()
    rig.fed_bpm = Bpm(80)
    rig.feed(Bpm(80))  # a fresh heart rate: only the bridge refuses
    before = list(rig.drive.trace)
    result = (
        await rig.runtime.start(rig.program, Subject(subject_id="s", operator=OPERATOR))
        if programme
        else await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, BENCH_RPM)
    )
    assert result == Err(StoBridgedOnBoard())
    assert rig.drive.trace == before
    assert rig.state() is RuntimeState.IDLE


@pytest.mark.parametrize("programme", [True, False])
async def test_the_revalidation_after_the_inspection_refuses_a_person_on_board(
    monkeypatch: pytest.MonkeyPatch, programme: bool
) -> None:
    """READY when the start is judged, the declared bridge once the drive was read."""
    source = MemorySto(
        StoReading(
            state=StoState.READY,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=Monotonic(0.0),
            detail="relay",
        )
    )
    rig = sto_rig(sto=source)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    rig.fed_bpm = Bpm(80)
    rig.feed(Bpm(80))
    source.reading = replace(source.reading, last_success_at=rig.now)
    original = FakeDrive.read_limits

    async def read_limits(drive: FakeDrive) -> Result[DriveLimits, DriveError]:
        result = await original(drive)
        source.reading = bridged_reading()
        return result

    monkeypatch.setattr(FakeDrive, "read_limits", read_limits)
    result = (
        await rig.runtime.start(rig.program, Subject(subject_id="s", operator=OPERATOR))
        if programme
        else await rig.runtime.start_manual(Occupancy.OCCUPIED, OPERATOR, BENCH_RPM)
    )
    assert result == Err(StoBridgedOnBoard())
    assert "cmd:ENABLE_OPERATION" not in rig.drive.trace
    assert not rig.runtime.output_enabled


async def test_every_console_start_with_a_person_on_board_is_refused_with_the_bench_reason(
    tmp_path: Path,
) -> None:
    """Signed medical parameters, programmes and a person on board enabled: refused all the same."""
    rig, _ = make_rig(tmp_path, env=BRIDGED_PROGRAMME_ENV)
    try:
        config = rig.config
        assert config.medical.signed
        assert config.programs_enabled
        assert config.occupied_enabled
        async with rig.http() as client:
            assert (await attest_bench(client)).status_code == HTTPStatus.OK
            programme = await client.post(
                "/api/session/start",
                json={
                    "profile_id": SHORT_PROFILE,
                    "operator": OPERATOR,
                    "total_duration_s": None,
                    "subject_id": RIDER_ID,
                },
            )
            occupied = await client.post(
                "/api/manual/start",
                json={
                    "occupancy": "occupied",
                    "operator": OPERATOR,
                    "subject_id": RIDER_ID,
                },
            )
            for refused in (programme, occupied):
                assert refused.status_code == HTTPStatus.PRECONDITION_FAILED, refused.text
                assert refused.json()["code"] == STO_BRIDGED_BENCH_ONLY
                assert refused.json()["detail"] == BRIDGED_BENCH_ONLY
            surface = rig.panel.surface
            assert surface.check_start() == Err(StoBridgedOnBoard())  # who is on board, unsaid
            assert surface.check_start(Occupancy.OCCUPIED) == Err(StoBridgedOnBoard())
            assert surface.check_start(Occupancy.BENCH) == Ok(None)
            assert surface.check_remote_launch() == Err(StoBridgedRemoteLaunch())
            await rig.tick(1.0)
            assert runtime_state(rig) is RuntimeState.IDLE
            bench = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert bench.status_code == HTTPStatus.ACCEPTED, bench.text
            await rig.tick(1.0)
            assert runtime_state(rig) is RuntimeState.RUNNING
    finally:
        await rig.panel.close()


async def test_a_bench_start_under_a_latch_of_the_runtime_is_refused_with_the_click(
    tmp_path: Path,
) -> None:
    """The route's latched-floor check knows the bench passes the bridge: a 409, not a 202."""
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        async with rig.http() as client:
            await start_bench(rig, client, 5)
            await rig.tick(2.0)
            await deduce_an_opening(rig)
            await until_idle(rig)
            assert rig.panel.runtime.standing is not None
            refused = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert refused.status_code == HTTPStatus.CONFLICT, refused.text
            assert RULE_STO_OPEN in refused.text
    finally:
        await rig.panel.close()


# --- the dashboard's launch: a programme, refused before training/start, in its words ---


async def test_a_launch_from_the_dashboard_is_refused_in_its_own_words_and_never_confirmed(
    tmp_path: Path,
) -> None:
    """Signed parameters, programmes and a person on board enabled: the bridge refuses first,
    with the reason of a launch from the site, not the console's."""
    clock = ManualClock(Monotonic(10.0))
    dashboard = dashboard_tests.Dashboard()
    config = dashboard_tests.config_of(signed(), **SERIAL, STO_SOURCE="bridged_declared")
    assert config.medical.signed
    assert config.programs_enabled
    assert config.occupied_enabled
    simulator = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
        drive=DriveSide(backend=simulator, simulator=simulator, release=lambda: None),
    )
    linked = dashboard_tests.Linked(clock=clock, panel=panel, dashboard=dashboard)
    try:
        assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
        subscriber = panel.hub.subscribe()
        dashboard.answer(
            "/api/machine/training/poll",
            dashboard_tests.launch_answer(dict(dashboard_tests.LAUNCH)),
        )
        await linked.run(0.2)
        dashboard_tests.confirm_linked(panel)
        await linked.run(2.8)
        assert panel.runtime.state is RuntimeState.IDLE
        assert dashboard.to(dashboard_tests.START) == []  # nothing confirmed to the dashboard
        (end,) = dashboard.to("/api/machine/training/end")
        assert end["failed"] is True
        assert end["reason"] == f"refusee par la machine : {BRIDGED_REMOTE_LAUNCH}"
        said: list[str] = []
        while BRIDGED_REMOTE_LAUNCH not in said:
            payload = await asyncio.wait_for(subscriber.next_payload(), 1)
            if payload.event is not None:
                said.append(payload.event.detail)
    finally:
        await panel.close()


# =========================================================================
# EX-B3 / D5: the bench attestation, and only it, under the declared bridge
# =========================================================================


def test_the_supervisor_attests_the_statement_of_its_mode_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.training.safety")
    clock = ManualClock()
    limits = sto_rig().runtime.supervisor.limits
    supervisor = SafetySupervisor(clock=clock, limits=limits, attestation=BENCH_BRIDGED_ATTESTATION)
    assert supervisor.attestation_statement == BENCH_BRIDGED_ATTESTATION
    assert supervisor.require_estop_confirmed() == Err(EstopUnattested(BENCH_BRIDGED_ATTESTATION))
    assert supervisor.confirm_estop_wiring(" ") == Err(
        Unattributed(
            "an attestation must name the operator making it: "
            "an unattributable safety record is not a safety record"
        )
    )
    recorded = supervisor.confirm_estop_wiring(OPERATOR)
    assert isinstance(recorded, Ok)
    assert recorded.value.statement == BENCH_BRIDGED_ATTESTATION
    assert supervisor.require_estop_confirmed() == recorded
    assert any(
        record.getMessage().endswith(f": {BENCH_BRIDGED_ATTESTATION}")
        and OPERATOR in record.getMessage()
        for record in caplog.records
    )
    # A console started again is a new supervisor: attested by nobody.
    again = SafetySupervisor(clock=clock, limits=limits, attestation=BENCH_BRIDGED_ATTESTATION)
    assert again.require_estop_confirmed() == Err(EstopUnattested(BENCH_BRIDGED_ATTESTATION))
    assert BENCH_BRIDGED_ATTESTATION == (
        "le STO est ponte, aucun arret d'urgence materiel ; l'arret d'urgence est la coupure de "
        "la prise secteur ; capsule vide ou lestee, aucune personne a bord ; personne dans le "
        "rayon du bras pendant la rotation"
    )


def test_a_shared_supervisor_must_attest_the_runtime_s_statement() -> None:
    rig = sto_rig()
    shared = SafetySupervisor(clock=rig.clock, limits=rig.runtime.supervisor.limits)
    with pytest.raises(ValueError, match="another mode"):
        TrainingRuntime(
            clock=rig.clock,
            drive=rig.drive,
            geometry=GEOMETRY,
            limits=LIMITS,
            safety=shared.limits,
            supervisor=shared,
            attestation=BENCH_BRIDGED_ATTESTATION,
        )


async def test_an_unattested_bench_console_names_the_bench_statement() -> None:
    rig = sto_rig(sto=BridgedDeclaredStoSource())
    runtime = TrainingRuntime(
        clock=rig.clock,
        drive=rig.drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=rig.runtime.supervisor.limits,
        sto=BridgedDeclaredStoSource(),
        attestation=BENCH_BRIDGED_ATTESTATION,
    )
    refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, BENCH_RPM)
    assert refused == Err(NotAttested(BENCH_BRIDGED_ATTESTATION))


async def test_the_route_takes_the_bench_confirmation_by_name_and_records_it_verbatim(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="src.training.safety")
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        async with rig.http() as client:
            status = parse(StatusRow, await client.get("/api/status"))
            assert not status.attested
            assert status.attestation_statement == BENCH_BRIDGED_ATTESTATION
            assert status.attestation_fields == BENCH_ATTESTATION_FIELDS
            assert status.estop_released_label == BENCH_ESTOP_RELEASED_LABEL
            assert status.estop_released_label == (
                "aucun coup de poing n'est cable : la cause de l'arret d'urgence de l'ecran "
                "est ecartee et personne n'est dans le rayon du bras"
            )
            unattested = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert unattested.status_code == HTTPStatus.PRECONDITION_FAILED
            assert BENCH_BRIDGED_ATTESTATION in unattested.json()["detail"]
            for body in (
                {"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
                {
                    "operator": OPERATOR,
                    "bench_bridged_declared": True,
                    "sto_jumper_removed": True,
                },
                {"operator": OPERATOR, "bench_bridged_declared": True, "mushroom_wired_nc": True},
                {"operator": OPERATOR},
                {"operator": " ", "bench_bridged_declared": True},
            ):
                refused = await client.post("/api/safety/attest", json=body)
                assert refused.status_code == HTTPStatus.BAD_REQUEST, body
            assert not parse(StatusRow, await client.get("/api/status")).attested
            assert not any(BENCH_BRIDGED_ATTESTATION in r.getMessage() for r in caplog.records)
            accepted = await attest_bench(client)
            assert accepted.status_code == HTTPStatus.OK
            row = parse(AttestationRow, accepted)
            assert (row.operator, row.statement) == (OPERATOR, BENCH_BRIDGED_ATTESTATION)
            after = parse(StatusRow, await client.get("/api/status"))
            assert after.attested
            assert after.attestation == row
        logged = [r.getMessage() for r in caplog.records if r.name == "src.training.safety"]
        assert any(
            message.endswith(f": {BENCH_BRIDGED_ATTESTATION}") and repr(OPERATOR) in message
            for message in logged
        )
    finally:
        await rig.panel.close()
    # The console started again: attested by nobody.
    (tmp_path / "again").mkdir()
    again, _ = make_rig(tmp_path / "again", env=BRIDGED_ENV)
    try:
        async with again.http() as client:
            assert not parse(StatusRow, await client.get("/api/status")).attested
    finally:
        await again.panel.close()


@pytest.mark.parametrize(
    "env",
    [
        BENCH_ENV,
        {**BENCH_ENV, "STO_SOURCE": "sim_open"},
        {**BENCH_ENV, **SERIAL},
        {**BENCH_ENV, **SERIAL, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI1"},
    ],
    ids=["sim_ready", "sim_open", "none", "drive_input"],
)
async def test_under_every_other_source_the_bench_confirmation_is_refused_and_anh92_stands(
    tmp_path: Path, env: Mapping[str, str]
) -> None:
    rig, _ = make_rig(tmp_path, env=env)
    try:
        async with rig.http() as client:
            status = parse(StatusRow, await client.get("/api/status"))
            assert status.attestation_statement == ESTOP_ATTESTATION
            assert status.attestation_fields == ESTOP_WIRING_FIELDS
            assert status.estop_released_label == ESTOP_RELEASED_LABEL
            for body in (
                {"operator": OPERATOR, "bench_bridged_declared": True},
                {
                    "operator": OPERATOR,
                    "bench_bridged_declared": True,
                    "sto_jumper_removed": True,
                    "mushroom_wired_nc": True,
                },
            ):
                refused = await client.post("/api/safety/attest", json=body)
                assert refused.status_code == HTTPStatus.BAD_REQUEST
                assert "bridged_declared" in refused.text
            one_box = await client.post(
                "/api/safety/attest", json={"operator": OPERATOR, "sto_jumper_removed": True}
            )
            assert one_box.status_code == HTTPStatus.BAD_REQUEST
            assert "both confirmations are required" in one_box.text
            assert not parse(StatusRow, await client.get("/api/status")).attested
            await attest(client)
            after = parse(StatusRow, await client.get("/api/status"))
            assert after.attestation is not None
            assert after.attestation.statement == ESTOP_ATTESTATION
        # The dashboard's launch: the gates of a person on board, as before.
        surface = rig.panel.surface
        remote = surface.check_remote_launch()
        assert remote == surface.check_start(Occupancy.OCCUPIED)
        ready = env.get("MOTOR_BACKEND") == "sim" and env.get("STO_SOURCE") in (None, "sim_ready")
        assert (remote == Ok(None)) is ready  # READY alone passes, as in ANH-92
    finally:
        await rig.panel.close()


# =========================================================================
# EX-B4 / D2: the state on every local surface, never on the heartbeat
# =========================================================================


async def test_the_declaration_reaches_status_snapshot_and_socket_without_an_age(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        async with rig.http() as client:
            early = parse(StatusRow, await client.get("/api/status"))  # before any tick
            snapshot = await rig.tick(0.2)
            status = parse(StatusRow, await client.get("/api/status"))
            local = parse(SnapshotRow, await client.get("/api/snapshot"))
        assert snapshot.sto == bridged_reading()
        assert snapshot.sto_age is None
        row = SnapshotRow.of(snapshot, now=rig.clock.monotonic()).sto
        assert (row.state, row.source, row.age_s, row.refusal) == (
            "bridged",
            "bridged_declared",
            None,
            BRIDGED_BENCH_ONLY,
        )
        assert early.sto == status.sto == local.sto == row
        app = create_app(services=rig.panel.services, config=rig.config.web)
        frames = socket_frames(await probe_socket(app, origin=None, token=None, replies=1))
        assert [frame.snapshot.sto for frame in frames if frame.snapshot is not None] == [row]
    finally:
        await rig.panel.close()


def test_the_heartbeat_row_ignores_the_declaration() -> None:
    original = make_snapshot()
    bridged = replace(original, sto=bridged_reading())
    assert live_row(bridged, "s") == live_row(original, "s")
    assert set(live_row(bridged, "s")) == set(live_row(original, "s"))


# =========================================================================
# EX-B6 / D4: every other rule stays, nothing restarts by itself
# =========================================================================


async def deduce_an_opening(rig: Rig) -> None:
    """The drive removes its output stage by itself (power cut and back): ``sto_open``."""
    chain = rig.simulator.sto_chain
    assert chain is not None
    chain.open(rig.clock.monotonic())
    await rig.tick(0.2)
    chain.close(rig.clock.monotonic())
    await rig.tick(0.2)


async def until_idle(rig: Rig) -> None:
    """Tick until the console's surface is back at rest, the session over."""
    for _ in range(3000):
        await rig.tick(0.2)
        if rig.panel.surface.run_state is RunState.IDLE:
            return
    raise AssertionError("the session never ended")


async def until_stopped(rig: Rig) -> None:
    for _ in range(600):
        await rig.tick(0.2)
        if not rig.panel.runtime.stop_in_progress:
            return
    raise AssertionError("the stop never ended")


async def test_the_screen_estop_latches_a_bench_session_and_wants_a_named_release(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        async with rig.http() as client:
            await start_bench(rig, client, 5)
            await rig.tick(3.0)
            assert rig.panel.runtime.applied_rpm > 0
            pressed = await client.post("/api/session/estop", json={"operator": OPERATOR})
            assert pressed.status_code == HTTPStatus.OK, pressed.text
            snapshot = await rig.tick(0.2)
            assert snapshot.setpoint.motor_rpm == 0
            verdict = rig.panel.runtime.standing
            assert verdict is not None
            assert (verdict.rule, verdict.action, verdict.latched) == (
                RULE_OPERATOR_ESTOP,
                SafetyAction.QUICK_STOP,
                True,
            )
            await until_stopped(rig)
            held = await client.post(
                "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
            )
            assert held.status_code == HTTPStatus.CONFLICT
            assert "estop_released" in held.text
            nameless = await client.post(
                "/api/safety/acknowledge", json={"operator": " ", "estop_released": True}
            )
            assert nameless.status_code == HTTPStatus.BAD_REQUEST
            cleared = await client.post(
                "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
            )
            assert cleared.status_code == HTTPStatus.OK, cleared.text
            await rig.tick(3.0)
            # Cleared is not resumed: nothing turns until a new start.
            assert runtime_state(rig) is not RuntimeState.RUNNING
            assert rig.panel.runtime.applied_rpm == 0
            assert rig.simulator.commanded_setpoint == 0
            again = await client.post(
                "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
            )
            assert again.status_code == HTTPStatus.ACCEPTED, again.text
            await rig.tick(1.0)
            assert runtime_state(rig) is RuntimeState.RUNNING
    finally:
        await rig.panel.close()


async def test_an_opening_shown_by_the_drive_s_word_latches_sto_open_and_no_reading_is_lost(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path, env=BRIDGED_ENV)
    try:
        async with rig.http() as client:
            await start_bench(rig, client, 5)
            await rig.tick(10.0)  # far beyond STO_STALE_AFTER, and nothing is ever read
            rules = {verdict.rule for verdict in rig.panel.runtime.supervisor.live}
            assert RULE_STO_UNREADABLE not in rules
            assert standing_of(rig) is None
            await deduce_an_opening(rig)
            verdict = standing_of(rig)
            assert verdict is not None
            assert (verdict.rule, verdict.detail, verdict.latched) == (
                RULE_STO_OPEN,
                STO_OPEN_DEDUCED,
                True,
            )
            assert rig.panel.runtime.applied_rpm == 0
            await until_stopped(rig)
            # No READY reading exists: the declaration still standing takes its place.
            cleared = await client.post(
                "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": False}
            )
            assert cleared.status_code == HTTPStatus.OK, cleared.text
            await rig.tick(2.0)
            assert runtime_state(rig) is not RuntimeState.RUNNING
            assert rig.simulator.commanded_setpoint == 0
    finally:
        await rig.panel.close()


async def test_go_silent_stays_terminal_under_the_declared_bridge() -> None:
    rig = bridged_rig()
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, BENCH_RPM), Ok)
    rig.runtime.trip_from_thread("test_silence", SafetyAction.GO_SILENT, "test")
    await rig.step()
    refused = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, GoSilentIsTerminal)


async def test_a_bench_estop_needs_the_release_confirmation_at_the_runtime_too() -> None:
    rig = bridged_rig()
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, BENCH_RPM), Ok)
    rig.runtime.request_estop("operator")
    await rig.run(2.0)
    held = rig.runtime.acknowledge(OPERATOR)
    assert isinstance(held, Err)
    assert isinstance(held.error, EmergencyStopStillLatched)
    assert rig.runtime.acknowledge("", estop_released=True) == Err(
        Unattributed(
            "an acknowledgement must name the operator making it: "
            "an unattributable safety record is not a safety record"
        )
    )
    assert isinstance(rig.runtime.acknowledge(OPERATOR, estop_released=True), Ok)


@pytest.mark.parametrize(
    "case",
    ["none", "sim_open", "drive_input_unread", "drive_input_old", "bridged_not_declared"],
)
async def test_an_acknowledgement_without_a_ready_reading_is_refused_under_every_other_source(
    case: str,
) -> None:
    clock = sto_rig().clock
    port = RegisterPort()
    port.result = Ok(RawRegister(1))
    polled = DriveInputStoSource(
        clock=clock, drive=port, address=RegisterAddress(5202), input_=LogicInput.LI1
    )
    sources: dict[str, StoSource] = {
        "none": UnconfiguredStoSource(),
        "sim_open": SimulatedStoSource(
            chain=SimulatedStoChain(ready=False), source=StoSourceKind.SIM_OPEN
        ),
        "drive_input_unread": DriveInputStoSource(
            clock=clock, drive=port, address=RegisterAddress(5202), input_=LogicInput.LI1
        ),
        "drive_input_old": polled,
        # A BRIDGED state from a source that does not declare it is not the declaration.
        "bridged_not_declared": MemorySto(
            StoReading(
                state=StoState.BRIDGED,
                source=StoSourceKind.DRIVE_INPUT,
                last_success_at=None,
                detail="not a declaration",
            )
        ),
    }
    await polled.poll()
    assert sto_ready(polled.latest(clock.monotonic()))
    rig = sto_rig(sto=sources[case])
    rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))  # the polled READY is now too old
    rig.runtime.request_estop("operator")
    await rig.step()
    refused = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, StoNotReady)
    assert rig.runtime.standing is not None


async def test_sto_open_without_a_reading_taken_since_is_still_refused_under_drive_input() -> None:
    """ANH-92 unchanged: only a READY reading taken after the latch acknowledges it."""
    source = MemorySto(
        StoReading(
            state=StoState.READY,
            source=StoSourceKind.DRIVE_INPUT,
            last_success_at=Monotonic(0.0),
            detail="relay",
        )
    )
    rig = sto_rig(sto=source)
    source.reading = replace(source.reading, last_success_at=rig.now)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, BENCH_RPM), Ok)
    before = rig.now
    source.reading = replace(source.reading, state=StoState.OPEN, last_success_at=rig.now)
    await rig.step()
    verdict = rig.runtime.standing
    assert verdict is not None
    assert verdict.rule == RULE_STO_OPEN
    source.reading = replace(source.reading, state=StoState.READY, last_success_at=before)
    refused = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(refused, Err)
    assert isinstance(refused.error, StoNotReady)
