#!/usr/bin/env bash
# The gate. Nothing merges unless this passes.
#
# Run from raspberry-pi/ (used on the Pi and in CI):
#     ./scripts/check.sh
#
# Stages run cheapest-first, but all of them run even if an earlier one fails:
# seeing all the damage at once beats one class of problem per round-trip.
#
# With nothing set, the test stage is the single pytest it has always been.
# Three variables run it as several independent pytest processes instead, all
# through ../scripts/ci/pi_gate_parallel.py: same tests, same combined branch
# coverage, same threshold, and the gate fails unless every process exits
# cleanly and every test is proven to have run exactly once.
#
#   PI_GATE_PROCESSES=N
#       the whole suite here, as N processes, judged here: for one machine.
#       The runner is tested first.
#
#   PI_GATE_SHARES=0-3/8 PI_GATE_EVIDENCE=<dir>
#       lint and types as usual, then only shares 0 to 3 of the suite cut into
#       8, one process each. Their records and their coverage data are left in
#       <dir>. Fails if a process crashes, hangs or has a failing test. Proves
#       nothing about the other shares, and applies no threshold.
#
#   PI_GATE_COMBINE=8 PI_GATE_EVIDENCE=<dir>
#       no lint, no types, no test of the console: <dir> holds what the jobs
#       that ran the 8 shares left. Tests the runner itself, then proves that
#       every test ran in exactly one share, merges the coverage data and
#       applies the 100 % threshold once, to the total. The caller must also
#       require that each of those jobs succeeded.
#
# The CI cuts the suite the second way and judges it the third (jobs
# `pi-tests` and `pi-gate` of ../.github/workflows/ci.yml). The same two steps
# reproduce it on one machine: PI_GATE_SHARES=0-7/8, then PI_GATE_COMBINE=8,
# with the same <dir>.
#
# QUALITY_REPORT_DIR=<dir> (optional; the CI jobs set it) keeps in <dir> what
# the quality report of the run reads (../scripts/ci/quality-report.mjs): how
# each stage below ended (stages.tsv), the JUnit file of each process started
# here and, where the coverage is judged, the coverage as JSON. It changes no
# verdict.
#
# See .claude/skills/anheart-strict-python/SKILL.md
set -uo pipefail

PROCESSES="${PI_GATE_PROCESSES:-}"
SHARES="${PI_GATE_SHARES:-}"
COMBINE="${PI_GATE_COMBINE:-}"
EVIDENCE="${PI_GATE_EVIDENCE:-}"
ways=0
for way in "$PROCESSES" "$SHARES" "$COMBINE"; do
    if [ -n "$way" ]; then ways=$((ways + 1)); fi
done
if [ "$ways" -gt 1 ]; then
    echo "PI_GATE_PROCESSES, PI_GATE_SHARES and PI_GATE_COMBINE are three ways to run the tests: set one." >&2
    exit 1
fi
if [ -n "$SHARES$COMBINE" ] && [ -z "$EVIDENCE" ]; then
    echo "PI_GATE_EVIDENCE must name the directory the shares are kept in." >&2
    exit 1
fi
if [ -z "$SHARES$COMBINE" ] && [ -n "$EVIDENCE" ]; then
    echo "PI_GATE_EVIDENCE goes with PI_GATE_SHARES or PI_GATE_COMBINE." >&2
    exit 1
fi

# Named from where the gate was called, before this script changes directory.
case "$EVIDENCE" in
    "" | /*) ;;
    *) EVIDENCE="$PWD/$EVIDENCE" ;;
esac
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

RUNNER="../scripts/ci/pi_gate_parallel.py"
RUNNER_TESTS="../scripts/ci/test_pi_gate_parallel.py"

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

# Everywhere but where the shares of other jobs are judged: each of those jobs
# ran these checks, on the same files.
if [ -z "$COMBINE" ]; then
    stage "lint (ruff)"                              "$PY" -m ruff check .
    stage "format (ruff)"                            "$PY" -m ruff format --check .
    stage "types (basedpyright, strict, zero Any)"   "$BPR"
    stage "types (mypy, strict)"                     "$PY" -m mypy .

    # The gate's own helpers live outside this directory, in ../scripts/ci: the
    # parallel runner, its pytest plugin and their tests. Code that decides the
    # gate gets the same four checkers and the same rules as the code above
    # (../scripts/ci/pyproject.toml only points back at pyproject.toml here).
    # Skipped, and said so, only in a copy of raspberry-pi/ that came without them.
    HELPERS=("$RUNNER" ../scripts/ci/pi_gate_shard.py "$RUNNER_TESTS")
    if [ -d ../scripts/ci ]; then
        stage "gate helpers: lint (ruff)"            "$PY" -m ruff check "${HELPERS[@]}"
        stage "gate helpers: format (ruff)"          "$PY" -m ruff format --check "${HELPERS[@]}"
        stage "gate helpers: types (basedpyright)"   "$BPR" --project ../scripts/ci
        stage "gate helpers: types (mypy)"           env MYPYPATH=../scripts/ci "$PY" -m mypy --config-file pyproject.toml "${HELPERS[@]}"
    else
        printf '\n=== gate helpers: ../scripts/ci is not here, nothing to check ===\n'
    fi
fi

if [ -n "$SHARES" ]; then
    stage "tests, shares $SHARES"                    "$PY" "$RUNNER" --shares "$SHARES" --evidence "$EVIDENCE" ${REPORT:+--report "$REPORT"}
elif [ -n "$COMBINE" ]; then
    stage "parallel runner (its own tests)"          "$PY" -m pytest "$RUNNER_TESTS" ${REPORT:+"--junitxml=$REPORT/junit-gate-runner.xml"}
    stage "tests, $COMBINE shares: every test once + 100% branch coverage" \
                                                     "$PY" "$RUNNER" --combine "$COMBINE" --evidence "$EVIDENCE" --fail-under 100 ${REPORT:+--report "$REPORT"}
elif [ -n "$PROCESSES" ]; then
    stage "parallel runner (its own tests)"          "$PY" -m pytest "$RUNNER_TESTS" ${REPORT:+"--junitxml=$REPORT/junit-gate-runner.xml"}
    stage "tests + 100% branch coverage"             "$PY" "$RUNNER" --processes "$PROCESSES" --fail-under 100 ${REPORT:+--report "$REPORT"}
else
    stage "tests + 100% branch coverage"             "$PY" -m pytest --cov --cov-branch --cov-fail-under=100
fi

echo
if [ ${#failures[@]} -gt 0 ]; then
    printf 'GATE FAILED: %s\n' "$(IFS=', '; echo "${failures[*]}")" >&2
    exit 1
fi
echo "GATE PASSED"
