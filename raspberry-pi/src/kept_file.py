"""Reading a file this console keeps, or ships: never through a link, never waiting on a
FIFO, never a file over its bound read whole (ANH-149).

A leaf, so that every module that reads such a file can, :mod:`src.medical` included,
which :mod:`src.console_access` imports.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Final

# POSIX: a link in a kept file's place is refused, and a FIFO is never waited on.
_KEPT_FLAGS: Final[int] = (
    os.O_RDONLY if sys.platform == "win32" else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
)


def read_kept(path: Path, limit: int) -> bytes:
    """What a file this console keeps holds. Raises ``OSError``: ``FileNotFoundError``
    when there is none; another for a link, a FIFO, a directory or anything but a
    regular file in its place, and for a file over ``limit`` bytes, never read whole."""
    descriptor = os.open(path, _KEPT_FLAGS)
    content = b""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        while len(content) <= limit:
            chunk = os.read(descriptor, limit + 1 - len(content))
            if not chunk:
                break
            content += chunk
    finally:
        os.close(descriptor)
    if len(content) > limit:
        raise OSError(errno.EFBIG, "file too large")
    return content
