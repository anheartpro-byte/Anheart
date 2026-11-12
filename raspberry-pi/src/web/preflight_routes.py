"""The pre-flight page's routes: ``GET /api/preflight`` and ``POST /api/preflight/run`` (ANH-142).

The page shows the list as the console judges it now, for the bench and for a
person on board, and asks for a new run ("Relancer"). It decides nothing: the
arming gate judges the same list again at every start.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, HTTPException, params

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


@dataclass(frozen=True, slots=True)
class PreflightRow:
    """``GET /api/preflight``: the list for both occupancies, and the last completed run."""

    running: bool
    last: str | None
    """The verdict of the last completed run (``ok``, ``warn``, ``block``); null before one."""

    battery_pct: float | None
    bench: PreflightReportRow
    occupied: PreflightReportRow

    @classmethod
    def of(cls, monitor: PreflightMonitor) -> PreflightRow:
        last = monitor.last
        return cls(
            running=monitor.running,
            last=None if last is None else last.verdict.value,
            battery_pct=monitor.battery,
            bench=PreflightReportRow.of(monitor.report(Occupancy.BENCH)),
            occupied=PreflightReportRow.of(monitor.report(Occupancy.OCCUPIED)),
        )


def register_preflight(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """The two routes, behind the token check like every route of the console."""

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
    async def run_preflight() -> PreflightRow:
        """Start a new run: the BITalino's battery is read again at rest, then every control
        is waited for again. Starts nothing and refuses nothing by itself."""
        monitor = services.preflight
        if monitor is None:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=NO_PREFLIGHT)
        monitor.rerun()
        return PreflightRow.of(monitor)
