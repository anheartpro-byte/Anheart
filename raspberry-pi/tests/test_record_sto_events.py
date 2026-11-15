"""A1-20: the record says what the console read of the safety chain, at each change.

One ``sto`` event at the first tick, then one per change of state or source,
never one per read: the record keeps what a replay needs to read the chain as
the console did (``simulation/replay.py``), and stays within its bounds. Its
detail is closed, a state and a source of the console's own enums, and nothing
that is not exactly that reads back as one.
"""

from __future__ import annotations

from itertools import product

import pytest

from src.motor.sto import StoSourceKind, StoState
from src.record.reader import LONGEST_LINE
from src.record.schema import read_sto_detail, sto_detail


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
