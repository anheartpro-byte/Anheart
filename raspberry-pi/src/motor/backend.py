"""The drive protocol, under the import path it was first given.

:class:`~src.motor.drive.DriveBackend` is defined in :mod:`src.motor.drive`, next to
the types every one of its methods speaks. This module only gives it back under
its earlier path, so that an import written against it keeps resolving to the
same class.
"""

from __future__ import annotations

from src.motor.drive import DriveBackend

__all__ = ["DriveBackend"]
