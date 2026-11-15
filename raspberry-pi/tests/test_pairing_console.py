"""ANH-115 EX-2: a console with no key, paired from its first-power-up screen.

The routes (``GET`` and ``POST /api/pairing``), behind the device token and answered
only by a console that has no key; the console built with its screen, and without one
when it has a key; the startup that reads ``data/machine.env``; and the whole of it on
the simulated drive: the code typed, the key written, the controlled restart at rest,
and the next start linked to the dashboard.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import httpx
import pytest

from src import local_panel
from src.clock import ManualClock
from src.config_sync import AppliedConfig
from src.local_config import LocalConfig
from src.local_panel import EXIT_OK, EXIT_RESTART, build_panel, main
from src.pairing import (
    NO_ADDRESS,
    PairedLink,
    PairingDesk,
    PairingSetup,
    read_paired,
    startup_environment,
    write_paired,
)
from src.result import Ok
from src.web.app import create_app
from src.web.deps import WebConfig
from src.web.pairing_routes import PAIRED
from tests.test_local_panel import BASE_ENV, FakeWeb, config_from
from tests.test_pairing import (
    CODE,
    KEY,
    LINK,
    MACHINE,
    ORIGIN,
    PREFIX,
    SERIAL,
    answer,
    client_of,
    serial_file,
)
from tests.test_web_access import TOKEN, console, device

LINKED_ENV: Final[Mapping[str, str]] = {**BASE_ENV, "MACHINE_API_KEY": KEY, "CONVEX_URL": ORIGIN}

type Handler = Callable[[httpx.Request], httpx.Response]


def dashboard(asked: list[httpx.Request] | None = None) -> Handler:
    """A dashboard that accepts the code: it answers the pairing, and nothing else."""

    def handle(request: httpx.Request) -> httpx.Response:
        if asked is not None:
            asked.append(request)
        if request.url.path != "/api/machine/pair":
            return httpx.Response(404)
        return httpx.Response(200, json=answer())

    return handle


def setup(tmp_path: Path, handler: Handler, address: str | None = ORIGIN) -> PairingSetup:
    return PairingSetup(
        path=tmp_path / "data" / "machine.env",
        serial_path=serial_file(tmp_path),
        address=address,
        client=client_of(handler),
    )


# --- the routes ---------------------------------------------------------------------------


async def test_a_console_with_a_key_answers_no_pairing(tmp_path: Path) -> None:
    c = console(tmp_path)
    async with c.client() as client:
        read = await client.get("/api/pairing", headers=device())
        sent = await client.post("/api/pairing", json={"code": CODE}, headers=device())
    assert (read.status_code, read.json()) == (httpx.codes.CONFLICT, {"detail": PAIRED})
    assert (sent.status_code, sent.json()) == (httpx.codes.CONFLICT, {"detail": PAIRED})


async def test_the_screen_pairs_behind_the_device_token_and_never_echoes_the_code(
    tmp_path: Path,
) -> None:
    c = console(tmp_path)
    services = replace(c.services, pairing=PairingDesk(setup(tmp_path, dashboard())))
    app = create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        nobody = await client.post("/api/pairing", json={"code": CODE})
        assert nobody.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN)
        before = await client.get("/api/pairing", headers=device())
        paired = await client.post("/api/pairing", json={"code": CODE}, headers=device())
        after = await client.get("/api/pairing", headers=device())
        again = await client.post("/api/pairing", json={"code": CODE}, headers=device())
    shown = cast("dict[str, object]", before.json())
    steps = cast("list[dict[str, str]]", shown.pop("steps"))
    assert shown == {
        "serial": SERIAL,
        "serial_problem": None,
        "address": ORIGIN,
        "key_prefix": None,
    }
    assert [(step["id"], step["label"]) for step in steps] == [
        ("ota", "Inscription au service de mise a jour"),
        ("network", "Reseau de la console"),
    ]
    assert paired.status_code == httpx.codes.OK
    assert paired.json()["key_prefix"] == PREFIX
    assert CODE not in paired.text
    assert KEY not in paired.text
    assert after.json()["key_prefix"] == PREFIX
    assert again.status_code == httpx.codes.CONFLICT
    assert read_paired(tmp_path / "data" / "machine.env") == Ok(LINK)


async def test_a_refusal_is_a_conflict_in_the_screen_s_words(tmp_path: Path) -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "pairing_refused", "message": "x"})

    c = console(tmp_path)
    services = replace(c.services, pairing=PairingDesk(setup(tmp_path, refused)))
    app = create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        sent = await client.post("/api/pairing", json={"code": CODE}, headers=device())
    assert sent.status_code == httpx.codes.CONFLICT
    assert cast("dict[str, str]", sent.json())["detail"].startswith("code refuse")
    assert CODE not in sent.text


async def test_a_console_with_no_address_shows_why_and_pairs_nothing(tmp_path: Path) -> None:
    asked: list[httpx.Request] = []
    c = console(tmp_path)
    services = replace(
        c.services, pairing=PairingDesk(setup(tmp_path, dashboard(asked), address=None))
    )
    app = create_app(services=services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN))
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        shown = await client.get("/api/pairing", headers=device())
        sent = await client.post(
            "/api/pairing", json={"code": CODE, "address": ORIGIN}, headers=device()
        )
    assert cast("dict[str, object]", shown.json())["address"] is None
    assert (sent.status_code, sent.json()) == (httpx.codes.CONFLICT, {"detail": NO_ADDRESS})
    assert asked == []


# --- the console --------------------------------------------------------------------------


def test_a_console_with_a_key_is_built_without_the_screen(tmp_path: Path) -> None:
    linked = build_panel(
        config_from(LINKED_ENV),
        clock=ManualClock(),
        profiles_path=tmp_path / "p.json",
        pairing=setup(tmp_path, dashboard()),
    )
    assert linked.services.pairing is None


Seen = list[tuple[LocalConfig, Mapping[str, str] | None]]


def capture(monkeypatch: pytest.MonkeyPatch) -> Seen:
    seen: Seen = []

    async def fake(
        config: LocalConfig,
        *,
        stopped: Callable[[int], object],
        notify_socket: str | None,
        applied: AppliedConfig | None = None,
        local_env: Mapping[str, str] | None = None,
    ) -> int:
        del stopped, notify_socket, applied
        seen.append((config, local_env))
        return EXIT_OK

    monkeypatch.setattr(local_panel, "run_console", fake)
    return seen


def test_main_starts_linked_on_what_a_pairing_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "machine.env"
    assert write_paired(path, LINK) == Ok(None)
    monkeypatch.setattr(local_panel, "PAIRED_PATH", path)
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", tmp_path / "none.json")
    seen = capture(monkeypatch)
    assert main(BASE_ENV) == EXIT_OK
    config, local_env = seen[0]
    assert config.cloud is not None
    assert (config.cloud.url, config.cloud.api_key) == (ORIGIN, KEY)
    assert config.record.machine_id == MACHINE
    assert local_env is not None
    assert local_env["RECORD_MACHINE_ID"] == MACHINE


def test_main_ignores_a_file_that_is_not_one_and_starts_unpaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "machine.env"
    path.write_text(f"MACHINE_API_KEY={KEY}\nMOTOR_MAX_RPM=900\n")
    monkeypatch.setattr(local_panel, "PAIRED_PATH", path)
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", tmp_path / "none.json")
    seen = capture(monkeypatch)
    assert main(BASE_ENV) == EXIT_OK
    config, _ = seen[0]
    assert config.cloud is None
    assert "data/machine.env ignored" in caplog.text
    assert KEY not in caplog.text


def test_main_leaves_the_file_out_whole_when_the_env_has_its_own_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "machine.env"
    assert write_paired(path, LINK) == Ok(None)
    monkeypatch.setattr(local_panel, "PAIRED_PATH", path)
    monkeypatch.setattr(local_panel, "APPLIED_CONFIG_PATH", tmp_path / "none.json")
    other = "anh1." + "3" * 32 + "." + "4" * 64
    seen = capture(monkeypatch)
    elsewhere = "https://other.convex.site"
    assert main({**BASE_ENV, "MACHINE_API_KEY": other, "CONVEX_URL": elsewhere}) == EXIT_OK
    config, _ = seen[0]
    assert config.cloud is not None
    assert (config.cloud.url, config.cloud.api_key) == (elsewhere, other)
    # Not the file's machine either: nothing of the file is mixed with the .env's key.
    assert config.record.machine_id == "unassigned"
    assert "data/machine.env not used (the .env sets MACHINE_API_KEY)" in caplog.text
    assert other not in caplog.text
    assert KEY not in caplog.text


async def test_on_the_simulated_drive_a_code_typed_pairs_restarts_and_links_the_next_start(
    tmp_path: Path,
) -> None:
    """The simulation of a commissioning: a console with no key, its screen, the code an
    admin issued, the key written, the controlled restart, and the next start linked."""
    asked: list[httpx.Request] = []
    panel = build_panel(
        config_from(BASE_ENV),
        clock=ManualClock(),
        profiles_path=tmp_path / "data" / "profiles.json",
        pairing=setup(tmp_path, dashboard(asked)),
    )
    assert isinstance(panel.services.pairing, PairingDesk)
    app = create_app(
        services=panel.services, config=WebConfig(host="127.0.0.1", port=8080, token=TOKEN)
    )
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        shown = await client.get("/api/pairing", headers=device())
        paired = await client.post("/api/pairing", json={"code": CODE}, headers=device())
    assert cast("dict[str, object]", shown.json())["address"] == ORIGIN
    assert paired.status_code == httpx.codes.OK
    assert [str(request.url) for request in asked] == [f"{ORIGIN}/api/machine/pair"]
    # The restart asked is taken at rest, with its own code.
    outcome = await panel.run(asyncio.Event(), FakeWeb())
    assert outcome == EXIT_RESTART
    written = tmp_path / "data" / "machine.env"
    assert read_paired(written) == Ok(PairedLink(KEY, ORIGIN, MACHINE))
    # The next start: the environment main() starts on (startup_environment).
    restarted = config_from(startup_environment(BASE_ENV, written))
    assert restarted.cloud is not None
    assert (restarted.cloud.url, restarted.cloud.api_key) == (ORIGIN, KEY)
    assert restarted.record.machine_id == MACHINE
