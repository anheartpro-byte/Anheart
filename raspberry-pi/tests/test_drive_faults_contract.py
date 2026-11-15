"""ANH-149: the console's reset policy per LFT code is the one Convex reads.

``contracts/drive-faults.json`` lists every fault the drive can latch with the
machine's policy for it; Convex closes programmed sessions after a
``manual_reset_only`` one until an intervention is written in the logbook. The
console decides the same from :attr:`~src.motor.drive.FaultSpec.resettable`:
the two must never disagree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

from src.motor.drive import DriveFault, FaultCategory

CONTRACT: Final[Path] = Path(__file__).resolve().parents[2] / "contracts" / "drive-faults.json"


def test_every_fault_the_drive_latches_has_the_console_s_policy_in_the_contract() -> None:
    document = cast("dict[str, object]", json.loads(CONTRACT.read_text(encoding="utf-8")))
    assert document["schema_version"] == 1
    assert document["policies"] == ["console_reset", "manual_reset_only"]
    console = {
        fault.spec.mnemonic: "console_reset" if fault.spec.resettable else "manual_reset_only"
        for fault in DriveFault
        if fault.spec.category is not FaultCategory.NONE
    }
    assert document["faults"] == console
    assert "nOF" not in console, "no fault stored is no fault"
