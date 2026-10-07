"""The console records its sessions: ANH-128 EX-1, EX-3, EX-4, EX-5, on the REAL console.

Each case builds the real composition root over the simulated drive and the
simulated BITalino (``tests/record_console_support.py``), drives it through
the operator's HTTP API, and reads the record back with the shared reader.
"""

from __future__ import annotations

import errno
import logging
import re
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest

import src.record.journal as journal_module
from src import local_panel
from src.bitalino_client import ChannelData, LinkStats, SampleBatch
from src.clock import ManualClock
from src.cloud_sync import DASHBOARD_OPERATOR
from src.control_surface import EventKind as SurfaceEvent
from src.control_surface import SessionEvent
from src.local_panel import (
    describe_record_storage,
    describe_start_refusal,
    open_journal,
)
from src.motor.drive import DriveFault
from src.record.journal import (
    MIN_FREE_BYTES,
    STORAGE_STALE_AFTER,
    Cause,
    Journal,
    JournalStatus,
    Storage,
)
from src.record.reader import read
from src.record.schema import EventKind, RecordError
from src.record.session import (
    BROKEN,
    INTERRUPTED,
    MAX_NAMES,
    MAX_TEXT,
    SessionRecorder,
    SessionRequest,
    Stamp,
    describe_status,
    manifest_for,
    names_of,
    opaque,
    operator_alias,
    record_kind,
    redact,
    stamp_for,
    storage_gate,
)
from src.result import Ok
from src.training.motion import DEFAULT_MOTION_LIMITS
from src.training.plan import ProfileStore, Program
from src.training.runtime import EndReason, RecordStorageLow, RuntimeLimits, RuntimeState
from src.training.safety import SafetyLimits
from src.training.types import Occupancy, SafetyAction
from src.units import Bpm, Metres, Monotonic, MotorRpm, RpmPerSecond, Seconds, UnixMillis
from tests.record_console_support import (
    recorded_rig,
    set_target,
    start_bench,
    stop,
)
from tests.record_journal_support import refuse_writes
from tests.test_failure_rig import (
    BENCH_ENV,
    OPERATOR,
    PROGRAMME_ENV,
    SHORT_PROFILE,
    SHORT_STORE,
    attest,
    config_of,
    start_programme,
)

ALIAS = operator_alias(OPERATOR)
RECORD_NAME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z_[0-9a-f]{16}")

STAMP = Stamp(
    machine_id="machine-1",
    organization_id="org-1",
    software_version="test",
    config_hash="ab" * 32,
    medical_parameters_version="cd" * 32,
)


# =========================================================================
# EX-1: one directory per session, opened at arming, closed at the end
# =========================================================================


async def test_ex1_a_manual_session_is_recorded_from_arming_to_its_end(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await recorded.tick(1.0)
        assert recorded.records() == [], "nothing is recorded before a session is armed"

        await start_bench(recorded, session)
        path = recorded.only_record()
        assert RECORD_NAME.fullmatch(path.name)
        opening = read(path)
        assert isinstance(opening, Ok)
        assert opening.value.manifest.ended_at is None
        assert opening.value.manifest.end_reason is None
        assert not (path / "checksums.sha256").exists()

        await set_target(session, 5.0)
        await recorded.tick(20.0)
        turning = recorded.rig.panel.runtime.snapshot().measured.motor_rpm
        assert turning > 55
        await stop(recorded, session, 60.0)

    assert recorded.state() is RuntimeState.FINISHED
    recording = recorded.recording()
    assert recording.warnings == ()
    manifest = recording.manifest
    assert (manifest.kind, manifest.occupancy) == ("manual", "bench")
    assert manifest.end_reason == "operator_stop"
    assert manifest.ended_at is not None
    assert manifest.operator == ALIAS
    assert manifest.subject_id is None
    assert manifest.profile is None
    assert (manifest.machine_id, manifest.organization_id) == ("unassigned", "unassigned")
    observation = manifest.end_observation
    assert observation is not None
    assert observation.runtime_state == "finished"
    assert observation.shaft_motor_rpm == 0
    assert observation.runtime_output_enabled is False
    assert observation.stop_reason == "operator pressed STOP"

    rows = recording.rows
    assert rows[0].t == 0.0
    assert [round(b.t - a.t, 3) for a, b in pairwise(rows)] == [0.2] * (len(rows) - 1), (
        "one row per control tick, none invented"
    )
    assert max(row.measured_motor_rpm for row in rows) == turning
    assert rows[-1].state == "finished"
    assert rows[-1].measured_motor_rpm == 0
    assert all(row.sim_state for row in rows), "the simulated drive states its own state"
    assert all(row.hr_true is None for row in rows)
    assert all(row.drive_status_word is not None for row in rows if row.measured_fresh)

    kinds = [(event.kind, event.detail, event.actor) for event in recording.events]
    assert kinds[0] == (EventKind.OPERATOR_ACTION, "manual session started", ALIAS)
    assert (EventKind.PHASE, "hold", "system") in kinds
    assert (EventKind.OPERATOR_ACTION, "end_requested: operator pressed STOP", ALIAS) in kinds
    assert kinds[-1] == (EventKind.END, "operator_stop", "system")
    assert all(event.t == round(event.t, 3) for event in recording.events)
    assert recorded.said() == []
    assert not recorded.recorder.recording


async def test_ex1_a_second_session_gets_a_second_directory(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await stop(recorded, session, 5.0)
        assert recorded.state() is RuntimeState.FINISHED
        recorded.clock.advance(Seconds(2.0))
        await start_bench(recorded, session)
        await stop(recorded, session, 5.0)
    first, second = recorded.records()
    for path in (first, second):
        loaded = read(path)
        assert isinstance(loaded, Ok)
        assert loaded.value.warnings == ()
        assert loaded.value.manifest.end_reason == "operator_stop"
    assert first.name != second.name


async def test_ex1_a_programmed_session_freezes_its_programme_numbers_only(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path, env=PROGRAMME_ENV)
    async with recorded.rig.http() as session:
        await start_programme(session)
        await recorded.tick(2.0)
    manifest = recorded.recording().manifest
    assert (manifest.kind, manifest.occupancy) == ("auto", "occupied")
    profile = manifest.profile
    assert profile is not None
    assert (profile.zone_low_bpm, profile.zone_high_bpm, profile.max_rpm) == (118, 138, 276)
    assert profile.channels == ("ECG",)
    assert manifest.subject_id is None, "a start typed at the console names no rider"
    on_disk = recorded.everything_on_disk()
    assert SHORT_PROFILE.encode() not in on_disk
    assert b"failure injection" not in on_disk, "the profile's display name is not recorded"
    events = recorded.recording().events
    assert events[0].detail == "auto session started"
    assert (EventKind.PHASE, "baseline") in {(e.kind, e.detail) for e in events}


async def test_ex1_the_console_exit_closes_the_record_after_the_drive(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        await recorded.tick(15.0)
    detail = await recorded.rig.panel.close()
    recording = recorded.recording()
    assert recording.warnings == ()
    assert recording.manifest.end_reason == "shutdown"
    observation = recording.manifest.end_observation
    assert observation is not None
    assert observation.shutdown_detail == detail
    assert recording.events[-1].kind is EventKind.END
    assert await recorded.rig.left_stopped() == ""


async def test_ex1_an_idle_console_exits_without_writing_a_record(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    await recorded.tick(1.0)
    await recorded.rig.panel.close()
    assert recorded.records() == []


# =========================================================================
# What the record holds about people: an alias, never a name
# =========================================================================


async def test_ex1_no_operator_name_and_no_address_reaches_the_disk(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        await recorded.tick(5.0)
        pressed = await session.post(
            "/api/session/estop",
            json={"operator": "Second Person", "reason": "call jane.doe@example.org now"},
        )
        assert pressed.status_code == 200
        await recorded.tick(40.0)
        cleared = await session.post(
            "/api/safety/acknowledge", json={"operator": "Third Person", "estop_released": True}
        )
        assert cleared.status_code == 200, cleared.text
        await recorded.tick(2.0)
    recording = recorded.recording()
    on_disk = recorded.everything_on_disk().lower()
    for name in (OPERATOR, "second person", "third person", "jane.doe", "example.org"):
        assert name.encode() not in on_disk, f"{name!r} was written to the record"
    assert b"[redacted]" in on_disk
    actors = {event.actor for event in recording.events}
    assert {ALIAS, operator_alias("Second Person"), "system"} <= actors
    verdicts = [e.detail for e in recording.events if e.kind is EventKind.VERDICT]
    assert any(detail.startswith("operator_estop (QUICK_STOP)") for detail in verdicts)
    assert recording.manifest.end_reason == "emergency_stop"


def test_an_operator_alias_is_stable_opaque_and_never_the_name() -> None:
    alias = operator_alias("  Dr.  Attending ")
    assert alias == operator_alias("dr. attending")
    assert re.fullmatch(r"op-[0-9a-f]{16}", alias)
    assert operator_alias("someone else") != alias
    assert operator_alias("   ") == "unattributed"
    assert opaque("subject", "k57abc_DEF-9") == "k57abc_DEF-9"
    assert re.fullmatch(r"subject-[0-9a-f]{16}", opaque("subject", "Jean Dupont"))


def test_redaction_removes_whole_names_and_addresses_and_leaves_words_alone() -> None:
    names = ("al", "Dr. Attending")
    assert redact("manual session: Al said so", names) == "manual session: [redacted] said so"
    assert redact("by dr. attending <a@b.fr>", names) == "by [redacted] <[redacted]>"
    assert redact("nothing to hide", ()) == "nothing to hide"


def short_program(tmp_path: Path) -> Program:
    defaults = tmp_path / "defaults.json"
    defaults.write_text(SHORT_STORE, encoding="utf-8")
    store = ProfileStore(tmp_path / "profiles.json", defaults)
    assert isinstance(store.load(), Ok)
    resolved = store.resolve(
        SHORT_PROFILE, at=UnixMillis(1_700_000_000_123), total_duration_s=Seconds(420.0)
    )
    assert isinstance(resolved, Ok)
    return resolved.value


def test_the_manifest_of_a_dashboard_launch_carries_identifiers_only(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(12.5), UnixMillis(1_700_000_000_000))
    program = short_program(tmp_path)
    request = SessionRequest(
        occupancy=Occupancy.OCCUPIED,
        operator=f"Jean Dupont ({DASHBOARD_OPERATOR})",
        program=program,
        subject_id="k17subject",
        cloud_session_id="j57session",
    )
    manifest = manifest_for(STAMP, request, clock)
    assert (manifest.session_id, manifest.subject_id) == ("j57session", "k17subject")
    assert (manifest.machine_id, manifest.organization_id) == ("machine-1", "org-1")
    assert manifest.local_ref == manifest.record_id[:16]
    assert manifest.clocks.monotonic_start == 12.5
    assert manifest.started_at == "2023-11-14T22:13:32.500000Z"
    profile = manifest.profile
    assert profile is not None
    assert (profile.source_rev, profile.resolved_at, profile.total_overridden) == (
        program.source_rev,
        1_700_000_000_123,
        True,
    )
    assert profile.total_duration_s == 420.0
    assert "Jean" not in repr(manifest)
    assert "failure injection" not in repr(manifest)

    unsafe = manifest_for(
        STAMP, replace(request, subject_id="Jean Dupont", cloud_session_id="a b/c"), clock
    )
    assert re.fullmatch(r"subject-[0-9a-f]{16}", unsafe.subject_id or "")
    assert re.fullmatch(r"session-[0-9a-f]{16}", unsafe.session_id or "")
    assert manifest_for(STAMP, replace(request, subject_id="  "), clock).subject_id is None


def test_the_stamp_hashes_what_shapes_a_session_and_no_secret() -> None:
    limits = RuntimeLimits(slew=RpmPerSecond(15.0), start_hysteresis_rpm=MotorRpm(10))
    safety = SafetyLimits(hard_max_bpm=Bpm(148), critical_bpm=Bpm(158))
    env = {
        **BENCH_ENV,
        "RECORD_MACHINE_ID": "machine-7",
        "RECORD_ORGANIZATION_ID": "org-9",
        "ANHEART_SOFTWARE_VERSION": "1.4.2+abc",
    }
    base = stamp_for(config_of(env), limits, safety, DEFAULT_MOTION_LIMITS)
    assert (base.machine_id, base.organization_id, base.software_version) == (
        "machine-7",
        "org-9",
        "1.4.2+abc",
    )
    assert re.fullmatch(r"[0-9a-f]{64}", base.config_hash)
    assert re.fullmatch(r"[0-9a-f]{64}", base.medical_parameters_version)

    secrets = {
        **env,
        "UI_TOKEN": "a-console-token",
        "MACHINE_API_KEY": "machine-key",
        "CONVEX_URL": "https://example.convex.site",
    }
    assert stamp_for(config_of(secrets), limits, safety, DEFAULT_MOTION_LIMITS) == base

    other_radius = stamp_for(
        config_of({**env, "ARM_RADIUS_M": "1.6"}), limits, safety, DEFAULT_MOTION_LIMITS
    )
    assert other_radius.config_hash != base.config_hash
    assert other_radius.medical_parameters_version == base.medical_parameters_version

    other_tiers = stamp_for(
        config_of(env),
        limits,
        SafetyLimits(hard_max_bpm=Bpm(150), critical_bpm=Bpm(160)),
        DEFAULT_MOTION_LIMITS,
    )
    assert other_tiers.medical_parameters_version != base.medical_parameters_version
    assert other_tiers.config_hash != base.config_hash

    serial = stamp_for(
        config_of({**env, "MOTOR_BACKEND": "serial", "MOTOR_PORT": "/dev/ttyUSB0"}),
        limits,
        safety,
        DEFAULT_MOTION_LIMITS,
    )
    assert serial.config_hash != base.config_hash


# =========================================================================
# EX-4: no arming under 500 MB free
# =========================================================================


def low_disk(monkeypatch: pytest.MonkeyPatch, free: int | None) -> None:
    def measured(_root: Path, at: Monotonic) -> Storage:
        return Storage(free, at)

    monkeypatch.setattr(journal_module, "measure", measured)


@pytest.mark.parametrize("free", [0, 499_999_999])
async def test_ex4_a_start_is_refused_under_500_mb_free_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free: int
) -> None:
    low_disk(monkeypatch, free)
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await recorded.tick(1.0)
    runtime = recorded.rig.panel.runtime
    assert runtime.state is RuntimeState.IDLE
    assert runtime.output_enabled is False
    assert recorded.records() == []
    assert recorded.rig.refusals() == [
        "demarrage refuse : espace disque insuffisant pour l'enregistrement de seance : "
        f"{free // 1_000_000} Mo libres sous {recorded.root}, 500 Mo requis. Liberer de l'espace"
    ]


async def test_ex4_exactly_500_mb_free_is_enough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    low_disk(monkeypatch, MIN_FREE_BYTES)
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
    assert recorded.state() is RuntimeState.RUNNING
    assert recorded.rig.refusals() == []
    assert len(recorded.records()) == 1


async def test_ex4_an_unmeasurable_directory_refuses_a_programme_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    low_disk(monkeypatch, None)
    recorded = recorded_rig(tmp_path, env=PROGRAMME_ENV)
    async with recorded.rig.http() as session:
        await start_programme(session)
        await recorded.tick(1.0)
    assert recorded.state() is RuntimeState.IDLE
    assert recorded.rig.refusals() == [
        "demarrage refuse : enregistrement de seance impossible, espace libre illisible sous "
        f"{recorded.root} (dossier absent, droits, disque)"
    ]
    # An unusable directory is said once, before anybody tries to start.
    assert recorded.said() == [
        f"enregistrement de seance indisponible : dossier {recorded.root} inutilisable "
        "(creation, droits ou mesure de l'espace libre)"
    ]


def test_ex4_the_gate_reads_the_last_measurement_and_names_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    free = [MIN_FREE_BYTES - 1]

    def measured(_root: Path, at: Monotonic) -> Storage:
        return Storage(free[0], at)

    monkeypatch.setattr(journal_module, "measure", measured)
    clock = ManualClock()
    journal = Journal(tmp_path / "records", clock)
    gate = storage_gate(journal, clock)
    refusal = gate()
    assert refusal is not None
    assert refusal == RecordStorageLow(MIN_FREE_BYTES - 1, MIN_FREE_BYTES, str(journal.root))
    assert describe_start_refusal(refusal).startswith("demarrage refuse : espace disque")
    free[0] = MIN_FREE_BYTES
    assert gate() == refusal, "the gate measures nothing itself"
    clock.advance(Seconds(5.0))
    journal.drain()
    assert gate() is None
    unreadable = RecordStorageLow(None, MIN_FREE_BYTES, "/x")
    assert "illisible sous /x" in describe_record_storage(unreadable)


def test_ex4_a_measurement_nobody_refreshed_for_fifteen_seconds_refuses_whatever_it_said(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal thread stuck on a dead disk leaves its last number behind: it must not arm."""
    reads: list[Path] = []

    def measured(root: Path, at: Monotonic) -> Storage:
        reads.append(root)
        return Storage(10 * MIN_FREE_BYTES, at)

    monkeypatch.setattr(journal_module, "measure", measured)
    clock = ManualClock(Monotonic(50.0))
    journal = Journal(tmp_path / "records", clock)
    gate = storage_gate(journal, clock)
    assert gate() is None

    clock.advance(STORAGE_STALE_AFTER)
    assert gate() is None, "fifteen seconds old exactly is still evidence"
    clock.advance(Seconds(0.25))
    refusal = gate()
    assert refusal is not None
    assert refusal == RecordStorageLow(
        10 * MIN_FREE_BYTES, MIN_FREE_BYTES, str(journal.root), stale_for=Seconds(15.25)
    )
    assert describe_start_refusal(refusal) == (
        "demarrage refuse : enregistrement de seance impossible, espace libre sous "
        f"{journal.root} mesure il y a 15 s : le disque ne repond plus"
    )
    assert len(reads) == 1, "the gate reads a value and a clock: it never asks the disk"

    # The thread comes back, measures again, and the gate opens again.
    journal.drain()
    assert len(reads) == 2
    assert gate() is None


async def test_ex4_a_console_whose_journal_is_stuck_refuses_the_start_and_says_why(
    tmp_path: Path,
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        # Sixteen seconds of console with nobody draining: the journal thread is stuck.
        await recorded.tick(16.0, drain=False)
        await attest(session)
        started = await session.post(
            "/api/manual/start", json={"occupancy": "bench", "operator": OPERATOR}
        )
        assert started.status_code == 202
        await recorded.tick(1.0, drain=False)
    assert recorded.state() is RuntimeState.IDLE
    assert recorded.rig.panel.runtime.output_enabled is False
    assert recorded.rig.refusals() == [
        "demarrage refuse : enregistrement de seance impossible, espace libre sous "
        f"{recorded.root} mesure il y a 16 s : le disque ne repond plus"
    ]


# =========================================================================
# EX-5: raw blocks as received, and what the link lost
# =========================================================================


async def test_ex5_every_raw_block_is_written_with_its_sequence_number(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path, env={**BENCH_ENV, "SENSORS": "ECG,EDA"})
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await recorded.tick(4.0)
        # Written as it arrives: the blocks are on disk while the session runs.
        on_disk = sorted(p.name for p in (recorded.only_record() / "ecg_raw").iterdir())
        assert on_disk[:3] == ["000000.bin.gz", "000001.bin.gz", "000002.bin.gz"]
        await stop(recorded, session, 5.0)
    recording = recorded.recording()
    blocks = recording.raw
    assert [block.header.seq for block in blocks] == list(range(len(blocks)))
    assert len(blocks) >= 20
    assert all(block.header.channels == ("ECG", "EDA") for block in blocks)
    assert all(block.header.n_samples == 200 for block in blocks)
    starts = [block.header.t_first for block in blocks]
    assert [round(b - a, 3) for a, b in pairwise(starts)] == [0.2] * (len(starts) - 1)
    assert all(0 <= value <= 1023 for block in blocks for value in block.samples[0])
    channels = {row.channel for row in recording.sensors}
    assert channels == {"ECG", "EDA"}


def recorder_with_link(tmp_path: Path, stats: list[LinkStats]) -> tuple[SessionRecorder, Journal]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)

    def read_stats() -> LinkStats:
        return stats[0]

    recorder = SessionRecorder(
        clock=clock,
        journal=journal,
        stamp=STAMP,
        radius=Metres(1.5),
        leg_tip=Metres(2.43),
        link_stats=read_stats,
    )
    return recorder, journal


def test_ex5_a_batch_lost_by_the_bitalino_is_a_warning_with_the_loss_counters(
    tmp_path: Path,
) -> None:
    stats = [LinkStats(frames=1000, filled_samples=3)]
    recorder, journal = recorder_with_link(tmp_path, stats)
    recorder.note_link()  # no session: nothing to note
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    recorder.note_link()  # the session's baseline: what was lost before it is not its loss
    stats[0] = LinkStats(frames=1200, filled_samples=3)
    recorder.note_link()  # frames alone are not a loss... but any change is reported
    stats[0] = LinkStats(
        frames=1400,
        filled_samples=19,
        dropped_backlog_samples=200,
        sync_losses=2,
        skipped_bytes=7,
        reconnects=1,
    )
    recorder.note_link()
    recorder.note_link()  # unchanged: nothing more
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    warnings = [e.detail for e in loaded.value.events if e.kind is EventKind.WARNING]
    assert warnings == [
        "bitalino_loss: filled_samples=+0 dropped_backlog_samples=+0 sync_losses=+0 "
        "skipped_bytes=+0 reconnects=+0 totals=3/0/0/0/0",
        "bitalino_loss: filled_samples=+16 dropped_backlog_samples=+200 sync_losses=+2 "
        "skipped_bytes=+7 reconnects=+1 totals=19/200/2/7/1",
    ]


def test_ex5_a_block_the_queue_refuses_leaves_a_hole_in_the_numbering(tmp_path: Path) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(
        tmp_path / "records", clock, limits=journal_module.Limits(entries=100, samples=400)
    )
    recorder = SessionRecorder(
        clock=clock, journal=journal, stamp=STAMP, radius=Metres(1.5), leg_tip=Metres(1.5)
    )

    def acquired(samples: int) -> SampleBatch:
        return SampleBatch(
            timestamp=clock.unix_millis(),
            channels=[ChannelData(channel="ECG", values=[512.0] * samples)],
        )

    recorder.note_batch(acquired(200))  # no session yet
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    recorder.note_batch(acquired(200))
    recorder.note_batch(SampleBatch(timestamp=clock.unix_millis(), channels=[]))
    recorder.note_batch(
        SampleBatch(timestamp=clock.unix_millis(), channels=[ChannelData(channel="ECG", values=[])])
    )
    recorder.note_batch(acquired(200))
    recorder.note_batch(acquired(200))  # refused: the sample bound is reached
    journal.drain()
    recorder.note_batch(acquired(200))
    journal.drain()
    path = journal.status(clock.monotonic()).path
    assert path is not None
    names = sorted(p.name for p in (path / "ecg_raw").iterdir())
    assert names == ["000000.bin.gz", "000001.bin.gz", "000003.bin.gz"]
    recorder.refresh(clock.monotonic())
    assert recorder.degraded
    assert recorder.status.cause is Cause.QUEUE_FULL


# =========================================================================
# EX-3: a bad disk costs the record, never the session
# =========================================================================


async def test_ex3_a_full_disk_never_stops_the_session_and_the_operator_is_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        await recorded.tick(2.0)
        assert not recorded.degraded()

        refuse_writes(monkeypatch, "ticks.csv", errno.ENOSPC, torn=b"12.3,runn")
        await recorded.tick(30.0)

        runtime = recorded.rig.panel.runtime
        snapshot = runtime.snapshot()
        assert recorded.state() is RuntimeState.RUNNING
        assert snapshot.safety_action is SafetyAction.NONE
        assert snapshot.measured.motor_rpm == runtime.manual_target, "the climb went on"
        assert recorded.degraded()
        assert recorded.said() == [
            "enregistrement de seance degrade : disque plein (append). "
            "La seance et la securite continuent."
        ]
        await stop(recorded, session, 60.0)

    assert recorded.state() is RuntimeState.FINISHED
    assert await recorded.rig.left_stopped() == ""
    recording = recorded.recording()
    assert recording.manifest.end_reason == "operator_stop"
    warnings = [e.detail for e in recording.events if e.kind is EventKind.WARNING]
    assert warnings[0].startswith("record_degraded: dropped=0 failures=")
    assert "last=append:OSError:ENOSPC" in warnings[0]
    assert len(recording.rows) == 11, "the rows written before the disk filled up are intact"


async def test_ex3_a_recorder_bug_costs_the_record_and_never_a_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    recorded = recorded_rig(tmp_path)

    def broken(_self: Journal, _entry: journal_module.Entry) -> bool:
        raise RuntimeError("injected: a bug in the recorder's path")

    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        monkeypatch.setattr(Journal, "submit", broken)
        with caplog.at_level(logging.ERROR, logger="src.record.session"):
            await recorded.tick(20.0)
            turning = recorded.rig.panel.runtime.snapshot().measured.motor_rpm
            pressed = await session.post("/api/session/estop", json={})
            assert pressed.status_code == 200
            await recorded.tick(60.0)
        assert turning > 55, "the session went on turning behind the broken recorder"
        runtime = recorded.rig.panel.runtime
        assert runtime.end_reason is EndReason.EMERGENCY_STOP, "the stop still works"
    assert recorded.degraded()
    assert recorded.recorder.is_degraded()
    assert recorded.said() == [BROKEN]
    assert caplog.text.count("session recorder:") == 1, "one traceback, not one per tick"
    assert await recorded.rig.left_stopped() == ""


def status_with(cause: Cause | None, error: RecordError | None = None) -> JournalStatus:
    return JournalStatus(
        recording=True,
        degraded=cause is not None,
        cause=cause,
        error=error,
        dropped=4,
        failures=2,
        pending=17,
        path=None,
        free_bytes=1,
    )


def test_ex3_every_cause_has_its_sentence_for_the_operator() -> None:
    root = Path("/srv/records")
    safe = "La seance et la securite continuent."
    assert describe_status(status_with(None), root) == "enregistrement de seance retabli"
    assert describe_status(status_with(Cause.STALLED), root) == (
        "enregistrement de seance degrade : le disque ne repond plus (17 elements en attente). "
        f"{safe}"
    )
    assert describe_status(status_with(Cause.QUEUE_FULL), root) == (
        "enregistrement de seance degrade : file d'ecriture pleine, des mesures sont perdues. "
        f"{safe}"
    )
    assert "dossier /srv/records inutilisable" in describe_status(
        status_with(Cause.STORAGE_UNAVAILABLE), root
    )
    for detail, said in (
        ("OSError:ENOSPC", "disque plein (append)"),
        ("OSError:EDQUOT", "disque plein (append)"),
        ("PermissionError:EACCES", "ecriture refusee, droits insuffisants (append)"),
        ("OSError:EROFS", "ecriture refusee, droits insuffisants (append)"),
        ("OSError:EIO", "erreur d'ecriture (append : OSError:EIO)"),
    ):
        described = describe_status(
            status_with(Cause.WRITE_FAILED, RecordError("append", detail)), root
        )
        assert described == f"enregistrement de seance degrade : {said}. {safe}"
    assert "aucun dossier d'enregistrement ouvert" in describe_status(
        status_with(Cause.WRITE_FAILED), root
    )


def test_a_cause_or_an_event_kind_nobody_classified_fails_loudly() -> None:
    """The ``assert_never`` guards: a new member must be given words, not fall through."""
    with pytest.raises(AssertionError):
        describe_status(status_with(cast("Cause", "worn_out")), Path("/srv/records"))
    with pytest.raises(AssertionError):
        record_kind(cast("SurfaceEvent", "something_new"))


async def test_a_drive_fault_is_an_event_with_its_mnemonic_and_its_code(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await set_target(session, 5.0)
        await recorded.tick(15.0)
        recorded.rig.simulator.inject_fault(DriveFault.OVERCURRENT)
        await recorded.tick(180.0)
        # Nobody reset the fault: the runtime is still ENDING, and stays so.
        assert recorded.state() is RuntimeState.ENDING
        rows_at_close = len(recorded.recording().rows)
        await recorded.tick(30.0)
    recording = recorded.recording()
    assert recording.warnings == ()
    faults = [e.detail for e in recording.events if e.kind is EventKind.DRIVE_FAULT]
    assert len(faults) == 1
    assert faults[0].startswith("OCF (LFT ")
    rules = [e.detail for e in recording.events if e.kind is EventKind.VERDICT]
    assert any(rule.startswith("drive_fault (") for rule in rules)
    # The record closed when the session's phase reached DONE; it does not grow
    # for as long as the fault stays latched.
    assert recording.manifest.end_reason == "safety_verdict"
    assert len(recording.rows) == rows_at_close
    assert recording.rows[-1].phase == "done"
    observation = recording.manifest.end_observation
    assert observation is not None
    assert (observation.runtime_state, observation.drive_state) == ("ending", "FAULT")
    assert observation.shaft_motor_rpm == 0


async def test_ex3_the_operator_is_told_when_the_record_recovers(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        # The disk stops answering: the console ticks on, nothing is drained.
        await recorded.tick(6.0, drain=False)
        assert recorded.recorder.status.cause is Cause.STALLED
        assert recorded.state() is RuntimeState.RUNNING
        await recorded.tick(1.0)
    said = recorded.said()
    assert len(said) == 2
    assert said[0].startswith("enregistrement de seance degrade : le disque ne repond plus")
    assert said[1] == "enregistrement de seance retabli"
    assert not recorded.degraded()


# =========================================================================
# The rest of the recorder's edges
# =========================================================================


def bare_recorder(tmp_path: Path) -> tuple[SessionRecorder, Journal, ManualClock]:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_700_000_000_000))
    journal = Journal(tmp_path / "records", clock)
    recorder = SessionRecorder(
        clock=clock, journal=journal, stamp=STAMP, radius=Metres(1.5), leg_tip=Metres(1.5)
    )
    return recorder, journal, clock


def surface_event(kind: SurfaceEvent, operator: str, detail: str, at: float) -> SessionEvent:
    return SessionEvent(
        kind=kind,
        at=Monotonic(at),
        wall_clock=UnixMillis(1_700_000_000_000),
        operator=operator,
        detail=detail,
    )


def test_console_events_land_in_the_records_closed_vocabulary(tmp_path: Path) -> None:
    recorder, journal, _clock = bare_recorder(tmp_path)
    recorder.note_event(surface_event(SurfaceEvent.REFUSED, OPERATOR, "before any session", 9.0))
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    remote = f"Jean Dupont ({DASHBOARD_OPERATOR})"
    for kind, operator, detail in (
        (SurfaceEvent.START_REQUESTED, OPERATOR, "manuel bench"),
        (SurfaceEvent.FAULT_RESET_REQUESTED, OPERATOR, "reset defaut"),
        (SurfaceEvent.ATTESTED, OPERATOR, "wiring attested"),
        (SurfaceEvent.EMERGENCY_STOP, "", "unattributed: pressed"),
        (SurfaceEvent.ACKNOWLEDGED, OPERATOR, "operator_estop"),
        (SurfaceEvent.REFUSED, OPERATOR, f"consigne refusee par {OPERATOR}"),
        (SurfaceEvent.END_REQUESTED, remote, "arret demande par Jean Dupont"),
        (SurfaceEvent.REFUSED, remote, "refuse"),
        (SurfaceEvent.SESSION_RUNNING, "", "session running"),
        (SurfaceEvent.SESSION_IDLE, "", "session idle"),
        (SurfaceEvent.RECORDING, "", "enregistrement degrade"),
    ):
        recorder.note_event(surface_event(kind, operator, detail, 10.5))
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    seen = [(e.kind.value, e.detail, e.actor) for e in loaded.value.events[1:]]
    assert seen == [
        ("operator_action", "start_requested: manuel bench", ALIAS),
        ("operator_action", "fault_reset_requested: reset defaut", ALIAS),
        ("operator_action", "attested: wiring attested", ALIAS),
        ("operator_action", "emergency_stop: unattributed: pressed", "system"),
        ("verdict_ack", "acknowledged: operator_estop", ALIAS),
        ("refusal", "refused: consigne refusee par [redacted]", ALIAS),
        ("remote_command", "end_requested: arret demande par [redacted]", "remote"),
        ("refusal", "refused: refuse", "remote"),
    ]
    assert all(event.t == 0.5 for event in loaded.value.events[1:])


def test_a_long_reason_is_cut_to_a_message_and_a_remote_name_is_known_both_ways(
    tmp_path: Path,
) -> None:
    assert names_of("  Jean   Dupont (tableau de bord) ") == (
        "Jean Dupont (tableau de bord)",
        "Jean Dupont",
    )
    assert names_of("tableau de bord") == ("tableau de bord",)
    assert names_of("   ") == ()
    recorder, journal, _clock = bare_recorder(tmp_path)
    recorder.begin(
        SessionRequest(occupancy=Occupancy.BENCH, operator="Jean Dupont (tableau de bord)")
    )
    recorder.note_event(
        surface_event(SurfaceEvent.REFUSED, "", "Jean Dupont a dit : " + "x" * 2000, 11.0)
    )
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    detail = loaded.value.events[-1].detail
    assert detail.startswith("refused: [redacted] a dit : xxx")
    assert len(detail) == MAX_TEXT - len("Jean Dupont") + len("[redacted]")


def test_past_too_many_operator_names_the_text_is_withheld_not_leaked(tmp_path: Path) -> None:
    recorder, journal, _clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    for index in range(MAX_NAMES + 5):
        recorder.note_event(
            surface_event(
                SurfaceEvent.REFUSED, f"visitor {index}", f"said by visitor {index}", 11.0
            )
        )
    journal.drain()
    path = journal.status(Monotonic(0.0)).path
    assert path is not None
    loaded = read(path)
    assert isinstance(loaded, Ok)
    details = [event.detail for event in loaded.value.events[1:]]
    assert details[0] == "refused: said by [redacted]"
    assert details[-1] == "[redacted]"
    assert not any("visitor" in detail for detail in details)


def test_a_session_begun_over_an_open_one_closes_it_as_interrupted(tmp_path: Path) -> None:
    recorder, journal, clock = bare_recorder(tmp_path)
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=""))
    journal.drain()
    first = journal.status(clock.monotonic()).path
    assert first is not None
    clock.advance(Seconds(2.0))
    recorder.begin(SessionRequest(occupancy=Occupancy.BENCH, operator=OPERATOR))
    journal.drain()
    loaded = read(first)
    assert isinstance(loaded, Ok)
    assert loaded.value.manifest.end_reason == INTERRUPTED
    assert loaded.value.manifest.operator == "unattributed"
    assert recorder.recording


async def test_a_record_closed_before_the_runtime_ended_says_interrupted(tmp_path: Path) -> None:
    recorded = recorded_rig(tmp_path)
    async with recorded.rig.http() as session:
        await start_bench(recorded, session)
        await recorded.tick(1.0)
    runtime = recorded.rig.panel.runtime
    recorded.recorder.finish(recorded.clock.monotonic(), runtime, "said by the caller")
    recorded.recorder.finish(recorded.clock.monotonic(), runtime, "nothing left to close")
    recorded.drain()
    manifest = recorded.recording().manifest
    assert manifest.end_reason == INTERRUPTED
    observation = manifest.end_observation
    assert observation is not None
    assert observation.runtime_state == "running"
    assert observation.shutdown_detail == "said by the caller"
    assert observation.stop_reason is None
    assert observation.lfrd_motor_rpm == 0


def test_open_journal_resolves_the_directory_under_the_project_or_takes_it_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_panel, "PROJECT_ROOT", tmp_path / "project")
    clock = ManualClock()
    relative = open_journal(config_of(BENCH_ENV), clock)
    assert relative.root == tmp_path / "project" / "data" / "records"
    assert relative.root.is_dir()
    absolute = open_journal(
        config_of({**BENCH_ENV, "RECORD_ROOT": str(tmp_path / "elsewhere")}), clock
    )
    assert absolute.root == tmp_path / "elsewhere"
