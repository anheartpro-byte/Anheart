"""What the console's page says of the machine's configuration (ANH-141), as plain data.

Apart from :mod:`src.config_sync`, which decides, so that the web layer reads
it without importing the dashboard link or the configuration's rules.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ChangeView:
    """One key a revision changes, as the operator reads it."""

    key: str
    safety: bool
    """A key of the safety family: it waits for an operator at the console."""

    before: str
    after: str


@dataclass(frozen=True, slots=True)
class PendingView:
    """A revision that changes a safety key, waiting for an operator signed in at the
    console to confirm it."""

    revision: int
    changes: tuple[ChangeView, ...]


@dataclass(frozen=True, slots=True)
class ConfigView:
    """The configuration now (``GET /api/config``)."""

    revision: int | None
    """The dashboard's revision the console runs with; ``None``: its ``.env``."""

    config_hash: str | None
    name: str | None
    location: str | None
    pending: PendingView | None
    refusal: str | None
    """Why the last configuration received was refused, until another is taken."""

    restarting: bool
    """A revision was just applied: the console restarts at rest to run it."""


ENV_VIEW: Final[ConfigView] = ConfigView(
    revision=None,
    config_hash=None,
    name=None,
    location=None,
    pending=None,
    refusal=None,
    restarting=False,
)
"""A console on its ``.env`` that asks no dashboard for a configuration."""
