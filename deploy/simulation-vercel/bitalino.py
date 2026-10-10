"""Placeholder for the ``bitalino`` package on the hosted simulation.

``src/bitalino_client.py`` imports ``BITalino`` at module load. The real
package needs a Bluetooth stack that a hosted function does not have, and the
simulation never opens a device: it uses ``src.sim.bitalino``. This stand-in
lets the import succeed and refuses loudly if anything ever tries to connect.
"""

from __future__ import annotations


class BITalino:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "No BITalino on the hosted simulation: this process only runs simulated devices."
        )
