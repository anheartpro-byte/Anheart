#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --help ]]; then
    echo 'Usage: bash scripts/ci/check-men.sh [docs-directory [verified-issues.tsv]]'
    exit 0
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec node "$script_dir/check-men.mjs" "${1:-$script_dir/../../docs}" "${2:-$script_dir/men-linear-issues.tsv}"
