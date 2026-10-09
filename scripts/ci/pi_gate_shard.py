"""pytest plugin: run one share of a suite and leave evidence of what ran.

Loaded only by ``pi_gate_parallel.py`` (``-p pi_gate_shard``), never by a
plain pytest run. Each process collects the WHOLE suite, exactly as the serial
gate does, then keeps the tests that belong to it and hands the rest back to
pytest as deselected. Two suites are dealt out this way, each by its own rule
(``pi_owners``, ``simulation_owners``): the Pi suite (the default) and the
simulation battery (``--pi-gate-suite``).

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

Both bind a port for real. Each asks the system for a free one and then binds
it (``free_port`` in ``raspberry-pi/tests/test_web_api.py``; they once shared
the fixed port 8099): between the answer and the bind, another process may be
given the same number. One process runs its tests one after the other, which
is the only guarantee two processes cannot give. A name listed here that is no
longer collected stops the run: a stale entry would silently give the
guarantee up.
"""

PI_SLOW_SECONDS: Final[Mapping[str, Mapping[str, int]]] = {
    "tests/test_cloud_contract.py": {
        "test_nothing_else_from_a_dashboard_of_another_major_touches_a_running_session": 16,
    },
    "tests/test_cooldown_freeze_console.py": {
        "test_at_the_console_a_programme_frozen_on_its_plateau_comes_down_and_ends_on_time": 41,
    },
    "tests/test_failure_drive.py": {
        "test_comms_loss_at_every_programme_phase_goes_silent[recovery]": 37,
        "test_comms_loss_at_every_programme_phase_goes_silent[cooldown]": 31,
        "test_comms_loss_at_every_programme_phase_goes_silent[hold]": 22,
        "test_a_refused_disable_at_standstill_is_shown_to_the_operator": 11,
        "test_comms_loss_at_every_programme_phase_goes_silent[warmup]": 11,
        "test_a_drive_fault_at_speed_ends_the_session_with_its_mnemonic": 10,
        "test_a_measured_speed_that_does_not_follow_trips_tracking_error": 10,
        "test_comms_loss_in_a_manual_session_goes_silent_and_tto_stops_it[stopping]": 10,
    },
    "tests/test_failure_ecg.py": {
        "test_a_signal_the_dsp_cannot_read_ends_on_hr_stale[corrupted_garbage]": 44,
        "test_a_permanent_disconnect_at_every_phase_ends_on_hr_stale[recovery]": 34,
        "test_a_permanent_disconnect_at_every_phase_ends_on_hr_stale[cooldown]": 28,
        "test_a_permanent_disconnect_at_every_phase_ends_on_hr_stale[hold]": 19,
        "test_a_signal_the_dsp_cannot_read_ends_on_hr_stale[nan_values]": 16,
        "test_a_signal_the_dsp_cannot_read_ends_on_hr_stale[saturated]": 14,
        "test_a_signal_the_dsp_cannot_read_ends_on_hr_stale[flat_line_electrodes_off]": 13,
        "test_a_signal_the_dsp_cannot_read_ends_on_hr_stale[mains_50hz]": 13,
        "test_gapped_samples_never_yield_a_false_good_heart_rate": 12,
        "test_a_transient_disconnect_reconnects_and_the_session_carries_on": 10,
    },
    "tests/test_local_panel_e2e.py": {
        "test_manual_0_300_150_0_ramps_conform_then_stop_zeroes_the_target": 32,
        "test_a_comms_loss_goes_silent_and_nothing_resumes": 23,
    },
    "tests/test_manual_target_held_console.py": {
        "test_with_a_rider_the_console_follows_a_first_target_and_refuses_one_without_a_rate": 27,
        "test_with_a_rider_the_console_announces_a_hold_before_a_target_is_typed": 10,
    },
    "tests/test_panel_presence.py": {
        "test_an_intrusion_while_turning_stops_the_motor": 11,
    },
    "tests/test_record_endurance.py": {
        "test_ex10_a_few_sessions_show_no_accumulation_and_no_drift": 20,
    },
    "tests/test_record_session.py": {
        "test_a_drive_fault_is_an_event_with_its_mnemonic_and_its_code": 24,
    },
    "tests/test_record_tick_isolation.py": {
        "test_ex2_the_tick_takes_the_same_time_with_the_writer_active": 44,
        "test_ex3_record_reads_stuck_on_a_dead_disk_take_nothing_from_an_occupied_session": 27,
        "test_ex3_a_disk_that_never_answers_stalls_the_record_and_never_the_loop": 15,
    },
    "tests/test_runtime.py": {
        "test_a_whole_session_actually_holds_the_occupant_in_the_zone": 20,
        "test_a_whole_session_walks_the_phases_in_order_and_on_the_timeline": 20,
        "test_the_setpoint_stays_inside_its_domain_for_a_whole_session": 20,
        "test_a_phase_transition_does_not_step_the_setpoint": 13,
    },
    "tests/test_runtime_cooldown_freeze.py": {
        "test_at_every_alignment_the_descent_is_never_behind_and_never_too_fast": 44,
        (
            "test_on_the_shipped_programme_the_descent_under_a_freeze_is_never_behind_the_"
            "ordinary_one"
        ): 29,
        "test_whenever_a_freeze_is_latched_the_programme_ends_on_time_and_never_rises": 29,
        "test_the_shipped_programme_frozen_on_its_plateau_comes_down_and_ends_on_time": 20,
    },
    "tests/test_runtime_session_overrun.py": {
        "test_the_shipped_programme_then_twice_its_length_at_rest_and_a_new_start": 26,
    },
    "tests/test_runtime_standstill.py": {
        "test_a_heart_rate_drifting_above_the_zone_until_standstill_ends_the_session": 12,
        "test_the_last_step_taken_by_the_regulation_after_a_warning_ends_the_session": 11,
        "test_the_regulation_still_lowers_and_raises_the_speed_of_a_turning_arm": 10,
    },
    "tests/test_safety.py": {
        "test_isolated_outliers_never_trip_the_rate_rule": 12,
    },
    "tests/test_safety_session_overrun.py": {
        "test_a_session_that_is_over_is_not_judged_however_long_ago_it_started[Phase.BASELINE]": 11,
        "test_a_session_that_is_over_is_not_judged_however_long_ago_it_started[Phase.HOLD]": 11,
        "test_a_session_that_is_over_is_not_judged_however_long_ago_it_started[Phase.RECOVERY]": 11,
    },
    "tests/test_sensor_emg.py": {
        "test_a_clean_generator_is_graded_good_nearly_always": 11,
    },
    "tests/test_session_overrun_console.py": {
        "test_a_programme_launched_from_the_site_then_twice_its_length_at_rest_takes_a_start": 60,
    },
    "tests/test_standstill_console.py": {
        "test_after_a_standstill_the_console_restarts_nothing_and_refuses_every_start": 18,
    },
}
"""The slowest tests of the Pi, by file, in seconds: every one of ten seconds or more.

Measured on CI (run 37706142121 of 8 October 2026, the first whose tests ran as
eight shares on two runners; the seconds of the slower runner, 2.07 times
slower on the 67 cases both ran, are brought to the faster one). A name
without a parameter is every case of that test, each taken to cost that much;
a name with one is that case alone, for the tests whose cases differ: a
programme cut short at its last phase runs for six times as long as one cut
at its first.

The 52 lines measured then named 119 of the 4267 tests and two thirds of their
time; three of them named tests that were rewritten since (ANH-185) and are
gone from the table. Dealt
by their position alone, the long cases of several tests met in the same
shares, and shares 4 to 7 had a third more work than shares 0 to 3 (1568 s
against 1166 s, on one runner). The numbers only steer the dealing. A name
that is no longer collected is ignored, a slow test that is
not listed is dealt like any other: both show as an unbalanced gate, in the
``--durations`` each process prints and in the JUnit file it leaves, never as
a wrong verdict. To refresh the table, read those files (``junit-<share>.xml``
in the ``pi-evidence-*`` artifacts of a run).
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
    "tests/test_real_records.py[auto_jog_150_dsp]": 225,
    "tests/test_battery.py[auto_jog_150_dsp]": 220,
    "tests/test_real_records.py[fault_ecg_electrode_off_dsp]": 155,
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
The two ``test_real_records.py`` lines are the replays of recorded sessions,
which go through the same signal processing (run 37548774542 of 7 October).
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


def pi_seconds(nodeid: str) -> int:
    """What ``PI_SLOW_SECONDS`` takes a test of the Pi to cost; 0 when it does not list it."""
    file, _, test = nodeid.partition("::")
    listed = PI_SLOW_SECONDS.get(file, {})
    return listed.get(test, listed.get(test.partition("[")[0], 0))


def pi_owners(nodeids: Sequence[str], count: int) -> Sequence[int]:
    """The process that runs each test of the Pi, in collection order.

    Three rules, all of them about time only: whatever comes out of here, the
    runner still proves from the processes' own records that every test ran
    exactly once.

    * the tests of ``SAME_PROCESS`` go to process 0;
    * the tests ``PI_SLOW_SECONDS`` lists go, slowest first, each to the
      process that has the least of them so far, so that every process is
      handed about the same number of their seconds;
    * every other test goes by its position in the collection, in turn, as
      all of them did before that table: a run of parametrized cases is
      spread over every process instead of landing in one.
    """
    load = dict.fromkeys(range(count), 0)
    owner_of: dict[str, int] = {}
    slow = [nodeid for nodeid in nodeids if nodeid not in SAME_PROCESS and pi_seconds(nodeid)]
    # sorted() keeps the order of equal keys: tests of the same cost stay as collected.
    for nodeid in sorted(slow, key=lambda nodeid: -pi_seconds(nodeid)):
        owner_of[nodeid] = min(load, key=lambda process: (load[process], process))
        load[owner_of[nodeid]] += pi_seconds(nodeid)
    return [
        0 if nodeid in SAME_PROCESS else owner_of.get(nodeid, position % count)
        for position, nodeid in enumerate(nodeids)
    ]


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
        """Whether a Pi test dealt by its ``position`` in the collection belongs to this process.

        Positions are dealt out in turn, so a run of slow parametrized cases is
        spread over every process instead of landing in one. This is the rule
        of every test ``PI_SLOW_SECONDS`` does not list (see ``pi_owners``).
        """
        owner = 0 if nodeid in SAME_PROCESS else position % self.count
        return owner == self.index

    def selects(self, nodeids: Sequence[str]) -> Sequence[bool]:
        """For each collected test, in order, whether this process runs it."""
        match self.suite:
            case Suite.PI:
                owners = pi_owners(nodeids, self.count)
                return [owner == self.index for owner in owners]
            case Suite.SIMULATION:
                owners = simulation_owners(nodeids, self.count)
                return [owner == self.index for owner in owners]
        raise assert_never(self.suite)

    def absent(self, nodeids: Sequence[str]) -> Sequence[str]:
        """What the dealing rule names that is no longer collected, sorted."""
        match self.suite:
            case Suite.PI:
                return sorted(SAME_PROCESS.difference(nodeids))
            case Suite.SIMULATION:
                files = {file_of(nodeid) for nodeid in nodeids}
                return sorted(ALONE_IN_PROCESS_ZERO.difference(files))
        raise assert_never(self.suite)

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
