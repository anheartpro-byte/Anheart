#!/usr/bin/env python3
"""Test BITalino connection and data acquisition."""

import asyncio
import sys
import os
import argparse
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bitalino_client import BITalinoClient, discover_bitalino_devices


async def scan_devices():
    """Scan for BITalino devices."""
    print("=" * 60)
    print("Scanning for BITalino devices...")
    print("Make sure Bluetooth is enabled and the device is powered on.")
    print("=" * 60)
    print()
    
    devices = await discover_bitalino_devices(timeout=10.0)
    
    if not devices:
        print("\nNo BITalino devices found!")
        print("\nTroubleshooting tips:")
        print("  1. Make sure Bluetooth is enabled: bluetoothctl power on")
        print("  2. Ensure the BITalino/psychoBIT is powered on")
        print("  3. Try pairing the device first: bluetoothctl pair <MAC>")
        print("  4. Run with sudo if needed for Bluetooth access")
        return None
    
    print(f"\nFound {len(devices)} device(s):")
    for i, d in enumerate(devices):
        print(f"  [{i + 1}] {d['name']} - {d['address']}")
    
    return devices


async def test_connection(mac_address: str):
    """Test connection to a BITalino device."""
    print()
    print("=" * 60)
    print(f"Testing connection to: {mac_address}")
    print("=" * 60)
    print()
    
    client = BITalinoClient(
        mac_address=mac_address,
        channels=[0],  # ECG channel
        sample_rate=1000,
    )
    
    # Test connection
    print("Connecting...")
    connected = await client.connect(timeout=30.0)
    
    if not connected:
        print("Failed to connect!")
        return False
    
    print("Connected successfully!")
    
    # Get device state
    print("\nGetting device state...")
    state = await client.get_state()
    if state:
        print(f"  Device state: {state}")
    
    # Test data acquisition
    print("\nStarting data acquisition (5 seconds)...")
    if await client.start_acquisition():
        start_time = time.time()
        total_samples = 0
        
        while time.time() - start_time < 5.0:
            await asyncio.sleep(0.5)
            batch = await client.read_samples(count=100)
            
            if batch:
                for ch in batch.channels:
                    num_samples = len(ch.values)
                    total_samples += num_samples
                    
                    # Calculate basic stats
                    if ch.values:
                        min_val = min(ch.values)
                        max_val = max(ch.values)
                        avg_val = sum(ch.values) / len(ch.values)
                        print(f"  {ch.channel}: {num_samples} samples, "
                              f"min={min_val:.0f}, max={max_val:.0f}, avg={avg_val:.1f}")
        
        print(f"\nTotal samples collected: {total_samples}")
        print(f"Expected samples (5s @ 1000Hz): 5000")
        print(f"Effective sample rate: {total_samples / 5:.0f} Hz")
        
        await client.stop_acquisition()
        print("\nStopped acquisition")
    else:
        print("Failed to start acquisition!")
    
    # Disconnect
    await client.disconnect()
    print("Disconnected")
    
    return True


async def main():
    parser = argparse.ArgumentParser(description="Test BITalino device")
    parser.add_argument(
        "--mac",
        type=str,
        help="MAC address of the BITalino device (e.g., 20:16:07:18:17:02)",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan for devices, don't test connection",
    )
    args = parser.parse_args()
    
    if args.scan_only or not args.mac:
        devices = await scan_devices()
        
        if args.scan_only or not devices:
            return
        
        # Ask user to select device
        print()
        selection = input("Enter device number to test (or press Enter to skip): ").strip()
        
        if not selection:
            return
        
        try:
            idx = int(selection) - 1
            if 0 <= idx < len(devices):
                mac = devices[idx]["address"]
            else:
                print("Invalid selection")
                return
        except ValueError:
            print("Invalid input")
            return
    else:
        mac = args.mac
    
    await test_connection(mac)


if __name__ == "__main__":
    asyncio.run(main())
