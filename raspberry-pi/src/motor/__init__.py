"""Motor control: the drive seam and the things that implement it.

Layout, and the rule that keeps it honest:

* ``drive.py`` is the **seam**. It is pure - no ``pymodbus``, no ``serial``,
  nothing that touches a wire - and it owns the CiA402 state decoding, the
  command words, the fault vocabulary, the register map and the closed
  ``DriveError`` union. A test parses its imports and fails if hardware ever
  creeps in.
* ``atv320.py`` is the only module in the whole codebase allowed to import
  ``pymodbus`` or ``serial`` (contract rule 5). It implements
  ``drive.DriveBackend`` and nothing else in ``src/`` may import it directly.
* the simulator under ``src/sim/`` implements the same protocol, which is what
  lets the closed-loop safety tests run a 45-minute session in under a second
  with no drive attached, exercising the same types the hardware path uses.

Deliberately no re-exports here. One blessed import path per symbol
(``from src.motor.drive import DriveState``) keeps grep honest about who
depends on what, and it means adding a sibling module never requires editing
this file.

See .claude/skills/anheart-strict-python/SKILL.md.
"""
