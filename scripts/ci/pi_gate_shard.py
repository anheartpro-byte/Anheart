"""pytest plugin: run one share of a suite and leave evidence of what ran.

Loaded only by ``pi_gate_parallel.py`` (``-p pi_gate_shard``), never by a
plain pytest run. Each process collects the WHOLE suite, exactly as the serial
gate does, then keeps the tests that belong to it and hands the rest back to
pytest as deselected. Two suites are dealt out this way, each by its own rule:
the Pi suite (the default) and the simulation battery (``--pi-gate-suite``).

Nothing here decides whether the gate passes. The plugin only writes down two
facts, which ``pi_gate_parallel.py`` checks afterwards without trusting the
selection made here:

* ``collected-<k>.txt``: every test id this process collected, in order;
* ``executed-<k>.txt``: every test this process ran to the end, with its
  verdict. It is written when the session finishes, so its presence also says
  "the tests are over, only interpreter exit is left".

The tests must run in the environment the gate was started with, not in one
the runner altered: a variable added for the runner's convenience is inherited
by every process a test starts and can change what that process does. So the
runner talks to this plugin through command-line options, and what it cannot
avoid changing to start pytest is put back before the first ``conftest.py``
is imported, so before any test module too: the two variables ``PYTHONPATH``
(to find this module) and ``COVERAGE_FILE`` (read once by pytest-cov when it
starts measuring), and the entry ``PYTHONPATH`` added to ``sys.path``.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, assert_never

import pytest

OPTION_SHARE: Final[str] = "--pi-gate-share"
"""``<index>/<count>``, zero-based: which share of the suite this process runs."""

OPTION_EVIDENCE: Final[str] = "--pi-gate-evidence"
"""Directory the two evidence files are written to."""

OPTION_RESTORE: Final[str] = "--pi-gate-restore"
"""``NAME=value`` or ``NAME``: a variable to put back as the runner found it."""

OPTION_SUITE: Final[str] = "--pi-gate-suite"
"""``pi`` (when absent) or ``simulation``: whose rule deals the tests out."""


class Suite(Enum):
    """The suites this plugin knows how to deal out."""

    PI = "pi"
    SIMULATION = "simulation"


SAME_PROCESS: Final[frozenset[str]] = frozenset(
    {
        "tests/test_web_api.py::test_the_server_serves_and_stops_without_touching_the_signal_handlers",
        "tests/test_local_panel.py::test_the_production_web_runner_binds_and_exits",
    }
)
"""Tests that must never run at the same time, so they all go to process 0.

Both bind the fixed port ``SERVE_TEST_PORT`` (8099) for real. One process runs
its tests one after the other, which is the only guarantee two processes
cannot give. A name listed here that is no longer collected stops the run:
a stale entry would silently give the guarantee up.
"""

ALONE_IN_PROCESS_ZERO: Final[frozenset[str]] = frozenset({"tests/test_cohort.py"})
"""Simulation test files that go whole to process 0, which then runs nothing else.

The first cohort test that needs an outcome runs the WHOLE cohort, over every
CPU of the machine, and the two parametrized tests then read that one result
90 times each. Dealt out like the rest, every process would run the whole
cohort again. Nothing else is given to process 0, so that it can be started
alone on a machine and leave the CPUs to the cohort. A file listed here from
which nothing is collected any more stops the run: the cost of a stale entry
(every process running the cohort) would otherwise only show as a slow gate.
"""

SLOW_SECONDS: Final[Mapping[str, int]] = {
    "tests/test_battery.py[fault_ecg_mains_burst_dsp]": 390,
    "tests/test_battery.py[fault_ecg_electrode_off_dsp]": 380,
    "tests/test_quick.py[fault_ecg_mains_burst_dsp]": 310,
    "tests/test_quick.py[fault_ecg_electrode_off_dsp]": 240,
    "tests/test_battery.py[auto_jog_150_dsp]": 220,
    "tests/test_failures.py[ecg_dsp_corrupted]": 120,
    "tests/test_properties.py::test_any_heart_rate_the_sensor_reports_keeps_every_invariant": 120,
    "tests/test_failures.py[ecg_dsp_mains]": 105,
    "tests/test_battery.py[fault_bitalino_disconnect_dsp]": 90,
    "tests/test_failures.py[ecg_dsp_stopped]": 85,
    "tests/test_properties.py::test_any_plausible_subject_keeps_every_invariant_in_a_programme": 80,
    "tests/test_failures.py[ecg_dsp_saturated]": 80,
    "tests/test_failures.py[ecg_dsp_flat]": 80,
    "tests/test_failures.py[ecg_dsp_gaps]": 55,
    (
        "tests/test_properties.py"
        "::test_every_ending_at_any_moment_of_a_manual_session_stops_the_motor"
    ): 45,
}
"""The slowest runs of the simulation battery, by ``shared_run``, in seconds.

Measured on CI (run 37539316521 of 6 October 2026, four processes on a 4-CPU
runner): the runs through the real signal processing and the property tests.
A handful of them weigh as much as everything else, so dealing the tests out
in turn left one process with fifteen minutes of work and another with three.
These numbers only steer the dealing. A name that is no longer collected is
ignored, a slow run that is not listed is dealt like any other: both show as
an unbalanced gate, in the ``--durations`` each process prints, never as a
wrong verdict. To refresh the table, read those lines.
"""

OTHER_SECONDS: Final[int] = 6
"""What a run that is not in ``SLOW_SECONDS`` is taken to cost, on average."""

_SEVERITY: Final[Mapping[str, int]] = {
    "passed": 0,
    "skipped": 1,
    "xfailed": 1,
    "xpassed": 1,
    "failed": 2,
    "error": 2,
}


def file_of(nodeid: str) -> str:
    """The file a test id names: what comes before its first ``::``."""
    return nodeid.partition("::")[0]


def shared_run(nodeid: str) -> str:
    """What the simulation tests that reuse one cached run have in common.

    The battery asks three things of each scenario, the failure matrix two of
    each case, and every one of them reads a run its file keeps for the whole
    process: the first test to ask pays for it. Those tests carry the same
    parameter id in the same file, so that is the group. A test without a
    parameter id is a group of its own.
    """
    name, bracket, rest = nodeid.partition("[")
    if bracket and rest.endswith("]"):
        return f"{file_of(name)}[{rest[:-1]}]"
    return nodeid


def simulation_owners(nodeids: Sequence[str], count: int) -> Sequence[int]:
    """The process that runs each simulation test, in collection order.

    Three rules, all of them about time only: whatever comes out of here, the
    runner still proves from the processes' own records that every test ran
    exactly once.

    * the files of ``ALONE_IN_PROCESS_ZERO`` go whole to process 0;
    * elsewhere, the tests of one ``shared_run`` go to the same process, so
      that the run they share is made once;
    * each of those groups goes to the process that has the least to do so
      far, among every process but 0 (process 0 too when it is the only one):
      first the groups of ``SLOW_SECONDS``, slowest first, then the others in
      the order they are first met. With nothing slow this is dealing them
      out in turn.
    """
    dealt_to = range(1, count) if count > 1 else range(1)
    alone = [file_of(nodeid) in ALONE_IN_PROCESS_ZERO for nodeid in nodeids]
    dealt = [nodeid for nodeid, apart in zip(nodeids, alone, strict=True) if not apart]
    met = dict.fromkeys(shared_run(nodeid) for nodeid in dealt)
    load = dict.fromkeys(dealt_to, 0)
    owner_of: dict[str, int] = {}
    # sorted() keeps the order of equal keys: the groups that are not slow stay as first met.
    for group in sorted(met, key=lambda group: -SLOW_SECONDS.get(group, 0)):
        owner_of[group] = min(dealt_to, key=lambda process: (load[process], process))
        load[owner_of[group]] += SLOW_SECONDS.get(group, OTHER_SECONDS)
    return [
        0 if apart else owner_of[shared_run(nodeid)]
        for nodeid, apart in zip(nodeids, alone, strict=True)
    ]


@dataclass(frozen=True, slots=True)
class Share:
    """Which part of the suite one process runs, and where it reports."""

    index: int
    count: int
    evidence: Path
    suite: Suite = Suite.PI

    def owns(self, position: int, nodeid: str) -> bool:
        """Whether the Pi test collected at ``position`` belongs to this process.

        Positions are dealt out in turn, so a run of slow parametrized cases is
        spread over every process instead of landing in one.
        """
        owner = 0 if nodeid in SAME_PROCESS else position % self.count
        return owner == self.index

    def selects(self, nodeids: Sequence[str]) -> Sequence[bool]:
        """For each collected test, in order, whether this process runs it."""
        match self.suite:
            case Suite.PI:
                return [self.owns(position, nodeid) for position, nodeid in enumerate(nodeids)]
            case Suite.SIMULATION:
                owners = simulation_owners(nodeids, self.count)
                return [owner == self.index for owner in owners]
            case _ as unreachable:
                assert_never(unreachable)

    def absent(self, nodeids: Sequence[str]) -> Sequence[str]:
        """What the dealing rule names that is no longer collected, sorted."""
        match self.suite:
            case Suite.PI:
                return sorted(SAME_PROCESS.difference(nodeids))
            case Suite.SIMULATION:
                files = {file_of(nodeid) for nodeid in nodeids}
                return sorted(ALONE_IN_PROCESS_ZERO.difference(files))
            case _ as unreachable:
                assert_never(unreachable)

    def collected_file(self) -> Path:
        return self.evidence / f"collected-{self.index}.txt"

    def executed_file(self) -> Path:
        return self.evidence / f"executed-{self.index}.txt"


def parse_suite(names: Sequence[str]) -> Suite:
    """The suite named on the command line: the Pi's when none is."""
    if not names:
        return Suite.PI
    known = {suite.value: suite for suite in Suite}
    if len(names) != 1 or names[0] not in known:
        raise pytest.UsageError(f"{OPTION_SUITE} must be one of {sorted(known)}, got {names}")
    return known[names[0]]


def parse_share(text: str, evidence: str, suite: Suite = Suite.PI) -> Share:
    """Read ``<index>/<count>``; anything else is a usage error, never a guess."""
    index_text, separator, count_text = text.partition("/")
    if separator != "/" or not index_text.isdecimal() or not count_text.isdecimal():
        raise pytest.UsageError(f"{OPTION_SHARE} must look like 0/4, got {text!r}")
    index, count = int(index_text), int(count_text)
    if count < 1 or index >= count:
        raise pytest.UsageError(f"{OPTION_SHARE}={text!r} names no share of the suite")
    return Share(index=index, count=count, evidence=Path(evidence), suite=suite)


def given(arguments: Sequence[str], option: str) -> Sequence[str]:
    """Every value passed for ``option``, which the runner writes ``--option=value``."""
    prefix = f"{option}="
    return [argument[len(prefix) :] for argument in arguments if argument.startswith(prefix)]


def restore_environment(assignments: Sequence[str]) -> None:
    """Put back the variables the runner had to set to start this process.

    ``NAME=value`` restores a value, ``NAME`` alone removes the variable. From
    here on the tests, and every process they start, see the environment the
    gate itself was started with, exactly as under the serial gate.
    """
    for assignment in assignments:
        name, has_value, value = assignment.partition("=")
        if has_value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)


def restore_import_path() -> None:
    """Take this module's directory back out of ``sys.path``.

    ``PYTHONPATH`` put it there so that ``-p pi_gate_shard`` could be imported.
    Now that it is, nothing else may be importable from here by a test that
    could not import it under the serial gate.
    """
    here = str(Path(__file__).resolve().parent)
    if here in sys.path:
        sys.path.remove(here)


def verdict_of(report: pytest.TestReport) -> str | None:
    """The category pytest itself would print for this report, if any."""
    expected_to_fail = hasattr(report, "wasxfail")
    if report.failed:
        return "failed" if report.when == "call" else "error"
    if report.skipped:
        return "xfailed" if expected_to_fail else "skipped"
    if report.when == "call":
        return "xpassed" if expected_to_fail else "passed"
    return None


def write_lines(path: Path, lines: Sequence[str]) -> None:
    """Write the file whole or not at all: a half-written list must not exist."""
    partial = path.with_suffix(".partial")
    partial.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    partial.replace(path)


class ShareRecorder:
    """Selects this process's tests and records what happened to each.

    Mutable on purpose: verdicts accumulate as pytest reports each phase of
    each test, and are written out once, when the session finishes.
    """

    def __init__(self, share: Share) -> None:
        self._share: Share = share
        self._verdicts: dict[str, str] = {}
        self._finished: list[str] = []

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        nodeids = [item.nodeid for item in items]
        write_lines(self._share.collected_file(), nodeids)
        absent = self._share.absent(nodeids)
        if absent:
            raise pytest.UsageError(
                "tests pinned to one process are no longer collected, update SAME_PROCESS or "
                f"ALONE_IN_PROCESS_ZERO in scripts/ci/pi_gate_shard.py: {absent}"
            )
        kept: list[pytest.Item] = []
        dropped: list[pytest.Item] = []
        for item, selected in zip(items, self._share.selects(nodeids), strict=True):
            (kept if selected else dropped).append(item)
        items[:] = kept
        config.hook.pytest_deselected(items=dropped)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        verdict = verdict_of(report)
        known = self._verdicts.get(report.nodeid)
        if verdict is not None and (known is None or _SEVERITY[verdict] > _SEVERITY[known]):
            self._verdicts[report.nodeid] = verdict
        if report.when == "teardown":
            self._finished.append(report.nodeid)

    def pytest_sessionfinish(self) -> None:
        lines = [f"{self._verdicts.get(nodeid, 'error')} {nodeid}" for nodeid in self._finished]
        write_lines(self._share.executed_file(), lines)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pi-gate", "one share of a suite (set by pi_gate_parallel.py)")
    group.addoption(OPTION_SHARE, help="<index>/<count>: the share this process runs")
    group.addoption(OPTION_SUITE, help="pi (default) or simulation: whose dealing rule applies")
    group.addoption(OPTION_EVIDENCE, help="directory for the collected and executed lists")
    group.addoption(
        OPTION_RESTORE,
        action="append",
        help="NAME=value or NAME: an environment variable to put back before the tests",
    )


def pytest_load_initial_conftests(args: list[str]) -> None:
    """Put the process back as the serial gate would have started it.

    This runs after pytest-cov has started measuring (its own implementation
    of this hook asks to go first, and it is where it reads ``COVERAGE_FILE``)
    and before pytest imports the first ``conftest.py`` (its implementation
    asks to go last). Were that order ever to change, the coverage data would
    land outside the place the runner looks for it, and the runner would fail
    the gate: "left no coverage data".
    """
    restore_environment(given(args, OPTION_RESTORE))
    restore_import_path()


def pytest_configure(config: pytest.Config) -> None:
    # Read from the command line as given, not through getoption(): that keeps
    # every value a plain string, with no untyped value to narrow.
    arguments = config.invocation_params.args
    shares = given(arguments, OPTION_SHARE)
    evidence = given(arguments, OPTION_EVIDENCE)
    if len(shares) != 1 or len(evidence) != 1:
        raise pytest.UsageError(
            f"pi_gate_shard needs one {OPTION_SHARE}= and one {OPTION_EVIDENCE}=: "
            "pi_gate_parallel.py starts it"
        )
    suite = parse_suite(given(arguments, OPTION_SUITE))
    config.pluginmanager.register(
        ShareRecorder(parse_share(shares[0], evidence[0], suite)), "pi-gate-share"
    )
