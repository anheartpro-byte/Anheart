"""READ-ONLY probe of the ATV320 over Modbus RTU.

Writes NOTHING. Reads a few status registers and tries both candidate register
offsets, so we can tell apart three very different situations that all look the
same from the outside:
  * the serial port itself does not open       -> driver / port problem
  * the port opens but the drive never answers -> drive unpowered, or Add=0
  * the drive answers                          -> we can read its real state

Deliberately no writes: a misaddressed write could land in a live Altivar
parameter such as ACC or HSP and silently remove a safety ceiling.
"""

import os
import sys

# Run from anywhere: put the project root (parent of scripts/) on the path,
# same convention as scripts/diagnose_columns.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect
import sys

from pymodbus.client import ModbusSerialClient

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
SLAVE = 1

# Altivar logical addresses (read-only ones only).
REGS = {"ETA": 3201, "RFRD": 8604, "LCR": 3204, "LFT": 7121, "LFRD_echo": 8602}

print(f"pymodbus signature : {inspect.signature(ModbusSerialClient.__init__)}")
print(f"read_holding_registers : {inspect.signature(ModbusSerialClient.read_holding_registers)}")
print()

client = ModbusSerialClient(
    port=PORT, baudrate=19200, bytesize=8, parity="E", stopbits=1, timeout=1.0
)

print(f"ouverture de {PORT} a 19200 8E1 ...")
if not client.connect():
    print("  ECHEC: le port ne s'ouvre pas (occupe par SoMove ? pilote ?)")
    raise SystemExit(3)
print("  port OUVERT")

answered = False
for offset in (0, -1):
    print(f"\n--- essai avec decalage de registre {offset:+d} ---")
    for name, base in REGS.items():
        addr = base + offset
        try:
            rr = client.read_holding_registers(address=addr, count=1, slave=SLAVE)
        except Exception as exc:
            print(f"  {name:<10} @{addr:<5} EXCEPTION {type(exc).__name__}: {exc}")
            continue
        if rr.isError():
            print(f"  {name:<10} @{addr:<5} pas de reponse / erreur: {rr}")
        else:
            raw = rr.registers[0]
            signed = raw - 0x10000 if raw > 32767 else raw
            print(f"  {name:<10} @{addr:<5} = {raw} (0x{raw:04X}, signe {signed})")
            answered = True

client.close()
print()
if answered:
    print("=> LE VARIATEUR REPOND. On peut decoder son etat.")
else:
    print("=> Port OK mais AUCUNE reponse du variateur.")
    print("   Causes, par ordre de probabilite :")
    print("     1. Add = 0 (defaut) -> le variateur ignore le Modbus. A mettre a 1.")
    print("     2. Variateur hors tension.")
    print("     3. tbr/tFO differents de 19200 / 8E1.")
    print("     4. Cablage RJ45 broches 4/5/8, ou A et B inverses.")
