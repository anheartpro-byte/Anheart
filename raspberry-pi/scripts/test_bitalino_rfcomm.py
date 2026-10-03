#!/usr/bin/env python3
"""READ-ONLY check of the BITalino over macOS IOBluetooth RFCOMM. Touches no motor.

    .venv/bin/python scripts/test_bitalino_rfcomm.py
    .venv/bin/python scripts/test_bitalino_rfcomm.py --address rfcomm:98-d3-91-fe-4e-9f --seconds 20

What it does, through the application's own code path (``BITalinoClient`` with
``rfcomm_factory``, i.e. ``src/bitalino_rfcomm_macos.py``, the real
``FrameDecoder`` and the real DSP through ``src/ecg_pipeline.py``):

1. opens RFCOMM channel 1 to the device and reads its firmware version;
2. acquires A1 (ECG) at 1000 Hz for ``--seconds`` (default 20);
3. prints, once per second, the decoded frame rate, the corruption counters
   (CRC failures while aligned, bytes skipped to resync), the gaps the device
   dropped (filled by holding the last value), reconnects, the signal quality
   and the heart rate - a BPM appears only when the quality is GOOD;
4. stops the stream, closes the channel and prints a verdict.

Why it is safe: the only bytes sent to the BITalino are the vendor's
version (0x07), set-rate, start and stop commands. Nothing here imports the
drive code. The BITalino should be worn by a seated person, OFF the machine.

The address defaults to ``BITALINO_ADDRESS`` from the environment (or .env),
then to the bench unit ``rfcomm:98-d3-91-fe-4e-9f``. Pair the device once in
the macOS Bluetooth settings (PIN 1234) before the first run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.bitalino_client import BITalinoClient, LinkStats
from src.bitalino_rfcomm_macos import is_rfcomm_address, rfcomm_factory
from src.ecg_pipeline import load_treatment, treat_ecg

SAMPLE_RATE = 1000
DSP_RATE = 250
ECG_CHANNEL = 0  # A1
DEFAULT_ADDRESS = "rfcomm:98-d3-91-fe-4e-9f"
POLL_S = 0.05
READ_COUNT = SAMPLE_RATE // 10


def default_address() -> str:
    address = os.environ.get("BITALINO_ADDRESS", "")
    if not address:
        env_file = Path(__file__).resolve().parent.parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                if key.strip() == "BITALINO_ADDRESS":
                    address = value.strip().strip("'\"")
    return address if is_rfcomm_address(address) else DEFAULT_ADDRESS


def line(elapsed: float, stats: LinkStats, samples: int, quality: str, bpm: int | None) -> str:
    rate = stats.frames / elapsed if elapsed > 0 else 0.0
    shown = f"{bpm:3d}" if bpm is not None else "  -"
    return (
        f"{elapsed:5.1f} s | {rate:7.1f} trames/s | echantillons {samples:6d} | "
        f"CRC {stats.sync_losses:3d} | octets sautes {stats.skipped_bytes:4d} | "
        f"trous {stats.filled_samples:4d} | reconnexions {stats.reconnects} | "
        f"qualite {quality:<14} | BPM {shown}"
    )


async def run(address: str, seconds: float) -> int:
    print("=" * 78)
    print(f"BITalino RFCOMM (lecture seule) : {address}, A1 a {SAMPLE_RATE} Hz, {seconds:.0f} s")
    print("=" * 78)
    client = BITalinoClient(
        address,
        channels=[ECG_CHANNEL],
        sample_rate=SAMPLE_RATE,
        auto_pair=False,
        device_factory=rfcomm_factory,
    )
    print("Connexion (canal RFCOMM 1)...")
    if not await client.connect(timeout=30.0):
        print("ECHEC : connexion impossible. Appareil allume, appaire (PIN 1234), a portee ?")
        return 2
    print("Connecte (la version du firmware est dans le journal ci-dessus).")

    treatment = load_treatment(SAMPLE_RATE, DSP_RATE)
    samples = 0
    bpms: list[int] = []
    first_bpm_at: float | None = None
    last_seq = 0
    quality = "no_signal"
    bpm: int | None = None
    try:
        if not await client.start_acquisition():
            print("ECHEC : l'acquisition n'a pas demarre.")
            return 3
        started = time.monotonic()
        next_print = started + 1.0
        while (now := time.monotonic()) - started < seconds:
            batch = await client.read_samples(READ_COUNT)
            if batch is not None:
                samples += len(batch.channels[0].values)
                frame = treat_ecg(treatment, batch)
                if frame is not None and frame.metrics.seq != last_seq:
                    last_seq = frame.metrics.seq
                    quality = frame.metrics.quality.value
                    bpm = frame.metrics.bpm
                    if bpm is not None:
                        bpms.append(bpm)
                        if first_bpm_at is None:
                            first_bpm_at = now - started
            if now >= next_print:
                print(line(now - started, client.link_stats(), samples, quality, bpm))
                next_print += 1.0
            if not client.is_acquiring:
                print("Liaison perdue definitivement.")
                break
            await asyncio.sleep(POLL_S)
        elapsed = time.monotonic() - started
        stats = client.link_stats()
    finally:
        await client.disconnect()

    return print_summary(elapsed, stats, bpms, first_bpm_at, quality)


def print_summary(
    elapsed: float, stats: LinkStats, bpms: list[int], first_bpm_at: float | None, quality: str
) -> int:
    rate = stats.frames / elapsed if elapsed > 0 else 0.0
    print()
    print("-" * 78)
    print(f"Duree              : {elapsed:.1f} s")
    print(f"Trames decodees    : {stats.frames} ({rate:.1f} /s, attendu ~{SAMPLE_RATE})")
    print(
        f"Corruption         : {stats.sync_losses} echecs CRC, {stats.skipped_bytes} octets sautes"
    )
    print(f"Trous (trames perdues par l'appareil) : {stats.filled_samples}")
    print(f"Echantillons abandonnes (retard)       : {stats.dropped_backlog_samples}")
    print(f"Reconnexions       : {stats.reconnects}")
    if bpms:
        print(
            f"BPM                : {len(bpms)} mesures, mediane {statistics.median(bpms):.0f}, "
            f"min {min(bpms)}, max {max(bpms)}, premiere a {first_bpm_at or 0.0:.1f} s"
        )
    else:
        print(f"BPM                : aucun (derniere qualite : {quality}). Electrodes ?")
    healthy = (
        abs(rate - SAMPLE_RATE) <= 0.05 * SAMPLE_RATE
        and stats.reconnects == 0
        and stats.sync_losses == 0
    )
    print("VERDICT            :", "liaison OK" if healthy else "liaison A VERIFIER")
    print("-" * 78)
    return 0 if healthy else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--address", default=None, help="rfcomm:XX-XX-XX-XX-XX-XX")
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    address = args.address or default_address()
    if not is_rfcomm_address(address):
        print(f"Adresse invalide : {address!r} (attendu rfcomm:XX-XX-XX-XX-XX-XX)")
        return 64
    if sys.platform != "darwin":
        print("Ce transport n'existe que sur macOS (IOBluetooth). Sur le Pi : /dev/rfcomm0.")
        return 64
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(address, args.seconds))


if __name__ == "__main__":
    sys.exit(main())
