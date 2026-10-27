"""The harness's ``drive_input`` variant reads the STO chain as the console does.

With ``"sto_source": "drive_input"`` the harness's runtime reads register 5202
of the simulated drive through the console's own source
(``DriveInputStoSource``) and guard (``StoReadGate``), at the cadence of the
console's polling task. These tests prove each part on a bench session
stopped by the operator: every frame that reaches the simulated drive is a
read of that source the gate permitted, none during the stop; the read
instants are those the real polling task produces on the same ticks; and the
scenario key picks the variant. The failure matrix's cases are judged in
``test_failures.py`` and ``test_failures_drive_input.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from itertools import pairwise
from typing import Final

import pytest
from tests.virtual_time import run_virtual

from simulation.harness import (
    TICK,
    RunResult,
    StoFeedbackPort,
    StoObservation,
    poll_after_tick,
    run_scenario,
)
from simulation.scenario import Scenario, StoVariant, parse_scenario
from simulation.tracefile import JsonValue, Row
from src.clock import ManualClock
from src.local_panel import StoReadGate
from src.motor.drive import LOGIC_INPUTS_LOGICAL, DriveError, LogicInput
from src.motor.sto import DriveInputStoSource
from src.result import Err, Ok, Result
from src.units import Monotonic, RawRegister, RegisterAddress, Seconds

STOP_AT: Final[float] = 60.0
BENCH_STOP: Final[Mapping[str, JsonValue]] = {
    "name": "drive_input_bench_stop",
    "kind": "manual",
    "duration_s": 160,
    "sto_source": "drive_input",
    "actions": [
        {"at_s": 2.0, "do": "manual_target", "output_rpm": 3.0},
        {"at_s": STOP_AT, "do": "operator_stop"},
    ],
}
"""The bench at 3 output rpm, stopped by the operator: the stop ends well inside the run."""


def _scenario(**changes: JsonValue) -> Scenario:
    parsed = parse_scenario(json.dumps({**BENCH_STOP, **changes}))
    assert isinstance(parsed, Ok), parsed
    return parsed.value


def _run(**changes: JsonValue) -> RunResult:
    return asyncio.run(run_scenario(_scenario(**changes)))


def _pairs(result: RunResult) -> list[tuple[Row, StoObservation]]:
    """Each tick's row with what the STO reading did in the gap after it."""
    return list(zip(result.trace.rows, result.sto, strict=True))


def _stopping(row: Row) -> bool:
    """The operator's stop is under way: the ending walks down, the run command still held."""
    return row.state == "ending" and row.output_enabled


# -- the variant goes through the console's source and gate -----------------------


def test_every_5202_frame_is_a_read_of_the_console_source_that_its_gate_permitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spies on the production classes (the gate's answers, the source's polls) and the port."""
    answers: list[bool] = []
    attempts: list[int] = []
    frames: list[RegisterAddress] = []
    permitted = StoReadGate.permitted
    poll = DriveInputStoSource.poll
    read_register = StoFeedbackPort.read_register

    def asked(gate: StoReadGate) -> bool:
        answer = permitted(gate)
        answers.append(answer)
        return answer

    async def polled(source: DriveInputStoSource) -> None:
        await poll(source)
        attempts.append(source.attempts)

    async def sent(
        port: StoFeedbackPort, address: RegisterAddress
    ) -> Result[RawRegister, DriveError]:
        frames.append(address)
        return await read_register(port, address)

    monkeypatch.setattr(StoReadGate, "permitted", asked)
    monkeypatch.setattr(DriveInputStoSource, "poll", polled)
    monkeypatch.setattr(StoFeedbackPort, "read_register", sent)
    scenario = _scenario()
    result = asyncio.run(run_scenario(scenario))
    assert set(frames) == {LOGIC_INPUTS_LOGICAL}
    assert len(answers) == len(attempts)  # every poll asked the gate, once
    assert answers.count(True) == len(frames) == attempts[-1]
    recorded = sum(view.reads for view in result.sto)
    assert recorded > 100
    preroll = round(scenario.preroll / (3 * TICK))
    assert 0 < len(frames) - recorded <= preroll + 1  # the pre-roll's reads
    pairs = _pairs(result)
    stopping = [(row, view) for row, view in pairs if _stopping(row)]
    assert len(stopping) >= 30  # 149 motor rpm walked down, then held to standstill
    assert stopping[0][0].t == pytest.approx(STOP_AT)
    assert not any(view.reads for _, view in stopping)
    # Refused after every stop tick but the two at most still in the last read's period.
    assert answers.count(False) >= len(stopping) - 2
    after = [view for row, view in pairs if row.t > stopping[-1][0].t]
    assert sum(view.reads for view in after) >= len(after) // 3


def test_with_the_gate_bypassed_the_same_stop_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def always(_gate: StoReadGate) -> bool:
        return True

    monkeypatch.setattr(StoReadGate, "permitted", always)
    result = _run()
    assert sum(view.reads for row, view in _pairs(result) if _stopping(row)) > 10


def test_with_no_reading_yet_the_first_start_is_refused() -> None:
    """No pre-roll: the start comes before any read, and an unread chain arms nothing."""
    result = _run(preroll_s=0)
    assert result.start_refusal == "StoNotReady"
    assert all(row.state != "running" for row in result.trace.rows)
    assert any(view.reads for view in result.sto)


def test_a_chain_read_from_memory_sends_no_5202_frame() -> None:
    result = _run(sto_source="sim_ready")
    assert result.sto
    assert not any(view.reads for view in result.sto)


# -- the cadence: the harness's polls fall where the polling task's do -------------

TICKS: Final[int] = 60


class ReadPort:
    """Register 5202, always answering: the instant of each read noted."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock: ManualClock = clock
        # Appended by the one source reading through this port.
        self.instants: list[float] = []

    async def read_register(self, address: RegisterAddress) -> Result[RawRegister, DriveError]:
        del address
        self.instants.append(float(self.clock.monotonic()))
        return Ok(RawRegister(1))


class Ticks:
    """The gate's side of the bargain, alone: one permission per completed tick, or none."""

    def __init__(self, refused: frozenset[int]) -> None:
        self.refused: frozenset[int] = refused
        # The completed tick whose permission stands, the count, and the tick event.
        self.standing: int | None = None
        self.completed: int = 0
        self.ticked: asyncio.Event = asyncio.Event()

    def complete(self) -> None:
        self.standing = self.completed
        self.completed += 1
        self.ticked.set()

    def permitted(self) -> bool:
        standing = self.standing
        self.standing = None
        return standing is not None and standing not in self.refused

    async def next_tick(self, bound: Seconds) -> None:
        self.ticked.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.ticked.wait(), bound)


def _source(clock: ManualClock, ticks: Ticks) -> tuple[DriveInputStoSource, ReadPort]:
    port = ReadPort(clock)
    source = DriveInputStoSource(
        clock=clock, drive=port, address=LOGIC_INPUTS_LOGICAL, input_=LogicInput.LI1
    )
    source.guard_polling(ticks.permitted)
    return source, port


async def _polling_task(clock: ManualClock, ticks: Ticks) -> list[float]:
    """The console's own task (``DriveInputStoSource.run``), ticks every TICK."""
    source, port = _source(clock, ticks)
    source.pace_polling(ticks.next_tick)
    task = asyncio.create_task(source.run())
    for _ in range(TICKS):
        await asyncio.sleep(TICK)
        ticks.complete()
    await asyncio.sleep(TICK / 2)  # the poll after the last tick, then no further tick
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return port.instants


async def _harness(clock: ManualClock, ticks: Ticks) -> list[float]:
    """The harness's poll after each completed tick (:func:`poll_after_tick`)."""
    source, port = _source(clock, ticks)
    last: Monotonic | None = None
    for _ in range(TICKS):
        clock.advance(TICK)
        ticks.complete()
        last = await poll_after_tick(source, last, clock.monotonic())
    return port.instants


@pytest.mark.parametrize(
    "refused",
    [
        frozenset[int](),
        frozenset(range(10, 18)),
        frozenset(range(1, TICKS, 2)),
        frozenset({3}),
        frozenset({0, 1, 2, 3, 4}),
    ],
    ids=["always", "a_stop", "every_other_tick", "a_refused_due_tick", "late_start"],
)
def test_the_harness_reads_at_the_instants_of_the_console_polling_task(
    refused: frozenset[int],
) -> None:
    task_clock = ManualClock(Monotonic(100.0))
    task = run_virtual(task_clock, _polling_task(task_clock, Ticks(refused)))
    harness = asyncio.run(_harness(ManualClock(Monotonic(100.0)), Ticks(refused)))
    assert len(task) >= TICKS // 5
    assert harness == pytest.approx(task, abs=1e-9)


def test_the_harness_reads_once_every_three_ticks_while_nothing_withholds_them() -> None:
    result = _run()
    read = [row.t for row, view in _pairs(result) if view.reads and row.t < STOP_AT]
    assert all(view.reads <= 1 for view in result.sto)
    assert {round(later - earlier, 6) for earlier, later in pairwise(read)} == {round(3 * TICK, 6)}


# -- the scenario key --------------------------------------------------------------


def test_the_sto_source_key_chooses_the_reading_and_refuses_anything_else() -> None:
    base = {"name": "keyed", "kind": "manual", "duration_s": 1}

    def variant(document: Mapping[str, object]) -> StoVariant:
        parsed = parse_scenario(json.dumps(document))
        assert isinstance(parsed, Ok)
        return parsed.value.sto

    assert variant(base) is StoVariant.SIM_READY
    assert variant({**base, "sto_source": "sim_ready"}) is StoVariant.SIM_READY
    assert variant({**base, "sto_source": "drive_input"}) is StoVariant.DRIVE_INPUT
    for value in ("none", "sim_open", "DRIVE_INPUT"):
        refused = parse_scenario(json.dumps({**base, "sto_source": value}))
        assert isinstance(refused, Err)
        assert "sto_source" in refused.error.detail
