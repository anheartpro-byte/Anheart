"""The training session: its vocabulary, its plan, its control law, its limits.

Layout, and the rule that keeps it honest:

* ``types.py`` is the **vocabulary**. Enums, frozen records and the invariants
  written onto them - no control logic, no I/O, no clock. Everything else in
  this package, plus the runtime and the web UI, imports its names from there,
  so "phase", "safety action", "heart-rate sample" and "telemetry" mean one
  thing in this system rather than one thing per module.
* the plan, the control law and the safety supervisor are built on top of it.
  They may import ``types``; ``types`` imports none of them. That direction is
  what stops the vocabulary from acquiring policy.

``types.py`` is allowed to import ``src.motor.drive`` because that module is
the pure drive *seam* (no ``pymodbus``, no ``serial``): telemetry has to name
the drive's own state and fault vocabulary, and re-describing them here would
create a second set of names that could disagree with the drive.

Deliberately no re-exports here. One blessed import path per symbol
(``from src.training.types import Phase``) keeps grep honest about who depends
on what, and it means adding a sibling module never requires editing this file.

See .claude/skills/anheart-strict-python/SKILL.md.
"""
