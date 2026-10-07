"""The parallel runner is part of the gate, so it is tested like the gate.

Run by ``raspberry-pi/scripts/check.sh`` before it trusts the runner with the
real suite. Three kinds of test:

* the partition proof, the dealing of tests and the merge of coverage data, as
  plain functions;
* the whole runner against a throwaway project, once green and then once for
  each way a run must NOT be able to pass: a failing test, a hole in the
  combined coverage (with and without stale data lying around), a process
  that dies while the interpreter shuts down, a process that never exits, a
  process that leaves no record or no usable coverage data, and test ids that
  differ between processes;
* the environment and the import path the tests run with, which must be those
  of the serial gate;
* the runner being told to stop;
* the simulation battery's own dealing rule (tests that share one cached run
  stay together, the cohort has a process to itself), as plain functions and
  against a throwaway battery;
* a suite whose shares are run by several calls, as the CI jobs of the
  simulation gate do (``--shares`` then ``--combine``): the green case, then
  once for each way the calls together must NOT be able to pass;
* what ``--report`` leaves for the quality report of the run, and that it
  changes no verdict: a red run stays red, a report that cannot be written
  leaves a green run green.

Several of the throwaway scenarios are built so that ONE check is all that
stands between them and a pass: process 0 alone covers the whole of the
project, so a run where process 1 loses its coverage data or its record still
has complete coverage and passing tests.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import pytest
from coverage import CoverageData
from pi_gate_parallel import (
    Mode,
    check_partition,
    merge_coverage,
    parse_arguments,
    parse_shares,
    read_lines,
    report_coverage,
)
from pi_gate_shard import (
    ALONE_IN_PROCESS_ZERO,
    OTHER_SECONDS,
    SAME_PROCESS,
    SLOW_SECONDS,
    Share,
    Suite,
    given,
    parse_share,
    parse_suite,
    restore_environment,
    restore_import_path,
    shared_run,
    simulation_owners,
)

RUNNER: Final[Path] = Path(__file__).with_name("pi_gate_parallel.py")
RUN_TIMEOUT_S: Final[float] = 300.0
STARTUP_TIMEOUT_S: Final[float] = 120.0

PROJECT: Final[Mapping[str, str]] = {
    # fail_under is set like in the real project: a share judged on its own,
    # partial, coverage would fail here exactly as it would there.
    "pyproject.toml": (
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-v"\n\n'
        '[tool.coverage.run]\nbranch = true\nsource = ["src"]\nrelative_files = true\n\n'
        "[tool.coverage.report]\nshow_missing = true\nfail_under = 100\n"
    ),
    "src/__init__.py": "",
    "src/lib.py": (
        "def sign(value: int) -> int:\n    if value < 0:\n        return -1\n    return 1\n"
    ),
    "tests/__init__.py": "",
    # With two processes, process 0 runs test_negative and test_zero, which
    # together cover every line and branch; process 1 runs the other two.
    "tests/test_lib.py": (
        "from src.lib import sign\n\n\n"
        "def test_negative() -> None:\n    assert sign(-3) == -1\n\n\n"
        "def test_positive() -> None:\n    assert sign(3) == 1\n\n\n"
        "def test_zero() -> None:\n    assert sign(0) == 1\n\n\n"
        "def test_large() -> None:\n    assert sign(10**9) == 1\n"
    ),
    # The tests the real suite pins to one process, under the same ids.
    **{
        nodeid.partition("::")[0]: f"def {nodeid.partition('::')[2]}() -> None:\n    pass\n"
        for nodeid in SAME_PROCESS
    },
}

WHICH_PROCESS: Final[str] = (
    "import os\nimport sys\nfrom pathlib import Path\n\n\n"
    "def option(name: str) -> str:\n"
    '    given = [a.partition("=")[2] for a in sys.argv if a.startswith(f"--pi-gate-{name}=")]\n'
    '    return given[0] if given else ""\n\n\n'
    'SHARE = option("share")\n'
    'IN_PROCESS_ONE = SHARE.startswith("1/")\n'
    'COVERAGE_DATA = Path(option("evidence"), "coverage-1", ".coverage")\n\n'
)
"""The start of a throwaway conftest: which process it is in, read from the
command line, since the runner leaves nothing about itself in the environment."""

ONLY_IN_PROCESS_ONE: Final[str] = WHICH_PROCESS + "if IN_PROCESS_ONE:\n"

RECORD_SURROUNDINGS: Final[str] = (
    "import json\n" + WHICH_PROCESS + "\n"
    "def surroundings() -> dict[str, object]:\n"
    '    return {"environment": dict(os.environ), "import path": list(sys.path)}\n\n\n'
    "AT_IMPORT = surroundings()\n\n\n"
    "def pytest_collection_finish() -> None:\n"
    '    seen = {"at conftest import": AT_IMPORT, "before the tests": surroundings()}\n'
    '    name = SHARE.partition("/")[0] or "serial"\n'
    '    Path(f"surroundings-{name}.json").write_text(json.dumps(seen))\n'
)
"""A conftest that writes down what code sees around it: the environment and
the import path, when the conftest is imported and when the tests are about to
start."""

SLOW_TESTS: Final[str] = (
    "import os\nimport time\nfrom pathlib import Path\n\n\n"
    "def announce_then_wait() -> None:\n"
    '    Path("pids").mkdir(exist_ok=True)\n'
    '    Path("pids", str(os.getpid())).touch()\n'
    "    time.sleep(600)\n\n\n"
    "def test_slow_one() -> None:\n    announce_then_wait()\n\n\n"
    "def test_slow_two() -> None:\n    announce_then_wait()\n"
)
"""Two tests, one for each process, that say who they are and then stay."""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for name, content in PROJECT.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def clean_environment() -> Mapping[str, str]:
    """Our environment without what would leak this run into the throwaway one."""
    inherited = ("PYTEST_ADDOPTS", "PYTHONPATH", "COVERAGE_FILE")
    return {name: value for name, value in os.environ.items() if name not in inherited}


def gate_command(exit_grace: float) -> Sequence[str]:
    command = [sys.executable, str(RUNNER), "--processes", "2", "--fail-under", "100"]
    return [*command, "--exit-grace", str(exit_grace)]


def run_gate(
    project: Path, *, exit_grace: float = 60.0, environment: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real runner, two processes, against the throwaway project."""
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        gate_command(exit_grace),
        cwd=project,
        env=clean_environment() if environment is None else environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )


def misbehave_in_process_one(project: Path, hook: str, body: str, *, first: bool = False) -> None:
    """Write a conftest whose one hook misbehaves, in process 1 only."""
    decorator = "@pytest.hookimpl(tryfirst=True)\n" if first else ""
    conftest = (
        WHICH_PROCESS
        + "import pytest\n\n\n"
        + f"{decorator}def {hook}() -> None:\n    if IN_PROCESS_ONE:\n        {body}\n"
    )
    (project / "tests/conftest.py").write_text(conftest, encoding="utf-8")


def remove_the_only_test_of_the_negative_branch(project: Path) -> None:
    tests = project / "tests/test_lib.py"
    kept = tests.read_text(encoding="utf-8").replace(
        "def test_negative() -> None:\n    assert sign(-3) == -1\n", ""
    )
    tests.write_text(kept, encoding="utf-8")


def eventually(condition: Callable[[], bool], within: float) -> bool:
    """Poll in real time: these tests watch real processes come and go."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# --- The partition proof ---------------------------------------------------


def test_a_complete_disjoint_run_is_accepted() -> None:
    collected = [["a", "b", "c", "d"], ["a", "b", "c", "d"]]
    assert check_partition(collected, [["a", "c"], ["d", "b"]]) == []


def test_a_test_that_ran_nowhere_is_reported() -> None:
    collected = [["a", "b", "c"], ["a", "b", "c"]]
    problems = check_partition(collected, [["a"], ["b"]])
    assert problems == ["1 collected tests ran in no process: c"]


def test_a_test_that_ran_twice_is_reported() -> None:
    collected = [["a", "b"], ["a", "b"]]
    problems = check_partition(collected, [["a", "b"], ["b"]])
    assert problems == ["1 tests ran more than once: b"]


def test_a_test_nobody_collected_is_reported() -> None:
    collected = [["a", "b"], ["a", "b"]]
    problems = check_partition(collected, [["a"], ["b", "z"]])
    assert problems == ["1 tests ran without having been collected: z"]


def test_processes_that_collected_different_ids_are_reported() -> None:
    """Every test ran once here; only the ids disagree, and that alone must fail."""
    collected = [["a", "b at 0x1"], ["a", "b at 0x2"]]
    problems = check_partition(collected, [["a"], ["b at 0x1"]])
    assert len(problems) == 1
    assert "did not collect the same tests" in problems[0]
    assert "position 1" in problems[0]


def test_a_shorter_collection_is_reported() -> None:
    problems = check_partition([["a", "b"], ["a"]], [["a"], ["b"]])
    assert len(problems) == 1
    assert "1 against 2" in problems[0]


def test_an_id_collected_twice_is_reported() -> None:
    collected = [["a", "a"], ["a", "a"]]
    problems = check_partition(collected, [["a"], []])
    assert problems == ["1 test ids are not unique: a"]


def test_a_record_is_read_line_by_line_and_an_absent_one_is_not_empty(tmp_path: Path) -> None:
    """No record and an empty record are different facts: the first fails the gate."""
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    (tmp_path / "two.txt").write_text("passed a\nskipped b\n", encoding="utf-8")
    assert read_lines(tmp_path / "absent.txt") is None
    assert read_lines(tmp_path / "empty.txt") == []
    assert read_lines(tmp_path / "two.txt") == ["passed a", "skipped b"]


# --- Dealing the tests out -------------------------------------------------


@pytest.mark.parametrize("count", [1, 2, 3, 4, 7])
def test_every_position_belongs_to_exactly_one_process(count: int, tmp_path: Path) -> None:
    shares = [Share(index=index, count=count, evidence=tmp_path) for index in range(count)]
    for position in range(50):
        owners = [
            share.index for share in shares if share.owns(position, f"tests/t.py::t{position}")
        ]
        assert len(owners) == 1


def test_pinned_tests_all_go_to_process_zero_whatever_their_position(tmp_path: Path) -> None:
    shares = [Share(index=index, count=4, evidence=tmp_path) for index in range(4)]
    for position in range(8):
        for nodeid in SAME_PROCESS:
            assert [share.index for share in shares if share.owns(position, nodeid)] == [0]


@pytest.mark.parametrize("text", ["", "4", "a/b", "4/4", "5/4", "0/0", "-1/4", "1/4/2"])
def test_a_share_that_names_nothing_is_refused(text: str, tmp_path: Path) -> None:
    with pytest.raises(pytest.UsageError):
        parse_share(text, str(tmp_path))


# --- Merging the coverage data ---------------------------------------------


def coverage_file(path: Path, arcs: Mapping[str, Collection[tuple[int, int]]]) -> Path:
    data = CoverageData(basename=str(path))
    data.add_arcs(arcs)
    data.write()
    return path


def test_the_coverage_of_every_process_ends_up_in_the_merged_data(tmp_path: Path) -> None:
    first = coverage_file(tmp_path / "first", {"src/lib.py": [(-1, 1), (1, 2)]})
    second = coverage_file(tmp_path / "second", {"src/lib.py": [(1, 3)], "src/more.py": [(-1, 1)]})
    assert merge_coverage([first, second], tmp_path / "merged") == []
    merged = CoverageData(basename=str(tmp_path / "merged"))
    merged.read()
    assert merged.measured_files() == {"src/lib.py", "src/more.py"}
    assert set(merged.arcs("src/lib.py") or ()) == {(-1, 1), (1, 2), (1, 3)}


def test_a_process_without_coverage_data_is_an_error(tmp_path: Path) -> None:
    first = coverage_file(tmp_path / "first", {"src/lib.py": [(-1, 1)]})
    problems = merge_coverage([first, tmp_path / "absent"], tmp_path / "merged")
    assert problems == ["process 1 left no coverage data"]


def test_coverage_data_that_cannot_be_read_is_an_error(tmp_path: Path) -> None:
    """``coverage combine`` would warn, count the file as errored, and exit 0."""
    first = coverage_file(tmp_path / "first", {"src/lib.py": [(-1, 1)]})
    (tmp_path / "second").write_bytes(b"this is not a coverage database")
    problems = merge_coverage([first, tmp_path / "second"], tmp_path / "merged")
    assert len(problems) == 1
    assert problems[0].startswith("process 1 left unusable coverage data: ")


def test_coverage_data_that_measured_nothing_is_an_error(tmp_path: Path) -> None:
    first = coverage_file(tmp_path / "first", {"src/lib.py": [(-1, 1)]})
    second = coverage_file(tmp_path / "second", {})
    problems = merge_coverage([first, second], tmp_path / "merged")
    assert problems == ["process 1 left coverage data that measured nothing"]


# --- The whole runner, against a throwaway project -------------------------


def test_a_green_project_passes_and_pinned_tests_share_a_process(project: Path) -> None:
    result = run_gate(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] tests collected by each process: 6 / 6" in result.stdout
    assert "[gate] tests run by each process: 4 + 2" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] verdicts over all processes: 6 passed" in result.stdout
    assert "[gate] coverage data merged from 2 of 2 processes" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert (project / "coverage.xml").is_file()
    for pinned in SAME_PROCESS:
        ran_in = {line[:4] for line in result.stdout.splitlines() if f"{pinned} PASSED" in line}
        assert ran_in == {"[p0]"}


def test_a_failing_test_fails_the_gate(project: Path) -> None:
    with (project / "tests/test_lib.py").open("a", encoding="utf-8") as tests:
        tests.write("\n\ndef test_broken() -> None:\n    assert sign(1) == -1\n")
    result = run_gate(project)
    assert result.returncode == 1
    assert "did not end cleanly: exit code 1" in result.stdout
    assert "1 tests failed or errored according to the processes' records" in result.stdout
    assert "[gate] partition proven" in result.stdout


def test_a_hole_in_the_combined_coverage_fails_the_gate(project: Path) -> None:
    remove_the_only_test_of_the_negative_branch(project)
    result = run_gate(project)
    assert result.returncode == 1
    assert "[gate] verdicts over all processes: 5 passed" in result.stdout
    assert "combined coverage is below the required 100%" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout
    assert (project / "coverage.xml").is_file()


def test_stale_coverage_data_in_the_directory_cannot_fill_a_hole(project: Path) -> None:
    """Complete data from an earlier run lies next to where coverage would look.

    ``coverage report`` combines every ``.coverage.*`` file it finds beside its
    data file before it reports. Reporting from the working directory would
    count this stale file and call the hole below covered.
    """
    earlier = subprocess.run(
        [sys.executable, "-m", "pytest", "--cov", "--cov-branch", "-p", "no:cacheprovider"],
        cwd=project,
        env=clean_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )
    assert earlier.returncode == 0, earlier.stdout + earlier.stderr
    stale = project / ".coverage.stalehost.99999.Xstale"
    (project / ".coverage").rename(stale)
    remove_the_only_test_of_the_negative_branch(project)

    result = run_gate(project)

    assert result.returncode == 1, result.stdout
    assert "combined coverage is below the required 100%" in result.stdout
    assert "[gate] coverage data merged from 2 of 2 processes" in result.stdout
    assert "Combined" not in result.stdout, "something other than this run's data was read"
    assert stale.is_file(), "the runner has no business touching the working directory"


def test_a_process_killed_while_the_interpreter_shuts_down_fails_the_gate(project: Path) -> None:
    """Every test passes and coverage is complete: only the exit status tells."""
    (project / "tests/conftest.py").write_text(
        "import atexit\nimport signal\n"
        + ONLY_IN_PROCESS_ONE
        + "    atexit.register(os.kill, os.getpid(), signal.SIGKILL)\n",
        encoding="utf-8",
    )
    result = run_gate(project)
    assert result.returncode == 1
    assert "process 1 did not end cleanly: killed by SIGKILL" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_process_that_never_exits_is_killed_and_fails_the_gate(project: Path) -> None:
    """A thread left blocked: tests pass, coverage is complete, the process stays."""
    (project / "tests/conftest.py").write_text(
        "import threading\n"
        + ONLY_IN_PROCESS_ONE
        + "    threading.Thread(target=threading.Event().wait).start()\n",
        encoding="utf-8",
    )
    result = run_gate(project, exit_grace=1.0)
    assert result.returncode == 1
    assert "process 1 finished its tests but had not exited 1 s later" in result.stdout
    assert "[gate] process 0 ended: exit code 0" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout
    summaries = [line for line in result.stdout.splitlines() if line.startswith("[p1] ==")]
    assert any("2 passed" in line for line in summaries), "the stuck process's summary was lost"


def test_a_process_that_leaves_no_record_fails_the_gate(project: Path) -> None:
    """It ran its tests, saved its coverage and exited 0, but never said what it ran."""
    misbehave_in_process_one(project, "pytest_sessionfinish", "os._exit(0)", first=True)
    result = run_gate(project)
    assert result.returncode == 1
    assert "[gate] process 1 ended: exit code 0" in result.stdout
    assert "process 1 left no record of what it collected and ran" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_process_whose_coverage_data_is_gone_fails_the_gate(project: Path) -> None:
    """Process 0 covers everything on its own, so the threshold alone would pass."""
    misbehave_in_process_one(project, "pytest_unconfigure", "COVERAGE_DATA.unlink()")
    result = run_gate(project)
    assert result.returncode == 1
    assert "process 1 left no coverage data" in result.stdout
    assert "[gate] coverage data merged from 1 of 2 processes" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_process_whose_coverage_data_is_unreadable_fails_the_gate(project: Path) -> None:
    """Same as above, with a file that is there but is not coverage data."""
    misbehave_in_process_one(project, "pytest_unconfigure", 'COVERAGE_DATA.write_bytes(b"junk")')
    result = run_gate(project)
    assert result.returncode == 1
    assert "process 1 left unusable coverage data" in result.stdout
    assert "[gate] coverage data merged from 1 of 2 processes" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_ids_that_differ_between_processes_fail_the_gate(project: Path) -> None:
    (project / "tests/test_ids.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("build", [lambda: 1, lambda: 2], ids=str)\n'
        "def test_built(build) -> None:\n    assert build()\n",
        encoding="utf-8",
    )
    result = run_gate(project)
    assert result.returncode == 1
    assert "process 1 did not collect the same tests as process 0" in result.stdout
    assert "[gate] partition proven" not in result.stdout


def test_a_pinned_test_that_disappeared_fails_the_gate(project: Path) -> None:
    (project / "tests/test_local_panel.py").unlink()
    result = run_gate(project)
    assert result.returncode == 1
    assert "no longer collected" in result.stdout


# --- The environment the tests run in --------------------------------------


def recorded(path: Path) -> object:
    """What one process wrote down. Untyped JSON, kept as ``object``: only compared."""
    surroundings: object = json.loads(path.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    return surroundings


@pytest.mark.parametrize("already_set", [False, True], ids=["variables-unset", "variables-set"])
def test_the_tests_run_in_the_surroundings_of_the_serial_gate(
    project: Path, tmp_path_factory: pytest.TempPathFactory, *, already_set: bool
) -> None:
    """Whatever the runner changes around the tests, their children inherit.

    A variable set for the runner's convenience once made a worker started by a
    test write its ready line in two system calls instead of one, and the test
    reading it failed. So the claim is exact: in every process, from the first
    conftest on, the environment and the import path are the ones a plain
    serial pytest gets, entry for entry. It is checked both when the runner
    has to add its two variables and when it has to replace values of ours.
    """
    (project / "tests/conftest.py").write_text(RECORD_SURROUNDINGS, encoding="utf-8")
    environment = dict(clean_environment())
    if already_set:
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        environment["PYTHONPATH"] = str(elsewhere)
        environment["COVERAGE_FILE"] = str(elsewhere / "coverage = of ours")
    serial = subprocess.run(
        [sys.executable, "-m", "pytest", "--cov", "--cov-branch", "-p", "no:cacheprovider"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )
    assert serial.returncode == 0, serial.stdout + serial.stderr

    result = run_gate(project, environment=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    expected = recorded(project / "surroundings-serial.json")
    assert recorded(project / "surroundings-0.json") == expected
    assert recorded(project / "surroundings-1.json") == expected


def test_variables_are_put_back_and_only_those(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WAS_SET", "by the runner")
    monkeypatch.setenv("WAS_ADDED", "by the runner")
    monkeypatch.setenv("UNTOUCHED", "stays")
    restore_environment(["WAS_SET=before=with an equals sign", "WAS_ADDED", "NEVER_THERE"])
    assert os.environ["WAS_SET"] == "before=with an equals sign"
    assert "WAS_ADDED" not in os.environ
    assert "NEVER_THERE" not in os.environ
    assert os.environ["UNTOUCHED"] == "stays"


def test_the_plugin_directory_leaves_the_import_path_once(monkeypatch: pytest.MonkeyPatch) -> None:
    here = str(Path(__file__).resolve().parent)
    monkeypatch.setattr(sys, "path", ["first", here, "between", here, "last"])
    restore_import_path()
    assert sys.path == ["first", "between", here, "last"]
    monkeypatch.setattr(sys, "path", ["first", "last"])
    restore_import_path()
    assert sys.path == ["first", "last"]


def test_only_the_options_asked_for_are_read() -> None:
    arguments = ["-p", "x", "--pi-gate-restore=A=1", "--pi-gate-share=0/2", "--pi-gate-restore=B"]
    assert given(arguments, "--pi-gate-restore") == ["A=1", "B"]
    assert given(arguments, "--pi-gate-share") == ["0/2"]
    assert given(arguments, "--pi-gate-evidence") == []


# --- Being told to stop ----------------------------------------------------


def test_sigterm_stops_the_runner_and_every_process_it_started(project: Path) -> None:
    """A terminated runner must not leave its pytest processes running behind it."""
    (project / "tests/test_slow.py").write_text(SLOW_TESTS, encoding="utf-8")
    announced = project / "pids"
    runner = subprocess.Popen(  # noqa: S603  # fixed argv, no shell
        gate_command(60.0),
        cwd=project,
        env=clean_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    started: list[int] = []
    try:
        both_waiting = eventually(
            lambda: announced.is_dir() and len(list(announced.iterdir())) == 2, STARTUP_TIMEOUT_S
        )
        started = [int(entry.name) for entry in announced.iterdir()] if announced.is_dir() else []
        assert both_waiting, f"only {started} reached their slow test"

        runner.send_signal(signal.SIGTERM)

        assert runner.wait(timeout=60) == 128 + signal.SIGTERM
        assert eventually(lambda: not any(is_alive(pid) for pid in started), 10.0)
    finally:
        runner.kill()
        runner.wait()
        for pid in started:
            if is_alive(pid):
                os.kill(pid, signal.SIGKILL)


# --- The simulation battery's dealing rule ---------------------------------

COHORT: Final[str] = next(iter(ALONE_IN_PROCESS_ZERO))

SIMULATION_IDS: Final[Sequence[str]] = (
    "tests/test_battery.py::test_invariants[jog]",
    "tests/test_battery.py::test_invariants[bench]",
    "tests/test_battery.py::test_invariants[estop]",
    "tests/test_battery.py::test_motor_stopped[jog]",
    "tests/test_battery.py::test_motor_stopped[bench]",
    "tests/test_battery.py::test_motor_stopped[estop]",
    "tests/test_battery.py::test_the_battery_is_complete",
    f"{COHORT}::test_the_cohort_file_is_current",
    f"{COHORT}::test_invariants[S01-jog]",
    f"{COHORT}::test_motor_stopped[S01-jog]",
    "tests/test_failures.py::test_handled[jog]",
    "tests/test_failures.py::test_motor_stopped[jog]",
    "tests/test_units.py::test_plain",
)
"""Shaped like the real battery: runs shared inside a file, and the cohort."""


def test_simulation_tests_that_share_a_run_go_to_the_same_process() -> None:
    owners = dict(zip(SIMULATION_IDS, simulation_owners(SIMULATION_IDS, 4), strict=True))
    battery = "tests/test_battery.py"
    # Groups in the order they are first met, dealt in turn to processes 1, 2, 3.
    assert owners[f"{battery}::test_invariants[jog]"] == 1
    assert owners[f"{battery}::test_invariants[bench]"] == 2
    assert owners[f"{battery}::test_invariants[estop]"] == 3
    assert owners[f"{battery}::test_the_battery_is_complete"] == 1
    assert owners["tests/test_failures.py::test_handled[jog]"] == 2
    assert owners["tests/test_units.py::test_plain"] == 3
    # The second question about a run goes where the first one made it.
    for run in ("jog", "bench", "estop"):
        asked_first = owners[f"{battery}::test_invariants[{run}]"]
        assert owners[f"{battery}::test_motor_stopped[{run}]"] == asked_first
    handled = owners["tests/test_failures.py::test_handled[jog]"]
    assert owners["tests/test_failures.py::test_motor_stopped[jog]"] == handled


def test_the_cohort_has_process_zero_to_itself() -> None:
    owners = simulation_owners(SIMULATION_IDS, 4)
    for nodeid, owner in zip(SIMULATION_IDS, owners, strict=True):
        assert (owner == 0) == nodeid.startswith(COHORT), nodeid


def a_test_of(group: str) -> str:
    """A test id whose ``shared_run`` is ``group``, as the table of slow runs names it."""
    file, bracket, parameter = group.partition("[")
    return f"{file}::test_it[{parameter}" if bracket else group


def test_the_slowest_simulation_runs_are_dealt_first_and_weighed() -> None:
    """Collected last, after sixty light runs: they still open the dealing, slowest first."""
    slowest_first = sorted(SLOW_SECONDS, key=lambda group: -SLOW_SECONDS[group])
    for group in slowest_first:
        assert shared_run(a_test_of(group)) == group, "not the name of a group of tests"
        assert SLOW_SECONDS[group] > OTHER_SECONDS
    light = [f"tests/test_failures.py::test_handled[light{n}]" for n in range(60)]
    nodeids = [*light, *(a_test_of(group) for group in slowest_first)]

    owners = dict(zip(nodeids, simulation_owners(nodeids, 4), strict=True))

    assert [owners[a_test_of(group)] for group in slowest_first[:3]] == [1, 2, 3]
    work: dict[int, int] = {1: 0, 2: 0, 3: 0}
    runs: dict[int, int] = {1: 0, 2: 0, 3: 0}
    for nodeid, owner in owners.items():
        work[owner] += SLOW_SECONDS.get(shared_run(nodeid), OTHER_SECONDS)
        runs[owner] += 1
    # The light runs fill what the slow ones left uneven, to within one of them.
    assert max(work.values()) - min(work.values()) <= OTHER_SECONDS
    # Dealt in turn, as before this table, every process would have had 25 runs.
    assert max(runs.values()) > min(runs.values())


@pytest.mark.parametrize("count", [1, 2, 3, 4, 13])
def test_every_simulation_test_has_one_process_whatever_their_number(count: int) -> None:
    nodeids = [
        f"tests/test_m{m}.py::test_t{t}[{p}]" for m in range(3) for t in range(4) for p in "abcde"
    ]
    nodeids += [f"{COHORT}::test_c{c}" for c in range(7)]
    owners = simulation_owners(nodeids, count)
    assert len(owners) == len(nodeids)
    assert set(owners) <= set(range(count))
    groups: dict[str, set[int]] = {}
    for nodeid, owner in zip(nodeids, owners, strict=True):
        groups.setdefault(shared_run(nodeid), set()).add(owner)
    dealt = [next(iter(where)) for group, where in groups.items() if not group.startswith(COHORT)]
    assert all(len(where) == 1 for group, where in groups.items() if not group.startswith(COHORT))
    if count == 1:
        assert set(owners) == {0}
    else:
        # In turn: no process is given two more runs than another.
        runs_of = [dealt.count(process) for process in range(1, count)]
        assert 0 not in dealt
        assert max(runs_of) - min(runs_of) <= 1


def test_the_run_two_tests_share_is_read_from_their_ids() -> None:
    assert shared_run("tests/t.py::test_x[a-b]") == "tests/t.py[a-b]"
    assert shared_run("tests/t.py::test_y[a-b]") == "tests/t.py[a-b]"
    assert shared_run("tests/t.py::TestK::test_x[a-b]") == "tests/t.py[a-b]"
    assert shared_run("tests/u.py::test_x[a-b]") == "tests/u.py[a-b]"
    assert shared_run("tests/t.py::test_x[p[0]]") == "tests/t.py[p[0]]"
    assert shared_run("tests/t.py::test_x") == "tests/t.py::test_x"


def test_the_suite_is_the_pi_one_unless_named() -> None:
    assert parse_suite([]) is Suite.PI
    assert parse_suite(["pi"]) is Suite.PI
    assert parse_suite(["simulation"]) is Suite.SIMULATION
    for refused in (["battery"], ["pi", "simulation"], [""]):
        with pytest.raises(pytest.UsageError):
            parse_suite(refused)


def test_each_suite_names_what_it_no_longer_collects(tmp_path: Path) -> None:
    pi = Share(index=0, count=2, evidence=tmp_path)
    simulation = Share(index=0, count=2, evidence=tmp_path, suite=Suite.SIMULATION)
    assert pi.absent(sorted(SAME_PROCESS)) == []
    assert pi.absent(SIMULATION_IDS) == sorted(SAME_PROCESS)
    assert simulation.absent(SIMULATION_IDS) == []
    assert simulation.absent(sorted(SAME_PROCESS)) == sorted(ALONE_IN_PROCESS_ZERO)


BATTERY: Final[Mapping[str, str]] = {
    "pyproject.toml": PROJECT["pyproject.toml"],
    "src/__init__.py": "",
    "src/lib.py": PROJECT["src/lib.py"],
    "tests/__init__.py": "",
    # Three questions about each of five runs. A run is made by the first test
    # of a process to ask for it, and leaves a file that names the process.
    "tests/test_battery.py": (
        "import os\nfrom pathlib import Path\n\nimport pytest\n\n"
        "RUNS: dict[str, int] = {}\n"
        'NAMES = ["a", "b", "c", "d", "e"]\n\n\n'
        "def run(name: str) -> int:\n"
        "    if name not in RUNS:\n"
        '        Path(f"made-{name}-by-{os.getpid()}").touch()\n'
        "        RUNS[name] = len(name)\n"
        "    return RUNS[name]\n\n\n"
        '@pytest.mark.parametrize("name", NAMES)\n'
        "def test_first(name: str) -> None:\n    assert run(name) == 1\n\n\n"
        '@pytest.mark.parametrize("name", NAMES)\n'
        "def test_second(name: str) -> None:\n    assert run(name) == 1\n\n\n"
        '@pytest.mark.parametrize("name", NAMES)\n'
        "def test_third(name: str) -> None:\n    assert run(name) == 1\n\n\n"
        "def test_alone() -> None:\n    assert NAMES\n"
    ),
    # The file the simulation rule sends whole to process 0.
    COHORT: (
        "from src.lib import sign\n\n\n"
        "def test_negative() -> None:\n    assert sign(-3) == -1\n\n\n"
        "def test_positive() -> None:\n    assert sign(3) == 1\n"
    ),
}


@pytest.fixture
def battery(tmp_path: Path) -> Path:
    for name, content in BATTERY.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def run_runner(
    directory: Path, *arguments: str, exit_grace: float = 60.0
) -> subprocess.CompletedProcess[str]:
    """Run the real runner as asked, in a throwaway project."""
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        [sys.executable, str(RUNNER), *arguments, "--exit-grace", str(exit_grace)],
        cwd=directory,
        env=clean_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )


def test_a_battery_makes_each_shared_run_once_and_keeps_the_cohort_apart(battery: Path) -> None:
    result = run_runner(battery, "--processes", "3", "--suite", "simulation", "--fail-under", "100")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] tests collected by each process: 18 / 18 / 18" in result.stdout
    assert "[gate] tests run by each process: 2 + 9 + 7" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    for name in "abcde":
        made = list(battery.glob(f"made-{name}-by-*"))
        assert len(made) == 1, f"run {name} was made {len(made)} times: one per process that asked"
    ran_cohort = {line[:4] for line in result.stdout.splitlines() if f"{COHORT}::" in line}
    assert ran_cohort == {"[p0]"}
    passed_in_zero = [line for line in result.stdout.splitlines() if line.startswith("[p0] tests/")]
    assert len(passed_in_zero) == 2, "process 0 ran something besides the cohort"


def test_the_same_battery_dealt_by_position_makes_its_runs_again(battery: Path) -> None:
    """What the rule above is for: the Pi rule would make most runs three times."""
    for nodeid in SAME_PROCESS:
        name, _, function = nodeid.partition("::")
        (battery / name).write_text(f"def {function}() -> None:\n    pass\n", encoding="utf-8")
    result = run_runner(battery, "--processes", "3", "--fail-under", "100")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(list(battery.glob("made-*"))) > 5


def test_a_battery_without_its_cohort_file_fails_the_gate(battery: Path) -> None:
    (battery / COHORT).unlink()
    result = run_runner(battery, "--processes", "3", "--suite", "simulation", "--fail-under", "100")
    assert result.returncode == 1
    assert "no longer collected" in result.stdout
    assert "ALONE_IN_PROCESS_ZERO" in result.stdout


# --- Reading the command line ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "shares", "count"),
    [("0-0/1", range(1), 1), ("5-8/13", range(5, 9), 13), ("0-12/13", range(13), 13)],
)
def test_a_span_of_shares_is_read(text: str, shares: range, count: int) -> None:
    assert parse_shares(text) == (shares, count)


@pytest.mark.parametrize(
    "text",
    ["", "4", "4/13", "4-/13", "-4/13", "5-4/13", "5-13/13", "a-b/c", "1-2/3/4", "1-2", "0-0/0"],
)
def test_a_span_that_names_no_share_is_refused(text: str) -> None:
    assert parse_shares(text) is None


def test_each_way_to_call_the_runner_is_told_apart(tmp_path: Path) -> None:
    directory = str(tmp_path)
    whole = parse_arguments(["--processes", "4", "--fail-under", "100"])
    assert (whole.mode, whole.shares, whole.count) == (Mode.WHOLE, range(4), 4)
    assert (whole.suite, whole.evidence, whole.fail_under) == ("pi", None, "100")
    assert whole.report is None
    reported = parse_arguments(["--processes", "4", "--fail-under", "100", "--report", directory])
    assert (reported.mode, reported.report) == (Mode.WHOLE, tmp_path)
    part = parse_arguments(["--shares", "5-8/13", "--evidence", directory, "--suite", "simulation"])
    assert (part.mode, part.shares, part.count) == (Mode.PART, range(5, 9), 13)
    assert (part.suite, part.evidence, part.report) == ("simulation", tmp_path, None)
    combine = parse_arguments(["--combine", "13", "--evidence", directory, "--fail-under", "99.5"])
    assert (combine.mode, combine.shares, combine.count) == (Mode.COMBINE, range(0), 13)
    assert (combine.evidence, combine.fail_under) == (tmp_path, "99.5")


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--processes", "4"],
        ["--processes", "0", "--fail-under", "100"],
        ["--processes", "4", "--fail-under", "100", "--evidence", "kept"],
        ["--processes", "2", "--shares", "0-1/2", "--evidence", "kept", "--fail-under", "100"],
        ["--shares", "5-8/13"],
        ["--shares", "9-8/13", "--evidence", "kept"],
        ["--shares", "5-8/13", "--evidence", "kept", "--suite", "battery"],
        ["--combine", "13", "--evidence", "kept"],
        ["--combine", "13", "--fail-under", "100"],
        ["--combine", "0", "--evidence", "kept", "--fail-under", "100"],
    ],
)
def test_a_call_that_does_not_say_enough_is_refused(arguments: Sequence[str]) -> None:
    with pytest.raises(SystemExit):
        parse_arguments(arguments)


# --- One suite, its shares run by several calls ----------------------------


def test_some_of_the_shares_are_not_blamed_for_the_tests_of_the_others() -> None:
    collected = [["a", "b", "c", "d"], ["a", "b", "c", "d"]]
    assert check_partition(collected, [["a"], ["b"]], first=5, whole=False) == []
    twice = check_partition(collected, [["a"], ["a"]], first=5, whole=False)
    assert twice == ["1 tests ran more than once: a"]
    differing = check_partition([["a", "b"], ["a", "x"]], [["a"], []], first=5, whole=False)
    assert len(differing) == 1
    assert "process 6 did not collect the same tests as process 5" in differing[0]


def test_a_missing_measure_is_named_after_its_own_process(tmp_path: Path) -> None:
    present = coverage_file(tmp_path / "present", {"src/lib.py": [(-1, 1)]})
    problems = merge_coverage([present, tmp_path / "absent"], tmp_path / "merged", first=5)
    assert problems == ["process 6 left no coverage data"]


@pytest.fixture
def kept(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory for the evidence, outside the project and not yet there."""
    return tmp_path_factory.mktemp("evidence") / "kept" / "here"


def run_shares(
    project: Path, shares: str, kept: Path, *, exit_grace: float = 60.0
) -> subprocess.CompletedProcess[str]:
    return run_runner(project, "--shares", shares, "--evidence", str(kept), exit_grace=exit_grace)


def combine(project: Path, kept: Path, count: int = 2) -> subprocess.CompletedProcess[str]:
    arguments = ["--combine", str(count), "--evidence", str(kept), "--fail-under", "100"]
    return run_runner(project, *arguments)


@pytest.mark.parametrize(
    "calls",
    [["0-0/2", "1-1/2"], ["1-1/2", "0-0/2"], ["0-1/2"], ["0-0/3", "1-2/3"]],
    ids=["one-share-each", "in-any-order", "all-in-one-call", "one-then-two"],
)
def test_shares_run_by_separate_calls_are_proven_together(
    project: Path, kept: Path, calls: Sequence[str]
) -> None:
    for shares in calls:
        part = run_shares(project, shares, kept)
        assert part.returncode == 0, part.stdout + part.stderr
        assert "exited cleanly and left their records" in part.stdout
        # No call that ran part of the suite may speak for the whole of it.
        assert "[gate] partition proven" not in part.stdout
        assert "required coverage" not in part.stdout
    assert not (project / "coverage.xml").exists()

    count = int(calls[0].rpartition("/")[2])
    result = combine(project, kept, count)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] partition proven" in result.stdout
    assert "[gate] verdicts over all processes: 6 passed" in result.stdout
    assert f"[gate] coverage data merged from {count} of {count} processes" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert (project / "coverage.xml").is_file()


def test_a_share_no_call_ran_fails_the_combined_gate(project: Path, kept: Path) -> None:
    """Process 0 covers everything and passes: only the missing record tells."""
    assert run_shares(project, "0-0/2", kept).returncode == 0
    result = combine(project, kept)
    assert result.returncode == 1
    assert "process 1 left no record of what it collected and ran" in result.stdout
    assert "process 1 left no coverage data" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] partition proven" not in result.stdout


def test_shares_cut_differently_by_two_calls_fail_the_combined_gate(
    project: Path, kept: Path
) -> None:
    """One call cut the suite in three, the other in two: a test twice, another never.

    Both calls pass, every test that ran passed and coverage is complete. The
    proof does not know how either call chose its tests, and does not need to.
    """
    assert run_shares(project, "0-0/3", kept).returncode == 0
    assert run_shares(project, "1-1/2", kept).returncode == 0
    result = combine(project, kept)
    assert result.returncode == 1
    assert "1 collected tests ran in no process: tests/test_lib.py::test_zero" in result.stdout
    assert "1 tests ran more than once: tests/test_lib.py::test_large" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert "[gate] TESTS FAILED, 2 reason(s):" in result.stdout


def test_a_failing_test_fails_its_own_call_and_the_combined_gate(project: Path, kept: Path) -> None:
    with (project / "tests/test_lib.py").open("a", encoding="utf-8") as tests:
        tests.write("\n\ndef test_broken() -> None:\n    assert sign(1) == -1\n")
    ran_it = run_shares(project, "0-0/2", kept)
    assert ran_it.returncode == 1
    assert "process 0 did not end cleanly: exit code 1" in ran_it.stdout
    assert "1 tests failed or errored according to the processes' records" in ran_it.stdout
    assert run_shares(project, "1-1/2", kept).returncode == 0
    # Should whoever gathers the shares forget to ask how each call ended.
    result = combine(project, kept)
    assert result.returncode == 1
    assert "[gate] partition proven" in result.stdout
    assert "1 tests failed or errored according to the processes' records" in result.stdout


def test_a_process_killed_at_shutdown_fails_the_call_that_ran_it(project: Path, kept: Path) -> None:
    (project / "tests/conftest.py").write_text(
        "import atexit\nimport signal\n"
        + ONLY_IN_PROCESS_ONE
        + "    atexit.register(os.kill, os.getpid(), signal.SIGKILL)\n",
        encoding="utf-8",
    )
    result = run_shares(project, "1-1/2", kept)
    assert result.returncode == 1
    assert "process 1 did not end cleanly: killed by SIGKILL" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_process_that_never_exits_fails_the_call_that_ran_it(project: Path, kept: Path) -> None:
    (project / "tests/conftest.py").write_text(
        "import threading\n"
        + ONLY_IN_PROCESS_ONE
        + "    threading.Thread(target=threading.Event().wait).start()\n",
        encoding="utf-8",
    )
    result = run_shares(project, "1-1/2", kept, exit_grace=1.0)
    assert result.returncode == 1
    assert "process 1 finished its tests but had not exited 1 s later" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_hole_in_the_coverage_shows_once_every_share_is_gathered(
    project: Path, kept: Path
) -> None:
    remove_the_only_test_of_the_negative_branch(project)
    assert run_shares(project, "0-0/2", kept).returncode == 0
    assert run_shares(project, "1-1/2", kept).returncode == 0
    result = combine(project, kept)
    assert result.returncode == 1
    assert "[gate] partition proven" in result.stdout
    assert "combined coverage is below the required 100%" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_a_share_without_its_measure_fails_the_call_that_ran_it(project: Path, kept: Path) -> None:
    misbehave_in_process_one(project, "pytest_unconfigure", "COVERAGE_DATA.unlink()")
    result = run_shares(project, "1-1/2", kept)
    assert result.returncode == 1
    assert "process 1 left no coverage data" in result.stdout
    assert "[gate] TESTS FAILED, 1 reason(s):" in result.stdout


def test_what_an_earlier_call_left_for_a_share_is_not_judged_again(
    project: Path, kept: Path
) -> None:
    """The directory is kept, so it can be used twice: the second time must not read the first."""
    assert run_shares(project, "0-1/2", kept).returncode == 0
    assert (kept / "executed-1.txt").is_file()
    misbehave_in_process_one(project, "pytest_sessionfinish", "os._exit(0)", first=True)

    again = run_shares(project, "1-1/2", kept)

    assert again.returncode == 1
    assert "[gate] process 1 ended: exit code 0" in again.stdout
    assert "process 1 left no record of what it collected and ran" in again.stdout
    assert not (kept / "executed-1.txt").exists()
    assert combine(project, kept).returncode == 1


# --- What is left for the quality report of the run ------------------------

OUTSIDE_THE_THRESHOLD: Final[Mapping[str, str]] = {
    # The threshold judges src/lib.py alone, as the real project judges its
    # safety chain alone; src/extra.py is measured and has a branch no test takes.
    "pyproject.toml": PROJECT["pyproject.toml"] + 'include = ["src/lib.py"]\n',
    "src/extra.py": (
        "def clamp(value: int) -> int:\n    if value > 9:\n        return 9\n    return value\n"
    ),
    "tests/test_extra.py": (
        "from src.extra import clamp\n\n\ndef test_small() -> None:\n    assert clamp(3) == 3\n"
    ),
}


WHOLE: Final[Sequence[str]] = ("--processes", "2", "--fail-under", "100")
"""The whole throwaway suite in one call, as the Pi gate runs the real one."""


def json_object(value: object) -> Mapping[str, object]:
    """A JSON object read back, its values still to be told apart."""
    assert isinstance(value, dict)
    return cast("Mapping[str, object]", value)


def coverage_report(path: Path) -> tuple[Collection[str], Mapping[str, object]]:
    """The files a coverage JSON report names, and its totals."""
    report = json_object(recorded(path))
    return set(json_object(report["files"])), json_object(report["totals"])


def junit_counts(path: Path) -> tuple[int, int]:
    """How many tests, and how many failures, pytest's own JUnit file states."""
    suite = re.search(
        r'<testsuite [^>]*failures="(\d+)"[^>]* tests="(\d+)"', path.read_text("utf-8")
    )
    assert suite is not None, path
    return int(suite.group(2)), int(suite.group(1))


def test_a_report_holds_each_process_and_both_coverages(project: Path, kept: Path) -> None:
    for name, content in OUTSIDE_THE_THRESHOLD.items():
        (project / name).write_text(content, encoding="utf-8")

    result = run_runner(project, *WHOLE, "--report", str(kept))

    # The hole is outside what the threshold judges: the verdict is that of a run with no report.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    counts = [junit_counts(kept / f"junit-{index}.xml") for index in range(2)]
    assert sum(tests for tests, _ in counts) == 7
    assert [failures for _, failures in counts] == [0, 0]
    judged, judged_totals = coverage_report(kept / "coverage-gate.json")
    assert judged == {"src/lib.py"}
    assert judged_totals["missing_lines"] == 0
    assert judged_totals["missing_branches"] == 0
    measured, measured_totals = coverage_report(kept / "coverage-all.json")
    assert {"src/lib.py", "src/extra.py"} <= set(measured)
    assert measured_totals["missing_lines"] == 1
    assert measured_totals["missing_branches"] == 1


def test_a_report_does_not_make_a_red_run_green(project: Path, kept: Path) -> None:
    remove_the_only_test_of_the_negative_branch(project)
    with (project / "tests/test_lib.py").open("a", encoding="utf-8") as tests:
        tests.write("\n\ndef test_broken() -> None:\n    assert sign(1) == -1\n")

    result = run_runner(project, *WHOLE, "--report", str(kept))

    assert result.returncode == 1
    assert "combined coverage is below the required 100%" in result.stdout
    assert "1 tests failed or errored according to the processes' records" in result.stdout
    # What went wrong is in the report too: it is read whatever the verdict.
    assert sum(junit_counts(kept / f"junit-{index}.xml")[1] for index in range(2)) == 1
    _, judged_totals = coverage_report(kept / "coverage-gate.json")
    assert judged_totals["missing_lines"] == 1


def test_a_report_that_cannot_be_written_changes_no_verdict(project: Path) -> None:
    """The directory asked for is under a file: nothing can be created there."""
    nowhere = project / "pyproject.toml" / "report"

    result = run_runner(project, *WHOLE, "--report", str(nowhere))

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[gate] quality report: nothing is left in {nowhere}" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout


def test_a_report_directory_that_cannot_be_written_to_changes_no_verdict(
    project: Path, kept: Path
) -> None:
    """The directory is there and closed: a pytest told to write there would end in error."""
    if os.geteuid() == 0:
        pytest.skip("no directory is closed to root")
    kept.mkdir(parents=True)
    kept.chmod(0o555)
    try:
        result = run_runner(project, *WHOLE, "--report", str(kept))
    finally:
        kept.chmod(0o755)

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"[gate] quality report: nothing is left in {kept}" in result.stdout
    assert "[gate] process 0 ended: exit code 0" in result.stdout
    assert "[gate] process 1 ended: exit code 0" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert list(kept.iterdir()) == []


def test_a_coverage_report_that_cannot_be_written_is_said_and_stops_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``coverage json`` fails on both reports: there is no data where it is told to read."""
    report_coverage(tmp_path / "no data here" / ".coverage", tmp_path)

    said = capsys.readouterr().out
    assert "[gate] quality report: coverage-gate.json could not be written" in said
    assert "[gate] quality report: coverage-all.json could not be written" in said
    assert not (tmp_path / "coverage-gate.json").exists()
    assert not (tmp_path / "coverage-all.json").exists()


def test_a_report_file_that_cannot_be_replaced_changes_no_verdict(
    project: Path, kept: Path
) -> None:
    """A directory stands where a file of the report goes: it is neither removed nor written.

    One of the two coverage reports and the JUnit file of process 1. The
    verdict is that of a run with no report, and what can be written is.
    """
    (kept / "coverage-gate.json" / "in the way").mkdir(parents=True)
    (kept / "junit-1.xml" / "in the way").mkdir(parents=True)

    result = run_runner(project, *WHOLE, "--report", str(kept))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] quality report: junit-1.xml cannot be replaced" in result.stdout
    assert "[gate] quality report: coverage-gate.json cannot be replaced" in result.stdout
    assert "[gate] quality report: coverage-gate.json could not be written" in result.stdout
    assert "[gate] process 1 ended: exit code 0" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] required coverage of 100% reached on the combined data" in result.stdout
    assert junit_counts(kept / "junit-0.xml") == (4, 0)
    assert coverage_report(kept / "coverage-all.json")[0] == {"src/__init__.py", "src/lib.py"}
    assert (kept / "coverage-gate.json" / "in the way").is_dir()
    assert (kept / "junit-1.xml" / "in the way").is_dir()


def test_shares_report_their_tests_and_the_combining_call_the_coverage(
    project: Path, kept: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    parts = kept / "quality"
    assert run_shares_reporting(project, "0-0/2", kept, parts).returncode == 0
    assert run_shares_reporting(project, "1-1/2", kept, parts).returncode == 0
    assert sum(junit_counts(parts / f"junit-{index}.xml")[0] for index in range(2)) == 6
    assert not (parts / "coverage-gate.json").exists(), "a part cannot speak of the whole"

    whole = tmp_path_factory.mktemp("report")
    arguments = ["--combine", "2", "--evidence", str(kept), "--fail-under", "100"]
    result = run_runner(project, *arguments, "--report", str(whole))

    assert result.returncode == 0, result.stdout + result.stderr
    assert coverage_report(whole / "coverage-gate.json")[0] == {"src/__init__.py", "src/lib.py"}
    assert coverage_report(whole / "coverage-all.json")[0] == {"src/__init__.py", "src/lib.py"}
    assert not list(whole.glob("junit-*.xml")), "no test is run by the combining call"


def run_shares_reporting(
    project: Path, shares: str, kept: Path, report: Path
) -> subprocess.CompletedProcess[str]:
    arguments = ["--shares", shares, "--evidence", str(kept), "--report", str(report)]
    return run_runner(project, *arguments)


def test_the_report_of_an_earlier_call_is_not_left_for_a_process_that_wrote_none(
    project: Path, kept: Path
) -> None:
    parts = kept / "quality"
    assert run_shares_reporting(project, "0-1/2", kept, parts).returncode == 0
    assert junit_counts(parts / "junit-1.xml") == (2, 0)
    misbehave_in_process_one(project, "pytest_sessionfinish", "os._exit(0)", first=True)

    again = run_shares_reporting(project, "1-1/2", kept, parts)

    assert again.returncode == 1
    assert not (parts / "junit-1.xml").exists()
    assert junit_counts(parts / "junit-0.xml") == (4, 0), "the other process's file is not ours"
