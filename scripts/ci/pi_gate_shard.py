"""pytest plugin: run one share of the Pi suite and leave evidence of what ran.

Loaded only by ``pi_gate_parallel.py`` (``-p pi_gate_shard``), never by a
plain pytest run. Each process collects the WHOLE suite, exactly as the serial
gate does, then keeps the tests at the positions that belong to it and hands
the rest back to pytest as deselected.

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
from pathlib import Path
from typing import Final

import pytest

OPTION_SHARE: Final[str] = "--pi-gate-share"
"""``<index>/<count>``, zero-based: which share of the suite this process runs."""

OPTION_EVIDENCE: Final[str] = "--pi-gate-evidence"
"""Directory the two evidence files are written to."""

OPTION_RESTORE: Final[str] = "--pi-gate-restore"
"""``NAME=value`` or ``NAME``: a variable to put back as the runner found it."""

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

_SEVERITY: Final[Mapping[str, int]] = {
    "passed": 0,
    "skipped": 1,
    "xfailed": 1,
    "xpassed": 1,
    "failed": 2,
    "error": 2,
}


@dataclass(frozen=True, slots=True)
class Share:
    """Which part of the suite one process runs, and where it reports."""

    index: int
    count: int
    evidence: Path

    def owns(self, position: int, nodeid: str) -> bool:
        """Whether the test collected at ``position`` belongs to this process.

        Positions are dealt out in turn, so a run of slow parametrized cases is
        spread over every process instead of landing in one.
        """
        owner = 0 if nodeid in SAME_PROCESS else position % self.count
        return owner == self.index

    def collected_file(self) -> Path:
        return self.evidence / f"collected-{self.index}.txt"

    def executed_file(self) -> Path:
        return self.evidence / f"executed-{self.index}.txt"


def parse_share(text: str, evidence: str) -> Share:
    """Read ``<index>/<count>``; anything else is a usage error, never a guess."""
    index_text, separator, count_text = text.partition("/")
    if separator != "/" or not index_text.isdecimal() or not count_text.isdecimal():
        raise pytest.UsageError(f"{OPTION_SHARE} must look like 0/4, got {text!r}")
    index, count = int(index_text), int(count_text)
    if count < 1 or index >= count:
        raise pytest.UsageError(f"{OPTION_SHARE}={text!r} names no share of the suite")
    return Share(index=index, count=count, evidence=Path(evidence))


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
        absent = sorted(SAME_PROCESS.difference(nodeids))
        if absent:
            raise pytest.UsageError(
                "tests pinned to one process are no longer collected, update SAME_PROCESS in "
                f"scripts/ci/pi_gate_shard.py: {absent}"
            )
        kept: list[pytest.Item] = []
        dropped: list[pytest.Item] = []
        for position, item in enumerate(items):
            (kept if self._share.owns(position, item.nodeid) else dropped).append(item)
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
    group = parser.getgroup("pi-gate", "one share of the Pi suite (set by pi_gate_parallel.py)")
    group.addoption(OPTION_SHARE, help="<index>/<count>: the share this process runs")
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
    config.pluginmanager.register(
        ShareRecorder(parse_share(shares[0], evidence[0])), "pi-gate-share"
    )
