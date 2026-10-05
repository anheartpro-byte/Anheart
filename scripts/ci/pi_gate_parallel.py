"""Run the Pi test suite as several independent pytest processes, fail closed.

Started by ``raspberry-pi/scripts/check.sh`` when ``PI_GATE_PROCESSES`` is set,
from the ``raspberry-pi/`` directory. It replaces the single
``pytest --cov --cov-branch --cov-fail-under=<n>`` of the serial gate with:

1. ``<processes>`` plain ``python -m pytest`` processes, each running its own
   share of the suite (``pi_gate_shard.py``) and measuring its own coverage;
2. a check of every exit code. A process that crashes, even while the
   interpreter is shutting down after its last test, fails the gate. A process
   that finishes its tests and then does not exit is killed after
   ``EXIT_GRACE_S`` and fails the gate too;
3. a proof, from what each process wrote down, that all of them collected the
   same tests and that every collected test ran in exactly one of them;
4. ``coverage combine`` of every share, then ``coverage report
   --fail-under=<n>`` once, on the combined data.

Any of these going wrong makes the exit code 1. Nothing is retried.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

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

_OUTPUT_LOCK: Final[threading.Lock] = threading.Lock()


@dataclass(frozen=True, slots=True)
class Settings:
    processes: int
    fail_under: str
    exit_grace: float


@dataclass(frozen=True, slots=True)
class Running:
    """One pytest process and the files it is expected to leave behind."""

    index: int
    process: subprocess.Popen[str]
    pump: threading.Thread
    collected_file: Path
    executed_file: Path
    coverage_file: Path


def say(text: str) -> None:
    with _OUTPUT_LOCK:
        sys.stdout.write(f"{text}\n")
        sys.stdout.flush()


class Arguments(argparse.Namespace):
    """Typed view of the command line, so nothing parsed is ever ``Any``."""

    processes: int = 0
    fail_under: float = 0.0
    exit_grace: float = EXIT_GRACE_S


def parse_arguments(arguments: Sequence[str]) -> Settings:
    parser = argparse.ArgumentParser(description="Run the Pi tests as several pytest processes.")
    parser.add_argument("--processes", type=int, required=True)
    parser.add_argument("--fail-under", type=float, required=True)
    parser.add_argument("--exit-grace", type=float, default=EXIT_GRACE_S)
    parsed = parser.parse_args(arguments, namespace=Arguments())
    if parsed.processes < 1:
        parser.error("--processes must be at least 1")
    return Settings(
        processes=parsed.processes,
        fail_under=f"{parsed.fail_under:g}",
        exit_grace=parsed.exit_grace,
    )


def pump(index: int, stream: TextIO) -> None:
    """Copy one process's output to ours, a whole line at a time, labelled."""
    for line in stream:
        say(f"[p{index}] {line.rstrip()}")


def start(index: int, count: int, evidence: Path) -> Running:
    coverage_directory = evidence / f"coverage-{index}"
    coverage_directory.mkdir()
    inherited = os.environ.get("PYTHONPATH")
    search_path = (
        str(PLUGIN_DIRECTORY) if not inherited else f"{PLUGIN_DIRECTORY}{os.pathsep}{inherited}"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": search_path,
        "PYTHONUNBUFFERED": "1",
        "PI_GATE_SHARE": f"{index}/{count}",
        "PI_GATE_EVIDENCE": str(evidence),
        "COVERAGE_FILE": str(coverage_directory / ".coverage"),
    }
    # Each share must not judge its own, partial, coverage: the threshold is
    # applied once, to the combined data, by combined_coverage() below.
    # No cache provider: several processes would overwrite one .pytest_cache,
    # and nothing in the gate reads it (no --lf, no test uses the cache).
    command = [sys.executable, "-m", "pytest", "-p", PLUGIN, "-p", "no:cacheprovider"]
    command += ["--cov", "--cov-branch", "--cov-fail-under=0", "--cov-report="]
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
    return Running(
        index=index,
        process=process,
        pump=reader,
        collected_file=evidence / f"collected-{index}.txt",
        executed_file=evidence / f"executed-{index}.txt",
        coverage_file=coverage_directory / ".coverage",
    )


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
            code = share.process.poll()
            if code is not None:
                share.pump.join(PUMP_JOIN_S)  # its last lines first, then the verdict
                say(f"[gate] process {share.index} ended: {describe_exit(code)}")
                if code != 0:
                    problems.append(
                        f"process {share.index} did not end cleanly: {describe_exit(code)}"
                    )
                continue
            if share.executed_file.exists():
                since = tests_over_since.setdefault(share.index, time.monotonic())
                if time.monotonic() - since > exit_grace:
                    share.process.kill()
                    share.process.wait()
                    share.pump.join(PUMP_JOIN_S)
                    say(f"[gate] process {share.index} killed: still alive after its tests")
                    problems.append(
                        f"process {share.index} finished its tests but had not exited "
                        f"{exit_grace:g} s later (a thread or a native library is blocking "
                        "interpreter exit; a serial run would never return)"
                    )
                    continue
            waiting.append(share)
        pending = waiting
        if pending:
            time.sleep(POLL_S)
    return problems


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
    collected: Sequence[Sequence[str]], executed: Sequence[Sequence[str]]
) -> Sequence[str]:
    """Prove the shares are disjoint and that together they are the whole suite.

    Deliberately blind to how the shares were chosen: it only compares what
    each process collected with what the processes ran. Returns one line per
    violation, so an empty result is the proof.
    """
    problems: list[str] = []
    reference = list(collected[0])
    for index, other in enumerate(collected[1:], start=1):
        if list(other) != reference:
            problems.append(
                f"process {index} did not collect the same tests as process 0 "
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
    never = [nodeid for nodeid in reference if runs[nodeid] == 0]
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


def partition_evidence(running: Sequence[Running]) -> Sequence[str]:
    """Read what every process wrote down and check it; say what was proven."""
    problems: list[str] = []
    collected: list[Sequence[str]] = []
    executed: list[Sequence[str]] = []
    verdicts: Counter[str] = Counter()
    for share in running:
        collected_lines = read_lines(share.collected_file)
        executed_lines = read_lines(share.executed_file)
        if collected_lines is None or executed_lines is None:
            problems.append(f"process {share.index} left no record of what it collected and ran")
            continue
        collected.append(collected_lines)
        pairs = [line.partition(" ") for line in executed_lines]
        executed.append([nodeid for _, _, nodeid in pairs])
        verdicts.update(verdict for verdict, _, _ in pairs)
    if problems:
        return problems
    problems.extend(check_partition(collected, executed))
    summary = ", ".join(f"{count} {verdict}" for verdict, count in sorted(verdicts.items()))
    say(f"[gate] tests collected by each process: {' / '.join(str(len(c)) for c in collected)}")
    say(f"[gate] tests run by each process: {' + '.join(str(len(e)) for e in executed)}")
    say(f"[gate] verdicts over all processes: {summary or 'none'}")
    if not problems:
        say("[gate] partition proven: identical collection everywhere, every test ran exactly once")
    # Exit codes already carry this; the records must not contradict them.
    not_passed = sum(count for verdict, count in verdicts.items() if verdict not in ACCEPTED)
    if not_passed:
        problems.append(f"{not_passed} tests failed or errored according to the processes' records")
    return problems


def run_coverage(*arguments: str) -> int:
    command = [sys.executable, "-m", "coverage", *arguments]
    return subprocess.run(command, check=False).returncode  # noqa: S603  # fixed argv, no shell


def combined_coverage(running: Sequence[Running], fail_under: str) -> Sequence[str]:
    """Combine every share's data, then apply the threshold once to the total."""
    problems = [
        f"process {share.index} left no coverage data"
        for share in running
        if not share.coverage_file.is_file()
    ]
    present = [str(share.coverage_file) for share in running if share.coverage_file.is_file()]
    if not present:
        return [*problems, "no coverage data at all: nothing to combine"]
    sys.stdout.flush()
    if run_coverage("combine", *present) != 0:
        return [*problems, "coverage combine failed"]
    if run_coverage("report", f"--fail-under={fail_under}") != 0:
        problems.append(
            f"combined coverage is below the required {fail_under}% (or could not be reported)"
        )
    else:
        say(f"[gate] required coverage of {fail_under}% reached on the combined data")
    if run_coverage("xml", "-o", "coverage.xml") != 0:
        problems.append("coverage xml failed")
    return problems


def main(arguments: Sequence[str]) -> int:
    settings = parse_arguments(arguments)
    running: list[Running] = []
    with tempfile.TemporaryDirectory(prefix="pi-gate-") as scratch:
        evidence = Path(scratch)
        try:
            # One by one, so the finally clause can stop whatever did start.
            for index in range(settings.processes):
                running.append(start(index, settings.processes, evidence))  # noqa: PERF401
            say(f"[gate] {settings.processes} pytest processes started, one share each")
            problems = list(wait_for_exit(running, settings.exit_grace))
        finally:
            for share in running:
                if share.process.poll() is None:
                    share.process.kill()
        problems.extend(partition_evidence(running))
        problems.extend(combined_coverage(running, settings.fail_under))
    if problems:
        say(f"[gate] TESTS FAILED, {len(problems)} reason(s):")
        for problem in problems:
            say(f"[gate]   {problem}")
        return 1
    say("[gate] every process exited cleanly, every test ran once, combined coverage is sufficient")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
