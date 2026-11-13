"""ANH-164: the instrument of the endurance scenario, and its judgement.

What is read from the process and from the data folder, each on a case built
by hand, then every threshold on a run that holds and on a run that breaks it:
a judgement that cannot fail proves nothing.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from simulation.endurance_measures import (
    BYTES_PER_SECOND_RESERVED,
    CSV_HEADER,
    EDGES,
    KIB,
    LOGBOOK_LIMIT,
    MIB,
    SAMPLES_SHOWN,
    WARM_UP_S,
    Check,
    DataFolder,
    DataState,
    Footprint,
    Gaps,
    Histogram,
    Kind,
    Mode,
    Quantiles,
    Run,
    Sample,
    Sampler,
    Session,
    Status,
    Sync,
    check_data,
    check_descriptors,
    check_rotation,
    check_rss,
    check_sessions,
    check_tick,
    check_tick_gap,
    darwin_memory,
    document,
    footprint,
    judge,
    judged,
    markdown,
    observed,
    open_descriptors,
    passed,
    per_hour,
    progress,
    resident_bytes,
    said_by,
    samples_from_csv,
    warm_up,
    write_report,
)
from simulation.harness import PANEL_SAFETY
from simulation.tests.conftest import document as parsed
from simulation.tests.conftest import obj, seq
from src.record.cursor import BASELINE_NAME, CURSOR_SUFFIX
from src.record.logbook import DIRECTORY as LOGBOOK
from src.record.retention import CHECKSUMS, confirm_deposit
from src.result import Ok
from src.units import UnixMillis

OPEN = "2026-10-10T101500Z_0123456789abcdef"
CLOSED = "2026-10-10T091500Z_fedcba9876543210"

# =========================================================================
# The tick
# =========================================================================


def test_ex1_the_histogram_never_understates_a_centile_and_costs_the_same_whatever_it_counted() -> (
    None
):
    ticks = Histogram()
    assert ticks.summary() is None, "nothing counted, nothing said"
    for _ in range(98):
        ticks.add(1.0)
    ticks.add(40.0)
    ticks.add(200.0)

    said = ticks.summary()

    assert said is not None
    assert said.count == 100
    # The upper edge of the bucket: at or above the true value, at most 5 % over it.
    assert 1.0 <= said.p50 <= 1.05
    assert 40.0 <= said.p99 <= 42.0
    assert said.highest == 200.0
    # One value: every centile is that value, never an edge above it.
    lone = Histogram()
    lone.add(3.3)
    alone = lone.summary()
    assert alone == Quantiles(count=1, p50=3.3, p99=3.3, highest=3.3)
    # A stall longer than the last edge is still counted, in the last bucket.
    lone.add(EDGES[-1] * 10)
    stalled = lone.summary()
    assert stalled is not None
    assert stalled.p99 == EDGES[-1]
    assert stalled.highest == EDGES[-1] * 10
    ticks.reset()
    assert ticks.summary() is None


# =========================================================================
# The process
# =========================================================================


def test_ex1_resident_memory_is_read_from_proc_and_asked_elsewhere_where_there_is_no_proc(
    tmp_path: Path,
) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmPeak:\t  999 kB\nVmRSS:\t   51200 kB\n", encoding="ascii")

    from_proc = resident_bytes(status=status)
    elsewhere = resident_bytes(status=tmp_path / "absent", elsewhere=lambda pid: pid * 0 + 2048)

    assert from_proc == 51200 * KIB
    assert elsewhere == 2048
    status.write_text("Name:\tkthreadd\n", encoding="ascii")
    with pytest.raises(ValueError, match="no VmRSS line"):
        resident_bytes(status=status)


FOOTPRINT_REPORT = """======================================================================
python [26931]: 64-bit    Footprint: 146965200 B (16384 bytes per page)
======================================================================

Auxiliary data:
    phys_footprint: 146965200 B
    phys_footprint_peak: 149078736 B
"""


def test_ex1_on_macos_the_physical_footprint_is_read_and_ps_only_as_a_last_resort(
    tmp_path: Path,
) -> None:
    """The resident size macOS prints falls when it compresses pages: it cannot show a leak."""
    tool = tmp_path / "footprint"
    tool.write_text("", encoding="ascii")
    asked: list[Sequence[str]] = []

    def answering(report: str) -> Callable[[Sequence[str]], str]:
        def said(command: Sequence[str]) -> str:
            asked.append(command)
            return "  87632\n" if command[0] == "/bin/ps" else report

        return said

    counted = darwin_memory(4242, footprint=tool, said=answering(FOOTPRINT_REPORT))
    unreadable = darwin_memory(4242, footprint=tool, said=answering("footprint: no such line\n"))
    without = darwin_memory(4242, footprint=tmp_path / "absent", said=answering(""))

    assert counted == 146_965_200, "the compressed pages are counted: not the resident 87 632 KiB"
    assert asked[0] == [str(tool), "-f", "bytes", "--noCategories", "4242"]
    assert unreadable == 87_632 * KIB
    assert without == 87_632 * KIB
    assert asked[-1] == ["/bin/ps", "-o", "rss=", "-p", "4242"]


def test_ex1_this_process_can_be_read_on_the_machine_the_tests_run_on() -> None:
    """The real sources, whatever the system: a number of megabytes, a handful of descriptors."""
    listed = said_by(["/bin/ps", "-o", "rss=", "-p", str(os.getpid())])
    resident = resident_bytes()
    measured = darwin_memory(os.getpid())
    descriptors = open_descriptors()

    assert int(listed.strip()) > 1000
    assert 10 * MIB < resident < 4096 * MIB
    assert 10 * MIB < measured < 4096 * MIB
    assert descriptors >= 3


def test_ex1_open_descriptors_are_counted_where_the_system_lists_them(tmp_path: Path) -> None:
    listed = tmp_path / "fd"
    listed.mkdir()
    for name in ("0", "1", "2", "7"):
        (listed / name).write_text("", encoding="ascii")

    from_proc = open_descriptors(listed=listed)
    from_dev = open_descriptors(listed=tmp_path / "absent", fallback=listed)

    assert from_proc == 4
    assert from_dev == 4


# =========================================================================
# The data folder
# =========================================================================


def record(root: Path, name: str, *, blocks: int, closed: bool) -> Path:
    """A record directory as the writer leaves it: streams, raw blocks, checksums once closed."""
    folder = root / name
    (folder / "ecg_raw").mkdir(parents=True)
    (folder / "manifest.json").write_text("{}", encoding="utf-8")
    (folder / "ticks.csv").write_text("t\n" * 50, encoding="utf-8")
    for index in range(blocks):
        (folder / "ecg_raw" / f"{index:06d}.bin").write_bytes(b"\x00" * 300)
    if closed:
        (folder / CHECKSUMS).write_text("0" * 64 + "  manifest.json\n", encoding="utf-8")
    return folder


def test_ex1_a_footprint_counts_bytes_blocks_and_inodes_and_survives_a_file_that_goes_away(
    tmp_path: Path,
) -> None:
    folder = record(tmp_path, OPEN, blocks=4, closed=False)

    taken = footprint(folder)
    one_file = footprint(folder / "manifest.json")
    nothing = footprint(tmp_path / "absent")

    # The directory, ecg_raw/, the manifest, the ticks and four blocks.
    assert taken.entries == 8
    assert taken.size == 2 + 100 + 4 * 300
    assert taken.allocated % 512 == 0
    assert one_file == Footprint(size=2, allocated=one_file.allocated, entries=1)
    assert nothing == Footprint()

    def vanishing(path: Path) -> os.stat_result:
        if path.name == "000002.bin":
            raise FileNotFoundError(path)
        return os.lstat(path)

    without = footprint(folder, stat=vanishing)
    assert without.entries == 7
    assert without.size == taken.size - 300


def test_ex1_the_data_folder_is_measured_record_by_record_and_a_closed_record_only_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    closed = record(root, CLOSED, blocks=6, closed=True)
    record(root, OPEN, blocks=2, closed=False)
    (root / (CLOSED + CURSOR_SUFFIX)).write_text("{}", encoding="utf-8")
    (root / BASELINE_NAME).write_text("{}", encoding="utf-8")
    (root / LOGBOOK).mkdir()
    (root / LOGBOOK / "events.jsonl").write_text("x" * 700, encoding="utf-8")
    folder = DataFolder(root)

    first = folder.measure()

    assert folder.root == root
    assert set(first.records) == {CLOSED, OPEN}
    assert first.records[CLOSED].entries == 11  # folder, ecg_raw/, manifest, ticks, checksums, 6
    assert first.records[OPEN].entries == 6
    assert (first.closed, first.deposited, first.purged, first.stray) == (1, 0, 0, ())
    assert first.logbook.size == 700
    # The root, both records, the cursor, the baseline, the logbook and its file.
    assert first.total.entries == 1 + 11 + 6 + 1 + 1 + 2

    # The open record grows and is walked again; the closed one is not.
    (root / OPEN / "ecg_raw" / "000002.bin").write_bytes(b"\x00" * 300)
    (closed / "ecg_raw" / "added-after-the-close.bin").write_bytes(b"\x00" * 300)
    second = folder.measure()
    assert second.records[OPEN].entries == 7
    assert second.records[CLOSED] == first.records[CLOSED]


def test_ex2_what_is_left_next_to_the_records_is_named_and_a_confirmed_deposit_is_counted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    closed = record(root, CLOSED, blocks=1, closed=True)
    (root / ("2026-01-01T000000Z_gone" + CURSOR_SUFFIX)).write_text("{}", encoding="utf-8")
    (root / "core.dump").write_text("x", encoding="utf-8")
    folder = DataFolder(root)

    before = folder.measure()
    confirmed = confirm_deposit(closed, "storage-1", UnixMillis(1_790_000_000_000))
    after = folder.measure()

    # A cursor whose record is gone is not "the cursor of a record": it is named too.
    assert before.stray == ("2026-01-01T000000Z_gone" + CURSOR_SUFFIX, "core.dump")
    assert before.deposited == 0
    assert isinstance(confirmed, Ok)
    assert after.deposited == 1
    assert after.stray == before.stray, "the deposit marker of a record is expected next to it"


@pytest.mark.parametrize(
    "kind", ["valid", "unexpected", "directory", "link", "root_file", "root_link", "unreadable"]
)
def test_configuration_documents_are_counted_separately_and_unknown_entries_stay_stray(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    root = tmp_path / "records"
    directory = root / "config"
    directory.mkdir(parents=True)
    document = directory / ("a" * 64 + ".json")
    document.write_bytes(b"{}")
    expected: tuple[str, ...] = ()
    if kind == "unexpected":
        (directory / "unexpected.json").write_bytes(b"x")
        expected = ("config/unexpected.json",)
    elif kind == "directory":
        document.unlink()
        document.mkdir()
        expected = ("config/" + document.name,)
    elif kind == "link":
        document.unlink()
        document.symlink_to(tmp_path / "absent")
        expected = ("config/" + document.name,)
    elif kind in {"root_file", "root_link"}:
        document.unlink()
        directory.rmdir()
        if kind == "root_file":
            directory.write_bytes(b"x")
        else:
            directory.symlink_to(tmp_path / "absent")
        expected = ("config",)
    elif kind == "unreadable":
        original = Path.iterdir

        def denied(path: Path) -> Iterator[Path]:
            if path == directory:
                raise PermissionError(path)
            return original(path)

        monkeypatch.setattr(Path, "iterdir", denied)
        expected = ("config",)
    found = DataFolder(root).measure()
    assert found.stray == expected
    assert found.records == {}
    assert found.total.entries == 1 + found.configuration.entries
    assert found.total.size == found.configuration.size


def test_ex2_a_record_the_retention_removed_is_counted_as_purged(tmp_path: Path) -> None:
    root = tmp_path / "records"
    closed = record(root, CLOSED, blocks=1, closed=True)
    folder = DataFolder(root)
    assert folder.measure().closed == 1

    shutil.rmtree(closed)
    after = folder.measure()

    assert (after.closed, after.purged, dict(after.records)) == (0, 1, {})
    missing = DataFolder(tmp_path / "never-created").measure()
    assert missing.total == Footprint(entries=1)
    assert missing.records == {}


def test_ex1_a_reading_holds_the_four_measures(tmp_path: Path) -> None:
    root = tmp_path / "records"
    record(root, OPEN, blocks=3, closed=False)
    tick = Quantiles(count=5, p50=1.0, p99=2.0, highest=3.0)

    sampler = Sampler(DataFolder(root))
    reading = sampler.take(42.0, tick)
    # Counted at every reading, not once for the run: a descriptor opened since is seen.
    with (tmp_path / "held").open("w", encoding="ascii"):
        held = sampler.take(43.0, None)

    assert held.descriptors == reading.descriptors + 1
    assert reading.at == 42.0
    assert reading.rss > 10 * MIB
    assert reading.descriptors >= 3
    assert reading.data_entries == 1 + 7
    assert reading.data_bytes > 0
    assert reading.blocks is not None
    assert reading.blocks > 1000
    assert reading.tick is tick
    # What the run says of it as it goes, one line a reading.
    said = progress(replace(reading, rss=150 * MIB, descriptors=9, data_bytes=3 * MIB))
    assert said == (
        "0 h 01 : 150.0 Mo résidents, 9 descripteurs, 3.0 Mo et 8 fichiers de données, "
        "tic p50 / p99 1.00 / 2.00 ms\n"
    )


# =========================================================================
# The judgement
# =========================================================================


def reading(at: float, *, rss: float = 100.0, descriptors: int = 12) -> Sample:
    return Sample(
        at=at,
        rss=round(rss * MIB),
        descriptors=descriptors,
        data_bytes=round(at) * 20_000,
        data_entries=round(at) * 5,
    )


def session(index: int) -> Session:
    return Session(
        index=index,
        kind=Kind.MANUAL if index % 2 else Kind.PROGRAMME,
        started_at=index * 1000.0,
        recorded=600.0,
        end="operator_stop",
        as_planned=True,
        delivered=True,
        points=600,
        record=Footprint(size=3_000_000, allocated=14_000_000, entries=3008),
    )


STEADY_DATA = DataState(
    total=Footprint(size=6_100_000, allocated=28_500_000, entries=6022),
    records={CLOSED: Footprint(entries=3008), OPEN: Footprint(entries=3008)},
    closed=2,
    logbook=Footprint(size=900, allocated=8192, entries=2),
    stray=(),
    deposited=2,
    purged=1,
)


def steady(hours: float = 24.0) -> Run:
    """A day in good health: flat memory, a short tick, descriptors that come and go."""
    covered = hours * 3600
    count = 96
    return Run(
        mode=Mode.SIMULATED,
        requested=covered,
        covered=covered,
        wall=3000.0,
        cpu=2800.0,
        samples=tuple(
            reading(covered * (index + 1) / count, rss=100 + index % 3, descriptors=12 + index % 4)
            for index in range(count)
        ),
        tick=Quantiles(count=432_000, p50=0.9, p99=4.5, highest=80.0),
        sessions=(session(1), session(2)),
        sync=Sync(
            heartbeats=8640,
            health_reports=8640,
            max_heartbeat_gap_ms=10_000,
            points=1200,
            rejected=0,
            duplicates=0,
            owed=0,
        ),
        data=STEADY_DATA,
        notes=("santé absente du heartbeat",),
    )


def by_name(checks: tuple[Check, ...]) -> dict[str, Check]:
    return {check.name: check for check in checks}


def test_ex2_a_day_in_good_health_passes_every_threshold_including_rotation() -> None:
    checks = judge(steady())

    statuses = {check.name: check.status for check in checks}
    assert statuses == {
        "rss": Status.PASS,
        "tick": Status.PASS,
        "descriptors": Status.PASS,
        "data": Status.PASS,
        "rotation": Status.PASS,
        "sessions": Status.PASS,
    }
    assert passed(checks)
    assert "2 dépôts confirmés, 1 enregistrements purgés" in by_name(checks)["rotation"].measured


def test_ex2_the_first_hour_is_left_out_and_a_short_run_leaves_out_its_first_quarter() -> None:
    assert warm_up(24 * 3600.0) == WARM_UP_S
    assert warm_up(2 * 3600.0) == WARM_UP_S
    assert warm_up(600.0) == 150.0
    # What the first hour does is not judged: caches fill, the DSP is loaded.
    run = steady()
    early = tuple(
        replace(sample, rss=sample.rss // 3) if sample.at < WARM_UP_S else sample
        for sample in run.samples
    )

    verdict = check_rss(replace(run, samples=early))

    assert verdict.status is Status.PASS


def test_ex2_memory_that_grows_by_more_than_a_tenth_after_the_first_hour_fails() -> None:
    run = steady()
    leaking = tuple(
        replace(sample, rss=round((100 + 15 * sample.at / run.covered) * MIB))
        for sample in run.samples
    )
    shrinking = tuple(
        replace(sample, rss=round((100 - 15 * sample.at / run.covered) * MIB))
        for sample in run.samples
    )

    grown = check_rss(replace(run, samples=leaking))
    shrunk = check_rss(replace(run, samples=shrinking))
    within = check_rss(replace(run, samples=leaking[:6]))

    assert grown.status is Status.FAIL
    assert "écart maximal 14." in grown.measured
    assert shrunk.status is Status.FAIL, "the ticket says stable, within ten per cent either way"
    assert within.status is Status.FAIL
    assert "trop peu" in within.measured, "a run too short to judge is not a run that passes"


def test_ex2_memory_is_held_to_ten_per_cent_exactly() -> None:
    """At 10 % from the first reading after the warm-up it passes, at 11 % it fails."""
    flat = tuple(replace(sample, rss=100 * MIB) for sample in steady().samples)

    def peak(rss_mib: int) -> Check:
        top = tuple(
            replace(sample, rss=rss_mib * MIB) if index == len(flat) // 2 else sample
            for index, sample in enumerate(flat)
        )
        return check_rss(replace(steady(), samples=top))

    assert peak(110).status is Status.PASS
    assert peak(90).status is Status.PASS
    assert peak(111).status is Status.FAIL
    assert peak(89).status is Status.FAIL
    assert "écart maximal 11.0%" in peak(111).measured


def test_ex2_a_tick_whose_99th_centile_reaches_150_ms_fails() -> None:
    run = steady()

    slow = check_tick(replace(run, tick=Quantiles(count=1000, p50=2.0, p99=150.0, highest=400.0)))
    absent = check_tick(replace(run, tick=None))

    assert slow is not None
    assert slow.status is Status.FAIL
    assert "p99 150.00 ms" in slow.measured
    assert absent is None, "a console read from outside has no timed tick: nothing is claimed"


def test_ex2_a_descriptor_that_leaks_fails_and_descriptors_that_come_and_go_do_not() -> None:
    run = steady()
    leaking = tuple(
        replace(sample, descriptors=12 + index // 10) for index, sample in enumerate(run.samples)
    )

    fine = check_descriptors(run)
    leak = check_descriptors(replace(run, samples=leaking))
    few = check_descriptors(replace(run, samples=run.samples[:6]))

    assert fine.status is Status.PASS
    assert fine.measured == (
        "12 au début, au moins 12 sur la fin (5 relevés depuis 23 h 00) ; au plus 15, 15 à la fin"
    )
    assert leak.status is Status.FAIL
    # The low before the end is above its lowest tenth: 7 readings at 12, then 13.
    assert "13 au début, au moins 21 sur la fin" in leak.measured
    assert few.status is Status.FAIL
    assert "trop peu" in few.measured


def counted(run: Run, descriptors: Callable[[float], int]) -> Run:
    """``run`` with, at each reading, the descriptors ``descriptors`` gives for its instant."""
    return replace(
        run,
        samples=tuple(
            replace(sample, descriptors=descriptors(sample.at)) for sample in run.samples
        ),
    )


def leaking_from(start: float, every: float) -> Callable[[float], int]:
    """Twelve descriptors open, and one more leaked every ``every`` seconds from ``start`` on."""
    return lambda at: 12 if at < start else 13 + int((at - start) // every)


TWO_HOURS = replace(steady(2.0), samples=tuple(reading(30.0 * (n + 1)) for n in range(240)))
"""The shape of the two real-time hours on the Pi: a reading every 30 s."""


def test_ex2_a_leak_that_starts_late_in_the_day_fails_unless_it_starts_in_its_last_hour() -> None:
    """The nightly day: a session every ~1900 s, one descriptor leaked by each."""
    day = steady()

    for start in (13 * 3600.0, 22 * 3600.0 + 45 * 60):
        late = check_descriptors(counted(day, leaking_from(start, 1900.0)))
        assert late.status is Status.FAIL, f"leak from {start} s"
    blind = check_descriptors(counted(day, leaking_from(23 * 3600.0 + 15 * 60, 1900.0)))

    assert blind.status is Status.PASS, "the documented blind window: the last hour"
    assert "(5 relevés depuis 23 h 00)" in blind.measured


def test_ex2_a_leak_that_starts_late_in_two_hours_fails_unless_in_their_last_quarter_hour() -> None:
    """The two real-time hours on the Pi: a cycle every ~11 minutes, one descriptor each."""
    for start in (91 * 60.0, 104 * 60.0):
        late = check_descriptors(counted(TWO_HOURS, leaking_from(start, 660.0)))
        assert late.status is Status.FAIL, f"leak from minute {start / 60:.0f}"
    blind = check_descriptors(counted(TWO_HOURS, leaking_from(106 * 60.0, 660.0)))

    assert blind.status is Status.PASS, "the documented blind window: the last 15 minutes"
    assert "(31 relevés depuis 1 h 45)" in blind.measured


def test_ex2_a_descriptor_open_or_closed_for_one_reading_fails_nothing() -> None:
    flat = counted(steady(), lambda _at: 12)
    start = len(flat.samples) - len(judged(flat))

    def one(index: int, descriptors: int) -> Check:
        return check_descriptors(
            replace(
                flat,
                samples=tuple(
                    replace(sample, descriptors=descriptors) if number == index else sample
                    for number, sample in enumerate(flat.samples)
                ),
            )
        )

    for index in (start + 3, 50, len(flat.samples) - 3, len(flat.samples) - 1):
        for descriptors in (40, 11):
            verdict = one(index, descriptors)
            assert verdict.status is Status.PASS, (index, descriptors, verdict.measured)
    # Before the end, a tenth of the readings (8 of 88 here) may be caught with one closed.
    before = range(start, len(flat.samples) - 5)
    for lows, status in ((2, Status.PASS), (8, Status.PASS), (9, Status.FAIL)):
        closed = {before[n * 9] for n in range(lows)}
        dipped = replace(
            flat,
            samples=tuple(
                replace(sample, descriptors=11) if number in closed else sample
                for number, sample in enumerate(flat.samples)
            ),
        )
        assert check_descriptors(dipped).status is status, lows
    # The documented price of the lowest of the end: one reading there caught with a
    # descriptor closed hides a leak of one descriptor.
    leaked = counted(flat, lambda at: 13 if at >= 13 * 3600.0 else 12)
    hidden = replace(
        leaked,
        samples=tuple(
            replace(sample, descriptors=12) if number == len(flat.samples) - 2 else sample
            for number, sample in enumerate(leaked.samples)
        ),
    )
    assert check_descriptors(leaked).status is Status.FAIL
    assert check_descriptors(hidden).status is Status.PASS
    # Five readings judged, the last quarter holds one: the end is still two of them, and
    # the low of the three before it their middle one. One reading caught high at the end
    # and one caught low before it fail nothing.
    short = replace(
        steady(1.0),
        samples=(
            reading(600.0),
            reading(1000.0, descriptors=11),
            reading(1100.0),
            reading(1200.0),
            reading(1300.0),
            reading(2000.0, descriptors=40),
        ),
    )
    assert check_descriptors(short).status is Status.PASS


def test_ex2_four_readings_after_the_warm_up_are_too_few_for_the_descriptors() -> None:
    """With two readings before the end, their second lowest would be their highest: a
    leak would pass. Four readings are refused instead, and five keep three before it."""
    for counts in ((12, 40, 13, 13), (12, 13, 13, 13)):
        four = replace(
            steady(1.0),
            samples=tuple(
                reading(1000.0 + 100.0 * n, descriptors=count) for n, count in enumerate(counts)
            ),
        )
        verdict = check_descriptors(four)
        assert verdict.status is Status.FAIL, counts
        assert verdict.measured == "4 relevés après la chauffe : trop peu"
    # Five, the last three close together: three still come before the end, whose middle
    # one shows the leak that the higher of two would hide.
    clustered = replace(
        steady(1.0),
        samples=tuple(
            reading(at, descriptors=count)
            for at, count in ((1000.0, 12), (1100.0, 40), (1900.0, 12), (1950.0, 13), (2000.0, 13))
        ),
    )
    assert check_descriptors(clustered).status is Status.FAIL


STALL_MS = PANEL_SAFETY.loop_stall_freeze_gap * 1000.0
"""The gap at which the console's ``loop_stall`` rule freezes the arm, in ms."""

GAPS = Gaps(
    bound=STALL_MS,
    exact=Quantiles(count=36_000, p50=201.0, p99=229.0, highest=517.0),
    seen=86_400.0,
)
"""A real-time run in good health, as the page saw its ticks."""


def test_ex2_in_real_time_a_stall_fails_tick_gap_and_names_its_cause() -> None:
    run = replace(steady(), mode=Mode.REAL_TIME, gaps=GAPS)
    exact = GAPS.exact
    assert exact is not None
    over_merges = Quantiles(count=3, p50=420.0, p99=633.6, highest=633.6)

    fine = check_tick_gap(run)
    merged = check_tick_gap(replace(run, gaps=replace(GAPS, merged=over_merges)))
    reached = check_tick_gap(
        replace(run, gaps=replace(GAPS, exact=replace(exact, highest=STALL_MS)))
    )
    latched = check_tick_gap(replace(run, gaps=replace(GAPS, verdict="took 1.00 s between ticks")))
    unread = check_tick_gap(replace(run, gaps=replace(GAPS, exact=None)))
    only_merged = check_tick_gap(replace(run, gaps=replace(GAPS, exact=None, merged=over_merges)))
    partial = check_tick_gap(replace(run, gaps=replace(GAPS, seen=0.89 * 86_400)))

    assert fine is not None
    assert fine.status is Status.PASS
    assert fine.threshold == (
        "aucun verdict loop_stall de la console ; écart entre deux tics consécutifs < 600 ms ; "
        "tics relevés sur 90% de l'essai au moins"
    )
    assert (
        fine.measured == "p50 201.0 ms, p99 229.0 ms, maximum 517.0 ms sur 36000 écarts consécutifs"
    )
    # A gap over snapshots the hub merged adds two real ones: it is said, not judged a stall.
    assert merged is not None
    assert merged.status is Status.PASS
    assert "3 écarts sur des instantanés fusionnés par le hub (au plus 633.6 ms)" in merged.measured
    # A page that only ever read merged snapshots: the console's verdict decides alone.
    assert only_merged is not None
    assert only_merged.status is Status.PASS
    for verdict, cause in (
        (reached, "un écart de 600.0 ms entre deux tics consécutifs"),
        (latched, "la console a levé loop_stall : took 1.00 s between ticks"),
        (unread, "aucun écart entre deux tics relevé"),
        (partial, "tics relevés sur 76896 s des 86400 s de l'essai"),
    ):
        assert verdict is not None
        assert verdict.status is Status.FAIL
        assert verdict.measured.startswith(cause)
    assert check_tick_gap(steady()) is None, "on the manual clock every gap is 200 ms"
    assert [check.name for check in judge(run)] == [
        "rss",
        "tick",
        "tick_gap",
        "descriptors",
        "data",
        "rotation",
        "sessions",
    ]


def test_ex2_the_data_folder_is_held_to_what_the_arming_gate_plans_for() -> None:
    run = steady()
    full = replace(session(1), record=Footprint(size=1, allocated=1, entries=3093))
    crowded = replace(full, record=Footprint(size=1, allocated=1, entries=3094))
    heavy = replace(session(2), record=Footprint(allocated=round(BYTES_PER_SECOND_RESERVED * 700)))
    logbook = replace(STEADY_DATA, logbook=Footprint(size=LOGBOOK_LIMIT + 1))
    littered = replace(STEADY_DATA, stray=("core.dump", ".sync-0.tmp"))

    fine = check_data(run)
    at_limit = check_data(replace(run, sessions=(full, session(2))))
    too_many = check_data(replace(run, sessions=(crowded, session(2))))
    too_heavy = check_data(replace(run, sessions=(session(1), heavy)))
    long_logbook = check_data(replace(run, data=logbook))
    stray = check_data(replace(run, data=littered))
    unread = check_data(replace(run, data=None))
    idle = check_data(replace(run, sessions=()))

    assert fine is not None
    assert fine.status is Status.PASS
    assert at_limit is not None
    assert at_limit.status is Status.PASS
    # 600 s of session: 3008 files, as many as the gate plans; an hour is 18 048.
    assert "18048 fichiers par heure de séance" in fine.measured
    assert too_many is not None
    assert (too_many.status, "séance 1 : 3094 fichiers" in too_many.measured) == (Status.FAIL, True)
    assert too_heavy is not None
    assert (too_heavy.status, "séance 2" in too_heavy.measured) == (Status.FAIL, True)
    assert long_logbook is not None
    assert (long_logbook.status, "journal hors séance" in long_logbook.measured) == (
        Status.FAIL,
        True,
    )
    assert stray is not None
    assert (stray.status, "core.dump, .sync-0.tmp" in stray.measured) == (Status.FAIL, True)
    assert unread is None
    assert idle is not None
    assert "aucune séance enregistrée" in idle.measured
    assert per_hour(replace(run, sessions=())) is None


def test_ex2_rotation_requires_confirmation_and_observed_purge_on_long_runs() -> None:
    run = steady()
    deposited = replace(STEADY_DATA, deposited=1)
    purged = replace(STEADY_DATA, closed=1, deposited=1, purged=1)
    empty = replace(STEADY_DATA, closed=0, purged=0)

    today = check_rotation(run)
    one_day = check_rotation(replace(run, data=deposited))
    after_a_purge = check_rotation(replace(run, data=purged))

    assert today is not None
    assert today.status is Status.PASS
    assert one_day is not None
    assert one_day.status is Status.FAIL
    assert after_a_purge is not None
    assert after_a_purge.status is Status.PASS
    assert not passed((one_day,))
    no_purge = check_rotation(replace(run, data=replace(STEADY_DATA, purged=0)))
    assert no_purge is not None
    assert no_purge.status is Status.FAIL
    short_run = check_rotation(replace(run, covered=30, data=replace(STEADY_DATA, purged=0)))
    assert short_run is not None
    assert short_run.status is Status.PASS
    missing = check_rotation(replace(run, data=empty))
    assert missing is not None
    assert missing.status is Status.FAIL
    lost = check_rotation(replace(run, data=replace(empty, deposited=0, purged=1)))
    assert lost is not None
    assert lost.status is Status.FAIL
    assert check_rotation(replace(run, sessions=(), data=empty)) is None
    assert check_rotation(replace(run, data=None)) is None


def test_ex2_a_session_that_ends_otherwise_or_never_reaches_the_dashboard_fails() -> None:
    run = steady()
    sync = run.sync
    assert sync is not None

    fine = check_sessions(run)
    stopped = check_sessions(replace(run, anomaly="POST /api/manual/start: HTTP 409"))
    none = check_sessions(replace(run, sessions=()))
    tripped = replace(session(1), end="safety_verdict", as_planned=False)
    lost = replace(session(2), delivered=False)
    unplanned = check_sessions(replace(run, sessions=(tripped, session(2))))
    undelivered = check_sessions(replace(run, sessions=(session(1), lost)))
    refused = check_sessions(replace(run, sync=replace(sync, rejected=3, owed=1)))
    missing_health = check_sessions(replace(run, sync=replace(sync, health_reports=0)))
    delayed_health = check_sessions(replace(run, sync=replace(sync, max_heartbeat_gap_ms=12_000)))
    outside = check_sessions(replace(run, sync=None))

    assert fine is not None
    assert fine.status is Status.PASS
    assert "2 séances (1 manuelles, 1 programmées)" in fine.measured
    for verdict, said in (
        (stopped, "arrêt du scénario : POST /api/manual/start: HTTP 409"),
        (none, "aucune séance"),
        (unplanned, "1 fins imprévues (séance 1 : safety_verdict)"),
        (undelivered, "séances non livrées : [2]"),
        (refused, "3 points refusés, 1 envois encore dus"),
        (missing_health, "santé absente"),
        (delayed_health, "11 secondes"),
    ):
        assert verdict is not None
        assert verdict.status is Status.FAIL
        assert said in verdict.measured
    assert outside is None


# =========================================================================
# The report
# =========================================================================


def test_ex3_the_report_keeps_every_reading_and_says_what_was_not_active(tmp_path: Path) -> None:
    exact = Quantiles(count=10, p50=200.0, p99=212.0, highest=260.0)
    run = replace(steady(), gaps=Gaps(bound=STALL_MS, exact=exact, seen=86_400.0))
    checks = judge(run)

    data, summary = write_report(run, checks, tmp_path / "out")
    report = parsed(data.read_text(encoding="utf-8"))
    text = summary.read_text(encoding="utf-8")

    assert (report["scenario"], report["mode"], report["verdict"]) == (
        "endurance",
        "simulated",
        "PASS",
    )
    assert (report["requested_s"], report["wall_s"], report["cpu_s"]) == (86400.0, 3000.0, 2800.0)
    assert [obj(check)["name"] for check in seq(report["checks"])] == [
        "rss",
        "tick",
        "tick_gap",
        "descriptors",
        "data",
        "rotation",
        "sessions",
    ]
    assert len(seq(report["samples"])) == 96
    assert obj(seq(report["samples"])[0])["allocated_blocks"] is None
    assert obj(report["tick_ms"]) == {"count": 432_000, "p50": 0.9, "p99": 4.5, "max": 80.0}
    assert obj(report["tick_period_ms"])["p99"] == 212.0
    assert obj(report["tick_gaps"]) == {
        "bound_ms": 600.0,
        "merged_ms": None,
        "seen_s": 86_400.0,
        "loop_stall_verdict": None,
    }
    assert obj(report["data"])["bounded_by_rotation"] is True
    assert obj(obj(report["data"])["per_recorded_hour"])["entries"] == 18048
    assert obj(report["sync"])["heartbeats"] == 8640
    assert obj(report["sync"])["health_reports"] == 8640
    assert obj(report["sync"])["max_heartbeat_gap_ms"] == 10_000
    assert [obj(line)["kind"] for line in seq(report["sessions"])] == ["manual", "programme"]
    assert report["notes"] == ["santé absente du heartbeat"]
    assert text == markdown(run, checks)
    assert text.startswith("## Endurance de la console (simulated) : PASS\n")
    assert "| `rotation` | PASS |" in text
    assert "Écart entre deux tics : p50 200.0 ms, p99 212.0 ms, maximum 260.0 ms." in text
    assert "* santé absente du heartbeat" in text
    # A few readings in the summary, the last one always; all of them in the JSON.
    rows = [line for line in text.splitlines() if re.match(r"\| \d+ h \d\d \|", line)]
    assert len(rows) == SAMPLES_SHOWN
    assert rows[-1].startswith("| 24 h 00 |")


def test_ex3_a_run_read_from_outside_reports_only_what_was_read() -> None:
    readings = "\n".join(
        [
            "# relevé au banc, une ligne par minute",
            CSV_HEADER,
            "",
            *(
                f"{1_790_000_000 + 60 * minute},{51_200 + minute},14,{2000 * minute},{300 * minute}"
                for minute in range(10)
            ),
        ]
    )

    samples = samples_from_csv(readings)
    run = observed(samples, ("relevé à la main",))
    checks = judge(run)
    report = document(run, checks)
    text = markdown(run, checks)

    assert len(samples) == 10
    assert (samples[0].at, samples[-1].at) == (0.0, 540.0)
    assert samples[3].rss == 51_203 * KIB
    assert samples[3].data_bytes == 6000 * KIB
    assert samples[3].data_entries == 900
    assert (run.mode, run.covered, run.tick, run.data, run.sync) == (
        Mode.OBSERVED,
        540.0,
        None,
        None,
        None,
    )
    assert [check.name for check in checks] == ["rss", "descriptors"]
    assert passed(checks)
    assert (report["tick_ms"], report["data"], report["sync"]) == (None, None, None)
    assert "Écart entre deux tics" not in text
    assert "| 0 h 09 | 50.0 Mo | 14 |" in text
    assert "| - |" in text, "no tick was timed: the column says so"
    silent = markdown(replace(run, notes=()), checks)
    assert "Limites de cet essai" not in silent


def test_ex3_a_file_of_readings_that_is_not_five_integers_a_line_is_refused_with_its_line() -> None:
    with pytest.raises(ValueError, match="line 3: expected epoch_s,rss_kb"):
        samples_from_csv(f"{CSV_HEADER}\n1,2,3,4,5\n1,2,3,quatre,5\n")
    with pytest.raises(ValueError, match="line 1"):
        samples_from_csv("1,2,3\n")
    nothing = observed(samples_from_csv(CSV_HEADER), ())
    assert nothing.covered == 0.0
    assert check_rss(nothing).status is Status.FAIL
