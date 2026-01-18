"""BITalino Bluetooth Classic client for reading biosensor data."""

import asyncio
import logging
import time
import threading
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


CHANNEL_NAMES = {
    0: "ECG",
    1: "EMG",
    2: "EDA",
    3: "EEG",
    4: "ACC",
    5: "LUX",
}


class BITalinoClient:
    """Bluetooth Classic client for BITalino / psychoBIT devices."""

    def __init__(
        self,
        mac_address: str,
        channels: list[int] | None = None,
        sample_rate: int = 1000,
    ):
        """
        Initialize BITalino client.
        
        Args:
            mac_address: Bluetooth MAC address of the device
            channels: List of analog channels to read (0-5)
            sample_rate: Sampling rate in Hz (1, 10, 100, or 1000)
        """
        self.mac_address = mac_address
        self.channels = channels if channels is not None else [0]
        self.sample_rate = sample_rate
        
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
