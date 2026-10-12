"""The console's certificate, turned into what the server serves with.

**Read once.** The two files of :class:`~src.web.deps.TlsFiles` are opened
here and nowhere else, when the console starts: first by the entry point,
which refuses a certificate it cannot serve as a configuration error, before
anything of the machine is opened; then when the server is built. After that
the console holds its certificate in memory. Nothing on the control loop's
thread ever waits on these files, and replacing or removing them under a
running console changes nothing until its next start.

**The dates are not judged here.** A server has no use for the validity dates
of its own certificate: the tablet judges them, with its own clock. A console
that refused to start, or stopped serving, because its certificate had expired
would turn a renewal that is late into a machine nobody can reach, and the
clock of a Pi without a battery is the last thing to bet that on. So an
expired certificate is served like any other, the tablet's browser says what
it thinks of it, and ``scripts/pi/make_console_tls.sh --check`` is what tells
a person, ahead of time, that the renewal is due.

**No passphrase.** The console starts unattended, at boot and after a
watchdog restart: a key that needs somebody to type something is a console
that does not come back. It is refused here by name rather than left to the
TLS library, which would otherwise ask the terminal.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from src.result import Err, Ok, Result
from src.web.deps import TlsFiles

_CERTIFICATE_MARK: Final[bytes] = b"-----BEGIN CERTIFICATE-----"
_KEY_MARK: Final[bytes] = b"PRIVATE KEY-----"
"""What a PEM file of each kind contains: enough to tell which of the two
files is the wrong one, which the TLS library's own error does not say."""

_KEY_MISMATCH: Final[str] = "KEY_VALUES_MISMATCH"
"""The TLS library's name for a private key that is not the certificate's."""


@dataclass(frozen=True, slots=True)
class TlsRefused:
    """Why the certificate cannot be served, in words for the console's journal.

    Paths and reasons only: nothing read from either file is ever in it.
    """

    detail: str


def _unusable(role: str, path: Path, mark: bytes) -> str | None:
    """Why ``path`` cannot be the ``role`` it is configured as; ``None`` when it can."""
    if not path.is_file():
        return f"the {role} {path} is not a file the console can see"
    try:
        content = path.read_bytes()
    except OSError as error:
        return f"the {role} {path} cannot be read: {error.strerror}"
    if mark not in content:
        return f"{path} holds no PEM {role}"
    return None


def server_context(files: TlsFiles) -> Result[ssl.SSLContext, TlsRefused]:
    """The TLS context the server serves with, or why these files cannot be served.

    TLS 1.2 at least, and the library's own choice of ciphers for a server.
    No client certificate is asked for: who may talk to the console is the
    token's business, and tomorrow the operator's login.

    Blocking: it reads two files. Called at startup only, never from a tick.
    """
    for refusal in (
        _unusable("certificate", files.certificate_path, _CERTIFICATE_MARK),
        _unusable("private key", files.key_path, _KEY_MARK),
    ):
        if refusal is not None:
            return Err(TlsRefused(refusal))

    asked: list[bool] = []

    def no_passphrase() -> str:
        """Called by the TLS library only when the key is protected by a passphrase."""
        asked.append(True)
        return ""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(
            certfile=files.certificate_path, keyfile=files.key_path, password=no_passphrase
        )
    except OSError as error:  # ssl.SSLError is one
        if asked:
            return Err(
                TlsRefused(
                    f"the private key {files.key_path} is protected by a passphrase: the "
                    "console starts unattended and has nobody to ask"
                )
            )
        if isinstance(error, ssl.SSLError) and error.reason == _KEY_MISMATCH:
            return Err(
                TlsRefused(
                    f"the private key {files.key_path} is not the one of the certificate "
                    f"{files.certificate_path}"
                )
            )
        return Err(
            TlsRefused(
                f"the TLS library refuses {files.certificate_path} with {files.key_path}: {error}"
            )
        )
    return Ok(context)
