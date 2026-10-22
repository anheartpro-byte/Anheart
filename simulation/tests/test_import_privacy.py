import gzip
import io
from dataclasses import replace
from pathlib import Path

import pytest
from tests.record_support import manifest

from simulation.import_privacy import (
    NEUTRAL_FOLDER,
    NEUTRAL_MILLIS,
    NEUTRAL_UTC,
    anonymous_events,
    anonymous_manifest,
    identifying_content,
    identifying_frames,
    identities,
    machine_detail,
    remaining_identity,
)
from simulation.real_records import ANONYMIZED, UNVERSIONED
from src.record.reader import Recording
from src.record.schema import DriveFrame, EndObservation, Event, EventKind
from src.result import Err, Ok
from src.units import Seconds


def recording(*events: Event, raw_hex: str | None = None) -> Recording:
    frame = DriveFrame(
        t=1, kind="modbus_send", register=None, value=None, ok=True, latency_ms=0, raw_hex=raw_hex
    )
    return Recording(manifest(), (), tuple(events), (frame,), (), (), ())


@pytest.mark.parametrize(
    ("kind", "text", "expected"),
    [
        (EventKind.OPERATOR_ACTION, "stop", "stop"),
        (EventKind.REMOTE_COMMAND, "manual_target output_rpm=3.0", "manual_target output_rpm=3.0"),
        (
            EventKind.REMOTE_COMMAND,
            "manual_target output_rpm=03.000",
            "manual_target output_rpm=3.0",
        ),
        (
            EventKind.VERDICT_ACK,
            "acknowledge estop_released=true",
            "acknowledge estop_released=true",
        ),
        (EventKind.VERDICT, "hr_stale (FREEZE): 999 bpm SECRET 2026-10-15", "hr_stale (FREEZE)"),
        (EventKind.VERDICT, "sto_open (QUICK_STOP): SECRET", "sto_open (QUICK_STOP)"),
        (EventKind.VERDICT, "sto_unreadable (RAMP_DOWN): SECRET", "sto_unreadable (RAMP_DOWN)"),
        (EventKind.VERDICT, "unknown (FREEZE): SECRET", ANONYMIZED),
        (EventKind.VERDICT, "hr_stale (UNKNOWN): SECRET", ANONYMIZED),
        (EventKind.VERDICT, "SECRET", ANONYMIZED),
        (EventKind.VERDICT, "cleared", "cleared"),
        (EventKind.DRIVE_FAULT, "nOF (LFT 0): SECRET", "nOF (LFT 0)"),
        (EventKind.DRIVE_FAULT, "bad (LFT 1)", ANONYMIZED),
        (EventKind.DRIVE_FAULT, "nOF (LFT 65536)", ANONYMIZED),
        (EventKind.DRIVE_FAULT, "SECRET", ANONYMIZED),
        (EventKind.DRIVE_FAULT, "cleared", "cleared"),
        (EventKind.PHASE, "hold", "hold"),
        (EventKind.PHASE, "hold SECRET", ANONYMIZED),
        (EventKind.END, "operator_stop", "operator_stop"),
        (EventKind.END, "SECRET", ANONYMIZED),
        (
            EventKind.WARNING,
            "record_degraded: dropped=1 failures=2 drive_frames_lost=3 last=SECRET",
            "record_degraded: dropped=1 failures=2 drive_frames_lost=3",
        ),
        (
            EventKind.WARNING,
            "bitalino_loss: filled_samples=+1 reconnects=+2 totals=1/2/3/4/5 SECRET",
            "bitalino_loss: filled_samples=+1 reconnects=+2 totals=1/2/3/4/5",
        ),
        (EventKind.WARNING, "dashboard: SECRET 999 bpm 2026-10-15", "dashboard:"),
        (EventKind.WARNING, "console: SECRET", "console:"),
        (EventKind.WARNING, "replay_config: SECRET", "replay_config:"),
        (EventKind.WARNING, "replay_preamble: SECRET", "replay_preamble:"),
        (
            EventKind.WARNING,
            "replay_incomplete: pending_ecg_delivery",
            "replay_incomplete: pending_ecg_delivery",
        ),
        (EventKind.WARNING, "replay_incomplete: SECRET", "replay_incomplete:"),
        (EventKind.WARNING, "unknown: SECRET", ANONYMIZED),
        (EventKind.REFUSAL, "SECRET 999 bpm", ANONYMIZED),
        (EventKind.PREFLIGHT, "SECRET", ANONYMIZED),
    ],
)
def test_only_closed_machine_facts_survive(kind: EventKind, text: str, expected: str) -> None:
    assert machine_detail(Event(t=-1.2, kind=kind, detail=text, actor="op-secret")) == Ok(expected)


def test_commands_are_never_replaced_by_an_ignored_event() -> None:
    event = Event(t=-1.2, kind=EventKind.OPERATOR_ACTION, detail="stop SECRET", actor="op-secret")
    assert isinstance(machine_detail(event), Err)
    assert isinstance(anonymous_events(recording(event)), Err)


def test_actors_and_relative_instants_are_preserved_by_role() -> None:
    original = recording(
        *(
            Event(t=-1.2, kind=EventKind.PHASE, detail="hold", actor=actor)
            for actor in ("system", "remote", "op-secret", "another-operator")
        )
    )
    events = anonymous_events(original)
    assert isinstance(events, Ok)
    assert [event.actor for event in events.value] == ["system", "remote", ANONYMIZED, ANONYMIZED]
    assert [event.t for event in events.value] == [-1.2] * 4
    known = identities(original)
    assert "op-secret" in known
    assert "another-operator" in known
    assert "system" not in known
    assert "remote" not in known


def test_only_identities_dates_and_export_version_change_in_manifest() -> None:
    source = manifest()
    source = replace(
        source, clocks=replace(source.clocks, wall_trusted=True, ntp_offset_s=Seconds(12.3))
    )
    anon = anonymous_manifest(source, "opaque_01")
    assert anon.started_at == anon.ended_at == anon.clocks.utc_start == NEUTRAL_UTC
    assert anon.clocks.wall_trusted is None
    assert anon.clocks.ntp_offset_s is None
    assert anon.clocks.monotonic_start == source.clocks.monotonic_start
    assert anon.profile is None
    assert anon.software_version == UNVERSIONED
    assert (
        replace(
            anon,
            record_id=source.record_id,
            local_ref=source.local_ref,
            machine_id=source.machine_id,
            organization_id=source.organization_id,
            session_id=source.session_id,
            subject_id=source.subject_id,
            operator=source.operator,
            started_at=source.started_at,
            ended_at=source.ended_at,
            clocks=source.clocks,
            profile=source.profile,
            software_version=source.software_version,
        )
        == source
    )
    assert anonymous_manifest(replace(source, profile=None), "opaque_02").profile is None


def test_final_observation_loses_only_its_two_free_text_fields() -> None:
    observation = EndObservation(
        t=12.4,
        runtime_state="finished",
        drive_state="disabled",
        runtime_output_enabled=False,
        runtime_applied_rpm=0,
        lfrd_motor_rpm=0,
        shaft_motor_rpm=0,
        energised=False,
        silent=False,
        stop_reason="Mme Martin FC 192 bpm",
        shutdown_detail="port /dev/serial/by-id/FTDI-SECRET",
    )
    source = replace(manifest(), end_observation=observation)
    anon = anonymous_manifest(source, "opaque_01")
    assert anon.end_observation == replace(observation, stop_reason=None, shutdown_detail=None)
    assert source.end_observation == observation


@pytest.mark.parametrize(
    "content",
    [
        b"2026-10-15T15:10:01Z",
        b"2026/10/15",
        b"20261015T151001Z",
        b"1792077001",
        b"1792077001.5",
        b"1792077001000",
        b"16/10/2026",
        b"16.10.2026",
        b"s20261016",
        b"20261016",
        b"1792077001000000",
        b"OP-SECRET",
        b"somebody@example.test",
    ],
)
def test_dates_identities_and_addresses_are_detected(content: bytes) -> None:
    assert identifying_content(content, ("op-secret",))


def test_neutral_dates_and_relative_instants_are_not_identifying() -> None:
    assert not identifying_content(
        f"{NEUTRAL_UTC} {NEUTRAL_FOLDER} {NEUTRAL_MILLIS} -11.8 0.2".encode(), ("op-secret",)
    )
    assert not identifying_content(b"0.4545454545455 -11.8000000001", ())
    assert not identifying_content(b"a10b28451935c20d20261016e22f", ())
    assert not identifying_content(b"\x00somebody@example.test\xff", (), text=False)


@pytest.mark.parametrize("metadata", ["date", "name", "second_member"])
def test_the_gzip_envelope_cannot_keep_an_absolute_date_or_name(
    tmp_path: Path, metadata: str
) -> None:
    folder = tmp_path / "record"
    folder.mkdir()
    payload = b"{}\n\x00\x01"
    if metadata == "date":
        content = gzip.compress(payload, mtime=1_700_000_000)
    elif metadata == "name":
        stream = io.BytesIO()
        with gzip.GzipFile(filename="op-secret", fileobj=stream, mode="wb", mtime=0) as writer:
            writer.write(payload)
        content = stream.getvalue()
    else:
        content = gzip.compress(payload, mtime=0) + gzip.compress(b"", mtime=1_700_000_000)
    (folder / "000000.bin.gz").write_bytes(content)
    assert remaining_identity(folder, ())


@pytest.mark.parametrize(
    "where", ["plain", "header", "header_email", "samples", "name", "hex", "bad_hex", "empty_hex"]
)
def test_every_file_raw_header_and_decoded_transport_is_checked(tmp_path: Path, where: str) -> None:
    content = b"op-secret 2026-10-15T15:10:01Z"
    folder = tmp_path / "record"
    folder.mkdir()
    (folder / "empty").mkdir()
    (folder / "safe.txt").write_bytes(b"ordinary")
    if where == "plain":
        (folder / "drive_frames.jsonl").write_bytes(content)
    elif where == "header":
        (folder / "000000.bin.gz").write_bytes(gzip.compress(content + b"\n\x00\x01", mtime=0))
    elif where == "header_email":
        (folder / "000000.bin.gz").write_bytes(
            gzip.compress(b"somebody@example.test\n\x00\xff", mtime=0)
        )
    elif where == "samples":
        (folder / "000000.bin.gz").write_bytes(gzip.compress(b"{}\n" + content, mtime=0))
    elif where == "name":
        (folder / "op-secret").mkdir()
    else:
        raw_hex = {"hex": content.hex(), "bad_hex": "not hex", "empty_hex": ""}[where]
        assert identifying_frames(recording(raw_hex=raw_hex), ("op-secret",)) is (
            where != "empty_hex"
        )
        assert not remaining_identity(folder, ("op-secret",))
        return
    assert remaining_identity(folder, ("op-secret",))
    assert not identifying_frames(recording(), ("op-secret",))
