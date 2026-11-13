"""A ballasted bench session launched from the dashboard (ANH-250, contract 1.9).

What the console does with it, layer by layer:

* the poll answer: a closed union read field by field. A bench launch has
  its own shape, nothing of a rider; an automatic one keeps the shape every
  console of this major reads; a mode this console does not know, or a bench
  launch with one field out of its shape, is ignored and never armed. The
  parser of a 1.8 console drops the bench shape;
* the offer and its confirmation (:class:`~src.cloud_sync.CloudSync`, a
  scripted dashboard): no passenger to look for, and the operator's
  attestation required, ``bench_unattested`` without it;
* the real simulated console: the confirmed launch is a manual bench session,
  under a declared STO bridge too, whose every refusal reaches the dashboard
  as a failed session; the dashboard's stop ends it on the ramp; its record
  carries the ballast and the attestation. A launch with a person on board
  stays refused under the bridge.
"""

from __future__ import annotations

import logging
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import (
    DASHBOARD_OPERATOR,
    MAX_LAUNCH_NOTES,
    AutoLaunch,
    BenchLaunch,
    _auto_launch_of,  # pyright: ignore[reportPrivateUsage]  # the parser of a 1.8 console
    _launch_of,  # pyright: ignore[reportPrivateUsage]  # the poll's one parser
    _safe_launch_refusal,  # pyright: ignore[reportPrivateUsage]  # what is said of a refusal
)
from src.control_surface import LOCAL_SUBJECT, StartManual
from src.local_panel import OUT_OF_SERVICE, DriveSide, build_panel
from src.motor.simulated import SimulatedDrive, SimulatedDriveConfig
from src.record.reader import read
from src.record.schema import Manifest, RecordedBallast
from src.record.session import BENCH_ATTESTED
from src.record.writer import MANIFEST
from src.remote_launch import (
    BENCH_LAUNCH_ATTESTATION,
    ConfirmationCode,
    ConfirmationRefusal,
    LaunchMode,
    LaunchStage,
    RemoteLaunchView,
)
from src.result import Err, Ok
from src.service_state import THRESHOLD
from src.training.plan import JsonValue
from src.training.preflight import BRIDGED_REMOTE_LAUNCH, StoBridgedRemoteLaunch
from src.training.runtime import RuntimeState
from src.training.types import MAX_BALLAST_KG, Ballast, Occupancy
from src.units import Kilograms, OutputRpm
from src.web.app import create_app
from src.web.schemas import RemoteLaunchRow
from tests.access_support import OPERATOR_ID, signed_http
from tests.medical_support import signed
from tests.record_support import manifest
from tests.test_cloud_sync import (
    LAUNCH,
    OPERATOR,
    POLL_PATH,
    START,
    STATUS,
    WEB_KEY,
    Dashboard,
    Linked,
    config_of,
    launch_answer,
    linked,
    rig,
    state_of,
    status_answer,
)
from tests.test_contract import shared
from tests.test_out_of_service_console import console as out_of_service_console

END: Final[str] = "/api/machine/training/end"

BENCH: Final[Mapping[str, JsonValue]] = {
    "sessionId": "remote-bench",
    "mode": "bench",
    "occupancy": "ballast",
    "ballastKg": 80,
    "notes": "sac de sable sangle",
    "operatorName": "Dr Manager",
}

LAUNCHED_BY: Final[str] = f"Dr Manager ({DASHBOARD_OPERATOR})"


def bench(**changes: JsonValue) -> dict[str, JsonValue]:
    return {**BENCH, **changes}


def without(key: str) -> dict[str, JsonValue]:
    return {name: value for name, value in BENCH.items() if name != key}


# =========================================================================
# The poll answer
# =========================================================================


def test_a_bench_launch_is_read_with_its_ballast_and_its_note() -> None:
    assert _launch_of({"session": dict(BENCH)}) == BenchLaunch(
        session_id="remote-bench",
        operator="Dr Manager",
        ballast=Ballast(mass_kg=Kilograms(80.0)),
        notes="sac de sable sangle",
    )


@pytest.mark.parametrize(
    ("given", "mass"),
    [(None, None), (0.5, 0.5), (80, 80.0), (MAX_BALLAST_KG, MAX_BALLAST_KG)],
)
def test_a_declared_mass_is_kept_and_a_mass_not_given_is_none(
    given: float | None, mass: float | None
) -> None:
    launch = _launch_of({"session": bench(ballastKg=given, notes=None)})
    assert isinstance(launch, BenchLaunch)
    assert launch.ballast == Ballast(mass_kg=None if mass is None else Kilograms(mass))
    assert launch.notes is None


def test_an_empty_operator_is_the_dashboard() -> None:
    launch = _launch_of({"session": bench(operatorName="")})
    assert isinstance(launch, BenchLaunch)
    assert launch.operator == DASHBOARD_OPERATOR


@pytest.mark.parametrize(
    "session",
    [
        without("sessionId"),
        bench(sessionId=3),
        without("operatorName"),
        bench(operatorName=None),
        without("occupancy"),
        bench(occupancy="occupied"),
        bench(occupancy="bench"),
        without("ballastKg") | {"ballastKg": "80"},
        bench(ballastKg=0),
        bench(ballastKg=-1),
        bench(ballastKg=MAX_BALLAST_KG + 0.1),
        bench(ballastKg=True),
        bench(ballastKg=math.nan),
        bench(ballastKg=math.inf),
        bench(ballastKg=[80]),
        bench(notes=3),
        bench(notes="n" * (MAX_LAUNCH_NOTES + 1)),
    ],
    ids=[
        "no-session",
        "session-not-text",
        "no-operator",
        "operator-null",
        "no-occupancy",
        "occupied",
        "occupancy-bench",
        "mass-text",
        "mass-zero",
        "mass-negative",
        "mass-over-bound",
        "mass-boolean",
        "mass-nan",
        "mass-infinite",
        "mass-list",
        "notes-not-text",
        "notes-too-long",
    ],
)
def test_a_bench_launch_out_of_its_shape_is_ignored(
    session: dict[str, JsonValue], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="src.cloud_sync"):
        assert _launch_of({"session": session}) is None
    assert "malformed bench launch ignored" in caplog.text


def test_a_note_at_its_bound_is_kept() -> None:
    launch = _launch_of({"session": bench(notes="n" * MAX_LAUNCH_NOTES)})
    assert isinstance(launch, BenchLaunch)


@pytest.mark.parametrize("mode", ["manual", "BENCH", None, 3, ""])
def test_a_mode_this_console_does_not_know_is_never_guessed_at(
    mode: JsonValue, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="src.cloud_sync"):
        assert _launch_of({"session": bench(mode=mode)}) is None
        assert _launch_of({"session": {**LAUNCH, "mode": mode}}) is None
    assert "launch of an unknown mode ignored" in caplog.text


def test_an_automatic_launch_is_read_with_its_mode_or_without_one() -> None:
    """A dashboard of 1.8 says no mode: its launches are automatic, as they always were."""
    without_mode = _launch_of({"session": dict(LAUNCH)})
    with_mode = _launch_of({"session": {**LAUNCH, "mode": "auto"}})
    assert isinstance(without_mode, AutoLaunch)
    assert with_mode == without_mode


def test_a_console_of_1_8_drops_the_bench_shape_unarmed() -> None:
    """What a 1.8 console reads is the automatic shape alone: a bench launch lacks the
    programme, the rider and the heart rate it requires, and is dropped as malformed."""
    assert not {"profileId", "subjectId", "subjectHrMax"} & set(BENCH)
    assert _auto_launch_of(dict(BENCH)) is None
    # And a bench payload that claims to be automatic is no automatic launch either.
    assert _launch_of({"session": bench(mode="auto")}) is None


def test_what_is_said_of_a_refused_bench_launch_is_left_as_it_is() -> None:
    launch = _launch_of({"session": dict(BENCH)})
    assert _safe_launch_refusal("console occupee", launch) == "console occupee"


# =========================================================================
# The offer and its confirmation, against a scripted dashboard
# =========================================================================


async def test_the_offer_shows_the_ballast_the_note_and_the_attestation(tmp_path: Path) -> None:
    r = rig(tmp_path, with_roster=False)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step()
    view = r.sync.current_remote_launch()
    assert view == RemoteLaunchView(
        session_id="remote-bench",
        mode=LaunchMode.BENCH,
        profile_id=None,
        program_name=None,
        launched_by="Dr Manager",
        passenger_name=None,
        passenger_reason=None,
        stage=LaunchStage.WAITING_CONFIRMATION,
        reason=None,
        ballast_kg=Kilograms(80.0),
        notes="sac de sable sangle",
        attestation=BENCH_LAUNCH_ATTESTATION,
    )
    assert view is not None
    row = RemoteLaunchRow.of(view)
    # Nobody on board, nobody to find in a list: confirmable without a passenger.
    assert (row.mode, row.program, row.passenger_name, row.can_confirm) == (
        "bench",
        None,
        None,
        True,
    )


async def test_without_the_attestation_the_confirmation_is_refused_and_nothing_is_sent(
    tmp_path: Path,
) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step()
    refused = r.sync.confirm_remote_launch("remote-bench", OPERATOR)
    assert refused == Err(
        ConfirmationRefusal(
            ConfirmationCode.BENCH_UNATTESTED,
            f"attestation requise : {BENCH_LAUNCH_ATTESTATION}",
        )
    )
    await r.step()
    assert r.dashboard.to(START) == []
    assert r.panel.surface.pending is None
    view = r.sync.current_remote_launch()
    assert view is not None
    assert view.stage is LaunchStage.WAITING_CONFIRMATION


async def test_attested_it_starts_a_manual_bench_session_that_carries_the_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    r = rig(tmp_path, with_roster=False)
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step()
    with caplog.at_level(logging.INFO, logger="src.cloud_sync"):
        assert isinstance(r.sync.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    assert "bench attestation was given at the console" in caplog.text
    await r.step()
    assert [body["sessionId"] for body in r.dashboard.to(START)] == ["remote-bench"]
    pending = r.panel.surface.pending
    assert isinstance(pending, StartManual)
    assert pending == replace(
        pending,
        occupancy=Occupancy.BENCH,
        operator=LAUNCHED_BY,
        subject_id=LOCAL_SUBJECT,
        cloud_session_id="remote-bench",
        confirmed_by=OPERATOR,
        ballast=Ballast(mass_kg=Kilograms(80.0)),
    )


async def test_an_automatic_launch_does_not_read_the_attestation(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
    await r.step()
    assert isinstance(r.sync.confirm_remote_launch("remote-1", OPERATOR, True), Ok)


async def test_a_bench_launch_is_taken_whatever_the_programmes(tmp_path: Path) -> None:
    """Programmes off, parameters signed: an automatic launch is not taken, a bench one is."""
    r = rig(tmp_path, programs=False, panel_programs=False, medical=signed())
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step()
    view = r.sync.current_remote_launch()
    assert view is not None
    assert view.mode is LaunchMode.BENCH


async def test_a_changed_bench_offer_needs_a_new_confirmation(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step()
    r.dashboard.answer(POLL_PATH, launch_answer(bench(ballastKg=90)))
    await r.step(3.0)
    view = r.sync.current_remote_launch()
    assert view is not None
    assert view.ballast_kg == Kilograms(90.0)
    assert view.reason == "l'offre a change, une nouvelle confirmation est requise"


@pytest.mark.parametrize("version", ["1.8", "2.9"])
async def test_a_bench_launch_from_a_dashboard_of_another_major_or_minor_is_judged_by_the_major(
    tmp_path: Path, version: str
) -> None:
    """A minor is never refused, a major always is: a bench launch under another major
    is a refusal owed, never an offer."""
    r = rig(tmp_path)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH), version=version))
    await r.step()
    offered = r.sync.current_remote_launch()
    assert (offered is not None) is (version == "1.8")


async def test_a_bench_launch_refused_before_its_start_is_a_failed_session_never_confirmed(
    tmp_path: Path,
) -> None:
    """A verdict standing at the console: the gates of a bench start refuse it before
    ``training/start``, and the dashboard is told why."""
    rig_ = linked(tmp_path)
    rig_.panel.surface.submit_estop(operator=OPERATOR, reason="test: a latched verdict")
    await rig_.run(6.0)  # the stop is over; its verdict stands until acknowledged
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(4.0)  # the next poll
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    await rig_.run(2.0)
    assert rig_.panel.runtime.manual is None
    assert rig_.dashboard.to(START) == []
    (end,) = rig_.dashboard.to(END)
    assert end["sessionId"] == "remote-bench"
    assert end["failed"] is True
    assert str(end["reason"]).startswith("refusee par la machine : ")
    await rig_.panel.close()


async def test_a_bench_launch_refused_at_its_arming_after_the_2xx_is_a_failed_session(
    tmp_path: Path,
) -> None:
    """Every refusal of :meth:`LocalPanel._start_manual` for a launch reaches the dashboard:
    here the runtime's, a stop latched between the dashboard's 2xx and the arming."""
    rig_ = linked(tmp_path)
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(0.2)
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    await rig_.run(0.2)
    assert len(rig_.dashboard.to(START)) == 1
    assert isinstance(rig_.panel.surface.pending, StartManual)
    rig_.panel.surface.submit_estop(operator=OPERATOR, reason="test: stopped before arming")
    await rig_.run(2.0)
    assert rig_.panel.runtime.manual is None
    (end,) = rig_.dashboard.to(END)
    assert end["sessionId"] == "remote-bench"
    assert end["failed"] is True
    assert str(end["reason"]).startswith("refusee par la machine : ")
    await rig_.panel.close()


# =========================================================================
# The real simulated console: launched, confirmed, driven, stopped, recorded
# =========================================================================


async def confirm_over_http(rig_: Linked, operator: str, *, attested: bool) -> httpx.Response:
    web = replace(config_of().web, token=WEB_KEY)
    app = create_app(services=rig_.panel.services, config=web)
    async with signed_http(app, rig_.panel.services) as session:
        headers = {"X-Anheart-Token": WEB_KEY}
        offered = await session.get("/api/remote-launch", headers=headers)
        assert offered.status_code == HTTPStatus.OK
        assert offered.json()["mode"] == "bench"
        assert offered.json()["ballast_kg"] == 80
        assert offered.json()["attestation"] == BENCH_LAUNCH_ATTESTATION
        return await session.post(
            "/api/remote-launch/confirm",
            headers=headers,
            json={"session_id": "remote-bench", "operator": operator, "bench_attested": attested},
        )


async def test_the_site_launches_a_ballasted_bench_session_the_console_confirms_and_stops(
    tmp_path: Path,
) -> None:
    confirmer = "Console Operator"
    rig_ = linked(tmp_path, record_root=tmp_path / "records")
    rig_.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await rig_.run(0.2)

    unattested = await confirm_over_http(rig_, confirmer, attested=False)
    assert unattested.status_code == HTTPStatus.CONFLICT
    assert unattested.json()["code"] == ConfirmationCode.BENCH_UNATTESTED.value
    await rig_.run(1.0)
    assert rig_.dashboard.to(START) == []

    attested = await confirm_over_http(rig_, confirmer, attested=True)
    assert attested.status_code == HTTPStatus.ACCEPTED
    await rig_.run(1.8)
    assert state_of(rig_.panel) is RuntimeState.RUNNING
    manual = rig_.panel.runtime.manual
    assert manual is not None
    assert manual.occupancy is Occupancy.BENCH
    assert [body["sessionId"] for body in rig_.dashboard.to(START)] == ["remote-bench"]
    # Launched from the dashboard: known there already, never declared as a local session.
    assert rig_.dashboard.to("/api/machine/training/local") == []
    cloud = rig_.panel.cloud
    assert cloud is not None
    assert cloud.current_session_id == "remote-bench"

    # The speed is set at the console, at the motion limits.
    target = rig_.panel.surface.submit_manual_target(output_rpm=OutputRpm(3.0), operator=confirmer)
    assert isinstance(target, Ok)
    await rig_.run(10.0)
    assert rig_.panel.runtime.snapshot().setpoint.motor_rpm > 0

    rig_.dashboard.answer(STATUS, status_answer(stop=True))
    await rig_.run(3.0)
    assert state_of(rig_.panel) in (RuntimeState.ENDING, RuntimeState.FINISHED)
    assert rig_.panel.runtime.stop_reason == "arret demande depuis le tableau de bord"
    await rig_.panel.close()

    recorder = rig_.panel.recorder
    assert recorder is not None
    recorder.journal.drain()
    (events,) = (
        path
        for path in recorder.journal.root.rglob("events.jsonl")
        if path.parent.name != "logbook"
    )
    loaded = read(events.parent)
    assert isinstance(loaded, Ok)
    record = loaded.value
    assert record.manifest.ballast == RecordedBallast(mass_kg=80.0)
    assert (record.manifest.kind, record.manifest.occupancy) == ("manual", "bench")
    assert record.manifest.session_id is not None
    attestations = [event for event in record.events if event.detail == BENCH_ATTESTED]
    assert len(attestations) == 1
    assert attestations[0].actor == OPERATOR_ID, "the account signed in (ANH-152)"
    assert BENCH_LAUNCH_ATTESTATION in BENCH_ATTESTED


async def test_under_a_declared_bridge_a_bench_launch_runs_and_a_programme_stays_refused(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    dashboard = Dashboard()
    config = config_of(
        signed(),
        MOTOR_BACKEND="serial",
        MOTOR_PORT="/dev/ttyUSB-test",
        STO_SOURCE="bridged_declared",
    )
    simulator = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
        drive=DriveSide(backend=simulator, simulator=simulator, release=lambda: None),
    )
    rig_ = Linked(clock=clock, panel=panel, dashboard=dashboard)
    surface = panel.surface
    try:
        assert isinstance(surface.attest_estop_wiring(OPERATOR), Ok)
        # A person on board stays refused under the bridge, from the site as at the console.
        assert surface.check_remote_launch() == Err(StoBridgedRemoteLaunch())
        assert surface.check_remote_launch(Occupancy.OCCUPIED) == Err(StoBridgedRemoteLaunch())
        assert surface.check_remote_launch(Occupancy.BENCH) == Ok(None)

        dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
        await rig_.run(0.2)
        cloud = panel.cloud
        assert cloud is not None
        assert isinstance(cloud.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
        await rig_.run(1.8)
        assert state_of(panel) is RuntimeState.RUNNING
        dashboard.answer(STATUS, status_answer(stop=True))
        await rig_.run(3.0)
        assert state_of(panel) in (RuntimeState.ENDING, RuntimeState.FINISHED)
    finally:
        await panel.close()


async def test_under_a_declared_bridge_a_programme_from_the_site_is_still_refused(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    dashboard = Dashboard()
    config = config_of(
        signed(),
        MOTOR_BACKEND="serial",
        MOTOR_PORT="/dev/ttyUSB-test",
        STO_SOURCE="bridged_declared",
    )
    simulator = SimulatedDrive(clock, SimulatedDriveConfig(reads_reset_watchdog=True))
    panel = build_panel(
        config,
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _c: dashboard,
        drive=DriveSide(backend=simulator, simulator=simulator, release=lambda: None),
    )
    rig_ = Linked(clock=clock, panel=panel, dashboard=dashboard)
    try:
        assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
        dashboard.answer(POLL_PATH, launch_answer(dict(LAUNCH)))
        await rig_.run(0.2)
        cloud = panel.cloud
        assert cloud is not None
        assert isinstance(cloud.confirm_remote_launch("remote-1", OPERATOR, True), Ok)
        await rig_.run(2.8)
        assert state_of(panel) is RuntimeState.IDLE
        assert dashboard.to(START) == []
        (end,) = dashboard.to(END)
        assert end["reason"] == f"refusee par la machine : {BRIDGED_REMOTE_LAUNCH}"
    finally:
        await panel.close()


# =========================================================================
# The record: the ballast is kept, and an old record stays readable
# =========================================================================


def test_a_record_written_before_the_ballast_is_read_without_one() -> None:
    old = manifest()
    written = MANIFEST.dump_json(old)
    # A session that declares none writes nothing of it: the record is as it always was.
    assert b"ballast" not in written
    assert MANIFEST.validate_json(written).ballast is None
    assert MANIFEST.validate_json(written[:-1] + b',"ballast":null}').ballast is None


@pytest.mark.parametrize("mass", [80.0, None])
def test_a_ballast_is_written_and_read_back(mass: float | None) -> None:
    with_ballast: Manifest = replace(manifest(), ballast=RecordedBallast(mass_kg=mass))
    read_back = MANIFEST.validate_json(MANIFEST.dump_json(with_ballast))
    assert read_back.ballast == RecordedBallast(mass_kg=mass)


@pytest.mark.parametrize("mass", [0, -1, math.nextafter(MAX_BALLAST_KG, math.inf)])
def test_a_record_s_ballast_out_of_its_bounds_is_refused(mass: float) -> None:
    written = MANIFEST.dump_json(replace(manifest(), ballast=RecordedBallast(mass_kg=80.0)))
    altered = written.replace(b'"mass_kg":80.0', f'"mass_kg":{mass}'.encode())
    with pytest.raises(ValueError, match="mass_kg"):
        MANIFEST.validate_json(altered)


def test_a_record_s_ballast_at_its_bound_is_kept() -> None:
    at_bound = replace(manifest(), ballast=RecordedBallast(mass_kg=MAX_BALLAST_KG))
    read_back = MANIFEST.validate_json(MANIFEST.dump_json(at_bound))
    assert read_back.ballast == RecordedBallast(mass_kg=MAX_BALLAST_KG)


def test_the_launch_never_reaches_a_record_but_as_its_ballast() -> None:
    """The launcher's note is shown at the console and kept nowhere else."""
    fields = cast("Mapping[str, object]", RecordedBallast.__dataclass_fields__)
    assert set(fields) == {"mass_kg"}


# =========================================================================
# The gates nobody reaches but by a bug: a launch of no known mode
# =========================================================================


@dataclass(frozen=True, slots=True)
class _Unknown:
    """Something offered as a launch that is neither mode: only a bug could make one."""

    session_id: str = "remote-unknown"
    operator: str = "nobody"


async def test_a_launch_of_no_known_mode_hits_every_guard(tmp_path: Path) -> None:
    r = rig(tmp_path)
    bogus = cast("AutoLaunch", _Unknown())
    r.sync._offered_launch = bogus  # pyright: ignore[reportPrivateUsage]  # only a bug sets it
    with pytest.raises(AssertionError):
        r.sync.current_remote_launch()
    with pytest.raises(AssertionError):
        r.sync._confirmation_refusal(bogus, True)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AssertionError):
        r.sync._submitted(bogus, OPERATOR)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AssertionError):
        r.panel.preflight_remote_start(bogus)
    await r.panel.close()


# =========================================================================
# A console out of service arms no bench launch either
# =========================================================================


async def test_out_of_service_a_bench_launch_is_refused_before_it_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig_ = out_of_service_console(tmp_path, deaths=THRESHOLD, monkeypatch=monkeypatch)
    launch = _launch_of({"session": dict(BENCH)})
    assert isinstance(launch, BenchLaunch)
    assert rig_.panel.preflight_remote_start(launch) == Err(f"demarrage refuse : {OUT_OF_SERVICE}")
    await rig_.panel.close()


# =========================================================================
# Under a real STO source, a bench launch arms only on a READY chain
# =========================================================================


@pytest.mark.parametrize("chain", ["open", "ready"])
async def test_under_a_real_sto_source_a_bench_launch_is_refused_unless_the_chain_is_ready(
    tmp_path: Path, chain: str
) -> None:
    """The bridge exception never reaches a real source: an open chain refuses the bench
    launch before ``training/start``, and the dashboard is told so as a failed start."""
    r = rig(tmp_path, sto_source="sim_ready")
    assert isinstance(r.panel.surface.attest_estop_wiring(OPERATOR), Ok)
    r.dashboard.answer(POLL_PATH, launch_answer(dict(BENCH)))
    await r.step(0.2)
    assert isinstance(r.sync.confirm_remote_launch("remote-bench", OPERATOR, True), Ok)
    simulator = r.panel.drive.simulator
    assert simulator is not None
    assert simulator.sto_chain is not None
    if chain == "open":
        simulator.sto_chain.open(r.clock.monotonic())
    await r.step()
    if chain == "ready":
        assert [body["sessionId"] for body in r.dashboard.to(START)] == ["remote-bench"]
        assert isinstance(r.panel.surface.pending, StartManual)
    else:
        assert r.dashboard.to(START) == []
        assert r.panel.surface.pending is None
        (end,) = r.dashboard.to(END)
        assert end["sessionId"] == "remote-bench"
        assert end["failed"] is True
        assert end["reason"] == (
            "refusee par la machine : chaine STO ouverte : retablir la chaine de securite"
        )
    await r.panel.close()


# =========================================================================
# The console's words and bounds are the shared contract's
# =========================================================================


def _unaccented(text: str) -> str:
    """``text`` as the console writes it: the same letters, without their accents."""
    return "".join(
        letter
        for letter in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(letter)
    )


def test_the_attestation_and_the_bounds_are_the_shared_contract_s() -> None:
    bench_launch = shared()["bench_launch"]
    assert isinstance(bench_launch, dict)
    section = cast("Mapping[str, object]", bench_launch)
    attestation = section["attestation"]
    assert isinstance(attestation, str)
    assert _unaccented(attestation) == BENCH_LAUNCH_ATTESTATION
    assert BENCH_LAUNCH_ATTESTATION.isascii()
    assert section["max_ballast_kg"] == MAX_BALLAST_KG
    assert section["max_notes_chars"] == MAX_LAUNCH_NOTES
    assert section["since"] == "1.9"
