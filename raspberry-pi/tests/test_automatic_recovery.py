import pytest

from src.clock import ManualClock
from src.motor.atv320 import ATV320Drive
from src.motor.drive import DriveState, RegisterMap
from src.result import Err, Ok
from src.training.runtime import IDLE_POLL_PERIOD, TrainingRuntime
from src.training.safety import RULE_COMMS_LOST, GoSilentIsTerminal, SafetyLimits
from src.training.types import Occupancy, SafetyAction
from src.units import Bpm, MotorRpm
from tests import test_atv320
from tests.test_atv320 import Behave, FakeBus, build_drive, parameter_exception
from tests.test_runtime import GEOMETRY, LIMITS

clock = test_atv320.clock
bus = test_atv320.bus
make_bus = test_atv320.make_bus


def runtime_for(clock: ManualClock, drive: ATV320Drive, failures: int = 3) -> TrainingRuntime:
    return TrainingRuntime(
        clock=clock,
        drive=drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=SafetyLimits(
            hard_max_bpm=Bpm(148), critical_bpm=Bpm(158), comms_lost_failures=failures
        ),
    )


@pytest.mark.parametrize("failures", [2, 3, 4])
async def test_failed_first_eta_recovers_automatically_then_requires_manual_intervention(
    clock: ManualClock, bus: FakeBus, failures: int
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive, failures)
    bus.sticky_reads = parameter_exception()
    for attempt in range(failures):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
        assert runtime.silent is (attempt + 1 == failures)
    assert bus.reads() == [RegisterMap().eta] * failures
    assert bus.writes() == []
    assert runtime.output_enabled
    assert runtime.snapshot().drive_state is DriveState.COMM_LOST
    verdict = runtime.standing
    assert verdict is not None
    assert verdict.rule == RULE_COMMS_LOST
    assert verdict.action is SafetyAction.GO_SILENT
    result = runtime.acknowledge("synthetic operator")
    assert isinstance(result, Err)
    assert isinstance(result.error, GoSilentIsTerminal)
    before = len(bus.log)
    assert isinstance(
        await runtime.start_manual(Occupancy.BENCH, "synthetic operator", MotorRpm(276)), Err
    )
    bus.sticky_reads = Behave.NORMALLY
    for _ in range(5):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
    report = await runtime.shutdown("synthetic terminal verification")
    assert report.silent
    assert not report.output_disabled
    assert report.emergency is None
    assert len(bus.log) == before


async def test_first_eta_failure_recovers_observation_without_motion(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    bus.script_reads.append(parameter_exception())
    await runtime.tick(clock.monotonic())
    assert runtime.idle_link.consecutive_failures == 1
    assert not runtime.silent
    clock.advance(IDLE_POLL_PERIOD)
    snapshot = await runtime.tick(clock.monotonic())
    assert snapshot.drive_state is DriveState.SWITCH_ON_DISABLED
    assert runtime.idle_link.reads == 1
    assert runtime.idle_link.consecutive_failures == 0
    assert not runtime.silent
    assert bus.writes() == []
    assert runtime.manual is None


async def test_local_connect_failures_retry_indefinitely_then_recover(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    bus.port_opens = False
    for _ in range(10):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
        assert not runtime.silent
    assert bus.log == []
    bus.port_opens = True
    clock.advance(IDLE_POLL_PERIOD)
    snapshot = await runtime.tick(clock.monotonic())
    assert snapshot.drive_state is DriveState.SWITCH_ON_DISABLED
    assert runtime.idle_link.consecutive_failures == 0
    assert bus.writes() == []


async def test_later_no_frame_failures_do_not_erase_an_outstanding_unknown_episode(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    bus.script_reads.append(parameter_exception())
    await runtime.tick(clock.monotonic())
    bus.port_opens = False
    for _ in range(2):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
    assert runtime.silent
    assert bus.reads() == [RegisterMap().eta]
    assert bus.writes() == []


async def test_proven_address_permits_only_one_terminal_emergency_zero(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    assert isinstance(await drive.open(), Ok)
    runtime = runtime_for(clock, drive)
    bus.sticky_reads = parameter_exception()
    for _ in range(3):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
    assert runtime.silent
    assert bus.writes() == [(RegisterMap().lfrd, 0)]
    assert runtime.output_enabled
    assert runtime.snapshot().drive_state is DriveState.COMM_LOST
    before = len(bus.log)
    report = await runtime.shutdown("terminal proven-address verification")
    assert report.silent
    assert not report.output_disabled
    assert len(bus.log) == before


async def test_successful_open_alone_does_not_reset_failed_status_episode(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    for _ in range(3):
        bus.script_reads.extend((Behave.NORMALLY, parameter_exception()))
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
    assert runtime.silent
    assert len(bus.reads()) == 6
    assert bus.writes() == [(RegisterMap().lfrd, 0)]


async def test_complete_status_ends_the_previous_unknown_episode(
    clock: ManualClock, bus: FakeBus
) -> None:
    drive = build_drive(clock, bus)
    runtime = runtime_for(clock, drive)
    bus.script_reads.append(parameter_exception())
    await runtime.tick(clock.monotonic())
    clock.advance(IDLE_POLL_PERIOD)
    await runtime.tick(clock.monotonic())
    bus.sticky_reads = parameter_exception()
    for _ in range(2):
        clock.advance(IDLE_POLL_PERIOD)
        await runtime.tick(clock.monotonic())
        assert not runtime.silent
    clock.advance(IDLE_POLL_PERIOD)
    await runtime.tick(clock.monotonic())
    assert runtime.silent
