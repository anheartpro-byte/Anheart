"""``python -m simulation.quick``: instant verdicts - scenario, subject, cohort, failures.

    python -m simulation.quick manual_27_rpm          # a scenario file (by name)
    python -m simulation.quick drive_fault_overcurrent_manual   # a failure case (by name)
    python -m simulation.quick S07                    # one cohort subject, all three sessions
    python -m simulation.quick --cohort               # 30 subjects x 3 sessions
    python -m simulation.quick --failures             # the failure matrix (DSP cases with --dsp)
    python -m simulation.quick --all                  # everything above
    python -m simulation.quick --cohort --workers 8 --out /tmp/q

As fast as the runtime allows: the DIRECT heart-rate sensor model (a
``dsp``-mode scenario is run in ``direct`` mode unless ``--dsp`` is given; one
whose actions need the real DSP is skipped), every run on its own process, the
deterministic clock. Prints a compact verdict table, then writes
``report.json`` and a self-contained ``report.html`` (charts per run: output
and setpoint rpm, measured heart rate against the zone, g at the reference
radius and at the leg tip, and the operator messages) to ``--out``.

Exit code 0 when every run is PASS or a documented known defect (XFAIL).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum, unique
from pathlib import Path
from typing import Final, assert_never

from simulation.cohort.battery import Eligibility, SessionType, run_subject
from simulation.cohort.generate import CohortSubject, load_cohort
from simulation.failures import FailureCase, cases, judge, scenario_of
from simulation.harness import DSP_ONLY_ACTIONS, RunResult, run_scenario
from simulation.invariants import check_expectations, check_invariants, measure
from simulation.scenario import EcgMode, Scenario, load_scenario, resolve, scenario_paths
from simulation.tracefile import JsonValue
from src.result import Err
from src.sim.physiology import SIGNAL_ARTIFACTS

OUT_DIR: Final[Path] = Path(__file__).resolve().parent / "out"
MAX_POINTS: Final[int] = 400
"""Chart points per series: enough to see a ramp, small enough for 150 runs in one page."""


@unique
class JobKind(Enum):
    SCENARIO = "scenario"
    FAILURE = "failure"
    COHORT = "cohort"


@dataclass(frozen=True, slots=True)
class Job:
    kind: JobKind
    key: str
    """Scenario file path, failure case name, or ``S07:auto_jog``."""

    dsp: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Series:
    t: tuple[float, ...]
    output_rpm: tuple[float, ...]
    setpoint_rpm: tuple[float, ...]
    hr_true: tuple[int | None, ...]
    hr_live: tuple[int | None, ...]
    g_reference: tuple[float, ...]
    g_leg_tip: tuple[float, ...]


EMPTY: Final[Series] = Series(
    t=(), output_rpm=(), setpoint_rpm=(), hr_true=(), hr_live=(), g_reference=(), g_leg_tip=()
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReport:
    """One run, compact: the verdict, the headline numbers, the chart data."""

    name: str
    group: JobKind
    status: str
    """PASS, FAIL, XFAIL (fails as documented), FIXED? (documented, but passed), SKIPPED."""

    end: str
    peak_output_rpm: float
    peak_g_reference: float
    peak_g_leg_tip: float
    in_zone: float | None
    rules: tuple[str, ...]
    violations: tuple[str, ...]
    known_defect: str | None
    wall_s: float
    zone: tuple[int, int] | None
    series: Series
    messages: tuple[tuple[float, str], ...]

    def to_json(self) -> Mapping[str, JsonValue]:
        return {
            "name": self.name,
            "group": self.group.value,
            "status": self.status,
            "end": self.end,
            "peak_output_rpm": round(self.peak_output_rpm, 3),
            "peak_g_reference": round(self.peak_g_reference, 4),
            "peak_g_leg_tip": round(self.peak_g_leg_tip, 4),
            "in_zone": None if self.in_zone is None else round(self.in_zone, 3),
            "rules": list(self.rules),
            "violations": list(self.violations),
            "known_defect": self.known_defect,
            "wall_s": round(self.wall_s, 3),
        }


def status_of(violations: Sequence[object], known_defect: str | None) -> str:
    if known_defect is None:
        return "PASS" if not violations else "FAIL"
    return "XFAIL" if violations else "FIXED?"


def series_of(result: RunResult) -> Series:
    rows = result.trace.rows
    rows = rows[:: max(1, len(rows) // MAX_POINTS)]
    return Series(
        t=tuple(round(row.t, 1) for row in rows),
        output_rpm=tuple(round(row.output_rpm, 2) for row in rows),
        setpoint_rpm=tuple(round(row.setpoint_output_rpm, 2) for row in rows),
        hr_true=tuple(row.hr_true for row in rows),
        hr_live=tuple(row.hr_live for row in rows),
        g_reference=tuple(round(row.g_reference, 3) for row in rows),
        g_leg_tip=tuple(round(row.g_leg_tip, 3) for row in rows),
    )


def report_of(
    name: str,
    *,
    group: JobKind,
    result: RunResult,
    violations: Sequence[str],
    known_defect: str | None,
    wall: float,
) -> RunReport:
    metrics = measure(result)
    profile = result.profile
    return RunReport(
        name=name,
        group=group,
        status=status_of(violations, known_defect),
        end=(result.start_refusal and f"refused ({result.start_refusal})")
        or metrics.end_reason
        or "-",
        peak_output_rpm=metrics.peak_output_rpm,
        peak_g_reference=metrics.peak_g_reference,
        peak_g_leg_tip=metrics.peak_g_leg_tip,
        in_zone=metrics.in_zone_fraction,
        rules=metrics.rules,
        violations=tuple(violations),
        known_defect=known_defect,
        wall_s=wall,
        zone=None if profile is None else (int(profile.zone_low_bpm), int(profile.zone_high_bpm)),
        series=series_of(result),
        messages=tuple((round(m.t, 1), m.text) for m in result.messages),
    )


def skipped(name: str, group: JobKind, why: str) -> RunReport:
    return RunReport(
        name=name,
        group=group,
        status="SKIPPED",
        end=why,
        peak_output_rpm=0.0,
        peak_g_reference=0.0,
        peak_g_leg_tip=0.0,
        in_zone=None,
        rules=(),
        violations=(),
        known_defect=None,
        wall_s=0.0,
        zone=None,
        series=EMPTY,
        messages=(),
    )


def fast(scenario: Scenario) -> Scenario | None:
    """``scenario`` on the DIRECT sensor model; ``None`` when it needs the real DSP."""
    if scenario.ecg.mode is EcgMode.DIRECT:
        return scenario
    if any(window.event in SIGNAL_ARTIFACTS for window in scenario.events):
        return None
    if any(isinstance(action, DSP_ONLY_ACTIONS) for action in scenario.actions):
        return None
    return replace(scenario, ecg=replace(scenario.ecg, mode=EcgMode.DIRECT))


def _scenario_job(job: Job) -> RunReport:
    loaded = load_scenario(Path(job.key))
    if isinstance(loaded, Err):
        raise ValueError(loaded.error.detail)
    scenario = loaded.value if job.dsp else fast(loaded.value)
    if scenario is None:
        return skipped(loaded.value.name, job.kind, "needs --dsp")
    began = time.perf_counter()
    result = asyncio.run(run_scenario(scenario))
    violations = [str(v) for v in (*check_invariants(result), *check_expectations(result))]
    wall = time.perf_counter() - began
    return report_of(
        scenario.name,
        group=job.kind,
        result=result,
        violations=violations,
        known_defect=scenario.known_defect,
        wall=wall,
    )


def _failure_job(job: Job) -> RunReport:
    case = next(case for case in cases() if case.name == job.key)
    scenario = scenario_of(case)
    if scenario.ecg.mode is EcgMode.DSP and not job.dsp:
        return skipped(case.name, job.kind, "needs --dsp")
    began = time.perf_counter()
    result = asyncio.run(run_scenario(scenario))
    violations = [str(v) for v in judge(case, result)]
    wall = time.perf_counter() - began
    return report_of(
        case.name,
        group=job.kind,
        result=result,
        violations=violations,
        known_defect=case.known_defect,
        wall=wall,
    )


def _cohort_job(job: Job) -> RunReport:
    subject_id, session_name = job.key.split(":")
    subject = next(s for s in load_cohort() if s.subject_id == subject_id)
    session = SessionType(session_name)
    kept: list[RunResult] = []
    began = time.perf_counter()
    outcome = run_subject(subject, session, keep=kept.append)
    wall = time.perf_counter() - began
    name = f"{subject_id}_{session.value}"
    if not kept:
        refused = skipped(name, job.kind, "refused")
        return replace(
            refused,
            status=status_of(outcome.violations, None),
            violations=outcome.violations,
            wall_s=wall,
            messages=((0.0, _refusal(outcome.eligibility)),),
        )
    return report_of(
        name,
        group=job.kind,
        result=kept[0],
        violations=outcome.violations,
        known_defect=outcome.known_defect,
        wall=wall,
    )


def _refusal(gate: Eligibility | None) -> str:
    return "" if gate is None or gate.refusal is None else gate.refusal


def execute(job: Job) -> RunReport:
    """One job, in whichever process runs it."""
    logging.disable(logging.CRITICAL)
    match job.kind:
        case JobKind.SCENARIO:
            return _scenario_job(job)
        case JobKind.FAILURE:
            return _failure_job(job)
        case JobKind.COHORT:
            return _cohort_job(job)
        case _ as unreachable:
            assert_never(unreachable)


def cohort_jobs(subjects: Sequence[CohortSubject]) -> list[Job]:
    return [
        Job(JobKind.COHORT, f"{subject.subject_id}:{session.value}")
        for subject in subjects
        for session in SessionType
    ]


def failure_jobs(matrix: Sequence[FailureCase], *, dsp: bool) -> list[Job]:
    return [Job(JobKind.FAILURE, case.name, dsp=dsp) for case in matrix]


def jobs_for(target: str, *, dsp: bool) -> list[Job]:
    """A failure case, a scenario file, or a cohort subject, by name. Raises ``ValueError``."""
    matrix = {case.name for case in cases()}
    if target in matrix:
        return [Job(JobKind.FAILURE, target, dsp=dsp)]
    path = resolve(target)
    if path.exists():
        return [Job(JobKind.SCENARIO, str(path), dsp=dsp)]
    subjects = [s for s in load_cohort() if s.subject_id == target.upper()]
    if subjects:
        return cohort_jobs(subjects)
    raise ValueError(f"{target!r} is neither a scenario, a failure case nor a subject id")


def run_jobs(jobs: Sequence[Job], workers: int) -> tuple[RunReport, ...]:
    if workers <= 1 or len(jobs) <= 1:
        return tuple(execute(job) for job in jobs)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return tuple(pool.map(execute, jobs))


# =========================================================================
# Output
# =========================================================================


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def table(reports: Sequence[RunReport]) -> str:
    """The compact verdict table."""
    width = max((len(r.name) for r in reports), default=10)
    lines = [
        f"{'result':7} {'run':{width}} {'end':22} {'rpm':>5} {'g leg':>5} {'zone':>5} "
        f"{'viol':>4} {'s':>5}"
    ]
    lines.extend(
        f"{r.status:7} {r.name:{width}} {r.end[:22]:22} {r.peak_output_rpm:5.1f} "
        f"{r.peak_g_leg_tip:5.2f} {_fmt(r.in_zone):>5} {len(r.violations):4d} {r.wall_s:5.2f}"
        for r in reports
    )
    return "\n".join(lines)


def counts(reports: Sequence[RunReport]) -> Mapping[str, int]:
    found: dict[str, int] = {}
    for report in reports:
        found[report.status] = found.get(report.status, 0) + 1
    return found


_CSS: Final[str] = """
:root { color-scheme: light; --bg:#fcfcfb; --card:#ffffff; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#8a8984; --grid:#e6e5e0; --zone:#e9e8e3; --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --pass:#008300; --fail:#e34948; --xfail:#c98500; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --bg:#1a1a19; --card:#232322; --ink:#ffffff; --ink2:#c3c2b7; --muted:#8f8e86; --grid:#3a3a37;
  --zone:#34342f; --s1:#3987e5; --s2:#d95926; --s3:#199e70; --pass:#3fae3f; --fail:#e66767;
  --xfail:#e0a100; } }
:root[data-theme="dark"] { color-scheme: dark; --bg:#1a1a19; --card:#232322; --ink:#ffffff;
  --ink2:#c3c2b7; --muted:#8f8e86; --grid:#3a3a37; --zone:#34342f; --s1:#3987e5; --s2:#d95926;
  --s3:#199e70; --pass:#3fae3f; --fail:#e66767; --xfail:#e0a100; }
body { margin:0; padding:16px; background:var(--bg); color:var(--ink);
  font:14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--ink2); margin:0 0 16px; }
table.v { border-collapse:collapse; width:100%; margin-bottom:24px;
  font-variant-numeric:tabular-nums; }
table.v th, table.v td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--grid); }
table.v th { color:var(--ink2); font-weight:600; }
.wrap { overflow-x:auto; }
.tag { font-weight:700; } .PASS { color:var(--pass); } .FAIL { color:var(--fail); }
.XFAIL, .FIXED\\? { color:var(--xfail); } .SKIPPED { color:var(--muted); }
details.run { background:var(--card); border:1px solid var(--grid); border-radius:8px;
  margin:0 0 12px; padding:8px 12px; } details.run summary { cursor:pointer; }
.charts { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:12px;
  margin-top:8px; }
.chart h3 { font-size:12px; color:var(--ink2); margin:0 0 2px; font-weight:600; }
.legend { font-size:11px; color:var(--ink2); } .legend i { display:inline-block; width:10px;
  height:2px; vertical-align:middle; margin:0 4px 0 8px; }
svg { width:100%; height:auto; display:block; } svg text { fill:var(--muted); font-size:9px; }
.msgs { font-size:12px; color:var(--ink2); max-height:160px; overflow:auto; margin:8px 0 0;
  padding-left:18px; } .defect { font-size:12px; color:var(--xfail); }
.viol { font-size:12px; color:var(--fail); }
"""

W: Final[int] = 300
H: Final[int] = 120
PAD: Final[int] = 26


def _path(
    ts: Sequence[float], ys: Sequence[float | int | None], box: tuple[float, float, float, float]
) -> str:
    """An SVG path through the points; a ``None`` lifts the pen. ``box`` is (x0, x1, y0, y1)."""
    x0, x1, y0, y1 = box
    span_x = (x1 - x0) or 1.0
    span_y = (y1 - y0) or 1.0
    parts: list[str] = []
    pen = "M"
    for t, y in zip(ts, ys, strict=True):
        if y is None:
            pen = "M"
            continue
        px = PAD + (t - x0) / span_x * (W - PAD - 4)
        py = H - 14 - (float(y) - y0) / span_y * (H - 22)
        parts.append(f"{pen}{px:.1f},{py:.1f}")
        pen = "L"
    return " ".join(parts)


def chart(
    title: str,
    ts: Sequence[float],
    lines: Sequence[tuple[str, str, Sequence[float | int | None]]],
    band: tuple[int, int] | None = None,
) -> str:
    """One small line chart as inline SVG: one y axis, recessive grid, a legend."""
    values = [float(v) for _, _, ys in lines for v in ys if v is not None]
    if band is not None:
        values += [float(band[0]), float(band[1])]
    if not ts or not values:
        return (
            f'<div class="chart"><h3>{html.escape(title)}</h3><p class="legend">no data</p></div>'
        )
    x0, x1 = ts[0], ts[-1]
    y0, y1 = min(0.0, *values), max(values) * 1.05 or 1.0
    grid = "".join(
        f'<line x1="{PAD}" x2="{W - 4}" y1="{H - 14 - f * (H - 22):.1f}" '
        f'y2="{H - 14 - f * (H - 22):.1f}" stroke="var(--grid)" stroke-width="1"/>'
        f'<text x="{PAD - 3}" y="{H - 11 - f * (H - 22):.1f}" text-anchor="end">'
        f"{y0 + f * (y1 - y0):.3g}</text>"
        for f in (0.0, 0.5, 1.0)
    )
    zone = ""
    if band is not None:
        top = H - 14 - (band[1] - y0) / ((y1 - y0) or 1.0) * (H - 22)
        bottom = H - 14 - (band[0] - y0) / ((y1 - y0) or 1.0) * (H - 22)
        zone = (
            f'<rect x="{PAD}" y="{top:.1f}" width="{W - PAD - 4}" height="{bottom - top:.1f}" '
            'fill="var(--zone)"/>'
        )
    paths = "".join(
        f'<path d="{_path(ts, ys, (x0, x1, y0, y1))}" fill="none" stroke="var({color})" '
        'stroke-width="2" stroke-linejoin="round"/>'
        for _, color, ys in lines
    )
    axis = (
        f'<text x="{PAD}" y="{H - 2}">{x0:.0f} s</text>'
        f'<text x="{W - 4}" y="{H - 2}" text-anchor="end">{x1:.0f} s</text>'
    )
    legend = "".join(
        f'<i style="background:var({color})"></i>{html.escape(label)}' for label, color, _ in lines
    )
    if band is not None:
        legend += '<i style="background:var(--zone);height:8px"></i>zone'
    return (
        f'<div class="chart"><h3>{html.escape(title)}</h3>'
        f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(title)}">'
        f"<title>{html.escape(title)}</title>{zone}{grid}{paths}{axis}</svg>"
        f'<div class="legend">{legend}</div></div>'
    )


def _run_html(report: RunReport) -> str:
    s = report.series
    charts = (
        chart(
            "output speed, rpm",
            s.t,
            [("measured", "--s1", s.output_rpm), ("setpoint", "--s2", s.setpoint_rpm)],
        )
        + chart(
            "heart rate, bpm",
            s.t,
            [("measured (usable)", "--s1", s.hr_live), ("true", "--s2", s.hr_true)],
            report.zone,
        )
        + chart(
            "centripetal g",
            s.t,
            [("at 1.5 m", "--s1", s.g_reference), ("leg tip", "--s2", s.g_leg_tip)],
        )
    )
    messages = "".join(
        f"<li>{t:.1f} s - {html.escape(text)}</li>" for t, text in report.messages[:40]
    )
    defect = (
        f'<p class="defect">{html.escape(report.known_defect)}</p>' if report.known_defect else ""
    )
    violations = "".join(f'<p class="viol">{html.escape(v)}</p>' for v in report.violations[:8])
    return (
        f'<details class="run" id="{html.escape(report.name)}"><summary>'
        f'<span class="tag {report.status}">{report.status}</span> {html.escape(report.name)} '
        f"- {html.escape(report.end)}, peak {report.peak_output_rpm:.1f} rpm, "
        f"{report.peak_g_leg_tip:.2f} g at the leg tip</summary>{defect}{violations}"
        f'<div class="charts">{charts}</div>'
        f'<ol class="msgs">{messages}</ol></details>'
    )


def render_html(reports: Sequence[RunReport], wall: float) -> str:
    rows = "".join(
        f'<tr><td class="tag {r.status}">{r.status}</td><td><a href="#{html.escape(r.name)}">'
        f"{html.escape(r.name)}</a></td><td>{r.group.value}</td><td>{html.escape(r.end)}</td>"
        f"<td>{r.peak_output_rpm:.1f}</td><td>{r.peak_g_reference:.2f}</td>"
        f"<td>{r.peak_g_leg_tip:.2f}</td><td>{_fmt(r.in_zone)}</td>"
        f"<td>{html.escape(', '.join(r.rules)) or '-'}</td><td>{len(r.violations)}</td></tr>"
        for r in reports
    )
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts(reports).items()))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Simulation Verdicts</title><style>{_CSS}</style></head><body>"
        "<h1>Anheart simulation - quick verdicts</h1>"
        f'<p class="sub">{len(reports)} runs ({summary}) in {wall:.1f} s wall. '
        "Charts: output rpm (measured and setpoint), heart rate against the zone, g at the "
        "reference radius and at the leg tip; below each, what the operator was told.</p>"
        '<div class="wrap"><table class="v"><thead><tr><th>result</th><th>run</th><th>group</th>'
        "<th>end</th><th>peak rpm</th><th>g 1.5 m</th><th>g leg tip</th><th>in zone</th>"
        f"<th>rules</th><th>violations</th></tr></thead><tbody>{rows}</tbody></table></div>"
        + "".join(_run_html(r) for r in reports)
        + "</body></html>"
    )


def write_reports(reports: Sequence[RunReport], out: Path, wall: float) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    json_path.write_text(
        json.dumps(
            {
                "wall_s": round(wall, 3),
                "counts": dict(counts(reports)),
                "runs": [r.to_json() for r in reports],
            },
            indent=2,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    html_path = out / "report.html"
    html_path.write_text(render_html(reports, wall), encoding="utf-8")
    return json_path, html_path


class _Args(argparse.Namespace):
    target: str | None = None
    cohort: bool = False
    failures: bool = False
    all: bool = False
    dsp: bool = False
    workers: int = 0
    out: Path = OUT_DIR


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulation.quick", description=__doc__)
    parser.add_argument("target", nargs="?", help="scenario, failure case or subject id")
    parser.add_argument("--cohort", action="store_true", help="30 subjects x 3 sessions")
    parser.add_argument("--failures", action="store_true", help="the failure matrix")
    parser.add_argument("--all", action="store_true", help="scenarios, failures and cohort")
    parser.add_argument("--dsp", action="store_true", help="run the real DSP where asked")
    parser.add_argument("--workers", type=int, default=0, help="processes (0: one per CPU)")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="where report.* go")
    args = parser.parse_args(argv, namespace=_Args())
    jobs: list[Job] = []
    if args.target is not None:
        jobs += jobs_for(args.target, dsp=args.dsp)
    if args.all:
        jobs += [Job(JobKind.SCENARIO, str(path), dsp=args.dsp) for path in scenario_paths()]
    if args.failures or args.all:
        jobs += failure_jobs(cases(), dsp=args.dsp)
    if args.cohort or args.all:
        jobs += cohort_jobs(load_cohort())
    if not jobs:
        parser.error("name a scenario, a failure case or a subject, or use --cohort/--failures")
    workers = args.workers or (os.cpu_count() or 1)
    began = time.perf_counter()
    reports = run_jobs(jobs, workers)
    wall = time.perf_counter() - began
    print(table(reports))  # noqa: T201  # a CLI
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts(reports).items()))
    json_path, html_path = write_reports(reports, args.out, wall)
    print(f"{len(reports)} runs: {summary} - {wall:.2f} s wall ({workers} workers)")  # noqa: T201
    print(f"report: {html_path}  data: {json_path}")  # noqa: T201
    return 0 if all(r.status in {"PASS", "XFAIL", "SKIPPED"} for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
