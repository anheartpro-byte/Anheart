# AnHeart Raspberry Pi Client

Python application for collecting BITalino ECG data and streaming to the AnHeart cloud platform.

## Features

- Bluetooth Classic connection to BITalino devices (BITalino, psychoBIT, etc.)
- Real-time ECG data streaming to Convex backend
- Offline data buffering when network unavailable
- Automatic reconnection and recovery
- Systemd service for auto-start

## Requirements

### Hardware

- Raspberry Pi 4 or 5 (or any Linux computer with Bluetooth)
- BITalino device (BITalino (r)evolution, psychoBIT, etc.)
- WiFi or Ethernet connection

### Software

- Python 3.9+ (tested with 3.11, 3.12, 3.14)
- Bluetooth enabled and working
- System packages for PyBluez

---

## Installation

### Step 1: Install System Dependencies

The `bitalino` library requires PyBluez, which needs system-level Bluetooth libraries.

**On Raspberry Pi / Debian / Ubuntu:**

```bash
sudo apt update
sudo apt install -y \
    python3-dev \
    python3-venv \
    bluetooth \
    libbluetooth-dev \
    bluez \
    bluez-tools
```

**On Arch Linux / CachyOS:**

```bash
sudo pacman -S python bluez bluez-utils
```

**On Fedora:**

```bash
sudo dnf install python3-devel bluez bluez-libs-devel
```

### Step 2: Enable Bluetooth

```bash
# Start bluetooth service
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# Make sure Bluetooth is not blocked
sudo rfkill unblock bluetooth

# Power on the adapter
bluetoothctl power on
```

### Step 3: Create Virtual Environment

```bash
cd raspberry-pi

# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate
```

### Step 4: Install Python Dependencies

```bash
# Upgrade pip first
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt
```

**If `pip install` fails with PyBluez errors:**

```bash
# Try installing PyBluez separately first
pip install PyBluez

# If that fails, try the bitalino-specific fork
pip install PyBluez-bitalino

# Then install the rest
pip install -r requirements.txt
```

**Alternative: Install from system packages (Raspberry Pi):**

```bash
# On Raspberry Pi OS, you can use apt
sudo apt install python3-bluez

# Then create venv with system packages access
python3 -m venv venv --system-site-packages
source venv/bin/activate
pip install -r requirements.txt
```

### Step 5: Pair Your BITalino Device

Before the application can connect, you need to pair the BITalino with your system.

```bash
# Start bluetoothctl
bluetoothctl

# Inside bluetoothctl:
agent on
default-agent
scan on

# Wait for your BITalino to appear (e.g., "BITalino-XX-XX")
# Note the MAC address (format: XX:XX:XX:XX:XX:XX)

# Pair with the device (PIN is usually 1234)
pair XX:XX:XX:XX:XX:XX
# Enter PIN: 1234

# Trust the device
trust XX:XX:XX:XX:XX:XX

# Exit
quit
```

### Step 6: Configure

```bash
# Copy example config
cp .env.example .env

# Edit with your values
nano .env
```

Required settings in `.env`:

```bash
# Convex URL (use .convex.site for HTTP endpoints)
CONVEX_URL=https://your-project.convex.site

# Machine API key (get from web dashboard when creating a machine)
MACHINE_API_KEY=your_64_character_api_key_here

# BITalino MAC address (from pairing step)
BITALINO_MAC=XX:XX:XX:XX:XX:XX

# Sample rate (100 Hz recommended for monitoring)
SAMPLE_RATE=100
```

### Step 7: Test the Connection

```bash
# Activate venv if not already
source venv/bin/activate

# Test BITalino discovery
python scripts/discover_devices.py

# Test BITalino connection and data
python scripts/test_bitalino.py --mac XX:XX:XX:XX:XX:XX

# Run the full application
python -m src.main --debug
```

---

## Configuration Reference

| Variable             | Required | Default   | Description                             |
| -------------------- | -------- | --------- | --------------------------------------- |
| CONVEX_URL           | Yes      | -         | Convex deployment URL (.convex.site)    |
| MACHINE_API_KEY      | Yes      | -         | 64-character API key from dashboard     |
| BITALINO_MAC         | Yes      | -         | BITalino Bluetooth MAC address          |
| SAMPLE_RATE          | No       | 100       | Sample rate in Hz (1, 10, 100, or 1000) |
| BATCH_INTERVAL_MS    | No       | 1000      | How often to send data (ms)             |
| HEARTBEAT_INTERVAL_S | No       | 30        | Heartbeat interval (seconds)            |
| LOG_LEVEL            | No       | INFO      | Logging level (DEBUG, INFO, WARNING)    |
| BUFFER_DB_PATH       | No       | buffer.db | Path for offline data buffer            |

### Sample Rate Recommendations

| Rate    | Use Case                        | Data per minute |
| ------- | ------------------------------- | --------------- |
| 100 Hz  | Monitoring, basic visualization | 6,000 samples   |
| 250 Hz  | Detailed analysis               | 15,000 samples  |
| 500 Hz  | Clinical quality                | 30,000 samples  |
| 1000 Hz | Research, maximum detail        | 60,000 samples  |

**Recommendation:** Use **100 Hz** for most monitoring applications.

---

## Running as a Service

### Install Service

```bash
# Make install script executable
chmod +x scripts/install.sh

# Run install (requires sudo)
sudo ./scripts/install.sh
```

### Control Service

```bash
# Start
sudo systemctl start anheart

# Stop
sudo systemctl stop anheart

# Restart
sudo systemctl restart anheart

# Check status
sudo systemctl status anheart

# Enable auto-start on boot
sudo systemctl enable anheart

# View logs
journalctl -u anheart -f
```

---

## Troubleshooting

### "pip install" fails with PyBluez errors

**Error:** `error: command 'gcc' failed` or `bluetooth/bluetooth.h: No such file`

**Solution:** Install Bluetooth development libraries:

```bash
# Debian/Ubuntu/Raspberry Pi
sudo apt install libbluetooth-dev python3-dev

# Then retry
pip install -r requirements.txt
```

### Cannot find BITalino device

**Error:** Device not appearing in scan

**Solutions:**

1. Make sure BITalino is powered ON
2. Check Bluetooth is enabled:
   ```bash
   bluetoothctl power on
   rfkill unblock bluetooth
   ```
3. Try scanning manually:
   ```bash
   bluetoothctl
   scan on
   # Wait 10-15 seconds
   ```
4. Power cycle the BITalino device

### Connection refused / Device busy

**Error:** `[Errno 16] Device or resource busy` or `Connection refused`

**Solutions:**

1. Make sure no other application is using the BITalino
2. Check if already paired:
   ```bash
   bluetoothctl info XX:XX:XX:XX:XX:XX
   ```
3. Remove and re-pair:
   ```bash
   bluetoothctl remove XX:XX:XX:XX:XX:XX
   bluetoothctl pair XX:XX:XX:XX:XX:XX
   ```

### Server connection fails

**Error:** `HTTP 404` or `Invalid API key`

**Solutions:**

1. Use `.convex.site` URL (not `.convex.cloud`)
2. Check API key is exactly 64 characters
3. Verify machine exists in web dashboard
4. Test connectivity:
   ```bash
   curl -X POST "https://your-project.convex.site/api/machine/heartbeat" \
     -H "Authorization: Bearer YOUR_API_KEY"
   ```

### Data not appearing in dashboard

**Solutions:**

1. Create a session from the web UI first
2. Check the RPi client logs for errors
3. Verify machine status is "online" in dashboard
4. Check ECG electrodes are connected properly

### Signal quality is "Poor"

**Cause:** ECG electrodes not connected or poor contact

**Solutions:**

1. Connect electrodes to body:
   - Red (RA) → Right arm/wrist
   - Black (LA) → Left arm/wrist
   - White (REF) → Right leg or lower torso
2. Use electrode gel for better contact
3. Clean skin before applying electrodes

---

## Development

### Run Tests

```bash
source venv/bin/activate
pytest
```

### Run with Debug Logging

```bash
python -m src.main --debug
```

### Project Structure

```
raspberry-pi/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration loading
│   ├── bitalino_client.py   # BITalino Bluetooth Classic client
│   ├── convex_client.py     # HTTP client for Convex
│   ├── data_buffer.py       # SQLite offline buffer
│   └── session_manager.py   # State machine for sessions
├── scripts/
│   ├── discover_devices.py  # Find BITalino devices
│   ├── test_bitalino.py     # Test BITalino connection
│   ├── pair_device.sh       # Pairing helper
│   ├── install.sh           # Service installer
│   └── anheart.service      # Systemd service file
├── tests/
│   ├── test_config.py
│   ├── test_bitalino.py
│   ├── test_convex_client.py
│   └── test_buffer.py
├── requirements.txt
├── .env.example
├── .env                     # Your configuration (not in git)
└── README.md
```

---

## ECG Data Format

The BITalino sends data as 10-bit ADC values (0-1023):

- **Baseline:** ~512 (when no signal)
- **Peaks:** Values above/below baseline represent ECG waveform
- **Sample rate:** Configurable (100 Hz recommended)

Each data batch contains:

```json
{
  "sessionId": "session_id",
  "timestamp": 1234567890123,
  "samples": [
    {
      "channel": "ECG",
      "values": [512, 515, 520, 890, 520, 510, ...]
    }
  ]
}
```

---

## Support

For issues and feature requests, please open an issue on GitHub.
