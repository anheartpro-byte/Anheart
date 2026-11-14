"""R2 probe, ANH-149 contract 1.16: what a console that knows no count lets through.

The review's question, kept as a test: a console that has read no 1.16 poll since it
started, and keeps no count from before (no file, or one that does not parse), judges
the drive interventions orange, as it judges the incidents: it lets an occupied start
through. A count kept from before the restart is what closes the likely sequence (a
drive fault, then the machine switched off and on again): it refuses that start.
"""

from __future__ import annotations

from src.training.preflight import CONTROLS, judge, refusal_of
from src.training.types import Occupancy
from src.units import UnixMillis
from tests.test_preflight import _green  # pyright: ignore[reportPrivateUsage]  # one fixture


def test_probe_unannounced_count_lets_an_occupied_start_through() -> None:
    """Never known: unknown, orange, as before ANH-149's follow-up."""
    inputs = _green(drive_interventions_due=None, incidents_open_high=None)
    assert refusal_of(judge(CONTROLS, inputs, Occupancy.OCCUPIED)) is None


def test_probe_a_count_kept_from_before_the_restart_refuses_it() -> None:
    inputs = _green(
        drive_interventions_due=1,
        drive_interventions_restored=UnixMillis(1_700_000_000_000),
        incidents_open_high=None,
    )
    refused = refusal_of(judge(CONTROLS, inputs, Occupancy.OCCUPIED))
    assert refused is not None
    assert refused.control == "drive_interventions"
