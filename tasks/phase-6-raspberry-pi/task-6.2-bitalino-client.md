# Task 6.2: BITalino BLE Client

## Objective

Implement BLE connection to BITalino Core BT for reading biosensor data.

## Dependencies

- Task 6.1 (Project Setup) completed
- BITalino Core BT device available for testing

---

## Acceptance Criteria

### BITalinoClient Class

- [ ] Async connect/disconnect methods
- [ ] Start/stop acquisition methods
- [ ] Read samples method returns formatted data
- [ ] Handles connection errors gracefully
- [ ] Reconnection logic on disconnect
- [ ] Configurable channels and sample rate

### Data Format

- [ ] Returns list of channel data
- [ ] Each channel has name and values array
- [ ] Values are normalized (0-1023 for 10-bit ADC)
- [ ] Timestamps are accurate

### Error Handling

- [ ] Connection timeout handling
- [ ] Disconnection detection
- [ ] Invalid channel handling
- [ ] Device not found handling

---

## Implementation

```python
# raspberry-pi/src/bitalino_client.py
"""BITalino BLE client for reading biosensor data."""

import asyncio
import logging
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

logger = logging.getLogger(__name__)


@dataclass
class ChannelData:
    """Data from a single channel."""
    channel: str
    values: list[float]


@dataclass
class SampleBatch:
    """Batch of samples from all channels."""
    timestamp: int  # Unix timestamp in ms
    channels: list[ChannelData]


# BITalino BLE UUIDs (these may vary by device version)
BITALINO_SERVICE_UUID = "00001101-0000-1000-8000-00805f9b34fb"
BITALINO_CHAR_UUID = "00001102-0000-1000-8000-00805f9b34fb"

# Channel mapping
CHANNEL_NAMES = {
    0: "ECG",
    1: "EMG",
    2: "EDA",
    3: "EEG",
    4: "ACC",
    5: "LUX",
}


class BITalinoClient:
    """BLE client for BITalino Core BT."""

    def __init__(
        self,
        mac_address: str,
        channels: list[int] = [0],  # Default to channel 0 (ECG)
        sample_rate: int = 1000,
    ):
        self.mac_address = mac_address
        self.channels = channels
        self.sample_rate = sample_rate
        self.client: Optional[BleakClient] = None
        self.is_connected = False
        self.is_acquiring = False
        self._buffer: list[list[int]] = []
        self._on_disconnect: Optional[Callable[[], Awaitable[None]]] = None

    async def connect(self, timeout: float = 30.0) -> bool:
        """Connect to BITalino device."""
        logger.info(f"Connecting to BITalino at {self.mac_address}...")

        try:
            self.client = BleakClient(
                self.mac_address,
                disconnected_callback=self._handle_disconnect,
            )
            await asyncio.wait_for(
                self.client.connect(),
                timeout=timeout,
            )
            self.is_connected = True
            logger.info("Connected to BITalino")
            return True

        except asyncio.TimeoutError:
            logger.error("Connection timeout")
            return False
        except BleakError as e:
            logger.error(f"BLE connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from BITalino."""
        if self.client and self.is_connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self.is_connected = False
                self.is_acquiring = False
                logger.info("Disconnected from BITalino")

    def _handle_disconnect(self, client: BleakClient) -> None:
        """Handle unexpected disconnection."""
        logger.warning("BITalino disconnected unexpectedly")
        self.is_connected = False
        self.is_acquiring = False
        if self._on_disconnect:
            asyncio.create_task(self._on_disconnect())

    def set_disconnect_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Set callback for disconnect events."""
        self._on_disconnect = callback

    async def start_acquisition(self) -> bool:
        """Start data acquisition."""
        if not self.is_connected or not self.client:
            logger.error("Not connected")
            return False

        try:
            # Build channel mask
            channel_mask = sum(1 << ch for ch in self.channels)

            # Send start command
            # Format: [sample_rate_code, channel_mask]
            rate_codes = {1: 0x01, 10: 0x02, 100: 0x03, 1000: 0x04}
            rate_code = rate_codes.get(self.sample_rate, 0x04)

            command = bytes([0x01, rate_code, channel_mask])

            # Note: Actual command format depends on BITalino firmware version
            # This is a simplified example - adjust based on actual protocol
            await self.client.write_gatt_char(BITALINO_CHAR_UUID, command)

            # Start notification handler
            await self.client.start_notify(
                BITALINO_CHAR_UUID,
                self._handle_notification,
            )

            self.is_acquiring = True
            self._buffer = []
            logger.info(f"Started acquisition: channels={self.channels}, rate={self.sample_rate}Hz")
            return True

        except Exception as e:
            logger.error(f"Failed to start acquisition: {e}")
            return False

    async def stop_acquisition(self) -> None:
        """Stop data acquisition."""
        if not self.is_connected or not self.client:
            return

        try:
            # Send stop command
            await self.client.write_gatt_char(BITALINO_CHAR_UUID, bytes([0x00]))
            await self.client.stop_notify(BITALINO_CHAR_UUID)
            self.is_acquiring = False
            logger.info("Stopped acquisition")
        except Exception as e:
            logger.warning(f"Error stopping acquisition: {e}")

    def _handle_notification(
        self, sender: int, data: bytearray
    ) -> None:
        """Handle incoming BLE notifications."""
        # Parse BITalino data packet
        # Format depends on firmware version - this is simplified
        try:
            # Each sample is 2 bytes (10-bit ADC value)
            samples_per_channel = len(data) // (2 * len(self.channels))

            for ch_idx, channel in enumerate(self.channels):
                if len(self._buffer) <= ch_idx:
                    self._buffer.append([])

                for s in range(samples_per_channel):
                    offset = (s * len(self.channels) + ch_idx) * 2
                    if offset + 1 < len(data):
                        value = int.from_bytes(
                            data[offset:offset+2],
                            byteorder='little'
                        ) & 0x3FF  # 10-bit mask
                        self._buffer[ch_idx].append(value)

        except Exception as e:
            logger.warning(f"Error parsing notification: {e}")

    async def read_samples(self, count: int = 1000) -> Optional[SampleBatch]:
        """
        Read accumulated samples.

        Args:
            count: Number of samples to read per channel

        Returns:
            SampleBatch with data from all channels, or None if not enough data
        """
        if not self.is_acquiring:
            return None

        # Check if we have enough samples
        min_samples = min(len(ch) for ch in self._buffer) if self._buffer else 0
        if min_samples < count:
            return None

        import time
        timestamp = int(time.time() * 1000)

        # Extract samples
        channels = []
        for ch_idx, channel in enumerate(self.channels):
            values = self._buffer[ch_idx][:count]
            self._buffer[ch_idx] = self._buffer[ch_idx][count:]

            channel_name = CHANNEL_NAMES.get(channel, f"CH{channel}")
            channels.append(ChannelData(channel=channel_name, values=values))

        return SampleBatch(timestamp=timestamp, channels=channels)

    async def get_battery_level(self) -> Optional[int]:
        """Get battery level if available."""
        # This would read from battery characteristic if available
        # Return None for now - implement based on device capabilities
        return None


async def discover_bitalino_devices(timeout: float = 10.0) -> list[dict]:
    """Discover nearby BITalino devices."""
    logger.info("Scanning for BITalino devices...")

    devices = []
    discovered = await BleakScanner.discover(timeout=timeout)

    for device in discovered:
        # Filter for BITalino devices by name
        if device.name and "bitalino" in device.name.lower():
            devices.append({
                "name": device.name,
                "address": device.address,
            })
            logger.info(f"Found: {device.name} ({device.address})")

    return devices
```

```python
# raspberry-pi/tests/test_bitalino.py
"""Test BITalino client."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.bitalino_client import BITalinoClient, ChannelData, SampleBatch


@pytest.fixture
def client():
    return BITalinoClient(
        mac_address="AA:BB:CC:DD:EE:FF",
        channels=[0],
        sample_rate=1000,
    )


@pytest.mark.asyncio
async def test_client_initialization(client):
    """Test client initializes correctly."""
    assert client.mac_address == "AA:BB:CC:DD:EE:FF"
    assert client.channels == [0]
    assert client.sample_rate == 1000
    assert not client.is_connected
    assert not client.is_acquiring


@pytest.mark.asyncio
@patch("src.bitalino_client.BleakClient")
async def test_connect_success(mock_bleak, client):
    """Test successful connection."""
    mock_instance = AsyncMock()
    mock_bleak.return_value = mock_instance

    result = await client.connect(timeout=5.0)

    assert result is True
    assert client.is_connected is True


@pytest.mark.asyncio
@patch("src.bitalino_client.BleakClient")
async def test_connect_timeout(mock_bleak, client):
    """Test connection timeout."""
    mock_instance = AsyncMock()
    mock_instance.connect.side_effect = asyncio.TimeoutError()
    mock_bleak.return_value = mock_instance

    result = await client.connect(timeout=1.0)

    assert result is False
    assert client.is_connected is False


def test_sample_batch_format():
    """Test sample batch data structure."""
    batch = SampleBatch(
        timestamp=1234567890,
        channels=[
            ChannelData(channel="ECG", values=[100, 200, 300]),
        ],
    )

    assert batch.timestamp == 1234567890
    assert len(batch.channels) == 1
    assert batch.channels[0].channel == "ECG"
    assert batch.channels[0].values == [100, 200, 300]
```

---

## Testing Steps

### Unit Tests

1. Run `pytest tests/test_bitalino.py`
2. Verify all mocked tests pass

### Integration Tests (with real device)

1. Configure `.env` with real BITalino MAC
2. Run discovery script to find device
3. Test connection/disconnection
4. Start acquisition and read samples
5. Verify data format and values

### Discovery Test

```python
# raspberry-pi/scripts/discover_devices.py
import asyncio
from src.bitalino_client import discover_bitalino_devices

async def main():
    devices = await discover_bitalino_devices()
    print(f"Found {len(devices)} devices")
    for d in devices:
        print(f"  {d['name']}: {d['address']}")

asyncio.run(main())
```

---

## Notes

- BITalino Core BT uses BLE, different from classic Bluetooth BITalino
- Exact protocol depends on firmware version
- May need to adjust UUIDs and command format for your device
- bleak library handles cross-platform BLE
