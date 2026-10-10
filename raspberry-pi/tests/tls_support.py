"""TLS material for the tests: made when a test runs, in its own directory, never kept.

The repository holds no certificate and no key, and its secret scan fails on
anything that looks like one. So every test that needs a certificate has one
made for it:

* :func:`make_material` runs the real ``scripts/pi/make_console_tls.sh``, the
  script that makes the console's certificate on a Pi: what the console's tests
  serve is what a commissioned machine serves;
* :func:`issue_dated` makes a certificate whose dates the test chooses (one
  that expired yesterday), which the script cannot be asked for. It keeps an
  authority's key in the test's directory for the time it takes, which the
  script never does.

Both need ``bash`` and the ``openssl`` command: :data:`requires_openssl` skips
a test on a machine that has neither.
"""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from src.web.deps import TlsFiles

BASH: Final[str | None] = shutil.which("bash")
OPENSSL: Final[str | None] = shutil.which("openssl")

SCRIPT: Final[Path] = Path(__file__).resolve().parents[1] / "scripts" / "pi" / "make_console_tls.sh"

requires_openssl: Final[pytest.MarkDecorator] = pytest.mark.skipif(
    BASH is None or OPENSSL is None,
    reason="the console's certificate is made by a bash script that calls openssl",
)

AP_ADDRESS: Final[str] = "10.42.0.1"
"""The address the tests give the console's access point. Never bound: the
servers of these tests listen on this machine's loopback and nowhere else."""

AP_NAME: Final[str] = "anheart.internal"

AUTHORITY: Final[str] = "authority.crt"
CERTIFICATE: Final[str] = "console.crt"
KEY: Final[str] = "console.key"

_CA_CONFIGURATION: Final[str] = """\
[req]
distinguished_name = subject
prompt = no

[subject]
CN = {subject}

[ca]
default_ca = test_authority

[test_authority]
database = {work}/index.txt
new_certs_dir = {work}
serial = {work}/serial
default_md = sha256
policy = anything
unique_subject = no

[anything]
commonName = supplied

[authority]
basicConstraints = critical, CA:TRUE

[console]
basicConstraints = critical, CA:FALSE
extendedKeyUsage = serverAuth
subjectAltName = {names}
"""


def client_context() -> ssl.SSLContext:
    """A TLS client of today: TLS 1.2 at least, and it verifies what it is shown.

    It trusts no authority yet: the caller says which.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def unverified() -> ssl.SSLContext:
    """A client that judges nothing of the certificate it is shown.

    It stands for a connection opened before a certificate expired, and for an
    operator who passed the browser's warning: never for a tablet set up right.
    """
    context = client_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


@dataclass(frozen=True, slots=True)
class Material:
    """One authority, and the certificate and key it signed, in a directory."""

    directory: Path

    @property
    def authority(self) -> Path:
        return self.directory / AUTHORITY

    @property
    def certificate(self) -> Path:
        return self.directory / CERTIFICATE

    @property
    def key(self) -> Path:
        return self.directory / KEY

    @property
    def files(self) -> TlsFiles:
        """What the console is configured with."""
        return TlsFiles(certificate_path=self.certificate, key_path=self.key)

    def client(self) -> ssl.SSLContext:
        """A standard TLS client that trusts this authority and no other: a tablet
        on which this authority, and nothing else, was installed."""
        context = client_context()
        context.load_verify_locations(cafile=self.authority)
        return context


def tool(name: str | None) -> str:
    """The path of a command the test was not skipped for lacking."""
    if name is None:
        raise AssertionError("this test needs bash and openssl: mark it requires_openssl")
    return name


def run_script(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real script under bash; never raises on its exit code."""
    return subprocess.run(  # noqa: S603  # the repository's own script, fixed arguments
        [tool(BASH), str(SCRIPT), *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )


def make_material(directory: Path, *names: str) -> Material:
    """An authority and a console certificate made by the real script, for ``names``.

    With no name, the script's own defaults. The loopback names are always
    carried, which is how a test reaches a console it serves on ``127.0.0.1``.
    """
    arguments = ["--dir", str(directory)]
    for name in names:
        arguments += ["--name", name]
    done = run_script(*arguments)
    if done.returncode != 0:
        raise AssertionError(
            f"the script failed ({done.returncode}):\n{done.stdout}\n{done.stderr}"
        )
    return Material(directory)


def openssl(*arguments: str) -> str:
    """Run the ``openssl`` command; what it printed. Fails the test when it fails."""
    done = subprocess.run(  # noqa: S603  # a fixed tool, arguments built here
        [tool(OPENSSL), *arguments], capture_output=True, text=True, check=False, timeout=120
    )
    if done.returncode != 0:
        raise AssertionError(f"openssl {arguments[0]} failed:\n{done.stderr}")
    return done.stdout


def openssl_status(*arguments: str) -> int:
    """Run the ``openssl`` command for its exit code alone."""
    done = subprocess.run(  # noqa: S603  # a fixed tool, arguments built here
        [tool(OPENSSL), *arguments], capture_output=True, text=True, check=False, timeout=120
    )
    return done.returncode


P256: Final[tuple[str, ...]] = (
    *("-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256"),
    # Said out loud: a key written with its curve spelled out in full, which is
    # what some openssl builds do by default, is refused by a TLS client.
    *("-pkeyopt", "ec_param_enc:named_curve"),
)


def new_key(path: Path) -> Path:
    """A fresh private key in ``path``: for a test that needs one that is not the console's."""
    openssl("genpkey", *P256, "-out", str(path))
    return path


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%d%H%M%SZ")


def issue_dated(directory: Path, *, not_before: datetime, not_after: datetime) -> Material:
    """An authority valid today, and a certificate valid between the two instants."""
    work = directory / "work"
    work.mkdir(parents=True)
    (work / "index.txt").write_text("", encoding="utf-8")
    (work / "serial").write_text("01\n", encoding="utf-8")
    # One file per subject: not every openssl honours -subj next to a configuration.
    names = f"IP:{AP_ADDRESS}, DNS:{AP_NAME}, IP:127.0.0.1, DNS:localhost"
    of_authority = work / "authority.cnf"
    of_console = work / "console.cnf"
    for configuration, subject in ((of_authority, "test authority"), (of_console, "console")):
        configuration.write_text(
            _CA_CONFIGURATION.format(work=work, names=names, subject=subject), encoding="utf-8"
        )
    authority_key = new_key(work / "authority.key")
    material = Material(directory)
    openssl(
        "req", "-x509", "-new", "-key", str(authority_key), "-sha256", "-days", "2",
        "-config", str(of_authority), "-extensions", "authority", "-out", str(material.authority),
    )  # fmt: skip
    new_key(material.key)
    request = work / "console.csr"
    openssl(
        "req", "-new", "-key", str(material.key), "-config", str(of_console), "-out", str(request)
    )
    openssl(
        "ca", "-batch", "-notext", "-config", str(of_authority),
        "-cert", str(material.authority), "-keyfile", str(authority_key),
        "-in", str(request), "-out", str(material.certificate), "-extensions", "console",
        "-startdate", _stamp(not_before), "-enddate", _stamp(not_after),
    )  # fmt: skip
    shutil.rmtree(work)
    return material


def loopback_listener() -> socket.socket:
    """A listening socket on this machine's loopback, on a port the system chose.

    Handed to the server instead of the address it is configured for: the
    console of a test is configured for an address of a network and is never
    reachable from one.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    return listener


def port_of(listener: socket.socket) -> int:
    port: int = listener.getsockname()[1]  # pyright: ignore[reportAny]  # the socket API's own
    return port
