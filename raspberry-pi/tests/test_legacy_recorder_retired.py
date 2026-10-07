"""The legacy ECG recorder is gone: this directory runs the console, and nothing else.

One mode remains, the training sessions of the local console
(``python -m src.local_panel``). The recorder that used to live next to it had
its own entry point, its own configuration and its own keys in ``.env``; the
tests below pin what its removal means here:

* its modules and their tests no longer exist, and nothing names them;
* ``pyproject.toml`` exempts no file that is not on disk;
* the ``.env`` templates carry no key that nothing reads;
* every deployment file starts the console, and the container health check
  asks the console's own ``/healthz`` (the install itself, one path and pinned
  versions, is ``tests/test_pi_install.py``);
* the image's command, run as written, answers that health check in simulation
  and stops cleanly on SIGTERM.

The last one stands in for building the image, which no test does: it runs the
``ENTRYPOINT`` and ``CMD`` of the ``Dockerfile`` on this interpreter.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final, cast

import pytest

from src.local_config import load_local_config
from src.result import Err, Ok

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DOCKERFILE: Final[Path] = PROJECT_ROOT / "Dockerfile"
ENTRYPOINT: Final[Path] = PROJECT_ROOT / "docker" / "entrypoint.sh"
SERVICE_UNIT: Final[Path] = PROJECT_ROOT / "scripts" / "anheart.service"
INSTALLER: Final[Path] = PROJECT_ROOT / "scripts" / "install.sh"
ENV_TEMPLATES: Final[tuple[Path, ...]] = (
    PROJECT_ROOT / ".env.example",
    PROJECT_ROOT / ".env.pi.example",
)

CONSOLE_MODULE: Final[str] = "src.local_panel"

RETIRED_FILES: Final[tuple[str, ...]] = (
    "src/main.py",
    "src/session_manager.py",
    "src/convex_client.py",
    "src/data_buffer.py",
    "src/config.py",
    "tests/test_buffer.py",
    "tests/test_config.py",
    "tests/test_convex_client.py",
)

#: How a retired module was imported, launched or pointed at. Word-bounded so
#: that ``src.main`` does not match a longer dotted name.
RETIRED_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"\bsrc[./](?:main|session_manager|convex_client|data_buffer|config)\b(?!\.json)"
    r"|\b(?:session_manager|convex_client|data_buffer)\b"
)

#: Keys of the ``.env`` templates that no Python module reads, and who does.
KEYS_READ_OUTSIDE_PYTHON: Final[Mapping[str, str]] = {
    "BITALINO_MAC": "docker/entrypoint.sh binds the RFCOMM node to it",
    "MPLBACKEND": "matplotlib, imported by BioSPPy",
}

SCANNED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".sh", ".toml", ".yml", ".md", ".service", ".txt", ".example", ".mjs", ".js", ".html"}
)
SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".venv", "venv", "data", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)

STARTUP_TIMEOUT_S: Final[float] = 90.0
SHUTDOWN_TIMEOUT_S: Final[float] = 60.0
POLL_PERIOD_S: Final[float] = 0.25


def _captures(pattern: str, text: str) -> list[str]:
    """The first group of every match, line by line. ``re.findall`` is untyped: narrowed here."""
    found: list[str] = re.findall(pattern, text, re.MULTILINE)
    return found


def _console_keys() -> frozenset[str]:
    """Every ``.env`` key ``src/local_config.py`` declares, read from its source."""
    source = (PROJECT_ROOT / "src" / "local_config.py").read_text(encoding="utf-8")
    return frozenset(_captures(r'^KEY_[A-Z_]+: Final\[str\] = "([A-Z0-9_]+)"', source))


def _assigned_keys(path: Path) -> list[str]:
    """Keys a template sets or offers commented out (``KEY=`` and ``# KEY=``)."""
    text = path.read_text(encoding="utf-8")
    return _captures(r"^(?:# ?)?([A-Z][A-Z0-9_]*)=", text)


def _instruction(name: str) -> str:
    """The argument of the one ``name`` instruction of the Dockerfile, continuations joined."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    found = _captures(rf"^{name}\s+(.+)$", text)
    assert len(found) == 1, f"expected exactly one {name} instruction, found {found}"
    return found[0].strip()


def _exec_form(argument: str) -> list[str]:
    """``["a", "b"]`` as its words. Enough for this Dockerfile: no escaped quote in it."""
    assert re.fullmatch(r"\[.*\]", argument), f"not exec form: {argument}"
    return _captures(r'"([^"]*)"', argument)


def _healthcheck_command() -> list[str]:
    """The shell-form command of the HEALTHCHECK instruction, as an argv."""
    _options, _, command = _instruction("HEALTHCHECK").partition(" CMD ")
    assert command, "the HEALTHCHECK instruction carries no CMD"
    return shlex.split(command)


def _scanned_files() -> list[Path]:
    """Every text file of this directory a reference could hide in, this test apart."""
    return [
        path
        for path in sorted(PROJECT_ROOT.rglob("*"))
        if path.is_file()
        and path != Path(__file__).resolve()
        and not SKIPPED_DIRECTORIES.intersection(path.relative_to(PROJECT_ROOT).parts)
        and (path.suffix in SCANNED_SUFFIXES or path.name in {"Dockerfile", ".dockerignore"})
    ]


# --- The modules are gone --------------------------------------------------


def test_the_recorder_modules_and_their_tests_are_gone() -> None:
    present = [name for name in RETIRED_FILES if (PROJECT_ROOT / name).exists()]
    assert not present, f"the legacy ECG recorder is back: {present}"


def test_nothing_in_this_directory_names_a_retired_module() -> None:
    """No import, no launch command, no exemption, no sentence in a guide."""
    hits = [
        f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}"
        for path in _scanned_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if RETIRED_REFERENCE.search(line)
    ]
    assert not hits, "the legacy ECG recorder is still named:\n  " + "\n  ".join(hits)


def test_the_reference_pattern_catches_what_it_is_for() -> None:
    """The scan above is only worth what its pattern sees."""
    for line in (
        "python -m src.main",
        "from src.config import get_config",
        "from .session_manager import SessionManager",
        '"src/convex_client.py",',
        "src/data_buffer[.]py$",
        "``src/main.py`` (the Convex recorder)",
    ):
        assert RETIRED_REFERENCE.search(line), line
    for line in (
        "python -m src.local_panel",
        "from src.local_config import load_local_config",
        "simulation/cad/machine_geometry.json",
        "src.motor.drive",
    ):
        assert not RETIRED_REFERENCE.search(line), line


def test_pyproject_exempts_no_file_that_is_not_on_disk() -> None:
    """An exemption that outlives its file is how the next one hides."""
    parsed: object = tomllib.loads(  # pyright: ignore[reportAny]
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    tool = _table(_table(parsed)["tool"])
    listed: list[str] = [
        *_strings(_table(tool["basedpyright"])["exclude"]),
        *(
            entry.replace("[.]", ".").rstrip("$")
            for entry in _strings(_table(tool["mypy"])["exclude"])
        ),
        *_table(_table(_table(tool["ruff"])["lint"])["per-file-ignores"]),
    ]
    files = [entry for entry in listed if entry.endswith(".py")]
    assert "src/signal_processing.py" in files, "the scan read nothing"
    missing = sorted({entry for entry in files if not (PROJECT_ROOT / entry).is_file()})
    assert not missing, f"pyproject.toml still exempts files that do not exist: {missing}"


def _table(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict), f"expected a TOML table, got {type(value).__name__}"
    return cast("Mapping[str, object]", value)


def _strings(value: object) -> Sequence[str]:
    assert isinstance(value, list), f"expected a TOML list, got {type(value).__name__}"
    items = cast("Sequence[object]", value)
    for item in items:
        assert isinstance(item, str), f"expected a list of strings, found {item!r}"
    return cast("Sequence[str]", items)


# --- The configuration is the console's alone ------------------------------


def test_the_console_requires_three_keys_and_no_dashboard_credential() -> None:
    """``MOTOR_BACKEND``, ``ECG_SOURCE`` and the measured radius: nothing else is mandatory."""
    refused = load_local_config({})
    assert isinstance(refused, Err)
    assert [problem.key for problem in refused.error] == [
        "MOTOR_BACKEND",
        "ECG_SOURCE",
        "ARM_RADIUS_M",
    ]

    accepted = load_local_config(
        {"MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim", "ARM_RADIUS_M": "1.5"}
    )
    assert isinstance(accepted, Ok)
    assert accepted.value.cloud is None


@pytest.mark.parametrize("template", ENV_TEMPLATES, ids=[path.name for path in ENV_TEMPLATES])
def test_the_env_templates_carry_only_keys_something_reads(template: Path) -> None:
    """A key nothing reads is a setting an operator believes they made."""
    known = _console_keys() | frozenset(KEYS_READ_OUTSIDE_PYTHON)
    assert "MOTOR_BACKEND" in known, "the console's keys were not found in its source"
    unread = sorted(set(_assigned_keys(template)) - known)
    assert not unread, f"{template.name} sets keys that nothing reads: {unread}"


# --- Every deployment file starts the console ------------------------------


def test_the_image_runs_the_console_through_the_entrypoint() -> None:
    assert _exec_form(_instruction("CMD")) == ["python", "-m", CONSOLE_MODULE]
    assert _exec_form(_instruction("ENTRYPOINT")) == ["anheart-entrypoint"]
    copied = re.search(
        r"^COPY (\S+) /usr/local/bin/anheart-entrypoint$", _dockerfile(), re.MULTILINE
    )
    assert copied is not None
    assert PROJECT_ROOT / copied.group(1) == ENTRYPOINT
    assert ENTRYPOINT.read_text(encoding="utf-8").rstrip().endswith('exec "$@"')


def test_the_container_health_check_asks_the_console_healthz() -> None:
    command = _healthcheck_command()
    assert command[:2] == ["python", "-c"]
    assert "/healthz" in command[2]
    assert "UI_PORT" in command[2], "the check must follow the port the console listens on"
    # The unit that runs the image leaves that check in place (tests/test_pi_install.py).
    assert "health" not in SERVICE_UNIT.read_text(encoding="utf-8").lower()


def test_the_systemd_unit_and_the_installer_start_the_console() -> None:
    """The unit runs the image as it is: its command is the console (see above)."""
    unit = SERVICE_UNIT.read_text(encoding="utf-8").replace("\\\n", " ")
    started = _captures(r"^ExecStart=(.+)$", unit)
    assert len(started) == 1
    words = started[0].split()
    assert words[:2] == ["/usr/bin/docker", "run"]
    # The image is the last word: no command and no entrypoint replace the image's own.
    assert words[-1] == "${ANHEART_IMAGE}"
    assert "--entrypoint" not in words

    installer = INSTALLER.read_text(encoding="utf-8")
    # The installer builds that image from this directory, under the name the
    # unit is told to run, and installs this unit.
    assert 'readonly IMAGE_NAME="anheart-console"' in installer
    assert 'readonly image="$IMAGE_NAME:$version"' in installer
    assert "Environment=ANHEART_IMAGE=$image" in installer
    assert 'install_file "$TREE/scripts/$SERVICE.service" "$UNIT_FILE"' in installer


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


# --- The image's command, run as written -----------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]  # pyright: ignore[reportAny]
    return port


def _local_argv(argv: Sequence[str]) -> list[str]:
    """An argv of the image, with this interpreter and the entrypoint's path in the checkout."""
    replaced = {"python": sys.executable, "anheart-entrypoint": str(ENTRYPOINT)}
    return [replaced.get(word, word) for word in argv]


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="the entrypoint is a POSIX shell script")
def test_the_image_command_answers_its_own_health_check_in_simulation() -> None:
    """ENTRYPOINT + CMD, then HEALTHCHECK, then ``docker stop``: without the container.

    The acceptance criterion is a container that starts the console in
    simulation and answers ``/healthz``. Building the image is a manual check;
    what the image *runs* is checked here, word for word from the Dockerfile.
    """
    environment = {
        **os.environ,
        "MOTOR_BACKEND": "sim",
        "ECG_SOURCE": "sim",
        "ARM_RADIUS_M": "1.5",
        "UI_HOST": "127.0.0.1",
        "UI_PORT": str(_free_port()),
        "UI_TOKEN": "",
        # Blank wins over a developer's .env: this console must reach no dashboard.
        "MACHINE_API_KEY": "",
        "CONVEX_URL": "",
        "PRESENCE_SOURCE": "none",
        "MPLBACKEND": "Agg",
    }
    command = [
        "sh",
        *_local_argv([*_exec_form(_instruction("ENTRYPOINT")), *_exec_form(_instruction("CMD"))]),
    ]
    health = _local_argv(_healthcheck_command())

    console = subprocess.Popen(  # noqa: S603  # argv read from the Dockerfile, no shell
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        healthy = _wait_until_healthy(console, health, environment)
        console.send_signal(signal.SIGTERM)
        output, _ = console.communicate(timeout=SHUTDOWN_TIMEOUT_S)
    finally:
        if console.poll() is None:
            console.kill()
            console.communicate()

    assert healthy, f"the console never answered its health check:\n{output}"
    assert console.returncode == 0, f"SIGTERM did not end the console cleanly:\n{output}"


def _wait_until_healthy(
    console: subprocess.Popen[str], health: Sequence[str], environment: Mapping[str, str]
) -> bool:
    """Run the health check until it passes, the console exits, or the budget is spent."""
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline and console.poll() is None:
        checked = subprocess.run(  # noqa: S603  # argv read from the Dockerfile, no shell
            health, cwd=PROJECT_ROOT, env=environment, capture_output=True, check=False
        )
        if checked.returncode == 0:
            return True
        time.sleep(POLL_PERIOD_S)
    return False
