"""Measure signal loss with the real harness, without changing its control path.

The deterministic grid has exactly 724 cases: standard_30_min and
jog_150_30_min, each with fit, unfit, fast_responder and slow_responder.
For each of these eight baselines, 30 standstill losses occur every 0.2 s
from 6 s to 0.2 s before its observed first command; 26 losses sample the
observed moving WARMUP rises and 25 sample moving HOLD ticks, at evenly
spaced order statistics including both endpoints. These 648 cases weight
the first-step boundary densely while covering the complete moving phases.
The remaining 76 losses occur at integer seconds 5..80 of the occupied
manual scenario, after its first command, covering the manual rise.
No-loss baselines determine the grid, never a guessed programme timestamp.
``--subset`` takes the first, middle and last case of each category/base.

Each loss replaces subsequent ECG readings with fresh no_signal readings
at 1 Hz until the horizon min(base horizon, loss + 15 s). Loss time is the
injection request; measurement starts at the first actual missing-rate
reading. Reading times include pre-roll and are relative to START.
The maximum rise is peak applied command minus the command immediately
before that reading; every positive tick increment is also retained.
``--delay`` can be repeated to evaluate several candidate freshness limits
in one replay. A failed property returns 1 and lists every offending case.
Only summaries are written, one CSV row per case file, never full traces.
``--dsp`` instead measures the seven reference sessions through real DSP.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import math
import sys
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from itertools import pairwise
from pathlib import Path
from time import perf_counter
from typing import Final

from simulation.harness import RunResult, run_scenario
from simulation.scenario import EcgMode, EcgQuality, Scenario, load_scenario, resolve
from simulation.signal_readings import dsp_reference
from simulation.tracefile import JsonValue, Row
from src.result import Err
from src.training.types import SafetyAction, SignalQuality
from src.units import MotorRpm, Seconds

PROGRAMS: Final = ("auto_standard_30_min", "auto_jog_subject_fit")
SUBJECTS: Final = ("fit", "unfit", "fast_responder", "slow_responder")
LOSS_WINDOW: Final = Seconds(15)
EPSILON: Final = 1e-7


class Category(Enum):
    REST = "programme_rest"
    ROTATING = "programme_rotating"
    MANUAL = "manual_occupied"


@dataclass(frozen=True, slots=True)
class LossCase:
    name: str
    category: Category
    scenario: Scenario
    at: Seconds


@dataclass(frozen=True, slots=True)
class Rise:
    t: Seconds
    amount: MotorRpm
    applied: MotorRpm
    from_rest: bool
    rule: str | None
    action: SafetyAction

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "t": self.t,
            "amount_motor_rpm": self.amount,
            "applied_motor_rpm": self.applied,
            "from_rest": self.from_rest,
            "rule": self.rule,
            "action": self.action.name,
        }


@dataclass(frozen=True, slots=True)
class LossMeasurement:
    name: str
    category: Category
    injected_at: Seconds
    last_usable: Seconds | None
    first_missing: Seconds
    before: MotorRpm
    max_rise: MotorRpm
    rises: tuple[Rise, ...]
    first_verdict: tuple[Seconds, SafetyAction, str] | None

    def violations(self, delay: Seconds) -> tuple[str, ...]:
        reasons: list[str] = []
        for rise in self.rises:
            stale = self.last_usable is None or rise.t > self.last_usable + delay + EPSILON
            if stale:
                reasons.append(f"late_rise@{rise.t:.3f}")
            if rise.from_rest and stale:
                reasons.append(f"start_without_recent_reading@{rise.t:.3f}")
            if rise.rule is not None or rise.action is not SafetyAction.NONE:
                reasons.append(f"rise_under_verdict@{rise.t:.3f}")
        return tuple(reasons)

    def to_json(self) -> dict[str, JsonValue]:
        last = None if not self.rises else self.rises[-1].t
        verdict = self.first_verdict
        return {
            "name": self.name,
            "category": self.category.value,
            "injected_at_s": self.injected_at,
            "last_usable_s": self.last_usable,
            "first_missing_s": self.first_missing,
            "before_motor_rpm": self.before,
            "max_rise_motor_rpm": self.max_rise,
            "rises": [rise.to_json() for rise in self.rises],
            "starts_from_rest": any(rise.from_rest for rise in self.rises),
            "rises_under_verdict": [
                rise.to_json()
                for rise in self.rises
                if rise.rule is not None or rise.action is not SafetyAction.NONE
            ],
            "last_rise_after_usable_s": None
            if last is None or self.last_usable is None
            else last - self.last_usable,
            "last_rise_after_missing_s": None if last is None else last - self.first_missing,
            "first_verdict": None
            if verdict is None
            else {
                "t": verdict[0],
                "action": verdict[1].name,
                "rule": verdict[2],
            },
        }


def load(name: str) -> Scenario:
    result = load_scenario(resolve(name))
    if isinstance(result, Err):
        raise ValueError(result.error.detail)
    return result.value


def run(scenario: Scenario) -> RunResult:
    logging.getLogger("src.training.runtime").setLevel(logging.ERROR)
    return asyncio.run(run_scenario(scenario))


def sample_times(rows: Sequence[Row], count: int) -> tuple[Seconds, ...]:
    if len(rows) < count:
        raise ValueError(f"need {count} eligible ticks, got {len(rows)}")
    return tuple(
        Seconds(round(rows[round(i * (len(rows) - 1) / (count - 1))].t, 6)) for i in range(count)
    )


def cases_for(scenario: Scenario, rows: Sequence[Row]) -> tuple[LossCase, ...]:
    first = next(row.t for row in rows if row.setpoint_motor_rpm > 0)
    warmup = [
        row
        for previous, row in pairwise(rows)
        if row.phase == "warmup"
        and row.measured_motor_rpm > 0
        and row.setpoint_motor_rpm > previous.setpoint_motor_rpm
    ]
    hold = [row for row in rows if row.phase == "hold" and row.measured_motor_rpm > 0]
    times = (
        (Category.REST, tuple(Seconds(round(first - 6 + i * 0.2, 6)) for i in range(30))),
        (Category.ROTATING, (*sample_times(warmup, 26), *sample_times(hold, 25))),
    )
    return tuple(
        LossCase(f"{scenario.name}-{category.value}-{i:03d}", category, scenario, at)
        for category, instants in times
        for i, at in enumerate(instants)
    )


def select_cases(*, subset: bool = False) -> tuple[LossCase, ...]:
    cases: list[LossCase] = []
    for program in PROGRAMS:
        for subject in SUBJECTS:
            base = load(program)
            scenario = replace(
                base,
                name=f"{program}-{subject}",
                subject=load(f"auto_jog_subject_{subject}").subject,
            )
            cases.extend(cases_for(scenario, run(scenario).trace.rows))
    manual = load("manual_occupied_ceiling")
    cases.extend(
        LossCase(f"manual-{i:03d}", Category.MANUAL, manual, Seconds(5 + i)) for i in range(76)
    )
    if not subset:
        return tuple(cases)
    groups: dict[tuple[str, Category], list[LossCase]] = {}
    for case in cases:
        groups.setdefault((case.scenario.name, case.category), []).append(case)
    return tuple(
        group[i] for group in groups.values() for i in (0, len(group) // 2, len(group) - 1)
    )


def extract(case: LossCase, result: RunResult) -> LossMeasurement:
    missing = next(
        reading.t
        for reading in result.readings
        if reading.t + EPSILON >= case.at and reading.bpm is None
    )
    usable = [reading.t for reading in result.readings if reading.accepted and reading.t < missing]
    rows = result.trace.rows
    before = next(
        (row.setpoint_motor_rpm for row in reversed(rows) if row.t < missing - EPSILON), 0
    )
    rises = tuple(
        Rise(
            Seconds(row.t),
            MotorRpm(row.setpoint_motor_rpm - previous.setpoint_motor_rpm),
            MotorRpm(row.setpoint_motor_rpm),
            previous.setpoint_motor_rpm == 0,
            row.safety_rule,
            SafetyAction[row.safety_action],
        )
        for previous, row in zip(
            (replace(rows[0], setpoint_motor_rpm=0), *rows[:-1]), rows, strict=True
        )
        if row.t + EPSILON >= missing and row.setpoint_motor_rpm > previous.setpoint_motor_rpm
    )
    verdict = next(
        (
            (Seconds(row.t), SafetyAction[row.safety_action], row.safety_rule)
            for row in rows
            if row.t + EPSILON >= missing and row.safety_rule is not None
        ),
        None,
    )
    peak = max(
        (row.setpoint_motor_rpm for row in rows if row.t + EPSILON >= missing), default=before
    )
    return LossMeasurement(
        case.name,
        case.category,
        case.at,
        max(usable, default=None),
        missing,
        MotorRpm(before),
        MotorRpm(max(0, peak - before)),
        rises,
        verdict,
    )


def replay(case: LossCase) -> LossMeasurement:
    horizon = Seconds(min(case.scenario.duration, case.at + LOSS_WINDOW))
    scenario = replace(
        case.scenario,
        duration=horizon,
        ecg=replace(case.scenario.ecg, mode=EcgMode.DIRECT, period=Seconds(1)),
        actions=tuple(
            sorted(
                (
                    *case.scenario.actions,
                    EcgQuality(case.at, SignalQuality.NO_SIGNAL, Seconds(LOSS_WINDOW + 1)),
                ),
                key=lambda action: action.at,
            )
        ),
    )
    return extract(case, run(scenario))


def write_csv(path: Path, entries: Sequence[dict[str, JsonValue]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(entries[0]))
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {key: json.dumps(value, sort_keys=True) for key, value in entry.items()}
            )


def loss_summary(
    measurements: Sequence[LossMeasurement], delays: Sequence[Seconds]
) -> dict[str, JsonValue]:
    categories: dict[str, JsonValue] = {}
    for category in Category:
        group = [measurement for measurement in measurements if measurement.category is category]
        categories[category.value] = {
            "count": len(group),
            "max_rise_motor_rpm": max((m.max_rise for m in group), default=0),
            "max_rise_already_moving_motor_rpm": max(
                (m.max_rise for m in group if m.before > 0), default=0
            ),
            "starts_from_rest": sum(any(r.from_rest for r in m.rises) for m in group),
            "max_last_rise_after_missing_s": max(
                (m.rises[-1].t - m.first_missing for m in group if m.rises), default=None
            ),
            "max_last_rise_after_usable_s": max(
                (
                    m.rises[-1].t - m.last_usable
                    for m in group
                    if m.rises and m.last_usable is not None
                ),
                default=None,
            ),
        }
    return {
        "count": len(measurements),
        "categories": categories,
        "acceptance": {
            str(delay): [
                {"name": m.name, "reasons": list(m.violations(delay))}
                for m in measurements
                if m.violations(delay)
            ]
            for delay in delays
        },
        "measurements": [m.to_json() for m in measurements],
    }


class _Args(argparse.Namespace):
    out: Path = Path()
    jobs: int = 4
    delay: Sequence[float] | None = None
    subset: bool = False
    dsp: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--jobs", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--delay", type=float, action="append", default=[])
    parser.add_argument("--subset", action="store_true")
    parser.add_argument("--dsp", action="store_true")
    args = parser.parse_args(argv, namespace=_Args())
    delays = args.delay or ()
    if any(not math.isfinite(d) or d < 0 for d in delays):
        parser.error("--delay must be finite and non-negative")
    started = perf_counter()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.dsp:
        summary = dsp_reference(args.out, args.jobs)
        failed = False
    else:
        cases = select_cases(subset=args.subset)
        if args.jobs == 1:
            measurements = tuple(map(replay, cases))
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as pool:
                measurements = tuple(pool.map(replay, cases))
        for measurement in measurements:
            write_csv(args.out / f"{measurement.name}.csv", [measurement.to_json()])
        summary = loss_summary(measurements, [Seconds(d) for d in delays])
        write_csv(args.out / "instants.csv", [m.to_json() for m in measurements])
        sys.stdout.write(json.dumps(summary["categories"], indent=2) + "\n")
        sys.stdout.write(json.dumps(summary["acceptance"], indent=2) + "\n")
        failed = any(m.violations(Seconds(d)) for m in measurements for d in delays)
    summary["wall_seconds"] = perf_counter() - started
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
