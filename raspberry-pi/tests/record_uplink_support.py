"""What the tests of the sending share: a scripted link, records on disk, a stepped uplink.

The records are REAL ones, written by the format's own writer, and the reads
and the cursor writes go through the real record I/O threads. Only the link
(:class:`Link`) and the clock are the test's.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from src.clock import ManualClock
from src.record.cursor import Cursor, CursorRead, load
from src.record.export import RecordIo
from src.record.rows import JsonValue
from src.record.schema import EndObservation, Event, EventKind, Manifest, Profile
from src.record.writer import Writer
from src.record_uplink import (
    EVENTS_PATH,
    LOCAL_PATH,
    TELEMETRY_PATH,
    Acked,
    Answer,
    ArmedSession,
    Declaration,
    RecordSource,
    RecordUplink,
)
from src.result import Ok
from src.units import Monotonic, Seconds, UnixMillis
from tests.record_support import manifest, row

EPOCH_MS = 1_791_195_062_000
"""The wall clock when the monotonic clock of these tests reads 0."""

BOOT = "0f8d3c2a-1b4e-4c6d-9e7f-123456789abc"

type Body = Mapping[str, JsonValue]
type Script = Answer | Callable[[Body], Answer]


@dataclass
class Link:
    """A :class:`~src.record_uplink.Sender` that answers from a table and keeps every request."""

    answers: dict[str, Script] = field(default_factory=dict[str, Script])
    sent: list[tuple[str, Body]] = field(default_factory=list[tuple[str, Body]])
    declared: dict[str, str] = field(default_factory=dict[str, str])
    """As the dashboard does: one session per reference, however often it is declared."""

    def answer(self, path: str, script: Script) -> None:
        self.answers[path] = script

    async def send(self, path: str, body: Body) -> Answer:
        self.sent.append((path, body))
        script = self.answers.get(path)
        if script is None:
            if path == LOCAL_PATH:
                reference = str(body["localRef"])
                known = self.declared.setdefault(reference, f"cloud-{len(self.declared) + 1}")
                return Acked({"sessionId": known})
            return Acked({"stored": 0, "duplicates": 0, "rejected": 0})
        return script(body) if callable(script) else script

    def to(self, path: str) -> list[Body]:
        return [body for sent, body in self.sent if sent == path]

    def paths(self) -> list[str]:
        return [path.rsplit("/", 1)[-1] for path, _body in self.sent]

    def batches(self, session_id: str | None = None) -> list[list[float]]:
        """The ``elapsedS`` of the points of each telemetry request, in the order sent."""
        return [
            [point["elapsedS"] for point in cast("list[Mapping[str, float]]", body["points"])]
            for body in self.to(TELEMETRY_PATH)
            if session_id is None or body["sessionId"] == session_id
        ]

    def seconds(self, session_id: str | None = None) -> list[float]:
        """The ``elapsedS`` of every point sent, in the order sent."""
        return [second for batch in self.batches(session_id) for second in batch]

    def ranks(self) -> list[int]:
        """The ``seq`` of every event sent, in the order sent."""
        return [
            event["seq"]
            for body in self.to(EVENTS_PATH)
            for event in cast("list[Mapping[str, int]]", body["events"])
        ]


@dataclass
class Disk:
    """The records directory, and which record the console says it has open."""

    root: Path
    current: Path | None = None

    def in_progress(self) -> Path | None:
        return self.current


@dataclass
class Bench:
    """One uplink with its clock, its link and its disk."""

    clock: ManualClock
    link: Link
    disk: Disk
    uplink: RecordUplink
    cancelled: list[str]

    async def step(self, seconds: float = 1.0) -> None:
        self.clock.advance(Seconds(seconds))
        await self.uplink.step()

    async def run(self, seconds: int) -> None:
        for _ in range(seconds):
            await self.step()

    def following(self) -> str | None:
        """Read through a call: a checker keeps a property's narrowing across an ``await``."""
        return self.uplink.following

    def refusal_owed(self) -> bool:
        """Read afresh, for the same reason."""
        return self.uplink.refusal_owed

    def cursor(self, record: Path) -> CursorRead:
        return load(record)

    def restarted(self, *, boot_id: str | None = BOOT, io: RecordIo | None = None) -> Bench:
        """The console after a restart: nothing in memory, the same disk, the same clock."""
        return bench(
            self.disk.root, clock=self.clock, link=self.link, boot_id=boot_id, io=io, recording=True
        )


def bench(
    root: Path,
    *,
    clock: ManualClock | None = None,
    link: Link | None = None,
    boot_id: str | None = BOOT,
    io: RecordIo | None = None,
    recording: bool = True,
) -> Bench:
    clock = ManualClock(Monotonic(100.0), UnixMillis(EPOCH_MS)) if clock is None else clock
    link = Link() if link is None else link
    disk = Disk(root)
    cancelled: list[str] = []
    source = (
        RecordSource(
            root=root,
            current=disk.in_progress,
            io=RecordIo(1) if io is None else io,
            boot_id=boot_id,
        )
        if recording
        else None
    )
    uplink = RecordUplink(clock=clock, sender=link, source=source, start_refused=cancelled.append)
    return Bench(clock=clock, link=link, disk=disk, uplink=uplink, cancelled=cancelled)


def armed(clock: ManualClock, *, remote: str | None = None, kind: str = "manual") -> ArmedSession:
    """A session the runtime arms now, as the link describes it to the sending."""
    return ArmedSession(
        declaration=Declaration(
            kind=kind,
            operator="dr. attending",
            occupancy="bench" if kind == "manual" else None,
            profile_id=None if kind == "manual" else "standard_30_min",
            profile_name=None if kind == "manual" else "30 min",
            zone_low_bpm=None if kind == "manual" else 118,
            zone_high_bpm=None if kind == "manual" else 138,
            total_duration_s=None if kind == "manual" else 1800.0,
            subject_hr_max=None if kind == "manual" else 162,
        ),
        started_at=clock.unix_millis(),
        cloud_session_id=remote,
    )


def stamp_of(millis: int) -> str:
    return datetime.fromtimestamp(millis / 1000, UTC).isoformat().replace("+00:00", "Z")


def described(clock: ManualClock, ref: str, *, remote: str | None = None) -> Manifest:
    """The manifest of a session armed now on ``clock``."""
    started = stamp_of(int(clock.unix_millis()))
    base = manifest()
    return dataclasses.replace(
        base,
        local_ref=ref,
        session_id=remote,
        started_at=started,
        clocks=dataclasses.replace(
            base.clocks, utc_start=started, monotonic_start=clock.monotonic()
        ),
    )


@dataclass
class Recorded:
    """One record being written, as the journal thread would, by the test."""

    writer: Writer
    ticks: int = 0
    events: int = 0

    @property
    def path(self) -> Path:
        return self.writer.path

    def tick(self, seconds: float, *, hertz: int = 5) -> None:
        """Append ``seconds`` more of ticks."""
        for _ in range(round(seconds * hertz)):
            written = self.writer.tick(
                dataclasses.replace(row(), t=self.ticks / hertz, measured_motor_rpm=self.ticks)
            )
            assert isinstance(written, Ok)
            self.ticks += 1

    def event(self, count: int = 1) -> None:
        for _ in range(count):
            written = self.writer.event(
                Event(t=float(self.events), kind=EventKind.PHASE, detail=f"phase {self.events}")
            )
            assert isinstance(written, Ok)
            self.events += 1

    def close(self, reason: str = "operator_stop", *, stop: str | None = "fini") -> None:
        """Close the record as the console does, at its last tick."""
        observation = EndObservation(t=self.ticks / 5, stop_reason=stop)
        closed = self.writer.close(ManualClock(), reason, observation)
        assert isinstance(closed, Ok)


def recording(
    root: Path, clock: ManualClock, ref: str = "ref-1", *, remote: str | None = None
) -> Recorded:
    """Create the record of a session armed now."""
    created = Writer.create(root, described(clock, ref, remote=remote))
    assert isinstance(created, Ok)
    return Recorded(created.value)


PROGRAMME = Profile(
    total_duration_s=600.0,
    baseline_s=60.0,
    warmup_max_s=120.0,
    hold_min_s=300.0,
    cooldown_s=60.0,
    recovery_s=60.0,
    zone_low_bpm=118,
    zone_high_bpm=138,
    hard_max_bpm=148,
    critical_bpm=158,
    subject_hr_max=162,
    min_run_rpm=55,
    max_rpm=276,
    warmup_rpm_ceiling_fraction=0.5,
    channels=("ECG",),
    allow_above_nameplate=False,
    source_rev=1,
    resolved_at=1,
    total_overridden=False,
)


def launched_programme(root: Path) -> Path:
    """The record of a programme launched from the dashboard, as ``k17remote``."""
    described_as = dataclasses.replace(
        manifest(), session_id="k17remote", kind="auto", occupancy="occupied", profile=PROGRAMME
    )
    created = Writer.create(root, described_as)
    assert isinstance(created, Ok)
    return created.value.path


def cursor_of_record(record: Path) -> Cursor:
    found = load(record)
    assert isinstance(found, Cursor), found
    return found
