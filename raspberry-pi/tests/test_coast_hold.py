"""The coast-down hold (ANH-92): no start and no fault reset while a shaft may still coast.

RFRD, the drive's output frequency, reads 0 with the output stage off, a coast
included. Once a read shows the stage dropped after one that drove the shaft, the
console holds every start and fault reset for ``COAST_TAU_S`` x ln(v), v the last
speed read plus one read period of the fastest ramp, capped at the nameplate. The
hold is no latch, is kept across a restart, and keeps the ending in COOLDOWN.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, final, override

import httpx
import pytest

from src import kept_coast_hold
from src.clock import ManualClock
from src.cloud_sync import BenchLaunch
from src.kept_coast_hold import COAST_HOLD_FILE, HoldKeeper, kept_hold, stored_hold
from src.local_config import coast_tau
from src.local_panel import describe_reset_refusal, describe_start_refusal
from src.motor.coast import (
    DEFAULT_COAST_TAU,
    FASTEST_RAMP,
    PLATE_RPM,
    CoastStart,
    CoastWatch,
    coast_hold,
    hold_cap,
    longest_hold,
)
from src.motor.drive import CommTimeout, DriveError, DriveFault, DriveState, DriveStatus
from src.motor.simulated import SimState, SimulatedDrive
from src.motor.sto import SimulatedStoChain
from src.result import Err, Ok, Result
from src.training.runtime import CoastDownHeld, CoastDownHold, RuntimeLimits, RuntimeState
from src.training.types import Ballast, Occupancy, Phase
from src.units import (
    Amperes,
    Monotonic,
    MotorRpm,
    OutputRpm,
    RpmPerSecond,
    Seconds,
    StatusWord,
    UnixMillis,
)
from src.wall_clock import Synchronised, Unsynchronised, WallClockProbe, WallClockState
from src.web.schemas import SnapshotRow
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    Rig,
    Wrapped,
    make_rig,
    precommanded,
    start_manual,
)
from tests.test_out_of_service_console import console

TAU: Final[Seconds] = DEFAULT_COAST_TAU
ONE: Final[MotorRpm] = MotorRpm(1)
TICK: Final[Seconds] = Seconds(0.2)
IDLE_POLL: Final[Seconds] = Seconds(0.5)
START_MS: Final[int] = 1_700_000_000_000
HOUR_MS: Final[int] = 3_600_000
MIN_RUN: Final[MotorRpm] = MotorRpm(55)


def status(state: DriveState, rpm: int, lfrd: int = 0, *, word: int = 0x0637) -> DriveStatus:
    return DriveStatus(
        state=state,
        status_word=StatusWord(word),
        setpoint_echo_rpm=MotorRpm(lfrd),
        output_rpm=MotorRpm(rpm),
        current=Amperes(0.0),
        fault=None,
        fault_code=None,
    )


# =========================================================================
# The law
# =========================================================================


def test_the_hold_is_tau_ln_of_the_speed_and_one_read_of_the_fastest_ramp() -> None:
    assert FASTEST_RAMP == 345.0
    assert coast_hold(MotorRpm(149), since_read=TICK, tau=TAU, standstill=ONE) == pytest.approx(
        40.0 * math.log(149 + 69)
    )
    assert coast_hold(
        MotorRpm(-149), since_read=IDLE_POLL, tau=TAU, standstill=ONE
    ) == pytest.approx(40.0 * math.log(149 + 172.5))
    assert coast_hold(MotorRpm(149), since_read=Seconds(0.0), tau=TAU, standstill=ONE) == (
        pytest.approx(40.0 * math.log(149))
    )
    assert coast_hold(MotorRpm(0), since_read=Seconds(0.0), tau=TAU, standstill=ONE) == 0.0


def test_no_hold_is_longer_than_one_from_the_nameplate() -> None:
    cap = hold_cap(TAU, ONE)
    assert cap == pytest.approx(40.0 * math.log(1380.0))
    assert round(cap) == 289
    assert coast_hold(PLATE_RPM, since_read=TICK, tau=TAU, standstill=ONE) == cap
    assert coast_hold(MotorRpm(65_000), since_read=TICK, tau=TAU, standstill=ONE) == cap


# =========================================================================
# What starts one
# =========================================================================


@pytest.mark.parametrize(
    "dropped",
    [
        DriveState.SWITCH_ON_DISABLED,  # the STO
        DriveState.FAULT,  # a fault that freewheels
        DriveState.READY,  # word 6
        DriveState.SWITCHED_ON,  # word 7's ramp over, or a drop seen there
    ],
    ids=["sto", "freewheel_fault", "word_6", "switched_on"],
)
def test_a_read_with_the_stage_off_after_one_that_drove_the_shaft_starts_a_hold(
    dropped: DriveState,
) -> None:
    watch = CoastWatch(tau=TAU, standstill=ONE)
    assert watch.note(status(DriveState.OPERATION_ENABLED, 1000, 1000), TICK) is None
    started = watch.note(status(dropped, 0, 1000), TICK)
    assert started == CoastStart(
        coast_hold(MotorRpm(1000), since_read=TICK, tau=TAU, standstill=ONE),
        "the output stage dropped at 1000 motor rpm, LFRD 1000",
    )
    # Once: the next read with the stage still off starts nothing more.
    assert watch.note(status(dropped, 0, 1000), TICK) is None


def test_rfrd_showing_motion_is_the_stage_on_whatever_the_status_word() -> None:
    """A fault reaction that ramps shows FAULT with RFRD falling: the stage still drives."""
    watch = CoastWatch(tau=TAU, standstill=ONE)
    assert watch.note(status(DriveState.FAULT, 600), TICK) is None
    started = watch.note(status(DriveState.FAULT, 0), TICK)
    assert started is not None
    assert started.hold == coast_hold(MotorRpm(600), since_read=TICK, tau=TAU, standstill=ONE)


def test_a_stop_that_ends_at_standstill_before_the_stage_drops_holds_nothing() -> None:
    """The console's own stop: the ramp to 0 under torque, then 7 and 6 at standstill."""
    watch = CoastWatch(tau=TAU, standstill=ONE)
    for rpm in (300, 120, 10):
        assert watch.note(status(DriveState.OPERATION_ENABLED, rpm, 0), TICK) is None
    assert watch.note(status(DriveState.OPERATION_ENABLED, 0, 0), TICK) is None
    assert watch.note(status(DriveState.SWITCHED_ON, 0, 0), TICK) is None
    assert watch.note(status(DriveState.READY, 0, 0), TICK) is None


def test_the_first_read_with_the_stage_off_and_a_reference_holds_from_the_reference() -> None:
    """Another master, or a process that died, left LFRD: nothing says how fast it turned."""
    watch = CoastWatch(tau=TAU, standstill=ONE)
    started = watch.note(status(DriveState.SWITCH_ON_DISABLED, 0, -149), IDLE_POLL)
    assert started == CoastStart(
        Seconds(40.0 * math.log(149)), "found with the stage off and LFRD at 149"
    )
    quiet = CoastWatch(tau=TAU, standstill=ONE)
    assert quiet.note(status(DriveState.SWITCH_ON_DISABLED, 0, 0), IDLE_POLL) is None
    # Only the first read: a reference read later is this console's own business.
    assert quiet.note(status(DriveState.SWITCH_ON_DISABLED, 0, 149), IDLE_POLL) is None


# =========================================================================
# The real console
# =========================================================================


@final
class Fixed(WallClockProbe):
    """A wall clock whose standing never changes."""

    def __init__(self, state: WallClockState) -> None:
        self._state = state

    @override
    def read(self) -> WallClockState:
        return self._state


TRUSTED: Final[Fixed] = Fixed(Synchronised(estimated_error=Seconds(0.01), max_error=Seconds(0.1)))
UNTRUSTED: Final[Fixed] = Fixed(Unsynchronised())


def chain_of(rig: Rig) -> SimulatedStoChain:
    chain = rig.simulator.sto_chain
    assert chain is not None
    return chain


def held(rig: Rig) -> Seconds | None:
    """Read afresh: a checker would otherwise keep a narrowing across an ``await``."""
    return rig.panel.runtime.snapshot().coast_hold


def may_coast(rig: Rig) -> bool:
    """Read afresh, as :func:`held`."""
    return rig.panel.runtime.coast_possible


def state_of(rig: Rig) -> RuntimeState:
    return rig.panel.runtime.state


async def at_speed(rig: Rig, session: httpx.AsyncClient) -> MotorRpm:
    """A bench session at 3 output rpm; the last RFRD the console read."""
    await start_manual(rig, session, 3.0)
    await rig.tick(30.0)
    rpm = rig.panel.runtime.snapshot().measured.motor_rpm
    assert rpm > 100
    return rpm


async def test_an_sto_opening_at_speed_holds_starts_through_the_acknowledgement(
    tmp_path: Path,
) -> None:
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        rpm = await at_speed(rig, session)
        chain_of(rig).open(rig.clock.monotonic())
        await rig.tick(0.2)
        expected = coast_hold(rpm, since_read=TICK, tau=TAU, standstill=ONE)
        left = held(rig)
        assert left == pytest.approx(expected, abs=0.25)
        assert rig.panel.runtime.snapshot().measured.motor_rpm == 0  # RFRD sees nothing
        chain_of(rig).close(rig.clock.monotonic())
        await rig.tick(5.0)
        # The sto_open latch is acknowledged: the hold is no latch, and stays.
        acknowledged = await session.post(
            "/api/safety/acknowledge", json={"operator": OPERATOR, "estop_released": True}
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert rig.panel.runtime.standing is None
        refused = await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(refused, Err)
        assert isinstance(refused.error, CoastDownHold)
        remaining = refused.error.remaining
        assert remaining == pytest.approx(expected - 5.0, abs=0.25)
        assert describe_start_refusal(refused.error) == (
            "demarrage refuse : arbre peut-etre encore en roue libre (etage de puissance "
            f"coupe, RFRD a 0) : attendre encore {math.ceil(remaining)} s"
        )
        # The ending waits for the hold: no confirmed standstill on a shaft that may coast.
        assert state_of(rig) is RuntimeState.ENDING
        assert rig.panel.runtime.snapshot().phase is Phase.COOLDOWN
        await rig.tick(remaining - 1.0)
        assert held(rig) is not None
        assert state_of(rig) is RuntimeState.ENDING
        assert rig.simulator.shaft_rpm == 0, "the hold outlasted the simulator's own coast"
        await rig.tick(1.4)
        assert held(rig) is None
        assert state_of(rig) is RuntimeState.FINISHED
        accepted = await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
        assert isinstance(accepted, Ok)
    await rig.panel.close()


async def test_a_resettable_fault_while_the_arm_coasts_is_not_reset_until_the_hold_ends(
    tmp_path: Path,
) -> None:
    """The fault-reset limit: OLF is resettable, its stage drops into a freewheel, and the
    faulted drive's RFRD reads 0. The reset waits for the hold, whatever the phase."""
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        rpm = await at_speed(rig, session)
        rig.simulator.inject_fault(DriveFault.MOTOR_OVERLOAD)
        await rig.tick(2.0)
        assert rig.panel.runtime.snapshot().drive_state is DriveState.FAULT
        assert rig.panel.runtime.snapshot().measured.motor_rpm == 0
        assert rig.simulator.shaft_rpm > 50
        refused = await rig.panel.runtime.fault_reset()
        assert isinstance(refused, Err)
        assert isinstance(refused.error, CoastDownHeld)
        expected = coast_hold(rpm, since_read=TICK, tau=TAU, standstill=ONE)
        assert refused.error.remaining == pytest.approx(expected - 1.8, abs=0.25)
        assert describe_reset_refusal(refused.error).startswith(
            "reset refuse : arbre peut-etre encore en roue libre"
        )
        left = held(rig)
        assert left is not None
        await rig.tick(left + 0.4)
        assert rig.simulator.shaft_rpm == 0
        reset = await rig.panel.runtime.fault_reset()
        assert isinstance(reset, Ok)
    await rig.panel.close()


async def test_a_drop_seen_after_a_lost_read_holds_from_the_read_that_sees_it(
    tmp_path: Path,
) -> None:
    """The last speed read before the loss, and one read period: from when the link is
    back. (Three lost exchanges in a row and the console goes silent: it reads no more,
    and the next process holds from LFRD.)"""
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        rpm = await at_speed(rig, session)
        rig.simulator.inject_comms_loss(Seconds(0.3))
        chain_of(rig).open(rig.clock.monotonic())
        await rig.tick(0.2)
        assert held(rig) is None, "nothing read: nothing known yet"
        assert not rig.panel.runtime.silent
        await rig.tick(0.2)
        expected = coast_hold(rpm, since_read=TICK, tau=TAU, standstill=ONE)
        assert held(rig) == pytest.approx(expected, abs=0.01)
    await rig.panel.close()


async def started_hold(
    tmp_path: Path, probe: WallClockProbe | None = None, env: Mapping[str, str] = BENCH_ENV
) -> tuple[Rig, Seconds]:
    """A console whose hold has just started, and that hold."""
    rig, _ = make_rig(tmp_path, wall_clock=probe, env=env)
    async with rig.http() as session:
        await at_speed(rig, session)
        chain_of(rig).open(rig.clock.monotonic())
        await rig.tick(0.2)
    left = held(rig)
    assert left is not None
    return rig, left


def restarted(
    tmp_path: Path, wall: UnixMillis, probe: WallClockProbe, env: Mapping[str, str] = BENCH_ENV
) -> Rig:
    """A new console on the same data, its wall clock at ``wall``, attested."""
    clock = ManualClock(Monotonic(10.0), wall)
    rig, _ = make_rig(tmp_path, clock=clock, wall_clock=probe, env=env)
    assert isinstance(rig.panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    return rig


@pytest.mark.parametrize(
    ("written", "read"),
    [(TRUSTED, TRUSTED), (TRUSTED, UNTRUSTED), (UNTRUSTED, TRUSTED), (UNTRUSTED, UNTRUSTED)],
    ids=["kept_then_kept", "kept_then_lost", "set_right_in_between", "never_kept"],
)
async def test_a_restart_mid_hold_keeps_it_to_its_end_only_on_a_clock_kept_at_both_ends(
    tmp_path: Path, written: Fixed, read: Fixed
) -> None:
    """A clock nobody kept at the write may have been wrong by anything: an hour behind
    here, set right by a time service before the restart. Its stored end means nothing,
    so the whole stored length runs again."""
    rig, left = await started_hold(tmp_path, written)
    kept = tmp_path / COAST_HOLD_FILE
    started_at = rig.clock.unix_millis()
    await rig.panel.close()
    assert kept.is_file()
    # 30 s pass while nothing runs, then a new process starts on the same data directory.
    behind = HOUR_MS if written is UNTRUSTED else 0
    again = restarted(tmp_path, UnixMillis(rig.clock.unix_millis() + 30_000 + behind), read)
    gone = (again.clock.unix_millis() - started_at - behind) / 1000
    assert gone >= 30.0
    await again.tick(0.2)
    kept_left = held(again)
    assert kept_left is not None
    # From the first read: to the stored end, or the whole stored length, never less.
    both = written is TRUSTED and read is TRUSTED
    assert kept_left == pytest.approx(left - gone if both else left, abs=0.3)
    refused = await again.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, CoastDownHold)
    await again.panel.close()


async def test_a_restart_on_a_lower_coast_tau_s_never_shortens_the_hold_it_finds(
    tmp_path: Path,
) -> None:
    """A hold started at 300 s is kept whole by a console restarted at 5 s, whose own
    longest hold is 36 s: the cap is the longest any COAST_TAU_S gives."""
    rig, left = await started_hold(tmp_path, env={**BENCH_ENV, "COAST_TAU_S": "300"})
    assert left > 1500.0
    await rig.panel.close()
    again = restarted(
        tmp_path,
        rig.clock.unix_millis(),
        UNTRUSTED,
        env={**BENCH_ENV, "COAST_TAU_S": "5"},
    )
    assert hold_cap(Seconds(5.0), ONE) < 40.0
    await again.tick(0.2)
    assert held(again) == pytest.approx(left, abs=0.3)
    await again.panel.close()


async def test_after_an_unclean_stop_the_first_read_with_the_stage_off_holds_the_longest_coast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process before died, or froze: its last reads, its hold, nobody kept them, and
    the exit stop it may have run zeroed LFRD. A start whose first read is its own arming
    read is refused on that read; the hold runs from it."""
    rig = console(tmp_path, deaths=1, monkeypatch=monkeypatch)
    assert isinstance(rig.panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    assert held(rig) is None, "nothing read yet: nothing started"
    refused = await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, CoastDownHold)
    assert refused.error.remaining == pytest.approx(hold_cap(TAU, ONE))
    assert rig.simulator.commanded_setpoint == 0
    await rig.tick(0.2)
    assert held(rig) == pytest.approx(hold_cap(TAU, ONE) - 0.2)
    await rig.panel.close()


async def test_after_an_unclean_stop_a_first_read_that_shows_the_stage_on_holds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The drive was left driving the shaft: RFRD reads it, and the stop the console then
    makes ends at a standstill read under torque."""
    clock = ManualClock(Monotonic(100.0), UnixMillis(START_MS))
    rig = console(
        tmp_path,
        deaths=1,
        monkeypatch=monkeypatch,
        clock=clock,
        simulator=await precommanded(clock, 300),
    )
    await rig.tick(0.2)
    assert held(rig) is None
    await rig.tick(60.0)
    assert rig.simulator.shaft_rpm == 0
    assert held(rig) is None
    await rig.panel.close()


async def test_a_clean_exit_with_the_stage_maybe_on_leaves_the_longest_coast_from_the_next_read(
    tmp_path: Path,
) -> None:
    """The exit's own stop reads RFRD only, which a stage dropped during it reads 0 too:
    the next start holds the longest coast from its first read, whatever its clock says."""
    rig, _ = make_rig(tmp_path, wall_clock=TRUSTED)
    async with rig.http() as session:
        await at_speed(rig, session)
    await rig.panel.close()
    kept = tmp_path / COAST_HOLD_FILE
    assert kept_hold(kept, now=rig.clock.unix_millis(), trusted=True, cap=longest_hold(ONE)) == (
        pytest.approx(hold_cap(TAU, ONE), abs=0.01)
    ), "the whole of it, the clock not believed"
    again = restarted(tmp_path, UnixMillis(rig.clock.unix_millis() + HOUR_MS), TRUSTED)
    await again.tick(0.2)
    assert held(again) == pytest.approx(hold_cap(TAU, ONE), abs=0.01)
    await again.panel.close()


async def test_a_restart_after_a_session_ended_at_rest_holds_nothing(tmp_path: Path) -> None:
    """The bench's ordinary restart: STOP, the descent read to a standstill with the stage
    on, the stage removed at rest, then the console stopped. Nothing is left to wait for."""
    rig, _ = make_rig(tmp_path, wall_clock=TRUSTED)
    async with rig.http() as session:
        await at_speed(rig, session)
    rig.panel.runtime.request_stop("operator: stop button")
    for _ in range(600):
        if state_of(rig) is RuntimeState.FINISHED:
            break
        await rig.tick(1.0)
    assert state_of(rig) is RuntimeState.FINISHED
    assert held(rig) is None
    assert not may_coast(rig)
    await rig.panel.close()
    assert not (tmp_path / COAST_HOLD_FILE).exists()
    again = restarted(tmp_path, UnixMillis(rig.clock.unix_millis() + 30_000), TRUSTED)
    await again.tick(0.2)
    assert held(again) is None
    accepted = await again.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(accepted, Ok)
    await again.panel.close()


class ReadsLost(Wrapped):
    """Status reads that get no answer while ``lost``; the writes still land (ANH-92, M1)."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.lost: bool = False

    @override
    async def read_status(self) -> Result[DriveStatus, DriveError]:
        if self.lost:
            return Err(CommTimeout(after=Seconds(0.1)))
        return await self.inner.read_status()


async def driven_unread(tmp_path: Path) -> tuple[Rig, ReadsLost]:
    """Armed (its read showed the stage off), then two ticks whose reads are lost while the
    setpoint's writes land: the stage driven towards 55 motor rpm, no read since."""
    rig, wrapper = make_rig(tmp_path, wrap=ReadsLost)
    assert isinstance(wrapper, ReadsLost)
    runtime = rig.panel.runtime
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    started = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(started, Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(3.0)), Ok)
    wrapper.lost = True
    await rig.tick(0.4)
    assert not runtime.silent
    assert rig.simulator.commanded_setpoint == MIN_RUN
    return rig, wrapper


async def test_a_stage_enabled_and_a_reference_written_since_the_last_read_bound_the_hold(
    tmp_path: Path,
) -> None:
    """The reads that would have shown the stage on and the shaft heading for LFRD 55 were
    lost; the read that comes back shows the stage dropped, RFRD 0. What this console did
    itself, the enable and each LFRD that landed, says where the shaft may have got to."""
    rig, wrapper = await driven_unread(tmp_path)
    chain_of(rig).open(rig.clock.monotonic())
    wrapper.lost = False
    await rig.tick(0.2)
    left = held(rig)
    assert left is not None
    from_reference = coast_hold(MIN_RUN, since_read=Seconds(0.0), tau=TAU, standstill=ONE)
    assert left == pytest.approx(from_reference, abs=0.25)
    await rig.panel.close()


class AnswersLost(ReadsLost):
    """A reference that lands with its answer lost, while ``lose_answer``: once."""

    def __init__(self, inner: SimulatedDrive) -> None:
        super().__init__(inner)
        self.lose_answer: bool = False

    @override
    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        landed = await self.inner.write_speed(rpm)
        if self.lose_answer and rpm != 0:
            self.lose_answer = False
            return Err(CommTimeout(after=Seconds(0.1)))
        return landed


async def test_a_reference_that_lands_with_its_answer_lost_bounds_the_hold(
    tmp_path: Path,
) -> None:
    """LFRD 55 lands, its answer lost, the reads lost too; the target back to 0 and its
    0 answered; the stage drops. The read that comes back shows RFRD 0 and LFRD 0: the
    55 tried in between says where the shaft may have got to (review round 2)."""
    rig, wrapper = make_rig(tmp_path, wrap=AnswersLost)
    assert isinstance(wrapper, AnswersLost)
    runtime = rig.panel.runtime
    assert isinstance(runtime.confirm_estop_wiring(OPERATOR), Ok)
    started = await runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(started, Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(3.0)), Ok)
    await rig.tick(0.2)
    wrapper.lost = True
    wrapper.lose_answer = True
    await rig.tick(0.2)
    assert not wrapper.lose_answer, "the 55 was tried"
    assert rig.simulator.commanded_setpoint == MIN_RUN, "and landed"
    assert isinstance(runtime.set_manual_target(OutputRpm(0.0)), Ok)
    await rig.tick(0.2)
    assert rig.simulator.commanded_setpoint == 0
    assert not runtime.silent
    chain_of(rig).open(rig.clock.monotonic())
    wrapper.lost = False
    await rig.tick(0.2)
    left = held(rig)
    assert left is not None
    from_reference = coast_hold(MIN_RUN, since_read=Seconds(0.0), tau=TAU, standstill=ONE)
    assert left == pytest.approx(from_reference, abs=0.25)
    await rig.panel.close()


async def test_an_exit_with_the_stage_driven_since_the_last_read_leaves_the_longest_coast(
    tmp_path: Path,
) -> None:
    rig, _ = await driven_unread(tmp_path)
    assert may_coast(rig), "enabled and driven towards 55 by this console, never read since"
    await rig.panel.close()
    kept = kept_hold(
        tmp_path / COAST_HOLD_FILE, now=rig.clock.unix_millis(), trusted=True, cap=longest_hold(ONE)
    )
    assert kept == pytest.approx(hold_cap(TAU, ONE), abs=0.01)


async def test_an_exit_with_the_stage_on_at_rest_towards_nothing_leaves_nothing(
    tmp_path: Path,
) -> None:
    """The install scenario's session: manual, target 0, the stage on, read at rest with
    LFRD 0. The drive holds the shaft at 0 Hz: the stage dropping at the exit leaves no
    coast, and the next start waits for none."""
    rig, _ = make_rig(tmp_path, wall_clock=TRUSTED)
    async with rig.http() as session:
        await start_manual(rig, session, 0.0)
    await rig.tick(1.0)
    assert state_of(rig) is RuntimeState.RUNNING
    assert rig.simulator.sim_state is SimState.OPERATION_ENABLED
    assert not may_coast(rig)
    await rig.panel.close()
    assert not (tmp_path / COAST_HOLD_FILE).exists()
    again = restarted(tmp_path, rig.clock.unix_millis(), TRUSTED)
    await again.tick(0.2)
    assert held(again) is None
    await again.panel.close()


async def test_a_hold_the_exit_cannot_keep_makes_the_stop_an_unclean_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next start would find no hold: it must count this stop as unclean, and hold the
    longest coast from its first read."""

    def refused(_path: Path, _content: bytes) -> Err[str]:
        return Err("the disk refuses")

    monkeypatch.setattr(kept_coast_hold, "replace_file", refused)
    rig, _ = await started_hold(tmp_path)
    assert not rig.panel._stalled_at_stop  # pyright: ignore[reportPrivateUsage]
    await rig.panel.close()
    assert rig.panel._stalled_at_stop  # pyright: ignore[reportPrivateUsage]


def expected_of(watch: CoastWatch) -> Seconds | None:
    """Read afresh: a checker would otherwise keep a narrowing across a call."""
    return watch.expected


def test_an_expected_hold_starts_at_the_next_read_that_shows_the_stage_off() -> None:
    watch = CoastWatch(tau=TAU, standstill=ONE)
    watch.expect(Seconds(100.0), "a drop nobody saw")
    watch.expect(Seconds(50.0), "a shorter one")
    assert expected_of(watch) == 100.0
    assert watch.note(status(DriveState.FAULT, 0), TICK) == CoastStart(
        Seconds(100.0), "a drop nobody saw"
    )
    assert expected_of(watch) is None
    # The longer of it and what the read itself starts.
    found = CoastWatch(tau=TAU, standstill=ONE)
    found.expect(Seconds(1.0), "a short one")
    started = found.note(status(DriveState.FAULT, 0, 149), TICK)
    assert started is not None
    assert started.hold == pytest.approx(40.0 * math.log(149))
    # A read that shows the stage on reads the shaft: nothing is expected any more.
    driven = CoastWatch(tau=TAU, standstill=ONE)
    driven.expect(Seconds(100.0), "a drop nobody saw")
    assert driven.note(status(DriveState.OPERATION_ENABLED, 0), TICK) is None
    assert driven.stage_on
    assert expected_of(driven) is None
    assert driven.note(status(DriveState.READY, 0), TICK) is None


def coasting(watch: CoastWatch) -> bool:
    """Read afresh, as :func:`expected_of`."""
    return watch.may_coast


def test_what_this_process_wrote_bounds_the_hold_and_the_exit() -> None:
    """The enable and each LFRD that landed count, read or not (review M1); a stage read off
    learns nothing from a reference written to it."""
    watch = CoastWatch(tau=TAU, standstill=ONE)
    watch.referenced(MotorRpm(500))
    assert not coasting(watch), "the stage is off: a reference moves nothing"
    assert watch.kept_after_exit(confirmed=True, left=None) is None
    watch.driven()
    assert watch.stage_on
    assert not coasting(watch), "on at rest, LFRD 0: held at 0 Hz"
    watch.referenced(MotorRpm(-300))
    watch.referenced(MotorRpm(100))
    assert coasting(watch)
    cap = hold_cap(TAU, ONE)
    assert watch.kept_after_exit(confirmed=True, left=Seconds(10.0)) == cap
    started = watch.note(status(DriveState.FAULT, 0, 0), TICK)
    assert started == CoastStart(
        Seconds(40.0 * math.log(300)), "the output stage dropped at 0 motor rpm, LFRD 300"
    )
    assert watch.kept_after_exit(confirmed=True, left=Seconds(10.0)) == 10.0
    assert watch.kept_after_exit(confirmed=False, left=Seconds(10.0)) == cap
    # A stage read on keeps what the read showed: an enable after it lowers nothing.
    read = CoastWatch(tau=TAU, standstill=ONE)
    assert read.note(status(DriveState.OPERATION_ENABLED, 300, 300), TICK) is None
    read.driven()
    dropped = read.note(status(DriveState.FAULT, 0, 0), TICK)
    assert dropped is not None
    assert dropped.hold == coast_hold(MotorRpm(300), since_read=TICK, tau=TAU, standstill=ONE)


def test_a_reference_written_in_one_jump_bounds_the_hold_whatever_the_last_speed_read() -> None:
    """The bench's slider writes LFRD in one jump: read at 100 rpm towards 1000, then the
    link lost, then the drop. The shaft may have gone all the way to the reference."""
    watch = CoastWatch(tau=TAU, standstill=ONE)
    assert watch.note(status(DriveState.OPERATION_ENABLED, 100, 1000), IDLE_POLL) is None
    started = watch.note(status(DriveState.FAULT, 0, 1000), IDLE_POLL)
    assert started == CoastStart(
        Seconds(40.0 * math.log(1000)), "the output stage dropped at 100 motor rpm, LFRD 1000"
    )
    # The reference read with the last read that drove the shaft counts too: LFRD zeroed
    # by the time the drop is read says nothing of where the ramp had taken the shaft.
    zeroed = CoastWatch(tau=TAU, standstill=ONE)
    assert zeroed.note(status(DriveState.OPERATION_ENABLED, 100, 1000), IDLE_POLL) is None
    started = zeroed.note(status(DriveState.FAULT, 0, 0), IDLE_POLL)
    assert started is not None
    assert started.hold == pytest.approx(40.0 * math.log(1000))
    # Read at rest under torque, a reference just written: no longer nothing.
    resting = CoastWatch(tau=TAU, standstill=ONE)
    assert resting.note(status(DriveState.OPERATION_ENABLED, 0, 0), TICK) is None
    started = resting.note(status(DriveState.SWITCH_ON_DISABLED, 0, 500), TICK)
    assert started is not None
    assert started.hold == pytest.approx(40.0 * math.log(500))


async def test_the_hold_file_goes_once_the_hold_is_over_and_a_bad_one_is_the_longest(
    tmp_path: Path,
) -> None:
    rig, left = await started_hold(tmp_path)
    kept = tmp_path / COAST_HOLD_FILE
    await rig.tick(left + 0.4)
    assert held(rig) is None
    await rig.panel.close()  # which waits for the keeper's last change
    assert not kept.exists()
    cap = hold_cap(TAU, ONE)
    now = UnixMillis(START_MS)
    assert kept_hold(kept, now=now, trusted=True, cap=cap) is None
    kept.write_text("not a hold", encoding="utf-8")
    assert kept_hold(kept, now=now, trusted=True, cap=cap) == cap
    kept.unlink()
    os.mkfifo(kept)
    assert kept_hold(kept, now=now, trusted=False, cap=cap) == cap
    kept.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"until_unix_ms": 1, "remaining_s": 1.0}', encoding="utf-8")
    kept.symlink_to(target)
    assert kept_hold(kept, now=now, trusted=False, cap=cap) == cap


async def test_a_launch_from_the_dashboard_is_refused_with_the_hold_before_it_is_confirmed(
    tmp_path: Path,
) -> None:
    """Not confirmed to the dashboard, then refused at the arming: refused before, in the
    words the arming would have used."""
    rig, _ = make_rig(tmp_path)
    rig.panel.runtime.hold_coast(rig.clock.monotonic(), Seconds(100.0), "a drop")
    launch = BenchLaunch(
        session_id="remote-bench", operator="Dr Manager", ballast=Ballast(None), notes=None
    )
    refused = rig.panel.preflight_remote_start(launch)
    assert refused == Err(describe_start_refusal(CoastDownHold(Seconds(100.0))))
    await rig.panel.close()


async def test_the_ending_is_given_the_hold_only_once_its_reference_is_zero(
    tmp_path: Path,
) -> None:
    """``session_overrun`` gives the ending the hold (COOLDOWN lasts it), but not a descent
    still above zero: that one is late on its own account, whatever may coast."""
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await at_speed(rig, session)
    runtime = rig.panel.runtime
    runtime.request_stop("operator: stop button")
    await rig.tick(0.2)
    assert runtime.applied_rpm != 0
    runtime.hold_coast(rig.clock.monotonic(), Seconds(1000.0), "a drop")
    ending = runtime._ending_in_progress()  # pyright: ignore[reportPrivateUsage]
    assert ending is not None
    assert ending.descent < 100.0
    for _ in range(600):
        if runtime.applied_rpm == 0:
            break
        await rig.tick(0.2)
    assert runtime.applied_rpm == 0
    ending = runtime._ending_in_progress()  # pyright: ignore[reportPrivateUsage]
    assert ending is not None
    assert ending.descent > 900.0
    await rig.panel.close()


async def test_a_hold_is_never_shortened_by_a_shorter_one(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    now = rig.clock.monotonic()
    runtime.hold_coast(now, Seconds(100.0), "a first drop")
    runtime.hold_coast(now, Seconds(10.0), "a shorter one")
    assert runtime.coast_hold_remaining(now) == pytest.approx(100.0)
    runtime.hold_coast(now, Seconds(10_000.0), "a longer one, capped")
    # At the longest hold of any COAST_TAU_S, not this console's: 300 ln 1380.
    assert runtime.coast_hold_remaining(now) == pytest.approx(longest_hold(ONE))
    assert round(longest_hold(ONE)) == 2169
    await rig.panel.close()


def test_the_runtime_refuses_a_time_constant_outside_the_key_s_bounds() -> None:
    for tau in (4.0, 301.0):
        with pytest.raises(ValueError, match="coast_tau must be within"):
            _ = RuntimeLimits(
                slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10), coast_tau=Seconds(tau)
            )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("4.9", "4.9 s hors de [5.0, 300.0]"),
        ("301", "301.0 s hors de [5.0, 300.0]"),
        ("vite", None),
    ],
)
def test_coast_tau_s_is_read_within_its_bounds(text: str, problem: str | None) -> None:
    read = coast_tau({"COAST_TAU_S": text})
    assert isinstance(read, Err)
    assert read.error.key == "COAST_TAU_S"
    if problem is not None:
        assert read.error.detail == problem
    assert coast_tau({"COAST_TAU_S": "12.5"}) == Ok(Seconds(12.5))
    assert coast_tau({}) == Ok(DEFAULT_COAST_TAU)


def test_a_kept_hold_is_never_longer_than_the_cap_nor_kept_once_over(tmp_path: Path) -> None:
    kept = tmp_path / COAST_HOLD_FILE
    cap = Seconds(100.0)
    now = UnixMillis(START_MS)
    kept.write_bytes(stored_hold(Seconds(500.0), now=now, trusted=True))
    assert kept_hold(kept, now=now, trusted=True, cap=cap) == cap
    assert kept_hold(kept, now=now, trusted=False, cap=cap) == cap
    # Written on a clock an hour behind that nobody kept, read once one keeps it: its
    # stored end is an hour gone, and means nothing. The whole of it runs again.
    behind = UnixMillis(START_MS - HOUR_MS)
    kept.write_bytes(stored_hold(Seconds(50.0), now=behind, trusted=False))
    assert kept_hold(kept, now=now, trusted=True, cap=cap) == 50.0
    kept.write_bytes(stored_hold(Seconds(0.0), now=UnixMillis(START_MS - 1), trusted=True))
    assert kept_hold(kept, now=now, trusted=True, cap=cap) is None
    assert kept_hold(kept, now=now, trusted=False, cap=cap) is None


async def test_the_page_is_sent_the_countdown_the_console_holds(tmp_path: Path) -> None:
    rig, left = await started_hold(tmp_path)
    await rig.tick(1.0)
    row = SnapshotRow.of(rig.panel.runtime.snapshot())
    assert row.coast_hold_s == pytest.approx(left - 1.0, abs=0.01)
    await rig.tick(left)
    assert SnapshotRow.of(rig.panel.runtime.snapshot()).coast_hold_s is None
    await rig.panel.close()


async def test_a_hold_the_disk_refuses_is_said_and_the_console_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    now = rig.clock.monotonic()
    runtime.hold_coast(now, Seconds(30.0), "a drop")
    (tmp_path / "a_directory").mkdir()
    (tmp_path / "a_file").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.kept_coast_hold"):
        nowhere = HoldKeeper(tmp_path / "a_file" / COAST_HOLD_FILE, lambda: False)
        nowhere.keep(runtime, rig.clock, now)
        landed = nowhere.close()
        assert not landed, "a hold the disk refused is said not kept"
        stuck = HoldKeeper(tmp_path / "a_directory", lambda: False)
        stuck.keep(runtime, rig.clock, Monotonic(now + 31.0))  # over: the file is removed
        removed = stuck.close()
        assert removed, "a file left behind is a hold for nothing, not one missing"
    assert "coast-down hold not kept across a restart" in caplog.text
    assert "coast-down hold file not removed" in caplog.text
    await rig.panel.close()
