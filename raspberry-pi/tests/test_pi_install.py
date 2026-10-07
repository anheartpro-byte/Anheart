"""The install of the Raspberry Pi: one path, pinned versions, a script safe to run again.

A new Pi is installed by ``scripts/install.sh`` and starts the console by
itself at power-on (``scripts/anheart.service``). What that means is pinned
here, without a Pi and without Docker:

* **one path**: the console's Docker image, started by systemd. No Compose
  file, no virtual environment on the host;
* **pinned versions**: the base image by release and digest, every Python
  package by exact version and hash, and ``docs/pi-image.md`` listing each of
  them as the files have it;
* **the installer**, run for real under ``bash`` against a scratch root
  (``ANHEART_ROOT``) with scripted system commands first on ``PATH``: what a
  first run creates, that a second run changes and restarts nothing, that an
  existing configuration is never rewritten and its secret never shown, and
  that a console which does not say it is at rest is never restarted;
* **the CI test**: its workflow starts the end-to-end script, can only read
  the repository and takes no name of a required check.

The end-to-end run, a real install in a stand-in machine where the console
really starts and answers ``/healthz``, is ``scripts/pi/test_install.sh``,
which the workflow ``pi-install.yml`` runs on an arm64 runner.
"""

from __future__ import annotations

import itertools
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from src.local_config import EcgSource, MotorBackend, load_local_config
from src.result import Ok

CONSOLE: Final[Path] = Path(__file__).resolve().parents[1]
REPOSITORY: Final[Path] = CONSOLE.parent
INSTALLER: Final[Path] = CONSOLE / "scripts" / "install.sh"
SERVICE_UNIT: Final[Path] = CONSOLE / "scripts" / "anheart.service"
END_TO_END: Final[Path] = CONSOLE / "scripts" / "pi" / "test_install.sh"
DOCKERFILE: Final[Path] = CONSOLE / "Dockerfile"
ENV_TEMPLATE: Final[Path] = CONSOLE / ".env.pi.example"
VERSION_FILE: Final[Path] = CONSOLE / "VERSION"
LOCK: Final[Path] = CONSOLE / "requirements-lock.txt"
BUILD_LOCK: Final[Path] = CONSOLE / "requirements-build-lock.txt"
VERSIONS_PAGE: Final[Path] = REPOSITORY / "docs" / "pi-image.md"
WORKFLOWS: Final[Path] = REPOSITORY / ".github" / "workflows"
WORKFLOW: Final[Path] = WORKFLOWS / "pi-install.yml"

BASH: Final[str | None] = shutil.which("bash")
needs_bash: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    BASH is None, reason="install.sh is a bash script"
)

# Synthetic, for these tests only.
MACHINE_KEY: Final[str] = "synthetic-machine-key-" + "0" * 42
PAGE_TOKEN: Final[str] = "synthetic-page-token-0123456789"  # noqa: S105  # synthetic

IDLE: Final[str] = '{"run_state":"idle","estop_latched":false}'
RUNNING: Final[str] = '{"run_state":"running","estop_latched":false}'
HEALTHY: Final[str] = '{"status":"ok","service":"anheart-operator-interface"}'

#: The system commands install.sh may not really run in a test. Each records
#: its arguments, then answers from the files of ``$STUB_STATE``.
STUB: Final[str] = r"""#!/bin/sh
name="$(basename "$0")"
printf '%s %s\n' "$name" "$*" >> "$STUB_STATE/calls"
has() { [ -e "$STUB_STATE/$1" ]; }
for last in "$@"; do :; done
case "$name" in
    id) cat "$STUB_STATE/uid" ;;
    dpkg) echo arm64 ;;
    dpkg-query)
        case "$*" in
            *Status-Status*)
                if has packages; then printf installed; else printf not-installed; fi ;;
            *) printf '0.0-scripted' ;;
        esac ;;
    apt-get) if [ "$1" = install ]; then : > "$STUB_STATE/packages"; fi ;;
    getent) has account ;;
    useradd) : > "$STUB_STATE/account" ;;
    chown | sleep | journalctl) ;;
    systemctl)
        case "$1" in
            is-active) has active ;;
            is-enabled) has enabled ;;
            start | restart) : > "$STUB_STATE/active" ;;
            enable) case "$*" in *anheart.service*) : > "$STUB_STATE/enabled" ;; esac ;;
            status) echo "scripted status" ;;
        esac ;;
    docker)
        case "$1" in
            image)
                has "image.$(printf '%s' "$last" | tr ':' '_')" || exit 1
                cat "$STUB_STATE/image.$(printf '%s' "$last" | tr ':' '_')" ;;
            build)
                printf 'sha256:%s' "$(cat "$STUB_STATE/source")" \
                    > "$STUB_STATE/image.$(printf '%s' "$3" | tr ':' '_')"
                if has status-after-build; then
                    cp "$STUB_STATE/status-after-build" "$STUB_STATE/status"
                fi ;;
        esac ;;
    curl)
        case "$*" in *"--config -"*) cat >> "$STUB_STATE/curl-stdin" ;; esac
        has active || exit 7
        case "$last" in
            */healthz) has healthz || exit 22; cat "$STUB_STATE/healthz" ;;
            */api/status) cat "$STUB_STATE/status" ;;
        esac ;;
esac
"""
STUBBED: Final[tuple[str, ...]] = (
    "id",
    "dpkg",
    "dpkg-query",
    "apt-get",
    "getent",
    "useradd",
    "chown",
    "sleep",
    "journalctl",
    "systemctl",
    "docker",
    "curl",
)


def _captures(pattern: str, text: str) -> list[str]:
    """The first group of every match, line by line. ``re.findall`` is untyped: narrowed here."""
    found: list[str] = re.findall(pattern, text, re.MULTILINE)
    return found


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _version() -> str:
    return _read(VERSION_FILE).strip()


# =========================================================================
# The installer, run for real against a scratch root
# =========================================================================


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one run of the installer said and asked of the system."""

    exit_code: int
    output: str
    calls: tuple[str, ...]

    def called(self, *words: str) -> bool:
        """Whether one recorded command starts with ``words[0]`` and holds every other word."""
        return any(
            call.startswith(words[0]) and all(word in call for word in words[1:])
            for call in self.calls
        )


@dataclass(frozen=True, slots=True)
class Machine:
    """A scratch system: the root the installer writes under, its copy of the tree, the stubs."""

    root: Path
    tree: Path
    tools: Path
    state: Path

    @property
    def env_file(self) -> Path:
        return self.root / "etc" / "anheart" / "anheart.env"

    @property
    def unit(self) -> Path:
        return self.root / "etc" / "systemd" / "system" / "anheart.service"

    @property
    def drop_in(self) -> Path:
        return self.unit.parent / "anheart.service.d" / "10-version.conf"

    def set(self, name: str, text: str = "") -> None:
        """Script one answer of the stubs (see :data:`STUB`)."""
        (self.state / name).write_text(text, encoding="utf-8")

    def unset(self, name: str) -> None:
        (self.state / name).unlink(missing_ok=True)

    def install(self, *arguments: str) -> Outcome:
        """Run the real ``install.sh`` of this machine's tree. Each run records its own calls."""
        assert BASH is not None
        calls = self.state / "calls"
        calls.unlink(missing_ok=True)
        done = subprocess.run(  # noqa: S603  # fixed argv, no shell, scripted tools only
            [BASH, str(self.tree / "scripts" / "install.sh"), *arguments],
            env={
                "PATH": f"{self.tools}{os.pathsep}{os.environ['PATH']}",
                "ANHEART_ROOT": str(self.root),
                "ANHEART_HEALTH_TIMEOUT_S": "4",
                "STUB_STATE": str(self.state),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        recorded = tuple(_read(calls).splitlines()) if calls.exists() else ()
        return Outcome(exit_code=done.returncode, output=done.stdout + done.stderr, calls=recorded)


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    """A fresh Debian 12 as the installer sees it: nothing installed, nothing running."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text(
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_CODENAME=bookworm\nID=debian\n',
        encoding="utf-8",
    )
    tree = tmp_path / "anheart" / "raspberry-pi"
    (tree / "scripts").mkdir(parents=True)
    for source in (INSTALLER, SERVICE_UNIT):
        shutil.copy(source, tree / "scripts" / source.name)
    for source in (ENV_TEMPLATE, VERSION_FILE):
        shutil.copy(source, tree / source.name)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in STUBBED:
        tool = tools / name
        tool.write_text(STUB, encoding="utf-8")
        tool.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    made = Machine(root=root, tree=tree, tools=tools, state=state)
    made.set("uid", "0\n")
    made.set("source", "0001")
    made.set("status", IDLE)
    made.set("healthz", HEALTHY)
    return made


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _settings(path: Path) -> dict[str, str]:
    """The ``KEY=value`` lines of a configuration, as ``docker run --env-file`` reads them."""
    settings: dict[str, str] = {}
    for line in _read(path).splitlines():
        if re.match(r"[A-Z]", line):
            key, _, value = line.partition("=")
            settings[key] = value
    return settings


@pytest.mark.slow
@needs_bash
def test_a_first_install_leaves_the_account_the_directories_and_the_service(
    machine: Machine,
) -> None:
    """EX-2: system packages, the account, the two directories, the unit enabled and started."""
    version = _version()
    first = machine.install()
    assert first.exit_code == 0, first.output

    for package in ("docker.io", "bluez", "libusb-1.0-0"):
        assert first.called("apt-get install", package), package
    assert first.called("useradd", "--system", "anheart")

    state = machine.root / "var" / "lib" / "anheart"
    assert _mode(state / "data") == 0o750
    assert _mode(state / "records") == 0o700, "the session records are private"
    for directory in (state, state / "data", state / "records"):
        assert first.called("chown anheart:anheart", str(directory)), directory

    assert _read(machine.unit) == _read(SERVICE_UNIT)
    assert first.called("docker build", f"--tag anheart-console:{version}", str(machine.tree))
    assert first.called("systemctl enable anheart.service")
    assert first.called("systemctl start anheart.service")
    assert not first.called("systemctl restart")
    # The start test: the console answered /healthz, and said it was at rest.
    assert first.called("curl", "/healthz")
    assert first.called("curl", "/api/status")
    assert "the console is at rest" in first.output

    # Nothing is started before there is an image and a unit to start.
    order = [
        call.split(" ", 2)[1] for call in first.calls if call.startswith(("docker ", "systemctl "))
    ]
    assert order.index("build") < order.index("start")


@pytest.mark.slow
@needs_bash
def test_systemd_is_told_the_version_of_the_version_file(machine: Machine) -> None:
    """EX-4: ``systemctl status anheart`` shows the version, and the unit runs that image."""
    (machine.tree / "VERSION").write_text("pi-1.4.2\n", encoding="utf-8")
    done = machine.install()
    assert done.exit_code == 0, done.output
    written = _read(machine.drop_in)
    assert "Description=Console opérateur Anheart, version pi-1.4.2\n" in written
    assert "Environment=ANHEART_IMAGE=anheart-console:pi-1.4.2\n" in written
    assert done.called("docker build", "--label org.opencontainers.image.version=pi-1.4.2")


@pytest.mark.slow
@needs_bash
@pytest.mark.parametrize("content", ["", "pi 1.4.2\n", "pi-1.4.2+local\n", "../pi\n"])
def test_a_version_that_cannot_name_an_image_is_refused_before_anything(
    machine: Machine, content: str
) -> None:
    (machine.tree / "VERSION").write_text(content, encoding="utf-8")
    done = machine.install()
    assert done.exit_code == 1
    assert "does not hold a usable version" in done.output
    assert not done.called("apt-get")
    assert not done.called("docker")
    assert not machine.unit.exists()


@pytest.mark.slow
@needs_bash
def test_a_second_install_changes_nothing_and_restarts_nothing(machine: Machine) -> None:
    """EX-2, idempotence: same files, no package manager, no start, no restart."""
    assert machine.install().exit_code == 0
    before = {path: path.read_bytes() for path in (machine.env_file, machine.unit, machine.drop_in)}

    second = machine.install()
    assert second.exit_code == 0, second.output
    assert {path: path.read_bytes() for path in before} == before
    for never in ("apt-get", "useradd", "systemctl start", "systemctl restart"):
        assert not second.called(never), never
    assert "nothing changed: left alone" in second.output
    assert second.called("systemctl is-enabled")


@pytest.mark.slow
@needs_bash
def test_the_generated_configuration_is_the_template_and_holds_no_secret(
    machine: Machine,
) -> None:
    """EX-2: written from ``.env.pi.example``, private, with no key of any dashboard."""
    assert machine.install().exit_code == 0
    assert _read(machine.env_file) == _read(ENV_TEMPLATE)
    assert _mode(machine.env_file) == 0o600
    settings = _settings(machine.env_file)
    assert settings["MACHINE_API_KEY"] == "", "a new machine is linked to no dashboard"
    assert "UI_TOKEN" not in settings
    loaded = load_local_config(settings)
    assert isinstance(loaded, Ok), "the console would refuse the generated configuration"
    assert loaded.value.cloud is None


def test_the_template_is_written_the_way_docker_reads_a_configuration() -> None:
    """``docker run --env-file`` strips nothing: no quote, no trailing comment or space."""
    for key, value in _settings(ENV_TEMPLATE).items():
        assert not re.search(r"\s#|^[\"']|[\"']$|\s$", value), f"{key} would not be read as written"


@pytest.mark.slow
@needs_bash
def test_a_value_docker_would_misread_is_named_without_being_shown(machine: Machine) -> None:
    assert machine.install().exit_code == 0
    assert "read as part of the value" not in machine.install().output
    machine.env_file.write_text(
        _read(ENV_TEMPLATE)
        .replace("MOTOR_MAX_RPM=300\n", "MOTOR_MAX_RPM=300   # bench ceiling\n")
        .replace("# UI_TOKEN=\n", f'UI_TOKEN="{PAGE_TOKEN}"\n'),
        encoding="utf-8",
    )
    warned = machine.install()
    assert warned.exit_code == 0, warned.output
    assert "read as part of the value of: MOTOR_MAX_RPM UI_TOKEN (write KEY=value" in warned.output
    assert PAGE_TOKEN not in warned.output
    assert "bench ceiling" not in warned.output


@pytest.mark.slow
@needs_bash
def test_simulation_is_written_into_a_new_configuration_only(machine: Machine) -> None:
    """``--simulation``: the console's own switch, and never on a file that exists."""
    first = machine.install("--simulation")
    assert first.exit_code == 0, first.output
    settings = _settings(machine.env_file)
    template = _settings(ENV_TEMPLATE)
    assert settings == {**template, "MOTOR_BACKEND": "sim", "ECG_SOURCE": "sim"}
    loaded = load_local_config(settings)
    assert isinstance(loaded, Ok)
    assert loaded.value.motor_backend is MotorBackend.SIM
    assert loaded.value.ecg_source is EcgSource.SIM
    assert loaded.value.cloud is None, "the simulation contacts no dashboard"
    assert "SIMULATION" in first.output

    machine.env_file.write_text(_read(ENV_TEMPLATE), encoding="utf-8")
    again = machine.install("--simulation")
    assert again.exit_code == 0, again.output
    assert _read(machine.env_file) == _read(ENV_TEMPLATE)
    assert "--simulation only applies to a new file" in again.output


@pytest.mark.slow
@needs_bash
def test_an_existing_configuration_is_kept_and_its_secrets_are_never_shown(
    machine: Machine,
) -> None:
    """The machine's key and the page's token: not rewritten, not printed, not on a command line."""
    assert machine.install().exit_code == 0
    filled = (
        _read(ENV_TEMPLATE)
        .replace("MACHINE_API_KEY=\n", f"MACHINE_API_KEY={MACHINE_KEY}\n")
        .replace("# UI_TOKEN=\n", f"UI_TOKEN={PAGE_TOKEN}\n")
    )
    assert MACHINE_KEY in filled
    assert PAGE_TOKEN in filled
    machine.env_file.write_text(filled, encoding="utf-8")
    machine.env_file.chmod(0o644)
    # A new image: the console is asked whether it is at rest, with its token.
    machine.set("source", "0002")

    done = machine.install()
    assert done.exit_code == 0, done.output
    assert _read(machine.env_file) == filled
    assert _mode(machine.env_file) == 0o600, "an existing file is made private again"
    assert done.called("chown root:root", str(machine.env_file))
    for secret in (MACHINE_KEY, PAGE_TOKEN):
        assert secret not in done.output
        assert not any(secret in call for call in done.calls), "a secret on a command line"
    assert done.called("systemctl restart anheart.service")
    # The token reached curl all the same: on its standard input.
    assert f"x-anheart-token: {PAGE_TOKEN}" in _read(machine.state / "curl-stdin")


@pytest.mark.slow
@needs_bash
@pytest.mark.parametrize("answer", [RUNNING, "", '{"detail":"a valid x-anheart-token header"}'])
def test_a_console_that_does_not_say_it_is_at_rest_is_never_replaced(
    machine: Machine, answer: str
) -> None:
    """A session, no answer or a refusal: nothing is built, written or restarted."""
    assert machine.install().exit_code == 0
    before = {path: path.read_bytes() for path in (machine.unit, machine.drop_in)}
    (machine.tree / "VERSION").write_text("pi-9.9.9\n", encoding="utf-8")
    machine.set("source", "0002")
    machine.set("status", answer)

    refused = machine.install()
    assert refused.exit_code == 1
    assert "does not say it is at rest: nothing is restarted" in refused.output
    for never in (
        "docker build",
        "systemctl restart",
        "systemctl start",
        "systemctl stop",
        "apt-get",
    ):
        assert not refused.called(never), never
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.slow
@needs_bash
def test_a_session_started_during_the_build_is_not_interrupted(machine: Machine) -> None:
    """The console is asked again right before the restart: the build took minutes."""
    assert machine.install().exit_code == 0
    machine.set("source", "0002")
    machine.set("status-after-build", RUNNING)

    refused = machine.install()
    assert refused.exit_code == 1
    assert refused.called("docker build")
    assert not refused.called("systemctl restart")
    assert "does not say it is at rest: nothing is restarted" in refused.output


@pytest.mark.slow
@needs_bash
def test_a_console_at_rest_is_restarted_when_its_image_changed(machine: Machine) -> None:
    assert machine.install().exit_code == 0
    machine.set("source", "0002")

    done = machine.install()
    assert done.exit_code == 0, done.output
    names = [call.split(" ", 2)[:2] for call in done.calls]
    asked = [index for index, call in enumerate(done.calls) if "/api/status" in call]
    restarted = names.index(["systemctl", "restart"])
    assert asked, "the console was never asked whether it was at rest"
    assert any(index < restarted for index in asked)
    assert "the console is at rest" in done.output


@pytest.mark.slow
@needs_bash
def test_the_start_test_fails_when_the_console_never_answers_healthz(machine: Machine) -> None:
    """EX-2, start test: a service that is started but does not answer is a failed install."""
    machine.unset("healthz")
    failed = machine.install()
    assert failed.exit_code == 1
    assert "did not answer" in failed.output
    assert "/healthz" in failed.output
    assert failed.called("journalctl", "anheart")


@pytest.mark.slow
@needs_bash
def test_the_start_test_fails_when_a_started_console_is_not_at_rest(machine: Machine) -> None:
    """A console this script started and that reports anything but rest: refused out loud."""
    machine.set("status", RUNNING)
    failed = machine.install()
    assert failed.exit_code == 1
    assert "does not say it is at rest after its start" in failed.output


@pytest.mark.slow
@needs_bash
def test_another_system_than_the_pinned_one_is_refused(machine: Machine) -> None:
    (machine.root / "etc" / "os-release").write_text(
        "VERSION_CODENAME=trixie\nID=debian\n", encoding="utf-8"
    )
    refused = machine.install()
    assert refused.exit_code == 1
    assert "bookworm" in refused.output
    assert not refused.called("apt-get")
    assert not refused.called("docker")


@pytest.mark.slow
@needs_bash
def test_the_installer_says_which_raspberry_pi_os_it_was_checked_on(machine: Machine) -> None:
    """The reference of the flashed image is compared with the pinned one, and only warned about."""
    pinned = _captures(r'^readonly OS_REFERENCE="([0-9-]+)"$', _read(INSTALLER))
    assert len(pinned) == 1
    issue = machine.root / "etc" / "rpi-issue"

    issue.write_text(
        f"Raspberry Pi reference {pinned[0]}\nGenerated using pi-gen\n", encoding="utf-8"
    )
    same = machine.install()
    assert same.exit_code == 0, same.output
    assert f"Raspberry Pi OS reference {pinned[0]}" in same.output
    assert "WARNING: Raspberry Pi OS reference" not in same.output

    issue.write_text("Raspberry Pi reference 2031-01-01\n", encoding="utf-8")
    other = machine.install()
    assert other.exit_code == 0, other.output
    assert (
        f"WARNING: Raspberry Pi OS reference '2031-01-01', this version was checked on {pinned[0]}"
        in other.output
    )


@pytest.mark.slow
@needs_bash
def test_the_installer_refuses_to_run_without_root_or_with_an_unknown_option(
    machine: Machine,
) -> None:
    machine.set("uid", "1000\n")
    refused = machine.install()
    assert refused.exit_code == 1
    assert "sudo bash scripts/install.sh" in refused.output
    assert not refused.called("apt-get")

    machine.set("uid", "0\n")
    unknown = machine.install("--force")
    assert unknown.exit_code == 2
    assert "usage:" in unknown.output
    assert not unknown.called("apt-get")


# =========================================================================
# One path: the image, started by systemd
# =========================================================================


def _unit_lines() -> list[str]:
    """The unit's directives, continuation lines joined, comments left out."""
    joined = _read(SERVICE_UNIT).replace("\\\n", " ")
    return [
        line.strip() for line in joined.splitlines() if line.strip() and not line.startswith("#")
    ]


def _directive(name: str) -> list[str]:
    return [line.partition("=")[2] for line in _unit_lines() if line.startswith(f"{name}=")]


def test_one_way_to_run_the_console_on_a_pi_remains() -> None:
    """EX-1: no Compose file, no environment built on the host, one unit that runs the image."""
    assert not sorted(path.name for path in CONSOLE.glob("docker-compose*"))
    installer = _read(INSTALLER)
    for native in ("-m venv", "pip install", "python3-venv", "docker compose"):
        assert native not in installer, f"install.sh still knows the other path: {native}"
    started = _directive("ExecStart")
    assert len(started) == 1
    words = started[0].split()
    assert words[:3] == ["/usr/bin/docker", "run", "--rm"]
    # The image is the last word: nothing replaces the command the image runs,
    # which tests/test_legacy_recorder_retired.py holds to the console.
    assert words[-1] == "${ANHEART_IMAGE}"
    assert "--entrypoint" not in words


def test_the_unit_gives_the_console_its_configuration_and_its_two_directories() -> None:
    words = _directive("ExecStart")[0].split()
    pairs = set(itertools.pairwise(words))
    assert ("--env-file", "/etc/anheart/anheart.env") in pairs
    assert ("--volume", "/var/lib/anheart/data:/app/data") in pairs
    # The default RECORD_ROOT of the console, data/records: no key to set.
    assert ("--volume", "/var/lib/anheart/records:/app/data/records") in pairs
    installer = _read(INSTALLER)
    assert 'readonly ENV_FILE="$CONFIG_DIR/anheart.env"' in installer
    assert 'readonly CONFIG_DIR="$ROOT/etc/anheart"' in installer
    assert 'readonly STATE_DIR="$ROOT/var/lib/anheart"' in installer


def test_the_unit_stops_the_console_on_its_controlled_stop_and_never_loops_on_a_refusal() -> None:
    """SIGTERM with time for the ramp; a restart on failure; none on a configuration error."""
    assert _directive("ExecStop") == ["/usr/bin/docker stop -t 60 anheart"]
    assert int(_directive("TimeoutStopSec")[0]) > 60
    assert _directive("Restart") == ["on-failure"]
    assert _directive("RestartPreventExitStatus") == ["2"]
    assert _directive("WantedBy") == ["multi-user.target"]
    assert "docker.service" in _directive("Requires")[0]
    assert "docker.service" in _directive("After")[0]
    # Nothing in the unit asks for motion or names a session: it starts an image.
    unit = "\n".join(_unit_lines())
    for word in ("curl", "/api/", "ExecStartPost"):
        assert word not in unit, word
    # A console is only ever ended by its controlled stop: never killed, never
    # removed by force, whatever step of the unit finds it.
    for forced in ("docker kill", "rm --force", "rm -f", "SIGKILL", "KillSignal"):
        assert forced not in unit, forced
    for step in (*_directive("ExecStartPre"), *_directive("ExecStopPost")):
        assert "/usr/bin/docker stop -t 60 anheart" in step


def test_the_unit_leaves_the_image_health_check_on_healthz_in_place() -> None:
    """EX-4: the health check is the image's, on ``/healthz``; the unit does not replace it."""
    unit = "\n".join(_unit_lines())
    assert "--no-healthcheck" not in unit
    assert "--health-" not in unit
    dockerfile = _read(DOCKERFILE)
    assert re.search(r"^HEALTHCHECK .*\n\s+CMD .*/healthz", dockerfile, re.MULTILINE)
    assert '"$(console_url)/healthz"' in _read(INSTALLER), "the start test asks /healthz too"


# =========================================================================
# Pinned versions
# =========================================================================

PIN: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.]+)")


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(path: Path) -> dict[str, str]:
    """``name: version`` for every requirement of a file; a requirement that is not exact fails."""
    pins: dict[str, str] = {}
    for line in _read(path).splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "-r ", "--hash")):
            continue
        matched = PIN.match(line)
        assert matched is not None, f"{path.name}: not an exact version: {line}"
        pins[_normalized(matched.group(1))] = matched.group(2)
    return pins


def _locked(path: Path) -> dict[str, str]:
    """``name: version`` for every package of a lock file; each must carry a hash."""
    entries = re.split(r"\n(?=[A-Za-z0-9])", _read(path))
    pins: dict[str, str] = {}
    for raw in entries:
        entry = "\n".join(line for line in raw.splitlines() if not line.startswith("#")).strip()
        if not entry:
            continue
        matched = PIN.match(entry)
        assert matched is not None, f"{path.name}: unreadable entry: {entry[:60]}"
        assert "--hash=sha256:" in entry, f"{path.name}: {matched.group(1)} has no hash"
        pins[_normalized(matched.group(1))] = matched.group(2)
    return pins


def _base_image() -> str:
    found = _captures(r"^FROM (\S+)$", _read(DOCKERFILE))
    assert len(found) == 1
    return found[0]


def test_the_base_image_is_python_3_12_by_release_and_digest() -> None:
    """EX-3: Python 3.12, an exact release, and the digest that makes the tag immutable."""
    assert re.fullmatch(r"python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}", _base_image())


def test_every_runtime_requirement_is_an_exact_version() -> None:
    """EX-3: ``requirements-base.txt`` pinned, and what the image adds to it."""
    base = _requirements(CONSOLE / "requirements-base.txt")
    assert {"numpy", "scipy", "pymodbus", "pyftdi", "fastapi", "pydantic"} <= set(base)
    production = _requirements(CONSOLE / "requirements-prod.txt")
    assert set(production) == {"bitalino", "pexpect"}
    tools = _requirements(CONSOLE / "requirements-build.txt")
    assert set(tools) == {"setuptools", "wheel"}


def test_the_lock_files_hold_every_requirement_at_its_version_with_a_hash() -> None:
    locked = _locked(LOCK)
    wanted = {
        **_requirements(CONSOLE / "requirements-base.txt"),
        **_requirements(CONSOLE / "requirements-prod.txt"),
    }
    # macOS only (its marker keeps it off the Pi): not in the image's lock.
    del wanted["pyobjc-framework-iobluetooth"]
    for name, version in wanted.items():
        assert locked.get(name) == version, f"{name}: {version} required, {locked.get(name)} locked"
    assert len(locked) > len(wanted), "the lock names the packages the requirements pull in"
    assert not [name for name in locked if name.startswith("pyobjc")]

    tools = _locked(BUILD_LOCK)
    for name, version in _requirements(CONSOLE / "requirements-build.txt").items():
        assert tools.get(name) == version, name


def test_the_image_installs_the_lock_files_and_nothing_else() -> None:
    """Hashes required, no upgrade of pip, no requirement file that is not a lock."""
    dockerfile = _read(DOCKERFILE).replace("\\\n", " ")
    installs = [
        line for line in dockerfile.splitlines() if re.match(r"RUN .*\bpip install\b", line)
    ]
    assert len(installs) == 1
    commands = [command.split() for command in installs[0].removeprefix("RUN ").split("&&")]
    assert [command[command.index("-r") + 1] for command in commands] == [
        BUILD_LOCK.name,
        LOCK.name,
    ]
    for command in commands:
        assert command[:2] == ["pip", "install"]
        assert "--require-hashes" in command
        assert "--upgrade" not in command
    # The one package built from source is built with the locked tools.
    assert "--no-build-isolation" in commands[1]
    copied = _captures(r"^COPY (requirements\S*(?: requirements\S*)*) \./$", dockerfile)
    assert copied == [f"{BUILD_LOCK.name} {LOCK.name}"]


def test_the_image_carries_its_version() -> None:
    assert re.search(r"^COPY VERSION \./VERSION$", _read(DOCKERFILE), re.MULTILINE)


def test_the_versions_page_lists_every_pinned_version() -> None:
    """EX-3: ``docs/pi-image.md`` says what the files say, version by version."""
    page = _read(VERSIONS_PAGE)
    image, _, digest = _base_image().partition("@")
    assert f"`{image}`" in page
    assert digest in page

    installer = _read(INSTALLER)
    reference = _captures(r'^readonly OS_REFERENCE="([0-9-]+)"$', installer)[0]
    codename = _captures(r'^readonly OS_CODENAME="([a-z]+)"$', installer)[0]
    assert f"`{reference}-raspios-{codename}-arm64-lite.img.xz`" in page
    assert re.search(
        rf"{reference}-raspios-{codename}-arm64-lite\.img\.xz.*\n?.*`[0-9a-f]{{64}}`", page
    )

    wanted = {
        **_requirements(CONSOLE / "requirements-base.txt"),
        **_requirements(CONSOLE / "requirements-prod.txt"),
        **_requirements(CONSOLE / "requirements-build.txt"),
    }
    listed: list[tuple[str, str]] = re.findall(
        r"^\| `([A-Za-z0-9._-]+)` \| `([^`]+)` \|", page, re.MULTILINE
    )
    rows = {_normalized(name): version for name, version in listed}
    for name, version in wanted.items():
        assert rows.get(name) == version, (
            f"{name}: {version} in the files, {rows.get(name)} on the page"
        )

    stand_in = _captures(r'^readonly MACHINE_BASE="(\S+)"$', _read(END_TO_END))
    assert len(stand_in) == 1
    assert stand_in[0].partition("@")[0] in page


# =========================================================================
# The CI test
# =========================================================================


def _workflow(path: Path) -> str:
    """A workflow without its comment lines: what GitHub acts on."""
    return "\n".join(line for line in _read(path).splitlines() if not line.lstrip().startswith("#"))


def _job_names(text: str) -> set[str]:
    """Every name a job of a workflow reports under: its id and its display name."""
    body = text.partition("\njobs:\n")[2]
    return set(_captures(r"^ {2}([a-z][a-z0-9-]*):[ \t]*$", body)) | set(
        _captures(r"^ {4}name:[ \t]*(.+)$", body)
    )


def test_the_workflow_runs_the_end_to_end_install_on_arm64() -> None:
    """EX-5: the real install, in a stand-in machine, on the architecture of the Pi."""
    workflow = _workflow(WORKFLOW)
    assert "runs-on: ubuntu-24.04-arm\n" in workflow
    assert "bash raspberry-pi/scripts/pi/test_install.sh" in workflow
    script = _read(END_TO_END)
    assert 'bash "$COPY/scripts/install.sh" --simulation' in script, "the real installer is run"
    for proof in (
        "systemctl is-enabled anheart",
        '"status":"ok"',
        '"run_state":"idle"',
        '"attested":false',
        "systemctl status anheart",
        "/var/lib/anheart/records -mindepth 2 -maxdepth 2 -name manifest.json",
        "docker stop -t 120",
    ):
        assert proof in script, proof


def test_the_workflow_can_only_read_and_pins_every_action() -> None:
    workflow = _workflow(WORKFLOW)
    permissions = re.search(r"^permissions:\n((?: {2}.*\n)+)", workflow, re.MULTILINE)
    assert permissions is not None
    assert permissions.group(1) == "  contents: read\n"
    assert ": write" not in workflow
    assert "write-all" not in workflow
    assert "secrets." not in workflow
    assert "pull_request_target" not in workflow
    assert "workflow_run" not in workflow
    used = _captures(r"^ +(?:- )?uses:[ \t]*(.*)$", workflow)
    assert used
    for action in used:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+", action), action
    assert workflow.count("persist-credentials: false") == workflow.count("actions/checkout@")
    # No value a pull request controls is written into a script.
    scripts = "\n".join(_captures(r"^ {10}(.*)$", workflow))
    assert "${{" not in scripts


def test_the_workflow_takes_no_name_of_a_required_check() -> None:
    taken = _job_names(_workflow(WORKFLOWS / "ci.yml"))
    assert {"pi-gate", "docs"} <= taken, "the jobs of ci.yml were not read"
    mine = _job_names(_workflow(WORKFLOW))
    assert mine == {"pi-install"}
    assert not mine & taken


def test_the_workflow_starts_for_every_file_the_install_is_made_of() -> None:
    """On a pull request it is filtered by path: each of these must be in the filter."""
    workflow = _workflow(WORKFLOW)
    listed = set(_captures(r"^ {6}- '([^']+)'$", workflow))
    for path in (
        ".github/workflows/pi-install.yml",
        "raspberry-pi/Dockerfile",
        "raspberry-pi/.dockerignore",
        "raspberry-pi/.env.pi.example",
        "raspberry-pi/VERSION",
        "raspberry-pi/docker/**",
        "raspberry-pi/requirements*.txt",
        "raspberry-pi/scripts/install.sh",
        "raspberry-pi/scripts/anheart.service",
        "raspberry-pi/scripts/pi/test_install.sh",
    ):
        assert path in listed, path
    # Every push to develop runs it whatever changed: the console's sources included.
    assert re.search(r"^ {2}push:\n {4}branches: \[develop\]$", workflow, re.MULTILINE)
