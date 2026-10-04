#!/usr/bin/env bash
# Copy the console to a Raspberry Pi and (re)build it there.
#
#     bash scripts/pi/deploy.sh pi@anheart-pi.local            # copy + build
#     bash scripts/pi/deploy.sh pi@anheart-pi.local --start    # ... and start
#
# Run from the development machine. The Pi's own .env and data/ are never
# overwritten: they are the machine's, not the repository's.
#
# --start refuses to act while a session is running on the Pi: replacing the
# console under a turning machine is not something a script decides.
set -euo pipefail

target="${1:-}"
action="${2:-}"
if [ -z "$target" ]; then
    echo "usage: $0 user@host [--start]" >&2
    exit 2
fi

here="$(cd "$(dirname "$0")/../.." && pwd)"
remote_dir="anheart/raspberry-pi"

echo "== copy to $target:~/$remote_dir"
ssh "$target" "mkdir -p ~/$remote_dir"
rsync -az --delete \
    --exclude '.env' --exclude 'data/' --exclude '.venv/' --exclude 'venv/' \
    --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' --exclude '.hypothesis/' --exclude '.coverage' \
    --exclude 'tests/' --exclude 'stubs/' \
    "$here/" "$target:~/$remote_dir/"

echo "== build the image on the Pi (the first build takes several minutes)"
ssh "$target" "cd ~/$remote_dir && docker compose build"

if [ "$action" != "--start" ]; then
    echo "Copied and built. On the Pi: bash scripts/pi/preflight.sh && docker compose up -d"
    exit 0
fi

echo "== preflight"
ssh "$target" "cd ~/$remote_dir && bash scripts/pi/preflight.sh"

echo "== is a session running?"
state="$(ssh "$target" "cd ~/$remote_dir && port=\$(grep -E '^UI_PORT=' .env | cut -d= -f2); curl -s --max-time 3 http://127.0.0.1:\${port:-8080}/api/status" || true)"
case "$state" in
    *'"run_state":"idle"'*|"") ;;
    *) echo "The console on the Pi is not idle. Stop the session at the machine first." >&2; exit 1 ;;
esac

echo "== start"
ssh "$target" "cd ~/$remote_dir && docker compose up -d && sleep 8 && docker compose ps && docker compose logs --tail 12"
