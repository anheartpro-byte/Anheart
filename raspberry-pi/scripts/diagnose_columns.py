#!/usr/bin/env python3
"""Diagnose the raw BITalino data frame.

This connects to a BITalino, starts acquisition, and prints the *raw* matrix
returned by ``device.read()`` so we can see exactly what each column contains:
sequence number, digital lines, and analog channels. Use it to confirm which
column carries the real 10-bit ECG (range ~0-1023, baseline ~512) and whether
the signal is dominated by 50 Hz mains hum (poor electrode contact).

Run on the Raspberry Pi with electrodes attached to a person:

    python3 scripts/diagnose_columns.py --mac 20:16:07:18:17:02
    # or a serial port:
    python3 scripts/diagnose_columns.py --mac /dev/rfcomm0

Then paste the output. The ECG column is the one that varies widely inside
0-1023; digital lines stay in {0,1}; the sequence column ramps 0-15.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from bitalino import BITalino

# Standard BITalino frame layout: [seq, I1, I2, O1, O2, A1, A2, ...]
BASE_COLUMN_NAMES = ["seq", "I1", "I2", "O1", "O2"]


def dominant_freq(signal: np.ndarray, fs: float) -> tuple[float, float]:
    """Return (dominant_frequency_hz, normalized_power) via FFT, ignoring DC."""
    x = signal - np.mean(signal)
    if np.allclose(x, 0):
        return 0.0, 0.0
    spectrum = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), d=1.0 / fs)
    # Ignore the DC bin
    spectrum[0] = 0.0
    peak_idx = int(np.argmax(spectrum))
    total = float(np.sum(spectrum)) or 1.0
    return float(freqs[peak_idx]), float(spectrum[peak_idx] / total)


def diagnose(mac: str, channels: list[int], fs: int, seconds: float) -> None:
    print("=" * 70)
    print(f"Connecting to {mac} ...")
    device = BITalino(mac)
    try:
        print(f"Device version: {device.version()}")
    except Exception as e:
        print(f"(could not read version: {e})")

    print(f"Starting acquisition: channels={channels}, rate={fs} Hz")
    device.start(fs, channels)

    # Warm up (discard the first read; startup frames can be partial)
    try:
        device.read(fs // 10)
    except Exception:
        pass

    collected: list[np.ndarray] = []
    start = time.time()
    print(f"Reading ~{seconds:.0f}s of data ...")
    while time.time() - start < seconds:
        collected.append(device.read(fs // 10))

    device.stop()
    device.close()

    data = np.vstack(collected)
    n_rows, n_cols = data.shape
    print()
    print(f"Raw read() matrix: {n_rows} rows x {n_cols} cols")
    print("Expected layout: [seq, I1, I2, O1, O2, A1..] -> analog channels start at column 5")
    print()

    header = f"{'col':>3}  {'name':>6}  {'min':>6}  {'max':>6}  {'mean':>8}  {'std':>7}  {'domFreq(Hz)':>11}  first-8"
    print(header)
    print("-" * len(header))

    for col in range(n_cols):
        column = data[:, col]
        if col < len(BASE_COLUMN_NAMES):
            name = BASE_COLUMN_NAMES[col]
        else:
            # analog channel: map back to requested channel index
            analog_idx = col - 5
            ch = channels[analog_idx] if analog_idx < len(channels) else "?"
            name = f"A{ch}"
        freq, power = dominant_freq(column.astype(float), fs)
        first8 = ", ".join(str(int(v)) for v in column[:8])
        print(
            f"{col:>3}  {name:>6}  {int(column.min()):>6}  {int(column.max()):>6}  "
            f"{column.mean():>8.1f}  {column.std():>7.1f}  "
            f"{freq:>7.1f} ({power:.0%})  [{first8}]"
        )

    print()
    print("Interpretation:")
    print("  * ECG column  -> wide range inside 0-1023, baseline ~512, std > ~30")
    print("  * mains hum   -> dominant freq == 50 Hz (EU) / 60 Hz (US) with high")
    print("                   power share => electrodes not contacting well")
    print("  * flat column -> min==max==~0 => sensor not plugged into that port")
    print("  * digital I/O -> only values {0,1}; seq ramps 0..15")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose raw BITalino columns")
    parser.add_argument(
        "--mac", required=True, help="BITalino MAC address or serial port (/dev/rfcomm0)"
    )
    parser.add_argument(
        "--channels", default="0", help="Comma-separated analog channels to read (default: 0)"
    )
    parser.add_argument("--rate", type=int, default=1000, help="Sample rate in Hz (default: 1000)")
    parser.add_argument(
        "--seconds", type=float, default=5.0, help="How many seconds to capture (default: 5)"
    )
    args = parser.parse_args()

    channels = [int(c) for c in args.channels.split(",") if c.strip() != ""]

    try:
        diagnose(args.mac, channels, args.rate, args.seconds)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
