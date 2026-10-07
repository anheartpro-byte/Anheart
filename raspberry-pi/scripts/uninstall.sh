#!/usr/bin/env bash
# Remove the AnHeart console service installed by scripts/install.sh.
#
#     sudo bash scripts/uninstall.sh
#
# Stops the console the way it is always stopped (SIGTERM: the speed reference
# is zeroed and the drive link released), then removes the systemd unit.
# Deliberately left in place, because they are the machine's and not this
# script's to delete: the configuration (/etc/anheart/anheart.env, with the
# machine's API key), the data (/var/lib/anheart: profiles and session
# records), the account `anheart`, the Docker images and the system packages.
set -euo pipefail

readonly SERVICE="anheart"

if [ "$(id -u)" -ne 0 ]; then
    echo "uninstall.sh: run it as root: sudo bash scripts/uninstall.sh" >&2
    exit 1
fi

echo "Stopping and disabling $SERVICE..."
systemctl disable --now "$SERVICE.service" 2>/dev/null || true

rm -f "/etc/systemd/system/$SERVICE.service"
rm -rf "/etc/systemd/system/$SERVICE.service.d"
systemctl daemon-reload

echo "Service removed."
echo "Left in place: /etc/anheart/anheart.env, /var/lib/anheart, the account anheart, the Docker images."
