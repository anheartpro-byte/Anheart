"""The parallel runner is part of the gate, so it is tested like the gate.

Run by ``raspberry-pi/scripts/check.sh`` before it trusts the runner with the
real suite. Two kinds of test:

* the partition proof and the dealing of tests, as plain functions;
* the whole runner against a throwaway project, once green and then once for
  each way a run must NOT be able to pass: a failing test, a hole in the
  combined coverage, a process that dies while the interpreter shuts down, a
  process that never exits, and test ids that differ between processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from pi_gate_parallel import check_partition, read_lines
from pi_gate_shard import SAME_PROCESS, Share, parse_share

RUNNER: Final[Path] = Path(__file__).with_name("pi_gate_parallel.py")
RUN_TIMEOUT_S: Final[float] = 300.0

PROJECT: Final[Mapping[str, str]] = {
    "pyproject.toml": (
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-v"\n\n'
        '[tool.coverage.run]\nbranch = true\nsource = ["src"]\nrelative_files = true\n\n'
        "[tool.coverage.report]\nshow_missing = true\n"
    ),
    "src/__init__.py": "",
    "src/lib.py": (
        "def sign(value: int) -> int:\n    if value < 0:\n        return -1\n    return 1\n"
    ),
    "tests/__init__.py": "",
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

ONLY_IN_PROCESS_ONE: Final[str] = 'import os\n\nif os.environ["PI_GATE_SHARE"].startswith("1/"):\n'


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for name, content in PROJECT.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return tmp_path


def run_gate(project: Path, *, exit_grace: float = 60.0) -> subprocess.CompletedProcess[str]:
    """Run the real runner, two processes, against the throwaway project."""
    inherited = (
        "PYTEST_ADDOPTS",
        "PYTHONPATH",
        "COVERAGE_FILE",
        "PI_GATE_SHARE",
        "PI_GATE_EVIDENCE",
    )
    environment = {name: value for name, value in os.environ.items() if name not in inherited}
    command = [sys.executable, str(RUNNER), "--processes", "2", "--fail-under", "100"]
    command += ["--exit-grace", str(exit_grace)]
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        command,
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=RUN_TIMEOUT_S,
    )


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


# --- The whole runner, against a throwaway project -------------------------


def test_a_green_project_passes_and_pinned_tests_share_a_process(project: Path) -> None:
    result = run_gate(project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[gate] tests collected by each process: 6 / 6" in result.stdout
    assert "[gate] tests run by each process: 4 + 2" in result.stdout
    assert "[gate] partition proven" in result.stdout
    assert "[gate] verdicts over all processes: 6 passed" in result.stdout
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
    tests = project / "tests/test_lib.py"
    kept = tests.read_text(encoding="utf-8").replace(
        "def test_negative() -> None:\n    assert sign(-3) == -1\n", ""
    )
    tests.write_text(kept, encoding="utf-8")
    result = run_gate(project)
    assert result.returncode == 1
    assert "[gate] verdicts over all processes: 5 passed" in result.stdout
    assert "combined coverage is below the required 100%" in result.stdout
    assert "did not end cleanly" not in result.stdout


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
