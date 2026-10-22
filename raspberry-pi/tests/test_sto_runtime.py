from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Never, cast

import pytest

from src.cloud_sync import describe_surface_refusal
from src.local_panel import describe_start_refusal
from src.motor.drive import CommTimeout, ControlWord, DriveError, DriveLimits, LogicInput
from src.motor.simulated import SimulatedDrive
from src.motor.sto import (
    STO_STALE_AFTER,
    DriveInputStoSource,
    StoReading,
    StoSourceKind,
    StoState,
    UnconfiguredStoSource,
)
from src.result import Err, Ok, Result
from src.training.hr_control import HeartRateController
from src.training.preflight import StoNotReady, check_sto_ready, describe_sto_refusal
from src.training.runtime import (
    RULE_DRIVE_PRECOMMANDED,
    RULE_STO_OPEN,
    RULE_STO_UNREADABLE,
    EndReason,
    RuntimeState,
    SafetyStanding,
    Subject,
    TrainingRuntime,
)
from src.training.safety import SafetyLimits, Unattributed
from src.training.types import Occupancy, Phase, SafetyAction
from src.units import Bpm, Monotonic, MotorRpm, OutputRpm, RawRegister, RegisterAddress, Seconds
from tests import test_cloud_sync as dashboard_tests
from tests.roster_support import RIDER_ID
from tests.sto_support import ready_sto
from tests.test_failure_rig import PROGRAMME_ENV, attest, make_rig, start_manual
from tests.test_runtime import GEOMETRY, LIMITS, OPERATOR, FakeDrive, Rig
from tests.test_runtime import sto_rig as _rig
from tests.test_sto import RegisterPort


@dataclass(slots=True)
class MemorySto:
    """Mutable cached reading, changed only by the test between cooperative ticks."""

    reading: StoReading

    def latest(self, now: Monotonic) -> StoReading:
        del now
        return self.reading


def reading(state: StoState, at: Monotonic | None = None) -> StoReading:
    return StoReading(
        state=state,
        source=StoSourceKind.DRIVE_INPUT,
        last_success_at=at,
        detail="relay test detail",
    )


def controller_must_not_run(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("the safety verdict must decide before the controller")


async def reach(rig: Rig, phase: Phase) -> None:
    for _ in range(800):
        if rig.phase() is phase:
            return
        await rig.step()
    raise AssertionError(f"phase {phase} was never reached: {rig.phase()}")


@pytest.mark.parametrize("state", list(StoState))
def test_preflight_is_pure_and_preserves_the_state_and_detail(state: StoState) -> None:
    observed = reading(state)
    result = check_sto_ready(observed)
    if state is StoState.READY:
        assert result is None
    else:
        assert result is not None
        assert result == StoNotReady(state=state, detail=observed.detail)
        assert "STO" in describe_start_refusal(result)
        assert describe_surface_refusal(result) == describe_sto_refusal(result)
    assert observed == reading(state)


def test_preflight_words_are_exhaustive() -> None:
    assert describe_sto_refusal(StoNotReady(state=StoState.READY, detail="")) == "chaine STO prete"
    with pytest.raises(AssertionError):
        describe_sto_refusal(StoNotReady(state=cast("StoState", object()), detail=""))


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("state", [StoState.OPEN, StoState.UNKNOWN, StoState.NOT_CONFIGURED])
async def test_every_runtime_start_refuses_a_nonready_reading_without_touching_drive(
    state: StoState, manual: bool
) -> None:
    source = MemorySto(reading(state))
    rig = _rig(sto=source)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    result = (
        await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        if manual
        else await rig.runtime.start(rig.program, Subject(subject_id="s", operator=OPERATOR))
    )
    assert isinstance(result, Err)
    assert result.error == StoNotReady(state=state, detail=source.reading.detail)
    assert rig.drive.trace == []
    assert rig.state() is RuntimeState.IDLE


async def test_no_source_means_not_configured_and_never_ready() -> None:
    rig = _rig()
    runtime = TrainingRuntime(
        clock=rig.clock,
        drive=rig.drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=rig.runtime.supervisor.limits,
    )
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    result = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(result, Err)
    assert isinstance(result.error, StoNotReady)
    assert result.error.state is StoState.NOT_CONFIGURED
    assert rig.drive.trace == []


async def test_sto_is_revalidated_after_awaited_drive_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ready_sto()
    rig = _rig(sto=source)
    original = FakeDrive.read_limits

    async def read_limits(drive: FakeDrive) -> Result[DriveLimits, DriveError]:
        result = await original(drive)
        source.chain.open(rig.now)
        return result

    monkeypatch.setattr(FakeDrive, "read_limits", read_limits)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    result = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(result, Err)
    assert isinstance(result.error, StoNotReady)
    assert "cmd:ENABLE_OPERATION" not in rig.drive.trace
    assert not rig.runtime.output_enabled


@pytest.mark.parametrize("expired", [False, True])
async def test_real_cached_source_refuses_failure_and_age_on_both_start_paths(
    expired: bool,
) -> None:
    rig = _rig()
    port = RegisterPort()
    port.result = Ok(RawRegister(1))
    source = DriveInputStoSource(
        clock=rig.clock, drive=port, address=RegisterAddress(5202), input_=LogicInput.LI1
    )
    runtime = TrainingRuntime(
        clock=rig.clock,
        drive=rig.drive,
        geometry=GEOMETRY,
        limits=LIMITS,
        safety=rig.runtime.supervisor.limits,
        sto=source,
    )
    await source.poll()
    if expired:
        rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
    else:
        port.result = Err(CommTimeout(after=Seconds(0.1)))
        await source.poll()
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    for result in (
        await runtime.start(rig.program, Subject(subject_id="s", operator=OPERATOR)),
        await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)),
    ):
        assert isinstance(result, Err)
        assert isinstance(result.error, StoNotReady)
        assert result.error.state is StoState.UNKNOWN
    assert rig.drive.trace == []


@pytest.mark.parametrize(
    "phase", [Phase.BASELINE, Phase.WARMUP, Phase.HOLD, Phase.COOLDOWN, Phase.RECOVERY]
)
async def test_open_sto_wins_in_the_same_tick_in_every_programme_phase(
    phase: Phase, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ready_sto()
    rig = _rig(sto=source)
    rig.fed_bpm = Bpm(82)
    assert isinstance(await rig.start(), Ok)
    if phase not in (Phase.BASELINE, Phase.WARMUP):
        await reach(rig, Phase.WARMUP)
        rig.fed_bpm = Bpm(120)
    await reach(rig, phase)
    source.chain.open(rig.now)
    monkeypatch.setattr(HeartRateController, "update", controller_must_not_run)
    await rig.step()
    verdict = rig.standing()
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_STO_OPEN,
        SafetyAction.QUICK_STOP,
        True,
    )
    assert "hardware" in verdict.detail
    assert "cut torque" in verdict.detail
    assert rig.runtime.applied_rpm == rig.drive.commanded_rpm == 0
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT
    assert rig.state() is RuntimeState.ENDING


@pytest.mark.parametrize("stage", ["rise", "hold", "descent"])
async def test_open_sto_ends_each_manual_motion_phase(stage: str) -> None:
    source = ready_sto()
    rig = _rig(sto=source)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(100):
        await rig.step()
        if stage == "rise" and 0 < rig.runtime.applied_rpm < rig.runtime.manual_target:
            break
        if stage != "rise" and rig.runtime.applied_rpm == rig.runtime.manual_target:
            break
    if stage == "descent":
        assert isinstance(rig.runtime.set_manual_target(OutputRpm(0)), Ok)
        await rig.step()
        assert rig.runtime.applied_rpm > rig.runtime.manual_target
    elif stage == "rise":
        assert 0 < rig.runtime.applied_rpm < rig.runtime.manual_target
    else:
        assert rig.runtime.applied_rpm == rig.runtime.manual_target
    source.chain.open(rig.now)
    await rig.step()
    verdict = rig.standing()
    assert verdict is not None
    assert verdict.rule == RULE_STO_OPEN
    assert verdict.action is SafetyAction.QUICK_STOP
    assert rig.runtime.applied_rpm == rig.runtime.manual_target == rig.drive.commanded_rpm == 0
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT


async def test_a_single_failed_sto_read_does_not_end_motion_but_age_does() -> None:
    port = RegisterPort()
    port.result = Ok(RawRegister(1))
    rig = _rig()
    source = DriveInputStoSource(
        clock=rig.clock, drive=port, address=RegisterAddress(5202), input_=LogicInput.LI1
    )
    rig = replace(
        rig,
        runtime=TrainingRuntime(
            clock=rig.clock,
            drive=rig.drive,
            geometry=GEOMETRY,
            limits=LIMITS,
            safety=rig.runtime.supervisor.limits,
            sto=source,
        ),
    )
    await source.poll()
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(rig.runtime.set_manual_target(OutputRpm(3)), Ok)
    for _ in range(30):
        await source.poll()
        await rig.step()
    assert rig.runtime.applied_rpm > 0
    await source.poll()
    last = rig.now
    port.result = Err(CommTimeout(after=Seconds(0.1)))
    await source.poll()
    assert source.latest(rig.now).state is StoState.UNKNOWN
    for _ in range(9):
        await rig.step()
        assert rig.runtime.standing is None
    rig.clock.advance(Seconds(last + STO_STALE_AFTER - rig.now))
    await rig.runtime.tick(rig.now)
    assert rig.runtime.standing is None
    rig.clock.advance(Seconds(0.001))
    await rig.runtime.tick(rig.now)
    verdict = rig.standing()
    assert verdict is not None
    assert verdict.rule == RULE_STO_UNREADABLE
    assert verdict.action is SafetyAction.RAMP_DOWN
    assert verdict.latched
    assert rig.runtime.end_reason is EndReason.SAFETY_VERDICT


@pytest.mark.parametrize(
    "phase", [Phase.BASELINE, Phase.WARMUP, Phase.HOLD, Phase.COOLDOWN, Phase.RECOVERY]
)
async def test_never_read_sto_latches_during_possible_motion(
    phase: Phase, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = ready_sto()
    rig = _rig(sto=source)
    rig.fed_bpm = Bpm(82)
    assert isinstance(await rig.start(), Ok)
    if phase not in (Phase.BASELINE, Phase.WARMUP):
        await reach(rig, Phase.WARMUP)
        rig.fed_bpm = Bpm(120)
    await reach(rig, phase)

    def unreadable(_source: object, _now: Monotonic) -> StoReading:
        return reading(StoState.UNKNOWN)

    monkeypatch.setattr(type(source), "latest", unreadable)
    await rig.step()
    assert rig.runtime.standing is not None
    assert rig.runtime.standing.rule == RULE_STO_UNREADABLE


@pytest.mark.parametrize("configured", [False, True])
async def test_idle_unknown_speed_only_refuses_sto_start_without_latching(configured: bool) -> None:
    source = MemorySto(reading(StoState.UNKNOWN)) if configured else UnconfiguredStoSource()
    rig = _rig(sto=source)
    rig.drive.status_error = CommTimeout(after=Seconds(0.1))
    for _ in range(5):
        await rig.step()
    assert rig.standing() is None
    assert not rig.runtime.silent
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    refused = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, StoNotReady)
    assert refused.error.state is (StoState.UNKNOWN if configured else StoState.NOT_CONFIGURED)
    rig.drive.status_error = None
    await rig.run(2)
    assert rig.standing() is None


async def test_not_configured_is_not_a_lost_reading_even_during_an_armed_session() -> None:
    source = MemorySto(reading(StoState.READY))
    rig = _rig(sto=source)
    assert isinstance(await rig.start(), Ok)
    source.reading = reading(StoState.NOT_CONFIGURED)
    await rig.step()
    assert rig.standing() is None


@pytest.mark.parametrize("state", [StoState.OPEN, StoState.UNKNOWN, StoState.NOT_CONFIGURED])
async def test_any_acknowledgement_requires_ready_even_for_another_latch(state: StoState) -> None:
    source = MemorySto(reading(StoState.READY))
    rig = _rig(sto=source)
    rig.runtime.request_estop("operator")
    before = list(rig.drive.trace)
    source.reading = reading(state)
    result = rig.runtime.acknowledge(OPERATOR, estop_released=True)
    assert isinstance(result, Err)
    assert isinstance(result.error, StoNotReady)
    assert result.error.state is state
    assert rig.runtime.standing is not None
    unnamed = rig.runtime.acknowledge(" ", estop_released=True)
    assert isinstance(unnamed, Err)
    assert isinstance(unnamed.error, Unattributed)
    source.reading = reading(StoState.READY)
    assert isinstance(rig.runtime.acknowledge(OPERATOR, estop_released=True), Ok)
    assert rig.standing() is None
    assert rig.drive.trace == before


@pytest.mark.parametrize("unknown", [False, True])
async def test_idle_turning_or_unknown_speed_latches_open_sto(unknown: bool) -> None:
    source = ready_sto()
    rig = _rig(sto=source, rpm=MotorRpm(100) if not unknown else MotorRpm(0))
    if unknown:
        rig.drive.status_error = CommTimeout(after=Seconds(0.1))
    source.chain.open(rig.now)
    await rig.step()
    verdict = rig.standing()
    assert verdict is not None
    assert verdict.latched
    assert verdict.action is SafetyAction.QUICK_STOP
    assert verdict.rule == (RULE_STO_OPEN if unknown else RULE_DRIVE_PRECOMMANDED)
    source.chain.close(rig.now)
    assert isinstance(rig.runtime.confirm_estop_wiring(OPERATOR), Ok)
    result = await rig.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(result, Err)
    assert isinstance(result.error, SafetyStanding)


async def test_stale_idle_zero_is_not_proof_of_standstill() -> None:
    source = ready_sto()
    rig = _rig(
        sto=source,
        safety=SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158), comms_lost_failures=100),
    )
    await rig.step()
    rig.drive.status_error = CommTimeout(after=Seconds(0.1))
    await rig.run(2.2)
    assert rig.runtime.standing is None
    source.chain.open(rig.now)
    await rig.step()
    verdict = rig.standing()
    assert verdict is not None
    assert verdict.rule == RULE_STO_OPEN
    assert verdict.action is SafetyAction.QUICK_STOP


async def test_idle_standstill_open_and_close_only_gate_start(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    chain = rig.simulator.sto_chain
    assert chain is not None
    async with rig.http() as client:
        await attest(client)
        await rig.tick(0.2)
        chain.open(rig.clock.monotonic())
        await rig.tick(0.2)
        assert rig.panel.runtime.standing is None
        refused = await client.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert refused.status_code == 412
        assert refused.json()["code"] == "sto_not_ready"
        chain.close(rig.clock.monotonic())
        await rig.tick(0.2)
        assert rig.panel.runtime.standing is None
        accepted = await client.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert accepted.status_code == 202
        await rig.tick(0.2)
        assert rig.panel.runtime.state is RuntimeState.RUNNING
    await rig.panel.close()


@pytest.mark.parametrize(
    "phase", [RuntimeState.IDLE, RuntimeState.RUNNING, RuntimeState.ENDING, RuntimeState.FINISHED]
)
async def test_real_console_never_reactivates_after_close_without_acknowledgement(
    tmp_path: Path, phase: RuntimeState, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as client:
        if phase is RuntimeState.IDLE:
            await attest(client)
            assert isinstance(await rig.simulator.open(), Ok)
            for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
                assert isinstance(await rig.simulator.write_command(word), Ok)
            assert isinstance(await rig.simulator.write_speed(MotorRpm(100)), Ok)
            rig.simulator.advance(rig.clock.advance(Seconds(1)))
        else:
            await start_manual(rig, client, 3.0)
            await rig.tick(15)
            if phase in (RuntimeState.ENDING, RuntimeState.FINISHED):
                rig.panel.runtime.request_stop("operator")
                if phase is RuntimeState.ENDING:
                    await rig.tick(0.2)
                else:
                    await rig.tick(150)
            assert rig.panel.runtime.state is phase
            if phase is RuntimeState.FINISHED:
                for word in (
                    ControlWord.SHUTDOWN,
                    ControlWord.SWITCH_ON,
                    ControlWord.ENABLE_OPERATION,
                ):
                    assert isinstance(await rig.simulator.write_command(word), Ok)
                assert isinstance(await rig.simulator.write_speed(MotorRpm(100)), Ok)
                rig.simulator.advance(rig.clock.advance(Seconds(1)))
        chain = rig.simulator.sto_chain
        assert chain is not None
        chain.open(rig.clock.monotonic())
        await rig.tick(0.2)
        assert rig.panel.runtime.standing is not None
        assert rig.panel.runtime.applied_rpm == rig.simulator.commanded_setpoint == 0
        refused_ack = await client.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert refused_ack.status_code == 409
        assert "chaine STO ouverte" in refused_ack.text
        chain.close(rig.clock.monotonic())
        words: list[ControlWord] = []
        speeds: list[MotorRpm] = []
        original_command, original_speed = SimulatedDrive.write_command, SimulatedDrive.write_speed

        async def command(drive: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
            words.append(word)
            return await original_command(drive, word)

        async def speed(drive: SimulatedDrive, rpm: MotorRpm) -> Result[None, DriveError]:
            speeds.append(rpm)
            return await original_speed(drive, rpm)

        monkeypatch.setattr(SimulatedDrive, "write_command", command)
        monkeypatch.setattr(SimulatedDrive, "write_speed", speed)
        await rig.tick(150)
        assert rig.panel.runtime.applied_rpm == rig.simulator.commanded_setpoint == 0
        assert ControlWord.ENABLE_OPERATION not in words
        assert all(rpm == 0 for rpm in speeds)
        result = await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(result, Err)
        assert isinstance(result.error, SafetyStanding)
        acknowledged = await client.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert acknowledged.status_code == 200
        await rig.tick(0.4)
        assert rig.panel.runtime.applied_rpm == 0
        assert ControlWord.ENABLE_OPERATION not in words
        assert isinstance(
            await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
        )
    await rig.panel.close()


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("mode", ["open", "none", "never", "failed", "stale"])
async def test_local_http_starts_return_the_named_sto_refusal(
    tmp_path: Path, manual: bool, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sto_env = {"STO_SOURCE": {"open": "sim_open", "none": "none"}.get(mode, "drive_input")}
    if mode in ("never", "failed", "stale"):
        sto_env["STO_DRIVE_INPUT"] = "LI1"
    rig, _ = make_rig(tmp_path, env={**PROGRAMME_ENV, **sto_env})
    source = rig.panel.sto
    if mode in ("failed", "stale"):
        assert isinstance(source, DriveInputStoSource)
        await rig.panel.control_step()
        await source.poll()
        if mode == "failed":

            async def failed_read(
                _drive: SimulatedDrive, _address: RegisterAddress
            ) -> Result[RawRegister, DriveError]:
                return Err(CommTimeout(after=Seconds(0.1)))

            monkeypatch.setattr(SimulatedDrive, "read_register", failed_read)
            await rig.panel.control_step()
            await source.poll()
        else:
            rig.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
    async with rig.http() as client:
        await attest(client)
        response = await client.post(
            "/api/manual/start" if manual else "/api/session/start",
            json=(
                {"occupancy": "bench", "operator": OPERATOR}
                if manual
                else {"profile_id": "failure_short", "operator": OPERATOR, "subject_id": RIDER_ID}
            ),
        )
        assert response.status_code == 412
        assert response.json()["code"] == "sto_not_ready"
        assert "STO" in response.text
        assert rig.panel.surface.pending is None
    await rig.panel.close()


@pytest.mark.parametrize("mode", ["open", "none", "failed", "stale", "ready"])
async def test_dashboard_launch_reports_each_sto_refusal_before_confirming_start(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_kind = "drive_input" if mode in ("failed", "stale") else "sim_ready"
    r = dashboard_tests.rig(tmp_path, sto_source=source_kind)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    source = r.panel.sto
    sim = r.panel.drive.simulator
    assert sim is not None
    if isinstance(source, DriveInputStoSource):
        await r.panel.control_step()
        await source.poll()
    r.dashboard.answer(
        dashboard_tests.POLL_PATH, dashboard_tests.launch_answer(dict(dashboard_tests.LAUNCH))
    )
    await r.step(0.2)
    if isinstance(source, DriveInputStoSource):
        await r.panel.control_step()
    r.confirm_offer()
    if mode == "open":
        assert sim.sto_chain is not None
        sim.sto_chain.open(r.clock.monotonic())
    elif mode == "none":

        def unconfigured(_source: object, now: Monotonic) -> StoReading:
            del now
            return reading(StoState.NOT_CONFIGURED)

        monkeypatch.setattr(type(source), "latest", unconfigured)
    elif mode == "failed":

        async def failed_read(
            _drive: SimulatedDrive, _address: RegisterAddress
        ) -> Result[RawRegister, DriveError]:
            return Err(CommTimeout(after=Seconds(0.1)))

        monkeypatch.setattr(SimulatedDrive, "read_register", failed_read)
        assert isinstance(source, DriveInputStoSource)
        await r.panel.control_step()
        await source.poll()
    elif mode == "stale":
        r.clock.advance(Seconds(STO_STALE_AFTER + 0.001))
    await r.step()
    if mode == "ready":
        assert len(r.dashboard.to(dashboard_tests.START)) == 1
        assert r.panel.surface.pending is not None
    else:
        assert r.dashboard.to(dashboard_tests.START) == []
        assert r.panel.surface.pending is None
        ends = r.dashboard.to("/api/machine/training/end")
        assert len(ends) == 1
        assert ends[0]["failed"] is True
        assert "STO" in str(ends[0]["reason"])
    await r.panel.close()
