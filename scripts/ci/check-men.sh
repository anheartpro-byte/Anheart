#!/usr/bin/env bash
# Threat IDs are defined by Markdown headings or the first cell of a table.
set -euo pipefail
docs_dir="${1:-docs}"
if [[ ! -f "$docs_dir/menaces.md" ]]; then
    echo 'MEN validation pending ANH-136: docs/menaces.md does not exist.'
    exit 0
fi
definitions=$(awk '
    /^#{1,6} +MEN-[0-9]+([[:space:]]|$)/ || /^\| *MEN-[0-9]+ *\|/ {
        match($0, /MEN-[0-9]+/)
        print substr($0, RSTART, RLENGTH)
    }
' "$docs_dir/menaces.md" | sort)
if [[ -z "$definitions" ]]; then
    echo 'menaces.md exists but defines no MEN identifiers.' >&2
    exit 1
fi
duplicates=$(printf '%s\n' "$definitions" | uniq -d)
if [[ -n "$duplicates" ]]; then
    printf 'Duplicate threat identifiers:\n%s\n' "$duplicates" >&2
    exit 1
fi
references=$(grep -rhoE 'MEN-[0-9]+' "$docs_dir" --include='*.md' | sort -u)
missing=$(comm -23 <(printf '%s\n' "$references") <(printf '%s\n' "$definitions"))
if [[ -n "$missing" ]]; then
    printf 'Undefined threat identifiers:\n%s\n' "$missing" >&2
    exit 1
fi
echo 'MEN identifiers resolved without duplicates.'
