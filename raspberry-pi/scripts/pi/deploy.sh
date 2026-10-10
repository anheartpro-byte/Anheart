#!/usr/bin/env bash
# Copy the console's sources to a Raspberry Pi. It installs nothing there.
#
#     bash scripts/pi/deploy.sh pi@anheart-pi.local
#
# Run from the development machine, from raspberry-pi/. The machine's
# configuration (/etc/anheart/anheart.env) and its data (/var/lib/anheart) are
# outside the copied directory: this script cannot touch them.
#
# Installing, building the image and starting the console is ONE script, run on
# the Pi itself: scripts/install.sh (docs/pi-image.md). It is the one that
# refuses to replace the console while a session is running.
set -euo pipefail

target="${1:-}"
if [ -z "$target" ] || [ "$#" -ne 1 ]; then
    echo "usage: $0 user@host" >&2
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

echo "Copied. On the Pi: cd ~/$remote_dir && sudo bash scripts/install.sh"
