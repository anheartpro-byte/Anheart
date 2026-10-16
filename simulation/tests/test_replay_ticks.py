from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from simulation.replay import replay_recording
from simulation.replay_report import Outcome
from simulation.scenario import Expectation, ManualTarget, OperatorStop
from simulation.tests.replay_support import record, short
from src.record.reader import Recording, read
from src.result import Ok
from src.units import OutputRpm, Seconds


@pytest.fixture(scope="module")
def plateau(tmp_path_factory: pytest.TempPathFactory) -> Recording:
    scenario = short(
        "manual_27_rpm",
        duration=65.0,
        actions=(
            ManualTarget(Seconds(1.0), OutputRpm(6.0), Expectation.ACCEPTED),
            OperatorStop(Seconds(50.0)),
        ),
    )
    loaded = read(record(scenario, tmp_path_factory.mktemp("plateau")))
    assert isinstance(loaded, Ok)
    return loaded.value


@pytest.mark.parametrize("extra", [False, True], ids=["missing", "extra"])
def test_a_changed_tick_on_a_constant_plateau_is_named_within_one_tick(
    plateau: Recording, extra: bool
) -> None:
    index = next(i for i, row in enumerate(plateau.rows) if row.t == 40.0)
    assert {row.setpoint_motor_rpm for row in plateau.rows[index - 1 : index + 3]} == {299}
    if extra:
        at = 40.1
        rows = (
            *plateau.rows[: index + 1],
            replace(plateau.rows[index], t=at),
            *plateau.rows[index + 1 :],
        )
    else:
        at = 40.0
        rows = (*plateau.rows[:index], *plateau.rows[index + 1 :])
    result = asyncio.run(replay_recording(replace(plateau, rows=rows)))
    assert isinstance(result, Ok)
    report = result.value
    assert report.outcome is Outcome.DIVERGENCE
    divergence = report.divergence
    assert divergence is not None
    assert at <= divergence.t <= at + 0.2
    assert divergence.tick == index + int(extra)
    assert ("extra" if extra else "missing") in divergence.requested
    assert "speed 299 motor rpm" in divergence.requested + divergence.recorded
    assert report.comparison.matches
    assert report.comparison.tolerated_rpm == (0, 0)
    assert report.comparison.shifted_transitions == (0, 0)


def test_the_unchanged_plateau_still_replays_exactly(plateau: Recording) -> None:
    result = asyncio.run(replay_recording(plateau))
    assert isinstance(result, Ok)
    assert result.value.matches
    assert result.value.comparison.deviating_ticks == 0
