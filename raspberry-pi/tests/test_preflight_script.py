"""``scripts/pi/preflight.sh`` and the versioned contract (ANH-133).

The person who installs a machine reads this script's verdict before starting
the console, so what it says about the dashboard's contract must be true. In
particular a dashboard older than the contract accepts the console's key and
answers 200, and the console from the same tree will still refuse every remote
launch from it: the script may only say the contract is *served* when the
dashboard itself announces a version of the console's major.

The script is run for real, under ``bash``, in a copy of the console's tree,
with a scripted ``curl`` and a scripted ``docker`` first on ``PATH``: no
network, no daemon. Only its "Dashboard link" lines are judged here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from src.contract import CONTRACT_HEADER, CONTRACT_VERSION

BASH: Final[str | None] = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="preflight.sh is a bash script")

CONSOLE: Final[Path] = Path(__file__).resolve().parents[1]
URL: Final[str] = "https://example.convex.site"
KEY: Final[str] = "synthetic-machine-key"

ENV_FILE: Final[str] = f"""MOTOR_BACKEND=sim
ECG_SOURCE=sim
ARM_RADIUS_M=1.5
CONVEX_URL={URL}
MACHINE_API_KEY={KEY}
"""

FAKE_CURL: Final[str] = """#!/bin/sh
# Scripted curl: records its arguments, then prints the canned body and status
# the way `-w '\\n%{http_code}'` does.
printf '%s\\n' "$@" > "$FAKE_CURL_LOG"
printf '%s\\n%s' "$FAKE_BODY" "$FAKE_CODE"
"""

FAKE_DOCKER: Final[str] = """#!/bin/sh
echo "0.0.0-scripted"
"""

NOTHING_WAITING: Final[str] = '{"session":null,"server_contract_version":"%s"}'
BEFORE_THE_CONTRACT: Final[str] = '{"session":null}'


@dataclass(frozen=True, slots=True)
class Verdict:
    """What one run of the script said and did."""

    exit_code: int
    output: str
    curl_arguments: tuple[str, ...]

    @property
    def dashboard(self) -> tuple[str, ...]:
        """The lines of the "Dashboard link" section, without their indentation."""
        lines = self.output.splitlines()
        start = lines.index("5. Dashboard link") + 1
        end = lines.index("", start)
        return tuple(line.strip() for line in lines[start:end])

    def said(self, level: str, *words: str) -> bool:
        """Whether one dashboard line of that level contains every one of ``words``."""
        return any(
            line.startswith(level) and all(word in line for word in words)
            for line in self.dashboard
        )


def preflight(
    tmp_path: Path, *, code: str, body: str, contract_source: str | None = None
) -> Verdict:
    """Run the real script in a copy of the console's tree, against a scripted dashboard."""
    assert BASH is not None
    tree = tmp_path / "raspberry-pi"
    scripts = tree / "scripts" / "pi"
    scripts.mkdir(parents=True)
    shutil.copy(CONSOLE / "scripts" / "pi" / "preflight.sh", scripts / "preflight.sh")
    (tree / "src").mkdir()
    source = (
        (CONSOLE / "src" / "contract.py").read_text(encoding="utf-8")
        if contract_source is None
        else contract_source
    )
    (tree / "src" / "contract.py").write_text(source, encoding="utf-8")
    (tree / ".env").write_text(ENV_FILE, encoding="utf-8")
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, text in (("curl", FAKE_CURL), ("docker", FAKE_DOCKER)):
        tool = tools / name
        tool.write_text(text, encoding="utf-8")
        tool.chmod(0o755)
    log = tmp_path / "curl.log"
    done = subprocess.run(  # noqa: S603  # fixed argv, no shell, scripted tools only
        [BASH, str(scripts / "preflight.sh")],
        env={
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CODE": code,
            "FAKE_BODY": body,
            "FAKE_CURL_LOG": str(log),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    arguments = tuple(log.read_text(encoding="utf-8").splitlines()) if log.exists() else ()
    return Verdict(exit_code=done.returncode, output=done.stdout, curl_arguments=arguments)


# =========================================================================
# "Served" is said on evidence only
# =========================================================================


@pytest.mark.parametrize("announced", [CONTRACT_VERSION, "1.7"])
def test_the_contract_is_said_served_when_the_dashboard_announces_this_major(
    tmp_path: Path, announced: str
) -> None:
    verdict = preflight(tmp_path, code="200", body=NOTHING_WAITING % announced)
    assert verdict.said("ok", URL, "key accepted")
    assert verdict.said("ok", f"contract {CONTRACT_VERSION} served", f"announces {announced}")
    assert not any(line.startswith(("WARN", "FAIL")) for line in verdict.dashboard)
    assert verdict.exit_code == 0


def test_a_dashboard_older_than_the_contract_is_not_said_to_serve_it(tmp_path: Path) -> None:
    """It accepts the key and answers 200, and this console refuses its launches all the same."""
    verdict = preflight(tmp_path, code="200", body=BEFORE_THE_CONTRACT)
    assert verdict.said("ok", URL, "key accepted")
    assert not any("served" in line for line in verdict.dashboard)
    assert verdict.said("WARN", "does not announce a contract", "will refuse every remote launch")
    assert verdict.exit_code == 0  # a warning: the console itself runs


@pytest.mark.parametrize("announced", ["2.0", "0.9", "10.3"])
def test_a_dashboard_announcing_another_major_is_not_said_to_serve_it(
    tmp_path: Path, announced: str
) -> None:
    verdict = preflight(tmp_path, code="200", body=NOTHING_WAITING % announced)
    assert not any("served" in line for line in verdict.dashboard)
    assert verdict.said(
        "WARN",
        f"announces contract {announced}",
        f"this console speaks {CONTRACT_VERSION}",
        "will refuse every remote launch",
    )


@pytest.mark.parametrize("announced", ["one", "1", "", "1.x"])
def test_a_version_that_cannot_be_read_counts_as_not_announced(
    tmp_path: Path, announced: str
) -> None:
    verdict = preflight(tmp_path, code="200", body=NOTHING_WAITING % announced)
    assert not any("served" in line for line in verdict.dashboard)
    assert verdict.said("WARN", "does not announce a contract")


def test_a_dashboard_that_refuses_the_contract_fails_the_preflight(tmp_path: Path) -> None:
    body = '{"error":"contract_unsupported","message":"Unsupported","supported":["2"]}'
    verdict = preflight(tmp_path, code="426", body=body)
    assert verdict.said("FAIL", "key accepted", f"does not serve contract {CONTRACT_VERSION}")
    assert not any(line.startswith("ok") for line in verdict.dashboard)
    assert verdict.exit_code == 1


@pytest.mark.parametrize(
    ("code", "level", "words", "exit_code"),
    [
        ("401", "FAIL", "the key is refused", 1),
        ("000", "FAIL", "unreachable", 1),
        ("500", "WARN", "answered HTTP 500", 0),
    ],
)
def test_other_answers_keep_their_meaning(
    tmp_path: Path, code: str, level: str, words: str, exit_code: int
) -> None:
    verdict = preflight(tmp_path, code=code, body="")
    assert verdict.said(level, words)
    assert not any("served" in line for line in verdict.dashboard)
    assert verdict.exit_code == exit_code


# =========================================================================
# What it sends, and what it keeps to itself
# =========================================================================


def test_the_request_is_the_idle_console_s_own_read_with_its_contract(tmp_path: Path) -> None:
    verdict = preflight(tmp_path, code="200", body=NOTHING_WAITING % CONTRACT_VERSION)
    sent = verdict.curl_arguments
    assert f"{URL}/api/machine/training/poll" in sent
    assert f"Authorization: Bearer {KEY}" in sent
    # The version is read out of src/contract.py by the script itself.
    assert f"{CONTRACT_HEADER}: {CONTRACT_VERSION}" in sent
    # A plain GET: nothing is posted, so nothing can be recorded or started.
    assert not {"-X", "POST", "-d", "--data"} & set(sent)


def test_the_answer_is_read_and_never_printed(tmp_path: Path) -> None:
    """A poll answer can carry a waiting launch, which names a rider."""
    body = (
        '{"session":{"sessionId":"remote-1","subjectLabel":"Synthetic Rider"},'
        f'"server_contract_version":"{CONTRACT_VERSION}"}}'
    )
    verdict = preflight(tmp_path, code="200", body=body)
    assert verdict.said("ok", f"contract {CONTRACT_VERSION} served")
    assert "Synthetic Rider" not in verdict.output
    assert "remote-1" not in verdict.output
    assert KEY not in verdict.output


def test_a_contract_version_that_cannot_be_read_fails_the_preflight(tmp_path: Path) -> None:
    verdict = preflight(
        tmp_path,
        code="200",
        body=NOTHING_WAITING % CONTRACT_VERSION,
        contract_source="# no version here\n",
    )
    assert verdict.said("FAIL", "contract version could not be read")
    assert verdict.dashboard == (verdict.dashboard[0],)  # that line and nothing else
    assert verdict.curl_arguments == ()  # the dashboard is not asked without a version
    assert verdict.exit_code == 1
