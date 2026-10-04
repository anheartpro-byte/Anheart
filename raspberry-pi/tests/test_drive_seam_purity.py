from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

from src.motor.backend import DriveBackend as BackendProtocol
from src.motor.drive import DriveBackend

MOTOR_SOURCE: Final[Path] = Path(__file__).resolve().parent.parent / "src" / "motor"
SEAM_SOURCES: Final[tuple[Path, ...]] = (
    MOTOR_SOURCE / "drive.py",
    MOTOR_SOURCE / "backend.py",
    MOTOR_SOURCE / "acquisition.py",
)


def _imported_roots(source: Path) -> frozenset[str]:
    """Top-level names of everything a module imports, read from its source.

    Parsed rather than introspected at runtime: ``sys.modules`` is polluted by
    whatever else the test session imported, so an import check done that way
    passes or fails depending on test order.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            module = node.module
            # A relative import would record "" and fail the allow-list below,
            # which is intended: this repo imports absolutely, from src.*.
            roots.add("" if module is None else module.split(".")[0])
    return frozenset(roots)


@pytest.mark.parametrize("source_path", SEAM_SOURCES)
def test_the_drive_seam_imports_no_hardware_library(source_path: Path) -> None:
    """The seam must stay pure, or the simulator stops being equivalent.

    ``pymodbus`` and ``serial`` belong to ``src/motor/atv320.py`` alone
    (contract rule 5). If either appeared here, every module that decodes a
    status word would drag a serial stack behind it, and the closed-loop safety
    tests would no longer be exercising a hardware-free path.
    """
    roots = _imported_roots(source_path)
    for banned in ("pymodbus", "serial", "bitalino", "biosppy", "numpy", "scipy"):
        assert banned not in roots, banned


@pytest.mark.parametrize("source_path", SEAM_SOURCES)
def test_the_drive_seam_imports_only_the_standard_library_and_src(source_path: Path) -> None:
    """An allow-list, so a new dependency has to be a deliberate edit.

    Stricter than the ban-list above on purpose: the interesting failure is not
    somebody importing pymodbus here, it is a helpful refactor pulling in
    something that makes this module un-simulatable.
    """
    allowed = {"__future__", "collections", "dataclasses", "enum", "types", "typing", "src"}
    assert _imported_roots(source_path) <= allowed


@pytest.mark.parametrize("source_path", SEAM_SOURCES)
def test_the_seam_reads_no_clock(source_path: Path) -> None:
    """Timing decisions take `now` or an injected Clock (contract rule 4).

    There is a repo-wide grep test for this; it is repeated for this file
    because a decoder that timestamps its own output is exactly how a stale
    status starts looking fresh.
    """
    source = source_path.read_text(encoding="utf-8")
    assert "time.monotonic" not in source
    assert "time.time" not in source


def test_public_drive_import_preserves_the_extracted_protocol_identity() -> None:
    # Given the defining protocol and its existing public import.
    # When consumers resolve either entry point.
    # Then structural checks use the same protocol object.
    assert DriveBackend is BackendProtocol
