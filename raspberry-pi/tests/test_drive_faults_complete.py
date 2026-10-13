"""Every fault the ATV320 can latch stops the machine, and says which.

The owner's requirement, tested code by code: for EACH of the 61 values of the
drive's LFT enumeration (and for a code no table knows), injected while the arm
turns under a real console (``build_panel``, simulated drive):

* the arm reaches 0 and the drive, read afresh, reports FAULT (no torque),
* the ``drive_fault`` safety verdict is latched, naming the code shown on the
  drive's display,
* nothing resets the fault by itself and nothing restarts by itself: the drive
  is still in FAULT two minutes later, no word was written to it since the
  fault (no FAULT_RESET, no ENABLE_OPERATION) and its speed reference only
  came down,
* the operator's own reset is sent only for a fault the table lets the console
  reset, and sent or refused it puts no torque back,

and a drive found already in fault refuses every start with its mnemonic.

The value 0 is injected too: a status word that says FAULT while LFT reads
"no fault stored" stops and latches like any other, so no entry of the table
can turn a fault the drive reports into no fault.

The expected table below is copied from the manufacturer's file for THIS drive
(Schneider Electric NVE41316, "ATV320 - Communication Parameters", V3.5 of
3 January 2022, sheet "Enumerations", list LFT), NOT derived from
``src/motor/drive.py``: it is what the source table is checked against. The
file prints the codes in capitals (``CRF1``); they are written here as the
drive's four-character display shows them (``CrF1``), which is what the
console repeats. The lists of what clears each fault are copied from the
programming manual (NVE41295.06 of 04/2025, "How To Clear the Error Codes?",
page 403). Neither file is in the repository: ``docs/raspberry-pi.md`` says
where they come from.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

import pytest

from src.clock import ManualClock
from src.local_config import LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel
from src.motor.drive import (
    LFT_FAULT_CODES,
    ControlWord,
    DriveError,
    DriveFault,
    DriveState,
    FaultCategory,
    describe_fault,
)
from src.motor.simulated import SimState, SimulatedDrive
from src.result import Err, Ok, Result
from src.training.runtime import RuntimeState
from src.training.safety import RULE_DRIVE_FAULT
from src.training.types import Occupancy
from src.units import Monotonic, MotorRpm, OutputRpm, RawRegister, Seconds, UnixMillis

SCHNEIDER_LFT: Final[tuple[tuple[int, str], ...]] = (
    (0, "nOF"),
    (2, "EEF1"),
    (3, "CFF"),
    (4, "CFI"),
    (5, "SLF1"),
    (6, "ILF"),
    (7, "CnF"),
    (8, "EPF1"),
    (9, "OCF"),
    (10, "CrF1"),
    (11, "SPF"),
    (12, "AnF"),
    (16, "OHF"),
    (17, "OLF"),
    (18, "ObF"),
    (19, "OSF"),
    (20, "OPF1"),
    (21, "PHF"),
    (22, "USF"),
    (23, "SCF1"),
    (24, "SOF"),
    (25, "tnF"),
    (26, "InF1"),
    (27, "InF2"),
    (28, "InF3"),
    (29, "InF4"),
    (30, "EEF2"),
    (32, "SCF3"),
    (33, "OPF2"),
    (34, "COF"),
    (35, "bLF"),
    (38, "EPF2"),
    (41, "brF"),
    (42, "SLF2"),
    (44, "SSF"),
    (45, "SLF3"),
    (49, "PtFL"),
    (50, "OtFL"),
    (51, "InF9"),
    (52, "InFA"),
    (53, "InFb"),
    (54, "tJF"),
    (55, "SCF4"),
    (56, "SCF5"),
    (58, "FCF1"),
    (59, "FCF2"),
    (64, "LCF"),
    (67, "HdF"),
    (68, "InF6"),
    (69, "InFE"),
    (71, "LFF3"),
    (73, "HCF"),
    (76, "dLF"),
    (77, "CFI2"),
    (99, "CSF"),
    (100, "ULF"),
    (101, "OLC"),
    (105, "ASF"),
    (107, "SAFF"),
    (108, "FbE"),
    (109, "FbES"),
)
"""(LFT value, mnemonic) as the manufacturer lists them for the ATV320, 0 = no fault."""

ATV32_ONLY: Final[tuple[tuple[int, str], ...]] = (
    (1, "InF"),
    (31, "SCF2"),
    (43, "ECF"),
    (46, "PrF"),
    (57, "SrF"),
    (61, "AI2F"),
    (66, "dCF"),
)
"""What the table held from the ATV32's enumeration and neither ATV320 document lists.

No longer named: read off this drive, each is a code the table does not know.
"""


def codes(listed: str) -> frozenset[str]:
    """The codes of a list written as the manual prints one: separated by commas."""
    return frozenset(code.strip() for code in listed.split(","))


# NVE41295.06, "How To Clear the Error Codes?": the four columns of its table,
# in the manual's capitals. Every one of the 60 faults is in exactly one.
POWER_RESET_ONLY: Final[frozenset[str]] = codes(
    "ANF, ASF, BLF, BRF, CRF1, EEF1, EEF2, FCF1, HDF, ILF, INF1, INF2, INF3, INF4, INF6, INF9, "
    "INFA, INFB, INFE, SAFF, SOF, SPF"
)
"""Cleared by switching the drive off and on, once the cause is removed."""

RESET_OR_AUTOMATIC_RESTART: Final[frozenset[str]] = codes(
    "CNF, COF, EPF1, EPF2, FBES, FCF2, LCF, LFF3, OBF, OHF, OLF, OLC, OPF1, OPF2, OSF, OTFL, "
    "PTFL, SCF4, SCF5, SLF1, SLF2, SLF3, SSF, TJF, TNF, ULF"
)
"""Cleared by a reset, or by the drive's automatic restart when that function is on."""

CLEARED_WITH_THEIR_CAUSE: Final[frozenset[str]] = codes(
    "CFF, CFI, CFI2, CSF, DLF, FBE, HCF, PHF, USF"
)
"""Cleared by the drive itself as soon as their cause disappears."""

EXTENDED_RESET_ONLY: Final[frozenset[str]] = codes("OCF, SCF1, SCF3")
"""A power reset, unless the extended fault reset is switched on (the factory leaves it off)."""

STRICTER_THAN_THE_MANUAL: Final[frozenset[str]] = codes(
    "CFF, CFI, CFI2, HCF, FbE, FbES, tnF, OPF1, OPF2, FCF2, LCF, SCF4, SCF5"
)
"""What the manual lets a reset (or the cause going away) clear, and this console refuses.

Written out so that loosening one of them is a change to this line, read by a
person: the drive's configuration in doubt, the motor or its contactors
disconnected, a short circuit.
"""

OPERATOR: Final[str] = "dr. attending"
TICK: Final[Seconds] = Seconds(0.2)
ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "MOTOR_MAX_RPM": "1380",
}

NEVER_RESET_FROM_THE_CONSOLE: Final[frozenset[str]] = frozenset(
    {
        # Short circuits, desaturation: electrical danger.
        "SCF1",
        "SCF3",
        "SCF4",
        "SCF5",
        "HdF",
        "OCF",
        # The drive's own electronics and memories.
        "InF1",
        "InF2",
        "InF3",
        "InF4",
        "InF6",
        "InF9",
        "InFA",
        "InFb",
        "InFE",
        "EEF1",
        "EEF2",
        "CrF1",
        "ILF",
        # Integrated safety functions.
        "SAFF",
        # Loss of the motor, of its load or of its measurement.
        "OPF1",
        "OPF2",
        "SOF",
        "SPF",
        "AnF",
        # The program inside the drive: its configuration is in doubt.
        "FbE",
        "FbES",
    }
)


def fault_id(fault: DriveFault) -> str:
    """The test id: the mnemonic on the drive's display."""
    return fault.mnemonic


def config() -> LocalConfig:
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok), loaded
    return loaded.value


# =========================================================================
# The table is the manufacturer's
# =========================================================================


def test_the_code_table_is_exactly_the_manufacturer_s() -> None:
    table = {int(code): fault.mnemonic for code, fault in LFT_FAULT_CODES.items()}
    assert table == dict(SCHNEIDER_LFT)
    assert len(table) == 61


def test_the_two_atv320_documents_name_the_same_sixty_faults() -> None:
    """The parameter file and the programming manual agree, and so does the table.

    Each of the 60 faults of the LFT list is in exactly one column of the
    manual's table of what clears a fault: a code one document has and the
    other lacks fails here, whichever way.
    """
    columns = (
        POWER_RESET_ONLY,
        RESET_OR_AUTOMATIC_RESTART,
        CLEARED_WITH_THEIR_CAUSE,
        EXTENDED_RESET_ONLY,
    )
    manual = (
        POWER_RESET_ONLY
        | RESET_OR_AUTOMATIC_RESTART
        | CLEARED_WITH_THEIR_CAUSE
        | EXTENDED_RESET_ONLY
    )
    assert sum(len(column) for column in columns) == len(manual) == 60
    listed = {mnemonic.upper() for code, mnemonic in SCHNEIDER_LFT if code != 0}
    assert listed == manual
    named = {fault.mnemonic.upper() for fault in LFT_FAULT_CODES.values()}
    assert named == manual | {"NOF"}


def test_the_console_resets_nothing_the_manual_clears_only_by_a_power_reset() -> None:
    """The reset policy is never looser than the manufacturer's, and where it is stricter is said.

    Fails for any fault the console would reset from a screen while the manual
    wants the drive switched off, the cause found and the power restored.
    """
    by_mnemonic = {fault.mnemonic.upper(): fault for fault in LFT_FAULT_CODES.values()}
    for mnemonic in POWER_RESET_ONLY | EXTENDED_RESET_ONLY:
        assert not by_mnemonic[mnemonic].resettable, mnemonic
    refused_although_clearable = {
        by_mnemonic[mnemonic].mnemonic
        for mnemonic in RESET_OR_AUTOMATIC_RESTART | CLEARED_WITH_THEIR_CAUSE
        if not by_mnemonic[mnemonic].resettable
    }
    assert refused_although_clearable == STRICTER_THAN_THE_MANUAL
    resettable = [fault for fault in DriveFault if fault.resettable]
    assert len(resettable) == 22


@pytest.mark.parametrize(("code", "atv32_mnemonic"), ATV32_ONLY)
def test_a_code_only_the_atv32_documents_is_not_named_after_another_drive(
    code: int, atv32_mnemonic: str
) -> None:
    """Seven codes left the table with evidence, not silently: this is what they read as now.

    An unknown code with its number, which stops and latches like any fault
    (the injection test below, case ``?``) and is never reset from the console.
    """
    report = describe_fault(RawRegister(code))
    assert report.fault is DriveFault.UNKNOWN
    assert str(code) in report.message
    assert not report.fault.resettable
    assert atv32_mnemonic not in {fault.mnemonic for fault in DriveFault}


def test_no_code_but_zero_reads_as_no_fault() -> None:
    """No entry of the table can turn a fault the drive reports into "no fault".

    Over every value a register can hold: 0 alone is "no fault stored", every
    other value is a named fault or an unknown one, and neither "no fault" nor
    "unknown" can be reset from the console. Whether a fault EXISTS is not the
    table's to say at all: the status word decides (the injection test below
    stops and latches on LFT = 0 as on any other value).
    """
    assert LFT_FAULT_CODES[RawRegister(0)] is DriveFault.NO_FAULT_STORED
    assert DriveFault.UNKNOWN not in LFT_FAULT_CODES.values()
    assert len(set(LFT_FAULT_CODES.values())) == len(LFT_FAULT_CODES)
    silent = [
        code
        for code in range(1, 0x10000)
        if describe_fault(RawRegister(code)).fault is DriveFault.NO_FAULT_STORED
    ]
    assert silent == []
    harmless = [fault for fault in DriveFault if fault.category is FaultCategory.NONE]
    assert harmless == [DriveFault.NO_FAULT_STORED]
    assert not DriveFault.NO_FAULT_STORED.resettable
    assert not DriveFault.UNKNOWN.resettable


def test_every_fault_is_described_in_french_and_categorised() -> None:
    for fault in DriveFault:
        spec = fault.spec
        assert spec.mnemonic
        assert spec.meaning.endswith("."), fault
        assert isinstance(spec.category, FaultCategory)
    assert DriveFault.UNKNOWN.category is FaultCategory.UNIDENTIFIED
    assert DriveFault.NO_FAULT_STORED.category is FaultCategory.NONE


def test_dangerous_and_unknown_faults_are_never_reset_from_the_console() -> None:
    by_mnemonic = {fault.mnemonic: fault for fault in DriveFault}
    for mnemonic in NEVER_RESET_FROM_THE_CONSOLE:
        assert not by_mnemonic[mnemonic].resettable, mnemonic
    assert not DriveFault.UNKNOWN.resettable
    assert not DriveFault.NO_FAULT_STORED.resettable
    # And the ordinary, recoverable ones are.
    for mnemonic in ("SLF1", "USF", "OLF", "OHF", "ObF", "EPF1"):
        assert by_mnemonic[mnemonic].resettable, mnemonic


@pytest.mark.parametrize("code", [13, 110, 200, 0xFFFF])
def test_a_code_no_table_knows_is_unknown_and_carries_its_number(code: int) -> None:
    report = describe_fault(RawRegister(code))
    assert report.fault is DriveFault.UNKNOWN
    assert str(code) in report.message


# =========================================================================
# Every fault stops the machine
# =========================================================================

FAULTS_TO_INJECT: Final[tuple[DriveFault, ...]] = (*LFT_FAULT_CODES.values(), DriveFault.UNKNOWN)
"""Every entry of the table, LFT = 0 included, and a code the table does not know."""


@dataclass(frozen=True, slots=True)
class Written:
    """What the console wrote to the drive, in order. The two lists grow as it writes."""

    words: list[ControlWord]
    references: list[MotorRpm]


def record_writes(monkeypatch: pytest.MonkeyPatch) -> Written:
    written = Written(words=[], references=[])
    write_command = SimulatedDrive.write_command
    write_speed = SimulatedDrive.write_speed

    async def command(self: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
        written.words.append(word)
        return await write_command(self, word)

    async def speed(self: SimulatedDrive, rpm: MotorRpm) -> Result[None, DriveError]:
        written.references.append(rpm)
        return await write_speed(self, rpm)

    monkeypatch.setattr(SimulatedDrive, "write_command", command)
    monkeypatch.setattr(SimulatedDrive, "write_speed", speed)
    return written


async def run(panel: LocalPanel, clock: ManualClock, seconds: float) -> None:
    """The control loop only: a bench session needs no heart rate, and skipping the
    ECG DSP keeps 62 faults x 150 simulated seconds to a few seconds of CPU."""
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()


def simulator_of(panel: LocalPanel) -> SimulatedDrive:
    simulator = panel.drive.simulator
    assert simulator is not None
    return simulator


async def turning(tmp_path: Path, clock: ManualClock) -> LocalPanel:
    """A real console in a manual bench session, the arm turning at 3 rpm."""
    panel = build_panel(config(), clock=clock, profiles_path=tmp_path / "p.json")
    attested = panel.surface.attest_estop_wiring(OPERATOR)
    assert isinstance(attested, Ok)
    started = panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(started, Ok)
    await run(panel, clock, 1.0)
    target = panel.surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR)
    assert isinstance(target, Ok)
    await run(panel, clock, 20.0)
    measured = panel.runtime.snapshot().measured.motor_rpm
    assert measured > 50, "the arm must be turning"
    return panel


@pytest.mark.parametrize("fault", FAULTS_TO_INJECT, ids=fault_id)
async def test_every_fault_while_turning_stops_the_arm_and_latches(
    fault: DriveFault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_writes(monkeypatch)
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = await turning(tmp_path, clock)

    simulator = simulator_of(panel)
    words_before = len(written.words)
    references_before = len(written.references)
    simulator.inject_fault(fault)
    await run(panel, clock, 120.0)

    snapshot = panel.runtime.snapshot()
    assert snapshot.measured.motor_rpm == 0, "the arm has stopped"
    assert panel.runtime.applied_rpm == 0
    # The drive itself, read afresh, says FAULT: its output stage is off (no
    # torque). The runtime's own flag deliberately stays "enabled" - a belief
    # that reads "cannot tell" as "may turn" - until a named fault reset.
    assert snapshot.drive_state is DriveState.FAULT
    assert snapshot.drive_status_age is not None
    assert snapshot.drive_status_age < 2.0
    assert panel.runtime.state in (RuntimeState.ENDING, RuntimeState.FINISHED)
    standing = panel.runtime.supervisor.standing
    assert standing is not None
    assert standing.latched
    verdicts = {v.rule for v in panel.runtime.supervisor.live} | {standing.rule}
    assert RULE_DRIVE_FAULT in verdicts or standing.rule == RULE_DRIVE_FAULT
    shown = snapshot.fault
    assert shown is not None, "the operator is told which fault"
    assert shown.fault is fault
    assert (fault.mnemonic if fault is not DriveFault.UNKNOWN else str(shown.raw_code)) in (
        shown.message
    )
    # Never reset by itself: still in FAULT, and no FAULT_RESET word, ever.
    assert snapshot.drive_state is DriveState.FAULT
    assert ControlWord.FAULT_RESET not in written.words
    # Nothing restarts by itself either: not one word since the fault, so no
    # ENABLE_OPERATION, and a speed reference that only came down, to 0.
    assert written.words[words_before:] == []
    references = written.references[references_before:]
    assert all(later <= earlier for earlier, later in pairwise(references)), references
    assert references[-1] == 0

    # The operator's own reset, asked by name. The table decides one thing: whether the
    # console sends it. Sent or refused, it puts no torque back and moves nothing, and the
    # verdict stays latched: only a named acknowledgement and a new start go further.
    asked = panel.surface.submit_fault_reset(operator=OPERATOR)
    assert isinstance(asked, Ok)
    await run(panel, clock, 5.0)
    since_fault = written.words[words_before:]
    reset_sent = ControlWord.FAULT_RESET in since_fault
    assert reset_sent is fault.resettable
    assert ControlWord.ENABLE_OPERATION not in since_fault
    output_stage_off = (
        {SimState.SWITCH_ON_DISABLED, SimState.READY_TO_SWITCH_ON}
        if fault.resettable
        else {SimState.FAULT}
    )
    assert simulator.sim_state in output_stage_off
    assert panel.runtime.snapshot().measured.motor_rpm == 0
    assert panel.runtime.applied_rpm == 0
    after_reset = panel.runtime.supervisor.standing
    assert after_reset is not None
    assert after_reset.latched
    await panel.close()


CLEARING_BY_THEMSELVES: Final[tuple[DriveFault, ...]] = tuple(
    fault
    for fault in LFT_FAULT_CODES.values()
    if fault.mnemonic.upper() in CLEARED_WITH_THEIR_CAUSE
)


@pytest.mark.parametrize("fault", CLEARING_BY_THEMSELVES, ids=fault_id)
async def test_a_fault_the_drive_clears_by_itself_leaves_the_console_latched(
    fault: DriveFault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nine faults clear "as soon as their cause disappears", the manual says (USF is one).

    The drive then leaves FAULT with nobody asking, whatever the table says of
    the reset. The stop and the latch are the console's, not the drive's: it
    enables nothing, its reference stays at 0, its verdict still stands and a
    start is refused until that verdict is acknowledged by name. The simulated
    drive does not clear a fault alone, so the same transition out of FAULT is
    taken here at the drive, behind the console's back.
    """
    assert len(CLEARING_BY_THEMSELVES) == 9
    written = record_writes(monkeypatch)
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = await turning(tmp_path, clock)
    simulator = simulator_of(panel)
    simulator.inject_fault(fault)
    await run(panel, clock, 120.0)
    faulted = simulator.sim_state
    assert faulted is SimState.FAULT

    cleared = await simulator.write_command(ControlWord.FAULT_RESET)
    assert cleared == Ok(None)
    words_before = len(written.words)
    references_before = len(written.references)
    await run(panel, clock, 60.0)

    assert ControlWord.ENABLE_OPERATION not in written.words[words_before:]
    assert set(written.references[references_before:]) <= {MotorRpm(0)}
    resting = simulator.sim_state
    assert resting in {SimState.SWITCH_ON_DISABLED, SimState.READY_TO_SWITCH_ON}
    assert panel.runtime.snapshot().measured.motor_rpm == 0
    assert panel.runtime.applied_rpm == 0
    standing = panel.runtime.supervisor.standing
    assert standing is not None
    assert standing.latched
    assert standing.rule == RULE_DRIVE_FAULT
    start = panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR)
    assert isinstance(start, Err)
    await run(panel, clock, 5.0)
    assert ControlWord.ENABLE_OPERATION not in written.words[words_before:]
    assert panel.runtime.snapshot().measured.motor_rpm == 0
    await panel.close()


@pytest.mark.parametrize(
    "fault",
    [
        DriveFault.OVERCURRENT,
        DriveFault.MODBUS_COMM_LOSS,
        DriveFault.LOAD_SLIPPING,
        DriveFault.SAFETY_FUNCTION,
        DriveFault.UNKNOWN,
    ],
    ids=fault_id,
)
async def test_a_drive_found_in_fault_refuses_every_start(
    fault: DriveFault, tmp_path: Path
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(config(), clock=clock, profiles_path=tmp_path / "p.json")
    simulator_of(panel).inject_fault(fault)
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 2.0)
    # Refused ("demarrage refuse : variateur en defaut (<mnemonic>, LFT n)"), nothing
    # armed, and the idle drive's fault latched as a verdict needing a named reset.
    assert panel.runtime.state is not RuntimeState.RUNNING
    assert panel.runtime.manual is None
    assert panel.runtime.snapshot().measured.motor_rpm == 0
    assert not panel.runtime.output_enabled
    standing = panel.runtime.supervisor.standing
    assert standing is not None
    assert standing.rule == RULE_DRIVE_FAULT
    assert standing.latched
    await panel.close()
