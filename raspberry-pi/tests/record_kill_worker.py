"""Subprocess fixture: the REAL console in simulation, recording a session until it is killed.

``python tests/record_kill_worker.py <records directory>``: builds the console
exactly as ``python -m src.local_panel`` does with ``MOTOR_BACKEND=sim
ECG_SOURCE=sim`` (real clock, real task wiring, real DSP, the journal on its
own thread), arms a bench manual session, prints ``ARMED`` and then runs until
the parent kills it. Nothing here stops by itself: the abrupt stop IS the test
(``tests/test_record_abrupt_stop.py``).

While it runs, a target below the slowest running speed is asked for twice a
second. The runtime refuses each one, which puts an event in the record twice a
second: the test can then say how fresh the EVENT stream was at the kill, not
only the ticks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.clock import RealClock
from src.local_config import load_local_config
from src.local_panel import LocalPanel, build_panel, open_journal
from src.result import Ok
from src.training.runtime import RuntimeState
from src.training.types import Occupancy
from src.units import OutputRpm

OPERATOR = "kill test"
TOO_SLOW = OutputRpm(0.5)
"""About 25 motor rpm: under the 55 rpm floor, so the runtime refuses it."""


class NoWeb:
    """A web runner that serves nothing and never ends by itself."""

    def __init__(self) -> None:
        self._exit: asyncio.Event = asyncio.Event()

    async def serve(self) -> None:
        await self._exit.wait()

    def request_exit(self) -> None:
        self._exit.set()


async def operate(panel: LocalPanel) -> None:
    surface = panel.surface
    await asyncio.sleep(0.3)
    if not isinstance(surface.attest_estop_wiring(OPERATOR), Ok):
        raise TypeError("the attestation was refused")
    if not isinstance(
        surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    ):
        raise TypeError("the start was refused")
    for _ in range(200):
        if panel.runtime.state is RuntimeState.RUNNING:
            break
        await asyncio.sleep(0.05)
    sys.stdout.write("ARMED\n")
    sys.stdout.flush()
    while True:
        surface.note_presence(OPERATOR)
        surface.submit_manual_target(output_rpm=TOO_SLOW, operator=OPERATOR)
        await asyncio.sleep(0.5)


async def main(root: Path) -> int:
    loaded = load_local_config(
        {
            "MOTOR_BACKEND": "sim",
            "ECG_SOURCE": "sim",
            "ARM_RADIUS_M": "1.5",
            "SENSORS": "ECG,EDA",
            "RECORD_ROOT": str(root),
        }
    )
    if not isinstance(loaded, Ok):
        raise TypeError(f"configuration refused: {loaded.error}")
    config = loaded.value
    clock = RealClock()
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=root.parent / "profiles.json",
        journal=open_journal(config, clock),
    )
    operator = asyncio.create_task(operate(panel))
    try:
        return await panel.run(asyncio.Event(), NoWeb())
    finally:
        operator.cancel()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(Path(sys.argv[1]))))
