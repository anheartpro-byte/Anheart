#!/usr/bin/env python3
"""Discover BITalino/psychoBIT devices via Bluetooth."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bitalino_client import discover_bitalino_devices


async def main():
    print("=" * 60)
    print("BITalino / psychoBIT Device Scanner")
    print("=" * 60)
    print()
    print("Scanning for Bluetooth devices...")
    print("This may take up to 10 seconds.")
    print()
    
    devices = await discover_bitalino_devices(timeout=10.0)
    
    if not devices:
        print("No BITalino/psychoBIT devices found.")
        print()
        print("Troubleshooting tips:")
        print("  1. Enable Bluetooth: bluetoothctl power on")
        print("  2. Ensure device is powered on and not connected elsewhere")
        print("  3. Try pairing first: bluetoothctl pair <MAC_ADDRESS>")
        print("  4. Run with sudo if Bluetooth permissions are needed")
        print()
        print("To manually pair:")
        print("  bluetoothctl")
        print("  > scan on")
        print("  > pair XX:XX:XX:XX:XX:XX")
        print("  > trust XX:XX:XX:XX:XX:XX")
        print("  > quit")
        return
    
    print(f"Found {len(devices)} compatible device(s):")
    print()
    for d in devices:
        print(f"  Name: {d['name']}")
        print(f"  MAC:  {d['address']}")
        print()
    
    print("To test a device, run:")
    print(f"  python scripts/test_bitalino.py --mac <MAC_ADDRESS>")


if __name__ == "__main__":
    asyncio.run(main())
