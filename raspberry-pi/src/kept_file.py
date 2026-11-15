"""Reading a file this console keeps, or ships: never through a link, never waiting on a
FIFO, never a file over its bound read whole (ANH-149).

A leaf, so that every module that reads such a file can, :mod:`src.medical` included,
which :mod:`src.console_access` imports. A file read as a stream (a record's ticks, a
raw block) is opened the same way (:func:`open_kept`), its reader bounding what it reads.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from io import BufferedReader
from pathlib import Path
from typing import Final

# POSIX: a link in a kept file's place is refused, and a FIFO is never waited on.
_KEPT_FLAGS: Final[int] = (
    os.O_RDONLY if sys.platform == "win32" else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
)


def open_kept(path: Path) -> BufferedReader:
    """``path`` opened to be read, only when it is a regular file. Raises ``OSError``:
    ``FileNotFoundError`` when there is none; another for a link, a FIFO, a directory or
    anything but a regular file in its place, never waited on."""
    descriptor = os.open(path, _KEPT_FLAGS)
    try:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError:
        os.close(descriptor)
        raise
    if not regular:
        os.close(descriptor)
        raise OSError(errno.EINVAL, "not a regular file")
    return os.fdopen(descriptor, "rb")


def read_kept(path: Path, limit: int) -> bytes:
    """What a file this console keeps holds, opened as :func:`open_kept` opens it. Raises
    ``OSError``, as it does, and for a file over ``limit`` bytes, never read whole."""
    with open_kept(path) as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise OSError(errno.EFBIG, "file too large")
    return content
