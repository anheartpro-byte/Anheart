# AnHeart Raspberry Pi Client

Python application for collecting BITalino ECG data and streaming to the AnHeart cloud platform.

## Features

- BLE connection to BITalino Core BT
- Real-time data streaming to Convex backend
- Offline data buffering when network unavailable
- Automatic reconnection and recovery
- Systemd service for auto-start

## Requirements

- Raspberry Pi 4 or 5
- Python 3.9+
- BITalino Core BT device
- WiFi or Ethernet connection

## Quick Start

```bash
# Navigate to raspberry-pi folder
cd raspberry-pi

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Edit with your values

# Test connection
python -m src.main --debug
```

## Configuration

Edit `.env` file with your settings:

| Variable             | Required | Description                          |
| -------------------- | -------- | ------------------------------------ |
| CONVEX_URL           | Yes      | Convex deployment URL                |
| MACHINE_API_KEY      | Yes      | 64-character API key from dashboard  |
| BITALINO_MAC         | Yes      | BITalino Bluetooth MAC address       |
| SAMPLE_RATE          | No       | Sample rate in Hz (default: 1000)    |
| BATCH_INTERVAL_MS    | No       | Batch interval in ms (default: 1000) |
| HEARTBEAT_INTERVAL_S | No       | Heartbeat interval (default: 30)     |

### Finding BITalino MAC Address

```bash
# Scan for Bluetooth devices
bluetoothctl scan on
# Look for device named "BITalino" or similar
```

## Running as Service

```bash
# Install service (run with sudo)
sudo ./scripts/install.sh

# Control service
sudo systemctl start anheart
sudo systemctl stop anheart
sudo systemctl status anheart

# View logs
journalctl -u anheart -f
```

## Development

```bash
# Run tests
pytest

# Run with debug logging
python -m src.main --debug

# Run specific test
pytest tests/test_buffer.py -v
```

## Troubleshooting

### Cannot connect to BITalino

- Ensure Bluetooth is enabled: `sudo systemctl status bluetooth`
- Check MAC address is correct
- BITalino may need to be reset (power cycle)

### Connection to server fails

- Check CONVEX_URL is correct
- Verify API key is valid (64 characters)
- Check network connectivity: `ping google.com`

### Data not appearing in dashboard

- Check session was created in web UI
- Verify machine status is "online"
- Check logs: `journalctl -u anheart -f`

## File Structure

```
raspberry-pi/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration
│   ├── bitalino_client.py   # BITalino BLE
│   ├── convex_client.py     # HTTP client
│   ├── data_buffer.py       # Offline storage
│   └── session_manager.py   # State machine
├── tests/
│   ├── test_config.py
│   ├── test_bitalino.py
│   ├── test_convex_client.py
│   └── test_buffer.py
├── scripts/
│   ├── install.sh
│   └── anheart.service
├── requirements.txt
├── .env.example
└── README.md
```
