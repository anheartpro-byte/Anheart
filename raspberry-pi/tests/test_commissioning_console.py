"""ANH-115 EX-4: the commissioning card's routes and the console that offers it.

``GET /api/commissioning`` and ``POST /api/commissioning/run``: hidden (``null``) and refused
on a console linked to no dashboard, the run by an operator signed in and named, every
refusal a conflict in the card's words; and the console that builds the desk only when it
is linked and judges a pre-flight, its dashboard link bound once built.
"""

from __future__ import annotations

import asyncio
from abc import abstractmethod
from collections.abc import Awaitable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, Protocol, cast

import httpx
import pytest

from src.clock import ManualClock
from src.cloud_sync import CloudError, Document
from src.commissioning import (
    COMMISSIONING_PATH,
    NOT_AT_REST,
    RUN_WAIT,
    UNATTESTED,
    CommissioningDesk,
    DashboardLink,
    PreflightRuns,
    dropped,
)
from src.commissioning import NOT_LINKED as NOT_LINKED_YET
from src.commissioning_view import (
    CommissioningScreen,
    CommissioningView,
    Finding,
    LastRun,
    NotRecorded,
    Recorded,
    Verdict,
)
from src.contract import ContractVersion
from src.local_panel import CONTROL_PERIOD, build_panel
from src.motor.simulated import SimulatedDrive
from src.result import Err, Ok, Result
from src.training.plan import JsonValue
from src.training.preflight import ControlResult, ControlState, PreflightInputs
from src.training.types import Occupancy, RunMode
from src.units import Monotonic, Seconds, UnixMillis
from src.web.app import create_app
from src.web.commissioning_routes import NOT_LINKED
from src.web.deps import WebConfig
from tests.access_support import OPERATOR_ID
from tests.test_cloud_sync import Dashboard, config_of
from tests.test_local_panel import BASE_ENV, OPERATOR, config_from
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


async def test_the_panel_runs_one_over_its_runtime_its_sto_and_its_link(tmp_path: Path) -> None:
    """The four parts build_panel gives the desk: the runtime's attestation, the STO, the
    link's transport and what its last heartbeat announced."""
    dashboard = Dashboard()
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_800_000_000_000))
    panel = build_panel(
        config_of(),
        clock=clock,
        profiles_path=tmp_path / "p.json",
        transport=lambda _config: dashboard,
        preflight=True,
    )
    desk = panel.services.commissioning
    assert isinstance(desk, CommissioningDesk)
    assert desk.view().refusal == NOT_LINKED_YET
    clock.advance(Seconds(1.0))
    await panel.cloud_step()
    assert desk.view().refusal == UNATTESTED
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR_ID), Ok)
    await panel.control_cycle()
    assert desk.view().refusal is None
    run = asyncio.create_task(desk.run(OPERATOR_ID))
    await asyncio.sleep(0)
    # The pre-flight's run is waited for no longer than its wait, the console ticking all along
    # (a drive left without its keepalive would fault); then the drive is read again.
    for _ in range(round(RUN_WAIT / CONTROL_PERIOD)):
        clock.advance(CONTROL_PERIOD)
        await panel.control_cycle()
    done = await run
    assert isinstance(done, Ok)
    (body,) = dashboard.to(COMMISSIONING_PATH)
    attestation = cast("Mapping[str, object]", body["attestation"])
    assert (body["operator"], attestation["operator"]) == (OPERATOR_ID, OPERATOR_ID)
    assert body["ranAt"] == clock.unix_millis()
    sto = panel.services.sto
    assert sto is not None
    assert body["stoSource"] == sto.latest(clock.monotonic()).source.value
    # The STO the desk read is the console's own: its reading, judged ready for the bench.
    assert {c["id"]: c["state"] for c in cast("list[Mapping[str, str]]", body["controls"])}[
        "sto_ready"
    ] == "ok"
    controls = cast("list[Mapping[str, str]]", body["controls"])
    assert {c["id"]: c["state"] for c in controls}["drive_limits"] == "ok"
    assert body["drive"] is not None
    await panel.close()


class Settled:
    """The panel's own pre-flight, its run already over: the desk goes straight to the read."""

    def __init__(self, inner: PreflightRuns) -> None:
        self.inner = inner

    @property
    def running(self) -> bool:
        return False

    def rerun(self) -> None:
        """Nothing to run again: the run is over."""

    def inputs(self) -> PreflightInputs:
        return self.inner.inputs()


class Exchange(Protocol):
    """One exchange of the simulated drive, as a method of its class."""

    @abstractmethod
    def __call__(self, drive: SimulatedDrive, /, *args: object) -> Awaitable[object]:
        """Send it, and await its answer."""


def yielding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each exchange of the simulated drive yields to the loop, as the ATV320 driver's
    executor does on copper."""
    for name in ("read_status", "read_limits", "write_command", "write_speed"):
        real = cast("Exchange", getattr(SimulatedDrive, name))

        def wrapped(real: Exchange = real) -> Exchange:
            async def call(self: SimulatedDrive, *args: object) -> object:
                await asyncio.sleep(0)
                return await real(self, *args)

            return call

        monkeypatch.setattr(SimulatedDrive, name, wrapped())


async def test_a_start_waited_for_or_armed_is_not_rest_whatever_the_snapshot_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review probe P1: a start in the mailbox, then one armed while a run reads the drive.
    The snapshot stays REPOS all along; the console's own rule says otherwise."""
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_800_000_000_000))
    panel = build_panel(
        config_from(LINKED_ENV), clock=clock, profiles_path=tmp_path / "p.json", preflight=True
    )
    desk = panel.services.commissioning
    assert isinstance(desk, CommissioningDesk)
    sent: list[Mapping[str, JsonValue]] = []

    async def post(body: Mapping[str, JsonValue]) -> Result[Document, CloudError]:
        sent.append(body)
        return Ok({"recordId": "r", "verdict": "conforming", "findings": [], "signed": True})

    link = DashboardLink(post=post, announced=lambda: ContractVersion("1.18"))
    desk.bind(link)
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    for _ in range(3):
        clock.advance(Seconds(0.2))
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()
    yielding(monkeypatch)
    assert desk.view().refusal is None
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    assert panel.runtime.snapshot().mode is RunMode.REPOS
    assert desk.view().refusal == f"console pas au repos : {NOT_AT_REST}"
    assert await desk.run("user-1") == Err(f"console pas au repos : {NOT_AT_REST}")
    await panel.control_step()  # the start armed and running
    await panel.close()

    # Again on a console at rest: the run passes its checks, then a start is armed while it
    # reads the drive. Nothing is sent.
    clock = ManualClock(Monotonic(100.0), UnixMillis(1_800_000_000_000))
    panel = build_panel(
        config_from(LINKED_ENV), clock=clock, profiles_path=tmp_path / "q.json", preflight=True
    )
    built = panel.services.commissioning
    assert isinstance(built, CommissioningDesk)
    setup = built._setup  # pyright: ignore[reportPrivateUsage]  # the panel's own setup
    racing = CommissioningDesk(replace(setup, monitor=Settled(setup.monitor)))
    racing.bind(link)
    assert isinstance(panel.surface.attest_estop_wiring(OPERATOR), Ok)
    for _ in range(3):
        clock.advance(Seconds(0.2))
        panel.surface.note_presence(OPERATOR)
        await panel.control_step()
    assert racing.view().refusal is None
    run = asyncio.create_task(racing.run("user-1"))
    await asyncio.sleep(0)
    assert isinstance(
        panel.surface.submit_start_manual(occupancy=Occupancy.BENCH, operator=OPERATOR), Ok
    )
    tick = asyncio.create_task(panel.control_step())
    _, done = await asyncio.gather(tick, run)
    assert done == Err(dropped(NOT_AT_REST))
    assert sent == []
    assert panel.runtime.mode is RunMode.MANUEL
    await panel.close()
