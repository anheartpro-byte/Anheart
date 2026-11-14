"""The first-power-up screen's routes: ``GET /api/pairing`` and ``POST /api/pairing`` (ANH-115).

Only a console that has no key offers them: one that has a key answers **409** to both.
Behind the device token like every route of the console, and without an operator
signed in: no operator list exists before a console is paired, and a pairing takes a
code an admin issued on the site for this Pi anyway. The code is never echoed, stored
or logged; the key the dashboard gives is written to ``data/machine.env`` and shown by
its prefix only (:mod:`src.pairing`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Final

from fastapi import FastAPI, HTTPException, params

from src.pairing_view import PairingScreen, PendingStep
from src.result import Err
from src.web.deps import Services

PAIRED: Final[str] = "cette console a deja une cle : aucun appairage a faire"


@dataclass(frozen=True, slots=True)
class PendingStepRow:
    """A step of the commissioning this console does not do yet."""

    id: str
    label: str
    detail: str

    @classmethod
    def of(cls, step: PendingStep) -> PendingStepRow:
        return cls(step.id, step.label, step.detail)


@dataclass(frozen=True, slots=True)
class PairingRow:
    """``GET /api/pairing``: the Pi, where the console asks, and a pairing already done."""

    serial: str | None
    serial_problem: str | None
    """Why the Pi's serial number cannot be read; null when it can."""

    address: str | None
    """The dashboard's address, from the root's ``.env``; null: none is set, and the screen
    offers no pairing (an address typed there could be any server)."""

    key_prefix: str | None
    """The prefix of the key received; null before a pairing. The console then restarts."""

    steps: tuple[PendingStepRow, ...]


@dataclass(frozen=True, slots=True)
class PairingBody:
    """The code shown on the site. Nothing else: the address is the root's ``.env``'s."""

    code: str


@dataclass(frozen=True, slots=True)
class PairedRow:
    """``POST /api/pairing`` accepted: the key's prefix; the console restarts at rest."""

    key_prefix: str
    steps: tuple[PendingStepRow, ...]


def _desk(services: Services) -> PairingScreen:
    desk = services.pairing
    if desk is None:
        raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=PAIRED)
    return desk


def register_pairing(app: FastAPI, *, services: Services, auth: Sequence[params.Depends]) -> None:
    """The routes, behind the device token; no operator: there is none before a pairing."""

    @app.get("/api/pairing", dependencies=auth, tags=["system"])
    async def read_pairing() -> PairingRow:
        """What the screen shows. **409** on a console that has a key."""
        view = _desk(services).view()
        return PairingRow(
            serial=view.serial,
            serial_problem=view.serial_problem,
            address=view.address,
            key_prefix=view.key_prefix,
            steps=tuple(PendingStepRow.of(step) for step in view.steps),
        )

    @app.post("/api/pairing", dependencies=auth, tags=["system"])
    async def pair(body: PairingBody) -> PairedRow:
        """Send the code with this Pi's serial number, write the key received, restart.

        **409** with the reason, in the screen's words, for every refusal: a console that
        has a key, a pairing under way or done, an unreadable serial number, no address,
        a code refused by the dashboard, an answer it cannot take, a file it cannot write.
        """
        done = await _desk(services).pair(body.code)
        if isinstance(done, Err):
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=done.error)
        return PairedRow(
            key_prefix=done.value.key_prefix,
            steps=tuple(PendingStepRow.of(step) for step in done.value.steps),
        )
