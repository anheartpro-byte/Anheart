"""What the tidy-up of the console's code (ANH-200) must keep true.

Two things changed shape and are pinned here; everything else is held by the
existing suite, which was not edited:

* a refused ``Origin`` header is quoted in the log on one bounded line;
* a protocol member is abstract: a class that inherits a protocol and leaves a
  member out cannot be built, instead of answering ``None`` in its place.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast, override

import pytest

from src.clock import Clock, ManualClock
from src.control_surface import Command
from src.units import Monotonic
from src.web.schemas import CommandRow
from src.web.ws import LOGGED_HEADER_LIMIT, logged_header
from tests.test_web_api import TOKEN, build_rig, probe_socket

WS_LOGGER = "src.web.ws"

# =========================================================================
# The refused origin, in the log
# =========================================================================


def test_a_short_header_is_logged_as_it_was_sent() -> None:
    assert logged_header("http://tablet.local:8080") == "http://tablet.local:8080"


def test_an_absent_header_stays_absent() -> None:
    assert logged_header(None) is None


@pytest.mark.parametrize("line_break", ["\r\n", "\n", "\r"])
def test_a_logged_header_stays_on_one_line(line_break: str) -> None:
    logged = logged_header(f"http://a.example{line_break}second line")
    assert logged == "http://a.example second line"


def test_a_logged_header_is_cut_at_the_limit_and_says_so() -> None:
    sent = "http://" + "a" * 5000
    logged = logged_header(sent)
    assert logged is not None
    assert logged.startswith(sent[:LOGGED_HEADER_LIMIT])
    assert logged.endswith(f"({len(sent)} characters received)")
    assert len(logged) < LOGGED_HEADER_LIMIT + 40


def test_a_header_exactly_at_the_limit_is_not_cut() -> None:
    sent = "h" * LOGGED_HEADER_LIMIT
    assert logged_header(sent) == sent


async def test_a_refused_origin_is_quoted_on_one_bounded_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The handshake is refused as before; what the log keeps of the header is bounded."""
    rig = build_rig(tmp_path)
    origin = "http://a.example\r\nsecond line " + "x" * 5000
    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        sent = await probe_socket(rig.app, origin=origin, token=TOKEN)
    assert [message["type"] for message in sent] == ["websocket.close"]
    lines = [record.getMessage() for record in caplog.records if record.name == WS_LOGGER]
    assert len(lines) == 1
    (line,) = lines
    assert "\n" not in line
    assert "\r" not in line
    assert "http://a.example second line" in line
    assert len(line) < LOGGED_HEADER_LIMIT + 120


# =========================================================================
# Protocol members are abstract
# =========================================================================


class _HalfAClock(Clock):
    """Inherits the protocol and leaves ``unix_millis`` out."""

    @override
    def monotonic(self) -> Monotonic:
        return Monotonic(0.0)


def test_a_class_that_leaves_a_protocol_member_out_cannot_be_built() -> None:
    with pytest.raises(TypeError, match="unix_millis"):
        _ = _HalfAClock()  # type: ignore[abstract]  # pyright: ignore[reportAbstractUsage]  # the point of the test


def test_a_class_that_defines_every_member_still_satisfies_the_protocol() -> None:
    assert isinstance(ManualClock(), Clock)


# =========================================================================
# A guard that still fires
# =========================================================================


def test_a_command_that_is_not_one_fails_loudly_in_its_row() -> None:
    with pytest.raises(AssertionError):
        _ = CommandRow.of(cast("Command", "bogus"))
