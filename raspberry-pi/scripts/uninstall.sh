#!/bin/bash
set -e

SERVICE_NAME="anheart"

echo "Uninstalling AnHeart service..."

systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true

rm -f /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload

echo "Service uninstalled."
echo "Note: Application files in /home/pi/anheart were not removed."
