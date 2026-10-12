"""ANH-151 EX-3: the console serves TLS off loopback, and refuses clear HTTP there.

Five things are held here, each by a test that fails without it:

* **the rule**: off loopback, no certificate means no console. It refuses to
  start and says why, as it does without a token; on loopback clear HTTP stays;
* **the origin**: a console that serves its certificate accepts ``https``
  origins and no ``http`` one on its telemetry socket;
* **the files**: a certificate the console cannot serve is a configuration
  error, named file by file, said before anything of the machine is opened;
* **the wire**: the page, the API and the telemetry socket are really served
  over TLS, with the token and the origin checked as before, to a client that
  trusts the console's authority and no other; a client that speaks clear
  HTTP to that port gets no answer at all;
* **the dates**: an expired certificate is served like any other. The client
  is the one that refuses it.

Every server here listens on this machine's loopback, on a socket handed to
it, while the console it serves is configured for an address of a network
(:data:`~tests.tls_support.AP_ADDRESS`): nothing is ever reachable from one.
The certificates are made when the test runs (:mod:`tests.tls_support`).

What a session does under TLS clients that stall is in
``tests/test_console_tls_load.py``; the script that makes the certificate has
``tests/test_console_tls_script.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import ssl
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Final

import anyio
import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidMessage, InvalidStatus
from websockets.typing import Origin

from src import local_panel
from src.clock import ManualClock
from src.health_probe import healthy
from src.local_config import ConfigProblem, LocalConfig, load_local_config
from src.local_panel import (
    EXIT_CONFIG,
    EXIT_OK,
    UvicornRunner,
    build_panel,
    describe_config,
    main,
    tls_problem,
)
from src.result import Err, Ok
from src.web.app import build_server, create_app, serve, tls_context_factory
from src.web.deps import (
    MIN_TOKEN_LENGTH,
    TOKEN_HEADER,
    Services,
    TlsFiles,
    WebConfig,
    clear_http_refusal,
    origin_of,
    token_refusal,
)
from src.web.tls import server_context
from src.web.ws import TELEMETRY_PATH
from tests.test_web_api import GEOMETRY, TOKEN, Rig, StubPorts, build_rig, free_port, make_snapshot
from tests.tls_support import (
    AP_ADDRESS,
    AP_NAME,
    Material,
    client_context,
    issue_dated,
    loopback_listener,
    make_material,
    new_key,
    openssl,
    port_of,
    requires_openssl,
    unverified,
)

SIM_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
    "UI_PORT": "8090",
}

NOWHERE: Final[TlsFiles] = TlsFiles(
    certificate_path=Path("/etc/anheart/tls/console.crt"),
    key_path=Path("/etc/anheart/tls/console.key"),
)
"""Two paths for the tests of the rules, which open neither."""

TLS_PROBLEM: Final[str] = "UI_HOST/UI_TLS_CERT/UI_TLS_KEY"
FILES_PROBLEM: Final[str] = "UI_TLS_CERT/UI_TLS_KEY"


def _problems(env: Mapping[str, str]) -> dict[str, str]:
    loaded = load_local_config(env)
    assert isinstance(loaded, Err), f"accepted a bad configuration: {loaded!r}"
    return {problem.key: problem.detail for problem in loaded.error}


def _config(env: Mapping[str, str]) -> LocalConfig:
    loaded = load_local_config(env)
    assert isinstance(loaded, Ok), f"unexpected problems: {loaded!r}"
    return loaded.value


# =========================================================================
# The rule: off loopback, TLS or nothing
# =========================================================================


def test_ex3_serving_off_loopback_without_a_certificate_is_refused() -> None:
    """With a good token and no certificate: the token would cross the network in clear."""
    with pytest.raises(ValueError, match="in clear HTTP") as refused:
        WebConfig(host=AP_ADDRESS, port=8080, token=TOKEN)
    assert "served over TLS or not at all" in str(refused.value)
    assert "make_console_tls.sh" in str(refused.value), "the refusal says what makes one"


def test_ex3_serving_off_loopback_with_a_certificate_is_allowed() -> None:
    config = WebConfig(host=AP_ADDRESS, port=8080, token=TOKEN, tls=NOWHERE)
    assert not config.is_loopback
    assert config.scheme == "https"


def test_ex3_a_certificate_does_not_stand_in_for_the_token() -> None:
    """TLS says who the console is. It says nothing of who is asking."""
    with pytest.raises(ValueError, match="without a token"):
        WebConfig(host=AP_ADDRESS, port=8080, token=None, tls=NOWHERE)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_ex3_loopback_stays_in_clear_without_a_certificate(host: str) -> None:
    """Development, the simulator, the tests: nothing leaves the machine."""
    config = WebConfig(host=host, port=8080)
    assert config.tls is None
    assert config.scheme == "http"


def test_ex3_the_two_refusals_are_told_apart() -> None:
    assert token_refusal("127.0.0.1", None) is None
    assert clear_http_refusal("127.0.0.1", None) is None
    assert clear_http_refusal(AP_ADDRESS, NOWHERE) is None
    assert token_refusal(AP_ADDRESS, TOKEN) is None
    missing = token_refusal(AP_ADDRESS, None)
    assert missing is not None
    assert "without a token" in missing
    short = token_refusal(AP_ADDRESS, "x" * (MIN_TOKEN_LENGTH - 1))
    assert short is not None
    assert "characters are required" in short
    clear = clear_http_refusal("0.0.0.0", None)  # noqa: S104  # the refusal is the point
    assert clear is not None
    assert "'0.0.0.0'" in clear


# =========================================================================
# The origin: https, and no longer http, where the console serves TLS
# =========================================================================


def test_ex3_off_loopback_the_socket_accepts_the_https_origin_and_not_the_http_one() -> None:
    config = WebConfig(host=AP_ADDRESS, port=8080, token=TOKEN, tls=NOWHERE)
    assert config.origin_allowed(f"https://{AP_ADDRESS}:8080")
    assert not config.origin_allowed(f"http://{AP_ADDRESS}:8080")
    assert all(origin.startswith("https://") for origin in config.allowed_origins)


def test_ex3_the_names_of_the_certificate_are_origins_too() -> None:
    """A console that listens on every address is opened at the access point's."""
    config = WebConfig(
        host="0.0.0.0",  # noqa: S104  # a configuration, never bound
        port=8443,
        token=TOKEN,
        tls=NOWHERE,
        names=(AP_ADDRESS, AP_NAME),
    )
    assert config.origin_allowed(f"https://{AP_ADDRESS}:8443")
    assert config.origin_allowed(f"https://{AP_NAME}:8443")
    assert not config.origin_allowed(f"http://{AP_NAME}:8443")
    assert not config.origin_allowed(f"https://{AP_NAME}:8444")
    assert not config.origin_allowed("https://clinic-wifi-printer.local:8443")


def test_ex3_a_loopback_console_given_a_certificate_speaks_tls_and_says_so() -> None:
    config = WebConfig(host="127.0.0.1", port=8080, tls=NOWHERE)
    assert config.scheme == "https"
    assert config.origin_allowed("https://127.0.0.1:8080")
    assert config.origin_allowed("https://localhost:8080")
    assert not config.origin_allowed("http://127.0.0.1:8080")


def test_ex3_a_loopback_console_in_clear_keeps_the_origins_it_had() -> None:
    config = WebConfig(host="127.0.0.1", port=8080)
    assert config.allowed_origins == (
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8080",
        "http://localhost:8080",
        "https://localhost:8080",
    )


def test_ex3_an_origin_leaves_out_the_port_that_is_its_schemes_own() -> None:
    """A page opened at ``https://10.42.0.1/`` never says ``:443``."""
    assert origin_of("https", AP_ADDRESS, 443) == f"https://{AP_ADDRESS}"
    assert origin_of("https", AP_ADDRESS, 8443) == f"https://{AP_ADDRESS}:8443"
    assert origin_of("http", "127.0.0.1", 80) == "http://127.0.0.1"
    assert origin_of("http", "127.0.0.1", 443) == "http://127.0.0.1:443"
    config = WebConfig(host=AP_ADDRESS, port=443, token=TOKEN, tls=NOWHERE)
    assert config.allowed_origins == (f"https://{AP_ADDRESS}",)


def test_ex3_an_extra_origin_in_clear_is_refused_on_a_console_that_serves_tls() -> None:
    with pytest.raises(ValueError, match="only an https origin") as refused:
        WebConfig(
            host=AP_ADDRESS,
            port=8080,
            token=TOKEN,
            tls=NOWHERE,
            extra_origins=("https://tablet.internal", "http://tablet.internal"),
        )
    assert "'http://tablet.internal'" in str(refused.value)


# =========================================================================
# The configuration keys
# =========================================================================


def test_ex3_no_key_no_tls() -> None:
    web = _config(SIM_ENV).web
    assert web.tls is None
    assert web.names == ()
    blank = _config({**SIM_ENV, "UI_TLS_CERT": " ", "UI_TLS_KEY": "", "UI_TLS_NAMES": " , "}).web
    assert blank.tls is None
    assert blank.names == ()


def test_ex3_the_two_paths_are_read_and_not_opened() -> None:
    web = _config(
        {
            **SIM_ENV,
            "UI_HOST": "0.0.0.0",  # noqa: S104  # a configuration, never bound
            "UI_TOKEN": TOKEN,
            "UI_TLS_CERT": "/etc/anheart/tls/console.crt",
            "UI_TLS_KEY": "data/tls/console.key",
            "UI_TLS_NAMES": f" {AP_ADDRESS}, Anheart.Internal ,,{AP_ADDRESS}",
        }
    ).web
    assert web.tls == TlsFiles(
        certificate_path=Path("/etc/anheart/tls/console.crt"), key_path=Path("data/tls/console.key")
    )
    assert web.names == (AP_ADDRESS, AP_NAME), "trimmed, lower case, each once"
    assert web.origin_allowed(f"https://{AP_NAME}:8090")


def test_ex3_off_loopback_without_a_certificate_the_configuration_is_refused() -> None:
    problems = _problems({**SIM_ENV, "UI_HOST": AP_ADDRESS, "UI_TOKEN": TOKEN})
    assert set(problems) == {TLS_PROBLEM}
    assert "in clear HTTP" in problems[TLS_PROBLEM]


def test_ex3_a_missing_token_and_a_missing_certificate_are_said_at_once() -> None:
    """An operator fixes a .env in one go, not one line per restart."""
    problems = _problems({**SIM_ENV, "UI_HOST": AP_ADDRESS, "UI_TLS_NAMES": "not a name"})
    assert set(problems) == {"UI_HOST/UI_PORT/UI_TOKEN", TLS_PROBLEM, "UI_TLS_NAMES"}
    assert "without a token" in problems["UI_HOST/UI_PORT/UI_TOKEN"]


@pytest.mark.parametrize(
    ("given", "missing"), [("UI_TLS_CERT", "UI_TLS_KEY"), ("UI_TLS_KEY", "UI_TLS_CERT")]
)
def test_ex3_a_certificate_without_its_key_or_a_key_without_its_certificate_is_refused(
    given: str, missing: str
) -> None:
    problems = _problems({**SIM_ENV, given: "/etc/anheart/tls/one-of-the-two"})
    assert set(problems) == {missing}
    assert problems[missing] == (
        f"obligatoire avec {given} : le certificat de la console et sa cle privee vont ensemble"
    )
    # Off loopback, half a certificate is said once, as what it is.
    off_loopback = _problems(
        {**SIM_ENV, given: "/etc/anheart/tls/x", "UI_HOST": AP_ADDRESS, "UI_TOKEN": TOKEN}
    )
    assert set(off_loopback) == {missing}


@pytest.mark.parametrize(
    "name", ["https://10.42.0.1", "10.42.0.1:8080", "fd00::1", "tablet_1", "-x.internal", "a b"]
)
def test_ex3_a_name_that_is_not_one_is_refused(name: str) -> None:
    problems = _problems(
        {**SIM_ENV, "UI_TLS_CERT": "/c", "UI_TLS_KEY": "/k", "UI_TLS_NAMES": f"{AP_ADDRESS},{name}"}
    )
    assert set(problems) == {"UI_TLS_NAMES"}
    assert problems["UI_TLS_NAMES"].startswith(f"{name.lower()!r} : attendu une adresse IPv4")


def test_ex3_names_without_a_certificate_are_refused_rather_than_ignored() -> None:
    problems = _problems({**SIM_ENV, "UI_TLS_NAMES": AP_ADDRESS})
    assert set(problems) == {"UI_TLS_NAMES"}
    assert problems["UI_TLS_NAMES"].startswith("sans effet sans certificat")


def test_ex3_a_port_out_of_range_is_still_a_problem_of_the_bind() -> None:
    problems = _problems({**SIM_ENV, "UI_PORT": "70000"})
    assert set(problems) == {"UI_HOST/UI_PORT/UI_TOKEN"}
    assert "port must be" in problems["UI_HOST/UI_PORT/UI_TOKEN"]
    # Each problem once: half a certificate is not also blamed on the names given with it.
    both = _problems(
        {**SIM_ENV, "UI_PORT": "http", "UI_TLS_CERT": "/etc/anheart/tls/c", "UI_TLS_NAMES": AP_NAME}
    )
    assert set(both) == {"UI_PORT", "UI_TLS_KEY"}


def test_ex3_the_banner_says_https_when_the_console_serves_its_certificate() -> None:
    clear = describe_config(_config(SIM_ENV))
    assert "Console du banc sur http://127.0.0.1:8090/" in clear
    served = describe_config(_config({**SIM_ENV, "UI_TLS_CERT": "/c", "UI_TLS_KEY": "/k"}))
    assert "Console du banc sur https://127.0.0.1:8090/" in served


# =========================================================================
# The files: what the console can serve, and what it says of the rest
# =========================================================================


@pytest.fixture
def material(tmp_path: Path) -> Material:
    """An authority and a console certificate, made by the real script."""
    return make_material(tmp_path / "tls", AP_ADDRESS, AP_NAME)


def refusal(files: TlsFiles) -> str:
    loaded = server_context(files)
    assert isinstance(loaded, Err), "a certificate that cannot be served was accepted"
    return loaded.error.detail


@requires_openssl
def test_ex3_the_scripts_certificate_is_one_the_console_can_serve(material: Material) -> None:
    loaded = server_context(material.files)
    assert isinstance(loaded, Ok)
    context = loaded.value
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert context.verify_mode is ssl.CERT_NONE, "the console asks no certificate of a tablet"


@requires_openssl
@pytest.mark.parametrize("role", ["certificate", "private key"])
def test_ex3_a_file_that_is_not_there_is_named(material: Material, role: str) -> None:
    gone = material.directory / "not-there.pem"
    files = (
        TlsFiles(certificate_path=gone, key_path=material.key)
        if role == "certificate"
        else TlsFiles(certificate_path=material.certificate, key_path=gone)
    )
    said = refusal(files)
    assert said == f"the {role} {gone} is not a file the console can see"
    # A directory, which is what a mount that went wrong leaves, is not one either.
    of_a_directory = refusal(TlsFiles(certificate_path=material.directory, key_path=material.key))
    assert "is not a file the console can see" in of_a_directory


@requires_openssl
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_ex3_a_key_the_console_may_not_read_is_named(material: Material) -> None:
    material.key.chmod(0)
    try:
        said = refusal(material.files)
    finally:
        material.key.chmod(0o600)
    assert said == f"the private key {material.key} cannot be read: Permission denied"


@requires_openssl
def test_ex3_two_paths_given_the_wrong_way_round_are_told_apart(material: Material) -> None:
    swapped = refusal(TlsFiles(certificate_path=material.key, key_path=material.certificate))
    assert swapped == f"{material.key} holds no PEM certificate"
    twice = refusal(TlsFiles(certificate_path=material.certificate, key_path=material.certificate))
    assert twice == f"{material.certificate} holds no PEM private key"


@requires_openssl
def test_ex3_a_key_that_is_not_the_certificates_is_refused(
    material: Material, tmp_path: Path
) -> None:
    other = new_key(tmp_path / "other.key")
    said = refusal(TlsFiles(certificate_path=material.certificate, key_path=other))
    assert said == (
        f"the private key {other} is not the one of the certificate {material.certificate}"
    )


@requires_openssl
def test_ex3_a_key_behind_a_passphrase_is_refused_without_asking_anybody(
    material: Material, tmp_path: Path
) -> None:
    """The console starts unattended: the TLS library must never ask the terminal."""
    phrase = tmp_path / "phrase"
    phrase.write_text("only-for-this-test\n", encoding="utf-8")
    locked = tmp_path / "locked.key"
    openssl(
        *("pkey", "-in", str(material.key), "-aes256"),
        *("-passout", f"file:{phrase}", "-out", str(locked)),
    )
    said = refusal(TlsFiles(certificate_path=material.certificate, key_path=locked))
    assert said == (
        f"the private key {locked} is protected by a passphrase: the console starts "
        "unattended and has nobody to ask"
    )


@requires_openssl
def test_ex3_a_damaged_file_is_refused_in_the_tls_librarys_words(
    material: Material, tmp_path: Path
) -> None:
    damaged = tmp_path / "damaged.crt"
    damaged.write_text(
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="utf-8"
    )
    said = refusal(TlsFiles(certificate_path=damaged, key_path=material.key))
    assert said.startswith(f"the TLS library refuses {damaged} with {material.key}: ")


@requires_openssl
def test_ex3_no_refusal_quotes_what_a_file_holds(material: Material, tmp_path: Path) -> None:
    """Paths and reasons only: a key that is refused is still a key."""
    secret = "".join(material.key.read_text(encoding="utf-8").splitlines()[1:-1])
    other = new_key(tmp_path / "other.key")
    for files in (
        TlsFiles(certificate_path=material.key, key_path=material.certificate),
        TlsFiles(certificate_path=material.certificate, key_path=other),
        TlsFiles(certificate_path=material.key, key_path=material.key),
    ):
        said = refusal(files)
        assert secret[:24] not in said
        assert "PRIVATE KEY" not in said


# =========================================================================
# The entry point: a certificate it cannot serve is a configuration error
# =========================================================================


def _never_run(monkeypatch: pytest.MonkeyPatch) -> list[LocalConfig]:
    """Replace ``run_console``: a console that gets that far was not refused."""
    ran: list[LocalConfig] = []

    async def fake(config: LocalConfig, *, stopped: object, notify_socket: str | None) -> int:
        del stopped, notify_socket
        ran.append(config)
        return EXIT_OK

    monkeypatch.setattr(local_panel, "run_console", fake)
    return ran


def test_ex3_main_refuses_to_start_off_loopback_without_a_certificate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ran = _never_run(monkeypatch)
    code = main({**SIM_ENV, "UI_HOST": AP_ADDRESS, "UI_TOKEN": TOKEN})
    assert code == EXIT_CONFIG, "the exit code the service does not restart on"
    assert ran == []
    assert f"configuration: {TLS_PROBLEM}: refusing to serve the operator interface" in caplog.text
    assert "in clear HTTP" in caplog.text


def test_ex3_main_refuses_a_certificate_it_cannot_serve_before_building_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad certificate file at startup: said once, exit 2, nothing opened."""
    ran = _never_run(monkeypatch)
    records = tmp_path / "records"
    gone = tmp_path / "console.crt"
    code = main(
        {
            **SIM_ENV,
            "RECORD_ROOT": str(records),
            "UI_TLS_CERT": str(gone),
            "UI_TLS_KEY": str(tmp_path / "console.key"),
        }
    )
    assert code == EXIT_CONFIG
    assert ran == []
    assert not records.exists(), "nothing of the console was built"
    assert (
        f"configuration: {FILES_PROBLEM}: the certificate {gone} is not a file the console can see"
    ) in caplog.text
    assert "Console du banc sur" not in caplog.text, "no banner for a console that did not start"


@requires_openssl
def test_ex3_main_starts_a_console_whose_certificate_can_be_served(
    material: Material, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ran = _never_run(monkeypatch)
    env = {
        **SIM_ENV,
        "UI_HOST": AP_ADDRESS,
        "UI_TOKEN": TOKEN,
        "UI_TLS_CERT": str(material.certificate),
        "UI_TLS_KEY": str(material.key),
        "UI_TLS_NAMES": AP_NAME,
    }
    code = main(env)
    assert code == EXIT_OK
    assert [config.web.tls for config in ran] == [material.files]
    assert f"Console du banc sur https://{AP_ADDRESS}:8090/" in caplog.text
    assert TOKEN not in caplog.text


def test_ex3_a_console_without_a_certificate_has_no_tls_problem() -> None:
    none = tls_problem(_config(SIM_ENV))
    assert none is None
    unserved = tls_problem(_config({**SIM_ENV, "UI_TLS_CERT": "/c", "UI_TLS_KEY": "/k"}))
    assert unserved == ConfigProblem(
        FILES_PROBLEM, "the certificate /c is not a file the console can see"
    )


def test_ex3_a_certificate_that_can_be_served_is_no_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry point asks one thing of the certificate: can it be served.

    With a stand-in for the answer, so that this branch of the entry point is
    exercised on a machine that has no ``openssl`` to make a certificate with.
    ``test_ex3_main_starts_a_console_whose_certificate_can_be_served`` does it
    with real files.
    """

    def servable(_files: TlsFiles) -> Ok[ssl.SSLContext]:
        return Ok(client_context())

    monkeypatch.setattr(local_panel, "server_context", servable)
    none = tls_problem(_config({**SIM_ENV, "UI_TLS_CERT": "/c", "UI_TLS_KEY": "/k"}))
    assert none is None


# =========================================================================
# The server: built with the certificate in hand, or not built
# =========================================================================


def services_of(rig: Rig) -> Services:
    return Services(
        clock=rig.clock,
        surface=rig.surface,
        hub=rig.hub,
        supervisor=rig.supervisor,
        store=rig.store,
        ports=StubPorts(),
        geometry=GEOMETRY,
    )


def test_ex3_a_console_in_clear_is_built_without_tls(tmp_path: Path) -> None:
    rig = build_rig(tmp_path)
    factory = tls_context_factory(rig.config)
    assert factory is None
    server = build_server(rig.app, rig.config)
    assert not server.config.is_ssl


def test_ex3_building_a_server_on_a_certificate_it_cannot_serve_refuses_rather_than_serve_in_clear(
    tmp_path: Path,
) -> None:
    rig = build_rig(tmp_path)
    config = WebConfig(host=AP_ADDRESS, port=8443, token=TOKEN, tls=NOWHERE)
    with pytest.raises(RuntimeError, match="refusing to serve: the certificate"):
        build_server(create_app(services=services_of(rig), config=config), config)


@requires_openssl
def test_ex3_the_certificate_is_read_when_the_server_is_built_and_never_again(
    material: Material, tmp_path: Path
) -> None:
    """Replacing or removing the files under a running console changes nothing."""
    rig = build_rig(tmp_path)
    config = WebConfig(host=AP_ADDRESS, port=8443, token=TOKEN, tls=material.files)
    server = build_server(create_app(services=services_of(rig), config=config), config)
    assert server.config.is_ssl
    material.certificate.unlink()
    material.key.unlink()
    server.config.load()  # what uvicorn does, later, from the task that serves
    assert isinstance(server.config.ssl, ssl.SSLContext)


@requires_openssl
async def test_ex3_the_production_runner_serves_tls_on_the_address_it_is_given(
    material: Material, tmp_path: Path
) -> None:
    """The console's own runner, bound for real: on loopback, where a certificate is allowed too."""
    env = {
        **SIM_ENV,
        "UI_PORT": str(free_port()),
        "UI_TLS_CERT": str(material.certificate),
        "UI_TLS_KEY": str(material.key),
    }
    config = _config(env)
    panel = build_panel(config, clock=ManualClock(), profiles_path=tmp_path / "p.json")
    runner = UvicornRunner(panel.services, config)
    serving = asyncio.create_task(runner.serve())
    try:
        async with httpx.AsyncClient(
            verify=material.client(), base_url=f"https://127.0.0.1:{config.web.port}"
        ) as client:
            answered = await retried(client, "/healthz")
    finally:
        runner.request_exit()
        await asyncio.wait_for(serving, 10.0)
        await panel.close()
    assert answered.status_code == HTTPStatus.OK


async def retried(client: httpx.AsyncClient, path: str) -> httpx.Response:
    """GET ``path`` once the server listens; a refused connection is retried for 10 s."""
    for _ in range(200):
        try:
            return await client.get(path)
        except httpx.ConnectError:
            await asyncio.sleep(0.05)
    raise AssertionError("the server never listened")


# =========================================================================
# The wire: a console configured for the network, served on loopback
# =========================================================================


@dataclass(frozen=True, slots=True)
class Served:
    """A console over TLS: configured for the access point, listening on loopback."""

    rig: Rig
    config: WebConfig
    port: int

    @property
    def url(self) -> str:
        """Where the test reaches it. ``127.0.0.1`` is one of the certificate's names."""
        return f"https://127.0.0.1:{self.port}"

    def origin(self, scheme: str = "https", host: str = AP_ADDRESS) -> str:
        """What a browser that opened the page at ``host`` says it comes from."""
        return f"{scheme}://{host}:{self.port}"


@contextlib.asynccontextmanager
async def serving(rig: Rig, files: TlsFiles) -> AsyncGenerator[Served]:
    listener = loopback_listener()
    port = port_of(listener)
    config = WebConfig(host=AP_ADDRESS, port=port, token=TOKEN, tls=files, names=(AP_NAME,))
    server = build_server(create_app(services=services_of(rig), config=config), config)
    task = asyncio.create_task(serve(server, sockets=[listener]))
    try:
        for _ in range(500):
            if server.started or task.done():
                break
            await asyncio.sleep(0.01)
        assert server.started, "the server never started"
        yield Served(rig=rig, config=config, port=port)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15.0)


@pytest.fixture
async def served(tmp_path: Path) -> AsyncIterator[Served]:
    made = make_material(tmp_path / "tls", AP_ADDRESS, AP_NAME)
    async with serving(build_rig(tmp_path), made.files) as console:
        yield console


def trusting(tmp_path: Path) -> ssl.SSLContext:
    """The client of a tablet on which this console's authority was installed."""
    return Material(tmp_path / "tls").client()


async def test_installed_health_probe_verifies_the_console_authority(
    served: Served, tmp_path: Path
) -> None:
    env = {
        "UI_HOST": "127.0.0.1",
        "UI_PORT": str(served.port),
        "UI_TLS_CERT": str(tmp_path / "tls" / "console.crt"),
    }
    assert await anyio.to_thread.run_sync(healthy, env)
    other = make_material(tmp_path / "other", "localhost")
    env["UI_TLS_CERT"] = str(other.certificate)
    assert not await anyio.to_thread.run_sync(healthy, env)


async def test_installed_health_probe_does_not_fall_back_to_clear_http(served: Served) -> None:
    assert not await anyio.to_thread.run_sync(
        healthy, {"UI_HOST": "127.0.0.1", "UI_PORT": str(served.port)}
    )


@pytest.mark.parametrize("host", ["invalid host", "127.0.0.1/path", "[::1]"])
def test_installed_health_probe_refuses_invalid_host(host: str) -> None:
    assert not healthy({"UI_HOST": host})


def handshake(port: int, client: ssl.SSLContext, name: str | None) -> str:
    """Open a TLS connection to the console as ``name``; the protocol version agreed."""
    with (
        socket.create_connection(("127.0.0.1", port), timeout=10.0) as link,
        client.wrap_socket(link, server_hostname=name) as secured,
    ):
        version = secured.version()
    assert version is not None
    return version


@requires_openssl
async def test_ex3_the_page_and_the_api_are_served_over_tls_with_the_token(
    served: Served, tmp_path: Path
) -> None:
    async with httpx.AsyncClient(verify=trusting(tmp_path), base_url=served.url) as client:
        page = await client.get("/")
        script = await client.get("/app.js")
        health = await client.get("/healthz")
        anonymous = await client.get("/api/status")
        wrong = await client.get("/api/status", headers={TOKEN_HEADER: TOKEN[:-1]})
        status = await client.get("/api/status", headers={TOKEN_HEADER: TOKEN})
        stop = await client.post("/api/session/estop", headers={TOKEN_HEADER: TOKEN}, json={})
    assert page.status_code == HTTPStatus.OK
    assert "<title>" in page.text
    assert script.status_code == HTTPStatus.OK
    assert health.status_code == HTTPStatus.OK
    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
    assert wrong.status_code == HTTPStatus.UNAUTHORIZED
    assert status.status_code == HTTPStatus.OK
    assert f'"host":"{AP_ADDRESS}"' in status.text
    assert '"loopback":false' in status.text
    assert stop.status_code == HTTPStatus.OK
    assert served.rig.surface.estop_latched, "a command sent over TLS reached the console"
    for response in (page, health, status):
        assert "strict-transport-security" not in response.headers, (
            "no HSTS: it would take from an operator the means to pass a warning "
            "on the day the certificate has expired and the machine must be stopped"
        )


@requires_openssl
@pytest.mark.parametrize("name", [AP_ADDRESS, AP_NAME, "127.0.0.1", "localhost"])
async def test_ex3_a_client_that_trusts_the_authority_accepts_the_console_under_each_of_its_names(
    served: Served, tmp_path: Path, name: str
) -> None:
    version = await asyncio.to_thread(handshake, served.port, trusting(tmp_path), name)
    assert version in {"TLSv1.2", "TLSv1.3"}


@requires_openssl
async def test_ex3_the_console_is_refused_under_a_name_its_certificate_does_not_carry(
    served: Served, tmp_path: Path
) -> None:
    with pytest.raises(ssl.SSLCertVerificationError) as refused:
        await asyncio.to_thread(handshake, served.port, trusting(tmp_path), "10.42.0.2")
    assert "not valid for '10.42.0.2'" in refused.value.verify_message


@requires_openssl
async def test_ex3_a_client_that_trusts_another_authority_refuses_the_console(
    served: Served, tmp_path: Path
) -> None:
    """Another Pi's authority, made the same way for the same names, recognises nothing here."""
    other = make_material(tmp_path / "another-pi", AP_ADDRESS, AP_NAME)
    with pytest.raises(ssl.SSLCertVerificationError) as refused:
        await asyncio.to_thread(handshake, served.port, other.client(), AP_ADDRESS)
    assert refused.value.verify_message == "unable to get local issuer certificate"
    # And a tablet on which nothing was installed: the authorities it came with.
    untouched = client_context()
    untouched.load_default_certs()
    with pytest.raises(ssl.SSLCertVerificationError):
        await asyncio.to_thread(handshake, served.port, untouched, AP_ADDRESS)


def speak_clear(port: int) -> bytes:
    """Everything the console's port answers to a clear request that carries the token."""
    request = (
        f"GET /api/status HTTP/1.1\r\nHost: {AP_ADDRESS}:{port}\r\n{TOKEN_HEADER}: {TOKEN}\r\n\r\n"
    )
    answer = b""
    with socket.create_connection(("127.0.0.1", port), timeout=10.0) as link:
        link.sendall(request.encode("ascii"))
        with contextlib.suppress(ConnectionError):
            while chunk := link.recv(4096):
                answer += chunk
    return answer


async def opens_in_clear(port: int) -> bool:
    """Whether a WebSocket handshake in clear, token included, gets through to that port."""
    try:
        async with connect(f"ws://127.0.0.1:{port}{TELEMETRY_PATH}?token={TOKEN}", proxy=None):
            return True
    except (InvalidMessage, OSError):
        return False


TLS_ALERT: Final[int] = 0x15
"""First byte of a TLS alert record: the one thing a TLS library may say to a
peer that did not speak TLS, in seven bytes that carry nothing of the console."""


@requires_openssl
async def test_ex3_clear_http_gets_no_answer_from_the_console_and_no_redirect(
    served: Served, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing useful comes back: no page, no status, no ``Location`` to follow in clear.

    The connection is closed. Whether the TLS library first says "alert" in
    its own framing is left to it; an HTTP answer of any kind is what must
    never be there.
    """
    answer = await asyncio.to_thread(speak_clear, served.port)
    assert answer == b"" or (len(answer) == 7 and answer[0] == TLS_ALERT), answer
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{served.port}") as client:
        with pytest.raises(httpx.TransportError):
            await client.get("/healthz")
    opened = await opens_in_clear(served.port)
    assert not opened, "a clear socket was opened on the TLS port"
    # The console goes on serving, and wrote nothing for a peer it never heard as one.
    assert served.rig.hub.client_count == 0
    said = [
        f"{record.name}: {record.getMessage()}"
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert said == [], "a peer that never spoke TLS cannot fill the journal"
    version = await asyncio.to_thread(handshake, served.port, unverified(), None)
    assert version in {"TLSv1.2", "TLSv1.3"}


async def first_frame(
    served: Served, client: ssl.SSLContext, *, origin: str | None, token: str | None
) -> str:
    """Open the telemetry socket over TLS; its first frame. Raises what the handshake raises."""
    query = "" if token is None else f"?token={token}"
    async with connect(
        f"wss://127.0.0.1:{served.port}{TELEMETRY_PATH}{query}",
        ssl=client,
        origin=None if origin is None else Origin(origin),
        proxy=None,
        open_timeout=10.0,
    ) as link:
        frame = await asyncio.wait_for(link.recv(), 10.0)
    assert isinstance(frame, str)
    return frame


@requires_openssl
async def test_ex3_the_telemetry_socket_streams_over_tls_to_the_page_of_the_console(
    served: Served, tmp_path: Path
) -> None:
    served.rig.surface.publish(make_snapshot())
    client = trusting(tmp_path)
    for origin in (served.origin(), served.origin(host=AP_NAME), None):
        frame = await first_frame(served, client, origin=origin, token=TOKEN)
        assert '"kind":"snapshot"' in frame, origin
    await asyncio.sleep(0.05)
    assert served.rig.hub.client_count == 0, "each client is released when its socket goes"


@dataclass(frozen=True, slots=True)
class Knock:
    """One handshake the socket must refuse, and what the console's journal says of it."""

    origin: str
    """``http`` or ``https`` for the console's own address, or ``foreign``."""

    token: str | None
    logged: str


@requires_openssl
@pytest.mark.parametrize(
    "knock",
    [
        Knock("http", TOKEN, "refused telemetry socket from origin 'http://10.42.0.1:"),
        Knock("foreign", TOKEN, "refused telemetry socket from origin 'https://clinic-wifi-"),
        Knock("https", None, "refused telemetry socket: bad or missing token"),
        Knock("https", TOKEN[:-1], "refused telemetry socket: bad or missing token"),
    ],
    ids=["http-origin", "foreign-origin", "no-token", "wrong-token"],
)
async def test_ex3_the_socket_keeps_its_two_checks_under_tls(
    served: Served, tmp_path: Path, caplog: pytest.LogCaptureFixture, knock: Knock
) -> None:
    """The ``http`` origin of the console's own address is refused like a stranger's."""
    served.rig.surface.publish(make_snapshot())
    sent = (
        f"https://clinic-wifi-printer.local:{served.port}"
        if knock.origin == "foreign"
        else served.origin(scheme=knock.origin)
    )
    with pytest.raises(InvalidStatus) as refused:
        await first_frame(served, trusting(tmp_path), origin=sent, token=knock.token)
    assert refused.value.response.status_code == HTTPStatus.FORBIDDEN
    assert knock.logged in caplog.text
    assert served.rig.hub.client_count == 0


# =========================================================================
# The dates: an expired certificate is served, and the client refuses it
# =========================================================================


@requires_openssl
async def test_ex3_an_expired_certificate_is_served_and_only_the_client_refuses_it(
    tmp_path: Path,
) -> None:
    """The console does not judge its own dates: it would bet a reachable machine on its clock.

    A client that verifies, which is a tablet opening a NEW connection, is
    told the certificate has expired. One that does not judge it, which
    stands for a connection opened before the expiry and for an operator who
    passed the browser's warning, is served and commands the console.
    """
    now = datetime.now(UTC)
    expired = issue_dated(
        tmp_path / "expired", not_before=now - timedelta(days=30), not_after=now - timedelta(days=1)
    )
    loaded = server_context(expired.files)
    assert isinstance(loaded, Ok), "an expired certificate is not refused"
    rig = build_rig(tmp_path)
    async with serving(rig, expired.files) as console:
        with pytest.raises(ssl.SSLCertVerificationError) as refused:
            await asyncio.to_thread(handshake, console.port, expired.client(), AP_ADDRESS)
        async with httpx.AsyncClient(verify=unverified(), base_url=console.url) as client:
            health = await client.get("/healthz")
            stop = await client.post("/api/session/estop", headers={TOKEN_HEADER: TOKEN}, json={})
    assert refused.value.verify_message == "certificate has expired"
    assert health.status_code == HTTPStatus.OK
    assert stop.status_code == HTTPStatus.OK
    assert rig.surface.estop_latched
