"""A1-20: the record says what the console read of the safety chain, at each change.

One ``sto`` event at the first tick, then one per change of state or source,
never one per read: the record keeps what a replay needs to read the chain as
the console did (``simulation/replay.py``), and stays within its bounds. Its
detail is closed, a state and a source of the console's own enums, and nothing
that is not exactly that reads back as one.
"""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from src.motor.sto import SimulatedStoSource, StoSourceKind, StoState
from src.record.reader import LONGEST_LINE
from src.record.schema import EventKind, read_sto_detail, sto_detail
from tests.record_console_support import recorded_rig, set_target, start_bench


@pytest.mark.parametrize(("state", "source"), list(product(StoState, StoSourceKind)))
def test_every_state_and_source_reads_back_as_itself(
    state: StoState, source: StoSourceKind
) -> None:
    detail = sto_detail(state, source)
    assert read_sto_detail(detail) == (state, source)
    assert len(detail) < LONGEST_LINE // 512, "a few dozen bytes, far within a line"


@pytest.mark.parametrize(
    "detail",
    [
        "open",
        "open (",
        "open (drive_input",
        "open (Jeanne Martin)",
        "Jeanne (drive_input)",
        "open (drive_input) Jeanne",
        "open  (drive_input)",
        "",
    ],
)
def test_nothing_but_a_state_and_a_source_reads_as_a_chain(detail: str) -> None:
    assert read_sto_detail(detail) is None


async def test_the_record_tells_the_chain_once_then_at_its_change_never_at_each_read(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    source = recorded.rig.panel.sto
    assert isinstance(source, SimulatedStoSource)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        # Twenty seconds READY first: the session ends soon after the opening, whose
        # stop is over at the tick that sees the stage off, RFRD reading 0
        # (docs/securite.md, section 3).
        await recorded.tick(20.0)
        source.chain.open(recorded.clock.monotonic())
        await recorded.tick(10.0)
    recording = recorded.recording()
    told = [
        (event.detail, event.actor) for event in recording.events if event.kind is EventKind.STO
    ]
    assert told == [("ready (sim_ready)", "system"), ("open (sim_ready)", "system")]
    assert len(recording.rows) > 100, "read at every tick, told twice"
