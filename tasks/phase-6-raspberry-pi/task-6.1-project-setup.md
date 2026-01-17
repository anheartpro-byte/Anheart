# Task 6.1: Raspberry Pi Project Setup

## Objective

Create the Python project structure for the Raspberry Pi BITalino client.

## Dependencies

None - this is the foundation task.

---

## Acceptance Criteria

### Project Structure

- [ ] `raspberry-pi/` folder created
- [ ] `src/` package with `__init__.py`
- [ ] `tests/` package with `__init__.py`
- [ ] `scripts/` folder for installation scripts
- [ ] `requirements.txt` with all dependencies
- [ ] `.env.example` with required variables
- [ ] `README.md` with setup instructions

### Dependencies

- [ ] bleak (BLE library)
- [ ] httpx (async HTTP client)
- [ ] pydantic (data validation)
- [ ] python-dotenv (environment variables)
- [ ] pytest (testing)

### Configuration

- [ ] `config.py` loads from environment
- [ ] Validates required variables
- [ ] Provides defaults for optional values

---

## Implementation

```
mkdir -p raspberry-pi/src raspberry-pi/tests raspberry-pi/scripts
```

```python
# raspberry-pi/requirements.txt
bleak>=0.21.0
httpx>=0.27.0
pydantic>=2.0.0
python-dotenv>=1.0.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

```
# raspberry-pi/.env.example
# Convex Configuration
CONVEX_URL=https://your-project.convex.cloud
MACHINE_API_KEY=your-64-character-api-key

# BITalino Configuration
BITALINO_MAC=XX:XX:XX:XX:XX:XX

# Optional Settings
SAMPLE_RATE=1000
BATCH_INTERVAL_MS=1000
HEARTBEAT_INTERVAL_S=30
LOG_LEVEL=INFO
```

```python
# raspberry-pi/src/__init__.py
"""AnHeart Raspberry Pi Client"""
__version__ = "1.0.0"
```

```python
# raspberry-pi/src/config.py
"""Configuration management for AnHeart RPi client."""

import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()


class Config(BaseModel):
    """Application configuration."""

    # Required
    convex_url: str = Field(..., description="Convex deployment URL")
    machine_api_key: str = Field(..., description="Machine API key (64 chars)")
    bitalino_mac: str = Field(..., description="BITalino MAC address")

    # Optional with defaults
    sample_rate: int = Field(default=1000, description="Sample rate in Hz")
    batch_interval_ms: int = Field(default=1000, description="Batch interval in ms")
    heartbeat_interval_s: int = Field(default=30, description="Heartbeat interval in seconds")
    log_level: str = Field(default="INFO", description="Logging level")
    buffer_db_path: str = Field(default="buffer.db", description="Offline buffer database path")

    @classmethod
    def from_env(cls) -> "Config":
        """Load configuration from environment variables."""
        convex_url = os.getenv("CONVEX_URL")
        api_key = os.getenv("MACHINE_API_KEY")
        bitalino_mac = os.getenv("BITALINO_MAC")

        if not convex_url:
            raise ValueError("CONVEX_URL environment variable is required")
        if not api_key:
            raise ValueError("MACHINE_API_KEY environment variable is required")
        if not bitalino_mac:
            raise ValueError("BITALINO_MAC environment variable is required")

        return cls(
            convex_url=convex_url,
            machine_api_key=api_key,
            bitalino_mac=bitalino_mac,
            sample_rate=int(os.getenv("SAMPLE_RATE", "1000")),
            batch_interval_ms=int(os.getenv("BATCH_INTERVAL_MS", "1000")),
            heartbeat_interval_s=int(os.getenv("HEARTBEAT_INTERVAL_S", "30")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            buffer_db_path=os.getenv("BUFFER_DB_PATH", "buffer.db"),
        )


# Global config instance
config: Config | None = None


def get_config() -> Config:
    """Get or create configuration instance."""
    global config
    if config is None:
        config = Config.from_env()
    return config
```

```python
# raspberry-pi/tests/__init__.py
"""Tests for AnHeart RPi client."""
```

```python
# raspberry-pi/tests/test_config.py
"""Test configuration loading."""

import os
import pytest
from src.config import Config


def test_config_from_env():
    """Test loading config from environment."""
    os.environ["CONVEX_URL"] = "https://test.convex.cloud"
    os.environ["MACHINE_API_KEY"] = "a" * 64
    os.environ["BITALINO_MAC"] = "AA:BB:CC:DD:EE:FF"

    config = Config.from_env()

    assert config.convex_url == "https://test.convex.cloud"
    assert config.machine_api_key == "a" * 64
    assert config.bitalino_mac == "AA:BB:CC:DD:EE:FF"
    assert config.sample_rate == 1000
    assert config.batch_interval_ms == 1000


def test_config_missing_required():
    """Test error when required vars missing."""
    os.environ.pop("CONVEX_URL", None)

    with pytest.raises(ValueError, match="CONVEX_URL"):
        Config.from_env()
```

````markdown
# raspberry-pi/README.md

# AnHeart Raspberry Pi Client

Python application for BITalino ECG data collection and streaming to AnHeart cloud.

## Requirements

- Raspberry Pi 4 or 5
- Python 3.9+
- BITalino Core BT device

## Quick Start

```bash
# Clone or copy files
cd raspberry-pi

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
nano .env  # Edit with your values

# Run
python -m src.main
```
````

## Configuration

| Variable             | Required | Default | Description                         |
| -------------------- | -------- | ------- | ----------------------------------- |
| CONVEX_URL           | Yes      | -       | Convex deployment URL               |
| MACHINE_API_KEY      | Yes      | -       | 64-character API key from dashboard |
| BITALINO_MAC         | Yes      | -       | BITalino Bluetooth MAC address      |
| SAMPLE_RATE          | No       | 1000    | Sample rate in Hz                   |
| BATCH_INTERVAL_MS    | No       | 1000    | Batch interval in ms                |
| HEARTBEAT_INTERVAL_S | No       | 30      | Heartbeat interval in seconds       |

## Running as Service

```bash
sudo ./scripts/install.sh
sudo systemctl status anheart
```

## Testing

```bash
pytest
```

````

```bash
# raspberry-pi/scripts/install.sh
#!/bin/bash
set -e

echo "Installing AnHeart ECG Monitor..."

# Install dependencies
pip3 install -r requirements.txt

# Copy service file
sudo cp scripts/anheart.service /etc/systemd/system/

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable anheart
sudo systemctl start anheart

echo "Installation complete!"
echo "Check status: sudo systemctl status anheart"
````

```ini
# raspberry-pi/scripts/anheart.service
[Unit]
Description=AnHeart ECG Monitor
After=network.target bluetooth.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/anheart/raspberry-pi
Environment=PATH=/home/pi/anheart/raspberry-pi/venv/bin
ExecStart=/home/pi/anheart/raspberry-pi/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## Testing Steps

1. Create all files in `raspberry-pi/` directory
2. Create virtual environment: `python3 -m venv venv`
3. Activate: `source venv/bin/activate`
4. Install: `pip install -r requirements.txt`
5. Run config test: `pytest tests/test_config.py`
6. Verify tests pass
