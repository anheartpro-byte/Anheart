"""Sign the medical parameters: what the medical signer runs, on their own computer, never on a Pi.

Three commands, from ``raspberry-pi/``::

    python scripts/sign_medical_parameters.py generate-key ~/anheart-medical.key
    python scripts/sign_medical_parameters.py sign ~/anheart-medical.key
    python scripts/sign_medical_parameters.py verify

``generate-key`` makes the medical key pair, once: the private key, encrypted
with a passphrase asked twice, at the path given, which must be outside this
repository (the script refuses a path inside it, so the key cannot be
committed by mistake); and its public half written over ``config/medical.pub``,
the key every console checks the signature against. That key is the medical
one and no other: never use it to sign anything else (the programme catalogue
has a key of its own).

``sign`` reads ``config/medical_parameters.json``, refuses it if the console
would refuse it (the same parser, :func:`src.medical.parse_document`), refuses
a private key whose public half is not ``config/medical.pub``, then writes the
detached Ed25519 signature of the file's exact bytes to
``config/medical_parameters.sig`` and checks it the way the console will.

``verify`` checks the three files as the console does at startup, and says
which version is signed, by whom, and with which key.

The same check, without this repository's code (OpenSSL 3)::

    openssl pkeyutl -verify -pubin -inkey config/medical.pub -rawin \\
        -in config/medical_parameters.json -sigfile config/medical_parameters.sig

The file and its signature travel together: change one byte of the file, a
space or a line ending included, and the console starts in bench mode until
the file is signed again. See docs/securite.md, "Decisions medicales".
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

# Run from anywhere: put the project root (parent of scripts/) on the path,
# same convention as the other scripts.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.medical import (
    PARAMETERS_PATH,
    PUBLIC_KEY_PATH,
    SIGNATURE_PATH,
    parse_document,
    status_of,
)
from src.result import Err

REPOSITORY = Path(__file__).resolve().parents[2]
"""The root of the repository: no private key is ever written under it."""


class SigningError(Exception):
    """A refusal, said in words; the command exits with code 1."""


def public_pem(private: Ed25519PrivateKey) -> bytes:
    """The public half of ``private``, as ``config/medical.pub`` holds it."""
    return private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def generate_key(
    private_path: Path, passphrase: bytes, public_path: Path = PUBLIC_KEY_PATH
) -> None:
    """Write a new key pair: the private key encrypted at ``private_path``, the public one at ``public_path``."""
    target = private_path.expanduser().resolve()
    if target.is_relative_to(REPOSITORY):
        raise SigningError(f"{target} est dans le depot : la cle privee doit rester hors du depot")
    if target.exists():
        raise SigningError(f"{target} existe deja : aucune cle n'est ecrasee")
    if not passphrase:
        raise SigningError("phrase secrete vide : la cle privee doit etre chiffree")
    private = Ed25519PrivateKey.generate()
    encrypted = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encrypted)
    public_path.write_bytes(public_pem(private))


def load_private_key(private_path: Path, passphrase: bytes | None) -> Ed25519PrivateKey:
    """The medical private key at ``private_path``; refused if it is not an Ed25519 one."""
    try:
        loaded = serialization.load_pem_private_key(private_path.read_bytes(), passphrase)
    except (OSError, ValueError, TypeError) as error:
        raise SigningError(f"cle privee illisible : {error}") from error
    if not isinstance(loaded, Ed25519PrivateKey):
        raise SigningError("la cle privee n'est pas une cle Ed25519")
    return loaded


def sign(
    private: Ed25519PrivateKey,
    parameters_path: Path = PARAMETERS_PATH,
    signature_path: Path = SIGNATURE_PATH,
    public_key_path: Path = PUBLIC_KEY_PATH,
) -> str:
    """Sign the file with ``private`` and check the result as the console will. Returns its version."""
    content = parameters_path.read_bytes()
    parsed = parse_document(content, where=parameters_path.name)
    if isinstance(parsed, Err):
        raise SigningError("fichier refuse par la console : " + " ; ".join(parsed.error))
    if public_pem(private) != public_key_path.read_bytes():
        raise SigningError(
            f"cette cle privee n'est pas celle de {public_key_path.name} : "
            "la console refuserait la signature"
        )
    signature_path.write_bytes(private.sign(content))
    status = status_of(parsed.value, signature_path=signature_path, public_key_path=public_key_path)
    if status.warning is not None:
        raise SigningError(status.warning)
    return parsed.value.version


def describe(
    parameters_path: Path = PARAMETERS_PATH,
    signature_path: Path = SIGNATURE_PATH,
    public_key_path: Path = PUBLIC_KEY_PATH,
) -> str:
    """What the console would say of the three files at startup."""
    parsed = parse_document(parameters_path.read_bytes(), where=parameters_path.name)
    if isinstance(parsed, Err):
        raise SigningError("fichier refuse par la console : " + " ; ".join(parsed.error))
    document = parsed.value
    status = status_of(document, signature_path=signature_path, public_key_path=public_key_path)
    if status.warning is not None:
        raise SigningError(status.warning)
    return (
        f"version {document.version}, signee le {document.issued_at} par {document.signed_by} "
        f"(sha256 {document.sha256})"
    )


def _passphrase(confirm: bool) -> bytes:
    first = getpass.getpass("phrase secrete de la cle medicale : ")
    if confirm and getpass.getpass("la meme, encore : ") != first:
        raise SigningError("les deux phrases secretes different")
    return first.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-key", help="creer la paire de cles medicale")
    generate.add_argument("private_key", type=Path)
    signing = commands.add_parser("sign", help="signer config/medical_parameters.json")
    signing.add_argument("private_key", type=Path)
    commands.add_parser("verify", help="verifier comme la console au demarrage")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "generate-key":
            generate_key(arguments.private_key, _passphrase(confirm=True))
            print(f"cle privee : {arguments.private_key} ; cle publique : {PUBLIC_KEY_PATH}")
        elif arguments.command == "sign":
            private = load_private_key(arguments.private_key, _passphrase(confirm=False))
            print(f"signe : version {sign(private)}, {SIGNATURE_PATH}")
        else:
            print(describe())
    except SigningError as error:
        print(f"refuse : {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
