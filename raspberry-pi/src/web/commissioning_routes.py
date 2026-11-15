"""The commissioning card's routes: ``GET /api/commissioning`` and ``POST /api/commissioning/run``
(ANH-115).

Only a console linked to a dashboard offers them: one that is not answers ``null`` to the
read and **409** to the run. Behind the device token like every route of the console; the
run takes an operator signed in (ANH-152), who is named in the record the dashboard keeps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final, assert_never

from fastapi import Depends, FastAPI, HTTPException, Request, params

from src.commissioning_view import CommissioningView, LastRun, NotRecorded, Recorded
from src.console_access import SignedIn
from src.result import Err
from src.web.deps import Services

NOT_LINKED: Final[str] = (
    "mise en service : console non reliee a un tableau de bord, rien ne l'enregistrerait"
)


@dataclass(frozen=True, slots=True)
class ControlRow:
    """One control of the commissioning, as it was judged."""

    id: str
    label: str
    state: str
    detail: str


@dataclass(frozen=True, slots=True)
class FindingRow:
    """One finding of the dashboard: what the registry expects, what was read."""

    id: str
    expected: str
    read: str


@dataclass(frozen=True, slots=True)
class LastRunRow:
    """The last commissioning: when, by whom, what it judged, what the dashboard said."""

    at: int
    operator: str
    controls: tuple[ControlRow, ...]
    recorded: bool
    record_id: str | None
    verdict: str | None
    """``conforming`` or ``nonconforming``; null when it was not recorded."""

    findings: tuple[FindingRow, ...]
    signed: bool | None
    reason: str | None
    """Why the dashboard did not record it; null when it did."""

    @classmethod
    def of(cls, last: LastRun) -> LastRunRow:
        controls = tuple(
            ControlRow(result.id, result.label, result.state.value, result.detail)
            for result in last.controls
        )
        outcome = last.outcome
        match outcome:
            case Recorded():
                return cls(
                    at=last.at,
                    operator=last.operator,
                    controls=controls,
                    recorded=True,
                    record_id=outcome.record_id,
                    verdict=outcome.verdict.value,
                    findings=tuple(
                        FindingRow(finding.id, finding.expected, finding.read)
                        for finding in outcome.findings
                    ),
                    signed=outcome.signed,
                    reason=None,
                )
            case NotRecorded():
                return cls(
                    at=last.at,
                    operator=last.operator,
                    controls=controls,
                    recorded=False,
                    record_id=None,
                    verdict=None,
                    findings=(),
                    signed=None,
                    reason=outcome.reason,
                )
        raise assert_never(outcome)


@dataclass(frozen=True, slots=True)
class CommissioningRow:
    """``GET /api/commissioning``: one under way, why none can start now, the last one."""

    running: bool
    refusal: str | None
    last: LastRunRow | None

    @classmethod
    def of(cls, view: CommissioningView) -> CommissioningRow:
        last = view.last
        return cls(
            running=view.running,
            refusal=view.refusal,
            last=None if last is None else LastRunRow.of(last),
        )


def register_commissioning(
    app: FastAPI,
    *,
    services: Services,
    auth: Sequence[params.Depends],
    acting: Callable[[Request], Awaitable[SignedIn]],
) -> None:
    """The routes, behind the device token; the run takes an operator signed in."""

    @app.get("/api/commissioning", dependencies=auth, tags=["safety"])
    async def read_commissioning() -> CommissioningRow | None:
        """What the card shows, or ``null`` on a console linked to no dashboard."""
        desk = services.commissioning
        return None if desk is None else CommissioningRow.of(desk.view())

    @app.post("/api/commissioning/run", dependencies=auth, tags=["safety"])
    async def run_commissioning(who: SignedIn = Depends(acting)) -> CommissioningRow:
        """Run the commissioning checks at rest, then send them to the dashboard.

        Answered once the dashboard did, or did not, record it: a minute at most. **409**
        with the reason, in the card's words, when none can start (one under way, no
        dashboard of 1.18, no emergency-stop attestation, the console not at rest), or
        when the console left rest while it ran, nothing sent.
        """
        desk = services.commissioning
        if desk is None:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=NOT_LINKED)
        done = await desk.run(who.user_id)
        if isinstance(done, Err):
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=done.error)
        return CommissioningRow.of(desk.view())
