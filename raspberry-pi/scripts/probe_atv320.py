"""READ-ONLY probe of the ATV320 over Modbus RTU.

    python scripts/probe_atv320.py ftdi://schneider:rs485/1      # macOS, Schneider cable
    python scripts/probe_atv320.py /dev/ttyUSB0                   # Pi
    python scripts/probe_atv320.py COM3 248                       # Windows, explicit slave

Writes NOTHING. Reads a few status registers and tries both candidate register
offsets, so we can tell apart three very different situations that all look the
same from the outside:
  * the serial port itself does not open       -> driver / port problem
  * the port opens but the drive never answers -> drive unpowered, or Add=0
  * the drive answers                          -> we can read its real state

Deliberately no writes: a misaddressed write could land in a live Altivar
parameter such as ACC or HSP and silently remove a safety ceiling.

The client comes from ``src.motor.atv320.serial_master``, the one place the
application builds one: an ``ftdi://`` port gets the buffered FTDI transport
(``src.motor.ftdi_link``) whose ``in_waiting`` works, so a read takes tens of
milliseconds instead of the full serial timeout pyftdi's own port costs.
"""

from __future__ import annotations

import os
import sys
import time

# Run from anywhere: put the project root (parent of scripts/) on the path,
# same convention as scripts/diagnose_columns.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clock import RealClock
from src.motor.atv320 import SerialSettings, serial_master
from src.motor.drive import (
    ACC_LOGICAL,
    DEC_LOGICAL,
    ETA_LOGICAL,
    HSP_LOGICAL,
    LCR_LOGICAL,
    LFRD_LOGICAL,
    LFT_LOGICAL,
    LSP_LOGICAL,
    RFRD_LOGICAL,
    TFR_LOGICAL,
)
from src.motor.drive_process_lock import DriveOwnershipError
from src.units import Seconds

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
# 248 is the Schneider point-to-point access address: the drive answers on it
# whatever its configured Add is, which is how SoMove finds a drive whose
# address is unknown. Measured on this bench: 248 answers, 1 does not.
SLAVE = int(sys.argv[2]) if len(sys.argv) > 2 else 248

# Altivar logical addresses (read-only ones only), from src/motor/drive.py so
# the probe and the application cannot disagree. Bench, offset 0:
#   tFr@3103 = 600 (60.0 Hz), HSP@3104 = 500 (50.0 Hz), LSP@3105 = 0,
#   ACC@9001 = 30, dEC@9002 = 30 (0.1 s), LFT@7121.
REGS = {
    "ETA": ETA_LOGICAL,
    "RFRD": RFRD_LOGICAL,
    "LCR": LCR_LOGICAL,
    "LFT": LFT_LOGICAL,
    "LFRD_echo": LFRD_LOGICAL,
    "tFr": TFR_LOGICAL,
    "HSP": HSP_LOGICAL,
    "LSP": LSP_LOGICAL,
    "ACC": ACC_LOGICAL,
    "dEC": DEC_LOGICAL,
}

# 1 s rather than the application's 0.1 s: a probe is where a slow link should
# still answer so it can be diagnosed. With the ftdi:// transport a healthy
# read returns long before this.
settings = SerialSettings(port=PORT, slave_address=SLAVE, timeout=Seconds(1.0))
master = serial_master(settings, RealClock())

print(f"ouverture de {PORT} a 19200 8E1, esclave {SLAVE} ...")
try:
    connected = master.connect()
except DriveOwnershipError as error:
    print(f"  REFUS: {error}")
    raise SystemExit(3) from error
if not connected:
    print("  ECHEC: le port ne s'ouvre pas (occupe par SoMove ? pilote ? libusb ?)")
    raise SystemExit(3)
print("  port OUVERT")

answered = False
for offset in (0, -1):
    print(f"\n--- essai avec decalage de registre {offset:+d} ---")
    for name, base in REGS.items():
        addr = base + offset
        started = time.monotonic()
        try:
            rr = master.read_holding_registers(addr, count=1, slave=SLAVE)
        except Exception as exc:
            print(f"  {name:<10} @{addr:<5} EXCEPTION {type(exc).__name__}: {exc}")
            continue
        ms = (time.monotonic() - started) * 1000.0
        registers = getattr(rr, "registers", None)
        is_error = getattr(rr, "isError", None)
        if isinstance(rr, Exception) or (callable(is_error) and is_error()) or not registers:
            print(f"  {name:<10} @{addr:<5} pas de reponse / erreur ({ms:.0f} ms): {rr}")
        else:
            raw = registers[0]
            signed = raw - 0x10000 if raw > 32767 else raw
            print(f"  {name:<10} @{addr:<5} = {raw} (0x{raw:04X}, signe {signed}) [{ms:.0f} ms]")
            answered = True

master.close()
print()
if answered:
    print("=> LE VARIATEUR REPOND. On peut decoder son etat.")
else:
    print("=> Port OK mais AUCUNE reponse du variateur.")
    print("   Causes, par ordre de probabilite :")
    print("     1. Mauvaise adresse esclave (248 = point a point Schneider).")
    print("     2. Variateur hors tension.")
    print("     3. tbr/tFO differents de 19200 / 8E1.")
    print("     4. Cablage RJ45 broches 4/5/8, ou A et B inverses.")
