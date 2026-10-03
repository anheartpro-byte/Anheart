"""Every fault the ATV320 can latch stops the machine, and says which.

The owner's requirement, tested code by code: for EACH of the 67 values of the
drive's LFT enumeration (and for a code no table knows), injected while the arm
turns under a real console (``build_panel``, simulated drive):

* the arm reaches 0 and the drive, read afresh, reports FAULT (no torque),
* the ``drive_fault`` safety verdict is latched, naming the code shown on the
  drive's display,
* nothing resets the fault by itself: the drive is still in FAULT a minute later
  and no FAULT_RESET word was ever written,

and a drive found already in fault refuses every start with its mnemonic.

The expected table below is copied from the manufacturer's file
(``ATV32_communication_parameters_A1.2IE03.xls``, Enumerations sheet, LFT), NOT
derived from ``src/motor/drive.py``: it is what the source table is checked
against.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from src.motor.simulated import SimulatedDrive
from src.result import Ok, Result
from src.training.runtime import RuntimeState
from src.training.safety import RULE_DRIVE_FAULT
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, RawRegister, Seconds, UnixMillis

SCHNEIDER_LFT: Final[tuple[tuple[int, str], ...]] = (
    (0, "nOF"),
    (1, "InF"),
    (2, "EEF1"),
    (3, "CFF"),
    (4, "CFI"),
    (5, "SLF1"),
    (6, "ILF"),
    (7, "CnF"),
    (8, "EPF1"),
    (9, "OCF"),
    (10, "CrF"),
    (11, "SPF"),
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
    (31, "SCF2"),
    (32, "SCF3"),
    (33, "OPF2"),
    (34, "COF"),
    (35, "bLF"),
    (38, "EPF2"),
    (41, "brF"),
    (42, "SLF2"),
    (43, "ECF"),
    (44, "SSF"),
    (45, "SLF3"),
    (46, "PrF"),
    (49, "PtFL"),
    (50, "OtFL"),
    (51, "InF9"),
    (52, "InFA"),
    (53, "InFb"),
    (54, "tJF"),
    (55, "SCF4"),
    (56, "SCF5"),
    (57, "SrF"),
    (58, "FCF1"),
    (59, "FCF2"),
    (61, "AI2F"),
    (64, "LCF"),
    (66, "dCF"),
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
"""(LFT value, mnemonic) as the manufacturer lists them, 0 = no fault."""

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
        # Short circuits, leakage, desaturation: electrical danger.
        "SCF1",
        "SCF2",
        "SCF3",
        "SCF4",
        "SCF5",
        "dCF",
        "HdF",
        "OCF",
        # The drive's own electronics and memories.
        "InF",
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
        "CrF",
        "ILF",
        # Integrated safety functions.
        "PrF",
        "SAFF",
        # Loss of the motor or of its measurement.
        "OPF1",
        "OPF2",
        "SOF",
        "SPF",
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


@pytest.mark.parametrize("code", [12, 110, 200, 0xFFFF])
def test_a_code_no_table_knows_is_unknown_and_carries_its_number(code: int) -> None:
    report = describe_fault(RawRegister(code))
    assert report.fault is DriveFault.UNKNOWN
    assert str(code) in report.message


# =========================================================================
# Every fault stops the machine
# =========================================================================

FAULTS_TO_INJECT: Final[tuple[DriveFault, ...]] = (
    *(fault for fault in LFT_FAULT_CODES.values() if fault is not DriveFault.NO_FAULT_STORED),
    DriveFault.UNKNOWN,
)


def record_commands(monkeypatch: pytest.MonkeyPatch) -> list[ControlWord]:
    written: list[ControlWord] = []
    original = SimulatedDrive.write_command

    async def recording(self: SimulatedDrive, word: ControlWord) -> Result[None, DriveError]:
        written.append(word)
        return await original(self, word)

    monkeypatch.setattr(SimulatedDrive, "write_command", recording)
    return written


async def run(panel: LocalPanel, clock: ManualClock, seconds: float) -> None:
    """The control loop only: a bench session needs no heart rate, and skipping the
    ECG DSP keeps 67 faults x 140 simulated seconds to a few seconds of CPU."""
    for _ in range(round(seconds / TICK)):
        clock.advance(TICK)
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()


def simulator_of(panel: LocalPanel) -> SimulatedDrive:
    simulator = panel.drive.simulator
    assert simulator is not None
    return simulator


@pytest.mark.parametrize("fault", FAULTS_TO_INJECT, ids=fault_id)
async def test_every_fault_while_turning_stops_the_arm_and_latches(
    fault: DriveFault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = record_commands(monkeypatch)
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(config(), clock=clock, profiles_path=tmp_path / "p.json")
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    await run(panel, clock, 1.0)
    assert isinstance(
        panel.surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=OPERATOR), Ok
    )
    await run(panel, clock, 20.0)
    assert panel.runtime.snapshot().measured.motor_rpm > 50, "the arm must be turning"

    simulator_of(panel).inject_fault(fault)
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
    assert ControlWord.FAULT_RESET not in written
    await panel.close()


@pytest.mark.parametrize(
    "fault",
    [
        DriveFault.OVERCURRENT,
        DriveFault.MODBUS_COMM_LOSS,
        DriveFault.POWER_REMOVAL,
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
