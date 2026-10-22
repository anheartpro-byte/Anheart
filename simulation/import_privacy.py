from __future__ import annotations

import re
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Final

from simulation.harness import REMOTE, SYSTEM
from simulation.real_records import ANONYMIZED, LOCAL_REF_LENGTH, UNVERSIONED, library_identity
from src.motor.drive import DriveFault
from src.presence.monitor import MOTION_RULES, START_RULES
from src.record import commands
from src.record.codec import EMAIL
from src.record.reader import Recording
from src.record.schema import Event, EventKind, Manifest
from src.result import Err, Ok, Result
from src.training.runtime import RULE_STO_OPEN, RULE_STO_UNREADABLE, EndReason
from src.training.safety import ALL_RULES
from src.training.types import Phase, SafetyAction

NEUTRAL_UTC: Final[str] = "2000-01-01T00:00:00Z"
NEUTRAL_MILLIS: Final[int] = 946_684_800_000
NEUTRAL_FOLDER: Final[str] = "2000-01-01T000000Z"
_DATES: Final = re.compile(
    rb"(?<![0-9A-Za-z])(?:[12][0-9]{3}[-/.][01][0-9][-/\.][0-3][0-9]"
    rb"(?:T[0-9:.]+Z)?|[0-3][0-9][/.][01][0-9][/.][12][0-9]{3})"
    rb"|(?<![0-9A-Za-z])(?:[A-Za-z_]+)?[12][0-9]{3}[01][0-9][0-3][0-9]"
    rb"(?:T[0-9]{6}Z)?(?![0-9A-Za-z])"
    rb"|(?<![0-9A-Za-z.])[1-9][0-9]{9}(?:[0-9]{3}|[0-9]{6})?(?![0-9A-Za-z])"
)
_RULES: Final = frozenset(
    (*ALL_RULES, *MOTION_RULES, *START_RULES, RULE_STO_OPEN, RULE_STO_UNREADABLE)
)
_ACTIONS: Final = frozenset(action.name for action in SafetyAction)
_FAULTS: Final = frozenset(fault.mnemonic for fault in DriveFault)
_PHASES: Final = frozenset(phase.value for phase in Phase)
_MAX_REGISTER: Final[int] = 65_535
_ENDS: Final = frozenset((*[reason.value for reason in EndReason], "interrupted", "superseded"))
_WARNINGS: Final = frozenset(
    {
        "record_degraded",
        "replay_config",
        "replay_preamble",
        "replay_incomplete",
        "console",
        "dashboard",
        "bitalino_loss",
    }
)
_COUNTERS: Final = {
    "record_degraded": re.compile(r"(?:dropped|failures|drive_frames_lost)=[0-9]+"),
    "bitalino_loss": re.compile(
        r"(?:filled_samples|dropped_backlog_samples|sync_losses|skipped_bytes|reconnects)="
        r"\+?[0-9]+|totals=[0-9]+(?:/[0-9]+){4}"
    ),
}


def anonymous_manifest(manifest: Manifest, name: str) -> Manifest:
    identity = library_identity(name)
    profile = manifest.profile
    observation = manifest.end_observation
    return replace(
        manifest,
        record_id=identity,
        local_ref=identity[:LOCAL_REF_LENGTH],
        machine_id=ANONYMIZED,
        organization_id=ANONYMIZED,
        session_id=None,
        operator=ANONYMIZED,
        subject_id=None,
        software_version=UNVERSIONED,
        started_at=NEUTRAL_UTC,
        ended_at=NEUTRAL_UTC,
        clocks=replace(
            manifest.clocks, utc_start=NEUTRAL_UTC, wall_trusted=None, ntp_offset_s=None
        ),
        profile=None if profile is None else replace(profile, resolved_at=NEUTRAL_MILLIS),
        end_observation=(
            None
            if observation is None
            else replace(observation, stop_reason=None, shutdown_detail=None)
        ),
    )


def machine_detail(event: Event) -> Result[str, str]:  # noqa: PLR0911  # one return per closed form
    text = event.detail
    if event.kind in commands.INPUT_KINDS:
        parsed = commands.parse(text)
        return (
            Err("an input event is not a replay command")
            if isinstance(parsed, Err)
            else Ok(commands.encode(parsed.value))
        )
    head = text.partition(":")[0]
    if event.kind is EventKind.VERDICT:
        verdict = re.fullmatch(r"([a-z_]+) \(([A-Z_]+)\)", head)
        if verdict is not None and verdict[1] in _RULES and verdict[2] in _ACTIONS:
            return Ok(head)
        return Ok("cleared" if text == "cleared" else ANONYMIZED)
    if event.kind is EventKind.DRIVE_FAULT:
        fault = re.fullmatch(r"([A-Za-z0-9_]+) \(LFT ([0-9]+)\)", head)
        if fault is not None and fault[1] in _FAULTS and int(fault[2]) <= _MAX_REGISTER:
            return Ok(head)
        return Ok("cleared" if text == "cleared" else ANONYMIZED)
    if event.kind is EventKind.PHASE:
        return Ok(text if text in _PHASES else ANONYMIZED)
    if event.kind is EventKind.END:
        return Ok(text if text in _ENDS else ANONYMIZED)
    if event.kind is EventKind.WARNING and head in _WARNINGS:
        if text == "replay_incomplete: pending_ecg_delivery":
            return Ok(text)
        pattern = _COUNTERS.get(head)
        counters = (
            ()
            if pattern is None
            else tuple(word for word in text.partition(":")[2].split() if pattern.fullmatch(word))
        )
        return Ok(head + ":" + (" " + " ".join(counters) if counters else ""))
    return Ok(ANONYMIZED)


def anonymous_events(recording: Recording) -> Result[tuple[Event, ...], str]:
    events: list[Event] = []
    for event in recording.events:
        detail = machine_detail(event)
        if isinstance(detail, Err):
            return detail
        actor = event.actor if event.actor in {SYSTEM, REMOTE} else ANONYMIZED
        events.append(replace(event, detail=detail.value, actor=actor))
    return Ok(tuple(events))


def identities(recording: Recording) -> tuple[str, ...]:
    manifest = recording.manifest
    values = (
        manifest.record_id,
        manifest.machine_id,
        manifest.organization_id,
        manifest.session_id,
        manifest.local_ref,
        manifest.operator,
        manifest.subject_id,
        *(event.actor for event in recording.events),
    )
    return tuple(
        sorted({value for value in values if value and value not in {SYSTEM, REMOTE, ANONYMIZED}})
    )


def identifying_content(content: bytes, known: tuple[str, ...], *, text: bool = True) -> bool:
    lowered = content.lower()
    if any(value.lower().encode("utf-8") in lowered for value in known):
        return True
    neutral = content.replace(NEUTRAL_UTC.encode(), b"").replace(NEUTRAL_FOLDER.encode(), b"")
    neutral = neutral.replace(str(NEUTRAL_MILLIS).encode(), b"")
    if _DATES.search(neutral) is not None:
        return True
    return text and EMAIL.search(content.decode("utf-8", errors="replace")) is not None


def remaining_identity(folder: Path, known: tuple[str, ...]) -> bool:
    for path in folder.rglob("*"):
        if identifying_content(path.relative_to(folder).as_posix().encode(), known):
            return True
        if path.is_file():
            content = path.read_bytes()
            if path.name.endswith(".bin.gz"):
                if content[3:8] != bytes(5):
                    return True
                decoder = zlib.decompressobj(wbits=31)
                decoded = decoder.decompress(content)
                if decoder.unused_data:
                    return True
                header, _, samples = decoded.partition(b"\n")
                if identifying_content(samples, known, text=False):
                    return True
                content = header
            if identifying_content(content, known):
                return True
    return False


def identifying_frames(recording: Recording, known: tuple[str, ...]) -> bool:
    for frame in recording.frames:
        if frame.raw_hex is not None:
            try:
                content = bytes.fromhex(frame.raw_hex)
            except ValueError:
                return True
            if identifying_content(content, known, text=False):
                return True
    return False
