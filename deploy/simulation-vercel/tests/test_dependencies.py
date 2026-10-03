from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Final, TypedDict

from packaging.requirements import Requirement
from pydantic import TypeAdapter

ROOT: Final[Path] = Path(__file__).resolve().parents[3]


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
