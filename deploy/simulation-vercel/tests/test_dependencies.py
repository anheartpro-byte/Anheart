from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path
from typing import Final, TypedDict

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from pydantic import TypeAdapter

ROOT: Final[Path] = Path(__file__).resolve().parents[3]
HOSTED: Final[Path] = ROOT / "deploy/simulation-vercel"
IMAGE_LOCK: Final[Path] = ROOT / "raspberry-pi/requirements-lock.txt"


class ProjectDependencies(TypedDict):
    dependencies: tuple[str, ...]


class DeploymentManifest(TypedDict):
    project: ProjectDependencies


def test_git_and_standalone_builds_install_the_same_dependencies() -> None:
    # Given the standalone requirements, when parsing the Git manifest, then constraints match.
    standalone = (ROOT / "deploy/simulation-vercel/requirements.txt").read_text(encoding="utf-8")
    expected = {
        str(Requirement(line.strip()))
        for line in standalone.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    manifest = TypeAdapter(DeploymentManifest).validate_python(
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    )
    assert {
        str(Requirement(dependency)) for dependency in manifest["project"]["dependencies"]
    } == expected


def _locked(path: Path) -> dict[str, Version]:
    entries = re.split(r"\n(?=[A-Za-z0-9])", path.read_text(encoding="utf-8"))
    versions: dict[str, Version] = {}
    for raw in entries:
        entry = "\n".join(line for line in raw.splitlines() if not line.startswith("#")).strip()
        if not entry:
            continue
        requirement = Requirement(entry.splitlines()[0].removesuffix("\\").strip())
        specifiers = tuple(requirement.specifier)
        assert len(specifiers) == 1, requirement
        assert specifiers[0].operator == "==", requirement
        assert re.search(r"--hash=sha256:[0-9a-f]{64}(?:\s|$)", entry), requirement
        versions[canonicalize_name(requirement.name)] = Version(specifiers[0].version)
    assert versions, path
    return versions


def test_the_hosted_lock_satisfies_every_input_requirement() -> None:
    locked = _locked(HOSTED / "requirements-lock.txt")
    for line in (HOSTED / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        assert locked[canonicalize_name(requirement.name)] in requirement.specifier, requirement


def test_every_hosted_package_is_tested_at_the_version_of_the_image() -> None:
    image = _locked(IMAGE_LOCK)
    for name, version in _locked(HOSTED / "requirements-lock.txt").items():
        assert image.get(name) == version, name


def test_the_hosted_lock_command_preserves_platform_constraints_and_cutoff() -> None:
    command = shlex.split(
        (HOSTED / "requirements-lock.txt").read_text(encoding="utf-8").splitlines()[1][5:]
    )
    image_command = shlex.split(IMAGE_LOCK.read_text(encoding="utf-8").splitlines()[1][5:])
    assert command[:4] == ["uv", "pip", "compile", "requirements.txt"]
    assert command[command.index("-c") + 1] == "../../raspberry-pi/requirements-base.txt"
    assert command[command.index("--python-platform") + 1] == "x86_64-unknown-linux-gnu"
    assert command[command.index("--python-version") + 1] == "3.12"
    assert "--generate-hashes" in command
    assert (
        command[command.index("--exclude-newer") + 1]
        == image_command[image_command.index("--exclude-newer") + 1]
    )


def test_the_root_requirements_include_only_the_hosted_lock() -> None:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    installed = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert installed == ["-r deploy/simulation-vercel/requirements-lock.txt"]
