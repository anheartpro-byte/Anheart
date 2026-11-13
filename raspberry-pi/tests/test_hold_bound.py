"""How long the dashboard holds the rider of a silent programme (ANH-233), on the console's side.

The dashboard stops holding the rider and the machine of an interrupted auto
session once its start, its duration and ``AUTO_RAMP_MARGIN_MS``
(``convex/training.ts``) have passed. That margin is ``auto_hold_margin_s`` of
the shared file ``contracts/hold-bound.json``, to which the dashboard's tests
hold the constant. This test keeps it long enough: it works out, with this
console's own code and files, the longest a working console can still be busy
with a programme past its planned duration, and fails unless the margin is
that, rounded up to the minute. A medical file signed with slower motion
limits, a slower programme slew, a longer overrun grace or a longer shipped
recovery fails here.

The worst case, in order:

* the console arms up to ``repeated_start_window_s`` after the start the
  server dated (a repeated confirmation, contract 1.8);
* ``session_overrun`` ends a programme still running ``overrun_grace`` past
  its duration, and an ending opened in time opens no later;
* that ending's descent from the nameplate speed, as the runtime expects it
  (``TrainingRuntime._expected_descent``): the walk on the signed motion
  limits plus the passage to zero, or twice the speed over the programme
  slew, whichever is longer, plus the drive's own ramp. The walk is judged at
  the widest radius a console accepts, where the g-rate limit is slowest;
* then the profile's whole recovery again, motor stopped, the rider on board.

A custom profile with a longer recovery, or allowed above the nameplate, is
not covered: an open point (docs/convex.md).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

from src.geometry import MachineGeometry
from src.local_config import MAX_ARM_RADIUS_M
from src.local_panel import RUNTIME_LIMITS
from src.medical import SHIPPED_PARAMETERS
from src.result import Ok
from src.training.motion import limits_of, ramp_duration
from src.training.plan import (
    COMMISSIONED_DECEL_S,
    MIN_RECOVERY_S,
    NAMEPLATE_MOTOR_RPM,
    SHIPPED_DEFAULTS_PATH,
    parse_store,
)
from src.training.safety import SafetyLimits
from src.units import Metres, MotorRpm

SHARED: Final[Path] = Path(__file__).resolve().parents[2] / "contracts" / "hold-bound.json"


def shared_seconds(key: str) -> float:
    """One number of the shared file, in seconds."""
    loaded: object = json.loads(SHARED.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]  # narrowed below
    assert isinstance(loaded, dict)
    value = cast("Mapping[str, object]", loaded)[key]
    assert isinstance(value, int | float)
    return float(value)


def longest_late_ending() -> float:
    """The descent from the nameplate, then the longest shipped recovery: a late ending."""
    store = parse_store(SHIPPED_DEFAULTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(store, Ok)
    profiles = store.value.profiles
    slew = float(RUNTIME_LIMITS.slew)
    widest = MachineGeometry(radius=Metres(MAX_ARM_RADIUS_M))
    walk = ramp_duration(
        NAMEPLATE_MOTOR_RPM, MotorRpm(0), limits_of(SHIPPED_PARAMETERS.motion_limits), widest
    )
    passage = max(profile.min_run_rpm for profile in profiles) / slew
    # As the runtime counts it: a walk that never reaches zero counts as none.
    walked = (0.0 if walk is None else float(walk)) + passage
    descent = max(walked, 2.0 * NAMEPLATE_MOTOR_RPM / slew) + COMMISSIONED_DECEL_S
    recovery = max(MIN_RECOVERY_S, *(profile.recovery_s for profile in profiles))
    return descent + recovery


def test_the_margin_is_the_longest_a_working_console_runs_a_programme_past_its_duration() -> None:
    grace = SafetyLimits(
        hard_max_bpm=SHIPPED_PARAMETERS.hr_hard_max_bpm,
        critical_bpm=SHIPPED_PARAMETERS.hr_critical_bpm,
    ).overrun_grace
    worst = shared_seconds("repeated_start_window_s") + grace + longest_late_ending()
    assert shared_seconds("auto_hold_margin_s") == math.ceil(worst / 60.0) * 60.0, worst
