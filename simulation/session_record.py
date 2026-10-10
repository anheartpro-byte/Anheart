import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

from pydantic import TypeAdapter

from simulation.scenario import Scenario, SessionKind
from src.clock import Clock
from src.contract import read_software_version
from src.record.codec import document
from src.record.rows import JsonValue
from src.record.schema import Clocks, GeometrySnapshot, Manifest, Profile
from src.training.plan import INITIAL_REV, SHIPPED_DEFAULTS_PATH
from src.units import Monotonic

SCENARIO: Final[TypeAdapter[Scenario]] = TypeAdapter(Scenario)
GEOMETRY: Final[TypeAdapter[GeometrySnapshot]] = TypeAdapter(GeometrySnapshot)


def manifest_for(
    scenario: Scenario, clock: Clock, origin: Monotonic, geometry: Mapping[str, JsonValue]
) -> Manifest:
    stamp = (
        datetime.fromtimestamp(clock.unix_millis() / 1000, UTC).isoformat().replace("+00:00", "Z")
    )
    profile = scenario.profile
    frozen: Profile | None = None
    if profile is not None:
        frozen = Profile(
            total_duration_s=profile.total_duration_s,
            baseline_s=profile.baseline_s,
            warmup_max_s=profile.warmup_max_s,
            hold_min_s=profile.hold_min_s,
            cooldown_s=profile.cooldown_s,
            recovery_s=profile.recovery_s,
            zone_low_bpm=profile.zone_low_bpm,
            zone_high_bpm=profile.zone_high_bpm,
            hard_max_bpm=profile.hard_max_bpm,
            critical_bpm=profile.critical_bpm,
            subject_hr_max=profile.subject_hr_max,
            min_run_rpm=profile.min_run_rpm,
            max_rpm=profile.max_rpm,
            warmup_rpm_ceiling_fraction=profile.warmup_rpm_ceiling_fraction,
            channels=tuple(channel.value for channel in profile.channels),
            allow_above_nameplate=profile.allow_above_nameplate,
            source_rev=INITIAL_REV,
            resolved_at=clock.unix_millis(),
            total_overridden=False,
        )
    config = json.dumps(
        {"scenario": document(SCENARIO, scenario), "resolved_geometry_and_limits": geometry},
        sort_keys=True,
        allow_nan=False,
    )
    config_hash = hashlib.sha256(config.encode()).hexdigest()
    identity = uuid4().hex
    return Manifest(
        schema_version=2,
        record_id=identity,
        machine_id="simulation",
        organization_id="synthetic",
        session_id=None,
        local_ref=identity[:16],
        kind="auto" if scenario.kind is SessionKind.AUTO else "manual",
        occupancy="occupied"
        if scenario.kind is SessionKind.AUTO
        else scenario.manual.occupancy.value,
        operator="sim-operator",
        subject_id="synthetic-subject",
        profile=frozen,
        config_hash=config_hash,
        # The build the scenario ran: raspberry-pi/VERSION, as on the console.
        software_version=read_software_version(),
        contract_version="2",
        medical_parameters_version=hashlib.sha256(SHIPPED_DEFAULTS_PATH.read_bytes()).hexdigest(),
        clocks=Clocks(monotonic_start=origin, utc_start=stamp, ntp_offset_s=None),
        started_at=stamp,
        ended_at=None,
        end_reason=None,
        preflight=None,
        geometry=GEOMETRY.validate_json(
            json.dumps(
                {field.name: geometry[field.name] for field in fields(GeometrySnapshot)},
                allow_nan=False,
            )
        ),
    )
