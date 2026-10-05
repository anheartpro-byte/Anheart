"""Evidence that the typing contract is actually enforced, not merely claimed.

Everything else in the suite tests behaviour at runtime. These tests check that
the *compile-time* guarantees still hold, because those guarantees are what the
safety argument leans on:

* an unhandled drive-error variant must fail the type check, by name;
* the checkers must be configured strictly enough to see it.

Without this, a tool upgrade or a config edit could silently turn the whole
Result arrangement into decoration and every other test would still be green.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
INCOMPLETE_FIXTURE = PROJECT_ROOT / "tests" / "typing_fixtures" / "incomplete_match.py"

CHECK_TIMEOUT_S = 300


# --- The one place Any is allowed in, then immediately parsed out --------
#
# tomllib.loads returns dict[str, Any], so every downstream access would be an
# Any leak. Rather than sprinkle suppressions, the untyped value is confined to
# this single function and turned into `object` at once; the readers below
# narrow it with isinstance. Exactly rule 6 of the contract, applied to the
# test code that enforces rule 6.


def _as_table(value: object) -> Mapping[str, object]:
    """Narrow a parsed TOML value to a table. TOML keys are strings by construction."""
    assert isinstance(value, dict), f"expected a TOML table, got {type(value).__name__}"
    return cast("Mapping[str, object]", value)


def _as_str_list(value: object) -> Sequence[str]:
    """Narrow a parsed TOML value to a list of strings, checking every element."""
    assert isinstance(value, list), f"expected a TOML list, got {type(value).__name__}"
    items = cast("Sequence[object]", value)
    for item in items:
        assert isinstance(item, str), f"expected a list of strings, found {item!r}"
    return cast("Sequence[str]", items)


def _load_pyproject() -> Mapping[str, object]:
    """Parse pyproject.toml into a structure with no Any in it."""
    parsed: object = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))  # pyright: ignore[reportAny]
    return _as_table(parsed)


def _table(parent: Mapping[str, object], *path: str) -> Mapping[str, object]:
    """Walk into a nested TOML table, asserting each level exists and is a table."""
    current = parent
    for key in path:
        assert key in current, f"pyproject.toml is missing [{'.'.join(path)}]"
        current = _as_table(current[key])
    return current


def _string_list(table: Mapping[str, object], key: str) -> Sequence[str]:
    assert key in table, f"{key} is missing"
    return _as_str_list(table[key])


def _run_mypy(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # fixed argv, no shell
        [sys.executable, "-m", "mypy", str(target)],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
        timeout=CHECK_TIMEOUT_S,
    )


# --- The core guarantee --------------------------------------------------


@pytest.mark.slow
def test_unhandled_error_variant_fails_the_type_check(tmp_path: Path) -> None:
    """Forgetting a drive-error case must not be able to reach production.

    tests/typing_fixtures/incomplete_match.py handles CommTimeout and omits
    BadCrc. The checker must reject it and must name the variant that was
    missed, so the developer is told what to fix rather than merely that
    something is wrong.

    The fixture is copied out of tests/typing_fixtures/ first: that directory
    is excluded in pyproject.toml (so the normal pass stays green), and the
    exclusion would otherwise make this check silently vacuous.
    """
    target = tmp_path / "incomplete_match.py"
    target.write_text(INCOMPLETE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    result = _run_mypy(target)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, (
        "mypy ACCEPTED a match that fails to handle one of the error variants. "
        "Exhaustiveness checking is no longer working, so the Result design is "
        f"providing no guarantee.\n{combined}"
    )
    assert "BadCrc" in combined, (
        "the checker rejected the fixture but did not name the missing variant "
        f"(BadCrc), so the diagnostic is not actionable.\n{combined}"
    )
    assert "assert_never" in combined, (
        f"the failure did not come from the assert_never exhaustiveness check.\n{combined}"
    )


# --- The configuration that makes it work -------------------------------


def test_mypy_is_configured_strictly() -> None:
    """The settings that make zero-Any real, asserted so they cannot be dropped quietly."""
    mypy_config = _table(_load_pyproject(), "tool", "mypy")
    for flag in (
        "strict",
        "disallow_any_explicit",
        "disallow_any_generics",
        "warn_return_any",
        "strict_equality",
    ):
        assert mypy_config.get(flag) is True, f"mypy.{flag} must stay enabled"

    error_codes = _string_list(mypy_config, "enable_error_code")
    # Without ignore-without-code a bare `# type: ignore` silences everything on
    # its line; without exhaustive-match the assert_never proof weakens; and
    # redundant-expr is what still catches dead comparisons now that
    # basedpyright's reportUnnecessaryComparison is off (see pyproject.toml).
    for code in (
        "ignore-without-code",
        "exhaustive-match",
        "possibly-undefined",
        "redundant-expr",
    ):
        assert code in error_codes, f"mypy error code {code!r} must stay enabled"


def test_basedpyright_is_configured_strictly() -> None:
    config = _table(_load_pyproject(), "tool", "basedpyright")
    assert config.get("typeCheckingMode") == "strict"
    for report in (
        "reportAny",
        "reportExplicitAny",
        "reportUnknownMemberType",
        "reportUnknownArgumentType",
        "reportUnknownVariableType",
        "reportMissingTypeStubs",
    ):
        assert config.get(report) == "error", f"basedpyright {report} must stay an error"


#: Safety-chain modules still outside the coverage gate. Pinned here so the
#: debt can only be paid down, never quietly extended: adding a module to
#: pyproject.toml's coverage_pending without editing this literal fails the
#: test below.
EXPECTED_COVERAGE_PENDING: frozenset[str] = frozenset(
    {
        "src/signal_processing.py",
    }
)


def test_coverage_gate_is_set_to_one_hundred_percent() -> None:
    report = _table(_load_pyproject(), "tool", "coverage", "report")
    assert report.get("fail_under") == 100


def test_every_safety_chain_module_is_gated_or_explicitly_pending() -> None:
    """Nothing in the safety chain may simply fall off the radar.

    Each module either sits under the 100% gate or is named in
    coverage_pending. There is no third state, so a module cannot be dropped
    from the gate without the omission being recorded.
    """
    pyproject = _load_pyproject()
    included = set(_string_list(_table(pyproject, "tool", "coverage", "report"), "include"))
    anheart = _table(pyproject, "tool", "anheart")
    pending = set(_string_list(anheart, "coverage_pending"))
    safety_chain = set(_string_list(anheart, "safety_chain"))

    unaccounted = safety_chain - included - pending
    assert not unaccounted, (
        "these safety-chain modules are neither gated nor listed as pending, so "
        f"nothing is tracking them: {sorted(unaccounted)}"
    )
    overlap = included & pending
    assert not overlap, f"listed as both gated and pending: {sorted(overlap)}"


def test_coverage_debt_does_not_grow() -> None:
    """coverage_pending must match the pinned set exactly.

    Shrinking it means editing both pyproject.toml and this test, which is the
    point: paying the debt down is deliberate, and adding to it is impossible
    to do by accident.
    """
    pending = set(_string_list(_table(_load_pyproject(), "tool", "anheart"), "coverage_pending"))
    added = sorted(pending - EXPECTED_COVERAGE_PENDING)
    assert not added, (
        "new modules were excluded from the 100% coverage gate: "
        f"{added}. The safety chain does not accept new debt."
    )
    removed = sorted(EXPECTED_COVERAGE_PENDING - pending)
    assert not removed, (
        f"{removed} are now covered -- remove them from EXPECTED_COVERAGE_PENDING "
        "so the list keeps reflecting reality."
    )


def test_safety_chain_lists_the_modules_the_heart_rate_flows_through() -> None:
    """The declared safety chain must not silently lose a member."""
    safety_chain = set(_string_list(_table(_load_pyproject(), "tool", "anheart"), "safety_chain"))
    for required in (
        "src/units.py",
        "src/result.py",
        "src/clock.py",
        "src/motor/*",
        "src/training/*",
        "src/sim/*",
        "src/bitalino_client.py",
        "src/signal_processing.py",
        "src/geometry.py",
        "src/ecg_pipeline.py",
        "src/local_config.py",
        "src/bitalino_rfcomm_macos.py",
        "src/local_panel.py",
        "src/panel_status.py",
    ):
        assert required in safety_chain, (
            f"{required} dropped out of the declared safety chain; the heart "
            "rate that drives the motor passes through it"
        )


def test_branch_coverage_is_enabled() -> None:
    """Line coverage alone would miss an untested `if` with no else."""
    run = _table(_load_pyproject(), "tool", "coverage", "run")
    assert run.get("branch") is True


def test_record_contract_rejects_unknown_events_clock_mixups_and_unhandled_errors(
    tmp_path: Path,
) -> None:
    fixture = PROJECT_ROOT / "tests" / "typing_fixtures" / "record_contract.py"
    target = tmp_path / "record_contract.py"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    checked = _run_mypy(target)
    assert checked.returncode != 0
    for rejected_type in ("EventKind", "Monotonic", "Err[RecordError]"):
        assert rejected_type in checked.stdout
