"""Tests for the simulated ATV320.

These are not tests of a mock. They are the evidence for the claims the safety
layer will be built on top of, none of which can be checked against hardware in
CI:

1. **A start sequence that skips a step does not start the motor.** The CiA402
   transitions are refused out of order, naming the state that was required, so
   a sequencing bug fails here instead of silently doing nothing on the bench.
2. **A stop is not instant.** Torque exists in one state only; everywhere else
   a loaded centrifuge coasts for minutes on an exponential decay. Any rule
   that assumes "commanded stopped" means "stopped" fails against this plant.
3. **Going silent stops the motor, by itself.** The drive's own ttO timeout
   latches SLF and ramps down without being asked, which is the only backstop
   this machine has - the STO input is jumpered.
4. **The silent failures are reachable on purpose.** A misaddressed write that
   is acknowledged and discarded, a reversed phase order, an emergency stop
   that returns having done nothing: each has a test, because none of them is
   visible at runtime.

Time is a :class:`~src.clock.ManualClock` throughout, so every dwell is exact
and a two-and-a-half-minute coast-down runs in milliseconds.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.clock import ManualClock
from src.motor.drive import (
    LFT_FAULT_CODES,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveFault,
    DriveFaulted,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    UnexpectedState,
    decode_status_word,
    describe_fault,
)
from src.motor.simulated import (
    ATV320_UNDEFINED_BITS,
    COMMAND_REQUIRES,
    DEFAULT_SIM_CONFIG,
    ETA_WORDS,
    FAULT_REACTIONS,
    FAULT_STATES,
    LEGAL_TRANSITIONS,
    REACTION_STATES,
    UNMAPPED_FAULT_CODE,
    FaultReaction,
    SimState,
    SimulatedDrive,
    SimulatedDriveConfig,
    lft_code_for,
)
from src.result import Err, Ok, Result
from src.units import Amperes, Monotonic, MotorRpm, OutOfRange, RawRegister, Seconds

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIM_SOURCE = PROJECT_ROOT / "src" / "motor" / "simulated.py"

#: The full start sequence from the commissioning notes.
START_SEQUENCE: tuple[ControlWord, ...] = (
    ControlWord.SHUTDOWN,
    ControlWord.SWITCH_ON,
    ControlWord.ENABLE_OPERATION,
)

#: One control-loop tick. The real loop runs at 5 Hz, and the plant is a
#: first-order explicit integration, so stepping at the loop's own rate is what
#: the numbers below are checked against.
TICK = Seconds(0.2)

#: Current thresholds a safety rule would plausibly use against a 2.15 A
#: nameplate: ~110% to warn, ~140% to trip. They are here so the model can be
#: shown to cross them at speeds and accelerations the machine really runs at.
WARN_CURRENT = Amperes(2.4)
TRIP_CURRENT = Amperes(3.0)

#: A direct system-clock call, the same pattern tests/test_clock.py greps for.
DIRECT_TIME_CALL = re.compile(r"\btime\.(monotonic|time|perf_counter|monotonic_ns|time_ns)\s*\(")


# =========================================================================
# Helpers
# =========================================================================


def _ok[T, E](result: Result[T, E]) -> T:
    """Assert success and return the value, so a test reads as its assertion."""
    assert isinstance(result, Ok), result
    return result.value


def _err[T, E](result: Result[T, E]) -> E:
    """Assert failure and return the error."""
    assert isinstance(result, Err), result
    return result.error


async def _opened(
    clock: ManualClock,
    config: SimulatedDriveConfig = DEFAULT_SIM_CONFIG,
) -> SimulatedDrive:
    """A powered, resting drive with the link open and nothing enabled."""
    sim = SimulatedDrive(clock, config)
    _ok(await sim.open())
    return sim


async def _started(
    clock: ManualClock,
    config: SimulatedDriveConfig = DEFAULT_SIM_CONFIG,
) -> SimulatedDrive:
    """A drive taken all the way to OPERATION_ENABLED, the proper way round."""
    sim = await _opened(clock, config)
    for word in START_SEQUENCE:
        _ok(await sim.write_command(word))
    assert _sim_state(sim) is SimState.OPERATION_ENABLED
    return sim


async def _run_enabled(
    sim: SimulatedDrive,
    clock: ManualClock,
    duration: Seconds,
    *,
    step: Seconds = TICK,
) -> None:
    """Advance with the enable word rewritten every tick: the real keepalive."""
    for _ in range(round(duration / step)):
        sim.advance(clock.advance(step))
        _ok(await sim.write_command(ControlWord.ENABLE_OPERATION))


async def _run_fed(
    sim: SimulatedDrive,
    clock: ManualClock,
    duration: Seconds,
    *,
    step: Seconds = TICK,
) -> None:
    """Advance while feeding the watchdog with a write that changes no state.

    Rewriting a zero speed reference keeps the drive's ttO timeout happy without
    touching the state machine, which is what a test about the *plant* wants:
    the coast being measured must not be cut short by an SLF nobody asked about.
    """
    for _ in range(round(duration / step)):
        sim.advance(clock.advance(step))
        _ok(await sim.write_speed(MotorRpm(0)))


def _run_silent(
    sim: SimulatedDrive,
    clock: ManualClock,
    duration: Seconds,
    *,
    step: Seconds = TICK,
) -> None:
    """Advance with no frames at all: the controller has gone away."""
    for _ in range(round(duration / step)):
        sim.advance(clock.advance(step))


def _sim_state(sim: SimulatedDrive) -> SimState:
    """Read the drive's own CiA402 state.

    A function rather than ``sim.sim_state`` written at the call site: mypy
    narrows a property access to the literal it was asserted against and keeps
    that narrowing across the intervening awaits, so a second assertion about
    the same drive in the same test gets reported as a non-overlapping
    comparison. A call expression is not narrowed, so each assertion here is
    checked against the declared type, which is what these tests mean.
    """
    return sim.sim_state


async def _status(sim: SimulatedDrive) -> DriveStatus:
    return _ok(await sim.read_status())


async def _spun_up(clock: ManualClock) -> SimulatedDrive:
    """A drive at its nominal speed, reached the way a session would reach it."""
    sim = await _started(clock)
    _ok(await sim.write_speed(DEFAULT_SIM_CONFIG.nominal_rpm))
    await _run_enabled(sim, clock, DEFAULT_SIM_CONFIG.acceleration_time)
    assert (await _status(sim)).output_rpm == DEFAULT_SIM_CONFIG.nominal_rpm
    return sim


# =========================================================================
# Configuration
# =========================================================================


def test_a_plant_that_cannot_be_integrated_refuses_to_exist() -> None:
    """Each of these divides by zero or freezes the shaft, and names itself.

    Raising at construction rather than returning a ``Result`` is the boundary
    ``RegisterMap`` already draws: this is built at startup with nothing
    spinning, and refusing to start is the right answer to a plant model that
    cannot be integrated. The message has to name the knob - "invalid
    configuration" is not something anyone can act on.
    """
    with pytest.raises(ValueError, match="nominal_rpm must be positive"):
        SimulatedDriveConfig(nominal_rpm=MotorRpm(0))
    with pytest.raises(ValueError, match="max_rpm must be positive"):
        SimulatedDriveConfig(max_rpm=MotorRpm(-10))
    with pytest.raises(ValueError, match="acceleration_time must be positive"):
        SimulatedDriveConfig(acceleration_time=Seconds(0.0))
    with pytest.raises(ValueError, match="tau_coast must be positive"):
        SimulatedDriveConfig(tau_coast=Seconds(-1.0))
    with pytest.raises(ValueError, match="tto must be positive"):
        SimulatedDriveConfig(tto=Seconds(0.0))


def test_the_default_ramp_rate_is_the_nameplate_over_the_ramp_time() -> None:
    """1380 rpm / 10 s = 138 rpm/s, the number every ramp assertion here uses."""
    sim = SimulatedDrive(ManualClock())
    assert sim.ramp_rate == pytest.approx(138.0)


# =========================================================================
# The state machine: the ETA words
# =========================================================================


def test_every_state_reports_a_status_word() -> None:
    """Total over SimState, or the simulator could not answer a read at all."""
    assert set(ETA_WORDS) == set(SimState)


def test_every_simulated_word_carries_the_bits_the_profile_does_not_define() -> None:
    """The ATV320 sets bits 9 and 10. A consumer that forgets to mask must fail.

    Emitting tidy single-byte words instead would make a masking bug invisible
    in CI and obvious only once a real drive is on the bench.
    """
    for state, word in ETA_WORDS.items():
        assert word & ATV320_UNDEFINED_BITS == ATV320_UNDEFINED_BITS, state


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (SimState.NOT_READY_TO_SWITCH_ON, DriveState.NOT_READY),
        (SimState.SWITCH_ON_DISABLED, DriveState.SWITCH_ON_DISABLED),
        (SimState.READY_TO_SWITCH_ON, DriveState.READY),
        (SimState.SWITCHED_ON, DriveState.SWITCHED_ON),
        (SimState.OPERATION_ENABLED, DriveState.OPERATION_ENABLED),
        # The dangerous one: a machine decelerating from full speed decodes to
        # the same reassuring SWITCHED_ON as a machine sitting still.
        (SimState.DISABLING_ON_RAMP, DriveState.SWITCHED_ON),
        (SimState.FAULT_REACTION_RAMP_STOP, DriveState.FAULT),
        (SimState.FAULT_REACTION_FREEWHEEL, DriveState.FAULT),
        (SimState.FAULT, DriveState.FAULT),
    ],
)
def test_each_state_decodes_to_the_right_drive_state(state: SimState, expected: DriveState) -> None:
    """The words are decoded by the real decoder, not by a second copy of it."""
    assert decode_status_word(ETA_WORDS[state]) is expected


def test_the_two_fault_reactions_are_indistinguishable_from_outside() -> None:
    """One is ramping the motor down; the other has left it coasting.

    They share a status word because the profile gives them one, and that is a
    fact with consequences: the safety layer cannot learn from ETA whether the
    centrifuge is being stopped or is merely free. It has to read output_rpm.
    """
    ramping = ETA_WORDS[SimState.FAULT_REACTION_RAMP_STOP]
    coasting = ETA_WORDS[SimState.FAULT_REACTION_FREEWHEEL]
    assert ramping == coasting
    assert decode_status_word(ramping) is DriveState.FAULT


def test_the_reaction_states_are_the_two_that_share_a_word() -> None:
    """A pin on the module's own sets, which several of its rules key off."""
    assert REACTION_STATES < FAULT_STATES
    settled = FAULT_STATES - REACTION_STATES
    assert settled == frozenset({SimState.FAULT})


# =========================================================================
# The state machine: sequencing
# =========================================================================


async def test_the_documented_start_sequence_reaches_operation_enabled() -> None:
    """6 -> 7 -> 15, with the drive's own state checked at every step."""
    clock = ManualClock()
    sim = await _opened(clock)
    assert (await _status(sim)).state is DriveState.SWITCH_ON_DISABLED

    _ok(await sim.write_command(ControlWord.SHUTDOWN))
    assert (await _status(sim)).state is DriveState.READY

    _ok(await sim.write_command(ControlWord.SWITCH_ON))
    assert (await _status(sim)).state is DriveState.SWITCHED_ON

    _ok(await sim.write_command(ControlWord.ENABLE_OPERATION))
    assert (await _status(sim)).state is DriveState.OPERATION_ENABLED


async def test_skipping_the_switch_on_step_is_refused() -> None:
    """The whole point of the module: 6 -> 15 must not start the motor.

    A drive that quietly accepted this would make the bug appear only on the
    bench, as a machine that does not turn and a log that says everything
    worked.
    """
    clock = ManualClock()
    sim = await _opened(clock)
    _ok(await sim.write_command(ControlWord.SHUTDOWN))

    error = _err(await sim.write_command(ControlWord.ENABLE_OPERATION))
    assert error == UnexpectedState(expected=DriveState.SWITCHED_ON, actual=DriveState.READY)
    assert _sim_state(sim) is SimState.READY_TO_SWITCH_ON

    # And the refusal is real: a setpoint written to a drive that was never
    # enabled turns nothing.
    _ok(await sim.write_speed(MotorRpm(900)))
    await _run_fed(sim, clock, Seconds(5.0))
    assert (await _status(sim)).output_rpm == MotorRpm(0)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        (ControlWord.SWITCH_ON, DriveState.READY),
        (ControlWord.ENABLE_OPERATION, DriveState.SWITCHED_ON),
        (ControlWord.FAULT_RESET, DriveState.FAULT),
    ],
)
async def test_commands_out_of_order_name_the_state_they_needed(
    word: ControlWord, expected: DriveState
) -> None:
    """ "expected SWITCHED_ON, actual READY" is a diagnosis; "rejected" is not.

    A fault reset with nothing to reset is in here too. A real drive ignores
    one, but a caller that sends it has misunderstood the machine's state, and
    there is no automatic fault reset anywhere in this system for it to be part
    of.
    """
    clock = ManualClock()
    sim = await _opened(clock)
    error = _err(await sim.write_command(word))
    assert error == UnexpectedState(expected=expected, actual=DriveState.SWITCH_ON_DISABLED)


async def test_a_drive_still_running_its_self_test_accepts_nothing() -> None:
    """Including SHUTDOWN: it has not finished agreeing to exist yet."""
    clock = ManualClock()
    sim = SimulatedDrive(clock, initial_state=SimState.NOT_READY_TO_SWITCH_ON)
    _ok(await sim.open())
    assert (await _status(sim)).state is DriveState.NOT_READY

    for word in ControlWord:
        error = _err(await sim.write_command(word))
        assert error == UnexpectedState(
            expected=COMMAND_REQUIRES[word],
            actual=DriveState.NOT_READY,
        )


async def test_the_self_test_finishes_on_its_own_and_then_the_drive_starts() -> None:
    """CiA402 transition 1 is automatic; everything after it is commanded.

    The wait is modelled only so the state can be observed changing. Nothing
    safety-relevant may depend on its length: the rule is to read the drive's
    state, never to wait a fixed time and assume.
    """
    clock = ManualClock()
    sim = SimulatedDrive(clock, initial_state=SimState.NOT_READY_TO_SWITCH_ON)
    _ok(await sim.open())

    _run_silent(sim, clock, Seconds(0.4))
    assert _sim_state(sim) is SimState.NOT_READY_TO_SWITCH_ON

    _run_silent(sim, clock, Seconds(1.0))
    assert _sim_state(sim) is SimState.SWITCH_ON_DISABLED
    for word in START_SEQUENCE:
        _ok(await sim.write_command(word))


async def test_the_keepalive_may_be_rewritten_for_ever() -> None:
    """Re-issuing 15 while enabled is how the ttO watchdog is fed.

    If this were refused as "already enabled", the only way to keep the drive's
    timeout happy would be some other write, and the control loop's whole
    arrangement - keepalive first, every tick - would be wrong.
    """
    clock = ManualClock()
    sim = await _started(clock)
    await _run_enabled(sim, clock, Seconds(20.0))
    assert _sim_state(sim) is SimState.OPERATION_ENABLED
    assert (await _status(sim)).fault is None


async def test_stopping_is_the_documented_pair_of_words() -> None:
    """7 then 6, from the commissioning notes, and both are accepted."""
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_command(ControlWord.SWITCH_ON))
    assert _sim_state(sim) is SimState.SWITCHED_ON
    _ok(await sim.write_command(ControlWord.SHUTDOWN))
    assert _sim_state(sim) is SimState.READY_TO_SWITCH_ON


async def test_a_drive_found_already_enabled_reports_itself_that_way() -> None:
    """The startup hazard the contract names: a previous process died running.

    Opening the link must not change anything, so the software sees
    OPERATION_ENABLED at 900 rpm and can do what the rule requires - command
    zero, disable, latch, and wait for a human.
    """
    clock = ManualClock()
    sim = SimulatedDrive(
        clock,
        initial_state=SimState.OPERATION_ENABLED,
        initial_rpm=MotorRpm(900),
    )
    _ok(await sim.open())
    status = await _status(sim)
    assert status.state is DriveState.OPERATION_ENABLED
    assert status.output_rpm == MotorRpm(900)


def test_a_fault_cannot_be_a_starting_state() -> None:
    """A latched fault has to carry an LFT code, so it is injected, not declared."""
    for state in sorted(FAULT_STATES, key=lambda member: member.name):
        with pytest.raises(ValueError, match="not a valid starting state"):
            SimulatedDrive(ManualClock(), initial_state=state)


def test_the_transition_table_has_no_entry_for_a_fault_state_except_the_reset() -> None:
    """Nothing is commanded while a fault is latched, and the table says so too."""
    faulted = {(state, word) for (state, word) in LEGAL_TRANSITIONS if state in FAULT_STATES}
    assert faulted == {(SimState.FAULT, ControlWord.FAULT_RESET)}


# =========================================================================
# The ramp
# =========================================================================


async def test_the_ramp_reaches_the_setpoint_in_the_configured_time() -> None:
    """1380 rpm in 10 s, and not before: the drive's ACC ramp, not a step."""
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(1380)))

    await _run_enabled(sim, clock, Seconds(0.2))
    assert (await _status(sim)).output_rpm == MotorRpm(28)  # 138 rpm/s * 0.2 s

    await _run_enabled(sim, clock, Seconds(4.8))
    assert (await _status(sim)).output_rpm == MotorRpm(690)  # half way at half time

    await _run_enabled(sim, clock, Seconds(5.0))
    assert (await _status(sim)).output_rpm == MotorRpm(1380)


async def test_the_setpoint_is_echoed_even_when_the_ceiling_refuses_it() -> None:
    """HSP clamps the shaft; it does not tidy away what was asked for.

    The echo is the only evidence a write landed, so it has to report the
    written value. A controller demanding 9000 rpm is then visible as a
    controller demanding 9000 rpm, rather than as a machine sitting at its
    ceiling for no stated reason.
    """
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(9000)))
    await _run_enabled(sim, clock, Seconds(20.0))

    status = await _status(sim)
    assert status.setpoint_echo_rpm == MotorRpm(9000)
    assert status.output_rpm == DEFAULT_SIM_CONFIG.max_rpm


async def test_a_setpoint_outside_a_signed_register_is_refused_before_anything_is_sent() -> None:
    """Parsing at the boundary: 40000 rpm would wrap into a reverse speed.

    Checked through the same ``src.units`` validator the hardware path uses, so
    the simulator cannot accept a value the real driver would reject.
    """
    clock = ManualClock()
    sim = await _started(clock)
    error = _err(await sim.write_speed(MotorRpm(40000)))
    assert isinstance(error, OutOfRange)
    assert error.quantity == "signed16"
    assert (await _status(sim)).setpoint_echo_rpm == MotorRpm(0)


# =========================================================================
# The coast: what a stop command really does
# =========================================================================


async def test_dropping_the_output_stage_leaves_the_centrifuge_turning() -> None:
    """The hazard, made measurable. tau_coast = 20 s from 1380 rpm.

    Command word 6 out of OPERATION_ENABLED is CiA402 transition 8: the output
    stage goes away and the load coasts. A simulator that snapped to zero here
    would let every "stop the machine" rule pass while being wrong by minutes.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)

    _ok(await sim.write_command(ControlWord.SHUTDOWN))
    assert _sim_state(sim) is SimState.READY_TO_SWITCH_ON

    await _run_fed(sim, clock, Seconds(0.2))
    assert (await _status(sim)).output_rpm > MotorRpm(1300)  # not a step change

    await _run_fed(sim, clock, Seconds(19.8))
    assert (await _status(sim)).output_rpm == pytest.approx(1380.0 * math.exp(-1.0), abs=2.0)

    await _run_fed(sim, clock, Seconds(40.0))
    assert (await _status(sim)).output_rpm > MotorRpm(0)  # still turning after a minute


async def test_the_coast_does_eventually_reach_a_standstill() -> None:
    """An exponential never reaches zero, and "1 rpm for ever" is not truer.

    Below the finest speed the drive can report the shaft is called stopped -
    which is also what lets a fault reaction know it is over.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.write_command(ControlWord.SHUTDOWN))

    # 20 * ln(1380) ~ 145 s to fall below 1 rpm.
    await _run_fed(sim, clock, Seconds(150.0))
    assert (await _status(sim)).output_rpm == MotorRpm(0)


async def test_word_seven_ramps_the_machine_down_and_word_six_abandons_it() -> None:
    """The single most important difference in this module, measured.

    Both words look like "stop" and both take the drive out of OPERATION
    ENABLED. Only word 7 (CiA402 transition 5, which this drive is commissioned
    to ramp) keeps the output stage driving the shaft down; word 6 (transition
    8) drops it and leaves a loaded centrifuge coasting.

    Two orders of magnitude, and it is measured here rather than asserted about
    a state name, because the drive reports SWITCHED_ON while ramping and READY
    while coasting - the coasting one being the more reassuring of the two.

    This is the test that fails if the simulator ever goes back to modelling
    torque as existing in OPERATION_ENABLED alone: with that model both words
    coast, the real driver's stop sequence looks correct in CI, and the machine
    freewheels on the bench.
    """
    # A clock each: the two machines are being compared over the same elapsed
    # time, and one shared ManualClock would hand the second one the first
    # one's advances as silence and trip its ttO watchdog.
    ramp_clock = ManualClock()
    ramped = await _spun_up(ramp_clock)
    _ok(await ramped.write_command(ControlWord.SWITCH_ON))
    assert _sim_state(ramped) is SimState.DISABLING_ON_RAMP

    coast_clock = ManualClock()
    coasted = await _spun_up(coast_clock)
    _ok(await coasted.write_command(ControlWord.SHUTDOWN))
    assert _sim_state(coasted) is SimState.READY_TO_SWITCH_ON

    # 1380 rpm at 138 rpm/s is 10 s of ramp. One second in, the ramped machine
    # has already shed ten times what the coasting one has.
    await _run_fed(ramped, ramp_clock, Seconds(1.0))
    await _run_fed(coasted, coast_clock, Seconds(1.0))
    assert (await _status(ramped)).output_rpm == MotorRpm(1242)
    assert (await _status(coasted)).output_rpm > MotorRpm(1300)

    # Ten seconds: the ramp is finished and the output stage is now genuinely
    # off. The freewheel is at 1380 * exp(-0.5) ~ 837 rpm and has 135 s to go.
    await _run_fed(ramped, ramp_clock, Seconds(9.0))
    await _run_fed(coasted, coast_clock, Seconds(9.0))
    assert (await _status(ramped)).output_rpm == MotorRpm(0)
    assert _sim_state(ramped) is SimState.SWITCHED_ON
    assert (await _status(coasted)).output_rpm > MotorRpm(800)

    await _run_fed(coasted, coast_clock, Seconds(120.0))
    assert (await _status(coasted)).output_rpm > MotorRpm(0), (
        "the freewheel is still turning two minutes after the 'stop'"
    )


async def test_the_ramp_stop_keeps_driving_and_says_nothing_about_it() -> None:
    """Torque and current during transition 5, and an ETA word that hides both.

    The status word is the SWITCHED_ON one - the word a drive with its output
    stage off also emits - while the drive is drawing current to brake 420 J of
    rotating mass. Nothing may read "SWITCHED_ON" as "stopped"; only
    ``output_rpm`` speaks about motion.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.write_command(ControlWord.SWITCH_ON))
    await _run_fed(sim, clock, Seconds(1.0))

    status = await _status(sim)
    assert status.state is DriveState.SWITCHED_ON
    assert status.status_word == ETA_WORDS[SimState.SWITCHED_ON]
    assert status.output_rpm == MotorRpm(1242)
    assert status.current > Amperes(0.0), "the output stage is braking, so it draws current"


async def test_a_ramp_stop_can_be_taken_back_and_abandoned_mid_way() -> None:
    """Re-enabling mid-ramp resumes the reference; word 6 mid-ramp coasts.

    Both matter for a real close sequence: a caller that changes its mind must
    not be told it has a sequencing bug, and a caller that gives up half way
    down the ramp must not be told the machine stopped.
    """
    resume_clock = ManualClock()
    resumed = await _spun_up(resume_clock)
    _ok(await resumed.write_command(ControlWord.SWITCH_ON))
    # Silent rather than fed, and under the 3 s ttO: feeding the watchdog with
    # a zero speed write would zero the reference this test then resumes onto.
    _run_silent(resumed, resume_clock, Seconds(2.0))
    _ok(await resumed.write_command(ControlWord.ENABLE_OPERATION))
    assert _sim_state(resumed) is SimState.OPERATION_ENABLED
    assert (await _status(resumed)).output_rpm == MotorRpm(1104), "2 s of ramp was real"
    await _run_enabled(resumed, resume_clock, Seconds(3.0))
    assert (await _status(resumed)).output_rpm == MotorRpm(1380), "back on the reference"

    abandon_clock = ManualClock()
    abandoned = await _spun_up(abandon_clock)
    _ok(await abandoned.write_command(ControlWord.SWITCH_ON))
    _run_silent(abandoned, abandon_clock, Seconds(2.0))
    _ok(await abandoned.write_command(ControlWord.SHUTDOWN))
    assert _sim_state(abandoned) is SimState.READY_TO_SWITCH_ON
    await _run_fed(abandoned, abandon_clock, Seconds(8.0))
    # 1104 * exp(-0.4) ~ 740: the ramp stopped ramping the moment the output
    # stage went away, and what was left of the speed is now coasting.
    assert (await _status(abandoned)).output_rpm > MotorRpm(700), "coasting, not ramping"


async def test_a_ramp_stop_of_a_machine_at_rest_is_over_before_it_starts() -> None:
    """Word 7 at standstill lands in SWITCHED_ON, not in a transient.

    Otherwise the drive would park in a state whose only exit is a shaft
    slowing down, on a shaft that is already still.
    """
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_command(ControlWord.SWITCH_ON))
    assert _sim_state(sim) is SimState.SWITCHED_ON


async def test_nothing_turns_while_the_output_stage_is_off() -> None:
    """A reference written to a switched-on-but-not-enabled drive does nothing."""
    clock = ManualClock()
    sim = await _opened(clock)
    _ok(await sim.write_command(ControlWord.SHUTDOWN))
    _ok(await sim.write_command(ControlWord.SWITCH_ON))
    _ok(await sim.write_speed(MotorRpm(1380)))
    await _run_fed(sim, clock, Seconds(10.0))
    assert (await _status(sim)).output_rpm == MotorRpm(0)


# =========================================================================
# The ttO watchdog: the only backstop this machine has
# =========================================================================


async def test_going_silent_latches_slf_and_ramps_the_motor_down() -> None:
    """The path that matters most: the software stops writing, the drive stops.

    With STO jumpered there is no independent way to remove torque, so this
    watchdog is the last line. Its reaction is a controlled ramp rather than a
    freewheel, which is the only reason a silent controller is survivable.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)

    _run_silent(sim, clock, Seconds(3.2))  # just past ttO
    status = await _status(sim)
    assert status.state is DriveState.FAULT
    assert status.fault_present
    assert status.fault is DriveFault.MODBUS_COMM_LOSS
    assert _sim_state(sim) is SimState.FAULT_REACTION_RAMP_STOP
    assert status.output_rpm > MotorRpm(1300)  # the fault has only just latched

    # Driven down its own ramp rather than left to coast, which is the whole
    # difference: 138 rpm/s over 5 s is 690 rpm gone, where an exponential
    # decay from this speed would have lost about 300.
    _run_silent(sim, clock, Seconds(5.0))
    lost = status.output_rpm - (await _status(sim)).output_rpm
    assert lost == pytest.approx(138.0 * 5.0, abs=1.0)

    _run_silent(sim, clock, Seconds(5.2))
    assert (await _status(sim)).output_rpm == MotorRpm(0)
    assert _sim_state(sim) is SimState.FAULT


async def test_the_watchdog_trips_from_standstill_without_a_reaction_state() -> None:
    """SLF is raised whether or not the motor is turning, as the keypad shows.

    Deliberate: the software does not get to drop the keepalive whenever it
    believes the machine is idle, because "idle" is its own belief and this
    watchdog is the thing that catches the belief being wrong.
    """
    clock = ManualClock()
    sim = await _started(clock)
    _run_silent(sim, clock, Seconds(3.2))
    assert _sim_state(sim) is SimState.FAULT
    assert (await _status(sim)).fault is DriveFault.MODBUS_COMM_LOSS


async def test_a_keepalive_inside_the_timeout_keeps_the_drive_happy() -> None:
    """Otherwise the test above would only be proving that the clock works."""
    clock = ManualClock()
    sim = await _started(clock)
    for _ in range(10):
        _run_silent(sim, clock, Seconds(2.4))  # silent, but inside ttO
        _ok(await sim.write_command(ControlWord.ENABLE_OPERATION))
    assert _sim_state(sim) is SimState.OPERATION_ENABLED


async def test_a_drive_that_has_never_heard_from_us_does_not_raise_slf() -> None:
    """Otherwise every ATV320 sitting powered on a shelf would show a fault."""
    clock = ManualClock()
    sim = SimulatedDrive(clock)
    _run_silent(sim, clock, Seconds(100.0))
    assert _sim_state(sim) is SimState.SWITCH_ON_DISABLED
    _ok(await sim.open())
    assert (await _status(sim)).fault is None


async def test_closing_the_link_does_not_stop_the_motor_but_the_watchdog_does() -> None:
    """close() drops the port; the drive keeps doing exactly what it was doing.

    Modelled as the bare minimum a close can be, because a safety layer that
    only works when close() also stops the machine is a safety layer that
    breaks the day somebody pulls the cable instead.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.close())

    _run_silent(sim, clock, Seconds(1.0))
    assert _sim_state(sim) is SimState.OPERATION_ENABLED  # still commanded, still turning

    _run_silent(sim, clock, Seconds(2.4))
    _ok(await sim.open())
    status = await _status(sim)
    assert status.fault is DriveFault.MODBUS_COMM_LOSS
    assert status.output_rpm < MotorRpm(1380)


async def test_a_read_alone_does_not_feed_the_watchdog() -> None:
    """Stricter than the hardware, on purpose.

    A real ATV320 resets its timeout on any frame it receives, reads included.
    Modelling writes only makes this simulator trip sooner than the drive
    would, so software built against it keeps a *write* keepalive alive - which
    is correct under either reading. The opposite assumption would let a loop
    that only polls look healthy in CI and lose the motor on the bench.
    """
    clock = ManualClock()
    sim = await _started(clock)
    for _ in range(20):
        sim.advance(clock.advance(TICK))
        await sim.read_status()
    assert _sim_state(sim) is SimState.FAULT


async def test_a_failed_open_still_leaves_the_watchdog_armable() -> None:
    """A drive that would not answer must not end up permanently unwatched.

    The first frame of any kind - here a read, once the link heals -
    establishes communication and starts the timeout counting from then.
    """
    clock = ManualClock()
    sim = SimulatedDrive(clock)
    sim.inject_comms_loss(Seconds(1.0))
    assert isinstance(_err(await sim.open()), CommTimeout)

    _run_silent(sim, clock, Seconds(1.0))
    await _status(sim)  # a read, not a write: establishes comms, feeds nothing
    _run_silent(sim, clock, Seconds(3.2))
    assert _sim_state(sim) is SimState.FAULT


# =========================================================================
# Faults
# =========================================================================


def test_every_fault_has_a_reaction() -> None:
    """Total over DriveFault, or a new fault would have no modelled behaviour."""
    assert set(FAULT_REACTIONS) == set(DriveFault)


def test_only_the_comms_timeout_is_commissioned_to_ramp() -> None:
    """Every other fault has already removed the drive's ability to control.

    Pinned so that widening the ramping set is a deliberate edit: a fault that
    freewheels when the drive could have ramped is minutes of uncontrolled
    coast-down, and one that claims to ramp when it cannot is worse.
    """
    ramping = {
        fault
        for fault, reaction in FAULT_REACTIONS.items()
        if reaction is FaultReaction.RAMP_TO_STOP
    }
    assert ramping == {DriveFault.MODBUS_COMM_LOSS}


def test_fault_codes_come_from_the_shared_table() -> None:
    """Not from a second copy of a table already marked PROVISIONAL.

    A correction made in drive.py has to reach the simulator, or CI starts
    testing fault numbers the drive does not use.
    """
    for code, fault in LFT_FAULT_CODES.items():
        assert lft_code_for(fault) == code
        assert describe_fault(lft_code_for(fault)).fault is fault


def test_a_fault_the_table_cannot_name_degrades_to_unknown() -> None:
    """The table is incomplete by admission, so this path is live in production."""
    assert UNMAPPED_FAULT_CODE not in LFT_FAULT_CODES
    assert lft_code_for(DriveFault.NO_MOTOR) == UNMAPPED_FAULT_CODE
    assert describe_fault(UNMAPPED_FAULT_CODE).fault is DriveFault.UNKNOWN


def test_an_alternative_code_table_is_actually_used() -> None:
    """Proves the codes are looked up, not hardcoded next to the enum."""
    corrected: dict[RawRegister, DriveFault] = {RawRegister(77): DriveFault.OVERCURRENT}
    assert lft_code_for(DriveFault.OVERCURRENT, corrected) == RawRegister(77)


async def test_an_injected_fault_freewheels_and_says_so_in_the_register() -> None:
    """ObF drops the drive to freewheel - the commissioning notes say so.

    So the centrifuge is coasting, uncontrolled, and the only field that admits
    it is output_rpm: the status word is the same one a controlled ramp-down
    reports.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    sim.inject_fault(DriveFault.DC_BUS_OVERVOLTAGE)

    status = await _status(sim)
    assert status.state is DriveState.FAULT
    assert status.fault is DriveFault.DC_BUS_OVERVOLTAGE
    assert _sim_state(sim) is SimState.FAULT_REACTION_FREEWHEEL

    await _run_fed(sim, clock, Seconds(60.0))
    assert (await _status(sim)).output_rpm == pytest.approx(1380.0 * math.exp(-3.0), abs=2.0)
    assert _sim_state(sim) is SimState.FAULT_REACTION_FREEWHEEL


async def test_an_injected_fault_at_standstill_settles_immediately() -> None:
    """There is no reaction to run when there is no energy to get rid of."""
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_fault(DriveFault.UNDERVOLTAGE)
    assert _sim_state(sim) is SimState.FAULT
    assert (await _status(sim)).fault is DriveFault.UNDERVOLTAGE


async def test_a_fault_with_no_code_reports_what_the_hardware_path_would() -> None:
    """NO_MOTOR has no number in the table, so a real stack would say UNKNOWN.

    The simulator says UNKNOWN too, rather than reporting a name only it knows:
    a simulator better informed than the code under test is a simulator that
    hides a gap.
    """
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_fault(DriveFault.NO_MOTOR)
    assert (await _status(sim)).fault is DriveFault.UNKNOWN


async def test_no_command_is_accepted_while_a_fault_is_latched() -> None:
    """Including a restart attempt. There is no automatic resumption anywhere."""
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_fault(DriveFault.MOTOR_OVERLOAD)

    for word in (ControlWord.SHUTDOWN, ControlWord.SWITCH_ON, ControlWord.ENABLE_OPERATION):
        error = _err(await sim.write_command(word))
        assert error == DriveFaulted(
            fault=DriveFault.MOTOR_OVERLOAD,
            raw_code=lft_code_for(DriveFault.MOTOR_OVERLOAD),
        )
    assert _sim_state(sim) is SimState.FAULT


async def test_a_fault_cannot_be_reset_while_the_machine_is_still_coasting() -> None:
    """You do not reset your way out of a spinning centrifuge.

    CiA402 forbids it and so does the physics: the reaction has not finished,
    and clearing the fault would make the drive startable with hundreds of
    joules still in the rig.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    sim.inject_fault(DriveFault.OVERCURRENT)
    assert _sim_state(sim) is SimState.FAULT_REACTION_FREEWHEEL

    error = _err(await sim.write_command(ControlWord.FAULT_RESET))
    assert error == DriveFaulted(
        fault=DriveFault.OVERCURRENT,
        raw_code=lft_code_for(DriveFault.OVERCURRENT),
    )


async def test_a_settled_fault_resets_and_the_drive_can_be_started_again() -> None:
    """And the reset is an explicit action, never something this code decides."""
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_fault(DriveFault.UNDERVOLTAGE)

    _ok(await sim.write_command(ControlWord.FAULT_RESET))
    assert _sim_state(sim) is SimState.SWITCH_ON_DISABLED
    status = await _status(sim)
    assert status.fault is None
    assert not status.fault_present

    for word in START_SEQUENCE:
        _ok(await sim.write_command(word))
    assert _sim_state(sim) is SimState.OPERATION_ENABLED


async def test_a_reference_may_be_written_to_a_faulted_drive() -> None:
    """Harmless, and it is what the register does. It just is not followed."""
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_fault(DriveFault.INTERNAL)
    _ok(await sim.write_speed(MotorRpm(600)))

    # Silent, not fed: a latched fault makes the ttO watchdog irrelevant, and
    # the keepalive write would overwrite the reference being checked.
    _run_silent(sim, clock, Seconds(10.0))
    status = await _status(sim)
    assert status.setpoint_echo_rpm == MotorRpm(600)
    assert status.output_rpm == MotorRpm(0)


# =========================================================================
# The current model
# =========================================================================


async def test_a_stopped_and_disabled_drive_draws_nothing() -> None:
    """No output stage, no current - not even the magnetising component."""
    clock = ManualClock()
    sim = await _opened(clock)
    assert (await _status(sim)).current == Amperes(0.0)


async def test_a_coasting_centrifuge_draws_nothing_while_still_turning() -> None:
    """Which is what makes "enabled, commanded, drawing nothing" diagnostic.

    Current is evidence about the drive's output stage, not about the shaft. A
    no-load rule that read zero current as "stopped" would be reading it as the
    exact opposite of what it means here.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.write_command(ControlWord.SHUTDOWN))
    await _run_fed(sim, clock, Seconds(1.0))

    status = await _status(sim)
    assert status.output_rpm > MotorRpm(1000)
    assert status.current == Amperes(0.0)


async def test_steady_running_at_nominal_sits_just_under_the_nameplate() -> None:
    """0.9 A magnetising + 1.0 A load = 1.9 A against a 2.15 A nameplate."""
    clock = ManualClock()
    sim = await _spun_up(clock)
    await _run_enabled(sim, clock, Seconds(2.0))  # steady, so no acceleration term
    current = (await _status(sim)).current
    assert current == pytest.approx(1.9, abs=0.01)
    assert current < WARN_CURRENT


async def test_accelerating_costs_current_and_crosses_the_warning_threshold() -> None:
    """Torque is current: 138 rpm/s adds 0.55 A on top of the running load."""
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(1380)))
    await _run_enabled(sim, clock, Seconds(9.8))  # still ramping, nearly at speed

    current = (await _status(sim)).current
    assert current == pytest.approx(0.9 + 1352.4 / 1380.0 + 0.004 * 138.0, abs=0.01)
    assert WARN_CURRENT <= current < TRIP_CURRENT


async def test_a_ramp_short_enough_to_trip_the_overcurrent_rule_does_so() -> None:
    """A 4 s ramp to nominal is 345 rpm/s, and that crosses the trip level.

    Which is the point of having the term at all: a safety rule needs something
    to fire on, and "the machine was accelerated too hard" is a real way to get
    there.
    """
    clock = ManualClock()
    config = replace(DEFAULT_SIM_CONFIG, acceleration_time=Seconds(4.0))
    sim = await _started(clock, config)
    _ok(await sim.write_speed(config.nominal_rpm))
    await _run_enabled(sim, clock, Seconds(3.8))

    assert (await _status(sim)).current > TRIP_CURRENT


async def test_the_current_is_clipped_where_the_drive_would_clip_it() -> None:
    """The drive has its own current limit, and the model may not exceed it."""
    clock = ManualClock()
    config = replace(
        DEFAULT_SIM_CONFIG,
        acceleration_time=Seconds(0.5),
        current_limit=Amperes(2.5),
    )
    sim = await _started(clock, config)
    _ok(await sim.write_speed(config.nominal_rpm))
    await _run_enabled(sim, clock, Seconds(0.4))

    assert (await _status(sim)).current == Amperes(2.5)


# =========================================================================
# Injected conditions
# =========================================================================


async def test_comms_loss_fails_every_operation_and_then_heals() -> None:
    """The caller sees timeouts; the drive sees silence. Both halves matter.

    This is the "software went away" scenario end to end: the calls fail, the
    centrifuge keeps spinning, the drive's own watchdog starts stopping it, and
    when the link comes back the machine explains itself with SLF.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    sim.inject_comms_loss(Seconds(5.0))

    assert isinstance(_err(await sim.read_status()), CommTimeout)
    assert isinstance(_err(await sim.write_speed(MotorRpm(0))), CommTimeout)
    assert isinstance(_err(await sim.write_command(ControlWord.SHUTDOWN)), CommTimeout)

    _run_silent(sim, clock, Seconds(5.2))  # past the window, not exactly on it
    status = await _status(sim)
    assert status.fault is DriveFault.MODBUS_COMM_LOSS
    assert status.output_rpm < MotorRpm(1380)
    assert status.output_rpm > MotorRpm(0)  # a ramp-down takes 10 s, not 5


async def test_latency_inside_the_budget_is_invisible() -> None:
    """A few milliseconds on a Modbus exchange is not a safety event."""
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_latency(Seconds(0.05))
    _ok(await sim.read_status())
    _ok(await sim.write_speed(MotorRpm(100)))


async def test_latency_over_the_budget_is_a_timeout_that_starves_the_watchdog() -> None:
    """A frame that timed out never arrived, so it cannot have fed the drive.

    The compound failure is the interesting one: the controller believes it is
    writing a keepalive, every write times out, and the drive stops the motor
    because as far as it is concerned nobody is talking to it at all.
    """
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_latency(Seconds(2.0))

    error = _err(await sim.write_command(ControlWord.ENABLE_OPERATION))
    assert error == CommTimeout(after=Seconds(2.0))

    for _ in range(20):
        sim.advance(clock.advance(TICK))
        assert isinstance(_err(await sim.write_command(ControlWord.ENABLE_OPERATION)), CommTimeout)
    assert _sim_state(sim) is SimState.FAULT


async def test_a_misaddressed_write_is_acknowledged_and_discarded() -> None:
    """The silent one. Ok returned, watchdog fed, reference never moved.

    A wrong ``RegisterMap`` offset writes the speed setpoint into whatever
    parameter is next door. Nothing fails, nothing logs, and the only evidence
    is the echo not following the write - which is why the seam reports an echo
    at all.
    """
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(300)))
    sim.inject_register_offset_error()

    _ok(await sim.write_speed(MotorRpm(1380)))
    await _run_enabled(sim, clock, Seconds(10.0))

    status = await _status(sim)
    assert status.setpoint_echo_rpm == MotorRpm(300)  # the echo tells the truth
    assert status.output_rpm == MotorRpm(300)
    assert status.fault is None  # the frames did arrive, so the watchdog is fed


async def test_a_misaddressed_command_never_enables_the_drive() -> None:
    """The whole start sequence is acknowledged and the machine never starts."""
    clock = ManualClock()
    sim = await _opened(clock)
    sim.inject_register_offset_error()
    for word in START_SEQUENCE:
        _ok(await sim.write_command(word))
    assert _sim_state(sim) is SimState.SWITCH_ON_DISABLED


async def test_a_reversed_phase_order_turns_the_shaft_the_other_way() -> None:
    """And the echo still reads positive, so only the sign of RFRD shows it.

    A clamp written as ``rpm <= max_rpm`` instead of ``abs(rpm) <= max_rpm``
    accepts a centrifuge running backwards at full speed, which is precisely
    the bug this injection exists to fail.
    """
    clock = ManualClock()
    sim = await _started(clock)
    sim.inject_reverse()
    _ok(await sim.write_speed(MotorRpm(1380)))
    await _run_enabled(sim, clock, Seconds(11.0))  # 10 s to ramp, then steady

    status = await _status(sim)
    assert status.setpoint_echo_rpm == MotorRpm(1380)
    assert status.output_rpm == MotorRpm(-1380)
    # Current does not care which way the shaft turns.
    assert status.current == pytest.approx(1.9, abs=0.01)


# =========================================================================
# The link
# =========================================================================


async def test_nothing_works_before_the_link_is_open() -> None:
    """And the failure names the link rather than inventing a drive state."""
    clock = ManualClock()
    sim = SimulatedDrive(clock)
    assert isinstance(_err(await sim.read_status()), BadResponse)
    assert isinstance(_err(await sim.write_speed(MotorRpm(0))), BadResponse)
    assert isinstance(_err(await sim.write_command(ControlWord.SHUTDOWN)), BadResponse)


async def test_a_closed_link_yields_no_status_at_all_rather_than_a_stopped_one() -> None:
    """A fabricated 0 rpm is the lie the whole seam is arranged against.

    So there is no COMM_LOST status and no zeroed speed fields: a caller that
    cannot hear the drive has the last status and the knowledge that it is old.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.close())
    assert isinstance(_err(await sim.read_status()), BadResponse)


async def test_closing_twice_is_not_an_error() -> None:
    """atexit and a signal handler both close, and both can run."""
    clock = ManualClock()
    sim = await _opened(clock)
    _ok(await sim.close())
    _ok(await sim.close())


async def test_reopening_works_and_does_not_disturb_the_drive() -> None:
    """open() acquires a link. It must not command anything, ever."""
    clock = ManualClock()
    sim = await _spun_up(clock)
    _ok(await sim.close())
    _ok(await sim.open())
    assert _sim_state(sim) is SimState.OPERATION_ENABLED


# =========================================================================
# Emergency disable
# =========================================================================


async def test_emergency_disable_zeroes_the_reference_and_keeps_the_run_command() -> None:
    """What it does, and - just as importantly - what it must NOT do.

    It leaves the drive in OPERATION_ENABLED on purpose. That is not an
    oversight or a partial stop: a run command removed from a turning machine
    is CiA402 transition 8, so the reference is zeroed instead and the drive
    ramps the shaft down itself. This test fails if the emergency path ever goes
    back to dropping the output stage, which is the thing that turned a 10 s
    stop into a 145 s freewheel.

    The call returning means the attempt was made, not that anything has
    stopped: the ramp still takes ten seconds here and 3-4 s on the bench.
    """
    clock = ManualClock()
    sim = await _spun_up(clock)
    assert sim.emergency_disable_blocking(Seconds(1.0)) is EmergencyStopOutcome.ACKNOWLEDGED

    assert _sim_state(sim) is SimState.OPERATION_ENABLED
    status = await _status(sim)
    assert status.setpoint_echo_rpm == MotorRpm(0)
    assert status.state is DriveState.OPERATION_ENABLED
    assert status.output_rpm == MotorRpm(1380)  # nothing has slowed down yet

    # Still driving, so this is a RAMP: 138 rpm/s, standstill after 10 s.
    await _run_fed(sim, clock, Seconds(9.0))
    assert (await _status(sim)).output_rpm > MotorRpm(0), "not instant, either"
    await _run_fed(sim, clock, Seconds(1.2))
    assert (await _status(sim)).output_rpm == MotorRpm(0)


async def test_the_emergency_stop_is_the_fastest_stop_available_here() -> None:
    """The measurement behind "do not remove the run command".

    Zeroing the reference and leaving the drive enabled reaches standstill on
    the ramp; taking the output stage away as well leaves the same machine
    coasting. Ten seconds against a hundred and forty-five, on this plant's own
    numbers - and worse on the bench, where dEC is 3-4 s and tau_coast is
    unchanged.
    """
    ramp_clock = ManualClock()
    ramped = await _spun_up(ramp_clock)
    coast_clock = ManualClock()
    coasted = await _spun_up(coast_clock)

    ramped.emergency_disable_blocking(Seconds(1.0))
    coasted.emergency_disable_blocking(Seconds(1.0))
    _ok(await coasted.write_command(ControlWord.SHUTDOWN))  # the old behaviour

    await _run_fed(ramped, ramp_clock, Seconds(11.0))
    await _run_fed(coasted, coast_clock, Seconds(11.0))
    assert (await _status(ramped)).output_rpm == MotorRpm(0)
    assert (await _status(coasted)).output_rpm > MotorRpm(700)


def test_emergency_disable_needs_no_event_loop() -> None:
    """It has to be callable from atexit, a signal handler and an except branch.

    Those are exactly the places where there is no running loop, or where the
    loop is the thing that died.
    """
    clock = ManualClock()
    sim = asyncio.run(_spun_up(clock))
    assert sim.emergency_disable_blocking(Seconds(1.0)) is EmergencyStopOutcome.ACKNOWLEDGED
    assert asyncio.run(_status(sim)).setpoint_echo_rpm == MotorRpm(0)


async def test_emergency_disable_on_a_faulted_drive_still_zeroes_the_reference() -> None:
    """No state change to make, and it must not raise on the way past."""
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(600)))
    sim.inject_fault(DriveFault.INTERNAL)

    assert sim.emergency_disable_blocking(Seconds(1.0)) is EmergencyStopOutcome.ACKNOWLEDGED
    assert _sim_state(sim) is SimState.FAULT
    assert (await _status(sim)).setpoint_echo_rpm == MotorRpm(0)


def _break_by_closing_the_link(sim: SimulatedDrive) -> None:
    _ok(asyncio.run(sim.close()))


def _break_by_losing_comms(sim: SimulatedDrive) -> None:
    sim.inject_comms_loss(Seconds(5.0))


def _break_by_slow_replies(sim: SimulatedDrive) -> None:
    sim.inject_latency(Seconds(2.0))


def _break_by_misaddressing(sim: SimulatedDrive) -> None:
    sim.inject_register_offset_error()


@pytest.mark.parametrize(
    ("break_it", "reported"),
    [
        (_break_by_closing_the_link, EmergencyStopOutcome.NOTHING_SENT),
        (_break_by_losing_comms, EmergencyStopOutcome.SENT_UNCONFIRMED),
        (_break_by_slow_replies, EmergencyStopOutcome.SENT_UNCONFIRMED),
        # The nasty one: the drive really does acknowledge a write into the
        # wrong parameter, so the honest outcome is indistinguishable from
        # success. No return value can save a caller from this; only reading
        # the drive back afterwards can.
        (_break_by_misaddressing, EmergencyStopOutcome.ACKNOWLEDGED),
    ],
)
def test_emergency_disable_can_fail_and_leave_the_setpoint_commanded(
    break_it: Callable[[SimulatedDrive], None],
    reported: EmergencyStopOutcome,
) -> None:
    """The failure mode worth having a test for, now with an outcome attached.

    Four ways for the last-resort stop to achieve nothing. Three of them are
    now *reportable*, which is the whole reason the call returns something: an
    atexit or signal path that sees NOTHING_SENT knows the motor is still
    commanded at its old setpoint and can escalate, where before it saw the
    same ``None`` as a success.

    The fourth cannot be reported and is asserted as such. A safety layer has
    to verify the drive afterwards rather than trust this call, whatever it
    says.
    """
    clock = ManualClock()
    sim = asyncio.run(_spun_up(clock))
    break_it(sim)

    assert sim.emergency_disable_blocking(Seconds(1.0)) is reported
    # The reference is what the emergency write carries, so an unchanged
    # reference is the evidence that nothing happened. The state cannot be that
    # evidence any more: this call deliberately leaves the drive enabled.
    assert sim.commanded_setpoint == DEFAULT_SIM_CONFIG.nominal_rpm
    assert _sim_state(sim) is SimState.OPERATION_ENABLED


# =========================================================================
# Time
# =========================================================================


async def test_the_plant_refuses_to_be_advanced_backwards() -> None:
    """A backwards monotonic reading means two clocks got mixed up.

    Absorbing that as "no time passed" would hide the bug, so the simulation
    harness raises - matching ``ManualClock.advance``, and leaving the async
    methods unable to raise whatever the clock does.
    """
    clock = ManualClock()
    sim = await _opened(clock)
    sim.advance(clock.advance(Seconds(1.0)))
    with pytest.raises(ValueError, match="backwards"):
        sim.advance(Monotonic(0.5))


async def test_a_clock_behind_the_plant_is_a_no_op_rather_than_a_failure() -> None:
    """Because this path runs inside the async methods, where nothing may raise."""
    clock = ManualClock()
    sim = await _spun_up(clock)
    sim.advance(Monotonic(clock.monotonic() + 2.0))  # step the plant past its clock

    status = await _status(sim)  # reads the clock, which is now behind
    assert status.output_rpm == MotorRpm(1380)
    assert _sim_state(sim) is SimState.OPERATION_ENABLED


def test_the_simulator_reads_no_clock_of_its_own() -> None:
    """Contract rule 4. There is a repo-wide grep test; this one is local.

    A plant that read the system clock would make a 45-minute session take
    45 minutes to test, and the closed-loop evidence for the control law is the
    only evidence there is before the hardware exists.
    """
    source = SIM_SOURCE.read_text(encoding="utf-8")
    assert not DIRECT_TIME_CALL.search(source)
    assert "import time" not in source


# =========================================================================
# The invariants coverage cannot give
# =========================================================================


async def _check_speed_invariants(steps: Sequence[tuple[int, float]]) -> None:
    """Drive the plant through an arbitrary schedule, asserting the two bounds."""
    clock = ManualClock()
    sim = await _started(clock)
    previous = MotorRpm(0)
    for raw_setpoint, raw_dt in steps:
        dt = Seconds(raw_dt)
        _ok(await sim.write_speed(MotorRpm(raw_setpoint)))
        sim.advance(clock.advance(dt))
        observed = (await _status(sim)).output_rpm

        assert abs(observed) <= DEFAULT_SIM_CONFIG.max_rpm
        # The slew bound carries one rpm of slack because the drive reports
        # whole rpm: both ends of the comparison are rounded, so an exactly
        # rate-limited step can read one count wider than it really was.
        assert abs(observed - previous) <= sim.ramp_rate * dt + 1.0
        previous = observed


@given(
    steps=st.lists(
        st.tuples(
            st.integers(min_value=-2000, max_value=2000),
            st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
        ),
        min_size=1,
        max_size=25,
    )
)
@settings(deadline=None, max_examples=150)
def test_the_shaft_respects_its_ceiling_and_its_slew_rate_always(
    steps: Sequence[tuple[int, float]],
) -> None:
    """For ANY sequence of setpoints and time steps, whatever the drive is doing.

    These two bounds are what the safety layer is entitled to assume, and no
    amount of branch coverage implies them: a rate limiter with a sign error
    passes every example test that only ever accelerates. Setpoints are drawn
    beyond the ceiling and in both directions on purpose; the steps stay inside
    the ttO timeout so that the ramp is what is being measured.

    One ``asyncio.run`` per example, because the plant's write path is async
    while hypothesis is not.
    """
    asyncio.run(_check_speed_invariants(steps))


async def _check_invariants_with_commands(
    steps: Sequence[tuple[int, float, int]],
) -> None:
    """The same two bounds, with command words thrown in between the steps.

    The property above only ever drives an OPERATION_ENABLED drive, so it says
    nothing about the state introduced for the ramp-stop - which is also
    energised, and therefore also capable of breaking the slew bound. Here the
    schedule wanders through the state machine as well: stop on a ramp, drop the
    output stage, re-enable half way down, keep the keepalive going.

    Refused commands are expected and not asserted about; the point is that
    whatever the drive ends up doing, the shaft still obeys its ceiling and its
    rate limit.
    """
    words = tuple(ControlWord)
    clock = ManualClock()
    sim = await _started(clock)
    previous = MotorRpm(0)
    for raw_setpoint, raw_dt, word_index in steps:
        dt = Seconds(raw_dt)
        _ok(await sim.write_speed(MotorRpm(raw_setpoint)))
        await sim.write_command(words[word_index % len(words)])
        sim.advance(clock.advance(dt))
        observed = (await _status(sim)).output_rpm

        assert abs(observed) <= DEFAULT_SIM_CONFIG.max_rpm
        assert abs(observed - previous) <= sim.ramp_rate * dt + 1.0, _sim_state(sim)
        previous = observed


@given(
    steps=st.lists(
        st.tuples(
            st.integers(min_value=-2000, max_value=2000),
            st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
            st.integers(min_value=0, max_value=len(ControlWord) - 1),
        ),
        min_size=1,
        max_size=25,
    )
)
@settings(deadline=None, max_examples=150)
def test_the_bounds_hold_through_any_walk_of_the_state_machine(
    steps: Sequence[tuple[int, float, int]],
) -> None:
    """Ceiling and slew rate, for any mixture of setpoints, steps and commands.

    Added with the ramp-stop state, because that state is the second place in
    this module where the drive drives the shaft - and a rate limiter that is
    only exercised from one state is a rate limiter with one untested half.
    """
    asyncio.run(_check_invariants_with_commands(steps))


#: The case hypothesis found once the example budget was raised from the suite's
#: 150 to 3000 (seed 0). Five steps, the last of which asked the ramp for
#: 119.67 rpm of change and got 121: the shaft was at -120.66 rpm, the ramp
#: brought it to -0.99, and a standstill floor that applied under torque as well
#: as during a coast then snapped that last 0.99 rpm away in the same step.
#:
#: Small in magnitude and large in meaning: the slew bound is one of exactly two
#: properties the safety layer is entitled to assume about this plant, and a
#: model that breaks it teaches the layer above to expect something the model
#: does not provide. Kept as an explicit case because a 150-example run finds it
#: roughly never.
SLEW_VIOLATION_CASE: tuple[tuple[int, float], ...] = (
    (-84, 0.60546875),
    (-202, 0.857421875),
    (0, 0.5785372970204371),
    (0, 0.010000000000000002),
    (0, 0.8671875),
)


async def test_the_standstill_floor_cannot_break_the_slew_bound_under_torque() -> None:
    """Regression: the exact schedule that violated the bound, as a plain test.

    Runs the same assertions as the property above, so it fails for the same
    reason and with the same message, but deterministically - no seed, no
    example budget, no chance of a green run hiding it.
    """
    await _check_speed_invariants(SLEW_VIOLATION_CASE)


async def test_a_ramp_to_zero_lands_on_exactly_zero_without_a_floor() -> None:
    """Why removing the floor from the torque branch is safe as well as correct.

    The floor exists because an exponential coast never reaches zero. A ramp
    does: it assigns its target once the remaining gap is inside one step. So
    the ramp needs no floor, and a fault reaction - which ends on an exact
    comparison against 0.0 - still settles.
    """
    clock = ManualClock()
    sim = await _started(clock)
    _ok(await sim.write_speed(MotorRpm(60)))
    await _run_enabled(sim, clock, Seconds(1.0))
    assert (await _status(sim)).output_rpm == MotorRpm(60)

    _ok(await sim.write_speed(MotorRpm(0)))
    # 60 rpm at 138 rpm/s is 0.44 s. Step past it in one go and the ramp must
    # arrive at a true zero rather than at "nearly zero".
    await _run_enabled(sim, clock, Seconds(1.0))
    assert (await _status(sim)).output_rpm == MotorRpm(0)

    # And a ramp-stop fault reaction still knows when it is over.
    _ok(await sim.write_speed(MotorRpm(600)))
    await _run_enabled(sim, clock, Seconds(5.0))
    _run_silent(sim, clock, Seconds(3.2))  # silence -> SLF -> ramp to stop
    assert _sim_state(sim) is SimState.FAULT_REACTION_RAMP_STOP
    _run_silent(sim, clock, Seconds(6.0))
    assert _sim_state(sim) is SimState.FAULT, "the reaction ended on an exact zero"


# =========================================================================
# The simulator really is interchangeable with the hardware
# =========================================================================


def _accepts_a_backend(backend: DriveBackend) -> DriveBackend:
    """The static half of the conformance check.

    Passing the simulator through a parameter typed as the protocol is what
    makes a signature drift - a dropped Result, an async that became sync - a
    type error rather than a surprise the day the real driver is swapped in.
    """
    return backend


def test_the_simulator_satisfies_the_drive_seam() -> None:
    """Both halves: the static one that matters, and the runtime one startup uses."""
    sim = SimulatedDrive(ManualClock())
    assert _accepts_a_backend(sim) is sim
    assert isinstance(sim, DriveBackend)


def test_only_the_emergency_stop_is_synchronous() -> None:
    """The sync/async split is the contract, and it is copied exactly."""
    assert not inspect.iscoroutinefunction(SimulatedDrive.emergency_disable_blocking)
    for method in (
        SimulatedDrive.open,
        SimulatedDrive.close,
        SimulatedDrive.write_command,
        SimulatedDrive.write_speed,
        SimulatedDrive.read_status,
    ):
        assert inspect.iscoroutinefunction(method), method


def _imported_roots(source: Path) -> frozenset[str]:
    """Top-level names of everything a module imports, parsed from its source."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            roots.add("" if module is None else module.split(".")[0])
    return frozenset(roots)


def test_the_simulator_imports_no_hardware_library() -> None:
    """It is the hardware-free path; a serial stack in here would end that."""
    roots = _imported_roots(SIM_SOURCE)
    for banned in ("pymodbus", "serial", "bitalino", "biosppy", "numpy", "scipy"):
        assert banned not in roots, banned


def test_the_simulator_imports_only_the_standard_library_and_src() -> None:
    """An allow-list, so a new dependency has to be a deliberate edit."""
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "enum",
        "math",
        "types",
        "typing",
        "src",
    }
    assert _imported_roots(SIM_SOURCE) <= allowed
