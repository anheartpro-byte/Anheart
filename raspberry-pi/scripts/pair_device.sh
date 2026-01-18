#!/bin/bash
# Automatic BITalino Bluetooth pairing script
# Default PIN for BITalino is 1234
#
# Usage: ./pair_device.sh [MAC_ADDRESS] [PIN]
# Example: ./pair_device.sh 98:D3:91:FE:4E:9F 1234

set -e

MAC="${1:-}"
PIN="${2:-1234}"

# If no MAC provided, try to get from .env file
if [ -z "$MAC" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    ENV_FILE="$SCRIPT_DIR/../.env"
    
    if [ -f "$ENV_FILE" ]; then
        MAC=$(grep -E "^BITALINO_MAC=" "$ENV_FILE" | cut -d'=' -f2 | tr -d '"' | tr -d "'")
    fi
fi

if [ -z "$MAC" ]; then
    echo "Error: No MAC address provided"
    echo "Usage: $0 <MAC_ADDRESS> [PIN]"
    echo "   or: Set BITALINO_MAC in .env file"
    exit 1
fi

echo "=============================================="
echo "BITalino Automatic Pairing"
echo "=============================================="
echo "Device: $MAC"
echo "PIN: $PIN"
echo ""

# Check if already paired
PAIRED=$(bluetoothctl info "$MAC" 2>/dev/null | grep "Paired: yes" || true)
if [ -n "$PAIRED" ]; then
    echo "Device is already paired!"
    bluetoothctl info "$MAC" | grep -E "Name|Paired|Trusted|Connected"
    exit 0
fi

echo "Setting up Bluetooth agent for automatic PIN entry..."

# Kill any existing bt-agent
pkill -f "bt-agent" 2>/dev/null || true

# Check if bt-agent is available (from bluez-tools package)
if command -v bt-agent &> /dev/null; then
    echo "Using bt-agent for automatic PIN entry..."
    
    # Start bt-agent in background with PIN
    bt-agent -c NoInputNoOutput -p "$PIN" &
    AGENT_PID=$!
    sleep 2
    
    # Now pair
    echo "Pairing with device..."
    bluetoothctl pair "$MAC" || true
    
    # Trust the device
    echo "Trusting device..."
    bluetoothctl trust "$MAC"
    
    # Kill agent
    kill $AGENT_PID 2>/dev/null || true
else
    echo "bt-agent not found, using expect-based approach..."
    
    # Check if expect is installed
    if ! command -v expect &> /dev/null; then
        echo "Installing expect..."
        sudo apt-get install -y expect
    fi
    
    # Use expect for automatic PIN entry
    expect << EOF
set timeout 30
spawn bluetoothctl
expect "#"
send "agent on\r"
expect "#"
send "default-agent\r"
expect "#"
send "pair $MAC\r"
expect {
    "PIN code:" {
        send "$PIN\r"
        exp_continue
    }
    "Passkey:" {
        send "$PIN\r"
        exp_continue
    }
    "Enter PIN code:" {
        send "$PIN\r"
        exp_continue
    }
    "Pairing successful" {
        puts "Pairing successful!"
    }
    "Already exists" {
        puts "Already paired"
    }
    "Failed" {
        puts "Pairing failed"
    }
    timeout {
        puts "Timeout waiting for pairing"
    }
}
sleep 2
send "trust $MAC\r"
expect "#"
send "quit\r"
expect eof
EOF
fi

echo ""
echo "=============================================="
echo "Pairing complete! Checking status..."
echo "=============================================="
bluetoothctl info "$MAC" | grep -E "Name|Paired|Trusted|Connected" || echo "Could not get device info"

# rfcomm was deprecated in newer BlueZ versions
# PyBluez connects directly via MAC address without needing rfcomm bind
# Only attempt if rfcomm command exists (older systems)
if command -v rfcomm &> /dev/null; then
    echo ""
    echo "Setting up RFCOMM serial binding (legacy)..."
    sudo rfcomm release 0 2>/dev/null || true
    sudo rfcomm bind 0 "$MAC" 1 2>/dev/null || echo "Note: rfcomm bind failed, but may not be needed"
fi

echo ""
echo "Done! The BITalino should now connect automatically."
echo "You can test with: python scripts/test_bitalino.py"
