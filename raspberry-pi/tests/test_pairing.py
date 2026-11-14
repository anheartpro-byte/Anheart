"""ANH-115 EX-2: pairing a console that has no key (:mod:`src.pairing`).

The serial number read as the dashboard compares it, ``data/machine.env`` written
atomically and private and read with a strict parser, the ``.env`` winning key by key,
the call to ``POST /api/machine/pair`` and every answer it can get, and the screen's
side: one pairing, a machine of the ``.env`` never mixed with another, then a restart.
"""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Final

import httpx
import pytest

from src import pairing
from src.contract import CONTRACT_HEADER, CONTRACT_VERSION
from src.pairing import (
    ALREADY_PAIRED,
    MAX_FILE_BYTES,
    PAIRING_BUSY,
    PairedLink,
    PairingDesk,
    PairingRefused,
    PairingSetup,
    PairingThrottled,
    PairingUnreachable,
    PairingUnusable,
    console_network,
    describe_pairing_error,
    enrol_ota,
    https_origin,
    paired_environment,
    parse_paired,
    read_paired,
    read_serial,
    render_paired,
    request_pairing,
    startup_environment,
    write_paired,
)
from src.result import Err, Ok

ORIGIN: Final[str] = "https://deploiement.convex.site"
SERIAL: Final[str] = "10000000c0ffee42"
MACHINE: Final[str] = "machine123"
# Of the form of a machine key, made of one repeated digit: no secret of anything.
KEY: Final[str] = "anh1." + "1" * 32 + "." + "2" * 64
PREFIX: Final[str] = "anh1.11111111"
CODE: Final[str] = "7KQ2-M4XP"
LINK: Final[PairedLink] = PairedLink(KEY, ORIGIN, MACHINE)

type Handler = Callable[[httpx.Request], httpx.Response]


def answer(
    machine_id: object = MACHINE,
    api_key: object = KEY,
    key_prefix: object = PREFIX,
    convex_url: object = ORIGIN,
) -> dict[str, object]:
    return {
        "machineId": machine_id,
        "apiKey": api_key,
        "keyPrefix": key_prefix,
        "convexUrl": convex_url,
        "server_contract_version": "1.17",
    }


def client_of(handler: Handler) -> Callable[[], httpx.AsyncClient]:
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def serial_file(tmp_path: Path, raw: bytes = SERIAL.upper().encode() + b"\x00") -> Path:
    path = tmp_path / "serial-number"
    path.write_bytes(raw)
    return path


# --- the address, the serial number -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        (ORIGIN, ORIGIN),
        (f" {ORIGIN}/ ", ORIGIN),
        ("https://deploiement.convex.site:8443", "https://deploiement.convex.site:8443"),
        ("http://deploiement.convex.site", None),
        ("https://", None),
        ("https://deploiement.convex.site/api", None),
        ("https://deploiement.convex.site?x=1", None),
        ("https://deploiement.convex.site#x", None),
        ("https://user:pass@deploiement.convex.site", None),
        ("https://[::1", None),
        ("", None),
    ],
)
def test_only_an_https_origin_is_an_address(url: str, origin: str | None) -> None:
    assert https_origin(url) == origin


def test_the_serial_number_is_read_as_the_dashboard_compares_it(tmp_path: Path) -> None:
    assert read_serial(serial_file(tmp_path)) == Ok(SERIAL)
    assert read_serial(serial_file(tmp_path, b"not a serial\x00")) == Err("mal forme")
    assert read_serial(tmp_path / "none") == Err("illisible (FileNotFoundError:ENOENT)")


# --- the file -----------------------------------------------------------------------------


def test_the_file_holds_the_three_keys_in_order_and_reads_back() -> None:
    text = render_paired(LINK).decode()
    assert text == f"MACHINE_API_KEY={KEY}\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID={MACHINE}\n"
    assert parse_paired(text) == Ok(LINK)
    assert parse_paired(text.rstrip("\n")) == Ok(LINK)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "3 lignes attendues, 0 lues"),
        (f"MACHINE_API_KEY={KEY}\nCONVEX_URL={ORIGIN}\n", "3 lignes attendues, 2 lues"),
        (
            f"MACHINE_API_KEY={KEY}\nCONVEX_URL={ORIGIN}\nMOTOR_MAX_RPM=900\n",
            "ligne 3 illisible",
        ),
        (f"# pairing\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID={MACHINE}\n", "ligne 1 illisible"),
        (
            f'MACHINE_API_KEY="{KEY}"\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID=x\n',
            "ligne 1 illisible",
        ),
        (
            f"MACHINE_API_KEY=${{KEY}}\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID=x\n",
            "ligne 1 illisible",
        ),
        (
            f"MACHINE_API_KEY={KEY}\nMACHINE_API_KEY={KEY}\nCONVEX_URL={ORIGIN}\n",
            "MACHINE_API_KEY en double",
        ),
        (
            f"MACHINE_API_KEY=anh1.short\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID={MACHINE}\n",
            "MACHINE_API_KEY mal formee",
        ),
        (
            f"MACHINE_API_KEY={KEY}\nCONVEX_URL=http://x.convex.site\nRECORD_MACHINE_ID={MACHINE}\n",
            "CONVEX_URL : une adresse https sans chemin attendue",
        ),
        (
            f"MACHINE_API_KEY={KEY}\nCONVEX_URL={ORIGIN}\nRECORD_MACHINE_ID=a/b\n",
            "RECORD_MACHINE_ID mal forme",
        ),
    ],
)
def test_a_file_with_anything_else_is_refused_and_never_repeats_a_value(
    text: str, why: str
) -> None:
    assert parse_paired(text) == Err(why)
    assert KEY not in why


def test_the_file_is_written_private_atomically_and_read_back(tmp_path: Path) -> None:
    path = tmp_path / "data" / "machine.env"
    assert read_paired(path) == Ok(None)
    assert write_paired(path, LINK) == Ok(None)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / "data" / ".machine.env.tmp").exists()
    assert read_paired(path) == Ok(LINK)
    # Written again over the first: replaced, never appended.
    other = PairedLink(KEY, ORIGIN, "machine456")
    assert write_paired(path, other) == Ok(None)
    assert read_paired(path) == Ok(other)


def test_a_link_where_the_file_belongs_is_neither_written_nor_read(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(render_paired(LINK).decode())
    path = tmp_path / "machine.env"
    path.symlink_to(target)
    assert write_paired(path, LINK) == Err("un lien est a la place du fichier")
    assert read_paired(path) == Err("pas un fichier ordinaire")


def test_a_write_the_disk_refuses_leaves_no_file(tmp_path: Path) -> None:
    parent = tmp_path / "a-file"
    parent.write_text("")
    assert write_paired(parent / "machine.env", LINK) == Err("FileExistsError:EEXIST")


def test_a_failure_after_the_temporary_file_takes_it_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_path: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pairing, "fsync_path", refuse)
    path = tmp_path / "machine.env"
    assert write_paired(path, LINK) == Err("OSError:ENOSPC")
    assert list(tmp_path.iterdir()) == []


def test_what_is_not_a_small_ascii_file_of_the_three_keys_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "machine.env"
    folder.mkdir()
    assert read_paired(folder) == Err("pas un fichier ordinaire")
    big = tmp_path / "big.env"
    big.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert read_paired(big) == Err("trop grand")
    latin = tmp_path / "latin.env"
    latin.write_bytes("CONVEX_URL=é\n".encode("latin-1"))
    assert read_paired(latin) == Err("pas du texte ASCII")
    extra = tmp_path / "extra.env"
    extra.write_text(render_paired(LINK).decode() + "MOTOR_MAX_RPM=900\n")
    assert read_paired(extra) == Err("3 lignes attendues, 4 lues")
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    assert read_paired(blocked / "machine.env") == Err("NotADirectoryError:ENOTDIR")

    def refuse(*_args: object) -> int:
        raise PermissionError(13, "Permission denied")

    good = tmp_path / "good.env"
    good.write_bytes(render_paired(LINK))
    monkeypatch.setattr(os, "open", refuse)
    assert read_paired(good) == Err("PermissionError:EACCES")


# --- the .env and the file ----------------------------------------------------------------


def test_each_key_the_env_sets_wins_and_is_named_when_the_file_says_otherwise() -> None:
    assert paired_environment({"A": "1"}, None) == ({"A": "1"}, ())
    merged, differing = paired_environment(
        {"CONVEX_URL": ORIGIN, "RECORD_MACHINE_ID": "unassigned", "MACHINE_API_KEY": " "}, LINK
    )
    assert (merged["MACHINE_API_KEY"], merged["CONVEX_URL"], merged["RECORD_MACHINE_ID"]) == (
        KEY,
        ORIGIN,
        MACHINE,
    )
    assert differing == ()
    merged, differing = paired_environment(
        {"CONVEX_URL": "https://other.convex.site", "RECORD_MACHINE_ID": "machine999"}, LINK
    )
    assert merged["CONVEX_URL"] == "https://other.convex.site"
    assert merged["RECORD_MACHINE_ID"] == "machine999"
    assert merged["MACHINE_API_KEY"] == KEY
    assert differing == ("CONVEX_URL", "RECORD_MACHINE_ID")


def test_the_console_starts_on_the_file_or_says_why_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "machine.env"
    assert startup_environment({"UI_PORT": "8090"}, path) == {"UI_PORT": "8090"}
    assert write_paired(path, LINK) == Ok(None)
    started = startup_environment({"RECORD_MACHINE_ID": "machine999"}, path)
    assert started["MACHINE_API_KEY"] == KEY
    assert started["RECORD_MACHINE_ID"] == "machine999"
    assert "RECORD_MACHINE_ID of the .env differs from data/machine.env" in caplog.text
    path.write_text("MOTOR_MAX_RPM=900\n")
    assert startup_environment({"UI_PORT": "8090"}, path) == {"UI_PORT": "8090"}
    assert "data/machine.env ignored (3 lignes attendues, 1 lues)" in caplog.text
    assert KEY not in caplog.text


# --- the call -----------------------------------------------------------------------------


async def test_the_dashboard_is_asked_with_the_code_the_serial_and_the_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answer())

    async with client_of(handler)() as client:
        paired = await request_pairing(client, ORIGIN, CODE, SERIAL)
    assert paired == Ok(pairing.Paired(MACHINE, KEY, PREFIX, ORIGIN))
    assert str(seen[0].url) == f"{ORIGIN}/api/machine/pair"
    assert seen[0].headers[CONTRACT_HEADER] == CONTRACT_VERSION
    assert "authorization" not in seen[0].headers
    assert seen[0].content == b'{"code":"7KQ2-M4XP","serialPi":"10000000c0ffee42"}'


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401, json={"error": "pairing_refused", "message": "x"}), PairingRefused()),
        (httpx.Response(401, json={"error": "unauthorized"}), PairingUnusable("statut 401")),
        (httpx.Response(429, json={"error": "rate_limited"}), PairingThrottled()),
        (
            httpx.Response(426, json={"error": "contract_unsupported"}),
            PairingUnusable("statut 426"),
        ),
        (httpx.Response(500, text="oops"), PairingUnusable("statut 500")),
        (httpx.Response(200, text="not json"), PairingUnusable("pas un objet JSON")),
        (httpx.Response(200, json=[1]), PairingUnusable("pas un objet JSON")),
        (
            httpx.Response(200, json=answer(machine_id=None)),
            PairingUnusable("identifiant de machine"),
        ),
        (
            httpx.Response(200, json=answer(machine_id="a/b")),
            PairingUnusable("identifiant de machine"),
        ),
        (httpx.Response(200, json=answer(api_key=7)), PairingUnusable("cle")),
        (httpx.Response(200, json=answer(api_key="anh1.x")), PairingUnusable("cle")),
        (httpx.Response(200, json=answer(key_prefix=None)), PairingUnusable("prefixe de la cle")),
        (
            httpx.Response(200, json=answer(key_prefix="anh1.99")),
            PairingUnusable("prefixe de la cle"),
        ),
        (
            httpx.Response(200, json=answer(convex_url=None)),
            PairingUnusable("adresse : pas celle qui a repondu"),
        ),
        (
            httpx.Response(200, json=answer(convex_url="https://other.convex.site")),
            PairingUnusable("adresse : pas celle qui a repondu"),
        ),
        (
            httpx.Response(200, json=answer(convex_url="http://deploiement.convex.site")),
            PairingUnusable("adresse : pas celle qui a repondu"),
        ),
    ],
)
async def test_every_other_answer_is_an_error_and_nothing_of_it_is_taken(
    response: httpx.Response, error: object
) -> None:
    async with client_of(lambda _request: response)() as client:
        assert await request_pairing(client, ORIGIN, CODE, SERIAL) == Err(error)


async def test_no_answer_is_said_by_its_kind() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with client_of(handler)() as client:
        assert await request_pairing(client, ORIGIN, CODE, SERIAL) == Err(
            PairingUnreachable("ConnectError")
        )


def test_each_error_is_said_on_the_screen_never_with_the_code() -> None:
    said = [
        describe_pairing_error(PairingRefused()),
        describe_pairing_error(PairingThrottled()),
        describe_pairing_error(PairingUnreachable("ConnectError")),
        describe_pairing_error(PairingUnusable("cle")),
    ]
    assert said[0].startswith("code refuse")
    assert said[1].startswith("trop de demandes")
    assert said[2] == "tableau de bord injoignable (ConnectError) : verifier le reseau et l'adresse"
    assert said[3] == "reponse inattendue du tableau de bord (cle) : rien n'est ecrit"
    with pytest.raises(AssertionError):
        describe_pairing_error(object())  # type: ignore[arg-type]  # an impossible value


def test_the_two_steps_the_console_does_not_do_yet_name_their_tickets() -> None:
    assert enrol_ota().id == "ota"
    assert "ANH-169" in enrol_ota().detail
    assert console_network().id == "network"
    assert "ANH-151" in console_network().detail


# --- the screen's side --------------------------------------------------------------------


def desk(
    tmp_path: Path,
    handler: Handler | None = None,
    *,
    address: str | None = ORIGIN,
    machine_id: str | None = None,
    serial: Path | None = None,
) -> PairingDesk:
    return PairingDesk(
        PairingSetup(
            path=tmp_path / "data" / "machine.env",
            serial_path=serial_file(tmp_path) if serial is None else serial,
            address=address,
            machine_id=machine_id,
            client=client_of(handler or (lambda _request: httpx.Response(200, json=answer()))),
        )
    )


def test_the_setup_takes_the_env_s_address_and_machine_when_it_sets_them(tmp_path: Path) -> None:
    path = tmp_path / "machine.env"
    assert PairingSetup.of({}, path) == PairingSetup(path=path)
    assert PairingSetup.of(
        {"CONVEX_URL": f"{ORIGIN}/", "RECORD_MACHINE_ID": MACHINE}, path
    ) == PairingSetup(path=path, address=ORIGIN, machine_id=MACHINE)
    assert PairingSetup.of({"CONVEX_URL": "http://localhost:3210"}, path).address is None


async def test_the_default_client_is_one_of_httpx() -> None:
    async with PairingSetup(path=Path("unused")).client() as client:
        assert isinstance(client, httpx.AsyncClient)


async def test_a_pairing_writes_the_file_asks_for_the_restart_then_takes_no_other(
    tmp_path: Path,
) -> None:
    restarts: list[str] = []
    screen = desk(tmp_path)
    screen.bind_restart(lambda: restarts.append("restart"))
    before = screen.view()
    assert (before.serial, before.serial_problem, before.address, before.key_prefix) == (
        SERIAL,
        None,
        ORIGIN,
        None,
    )
    assert [step.id for step in before.steps] == ["ota", "network"]
    done = await screen.pair(f"  {CODE} ", None)
    assert isinstance(done, Ok)
    assert done.value.key_prefix == PREFIX
    assert [step.id for step in done.value.steps] == ["ota", "network"]
    assert restarts == ["restart"]
    assert read_paired(tmp_path / "data" / "machine.env") == Ok(LINK)
    assert screen.view().key_prefix == PREFIX
    assert await screen.pair(CODE, None) == Err(ALREADY_PAIRED)
    assert restarts == ["restart"]


async def test_the_address_typed_is_asked_when_the_env_sets_none(tmp_path: Path) -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json=answer())

    screen = desk(tmp_path, handler, address=None)
    assert screen.view().address is None
    for typed in (None, "", "http://deploiement.convex.site"):
        refused = await screen.pair(CODE, typed)
        assert refused == Err(
            "adresse du tableau de bord attendue : https://<deploiement>.convex.site"
        )
    assert asked == []
    assert isinstance(await screen.pair(CODE, f"{ORIGIN}/"), Ok)
    assert asked == [f"{ORIGIN}/api/machine/pair"]


async def test_no_pairing_without_a_serial_number_or_a_code(tmp_path: Path) -> None:
    blind = desk(tmp_path, serial=tmp_path / "none")
    assert blind.view().serial is None
    assert blind.view().serial_problem == "illisible (FileNotFoundError:ENOENT)"
    assert await blind.pair(CODE, None) == Err(
        "numero de serie du Pi illisible (FileNotFoundError:ENOENT) : aucun appairage possible"
    )
    screen = desk(tmp_path)
    for code in ("", "   ", "x" * 33):
        assert await screen.pair(code, None) == Err(
            "code d'appairage attendu : les 8 caracteres affiches sur le site"
        )


async def test_one_pairing_at_a_time(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(_request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=answer())

    screen = PairingDesk(
        PairingSetup(
            path=tmp_path / "machine.env",
            serial_path=serial_file(tmp_path),
            address=ORIGIN,
            client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow)),
        )
    )
    first = asyncio.create_task(screen.pair(CODE, None))
    await asyncio.sleep(0.01)
    assert await screen.pair(CODE, None) == Err(PAIRING_BUSY)
    release.set()
    assert isinstance(await first, Ok)


async def test_a_refused_code_writes_nothing_and_another_may_follow(tmp_path: Path) -> None:
    answers = iter(
        [
            httpx.Response(401, json={"error": "pairing_refused", "message": "x"}),
            httpx.Response(200, json=answer()),
        ]
    )
    screen = desk(tmp_path, lambda _request: next(answers))
    refused = await screen.pair(CODE, None)
    assert isinstance(refused, Err)
    assert refused.error.startswith("code refuse")
    assert not (tmp_path / "data" / "machine.env").exists()
    assert isinstance(await screen.pair(CODE, None), Ok)


async def test_a_code_of_another_machine_than_the_env_s_is_never_written(tmp_path: Path) -> None:
    screen = desk(tmp_path, machine_id="machine999")
    refused = await screen.pair(CODE, None)
    assert refused == Err(
        "le code est celui de la machine machine123, et cette console est reglee pour "
        "machine999 (RECORD_MACHINE_ID) : rien n'est ecrit ; corriger le .env puis demander un "
        "nouveau code"
    )
    assert not (tmp_path / "data" / "machine.env").exists()
    (tmp_path / "same").mkdir()
    same = desk(tmp_path / "same", machine_id=MACHINE)
    assert isinstance(await same.pair(CODE, None), Ok)


async def test_a_key_the_disk_refuses_is_said_and_nothing_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restarts: list[str] = []
    screen = desk(tmp_path)
    screen.bind_restart(lambda: restarts.append("restart"))

    def full(_path: Path, _link: PairedLink) -> Err[str]:
        return Err("OSError:ENOSPC")

    monkeypatch.setattr(pairing, "write_paired", full)
    assert await screen.pair(CODE, None) == Err(
        "la cle recue n'a pas pu etre ecrite (OSError:ENOSPC) : en demander un nouveau code"
    )
    assert restarts == []
    assert screen.view().key_prefix is None
