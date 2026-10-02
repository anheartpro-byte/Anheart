#!/usr/bin/env python3
"""Discover BITalino/psychoBIT devices via Bluetooth."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def discover_bitalino_devices(scan_seconds: float = 10.0) -> list[dict]:
    """Scan for nearby BITalino/psychoBIT devices over Bluetooth Classic.

    Needs PyBluez (Linux) and may need sudo. Lives here rather than in
    src/bitalino_client.py because only these operator scripts use it.
    """
    try:
        import bluetooth
    except ImportError:
        print("PyBluez not installed; on the Pi use: bluetoothctl scan on")
        return []

    loop = asyncio.get_running_loop()
    try:
        nearby = await loop.run_in_executor(
            None,
            lambda: bluetooth.discover_devices(
                duration=int(scan_seconds), lookup_names=True, lookup_class=False
            ),
        )
    except Exception as e:
        print(f"Bluetooth scan error: {e}")
        return []

    devices = []
    for addr, name in nearby:
        if any(x in (name or "").lower() for x in ["bitalino", "psychobit", "plux"]):
            devices.append({"name": name, "address": addr})
    if not devices:
        print("No BITalino devices found. All discovered devices:")
        for addr, name in nearby:
            print(f"  {name or 'Unknown'} ({addr})")
    return devices


async def main():
    print("=" * 60)
    print("BITalino / psychoBIT Device Scanner")
    print("=" * 60)
    print()
    print("Scanning for Bluetooth devices...")
    print("This may take up to 10 seconds.")
    print()

    devices = await discover_bitalino_devices(scan_seconds=10.0)

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
    print("  python scripts/test_bitalino.py --mac <MAC_ADDRESS>")


if __name__ == "__main__":
    asyncio.run(main())
