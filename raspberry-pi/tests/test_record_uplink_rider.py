"""ANH-84: a session started at the machine is declared to the dashboard with its rider.

The rider's identifier (the record's ``subject_id``, a pseudonym) and, while
the console still holds it in memory, their name as the riders' list gave it.
After a restart the name is gone, never having been written: the session is
declared with the identifier alone, which is what attributes it on the
dashboard. A bench session names nobody.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Final

from src.record.upload import read_head
from src.record.writer import Writer
from src.record_uplink import LOCAL_PATH, Held, declaration_from
from src.result import Ok
from src.units import Seconds
from tests.record_uplink_support import armed, bench, described, launched_programme
from tests.roster_support import RIDER_ID, RIDER_NAME

DOWN: Final[Held] = Held("no route to host")


def everything_on_disk(root: Path) -> bytes:
    """Every byte of every file under ``root``."""
    return b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


def test_a_manifest_alone_names_its_rider_by_identifier_only(tmp_path: Path) -> None:
    created = Writer.create(
        tmp_path,
        dataclasses.replace(
            described(bench(tmp_path / "unused").clock, "ref-rider"),
            occupancy="occupied",
            subject_id=RIDER_ID,
        ),
    )
    assert isinstance(created, Ok)
    head = read_head(created.value.path)
    assert isinstance(head, Ok)

    declaration = declaration_from(head.value)

    assert (declaration.user_id, declaration.subject_label) == (RIDER_ID, None)


def test_a_manifest_that_names_nobody_declares_nobody(tmp_path: Path) -> None:
    head = read_head(launched_programme(tmp_path))
    assert isinstance(head, Ok)
    assert head.value.subject_id is None
    assert declaration_from(head.value).user_id is None


async def test_a_session_for_a_rider_is_declared_with_their_identifier_and_name(
    tmp_path: Path,
) -> None:
    b = bench(tmp_path, recording=False)
    session = armed(b.clock)
    b.uplink.begin(
        dataclasses.replace(
            session,
            declaration=dataclasses.replace(
                session.declaration,
                occupancy="occupied",
                user_id=RIDER_ID,
                subject_label=RIDER_NAME,
            ),
        )
    )

    await b.step()

    declared = b.link.to(LOCAL_PATH)[0]
    assert (declared["occupancy"], declared["userId"], declared["subjectLabel"]) == (
        "occupied",
        RIDER_ID,
        RIDER_NAME,
    )


async def test_a_bench_session_declares_no_rider(tmp_path: Path) -> None:
    b = bench(tmp_path, recording=False)
    b.uplink.begin(armed(b.clock))

    await b.step()

    declared = b.link.to(LOCAL_PATH)[0]
    assert declared["occupancy"] == "bench"
    assert "userId" not in declared
    assert "subjectLabel" not in declared


async def test_after_a_restart_the_session_is_declared_for_its_rider_without_their_name(
    tmp_path: Path,
) -> None:
    # Given a session of a rider the dashboard never heard of: the link was down
    # from its start, and the console was then restarted.
    b = bench(tmp_path)
    b.link.answer(LOCAL_PATH, DOWN)
    session = armed(b.clock)
    b.uplink.begin(
        dataclasses.replace(
            session,
            declaration=dataclasses.replace(
                session.declaration,
                occupancy="occupied",
                user_id=RIDER_ID,
                subject_label=RIDER_NAME,
            ),
        )
    )
    created = Writer.create(
        tmp_path,
        dataclasses.replace(
            described(b.clock, "ref-rider"), occupancy="occupied", subject_id=RIDER_ID
        ),
    )
    assert isinstance(created, Ok)
    b.disk.current = created.value.path
    await b.step()
    assert [body["userId"] for body in b.link.to(LOCAL_PATH)] == [RIDER_ID]

    # When the restarted console reaches the dashboard.
    del b.link.answers[LOCAL_PATH]
    restarted = b.restarted()
    restarted.clock.advance(Seconds(60.0))
    await restarted.run(2)

    # Then the session is declared for the rider, by identifier: their name was
    # in the memory of the console that is gone, and never on its disk.
    declared = restarted.link.to(LOCAL_PATH)[-1]
    assert declared["localRef"] == "ref-rider"
    assert declared["userId"] == RIDER_ID
    assert "subjectLabel" not in declared
    assert RIDER_NAME.encode() not in everything_on_disk(tmp_path)
