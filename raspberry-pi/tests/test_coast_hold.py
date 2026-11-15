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
from pathlib import Path
from typing import Final, final, override

import httpx
import pytest

from src.clock import ManualClock
from src.kept_coast_hold import COAST_HOLD_FILE, HoldKeeper, kept_hold
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
)
from src.motor.drive import DriveFault, DriveState, DriveStatus
from src.motor.sto import SimulatedStoChain
from src.result import Err, Ok
from src.training.runtime import CoastDownHeld, CoastDownHold, RuntimeLimits, RuntimeState
from src.training.types import Occupancy, Phase
from src.units import (
    Amperes,
    Monotonic,
    MotorRpm,
    RpmPerSecond,
    Seconds,
    StatusWord,
    UnixMillis,
)
from src.wall_clock import Synchronised, Unsynchronised, WallClockProbe, WallClockState
from src.web.schemas import SnapshotRow
from tests.test_failure_rig import OPERATOR, Rig, make_rig, start_manual

TAU: Final[Seconds] = DEFAULT_COAST_TAU
ONE: Final[MotorRpm] = MotorRpm(1)
TICK: Final[Seconds] = Seconds(0.2)
IDLE_POLL: Final[Seconds] = Seconds(0.5)
START_MS: Final[int] = 1_700_000_000_000


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
        "the output stage dropped at 1000 motor rpm",
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
        assert isinstance(
            await rig.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276)), Ok
        )
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
        assert isinstance(await rig.panel.runtime.fault_reset(), Ok)
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


async def started_hold(tmp_path: Path) -> tuple[Rig, Seconds]:
    """A console whose hold has just started, and that hold."""
    rig, _ = make_rig(tmp_path)
    async with rig.http() as session:
        await at_speed(rig, session)
        chain_of(rig).open(rig.clock.monotonic())
        await rig.tick(0.2)
    left = held(rig)
    assert left is not None
    return rig, left


def restarted(tmp_path: Path, wall: UnixMillis, probe: WallClockProbe) -> Rig:
    """A new console on the same data, its wall clock at ``wall``, attested."""
    rig, _ = make_rig(tmp_path, clock=ManualClock(Monotonic(10.0), wall), wall_clock=probe)
    assert isinstance(rig.panel.runtime.confirm_estop_wiring(OPERATOR), Ok)
    return rig


@pytest.mark.parametrize("trusted", [True, False], ids=["trusted_clock", "untrusted_clock"])
async def test_a_restart_mid_hold_keeps_it_to_its_end_or_whole(
    tmp_path: Path, trusted: bool
) -> None:
    rig, left = await started_hold(tmp_path)
    kept = tmp_path / COAST_HOLD_FILE
    started_at = rig.clock.unix_millis()
    await rig.panel.close()
    assert kept.is_file()
    # 30 s pass while nothing runs, then a new process starts on the same data directory.
    later = UnixMillis(rig.clock.unix_millis() + 30_000)
    again = restarted(tmp_path, later, TRUSTED if trusted else UNTRUSTED)
    gone = (again.clock.unix_millis() - started_at) / 1000
    assert gone >= 30.0
    await again.tick(0.2)
    kept_left = held(again)
    assert kept_left is not None
    # Trusted: to the stored end. Untrusted: the whole stored length again, never less.
    assert kept_left == pytest.approx(left - gone - 0.2 if trusted else left - 0.2, abs=0.3)
    refused = await again.panel.runtime.start_manual(Occupancy.BENCH, OPERATOR, MotorRpm(276))
    assert isinstance(refused, Err)
    assert isinstance(refused.error, CoastDownHold)
    await again.panel.close()


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


async def test_a_hold_is_never_shortened_by_a_shorter_one(tmp_path: Path) -> None:
    rig, _ = make_rig(tmp_path)
    runtime = rig.panel.runtime
    now = rig.clock.monotonic()
    runtime.hold_coast(now, Seconds(100.0), "a first drop")
    runtime.hold_coast(now, Seconds(10.0), "a shorter one")
    assert runtime.coast_hold_remaining(now) == pytest.approx(100.0)
    runtime.hold_coast(now, Seconds(10_000.0), "a longer one, capped")
    assert runtime.coast_hold_remaining(now) == pytest.approx(hold_cap(TAU, ONE))
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
    kept.write_text(
        f'{{"until_unix_ms": {START_MS + 500_000}, "remaining_s": 500.0}}', encoding="utf-8"
    )
    assert kept_hold(kept, now=now, trusted=True, cap=cap) == cap
    assert kept_hold(kept, now=now, trusted=False, cap=cap) == cap
    kept.write_text(f'{{"until_unix_ms": {START_MS - 1}, "remaining_s": 0.0}}', encoding="utf-8")
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
        nowhere = HoldKeeper(tmp_path / "a_file" / COAST_HOLD_FILE)
        nowhere.keep(runtime, rig.clock, now)
        nowhere.close()
        stuck = HoldKeeper(tmp_path / "a_directory")
        stuck.keep(runtime, rig.clock, Monotonic(now + 31.0))  # over: the file is removed
        stuck.close()
    assert "coast-down hold not kept across a restart" in caplog.text
    assert "coast-down hold file not removed" in caplog.text
    await rig.panel.close()
