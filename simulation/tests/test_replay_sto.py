"""A1-20: the replay reads the safety chain as the console read it, from the record.

The console writes a ``sto`` event at the first tick and at each change of what
it reads of the chain, never one per read. The replay's runtime reads the chain
from those events: a chain that opens in the session opens in its replay, at the
instant it did, with the verdict it had; a chain the record says opened opens
in the replay at that instant, whatever the drive's answers say. A record
without them replays on a ready chain, as before, and one whose ``sto`` event
names something the console could not have written is not replayed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simulation.replay import replay
from simulation.replay_report import Outcome
from simulation.scenario import Expectation, ManualTarget, StoOpen
from simulation.tests.replay_support import Line, copy, edit_lines, record, reseal, short
from src.record.reader import Recording, read
from src.record.schema import EventKind
from src.result import Err, Ok
from src.training.runtime import RULE_STO_OPEN
from src.units import OutputRpm, Seconds

TARGET = ManualTarget(Seconds(1.0), OutputRpm(27.0), Expectation.ACCEPTED)


@pytest.fixture(scope="module")
def opened(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A manual session turning when the safety chain opens."""
    return record(
        short("manual_27_rpm", duration=8.0, actions=(TARGET, StoOpen(Seconds(4.0)))),
        tmp_path_factory.mktemp("opened"),
    )


@pytest.fixture(scope="module")
def turning(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A manual session turning on a chain that stays ready."""
    return record(
        short("manual_27_rpm", duration=6.0, actions=(TARGET,)),
        tmp_path_factory.mktemp("turning"),
    )


def loaded(folder: Path) -> Recording:
    result = read(folder)
    assert isinstance(result, Ok), result
    return result.value


def told(folder: Path) -> list[tuple[float, str]]:
    return [
        (event.t, event.detail) for event in loaded(folder).events if event.kind is EventKind.STO
    ]


def test_the_record_tells_the_chain_once_then_at_each_change(opened: Path) -> None:
    said = told(opened)
    assert [detail for _t, detail in said] == ["ready (sim_ready)", "open (sim_ready)"]
    verdict = next(row.t for row in loaded(opened).rows if row.safety_rule == RULE_STO_OPEN)
    assert said[1][0] == pytest.approx(verdict, abs=0.001), "told at the tick that read it"


def test_a_chain_that_opens_in_the_session_opens_in_its_replay(opened: Path) -> None:
    replayed = replay(opened)
    assert isinstance(replayed, Ok), replayed
    assert replayed.value.matches, replayed.value.to_text()


def test_a_chain_the_record_says_opened_opens_in_the_replay_at_that_instant(
    turning: Path, tmp_path: Path
) -> None:
    """What drives the replay's chain is the events, not the drive: the same session told
    with the chain open from 3 s. At 3 s the replayed runtime reads it open and zeroes the
    setpoint, from the reading alone, where the record has the session turning on: the
    replay stops there, the drive's recorded answers no longer fitting its questions."""
    said = copy(turning, tmp_path)

    def open_at_3(lines: list[Line]) -> list[Line]:
        later = next(i for i, line in enumerate(lines) if float(str(line["t"])) > 3.0)
        opening: Line = {"t": 3.0, "kind": "sto", "detail": "open (sim_ready)", "actor": "system"}
        return [*lines[:later], opening, *lines[later:]]

    edit_lines(said, "events.jsonl", open_at_3)
    reseal(said)
    replayed = replay(said)
    assert isinstance(replayed, Ok), replayed
    report = replayed.value
    assert report.outcome is Outcome.DIVERGENCE, report.to_text()
    divergence = report.divergence
    assert divergence is not None
    assert divergence.t == pytest.approx(3.0, abs=0.001)
    assert "the runtime requested speed 0 motor rpm" in report.to_text()


def test_a_sto_event_the_console_could_not_have_written_is_not_replayed(
    opened: Path, tmp_path: Path
) -> None:
    planted = copy(opened, tmp_path)

    def plant(lines: list[Line]) -> list[Line]:
        for line in lines:
            if line["kind"] == "sto":
                line["detail"] = "open (Jeanne Martin)"
        return lines

    edit_lines(planted, "events.jsonl", plant)
    reseal(planted)
    refused = replay(planted)
    assert isinstance(refused, Err), refused
    assert "a sto event names no state and source of the console" in refused.error.detail
