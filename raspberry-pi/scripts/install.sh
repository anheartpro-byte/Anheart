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

# Install system dependencies for Bluetooth and Python compilation
echo "Installing system dependencies..."
apt-get update
apt-get install -y libbluetooth-dev bluetooth bluez expect

# rfcomm was removed in newer Raspberry Pi OS - install bluez-tools as alternative
apt-get install -y bluez-tools 2>/dev/null || echo "Note: bluez-tools not available, skipping"

# Check Python version and upgrade if needed
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2)
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d'.' -f2)
echo "Current Python version: $PYTHON_VERSION (major=$PYTHON_MAJOR, minor=$PYTHON_MINOR)"

# Check if Python >= 3.9 using integer comparison
NEEDS_UPGRADE=false
if [ "$PYTHON_MAJOR" -lt 3 ]; then
    NEEDS_UPGRADE=true
elif [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 9 ]; then
    NEEDS_UPGRADE=true
fi

if [ "$NEEDS_UPGRADE" = true ]; then
    echo ""
    echo "Python $PYTHON_VERSION is too old. Installing Python 3.11..."
    echo ""
    
    # Install Python 3.11 (available in Raspberry Pi OS repos or via deadsnakes)
    if apt-cache show python3.11 &>/dev/null; then
        # Python 3.11 available in repos
        apt-get install -y python3.11 python3.11-venv python3.11-dev
        PYTHON_CMD="python3.11"
    elif apt-cache show python3.10 &>/dev/null; then
        # Fallback to Python 3.10
        apt-get install -y python3.10 python3.10-venv python3.10-dev
        PYTHON_CMD="python3.10"
    else
        # Build from source as last resort (takes ~15 minutes on Pi 4)
        echo "Python 3.11 not in repos. Building from source..."
        echo "This will take 10-20 minutes on Raspberry Pi 4..."
        
        # Install build dependencies
        apt-get install -y build-essential zlib1g-dev libncurses5-dev \
            libgdbm-dev libnss3-dev libssl-dev libreadline-dev libffi-dev \
            libsqlite3-dev wget libbz2-dev
        
        # Download and build Python 3.11
        cd /tmp
        wget -q https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
        tar -xzf Python-3.11.9.tgz
        cd Python-3.11.9
        
        ./configure --enable-optimizations --with-ensurepip=install
        make -j$(nproc)
        make altinstall
        
        cd /tmp
        rm -rf Python-3.11.9 Python-3.11.9.tgz
        
        PYTHON_CMD="python3.11"
    fi
    
    # Verify installation
    NEW_VERSION=$($PYTHON_CMD --version 2>&1 | cut -d' ' -f2)
    NEW_MAJOR=$(echo "$NEW_VERSION" | cut -d'.' -f1)
    NEW_MINOR=$(echo "$NEW_VERSION" | cut -d'.' -f2)
    echo "Installed Python version: $NEW_VERSION"
    
    if [ "$NEW_MAJOR" -lt 3 ] || ([ "$NEW_MAJOR" -eq 3 ] && [ "$NEW_MINOR" -lt 9 ]); then
        echo "Error: Failed to install Python 3.9+. Please install manually."
        exit 1
    fi
else
    echo "Python version is OK (>= 3.9)"
    PYTHON_CMD="python3"
fi

# Install Python dev packages for the version we're using
apt-get install -y ${PYTHON_CMD}-dev ${PYTHON_CMD}-venv 2>/dev/null || apt-get install -y python3-dev python3-venv

echo "Using Python: $PYTHON_CMD ($($PYTHON_CMD --version))"

# Remove old venv if Python version changed
if [ -d "${INSTALL_DIR}/venv" ]; then
    VENV_PYTHON_VERSION=$("${INSTALL_DIR}/venv/bin/python" --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2 || echo "0")
    CURRENT_PYTHON_VERSION=$($PYTHON_CMD --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
    
    if [ "$VENV_PYTHON_VERSION" != "$CURRENT_PYTHON_VERSION" ]; then
        echo "Python version changed ($VENV_PYTHON_VERSION -> $CURRENT_PYTHON_VERSION). Recreating venv..."
        rm -rf "${INSTALL_DIR}/venv"
    fi
fi

if [ ! -d "${INSTALL_DIR}/venv" ]; then
    echo "Creating virtual environment with $PYTHON_CMD..."
    $PYTHON_CMD -m venv "${INSTALL_DIR}/venv"
fi

echo "Installing Python dependencies..."
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

# Setup Bluetooth auto-pairing if MAC address is configured
if [ -f "${INSTALL_DIR}/.env" ]; then
    BITALINO_MAC=$(grep -E "^BITALINO_MAC=" "${INSTALL_DIR}/.env" | cut -d'=' -f2 | tr -d '"' | tr -d "'")
    if [ -n "$BITALINO_MAC" ]; then
        echo ""
        echo "Setting up Bluetooth pairing for BITalino..."
        echo "MAC Address: $BITALINO_MAC"
        echo "PIN: 1234 (default)"
        
        # Run pairing script (as regular user, not root)
        sudo -u pi bash "${INSTALL_DIR}/scripts/pair_device.sh" "$BITALINO_MAC" "1234" || {
            echo "Auto-pairing failed. You may need to pair manually."
            echo "Run: ${INSTALL_DIR}/scripts/pair_device.sh $BITALINO_MAC"
        }
    fi
fi

echo ""
echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Configure: nano ${INSTALL_DIR}/.env"
echo "  2. Pair BITalino: ${INSTALL_DIR}/scripts/pair_device.sh"
echo "  3. Test manually: cd ${INSTALL_DIR} && ./venv/bin/python -m src.main"
echo "  4. Enable service: systemctl enable ${SERVICE_NAME}"
echo "  5. Start service: systemctl start ${SERVICE_NAME}"
echo "  6. Check status: systemctl status ${SERVICE_NAME}"
echo "  7. View logs: journalctl -u ${SERVICE_NAME} -f"
