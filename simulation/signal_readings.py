"""DSP reference statistics from inputs accepted or rejected by the real tracker.

Completed accepted-reading gaps include the last pre-start accepted reading;
the final unfinished gap is censored and reported separately. Histogram bins
1..4 use nearest-second boundaries (1.5, 2.5, 3.5, 4.5); subsequent bins end
at 10 and 20 s. Threshold counts and exposure include the censored tail.
For threshold D, exposure is the intersection of [previous accepted + D,
next accepted or session end] with observed WARMUP/HOLD tick intervals.
Each intersecting gap is counted once per phase, including phase crossings.
The phase on the right-hand tick owns its preceding interval, as the runtime
counters do. No gap is inferred from per-tick display values.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Final

from simulation.harness import RunResult, run_scenario
from simulation.scenario import EcgMode, Scenario, load_scenario, resolve
from simulation.tracefile import JsonValue, Row
from src.result import Err
from src.training.types import Phase, SafetyAction
from src.units import Seconds

REFERENCES: Final = (
    "auto_jog_150_dsp",
    "fault_bitalino_disconnect_dsp",
    "fault_ecg_electrode_off_dsp",
    "fault_ecg_mains_burst_dsp",
    "auto_standard_30_min",
    "auto_standard_45_min",
    "auto_jog_150_nominal",
)
THRESHOLDS: Final = (Seconds(1.5), Seconds(2.5), Seconds(3.0), Seconds(4.0))
GAP_BINS: Final = ("1", "2", "3", "4", "5-10", "10-20", ">20")
GAP_LIMITS: Final = (1.5, 2.5, 3.5, 4.5, 10.0, 20.0, float("inf"))
EPSILON: Final = 1e-7


def exposure(
    gaps: Sequence[tuple[Seconds, Seconds]], rows: Sequence[Row], threshold: Seconds, phase: Phase
) -> dict[str, JsonValue]:
    durations = [
        sum(
            max(0.0, min(end, row.t) - max(start + threshold, previous.t))
            for previous, row in pairwise(rows)
            if row.phase == phase.value
        )
        for start, end in gaps
    ]
    return {"count": sum(duration > EPSILON for duration in durations), "seconds": sum(durations)}


def measure_readings(result: RunResult) -> dict[str, JsonValue]:
    rows = result.trace.rows
    end = Seconds(next((row.t for row in rows if row.state == "finished"), rows[-1].t))
    readings = [reading for reading in result.readings if 0 <= reading.t <= end]
    accepted = [reading.t for reading in result.readings if reading.accepted and reading.t <= end]
    before = [at for at in accepted if at < 0]
    accepted = [*before[-1:], *(at for at in accepted if at >= 0)]
    completed = list(pairwise(accepted))
    gaps = [*completed, (accepted[-1] if accepted else Seconds(0), end)]
    histogram: dict[str, int] = dict.fromkeys(GAP_BINS, 0)
    for start, stop in completed:
        index = next(i for i, limit in enumerate(GAP_LIMITS) if stop - start <= limit + 1e-7)
        histogram[GAP_BINS[index]] += 1
    timeline: list[JsonValue] = []
    previous_verdict: tuple[SafetyAction, str | None] = (SafetyAction.NONE, None)
    for row in rows:
        verdict = (SafetyAction[row.safety_action], row.safety_rule)
        if verdict != previous_verdict:
            timeline.append({"t": row.t, "action": verdict[0].name, "rule": verdict[1]})
        previous_verdict = verdict
    profile = result.profile
    hold_at = next((row.t for row in rows if row.phase == "hold"), None)
    zone_at = next(
        (
            row.t
            for row in rows
            if row.hr_live is not None
            and profile is not None
            and profile.zone_low_bpm <= row.hr_live <= profile.zone_high_bpm
        ),
        None,
    )
    early = (
        profile is not None
        and hold_at is not None
        and hold_at < profile.baseline_s + profile.warmup_max_s - 1e-7
    )
    counters = result.counters
    no_rate = sum(reading.bpm is None for reading in readings)
    return {
        "scenario": result.scenario.name,
        "duration_s": end,
        "end_reason": result.trace.final.end_reason,
        "first_movement_s": next((row.t for row in rows if row.measured_motor_rpm > 0), None),
        "first_command_s": next((row.t for row in rows if row.setpoint_motor_rpm > 0), None),
        "zone_reached_s": zone_at,
        "hold_started_s": hold_at,
        "warmup_ended_early": early,
        "warmup_zone_arrival_s": hold_at if early else None,
        "max_motor_rpm": max(row.measured_motor_rpm for row in rows),
        "max_applied_motor_rpm": max(row.setpoint_motor_rpm for row in rows),
        "counters_s": {
            "in_zone": counters.in_zone,
            "below_zone": counters.below_zone,
            "above_zone": counters.above_zone,
            "total": counters.total,
        },
        "readings": len(readings),
        "without_rate": no_rate,
        "without_rate_fraction": no_rate / len(readings) if readings else None,
        "accepted": sum(reading.accepted for reading in readings),
        "quality_counts": dict(Counter(reading.quality.value for reading in readings)),
        "gap_histogram": histogram,
        "completed_gaps": len(completed),
        "longest_completed_gap_s": max((b - a for a, b in completed), default=None),
        "censored_tail_s": end - (accepted[-1] if accepted else 0.0),
        "gap_exposure": {
            phase.value: {
                str(d): exposure(gaps, (replace(rows[0], t=0), *rows), d, phase) for d in THRESHOLDS
            }
            for phase in (Phase.WARMUP, Phase.HOLD)
        },
        "verdicts": timeline,
    }


def reference_scenario(name: str) -> Scenario:
    loaded = load_scenario(resolve(name))
    if isinstance(loaded, Err):
        raise ValueError(loaded.error.detail)
    base = loaded.value
    return replace(base, ecg=replace(base.ecg, mode=EcgMode.DSP))


def reference(name: str) -> dict[str, JsonValue]:
    logging.getLogger("src.training.runtime").setLevel(logging.ERROR)
    return measure_readings(asyncio.run(run_scenario(reference_scenario(name))))


def dsp_reference(out: Path, jobs: int) -> dict[str, JsonValue]:
    if jobs == 1:
        entries = list(map(reference, REFERENCES))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            entries = list(pool.map(reference, REFERENCES))
    with (out / "dsp.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(entries[0]))
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {key: json.dumps(value, sort_keys=True) for key, value in entry.items()}
            )
    return {"sessions": entries}
