"""The pre-flight page's routes: ``GET /api/preflight``, ``POST /api/preflight/run`` (ANH-142)
and ``POST /api/preflight/acknowledge`` (ANH-145).

The page shows the list as the console judges it now, for the bench and for a
person on board, and asks for a new run ("Relancer"). It decides nothing: the
arming gate judges the same list again at every start. The acknowledgement is
the operator's, by name: the stops the dashboard may not count are no longer a
reason to hold a person on board.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import Depends, FastAPI, HTTPException, Request, params

from src.console_access import SignedIn
from src.record_uplink import LOST_STOP, NOT_LINKED
from src.result import Err
from src.training.preflight import PreflightMonitor, PreflightReport
from src.training.types import Occupancy
from src.web.deps import Services

NO_PREFLIGHT: Final[str] = "aucun pre-vol sur cette console"


@dataclass(frozen=True, slots=True)
class PreflightControlRow:
    """One control: ``state`` is ``ok``, ``pending``, ``warn`` or ``block``."""

    id: str
    label: str
    state: str
    detail: str


@dataclass(frozen=True, slots=True)
class PreflightReportRow:
    """The list judged for one occupancy, in its order."""

    occupancy: str
    verdict: str
    first_block: str | None
    """The reason a start would be refused now; null when nothing refuses it."""

    controls: tuple[PreflightControlRow, ...]

    @classmethod
    def of(cls, report: PreflightReport) -> PreflightReportRow:
        first = report.first_block
        return cls(
            occupancy=report.occupancy.value,
            verdict=report.verdict.value,
            first_block=None if first is None else first.detail,
            controls=tuple(
                PreflightControlRow(r.id, r.label, r.state.value, r.detail) for r in report.results
            ),
        )


_LOST_PREFIX: Final[str] = LOST_STOP.split("{why}", maxsplit=1)[0]


@dataclass(frozen=True, slots=True)
class PreflightRow:
    """``GET /api/preflight``: the list for both occupancies, and the last completed run."""

    running: bool
    last: str | None
    """The verdict of the last completed run (``ok``, ``warn``, ``block``); null before one."""

    battery_pct: float | None
    bench: PreflightReportRow
    occupied: PreflightReportRow
    unreported_stop: str | None = None
    """Why a person on board is held for a stop the dashboard may not count (ANH-145);
    null when none is."""
    acknowledgeable: bool = False
    """Whether that hold is a session lost for good, the only one the operator may
    acknowledge (``POST /api/preflight/acknowledge``); a hold that clears by itself is not."""

    @classmethod
    def of(cls, monitor: PreflightMonitor) -> PreflightRow:
        last = monitor.last
        held = monitor.inputs().unreported_stop
        return cls(
            running=monitor.running,
            last=None if last is None else last.verdict.value,
            unreported_stop=held,
            # The uplink names a loss first, in these words (pinned by its tests).
            acknowledgeable=held is not None and held.startswith(_LOST_PREFIX),
            battery_pct=monitor.battery,
            bench=PreflightReportRow.of(monitor.report(Occupancy.BENCH)),
            occupied=PreflightReportRow.of(monitor.report(Occupancy.OCCUPIED)),
        )


def register_preflight(
    app: FastAPI,
    *,
    services: Services,
    auth: Sequence[params.Depends],
    acting: Callable[[Request], Awaitable[SignedIn]],
) -> None:
    """The routes, behind the device token like every route of the console; the two that
    act take an operator signed in (ANH-152)."""

    @app.get("/api/preflight", dependencies=auth, tags=["safety"])
    async def read_preflight() -> PreflightRow | None:
        """The pre-flight now, or ``null`` on a console that has none."""
        monitor = services.preflight
        return None if monitor is None else PreflightRow.of(monitor)

    @app.post(
        "/api/preflight/run",
        dependencies=auth,
        status_code=HTTPStatus.ACCEPTED,
        tags=["safety"],
    )
    async def run_preflight(_who: SignedIn = Depends(acting)) -> PreflightRow:
        """Start a new run: the BITalino's battery is read again at rest, then every control
        is waited for again. Starts nothing and refuses nothing by itself."""
        monitor = services.preflight
        if monitor is None:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=NO_PREFLIGHT)
        monitor.rerun()
        return PreflightRow.of(monitor)

    @app.post("/api/preflight/acknowledge", dependencies=auth, tags=["safety"])
    async def acknowledge_stops(who: SignedIn = Depends(acting)) -> PreflightRow:
        """Release, by the operator signed in, the sessions the dashboard will never have
        (refused for good, unreadable, given up) that hold a person on board. Nothing
        else: a hold that clears once the dashboard has a session stays. Starts nothing.
        **409** while a session runs, or when nothing of that kind is held."""
        monitor = services.preflight
        acknowledge = services.stop_acknowledge
        if monitor is None or acknowledge is None:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=NOT_LINKED)
        released = await acknowledge(who.user_id)
        if isinstance(released, Err):
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=released.error)
        return PreflightRow.of(monitor)
