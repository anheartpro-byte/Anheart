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

try:
    from bitalino import BITalino, ExceptionCode
except ImportError:
    # The `bitalino` package depends on native Bluetooth libraries that are only
    # present on the Raspberry Pi. Import it lazily so this module's pure logic
    # (frame parsing, channel maps) can be imported and unit-tested off-device.
    BITalino = None  # type: ignore[assignment,misc]
    ExceptionCode = None  # type: ignore[assignment,misc]

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


# Authoritative mapping between sensor names and BITalino analog channel indices
# (A1-A6 -> 0-5). This is the SINGLE source of truth for name<->channel: both this
# client and the session manager import it, so a requested sensor is always read
# from the correct column AND labeled consistently in the stored data.
CHANNEL_MAP: dict[str, int] = {
    "ECG": 0,   # A1 - Electrocardiography (heart)
    "EDA": 1,   # A2 - Electrodermal Activity (skin conductance/stress)
    "SpO2": 2,  # A3 - Pulse Oximetry (blood oxygen via finger clip)
    "RESP": 3,  # A4 - Respiration (chest band)
    "EMG": 4,   # A5 - Electromyography (muscle)
    "LUX": 5,   # A6 - Light sensor or other
}

# Reverse lookup, derived from CHANNEL_MAP so the two can never drift apart.
CHANNEL_NAMES: dict[int, str] = {index: name for name, index in CHANNEL_MAP.items()}

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

        # Leftover samples carried between read_samples() calls. Owned solely by
        # the reader side and always prepended (oldest-first) to newly drained
        # chunks, so batches stay strictly time-contiguous. Never re-inserted into
        # the shared queue (which the acquisition thread appends newer data to).
        self._leftover: Optional[np.ndarray] = None
        self._leftover_ts: int = 0
        # One-shot signal-integrity check on the first batch of an acquisition.
        self._first_batch_checked = False
        
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
            self._leftover = None
            self._leftover_ts = 0
            self._first_batch_checked = False
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

        # Drain the queue in FIFO order. The acquisition thread only ever *appends*
        # newly read chunks, so draining preserves capture order.
        rows: list[np.ndarray] = []
        latest_timestamp = self._leftover_ts or int(time.time() * 1000)

        # Prepend any leftover from the previous call FIRST so the assembled block
        # stays strictly oldest-to-newest (this is what fixes the reordering race:
        # older samples are never placed behind newer ones).
        if self._leftover is not None:
            rows.append(self._leftover)
            self._leftover = None

        while not self._buffer.empty():
            try:
                timestamp, data = self._buffer.get_nowait()
                rows.append(data)
                latest_timestamp = timestamp
            except Empty:
                break

        if not rows:
            return None

        combined = np.vstack(rows)

        # Not enough for a full batch yet: keep it (still oldest-first) for later.
        if combined.shape[0] < count:
            self._leftover = combined
            self._leftover_ts = latest_timestamp
            return None

        # Extract channel data.
        # BITalino read() matrix layout: [seq, I1, I2, O1, O2, A1, A2, ...].
        # Analog channels start at column 5, in the SAME order they were requested,
        # so the Nth requested channel is at column 5 + N.
        channels = []
        for ch_idx, channel in enumerate(self.channels):
            analog_col = 5 + ch_idx
            if analog_col < combined.shape[1]:
                values = combined[:count, analog_col].tolist()
            else:
                values = [0] * count

            channel_name = CHANNEL_NAMES.get(channel, f"CH{channel}")
            channels.append(ChannelData(channel=channel_name, values=values))

        # Carry the surplus (newest samples) forward, still oldest-first.
        if combined.shape[0] > count:
            self._leftover = combined[count:]
            self._leftover_ts = latest_timestamp

        self._check_signal_integrity(channels)

        return SampleBatch(timestamp=latest_timestamp, channels=channels)

    def _check_signal_integrity(self, channels: list[ChannelData]) -> None:
        """Warn once if a channel carries no real analog signal.

        A live 10-bit analog channel swings across 0-1023; a channel stuck at a
        near-constant tiny value (e.g. the {0,1,3} we saw when the sensor was not
        plugged into its port) means no signal is reaching the ADC. Surfacing this
        immediately prevents silently recording an unusable session.
        """
        if self._first_batch_checked:
            return
        self._first_batch_checked = True

        for ch in channels:
            if not ch.values:
                continue
            arr = np.asarray(ch.values, dtype=float)
            if arr.max() <= 4 and arr.std() < 1.0:
                logger.warning(
                    "Channel %s has no analog signal (max=%d, std=%.2f) - check "
                    "the sensor is plugged into its BITalino port and powered.",
                    ch.channel, int(arr.max()), float(arr.std()),
                )

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
