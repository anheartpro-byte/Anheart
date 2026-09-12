"""Live BITalino read through the REAL on-device DSP pipeline.

Not a connectivity ping: it feeds actual frames into signal_processing.py so we
learn whether the signal is USABLE (electrodes, port A1, mains hum), which is
what actually blocked the previous captures.
"""

import os
import sys

# Run from anywhere: put the project root (parent of scripts/) on the path,
# same convention as scripts/diagnose_columns.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sys
import time

from src.signal_processing import BIOSPPY_AVAILABLE, SignalTreatment

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM4"
RATE = 1000
SECONDS = 8
CHANNELS = [0]  # A1 -> ECG

print(f"biosppy disponible : {BIOSPPY_AVAILABLE}")
print(f"ouverture de {PORT} ...", flush=True)

try:
    from bitalino import BITalino
except Exception as exc:
    print(f"ECHEC import bitalino : {exc}")
    raise SystemExit(2) from exc

try:
    dev = BITalino(PORT, timeout=5.0)
except Exception as exc:
    print(f"ECHEC ouverture : {type(exc).__name__}: {exc}")
    raise SystemExit(3) from exc

try:
    print(f"  connecte. version firmware : {dev.version()}")
    dev.start(RATE, CHANNELS)
    print(f"  acquisition demarree : {RATE} Hz, canaux {CHANNELS} (A1)")

    treat = SignalTreatment(fs_in=RATE, fs_out=250)
    t0 = time.time()
    got = 0

    for sec in range(SECONDS):
        frames = dev.read(RATE)  # 1 s
        got += frames.shape[0]
        raw = frames[:, 5].astype(float)  # colonne A1

        samples, metrics = treat.treat_batch([{"channel": "ECG", "values": raw.tolist()}])
        ecg = metrics.get("ECG", {})
        treated = samples[0]["values"]

        print(
            f"  [{sec + 1}/{SECONDS}] brut min={raw.min():4.0f} max={raw.max():4.0f} "
            f"std={raw.std():6.1f} | traite {len(treated)} ech @250Hz "
            f"({treated[0] if treated else 0:+.3f} mV) | "
            f"qualite={ecg.get('quality', '?'):<16} "
            f"bpm={ecg.get('heartRate', '--')} hrv={ecg.get('hrv', '--')}",
            flush=True,
        )

    dev.stop()
    elapsed = time.time() - t0
    print(f"\n  {got} echantillons en {elapsed:.1f}s -> {got / elapsed:.0f} Hz effectif")
finally:
    try:
        dev.close()
        print("  ferme proprement")
    except Exception as exc:
        print(f"  fermeture : {exc}")
