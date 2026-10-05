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
* the runner being told to stop.

Several of the throwaway scenarios are built so that ONE check is all that
stands between them and a pass: process 0 alone covers the whole of the
project, so a run where process 1 loses its coverage data or its record still
has complete coverage and passing tests.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from coverage import CoverageData
from pi_gate_parallel import check_partition, merge_coverage, read_lines
from pi_gate_shard import (
    SAME_PROCESS,
    Share,
    given,
    parse_share,
    restore_environment,
    restore_import_path,
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
