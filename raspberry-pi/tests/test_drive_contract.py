"""Tests for the drive seam.

These are not tests that an enum has the members it has. They are the evidence
for four claims the safety layer above is allowed to rely on:

1. :func:`decode_status_word` is **total**: every one of the 65536 possible ETA
   words yields a state, and none of them raises. A decoder that could raise
   would raise inside the control loop with the motor commanded.
2. A fault **wins** over every other reading of the status word, including the
   patterns that look operational.
3. An unrecognised fault code degrades to ``UNKNOWN`` carrying the raw number.
   The module never names a fault it cannot identify.
4. The seam has **no hardware dependency** - so the simulator and the real
   drive really are interchangeable, and the closed-loop safety tests are
   testing the same types the hardware path uses.

The numbers used for status words are real ATV320-shaped words (0x06xx: bits 9
and 10 set, which this profile does not define) rather than tidy single-byte
values, plus the boundary cases 0x0000 and 0xFFFF, because masking bugs hide in
the bits nobody chose.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import assert_never, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from src.motor.acquisition import AcquisitionEvidence
from src.motor.drive import (
    ACC_LOGICAL,
    CMD_LOGICAL,
    DEC_LOGICAL,
    DEFAULT_MAX_MOTOR_HZ,
    ETA_LOGICAL,
    HSP_LOGICAL,
    LCR_LOGICAL,
    LFRD_LOGICAL,
    LFT_FAULT_CODES,
    LFT_LOGICAL,
    LSP_LOGICAL,
    REGISTER_OFFSET_MAX,
    REGISTER_OFFSET_MIN,
    RFRD_LOGICAL,
    TFR_LOGICAL,
    UNVERIFIED_PARAMETERS,
    BadResponse,
    CommTimeout,
    ControlWord,
    DriveBackend,
    DriveError,
    DriveFault,
    DriveFaulted,
    DriveLimits,
    DriveParameter,
    DriveState,
    DriveStatus,
    EmergencyStopOutcome,
    EnableUnconfirmed,
    FaultReport,
    HighSpeedAboveCeiling,
    HighSpeedAboveMaxFrequency,
    LimitViolation,
    LowSpeedNotZero,
    RegisterMap,
    StopUnconfirmed,
    UnexpectedState,
    check_limits,
    decode_current,
    decode_limits,
    decode_speed,
    decode_status_word,
    decode_tenth_hertz,
    decode_tenth_seconds,
    describe_fault,
    describe_violation,
)
from src.result import Err, Ok, Result
from src.units import Amperes, Hertz, MotorRpm, OutOfRange, RawRegister, Seconds, StatusWord

#: The full 16-bit register domain. Small enough to sweep exhaustively, which
#: is a stronger statement than any number of sampled cases.
ALL_WORDS = range(0x10000)

#: Realistic ETA words: the ATV320 sets bits 9 and 10, which this profile does
#: not define, so a decoder that forgot to mask would fail on these and pass on
#: the textbook single-byte values.
ETA_RUNNING = StatusWord(0x0637)
ETA_SWITCHED_ON = StatusWord(0x0633)
ETA_READY = StatusWord(0x0631)
ETA_SWITCH_ON_DISABLED = StatusWord(0x0640)
ETA_FAULT = StatusWord(0x0638)
ETA_NOT_READY = StatusWord(0x0600)


def _assign(target: object, name: str, value: object) -> None:
    """Attempt an attribute assignment that the type checker knows is illegal.

    Exists so the immutability tests can try the mutation at runtime without a
    suppression comment: ``status.output_rpm = ...`` is a type error, which is
    the *static* half of the guarantee, while these tests are checking the
    runtime half still holds for code that got past the checkers some other
    way (a dict of objects, an untyped caller, a future refactor).
    """
    setattr(target, name, value)


def _status(
    state: DriveState,
    *,
    word: StatusWord = ETA_RUNNING,
    fault: DriveFault | None = None,
) -> DriveStatus:
    """A DriveStatus with the fields this test does not care about filled in."""
    return DriveStatus(
        state=state,
        status_word=word,
        setpoint_echo_rpm=MotorRpm(600),
        output_rpm=MotorRpm(598),
        current=Amperes(1.9),
        fault=fault,
    )


# =========================================================================
# decode_status_word: totality
# =========================================================================


@given(st.integers(min_value=0x0000, max_value=0xFFFF))
def test_decode_status_word_is_total(raw: int) -> None:
    """Any 16-bit word decodes to a state, and nothing raises.

    The property that matters is not which state a random word maps to - it is
    that there is no input for which this function fails. It runs in the
    control loop, where an exception leaves the motor commanded while the
    traceback unwinds.
    """
    assert isinstance(decode_status_word(StatusWord(raw)), DriveState)


def test_every_word_in_the_domain_decodes_and_only_comm_lost_is_unreachable() -> None:
    """Swept exhaustively, not sampled: 65536 cases is cheaper than doubt.

    COMM_LOST must be unreachable here by construction. A lost link delivers no
    word to decode, so a decoder that could produce COMM_LOST would be
    inventing evidence about a drive it cannot hear.
    """
    reachable = {decode_status_word(StatusWord(raw)) for raw in ALL_WORDS}
    assert reachable == set(DriveState) - {DriveState.COMM_LOST}


# =========================================================================
# decode_status_word: every state
# =========================================================================


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        (ETA_RUNNING, DriveState.OPERATION_ENABLED),
        (ETA_SWITCHED_ON, DriveState.SWITCHED_ON),
        (ETA_READY, DriveState.READY),
        (ETA_SWITCH_ON_DISABLED, DriveState.SWITCH_ON_DISABLED),
        (ETA_FAULT, DriveState.FAULT),
        (ETA_NOT_READY, DriveState.NOT_READY),
    ],
)
def test_decode_status_word_names_each_state(word: StatusWord, expected: DriveState) -> None:
    assert decode_status_word(word) == expected


def test_ready_and_switched_on_are_distinguished() -> None:
    """0x21 and 0x23 are different states, whatever the commissioning notes say.

    The notes glossed 0x23 as "ready to start (switched on)", collapsing the
    two. They are not the same: SWITCHED_ON means the output stage is on and
    one command word away from turning the motor, READY means it is not. A
    start sequence that cannot tell them apart cannot verify its own progress.
    """
    assert decode_status_word(StatusWord(0x0021)) == DriveState.READY
    assert decode_status_word(StatusWord(0x0023)) == DriveState.SWITCHED_ON


# =========================================================================
# decode_status_word: fault precedence
# =========================================================================


def test_fault_wins_over_the_operational_bit_pattern() -> None:
    """0x2F is the operation-enabled pattern with the fault bit added.

    This is the case the ordering exists for. Tested after the running
    patterns, this word decodes as NOT_READY - which reads as "stopped" to
    anything that does not know better, on a drive that is faulted with a
    person in the centrifuge. Tested with a narrower mask, it decodes as
    "running" and the fault is never reported at all.
    """
    assert decode_status_word(StatusWord(0x2F)) == DriveState.FAULT
    assert decode_status_word(StatusWord(0x062F)) == DriveState.FAULT


def test_fault_reaction_active_is_a_fault() -> None:
    """0x0F: the drive is reacting to a fault, i.e. still decelerating.

    Reported as FAULT on purpose. This is the state in which the centrifuge is
    both faulted and moving, so it is the last one that may be filtered out as
    a transient.
    """
    assert decode_status_word(StatusWord(0x000F)) == DriveState.FAULT
    assert decode_status_word(StatusWord(0x060F)) == DriveState.FAULT


def test_fault_wins_over_switch_on_disabled_and_ready_patterns() -> None:
    """Every pattern plus the fault bit is a fault, never the pattern."""
    assert decode_status_word(StatusWord(0x48)) == DriveState.FAULT  # 0x40 | fault
    assert decode_status_word(StatusWord(0x29)) == DriveState.FAULT  # 0x21 | fault
    assert decode_status_word(StatusWord(0x2B)) == DriveState.FAULT  # 0x23 | fault


# =========================================================================
# decode_status_word: boundaries and undefined bits
# =========================================================================


def test_all_bits_set_is_a_fault() -> None:
    """0xFFFF: garbage, a stuck bus, or a genuinely faulted drive.

    The fault bit is set, so the answer is FAULT. Erring towards the state that
    makes the safety layer act is the correct bias for an implausible word.
    """
    assert decode_status_word(StatusWord(0xFFFF)) == DriveState.FAULT


def test_all_bits_except_the_fault_bit_is_not_ready() -> None:
    """0xFFF7 matches no pattern, so it must not be mistaken for one."""
    assert decode_status_word(StatusWord(0xFFF7)) == DriveState.NOT_READY


def test_undefined_high_bits_are_ignored() -> None:
    """Bit 15 set alongside a valid pattern must not change the decode."""
    assert decode_status_word(StatusWord(0x8027)) == DriveState.OPERATION_ENABLED
    assert decode_status_word(StatusWord(0x8000)) == DriveState.NOT_READY
    assert decode_status_word(StatusWord(0x0000)) == DriveState.NOT_READY


def test_quick_stop_active_decodes_as_not_ready() -> None:
    """0x07 (quick-stop-active) has no member of its own, and that is a trap.

    Pinned here so the trap is documented rather than discovered: during a
    quick stop the motor is STILL TURNING, yet the decoded state is NOT_READY.
    Nothing may infer motion from DriveState alone - output_rpm is the field
    that speaks about the shaft.
    """
    assert decode_status_word(StatusWord(0x0007)) == DriveState.NOT_READY


# =========================================================================
# Faults
# =========================================================================


def test_every_known_code_is_described_without_losing_the_number() -> None:
    """Each code in the table names its fault and keeps the raw value."""
    for code, expected in LFT_FAULT_CODES.items():
        report = describe_fault(code)
        assert report.fault == expected, code
        assert report.raw_code == code
        assert expected.mnemonic in report.message
        assert str(int(code)) in report.message


def test_code_zero_means_no_fault_stored_not_a_fault() -> None:
    """LFT holds the LAST fault, so it reads 0 on a healthy drive.

    If 0 were mapped to any real fault, every faultless drive would report one
    and the operator would learn to ignore the fault display. This is the one
    entry in the provisional table that is not a guess.
    """
    assert describe_fault(RawRegister(0)).fault == DriveFault.NO_FAULT_STORED


@pytest.mark.parametrize("code", [12, 110, 200, 0xFFFF])
def test_unknown_codes_degrade_to_unknown_carrying_the_raw_number(code: int) -> None:
    """The module never claims a fault it cannot identify.

    "code de defaut inconnu 110" sends an operator to read the drive's own
    display. A plausible-but-wrong name sends them to fix the wrong thing, and
    they have no way to tell which they were given.
    """
    report = describe_fault(RawRegister(code))
    assert report.fault == DriveFault.UNKNOWN
    assert report.raw_code == code
    assert str(code) in report.message
    assert f"0x{code:04X}" in report.message


def test_an_unknown_code_never_borrows_a_known_mnemonic() -> None:
    """The failure mode being excluded: an unrecognised code reading as a real fault."""
    message = describe_fault(RawRegister(110)).message
    for fault in DriveFault:
        if fault == DriveFault.UNKNOWN:
            continue
        assert fault.mnemonic not in message, fault


def test_the_code_table_can_be_corrected_without_a_code_change() -> None:
    """Commissioning must be able to extend the table (a newer firmware's codes).

    Passing a corrected table has to work, because the alternative - editing
    this module on the bench, under time pressure, next to a machine - is how
    unreviewed changes reach a safety path.
    """
    corrected: Mapping[RawRegister, DriveFault] = {RawRegister(27): DriveFault.OVERCURRENT}
    assert describe_fault(RawRegister(27), corrected).fault == DriveFault.OVERCURRENT
    # And a code that is in the default table but not in the override is then
    # unknown rather than silently falling back to the default.
    assert describe_fault(RawRegister(22), corrected).fault == DriveFault.UNKNOWN


def test_every_fault_has_a_mnemonic_and_a_meaning() -> None:
    """Adding a fault without operator-facing text must fail here, not on the machine.

    Both lookups are total mappings, so a missing entry is a KeyError at the
    worst possible moment: while somebody is trying to find out why the drive
    tripped.
    """
    for fault in DriveFault:
        assert fault.mnemonic, fault
        assert fault.meaning, fault
        assert fault.meaning.endswith((".", "?")), fault


def test_the_commissioning_notes_vocabulary_is_all_present() -> None:
    """Every mnemonic the notes ask us to surface exists (the manual numbers some: SLF1...)."""
    mnemonics = {fault.mnemonic for fault in DriveFault}
    for required in ("USF", "OPF", "OCF", "OLF", "SCF", "SLF", "nOF", "InF", "ObF"):
        assert any(m.startswith(required) for m in mnemonics), required


def test_fault_report_is_immutable() -> None:
    report = describe_fault(RawRegister(22))
    with pytest.raises(FrozenInstanceError):
        _assign(report, "fault", DriveFault.UNKNOWN)


# =========================================================================
# Control words
# =========================================================================


def test_control_words_are_the_cia402_values() -> None:
    """A typo here commands the wrong thing, so the numbers are pinned.

    From the commissioning notes: 6 ready-to-switch-on, 7 switched-on,
    15 operation-enabled (the motor may turn), 128 fault acknowledge.

    Compared through ``int()`` because that is what reaches the register, and
    because mypy's ``strict_equality`` treats an enum member and an int literal
    as non-overlapping types - which is the right call in general and exactly
    what we want to bypass here, where the integer IS the contract.
    """
    assert int(ControlWord.SHUTDOWN) == 6
    assert int(ControlWord.SWITCH_ON) == 7
    assert int(ControlWord.ENABLE_OPERATION) == 15
    assert int(ControlWord.FAULT_RESET) == 128


def test_control_words_are_integers_for_a_register_write() -> None:
    """They are written to CMD as integers, so int-ness is part of the contract."""
    assert int(ControlWord.ENABLE_OPERATION) == 15
    assert RawRegister(ControlWord.SHUTDOWN) == 6


def test_only_one_control_word_can_turn_the_motor() -> None:
    """Documented so a future "fast stop" addition has to argue with a test.

    There is no quick-stop word on purpose: the DC bus absorbs ~11 J of the
    ~420 J in the spinning rig, so a stop faster than the commissioned ramp
    trips ObF into freewheel, which is slower and uncontrolled.
    """
    assert set(ControlWord) == {
        ControlWord.SHUTDOWN,
        ControlWord.SWITCH_ON,
        ControlWord.ENABLE_OPERATION,
        ControlWord.FAULT_RESET,
    }


# =========================================================================
# DriveStatus
# =========================================================================


def test_drive_status_is_immutable() -> None:
    """A decision taken from an observation must not change under the decision."""
    status = _status(DriveState.OPERATION_ENABLED)
    with pytest.raises(FrozenInstanceError):
        _assign(status, "output_rpm", MotorRpm(900))
    with pytest.raises(FrozenInstanceError):
        _assign(status, "state", DriveState.NOT_READY)


def test_drive_status_is_slotted() -> None:
    """No instance dict: built at the polling rate, and typos cannot add fields.

    Two exception types are accepted because CPython's generated ``__setattr__``
    for a frozen **slotted** dataclass picks between them: a name that is a
    field raises ``FrozenInstanceError``, while a name that is not falls
    through to a ``super()`` call whose closure captured the pre-slots class
    and raises ``TypeError``. The contract under test is that neither
    assignment succeeds, not which exception CPython happens to choose.
    """
    status = _status(DriveState.READY)
    assert not hasattr(status, "__dict__")
    with pytest.raises((AttributeError, TypeError)):
        _assign(status, "rpm", MotorRpm(0))
    assert not hasattr(status, "rpm")


def test_fault_present_follows_the_status_word_not_the_name() -> None:
    """The status word decides whether a fault exists; `fault` only names it.

    A named fault with a non-FAULT state is a fault that was read and then
    cleared, or an LFT register still holding history. Neither is "a fault
    now", and letting each caller decide would give a different answer at each
    call site.
    """
    faulted = _status(DriveState.FAULT, word=ETA_FAULT, fault=DriveFault.UNDERVOLTAGE)
    assert faulted.fault_present

    unnamed = _status(DriveState.FAULT, word=ETA_FAULT, fault=None)
    assert unnamed.fault_present, "a fault we cannot name is still a fault"

    historical = _status(DriveState.OPERATION_ENABLED, fault=DriveFault.UNDERVOLTAGE)
    assert not historical.fault_present


def test_a_status_keeps_the_raw_word_it_was_decoded_from() -> None:
    """Kept so a log can show what the drive actually said, not our reading of it."""
    status = _status(DriveState.OPERATION_ENABLED, word=ETA_RUNNING)
    assert status.status_word == ETA_RUNNING
    assert decode_status_word(status.status_word) == status.state


# =========================================================================
# Register decoding
# =========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (600, 600),
        (32767, 32767),
        (32768, -32768),
        (65236, -300),
        (65535, -1),
    ],
)
def test_speed_registers_are_signed(raw: int, expected: int) -> None:
    """Read as unsigned, -1 rpm looks like 65535 rpm and clears every ceiling."""
    assert decode_speed(RawRegister(raw)) == MotorRpm(expected)


def test_current_is_scaled_once() -> None:
    """LCR is in 0.1 A units; the nameplate 2.15 A therefore reads around 21."""
    assert decode_current(RawRegister(0)) == Amperes(0.0)
    assert decode_current(RawRegister(21)) == pytest.approx(2.1)
    assert decode_current(RawRegister(215)) == pytest.approx(21.5)


def test_current_has_no_plausibility_ceiling_here() -> None:
    """Pinned so the absence is a decision, not an oversight.

    A 6553 A reading is obviously a decode error, but the plausible maximum is
    a property of this motor, so the check belongs with the other machine
    limits in the safety layer rather than buried in a decoder.
    """
    assert decode_current(RawRegister(0xFFFF)) == pytest.approx(6553.5)


# =========================================================================
# The register map
# =========================================================================


def test_default_map_uses_the_logical_addresses_from_the_notes() -> None:
    registers = RegisterMap()
    assert registers.cmd == CMD_LOGICAL == 8501
    assert registers.lfrd == LFRD_LOGICAL == 8602
    assert registers.eta == ETA_LOGICAL == 3201
    assert registers.rfrd == RFRD_LOGICAL == 8604
    assert registers.lcr == LCR_LOGICAL == 3204
    assert registers.lft == LFT_LOGICAL == 7121


@pytest.mark.parametrize("offset", [REGISTER_OFFSET_MIN, 0, REGISTER_OFFSET_MAX])
def test_the_offset_shifts_every_address_by_the_same_amount(offset: int) -> None:
    """One offset, applied in one place. Any address that missed it is a bug.

    A write that misses the offset does not fail: most Altivar parameters are
    writable while running, so it lands in whichever parameter is next door -
    ACC, HSP or a protection threshold - and every later read still looks fine.
    """
    registers = RegisterMap(offset=offset)
    assert registers.cmd == CMD_LOGICAL + offset
    assert registers.lfrd == LFRD_LOGICAL + offset
    assert registers.eta == ETA_LOGICAL + offset
    assert registers.rfrd == RFRD_LOGICAL + offset
    assert registers.lcr == LCR_LOGICAL + offset
    assert registers.lft == LFT_LOGICAL + offset


def test_resolve_is_the_only_place_the_offset_is_applied() -> None:
    """Any address, including one this map does not name, gets the same treatment."""
    registers = RegisterMap(offset=1)
    assert registers.resolve(ETA_LOGICAL) == 3202
    assert registers.resolve(CMD_LOGICAL) == registers.cmd


@pytest.mark.parametrize("offset", [-2, 2, 100, -8501])
def test_an_implausible_offset_refuses_to_construct(offset: int) -> None:
    """Only a 0/1-based indexing difference is a legitimate offset.

    Anything larger addresses a different parameter entirely, so the process
    refuses to start rather than writing a speed reference into one. Startup is
    the right place for this: nothing is spinning yet, and there is no motor to
    leave commanded.
    """
    with pytest.raises(ValueError, match="outside"):
        RegisterMap(offset=offset)


def test_the_register_map_is_immutable() -> None:
    """The addressing cannot change after the operator approved it."""
    registers = RegisterMap()
    with pytest.raises(FrozenInstanceError):
        _assign(registers, "offset", 1)


def test_the_addresses_are_all_distinct() -> None:
    """A copy-paste in the map would silently alias a read onto a write."""
    registers = RegisterMap()
    addresses = (
        registers.cmd,
        registers.lfrd,
        registers.eta,
        registers.rfrd,
        registers.lcr,
        registers.lft,
    )
    assert len(set(addresses)) == len(addresses)


# =========================================================================
# The closed error union
# =========================================================================


def _handle(result: Result[MotorRpm, DriveError]) -> str:
    """Every drive error handled, with assert_never proving it to the checker.

    Note the shape: narrow to ``Err(error)`` first, then match the error on its
    own. Matching variants directly inside ``Err(...)`` runs correctly but does
    not narrow the type argument, so ``assert_never`` would not see
    exhaustiveness and adding a variant would stop breaking the build.

    This function is the reason a new ``DriveError`` variant cannot ship
    unhandled: it fails to compile here, by name.
    """
    match result:
        case Ok(rpm):
            return f"ok:{rpm}"
        case Err(error):
            return _describe(error)
    raise assert_never(result)


def _describe(error: DriveError) -> str:
    """The five transport diagnoses, plus the two that are about the motor.

    Split from :func:`_handle` along the seam the union itself draws: five ways
    the wire can fail, and two statements about the output state being unknown.
    The ``assert_never`` here is the one a new variant collides with.
    """
    match error:
        case CommTimeout(after=after):
            return f"timeout:{after}"
        case BadResponse(detail=detail):
            return f"bad:{detail}"
        case UnexpectedState(expected=expected, actual=actual):
            return f"state:{expected.name}->{actual.name}"
        case DriveFaulted(fault=fault, raw_code=code):
            return f"fault:{fault.mnemonic}:{code}"
        case OutOfRange(quantity=quantity):
            return f"range:{quantity}"
        case EnableUnconfirmed() | StopUnconfirmed() as unknown:
            return _describe_unknown_output_state(unknown)
    raise assert_never(error)


def _describe_unknown_output_state(error: EnableUnconfirmed | StopUnconfirmed) -> str:
    """The two variants that say "the motor may be turning and I cannot prove it".

    Handled apart from the transport failures because the correct response
    differs in kind: a CommTimeout says "fix the link", these say "the machine
    may be running, and here is what is known about the attempt to stop it".
    """
    match error:
        case EnableUnconfirmed(reference_zeroed=zeroed, run_command_removed=removed):
            return f"enable?:{zeroed}:{removed}"
        case StopUnconfirmed(waited=waited, last_output_rpm=rpm):
            return f"stop?:{waited}:{rpm}"
    raise assert_never(error)


def test_every_error_variant_is_handled_and_carries_its_context() -> None:
    """Each variant carries what a log line or an operator screen needs.

    Bare sentinels were rejected for exactly this reason: "comms timed out"
    without the duration cannot be told apart from a tuning problem at 2am.
    """
    assert _handle(Ok(MotorRpm(600))) == "ok:600"
    assert _handle(Err(CommTimeout(Seconds(0.6)))) == "timeout:0.6"
    assert _handle(Err(BadResponse("crc mismatch"))) == "bad:crc mismatch"
    assert (
        _handle(Err(UnexpectedState(DriveState.READY, DriveState.OPERATION_ENABLED)))
        == "state:READY->OPERATION_ENABLED"
    )
    assert _handle(Err(DriveFaulted(DriveFault.UNDERVOLTAGE, RawRegister(22)))) == "fault:USF:22"
    assert _handle(Err(OutOfRange("rpm", 1500.0, 0.0, 900.0))) == "range:rpm"
    # The two that are not transport diagnoses. Each carries what the caller has
    # to branch on: whether the rollback landed, and how fast the shaft still
    # was when the stop ran out of budget.
    unconfirmed = EnableUnconfirmed(
        detail="15 may have landed",
        reference_zeroed=True,
        run_command_removed=False,
    )
    assert _handle(Err(unconfirmed)) == "enable?:True:False"
    assert (
        _handle(Err(StopUnconfirmed(Seconds(20.0), MotorRpm(420), "still turning")))
        == "stop?:20.0:420"
    )


def test_error_variants_are_immutable() -> None:
    """An error value must read the same to the logger as to the handler."""
    error = CommTimeout(Seconds(0.5))
    with pytest.raises(FrozenInstanceError):
        _assign(error, "after", Seconds(60.0))


# =========================================================================
# The seam: DriveBackend
# =========================================================================


class _RecordingBackend:
    """A backend that records what it was asked to do.

    Mutable on purpose, which is the exception rather than the rule in this
    codebase: recording calls is the whole point. It exists to prove the
    protocol is implementable with no hardware, which is the property the
    simulator and the closed-loop safety tests depend on.
    """

    def __init__(self) -> None:
        self.opened: bool = False
        self._possible_frames: int = 0
        self._address_proven: bool = False
        self.closed: bool = False
        self.commands: list[ControlWord] = []
        self.speeds: list[MotorRpm] = []
        self.emergency_timeouts: list[Seconds] = []

    @property
    def acquisition_evidence(self) -> AcquisitionEvidence:
        return AcquisitionEvidence(self._possible_frames, self._address_proven)

    async def open(self) -> Result[None, DriveError]:
        self.opened = True
        self._possible_frames += 1
        self._address_proven = True
        return Ok(None)

    async def close(self) -> Result[None, DriveError]:
        self.closed = True
        self._possible_frames += 1
        return Ok(None)

    async def write_command(self, word: ControlWord) -> Result[None, DriveError]:
        self._possible_frames += 1
        self.commands.append(word)
        return Ok(None)

    async def write_speed(self, rpm: MotorRpm) -> Result[None, DriveError]:
        self._possible_frames += 1
        self.speeds.append(rpm)
        return Ok(None)

    async def read_status(self) -> Result[DriveStatus, DriveError]:
        self._possible_frames += 1
        return Ok(_status(DriveState.OPERATION_ENABLED))

    async def read_limits(self) -> Result[DriveLimits, DriveError]:
        self._possible_frames += 1
        return Ok(BENCH_LIMITS)

    @property
    def emergency_budget(self) -> Seconds:
        return Seconds(1.0)

    def emergency_disable_blocking(self, timeout: Seconds) -> EmergencyStopOutcome:
        self._possible_frames += 1
        self.emergency_timeouts.append(timeout)
        return EmergencyStopOutcome.ACKNOWLEDGED


def _accepts_a_backend(backend: DriveBackend) -> DriveBackend:
    """The static half of the conformance check.

    Passing the fake through a parameter typed as the protocol is what makes a
    signature drift - a changed return type, a dropped ``Result``, an ``async``
    that became sync - a type error rather than a surprise at runtime.
    """
    return backend


def test_the_protocol_is_implementable_with_no_hardware() -> None:
    """Static and runtime conformance, both.

    Static conformance is the one that matters for the simulator: it is what
    guarantees the safety layer cannot tell the two implementations apart. The
    ``isinstance`` check is the runtime half, which is what a startup check has
    to use.
    """
    fake = _RecordingBackend()
    assert _accepts_a_backend(fake) is fake
    assert isinstance(fake, DriveBackend)


def test_a_class_missing_a_method_is_not_a_backend() -> None:
    """Otherwise the runtime check would be decoration."""
    assert not isinstance(object(), DriveBackend)


async def test_a_backend_reports_results_rather_than_raising() -> None:
    """Every fallible operation hands back a value the caller has to look at."""
    backend = _RecordingBackend()
    opened = await backend.open()
    assert isinstance(opened, Ok)
    assert isinstance(await backend.write_command(ControlWord.ENABLE_OPERATION), Ok)
    assert isinstance(await backend.write_speed(MotorRpm(600)), Ok)

    status = await backend.read_status()
    assert isinstance(status, Ok)
    assert status.value.state == DriveState.OPERATION_ENABLED
    assert await backend.read_limits() == Ok(BENCH_LIMITS)
    closed = await backend.close()
    assert isinstance(closed, Ok)

    assert backend.commands == [ControlWord.ENABLE_OPERATION]
    assert backend.speeds == [MotorRpm(600)]


def test_emergency_disable_is_synchronous_and_everything_else_is_not() -> None:
    """The sync/async split is the contract, not an implementation detail.

    ``emergency_disable_blocking`` has to be callable from atexit, from an OS
    signal handler, and from an except branch - places with no running event
    loop, and places where the loop may be the thing that died. If it were a
    coroutine it would silently do nothing on exactly those paths: an
    un-awaited coroutine is a warning, not a stop command.
    """
    assert not inspect.iscoroutinefunction(DriveBackend.emergency_disable_blocking)
    for method in (
        DriveBackend.open,
        DriveBackend.close,
        DriveBackend.write_command,
        DriveBackend.write_speed,
        DriveBackend.read_status,
        DriveBackend.read_limits,
    ):
        assert inspect.iscoroutinefunction(method), method


def test_emergency_disable_needs_no_event_loop() -> None:
    """Called from plain synchronous code, with no loop anywhere in sight."""
    backend = _RecordingBackend()
    outcome = backend.emergency_disable_blocking(Seconds(1.0))
    assert outcome is EmergencyStopOutcome.ACKNOWLEDGED
    assert backend.emergency_timeouts == [Seconds(1.0)]


def test_the_emergency_stop_reports_what_it_achieved() -> None:
    """The return value is part of the seam, not an implementation detail.

    The callers are ``atexit``, an OS signal handler and an ``except`` branch:
    places with no way to ask a follow-up question, and places where escalating
    is the only option left. So "no frame ever reached the drive" has to be
    distinguishable from "the drive acknowledged it", and it was not while this
    method returned ``None``.

    None of the three means the machine has stopped, which is why every member
    is named after the attempt rather than after the shaft.
    """
    assert set(EmergencyStopOutcome) == {
        EmergencyStopOutcome.ACKNOWLEDGED,
        EmergencyStopOutcome.SENT_UNCONFIRMED,
        EmergencyStopOutcome.NOTHING_SENT,
    }
    for member in EmergencyStopOutcome:
        assert "stopped" not in member.name.lower(), member


# =========================================================================
# The seam really is a seam
# =========================================================================


def test_fault_report_is_a_record_not_a_pair() -> None:
    """describe_fault returns a named record, so the raw code cannot be dropped.

    A ``(fault, message)`` tuple would have put the code in the prose, and the
    first caller that wanted the number back would have parsed it out of a
    sentence.
    """
    report: FaultReport = describe_fault(RawRegister(9))
    assert report.fault == DriveFault.OVERCURRENT
    # The raw code is a field in its own right, not only a substring of the
    # message: DriveFaulted and every log line need the number, not the prose.
    assert report.raw_code == RawRegister(9)
    assert str(int(report.raw_code)) in report.message


# =========================================================================
# The commissioned limits: read back, parsed, judged
# =========================================================================

BENCH_LIMITS = decode_limits(
    tfr=RawRegister(600),
    hsp=RawRegister(500),
    lsp=RawRegister(0),
    acc=RawRegister(30),
    dec=RawRegister(30),
)
"""What the bench read today: tFr 600, HSP 500, LSP 0, ACC 30, dEC 30."""


def test_the_limit_addresses_are_the_ones_read_on_the_bench() -> None:
    """tFr, HSP and LSP are 3103/3104/3105 - an earlier probe had them shifted by one."""
    assert (TFR_LOGICAL, HSP_LOGICAL, LSP_LOGICAL) == (3103, 3104, 3105)
    assert (ACC_LOGICAL, DEC_LOGICAL) == (9001, 9002)
    shifted = RegisterMap(offset=-1)
    assert (shifted.tfr, shifted.hsp, shifted.lsp, shifted.acc, shifted.dec) == (
        3102,
        3103,
        3104,
        9000,
        9001,
    )


def test_the_bench_registers_parse_into_units_exactly() -> None:
    """500 is exactly 50.0 Hz, so an exact comparison against a 50.0 Hz ceiling is sound."""
    assert (
        DriveLimits(
            max_frequency=Hertz(60.0),
            high_speed=Hertz(50.0),
            low_speed=Hertz(0.0),
            acceleration=Seconds(3.0),
            deceleration=Seconds(3.0),
        )
        == BENCH_LIMITS
    )
    assert BENCH_LIMITS.describe() == ("tFr=60.0 Hz, HSP=50.0 Hz, LSP=0.0 Hz, ACC=3.0 s, dEC=3.0 s")


@given(st.integers(min_value=0, max_value=0xFFFF))
def test_tenths_decode_exactly(raw: int) -> None:
    """Dividing by ten is correctly rounded, so every register round-trips."""
    assert round(decode_tenth_hertz(RawRegister(raw)) * 10) == raw
    assert round(decode_tenth_seconds(RawRegister(raw)) * 10) == raw


def test_the_bench_limits_arm_under_the_default_ceiling() -> None:
    """HSP = 50.0 Hz against the 50.0 Hz default: equal is accepted."""
    assert DEFAULT_MAX_MOTOR_HZ == 50.0
    assert check_limits(BENCH_LIMITS, DEFAULT_MAX_MOTOR_HZ) == Ok(BENCH_LIMITS)


def _limits(*, tfr: int = 600, hsp: int = 500, lsp: int = 0) -> DriveLimits:
    return decode_limits(
        tfr=RawRegister(tfr),
        hsp=RawRegister(hsp),
        lsp=RawRegister(lsp),
        acc=RawRegister(30),
        dec=RawRegister(30),
    )


def test_a_non_zero_low_speed_is_refused() -> None:
    """LSP > 0 means LFRD = 0 holds the motor at LSP: no zero reference would stop it."""
    outcome = check_limits(_limits(lsp=1), DEFAULT_MAX_MOTOR_HZ)
    assert outcome == Err(LowSpeedNotZero(low_speed=Hertz(0.1)))
    assert isinstance(outcome, Err)
    assert outcome.error.parameter is DriveParameter.LSP


def test_a_high_speed_above_max_frequency_is_refused() -> None:
    """HSP above tFr is incoherent commissioning: HSP is then not the real ceiling."""
    outcome = check_limits(_limits(tfr=400, hsp=450), DEFAULT_MAX_MOTOR_HZ)
    assert outcome == Err(
        HighSpeedAboveMaxFrequency(high_speed=Hertz(45.0), max_frequency=Hertz(40.0))
    )
    assert isinstance(outcome, Err)
    assert outcome.error.parameter is DriveParameter.HSP


def test_a_high_speed_above_the_ceiling_is_refused() -> None:
    """One count above the ceiling is enough."""
    outcome = check_limits(_limits(hsp=501), DEFAULT_MAX_MOTOR_HZ)
    assert outcome == Err(HighSpeedAboveCeiling(high_speed=Hertz(50.1), ceiling=Hertz(50.0)))
    assert isinstance(outcome, Err)
    assert outcome.error.parameter is DriveParameter.HSP


def test_a_lowered_ceiling_refuses_the_uncoupled_bench_setting() -> None:
    """The coupling procedure: lower the ceiling, and HSP = 50 Hz no longer arms."""
    outcome = check_limits(BENCH_LIMITS, Hertz(20.0))
    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, HighSpeedAboveCeiling)


def test_low_speed_is_judged_first() -> None:
    """Several violations at once: the one that breaks every stop is the one named."""
    outcome = check_limits(_limits(tfr=100, hsp=900, lsp=50), DEFAULT_MAX_MOTOR_HZ)
    assert isinstance(outcome, Err)
    assert isinstance(outcome.error, LowSpeedNotZero)


@pytest.mark.parametrize(
    ("violation", "mnemonic"),
    [
        (LowSpeedNotZero(low_speed=Hertz(5.0)), "LSP"),
        (HighSpeedAboveMaxFrequency(high_speed=Hertz(70.0), max_frequency=Hertz(60.0)), "tFr"),
        (HighSpeedAboveCeiling(high_speed=Hertz(60.0), ceiling=Hertz(50.0)), "HSP"),
    ],
)
def test_every_refusal_names_the_keypad_parameter(violation: LimitViolation, mnemonic: str) -> None:
    """The operator is standing at the keypad: the sentence names what to look for."""
    detail = describe_violation(violation)
    assert mnemonic in detail
    assert violation.parameter.value in detail


def test_a_value_that_is_not_a_violation_fails_loudly() -> None:
    """The exhaustive match's last arm, reached only by an ill-typed caller."""
    with pytest.raises(AssertionError):
        describe_violation(cast("LimitViolation", "not-a-violation"))


def test_tto_and_sll_are_listed_as_unverified_rather_than_read() -> None:
    """No guessed address: the two parameters are named for a keypad check instead."""
    named = {item.parameter for item in UNVERIFIED_PARAMETERS}
    assert named == {DriveParameter.TTO, DriveParameter.SLL}
    for item in UNVERIFIED_PARAMETERS:
        line = item.describe()
        assert line.startswith(f"{item.parameter.value}: not verified over Modbus")
        assert "check on the keypad" in line
