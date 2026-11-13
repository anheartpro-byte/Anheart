"""ANH-152 end to end, in simulation: the real console, its list, its sign-in, its stops.

The composition root of :func:`src.local_panel.build_panel` over the simulated
drive and BITalino, linked to a dashboard double that signs the operator list
with a key made for the test (its public half given to the console in place of
the one of the image). Through the real web app:

* a console that never received a list opens no action, and its E-STOP works;
* the list arrives at rest, an operator of it signs in and acts under their account;
* disabled on the site, they are signed out at the next synchronisation and can no
  longer sign in, and a restart keeps it so (the list is verified again from disk).
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Final, override

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import TypeAdapter

from src.clock import ManualClock
from src.cloud_sync import Document, Refused, Unreachable
from src.console_access import (
    LIST_KIND,
    LOOPBACK_DEVICE,
    OPERATORS_PATH,
    ConsoleAccess,
    device_actor,
)
from src.local_config import LocalConfig, load_local_config
from src.local_panel import LocalPanel, build_panel
from src.operator_sync import OperatorSync
from src.result import Ok, Result
from src.units import Monotonic, Seconds, UnixMillis
from src.web.app import create_app
from src.web.schemas import AccessRow
from tests.access_support import CODE, OPERATOR_ID, OPERATOR_NAME, held_list, signed_code
from tests.fake_dashboard import FakeDashboard

ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
    "MACHINE_API_KEY": "machine-key",
    "CONVEX_URL": "https://example.convex.site",
}

type Reply = Result[Document, Unreachable | Refused]


@dataclass
class SigningDashboard(FakeDashboard):
    """The dashboard double, answering the operator list it is told to sign."""

    signer: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    operators: tuple[str, ...] = ()
    issued_at: int = 1_790_000_000_000

    @override
    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Reply:
        if path != OPERATORS_PATH:
            return await super().get(path, params)
        code = signed_code(CODE)
        entries = [
            {
                "userId": user_id,
                "name": OPERATOR_NAME,
                "code": {
                    "algorithm": "scrypt",
                    "n": code.n,
                    "r": code.r,
                    "p": code.p,
                    "salt": base64.b64encode(code.salt).decode(),
                    "hash": base64.b64encode(code.digest).decode(),
                },
            }
            for user_id in self.operators
        ]
        text = json.dumps(
            {
                "kind": LIST_KIND,
                "machineId": "machine-1",
                "issuedAt": self.issued_at,
                "operators": entries,
                "devices": [],
            }
        )
        signature = base64.b64encode(self.signer.sign(text.encode())).decode()
        return Ok({"status": "signed", "payload": text, "signature": signature})

    def public(self) -> bytes:
        return self.signer.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )


def config() -> LocalConfig:
    loaded = load_local_config(ENV)
    assert isinstance(loaded, Ok)
    return loaded.value


def console(
    tmp_path: Path, clock: ManualClock, dashboard: SigningDashboard, monkeypatch: pytest.MonkeyPatch
) -> LocalPanel:
    monkeypatch.setattr("src.local_panel.read_public_key", dashboard.public)
    return build_panel(
        config(),
        clock=clock,
        profiles_path=tmp_path / "data" / "profiles.local.json",
        transport=lambda _config: dashboard,
    )


def client(panel: LocalPanel) -> httpx.AsyncClient:
    app = create_app(services=panel.services, config=config().web)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def sign_in(session: httpx.AsyncClient) -> httpx.Response:
    return await session.post("/api/access/sign-in", json={"user_id": OPERATOR_ID, "code": CODE})


async def test_no_action_before_the_list_then_an_operator_acts_until_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_790_000_000_000))
    dashboard = SigningDashboard(now=lambda: int(clock.unix_millis()))
    panel = console(tmp_path, clock, dashboard, monkeypatch)
    async with client(panel) as page:
        # A fresh console: nothing received, nothing opens but the reads and the stops.
        assert (await page.get("/api/access")).json()["received"] is False
        refused = await page.post("/api/manual/start", json={"occupancy": "bench"})
        assert refused.status_code == HTTPStatus.UNAUTHORIZED
        assert refused.json()["code"] == "operator_required"
        attest = {"sto_jumper_removed": True, "mushroom_wired_nc": True}
        assert (await page.post("/api/safety/attest", json=attest)).status_code == 401
        assert (await sign_in(page)).status_code == HTTPStatus.FORBIDDEN
        assert (await page.get("/api/status")).status_code == HTTPStatus.OK

        # The list arrives at rest.
        dashboard.operators = (OPERATOR_ID,)
        await panel.operators_step()
        assert (tmp_path / "data" / "operators.json").exists()
        assert (await sign_in(page)).status_code == HTTPStatus.OK
        attested = await page.post("/api/safety/attest", json=attest)
        assert attested.status_code == HTTPStatus.OK
        assert attested.json()["operator"] == OPERATOR_ID

        # Disabled on the site: at the next synchronisation, signed out and refused.
        dashboard.operators = ()
        dashboard.issued_at += 60_000
        clock.advance(Seconds(60.0))
        await panel.operators_step()
        assert (await page.post("/api/presence")).status_code == HTTPStatus.UNAUTHORIZED
        assert (await sign_in(page)).status_code == HTTPStatus.FORBIDDEN

    # A restart: the list on disk is verified again and still holds nobody.
    restarted = console(tmp_path, clock, dashboard, monkeypatch)
    async with client(restarted) as page:
        listed = TypeAdapter(AccessRow).validate_json((await page.get("/api/access")).content)
        assert listed.received
        assert listed.operators == ()
        assert (await sign_in(page)).status_code == HTTPStatus.FORBIDDEN
        stop = await page.post("/api/session/estop", json={})
        assert stop.status_code == HTTPStatus.OK
        assert stop.json()["operator"] == device_actor(LOOPBACK_DEVICE)
        assert restarted.runtime.supervisor.standing is not None


async def test_a_list_another_key_signed_opens_nothing_after_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_790_000_000_000))
    dashboard = SigningDashboard(now=lambda: int(clock.unix_millis()), operators=(OPERATOR_ID,))
    panel = console(tmp_path, clock, dashboard, monkeypatch)
    await panel.operators_step()
    assert panel.services.access is not None
    assert panel.services.access.held is not None
    # A new image with another public key: the stored list no longer verifies.
    rotated = SigningDashboard(now=dashboard.now)
    restarted = console(tmp_path, clock, rotated, monkeypatch)
    assert restarted.services.access is not None
    assert restarted.services.access.held is None
    async with client(restarted) as page:
        assert (await sign_in(page)).status_code == HTTPStatus.FORBIDDEN
        assert (await page.post("/api/session/estop", json={})).status_code == HTTPStatus.OK


async def test_a_bug_in_the_list_step_leaves_the_console_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock(Monotonic(10.0), UnixMillis(1_790_000_000_000))
    panel = console(tmp_path, clock, SigningDashboard(now=lambda: 0), monkeypatch)

    async def broken(_self: OperatorSync) -> None:
        raise RuntimeError("synthetic")

    monkeypatch.setattr(OperatorSync, "step", broken)
    await panel.operators_step()
    assert "operator list step failed; the list in force is unchanged" in caplog.text


async def test_a_console_given_its_access_keeps_it(tmp_path: Path) -> None:
    clock = ManualClock()
    given = ConsoleAccess(clock=clock, held=held_list(), lockout_path=None)
    loaded = load_local_config({**ENV, "MACHINE_API_KEY": ""})
    assert isinstance(loaded, Ok)
    panel = build_panel(loaded.value, clock=clock, profiles_path=tmp_path / "p.json", access=given)
    assert panel.services.access is given


async def test_an_unlinked_console_asks_for_no_list(tmp_path: Path) -> None:
    loaded = load_local_config({**ENV, "MACHINE_API_KEY": ""})
    assert isinstance(loaded, Ok)
    panel = build_panel(loaded.value, clock=ManualClock(), profiles_path=tmp_path / "p.json")
    await panel.operators_step()
    assert panel.services.access is not None
    assert panel.services.access.held is None
