"""ANH-115 EX-4: the commissioning card's routes and the console that offers it.

``GET /api/commissioning`` and ``POST /api/commissioning/run``: hidden (``null``) and refused
on a console linked to no dashboard, the run by an operator signed in and named, every
refusal a conflict in the card's words; and the console that builds the desk only when it
is linked and judges a pre-flight, its dashboard link bound once built.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import httpx

from src.clock import ManualClock
from src.commissioning import NOT_LINKED as NOT_LINKED_YET
from src.commissioning import CommissioningDesk
from src.commissioning_view import (
    CommissioningScreen,
    CommissioningView,
    Finding,
    LastRun,
    NotRecorded,
    Recorded,
    Verdict,
)
from src.local_panel import build_panel
from src.result import Err, Ok, Result
from src.training.preflight import ControlResult, ControlState
from src.units import UnixMillis
from src.web.app import create_app
from src.web.commissioning_routes import NOT_LINKED
from src.web.deps import WebConfig
from tests.access_support import OPERATOR_ID
from tests.test_local_panel import BASE_ENV, config_from
from tests.test_pairing import KEY, ORIGIN
from tests.test_web_access import TOKEN, console, device, signed

LINKED_ENV: Final[Mapping[str, str]] = {**BASE_ENV, "MACHINE_API_KEY": KEY, "CONVEX_URL": ORIGIN}
CONTROLS: Final[tuple[ControlResult, ...]] = (
    ControlResult("sto_source", "Source de la chaine STO", ControlState.BLOCK, "STO ponte"),
)
RECORDED: Final[LastRun] = LastRun(
    at=UnixMillis(1_800_000_000_000),
    operator=OPERATOR_ID,
    controls=CONTROLS,
    outcome=Recorded(
        "record-1",
        Verdict.NONCONFORMING,
        (Finding("sto_bridged", "drive_input", "bridged_declared"),),
        False,
    ),
)


class Desk:
    """A commissioning desk that answers what the test says, and notes who ran it."""

    def __init__(self, view: CommissioningView, run: Result[LastRun, str]) -> None:
        self.shown = view
        self.answer = run
        self.operators: list[str] = []

    def view(self) -> CommissioningView:
        return self.shown

    async def run(self, operator: str) -> Result[LastRun, str]:
        self.operators.append(operator)
        return self.answer


def client_of(desk: CommissioningScreen | None, tmp_path: Path) -> httpx.AsyncClient:
    c = console(tmp_path)
    services = replace(c.services, commissioning=desk)
    app = create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def test_a_console_linked_to_no_dashboard_hides_the_card_and_runs_none(
    tmp_path: Path,
) -> None:
    async with client_of(None, tmp_path) as client:
        assert (await signed(client)).status_code == httpx.codes.OK
        read = await client.get("/api/commissioning", headers=device())
        sent = await client.post("/api/commissioning/run", headers=device())
    assert (read.status_code, read.json()) == (httpx.codes.OK, None)
    assert (sent.status_code, sent.json()) == (httpx.codes.CONFLICT, {"detail": NOT_LINKED})


async def test_the_card_reads_what_the_desk_shows(tmp_path: Path) -> None:
    desk = Desk(
        CommissioningView(running=False, refusal="console pas au repos", last=None), Ok(RECORDED)
    )
    async with client_of(desk, tmp_path) as client:
        empty = await client.get("/api/commissioning", headers=device())
        desk.shown = CommissioningView(running=True, refusal=None, last=RECORDED)
        recorded = await client.get("/api/commissioning", headers=device())
        desk.shown = CommissioningView(
            running=False,
            refusal=None,
            last=replace(RECORDED, outcome=NotRecorded("tableau de bord injoignable")),
        )
        refused = await client.get("/api/commissioning", headers=device())
    assert empty.json() == {"running": False, "refusal": "console pas au repos", "last": None}
    assert recorded.json() == {
        "running": True,
        "refusal": None,
        "last": {
            "at": 1_800_000_000_000,
            "operator": OPERATOR_ID,
            "controls": [
                {
                    "id": "sto_source",
                    "label": "Source de la chaine STO",
                    "state": "block",
                    "detail": "STO ponte",
                }
            ],
            "recorded": True,
            "record_id": "record-1",
            "verdict": "nonconforming",
            "findings": [
                {"id": "sto_bridged", "expected": "drive_input", "read": "bridged_declared"}
            ],
            "signed": False,
            "reason": None,
        },
    }
    last = cast("dict[str, object]", refused.json()["last"])
    assert (
        last["recorded"],
        last["verdict"],
        last["findings"],
        last["signed"],
        last["reason"],
    ) == (
        False,
        None,
        [],
        None,
        "tableau de bord injoignable",
    )


async def test_a_run_takes_an_operator_signed_in_who_is_named(tmp_path: Path) -> None:
    desk = Desk(CommissioningView(running=False, refusal=None, last=RECORDED), Ok(RECORDED))
    async with client_of(desk, tmp_path) as client:
        nobody = await client.post("/api/commissioning/run", headers=device())
        assert (await signed(client)).status_code == httpx.codes.OK
        ran = await client.post("/api/commissioning/run", headers=device())
        desk.answer = Err("console pas au repos : variateur en marche")
        refused = await client.post("/api/commissioning/run", headers=device())
    assert nobody.status_code == httpx.codes.UNAUTHORIZED
    assert ran.status_code == httpx.codes.OK
    assert cast("dict[str, object]", ran.json())["last"] is not None
    assert (refused.status_code, refused.json()) == (
        httpx.codes.CONFLICT,
        {"detail": "console pas au repos : variateur en marche"},
    )
    assert desk.operators == [OPERATOR_ID, OPERATOR_ID]


def test_a_linked_console_with_a_pre_flight_offers_one_bound_to_its_dashboard(
    tmp_path: Path,
) -> None:
    linked = build_panel(
        config_from(LINKED_ENV),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
        preflight=True,
    )
    desk = linked.services.commissioning
    assert isinstance(desk, CommissioningDesk)
    # Bound, but the dashboard has not announced 1.18 yet: no heartbeat has been answered.
    assert desk.view() == CommissioningView(running=False, refusal=NOT_LINKED_YET, last=None)


def test_a_console_without_a_dashboard_or_a_pre_flight_offers_none(tmp_path: Path) -> None:
    for env, preflight in ((BASE_ENV, True), (LINKED_ENV, False)):
        panel = build_panel(
            config_from(env),
            clock=ManualClock(),
            profiles_path=tmp_path / "p.json",
            preflight=preflight,
        )
        assert panel.services.commissioning is None
