"""A path named from outside, kept inside the directory it may designate.

A name that comes from a request is not a path yet. :func:`resolve_under`
joins it to the directory it is allowed to designate something in, follows
every link, and gives the result back only when it is still inside that
directory. The caller reads the path it gets back and never joins the name it
was given to anything again.
"""

import os
from pathlib import Path


def resolve_under(root: Path, requested: str) -> Path | None:
    """What ``requested`` designates under ``root``, links followed; ``None`` when it leaves it.

    ``root`` itself is not under ``root``: a request names something in it.
    The path need not exist. Never raises: a name no file can carry (a NUL
    byte, an unencodable character) designates nothing.
    """
    try:
        base = os.path.realpath(root)
        # `os.path` on purpose. `realpath`, then `startswith` on what it gave
        # back, is the form the static analysis of this repository recognises
        # as a confinement; `Path.resolve` and `is_relative_to` say the same
        # and are not recognised.
        target = os.path.realpath(os.path.join(base, requested))  # noqa: PTH118
    except ValueError:
        return None
    if target == base:
        return None
    # The separator ends the prefix: `records-old` is not under `records`.
    if target.startswith(os.path.join(base, "")):  # noqa: PTH118
        return Path(target)
    return None
