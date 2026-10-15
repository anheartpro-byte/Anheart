"""The medical parameters (ANH-139): one versioned file, its signature, and what is read of them.

EX-1: the shipped file holds today's values, exactly, and nothing else does.
EX-2: a detached Ed25519 signature by the key embedded next to it. EX-7: a
valid signature, an invalid one, a missing one, a file altered by one byte, and
the version as each side says it. Every key used here is made by the test; the
private half of the key that signed the shipped file was destroyed after
signing it (docs/securite.md, "Decisions medicales").
"""

from __future__ import annotations

import getpass
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given, settings
from hypothesis import strategies as st

import scripts.sign_medical_parameters as signer
from src.medical import (
    BENCH_ONLY,
    CONFIG_DIRECTORY,
    PARAMETERS_PATH,
    PUBLIC_KEY_PATH,
    SHIPPED_DOCUMENT,
    SHIPPED_PARAMETERS,
    SIGNATURE_PATH,
    UNSIGNED_RECORD_PREFIX,
    UNSIGNED_SENTENCE,
    HrMaxFormula,
    MedicalDocument,
    MedicalParametersUnusableError,
    MedicalStatus,
    MotionLimitValues,
    SignatureProblem,
    Unsigned,
    parse_document,
    read_document,
    shipped_document,
    shipped_status,
    status_of,
    verify_signature,
)
from src.result import Err, Ok
from src.units import Bpm, GLoadPerSecond, MotorRpm, OutputRpmPerSecond, ResultantG, Seconds
from tests.medical_support import SHIPPED_CONTENT, public_pem, with_values, write_signed

#: A key of this test run, never written anywhere but a test's directory.
KEY: Final[Ed25519PrivateKey] = Ed25519PrivateKey.generate()


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    # A JSON object: str keys, values still unknown.
    return cast("dict[str, object]", value)


def _shipped_tree() -> dict[str, object]:
    decoded: object = json.loads(SHIPPED_CONTENT)  # pyright: ignore[reportAny]  # narrowed next
    return _object(decoded)


DOCUMENT: Final[dict[str, object]] = _shipped_tree()


def _encoded(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


def _with(path: tuple[str, ...], value: object) -> bytes:
    """The shipped document with one member set (or removed, for ``...``), encoded."""
    document = _shipped_tree()
    target = document
    for name in path[:-1]:
        target = _object(target[name])
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return _encoded(document)


def _problems(content: bytes) -> tuple[str, ...]:
    parsed = parse_document(content)
    assert isinstance(parsed, Err), f"accepted: {parsed!r}"
    return parsed.error


def _document(content: bytes) -> MedicalDocument:
    parsed = parse_document(content)
    assert isinstance(parsed, Ok), f"refused: {parsed!r}"
    return parsed.value


# =========================================================================
# EX-1: the shipped file, with exactly the values the code held before it
# =========================================================================


def test_the_shipped_file_holds_today_s_values_exactly() -> None:
    """Every value the code held before ANH-139, now read from the signed file.

    Written out by hand: a value changed in the file fails here, and must come
    with a medical decision (and a new signature).
    """
    assert SHIPPED_PARAMETERS.hr_hard_max_bpm == Bpm(148)
    assert SHIPPED_PARAMETERS.hr_critical_bpm == Bpm(158)
    assert SHIPPED_PARAMETERS.min_rider_age == 18
    assert SHIPPED_PARAMETERS.occupied_ceiling_g == ResultantG(1.2)
    assert SHIPPED_PARAMETERS.zone_ceiling_fraction == 0.9
    assert SHIPPED_PARAMETERS.hr_max_formula is HrMaxFormula.TANAKA
    assert SHIPPED_PARAMETERS.motion_limits == MotionLimitValues(
        output_rpm_per_s=OutputRpmPerSecond(0.25),
        g_per_s=GLoadPerSecond(0.03),
        min_run_motor_rpm=MotorRpm(55),
        max_interval_s=Seconds(1.0),
    )
    assert SHIPPED_PARAMETERS.recovery_min_s == Seconds(60.0)
    assert SHIPPED_PARAMETERS.cooldown_min_s == Seconds(4.0)


def test_the_shipped_file_names_its_version_its_date_and_its_signer() -> None:
    assert SHIPPED_DOCUMENT.version == "0.1.0"
    assert SHIPPED_DOCUMENT.issued_at == "2026-10-14"
    # The test key, to be replaced by the medical key of ANH-98.
    assert SHIPPED_DOCUMENT.signed_by == "cle-de-test"
    assert SHIPPED_DOCUMENT.content == SHIPPED_CONTENT
    assert len(SHIPPED_DOCUMENT.sha256) == 64
    # The release check-list finds every file that carries a medical value by its marker.
    assert "[MED]" in str(DOCUMENT["_comment"])


def test_the_shipped_file_is_read_once_and_its_bytes_kept() -> None:
    assert shipped_document() == SHIPPED_DOCUMENT
    assert "content" not in repr(SHIPPED_DOCUMENT)


def test_the_old_motion_limits_file_is_gone() -> None:
    """One source: the four motion limits are in the medical file, nowhere else."""
    assert not (CONFIG_DIRECTORY / "motion_limits.json").exists()


def test_no_private_key_is_shipped() -> None:
    for path in CONFIG_DIRECTORY.iterdir():
        assert b"PRIVATE KEY" not in path.read_bytes(), path


# =========================================================================
# EX-2: the signature of the shipped file
# =========================================================================


def test_the_shipped_file_is_signed_by_the_embedded_key() -> None:
    status = shipped_status()
    assert status.signed
    assert status.unsigned is None
    assert status.warning is None
    assert status.parameters == SHIPPED_PARAMETERS


def test_the_signature_is_a_detached_ed25519_one_over_the_exact_bytes() -> None:
    assert len(SIGNATURE_PATH.read_bytes()) == 64
    assert PUBLIC_KEY_PATH.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----\n")
    verified = verify_signature(
        PARAMETERS_PATH.read_bytes(), SIGNATURE_PATH.read_bytes(), PUBLIC_KEY_PATH.read_bytes()
    )
    assert verified == Ok(None)


# =========================================================================
# EX-7: valid, invalid, missing, altered by one byte
# =========================================================================


def test_a_valid_signature_verifies(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    document = _document(files.parameters.read_bytes())
    status = status_of(document, signature_path=files.signature, public_key_path=files.public_key)
    assert status.signed


@settings(max_examples=200, deadline=None)
@given(
    position=st.integers(min_value=0, max_value=len(SHIPPED_CONTENT) - 1), flip=st.integers(1, 255)
)
def test_a_file_altered_by_one_byte_anywhere_no_longer_verifies(position: int, flip: int) -> None:
    signature = KEY.sign(SHIPPED_CONTENT)
    altered = bytearray(SHIPPED_CONTENT)
    altered[position] ^= flip
    verified = verify_signature(bytes(altered), signature, public_pem(KEY))
    assert verified == Err(
        Unsigned(SignatureProblem.SIGNATURE_INVALID, "la signature ne correspond pas au fichier")
    )


def test_a_value_changed_by_one_byte_still_parses_and_is_unsigned(tmp_path: Path) -> None:
    """148 bpm becomes 149: the file reads, its signature does not: the bench mode."""
    files = write_signed(tmp_path, signer=KEY)
    altered = SHIPPED_CONTENT.replace(b'"hr_hard_max_bpm": 148', b'"hr_hard_max_bpm": 149')
    assert len(altered) == len(SHIPPED_CONTENT)
    files.parameters.write_bytes(altered)
    document = _document(files.parameters.read_bytes())
    assert document.parameters.hr_hard_max_bpm == 149
    status = status_of(document, signature_path=files.signature, public_key_path=files.public_key)
    assert not status.signed
    assert status.unsigned is not None
    assert status.unsigned.problem is SignatureProblem.SIGNATURE_INVALID


def test_a_line_ending_changed_is_an_altered_file(tmp_path: Path) -> None:
    """What a checkout that converts line endings would do (hence .gitattributes)."""
    files = write_signed(tmp_path, signer=KEY)
    document = _document(SHIPPED_CONTENT.replace(b"\n", b"\r\n"))
    status = status_of(document, signature_path=files.signature, public_key_path=files.public_key)
    assert not status.signed


def test_a_missing_signature_is_the_bench_mode(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    files.signature.unlink()
    status = status_of(
        SHIPPED_DOCUMENT, signature_path=files.signature, public_key_path=files.public_key
    )
    assert status.unsigned is not None
    assert status.unsigned.problem is SignatureProblem.SIGNATURE_MISSING
    assert status.unsigned.detail == "signature medical_parameters.sig absente ou illisible"


def test_a_missing_public_key_is_the_bench_mode(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    files.public_key.unlink()
    status = status_of(
        SHIPPED_DOCUMENT, signature_path=files.signature, public_key_path=files.public_key
    )
    assert status.unsigned is not None
    assert status.unsigned.problem is SignatureProblem.PUBLIC_KEY_MISSING
    assert status.unsigned.detail == "cle publique medical.pub absente ou illisible"


def test_a_signature_by_another_key_does_not_verify() -> None:
    other = Ed25519PrivateKey.generate()
    verified = verify_signature(SHIPPED_CONTENT, other.sign(SHIPPED_CONTENT), public_pem(KEY))
    assert isinstance(verified, Err)
    assert verified.error.problem is SignatureProblem.SIGNATURE_INVALID


@pytest.mark.parametrize("signature", [b"", b"\x00" * 63, b"\x00" * 64, b"\x00" * 65])
def test_a_signature_of_any_other_shape_does_not_verify(signature: bytes) -> None:
    verified = verify_signature(SHIPPED_CONTENT, signature, public_pem(KEY))
    assert isinstance(verified, Err)
    assert verified.error.problem is SignatureProblem.SIGNATURE_INVALID


def test_a_public_key_that_is_not_one_is_refused() -> None:
    for garbage in (
        b"",
        b"not a key",
        b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n",
    ):
        verified = verify_signature(SHIPPED_CONTENT, KEY.sign(SHIPPED_CONTENT), garbage)
        assert isinstance(verified, Err)
        assert verified.error.problem is SignatureProblem.PUBLIC_KEY_INVALID
        assert verified.error.detail.startswith("cle publique illisible")


def test_a_public_key_of_another_algorithm_is_refused() -> None:
    """An EC key in the right file is not the medical key, whatever it signs."""
    elliptic = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    verified = verify_signature(SHIPPED_CONTENT, KEY.sign(SHIPPED_CONTENT), elliptic)
    assert isinstance(verified, Err)
    assert verified.error.problem is SignatureProblem.PUBLIC_KEY_INVALID
    assert "Ed25519" in verified.error.detail


# =========================================================================
# The status, and the version as each side says it
# =========================================================================

WHY: Final[Unsigned] = Unsigned(SignatureProblem.SIGNATURE_MISSING, "signature absente")


def test_a_signed_status_announces_and_records_the_file_s_version() -> None:
    status = MedicalStatus(document=SHIPPED_DOCUMENT, unsigned=None)
    assert status.version == status.announced_version == status.recorded_version == "0.1.0"
    assert status.warning is None


def test_an_unsigned_status_vouches_for_no_version() -> None:
    """The heartbeat says none, the record says which bytes: an unsigned file may say anything."""
    status = MedicalStatus(document=SHIPPED_DOCUMENT, unsigned=WHY)
    assert status.version == "0.1.0"
    assert status.announced_version is None
    assert status.recorded_version == f"{UNSIGNED_RECORD_PREFIX}{SHIPPED_DOCUMENT.sha256}"
    assert status.recorded_version != status.version


def test_the_bench_mode_has_one_sentence() -> None:
    warning = MedicalStatus(document=SHIPPED_DOCUMENT, unsigned=WHY).warning
    assert warning == f"{UNSIGNED_SENTENCE} (signature absente) : {BENCH_ONLY}"
    assert warning is not None
    assert warning.startswith("parametres medicaux non signes")


# =========================================================================
# Parsing: what the console refuses to start with
# =========================================================================


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xfe not utf-8",
        b"{not json",
        b'{"version": NaN}',
        b'{"version": "0.1.0", "version": "9.9.9"}',
        b"\xef\xbb\xbf{}",
    ],
    ids=["not-utf8", "not-json", "nan", "duplicate", "bom"],
)
def test_a_file_that_is_not_strict_json_is_refused(content: bytes) -> None:
    (problem,) = _problems(content)
    assert problem.startswith("medical_parameters.json : JSON UTF-8 illisible")


@pytest.mark.parametrize(("content", "kind"), [(b"null", "null"), (b"[]", "list"), (b"7", "int")])
def test_a_document_that_is_not_an_object_is_refused(content: bytes, kind: str) -> None:
    assert _problems(content) == (f"medical_parameters.json : objet JSON attendu, trouve {kind}",)


def test_every_problem_is_said_at_once_and_each_once() -> None:
    """An absent object is said absent, not its every member as well."""
    problems = _problems(_encoded({"version": "1", "extra": True, "_comment": None}))
    assert problems == (
        "medical_parameters.json.issued_at : absent",
        "medical_parameters.json.signed_by : absent",
        "medical_parameters.json.parameters : absent",
        "medical_parameters.json.extra : membre inconnu",
        "medical_parameters.json._comment : texte attendu, trouve null",
        "medical_parameters.json.version : MAJEUR.MINEUR.CORRECTIF attendu, trouve '1'",
    )


def test_an_absent_motion_object_is_said_once() -> None:
    assert _problems(_with(("parameters", "motion_limits"), ...)) == (
        "medical_parameters.json.parameters.motion_limits : absent",
    )


def test_the_comment_is_optional() -> None:
    assert _document(_with(("_comment",), ...)).parameters == SHIPPED_PARAMETERS


@pytest.mark.parametrize(
    ("path", "value", "said"),
    [
        (("version",), "1.0", "version : MAJEUR.MINEUR.CORRECTIF"),
        (("version",), "1.0.0-rc1", "version : MAJEUR.MINEUR.CORRECTIF"),
        (("version",), "01.0.0", "version : MAJEUR.MINEUR.CORRECTIF"),
        (("version",), 1, "version : MAJEUR.MINEUR.CORRECTIF"),
        (("issued_at",), "14/10/2026", "issued_at : date AAAA-MM-JJ"),
        (("issued_at",), "2026-02-30", "issued_at : '2026-02-30' n'est pas une date du calendrier"),
        (("signed_by",), "Dr Jeanne Martin", "signed_by : identifiant du signataire"),
        (("signed_by",), "-x", "signed_by : identifiant du signataire"),
        (("parameters",), [], "parameters : objet JSON attendu, trouve list"),
        (("parameters", "hr_hard_max_bpm"), 148.0, "hr_hard_max_bpm : entier strictement positif"),
        (("parameters", "hr_hard_max_bpm"), True, "hr_hard_max_bpm : entier strictement positif"),
        (("parameters", "min_rider_age"), "18", "min_rider_age : entier strictement positif"),
        (("parameters", "min_rider_age"), 0, "min_rider_age : entier strictement positif"),
        (("parameters", "occupied_ceiling_g"), -1.2, "occupied_ceiling_g : nombre fini"),
        (("parameters", "recovery_min_s"), False, "recovery_min_s : nombre fini"),
        (("parameters", "cooldown_min_s"), "4", "cooldown_min_s : nombre fini"),
        (("parameters", "cooldown_min_s"), 0, "cooldown_min_s : nombre fini"),
        (
            ("parameters", "zone_ceiling_fraction"),
            1.2,
            "zone_ceiling_fraction : 1.2 au-dessus de 1",
        ),
        (("parameters", "hr_max_formula"), "fox", "hr_max_formula : 'tanaka' attendu"),
        (("parameters", "hr_critical_bpm"), 148, "hr_critical_bpm : 148 bpm doit etre au-dessus"),
        (("parameters", "hr_critical_bpm"), 120, "hr_critical_bpm : 120 bpm doit etre au-dessus"),
        (("parameters", "motion_limits"), None, "motion_limits : objet JSON attendu, trouve null"),
        (("parameters", "motion_limits", "min_run_motor_rpm"), 55.5, "min_run_motor_rpm : entier"),
        (("parameters", "motion_limits", "g_per_s"), None, "g_per_s : nombre fini"),
        (("parameters", "motion_limits", "jerk"), 1.0, "jerk : membre inconnu"),
        (("parameters", "comment"), "x", "parameters.comment : membre inconnu"),
    ],
)
def test_a_member_that_is_not_what_the_console_reads_is_refused(
    path: tuple[str, ...], value: object, said: str
) -> None:
    problems = _problems(_with(path, value))
    assert any(said in problem for problem in problems), problems


@pytest.mark.parametrize("member", ["hr_max_formula", "hr_hard_max_bpm", "cooldown_min_s"])
def test_an_absent_parameter_is_said_absent_once(member: str) -> None:
    assert _problems(_with(("parameters", member), ...)) == (
        f"medical_parameters.json.parameters.{member} : absent",
    )


def test_a_number_too_large_to_be_finite_is_refused() -> None:
    """``1e400`` is valid JSON, and ``json`` reads it as infinity."""
    content = SHIPPED_CONTENT.replace(b'"occupied_ceiling_g": 1.2', b'"occupied_ceiling_g": 1e400')
    assert content != SHIPPED_CONTENT
    assert _problems(content) == (
        "medical_parameters.json.parameters.occupied_ceiling_g : nombre fini strictement "
        + "positif attendu, trouve inf",
    )


FLOAT_MEMBERS: Final[tuple[str, ...]] = (
    "occupied_ceiling_g",
    "zone_ceiling_fraction",
    "recovery_min_s",
    "cooldown_min_s",
    "motion_limits.output_rpm_per_s",
    "motion_limits.g_per_s",
    "motion_limits.max_interval_s",
)
MAX_JSON_INTEGER: Final[int] = int("9" * sys.int_info.default_max_str_digits)


@pytest.mark.parametrize("member", FLOAT_MEMBERS)
@pytest.mark.parametrize("sign", [1, -1])
def test_an_integer_no_float_can_represent_is_a_documented_refusal(
    tmp_path: Path, member: str, sign: int
) -> None:
    content = with_values({member: sign * 10**399})
    expected = (
        f"medical_parameters.json.parameters.{member} : nombre fini strictement "
        "positif attendu, trouve un entier de 400 chiffres"
    )
    assert _problems(content) == (expected,)
    path = tmp_path / "medical_parameters.json"
    path.write_bytes(content)
    with pytest.raises(MedicalParametersUnusableError) as raised:
        shipped_document(path)
    assert raised.value.problems == (expected,)


@pytest.mark.parametrize(
    "member",
    [
        *FLOAT_MEMBERS,
        "hr_hard_max_bpm",
        "hr_critical_bpm",
        "min_rider_age",
        "motion_limits.min_run_motor_rpm",
    ],
)
@settings(deadline=None)
@given(value=st.integers(min_value=-MAX_JSON_INTEGER, max_value=MAX_JSON_INTEGER))
def test_every_json_integer_in_a_numeric_member_returns_a_result(member: str, value: int) -> None:
    assert isinstance(parse_document(with_values({member: value})), Ok | Err)


def test_whole_seconds_are_read_as_seconds() -> None:
    document = _document(_with(("parameters", "recovery_min_s"), 90))
    assert document.parameters.recovery_min_s == 90.0
    assert isinstance(document.parameters.recovery_min_s, float)


def test_the_parsed_document_carries_the_digest_of_its_bytes() -> None:
    content = _with(("version",), "1.2.3")
    document = _document(content)
    assert document.version == "1.2.3"
    assert document.content == content
    assert document.sha256 != SHIPPED_DOCUMENT.sha256


def test_a_file_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    missing = read_document(tmp_path / "medical_parameters.json")
    assert isinstance(missing, Err)
    (problem,) = missing.error
    assert problem.startswith("medical_parameters.json illisible")


def test_an_unusable_shipped_file_stops_the_console(tmp_path: Path) -> None:
    """Not the bench mode: there is no value to run even a bench session with."""
    broken = tmp_path / "medical_parameters.json"
    broken.write_bytes(_with(("parameters", "motion_limits"), ...))
    with pytest.raises(MedicalParametersUnusableError, match="motion_limits : absent") as raised:
        shipped_document(broken)
    assert raised.value.problems == ("medical_parameters.json.parameters.motion_limits : absent",)
    assert isinstance(raised.value, ValueError)


# =========================================================================
# The signer's script (off the Pi)
# =========================================================================

PASSPHRASE: Final[bytes] = b"phrase secrete de test"


def test_the_signer_makes_a_key_signs_and_the_console_agrees(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    private = tmp_path / "keys" / "medical.key"
    signer.generate_key(private, PASSPHRASE, public_path=files.public_key)
    assert private.stat().st_mode & 0o777 == 0o600
    assert b"ENCRYPTED PRIVATE KEY" in private.read_bytes()
    key = signer.load_private_key(private, PASSPHRASE)
    assert files.public_key.read_bytes() == public_pem(key)
    version = signer.sign(
        key,
        parameters_path=files.parameters,
        signature_path=files.signature,
        public_key_path=files.public_key,
    )
    assert version == SHIPPED_DOCUMENT.version
    status = status_of(
        _document(files.parameters.read_bytes()),
        signature_path=files.signature,
        public_key_path=files.public_key,
    )
    assert status.signed
    described = signer.describe(files.parameters, files.signature, files.public_key)
    assert described.startswith("version 0.1.0, signee le 2026-10-14 par cle-de-test")


def test_the_signer_never_writes_a_private_key_in_the_repository(tmp_path: Path) -> None:
    inside = signer.REPOSITORY / "raspberry-pi" / "config" / "medical.key"
    with pytest.raises(signer.SigningError, match="hors du depot"):
        signer.generate_key(inside, PASSPHRASE, public_path=tmp_path / "medical.pub")
    assert not inside.exists()


def test_the_signer_never_overwrites_a_key_nor_writes_one_in_clear(tmp_path: Path) -> None:
    existing = tmp_path / "medical.key"
    existing.write_bytes(b"x")
    with pytest.raises(signer.SigningError, match="existe deja"):
        signer.generate_key(existing, PASSPHRASE, public_path=tmp_path / "medical.pub")
    with pytest.raises(signer.SigningError, match="phrase secrete vide"):
        signer.generate_key(tmp_path / "new.key", b"", public_path=tmp_path / "medical.pub")


def test_the_signer_refuses_a_wrong_passphrase_or_another_kind_of_key(tmp_path: Path) -> None:
    private = tmp_path / "medical.key"
    signer.generate_key(private, PASSPHRASE, public_path=tmp_path / "medical.pub")
    with pytest.raises(signer.SigningError, match="cle privee illisible"):
        signer.load_private_key(private, b"autre chose")
    elliptic = tmp_path / "ec.key"
    elliptic.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(signer.SigningError, match="Ed25519"):
        signer.load_private_key(elliptic, None)


def test_the_signer_refuses_a_key_that_is_not_the_embedded_one(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    before = files.signature.read_bytes()
    with pytest.raises(signer.SigningError, match=r"n'est pas celle de medical\.pub"):
        signer.sign(
            Ed25519PrivateKey.generate(),
            parameters_path=files.parameters,
            signature_path=files.signature,
            public_key_path=files.public_key,
        )
    assert files.signature.read_bytes() == before


def test_the_signer_refuses_a_file_the_console_would_refuse(tmp_path: Path) -> None:
    files = write_signed(tmp_path, content=_with(("version",), "un"), signer=KEY)
    with pytest.raises(signer.SigningError, match="fichier refuse par la console"):
        signer.sign(
            KEY,
            parameters_path=files.parameters,
            signature_path=files.signature,
            public_key_path=files.public_key,
        )
    with pytest.raises(signer.SigningError, match="fichier refuse par la console"):
        signer.describe(files.parameters, files.signature, files.public_key)


def test_the_signer_s_check_says_when_the_signature_does_not_hold(tmp_path: Path) -> None:
    files = write_signed(tmp_path, signer=KEY)
    files.parameters.write_bytes(SHIPPED_CONTENT + b"\n")
    with pytest.raises(signer.SigningError, match="parametres medicaux non signes"):
        signer.describe(files.parameters, files.signature, files.public_key)


def test_the_signer_s_check_of_the_shipped_files_passes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert signer.main(["verify"]) == 0
    assert f"sha256 {SHIPPED_DOCUMENT.sha256}" in capsys.readouterr().out


def test_the_signer_s_commands_ask_the_passphrase_and_say_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["une", "autre"])

    def ask(_prompt: str) -> str:
        return next(answers)

    def always(_prompt: str) -> str:
        return "x"

    monkeypatch.setattr(getpass, "getpass", ask)
    assert signer.main(["generate-key", str(tmp_path / "medical.key")]) == 1
    assert "les deux phrases secretes different" in capsys.readouterr().err
    monkeypatch.setattr(getpass, "getpass", always)
    assert signer.main(["sign", str(tmp_path / "absent.key")]) == 1
    assert "cle privee illisible" in capsys.readouterr().err


def test_a_status_built_by_hand_keeps_the_parameters_it_was_given() -> None:
    """What the other tests build (tests/medical_support.py) is a status like any other."""
    changed = replace(SHIPPED_PARAMETERS, min_rider_age=21)
    status = MedicalStatus(document=replace(SHIPPED_DOCUMENT, parameters=changed), unsigned=None)
    assert status.parameters.min_rider_age == 21
