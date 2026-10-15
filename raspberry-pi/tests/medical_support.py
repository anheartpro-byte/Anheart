"""Medical parameters for the tests: the shipped values, changed, signed or not, and real keys.

A :class:`~src.medical.MedicalStatus` is a value: a test that is about what the
console does with signed (or unsigned) parameters builds one here, without
files. A test that is about the signature itself writes the three files with a
key it generates (:func:`write_signed`); no private key exists anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.medical import (
    PARAMETERS_PATH,
    SHIPPED_DOCUMENT,
    SHIPPED_PARAMETERS,
    MedicalParameters,
    MedicalStatus,
    SignatureProblem,
    Unsigned,
)

SHIPPED_CONTENT: Final[bytes] = PARAMETERS_PATH.read_bytes()
"""The shipped file's bytes, as committed."""

INVALID: Final[Unsigned] = Unsigned(
    SignatureProblem.SIGNATURE_INVALID, "la signature ne correspond pas au fichier"
)


def signed(parameters: MedicalParameters = SHIPPED_PARAMETERS) -> MedicalStatus:
    """The shipped document with ``parameters``, taken as signed."""
    return MedicalStatus(document=replace(SHIPPED_DOCUMENT, parameters=parameters), unsigned=None)


def unsigned(
    parameters: MedicalParameters = SHIPPED_PARAMETERS, why: Unsigned = INVALID
) -> MedicalStatus:
    """The shipped document with ``parameters``, whose signature did not verify."""
    return MedicalStatus(document=replace(SHIPPED_DOCUMENT, parameters=parameters), unsigned=why)


def public_pem(private: Ed25519PrivateKey) -> bytes:
    """``private``'s public half as ``config/medical.pub`` holds it."""
    return private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


@dataclass(frozen=True, slots=True)
class SignedFiles:
    """The three files of the medical parameters, written in a test's directory."""

    parameters: Path
    signature: Path
    public_key: Path


def write_signed(
    directory: Path,
    content: bytes = SHIPPED_CONTENT,
    signer: Ed25519PrivateKey | None = None,
) -> SignedFiles:
    """``content``, its signature by ``signer`` (a fresh key by default) and its public half."""
    chosen = Ed25519PrivateKey.generate() if signer is None else signer
    files = SignedFiles(
        parameters=directory / "medical_parameters.json",
        signature=directory / "medical_parameters.sig",
        public_key=directory / "medical.pub",
    )
    files.parameters.write_bytes(content)
    files.signature.write_bytes(chosen.sign(content))
    files.public_key.write_bytes(public_pem(chosen))
    return files
