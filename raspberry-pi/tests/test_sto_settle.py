from pathlib import Path

import pytest

from src.motor.drive import CommTimeout, ControlWord, DriveError
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Err, Ok, Result
from src.training.preflight import StoNotReady
from src.training.runtime import (
    RULE_DISABLE_REFUSED,
    RULE_STO_OPEN,
    EndReason,
    RuntimeState,
    SafetyStanding,
    Subject,
)
from src.training.types import Occupancy, Phase, SafetyAction, SignalQuality, TelemetrySnapshot
from src.units import Bpm, MotorRpm, OutputRpm, Seconds
from tests.roster_support import RIDER_ID
from tests.test_failure_rig import PROGRAMME_ENV, attest, make_rig
from tests.test_runtime import OPERATOR, sto_rig


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("through_panel", [False, True])
async def test_sto_held_open_until_standstill_keeps_only_its_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual: bool, through_panel: bool
) -> None:
    rig, _ = make_rig(tmp_path, env=PROGRAMME_ENV)
    runtime = rig.panel.runtime
    chain = rig.simulator.sto_chain
    assert chain is not None
    programme = sto_rig().program
    seq = 0

    async def step() -> TelemetrySnapshot:
        nonlocal seq
        now = rig.clock.advance(Seconds(0.2))
        seq += 1
        runtime.observe_ecg(now, seq, SignalQuality.GOOD, Bpm(82))
        runtime.note_presence(now)
        return await rig.panel.control_step() if through_panel else await runtime.tick(now)

    async with rig.http() as client:
        await attest(client)
        if through_panel:
            response = await client.post(
                "/api/manual/start" if manual else "/api/session/start",
                json=(
                    {"occupancy": "bench", "operator": OPERATOR}
                    if manual
                    else {
                        "profile_id": "failure_short",
                        "operator": OPERATOR,
                        "subject_id": RIDER_ID,
                    }
                ),
            )
            assert response.status_code == 202, response.text
            await step()
        elif manual:
            assert isinstance(
                await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
            )
        else:
            assert isinstance(
                await runtime.start(programme, Subject(subject_id="s", operator=OPERATOR)), Ok
            )
        if manual:
            assert isinstance(runtime.set_manual_target(OutputRpm(3)), Ok)
        for _ in range(800):
            snapshot = await step()
            if snapshot.measured.motor_rpm > 10 and (manual or snapshot.phase is Phase.WARMUP):
                break
        else:
            raise AssertionError("the session never reached the injection phase while turning")
        assert snapshot.phase is (Phase.HOLD if manual else Phase.WARMUP)
        words: list[ControlWord] = []
        speeds: list[MotorRpm] = []
        original_command = SimulatedDrive.write_command
        original_speed = SimulatedDrive.write_speed

        async def command(drive: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
            words.append(word)
            return await original_command(drive, word)

        async def speed(drive: SimulatedDrive, rpm: MotorRpm) -> Result[None, DriveError]:
            speeds.append(rpm)
            return await original_speed(drive, rpm)

        monkeypatch.setattr(SimulatedDrive, "write_command", command)
        monkeypatch.setattr(SimulatedDrive, "write_speed", speed)
        chain.open(rig.clock.monotonic())
        assert rig.simulator.sim_state is SimState.SWITCH_ON_DISABLED
        await step()
        assert speeds[-1] == 0
        speeds.clear()
        for _ in range(1100):
            snapshot = await step()
            assert not runtime.silent
            assert snapshot.safety is not None
            assert (snapshot.safety.rule, snapshot.safety.action, snapshot.safety.latched) == (
                RULE_STO_OPEN,
                SafetyAction.QUICK_STOP,
                True,
            )
            assert runtime.applied_rpm == rig.simulator.commanded_setpoint == 0
        assert snapshot.measured.motor_rpm == 0
        assert not runtime.output_enabled
        assert runtime.end_reason is EndReason.SAFETY_VERDICT
        assert runtime.state is RuntimeState.FINISHED
        assert words == []
        assert all(rpm == 0 for rpm in speeds)
        refused = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(refused, Err)
        assert isinstance(refused.error, SafetyStanding)
        acknowledged = runtime.acknowledge(OPERATOR, estop_released=True)
        assert isinstance(acknowledged, Err)
        assert isinstance(acknowledged.error, StoNotReady)
        chain.close(rig.clock.monotonic())
        assert isinstance(runtime.acknowledge(OPERATOR, estop_released=True), Ok)
        for _ in range(10):
            await step()
        assert runtime.applied_rpm == rig.simulator.commanded_setpoint == 0
        assert words == []
        assert isinstance(await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
        assert words == [ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION]
    await runtime.shutdown("test complete")
    await rig.panel.close()


async def test_ready_sto_does_not_hide_a_drive_that_refuses_output_removal() -> None:
    source = sto_rig()
    runtime = source.runtime
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert isinstance(await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(3)), Ok)
    await source.run(15)
    source.drive.command_errors[ControlWord.SWITCH_ON] = CommTimeout(after=Seconds(0.1))
    source.drive.command_errors[ControlWord.SHUTDOWN] = CommTimeout(after=Seconds(0.1))
    runtime.request_stop("operator")
    await source.run(30)
    assert runtime.silent
    verdict = source.standing()
    assert verdict is not None
    assert (verdict.rule, verdict.action, verdict.latched) == (
        RULE_DISABLE_REFUSED,
        SafetyAction.GO_SILENT,
        True,
    )
