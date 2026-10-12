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
  that a console which does not say it is at rest is never restarted, whether
  the service started it or not;
* **the image's entrypoint**, run for real under ``sh``: the session records
  are stamped with the version the image was built from;
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
import shlex
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from src.contract import UNKNOWN_SOFTWARE_VERSION, VERSION_PATH, read_software_version
from src.local_config import EcgSource, MotorBackend, load_local_config
from src.result import Ok
from src.service_state import THRESHOLD, WINDOW

CONSOLE: Final[Path] = Path(__file__).resolve().parents[1]
REPOSITORY: Final[Path] = CONSOLE.parent
INSTALLER: Final[Path] = CONSOLE / "scripts" / "install.sh"
HARDENER: Final[Path] = CONSOLE / "scripts" / "harden.sh"
UNINSTALLER: Final[Path] = CONSOLE / "scripts" / "uninstall.sh"
BIND_HELPER: Final[Path] = CONSOLE / "scripts" / "pi" / "bind-bitalino.sh"
SERVICE_UNIT: Final[Path] = CONSOLE / "scripts" / "anheart.service"
END_TO_END: Final[Path] = CONSOLE / "scripts" / "pi" / "test_install.sh"
DOCKERFILE: Final[Path] = CONSOLE / "Dockerfile"
ENTRYPOINT: Final[Path] = CONSOLE / "docker" / "entrypoint.sh"
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

SH: Final[str | None] = shutil.which("sh")
needs_sh: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    SH is None, reason="the entrypoint is a POSIX shell script"
)

# Synthetic, for these tests only.
MACHINE_KEY: Final[str] = "synthetic-machine-key-" + "0" * 42
PAGE_TOKEN: Final[str] = "synthetic-page-token-0123456789"  # noqa: S105  # synthetic

#: Numbers no account and no file of a test machine has.
FOREIGN_ID: Final[int] = 424242
#: The group of the serial devices, as the scripted ``getent`` numbers it.
DEVICE_GID: Final[int] = 20

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
    # `id -u`: who runs the script. With a name: the numbers of that account,
    # which the tests choose (see Machine.owned_by_the_account).
    id)
        case "$*" in
            "-u") cat "$STUB_STATE/uid" ;;
            "-u anheart") has account || exit 1; cat "$STUB_STATE/account-uid" ;;
            "-g anheart") has account || exit 1; cat "$STUB_STATE/account-gid" ;;
            "-u root") cat "$STUB_STATE/root-uid" ;;
            "-g root") cat "$STUB_STATE/root-gid" ;;
            "-nG anheart") cat "$STUB_STATE/account-groups" ;;
            *) exit 1 ;;
        esac ;;
    dpkg) echo arm64 ;;
    dpkg-query)
        case "$*" in
            *Status-Status*)
                if has packages; then printf installed; else printf not-installed; fi ;;
            *) printf '0.0-scripted' ;;
        esac ;;
    apt-get) if [ "$1" = install ]; then : > "$STUB_STATE/packages"; fi ;;
    getent)
        case "$1" in
            passwd)
                has account || exit 2
                printf 'anheart:x:%s:%s::/var/lib/anheart:%s\n' "$(cat "$STUB_STATE/account-uid")" \
                    "$(cat "$STUB_STATE/account-gid")" "$(cat "$STUB_STATE/account-shell")" ;;
            group)
                has device-gid || exit 2
                printf '%s:x:%s:\n' "$2" "$(cat "$STUB_STATE/device-gid")" ;;
        esac ;;
    useradd)
        : > "$STUB_STATE/account"
        groups="anheart"
        while [ "$#" -gt 1 ]; do
            case "$1" in
                --shell) printf '%s' "$2" > "$STUB_STATE/account-shell" ;;
                --groups) groups="$groups $2" ;;
            esac
            shift
        done
        printf '%s\n' "$groups" > "$STUB_STATE/account-groups" ;;
    usermod)
        while [ "$#" -gt 1 ]; do
            case "$1" in
                --shell) printf '%s' "$2" > "$STUB_STATE/account-shell" ;;
                --groups)
                    printf '%s %s\n' "$(cat "$STUB_STATE/account-groups")" "$2" \
                        > "$STUB_STATE/account-groups" ;;
            esac
            shift
        done ;;
    passwd)
        case "$1" in
            --status)
                printf 'anheart %s 10/10/2026 0 99999 7 -1\n' \
                    "$(cat "$STUB_STATE/account-password")" ;;
            --lock) printf L > "$STUB_STATE/account-password" ;;
        esac ;;
    # chown records what it was asked and changes nothing: no test runs as root.
    chown | sleep | journalctl | udevadm) ;;
    systemctl)
        case "$1" in
            # The console's unit, or another one named last (a timer of apt).
            is-active)
                case "$last" in
                    anheart | anheart.service) has active ;;
                    *) has "active.$last" ;;
                esac ;;
            is-enabled)
                case "$last" in
                    anheart.service) has enabled ;;
                    *) if has "masked.$last"; then echo masked; exit 1; fi; echo enabled ;;
                esac ;;
            mask) : > "$STUB_STATE/masked.$last" ;;
            unmask) rm -f "$STUB_STATE/masked.$last" ;;
            stop)
                case "$last" in
                    anheart.service) rm -f "$STUB_STATE/active" ;;
                    *) rm -f "$STUB_STATE/active.$last" ;;
                esac ;;
            disable)
                case "$*" in *anheart.service*)
                    rm -f "$STUB_STATE/active" "$STUB_STATE/enabled" ;;
                esac ;;
            # The unit first stops and removes any container named anheart.
            # Another unit restarted (the system journal) is not the console.
            start | restart)
                case "$*" in *anheart.service*)
                    rm -f "$STUB_STATE/foreign-console"; : > "$STUB_STATE/active" ;;
                esac ;;
            enable)
                case "$*" in
                    *anheart.service*) : > "$STUB_STATE/enabled" ;;
                    *chrony.service*) if has chrony-broken; then exit 1; fi ;;
                esac ;;
            status) echo "scripted status" ;;
        esac ;;
    docker)
        case "$1" in
            # `docker container inspect --format {{.State.Running}} anheart`
            container) has foreign-console || exit 1; printf true ;;
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
        has active || has foreign-console || exit 7
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
    "usermod",
    "passwd",
    "udevadm",
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

    @property
    def chrony_conf(self) -> Path:
        return self.root / "etc" / "chrony" / "conf.d" / "anheart.conf"

    @property
    def journald_conf(self) -> Path:
        return self.root / "etc" / "systemd" / "journald.conf.d" / "anheart.conf"

    @property
    def manager_drop_in(self) -> Path:
        """systemd's own settings, as written by the installer: the hardware watchdog."""
        return self.root / "etc" / "systemd" / "system.conf.d" / "10-anheart-watchdog.conf"

    @property
    def account_drop_in(self) -> Path:
        """The account the console runs under in its container, by number."""
        return self.unit.parent / "anheart.service.d" / "20-account.conf"

    @property
    def state_dir(self) -> Path:
        """Everything the console writes: ``/var/lib/anheart``."""
        return self.root / "var" / "lib" / "anheart"

    @property
    def bind_helper(self) -> Path:
        """What the unit runs on the machine before the console."""
        return self.root / "usr" / "local" / "lib" / "anheart" / "bind-bitalino"

    @property
    def device_rule(self) -> Path:
        return self.root / "etc" / "udev" / "rules.d" / "70-anheart-drive.rules"

    @property
    def apt_conf(self) -> Path:
        return self.root / "etc" / "apt" / "apt.conf.d" / "99-anheart-no-automatic-updates"

    def owned_by_the_account(self, *, owned: bool) -> None:
        """Choose whose the files of the scratch root are, as the scripts will see it.

        No test runs as root, so no file can really change hands. What the
        scripts compare a file's owner with is the numbers of the account
        ``anheart``, which ``id`` gives them: ``owned`` hands out the numbers
        these files really have, and otherwise numbers that nothing has.
        """
        real = self.root.stat()
        user, group = (real.st_uid, real.st_gid) if owned else (FOREIGN_ID, FOREIGN_ID)
        self.set("account-uid", f"{user}\n")
        self.set("account-gid", f"{group}\n")

    def set(self, name: str, text: str = "") -> None:
        """Script one answer of the stubs (see :data:`STUB`)."""
        (self.state / name).write_text(text, encoding="utf-8")

    def unset(self, name: str) -> None:
        (self.state / name).unlink(missing_ok=True)

    def install(self, *arguments: str) -> Outcome:
        """Run the real ``install.sh`` of this machine's tree. Each run records its own calls."""
        return self.run("install.sh", *arguments)

    def harden(self, *arguments: str) -> Outcome:
        """Run the real ``harden.sh`` of this machine's tree."""
        return self.run("harden.sh", *arguments)

    def uninstall(self) -> Outcome:
        """Run the real ``uninstall.sh`` of this machine's tree."""
        return self.run("uninstall.sh")

    def run(self, script: str, *arguments: str) -> Outcome:
        """One of the three scripts of ``scripts/``, for real, against the scratch root."""
        assert BASH is not None
        calls = self.state / "calls"
        calls.unlink(missing_ok=True)
        done = subprocess.run(  # noqa: S603  # fixed argv, no shell, scripted tools only
            [BASH, str(self.tree / "scripts" / script), *arguments],
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
    (tree / "scripts" / "pi").mkdir(parents=True)
    for source in (INSTALLER, HARDENER, UNINSTALLER, SERVICE_UNIT):
        shutil.copy(source, tree / "scripts" / source.name)
    shutil.copy(BIND_HELPER, tree / "scripts" / "pi" / BIND_HELPER.name)
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
    # The account, once useradd made it: its files are its own, it cannot log
    # in and has no password. What root owns is root's (the same real numbers:
    # nothing here can be given to anybody).
    made.owned_by_the_account(owned=True)
    real = root.stat()
    made.set("root-uid", f"{real.st_uid}\n")
    made.set("root-gid", f"{real.st_gid}\n")
    made.set("device-gid", f"{DEVICE_GID}\n")
    made.set("account-shell", "/usr/sbin/nologin")
    made.set("account-password", "L")
    made.set("account-groups", "anheart dialout\n")
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

    for package in ("docker.io", "bluez", "libusb-1.0-0", "socat"):
        assert first.called("apt-get install", package), package
    # An account that cannot log in, in the group of the serial devices.
    assert first.called(
        "useradd", "--system", "--shell /usr/sbin/nologin", "--groups dialout", "anheart"
    )

    # Everything the console writes is the account's alone. The files of this
    # scratch root already are its own; a tree that is not is further down.
    state = machine.state_dir
    for directory in (state, state / "data", state / "records", state / "data" / "records"):
        assert _mode(directory) == 0o700, directory
    assert "== Step: files" in first.output, "the installer runs the step that protects the files"

    assert _read(machine.unit) == _read(SERVICE_UNIT)
    # What the unit runs on the machine before the console is installed with it.
    assert _read(machine.bind_helper) == _read(BIND_HELPER)
    assert _mode(machine.bind_helper) == 0o755
    assert first.called("docker build", f"--tag anheart-console:{version}", str(machine.tree))
    assert first.called("systemctl enable anheart.service")
    assert first.called("systemctl start anheart.service")
    assert not first.called("systemctl restart", "anheart")
    # The start test: the console answered /healthz, and said it was at rest.
    assert first.called("curl", "/healthz")
    assert first.called("curl", "/api/status")
    assert "the console is at rest" in first.output

    # Nothing is started before there is an image and a unit to start.
    order = [
        call.split(" ", 2)[1] for call in first.calls if call.startswith(("docker ", "systemctl "))
    ]
    assert order.index("build") < order.index("start")
    # The machine is closed before the image is built: a build that fails
    # leaves it protected all the same.
    assert order.index("mask") < order.index("build")


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
    kept = (
        machine.env_file,
        machine.unit,
        machine.drop_in,
        machine.account_drop_in,
        machine.manager_drop_in,
        machine.bind_helper,
        machine.device_rule,
        machine.apt_conf,
    )
    before = {path: path.read_bytes() for path in kept}

    second = machine.install()
    assert second.exit_code == 0, second.output
    assert {path: path.read_bytes() for path in before} == before
    for never in (
        "apt-get",
        "useradd",
        "usermod",
        "passwd --lock",
        "chown",
        "udevadm",
        "systemctl mask",
        "systemctl stop",
        "systemctl start",
        "systemctl restart",
    ):
        assert not second.called(never), never
    assert "nothing changed: left alone" in second.output
    assert "harden.sh: nothing to change" in second.output
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
    for secret in (MACHINE_KEY, PAGE_TOKEN):
        assert secret not in done.output
        assert not any(secret in call for call in done.calls), "a secret on a command line"
    assert done.called("systemctl stop anheart.service")
    assert done.called("systemctl start anheart.service")
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
    for never in ("systemctl restart", "systemctl stop anheart", "systemctl start anheart"):
        assert not refused.called(never), never
    assert "does not say it is at rest: nothing is restarted" in refused.output


@pytest.mark.slow
@needs_bash
def test_a_console_at_rest_is_restarted_when_its_image_changed(machine: Machine) -> None:
    """Asked first, then stopped, and only then started on the new image.

    Stopped and started, not restarted in one go: what a console that still ran
    as root wrote while it stopped is given to the account in between, before
    the new console, which is no longer root, reads it.
    """
    assert machine.install().exit_code == 0
    machine.set("source", "0002")

    done = machine.install()
    assert done.exit_code == 0, done.output
    asked = [index for index, call in enumerate(done.calls) if "/api/status" in call]
    stopped = done.calls.index("systemctl stop anheart.service")
    started = done.calls.index("systemctl start anheart.service")
    assert asked, "the console was never asked whether it was at rest"
    assert any(index < stopped for index in asked)
    assert stopped < started
    assert not done.called("systemctl restart")
    assert "the console is at rest" in done.output
    # The files are looked at twice: before the build, and between the stop and the start.
    assert done.output.count("== Step: files") == 2
    looked = [index for index, call in enumerate(done.calls) if call == "id -u anheart"]
    assert any(stopped < index < started for index in looked)


@pytest.mark.slow
@needs_bash
@pytest.mark.parametrize("answer", [RUNNING, ""])
def test_a_console_the_service_did_not_start_is_asked_before_anything_is_touched(
    machine: Machine, answer: str
) -> None:
    """A container named ``anheart`` run another way: the unit would stop it at its start."""
    machine.set("foreign-console")
    machine.set("status", answer)

    refused = machine.install()
    assert refused.exit_code == 1
    assert refused.called("docker container inspect", "anheart")
    assert "does not say it is at rest: nothing is restarted" in refused.output
    assert "sudo docker stop -t 60 anheart" in refused.output, "how to stop it on purpose"
    for never in ("apt-get", "useradd", "docker build", "systemctl start", "systemctl restart"):
        assert not refused.called(never), never
    assert not machine.unit.exists()
    assert not machine.env_file.exists()


@pytest.mark.slow
@needs_bash
def test_a_console_the_service_did_not_start_is_replaced_only_at_rest(machine: Machine) -> None:
    """At rest it is asked twice: before anything, and again right before the unit starts."""
    machine.set("foreign-console")

    done = machine.install()
    assert done.exit_code == 0, done.output
    names = [call.split(" ", 2)[:2] for call in done.calls]
    asked = [index for index, call in enumerate(done.calls) if "/api/status" in call]
    built = names.index(["docker", "build"])
    started = names.index(["systemctl", "start"])
    assert asked[0] < names.index(["dpkg-query", "--show"]), "asked before anything is installed"
    assert any(built < index < started for index in asked), "asked again after the build"
    assert not done.called("systemctl restart", "anheart")
    assert "the console is at rest" in done.output


@pytest.mark.slow
@needs_bash
def test_a_console_the_service_did_not_start_is_left_alone_if_a_session_began_meanwhile(
    machine: Machine,
) -> None:
    machine.set("foreign-console")
    machine.set("status-after-build", RUNNING)

    refused = machine.install()
    assert refused.exit_code == 1
    assert refused.called("docker build")
    assert not refused.called("systemctl start")
    # The system journal was restarted on this first run; the console never is.
    assert not refused.called("systemctl restart", "anheart")
    assert "does not say it is at rest: nothing is restarted" in refused.output


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


# =========================================================================
# ANH-163: the time service and the system journal
# =========================================================================


def _directives(path: Path) -> list[str]:
    """The lines of a configuration file that are not comments or blank."""
    return [
        line.strip()
        for line in _read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


@pytest.mark.slow
@needs_bash
def test_anh163_ex1_the_install_sets_up_the_time_service(machine: Machine) -> None:
    """chrony installed, given the two directives the console's reading relies on, started."""
    first = machine.install()
    assert first.exit_code == 0, first.output
    assert first.called("apt-get install", "chrony")
    assert _directives(machine.chrony_conf) == ["makestep 1 3", "rtcsync"]
    assert _mode(machine.chrony_conf) == 0o644
    assert first.called("systemctl try-restart chrony.service"), "a new file is read at a start"
    assert first.called("systemctl enable --now chrony.service")
    order = [call for call in first.calls if call.startswith(("apt-get install", "systemctl"))]
    assert order.index("systemctl enable --now chrony.service") > next(
        index for index, call in enumerate(order) if call.startswith("apt-get install")
    ), "the time service is started once its package is there"

    second = machine.install()
    assert second.exit_code == 0, second.output
    assert not second.called("systemctl try-restart"), "an unchanged file restarts nothing"
    assert second.called("systemctl enable --now chrony.service")
    assert f"{machine.chrony_conf} unchanged" in second.output


@pytest.mark.slow
@needs_bash
def test_anh163_ex1_a_real_time_clock_is_reported_when_the_machine_has_one(
    machine: Machine,
) -> None:
    """Without one (a Pi as delivered) the install says what that means; with one, it names it."""
    without = machine.install()
    assert without.exit_code == 0, without.output
    assert "no real-time clock on this machine" in without.output
    assert "the console says so" in without.output

    clock = machine.root / "sys" / "class" / "rtc" / "rtc0"
    clock.mkdir(parents=True)
    (clock / "name").write_text("rtc-ds1307 1-0068\n", encoding="utf-8")
    found = machine.install()
    assert found.exit_code == 0, found.output
    assert "real-time clock found: rtc0 (rtc-ds1307 1-0068)" in found.output
    assert "no real-time clock" not in found.output
    assert "rtcsync" in _directives(machine.chrony_conf), "what keeps that clock on time"


@pytest.mark.slow
@needs_bash
def test_anh163_ex1_a_time_service_that_does_not_start_does_not_stop_the_install(
    machine: Machine,
) -> None:
    """The console is installed all the same, and will say its clock is not trusted."""
    machine.set("chrony-broken")
    done = machine.install()
    assert done.exit_code == 0, done.output
    assert "WARNING: the time service (chrony) did not start" in done.output
    assert "the console will say its clock is not trusted" in done.output
    assert done.called("systemctl start anheart.service")


@pytest.mark.slow
@needs_bash
def test_anh163_ex2_the_install_keeps_the_journal_on_disk_and_bounds_it(
    machine: Machine,
) -> None:
    """``Storage=persistent`` and ``SystemMaxUse=200M``, read by journald at its restart."""
    first = machine.install()
    assert first.exit_code == 0, first.output
    assert _directives(machine.journald_conf) == [
        "[Journal]",
        "Storage=persistent",
        "SystemMaxUse=200M",
    ]
    assert _mode(machine.journald_conf) == 0o644
    assert first.called("systemctl restart systemd-journald.service")
    assert first.called("journalctl --flush")

    second = machine.install()
    assert second.exit_code == 0, second.output
    assert not second.called("systemctl restart"), "an unchanged file restarts nothing"
    assert not second.called("journalctl --flush")
    assert f"{machine.journald_conf} unchanged" in second.output


@pytest.mark.slow
@needs_bash
def test_anh163_a_change_of_the_two_system_files_never_restarts_the_console(
    machine: Machine,
) -> None:
    """A machine installed before this script set them: its console is left running."""
    assert machine.install().exit_code == 0
    machine.chrony_conf.unlink()
    machine.journald_conf.write_text("[Journal]\nStorage=volatile\n", encoding="utf-8")

    done = machine.install()
    assert done.exit_code == 0, done.output
    assert done.called("systemctl try-restart chrony.service")
    assert done.called("systemctl restart systemd-journald.service")
    assert not done.called("systemctl restart", "anheart")
    assert not done.called("systemctl start", "anheart")
    assert "nothing changed: left alone" in done.output
    assert _directives(machine.journald_conf)[1:] == ["Storage=persistent", "SystemMaxUse=200M"]


@pytest.mark.slow
@needs_bash
def test_anh163_nothing_of_the_system_is_touched_under_a_console_that_is_not_at_rest(
    machine: Machine,
) -> None:
    """The refusal comes first: neither file is written, neither service restarted."""
    assert machine.install().exit_code == 0
    machine.chrony_conf.unlink()
    machine.journald_conf.unlink()
    machine.set("status", RUNNING)

    refused = machine.install()
    assert refused.exit_code == 1
    assert not machine.chrony_conf.exists()
    assert not machine.journald_conf.exists()
    assert not refused.called("systemctl restart")
    assert not refused.called("systemctl try-restart")


def test_anh163_the_end_to_end_test_checks_the_clock_and_the_journal_in_a_real_system() -> None:
    """What the stubs above cannot show is asked of a real systemd, by the CI's install test."""
    script = _read(END_TO_END)
    for needed in (
        "/etc/chrony/conf.d/anheart.conf",
        "systemctl is-enabled chrony",
        "/etc/systemd/journald.conf.d/anheart.conf",
        "/var/log/journal",
        "max 200.0M",
        "timedatectl show --property=NTPSynchronized",
        '"wall_clock":{"state":"',
    ):
        assert needed in script, needed


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
    # Before `docker run`, only the shell line that starts the watchdog relay and
    # then becomes the command it was given (see the tests of the relay below).
    assert words[:2] == ["/bin/sh", "-c"]
    run = words.index("/usr/bin/docker")
    assert words[run - 1] == "anheart"
    assert words[run : run + 3] == ["/usr/bin/docker", "run", "--rm"]
    assert words.count("/usr/bin/docker") == 1
    # The image is the last word: nothing replaces the command the image runs,
    # which tests/test_legacy_recorder_retired.py holds to the console.
    assert words[-1] == "${ANHEART_IMAGE}"
    assert "--entrypoint" not in words


def test_the_unit_never_fetches_an_image() -> None:
    """An image that is not on the machine fails the start at once: nothing is pulled."""
    words = _directive("ExecStart")[0].split()
    assert ("--pull", "never") in set(itertools.pairwise(words))
    assert words.index("--pull") < words.index("${ANHEART_IMAGE}")
    # The image the unit names when no version was installed exists nowhere,
    # and neither does the account: a unit installed by hand does not start.
    assert _directive("Environment") == [
        "ANHEART_IMAGE=anheart-console:not-installed",
        "ANHEART_ACCOUNT=not-installed",
        "ANHEART_GROUPS=",
    ]


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
    # The controlled stop first; then the line waits for the Docker client (see
    # test_anh151_ex4_the_stop_waits_for_the_docker_client_to_end_by_itself).
    assert _stop_line().startswith("/usr/bin/docker stop -t 60 anheart; ")
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
    around = (*_directive("ExecStartPre"), *_directive("ExecStopPost"))
    touching = [step for step in around if "docker" in step]
    assert len(touching) == 2, "a step before the start and one after the stop find a console"
    for step in touching:
        assert "/usr/bin/docker stop -t 60 anheart" in step
    # The one other step binds the sensor's serial node: it never talks to Docker.
    assert [step for step in around if step not in touching] == [
        "-/usr/local/lib/anheart/bind-bitalino"
    ]


# =========================================================================
# ANH-162: the watchdog of the unit, the restart policy, the hardware watchdog
# =========================================================================


def test_ex1_the_unit_must_hear_the_console_every_ten_seconds_once_it_said_ready() -> None:
    """``Type=notify``: the watchdog counts from the console's own "ready", never from
    the instant the image starts, which on a cold SD card is long before the first tick."""
    assert _directive("Type") == ["notify"]
    assert _directive("WatchdogSec") == ["10"]
    assert int(_directive("TimeoutStartSec")[0]) >= 180, "the installer itself waits 180 s"
    installer = _read(INSTALLER)
    assert 'readonly HEALTH_TIMEOUT_S="${ANHEART_HEALTH_TIMEOUT_S:-180}"' in installer


def test_ex1_the_watchdog_asks_for_the_controlled_stop_and_the_unit_never_kills_a_console() -> None:
    """A console whose loop is stuck is stopped like any other: SIGTERM first. The one
    that answers nothing is ended by the unit's usual ``docker stop``, which escalates."""
    assert _directive("WatchdogSignal") == ["SIGTERM"]
    abort = int(_directive("TimeoutAbortSec")[0])
    assert 0 < abort < int(_directive("TimeoutStopSec")[0])
    # What follows the watchdog is ExecStopPost, never ExecStop: it does the stop itself.
    assert "/usr/bin/docker stop -t 60 anheart" in _directive("ExecStopPost")[0]


def _relay() -> tuple[str, list[str]]:
    """The shell line of ``ExecStart`` as ``sh`` receives it: its script, then its arguments."""
    started = re.fullmatch(r"/bin/sh -c '([^']*)' (.*)", _directive("ExecStart")[0])
    assert started is not None, "ExecStart is no longer `/bin/sh -c '<relay>' <command>`"
    # systemd turns `$$` into `$` before the shell sees the line, and reads a
    # quoted argument as one word, the way a shell does.
    return started.group(1).replace("$$", "$"), shlex.split(started.group(2))


def test_ex1_the_unit_hands_the_console_a_socket_that_a_process_of_the_unit_listens_on() -> None:
    """systemd only hears the processes of the unit; the console is a process of the
    container. The relay is one of the unit's, and the console is given its socket."""
    assert _directive("NotifyAccess") == ["all"], "the relay is not the unit's main process"
    assert _directive("RuntimeDirectory") == ["anheart"]
    script, command = _relay()
    assert script == (
        "/usr/bin/install -d -m 0700 -o anheart -g anheart /run/anheart/relay || exit 1; "
        "/usr/bin/setpriv --reuid=anheart --regid=anheart --clear-groups "
        "/usr/bin/socat -u UNIX-RECV:/run/anheart/relay/notify,unlink-early "
        'UNIX-SENDTO:"$NOTIFY_SOCKET" & exec "$@"'
    )
    pairs = set(itertools.pairwise(command))
    # The console is handed the relay's directory, where it finds the socket.
    assert ("--volume", "/run/anheart/relay:/run/anheart") in pairs
    assert ("--env", "NOTIFY_SOCKET=/run/anheart/notify") in pairs
    # A start with no relay would never be heard: it is refused at once instead.
    assert "/usr/bin/socat" in _directive("AssertFileIsExecutable")
    packages = _captures(r"^readonly PACKAGES=\((.*)\)$", _read(INSTALLER))
    assert "socat" in packages[0].split()


RELAY_STUB: Final[str] = """#!/bin/sh
printf '%s\\n' "$@" > "$RELAY_SEEN"
"""
DOCKER_STUB: Final[str] = """#!/bin/sh
printf '%s\\n' "$0" "$@" > "$DOCKER_SEEN"
exit 7
"""
#: ``install``, as the start line calls it to make the relay's directory: it says what it
#: was asked, and fails when told to.
INSTALL_STUB: Final[str] = """#!/bin/sh
printf '%s\\n' "$@" > "$INSTALL_SEEN"
exit "${INSTALL_EXIT:-0}"
"""
#: ``setpriv``: it says which account it was asked for, then becomes the command.
SETPRIV_STUB: Final[str] = """#!/bin/sh
while [ "$#" -gt 0 ]; do
    case "$1" in
        --*) printf '%s\\n' "$1" >> "$SETPRIV_SEEN.part"; shift ;;
        *) break ;;
    esac
done
mv "$SETPRIV_SEEN.part" "$SETPRIV_SEEN"
exec "$@"
"""


@dataclass(frozen=True, slots=True)
class StartLine:
    """What one run of the unit's start line did, with every tool it names scripted."""

    exit_code: int
    directory: Path
    arguments: tuple[str, ...]
    docker: Path

    def seen(self, name: str) -> list[str] | None:
        """What the scripted tool ``name`` was given, one argument a line; ``None``: never run."""
        record = self.directory / f"{name}-seen"
        for _ in range(100):
            if record.exists() and _read(record).endswith("\n"):
                return _read(record).splitlines()
            time.sleep(0.02)
        return None


def _run_start_line(directory: Path, *, install_exit: int = 0) -> StartLine:
    """The shell line of ``ExecStart``, run for real: the relay, ``install``, ``setpriv`` and
    ``docker`` are scripts of ``directory`` that record what they were asked."""
    assert SH is not None
    script, command = _relay()
    tools = {
        "/usr/bin/socat": ("relay", RELAY_STUB),
        "/usr/bin/docker": ("docker", DOCKER_STUB),
        "/usr/bin/install": ("install", INSTALL_STUB),
        "/usr/bin/setpriv": ("setpriv", SETPRIV_STUB),
    }
    for name, content in tools.values():
        tool = directory / name
        tool.write_text(content, encoding="utf-8")
        tool.chmod(0o755)
    for real, (name, _) in tools.items():
        if real != "/usr/bin/docker":
            assert script.count(real) == 1, real
            script = script.replace(real, str(directory / name))
    assert command[:2] == ["anheart", "/usr/bin/docker"]
    arguments = tuple(command[2:])
    done = subprocess.run(  # noqa: S603  # fixed argv, scripted tools only
        [SH, "-c", script, "anheart", str(directory / "docker"), *arguments],
        env={
            "PATH": os.environ["PATH"],
            "NOTIFY_SOCKET": "/run/systemd/notify",
            "RELAY_SEEN": str(directory / "relay-seen"),
            "DOCKER_SEEN": str(directory / "docker-seen"),
            "INSTALL_SEEN": str(directory / "install-seen"),
            "SETPRIV_SEEN": str(directory / "setpriv-seen"),
            "INSTALL_EXIT": str(install_exit),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return StartLine(done.returncode, directory, arguments, directory / "docker")


@needs_sh
def test_ex1_the_unit_starts_the_relay_then_becomes_the_command_it_was_given(
    tmp_path: Path,
) -> None:
    """The shell line of ``ExecStart``, run for real with a scripted relay and a scripted
    ``docker``: the relay is started on systemd's own socket, and the shell is replaced by
    ``docker run`` with every argument and with its exit code."""
    line = _run_start_line(tmp_path)
    assert line.exit_code == 7, "the unit's main process ends with the console's exit code"
    seen = line.seen("docker")
    assert seen is not None
    assert seen == [str(line.docker), *line.arguments], "every argument reaches docker, in order"
    assert seen[-1] == "${ANHEART_IMAGE}"
    # An argument systemd reads as one word reaches docker as one word.
    assert "c 188:* rw" in seen
    assert line.seen("relay") == [
        "-u",
        "UNIX-RECV:/run/anheart/relay/notify,unlink-early",
        "UNIX-SENDTO:/run/systemd/notify",
    ]


def test_ex2_a_failure_is_followed_by_a_restart_two_seconds_later_within_a_limit() -> None:
    assert _directive("Restart") == ["on-failure"]
    assert _directive("RestartSec") == ["2"]
    assert _directive("RestartPreventExitStatus") == ["2"], "a refused configuration never loops"
    burst = int(_directive("StartLimitBurst")[0])
    interval = int(_directive("StartLimitIntervalSec")[0])
    assert burst == 10
    # The slowest loop is the frozen console: 10 s unheard, the abort timeout, then
    # the 60 s of the unit's stop. The limit must still be reached inside its interval.
    slowest = int(_directive("WatchdogSec")[0]) + int(_directive("TimeoutAbortSec")[0]) + 60
    assert (burst + 1) * (slowest + int(_directive("RestartSec")[0]) + 20) < interval
    # The console puts itself out of service long before systemd gives up on it.
    assert burst > THRESHOLD
    assert interval >= WINDOW


@pytest.mark.slow
@needs_bash
def test_ex1_the_installer_has_systemd_arm_the_hardware_watchdog_and_restarts_nothing_for_it(
    machine: Machine,
) -> None:
    first = machine.install()
    assert first.exit_code == 0, first.output
    written = _read(machine.manager_drop_in)
    assert "[Manager]\nRuntimeWatchdogSec=15s\n" in written
    assert _mode(machine.manager_drop_in) == 0o644
    assert "no /dev/watchdog on this machine" in first.output, "a machine without one is told"
    # The file is in place before systemd is asked to read its configuration again.
    reload = next(index for index, call in enumerate(first.calls) if "daemon-reload" in call)
    start = next(index for index, call in enumerate(first.calls) if "start anheart" in call)
    assert reload < start
    # A console already running is not restarted because of this file alone.
    machine.manager_drop_in.unlink()
    again = machine.install()
    assert again.exit_code == 0, again.output
    assert machine.manager_drop_in.exists()
    assert not again.called("systemctl restart")
    assert "nothing changed: left alone" in again.output
    # With the device there, nothing is warned about.
    (machine.root / "dev").mkdir()
    (machine.root / "dev" / "watchdog").write_text("", encoding="utf-8")
    third = machine.install()
    assert third.exit_code == 0, third.output
    assert "/dev/watchdog is present" in third.output
    assert "no /dev/watchdog" not in third.output
    assert f"{machine.manager_drop_in} unchanged" in third.output


def test_the_unit_leaves_the_image_health_check_on_healthz_in_place() -> None:
    """EX-4: the health check is the image's, on ``/healthz``; the unit does not replace it."""
    unit = "\n".join(_unit_lines())
    assert "--no-healthcheck" not in unit
    assert "--health-" not in unit
    dockerfile = _read(DOCKERFILE)
    assert re.search(
        r"^HEALTHCHECK .*\n\s+CMD python -m src.health_probe$", dockerfile, re.MULTILINE
    )


# =========================================================================
# The version the session records are stamped with
# =========================================================================

UNSET: Final[str] = "<unset>"
RETIRED_KEY: Final[str] = "ANHEART_SOFTWARE_VERSION"
"""The key the image used to hand the console its version through. Nothing reads it now."""


def _handed(directory: Path, *, built: str | None, configured: str | None = None) -> str:
    """What the entrypoint, run in ``directory``, leaves in the console's environment
    under the retired key.

    ``built`` is the content of the ``VERSION`` file there (``None``: no file),
    ``configured`` what the machine's configuration sets (``None``: nothing).
    """
    assert SH is not None
    if built is not None:
        (directory / "VERSION").write_text(built, encoding="utf-8")
    environment = {"PATH": os.environ["PATH"]}
    if configured is not None:
        environment[RETIRED_KEY] = configured
    done = subprocess.run(  # noqa: S603  # fixed argv, the entrypoint of this repository
        [SH, str(ENTRYPOINT), SH, "-c", f'printf %s "${{{RETIRED_KEY}-{UNSET}}}"'],
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_ex5_the_records_of_an_installed_pi_carry_the_version_the_image_was_built_from() -> None:
    """The console reads the image's ``VERSION`` itself: the page, the heartbeat, the manifest.

    Before, the entrypoint handed it over through the environment, for the
    manifest alone, and a console started any other way wrote ``unversioned``.
    """
    version = _version()
    assert read_software_version() == version
    assert version != UNKNOWN_SOFTWARE_VERSION
    # The end-to-end install still requires it of a real record on a real machine.
    assert r"\"software_version\":\"$version\"" in _read(END_TO_END)


@needs_sh
@pytest.mark.parametrize("built", ["pi-1.4.2\n", "$(id)\n", None])
def test_ex5_the_entrypoint_hands_the_console_no_version(tmp_path: Path, built: str | None) -> None:
    """Whatever ``VERSION`` holds, the entrypoint neither reads it nor exports anything of it."""
    handed = _handed(tmp_path, built=built)
    assert handed == UNSET
    # What the machine's configuration sets is passed on untouched, and ignored by the console.
    passed_on = _handed(tmp_path, built=built, configured="set-by-hand")
    assert passed_on == "set-by-hand"
    entrypoint = _read(ENTRYPOINT)
    assert RETIRED_KEY not in entrypoint
    assert "cat VERSION" not in entrypoint
    assert "export " not in entrypoint


def test_ex5_no_example_configuration_offers_the_retired_key() -> None:
    for template in (ENV_TEMPLATE, CONSOLE / ".env.example"):
        assert RETIRED_KEY not in _read(template), template.name
    unit = "\n".join(_unit_lines())
    assert RETIRED_KEY not in unit
    assert RETIRED_KEY not in _read(INSTALLER)
    assert RETIRED_KEY not in _read(DOCKERFILE)


def test_the_console_finds_the_version_file_where_the_image_puts_it() -> None:
    """``VERSION`` is read next to ``src/``, wherever the console is started from."""
    assert VERSION_PATH == VERSION_FILE
    dockerfile = _read(DOCKERFILE)
    workdir = dockerfile.index("\nWORKDIR /app\n")
    assert len(_captures(r"^(WORKDIR) ", dockerfile)) == 1
    # Both copied under the one working directory: /app/src and /app/VERSION.
    assert workdir < dockerfile.index("\nCOPY src/ ./src/\n")
    assert workdir < dockerfile.index("\nCOPY VERSION ./VERSION\n")


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
        # The record is stamped with the version; the service stopped during a
        # session; a console the service did not start.
        r"\"software_version\":\"$version\"",
        "in_machine systemctl stop anheart",
        "Result=success",
        "shutdown complete: emergency zero",
        '"end_reason":"shutdown"',
        '--name anheart --network host --env-file "$ENV_FILE"',
        "{{.HostConfig.AutoRemove}}",
        "docker stop -t 120",
        # ANH-162: the watchdog fed by a healthy console, a frozen console
        # restarted by it, the restart loop and the way out of it.
        "WatchdogUSec=10s",
        "kill -STOP $console_pid",
        "Watchdog timeout (limit 10s)",
        "not a clean stop",
        "status=1/FAILURE",
        '"unclean_stop":true',
        "unclean_stop: demarrage apres arret non propre",
        '"out_of_service":true',
        "/api/service/restore",
        "RuntimeWatchdogSec=15s",
    ):
        assert proof in script, proof
    # The stand-in never arms the hardware watchdog of the machine that runs the test.
    assert 'CMD ["/lib/systemd/systemd", "systemd.watchdog_device=/dev/null"]' in script


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


# =========================================================================
# ANH-151, lot 1: the console without the privileges it does not need
# =========================================================================
#
# Two halves. What systemd runs (the relay, the Docker client) and what runs
# in the container (the console). Each reduction is pinned here on the unit,
# the image and the scripts, and proved in a real system by the end-to-end
# test, whose proofs are listed at the end of this section.

DOCKERFILE_USER: Final[str] = "anheart"
#: The console's own /tmp, as the unit asks Docker for it: an option of
#: ``docker run``, never a path of the machine that runs these tests.
CONSOLE_TMP: Final[str] = "/tmp:rw,noexec,nosuid,nodev,size=64m"  # noqa: S108  # see above
HARDEN_STEPS: Final[tuple[str, ...]] = ("account", "files", "updates")
UPDATE_UNITS: Final[tuple[str, ...]] = (
    "apt-daily.timer",
    "apt-daily-upgrade.timer",
    "unattended-upgrades.service",
)


def _run_command() -> list[str]:
    """The arguments of the unit's ``docker run``, as docker receives them."""
    command = _relay()[1]
    assert command[:3] == ["anheart", "/usr/bin/docker", "run"]
    return command[3:]


def _options(name: str) -> list[str]:
    """Every value the unit gives ``docker run`` for the option ``name``."""
    command = _run_command()
    return [value for option, value in itertools.pairwise(command) if option == name]


def test_anh151_ex4_the_console_is_given_the_account_and_nothing_more() -> None:
    """In its container the console is the account, with no capability, on a read-only
    image; ``--privileged`` and the machine's D-Bus are gone."""
    command = _run_command()
    for gone in ("--privileged", "--cap-add", "--device", "--pid", "--ipc", "--userns"):
        assert gone not in command, gone
    assert not [word for word in command if "dbus" in word], "the machine's D-Bus is mounted"
    # The account and its group, as numbers the installer writes (see below).
    assert _options("--user") == ["${ANHEART_ACCOUNT}"]
    assert "$ANHEART_GROUPS" in command
    assert _options("--cap-drop") == ["ALL"]
    assert _options("--security-opt") == ["no-new-privileges"]
    assert "--read-only" in command
    # Memory, bounded, and nothing in it can be executed.
    assert _options("--tmpfs") == [CONSOLE_TMP]
    assert _options("--network") == ["host"]
    # Its data, the relay's socket, and the machine's device nodes: nothing else is mounted.
    assert _options("--volume") == [
        "/dev:/dev",
        "/var/lib/anheart/data:/app/data",
        "/var/lib/anheart/records:/app/data/records",
        "/var/lib/anheart/tls:/run/anheart-tls:ro",
        "/run/anheart/relay:/run/anheart",
    ]


def test_anh151_ex4_the_console_may_open_three_classes_of_devices_and_no_other() -> None:
    """The USB serial adapters, the USB devices themselves, the Bluetooth serial nodes."""
    assert _options("--device-cgroup-rule") == ["c 188:* rw", "c 189:* rw", "c 216:* rw"]
    # The Schneider cable has no serial driver in the kernel: a rule of the
    # machine gives that USB device to the group the console has.
    installer = _read(INSTALLER)
    assert (
        'SUBSYSTEM=="usb", ATTR{idVendor}=="16de", ATTR{idProduct}=="0003", '
        'GROUP="$DEVICE_GROUP", MODE="0660"'
    ) in installer
    assert 'readonly DEVICE_GROUP="dialout"' in installer


@pytest.mark.slow
@needs_bash
def test_anh151_ex4_the_installer_tells_the_unit_the_account_by_number(machine: Machine) -> None:
    """Docker looks a name up in the image, which does not know this machine's accounts."""
    first = machine.install()
    assert first.exit_code == 0, first.output
    real = machine.root.stat()
    assert _directives(machine.account_drop_in) == [
        "[Service]",
        f"Environment=ANHEART_ACCOUNT={real.st_uid}:{real.st_gid}",
        f'Environment="ANHEART_GROUPS=--group-add {DEVICE_GID}"',
    ]
    # Another number for the group of the serial devices: the unit is told, and
    # the console, at rest, is started again to have it.
    machine.set("device-gid", "113\n")
    again = machine.install()
    assert again.exit_code == 0, again.output
    assert 'Environment="ANHEART_GROUPS=--group-add 113"' in _read(machine.account_drop_in)
    assert again.called("systemctl stop anheart.service")
    assert again.called("systemctl start anheart.service")
    # No such group: nothing is written that would start a console without it.
    machine.unset("device-gid")
    before = machine.account_drop_in.read_bytes()
    refused = machine.install()
    assert refused.exit_code == 1
    assert "the group dialout is missing" in refused.output
    assert machine.account_drop_in.read_bytes() == before
    assert not refused.called("systemctl start anheart.service")


@pytest.mark.slow
@needs_bash
@pytest.mark.parametrize("number", ["account-uid", "account-gid"])
def test_anh151_ex4_an_account_with_a_number_of_roots_is_refused_before_anything_is_built(
    machine: Machine, number: str
) -> None:
    """The unit runs the console under the numbers of the account: were they root's, the
    console would be root, with everything else in place. Nothing is built or started."""
    machine.set(number, "0\n")
    refused = machine.install()
    assert refused.exit_code == 1
    assert "the account anheart has a number of root's" in refused.output
    for never in ("docker build", "systemctl start", "chown", "systemctl mask"):
        assert not refused.called(never), never
    assert not machine.unit.exists()
    assert not machine.account_drop_in.exists()
    # The step that gives the files away refuses on its own too.
    alone = machine.harden("files")
    assert alone.exit_code == 1
    assert "the account anheart has a number of root's" in alone.output
    assert not alone.called("chown")


@needs_sh
def test_anh151_ex4_the_relay_runs_as_the_account_in_a_directory_given_to_it(
    tmp_path: Path,
) -> None:
    """The relay reads what the console sends: it is the account, not root. It listens in
    a directory of its own, made for the account alone before anything else.

    Not ``/run/anheart`` itself: systemd takes that one back, as root's, every time it runs
    a line of the unit, the line of the stop included, and a console that stops could then
    no longer say so. What systemd finds as it expects it, it does not look into."""
    assert _directive("RuntimeDirectory") == ["anheart"]
    assert _directive("RuntimeDirectoryMode") == ["0755"]
    assert sorted(_directive("AssertFileIsExecutable")) == ["/usr/bin/setpriv", "/usr/bin/socat"]
    line = _run_start_line(tmp_path)
    assert line.exit_code == 7
    assert line.seen("install") == [
        "-d",
        "-m",
        "0700",
        "-o",
        "anheart",
        "-g",
        "anheart",
        "/run/anheart/relay",
    ]
    assert line.seen("setpriv") == ["--reuid=anheart", "--regid=anheart", "--clear-groups"]
    # What setpriv becomes is the relay, with the arguments the unit gave it.
    relayed = line.seen("relay")
    assert relayed is not None
    assert relayed[1:] == [
        "UNIX-RECV:/run/anheart/relay/notify,unlink-early",
        "UNIX-SENDTO:/run/systemd/notify",
    ]


@needs_sh
def test_anh151_ex4_a_start_that_cannot_give_the_relay_its_directory_fails_at_once(
    tmp_path: Path,
) -> None:
    """A relay that cannot listen is a console nobody hears: no console is started at all."""
    line = _run_start_line(tmp_path, install_exit=1)
    assert line.exit_code == 1
    assert line.seen("install") is not None
    assert line.seen("docker") is None, "the console was started with no relay to hear it"
    assert line.seen("relay") is None


def _stop_line() -> str:
    """The shell line of ``ExecStop`` as ``sh`` receives it."""
    stopped = re.fullmatch(r"/bin/sh -c '([^']*)'", _directive("ExecStop")[0])
    assert stopped is not None, "ExecStop is no longer `/bin/sh -c '<stop>'`"
    return stopped.group(1).replace("$$", "$")


STOP_STUB: Final[str] = """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_SEEN"
exit "${DOCKER_EXIT:-0}"
"""


def _alive(process: int) -> bool:
    try:
        os.kill(process, 0)
    except ProcessLookupError:
        return False
    return True


@needs_sh
@pytest.mark.parametrize("stop_exit", [0, 1])
def test_anh151_ex4_the_stop_waits_for_the_docker_client_to_end_by_itself(
    tmp_path: Path, stop_exit: int
) -> None:
    """``docker stop`` returns when the console is gone; the unit's main process, the Docker
    client, ends a moment later with the console's exit code. systemd ends what is left of
    the unit as soon as the stop line returns: the line waits for that client, so that what
    systemd reads is the console's code and not the signal it would have ended the client
    with. The line itself still ends with what ``docker stop`` said."""
    assert SH is not None
    docker = tmp_path / "docker"
    docker.write_text(STOP_STUB, encoding="utf-8")
    docker.chmod(0o755)
    script = _stop_line()
    assert script.count("/usr/bin/docker") == 1
    script = script.replace("/usr/bin/docker", str(docker))
    environment = {
        "PATH": os.environ["PATH"],
        "DOCKER_SEEN": str(tmp_path / "docker-seen"),
        "DOCKER_EXIT": str(stop_exit),
    }
    # A main process that ends by itself a second later. A thread collects it the
    # moment it ends, as systemd does of the client: once gone, it is gone.
    with subprocess.Popen([SH, "-c", "exec sleep 1"]) as client:  # noqa: S603  # fixed argv
        collector = threading.Thread(target=client.wait, daemon=True)
        collector.start()
        assert _alive(client.pid)
        begun = time.monotonic()
        done = subprocess.run(  # noqa: S603  # fixed argv, a scripted docker
            [SH, "-c", script],
            env={**environment, "MAINPID": str(client.pid)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        waited = time.monotonic() - begun
        still_there = _alive(client.pid)
        collector.join(timeout=10)
    assert done.returncode == stop_exit, done.stderr
    assert _read(tmp_path / "docker-seen").splitlines() == ["stop -t 60 anheart"]
    assert not still_there, "the stop line returned while the Docker client was still there"
    assert waited > 0.5, "the stop line did not wait for the Docker client"

    # No main process left to wait for (it ended first): the line returns at once.
    begun = time.monotonic()
    again = subprocess.run(  # noqa: S603  # fixed argv, a scripted docker
        [SH, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert again.returncode == stop_exit
    assert time.monotonic() - begun < 5
    # Bounded: a client that never ends is not waited for longer than the unit's
    # stop allows after the 60 s of the ramp.
    bound = re.search(r'\[ "\$waited" -lt (\d+) \].*/bin/sleep (0\.\d+)', script)
    assert bound is not None
    longest = int(bound.group(1)) * float(bound.group(2))
    assert 60 + longest < int(_directive("TimeoutStopSec")[0])


def test_anh151_ex4_the_unit_says_what_stays_root_and_claims_nothing_else() -> None:
    """The Docker client is root because the daemon is root's. The unit's own processes are
    closed in, which binds the relay; nothing in the unit pretends the client is not root."""
    assert _directive("NoNewPrivileges") == ["yes"]
    assert _directive("ProtectSystem") == ["strict"]
    assert _directive("ProtectHome") == ["yes"]
    # The unit's processes write nowhere but /run/anheart: no path is opened to
    # them, and none is needed, because the daemon mounts the console's data.
    for absent in ("ReadWritePaths", "User", "Group", "DynamicUser", "CapabilityBoundingSet"):
        assert _directive(absent) == [], f"{absent}= would say the Docker client is not root"
    # Not narrower, and the reason is written next to it.
    assert _directive("NotifyAccess") == ["all"]
    text = _read(SERVICE_UNIT)
    assert "The Docker daemon is root's" in text
    assert "no option of this unit bounds that" in text


RFCOMM_STUB: Final[str] = """#!/bin/sh
printf '%s\\n' "$*" >> "$RFCOMM_SEEN"
if [ "$1" = bind ]; then exit "${RFCOMM_BIND_EXIT:-0}"; fi
exit 0
"""
SENSOR: Final[str] = "98:D3:91:FE:4E:9F"


@dataclass(frozen=True, slots=True)
class Bound:
    """What one run of the step that binds the sensor's serial node did."""

    exit_code: int
    output: str
    asked: tuple[str, ...]
    """What it asked of ``rfcomm``, one call a line."""


def _bind(directory: Path, configuration: str | None, *, bind_exit: int = 0) -> Bound:
    """``scripts/pi/bind-bitalino.sh``, run for real against a configuration and a
    scripted ``rfcomm``. ``configuration`` is the machine's file (``None``: no file)."""
    assert SH is not None
    tools = directory / "tools"
    tools.mkdir(exist_ok=True)
    tool = tools / "rfcomm"
    tool.write_text(RFCOMM_STUB, encoding="utf-8")
    tool.chmod(0o755)
    env_file = directory / "anheart.env"
    env_file.unlink(missing_ok=True)
    if configuration is not None:
        env_file.write_text(configuration, encoding="utf-8")
    seen = directory / "rfcomm-seen"
    seen.unlink(missing_ok=True)
    done = subprocess.run(  # noqa: S603  # fixed argv, the script of this repository
        [SH, str(BIND_HELPER)],
        env={
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "ANHEART_ENV_FILE": str(env_file),
            "RFCOMM_SEEN": str(seen),
            "RFCOMM_BIND_EXIT": str(bind_exit),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    asked = tuple(_read(seen).splitlines()) if seen.exists() else ()
    return Bound(done.returncode, done.stdout + done.stderr, asked)


@needs_sh
def test_anh151_ex4_the_sensor_serial_node_is_bound_on_the_machine_before_the_console(
    tmp_path: Path,
) -> None:
    """Binding the node takes a right over the network the console is not given: the
    service does it, as root, on the machine, with what the configuration says."""
    serial = f"ECG_SOURCE=serial\nBITALINO_ADDRESS=/dev/rfcomm0\nBITALINO_MAC={SENSOR}\n"
    bound = _bind(tmp_path, serial)
    assert bound.exit_code == 0
    # Released first: the address may have changed since the last start.
    assert bound.asked == ("release 0", f"bind 0 {SENSOR} 1")
    assert f"/dev/rfcomm0 is bound to the BITalino {SENSOR}" in bound.output

    # The unit runs it after it has stopped a console left behind, and before
    # its own; a failure of this step never prevents the start.
    before = _directive("ExecStartPre")
    assert len(before) == 2
    assert "/usr/bin/docker stop -t 60 anheart" in before[0]
    assert before[1] == "-/usr/local/lib/anheart/bind-bitalino"
    assert 'readonly BIND_HELPER="$ROOT/usr/local/lib/anheart/bind-bitalino"' in _read(INSTALLER)
    # The image no longer tries: it has no right to.
    entrypoint = [line.strip() for line in _read(ENTRYPOINT).splitlines()]
    assert not [line for line in entrypoint if line.startswith("rfcomm ")]


@needs_sh
@pytest.mark.parametrize(
    ("configuration", "said"),
    [
        pytest.param(None, "", id="no configuration"),
        pytest.param(f"ECG_SOURCE=sim\nBITALINO_MAC={SENSOR}\n", "", id="simulated sensor"),
        pytest.param(
            f"ECG_SOURCE=serial\nBITALINO_ADDRESS={SENSOR}\nBITALINO_MAC={SENSOR}\n",
            "",
            id="a Bluetooth address, not a node",
        ),
        pytest.param(
            "ECG_SOURCE=serial\nBITALINO_ADDRESS=COM4\n", "", id="a serial port of another kind"
        ),
        pytest.param(
            "ECG_SOURCE=serial\nBITALINO_ADDRESS=/dev/rfcomm0; reboot\n",
            "",
            id="a node name that is not one",
        ),
        pytest.param(
            "ECG_SOURCE=serial\nBITALINO_ADDRESS=/dev/rfcomm0\nBITALINO_MAC=\n",
            "BITALINO_MAC is not the address of a sensor",
            id="no address",
        ),
        pytest.param(
            "ECG_SOURCE=serial\nBITALINO_ADDRESS=/dev/rfcomm0\nBITALINO_MAC=$(reboot)\n",
            "BITALINO_MAC is not the address of a sensor",
            id="an address that is not one",
        ),
    ],
)
def test_anh151_ex4_nothing_is_bound_unless_the_configuration_names_a_node_and_a_sensor(
    tmp_path: Path, configuration: str | None, said: str
) -> None:
    """Whatever the configuration holds reaches ``rfcomm`` as an argument or not at all."""
    bound = _bind(tmp_path, configuration)
    assert bound.exit_code == 0, "this step never prevents the console from starting"
    assert bound.asked == ()
    assert said in bound.output


@needs_sh
def test_anh151_ex4_a_node_that_cannot_be_bound_does_not_prevent_the_start(tmp_path: Path) -> None:
    """No adapter, a sensor that was never paired: said, and the console starts anyway."""
    serial = (
        f"ECG_SOURCE=sim\nECG_SOURCE=serial\nBITALINO_ADDRESS=/dev/rfcomm3\nBITALINO_MAC={SENSOR}\n"
    )
    failed = _bind(tmp_path, serial, bind_exit=1)
    assert failed.exit_code == 0
    # The last value of a key is the one docker hands the console: the same one here.
    assert failed.asked == ("release 3", f"bind 3 {SENSOR} 1")
    assert f"/dev/rfcomm3 could not be bound to {SENSOR}" in failed.output


def test_anh151_ex4_the_image_never_runs_the_console_as_root() -> None:
    """Started by the service it is the machine's account, by number; started any other
    way it is the image's own account. What it reads is readable by either."""
    dockerfile = _read(DOCKERFILE).replace("\\\n", " ")
    users = _captures(r"^USER (\S+)$", dockerfile)
    assert users == [DOCKERFILE_USER]
    assert dockerfile.index(f"\nUSER {DOCKERFILE_USER}\n") < dockerfile.index("\nENTRYPOINT ")
    made = re.search(
        r"^RUN groupadd --gid (\d+) anheart .*useradd --uid (\d+) ", dockerfile, re.MULTILINE
    )
    assert made is not None
    assert int(made.group(1)) == int(made.group(2)) >= 1000, "not a number a system account has"
    assert "chmod -R a+rX /app/src /app/config /app/VERSION" in dockerfile
    assert "chown anheart:anheart /app/data" in dockerfile
    # No home directory, and an image that cannot be written: the one cache
    # the console's libraries keep goes to /tmp.
    assert "ENV MPLCONFIGDIR=/tmp/matplotlib" in dockerfile


@needs_sh
def test_anh151_ex6_every_file_the_console_creates_is_the_accounts_alone(tmp_path: Path) -> None:
    """The entrypoint closes the mask before it hands over: the local profiles, an archive
    being exported, the lock on the drive cable. The session records set their own modes."""
    assert SH is not None
    made = tmp_path / "made-by-the-console"
    done = subprocess.run(  # noqa: S603  # fixed argv, the entrypoint of this repository
        [SH, str(ENTRYPOINT), SH, "-c", f'umask; : > "{made}"'],
        env={
            "PATH": os.environ["PATH"],
            "ECG_SOURCE": "serial",
            "BITALINO_ADDRESS": "/dev/rfcomm-not-there",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() in {"0077", "077"}
    assert _mode(made) == 0o600
    # A sensor node that is not there is said, and the console is started all the same.
    assert "/dev/rfcomm-not-there is not there" in done.stderr


# --- EX-6: owners and modes, at creation and on an existing machine ---------------------


def _console_files(machine: Machine) -> dict[str, Path]:
    """What a console that ran leaves under ``/var/lib/anheart``, by what it is."""
    records = machine.state_dir / "records"
    session = records / "2026-10-10T101010Z_bench"
    return {
        "the local profiles": machine.state_dir / "data" / "profiles.local.json",
        "the restart state": records / "service-state.json",
        "the logbook": records / "logbook" / "events.jsonl",
        "a session record": session / "manifest.json",
        "a block of a session record": session / "ecg_raw" / "000001.bin",
    }


def _write_console_files(machine: Machine, *, directory: int, file: int) -> dict[str, bytes]:
    """Write them with these modes, as an older console did; what each holds."""
    held: dict[str, bytes] = {}
    for name, path in _console_files(machine).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"content of {name}\n".encode())
        path.chmod(file)
        held[name] = path.read_bytes()
    for path in _console_files(machine).values():
        for parent in path.relative_to(machine.state_dir).parents:
            (machine.state_dir / parent).chmod(directory)
    return held


@pytest.mark.slow
@needs_bash
def test_anh151_ex6_the_configuration_and_the_data_are_created_private(machine: Machine) -> None:
    """A new machine: the configuration is root's alone, the data the account's alone."""
    done = machine.install()
    assert done.exit_code == 0, done.output
    assert _mode(machine.env_file) == 0o600
    assert _mode(machine.env_file.parent) == 0o755
    for directory in ("", "data", "records", "data/records"):
        assert _mode(machine.state_dir / directory) == 0o700, directory
    # The configuration stays root's: the account that runs the console can
    # neither read the machine's key there nor rewrite the limits it applies.
    hardener = _read(HARDENER)
    assert 'owner_of root:root "$root_ids" "$ENV_FILE"' in hardener
    assert 'mode_of 0600 "$ENV_FILE"' in hardener


@pytest.mark.slow
@needs_bash
def test_anh151_ex6_an_existing_machine_is_corrected_and_no_content_is_touched(
    machine: Machine,
) -> None:
    """Records written with open modes by an older console, a configuration anybody can
    read: the installer closes each of them, and reads or writes none."""
    assert machine.install().exit_code == 0
    held = _write_console_files(machine, directory=0o755, file=0o644)
    machine.env_file.chmod(0o644)
    machine.env_file.parent.chmod(0o777)
    configuration = machine.env_file.read_bytes()

    done = machine.install()
    assert done.exit_code == 0, done.output
    for name, path in _console_files(machine).items():
        assert _mode(path) == 0o600, name
        assert path.read_bytes() == held[name], f"the content of {name} was touched"
        for parent in path.relative_to(machine.state_dir).parents:
            assert _mode(machine.state_dir / parent) == 0o700, f"{name}: {parent}"
    assert _mode(machine.env_file) == 0o600
    assert _mode(machine.env_file.parent) == 0o755
    assert machine.env_file.read_bytes() == configuration
    # The console that was running is not restarted for it.
    assert "nothing changed: left alone" in done.output
    for never in ("systemctl stop", "systemctl start", "systemctl restart"):
        assert not done.called(never), never


@pytest.mark.slow
@needs_bash
def test_anh151_ex6_what_the_owner_may_do_is_left_as_it_is(machine: Machine) -> None:
    """Only what is open to others is closed: a record made read-only stays read-only, and
    the three directories the installer makes get their whole mode back."""
    assert machine.install().exit_code == 0
    _write_console_files(machine, directory=0o755, file=0o644)
    sealed = _console_files(machine)["a session record"]
    sealed.chmod(0o444)
    (machine.state_dir / "records").chmod(0o500)

    done = machine.harden("files")
    assert done.exit_code == 0, done.output
    assert _mode(sealed) == 0o400
    assert _mode(machine.state_dir / "records") == 0o700


@pytest.mark.slow
@needs_bash
def test_anh151_ex6_what_is_not_the_accounts_is_given_to_it(machine: Machine) -> None:
    """A machine installed before the console stopped running as root: everything it wrote
    is root's. Each entry is named by a dry run and handed to ``chown`` by a run."""
    assert machine.install().exit_code == 0
    _write_console_files(machine, directory=0o700, file=0o600)
    machine.owned_by_the_account(owned=False)
    wanted = [machine.state_dir, *_console_files(machine).values()]

    dry = machine.harden("--dry-run", "files")
    assert dry.exit_code == 0, dry.output
    for path in wanted:
        assert f"   would give to anheart:anheart: {path}\n" in dry.output, path
    assert not dry.called("chown")

    # The scripted chown changes nothing. The run hands it every entry, a link
    # as a link, then looks again: what is still not the account's is said,
    # and the script ends in failure rather than say it protected the files.
    done = machine.harden("files")
    for path in wanted:
        assert done.called("chown -h anheart:anheart", str(path)), path
    assert done.exit_code == 1
    assert "WARNING: could not give what is under" in done.output
    assert "could not be made" in done.output


@pytest.mark.slow
@needs_bash
def test_anh151_ex6_a_link_in_the_data_is_never_followed(machine: Machine, tmp_path: Path) -> None:
    """The console's account can plant a link in its own directories: what root then does
    there never reaches what the link points to."""
    assert machine.install().exit_code == 0
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "a-file-of-the-system"
    victim.write_text("not the console's\n", encoding="utf-8")
    victim.chmod(0o644)
    outside.chmod(0o755)
    records = machine.state_dir / "records"
    (records / "to-a-file").symlink_to(victim)
    (records / "to-a-directory").symlink_to(outside, target_is_directory=True)

    done = machine.harden("files")
    assert done.exit_code == 0, done.output
    assert _mode(victim) == 0o644
    assert _mode(outside) == 0o755

    # Not the account's: the links themselves are named, never what is behind them.
    machine.owned_by_the_account(owned=False)
    dry = machine.harden("--dry-run", "files")
    assert f"would give to anheart:anheart: {records / 'to-a-directory'}\n" in dry.output
    assert str(victim) not in dry.output
    assert f"{records / 'to-a-directory'}/" not in dry.output
    # In the script itself: no chown that follows a link, no find that does.
    code = "\n".join(
        line for line in _read(HARDENER).splitlines() if not line.lstrip().startswith("#")
    )
    assert _captures(r"\bchown (\S+)", code) == ["-h", "-h"]
    assert set(_captures(r"\bfind (\S+)", code)) == {"-P", '"${look[@]}"'}
    assert code.count('-P "$tree"') == 2, "the two passes over a tree start with find -P"


# --- EX-7: no automatic update --------------------------------------------------------


@pytest.mark.slow
@needs_bash
def test_anh151_ex7_the_install_turns_off_every_automatic_package_update(
    machine: Machine,
) -> None:
    """apt's periodic jobs are off in the file apt reads last, and the three units that run
    them are masked: a package installed later cannot turn its own job back on."""
    machine.set("active.apt-daily.timer")
    first = machine.install()
    assert first.exit_code == 0, first.output
    assert [line for line in _directives(machine.apt_conf) if not line.startswith("//")] == [
        'APT::Periodic::Enable "0";',
        'APT::Periodic::Update-Package-Lists "0";',
        'APT::Periodic::Download-Upgradeable-Packages "0";',
        'APT::Periodic::Unattended-Upgrade "0";',
        'APT::Periodic::AutocleanInterval "0";',
    ]
    assert _mode(machine.apt_conf) == 0o644
    # apt reads the files of its directory in order: this one comes after the
    # distribution's (10periodic, 20auto-upgrades), and its name is one apt accepts.
    assert re.fullmatch(r"99[a-z0-9-]+", machine.apt_conf.name)
    for unit in UPDATE_UNITS:
        assert first.called(f"systemctl mask {unit}"), unit
    # A timer that was running is stopped; a job in progress is left to end.
    assert first.called("systemctl stop apt-daily.timer")
    assert not first.called("systemctl stop apt-daily.service")
    assert not first.called("apt-get remove")
    assert not first.called("apt-get purge")

    second = machine.install()
    assert second.exit_code == 0, second.output
    assert not second.called("systemctl mask")
    # Turned back on by hand, or by a package: the next install turns it off again.
    machine.unset("masked.unattended-upgrades.service")
    machine.apt_conf.write_text('APT::Periodic::Unattended-Upgrade "1";\n', encoding="utf-8")
    third = machine.install()
    assert third.exit_code == 0, third.output
    assert third.called("systemctl mask unattended-upgrades.service")
    assert 'APT::Periodic::Unattended-Upgrade "0";' in _read(machine.apt_conf)
    assert "nothing changed: left alone" in third.output, "the console is not restarted for it"


# --- EX-8: harden.sh, its dry run, its steps, and the uninstall ---------------------------


def _drift(machine: Machine) -> None:
    """Undo by hand, on an installed machine, one thing of each step."""
    machine.env_file.chmod(0o644)
    (machine.state_dir / "records").chmod(0o755)
    machine.set("account-shell", "/bin/bash")
    machine.set("account-password", "P")
    machine.set("account-groups", "anheart\n")
    machine.unset("masked.apt-daily.timer")
    machine.apt_conf.unlink()


def _snapshot(machine: Machine) -> dict[str, str]:
    """Everything a run of ``harden.sh`` could change: modes, and the scripted system."""
    seen = {
        str(path): oct(_mode(path))
        for path in (machine.env_file, machine.state_dir, *machine.state_dir.rglob("*"))
    }
    seen["apt"] = _read(machine.apt_conf) if machine.apt_conf.exists() else "<absent>"
    for state in sorted(machine.state.iterdir()):
        if state.name != "calls":
            seen[f"system:{state.name}"] = _read(state)
    return seen


@pytest.mark.slow
@needs_bash
def test_anh151_ex8_a_dry_run_says_exactly_what_a_run_would_do_and_does_nothing(
    machine: Machine,
) -> None:
    assert machine.install().exit_code == 0
    _drift(machine)
    before = _snapshot(machine)

    dry = machine.harden("--dry-run")
    assert dry.exit_code == 0, dry.output
    announced = [line.strip() for line in dry.output.splitlines() if line.startswith("   would ")]
    assert announced == [
        "would give the account anheart the shell /usr/sbin/nologin in place of /bin/bash",
        "would lock the password of the account anheart",
        "would add the account anheart to the group dialout",
        f"would set the mode of {machine.env_file} to 0600",
        f"would set the mode of {machine.state_dir / 'records'} to 0700",
        f"would write {machine.apt_conf} (the periodic jobs of apt are off)",
        "would mask apt-daily.timer",
    ]
    assert "harden.sh, dry run: 7 change(s) would be made, none was" in dry.output
    assert _snapshot(machine) == before, "the dry run changed something"
    for never in ("usermod", "passwd --lock", "chown", "systemctl mask", "systemctl stop"):
        assert not dry.called(never), never

    done = machine.harden()
    assert done.exit_code == 0, done.output
    made = [line.strip() for line in done.output.splitlines() if line.startswith("   done: ")]
    assert made == [line.replace("would ", "done: ", 1) for line in announced]
    assert "harden.sh: 7 change(s) made" in done.output
    assert done.called("usermod --shell /usr/sbin/nologin anheart")
    assert done.called("passwd --lock anheart")
    assert done.called("usermod --append --groups dialout anheart")
    assert done.called("systemctl mask apt-daily.timer")
    assert _mode(machine.env_file) == 0o600
    assert _mode(machine.state_dir / "records") == 0o700
    # It closes the machine around the console and never touches the console.
    for never in ("docker", "curl", "apt-get", "systemctl restart", "systemctl daemon-reload"):
        assert not done.called(never), never
    assert not done.called("systemctl", "anheart")

    again = machine.harden("--dry-run")
    assert again.exit_code == 0, again.output
    assert "harden.sh, dry run: nothing to change" in again.output
    assert "would " not in again.output
    idle = machine.harden()
    assert idle.exit_code == 0, idle.output
    assert "harden.sh: nothing to change" in idle.output


@pytest.mark.slow
@needs_bash
def test_anh151_ex8_harden_runs_the_steps_it_is_asked_in_its_own_order(machine: Machine) -> None:
    assert machine.install().exit_code == 0
    _drift(machine)
    # Named backwards: they run in the order of the script all the same.
    done = machine.harden("updates", "files")
    assert done.exit_code == 0, done.output
    steps = [line for line in done.output.splitlines() if line.startswith("== Step: ")]
    assert steps == ["== Step: files", "== Step: updates"]
    assert not done.called("usermod"), "a step that was not asked for ran"
    assert "harden.sh: 4 change(s) made" in done.output

    # The steps, without root and without touching anything.
    machine.set("uid", "1000\n")
    listed = machine.harden("--list")
    assert listed.exit_code == 0, listed.output
    assert [line.split()[0] for line in listed.output.splitlines()] == list(HARDEN_STEPS)
    assert listed.calls == ()
    refused = machine.harden("--dry-run")
    assert refused.exit_code == 1
    assert "sudo bash scripts/harden.sh" in refused.output
    machine.set("uid", "0\n")
    for unknown in ("firewall", "--force"):
        wrong = machine.harden(unknown)
        assert wrong.exit_code == 2, unknown
        assert "usage:" in wrong.output
        assert not wrong.called("systemctl")
    assert machine.harden("--help").exit_code == 0


@pytest.mark.slow
@needs_bash
def test_anh151_ex8_harden_refuses_a_machine_the_console_is_not_installed_on(
    machine: Machine,
) -> None:
    """Before ``install.sh``: no account to give anything to. Said, and nothing is changed."""
    for step in ("account", "files"):
        refused = machine.harden(step)
        assert refused.exit_code == 1, step
        assert "sudo bash scripts/install.sh creates it" in refused.output
        for never in ("usermod", "chown", "passwd --lock"):
            assert not refused.called(never), never


def test_anh151_ex8_harden_is_written_to_take_the_steps_of_the_next_lots() -> None:
    """One name in ``STEPS`` and three functions a step: the network, the firewall, the
    certificate and the read-only root are added the same way."""
    hardener = _read(HARDENER)
    steps = _captures(r"^readonly STEPS=\((.*)\)$", hardener)
    assert len(steps) == 1
    assert tuple(steps[0].split()) == HARDEN_STEPS
    for step in HARDEN_STEPS:
        for function in ("describe", "step", "undo"):
            assert re.search(rf"^{function}_{step}\(\) \{{$", hardener, re.MULTILINE), (
                f"{function}_{step}"
            )
    # The installer runs the steps of this lot at every install, by name: a
    # step added later is not run by an install unless it is named there too.
    assert 'bash "$HARDEN" account files updates' in _read(INSTALLER)
    assert 'bash "$HARDEN" --undo' in _read(UNINSTALLER)
    # Run as root on a machine: nothing is evaluated, fetched or installed.
    for script in (HARDENER, UNINSTALLER, BIND_HELPER):
        code = "\n".join(
            line for line in _read(script).splitlines() if not line.lstrip().startswith("#")
        )
        for banned in ("eval ", "curl", "wget", "apt-get", "pip ", "docker pull"):
            assert banned not in code, f"{script.name}: {banned}"
        assert "set -u" in code or "set -euo pipefail" in code, script.name


@pytest.mark.slow
@needs_bash
def test_anh151_ex8_an_uninstall_takes_back_what_the_install_and_the_hardening_set(
    machine: Machine,
) -> None:
    """Every setting written for the console goes; the configuration, the data and the
    account stay, as private as they were; the install can be run again."""
    assert machine.install().exit_code == 0
    held = _write_console_files(machine, directory=0o700, file=0o600)
    configuration = machine.env_file.read_bytes()
    taken = (
        machine.unit,
        machine.drop_in.parent,
        machine.bind_helper.parent,
        machine.chrony_conf,
        machine.journald_conf,
        machine.manager_drop_in,
        machine.device_rule,
        machine.apt_conf,
    )
    for path in taken:
        assert path.exists(), f"{path} was never installed: nothing to take back"

    done = machine.uninstall()
    assert done.exit_code == 0, done.output
    for path in taken:
        assert not path.exists(), f"{path} is still there"
    # The console is stopped by its controlled stop before its unit goes.
    assert done.calls.index("systemctl disable --now anheart.service") < done.calls.index(
        "systemctl daemon-reload"
    )
    assert done.called("systemctl try-restart chrony.service")
    assert done.called("systemctl restart systemd-journald.service")
    for unit in UPDATE_UNITS:
        assert done.called(f"systemctl unmask {unit}"), unit
    # Left on purpose: the machine's key, its data, its account, its images, its packages.
    assert machine.env_file.read_bytes() == configuration
    assert _mode(machine.env_file) == 0o600
    for name, path in _console_files(machine).items():
        assert path.read_bytes() == held[name], name
        assert _mode(path) == 0o600, name
    assert _mode(machine.state_dir) == 0o700
    for never in ("userdel", "apt-get", "docker", "chown", "systemctl disable chrony"):
        assert not done.called(never), never

    # What is already gone is not an error.
    again = machine.uninstall()
    assert again.exit_code == 0, again.output
    assert "the unit was not installed" in again.output
    assert not again.called("systemctl unmask")

    back = machine.install()
    assert back.exit_code == 0, back.output
    for path in taken:
        assert path.exists(), f"{path} did not come back"
    assert machine.env_file.read_bytes() == configuration


@pytest.mark.slow
@needs_bash
def test_anh151_ex8_only_root_uninstalls(machine: Machine) -> None:
    assert machine.install().exit_code == 0
    machine.set("uid", "1000\n")
    refused = machine.uninstall()
    assert refused.exit_code == 1
    assert "sudo bash scripts/uninstall.sh" in refused.output
    assert machine.unit.exists()
    assert not refused.called("systemctl")


def test_anh151_ex8_the_end_to_end_test_proves_each_reduction_in_a_real_system() -> None:
    """What the scripted commands above cannot show is asked of a real systemd, a real
    Docker and a real kernel, as root, by the CI's install test."""
    script = _read(END_TO_END)
    for proof in (
        # EX-4, the container: who the console is, and what the kernel gives it.
        '[ "$(in_console id -u):$(in_console id -g)" = "$account" ]',
        "{{.HostConfig.Privileged}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}",
        '"false true [ALL] [no-new-privileges]"',
        "CapPrm: 0000000000000000 CapEff: 0000000000000000 CapBnd: 0000000000000000",
        "NoNewPrivs: 1",
        ": > /app/written-by-the-install-test",
        "mknod -m 0660 /dev/ttyUSB7 c 188 7",
        'expect_in "$(what_opening /dev/anheart-test-refused)" "EPERM"',
        'expect_in "$(what_opening /dev/anheart-test-other)" "EACCES"',
        "{{.Config.User}}",
        # EX-4, the unit: the relay is the account; the client is root, and said to be.
        '"Uid: $relay_user $relay_user $relay_user $relay_user "',
        'expect_in "$(owned /run/anheart/relay)" "anheart:anheart 700"',
        # A stop: the console still reaches its relay, and systemd reads its exit code.
        "the console that stops could not reach its relay any more",
        '"ExecMainStatus=0"',
        "NoNewPrivileges,ProtectSystem,ProtectHome",
        'nsenter --target "$main_pid" --mount',
        "the Docker client of the unit is root",
        # EX-6: created private, corrected on a machine that is not, content untouched.
        'expect_in "$(owned "$ENV_FILE")" "root:root 600"',
        'expect_in "$(owned /var/lib/anheart/records/service-state.json)" "anheart:anheart 600"',
        "in_machine chown root:root /var/lib/anheart/records/service-state.json",
        'expect_in "$(owned "$logbook")" "anheart:anheart 600"',
        "-perm /077",
        "neither content was touched",
        # EX-7: apt itself says what it applies.
        "apt-config dump",
        'APT::Periodic::Unattended-Upgrade "0";',
        "is-enabled",
        # EX-8: a dry run, a real run, a second dry run; then the uninstall.
        "harden --dry-run",
        "change(s) would be made, none was",
        '"harden.sh: 9 change(s) made"',
        '"harden.sh, dry run: nothing to change"',
        'in_machine bash "$COPY/scripts/uninstall.sh"',
        "install.sh failed after an uninstall",
    ):
        assert proof in script, proof
    # Steps 1 to 10 of the tickets before this one still run, in that order, after it.
    steps = _captures(r'^step "(\d+)\. ', script)
    assert steps == [str(number) for number in range(1, 13)]


def test_anh151_ex8_the_workflow_lints_every_script_that_runs_on_a_pi() -> None:
    """ShellCheck, before the install test: a warning or an error fails the job."""
    workflow = _workflow(WORKFLOW)
    listed = set(_captures(r"^ {6}- '([^']+)'$", workflow))
    for path in (
        "raspberry-pi/scripts/harden.sh",
        "raspberry-pi/scripts/uninstall.sh",
        "raspberry-pi/scripts/pi/bind-bitalino.sh",
    ):
        assert path in listed, f"a change of {path} would not start the install test"
    lint = workflow.index("shellcheck --severity=warning")
    assert lint < workflow.index("bash raspberry-pi/scripts/pi/test_install.sh")
    linted = set(_captures(r"^ {12}(raspberry-pi/\S+\.sh)(?: \\)?$", workflow))
    assert linted >= {
        "raspberry-pi/scripts/install.sh",
        "raspberry-pi/scripts/harden.sh",
        "raspberry-pi/scripts/uninstall.sh",
        "raspberry-pi/scripts/pi/bind-bitalino.sh",
        "raspberry-pi/scripts/pi/test_install.sh",
        "raspberry-pi/docker/entrypoint.sh",
    }
    for path in linted:
        assert (REPOSITORY / path).is_file(), path


def test_anh151_ex8_the_page_of_the_image_carries_the_check_list() -> None:
    """``docs/pi-image.md``: one line of the check-list for each step of ``harden.sh``, and
    the lots that are not written yet are said not to be."""
    page = _read(VERSIONS_PAGE)
    section = page.partition("\n## 10. Durcissement")[2]
    assert section, "the page has no section on the hardening"
    for step in HARDEN_STEPS:
        assert f"`{step}`" in section, step
    for later in ("point d'accès", "pare-feu", "tls", "lecture seule"):
        assert later in section.casefold(), later
    assert section.count("**non fait**") >= 4, "a lot that is not written is said not to be"
    assert "`sudo bash scripts/harden.sh --dry-run`" in section
