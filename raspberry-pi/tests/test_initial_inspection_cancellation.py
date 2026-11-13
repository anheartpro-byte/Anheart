"""An in-flight initial observation belongs to teardown, not to a cancelled caller."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import override

import httpx
import pytest

from src.clock import ManualClock
from src.local_panel import EXIT_FAILED
from src.motor.atv320 import SerialSettings, inspection_budget_for
from src.motor.drive import (
    CommTimeout,
    ControlWord,
    DriveError,
    DriveStatus,
    EmergencyStopOutcome,
)
from src.motor.simulated import SimState, SimulatedDrive, SimulatedDriveConfig
from src.result import Err, Ok, Result
from src.training.runtime import IDLE_POLL_PERIOD
from src.training.safety import RULE_COMMS_LOST
from src.training.types import SafetyAction
from src.units import Monotonic, MotorRpm, Seconds, UnixMillis
from tests.test_arming_cancellation import HeldCommand
from tests.test_failure_rig import attest, make_rig
from tests.test_local_panel import FakeWeb


class HeldInspection(HeldCommand):
    """Mutable events hold an initial reply; counts observe attempted motion writes."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.hold_open = False
        self.fail_open = False
        self.fail_before_open = False
        self.fail_read = False
        self.writes = 0
        self.reads = 0

    @override
    async def open(self) -> Result[None, DriveError]:
        result = Err(CommTimeout(Seconds(0.05))) if self.fail_before_open else await super().open()
        if self.fail_open:
            result = Err(CommTimeout(Seconds(0.05)))
        if self.hold_open and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return result

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self.reads += 1
        result = await self.inner.read_status()
        if self.fail_read:
            result = Err(CommTimeout(Seconds(0.05)))
        if not self.hold_open and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return result

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self.writes += 1
        return await super().write_speed(rpm)

    @override
    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self.writes += 1
        return await super().write_command(word)

    @override
    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self.writes += 1
        return super().emergency_disable_blocking(timeout)


@dataclass(frozen=True, slots=True)
class InspectionCase:
    hold_open: bool
    requested_start: bool
    parent_run: bool
    precommanded: bool


@pytest.mark.parametrize(
    "case",
    [
        InspectionCase(
            hold_open=hold_open,
            requested_start=requested_start,
            parent_run=parent_run,
            precommanded=precommanded,
        )
        for hold_open in (False, True)
        for requested_start in (False, True)
        for parent_run in (False, True)
        for precommanded in (False, True)
    ],
)
async def test_cancel_initial_inspection_stops_only_observed_motion(
    tmp_path: Path, case: InspectionCase
) -> None:
    # Given a held initial reply and either a normal idle or precommanded drive.
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.hold_open = case.hold_open
    if case.precommanded:
        opened = await rig.simulator.open()
        assert isinstance(opened, Ok)
        for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
            assert isinstance(await rig.simulator.write_command(word), Ok)
        assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if case.requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(
            rig.panel.run(stop, web) if case.parent_run else rig.panel.control_step()
        )
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            # When cancellation races the initial reply, before its caller sees it.
            task.cancel()
            if case.parent_run:
                await asyncio.wait_for(web.exit.wait(), 2)
                asyncio.get_running_loop().call_soon(held.release.set)
            else:
                held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await rig.panel.close()
            # Then observed motion is stopped; confirmed idle still gets no writes.
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            if not case.precommanded:
                assert held.writes == 0
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


@pytest.mark.parametrize("requested_start", [False, True])
@pytest.mark.parametrize("precommanded", [False, True])
async def test_cancelled_unreadable_acquired_drive_is_stopped_without_resumption(
    tmp_path: Path, requested_start: bool, precommanded: bool
) -> None:
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.fail_read = True
    if precommanded:
        opened = await rig.simulator.open()
        assert isinstance(opened, Ok)
        for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
            assert isinstance(await rig.simulator.write_command(word), Ok)
        assert isinstance(await rig.simulator.write_speed(MotorRpm(240)), Ok)
    async with rig.http() as client:
        if requested_start:
            await attest(client)
            response = await client.post(
                "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
            )
            assert response.status_code == 202
        task = asyncio.create_task(rig.panel.control_step())
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            task.cancel()
            held.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            await rig.panel.close()
            assert held.writes > 0
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
            exchanges = (held.reads, held.writes)
            await rig.panel.control_step()
            assert (held.reads, held.writes) == exchanges
        finally:
            held.release.set()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


async def _start_bench(client: httpx.AsyncClient) -> None:
    await attest(client)
    response = await client.post(
        "/api/manual/start", json={"operator": "synthetic operator", "occupancy": "bench"}
    )
    assert response.status_code == 202


async def _precommand(drive: SimulatedDrive) -> None:
    """A drive a crashed process left enabled at speed."""
    opened = await drive.open()
    assert isinstance(opened, Ok)
    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        assert isinstance(await drive.write_command(word), Ok)
    assert isinstance(await drive.write_speed(MotorRpm(240)), Ok)


class HeldBeforeReading(HeldInspection):
    """The status read is held before the drive is read: its reply is the drive at release."""

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self.reads += 1
        if not self.hold_open and not self.accepted.is_set():
            self.accepted.set()
            await self.release.wait()
        return await self.inner.read_status()


class OverTheRealLink(HeldBeforeReading):
    """As slow to answer as the real link may be: its inspection budget, about 5 s."""

    @property
    @override
    def inspection_budget(self) -> Seconds:
        return inspection_budget_for(SerialSettings(port="ftdi://ftdi:232r/1"))


class Stubborn(HeldInspection):
    """A held call that does not honour its first cancellation: a non-conforming drive."""

    @override
    async def open(self) -> Result[None, DriveError]:
        try:
            return await super().open()
        except asyncio.CancelledError:
            await self.release.wait()
            return await self.inner.open()

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        try:
            return await super().read_status()
        except asyncio.CancelledError:
            await self.release.wait()
            return await self.inner.read_status()


def _bound(rig_drive: HeldInspection, rig_gap: Seconds) -> Seconds:
    """How long after the stop the console waits for an inspection's reply."""
    return max(rig_drive.inspection_budget, rig_gap)


@pytest.mark.parametrize("ticked", [True, False], ids=["after a tick", "at the first tick"])
@pytest.mark.parametrize(
    ("requested_start", "hold_open"),
    [(False, False), (True, False), (True, True)],
    ids=["idle status read", "start status read", "start open"],
)
async def test_a_drive_call_that_never_returns_holds_the_stop_no_longer_than_its_budget(
    tmp_path: Path, requested_start: bool, hold_open: bool, ticked: bool
) -> None:
    """ANH-162: once the drive has had, since the stop, the longest it can take to answer
    an inspection, on the console's own clock, the stop stops waiting for it. Nothing more
    is sent, not a read (the drive's ttO owns any stop), and the stop completes, unclean,
    whether a tick had completed before or not."""
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    held.hold_open = hold_open
    if ticked:
        held.accepted.set()  # the first tick reads the drive and comes back
        await rig.panel.control_cycle()
        held.accepted.clear()  # the next inspection never returns
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if requested_start:
            await _start_bench(client)
        elif ticked:
            rig.clock.advance(IDLE_POLL_PERIOD)
        task = asyncio.create_task(rig.panel.run(stop, web))
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            reads, writes = held.reads, held.writes
            stop.set()  # systemd, or an operator, asks the console to stop
            await asyncio.wait_for(web.exit.wait(), 2)
            await asyncio.sleep(0.1)  # the stop reaches the inspection's owner
            bound = _bound(held, rig.panel.runtime.supervisor.limits.loop_stall_silent_gap)
            rig.clock.advance(Seconds(bound - 0.1))
            await asyncio.sleep(0.2)
            assert not task.done(), "the bound has not passed on the console's clock"
            rig.clock.advance(Seconds(0.2))
            # Not wait_for: it would wait for a cancelled stop that still waits on the drive.
            await asyncio.wait((task,), timeout=2)
            assert task.done(), "the controlled stop completed"
            assert task.result() == EXIT_FAILED
            runtime = rig.panel.runtime
            assert runtime.silent
            assert runtime.gave_up_on_the_link
            standing = runtime.standing
            assert standing is not None
            assert standing.rule == RULE_COMMS_LOST
            assert standing.action is SafetyAction.GO_SILENT
            assert "did not answer an inspection" in standing.detail
            assert (held.reads, held.writes) == (reads, writes), "not a frame, not a read"
            held.release.set()  # the call returns at last: its answer is dropped
            await asyncio.sleep(0.05)
            assert (held.reads, held.writes) == (reads, writes)
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


@pytest.mark.parametrize(
    ("requested_start", "hold_open"),
    [(False, False), (True, False), (True, True)],
    ids=["idle status read", "start status read", "start open"],
)
async def test_a_drive_that_answers_late_but_within_its_budget_is_still_stopped(
    tmp_path: Path, requested_start: bool, hold_open: bool
) -> None:
    """The stop waits for the reply as long as the drive may take to give it: a drive
    found enabled then is zeroed and switched off, as it always was."""
    rig, held = make_rig(tmp_path, wrap=HeldBeforeReading)
    assert isinstance(held, HeldBeforeReading)
    held.hold_open = hold_open
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if requested_start:
            await _start_bench(client)
        task = asyncio.create_task(rig.panel.run(stop, web))
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            stop.set()
            await asyncio.wait_for(web.exit.wait(), 2)
            await asyncio.sleep(0.1)
            bound = _bound(held, rig.panel.runtime.supervisor.limits.loop_stall_silent_gap)
            rig.clock.advance(Seconds(bound - 0.2))
            await asyncio.sleep(0.2)
            assert not task.done(), "it waits"
            await _precommand(rig.simulator)  # what the reply will show
            held.release.set()
            await asyncio.wait((task,), timeout=2)
            assert task.done()
            assert not rig.panel.runtime.gave_up_on_the_link
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


async def test_a_drive_that_answers_a_little_late_over_the_real_link_is_not_abandoned(
    tmp_path: Path,
) -> None:
    """The second reviewer's probe: a reply 3.1 s after the stop, past the supervisor's
    silence bound but within what the real link may take, from a drive left enabled at
    speed. It is zeroed and switched off, never left to its ttO."""
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    # The drive's own ttO as it leaves the factory, 10 s: not what stops it here.
    factory = SimulatedDriveConfig(reads_reset_watchdog=True, tto=Seconds(10.0))
    rig, held = make_rig(
        tmp_path, clock=clock, simulator=SimulatedDrive(clock, factory), wrap=OverTheRealLink
    )
    assert isinstance(held, OverTheRealLink)
    held.hold_open = True
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        await _start_bench(client)
        task = asyncio.create_task(rig.panel.run(stop, web))
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            stop.set()
            await asyncio.wait_for(web.exit.wait(), 2)
            await asyncio.sleep(0.1)
            rig.clock.advance(Seconds(3.1))
            await asyncio.sleep(0.2)
            assert not task.done()
            await _precommand(rig.simulator)
            held.release.set()
            await asyncio.wait((task,), timeout=2)
            assert task.done()
            assert not rig.panel.runtime.gave_up_on_the_link
            assert rig.simulator.commanded_setpoint == 0
            assert rig.simulator.sim_state is not SimState.OPERATION_ENABLED
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()


async def test_the_wait_counts_from_the_stop_not_from_the_inspection_s_start(
    tmp_path: Path,
) -> None:
    """An inspection already 10 s long when the stop comes is still given the drive's
    whole budget from the stop: never less than its own worst case, whenever it began."""
    rig, held = make_rig(tmp_path, wrap=HeldInspection)
    assert isinstance(held, HeldInspection)
    stop = asyncio.Event()
    web = FakeWeb()
    task = asyncio.create_task(rig.panel.run(stop, web))
    try:
        await asyncio.wait_for(held.accepted.wait(), 2)
        rig.clock.advance(Seconds(10.0))  # the inspection has run this long
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(web.exit.wait(), 2)
        await asyncio.sleep(0.1)
        bound = _bound(held, rig.panel.runtime.supervisor.limits.loop_stall_silent_gap)
        rig.clock.advance(Seconds(bound - 0.1))
        await asyncio.sleep(0.2)
        assert not task.done(), "the bound counts from the stop"
        rig.clock.advance(Seconds(0.2))
        await asyncio.wait((task,), timeout=2)
        assert task.done()
        assert rig.panel.runtime.silent
    finally:
        held.release.set()
        stop.set()
        web.request_exit()
        await asyncio.gather(task, return_exceptions=True)
        await rig.panel.close()


@pytest.mark.parametrize(
    ("requested_start", "hold_open"),
    [(False, False), (False, True), (True, False), (True, True)],
    ids=["idle status read", "idle open", "start status read", "start open"],
)
async def test_a_call_that_ignores_its_cancel_still_lets_the_stop_end_and_reads_nothing(
    tmp_path: Path, requested_start: bool, hold_open: bool
) -> None:
    """The second reviewer's probe: a call that does not honour its cancellation comes
    back after the console went silent. The stop has ended, and nothing more is read,
    nor believed of the link."""
    rig, held = make_rig(tmp_path, wrap=Stubborn)
    assert isinstance(held, Stubborn)
    held.hold_open = hold_open
    stop = asyncio.Event()
    web = FakeWeb()
    async with rig.http() as client:
        if requested_start:
            await _start_bench(client)
        task = asyncio.create_task(rig.panel.run(stop, web))
        try:
            await asyncio.wait_for(held.accepted.wait(), 2)
            reads, writes = held.reads, held.writes
            stop.set()
            await asyncio.wait_for(web.exit.wait(), 2)
            await asyncio.sleep(0.1)
            bound = _bound(held, rig.panel.runtime.supervisor.limits.loop_stall_silent_gap)
            rig.clock.advance(Seconds(bound + 0.1))
            await asyncio.wait((task,), timeout=2)
            assert task.done()
            assert rig.panel.runtime.silent
            held.release.set()  # the open comes back now, after the silence
            await asyncio.sleep(0.1)
            assert (held.reads, held.writes) == (reads, writes)
            assert not rig.panel.runtime.drive_link_acquired or not hold_open
        finally:
            held.release.set()
            stop.set()
            web.request_exit()
            await asyncio.gather(task, return_exceptions=True)
            await rig.panel.close()
