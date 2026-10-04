"""Cross-check: the harness climbs exactly like the production composition root.

The battery drives :class:`~src.training.runtime.TrainingRuntime` directly
(programmes are refused by the console until milestone M5). This test builds
the REAL console with :func:`src.local_panel.build_panel` - simulated drive,
simulated BITalino, real DSP - and checks that a manual climb to 27 output rpm
walks through exactly the same setpoints as the harness's ``manual_27_rpm``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from simulation.rig import load_geometry
from simulation.scenario import SCENARIO_DIR
from simulation.tests.conftest import run_file
from src.bitalino_client import SampleBatch
from src.clock import ManualClock
from src.ecg_pipeline import EcgFrame, Treatment, treat_ecg
from src.local_config import load_local_config
from src.local_panel import build_panel
from src.result import Ok
from src.training.types import Occupancy
from src.units import Monotonic, OutputRpm, Seconds, UnixMillis

_RIG = load_geometry()
assert isinstance(_RIG, Ok), _RIG

ENV = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
    "MOTOR_MAX_RPM": "1380",
    # The harness judges the g-rate at the rig's leg tip; so must the console.
    "LEG_TIP_RADIUS_M": str(float(_RIG.value.leg_tip_radius)),
}


async def _inline(treatment: Treatment, batch: SampleBatch) -> EcgFrame | None:
    return treat_ecg(treatment, batch)


def _climb(setpoints: list[int]) -> list[int]:
    """The distinct setpoints on the way up to the first peak."""
    out: list[int] = []
    for value in setpoints:
        if not out or value != out[-1]:
            out.append(value)
        if value == max(setpoints):
            break
    return out


async def _console_climb(tmp_path: Path) -> list[int]:
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok)
    config = loaded.value
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    panel = build_panel(config, clock=clock, profiles_path=tmp_path / "p.json", treat=_inline)
    runtime = panel.runtime
    assert isinstance(runtime.confirm_estop_wiring("sim operator"), Ok)
    started = await runtime.start_manual(Occupancy.BENCH, "sim operator", config.motor_max_rpm)
    assert isinstance(started, Ok)
    assert isinstance(runtime.set_manual_target(OutputRpm(27.0)), Ok)
    console: list[int] = []
    for _ in range(round(130.0 / 0.2)):
        clock.advance(Seconds(0.2))
        panel.surface.note_presence("sim operator")  # the attendant's page pings
        await panel.ecg_step()
        console.append(int((await panel.control_step()).setpoint.motor_rpm))
    await panel.close()
    return console


def test_the_console_climbs_to_27_rpm_through_the_same_setpoints(tmp_path: Path) -> None:
    console = asyncio.run(_console_climb(tmp_path))
    harness = [
        row.setpoint_motor_rpm for row in run_file(SCENARIO_DIR / "manual_27_rpm.json").trace.rows
    ]
    assert max(console) == 1344
    assert _climb(console) == _climb(harness)
