"""What the endurance scenario measures, and how a run is judged (ANH-164).

:mod:`simulation.endurance` keeps the real console alive for hours; this module
is the instrument next to it. It reads four things, none of them through a
hook in the console:

* the **resident memory** of the process (``/proc/self/status``, or ``ps``
  where there is no ``/proc``);
* its **open file descriptors** (``/proc/self/fd``, or ``/dev/fd``);
* the **data folder**: what the file system gave it (allocated bytes) and how
  many files and directories it holds (inodes), record by record;
* the **duration of the control tick**, kept in a histogram of fixed size so
  that the instrument itself does not grow with the run.

And it judges a run against the thresholds of the ticket (:func:`judge`):

``rss``
    after the warm-up (the first hour, or the first quarter of a run shorter
    than two hours) every reading stays within 10 % of the first one;
``tick``
    the 99th centile of the tick stays under 150 ms;
``descriptors``
    the highest count of the second half of the run does not exceed the
    highest count of the first half: a descriptor that leaks grows without end;
``data``
    what the code guarantees today about the data folder: a record takes no
    more files than the arming gate plans for (five raw blocks a second), no
    more bytes a second than the reserve the gate keeps for the longest
    session, the logbook stays within its two files, and nothing else is left
    next to the records;
``rotation``
    the folder is bounded by the retention. **A documented known defect**
    (:data:`KNOWN_UNBOUNDED_DATA`): the retention removes a record only once its
    deposit off the machine is confirmed, and nothing confirms a deposit yet.
    Reported ``XFAIL`` while that is so; the day a deposit is confirmed the
    line turns ``FIXED?`` and the run fails until the true bound is written;
``sessions``
    every session ended the way the scenario planned it and reached the
    simulated dashboard whole, and the scenario met nothing it did not expect.

See .claude/skills/anheart-strict-python/SKILL.md.
"""

from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import sys
from bisect import bisect_left
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique
from itertools import accumulate
from pathlib import Path
from stat import S_ISDIR
from typing import Final

from src.record.cursor import BASELINE_NAME, CURSOR_SUFFIX
from src.record.journal import (
    FIXED_ENTRIES,
    LONGEST_SESSION,
    MIN_FREE_BYTES,
    RAW_BLOCKS_PER_SECOND,
)
from src.record.logbook import DIRECTORY as LOGBOOK
from src.record.logbook import MAX_BYTES as LOGBOOK_FILE_BYTES
from src.record.retention import CHECKSUMS, MARKER_SUFFIX, RECORD_NAME, confirmed_deposit

RSS_BAND: Final[float] = 0.10
"""After the warm-up the resident memory stays within this share of its first reading."""

TICK_P99_LIMIT_MS: Final[float] = 150.0
"""The 99th centile of the control tick, in milliseconds, stays under this."""

WARM_UP_S: Final[float] = 3600.0
"""The first hour is left out of the memory and descriptor judgements: caches fill."""

SHORT_RUN_WARM_UP: Final[float] = 0.25
"""A run shorter than two warm-ups leaves its first quarter out instead."""

MIN_JUDGED: Final[int] = 4
"""Readings needed after the warm-up before memory or descriptors can be judged."""

RECORD_SLACK_S: Final[float] = 5.0
"""Seconds a record may last beyond what the scenario timed of its session (it times
whole seconds, from the command to the first second the console is seen finished)."""

BYTES_PER_SECOND_RESERVED: Final[float] = MIN_FREE_BYTES / LONGEST_SESSION
"""What the arming gate keeps free, spread over the longest session it plans for."""

LOGBOOK_LIMIT: Final[int] = 2 * LOGBOOK_FILE_BYTES + 4096
"""The logbook is two files of at most :data:`~src.record.logbook.MAX_BYTES`, and a directory."""

KNOWN_UNBOUNDED_DATA: Final[str] = (
    "aucun dépôt hors machine n'est confirmé (le dépôt n'existe pas encore) : la rétention "
    "ne retire rien et le dossier grandit jusqu'au refus d'armer faute de place"
)
"""Why ``rotation`` is an expected failure today (see the module docstring)."""

KIB: Final[int] = 1024
MIB: Final[int] = 1024 * 1024

EDGES: Final[tuple[float, ...]] = tuple(0.01 * 10 ** (step / 50) for step in range(1, 351))
"""Upper edges of the histogram's buckets, in ms: 0.0105 ms to 100 s, 4.7 % apart."""

CSV_HEADER: Final[str] = "epoch_s,rss_kb,descriptors,data_kb,data_inodes"
"""The columns of a file of readings taken by hand on a bench (docs/pi-image.md)."""

SAMPLES_SHOWN: Final[int] = 12
"""Readings shown in the Markdown summary; the JSON report keeps them all."""

HALF: Final[float] = 0.50
P99: Final[float] = 0.99


# =========================================================================
# The tick
# =========================================================================


@dataclass(frozen=True, slots=True)
class Quantiles:
    """What a histogram says of the durations it was given, in milliseconds.

    ``p50`` and ``p99`` are the upper edge of the bucket the centile falls in,
    never above ``highest``: at most 5 % over the true value, never under it.
    """

    count: int
    p50: float
    p99: float
    highest: float


class Histogram:
    """Durations in buckets of fixed number: a day of ticks costs what a second does.

    Mutable, owned by whoever times the ticks.
    """

    __slots__ = ("_counts", "_highest", "_total")

    def __init__(self) -> None:
        self._counts: list[int] = [0] * len(EDGES)
        self._total: int = 0
        self._highest: float = 0.0

    def add(self, milliseconds: float) -> None:
        """Count one duration. One past the last edge goes to the last bucket."""
        index = min(bisect_left(EDGES, milliseconds), len(EDGES) - 1)
        self._counts[index] += 1
        self._total += 1
        self._highest = max(self._highest, milliseconds)

    def _quantile(self, share: float) -> float:
        """The upper edge of the bucket that holds the value of rank ``share``; one at least."""
        rank = max(1, math.ceil(share * self._total))
        index = bisect_left(list(accumulate(self._counts)), rank)
        return min(EDGES[index], self._highest)

    def summary(self) -> Quantiles | None:
        """The centiles so far, or ``None`` when nothing was counted."""
        if self._total == 0:
            return None
        return Quantiles(
            count=self._total,
            p50=self._quantile(HALF),
            p99=self._quantile(P99),
            highest=self._highest,
        )

    def reset(self) -> None:
        """Forget everything: the next window starts empty."""
        self._counts = [0] * len(EDGES)
        self._total = 0
        self._highest = 0.0


# =========================================================================
# The process
# =========================================================================


def ps_resident_kib(pid: int) -> str:
    """What ``ps`` prints for the resident size of ``pid``, in KiB. Raises on failure."""
    listed = subprocess.run(  # noqa: S603  # a fixed command, and a process id of ours
        ["/bin/ps", "-o", "rss=", "-p", str(pid)],
        check=True,
        capture_output=True,
        text=True,
    )
    return listed.stdout


def resident_bytes(
    *, status: Path = Path("/proc/self/status"), ps: Callable[[int], str] = ps_resident_kib
) -> int:
    """The resident memory of this process, in bytes.

    Linux says it in ``/proc/self/status`` (``VmRSS``). Where there is no
    ``/proc`` (macOS, a developer's workstation) ``ps`` is asked instead.
    Raises ``ValueError`` when neither gives a number.
    """
    try:
        text = status.read_text(encoding="ascii")
    except OSError:
        return int(ps(os.getpid()).strip()) * KIB
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * KIB
    raise ValueError(f"{status}: no VmRSS line")


def open_descriptors(
    *, listed: Path = Path("/proc/self/fd"), fallback: Path = Path("/dev/fd")
) -> int:
    """How many file descriptors this process holds open.

    The count includes the one opened to list them: the same one at every
    reading, so it changes no comparison.
    """
    try:
        return sum(1 for _ in listed.iterdir())
    except OSError:
        return sum(1 for _ in fallback.iterdir())


# =========================================================================
# The data folder
# =========================================================================


@dataclass(frozen=True, slots=True)
class Footprint:
    """What a file or a directory tree takes on the disk."""

    size: int = 0
    """Apparent bytes: the sum of the lengths of the files."""

    allocated: int = 0
    """Bytes the file system gave them: whole blocks, so more than ``size`` for small files."""

    entries: int = 0
    """Files and directories: the inodes."""

    def plus(self, other: Footprint) -> Footprint:
        """Both together."""
        return Footprint(
            size=self.size + other.size,
            allocated=self.allocated + other.allocated,
            entries=self.entries + other.entries,
        )


def _entry(path: Path, stat: Callable[[Path], os.stat_result]) -> Footprint:
    """What one file or one directory takes by itself; nothing when it is gone."""
    try:
        found = stat(path)
    except OSError:
        return Footprint()
    return Footprint(
        size=0 if S_ISDIR(found.st_mode) else found.st_size,
        allocated=found.st_blocks * 512,
        entries=1,
    )


def footprint(path: Path, stat: Callable[[Path], os.stat_result] = os.lstat) -> Footprint:
    """What ``path`` and everything under it take. Never raises.

    A file that goes away while this looks (a cursor being renamed into
    place, a record the retention removes) is counted as absent.
    """
    total = _entry(path, stat)
    for directory, folders, files in os.walk(path):
        for name in (*folders, *files):
            total = total.plus(_entry(Path(directory) / name, stat))
    return total


@dataclass(frozen=True, slots=True)
class DataState:
    """The data folder at one instant."""

    total: Footprint
    records: Mapping[str, Footprint]
    """Every record directory, by name: the closed ones and the one being written."""

    closed: int
    """How many of them are closed (they carry their checksums)."""

    logbook: Footprint
    stray: tuple[str, ...]
    """Names next to the records that are neither a record, nor the cursor or the deposit
    marker of one, nor the synchronisation's baseline, nor the logbook."""

    deposited: int
    """Closed records whose deposit off the machine is confirmed: what the retention may remove."""

    purged: int
    """Closed records seen once and gone since: removed by the retention."""


class DataFolder:
    """The records directory of the console, measured without walking it whole each time.

    A closed record never changes (its checksums index every file), so what it
    takes is measured once and kept: a day of sessions is hundreds of
    thousands of small files. Mutable, single owner.
    """

    __slots__ = ("_closed", "_purged", "_root")

    def __init__(self, root: Path) -> None:
        self._root: Path = root
        self._closed: dict[str, Footprint] = {}
        self._purged: int = 0

    @property
    def root(self) -> Path:
        """The directory measured."""
        return self._root

    def measure(self) -> DataState:
        """Walk what can have changed since the last call. Never raises."""
        try:
            children = sorted(self._root.iterdir())
        except OSError:
            children = []
        names = {child.name for child in children}
        records: dict[str, Footprint] = {}
        logbook = Footprint()
        stray: list[str] = []
        total = Footprint(entries=1)
        deposited = 0
        for child in children:
            taken = self._closed.get(child.name)
            if taken is None:
                # Looked for BEFORE the walk: a record closed while it is walked
                # is measured again next time, with its last file.
                closed = (child / CHECKSUMS).exists()
                taken = footprint(child)
                if closed and RECORD_NAME.fullmatch(child.name) is not None:
                    self._closed[child.name] = taken
            total = total.plus(taken)
            if RECORD_NAME.fullmatch(child.name) is not None:
                records[child.name] = taken
                deposited += int(confirmed_deposit(child) is not None)
            elif child.name == LOGBOOK:
                logbook = taken
            elif not _beside_a_record(child.name, names):
                stray.append(child.name)
        for name in tuple(self._closed):
            if name not in names:
                del self._closed[name]
                self._purged += 1
        return DataState(
            total=total,
            records=records,
            closed=len(self._closed),
            logbook=logbook,
            stray=tuple(stray),
            deposited=deposited,
            purged=self._purged,
        )


def _beside_a_record(name: str, names: set[str]) -> bool:
    """Whether ``name`` is one of the files the console keeps next to its records."""
    if name == BASELINE_NAME:
        return True
    for suffix in (CURSOR_SUFFIX, MARKER_SUFFIX):
        if name.endswith(suffix) and name.removesuffix(suffix) in names:
            return True
    return False


# =========================================================================
# One reading, one session, one run
# =========================================================================


@dataclass(frozen=True, slots=True, kw_only=True)
class Sample:
    """One reading of the process and of its data folder."""

    at: float
    """Seconds since the run started: simulated seconds on the manual clock, real ones else."""

    rss: int
    descriptors: int
    data_bytes: int
    """Allocated bytes under the data folder."""

    data_entries: int
    blocks: int | None = None
    """Memory blocks the interpreter holds allocated (``sys.getallocatedblocks``): what the
    Python objects of the process take, whatever the system does with its pages. ``None``
    for a process read from outside."""

    tick: Quantiles | None = None
    """The control ticks since the previous reading."""


class Sampler:
    """Takes the readings of this process. Everything here may block: never on a live loop."""

    __slots__ = ("_folder",)

    def __init__(self, folder: DataFolder) -> None:
        self._folder: DataFolder = folder

    def take(self, at: float, tick: Quantiles | None) -> Sample:
        """One reading, now."""
        data = self._folder.measure().total
        return Sample(
            at=at,
            rss=resident_bytes(),
            descriptors=open_descriptors(),
            data_bytes=data.allocated,
            data_entries=data.entries,
            blocks=sys.getallocatedblocks(),
            tick=tick,
        )


@unique
class Kind(Enum):
    """The two sessions the scenario chains."""

    MANUAL = "manual"
    PROGRAMME = "programme"


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """One session of the run, as the scenario saw it end."""

    index: int
    kind: Kind
    started_at: float
    recorded: float
    """Seconds from the start command to the first second the console was seen finished."""

    end: str
    """How the console says it ended."""

    as_planned: bool
    delivered: bool
    """Whether the simulated dashboard held it whole and closed when the rest that follows ended."""

    points: int
    """1 Hz points of it the dashboard stored."""

    record: Footprint


@dataclass(frozen=True, slots=True, kw_only=True)
class Sync:
    """What reached the simulated dashboard over the run."""

    heartbeats: int
    points: int
    rejected: int
    duplicates: int
    owed: int
    """Sessions or refusals the console still owed the dashboard when the run ended."""


@unique
class Mode(Enum):
    """How the run was made."""

    SIMULATED = "simulated"
    REAL_TIME = "real-time"
    OBSERVED = "observed"
    """Readings taken from outside a console somebody else ran (a bench day)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """Everything one run measured."""

    mode: Mode
    requested: float
    covered: float
    """Seconds the run really covered, on the clock of its mode."""

    wall: float
    samples: tuple[Sample, ...]
    cpu: float = 0.0
    """Processor seconds the process spent on the run: what it costs whatever else the
    machine was doing. 0 for a console read from outside."""

    tick: Quantiles | None = None
    period: Quantiles | None = None
    """Time between two ticks, real-time runs only: what the ``loop_stall`` rule judges."""

    sessions: tuple[Session, ...] = ()
    sync: Sync | None = None
    data: DataState | None = None
    anomaly: str | None = None
    """What the scenario met that it did not expect, and stopped on."""

    notes: tuple[str, ...] = ()
    """What was not active or not measured, said in the report."""


# =========================================================================
# The judgement
# =========================================================================


@unique
class Status(Enum):
    """The framework's statuses (docs/framework-de-test.md, sections 6 and 12)."""

    PASS = "PASS"  # noqa: S105  # a verdict, not a secret
    FAIL = "FAIL"
    XFAIL = "XFAIL"
    FIXED = "FIXED?"


@dataclass(frozen=True, slots=True)
class Check:
    """One threshold, and what the run measured against it."""

    name: str
    status: Status
    threshold: str
    measured: str


def megabytes(count: float) -> str:
    """``count`` bytes, in MiB with one decimal."""
    return f"{count / MIB:.1f} Mo"


def hours_minutes(seconds: float) -> str:
    """``seconds`` as hours and minutes."""
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d}"


def warm_up(covered: float) -> float:
    """Seconds left out of the memory and descriptor judgements of a run of ``covered`` seconds."""
    return WARM_UP_S if covered >= 2 * WARM_UP_S else covered * SHORT_RUN_WARM_UP


def judged(run: Run) -> tuple[Sample, ...]:
    """The readings taken after the warm-up."""
    start = warm_up(run.covered)
    return tuple(sample for sample in run.samples if sample.at >= start)


def check_rss(run: Run) -> Check:
    """Resident memory within 10 % of its first reading after the warm-up."""
    threshold = (
        f"stable à ±{RSS_BAND:.0%} après la période de chauffe "
        f"({hours_minutes(warm_up(run.covered))})"
    )
    kept = judged(run)
    if len(kept) < MIN_JUDGED:
        return Check(
            "rss", Status.FAIL, threshold, f"{len(kept)} relevés après la chauffe : trop peu"
        )
    reference = kept[0].rss
    low = min(sample.rss for sample in kept)
    high = max(sample.rss for sample in kept)
    worst = max(high - reference, reference - low) / reference
    measured = (
        f"{megabytes(reference)} à {hours_minutes(kept[0].at)}, puis de {megabytes(low)} à "
        f"{megabytes(high)} ({megabytes(kept[-1].rss)} à la fin) : écart maximal {worst:.1%}"
    )
    return Check("rss", Status.PASS if worst <= RSS_BAND else Status.FAIL, threshold, measured)


def check_tick(run: Run) -> Check | None:
    """The 99th centile of the tick under 150 ms. ``None`` when no tick was timed."""
    tick = run.tick
    if tick is None:
        return None
    threshold = f"p99 < {TICK_P99_LIMIT_MS:.0f} ms"
    measured = (
        f"p50 {tick.p50:.2f} ms, p99 {tick.p99:.2f} ms, maximum {tick.highest:.1f} ms "
        f"sur {tick.count} tics"
    )
    status = Status.PASS if tick.p99 < TICK_P99_LIMIT_MS else Status.FAIL
    return Check("tick", status, threshold, measured)


def check_descriptors(run: Run) -> Check:
    """No descriptor leaks: the second half of the run opens no more than the first."""
    threshold = "le maximum de la seconde moitié ne dépasse pas celui de la première"
    kept = judged(run)
    if len(kept) < MIN_JUDGED:
        return Check(
            "descriptors",
            Status.FAIL,
            threshold,
            f"{len(kept)} relevés après la chauffe : trop peu",
        )
    half = len(kept) // 2
    first = max(sample.descriptors for sample in kept[:half])
    second = max(sample.descriptors for sample in kept[half:])
    measured = (
        f"au plus {first} puis {second} descripteurs ouverts ({kept[-1].descriptors} à la fin)"
    )
    return Check(
        "descriptors", Status.PASS if second <= first else Status.FAIL, threshold, measured
    )


def record_problems(session: Session) -> tuple[str, ...]:
    """Where the record of ``session`` takes more than the arming gate plans for."""
    lasted = session.recorded + RECORD_SLACK_S
    planned = round(RAW_BLOCKS_PER_SECOND * lasted) + FIXED_ENTRIES
    problems: list[str] = []
    if session.record.entries > planned:
        problems.append(
            f"séance {session.index} : {session.record.entries} fichiers pour "
            f"{session.recorded:.0f} s, {planned} prévus"
        )
    if session.record.allocated > BYTES_PER_SECOND_RESERVED * lasted:
        problems.append(
            f"séance {session.index} : {megabytes(session.record.allocated)} pour "
            f"{session.recorded:.0f} s, au-delà de la réserve de l'armement"
        )
    return tuple(problems)


def per_hour(run: Run) -> Footprint | None:
    """What an hour of recorded session adds to the data folder, or ``None`` with no session."""
    seconds = sum(session.recorded for session in run.sessions)
    if seconds <= 0:
        return None
    taken = Footprint()
    for session in run.sessions:
        taken = taken.plus(session.record)
    scale = 3600.0 / seconds
    return Footprint(
        size=round(taken.size * scale),
        allocated=round(taken.allocated * scale),
        entries=round(taken.entries * scale),
    )


def check_data(run: Run) -> Check | None:
    """The data folder grows as the code guarantees. ``None`` when it was not walked."""
    data = run.data
    if data is None:
        return None
    threshold = (
        f"par enregistrement : au plus {RAW_BLOCKS_PER_SECOND} fichiers par seconde plus "
        f"{FIXED_ENTRIES}, et {BYTES_PER_SECOND_RESERVED / KIB:.0f} Kio par seconde ; "
        f"journal hors séance sous {megabytes(LOGBOOK_LIMIT)} ; rien d'autre à côté"
    )
    problems = [problem for session in run.sessions for problem in record_problems(session)]
    if data.logbook.size > LOGBOOK_LIMIT:
        problems.append(f"journal hors séance : {megabytes(data.logbook.size)}")
    if data.stray:
        problems.append(f"fichiers inattendus : {', '.join(data.stray)}")
    if problems:
        return Check("data", Status.FAIL, threshold, " ; ".join(problems))
    hourly = per_hour(run)
    rate = (
        "aucune séance enregistrée"
        if hourly is None
        else f"{megabytes(hourly.allocated)} et {hourly.entries} fichiers par heure de séance"
    )
    measured = (
        f"{megabytes(data.total.allocated)} et {data.total.entries} fichiers pour "
        f"{len(data.records)} enregistrements ({rate}) ; journal hors séance "
        f"{data.logbook.size} octets"
    )
    return Check("data", Status.PASS, threshold, measured)


def check_rotation(run: Run) -> Check | None:
    """The folder bounded by the retention: a documented known defect. ``None`` with no record."""
    data = run.data
    if data is None or data.closed + data.purged == 0:
        return None
    threshold = "borné par la rotation : les enregistrements déposés et confirmés sont purgés"
    acted = data.deposited + data.purged
    if acted:
        measured = (
            f"{data.deposited} dépôts confirmés, {data.purged} enregistrements purgés : le "
            "défaut connu ne tient plus, écrire la vraie borne et retirer KNOWN_UNBOUNDED_DATA"
        )
        return Check("rotation", Status.FIXED, threshold, measured)
    measured = (
        f"{data.closed} enregistrements clos, 0 dépôt confirmé, 0 purgé. Défaut connu : "
        f"{KNOWN_UNBOUNDED_DATA}"
    )
    return Check("rotation", Status.XFAIL, threshold, measured)


def check_sessions(run: Run) -> Check | None:
    """Every session as planned and whole on the dashboard. ``None`` for an observed run."""
    sync = run.sync
    if sync is None:
        return None
    threshold = (
        "chaque séance finit comme prévu et arrive entière au tableau de bord simulé ; "
        "rien d'inattendu"
    )
    sessions = run.sessions
    problems: list[str] = []
    if run.anomaly is not None:
        problems.append(f"arrêt du scénario : {run.anomaly}")
    if not sessions:
        problems.append("aucune séance")
    unplanned = [session for session in sessions if not session.as_planned]
    if unplanned:
        problems.append(
            f"{len(unplanned)} fins imprévues (séance {unplanned[0].index} : {unplanned[0].end})"
        )
    undelivered = [session.index for session in sessions if not session.delivered]
    if undelivered:
        problems.append(f"séances non livrées : {undelivered}")
    if sync.rejected or sync.owed:
        problems.append(f"{sync.rejected} points refusés, {sync.owed} envois encore dus")
    if problems:
        return Check("sessions", Status.FAIL, threshold, " ; ".join(problems))
    manual = sum(1 for session in sessions if session.kind is Kind.MANUAL)
    measured = (
        f"{len(sessions)} séances ({manual} manuelles, {len(sessions) - manual} programmées), "
        f"{sync.points} points 1 Hz livrés, {sync.heartbeats} heartbeats, 0 refusé, 0 dû"
    )
    return Check("sessions", Status.PASS, threshold, measured)


def judge(run: Run) -> tuple[Check, ...]:
    """Every threshold that applies to ``run``, in the order of the report."""
    checks = (
        check_rss(run),
        check_tick(run),
        check_descriptors(run),
        check_data(run),
        check_rotation(run),
        check_sessions(run),
    )
    return tuple(check for check in checks if check is not None)


def passed(checks: Sequence[Check]) -> bool:
    """Whether a run with these checks is green: every one PASS or a documented XFAIL."""
    return all(check.status in {Status.PASS, Status.XFAIL} for check in checks)


# =========================================================================
# The report
# =========================================================================


def _quantiles(values: Quantiles | None) -> Mapping[str, object] | None:
    if values is None:
        return None
    return {
        "count": values.count,
        "p50": round(values.p50, 3),
        "p99": round(values.p99, 3),
        "max": round(values.highest, 3),
    }


def _footprint(taken: Footprint) -> Mapping[str, object]:
    return {"bytes": taken.size, "allocated_bytes": taken.allocated, "entries": taken.entries}


def _sample(sample: Sample) -> Mapping[str, object]:
    return {
        "at_s": round(sample.at, 1),
        "rss_bytes": sample.rss,
        "descriptors": sample.descriptors,
        "data_allocated_bytes": sample.data_bytes,
        "data_entries": sample.data_entries,
        "allocated_blocks": sample.blocks,
        "tick_ms": _quantiles(sample.tick),
    }


def _session(session: Session) -> Mapping[str, object]:
    return {
        "index": session.index,
        "kind": session.kind.value,
        "started_at_s": round(session.started_at, 1),
        "recorded_s": round(session.recorded, 1),
        "end": session.end,
        "as_planned": session.as_planned,
        "delivered": session.delivered,
        "points": session.points,
        "record": _footprint(session.record),
    }


def _data(run: Run) -> Mapping[str, object] | None:
    data = run.data
    if data is None:
        return None
    hourly = per_hour(run)
    return {
        "total": _footprint(data.total),
        "records": len(data.records),
        "closed": data.closed,
        "per_recorded_hour": None if hourly is None else _footprint(hourly),
        "logbook_bytes": data.logbook.size,
        "stray": list(data.stray),
        "deposited": data.deposited,
        "purged": data.purged,
        "bounded_by_rotation": bool(data.deposited + data.purged),
    }


def _sync(sync: Sync | None) -> Mapping[str, object] | None:
    if sync is None:
        return None
    return {
        "heartbeats": sync.heartbeats,
        "points": sync.points,
        "rejected": sync.rejected,
        "duplicates": sync.duplicates,
        "owed_at_the_end": sync.owed,
    }


def document(run: Run, checks: Sequence[Check]) -> Mapping[str, object]:
    """The JSON report of a run: every reading, every session, every threshold."""
    return {
        "scenario": "endurance",
        "ticket": "ANH-164",
        "mode": run.mode.value,
        "verdict": "PASS" if passed(checks) else "FAIL",
        "requested_s": round(run.requested, 1),
        "covered_s": round(run.covered, 1),
        "wall_s": round(run.wall, 1),
        "cpu_s": round(run.cpu, 1),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpus": os.cpu_count(),
        },
        "checks": [
            {
                "name": check.name,
                "status": check.status.value,
                "threshold": check.threshold,
                "measured": check.measured,
            }
            for check in checks
        ],
        "tick_ms": _quantiles(run.tick),
        "tick_period_ms": _quantiles(run.period),
        "sync": _sync(run.sync),
        "data": _data(run),
        "anomaly": run.anomaly,
        "notes": list(run.notes),
        "sessions": [_session(session) for session in run.sessions],
        "samples": [_sample(sample) for sample in run.samples],
    }


def _shown(samples: Sequence[Sample]) -> Sequence[Sample]:
    """At most :data:`SAMPLES_SHOWN` readings, evenly spread, the last one always."""
    if len(samples) <= SAMPLES_SHOWN:
        return samples
    step = (len(samples) - 1) / (SAMPLES_SHOWN - 1)
    return [samples[round(index * step)] for index in range(SAMPLES_SHOWN)]


def _tick_cell(tick: Quantiles | None) -> str:
    return "-" if tick is None else f"{tick.p50:.2f} / {tick.p99:.2f}"


def markdown(run: Run, checks: Sequence[Check]) -> str:
    """The summary a person reads: the verdict, each threshold, a few readings."""
    verdict = "PASS" if passed(checks) else "FAIL"
    lines = [
        f"## Endurance de la console ({run.mode.value}) : {verdict}",
        "",
        f"Durée couverte : {hours_minutes(run.covered)} "
        f"({hours_minutes(run.requested)} demandées), en {run.wall:.0f} s d'horloge murale "
        f"et {run.cpu:.0f} s de processeur.",
        "",
        "| Seuil | Statut | Attendu | Mesuré |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| `{check.name}` | {check.status.value} | {check.threshold} | {check.measured} |"
        for check in checks
    )
    period = run.period
    if period is not None:
        lines.extend(
            [
                "",
                f"Écart entre deux tics : p50 {period.p50:.1f} ms, p99 {period.p99:.1f} ms, "
                f"maximum {period.highest:.1f} ms.",
            ]
        )
    lines.extend(
        [
            "",
            "| Instant | Mémoire résidente | Descripteurs | Données | Fichiers "
            "| Tic p50 / p99 (ms) |",
            "|---|---|---|---|---|---|",
        ]
    )
    lines.extend(
        f"| {hours_minutes(sample.at)} | {megabytes(sample.rss)} | {sample.descriptors} | "
        f"{megabytes(sample.data_bytes)} | {sample.data_entries} | {_tick_cell(sample.tick)} |"
        for sample in _shown(run.samples)
    )
    if run.notes:
        lines.extend(["", "Limites de cet essai :", ""])
        lines.extend(f"* {note}" for note in run.notes)
    return "\n".join(lines) + "\n"


def write_report(run: Run, checks: Sequence[Check], out: Path) -> tuple[Path, Path]:
    """Write ``endurance.json`` and ``endurance.md`` under ``out``; returns both paths."""
    out.mkdir(parents=True, exist_ok=True)
    data = out / "endurance.json"
    summary = out / "endurance.md"
    data.write_text(
        json.dumps(document(run, checks), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    summary.write_text(markdown(run, checks), encoding="utf-8")
    return data, summary


# =========================================================================
# Readings taken by hand, on a bench
# =========================================================================


def samples_from_csv(text: str) -> tuple[Sample, ...]:
    """The readings of a file with the columns of :data:`CSV_HEADER`.

    The header line, empty lines and lines starting with ``#`` are skipped.
    Sizes are in KiB, as ``du -sk`` and ``/proc/<pid>/status`` print them.
    Raises ``ValueError`` naming the first line that is not five integers.
    """
    samples: list[Sample] = []
    origin: int | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line == CSV_HEADER:
            continue
        cells = line.split(",")
        if len(cells) != len(CSV_HEADER.split(",")) or not all(
            cell.strip().isdigit() for cell in cells
        ):
            raise ValueError(f"line {number}: expected {CSV_HEADER}, got {line!r}")
        epoch, rss, descriptors, data, inodes = (int(cell) for cell in cells)
        origin = epoch if origin is None else origin
        samples.append(
            Sample(
                at=float(epoch - origin),
                rss=rss * KIB,
                descriptors=descriptors,
                data_bytes=data * KIB,
                data_entries=inodes,
            )
        )
    return tuple(samples)


def observed(samples: Sequence[Sample], notes: Sequence[str]) -> Run:
    """The run a file of readings describes: a console somebody else ran, seen from outside."""
    covered = samples[-1].at if samples else 0.0
    return Run(
        mode=Mode.OBSERVED,
        requested=covered,
        covered=covered,
        wall=covered,
        samples=tuple(samples),
        notes=tuple(notes),
    )
