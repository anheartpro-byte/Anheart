"""Tests for the injected clocks, including the rule that forbids reading time directly.

``test_no_direct_time_calls_in_src`` is the load-bearing one: it is what keeps
the accelerated closed-loop simulation possible. If any module in ``src/``
starts calling ``time.monotonic()`` on its own, a 45-minute session stops being
testable in under a second and the control law loses the only evidence it has
before hardware exists.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.clock import Clock, ManualClock, RealClock, SimClock
from src.units import Monotonic, Seconds, UnixMillis

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

#: A direct call site for the system clock.
DIRECT_TIME_CALL = re.compile(r"\btime\.(monotonic|time|perf_counter|monotonic_ns|time_ns)\s*\(")


def _direct_time_calls() -> dict[str, list[str]]:
    """Map each module under src/ to its direct clock-reading lines."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        if path.name == "clock.py":
            continue
        hits = [
            f"{path.relative_to(SRC_DIR.parent)}:{lineno}: {line.strip()}"
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
            if DIRECT_TIME_CALL.search(line)
        ]
        if hits:
            found[path.name] = hits
    return found


def test_no_direct_time_calls_in_src() -> None:
    """Only src/clock.py may read the system clock. See SKILL.md rule 4.

    No module is exempt: the last ones written before the rule are gone.
    """
    offenders = [line for lines in _direct_time_calls().values() for line in lines]
    assert not offenders, (
        "these modules read the clock directly instead of taking an injected Clock, "
        "which makes accelerated session tests impossible:\n  " + "\n  ".join(offenders)
    )


# --- RealClock -----------------------------------------------------------


def test_real_clock_satisfies_the_protocol() -> None:
    assert isinstance(RealClock(), Clock)


def test_real_clock_monotonic_does_not_go_backwards() -> None:
    clock = RealClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second >= first


def test_real_clock_unix_millis_is_near_the_system_clock() -> None:
    clock = RealClock()
    before = int(time.time() * 1000)
    value = clock.unix_millis()
    after = int(time.time() * 1000)
    assert before <= value <= after + 1


# --- ManualClock ---------------------------------------------------------


def test_manual_clock_satisfies_the_protocol() -> None:
    assert isinstance(ManualClock(), Clock)


def test_manual_clock_starts_where_told_and_does_not_drift() -> None:
    clock = ManualClock(start=Monotonic(100.0))
    assert clock.monotonic() == 100.0
    assert clock.monotonic() == 100.0


def test_manual_clock_advances_exactly() -> None:
    """Exactness is the point: a 10 s safety dwell is tested with exactly 10 s."""
    clock = ManualClock()
    assert clock.advance(Seconds(10.0)) == 10.0
    assert clock.advance(Seconds(0.5)) == 10.5
    assert clock.monotonic() == 10.5


def test_manual_clock_allows_a_zero_advance() -> None:
    clock = ManualClock(start=Monotonic(3.0))
    assert clock.advance(Seconds(0.0)) == 3.0


def test_manual_clock_refuses_to_run_backwards() -> None:
    """A monotonic clock cannot go back; silently allowing it would hide bugs."""
    clock = ManualClock()
    with pytest.raises(ValueError, match="monotonic"):
        clock.advance(Seconds(-1.0))


def test_manual_clock_derives_wall_time_from_its_epoch() -> None:
    clock = ManualClock(start=Monotonic(0.0), epoch_millis=UnixMillis(1_700_000_000_000))
    assert clock.unix_millis() == 1_700_000_000_000
    clock.advance(Seconds(2.5))
    assert clock.unix_millis() == 1_700_000_002_500


# --- SimClock ------------------------------------------------------------


def test_sim_clock_satisfies_the_protocol() -> None:
    assert isinstance(SimClock(), Clock)


def test_sim_clock_defaults_to_real_speed() -> None:
    clock = SimClock()
    assert clock.speed == 1.0


def test_sim_clock_rejects_non_positive_speed() -> None:
    for speed in (0.0, -1.0):
        with pytest.raises(ValueError, match="positive"):
            SimClock(speed=speed)


def test_sim_clock_scales_elapsed_time() -> None:
    """At 1000x, a few real milliseconds must read as seconds of session time."""
    clock = SimClock(speed=1000.0, origin=Monotonic(0.0))
    time.sleep(0.01)
    assert clock.monotonic() >= 5.0


def test_sim_clock_starts_at_its_origin() -> None:
    clock = SimClock(speed=60.0, origin=Monotonic(500.0))
    assert clock.monotonic() >= 500.0


def test_sim_clock_wall_time_tracks_its_epoch() -> None:
    clock = SimClock(speed=60.0, epoch_millis=UnixMillis(1_700_000_000_000))
    assert clock.unix_millis() >= 1_700_000_000_000


def test_sim_clock_reports_real_duration_for_a_simulated_one() -> None:
    """scripts/simulate_session.py needs this to size its sleeps."""
    clock = SimClock(speed=60.0)
    assert clock.real_seconds_for(Seconds(2700.0)) == 45.0
