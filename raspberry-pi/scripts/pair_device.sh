#!/bin/bash
# Script to help pair BITalino/psychoBIT devices
# Default PIN for BITalino is 1234

MAC="${1:-98:D3:91:FE:4E:9F}"
PIN="${2:-1234}"

echo "=============================================="
echo "BITalino Pairing Helper"
echo "=============================================="
echo ""
echo "Device: $MAC"
echo "PIN: $PIN"
echo ""
echo "Please follow these steps:"
echo ""
echo "1. Make sure the BITalino is powered ON"
echo "2. Run: bluetoothctl"
echo "3. Inside bluetoothctl, run these commands:"
echo ""
echo "   agent on"
echo "   default-agent"
echo "   pair $MAC"
echo "   (enter PIN: $PIN when prompted)"
echo "   trust $MAC"
echo "   quit"
echo ""
echo "=============================================="
echo ""
echo "Attempting automatic pairing..."
echo ""

# Try to pair using bluetoothctl with expect-like behavior
{
    echo "agent on"
    echo "default-agent"
    sleep 1
    echo "pair $MAC"
    sleep 5
    echo "$PIN"
    sleep 2
    echo "trust $MAC"
    sleep 1
    echo "quit"
} | bluetoothctl

echo ""
echo "Checking pairing status..."
bluetoothctl info $MAC | grep -E "Paired|Trusted|Connected"
