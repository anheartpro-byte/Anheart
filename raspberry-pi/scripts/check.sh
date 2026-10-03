#!/usr/bin/env bash
# The gate. Nothing merges unless this passes.
#
# Run from raspberry-pi/ (used on the Pi and in CI):
#     ./scripts/check.sh
#
# Stages run cheapest-first, but all of them run even if an earlier one fails:
# seeing all the damage at once beats one class of problem per round-trip.
#
# See .claude/skills/anheart-strict-python/SKILL.md
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
    BPR=".venv/bin/basedpyright"
elif [ -x ".venv/Scripts/python.exe" ]; then
    PY=".venv/Scripts/python.exe"
    BPR=".venv/Scripts/basedpyright.exe"
else
    echo "No venv found at .venv - create it first:" >&2
    echo "    python -m venv .venv && . .venv/bin/activate" >&2
    echo "    pip install -r requirements-dev.txt" >&2
    echo "    pip install pyserial && pip install --no-deps bitalino" >&2
    exit 1
fi

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
stage "types (mypy, strict)"                     "$PY" -m mypy .
stage "tests + 100% branch coverage"             "$PY" -m pytest --cov --cov-branch --cov-fail-under=100

echo
if [ ${#failures[@]} -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "$(IFS=', '; echo "${failures[*]}")" >&2
    exit 1
fi
echo "GATE PASSED"
