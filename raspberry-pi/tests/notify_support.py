"""A service manager's side of the notification socket, for the tests of ANH-162.

systemd listens on a local datagram socket and hands its path to the service
as ``NOTIFY_SOCKET``. :class:`Listener` is that socket, real: what the console
sends is read here exactly as systemd would receive it. The fixtures that make
one are in ``conftest.py`` (``listener``, ``short_directory``).
"""

from __future__ import annotations

import socket
from pathlib import Path


class Listener:
    """Bound, never blocking, read on demand."""

    def __init__(self, directory: Path) -> None:
        self.path: Path = directory / "notify"
        self.socket: socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path))
        self.socket.setblocking(False)

    def heard(self) -> list[bytes]:
        """Every datagram waiting, in order."""
        taken: list[bytes] = []
        while True:
            try:
                taken.append(self.socket.recv(4096))
            except BlockingIOError:
                return taken

    def close(self) -> None:
        self.socket.close()
