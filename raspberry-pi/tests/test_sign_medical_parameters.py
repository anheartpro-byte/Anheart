from __future__ import annotations

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
            {"motion_limits.g_per_s": 0.6}, "g_rate", ("verify", "sign"), id="motion-import"
        ),
        pytest.param(
            {"hr_critical_bpm": 250},
            f"{MEDICAL_FILE}:hr_critical_bpm: 250 bpm",
            ("verify",),
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


def test_console_import_with_shipped_motion_out_of_bounds_exits_one(tmp_path: Path) -> None:
    copied = _copy_installation(tmp_path, {"motion_limits.g_per_s": 0.6})
    result = subprocess.run(
        [sys.executable, "-c", "import src.local_config"],
        cwd=copied,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "ValueError: g_rate must be finite and in (0, 0.5], got 0.6" in result.stderr
    assert "src/training/motion.py" in result.stderr
    assert f"{MEDICAL_FILE}:motion_limits" not in result.stderr
