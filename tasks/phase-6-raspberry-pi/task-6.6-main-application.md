# Task 6.6: Main Application

## Objective

Create the main entry point and systemd service for the Raspberry Pi application.

## Dependencies

- All previous Phase 6 tasks completed

---

## Acceptance Criteria

### Main Entry Point

- [ ] Loads configuration from environment
- [ ] Sets up logging
- [ ] Initializes and runs session manager
- [ ] Handles graceful shutdown (SIGTERM, SIGINT)
- [ ] Logs errors and restarts on failure

### Systemd Integration

- [ ] Service file configured
- [ ] Auto-start on boot
- [ ] Restart on failure
- [ ] Proper user permissions

### Command Line Options

- [ ] `--version` shows version
- [ ] `--config` allows custom .env path
- [ ] `--debug` enables debug logging

---

## Implementation

```python
# raspberry-pi/src/main.py
"""Main entry point for AnHeart Raspberry Pi client."""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from . import __version__
from .config import Config, get_config
from .session_manager import SessionManager


# Global for signal handling
_manager: SessionManager | None = None


def setup_logging(level: str) -> None:
    """Configure logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reduce noise from libraries
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="AnHeart ECG Monitor - Raspberry Pi Client"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"anheart-rpi {__version__}",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to .env configuration file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def handle_shutdown(signum, frame):
    """Handle shutdown signals."""
    global _manager
    logger = logging.getLogger(__name__)
    logger.info(f"Received signal {signum}, shutting down...")

    if _manager:
        # Create task to stop manager
        asyncio.create_task(_manager.stop())


async def main_async(config: Config) -> None:
    """Async main function."""
    global _manager
    logger = logging.getLogger(__name__)

    # Create session manager
    _manager = SessionManager(config)

    try:
        # Initialize
        await _manager.start()

        # Run main loop
        await _manager.run()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        await _manager.stop()


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Load custom config if specified
    if args.config:
        from dotenv import load_dotenv
        load_dotenv(args.config)

    # Load configuration
    try:
        config = get_config()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    # Setup logging
    log_level = "DEBUG" if args.debug else config.log_level
    setup_logging(log_level)

    logger = logging.getLogger(__name__)
    logger.info(f"Starting AnHeart RPi Client v{__version__}")
    logger.info(f"Convex URL: {config.convex_url}")
    logger.info(f"BITalino MAC: {config.bitalino_mac}")

    # Setup signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Run
    try:
        asyncio.run(main_async(config))
        return 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

```ini
# raspberry-pi/scripts/anheart.service
[Unit]
Description=AnHeart ECG Monitor
Documentation=https://github.com/yourrepo/anheart
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=pi
WorkingDirectory=/home/pi/anheart/raspberry-pi
Environment="PATH=/home/pi/anheart/raspberry-pi/venv/bin:/usr/bin"
ExecStart=/home/pi/anheart/raspberry-pi/venv/bin/python -m src.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=anheart

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/pi/anheart/raspberry-pi

[Install]
WantedBy=multi-user.target
```

```bash
# raspberry-pi/scripts/install.sh
#!/bin/bash
set -e

INSTALL_DIR="/home/pi/anheart/raspberry-pi"
SERVICE_NAME="anheart"

echo "==================================="
echo "AnHeart ECG Monitor - Installation"
echo "==================================="

# Check if running as root for service installation
if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo for service installation"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
if (( $(echo "$PYTHON_VERSION < 3.9" | bc -l) )); then
    echo "Error: Python 3.9+ required (found $PYTHON_VERSION)"
    exit 1
fi

# Create virtual environment if not exists
if [ ! -d "${INSTALL_DIR}/venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "${INSTALL_DIR}/venv"
fi

# Install dependencies
echo "Installing dependencies..."
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

# Check configuration
if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo ""
    echo "WARNING: Configuration file not found!"
    echo "Copy .env.example to .env and configure:"
    echo "  cp ${INSTALL_DIR}/.env.example ${INSTALL_DIR}/.env"
    echo "  nano ${INSTALL_DIR}/.env"
    echo ""
fi

# Install systemd service
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
```

```bash
# raspberry-pi/scripts/uninstall.sh
#!/bin/bash
set -e

SERVICE_NAME="anheart"

echo "Uninstalling AnHeart service..."

# Stop and disable service
systemctl stop ${SERVICE_NAME} 2>/dev/null || true
systemctl disable ${SERVICE_NAME} 2>/dev/null || true

# Remove service file
rm -f /etc/systemd/system/${SERVICE_NAME}.service
systemctl daemon-reload

echo "Service uninstalled."
echo "Note: Application files in /home/pi/anheart were not removed."
```

---

## Testing Steps

### Manual Testing

```bash
cd raspberry-pi
source venv/bin/activate

# Test with debug
python -m src.main --debug

# Test version
python -m src.main --version
```

### Service Testing

```bash
# Install
sudo ./scripts/install.sh

# Configure
nano .env

# Start
sudo systemctl start anheart

# Check status
sudo systemctl status anheart

# View logs
journalctl -u anheart -f

# Stop
sudo systemctl stop anheart
```

### Full Integration Test

1. Configure .env with real credentials
2. Start service
3. Create session in web UI
4. Verify data appears in dashboard
5. End session
6. Verify summary generated
