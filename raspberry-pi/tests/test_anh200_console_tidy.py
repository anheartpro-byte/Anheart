"""What the tidy-up of the console's code (ANH-200) must keep true.

What changed shape is pinned here; everything else is held by the existing
suite:

* a refused ``Origin`` header is quoted in the log on one bounded line;
* an HTTP handler answers with success for an ``Ok`` alone;
* a protocol member is abstract: a class that inherits a protocol and leaves a
  member out cannot be built, instead of answering ``None`` in its place;
* a ``match`` whose arms return ends with ``raise assert_never(subject)``
  after it, and a forgotten variant is still refused by the type checker;
* the types that moved to break import cycles are the same classes under the
  names they had, and each module of those cycles still imports first.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Final, cast, override

import httpx
import pytest
from fastapi import FastAPI

import src.local_config
import src.panel_status
from src.clock import Clock, ManualClock
from src.control_surface import Command, ControlSurface
from src.result import Ok, Result
from src.telemetry import TelemetryHub
from src.training.plan import ProfileStore
from src.training.safety import SafetySupervisor
from src.training.types import Occupancy, OccupancyRefused
from src.units import Monotonic, MotorRpm
from src.web import routes
from src.web.app import create_app
from src.web.deps import Services, WebConfig
from src.web.profile_writer import ProfileWriter
from src.web.schemas import CommandRow
from src.web.ws import LOGGED_HEADER_LIMIT, logged_header
from tests.test_web_api import (
    GEOMETRY,
    OPERATOR,
    PROFILE_ID,
    TOKEN,
    StubPorts,
    auth,
    build_limits,
    build_rig,
    probe_socket,
    profile_document,
)

WS_LOGGER = "src.web.ws"

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

CHECK_TIMEOUT_S: Final[int] = 300

# =========================================================================
# The refused origin, in the log
# =========================================================================


def test_a_short_header_is_logged_quoted_as_it_was_sent() -> None:
    assert logged_header("http://tablet.local:8080") == "'http://tablet.local:8080'"


def test_an_absent_header_reads_none() -> None:
    assert logged_header(None) == "None"


@pytest.mark.parametrize("line_break", ["\r\n", "\n", "\r"])
def test_a_logged_header_stays_on_one_line(line_break: str) -> None:
    logged = logged_header(f"http://a.example{line_break}second line")
    assert logged == "'http://a.example second line'"


def test_a_logged_header_is_cut_at_the_limit_and_says_so() -> None:
    sent = "http://" + "a" * 5000
    logged = logged_header(sent)
    assert logged.startswith(repr(sent)[:LOGGED_HEADER_LIMIT])
    assert logged.endswith(f"({len(sent)} characters received)")
    assert len(logged) < LOGGED_HEADER_LIMIT + 40


def test_a_header_whose_written_form_is_exactly_at_the_limit_is_not_cut() -> None:
    sent = "h" * (LOGGED_HEADER_LIMIT - 2)
    assert logged_header(sent) == repr(sent)
    assert len(logged_header(sent)) == LOGGED_HEADER_LIMIT


@pytest.mark.parametrize("character", ["\x00", "\x1b", "\u200b", "\U000e0001"])
def test_the_bound_is_on_what_is_written_escapes_included(character: str) -> None:
    """A character the log writes as an escape counts for what it takes on the line."""
    logged = logged_header(character * 5000)
    assert len(logged) < LOGGED_HEADER_LIMIT + 40
    assert logged.isascii()
    assert logged.isprintable()


async def test_a_refused_origin_is_quoted_on_one_bounded_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The handshake is refused as before; what the log keeps of the header is bounded."""
    rig = build_rig(tmp_path)
    origin = "http://a.example\r\nsecond line " + "x" * 5000
    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        sent = await probe_socket(rig.app, origin=origin, token=TOKEN)
    assert [message["type"] for message in sent] == ["websocket.close"]
    lines = [record.getMessage() for record in caplog.records if record.name == WS_LOGGER]
    assert len(lines) == 1
    (line,) = lines
    assert "\n" not in line
    assert "\r" not in line
    assert "http://a.example second line" in line
    assert len(line) < LOGGED_HEADER_LIMIT + 120


async def test_an_ordinary_refused_origin_is_logged_as_it_always_was(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = build_rig(tmp_path)
    with caplog.at_level(logging.WARNING, logger=WS_LOGGER):
        _ = await probe_socket(rig.app, origin="http://clinic-wifi-printer.local", token=TOKEN)
    lines = [record.getMessage() for record in caplog.records if record.name == WS_LOGGER]
    assert lines == ["refused telemetry socket from origin 'http://clinic-wifi-printer.local'"]


# =========================================================================
# A handler answers with success only for an Ok
# =========================================================================

NOT_A_RESULT: Final[str] = "neither an Ok nor an Err"


def _not_a_result(*_args: object, **_kwargs: object) -> str:
    return NOT_A_RESULT


async def _not_a_result_awaited(*_args: object, **_kwargs: object) -> str:
    return NOT_A_RESULT


@dataclass(frozen=True, slots=True)
class _Handler:
    """One handler, the call whose answer it unwraps, and a request that reaches that call."""

    name: str
    owner: object
    attribute: str
    awaited: bool
    method: str
    path: str
    body: Mapping[str, object] | None


HANDLERS: Final[tuple[_Handler, ...]] = (
    _Handler(
        "upsert_profile",
        routes,
        "parse_profile",
        False,
        "PUT",
        "/api/profiles/bench_short?rev=0",
        profile_document("bench_short"),
    ),
    _Handler(
        "_commit_profile",
        ProfileWriter,
        "upsert",
        True,
        "PUT",
        "/api/profiles/bench_short?rev=0",
        profile_document("bench_short"),
    ),
    _Handler(
        "delete_profile",
        ProfileWriter,
        "delete",
        True,
        "DELETE",
        f"/api/profiles/{PROFILE_ID}?rev=0",
        None,
    ),
    _Handler(
        "preview_plan",
        ProfileStore,
        "resolve",
        False,
        "POST",
        "/api/plan/preview",
        {"profile_id": PROFILE_ID},
    ),
    _Handler(
        "start_session",
        ControlSurface,
        "submit_start",
        False,
        "POST",
        "/api/session/start",
        {"profile_id": PROFILE_ID, "operator": OPERATOR},
    ),
    _Handler(
        "stop_session",
        ControlSurface,
        "submit_end",
        False,
        "POST",
        "/api/session/stop",
        {"operator": OPERATOR},
    ),
    _Handler(
        "start_manual",
        ControlSurface,
        "submit_start_manual",
        False,
        "POST",
        "/api/manual/start",
        {"occupancy": "bench", "operator": OPERATOR},
    ),
    _Handler(
        "manual_target",
        ControlSurface,
        "submit_manual_target",
        False,
        "POST",
        "/api/manual/target",
        {"output_rpm": 5.0, "operator": OPERATOR},
    ),
    _Handler(
        "fault_reset",
        ControlSurface,
        "submit_fault_reset",
        False,
        "POST",
        "/api/drive/fault-reset",
        {"operator": OPERATOR},
    ),
    _Handler(
        "attest",
        ControlSurface,
        "attest_estop_wiring",
        False,
        "POST",
        "/api/safety/attest",
        {"operator": OPERATOR, "sto_jumper_removed": True, "mushroom_wired_nc": True},
    ),
    _Handler(
        "acknowledge",
        ControlSurface,
        "acknowledge",
        False,
        "POST",
        "/api/safety/acknowledge",
        {"operator": OPERATOR},
    ),
)


def _app_with_manual_sessions(tmp_path: Path) -> FastAPI:
    """The interface of ``build_rig``, plus a ceiling for each occupancy so manual routes open."""
    clock = ManualClock()
    supervisor = SafetySupervisor(clock=clock, limits=build_limits())
    hub = TelemetryHub(clock=clock)
    store = ProfileStore(tmp_path / "profiles.json")
    assert isinstance(store.load(), Ok)

    def ceiling(_occupancy: Occupancy) -> Result[MotorRpm, OccupancyRefused]:
        return Ok(MotorRpm(1380))

    services = Services(
        clock=clock,
        surface=ControlSurface(clock=clock, supervisor=supervisor, sink=hub),
        hub=hub,
        supervisor=supervisor,
        store=store,
        ports=StubPorts(),
        geometry=GEOMETRY,
        ceilings=ceiling,
    )
    return create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))


@pytest.mark.parametrize("handler", HANDLERS, ids=[handler.name for handler in HANDLERS])
async def test_a_handler_given_something_that_is_not_a_result_answers_500(
    handler: _Handler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success is answered for an ``Ok`` alone: anything else is a refusal or a server error.

    No caller can produce this (the types forbid it), so it is forced: the call
    whose answer the handler unwraps is replaced by one that returns a string.
    """
    app = _app_with_manual_sessions(tmp_path)
    monkeypatch.setattr(
        handler.owner,
        handler.attribute,
        _not_a_result_awaited if handler.awaited else _not_a_result,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.request(
            handler.method, handler.path, headers=auth(), json=handler.body
        )
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR, response.text


# =========================================================================
# Protocol members are abstract
# =========================================================================


class _HalfAClock(Clock):
    """Inherits the protocol and leaves ``unix_millis`` out."""

    @override
    def monotonic(self) -> Monotonic:
        return Monotonic(0.0)


def test_a_class_that_leaves_a_protocol_member_out_cannot_be_built() -> None:
    with pytest.raises(TypeError, match="unix_millis"):
        _ = _HalfAClock()  # type: ignore[abstract]  # pyright: ignore[reportAbstractUsage]  # the point of the test


def test_a_class_that_defines_every_member_still_satisfies_the_protocol() -> None:
    assert isinstance(ManualClock(), Clock)


# =========================================================================
# The exhaustiveness guard, in the form that raises after the match
# =========================================================================


def test_a_command_that_is_not_one_fails_loudly_in_its_row() -> None:
    with pytest.raises(AssertionError):
        _ = CommandRow.of(cast("Command", "bogus"))


INCOMPLETE_MATCH: Final[str] = '''"""Deliberately wrong: ``words_for`` forgets ``BadCrc``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never


@dataclass(frozen=True, slots=True)
class CommTimeout:
    after_s: float


@dataclass(frozen=True, slots=True)
class BadCrc:
    expected: int


FakeDriveError = CommTimeout | BadCrc


def words_for(error: FakeDriveError) -> str:
    match error:
        case CommTimeout():
            return "timeout"
    raise assert_never(error)
'''


@pytest.mark.slow
def test_a_forgotten_variant_is_refused_when_the_guard_follows_the_match(tmp_path: Path) -> None:
    """The form of contract rule 10 keeps the guarantee of rule 3, and names the variant."""
    target = tmp_path / "incomplete_match_after.py"
    target.write_text(INCOMPLETE_MATCH, encoding="utf-8")
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        [sys.executable, "-m", "mypy", str(target)],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
        timeout=CHECK_TIMEOUT_S,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"mypy accepted a match that forgets a variant.\n{combined}"
    assert "BadCrc" in combined, combined
    assert "exhaustive-match" in combined, combined
    assert "assert_never" in combined, combined


# =========================================================================
# Types that moved keep their names, and no module needs another loaded first
# =========================================================================


def test_the_link_kinds_are_the_same_classes_under_the_configuration_names() -> None:
    assert src.local_config.MotorBackend is src.panel_status.MotorBackend
    assert src.local_config.EcgSource is src.panel_status.EcgSource
    assert src.local_config.MotorBackend("sim") is src.panel_status.MotorBackend.SIM


@pytest.mark.parametrize(
    "module",
    [
        "src.motor.backend",
        "src.motor.drive",
        "src.local_config",
        "src.web.deps",
        "src.panel_status",
        "src.panel_lifecycle",
        "src.local_panel",
    ],
)
def test_each_module_of_the_former_cycles_imports_first(module: str) -> None:
    """In a fresh interpreter, so that no other test's imports can have prepared the ground."""
    result = subprocess.run(  # noqa: S603  # fixed argv, no shell
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
        timeout=CHECK_TIMEOUT_S,
    )
    assert result.returncode == 0, result.stderr
