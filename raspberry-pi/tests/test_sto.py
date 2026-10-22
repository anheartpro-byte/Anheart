from __future__ import annotations

import asyncio
import inspect
import signal
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.clock import ManualClock
from src.local_config import LocalConfig, MotorBackend, default_sto_source, load_local_config
from src.local_panel import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_OK,
    build_panel,
    build_sto,
    install_stop_signals,
    main,
)
from src.motor.drive import (
    LOGIC_INPUTS_LOGICAL,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveState,
    EnableUnconfirmed,
    LogicInput,
    RegisterMap,
    StopUnconfirmed,
    UnexpectedState,
    decode_logic_input,
)
from src.motor.simulated import ETA_WORDS, SimState, SimulatedDrive, SimulatedDriveConfig
from src.motor.sto import (
    STO_POLL_PERIOD,
    STO_STALE_AFTER,
    DriveInputStoSource,
    SimulatedStoChain,
    SimulatedStoSource,
    StoSourceKind,
    StoState,
    UnconfiguredStoSource,
    sto_ready,
)
from src.result import Err, Ok, Result
from src.training import runtime
from src.units import (
    Amperes,
    Monotonic,
    MotorRpm,
    OutOfRange,
    RawRegister,
    RegisterAddress,
    Seconds,
)
from tests.test_failure_process import cruising
from tests.test_failure_rig import make_rig
from tests.test_local_config import BENCH_ENV, SIM_ENV
from tests.test_local_panel import FakeWeb
from tests.test_runtime import FakeDrive

ERRORS: Final[tuple[DriveError, ...]] = (
    CommTimeout(after=Seconds(0.1)),
    BadResponse(detail="private device text"),
    UnexpectedState(expected=DriveState.READY, actual=DriveState.FAULT),
    DriveFaulted(fault=DriveFault.MODBUS_COMM_LOSS, raw_code=RawRegister(5)),
    OutOfRange(value=1, low=0, high=0, quantity="register"),
    EnableUnconfirmed(detail="private", reference_zeroed=False, run_command_removed=False),
    StopUnconfirmed(waited=Seconds(1), last_output_rpm=MotorRpm(2), detail="private"),
)


def config_for(env: Mapping[str, str]) -> LocalConfig:
    loaded = load_local_config(env)
    assert isinstance(loaded, Ok), loaded
    return loaded.value


class RegisterPort:
    def __init__(self) -> None:
        self.result: Result[RawRegister, DriveError] = Ok(RawRegister(0))
        self.addresses: list[RegisterAddress] = []

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        self.addresses.append(address)
        return self.result


@pytest.mark.parametrize("input_", list(LogicInput))
@given(raw=st.integers(min_value=0, max_value=65535))
def test_logic_input_uses_only_its_own_bit(input_: LogicInput, raw: int) -> None:
    mask = 1 << input_
    assert decode_logic_input(RawRegister(raw | mask), input_)
    assert not decode_logic_input(RawRegister(raw & ~mask), input_)


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_logic_input_register_uses_the_single_map_offset(offset: int) -> None:
    assert RegisterMap(offset=offset).logic_inputs == 5202 + offset


@pytest.mark.parametrize("ready", [True, False])
def test_simulated_source_tracks_each_contact_change_immediately(ready: bool) -> None:
    chain = SimulatedStoChain(ready=ready)
    source = SimulatedStoSource(
        chain=chain, source=StoSourceKind.SIM_READY if ready else StoSourceKind.SIM_OPEN
    )
    assert sto_ready(source.latest(Monotonic(0))) is ready
    chain.open(Monotonic(1))
    reading = source.latest(Monotonic(1))
    assert reading.state is StoState.OPEN
    assert not sto_ready(reading)
    chain.close(Monotonic(2))
    assert sto_ready(source.latest(Monotonic(2)))
    assert source.latest(Monotonic(2)).last_success_at == Monotonic(2)


def test_unconfigured_source_never_attests_readiness() -> None:
    reading = UnconfiguredStoSource().latest(Monotonic(100))
    assert reading.state is StoState.NOT_CONFIGURED
    assert reading.source is StoSourceKind.NONE
    assert reading.last_success_at is None
    assert not sto_ready(reading)


@pytest.mark.parametrize(("raw", "state"), [(0, StoState.OPEN), (8, StoState.READY)])
async def test_drive_source_success_is_stamped_at_completion_then_expires(
    raw: int, state: StoState
) -> None:
    clock, port = ManualClock(), RegisterPort()
    source = DriveInputStoSource(
        clock=clock, drive=port, address=RegisterAddress(5201), input_=LogicInput.LI4
    )
    assert source.latest(clock.monotonic()).state is StoState.UNKNOWN
    assert not sto_ready(source.latest(clock.monotonic()))
    port.result = Ok(RawRegister(raw))
    clock.advance(Seconds(3))
    await source.poll()
    assert port.addresses == [5201]
    assert source.latest(clock.monotonic()).state is state
    assert source.latest(clock.monotonic()).last_success_at == Monotonic(3)
    clock.advance(STO_STALE_AFTER)
    assert source.latest(clock.monotonic()).state is state
    clock.advance(Seconds(0.001))
    assert source.latest(clock.monotonic()).state is StoState.UNKNOWN
    assert source.latest(clock.monotonic()).last_success_at == Monotonic(3)
    assert source.latest(clock.monotonic()).detail == "stale reading"
    assert port.addresses == [5201]


@pytest.mark.parametrize("error", ERRORS)
async def test_every_drive_error_invalidates_immediately_and_keeps_only_variant(
    error: DriveError,
) -> None:
    clock, port = ManualClock(), RegisterPort()
    source = DriveInputStoSource(
        clock=clock, drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )
    port.result = Err(error)
    await source.poll()
    assert source.latest(clock.monotonic()).last_success_at is None
    port.result = Ok(RawRegister(1))
    await source.poll()
    assert sto_ready(source.latest(clock.monotonic()))
    clock.advance(Seconds(0.1))
    port.result = Err(error)
    await source.poll()
    reading = source.latest(clock.monotonic())
    assert reading.state is StoState.UNKNOWN
    assert reading.last_success_at == Monotonic(0)
    assert reading.detail == type(error).__name__
    clock.advance(Seconds(10))
    assert source.latest(clock.monotonic()).detail == type(error).__name__


async def test_polling_loop_yields_at_technical_period_and_is_cancellable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, port = ManualClock(), RegisterPort()
    source = DriveInputStoSource(
        clock=clock, drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )
    periods: list[float] = []

    async def sleep(period: float) -> None:
        periods.append(period)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await source.run()
    assert len(port.addresses) == 1
    assert periods == [STO_POLL_PERIOD]


async def test_source_rejects_impossible_result_and_error_variants() -> None:
    port = RegisterPort()
    source = DriveInputStoSource(
        clock=ManualClock(), drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )
    port.result = cast("Result[RawRegister, DriveError]", object())
    with pytest.raises(AssertionError):
        await source.poll()
    port.result = Err(cast("DriveError", object()))
    with pytest.raises(AssertionError):
        await source.poll()


async def test_poll_stamps_completion_and_keeps_the_period_despite_read_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, port = ManualClock(), RegisterPort()
    source = DriveInputStoSource(
        clock=clock, drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )
    periods: list[float] = []

    async def delayed_read(
        _port: RegisterPort, _address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        clock.advance(Seconds(0.2))
        return Ok(RawRegister(1))

    async def sleep(period: float) -> None:
        periods.append(period)
        raise asyncio.CancelledError

    monkeypatch.setattr(RegisterPort, "read_register", delayed_read)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await source.run()
    assert source.latest(clock.monotonic()).last_success_at == Monotonic(0.2)
    assert periods == [0.3]


async def enable(drive: SimulatedDrive) -> None:
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await drive.write_command(word), Ok)


async def test_sto_cuts_torque_without_polling_and_never_restarts_on_closing() -> None:
    clock, chain = ManualClock(), SimulatedStoChain()
    drive = SimulatedDrive(clock, sto_chain=chain, initial_rpm=MotorRpm(900))
    assert isinstance(await drive.open(), Ok)
    await enable(drive)
    assert isinstance(await drive.write_speed(MotorRpm(900)), Ok)
    chain.open(clock.monotonic())
    assert drive.sim_state is SimState.SWITCH_ON_DISABLED
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.status_word == ETA_WORDS[SimState.SWITCH_ON_DISABLED]
    assert status.value.current == Amperes(0.0)
    assert status.value.output_rpm == 900
    assert isinstance(await drive.write_speed(MotorRpm(1300)), Ok)
    clock.advance(Seconds(1))
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert 0 < status.value.output_rpm < 900
    assert status.value.current == Amperes(0.0)
    coasting = status.value.output_rpm
    chain.close(clock.monotonic())
    clock.advance(Seconds(1))
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.state is DriveState.SWITCH_ON_DISABLED
    assert status.value.current == Amperes(0.0)
    assert status.value.output_rpm < coasting
    assert isinstance(await drive.write_command(ControlWord.ENABLE_OPERATION), Err)
    await enable(drive)
    enabled = await drive.read_status()
    assert isinstance(enabled, Ok)
    assert enabled.value.state is DriveState.OPERATION_ENABLED


async def test_simulated_drive_open_chain_refuses_enable_and_models_only_relay_register() -> None:
    clock, chain = ManualClock(), SimulatedStoChain(ready=False)
    drive = SimulatedDrive(clock, SimulatedDriveConfig(sto_input=LogicInput.LI6), sto_chain=chain)
    assert isinstance(await drive.read_register(LOGIC_INPUTS_LOGICAL), Err)
    assert isinstance(await drive.open(), Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        refused = await drive.write_command(word)
        assert isinstance(refused, Err)
        assert isinstance(refused.error, UnexpectedState)
        assert refused.error.actual is DriveState.SWITCH_ON_DISABLED
    assert await drive.read_register(LOGIC_INPUTS_LOGICAL) == Ok(RawRegister(0))
    chain.close(clock.monotonic())
    assert await drive.read_register(LOGIC_INPUTS_LOGICAL) == Ok(RawRegister(32))
    unsupported = await drive.read_register(RegisterAddress(5203))
    assert isinstance(unsupported, Err)
    assert isinstance(unsupported.error, BadResponse)
    with pytest.raises(ValueError, match="already connected"):
        drive.connect_sto(chain)
    bare = SimulatedDrive(clock)
    assert isinstance(await bare.open(), Ok)
    assert await bare.read_register(LOGIC_INPUTS_LOGICAL) == Ok(RawRegister(0))


@pytest.mark.parametrize(
    ("backend", "expected"),
    [(MotorBackend.SIM, StoSourceKind.SIM_READY), (MotorBackend.SERIAL, StoSourceKind.NONE)],
)
def test_provisional_default_is_isolated(backend: MotorBackend, expected: StoSourceKind) -> None:
    assert default_sto_source(backend) is expected
    assert config_for(SIM_ENV if backend is MotorBackend.SIM else BENCH_ENV).sto_source is expected


@pytest.mark.parametrize("source", list(StoSourceKind))
@pytest.mark.parametrize("input_", list(LogicInput))
async def test_config_and_panel_build_each_source_with_shared_simulated_chain(
    tmp_path: Path, source: StoSourceKind, input_: LogicInput
) -> None:
    env = {**SIM_ENV, "STO_SOURCE": source.value}
    if source is StoSourceKind.DRIVE_INPUT:
        env["STO_DRIVE_INPUT"] = input_.name
    config = config_for(env)
    clock = ManualClock()
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "profiles.json")
    reading = panel.sto.latest(clock.monotonic())
    assert reading.source is source
    simulator = panel.drive.simulator
    assert simulator is not None
    chain = simulator.sto_chain
    assert chain is not None
    if isinstance(panel.sto, DriveInputStoSource):
        assert isinstance(await simulator.open(), Ok)
        await panel.sto.poll()
        assert sto_ready(panel.sto.latest(clock.monotonic()))
        chain.open(clock.monotonic())
        await panel.sto.poll()
        assert panel.sto.latest(clock.monotonic()).state is StoState.OPEN
    elif source is StoSourceKind.NONE:
        assert reading.state is StoState.NOT_CONFIGURED
    else:
        assert sto_ready(reading) is (source is StoSourceKind.SIM_READY)
        chain.open(clock.monotonic())
        assert panel.sto.latest(clock.monotonic()).state is StoState.OPEN
    await panel.close()


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"STO_SOURCE": "wrong"}, "STO_SOURCE"),
        ({"STO_SOURCE": "drive_input"}, "STO_DRIVE_INPUT"),
        ({"STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI7"}, "STO_DRIVE_INPUT"),
        ({"STO_SOURCE": "none", "STO_DRIVE_INPUT": "LI1"}, "STO_DRIVE_INPUT"),
        ({"STO_SOURCE": "none", "STO_DRIVE_INPUT": "wrong"}, "STO_DRIVE_INPUT"),
    ],
)
def test_sto_config_collects_problems(changes: dict[str, str], key: str) -> None:
    loaded = load_local_config({**SIM_ENV, **changes, "ARM_RADIUS_M": "invalid"})
    assert isinstance(loaded, Err)
    assert {key, "ARM_RADIUS_M"} <= {problem.key for problem in loaded.error}
    if changes["STO_SOURCE"] == "wrong":
        problem = next(problem for problem in loaded.error if problem.key == key)
        assert all(source.value in problem.detail for source in StoSourceKind)


@pytest.mark.parametrize("source", ["sim_ready", "sim_open"])
def test_simulated_sto_in_front_of_serial_drive_refuses_console_start(source: str) -> None:
    env = {**BENCH_ENV, "STO_SOURCE": source}
    loaded = load_local_config(env)
    assert isinstance(loaded, Err)
    assert any(problem.key == "STO_SOURCE" for problem in loaded.error)
    assert main(env) == EXIT_CONFIG


def test_serial_drive_input_uses_its_register_offset_and_checks_required_composition() -> None:
    config = config_for({**BENCH_ENV, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI2"})
    clock, drive = ManualClock(), SimulatedDrive(ManualClock())
    assert isinstance(build_sto(config, clock, drive, None), DriveInputStoSource)
    with pytest.raises(ValueError, match="STO_DRIVE_INPUT"):
        build_sto(replace(config, sto_drive_input=None), clock, drive, None)
    with pytest.raises(TypeError, match="register-reading"):
        build_sto(config, clock, FakeDrive(clock), None)
    with pytest.raises(ValueError, match="shared simulated"):
        build_sto(replace(config, sto_source=StoSourceKind.SIM_READY), clock, drive, None)
    with pytest.raises(AssertionError):
        build_sto(replace(config, sto_source=cast("StoSourceKind", object())), clock, drive, None)
    with pytest.raises(AssertionError):
        default_sto_source(cast("MotorBackend", object()))


async def test_serial_composition_resolves_the_input_address_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_for(
        {
            **BENCH_ENV,
            "STO_SOURCE": "drive_input",
            "STO_DRIVE_INPUT": "LI2",
            "MOTOR_REG_OFFSET": "-1",
        }
    )
    clock = ManualClock()
    drive = SimulatedDrive(clock)
    addresses: list[RegisterAddress] = []

    async def read(
        _drive: SimulatedDrive, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        addresses.append(address)
        return Ok(RawRegister(2))

    monkeypatch.setattr(SimulatedDrive, "read_register", read)
    source = build_sto(config, clock, drive, None)
    assert isinstance(source, DriveInputStoSource)
    await source.poll()
    assert addresses == [5201]
    assert sto_ready(source.latest(clock.monotonic()))


@pytest.mark.parametrize("initial_rpm", [MotorRpm(0), MotorRpm(500)])
async def test_opening_and_closing_sto_preserves_latched_fault_and_lft(
    initial_rpm: MotorRpm,
) -> None:
    clock, chain = ManualClock(), SimulatedStoChain()
    drive = SimulatedDrive(clock, sto_chain=chain, initial_rpm=initial_rpm)
    assert isinstance(await drive.open(), Ok)
    drive.inject_fault(DriveFault.MODBUS_COMM_LOSS)
    before = await drive.read_status()
    assert isinstance(before, Ok)
    chain.open(clock.monotonic())
    chain.close(clock.monotonic())
    for _ in range(2):
        clock.advance(Seconds(0.2))
        after = await drive.read_status()
        assert isinstance(after, Ok)
        assert after.value.state is DriveState.FAULT
        assert after.value.fault_code == before.value.fault_code
        assert after.value.current == Amperes(0.0)
        assert abs(after.value.output_rpm) <= abs(initial_rpm)
    assert isinstance(await drive.write_command(ControlWord.SHUTDOWN), Err)
    clock.advance(Seconds(200))
    assert isinstance(await drive.write_command(ControlWord.FAULT_RESET), Ok)
    assert drive.sim_state is SimState.SWITCH_ON_DISABLED
    await enable(drive)


async def test_a_fault_arriving_after_sto_open_cannot_restore_torque() -> None:
    clock, chain = ManualClock(), SimulatedStoChain()
    drive = SimulatedDrive(clock, sto_chain=chain, initial_rpm=MotorRpm(500))
    assert isinstance(await drive.open(), Ok)
    chain.open(clock.monotonic())
    drive.inject_fault(DriveFault.MODBUS_COMM_LOSS)
    assert drive.sim_state is SimState.FAULT_REACTION_FREEWHEEL
    clock.advance(Seconds(0.2))
    status = await drive.read_status()
    assert isinstance(status, Ok)
    assert status.value.current == Amperes(0.0)
    assert status.value.output_rpm < 500


def test_opening_sto_cuts_torque_with_the_drive_link_disconnected() -> None:
    clock, chain = ManualClock(), SimulatedStoChain()
    drive = SimulatedDrive(
        clock, sto_chain=chain, initial_state=SimState.OPERATION_ENABLED, initial_rpm=MotorRpm(500)
    )
    chain.open(clock.monotonic())
    assert drive.sim_state is SimState.SWITCH_ON_DISABLED
    chain.close(clock.monotonic())
    assert drive.sim_state is SimState.SWITCH_ON_DISABLED


def test_poll_is_absent_from_training_modules_by_construction() -> None:
    for module in Path(inspect.getfile(runtime)).parent.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert ".poll(" not in source
        assert ".read_register(" not in source


@pytest.mark.parametrize("ending", ["stop", "cancel", "web_failure", "exception", "sigterm"])
async def test_sto_polling_task_is_owned_and_cancelled_on_every_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    config = config_for({**SIM_ENV, "STO_SOURCE": "drive_input", "STO_DRIVE_INPUT": "LI1"})
    clock = ManualClock()
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json")
    entered, cleaned, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked_read(
        _drive: SimulatedDrive, _address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        entered.set()
        try:
            await release.wait()
            raise RuntimeError("poll failure")
        finally:
            cleaned.set()

    monkeypatch.setattr(SimulatedDrive, "read_register", blocked_read)
    before = asyncio.all_tasks()
    stop = asyncio.Event()
    web = FakeWeb(fail_at_start=ending == "web_failure")
    runner = asyncio.create_task(panel.run(stop, web))
    await asyncio.wait_for(entered.wait(), 2)
    children = asyncio.all_tasks() - before - {runner}
    assert any(child.get_name() == "sto-poll" for child in children)
    if ending == "cancel":
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 2)
    else:
        if ending == "exception":
            release.set()
        elif ending == "sigterm":
            remove = install_stop_signals(stop)
            try:
                signal.raise_signal(signal.SIGTERM)
                await asyncio.sleep(0)
            finally:
                remove()
        elif ending == "stop":
            stop.set()
        assert await asyncio.wait_for(runner, 2) == (
            EXIT_FAILED if ending in ("web_failure", "exception") else EXIT_OK
        )
    assert cleaned.is_set()
    assert all(child.done() for child in children)
    assert stop.is_set()


@pytest.mark.parametrize("ending", ["stop", "cancel", "web_failure"])
async def test_blocked_sto_poll_does_not_delay_stopping_a_running_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    from_env = {
        **SIM_ENV,
        "STO_SOURCE": "drive_input",
        "STO_DRIVE_INPUT": "LI1",
        "MOTOR_MAX_RPM": "1380",
    }
    rig, _ = make_rig(tmp_path, env=from_env)
    await cruising(rig)
    assert rig.simulator.commanded_setpoint > 0
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def blocked_read(
        _drive: SimulatedDrive, _address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        entered.set()
        try:
            await asyncio.Event().wait()
            return Ok(RawRegister(1))
        finally:
            cleaned.set()

    monkeypatch.setattr(SimulatedDrive, "read_register", blocked_read)
    stop = asyncio.Event()
    web = FakeWeb(fail_at_start=ending == "web_failure")
    runner = asyncio.create_task(rig.panel.run(stop, web))
    await asyncio.wait_for(entered.wait(), 2)
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
    assert cleaned.is_set()
    assert rig.simulator.commanded_setpoint == 0
    assert await rig.left_stopped() == ""
