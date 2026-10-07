#!/usr/bin/env bash
# The gate. Nothing merges unless this passes.
#
# Run from raspberry-pi/ (used on the Pi and in CI):
#     ./scripts/check.sh
#
# Stages run cheapest-first, but all of them run even if an earlier one fails:
# seeing all the damage at once beats one class of problem per round-trip.
#
# PI_GATE_PROCESSES=N (optional; the CI job sets it to the runner's CPU count)
# runs the same test stage as N independent pytest processes instead of one.
# Same tests, same combined branch coverage, same threshold; the gate fails
# unless every process exits cleanly and every test is proven to have run
# exactly once. The runner that does this is tested first, on every run. See
# ../scripts/ci/pi_gate_parallel.py. Unset, the test stage is the single
# pytest it has always been.
#
# QUALITY_REPORT_DIR=<dir> (optional; the CI job sets it) keeps in <dir> what
# the quality report of the run reads (../scripts/ci/quality-report.mjs): how
# each stage below ended (stages.tsv) and, from the test stage run as several
# processes, the JUnit file of each process and the coverage as JSON. It
# changes no verdict.
#
# See .claude/skills/anheart-strict-python/SKILL.md
set -uo pipefail

# Named from where the gate was called, before this script changes directory.
REPORT="${QUALITY_REPORT_DIR:-}"
case "$REPORT" in
    "" | /*) ;;
    *) REPORT="$PWD/$REPORT" ;;
esac

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

# Nothing below reads $REPORT back: a directory that cannot be written to is
# said, and the gate then runs exactly as it does without one.
if [ -n "$REPORT" ] && ! { mkdir -p "$REPORT" && : > "$REPORT/stages.tsv"; }; then
    echo "QUALITY_REPORT_DIR: nothing can be kept in $REPORT, the gate runs without it." >&2
    REPORT=""
fi

stage() {
    local name="$1"; shift
    local outcome=passed
    printf '\n=== %s ===\n' "$name"
    if ! "$@"; then
        failures+=("$name")
        outcome=failed
        printf 'FAILED: %s\n' "$name" >&2
    fi
    if [ -n "$REPORT" ]; then
        printf '%s\t%s\n' "$outcome" "$name" >> "$REPORT/stages.tsv"
    fi
}

stage "lint (ruff)"                              "$PY" -m ruff check .
stage "format (ruff)"                            "$PY" -m ruff format --check .
stage "types (basedpyright, strict, zero Any)"   "$BPR"
stage "types (mypy, strict)"                     "$PY" -m mypy .

# The gate's own helpers live outside this directory, in ../scripts/ci: the
# parallel runner, its pytest plugin and their tests. Code that decides the
# gate gets the same four checkers and the same rules as the code above
# (../scripts/ci/pyproject.toml only points back at pyproject.toml here).
# Skipped, and said so, only in a copy of raspberry-pi/ that came without them.
HELPERS=(../scripts/ci/pi_gate_parallel.py ../scripts/ci/pi_gate_shard.py ../scripts/ci/test_pi_gate_parallel.py)
if [ -d ../scripts/ci ]; then
    stage "gate helpers: lint (ruff)"            "$PY" -m ruff check "${HELPERS[@]}"
    stage "gate helpers: format (ruff)"          "$PY" -m ruff format --check "${HELPERS[@]}"
    stage "gate helpers: types (basedpyright)"   "$BPR" --project ../scripts/ci
    stage "gate helpers: types (mypy)"           env MYPYPATH=../scripts/ci "$PY" -m mypy --config-file pyproject.toml "${HELPERS[@]}"
else
    printf '\n=== gate helpers: ../scripts/ci is not here, nothing to check ===\n'
fi

if [ -n "${PI_GATE_PROCESSES:-}" ]; then
    stage "parallel runner (its own tests)"      "$PY" -m pytest ../scripts/ci/test_pi_gate_parallel.py ${REPORT:+"--junitxml=$REPORT/junit-gate-runner.xml"}
    stage "tests + 100% branch coverage"         "$PY" ../scripts/ci/pi_gate_parallel.py --processes "$PI_GATE_PROCESSES" --fail-under 100 ${REPORT:+--report "$REPORT"}
else
    stage "tests + 100% branch coverage"         "$PY" -m pytest --cov --cov-branch --cov-fail-under=100
fi

echo
if [ ${#failures[@]} -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "$(IFS=', '; echo "${failures[*]}")" >&2
    exit 1
fi
echo "GATE PASSED"
