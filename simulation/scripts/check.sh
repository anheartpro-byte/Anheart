#!/usr/bin/env bash
# The simulation gate: lint, both type checkers, the scenario battery + coverage.
#
# Run from anywhere:
#     simulation/scripts/check.sh
#
# Uses raspberry-pi/.venv (the same interpreter and tool versions as the code
# under test). Every stage runs even if an earlier one fails.
#
# With nothing set, the battery is the single pytest it has always been. The
# CI cuts it into shares run by several jobs, with two variables (see
# ../scripts/ci/pi_gate_parallel.py, the runner the Pi gate already uses):
#
#   SIMULATION_GATE_SHARES=5-8/13 SIMULATION_GATE_EVIDENCE=<dir>
#       lint and types as usual, then only shares 5 to 8 of the battery cut
#       into 13, one independent pytest process each. Their records and their
#       coverage data are left in <dir>. Fails if a process crashes, hangs or
#       has a failing test. Proves nothing about the other shares.
#
#   SIMULATION_GATE_COMBINE=13 SIMULATION_GATE_EVIDENCE=<dir>
#       no lint, no types, no test: <dir> holds what the jobs that ran the 13
#       shares left. Tests the runner itself, then proves that every test ran
#       in exactly one share, merges the coverage data and applies the 100 %
#       threshold once, to the total. The caller must also require that each
#       of those jobs succeeded.
#
# The same two steps reproduce the CI on one machine: SIMULATION_GATE_SHARES=0-12/13,
# then SIMULATION_GATE_COMBINE=13, with the same <dir>.
set -uo pipefail

SHARES="${SIMULATION_GATE_SHARES:-}"
COMBINE="${SIMULATION_GATE_COMBINE:-}"
EVIDENCE="${SIMULATION_GATE_EVIDENCE:-}"
if [ -n "$SHARES" ] && [ -n "$COMBINE" ]; then
    echo "SIMULATION_GATE_SHARES and SIMULATION_GATE_COMBINE are two different steps: set one." >&2
    exit 1
fi
if [ -n "$SHARES$COMBINE" ] && [ -z "$EVIDENCE" ]; then
    echo "SIMULATION_GATE_EVIDENCE must name the directory the shares are kept in." >&2
    exit 1
fi
if [ -z "$SHARES$COMBINE" ] && [ -n "$EVIDENCE" ]; then
    echo "SIMULATION_GATE_EVIDENCE goes with SIMULATION_GATE_SHARES or SIMULATION_GATE_COMBINE." >&2
    exit 1
fi
# Named from where the gate was called, before this script changes directory.
case "$EVIDENCE" in
    "" | /*) ;;
    *) EVIDENCE="$PWD/$EVIDENCE" ;;
esac

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

RUNNER="../scripts/ci/pi_gate_parallel.py"

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

if [ -z "$COMBINE" ]; then
    stage "lint (ruff)"                              "$PY" -m ruff check .
    stage "format (ruff)"                            "$PY" -m ruff format --check .
    stage "types (basedpyright, strict, zero Any)"   "$BPR"
    stage "types (mypy, strict)"                     "$PY" -m mypy --config-file pyproject.toml -p simulation
fi

if [ -n "$SHARES" ]; then
    stage "scenario battery, shares $SHARES"         "$PY" "$RUNNER" --suite simulation --shares "$SHARES" --evidence "$EVIDENCE"
elif [ -n "$COMBINE" ]; then
    stage "parallel runner (its own tests)"          "$PY" -m pytest ../scripts/ci/test_pi_gate_parallel.py
    stage "scenario battery, $COMBINE shares: every test once + coverage" \
                                                     "$PY" "$RUNNER" --suite simulation --combine "$COMBINE" --evidence "$EVIDENCE" --fail-under 100
else
    stage "scenario battery + coverage"              "$PY" -m pytest --cov --cov-branch "$@"
fi

echo
if [ ${#failures[@]} -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "$(IFS=', '; echo "${failures[*]}")" >&2
    exit 1
fi
echo "GATE PASSED"
