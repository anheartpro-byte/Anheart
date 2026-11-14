"""ANH-149: what the pre-flight makes of the counts a console holds after a restart.

A console that has read no 1.16 poll since it started, and keeps no count from before
(no file, or one that does not parse), judges the drive interventions orange, as it
judges the incidents: it lets an occupied start through. A count kept from before the
restart is what closes the likely sequence (a drive fault, then the machine switched off
and on again): it refuses that start.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from src.cloud_sync import COUNT_FILE_MAX_BYTES, HeldCount, load_count
from src.training.preflight import CONTROLS, judge, refusal_of
from src.training.types import Occupancy
from src.units import UnixMillis
from tests.test_preflight import _green  # pyright: ignore[reportPrivateUsage]  # one fixture


def test_a_count_never_known_lets_an_occupied_start_through_orange() -> None:
    """Never known: unknown, orange, as before the count was kept."""
    inputs = _green(drive_interventions_due=None, incidents_open_high=None)
    assert refusal_of(judge(CONTROLS, inputs, Occupancy.OCCUPIED)) is None


def test_a_count_file_written_by_hand_is_read_only_in_the_shape_this_console_writes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Crafted files: a 0 is a 0 from before the restart; a negative count, a count that
    is a float, a key this console never writes are unknown, and said."""
    good = tmp_path / "good.json"
    good.write_text('{"schema_version":1,"count":0,"read_at":1}', encoding="utf-8")
    assert load_count(good) == HeldCount(0, UnixMillis(1), restored=True)
    for name, content in (
        ("negative", '{"schema_version":1,"count":-1,"read_at":1}'),
        ("float", '{"schema_version":1,"count":1.0,"read_at":1}'),
        ("extra", '{"schema_version":1,"count":2,"read_at":1,"x":0}'),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.cloud_sync"):
            assert load_count(path) is None, name
        assert f"{name}.json is not a count this console wrote" in caplog.text


def test_a_count_file_that_is_a_link_a_fifo_or_too_large_is_unknown_and_never_read_whole(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A link to a large file, a FIFO in its place (startup never waits on it), a regular
    file over its bound: unknown, said, and the file is not read whole."""
    large = tmp_path / "large"
    large.write_bytes(
        b" " * (COUNT_FILE_MAX_BYTES + 1) + b'{"schema_version":1,"count":3,"read_at":1}'
    )
    link = tmp_path / "link.json"
    link.symlink_to(large)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    for path in (large, link, fifo):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="src.cloud_sync"):
            assert load_count(path) is None, path.name
        assert f"{path.name} unreadable (OSError:" in caplog.text


def test_a_count_kept_from_before_the_restart_refuses_an_occupied_start() -> None:
    inputs = _green(
        drive_interventions_due=1,
        drive_interventions_restored=UnixMillis(1_700_000_000_000),
        incidents_open_high=None,
    )
    refused = refusal_of(judge(CONTROLS, inputs, Occupancy.OCCUPIED))
    assert refused is not None
    assert refused.control == "drive_interventions"
