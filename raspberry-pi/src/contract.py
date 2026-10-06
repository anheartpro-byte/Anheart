"""The versioned contract between this machine and the dashboard (Convex).

One number, ``major.minor``, names the shape of everything the console sends
and everything it accepts back. The two sides must share the **major**:

* every request carries :data:`CONTRACT_HEADER`, and a server that does not
  serve this major answers 426 with :data:`CONTRACT_UNSUPPORTED` instead of
  reading a body it would misunderstand;
* every poll answer names the server's own version, and
  :func:`server_refusal` is what keeps a launch written for another major from
  ever being armed here. An answer that names no version is not an answer of
  this contract, and is refused the same way.

A different **minor** is fine in both directions: within one major each side
relies only on what every minor of it provides.

Why the version is a constant and not read from a file at run time
------------------------------------------------------------------
The single source is ``contracts/machine-api.json`` at the repository root,
which the dashboard's code reads directly. This console is deployed as the
``raspberry-pi/`` directory alone (``scripts/pi/deploy.sh``, the Docker build
context), so that file is not on the machine. The constants below are therefore
pinned to it by ``tests/test_contract.py``: a change on either side that is not
made on the other fails the gate before it can reach a machine.

The software version is different: it is a fact about a build, not about the
code, so it IS read from a file (``raspberry-pi/VERSION``, written by the
release process), once, at startup.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NewType, cast

_logger: logging.Logger = logging.getLogger(__name__)

ContractVersion = NewType("ContractVersion", str)
"""A well-formed ``major.minor`` contract version."""

ErrorCode = NewType("ErrorCode", str)
"""A stable error code of the machine API (``error_codes`` of the shared file)."""

SoftwareVersion = NewType("SoftwareVersion", str)
"""What this build calls itself: a git tag such as ``pi-0.4.2``."""

CONTRACT_VERSION: Final[ContractVersion] = ContractVersion("1.0")
"""The contract this console speaks. Pinned to ``contracts/machine-api.json``."""

CONTRACT_HEADER: Final[str] = "X-Anheart-Contract"
"""The request header that carries :data:`CONTRACT_VERSION`."""

SERVER_VERSION_FIELD: Final[str] = "server_contract_version"
"""Where a poll answer names the server's contract version."""

CONTRACT_UNSUPPORTED: Final[ErrorCode] = ErrorCode("contract_unsupported")
"""The code of the 426 a server answers to a major it does not serve."""

UNKNOWN_SOFTWARE_VERSION: Final[SoftwareVersion] = SoftwareVersion("pi-unknown")
"""Reported when ``VERSION`` is missing or unreadable: never a guess."""

VERSION_PATH: Final[Path] = Path(__file__).resolve().parent.parent / "VERSION"
"""``raspberry-pi/VERSION`` in a checkout, ``/app/VERSION`` in the image."""

UNKNOWN_THEIRS: Final[str] = "inconnu"
"""What the console shows for a server version it could not read."""

# ``[0-9]`` and never ``\d``: in a ``str`` pattern ``\d`` also matches the digits
# of every other script, and a version is ASCII.
_VERSION: Final[re.Pattern[str]] = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
_MAJOR: Final[re.Pattern[str]] = re.compile(r"0|[1-9][0-9]{0,3}")
_CODE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SOFTWARE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")


def version_of(value: object) -> ContractVersion | None:
    """The contract version in an untrusted value; ``None`` unless it is exactly one."""
    if isinstance(value, str) and _VERSION.fullmatch(value) is not None:
        return ContractVersion(value)
    return None


def major_of(version: ContractVersion) -> str:
    """The major of a contract version: what both sides must share."""
    return version.partition(".")[0]


def error_code_of(value: object) -> ErrorCode | None:
    """The stable code in an untrusted value; ``None`` for a sentence or anything else."""
    if isinstance(value, str) and _CODE.fullmatch(value) is not None:
        return ErrorCode(value)
    return None


def majors_of(value: object) -> tuple[str, ...]:
    """The contract majors a 426 says the server serves; anything unreadable is left out."""
    if not isinstance(value, list):
        return ()
    # A JSON array: its items are untrusted, so each is narrowed before use.
    items = cast("Sequence[object]", value)
    return tuple(
        item for item in items if isinstance(item, str) and _MAJOR.fullmatch(item) is not None
    )


def incompatible_server(theirs: str) -> str:
    """The one sentence the console shows and logs for a server it cannot work with."""
    return f"serveur incompatible (contrat {CONTRACT_VERSION} vs {theirs})"


def server_refusal(announced: object) -> str | None:
    """Why a poll answer must not arm anything here; ``None`` when it is of this major.

    ``announced`` is the answer's :data:`SERVER_VERSION_FIELD`, untrusted. A
    missing or unreadable version is refused: nothing says the rest of the
    answer means what this console would take it to mean.
    """
    theirs = version_of(announced)
    if theirs is None:
        return incompatible_server(UNKNOWN_THEIRS)
    if major_of(theirs) == major_of(CONTRACT_VERSION):
        return None
    return incompatible_server(theirs)


def unsupported_refusal(supported: Sequence[str]) -> str:
    """The sentence for a 426: this console's version against the majors the server serves."""
    return incompatible_server(", ".join(supported) if supported else UNKNOWN_THEIRS)


def read_software_version(path: Path = VERSION_PATH) -> SoftwareVersion:
    """This build's version, from ``VERSION``; :data:`UNKNOWN_SOFTWARE_VERSION` if it has none.

    Read once at startup, never from the control loop. A missing, unreadable
    or malformed file must not keep the console from starting: the dashboard
    is told the version is unknown, which is true.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        _logger.warning("software version unreadable (%s): reporting it as unknown", error)
        return UNKNOWN_SOFTWARE_VERSION
    if _SOFTWARE.fullmatch(text) is None:
        _logger.warning("software version malformed in %s: reporting it as unknown", path)
        return UNKNOWN_SOFTWARE_VERSION
    return SoftwareVersion(text)
