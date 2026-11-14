"""What the first-power-up screen shows and asks (ANH-115), apart from how it is done.

The web layer reads this; :mod:`src.pairing` does it. Kept apart as :mod:`src.config_view`
is: the console's configuration (:mod:`src.local_config`) is read by the web layer's
setup, and must not depend on the module that pairs.
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Protocol

from src.result import Result


@dataclass(frozen=True, slots=True)
class PendingStep:
    """A step of the commissioning this console does not do yet, with the ticket that will."""

    id: str
    label: str
    detail: str


@dataclass(frozen=True, slots=True)
class PairingView:
    """What the screen shows: the Pi, where it asks, and a pairing already done."""

    serial: str | None
    serial_problem: str | None
    address: str | None
    """``None``: the screen asks for the dashboard's address."""

    key_prefix: str | None
    """The key's prefix once paired; the console then restarts at rest."""

    steps: tuple[PendingStep, ...]


@dataclass(frozen=True, slots=True)
class PairingDone:
    """A pairing written: the key's prefix, and the steps that follow."""

    key_prefix: str
    steps: tuple[PendingStep, ...]


class PairingScreen(Protocol):
    """What ``/api/pairing`` asks of a console with no key (:class:`~src.pairing.PairingDesk`)."""

    @abstractmethod
    def view(self) -> PairingView:
        """What the screen shows now."""

    @abstractmethod
    async def pair(self, code: str, typed_address: str | None) -> Result[PairingDone, str]:
        """Send ``code``, write what the dashboard gives, ask for the restart; or why not."""
