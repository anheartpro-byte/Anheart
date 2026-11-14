"""ANH-141 EX-4: the console's page reads its configuration; an operator signed in applies a
safety revision (``GET /api/config``, ``POST /api/config/apply``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx

from src.config_view import ENV_VIEW, ChangeView, ConfigView, PendingView
from src.result import Err, Ok, Result
from src.web.app import create_app
from src.web.deps import WebConfig
from tests.access_support import sign_in
from tests.test_web_access import TOKEN, console, device

WAITING = ConfigView(
    revision=3,
    config_hash="ab" * 32,
    name="Banc",
    location="Atelier",
    pending=PendingView(4, (ChangeView("MOTOR_MAX_RPM", True, "300", "400"),)),
    refusal=None,
    restarting=False,
)


async def test_the_page_reads_the_configuration_and_a_env_console_says_none(
    tmp_path: Path,
) -> None:
    c = console(tmp_path)
    async with c.client() as client:
        answer = await client.get("/api/config", headers=device())
    assert answer.status_code == httpx.codes.OK
    assert answer.json()["revision"] is None
    assert answer.json() == {
        "revision": ENV_VIEW.revision,
        "config_hash": None,
        "name": None,
        "location": None,
        "pending": None,
        "refusal": None,
        "restarting": False,
    }


async def test_an_operator_signed_in_applies_the_safety_revision_waiting(tmp_path: Path) -> None:
    confirmed: list[str] = []

    async def confirm(operator: str) -> Result[int, str]:
        confirmed.append(operator)
        return Ok(4)

    c = console(tmp_path)
    services = replace(c.services, config_view=lambda: WAITING, config_confirm=confirm)
    app = create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        read = await client.get("/api/config", headers=device())
        assert read.json()["pending"] == {
            "revision": 4,
            "changes": [{"key": "MOTOR_MAX_RPM", "safety": True, "before": "300", "after": "400"}],
        }
        nobody = await client.post("/api/config/apply", headers=device())
        assert nobody.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)
        assert confirmed == []
        await sign_in(client, services, device())
        applied = await client.post("/api/config/apply", headers=device())
    assert applied.status_code == httpx.codes.OK
    assert applied.json() == {"revision": 4}
    assert len(confirmed) == 1


async def test_nothing_waiting_or_away_from_rest_is_a_conflict(tmp_path: Path) -> None:
    async def refuse(_operator: str) -> Result[int, str]:
        return Err("console pas au repos : terminer la seance avant d'appliquer")

    c = console(tmp_path)
    for confirm in (None, refuse):
        services = replace(c.services, config_confirm=confirm)
        app = create_app(
            services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN)
        )
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await sign_in(client, services, device())
            answer = await client.post("/api/config/apply", headers=device())
        assert answer.status_code == httpx.codes.CONFLICT
        assert answer.json()["detail"]
