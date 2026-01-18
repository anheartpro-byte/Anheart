#!/bin/bash
set -e

INSTALL_DIR="/home/pi/anheart/raspberry-pi"
SERVICE_NAME="anheart"

echo "==================================="
echo "AnHeart ECG Monitor - Installation"
echo "==================================="

if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo for service installation"
    exit 1
fi

PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
if (( $(echo "$PYTHON_VERSION < 3.9" | bc -l) )); then
    echo "Error: Python 3.9+ required (found $PYTHON_VERSION)"
    exit 1
fi

if [ ! -d "${INSTALL_DIR}/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${INSTALL_DIR}/venv"
fi

echo "Installing dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo ""
    echo "WARNING: Configuration file not found!"
    echo "Copy .env.example to .env and configure:"
    echo "  cp ${INSTALL_DIR}/.env.example ${INSTALL_DIR}/.env"
    echo "  nano ${INSTALL_DIR}/.env"
    echo ""
fi

echo "Installing systemd service..."
cp "${INSTALL_DIR}/scripts/anheart.service" /etc/systemd/system/
systemctl daemon-reload

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Configure: nano ${INSTALL_DIR}/.env"
echo "  2. Test manually: cd ${INSTALL_DIR} && ./venv/bin/python -m src.main"
echo "  3. Enable service: systemctl enable ${SERVICE_NAME}"
echo "  4. Start service: systemctl start ${SERVICE_NAME}"
echo "  5. Check status: systemctl status ${SERVICE_NAME}"
echo "  6. View logs: journalctl -u ${SERVICE_NAME} -f"
