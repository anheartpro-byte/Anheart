#!/bin/bash
# Test script to verify Docker setup on Raspberry Pi
# Run this after copying files to Pi: ./scripts/test_docker.sh

set -e

echo "=============================================="
echo "AnHeart Docker Setup Test"
echo "=============================================="
echo ""

# Check Docker is installed
echo "1. Checking Docker installation..."
if ! command -v docker &> /dev/null; then
    echo "   ERROR: Docker not installed!"
    echo "   Run: curl -fsSL https://get.docker.com | sh"
    exit 1
fi
echo "   Docker version: $(docker --version)"

# Check Docker Compose
echo ""
echo "2. Checking Docker Compose..."
if docker compose version &> /dev/null; then
    echo "   Docker Compose version: $(docker compose version --short)"
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &> /dev/null; then
    echo "   docker-compose version: $(docker-compose --version)"
    COMPOSE_CMD="docker-compose"
else
    echo "   ERROR: Docker Compose not found!"
    exit 1
fi

# Check .env file
echo ""
echo "3. Checking configuration..."
if [ ! -f ".env" ]; then
    echo "   ERROR: .env file not found!"
    echo "   Run: cp .env.example .env && nano .env"
    exit 1
fi
echo "   .env file: OK"

# Check required env vars
source .env 2>/dev/null || true
if [ -z "$CONVEX_URL" ]; then
    echo "   ERROR: CONVEX_URL not set in .env"
    exit 1
fi
if [ -z "$MACHINE_API_KEY" ]; then
    echo "   ERROR: MACHINE_API_KEY not set in .env"
    exit 1
fi
if [ -z "$BITALINO_MAC" ]; then
    echo "   ERROR: BITALINO_MAC not set in .env"
    exit 1
fi
echo "   CONVEX_URL: $CONVEX_URL"
echo "   BITALINO_MAC: $BITALINO_MAC"
echo "   MACHINE_API_KEY: [set]"

# Check Bluetooth
echo ""
echo "4. Checking Bluetooth..."
if ! systemctl is-active --quiet bluetooth; then
    echo "   WARNING: Bluetooth service not running"
    echo "   Run: sudo systemctl start bluetooth"
else
    echo "   Bluetooth service: running"
fi

# Check if BITalino is paired
echo ""
echo "5. Checking BITalino pairing..."
if bluetoothctl info "$BITALINO_MAC" 2>/dev/null | grep -q "Paired: yes"; then
    echo "   BITalino paired: YES"
else
    echo "   WARNING: BITalino not paired!"
    echo "   Run: sudo bluetoothctl"
    echo "   Then: agent on -> default-agent -> pair $BITALINO_MAC -> (PIN: 1234) -> trust $BITALINO_MAC"
fi

# Build Docker image
echo ""
echo "6. Building Docker image..."
$COMPOSE_CMD build

# Test run (dry run)
echo ""
echo "7. Testing configuration import..."
docker run --rm \
    --env-file .env \
    anheart-rpi:latest \
    python -c "from src.config import get_config; c = get_config(); print(f'Config OK: {c.convex_url}')"

echo ""
echo "=============================================="
echo "All tests passed!"
echo "=============================================="
echo ""
echo "To start the service:"
echo "  $COMPOSE_CMD up -d"
echo ""
echo "To view logs:"
echo "  $COMPOSE_CMD logs -f"
echo ""
