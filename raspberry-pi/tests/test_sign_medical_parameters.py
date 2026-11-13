"""Check medical signing against the console's bounds and command-line refusals."""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import scripts.sign_medical_parameters as signer
from src.local_config import MEDICAL_FILE, ConfigProblem, load_local_config
from src.medical import MedicalStatus, parse_document
from src.result import Err, Ok
from tests.medical_support import with_values, write_signed

SIM_ENV: Final[Mapping[str, str]] = {
    "MOTOR_BACKEND": "sim",
    "ECG_SOURCE": "sim",
    "ARM_RADIUS_M": "1.5",
}
PI: Final[Path] = Path(__file__).resolve().parents[1]


def _console_problems(content: bytes) -> tuple[ConfigProblem, ...]:
    parsed = parse_document(content)
    assert isinstance(parsed, Ok), parsed
    loaded = load_local_config(SIM_ENV, medical=MedicalStatus(document=parsed.value, unsigned=None))
    if isinstance(loaded, Err):
        return tuple(
            problem for problem in loaded.error if problem.key.startswith(f"{MEDICAL_FILE}:")
        )
    return ()


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"hr_critical_bpm": 250}, id="ticket-critical-250"),
        pytest.param({"hr_hard_max_bpm": 99}, id="hard-99"),
        pytest.param({"hr_hard_max_bpm": 221, "hr_critical_bpm": 222}, id="hard-221"),
        pytest.param({"hr_critical_bpm": 221}, id="critical-221"),
        pytest.param({"min_rider_age": 9}, id="age-9"),
        pytest.param({"min_rider_age": 101}, id="age-101"),
        pytest.param({"motion_limits.output_rpm_per_s": 2.01}, id="acceleration-2.01"),
        pytest.param({"motion_limits.g_per_s": 0.51}, id="g-rate-0.51"),
        pytest.param({"motion_limits.min_run_motor_rpm": 201}, id="min-run-201"),
        pytest.param({"motion_limits.max_interval_s": 2.01}, id="interval-2.01"),
        pytest.param(
            {"hr_hard_max_bpm": 10**399, "hr_critical_bpm": 10**399 + 1}, id="huge-integer-tier"
        ),
        pytest.param(
            {
                "hr_hard_max_bpm": 99,
                "hr_critical_bpm": 250,
                "min_rider_age": 101,
                "motion_limits.g_per_s": 0.51,
            },
            id="all-problems-in-console-order",
        ),
    ],
)
def test_sign_and_verify_refuse_the_console_s_problems_before_keys_or_signature(
    tmp_path: Path, changes: Mapping[str, int | float]
) -> None:
    key = Ed25519PrivateKey.generate()
    files = write_signed(tmp_path, signer=key)
    before = files.signature.read_bytes()
    content = with_values(changes)
    files.parameters.write_bytes(content)
    problems = _console_problems(content)
    assert problems
    expected = "la console refuserait ce fichier au demarrage (code 2) : " + " ; ".join(
        f"{problem.key}: {problem.detail}" for problem in problems
    )
    with pytest.raises(signer.SigningError) as signing:
        signer.sign(
            Ed25519PrivateKey.generate(), files.parameters, files.signature, files.public_key
        )
    assert str(signing.value) == expected
    assert files.signature.read_bytes() == before
    with pytest.raises(signer.SigningError) as verifying:
        signer.describe(files.parameters, files.signature, files.public_key)
    assert str(verifying.value) == expected
    assert files.signature.read_bytes() == before


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"hr_hard_max_bpm": 100, "hr_critical_bpm": 101}, id="tiers-100-101"),
        pytest.param({"hr_hard_max_bpm": 219, "hr_critical_bpm": 220}, id="tiers-219-220"),
        pytest.param({"min_rider_age": 10}, id="age-10"),
        pytest.param({"min_rider_age": 100}, id="age-100"),
        pytest.param({"motion_limits.output_rpm_per_s": 2.0}, id="acceleration-2.0"),
        pytest.param({"motion_limits.g_per_s": 0.5}, id="g-rate-0.5"),
        pytest.param({"motion_limits.min_run_motor_rpm": 1}, id="min-run-1"),
        pytest.param({"motion_limits.min_run_motor_rpm": 200}, id="min-run-200"),
        pytest.param({"motion_limits.max_interval_s": 2.0}, id="interval-2.0"),
    ],
)
def test_sign_and_verify_accept_the_console_s_boundary_values(
    tmp_path: Path, changes: Mapping[str, int | float]
) -> None:
    key = Ed25519PrivateKey.generate()
    files = write_signed(tmp_path, signer=key)
    content = with_values(changes)
    files.parameters.write_bytes(content)
    assert _console_problems(content) == ()
    assert signer.sign(key, files.parameters, files.signature, files.public_key) == "0.1.0"
    assert files.signature.read_bytes() == key.sign(content)
    assert signer.describe(files.parameters, files.signature, files.public_key).startswith(
        "version 0.1.0"
    )


def _copy_installation(tmp_path: Path, changes: Mapping[str, int | float]) -> Path:
    copied = tmp_path / "raspberry-pi"
    shutil.copytree(PI / "src", copied / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(PI / "config", copied / "config")
    (copied / "scripts").mkdir()
    shutil.copy2(PI / "scripts" / "sign_medical_parameters.py", copied / "scripts")
    shutil.copy2(PI / "VERSION", copied / "VERSION")
    shutil.copytree(PI.parent / "contracts", tmp_path / "contracts")
    (copied / "config" / "medical_parameters.json").write_bytes(with_values(changes))
    return copied


@pytest.mark.parametrize(
    ("changes", "problem", "commands"),
    [
        pytest.param(
            {"occupied_ceiling_g": 10**399},
            "un entier de 400 chiffres",
            ("verify", "sign"),
            id="huge-integer",
        ),
        pytest.param(
            {"motion_limits.g_per_s": 0.6},
            f"{MEDICAL_FILE}:motion_limits: g_rate (g_per_s) must be",
            ("verify", "sign"),
            id="motion-bounds",
        ),
        pytest.param(
            {"recovery_min_s": 301.0},
            "profils livres (profiles.default.json)",
            ("verify", "sign"),
            id="recovery-above-shipped-profiles",
        ),
        pytest.param(
            {"cooldown_min_s": 241.0},
            "profils livres (profiles.default.json)",
            ("verify", "sign"),
            id="cooldown-above-shipped-profiles",
        ),
        pytest.param(
            {"zone_ceiling_fraction": 0.85},
            "profils livres (profiles.default.json)",
            ("verify", "sign"),
            id="zone-ceiling-below-shipped-profiles",
        ),
        pytest.param(
            {"hr_critical_bpm": 250},
            f"{MEDICAL_FILE}:hr_critical_bpm: 250 bpm",
            ("verify", "sign"),
            id="critical-250",
        ),
    ],
)
def test_cli_refuses_unusable_shipped_values_without_a_traceback_or_a_write(
    tmp_path: Path, changes: Mapping[str, int | float], problem: str, commands: tuple[str, ...]
) -> None:
    copied = _copy_installation(tmp_path, changes)
    signature = copied / "config" / "medical_parameters.sig"
    before = signature.read_bytes()
    for command in commands:
        arguments = [sys.executable, "scripts/sign_medical_parameters.py", command]
        if command == "sign":
            arguments.append(str(tmp_path / "absent.key"))
        result = subprocess.run(  # noqa: S603 - fixed executable, scripts and test paths
            arguments,
            cwd=copied,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 1, result
        assert result.stderr.startswith("refuse :"), result.stderr
        assert problem in result.stderr
        assert "Traceback" not in result.stderr
        assert "phrase secrete" not in result.stderr
        assert result.stdout == ""
        assert signature.read_bytes() == before


def test_main_refuses_out_of_bounds_before_asking_for_a_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    files = write_signed(tmp_path)
    before = files.signature.read_bytes()
    files.parameters.write_bytes(with_values({"hr_critical_bpm": 250}))
    monkeypatch.setattr(signer, "PARAMETERS_PATH", files.parameters)
    monkeypatch.setattr(signer, "SIGNATURE_PATH", files.signature)
    monkeypatch.setattr(signer, "PUBLIC_KEY_PATH", files.public_key)

    def unexpected_prompt(_prompt: str) -> str:
        pytest.fail("an out-of-bounds file must be refused before asking for a passphrase")

    monkeypatch.setattr(getpass, "getpass", unexpected_prompt)
    assert signer.main(["sign", str(tmp_path / "absent.key")]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("refuse : la console refuserait ce fichier au demarrage (code 2)")
    assert f"{MEDICAL_FILE}:hr_critical_bpm: 250 bpm" in output.err
    assert "phrase secrete" not in output.err
    assert files.signature.read_bytes() == before


def test_main_verifies_the_three_files_it_is_pointed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``verify`` and ``sign`` are handed the same three paths, whichever is changed."""
    files = write_signed(tmp_path)
    monkeypatch.setattr(signer, "PARAMETERS_PATH", files.parameters)
    monkeypatch.setattr(signer, "SIGNATURE_PATH", files.signature)
    monkeypatch.setattr(signer, "PUBLIC_KEY_PATH", files.public_key)
    assert signer.main(["verify"]) == 0
    assert capsys.readouterr().out.startswith("version 0.1.0")
    files.signature.write_bytes(b"not a signature")
    assert signer.main(["verify"]) == 1
    assert capsys.readouterr().err.startswith("refuse : ")


def test_module_import_with_unusable_shipped_integer_reraises(tmp_path: Path) -> None:
    copied = _copy_installation(tmp_path, {"occupied_ceiling_g": 10**399})
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.sign_medical_parameters"],
        cwd=copied,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode != 0
    assert "MedicalParametersUnusableError" in result.stderr
    assert "un entier de 400 chiffres" in result.stderr


@pytest.mark.parametrize(
    ("changes", "said"),
    [
        pytest.param(
            {"motion_limits.g_per_s": 0.6},
            f"{MEDICAL_FILE}:motion_limits: g_rate (g_per_s) must be finite and in (0, 0.5], "
            "got 0.6",
            id="motion-above-its-bound",
        ),
        pytest.param({"motion_limits.g_per_s": 0}, "g_per_s", id="motion-zero"),
        pytest.param({"motion_limits.g_per_s": -0.1}, "g_per_s", id="motion-negative"),
        pytest.param(
            {"motion_limits.min_run_motor_rpm": 0}, "min_run_motor_rpm", id="min-run-zero"
        ),
        pytest.param({"motion_limits.max_interval_s": 0}, "max_interval_s", id="interval-zero"),
        pytest.param(
            {"recovery_min_s": 301.0},
            "the profile store cannot start",
            id="shipped-profiles-refused",
        ),
    ],
)
def test_a_medical_file_the_console_cannot_run_with_keeps_the_service_down(
    tmp_path: Path, changes: Mapping[str, int | float], said: str
) -> None:
    """ANH-235: whatever refuses it, the bounds of the console at startup, the parser of
    the medical file at import, or the shipped profiles it makes unreadable: one
    ``configuration:`` line, code 2, the one the service does not restart on. Never a
    traceback and code 1, started again and again."""
    copied = _copy_installation(tmp_path, changes)
    result = subprocess.run(
        [sys.executable, "-m", "src.local_panel"],
        cwd=copied,
        env={"PATH": os.environ["PATH"], **SIM_ENV},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, result
    lines = [line for line in result.stderr.splitlines() if "configuration: " in line]
    assert len(lines) == 1, result.stderr
    assert said in lines[0]
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"recovery_min_s": 300.0}, id="recovery-300"),
        pytest.param({"cooldown_min_s": 240.0}, id="cooldown-240"),
        pytest.param({"zone_ceiling_fraction": 0.852}, id="zone-ceiling-0.852"),
    ],
)
def test_verify_accepts_the_shipped_profiles_at_their_edge(
    tmp_path: Path, changes: Mapping[str, int | float]
) -> None:
    """Edited, the file is no longer the one signed: verify gets to the signature."""
    copied = _copy_installation(tmp_path, changes)
    result = subprocess.run(
        [sys.executable, "scripts/sign_medical_parameters.py", "verify"],
        cwd=copied,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1, result
    assert "la signature ne correspond pas au fichier" in result.stderr
    assert "profils livres" not in result.stderr


def test_a_file_whose_profile_values_changed_since_the_command_started_is_refused(
    tmp_path: Path,
) -> None:
    """The shipped profiles are judged with the values the command read at its start."""
    key = Ed25519PrivateKey.generate()
    files = write_signed(tmp_path, signer=key)
    before = files.signature.read_bytes()
    files.parameters.write_bytes(with_values({"recovery_min_s": 61.0}))
    with pytest.raises(signer.SigningError, match="la relancer pour juger les profils livres"):
        signer.sign(key, files.parameters, files.signature, files.public_key)
    assert files.signature.read_bytes() == before


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        pytest.param({"hr_hard_max_bpm": 150, "hr_critical_bpm": 160}, "paliers", id="tiers"),
        pytest.param({"motion_limits.min_run_motor_rpm": 200}, "min_run_rpm", id="min-run"),
    ],
)
def test_a_file_the_shipped_profiles_cannot_run_under_is_signed_with_a_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changes: Mapping[str, int | float],
    why: str,
) -> None:
    """ANH-235: read, they would not be run: the signer is told which, the file is signed."""
    key = Ed25519PrivateKey.generate()
    files = write_signed(tmp_path, signer=key)
    assert signer.describe(files.parameters, files.signature, files.public_key)
    assert capsys.readouterr().err == "", "the shipped file: no warning"
    files.parameters.write_bytes(with_values(changes))
    assert signer.sign(key, files.parameters, files.signature, files.public_key) == "0.1.0"
    warned = capsys.readouterr().err
    assert warned.startswith("attention : avec ce fichier la console ne lancerait pas")
    assert f"standard_30_min ({why}" in warned
    assert f"standard_45_min ({why}" in warned
