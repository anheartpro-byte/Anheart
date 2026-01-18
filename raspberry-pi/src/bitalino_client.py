"""BITalino Bluetooth Classic client for reading biosensor data."""

import asyncio
import logging
import time
import threading
import subprocess
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from queue import Queue, Empty

import numpy as np
from bitalino import BITalino, ExceptionCode

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


# BITalino analog channel names
# These correspond to A1-A6 on the BITalino board
# You can plug any sensor into any channel, but this is the recommended mapping:
CHANNEL_NAMES = {
    0: "ECG",      # A1 - Electrocardiography (heart)
    1: "EDA",      # A2 - Electrodermal Activity (skin conductance/stress)
    2: "SpO2",     # A3 - Pulse Oximetry (blood oxygen via finger clip)
    3: "RESP",     # A4 - Respiration (chest band)
    4: "EMG",      # A5 - Electromyography (muscle) or LUX (light)
    5: "LUX",      # A6 - Light sensor or other
}

# Default BITalino PIN code
BITALINO_PIN = "1234"


def pair_bluetooth_device(mac_address: str, pin: str = BITALINO_PIN) -> bool:
    """
    Pair with a Bluetooth device using bluetoothctl.
    
    Args:
        mac_address: Bluetooth MAC address
        pin: PIN code (default 1234 for BITalino)
    
    Returns:
        True if pairing successful or already paired
    """
    try:
        # Check if already paired
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        if "Paired: yes" in result.stdout:
            logger.info(f"Device {mac_address} already paired")
            return True
        
        logger.info(f"Pairing with {mac_address} using PIN {pin}...")
        
        # Create expect-like script for pairing
        pair_script = f"""
import pexpect
import sys

child = pexpect.spawn('bluetoothctl', encoding='utf-8', timeout=30)
child.expect('#')
child.sendline('agent on')
child.expect('#')
child.sendline('default-agent')
child.expect('#')
child.sendline('pair {mac_address}')

try:
    i = child.expect(['PIN code:', 'Passkey:', 'Enter PIN', 'Pairing successful', 'AlreadyExists', 'Failed'], timeout=30)
    if i in [0, 1, 2]:
        child.sendline('{pin}')
        child.expect(['Pairing successful', 'AlreadyExists', 'Failed'], timeout=30)
except:
    pass

child.sendline('trust {mac_address}')
child.expect('#')
child.sendline('quit')
child.close()
print('OK')
"""
        
        # Try using pexpect if available
        try:
            result = subprocess.run(
                ["python3", "-c", pair_script],
                capture_output=True,
                text=True,
                timeout=60
            )
            if "OK" in result.stdout:
                logger.info(f"Pairing successful")
                return True
        except Exception as e:
            logger.warning(f"pexpect pairing failed: {e}")
        
        # Fallback: simple bluetoothctl commands (may require manual PIN entry)
        commands = [
            ["bluetoothctl", "agent", "on"],
            ["bluetoothctl", "default-agent"],
            ["bluetoothctl", "pair", mac_address],
            ["bluetoothctl", "trust", mac_address],
        ]
        
        for cmd in commands:
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
            except Exception:
                pass
        
        # Check if pairing succeeded
        result = subprocess.run(
            ["bluetoothctl", "info", mac_address],
            capture_output=True,
            text=True,
            timeout=10
        )
        if "Paired: yes" in result.stdout:
            logger.info(f"Pairing successful")
            return True
        
        logger.warning(f"Automatic pairing may have failed. Device might need manual pairing.")
        return False
        
    except Exception as e:
        logger.error(f"Pairing error: {e}")
        return False


class BITalinoClient:
    """Bluetooth Classic client for BITalino / psychoBIT devices."""

    def __init__(
        self,
        mac_address: str,
        channels: list[int] | None = None,
        sample_rate: int = 1000,
        auto_pair: bool = True,
    ):
        """
        Initialize BITalino client.
        
        Args:
            mac_address: Bluetooth MAC address OR serial port (e.g., /dev/rfcomm0)
            channels: List of analog channels to read (0-5)
            sample_rate: Sampling rate in Hz (1, 10, 100, or 1000)
            auto_pair: Automatically attempt Bluetooth pairing with PIN 1234
        """
        self.mac_address = mac_address
        self.channels = channels if channels is not None else [0]
        self.sample_rate = sample_rate
        self.auto_pair = auto_pair
        
        # Check if using serial port instead of MAC address
        self.use_serial = mac_address.startswith("/dev/")
        
        self._device: Optional[BITalino] = None
        self.is_connected = False
        self.is_acquiring = False
        
        self._buffer: Queue = Queue()
        self._acquisition_thread: Optional[threading.Thread] = None
        self._stop_acquisition = threading.Event()
        self._on_disconnect: Optional[Callable[[], Awaitable[None]]] = None
        
        # Validate inputs
        if sample_rate not in [1, 10, 100, 1000]:
            raise ValueError(f"Sample rate must be 1, 10, 100, or 1000 Hz, got {sample_rate}")
        for ch in self.channels:
            if ch < 0 or ch > 5:
                raise ValueError(f"Channel must be 0-5, got {ch}")

    async def connect(self, timeout: float = 30.0) -> bool:
        """Connect to BITalino device."""
        logger.info(f"Connecting to BITalino at {self.mac_address}...")
        
        # Auto-pair if using Bluetooth MAC address
        if self.auto_pair and not self.use_serial:
            pair_bluetooth_device(self.mac_address, BITALINO_PIN)
        
        try:
            # Run blocking connection in thread pool
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._connect_sync),
                timeout=timeout
            )
            self.is_connected = True
            logger.info("Connected to BITalino")
            
            # Get device version for debugging
            if self._device is not None:
                try:
                    version = self._device.version()
                    logger.info(f"Device version: {version}")
                except Exception as e:
                    logger.warning(f"Could not get device version: {e}")
            
            return True
            
        except asyncio.TimeoutError:
            logger.error("Connection timeout")
            return False
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False

    def _connect_sync(self) -> None:
        """Synchronous connection (runs in thread)."""
        self._device = BITalino(self.mac_address)

    async def disconnect(self) -> None:
        """Disconnect from BITalino."""
        if self.is_acquiring:
            await self.stop_acquisition()
        
        if self._device and self.is_connected:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._device.close)
            except Exception as e:
                logger.warning(f"Error during disconnect: {e}")
            finally:
                self._device = None
                self.is_connected = False
                logger.info("Disconnected from BITalino")

    def set_disconnect_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Set callback for disconnect events."""
        self._on_disconnect = callback

    async def start_acquisition(self) -> bool:
        """Start data acquisition."""
        if not self.is_connected or not self._device:
            logger.error("Not connected")
            return False
        
        if self.is_acquiring:
            logger.warning("Already acquiring")
            return True
        
        try:
            # Clear buffer and reset stop event
            while not self._buffer.empty():
                try:
                    self._buffer.get_nowait()
                except Empty:
                    break
            self._stop_acquisition.clear()
            
            # Start acquisition in background thread
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, 
                self._device.start, 
                self.sample_rate, 
                self.channels
            )
            
            # Start reading thread
            self._acquisition_thread = threading.Thread(
                target=self._acquisition_loop,
                daemon=True
            )
            self._acquisition_thread.start()
            
            self.is_acquiring = True
            logger.info(f"Started acquisition: channels={self.channels}, rate={self.sample_rate}Hz")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start acquisition: {e}")
            return False

    def _acquisition_loop(self) -> None:
        """Background thread for reading data from device."""
        nsamples = max(10, self.sample_rate // 10)  # Read chunks (e.g., 100 samples at 1000Hz)
        
        while not self._stop_acquisition.is_set():
            try:
                if self._device:
                    # Read samples (blocking call)
                    data = self._device.read(nsamples)
                    timestamp = int(time.time() * 1000)
                    self._buffer.put((timestamp, data))
            except Exception as e:
                logger.error(f"Error reading data: {e}")
                self.is_acquiring = False
                self.is_connected = False
                
                # Trigger disconnect callback
                if self._on_disconnect:
                    try:
                        loop = asyncio.new_event_loop()
                        loop.run_until_complete(self._on_disconnect())
                        loop.close()
                    except Exception:
                        pass
                break

    async def stop_acquisition(self) -> None:
        """Stop data acquisition."""
        if not self.is_acquiring:
            return
        
        try:
            # Signal thread to stop
            self._stop_acquisition.set()
            
            # Wait for thread to finish
            if self._acquisition_thread and self._acquisition_thread.is_alive():
                self._acquisition_thread.join(timeout=2.0)
            
            # Stop device acquisition
            if self._device:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._device.stop)
            
            self.is_acquiring = False
            logger.info("Stopped acquisition")
            
        except Exception as e:
            logger.warning(f"Error stopping acquisition: {e}")
            self.is_acquiring = False

    async def read_samples(self, count: int = 1000) -> Optional[SampleBatch]:
        """
        Read accumulated samples.
        
        Args:
            count: Minimum number of samples to read per channel
        
        Returns:
            SampleBatch with data from all channels, or None if not enough data
        """
        if not self.is_acquiring:
            return None
        
        # Collect data from queue
        all_data = []
        latest_timestamp = int(time.time() * 1000)
        
        while not self._buffer.empty():
            try:
                timestamp, data = self._buffer.get_nowait()
                all_data.append(data)
                latest_timestamp = timestamp
            except Empty:
                break
        
        if not all_data:
            return None
        
        # Combine all chunks
        combined = np.vstack(all_data)
        
        # Check if we have enough samples
        if combined.shape[0] < count:
            # Put data back for next time
            self._buffer.put((latest_timestamp, combined))
            return None
        
        # Extract channel data
        # BITalino data format: [seq, d1, d2, d3, d4, a1, a2, a3, a4, a5, a6]
        # Analog channels start at index 5 (after seq and 4 digital channels)
        # Channel 0 (A1) -> column 5
        # Channel 1 (A2) -> column 6
        # etc.
        channels = []
        for ch_idx, channel in enumerate(self.channels):
            # Analog data column = 5 + channel_index_in_list
            # When we request channels [0], we get data at column 5
            # When we request channels [0, 1], we get data at columns 5, 6
            analog_col = 5 + ch_idx
            if analog_col < combined.shape[1]:
                values = combined[:count, analog_col].tolist()
            else:
                values = [0] * count
            
            channel_name = CHANNEL_NAMES.get(channel, f"CH{channel}")
            channels.append(ChannelData(channel=channel_name, values=values))
        
        # Keep remaining data in buffer
        if combined.shape[0] > count:
            remaining = combined[count:]
            self._buffer.put((latest_timestamp, remaining))
        
        return SampleBatch(timestamp=latest_timestamp, channels=channels)

    async def get_battery_level(self) -> Optional[int]:
        """Get battery level (0-100)."""
        if not self._device or not self.is_connected:
            return None
        
        try:
            loop = asyncio.get_event_loop()
            # BITalino battery threshold is set via pwm (0-63)
            # This is more of a low-battery warning threshold, not actual level
            # Return None as actual battery level isn't directly readable
            return None
        except Exception:
            return None

    async def get_state(self) -> Optional[dict]:
        """Get device state including digital inputs."""
        if not self._device or not self.is_connected:
            return None
        
        try:
            loop = asyncio.get_event_loop()
            state = await loop.run_in_executor(None, self._device.state)
            return state
        except Exception as e:
            logger.warning(f"Could not get state: {e}")
            return None


async def discover_bitalino_devices(timeout: float = 10.0) -> list[dict]:
    """
    Discover nearby BITalino devices via Bluetooth scan.
    
    Note: Requires bluetooth to be enabled and may need root/sudo on Linux.
    """
    import bluetooth
    
    logger.info("Scanning for Bluetooth devices...")
    
    devices = []
    
    try:
        loop = asyncio.get_event_loop()
        nearby = await loop.run_in_executor(
            None, 
            lambda: bluetooth.discover_devices(
                duration=int(timeout), 
                lookup_names=True,
                lookup_class=False
            )
        )
        
        for addr, name in nearby:
            # Filter for BITalino/psychoBIT devices
            name_lower = name.lower() if name else ""
            if any(x in name_lower for x in ["bitalino", "psychobit", "plux"]):
                devices.append({
                    "name": name,
                    "address": addr,
                })
                logger.info(f"Found: {name} ({addr})")
        
        if not devices:
            # Show all devices for debugging
            logger.info("No BITalino devices found. All discovered devices:")
            for addr, name in nearby:
                logger.info(f"  {name or 'Unknown'} ({addr})")
    
    except ImportError:
        logger.error("PyBluez not installed. Install with: pip install PyBluez")
    except Exception as e:
        logger.error(f"Bluetooth scan error: {e}")
    
    return devices
