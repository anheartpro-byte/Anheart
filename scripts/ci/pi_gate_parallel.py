"""Run a test suite as several independent pytest processes, fail closed.

Started by ``raspberry-pi/scripts/check.sh`` when ``PI_GATE_PROCESSES`` is set,
from the ``raspberry-pi/`` directory, and by ``simulation/scripts/check.sh``
for the simulation battery, from ``simulation/``. It replaces the single
``pytest --cov --cov-branch --cov-fail-under=<n>`` of the serial gate with:

1. several plain ``python -m pytest`` processes, each running its own share of
   the suite (``pi_gate_shard.py``) and measuring its own coverage;
2. a check of every exit code. A process that crashes, even while the
   interpreter is shutting down after its last test, fails the gate. A process
   that finishes its tests and then does not exit is killed after
   ``EXIT_GRACE_S`` and fails the gate too;
3. a proof, from what each process wrote down, that all of them collected the
   same tests and that every collected test ran in exactly one of them;
4. a merge of the coverage data of every process, each of which must be
   present and readable, into a private file nothing else can add to, then
   ``coverage report --fail-under=<n>`` once, on that merged data.

Any of these going wrong makes the exit code 1. Nothing is retried.

The shares need not all run on one machine. Three ways to call it:

* ``--processes N``: the whole suite here, as N processes, judged here (the
  Pi gate). Steps 1 to 4.
* ``--shares FIRST-LAST/COUNT --evidence DIR``: only those shares of a suite
  cut into COUNT, here; what they wrote down and measured is left in DIR.
  Steps 1 and 2, and of 3 and 4 what one part can tell: no test ran twice
  here, no test failed, every record and every measure is there and readable.
* ``--combine COUNT --evidence DIR``: no test is run. DIR holds what the jobs
  that ran the COUNT shares left; steps 3 and 4 on all of them. Whoever calls
  this must also require that each of those jobs succeeded: step 2 is theirs.

``--report DIR`` can be added to any of the three. It leaves in DIR what the
quality report of the run reads (``scripts/ci/quality-report.mjs``): the JUnit
file of each process started here, and the coverage of the combined data as
JSON, once for the files the threshold judges and once for every file that was
measured. Nothing above reads DIR back: a file that cannot be written there is
said in the output and changes no verdict.

Two limits, stated rather than hidden. The proof in 3 compares the processes
with each other, not with a serial run: a test that every process leaves out
in the same way (a filter in ``PYTEST_ADDOPTS``, say) is not noticed here, any
more than the serial gate notices it. And the way tests are grouped depends on
the number of shares, hence on the machine or on the workflow: a test that
only fails next to certain neighbours may fail with one count and pass with
another.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import FrameType
from typing import Final, TextIO, assert_never

from coverage import CoverageData
from coverage.exceptions import CoverageException

PLUGIN: Final[str] = "pi_gate_shard"
PLUGIN_DIRECTORY: Final[Path] = Path(__file__).resolve().parent

EXIT_GRACE_S: Final[float] = 300.0
"""How long a process may take to exit once its tests are over.

The clock starts when the process has written its ``executed`` file, that is
after its last test, after its coverage data is saved. All that is left is
interpreter shutdown, which takes well under a second when nothing is stuck.
Five minutes cannot be reached by a healthy exit on any runner, and it turns a
thread left blocked into a failure with a message instead of a job that sits
until its time limit. It only ever turns a run into a failure, never into a
pass. A process stuck BEFORE the end of its tests is not timed here: like the
serial gate, the run then waits until the job's own time limit.
"""

POLL_S: Final[float] = 0.5
PUMP_JOIN_S: Final[float] = 10.0
EXAMPLES: Final[int] = 5

ACCEPTED: Final[frozenset[str]] = frozenset({"passed", "skipped", "xfailed", "xpassed"})
"""Verdicts a green pytest run can contain. Anything else fails the gate."""

SUITES: Final[tuple[str, ...]] = ("pi", "simulation")
"""The dealing rules ``pi_gate_shard.py`` knows, by the name it is given."""

_OUTPUT_LOCK: Final[threading.Lock] = threading.Lock()


class Mode(Enum):
    """What one call of this runner is asked to do."""

    WHOLE = "whole"
    """Every share here, and the verdict on all of them."""
    PART = "part"
    """Some shares here; the verdict on the whole belongs to ``COMBINE``."""
    COMBINE = "combine"
    """No test: the verdict on shares that several ``PART`` calls ran."""


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Mode
    shares: range
    """The shares started by this call (none when combining)."""
    count: int
    """How many shares the suite is cut into, over every call."""
    suite: str
    fail_under: str
    exit_grace: float
    evidence: Path | None
    """Where the records and measures are kept; ``None``: private to this call."""
    report: Path | None = None
    """Where what the quality report reads is left; ``None``: nothing is left."""


@dataclass(frozen=True, slots=True)
class Record:
    """The files one process is expected to leave behind."""

    index: int
    collected_file: Path
    executed_file: Path
    coverage_file: Path


@dataclass(frozen=True, slots=True)
class Running:
    """One pytest process, and what it is expected to leave behind."""

    record: Record
    process: subprocess.Popen[str]
    pump: threading.Thread


def say(text: str) -> None:
    with _OUTPUT_LOCK:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()


class Arguments(argparse.Namespace):
    """Typed view of the command line, so nothing parsed is ever ``Any``."""

    processes: int | None = None
    shares: str | None = None
    combine: int | None = None
    evidence: Path | None = None
    report: Path | None = None
    suite: str = SUITES[0]
    fail_under: float | None = None
    exit_grace: float = EXIT_GRACE_S


def parse_shares(text: str) -> tuple[range, int] | None:
    """Read ``FIRST-LAST/COUNT``, zero-based and inclusive; ``None`` if it is not that."""
    span, slash, count_text = text.partition("/")
    first_text, dash, last_text = span.partition("-")
    numbers = (first_text, last_text, count_text)
    if slash != "/" or dash != "-" or not all(number.isdecimal() for number in numbers):
        return None
    first, last, count = int(first_text), int(last_text), int(count_text)
    if not first <= last < count:
        return None
    return range(first, last + 1), count


def parse_arguments(arguments: Sequence[str]) -> Settings:
    parser = argparse.ArgumentParser(description="Run a suite as several pytest processes.")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--processes", type=int, help="the whole suite here, as this many processes")
    what.add_argument("--shares", help="FIRST-LAST/COUNT: only these shares here (see --evidence)")
    what.add_argument("--combine", type=int, help="judge this many shares that other calls ran")
    parser.add_argument("--evidence", type=Path, help="where --shares leaves, --combine reads")
    parser.add_argument("--report", type=Path, help="where to leave what the quality report reads")
    parser.add_argument("--suite", choices=SUITES, default=SUITES[0], help="whose dealing rule")
    parser.add_argument("--fail-under", type=float, help="required combined coverage, percent")
    parser.add_argument("--exit-grace", type=float, default=EXIT_GRACE_S)
    parsed = parser.parse_args(arguments, namespace=Arguments())
    judged_here = parsed.shares is None
    if judged_here and parsed.fail_under is None:
        parser.error("--fail-under is required with --processes and with --combine")
    if (parsed.processes is None) != (parsed.evidence is not None):
        parser.error("--evidence goes with --shares and --combine, and only with them")
    if parsed.processes is not None:
        mode, shares, count = Mode.WHOLE, range(max(parsed.processes, 0)), parsed.processes
    elif parsed.combine is not None:
        mode, shares, count = Mode.COMBINE, range(0), parsed.combine
    else:
        cut = parse_shares(parsed.shares or "")
        if cut is None:
            parser.error(f"--shares must look like 4-7/13, got {parsed.shares!r}")
        mode, (shares, count) = Mode.PART, cut
    if count < 1:
        parser.error("--processes and --combine must be at least 1")
    return Settings(
        mode=mode,
        shares=shares,
        count=count,
        suite=parsed.suite,
        fail_under="0" if parsed.fail_under is None else f"{parsed.fail_under:g}",
        exit_grace=parsed.exit_grace,
        evidence=parsed.evidence,
        report=parsed.report,
    )


def record_of(index: int, evidence: Path) -> Record:
    """Where share ``index`` writes, and where it is read back from."""
    return Record(
        index=index,
        collected_file=evidence / f"collected-{index}.txt",
        executed_file=evidence / f"executed-{index}.txt",
        coverage_file=evidence / f"coverage-{index}" / ".coverage",
    )


def forget(record: Record) -> None:
    """Remove what an earlier call left for this share.

    A directory that is kept can be used twice. Whatever a process fails to
    write must then be missing, not still there from the time before.
    """
    record.collected_file.unlink(missing_ok=True)
    record.executed_file.unlink(missing_ok=True)
    shutil.rmtree(record.coverage_file.parent, ignore_errors=True)


def report_directory(settings: Settings) -> Path | None:
    """The directory given with ``--report``, created; ``None`` when nothing is to be left.

    What goes there feeds the quality report of the run and nothing else: no
    verdict of this runner reads it back. So a directory that cannot be created,
    or that cannot be written to, is said and then done without, never counted
    as a problem. Being there is not enough: a pytest told to write its JUnit
    file where it cannot ends with an error, which would fail the gate.
    """
    if settings.report is None:
        return None
    try:
        settings.report.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.report):
            pass
    except OSError as error:
        say(f"[gate] quality report: nothing is left in {settings.report} ({error})")
        return None
    return settings.report


def cleared(target: Path) -> bool:
    """Remove what an earlier call left at ``target``; say whether the place is free.

    Each call writes the files of the quality report afresh, so that a file
    that is there is one this call wrote. What cannot be removed (a directory
    standing in its place) is left alone and said: nothing of the report may
    stop the gate.
    """
    try:
        target.unlink(missing_ok=True)
    except OSError as error:
        say(f"[gate] quality report: {target.name} cannot be replaced ({error})")
        return False
    return True


def pump(index: int, stream: TextIO) -> None:
    """Copy one process's output to ours, a whole line at a time, labelled."""
    for line in stream:
        say(f"[p{index}] {line.rstrip()}")


def start(
    index: int, count: int, evidence: Path, suite: str = SUITES[0], report: Path | None = None
) -> Running:
    """Start one pytest process on its share of the suite.

    With ``report``, the process also writes its JUnit file there, for the
    quality report of the run: pytest's own account of each test and of the
    time it took. No verdict reads it.

    The tests must see the environment this runner was started with, as they
    do under the serial gate: whatever is added here is inherited by every
    process a test starts. (``PYTHONUNBUFFERED``, once set here to get the
    output sooner, made a worker started by a test write ``OPEN`` and its
    newline in two system calls, and the test that read them failed.) So the
    plugin is told its share on the command line, and the two variables pytest
    cannot be started without are handed to it, with the value they had here,
    to be put back before the first ``conftest.py`` is imported.
    """
    record = record_of(index, evidence)
    forget(record)
    record.coverage_file.parent.mkdir()
    inherited = os.environ.get("PYTHONPATH")
    needed_to_start = {
        # For ``-p pi_gate_shard`` to be importable.
        "PYTHONPATH": (
            str(PLUGIN_DIRECTORY) if not inherited else f"{PLUGIN_DIRECTORY}{os.pathsep}{inherited}"
        ),
        # Read once by pytest-cov when it starts measuring: this process's own file.
        "COVERAGE_FILE": str(record.coverage_file),
    }
    put_back = [
        f"--pi-gate-restore={name}={os.environ[name]}"
        if name in os.environ
        else f"--pi-gate-restore={name}"
        for name in needed_to_start
    ]
    # No cache provider: several processes would overwrite one .pytest_cache,
    # and nothing in the gate reads it (no --lf, no test uses the cache).
    command = [sys.executable, "-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider"]
    command += [f"--pi-gate-share={index}/{count}", f"--pi-gate-evidence={evidence}"]
    command += [f"--pi-gate-suite={suite}", *put_back]
    # Each share must not judge its own, partial, coverage: the threshold is
    # applied once, to the combined data, by combined_coverage() below.
    command += ["--cov", "--cov-branch", "--cov-fail-under=0", "--cov-report="]
    if report is not None:
        junit = report / f"junit-{index}.xml"
        # Like forget() above: a process that dies before writing its file
        # must leave none, not the one of an earlier call. Where that file
        # cannot be removed, this process is not asked for one.
        if cleared(junit):
            command += [f"--junitxml={junit}"]
    environment = {**os.environ, **needed_to_start}
    process = subprocess.Popen(  # noqa: S603  # fixed argv, no shell
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.stdout is None:  # cannot happen with stdout=PIPE; refuse rather than assume
        process.kill()
        raise RuntimeError("pytest was started without an output pipe")
    reader = threading.Thread(target=pump, args=(index, process.stdout), daemon=True)
    reader.start()
    return Running(record=record, process=process, pump=reader)


def describe_exit(code: int) -> str:
    if code >= 0:
        return f"exit code {code}"
    try:
        return f"killed by {signal.Signals(-code).name}"
    except ValueError:
        return f"killed by signal {-code}"


def wait_for_exit(running: Sequence[Running], exit_grace: float) -> Sequence[str]:
    """Wait for every process to end by itself; report each one that did not.

    The wall clock is read here on purpose: this is CI tooling timing real
    processes, outside the controller code that must take an injected clock.
    """
    problems: list[str] = []
    tests_over_since: dict[int, float] = {}
    pending = list(running)
    while pending:
        waiting: list[Running] = []
        for share in pending:
            index = share.record.index
            code = share.process.poll()
            if code is not None:
                share.pump.join(PUMP_JOIN_S)  # its last lines first, then the verdict
                say(f"[gate] process {index} ended: {describe_exit(code)}")
                if code != 0:
                    problems.append(f"process {index} did not end cleanly: {describe_exit(code)}")
                continue
            if share.record.executed_file.exists():
                since = tests_over_since.setdefault(index, time.monotonic())
                if time.monotonic() - since > exit_grace:
                    share.process.kill()
                    share.process.wait()
                    share.pump.join(PUMP_JOIN_S)
                    say(f"[gate] process {index} killed: still alive after its tests")
                    problems.append(
                        f"process {index} finished its tests but had not exited "
                        f"{exit_grace:g} s later (a thread or a native library is blocking "
                        "interpreter exit; a serial run would never return)"
                    )
                    continue
            waiting.append(share)
        pending = waiting
        if pending:
            time.sleep(POLL_S)
    return problems


def run_shares(settings: Settings, evidence: Path) -> tuple[Sequence[Record], Sequence[str]]:
    """Start the shares of this call, wait for them all, stop what is left."""
    running: list[Running] = []
    report = report_directory(settings)
    try:
        # One by one, so the finally clause can stop whatever did start.
        for index in settings.shares:
            running.append(start(index, settings.count, evidence, settings.suite, report))  # noqa: PERF401
        say(f"[gate] {len(running)} pytest processes started, one share each")
        problems = wait_for_exit(running, settings.exit_grace)
    finally:
        # Interrupted or not, nothing started here may outlive this run.
        for share in running:
            if share.process.poll() is None:
                share.process.kill()
                share.process.wait()
    return [share.record for share in running], problems


def read_lines(path: Path) -> Sequence[str] | None:
    if not path.is_file():
        return None
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines[-1] == "":  # the newline that ends the last line, or an empty file
        lines.pop()
    return lines


def examples(nodeids: Sequence[str]) -> str:
    shown = ", ".join(nodeids[:EXAMPLES])
    return shown if len(nodeids) <= EXAMPLES else f"{shown}, and {len(nodeids) - EXAMPLES} more"


def first_difference(reference: Sequence[str], other: Sequence[str]) -> str:
    """Where two collections part ways, for the failure message."""
    for position, (expected, found) in enumerate(zip(reference, other, strict=False)):
        if expected != found:
            return f"first difference at position {position}: {found!r} against {expected!r}"
    return f"one stops after {min(len(reference), len(other))} tests, the other goes on"


def check_partition(
    collected: Sequence[Sequence[str]],
    executed: Sequence[Sequence[str]],
    *,
    first: int = 0,
    whole: bool = True,
) -> Sequence[str]:
    """Prove the shares are disjoint and that together they are the whole suite.

    Deliberately blind to how the shares were chosen: it only compares what
    each process collected with what the processes ran. Returns one line per
    violation, so an empty result is the proof. ``first`` is the number of the
    first process, for the messages. With ``whole`` false the lists are those
    of some of the shares only: a test that ran in none of them is then
    another's, and is not counted against these.
    """
    problems: list[str] = []
    reference = list(collected[0])
    for index, other in enumerate(collected[1:], start=first + 1):
        if list(other) != reference:
            problems.append(
                f"process {index} did not collect the same tests as process {first} "
                f"({len(other)} against {len(reference)}; {first_difference(reference, other)}): "
                "test ids must be identical in every process"
            )
    twice_collected = [nodeid for nodeid, times in Counter(reference).items() if times > 1]
    if twice_collected:
        problems.append(
            f"{len(twice_collected)} test ids are not unique: {examples(twice_collected)}"
        )
    runs = Counter(nodeid for share in executed for nodeid in share)
    known = set(reference)
    never = [nodeid for nodeid in reference if runs[nodeid] == 0] if whole else []
    repeated = [nodeid for nodeid, times in runs.items() if times > 1]
    unknown = [nodeid for nodeid in runs if nodeid not in known]
    if never:
        problems.append(f"{len(never)} collected tests ran in no process: {examples(never)}")
    if repeated:
        problems.append(f"{len(repeated)} tests ran more than once: {examples(repeated)}")
    if unknown:
        problems.append(
            f"{len(unknown)} tests ran without having been collected: {examples(unknown)}"
        )
    return problems


def partition_evidence(records: Sequence[Record], *, whole: bool = True) -> Sequence[str]:
    """Read what every process wrote down and check it; say what was proven.

    ``whole`` false: ``records`` are some of the shares only (see ``check_partition``).
    """
    problems: list[str] = []
    collected: list[Sequence[str]] = []
    executed: list[Sequence[str]] = []
    verdicts: Counter[str] = Counter()
    for record in records:
        collected_lines = read_lines(record.collected_file)
        executed_lines = read_lines(record.executed_file)
        if collected_lines is None or executed_lines is None:
            problems.append(f"process {record.index} left no record of what it collected and ran")
            continue
        collected.append(collected_lines)
        pairs = [line.partition(" ") for line in executed_lines]
        executed.append([nodeid for _, _, nodeid in pairs])
        verdicts.update(verdict for verdict, _, _ in pairs)
    if problems:
        return problems
    problems.extend(check_partition(collected, executed, first=records[0].index, whole=whole))
    summary = ", ".join(f"{count} {verdict}" for verdict, count in sorted(verdicts.items()))
    say(f"[gate] tests collected by each process: {' / '.join(str(len(c)) for c in collected)}")
    say(f"[gate] tests run by each process: {' + '.join(str(len(e)) for e in executed)}")
    say(f"[gate] verdicts over all processes: {summary or 'none'}")
    if not problems and whole:
        say("[gate] partition proven: identical collection everywhere, every test ran exactly once")
    if not problems and not whole:
        say("[gate] identical collection in these processes, no test ran twice among them")
    # Exit codes already carry this; the records must not contradict them.
    not_passed = sum(count for verdict, count in verdicts.items() if verdict not in ACCEPTED)
    if not_passed:
        problems.append(f"{not_passed} tests failed or errored according to the processes' records")
    return problems


def merge_coverage(
    coverage_files: Sequence[Path], merged_file: Path, *, first: int = 0
) -> Sequence[str]:
    """Merge the coverage data of every process into one private file.

    ``coverage_files`` holds one path per process, in process order, starting
    with process ``first``. Done here, file by file, rather than by
    ``coverage combine``: exactly the files of THIS run are read, and a file
    that is missing, unreadable or empty is an error, where
    ``coverage combine`` only warns and exits 0.
    ``CoverageData.update`` is the call ``coverage combine`` makes for each
    file it reads. No path mapping is needed: every process ran in a checkout
    of the same directory, where coverage is told to record relative names,
    so a source file has the same name in every share.
    """
    problems: list[str] = []
    merged = CoverageData(basename=str(merged_file))
    used = 0
    for index, coverage_file in enumerate(coverage_files, start=first):
        if not coverage_file.is_file():
            problems.append(f"process {index} left no coverage data")
            continue
        try:
            part = CoverageData(basename=str(coverage_file))
            part.read()
            measured = len(part.measured_files())
            merged.update(part)
        except CoverageException as error:
            problems.append(f"process {index} left unusable coverage data: {error}")
            continue
        if measured == 0:
            problems.append(f"process {index} left coverage data that measured nothing")
            continue
        used += 1
    say(f"[gate] coverage data merged from {used} of {len(coverage_files)} processes")
    return problems


def run_coverage(merged_file: Path, *arguments: str) -> int:
    """Run one coverage report command on the merged data, and on nothing else.

    ``coverage report`` and ``coverage xml`` first combine any ``.coverage.*``
    file lying next to their data file. The data file is therefore a private
    one, alone in a directory this run created: a stale file left in the
    working directory can no longer be counted in the total.
    """
    command = [sys.executable, "-m", "coverage", *arguments, f"--data-file={merged_file}"]
    environment = {**os.environ, "COVERAGE_FILE": str(merged_file)}
    sys.stdout.flush()
    return subprocess.run(command, env=environment, check=False).returncode  # noqa: S603  # fixed argv


def report_coverage(merged_file: Path, report: Path) -> None:
    """Leave the combined coverage as JSON for the quality report, twice.

    ``coverage-gate.json`` holds what the threshold is applied to: the files of
    the project's ``include`` list. ``coverage-all.json`` holds every file that
    was measured (``--include=*`` replaces that list for this one report),
    which no threshold judges. Neither can change the verdict: the threshold
    is applied by the caller, and a report that cannot be written is said, not
    counted as a problem.
    """
    for name, scope in (("coverage-gate.json", ()), ("coverage-all.json", ("--include=*",))):
        target = report / name
        written = cleared(target) and (
            run_coverage(merged_file, "json", "--fail-under=0", *scope, "-o", str(target)) == 0
        )
        if not written:
            say(f"[gate] quality report: {name} could not be written")


def combined_coverage(
    records: Sequence[Record], fail_under: str, private: Path, report: Path | None = None
) -> Sequence[str]:
    """Merge every share's data, then apply the threshold once to the total."""
    private.mkdir()
    merged_file = private / ".coverage"
    problems = list(merge_coverage([record.coverage_file for record in records], merged_file))
    if run_coverage(merged_file, "report", f"--fail-under={fail_under}") != 0:
        problems.append(
            f"combined coverage is below the required {fail_under}% (or could not be reported)"
        )
    else:
        say(f"[gate] required coverage of {fail_under}% reached on the combined data")
    # The threshold was applied just above; here only a failure to write counts.
    if run_coverage(merged_file, "xml", "--fail-under=0", "-o", "coverage.xml") != 0:
        problems.append("coverage.xml could not be written")
    if report is not None:
        report_coverage(merged_file, report)
    return problems


def usable_coverage(records: Sequence[Record], private: Path) -> Sequence[str]:
    """Check that every share run here left coverage data the combining call can read.

    The data is merged into a file that is thrown away: no threshold can be
    applied to part of the suite. This only makes a share whose measure is
    missing fail in the job that ran it, where its output is.
    """
    private.mkdir()
    files = [record.coverage_file for record in records]
    return merge_coverage(files, private / ".coverage", first=records[0].index)


def stop_on_sigterm(signum: int, _frame: FrameType | None) -> None:
    """Make SIGTERM unwind like Ctrl-C, so the processes started here are stopped."""
    raise SystemExit(128 + signum)


def kept_evidence(settings: Settings) -> Path:
    """The directory given with ``--shares`` or ``--combine``."""
    if settings.evidence is None:  # parse_arguments refuses it; said again for the type checkers
        raise RuntimeError("--evidence is required with --shares and --combine")
    return settings.evidence


def judge_whole(settings: Settings, private: Path) -> tuple[Sequence[str], str]:
    """Every share here: exit codes, partition, coverage threshold."""
    records, exits = run_shares(settings, private)
    problems = [*exits, *partition_evidence(records)]
    problems += combined_coverage(
        records, settings.fail_under, private / "combined", report_directory(settings)
    )
    passed = "every process exited cleanly, every test ran once, combined coverage is sufficient"
    return problems, passed


def judge_part(settings: Settings, private: Path) -> tuple[Sequence[str], str]:
    """Some shares here: exit codes, and what one part can tell of the rest."""
    evidence = kept_evidence(settings)
    evidence.mkdir(parents=True, exist_ok=True)
    records, exits = run_shares(settings, evidence)
    problems = [*exits, *partition_evidence(records, whole=False)]
    problems += usable_coverage(records, private / "readable")
    passed = (
        f"processes {settings.shares[0]} to {settings.shares[-1]} of {settings.count} exited "
        "cleanly and left their records; whether every test ran once, and the coverage, are "
        "judged where every share is gathered (--combine)"
    )
    return problems, passed


def judge_combined(settings: Settings, private: Path) -> tuple[Sequence[str], str]:
    """No test: partition and coverage threshold over what other calls left."""
    evidence = kept_evidence(settings)
    records = [record_of(index, evidence) for index in range(settings.count)]
    problems = [*partition_evidence(records)]
    problems += combined_coverage(
        records, settings.fail_under, private / "combined", report_directory(settings)
    )
    passed = (
        f"every test ran once over the {settings.count} processes gathered, combined coverage "
        "is sufficient (their exit codes were judged where they ran)"
    )
    return problems, passed


def judge(settings: Settings, private: Path) -> tuple[Sequence[str], str]:
    """Do what this call was asked: the problems found, and what a pass means."""
    match settings.mode:
        case Mode.WHOLE:
            return judge_whole(settings, private)
        case Mode.PART:
            return judge_part(settings, private)
        case Mode.COMBINE:
            return judge_combined(settings, private)
        case _ as unreachable:
            assert_never(unreachable)


def main(arguments: Sequence[str]) -> int:
    settings = parse_arguments(arguments)
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    with tempfile.TemporaryDirectory(prefix="pi-gate-") as scratch:
        problems, passed = judge(settings, Path(scratch))
    if problems:
        say(f"[gate] TESTS FAILED, {len(problems)} reason(s):")
        for problem in problems:
            say(f"[gate]   {problem}")
        return 1
    say(f"[gate] {passed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
