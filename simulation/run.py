"""``python -m simulation.run``: run scenarios, write their traces, judge them.

    python -m simulation.run jog_150_nominal            # one scenario, by name or path
    python -m simulation.run jog_150_nominal --csv      # plus a CSV of the rows
    python -m simulation.run --list                     # the battery
    python -m simulation.run --all                      # every scenario + out/summary.md
    python -m simulation.run --replay out/<folder>      # a record against today's runtime
    python -m simulation.run --replay <file>.tar.gz --json
    python -m simulation.run --export-real              # remake the simulated library records

Recordings go to ``simulation/out/<UTC>_<local_ref>/`` (git-ignored); open
them with the live viewer's ``?trace=out/<folder>``. The optional CSV uses
the opaque local reference as its filename. The exit code is 0 only when
every invariant and every expectation held.

``--replay`` gives a recorded session back to the real runtime and compares what
it decides with what was recorded (:mod:`simulation.replay`): exit code 0 when
they match, 1 on a difference or a divergence, 2 when the record cannot be
replayed at all. ``--export-real [NAME ...]`` runs battery scenarios and puts
their records in the library the gate replays (:mod:`simulation.real_records`);
with no name it remakes every simulated record the library already holds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from simulation.harness import RunResult, run_scenario
from simulation.invariants import Metrics, Violation, check_expectations, check_invariants, measure
from simulation.real_records import REAL_DIR, export, replay_path, simulated
from simulation.scenario import load_scenario, resolve, scenario_paths
from src.record.codec import Privacy
from src.result import Err

OUT_DIR: Final[Path] = Path(__file__).resolve().parent / "out"

EXIT_MATCH: Final[int] = 0
EXIT_DIFFERENT: Final[int] = 1
"""The runtime no longer decides what the record says: a finding, not a failure of the tool."""

EXIT_UNUSABLE: Final[int] = 2
"""The record cannot be replayed at all."""


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def describe(result: RunResult, metrics: Metrics, violations: Sequence[Violation]) -> str:
    """A short human summary of one run."""
    rig = result.rig
    lines = [
        f"scenario {result.scenario.name} ({result.scenario.kind.value}, "
        f"ecg {result.scenario.ecg.mode.value}): {len(result.trace.rows)} ticks, "
        f"{len(result.trace.frames)} drive frames",
        f"  start: {'refused (' + result.start_refusal + ')' if result.start_refusal else 'ok'}; "
        f"end: {metrics.end_reason}; final: {result.trace.final.runtime_state}, "
        f"drive {result.trace.final.sim_state}, shaft {result.trace.final.shaft_motor_rpm} rpm",
        f"  peak {metrics.peak_output_rpm:.2f} out rpm ({metrics.peak_motor_rpm} motor rpm, "
        f"{metrics.peak_hertz:.1f} Hz); g {metrics.peak_g_reference:.3f} at "
        f"{float(rig.reference_radius):.3f} m, {metrics.peak_g_leg_tip:.3f} at the leg tip "
        f"({float(rig.leg_tip_radius):.3f} m)",
        f"  peak setpoint rate {metrics.peak_setpoint_rate_output_rpm_s:.3f}, arm accel "
        f"{metrics.peak_arm_accel_output_rpm_s:.3f} out rpm/s "
        f"(limit {float(result.motion.output_accel)}); peak arm g-dot "
        f"{metrics.peak_g_rate_reference:.4f} g/s ref, {metrics.peak_g_rate_leg_tip:.4f} g/s "
        f"leg tip (limit {float(result.motion.g_rate)})",
        f"  in zone {_fmt(metrics.in_zone_fraction)}; rules {list(metrics.rules)}",
    ]
    if violations:
        lines.append(f"  VIOLATIONS ({len(violations)}):")
        lines.extend(f"    {violation}" for violation in violations[:20])
    else:
        lines.append("  all invariants and expectations hold")
    return "\n".join(lines)


def run_one(
    path: Path, out_dir: Path, *, csv: bool
) -> tuple[RunResult, Metrics, tuple[Violation, ...]]:
    """Run, write and judge one scenario file. Raises ``ValueError`` on an invalid file."""
    loaded = load_scenario(path)
    if isinstance(loaded, Err):
        raise ValueError(loaded.error.detail)
    result = asyncio.run(run_scenario(loaded.value))
    violations = (*check_invariants(result), *check_expectations(result))
    result.trace.write_record(out_dir, Privacy())
    if csv:
        result.trace.write_csv(out_dir / f"{result.trace.manifest.local_ref}.csv")
    return result, measure(result), violations


def status(result: RunResult, violations: Sequence[Violation]) -> str:
    """PASS, FAIL, KNOWN DEFECT (fails as documented) or FIXED? (a known defect that passed)."""
    if result.scenario.known_defect is None:
        return "PASS" if not violations else f"FAIL ({len(violations)})"
    return f"KNOWN DEFECT ({len(violations)})" if violations else "FIXED? remove known_defect"


def summary_table(entries: Sequence[tuple[RunResult, Metrics, tuple[Violation, ...]]]) -> str:
    """The battery as a Markdown table (the one in README.md)."""
    lines = [
        "| scenario | kind | end | peak out rpm | g ref | g leg tip | rules | result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result, metrics, violations in entries:
        end = (result.start_refusal and f"refused ({result.start_refusal})") or metrics.end_reason
        lines.append(
            f"| {result.scenario.name} | {result.scenario.kind.value} | {end} | "
            f"{metrics.peak_output_rpm:.1f} | {metrics.peak_g_reference:.2f} | "
            f"{metrics.peak_g_leg_tip:.2f} | {', '.join(metrics.rules) or '-'} | "
            f"{status(result, violations)} |"
        )
    return "\n".join(lines)


def replay_command(record: Path, *, as_json: bool) -> int:
    """``--replay``: print the report (text, or one line of JSON) and say how it went."""
    result = replay_path(record)
    if isinstance(result, Err):
        print(f"replay impossible: {result.error.detail}", file=sys.stderr)
        return EXIT_UNUSABLE
    report = result.value
    print(report.to_json() if as_json else report.to_text(), end="")
    return EXIT_MATCH if report.matches else EXIT_DIFFERENT


def export_command(names: Sequence[str], library: Path) -> int:
    """``--export-real``: remake the named records, or every simulated one of the library."""
    failed = False
    for name in names or simulated(library):
        exported = export(name, library)
        if isinstance(exported, Err):
            print(f"{name}: {exported.error}", file=sys.stderr)
            failed = True
        else:
            print(f"{name}: {exported.value}")
    return 1 if failed else 0


class _Args(argparse.Namespace):
    """The parsed command line, typed (a bare ``Namespace`` is all ``Any``)."""

    scenario: str | None = None
    all: bool = False
    list: bool = False
    csv: bool = False
    out: Path = OUT_DIR
    replay: Path | None = None
    json: bool = False
    export_real: Sequence[str] | None = None
    library: Path = REAL_DIR


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.run", description=__doc__)
    parser.add_argument("scenario", nargs="?", help="scenario name or path")
    parser.add_argument("--all", action="store_true", help="run every scenario")
    parser.add_argument("--list", action="store_true", help="list the scenarios")
    parser.add_argument("--csv", action="store_true", help="also write a CSV of the rows")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="output directory")
    parser.add_argument(
        "--replay", type=Path, metavar="RECORD", help="replay a record folder or its .tar.gz"
    )
    parser.add_argument("--json", action="store_true", help="with --replay: the report as JSON")
    parser.add_argument(
        "--export-real",
        nargs="*",
        metavar="NAME",
        help="put the records of these battery scenarios in the library (none: remake all)",
    )
    parser.add_argument("--library", type=Path, default=REAL_DIR, help="the library directory")
    args = parser.parse_args(argv, namespace=_Args())
    out_dir = args.out
    if args.replay is not None:
        return replay_command(args.replay, as_json=args.json)
    if args.export_real is not None:
        return export_command(args.export_real, args.library)
    if args.list:
        for path in scenario_paths():
            print(path.stem)
        return 0
    if args.all:
        entries = [run_one(path, out_dir, csv=args.csv) for path in scenario_paths()]
        for result, metrics, violations in entries:
            print(describe(result, metrics, violations))
        table = summary_table(entries)
        (out_dir / "summary.md").write_text(table + "\n", encoding="utf-8")
        (out_dir / "summary.json").write_text(
            json.dumps(
                [
                    {"scenario": r.scenario.name, "violations": [str(v) for v in vs]}
                    for r, _, vs in entries
                ],
                indent=2,
            ),
            encoding="utf-8",
        )
        print(table)
        return 0 if all(status(r, vs).startswith(("PASS", "KNOWN")) for r, _, vs in entries) else 1
    if args.scenario is None:
        parser.error("name a scenario, or use --list / --all")
    result, metrics, violations = run_one(resolve(args.scenario), out_dir, csv=args.csv)
    print(describe(result, metrics, violations))
    if result.scenario.known_defect is not None:
        print(f"  known defect: {result.scenario.known_defect}")
    print(f"  records: {out_dir}")
    return 0 if status(result, violations).startswith(("PASS", "KNOWN")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
