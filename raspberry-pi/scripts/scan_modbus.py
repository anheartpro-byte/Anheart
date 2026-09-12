"""Find the drive on the bus: scan slave address, then baud rate and parity.

READ-ONLY. Never writes.

"No response" has several very different causes and guessing between them wastes
bench time. This turns the question into evidence:

  * a response at some address  -> the drive is alive; Add is simply not 1
  * a response at another baud  -> tbr/tFO do not match
  * nothing anywhere           -> unpowered, or D0/D1 swapped, or Add = 0
                                  (address 0 is the Modbus broadcast address and
                                  a slave NEVER answers it, which is exactly why
                                  the factory default makes the drive look dead)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymodbus.client import ModbusSerialClient

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM3"
ETA = 3201
FAST_TIMEOUT = 0.12

# Factory format first, then the plausible alternatives.
FORMATS = [
    (19200, "E", 1),
    (19200, "N", 1),
    (19200, "O", 1),
    (9600, "E", 1),
    (9600, "N", 1),
    (38400, "E", 1),
    (4800, "E", 1),
]


def try_one(baud: int, parity: str, stop: int, slaves: range, timeout: float) -> list[int]:
    hits: list[int] = []
    client = ModbusSerialClient(
        port=PORT,
        baudrate=baud,
        bytesize=8,
        parity=parity,
        stopbits=stop,
        timeout=timeout,
        retries=1,
    )
    if not client.connect():
        print(f"  {baud} 8{parity}{stop}: port refuse de s'ouvrir")
        return hits
    try:
        for slave in slaves:
            try:
                rr = client.read_holding_registers(address=ETA, count=1, slave=slave)
            except Exception:  # noqa: S112  # a silent slave is the expected case here
                continue
            if not rr.isError():
                print(
                    f"  >>> REPONSE  baud={baud} 8{parity}{stop} addr={slave} "
                    f"ETA={rr.registers[0]} (0x{rr.registers[0]:04X})"
                )
                hits.append(slave)
    finally:
        client.close()
    return hits


print(f"port {PORT}\n")
print("=== phase 1 : balayage des adresses 1..247 au format usine 19200 8E1 ===")
found = try_one(19200, "E", 1, range(1, 248), FAST_TIMEOUT)
if not found:
    print("  aucune reponse sur les 247 adresses\n")
    print("=== phase 2 : autres formats serie, adresses 1..8 ===")
    for baud, parity, stop in FORMATS[1:]:
        hits = try_one(baud, parity, stop, range(1, 9), FAST_TIMEOUT)
        found.extend(hits)

print()
if found:
    print("=> VARIATEUR TROUVE. Notez l'adresse et le format ci-dessus.")
else:
    print("=> RIEN sur aucune adresse ni aucun format teste.")
    print("   Il ne reste que des causes physiques ou de parametrage clavier :")
    print("     1. Add = 0 (defaut usine). Address 0 = broadcast : un esclave")
    print("        n'y repond JAMAIS. C'est la cause la plus frequente.")
    print("     2. Variateur hors tension (ecran eteint).")
    print("     3. D0 et D1 inverses sur le RJ45 (broches 4 et 5).")
    print("     4. Masse (broche 8) non reliee.")
