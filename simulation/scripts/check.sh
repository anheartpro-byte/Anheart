#!/usr/bin/env bash
# The simulation gate: lint, both type checkers, the scenario battery + coverage.
#
# Run from anywhere:
#     simulation/scripts/check.sh
#
# Uses raspberry-pi/.venv (the same interpreter and tool versions as the code
# under test). Every stage runs even if an earlier one fails.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

VENV="../raspberry-pi/.venv"
if [ -x "$VENV/bin/python" ]; then
    PY="$VENV/bin/python"
    BPR="$VENV/bin/basedpyright"
elif [ -x "$VENV/Scripts/python.exe" ]; then
    PY="$VENV/Scripts/python.exe"
    BPR="$VENV/Scripts/basedpyright.exe"
else
    echo "No venv at raspberry-pi/.venv - create it first (see raspberry-pi/README.md)." >&2
    exit 1
fi

# `simulation.*` from the repository root, `src.*` from raspberry-pi/.
export PYTHONPATH="..:../raspberry-pi${PYTHONPATH:+:$PYTHONPATH}"

failures=()

stage() {
    local name="$1"; shift
    printf '\n=== %s ===\n' "$name"
    if ! "$@"; then
        failures+=("$name")
        printf 'FAILED: %s\n' "$name" >&2
    fi
}

stage "lint (ruff)"                              "$PY" -m ruff check .
stage "format (ruff)"                            "$PY" -m ruff format --check .
stage "types (basedpyright, strict, zero Any)"   "$BPR"
stage "types (mypy, strict)"                     "$PY" -m mypy --config-file pyproject.toml -p simulation
stage "scenario battery + coverage"              "$PY" -m pytest --cov --cov-branch "$@"

echo
if [ ${#failures[@]} -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "$(IFS=', '; echo "${failures[*]}")" >&2
    exit 1
fi
echo "GATE PASSED"
